#!/usr/bin/env python3
"""
Backfill stops + trains tables for the 2026-10-19 mid-route-code-change
parser fix.

Context (see AGENTS.md §8):
    The previous per-row guard (added 2026-08-11 for G4359 row-mixing)
    wrongly filtered out stops whose row sub-code differed from page-code,
    catching legitimate "train code change mid-route" cases like
    G7225 ↔ G7228. Result: 275 trains lost 芜湖 from their stop lists,
    blocking 221 stations from being reachable.

    The new diversity-based parser (see scrapers/train_detail.py) accepts
    these pages, distinguishing them from real row-mixing by counting
    distinct non-page-codes.

This script:
    1. Iterates every data/raw/<code>_snapshot.html
    2. Re-parses it with the new _parse_stop_table
    3. For each train whose parse differs from DB, REPLACES its stops +
       updates its trains row (stop_count / origin / dest / duration)
    4. Upserts any new station names encountered (without geocoding —
       that's a separate step)
    5. Does NOT touch meta table (last_updated stays as 2026-09-11)

Idempotent: re-running is a no-op for trains whose parse already matches
their DB state.

After running, do:
    python3 main.py geocode            # fill coords for new stations
    python3 main.py directions         # compute directions for them
    python3 main.py export-reach       # regenerate reach.json
    scripts/audit_post_fetch.py        # verify no regressions

Usage:
    python3 scripts/backfill_midroute_codechange.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db.connection import get_conn, transaction
from db.repository import upsert_station, upsert_city
from scrapers.train_detail import _parse_stop_table


DIRECTIONAL_SUFFIX = re.compile(r'(东|西|南|北)$')


def _infer_city(station_name: str) -> tuple[str, str]:
    """Best-effort city inference from station name (mirrors main.py helper)."""
    base = DIRECTIONAL_SUFFIX.sub('', station_name).replace('站', '')
    city = base if base else station_name
    return city, ""


def _parse_one_snapshot(html: str, train_code: str) -> list[dict]:
    """Find stop table, parse it, return list of stop dicts."""
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    stop_table = None
    for t in tables:
        text = t.get_text(" ", strip=True)
        if text.startswith("站次") or "站次" in text[:20]:
            stop_table = t
            break
    if not stop_table and tables:
        stop_table = tables[-1]
    if not stop_table:
        return []
    return _parse_stop_table(stop_table, train_code)


def _get_or_create_station_id(conn, station_name: str,
                               station_url: str) -> tuple[int, bool]:
    """Return (station_id, created_now). Upserts if missing."""
    row = conn.execute(
        "SELECT station_id FROM stations WHERE station_name = ?",
        (station_name,),
    ).fetchone()
    if row:
        return row["station_id"], False
    city, province = _infer_city(station_name)
    cid = upsert_city(city, province, conn=conn)
    sid = upsert_station(station_name, cid, station_url or "", conn=conn)
    return sid, True


def _train_stops_match(conn, train_code: str, new_stops: list[dict]) -> bool:
    """True if existing stops in DB match new_stops (sequence, station_name,
    running_minutes). Used as the idempotency check."""
    rows = conn.execute(
        "SELECT s.sequence, st.station_name, s.running_minutes "
        "FROM stops s JOIN stations st ON st.station_id = s.station_id "
        "WHERE s.train_code = ? ORDER BY s.sequence",
        (train_code,),
    ).fetchall()
    if len(rows) != len(new_stops):
        return False
    for db_row, new_stop in zip(rows, new_stops):
        if (db_row["sequence"] != new_stop["sequence"]
                or db_row["station_name"] != new_stop["station_name"]
                or db_row["running_minutes"] != new_stop["running_minutes"]):
            return False
    return True


def _total_duration(stops: list[dict]) -> int:
    """End-to-end duration in minutes (last running_minutes, since
    stops[0] is the origin with running_minutes=0)."""
    if not stops:
        return 0
    return int(stops[-1]["running_minutes"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would change; don't write to DB")
    args = ap.parse_args()

    raw_dir = ROOT / "data" / "raw"
    snapshots = sorted(raw_dir.glob("*_snapshot.html"))
    print(f"[backfill] Found {len(snapshots)} raw snapshots in {raw_dir}")
    if args.dry_run:
        print("[backfill] DRY-RUN — no DB writes\n")

    conn = get_conn()
    try:
        # Snapshot existing station lat/lon presence to detect newly-added
        # (un-geocoded) stations after the run.
        before_ungeocoded = {
            r["station_name"]
            for r in conn.execute(
                "SELECT station_name FROM stations "
                "WHERE lat IS NULL OR lon IS NULL"
            ).fetchall()
        }
        before_stations = {
            r["station_name"]
            for r in conn.execute("SELECT station_name FROM stations").fetchall()
        }

        updated = 0
        unchanged = 0
        rejected = 0
        cleaned_up = 0
        not_in_trains = 0
        new_station_names: set[str] = set()
        diff_examples: list[str] = []

        for path in snapshots:
            code = path.stem.replace("_snapshot", "").upper()
            try:
                html = path.read_text(encoding="utf-8")
            except Exception as e:
                print(f"  ! {code}: read failed: {e}")
                continue

            # Look up train's existing DB state ONCE per iteration
            # (avoids leaking cur_count from previous iterations when
            # the parser rejects and we need to check stop_count for cleanup).
            cur_count = conn.execute(
                "SELECT stop_count FROM trains WHERE train_code = ?", (code,)
            ).fetchone()
            if not cur_count:
                # Train not in our DB (added upstream but skip-if-unchanged
                # hasn't re-fetched). Skip — let the next fetch handle it.
                not_in_trains += 1
                continue

            new_stops = _parse_one_snapshot(html, code)

            if not new_stops:
                # Parser rejected (row-mixing). CLEAN UP any stale stops
                # from a previous backfill run that used a looser heuristic.
                # The new heuristic catches more row-mixing pages (e.g.,
                # G4741/G4744/D4836/Y408 etc.) — their old stops came from
                # an earlier, looser parser and need to be removed.
                if cur_count["stop_count"] > 0:
                    with transaction(conn) as c:
                        c.execute("DELETE FROM stops WHERE train_code = ?", (code,))
                        c.execute(
                            "UPDATE trains SET stop_count = 0 WHERE train_code = ?",
                            (code,),
                        )
                    cleaned_up += 1
                    rejected += 1
                else:
                    rejected += 1
                continue

            if _train_stops_match(conn, code, new_stops):
                unchanged += 1
                continue

            # --- Need to change this train's stops ---
            old_count = cur_count["stop_count"]
            old_wuhu = conn.execute(
                "SELECT COUNT(*) AS c FROM stops s "
                "JOIN stations st ON st.station_id = s.station_id "
                "WHERE s.train_code = ? AND st.station_name = '芜湖'",
                (code,),
            ).fetchone()["c"]
            new_wuhu = sum(1 for s in new_stops if s["station_name"] == "芜湖")

            if len(diff_examples) < 10:
                diff_examples.append(
                    f"{code}: stops {old_count}→{len(new_stops)}, "
                    f"芜湖-row {old_wuhu}→{new_wuhu}"
                )

            if args.dry_run:
                updated += 1
                continue

            # Upsert stations (track newly-created ones)
            for st in new_stops:
                _, created = _get_or_create_station_id(
                    conn, st["station_name"], st.get("station_url", ""),
                )
                if created:
                    new_station_names.add(st["station_name"])

            # Atomically replace stops + update trains row
            with transaction(conn) as c:
                c.execute("DELETE FROM stops WHERE train_code = ?", (code,))

                for st in new_stops:
                    sid = c.execute(
                        "SELECT station_id FROM stations "
                        "WHERE station_name = ?",
                        (st["station_name"],),
                    ).fetchone()["station_id"]
                    c.execute(
                        "INSERT INTO stops (train_code, sequence, station_id, "
                        "arrive_time, arrive_day, depart_time, depart_day, "
                        "stop_duration, running_minutes) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (code, st["sequence"], sid,
                         st["arrive_time"], st["arrive_day"],
                         st["depart_time"], st["depart_day"],
                         st["stop_duration"], st["running_minutes"]),
                    )

                # Update trains row: stop_count + origin/dest/duration
                o_sid = c.execute(
                    "SELECT station_id FROM stations WHERE station_name = ?",
                    (new_stops[0]["station_name"],),
                ).fetchone()["station_id"]
                d_sid = c.execute(
                    "SELECT station_id FROM stations WHERE station_name = ?",
                    (new_stops[-1]["station_name"],),
                ).fetchone()["station_id"]
                total_dur = _total_duration(new_stops)
                c.execute(
                    "UPDATE trains SET "
                    "  stop_count = ?, "
                    "  origin_station_id = ?, "
                    "  origin_time = COALESCE(NULLIF(?, ''), origin_time), "
                    "  dest_station_id = ?, "
                    "  dest_time = COALESCE(NULLIF(?, ''), dest_time), "
                    "  total_duration_minutes = ? "
                    "WHERE train_code = ?",
                    (len(new_stops), o_sid,
                     new_stops[0].get("depart_time", ""),
                     d_sid, new_stops[-1].get("arrive_time", ""),
                     total_dur, code),
                )

            updated += 1

        print(f"\n[backfill] Updated:           {updated}")
        print(f"[backfill] Unchanged (no-op): {unchanged}")
        print(f"[backfill] Parser-rejected:   {rejected}  (row-mixing, safe-fail stub)")
        if cleaned_up:
            print(f"[backfill]   └─ cleaned up     {cleaned_up}  (stale stops from earlier run)")
        print(f"[backfill] Not in trains:     {not_in_trains}  (skip-if-unchanged pending)")
        if diff_examples:
            print(f"\n[backfill] Sample diffs (first {len(diff_examples)}):")
            for ex in diff_examples:
                print(f"  • {ex}")

        if not args.dry_run and updated:
            after_ungeocoded = {
                r["station_name"]
                for r in conn.execute(
                    "SELECT station_name FROM stations "
                    "WHERE lat IS NULL OR lon IS NULL"
                ).fetchall()
            }
            newly_ungeocoded = after_ungeocoded - before_ungeocoded
            new_stations = (after_ungeocoded - before_ungeocoded
                            - new_station_names) | new_station_names
            if newly_ungeocoded:
                print(f"\n[backfill] Newly un-geocoded stations: {len(newly_ungeocoded)}")
                print(f"  → run: python3 main.py geocode")
                for n in sorted(newly_ungeocoded)[:10]:
                    print(f"    - {n}")
                if len(newly_ungeocoded) > 10:
                    print(f"    ... and {len(newly_ungeocoded) - 10} more")

        if args.dry_run:
            print("\n[backfill] DRY-RUN: no DB writes performed")
        else:
            print(f"\n[backfill] NEXT STEPS:")
            print("  1. python3 main.py geocode       # fill coords for new stations")
            print("  2. python3 main.py directions    # compute directions for them")
            print("  3. python3 main.py export-reach  # regenerate reach.json")
            print("  4. scripts/audit_post_fetch.py   # verify no regressions")

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())