#!/usr/bin/env python3
"""
Post-fetch audit — detect data-quality issues introduced by upstream changes.

Runs after every cron_fetch. Five checks against `data/train.db`:

  1. duplicate_stops_across_trains  ≥N stops with same (station_id, arrive_time)
                                     shared between two trains → row-mixing
  2. geographic_jumps               consecutive stops with haversine > THRESHOLD
  3. time_delta_anomalies           consecutive running_minutes jump > THRESHOLD
                                     (forward or backward) — day / sequence errors
  4. non_monotonic_running_minutes  stops with running_minutes < prev → time reversal
  5. duplicate_stops_within_train   same station_id appears ≥2 times in one train

Output:
  - stdout: human-readable summary
  - state/post_fetch_audit.json: full report + regression diff vs previous run

Exit codes:
  - 0  no NEW regressions (clean OR only persistent findings)
  - 1  NEW findings detected (regression vs previous run's baseline)
  - 2  script crashed

Why this split?
  - Persistent findings (pre-existing data-quality issues we haven't
    cleaned up yet) shouldn't fire cron alerts every day. Only NEW
    regressions — findings that weren't in last run's state/post_fetch_audit.json
    — should signal "something upstream changed, investigate".

Cron usage (scripts/cron_fetch.sh):
  python3 scripts/audit_post_fetch.py >> "$LOG" 2>&1 || \
      log "WARN: audit_post_fetch non-zero exit (see log + state/post_fetch_audit.json)"
  # Don't abort the cron on audit findings — they're informational.

Refs: AGENTS.md §10 (post-fetch audit, 第二道防线).
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db.connection import get_conn


# ── Thresholds ──────────────────────────────────────────────────────────
THRESHOLD_GEO_JUMP_KM = 800       # > 800km between consecutive stops → suspicious
THRESHOLD_TIME_DELTA_MIN = 240     # > 4h delta (forward or backward) → suspicious
THRESHOLD_SHARED_STOPS = 5         # ≥ 5 identical (station, time) shared by 2 trains

# Output paths
STATE_DIR = ROOT / "state"
OUTPUT_JSON = STATE_DIR / "post_fetch_audit.json"


# ── Helpers ─────────────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km. NaN inputs → 0."""
    if None in (lat1, lon1, lat2, lon2):
        return 0.0
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(min(1.0, math.sqrt(a)))


def _station_name(conn: sqlite3.Connection, station_id: int) -> str:
    row = conn.execute(
        "SELECT station_name FROM stations WHERE station_id = ?", (station_id,)
    ).fetchone()
    return row["station_name"] if row else f"?id={station_id}"


# ── Check 1: duplicate stops across trains ──────────────────────────────

def check_duplicate_stops_across_trains(conn: sqlite3.Connection) -> list[dict]:
    """
    Two trains sharing ≥ THRESHOLD_SHARED_STOPS stops with identical
    (station_id, arrive_time) AND shared_count ≥ THRESHOLD_OVERLAP_PCT% of
    both trains' total stops → row-mixing signal (G4359/G4362/G4363 pattern).

    Why overlap%? Trains like K551/K554 are paired opposite-direction runs
    of the same route — they legitimately share many stops. We only flag
    when the overlap is near-total (≥ 80%), which only happens with
    row-mixing bugs.
    """
    THRESHOLD_OVERLAP_PCT = 80
    sql = """
        WITH counts AS (
            SELECT train_code, COUNT(*) AS n FROM stops GROUP BY train_code
        ),
        shared AS (
            SELECT a.train_code AS t1, b.train_code AS t2, COUNT(*) AS shared
              FROM stops a
              JOIN stops b
                ON a.station_id = b.station_id
               AND a.arrive_time = b.arrive_time
               AND a.arrive_time != ''
               AND a.train_code < b.train_code
             GROUP BY a.train_code, b.train_code
            HAVING shared >= ?
        )
        SELECT s.t1, s.t2, s.shared,
               c1.n AS n1, c2.n AS n2,
               ROUND(100.0 * s.shared / MAX(c1.n, c2.n), 1) AS overlap_pct
          FROM shared s
          JOIN counts c1 ON s.t1 = c1.train_code
          JOIN counts c2 ON s.t2 = c2.train_code
         WHERE ROUND(100.0 * s.shared / MAX(c1.n, c2.n), 1) >= ?
         ORDER BY overlap_pct DESC, s.shared DESC
    """
    findings = []
    for r in conn.execute(sql, (THRESHOLD_SHARED_STOPS, THRESHOLD_OVERLAP_PCT)).fetchall():
        t1, t2 = sorted((r["t1"], r["t2"]))
        findings.append({
            "key": f"{t1}|{t2}",
            "train_a": t1,
            "train_b": t2,
            "shared_stops": r["shared"],
            "stops_a": r["n1"],
            "stops_b": r["n2"],
            "overlap_pct": r["overlap_pct"],
        })
    return findings


