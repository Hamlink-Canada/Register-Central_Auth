# Central credential service design

This document records the design interview confirmed by the user. Implementation
now follows these agreed decisions.

## Confirmed decisions

- Python service for credential lookup only, using the existing credential RPC.
- Provisioning, password generation/rotation, and administration belong to another project.
- HTTP verification and IAX challenge-response remain in their registrars.
- Implement CSV, trusted operator-installed Python module, and SQLAlchemy adapters initially.
- Shared database models belong in `pyCommon`; service adapters belong here.
- Exactly one authoritative backend per instance; no fallback or chaining.
- One network per deployment, with unique node IDs in that network. Overlapping
  node IDs in different networks use separate deployments and credential stores.
- The Python service terminates gRPC TLS. Support service-token and mutual-TLS
  authentication; plaintext requires explicit development configuration.

- Initially support PostgreSQL in production and SQLite for local development/testing.
- `pyCommon` owns `NodeCredential`: numeric node primary key, retrievable password,
  and enabled flag. One current password per node; disabled is treated as absent.
- Passwords follow registrar constraints: 1–79 printable ASCII characters without whitespace.
- Custom backends are trusted operator-installed Python modules with multiple functions,
  not a standalone `module:function` lookup callback. The required versioned lifecycle
  is recorded below.
  `None` means unknown/disabled; backend exceptions do not mean an unknown node.
  Configuration passed to modules is backend-specific, not the whole application configuration.
- Registrar identities are separate from node credentials. Configure bearer-token SHA-256
  digests and/or verified client-certificate SAN identities in operator-managed secrets.
- Every authenticated registrar may retrieve credentials for any node in this deployment.
  There are no node allowlists or per-node scopes. Unauthenticated requests are rejected
  before accessing the credential backend.
- Load CSV credentials and identity configuration at startup; restart after file changes.
  Validate CSV completely and reject malformed/duplicate records before accepting traffic.
- No central credential-result cache. Database/callable backends look up each permitted
  request; registrar caches remain responsible for their existing TTL behavior.

- `pyCommon` owns versioned migrations; operators run them explicitly with writable
  credentials. Central Auth uses a read-only database connection and never creates or
  changes tables at startup.
- Deployment authentication mode is token, mTLS, or both; both credentials must resolve
  to the same configured registrar in combined mode. Failed authentication never
  falls back to a weaker mode. Verified client SANs must match configured identities.
- RPC outcomes: absent/disabled node → NOT_FOUND; failed service authentication →
  UNAUTHENTICATED; invalid node ID → INVALID_ARGUMENT; backend failure/timeout →
  UNAVAILABLE; rate/capacity rejection → RESOURCE_EXHAUSTED; corrupt stored credentials
  or unexpected provider defects → INTERNAL. Errors must not expose secrets or tracebacks.
- Custom synchronous functions must enforce finite connection/query timeouts. Service
  worker/admission state is bounded; request cancellation does not release capacity
  while provider work is still running. No process-isolated plugin execution initially.
- Configurable global/per-registrar request budgets and failed-token source-address
  limits, with bounded tracking and no node-wide lockouts. Replicas require shared
  ingress controls; TLS handshake abuse also needs network-level protection.
- Validate configuration and the selected backend before accepting requests; invalid
  CSV/imports or inaccessible/incompatible database fail startup. Later database outages
  return UNAVAILABLE without terminating the process, allowing recovery.
- Provide authenticated standard gRPC health checks and optional metrics on a separate
  private listener. Metrics cover outcomes, latency, failures, saturation, and throttling,
  without credential values or node-ID labels. No administrative HTTP API.
- Docker and Kubernetes are first-class deployment targets and require health checks.

- Ship a Python health-check command for Docker HEALTHCHECK and Kubernetes exec probes.
  It uses verified TLS and configured credentials, finite timeouts, and secret-free exit
  reporting. Dedicated probe identities can check health but cannot retrieve credentials.
