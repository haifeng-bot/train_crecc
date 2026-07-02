"""
Repository layer — all DB reads/writes in one place.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from config import (
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
