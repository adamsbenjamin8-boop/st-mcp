"""
Claude API Fallback Parser
==========================
Called when no local vendor parser matches a quote.
1. Sends extracted PDF text to the Claude API
2. Gets back structured line items as JSON
3. Saves a new vendor parser file to quote_parsers/ so next time it's local
4. Self-tests the new parser before accepting it
5. Returns the parsed quote object
Uses claude-haiku (fast + cheap) by default.
Cost: ~$0.001 per quote (fraction of a cent).
"""
import importlib
import importlib.util
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import httpx

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
MODEL             = "claude-haiku-4-5"

PARSERS_DIR = Path(__file__).parent.parent / "quote_parsers"


@dataclass
class ParsedQuote:
    vendor: str = "Unknown"
    quote_no: str = ""
    cust_po: str = ""
    job_name: str = ""
    total: float = 0.0
    line_items: list = field(default_factory=list)


@dataclass
class LineItem:
    description: str
    qty: float
    unit_price: float
    ext_price: float
    part_no: str = ""
    uom: str = "EA"


def parse_with_claude(pdf_text: str, filename: str) -> Optional[ParsedQuote]:
    """
    Send quote text to Claude API, get back structured data.
    Also saves a new parser file for this vendor.
    Returns a ParsedQuote or None on failure.
    """
    if not ANTHROPIC_API_KEY:
        print("  WARNING: ANTHROPIC_API_KEY not set - skipping Claude fallback")
        return None

    print(f"  Unknown vendor - calling Claude API for {filename}...")

    prompt = f"""You are a purchase order data extractor. Extract the following from this vendor quote:
1. Vendor name (company name on the quote)
2. Quote/bid number
3. Customer PO number (if present - may be blank or a job name)
4. Job name (if present)
5. All line items with: part number, description, quantity, unit price, extended price, unit of measure

Return ONLY a JSON object with this exact structure:
{{
  "vendor": "string",
  "quote_no": "string",
  "cust_po": "string",
  "job_name": "string",
  "total": 0.00,
  "line_items": [
    {{
      "part_no": "string",
      "description": "string",
      "qty": 0,
      "unit_price": 0.00,
      "uom": "EA",
      "ext_price": 0.00
    }}
  ]
}}

QUOTE TEXT:
{pdf_text[:8000]}"""

    try:
        resp = httpx.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      MODEL,
                "max_tokens": 2048,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"]
    except Exception as e:
        print(f"  Claude API error: {e}")
        return None

    json_match = re.search(r'\{.*\}', content, re.DOTALL)
    if not json_match:
        print("  Could not find JSON in Claude response")
        return None

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}")
        return None

    quote = ParsedQuote(
        vendor=data.get("vendor", "Unknown"),
        quote_no=data.get("quote_no", ""),
        cust_po=data.get("cust_po", ""),
        job_name=data.get("job_name", ""),
        total=float(data.get("total", 0) or 0),
    )
    for item in data.get("line_items", []):
        try:
            quote.line_items.append(LineItem(
                part_no=str(item.get("part_no", "")),
                description=str(item.get("description", "")),
                qty=float(item.get("qty", 1) or 1),
                unit_price=float(item.get("unit_price", 0) or 0),
                uom=str(item.get("uom", "EA")),
                ext_price=float(item.get("ext_price", 0) or 0),
            ))
        except (ValueError, TypeError):
            continue

    print(f"  Claude extracted {len(quote.line_items)} items from {quote.vendor}")

    if quote.vendor and quote.vendor != "Unknown":
        _save_parser(quote.vendor, pdf_text)

    return quote


