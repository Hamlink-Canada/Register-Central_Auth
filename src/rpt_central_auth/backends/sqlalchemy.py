"""Read-only shared-model lookup. Migration execution belongs to rpt_common."""

import math
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from rpt_common.models.credentials import SUPPORTED_REVISIONS, NodeCredential
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from rpt_central_auth.errors import BackendUnavailable

API_VERSION = 1


@dataclass(repr=False)
class State:
    engine: object
    sqlite: bool
    ca_directory: object = field(default=None, repr=False)
    check_engine: object = field(default=None, repr=False)


def initialize(*, options, timeout):
    url = make_url(options["url"])
    ca_directory = None
    size = options.get("pool_size", 16)
    if type(size) is not int or size <= 0:
        raise ValueError("invalid database pool size")
    if url.get_backend_name() == "sqlite":
        if not url.database or url.database == ":memory:":
            raise ValueError("SQLite requires an existing file")
        path = Path(url.database).resolve(strict=True)
        if not path.is_file():
            raise ValueError("SQLite requires a database file")
        uri = "file:" + quote(str(path), safe="/:")
        url = url.set(
            drivername="sqlite+pysqlite", database=uri, query={"mode": "ro", "uri": "true"}
        )
        args = {"check_same_thread": False, "timeout": min(timeout, 1)}
        sqlite = True
    elif url.get_backend_name() == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
        tls, verify = options.get("tls", True), options.get("verify_tls", True)
        if type(tls) is not bool or type(verify) is not bool:
            raise ValueError("database TLS options must be booleans")
        if not tls and options.get("development") is not True:
            raise ValueError("unencrypted database connections require development")
        args = {"connect_timeout": max(1, math.ceil(min(timeout, 1))), "gssencmode": "disable"}
        if not tls:
            args["sslmode"] = "disable"
        elif verify:
            args["sslmode"] = "verify-full"
            if options.get("ca"):
                args["sslrootcert"] = options["ca"]
        else:
            # libpq's require mode otherwise verifies a default/URL/env root CA if it exists.
            # Keep a private empty directory for the lifetime of all pooled connections.
            ca_directory = tempfile.TemporaryDirectory(prefix="rpt-no-verify-")
            args.update(
                sslmode="require", sslrootcert=str(Path(ca_directory.name) / "absent-ca.pem")
            )
        sqlite = False
    else:
        raise ValueError("supported database engines are PostgreSQL and SQLite")
    try:
        engine = create_engine(
            url,
            connect_args=args,
            pool_size=size,
            max_overflow=0,
            pool_timeout=0.1,
            hide_parameters=True,
        )
        check_engine = create_engine(
            url, connect_args=args, poolclass=NullPool, hide_parameters=True
        )
        if sqlite:

            def readonly(connection, record):
                connection.execute("PRAGMA query_only=ON")
        else:

            def readonly(connection, record):
                previous = connection.autocommit
                connection.autocommit = True
                try:
                    connection.execute("SET default_transaction_read_only = on")
                finally:
                    connection.autocommit = previous

        for candidate in (engine, check_engine):
            event.listen(candidate, "connect", readonly)
        return State(engine, sqlite, ca_directory, check_engine)
    except BaseException:
        if ca_directory:
            ca_directory.cleanup()
        raise


@contextmanager
def connection(state, timeout, *, health=False):
    deadline = time.monotonic() + timeout
    try:
        with (state.check_engine if health else state.engine).connect() as conn:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackendUnavailable()
            raw = None
            if state.sqlite:
                raw = conn.connection.driver_connection
                raw.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
                conn.exec_driver_sql(f"PRAGMA busy_timeout={max(1, int(remaining * 1000))}")
            else:
                conn.execute(
                    text("SELECT set_config('statement_timeout', :value, true)"),
                    {"value": str(max(1, int(remaining * 1000)))},
                )
            try:
                yield conn
            finally:
                if raw is not None:
                    raw.set_progress_handler(None, 0)
    except SQLAlchemyError:
        raise BackendUnavailable() from None


def lookup(node_id, *, state, timeout):
    with connection(state, timeout) as conn, Session(bind=conn) as session:
        return session.scalar(
            select(NodeCredential.password).where(
                NodeCredential.node_id == node_id, NodeCredential.enabled.is_(True)
            )
        )


def check(*, state, timeout):
    with connection(state, timeout, health=True) as conn:
        revisions = conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        if len(revisions) != 1 or revisions[0] not in SUPPORTED_REVISIONS:
            raise BackendUnavailable()
        conn.execute(
            select(NodeCredential.node_id, NodeCredential.password, NodeCredential.enabled).limit(0)
        )


def close(*, state, timeout):
    state.engine.dispose()
    state.check_engine.dispose()
    if state.ca_directory is not None:
        state.ca_directory.cleanup()
