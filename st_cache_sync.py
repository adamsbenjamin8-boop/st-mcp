#!/usr/bin/env python3
"""
ServiceTitan Local Cache Sync
==============================
Pulls customers, locations, vendors, business units, and technicians
from the ServiceTitan API and stores them in a local SQLite database
at C:/ST/st_cache.db for fast lookups.

Run once manually to build the initial cache, then schedule nightly.

Usage:
    python st_cache_sync.py

Environment variables (same as servicetitan_writer.py):
    ST_CLIENT_ID, ST_CLIENT_SECRET, ST_APP_KEY, ST_TENANT_ID
"""

import os
import pathlib
import sqlite3
import time
import json
import httpx
from datetime import datetime

DB_PATH = "C:/ST/st_cache.db"

# Ensure the directory exists (important on non-dev machines)
pathlib.Path("C:/ST").mkdir(parents=True, exist_ok=True)
AUTH_BASE = "https://auth.servicetitan.io"
API_BASE  = "https://api.servicetitan.io"
TOKEN_URL = f"{AUTH_BASE}/connect/token"

_token_cache = {"access_token": None, "expires_at": 0.0}


def _require_env(name):
    val = os.environ.get(name, "")
    if not val:
        raise RuntimeError(f"Missing environment variable: {name}")
    return val


