"""
F.W. Webb Company Quote Parser
===================================
Handles THREE F.W. Webb formats:

  1. Quote PDF — branch-generated, sent by email
     Header: "F.W. Webb Company Quote"
     Fields: Quote Number, Customer PO#, tabular items

  2. Order Acknowledgement PDF
     Header: "ORDER ACKNOWLEDGEMENT"
     Fields: Purchase Ord#, tabular items (unit price only, no ext column)

  3. Cart PDF — printed from fwwebb.com shopping cart
     Header: "Cart #: XXXX-XXXXXXXXXX"
     Fields: Jobname, card-style items with Product # and Product Code
     NOTE: When both a Cart PDF and CSV exist for the same order, prefer the CSV —
     the CSV is more reliably structured for machine parsing.

Identified by: "F.W. Webb" in document text or "Cart #" + company header

Key fields extracted:
  - vendor:       "F.W. Webb Company"
  - quote_no:     Webb quote number (e.g. 96225586)
  - quote_date:   Date of quote
  - cust_po:      Customer PO# field (often contains job/customer name)
  - job_name:     Derived from Customer PO# or Ship-To address (e.g. CIRTEC)
  - line_items:   List with mfr_code, webb_code, description, qty, unit_price, ext_price

Format notes:
  - Line items delimited by "*---*" separator rows
  - Each item:
      Line 1: {qty}  *  {description}  {net_price}  {extension}  ( {ln#} )
      Line 2: {mfr_part_code}  ({webb_internal_code})
  - "BREAK-ST:" line = subtotal for that price break tier (ignore for totals)
  - Customer PO# field typically holds job/customer name, not a real PO#
  - Actual PO# to use when creating ST PO = Webb quote number
"""

import re
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class QuoteLineItem:
    mfr_code: str         # Manufacturer part number (e.g. GEO163375004)
    webb_code: str        # Webb internal code in parentheses (e.g. 268616)
    description: str      # Item description
    qty: float
    unit_price: float
    ext_price: float
    line_no: str          # Webb line number


@dataclass
class WebbQuote:
    vendor: str = "F.W. Webb Company"
    quote_no: str = ""
    quote_date: str = ""
    cust_po: str = ""     # Usually job/customer name
    job_name: str = ""    # Cleaned job name (from PO field or ship-to)
    total: float = 0.0
    line_items: list = field(default_factory=list)


def can_parse(text: str) -> bool:
    """Return True if this looks like an F.W. Webb document."""
    t = text.upper()
    return "F.W. WEBB" in t or "FW WEBB" in t or ("CART #" in t and "DENOMMEE" in t)


def _is_order_ack(text: str) -> bool:
    return "ORDER ACKNOWLEDGEMENT" in text.upper() or "ORDER ACK" in text.upper()


def _is_cart(text: str) -> bool:
    return "CART #:" in text.upper()


