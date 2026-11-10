"""Safe categories shared with installed backend modules."""


class BackendUnavailable(Exception):
    """A dependency cannot complete a lookup or health check."""


class InvalidCredential(Exception):
    """Stored data violates the credential contract."""


class CapacityExceeded(Exception):
    """No bounded work slot is available."""
