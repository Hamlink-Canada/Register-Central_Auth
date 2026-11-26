# Central registration credentials

Central Auth supplies generated node passwords to trusted HTTP and IAX registrars
over gRPC. Registrars still verify passwords and IAX challenges themselves. This
service performs lookup only; operators provision and administer credentials through
their chosen data-management process. Every authenticated registrar can retrieve any
node's credential within this deployment's network.

Choose one authoritative backend: a validated CSV snapshot, the shared SQLAlchemy
model from `pyCommon`, or an installed custom Python backend module. There is no
fallback between backends and no server-side credential-result cache.

For setup and architecture, see the [documentation index](docs/README.md),
[current decisions](docs/decisions.md), and the cross-project
[operator documentation](https://hamlink-canada.github.io/apt-rpt-docs/projects/central-auth/).

## Requirements

Use Docker or Kubernetes, a server TLS certificate, registrar service identities,
and a health-only probe identity. PostgreSQL deployments also need a migrated schema
and a read-only lookup account. SQLite is for local development/testing. No Kafka
connection is needed by Central Auth.

The image runs as UID/GID 10001 and listens on TCP 50051. Mounted configuration,
certificates, keys, and credential files must be readable by that user. `/tmp` must
be writable, including when the root filesystem is read-only.

## Configure a CSV deployment

Set `RPT_CONFIG_DIR=/config`. Dynaconf loads Python defaults, `settings.toml`, optional
`.secrets.toml`, then `RPT_` environment overrides; nested keys use two underscores.
Files are plain TOML without environment sections. Relative paths use the process
working directory, so use absolute paths for mounts. Dotenv is not loaded.

Create `/config/settings.toml`:

```toml
[tls]
cert = "/config/server.pem"
key = "/config/server.key"

[auth]
mode = "token"

[[auth.identities]]
name = "registrar-1"
role = "registrar"
token_digests = ["REPLACE_WITH_64_CHARACTER_SHA256_DIGEST"]

[[auth.identities]]
name = "health-probe"
role = "probe"
token_digests = ["REPLACE_WITH_A_DIFFERENT_SHA256_DIGEST"]

[backend]
module = "rpt_central_auth.backends.csv"

[backend.options]
file = "/config/credentials.csv"

[probe]
target = "localhost:50051"
ca = "/config/ca.pem"
```

The server certificate must cover `localhost` for this local probe and the hostname
used by registrars. Generate distinct high-entropy bearer tokens for each identity,
and calculate their SHA-256 digests without putting plaintext tokens in shell history:

```sh
python3 -c 'import getpass, hashlib; print(hashlib.sha256(getpass.getpass("Token: ").encode()).hexdigest())'
```

Replace the digest placeholders. Supply the registrar's token to its gRPC client.
Store the probe token in `/config/.secrets.toml`:

```toml
[probe]
token = "REPLACE_WITH_THE_PROBE_TOKEN"
```

The CSV format is `node_id,password`, with that exact header and unique canonical
positive numeric IDs. Passwords are 1–79 printable ASCII characters without
whitespace. Give nodes distinct system-generated passwords. Real files and secrets
must never be committed. The CSV is validated and loaded once; restart to reload.

## Run with Docker Compose

Build the image as described below, prepare the configuration directory, and use:

```yaml
services:
  central-auth:
    image: rpt-central-auth:local
    restart: unless-stopped
    environment:
      RPT_CONFIG_DIR: /config
    volumes:
      - ./config:/config:ro
    ports:
      - "127.0.0.1:50051:50051"
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: [ALL]
    security_opt:
      - no-new-privileges:true
    stop_grace_period: 30s
```

The image HEALTHCHECK runs `rpt-central-auth-health readiness`. For manual checks:

```sh
docker compose exec central-auth rpt-central-auth-health liveness
docker compose exec central-auth rpt-central-auth-health readiness
```

The probe prints no secrets and returns zero only when its selected state is serving.
For remote registrars expose the TLS endpoint through your private network instead
of the loopback-only mapping above. Configure their `RPT_AUTH__BACKEND=grpc`,
`RPT_GRPC__TARGET`, CA, and service token or client certificate.

## PostgreSQL and SQLite

Select `rpt_central_auth.backends.sqlalchemy` and put its URL in `.secrets.toml`:

```toml
[backend.options]
url = "postgresql+psycopg://lookup:REPLACE_PASSWORD@db.example.org/rpt_credentials"
ca = "/config/database-ca.pem"
```

Production PostgreSQL defaults to `sslmode=verify-full`. Set
`RPT_BACKEND__OPTIONS__VERIFY_TLS=false` explicitly to retain encryption while
skipping certificate/hostname verification. For a local Unix socket TLS is not used.
Unencrypted TCP requires both `RPT_ENVIRONMENT=development` and backend option
`development=true`, with `tls=false`. Secrets and CA files must be readable inside
the container. Use URL-encoded passwords in connection URLs.

Run the shared migrations separately. Central Auth validates the supported schema
and applies read-only transaction settings; it never creates tables or writes
credentials. Use a SELECT-only database account even though the adapter also blocks
writes at the connection level. Database outages return UNAVAILABLE and make
readiness fail; the process remains live and can recover.

For local SQLite set the URL to `sqlite:////absolute/path/credentials.db` after
migration. The file must exist and is opened read-only. If another process writes
in WAL mode, mount the containing directory and account for its WAL/shared-memory
files; mounting only the main live database file read-only is insufficient. Do not
use immutable mode for a live database or share SQLite between production Pods.

## Service identities and health

`auth.mode` selects `token`, `mtls`, or `both`. Mutual TLS requires `tls.client_ca`
and configured `sans` on each identity, including the probe. In `both` mode the token
and certificate must map to the same identity. A CA-signed certificate alone is not
authorization. Supplied invalid credentials never cause fallback to a weaker mode.
Token lists and SAN lists permit overlap during rotation; one credential cannot map
to multiple configured identities. Roles are `registrar` and `probe`.

Health uses authenticated standard gRPC Check/Watch. Native Kubernetes gRPC probes
cannot supply these credentials, so use the exec command. A probe identity has
health permission only and cannot retrieve passwords. Supply its token and/or
`probe.cert`/`probe.key` plus the server CA through mounted secrets.

Startup health means initial validation completed; liveness means the server is
responsive; readiness means the latest periodic backend check is successful and
fresh and shutdown has not begun. Backend checks have reserved capacity and probes
do not query the database on every invocation. Readiness does not change merely
because a lookup was rate-limited. Watch streams require authentication, known
service names, bounded concurrency, and periodic renewal.

## Kubernetes

Create a Secret named `rpt-auth-config` containing the flat filenames used above,
including the two TOML files, TLS materials, and CSV or database configuration.
Do not commit Secret values. Apply an adapted Deployment and Service:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: rpt-central-auth
spec:
  replicas: 2
  selector:
    matchLabels: {app: rpt-central-auth}
  template:
    metadata:
      labels: {app: rpt-central-auth}
    spec:
      terminationGracePeriodSeconds: 30
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
      containers:
        - name: auth
          image: ghcr.io/hamlink-canada/register-central-auth:1.0.1
          env:
            - {name: RPT_CONFIG_DIR, value: /config}
          ports:
            - {name: grpc, containerPort: 50051}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: [ALL]}
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {cpu: "1", memory: 256Mi}
          volumeMounts:
            - {name: config, mountPath: /config, readOnly: true}
            - {name: temporary, mountPath: /tmp}
          startupProbe:
            exec: {command: [rpt-central-auth-health, startup]}
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 30
          livenessProbe:
            exec: {command: [rpt-central-auth-health, liveness]}
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 3
          readinessProbe:
            exec: {command: [rpt-central-auth-health, readiness]}
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 3
      volumes:
        - name: config
          secret: {secretName: rpt-auth-config, defaultMode: 0440}
        - name: temporary
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: rpt-central-auth
spec:
  selector: {app: rpt-central-auth}
  ports:
    - {name: grpc, port: 50051, targetPort: grpc}
  type: ClusterIP
