import os
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import auth, db as db_module  # noqa: E402
from app.api import app  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.models import Role  # noqa: E402


@pytest.fixture()
def session_factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    monkeypatch.setattr("app.agent_tools.SessionLocal", factory)
    monkeypatch.setattr(db_module, "engine", engine)
    return factory


@pytest.fixture()
def db(session_factory):
    with session_factory() as s:
        yield s


@pytest.fixture()
def client(session_factory):
    def _get_db():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db
    with session_factory() as s:
        auth.create_user(s, "owner", "owner1234", Role.OWNER)
        auth.create_user(s, "staff", "staff1234", Role.EMPLOYEE)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def login(client, username, password):
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
