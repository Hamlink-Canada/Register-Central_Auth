# Use one authoritative backend per network deployment

Each central credential-service deployment serves one network and selects exactly one credential backend, initially CSV, a trusted operator-installed Python callable, or SQLAlchemy using shared models from `pyCommon`. There is no automatic fallback or chaining: an unavailable authoritative store must not expose an obsolete password from another source. This keeps node IDs unambiguous under the existing node-ID-only RPC, at the cost of separate deployments and credential stores for networks with overlapping IDs.
