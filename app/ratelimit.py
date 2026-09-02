"""In-memory token bucket rate limiting, keyed by client IP."""

from __future__ import annotations

import threading
import time

# Bucket eviction: the bucket window is per-minute, so a bucket untouched for
# this long is safely stale and can be dropped. The idle sweep alone cannot
# bound the map (fresh keys never go idle), so once over the hard cap the
# least recently used buckets are evicted as well.
_MAX_BUCKETS = 4096
_IDLE_SECONDS = 600.0


def client_ip(request, trust_proxy_count: int) -> str:
    """Best-effort client IP for rate limiting.

    `trust_proxy_count` is the number of reverse proxies in front of the app
    that append to X-Forwarded-For. Every proxy appends its view of the peer
    it received the request from, so with the chain client -> Cloudflare ->
    Traefik -> app, Traefik appends the Cloudflare edge IP (last entry) and
    Cloudflare appends the real client IP (second-to-last). A client can only
    ever PREPEND spoofed values, which therefore always sit to the LEFT of
    the trusted appends — so indexing from the right by the proxy count is
    not spoofable.

    count 0 (default): ignore X-Forwarded-For entirely and use the socket peer.
    count N >= 1: use entries[-N] when the header holds at least N entries,
    else fall back to the socket peer.
    """
    if trust_proxy_count >= 1:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            entries = [entry.strip() for entry in forwarded.split(",")]
            entries = [entry for entry in entries if entry]
            if len(entries) >= trust_proxy_count:
                return entries[-trust_proxy_count]
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """Token bucket: capacity = per_minute, refilled at per_minute/60 tokens/s."""

    def __init__(self, per_minute: int):
        self.capacity = max(1, int(per_minute))
        self._refill_per_second = self.capacity / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_refill)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * self._refill_per_second)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            # Writing (tokens, now) also refreshes last_refill, which doubles
            # as the bucket's last-used timestamp for LRU eviction below.
            self._buckets[key] = (tokens, now)
            if len(self._buckets) > _MAX_BUCKETS:
                self._evict(now)
            return allowed

    def _evict(self, now: float) -> None:
        """Keep the bucket map bounded.

        Buckets idle longer than _IDLE_SECONDS are stale (the window is
        per-minute) and dropped wholesale. But fresh keys never go idle, so
        sustained distinct-key traffic would still grow the map without
        limit; the hard cap below then evicts least-recently-used buckets
        until it fits again. Each allow() inserts at most one key, so the
        LRU pass removes a single entry per overflow, and the O(n) selection
        only ever runs once the map is over the cap.
        """
        stale = [
            key
            for key, (_, last) in self._buckets.items()
            if now - last > _IDLE_SECONDS
        ]
        for key in stale:
            del self._buckets[key]
        while len(self._buckets) > _MAX_BUCKETS:
            lru_key = min(self._buckets, key=lambda k: self._buckets[k][1])
            del self._buckets[lru_key]
