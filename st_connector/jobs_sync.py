"""
Jobs sync — ST → Smartsheet Jobs KPI sheet.
Runs every 15 minutes.

Removal rules:
  - Completed + invoice exported  → delete row
  - Cancelled + no cost (cost=0)  → delete row
  - Cancelled + cost > 0          → keep, append "[Cancelled-W/Cost]" to notes
"""

import logging
import time

from . import st_api, smartsheet_client as ss
from .config import JOBS_SHEET_ID, JOBS_COL_NAMES, SYNC_INTERVALS

log = logging.getLogger(__name__)

# Resolved at first sync call via col_map_from_sheet
_col_ids: dict = {}


def _resolve_cols(sheet: dict) -> None:
    global _col_ids
    if _col_ids:
        return
    cmap = ss.col_map_from_sheet(sheet)
    for key, title in JOBS_COL_NAMES.items():
        col_id = cmap.get(title.lower())
        if col_id:
            _col_ids[key] = col_id
        else:
            log.warning("[jobs] column not found in sheet: '%s'", title)


def _col(key: str) -> int | None:
    return _col_ids.get(key)


def _invoice_exported(job: dict) -> bool:
    """Check if the job's invoice status is Exported."""
    inv = job.get("invoice") or {}
    return inv.get("status", "") == "Exported"


def _job_cost(job: dict) -> float:
    inv = job.get("invoice") or {}
    summary = inv.get("summary") or {}
    total = summary.get("total") or inv.get("total") or 0
    try:
        return float(total)
    except (TypeError, ValueError):
        return 0.0


def _build_cells(job: dict, note_suffix: str = "") -> list:
    inv     = job.get("invoice") or {}
    inv_sum = inv.get("summary") or {}
    loc     = job.get("location") or {}
    addr    = loc.get("address") or {}
    address_str = ", ".join(filter(None, [
        addr.get("street"), addr.get("city"), addr.get("state"),
    ]))
    first_tech = ""
    for appt in (job.get("appointments") or []):
        for tech in (appt.get("technicians") or []):
            first_tech = tech.get("name", "")
            break
        if first_tech:
            break

    cells = []
    def _add(key, value):
        col = _col(key)
        if col:
            cells.append(ss.cell(col, value))

    _add("job_id",          job.get("id"))
    _add("job_num",         job.get("number"))
    _add("invoice_id",      inv.get("id"))
    _add("customer_name",   (job.get("customer") or {}).get("name"))
    _add("status",          job.get("status"))
    _add("created_date",    (job.get("createdOn") or "")[:10] or None)
    _add("first_dispatch",  (job.get("firstAppointmentDate") or "")[:10] or None)
    _add("completion_date", (job.get("completedOn") or "")[:10] or None)
    _add("location",        address_str or None)
    _add("job_type",        (job.get("type") or {}).get("name"))
    _add("invoice_status",  inv.get("status"))
    _add("total",           inv_sum.get("total") or inv.get("total"))
    _add("balance",         inv_sum.get("balance") or inv.get("balance"))
    _add("batch_number",    inv.get("batchNumber"))
    _add("business_unit",   (job.get("businessUnit") or {}).get("name"))

    return cells


def sync_once() -> None:
    log.info("[jobs] sync start")
    try:
        sheet = ss.get_sheet(JOBS_SHEET_ID)
        _resolve_cols(sheet)

        key_col = _col("job_id")
        if not key_col:
            log.error("[jobs] could not resolve job_id column — aborting sync")
            return

        existing = ss.get_cell_values(sheet, key_col, list(_col_ids.values()))

        jobs = st_api.fetch_jobs()
        log.info("[jobs] fetched %d from ST", len(jobs))

        to_add    = []
        to_update = []
        to_delete = []

        for job in jobs:
            job_id = str(job.get("id", ""))
            if not job_id:
                continue

            status   = job.get("status", "")
            exported = _invoice_exported(job)
            cost     = _job_cost(job)

            # Deletion rules
            if status == "Completed" and exported:
                if job_id in existing:
                    to_delete.append(existing[job_id]["_row_id"])
                continue

            if status == "Cancelled" and cost == 0:
                if job_id in existing:
                    to_delete.append(existing[job_id]["_row_id"])
                continue

            # Cancelled with cost — mark it
            note_suffix = " [Cancelled-W/Cost]" if status == "Cancelled" and cost > 0 else ""
            cells = _build_cells(job, note_suffix)

            if job_id in existing:
                row_data = existing[job_id]
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
            ss.add_rows(JOBS_SHEET_ID, to_add)
            log.info("[jobs] added %d rows", len(to_add))
        if to_update:
            ss.update_rows(JOBS_SHEET_ID, to_update)
            log.info("[jobs] updated %d rows", len(to_update))
        if to_delete:
            ss.delete_rows(JOBS_SHEET_ID, to_delete)
            log.info("[jobs] deleted %d rows", len(to_delete))

        log.info("[jobs] sync complete")
    except Exception as e:
        log.error("[jobs] sync error: %s", e, exc_info=True)


def run_loop() -> None:
    interval = SYNC_INTERVALS["jobs"]
    while True:
        sync_once()
        time.sleep(interval)


# ===========================================================================
# Write-back  (Smartsheet → ST)
# Gated by ST Developer App write grant: tn.jpm.jobs:w
# ===========================================================================

def writeback_to_st(changes: list[dict]) -> None:
    """
    Push Smartsheet job changes back to ST.

    Each item in ``changes``:
      {
        "job_id":    <ST job ID>,
        "field":     <field name, e.g. "status">,
        "new_value": <value to write>,
      }

    Silently skipped if tn.jpm.jobs:w is not in the token scopes.
    """
    from . import capabilities
    if not capabilities.can_write("jobs"):
        log.debug("[jobs] write-back skipped — write scope not granted")
        return

    by_id: dict[str, dict] = {}
    for ch in changes:
        jid = str(ch.get("job_id", "") or ch.get("st_id", ""))
        if not jid:
            continue
        by_id.setdefault(jid, {})[ch["field"]] = ch.get("new_value", ch.get("value"))

    _WRITABLE = {"status", "notes", "tags"}
    for jid, fields in by_id.items():
        payload = {k: v for k, v in fields.items() if k in _WRITABLE}
        if not payload:
            continue
        try:
            st_api.patch("jpm", f"jobs/{jid}", payload)
            log.info("[jobs] patched %s: %s", jid, list(payload))
        except Exception as exc:
            log.error("[jobs] patch failed for job %s: %s", jid, exc)