def _save_parser(vendor_name: str, sample_text: str):
    """
    Ask Claude to write a Python parser for this vendor, self-test it
    against the sample text, then save it to quote_parsers/.
    """
    if not ANTHROPIC_API_KEY:
        return

    safe_name   = re.sub(r'[^a-z0-9]', '_', vendor_name.lower()).strip('_')
    parser_path = PARSERS_DIR / f"{safe_name}.py"

    if parser_path.exists() and parser_path.stat().st_size > 100:
        return

    if parser_path.exists():
        parser_path.unlink()
        print(f"  WARNING: Removed empty/invalid parser for {vendor_name}, regenerating...")

    print(f"  Generating parser code for {vendor_name}...")

    prompt = f"""Write a Python parser for vendor quotes from "{vendor_name}".

The parser MUST follow these exact requirements:

1. `can_parse(text: str) -> bool`
   - The FIRST statement MUST be a hard vendor identity guard:
       if "<distinctive_vendor_keyword>" not in text.lower(): return False
     Replace <distinctive_vendor_keyword> with the most distinctive word from
     the vendor's name (e.g. "buckley", "trane", "ferguson"). This prevents
     the parser from ever matching a PDF that doesn't mention the vendor.
   - After that guard you may add 1-2 additional checks for vendor-specific
     patterns such as their email domain or website (e.g. "@buckleyonline.com").
   - DO NOT use generic patterns: no zip codes, no street suffixes (Road, Drive),
     no city/state pairs, no phone number formats — these match too many documents.

2. `parse(text: str)` returning an object with:
   - vendor (str), quote_no (str), cust_po (str), job_name (str)
   - net_total or total (float)
   - line_items: list of objects with part_no, description, qty,
     unit_price, unit_of_measure, total
   - REQUIRED FALLBACK: if line_items is empty after parsing but total > 0,
     append one item: part_no="SEE-QUOTE",
     description=f"{vendor_name} Quote {{quote_no}} - see attached PDF",
     qty=1, unit_price=total, unit_of_measure="EA", total=total

Use only Python standard library (re, dataclasses). No external imports.

Base the parser on this sample quote text:
{sample_text[:4000]}

Return ONLY valid Python code, no markdown fences, no explanation."""

    try:
        resp = httpx.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      MODEL,
                "max_tokens": 3000,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=45,
        )
        resp.raise_for_status()
        code = resp.json()["content"][0]["text"]

        code = re.sub(r'^```python\s*', '', code, flags=re.MULTILINE)
        code = re.sub(r'^```\s*$',      '', code, flags=re.MULTILINE)
        code = code.strip()

        if len(code) < 200 or 'can_parse' not in code or 'def parse' not in code:
            print(f"  WARNING: Generated parser for {vendor_name} looks invalid - not saving")
            _log_parser_failure(vendor_name, "invalid code generated")
            return

        header = f'"""\nAuto-generated parser for {vendor_name}.\nGenerated by Claude API on first encounter.\n"""\n\n'
        parser_path.write_text(header + code + "\n")

        # Self-test before accepting
        passed = _self_test_parser(parser_path, safe_name, vendor_name, sample_text)
        if not passed:
            print(f"  WARNING: Parser for {vendor_name} failed self-test - file kept for manual review")
            _log_parser_failure(vendor_name, "self-test failed: can_parse() returned False or 0 line items extracted")
            return

        print(f"  OK: Parser saved and self-tested: quote_parsers/{safe_name}.py")
        print(f"      Review and push with next scripts-v tag to deploy")

    except Exception as e:
        print(f"  WARNING: Could not generate parser: {e}")
        if parser_path.exists() and parser_path.stat().st_size == 0:
            parser_path.unlink()
        _log_parser_failure(vendor_name, f"API error: {e}")


