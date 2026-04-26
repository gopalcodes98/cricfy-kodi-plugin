# Cricfy Stremio Addon — Work in Progress

## Goal
Transform the existing Cricfy Kodi plugin into a Stremio addon, reusing all the Python business logic (crypto, M3U parsing, Firebase provider discovery) and exposing it via a FastAPI HTTP server that implements the Stremio addon protocol.

---

## Update — 2026-04-05: Proxy Performance Hardening (Buffering Still Under Investigation)

### Problem
Stream opens after a wait, shows ~1 frame every 10-20 seconds. VLC plays the same stream directly from M3U without issue.

### Changes Implemented

#### 1. Shared httpx client with connection pooling
- Replaced per-request `httpx.AsyncClient(...)` (one per segment!) with a single module-level `AsyncClient` initialized via FastAPI `lifespan`
- Config: `max_connections=100`, `max_keepalive_connections=30`, `keepalive_expiry=30`
- Eliminates TCP+TLS handshake overhead on every `.m4s`/`.ts` segment request
- Used `@asynccontextmanager` lifespan instead of deprecated `@app.on_event("startup")`

#### 2. Proxy headers cache pre-population
- `proxy_stream` now explicitly writes to `proxy_headers_cache` after channel resolution
- All subsequent `proxy_media` calls hit the cache (O(1) dict lookup) instead of rescanning providers/channels
- Eliminated duplicate `_resolve_channel_by_id` call that was happening in `proxy_stream` → `_get_proxy_headers`

#### 3. Async wrappers for synchronous blocking calls
- `lib/req.py` uses the synchronous `requests` library. On cache miss, `get_providers()` / `get_channels()` call `requests.get()` which blocks the entire asyncio event loop
- Added `_resolve_channel_async()` and `_get_proxy_headers_async()` wrappers using `asyncio.run_in_executor` so sync network calls run in a thread pool
- This was blocking all concurrent segment requests during cache miss scenarios

#### 4. Segment buffering (complete download before responding)
- Changed `/proxy/media` to buffer segments fully with `aread()` instead of streaming with `StreamingResponse`
- **Root cause of stall pattern**: With `StreamingResponse`, the player receives `200 OK + Content-Length:X` before we read a single byte. If CDN drops the connection mid-transfer, the player hangs waiting for the remaining bytes until its own timeout (~20-30s)
- With buffering: either the full segment arrives (delivered instantly at localhost speed) or CDN error is caught and a clean `502` is returned — player retries immediately instead of hanging

#### 5. `Accept-Encoding: identity` for segment requests
- Added to upstream segment requests to prevent CDN from compressing binary content
- httpx decompresses transparently but doesn't update the `Content-Length` header — without this, forwarded Content-Length could mismatch actual bytes delivered, causing player stalls

#### 6. Accurate Content-Length for buffered segments
- After `aread()`, set `Content-Length = str(len(body))` from actual bytes rather than the upstream header value

### Verification
- ✅ All 6 unit tests pass
- ✅ Server starts without errors
- ⚠️ Stream still shows limited frames — root cause not fully resolved

### Remaining Investigation Areas
1. **CDN throttling / anti-bot detection** — Our proxy patterns may trigger CDN rate limiting even though same IP as player
2. **DASH `BaseURL` rewriting** — Rewriting `<BaseURL>` to proxy URLs may confuse libmpv's DASH client in ways specific to JioTV's MPD structure
3. **`requests` library still used in `lib/req.py`** — Consider migrating to `httpx` async throughout to eliminate the blocking/executor workaround entirely
4. **libmpv DASH behavior** — Stremio Desktop uses libmpv; whether it honors our proxy MPD's absolute URLs vs falling back to BaseURL resolution for some segments is unclear without MPD inspection

---

## Update — 2026-04-03: Fixed Manifest Rewriting with Absolute Proxy URLs

### Problem Identified
- Streams show **only 1 frame**, then stop
- Root cause: Rewritten manifest URLs were relative (`/proxy/media?...`), which may not resolve correctly from the player's perspective, or there was degradation with nested manifest rewrites

