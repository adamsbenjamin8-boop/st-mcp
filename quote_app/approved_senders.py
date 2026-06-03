"""
Approved Vendor Email Senders
==============================
Manages the local cache of approved vendor email domains.

Security model:
  - Only emails from approved domains are processed into PO Requests
  - Unknown domains are quarantined and added to Smartsheet for approval
  - Approval requires a UUID token AND the vendor's ServiceTitan vendor ID to match
  - Both must be correct for the cache to update — prevents spoofed approvals

Smartsheet: "Approved Vendor Emails" (ServiceTitan Live Data workspace)
Local cache: C:/Program Files/ST_MCP/approved_senders.json
"""

import json
import os
import uuid
import httpx
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Smartsheet config
# ---------------------------------------------------------------------------
SMARTSHEET_API_KEY    = os.environ.get("SMARTSHEET_API_KEY", "")
APPROVED_SENDERS_SHEET = 1832230987976580   # Approved Vendor Emails sheet ID

# Column IDs
COL_VENDOR_NAME    = 1191779545419652
COL_EMAIL_DOMAIN   = 5695379172790148
COL_ST_VENDOR_ID   = 3443579359104900
COL_TOKEN          = 7947178986475396
COL_STATUS         = 628829591998340
COL_APPROVED_BY    = 5132429219368836
COL_DATE_ADDED     = 2880629405683588
COL_DATE_APPROVED  = 7384229033054084
COL_NOTES          = 1754729498840964

# Local cache file
CACHE_FILE = Path("C:/Program Files/ST_MCP/approved_senders.json")

# Approval email marker — must appear in subject of approval confirmation
APPROVAL_MARKER = "[VENDOR APPROVED]"


# ---------------------------------------------------------------------------
# Local cache helpers
# ---------------------------------------------------------------------------

def load_cache() -> dict:
    """
    Load approved_senders.json from disk.
    Returns dict: {email_domain: {vendor_name, st_vendor_id, approved_by, date_approved}}
    """
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_cache(cache: dict):
    """Write the approved senders cache to disk."""
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        print(f"  ⚠ Could not save approved_senders.json: {e}")


def is_approved(email_domain: str) -> bool:
    """Check if an email domain is in the local approved cache."""
    cache = load_cache()
    domain = email_domain.lower().lstrip("@")
    return domain in cache


def get_domain(email: str) -> str:
    """Extract domain from an email address."""
    return email.lower().split("@")[-1] if "@" in email else email.lower()


# ---------------------------------------------------------------------------
# Smartsheet sync
# ---------------------------------------------------------------------------

def _ss_headers() -> dict:
    return {
        "Authorization": f"Bearer {SMARTSHEET_API_KEY}",
        "Content-Type":  "application/json",
    }


