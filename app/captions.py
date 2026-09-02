"""Caption extraction: yt-dlp primary engine, youtube-transcript-api fallback.

`fetch_transcript` and `fetch_info` are the module-level entry points imported
by app.main, so tests can monkeypatch them. Engine details are internal.
"""

from __future__ import annotations

import glob
import html
import os
import re
import tempfile
from dataclasses import dataclass

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi

from app.errors import AppError

ENGINE_YTDLP = "yt-dlp"
ENGINE_YTA = "youtube-transcript-api"


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Track:
    language_code: str
    kind: str  # "manual" | "auto"


@dataclass
class CaptionResult:
    segments: list[Segment]
    title: str | None
    channel: str | None
    duration_seconds: float | None
    language: str | None
    is_auto_generated: bool
    available_tracks: list[Track]
    engine: str


@dataclass
class InfoResult:
    """Metadata-only lookup result for /info (no caption track is downloaded)."""

    title: str | None
    channel: str | None
    duration_seconds: float | None
    available_tracks: list[Track]
    engine: str


# --------------------------------------------------------------------------
# VTT parsing
# --------------------------------------------------------------------------

_CUE_RE = re.compile(
    r"^\s*(?P<start>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})"
    r"\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})"
)
_TAG_RE = re.compile(r"<[^>]*>")


