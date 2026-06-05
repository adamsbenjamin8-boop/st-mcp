"""
Vendor Router — detects vendor from a quote file and routes to the correct parser.
Auto-discovers any parser in quote_parsers/ that has can_parse() and parse() functions.
"""

import csv
import importlib
import io
import sys
from pathlib import Path

# Add parent to path so we can import quote_parsers
sys.path.insert(0, str(Path(__file__).parent.parent))

from quote_parsers import ferguson, johnstone, fwwebb, generic_csv

# Priority parsers — checked first (fast, known-good)
_PRIORITY_PARSERS = [
    ("Ferguson Enterprises", ferguson),
    ("F.W. Webb",            fwwebb),
    ("Johnstone Supply",     johnstone),
]


def _load_dynamic_parsers() -> list:
    """
    Load any additional parsers from quote_parsers/ that aren't in the priority list.
    Returns list of (vendor_name, module) tuples.
    """
    parsers_dir = Path(__file__).parent.parent / "quote_parsers"
    skip = {"ferguson", "fwwebb", "johnstone", "generic_csv", "__init__"}
    dynamic = []
    for f in sorted(parsers_dir.glob("*.py")):
        if f.stem in skip:
            continue
        try:
            mod = importlib.import_module(f"quote_parsers.{f.stem}")
            if callable(getattr(mod, "can_parse", None)) and callable(getattr(mod, "parse", None)):
                # Derive a display name from the module name
                vendor_name = f.stem.replace("_", " ").title()
                dynamic.append((vendor_name, mod))
        except Exception as e:
            print(f"  WARNING: Could not load parser {f.name}: {e}")
    return dynamic


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

    # Check priority parsers first
    for vendor_name, mod in _PRIORITY_PARSERS:
        if mod.can_parse(text):
            return vendor_name, mod.parse(text)

    # Check auto-discovered parsers
    for vendor_name, mod in _load_dynamic_parsers():
        try:
            if mod.can_parse(text):
                result = mod.parse(text)
                # Use vendor name from parsed result if available
                parsed_vendor = getattr(result, 'vendor', None) or vendor_name
                return parsed_vendor, result
        except Exception as e:
            print(f"  WARNING: Parser {vendor_name} failed: {e}")
            continue

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
