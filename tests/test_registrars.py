"""Exercise the existing registrar implementations against the real TLS service.

Run with uv sync --group integration and sibling registrar checkouts. Kafka delivery
is captured; the HTTP route and IAX UDP/challenge exchange use production code.
"""

import asyncio
import socket
import sys
from hashlib import md5
from pathlib import Path
from struct import pack, unpack
from threading import Thread

import pytest
from rpt_common.migrate import upgrade
from sqlalchemy import create_engine, text

from rpt_central_auth.server import Application
from tests.conftest import TOKENS


class Publisher:
    def __init__(self):
        self.sent = []

    def publish(self, *values):
        self.sent.append(values)


@pytest.mark.parametrize("backend", ["csv", "sqlalchemy", "custom"])
def test_registrars_against_central(settings, certificates, tmp_path, backend):
    workspace = Path(__file__).resolve().parents[2]
    if not all(
        (workspace / repo / "src").exists() for repo in ["Register-pyHTTP", "Register-pyIAX"]
    ):
        pytest.skip("registrar integration requires sibling source checkouts")
    pytest.importorskip("flask", reason="install the integration dependency group")
    pytest.importorskip("confluent_kafka", reason="install the integration dependency group")
    for repo in ["Register-pyHTTP", "Register-pyIAX"]:
        sys.path.insert(0, str(workspace / repo / "src"))
    from rpt_http.app import create_app
    from rpt_http.auth import GrpcCredentials as HTTPProvider
    from rpt_http.config import load_settings as http_settings
    from rpt_iax.auth import GrpcCredentials as IAXProvider
    from rpt_iax.cli import Server
    from rpt_iax.config import load_settings as iax_settings
    from rpt_iax.protocol import RegistrationProtocol, elements, ie
    from rpt_iax.security import AbuseGuard

    password = "generated-test-password"
    config = settings()
    if backend == "csv":
        file = tmp_path / "credentials.csv"
        file.write_text(f"node_id,password\n1234,{password}\n")
        config.BACKEND.MODULE = "rpt_central_auth.backends.csv"
        config.BACKEND.OPTIONS = {"file": str(file)}
    elif backend == "sqlalchemy":
        url = f"sqlite:///{tmp_path / 'credentials.db'}"
        upgrade(url)
        engine = create_engine(url)
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO node_credentials VALUES (1234, :password, 1)"),
                {"password": password},
            )
        engine.dispose()
        config.BACKEND.MODULE = "rpt_central_auth.backends.sqlalchemy"
        config.BACKEND.OPTIONS = {"url": url}

    async def scenario():
        app = Application(config)
        port = await app.start(address="127.0.0.1:0")
        rpc = {"target": f"localhost:{port}", "ca": str(certificates["ca"]), "token": TOKENS["a"]}
        http_config, iax_config = http_settings(grpc=rpc), iax_settings(grpc=rpc)
        http_provider, iax_provider = HTTPProvider(http_config), IAXProvider(iax_config)
        http_publisher, iax_publisher = Publisher(), Publisher()
        web = create_app(http_config, http_provider, http_publisher).test_client()
        guard = AbuseGuard(iax_config)
        protocol = RegistrationProtocol(iax_config, iax_provider, iax_publisher, guard)
        server = Server(("127.0.0.1", 0), protocol, guard, 4)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def iax_exchange():
            def frame(payload, dest=0, stamp=0, sequence=0):
                return pack("!HHIBBBB", 0x8001, dest, stamp, sequence, 0, 6, 13) + payload

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(2)
                sock.sendto(frame(ie(6, "1234")), server.server_address)
                challenge = sock.recv(4096)
                assert challenge[11] == 14
                server_call = unpack("!H", challenge[:2])[0] & 0x7FFF
                proof = md5(elements(challenge[12:])[15] + password.encode()).hexdigest()
                sock.sendto(
                    frame(ie(6, "1234") + ie(16, proof), server_call, 1, 1), server.server_address
                )
                assert sock.recv(4096)[11] == 15

        try:
            payload = {"data": {"nodes": {"1234": {"node": "1234", "passwd": password}}}}
            result = await asyncio.to_thread(web.post, "/", json=payload)
            assert result.status_code == 200 and "successfully registered" in result.json["data"][0]
            payload["data"]["nodes"]["1234"]["passwd"] = "wrong-generated-password"
            rejected = await asyncio.to_thread(web.post, "/", json=payload)
            assert "successfully registered" not in rejected.json["data"][0]
            await asyncio.to_thread(iax_exchange)
            assert len(http_publisher.sent) == len(iax_publisher.sent) == 1
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)
            http_provider.close()
            iax_provider.close()
            await app.stop()

    asyncio.run(scenario())
