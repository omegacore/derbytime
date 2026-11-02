import ast
import os
from pathlib import Path

from sqlmodel import Session, func, select

import derbytime.core
from derbytime.cli import main
from derbytime.config import load_settings
from derbytime.db import make_engine
from derbytime.models import Entry, Event, JoinToken


def test_core_has_no_web_or_db_imports():
    banned = {"fastapi", "starlette", "sqlmodel", "sqlalchemy", "jinja2", "derbytime.models", "derbytime.db"}
    core_dir = Path(derbytime.core.__file__).parent
    for path in core_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
                if node.level and node.module in ("models", "db", "app", "services", "routes"):
                    names = [f"derbytime.{node.module}"]
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

    copy = tmp_path / "copy.db"
    assert main(["--config", str(cfg), "backup", str(copy)]) == 0
    assert main(["--config", str(cfg), "backup", str(copy)]) == 1  # won't overwrite
    with Session(make_engine(copy)) as s:
        assert s.exec(select(func.count()).select_from(Entry)).one() == 40


def test_config_overrides(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[server]\nport = 9100\ndb_path = "x/race.db"\n[defaults.derby]\nlane_count = 6\n', encoding="utf-8")
    s = load_settings(cfg)
    assert s.port == 9100
    assert s.db_path == tmp_path / "x" / "race.db"
    assert s.type_defaults("derby").lane_count == 6
    assert s.type_defaults("derby").timing_source == "simulator"  # untouched default


def test_python_dash_m_and_version():
    import subprocess
    import sys

    src = str(Path(derbytime.core.__file__).parents[2])
    env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    out = subprocess.run([sys.executable, "-m", "derbytime", "--version"], capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("derbytime ")


def test_package_data_covers_every_non_python_file():
    """A data file outside the package-data globs works from a checkout but is
    missing from the wheel, so a pip-installed server would 404 or crash."""
    import tomllib

    root = Path(derbytime.core.__file__).parents[3]
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():  # installed from a wheel: nothing to check
        return
    globs = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]["derbytime"]
    prefixes = {g.split("**")[0] for g in globs}
    pkg = root / "src" / "derbytime"
    for path in pkg.rglob("*"):
        if path.is_dir() or path.suffix in (".py", ".pyc") or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(pkg).as_posix()
        assert any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in prefixes), f"{rel} is not in package-data"
