"""
Quote App — Entry Point
========================
Processes incoming vendor quotes → creates PO Requests in ServiceTitan
→ notifies purchaser via Teams.

Run manually:  python main.py
Run once:      python main.py --once
Process file:  python main.py --file path/to/quote.pdf

Normally launched by the ST MCP desktop app's folder watcher.
"""

import sys
import argparse
from pathlib import Path

# Ensure quote_parsers directory is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from quote_processor import process_quote_file
from folder_watcher import scan_and_process, start_watcher_thread
from config import QUOTES_INBOX_FOLDER


def main():
    parser = argparse.ArgumentParser(description="ST Quote Processor")
    parser.add_argument("--file",  help="Process a specific file")
    parser.add_argument("--once",  action="store_true", help="Scan once and exit")
    parser.add_argument("--watch", action="store_true", help="Watch folder continuously (default)")
    args = parser.parse_args()

    if args.file:
        # Process a single file
        result = process_quote_file(args.file)
        sys.exit(0 if result["success"] else 1)

    elif args.once:
        # One-shot scan of the inbox folder
        scan_and_process(QUOTES_INBOX_FOLDER, process_quote_file, one_shot=True)

    else:
        # Continuous watch (default)
        print("ST Quote Processor starting...")
        print(f"Inbox: {QUOTES_INBOX_FOLDER}")
        scan_and_process(QUOTES_INBOX_FOLDER, process_quote_file, one_shot=False)


if __name__ == "__main__":
    main()
