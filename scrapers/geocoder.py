"""
Geocode stations using Nominatim (OpenStreetMap).

Handles rate limiting, retry, and caches results into DB.
"""
from __future__ import annotations

import time
import sqlite3
from typing import Any

from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter

from config import GEOCODER_USER_AGENT, GEOCODER_RATE_LIMIT
from db.repository import get_all_station_names, update_station_geo, transaction


def _build_geocoder() -> Nominatim:
    return Nominatim(user_agent=GEOCODER_USER_AGENT)


def geocode_station(
    geocoder: Nominatim,
    station_name: str,
    retries: int = 2,
) -> tuple[float, float] | None:
    """
    Geocode one station name.

    Tries: "station_name 火车站 China", "station_name China",
           "station_name 站 China"

    Returns (lat, lon) or None.
    """
    queries = [
        f"{station_name} 站 China",
        f"{station_name} railway station China",
        f"{station_name} China",
    ]

    for q in queries:
        for attempt in range(retries):
            try:
                loc = geocoder.geocode(q, exactly_one=True, timeout=10)
                if loc:
                    return loc.latitude, loc.longitude
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(GEOCODER_RATE_LIMIT * 2)
                else:
                    print(f"  [geo] ✗ {station_name} ({q}): {e}")
        # brief pause between query strategies
        if not loc:
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
    geocode = RateLimiter(geocoder.geocode, min_delay_seconds=GEOCODER_RATE_LIMIT)
    success = 0
    errors = []

    for i, station_name in enumerate(pending):
        result = geocode_station(geocoder, station_name)
        if result:
            lat, lon = result
            update_station_geo(station_name, lat, lon, conn=conn)
            success += 1
            if i % 20 == 0 or i == 0:
                print(f"  [geo] ✓ {station_name}: ({lat:.4f}, {lon:.4f})  ({i+1}/{len(pending)})")
        else:
            errors.append(station_name)
            if i % 20 == 0:
                print(f"  [geo] ? {station_name}: not found  ({i+1}/{len(pending)})")

    print(f"[geo] Done: {success} geocoded, {len(errors)} failed")
    if errors:
        print(f"[geo] Failures ({len(errors)}): {', '.join(errors[:20])}{'...' if len(errors) > 20 else ''}")

    return success
