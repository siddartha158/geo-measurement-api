import os
import tempfile

# Must be set before the app is imported so tests use a throw-away database.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:  # context manager runs the startup (table creation)
        yield c
