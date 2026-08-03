import sys
from pathlib import Path

_CONTROLLER_SRC = Path(__file__).parents[2] / "controller"
if str(_CONTROLLER_SRC.parent) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_SRC.parent))
