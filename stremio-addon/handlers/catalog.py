from lib.providers import get_providers, get_channels
from lib.logger import log_error, log_info
from id_utils import make_channel_id, provider_hash


def handle_catalog(catalog_id: str, genre: str | None = None, skip: int = 0):
    """
    Returns Stremio catalog metas for all live channels across all providers.

    Supports optional `genre` filtering (maps to channel group_title).
    """
    if catalog_id != "cricfy-live":
        return {"metas": []}

    log_info("catalog", f"Handling catalog request (genre={genre}, skip={skip})")

    providers = get_providers()
    if not providers:
        log_error("catalog", "No providers returned")
        return {"metas": []}

    metas = []
    seen_ids: set[str] = set()

    for provider in providers:
        cat_link = provider.get("catLink") or provider.get("url") or ""
        if not cat_link or not cat_link.startswith("http"):
            continue

        try:
            channels = get_channels(cat_link)
        except Exception as e:
            log_error("catalog", f"Failed fetching channels for {cat_link}: {e}")
            continue

        for ch in channels:
            if not ch.title or not ch.url:
                continue

            # Apply genre filter if requested
            if genre and ch.group_title.lower() != genre.lower():
                continue

            channel_id = make_channel_id(cat_link, ch.title)

            # Skip duplicate IDs (same title across providers)
            if channel_id in seen_ids:
                continue
            seen_ids.add(channel_id)

            meta = {
                "id": channel_id,
                "type": "tv",
                "name": ch.title,
            }
            if ch.tvg_logo:
                meta["poster"] = ch.tvg_logo
                meta["logo"] = ch.tvg_logo
            if ch.group_title:
                meta["genre"] = [ch.group_title]
                meta["description"] = ch.group_title

            metas.append(meta)

    # Apply pagination
    paginated = metas[skip: skip + 100]
    log_info("catalog", f"Returning {len(paginated)} items (total={len(metas)})")
    return {"metas": paginated}
