from __future__ import annotations

import json
from pathlib import Path

import pytest

from qts_core.cache import DiskCache, RateLimiter


class TestRateLimiter:
    def test_first_call_never_waits(self) -> None:
        waits: list[float] = []
        rl = RateLimiter(2.0, sleep=waits.append)
        assert rl.acquire(now=100.0) == 0.0
        assert waits == []

    def test_second_call_waits_the_remainder(self) -> None:
        waits: list[float] = []
        rl = RateLimiter(2.0, sleep=waits.append)
        rl.acquire(now=100.0)
        waited = rl.acquire(now=100.5)  # only 0.5s elapsed of the 2s minimum
        assert waited == pytest.approx(1.5)
        assert waits == [pytest.approx(1.5)]

    def test_no_wait_when_enough_time_passed(self) -> None:
        waits: list[float] = []
        rl = RateLimiter(2.0, sleep=waits.append)
        rl.acquire(now=100.0)
        assert rl.acquire(now=103.0) == 0.0
        assert waits == []

    def test_never_sleeps_negative(self) -> None:
        waits: list[float] = []
        rl = RateLimiter(1.0, sleep=waits.append)
        rl.acquire(now=100.0)
        rl.acquire(now=500.0)
        assert all(w > 0 for w in waits)

    def test_zero_interval_is_a_noop(self) -> None:
        waits: list[float] = []
        rl = RateLimiter(0.0, sleep=waits.append)
        rl.acquire(now=100.0)
        assert rl.acquire(now=100.0) == 0.0
        assert waits == []

    def test_negative_interval_rejected(self) -> None:
        with pytest.raises(ValueError):
            RateLimiter(-1.0)


class TestDiskCache:
    def test_miss_then_hit(self, tmp_path: Path) -> None:
        c = DiskCache(tmp_path)
        calls = []

        def fetch() -> dict[str, int]:
            calls.append(1)
            return {"x": 1}

        v1, cached1 = c.get_or_fetch("k", fetch)
        v2, cached2 = c.get_or_fetch("k", fetch)
        assert (v1, v2) == ({"x": 1}, {"x": 1})
        assert (cached1, cached2) == (False, True)
        assert len(calls) == 1, "cache did not prevent the second fetch"

    def test_corrupt_entry_behaves_as_miss(self, tmp_path: Path) -> None:
        c = DiskCache(tmp_path)
        c.put("k", {"x": 1})
        # simulate a half-written / garbage file
        next(tmp_path.glob("*.json")).write_text("{not json")
        assert c.get("k") is None
        v, cached = c.get_or_fetch("k", lambda: {"x": 2})
        assert (v, cached) == ({"x": 2}, False)

    def test_write_is_atomic_no_temp_left_behind(self, tmp_path: Path) -> None:
        c = DiskCache(tmp_path)
        c.put("k", {"x": 1})
        assert list(tmp_path.glob("*.tmp")) == []
        assert json.loads(next(tmp_path.glob("*.json")).read_text()) == {"x": 1}

    def test_unsafe_key_is_sanitised(self, tmp_path: Path) -> None:
        c = DiskCache(tmp_path)
        c.put("SPY/../../etc/passwd", {"x": 1})
        written = list(tmp_path.glob("*.json"))
        assert len(written) == 1
        assert ".." not in written[0].name

    def test_missing_key_is_none(self, tmp_path: Path) -> None:
        assert DiskCache(tmp_path).get("nope") is None

    def test_key_can_never_escape_the_cache_directory(self, tmp_path: Path) -> None:
        c = DiskCache(tmp_path)
        for evil in ("../../etc/passwd", "..", "a/b/c", "~/.ssh/id_rsa"):
            p = c._path(evil)
            assert p.parent.resolve() == tmp_path.resolve(), evil
            assert ".." not in p.name, evil
