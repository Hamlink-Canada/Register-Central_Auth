"""Explicit Dynaconf file precedence; never log or print settings."""

import math
import os
from pathlib import Path

from dynaconf import Dynaconf
from dynaconf.loaders import env_loader

DEFAULTS = {
    "environment": "production",
    "host": "0.0.0.0",
    "port": 50051,
    "tls": {"insecure": False, "cert": "", "key": "", "client_ca": ""},
    "auth": {"mode": "token", "identities": []},
    "backend": {"module": "rpt_central_auth.backends.csv", "options": {}},
    "runtime": {
        "workers": 32,
        "max_rpcs": 128,
        "lookup_timeout": 1,
        "startup_timeout": 10,
        "check_interval": 10,
        "check_timeout": 1,
        "stale_after": 30,
        "shutdown_timeout": 15,
    },
    "limits": {
        "state_size": 16384,
        "global_rate": 60000,
        "global_burst": 2000,
        "registrar_rate": 6000,
        "registrar_burst": 120,
        "fail_ip_rate": 30,
        "fail_ip_burst": 30,
        "health_rate": 600,
        "health_burst": 60,
    },
    "metrics": {"enabled": False, "host": "127.0.0.1", "port": 9100},
    "probe": {
        "target": "localhost:50051",
        "ca": "",
        "cert": "",
        "key": "",
        "token": "",
        "timeout": 1,
    },
}


def load_settings(**overrides):
    settings = Dynaconf(
        envvar_prefix="RPT",
        environments=False,
        merge_enabled=True,
        settings_files=[],
        load_dotenv=False,
    )
    settings.update(DEFAULTS)
    directory = Path(os.environ.get("RPT_CONFIG_DIR", ".")).resolve()
    for name in ("settings.toml", ".secrets.toml"):
        path = directory / name
        if path.exists():
            settings.load_file(path=str(path))
    env_loader.load(settings, env="RPT")
    settings.update(overrides, merge=True)
    for value in [
        settings.PORT,
        settings.METRICS.PORT,
        settings.PROBE.TIMEOUT,
        *settings.RUNTIME.values(),
        *settings.LIMITS.values(),
    ]:
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("numeric settings must be finite and positive")
    for value in [
        settings.PORT,
        settings.METRICS.PORT,
        settings.RUNTIME.WORKERS,
        settings.RUNTIME.MAX_RPCS,
        settings.LIMITS.STATE_SIZE,
    ]:
        if type(value) is not int:
            raise ValueError("ports and capacities must be integers")
    if settings.PORT > 65535 or settings.METRICS.PORT > 65535:
        raise ValueError("invalid port")
    if settings.RUNTIME.STALE_AFTER <= settings.RUNTIME.CHECK_INTERVAL:
        raise ValueError("stale threshold must exceed check interval")
    if settings.RUNTIME.MAX_RPCS < settings.RUNTIME.WORKERS + 16:
        raise ValueError("reserve RPC capacity for health and watch streams")
    for value in [settings.TLS.INSECURE, settings.METRICS.ENABLED]:
        if type(value) is not bool:
            raise ValueError("boolean settings must be booleans")
    if settings.TLS.INSECURE and settings.ENVIRONMENT != "development":
        raise ValueError("plaintext gRPC requires development environment")
    if not settings.TLS.INSECURE and not (settings.TLS.CERT and settings.TLS.KEY):
        raise ValueError("server TLS certificate and key are required")
    if settings.AUTH.MODE not in ("token", "mtls", "both"):
        raise ValueError("invalid authentication mode")
    if settings.AUTH.MODE != "token" and (settings.TLS.INSECURE or not settings.TLS.CLIENT_CA):
        raise ValueError("mutual TLS requires client CA and encrypted transport")
    if settings.BACKEND.MODULE == "rpt_central_auth.backends.sqlalchemy":
        development = settings.BACKEND.OPTIONS.get("development", False)
        if development and settings.ENVIRONMENT != "development":
            raise ValueError("database development options require development environment")
    return settings
