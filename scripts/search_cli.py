"""Run the phase 1 command-line harness without installing the package."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from collection_search.cli import main  # noqa: E402

raise SystemExit(main())
