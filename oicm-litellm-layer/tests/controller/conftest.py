import os
import sys
from pathlib import Path

_CONTROLLER_SRC = Path(__file__).parents[2] / "controller"
if str(_CONTROLLER_SRC.parent) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_SRC.parent))

# CLUSTER_NAME is required rather than defaulted, because a guessed cluster name
# is a wrong answer that looks authoritative. Tests run outside a cluster, so
# the value the Deployment would inject is supplied here, before any controller
# module is imported.
os.environ.setdefault("CLUSTER_NAME", "alain")
