"""
Teams Notifier — sends PO notifications via incoming webhook.
No SMTP. Uses HTTP POST directly to Teams channel.
"""
import os
import httpx
from datetime import datetime
from pathlib import Path


def _load_env():
    env_file = Path("C:/Program Files/ST_MCP/.env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

_load_env()


def send_po_notification(
    vendor: str,
    vendor_matched: bool,
    vendor_st_name: str,
    total: float,
    job_name: str,
    po_id: int,
    po_number: str,
    item_count: int,
    notes: str = "",
    unmatched_count: int = 0,
) -> bool:
    webhook_url = os.environ.get("TEAMS_PURCHASING_WEBHOOK", "")
    if not webhook_url:
        print("  ⚠ TEAMS_PURCHASING_WEBHOOK not configured — skipping")
        return False

    po_url = f"https://go.servicetitan.com/#/new/inventory/purchase-orders/details/{po_id}"
    today  = datetime.now().strftime("%m/%d/%Y %I:%M %p")

    # Vendor line with confidence flag
    if vendor_matched:
        vendor_line = f"✅ {vendor_st_name}"
        theme_color = "0076D7"
    else:
        vendor_line = f"⚠️ {vendor_st_name} — verify before approving"
        theme_color = "FF8C00"

    facts = [
        {"name": "PO Number", "value": po_number},
        {"name": "Vendor",    "value": vendor_line},
        {"name": "Total",     "value": f"${total:,.2f}"},
        {"name": "Items",     "value": str(item_count)},
        {"name": "Job",       "value": job_name},
        {"name": "Time",      "value": today},
    ]

    if unmatched_count:
        facts.append({
            "name":  "⚠️ Pricebook",
            "value": f"{unmatched_count} items used HMIL fallback — check Missing Parts Queue"
        })

    if notes:
        facts.append({"name": "Notes", "value": notes})

    payload = {
        "@type":    "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary":      f"New PO: {vendor_st_name}",
        "themeColor":   theme_color,
        "sections": [{
            "activityTitle":    f"📦 New Purchase Order — {vendor_st_name}",
            "activitySubtitle": f"PO #{po_number} | ${total:,.2f}",
            "facts":            facts,
        }],
        "potentialAction": [{
            "@type": "OpenUri",
            "name":  "Open PO in ServiceTitan",
            "targets": [{"os": "default", "uri": po_url}]
        }]
    }

    try:
        r = httpx.post(webhook_url, json=payload, timeout=15)
        if r.status_code == 200:
            return True
        print(f"  ❌ Teams webhook returned {r.status_code}: {r.text[:100]}")
        return False
    except Exception as e:
        print(f"  ❌ Teams notification error: {e}")
        return False


def send_teams_alert(title: str, message: str, sender: str = "") -> bool:
    """Post a plain-text alert card to the purchasing Teams channel."""
    webhook_url = os.environ.get("TEAMS_PURCHASING_WEBHOOK", "")
    if not webhook_url:
        return False

    facts = []
    if sender:
        facts.append({"name": "From", "value": sender})

    payload = {
        "@type":    "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary":    title,
        "themeColor": "FF8C00",
        "sections": [{
            "activityTitle": title,
            "activityText":  message,
            "facts":         facts,
        }],
    }

    try:
        r = httpx.post(webhook_url, json=payload, timeout=15)
        if r.status_code == 200:
            return True
        print(f"  ❌ Teams alert returned {r.status_code}: {r.text[:100]}")
        return False
    except Exception as e:
        print(f"  ❌ Teams alert error: {e}")
        return False


def send_ack_notification(filename: str, sender: str, vendor: str = "Unknown") -> bool:
    """Send a simple Teams alert when an order acknowledgment is quarantined."""
    webhook_url = os.environ.get("TEAMS_PURCHASING_WEBHOOK", "")
    if not webhook_url:
        return False

    payload = {
        "@type":    "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary":    f"Order Acknowledgment Received — {filename}",
        "themeColor": "FF8C00",
        "sections": [{
            "activityTitle":    "📋 Order Acknowledgment Received — Manual Review Needed",
            "activitySubtitle": "This is a confirmation of an existing order, not a new quote. No PO was created.",
            "facts": [
                {"name": "File",   "value": filename},
                {"name": "Vendor", "value": vendor},
                {"name": "From",   "value": sender or "Unknown"},
                {"name": "Action", "value": "File saved to Quarantine — check the original email and verify the order in ServiceTitan"},
            ],
        }],
    }

    try:
        r = httpx.post(webhook_url, json=payload, timeout=15)
        if r.status_code == 200:
            return True
        print(f"  ❌ Teams ack notification returned {r.status_code}: {r.text[:100]}")
        return False
    except Exception as e:
        print(f"  ❌ Teams ack notification error: {e}")
        return False
