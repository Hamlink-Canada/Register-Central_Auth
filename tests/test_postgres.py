"""Opt-in isolated Docker PostgreSQL checks: RPT_TEST_POSTGRES=1."""

import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from rpt_common.migrate import upgrade
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from rpt_central_auth.backends import sqlalchemy
from rpt_central_auth.errors import BackendUnavailable

POSTGRES_IMAGE = (
    "postgres:17-alpine@sha256:3540c0362c809df084ffac0ae69fe794515f9de92692f02178bcdce0ef0302ac"
)


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.PIPE).strip()


@dataclass(repr=False)
class Database:
    writer: object
    reader: object
    ca: str


@pytest.fixture(scope="session")
def postgres(certificates, tmp_path_factory):
    if os.environ.get("RPT_TEST_POSTGRES") != "1":
        pytest.skip("set RPT_TEST_POSTGRES=1 for Docker PostgreSQL acceptance")
    directory = tmp_path_factory.mktemp("postgres")
    for source, name in zip(certificates["server"], ["server.pem", "server.key"]):
        shutil.copy(source, directory / name)
    password, reader_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    name = "rpt-auth-postgres-" + secrets.token_hex(4)
    env = os.environ.copy()
    env["POSTGRES_PASSWORD"] = password
    command = [
        "docker",
        "run",
        "--detach",
        "--name",
        name,
        "--publish",
        "127.0.0.1::5432",
        "--env",
        "POSTGRES_PASSWORD",
        "--env",
        "POSTGRES_DB=rpt_credentials",
        "--mount",
        f"type=bind,src={directory},dst=/input,readonly",
        "--entrypoint",
        "sh",
        POSTGRES_IMAGE,
        "-c",
        "cp /input/server.* /tmp/ && chown postgres:postgres /tmp/server.* && "
        "chmod 600 /tmp/server.key && exec docker-entrypoint.sh postgres "
        "-c ssl=on -c ssl_cert_file=/tmp/server.pem -c ssl_key_file=/tmp/server.key",
    ]
    try:
        subprocess.run(command, env=env, check=True, capture_output=True, text=True)
        port = int(docker("port", name, "5432/tcp").rsplit(":", 1)[1])
        ca = str(certificates["ca"])
        writer = URL.create(
            "postgresql+psycopg",
            username="postgres",
            password=password,
            host="localhost",
            port=port,
            database="rpt_credentials",
            query={"sslmode": "verify-full", "sslrootcert": ca},
        )
        deadline = time.monotonic() + 30
        while True:
            try:
                with psycopg.connect(
                    host="localhost",
                    port=port,
                    user="postgres",
                    password=password,
                    dbname="rpt_credentials",
                    sslmode="verify-full",
                    sslrootcert=ca,
                    connect_timeout=1,
                ) as conn:
                    conn.execute(
                        sql.SQL("CREATE ROLE rpt_reader LOGIN PASSWORD {}").format(
                            sql.Literal(reader_password)
                        )
                    )
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise RuntimeError("isolated PostgreSQL did not become ready") from None
                time.sleep(0.1)
        upgrade(writer)
        engine = create_engine(writer, hide_parameters=True)
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO node_credentials VALUES (1234, 'generated-test-password', true)")
            )
            conn.execute(
                text("INSERT INTO node_credentials VALUES (1235, 'disabled-password', false)")
            )
            conn.execute(text("GRANT CONNECT ON DATABASE rpt_credentials TO rpt_reader"))
            conn.execute(text("GRANT USAGE ON SCHEMA public TO rpt_reader"))
            conn.execute(text("GRANT SELECT ON node_credentials, alembic_version TO rpt_reader"))
        engine.dispose()
        yield Database(writer, writer.set(username="rpt_reader", password=reader_password), ca)
    finally:
        subprocess.run(["docker", "rm", "--force", "--volumes", name], capture_output=True)


def test_postgres_migration_readonly_and_query_timeout(postgres):
    state = sqlalchemy.initialize(options={"url": postgres.reader, "ca": postgres.ca}, timeout=2)
    try:
        sqlalchemy.check(state=state, timeout=1)
        assert sqlalchemy.lookup(1234, state=state, timeout=1) == "generated-test-password"
        assert sqlalchemy.lookup(1235, state=state, timeout=1) is None
        with pytest.raises(SQLAlchemyError), state.engine.begin() as connection:
            connection.execute(text("UPDATE node_credentials SET enabled=false"))
        started = time.monotonic()
        with pytest.raises(BackendUnavailable), sqlalchemy.connection(state, 0.03) as connection:
            connection.execute(text("SELECT pg_sleep(1)"))
        assert time.monotonic() - started < 0.9
    finally:
        sqlalchemy.close(state=state, timeout=1)


def test_postgres_verification_override_is_still_encrypted(postgres, certificates, monkeypatch):
    wrong_ca = str(certificates["stranger"][0])
    monkeypatch.setenv("PGSSLROOTCERT", wrong_ca)
    hostile = postgres.reader.set(
        host="127.0.0.1", query={"sslmode": "verify-full", "sslrootcert": wrong_ca}
    )
    verified = sqlalchemy.initialize(options={"url": hostile}, timeout=2)
    try:
        with pytest.raises(BackendUnavailable):
            sqlalchemy.check(state=verified, timeout=2)
    finally:
        sqlalchemy.close(state=verified, timeout=1)
    unchecked = sqlalchemy.initialize(options={"url": hostile, "verify_tls": False}, timeout=2)
    ca_directory = Path(unchecked.ca_directory.name)
    try:
        sqlalchemy.check(state=unchecked, timeout=1)
        with sqlalchemy.connection(unchecked, 1) as connection:
            assert (
                connection.scalar(text("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()"))
                is True
            )
    finally:
        sqlalchemy.close(state=unchecked, timeout=1)
    assert not ca_directory.exists()


def test_postgres_readonly_even_with_writer_credentials(postgres):
    state = sqlalchemy.initialize(options={"url": postgres.writer, "ca": postgres.ca}, timeout=2)
    try:
        with pytest.raises(SQLAlchemyError), state.engine.begin() as connection:
            connection.execute(text("UPDATE node_credentials SET enabled=false"))
    finally:
        sqlalchemy.close(state=state, timeout=1)