def _timestamp_to_seconds(raw: str) -> float:
    raw = raw.replace(",", ".")
    parts = raw.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours, minutes, seconds = "0", parts[0], parts[1]
    else:
        raise ValueError(f"unparseable VTT timestamp: {raw!r}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_vtt(content: str) -> list[Segment]:
    """Parse VTT cue lines into Segments.

    Inline tags (<c>, </c>, <00:00:01.559> word timings, formatting) are
    stripped and HTML entities unescaped; line structure is preserved with
    "\n" so the auto-caption cleaner can reason about rolling lines.
    """
    segments: list[Segment] = []
    start: float | None = None
    end: float | None = None
    lines: list[str] = []

    def flush() -> None:
        nonlocal start, end, lines
        if start is None:
            return
        text = "\n".join(lines).strip()
        if text:
            segments.append(Segment(round(start, 3), round(end or start, 3), text))
        start, end, lines = None, None, []

    for raw_line in content.splitlines():
        match = _CUE_RE.match(raw_line)
        if match:
            flush()
            start = _timestamp_to_seconds(match.group("start"))
            end = _timestamp_to_seconds(match.group("end"))
        elif start is not None:
            text = _collapse(html.unescape(_TAG_RE.sub("", raw_line)))
            if not text:
                flush()  # blank line terminates the cue
            else:
                lines.append(text)
        # Everything else (WEBVTT header, Kind/Language, NOTE, cue IDs) is skipped.
    flush()
    return segments


def clean_auto_segments(segments: list[Segment]) -> list[Segment]:
    """Dedupe YouTube auto-caption rolling windows.

    Auto VTTs repeat previously shown text: a cue is the previous cue's text
    plus one new line (multi-line window) or plus a few new words (single-line
    word rolling, where inline word timings are stripped into one long line).
    Each cue therefore emits only its genuinely-new remainder; a cue identical
    to the previous one just extends the previous segment's end time.
    """
    cleaned: list[Segment] = []
    prev_full = ""  # full collapsed text of the previous cue (the window)
    prev_last = ""  # last line of the previous cue

    for seg in segments:
        lines = [line for line in (_collapse(l) for l in seg.text.splitlines()) if line]
        if not lines:
            continue
        joined = " ".join(lines)

        if prev_full and joined == prev_full:
            if cleaned:
                cleaned[-1].end = seg.end  # identical repeat -> merge timing
            continue

        if prev_full and lines[0].startswith(prev_full + " "):
            # single-line word rolling: cue = previous text + new words inline
            remainder = [lines[0][len(prev_full):].strip()] + lines[1:]
        elif prev_full and lines[0] == prev_full:
            remainder = lines[1:]  # multi-line window: first line repeats everything
        elif prev_full and lines[0] == prev_last:
            remainder = lines[1:]  # 2-line window: first line repeats the last line
        else:
            remainder = list(lines)

        remainder = [line for line in remainder if line]
        if remainder:
            cleaned.append(Segment(seg.start, seg.end, " ".join(remainder)))
        elif cleaned:
            cleaned[-1].end = seg.end  # nothing new -> still grow the timing

        prev_full = joined
        prev_last = lines[-1]

    return cleaned


def _manual_segments(segments: list[Segment]) -> list[Segment]:
    """Manual tracks are clean; just join cue lines and drop empties."""
    out = []
    for seg in segments:
        text = _collapse(" ".join(seg.text.splitlines()))
        if text:
            out.append(Segment(seg.start, seg.end, text))
    return out


# --------------------------------------------------------------------------
# Track selection
# --------------------------------------------------------------------------

def _norm(code: str | None) -> str:
    return (code or "").strip().lower().replace("_", "-")


def _pick(tracks: list[Track]) -> Track:
    return sorted(tracks, key=_pick_key)[0]


def _pick_key(track: Track) -> tuple[int, str]:
    return (0 if track.kind == "manual" else 1, _norm(track.language_code))


def ordered_candidates(tracks: list[Track], requested: str | None) -> list[Track]:
    """All tracks, best-first, for engines that skip unusable tracks.

    Same fallback order as documented below, but every track is returned so
    an engine can continue past a track whose captions parse to nothing.
    With a requested language, candidates stay inside the language family
    (exact matches first, then prefix like 'en' <-> 'en-US'); if the family
    is empty there is no fallback -> no_such_language. With no requested
    language: any manual (English preferred) before any auto (English
    preferred), alphabetical within each group.
    """
    if not tracks:
        raise AppError("no_captions", 422, "This video has no caption tracks available.")

    req = _norm(requested) if requested else None
    if req:
        exact = [t for t in tracks if _norm(t.language_code) == req]
        prefix = [
            t
            for t in tracks
            if t not in exact
            and (
                _norm(t.language_code).startswith(req + "-")
                or req.startswith(_norm(t.language_code) + "-")
            )
        ]
        if not exact and not prefix:
            available = ", ".join(sorted({_norm(t.language_code) for t in tracks}))
            raise AppError(
                "no_such_language", 404,
                f"No caption track for language {requested!r}. Available: {available}",
            )
        return sorted(exact, key=_pick_key) + sorted(prefix, key=_pick_key)

    manual = [t for t in tracks if t.kind == "manual"]
    pool = manual or list(tracks)  # any manual, else any auto
    english = [t for t in pool if _norm(t.language_code) == "en"]
    english = english or [t for t in pool if _norm(t.language_code).startswith("en-")]
    head = _pick(english or sorted(pool, key=lambda t: _norm(t.language_code)))
    rest = sorted(
        (t for t in tracks if t is not head),
        key=lambda t: (0 if t.kind == "manual" else 1, _norm(t.language_code)),
    )
    return [head] + rest


def select_track(tracks: list[Track], requested: str | None) -> Track:
    """Best single track (the head of `ordered_candidates`).

    Fallback order: requested exact -> prefix -> any manual -> any auto.
    Deterministic: manual beats auto on ties, 'en' beats other languages,
    then alphabetical. With a requested language, the whole chain is scoped
    to that language family (exact, then prefix like 'en' <-> 'en-US'); if
    nothing in the family exists there is no fallback -> no_such_language.
    """
    return ordered_candidates(tracks, requested)[0]


# --------------------------------------------------------------------------
# Engine 1: yt-dlp
# --------------------------------------------------------------------------

def _yt_dlp_opts(timeout: int, cookies_file: str | None, **extra) -> dict:
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitlesformat": "vtt/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": timeout,
        # Do NOT pin player_client: 'web' needs PO tokens for subs; defaults work.
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file
    opts.update(extra)
    return opts


def _map_ytdlp_error(exc: Exception) -> AppError:
    msg = str(exc)
    low = msg.lower()
    if (
        "sign in to confirm" in low
        or "not a bot" in low
        or "bot check" in low
        or "http error 429" in low
        or "too many requests" in low
    ):
        return AppError(
            "upstream_error", 502,
            "YouTube is requiring verification for this request (bot check / HTTP 429). "
            "Export cookies to a Netscape file and set YOUTUBE_COOKIES_FILE, then retry.",
        )
    if (
        "video unavailable" in low
        or "private video" in low
        or "removed by the uploader" in low
        or "no longer available" in low
        or "does not exist" in low
    ):
        return AppError("video_unavailable", 404, "The video is unavailable or does not exist.")
    return AppError("upstream_error", 502, f"yt-dlp failed: {msg[:300]}")


def _extract_info(url: str, timeout: int, cookies_file: str | None) -> dict:
    """Phase-1 yt-dlp call: metadata + track lists, nothing downloaded yet."""
    try:
        with yt_dlp.YoutubeDL(_yt_dlp_opts(timeout, cookies_file)) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except yt_dlp.utils.DownloadError as exc:
        raise _map_ytdlp_error(exc) from exc


def _tracks_from_dicts(subtitles: dict, automatic: dict) -> list[Track]:
    def has_vtt(formats) -> bool:
        return any(f.get("ext") == "vtt" for f in formats or [])

    tracks = [
        Track(code, "manual")
        for code in sorted(subtitles)
        if code != "live_chat" and has_vtt(subtitles[code])
    ]
    manual_codes = {_norm(t.language_code) for t in tracks}
    tracks += [
        Track(code, "auto")
        for code in sorted(automatic)
        if code != "live_chat" and has_vtt(automatic[code]) and _norm(code) not in manual_codes
    ]
    return tracks


def _fetch_via_ytdlp(video_id: str, lang: str | None, timeout: int, cookies_file: str | None) -> CaptionResult:
    # Only the sanitized video ID is used to build the URL (SSRF guard).
    url = f"https://www.youtube.com/watch?v={video_id}"
    info = _extract_info(url, timeout, cookies_file)

    tracks = _tracks_from_dicts(info.get("subtitles") or {}, info.get("automatic_captions") or {})
    candidates = ordered_candidates(tracks, lang)  # raises no_captions / no_such_language

    # Phase 2: fetch exactly one VTT per candidate until one yields a real
    # transcript. Setting the write flags per kind also avoids manual/auto
    # filename collisions. A track that parses/cleans to zero segments is
    # malformed upstream output, NOT a transcript — skip it and try the next
    # candidate (and ultimately the next engine). Same for a per-track
    # DownloadError: remember the first one as terminal and keep trying the
    # remaining candidates (mirrors the yta loop below); only map/raise it
    # when every candidate is exhausted.
    terminal: yt_dlp.utils.DownloadError | None = None
    with tempfile.TemporaryDirectory(prefix="yt-transcriber-") as tmp:
        pattern = os.path.join(glob.escape(tmp), f"{video_id}.*.vtt")
        for track in candidates:
            for stale in glob.glob(pattern):
                os.remove(stale)  # never confuse a previous candidate's file

            opts = _yt_dlp_opts(
                timeout,
                cookies_file,
                outtmpl=os.path.join(tmp, "%(id)s.%(ext)s"),
                subtitleslangs=[track.language_code],
                writesubtitles=(track.kind == "manual"),
                writeautomaticsub=(track.kind == "auto"),
            )
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.extract_info(url, download=True)
            except yt_dlp.utils.DownloadError as exc:
                terminal = terminal or exc
                continue  # per-track failure -> try the next candidate

            matches = glob.glob(pattern)
            if not matches:
                continue  # yt-dlp reported the track but wrote no VTT
            with open(matches[0], encoding="utf-8", errors="replace") as fh:
                segments = parse_vtt(fh.read())

            if track.kind == "auto":
                segments = clean_auto_segments(segments)
            else:
                segments = _manual_segments(segments)
            if not segments:
                continue  # empty/unusable track -> next candidate

            duration = info.get("duration")
            return CaptionResult(
                segments=segments,
                title=info.get("title"),
                channel=info.get("channel") or info.get("uploader"),
                duration_seconds=int(duration) if duration else None,
                language=track.language_code,
                is_auto_generated=(track.kind == "auto"),
                available_tracks=tracks,
                engine=ENGINE_YTDLP,
            )

    if terminal is not None:
        raise _map_ytdlp_error(terminal) from terminal
    raise AppError(
        "no_captions", 422,
        "Caption tracks exist but none of them contained usable text.",
    )


# --------------------------------------------------------------------------
# Engine 2: youtube-transcript-api (instance-based API)
# --------------------------------------------------------------------------

def _map_yta_error(exc: Exception) -> AppError:
    name = type(exc).__name__
    msg = str(exc)
    if name == "VideoUnavailable":
        return AppError("video_unavailable", 404, "The video is unavailable or does not exist.")
    if name in ("TranscriptsDisabled", "NoTranscriptFound"):
        if name == "NoTranscriptFound":
            return AppError("no_such_language", 404, f"No caption track for the requested language. {msg[:200]}")
        return AppError("no_captions", 422, "This video has no captions available.")
    if name in ("RequestBlocked", "IpBlocked") or "429" in msg or "blocked" in msg.lower():
        # No cookies hint here: youtube-transcript-api cannot use cookies.
        return AppError(
            "upstream_error", 502,
            "YouTube blocked the caption request (bot check / rate limit). "
            "Try again later, or retry from a different network/IP.",
        )
    return AppError("upstream_error", 502, f"youtube-transcript-api failed: {msg[:300]}")


def _fetch_via_yta(video_id: str, lang: str | None) -> CaptionResult:
    try:
        ytt = YouTubeTranscriptApi()
        tracks = [
            Track(t.language_code, "auto" if t.is_generated else "manual")
            for t in ytt.list(video_id)
        ]
    except Exception as exc:
        raise _map_yta_error(exc) from exc

    candidates = ordered_candidates(tracks, lang)  # raises no_captions / no_such_language

    terminal: Exception | None = None
    for track in candidates:
        try:
            fetched = ytt.fetch(video_id, languages=[track.language_code])
        except Exception as exc:  # per-track failure -> try the next candidate
            terminal = terminal or exc
            continue

        segments = [
            Segment(round(s.start, 3), round(s.start + s.duration, 3), _collapse(s.text))
            for s in fetched
            if _collapse(s.text)
        ]
        if not segments:
            continue  # zero usable snippets -> not a transcript; next candidate

        return CaptionResult(
            segments=segments,
            title=None,  # yta exposes no video metadata
            channel=None,
            duration_seconds=None,
            language=getattr(fetched, "language_code", track.language_code),
            is_auto_generated=bool(getattr(fetched, "is_generated", track.kind == "auto")),
            available_tracks=sorted(tracks, key=lambda t: (t.language_code, t.kind)),
            engine=ENGINE_YTA,
        )

    if terminal is not None:
        raise _map_yta_error(terminal) from terminal
    raise AppError(
        "no_captions", 422,
        "Caption tracks exist but none of them contained usable text.",
    )


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def fetch_transcript(
    video_id: str,
    lang: str | None = None,
    request_timeout: int = 60,
    cookies_file: str | None = None,
) -> CaptionResult:
    """Try yt-dlp first; on no captions or any exception, fall back to yta.

    If both engines fail, a definitive yt-dlp answer (video_unavailable,
    no_captions, no_such_language) is kept; an ambiguous yt-dlp upstream_error
    (e.g. a bot check) is replaced by the youtube-transcript-api error, because
    yta produced the terminal failure and its message never suggests
    YOUTUBE_COOKIES_FILE (yta cannot use cookies).
    """
    primary_error: AppError | None = None
    try:
        return _fetch_via_ytdlp(video_id, lang, request_timeout, cookies_file)
    except AppError as exc:
        primary_error = exc
    except Exception as exc:  # unexpected yt-dlp crash -> still try engine 2
        primary_error = None
        if os.environ.get("YT_TRANSCRIBER_DEBUG"):
            print(f"[yt-dlp] unexpected error: {exc!r}")

    try:
        return _fetch_via_yta(video_id, lang)
    except AppError as yta_error:
        if primary_error is not None and primary_error.code != "upstream_error":
            raise primary_error
        raise yta_error


def fetch_info(
    video_id: str,
    request_timeout: int = 60,
    cookies_file: str | None = None,
) -> InfoResult:
    """Metadata-only lookup for /info: track lists, no caption download.

    yt-dlp's extract_info(download=False) is the primary source; if it fails
    or reports no usable track info, youtube-transcript-api's track listing
    is used (title stays None there — yta exposes no video metadata). Errors
    map like the transcript path (video_unavailable / upstream_error /
    no_captions), with the same terminal-failure attribution rule.
    """
    primary_error: AppError | None = None
    try:
        return _info_via_ytdlp(video_id, request_timeout, cookies_file)
    except AppError as exc:
        primary_error = exc
    except Exception as exc:
        primary_error = None
        if os.environ.get("YT_TRANSCRIBER_DEBUG"):
            print(f"[yt-dlp] unexpected error in fetch_info: {exc!r}")

    try:
        return _info_via_yta(video_id)
    except AppError as yta_error:
        if primary_error is not None and primary_error.code != "upstream_error":
            raise primary_error
        raise yta_error


def _info_via_ytdlp(video_id: str, timeout: int, cookies_file: str | None) -> InfoResult:
    url = f"https://www.youtube.com/watch?v={video_id}"
    info = _extract_info(url, timeout, cookies_file)
    tracks = _tracks_from_dicts(info.get("subtitles") or {}, info.get("automatic_captions") or {})
    if not tracks:
        raise AppError("no_captions", 422, "This video has no caption tracks available.")
    duration = info.get("duration")
    return InfoResult(
        title=info.get("title"),
        channel=info.get("channel") or info.get("uploader"),
        duration_seconds=int(duration) if duration else None,
        available_tracks=tracks,
        engine=ENGINE_YTDLP,
    )


def _info_via_yta(video_id: str) -> InfoResult:
    try:
        transcript_list = YouTubeTranscriptApi().list(video_id)
    except Exception as exc:
        raise _map_yta_error(exc) from exc
    tracks = sorted(
        (Track(t.language_code, "auto" if t.is_generated else "manual") for t in transcript_list),
        key=lambda t: (t.language_code, t.kind),
    )
    if not tracks:
        raise AppError("no_captions", 422, "This video has no caption tracks available.")
    return InfoResult(
        title=None,
        channel=None,
        duration_seconds=None,
        available_tracks=tracks,
        engine=ENGINE_YTA,
    )