```

This is a starting deployment example; size resources and limits for your network.
Restrict ingress to registrars/monitoring and egress to required database/DNS
services using your cluster's network policies. No public ingress is provided.

For migrations, use a separate Secret `rpt-auth-migration` containing `url` and any
CA file referenced by that URL. Run this Job deliberately, then deploy the service:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: rpt-credentials-migrate
spec:
  backoffLimit: 1
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
      containers:
        - name: migrate
          image: ghcr.io/hamlink-canada/register-central-auth:1.0.1
          command: [rpt-common-migrate, upgrade, --url-file, /migration/url]
          volumeMounts:
            - {name: migration, mountPath: /migration, readOnly: true}
      volumes:
        - name: migration
          secret: {secretName: rpt-auth-migration, defaultMode: 0440}
```

Configuration, identity, certificate, and CSV changes require a restart. For
Kubernetes use a controlled rolling restart and overlapping identities during
rotation. Registrar credential caches can retain old node passwords until their
TTL expires. SIGTERM reports unready, stops admission, drains requests, and closes
resources within the configured budget. A custom module that ignores its timeout
can cause a nonzero process exit after that budget instead of an indefinite hang.

## Limits and metrics

Defaults: 32 backend lookup workers, 128 concurrent RPCs, 1-second lookups,
10-second startup validation, checks every 10 seconds with a 1-second timeout,
30-second health staleness, and 15-second shutdown. Nested `RPT_RUNTIME__...`
variables configure these values. The probe RPC timeout defaults to one second.

