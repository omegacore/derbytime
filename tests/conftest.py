import pytest
from sqlmodel import Session

from derbytime.config import load_settings
from derbytime.db import init_db, make_engine


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no stray config.toml
    s = load_settings()
    s.db_path = tmp_path / "test.db"
    s.data_dir = tmp_path / "data"
    s.timers["simulator"]["delay_seconds"] = 0.01
    s.timers["simulator"]["dnf_chance"] = 0
    s.timers["simulator"]["outlier_chance"] = 0
    return s


@pytest.fixture
def engine(settings):
    eng = make_engine(settings.db_path)
    init_db(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine, expire_on_commit=False) as s:
        yield s
