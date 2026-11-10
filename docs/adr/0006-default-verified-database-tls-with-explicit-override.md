# Verify database TLS by default with an explicit compatibility override

Production PostgreSQL connections use verified TLS by default, with a local Unix socket as an alternative and explicitly unencrypted connections reserved for development. Operators may explicitly disable certificate and hostname verification while retaining TLS encryption, as requested for deployment compatibility. This preserves a verified default while allowing a deliberately less restrictive connection mode whose encryption does not establish the database server's identity.
