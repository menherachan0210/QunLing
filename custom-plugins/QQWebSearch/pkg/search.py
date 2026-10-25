"""Compatibility alias for the shared hosted-search implementation."""
import sys
from qunling import web_search

sys.modules[__name__] = web_search
