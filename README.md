# yt-transcriber

A personal, self-hosted HTTP service: send it a YouTube URL, get back the video's
transcript. It extracts YouTube's own captions (manual tracks first, auto-generated
as fallback) rather than running speech-to-text — that makes it fully free, instant,
and dependency-light. The flip side: a video with no captions at all returns a
`no_captions` error instead of a transcription. The API is designed for both humans
and AI agents — predictable JSON envelopes, plain-text/SRT/VTT rendering, and a
one-line instruction you can paste into any agent.

## Quickstart (local, Windows)

```bash
py -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt -r requirements-dev.txt
.venv/Scripts/python -m uvicorn app.main:app --reload
```

On Linux/macOS use `.venv/bin/python` instead of `.venv/Scripts/python`.

Then:

```bash
# JSON envelope (default)
curl "http://127.0.0.1:8000/transcript?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DjNQXAC9IVRw"

# Plain text with [MM:SS] paragraph timestamps
curl "http://127.0.0.1:8000/transcript?url=https%3A%2F%2Fyoutu.be%2FjNQXAC9IVRw&format=text&timestamps=true"

# Metadata + available caption tracks only
curl "http://127.0.0.1:8000/info?url=https%3A%2F%2Fyoutu.be%2FjNQXAC9IVRw"

curl "http://127.0.0.1:8000/health"
```

Interactive docs at `http://127.0.0.1:8000/docs`.

## Docker

```bash
docker build -t yt-transcriber .
docker run -d --name yt-transcriber \
  -p 8000:8000 \
  -e API_KEY=change-me \
  -v yt-transcriber-data:/data \
  yt-transcriber
```

The container runs as a non-root user, stores its cache at `/data/cache.sqlite3`,
and exposes a `HEALTHCHECK` against `/health`.

## API reference

### `GET /transcript`

| Param        | Default | Notes                                                            |
|--------------|---------|------------------------------------------------------------------|
| `url`        | —       | Required. YouTube URL (`watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`, `/live/`, m./music./nocookie hosts). |
| `format`     | `json`  | `json`, `text`, `srt`, `vtt`.                                    |
| `lang`       | —       | Caption language, e.g. `en` or `pt-BR`. Omit for automatic fallback. Must be up to 4 hyphen-joined subtags of 1-8 letters/digits each; anything else is a `404 no_such_language`. |
| `timestamps` | `false` | `text` format only: prefixes each paragraph with `[MM:SS]`.      |
| `refresh`    | `false` | Bypass the cache and rewrite it.                                 |

`json` envelope (always includes the plain-text `transcript` plus `segments`):

```json
{
  "video_id": "jNQXAC9IVRw",
  "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
  "title": "Me at the zoo",
  "channel": "jawed",
  "duration_seconds": 19,
  "language": "en",
  "is_auto_generated": false,
  "engine": "yt-dlp",
  "available_tracks": [{"language_code": "en", "kind": "manual"}],
  "format": "json",
  "cached": false,
  "transcript": "All right, so here we are...",
  "segments": [{"start": 0.0, "end": 2.0, "text": "All right, so here we are..."}]
}
```

`srt`/`vtt` responses return the raw rendered subtitle body (`text/plain` for srt,
`text/vtt` for vtt); `text` returns paragraph-joined plain text.

Language fallback: exact requested language → prefix match (`en` ↔ `en-US`) →
if no language was requested, any manual track (English preferred) → any
auto-generated track (English preferred). Requesting a language the video
doesn't have is a `404 no_such_language`.

### `GET /info?url=`

