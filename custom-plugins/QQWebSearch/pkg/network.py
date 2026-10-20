"""Compatibility alias; monkeypatches still reach the shared safe transport."""

import sys
from qunling import network as _shared

sys.modules[__name__] = _shared
