"""
Smartsheet Logger — logs quotes, missing parts, and unknown vendors.
All column IDs verified directly from sheets.
"""
import os
import httpx
from datetime import date
from pathlib import Path
from typing import List, Optional


def _load_env():
    env_file = Path("C:/Program Files/ST_MCP/.env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip()  # direct override, not setdefault

_load_env()

# ---------------------------------------------------------------------------
# Sheet IDs
# ---------------------------------------------------------------------------
QUOTE_PARSER_LOG_SHEET  = 4884926238248836
MISSING_PARTS_SHEET_ID  = 7913389283037060
APPROVED_VENDOR_SHEET   = 1832230987976580

# ---------------------------------------------------------------------------
# Quote Parser Log column IDs
# ---------------------------------------------------------------------------
QLOG_COL_VENDOR_NAME    = 7714584764125060
QLOG_COL_DATE           = 2085085229911940
QLOG_COL_FILE_NAME      = 6588684857282436
QLOG_COL_PARSED_BY      = 4336885043597188
QLOG_COL_PARSER_ADDED   = 8840484670967684
QLOG_COL_ITEMS          = 255497881292676
QLOG_COL_NOTES          = 4759097508663172
QLOG_COL_PO_NUMBER      = 6331607794618244   # already existed
QLOG_COL_JOB_NUMBER     = 1671217164881796   # new
QLOG_COL_CUSTOMER_NAME  = 6174816792252292   # new
QLOG_COL_ST_LINK        = 3923016978567044   # new
QLOG_COL_STATUS         = 5443014721048452   # new
QLOG_COL_SHIP_TO        = 569204280692612    # new
QLOG_COL_CUSTOMER_ADDR  = 4208915259887492   # new

# ---------------------------------------------------------------------------
# Missing Parts Queue column IDs (verified from sheet)
# ---------------------------------------------------------------------------
MP_COL_PART_NAME        = 2972461980487556
MP_COL_DESCRIPTION      = 7476061607858052
MP_COL_VENDOR           = 1846562073644932
MP_COL_VENDOR_PART      = 6350161701015428
MP_COL_EST_COST         = 4098361887330180
MP_COL_NOTES            = 8601961514700676
MP_COL_STATUS           = 439187190091652
MP_COL_DATE_ADDED       = 4942786817462148

# ---------------------------------------------------------------------------
# Approved Vendor Emails column IDs (verified from sheet)
# ---------------------------------------------------------------------------
AV_COL_VENDOR_NAME      = 1191779545419652
AV_COL_TYPE             = 2025839024967556
AV_COL_ST_VENDOR_NAME   = 5030097781559172
AV_COL_STATUS           = 628829591998340
AV_COL_EMAIL_DOMAIN     = 5695379172790148
AV_COL_VENDOR_CONTACT   = 6588800351637380
AV_COL_DATE_ADDED       = 2880629405683588
AV_COL_NOTES            = 1754729498840964


def _ss_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('SMARTSHEET_API_KEY', '')}",
        "Content-Type":  "application/json",
    }


def _add_rows(sheet_id: int, rows: list) -> Optional[int]:
    """Returns the created row ID on success, None on failure."""
    if not os.environ.get("SMARTSHEET_API_KEY"):
        print("WARNING: SMARTSHEET_API_KEY not configured")
        return None
    try:
        resp = httpx.post(
            f"https://api.smartsheet.com/2.0/sheets/{sheet_id}/rows",
            headers=_ss_headers(),
            json=rows,  # bare array — Smartsheet API requires [row, row, ...] not {"rows": [...]}
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["result"][0]["id"]
    except Exception as e:
        print(f"Smartsheet error: {e}")
        return None


def _attach_pdf_to_row(row_id: int, pdf_path) -> None:
    """Attach a PDF file to a Smartsheet row. Failure prints a warning and never raises."""
    try:
        api_key = os.environ.get("SMARTSHEET_API_KEY", "")
        if not api_key:
            print("WARNING: SMARTSHEET_API_KEY not configured — skipping PDF attachment")
            return
        pdf_path = Path(pdf_path)
        file_bytes = pdf_path.read_bytes()
        resp = httpx.post(
            f"https://api.smartsheet.com/2.0/sheets/{QUOTE_PARSER_LOG_SHEET}/rows/{row_id}/attachments",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (pdf_path.name, file_bytes, "application/pdf")},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"WARNING: Could not attach PDF to Smartsheet row {row_id}: {e}")


# ---------------------------------------------------------------------------
# Quote Parser Log
# ---------------------------------------------------------------------------
def log_quote(
    vendor_name: str,
    filename: str,
    parsed_by: str,
    item_count: int,
    parser_added: bool = False,
    notes: str = "",
    po_number=None,
    po_id=None,
    job_number=None,
    customer_name=None,
    pdf_path=None,
    status: str = "Processed",
    ship_to_address: str = "",
    customer_address: str = "",
) -> bool:
    cells = [
        {"columnId": QLOG_COL_VENDOR_NAME,   "value": vendor_name},
        {"columnId": QLOG_COL_DATE,           "value": date.today().isoformat()},
        {"columnId": QLOG_COL_FILE_NAME,      "value": filename},
        {"columnId": QLOG_COL_PARSED_BY,      "value": parsed_by},
        {"columnId": QLOG_COL_PARSER_ADDED,   "value": parser_added},
        {"columnId": QLOG_COL_ITEMS,          "value": str(item_count)},
        {"columnId": QLOG_COL_NOTES,          "value": notes},
        {"columnId": QLOG_COL_STATUS,         "value": status},
    ]
    if po_number is not None:
        cells.append({"columnId": QLOG_COL_PO_NUMBER,     "value": po_number})
    if job_number is not None:
        cells.append({"columnId": QLOG_COL_JOB_NUMBER,    "value": job_number})
    if customer_name is not None:
        cells.append({"columnId": QLOG_COL_CUSTOMER_NAME, "value": customer_name})
    if po_id is not None:
        cells.append({
            "columnId":  QLOG_COL_ST_LINK,
            "value":     "View PO",
            "hyperlink": {"url": f"https://go.servicetitan.com/#/new/inventory/purchase-orders/details/{po_id}"},
        })
    if ship_to_address:
        cells.append({"columnId": QLOG_COL_SHIP_TO,       "value": ship_to_address})
    if customer_address:
        cells.append({"columnId": QLOG_COL_CUSTOMER_ADDR, "value": customer_address})
    row_id = _add_rows(QUOTE_PARSER_LOG_SHEET, [{"cells": cells, "toBottom": True}])
    if row_id is not None and pdf_path is not None:
        _attach_pdf_to_row(row_id, pdf_path)
    return row_id is not None


