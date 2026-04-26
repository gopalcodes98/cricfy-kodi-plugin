import json
import hashlib
from lib.config import providers_cache, channels_cache
from lib.logger import log_error, log_info
from lib.crypto_utils import decrypt_content, decrypt_data
from lib.req import fetch_url
from lib.m3u_parser import PlaylistItem, parse_m3u
from lib.remote_config import get_provider_api_url

PROVIDERS_CACHE_KEY = "cricfy_providers"


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def get_providers():
    """
    Fetches and decrypts the list of providers from Cricfy.
    Uses in-memory TTLCache (24h) to avoid repeated network calls.
    """
    cached = providers_cache.get(PROVIDERS_CACHE_KEY)
    if cached is not None:
        return cached

    log_info("providers", "[Cache Miss] Fetching providers from remote URL")

    url = get_provider_api_url()
    if not url:
        log_error("providers", "Provider API URL is not found")
        return []

    response = fetch_url(f"{url}/cats.txt", timeout=15)
    if not response:
        return []

    try:
        decrypted_data = decrypt_data(response)
        if not decrypted_data:
            return []

        providers = json.loads(decrypted_data)
        if not isinstance(providers, list):
            return []

        providers_cache[PROVIDERS_CACHE_KEY] = providers
        log_info("providers", "Providers cached successfully")
        return providers
    except Exception as e:
        log_error("providers", f"Error parsing providers: {e}")
        return []


def get_channels(provider_url: str):
    """
    Fetches channels for a specific provider.
    Uses in-memory TTLCache (1h) keyed by provider URL hash.
    """
    if not provider_url or not provider_url.startswith("http"):
        log_error("providers", f"Skipping invalid provider URL: {provider_url!r}")
        return []

    cache_key = f"channels_{_hash_key(provider_url)}"
    cached = channels_cache.get(cache_key)
    if cached is not None:
        return cached

    log_info("providers", f"[Cache Miss] Fetching M3U URL ({provider_url})")

    try:
        content = fetch_url(provider_url, timeout=15)
        content = decrypt_content(content)
        channels = parse_m3u(content)
        channels_cache[cache_key] = channels
        return channels
    except Exception as e:
        log_error("providers", f"Error fetching M3U URL ({provider_url}): {e}")
        raise e
