"""
Folder Watcher — scans the Incoming Quotes folder for new files and processes them.
Runs as a polling loop (checks every 60 seconds).
Can also be called once as a one-shot scan.
"""

import time
import threading
from pathlib import Path
from typing import Callable

from config import QUOTES_INBOX_FOLDER

SUPPORTED_EXTENSIONS = {'.pdf', '.csv', '.xlsx'}

# Track files that have already been queued to avoid double-processing
_processed_files: set = set()


def scan_and_process(folder: Path, processor_fn: Callable, one_shot: bool = False):
    """
    Scan a folder for new quote files and call processor_fn on each.
    If one_shot=True, scan once and return.
    Otherwise, loops every 60 seconds.
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
                # Mark as seen immediately to avoid double-processing
                _processed_files.add(str(file))
                print(f"\nNew quote file detected: {file.name}")
                try:
                    processor_fn(str(file))
                except Exception as e:
                    print(f"Error processing {file.name}: {e}")
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
