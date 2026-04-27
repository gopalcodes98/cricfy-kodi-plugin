# Cricfy Stremio Addon

A Stremio addon that exposes live cricket and sports streams from Cricfy providers. Built with Python and FastAPI, reusing the same decryption and M3U parsing logic from the Kodi plugin.

---

## How It Works

1. On startup, the addon reads credentials from environment variables and writes them to `resources/` as files.
2. It fetches the list of providers dynamically from Firebase Remote Config (cached 24 hours).
3. For each provider, it fetches and decrypts the M3U playlist (cached 1 hour).
4. Stremio clients browse channels via the catalog, click to view metadata, and play streams.
5. Streams that require custom HTTP headers (User-Agent, Referer, Cookie) are routed through a built-in proxy endpoint so the player receives them correctly.

---

## Prerequisites

- Python 3.12 or higher
- `pip` (or `uv` if preferred)
- Valid Cricfy credentials:
  - Firebase API key and App ID
  - Android package name
  - At least one decryption secret key

---

## Local Setup

### 1. Clone and navigate

```bash
git clone <repo-url>
cd cricfy-kodi-plugin/stremio-addon
```

### 2. Create a virtual environment

```bash
python -m venv .venv
```

Activate it:

- **Windows (Command Prompt)**:
  ```cmd
  .venv\Scripts\activate.bat
  ```
- **Windows (PowerShell)**:
  ```powershell
  .venv\Scripts\Activate.ps1
  ```
- **Linux / macOS**:
  ```bash
  source .venv/bin/activate
  ```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

Or if you use `uv`:

```bash
uv sync
```

### 4. Set environment variables

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Open `.env` and set the following:

| Variable | Required | Description |
|---|---|---|
| `CRICFY_FIREBASE_API_KEY` | Yes | Firebase Web API key |
| `CRICFY_FIREBASE_APP_ID` | Yes | Firebase App ID (format: `1:123456:android:abcdef`) |
| `CRICFY_PACKAGE_NAME` | Yes | Android package name (e.g. `com.example.cricfy`) |
| `CRICFY_SECRET1` | At least one | Decryption key in `hex_key:hex_iv` format |
| `CRICFY_SECRET2` | At least one | Fallback decryption key in `hex_key:hex_iv` format |
| `HOST` | No | Bind host (default: `0.0.0.0`) |
| `PORT` | No | Bind port (default: `8000`) |

Export the variables in your shell before running:

**Linux / macOS:**
```bash
export CRICFY_FIREBASE_API_KEY=your_api_key
export CRICFY_FIREBASE_APP_ID=1:123456789:android:abcdef123456
export CRICFY_PACKAGE_NAME=com.example.cricfy
export CRICFY_SECRET1=aabbccdd11223344aabbccdd11223344:11223344aabbccdd11223344aabbccdd
export CRICFY_SECRET2=
export PORT=8000
```

**Windows (Command Prompt):**
```cmd
set CRICFY_FIREBASE_API_KEY=your_api_key
set CRICFY_FIREBASE_APP_ID=1:123456789:android:abcdef123456
set CRICFY_PACKAGE_NAME=com.example.cricfy
set CRICFY_SECRET1=aabbccdd11223344aabbccdd11223344:11223344aabbccdd11223344aabbccdd
set PORT=8000
```

**Windows (PowerShell):**
```powershell
$env:CRICFY_FIREBASE_API_KEY = "your_api_key"
$env:CRICFY_FIREBASE_APP_ID = "1:123456789:android:abcdef123456"
$env:CRICFY_PACKAGE_NAME = "com.example.cricfy"
$env:CRICFY_SECRET1 = "aabbccdd11223344aabbccdd11223344:11223344aabbccdd11223344aabbccdd"
$env:PORT = "8000"
```

### 5. Run the addon

```bash
python main.py
```

Or for development with auto-reload (credentials must already be in `resources/` from a previous `python main.py` run):

```bash
uvicorn app:app --reload
```

You should see output like:

```
[INFO] Resource files written successfully.
[INFO] Starting Cricfy Stremio Addon on 0.0.0.0:8000
[INFO] Add to Stremio: http://0.0.0.0:8000/manifest.json
INFO:     Started server process [...]
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### 6. Verify the addon is running

Open your browser or run:

```bash
curl http://localhost:8000/manifest.json
```

Expected response:

```json
{
  "id": "community.cricfy.live",
  "version": "1.0.0",
  "name": "Cricfy Live Sports",
  ...
}
```

Test the catalog endpoint:

```bash
curl http://localhost:8000/catalog/tv/cricfy-live.json
```

This should return a list of live channels as Stremio meta objects.

---

## Add to Stremio

### Desktop / Web

1. Open Stremio.
2. Click the puzzle icon (Addons) in the top-right.
3. Click **Install from URL** (or the search bar at the top of the addons page).
4. Enter:
   ```
   http://localhost:8000/manifest.json
   ```
5. Click **Install**.
6. Go to the **Discover** tab → select **Cricfy Live Sports** from the catalog dropdown.

> **Note:** Stremio on desktop can load `http://localhost` addons directly. If you are accessing Stremio through the web app at `https://web.strem.io`, the browser will block mixed content (HTTP from an HTTPS page). Use the desktop app for local testing.

