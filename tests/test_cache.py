"""Cache roundtrip, TTL expiry, language isolation, and payload shape."""

import sqlite3
import threading
import time

import pytest

from app import cache

DB = "cache.sqlite3"  # replaced per-test with tmp_path


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / DB)


def test_put_get_roundtrip(db):
    payload = {"video_id": "abc12345678", "transcript": "hello", "segments": []}
    cache.put(db, "abc12345678", "en", payload)
    assert cache.get(db, "abc12345678", "en", ttl_days=30) == payload


def test_miss_returns_none(db):
    assert cache.get(db, "missing00001", "en", ttl_days=30) is None


def test_ttl_expiry(db):
    cache.put(db, "abc12345678", "en", {"transcript": "x"})
    # ttl 0 days: everything older than "now" is expired
    assert cache.get(db, "abc12345678", "en", ttl_days=0) is None
    assert cache.get(db, "abc12345678", "en", ttl_days=1) == {"transcript": "x"}


def test_langs_are_independent(db):
    cache.put(db, "abc12345678", "en", {"transcript": "english"})
    cache.put(db, "abc12345678", "de", {"transcript": "deutsch"})
    assert cache.get(db, "abc12345678", "en", ttl_days=30) == {"transcript": "english"}
    assert cache.get(db, "abc12345678", "de", ttl_days=30) == {"transcript": "deutsch"}
    assert cache.get(db, "abc12345678", "fr", ttl_days=30) is None
    assert cache.get(db, "abc12345678", None, ttl_days=30) is None


def test_put_overwrites(db):
    cache.put(db, "abc12345678", "en", {"v": 1})
    cache.put(db, "abc12345678", "en", {"v": 2})
    assert cache.get(db, "abc12345678", "en", ttl_days=30) == {"v": 2}


def test_corrupt_or_missing_db_is_a_miss(tmp_path):
    bad = tmp_path / "not-a-db.sqlite3"
    bad.write_text("this is not sqlite")
    assert cache.get(str(bad), "abc12345678", "en", ttl_days=30) is None


def test_unwritable_db_path_never_raises(tmp_path):
    # A path under an existing *file* makes makedirs/connect fail (OSError);
    # get() must report a miss and put() must stay silent.
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("a file, not a directory")
    impossible = str(blocker / "sub" / "cache.sqlite3")
    cache.put(impossible, "abc12345678", "en", {"transcript": "hello"})  # must not raise
    assert cache.get(impossible, "abc12345678", "en", ttl_days=30) is None


# ----------------------------------------------------------------------
# Non-dict JSON payloads (corrupt/legacy rows) are misses, never errors
# ----------------------------------------------------------------------

def _plant_raw_payload(db, video_id: str, lang: str, raw: str) -> None:
    """Create the schema with a normal put(), then overwrite the payload with
    raw JSON text of any shape via sqlite directly."""
    cache.put(db, video_id, lang, {"seed": True})
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE transcripts SET payload = ? WHERE video_id = ? AND lang = ?",
            (raw, video_id, lang),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("raw", ["[]", '"a bare string"', "null", "42", "[1, 2]"])
def test_non_dict_json_payload_is_a_miss(db, raw):
    _plant_raw_payload(db, "abc12345678", "en", raw)
    assert cache.get(db, "abc12345678", "en", ttl_days=30) is None


def test_non_json_payload_is_a_miss(db):
    # A payload that is not JSON at all (e.g. BLOB b'not json') makes
    # json.loads raise json.JSONDecodeError, a ValueError; get() must report
    # a miss instead of letting it escape the never-raise contract.
    cache.put(db, "abc12345678", "en", {"seed": True})
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE transcripts SET payload = ? WHERE video_id = ? AND lang = ?",
            (b"not json", "abc12345678", "en"),
        )
        conn.commit()
    finally:
        conn.close()
    assert cache.get(db, "abc12345678", "en", ttl_days=30) is None


def test_null_byte_db_path_never_raises(tmp_path):
    # A CACHE_DB_PATH containing a "\0" makes sqlite3.connect (and os calls)
    # raise ValueError; get/put must no-op instead of raising.
    bad = str(tmp_path / "bad\0path.sqlite3")
    cache.put(bad, "abc12345678", "en", {"transcript": "hello"})  # must not raise
    assert cache.get(bad, "abc12345678", "en", ttl_days=30) is None


# ----------------------------------------------------------------------
# Schema-version invalidation: rows written by an older schema version
# (e.g. the previous track-selection algorithm) share (video_id, lang)
# keys with new payloads and must never survive a version bump.
# ----------------------------------------------------------------------

