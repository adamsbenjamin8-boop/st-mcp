"""
Entry point for the ST Connector managed service (used by desktop launcher).
Must be at repo root so the launcher can find it as a plain .py script.
Relative imports in st_connector require the package to be importable,
which is satisfied by inserting the repo root into sys.path here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from st_connector.connector_main import main

if __name__ == "__main__":
    main()
