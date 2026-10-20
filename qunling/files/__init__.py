"""Bounded, SDK-independent attachment reading and artifact creation.

Paths and resource limits are supplied by trusted host code, never by the model.
Run parsers in an isolated worker for untrusted uploads; this package does not
grant filesystem/network authority or provide a security sandbox itself.
"""

from .api import inspect_file, read_archive_member, read_file, search_file
from .core import DEFAULT_LIMITS, FileError, FileLimits
from .export import export_file

__all__ = [
    "DEFAULT_LIMITS", "FileError", "FileLimits", "inspect_file", "read_file",
    "read_archive_member", "search_file", "export_file",
]
