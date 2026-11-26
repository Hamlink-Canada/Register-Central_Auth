"""Bounded token buckets. These limits are local to this process."""

from time import monotonic

from cachetools import TTLCache


class Limits:
    def __init__(self, settings, timer=monotonic):
        self.settings = settings
        self.timer = timer
        self.ttl = max(
            600,
            60 * settings.FAIL_IP_BURST / settings.FAIL_IP_RATE,
            60 * settings.GLOBAL_BURST / settings.GLOBAL_RATE,
            60 * settings.REGISTRAR_BURST / settings.REGISTRAR_RATE,
            60 * settings.HEALTH_BURST / settings.HEALTH_RATE,
        )
        # Unauthenticated source state has an independent budget and cannot fill
        # the authenticated lookup/health stores.
        self.auth_buckets = TTLCache(settings.STATE_SIZE, self.ttl, timer=timer)
        self.lookup_buckets = TTLCache(settings.STATE_SIZE + 1, self.ttl, timer=timer)
        self.health_buckets = TTLCache(settings.STATE_SIZE, self.ttl, timer=timer)

    def bucket(self, buckets, key, rate, burst, *, consume=True):
        now = self.timer()
        if key not in buckets and len(buckets) >= buckets.maxsize:
            return False
        tokens, previous = buckets.get(key, (float(burst), now))
        tokens = min(float(burst), tokens + max(0, now - previous) * float(rate) / 60)
        allowed = tokens >= 1
        buckets[key] = (tokens - 1 if allowed and consume else tokens, now)
        return allowed

    def auth_allowed(self, source):
        return self.bucket(
            self.auth_buckets,
            source,
            self.settings.FAIL_IP_RATE,
            self.settings.FAIL_IP_BURST,
            consume=False,
        )

    def auth_failed(self, source):
        self.bucket(
            self.auth_buckets,
            source,
            self.settings.FAIL_IP_RATE,
            self.settings.FAIL_IP_BURST,
        )

    def lookup(self, identity):
        return self.bucket(
            self.lookup_buckets,
            "global",
            self.settings.GLOBAL_RATE,
            self.settings.GLOBAL_BURST,
        ) and self.bucket(
            self.lookup_buckets,
            identity,
            self.settings.REGISTRAR_RATE,
            self.settings.REGISTRAR_BURST,
        )

    def health(self, identity):
        return self.bucket(
            self.health_buckets,
            identity,
            self.settings.HEALTH_RATE,
            self.settings.HEALTH_BURST,
        )
