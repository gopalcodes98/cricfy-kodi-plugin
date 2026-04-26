from pathlib import Path
from cachetools import TTLCache

# Base path for resources (secrets, firebase config)
ADDON_PATH = Path(__file__).parent.parent

# In-memory caches (TTL handled automatically)
providers_cache: TTLCache = TTLCache(maxsize=10, ttl=86400)   # 24 hours
channels_cache: TTLCache = TTLCache(maxsize=200, ttl=3600)    # 1 hour
