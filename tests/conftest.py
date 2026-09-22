import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
from lab.store import Store  # noqa: E402


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "test.db")
    yield db
    db.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = Store(tmp_path / "api.db")
    monkeypatch.setattr(app_module, "store", db)
    with TestClient(app_module.app) as test_client:
        yield test_client
    db.close()
