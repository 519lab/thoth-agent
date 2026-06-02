# hermes_db.py — back-compat shim (Thoth->Thoth rename, P4). Remove in a later cleanup phase.
import sys
from thoth_db import *  # noqa: F401,F403  (re-export public names for "from hermes_db import foo")
import thoth_db as _real
sys.modules[__name__] = _real  # identity: hermes_db IS thoth_db (shares private names + live globals like _pool)
