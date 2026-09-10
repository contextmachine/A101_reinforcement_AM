from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from threading import Lock
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError

from .config import Settings


logger = logging.getLogger(__name__)

# Повторяем только УСТАНОВЛЕНИЕ соединения.
# SQL-запросы и транзакции автоматически не повторяются, чтобы не получить
# двойные INSERT/UPDATE при неясном результате предыдущей попытки.
_CONNECT_RETRY_DELAYS_SECONDS: tuple[float, ...] = (
    0.0,
    0.5,
    1.0,
    2.0,
    4.0,
    6.0,
    8.0,
    10.0,
)


class Database:
    """Lazy synchronous SQLAlchemy engine used by API threads and workers."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._engine: Engine | None = None
        self._engine_lock = Lock()

    @property
    def engine(self) -> Engine:
        # API вызывает store из threadpool, поэтому защищаем ленивое создание
        # Engine от одновременной инициализации несколькими потоками.
        if self._engine is None:
            with self._engine_lock:
                if self._engine is None:
                    self._engine = create_engine(
                        self.settings.database_url,
                        pool_pre_ping=True,
                        pool_size=max(
                            1,
                            int(self.settings.db_pool_size),
                        ),
                        max_overflow=max(
                            0,
                            int(self.settings.db_max_overflow),
                        ),
                        pool_recycle=max(
                            0,
                            int(
                                self.settings.db_pool_recycle_seconds
                            ),
                        ),
                        connect_args={
                            "options": (
                                "-c search_path="
                                f"{self.settings.postgres_schema},public"
                            ),
                            "connect_timeout": 3,
                        },
                    )

        return self._engine

    def _connect_with_retry(self) -> Connection:
        """Получить соединение, пережидая короткий restart/recovery PostgreSQL.

        Retry выполняется только пока соединение ещё НЕ установлено.
        После того как Connection получен и SQL начал выполняться,
        автоматического повтора операции здесь нет.
        """
        last_error: OperationalError | None = None

        for attempt, delay in enumerate(
            _CONNECT_RETRY_DELAYS_SECONDS,
            start=1,
        ):
            if delay > 0:
                time.sleep(delay)

            try:
                return self.engine.connect()

            except OperationalError as exc:
                last_error = exc

                if attempt < len(_CONNECT_RETRY_DELAYS_SECONDS):
                    next_delay = _CONNECT_RETRY_DELAYS_SECONDS[
                        attempt
                    ]

                    logger.warning(
                        "PostgreSQL connection unavailable "
                        "(attempt %s/%s); retry in %.1fs",
                        attempt,
                        len(_CONNECT_RETRY_DELAYS_SECONDS),
                        next_delay,
                    )

        if last_error is None:
            raise RuntimeError(
                "PostgreSQL connection retry loop finished "
                "without a connection or an error"
            )

        raise last_error

    def ping(self) -> bool:
        """Проверить доступность PostgreSQL без выброса OperationalError."""
        try:
            with self.connect() as conn:
                return bool(
                    conn.execute(text("SELECT 1")).scalar_one() == 1
                )
        except OperationalError:
            return False

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        """Открыть транзакцию.

        Повторяется только получение соединения. Если исключение возникает
        уже внутри транзакции, SQLAlchemy выполняет rollback, а исключение
        передаётся вызывающему коду без автоматического повторения SQL.
        """
        conn = self._connect_with_retry()

        try:
            with conn.begin():
                yield conn
        finally:
            conn.close()

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        """Получить соединение без явной транзакции приложения."""
        conn = self._connect_with_retry()

        try:
            yield conn
        finally:
            conn.close()

    def dispose(self) -> None:
        """Закрыть connection pool при штатном завершении процесса."""
        if self._engine is None:
            return

        with self._engine_lock:
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None