---

## Caching Behaviour

| Data | Cache TTL | Notes |
|---|---|---|
| Provider list | 24 hours | Fetched from Firebase Remote Config |
| Channel list per provider | 1 hour | Fetched and decrypted from M3U URL |

The cache is in-memory. Restarting the server clears all cached data.

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/manifest.json` | Stremio addon manifest |
| `GET` | `/catalog/tv/cricfy-live.json` | All live channels (supports `?genre=` and `?skip=`) |
| `GET` | `/meta/tv/{id}.json` | Channel metadata |
| `GET` | `/stream/tv/{id}.json` | Stream URL for a channel |
| `GET` | `/proxy/stream?id={id}` | Fetches and rewrites the initial DASH/HLS manifest with proxy URLs |
| `GET` | `/proxy/media?id={id}&url={url}` | Proxies individual segments and nested manifests with channel headers |
| `GET` | `/proxy/raw?url={url}&Header=Value` | Generic URL proxy for backward compatibility |

---

## Project Structure

```
stremio-addon/
├── lib/
│   ├── config.py       # In-memory cache setup, ADDON_PATH
│   ├── logger.py       # Standard Python logging wrapper
│   ├── providers.py    # Provider and channel fetching with caching
│   ├── crypto_utils.py # AES-CBC decryption for providers and M3U content
│   ├── m3u_parser.py   # M3U playlist parser (PlaylistItem)
│   ├── remote_config.py # Firebase Remote Config fetcher
│   └── req.py          # HTTP fetch utility
├── handlers/
│   ├── catalog.py      # Stremio catalog handler
│   ├── meta.py         # Stremio meta handler
│   └── stream.py       # Stremio stream handler + proxy URL builder
├── tests/
│   └── test_proxy_rewrite.py # Proxy URL rewriting tests
├── resources/          # Written at startup from env vars (git-ignored)
│   ├── secret1.txt
│   ├── secret2.txt
│   └── cricfy_properties.json
├── app.py              # FastAPI application and all routes
├── id_utils.py         # Stable channel ID generation and parsing
├── main.py             # Entry point: env setup + uvicorn start
├── manifest.py         # Stremio manifest definition
├── progress.md         # Development notes and in-progress work
├── pyproject.toml
├── requirements.txt
├── Dockerfile
├── .env.example
└── README.md
```
stremio-addon/
├── lib/
│   ├── config.py          # In-memory cache setup, ADDON_PATH
│   ├── logger.py          # Standard Python logging wrapper
│   ├── providers.py       # Provider and channel fetching with caching
│   ├── crypto_utils.py    # AES-CBC decryption for providers and M3U content
│   ├── m3u_parser.py      # M3U playlist parser (PlaylistItem)
│   ├── remote_config.py   # Firebase Remote Config fetcher
│   └── req.py             # HTTP fetch utility
├── handlers/
│   ├── catalog.py         # Stremio catalog handler
│   ├── meta.py            # Stremio meta handler
│   └── stream.py          # Stremio stream handler + proxy URL builder
├── resources/             # Written at startup from env vars (git-ignored)
│   ├── secret1.txt
│   ├── secret2.txt
│   └── cricfy_properties.json
├── app.py                 # FastAPI application and all routes
├── id_utils.py            # Stable channel ID generation and parsing
├── main.py                # Entry point: env setup + uvicorn start
├── manifest.py            # Stremio manifest definition
├── pyproject.toml
├── Dockerfile
└── .env.example
```

---

## Troubleshooting

**`Missing required environment variables` error on startup**
Make sure all required env vars are exported in the same shell session before running `python main.py`.

**Catalog returns empty list**
Firebase Remote Config fetch may have failed. Check the logs for `[remote_config]` errors. Verify your `CRICFY_FIREBASE_API_KEY`, `CRICFY_FIREBASE_APP_ID`, and `CRICFY_PACKAGE_NAME` values.

**Decryption failed errors in logs**
Your secret keys may be incorrect or in the wrong format. The expected format is `hex_encoded_key:hex_encoded_iv` with no spaces.

**Stream does not play in Stremio**
Streams requiring custom headers are proxied through `/proxy/stream`. If the stream still fails, check the proxy logs for upstream HTTP errors. The stream source may be geo-restricted or temporarily unavailable.

**Port already in use**
Change the port with `export PORT=8001` (or `set PORT=8001` on Windows) before running.
