"""
Version tracking for the ST MCP Desktop App.
This file is updated automatically by the GitHub Actions release workflow.
"""

APP_VERSION = "1.0.5"
GITHUB_REPO  = "adamsbenjamin8-boop/st-mcp"
UPDATE_FILES = [
    "servicetitan_writer.py",
    "st_cache_sync.py",
    # Quote app files — updated automatically with scripts-v releases
    "quote_app/main.py",
    "quote_app/config.py",
    "quote_app/vendor_router.py",
    "quote_app/st_client.py",
    "quote_app/teams_notifier.py",
    "quote_app/smartsheet_logger.py",
    "quote_app/quote_processor.py",
    "quote_app/folder_watcher.py",
    # Vendor parsers — add new vendors here
    "quote_parsers/__init__.py",
    "quote_parsers/ferguson.py",
    "quote_parsers/johnstone.py",
    "quote_parsers/fwwebb.py",
    "quote_parsers/generic_csv.py",
]