### Fixes Implemented
1. **Absolute proxy URLs**: Updated `_proxy_target_url()` to optionally generate absolute URLs when `proxy_base_url` is provided
2. **Request base URL**: Added `request: Request` parameter to `/proxy/stream` endpoint to capture the base URL
3. **Better logging**: Added logging to show how many proxy URLs were rewritten in each manifest
4. **Namespace preservation**: Ensured XML namespace handling is consistent in MPD rewriting
5. **Fixed URL encoding**: Added `%` to safe characters in URL encoding for better segment URL handling

### Code Changes
- `_proxy_target_url()`: Now accepts optional `proxy_base_url` parameter for absolute URLs
- `_rewrite_hls_manifest()`: Updated signature to accept `proxy_base_url`
- `_rewrite_mpd_manifest()`: Updated signature to accept `proxy_base_url`  
- `/proxy/stream`: Now accepts `Request` parameter and passes base URL to rewrite functions
- `/proxy/media`: Now passes request base URL when rewriting nested manifests

### Verification
- ✅ All 6 unit tests pass
- ✅ Server starts without errors
- ✅ Proxy URLs now use absolute paths (e.g., `http://localhost:8000/proxy/media?id=...&url=...`)
- ✅ Better logging shows proxy URL counts in manifests

---

## Previous Updates — 2026-04-03: Fixed httpx Stream Reading & Response Encoding

### Issues Fixed

#### 1. httpx AsyncClient stream parameter
- **Error**: `AsyncClient.get() got an unexpected keyword argument 'stream'`
- **Cause**: httpx doesn't accept `stream=True` on `.get()` method
- **Fix**: Changed to use `client.build_request()` + `client.send(upstream_request, stream=True)`

#### 2. StreamingResponse encoding of rewritten manifests
- **Error**: `AttributeError: 'int' object has no attribute 'encode'`
- **Cause**: `StreamingResponse(content=bytes)` tries to iterate bytes as integers, then calls `.encode()` on them
- **Fix**: Changed rewritten DASH/HLS manifests to use `Response` (not `StreamingResponse`) since content is fully loaded

#### 3. AsyncClient context manager closing prematurely
- **Error**: `[proxy] Proxy error:` (trying to read closed response)
- **Cause**: `async with httpx.AsyncClient(...) as client:` exited right after `upstream.raise_for_status()`, before reading response
- **Fix**: Moved all response reading/rewriting logic **inside** the context manager

#### 4. Content-Length header mismatch
- **Error**: `RuntimeError: Response content longer than Content-Length`
- **Cause**: Rewritten manifest URLs are longer than original URLs, changing content size. But upstream `Content-Length` header was passed unchanged.
- **Fix**: Remove `Content-Length` header from rewritten manifest responses (FastAPI auto-calculates correct length from actual content)

### Verification
- ✅ All 6 unit tests pass
- ✅ Server starts without errors
- ✅ `/proxy/stream` endpoint now handles DASH/HLS manifests correctly
- ✅ Rewritten manifests return correct Content-Length

---

## Update — 2026-04-02: Fixed Stream Header Issue

### Problem Identified
- Streams show **only a single frame** in Stremio, then stop
- VLC shows: `Input bitrate: 0 kb/s` + `Demuxed data size: 51864 KiB`
  - This means manifests + first few frame(s) arrive, but then segment requests fail silently
  - Root cause: **video player libmpv sends Browser-like headers that conflict with CDN authentication**

### Root Cause Analysis
When the player (libmpv in Stremio) requests media segments via `/proxy/media?id=...&url=...`, it was sending:
- `Accept: */*` or `Accept: application/dash+xml`
- `Accept-Language: en-US`  
- `Origin: http://127.0.0.1:8000` (local proxy origin)

For **JioTV/Hotstar** DASH streams with Akamai EdgeAuth tokens (`__hdnea__` cookie):
- The CDN **rejects segment requests** that have unexpected `Origin` or `Accept` headers
- Result: 403 Forbidden or request timeout silently kills the stream

### Fix Implemented
Modified `/proxy/media` endpoint to use **different header forwarding rules** based on request type:

1. **For media segments** (`.m4s`, `.ts`, `.key`, etc.) → Minimal headers:
   - Only forward: `Range` / `If-Range` (for seeking)
   - Always: Channel-specific headers (`Cookie`, `User-Agent`, `Referer` — Akamai auth wins)
   - **Never forward**: `Accept`, `Accept-Language`, `Origin`, or other player headers

