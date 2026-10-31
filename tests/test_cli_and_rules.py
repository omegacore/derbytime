import ast
from pathlib import Path

from sqlmodel import Session, func, select

import racemgr.core
from racemgr.cli import main
from racemgr.config import load_settings
from racemgr.db import make_engine
from racemgr.models import Entry, Event, JoinToken


def test_core_has_no_web_or_db_imports():
    banned = {"fastapi", "starlette", "sqlmodel", "sqlalchemy", "jinja2", "racemgr.models", "racemgr.db"}
    core_dir = Path(racemgr.core.__file__).parent
    for path in core_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
                if node.level and node.module in ("models", "db", "app", "services", "routes"):
                    names = [f"racemgr.{node.module}"]
            for n in names:
                assert not any(n == b or n.startswith(b + ".") for b in banned), f"{path.name} imports {n}"


def test_cli_init_seed_admin_url(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init-config"]) == 0
    cfg = tmp_path / "config.toml"
    assert cfg.exists()
    assert main(["--config", str(cfg), "init-db"]) == 0
    assert (tmp_path / "racemgr.db").exists()
    assert main(["--config", str(cfg), "seed-demo"]) == 0
    assert main(["--config", str(cfg), "seed-demo"]) == 1  # refuses duplicates
    assert main(["--config", str(cfg), "admin-url", "--port", "9000"]) == 0
    out = capsys.readouterr().out
    assert ":9000/join/" in out

    engine = make_engine(load_settings(cfg).db_path)
    with Session(engine) as s:
        assert len(s.exec(select(Event)).all()) == 2
        assert s.exec(select(func.count()).select_from(Entry)).one() == 40
        assert s.exec(select(JoinToken)).first().role.value == "admin"


def test_config_overrides(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[server]\nport = 9100\ndb_path = "x/race.db"\n[defaults.derby]\nlane_count = 6\n', encoding="utf-8")
    s = load_settings(cfg)
    assert s.port == 9100
    assert s.db_path == tmp_path / "x" / "race.db"
    assert s.type_defaults("derby").lane_count == 6
    assert s.type_defaults("derby").timing_source == "simulator"  # untouched default
