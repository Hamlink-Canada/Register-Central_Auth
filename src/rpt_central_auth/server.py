"""Async gRPC frontend with independently bounded blocking backend execution."""

import asyncio
import contextlib
import time
from pathlib import Path

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from prometheus_client import start_http_server
from rpt_common.models.credentials import valid_node_id
from rpt_protobuf.auth_request_pb2 import CredentialResponse
from rpt_protobuf.auth_request_pb2_grpc import (
    CredentialServiceServicer,
    add_CredentialServiceServicer_to_server,
)

from rpt_central_auth.auth import AuthenticationFailed, Authenticator
from rpt_central_auth.errors import BackendUnavailable, CapacityExceeded
from rpt_central_auth.limits import Limits
from rpt_central_auth.metrics import Metrics
from rpt_central_auth.module import Backend

SERVICES = {
    "startup": "rpt.central_auth.startup",
    "liveness": "rpt.central_auth.liveness",
    "readiness": "rpt.central_auth.readiness",
}
KNOWN_SERVICES = frozenset(["", "rpt.auth.v1.CredentialService", *SERVICES.values()])


class Credentials(CredentialServiceServicer):
    def __init__(self, app):
        self.app = app

    async def GetNodeCredential(self, request, context):
        started, outcome = time.monotonic(), "INTERNAL"
        try:
            identity = await self.app.authorize(context, "lookup")
            if identity.role != "registrar":
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "credential lookup denied")
            if not valid_node_id(request.node_id):
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid node ID")
            if self.app.draining:
                await context.abort(grpc.StatusCode.UNAVAILABLE, "service draining")
            if not self.app.limits.lookup(identity.name):
                await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "request budget exceeded")
            timeout = self.app.settings.RUNTIME.LOOKUP_TIMEOUT
            remaining = context.time_remaining()
            if remaining is not None:
                if remaining <= 0:
                    await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "deadline exceeded")
                timeout = min(timeout, remaining)
            try:
                password = await self.app.backend.lookup(request.node_id, timeout)
            except CapacityExceeded:
                await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "lookup capacity exceeded")
            except (TimeoutError, BackendUnavailable):
                await context.abort(grpc.StatusCode.UNAVAILABLE, "credential backend unavailable")
            except Exception:
                await context.abort(grpc.StatusCode.INTERNAL, "credential backend failure")
            if password is None:
                await context.abort(grpc.StatusCode.NOT_FOUND, "credential unavailable")
            outcome = "OK"
            return CredentialResponse(password=password)
        except grpc.aio.AbortError:
            outcome = context.code().name
            raise
        except asyncio.CancelledError:
            outcome = "CANCELLED"
            raise
        finally:
            self.app.metrics.requests.labels("lookup", outcome).inc()
            self.app.metrics.latency.observe(time.monotonic() - started)
            self.app.metrics.inflight.set(self.app.backend.lookups.active)


class Health(health.aio.HealthServicer):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.watchers = 0

    async def validate(self, request, context):
        await self.app.authorize(context, "health")
        if request.service not in KNOWN_SERVICES:
            await context.abort(grpc.StatusCode.NOT_FOUND, "unknown health service")
        await self.app.publish_health()

    async def Check(self, request, context):
        outcome = "OK"
        try:
            await self.validate(request, context)
            return await super().Check(request, context)
        except grpc.aio.AbortError:
            outcome = context.code().name
            raise
        except asyncio.CancelledError:
            outcome = "CANCELLED"
            raise
        finally:
            self.app.metrics.requests.labels("health", outcome).inc()

    async def Watch(self, request, context):
        await self.validate(request, context)
        if self.watchers >= 8:
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED, "health watch capacity exceeded"
            )
        self.watchers += 1
        try:
            await asyncio.wait_for(super().Watch(request, context), 30)
        except TimeoutError:
            await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "health watch renewal required")
        finally:
            self.watchers -= 1


