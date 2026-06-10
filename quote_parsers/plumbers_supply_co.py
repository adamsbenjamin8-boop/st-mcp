"""
Parser for Plumbers Supply Co. quotes.
Format: columnar invoice with Qty-Ord / Qty-Shp / UM / Product# / Description / List / Unit / Amount
"""
import re
from dataclasses import dataclass, field
from typing import List


@dataclass
class LineItem:
    part_no: str = ""
    description: str = ""
    qty: float = 1.0
    unit_price: float = 0.0
    unit_of_measure: str = "EA"
    total: float = 0.0


@dataclass
class ParsedQuote:
    vendor: str = "Plumbers Supply Co."
    quote_no: str = ""
    cust_po: str = ""
    job_name: str = ""
    total: float = 0.0
    line_items: List[LineItem] = field(default_factory=list)


def can_parse(text: str) -> bool:
    if "plumbers" not in text.lower():
        return False
    return bool(re.search(r"plumbers'?\s*supply|plumberssupplyco?", text, re.IGNORECASE))


def _money(s: str) -> float:
    try:
        return float(s.replace(',', ''))
    except (ValueError, TypeError):
        return 0.0


def parse(text: str) -> ParsedQuote:
    quote = ParsedQuote()

    # Quote number — last token before the word "Quote"
    m = re.search(r'(\S+)\s+Quote\b', text, re.IGNORECASE)
    if m:
        quote.quote_no = m.group(1).strip()

    # Customer PO
    m = re.search(r'Customer\s+PO\s*#?\s*(\S+)', text, re.IGNORECASE)
    if m:
        quote.cust_po = m.group(1).strip()

    # Job name (ship-to first line or explicit label)
    m = re.search(r'(?:job|project|ship\s*to)[:\s]+([^\n]{3,50})', text, re.IGNORECASE)
    if m:
        quote.job_name = m.group(1).strip()

    # Pre-tax total — line that starts with "Total" (not "Invoice Total")
    m = re.search(r'^Total\s+([\d,]+\.\d{2})', text, re.MULTILINE)
    if m:
        quote.total = _money(m.group(1))

    # ----------------------------------------------------------------
    # Line items
    # Each data row: <qty_ord> <qty_shp> <UM_letter> <product#> <description...> <list> <unit> <amount>
    # We want:  qty_ord (col 1), product# (col 4), description (middle), amount (last price).
    # Skip: qty_shp (\d+), UM letter ([A-Z]), list price, unit price.
    # Anchored ^ ... $ so the three trailing prices are consumed correctly.
    # ----------------------------------------------------------------
    line_re = re.compile(
        r'^(\d+)\s+'            # group 1: qty_ord
        r'\d+\s+'               # skip qty_shp
        r'[A-Z]\s+'             # skip UM/BO letter
        r'(\d+)\s+'             # group 2: product#
        r'(.+?)\s+'             # group 3: description (non-greedy, stops before prices)
        r'[\d,]+\.\d{2}\s+'    # skip list price
        r'[\d,]+\.\d{2}\s+'    # skip unit price
        r'([\d,]+\.\d{2})'     # group 4: amount (last price = what we owe)
        r'\s*$',
        re.MULTILINE
    )

    for m in line_re.finditer(text):
        try:
            qty    = float(m.group(1))
            partno = m.group(2).strip()
            desc   = m.group(3).strip()
            amount = _money(m.group(4))
            if qty <= 0 or amount <= 0:
                continue
            quote.line_items.append(LineItem(
                part_no=partno,
                description=desc,
                qty=qty,
                unit_price=amount,
                unit_of_measure="EA",
                total=amount * qty,
            ))
        except (ValueError, IndexError):
            continue

    # Fallback
    if not quote.line_items and quote.total > 0:
        quote.line_items.append(LineItem(
            part_no="SEE-QUOTE",
            description=f"Plumbers Supply Co. Quote {quote.quote_no} - see attached PDF",
            qty=1,
            unit_price=quote.total,
            unit_of_measure="EA",
            total=quote.total,
        ))

    return quote
