"""
Root conftest — adds demo/ and its module subfolders to sys.path so test
modules can import ontology, commercial_rules, and auth without installation.
Run pytest from demo/ or from demo/tests/.
"""

import sys
from pathlib import Path

_DEMO = Path(__file__).parent.parent
sys.path.insert(0, str(_DEMO))                  # auth
sys.path.insert(0, str(_DEMO / "ontology"))     # ontology
sys.path.insert(0, str(_DEMO / "rules"))        # commercial_rules
