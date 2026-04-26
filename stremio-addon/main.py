"""
Cricfy Stremio Addon — entry point.

Reads credentials from environment variables, writes resource files,
then starts the FastAPI server with uvicorn.
"""

from pathlib import Path
import os
import json
import sys

# Load .env file if present (must happen before any os.getenv calls)
_env_file = Path(__file__).resolve().parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

CURRENT_DIR = Path(__file__).resolve().parent
RESOURCES_DIR = CURRENT_DIR / "resources"

SECRET1_PATH = RESOURCES_DIR / "secret1.txt"
SECRET2_PATH = RESOURCES_DIR / "secret2.txt"
PROPERTIES_PATH = RESOURCES_DIR / "cricfy_properties.json"


def setup_env():
    api_key = os.getenv("CRICFY_FIREBASE_API_KEY")
    app_id = os.getenv("CRICFY_FIREBASE_APP_ID")
    pkg_name = os.getenv("CRICFY_PACKAGE_NAME")
    secret1 = os.getenv("CRICFY_SECRET1")
    secret2 = os.getenv("CRICFY_SECRET2")

    missing = []
    if not api_key:
        missing.append("CRICFY_FIREBASE_API_KEY")
    if not app_id:
        missing.append("CRICFY_FIREBASE_APP_ID")
    if not pkg_name:
        missing.append("CRICFY_PACKAGE_NAME")
    if not secret1 and not secret2:
        missing.append("CRICFY_SECRET1 or CRICFY_SECRET2")

    if missing:
        print(f"[ERROR] Missing required environment variables: {', '.join(missing)}", flush=True)
        sys.exit(1)

    RESOURCES_DIR.mkdir(parents=True, exist_ok=True)

    if secret1:
        SECRET1_PATH.write_text(secret1, encoding="utf-8")
    else:
        # Write empty placeholder so crypto_utils doesn't crash on read
        SECRET1_PATH.write_text("", encoding="utf-8")

    if secret2:
        SECRET2_PATH.write_text(secret2, encoding="utf-8")
    else:
        SECRET2_PATH.write_text("", encoding="utf-8")

    PROPERTIES_PATH.write_text(
        json.dumps({
            "cricfy_firebase_api_key": api_key,
            "cricfy_firebase_app_id": app_id,
            "cricfy_package_name": pkg_name,
        }, separators=(",", ":")),
        encoding="utf-8",
    )

    print("[INFO] Resource files written successfully.", flush=True)


if __name__ == "__main__":
    setup_env()

    # Ensure the working directory and sys.path point to stremio-addon/
    # so that uvicorn can find app.py and `from lib.xxx import ...` works
    # regardless of which directory the user ran `python` from.
    os.chdir(CURRENT_DIR)
    if str(CURRENT_DIR) not in sys.path:
        sys.path.insert(0, str(CURRENT_DIR))

    # Import uvicorn only after resource files are in place (lib modules read them at import time)
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    print(f"[INFO] Starting Cricfy Stremio Addon on {host}:{port}", flush=True)
    print(f"[INFO] Add to Stremio: http://{host}:{port}/manifest.json", flush=True)

    uvicorn.run("app:app", host=host, port=port, reload=False)
