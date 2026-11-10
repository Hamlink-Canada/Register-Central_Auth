import asyncio
import time

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from prometheus_client import generate_latest
from rpt_protobuf.auth_request_pb2 import CredentialRequest
from rpt_protobuf.auth_request_pb2_grpc import CredentialServiceStub

from rpt_central_auth.auth import AuthenticationFailed, Authenticator
from rpt_central_auth.errors import BackendUnavailable
from rpt_central_auth.probe import check as probe_check
from rpt_central_auth.server import SERVICES, Application
from tests.conftest import TOKENS


def credentials(certificates, client=None):
    kwargs = {"root_certificates": certificates["ca"].read_bytes()}
    if client:
        cert, key = certificates[client]
        kwargs.update(private_key=key.read_bytes(), certificate_chain=cert.read_bytes())
    return grpc.ssl_channel_credentials(**kwargs)


def metadata(name):
    return (("authorization", f"Bearer {TOKENS[name]}"),)


async def expect_error(awaitable, code):
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await awaitable
    assert caught.value.code() == code
    assert "sensitive" not in caught.value.details()


@pytest.mark.parametrize("mode", ["token", "mtls", "both"])
def test_real_tls_authentication_modes_and_probe_identity(settings, certificates, mode):
    async def scenario():
        config = settings(mode)
        app = Application(config)
        port = await app.start(address="127.0.0.1:0")
        client = "a" if mode != "token" else None
        channel = grpc.aio.secure_channel(f"localhost:{port}", credentials(certificates, client))
        probe_channel = grpc.aio.secure_channel(
            f"localhost:{port}", credentials(certificates, "probe" if mode != "token" else None)
        )
        try:
            md = metadata("a") if mode != "mtls" else ()
            result = await CredentialServiceStub(channel).GetNodeCredential(
                CredentialRequest(node_id=1234), metadata=md, timeout=2
            )
            assert result.password == "generated-test-password"
            pmd = metadata("probe") if mode != "mtls" else ()
            health = health_pb2_grpc.HealthStub(probe_channel)
            response = await health.Check(
                health_pb2.HealthCheckRequest(service=SERVICES["readiness"]),
                metadata=pmd,
                timeout=1,
            )
            assert response.status == health_pb2.HealthCheckResponse.SERVING
            await expect_error(
                CredentialServiceStub(probe_channel).GetNodeCredential(
                    CredentialRequest(node_id=1234), metadata=pmd, timeout=1
                ),
                grpc.StatusCode.PERMISSION_DENIED,
            )
            if mode == "both":
                await expect_error(
                    CredentialServiceStub(channel).GetNodeCredential(
                        CredentialRequest(node_id=1234), metadata=metadata("b"), timeout=1
                    ),
                    grpc.StatusCode.UNAUTHENTICATED,
                )
        finally:
            await channel.close()
            await probe_channel.close()
            assert await app.stop()

    asyncio.run(scenario())


def test_auth_precedes_backend_and_watch_is_protected(settings, certificates):
    async def scenario():
        app = Application(settings())
        port = await app.start(address="127.0.0.1:0")
        channel = grpc.aio.secure_channel(f"localhost:{port}", credentials(certificates))
        try:
            stub = CredentialServiceStub(channel)
            await expect_error(
                stub.GetNodeCredential(CredentialRequest(node_id=1234), timeout=1),
                grpc.StatusCode.UNAUTHENTICATED,
            )
            await expect_error(
                health_pb2_grpc.HealthStub(channel)
                .Watch(health_pb2.HealthCheckRequest(service=SERVICES["liveness"]), timeout=1)
                .read(),
                grpc.StatusCode.UNAUTHENTICATED,
            )
            assert app.backend.state.lookups == 0
        finally:
            await channel.close()
            await app.stop()

    asyncio.run(scenario())


def test_rpc_errors_are_generic_and_all_nodes_allowed(settings, certificates):
    async def scenario():
        app = Application(settings())
        port = await app.start(address="127.0.0.1:0")
        channel = grpc.aio.secure_channel(f"localhost:{port}", credentials(certificates))
        stub = CredentialServiceStub(channel)

        async def call(node=1234):
            return await stub.GetNodeCredential(
                CredentialRequest(node_id=node), metadata=metadata("a"), timeout=2
            )

        try:
            assert (await call(2**63 - 1)).password == "generated-test-password"
            await expect_error(call(0), grpc.StatusCode.INVALID_ARGUMENT)
            app.backend.state.value = None
            await expect_error(call(), grpc.StatusCode.NOT_FOUND)
            app.backend.state.value = "invalid password"
            await expect_error(call(), grpc.StatusCode.INTERNAL)
            app.backend.state.failure = BackendUnavailable("sensitive-password")
            await expect_error(call(), grpc.StatusCode.UNAVAILABLE)
            app.backend.state.failure = RuntimeError("sensitive-dsn")
            await expect_error(call(), grpc.StatusCode.INTERNAL)
            metrics = generate_latest(app.metrics.registry).decode()
            assert "sensitive-password" not in metrics and "node_id" not in metrics
        finally:
            await channel.close()
            await app.stop()

    asyncio.run(scenario())


