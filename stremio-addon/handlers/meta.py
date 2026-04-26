from lib.providers import get_providers, get_channels
from lib.logger import log_error, log_info
from id_utils import parse_channel_id, make_channel_id, provider_hash


def _find_channel(stremio_id: str):
    """
    Resolves a cricfy channel ID back to (provider, PlaylistItem).
    Returns (provider dict, PlaylistItem) or (None, None).
    """
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
            log_error("meta", f"Failed fetching channels: {e}")
            return None, None

        for ch in channels:
            if make_channel_id(cat_link, ch.title).split(":")[2] == chan_hash:
                return provider, ch

    return None, None


def handle_meta(content_type: str, stremio_id: str):
    """Returns Stremio meta object for a single channel."""
    if not stremio_id.startswith("cricfy:"):
        return {"meta": {}}

    log_info("meta", f"Resolving meta for {stremio_id}")

    provider, ch = _find_channel(stremio_id)
    if not ch:
        log_error("meta", f"Channel not found: {stremio_id}")
        return {"meta": {}}

    meta = {
        "id": stremio_id,
        "type": "tv",
        "name": ch.title,
        "description": ch.group_title or "Live Stream",
    }
    if ch.tvg_logo:
        meta["poster"] = ch.tvg_logo
        meta["logo"] = ch.tvg_logo
    if ch.group_title:
        meta["genre"] = [ch.group_title]

    return {"meta": meta}
