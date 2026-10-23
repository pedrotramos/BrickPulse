"""Thread-safe LRU + TTL cache bounded by the total weight of its entries, not their count."""

from __future__ import annotations

from brickpulse.environment import get_env

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from concurrent.futures import Future
from typing import Any

#: Default for ``get`` when a cached ``None`` must be told apart from a miss.
MISS = object()

#: TTL for a result the live read only partly returned (a section missed its budget),
#: so a retry soon gets the whole thing instead of the gap staying for the full TTL.
INCOMPLETE_TTL_S = float(get_env("FINOPS_INCOMPLETE_CACHE_TTL") or "60")


class BoundedCache:
    """Locked: sync endpoints share caches across FastAPI's thread pool."""

    def __init__(
        self, ttl_s: float, max_weight: int, weight: Callable[[Any], int] = lambda _: 1
    ):
        self.ttl_s = ttl_s
        self.max_weight = max_weight
        self._weight = weight
        # key -> (expires_at, value, weight)
        self._entries: OrderedDict[Hashable, tuple[float, Any, int]] = OrderedDict()
        self._total = 0
        self._lock = threading.Lock()
        self._loads: dict[Hashable, Future] = {}

    def get(self, key: Hashable, default: Any = None) -> Any:
        """The live value for ``key`` (marking it recently used), else ``default``."""
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return default
            if time.monotonic() >= hit[0]:
                self._entries.pop(key)
                self._total -= hit[2]
                return default
            self._entries.move_to_end(key)
            return hit[1]

    def put(self, key: Hashable, value: Any, ttl_s: float | None = None) -> None:
        """Store ``value``; ``ttl_s`` shortens this entry's TTL (never lengthens it)."""
        ttl = self.ttl_s if ttl_s is None else min(ttl_s, self.ttl_s)
        w = self._weight(value)
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._total -= old[2]
            if w > self.max_weight or ttl <= 0:
                return
            self._entries[key] = (time.monotonic() + ttl, value, w)
            self._total += w
            # Oversize values were rejected above; evict the least recently used.
            while self._total > self.max_weight and len(self._entries) > 1:
                _, (_, _, evicted) = self._entries.popitem(last=False)
                self._total -= evicted

    def get_or_load(self, key: Hashable, loader: Callable[[], Any]) -> Any:
        """Coalesce concurrent misses. Failed loads are never cached."""
        cached = self.get(key, MISS)
        if cached is not MISS:
            return cached
        with self._lock:
            future = self._loads.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._loads[key] = future
        if not owner:
            return future.result()
        try:
            value = self.get(key, MISS)
            if value is MISS:
                value = loader()
                self.put(key, value)
            future.set_result(value)
            return value
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._loads.pop(key, None)

    @property
    def total_weight(self) -> int:
        return self._total

    def keys(self) -> list[Hashable]:
        """Keys, least recently used first."""
        with self._lock:
            return list(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._total = 0

    def __len__(self) -> int:
        return len(self._entries)
