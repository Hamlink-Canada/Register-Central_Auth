"""Authenticated standard health probe. No secrets in arguments or output."""

import argparse
from pathlib import Path

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc

from rpt_central_auth.config import load_settings
from rpt_central_auth.server import SERVICES


def check(settings, service="readiness"):
    config = settings.PROBE
    if settings.TLS.INSECURE:
        channel = grpc.insecure_channel(config.TARGET)
    else:

        def read(path):
            return Path(path).read_bytes() if path else None

        if bool(config.CERT) != bool(config.KEY):
            raise ValueError("probe certificate and key must be paired")
        credentials = grpc.ssl_channel_credentials(
            root_certificates=read(config.CA),
            private_key=read(config.KEY),
            certificate_chain=read(config.CERT),
        )
        channel = grpc.secure_channel(config.TARGET, credentials)
    metadata = (("authorization", f"Bearer {config.TOKEN}"),) if config.TOKEN else ()
    try:
        response = health_pb2_grpc.HealthStub(channel).Check(
            health_pb2.HealthCheckRequest(service=SERVICES[service]),
            metadata=metadata,
            timeout=config.TIMEOUT,
        )
        return response.status == health_pb2.HealthCheckResponse.SERVING
    finally:
        channel.close()


def main():
    parser = argparse.ArgumentParser(
        description="Check Central Auth health using mounted configuration"
    )
    parser.add_argument("service", choices=list(SERVICES), nargs="?", default="readiness")
    args = parser.parse_args()
    try:
        healthy = check(load_settings(), args.service)
    except Exception:
        healthy = False
    raise SystemExit(0 if healthy else 1)
