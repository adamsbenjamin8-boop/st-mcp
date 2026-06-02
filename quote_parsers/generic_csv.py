"""
Generic CSV Quote Parser
===================================
Handles vendor quote exports in CSV format.

Supports the column layout:
  Line, Qty, Product, Cust Product, Ctrl#, Product Description,
  Branch Availability, Company Availability, Unit Price, Per/UOM, Ext. Price, Comment

Per/UOM notes:
  - "1/EA"    = each (unit price is per unit)
  - "100/FT"  = per 100 feet (divide unit price by 100 for per-foot price)
  - "1/FT"    = per foot
  - "100/EA"  = per hundred

Known vendors that use this CSV format:
  - F.W. Webb (filename is a long numeric quote/order number, e.g. 21338601269060.csv)
    Identify by: all-numeric filename + this column structure

Identification: filename ends in .csv AND contains these column headers
"""

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class QuoteLineItem:
    line_no: str
    vendor_part_no: str      # "Product" column
    ctrl_no: str             # "Ctrl#" — internal catalog number
    description: str
    qty: float
    unit_price: float        # normalized to per-unit
    uom: str                 # EA, FT, etc.
    raw_unit_price: float    # as listed on quote
    raw_per_uom: str         # e.g. "100/FT"
    ext_price: float


@dataclass
class CSVQuote:
    vendor: str = "Unknown CSV Vendor"
    quote_no: str = ""       # from filename if available
    line_items: list = field(default_factory=list)
    subtotal: float = 0.0


def can_parse_csv(filename: str, header_row: list) -> bool:
    """Return True if this CSV matches the known format."""
    if not filename.lower().endswith('.csv'):
        return False
    headers = [h.strip().strip('"').lower() for h in header_row]
    required = {'qty', 'product', 'product description', 'unit price', 'ext. price'}
    return required.issubset(set(headers))


def _normalize_price(unit_price: float, per_uom: str) -> tuple:
    """
    Normalize price to per-unit and return (unit_price_each, uom_label).
    per_uom examples: "1/EA", "100/FT", "1/FT", "100/EA"
    """
    per_uom = per_uom.strip().strip('"')
    match = re.match(r'(\d+)/([A-Z]+)', per_uom.upper())
    if match:
        divisor = float(match.group(1))
        uom_label = match.group(2)
        normalized = unit_price / divisor if divisor > 1 else unit_price
        return normalized, uom_label
    return unit_price, per_uom


def parse_csv(csv_text: str, filename: str = "", vendor_name: str = "Unknown CSV Vendor") -> CSVQuote:
    """
    Parse a vendor CSV quote.
    csv_text: raw file contents as string
    filename: original filename (used to extract quote number)
    vendor_name: set once vendor is identified
    """
    quote = CSVQuote(vendor=vendor_name)

    # Try to extract quote number from filename
    num_match = re.search(r'(\d{8,})', filename)
    if num_match:
        quote.quote_no = num_match.group(1)

    reader = csv.DictReader(io.StringIO(csv_text), quotechar='"')
    # Normalize headers
    fieldnames = [f.strip().strip('"') for f in (reader.fieldnames or [])]

    # Map normalized header names
    col_map = {}
    for f in fieldnames:
        key = f.lower().strip()
        col_map[key] = f

    for raw_row in reader:
        row = {k.strip().strip('"'): (v or '').strip().strip('"') for k, v in raw_row.items()}

        qty_str   = row.get('Qty', '').strip()
        product   = row.get('Product ', row.get('Product', '')).strip()  # trailing space variant
        ctrl      = row.get('Ctrl#', '').strip()
        desc      = row.get('Product Description', '').strip()
        price_str = row.get('Unit Price', '').strip()
        per_uom   = row.get('Per/UOM', '1/EA').strip()
        ext_str   = row.get('Ext. Price', '').strip()
        line_no   = row.get('Line', '').strip()

        if not qty_str or not price_str:
            continue

        try:
            qty       = float(qty_str)
            raw_price = float(price_str.replace(',', ''))
            ext_price = float(ext_str.replace(',', '')) if ext_str else 0.0
        except ValueError:
            continue

        unit_price_each, uom_label = _normalize_price(raw_price, per_uom)

        quote.line_items.append(QuoteLineItem(
            line_no=line_no,
            vendor_part_no=product,
            ctrl_no=ctrl,
            description=desc,
            qty=qty,
            unit_price=unit_price_each,
            uom=uom_label,
            raw_unit_price=raw_price,
            raw_per_uom=per_uom,
            ext_price=ext_price,
        ))

    quote.subtotal = sum(i.ext_price for i in quote.line_items)
    return quote


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python generic_csv.py path/to/quote.csv [VendorName]")
        sys.exit(1)

    vendor = sys.argv[2] if len(sys.argv) > 2 else "Unknown CSV Vendor"
    with open(sys.argv[1], encoding='utf-8', errors='replace') as f:
        content = f.read()

    q = parse_csv(content, filename=sys.argv[1], vendor_name=vendor)
    print(f"Vendor:    {q.vendor}")
    print(f"Quote No:  {q.quote_no}")
    print(f"Subtotal:  ${q.subtotal:,.2f}")
    print(f"\nLine Items ({len(q.line_items)}):")
    for item in q.line_items:
        print(f"  Ln{item.line_no}  [{item.vendor_part_no}] ({item.ctrl_no})")
        print(f"     {item.description}")
        print(f"     qty={item.qty}  unit=${item.unit_price:.4f}/ea  [{item.raw_per_uom}]  ext=${item.ext_price:.2f}")