def test_pre_versioning_db_without_meta_is_invalidated(db):
    # A DB written before schema versioning existed: transcripts rows, no
    # meta table at all — exactly what a pre-upgrade deployment has on disk.
    conn = sqlite3.connect(db)
    try:
        conn.execute(cache._SCHEMA)
        conn.execute(
            "INSERT INTO transcripts (video_id, lang, created_at, payload)"
            " VALUES (?, ?, ?, ?)",
            ("abc12345678", "en", int(time.time()), '{"transcript": "stale"}'),
        )
        conn.commit()
    finally:
        conn.close()

    assert cache.get(db, "abc12345678", "en", ttl_days=30) is None  # stale row gone
    cache.put(db, "abc12345678", "en", {"transcript": "fresh"})
    assert cache.get(db, "abc12345678", "en", ttl_days=30) == {"transcript": "fresh"}
    # The version is stamped, so the wipe is one-time, not per start.
    conn = sqlite3.connect(db)
    try:
        (value,) = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    finally:
        conn.close()
    assert value == str(cache.SCHEMA_VERSION)


def test_old_schema_version_rows_are_invalidated(db):
    cache.put(db, "abc12345678", "en", {"transcript": "stale"})
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()

    assert cache.get(db, "abc12345678", "en", ttl_days=30) is None
    cache.put(db, "abc12345678", "en", {"transcript": "fresh"})
    assert cache.get(db, "abc12345678", "en", ttl_days=30) == {"transcript": "fresh"}


def test_same_version_rows_survive_reinit(db):
    cache.put(db, "abc12345678", "en", {"transcript": "keep"})
    # Every get/put call re-runs the lazy init on a fresh connection, so this
    # exercises re-initialization: a matching version must not drop rows.
    for _ in range(3):
        assert cache.get(db, "abc12345678", "en", ttl_days=30) == {"transcript": "keep"}
        assert cache.get(db, "abc12345678", "de", ttl_days=30) is None  # unrelated miss
    # The row is still physically present, not merely re-created empty.
    conn = sqlite3.connect(db)
    try:
        (payload,) = conn.execute(
            "SELECT payload FROM transcripts WHERE video_id = ? AND lang = ?",
            ("abc12345678", "en"),
        ).fetchone()
    finally:
        conn.close()
    assert payload == '{"transcript": "keep"}'


# ----------------------------------------------------------------------
# Atomic migration: the version gate runs in one BEGIN IMMEDIATE write
# transaction, so a delayed/second initializer re-checks the version
# under the write lock and never drops rows written after the winner
# migrated.
# ----------------------------------------------------------------------

def test_double_init_after_legacy_row_keeps_fresh_write(db):
    # Sequential race sketch: init1 migrates a legacy DB, a fresh row is
    # written, then a second connection re-runs the init — the version now
    # matches, so the re-init must NOT drop the fresh row.
    conn = sqlite3.connect(db)
    try:
        conn.execute(cache._SCHEMA)
        conn.execute(
            "INSERT INTO transcripts (video_id, lang, created_at, payload)"
            " VALUES (?, ?, ?, ?)",
            ("abc12345678", "en", int(time.time()), '{"transcript": "stale"}'),
        )
        conn.commit()
    finally:
        conn.close()

    assert cache.get(db, "abc12345678", "en", ttl_days=30) is None  # init1: stale row gone
    cache.put(db, "abc12345678", "en", {"transcript": "fresh"})  # write between inits
    # init2 (fresh connection, full lazy init again) must not drop it.
    assert cache.get(db, "abc12345678", "en", ttl_days=30) == {"transcript": "fresh"}


def test_delayed_initializer_rechecks_version_under_write_lock(tmp_path):
    # The real race the atomicity fix targets, replayed deterministically:
    # a delayed initializer reads the stale schema version, then queues its
    # DROP behind another writer's lock; that writer commits a fresh row and
    # the new version stamp. The BEGIN IMMEDIATE gate forces the delayed
    # initializer to re-read the version under the write lock, so it must
    # not drop — the fresh row must survive alongside the delayed put's row.
    db = str(tmp_path / DB)
    cache.put(db, "seed0000001", "en", {"transcript": "seed"})  # current schema, stamped
    holder = sqlite3.connect(db, timeout=10)
    try:
        holder.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        holder.commit()  # pretend the stamp is legacy: version 1
        holder.execute("BEGIN IMMEDIATE")  # hold the write lock like a concurrent writer
        done = threading.Event()

        def delayed_put() -> None:
            cache.put(db, "fresh000001", "en", {"transcript": "fresh"})
            done.set()

        t = threading.Thread(target=delayed_put)
        t.start()
        time.sleep(0.2)  # let the put read version '1' and block on the write lock
        # The lock holder lands a fresh row and stamps the new version.
        holder.execute(
            "INSERT INTO transcripts (video_id, lang, created_at, payload)"
            " VALUES ('freshwrite', 'en', ?, ?)",
            (int(time.time()), '{"transcript": "written-while-put-waited"}'),
        )
        holder.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(cache.SCHEMA_VERSION),),
        )
        holder.commit()  # release the write lock; the delayed initializer proceeds
        t.join(timeout=10)
        assert done.is_set(), "delayed put never finished"
    finally:
        holder.close()

    assert cache.get(db, "freshwrite", "en", ttl_days=30) == {
        "transcript": "written-while-put-waited"
    }
    assert cache.get(db, "fresh000001", "en", ttl_days=30) == {"transcript": "fresh"}
