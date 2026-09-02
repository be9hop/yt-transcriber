"""HTTP contract tests against a fake caption engine (no network)."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import cache as cache_store
from app import captions as captions_mod
from app.captions import CaptionResult, InfoResult, Segment, Track
from app.errors import AppError
from app.main import INFO_CACHE_LANG, create_app

VIDEO_URL = "https://www.youtube.com/watch?v=abc12345678"
FAKE_RESULT = CaptionResult(
    segments=[
        Segment(0.0, 2.0, "Hello world."),
        Segment(6.0, 8.0, "Second paragraph here."),
    ],
    title="Test Video",
    channel="Test Channel",
    duration_seconds=8,
    language="en",
    is_auto_generated=False,
    available_tracks=[Track("en", "manual"), Track("de", "auto")],
    engine="yt-dlp",
)
FAKE_INFO = InfoResult(
    title="Test Video",
    channel="Test Channel",
    duration_seconds=8,
    available_tracks=[Track("en", "manual"), Track("de", "auto")],
    engine="yt-dlp",
)


def make_client(monkeypatch, tmp_path, **env) -> TestClient:
    """Fresh app per test: env set first, then create_app() reads it."""
    monkeypatch.setenv("CACHE_DB_PATH", str(tmp_path / "cache.sqlite3"))
    for name in ("API_KEY", "RATE_LIMIT_PER_MINUTE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, str(value))
    return TestClient(create_app())


def patch_engine(monkeypatch, result=None, error=None, calls=None):
    def fake_fetch(video_id, lang=None, request_timeout=60, cookies_file=None):
        if calls is not None:
            calls.append({"video_id": video_id, "lang": lang})
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(captions_mod, "fetch_transcript", fake_fetch)


def patch_info(monkeypatch, result=None, error=None, calls=None):
    def fake_fetch_info(video_id, request_timeout=60, cookies_file=None):
        if calls is not None:
            calls.append({"video_id": video_id})
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(captions_mod, "fetch_info", fake_fetch_info)


# ----------------------------------------------------------------------
# Happy paths
# ----------------------------------------------------------------------

def test_json_envelope(monkeypatch, tmp_path):
    patch_engine(monkeypatch, result=FAKE_RESULT)
    client = make_client(monkeypatch, tmp_path)
    resp = client.get("/transcript", params={"url": VIDEO_URL})
    assert resp.status_code == 200
    data = resp.json()
    assert data["video_id"] == "abc12345678"
    assert data["url"] == VIDEO_URL
    assert data["title"] == "Test Video"
    assert data["channel"] == "Test Channel"
    assert data["duration_seconds"] == 8
    assert data["language"] == "en"
    assert data["is_auto_generated"] is False
    assert data["engine"] == "yt-dlp"
    assert data["available_tracks"] == [
        {"language_code": "en", "kind": "manual"},
        {"language_code": "de", "kind": "auto"},
    ]
    assert data["format"] == "json"
    assert data["cached"] is False
    assert data["transcript"] == "Hello world.\n\nSecond paragraph here."
    assert data["segments"] == [
        {"start": 0.0, "end": 2.0, "text": "Hello world."},
        {"start": 6.0, "end": 8.0, "text": "Second paragraph here."},
    ]


def test_cache_hit_on_second_request(monkeypatch, tmp_path):
    calls = []
    patch_engine(monkeypatch, result=FAKE_RESULT, calls=calls)
    client = make_client(monkeypatch, tmp_path)
    assert client.get("/transcript", params={"url": VIDEO_URL}).json()["cached"] is False
    data = client.get("/transcript", params={"url": VIDEO_URL}).json()
    assert data["cached"] is True
    assert len(calls) == 1  # engine hit once, second response from cache


def test_refresh_bypasses_cache(monkeypatch, tmp_path):
    calls = []
    patch_engine(monkeypatch, result=FAKE_RESULT, calls=calls)
    client = make_client(monkeypatch, tmp_path)
    client.get("/transcript", params={"url": VIDEO_URL})
    data = client.get(
        "/transcript", params={"url": VIDEO_URL, "refresh": "true"}
    ).json()
    assert data["cached"] is False
    assert len(calls) == 2


def test_text_format_paragraphs_and_timestamps(monkeypatch, tmp_path):
    patch_engine(monkeypatch, result=FAKE_RESULT)
    client = make_client(monkeypatch, tmp_path)
    resp = client.get("/transcript", params={"url": VIDEO_URL, "format": "text"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == "Hello world.\n\nSecond paragraph here."

    resp = client.get(
        "/transcript", params={"url": VIDEO_URL, "format": "text", "timestamps": "true"}
    )
    assert resp.text == "[00:00] Hello world.\n\n[00:06] Second paragraph here."


def test_srt_format(monkeypatch, tmp_path):
    patch_engine(monkeypatch, result=FAKE_RESULT)
    client = make_client(monkeypatch, tmp_path)
    resp = client.get("/transcript", params={"url": VIDEO_URL, "format": "srt"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == (
        "1\n00:00:00,000 --> 00:00:02,000\nHello world.\n\n"
        "2\n00:00:06,000 --> 00:00:08,000\nSecond paragraph here.\n"
    )
    assert "segments" not in resp.text  # raw body, not a JSON envelope


def test_vtt_format(monkeypatch, tmp_path):
    patch_engine(monkeypatch, result=FAKE_RESULT)
    client = make_client(monkeypatch, tmp_path)
    resp = client.get("/transcript", params={"url": VIDEO_URL, "format": "vtt"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/vtt")
    assert resp.text.startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:02.000\nHello world." in resp.text
    assert "," not in resp.text  # VTT uses dot milliseconds


def test_lang_is_passed_to_engine(monkeypatch, tmp_path):
    calls = []
    patch_engine(monkeypatch, result=FAKE_RESULT, calls=calls)
    client = make_client(monkeypatch, tmp_path)
    client.get("/transcript", params={"url": VIDEO_URL, "lang": "de"})
    assert calls[0]["lang"] == "de"
    assert calls[0]["video_id"] == "abc12345678"


# ----------------------------------------------------------------------
# lang validation (cache-key collision / subtitleslangs injection guard)
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_lang",
    ["__info__", ".*", "en.*)|(", "en_US", "en US", "-en", "en-", "abcdefghi"],
)
def test_invalid_lang_is_rejected(monkeypatch, tmp_path, bad_lang):
    patch_engine(monkeypatch, result=FAKE_RESULT)
    client = make_client(monkeypatch, tmp_path)
    resp = client.get("/transcript", params={"url": VIDEO_URL, "lang": bad_lang})
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "no_such_language"
    assert body["error"]["message"] == f"Unsupported language code: {bad_lang}"


def test_lang_info_pseudo_key_cannot_reach_info_cache(monkeypatch, tmp_path):
    # /info stores its envelope under the "__info__" pseudo-language cache
    # key; asking /transcript for it must be a clean 404, never the info
    # envelope (json) or a missing-"segments" 500 (other formats).
    patch_info(monkeypatch, result=FAKE_INFO)
    client = make_client(monkeypatch, tmp_path)
    assert client.get("/info", params={"url": VIDEO_URL}).status_code == 200

    resp = client.get("/transcript", params={"url": VIDEO_URL, "lang": "__info__"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "no_such_language"
    assert "title" not in body
    assert "available_tracks" not in body

    resp = client.get(
        "/transcript",
        params={"url": VIDEO_URL, "lang": "__info__", "format": "text"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "no_such_language"


@pytest.mark.parametrize("good_lang", ["en", "en-US", "pt-BR"])
def test_valid_lang_accepted_end_to_end(monkeypatch, tmp_path, good_lang):
    calls = []
    patch_engine(monkeypatch, result=FAKE_RESULT, calls=calls)
    client = make_client(monkeypatch, tmp_path)
    resp = client.get("/transcript", params={"url": VIDEO_URL, "lang": good_lang})
    assert resp.status_code == 200
    data = resp.json()
    assert data["cached"] is False
    assert data["transcript"] == "Hello world.\n\nSecond paragraph here."
    assert calls == [{"video_id": "abc12345678", "lang": good_lang}]


def test_info_endpoint(monkeypatch, tmp_path):
    transcript_calls = []
    patch_engine(monkeypatch, result=FAKE_RESULT, calls=transcript_calls)
    patch_info(monkeypatch, result=FAKE_INFO)
    client = make_client(monkeypatch, tmp_path)
    resp = client.get("/info", params={"url": VIDEO_URL})
    assert resp.status_code == 200
    data = resp.json()
    assert data["video_id"] == "abc12345678"
    assert data["url"] == VIDEO_URL
    assert data["title"] == "Test Video"
    assert data["channel"] == "Test Channel"
    assert data["duration_seconds"] == 8
    assert data["available_tracks"][0] == {"language_code": "en", "kind": "manual"}
    assert "transcript" not in data
    assert "segments" not in data
    # /info must never run the full transcript pipeline
    assert transcript_calls == []


def test_info_endpoint_uses_fetch_info_not_transcript(monkeypatch, tmp_path):
    transcript_calls = []
    patch_engine(monkeypatch, result=FAKE_RESULT, calls=transcript_calls)
    info_calls = []
    patch_info(monkeypatch, result=FAKE_INFO, calls=info_calls)
    client = make_client(monkeypatch, tmp_path)
    assert client.get("/info", params={"url": VIDEO_URL}).status_code == 200
    assert len(info_calls) == 1
    assert info_calls[0]["video_id"] == "abc12345678"
    assert transcript_calls == []


def test_info_second_call_hits_cache(monkeypatch, tmp_path):
    info_calls = []
    patch_info(monkeypatch, result=FAKE_INFO, calls=info_calls)
    client = make_client(monkeypatch, tmp_path)
    first = client.get("/info", params={"url": VIDEO_URL}).json()
    second = client.get("/info", params={"url": VIDEO_URL}).json()
    assert len(info_calls) == 1  # metadata fetched once, served from cache after
    assert first == second


def test_info_refresh_bypasses_cache(monkeypatch, tmp_path):
    info_calls = []
    patch_info(monkeypatch, result=FAKE_INFO, calls=info_calls)
    client = make_client(monkeypatch, tmp_path)
    client.get("/info", params={"url": VIDEO_URL})
    assert len(info_calls) == 1  # normal second call would come from cache
    data = client.get(
        "/info", params={"url": VIDEO_URL, "refresh": "true"}
    ).json()
    assert len(info_calls) == 2  # refresh bypassed the cache and refetched
    assert data["title"] == "Test Video"


def test_info_error_mapping(monkeypatch, tmp_path):
    patch_info(monkeypatch, error=AppError("video_unavailable", 404, "gone"))
    client = make_client(monkeypatch, tmp_path)
    resp = client.get("/info", params={"url": VIDEO_URL})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "video_unavailable"


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------

def test_missing_url_is_invalid_url(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    resp = client.get("/transcript")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_url"


def test_garbage_url_is_invalid_url(monkeypatch, tmp_path):
    patch_engine(monkeypatch, result=FAKE_RESULT)
    client = make_client(monkeypatch, tmp_path)
    resp = client.get("/transcript", params={"url": "https://example.com/watch?v=x"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_url"


def test_unsupported_format(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    resp = client.get("/transcript", params={"url": VIDEO_URL, "format": "mp3"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unsupported_format"


def test_no_captions_422(monkeypatch, tmp_path):
    patch_engine(monkeypatch, error=AppError("no_captions", 422, "none"))
    client = make_client(monkeypatch, tmp_path)
    resp = client.get("/transcript", params={"url": VIDEO_URL})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "no_captions"
    assert isinstance(body["error"]["message"], str)


def test_video_unavailable_404(monkeypatch, tmp_path):
    patch_engine(monkeypatch, error=AppError("video_unavailable", 404, "gone"))
    client = make_client(monkeypatch, tmp_path)
    resp = client.get("/transcript", params={"url": VIDEO_URL})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "video_unavailable"


# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------

def test_auth_required_when_api_key_set(monkeypatch, tmp_path):
    patch_engine(monkeypatch, result=FAKE_RESULT)
    client = make_client(monkeypatch, tmp_path, API_KEY="secret123")

    assert client.get("/transcript", params={"url": VIDEO_URL}).status_code == 401
    resp = client.get(
        "/transcript", params={"url": VIDEO_URL}, headers={"X-API-Key": "wrong"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
    # ?key= is no longer accepted at all — even the correct key must 401
    assert client.get("/transcript", params={"url": VIDEO_URL, "key": "nope"}).status_code == 401
    assert client.get("/transcript", params={"url": VIDEO_URL, "key": "secret123"}).status_code == 401

    ok_header = client.get(
        "/transcript", params={"url": VIDEO_URL}, headers={"X-API-Key": "secret123"}
    )
    assert ok_header.status_code == 200


def test_auth_exempts_health_docs_and_root(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path, API_KEY="secret123")
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_no_auth_when_api_key_unset(monkeypatch, tmp_path):
    patch_engine(monkeypatch, result=FAKE_RESULT)
    client = make_client(monkeypatch, tmp_path)
    assert client.get("/transcript", params={"url": VIDEO_URL}).status_code == 200


# ----------------------------------------------------------------------
# Rate limiting
# ----------------------------------------------------------------------

def test_rate_limit_third_request_429(monkeypatch, tmp_path):
    patch_engine(monkeypatch, result=FAKE_RESULT)
    client = make_client(monkeypatch, tmp_path, RATE_LIMIT_PER_MINUTE=2)
    assert client.get("/transcript", params={"url": VIDEO_URL}).status_code == 200
    assert client.get("/transcript", params={"url": VIDEO_URL}).status_code == 200
    resp = client.get("/transcript", params={"url": VIDEO_URL})
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limited"
    # exempt paths stay open
    assert client.get("/health").status_code == 200


def test_xff_ignored_by_default_limit_from_socket_peer(monkeypatch, tmp_path):
    # Default TRUST_PROXY_COUNT=0: a fresh spoofed X-Forwarded-For on every
    # request must NOT escape the rate limit — the socket peer is the key.
    patch_engine(monkeypatch, result=FAKE_RESULT)
    client = make_client(monkeypatch, tmp_path, RATE_LIMIT_PER_MINUTE=1)
    r1 = client.get(
        "/transcript", params={"url": VIDEO_URL}, headers={"X-Forwarded-For": "1.2.3.4"}
    )
    r2 = client.get(
        "/transcript", params={"url": VIDEO_URL}, headers={"X-Forwarded-For": "5.6.7.8"}
    )
    r3 = client.get(
        "/transcript",
        params={"url": VIDEO_URL},
        headers={"X-Forwarded-For": "9.9.9.9, 10.10.10.10"},
    )
    assert r1.status_code == 200
    assert r2.status_code == 429
    assert r3.status_code == 429


def test_trust_proxy_count_one_keys_on_last_xff_entry(monkeypatch, tmp_path):
    # Count 1: the LAST XFF entry (appended by the trusted proxy) is the key;
    # spoofed values to its left are just noise and share the same bucket.
    patch_engine(monkeypatch, result=FAKE_RESULT)
    client = make_client(monkeypatch, tmp_path, RATE_LIMIT_PER_MINUTE=1, TRUST_PROXY_COUNT=1)
    r1 = client.get(
        "/transcript", params={"url": VIDEO_URL}, headers={"X-Forwarded-For": "1.2.3.4"}
    )
    r2 = client.get(
        "/transcript",
        params={"url": VIDEO_URL},
        headers={"X-Forwarded-For": "spoof-a, 1.2.3.4"},
    )
    r3 = client.get(
        "/transcript",
        params={"url": VIDEO_URL},
        headers={"X-Forwarded-For": "spoof-b, 1.2.3.4"},
    )
    assert r1.status_code == 200
    assert r2.status_code == 429
    assert r3.status_code == 429
    # a genuinely different trusted entry is a separate bucket
    r4 = client.get(
        "/transcript",
        params={"url": VIDEO_URL},
        headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8"},
    )
    assert r4.status_code == 200


def test_trust_proxy_count_above_chain_falls_back_to_socket_peer(monkeypatch, tmp_path):
    # Fewer XFF entries than the trusted count -> socket peer is the key.
    patch_engine(monkeypatch, result=FAKE_RESULT)
    client = make_client(monkeypatch, tmp_path, RATE_LIMIT_PER_MINUTE=1, TRUST_PROXY_COUNT=3)
    r1 = client.get(
        "/transcript", params={"url": VIDEO_URL}, headers={"X-Forwarded-For": "1.2.3.4"}
    )
    r2 = client.get(
        "/transcript", params={"url": VIDEO_URL}, headers={"X-Forwarded-For": "9.9.9.9"}
    )
    assert r1.status_code == 200
    assert r2.status_code == 429


def test_rate_limit_zero_disables_limiting(monkeypatch, tmp_path):
    patch_engine(monkeypatch, result=FAKE_RESULT)
    client = make_client(monkeypatch, tmp_path, RATE_LIMIT_PER_MINUTE=0)
    for _ in range(5):
        assert client.get("/transcript", params={"url": VIDEO_URL}).status_code == 200


def test_rate_limit_negative_disables_limiting(monkeypatch, tmp_path):
    patch_engine(monkeypatch, result=FAKE_RESULT)
    client = make_client(monkeypatch, tmp_path, RATE_LIMIT_PER_MINUTE=-3)
    for _ in range(3):
        assert client.get("/transcript", params={"url": VIDEO_URL}).status_code == 200


def test_health_and_root(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    assert client.get("/health").json() == {"status": "ok"}
    assert "<html" in client.get("/").text.lower()


# ----------------------------------------------------------------------
# Resilience: cache failures and unexpected errors
# ----------------------------------------------------------------------

def test_cache_failure_does_not_break_fetch(monkeypatch, tmp_path):
    # CACHE_DB_PATH under a *file* (not a directory) makes makedirs/connect
    # fail on every call; the transcript endpoint must still succeed.
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("a file, not a directory")
    patch_engine(monkeypatch, result=FAKE_RESULT)
    client = make_client(
        monkeypatch, tmp_path, CACHE_DB_PATH=str(blocker / "sub" / "cache.sqlite3")
    )
    resp = client.get("/transcript", params={"url": VIDEO_URL})
    assert resp.status_code == 200
    data = resp.json()
    assert data["transcript"] == "Hello world.\n\nSecond paragraph here."
    assert data["cached"] is False  # writes silently fail -> always a miss


def test_non_dict_cache_row_causes_clean_refetch(monkeypatch, tmp_path):
    # A row holding valid-but-non-object JSON must behave as a cache miss:
    # clean refetch with fresh data, never a TypeError -> 500.
    calls = []
    patch_engine(monkeypatch, result=FAKE_RESULT, calls=calls)
    client = make_client(monkeypatch, tmp_path)
    db_path = str(tmp_path / "cache.sqlite3")

    cache_store.put(db_path, "abc12345678", None, {"legacy": "row"})  # creates schema
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE transcripts SET payload = '[]' WHERE video_id = ? AND lang = ''",
            ("abc12345678",),
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/transcript", params={"url": VIDEO_URL})
    assert resp.status_code == 200
    data = resp.json()
    assert data["cached"] is False
    assert data["transcript"] == "Hello world.\n\nSecond paragraph here."
    assert len(calls) == 1  # the corrupt row was skipped, engine fetched fresh


def test_non_dict_info_cache_row_causes_clean_refetch(monkeypatch, tmp_path):
    # Same corruption on the /info cache key: a miss and fresh fetch, no 500.
    calls = []
    patch_info(monkeypatch, result=FAKE_INFO, calls=calls)
    client = make_client(monkeypatch, tmp_path)
    db_path = str(tmp_path / "cache.sqlite3")

    cache_store.put(db_path, "abc12345678", INFO_CACHE_LANG, {"legacy": "row"})
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE transcripts SET payload = '\"text\"' WHERE video_id = ? AND lang = ?",
            ("abc12345678", INFO_CACHE_LANG),
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/info", params={"url": VIDEO_URL})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Test Video"
    assert data["available_tracks"] == [
        {"language_code": "en", "kind": "manual"},
        {"language_code": "de", "kind": "auto"},
    ]
    assert len(calls) == 1


def test_drifted_transcript_cache_row_causes_clean_refetch(monkeypatch, tmp_path):
    # A dict-shaped row that lost "segments" (schema drift surviving on a
    # persistent /data volume) must behave exactly like a cache miss:
    # warning + fresh fetch + rewrite, never a KeyError -> 500 or a
    # 200 whose json/text bodies are missing fields.
    calls = []
    patch_engine(monkeypatch, result=FAKE_RESULT, calls=calls)
    client = make_client(monkeypatch, tmp_path)
    db_path = str(tmp_path / "cache.sqlite3")

    # Real envelope via a real put (engine called once)...
    assert client.get("/transcript", params={"url": VIDEO_URL}).status_code == 200
    # ...then drift it: the cached dict no longer has "segments".
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload FROM transcripts WHERE video_id = ? AND lang = ''",
            ("abc12345678",),
        ).fetchone()
        payload = json.loads(row[0])
        del payload["segments"]
        conn.execute(
            "UPDATE transcripts SET payload = ? WHERE video_id = ? AND lang = ''",
            (json.dumps(payload), "abc12345678"),
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/transcript", params={"url": VIDEO_URL})
    assert resp.status_code == 200
    data = resp.json()
    assert data["cached"] is False  # drifted row treated as a miss
    assert len(calls) == 2  # refetched live
    assert data["segments"] == [  # full fresh envelope, not a partial 200
        {"start": 0.0, "end": 2.0, "text": "Hello world."},
        {"start": 6.0, "end": 8.0, "text": "Second paragraph here."},
    ]
    assert data["transcript"] == "Hello world.\n\nSecond paragraph here."


def test_drifted_info_cache_row_causes_clean_refetch(monkeypatch, tmp_path):
    # Same drift on the /info cache key: a row without available_tracks is a
    # miss and a fresh fetch, not a 200 missing the field.
    calls = []
    patch_info(monkeypatch, result=FAKE_INFO, calls=calls)
    client = make_client(monkeypatch, tmp_path)
    db_path = str(tmp_path / "cache.sqlite3")

    assert client.get("/info", params={"url": VIDEO_URL}).status_code == 200
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload FROM transcripts WHERE video_id = ? AND lang = ?",
            ("abc12345678", INFO_CACHE_LANG),
        ).fetchone()
        payload = json.loads(row[0])
        del payload["available_tracks"]
        conn.execute(
            "UPDATE transcripts SET payload = ? WHERE video_id = ? AND lang = ?",
            (json.dumps(payload), "abc12345678", INFO_CACHE_LANG),
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/info", params={"url": VIDEO_URL})
    assert resp.status_code == 200
    data = resp.json()
    assert data["available_tracks"] == [
        {"language_code": "en", "kind": "manual"},
        {"language_code": "de", "kind": "auto"},
    ]
    assert len(calls) == 2  # drifted row was refetched, not served


def test_unexpected_error_returns_generic_envelope(monkeypatch, tmp_path):
    def boom(video_id, lang=None, request_timeout=60, cookies_file=None):
        raise RuntimeError("boom: secret internal detail D:\\paths\\leak")

    monkeypatch.setattr(captions_mod, "fetch_transcript", boom)
    monkeypatch.setenv("CACHE_DB_PATH", str(tmp_path / "cache.sqlite3"))
    for name in ("API_KEY", "RATE_LIMIT_PER_MINUTE"):
        monkeypatch.delenv(name, raising=False)
    client = TestClient(create_app(), raise_server_exceptions=False)

    resp = client.get("/transcript", params={"url": VIDEO_URL})
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["message"] == "An unexpected internal error occurred."
    assert "boom" not in resp.text
    assert "secret" not in resp.text


# ----------------------------------------------------------------------
# CORS (opt-in)
# ----------------------------------------------------------------------

def test_cors_disabled_by_default(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    resp = client.get("/health", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_cors_opt_in_origin_allowed(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path, CORS_ORIGINS="https://app.example")
    resp = client.get("/health", headers={"Origin": "https://app.example"})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "https://app.example"
    # non-listed origins get no allowance
    other = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers
