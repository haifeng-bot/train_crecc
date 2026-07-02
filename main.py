#!/usr/bin/env python3
"""
train_crecc — CLI entry point.

Usage:
    python main.py fetch              Full scrape pipeline
    python main.py status             Show database status
    python main.py geocode            Geocode ungeocoded stations
    python main.py directions         Compute 8-way directions for all stations
    python main.py reach <minutes>    Query reachable stations within N minutes
"""
from __future__ import annotations

import sys
import argparse
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from db.connection import get_conn, transaction
from db.repository import (
    create_tables, get_meta, set_meta,
    overwrite_trains_and_stops, db_size,
    query_reach, query_fastest_route, query_city_to_wuhu,
)
from scrapers.http import build_session, Sleeper
from scrapers.wuhu_page import parse_wuhu_page
from scrapers.train_detail import fetch_train_detail
from scrapers.geocoder import geocode_ungencoded_stations
from analysis.directions import compute_all_directions
from config import INTER_REQUEST_SLEEP, INTER_BATCH_SLEEP


def cmd_fetch():
    """Full scrape pipeline: check update → skip or scrape all."""
    create_tables()  # ensure DB exists
    conn = get_conn()

    print("[fetch] Fetching main station page...")
    session = build_session()
    try:
        page_data = parse_wuhu_page(session, cache_html=True)
    except Exception as e:
        print(f"  ✗ {e}")
        conn.close()
        sys.exit(1)

    update_time = page_data["data_update_time"]
    trains = page_data["trains"]
    stations = page_data["new_stations"]

    # Skip-if-unchanged
    last = get_meta("last_updated", conn=conn)
    if last and last == update_time:
        print(f"  ✓ No change since {last}, skipping.")
        set_meta("last_fetch_status", "unchanged", conn=conn)
        conn.close()
        return

    print(f"\n  Trains: {len(trains)} | Stations: {len(stations)}")
    print(f"  Last: {last} → Now: {update_time}")
    print("  → Full scrape starting...\n")

    # Fetch details
    sleeper = Sleeper(interval=INTER_REQUEST_SLEEP)
    train_rows = []
    stop_rows = []
    station_id_cache = {}
    failed = 0

    def _get_station_id(name):
        if name not in station_id_cache:
            sid = _ensure_station(name, stations, conn)
            station_id_cache[name] = sid
        return station_id_cache[name]

    for idx, tm in enumerate(trains):
        code = tm["train_code"]
        detail = fetch_train_detail(session, code, tm["detail_url"], sleeper,
                                     cache_html=True)

        if detail is None:
            failed += 1
            train_rows.append(_make_train_row(tm, tm["origin_station_name"],
                                              tm["dest_station_name"], _get_station_id, 0))
            continue

        stops = detail.get("stops", [])
        if not stops:
            failed += 1
            train_rows.append(_make_train_row(tm, tm["origin_station_name"],
                                              tm["dest_station_name"], _get_station_id, 0))
            continue

        # Origin = first stop, Dest = last stop (most reliable)
        o_name = stops[0]["station_name"]
        d_name = stops[-1]["station_name"]

        # Register all stations in stops
        for st in stops:
            _get_station_id(st["station_name"])

        train_rows.append({
            "train_code": code,
            "train_class": detail.get("train_class") or tm.get("train_class", ""),
            "train_class_full": detail.get("train_class_full") or tm.get("train_class_full", ""),
            "origin_station_id": _get_station_id(o_name),
            "origin_time": stops[0].get("depart_time") or tm.get("origin_time", ""),
            "dest_station_id": _get_station_id(d_name),
            "dest_time": stops[-1].get("arrive_time") or tm.get("dest_time", ""),
            "total_duration_minutes": detail.get("total_duration_minutes") or tm.get("total_duration_minutes", 0),
            "stop_count": len(stops),
            "detail_url": tm["detail_url"],
            "relation_to_wuhu": tm.get("relation_to_wuhu", "passing"),
        })

        for st in stops:
            stop_rows.append({
                "train_code": code,
                "sequence": st["sequence"],
                "station_id": station_id_cache[st["station_name"]],
                "arrive_time": st.get("arrive_time", ""),
                "arrive_day": st.get("arrive_day", 0),
                "depart_time": st.get("depart_time", ""),
                "depart_day": st.get("depart_day", 0),
                "stop_duration": st.get("stop_duration", 0),
                "running_minutes": st.get("running_minutes", 0),
            })

        if (idx + 1) % 50 == 0:
            print(f"  [{idx+1}/{len(trains)}] {code}")
            import time; time.sleep(INTER_BATCH_SLEEP)  # extra rest

    # Write to DB (reuse same conn)
    overwrite_trains_and_stops(train_rows, stop_rows, conn=conn)
    set_meta("last_updated", update_time, conn=conn)
    set_meta("last_fetch_status", "ok", conn=conn)
    from datetime import datetime, timezone
    set_meta("last_fetch_at", datetime.now(timezone.utc).isoformat(), conn=conn)
    stats = db_size(conn=conn)
    conn.close()

    print(f"\n  ✓ Done: {stats['trains']} trains, {stats['stops']} stops, "
          f"{stats['stations']} stations | Failed: {failed}")


