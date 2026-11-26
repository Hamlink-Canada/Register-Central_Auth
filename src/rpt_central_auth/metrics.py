"""Finite labels only: no node IDs, credential values, peers, or service tokens."""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class Metrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "rpt_auth_requests_total",
            "Completed RPCs",
            ["method", "outcome"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "rpt_auth_lookup_seconds", "Credential RPC latency", registry=self.registry
        )
        self.checks = Counter(
            "rpt_auth_backend_checks_total", "Backend checks", ["outcome"], registry=self.registry
        )
        self.inflight = Gauge(
            "rpt_auth_lookup_inflight", "Outstanding provider calls", registry=self.registry
        )
        self.ready = Gauge("rpt_auth_ready", "Backend readiness", registry=self.registry)
