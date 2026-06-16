"""
st_connector — entry point.

Starts one daemon thread per enabled sync module.
Each thread runs its own loop independently so one failure can't block others.

Usage:
    python -m st_connector.connector_main
    # or from repo root:
    python st_connector/connector_main.py
"""

import logging
import signal
import sys
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

from .config import ENABLED_MODULES, SYNC_INTERVALS   # noqa: E402
from . import capabilities                             # noqa: E402

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
# Startup capability report
# ---------------------------------------------------------------------------

def _print_capability_table(caps: dict) -> None:
    """
    Print a clear table of which modules are active and whether
    ST write-back is currently permitted by the Developer App.

    Example output:
      Module          Config      SS←ST Read   ST←SS Write
      ─────────────────────────────────────────────────────
      appointments    ENABLED     GRANTED      GRANTED
      jobs            ENABLED     GRANTED      read-only
      invoices        DISABLED    —            —
      tasks           ENABLED     GRANTED      read-only
      estimates       ENABLED     GRANTED      GRANTED
      pos             ENABLED     GRANTED      GRANTED
    """
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
        read_s  = "GRANTED"   if mc.get("read")  else "NO SCOPE"
        write_s = "GRANTED"   if mc.get("write") else "read-only"
        log.info(
            f"{module:<{COL[0]}}{'ENABLED':<{COL[1]}}{read_s:<{COL[2]}}{write_s:<{COL[3]}}"
        )
    log.info(sep)
    log.info(
        "Write-back scope can be toggled in the ST Developer App "
        "— changes take effect on the next token refresh (~15 min)."
    )


# ---------------------------------------------------------------------------
# Thread runner
# ---------------------------------------------------------------------------

def _safe_loop(name: str, module) -> None:
    """Keeps the thread alive across any exception from sync_once()."""
    interval = SYNC_INTERVALS.get(name, 900)
    while not _shutdown.is_set():
        try:
            module.sync_once()
        except Exception as exc:
            log.error("[%s] unhandled exception in sync_once: %s", name, exc, exc_info=True)
        # Invalidate capability cache before next cycle so Dev App changes are picked up
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

    # Probe ST Developer App capability grants before launching threads
    log.info("Checking ST Developer App capability grants…")
    caps = capabilities.get_all()
    _print_capability_table(caps)

    threads: list[threading.Thread] = []

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
        except Exception as exc:
            log.error("[%s] import failed: %s", name, exc)
            continue

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

        # Stagger startup 10 s so modules don't hit ST/SS simultaneously
        time.sleep(10)

    if not threads:
        log.error("No modules running. Check ENABLED_MODULES in config.py and ST scope grants.")
        sys.exit(1)

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
