"""
Johnstone Supply Quote Parser
===================================
Parses Johnstone Supply (The Balsan Group) Quotation PDFs.

Identified by: "JOHNSTONE SUPPLY" or "JOHNSTONE" + "Quotation" in document text

Key fields extracted:
  - vendor:        "Johnstone Supply"
  - quote_no:      Johnstone quote number (e.g. 611-102536577)
  - cust_po:       Customer PO Number field (may contain job reference or actual PO#)
  - job_name:      Job Name / Release Number field (may be blank)
  - expiration:    Quote expiration date
  - line_items:    List of dicts with vendor_code, mfr_code, catalog_pn, description,
                   qty, unit_price, ext_price

Format notes:
  - Each line item spans 3-4 lines in PDF:
      Line 1: {qty}ea  {vendor_code}  {mfr_code}  {start of description}  {unit_price}/ea  {ext_price}
      Line 2+: continuation of description
      Last:  Pn: {catalog_part_number}
  - Unit prices are shown as "225.870/ea" format
  - "Customer PO Number" field sometimes contains a job description, not a real PO#
"""

import re
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class QuoteLineItem:
    vendor_code: str       # Johnstone's stock code (e.g. B62-749)
    mfr_code: str          # Manufacturer part number (e.g. 01220500C)
    catalog_pn: str        # Pn: catalog number (e.g. 226827)
    description: str       # Full cleaned description
    qty: float
    unit_price: float
    ext_price: float


@dataclass
class JohnstoneQuote:
    vendor: str = "Johnstone Supply"
    quote_no: str = ""
    cust_po: str = ""       # May be job reference or real PO#
    job_name: str = ""
    expiration: str = ""
    subtotal: float = 0.0
    amount_due: float = 0.0
    line_items: list = field(default_factory=list)


def can_parse(text: str) -> bool:
    """Return True if this looks like a Johnstone Supply quote."""
    t = text.upper()
    return "JOHNSTONE SUPPLY" in t or ("JOHNSTONE" in t and "QUOTATION" in t)


def parse(text: str) -> Optional[JohnstoneQuote]:
    """
    Parse a Johnstone Supply quotation from extracted PDF text.
    Returns a JohnstoneQuote or None if parsing fails.
    """
    quote = JohnstoneQuote()

    # Quote number (e.g. 611-102536577)
    qn_match = re.search(r'QUOTE NUMBER\s*\n?\s*([A-Z0-9\-]+)', text)
    if qn_match:
        quote.quote_no = qn_match.group(1).strip()

    # Expiration date
    exp_match = re.search(r'EXPIRATION DATE\s*\n?\s*(\d{2}/\d{2}/\d{4})', text)
    if exp_match:
        quote.expiration = exp_match.group(1).strip()

    # Customer PO Number — may be a real PO or a job description
    po_match = re.search(r'CUSTOMER PO NUMBER\s*\n?\s*([^\n\r]+)', text, re.IGNORECASE)
    if po_match:
        val = po_match.group(1).strip()
        # Filter out header repeats
        if val.upper() not in ('CUSTOMER PO NUMBER', 'JOB NAME / RELEASE NUMBER', 'SALESPERSON'):
            quote.cust_po = val

    # Job Name / Release Number
    job_match = re.search(r'JOB NAME\s*/\s*RELEASE NUMBER\s*\n?\s*([^\n\r]+)', text, re.IGNORECASE)
    if job_match:
        val = job_match.group(1).strip()
        if val.upper() not in ('JOB NAME / RELEASE NUMBER', 'SALESPERSON', ''):
            quote.job_name = val

    # Subtotal and Amount Due
    sub_match = re.search(r'Subtotal\s+([\d,]+\.\d{2})', text)
    if sub_match:
        quote.subtotal = float(sub_match.group(1).replace(',', ''))

    amt_match = re.search(r'Amount Due\s+([\d,]+\.\d{2})', text)
    if amt_match:
        quote.amount_due = float(amt_match.group(1).replace(',', ''))

    # --- Line items ---
    # Pattern: {qty}ea  {vendor_code}  {mfr_code} ... {price}/ea  {ext}
    # The Pn: number appears on a following line
    # Strategy: find all "Nea ..." lines then collect subsequent text until next item or Pn:

    # Split into blocks — each item starts with a number followed by "ea"
    lines = text.split('\n')
    i = 0
    current_item = None

    # Regex for the opening line of an item
    item_start = re.compile(
        r'^(\d+)\s*ea\s+'                  # qty + "ea"
        r'([A-Z]\d+\-\d+[A-Z]?)\s+'        # vendor code (e.g. B62-749)
        r'(\S+)\s+'                         # mfr code
        r'(.+?)\s+'                         # start of description
        r'([\d.]+)/ea\s+'                   # unit price
        r'([\d,]+\.\d{2})',                 # ext price
        re.IGNORECASE
    )

    pn_line = re.compile(r'^Pn:\s*(\d+)', re.IGNORECASE)

    items = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = item_start.match(line)
        if m:
            if current_item:
                items.append(current_item)
            current_item = {
                'qty':         float(m.group(1)),
                'vendor_code': m.group(2).strip(),
                'mfr_code':    m.group(3).strip(),
                'description': m.group(4).strip(),
                'unit_price':  float(m.group(5)),
                'ext_price':   float(m.group(6).replace(',', '')),
                'catalog_pn':  '',
            }
        elif current_item:
            pn_m = pn_line.match(line)
            if pn_m:
                current_item['catalog_pn'] = pn_m.group(1).strip()
            elif line and not any(kw in line.upper() for kw in [
                'SUBTOTAL', 'AMOUNT DUE', 'FREIGHT', 'TAX', 'QUOTATION VALID',
                'BID VALID', 'REVIEWED', 'PRINTED', 'S&H'
            ]):
                # Continuation of description
                current_item['description'] += ' ' + line
        i += 1

    if current_item:
        items.append(current_item)

    for it in items:
        quote.line_items.append(QuoteLineItem(
            vendor_code=it['vendor_code'],
            mfr_code=it['mfr_code'],
            catalog_pn=it['catalog_pn'],
            description=it['description'].strip(),
            qty=it['qty'],
            unit_price=it['unit_price'],
            ext_price=it['ext_price'],
        ))

    return quote


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys, pdfplumber

    if len(sys.argv) < 2:
        print("Usage: python johnstone.py path/to/quote.pdf")
        sys.exit(1)

    with pdfplumber.open(sys.argv[1]) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    if not can_parse(text):
        print("Not a Johnstone Supply quote.")
        sys.exit(1)

    q = parse(text)
    print(f"Vendor:      {q.vendor}")
    print(f"Quote No:    {q.quote_no}")
    print(f"Cust PO:     {q.cust_po or '(blank)'}")
    print(f"Job:         {q.job_name or '(blank)'}")
    print(f"Expiration:  {q.expiration}")
    print(f"Amount Due:  ${q.amount_due:,.2f}")
    print(f"\nLine Items ({len(q.line_items)}):")
    for i, item in enumerate(q.line_items, 1):
        print(f"  {i}. [{item.vendor_code}] [{item.mfr_code}] (Pn:{item.catalog_pn})")
        print(f"     {item.description}")
        print(f"     qty={item.qty}  unit=${item.unit_price:.3f}  total=${item.ext_price:.2f}")
