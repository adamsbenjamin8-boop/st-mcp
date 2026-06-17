"""
Jobs sync — ST → Smartsheet Jobs KPI sheet.
Runs every 15 minutes.
Keeps only active jobs: Scheduled, InProgress, Hold.
Deletes rows when status transitions to Completed or Canceled.
"""

import logging
import time
from datetime import datetime, timezone, timedelta

from . import st_api, smartsheet_client as ss
from .config import JOBS_SHEET_ID, JOBS_COLS, SYNC_INTERVALS

log = logging.getLogger(__name__)

_STATUS_MAP = {
    "InProgress": "In Progress",
}


def _col(key: str) -> int | None:
    return JOBS_COLS.get(key)


def _build_cells(job: dict) -> list:
    inv  = job.get("invoice") or {}
    loc  = job.get("location") or {}
    addr = loc.get("address") or {}
    address_str = ", ".join(filter(None, [
        addr.get("street"), addr.get("city"), addr.get("state"),
    ]))

    raw_status = job.get("status", "")
    status = _STATUS_MAP.get(raw_status, raw_status)

    tagged = job.get("taggedTechnicians") or []
    assigned_techs = ", ".join(t.get("name", "") for t in tagged if t.get("name"))

    cells = []
    def _add(key, value):
        col = _col(key)
        if col and value is not None:
            cells.append(ss.cell(col, value))

    _add("job_num",              job.get("number"))
    _add("job_id",               job.get("id"))
    _add("status",               status)
    _add("customer_name",        (job.get("customer") or {}).get("name"))
    _add("customer_address",     address_str or None)
    _add("business_unit_id",     (job.get("businessUnit") or {}).get("id"))
    _add("business_unit",        (job.get("businessUnit") or {}).get("name"))
    _add("scheduled_date",       (job.get("start") or "")[:10] or None)
    _add("completion_date",      (job.get("completedOn") or "")[:10] or None)
    _add("job_type",             (job.get("type") or {}).get("name"))
    _add("invoice_num",          inv.get("number"))
    _add("jobs_total",           inv.get("total"))
    _add("customer_id",          (job.get("customer") or {}).get("id"))
    _add("location_id",          loc.get("id"))
    _add("assigned_technicians", assigned_techs or None)
    _add("sold_by",              (job.get("soldBy") or {}).get("name"))
    _add("primary_technician",   (job.get("assignedTo") or {}).get("name"))
    _add("first_dispatch",       (job.get("firstAppointmentDate") or "")[:10] or None)
    _add("invoice_date",         (inv.get("date") or "")[:10] or None)
    _add("created_date",         (job.get("createdOn") or "")[:10] or None)

    return cells


def sync_once(*, backfill: bool = False) -> None:
    log.info("[jobs] sync start%s", " (backfill)" if backfill else "")
    try:
        sheet = ss.get_sheet(JOBS_SHEET_ID)

        key_col = _col("job_id")
        if not key_col:
            log.error("[jobs] could not resolve job_id column — aborting sync")
            return

        existing = ss.get_cell_values(sheet, key_col, list(JOBS_COLS.values()))

        modified_after = None
        progress_cb = None
        if not backfill:
            interval = SYNC_INTERVALS["jobs"]
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=interval * 2)
            modified_after = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            def progress_cb(fetched, total):
                log.info("[jobs] Backfill: %d/%s records fetched",
                         fetched, total if total is not None else "?")

        jobs = st_api.fetch_jobs(
            modified_after=modified_after,
            progress_cb=progress_cb,
        )
        log.info("[jobs] fetched %d from ST", len(jobs))

        to_add    = []
        to_update = []
        to_delete = []

        for job in jobs:
            job_id = str(job.get("id", ""))
            if not job_id:
                continue

            status = job.get("status", "")

            # Remove inactive jobs — only Scheduled/InProgress/Hold stay in the sheet
            if status in ("Completed", "Canceled"):
                if job_id in existing:
                    to_delete.append(existing[job_id]["_row_id"])
                continue

            cells = _build_cells(job)

            if job_id in existing:
                row_data = existing[job_id]
                changed  = any(
                    str(c.get("value", "") or "") != str(row_data.get(c["columnId"], "") or "")
                    for c in cells
                    if c["columnId"] != key_col
                )
                if changed:
                    to_update.append({"id": row_data["_row_id"], "cells": cells, "strict": False})
            else:
                to_add.append({"cells": cells, "toBottom": True, "strict": False})

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
