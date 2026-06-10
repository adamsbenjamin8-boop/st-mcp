"""
Quote App — Configuration
=========================
All configurable settings in one place.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# ServiceTitan credentials (loaded from .env file next to this script)
# ---------------------------------------------------------------------------
ST_CLIENT_ID     = os.environ.get("ST_CLIENT_ID", "")
ST_CLIENT_SECRET = os.environ.get("ST_CLIENT_SECRET", "")
ST_APP_KEY       = os.environ.get("ST_APP_KEY", "")
ST_TENANT_ID     = os.environ.get("ST_TENANT_ID", "")

# ---------------------------------------------------------------------------
# Email accounts (for reference — monitored via Zapier)
# ---------------------------------------------------------------------------
ORDERS_EMAIL    = "Orders@denommeeplumbing.com"     # → PO creation workflow
ESTIMATES_EMAIL = "Estimates@denommeeplumbing.com"  # → add to estimate workflow

# ---------------------------------------------------------------------------
# OneDrive folder — Zapier drops incoming quote attachments here
# Set QUOTES_INBOX_FOLDER to the full path of the folder on this computer.
# ---------------------------------------------------------------------------
# Purchasing folders live in Documents so they're easy to find and already OneDrive-synced
_docs = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "OneDrive - Denommee Plumbing and Heating" / "Documents"
QUOTES_INBOX_FOLDER    = _docs / "Purchasing" / "Incoming Quotes"
ESTIMATES_INBOX_FOLDER = _docs / "Purchasing" / "Incoming Estimates"
PROCESSED_FOLDER       = _docs / "Purchasing" / "Processed"
QUARANTINE_DIR         = _docs / "Purchasing" / "Quarantine"

# ---------------------------------------------------------------------------
# Teams — Incoming Webhook URL for Purchasing channel notifications
# Create one in Teams: Purchasing channel → ... → Manage channel → Connectors
# → Incoming Webhook → Configure → Copy URL and paste below
# ---------------------------------------------------------------------------
TEAMS_PURCHASING_WEBHOOK = os.environ.get(
    "TEAMS_PURCHASING_WEBHOOK",
    ""   # ← paste webhook URL here, or set as env var
)

# ---------------------------------------------------------------------------
# Smartsheet — Quote Parser Log (auto-configured)
# ---------------------------------------------------------------------------
SMARTSHEET_API_KEY      = os.environ.get("SMARTSHEET_API_KEY", "")
QUOTE_PARSER_LOG_SHEET  = 4884926238248836   # Quote Parser Log sheet ID
MISSING_PARTS_SHEET     = 7913389283037060   # Missing Parts Queue sheet ID

# Smartsheet column IDs for Quote Parser Log
QLOG_COL_VENDOR         = 7714584764125060
QLOG_COL_DATE           = 2085085229911940
QLOG_COL_FILE           = 6588684857282436
QLOG_COL_PARSED_BY      = 4336885043597188
QLOG_COL_PARSER_ADDED   = 8840484670967684
QLOG_COL_ITEMS          = 255497881292676
QLOG_COL_NOTES          = 4759097508663172

# ---------------------------------------------------------------------------
# Vendor parsers folder
# ---------------------------------------------------------------------------
PARSERS_DIR = Path(__file__).parent.parent / "quote_parsers"

# ---------------------------------------------------------------------------
# ST API base URLs
# ---------------------------------------------------------------------------
ST_AUTH_URL = "https://auth.servicetitan.io/connect/token"
ST_API_BASE = "https://api.servicetitan.io"

