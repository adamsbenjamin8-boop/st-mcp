"""
Appointments sync — ST → Smartsheet Appointments sheet.
Runs every 5 minutes.
"""

import logging
import time
from datetime import datetime, timezone, timedelta

from . import st_api, smartsheet_client as ss
from .config import (
    APPT_SHEET_ID, APPT_COLS, SYNC_INTERVALS, APPT_RETENTION_DAYS,
)

log = logging.getLogger(__name__)

_COL = APPT_COLS
_KEY = _COL["appt_id"]

# appt_id (str) → job_id (int): appointments currently excluded from the sheet.
# Persists in-memory across sync cycles so we can detect when exclusion lifts.
_excluded: dict[str, int] = {}


def _iso_to_date(iso: str) -> str | None:
    """Extract YYYY-MM-DD from an ISO datetime string."""
    if not iso:
        return None
    try:
        return iso[:10]
    except Exception:
        return None


def _build_cells(appt: dict) -> list:
    start_iso  = appt.get("start", "")
    end_iso    = appt.get("end", "")
    start_date = _iso_to_date(start_iso)
    cells = [
        ss.cell(_COL["appt_id"],    appt.get("id")),
        ss.cell(_COL["job_id"],     appt.get("jobId")),
        ss.cell(_COL["appt_number"],appt.get("appointmentNumber")),
        ss.cell(_COL["status"],     appt.get("status")),
        ss.cell(_COL["start"],      start_iso),
        ss.cell(_COL["end"],        end_iso),
    ]
    if start_date:
        cells.append(ss.cell(_COL["start_date"], start_date))
    return cells


def _is_stale(appt: dict) -> bool:
    """True if appointment is finished and older than APPT_RETENTION_DAYS."""
    finished_statuses = {"Completed", "Cancelled", "Done"}
    if appt.get("status") not in finished_statuses:
        return False
    start_iso = appt.get("start", "")
    if not start_iso:
        return False
    try:
        appt_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        cutoff  = datetime.now(timezone.utc) - timedelta(days=APPT_RETENTION_DAYS)
        return appt_dt < cutoff
    except Exception:
        return False


def _is_excluded(appt: dict, job: dict | None) -> bool:
    """Return True if this appointment should be excluded from the sheet.

    Excluded when the parent job is Canceled, or when the job's invoice
    syncStatus is Exported or Bypassed. If job data is unavailable the
    appointment is included (fail-open).
    """
    if job is None:
        return False
    if job.get("status") == "Canceled":
        return True
    # The ST jobs API may return a nested invoice object with syncStatus.
    # If job["invoice"] is absent, we cannot check invoice status here.
    # TODO: if the jobs endpoint does not include invoice.syncStatus, add a
    #       fetch_invoices_for_jobs() helper in st_api.py and call it here.
    inv = job.get("invoice") or {}
    if inv.get("syncStatus") in ("Exported", "Bypassed"):
        return True
    return False


