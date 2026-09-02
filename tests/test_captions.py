"""VTT parsing, auto-caption rolling-window cleaning, and track selection."""

import pytest
import yt_dlp

from app import captions as captions_mod
from app.captions import (
    ENGINE_YTA,
    ENGINE_YTDLP,
    Segment,
    Track,
    clean_auto_segments,
    ordered_candidates,
    parse_vtt,
    select_track,
)
from app.errors import AppError

MANUAL_VTT = """WEBVTT
Kind: captions
Language: en

1
00:00:00.000 --> 00:00:02.500
Hello and welcome

00:00:02.500 --> 00:00:04.000
to the show

cue-with-id
00:01:05,120 --> 00:01:08,000
Third cue &amp; done
"""

AUTO_VTT = """WEBVTT

00:00:00.000 --> 00:00:02.000 align:start position:0%


00:00:02.000 --> 00:00:04.000 align:start position:0%
 welcome<00:00:02.199><c> to</c><00:00:02.399><c> the</c><00:00:02.599><c> show</c>

00:00:04.000 --> 00:00:06.000 align:start position:0%
 welcome to the show<00:00:04.199><c> today</c><00:00:04.399><c> we</c><00:00:04.599><c> talk</c>

00:00:06.000 --> 00:00:08.000 align:start position:0%
 welcome to the show today we talk
 about<00:00:06.199><c> stuff</c>

00:00:08.000 --> 00:00:10.000 align:start position:0%
 welcome to the show today we talk about stuff

00:00:10.000 --> 00:00:12.000 align:start position:0%
 welcome to the show today we talk about stuff<00:00:10.199><c> again</c>
"""


def test_parse_vtt_manual_track():
    segments = parse_vtt(MANUAL_VTT)
    assert [(s.start, s.end, s.text) for s in segments] == [
        (0.0, 2.5, "Hello and welcome"),
        (2.5, 4.0, "to the show"),
        # comma timestamps normalize to dots; entity unescaped; cue id skipped
        (65.12, 68.0, "Third cue & done"),
    ]


def test_parse_vtt_strips_inline_tags():
    segments = parse_vtt(AUTO_VTT)
    assert all("<" not in s.text and ">" not in s.text for s in segments)
    assert segments[1].text == "welcome to the show today we talk"


def test_clean_auto_segments_dedupes_rolling_window():
    cleaned = clean_auto_segments(parse_vtt(AUTO_VTT))
    # cue1 empty -> dropped; cue2 full text; cues 3-5 emit only the new tail;
    # cue4 identical to cue3's full text -> merged, emits nothing new.
    assert [(s.start, s.text) for s in cleaned] == [
        (2.0, "welcome to the show"),
        (4.0, "today we talk"),
        (6.0, "about stuff"),
        (10.0, "again"),
    ]
    # identical-repeat timing merge: last real end grows into cue4's end? No --
    # cue4 was identical, so cue3's segment end was extended to 10.0.
    assert cleaned[2].end == 10.0
    assert cleaned[-1].end == 12.0


def test_clean_auto_segments_handles_word_rolling_and_merge():
    raw = [
        Segment(0.0, 2.0, "i just wanna say"),
        Segment(2.0, 4.0, "i just wanna say thank you so much"),
        Segment(4.0, 6.0, "i just wanna say thank you so much"),
    ]
    cleaned = clean_auto_segments(raw)
    assert [(s.start, s.end, s.text) for s in cleaned] == [
        (0.0, 2.0, "i just wanna say"),
        (2.0, 6.0, "thank you so much"),  # new words emitted; identical repeat merges end
    ]


# ----------------------------------------------------------------------
# Track selection
# ----------------------------------------------------------------------

def test_select_prefers_exact_requested():
    tracks = [Track("en", "auto"), Track("fr", "manual"), Track("en-US", "manual")]
    assert select_track(tracks, "fr") == Track("fr", "manual")


def test_select_prefix_match_requested():
    # 'en' matches 'en-US' (and vice versa); manual beats auto on tie
    tracks = [Track("en-US", "manual"), Track("fr", "manual")]
    assert select_track(tracks, "en") == Track("en-US", "manual")
    assert select_track([Track("en", "manual")], "en-US") == Track("en", "manual")


def test_select_no_language_prefers_manual_then_english():
    # any manual beats any auto; 'en' preferred inside the pool
    tracks = [Track("de", "manual"), Track("en", "auto")]
    assert select_track(tracks, None) == Track("de", "manual")
    tracks = [Track("de", "auto"), Track("en-US", "auto"), Track("fr", "auto")]
    assert select_track(tracks, None) == Track("en-US", "auto")


def test_select_deterministic_without_english():
    tracks = [Track("pt-BR", "auto"), Track("de", "auto"), Track("es", "auto")]
    assert select_track(tracks, None) == Track("de", "auto")


def test_select_raises_no_such_language():
    tracks = [Track("en", "manual"), Track("de", "auto")]
    with pytest.raises(AppError) as excinfo:
        select_track(tracks, "fr")
    assert excinfo.value.code == "no_such_language"
    assert excinfo.value.http_status == 404


