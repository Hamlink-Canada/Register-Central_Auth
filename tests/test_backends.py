import asyncio
import sys
from types import ModuleType

import pytest
from rpt_common.migrate import upgrade
from sqlalchemy import create_engine, text

from rpt_central_auth.backends import csv, sqlalchemy
from rpt_central_auth.errors import BackendUnavailable, InvalidCredential
from rpt_central_auth.module import Backend


@pytest.fixture
def database(tmp_path):
    url = f"sqlite:///{tmp_path / 'credentials.db'}"
    upgrade(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO node_credentials VALUES (1234, 'generated-test-password', 1)")
        )
        conn.execute(text("INSERT INTO node_credentials VALUES (1235, 'disabled-password', 0)"))
    yield url, engine
    engine.dispose()


@pytest.mark.parametrize("backend", ["csv", "sqlalchemy", "custom"])
def test_module_contract(backend, tmp_path, database):
    file = tmp_path / "credentials.csv"
    file.write_text("node_id,password\n1234,generated-test-password\n")
    module = {
        "csv": "rpt_central_auth.backends.csv",
        "sqlalchemy": "rpt_central_auth.backends.sqlalchemy",
        "custom": "tests.backend_fixture",
    }[backend]
    options = {"file": str(file)} if backend == "csv" else {"url": database[0]}

    async def scenario():
        provider = Backend(module, options, workers=2)
        await provider.start(2)
        try:
            assert await provider.lookup(1234, 1) == "generated-test-password"
            if backend != "custom":
                assert await provider.lookup(999, 1) is None
            await provider.check(1)
        finally:
            assert await provider.close(1)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "contents",
    [
        "1,password\n",
        "node_id,password\n01,a\n",
        "node_id,password\n1,a\n1,b\n",
        "node_id,password\n1,a b\n",
        "node_id,password\n1,a,x\n",
        "node_id,password\n9223372036854775808,a\n",
    ],
)
def test_csv_rejects_bad_data(tmp_path, contents):
    file = tmp_path / "credentials.csv"
    file.write_text(contents)
    with pytest.raises(ValueError):
        asyncio.run(csv.initialize(options={"file": str(file)}, timeout=1))


def test_csv_initialization_offloads_blocking_read(monkeypatch, tmp_path):
    file = tmp_path / "credentials.csv"
    file.write_text("node_id,password\n1234,generated-test-password\n")
    called = []

    async def tracked(function, *args, **kwargs):
        called.append(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(csv.asyncio, "to_thread", tracked)
    state = asyncio.run(csv.initialize(options={"file": str(file)}, timeout=1))
    assert state[1234] == "generated-test-password"
    assert called


def test_sql_readonly_fresh_lookups_and_schema(database):
    url, writer = database

    async def scenario():
        state = await sqlalchemy.initialize(options={"url": url}, timeout=1)
        try:
            await sqlalchemy.check(state=state, timeout=1)
            assert await sqlalchemy.lookup(1235, state=state, timeout=1) is None
            with pytest.raises(BackendUnavailable):
                async with sqlalchemy.connection(state, 1) as conn:
                    await conn.execute("UPDATE node_credentials SET password='attempted-write'")
            with writer.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE node_credentials SET password='rotated-password' WHERE node_id=1234"
                    )
                )
            assert await sqlalchemy.lookup(1234, state=state, timeout=1) == "rotated-password"
            with writer.begin() as conn:
                conn.execute(text("UPDATE alembic_version SET version_num='unknown-future'"))
            with pytest.raises(BackendUnavailable):
                await sqlalchemy.check(state=state, timeout=1)
        finally:
            await sqlalchemy.close(state=state, timeout=1)

    asyncio.run(scenario())


def test_missing_sqlite_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        asyncio.run(sqlalchemy.initialize(options={"url": f"sqlite:///{path}"}, timeout=1))
    assert not path.exists()


def test_loader_rejects_bad_contract(monkeypatch):
    module = ModuleType("bad_backend")
    monkeypatch.setitem(sys.modules, "bad_backend", module)
    with pytest.raises(ValueError):
        Backend("bad_backend", {}, workers=1)
    module.API_VERSION = 1
    with pytest.raises(ValueError):
        Backend("bad_backend", {}, workers=1)


