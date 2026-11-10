"""Opt-in built-image acceptance: RPT_TEST_IMAGE=rpt-central-auth:review."""

import json
import os
import secrets
import shutil
import subprocess
import time

import pytest

from tests.conftest import TOKENS


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.PIPE).strip()


def test_nonroot_health_and_graceful_shutdown(settings, certificates, tmp_path):
    image = os.environ.get("RPT_TEST_IMAGE")
    if not image:
        pytest.skip("set RPT_TEST_IMAGE to a locally built image")
    config = settings()
    directory = tmp_path / "config"
    directory.mkdir(mode=0o755)
    # These are generated test-only credentials; the enclosing host temp tree is private.
    for source, name in [
        (certificates["server"][0], "server.pem"),
        (certificates["server"][1], "server.key"),
        (certificates["ca"], "ca.pem"),
    ]:
        shutil.copy(source, directory / name)
    registrar, probe = config.AUTH.IDENTITIES[0], config.AUTH.IDENTITIES[2]
    (directory / "settings.toml").write_text(f'''
[tls]
cert="/config/server.pem"
key="/config/server.key"
[[auth.identities]]
name="a"
role="registrar"
token_digests=["{registrar["token_digests"][0]}"]
[[auth.identities]]
name="probe"
role="probe"
token_digests=["{probe["token_digests"][0]}"]
[backend.options]
file="/config/credentials.csv"
[probe]
ca="/config/ca.pem"
''')
    (directory / ".secrets.toml").write_text(f'[probe]\ntoken="{TOKENS["probe"]}"\n')
    (directory / "credentials.csv").write_text("node_id,password\n1234,generated-test-password\n")
    for file in directory.iterdir():
        file.chmod(0o444)
    name = "rpt-auth-acceptance-" + secrets.token_hex(4)
    try:
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=bind,src={directory},dst=/config,readonly",
            "--env",
            "RPT_CONFIG_DIR=/config",
            image,
        )
        deadline = time.monotonic() + 35
        while True:
            info = json.loads(docker("inspect", name))[0]
            if info["State"]["Health"]["Status"] == "healthy":
                break
            if info["State"]["Status"] != "running" or time.monotonic() >= deadline:
                pytest.fail("container did not pass its configured health check")
            time.sleep(0.2)
        assert info["Config"]["User"] == "10001:10001"
        assert docker("exec", name, "id", "-u") == "10001"
        for service in ["startup", "liveness", "readiness"]:
            docker("exec", name, "rpt-central-auth-health", service)
        docker("stop", "--time", "30", name)
        assert json.loads(docker("inspect", name))[0]["State"]["ExitCode"] == 0
        captured = subprocess.run(
            ["docker", "logs", name], check=True, capture_output=True, text=True
        )
        logs = captured.stdout + captured.stderr
        assert "generated-test-password" not in logs
        assert all(token not in logs for token in TOKENS.values())
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "rpt-common-migrate",
            "--env",
            "RPT_MIGRATION_DATABASE_URL=sqlite:////tmp/migration.db",
            image,
            "upgrade",
        )
    finally:
        subprocess.run(["docker", "rm", "--force", "--volumes", name], capture_output=True)