def test_select_raises_no_captions_when_empty():
    with pytest.raises(AppError) as excinfo:
        select_track([], None)
    assert excinfo.value.code == "no_captions"


def test_ordered_candidates_no_request_all_tracks_manual_first():
    tracks = [Track("de", "auto"), Track("fr", "manual"), Track("en", "auto")]
    ordered = ordered_candidates(tracks, None)
    assert ordered[0] == Track("fr", "manual")  # any manual beats any auto
    assert len(ordered) == 3
    assert sorted(ordered, key=lambda t: (t.language_code, t.kind)) == sorted(
        tracks, key=lambda t: (t.language_code, t.kind)
    )


def test_ordered_candidates_requested_stays_in_family_exact_first():
    tracks = [Track("en", "auto"), Track("en-US", "manual"), Track("fr", "manual")]
    ordered = ordered_candidates(tracks, "en")
    assert ordered == [Track("en", "auto"), Track("en-US", "manual")]
    assert Track("fr", "manual") not in ordered


# ----------------------------------------------------------------------
# Empty-track fallthrough (faked upstreams, no network)
# ----------------------------------------------------------------------

EMPTY_VTT = "WEBVTT\nKind: captions\nLanguage: en\n"  # valid header, zero cues

WHITESPACE_VTT = (
    "WEBVTT\n\n"
    "00:00:00.000 --> 00:00:02.000\n \n\n"
    "00:00:02.000 --> 00:00:04.000\n<c> </c>\n"  # cues whose text strips to nothing
)

DE_VTT = """WEBVTT

00:00:00.000 --> 00:00:02.000
Hallo Welt

00:00:02.000 --> 00:00:04.000
Zweite Zeile
"""

YTDLP_INFO = {
    "title": "T",
    "duration": 10,
    "channel": "C",
    "subtitles": {"en": [{"ext": "vtt"}], "de": [{"ext": "vtt"}]},
    "automatic_captions": {},
}


class _FakeYoutubeDL:
    """yt_dlp.YoutubeDL stand-in: phase 1 returns INFO, phase 2 writes VTTs."""

    vtt_by_lang: dict[str, str] = {}

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        if not download:
            return dict(YTDLP_INFO)
        video_id = url.split("v=")[1]
        lang = self.opts["subtitleslangs"][0]
        # yt-dlp names subtitle files <id>.<lang>.vtt — mirror that so the
        # engine's glob matches, like the real thing.
        path = self.opts["outtmpl"].replace("%(id)s", video_id).replace("%(ext)s", f"{lang}.vtt")
        content = type(self).vtt_by_lang.get(lang)
        if content is not None:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        return {}


class _FakeTranscript:
    def __init__(self, language_code, is_generated):
        self.language_code = language_code
        self.is_generated = is_generated


class _FakeSnippet:
    def __init__(self, start, duration, text):
        self.start = start
        self.duration = duration
        self.text = text


class _FakeFetched(list):
    pass


def _fetched(snippets, language_code="en", is_generated=False):
    out = _FakeFetched(snippets)
    out.language_code = language_code
    out.is_generated = is_generated
    return out


