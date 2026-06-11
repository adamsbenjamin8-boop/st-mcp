"""
Quote Processor — orchestrates quote → PO workflow.
"""
import re
import json
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional
from config import PROCESSED_FOLDER, QUARANTINE_DIR
from vendor_router import detect_and_parse
from claude_parser import parse_with_claude, parse_with_claude_retry, maybe_regenerate_parser
from parser_quality import check_parse_quality
from parser_failures import record_failure, record_success
from st_client import (
    find_vendor_id, find_job_id, get_default_po_type_id,
    create_po_with_items, add_items_to_existing_po,
    find_existing_po_on_job, get_po_display_number, get_po_url, get_po_total,
    extract_job_reference, extract_job_reference_strict,
    DEFAULT_JOB_ID, DEFAULT_BUSINESS_UNIT_ID,
)
from teams_notifier import send_po_notification, send_teams_alert
from smartsheet_logger import log_quote, log_missing_parts, log_unknown_vendor, log_parser_issue

_LARGE_QUOTE_THRESHOLD = 500_000.0  # Teams warning added when quote total exceeds this

# Matches "ORDER ACKNOWLEDGEMENT" and F.W. Webb's spaced variant "O R D E R  A C K N O W L E D G E M E N T"
_ACK_PATTERN = re.compile(
    r'order\s+a\s*c\s*k\s*n\s*o\s*w\s*l\s*e\s*d\s*g\s*[eo]\s*[md]?\s*e?\s*n?\s*t?',
    re.IGNORECASE
)


