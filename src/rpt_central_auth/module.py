"""Versioned async backend loading with bounded in-flight work."""

import asyncio
import importlib
import inspect

from rpt_common.models.credentials import valid_password

from rpt_central_auth.errors import CapacityExceeded, InvalidCredential


class AsyncSlots:
    """Bound concurrent coroutine calls while preserving cancellation semantics."""

    def __init__(self, workers):
        self.semaphore = asyncio.Semaphore(workers)
        self.active = 0
        self.closed = False

    async def run(self, function, *args, timeout, **kwargs):
        deadline = asyncio.get_running_loop().time() + timeout
        if self.closed:
            raise CapacityExceeded()
        try:
            await asyncio.wait_for(self.semaphore.acquire(), timeout)
        except TimeoutError:
            raise CapacityExceeded() from None
        self.active += 1
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError()
            return await asyncio.wait_for(function(*args, timeout=remaining, **kwargs), remaining)
        finally:
            self.active -= 1
            self.semaphore.release()

    def shutdown(self):
        self.closed = True


class Backend:
    def __init__(self, module, options, *, workers):
        self.module = importlib.import_module(module)
        if (
            type(getattr(self.module, "API_VERSION", None)) is not int
            or self.module.API_VERSION != 2
        ):
            raise ValueError("unsupported backend module API")
        calls = {
            "initialize": ((), {"options": {}, "timeout": 1}),
            "lookup": ((1,), {"state": None, "timeout": 1}),
            "check": ((), {"state": None, "timeout": 1}),
            "close": ((), {"state": None, "timeout": 1}),
        }
        for name, (args, kwargs) in calls.items():
            function = getattr(self.module, name, None)
            if (
                not callable(function)
                or not inspect.iscoroutinefunction(function)
                or inspect.isasyncgenfunction(function)
            ):
                raise ValueError("backend functions must be async coroutines")
            inspect.signature(function).bind(*args, **kwargs)
        self.options = options
        self.lookups = AsyncSlots(workers)
        self.control = AsyncSlots(1)
        self.state = None
        self.initialized = False

    async def start(self, timeout):
        deadline = asyncio.get_running_loop().time() + timeout

        async def initialize_state(*, timeout):
            self.state = await self.module.initialize(options=self.options, timeout=timeout)
            self.initialized = True

        await self.control.run(initialize_state, timeout=timeout)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError()
        await self.check(remaining)

    async def lookup(self, node_id, timeout):
        result = await self.lookups.run(
            self.module.lookup, node_id, state=self.state, timeout=timeout
        )
        if result is not None and not valid_password(result):
            raise InvalidCredential()
        return result

    async def check(self, timeout):
        await self.control.run(self.module.check, state=self.state, timeout=timeout)

    async def close(self, timeout):
        deadline = asyncio.get_running_loop().time() + timeout
        while self.lookups.active or self.control.active:
            if asyncio.get_running_loop().time() >= deadline:
                self.lookups.shutdown()
                self.control.shutdown()
                return False
            await asyncio.sleep(0.01)
        clean = True
        try:
            if self.initialized:
                remaining = max(0.001, deadline - asyncio.get_running_loop().time())
                await self.control.run(self.module.close, state=self.state, timeout=remaining)
        except Exception:
            clean = False
        finally:
            self.lookups.shutdown()
            self.control.shutdown()
        return clean and not self.lookups.active and not self.control.active
