"""
Geocode stations using Nominatim (OpenStreetMap).

Handles rate limiting, retry, and caches results into DB.

Strategy (2026-10-19 rewrite):
- Sibling-first: if another station in the same city already has coords,
  reuse them. Catches "玉环已拿到错误坐标，4 个 sibling 在 50km 内" 类问题
  without any network call.
- City-based Nominatim: query "{city_name} {province} China" instead of
  "{station_name} 站 China". Nominatim misinterprets the old query — it
  matches random addresses containing "站" (a shop in Shenyang, a town
  in Jilin) instead of the actual station.
- Strict no-retry adapter (429 surfaces immediately, we control backoff).
- 1.5s delay between successful stations (Nominatim wants ≤1 req/s).
- 30s cool-down after every 50 stations (slow drift that lets rate limit recover).

Known trap (2026-10-19): querying "<station> 站 China" returns wrong results
because Nominatim matches the literal character 站 in random addresses. We
never use that query strategy anymore — start with "{station_name} China"
(city-level) or skip entirely if city context is provided.
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


def _sibling_lookup(conn: sqlite3.Connection, city_id: int,
                     exclude_station_id: int) -> tuple[float, float] | None:
    """Return coords of any other station in the same city, or None.

    Catches "station placed 1300 km away from its city-mates" class of bug
    (the 2026-10-19 玉环 incident). On hit, no Nominatim call needed.
    """
    row = conn.execute(
        "SELECT lat, lon FROM stations "
        "WHERE city_id = ? AND lat IS NOT NULL AND lon IS NOT NULL "
        "  AND station_id != ? "
        "ORDER BY station_id LIMIT 1",
        (city_id, exclude_station_id),
    ).fetchone()
    if row:
        return (row["lat"], row["lon"])
    return None


def _city_lookup(
    geocoder: Nominatim,
    station_name: str,
    city_name: str,
    province: str = "",
) -> tuple[float, float] | None:
    """City-level Nominatim query. Prefer city+province disambiguation.

    Strategies (tried in order, first hit wins):
      1. "{station_name} {city_name} {province} China"
      2. "{city_name} {province} China"
      3. "{station_name} {city_name} China"
      4. "{city_name} 市 China"  (only if city_name is not already a 市)
      5. "{city_name} China"

    On 429, brief wait + 1 retry of the same query. Skips to next query
    if still rate-limited.
    """
    strategies = []
    if province:
        strategies.append(f"{station_name} {city_name} {province} China")
        strategies.append(f"{city_name} {province} China")
    if city_name:
        strategies.append(f"{station_name} {city_name} China")
        if not city_name.endswith("市"):
            strategies.append(f"{city_name} 市 China")
        strategies.append(f"{city_name} China")
    # Last-ditch: bare station name (no Chinese suffix that confuses Nominatim)
    strategies.append(station_name)

    for q in strategies:
        loc = None
        try:
            loc = geocoder.geocode(q, exactly_one=True, timeout=10)
            if loc:
                return loc.latitude, loc.longitude
        except Exception as e:
            if _is_rate_limit(e):
                time.sleep(2)
                try:
                    loc = geocoder.geocode(q, exactly_one=True, timeout=10)
                    if loc:
                        return loc.latitude, loc.longitude
                except Exception as e2:
                    if _is_rate_limit(e2):
                        continue  # try next strategy
                    print(f"  [geo] ✗ {station_name} ({q}): {e2}", flush=True)
                    return None
            else:
                print(f"  [geo] ✗ {station_name} ({q}): {e}", flush=True)
                return None
        time.sleep(GEOCODER_RATE_LIMIT)
    return None


def geocode_ungencoded_stations(
    conn: sqlite3.Connection | None = None,
) -> int:
    """
    Geocode all stations with NULL lat. Skip already-coded ones.

    For each pending station:
      1. **Sibling-first**: if any other station in the same city has coords,
         copy them. No network call. Handles 玉环=城市里其他站 etc.
      2. **City-based Nominatim**: query Nominatim with city+province context
         for disambiguation. Skip the buggy "<name> 站 China" strategy.

    Returns count of newly geocoded stations.
    """
    with transaction(conn) as c:
        rows = c.execute("""
            SELECT s.station_id, s.station_name, s.city_id, c.city_name, c.province
            FROM stations s
            LEFT JOIN cities c ON s.city_id = c.city_id
            WHERE s.lat IS NULL
            ORDER BY s.station_name
        """).fetchall()
        pending = [
            {
                "station_id": r["station_id"],
                "station_name": r["station_name"],
                "city_id": r["city_id"],
                "city_name": r["city_name"] or "",
                "province": r["province"] or "",
            }
            for r in rows
        ]

    if not pending:
        print("[geo] All stations already geocoded.", flush=True)
        return 0

    with transaction(conn) as c:
        already = c.execute(
            "SELECT COUNT(*) AS n FROM stations WHERE lat IS NOT NULL"
        ).fetchone()["n"]
    print(f"[geo] Geocoding {len(pending)} stations "
          f"(skipping {already} already geocoded)...", flush=True)

    geocoder = _build_geocoder()
    success = 0
    failed = []
    by_source = {"sibling": 0, "nominatim": 0}

    for i, info in enumerate(pending):
        name = info["station_name"]
        city = info["city_name"]
        province = info["province"]

        # Step 1: sibling (no network call)
        result = None
        if info["city_id"] is not None:
            result = _sibling_lookup(conn, info["city_id"], info["station_id"])
            if result:
                by_source["sibling"] += 1

        # Step 2: city-based Nominatim
        if not result and (city or province):
            result = _city_lookup(geocoder, name, city, province)
            if result:
                by_source["nominatim"] += 1

        if result:
            lat, lon = result
            update_station_geo(name, lat, lon, conn=conn)
            success += 1
            if (i + 1) % 10 == 0 or i == 0:
                src = ("sibling" if by_source["sibling"] > success - by_source["nominatim"]
                       else "nominatim")
                print(f"  [geo] ✓ {name}: ({lat:.4f}, {lon:.4f})  "
                      f"[{src}]  ({i+1}/{len(pending)})", flush=True)
        else:
            failed.append(name)
            print(f"  [geo] ✗ {name} (city={city!r}): not found  "
                  f"({i+1}/{len(pending)})", flush=True)

        if (i + 1) % 50 == 0:
            print(f"  [geo] cool-down 30s ...", flush=True)
            time.sleep(30)
        else:
            time.sleep(GEOCODER_RATE_LIMIT)

    print(f"[geo] Done: {success} geocoded "
          f"(sibling={by_source['sibling']}, nominatim={by_source['nominatim']}), "
          f"{len(failed)} failed", flush=True)
    if failed:
        print(f"[geo] Failures ({len(failed)}): "
              f"{', '.join(failed[:30])}"
              f"{'...' if len(failed) > 30 else ''}", flush=True)

    return success