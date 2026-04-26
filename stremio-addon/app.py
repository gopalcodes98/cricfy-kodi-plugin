import asyncio
import re
import urllib.parse
from contextlib import asynccontextmanager
from xml.etree import ElementTree as ET

import httpx
from cachetools import TTLCache
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from manifest import MANIFEST
from handlers.catalog import handle_catalog
from handlers.meta import handle_meta
from handlers.stream import handle_stream, set_base_url
from lib.providers import get_providers, get_channels
from lib.logger import log_error, log_info
from id_utils import parse_channel_id, make_channel_id, provider_hash
from lib.m3u_parser import PlaylistItem

# Shared HTTP client with connection pooling — reused across all proxy requests
# to avoid per-request TCP handshakes (critical for DASH/HLS segment throughput)
_http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    _http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=5.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=30, keepalive_expiry=30),
    )
    log_info("app", "Addon started — shared HTTP client ready")
    yield
    await _http_client.aclose()


app = FastAPI(title="Cricfy Stremio Addon", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)


# ─── Manifest ────────────────────────────────────────────────────────────────

@app.get("/manifest.json")
def manifest():
    return JSONResponse(MANIFEST)


# ─── Catalog ─────────────────────────────────────────────────────────────────

@app.get("/catalog/{content_type}/{catalog_id}.json")
def catalog(
    content_type: str,
    catalog_id: str,
    genre: str = Query(default=None),
    skip: int = Query(default=0),
):
    result = handle_catalog(catalog_id, genre=genre, skip=skip)
    return JSONResponse(result)


# Stremio also hits /catalog/{type}/{id}/genre={x}&skip={n}.json (extra encoded in path)
@app.get("/catalog/{content_type}/{catalog_id}/{extras}.json")
def catalog_with_extras(content_type: str, catalog_id: str, extras: str):
    params = {}
    for part in extras.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            params[k] = urllib.parse.unquote(v)
    genre = params.get("genre")
    skip = int(params.get("skip", 0))
    result = handle_catalog(catalog_id, genre=genre, skip=skip)
    return JSONResponse(result)


# ─── Meta ─────────────────────────────────────────────────────────────────────

@app.get("/meta/{content_type}/{stremio_id}.json")
def meta(content_type: str, stremio_id: str):
    result = handle_meta(content_type, urllib.parse.unquote(stremio_id))
    return JSONResponse(result)


# ─── Stream ───────────────────────────────────────────────────────────────────

@app.get("/stream/{content_type}/{stremio_id}.json")
def stream(content_type: str, stremio_id: str, request: Request):
    # Detect base URL from request so proxy URLs are correct
    base = str(request.base_url).rstrip("/")
    set_base_url(base)

    result = handle_stream(content_type, urllib.parse.unquote(stremio_id))
    return JSONResponse(result)


# ─── Stream Proxy ─────────────────────────────────────────────────────────────

proxy_headers_cache: TTLCache = TTLCache(maxsize=512, ttl=3600)


def _resolve_channel_by_id(stremio_id: str) -> PlaylistItem | None:
    """Re-fetches the channel for proxy use (cache hit in most cases)."""
    parsed = parse_channel_id(stremio_id)
    if not parsed:
        return None

    prov_hash_val, chan_hash = parsed
    providers = get_providers()

    for provider in providers:
        cat_link = provider.get("catLink") or provider.get("url") or ""
        if not cat_link or not cat_link.startswith("http"):
            continue
        if provider_hash(cat_link) != prov_hash_val:
            continue
        try:
            channels = get_channels(cat_link)
        except Exception:
            return None
        for ch in channels:
            if make_channel_id(cat_link, ch.title).split(":")[2] == chan_hash:
                return ch

    return None


def _collect_proxy_headers(ch: PlaylistItem) -> dict[str, str]:
    headers: dict[str, str] = {}
    for k, v in (ch.headers or {}).items():
        headers[str(k)] = str(v)
    if ch.user_agent:
        headers["User-Agent"] = ch.user_agent
    if ch.referer:
        headers["Referer"] = ch.referer
    if ch.cookie:
        headers["Cookie"] = ch.cookie
    return headers


