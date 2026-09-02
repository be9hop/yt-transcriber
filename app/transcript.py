"""Renderers for the supported transcript output formats."""

from __future__ import annotations

from app.captions import Segment

# Paragraph breaks: inter-segment gap (seconds) or accumulated characters.
_PARAGRAPH_GAP_S = 3.0
_PARAGRAPH_MAX_CHARS = 800


def _fmt_clock(seconds: float, ms_sep: str = ".") -> str:
    """HH:MM:SS.mmm (or HH:MM:SS,mmm for SRT), clamped at zero."""
    total_ms = max(0, round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{ms_sep}{ms:03d}"


def _fmt_short(seconds: float) -> str:
    """[MM:SS], growing to [H:MM:SS] past an hour."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"[{hours}:{minutes:02d}:{secs:02d}]"
    return f"[{minutes:02d}:{secs:02d}]"


def render_text(segments: list[Segment], timestamps: bool = False) -> str:
    """Plain text, paragraph-joined: new paragraph on a >3s gap or >~800 chars."""
    paragraphs: list[tuple[float, str]] = []
    current: list[str] = []
    current_start: float | None = None
    current_chars = 0
    prev_end: float | None = None

    for seg in segments:
        gap_broken = prev_end is not None and seg.start - prev_end > _PARAGRAPH_GAP_S
        if current and (gap_broken or current_chars >= _PARAGRAPH_MAX_CHARS):
            paragraphs.append((current_start or 0.0, " ".join(current)))
            current, current_start, current_chars = [], None, 0
        if current_start is None:
            current_start = seg.start
        current.append(seg.text.strip())
        current_chars += len(seg.text) + 1
        prev_end = seg.end

    if current:
        paragraphs.append((current_start or 0.0, " ".join(current)))

    if timestamps:
        return "\n\n".join(f"{_fmt_short(start)} {text}" for start, text in paragraphs)
    return "\n\n".join(text for _, text in paragraphs)


def render_srt(segments: list[Segment]) -> str:
    blocks = [
        f"{i}\n{_fmt_clock(s.start, ',')} --> {_fmt_clock(s.end, ',')}\n{s.text.strip()}"
        for i, s in enumerate(segments, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_vtt(segments: list[Segment]) -> str:
    blocks = [
        f"{_fmt_clock(s.start)} --> {_fmt_clock(s.end)}\n{s.text.strip()}"
        for s in segments
    ]
    return "WEBVTT\n\n" + ("\n\n".join(blocks) + "\n" if blocks else "")
