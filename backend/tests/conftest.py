"""Test fixtures: a throwaway SQLite database per test, and an API client.

Each test gets a brand-new empty database file, so tests can't affect each
other or ever touch the real cirqle.db.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Must be set before app.db is imported — it reads them at import time.
os.environ["CIRQLE_RATE_LIMIT"] = "off"
os.environ["CIRQLE_EMAIL_MODE"] = "console"
os.environ.pop("DATABASE_URL", None)


@pytest.fixture()
def session(tmp_path, monkeypatch):
    from sqlmodel import Session, SQLModel, create_engine

    import app.db as db
    engine = create_engine(f"sqlite:///{tmp_path/'test.db'}",
                           connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def client(session):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import app

    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
