"""
Geocode stations using Nominatim (OpenStreetMap).

Handles rate limiting, retry, and caches results into DB.

Strategy:
- Strict no-retry adapter (429 surfaces immediately, we control backoff).
- 2 query strategies: "{name} 站 China", "{name} China".
- On 429: wait 5s and retry the same query once. If still 429, skip to next query.
- 1.5s delay between successful stations (Nominatim wants ≤1 req/s).
- 30s cool-down after every 50 stations (slow drift that lets rate limit recover).
"""
from __future__ import annotations

import time
import sqlite3

import requests
from geopy.geocoders import Nominatim
import geopy.exc
import urllib3.util.retry as _urllib3_retry

from config import GEOCODER_USER_AGENT, GEOCODER_RATE_LIMIT
from db.repository import update_station_geo, transaction


def _build_geocoder() -> Nominatim:
    """Nominatim client with strict no-retry adapter (urllib3 disabled)."""
    geocoder = Nominatim(user_agent=GEOCODER_USER_AGENT)
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(
        max_retries=_urllib3_retry.Retry(total=0, status=0)
    ))
    session.mount("http://", requests.adapters.HTTPAdapter(
        max_retries=_urllib3_retry.Retry(total=0, status=0)
    ))
    if hasattr(geocoder.adapter, "session"):
        geocoder.adapter.session = session
    return geocoder


def _is_rate_limit(exc: Exception) -> bool:
    return (
        isinstance(exc, geopy.exc.GeocoderRateLimited)
        or (isinstance(exc, geopy.exc.GeocoderServiceError) and "429" in str(exc))
    )


def geocode_station(
    geocoder: Nominatim,
    station_name: str,
) -> tuple[float, float] | None:
    """
    Try multiple query strategies. On 429, wait 5s and retry once.
    Returns (lat, lon) or None.
    """
    queries = [
        f"{station_name} 站 China",
        f"{station_name} railway station China",
        f"{station_name} China",
    ]

    for q in queries:
        loc = None
        try:
            loc = geocoder.geocode(q, exactly_one=True, timeout=10)
            if loc:
                return loc.latitude, loc.longitude
            # Valid response but no match — try next strategy
        except Exception as e:
            if _is_rate_limit(e):
                time.sleep(2)  # brief wait, then 1 retry
                try:
                    loc = geocoder.geocode(q, exactly_one=True, timeout=10)
                    if loc:
                        return loc.latitude, loc.longitude
                except Exception as e2:
                    if _is_rate_limit(e2):
                        # Still rate-limited — skip this station
                        return None
                    else:
                        print(f"  [geo] ✗ {station_name} ({q}): {e2}", flush=True)
                        return None
            else:
                print(f"  [geo] ✗ {station_name} ({q}): {e}", flush=True)
                return None
        # Pause between strategies
        time.sleep(GEOCODER_RATE_LIMIT)
    return None


def geocode_ungencoded_stations(
    conn: sqlite3.Connection | None = None,
) -> int:
    """
    Geocode all stations with NULL lat. Skip already-coded ones.
    Returns count of newly geocoded stations.
    """
    with transaction(conn) as c:
        rows = c.execute(
            "SELECT station_name FROM stations WHERE lat IS NULL"
        ).fetchall()
        pending = [r["station_name"] for r in rows]

    if not pending:
        print("[geo] All stations already geocoded.", flush=True)
        return 0

    with transaction(conn) as c:
        already = c.execute(
            "SELECT COUNT(*) AS n FROM stations WHERE lat IS NOT NULL"
        ).fetchone()["n"]
    print(f"[geo] Geocoding {len(pending)} stations "
          f"(skipping {already} already geocoded)...",
          flush=True)
    geocoder = _build_geocoder()
    success = 0
    errors = []

    for i, station_name in enumerate(pending):
        result = geocode_station(geocoder, station_name)
        if result:
            lat, lon = result
            update_station_geo(station_name, lat, lon, conn=conn)
            success += 1
            if (i + 1) % 10 == 0 or i == 0:
                print(f"  [geo] ✓ {station_name}: ({lat:.4f}, {lon:.4f})  "
                      f"({i+1}/{len(pending)})", flush=True)
        else:
            errors.append(station_name)
            print(f"  [geo] ✗ {station_name}: not found  "
                  f"({i+1}/{len(pending)})", flush=True)

        # Cool-down every 50 stations to let rate-limit recover
        if (i + 1) % 50 == 0:
            print(f"  [geo] cool-down 30s ...", flush=True)
            time.sleep(30)
        else:
            time.sleep(GEOCODER_RATE_LIMIT)  # 1s between stations

    print(f"[geo] Done: {success} geocoded, {len(errors)} failed", flush=True)
    if errors:
        print(f"[geo] Failures ({len(errors)}): "
              f"{', '.join(errors[:30])}"
              f"{'...' if len(errors) > 30 else ''}", flush=True)

    return success