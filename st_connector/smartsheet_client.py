"""
Smartsheet API wrapper for st_connector.
Covers: get sheet rows, add rows, update rows, delete rows, create sheet.
"""

import os
import logging
import httpx

from .config import SS_API_BASE, SS_WORKSPACE_ID

log = logging.getLogger(__name__)


def _headers() -> dict:
    key = os.environ.get("SMARTSHEET_API_KEY", "")
    if not key:
        raise RuntimeError("SMARTSHEET_API_KEY not set")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
    }


# ---------------------------------------------------------------------------
# Sheet fetch
# ---------------------------------------------------------------------------

def get_sheet(sheet_id: int) -> dict:
    """Returns the full sheet object (includes columns and rows)."""
    r = httpx.get(
        f"{SS_API_BASE}/sheets/{sheet_id}",
        headers=_headers(),
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def col_map_from_sheet(sheet: dict) -> dict:
    """
    Build {column_title_lower: column_id} from a sheet object.
    Useful for sheets where column IDs aren't hardcoded in config.
    """
    return {c["title"].lower(): c["id"] for c in sheet.get("columns", [])}


def rows_to_key_map(sheet: dict, key_col_id: int) -> dict:
    """
    Returns {str(key_value): row_id} for every row in the sheet,
    where key_value is the cell value in key_col_id.
    """
    mapping = {}
    for row in sheet.get("rows", []):
        for cell in row.get("cells", []):
            if cell.get("columnId") == key_col_id:
                val = cell.get("value")
                if val is not None:
                    mapping[str(val)] = row["id"]
                break
    return mapping


def get_cell_values(sheet: dict, key_col_id: int, value_col_ids: list) -> dict:
    """
    Returns {str(key): {col_id: value, ...}} for comparison during sync.
    """
    result = {}
    for row in sheet.get("rows", []):
        key = None
        vals = {}
        for cell in row.get("cells", []):
            cid = cell.get("columnId")
            if cid == key_col_id:
                key = str(cell.get("value", ""))
            if cid in value_col_ids:
                vals[cid] = cell.get("value")
        if key:
            vals["_row_id"] = row["id"]
            result[key] = vals
    return result


# ---------------------------------------------------------------------------
# Row writes
# ---------------------------------------------------------------------------

def add_rows(sheet_id: int, rows: list) -> list:
    """
    rows: list of {"cells": [...], "toBottom": True}
    Returns list of created row IDs.
    """
    if not rows:
        return []
    r = httpx.post(
        f"{SS_API_BASE}/sheets/{sheet_id}/rows",
        headers=_headers(),
        json=rows,
        timeout=30,
    )
    if r.is_error:
        log.error("[smartsheet] %s %s → %s | body: %s", r.request.method, str(r.request.url)[-80:], r.status_code, r.text[:2000])
    r.raise_for_status()
    return [row["id"] for row in r.json().get("result", [])]


def update_rows(sheet_id: int, rows: list) -> None:
    """
    rows: list of {"id": row_id, "cells": [...]}
    """
    if not rows:
        return
    r = httpx.put(
        f"{SS_API_BASE}/sheets/{sheet_id}/rows",
        headers=_headers(),
        json=rows,
        timeout=30,
    )
    if r.is_error:
        log.error("[smartsheet] %s %s → %s | body: %s", r.request.method, str(r.request.url)[-80:], r.status_code, r.text[:2000])
    r.raise_for_status()


def delete_rows(sheet_id: int, row_ids: list) -> None:
    if not row_ids:
        return
    # Smartsheet DELETE accepts up to 450 IDs per call
    for chunk_start in range(0, len(row_ids), 450):
        chunk = row_ids[chunk_start:chunk_start + 450]
        ids_param = ",".join(str(i) for i in chunk)
        r = httpx.delete(
            f"{SS_API_BASE}/sheets/{sheet_id}/rows",
            headers=_headers(),
            params={"ids": ids_param},
            timeout=30,
        )
        r.raise_for_status()


# ---------------------------------------------------------------------------
# Sheet creation
# ---------------------------------------------------------------------------

def find_sheet_in_workspace(workspace_id: int, sheet_name: str) -> int | None:
    """Returns sheet ID if a sheet with that name exists in the workspace, else None."""
    r = httpx.get(
        f"{SS_API_BASE}/workspaces/{workspace_id}",
        headers=_headers(),
        timeout=30,
    )
    r.raise_for_status()
    for sheet in r.json().get("sheets", []):
        if sheet.get("name") == sheet_name:
            return sheet["id"]
    return None


def create_sheet_in_workspace(
    workspace_id: int,
    sheet_name: str,
    columns: list,
) -> int:
    """
    columns: list of dicts with keys: title, type, primary (optional), options (for PICKLIST).
    Returns the new sheet ID.
    """
    ss_cols = []
    for col in columns:
        entry: dict = {"title": col["title"], "type": col["type"]}
        if col.get("primary"):
            entry["primary"] = True
        if col.get("options"):
            entry["options"] = col["options"]
        ss_cols.append(entry)

    r = httpx.post(
        f"{SS_API_BASE}/workspaces/{workspace_id}/sheets",
        headers=_headers(),
        json={"name": sheet_name, "columns": ss_cols},
        timeout=30,
    )
    r.raise_for_status()
    sheet_id = r.json()["result"]["id"]
    log.info("Created sheet '%s' id=%s", sheet_name, sheet_id)
    return sheet_id


def get_or_create_sheet(sheet_name: str, columns: list) -> int:
    """Find existing sheet in workspace or create it. Returns sheet ID."""
    existing = find_sheet_in_workspace(SS_WORKSPACE_ID, sheet_name)
    if existing:
        log.info("Sheet '%s' already exists id=%s", sheet_name, existing)
        return existing
    return create_sheet_in_workspace(SS_WORKSPACE_ID, sheet_name, columns)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cell(col_id: int, value) -> dict:
    """Build a Smartsheet cell dict."""
    return {"columnId": col_id, "value": value}


def cell_hyperlink(col_id: int, display: str, url: str) -> dict:
    return {
        "columnId": col_id,
        "value":    display,
        "hyperlink": {"url": url},
    }
