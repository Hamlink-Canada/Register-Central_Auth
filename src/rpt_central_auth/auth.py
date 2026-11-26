"""Configured identities; all authenticated registrars have network-wide access."""

import hashlib
import hmac
import re
from dataclasses import dataclass, field


class AuthenticationFailed(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    name: str
    role: str
    digests: tuple = field(repr=False)
    sans: tuple = field(repr=False)


class Authenticator:
    def __init__(self, settings):
        self.mode = settings.AUTH.MODE
        self.insecure = settings.TLS.INSECURE
        self.identities = []
        names, tokens, sans = set(), set(), set()
        for record in settings.AUTH.IDENTITIES:
            record = {str(key).lower(): value for key, value in record.items()}
            name, role = record.get("name"), record.get("role", "registrar")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
                raise ValueError("invalid service identity")
            if name in names or role not in ("registrar", "probe"):
                raise ValueError("ambiguous service identity")
            digests, identities = record.get("token_digests", []), record.get("sans", [])
            if not isinstance(digests, (list, tuple)) or not isinstance(identities, (list, tuple)):
                raise ValueError("identity credentials must be lists")
            if any(not isinstance(d, str) or not re.fullmatch(r"[0-9a-f]{64}", d) for d in digests):
                raise ValueError("invalid SHA-256 token digest")
            if any(
                not isinstance(s, str)
                or not 1 <= len(s) <= 255
                or any(ord(c) < 33 or ord(c) > 126 for c in s)
                for s in identities
            ):
                raise ValueError("invalid certificate SAN identity")
            if tokens.intersection(digests) or sans.intersection(identities):
                raise ValueError("credentials must map to one service identity")
            if self.mode in ("token", "both") and not digests:
                raise ValueError("authentication mode requires token digests")
            if self.mode in ("mtls", "both") and not identities:
                raise ValueError("authentication mode requires certificate SANs")
            names.add(name)
            tokens.update(digests)
            sans.update(identities)
            self.identities.append(Identity(name, role, tuple(digests), tuple(identities)))
        if not any(i.role == "registrar" for i in self.identities):
            raise ValueError("at least one registrar identity is required")

    def authenticate(self, metadata, auth_context):
        if not self.insecure and b"ssl" not in auth_context.get("transport_security_type", []):
            raise AuthenticationFailed()
        supplied = [value for key, value in metadata if key == "authorization"]
        token_identity = None
        if supplied:
            if len(supplied) != 1 or not isinstance(supplied[0], str):
                raise AuthenticationFailed()
            value = supplied[0]
            if not value.startswith("Bearer ") or not 8 <= len(value) <= 4096:
                raise AuthenticationFailed()
            token = value[7:]
            if any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise AuthenticationFailed()
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
            matches = []
            for identity in self.identities:
                # Compare every configured digest, without returning on the first match.
                found = False
                for candidate in identity.digests:
                    found = hmac.compare_digest(candidate, digest) or found
                if found:
                    matches.append(identity)
            if len(matches) != 1:
                raise AuthenticationFailed()
            token_identity = matches[0]
        certificate_identity = None
        if self.mode in ("mtls", "both"):
            try:
                presented = {
                    san.decode("utf-8", errors="strict")
                    for san in auth_context.get("x509_subject_alternative_name", [])
                }
            except (UnicodeError, AttributeError):
                raise AuthenticationFailed() from None
            matches = [
                identity for identity in self.identities if presented.intersection(identity.sans)
            ]
            if len(matches) != 1:
                raise AuthenticationFailed()
            certificate_identity = matches[0]
        if self.mode in ("token", "both") and token_identity is None:
            raise AuthenticationFailed()
        if token_identity and certificate_identity and token_identity != certificate_identity:
            raise AuthenticationFailed()
        identity = token_identity or certificate_identity
        if identity is None:
            raise AuthenticationFailed()
        return identity
