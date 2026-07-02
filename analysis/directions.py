"""
Compute 8-way cardinal direction from 芜湖 to each station.

Direction buckets are based on bearing (0° = North, clockwise):

    N:  [348.75, 360) ∪ [0, 11.25)
    NE: [11.25, 78.75)
    E:  [78.75, 101.25)
    SE: [101.25, 168.75)
    S:  [168.75, 191.25)
    SW: [191.25, 258.75)
    W:  [258.75, 281.25)
    NW: [281.25, 348.75)
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from config import HUB_STATION_NAME
from db.repository import transaction


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Initial bearing from point 1 to point 2, in degrees [0, 360).
    """
    dlon = math.radians(lon2 - lon1)
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)

    x = math.sin(dlon) * math.cos(lat2_r)
    y = (math.cos(lat1_r) * math.sin(lat2_r)
         - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon))

    bearing_deg = math.degrees(math.atan2(x, y))
    return (bearing_deg + 360) % 360


def bearing_to_direction(b: float) -> str:
    """Map bearing (degrees) to 8-way direction string."""
    if 348.75 <= b < 360 or 0 <= b < 11.25:
        return "N"
    elif 11.25 <= b < 78.75:
        return "NE"
    elif 78.75 <= b < 101.25:
        return "E"
    elif 101.25 <= b < 168.75:
        return "SE"
    elif 168.75 <= b < 191.25:
        return "S"
    elif 191.25 <= b < 258.75:
        return "SW"
    elif 258.75 <= b < 281.25:
        return "W"
    elif 281.25 <= b < 348.75:
        return "NW"
    return "?"  # should never happen


def compute_all_directions(
    conn: sqlite3.Connection | None = None,
) -> dict[str, str]:
    """
    Compute 8-way direction for every station with lat/lon, relative to HUB.

    Returns { station_name: direction }
    """
    with transaction(conn) as c:
        # Get 芜湖's coordinates
        hub = c.execute(
            "SELECT lat, lon FROM stations WHERE station_name = ?",
            (HUB_STATION_NAME,),
        ).fetchone()
        if not hub or hub["lat"] is None:
            print("[directions] Error: 芜湖 has no lat/lon. Geocode first.")
            return {}

        hub_lat, hub_lon = hub["lat"], hub["lon"]

        # Get all stations with lat/lon
        rows = c.execute(
            "SELECT station_id, station_name, lat, lon FROM stations "
            "WHERE lat IS NOT NULL AND station_name != ?",
            (HUB_STATION_NAME,),
        ).fetchall()

    if not rows:
        print("[directions] No stations with coordinates to compute.")
        return {}

    results = {}
    for r in rows:
        b = bearing(hub_lat, hub_lon, r["lat"], r["lon"])
        direction = bearing_to_direction(b)
        results[r["station_name"]] = direction

    # Bulk update
    with transaction(conn) as c:
        for name, dir_val in results.items():
            c.execute(
                "UPDATE stations SET direction = ? WHERE station_name = ?",
                (dir_val, name),
            )

    # Count per direction
    dir_counts = {}
    for d in results.values():
        dir_counts[d] = dir_counts.get(d, 0) + 1

    print("[directions] Direction counts:")
    for d in sorted(dir_counts.keys()):
        print(f"  {d}: {dir_counts[d]}")

    print(f"[directions] Total: {len(results)} stations")
    return results