- Distinct startup, liveness, and readiness health states. Liveness does not depend on
  backend availability; readiness reflects backend availability and request admission.
  Shutdown reports unready before draining.
- Shared table name is `node_credentials`; node IDs are explicit positive signed-int64
  values, passwords are required, and credentials default to disabled. Validate a known
  supported migration revision; do not assume unknown newer schemas are compatible.
- SQLite is for development/testing; PostgreSQL is the documented production database.
  Missing SQLite files must not be created by the service. Document shared-file/WAL
  requirements; do not recommend SQLite as a shared multi-Pod production database.
- Python package/command/image name for the service: `rpt_central_auth` /
  `rpt-central-auth`. Shared library package: `rpt_common`, with a migration command
  and no standalone runtime image.
- Follow existing Python 3.13+, src layout, uv, Dynaconf RPT_ settings, and
  AGPL-3.0-or-later conventions. Both shared libraries follow Git main; CI resolves
  each once per run. Service images publish through GitHub version tags.
- Configuration, token digest, certificate, and CSV rotation require restart initially.
  Support overlapping tokens/certificate identities and document rolling restarts.
- SIGTERM stops admission, reports unready, drains in-flight requests for a bounded
  period, then closes backend resources. Registrar credential caches retain their
  existing TTL behavior.

- Backend modules implement API_VERSION = 1 and synchronous initialize(options, timeout),
  lookup(node_id, state, timeout), check(state, timeout), and close(state, timeout)
  functions, using keyword arguments except node_id. initialize returns private backend
  state shared across lookup workers; state must support concurrent calls. Functions
  enforce finite timeouts and raise on failure; lookup returns a password or None.
- The module lifecycle replaces the earlier standalone lookup function interface.
  SQLAlchemy state owns its pool; each lookup owns a separate session.
- Modules may expose helpers/additional functions, but Central Auth calls only the defined
  lifecycle and does not expose arbitrary functions over gRPC. Provisioning remains separate.
- Configuration selects an installed dotted module path under backend.module and passes
  only backend.options to initialize. Validate the API version and required functions
  at startup. Install custom modules/dependencies in the image, not via runtime downloads.
- Reserve health handling and backend-check capacity separately from credential lookups.
  Periodic bounded checks update readiness state; probes do not initiate a database query
  per probe. Ordinary rate-limit rejection does not itself make an instance unready.

- Configurable initial operational defaults: gRPC port 50051; 32 concurrent backend
  lookups; 1-second lookup timeout; 10-second initial backend validation timeout;
  periodic checks every 10 seconds with a 1-second timeout; readiness stale after
  30 seconds; 1-second health RPC timeout; 15-second total shutdown budget.
  Recommend at least 30 seconds container termination allowance. These are initial
  limits, not performance guarantees. Request admission and rate tracking stay bounded.
- Production PostgreSQL uses verified TLS by default or a local Unix socket. Provide
  an explicit option to disable certificate/hostname verification while retaining TLS
  encryption. This is distinct from explicitly unencrypted development connections.
  Store database credentials/CA configuration in mounted secrets and use least-privilege
  database access.
- Deliver a non-root Docker image with HEALTHCHECK and README-embedded Docker Compose,
  Kubernetes Deployment/ClusterIP Service, exec probe, secret/configuration, optional
  metrics, and manually invoked migration Job examples. Do not create a separate examples
  tree or Helm chart. Detailed operator guidance belongs in apt-rpt-docs.
- Acceptance covers a common contract for all three backend modules, PostgreSQL/SQLite
  migrations and read-only lookup, token/mTLS/combined/probe-only identities, invalid and
  absent data, outages/deadlines/saturation, independent health capacity, real HTTP/IAX
  registrar integration, container health/non-root/shutdown checks, and Kubernetes
  manifest validation. Clearly report checks that cannot be exercised locally.

## Implementation confirmation

The user confirmed the consolidated design and authorized implementation.
