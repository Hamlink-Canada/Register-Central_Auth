"""A test-only installed module, including deliberately uncooperative work."""

import asyncio
from dataclasses import dataclass, field

API_VERSION = 2


@dataclass
class State:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    healthy: bool = True
    block: bool = False
    failure: object = None
    value: str | None = "generated-test-password"
    lookups: int = 0
    closed: bool = False


async def initialize(*, options, timeout):
    return State()


async def lookup(node_id, *, state, timeout):
    state.lookups += 1
    state.entered.set()
    if state.block:
        await asyncio.wait_for(state.release.wait(), timeout)
    if state.failure:
        raise state.failure
    return state.value


async def check(*, state, timeout):
    if not state.healthy:
        raise OSError("sensitive-backend-error-not-for-clients")


async def close(*, state, timeout):
    state.closed = True
