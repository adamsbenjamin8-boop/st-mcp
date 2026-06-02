"""
Quote Processor — main orchestration logic.
Processes a single quote file end-to-end:
  1. Detect vendor + parse
  2. Match job in ServiceTitan
  3. Create PO Request with all line items
  4. Send Teams notification
  5. Log to Smartsheet
  6. Move file to Processed folder
"""

import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import PROCESSED_FOLDER
from vendor_router import detect_and_parse
from st_client import (
    find_vendor_id, find_job_id, get_default_po_type_id,
    create_po_request, add_po_item, get_po_url
)
from teams_notifier import send_po_notification
from smartsheet_logger import log_quote


def process_quote_file(file_path: str, workflow: str = "po") -> dict:
    """
    Process a single quote file.
    workflow: "po" (create PO) or "estimate" (add to estimate — future)
    Returns dict with success status and details.
    """
    path = Path(file_path)
    result = {
        "file": path.name,
        "success": False,
        "vendor": None,
        "po_id": None,
        "item_count": 0,
        "error": None,
    }

    print(f"\n{'='*60}")
    print(f"Processing: {path.name}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        # Step 1: Detect vendor and parse
        vendor_name, parsed = detect_and_parse(file_path)

        if vendor_name is None or parsed is None:
            # Unknown vendor — log it and return for Claude escalation
            print(f"  ❌ Unknown vendor format — needs Claude parsing")
            log_quote(
                vendor_name="Unknown",
                filename=path.name,
                parsed_by="Claude AI",
                item_count=0,
                parser_added=False,
                notes="Unknown vendor format — manual review needed",
            )
            result["error"] = "unknown_vendor"
            return result

        print(f"  ✓ Vendor: {vendor_name}")
        items = getattr(parsed, 'line_items', [])
        result["vendor"] = vendor_name
        result["item_count"] = len(items)

        if not items:
            print(f"  ⚠  No line items found")
            result["error"] = "no_items"
            return result

        print(f"  ✓ {len(items)} line items extracted")

        # Step 2: Get job reference from quote
        job_name = getattr(parsed, 'job_name', '') or getattr(parsed, 'cust_po', '') or ''
        po_ref   = getattr(parsed, 'cust_po', '') or getattr(parsed, 'bid_no', '') or getattr(parsed, 'quote_no', '')

        # Step 3: Look up vendor ID in ServiceTitan
        vendor_id = find_vendor_id(vendor_name)
        if not vendor_id:
            print(f"  ⚠  Vendor '{vendor_name}' not found in ServiceTitan — using None")

        # Step 4: Try to match job
        job = None
        if job_name and job_name.lower() not in ('test', ''):
            job = find_job_id(job_name)
            if job:
                print(f"  ✓ Job matched: #{job['jobNumber']} — {job['customerName']}")
            else:
                print(f"  ⚠  Job '{job_name}' not matched — PO will be unassigned")

        # Step 5: Get PO type
        po_type_id = get_default_po_type_id()

        # Build memo for PO
        memo_parts = []
        if po_ref:
            memo_parts.append(f"Quote Ref: {po_ref}")
        if job_name and not job:
            memo_parts.append(f"Job reference on quote: {job_name} — please assign job before approving")
        memo = " | ".join(memo_parts)

        # Step 6: Create PO Request
        if not vendor_id or not po_type_id:
            note = f"Missing: {'vendor_id' if not vendor_id else ''} {'po_type_id' if not po_type_id else ''}"
            print(f"  ❌ Cannot create PO — {note}")
            result["error"] = note
            return result

        po = create_po_request(
            vendor_id=vendor_id,
            job_id=job["id"] if job else None,
            po_type_id=po_type_id,
            memo=memo,
        )

        if not po or "id" not in po:
            print(f"  ❌ PO creation failed")
            result["error"] = "po_creation_failed"
            return result

        po_id = po["id"]
        result["po_id"] = po_id
        print(f"  ✓ PO created: ID {po_id}")

        # Step 7: Add line items
        added = 0
        for item in items:
            # Build ST item dict
            desc      = getattr(item, 'description', '') or str(item)
            qty       = getattr(item, 'qty', 1)
            unit_price = getattr(item, 'unit_price', 0.0)
            part_no   = (getattr(item, 'part_no', '') or
                         getattr(item, 'vendor_part_no', '') or
                         getattr(item, 'vendor_code', '') or
                         getattr(item, 'mfr_code', ''))

            success = add_po_item(po_id, {
                "description":      desc,
                "quantity":         qty,
                "unitCost":         unit_price,
                "vendorPartNumber": part_no,
            })
            if success:
                added += 1

        print(f"  ✓ {added}/{len(items)} items added to PO")

        # Step 8: Send Teams notification
        total = getattr(parsed, 'net_total', 0.0) or getattr(parsed, 'total', 0.0) or getattr(parsed, 'amount_due', 0.0)
        teams_note = memo if (job_name and not job) else ""
        send_po_notification(
            vendor=vendor_name,
            total=total,
            job_name=job_name or "Not specified",
            po_id=po_id,
            item_count=added,
            notes=teams_note,
        )
        print(f"  ✓ Teams notification sent")

        # Step 9: Log to Smartsheet
        log_quote(
            vendor_name=vendor_name,
            filename=path.name,
            parsed_by="Local Parser",
            item_count=added,
            parser_added=True,
        )

        # Step 10: Move to Processed folder
        _move_to_processed(path)

        result["success"] = True
        print(f"  ✅ Done — PO #{po_id}: {get_po_url(po_id)}")

    except Exception as e:
        result["error"] = str(e)
        print(f"  ❌ Unexpected error: {e}")
        traceback.print_exc()

    return result


def _move_to_processed(path: Path):
    """Move a processed file to the Processed folder."""
    try:
        dest_dir = Path(PROCESSED_FOLDER)
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Add timestamp to avoid collisions
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = dest_dir / f"{path.stem}_{ts}{path.suffix}"
        shutil.move(str(path), str(dest))
    except Exception as e:
        print(f"  ⚠  Could not move file to Processed: {e}")
