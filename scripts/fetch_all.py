"""
Main pipeline: fetch wuhu page → compare update time → skip or full scrape.

Usage:
    python -m scripts.fetch_all
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import TRAIN_DETAIL_URL_TEMPLATE, INTER_REQUEST_SLEEP, INTER_BATCH_SLEEP, HTTP_TIMEOUT
from db.connection import get_conn
from db.repository import (
    create_tables, get_meta, set_meta,
    upsert_city, upsert_station, overwrite_trains_and_stops,
    db_size,
)
from scrapers.http import build_session, Sleeper
from scrapers.wuhu_page import parse_wuhu_page
from scrapers.train_detail import fetch_train_detail


def _ensure_station_in_db(
    station_name: str,
    url_to_station_id: dict,
    station_id_map: dict,
    conn,
):
    """Ensure a station exists in DB, return its station_id. Handles caching."""
    if station_name in station_id_map:
        return station_id_map[station_name]

    entry = url_to_station_id.get(station_name, {})
    city_name = entry.get("city", station_name)
    province = entry.get("province", "")
    station_url = entry.get("url", "")

    city_id = upsert_city(city_name, province, conn=conn)
    sid = upsert_station(station_name, city_id, station_url, conn=conn)
    station_id_map[station_name] = sid
    return sid


def run():
    """Main entry."""
    print("=" * 60)
    print("  train_crecc — 芜湖站数据抓取")
    print("=" * 60)

    # 1. Ensure DB exists
    conn = get_conn()
    create_tables(conn)
    conn.close()

    # 2. Fetch main page
    print("\n[1/4] Fetching main station page...")
    session = build_session()
    try:
        page_data = parse_wuhu_page(session, cache_html=True)
    except Exception as e:
        print(f"  ✗ Failed to fetch station page: {e}")
        return

    data_update_time = page_data["data_update_time"]
    station_name = page_data["station_name"]
    trains_list = page_data["trains"]
    new_stations = page_data["new_stations"]

    print(f"\n  Station: {station_name}")
    print(f"  Page update: {data_update_time}")
    print(f"  Trains found: {len(trains_list)}")
    print(f"  Stations found: {len(new_stations)}")

    # 3. Skip-if-unchanged check
    print("\n[2/4] Checking update timestamp...")
    last_updated = get_meta(key="last_updated")
    if last_updated and last_updated == data_update_time:
        print(f"  ✓ No update since {last_updated}. Skipping train details.")
        set_meta(key="last_fetch_status", value="unchanged")
        print("\nDone — nothing to do.")
        return
    else:
        print(f"  Last: {last_updated}")
        print(f"  Now:  {data_update_time}")
        print("  → Change detected. Proceeding with full scrape.")

    # 4. Build station name → metadata mapping
    url_to_station_id = {}
    for name, meta in new_stations.items():
        url_to_station_id[name] = meta

    # 5. Fetch every train detail
    print(f"\n[3/4] Fetching {len(trains_list)} train detail pages...")
    sleeper = Sleeper(interval=INTER_REQUEST_SLEEP)

    all_train_rows = []   # for DB insert
    all_stop_rows = []    # for DB insert

    # Cache mapping: station_name → station_id (to minimize DB calls)
    # We'll fill it in during the loop.
    station_id_cache = {}

    def get_sid(name):
        nonlocal station_id_cache
        if name not in station_id_cache:
            sid = _ensure_station_in_db(name, url_to_station_id, station_id_cache, conn)
            station_id_cache[name] = sid
        return station_id_cache[name]

    failed = 0
    skipped = 0

    for idx, train_meta in enumerate(trains_list):
        train_code = train_meta["train_code"]
        detail_url = train_meta["detail_url"]

        # Skip trains we already have cached origin/dest for
        origin_name = train_meta["origin_station_name"]
        dest_name = train_meta["dest_station_name"]

        result = fetch_train_detail(session, train_code, detail_url, sleeper,
                                     cache_html=True)

        if result is None:
            failed += 1
            # Still save what we have from the main page (no stops)
            origin_id = get_sid(origin_name)
            dest_id = get_sid(dest_name)

            all_train_rows.append({
                "train_code": train_code,
                "train_class": train_meta.get("train_class", ""),
                "train_class_full": train_meta.get("train_class_full", ""),
                "origin_station_id": origin_id,
                "origin_time": train_meta.get("origin_time", ""),
                "dest_station_id": dest_id,
                "dest_time": train_meta.get("dest_time", ""),
                "total_duration_minutes": train_meta.get("total_duration_minutes", 0),
                "stop_count": 0,
                "detail_url": detail_url,
                "relation_to_wuhu": train_meta.get("relation_to_wuhu", "passing"),
            })
            continue

        train_info = result
        stops = train_info["stops"]

        # Override train_class if detail page has nicer info
        cls = train_info.get("train_class") or train_meta.get("train_class", "")
        cls_full = train_info.get("train_class_full") or train_meta.get("train_class_full", "")

        # Get origin/dest from detail page if available
        detail_origin = train_info.get("origin_station_name")
        detail_dest = train_info.get("dest_station_name")

        # Find first and last stop from the stop list (most reliable)
        if stops:
            first_stop = stops[0]
            last_stop = stops[-1]
            det_origin_name = first_stop["station_name"]
            det_dest_name = last_stop["station_name"]
        else:
            det_origin_name = detail_origin or origin_name
            det_dest_name = detail_dest or dest_name

        origin_id = get_sid(det_origin_name)
        dest_id = get_sid(det_dest_name)

        # Ensure all stop stations exist in DB
        for st in stops:
            st_name = st["station_name"]
            if st_name not in station_id_cache:
                # Try to find station info - create from stop link URL
                st_url = st.get("station_url", "")
                st_city, st_prov = _infer_city_from_detail_url(st_url, st_name)
                if st_name not in url_to_station_id:
                    url_to_station_id[st_name] = {
                        "station_name": st_name,
                        "city": st_city,
                        "province": st_prov,
                        "url": st_url,
                    }
                sid = get_sid(st_name)
                station_id_cache[st_name] = sid

        # Build the train row
        duration = train_info.get("total_duration_minutes") or train_meta.get("total_duration_minutes", 0)
        all_train_rows.append({
            "train_code": train_code,
            "train_class": cls,
            "train_class_full": cls_full,
            "origin_station_id": origin_id,
            "origin_time": stops[0]["depart_time"] if stops and stops[0]["depart_time"] else (train_info.get("origin_time") or train_meta.get("origin_time", "")),
            "dest_station_id": dest_id,
            "dest_time": stops[-1]["arrive_time"] if stops and stops[-1]["arrive_time"] else (train_info.get("dest_time") or train_meta.get("dest_time", "")),
            "total_duration_minutes": duration,
            "stop_count": len(stops),
            "detail_url": detail_url,
            "relation_to_wuhu": train_meta.get("relation_to_wuhu", "passing"),
        })

        # Build stop rows
        for st in stops:
            sid = station_id_cache.get(st["station_name"])
            if sid is None:
                sid = get_sid(st["station_name"])
            all_stop_rows.append({
                "train_code": train_code,
                "sequence": st["sequence"],
                "station_id": sid,
                "arrive_time": st.get("arrive_time", ""),
                "arrive_day": st.get("arrive_day", 0),
                "depart_time": st.get("depart_time", ""),
                "depart_day": st.get("depart_day", 0),
                "stop_duration": st.get("stop_duration", 0),
                "running_minutes": st.get("running_minutes", 0),
            })

        # Progress
        if (idx + 1) % 50 == 0:
            print(f"  [{idx+1}/{len(trains_list)}] ... {train_code}")
            time.sleep(INTER_BATCH_SLEEP)

    # 6. Write to DB (full overwrite)
    print(f"\n[4/4] Writing to database...")
    conn = get_conn()
    overwrite_trains_and_stops(all_train_rows, all_stop_rows, conn=conn)

    # 7. Update meta
    set_meta(key="last_updated", value=data_update_time, conn=conn)
    set_meta(key="last_fetch_status", value="ok")
    from datetime import datetime, timezone
    set_meta(key="last_fetch_at", value=datetime.now(timezone.utc).isoformat(), conn=conn)
    conn.close()

    # 8. Summary
    stats = db_size()
    print(f"\n{'=' * 60}")
    print(f"  Done — {stats['trains']} trains, {stats['stops']} stops, {stats['stations']} stations")
    print(f"  Failed: {failed}")
    print(f"{'=' * 60}")


def _infer_city_from_detail_url(station_url: str, station_name: str) -> tuple[str, str]:
    """Infer city and province from a stop's station URL or name."""
    from scrapers.wuhu_page import _infer_city_province_from_url
    if station_url:
        return _infer_city_province_from_url(station_url)
    # Fallback: guess from station name suffix
    # 杭州西 → 杭州,  南昌西 → 南昌,  芜湖 → 芜湖
    import re
    city = re.sub(r"(东|西|南|北|站)$", "", station_name)
    return (city, "")


if __name__ == "__main__":
    run()
