"""
Ferguson Enterprises Quote Parser
===================================
Handles TWO Ferguson PDF formats:

  1. Branch Price Quotation — emailed from branch reps
     Header: "FERGUSON ENTERPRISES LLC Price Quotation"
     Fields: Bid No, Cust PO#, Job Name, tabular line items

  2. Online Draft Quote — printed from ferguson.com
     Header: "Quote Detail", "Draft Quote #: DQ..."
     Fields: Draft Quote #, Job Name, PO #, inline line items

Identified by: "FERGUSON" in document text

Key fields extracted:
  - vendor:       "Ferguson Enterprises"
  - bid_no:       Ferguson's internal bid/reference number (e.g. B530252)
  - cust_po:      Customer PO# field (may be blank — new PO needed if so)
  - job_name:     Job Name field (e.g. LOCKHEED)
  - bid_date:     Date of the quote
  - line_items:   List of dicts with part_no, description, qty, unit_price, unit_of_measure, total

Unit of Measure notes:
  - EA  = each       (unit_price is per unit)
  - C   = per 100    (unit_price is per 100 units — divide by 100 for each price)
  - PK  = per pack   (unit_price is per pack)
"""

import re
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class QuoteLineItem:
    part_no: str
    description: str
    qty: float
    unit_price: float        # always normalized to per-unit
    unit_of_measure: str     # EA, C, PK, etc.
    raw_price: float         # price as listed on quote (may be per-100 etc.)
    total: float


@dataclass
class FergusonQuote:
    vendor: str = "Ferguson Enterprises"
    bid_no: str = ""
    cust_po: str = ""
    job_name: str = ""
    bid_date: str = ""
    net_total: float = 0.0
    line_items: list = field(default_factory=list)


def can_parse(text: str) -> bool:
    """Return True if this text looks like a Ferguson quote (branch or online)."""
    t = text.upper()
    return "FERGUSON ENTERPRISES" in t or ("FERGUSON" in t and ("PRICE QUOTATION" in t or "QUOTE DETAIL" in t or "DRAFT QUOTE" in t))


def _is_online_quote(text: str) -> bool:
    """Return True if this is an online Draft Quote from ferguson.com"""
    return "DRAFT QUOTE" in text.upper() or "QUOTE DETAIL" in text.upper()


def _parse_online(text: str) -> FergusonQuote:
    """
    Parse the online Draft Quote format from ferguson.com.
    Example: Draft Quote #: DQ01488299
    Line items: {#} {product_code} {description} ${price} per {UNIT} {qty} ${total}
    """
    quote = FergusonQuote()

    dq_match = re.search(r'Draft Quote\s*#[:\s]+([A-Z0-9]+)', text, re.IGNORECASE)
    if dq_match:
        quote.bid_no = dq_match.group(1).strip()

    job_match = re.search(r'Job Name[:\s]*\n?\s*([^\n\r]+)', text, re.IGNORECASE)
    if job_match:
        val = job_match.group(1).strip()
        if val.lower() not in ('job name', ''):
            quote.job_name = val

    po_match = re.search(r'PO\s*#[:\s]*\n?\s*([^\n\r]+)', text, re.IGNORECASE)
    if po_match:
        val = po_match.group(1).strip()
        if val.lower() not in ('po #', 'po#', ''):
            quote.cust_po = val

    subtotal_match = re.search(r'Subtotal[:\s]*\$?([\d,]+\.\d{2})', text)
    if subtotal_match:
        quote.net_total = float(subtotal_match.group(1).replace(',', ''))

    # Line items: {#} {product_code} {desc} ${price} per {UNIT} {qty} ${total}
    line_re = re.compile(
        r'^\d+\s+'                          # line number
        r'([A-Z0-9\-]+)\s+'                 # product code
        r'(.+?)\s+'                         # description
        r'\$([\d.]+)\s+per\s+([A-Z]+)\s+'  # price per unit
        r'(\d+(?:\.\d+)?)\s+'               # qty
        r'\$([\d,]+\.\d+)',                 # total
        re.MULTILINE | re.IGNORECASE
    )

    for m in line_re.finditer(text):
        part_no     = m.group(1).strip()
        description = m.group(2).strip()
        unit_price  = float(m.group(3))
        um          = m.group(4).upper()
        qty         = float(m.group(5))
        total       = float(m.group(6).replace(',', ''))

        quote.line_items.append(QuoteLineItem(
            part_no=part_no,
            description=description,
            qty=qty,
            unit_price=unit_price,
            unit_of_measure=um,
            raw_price=unit_price,
            total=total,
        ))

    return quote


