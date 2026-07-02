"""
Repository layer — all DB reads/writes in one place.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from config import (
    DATA_DIR,
    HUB_STATION_NAME,
    META_KEY_LAST_UPDATED,
    META_KEY_LAST_FETCH_AT,
    META_KEY_LAST_FETCH_STATUS,
)
from db.connection import get_conn, transaction


# ── Schema ──────────────────────────────────────────────────────────────


def create_tables(conn: sqlite3.Connection | None = None) -> None:
    """Create all tables + views if they don't exist."""
    with transaction(conn) as c:
        schema = (conn or c).__class__
        # Read schema.sql from the project root
        from pathlib import Path
        sql_path = Path(__file__).resolve().parent.parent / "schema.sql"
        c.executescript(sql_path.read_text())
    print("[db] All tables + views exist.")


# ── Meta helpers ────────────────────────────────────────────────────────


def get_meta(key: str, conn: sqlite3.Connection | None = None) -> str | None:
    """Return a meta value, or None if key not present."""
    with transaction(conn) as c:
        row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str, conn: sqlite3.Connection | None = None) -> None:
    with transaction(conn) as c:
        c.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ── Cities & Stations ───────────────────────────────────────────────────


def upsert_city(city_name: str, province: str = "",
                conn: sqlite3.Connection | None = None) -> int:
    """Insert city if new, return city_id."""
    with transaction(conn) as c:
        c.execute(
            "INSERT INTO cities (city_name, province) VALUES (?, ?) "
            "ON CONFLICT(city_name) DO NOTHING",
            (city_name, province),
        )
        return c.execute(
            "SELECT city_id FROM cities WHERE city_name = ?", (city_name,)
        ).fetchone()["city_id"]


def upsert_station(station_name: str, city_id: int, station_url: str,
                   conn: sqlite3.Connection | None = None) -> int:
    """Insert station if new, return station_id."""
    with transaction(conn) as c:
        c.execute(
            "INSERT INTO stations (station_name, city_id, station_url) "
            "VALUES (?, ?, ?) ON CONFLICT(station_name) DO NOTHING",
            (station_name, city_id, station_url),
        )
        return c.execute(
            "SELECT station_id FROM stations WHERE station_name = ?",
            (station_name,),
        ).fetchone()["station_id"]


def get_all_station_names(conn: sqlite3.Connection | None = None) -> list[str]:
    with transaction(conn) as c:
        rows = c.execute("SELECT station_name FROM stations").fetchall()
        return [r["station_name"] for r in rows]


