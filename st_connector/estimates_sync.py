"""
Estimates sync — ST → Smartsheet Estimates sheet (created on first run).
Runs every 15 minutes.
"""

import logging
import time

from . import st_api, smartsheet_client as ss
from .config import (
    ESTIMATES_SHEET_NAME, ESTIMATES_COLUMNS, SYNC_INTERVALS,
)

log = logging.getLogger(__name__)

_sheet_id: int | None = None
_col_ids:  dict       = {}


def _ensure_sheet() -> int:
    global _sheet_id
    if _sheet_id:
        return _sheet_id
    _sheet_id = ss.get_or_create_sheet(ESTIMATES_SHEET_NAME, ESTIMATES_COLUMNS)
    return _sheet_id


def _resolve_cols(sheet: dict) -> None:
    global _col_ids
    if _col_ids:
        return
    _col_ids = {c["title"]: c["id"] for c in sheet.get("columns", [])}


def _col(title: str) -> int | None:
    return _col_ids.get(title)


def _st_link(estimate_id) -> str:
    return f"https://go.servicetitan.com/#/new/sales/estimates/{estimate_id}"


def _build_cells(est: dict) -> list:
    job = est.get("job") or {}
    cells = []

    def _add(title, value):
        col = _col(title)
        if col:
            cells.append(ss.cell(col, value))

    _add("Estimate ID",   est.get("id"))
    _add("Estimate #",    est.get("number") or est.get("name"))
    _add("Job ID",        est.get("jobId") or job.get("id"))
    _add("Job #",         est.get("jobNumber") or job.get("number"))
    _add("Customer Name", (est.get("customer") or {}).get("name"))
    _add("Status",        est.get("status"))
    _add("Total",         est.get("total") or (est.get("summary") or {}).get("total"))
    _add("Created Date",  (est.get("createdOn") or "")[:10] or None)
    _add("Sold Date",     (est.get("soldOn") or "")[:10] or None)
    _add("Business Unit", (est.get("businessUnit") or {}).get("name"))
    _add("Technician",    (est.get("technician") or {}).get("name"))
    _add("Notes",         est.get("notes") or est.get("summary", {}).get("notes") if isinstance(est.get("summary"), dict) else None)

    link_col = _col("ST Link")
    if link_col:
        cells.append(ss.cell_hyperlink(link_col, "View Estimate", _st_link(est.get("id"))))

    return cells


def sync_once() -> None:
    log.info("[estimates] sync start")
    try:
        sheet_id = _ensure_sheet()
        sheet    = ss.get_sheet(sheet_id)
        _resolve_cols(sheet)

        key_col = _col("Estimate ID")
        if not key_col:
            log.error("[estimates] Estimate ID column missing — aborting")
            return

        existing = ss.get_cell_values(sheet, key_col, list(_col_ids.values()))

        estimates = st_api.fetch_estimates()
        log.info("[estimates] fetched %d from ST", len(estimates))

        to_add    = []
        to_update = []

        for est in estimates:
            est_id = str(est.get("id", ""))
            if not est_id:
                continue

            cells = _build_cells(est)

            if est_id in existing:
                row_data = existing[est_id]
                changed  = any(
                    str(c.get("value", "") or "") != str(row_data.get(c["columnId"], "") or "")
                    for c in cells
                    if c["columnId"] != key_col
                )
                if changed:
                    to_update.append({"id": row_data["_row_id"], "cells": cells})
            else:
                to_add.append({"cells": cells, "toBottom": True})

        if to_add:
            ss.add_rows(sheet_id, to_add)
            log.info("[estimates] added %d rows", len(to_add))
        if to_update:
            ss.update_rows(sheet_id, to_update)
            log.info("[estimates] updated %d rows", len(to_update))

        log.info("[estimates] sync complete")
    except Exception as e:
        log.error("[estimates] sync error: %s", e, exc_info=True)


def run_loop() -> None:
    interval = SYNC_INTERVALS["estimates"]
    while True:
        sync_once()
        time.sleep(interval)


# ===========================================================================
# Write-back  (Smartsheet → ST)
# Gated by ST Developer App write grant: tn.sal.estimates:w
# ===========================================================================

def writeback_to_st(changes: list[dict]) -> None:
    """
    Push Smartsheet estimate changes back to ST.

    Each item in ``changes``:
      {
        "estimate_id": <ST estimate ID>,
        "field":       <field name, e.g. "status">,
        "new_value":   <value to write>,
      }

    Silently skipped if tn.sal.estimates:w is not in the token scopes.
    """
    from . import capabilities
    if not capabilities.can_write("estimates"):
        log.debug("[estimates] write-back skipped — write scope not granted")
        return

    by_id: dict[str, dict] = {}
    for ch in changes:
        eid = str(ch.get("estimate_id", "") or ch.get("st_id", ""))
        if not eid:
            continue
        by_id.setdefault(eid, {})[ch["field"]] = ch.get("new_value", ch.get("value"))

    # Only sell/dismiss status transitions are writable
    _WRITABLE = {"status"}
    for eid, fields in by_id.items():
        payload = {k: v for k, v in fields.items() if k in _WRITABLE}
        if not payload:
            continue
        try:
            st_api.patch("sales", f"estimates/{eid}", payload)
            log.info("[estimates] patched %s: %s", eid, list(payload))
        except Exception as exc:
            log.error("[estimates] patch failed for estimate %s: %s", eid, exc)
