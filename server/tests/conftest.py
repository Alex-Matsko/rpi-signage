import os
import tempfile

# Окружение должно быть задано до импорта приложения
_tmp = tempfile.mkdtemp(prefix="signage-test-")
os.environ["SIGNAGE_DATA_DIR"] = _tmp
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "test-password-123"

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def admin(client):
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "test-password-123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return client