def _get_proxy_headers(stremio_id: str) -> dict[str, str]:
    cached = proxy_headers_cache.get(stremio_id)
    if cached is not None:
        return dict(cached)

    ch = _resolve_channel_by_id(stremio_id)
    if not ch or not ch.url:
        raise HTTPException(status_code=404, detail="Stream not found")

    headers = _collect_proxy_headers(ch)
    proxy_headers_cache[stremio_id] = dict(headers)
    return headers


def _proxy_target_url(target_url: str, stremio_id: str, base_url: str | None = None) -> str:
    encoded_id = urllib.parse.quote(stremio_id, safe="")
    encoded_url = urllib.parse.quote(target_url, safe="/:%;$-_.~")
    proxy_url = f"/proxy/media?id={encoded_id}&url={encoded_url}"
    # Use absolute URL if base_url provided (for manifests), relative for other uses
    if base_url:
        return urllib.parse.urljoin(base_url, proxy_url)
    return proxy_url


def _make_absolute(url: str, base: str) -> str:
    return urllib.parse.urljoin(base, url)


def _is_hls_content_type(ct: str) -> bool:
    return any(t in ct.lower() for t in ("mpegurl", "x-mpegurl", "vnd.apple.mpegurl"))


def _is_dash_content_type(ct: str) -> bool:
    ct = ct.lower()
    return "dash+xml" in ct or "application/dash+xml" in ct


async def _rewrite_hls_manifest(text: str, base_url: str, stremio_id: str, proxy_base_url: str | None = None) -> str:
    """
    Rewrites all HLS playlist references so every nested playlist, key file,
    init map and media segment also goes through the local proxy.
    """
    uri_attr_re = re.compile(r'URI=(["\'])(.*?)(\1)')
    rewritten: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()

        if not stripped:
            rewritten.append(line)
            continue

        if stripped.startswith("#"):
            if "URI=" in line:
                line = uri_attr_re.sub(
                    lambda m: f"URI={m.group(1)}{_proxy_target_url(_make_absolute(m.group(2), base_url), stremio_id, proxy_base_url)}{m.group(3)}",
                    line,
                )
            rewritten.append(line)
            continue

        absolute = _make_absolute(stripped, base_url)
        rewritten.append(_proxy_target_url(absolute, stremio_id, proxy_base_url))

    return "\n".join(rewritten)


