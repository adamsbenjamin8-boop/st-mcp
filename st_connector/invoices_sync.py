"""
Invoices sync — ST → Smartsheet Invoice Report KPI sheet.
Runs every 15 minutes.
Removes rows when Invoice Status = "Exported".
"""

import logging
import time
from datetime import datetime, timezone, timedelta

from . import st_api, smartsheet_client as ss
from .config import INVOICE_SHEET_ID, INVOICE_COLS, SYNC_INTERVALS

log = logging.getLogger(__name__)

_COL = INVOICE_COLS
_KEY = _COL["invoice_id"]


def _build_cells(inv: dict) -> list:
    job = inv.get("job") or {}
    return [
        ss.cell(_COL["invoice_id"],    inv.get("id")),
        ss.cell(_COL["invoice_num"],   inv.get("number")),
        ss.cell(_COL["job_id"],        job.get("id") or inv.get("jobId")),
        ss.cell(_COL["job_num"],       job.get("number") or inv.get("jobNumber")),
        ss.cell(_COL["customer_name"], (inv.get("customer") or {}).get("name")),
        ss.cell(_COL["status"],        inv.get("status")),
        ss.cell(_COL["invoice_date"],  (inv.get("invoiceDate") or "")[:10] or None),
        ss.cell(_COL["total"],         inv.get("total")),
        ss.cell(_COL["balance"],       inv.get("balance")),
        ss.cell(_COL["batch_number"],  inv.get("batchNumber")),
        ss.cell(_COL["export_id"],     inv.get("exportId")),
        ss.cell(_COL["business_unit"], (inv.get("businessUnit") or {}).get("name")),
        ss.cell(_COL["labor_costs"],   inv.get("laborCost")),
        ss.cell(_COL["material_costs"],inv.get("materialCost")),
        ss.cell(_COL["is_adjustment"], bool(inv.get("isAdjustment", False))),
        ss.cell(_COL["income"],        inv.get("income")),
        ss.cell(_COL["labor"],         inv.get("labor")),
        ss.cell(_COL["notes"],         inv.get("invoiceNotes") or inv.get("notes")),
        ss.cell(_COL["costs_total"],   inv.get("costsTotal")),
    ]


def _is_empty(inv: dict) -> bool:
    total = inv.get("total") or 0
    try:
        return float(total) == 0
    except (TypeError, ValueError):
        return False


def sync_once(*, backfill: bool = False) -> None:
    log.info("[invoices] sync start%s", " (backfill)" if backfill else "")
    try:
        sheet    = ss.get_sheet(INVOICE_SHEET_ID)
        existing = ss.get_cell_values(sheet, _KEY, list(_COL.values()))

        modified_after = None
        progress_cb = None
        if not backfill:
            interval = SYNC_INTERVALS["invoices"]
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=interval * 2)
            modified_after = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            def progress_cb(fetched, total):
                log.info("[invoices] Backfill: %d/%s records fetched",
                         fetched, total if total is not None else "?")

        invoices = st_api.fetch_invoices(
            modified_after=modified_after,
            progress_cb=progress_cb,
        )
        log.info("[invoices] fetched %d from ST", len(invoices))

        to_add    = []
        to_update = []
        to_delete = []

        for inv in invoices:
            inv_id = str(inv.get("id", ""))
            if not inv_id:
                continue

            status = inv.get("status", "")

            # Remove rows for exported invoices
            if status == "Exported":
                if inv_id in existing:
                    to_delete.append(existing[inv_id]["_row_id"])
                continue

            cells = _build_cells(inv)
            # Mark empty invoices
            cells.append(ss.cell(_COL["empty_invoice"], _is_empty(inv)))

            if inv_id in existing:
                row_data = existing[inv_id]
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
            ss.add_rows(INVOICE_SHEET_ID, to_add)
            log.info("[invoices] added %d rows", len(to_add))
        if to_update:
            ss.update_rows(INVOICE_SHEET_ID, to_update)
            log.info("[invoices] updated %d rows", len(to_update))
        if to_delete:
            ss.delete_rows(INVOICE_SHEET_ID, to_delete)
            log.info("[invoices] deleted %d exported rows", len(to_delete))

        log.info("[invoices] sync complete")
    except Exception as e:
        log.error("[invoices] sync error: %s", e, exc_info=True)


def run_loop() -> None:
    interval = SYNC_INTERVALS["invoices"]
    while True:
        sync_once()
        time.sleep(interval)


# ===========================================================================
# Write-back  (Smartsheet → ST)
# Gated by ST Developer App write grant: tn.acc.invoices:w
# ===========================================================================

def writeback_to_st(changes: list[dict]) -> None:
    """
    Push Smartsheet invoice changes back to ST.

    Each item in ``changes``:
      {
        "invoice_id": <ST invoice ID>,
        "field":      <field name, e.g. "status">,
        "new_value":  <value to write>,
      }

    Silently skipped if tn.acc.invoices:w is not in the token scopes.
    """
    from . import capabilities
    if not capabilities.can_write("invoices"):
        log.debug("[invoices] write-back skipped — write scope not granted")
        return

    by_id: dict[str, dict] = {}
    for ch in changes:
        iid = str(ch.get("invoice_id", "") or ch.get("st_id", ""))
        if not iid:
            continue
        by_id.setdefault(iid, {})[ch["field"]] = ch.get("new_value", ch.get("value"))

    # Only status fields allowed — totals always come from ST, never written back
    _WRITABLE = {"status"}
    for iid, fields in by_id.items():
        payload = {k: v for k, v in fields.items() if k in _WRITABLE}
        if not payload:
            continue
        try:
            st_api.patch("accounting", f"invoices/{iid}", payload)
            log.info("[invoices] patched %s: %s", iid, list(payload))
        except Exception as exc:
            log.error("[invoices] patch failed for invoice %s: %s", iid, exc)
