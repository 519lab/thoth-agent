# hermes_bootstrap.py — back-compat shim (Hermes->Thoth rename, P4). Remove in a later cleanup phase.
import sys
from thoth_bootstrap import *  # noqa: F401,F403  (re-export public names for "from hermes_bootstrap import foo")
import thoth_bootstrap as _real
sys.modules[__name__] = _real  # identity: hermes_bootstrap IS thoth_bootstrap (shares private names + live globals like _pool)