class Application:
    def __init__(self, settings):
        self.settings = settings
        self.auth = Authenticator(settings)
        self.limits = Limits(settings.LIMITS)
        self.metrics = Metrics()
        options = {str(key).lower(): value for key, value in settings.BACKEND.OPTIONS.items()}
        self.backend = Backend(settings.BACKEND.MODULE, options, workers=settings.RUNTIME.WORKERS)
        self.draining = False
        self.started = False
        self.backend_ok = False
        self.last_check = 0
        self.monitor = None
        self.metrics_server = None
        self.health = Health(self)
        self.server = grpc.aio.server(
            maximum_concurrent_rpcs=settings.RUNTIME.MAX_RPCS,
            options=[
                ("grpc.max_receive_message_length", 4096),
                ("grpc.max_send_message_length", 4096),
                ("grpc.max_metadata_size", 16384),
            ],
        )
        add_CredentialServiceServicer_to_server(Credentials(self), self.server)
        health_pb2_grpc.add_HealthServicer_to_server(self.health, self.server)
        self.port = None
        self.metrics.inflight.set_function(lambda: self.backend.lookups.active)

    async def authorize(self, context, method):
        peer = context.peer()
        # gRPC peers are transport-supplied, not forwarded metadata. Ignore ephemeral ports.
        source = peer.rsplit(":", 1)[0] if peer.startswith(("ipv4:", "ipv6:")) else "local"
        if not self.limits.auth_allowed(source):
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED, "authentication budget exceeded"
            )
        try:
            identity = self.auth.authenticate(context.invocation_metadata(), context.auth_context())
        except AuthenticationFailed:
            self.limits.auth_failed(source)
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "service authentication required")
        if method == "health" and not self.limits.health(identity.name):
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "health budget exceeded")
        return identity

    @property
    def ready(self):
        return (
            self.started
            and not self.draining
            and self.backend_ok
            and (time.monotonic() - self.last_check <= self.settings.RUNTIME.STALE_AFTER)
        )

    async def publish_health(self):
        serving, down = (
            health_pb2.HealthCheckResponse.SERVING,
            health_pb2.HealthCheckResponse.NOT_SERVING,
        )
        for service, ok in [
            (SERVICES["startup"], self.started),
            (SERVICES["liveness"], self.started and not self.draining),
            (SERVICES["readiness"], self.ready),
            ("", self.ready),
            ("rpt.auth.v1.CredentialService", self.ready),
        ]:
            await self.health.set(service, serving if ok else down)
        self.metrics.ready.set(int(self.ready))
        self.metrics.inflight.set(self.backend.lookups.active)

    async def check_backend(self):
        try:
            await self.backend.check(self.settings.RUNTIME.CHECK_TIMEOUT)
            self.backend_ok = True
        except Exception:
            self.backend_ok = False
        self.last_check = time.monotonic()
        self.metrics.checks.labels("OK" if self.backend_ok else "UNAVAILABLE").inc()
        await self.publish_health()

    async def monitor_backend(self):
        while True:
            await asyncio.sleep(self.settings.RUNTIME.CHECK_INTERVAL)
            await self.check_backend()

    async def start(self, *, address=None):
        await self.publish_health()
        await self.backend.start(self.settings.RUNTIME.STARTUP_TIMEOUT)
        self.backend_ok, self.last_check = True, time.monotonic()
        address = address or f"{self.settings.HOST}:{self.settings.PORT}"
        if self.settings.TLS.INSECURE:
            self.port = self.server.add_insecure_port(address)
        else:
            cert = Path(self.settings.TLS.CERT).read_bytes()
            key = Path(self.settings.TLS.KEY).read_bytes()
            mutual = self.settings.AUTH.MODE in ("mtls", "both")
            roots = Path(self.settings.TLS.CLIENT_CA).read_bytes() if mutual else None
            credentials = grpc.ssl_server_credentials(
                ((key, cert),), root_certificates=roots, require_client_auth=mutual
            )
            self.port = self.server.add_secure_port(address, credentials)
        if not self.port:
            raise RuntimeError("cannot bind gRPC listener")
        await self.server.start()
        self.started = True
        await self.publish_health()
        self.monitor = asyncio.create_task(self.monitor_backend())
        if self.settings.METRICS.ENABLED:
            self.metrics_server = start_http_server(
                self.settings.METRICS.PORT,
                addr=self.settings.METRICS.HOST,
                registry=self.metrics.registry,
            )
        return self.port

    async def stop(self):
        self.draining = True
        await self.publish_health()
        if self.monitor is not None:
            self.monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.monitor
        budget = self.settings.RUNTIME.SHUTDOWN_TIMEOUT
        deadline = time.monotonic() + budget
        await self.server.stop(budget * 0.75)
        clean = await self.backend.close(max(0.001, deadline - time.monotonic()))
        if self.metrics_server is not None:
            server, thread = self.metrics_server
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)
        return clean