def _parse_cart(text: str) -> WebbQuote:
    """
    Parse F.W. Webb cart printout format.
    Cart # and date at top, card-style items with Product # and Product Code.
    NOTE: Prefer CSV over this format when both are available.
    """
    quote = WebbQuote()

    cart_match = re.search(r'Cart\s*#[:\s]+([A-Z0-9\-]+)', text, re.IGNORECASE)
    if cart_match:
        # Normalize: strip dashes for consistency with CSV filename
        quote.quote_no = cart_match.group(1).replace('-', '').strip()

    date_match = re.search(r'Cart\s*#.*?\n.*?(\d{2}/\d{2}/\d{4})', text, re.DOTALL | re.IGNORECASE)
    if date_match:
        quote.quote_date = date_match.group(1).strip()

    job_match = re.search(r'Jobname[:\s]*([^\n\r]+)', text, re.IGNORECASE)
    if job_match:
        val = job_match.group(1).strip()
        if val:
            quote.cust_po = val
            quote.job_name = val

    # Items: find Product # lines, then Product Code lines, then prices
    # Pattern: product name block, then Price/Qty/Total header, then $price qty $total
    # Product #: XXXXXX and Product Code: XXXXXX appear after each item block
    prod_no_re  = re.compile(r'Product\s*#[:\s]+(\d+)', re.IGNORECASE)
    prod_code_re = re.compile(r'Product\s+Code[:\s]+([A-Z0-9]+)', re.IGNORECASE)
    price_re    = re.compile(r'\$([\d.]+)\s+(\d+(?:\.\d+)?)\s+\$([\d,.]+)')

    lines = text.split('\n')
    items = []
    i = 0
    current_desc = []
    current_prod_no = ''
    current_prod_code = ''

    while i < len(lines):
        line = lines[i].strip()

        pn = prod_no_re.match(line)
        pc = prod_code_re.match(line)
        pm = price_re.search(line)

        if pn:
            current_prod_no = pn.group(1)
        elif pc:
            current_prod_code = pc.group(1)
        elif pm and current_prod_no:
            unit_price = float(pm.group(1))
            qty        = float(pm.group(2))
            total      = float(pm.group(3).replace(',', ''))
            # Description = all lines collected before Product # (skip "Price Qty Total" headers)
            desc = ' '.join(d for d in current_desc
                            if not re.match(r'^(Price|Qty|Total|\*Enter|to receive)', d, re.IGNORECASE))
            items.append({
                'desc': desc.strip(),
                'prod_no': current_prod_no,
                'prod_code': current_prod_code,
                'unit_price': unit_price,
                'qty': qty,
                'total': total,
            })
            current_desc = []
            current_prod_no = ''
            current_prod_code = ''
        elif not any(kw in line.upper() for kw in [
            'DENOMMEE', 'CART #', 'JOBNAME', 'NO SHIPPING', 'DATE:', 'TIME OPENED',
            'ITEM TOTAL', 'EXCLUDES TAX'
        ]) and line:
            current_desc.append(line)

        i += 1

    for it in items:
        quote.line_items.append(QuoteLineItem(
            mfr_code=it['prod_no'],
            webb_code=it['prod_code'],
            description=it['desc'],
            qty=it['qty'],
            unit_price=it['unit_price'],
            ext_price=it['total'],
            line_no='',
        ))

    quote.total = sum(i.ext_price for i in quote.line_items)
    return quote


