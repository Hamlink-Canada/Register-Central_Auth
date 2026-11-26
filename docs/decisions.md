# Current decisions

## Credential lookup and model ownership

Registrars retrieve a node's shared password through the credential lookup RPC and
verify the registration locally. `pyCommon` owns the shared credential model and its
migrations, so services use one schema definition. Central Auth uses the model for
read-only lookup; migrations are explicit administrative jobs.

## One backend per deployment

Each Central Auth deployment selects one configured backend at startup. It does not
fall back between database, CSV, or other backends on a per-request basis. This
keeps availability and authorization behavior predictable during backend failures.

## Service identity is distinct from node credentials

Registrar identity authenticates the service-to-service RPC. Node credentials are
returned only after that identity is authorized for the requested node. These
identities have separate storage and lifecycle.

## Versioned backend interface

Backend modules implement the service's versioned interface and are loaded through
the configured module path. This keeps backend-specific dependencies outside the
RPC layer while giving runtime validation one stable contract.

## Database TLS

Database connections verify the server certificate and hostname by default. Any
verification override must be explicit in configuration; production deployments
should provide the appropriate trust root.
