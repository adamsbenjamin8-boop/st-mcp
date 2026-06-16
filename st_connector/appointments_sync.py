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

        to_add    = []
        to_update = []
        to_delete = []

        st_ids_seen = set()
        for appt in appts:
            appt_id = str(appt.get("id", ""))
            if not appt_id:
                continue
            st_ids_seen.add(appt_id)

            if _is_stale(appt):
                if appt_id in existing:
                    to_delete.append(existing[appt_id]["_row_id"])
                continue

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
            log.info("[appointments] deleted %d stale rows", len(to_delete))

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
