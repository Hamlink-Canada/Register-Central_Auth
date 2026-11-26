"""A test-only installed module, including deliberately uncooperative work."""

from dataclasses import dataclass, field
from threading import Event

API_VERSION = 1


@dataclass
class State:
    entered: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    healthy: bool = True
    block: bool = False
    failure: object = None
    value: str | None = "generated-test-password"
    lookups: int = 0
    closed: bool = False


def initialize(*, options, timeout):
    return State()


def lookup(node_id, *, state, timeout):
    state.lookups += 1
    state.entered.set()
    if state.block:
        state.release.wait(2)
    if state.failure:
        raise state.failure
    return state.value


def check(*, state, timeout):
    if not state.healthy:
        raise OSError("sensitive-backend-error-not-for-clients")


def close(*, state, timeout):
    state.closed = True
