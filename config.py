"""
Global config for train_crecc project.

Single source of truth for URLs, paths, HTTP headers, timeouts.
Edit values here; everywhere else imports from here.
"""
from pathlib import Path

# --- Paths ---
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_HTML_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "train.db"

RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)

# --- Source URLs ---
STATION_PAGE_URL = "https://www.crecc.com/anhui/wuhu/wuhu.html"
TRAIN_DETAIL_URL_TEMPLATE = "https://www.crecc.com/huoche/{code}.html"

# --- HTTP ---
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 30            # seconds
HTTP_RETRY = 3
HTTP_RETRY_BACKOFF = 2.0     # exponential factor

# Polite throttling — crecc is a 3rd-party site, don't hammer it
INTER_REQUEST_SLEEP = 1.5    # seconds between train detail fetches
INTER_BATCH_SLEEP = 5.0      # every 50 trains, take a longer break

# --- Fetch / skip-if-unchanged ---
META_KEY_LAST_UPDATED = "last_updated"     # value = page's "更新时间" string
META_KEY_LAST_FETCH_AT = "last_fetch_at"   # value = ISO timestamp of our fetch
META_KEY_LAST_FETCH_STATUS = "last_fetch_status"  # ok | unchanged | failed

# --- Geocoding ---
GEOCODER_USER_AGENT = "train_crecc/0.1 (https://github.com/haifeng-bot; wuhu train monitor)"
GEOCODER_RATE_LIMIT = 1.1   # seconds between requests (Nominatim ToS: ≤1 req/sec)

# --- Hub (the visual + query anchor) ---
HUB_STATION_NAME = "芜湖"   # the central station all directions are computed from
