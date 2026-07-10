"""
fill_missing_coords.py — 给 stations 表里 lat/lon 为空的站补坐标 (2026-07-10)

两步 fallback：
  1. **同城市 sibling**：找同 city_id 已有 lat/lon 的另一站（通常是大站），
     直接复用其坐标。偏差 < 5km，前端地图看不出。
  2. **Nominatim city 查询**：用 city_name + "China" 查城市级坐标。
     偏差 5–30km，对省/国尺度地图可接受。

对 14 个站 step 1 直接命中，对 26 个站 step 2 命中（Nominatim 一次成功率 > 90%）。

用法:
    python3 scripts/fill_missing_coords.py           # dry-run
    python3 scripts/fill_missing_coords.py --apply   # 写库
    python3 scripts/fill_missing_coords.py --only "红安西,砀山"   # 只处理指定站
"""
import argparse
import os
import sys
import time
import sqlite3
from typing import Optional

import requests
from geopy.geocoders import Nominatim
import geopy.exc

# Allow `from config import ...` when run as script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import GEOCODER_USER_AGENT, GEOCODER_RATE_LIMIT  # noqa: E402

DB = os.path.join(os.path.dirname(__file__), "..", "data", "train.db")


def get_missing(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT s.station_id, s.station_name, s.city_id, c.city_name, c.province
        FROM stations s LEFT JOIN cities c ON s.city_id = c.city_id
        WHERE s.lat IS NULL OR s.lon IS NULL
        ORDER BY s.station_name
    """).fetchall()


def sibling_lookup(conn: sqlite3.Connection, city_id: int,
                   exclude_station_id: int) -> Optional[tuple[float, float]]:
    """同城市其他站已有坐标 → 直接复用"""
    row = conn.execute("""
        SELECT lat, lon FROM stations
        WHERE city_id = ? AND lat IS NOT NULL AND lon IS NOT NULL
          AND station_id != ?
        ORDER BY station_id LIMIT 1
    """, (city_id, exclude_station_id)).fetchone()
    if row:
        return (row["lat"], row["lon"])
    return None


def build_geocoder() -> Nominatim:
    g = Nominatim(user_agent=GEOCODER_USER_AGENT)
    sess = requests.Session()
    # No urllib3 retries; we control backoff.
    from urllib3.util.retry import Retry
    retry = Retry(total=0)
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    if hasattr(g.adapter, "session"):
        g.adapter.session = sess
    return g


def nominatim_city(geocoder: Nominatim, city_name: str,
                   province: str = "") -> Optional[tuple[float, float]]:
    """Query Nominatim for the city itself. Returns (lat, lon) or None."""
    queries = [
        f"{city_name} 市 China",
        f"{city_name} China",
    ]
    if province and province != city_name:
        queries.insert(0, f"{city_name} {province} China")

    for q in queries:
        try:
            loc = geocoder.geocode(q, exactly_one=True, timeout=10)
            if loc:
                return (loc.latitude, loc.longitude)
        except geopy.exc.GeocoderRateLimited:
            time.sleep(5)
            try:
                loc = geocoder.geocode(q, exactly_one=True, timeout=10)
                if loc:
                    return (loc.latitude, loc.longitude)
            except Exception:
                pass
        except Exception:
            pass
        time.sleep(GEOCODER_RATE_LIMIT)
    return None


def write_coord(conn: sqlite3.Connection, station_name: str,
                lat: float, lon: float) -> None:
    conn.execute("""
        UPDATE stations
        SET lat = ?, lon = ?, geocoded_at = datetime('now')
        WHERE station_name = ?
    """, (lat, lon, station_name))
    conn.commit()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="写入数据库（不加就是 dry-run）")
    p.add_argument("--only", type=str, default="",
                   help="只处理逗号分隔的 station_name 列表")
    args = p.parse_args()

    only_set = {n.strip() for n in args.only.split(",") if n.strip()}

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    missing = get_missing(conn)
    if only_set:
        missing = [m for m in missing if m["station_name"] in only_set]

    print(f"[fill-missing] {'APPLY' if args.apply else 'DRY-RUN'} "
          f"— {len(missing)} station(s) to fill\n", flush=True)

    geocoder = build_geocoder()
    summary = {"sibling": [], "nominatim": [], "failed": []}

    for row in missing:
        name = row["station_name"]
        city = row["city_name"] or ""
        province = row["province"] or ""

        # Step 1: sibling
        coord = sibling_lookup(conn, row["city_id"], row["station_id"])
        if coord:
            lat, lon = coord
            source = "sibling"
        else:
            # Step 2: Nominatim
            coord = nominatim_city(geocoder, city, province)
            if coord:
                lat, lon = coord
                source = "nominatim"
            else:
                summary["failed"].append(name)
                print(f"  ✗ {name} (city={city!r}): not found",
                      flush=True)
                continue

        print(f"  {'✓' if source == 'sibling' else '◌'} {name}: "
              f"({lat:.4f}, {lon:.4f})  via {source}  (city={city})",
              flush=True)
        summary[source].append(name)

        if args.apply:
            write_coord(conn, name, lat, lon)

        time.sleep(GEOCODER_RATE_LIMIT)

    print(f"\n[fill-missing] Summary:")
    print(f"  sibling:     {len(summary['sibling'])}")
    print(f"  nominatim:   {len(summary['nominatim'])}")
    print(f"  failed:      {len(summary['failed'])}")
    if summary["failed"]:
        print(f"  failed list: {', '.join(summary['failed'])}")
    if not args.apply:
        print("\n  (dry-run; pass --apply to write)")


if __name__ == "__main__":
    main()