2. **For manifest requests** (`.mpd`, `.m3u8`) → Normal headers:
   - Forward: `Accept`, `Accept-Language`, `Range`, `If-Range` from player
   - Always override with channel headers (auth wins)

### Code Changes
- `app.py` / `_forward_request_headers()`: Added `is_segment` parameter to distinguish request types
- `app.py` / `proxy_media()`: Detects segment URLs by extension (`.m4s`, `.ts`, `.m4a`, `.m4v`, `.key`, etc.)
- `tests/test_proxy_rewrite.py`: Added 2 regression tests for segment vs. manifest header handling

### Tests Updated
- All 6 tests pass (was 4, added 2 new segment header tests)
- Verified: segment requests get minimal headers, manifest requests get normal headers

---

## What Has Been Built

### Fix implemented today
- Added a **full adaptive proxy** in `app.py`:
  - `/proxy/stream` now rewrites both **DASH (`.mpd`)** and **HLS (`.m3u8`)** manifests
  - all nested playlists, keys, init files, and media segments are redirected through `/proxy/media`
- `/proxy/media` now injects the original channel headers (`User-Agent`, `Referer`, `Cookie`, and custom headers) on **every upstream request**, not just the first manifest request
- `handlers/stream.py` now returns the **local proxy stream first** and keeps the direct pipe-URL stream as a fallback
- Added regression coverage in `tests/test_proxy_rewrite.py`

### Local verification completed
- `d:/codebase/cricfy-kodi-plugin/stremio-addon/.venv/Scripts/python.exe -m unittest discover -s tests -v` → **3/3 tests passed**
- Live endpoint check confirmed:
  - `/stream/tv/{id}.json` returns `http://127.0.0.1:8000/proxy/stream?...` as the first stream URL
  - `/proxy/stream?id=...` returns `200 OK` with `Content-Type: application/dash+xml`
  - the rewritten manifest contains `/proxy/media?id=...`, confirming segment-level proxying is active

> Remaining real-world check: open the addon in **Stremio Desktop** and verify the stream now plays end-to-end. The server-side fix is in place and locally validated.

---

## What Has Been Built

### Directory: `stremio-addon/`

Full FastAPI-based Stremio addon with these files:

| File | Purpose |
|---|---|
| `main.py` | Entry point — loads `.env`, writes resource files, starts uvicorn |
| `app.py` | FastAPI app — all Stremio routes + stream proxy endpoints |
| `manifest.py` | Stremio addon manifest (single `cricfy-live` TV catalog) |
| `id_utils.py` | Stable channel ID: `cricfy:{prov_sha256[:10]}:{chan_sha256[:10]}` |
| `handlers/catalog.py` | Returns all channels from all providers as Stremio metas |
| `handlers/meta.py` | Returns metadata for a single channel |
| `handlers/stream.py` | Returns stream URLs with headers; builds pipe-URL format |
| `lib/config.py` | Adapted from Kodi — uses `cachetools.TTLCache` instead of StorageServer |
| `lib/logger.py` | Adapted from Kodi — uses Python `logging` instead of `xbmc.log` |
| `lib/providers.py` | Adapted from Kodi — uses TTLCache, added HTTP URL validation |
| `lib/crypto_utils.py` | Copied unchanged from Kodi plugin |
| `lib/m3u_parser.py` | Copied unchanged from Kodi plugin |
| `lib/remote_config.py` | Copied unchanged from Kodi plugin |
| `lib/req.py` | Copied unchanged from Kodi plugin |

### What Works
- Server starts correctly from any directory
- `.env` file is loaded automatically on startup
- Firebase Remote Config is fetched and provider list is decrypted and cached (24h)
- Channels are fetched, decrypted from M3U, and parsed per provider (1h cache)
- `/manifest.json` returns correct Stremio manifest
- `/catalog/tv/cricfy-live.json` returns channels
- `/stream/tv/{id}.json` returns stream objects with correct structure

---

## Current Problem: Streams Are Unplayable

