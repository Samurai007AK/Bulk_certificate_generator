"""Bulk certificate generator.

Every recipient in a submitted batch settles as ISSUED or REJECTED with a machine-readable
reason code. Nothing is silently skipped.
"""

from .api import create_app

__all__ = ["create_app"]
