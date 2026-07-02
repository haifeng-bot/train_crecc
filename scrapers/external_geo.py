"""
Import station coordinates from an external JSON dataset.

The OSM-derived file from epcm/TrainVis contains 3276 stations with
[lon, lat] coordinates. We load it, match against our pending stations
(stations with NULL lat), and bulk-update the DB.

This bypasses the per-station Nominatim rate limit by using a precompiled
dataset. Data is in WGS84, identical to what Nominatim returns.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from db.connection import get_conn
from db.repository import update_station_geo


def import_external_geo(
    json_path: Path | str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, int | list[str]]:
    """
    Read `{name: [lon, lat], ...}` JSON and update stations with NULL lat.

    Returns dict with matched, skipped_already_set, no_match_count, no_match_list.
    """
    p = Path(json_path)
    if not p.exists():
        raise FileNotFoundError(f"External geo file not found: {p}")

    close_conn = conn is None
    if close_conn:
        conn = get_conn()

    try:
        with p.open() as f:
            data: dict[str, list[float]] = json.load(f)
        print(f"[external] Loaded {len(data)} stations from {p.name}")

        rows = conn.execute(
            "SELECT station_id, station_name, lat FROM stations"
        ).fetchall()
        by_name = {r["station_name"]: r for r in rows}

        matched = 0
        skipped_already = 0
        no_match = []
        for name, coords in data.items():
            if name not in by_name:
                continue
            if by_name[name]["lat"] is not None:
                skipped_already += 1
                continue
            lon, lat = coords[0], coords[1]
            update_station_geo(name, lat, lon, conn=conn)
            matched += 1

        # Compute no-match list
        data_names = set(data.keys())
        for r in rows:
            if r["lat"] is None and r["station_name"] not in data_names:
                no_match.append(r["station_name"])

        print(f"[external] Matched: {matched}, "
              f"Skipped (already set): {skipped_already}, "
              f"No match: {len(no_match)}")

        return {
            "matched": matched,
            "skipped_already_set": skipped_already,
            "no_match_count": len(no_match),
            "no_match_list": no_match,
        }
    finally:
        if close_conn:
            conn.close()