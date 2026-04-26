MANIFEST = {
    "id": "community.cricfy.live",
    "version": "1.0.0",
    "name": "Cricfy Live Sports",
    "description": "Live cricket and sports streams via Cricfy providers.",
    "resources": ["catalog", "meta", "stream"],
    "types": ["tv"],
    "catalogs": [
        {
            "type": "tv",
            "id": "cricfy-live",
            "name": "Cricfy Live Sports",
            "extra": [
                {"name": "genre", "isRequired": False},
                {"name": "skip", "isRequired": False},
            ],
        }
    ],
    "behaviorHints": {
        "configurable": False,
        "adult": False,
    },
}
