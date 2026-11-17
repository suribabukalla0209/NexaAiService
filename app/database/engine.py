from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
import time
from typing import Iterator

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import Connection, make_url

_migration_lock = RLock()


class Database:
    """SQLAlchemy database boundary with SQLite and PostgreSQL transaction semantics."""

    def __init__(self, database_url: str | Path) -> None:
        if isinstance(database_url, Path):
            database_url = f"sqlite:///{database_url.expanduser().resolve().as_posix()}"
        if database_url.startswith("postgresql://"):
            database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
        self.url = database_url
        parsed_url = make_url(database_url)
        if parsed_url.drivername.startswith("sqlite"):
            self.engine: Engine = create_engine(
                parsed_url,
                connect_args={"check_same_thread": False, "timeout": 10},
                pool_pre_ping=True,
            )
            event.listen(self.engine, "connect", self._configure_sqlite_connection)
        elif parsed_url.drivername == "postgresql+psycopg":
            self.engine = create_engine(
                parsed_url,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=10,
                pool_recycle=1800,
            )
        else:
            raise ValueError("Database URL must use SQLite or postgresql+psycopg")

    @property
    def is_sqlite(self) -> bool:
        return self.engine.dialect.name == "sqlite"

    @property
    def database_file(self) -> Path:
        if not self.is_sqlite:
            raise RuntimeError("A PostgreSQL database has no local database file")
        database = make_url(self.url).database
        if database in (None, ":memory:"):
            raise RuntimeError("SQLite database file path is unavailable")
        return Path(database)

    @contextmanager
    def read(self) -> Iterator[Connection]:
        with self.engine.connect() as connection:
            yield connection

    @contextmanager
    def write(self) -> Iterator[Connection]:
        connection = self.engine.connect()
        try:
            if self.is_sqlite:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection.begin()
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        # Alembic uses process-global proxies; serialize concurrent startup in this process.
        with _migration_lock:
            alembic_ini = Path(__file__).resolve().parents[2] / "alembic.ini"
            config = Config(str(alembic_ini))
            config.set_main_option("script_location", str(alembic_ini.parent / "migrations"))
            config.set_main_option("prepend_sys_path", str(alembic_ini.parent))
            config.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
            with self.engine.connect() as connection:
                if self.is_sqlite:
                    self._enable_sqlite_wal(connection)
                config.attributes["connection"] = connection
                if self.engine.dialect.name == "postgresql":
                    connection.exec_driver_sql("SELECT pg_advisory_lock(731942, 1)")
                    connection.commit()
                    try:
                        command.upgrade(config, "head")
                    except Exception:
                        connection.rollback()
                        raise
                    finally:
                        if connection.in_transaction():
                            connection.rollback()
                        connection.exec_driver_sql("SELECT pg_advisory_unlock(731942, 1)")
                        connection.commit()
                else:
                    command.upgrade(config, "head")

    def dispose(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=10000")
        finally:
            cursor.close()

    @staticmethod
    def _enable_sqlite_wal(connection: Connection) -> None:
        deadline = time.monotonic() + 10
        while True:
            try:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
                connection.commit()
                return
            except Exception as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
