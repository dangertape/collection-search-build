"""PyInstaller entry for the windowed app."""

import os
import sys

# A windowed (no console) build starts with sys.stdout/stderr set to None, so
# any library that writes to or inspects them crashes. Point them at devnull.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from collection_search.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