def _rewrite_mpd_manifest(text: str, base_url: str, stremio_id: str, proxy_base_url: str | None = None) -> str:
    """
    Rewrites DASH MPD resource references so the manifest, init segments and
    media segments are all fetched through the local proxy with the same headers.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        log_error("proxy", f"Failed to parse MPD for rewrite: {e}")
        return text

    # Preserve namespace
    ns = ""
    ns_match = re.match(r"^\{(.+)\}", root.tag)
    if ns_match:
        ns = ns_match.group(1)
        ET.register_namespace("", ns)

    url_attrs = ("media", "initialization", "sourceURL", "index", "href")

    def walk(element: ET.Element, current_base: str) -> None:
        local_base = current_base

        for child in list(element):
            if child.tag.endswith("BaseURL") and child.text and child.text.strip():
                absolute_base = _make_absolute(child.text.strip(), current_base)
                child.text = _proxy_target_url(absolute_base, stremio_id, proxy_base_url)
                local_base = absolute_base

        for attr in url_attrs:
            value = element.attrib.get(attr)
            if value and not value.startswith("data:"):
                absolute = _make_absolute(value, local_base)
                element.attrib[attr] = _proxy_target_url(absolute, stremio_id, proxy_base_url)

        for child in list(element):
            if child.tag.endswith("BaseURL"):
                continue
            walk(child, local_base)

    walk(root, base_url)

    rewritten = ET.tostring(root, encoding="unicode")
    if text.lstrip().startswith("<?xml"):
        return f'<?xml version="1.0" encoding="UTF-8"?>\n{rewritten}'
    return rewritten


def _forward_request_headers(
    request: Request,
    channel_headers: dict[str, str] | None = None,
    is_segment: bool = False,
) -> dict[str, str]:
    """
    Collects headers to forward to upstream.
    Channel-specific headers take precedence over player-supplied headers.
    
    For segment requests (is_segment=True), only forward Range/If-Range from the player,
    since segments are fixed binary content that don't need browser-like headers.
    This prevents CDN authentication failures when players send interfering headers.
    """
    headers: dict[str, str] = {}
    
    # First, add channel-specific headers (these are the most important)
    if channel_headers:
        for k, v in channel_headers.items():
            # Only forward headers that make sense for upstream requests
            if k.lower() not in ("origin",):  # Don't forward Origin as it's often wrong
                headers[k] = v
    
    # For segment requests, only allow Range/If-Range from the player
    # The player's Accept, Accept-Language, User-Agent etc. can interfere with CDN auth
    if is_segment:
        segment_allowed = {"if-range", "range"}
        for k, v in request.headers.items():
            if k.lower() in segment_allowed:
                headers[k] = v
    else:
        # For non-segment requests (manifests, etc.), forward more headers
        allowed = {
            "accept",
            "accept-language",
            "if-range",
            "range",
            "user-agent",
            "referer",
            "cookie",
        }
        for k, v in request.headers.items():
            if k.lower() in allowed:
                # Don't override channel headers with player headers
                if k.lower() not in headers:
                    headers[k] = v
    
    return headers


def _passthrough_response_headers(headers: httpx.Headers) -> dict[str, str]:
    allowed = (
        "accept-ranges",
        "cache-control",
        "content-disposition",
        "content-length",
        "content-range",
        "etag",
        "last-modified",
    )
    response_headers = {"Access-Control-Allow-Origin": "*"}
    for key in allowed:
        if key in headers:
            response_headers[key.title()] = headers[key]
    return response_headers


async def _close_response(response: httpx.Response) -> None:
    await response.aclose()


async def _resolve_channel_async(stremio_id: str):
    """Runs the sync channel resolver in a thread pool so it never blocks the event loop.
    This matters on cache miss: get_providers()/get_channels() call the synchronous
    requests library, which would freeze all concurrent segment requests otherwise."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _resolve_channel_by_id, stremio_id)


async def _get_proxy_headers_async(stremio_id: str) -> dict[str, str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_proxy_headers, stremio_id)


@app.get("/proxy/stream")
async def proxy_stream(request: Request, id: str = Query(...)):
    """
    Proxies the initial manifest/stream URL with the channel's custom headers.
    Adaptive playlists are rewritten so every later request also stays on proxy.
    """
    stremio_id = urllib.parse.unquote(id)

    # Single lookup — result goes into the headers cache for all subsequent /proxy/media calls
    ch = await _resolve_channel_async(stremio_id)
    if not ch or not ch.url:
        raise HTTPException(status_code=404, detail="Stream not found")

    headers = _collect_proxy_headers(ch)
    # Populate cache so /proxy/media hits don't re-resolve the channel
    proxy_headers_cache[stremio_id] = dict(headers)

    log_info("proxy", f"Proxying {ch.url} with headers: {list(headers.keys())}")

    try:
        request_base = str(request.base_url).rstrip("/")

        upstream_request = _http_client.build_request("GET", ch.url, headers=headers)
        upstream = await _http_client.send(upstream_request, stream=True)
        upstream.raise_for_status()

        content_type = upstream.headers.get("content-type", "application/octet-stream")
        response_headers = _passthrough_response_headers(upstream.headers)
        response_headers["Access-Control-Allow-Origin"] = "*"

        if _is_dash_content_type(content_type) or ch.url.endswith(".mpd"):
            body = await upstream.aread()
            await upstream.aclose()
            rewritten = _rewrite_mpd_manifest(
                body.decode(upstream.encoding or "utf-8", errors="replace"),
                ch.url, stremio_id, proxy_base_url=request_base,
            )
            response_headers.pop("Content-Length", None)
            log_info("proxy", f"Rewritten MPD manifest with {rewritten.count('/proxy/media')} proxy URLs")
            return Response(
                content=rewritten.encode("utf-8"),
                media_type="application/dash+xml",
                headers=response_headers,
            )

        if _is_hls_content_type(content_type) or ch.url.endswith((".m3u8", ".m3u")):
            body = await upstream.aread()
            await upstream.aclose()
            rewritten = await _rewrite_hls_manifest(
                body.decode(upstream.encoding or "utf-8", errors="replace"),
                ch.url, stremio_id, proxy_base_url=request_base,
            )
            response_headers.pop("Content-Length", None)
            log_info("proxy", f"Rewritten HLS manifest with {rewritten.count('/proxy/media')} proxy URLs")
            return Response(
                content=rewritten.encode("utf-8"),
                media_type="application/vnd.apple.mpegurl",
                headers=response_headers,
            )

        return StreamingResponse(
            upstream.aiter_bytes(chunk_size=65536),
            status_code=upstream.status_code,
            media_type=content_type,
            headers=response_headers,
            background=BackgroundTask(_close_response, upstream),
        )

    except httpx.HTTPStatusError as e:
        log_error("proxy", f"Upstream HTTP error {e.response.status_code}: {ch.url}")
        raise HTTPException(status_code=e.response.status_code, detail="Upstream error")
    except Exception as e:
        log_error("proxy", f"Proxy error: {e}")
        raise HTTPException(status_code=502, detail="Proxy error")


