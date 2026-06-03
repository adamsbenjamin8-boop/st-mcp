"""
ST MCP Desktop App — Launcher
=====================================
System tray icon + status window for the Denomme & Plumbing ServiceTitan MCP servers.

Architecture
------------
  ProcessManager  — starts / monitors / restarts the Python MCP sub-processes
  StatusWindow    — customtkinter UI (opens on tray click)
  TrayIcon        — pystray icon (runs on a daemon thread)
  UpdateManager   — periodic GitHub update check (background thread)

Managed processes
-----------------
  1. ST Writer MCP   — servicetitan_writer.py
     (the writer / dispatch / PO / timesheet connector)

  2. ST Reader MCP   — configured via Claude's mcp_config; this app does NOT
     start it, but it can detect it via the named pipe / process list and show
     its health status.

Entry point: __main__ block at the bottom.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import customtkinter as ctk
import pystray
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Resolve paths — works both frozen (PyInstaller) and from source
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent.parent             # C:\ST\


def _find_python() -> Path:
    """
    Locate a working Python interpreter.
    Priority:
      1. Current interpreter (when running from source)
      2. Bundled Python next to the .exe (APP_DIR/python/python.exe)
      3. System Python on PATH
      4. Common Windows install locations
    """
    if not getattr(sys, "frozen", False):
        return Path(sys.executable)

    # Bundled Python (installer can ship one)
    bundled = APP_DIR / "python" / "python.exe"
    if bundled.exists():
        return bundled

    # System Python on PATH
    import shutil
    for name in ("python3.exe", "python.exe", "python3", "python"):
        found = shutil.which(name)
        if found:
            return Path(found)

    # Common Windows install paths
    local_app = Path(os.environ.get("LOCALAPPDATA", "C:/Users"))
    candidates = [
        local_app / "Programs" / "Python",
        Path("C:/Python312"), Path("C:/Python311"), Path("C:/Python310"),
        Path("C:/Python39"),
    ]
    for base in candidates:
        if base.is_dir():
            exe = base / "python.exe"
            if exe.exists():
                return exe
            # Versioned subdirectory (e.g. Programs/Python/Python311/python.exe)
            for sub in sorted(base.iterdir(), reverse=True):
                exe = sub / "python.exe"
                if exe.exists():
                    return exe

    # Fall back — will produce a clear error at runtime
    return Path("python.exe")


PYTHON_EXE = _find_python()

WRITER_SCRIPT     = APP_DIR / "servicetitan_writer.py"
CACHE_SYNC_SCRIPT = APP_DIR / "st_cache_sync.py"
LOCK_FILE         = APP_DIR / "st_mcp.lock"


def _acquire_single_instance_lock() -> bool:
    """
    Write a lock file containing our PID.
    Returns True if we got the lock (no other instance running),
    False if another instance is already running.
    """
    if LOCK_FILE.exists():
        try:
            existing_pid = int(LOCK_FILE.read_text().strip())
            # Check if that process is still alive
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, existing_pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return False   # Other instance is running
        except Exception:
            pass   # Stale lock — overwrite it
    try:
        LOCK_FILE.write_text(str(os.getpid()))
    except OSError:
        pass
    return True


def _release_single_instance_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass

# ---------------------------------------------------------------------------
# Process Manager
# ---------------------------------------------------------------------------

class ManagedProcess:
    def __init__(self, name: str, script: Path, env: Optional[dict] = None):
        self.name      = name
        self.script    = script
        self.env       = env or {}
        self._proc: Optional[subprocess.Popen] = None
        self._lock     = threading.Lock()
        self.last_start: Optional[datetime] = None
        self.restart_count = 0

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    def start(self):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return  # already running
            env = {**os.environ, **self.env}
            self._proc = subprocess.Popen(
                [str(PYTHON_EXE), str(self.script)],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            self.last_start = datetime.now()
            self.restart_count += 1

    def stop(self):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None

    def restart(self):
        self.stop()
        time.sleep(0.5)
        self.start()


class ProcessManager:
    """Manages all MCP sub-processes and auto-restarts crashed ones."""

    def __init__(self):
        # Load ST credentials from environment or a sidecar .env file
        creds = _load_credentials()

        self.writer = ManagedProcess(
            name="ST Writer MCP",
            script=WRITER_SCRIPT,
            env=creds,
        )
        self._processes = [self.writer]
        self._monitor_thread: Optional[threading.Thread] = None
        self._running = False

        # Cache sync — runs on startup and every 24 hours
        self.cache_sync = CacheSyncManager(env=creds)

        # Quote watcher — polls OneDrive inbox every minute
        self.quote_watcher = QuoteWatcherManager(env=creds)

        # Email monitor — checks orders mailbox every 2 minutes
        self._email_env = creds

    @property
    def python_ok(self) -> bool:
        # If we found a full absolute path, verify it exists.
        # If we fell back to bare "python.exe", assume it's on PATH (can't check without running it).
        return PYTHON_EXE.exists() if PYTHON_EXE.is_absolute() else True

    def start_all(self):
        if not self.python_ok:
            return   # StatusWindow will show the error
        self._running = True
        for p in self._processes:
            p.start()
        self.cache_sync.start()
        self.quote_watcher.start()
        # Start email monitor in a background thread via the quote app
        threading.Thread(target=self._start_email_monitor, daemon=True).start()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def stop_all(self):
        self._running = False
        for p in self._processes:
            p.stop()

    def restart_all(self):
        for p in self._processes:
            p.restart()

    def _start_email_monitor(self):
        """Start the email monitor for the orders mailbox."""
        try:
            email_script = APP_DIR / "quote_app" / "email_monitor.py"
            if not email_script.exists():
                logging.error("email_monitor.py not found — email monitoring disabled")
                return
            env = {**os.environ, **self._email_env}
            if not env.get("ORDERS_EMAIL_PASSWORD"):
                logging.error("ORDERS_EMAIL_PASSWORD not set — email monitor not starting")
                return
            # Log to file so failures are visible
            log_path = APP_DIR / "email_monitor.log"
            log_file = open(log_path, "a", encoding="utf-8")
            log_file.write(f"\n=== Email monitor started {datetime.now().isoformat()} ===\n")
            log_file.flush()
            # Run as a persistent subprocess — it loops internally
            subprocess.Popen(
                [str(PYTHON_EXE), str(email_script)],
                env=env,
                stdout=log_file,
                stderr=log_file,
                stdin=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception as e:
            logging.error(f"Could not start email monitor: {e}")

    def _monitor_loop(self):
        """Auto-restart any process that crashes."""
        while self._running:
            for p in self._processes:
                if not p.is_running:
                    p.start()
            time.sleep(5)

    @property
    def all_running(self) -> bool:
        return all(p.is_running for p in self._processes)


# ---------------------------------------------------------------------------
# Cache Sync Manager
# ---------------------------------------------------------------------------

class CacheSyncManager:
    """
    Runs st_cache_sync.py on startup and then once every 24 hours.
    Runs in a background thread so it never blocks the UI or the MCP services.
    """

    INTERVAL_HOURS = 24

    def __init__(self, env: dict):
        self.env         = env
        self.last_synced: Optional[datetime] = None
        self.is_running  = False
        self.last_status = "Not yet synced"
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Kick off the background sync thread."""
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def run_now(self):
        """Trigger an immediate sync (called from UI button)."""
        threading.Thread(target=self._do_sync, daemon=True).start()

    def _loop(self):
        # Sync immediately on first start, then every 24 hours
        self._do_sync()
        while True:
            time.sleep(self.INTERVAL_HOURS * 3600)
            self._do_sync()

    def _do_sync(self):
        if not CACHE_SYNC_SCRIPT.exists():
            self.last_status = "st_cache_sync.py not found"
            return
        self.is_running  = True
        self.last_status = "Syncing…"
        log_path = APP_DIR / "cache_sync.log"
        try:
            env = {**os.environ, **self.env}
            result = subprocess.run(
                [str(PYTHON_EXE), str(CACHE_SYNC_SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
                timeout=300,   # 5-minute hard limit
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            # Write output to log file for troubleshooting
            try:
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(f"=== Cache sync {datetime.now().isoformat()} ===\n")
                    f.write(result.stdout or "")
                    if result.stderr:
                        f.write("\n--- stderr ---\n")
                        f.write(result.stderr)
                    f.write(f"\nExit code: {result.returncode}\n")
            except OSError:
                pass

            if result.returncode == 0:
                self.last_synced = datetime.now()
                self.last_status = f"Last sync: {self.last_synced.strftime('%b %d %I:%M %p')}"
            else:
                self.last_status = "Sync failed — open cache_sync.log for details"
        except subprocess.TimeoutExpired:
            self.last_status = "Sync timed out"
        except Exception as e:
            self.last_status = f"Sync error: {e}"
        finally:
            self.is_running = False


# ---------------------------------------------------------------------------
# Quote Watcher Manager
# ---------------------------------------------------------------------------

QUOTE_APP_SCRIPT  = APP_DIR / "quote_app" / "main.py"
QUOTE_INBOX = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents" / "Purchasing" / "Incoming Quotes"


class QuoteWatcherManager:
    """
    Watches the OneDrive Incoming Quotes folder for new quote files and processes them.
    Runs as a background thread alongside the MCP services.
    """

    POLL_SECONDS = 60   # Check every minute

    def __init__(self, env: dict):
        self.env         = env
        self.last_processed: Optional[datetime] = None
        self.is_running  = False
        self.last_status = "Watching for quotes…"
        self.last_count  = 0
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if not QUOTE_APP_SCRIPT.exists():
            self.last_status = "quote_app/main.py not found"
            return
        QUOTE_INBOX.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def process_now(self):
        """Trigger an immediate scan (UI button)."""
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _loop(self):
        while True:
            self._do_scan()
            time.sleep(self.POLL_SECONDS)

    def _do_scan(self):
        if self.is_running:
            return
        # Check if any files are waiting
        try:
            files = [f for f in QUOTE_INBOX.iterdir()
                     if f.suffix.lower() in {'.pdf', '.csv', '.xlsx'}]
        except Exception:
            files = []

        if not files:
            return   # Nothing to do — silent

        self.is_running  = True
        self.last_status = f"Processing {len(files)} quote(s)…"
        log_path = APP_DIR / "quote_processor.log"

        try:
            env = {**os.environ, **self.env}
            result = subprocess.run(
                [str(PYTHON_EXE), str(QUOTE_APP_SCRIPT), "--once"],
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"\n=== Quote scan {datetime.now().isoformat()} ===\n")
                    f.write(result.stdout or "")
                    if result.stderr:
                        f.write("--- stderr ---\n")
                        f.write(result.stderr)
                    f.write(f"Exit: {result.returncode}\n")
            except OSError:
                pass

            if result.returncode == 0:
                self.last_processed = datetime.now()
                self.last_count     = len(files)
                self.last_status    = (
                    f"Last run: {self.last_processed.strftime('%b %d %I:%M %p')} "
                    f"({self.last_count} file{'s' if self.last_count != 1 else ''})"
                )
            else:
                self.last_status = "Processing failed — check quote_processor.log"
        except subprocess.TimeoutExpired:
            self.last_status = "Processing timed out"
        except Exception as e:
            self.last_status = f"Error: {e}"
        finally:
            self.is_running = False


# ---------------------------------------------------------------------------
# Credentials loader
# ---------------------------------------------------------------------------

def _load_credentials() -> dict:
    """
    Returns all env-var credentials needed by sub-processes.
    Priority: OS environment vars → APP_DIR/.env sidecar file.
    """
    keys = [
        "ST_CLIENT_ID", "ST_CLIENT_SECRET", "ST_APP_KEY", "ST_TENANT_ID",
        "ORDERS_EMAIL_ADDRESS", "ORDERS_EMAIL_PASSWORD",
        "SMARTSHEET_API_KEY", "ANTHROPIC_API_KEY", "TEAMS_PURCHASING_WEBHOOK",
    ]
    # Start with OS environment
    creds = {k: os.environ.get(k, "") for k in keys}
    # Always overlay with .env file so local installs pick up all credentials
    env_file = APP_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                k = k.strip()
                if k in keys:
                    creds[k] = v.strip()
    return {k: v for k, v in creds.items() if v}


# ---------------------------------------------------------------------------
# Status Window
# ---------------------------------------------------------------------------

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

GREEN  = "#2ecc71"
RED    = "#e74c3c"
YELLOW = "#f39c12"
GRAY   = "#888888"

class StatusWindow(ctk.CTk):
    def __init__(self, proc_manager: ProcessManager, tray_app: "TrayApp"):
        super().__init__()
        self.proc_manager = proc_manager
        self.tray_app     = tray_app
        self._update_result = None

        self.title("ST MCP — Status")
        self.geometry("480x490")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._hide)

        self._build_ui()
        self._refresh_loop()

    def _build_ui(self):
        pad = {"padx": 16, "pady": 8}

        # Header
        header = ctk.CTkFrame(self, fg_color="#1a1a2e", corner_radius=0)
        header.pack(fill="x")
        ctk.CTkLabel(header, text="ServiceTitan MCP",
                     font=ctk.CTkFont(size=18, weight="bold"),
                     text_color="white").pack(side="left", padx=16, pady=12)
        from version import APP_VERSION
        ctk.CTkLabel(header, text=f"v{APP_VERSION}",
                     font=ctk.CTkFont(size=12),
                     text_color=GRAY).pack(side="right", padx=16, pady=12)

        # Services section
        svc_frame = ctk.CTkFrame(self)
        svc_frame.pack(fill="x", **pad)
        ctk.CTkLabel(svc_frame, text="SERVICES",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=GRAY).pack(anchor="w", padx=12, pady=(8, 4))

        self._service_rows = {}
        for proc in self.proc_manager._processes:
            row = self._make_service_row(svc_frame, proc.name)
            self._service_rows[proc.name] = row

        # Cache sync row
        self._cache_row = self._make_service_row(svc_frame, "Cache Sync")

        # Quote watcher row
        self._quote_row = self._make_service_row(svc_frame, "Quote Watcher")

        # Action buttons
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=4)

        self.restart_btn = ctk.CTkButton(
            btn_frame, text="⟳  Restart Services", width=190,
            command=self._restart)
        self.restart_btn.pack(side="left", padx=(0, 6))

        self.sync_btn = ctk.CTkButton(
            btn_frame, text="⟳  Sync Cache", width=130,
            fg_color="#555", hover_color="#666",
            command=self._sync_cache_now)
        self.sync_btn.pack(side="left", padx=(0, 6))

        self.update_btn = ctk.CTkButton(
            btn_frame, text="↑  Updates", width=100,
            fg_color="#555", hover_color="#666",
            command=self._check_updates)
        self.update_btn.pack(side="left", padx=(0, 6))

        self.quote_btn = ctk.CTkButton(
            btn_frame, text="⚡ Quotes", width=90,
            fg_color="#555", hover_color="#666",
            command=self._process_quotes_now)
        self.quote_btn.pack(side="left", padx=(0, 6))

        self.log_btn = ctk.CTkButton(
            btn_frame, text="📋 Log", width=70,
            fg_color="#444", hover_color="#555",
            command=self._open_log)
        self.log_btn.pack(side="left")

        # Update banner (hidden until update found)
        self.update_banner = ctk.CTkFrame(self, fg_color="#1a3a1a", corner_radius=8)
        self.update_label  = ctk.CTkLabel(self.update_banner, text="",
                                           text_color=GREEN, wraplength=380)
        self.update_label.pack(padx=12, pady=8)
        self.install_btn = ctk.CTkButton(
            self.update_banner, text="Install Update & Restart",
            fg_color=GREEN, hover_color="#27ae60", text_color="black",
            command=self._install_update)
        self.install_btn.pack(pady=(0, 8))
        # Don't pack the banner yet — shown only when update is available

        # Status bar
        self.status_bar = ctk.CTkLabel(self, text="Running",
                                        text_color=GRAY,
                                        font=ctk.CTkFont(size=11))
        self.status_bar.pack(side="bottom", pady=6)

    def _make_service_row(self, parent, name: str) -> dict:
        row_frame = ctk.CTkFrame(parent, fg_color="#2a2a3e", corner_radius=6)
        row_frame.pack(fill="x", padx=12, pady=3)

        dot = ctk.CTkLabel(row_frame, text="●", text_color=GRAY,
                            font=ctk.CTkFont(size=16), width=24)
        dot.pack(side="left", padx=(10, 4), pady=8)

        lbl = ctk.CTkLabel(row_frame, text=name,
                            font=ctk.CTkFont(size=13), anchor="w")
        lbl.pack(side="left", padx=4, pady=8)

        detail = ctk.CTkLabel(row_frame, text="Starting…",
                              text_color=GRAY, font=ctk.CTkFont(size=11))
        detail.pack(side="right", padx=12, pady=8)

        return {"dot": dot, "detail": detail}

    def _refresh_loop(self):
        self._refresh_ui()
        self.after(3000, self._refresh_loop)

    def _refresh_ui(self):
        for proc in self.proc_manager._processes:
            row = self._service_rows.get(proc.name)
            if not row:
                continue
            if proc.is_running:
                row["dot"].configure(text_color=GREEN)
                since = ""
                if proc.last_start:
                    elapsed = datetime.now() - proc.last_start
                    h, rem = divmod(int(elapsed.total_seconds()), 3600)
                    m, s   = divmod(rem, 60)
                    since = f"up {h}h {m}m" if h else f"up {m}m {s}s"
                row["detail"].configure(text=f"PID {proc.pid}  {since}")
            else:
                row["dot"].configure(text_color=RED)
                row["detail"].configure(text="Stopped")

        # Cache sync row
        cs = self.proc_manager.cache_sync
        if cs.is_running:
            self._cache_row["dot"].configure(text_color=YELLOW)
            self._cache_row["detail"].configure(text="Syncing…")
        elif cs.last_synced:
            self._cache_row["dot"].configure(text_color=GREEN)
            self._cache_row["detail"].configure(text=cs.last_status)
        else:
            self._cache_row["dot"].configure(text_color=GRAY)
            self._cache_row["detail"].configure(text=cs.last_status)

        # Quote watcher row
        qw = self.proc_manager.quote_watcher
        if qw.is_running:
            self._quote_row["dot"].configure(text_color=YELLOW)
            self._quote_row["detail"].configure(text="Processing…")
        elif qw.last_processed:
            self._quote_row["dot"].configure(text_color=GREEN)
            self._quote_row["detail"].configure(text=qw.last_status)
        else:
            self._quote_row["dot"].configure(text_color=GRAY)
            self._quote_row["detail"].configure(text=qw.last_status)

        # Python missing warning
        if not self.proc_manager.python_ok:
            self.status_bar.configure(
                text="Python not found — install Python 3.9+ from python.org",
                text_color=RED,
            )

        # Update tray icon color
        self.tray_app.update_icon(self.proc_manager.all_running)

    def _hide(self):
        self.withdraw()

    def show(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _restart(self):
        self.status_bar.configure(text="Restarting services…", text_color=YELLOW)
        self.restart_btn.configure(state="disabled")
        def _do():
            self.proc_manager.restart_all()
            time.sleep(1)
            self.after(0, lambda: (
                self.status_bar.configure(text="Services restarted.", text_color=GREEN),
                self.restart_btn.configure(state="normal"),
            ))
        threading.Thread(target=_do, daemon=True).start()

    def _open_log(self):
        """Open the most recent log file in Notepad."""
        log_file = APP_DIR / "cache_sync.log"
        if not log_file.exists():
            log_file = APP_DIR / "app.log"
        if log_file.exists():
            os.startfile(str(log_file))
        else:
            self.status_bar.configure(text="No log file found yet.", text_color=GRAY)

    def _process_quotes_now(self):
        self.quote_btn.configure(state="disabled", text="Processing…")
        self.status_bar.configure(text="Scanning quote inbox…", text_color=GRAY)
        def _do():
            self.proc_manager.quote_watcher.process_now()
            time.sleep(0.5)
            while self.proc_manager.quote_watcher.is_running:
                time.sleep(0.5)
            self.after(0, lambda: (
                self.quote_btn.configure(state="normal", text="⚡ Quotes"),
                self.status_bar.configure(
                    text=self.proc_manager.quote_watcher.last_status,
                    text_color=GREEN,
                ),
            ))
        threading.Thread(target=_do, daemon=True).start()

    def _sync_cache_now(self):
        self.sync_btn.configure(state="disabled", text="Syncing…")
        self.status_bar.configure(text="Cache sync started…", text_color=GRAY)
        def _do():
            self.proc_manager.cache_sync.run_now()
            time.sleep(0.5)  # Give the thread a moment to set is_running = True
            # Wait for it to finish
            while self.proc_manager.cache_sync.is_running:
                time.sleep(0.5)
            self.after(0, lambda: (
                self.sync_btn.configure(state="normal", text="⟳  Sync Cache"),
                self.status_bar.configure(
                    text=self.proc_manager.cache_sync.last_status,
                    text_color=GREEN if self.proc_manager.cache_sync.last_synced else RED,
                ),
            ))
        threading.Thread(target=_do, daemon=True).start()

    def _check_updates(self):
        self.update_btn.configure(state="disabled", text="Checking…")
        self.status_bar.configure(text="Checking for updates…", text_color=GRAY)

        from updater import check_for_updates, UpdateResult

        def _do():
            result = check_for_updates()
            self.after(0, lambda: self._on_update_checked(result))

        threading.Thread(target=_do, daemon=True).start()

    def _on_update_checked(self, result):
        self.update_btn.configure(state="normal", text="↑  Updates")
        if result.available:
            self._update_result = result
            self.update_label.configure(
                text=f"Update v{result.version} available!\n{result.message[:120]}")
            self.update_banner.pack(fill="x", padx=16, pady=8)
            self.status_bar.configure(text=f"Update v{result.version} ready to install.",
                                       text_color=GREEN)
        else:
            self.status_bar.configure(text="You're up to date.", text_color=GREEN)

    def _install_update(self):
        if not self._update_result:
            return
        self.install_btn.configure(state="disabled", text="Installing…")
        result = self._update_result

        from updater import apply_script_update

        def _log(msg):
            self.after(0, lambda m=msg: self.status_bar.configure(text=m, text_color=GRAY))

        def _do():
            ok = apply_script_update(result, progress_cb=_log)
            if ok:
                _log("Update installed — syncing cache…")
                self.proc_manager.cache_sync.run_now()
                # Wait for cache sync to finish before restarting
                time.sleep(0.5)
                while self.proc_manager.cache_sync.is_running:
                    time.sleep(0.5)
                _log("Cache synced — restarting services…")
                time.sleep(1)
                self.proc_manager.restart_all()
                self.after(0, lambda: (
                    self.update_banner.pack_forget(),
                    self.status_bar.configure(text="Updated, cache synced & restarted ✓", text_color=GREEN),
                ))
            else:
                self.after(0, lambda: (
                    self.install_btn.configure(state="normal", text="Install Update & Restart"),
                    self.status_bar.configure(text="Update failed — check logs.", text_color=RED),
                ))

        threading.Thread(target=_do, daemon=True).start()


# ---------------------------------------------------------------------------
# Tray App
# ---------------------------------------------------------------------------

def _make_icon_image(color: str) -> Image.Image:
    """Generate a simple colored circle icon."""
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fill = (46, 204, 113) if color == "green" else (231, 76, 60)  # green / red
    draw.ellipse([4, 4, 60, 60], fill=fill)
    draw.text((22, 16), "D", fill="white")
    return img


class TrayApp:
    def __init__(self, proc_manager: ProcessManager):
        self.proc_manager = proc_manager
        self._icon_green  = _make_icon_image("green")
        self._icon_red    = _make_icon_image("red")
        self._window: Optional[StatusWindow] = None
        self._tray: Optional[pystray.Icon] = None

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("Open Status Window", self._open_window, default=True),
            pystray.MenuItem("Restart Services",   self._restart),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit",               self._quit),
        )

    def _open_window(self, icon=None, item=None):
        if self._window and self._window.winfo_exists():
            self._window.after(0, self._window.show)

    def _restart(self, icon=None, item=None):
        threading.Thread(target=self.proc_manager.restart_all, daemon=True).start()

    def _quit(self, icon=None, item=None):
        self.proc_manager.stop_all()
        if self._tray:
            self._tray.stop()

    def update_icon(self, running: bool):
        if self._tray:
            self._tray.icon = self._icon_green if running else self._icon_red

    def run(self):
        """Run the full app (blocking). Creates tray + Tkinter window."""
        # Start processes
        self.proc_manager.start_all()

        # Check for updates immediately on startup, then every 4 hours
        def _update_loop():
            from updater import check_for_updates
            # Small delay so UI is fully up before we show any banner
            time.sleep(10)
            while True:
                result = check_for_updates()
                if result.available and self._window:
                    self._window.after(0, lambda r=result: self._window._on_update_checked(r))
                time.sleep(4 * 3600)
        threading.Thread(target=_update_loop, daemon=True).start()

        # Build tray (runs on its own thread)
        self._tray = pystray.Icon(
            "ST MCP",
            icon=self._icon_green if self.proc_manager.all_running else self._icon_red,
            title="ST MCP — ServiceTitan Connector",
            menu=self._build_menu(),
        )
        tray_thread = threading.Thread(target=self._tray.run, daemon=True)
        tray_thread.start()

        # Build and run Tkinter status window on the main thread
        self._window = StatusWindow(self.proc_manager, self)
        self._window.after(100, self._wire_tray_window)
        self._window.mainloop()

        # When window closes (quit pressed), clean up
        self.proc_manager.stop_all()
        if self._tray:
            self._tray.stop()

    def _wire_tray_window(self):
        """Wire the tray Open action to the now-created window."""
        def _open(icon=None, item=None):
            self._window.after(0, self._window.show)
        self._tray.menu = pystray.Menu(
            pystray.MenuItem("Open Status Window", _open, default=True),
            pystray.MenuItem("Restart Services",   self._restart),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit",               self._quit),
        )
        # Start minimized to tray
        self._window.withdraw()


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Put APP_DIR at the front of sys.path so updated version.py (written next
    # to the .exe by the auto-updater) takes priority over the stale copy
    # bundled inside the executable.
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))

    # Set up crash logging — all unhandled exceptions go to app.log
    _log_path = APP_DIR / "app.log"
    logging.basicConfig(
        filename=str(_log_path),
        level=logging.ERROR,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    def _handle_exception(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
    sys.excepthook = _handle_exception

    if not _acquire_single_instance_lock():
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            "ST MCP Connector is already running.\nCheck the system tray.",
            "Already Running",
            0x40,  # MB_ICONINFORMATION
        )
        sys.exit(0)

    try:
        mgr = ProcessManager()
        app = TrayApp(mgr)
        app.run()
    finally:
        _release_single_instance_lock()
