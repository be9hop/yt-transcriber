"""Strict YouTube URL validation.

This is also the SSRF guard: callers must never pass user URLs to yt-dlp.
Only the 11-character video ID extracted here is ever handed upstream.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from app.errors import AppError

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "youtu.be",
    "www.youtu.be",
}

# Path-embedded IDs, valid on all youtube.com hosts (youtube-nocookie included).
_PATH_PREFIXES = ("/shorts/", "/embed/", "/live/", "/v/")


def _invalid(reason: str) -> AppError:
    return AppError(
        "invalid_url",
        400,
        f"Not a valid YouTube video URL: {reason}. "
        "Expected e.g. https://www.youtube.com/watch?v=VIDEO_ID or https://youtu.be/VIDEO_ID",
    )


def _single_segment(path: str) -> tuple[str, str]:
    """Split '<id>[/extra]' -> (id, extra); extra must be empty or a bare slash."""
    parts = path.split("/")
    return (parts[0] if parts else ""), "/".join(parts[1:])


def extract_video_id(url: str | None) -> str:
    """Return the 11-char video ID for a strictly-YouTube URL, else raise invalid_url."""
    if not url or not url.strip():
        raise _invalid("the 'url' query parameter is missing or empty")
    url = url.strip()

    try:
        parsed = urlparse(url)
    except ValueError as exc:  # malformed URL junk
        raise _invalid("URL could not be parsed") from exc

    if parsed.scheme not in ("http", "https"):
        raise _invalid(f"scheme must be http or https, got {parsed.scheme!r}")

    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise _invalid(f"host {host!r} is not an allowed YouTube host")

    video_id = ""
    extra = ""
    if host.endswith("youtu.be"):
        # https://youtu.be/<id>
        video_id, extra = _single_segment(parsed.path.lstrip("/"))
    elif parsed.path == "/watch":
        video_id = (parse_qs(parsed.query).get("v") or [""])[0]
    else:
        for prefix in _PATH_PREFIXES:
            if parsed.path.startswith(prefix):
                video_id, extra = _single_segment(parsed.path[len(prefix):])
                break

    video_id = video_id.strip()
    if extra.strip("/"):
        raise _invalid(f"unexpected trailing path {extra!r}")
    # Also rejects path-traversal and encoded junk: only [A-Za-z0-9_-]{11} passes.
    if not _VIDEO_ID_RE.match(video_id):
        raise _invalid(f"could not find an 11-character video ID in {host}{parsed.path!r}")

    return video_id