def _get_token():
    now = time.monotonic()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 30:
        return _token_cache["access_token"]
    resp = httpx.post(TOKEN_URL, data={
        "grant_type":    "client_credentials",
        "client_id":     _require_env("ST_CLIENT_ID"),
        "client_secret": _require_env("ST_CLIENT_SECRET"),
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"]   = now + data.get("expires_in", 3600)
    return _token_cache["access_token"]


def _headers():
    return {
        "Authorization": f"Bearer {_get_token()}",
        "ST-App-Key":    _require_env("ST_APP_KEY"),
        "Content-Type":  "application/json",
    }


def _url(api, path):
    tenant = _require_env("ST_TENANT_ID")
    return f"{API_BASE}/{api}/v2/tenant/{tenant}/{path}"


def _fetch_all(api, path, params=None):
    """Fetch all pages of a paginated endpoint."""
    all_data = []
    page = 1
    while True:
        p = {"page": page, "pageSize": 500}
        if params:
            p.update(params)
        resp = httpx.get(_url(api, path), headers=_headers(), params=p, timeout=60)
        if resp.status_code != 200:
            print(f"  WARNING: {path} returned {resp.status_code}")
            break
        data = resp.json()
        items = data.get("data", [])
        all_data.extend(items)
        print(f"  {path}: page {page}, got {len(items)} items (total so far: {len(all_data)})")
        if not data.get("hasMore"):
            break
        page += 1
    return all_data


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY,
            name TEXT,
            type TEXT,
            active INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY,
            name TEXT,
            customer_id INTEGER,
            address TEXT,
            city TEXT,
            state TEXT,
            zip TEXT,
            active INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY,
            name TEXT,
            type TEXT,
            value TEXT,
            customer_id INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS vendors (
            id INTEGER PRIMARY KEY,
            name TEXT,
            active INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS business_units (
            id INTEGER PRIMARY KEY,
            name TEXT,
            active INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS technicians (
            id INTEGER PRIMARY KEY,
            name TEXT,
            active INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS po_types (
            id INTEGER PRIMARY KEY,
            name TEXT,
            active INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS inventory_locations (
            id INTEGER PRIMARY KEY,
            name TEXT,
            active INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS pricebook_services (
            id INTEGER PRIMARY KEY,
            name TEXT,
            code TEXT,
            active INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS pricebook_materials (
            id INTEGER PRIMARY KEY,
            name TEXT,
            code TEXT,
            active INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS pricebook_equipment (
            id INTEGER PRIMARY KEY,
            name TEXT,
            code TEXT,
            active INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS job_types (
            id INTEGER PRIMARY KEY,
            name TEXT,
            active INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY,
            name TEXT,
            active INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS tag_types (
            id INTEGER PRIMARY KEY,
            name TEXT,
            type TEXT,
            active INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS job_cancel_reasons (
            id INTEGER PRIMARY KEY,
            name TEXT,
            active INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS membership_types (
            id INTEGER PRIMARY KEY,
            name TEXT,
            active INTEGER,
            raw JSON
        );
        CREATE TABLE IF NOT EXISTS sync_log (
            table_name TEXT PRIMARY KEY,
            last_synced TEXT,
            record_count INTEGER
        );
    """)
    conn.commit()


def sync_table(conn, table, api, path, row_fn):
    print(f"\nSyncing {table}...")
    items = _fetch_all(api, path)
    conn.execute(f"DELETE FROM {table}")
    for item in items:
        row = row_fn(item)
        placeholders = ", ".join(["?"] * len(row))
        cols = ", ".join(row.keys())
        conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})", list(row.values()))
    conn.execute("""
        INSERT OR REPLACE INTO sync_log (table_name, last_synced, record_count)
        VALUES (?, ?, ?)
    """, (table, datetime.now().isoformat(), len(items)))
    conn.commit()
    print(f"  Done: {len(items)} {table} synced.")


def main():
    print(f"ServiceTitan Cache Sync - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Database: {DB_PATH}\n")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    # Customers
    sync_table(conn, "customers", "crm", "customers", lambda r: {
        "id":     r.get("id"),
        "name":   r.get("name", ""),
        "type":   r.get("type", ""),
        "active": 1 if r.get("active", True) else 0,
        "raw":    json.dumps(r),
    })

    # Locations
    sync_table(conn, "locations", "crm", "locations", lambda r: {
        "id":          r.get("id"),
        "name":        r.get("name", ""),
        "customer_id": r.get("customerId"),
        "address":     r.get("address", {}).get("street", "") if r.get("address") else "",
        "city":        r.get("address", {}).get("city", "") if r.get("address") else "",
        "state":       r.get("address", {}).get("state", "") if r.get("address") else "",
        "zip":         r.get("address", {}).get("zip", "") if r.get("address") else "",
        "active":      1 if r.get("active", True) else 0,
        "raw":         json.dumps(r),
    })

    # Vendors
    sync_table(conn, "vendors", "inventory", "vendors", lambda r: {
        "id":     r.get("id"),
        "name":   r.get("name", ""),
        "active": 1 if r.get("active", True) else 0,
        "raw":    json.dumps(r),
    })

    # Business Units
    sync_table(conn, "business_units", "settings", "business-units", lambda r: {
        "id":     r.get("id"),
        "name":   r.get("name", ""),
        "active": 1 if r.get("active", True) else 0,
        "raw":    json.dumps(r),
    })

    # Technicians
    sync_table(conn, "technicians", "settings", "technicians", lambda r: {
        "id":     r.get("id"),
        "name":   r.get("name", ""),
        "active": 1 if r.get("active", True) else 0,
        "raw":    json.dumps(r),
    })

    # PO Types
    sync_table(conn, "po_types", "inventory", "purchase-order-types", lambda r: {
        "id":     r.get("id"),
        "name":   r.get("name", ""),
        "active": 1 if r.get("active", True) else 0,
        "raw":    json.dumps(r),
    })

    # Inventory Locations
    sync_table(conn, "inventory_locations", "inventory", "warehouses", lambda r: {
        "id":     r.get("id"),
        "name":   r.get("name", ""),
        "active": 1 if r.get("active", True) else 0,
        "raw":    json.dumps(r),
    })

    # Pricebook Services
    sync_table(conn, "pricebook_services", "pricebook", "services", lambda r: {
        "id":     r.get("id"),
        "name":   r.get("name", ""),
        "code":   r.get("code", ""),
        "active": 1 if r.get("active", True) else 0,
        "raw":    json.dumps(r),
    })
    # Pricebook Materials
    sync_table(conn, "pricebook_materials", "pricebook", "materials", lambda r: {
        "id":     r.get("id"),
        "name":   r.get("name", ""),
        "code":   r.get("code", ""),
        "active": 1 if r.get("active", True) else 0,
        "raw":    json.dumps(r),
    })
    # Pricebook Equipment
    sync_table(conn, "pricebook_equipment", "pricebook", "equipment", lambda r: {
        "id":     r.get("id"),
        "name":   r.get("name", ""),
        "code":   r.get("code", ""),
        "active": 1 if r.get("active", True) else 0,
        "raw":    json.dumps(r),
    })
    # Job Types
    sync_table(conn, "job_types", "jpm", "job-types", lambda r: {
        "id":     r.get("id"),
        "name":   r.get("name", ""),
        "active": 1 if r.get("active", True) else 0,
        "raw":    json.dumps(r),
    })
    # Campaigns
    sync_table(conn, "campaigns", "crm", "campaigns", lambda r: {
        "id":     r.get("id"),
        "name":   r.get("name", ""),
        "active": 1 if r.get("active", True) else 0,
        "raw":    json.dumps(r),
    })

    # Tag Types
    sync_table(conn, "tag_types", "jpm", "tag-types", lambda r: {
        "id":     r.get("id"),
        "name":   r.get("name", ""),
        "type":   r.get("type", ""),
        "active": 1 if r.get("active", True) else 0,
        "raw":    json.dumps(r),
    })

    # Job Cancel Reasons
    sync_table(conn, "job_cancel_reasons", "jpm", "job-cancel-reasons", lambda r: {
        "id":     r.get("id"),
        "name":   r.get("name", ""),
        "active": 1 if r.get("active", True) else 0,
        "raw":    json.dumps(r),
    })

    # Membership Types
    sync_table(conn, "membership_types", "memberships", "membership-types", lambda r: {
        "id":     r.get("id"),
        "name":   r.get("name", ""),
        "active": 1 if r.get("active", True) else 0,
        "raw":    json.dumps(r),
    })

    conn.close()
    print(f"\nSync complete: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
