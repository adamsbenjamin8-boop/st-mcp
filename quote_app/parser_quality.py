"""
Parser Quality Checks — pure functions, no API calls, no side effects.
"""


def check_parse_quality(items: list, stated_total: float, pdf_text: str) -> dict:
    """
    Three checks on a parsed quote result.
    Returns: {"passed": bool, "issues": list[str], "is_image_pdf": bool, "computed_total": float}

    Checks:
      1. Image PDF  — pdf_text < 100 chars (text extraction failed)
      2. Item data  — any item missing description, qty <= 0, or unit_price <= 0
      3. Reconcile  — sum(qty * unit_price) vs stated_total gap > 10 %
    """
    if len((pdf_text or "").strip()) < 100:
        return {"passed": False, "issues": ["image_pdf"], "is_image_pdf": True, "computed_total": 0.0}

    if not items:
        return {"passed": False, "issues": ["no_items"], "is_image_pdf": False, "computed_total": 0.0}

    issues   = []
    computed = 0.0

    seen_missing_prices = False
    seen_empty_descs    = False
    seen_invalid_qty    = False

    for item in items:
        desc       = (getattr(item, 'description', '') or '').strip()
        qty        = float(getattr(item, 'qty', 0) or 0)
        unit_price = float(getattr(item, 'unit_price', 0) or 0)

        if not desc:
            seen_empty_descs = True
        if qty <= 0:
            seen_invalid_qty = True
        if unit_price <= 0:
            seen_missing_prices = True

        computed += qty * unit_price

    if seen_empty_descs:
        issues.append("empty_descriptions")
    if seen_invalid_qty:
        issues.append("invalid_qty")
    if seen_missing_prices:
        issues.append("missing_unit_prices")

    if stated_total > 0 and items:
        gap = abs(computed - stated_total) / stated_total
        if gap > 0.10:
            issues.append("reconciliation_gap")

    return {
        "passed":         len(issues) == 0,
        "issues":         issues,
        "is_image_pdf":   False,
        "computed_total": round(computed, 2),
    }