# ── Check 2: geographic jumps ──────────────────────────────────────────

def check_geographic_jumps(conn: sqlite3.Connection) -> list[dict]:
    """
    Consecutive stops with haversine > THRESHOLD_GEO_JUMP_KM.
    Pulls all stops + coords in one query, iterates per train in Python.
    """
    sql = """
        SELECT s.train_code, s.sequence, st.station_id, st.station_name,
               st.lat, st.lon
          FROM stops s
          JOIN stations st ON s.station_id = st.station_id
         ORDER BY s.train_code, s.sequence
    """
    findings = []
    prev = None
    prev_code = None
    for r in conn.execute(sql).fetchall():
        if r["train_code"] != prev_code:
            prev = None
            prev_code = r["train_code"]
        if prev is not None and r["lat"] is not None and prev["lat"] is not None:
            dist = haversine_km(prev["lat"], prev["lon"], r["lat"], r["lon"])
            if dist > THRESHOLD_GEO_JUMP_KM:
                findings.append({
                    "key": f"{r['train_code']}|{r['sequence']}",
                    "train_code": r["train_code"],
                    "sequence": r["sequence"],
                    "from_station": prev["station_name"],
                    "to_station": r["station_name"],
                    "dist_km": round(dist, 1),
                })
        prev = r
        prev_code = r["train_code"]
    return findings


# ── Check 3 & 4: time delta + non-monotonic ────────────────────────────