def test_loader_rejects_sync_and_async_generator_hooks(monkeypatch):
    module = ModuleType("sync_backend")
    module.API_VERSION = 2
    module.initialize = lambda *, options, timeout: None
    module.lookup = lambda node_id, *, state, timeout: None
    module.check = lambda *, state, timeout: None
    module.close = lambda *, state, timeout: None
    monkeypatch.setitem(sys.modules, "sync_backend", module)
    with pytest.raises(ValueError, match="async coroutines"):
        Backend("sync_backend", {}, workers=1)


def test_postgres_tls_rejects_unix_socket_urls():
    async def scenario():
        for url in (
            "postgresql:///credentials",
            "postgresql:///credentials?host=%2Fvar%2Frun%2Fpostgresql",
            "postgresql://db.example/credentials?host=%2Ftmp",
        ):
            with pytest.raises(ValueError, match="Unix sockets"):
                await sqlalchemy.initialize(options={"url": url, "tls": True}, timeout=1)

    asyncio.run(scenario())


def test_postgres_connection_options_are_url_strings():
    async def scenario():
        state = await sqlalchemy.initialize(
            options={"url": "postgresql+psycopg://user:secret@db.example/credentials"},
            timeout=2,
        )
        try:
            assert state.url.drivername == "postgresql"
            assert state.url.query["connect_timeout"] == "1"
            assert all(isinstance(value, str) for value in state.url.query.values())
            assert sqlalchemy._connection_kwargs(state.url)["options"] == (
                "-c default_transaction_read_only=on"
            )
        finally:
            await sqlalchemy.close(state=state, timeout=1)

    asyncio.run(scenario())


def test_postgres_preserves_startup_options_and_enforces_readonly():
    async def scenario():
        state = await sqlalchemy.initialize(
            options={
                "url": "postgresql://user:secret@db.example/credentials?options=-c%20search_path%3Dcredential_schema"
            },
            timeout=2,
        )
        assert sqlalchemy._connection_kwargs(state.url)["options"] == (
            "-c search_path=credential_schema -c default_transaction_read_only=on"
        )

    asyncio.run(scenario())


def test_postgres_repeated_query_parameters_are_rejected():
    async def scenario():
        with pytest.raises(ValueError, match="repeated PostgreSQL query parameters"):
            await sqlalchemy.initialize(
                options={
                    "url": "postgresql://user:secret@db.example/credentials?host=db1&host=db2"
                },
                timeout=2,
            )

    asyncio.run(scenario())


def test_cancelled_postgres_operation_closes_connection(monkeypatch):
    class Connection:
        closed = False

        async def execute(self, statement, params=None):
            if statement == "SELECT slow":
                await asyncio.sleep(10)

        async def close(self):
            self.closed = True

    async def scenario():
        connection = Connection()

        async def connect(*args, **kwargs):
            return connection

        monkeypatch.setattr(sqlalchemy.psycopg.AsyncConnection, "connect", connect)
        state = sqlalchemy.State(sqlalchemy.make_url("postgresql://db.example/credentials"), False)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.02):
                async with sqlalchemy.connection(state, 1) as conn:
                    await conn.execute("SELECT slow")
        assert connection.closed

    asyncio.run(scenario())


def test_sqlite_busy_timeout_uses_remaining_deadline(monkeypatch):
    busy_timeouts = []

    class Connection:
        async def execute(self, statement, params=None):
            if statement.startswith("PRAGMA busy_timeout="):
                busy_timeouts.append(int(statement.partition("=")[2]))

        async def set_progress_handler(self, callback, instructions):
            pass

        async def close(self):
            pass

    async def scenario():
        async def connect(*args, **kwargs):
            return Connection()

        monkeypatch.setattr(sqlalchemy.aiosqlite, "connect", connect)
        slots = asyncio.Semaphore(0)
        state = sqlalchemy.State(
            sqlalchemy.make_url("sqlite:///unused.db"),
            True,
            "file:unused?mode=ro",
            lookup_slots=slots,
        )
        task = asyncio.create_task(_hold_connection(state))
        await asyncio.sleep(0.05)
        slots.release()
        await task
        assert len(busy_timeouts) == 1
        assert 0 < busy_timeouts[0] < 120

    async def _hold_connection(state):
        async with sqlalchemy.connection(state, 0.15):
            pass

    asyncio.run(scenario())


