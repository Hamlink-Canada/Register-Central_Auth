import pytest

from rpt_central_auth.config import load_settings
from rpt_central_auth.limits import Limits


def test_file_environment_precedence(monkeypatch, tmp_path, certificates):
    cert, key = certificates["server"]
    (tmp_path / "settings.toml").write_text(
        f'[tls]\ncert="{cert}"\nkey="{key}"\n[runtime]\nworkers=4\n'
    )
    (tmp_path / ".secrets.toml").write_text('[probe]\ntoken="test-only-probe"\n')
    monkeypatch.setenv("RPT_RUNTIME__WORKERS", "7")
    config = load_settings()
    assert config.RUNTIME.WORKERS == 7 and config.PROBE.TOKEN == "test-only-probe"
    assert config.RUNTIME.CHECK_TIMEOUT == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"tls": {"insecure": True}},
        {"runtime": {"workers": 0}},
        {"runtime": {"lookup_timeout": float("inf")}},
        {"runtime": {"max_rpcs": 1}},
        {"auth": {"mode": "invalid"}},
    ],
)
def test_invalid_settings(settings, overrides):
    with pytest.raises(ValueError):
        settings(**overrides)


def test_budgets_are_bounded_and_recover(settings):
    config = settings(limits={"state_size": 2, "fail_ip_burst": 1, "fail_ip_rate": 60})
    now = [1.0]
    limits = Limits(config.LIMITS, timer=lambda: now[0])
    assert limits.auth_allowed("one")
    limits.auth_failed("one")
    assert not limits.auth_allowed("one")
    assert limits.auth_allowed("two")
    assert not limits.auth_allowed("three")
    assert len(limits.buckets) == 2
    now[0] += 1
    assert limits.auth_allowed("one")


def test_nested_backend_environment_options(settings, monkeypatch, tmp_path):
    import asyncio

    from rpt_central_auth.server import Application

    file = tmp_path / "credentials.csv"
    file.write_text("node_id,password\n1234,generated-test-password\n")
    monkeypatch.setenv("RPT_BACKEND__OPTIONS__FILE", str(file))

    async def scenario():
        config = settings(backend={"module": "rpt_central_auth.backends.csv"})
        app = Application(config)
        await app.start(address="127.0.0.1:0")
        try:
            assert await app.backend.lookup(1234, 1) == "generated-test-password"
        finally:
            await app.stop()

    asyncio.run(scenario())
