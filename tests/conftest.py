import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from certgen.api import create_app  # noqa: E402
from certgen.config import Settings  # noqa: E402


@pytest.fixture
def env(tmp_path):
    """Open an app over a throwaway database.

        with env() as (app, client):            # worker thread running
        with env(start_worker=False) as (_, c): # deterministic, drive the worker by hand
        with env(name="same.sqlite3") as ...   # reopen the same database
    """

    @contextmanager
    def _env(*, name: str = "db.sqlite3", start_worker: bool = True):
        settings = Settings(
            database_url=f"sqlite:///{(tmp_path / name).as_posix()}",
            storage_dir=str(tmp_path / "certs"),
            poll_interval=0.01,
        )
        app = create_app(settings, start_worker=start_worker)
        with TestClient(app) as client:
            yield app, client

    _env.db_path = tmp_path / "db.sqlite3"
    return _env