def _ensure_station(name, stations_dict, conn):
    """Create city + station if needed, return station_id."""
    from db.repository import upsert_city, upsert_station
    entry = stations_dict.get(name, {})
    city = entry.get("city", "") or _infer_city(name)
    province = entry.get("province", "")
    url = entry.get("url", "")
    cid = upsert_city(city, province, conn=conn)
    return upsert_station(name, cid, url, conn=conn)


def _infer_city(station_name: str) -> str:
    """Fallback city inference from station name."""
    import re
    # Strip directional suffix: 杭州西 → 杭州, 南昌西 → 南昌
    base = re.sub(r'(东|西|南|北)$', '', station_name)
    # Also strip 站
    base = base.replace('站', '')
    return base if base else station_name


def _make_train_row(tm, o_name, d_name, get_sid, stop_count):
    return {
        "train_code": tm["train_code"],
        "train_class": tm.get("train_class", ""),
        "train_class_full": tm.get("train_class_full", ""),
        "origin_station_id": get_sid(o_name),
        "origin_time": tm.get("origin_time", ""),
        "dest_station_id": get_sid(d_name),
        "dest_time": tm.get("dest_time", ""),
        "total_duration_minutes": tm.get("total_duration_minutes", 0),
        "stop_count": stop_count,
        "detail_url": tm.get("detail_url", ""),
        "relation_to_wuhu": tm.get("relation_to_wuhu", "passing"),
    }


def cmd_status():
    """Show DB summary."""
    stats = db_size()
    print(f"Database: {stats}")

    for key in ["last_updated", "last_fetch_at", "last_fetch_status"]:
        val = get_meta(key)
        if val:
            print(f"  {key}: {val}")


def cmd_geocode():
    """Geocode ungeocoded stations."""
    conn = get_conn()
    count = geocode_ungencoded_stations(conn=conn)
    conn.close()
    print(f"Geocoded: {count}")


def cmd_directions():
    """Compute 8-way directions."""
    conn = get_conn()
    compute_all_directions(conn=conn)
    conn.close()


def cmd_reach(minutes: int):
    """Query reachable stations + fastest-train route."""
    conn = get_conn()
    rows = query_reach(minutes, conn=conn)
    print(f"{'Station':<12} {'Dir':<4} {'Min':>4} {'Tr':>3}  Fastest train & route")
    print("-" * 80)
    for r in rows:
        stops = query_fastest_route(r['fastest_train_code'],
                                    r['station_id'], conn=conn)

        if stops:
            route_str = " → ".join(
                f"{s['station_name']}({s['running_minutes']}m)"
                for s in stops
            )
        else:
            route_str = "(no route)"

        print(f"{r['station_name']:<12} {r['direction'] or '?':<4} "
              f"{r['min_minutes']:>4} {r['train_count']:>3}  "
              f"{r['fastest_train_code']}: {route_str}")
    conn.close()


def cmd_city(city: str):
    """Query trains from 芜湖 to a city."""
    conn = get_conn()
    rows = query_city_to_wuhu(city, conn=conn)
    conn.close()
    if not rows:
        print(f"No trains found from 芜湖 to {city}.")
        return
    print(f"{'Train':<10} {'From':<12} {'Dep':>5} {'To':<12} {'Arr':>5} {'Dur':>5}")
    print("-" * 60)
    for r in rows:
        print(f"{r['train_code']:<10} {r['from_station']:<12} "
              f"{r['from_dep']:>5} {r['to_station']:<12} "
              f"{r['to_arr']:>5} {r['duration_min']:>5}")


def main():
    parser = argparse.ArgumentParser(description="train_crecc — 芜湖站数据维护")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("fetch", help="全量抓取（跳过若更新时间未变）")
    sub.add_parser("status", help="显示数据库状态")
    sub.add_parser("geocode", help="地理编码未识别的车站")
    sub.add_parser("directions", help="计算从芜湖出发的 8 方位方向")

    reach_p = sub.add_parser("reach", help="查询 N 分钟内可达站")
    reach_p.add_argument("minutes", type=int)

    city_p = sub.add_parser("city", help="查询从芜湖到某城市的车次")
    city_p.add_argument("city", type=str)

    args = parser.parse_args()

    if args.command == "fetch":
        cmd_fetch()
    elif args.command == "status":
        cmd_status()
    elif args.command == "geocode":
        cmd_geocode()
    elif args.command == "directions":
        cmd_directions()
    elif args.command == "reach":
        cmd_reach(args.minutes)
    elif args.command == "city":
        cmd_city(args.city)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
