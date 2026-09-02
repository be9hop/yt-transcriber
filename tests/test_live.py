"""One live end-to-end test against a real, stable video. Opt-in only:

    RUN_LIVE_TESTS=1 python -m pytest -q -m live

Skips gracefully when the network or YouTube's bot checks get in the way.
"""

import os

import pytest

pytestmark = pytest.mark.live

# "Me at the zoo" — the first YouTube video ever uploaded, extremely stable,
# and captioned (manual English track).
VIDEO_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def test_live_transcript(monkeypatch, tmp_path):
    if os.environ.get("RUN_LIVE_TESTS") != "1":
        pytest.skip("set RUN_LIVE_TESTS=1 to run live network tests")

    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("CACHE_DB_PATH", str(tmp_path / "cache.sqlite3"))
    client = TestClient(create_app())

    resp = client.get(
        "/transcript", params={"url": VIDEO_URL, "format": "json"}
    )
    if resp.status_code != 200:
        pytest.skip(
            f"live fetch unavailable right now: HTTP {resp.status_code} {resp.text[:200]}"
        )
    data = resp.json()
    assert data["video_id"] == "jNQXAC9IVRw"
    assert data["transcript"].strip(), "transcript must not be empty"
    assert data["segments"], "segments must not be empty"
    assert data["engine"] in ("yt-dlp", "youtube-transcript-api")
    assert data["cached"] is False

    cached = client.get("/transcript", params={"url": VIDEO_URL}).json()
    assert cached["cached"] is True