def sync_from_smartsheet():
    """
    Pull approved rows from Smartsheet and update local cache.
    Called on startup and every 15 minutes.
    """
    if not SMARTSHEET_API_KEY:
        return

    try:
        resp = httpx.get(
            f"https://api.smartsheet.com/2.0/sheets/{APPROVED_SENDERS_SHEET}",
            headers=_ss_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        sheet = resp.json()
    except Exception as e:
        print(f"  ⚠ Could not sync approved senders from Smartsheet: {e}")
        return

    cache = {}
    col_idx = {c["id"]: c["title"] for c in sheet.get("columns", [])}

    for row in sheet.get("rows", []):
        cells = {c["columnId"]: c.get("value", "") for c in row.get("cells", [])}
        status = str(cells.get(COL_STATUS, "")).strip()
        if status != "Approved":
            continue
        domain = str(cells.get(COL_EMAIL_DOMAIN, "")).lower().lstrip("@").strip()
        if not domain:
            continue
        cache[domain] = {
            "vendor_name":   str(cells.get(COL_VENDOR_NAME, "")),
            "st_vendor_id":  str(cells.get(COL_ST_VENDOR_ID, "")),
            "approved_by":   str(cells.get(COL_APPROVED_BY, "")),
            "date_approved": str(cells.get(COL_DATE_APPROVED, "")),
        }

    save_cache(cache)
    print(f"  ✓ Approved senders synced: {len(cache)} domains")


# ---------------------------------------------------------------------------
# Unknown vendor handling
# ---------------------------------------------------------------------------

def queue_for_approval(vendor_name: str, email_domain: str, st_vendor_id: str,
                        sender_email: str, notes: str = "") -> str:
    """
    Add an unknown vendor to Smartsheet for approval.
    Returns the approval token.
    """
    token = str(uuid.uuid4()).replace("-", "")[:16].upper()
    today = date.today().isoformat()

    row = {"cells": [
        {"columnId": COL_VENDOR_NAME,   "value": vendor_name},
        {"columnId": COL_EMAIL_DOMAIN,  "value": email_domain.lstrip("@")},
        {"columnId": COL_ST_VENDOR_ID,  "value": str(st_vendor_id)},
        {"columnId": COL_TOKEN,         "value": token},
        {"columnId": COL_STATUS,        "value": "Pending Approval"},
        {"columnId": COL_DATE_ADDED,    "value": today},
        {"columnId": COL_NOTES,         "value": notes or f"First email from: {sender_email}"},
    ]}

    if SMARTSHEET_API_KEY:
        try:
            httpx.post(
                f"https://api.smartsheet.com/2.0/sheets/{APPROVED_SENDERS_SHEET}/rows",
                headers=_ss_headers(),
                json={"rows": [row], "toBottom": True},
                timeout=15,
            )
        except Exception as e:
            print(f"  ⚠ Could not add to Approved Vendor Emails sheet: {e}")

    print(f"  📋 Queued for approval: {vendor_name} ({email_domain}) — token: {token}")
    return token


# ---------------------------------------------------------------------------
# Approval email processing
# ---------------------------------------------------------------------------

def process_approval_email(subject: str) -> bool:
    """
    Check if an email subject is a vendor approval confirmation.
    Format: [VENDOR APPROVED] domain.com | token:XXXX | stid:1234567

    Returns True if cache was updated successfully.
    """
    if APPROVAL_MARKER not in subject:
        return False

    import re
    domain_match = re.search(r'\[VENDOR APPROVED\]\s+([\w.\-]+)', subject)
    token_match  = re.search(r'token:([A-Z0-9]{16})', subject)
    stid_match   = re.search(r'stid:(\d+)', subject)

    if not (domain_match and token_match and stid_match):
        print(f"  ⚠ Approval email malformed, ignoring: {subject}")
        return False

    domain   = domain_match.group(1).lower()
    token    = token_match.group(1)
    st_id    = stid_match.group(1)

    # Validate against Smartsheet
    if not SMARTSHEET_API_KEY:
        return False

    try:
        resp = httpx.get(
            f"https://api.smartsheet.com/2.0/sheets/{APPROVED_SENDERS_SHEET}",
            headers=_ss_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        sheet = resp.json()
    except Exception:
        return False

    for row in sheet.get("rows", []):
        cells = {c["columnId"]: str(c.get("value", "")).strip() for c in row.get("cells", [])}
        row_domain = cells.get(COL_EMAIL_DOMAIN, "").lower().lstrip("@")
        row_token  = cells.get(COL_TOKEN, "")
        row_stid   = cells.get(COL_ST_VENDOR_ID, "")
        row_status = cells.get(COL_STATUS, "")

        if row_domain == domain and row_token == token and row_stid == st_id:
            if row_status != "Approved":
                print(f"  ⚠ Approval email received but Smartsheet row not yet Approved — ignoring")
                return False

            # All checks pass — update local cache immediately
            cache = load_cache()
            cache[domain] = {
                "vendor_name":   cells.get(COL_VENDOR_NAME, ""),
                "st_vendor_id":  st_id,
                "approved_by":   cells.get(COL_APPROVED_BY, ""),
                "date_approved": date.today().isoformat(),
            }
            save_cache(cache)
            print(f"  ✅ Vendor approved and cache updated immediately: {domain}")
            return True

    print(f"  ⚠ Approval email token/stid mismatch — rejected: {domain}")
    return False
