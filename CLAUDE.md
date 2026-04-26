# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a dual-target repository:
1. **`plugin.video.cricfy/`** — A Kodi add-on for streaming live cricket/sports
2. **`stremio-addon/`** — A Stremio addon exposing the same content via FastAPI (in active development, forked from the Kodi plugin)

Both targets share the same core business logic: AES-CBC decryption of provider lists and M3U playlists, Firebase Remote Config discovery, and M3U parsing.

## Environment Setup

All secrets are injected via environment variables at startup — never committed to the repo. Required variables:

| Variable | Format |
|---|---|
| `CRICFY_FIREBASE_API_KEY` | String |
| `CRICFY_FIREBASE_APP_ID` | `1:NUMBER:android:HEX` |
| `CRICFY_PACKAGE_NAME` | Java package name |
| `CRICFY_SECRET1` | `hex_key:hex_iv` (hex-encoded AES key and IV) |
| `CRICFY_SECRET2` | `hex_key:hex_iv` (fallback, optional) |
| `HOST` | Stremio only, default `0.0.0.0` |
| `PORT` | Stremio only, default `8000` |

The root `main.py` and `stremio-addon/main.py` read these vars and write `resources/secret1.txt`, `resources/secret2.txt`, and `resources/cricfy_properties.json` at runtime. These files are git-ignored.

## Running the Stremio Addon

```bash
cd stremio-addon
cp .env.example .env  # fill in credentials
pip install -r requirements.txt  # or: uv sync
python main.py        # starts uvicorn on HOST:PORT
```

Or with Docker:
```bash
cd stremio-addon
docker build -t cricfy-stremio .
docker run -p 8000:8000 --env-file .env cricfy-stremio
```

## Running Tests

```bash
cd stremio-addon
python -m unittest tests/test_proxy_rewrite.py
```

The test suite covers proxy URL rewriting logic for HLS/DASH/DRM manifests (6 tests).

## Architecture

### Core Data Flow

1. **Firebase Remote Config** (`lib/remote_config.py`) — spoofs an Android client to fetch provider endpoint URLs from Firebase
2. **Provider fetch** (`lib/providers.py`) — downloads encrypted provider lists, decrypts via `lib/crypto_utils.py`
3. **M3U parsing** (`lib/m3u_parser.py`) — parses decrypted M3U into `PlaylistItem` objects with title, URL, headers, and optional DRM keys
4. **Playback** — Kodi hands off to `inputstream.adaptive`; Stremio proxies everything through FastAPI

### Stremio Proxy Architecture (Critical)

Players cannot carry custom HTTP headers (Cookie, User-Agent, Referer) in stream requests, so the Stremio addon proxies all content server-side:

- `/proxy/stream?id={channel_id}` — fetches the raw stream URL (M3U8/MPD) with injected headers, then rewrites all nested URLs before returning
- `/proxy/media?id={channel_id}&url={encoded_url}` — serves individual segments/keys with channel-specific headers applied

DASH (MPD) manifests are rewritten with ElementTree; HLS (M3U8) manifests are rewritten line-by-line. Both absolute and relative URLs must be handled.

**Critical implementation notes for the proxy (`app.py`)**:
- A single module-level `httpx.AsyncClient` is shared across all requests (connection pooling). It's initialized in the FastAPI `lifespan` context manager. Never create per-request clients.
- `lib/req.py` uses the **synchronous** `requests` library. Calling `get_providers()` / `get_channels()` from an async route without wrapping in `asyncio.run_in_executor` will block the entire event loop. Use `_resolve_channel_async()` and `_get_proxy_headers_async()` wrappers.
- Segments are **fully buffered** (`aread()`) before responding to the player. `StreamingResponse` for segments caused 20-30s stalls when CDN dropped connections mid-transfer (player hung waiting for the Content-Length it was promised).
- Segment requests include `Accept-Encoding: identity` to prevent CDN compression from making the upstream `Content-Length` header invalid.

### Kodi Plugin Routing (3 modes)

`plugin.video.cricfy/main.py` parses URL parameters:
- No `mode` → `list_providers()` — lists provider categories
- `mode=list_channels` → `list_channels(provider_url)` — fetches and lists channels
- `mode=play` → `play_video(provider_url, channel_title)` — plays with DRM/header config

### Shared vs. Target-Specific Code

`lib/` inside both `plugin.video.cricfy/` and `stremio-addon/` contains near-identical logic. The key difference is `lib/config.py` and `lib/logger.py`: the Kodi version uses `xbmc`/`StorageServer` APIs; the Stremio version uses Python's `logging` and `cachetools.TTLCache`.

### Caching

- **Providers**: TTL 24 hours
- **Channels per provider**: TTL 1 hour
- **Proxy headers** (Stremio): TTL 1 hour

### DRM

Streams may include Clearkey DRM. Keys are either hex-encoded inline (parsed from M3U `#EXT-X-KEY`) or come from a license server URL. Both plugins handle this; Stremio passes key info in stream objects to the player.

## Key Files

| File | Purpose |
|---|---|
| `stremio-addon/app.py` | Core FastAPI app + all proxy rewriting logic (~600 lines) |
| `stremio-addon/handlers/stream.py` | Builds Stremio stream objects + proxy URLs |
| `stremio-addon/id_utils.py` | Stable channel IDs via SHA256(provider_url + channel_title) |
| `lib/crypto_utils.py` | AES-CBC decrypt with `CRICFY_SECRET1`/`CRICFY_SECRET2` |
| `lib/remote_config.py` | Firebase Remote Config fetcher (Android spoof) |
| `lib/m3u_parser.py` | M3U/M3U8 → `PlaylistItem` objects |
| `stremio-addon/progress.md` | Development notes — read this for context on in-progress work |
| `plugin.video.cricfy/addon.xml` | Kodi manifest (version, dependencies) |
