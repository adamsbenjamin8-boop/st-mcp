"""
Estimates sync — ST → Smartsheet Estimates sheet.
Runs every 15 minutes.
Sheet ID 1600229966040964 (workspace: Service Titan Shuttle connection).
"""

import logging
import time
from datetime import datetime, timezone, timedelta

from . import st_api, smartsheet_client as ss
from .config import ESTIMATES_SHEET_ID, ESTIMATES_COLS, SYNC_INTERVALS

log = logging.getLogger(__name__)

_COL = ESTIMATES_COLS
_KEY = _COL["estimate_id"]


def _st_link(estimate_id) -> str:
    return f"https://go.servicetitan.com/#/new/sales/estimates/{estimate_id}"


def _location_str(est: dict) -> str | None:
    """Best-effort address string from estimate or its linked job."""
    for src in (est, est.get("job") or {}):
        loc  = src.get("location") or {}
        addr = loc.get("address") or loc
        if isinstance(addr, dict):
            parts = ", ".join(filter(None, [
                addr.get("street"), addr.get("city"), addr.get("state"),
            ]))
            if parts:
                return parts
    return None


def _build_cells(est: dict) -> list:
    job = est.get("job") or {}
    bu  = est.get("businessUnit") or {}
    tech = est.get("technician") or {}
    cells = [
        ss.cell(_COL["estimate_id"],      est.get("id")),
        ss.cell(_COL["estimate_num"],     est.get("number") or est.get("name")),
        ss.cell(_COL["job_id"],           est.get("jobId") or job.get("id")),
        ss.cell(_COL["job_num"],          est.get("jobNumber") or job.get("number")),
        ss.cell(_COL["customer_name"],    (est.get("customer") or {}).get("name")),
        ss.cell(_COL["status"],           est.get("status")),
        ss.cell(_COL["total"],            est.get("total") or (est.get("summary") or {}).get("total")),
        ss.cell(_COL["created_date"],     (est.get("createdOn") or "")[:10] or None),
        ss.cell(_COL["sold_date"],        (est.get("soldOn") or "")[:10] or None),
        ss.cell(_COL["business_unit"],    bu.get("name")),
        ss.cell(_COL["business_unit_id"], bu.get("id")),
        ss.cell(_COL["technician"],       tech.get("name")),
        ss.cell(_COL["location_address"], _location_str(est)),
        ss.cell(_COL["notes"],            est.get("notes")),
        ss.cell_hyperlink(_COL["st_link"], "View Estimate", _st_link(est.get("id"))),
    ]
    return cells


def sync_once(*, backfill: bool = False) -> None:
    log.info("[estimates] sync start%s", " (backfill)" if backfill else "")
    try:
        sheet    = ss.get_sheet(ESTIMATES_SHEET_ID)
        existing = ss.get_cell_values(sheet, _KEY, list(_COL.values()))

        modified_after = None
        progress_cb = None
        if not backfill:
            interval = SYNC_INTERVALS["estimates"]
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=interval * 2)
            modified_after = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            def progress_cb(fetched, total):
                log.info("[estimates] Backfill: %d/%s records fetched",
                         fetched, total if total is not None else "?")

        estimates = st_api.fetch_estimates(
            modified_after=modified_after,
            progress_cb=progress_cb,
        )
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
                    if c["columnId"] != _KEY
                )
                if changed:
                    to_update.append({"id": row_data["_row_id"], "cells": cells})
            else:
                to_add.append({"cells": cells, "toBottom": True})

        if to_add:
            ss.add_rows(ESTIMATES_SHEET_ID, to_add)
            log.info("[estimates] added %d rows", len(to_add))
        if to_update:
            ss.update_rows(ESTIMATES_SHEET_ID, to_update)
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

    Only sell/dismiss status transitions are written; all other fields ignored.
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
