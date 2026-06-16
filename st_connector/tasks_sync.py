"""
Tasks sync — ST → Smartsheet Tasks-KPI sheet.
Runs every 15 minutes.
Keeps open tasks (ToDo, InProgress, Open).
Removes completed tasks older than TASKS_RETENTION_DAYS.
"""

import logging
import time
from datetime import datetime, timezone, timedelta

from . import st_api, smartsheet_client as ss
from .config import TASKS_SHEET_ID, TASKS_COLS, SYNC_INTERVALS, TASKS_RETENTION_DAYS

log = logging.getLogger(__name__)

_COL = TASKS_COLS
_KEY = _COL["task_id"]

_OPEN_STATUSES = {"Open", "ToDo", "InProgress"}


def _is_stale_completed(task: dict) -> bool:
    status = task.get("status", "")
    if status in _OPEN_STATUSES:
        return False
    due_str = task.get("dueDate") or task.get("completedOn") or ""
    if not due_str:
        return True  # completed with no date — safe to remove
    try:
        dt      = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
        cutoff  = datetime.now(timezone.utc) - timedelta(days=TASKS_RETENTION_DAYS)
        return dt < cutoff
    except Exception:
        return False


def _build_cells(task: dict) -> list:
    job = task.get("job") or {}
    return [
        ss.cell(_COL["task_id"],       task.get("id")),
        ss.cell(_COL["job_id"],        task.get("jobId") or job.get("id")),
        ss.cell(_COL["job_number"],    task.get("jobNumber") or job.get("number")),
        ss.cell(_COL["description"],   task.get("description") or task.get("name")),
        ss.cell(_COL["priority"],      task.get("priority")),
        ss.cell(_COL["type"],          task.get("type") or task.get("typeName")),
        ss.cell(_COL["status"],        task.get("status")),
        ss.cell(_COL["due"],           (task.get("dueDate") or "")[:10] or None),
        ss.cell(_COL["customer_name"], (task.get("customer") or {}).get("name")),
        ss.cell(_COL["business_unit"], task.get("businessUnitId")),
    ]


def sync_once(*, backfill: bool = False) -> None:
    log.info("[tasks] sync start%s", " (backfill)" if backfill else "")
    try:
        sheet    = ss.get_sheet(TASKS_SHEET_ID)
        existing = ss.get_cell_values(sheet, _KEY, list(_COL.values()))

        modified_after = None
        progress_cb = None
        if not backfill:
            interval = SYNC_INTERVALS["tasks"]
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=interval * 2)
            modified_after = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            def progress_cb(fetched, total):
                log.info("[tasks] Backfill: %d/%s records fetched",
                         fetched, total if total is not None else "?")

        tasks = st_api.fetch_tasks(
            modified_after=modified_after,
            progress_cb=progress_cb,
        )
        log.info("[tasks] fetched %d from ST", len(tasks))

        to_add    = []
        to_update = []
        to_delete = []

        seen_ids = set()
        for task in tasks:
            task_id = str(task.get("id", ""))
            if not task_id:
                continue
            seen_ids.add(task_id)

            if _is_stale_completed(task):
                if task_id in existing:
                    to_delete.append(existing[task_id]["_row_id"])
                continue

            cells = _build_cells(task)

            if task_id in existing:
                row_data = existing[task_id]
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
            ss.add_rows(TASKS_SHEET_ID, to_add)
            log.info("[tasks] added %d rows", len(to_add))
        if to_update:
            ss.update_rows(TASKS_SHEET_ID, to_update)
            log.info("[tasks] updated %d rows", len(to_update))
        if to_delete:
            ss.delete_rows(TASKS_SHEET_ID, to_delete)
            log.info("[tasks] deleted %d stale-completed rows", len(to_delete))

        log.info("[tasks] sync complete")
    except Exception as e:
        log.error("[tasks] sync error: %s", e, exc_info=True)


def run_loop() -> None:
    interval = SYNC_INTERVALS["tasks"]
    while True:
        sync_once()
        time.sleep(interval)


# ===========================================================================
# Write-back  (Smartsheet → ST)
# Gated by ST Developer App write grant: tn.tsm.tasks:w
# ===========================================================================

def writeback_to_st(changes: list[dict]) -> None:
    """
    Push Smartsheet task changes back to ST.

    Each item in ``changes``:
      {
        "task_id":   <ST task ID>,
        "field":     <field name, e.g. "status">,
        "new_value": <value to write>,
      }

    Silently skipped if tn.tsm.tasks:w is not in the token scopes.
    """
    from . import capabilities
    if not capabilities.can_write("tasks"):
        log.debug("[tasks] write-back skipped — write scope not granted")
        return

    by_id: dict[str, dict] = {}
    for ch in changes:
        tid = str(ch.get("task_id", "") or ch.get("st_id", ""))
        if not tid:
            continue
        by_id.setdefault(tid, {})[ch["field"]] = ch.get("new_value", ch.get("value"))

    _WRITABLE = {"status", "dueDate", "assigneeId"}
    for tid, fields in by_id.items():
        payload = {k: v for k, v in fields.items() if k in _WRITABLE}
        if not payload:
            continue
        try:
            st_api.patch("jpm", f"job-orders/{tid}", payload)
            log.info("[tasks] patched %s: %s", tid, list(payload))
        except Exception as exc:
            log.error("[tasks] patch failed for task %s: %s", tid, exc)