def sync_once(*, backfill: bool = False) -> None:
    log.info("[appointments] sync start%s", " (backfill)" if backfill else "")
    try:
        sheet = ss.get_sheet(APPT_SHEET_ID)
        existing = ss.get_cell_values(sheet, _KEY, list(_COL.values()))

        modified_after = None
        progress_cb = None
        if not backfill:
            interval = SYNC_INTERVALS["appointments"]
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=interval * 2)
            modified_after = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            def progress_cb(fetched, total):
                log.info("[appointments] Backfill: %d/%s records fetched",
                         fetched, total if total is not None else "?")

        appts = st_api.fetch_appointments(
            modified_after=modified_after,
            progress_cb=progress_cb,
        )
        log.info("[appointments] fetched %d from ST", len(appts))

        # Batch-fetch parent jobs so exclusion rules can be checked without
        # one API call per appointment.
        job_ids = list({a["jobId"] for a in appts if a.get("jobId")})
        job_map = st_api.fetch_jobs_by_ids(job_ids) if job_ids else {}
        log.debug("[appointments] fetched %d parent jobs for exclusion check", len(job_map))

        to_add    = []
        to_update = []
        to_delete = []

        st_ids_seen = set()
        for appt in appts:
            appt_id = str(appt.get("id", ""))
            if not appt_id:
                continue
            st_ids_seen.add(appt_id)

            job_id = appt.get("jobId")
            job    = job_map.get(job_id) if job_id else None

            if appt.get("status") in ("Canceled", "Cancelled"):
                if appt_id in existing:
                    to_delete.append(existing[appt_id]["_row_id"])
                _excluded.pop(appt_id, None)
                continue

            if _is_stale(appt):
                if appt_id in existing:
                    to_delete.append(existing[appt_id]["_row_id"])
                _excluded.pop(appt_id, None)
                continue

            if _is_excluded(appt, job):
                if appt_id in existing:
                    to_delete.append(existing[appt_id]["_row_id"])
                if job_id:
                    _excluded[appt_id] = job_id
                log.debug("[appointments] excluded appt %s (job %s)", appt_id, job_id)
                continue
            else:
                # Exclusion may have been lifted — remove from tracking if present.
                _excluded.pop(appt_id, None)

            cells = _build_cells(appt)

            if appt_id in existing:
                row_data = existing[appt_id]
                changed = any(
                    str(c.get("value", "") or "") != str(row_data.get(c["columnId"], "") or "")
                    for c in cells
                    if c["columnId"] != _KEY  # skip key col itself
                )
                if changed:
                    to_update.append({"id": row_data["_row_id"], "cells": cells})
            else:
                to_add.append({"cells": cells, "toBottom": True})

        # --- Restoration pass ---
        # Re-check excluded appointments that were NOT seen in this sync cycle.
        # We only act when the parent job has been modified recently, keeping
        # this lightweight (one batch job fetch, one batch appointment fetch).
        candidates = {
            aid: jid for aid, jid in _excluded.items()
            if aid not in st_ids_seen
        }
        if candidates:
            candidate_job_ids = list(set(candidates.values()))
            candidate_jobs = st_api.fetch_jobs_by_ids(candidate_job_ids)

            # Filter to jobs modified since our last window (re-check all on backfill).
            recently_modified: set[int] = set()
            for jid, job in candidate_jobs.items():
                job_modified = job.get("modifiedOn", "")
                if not modified_after or job_modified >= modified_after:
                    recently_modified.add(jid)

            appt_ids_to_recheck = [
                int(aid) for aid, jid in candidates.items()
                if jid in recently_modified
            ]
            if appt_ids_to_recheck:
                rechecked = st_api.fetch_appointments_by_ids(appt_ids_to_recheck)
                for appt_id_int, appt in rechecked.items():
                    str_aid = str(appt_id_int)
                    jid = candidates.get(str_aid)
                    job = candidate_jobs.get(jid)
                    if not _is_excluded(appt, job) and not _is_stale(appt):
                        if str_aid not in existing:
                            to_add.append({"cells": _build_cells(appt), "toBottom": True})
                        _excluded.pop(str_aid, None)
                        log.info("[appointments] restored appt %s (exclusion lifted)", str_aid)

        # Rows in sheet but not seen from ST at all — leave them unless stale
        # (ST may stop returning very old appointments; don't delete unless confirmed stale)

        if to_add:
            ss.add_rows(APPT_SHEET_ID, to_add)
            log.info("[appointments] added %d rows", len(to_add))
        if to_update:
            ss.update_rows(APPT_SHEET_ID, to_update)
            log.info("[appointments] updated %d rows", len(to_update))
        if to_delete:
            ss.delete_rows(APPT_SHEET_ID, to_delete)
            log.info("[appointments] deleted %d rows (stale or excluded)", len(to_delete))

        log.info("[appointments] sync complete")
    except Exception as e:
        log.error("[appointments] sync error: %s", e, exc_info=True)


def run_loop() -> None:
    interval = SYNC_INTERVALS["appointments"]
    while True:
        sync_once()
        time.sleep(interval)


# ===========================================================================
# Write-back  (Smartsheet → ST)
# Gated by ST Developer App write grant: tn.jpm.appointments:w
# ===========================================================================

def writeback_to_st(changes: list[dict]) -> None:
    """
    Push Smartsheet appointment changes back to ST.

    Each item in ``changes`` should describe one field change:
      {
        "appt_id":   <ST appointment ID>,
        "field":     <field name, e.g. "status">,
        "new_value": <value to write>,
      }

    Silently skipped if the Developer App has not granted write access
    (tn.jpm.appointments:w not in token scopes).
    """
    from . import capabilities
    if not capabilities.can_write("appointments"):
        log.debug("[appointments] write-back skipped — write scope not granted")
        return

    by_id: dict[str, dict] = {}
    for ch in changes:
        aid = str(ch.get("appt_id", "") or ch.get("st_id", ""))
        if not aid:
            continue
        by_id.setdefault(aid, {})[ch["field"]] = ch.get("new_value", ch.get("value"))

    _WRITABLE = {"status", "start", "end"}
    for aid, fields in by_id.items():
        payload = {k: v for k, v in fields.items() if k in _WRITABLE}
        if not payload:
            continue
        try:
            st_api.patch("jpm", f"appointments/{aid}", payload)
            log.info("[appointments] patched %s: %s", aid, list(payload))
        except Exception as exc:
            log.error("[appointments] patch failed for appt %s: %s", aid, exc)
