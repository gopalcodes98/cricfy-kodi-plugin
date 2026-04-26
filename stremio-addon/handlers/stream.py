import re
import urllib.parse
from lib.providers import get_providers, get_channels
from lib.m3u_parser import PlaylistItem
from lib.req import license_headers
from lib.logger import log_error, log_info
from id_utils import parse_channel_id, make_channel_id, provider_hash

# Populated at runtime via set_base_url()
_base_url: str = "http://localhost:8000"


def set_base_url(url: str):
    global _base_url
    _base_url = url.rstrip("/")


def _find_channel(stremio_id: str):
    """Resolves cricfy channel ID → (cat_link, PlaylistItem) or (None, None)."""
    parsed = parse_channel_id(stremio_id)
    if not parsed:
        return None, None

    prov_hash, chan_hash = parsed
    providers = get_providers()

    for provider in providers:
        cat_link = provider.get("catLink") or provider.get("url") or ""
        if not cat_link or not cat_link.startswith("http"):
            continue
        if provider_hash(cat_link) != prov_hash:
            continue

        try:
            channels = get_channels(cat_link)
        except Exception as e:
            log_error("stream", f"Failed fetching channels: {e}")
            return None, None

        for ch in channels:
            if make_channel_id(cat_link, ch.title).split(":")[2] == chan_hash:
                return cat_link, ch

    return None, None


def _collect_headers(ch: PlaylistItem) -> dict:
    """
    Collects all custom headers from the channel in the same order as the
    Kodi plugin (extra headers first, then User-Agent, Referer, Cookie).
    """
    hdrs = {}
    if ch.headers:
        hdrs.update(ch.headers)
    if ch.user_agent:
        hdrs["User-Agent"] = ch.user_agent
    if ch.referer:
        hdrs["Referer"] = ch.referer
    if ch.cookie:
        hdrs["Cookie"] = ch.cookie
    return hdrs


def _build_pipe_url(url: str, headers: dict) -> str:
    """
    Builds a pipe-separated URL identical to what the Kodi plugin produces:
      https://stream.example.com/live.m3u8|User-Agent=...&Referer=...

    Header values are NOT URL-encoded — raw key=value pairs joined by &,
    exactly as Kodi's inputstream.adaptive expects and as mpv/ExoPlayer parse.
    """
    if not headers:
        return url
    raw = "&".join(f"{k}={v}" for k, v in headers.items())
    return f"{url}|{raw}"


def _build_proxy_url(stremio_id: str) -> str:
    encoded = urllib.parse.quote(stremio_id, safe="")
    return f"{_base_url}/proxy/stream?id={encoded}"


def _build_stream(ch: PlaylistItem, stremio_id: str) -> dict:
    """
    Constructs Stremio stream objects that mirror the Kodi plugin behaviour.

    For header-protected adaptive streams, the local proxy is now the primary
    option so DASH/HLS manifests and every segment request keep the same
    Cookie/User-Agent/Referer headers. A direct pipe-URL fallback is still
    included for players that support it natively.
    """
    headers = _collect_headers(ch)
    is_adaptive = any(ext in ch.url for ext in (".mpd", ".m3u8", ".m3u")) or ch.license_string

    common_hints: dict = {
        "notWebReady": True,
    }

    # DRM — mirrors the Kodi plugin's drm_legacy clearkey logic
    if ch.is_drm and ch.license_string:
        hex_pair_re = re.compile(r"^[0-9a-fA-F]+:[0-9a-fA-F]+$")

        if hex_pair_re.match(ch.license_string):
            common_hints["drm"] = {
                "scheme": "clearkey",
                "clearkeys": _parse_clearkey_pairs(ch.license_string),
            }
        elif ch.license_string.startswith("http"):
            common_hints["drm"] = {
                "scheme": "clearkey",
                "licenseUrl": ch.license_string,
                "licenseHeaders": license_headers,
            }

    streams = []

    if headers and is_adaptive:
        # Use proxy for adaptive streams that require headers
        streams.append({
            "title": ch.title or "Stream",
            "url": _build_proxy_url(stremio_id),
            "behaviorHints": dict(common_hints),
        })
    else:
        # Direct stream for non-adaptive or header-less content
        streams.append({
            "title": ch.title or "Stream",
            "url": _build_pipe_url(ch.url, headers) if headers else ch.url,
            "behaviorHints": dict(common_hints),
        })

    return streams


def _parse_clearkey_pairs(license_str: str) -> dict:
    """Parses 'kid1:key1,kid2:key2' into {kid: key} dict."""
    result = {}
    for pair in license_str.split(","):
        if ":" in pair:
            kid, key = pair.split(":", 1)
            result[kid.strip()] = key.strip()
    return result


def handle_stream(content_type: str, stremio_id: str):
    """Returns Stremio stream objects for a channel."""
    if not stremio_id.startswith("cricfy:"):
        return {"streams": []}

    log_info("stream", f"Resolving stream for {stremio_id}")

    _, ch = _find_channel(stremio_id)
    if not ch:
        log_error("stream", f"Channel not found: {stremio_id}")
        return {"streams": []}

    if not ch.url:
        return {"streams": []}

    return {"streams": _build_stream(ch, stremio_id)}
