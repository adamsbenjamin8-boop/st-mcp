"""
Parser Failure Tracker — JSON-backed consecutive failure counter.
Path: C:/Program Files/ST_MCP/parser_failures.json
"""
import json
from pathlib import Path

_FAILURES_FILE   = Path("C:/Program Files/ST_MCP/parser_failures.json")
_REGEN_THRESHOLD = 2


def _load() -> dict:
    if _FAILURES_FILE.exists():
        try:
            return json.loads(_FAILURES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save(data: dict):
    try:
        _FAILURES_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FAILURES_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  WARNING: Could not save parser_failures.json: {e}")


def record_failure(vendor_name: str, issue: str):
    """Increment consecutive failure count for vendor."""
    data = _load()
    key  = vendor_name.lower().strip()
    entry = data.get(key, {"consecutive_failures": 0, "last_issue": ""})
    entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
    entry["last_issue"] = issue
    data[key] = entry
    _save(data)


def record_success(vendor_name: str):
    """Reset consecutive failure count for vendor."""
    data = _load()
    key  = vendor_name.lower().strip()
    if key in data:
        data[key]["consecutive_failures"] = 0
    _save(data)


def get_consecutive_failures(vendor_name: str) -> int:
    """Return current consecutive failure count for vendor."""
    return _load().get(vendor_name.lower().strip(), {}).get("consecutive_failures", 0)


def should_regenerate_parser(vendor_name: str) -> bool:
    """True when consecutive failures have reached the regeneration threshold."""
    return get_consecutive_failures(vendor_name) >= _REGEN_THRESHOLD