def process_quote_file(file_path: str, workflow: str = "po") -> dict:
    path = Path(file_path)
    result = {"file": path.name, "success": False, "vendor": None,
              "po_id": None, "item_count": 0, "error": None}

    print(f"\n{'='*60}")
    print(f"Processing: {path.name}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    meta          = _load_meta(path)
    email_subject = meta.get("subject", "")
    email_body    = meta.get("body", "")
    email_sender  = meta.get("sender", "")

    try:
        # Extract PDF text early — used for pre-flight checks and later quality validation
        pdf_text = _extract_pdf_text(file_path)

        # Reject order acknowledgments — already-placed orders, not new quotes
        if _ACK_PATTERN.search(pdf_text):
            print(f"  Skipping {path.name} — order acknowledgment, not a quote")
            QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(QUARANTINE_DIR / path.name))
            try:
                send_teams_alert(
                    title="📋 Order Acknowledgment — Review Needed",
                    message=(
                        f"File **{path.name}** was received but is an order acknowledgment, "
                        f"not a new quote. It has been saved to Quarantine.\n\n"
                        f"Check the original email and verify this order in ServiceTitan."
                    ),
                    sender=email_sender or "unknown",
                )
            except Exception:
                pass
            return result

        # Step 1: Detect vendor and parse
        vendor_name, parsed = detect_and_parse(file_path)
        if vendor_name is None or parsed is None:
            print(f"  ⚠ Unknown vendor — trying Claude API fallback…")
            try:
                import pdfplumber
                with pdfplumber.open(file_path) as pdf:
                    raw_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            except Exception:
                raw_text = ""
            parsed = parse_with_claude(raw_text, path.name)
            if parsed:
                vendor_name     = parsed.vendor
                parsed_by_label = "Claude AI"
            else:
                log_quote(vendor_name="Unknown", filename=path.name, parsed_by="Claude AI",
                          item_count=0, parser_added=False,
                          notes="Claude API failed — manual review needed",
                          pdf_path=path)
                result["error"] = "unknown_vendor"
                return result
        else:
            parsed_by_label = "Local Parser"

        print(f"  ✓ Vendor: {vendor_name}")
        items = getattr(parsed, 'line_items', [])
        result["vendor"]     = vendor_name
        result["item_count"] = len(items)

        if not items:
            print(f"  ⚠  No line items found")
            result["error"] = "no_items"
            return result

        print(f"  ✓ {len(items)} line items extracted")

        # Quality check — validate parse result before creating PO
        _quote_total = float(
            getattr(parsed, 'net_total', None) or
            getattr(parsed, 'total', None)     or
            getattr(parsed, 'amount_due', None) or 0
        )
        quality = check_parse_quality(items, _quote_total, pdf_text)

        if not quality["passed"]:
            if not quality["is_image_pdf"] and parsed_by_label == "Claude AI":
                print(f"  ⚠ Quality check failed {quality['issues']} — retrying with Claude…")
                retry = parse_with_claude_retry(path, vendor_name, quality)
                if retry is not None:
                    parsed           = retry
                    items            = getattr(parsed, 'line_items', [])
                    result["item_count"] = len(items)
                    parsed_by_label  = "Claude AI (retry)"
                    _quote_total     = float(
                        getattr(parsed, 'net_total', None) or
                        getattr(parsed, 'total', None)     or
                        getattr(parsed, 'amount_due', None) or 0
                    )
                    quality = check_parse_quality(items, _quote_total, pdf_text)

        if quality["passed"]:
            record_success(vendor_name)
        else:
            record_failure(vendor_name, "; ".join(quality["issues"]))
            log_parser_issue(
                vendor_name=vendor_name or "Unknown",
                filename=path.name,
                parsed_by=parsed_by_label,
                issues=quality["issues"],
                computed_total=quality.get("computed_total", 0.0),
                stated_total=_quote_total,
                items_extracted=len(items),
                pdf_text_preview=pdf_text,
                pdf_path=path,
            )
            maybe_regenerate_parser(vendor_name or "", path)

        # Step 2: Vendor lookup (3-tier)
        vendor_id, vendor_st_name, vendor_exact = find_vendor_id(vendor_name)
        if vendor_exact:
            print(f"  ✓ Vendor matched: {vendor_st_name}")
        elif vendor_id != 474:
            print(f"  ⚠  Vendor partially matched as '{vendor_st_name}' — verify before approving")
            log_unknown_vendor(vendor_name=vendor_name, vendor_type="Unknown Vendor Name",
                               email_domain="", vendor_contact_email=email_sender)
        else:
            print(f"  ⚠  Vendor not matched — using Default Replenishment Vendor")
            log_unknown_vendor(vendor_name=vendor_name, vendor_type="Unknown Vendor Name",
                               email_domain="", vendor_contact_email=email_sender)

        # Step 3: Extract job reference — keyword-only from email subject/body
        job_ref = (
            extract_job_reference(email_subject) or
            extract_job_reference(email_body) or
            ''
        )
        po_ref = (getattr(parsed, 'cust_po', '') or
                  getattr(parsed, 'bid_no', '')  or
                  getattr(parsed, 'quote_no', '') or '')

        # Step 4: Match job — only on numeric job numbers
        job               = None
        using_default_job = False
        if job_ref and job_ref.lower() not in ('test', '') and job_ref.strip().isdigit():
            job = find_job_id(job_ref)
            if job:
                print(f"  ✓ Job matched: #{job['jobNumber']} — {job['customerName']}")
            else:
                print(f"  ⚠  Job '{job_ref}' not found — using default job")

        if not job:
            using_default_job = True
            job = {"id": DEFAULT_JOB_ID, "jobNumber": str(DEFAULT_JOB_ID),
                   "customerName": "Default", "businessUnitId": DEFAULT_BUSINESS_UNIT_ID}
            print(f"  → Default job #{DEFAULT_JOB_ID}")

        job_id    = job["id"]
        job_bu_id = job.get("businessUnitId") if not using_default_job else DEFAULT_BUSINESS_UNIT_ID
        po_type   = get_default_po_type_id()

        # Build memo
        memo_parts = []
        if po_ref:
            memo_parts.append(f"Quote Ref: {po_ref}")
        if using_default_job:
            if job_ref:
                memo_parts.append(f"Job ref on quote: {job_ref} — assign before approving")
            else:
                memo_parts.append("DEFAULT JOB — no job reference found — assign before approving")
        if email_sender:
            memo_parts.append(f"From: {email_sender}")
        memo = " | ".join(memo_parts)

        # Step 5: Build line items
        line_items = []
        for item in items:
            line_items.append({
                "description": getattr(item, 'description', '') or str(item),
                "qty":         getattr(item, 'qty', 1),
                "unit_price":  getattr(item, 'unit_price', 0.0),
                "part_no":     (getattr(item, 'part_no', '')       or
                                getattr(item, 'vendor_part_no', '') or
                                getattr(item, 'vendor_code', '')    or
                                getattr(item, 'mfr_code', '')       or ''),
            })

        # Step 6: Create new PO (always — add-to-existing returns 404 for PendingApproval POs)
        existing_po = None
        po_id       = None
        po_number   = ""
        unmatched   = []

        if existing_po:
            print(f"  ✓ Found existing PO #{existing_po['number']} — adding items")
            added, unmatched = add_items_to_existing_po(existing_po["id"], line_items)
            if added > 0:
                po_id     = existing_po["id"]
                po_number = existing_po["number"]
                print(f"  ✓ Added {added} items to PO #{po_number}")
            else:
                print(f"  ⚠ Could not add to existing PO — creating new PO")
                existing_po = None

        if not existing_po:
            po_id, unmatched = create_po_with_items(
                vendor_id=vendor_id, job_id=job_id, po_type_id=po_type,
                line_items=line_items, memo=memo, business_unit_id=job_bu_id,
            )
            if not po_id:
                print(f"  ❌ PO creation failed")
                result["error"] = "po_creation_failed"
                return result
            po_number = get_po_display_number(po_id)
            print(f"  ✓ PO created: #{po_number} with {len(line_items)} items")

        result["po_id"] = po_id

        # Step 7: Log unmatched items
        if unmatched:
            print(f"  ⚠  {len(unmatched)} items used HMIL fallback")
            try:
                log_missing_parts(vendor=vendor_st_name, po_id=po_id,
                                  filename=path.name, items=unmatched)
                print(f"  ✓ Missing parts logged")
            except Exception as e:
                print(f"  ⚠  Could not log missing parts: {e}")

        # Step 8: Teams notification
        total = get_po_total(po_id) or _quote_total
        _large_quote = f"⚠ Large quote: ${total:,.2f} — verify before approving" if total > _LARGE_QUOTE_THRESHOLD else ""
        teams_notes = " | ".join(filter(None, [memo if using_default_job else "", _large_quote]))
        sent = send_po_notification(
            vendor=vendor_name,
            vendor_matched=vendor_exact,
            vendor_st_name=vendor_st_name,
            total=total,
            job_name=f"#{job['jobNumber']}" if not using_default_job else "Default (unassigned)",
            po_id=po_id,
            po_number=po_number,
            item_count=len(line_items),
            notes=teams_notes,
            unmatched_count=len(unmatched),
        )
        print(f"  {'✓' if sent else '❌'} Teams notification {'sent' if sent else 'failed'}")

        # Step 9: Quote Parser Log
        log_quote(
            vendor_name=vendor_st_name,
            filename=path.name,
            parsed_by=parsed_by_label,
            item_count=len(line_items),
            parser_added=(parsed_by_label == "Local Parser"),
            po_number=po_number,
            po_id=po_id,
            job_number=job.get("jobNumber"),
            customer_name=job.get("customerName"),
            pdf_path=path,
        )

        # Step 10: Move to Processed
        _move_to_processed(path, vendor_st_name, po_ref or path.stem, po_number)

        # Clean up meta sidecar
        meta_path = path.with_suffix('.meta.json')
        if meta_path.exists():
            try:
                meta_path.unlink()
            except Exception:
                pass

        result["success"] = True
        print(f"  ✓ Done — PO #{po_number}: {get_po_url(po_id)}")

    except Exception as e:
        result["error"] = str(e)
        print(f"  ❌ Unexpected error: {e}")
        traceback.print_exc()

    return result


def _load_meta(path: Path) -> dict:
    meta_path = path.with_suffix('.meta.json')
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def _extract_pdf_text(file_path: str) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        return ""


def _move_to_processed(path: Path, vendor_name: str, quote_ref: str, po_number: str):
    """Naming: VendorName_QuoteRef_Date_PONumber.pdf"""
    try:
        dest_dir = Path(PROCESSED_FOLDER)
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe = lambda s, n: re.sub(r'[^\w-]', '_', s.strip())[:n]
        name = (f"{safe(vendor_name, 25)}_{safe(quote_ref, 20)}_"
                f"{datetime.now().strftime('%Y%m%d')}_{safe(po_number, 20)}{path.suffix}")
        dest = dest_dir / name
        if dest.exists():
            dest = dest_dir / name.replace(
                path.suffix, f"_{datetime.now().strftime('%H%M%S')}{path.suffix}")
        shutil.move(str(path), str(dest))
    except Exception as e:
        print(f"  ⚠  Could not move file to Processed: {e}")