`RPT_LIMITS__...` budgets are per minute: global 60000/burst 2000, per registrar
6000/burst 120, failed authentication per source 30/burst 30, and health per identity
600/burst 60. State is bounded at 16384 entries. CSV snapshots default to 100000
records (`backend.options.max_entries`); SQL lookup pools default to 16 connections; health checks use a separate temporary
connection so lookup saturation does not consume their capacity.
These are operational starting points, not performance guarantees. Replicas require
shared ingress controls and TLS handshake protection. There are no node lockouts.

Enable metrics with `RPT_METRICS__ENABLED=true`. They bind to `127.0.0.1:9100` by
default; choose a private Pod listener explicitly for scraping. Metrics contain
finite outcome labels, latency, backend checks, readiness, and outstanding work,
without passwords, tokens, node IDs, or peers. Do not expose metrics publicly.

## Custom modules

Set `backend.module` to your installed dotted Python module path. API version 1
requires synchronous `initialize`, `lookup`, `check`, and `close` functions:

```python
API_VERSION = 1


def initialize(*, options, timeout): ...
def lookup(node_id, *, state, timeout): ...
def check(*, state, timeout): ...
def close(*, state, timeout): ...
```

Top-level option names are normalized to lowercase so TOML and nested environment
overrides behave alike; option values are unchanged. Initialization returns private
state shared by workers. Lookup returns a password or
None for an unknown/disabled node. Availability failures raise
`rpt_central_auth.errors.BackendUnavailable`; unexpected defects become INTERNAL.
All functions must enforce their timeouts and support concurrent use. Check and
lookup can overlap; do not share a single database session between them. Cleanup
runs only after outstanding calls finish. Extra helpers are allowed but never
exposed automatically over gRPC. Install modules/dependencies in a derived image
at build time; the service never downloads plugins at runtime.

## Build and develop

Use sibling `rpt_protobuf` and `pyCommon` checkouts and Python 3.13+ with uv:

```sh
uv sync --locked --group integration
make check test build
RPT_TEST_POSTGRES=1 uv run --locked --group integration pytest
```

The integration group and sibling registrar checkouts exercise the actual HTTP
route and IAX UDP exchange against each backend. The opt-in PostgreSQL tests start
and remove an isolated official PostgreSQL container on a loopback-only port.

For a local image including the new shared code:

```sh
docker buildx build --load \
  --build-context rpt_protobuf=../rpt_protobuf \
  --build-context pycommon=../pyCommon \
  --tag rpt-central-auth:local .
```

Standard dependency declarations follow both libraries' Git `main` branches. uv uses
editable sibling overrides locally. CI resolves both branches once per run and
shares those exact revisions between tests and image jobs.
Tests target Python 3.13/3.14; image validation targets AMD64/ARM64. Published GitHub releases with `vMAJOR.MINOR.PATCH` tags
publish version and service-commit tags; stable releases also update `latest`.
There is no PyPI publication. Use image digests for exact deployment artifacts.

See [current decisions](docs/decisions.md) for the service boundaries and rationale. The protobuf
contract remains in `rpt_protobuf`; shared models and migrations remain in `pyCommon`.

Images are published only after the release workflow passes its tests and container
checks. Publishing a GitHub prerelease (for example, `v1.0.1-rc.1`) publishes its
version and commit-SHA image tags without updating `latest`. Pushing a tag alone
does not publish an image. Registry authentication uses the built-in `GITHUB_TOKEN`.

## License

[AGPL-3.0-or-later](LICENSE).