def log_parser_issue(
    vendor_name: str,
    filename: str,
    parsed_by: str,
    issues: list,
    computed_total: float,
    stated_total: float,
    items_extracted: int,
    pdf_text_preview: str,
    po_number=None,
    po_id=None,
    job_number=None,
    customer_name=None,
    pdf_path=None,
    status: str = "Processed",
) -> bool:
    """Write a quality-failure row to the Quote Parser Log sheet."""
    issue_str = "; ".join(issues) if issues else "unknown"
    notes = (
        f"QUALITY ISSUE: {issue_str} | "
        f"Computed: ${computed_total:,.2f} | "
        f"Stated: ${stated_total:,.2f} | "
        f"PDF chars: {len(pdf_text_preview)}"
    )
    cells = [
        {"columnId": QLOG_COL_VENDOR_NAME,   "value": vendor_name},
        {"columnId": QLOG_COL_DATE,           "value": date.today().isoformat()},
        {"columnId": QLOG_COL_FILE_NAME,      "value": filename},
        {"columnId": QLOG_COL_PARSED_BY,      "value": parsed_by},
        {"columnId": QLOG_COL_PARSER_ADDED,   "value": False},
        {"columnId": QLOG_COL_ITEMS,          "value": str(items_extracted)},
        {"columnId": QLOG_COL_NOTES,          "value": notes},
        {"columnId": QLOG_COL_STATUS,         "value": status},
    ]
    if po_number is not None:
        cells.append({"columnId": QLOG_COL_PO_NUMBER,     "value": po_number})
    if job_number is not None:
        cells.append({"columnId": QLOG_COL_JOB_NUMBER,    "value": job_number})
    if customer_name is not None:
        cells.append({"columnId": QLOG_COL_CUSTOMER_NAME, "value": customer_name})
    if po_id is not None:
        cells.append({
            "columnId":  QLOG_COL_ST_LINK,
            "value":     "View PO",
            "hyperlink": {"url": f"https://go.servicetitan.com/#/new/inventory/purchase-orders/details/{po_id}"},
        })
    row_id = _add_rows(QUOTE_PARSER_LOG_SHEET, [{"cells": cells, "toBottom": True}])
    if row_id is not None and pdf_path is not None:
        _attach_pdf_to_row(row_id, pdf_path)
    return row_id is not None


# ---------------------------------------------------------------------------
# Missing Parts Queue
# ---------------------------------------------------------------------------
def log_missing_parts(
    vendor: str,
    po_id: int,
    filename: str,
    items: List[dict],
) -> bool:
    if not items:
        return False
    today = date.today().isoformat()
    rows  = []
    for item in items:
        rows.append({"cells": [
            {"columnId": MP_COL_PART_NAME,   "value": item.get("part_no", "") or item.get("description", "")[:50]},
            {"columnId": MP_COL_DESCRIPTION, "value": item.get("description", "")},
            {"columnId": MP_COL_VENDOR,      "value": vendor},
            {"columnId": MP_COL_VENDOR_PART, "value": item.get("part_no", "")},
            {"columnId": MP_COL_EST_COST,    "value": str(item.get("cost", 0))},
            {"columnId": MP_COL_NOTES,       "value": f"PO ID: {po_id} | File: {filename}"},
            {"columnId": MP_COL_STATUS,      "value": "New"},
            {"columnId": MP_COL_DATE_ADDED,  "value": today},
        ], "toBottom": True})
    return _add_rows(MISSING_PARTS_SHEET_ID, rows) is not None


# ---------------------------------------------------------------------------
# Unknown Vendor / Email Sender — logs to Approved Vendor Emails sheet
# ---------------------------------------------------------------------------
def log_unknown_vendor(
    vendor_name: str,
    vendor_type: str,
    email_domain: str = "",
    vendor_contact_email: str = "",
    notes: str = "",
) -> bool:
    cells = [
        {"columnId": AV_COL_VENDOR_NAME, "value": vendor_name},
        {"columnId": AV_COL_TYPE,        "value": vendor_type},
        {"columnId": AV_COL_STATUS,      "value": "Pending Approval"},
        {"columnId": AV_COL_DATE_ADDED,  "value": date.today().isoformat()},
    ]
    if email_domain:
        cells.append({"columnId": AV_COL_EMAIL_DOMAIN, "value": email_domain})
    if vendor_contact_email:
        cells.append({"columnId": AV_COL_VENDOR_CONTACT, "value": vendor_contact_email})
    if notes:
        cells.append({"columnId": AV_COL_NOTES, "value": notes})
    return _add_rows(APPROVED_VENDOR_SHEET, [{"cells": cells, "toBottom": True}]) is not None