@app.api_route("/proxy/media", methods=["GET", "HEAD"])
async def proxy_media(request: Request, url: str = Query(...), id: str | None = Query(default=None)):
    """
    Proxies HLS/DASH segments, nested manifests and other media bytes while
    injecting the same per-channel headers on every upstream request.
    """
    target = urllib.parse.unquote(url)
    stremio_id = id if id else None

    # Use cached headers — avoids full provider/channel scan on every segment request.
    # Run in executor: get_providers()/get_channels() use the synchronous requests library;
    # calling them in an async function without an executor would block the event loop.
    channel_headers: dict[str, str] | None = None
    if stremio_id:
        try:
            channel_headers = await _get_proxy_headers_async(stremio_id)
        except HTTPException:
            raise

    target_path = target.split("?")[0].lower()
    is_segment = any(
        target_path.endswith(ext)
        for ext in (".m4s", ".m4a", ".m4v", ".ts", ".mp4", ".webm", ".mkv", ".key", ".bin", ".data")
    )
    if target_path.endswith((".m3u", ".m3u8", ".mpd")):
        is_segment = False

    headers = _forward_request_headers(request, channel_headers, is_segment=is_segment)
    if is_segment:
        # Ask CDN for uncompressed content so Content-Length in the response
        # matches the actual byte count we deliver (httpx decompresses transparently
        # but doesn't update the upstream Content-Length header).
        headers["Accept-Encoding"] = "identity"

    try:
        upstream_request = _http_client.build_request(request.method, target, headers=headers)
        upstream = await _http_client.send(upstream_request, stream=True)

        if upstream.status_code >= 400:
            body = await upstream.aread()
            await upstream.aclose()
            raise HTTPException(
                status_code=upstream.status_code,
                detail=body.decode(errors="ignore") or "Upstream error",
            )

        response_headers = _passthrough_response_headers(upstream.headers)
        media_type = upstream.headers.get("content-type", "application/octet-stream")

        if request.method == "HEAD":
            await upstream.aclose()
            return Response(
                content=b"",
                status_code=upstream.status_code,
                media_type=media_type,
                headers=response_headers,
            )

        request_base = str(request.base_url).rstrip("/")

        if stremio_id and (_is_dash_content_type(media_type) or target_path.endswith(".mpd")):
            body = await upstream.aread()
            await upstream.aclose()
            rewritten = _rewrite_mpd_manifest(
                body.decode(upstream.encoding or "utf-8", errors="replace"),
                target, stremio_id, proxy_base_url=request_base,
            )
            response_headers.pop("Content-Length", None)
            return Response(
                content=rewritten,
                status_code=upstream.status_code,
                media_type="application/dash+xml",
                headers=response_headers,
            )

        if stremio_id and (_is_hls_content_type(media_type) or target_path.endswith((".m3u8", ".m3u"))):
            body = await upstream.aread()
            await upstream.aclose()
            rewritten = await _rewrite_hls_manifest(
                body.decode(upstream.encoding or "utf-8", errors="replace"),
                target, stremio_id, proxy_base_url=request_base,
            )
            response_headers.pop("Content-Length", None)
            return Response(
                content=rewritten,
                status_code=upstream.status_code,
                media_type="application/vnd.apple.mpegurl",
                headers=response_headers,
            )

        # Buffer segments fully before sending to player.
        # With StreamingResponse the player receives "200 OK + Content-Length:X"
        # before we've read all bytes. If the CDN drops the connection mid-transfer
        # the player waits for the missing bytes until it times out (~20-30s).
        # Buffering means we either deliver the complete segment or return a 502 —
        # the player gets a clean retry signal with no partial-data stall.
        if is_segment:
            body = await upstream.aread()
            await upstream.aclose()
            # Recompute Content-Length from actual bytes in case CDN used compression
            response_headers["Content-Length"] = str(len(body))
            return Response(
                content=body,
                status_code=upstream.status_code,
                media_type=media_type,
                headers=response_headers,
            )

        return StreamingResponse(
            upstream.aiter_bytes(chunk_size=65536),
            status_code=upstream.status_code,
            media_type=media_type,
            headers=response_headers,
            background=BackgroundTask(_close_response, upstream),
        )

    except HTTPException:
        raise
    except Exception as e:
        log_error("proxy_media", f"Error proxying {target}: {e}")
        raise HTTPException(status_code=502, detail="Proxy error")


