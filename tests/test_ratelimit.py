"""Client-IP selection (X-Forwarded-For trust count) and bucket eviction."""

import pytest

from app.ratelimit import RateLimiter, client_ip


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, xff=None, host="203.0.113.9"):
        self.headers = {}
        if xff is not None:
            self.headers["x-forwarded-for"] = xff
        self.client = _FakeClient(host) if host is not None else None


# ----------------------------------------------------------------------
# client_ip trust-count semantics
# ----------------------------------------------------------------------

def test_count_zero_ignores_forwarded_for():
    assert client_ip(_FakeRequest(xff="1.2.3.4"), 0) == "203.0.113.9"
    assert client_ip(_FakeRequest(xff="spoofed, 1.2.3.4"), 0) == "203.0.113.9"
    assert client_ip(_FakeRequest(), 0) == "203.0.113.9"


def test_count_one_uses_last_entry():
    assert client_ip(_FakeRequest(xff="1.2.3.4"), 1) == "1.2.3.4"
    # leftmost value is client-controlled noise; the trusted append is last
    assert client_ip(_FakeRequest(xff="spoofed, 1.2.3.4"), 1) == "1.2.3.4"


def test_count_two_uses_second_to_last():
    # client -> Cloudflare -> Traefik -> app: Traefik appended the CF edge IP
    # (last), Cloudflare appended the real client IP (second-to-last).
    req = _FakeRequest(xff="spoofed, 198.51.100.7, 203.0.113.1")
    assert client_ip(req, 2) == "198.51.100.7"


def test_count_above_chain_falls_back_to_socket_peer():
    req = _FakeRequest(xff="1.2.3.4, 5.6.7.8")
    assert client_ip(req, 3) == "203.0.113.9"


def test_no_forwarded_header_falls_back_to_socket_peer():
    assert client_ip(_FakeRequest(), 2) == "203.0.113.9"


def test_no_client_peer_yields_unknown():
    assert client_ip(_FakeRequest(host=None), 0) == "unknown"


# ----------------------------------------------------------------------
# Bounded bucket map (idle sweep + hard LRU cap)
# ----------------------------------------------------------------------

class _FakeTime:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def test_bucket_map_idle_sweep(monkeypatch):
    from app import ratelimit as rl

    fake = _FakeTime()
    monkeypatch.setattr(rl, "time", fake)
    limiter = rl.RateLimiter(5000)  # capacity high enough that every call passes

    for i in range(4100):  # crosses the 4096 cap; all buckets fresh
        assert limiter.allow(f"10.0.0.{i}")
    assert len(limiter._buckets) == rl._MAX_BUCKETS  # hard bound held during the flood

    fake.now += 601.0  # every remaining bucket is now idle > 600s
    assert limiter.allow("10.9.9.9")  # crossing the cap again triggers the sweep
    assert len(limiter._buckets) == 1  # stale buckets dropped, only the new one left

    # bucket accounting still correct after eviction
    assert limiter.allow("10.9.9.9")
    tokens, _ = limiter._buckets["10.9.9.9"]
    assert tokens == pytest.approx(4998.0)

    # an evicted key starts over with a fresh full bucket
    assert limiter.allow("10.0.0.0")
    fresh_tokens, _ = limiter._buckets["10.0.0.0"]
    assert fresh_tokens == pytest.approx(4999.0)


def test_bucket_map_hard_cap_flood(monkeypatch):
    """Sustained distinct-key flood: fresh keys never go idle, so only the
    hard LRU cap keeps the map bounded."""
    from app import ratelimit as rl

    fake = _FakeTime()
    monkeypatch.setattr(rl, "time", fake)
    limiter = rl.RateLimiter(5000)

    for i in range(5000):  # well past the 4096 cap, no idle time ever passes
        assert limiter.allow(f"10.0.0.{i}")
    assert len(limiter._buckets) <= rl._MAX_BUCKETS

    # the just-used key is always the most recent and must never be evicted
    assert "10.0.0.4999" in limiter._buckets
    # the oldest keys were evicted instead
    assert "10.0.0.0" not in limiter._buckets

    # an evicted key starts over with a fresh full bucket (cap 5000, one call used)
    assert limiter.allow("10.0.0.0")
    fresh_tokens, _ = limiter._buckets["10.0.0.0"]
    assert fresh_tokens == pytest.approx(4999.0)


def test_bucket_map_lru_keeps_active_bucket_and_accounting(monkeypatch):
    """A steadily-used bucket survives the flood's LRU eviction and its token
    accounting stays exact after the evictions."""
    from app import ratelimit as rl

    fake = _FakeTime()
    monkeypatch.setattr(rl, "time", fake)
    limiter = rl.RateLimiter(6000)  # refills exactly 100 tokens per second

    for i in range(5000):
        fake.now += 1.0
        assert limiter.allow(f"10.0.0.{i}")  # distinct flood key at time T
        fake.now += 0.5
        assert limiter.allow("active")  # strictly newest entry on every touch

    assert len(limiter._buckets) <= rl._MAX_BUCKETS  # hard bound held
    assert "active" in limiter._buckets              # LRU kept the live bucket
    assert "10.0.0.0" not in limiter._buckets        # oldest flood keys evicted

    # Exact accounting after evictions. Every touch above refilled the active
    # bucket to full (+150 tokens >= capacity), leaving 5999 after the last
    # touch; +1.0s adds 100 (clamped back to full), then ten frozen-clock
    # calls consume one token each with no refill.
    fake.now += 1.0
    assert limiter.allow("active")
    for _ in range(10):
        assert limiter.allow("active")
    tokens, _ = limiter._buckets["active"]
    assert tokens == pytest.approx(6000.0 - 11)
