"""
pytest conftest: ensures project root is in sys.path so package imports work.
"""
import sys
from pathlib import Path

# Project root = parent of tests/
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
