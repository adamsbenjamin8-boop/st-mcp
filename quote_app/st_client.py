"""
ServiceTitan API Client for Quote App
"""
import os
import re
import time
import json
import sqlite3
import httpx
from datetime import date
from pathlib import Path
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_JOB_ID           = 115808181
DEFAULT_BUSINESS_UNIT_ID = 7616424
DEFAULT_INVENTORY_LOC_ID = 202
DEFAULT_PO_TYPE_ID       = 126
DEFAULT_VENDOR_ID        = 474
GENERIC_SKU_ID           = 27365999

SHIP_TO = {
    "description": "Tyngsboro Warehouse",
    "address": {
        "street": "21 Westech Drive", "unit": "",
        "city": "Tyngsboro", "state": "MA",
        "zip": "01879", "country": "USA"
    }
}

DB_PATH             = "C:/ST/st_cache.db"
VENDOR_MAPPING_PATH = Path("C:/Program Files/ST_MCP/vendor_mapping.json")

# Approved Vendor Emails sheet — column IDs
APPROVED_SHEET_ID       = 1832230987976580
COL_VENDOR_NAME         = 1191779545419652
COL_EMAIL_DOMAIN        = 5695379172790148
COL_VENDOR_CONTACT      = 6588800351637380
COL_ST_VENDOR_ID        = 3443579359104900
COL_APPROVAL_TOKEN      = 7947178986475396
COL_APPROVED_BY         = 5132429219368836
COL_DATE_ADDED          = 2880629405683588
COL_DATE_APPROVED       = 7384229033054084
COL_STATUS              = 628829591998340
COL_NOTES               = 1754729498840964
COL_TYPE                = 2025839024967556
COL_ST_VENDOR_NAME      = 5030097781559172

# ---------------------------------------------------------------------------
# Known vendor keywords loaded from ST cache at startup
# ---------------------------------------------------------------------------
_KNOWN_KEYWORDS = ["ferguson", "f.w. webb", "fw webb", "johnstone"]
_known_vendor_map: dict = {}

def _load_known_vendors():
    global _known_vendor_map
    try:
        conn = sqlite3.connect(DB_PATH)
        for kw in _KNOWN_KEYWORDS:
            rows = conn.execute(
                "SELECT id, name FROM vendors WHERE LOWER(name) LIKE ? "
                "AND LOWER(name) NOT LIKE '%do not use%' LIMIT 1",
                (f"%{kw}%",)
            ).fetchall()
            if rows:
                _known_vendor_map[kw] = {"id": rows[0][0], "name": rows[0][1]}
        conn.close()
    except Exception as e:
        print(f"Warning: Could not load known vendors: {e}")

_load_known_vendors()

def _load_user_vendor_map() -> dict:
    if VENDOR_MAPPING_PATH.exists():
        try:
            return json.loads(VENDOR_MAPPING_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_vendor_mapping(quote_name: str, st_vendor_name: str):
    """Save vendor mapping from Smartsheet approval email."""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT id, name FROM vendors WHERE LOWER(name) LIKE ? "
            "AND LOWER(name) NOT LIKE '%do not use%' LIMIT 1",
            (f"%{st_vendor_name.lower()[:30]}%",)
        ).fetchall()
        conn.close()
        if rows:
            mapping = _load_user_vendor_map()
            mapping[quote_name.lower().strip()] = {"id": rows[0][0], "name": rows[0][1]}
            VENDOR_MAPPING_PATH.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
            print(f"  ✓ Vendor mapping saved: '{quote_name}' → '{rows[0][1]}'")
            return True
    except Exception as e:
        print(f"  ⚠ Could not save vendor mapping: {e}")
    return False

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def _load_env():
    for env_file in [
        Path(__file__).parent.parent / ".env",
        Path("C:/Program Files/ST_MCP/.env"),
    ]:
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
            break

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
    return f"{API_BASE}/{api}/v2/tenant/{os.environ['ST_TENANT_ID']}/{path}"

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
# Vendor lookup — 3-tier
# ---------------------------------------------------------------------------
def find_vendor_id(vendor_name: str) -> Tuple[int, str, bool]:
    """
    Returns (vendor_id, st_vendor_name, is_confident_match)
    Tier 1: Known hard-coded vendors
    Tier 2: User-approved mappings from vendor_mapping.json
    Tier 3: Cache fuzzy search
    Fallback: Default Replenishment Vendor
    """
    if not vendor_name:
        return DEFAULT_VENDOR_ID, "Default Replenishment Vendor", False

    name_lower = vendor_name.lower()

    for kw, info in _known_vendor_map.items():
        if kw in name_lower:
            return info["id"], info["name"], True

    user_map = _load_user_vendor_map()
    if name_lower.strip() in user_map:
        info = user_map[name_lower.strip()]
        return info["id"], info["name"], True

    try:
        first_word = name_lower.split()[0] if name_lower.split() else name_lower
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT id, name FROM vendors WHERE LOWER(name) LIKE ? "
            "AND LOWER(name) NOT LIKE '%do not use%' LIMIT 1",
            (f"%{first_word}%",)
        ).fetchall()
        conn.close()
        if rows:
            return rows[0][0], rows[0][1], False
    except Exception:
        pass

    return DEFAULT_VENDOR_ID, "Default Replenishment Vendor", False

