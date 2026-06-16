"""
Purchase Orders sync — ST → Smartsheet POs sheet (created on first run).
Runs every 15 minutes.
"""

import logging
import time

from . import st_api, smartsheet_client as ss
from .config import (
    POS_SHEET_NAME, POS_COLUMNS, SYNC_INTERVALS,
)

log = logging.getLogger(__name__)

_sheet_id: int | None = None
_col_ids:  dict       = {}


def _ensure_sheet() -> int:
    global _sheet_id
    if _sheet_id:
        return _sheet_id
    _sheet_id = ss.get_or_create_sheet(POS_SHEET_NAME, POS_COLUMNS)
    return _sheet_id


def _resolve_cols(sheet: dict) -> None:
    global _col_ids
    if _col_ids:
        return
    _col_ids = {c["title"]: c["id"] for c in sheet.get("columns", [])}


def _col(title: str) -> int | None:
    return _col_ids.get(title)


def _st_link(po_id) -> str:
    return f"https://go.servicetitan.com/#/new/inventory/purchase-orders/details/{po_id}"


def _po_total(po: dict) -> float | None:
    try:
        items = po.get("items") or []
        return sum(
            float(i.get("cost", 0) or 0) * float(i.get("quantity", 1) or 1)
            for i in items
        ) or None
    except Exception:
        return None


def _build_cells(po: dict) -> list:
    job = po.get("job") or {}
    cells = []

    def _add(title, value):
        col = _col(title)
        if col:
            cells.append(ss.cell(col, value))

    _add("PO ID",         po.get("id"))
    _add("PO #",          po.get("number") or str(po.get("id", "")))
    _add("Job ID",        po.get("jobId") or job.get("id"))
    _add("Job #",         po.get("jobNumber") or job.get("number"))
    _add("Vendor",        (po.get("vendor") or {}).get("name") or po.get("vendorName"))
    _add("Status",        po.get("status"))
    _add("Total",         _po_total(po))
    _add("Created Date",  (po.get("date") or po.get("createdOn") or "")[:10] or None)
    _add("Required Date", (po.get("requiredOn") or "")[:10] or None)
    _add("Business Unit", (po.get("businessUnit") or {}).get("name"))

    link_col = _col("ST Link")
    if link_col:
        cells.append(ss.cell_hyperlink(link_col, "View PO", _st_link(po.get("id"))))

    return cells


def sync_once() -> None:
    log.info("[pos] sync start")
    try:
        sheet_id = _ensure_sheet()
        sheet    = ss.get_sheet(sheet_id)
        _resolve_cols(sheet)

        key_col = _col("PO ID")
        if not key_col:
            log.error("[pos] PO ID column missing — aborting")
            return

        existing = ss.get_cell_values(sheet, key_col, list(_col_ids.values()))

        pos = st_api.fetch_purchase_orders()
        log.info("[pos] fetched %d from ST", len(pos))

        to_add    = []
        to_update = []

        for po in pos:
            po_id = str(po.get("id", ""))
            if not po_id:
                continue

            cells = _build_cells(po)

            if po_id in existing:
                row_data = existing[po_id]
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
            log.info("[pos] added %d rows", len(to_add))
        if to_update:
            ss.update_rows(sheet_id, to_update)
            log.info("[pos] updated %d rows", len(to_update))

        log.info("[pos] sync complete")
    except Exception as e:
        log.error("[pos] sync error: %s", e, exc_info=True)


def run_loop() -> None:
    interval = SYNC_INTERVALS["pos"]
    while True:
        sync_once()
        time.sleep(interval)


# ===========================================================================
# Write-back  (Smartsheet → ST)
# Gated by ST Developer App write grant: tn.inv.purchaseorders:w
# ===========================================================================

def writeback_to_st(changes: list[dict]) -> None:
    """
    Push Smartsheet PO changes back to ST.

    Each item in ``changes``:
      {
        "po_id":     <ST PO ID>,
        "field":     <field name, e.g. "status">,
        "new_value": <value to write>,
      }

    Silently skipped if tn.inv.purchaseorders:w is not in the token scopes.
    """
    from . import capabilities
    if not capabilities.can_write("pos"):
        log.debug("[pos] write-back skipped — write scope not granted")
        return

    by_id: dict[str, dict] = {}
    for ch in changes:
        pid = str(ch.get("po_id", "") or ch.get("st_id", ""))
        if not pid:
            continue
        by_id.setdefault(pid, {})[ch["field"]] = ch.get("new_value", ch.get("value"))

    _WRITABLE = {"status", "requiredOn"}
    for pid, fields in by_id.items():
        payload = {k: v for k, v in fields.items() if k in _WRITABLE}
        if not payload:
            continue
        try:
            st_api.patch("inventory", f"purchase-orders/{pid}", payload)
            log.info("[pos] patched %s: %s", pid, list(payload))
        except Exception as exc:
            log.error("[pos] patch failed for PO %s: %s", pid, exc)
