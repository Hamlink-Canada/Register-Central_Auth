# Central Auth documentation

Central Auth is the credential lookup service for HTTP and IAX registrars. A
registrar proves its service identity over authenticated gRPC, requests a node's
current registration password, and verifies the node's request locally. Central
Auth authorizes the registrar identity and reads from one configured backend; it
does not receive registration requests or perform IAX challenge-response.

```text
Node --registration--> HTTP or IAX registrar --authorized gRPC--> Central Auth
                             |                                      |
                             +-- verifies node password             +-- CSV or SQL backend
                             +-- publishes accepted update to Kafka
```

The main moving pieces are:

- **RPC authentication**: registrar service identity is separate from the node
  password being retrieved. Probe identities can check health without reading
  credentials.
- **Backend module**: one configured implementation serves lookups; the service
  does not fail over to a second backend.
- **Shared model**: `pyCommon` owns the SQLAlchemy model and migrations. Central Auth
  reads the schema; an explicit migration command applies changes.
- **TLS**: server identity is verified by clients. Database connections verify
  their server certificate by default.

Start with the repository [README](../README.md) for a local run and configuration
summary. See [decisions](decisions.md) for the current architectural choices and
[glossary](glossary.md) for domain terms.

For current architectural choices and their rationale, see [decisions](decisions.md).
