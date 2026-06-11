"""
Folder Watcher — scans the Incoming Quotes folder for new files and processes them.
Runs as a polling loop (checks every 60 seconds).
Can also be called once as a one-shot scan.
"""

import time
import shutil
import traceback
import threading
from pathlib import Path
from typing import Callable

from config import QUOTES_INBOX_FOLDER

SUPPORTED_EXTENSIONS = {'.pdf', '.csv', '.xlsx'}

# Track files that have already been successfully processed to avoid double-processing.
# Failed files are NOT added here so they get retried on the next scan.
_processed_files: set = set()


def _get_failed_folder(inbox: Path) -> Path:
    failed = inbox.parent / "Failed"
    failed.mkdir(parents=True, exist_ok=True)
    return failed


def scan_and_process(folder: Path, processor_fn: Callable, one_shot: bool = False):
    """
    Scan a folder for new quote files and call processor_fn on each.
    If one_shot=True, scan once and return.
    Otherwise, loops every 60 seconds.

    On success  → file moves to Processed (handled by quote_processor).
    On failure  → file moves to Failed/ subfolder with a .error sidecar.
    """
    folder = Path(folder)
    if not folder.exists():
        print(f"Creating quotes inbox folder: {folder}")
        folder.mkdir(parents=True, exist_ok=True)

    print(f"Watching for quotes in: {folder}")

    while True:
        try:
            for file in sorted(folder.iterdir()):
                if file.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                if str(file) in _processed_files:
                    continue

                print(f"\nNew quote file detected: {file.name}")
                # Mark as seen NOW to prevent concurrent re-entry in the same scan pass.
                # If processing fails we REMOVE it so the next scan picks it up again —
                # unless we quarantine it to Failed/.
                _processed_files.add(str(file))

                try:
                    processor_fn(str(file))
                    # Success — leave in _processed_files so we don't reprocess.
                except Exception as e:
                    error_detail = traceback.format_exc()
                    print(f"  ✗ Error processing {file.name}: {e}")
                    print(error_detail)

                    # Move to Failed/ so operators can see what went wrong and
                    # files don't pile up invisibly in the inbox.
                    try:
                        failed_folder = _get_failed_folder(folder)
                        dest = failed_folder / file.name
                        # Avoid clobbering an existing failed copy
                        if dest.exists():
                            dest = failed_folder / (file.stem + f"_{int(time.time())}" + file.suffix)
                        shutil.move(str(file), str(dest))
                        # Write sidecar error file
                        dest.with_suffix(".error").write_text(
                            f"File: {file.name}\nError: {e}\n\n{error_detail}", encoding="utf-8"
                        )
                        print(f"  → Moved to Failed/: {dest.name}")
                    except Exception as move_err:
                        print(f"  ✗ Could not move to Failed/: {move_err}")
                        # Remove from seen set so next scan retries it
                        _processed_files.discard(str(file))

        except Exception as e:
            print(f"Watcher error: {e}")

        if one_shot:
            return

        time.sleep(60)   # Check every minute


def start_watcher_thread(processor_fn: Callable) -> threading.Thread:
    """Start the folder watcher in a daemon background thread."""
    t = threading.Thread(
        target=scan_and_process,
        args=(QUOTES_INBOX_FOLDER, processor_fn),
        daemon=True,
        name="QuoteFolderWatcher"
    )
    t.start()
    return t