# ---------------------------------------------------------------------------
# Job reference extraction
# ---------------------------------------------------------------------------
def extract_job_reference(text: str) -> Optional[str]:
    if not text:
        return None
    for pattern in [
        r'job\s*#?\s*(\d{5,})',
        r'job\s*number\s*:?\s*(\d{5,})',
        r'work\s*order\s*#?\s*(\d{5,})',
        r'wo\s*#?\s*(\d{5,})',
        r'po\s*#?\s*(\d{5,})',
        r'#\s*(\d{6,})',
    ]:
        m = re.search(pattern, text.lower())
        if m:
            return m.group(1)
    return None

# ---------------------------------------------------------------------------
# Job lookup
# ---------------------------------------------------------------------------
def find_job_id(job_ref: str) -> Optional[dict]:
    if not job_ref or job_ref.lower() in ('test', ''):
        return None
    try:
        for params in [
            {"number": job_ref, "pageSize": 5},
            {"name": job_ref, "jobStatus": "InProgress,Scheduled,Dispatched", "pageSize": 5},
        ]:
            data  = _get(("jpm", "jobs"), params)
            items = data.get("data", [])
            if items:
                j = items[0]
                c = j.get("customer") or {}
                return {
                    "id":             j["id"],
                    "jobNumber":      j.get("number", ""),
                    "customerName":   c.get("name", "") if isinstance(c, dict) else "",
                    "businessUnitId": j.get("businessUnitId"),
                }
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# Existing PO lookup
# ---------------------------------------------------------------------------
def find_existing_po_on_job(job_id: int, vendor_id: int) -> Optional[dict]:
    try:
        data = _get(("inventory", "purchase-orders"), {
            "jobId": job_id, "status": "Pending", "pageSize": 10,
        })
        for po in data.get("data", []):
            if po.get("vendorId") == vendor_id:
                return {"id": po["id"], "number": po.get("number", str(po["id"]))}
    except Exception:
        pass
    return None

def get_po_display_number(po_id: int) -> str:
    try:
        return _get(("inventory", f"purchase-orders/{po_id}"), {}).get("number", str(po_id))
    except Exception:
        return str(po_id)

def get_default_po_type_id() -> int:
    return DEFAULT_PO_TYPE_ID

# ---------------------------------------------------------------------------
# SKU lookup
# ---------------------------------------------------------------------------
def find_sku_id(vendor_part_no: str) -> Tuple[int, bool]:
    if not vendor_part_no:
        return GENERIC_SKU_ID, False
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT id FROM pricebook_materials WHERE LOWER(code) = ? LIMIT 1",
            (vendor_part_no.lower().strip(),)
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT id FROM pricebook_materials WHERE LOWER(name) LIKE ? LIMIT 1",
                (f"%{vendor_part_no.lower()[:20]}%",)
            ).fetchone()
        conn.close()