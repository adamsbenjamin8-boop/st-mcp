"""
Ferguson Enterprises Quote Parser
===================================
Handles THREE Ferguson PDF formats:
  1. Branch Price Quotation — emailed from branch reps
     Header: "FERGUSON ENTERPRISES LLC Price Quotation"
  2. Online Draft Quote — printed from ferguson.com
     Header: "Quote Detail", "Draft Quote #: DQ..."
  3. Proposal/Bid format — "Ferg-Proposal-BPLB..." files
     Header: "BRANCH - NEW ENGLAND", "Bid ID BPLB..."
"""
import re
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class QuoteLineItem:
    part_no: str
    description: str
    qty: float
    unit_price: float
    unit_of_measure: str
    raw_price: float
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
    patterns = [
        r"ferguson enterprises",
        r"fergusons?\s+quotation",
        r"Bid\s+ID\s+BPLB",
        r"nobody expects more from us than we do",
        r"ferguson\.com",
        r"price quotation",
        r"quote detail",
        r"draft quote",
        r"ferg.{0,5}proposal",
    ]
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def _is_online_quote(text: str) -> bool:
    return "DRAFT QUOTE" in text.upper() or "QUOTE DETAIL" in text.upper()


def _is_proposal_quote(text: str) -> bool:
    return bool(re.search(r'Bid\s+ID\s+BPLB', text, re.IGNORECASE))


