# Central Registration Credentials

The credential service supplies registration passwords to trusted registrars.
Registrars use those passwords to verify node registrations.

## Language

**Node**:
A repeater endpoint identified by a numeric node ID within its network.

**Registrar**:
A service that authenticates node registration requests and announces accepted registrations to the directory.

**Registration password**:
A system-generated shared secret used to prove a node's identity during registration.

**Credential service**:
A trusted source of registration passwords for authorized registrars.

**Registrar identity**:
The identity of a trusted registrar entitled to retrieve registration passwords for nodes in its network.

**Probe identity**:
An identity permitted to check service health without retrieving node credentials.
