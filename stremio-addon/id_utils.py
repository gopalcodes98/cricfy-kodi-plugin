import hashlib


def provider_hash(cat_link: str) -> str:
    """Stable 10-char hex fingerprint for a provider URL."""
    return hashlib.sha256(cat_link.encode()).hexdigest()[:10]


def channel_hash(title: str) -> str:
    """Stable 10-char hex fingerprint for a channel title."""
    return hashlib.sha256(title.encode()).hexdigest()[:10]


def make_channel_id(cat_link: str, title: str) -> str:
    return f"cricfy:{provider_hash(cat_link)}:{channel_hash(title)}"


def parse_channel_id(stremio_id: str):
    """
    Returns (prov_hash, chan_hash) tuple for a valid cricfy ID,
    or None if the ID doesn't belong to this addon.
    """
    parts = stremio_id.split(":")
    if len(parts) != 3 or parts[0] != "cricfy":
        return None
    return parts[1], parts[2]