def _parse_online(text: str) -> FergusonQuote:
    quote = FergusonQuote()
    m = re.search(r'Draft Quote\s*#[:\s]+([A-Z0-9]+)', text, re.IGNORECASE)
    if m:
        quote.bid_no = m.group(1).strip()
    m = re.search(r'Job Name[:\s]*\n?\s*([^\n\r]+)', text, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        if val.lower() not in ('job name', ''):
            quote.job_name = val
    m = re.search(r'PO\s*#[:\s]*\n?\s*([^\n\r]+)', text, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        if val.lower() not in ('po #', 'po#', ''):
            quote.cust_po = val
    m = re.search(r'Subtotal[:\s]*\$?([\d,]+\.\d{2})', text)
    if m:
        quote.net_total = float(m.group(1).replace(',', ''))
    line_re = re.compile(
        r'^\d+\s+([A-Z0-9\-]+)\s+(.+?)\s+\$([\d.]+)\s+per\s+([A-Z]+)\s+(\d+(?:\.\d+)?)\s+\$([\d,]+\.\d+)',
        re.MULTILINE | re.IGNORECASE
    )
    for m in line_re.finditer(text):
        quote.line_items.append(QuoteLineItem(
            part_no=m.group(1).strip(), description=m.group(2).strip(),
            qty=float(m.group(5)), unit_price=float(m.group(3)),
            unit_of_measure=m.group(4).upper(), raw_price=float(m.group(3)),
            total=float(m.group(6).replace(',', '')),
        ))
    return quote


def _parse_proposal(text: str) -> FergusonQuote:
    """
    Parse Bid ID BPLB proposal format.
    Part numbers always contain at least one digit (e.g. A3703001020, PFTSCOF2000WH).
    Line format: PARTNO  MFR  DESCRIPTION  QTY  U/M  $UNIT  $EXT
    """
    quote = FergusonQuote()

    m = re.search(r'Bid\s+ID\s+([A-Z0-9]+)', text, re.IGNORECASE)
    if m:
        quote.bid_no = m.group(1).strip()

    m = re.search(r'Job Name\s+(.+?)(?:\s+Salesperson|\n|Location)', text, re.IGNORECASE)
    if m:
        quote.job_name = m.group(1).strip()

    subtotals = re.findall(r'Subtotal:\s*\$?([\d,]+\.\d{2})', text, re.IGNORECASE)
    if subtotals:
        try:
            quote.net_total = sum(float(s.replace(',', '')) for s in subtotals)
        except ValueError:
            pass

    # Part numbers contain at least one digit — avoids matching plain words like STANDARD
    line_re = re.compile(
        r'^([A-Z][A-Z0-9\-\.\/]*\d[A-Z0-9\-\.\/]*)\s+'   # part_no (must have a digit)
        r'(?:[A-Z]{2,15}\s+)'                              # manufacturer (skip)
        r'(.+?)\s+'                                        # description
        r'(\d+(?:\.\d+)?)\s+'                              # qty
        r'(EA|LF|FT|PR|BX|PK|C|SF|LS|EA)\s+'             # UOM
        r'\$([\d,]+\.\d{2})\s+'                           # unit price
        r'\$([\d,]+\.\d{2})',                              # ext price
        re.MULTILINE | re.IGNORECASE
    )
    for m in line_re.finditer(text):
        try:
            quote.line_items.append(QuoteLineItem(
                part_no=m.group(1).strip(),
                description=m.group(2).strip(),
                qty=float(m.group(3)),
                unit_price=float(m.group(5).replace(',', '')),
                unit_of_measure=m.group(4).upper(),
                raw_price=float(m.group(5).replace(',', '')),
                total=float(m.group(6).replace(',', '')),
            ))
        except (ValueError, IndexError):
            continue

    # Fallback
    if not quote.line_items and quote.net_total > 0:
        quote.line_items.append(QuoteLineItem(
            part_no="SEE-QUOTE",
            description=f"Ferguson Enterprises Quote {quote.bid_no} - see attached PDF",
            qty=1, unit_price=quote.net_total, unit_of_measure="EA",
            raw_price=quote.net_total, total=quote.net_total,
        ))

    return quote


def parse(text: str) -> Optional[FergusonQuote]:
    if _is_online_quote(text):
        return _parse_online(text)
    if _is_proposal_quote(text):
        return _parse_proposal(text)

    # Branch Price Quotation format
    quote = FergusonQuote()
    m = re.search(r'Bid No[:\s]+([A-Z0-9]+)', text)
    if m:
        quote.bid_no = m.group(1).strip()
    m = re.search(r'Bid Date[:\s]+(\d{2}/\d{2}/\d{2,4})', text)
    if m:
        quote.bid_date = m.group(1).strip()
    m = re.search(r'Cust PO#[:\s]*([^\n\r]+)', text)
    if m:
        quote.cust_po = m.group(1).strip()
    m = re.search(r'Job Name[:\s]*([^\n\r]+)', text)
    if m:
        quote.job_name = m.group(1).strip()
    m = re.search(r'Net Total[:\s]*\$?([\d,]+\.?\d*)', text)
    if m:
        quote.net_total = float(m.group(1).replace(',', ''))

    table_match = re.search(
        r'Item\s+Description\s+Quantity\s+Net Price\s+UM\s+Total\s*\n(.*?)Net Total',
        text, re.DOTALL
    )
    if table_match:
        line_pattern = re.compile(
            r'^([A-Z0-9\-]+)\s+(.+?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+([A-Z]+)\s+([\d,]+(?:\.\d+)?)',
            re.MULTILINE
        )
        for m in line_pattern.finditer(table_match.group(1)):
            raw_price = float(m.group(4))
            um = m.group(5).upper()
            unit_price = raw_price / 100.0 if um == 'C' else raw_price
            quote.line_items.append(QuoteLineItem(
                part_no=m.group(1).strip(), description=m.group(2).strip(),
                qty=float(m.group(3)), unit_price=unit_price,
                unit_of_measure=um, raw_price=raw_price,
                total=float(m.group(6).replace(',', '')),
            ))

    if not quote.line_items and quote.net_total > 0:
        quote.line_items.append(QuoteLineItem(
            part_no="SEE-QUOTE",
            description=f"Ferguson Enterprises Quote {quote.bid_no} - see attached PDF",
            qty=1, unit_price=quote.net_total, unit_of_measure="EA",
            raw_price=quote.net_total, total=quote.net_total,
        ))

    return quote
