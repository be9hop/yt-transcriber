"""Cache roundtrip, TTL expiry, language isolation, and payload shape."""

import sqlite3

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
