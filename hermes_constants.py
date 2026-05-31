# hermes_constants.py — back-compat shim (Hermes->Thoth rename, P4). Remove in a later cleanup phase.
import sys
from thoth_constants import *  # noqa: F401,F403  (re-export public names for "from hermes_constants import foo")
import thoth_constants as _real
sys.modules[__name__] = _real  # identity: hermes_constants IS thoth_constants (shares private names + live globals like _pool)
