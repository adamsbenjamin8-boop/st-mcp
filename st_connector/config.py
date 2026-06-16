"""
st_connector — central configuration
All sheet IDs, column IDs, sync intervals, and feature flags live here.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Env loading — check C:/ST/ first, then Program Files, then repo root
# ---------------------------------------------------------------------------
def _load_env():
    candidates = [
        Path("C:/ST/.env"),
        Path("C:/Program Files/ST_MCP/.env"),
        Path(__file__).parent.parent / ".env",
    ]
    for p in candidates:
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
            break

_load_env()

# ---------------------------------------------------------------------------
# ServiceTitan
# ---------------------------------------------------------------------------
ST_AUTH_URL = "https://auth.servicetitan.io/connect/token"
ST_API_BASE = "https://api.servicetitan.io"

# ---------------------------------------------------------------------------
# Smartsheet
# ---------------------------------------------------------------------------
SS_API_BASE      = "https://api.smartsheet.com/2.0"
SS_WORKSPACE_ID  = 8604317599983492   # workspace where new sheets are created

# ---------------------------------------------------------------------------
# Existing sheets
# ---------------------------------------------------------------------------

# Appointments — sheet ID 7904757385482116
APPT_SHEET_ID    = 7904757385482116
APPT_COLS = {
    "appt_id":       6211071732043652,   # ST appointment ID  (unique key)
    "job_id":        3959271918358404,
    "appt_number":   300097221119876,
    "status":        4803696848490372,
    "start":         1369701381001092,   # ISO datetime string
    "end":           5873301008371588,
    "start_date":    1045188140814212,   # DATE type cell
}
# Columns to leave untouched (formula / Zapier columns)
APPT_READONLY_COLS = {
    "zap_date", "zap", "zap_TableRecordID",
}

# Invoice Report KPI — sheet ID 3671995526893444
INVOICE_SHEET_ID = 3671995526893444
INVOICE_COLS = {
    "invoice_id":    2008460738908036,   # ST invoice ID (unique key)
    "invoice_num":   7824066694303620,
    "job_id":        4827975381634948,
    "job_num":       2194567160090500,
    "customer_name": 1631617206669188,
    "status":        4446366973775748,   # PICKLIST: Exported, Pending, Bypassed
    "invoice_date":  4551920090042244,
    "total":         8949966601146244,
    "balance":       48320462671748,
    "batch_number":  1174220369514372,
    "export_id":     7929619810570116,
    "business_unit": 5178507136814980,
    "labor_costs":   5677819996884868,
    "material_costs":5114870043463556,
    "is_adjustment": 3426020183199620,   # CHECKBOX
    "empty_invoice": 611270416093060,    # CHECKBOX
    "income":        7366669857148804,
    "labor":         1737170322935684,
    "notes":         3988970136620932,
    "costs_total":   6803719903727492,
}

# Jobs KPI — sheet ID 8773592040820612
# Column IDs are looked up dynamically by title on first sync (see jobs_sync.py)
JOBS_SHEET_ID    = 8773592040820612
JOBS_COL_NAMES = {
    "job_id":           "Job ID",
    "job_num":          "Job #",
    "invoice_id":       "Invoice ID",
    "customer_name":    "Customer Name",
    "status":           "Job Status",
    "created_date":     "Created Date",
    "first_dispatch":   "First Dispatch",
    "completion_date":  "Completion Date",
    "location":         "Location Address",
    "job_type":         "Job Type",
    "invoice_status":   "Invoice Status",
    "total":            "Total",
    "balance":          "Balance",
    "batch_number":     "Batch Number",
    "business_unit":    "Business Unit",
}

# Tasks-KPI — sheet ID 2996979944607620
TASKS_SHEET_ID   = 2996979944607620
TASKS_COLS = {
    "task_id":       5788844565942148,   # unique key
    "job_id":        6070319542652804,
    "job_number":    6152058086576004,
    "description":   7477694426206084,
    "priority":      3537044752256900,   # PICKLIST: High, Normal, Urgent
    "type":          722294985150340,    # PICKLIST
    "status":        1848194891992964,   # PICKLIST: Open, Completed, ToDo, InProgress
    "due":           440820008439684,
    "customer_name": 1648458459205508,
    "business_unit": 7914097401876356,
}

# Estimates — new sheet, created on first run
ESTIMATES_SHEET_NAME = "Estimates"
ESTIMATES_COLUMNS = [
    {"title": "Estimate ID",   "type": "TEXT_NUMBER", "primary": True},
    {"title": "Estimate #",    "type": "TEXT_NUMBER"},
    {"title": "Job ID",        "type": "TEXT_NUMBER"},
    {"title": "Job #",         "type": "TEXT_NUMBER"},
    {"title": "Customer Name", "type": "TEXT_NUMBER"},
    {"title": "Status",        "type": "PICKLIST",    "options": ["Open", "Sold", "Dismissed"]},
    {"title": "Total",         "type": "TEXT_NUMBER"},
    {"title": "Created Date",  "type": "DATE"},
    {"title": "Sold Date",     "type": "DATE"},
    {"title": "Business Unit", "type": "TEXT_NUMBER"},
    {"title": "Technician",    "type": "TEXT_NUMBER"},
    {"title": "Notes",         "type": "TEXT_NUMBER"},
    {"title": "ST Link",       "type": "TEXT_NUMBER"},
]

# POs — new sheet, created on first run
POS_SHEET_NAME = "Purchase Orders"
POS_COLUMNS = [
    {"title": "PO ID",         "type": "TEXT_NUMBER", "primary": True},
    {"title": "PO #",          "type": "TEXT_NUMBER"},
    {"title": "Job ID",        "type": "TEXT_NUMBER"},
    {"title": "Job #",         "type": "TEXT_NUMBER"},
    {"title": "Vendor",        "type": "TEXT_NUMBER"},
    {"title": "Status",        "type": "PICKLIST",    "options": ["Open", "Closed", "Cancelled"]},
    {"title": "Total",         "type": "TEXT_NUMBER"},
    {"title": "Created Date",  "type": "DATE"},
    {"title": "Required Date", "type": "DATE"},
    {"title": "Business Unit", "type": "TEXT_NUMBER"},
    {"title": "ST Link",       "type": "TEXT_NUMBER"},
]

# ---------------------------------------------------------------------------
# Sync intervals (seconds)
# ---------------------------------------------------------------------------
SYNC_INTERVALS = {
    "appointments": 5 * 60,
    "jobs":         15 * 60,
    "invoices":     15 * 60,
    "tasks":        15 * 60,
    "estimates":    15 * 60,
    "pos":          15 * 60,
}

# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------
ENABLED_MODULES = {
    "appointments": True,
    "jobs":         True,
    "invoices":     True,
    "tasks":        True,
    "estimates":    True,
    "pos":          True,
}

# ---------------------------------------------------------------------------
# Removal / retention rules
# ---------------------------------------------------------------------------
APPT_RETENTION_DAYS   = 90   # remove completed/cancelled appointments older than this
TASKS_RETENTION_DAYS  = 30   # remove completed tasks older than this
