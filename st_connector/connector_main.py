"""
st_connector — entry point.

Starts one daemon thread per enabled sync module.
Each thread runs its own loop independently so one failure can't block others.

On first run (or when .sync_state.json is missing / backfill_complete=False),
performs a full historical backfill before starting normal interval threads.
Delete .sync_state.json to force a fresh backfill on next start.

Usage:
    python -m st_connector.connector_main
    # or from repo root:
    python st_connector/connector_main.py
"""

import json
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

from .config import ENABLED_MODULES, SYNC_INTERVALS, SYNC_STATE_FILE   # noqa: E402
from . import capabilities                                               # noqa: E402

_MODULE_MAP = {
    "appointments": "st_connector.appointments_sync",
    "jobs":         "st_connector.jobs_sync",
    "invoices":     "st_connector.invoices_sync",
    "tasks":        "st_connector.tasks_sync",
    "estimates":    "st_connector.estimates_sync",
    "pos":          "st_connector.pos_sync",
}

_shutdown = threading.Event()


# ---------------------------------------------------------------------------
# Sync state file
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        return json.loads(SYNC_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"backfill_complete": False, "last_sync": None}


def _save_state(state: dict) -> None:
    try:
        SYNC_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("Could not write sync state file: %s", exc)


# ---------------------------------------------------------------------------
# Startup capability report
# ---------------------------------------------------------------------------

def _print_capability_table(caps: dict) -> None:
    COL = (16, 12, 13, 13)
    hdr = (
        f"{'Module':<{COL[0]}}"
        f"{'Config':<{COL[1]}}"
        f"{'SS←ST Read':<{COL[2]}}"
        f"{'ST←SS Write':<{COL[3]}}"
    )
    sep = "─" * sum(COL)
    log.info(hdr)
    log.info(sep)
    for module in _MODULE_MAP:
        enabled = ENABLED_MODULES.get(module, False)
        if not enabled:
            log.info(
                f"{module:<{COL[0]}}{'DISABLED':<{COL[1]}}{'—':<{COL[2]}}{'—':<{COL[3]}}"
            )
            continue
        mc = caps.get(module, {})
        read_s  = "GRANTED"  if mc.get("read")  else "NO SCOPE"
        write_s = "GRANTED"  if mc.get("write") else "read-only"
        log.info(
            f"{module:<{COL[0]}}{'ENABLED':<{COL[1]}}{read_s:<{COL[2]}}{write_s:<{COL[3]}}"
        )
    log.info(sep)
    log.info(
        "Write-back scope can be toggled in the ST Developer App "
        "— changes take effect on the next token refresh (~15 min)."
    )


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def _run_backfill(loaded: list) -> None:
    """Run sync_once(backfill=True) for each module sequentially."""
    log.info("=" * 60)
    log.info("BACKFILL MODE — fetching all historical records from ST")
    log.info("This may take several minutes on first run.")
    log.info("=" * 60)

    for name, mod in loaded:
        log.info("─" * 40)
        log.info("Backfill starting: %s", name)
        try:
            mod.sync_once(backfill=True)
            log.info("Backfill complete: %s", name)
        except Exception as exc:
            log.error("Backfill failed for %s: %s", name, exc, exc_info=True)
            log.warning("%s will be caught up by normal sync threads.", name)
        # Brief pause between modules so we don't hammer both APIs
        if not _shutdown.is_set():
            time.sleep(5)


# ---------------------------------------------------------------------------
# Thread runner (normal mode)
# ---------------------------------------------------------------------------

def _safe_loop(name: str, module) -> None:
    """Keeps the thread alive across any exception from sync_once()."""
    interval = SYNC_INTERVALS.get(name, 900)
    while not _shutdown.is_set():
        try:
            module.sync_once()
        except Exception as exc:
            log.error("[%s] unhandled exception in sync_once: %s", name, exc, exc_info=True)
        capabilities.invalidate()
        _shutdown.wait(interval)


def _load_module(dotted: str):
    import importlib
    return importlib.import_module(dotted)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=" * 60)
    log.info("st_connector starting")
    log.info("=" * 60)

    log.info("Checking ST Developer App capability grants…")
    caps = capabilities.get_all()
    _print_capability_table(caps)

    # Load all enabled modules that have read scope
    loaded: list[tuple[str, object]] = []
    for name, enabled in ENABLED_MODULES.items():
        if not enabled:
            continue
        if not caps.get(name, {}).get("read"):
            log.warning(
                "[%s] read scope '%s' not granted — module skipped",
                name, capabilities.READ_SCOPES.get(name, "?"),
            )
            continue
        dotted = _MODULE_MAP.get(name)
        if not dotted:
            log.warning("[%s] no module mapping — skipping", name)
            continue
        try:
            mod = _load_module(dotted)
            loaded.append((name, mod))
        except Exception as exc:
            log.error("[%s] import failed: %s", name, exc)

    if not loaded:
        log.error("No modules could be loaded. Check config and ST scope grants.")
        sys.exit(1)

    # Backfill if this is the first run (state file missing or incomplete)
    state = _load_state()
    if not state.get("backfill_complete", False):
        _run_backfill(loaded)
        _save_state({
            "backfill_complete": True,
            "last_sync": datetime.now(timezone.utc).isoformat(),
        })
        log.info("=" * 60)
        log.info("Backfill finished — starting normal sync threads.")
        log.info("=" * 60)
    else:
        last = state.get("last_sync", "unknown")
        log.info("Backfill already complete (last sync: %s). Starting normal threads.", last)

    # Start one daemon thread per module with staggered startup
    threads: list[threading.Thread] = []
    for name, mod in loaded:
        interval = SYNC_INTERVALS.get(name, 900)
        log.info("[%s] starting thread (interval=%ds)", name, interval)
        t = threading.Thread(
            target=_safe_loop,
            args=(name, mod),
            name=f"sync-{name}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        time.sleep(10)

    log.info("%d sync thread(s) running.  Ctrl-C or SIGTERM to stop.", len(threads))

    def _handle_signal(signum, frame):
        log.info("Shutdown signal received — stopping…")
        _shutdown.set()

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not _shutdown.is_set():
        time.sleep(1)

    log.info("st_connector stopped.")


if __name__ == "__main__":
    main()
