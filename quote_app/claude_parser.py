"""
Claude API Fallback Parser
===========================
Called when no local vendor parser matches a quote.

1. Sends extracted PDF text to the Claude API
2. Gets back structured line items as JSON
3. Saves a new vendor parser file to quote_parsers/ so next time it's local
4. Returns the parsed quote object

Uses claude-haiku (fast + cheap) by default.
Cost: ~$0.001 per quote (fraction of a cent).
"""

import json
import os
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
MODEL             = "claude-haiku-4-5"   # Fast and cheap — ideal for structured extraction

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
        print("  ⚠  ANTHROPIC_API_KEY not set — skipping Claude fallback")
        return None

    print(f"  🤖 Unknown vendor — calling Claude API for {filename}…")

    prompt = f"""You are a purchase order data extractor. Extract the following from this vendor quote:

1. Vendor name (company name on the quote)
2. Quote/bid number
3. Customer PO number (if present — may be blank or a job name)
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
        print(f"  ❌ Claude API error: {e}")
        return None

    # Extract JSON from response
    json_match = re.search(r'\{.*\}', content, re.DOTALL)
    if not json_match:
        print(f"  ❌ Could not find JSON in Claude response")
        return None

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        print(f"  ❌ JSON parse error: {e}")
        return None

    # Build ParsedQuote
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

    print(f"  ✓ Claude extracted {len(quote.line_items)} items from {quote.vendor}")

    # Save a new parser for this vendor
    if quote.vendor and quote.vendor != "Unknown":
        _save_parser(quote.vendor, pdf_text)

    return quote


def _save_parser(vendor_name: str, sample_text: str):
    """
    Ask Claude to write a Python parser function for this vendor
    and save it to quote_parsers/.
    """
    if not ANTHROPIC_API_KEY:
        return

    safe_name = re.sub(r'[^a-z0-9]', '_', vendor_name.lower()).strip('_')
    parser_path = PARSERS_DIR / f"{safe_name}.py"

    # Skip if a valid (non-empty) parser already exists
    if parser_path.exists() and parser_path.stat().st_size > 100:
        return

    # Remove empty/invalid file so we can regenerate
    if parser_path.exists():
        parser_path.unlink()
        print(f"  WARNING: Removed empty/invalid parser for {vendor_name}, regenerating...")

    print(f"  📝 Generating parser code for {vendor_name}…")

    prompt = f"""Write a Python parser for vendor quotes from "{vendor_name}".

The parser must:
1. Have a `can_parse(text: str) -> bool` function that returns True if the text is from this vendor
2. Have a `parse(text: str)` function that returns an object with:
   - vendor (str)
   - quote_no (str)
   - cust_po (str)
   - job_name (str)
   - net_total or total (float)
   - line_items (list of objects with: part_no, description, qty, unit_price, unit_of_measure, total)

Use only Python standard library (re, dataclasses). No external imports.

Base the parser on this sample quote text:
{sample_text[:4000]}

Return ONLY the Python code, no explanation."""

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

        # Strip markdown code fences if present
        code = re.sub(r'^```python\s*', '', code, flags=re.MULTILINE)
        code = re.sub(r'^```\s*$', '', code, flags=re.MULTILINE)

        code = code.strip()

        # Validate -- must have can_parse and parse functions and be substantial
        if len(code) < 200 or 'can_parse' not in code or 'def parse' not in code:
            print(f"  WARNING: Generated parser for {vendor_name} looks invalid -- not saving")
            try:
                from smartsheet_logger import log_quote
                log_quote(vendor_name=vendor_name, filename="auto-parser",
                          parsed_by="Claude AI", item_count=0, parser_added=False,
                          notes=f"PARSER GENERATION FAILED -- invalid code. Send quote PDF to IT for manual parser.")
            except Exception:
                pass
            return

        header = f'"""\nAuto-generated parser for {vendor_name}.\nGenerated by Claude API on first encounter.\n"""\n\n'
        parser_path.write_text(header + code + "\n")
        print(f"  OK: Parser saved: quote_parsers/{safe_name}.py")
        print(f"     Review and push with next scripts-v tag to deploy to all computers")

    except Exception as e:
        print(f"  WARNING: Could not generate parser: {e}")
        if parser_path.exists() and parser_path.stat().st_size == 0:
            parser_path.unlink()
        try:
            from smartsheet_logger import log_quote
            log_quote(vendor_name=vendor_name, filename="auto-parser",
                      parsed_by="Claude AI", item_count=0, parser_added=False,
                      notes=f"PARSER GENERATION FAILED -- API error: {e}. Send quote PDF to IT for manual parser.")
        except Exception:
            pass