def check_time_anomalies(conn: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    """
    Two checks, single SQL pass:

    3. time_delta_anomalies — |running_minutes[k] - running_minutes[k-1]| > THRESHOLD
    4. non_monotonic_running_minutes — running_minutes[k] < running_minutes[k-1]

    Returns (time_delta_findings, non_monotonic_findings).
    """
    sql = """
        WITH ordered AS (
            SELECT train_code, sequence, running_minutes, station_id,
                   LAG(running_minutes) OVER w AS prev_rm,
                   LAG(station_id)    OVER w AS prev_station_id,
                   LAG(sequence)      OVER w AS prev_sequence
              FROM stops
            WINDOW w AS (PARTITION BY train_code ORDER BY sequence)
        )
        SELECT train_code, sequence, running_minutes, prev_rm,
               prev_station_id, station_id,
               (running_minutes - prev_rm) AS delta
          FROM ordered
         WHERE prev_rm IS NOT NULL
    """
    time_delta = []
    non_mono = []
    for r in conn.execute(sql).fetchall():
        delta = r["delta"]
        if delta < 0:
            non_mono.append({
                "key": f"{r['train_code']}|{r['sequence']}",
                "train_code": r["train_code"],
                "sequence": r["sequence"],
                "station": _station_name(conn, r["station_id"]),
                "prev_station": _station_name(conn, r["prev_station_id"]),
                "running_minutes": r["running_minutes"],
                "prev_running_minutes": r["prev_rm"],
                "delta_min": delta,
            })
        if abs(delta) > THRESHOLD_TIME_DELTA_MIN:
            time_delta.append({
                "key": f"{r['train_code']}|{r['sequence']}",
                "train_code": r["train_code"],
                "sequence": r["sequence"],
                "station": _station_name(conn, r["station_id"]),
                "prev_station": _station_name(conn, r["prev_station_id"]),
                "delta_min": delta,
            })
    return time_delta, non_mono


# ── Check 5: duplicate stops within one train ──────────────────────────

def check_duplicate_stops_within_train(conn: sqlite3.Connection) -> list[dict]:
    sql = """
        SELECT train_code, station_id, COUNT(*) AS occurrences
          FROM stops
         GROUP BY train_code, station_id
        HAVING occurrences > 1
         ORDER BY occurrences DESC, train_code
    """
    findings = []
    for r in conn.execute(sql).fetchall():
        findings.append({
            "key": f"{r['train_code']}|{r['station_id']}",
            "train_code": r["train_code"],
            "station": _station_name(conn, r["station_id"]),
            "occurrences": r["occurrences"],
        })
    return findings


# ── Regression diff vs previous run ────────────────────────────────────

def diff_findings(current: list[dict], previous: list[dict] | None) -> dict:
    """
    Tag findings as 'new' (in current but not previous) or 'persistent'
    (in both). If no previous, all findings are 'new' (first run).
    """
    if previous is None:
        return {
            "new": [f["key"] for f in current],
            "persistent": [],
            "resolved": [],
        }
    cur_keys = {f["key"] for f in current}
    prev_keys = {f["key"] for f in previous}
    return {
        "new": sorted(cur_keys - prev_keys),
        "persistent": sorted(cur_keys & prev_keys),
        "resolved": sorted(prev_keys - cur_keys),
    }


# ── Main ───────────────────────────────────────────────────────────────

CHECKS = [
    ("duplicate_stops_across_trains", check_duplicate_stops_across_trains),
    ("geographic_jumps",              check_geographic_jumps),
    ("time_delta_anomalies",          lambda c: check_time_anomalies(c)[0]),
    ("non_monotonic_running_minutes", lambda c: check_time_anomalies(c)[1]),
    ("duplicate_stops_within_train",  check_duplicate_stops_within_train),
]


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Load previous run for diff (best-effort; missing/corrupt → None)
    prev_payload = None
    if OUTPUT_JSON.exists():
        try:
            prev_payload = json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[audit] WARN: could not load previous {OUTPUT_JSON}: {e}",
                  file=sys.stderr)

    conn = get_conn()
    try:
        # DB size for the report header
        size = conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM trains) AS trains, "
            "(SELECT COUNT(*) FROM stops)  AS stops, "
            "(SELECT COUNT(*) FROM stations) AS stations, "
            "(SELECT COUNT(*) FROM cities)   AS cities"
        ).fetchone()

        # Run all checks
        report: dict = {}
        for name, fn in CHECKS:
            try:
                report[name] = fn(conn)
            except Exception as e:
                print(f"[audit] ERROR in check {name!r}: {e}", file=sys.stderr)
                return 2

        # Build payload
        checks_out: dict = {}
        total_findings = 0
        new_findings = 0
        for name, _ in CHECKS:
            current = report[name]
            # prev_payload["checks"][name] is a dict {findings, count, regression};
            # diff_findings expects just the findings list.
            prev_block = (prev_payload or {}).get("checks", {}).get(name, {})
            prev_findings = prev_block.get("findings", []) if isinstance(prev_block, dict) else []
            diff = diff_findings(current, prev_findings)
            checks_out[name] = {
                "findings": current,
                "count": len(current),
                "regression": diff,
            }
            total_findings += len(current)
            new_findings += len(diff["new"])

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "db_size": dict(size),
            "thresholds": {
                "geo_jump_km": THRESHOLD_GEO_JUMP_KM,
                "time_delta_min": THRESHOLD_TIME_DELTA_MIN,
                "shared_stops": THRESHOLD_SHARED_STOPS,
            },
            "checks": checks_out,
            "summary": {
                "total_findings": total_findings,
                "new_findings": new_findings,
                "persistent_findings": total_findings - new_findings,
            },
        }

        OUTPUT_JSON.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Human-readable summary
        print(f"[audit] {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
        print(f"[audit] DB: {size['trains']} trains, {size['stops']} stops, "
              f"{size['stations']} stations, {size['cities']} cities")
        print(f"[audit] Thresholds: geo>{THRESHOLD_GEO_JUMP_KM}km, "
              f"|Δt|>{THRESHOLD_TIME_DELTA_MIN}min, shared≥{THRESHOLD_SHARED_STOPS}stops")
        print(f"[audit] Total findings: {total_findings} "
              f"(new={new_findings}, persistent={total_findings - new_findings})")
        for name, _ in CHECKS:
            d = checks_out[name]
            new_n = len(d["regression"]["new"])
            tag = " ⚠ REGRESSION" if new_n else ""
            print(f"  - {name:<32} {d['count']:>4} findings{tag}")
            # Show top 3 findings (compact)
            for f in d["findings"][:3]:
                detail = " ".join(f"{k}={v}" for k, v in f.items() if k != "key")
                print(f"      • {detail}")
            if len(d["findings"]) > 3:
                print(f"      ... and {len(d['findings']) - 3} more (see JSON)")
        print(f"[audit] Wrote {OUTPUT_JSON.relative_to(ROOT)}")

        # Exit code: 1 only if NEW findings (regression vs previous run).
        # Persistent findings exit 0 — they're already in the JSON, cron
        # shouldn't alert on them daily. See header docstring.
        return 1 if new_findings > 0 else 0

    finally:
        conn.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[audit] FATAL: {e}", file=sys.stderr)
        raise