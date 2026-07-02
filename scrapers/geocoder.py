"""
Geocode stations using Nominatim (OpenStreetMap).

Handles rate limiting, retry, and caches results into DB.
"""
from __future__ import annotations

import time
import sqlite3
from typing import Any

import requests
from geopy.geocoders import Nominatim
import geopy.exc

from config import GEOCODER_USER_AGENT, GEOCODER_RATE_LIMIT
from db.repository import get_all_station_names, update_station_geo, transaction


def _build_geocoder() -> Nominatim:
    """Build a Nominatim client with strict timeout (no built-in retry).

    geopy 2.x adapters don't expose a max_retries knob, so we
    install an HTTPAdapter with max_retries=0 on the underlying session
    after construction. This ensures 429 returns immediately instead of
    silently hanging in urllib3's retry loop.
    """
    import urllib3.util.retry as _urllib3_retry
    geocoder = Nominatim(user_agent=GEOCODER_USER_AGENT)

    # Build a session with zero retries
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(
        max_retries=_urllib3_retry.Retry(total=0, status=0)
    ))
    session.mount("http://", requests.adapters.HTTPAdapter(
        max_retries=_urllib3_retry.Retry(total=0, status=0)
    ))

    # Replace the adapter's underlying pool manager / session.
    # geopy's URLLibAdapter wraps a requests.Session via `self.session`.
    if hasattr(geocoder.adapter, "session"):
        geocoder.adapter.session = session
    return geocoder


def geocode_station(
    geocoder: Nominatim,
    station_name: str,
    retries: int = 3,
) -> tuple[float, float] | None:
    """
    Geocode one station name.

    Tries: "station_name 站 China", "station_name railway station China",
           "station_name China". On 429 (rate-limited), backs off and retries.

    Returns (lat, lon) or None.
    """
    queries = [
        f"{station_name} 站 China",
        f"{station_name} railway station China",
        f"{station_name} China",
    ]

    for q in queries:
        loc = None
        for attempt in range(retries):
            try:
                loc = geocoder.geocode(q, exactly_one=True, timeout=10)
                if loc:
                    return loc.latitude, loc.longitude
                break  # valid response but no match — try next query
            except geopy.exc.GeocoderRateLimited:
                wait = 10 * (attempt + 1)
                print(f"  [geo] ⚠ {station_name} ({q}): rate-limited, "
                      f"waiting {wait}s (attempt {attempt+1}/{retries})")
                time.sleep(wait)
            except geopy.exc.GeocoderServiceError as e:
                if "429" in str(e):
                    wait = 10 * (attempt + 1)
                    print(f"  [geo] ⚠ {station_name} ({q}): 429 from Nominatim, "
                          f"waiting {wait}s (attempt {attempt+1}/{retries})")
                    time.sleep(wait)
                else:
                    print(f"  [geo] ✗ {station_name} ({q}): {e}")
                    break
            except Exception as e:
                print(f"  [geo] ✗ {station_name} ({q}): {e}")
                break
        # brief pause between query strategies
        time.sleep(GEOCODER_RATE_LIMIT)
    return None


def geocode_ungencoded_stations(
    conn: sqlite3.Connection | None = None,
    batch: bool = True,
) -> int:
    """
    Find all stations with NULL lat, geocode them, update DB.

    Returns count of newly geocoded stations.
    """
    with transaction(conn) as c:
        rows = c.execute(
            "SELECT station_name FROM stations WHERE lat IS NULL"
        ).fetchall()
        pending = [r["station_name"] for r in rows]

    if not pending:
        print("[geo] All stations already geocoded.")
        return 0

    print(f"[geo] Geocoding {len(pending)} stations...")
    geocoder = _build_geocoder()
    success = 0
    errors = []

    for i, station_name in enumerate(pending):
        result = geocode_station(geocoder, station_name)
        if result:
            lat, lon = result
            update_station_geo(station_name, lat, lon, conn=conn)
            success += 1
            if (i + 1) % 20 == 0 or i == 0:
                print(f"  [geo] ✓ {station_name}: ({lat:.4f}, {lon:.4f})  "
                      f"({i+1}/{len(pending)})")
        else:
            errors.append(station_name)
            print(f"  [geo] ✗ {station_name}: not found  "
                  f"({i+1}/{len(pending)})")

    print(f"[geo] Done: {success} geocoded, {len(errors)} failed")
    if errors:
        print(f"[geo] Failures ({len(errors)}): {', '.join(errors[:20])}"
              f"{'...' if len(errors) > 20 else ''}")

    return success
