"""Run configuration. Anything the app needs to find on disk lives here."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    # SQLAlchemy URL. SQLite by default so a clone runs with no setup; Postgres by URL
    # (install a driver, e.g. psycopg, and set CERTGEN_DATABASE_URL).
    database_url: str = field(
        default_factory=lambda: os.environ.get("CERTGEN_DATABASE_URL", "sqlite:///certgen.db")
    )
    storage_dir: str = field(
        default_factory=lambda: os.environ.get("CERTGEN_STORAGE_DIR", "certificates")
    )
    template_id: str = "course-completion"
    # How long the worker sleeps when it finds no PENDING rows.
    poll_interval: float = 0.02
    # How many PENDING rows one pass claims.
    batch_size: int = 256
