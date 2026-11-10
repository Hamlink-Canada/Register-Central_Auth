# Keep schema migrations with the shared database model

`pyCommon` owns the credential model and versioned migrations; operators run migrations explicitly with writable credentials, while Central Auth uses a read-only database connection and validates compatibility before serving. This lets the future provisioning project share the model without making credential lookups responsible for database changes, at the cost of deployment ordering and separate migration credentials. Central Auth never creates or migrates tables at startup.
