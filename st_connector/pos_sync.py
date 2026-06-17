"""
Purchase Orders sync — ST → Smartsheet Purchase Orders sheet.
Runs every 15 minutes.
Sheet ID 7840808857194372 (workspace: Service Titan Shuttle connection).
"""

import logging
import time
from datetime import datetime, timezone, timedelta

from . import st_api, smartsheet_client as ss
from .config import POS_SHEET_ID, POS_COLS, SYNC_INTERVALS

log = logging.getLogger(__name__)

_COL = POS_COLS
_KEY = _COL["po_id"]


def _st_link(po_id) -> str:
    return f"https://go.servicetitan.com/#/new/inventory/purchase-orders/details/{po_id}"


def _po_total(po: dict) -> float | None:
    try:
        items = po.get("items") or []
        total = sum(
            float(i.get("cost", 0) or 0) * float(i.get("quantity", 1) or 1)
            for i in items
        )
        return total or None
    except Exception:
        return None


def _ship_to_str(po: dict) -> str | None:
    """Extract ship-to location as a string from various possible field shapes."""
    raw = po.get("shipTo") or po.get("shipToAddress") or po.get("shippingAddress")
    if not raw:
        return None
    if isinstance(raw, str):
        return raw or None
    if isinstance(raw, dict):
        # Could be a location object or address object
        name = raw.get("name") or raw.get("description")
        if name:
            return name
        parts = ", ".join(filter(None, [
            raw.get("street"), raw.get("city"), raw.get("state"),
        ]))
        return parts or None
    return None


def _build_cells(po: dict) -> list:
    job    = po.get("job") or {}
    vendor = po.get("vendor") or {}
    bu     = po.get("businessUnit") or {}
    cells = [
        ss.cell(_COL["po_id"],            po.get("id")),
        ss.cell(_COL["po_num"],           po.get("number") or str(po.get("id", ""))),
        ss.cell(_COL["job_id"],           po.get("jobId") or job.get("id")),
        ss.cell(_COL["job_num"],          po.get("jobNumber") or job.get("number")),
        ss.cell(_COL["vendor"],           vendor.get("name") or po.get("vendorName")),
        ss.cell(_COL["vendor_id"],        vendor.get("id") or po.get("vendorId")),
        ss.cell(_COL["status"],           po.get("status")),
        ss.cell(_COL["total"],            _po_total(po)),
        ss.cell(_COL["created_date"],     (po.get("date") or po.get("createdOn") or "")[:10] or None),
        ss.cell(_COL["required_date"],    (po.get("requiredOn") or "")[:10] or None),
        ss.cell(_COL["business_unit"],    bu.get("name")),
        ss.cell(_COL["business_unit_id"], bu.get("id")),
        ss.cell(_COL["ship_to"],          _ship_to_str(po)),
        ss.cell(_COL["notes"],            po.get("notes") or po.get("comment")),
        ss.cell_hyperlink(_COL["st_link"], "View PO", _st_link(po.get("id"))),
    ]
    return cells


def sync_once(*, backfill: bool = False) -> None:
    log.info("[pos] sync start%s", " (backfill)" if backfill else "")
    try:
        sheet    = ss.get_sheet(POS_SHEET_ID)
        existing = ss.get_cell_values(sheet, _KEY, list(_COL.values()))

        modified_after = None
        progress_cb = None
        if not backfill:
            interval = SYNC_INTERVALS["pos"]
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=interval * 2)
            modified_after = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            def progress_cb(fetched, total):
                log.info("[pos] Backfill: %d/%s records fetched",
                         fetched, total if total is not None else "?")

        pos = st_api.fetch_purchase_orders(
            modified_after=modified_after,
            progress_cb=progress_cb,
        )
        log.info("[pos] fetched %d from ST", len(pos))

        to_add    = []
        to_update = []
        to_delete = []

        for po in pos:
            po_id = str(po.get("id", ""))
            if not po_id:
                continue

            if po.get("status") in ("Canceled", "Received"):
                if po_id in existing:
                    to_delete.append(existing[po_id]["_row_id"])
                continue

            cells = _build_cells(po)

            if po_id in existing:
                row_data = existing[po_id]
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
            ss.add_rows(POS_SHEET_ID, to_add)
            log.info("[pos] added %d rows", len(to_add))
        if to_update:
            ss.update_rows(POS_SHEET_ID, to_update)
            log.info("[pos] updated %d rows", len(to_update))
        if to_delete:
            ss.delete_rows(POS_SHEET_ID, to_delete)
            log.info("[pos] deleted %d inactive rows", len(to_delete))

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
