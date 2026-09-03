"""SQLite-backed response cache (stdlib sqlite3, WAL, per-call connections).

The cache is a pure optimization: get()/put() never raise. Any failure
(unwritable path, corrupt/locked DB, ...) is logged and treated as a
miss/skipped write so successful fetches are never turned into errors.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import time
from collections.abc import Iterator

logger = logging.getLogger(__name__)

# Bump when stored payloads change meaning (e.g. a new track-selection
# algorithm): rows from the old code share (video_id, lang) keys with new
# payloads, so the only safe migration is to drop them all.
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    video_id   TEXT    NOT NULL,
    lang       TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    payload    TEXT    NOT NULL,
    PRIMARY KEY (video_id, lang)
)
"""

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
)
"""


def _lang_key(lang: str | None) -> str:
    return "" if lang is None else lang


@contextlib.contextmanager
def _connect(db_path: str) -> Iterator[sqlite3.Connection]:
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        # The whole version gate runs inside one BEGIN IMMEDIATE write
        # transaction (default isolation: BEGIN is explicit, the commit is
        # ours): concurrent first-touch connections serialize on the write
        # lock, and the loser re-reads the version after acquiring it and
        # finds the winner's stamp — so a delayed initializer can no longer
        # drop a fresh row written between its version read and its DROP.
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(_SCHEMA)  # idempotent; keeps lazily-created DBs valid
            conn.execute(_META_SCHEMA)  # idempotent
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None or str(row[0]) != str(SCHEMA_VERSION):
                # Rows written under a different schema version (e.g. by a previous
                # track-selection algorithm) share (video_id, lang) keys but hold
                # results the new code must never serve. The cache is a pure
                # optimization, so dropping every row is always safe; re-stamping
                # the version makes this a one-time cost per change, not per start.
                conn.execute("DROP TABLE IF EXISTS transcripts")
                conn.execute(_SCHEMA)
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(SCHEMA_VERSION),),
                )
            # Migration committed before get/put use the connection below.
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        yield conn
        conn.commit()
    finally:
        conn.close()


def get(db_path: str, video_id: str, lang: str | None, ttl_days: int) -> dict | None:
    """Return the cached payload dict, or None on miss/expiry/corruption/error."""
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT created_at, payload FROM transcripts WHERE video_id = ? AND lang = ?",
                (video_id, _lang_key(lang)),
            ).fetchone()
        if row is None:
            return None
        if time.time() - row[0] > ttl_days * 86_400:
            return None
        payload = json.loads(row[1])
        if not isinstance(payload, dict):
            # Corrupt/legacy row holding valid but non-object JSON ("[]",
            # "null", a bare string, ...): treat it as a miss so callers can
            # never see a non-dict payload and fail on it.
            logger.warning(
                "cache payload is not a JSON object; treating as miss (%s/%s)",
                video_id,
                _lang_key(lang),
            )
            return None
        return payload
    except (sqlite3.Error, OSError, ValueError) as exc:
        # ValueError covers json.JSONDecodeError (payload that is not valid
        # JSON at all, not merely non-object JSON) and the ValueError
        # sqlite3/os raise for a db_path containing a null byte.
        logger.warning("cache read failed; treating as miss (%s): %s", db_path, exc)
        return None
    except TypeError:
        return None


def put(db_path: str, video_id: str, lang: str | None, payload: dict) -> None:
    """Store the payload; failures are logged and swallowed (never raised)."""
    try:
        with _connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO transcripts (video_id, lang, created_at, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(video_id, lang)
                DO UPDATE SET created_at = excluded.created_at, payload = excluded.payload
                """,
                (video_id, _lang_key(lang), int(time.time()), json.dumps(payload, ensure_ascii=False)),
            )
    except (sqlite3.Error, OSError, ValueError) as exc:
        # ValueError covers a null byte in db_path (sqlite3/os raise it) and
        # json.dumps failures like circular references.
        logger.warning("cache write failed; skipping cache (%s): %s", db_path, exc)
