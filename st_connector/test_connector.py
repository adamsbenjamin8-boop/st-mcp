"""
ST Connector Connectivity Test
Run this BEFORE the first full sync to verify all credentials and
API endpoints are reachable. Does NOT write any data to either system.

Usage:
    python test_connector.py

Exit codes:
    0 = all checks passed
    1 = one or more checks failed
"""

import sys
import json
import os
import requests
from datetime import datetime, timezone

# ── Load config ──────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.py")

try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", CONFIG_PATH)
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    # config._load_env() populates os.environ — expose keys as attributes for convenience
    for _k in ("ST_CLIENT_ID", "ST_CLIENT_SECRET", "ST_APP_KEY", "ST_TENANT_ID", "SMARTSHEET_API_KEY"):
        if not hasattr(config, _k):
            setattr(config, _k, os.environ.get(_k, ""))
except Exception as e:
    print(f"FAIL  Could not load config.py: {e}")
    sys.exit(1)

PASS = "\033[92m PASS\033[0m"
FAIL = "\033[91m FAIL\033[0m"

results = []

def check(label, ok, detail=""):
    status = PASS if ok else FAIL
    print(f"{status}  {label}" + (f"\n       {detail}" if detail else ""))
    results.append(ok)
    return ok


# ── 1. ServiceTitan token ────────────────────────────────────────────────────
print("\n── ServiceTitan ────────────────────────────────────────────────────────")
try:
    resp = requests.post(
        "https://auth.servicetitan.io/connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": config.ST_CLIENT_ID,
            "client_secret": config.ST_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    token_data = resp.json()
    token = token_data["access_token"]
    check("ST OAuth token", True, f"expires_in={token_data.get('expires_in')}s")
except Exception as e:
    check("ST OAuth token", False, str(e))
    print("\nCannot continue ST checks without a token.")
    token = None


if token:
    headers = {"Authorization": f"Bearer {token}", "ST-App-Key": config.ST_APP_KEY}
    base = "https://api.servicetitan.io"
    tenant = config.ST_TENANT_ID

    for name, path in [
        ("appointments",    f"/dispatch/v2/tenant/{tenant}/appointments"),
        ("jobs",            f"/jpm/v2/tenant/{tenant}/jobs"),
        ("invoices",        f"/accounting/v2/tenant/{tenant}/invoices"),
        ("job-orders",      f"/jpm/v2/tenant/{tenant}/job-orders"),
        ("estimates",       f"/sales/v2/tenant/{tenant}/estimates"),
        ("purchase-orders", f"/inventory/v2/tenant/{tenant}/purchase-orders"),
    ]:
        try:
            r = requests.get(base + path, headers=headers, params={"page": 1, "pageSize": 1}, timeout=15)
            r.raise_for_status()
            d = r.json()
            check(f"ST {name} endpoint", True, f"totalCount={d.get('totalCount', '?')}")
        except Exception as e:
            check(f"ST {name} endpoint", False, str(e))

    # JWT scopes
    try:
        import base64 as _b64
        parts = token.split(".")
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(_b64.urlsafe_b64decode(padded))
        scopes = payload.get("scope", "").split()
        write_scopes = [s for s in scopes if s.endswith(":w")]
        read_scopes  = [s for s in scopes if s.endswith(":r")]
        check("JWT read scopes",  len(read_scopes) > 0,  f"{len(read_scopes)} read scopes")
        check("JWT write scopes", len(write_scopes) > 0, f"{len(write_scopes)} write scopes" if write_scopes else "NONE — write-back disabled")
    except Exception as e:
        check("JWT scope decode", False, str(e))


# ── 2. Smartsheet ────────────────────────────────────────────────────────────
print("\n── Smartsheet ──────────────────────────────────────────────────────────")
SS_SHEETS = {
    "Appointments":    getattr(config, "APPOINTMENTS_SHEET_ID",  7904757385482116),
    "Invoice KPI":     getattr(config, "INVOICES_SHEET_ID",       3671995526893444),
    "Jobs KPI":        getattr(config, "JOBS_SHEET_ID",           8773592040820612),
    "Tasks KPI":       getattr(config, "TASKS_SHEET_ID",          2996979944607620),
    "Estimates":       getattr(config, "ESTIMATES_SHEET_ID",      1600229966040964),
    "Purchase Orders": getattr(config, "POS_SHEET_ID",            7840808857194372),
}

try:
    ss_headers = {
        "Authorization": f"Bearer {config.SMARTSHEET_API_KEY}",
        "Content-Type": "application/json",
    }
    r = requests.get("https://api.smartsheet.com/2.0/users/me", headers=ss_headers, timeout=15)
    r.raise_for_status()
    me = r.json()
    check("Smartsheet API key", True, f"user={me.get('email', '?')}")

    for name, sheet_id in SS_SHEETS.items():
        try:
            r = requests.get(
                f"https://api.smartsheet.com/2.0/sheets/{sheet_id}",
                headers=ss_headers,
                params={"include": "columnIds"},
                timeout=15,
            )
            r.raise_for_status()
            d = r.json()
            check(f"SS '{name}'", True, f"rows={d.get('totalRowCount', '?')}, id={sheet_id}")
        except Exception as e:
            check(f"SS '{name}'", False, str(e))

except Exception as e:
    check("Smartsheet API key", False, str(e))


# ── 3. Sync state ────────────────────────────────────────────────────────────
print("\n── Sync state ──────────────────────────────────────────────────────────")
state_path = os.path.join(os.path.dirname(__file__), ".sync_state.json")
if os.path.exists(state_path):
    with open(state_path) as f:
        state = json.load(f)
    check("Sync state file", True,
          f"backfill_complete={state.get('backfill_complete')}, last_sync={state.get('last_sync','?')}")
else:
    print(" INFO  No .sync_state.json — first run will do a full backfill.")
    results.append(True)


# ── Summary ──────────────────────────────────────────────────────────────────
print()
total = len(results)
passed = sum(results)
failed = total - passed
if failed == 0:
    print(f"✅  All {total} checks passed — connector is ready.")
    sys.exit(0)
else:
    print(f"❌  {failed}/{total} checks failed — fix the issues above before running.")
    sys.exit(1)
