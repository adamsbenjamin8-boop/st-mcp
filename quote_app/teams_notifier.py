"""
Teams Notifier — sends notifications via Power Automate Workflows webhook.
Uses Adaptive Card format (required by Workflows webhooks; MessageCard is legacy).
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


def _post(payload: dict) -> bool:
    webhook_url = os.environ.get("TEAMS_PURCHASING_WEBHOOK", "")
    if not webhook_url:
        print("  ⚠ TEAMS_PURCHASING_WEBHOOK not configured — skipping")
        return False
    try:
        r = httpx.post(webhook_url, json=payload, timeout=15)
        if r.status_code == 202 or r.status_code == 200:
            return True
        print(f"  ❌ Teams webhook returned {r.status_code}: {r.text[:120]}")
        return False
    except Exception as e:
        print(f"  ❌ Teams webhook error: {e}")
        return False


def _adaptive_card(body: list, actions: list | None = None) -> dict:
    """Wrap Adaptive Card body into the Workflows webhook envelope."""
    card: dict = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type":    "AdaptiveCard",
        "version": "1.2",
        "body":    body,
    }
    if actions:
        card["actions"] = actions
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content":     card,
        }],
    }


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
    po_url = f"https://go.servicetitan.com/#/new/inventory/purchase-orders/details/{po_id}"
    today  = datetime.now().strftime("%m/%d/%Y %I:%M %p")

    vendor_value  = f"✅ {vendor_st_name}" if vendor_matched else f"⚠️ {vendor_st_name} — verify before approving"
    header_color  = "Good" if vendor_matched else "Warning"

    facts = [
        {"title": "PO Number", "value": po_number},
        {"title": "Vendor",    "value": vendor_value},
        {"title": "Total",     "value": f"${total:,.2f}"},
        {"title": "Items",     "value": str(item_count)},
        {"title": "Job",       "value": job_name},
        {"title": "Time",      "value": today},
    ]
    if unmatched_count:
        facts.append({
            "title": "⚠️ Pricebook",
            "value": f"{unmatched_count} items used HMIL fallback — check Missing Parts Queue",
        })
    if notes:
        facts.append({"title": "Notes", "value": notes})

    body = [
        {
            "type":   "TextBlock",
            "text":   f"📦 New Purchase Order — {vendor_st_name}",
            "weight": "Bolder",
            "size":   "Medium",
            "color":  header_color,
            "wrap":   True,
        },
        {
            "type":     "TextBlock",
            "text":     f"PO #{po_number} | ${total:,.2f}",
            "isSubtle": True,
            "spacing":  "None",
        },
        {
            "type":  "FactSet",
            "facts": facts,
        },
    ]

    actions = [{
        "type":  "Action.OpenUrl",
        "title": "Open PO in ServiceTitan",
        "url":   po_url,
    }]

    return _post(_adaptive_card(body, actions))


def send_teams_alert(title: str, message: str, sender: str = "") -> bool:
    """Post a plain-text alert card to the purchasing Teams channel."""
    facts = []
    if sender:
        facts.append({"title": "From", "value": sender})

    body: list = [
        {
            "type":   "TextBlock",
            "text":   title,
            "weight": "Bolder",
            "size":   "Medium",
            "color":  "Warning",
            "wrap":   True,
        },
        {
            "type": "TextBlock",
            "text": message,
            "wrap": True,
        },
    ]
    if facts:
        body.append({"type": "FactSet", "facts": facts})

    return _post(_adaptive_card(body))


def send_ack_notification(filename: str, sender: str, vendor: str = "Unknown") -> bool:
    """Send a Teams alert when an order acknowledgment is quarantined."""
    return send_teams_alert(
        title="📋 Order Acknowledgment Received — Manual Review Needed",
        message=(
            f"File **{filename}** is an order acknowledgment, not a new quote. "
            f"No PO was created. It has been saved to Quarantine.\n\n"
            f"Check the original email and verify this order in ServiceTitan."
        ),
        sender=sender or vendor,
    )
