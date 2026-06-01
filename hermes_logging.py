# hermes_logging.py — back-compat shim (Thoth->Thoth rename, P4). Remove in a later cleanup phase.
import sys
from thoth_logging import *  # noqa: F401,F403  (re-export public names for "from hermes_logging import foo")
import thoth_logging as _real
sys.modules[__name__] = _real  # identity: hermes_logging IS thoth_logging (shares private names + live globals like _pool)
