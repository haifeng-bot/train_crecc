#!/usr/bin/env python3
"""
One-off cleanup for the G4359 / G4362 / G4363 row-mixing bug.

What happened:
    crecc.com's /huoche/g4359.html page mixed rows from three different
    trains (G4362, G4359, G4363) into a single stops table. Our scraper
    didn't validate the row-level train code, so all 12 rows got stamped
    with train_code='G4359' in our DB. The same phantom 12-stop sequence
    was also duplicated under train_code='G4362' and train_code='G4363'.

This script:
    1. Pre-flight check: report what *will* be deleted (counts + spot check)
    2. With --apply: DELETE those three trains' rows from stops + trains
    3. Verify post-state

Notes:
    - Stations table is NOT touched. Some stations (e.g. 芜湖, 宣城, 都匀东)
      are referenced by other legitimate trains and stay.
    - Cities table is NOT touched. Same reason.
    - After this, run `python main.py export-reach` to refresh the frontend
      JSON so it stops advertising these phantom routes.
    - Source page g4359.html is still broken upstream; if we re-run fetch
      before crecc fixes the page, the row-level guard in
      scrapers/train_detail.py will skip the mismatched rows but still
      write a stub train with 2 nonsense stops. The clean fix is to wait
      for upstream and re-fetch only after spot-checking.

Incident ref: AGENTS.md §8 (2026-08-11)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db.connection import get_conn, transaction


# Trains affected by the 2026-08-09 fetch mixing bug. Hardcoded because
# we know exactly which trains got corrupted; broader audit-style cleanup
# belongs in scripts/fix_data_issues.py.
AFFECTED = ("G4359", "G4362", "G4363")


def preflight(conn):
    print("[preflight] Will delete the following rows:\n")
    for code in AFFECTED:
        n_stops = conn.execute(
            "SELECT COUNT(*) AS n FROM stops WHERE train_code = ?", (code,)
        ).fetchone()["n"]
        n_trains = conn.execute(
            "SELECT COUNT(*) AS n FROM trains WHERE train_code = ?", (code,)
        ).fetchone()["n"]
        print(f"  {code}: {n_trains} trains row(s), {n_stops} stops row(s)")

    # Cross-check: any other train with identical stops?
    print("\n[preflight] Sanity check — any OTHER train with overlapping stops?")
    sample = conn.execute(
        "SELECT station_id, COUNT(*) AS hits "
        "FROM stops WHERE train_code IN (?, ?, ?) "
        "GROUP BY station_id HAVING hits > 1",
        AFFECTED,
    ).fetchall()
    if sample:
        print(f"  ⚠ {len(sample)} station_id(s) referenced multiple times "
              f"across the 3 affected trains — confirms row-mixing bug.")
    else:
        print("  (no duplicates)")

    # Stations and cities remain (read-only check)
    n_stations = conn.execute(
        "SELECT COUNT(*) AS n FROM stations WHERE station_name IN ("
        "SELECT DISTINCT st.station_name FROM stops s "
        "JOIN stations st ON s.station_id = st.station_id "
        "WHERE s.train_code IN (?, ?, ?))",
        AFFECTED,
    ).fetchone()["n"]
    print(f"\n[preflight] {n_stations} stations referenced by these trains "
          f"(preserved — may be shared with other trains).")


def apply_cleanup(conn):
    print("\n[apply] DELETING...")
    with transaction(conn) as c:
        n_stops = c.execute(
            "DELETE FROM stops WHERE train_code IN (?, ?, ?)", AFFECTED
        ).rowcount
        n_trains = c.execute(
            "DELETE FROM trains WHERE train_code IN (?, ?, ?)", AFFECTED
        ).rowcount
    print(f"  ✓ stops:     {n_stops} rows deleted")
    print(f"  ✓ trains:    {n_trains} rows deleted")


def verify(conn):
    print("\n[verify] Post-state:")
    for code in AFFECTED:
        n_stops = conn.execute(
            "SELECT COUNT(*) AS n FROM stops WHERE train_code = ?", (code,)
        ).fetchone()["n"]
        n_trains = conn.execute(
            "SELECT COUNT(*) AS n FROM trains WHERE train_code = ?", (code,)
        ).fetchone()["n"]
        status = "✓ clean" if n_stops == 0 and n_trains == 0 else "✗ STILL THERE"
        print(f"  {code}: {n_trains} trains, {n_stops} stops  [{status}]")

    totals = conn.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM trains) AS trains, "
        "(SELECT COUNT(*) FROM stops) AS stops, "
        "(SELECT COUNT(*) FROM stations) AS stations, "
        "(SELECT COUNT(*) FROM cities) AS cities"
    ).fetchone()
    print(f"\n  DB totals: {totals['trains']} trains, {totals['stops']} stops, "
          f"{totals['stations']} stations, {totals['cities']} cities")


def main():
    apply = "--apply" in sys.argv
    print("=" * 70)
    print(f"  G4359/G4362/G4363 row-mixing cleanup  "
          f"{'[DRY-RUN]' if not apply else '[APPLY]'}")
    print("=" * 70)

    conn = get_conn()
    try:
        preflight(conn)
        if apply:
            apply_cleanup(conn)
            verify(conn)
        else:
            print("\n[DRY-RUN] No changes made. Re-run with --apply to commit.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()