"""PyInstaller entry for the console shim Claude Desktop launches. Standard library only."""

import sys

from collection_search.shim import main

if __name__ == "__main__":
    sys.exit(main())