def test_empty_ytdlp_track_skipped_next_candidate_used(monkeypatch):
    _FakeYoutubeDL.vtt_by_lang = {"en": EMPTY_VTT, "de": DE_VTT}
    monkeypatch.setattr(captions_mod.yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    result = captions_mod._fetch_via_ytdlp("abc12345678", None, 60, None)
    assert result.engine == ENGINE_YTDLP
    assert result.language == "de"  # empty-but-valid en track did not win
    assert [s.text for s in result.segments] == ["Hallo Welt", "Zweite Zeile"]


def test_whitespace_only_ytdlp_track_skipped(monkeypatch):
    _FakeYoutubeDL.vtt_by_lang = {"en": WHITESPACE_VTT, "de": DE_VTT}
    monkeypatch.setattr(captions_mod.yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    result = captions_mod._fetch_via_ytdlp("abc12345678", None, 60, None)
    assert result.language == "de"
    assert result.segments


def test_ytdlp_download_error_on_one_candidate_next_candidate_used(monkeypatch):
    # A DownloadError on ONE candidate track must not abandon the loop: the
    # error is remembered as terminal and the remaining candidates are tried
    # (same pattern as the yta loop).
    class _FailFirstYoutubeDL(_FakeYoutubeDL):
        failing_langs = {"en"}

        def extract_info(self, url, download=False):
            if download:
                lang = self.opts["subtitleslangs"][0]
                if lang in type(self).failing_langs:
                    raise yt_dlp.utils.DownloadError(f"track fetch failed: {lang}")
            return super().extract_info(url, download)

    _FailFirstYoutubeDL.vtt_by_lang = {"en": EMPTY_VTT, "de": DE_VTT}
    monkeypatch.setattr(captions_mod.yt_dlp, "YoutubeDL", _FailFirstYoutubeDL)
    result = captions_mod._fetch_via_ytdlp("abc12345678", None, 60, None)
    assert result.engine == ENGINE_YTDLP
    assert result.language == "de"  # en errored -> de was still tried and won
    assert [s.text for s in result.segments] == ["Hallo Welt", "Zweite Zeile"]
    assert result.title == "T"  # phase-1 metadata survives the per-track error


def test_ytdlp_all_candidates_download_error_maps_error(monkeypatch):
    # Every candidate failing keeps the existing mapped-error behavior: the
    # first DownloadError is mapped after the loop is exhausted.
    class _AllFailYoutubeDL(_FakeYoutubeDL):
        def extract_info(self, url, download=False):
            if download:
                raise yt_dlp.utils.DownloadError("simulated per-track failure")
            return super().extract_info(url, download)

    _AllFailYoutubeDL.vtt_by_lang = {"en": EMPTY_VTT, "de": DE_VTT}
    monkeypatch.setattr(captions_mod.yt_dlp, "YoutubeDL", _AllFailYoutubeDL)
    with pytest.raises(AppError) as excinfo:
        captions_mod._fetch_via_ytdlp("abc12345678", None, 60, None)
    assert excinfo.value.code == "upstream_error"
    assert excinfo.value.http_status == 502
    assert "yt-dlp failed:" in excinfo.value.message
    assert "simulated per-track failure" in excinfo.value.message


def test_all_ytdlp_tracks_empty_falls_through_to_yta(monkeypatch):
    _FakeYoutubeDL.vtt_by_lang = {"en": EMPTY_VTT, "de": EMPTY_VTT}
    monkeypatch.setattr(captions_mod.yt_dlp, "YoutubeDL", _FakeYoutubeDL)

    class _YTA:
        def list(self, video_id):
            return [_FakeTranscript("en", False)]

        def fetch(self, video_id, languages=None):
            return _fetched(
                [_FakeSnippet(0.0, 1.0, "hello"), _FakeSnippet(1.0, 1.0, "   ")],
                "en",
                False,
            )

    monkeypatch.setattr(captions_mod, "YouTubeTranscriptApi", _YTA)
    result = captions_mod.fetch_transcript("abc12345678")
    assert result.engine == ENGINE_YTA  # yt-dlp produced nothing usable -> fallback
    assert [s.text for s in result.segments] == ["hello"]


def test_yta_zero_snippets_is_no_captions(monkeypatch):
    class _YTA:
        def list(self, video_id):
            return [_FakeTranscript("en", False)]

        def fetch(self, video_id, languages=None):
            return _fetched([], "en", False)

    monkeypatch.setattr(captions_mod, "YouTubeTranscriptApi", _YTA)
    with pytest.raises(AppError) as excinfo:
        captions_mod._fetch_via_yta("abc12345678", None)
    assert excinfo.value.code == "no_captions"
    assert excinfo.value.http_status == 422


def test_yta_whitespace_only_snippets_is_no_captions(monkeypatch):
    class _YTA:
        def list(self, video_id):
            return [_FakeTranscript("en", True)]

        def fetch(self, video_id, languages=None):
            return _fetched([_FakeSnippet(0.0, 1.0, "  "), _FakeSnippet(1.0, 1.0, "")], "en", True)

    monkeypatch.setattr(captions_mod, "YouTubeTranscriptApi", _YTA)
    with pytest.raises(AppError) as excinfo:
        captions_mod._fetch_via_yta("abc12345678", None)
    assert excinfo.value.code == "no_captions"


# ----------------------------------------------------------------------
# Fallback error attribution (cookies hint accuracy)
# ----------------------------------------------------------------------

def test_terminal_yta_error_does_not_mention_cookies(monkeypatch):
    def fake_ytdlp(video_id, lang, timeout, cookies):
        raise AppError(
            "upstream_error", 502,
            "YouTube is requiring verification. Export cookies and set YOUTUBE_COOKIES_FILE.",
        )

    def fake_yta(video_id, lang):
        raise AppError(
            "upstream_error", 502,
            "YouTube blocked the caption request (bot check / rate limit).",
        )

    monkeypatch.setattr(captions_mod, "_fetch_via_ytdlp", fake_ytdlp)
    monkeypatch.setattr(captions_mod, "_fetch_via_yta", fake_yta)
    with pytest.raises(AppError) as excinfo:
        captions_mod.fetch_transcript("abc12345678")
    # terminal failure came from yta, which cannot use cookies -> no hint
    assert "YOUTUBE_COOKIES_FILE" not in excinfo.value.message
    assert "blocked the caption request" in excinfo.value.message


def test_definitive_ytdlp_error_survives_yta_failure(monkeypatch):
    def fake_ytdlp(video_id, lang, timeout, cookies):
        raise AppError("video_unavailable", 404, "The video is unavailable or does not exist.")

    def fake_yta(video_id, lang):
        raise AppError("upstream_error", 502, "youtube-transcript-api failed: blocked")

    monkeypatch.setattr(captions_mod, "_fetch_via_ytdlp", fake_ytdlp)
    monkeypatch.setattr(captions_mod, "_fetch_via_yta", fake_yta)
    with pytest.raises(AppError) as excinfo:
        captions_mod.fetch_transcript("abc12345678")
    assert excinfo.value.code == "video_unavailable"
    assert excinfo.value.http_status == 404
