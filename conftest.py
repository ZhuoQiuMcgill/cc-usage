"""Make the cc_usage package importable when running pytest from the repo root,
without requiring an editable install."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
