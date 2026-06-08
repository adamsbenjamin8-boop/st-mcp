"""
Parser for Tunstall Corporation quotes.
Format: SALES QUOTATION with Item Code / Quantity / Unit Price / Ext. Price columns.
Auto-generated base replaced with hand-written parser.
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
    vendor: str = "Tunstall Corporation"
    quote_no: str = ""
    cust_po: str = ""
    job_name: str = ""
    net_total: float = 0.0
    total: float = 0.0
    line_items: List[LineItem] = field(default_factory=list)


def can_parse(text: str) -> bool:
    return bool(re.search(r'tunstall', text, re.IGNORECASE))


def _parse_money(s: str) -> float:
    try:
        return float(re.sub(r'[^\d.]', '', s))
    except (ValueError, TypeError):
        return 0.0


def parse(text: str) -> ParsedQuote:
    quote = ParsedQuote()

    # Quote number  (e.g. "37,696" or "37696")
    m = re.search(r'Quotation\s+No[:\s,]*([0-9,]+)', text, re.IGNORECASE)
    if m:
        quote.quote_no = m.group(1).replace(',', '').strip()

    # Customer PO — the cell below the "Customer PO" header is often blank;
    # grab it only if it looks like a real PO (not a column header word)
    m = re.search(r'Customer\s+PO\s*\n([^\n]{1,60})', text, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        skip = {'salesperson', 'contact', 'terms', 'shipping', 'freight', ''}
        if val.lower() not in skip:
            quote.cust_po = val

    # Project / job name
    m = re.search(r'Project\s+Name[:\s]*\n([^\n]+)', text, re.IGNORECASE)
    if m:
        quote.job_name = m.group(1).strip()

    # Total
    m = re.search(r'\bTotal[:\s]+\$?([\d,]+\.?\d*)', text, re.IGNORECASE)
    if m:
        quote.total = _parse_money(m.group(1))
        quote.net_total = quote.total

    # ----------------------------------------------------------------
    # Line items
    # The table looks like:
    #   Item Code   Quantity   Delivery Date   Unit Price   Ext. Price
    #   RUNTAL Electric   1   $1,035.00   $1,035.00
    #   EBB-1
    #   (1) - EB3-48-120D
    #   120 Volts/1 Phase/Output 586 Watts
    #   ...
    # ----------------------------------------------------------------
    table_m = re.search(
        r'Item Code\s+Quantity.*?Ext\.?\s*Price\s*\n(.*?)(?:Sub-Total|Remarks\b)',
        text, re.DOTALL | re.IGNORECASE
    )

    if table_m:
        table_text = table_m.group(1)

        # An item line: text, then qty integer, then two $ amounts
        item_re = re.compile(
            r'^(.+?)\s+(\d+(?:\.\d+)?)\s+(?:[\d/]+\s+)?\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})',
            re.MULTILINE
        )

        matches = list(item_re.finditer(table_text))
        for i, m in enumerate(matches):
            item_code   = m.group(1).strip()
            qty         = float(m.group(2))
            unit_price  = _parse_money(m.group(3))
            ext_price   = _parse_money(m.group(4))

            # Collect description lines between this match and the next
            start = m.end()
            end   = matches[i + 1].start() if i + 1 < len(matches) else len(table_text)
            desc_block = table_text[start:end].strip()

            # First non-blank line after the item line is often the part number
            desc_lines = [l.strip() for l in desc_block.splitlines() if l.strip()]
            part_no     = desc_lines[0] if desc_lines else item_code
            description = item_code
            if desc_lines:
                description = item_code + " — " + " / ".join(desc_lines)

            quote.line_items.append(LineItem(
                part_no=part_no,
                description=description,
                qty=qty,
                unit_price=unit_price,
                unit_of_measure="EA",
                total=ext_price,
            ))

    # Fallback: if table regex missed everything but we have a total,
    # create one line item from the total so the PO still gets created.
    if not quote.line_items and quote.total:
        quote.line_items.append(LineItem(
            part_no="SEE-QUOTE",
            description=f"Tunstall Quote {quote.quote_no} — see attached PDF",
            qty=1,
            unit_price=quote.total,
            unit_of_measure="EA",
            total=quote.total,
        ))

    return quote