@app.api_route("/proxy/segment", methods=["GET", "HEAD"])
async def proxy_segment(request: Request, url: str = Query(...), id: str | None = Query(default=None)):
    """
    Proxies DASH/HLS segments and nested manifests with proper headers matching
    Kodi's inputstream.adaptive.stream_headers. This endpoint handles all segment
    requests from libmpv's DASH client, including BaseURL resolution and segment
    fetching.
    """
    target = urllib.parse.unquote(url)
    stremio_id = id if id else None

    # Resolve channel headers — these are critical for CDN authentication
    # Run in executor: get_providers()/get_channels() use the synchronous requests library
    channel_headers: dict[str, str] | None = None
    if stremio_id:
        try:
            channel_headers = await _get_proxy_headers_async(stremio_id)
        except HTTPException:
            raise

    # Determine if this is a segment or manifest request
    target_path = target.split("?")[0].lower()
    is_segment = any(
        target_path.endswith(ext)
        for ext in (".m4s", ".m4a", ".m4v", ".ts", ".mp4", ".webm", ".mkv", ".key", ".bin", ".data")
    )
    if target_path.endswith((".m3u", ".m3u8", ".mpd")):
        is_segment = False

    # Build headers for upstream request
    headers = _forward_request_headers(request, channel_headers, is_segment=is_segment)

    # For segment requests, only allow Range/If-Range from the player
    # The player's Accept, Accept-Language, User-Agent etc. can interfere with CDN auth
    if is_segment:
        segment_allowed = {"if-range", "range"}
        for k, v in request.headers.items():
            if k.lower() in segment_allowed:
                headers[k] = v

    try:
        upstream_request = _http_client.build_request(request.method, target, headers=headers)
        upstream = await _http_client.send(upstream_request, stream=True)

        if upstream.status_code >= 400:
            body = await upstream.aread()
            await upstream.aclose()
            raise HTTPException(
                status_code=upstream.status_code,
                detail=body.decode(errors="ignore") or "Upstream error",
            )

        response_headers = _passthrough_response_headers(upstream.headers)
        media_type = upstream.headers.get("content-type", "application/octet-stream")

        if request.method == "HEAD":
            await upstream.aclose()
            return Response(
                content=b"",
                status_code=upstream.status_code,
                media_type=media_type,
                headers=response_headers,
            )

        request_base = str(request.base_url).rstrip("/")

        # Check if this is a DASH manifest (could be a nested manifest request)
        if stremio_id and (_is_dash_content_type(media_type) or target_path.endswith(".mpd")):
            body = await upstream.aread()
            await upstream.aclose()
            rewritten = _rewrite_mpd_manifest(
                body.decode(upstream.encoding or "utf-8", errors="replace"),
                target, stremio_id, proxy_base_url=request_base,
            )
            response_headers.pop("Content-Length", None)
            log_info("proxy", f"Rewritten DASH manifest with {rewritten.count('/proxy/segment')} proxy URLs")
            return Response(
                content=rewritten.encode("utf-8"),
                status_code=upstream.status_code,
                media_type="application/dash+xml",
                headers=response_headers,
            )

        # Check if this is an HLS manifest (could be a nested playlist request)
        if stremio_id and (_is_hls_content_type(media_type) or target_path.endswith((".m3u8", ".m3u"))):
            body = await upstream.aread()
            await upstream.aclose()
            rewritten = await _rewrite_hls_manifest(
                body.decode(upstream.encoding or "utf-8", errors="replace"),
                target, stremio_id, proxy_base_url=request_base,
            )
            response_headers.pop("Content-Length", None)
            log_info("proxy", f"Rewritten HLS manifest with {rewritten.count('/proxy/segment')} proxy URLs")
            return Response(
                content=rewritten.encode("utf-8"),
                status_code=upstream.status_code,
                media_type="application/vnd.apple.mpegurl",
                headers=response_headers,
            )

        # For segment requests, buffer fully before sending to player
        # This ensures we either deliver the complete segment or return a 502
        # the player gets a clean retry signal with no partial-data stall
        if is_segment:
            body = await upstream.aread()
            await upstream.aclose()
            # Recompute Content-Length from actual bytes in case CDN used compression
            response_headers["Content-Length"] = str(len(body))
            return Response(
                content=body,
                status_code=upstream.status_code,
                media_type=media_type,
                headers=response_headers,
            )

        # For other content types, stream directly
        return StreamingResponse(
            upstream.aiter_bytes(chunk_size=65536),
            status_code=upstream.status_code,
            media_type=media_type,
            headers=response_headers,
            background=BackgroundTask(_close_response, upstream),
        )

    except HTTPException:
        raise
    except Exception as e:
        log_error("proxy_segment", f"Error proxying {target}: {e}")
        raise HTTPException(status_code=502, detail="Proxy error")


