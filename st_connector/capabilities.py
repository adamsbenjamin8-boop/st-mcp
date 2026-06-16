"""
ST Developer App capability grants — runtime check via JWT scope decoding.

ServiceTitan issues client-credentials tokens with exactly the scopes granted
in the Developer App.  Each resource has separate :r (read) and :w (write)
scope tokens, e.g. ``tn.jpm.jobs:r`` and ``tn.jpm.jobs:w``.

When Ben removes a write grant in the Developer App, the next token refresh
drops the :w scope and ``can_write()`` returns False automatically — no code
change needed.

Scope format confirmed from live token:  list[str], e.g.
  ['tn.acc.invoices:r', 'tn.acc.invoices:w', 'tn.jpm.jobs:r', ...]
"""

import base64
import json
import logging
import threading
import time
from typing import Dict

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scope → module mapping  (confirmed against live token payload)
# ---------------------------------------------------------------------------

#: The :r scope required to read each module's data from ST.
READ_SCOPES: Dict[str, str] = {
    "appointments": "tn.jpm.appointments:r",
    "jobs":         "tn.jpm.jobs:r",
    "invoices":     "tn.acc.invoices:r",
    "tasks":        "tn.tsm.tasks:r",
    "estimates":    "tn.sal.estimates:r",
    "pos":          "tn.inv.purchaseorders:r",
}

#: The :w scope required to write back to ST for each module.
#: If this scope is absent from the token, write-back is silently skipped.
WRITE_SCOPES: Dict[str, str] = {
    "appointments": "tn.jpm.appointments:w",
    "jobs":         "tn.jpm.jobs:w",
    "invoices":     "tn.acc.invoices:w",
    "tasks":        "tn.tsm.tasks:w",
    "estimates":    "tn.sal.estimates:w",
    "pos":          "tn.inv.purchaseorders:w",
}

# Re-decode token scopes every 5 minutes (token lifetime is typically 15 min).
_CACHE_TTL = 300

_state: Dict = {
    "scopes":     None,   # frozenset[str] | None
    "fetched_at": 0.0,
}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _decode_jwt_payload(token: str) -> dict:
    """Base64-decode the JWT payload without verifying the signature."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        return json.loads(base64.b64decode(padded))
    except Exception as exc:
        log.warning("Could not decode JWT payload: %s", exc)
        return {}


def _parse_scopes(payload: dict) -> frozenset:
    """
    Extract the scope claim and normalise to frozenset[str].
    ST tokens carry scope as a list; older tokens may use a space-delimited string.
    """
    raw = payload.get("scope", [])
    if isinstance(raw, list):
        return frozenset(s.strip() for s in raw if s.strip())
    if isinstance(raw, str):
        return frozenset(s.strip() for s in raw.split() if s.strip())
    return frozenset()


def _refresh() -> frozenset:
    # Delayed import to avoid circular dependency (st_api imports config)
    from . import st_api
    try:
        token   = st_api._get_token()
        payload = _decode_jwt_payload(token)
        scopes  = _parse_scopes(payload)
        log.debug("Capability probe: %d scopes in token", len(scopes))
        return scopes
    except Exception as exc:
        log.warning("Could not read ST token scopes: %s", exc)
        return frozenset()


def _get_scopes() -> frozenset:
    with _lock:
        now = time.monotonic()
        if _state["scopes"] is None or now - _state["fetched_at"] > _CACHE_TTL:
            _state["scopes"]     = _refresh()
            _state["fetched_at"] = now
        return _state["scopes"]


def invalidate() -> None:
    """Force a re-read on the next capability check (e.g. after a forced token refresh)."""
    with _lock:
        _state["fetched_at"] = 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def can_read(module: str) -> bool:
    """True if the Developer App has granted read access for this module."""
    scope = READ_SCOPES.get(module)
    return scope in _get_scopes() if scope else False


def can_write(module: str) -> bool:
    """
    True if the Developer App has granted write access for this module.
    Write-back functions must call this before touching the ST API.
    """
    scope = WRITE_SCOPES.get(module)
    return scope in _get_scopes() if scope else False


def get_all() -> Dict[str, Dict[str, bool]]:
    """
    Returns capability snapshot for all modules:
      {"appointments": {"read": True, "write": False}, ...}
    """
    scopes = _get_scopes()
    return {
        module: {
            "read":  READ_SCOPES.get(module)  in scopes,
            "write": WRITE_SCOPES.get(module) in scopes,
        }
        for module in READ_SCOPES
    }
