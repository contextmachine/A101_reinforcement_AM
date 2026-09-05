from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from .config import Settings


class Database:
    """Lazy synchronous SQLAlchemy engine used by API threads and workers."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._engine: Engine | None = None

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(
                self.settings.database_url,
                pool_pre_ping=True,
                pool_size=max(1, int(self.settings.db_pool_size)),
                max_overflow=max(0, int(self.settings.db_max_overflow)),
                pool_recycle=max(0, int(self.settings.db_pool_recycle_seconds)),
                connect_args={"options": f"-c search_path={self.settings.postgres_schema},public"},
            )
        return self._engine

    def ping(self) -> bool:
        with self.engine.connect() as conn:
            return bool(conn.execute(text("SELECT 1")).scalar_one() == 1)

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            yield conn

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        with self.engine.connect() as conn:
            yield conn