def test_postgres_deadline_cancels_owner_before_closing_connection(monkeypatch):
    class Transport:
        finished = False

        def finish(self):
            self.finished = True

    class Connection:
        def __init__(self):
            self.pgconn = Transport()
            self.closed = False
            self.operation_cancelled = False

        async def execute(self, statement, params=None):
            if statement == "SELECT slow":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.operation_cancelled = True
                    raise

        async def close(self):
            assert self.operation_cancelled
            self.closed = True

    async def scenario():
        connection = Connection()

        async def connect(*args, **kwargs):
            return connection

        monkeypatch.setattr(sqlalchemy.psycopg.AsyncConnection, "connect", connect)

        async def operation():
            async with sqlalchemy.connection(
                sqlalchemy.State(sqlalchemy.make_url("postgresql://db.example/credentials"), False),
                0.02,
            ) as conn:
                await conn.execute("SELECT slow")

        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            await operation()
        assert asyncio.get_running_loop().time() - started < 0.2
        assert connection.closed
        assert not connection.pgconn.finished

    asyncio.run(scenario())


def test_sqlite_cleanup_cancellation_releases_connection_permit(monkeypatch):
    cleanup_started = asyncio.Event()

    class Connection:
        closed = False

        async def execute(self, *args, **kwargs):
            pass

        async def set_progress_handler(self, callback, instructions):
            if callback is None:
                cleanup_started.set()
                await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    async def scenario():
        connection = Connection()

        async def connect(*args, **kwargs):
            return connection

        monkeypatch.setattr(sqlalchemy.aiosqlite, "connect", connect)
        state = sqlalchemy.State(
            sqlalchemy.make_url("sqlite:///unused.db"),
            True,
            "file:unused?mode=ro",
            lookup_slots=asyncio.Semaphore(1),
        )

        async def operation():
            async with sqlalchemy.connection(state, 1):
                pass

        task = asyncio.create_task(operation())
        await cleanup_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert connection.closed
        assert state.lookup_slots._value == 1

    asyncio.run(scenario())


def test_async_timeout_cancels_backend_and_releases_work_slot():
    async def scenario():
        provider = Backend("tests.backend_fixture", {}, workers=1)
        await provider.start(1)
        provider.state.block = True
        try:
            with pytest.raises(TimeoutError):
                await provider.lookup(1234, 0.02)
            assert provider.lookups.active == 0
            assert provider.lookups.semaphore._value == 1
            provider.state.block = False
            assert await provider.lookup(1234, 0.1) == "generated-test-password"
        finally:
            provider.state.release.set()
            assert await provider.close(1)

    asyncio.run(scenario())


def test_corrupt_backend_result():
    async def scenario():
        provider = Backend("tests.backend_fixture", {}, workers=1)
        await provider.start(1)
        try:
            provider.state.value = "invalid password"
            with pytest.raises(InvalidCredential):
                await provider.lookup(1234, 1)
        finally:
            await provider.close(1)

    asyncio.run(scenario())


def test_late_initialization_is_closed(monkeypatch):
    from tests import backend_fixture

    original = backend_fixture.initialize

    async def initialize(*, options, timeout):
        await asyncio.sleep(0.04)
        return await original(options=options, timeout=timeout)

    monkeypatch.setattr(backend_fixture, "initialize", initialize)

    async def scenario():
        provider = Backend("tests.backend_fixture", {}, workers=1)
        with pytest.raises(TimeoutError):
            await provider.start(0.005)
        assert await provider.close(0.5)
        assert not provider.initialized

    asyncio.run(scenario())


def test_sql_health_has_independent_connection_capacity(database):
    async def scenario():
        state = await sqlalchemy.initialize(options={"url": database[0], "pool_size": 1}, timeout=1)
        try:
            async with sqlalchemy.connection(state, 1):
                await sqlalchemy.check(state=state, timeout=1)
        finally:
            await sqlalchemy.close(state=state, timeout=1)

    asyncio.run(scenario())