### Stream response looks correct (curl confirmed):
```json
{
  "streams": [
    {
      "title": "Star Sports 1 HD",
      "url": "https://jiotvmblive.cdn.jio.com/.../index.mpd?__hdnea__=...|User-Agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) Gecko/20100101 Firefox/149.0&Cookie=__hdnea__=st=...",
      "behaviorHints": {
        "notWebReady": true,
        "proxyHeaders": {
          "request": {
            "User-Agent": "Mozilla/5.0...",
            "Cookie": "__hdnea__=..."
          }
        }
      }
    },
    {
      "title": "Star Sports 1 HD (proxy)",
      "url": "http://localhost:8000/proxy/stream?id=cricfy%3A...",
      "behaviorHints": { "notWebReady": true }
    }
  ]
}
```

### Stream type observed
- DASH (`.mpd`) stream from JioTV (`jiotvmblive.cdn.jio.com`)
- Requires `Cookie: __hdnea__=...` (Akamai EdgeAuth token) on EVERY request — manifest AND all segment requests
- Token is also present as `?__hdnea__=...` query param in the URL

### What has been tried
1. **Server-side proxy** (`/proxy/stream`) — proxies the MPD manifest with headers, but DASH segment requests from the player then go directly to JioTV CDN without the cookie → fails
2. **`behaviorHints.proxyHeaders`** — Stremio injects headers; unclear if supported for local (non-published) addons
3. **Pipe URL format** (`url|User-Agent=...&Cookie=...`) — fixed URL encoding bug (was encoding `=` as `%3D` etc.); status unknown after fix

### Errors seen on startup / first request
1. `Content decryption failed: Invalid base64-encoded string: number of data characters (21) cannot be 1 more than a multiple of 4`
   - **Harmless** — `decrypt_content()` falls back to original content; happens for providers whose M3U content is not encrypted
2. `Error fetching M3U URL (N): Invalid URL 'N'`
   - **Fixed** — some providers have `catLink: "N"` as a placeholder; added `startswith("http")` guard in all handlers and `get_channels()`

---

## Next Investigation Steps

### 1. Verify pipe URL fix actually reaches the player correctly
After the URL-encoding fix, run curl again and confirm the URL looks like:
```
https://.../index.mpd?__hdnea__=st=...|User-Agent=Mozilla/5.0 (Windows NT 10.0...)&Cookie=__hdnea__=st=...
```
No `%2F`, `%3D`, `%7E` in the header portion after the `|`.

### 2. Test if `proxyHeaders` works in Stremio desktop
Stremio desktop uses **libmpv** for DASH/HLS. Check if `proxyHeaders.request` from the stream response actually causes mpv to send `Cookie` on segment requests. If not, this approach won't work for local addons.

### 3. Implement full DASH/HLS proxy (most reliable fix)
The current `/proxy/stream` endpoint only proxies the initial manifest. For DASH, segments must also be proxied.

Full proxy approach:
- Proxy `.mpd` manifest → rewrite all segment base URLs to point to `/proxy/segment?url=...&Cookie=...`
- New `/proxy/segment` endpoint: fetches the actual segment from CDN with headers and streams it back

This matches exactly what Kodi's `inputstream.adaptive.stream_headers` does — it applies headers to every single request including segments.

### 4. Alternative: Check if Stremio supports `streamHeaders` field
Some forks of the Stremio addon SDK / community implementations use a `streamHeaders` field directly on the stream object (separate from `behaviorHints`). Worth checking the current Stremio desktop source to see what stream object fields it actually reads.

---

## Known Working Configuration (Kodi Reference)
From `plugin.video.cricfy/main.py` — this is what we need to replicate:
```python
li.setProperty('inputstream.adaptive.manifest_headers', encoded_headers)
li.setProperty('inputstream.adaptive.stream_headers', encoded_headers)
url += '|' + encoded_headers  # raw, NOT URL-encoded
li.setPath(url)
```
`encoded_headers` format: `User-Agent=Mozilla/5.0...&Cookie=__hdnea__=st=...~exp=...` (raw values, `&` separator)

---

## How to Run
```bash
cd stremio-addon
pip install -r requirements.txt
python main.py
# Add to Stremio: http://localhost:8000/manifest.json
```

Requires `.env` file (copy from `.env.example`) with Firebase credentials and secret keys.
