"""FastAPI application: routes, auth, rate limiting, CORS, error handling."""

from __future__ import annotations

import logging
import re
import secrets

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app import cache as cache_store
from app import captions
from app import youtube
from app.captions import CaptionResult, Segment
from app.config import Settings
from app.errors import AppError
from app.ratelimit import RateLimiter, client_ip
from app.transcript import render_srt, render_text, render_vtt

logger = logging.getLogger(__name__)

VALID_FORMATS = ("json", "text", "srt", "vtt")

# BCP-47-lite language code: 1-8 alphanumeric subtags joined by hyphens.
# /transcript validates ?lang= against this before it is used anywhere: it
# bars underscores, so the "__info__" /info cache key can never be forged
# via a query param, and bars regex metacharacters before lang reaches
# yt-dlp's subtitleslangs.
_LANG_RE = re.compile(r"^[A-Za-z]{1,8}(-[A-Za-z0-9]{1,8}){0,3}$")

# Pseudo-language cache key for /info metadata. Unreachable from /transcript
# because ?lang= must match _LANG_RE above ("__" contains no letters).
INFO_CACHE_LANG = "__info__"

_USAGE_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>yt-transcriber</title></head>
<body style="font-family:system-ui,sans-serif;max-width:44rem;margin:3rem auto;padding:0 1rem;line-height:1.6">
<h1>yt-transcriber</h1>
<p>Self-hosted YouTube transcript service. Extracts YouTube's own captions
(manual first, auto-generated fallback) &mdash; no speech-to-text.</p>
<h2>Endpoints</h2>
<ul>
<li><code>/transcript?url=&lt;youtube-url&gt;&amp;format=json|text|srt|vtt&amp;lang=&lt;code&gt;&amp;timestamps=true|false&amp;refresh=true|false</code></li>
<li><code>/info?url=&lt;youtube-url&gt;&amp;refresh=true|false</code> &mdash; metadata + available tracks only</li>
<li><code>/health</code> &mdash; liveness probe</li>
</ul>
<h2>Errors</h2>
<p><code>{"error":{"code":"...","message":"..."}}</code></p>
<p>Interactive docs: <a href="/docs">/docs</a> &middot; OpenAPI: <a href="/openapi.json">/openapi.json</a></p>
</body></html>"""


def _transcript_envelope_ok(payload: dict) -> bool:
    """Schema-drift guard for cached /transcript envelopes.

    cache.get only guarantees a dict: the text/srt/vtt renderers index
    envelope["segments"] and format=json splats envelope["transcript"], so a
    hit lacking either (an old-envelope row surviving on a persistent volume)
    must be served as a miss, not a 500 or a 200 with missing fields.
    """
    return isinstance(payload.get("segments"), list) and isinstance(
        payload.get("transcript"), str
    )


def _info_envelope_ok(payload: dict) -> bool:
    """Schema-drift guard for cached /info payloads: available_tracks is
    splatted straight into the response, so it must be a list."""
    return isinstance(payload.get("available_tracks"), list)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app from current settings (env). Module-level `app` uses env at import."""
    settings = settings or Settings.load()
    limiter = RateLimiter(settings.RATE_LIMIT_PER_MINUTE)

    app = FastAPI(title="yt-transcriber", version="1.0.0", docs_url="/docs", redoc_url=None)
    if settings.CORS_ORIGINS:
        # Opt-in only: with no CORS_ORIGINS configured, no CORS headers are
        # emitted at all and browser apps on other origins cannot read us.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.CORS_ORIGINS),
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def require_api_key(request: Request) -> None:
        if not settings.API_KEY:
            return
        # Header only: ?key= is deliberately rejected — URLs leak into proxy
        # logs, browser history, and Referer headers.
        provided = request.headers.get("x-api-key") or ""
        if not secrets.compare_digest(provided.encode(), settings.API_KEY.encode()):
            raise AppError(
                "unauthorized", 401,
                "Missing or invalid API key. Pass the X-API-Key header.",
            )

    def rate_limit(request: Request) -> None:
        if settings.RATE_LIMIT_PER_MINUTE <= 0:
            return  # 0 (or negative) disables rate limiting; no buckets touched
        if not limiter.allow(client_ip(request, settings.TRUST_PROXY_COUNT)):
            raise AppError(
                "rate_limited", 429,
                "Too many requests from your address. Limit is "
                f"{settings.RATE_LIMIT_PER_MINUTE}/minute; retry shortly.",
            )

    guarded = [Depends(rate_limit), Depends(require_api_key)]

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Log the details server-side; the response must never echo str(exc),
        # which can leak paths and other internals to unauthenticated callers.
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected internal error occurred.",
                }
            },
        )

    def _envelope(video_id: str, url: str, result: CaptionResult) -> dict:
        return {
            "video_id": video_id,
            "url": url,
            "title": result.title,
            "channel": result.channel,
            "duration_seconds": result.duration_seconds,
            "language": result.language,
            "is_auto_generated": result.is_auto_generated,
            "engine": result.engine,
            "available_tracks": [
                {"language_code": t.language_code, "kind": t.kind}
                for t in result.available_tracks
            ],
            "transcript": render_text(result.segments, timestamps=False),
            "segments": [
                {"start": s.start, "end": s.end, "text": s.text} for s in result.segments
            ],
        }

    def _load(video_id: str, url: str, lang: str | None, refresh: bool) -> tuple[dict, bool]:
        """Cache-aware envelope load. Returns (envelope, cached)."""
        if not refresh:
            payload = cache_store.get(
                settings.CACHE_DB_PATH, video_id, lang, settings.CACHE_TTL_DAYS
            )
            if payload is not None:
                if _transcript_envelope_ok(payload):
                    payload["url"] = url  # keep the envelope's url pointing at this request
                    return payload, True
                logger.warning(
                    "cache payload for %s (lang=%s) has an invalid envelope shape; "
                    "treating as a miss and refetching",
                    video_id,
                    lang,
                )
        result = captions.fetch_transcript(
            video_id,
            lang=lang,
            request_timeout=settings.REQUEST_TIMEOUT,
            cookies_file=settings.YOUTUBE_COOKIES_FILE,
        )
        envelope = _envelope(video_id, url, result)
        cache_store.put(settings.CACHE_DB_PATH, video_id, lang, envelope)
        return envelope, False

    def _info_load(video_id: str, url: str, refresh: bool = False) -> dict:
        """Cache-aware /info load: metadata only, never the transcript pipeline."""
        if not refresh:
            payload = cache_store.get(
                settings.CACHE_DB_PATH, video_id, INFO_CACHE_LANG, settings.CACHE_TTL_DAYS
            )
            if payload is not None:
                if _info_envelope_ok(payload):
                    payload["url"] = url  # keep the envelope's url pointing at this request
                    return payload
                logger.warning(
                    "cache payload for %s (%s) has an invalid /info shape; "
                    "treating as a miss and refetching",
                    video_id,
                    INFO_CACHE_LANG,
                )
        result = captions.fetch_info(
            video_id,
            request_timeout=settings.REQUEST_TIMEOUT,
            cookies_file=settings.YOUTUBE_COOKIES_FILE,
        )
        payload = {
            "video_id": video_id,
            "url": url,
            "title": result.title,
            "channel": result.channel,
            "duration_seconds": result.duration_seconds,
            # No single track is selected by a metadata-only lookup, so the
            # selected-track fields stay neutral (same keys as before).
            "language": None,
            "is_auto_generated": False,
            "engine": result.engine,
            "available_tracks": [
                {"language_code": t.language_code, "kind": t.kind}
                for t in result.available_tracks
            ],
        }
        cache_store.put(settings.CACHE_DB_PATH, video_id, INFO_CACHE_LANG, payload)
        return payload

    def _segments_of(envelope: dict) -> list[Segment]:
        return [Segment(s["start"], s["end"], s["text"]) for s in envelope["segments"]]

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _USAGE_HTML

    @app.get("/transcript", dependencies=guarded, response_model=None)
    def transcript(
        url: str | None = Query(default=None),
        format: str = Query(default="json"),
        lang: str | None = Query(default=None),
        timestamps: bool = Query(default=False),
        refresh: bool = Query(default=False),
    ) -> Response | dict:
        video_id = youtube.extract_video_id(url)
        if format not in VALID_FORMATS:
            raise AppError(
                "unsupported_format", 400,
                f"Unsupported format {format!r}. Valid formats: {', '.join(VALID_FORMATS)}.",
            )
        if lang is not None and not _LANG_RE.fullmatch(lang):
            # Reject before the cache lookup (the "__info__" key collision)
            # and before lang reaches the caption engines (regex injection
            # via subtitleslangs). The value is agent-supplied, safe to echo.
            raise AppError(
                "no_such_language", 404,
                f"Unsupported language code: {lang}",
            )
        envelope, cached = _load(video_id, url or "", lang, refresh)

        if format == "json":
            return {**envelope, "format": "json", "cached": cached}
        if format == "text":
            body = render_text(_segments_of(envelope), timestamps=timestamps)
            return Response(body, media_type="text/plain; charset=utf-8")
        if format == "srt":
            return Response(
                render_srt(_segments_of(envelope)), media_type="text/plain; charset=utf-8"
            )
        return Response(render_vtt(_segments_of(envelope)), media_type="text/vtt; charset=utf-8")

    @app.get("/info", dependencies=guarded)
    def info(
        url: str | None = Query(default=None),
        refresh: bool = Query(default=False),
    ) -> dict:
        video_id = youtube.extract_video_id(url)
        return _info_load(video_id, url or "", refresh)

    return app


app = create_app()
