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
JOBS_SHEET_ID = 8773592040820612
JOBS_COLS = {
    "job_num":              2389438416244612,   # Job # (primary column)
    "job_id":               8200910226542468,   # Job ID (ST numeric id, sync key)
    "status":               5767138136772484,   # Status (picklist: Hold/In Progress/Scheduled)
    "customer_name":        6893038043615108,   # Customer Name
    "customer_address":     1263538509401988,   # Customer Address
    "business_unit_id":     7637960273121156,   # Business Unit ID
    "business_unit":        3515338323087236,   # Business Unit
    "scheduled_date":       8018937950457732,   # Scheduled Date
    "completion_date":      700588555980676,    # Completion Date
    "job_type":             4823210506014596,   # Job Type
    "invoice_num":          5949110412857220,   # Invoice #
    "jobs_total":           3697310599171972,   # Jobs Total
    "customer_id":          6582429110456196,   # Customer ID
    "location_id":          4752841761836932,   # Location ID
    "assigned_technicians": 6160216645390212,   # Assigned Technicians
    "sold_by":              3908416831704964,   # Sold By
    "primary_technician":   5597266691968900,   # Primary Technician
    "first_dispatch":       5738004180324228,   # First Dispatch
    "invoice_date":         7145379063877508,   # Invoice Date
    "created_date":         137638602559364,    # Created Date
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

# Estimates — sheet ID 1600229966040964 (workspace: Service Titan Shuttle connection)
ESTIMATES_SHEET_ID = 1600229966040964
ESTIMATES_COLS = {
    "estimate_id":      3023527740739460,   # primary key
    "estimate_num":     7527127368109956,
    "job_id":           1897627833896836,
    "job_num":          6401227461267332,
    "customer_name":    4149427647582084,
    "status":           8653027274952580,   # PICKLIST: Open/Sold/Dismissed
    "total":            490252950343556,
    "created_date":     4993852577714052,
    "sold_date":        2742052764028804,
    "business_unit":    7245652391399300,
    "business_unit_id": 1616152857186180,
    "technician":       6119752484556676,
    "location_address": 3867952670871428,
    "notes":            8371552298241924,
    "st_link":          1053202903764868,
}

# Purchase Orders — sheet ID 7840808857194372 (workspace: Service Titan Shuttle connection)
POS_SHEET_ID = 7840808857194372
POS_COLS = {
    "po_id":            5796079991033732,   # primary key
    "po_num":           3544280177348484,
    "job_id":           8047879804718980,
    "job_num":          729530410241924,
    "vendor":           5233130037612420,
    "vendor_id":        2981330223927172,
    "status":           7484929851297668,   # PICKLIST: Open/Submitted/PartiallyReceived/Received/Closed/Cancelled
    "total":            1855430317084548,
    "created_date":     6359029944455044,
    "required_date":    4107230130769796,
    "business_unit":    8610829758140292,
    "business_unit_id": 448055433531268,
    "ship_to":          4951655060901764,
    "notes":            2699855247216516,
    "st_link":          7203454874587012,
}

# ---------------------------------------------------------------------------
# Backfill / sync state
# ---------------------------------------------------------------------------
# .sync_state.json is written after the first full historical backfill.
# Delete this file to force a fresh backfill on the next connector start.
SYNC_STATE_FILE = Path(__file__).parent / ".sync_state.json"

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
