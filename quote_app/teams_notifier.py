"""
Teams Notifier — posts PO approval notifications to the Purchasing channel
by sending an email to the channel's email address.

Teams channel email: f34ce60e.denommeeplumbing.com@amer.teams.ms
Sent from: Orders@denommeeplumbing.com via Microsoft 365 SMTP
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Teams Purchasing channel email address
TEAMS_CHANNEL_EMAIL = "f34ce60e.denommeeplumbing.com@amer.teams.ms"

# Microsoft 365 SMTP settings
SMTP_HOST = "smtp.office365.com"
SMTP_PORT = 587

SENDER_EMAIL    = os.environ.get("ORDERS_EMAIL_ADDRESS", "Orders@denommeeplumbing.com")
SENDER_PASSWORD = os.environ.get("ORDERS_EMAIL_PASSWORD", "")


def send_po_notification(
    vendor: str,
    total: float,
    job_name: str,
    po_id: int,
    item_count: int,
    notes: str = "",
) -> bool:
    """
    Send a PO approval notification to the Purchasing Teams channel via email.
    Returns True if successful.
    """
    if not SENDER_PASSWORD:
        print("WARNING: ORDERS_EMAIL_PASSWORD not set — skipping Teams notification")
        return False

    po_url = f"https://go.servicetitan.com/#/Inventory/PurchaseOrder/{po_id}"
    job_display = job_name if job_name and job_name.lower() not in ('test', '') else "Unassigned — assign job before approving"

    subject = f"New PO Request — {vendor} | {job_display} | ${total:,.2f}"

    body_html = f"""
<html><body style="font-family: Arial, sans-serif; font-size: 14px;">
<h3 style="color: #0078D4;">🧾 New PO Request — {vendor}</h3>
<table style="border-collapse: collapse; margin-bottom: 16px;">
  <tr><td style="padding: 4px 12px 4px 0; color: #666;">Job</td><td style="padding: 4px 0;"><b>{job_display}</b></td></tr>
  <tr><td style="padding: 4px 12px 4px 0; color: #666;">Vendor</td><td style="padding: 4px 0;">{vendor}</td></tr>
  <tr><td style="padding: 4px 12px 4px 0; color: #666;">Items</td><td style="padding: 4px 0;">{item_count}</td></tr>
  <tr><td style="padding: 4px 12px 4px 0; color: #666;">Total</td><td style="padding: 4px 0;">${total:,.2f}</td></tr>
</table>
{"<p style='color:#cc0000;'>⚠ " + notes + "</p>" if notes else ""}
<a href="{po_url}" style="background:#0078D4;color:white;padding:10px 20px;text-decoration:none;border-radius:4px;display:inline-block;">
  Review &amp; Approve in ServiceTitan
</a>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = TEAMS_CHANNEL_EMAIL
    msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(SENDER_EMAIL, SENDER_PASSWORD)
            smtp.sendmail(SENDER_EMAIL, TEAMS_CHANNEL_EMAIL, msg.as_string())
        print(f"  ✓ Teams notification sent to Purchasing channel")
        return True
    except Exception as e:
        print(f"  ⚠ Teams notification failed: {e}")
        return False
