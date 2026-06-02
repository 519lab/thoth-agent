# hermes_state.py — back-compat shim (Thoth->Thoth rename, P4). Remove in a later cleanup phase.
import sys
from thoth_state import *  # noqa: F401,F403  (re-export public names for "from hermes_state import foo")
import thoth_state as _real
sys.modules[__name__] = _real  # identity: hermes_state IS thoth_state (shares private names + live globals like _pool)
