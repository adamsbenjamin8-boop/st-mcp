"""
ServiceTitan API Client for Quote App
======================================
Lightweight ST API client — handles auth and the specific endpoints
needed for PO creation from parsed quotes.
"""

import os
import time
import httpx
from pathlib import Path
from typing import Optional, List, Dict, Any


# ---------------------------------------------------------------------------
# Load credentials from .env sidecar (same pattern as st_cache_sync.py)
# ---------------------------------------------------------------------------
def _load_env():
    env_file = Path(__file__).parent.parent / ".env"
    if not env_file.exists():
        # Try Program Files install location
        env_file = Path("C:/Program Files/ST_MCP/.env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

AUTH_URL = "https://auth.servicetitan.io/connect/token"
API_BASE = "https://api.servicetitan.io"

_token_cache = {"token": None, "expires_at": 0.0}


def _get_token() -> str:
    now = time.monotonic()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 30:
        return _token_cache["token"]
    resp = httpx.post(AUTH_URL, data={
        "grant_type":    "client_credentials",
        "client_id":     os.environ["ST_CLIENT_ID"],
        "client_secret": os.environ["ST_CLIENT_SECRET"],
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"]      = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 3600)
    return _token_cache["token"]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "ST-App-Key":    os.environ["ST_APP_KEY"],
        "Content-Type":  "application/json",
    }


def _url(api: str, path: str) -> str:
    tenant = os.environ["ST_TENANT_ID"]
    return f"{API_BASE}/{api}/v2/tenant/{tenant}/{path}"


def _get(path_api: tuple, params: dict = None) -> dict:
    api, path = path_api
    r = httpx.get(_url(api, path), headers=_headers(), params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path_api: tuple, body: dict) -> dict:
    api, path = path_api
    r = httpx.post(_url(api, path), headers=_headers(), json=body, timeout=30)
    if r.status_code == 204:
        return {"success": True}
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Vendor lookup
# ---------------------------------------------------------------------------
def find_vendor_id(vendor_name: str) -> Optional[int]:
    """Search for a vendor by name in the local cache first, then ST API."""
    import sqlite3
    db_path = "C:/ST/st_cache.db"
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT id FROM vendors WHERE LOWER(name) LIKE ? LIMIT 5",
            (f"%{vendor_name.lower()[:20]}%",)
        ).fetchall()
        conn.close()
        if rows:
            return rows[0][0]
    except Exception:
        pass

    # Fallback: live API search
    try:
        data = _get(("inventory", "vendors"), {"name": vendor_name, "pageSize": 5})
        items = data.get("data", [])
        if items:
            return items[0]["id"]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Job lookup
# ---------------------------------------------------------------------------
def find_job_id(job_name: str) -> Optional[dict]:
    """
    Search for an open/in-progress job matching the given name.
    Returns dict with id, jobNumber, customerName or None.
    """
    if not job_name or job_name.lower() in ('test', ''):
        return None
    try:
        data = _get(("jpm", "jobs"), {
            "name": job_name,
            "jobStatus": "InProgress,Scheduled,Dispatched",
            "pageSize": 10,
        })
        items = data.get("data", [])
        if items:
            j = items[0]
            return {"id": j["id"], "jobNumber": j.get("number", ""), "customerName": j.get("customer", {}).get("name", "")}
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# PO Type lookup
# ---------------------------------------------------------------------------
def get_default_po_type_id() -> Optional[int]:
    """Get the first available PO type ID from cache."""
    import sqlite3
    db_path = "C:/ST/st_cache.db"
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT id FROM po_types LIMIT 1").fetchone()
        conn.close()
        if row:
            return row[0]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# PO creation
# ---------------------------------------------------------------------------
def create_po_request(
    vendor_id: int,
    job_id: Optional[int],
    po_type_id: int,
    memo: str = "",
) -> Optional[dict]:
    """Create a Purchase Order in ServiceTitan. Returns the PO dict or None."""
    body = {
        "vendorId":   vendor_id,
        "typeId":     po_type_id,
        "requiredOn": None,
        "memo":       memo,
    }
    if job_id:
        body["jobId"] = job_id
    try:
        return _post(("inventory", "purchase-orders"), body)
    except Exception as e:
        print(f"Error creating PO: {e}")
        return None


def add_po_item(po_id: int, item: dict) -> bool:
    """
    Add a single line item to a PO.
    item keys: description, quantity, unitCost, vendorPartNumber (optional)
    """
    body = {
        "description":      item.get("description", ""),
        "quantity":         item.get("quantity", 1),
        "unitCost":         item.get("unitCost", 0.0),
        "vendorPartNumber": item.get("vendorPartNumber", ""),
    }
    try:
        _post(("inventory", f"purchase-orders/{po_id}/items"), body)
        return True
    except Exception as e:
        print(f"Error adding PO item: {e}")
        return False


def get_po_url(po_id: int) -> str:
    """Return the ServiceTitan deep-link URL for a PO."""
    return f"https://go.servicetitan.com/#/Inventory/PurchaseOrder/{po_id}"
