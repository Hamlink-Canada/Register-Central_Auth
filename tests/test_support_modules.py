import pytest
from prometheus_client import generate_latest

from rpt_central_auth.metrics import Metrics
from rpt_central_auth.probe import check


@pytest.mark.parametrize(
    "change",
    [
        {"port": 65536},
        {"metrics": {"port": 0}},
        {"runtime": {"check_interval": 0}},
        {"runtime": {"stale_after": 10}},
        {"metrics": {"enabled": "yes"}},
        {"tls": {"insecure": True}, "environment": "production"},
        {"auth": {"mode": "mtls"}, "tls": {"client_ca": ""}},
    ],
)
def test_configuration_rejects_invalid_runtime_and_transport(settings, change):
    with pytest.raises(ValueError):
        settings(**change)


def test_configuration_allows_development_plaintext(settings):
    config = settings(environment="development", tls={"insecure": True})
    assert config.TLS.INSECURE is True


def test_metrics_export_only_finite_labels():
    metrics = Metrics()
    metrics.requests.labels("GetNodeCredential", "success").inc()
    metrics.checks.labels("ok").inc()
    metrics.ready.set(1)

    output = generate_latest(metrics.registry).decode()
    assert 'rpt_auth_requests_total{method="GetNodeCredential",outcome="success"} 1.0' in output
    assert "generated-secret" not in output
    assert "rpt_auth_lookup_inflight" in output


def test_probe_uses_insecure_channel_and_closes(monkeypatch, settings):
    calls = []

    class Stub:
        def __init__(self, channel):
            calls.append(("stub", channel))

        def Check(self, request, **kwargs):
            calls.append(("check", request.service, kwargs))
            return type("Response", (), {"status": 1})()

    class Channel:
        def close(self):
            calls.append(("close",))

    channel = Channel()
    monkeypatch.setattr("rpt_central_auth.probe.grpc.insecure_channel", lambda target: channel)
    monkeypatch.setattr("rpt_central_auth.probe.health_pb2_grpc.HealthStub", Stub)
    config = settings(environment="development", tls={"insecure": True}, probe={"token": "probe-token"})

    assert check(config, "readiness")
    assert calls[0] == ("stub", channel)
    assert calls[1][2]["metadata"] == (("authorization", "Bearer probe-token"),)
    assert calls[-1] == ("close",)


def test_probe_rejects_unpaired_client_certificate(settings):
    config = settings(probe={"cert": "/tmp/client.pem", "key": ""})
    with pytest.raises(ValueError, match="paired"):
        check(config)
