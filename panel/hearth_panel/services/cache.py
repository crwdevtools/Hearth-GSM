"""Tiny async TTL cache with per-key request coalescing."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


class TTLCache:
    def __init__(self, max_entries: int = 512) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self.max_entries = max_entries

    async def get(self, key: str, ttl: float, loader: Callable[[], Awaitable[T]]) -> T:
        hit = self._data.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            value = await loader()
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()  # mark retrieved so lone failures don't warn
            raise
        else:
            future.set_result(value)
            if len(self._data) >= self.max_entries:
                self._data.pop(next(iter(self._data)))
            self._data[key] = (time.monotonic() + ttl, value)
            return value
        finally:
            self._inflight.pop(key, None)

    def invalidate(self, prefix: str = "") -> None:
        for key in [k for k in self._data if k.startswith(prefix)]:
            self._data.pop(key, None)
