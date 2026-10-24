import os
import sys
import tempfile
from pathlib import Path

# Settings are read at import time, so they are set before any bot module is imported.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="family-test-")
os.environ["BOT_TOKEN"] = "123:test"
os.environ.pop("YANDEX_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    (db.config.data_dir / "family.sqlite3").unlink(missing_ok=True)
    db.init()
    yield


@pytest.fixture
def couple():
    """Маша (1) and Дима (2) in one couple. Returns the couple id."""
    db.save_user(1, "Маша")
    code = db.invite_code(1)
    db.save_user(2, "Дима")
    assert db.join(2, code) == "ok"
    return db.user(1)["couple_id"]