def parse(text: str) -> Optional[FergusonQuote]:
    """
    Parse a Ferguson price quotation from extracted PDF text.
    Returns a FergusonQuote or None if parsing fails.
    """
    quote = FergusonQuote()

    # --- Handle online Draft Quote format ---
    if _is_online_quote(text):
        return _parse_online(text)

    # --- Branch Price Quotation fields ---
    bid_match = re.search(r'Bid No[:\s]+([A-Z0-9]+)', text)
    if bid_match:
        quote.bid_no = bid_match.group(1).strip()

    date_match = re.search(r'Bid Date[:\s]+(\d{2}/\d{2}/\d{2,4})', text)
    if date_match:
        quote.bid_date = date_match.group(1).strip()

    # Cust PO# — may be blank
    po_match = re.search(r'Cust PO#[:\s]*([^\n\r]+)', text)
    if po_match:
        quote.cust_po = po_match.group(1).strip()

    # Job Name
    job_match = re.search(r'Job Name[:\s]*([^\n\r]+)', text)
    if job_match:
        quote.job_name = job_match.group(1).strip()

    # Net Total
    net_match = re.search(r'Net Total[:\s]*\$?([\d,]+\.?\d*)', text)
    if net_match:
        quote.net_total = float(net_match.group(1).replace(',', ''))

    # --- Line items ---
    # Find the table between the column header row and "Net Total:"
    table_match = re.search(
        r'Item\s+Description\s+Quantity\s+Net Price\s+UM\s+Total\s*\n(.*?)Net Total',
        text, re.DOTALL
    )
    if not table_match:
        return quote   # Return partial quote with header info even if table fails

    table_text = table_match.group(1)

    # Each line: PARTNO  DESCRIPTION  QTY  PRICE  UM  TOTAL
    # Part numbers are alphanumeric, no spaces. Description runs until numbers.
    line_pattern = re.compile(
        r'^([A-Z0-9\-]+)\s+'          # Part number
        r'(.+?)\s+'                    # Description (greedy, trimmed)
        r'(\d+(?:\.\d+)?)\s+'          # Quantity
        r'(\d+(?:\.\d+)?)\s+'          # Net Price
        r'([A-Z]+)\s+'                 # Unit of measure
        r'([\d,]+(?:\.\d+)?)',         # Total
        re.MULTILINE
    )

    for m in line_pattern.finditer(table_text):
        part_no     = m.group(1).strip()
        description = m.group(2).strip()
        qty         = float(m.group(3))
        raw_price   = float(m.group(4))
        um          = m.group(5).upper()
        total       = float(m.group(6).replace(',', ''))

        # Normalize price to per-unit
        if um == 'C':
            unit_price = raw_price / 100.0
        else:
            unit_price = raw_price

        quote.line_items.append(QuoteLineItem(
            part_no=part_no,
            description=description,
            qty=qty,
            unit_price=unit_price,
            unit_of_measure=um,
            raw_price=raw_price,
            total=total,
        ))

    return quote


# ---------------------------------------------------------------------------
# Quick test — run directly to verify against a live PDF
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys, pdfplumber, json

    if len(sys.argv) < 2:
        print("Usage: python ferguson.py path/to/quote.pdf")
        sys.exit(1)

    with pdfplumber.open(sys.argv[1]) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    if not can_parse(text):
        print("Not a Ferguson quote.")
        sys.exit(1)

    q = parse(text)
    print(f"Vendor:    {q.vendor}")
    print(f"Bid No:    {q.bid_no}")
    print(f"Cust PO#:  {q.cust_po or '(blank — new PO)'}")
    print(f"Job:       {q.job_name}")
    print(f"Date:      {q.bid_date}")
    print(f"Net Total: ${q.net_total:,.2f}")
    print(f"\nLine Items ({len(q.line_items)}):")
    for i, item in enumerate(q.line_items, 1):
        print(f"  {i:2}. {item.part_no:<20} {item.description:<40} "
              f"qty={item.qty}  unit=${item.unit_price:.4f}  total=${item.total:.2f}")
