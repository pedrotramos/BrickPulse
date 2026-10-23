from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from brickpulse.server.data.bounded_cache import BoundedCache, MISS


def test_weight_lru_expiry_and_none(monkeypatch):
    import brickpulse.server.data.bounded_cache as module
    now = [100]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    cache = BoundedCache(10, 4, len)
    cache.put("a", [1, 2])
    cache.put("b", [3, 4])
    cache.get("a")
    cache.put("c", [5, 6])
    assert cache.get("b", MISS) is MISS
    cache.put("huge", list(range(5)))
    assert cache.total_weight == 4
    now[0] = 111
    assert cache.get("a", MISS) is MISS
    assert cache.total_weight == 2
    cache.clear()
    assert cache.total_weight == 0
    other = BoundedCache(10, 1)
    other.put("none", None)
    assert other.get("none", MISS) is None


def test_single_flight_and_retry():
    cache = BoundedCache(10, 5)
    started, release = threading.Event(), threading.Event()
    count = []
    def load():
        count.append(1)
        started.set()
        assert release.wait(5)
        return [42]
    with ThreadPoolExecutor(max_workers=8) as pool:
        first = pool.submit(cache.get_or_load, "a", load)
        assert started.wait(5)
        others = [pool.submit(cache.get_or_load, "a", load) for _ in range(7)]
        release.set()
        assert all(f.result() == [42] for f in [first, *others])
    assert len(count) == 1
    def fail():
        raise ValueError("temporary")
    with pytest.raises(ValueError):
        cache.get_or_load("b", fail)
    assert cache.get_or_load("b", lambda: 7) == 7


def test_disabled_cache():
    cache = BoundedCache(0, 5)
    cache.put("a", 1)
    assert cache.get("a", MISS) is MISS
