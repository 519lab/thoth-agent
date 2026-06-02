# hermes_env.py — back-compat shim (Thoth->Thoth rename, P4). Remove in a later cleanup phase.
import sys
from thoth_env import *  # noqa: F401,F403  (re-export public names for "from hermes_env import foo")
import thoth_env as _real
sys.modules[__name__] = _real  # identity: hermes_env IS thoth_env (shares private names + live globals like _pool)
