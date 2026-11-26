"""Deadline-bound asynchronous credential lookup over PostgreSQL or SQLite."""

import asyncio
import math
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import aiosqlite
import psycopg
from rpt_common.models.credentials import SUPPORTED_REVISIONS
from sqlalchemy.engine import make_url

from rpt_central_auth.errors import BackendUnavailable

API_VERSION = 2


def _connection_kwargs(url):
    params = {
        "dbname": url.database,
        "host": url.host,
        "password": url.password,
        "port": url.port,
        "user": url.username,
    }
    params.update(url.query)
    return {key: value for key, value in params.items() if value is not None}


@dataclass(repr=False)
class State:
    url: object = field(repr=False)
    sqlite: bool
    sqlite_uri: str | None = field(default=None, repr=False)
    ca_directory: object = field(default=None, repr=False)
    lookup_slots: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(16), repr=False
    )
    health_slots: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(1), repr=False
    )


async def initialize(*, options, timeout):
    url = make_url(options["url"])
    ca_directory = None
    size = options.get("pool_size", 16)
    if type(size) is not int or size <= 0:
        raise ValueError("invalid database concurrency limit")
    if url.get_backend_name() == "sqlite":
        if not url.database or url.database == ":memory:":
            raise ValueError("SQLite requires an existing file")
        path = Path(url.database).resolve(strict=True)
        if not path.is_file():
            raise ValueError("SQLite requires a database file")
        sqlite_uri = "file:" + quote(str(path), safe="/:") + "?mode=ro"
        return State(
            url,
            True,
            sqlite_uri,
            lookup_slots=asyncio.Semaphore(size),
            health_slots=asyncio.Semaphore(1),
        )
    if url.get_backend_name() != "postgresql":
        raise ValueError("supported database engines are PostgreSQL and SQLite")
    # This backend connects with psycopg directly, not through SQLAlchemy's
    # dialect adapter, so strip any SQLAlchemy driver suffix from the DSN.
    url = url.set(drivername="postgresql")
    if any(isinstance(value, tuple) for value in url.query.values()):
        raise ValueError("repeated PostgreSQL query parameters are unsupported")

    tls, verify = options.get("tls", True), options.get("verify_tls", True)
    if type(tls) is not bool or type(verify) is not bool:
        raise ValueError("database TLS options must be booleans")
    if not tls and options.get("development") is not True:
        raise ValueError("unencrypted database connections require development")
    configured_host = url.query.get("host", url.host)
    configured_hostaddr = url.query.get("hostaddr")
    endpoints = (configured_host, configured_hostaddr)
    has_network_host = any(host and not str(host).startswith("/") for host in endpoints)
    has_socket_host = any(host and str(host).startswith("/") for host in endpoints)
    if tls and has_socket_host:
        raise ValueError("TLS requires a PostgreSQL network host; Unix sockets are unsupported")
    if tls and not has_network_host:
        raise ValueError("TLS requires a PostgreSQL network host; Unix sockets are unsupported")

    query = dict(url.query)
    query.update(connect_timeout=str(max(1, math.ceil(min(timeout, 1)))), gssencmode="disable")
    query["options"] = (
        f"{query.get('options', '').strip()} -c default_transaction_read_only=on"
    ).strip()
    if not tls:
        query["sslmode"] = "disable"
    elif verify:
        query["sslmode"] = "verify-full"
        if options.get("ca"):
            query["sslrootcert"] = options["ca"]
    else:
        # Force encryption without consulting ambient CA settings.
        ca_directory = tempfile.TemporaryDirectory(prefix="rpt-no-verify-")
        query.update(
            sslmode="require",
            sslrootcert=str(Path(ca_directory.name) / "absent-ca.pem"),
        )
    return State(
        url.set(query=query),
        False,
        ca_directory=ca_directory,
        lookup_slots=asyncio.Semaphore(size),
        health_slots=asyncio.Semaphore(1),
    )


@asynccontextmanager
async def connection(state, timeout, *, health=False):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    connection = None
    abort_handle = None
    deadline_expired = False
    slots = state.health_slots if health else state.lookup_slots
    acquired = False
    try:
        await asyncio.wait_for(slots.acquire(), timeout)
        acquired = True
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError()
        if state.sqlite:
            connection = await asyncio.wait_for(
                aiosqlite.connect(
                    state.sqlite_uri, uri=True, timeout=max(0.001, min(remaining, 1))
                ),
                remaining,
            )
            await connection.execute("PRAGMA query_only=ON")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError()
            await connection.execute(f"PRAGMA busy_timeout={max(1, int(remaining * 1000))}")
            await connection.set_progress_handler(lambda: int(loop.time() >= deadline), 100)
        else:
            connection = await asyncio.wait_for(
                psycopg.AsyncConnection.connect(**_connection_kwargs(state.url)),
                remaining,
            )

            operation_task = asyncio.current_task()

            def cancel_operation():
                nonlocal deadline_expired
                if operation_task is not None and not operation_task.done():
                    deadline_expired = True
                    operation_task.cancel()

            abort_handle = loop.call_at(deadline, cancel_operation)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError()
            await connection.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(max(1, int(remaining * 1000))),),
            )
        yield connection
    except asyncio.CancelledError:
        if connection is not None:
            # Never return a connection with an interrupted query to reuse.
            await connection.close()
            connection = None
        if not state.sqlite and deadline_expired:
            raise TimeoutError() from None
        raise
    except (psycopg.Error, aiosqlite.Error):
        if connection is not None:
            await connection.close()
            connection = None
        raise BackendUnavailable() from None
    finally:
        if abort_handle is not None:
            abort_handle.cancel()
        try:
            if connection is not None:
                try:
                    if state.sqlite:
                        await connection.set_progress_handler(None, 0)
                finally:
                    await connection.close()
        finally:
            if acquired:
                slots.release()


async def lookup(node_id, *, state, timeout):
    async with connection(state, timeout) as conn:
        if state.sqlite:
            async with conn.execute(
                "SELECT password FROM node_credentials WHERE node_id = ? AND enabled = 1",
                (node_id,),
            ) as cursor:
                row = await cursor.fetchone()
        else:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT password FROM node_credentials WHERE node_id = %s AND enabled IS TRUE",
                    (node_id,),
                )
                row = await cursor.fetchone()
        return row[0] if row else None


async def check(*, state, timeout):
    deadline = asyncio.get_running_loop().time() + timeout
    async with connection(state, timeout, health=True) as conn:
        if state.sqlite:
            async with conn.execute("SELECT version_num FROM alembic_version") as cursor:
                revisions = await cursor.fetchall()
            if len(revisions) != 1 or revisions[0][0] not in SUPPORTED_REVISIONS:
                raise BackendUnavailable()
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError()
            async with conn.execute(
                "SELECT node_id, password, enabled FROM node_credentials LIMIT 0"
            ):
                pass
        else:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT version_num FROM alembic_version")
                revisions = await cursor.fetchall()
                if len(revisions) != 1 or revisions[0][0] not in SUPPORTED_REVISIONS:
                    raise BackendUnavailable()
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError()
                await cursor.execute(
                    "SELECT node_id, password, enabled FROM node_credentials LIMIT 0"
                )


async def close(*, state, timeout):
    if state.ca_directory is not None:
        state.ca_directory.cleanup()
