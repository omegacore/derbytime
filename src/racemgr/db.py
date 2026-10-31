from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import event as sa_event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from . import models  # noqa: F401  (registers tables)


def make_engine(db_path: Path | str) -> Engine:
    url = "sqlite://" if str(db_path) == ":memory:" else f"sqlite:///{Path(db_path)}"
    kwargs: dict = {"connect_args": {"check_same_thread": False}}
    if url == "sqlite://":
        from sqlalchemy.pool import StaticPool

        kwargs["poolclass"] = StaticPool
    else:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # SQLAlchemy's default pool (5 + 10 overflow) is smaller than the 40
        # worker threads serving sync routes. RaceService checks connections
        # out on the event loop, and request sessions are only returned via
        # the event loop, so an exhausted pool froze the whole server for
        # pool_timeout. SQLite connections are cheap: keep the pool larger
        # than the threadpool (each request may hold two) and fail fast.
        kwargs.update(pool_size=20, max_overflow=100, pool_timeout=10)
    engine = create_engine(url, **kwargs)

    @sa_event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _rec):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        if url != "sqlite://":
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


def init_db(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)
    add_missing_columns(engine)


def add_missing_columns(engine: Engine) -> list[str]:
    """Tiny forward-only migration: add nullable columns that newer versions
    introduced to tables created by older ones. (Full migrations come later.)"""
    added = []
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            have = {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")')}
            for column in table.columns:
                if column.name in have or not column.nullable:
                    continue
                coltype = column.type.compile(dialect=engine.dialect)
                conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {coltype}')
                added.append(f"{table.name}.{column.name}")
    return added


def session_scope(engine: Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as session:
        yield session