def update_station_geo(station_name: str, lat: float, lon: float,
                       conn: sqlite3.Connection | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with transaction(conn) as c:
        c.execute(
            "UPDATE stations SET lat = ?, lon = ?, geocoded_at = ? "
            "WHERE station_name = ?",
            (lat, lon, now, station_name),
        )


def update_station_direction(station_name: str, direction: str,
                             conn: sqlite3.Connection | None = None) -> None:
    with transaction(conn) as c:
        c.execute(
            "UPDATE stations SET direction = ? WHERE station_name = ?",
            (direction, station_name),
        )


# ── Trains & Stops (full overwrite) ─────────────────────────────────────


def overwrite_trains_and_stops(
    trains: list[dict[str, Any]],
    stops: list[dict[str, Any]],
    conn: sqlite3.Connection | None = None,
) -> None:
    """
    DELETE existing data, then INSERT all trains + stops in one transaction.
    """
    t0 = datetime.now(timezone.utc).isoformat()

    with transaction(conn) as c:
        # DELETE order matters because of FK
        c.execute("DELETE FROM stops")
        c.execute("DELETE FROM trains")

        # Bulk insert trains
        TRAIN_COLS = [
            "train_code", "train_class", "train_class_full",
            "origin_station_id", "origin_time",
            "dest_station_id", "dest_time",
            "total_duration_minutes", "stop_count",
            "detail_url", "relation_to_wuhu",
        ]
        c.executemany(
            f"INSERT INTO trains ({', '.join(TRAIN_COLS)}, fetched_at) "
            f"VALUES ({', '.join('?' for _ in TRAIN_COLS)}, ?)",
            [ [r[c] for c in TRAIN_COLS] + [t0] for r in trains ],
        )

        # Bulk insert stops
        STOP_COLS = [
            "train_code", "sequence", "station_id",
            "arrive_time", "arrive_day", "depart_time", "depart_day",
            "stop_duration", "running_minutes",
        ]
        c.executemany(
            f"INSERT INTO stops ({', '.join(STOP_COLS)}) "
            f"VALUES ({', '.join('?' for _ in STOP_COLS)})",
            [ [r[c] for c in STOP_COLS] for r in stops ],
        )

    print(f"[db] Overwritten: {len(trains)} trains, {len(stops)} stops")


# ── Queries (for test / future use) ─────────────────────────────────────


def query_reach(max_minutes: int,
                conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    """Reachable stations within N minutes from 芜湖 (uses v_station_reach)."""
    with transaction(conn) as c:
        return c.execute(
            "SELECT * FROM v_station_reach WHERE min_minutes <= ? "
            "ORDER BY min_minutes",
            (max_minutes,),
        ).fetchall()


def query_fastest_route(train_code: str,
                        target_station_id: int,
                        conn: sqlite3.Connection | None = None
                        ) -> list[sqlite3.Row]:
    """
    Return the stops on `train_code` from 芜湖 (exclusive) up to and including
    the station with `target_station_id`. Empty list if not on the route or
    target appears before 芜湖.
    """
    with transaction(conn) as c:
        # Anchor: find 芜湖's sequence on this train
        wuhu = c.execute("""
            SELECT s.sequence
            FROM stops s
            JOIN stations st ON s.station_id = st.station_id
            WHERE s.train_code = ? AND st.station_name = '芜湖'
        """, (train_code,)).fetchone()
        if not wuhu:
            return []
        wuhu_seq = wuhu["sequence"]

        return c.execute("""
            SELECT
                s.sequence,
                st.station_name,
                s.arrive_time,
                s.depart_time,
                s.running_minutes
            FROM stops s
            JOIN stations st ON s.station_id = st.station_id
            WHERE s.train_code = ?
              AND s.sequence  > ?
              AND s.sequence <= (SELECT sequence FROM stops
                                   WHERE train_code = ? AND station_id = ?)
            ORDER BY s.sequence
        """, (train_code, wuhu_seq, train_code, target_station_id)).fetchall()


def query_city_to_wuhu(
    city_name: str,
    conn: sqlite3.Connection | None = None,
) -> list[sqlite3.Row]:
    """All trains connecting 芜湖 to any station in the given city (either direction)."""
    with transaction(conn) as c:
        return c.execute("""
            SELECT
                t.train_code,
                t.train_class,
                ws.station_name  AS from_station,
                ws_stop.depart_time AS from_dep,
                cs.station_name  AS to_station,
                cs_stop.arrive_time  AS to_arr,
                (cs_stop.running_minutes - ws_stop.running_minutes) AS duration_min
            FROM stops ws_stop
            JOIN stops cs_stop
                ON ws_stop.train_code = cs_stop.train_code
            JOIN stations ws ON ws_stop.station_id = ws.station_id
            JOIN stations cs ON cs_stop.station_id = cs.station_id
            JOIN trains t ON ws_stop.train_code = t.train_code
            WHERE ws.station_name = '芜湖'
              AND cs.city_id = (SELECT city_id FROM cities WHERE city_name = ?)
              AND ws_stop.sequence < cs_stop.sequence
            ORDER BY ws_stop.depart_time
        """, (city_name,)).fetchall()


def db_size(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Quick counts for each table."""
    with transaction(conn) as c:
        tables = ["meta", "cities", "stations", "trains", "stops"]
        counts = {}
        for t in tables:
            r = c.execute(f"SELECT COUNT(*) as n FROM {t}").fetchone()
            counts[t] = r["n"]
        return counts


# ── Export: reach.json for the frontend ─────────────────────────────────


def export_reach_json(
    output_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """
    Build the static JSON consumed by the frontend.

    For each station in v_station_reach, attach:
      - lat, lon, direction (from stations)
      - fastest route (stops from 芜湖 to this station, with each stop's
        lat/lon/running_minutes)

    Skips stations without lat/lon. Skips routes whose stops are missing
    coords (the polyline would break in the frontend).

    Returns the dict (and also writes to output_path if given).
    """
    if output_path is None:
        # Default: write to frontend/data/reach.json (served by Cloudflare Pages)
        frontend_dir = DATA_DIR.parent / "frontend" / "data"
        frontend_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(frontend_dir / "reach.json")

    close = conn is None
    if close:
        conn = get_conn()
    try:
        # Hub coords
        hub = conn.execute(
            "SELECT lat, lon FROM stations WHERE station_name = ?",
            (HUB_STATION_NAME,),
        ).fetchone()
        if not hub or hub["lat"] is None:
            raise RuntimeError("Hub station 芜湖 has no lat/lon — geocode first.")

        # Max minutes for the slider (cap at 720 for UX)
        max_row = conn.execute(
            "SELECT COALESCE(MAX(min_minutes), 0) AS m FROM v_station_reach"
        ).fetchone()
        max_minutes = min(max_row["m"], 720) if max_row["m"] else 720

        # All stations with reach data + their fastest route
        rows = conn.execute("""
            SELECT
                v.station_id,
                v.station_name,
                v.city_id,
                v.city_name,
                v.lat,
                v.lon,
                v.direction,
                v.min_minutes,
                v.max_minutes,
                v.train_count,
                v.fastest_train_code
            FROM v_station_reach v
            WHERE v.lat IS NOT NULL AND v.lon IS NOT NULL
            ORDER BY v.min_minutes
        """).fetchall()

        stations_out = []
        skipped_no_route = 0
        for r in rows:
            stops = query_fastest_route(r["fastest_train_code"],
                                        r["station_id"], conn=conn)
            if not stops:
                skipped_no_route += 1
                continue

            # Build stop list with coords (skip if any stop lacks coords)
            # ALWAYS prepend the hub (芜湖) so the polyline starts there
            route_stops = [{
                "name": HUB_STATION_NAME,
                "lat": hub["lat"],
                "lon": hub["lon"],
                "run_min": 0,
            }]
            ok = True
            for s in stops:
                srow = conn.execute(
                    "SELECT lat, lon FROM stations WHERE station_name = ?",
                    (s["station_name"],),
                ).fetchone()
                if not srow or srow["lat"] is None:
                    ok = False
                    break
                route_stops.append({
                    "name": s["station_name"],
                    "lat": srow["lat"],
                    "lon": srow["lon"],
                    "run_min": s["running_minutes"],
                })
            if not ok:
                skipped_no_route += 1
                continue

            stations_out.append({
                "id": r["station_id"],
                "name": r["station_name"],
                "city": r["city_name"],
                "lat": r["lat"],
                "lon": r["lon"],
                "direction": r["direction"],
                "min_minutes": r["min_minutes"],
                "max_minutes": r["max_minutes"],
                "train_count": r["train_count"],
                "fastest_train_code": r["fastest_train_code"],
                "route": route_stops,
            })

        out = {
            "hub": {
                "name": HUB_STATION_NAME,
                "lat": hub["lat"],
                "lon": hub["lon"],
            },
            "max_minutes": max_minutes,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "station_count": len(stations_out),
            "stations": stations_out,
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

        print(f"[export] {len(stations_out)} stations written "
              f"({skipped_no_route} skipped for missing route/coords) → {output_path}")

        return out
    finally:
        if close:
            conn.close()