Metadata + `available_tracks` only — no transcript string, no segments. This
is a lightweight metadata lookup: it never downloads or parses a caption
track (that's what `/transcript` is for), and the result is cached just like
a transcript. Because no single track gets selected here, `language` is
`null` and `is_auto_generated` is `false` in the `/info` response.

It also accepts an optional `refresh` parameter (`true`/`false`, default
`false`) with the same semantics as on `/transcript`: `refresh=true` bypasses
the cache, refetches the track list live, and rewrites the cached entry —
use it when a video's captions were added or changed after `/info` cached a
stale track list.

### `GET /health`

`{"status":"ok"}` — no auth, no rate limit. Also `GET /` returns a small HTML
usage page, and `/docs` serves interactive OpenAPI docs.

### Errors

```json
{"error": {"code": "no_captions", "message": "This video has no caption tracks available."}}
```

| Code                 | Status | Meaning                                                   |
|----------------------|--------|-----------------------------------------------------------|
| `invalid_url`        | 400    | `url` missing or not a strict YouTube video URL            |
| `unsupported_format` | 400    | `format` not in json/text/srt/vtt                          |
| `unauthorized`       | 401    | `API_KEY` set and no/wrong key                             |
| `video_unavailable`  | 404    | Video missing, private, or removed                         |
| `no_such_language`   | 404    | Requested language unsupported, or the video has no captions in it |
| `no_captions`        | 422    | The video genuinely has no captions anywhere               |
| `rate_limited`       | 429    | Token bucket empty for your IP                             |
| `upstream_error`     | 502    | YouTube blocked/errored the fetch (bot check; cookies may help — see above) |

### Hand this to your AI agent

> Fetch `https://<host>/transcript?url=<youtube-url>&format=json` (header `X-API-Key: <key>` if configured) and read `.transcript`.

That single line is the entire integration: the JSON envelope is stable and the
plain-text transcript is always in `.transcript`.

## Environment variables

| Variable               | Default                | Meaning                                                       |
|------------------------|------------------------|---------------------------------------------------------------|
| `HOST`                 | `0.0.0.0`              | Bind address for `uvicorn`.                                   |
| `PORT`                 | `8000`                 | Bind port (Docker CMD reads this).                            |
| `API_KEY`              | *(unset, auth off)*    | When set, `/transcript` and `/info` require the `X-API-Key` header. Query parameters are not accepted (URLs leak into logs/history/Referer). |
| `CACHE_DB_PATH`        | `/data/cache.sqlite3`  | SQLite file; falls back to `./cache.sqlite3` if `/data` is missing/unwritable. Cache failures never fail a request. |
| `CACHE_TTL_DAYS`       | `30`                   | Cache freshness window.                                       |
| `RATE_LIMIT_PER_MINUTE`| `30`                   | Token-bucket size per client IP per minute. `0` (or negative) disables rate limiting entirely. |
| `YOUTUBE_COOKIES_FILE` | *(unset)*              | Netscape cookie jar passed to yt-dlp.                         |
| `REQUEST_TIMEOUT`      | `60`                   | Upstream socket timeout for yt-dlp.                           |
| `TRUST_PROXY_COUNT`    | `0`                    | Number of reverse proxies appending to `X-Forwarded-For` (see below). `0` ignores the header and uses the socket peer. |
| `CORS_ORIGINS`         | *(empty, CORS off)*    | Comma-separated origins allowed to call the API from a browser cross-origin. Empty = no CORS headers at all. Only needed for browser apps on another origin. |

### Client IP and `TRUST_PROXY_COUNT`

Rate limiting keys on the client IP. `TRUST_PROXY_COUNT` is the number of
reverse proxies in front of the app that append to `X-Forwarded-For`:

- `0` (default) — the header is ignored entirely; the socket peer is used.
  Correct for direct exposure or when you don't need per-client limiting.
- `1` — one proxy in front (e.g. Easypanel's Traefik with Cloudflare in
  DNS-only mode). The last XFF entry is used.
- `2` — Cloudflare proxy (orange cloud) in front of Easypanel. The
  second-to-last entry is used (Cloudflare appends the real client IP,
  Traefik appends the Cloudflare edge IP last).

Indexing from the right by the proxy count is not spoofable: a client can
only prepend fake entries, which always sit to the left of the values your
own proxies appended. Setting the count higher than the real chain simply
falls back to the socket peer.

## Authentication & CORS

`API_KEY` guards `/transcript` and `/info` via the `X-API-Key` request header
only. The key is deliberately **not** accepted as a `?key=` query parameter —
URLs end up in proxy logs, browser history, and `Referer` headers. Always set
`API_KEY` on any deployment reachable from the public internet.

CORS is off by default: no `Access-Control-Allow-Origin` headers are emitted,
so web pages on other origins cannot read responses even when auth is off.
If you call this API from a browser app on another origin, opt in with
`CORS_ORIGINS`, e.g. `CORS_ORIGINS=https://app.example.com,https://editor.example.org`.

### About `YOUTUBE_COOKIES_FILE` (bot checks)

YouTube sometimes demands verification ("Sign in to confirm you're not a bot",
HTTP 429) — most often from datacenter IPs like VPS hosts. When that happens the
API returns `502 upstream_error`. The usual fix is exporting your browser's
YouTube cookies to a Netscape-format file (a browser extension like "Get
cookies.txt" can export it) and pointing `YOUTUBE_COOKIES_FILE` at it. Caveat:
you are using your own Google account's session against automation — use a
throwaway account if you're worried, keep the file private (it is account
access), and be aware YouTube may occasionally restrict the account.

## Deploy on Easypanel

Primary path (from a Git repo with a Dockerfile):

1. Push this repo to GitHub.
2. In Easypanel: **New Service → App**, source = your GitHub repo (Easypanel
   builds the `Dockerfile` itself).
3. Set environment variables: `API_KEY=...`, `CACHE_DB_PATH=/data/cache.sqlite3`,
   and `TRUST_PROXY_COUNT=1` (Easypanel's own proxy is the only reverse proxy
   in front of the app).
4. Mount a volume at `/data` (persists the SQLite cache across deploys).
5. Add your domain in the service's Domains tab (Easypanel terminates TLS).
6. Health check: set the path to `/health` (that's what the Dockerfile's
   `HEALTHCHECK` and Easypanel's probe should both hit).

Secondary path (prebuilt image):

```bash
docker build -t ghcr.io/<you>/yt-transcriber:latest .
docker push ghcr.io/<you>/yt-transcriber:latest
```

Then in Easypanel create an App service with **source = image** pointing at
`ghcr.io/<you>/yt-transcriber:latest`, and apply steps 3–6 above.

## Cloudflare in front

Point a `CNAME` record for e.g. `transcript.example.com` at your Easypanel
server host. Proxying (orange cloud) is fine for this service — caption
requests finish in seconds, well within Cloudflare's ~100s proxy timeout. If
you ever add ASR (long jobs), switch that hostname to DNS-only (grey cloud) or
raise the proxy timeout, or jobs will be cut off mid-flight.

Match `TRUST_PROXY_COUNT` to the chain so rate limiting keys on the real
client: `TRUST_PROXY_COUNT=2` with the orange cloud enabled (Cloudflare +
Easypanel both append to `X-Forwarded-For`), `TRUST_PROXY_COUNT=1` in
DNS-only mode (Easypanel only).

## Maintenance

The yt-dlp ↔ YouTube cat-and-mouse game never stops; when YouTube changes
something, extraction breaks until yt-dlp releases a fix. Update regularly:

```bash
# bump the pin in requirements.txt, then
pip install -U yt-dlp
```

…and redeploy. If you start seeing `upstream_error` / bot-check errors, set
`YOUTUBE_COOKIES_FILE` (see above).

## Limitations & roadmap

- Captionless videos fail with `422 no_captions` — there is no ASR in v1.
- When YouTube throttles or bot-checks a request, the API returns
  `502 upstream_error` (retry later / set `YOUTUBE_COOKIES_FILE`) rather than
  silently returning a wrong-language transcript from a mid-request fallback.
- Auto-generated captions are cleaned of rolling-window duplication, but the
  cleaning is heuristic; occasional artifacts remain.
- No job queue: requests are synchronous and fast (seconds).

Roadmap idea (documented, not built): a v2 `faster-whisper` CPU fallback for
captionless videos, backed by an async job endpoint (`POST /jobs` → poll
`GET /jobs/{id}`) so long transcriptions don't sit on an HTTP request.
