"""
Vendor Router — detects vendor from a quote file and routes to the correct parser.
"""

import csv
import io
import sys
from pathlib import Path

# Add parent to path so we can import quote_parsers
sys.path.insert(0, str(Path(__file__).parent.parent))

from quote_parsers import ferguson, johnstone, fwwebb, generic_csv


def detect_and_parse(file_path: str):
    """
    Given a file path, detect the vendor and parse the quote.
    Returns (vendor_name: str, parsed_quote: object) or raises ValueError.

    Supported formats: PDF, CSV
    """
    path = Path(file_path)
    ext  = path.suffix.lower()

    if ext == '.pdf':
        return _parse_pdf(path)
    elif ext == '.csv':
        return _parse_csv(path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")


def _extract_pdf_text(path: Path) -> str:
    """Extract text from PDF using pdfplumber."""
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("pdfplumber not installed — run: pip install pdfplumber")

    with pdfplumber.open(str(path)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _parse_pdf(path: Path):
    text = _extract_pdf_text(path)

    if ferguson.can_parse(text):
        return "Ferguson Enterprises", ferguson.parse(text)

    if fwwebb.can_parse(text):
        return "F.W. Webb", fwwebb.parse(text)

    if johnstone.can_parse(text):
        return "Johnstone Supply", johnstone.parse(text)

    # Unknown vendor — return None so caller can escalate to Claude
    return None, None


def _parse_csv(path: Path):
    """Parse a CSV quote file."""
    try:
        content = path.read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        raise ValueError(f"Could not read CSV: {e}")

    reader = csv.reader(io.StringIO(content))
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("Empty CSV file")

    if generic_csv.can_parse_csv(path.name, header):
        # Identify vendor from filename pattern
        vendor_name = _identify_csv_vendor(path.name, content)
        quote = generic_csv.parse_csv(content, filename=path.name, vendor_name=vendor_name)
        return vendor_name, quote

    return None, None


def _identify_csv_vendor(filename: str, content: str) -> str:
    """Try to identify vendor from CSV filename or content."""
    import re
    # F.W. Webb: all-numeric filename (e.g. 21338601269060.csv)
    if re.match(r'^\d+\.csv$', filename.lower()):
        return "F.W. Webb"

    # Future: add more vendor CSV identifiers here
    return "Unknown Vendor"
