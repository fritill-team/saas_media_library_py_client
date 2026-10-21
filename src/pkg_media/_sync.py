"""Run the async client from sync code.

``MediaClient`` owns one event loop on a daemon thread; each sync call
submits its coroutine there and waits. One loop per client keeps the HTTP
connection pool and the gRPC channel (both bound to a loop) usable from any
number of caller threads, e.g. FastAPI's threadpool.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


class LoopThread:
    """An event loop running on its own daemon thread."""

    def __init__(self, name: str = "pkg-media-client") -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run ``coro`` on the loop and return its result (or raise its error)."""
        if threading.current_thread() is self._thread:
            coro.close()
            raise RuntimeError("a sync media client was called from its own loop; use AsyncMediaClient there")
        if self.loop.is_closed():
            coro.close()
            raise RuntimeError("the media client is closed")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def stop(self) -> None:
        if self.loop.is_closed():
            return
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join()
        self.loop.close()


class SyncAPI:
    """A sync view of an async API object: its coroutine methods block,
    everything else (constants, ``tenant_id``…) is passed through."""

    def __init__(self, api: Any, runner: LoopThread) -> None:
        self._api = api
        self._runner = runner

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._api, name)
        if not inspect.iscoroutinefunction(attr):
            return attr

        @functools.wraps(attr)
        def call(*args: Any, **kwargs: Any) -> Any:
            return self._runner.run(attr(*args, **kwargs))

        return call

    def __repr__(self) -> str:
        return f"<sync {type(self._api).__name__}>"
