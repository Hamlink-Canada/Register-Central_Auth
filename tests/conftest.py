import hashlib
import subprocess

import pytest

from rpt_central_auth.config import load_settings

TOKENS = {name: "test-only-" + name + "-" + "x" * 32 for name in ["a", "b", "probe"]}


def openssl(*args):
    subprocess.run(["openssl", *args], check=True, capture_output=True)


@pytest.fixture(scope="session")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("tls")
    ca, ca_key = directory / "ca.pem", directory / "ca.key"
    openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca),
        "-days",
        "2",
        "-subj",
        "/CN=Test CA",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
    )
    result = {"ca": ca}
    for name in ["server", "a", "b", "probe", "stranger"]:
        key, request, cert = [directory / f"{name}.{ext}" for ext in ["key", "csr", "pem"]]
        openssl(
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(request),
            "-subj",
            f"/CN={name}",
        )
        extensions = directory / f"{name}.ext"
        san = "localhost" if name == "server" else name
        usage = "serverAuth" if name == "server" else "clientAuth"
        extensions.write_text(f"subjectAltName=DNS:{san}\nextendedKeyUsage={usage}\n")
        openssl(
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(ca),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(cert),
            "-days",
            "2",
            "-extfile",
            str(extensions),
        )
        result[name] = (cert, key)
    return result


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch, tmp_path):
    monkeypatch.setenv("RPT_CONFIG_DIR", str(tmp_path))


@pytest.fixture
def settings(certificates):
    cert, key = certificates["server"]
    identities = [
        {
            "name": name,
            "role": "probe" if name == "probe" else "registrar",
            "token_digests": [hashlib.sha256(TOKENS[name].encode()).hexdigest()],
            "sans": [name],
        }
        for name in TOKENS
    ]

    def make(mode="token", **overrides):
        values = {
            "tls": {"cert": str(cert), "key": str(key), "client_ca": str(certificates["ca"])},
            "auth": {"mode": mode, "identities": identities},
            "backend": {"module": "tests.backend_fixture"},
        }
        for option_name, value in overrides.items():
            if option_name in values and isinstance(value, dict):
                values[option_name].update(value)
            else:
                values[option_name] = value
        return load_settings(**values)

    return make
