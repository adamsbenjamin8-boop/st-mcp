"""
Smartsheet Logger — logs every processed quote to the Quote Parser Log sheet.
"""

import httpx
from datetime import date
from typing import Optional
from config import (
    SMARTSHEET_API_KEY,
    QUOTE_PARSER_LOG_SHEET,
    QLOG_COL_VENDOR, QLOG_COL_DATE, QLOG_COL_FILE,
    QLOG_COL_PARSED_BY, QLOG_COL_PARSER_ADDED,
    QLOG_COL_ITEMS, QLOG_COL_NOTES,
)


def log_quote(
    vendor_name: str,
    filename: str,
    parsed_by: str,          # "Local Parser" or "Claude AI"
    item_count: int,
    parser_added: bool = False,
    notes: str = "",
) -> bool:
    """
    Add a row to the Quote Parser Log Smartsheet.
    Returns True if successful.
    """
    if not SMARTSHEET_API_KEY:
        print("WARNING: SMARTSHEET_API_KEY not configured — skipping Smartsheet log")
        return False

    today = date.today().isoformat()

    cells = [
        {"columnId": QLOG_COL_VENDOR,       "value": vendor_name},
        {"columnId": QLOG_COL_DATE,         "value": today},
        {"columnId": QLOG_COL_FILE,         "value": filename},
        {"columnId": QLOG_COL_PARSED_BY,    "value": parsed_by},
        {"columnId": QLOG_COL_PARSER_ADDED, "value": parser_added},
        {"columnId": QLOG_COL_ITEMS,        "value": str(item_count)},
        {"columnId": QLOG_COL_NOTES,        "value": notes},
    ]

    payload = {"rows": [{"cells": cells, "toBottom": True}]}

    try:
        resp = httpx.post(
            f"https://api.smartsheet.com/2.0/sheets/{QUOTE_PARSER_LOG_SHEET}/rows",
            headers={
                "Authorization": f"Bearer {SMARTSHEET_API_KEY}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"Smartsheet log failed: {e}")
        return False