def parse(text: str) -> Optional[WebbQuote]:
    """
    Parse an F.W. Webb quote from extracted PDF text.
    Returns a WebbQuote or None if parsing fails.
    """
    # --- Cart format ---
    if _is_cart(text):
        return _parse_cart(text)

    quote = WebbQuote()

    # Quote number (labeled "Number" on the form, appears near date)
    qn_match = re.search(r'(?:Quote\s+)?Number\s*\n?\s*(\d{7,})', text, re.IGNORECASE)
    if not qn_match:
        # Also appears in closing text "QUOTE # 96225586"
        qn_match = re.search(r'QUOTE\s*#\s*(\d{6,})', text, re.IGNORECASE)
    if qn_match:
        quote.quote_no = qn_match.group(1).strip()

    # Date (MM/DD/YY format near the top)
    date_match = re.search(r'(\d{2}/\d{2}/\d{2,4})\s+\d{6,}', text)
    if date_match:
        quote.quote_date = date_match.group(1).strip()

    # Customer PO# or Purchase Ord# — Webb uses this for job name
    po_match = re.search(r'(?:Customer PO#|Purchase Ord#)\s+(.+?)(?:Rel#|Ship Via|\n)', text, re.IGNORECASE)
    if po_match:
        val = po_match.group(1).strip()
        quote.cust_po = val
        quote.job_name = val   # Treat as job name unless it's numeric

    # If PO field is blank or generic, try Ship-To for job name
    # Ship-To shows project name as last line of address
    shipto_match = re.search(
        r'LOWELL[^\n]*\n\s*([A-Z][A-Z0-9 ]{2,})\s*\n',
        text
    )
    if shipto_match and not quote.job_name:
        quote.job_name = shipto_match.group(1).strip()

    # Total
    total_match = re.search(r'Total:\s+([\d,]+\.\d{2})', text)
    if total_match:
        quote.total = float(total_match.group(1).replace(',', ''))

    # --- Line items ---
    # Between the *---* separator lines
    table_match = re.search(
        r'\*-+\*\s*\n\s*Quantity\s+Description.*?\n\s*\*-+\*(.*?)(?:BREAK-ST:|Sub Total:|$)',
        text, re.DOTALL | re.IGNORECASE
    )

    if table_match:
        table_text = table_match.group(1)
    else:
        # Fallback: everything between the two separator lines
        sep_matches = list(re.finditer(r'\*-{10,}\*', text))
        if len(sep_matches) >= 2:
            table_text = text[sep_matches[1].end():sep_matches[1].end()+2000]
        else:
            return quote

    lines = [l.strip() for l in table_text.split('\n') if l.strip()]

    # Quote format: qty * description  net_price  ext_price  (ln#)
    item_re_quote = re.compile(
        r'^(\d+)\s+\*\s+'
        r'(.+?)\s+'
        r'([\d.]+)\s+'
        r'([\d,]+\.\d{2})\s+'
        r'\(\s*(\d+)\s*\)',
    )
    # Order ack format: qty  description  $unit_price  (ln#)
    item_re_order = re.compile(
        r'^(\d+)\s+'
        r'(.+?)\s+'
        r'\$([\d.]+)\s+'
        r'\(\s*(\d+)\s*\)',
    )
    is_order = _is_order_ack(text)

    # Part code line — quote: MFR_CODE (webb_code) | order: PARTCODE alone
    code_re = re.compile(r'^([A-Z0-9/\-]+)\s*(?:\((\d+)\))?\s*$')

    current = None
    for line in lines:
        m = item_re_order.match(line) if is_order else item_re_quote.match(line)
        if m:
            if current:
                quote.line_items.append(QuoteLineItem(**current))
            if is_order:
                qty  = float(m.group(1))
                desc = m.group(2).strip()
                unit = float(m.group(3))
                ext  = round(qty * unit, 2)
                lno  = m.group(4).strip()
            else:
                qty  = float(m.group(1))
                desc = m.group(2).strip()
                unit = float(m.group(3))
                ext  = float(m.group(4).replace(',', ''))
                lno  = m.group(5).strip()
            current = {
                'qty':         qty,
                'description': desc,
                'unit_price':  unit,
                'ext_price':   ext,
                'line_no':     lno,
                'mfr_code':    '',
                'webb_code':   '',
            }
        elif current:
            cm = code_re.match(line)
            if cm:
                current['mfr_code']  = cm.group(1).strip()
                current['webb_code'] = cm.group(2).strip() if cm.group(2) else ''
            elif not any(kw in line.upper() for kw in ['BREAK-ST', 'SUB TOTAL', 'FREIGHT', 'HANDLING', 'TOTAL', 'TAX']):
                current['description'] += ' ' + line

    if current:
        quote.line_items.append(QuoteLineItem(**current))

    return quote


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys, pdfplumber

    if len(sys.argv) < 2:
        print("Usage: python fwwebb.py path/to/quote.pdf")
        sys.exit(1)

    with pdfplumber.open(sys.argv[1]) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    if not can_parse(text):
        print("Not an F.W. Webb quote.")
        sys.exit(1)

    q = parse(text)
    print(f"Vendor:     {q.vendor}")
    print(f"Quote No:   {q.quote_no}")
    print(f"Date:       {q.quote_date}")
    print(f"Cust PO:    {q.cust_po}")
    print(f"Job:        {q.job_name}")
    print(f"Total:      ${q.total:,.2f}")
    print(f"\nLine Items ({len(q.line_items)}):")
    for i, item in enumerate(q.line_items, 1):
        print(f"  {i}. Ln{item.line_no} [{item.mfr_code}] ({item.webb_code})")
        print(f"     {item.description}")
        print(f"     qty={item.qty}  unit=${item.unit_price:.3f}  total=${item.ext_price:.2f}")
