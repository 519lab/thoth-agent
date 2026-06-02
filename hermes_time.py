# hermes_time.py — back-compat shim (Thoth->Thoth rename, P4). Remove in a later cleanup phase.
import sys
from thoth_time import *  # noqa: F401,F403  (re-export public names for "from hermes_time import foo")
import thoth_time as _real
sys.modules[__name__] = _real  # identity: hermes_time IS thoth_time (shares private names + live globals like _pool)
