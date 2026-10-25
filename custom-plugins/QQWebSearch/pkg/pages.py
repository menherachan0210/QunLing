"""Compatibility module for the shared bounded public page reader."""
import sys
from qunling import web_pages

sys.modules[__name__] = web_pages
