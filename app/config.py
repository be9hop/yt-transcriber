"""Environment-driven settings."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _default_cache_path() -> str:
    """Prefer /data (container volume mount); fall back to CWD when missing/unwritable."""
    env = os.environ.get("CACHE_DB_PATH", "").strip()
    if env:
        return env
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return "/data/cache.sqlite3"
    return os.path.join(os.getcwd(), "cache.sqlite3")


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, "").strip() or default)


def _env_origins(name: str) -> tuple[str, ...]:
    """Comma-separated origins -> tuple; empty/missing -> no origins at all."""
    raw = os.environ.get(name, "")
    return tuple(origin.strip() for origin in raw.split(",") if origin.strip())


@dataclass(frozen=True)
class Settings:
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    API_KEY: str | None = None
    CACHE_DB_PATH: str = "cache.sqlite3"
    CACHE_TTL_DAYS: int = 30
    RATE_LIMIT_PER_MINUTE: int = 30
    YOUTUBE_COOKIES_FILE: str | None = None
    REQUEST_TIMEOUT: int = 60
    # Reverse proxies in front of the app that append to X-Forwarded-For.
    # 0 (default) = trust no header, rate-limit by socket peer.
    TRUST_PROXY_COUNT: int = 0
    # Browser origins allowed to call the API cross-origin; empty = CORS off.
    CORS_ORIGINS: tuple[str, ...] = ()

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            HOST=os.environ.get("HOST", "").strip() or "0.0.0.0",
            PORT=_env_int("PORT", 8000),
            API_KEY=os.environ.get("API_KEY", "").strip() or None,
            CACHE_DB_PATH=_default_cache_path(),
            CACHE_TTL_DAYS=_env_int("CACHE_TTL_DAYS", 30),
            RATE_LIMIT_PER_MINUTE=_env_int("RATE_LIMIT_PER_MINUTE", 30),
            YOUTUBE_COOKIES_FILE=os.environ.get("YOUTUBE_COOKIES_FILE", "").strip() or None,
            REQUEST_TIMEOUT=_env_int("REQUEST_TIMEOUT", 60),
            TRUST_PROXY_COUNT=_env_int("TRUST_PROXY_COUNT", 0),
            CORS_ORIGINS=_env_origins("CORS_ORIGINS"),
        )
