"""Bounded token buckets. These limits are local to this process."""

from time import monotonic

from cachetools import TTLCache


class Limits:
    def __init__(self, settings, timer=monotonic):
        self.settings = settings
        self.timer = timer
        self.buckets = TTLCache(settings.STATE_SIZE, 600, timer=timer)

    def bucket(self, key, rate, burst, *, consume=True):
        now = self.timer()
        if key not in self.buckets and len(self.buckets) >= self.buckets.maxsize:
            return False
        tokens, previous = self.buckets.get(key, (float(burst), now))
        tokens = min(float(burst), tokens + max(0, now - previous) * float(rate) / 60)
        allowed = tokens >= 1
        self.buckets[key] = (tokens - 1 if allowed and consume else tokens, now)
        return allowed

    def auth_allowed(self, source):
        return self.bucket(
            ("failed-auth", source),
            self.settings.FAIL_IP_RATE,
            self.settings.FAIL_IP_BURST,
            consume=False,
        )

    def auth_failed(self, source):
        self.bucket(
            ("failed-auth", source), self.settings.FAIL_IP_RATE, self.settings.FAIL_IP_BURST
        )

    def lookup(self, identity):
        return self.bucket(
            ("global",), self.settings.GLOBAL_RATE, self.settings.GLOBAL_BURST
        ) and self.bucket(
            ("registrar", identity), self.settings.REGISTRAR_RATE, self.settings.REGISTRAR_BURST
        )

    def health(self, identity):
        return self.bucket(
            ("health", identity), self.settings.HEALTH_RATE, self.settings.HEALTH_BURST
        )