def test_health_survives_saturation_and_tracks_outage_recovery(settings, certificates):
    async def scenario():
        config = settings(runtime={"workers": 1, "lookup_timeout": 0.02})
        app = Application(config)
        port = await app.start(address="127.0.0.1:0")
        channel = grpc.aio.secure_channel(f"localhost:{port}", credentials(certificates))
        stub, health = CredentialServiceStub(channel), health_pb2_grpc.HealthStub(channel)

        async def lookup():
            return await stub.GetNodeCredential(
                CredentialRequest(node_id=1234), metadata=metadata("a"), timeout=1
            )

        async def status(kind):
            return (
                await health.Check(
                    health_pb2.HealthCheckRequest(service=SERVICES[kind]),
                    metadata=metadata("probe"),
                    timeout=1,
                )
            ).status

        serving, down = (
            health_pb2.HealthCheckResponse.SERVING,
            health_pb2.HealthCheckResponse.NOT_SERVING,
        )
        try:
            app.backend.state.block = True
            await expect_error(lookup(), grpc.StatusCode.UNAVAILABLE)
            assert app.backend.lookups.active == 1
            await expect_error(lookup(), grpc.StatusCode.RESOURCE_EXHAUSTED)
            assert await status("liveness") == serving
            assert await status("readiness") == serving
            app.backend.state.healthy = False
            await app.check_backend()
            assert await status("readiness") == down
            assert await status("liveness") == serving
            app.backend.state.healthy = True
            await app.check_backend()
            assert await status("readiness") == serving
            app.last_check = time.monotonic() - config.RUNTIME.STALE_AFTER - 1
            assert await status("readiness") == down
            config.PROBE.TARGET = f"localhost:{port}"
            config.PROBE.CA = str(certificates["ca"])
            config.PROBE.TOKEN = TOKENS["probe"]
            assert await asyncio.to_thread(probe_check, config, "liveness")
        finally:
            app.backend.state.release.set()
            await channel.close()
            assert await app.stop()

    asyncio.run(scenario())


def test_tls_chain_alone_does_not_authorize(settings, certificates):
    async def scenario():
        app = Application(settings("mtls"))
        port = await app.start(address="127.0.0.1:0")
        channel = grpc.aio.secure_channel(
            f"localhost:{port}", credentials(certificates, "stranger")
        )
        try:
            await expect_error(
                CredentialServiceStub(channel).GetNodeCredential(
                    CredentialRequest(node_id=1234), timeout=1
                ),
                grpc.StatusCode.UNAUTHENTICATED,
            )
        finally:
            await channel.close()
            await app.stop()

    asyncio.run(scenario())


def test_ambiguous_sans_and_duplicate_tokens_rejected(settings):
    config = settings("mtls")
    auth = Authenticator(config)
    with pytest.raises(AuthenticationFailed):
        auth.authenticate(
            (), {"transport_security_type": [b"ssl"], "x509_subject_alternative_name": [b"a", b"b"]}
        )
    config.AUTH.IDENTITIES[1]["token_digests"] = config.AUTH.IDENTITIES[0]["token_digests"]
    with pytest.raises(ValueError):
        Authenticator(config)


def test_registrar_throttling_preserves_other_identity_and_health(settings, certificates):
    async def scenario():
        app = Application(settings(limits={"registrar_burst": 1, "registrar_rate": 0.001}))
        port = await app.start(address="127.0.0.1:0")
        channel = grpc.aio.secure_channel(f"localhost:{port}", credentials(certificates))
        stub = CredentialServiceStub(channel)
        try:

            async def lookup(identity):
                return await stub.GetNodeCredential(
                    CredentialRequest(node_id=1234), metadata=metadata(identity), timeout=1
                )

            await lookup("a")
            await expect_error(lookup("a"), grpc.StatusCode.RESOURCE_EXHAUSTED)
            assert (await lookup("b")).password == "generated-test-password"
            response = await health_pb2_grpc.HealthStub(channel).Check(
                health_pb2.HealthCheckRequest(service=SERVICES["readiness"]),
                metadata=metadata("probe"),
                timeout=1,
            )
            assert response.status == health_pb2.HealthCheckResponse.SERVING
        finally:
            await channel.close()
            await app.stop()

    asyncio.run(scenario())


def test_request_cancellation_retains_backend_slot(settings, certificates):
    async def scenario():
        app = Application(settings(runtime={"workers": 1}))
        port = await app.start(address="127.0.0.1:0")
        channel = grpc.aio.secure_channel(f"localhost:{port}", credentials(certificates))
        stub = CredentialServiceStub(channel)
        app.backend.state.block = True
        try:
            call = stub.GetNodeCredential(
                CredentialRequest(node_id=1234), metadata=metadata("a"), timeout=2
            )
            for _ in range(100):
                if app.backend.state.entered.is_set():
                    break
                await asyncio.sleep(0.002)
            assert app.backend.state.entered.is_set()
            call.cancel()
            await asyncio.sleep(0.01)
            assert app.backend.lookups.active == 1
            await expect_error(
                stub.GetNodeCredential(
                    CredentialRequest(node_id=1234), metadata=metadata("a"), timeout=1
                ),
                grpc.StatusCode.RESOURCE_EXHAUSTED,
            )
        finally:
            app.backend.state.release.set()
            await channel.close()
            await app.stop()

    asyncio.run(scenario())


def test_authenticated_health_watch_tracks_changes(settings, certificates):
    async def scenario():
        app = Application(settings())
        port = await app.start(address="127.0.0.1:0")
        channel = grpc.aio.secure_channel(f"localhost:{port}", credentials(certificates))
        watch = health_pb2_grpc.HealthStub(channel).Watch(
            health_pb2.HealthCheckRequest(service=SERVICES["readiness"]),
            metadata=metadata("probe"),
            timeout=2,
        )
        try:
            assert (await watch.read()).status == health_pb2.HealthCheckResponse.SERVING
            app.backend.state.healthy = False
            await app.check_backend()
            assert (await watch.read()).status == health_pb2.HealthCheckResponse.NOT_SERVING
        finally:
            watch.cancel()
            await channel.close()
            await app.stop()

    asyncio.run(scenario())