# ─── Legacy Raw Proxy ──────────────────────────────────────────────────────────

@app.api_route("/proxy/raw", methods=["GET", "HEAD"])
async def proxy_raw(request: Request, url: str = Query(...)):
    """
    Backwards-compatible generic URL proxy. Extra query parameters are treated
    as HTTP headers when present.
    """
    target = url
    headers = {k: v for k, v in request.query_params.items() if k != "url"}

    try:
        upstream_request = _http_client.build_request(request.method, target, headers=headers)
        upstream = await _http_client.send(upstream_request, stream=True)

        if upstream.status_code >= 400:
            body = await upstream.aread()
            await upstream.aclose()
            raise HTTPException(
                status_code=upstream.status_code,
                detail=body.decode(errors="ignore") or "Upstream error",
            )

        response_headers = _passthrough_response_headers(upstream.headers)
        media_type = upstream.headers.get("content-type", "application/octet-stream")

        if request.method == "HEAD":
            await upstream.aclose()
            return Response(
                content=b"",
                status_code=upstream.status_code,
                media_type=media_type,
                headers=response_headers,
            )

        return StreamingResponse(
            upstream.aiter_bytes(chunk_size=65536),
            status_code=upstream.status_code,
            media_type=media_type,
            headers=response_headers,
            background=BackgroundTask(_close_response, upstream),
        )
    except HTTPException:
        raise
    except Exception as e:
        log_error("proxy_raw", f"Error proxying {target}: {e}")
        raise HTTPException(status_code=502, detail="Proxy error")
