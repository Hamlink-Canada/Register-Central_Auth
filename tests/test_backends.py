import asyncio
import sys
from types import ModuleType

import pytest
from rpt_common.migrate import upgrade
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from rpt_central_auth.backends import csv, sqlalchemy
from rpt_central_auth.errors import BackendUnavailable, CapacityExceeded, InvalidCredential
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
        csv.initialize(options={"file": str(file)}, timeout=1)


def test_sql_readonly_fresh_lookups_and_schema(database):
    url, writer = database
    state = sqlalchemy.initialize(options={"url": url}, timeout=1)
    try:
        sqlalchemy.check(state=state, timeout=1)
        assert sqlalchemy.lookup(1235, state=state, timeout=1) is None
        with pytest.raises(SQLAlchemyError), state.engine.begin() as conn:
            conn.execute(text("UPDATE node_credentials SET password='attempted-write'"))
        with writer.begin() as conn:
            conn.execute(
                text("UPDATE node_credentials SET password='rotated-password' WHERE node_id=1234")
            )
        assert sqlalchemy.lookup(1234, state=state, timeout=1) == "rotated-password"
        with writer.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num='unknown-future'"))
        with pytest.raises(BackendUnavailable):
            sqlalchemy.check(state=state, timeout=1)
    finally:
        sqlalchemy.close(state=state, timeout=1)


def test_missing_sqlite_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        sqlalchemy.initialize(options={"url": f"sqlite:///{path}"}, timeout=1)
    assert not path.exists()


def test_loader_rejects_bad_contract(monkeypatch):
    module = ModuleType("bad_backend")
    monkeypatch.setitem(sys.modules, "bad_backend", module)
    with pytest.raises(ValueError):
        Backend("bad_backend", {}, workers=1)
    module.API_VERSION = 1
    with pytest.raises(ValueError):
        Backend("bad_backend", {}, workers=1)


def test_timeout_does_not_release_real_work_slot():
    async def scenario():
        provider = Backend("tests.backend_fixture", {}, workers=1)
        await provider.start(1)
        provider.state.block = True
        try:
            with pytest.raises(TimeoutError):
                await provider.lookup(1234, 0.02)
            assert provider.lookups.active == 1
            with pytest.raises(CapacityExceeded):
                await provider.lookup(1234, 0.02)
            await provider.check(0.1)
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
    import time

    from tests import backend_fixture

    original = backend_fixture.initialize

    def initialize(*, options, timeout):
        time.sleep(0.04)
        return original(options=options, timeout=timeout)

    monkeypatch.setattr(backend_fixture, "initialize", initialize)

    async def scenario():
        provider = Backend("tests.backend_fixture", {}, workers=1)
        with pytest.raises(TimeoutError):
            await provider.start(0.005)
        assert await provider.close(0.5)
        assert provider.initialized and provider.state.closed

    asyncio.run(scenario())


def test_sql_health_has_independent_connection_capacity(database):
    state = sqlalchemy.initialize(options={"url": database[0], "pool_size": 1}, timeout=1)
    try:
        with state.engine.connect():
            sqlalchemy.check(state=state, timeout=1)
    finally:
        sqlalchemy.close(state=state, timeout=1)
