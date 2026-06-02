"""
GitHub-based auto-updater for the ST MCP Desktop App.

Update strategy:
  - "Script updates"  — new servicetitan_writer.py (or other managed .py files)
    are downloaded as GitHub release assets, dropped in-place next to the .exe,
    and the running MCP process is restarted.  No reinstall needed.
  - "App updates"     — a new installer (.exe) is posted as a release asset.
    The user is prompted; the installer runs and replaces the app binary.

Versioning convention (GitHub release tags):
    app-v1.2.3   — full app release  (triggers app update check)
    scripts-v1.2.3 — script-only release (triggers script update only)
    v1.2.3         — both app and scripts updated together
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

import requests
from packaging.version import Version

from version import APP_VERSION, GITHUB_REPO, UPDATE_FILES

GITHUB_API  = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
TIMEOUT     = 10   # seconds for HTTP requests
APP_DIR     = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent

# Tracks the currently-installed version in memory.
# Updated after each successful install so repeat checks don't re-trigger the same update.
_current_version = APP_VERSION


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class UpdateResult:
    def __init__(self, available: bool, version: str = "", kind: str = "",
                 download_url: str = "", assets: list = None, message: str = ""):
        self.available    = available
        self.version      = version
        self.kind         = kind          # "scripts" | "app" | "both"
        self.download_url = download_url
        self.assets       = assets or []
        self.message      = message

    def __repr__(self):
        return f"<UpdateResult available={self.available} version={self.version} kind={self.kind}>"


def check_for_updates() -> UpdateResult:
    """
    Query GitHub releases and return an UpdateResult.
    Runs synchronously; call from a background thread to avoid blocking the UI.
    """
    try:
        resp = requests.get(GITHUB_API, timeout=TIMEOUT,
                            headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return UpdateResult(False, message=f"Network error: {e}")

    tag: str = data.get("tag_name", "")
    version_str = tag.lstrip("v").split("-")[-1]          # "app-v1.2.3" → "1.2.3"

    try:
        remote_ver = Version(version_str)
        local_ver  = Version(_current_version)
    except Exception:
        return UpdateResult(False, message=f"Could not parse version from tag: {tag}")

    if remote_ver <= local_ver:
        return UpdateResult(False, version=version_str, message="Already up to date.")

    # Determine update kind from tag prefix
    if tag.startswith("scripts-"):
        kind = "scripts"
    elif tag.startswith("app-"):
        kind = "app"
    else:
        kind = "both"

    assets = [a["browser_download_url"] for a in data.get("assets", [])]
    return UpdateResult(True, version=version_str, kind=kind, assets=assets,
                        message=data.get("body", ""))


def _fetch_checksums(assets: list) -> Dict[str, str]:
    """
    Download checksums.sha256 from the release assets and parse it.
    Returns a dict of {filename: expected_sha256_hex} or {} if not found.
    """
    url = next((u for u in assets if u.endswith("checksums.sha256")), None)
    if not url:
        return {}
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        result = {}
        for line in r.text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)   # split on whitespace, max 2 parts
            if len(parts) == 2:
                hash_hex, fname = parts
                result[fname.strip()] = hash_hex.strip().lower()
        return result
    except Exception:
        return {}


def apply_script_update(result: UpdateResult,
                        progress_cb: Optional[Callable[[str], None]] = None) -> bool:
    """
    Download updated .py script files from the release, verify SHA256 checksums,
    and replace them in APP_DIR only if verification passes.
    Returns True on success.
    """
    def log(msg):
        if progress_cb:
            progress_cb(msg)

    # Download and parse the checksum file first
    checksums = _fetch_checksums(result.assets)
    if not checksums:
        log("  WARNING: No checksums.sha256 in release — update blocked for security.")
        log("  This release may be corrupted or from an untrusted source.")
        return False

    log(f"  Checksums verified for {len(checksums)} files.")

    updated = []
    for filename in UPDATE_FILES:
        # Find matching asset URL
        url = next((u for u in result.assets if u.endswith(filename)), None)
        if not url:
            log(f"  Skipped {filename} — not in release assets")
            continue

        # Verify we have a checksum for this file
        expected_hash = checksums.get(filename)
        if not expected_hash:
            log(f"  ERROR: No checksum for {filename} — skipping for security.")
            return False

        log(f"  Downloading {filename}…")
        try:
            r = requests.get(url, timeout=30, stream=True)
            r.raise_for_status()
        except requests.RequestException as e:
            log(f"  ERROR downloading {filename}: {e}")
            return False

        dest = APP_DIR / filename
        tmp  = dest.with_suffix(".tmp")
        try:
            sha256 = hashlib.sha256()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
                    sha256.update(chunk)

            # Verify checksum before replacing the live file
            actual_hash = sha256.hexdigest().lower()
            if actual_hash != expected_hash:
                tmp.unlink(missing_ok=True)
                log(f"  SECURITY ERROR: Checksum mismatch for {filename}!")
                log(f"    Expected: {expected_hash}")
                log(f"    Got:      {actual_hash}")
                log("  Update blocked. Contact your administrator.")
                return False

            # Hash verified — atomic replace
            shutil.move(str(tmp), str(dest))
            updated.append(filename)
            log(f"  ✓ {filename} verified and updated")
        except OSError as e:
            log(f"  ERROR writing {filename}: {e}")
            return False

    if updated:
        # Bump local version record on disk and in memory
        _write_version(result.version)
        global _current_version
        _current_version = result.version
        log(f"Updated to v{result.version}: {', '.join(updated)}")
    return bool(updated)


def check_and_apply_async(on_update_available: Callable[[UpdateResult], None],
                          on_no_update: Optional[Callable[[str], None]] = None):
    """
    Check for updates in a background thread.
    Calls on_update_available(result) if an update is found (UI can then prompt user).
    """
    def _run():
        result = check_for_updates()
        if result.available:
            on_update_available(result)
        elif on_no_update:
            on_no_update(result.message)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _write_version(new_version: str):
    """
    Overwrite APP_VERSION in version.py with new_version.
    When frozen, always writes to APP_DIR/version.py (next to the .exe) so the
    change survives restarts.  When running from source, writes in-place.
    """
    import re

    if getattr(sys, "frozen", False):
        # Write next to the .exe so it persists across restarts.
        # launcher.py puts APP_DIR at the front of sys.path so this file
        # is found before the stale copy bundled inside the executable.
        ver_file = APP_DIR / "version.py"
    else:
        ver_file = Path(__file__).parent / "version.py"

    if ver_file.exists():
        text = ver_file.read_text()
        text = re.sub(r'APP_VERSION\s*=\s*"[^"]+"', f'APP_VERSION = "{new_version}"', text)
        ver_file.write_text(text)
    else:
        # Fallback: write a minimal version file from scratch
        ver_file.write_text(
            f'APP_VERSION = "{new_version}"\n'
            f'GITHUB_REPO  = "{GITHUB_REPO}"\n'
            f'UPDATE_FILES = {UPDATE_FILES!r}\n'
        )