def _self_test_parser(parser_path: Path, module_stem: str,
                      vendor_name: str, sample_text: str) -> bool:
    """
    Dynamically load the newly written parser and run two checks:
      1. can_parse(sample_text) must return True
      2. parse(sample_text) must return >= 1 line item
    Returns True if both pass.
    """
    try:
        full_module = f"quote_parsers.{module_stem}"
        if full_module in sys.modules:
            del sys.modules[full_module]

        spec = importlib.util.spec_from_file_location(full_module, parser_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        if not mod.can_parse(sample_text):
            print(f"  Self-test FAILED: {vendor_name} can_parse() returned False on sample text")
            return False

        result = mod.parse(sample_text)
        items  = getattr(result, 'line_items', [])
        if not items:
            print(f"  Self-test FAILED: {vendor_name} parse() returned 0 line items")
            return False

        print(f"  Self-test PASSED: can_parse=True, {len(items)} item(s) extracted")
        return True

    except Exception as e:
        print(f"  Self-test ERROR for {vendor_name}: {e}")
        return False


def parse_with_claude_retry(
    pdf_path: Path, vendor_name: str, quality_result: dict
) -> "Optional[ParsedQuote]":
    """
    Re-attempt parsing with a targeted prompt based on the quality failure type.
    Returns a ParsedQuote only if the retry result passes quality checks; None otherwise.
    Image PDFs are not retried.
    """
    if not ANTHROPIC_API_KEY:
        return None

    if quality_result.get("is_image_pdf"):
        return None

    try:
        import pdfplumber
        with pdfplumber.open(str(pdf_path)) as pdf:
            pdf_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        return None

    issues = quality_result.get("issues", [])
    if "reconciliation_gap" in issues:
        extra_guidance = (
            "\n\nIMPORTANT: A previous attempt had a significant total mismatch. "
            "Re-read carefully — you may have missed line items or confused a subtotal with the grand total. "
            "Include EVERY line item on the quote."
        )
    elif "missing_unit_prices" in issues:
        extra_guidance = (
            "\n\nIMPORTANT: A previous attempt was missing unit prices on some items. "
            "Re-read carefully — every item must have a unit_price > 0. "
            "If only an extended price is shown, divide by quantity."
        )
    else:
        extra_guidance = (
            "\n\nIMPORTANT: Re-read carefully and ensure description, qty, and unit_price "
            "are populated for every line item."
        )

    prompt = f"""You are a purchase order data extractor. Extract the following from this vendor quote:
1. Vendor name (company name on the quote)
2. Quote/bid number
3. Customer PO number (if present - may be blank or a job name)
4. Job name (if present)
5. All line items with: part number, description, quantity, unit price, extended price, unit of measure

Return ONLY a JSON object with this exact structure:
{{
  "vendor": "string",
  "quote_no": "string",
  "cust_po": "string",
  "job_name": "string",
  "total": 0.00,
  "line_items": [
    {{
      "part_no": "string",
      "description": "string",
      "qty": 0,
      "unit_price": 0.00,
      "uom": "EA",
      "ext_price": 0.00
    }}
  ]
}}{extra_guidance}

QUOTE TEXT:
{pdf_text[:8000]}"""

    try:
        resp = httpx.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      MODEL,
                "max_tokens": 2048,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"]
    except Exception as e:
        print(f"  Claude retry API error: {e}")
        return None

    json_match = re.search(r'\{.*\}', content, re.DOTALL)
    if not json_match:
        return None

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        return None

    quote = ParsedQuote(
        vendor=data.get("vendor", vendor_name),
        quote_no=data.get("quote_no", ""),
        cust_po=data.get("cust_po", ""),
        job_name=data.get("job_name", ""),
        total=float(data.get("total", 0) or 0),
    )
    for item in data.get("line_items", []):
        try:
            quote.line_items.append(LineItem(
                part_no=str(item.get("part_no", "")),
                description=str(item.get("description", "")),
                qty=float(item.get("qty", 1) or 1),
                unit_price=float(item.get("unit_price", 0) or 0),
                uom=str(item.get("uom", "EA")),
                ext_price=float(item.get("ext_price", 0) or 0),
            ))
        except (ValueError, TypeError):
            continue

    print(f"  Claude retry extracted {len(quote.line_items)} items")

    from parser_quality import check_parse_quality
    retry_quality = check_parse_quality(quote.line_items, quote.total, pdf_text)
    if retry_quality["passed"]:
        return quote
    print(f"  Claude retry still failed quality: {retry_quality['issues']}")
    return None


def maybe_regenerate_parser(vendor_name: str, pdf_path: Path) -> bool:
    """
    Delete and regenerate the saved parser for vendor_name if the consecutive failure
    threshold has been reached.  Resets the failure count after the attempt.
    Returns True if regeneration was attempted, False otherwise.
    """
    if not vendor_name:
        return False

    from parser_failures import should_regenerate_parser, record_success

    if not should_regenerate_parser(vendor_name):
        return False

    safe_name   = re.sub(r'[^a-z0-9]', '_', vendor_name.lower()).strip('_')
    parser_path = PARSERS_DIR / f"{safe_name}.py"

    if parser_path.exists():
        try:
            parser_path.unlink()
            print(f"  Auto-regen: deleted broken parser for {vendor_name}")
        except Exception as e:
            print(f"  Auto-regen: could not delete parser for {vendor_name}: {e}")
            return False
    else:
        print(f"  Auto-regen: no parser on disk for {vendor_name} — generating from scratch")

    try:
        import pdfplumber
        with pdfplumber.open(str(pdf_path)) as pdf:
            pdf_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        return False

    _save_parser(vendor_name, pdf_text)
    record_success(vendor_name)
    print(f"  Auto-regen: generation attempted for {vendor_name} — failure count reset")
    return True


def _log_parser_failure(vendor_name: str, reason: str):
    """Log a parser generation failure to Smartsheet for manual follow-up."""
    try:
        from smartsheet_logger import log_quote
        log_quote(
            vendor_name=vendor_name,
            filename="auto-parser",
            parsed_by="Claude AI",
            item_count=0,
            parser_added=False,
            notes=f"PARSER GENERATION FAILED - {reason}. Send quote PDF to IT for manual parser."
        )
    except Exception:
        pass
