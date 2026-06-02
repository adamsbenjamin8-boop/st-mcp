"""
Teams Notifier — posts PO approval notifications to the Purchasing channel.
Uses a Teams Incoming Webhook URL.
"""

import httpx
from typing import Optional
from config import TEAMS_PURCHASING_WEBHOOK


def send_po_notification(
    vendor: str,
    total: float,
    job_name: str,
    po_id: int,
    item_count: int,
    notes: str = "",
) -> bool:
    """
    Post a PO approval notification to the Purchasing Teams channel.
    Returns True if successful.
    """
    if not TEAMS_PURCHASING_WEBHOOK:
        print("WARNING: TEAMS_PURCHASING_WEBHOOK not configured — skipping Teams notification")
        return False

    po_url = f"https://go.servicetitan.com/#/Inventory/PurchaseOrder/{po_id}"

    job_display = job_name if job_name and job_name.lower() not in ('test', '') else "Unassigned — please assign job"
    notes_section = f"\n📝 {notes}" if notes else ""

    # Teams message card format
    message = {
        "@type":      "MessageCard",
        "@context":   "https://schema.org/extensions",
        "summary":    f"New PO Request — {vendor}",
        "themeColor": "0078D4",
        "title":      f"🧾 New PO Request — {vendor}",
        "sections": [
            {
                "facts": [
                    {"name": "Job",        "value": job_display},
                    {"name": "Vendor",     "value": vendor},
                    {"name": "Items",      "value": str(item_count)},
                    {"name": "Total",      "value": f"${total:,.2f}"},
                ],
                "text": notes_section,
            }
        ],
        "potentialAction": [
            {
                "@type": "OpenUri",
                "name":  "Review & Approve in ServiceTitan",
                "targets": [{"os": "default", "uri": po_url}],
            }
        ],
    }

    try:
        resp = httpx.post(TEAMS_PURCHASING_WEBHOOK, json=message, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"Teams notification failed: {e}")
        return False
