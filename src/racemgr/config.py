"""Load config.toml into typed settings.

Defaults live in config.example.toml (shipped with the package); a user file
overrides any subset of keys.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXAMPLE_PATH = Path(__file__).with_name("config.example.toml")


@dataclass
class TypeDefaults:
    lane_count: int
    timing_source: str
    scoring_mode: str
    min_time: float
    max_time: float
    dnf_time: float
    finalists_per_group: int
    passes_per_den: int = 2
    # projected end time: assumed seconds per heat (the race itself plus
    # staging/loading overhead) until a track has measured its own pace
    heat_seconds: float = 5.0
    heat_overhead_seconds: float = 40.0
    rerun_reasons: list[str] = field(default_factory=list)


@dataclass
class Settings:
    host: str
    port: int
    db_path: Path
    data_dir: Path
    public_base_url: str
    join_token_minutes: int
    admin_link_minutes: int
    device_cookie_days: int
    defaults: dict[str, TypeDefaults]
    timers: dict[str, dict[str, Any]]
    config_path: Path | None = None
    # set at runtime by `serve` so URLs printed/encoded use the right scheme
    use_https: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def type_defaults(self, event_type: str) -> TypeDefaults:
        return self.defaults[event_type]

    def timer_settings(self, kind: str) -> dict[str, Any]:
        return dict(self.timers.get(kind, {}))


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def find_config_path(explicit: str | os.PathLike | None = None) -> Path | None:
    if explicit:
        return Path(explicit)
    env = os.environ.get("RACEMGR_CONFIG")
    if env:
        return Path(env)
    local = Path.cwd() / "config.toml"
    return local if local.exists() else None


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    with EXAMPLE_PATH.open("rb") as f:
        data = tomllib.load(f)
    cfg_path = find_config_path(path)
    if cfg_path is not None:
        if not cfg_path.exists():
            raise FileNotFoundError(f"Config file not found: {cfg_path}")
        with cfg_path.open("rb") as f:
            data = _merge(data, tomllib.load(f))
    base_dir = cfg_path.parent.resolve() if cfg_path else Path.cwd()

    server = data["server"]
    auth = data["auth"]

    def resolve(p: str) -> Path:
        pp = Path(p)
        return pp if pp.is_absolute() else base_dir / pp

    defaults = {name: TypeDefaults(**vals) for name, vals in data["defaults"].items()}
    return Settings(
        host=server["host"],
        port=int(server["port"]),
        db_path=resolve(server["db_path"]),
        data_dir=resolve(server["data_dir"]),
        public_base_url=server.get("public_base_url", "").rstrip("/"),
        join_token_minutes=int(auth["join_token_minutes"]),
        admin_link_minutes=int(auth["admin_link_minutes"]),
        device_cookie_days=int(auth["device_cookie_days"]),
        defaults=defaults,
        timers=data.get("timers", {}),
        config_path=cfg_path,
        raw=data,
    )
