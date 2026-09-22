import sys
from pathlib import Path

TOOLBOX_DIR = Path(__file__).resolve().parents[1] / "frostbay-toolbox"
if str(TOOLBOX_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLBOX_DIR))

from textual_app import main

raise SystemExit(main())
