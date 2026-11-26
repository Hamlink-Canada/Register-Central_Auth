"""Versioned module loading and work slots retained until physical completion."""

import asyncio
import importlib
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor

from rpt_common.models.credentials import valid_password

from rpt_central_auth.errors import CapacityExceeded, InvalidCredential


class BoundedExecutor:
    def __init__(self, workers):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="credential-backend")
        self.slots = threading.BoundedSemaphore(workers)
        self.pending = set()
        self.lock = threading.Lock()
        self.closed = False

    async def run(self, function, *args, timeout, **kwargs):
        with self.lock:
            if self.closed or not self.slots.acquire(blocking=False):
                raise CapacityExceeded()
            try:
                future = self.pool.submit(function, *args, timeout=timeout, **kwargs)
            except BaseException:
                self.slots.release()
                raise
            self.pending.add(future)

        def finished(completed):
            with self.lock:
                self.pending.discard(completed)
            self.slots.release()

        future.add_done_callback(finished)
        wrapped = asyncio.wrap_future(future)
        # Consume late failures after a caller's timeout without logging exception text.
        wrapped.add_done_callback(lambda result: None if result.cancelled() else result.exception())
        return await asyncio.wait_for(asyncio.shield(wrapped), timeout)

    @property
    def active(self):
        with self.lock:
            return len(self.pending)

    def shutdown(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)


class Backend:
    def __init__(self, module, options, *, workers):
        self.module = importlib.import_module(module)
        if (
            type(getattr(self.module, "API_VERSION", None)) is not int
            or self.module.API_VERSION != 1
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
            if not callable(function) or inspect.iscoroutinefunction(function):
                raise ValueError("backend functions must be synchronous")
            inspect.signature(function).bind(*args, **kwargs)
        self.options = options
        self.lookups = BoundedExecutor(workers)
        self.control = BoundedExecutor(1)
        self.state = None
        self.initialized = False

    async def start(self, timeout):
        deadline = asyncio.get_running_loop().time() + timeout

        def initialize_state(*, timeout):
            # Retain ownership even if the caller times out before the module returns.
            state = self.module.initialize(options=self.options, timeout=timeout)
            self.state = state
            self.initialized = True
            return state

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
            await asyncio.sleep(0.02)
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
