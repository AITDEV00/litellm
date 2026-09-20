import sys
from pathlib import Path

_OICM_ROOT = Path(__file__).parents[2]
if str(_OICM_ROOT) not in sys.path:
    sys.path.insert(0, str(_OICM_ROOT))

import hooks as _hooks_pkg

sys.modules["litellm_hooks"] = _hooks_pkg
