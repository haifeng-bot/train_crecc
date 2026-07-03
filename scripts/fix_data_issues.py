"""
fix_data_issues.py — 修复 DB 中的数据问题 (2026-07-03 audit)

P0: 伪长停站 → 补 depart_day (+1) 使跨日站停靠时长正确
P1: D5487/D5490 南京南 seq=8 重复行 → 删除
P3: total_duration 偏差 → 根据始发终到时间+跨日重新计算

P2 (running_minutes 倒退) 是源站数据缺陷，不动。

用法:  python3 scripts/fix_data_issues.py
"""

import sqlite3
import sys
import os

DB = os.path.join(os.path.dirname(__file__), "..", "data", "train.db")


def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def fix_p0_pseudo_long_stops(conn, cur):
    """P0: 跨日站补 depart_day (+1), 重整 stop_duration"""
    rows = cur.execute("""
        SELECT rowid, train_code, sequence, arrive_time, depart_time,
               arrive_day, depart_day, stop_duration
        FROM stops
        WHERE stop_duration > 150 AND stop_duration < 1440
        ORDER BY train_code, sequence
    """).fetchall()

    fixed = 0
    for r in rows:
        arr_h, arr_m = map(int, r["arrive_time"].split(":"))
        dep_h, dep_m = map(int, r["depart_time"].split(":"))
        arr_total = arr_h * 60 + arr_m
        dep_total = dep_h * 60 + dep_m

        # Cross-midnight: arrive time > depart time but both in same day
        if arr_total > dep_total and r["arrive_day"] == r["depart_day"]:
            new_dep_day = r["depart_day"] + 1
            new_duration = (1440 - arr_total) + dep_total
            cur.execute("""
                UPDATE stops
                SET depart_day = ?, stop_duration = ?
                WHERE rowid = ?
            """, (new_dep_day, new_duration, r["rowid"]))
            print(f"  P0: {r['train_code']} seq={r['sequence']} "
                  f"{r['arrive_time']}->{r['depart_time']} "
                  f"day[{r['arrive_day']}->{r['depart_day']}→{new_dep_day}] "
                  f"dur {r['stop_duration']}→{new_duration}")
            fixed += 1

    if fixed == 0:
        print("  P0: 没有需要修复的伪长停站")
    else:
        print(f"  P0: 修复 {fixed} 处")
    return fixed


def fix_p1_duplicate_stops(conn, cur):
    """P1: D5487/D5490 南京南 seq=8 重复行删除"""
    # These are the specific duplicate rows: seq=8 for D5487/D5490 at 南京南
    rows = cur.execute("""
        SELECT s.rowid, s.train_code, s.sequence, s.arrive_time, s.depart_time
        FROM stops s
        JOIN stations st ON s.station_id = st.station_id
        WHERE st.station_name = '南京南'
          AND s.train_code IN ('D5487', 'D5490')
          AND s.sequence = 8
          AND s.stop_duration = 0
          AND s.running_minutes = 163
    """).fetchall()

    removed = 0
    for r in rows:
        cur.execute("DELETE FROM stops WHERE rowid = ?", (r["rowid"],))
        print(f"  P1: 删除 {r['train_code']} seq={r['sequence']} 南京南重复行")
        removed += 1

    if removed == 0:
        print("  P1: 没有找到需要删除的重复行")
    else:
        print(f"  P1: 删除 {removed} 行")
    return removed


def fix_p3_total_duration(conn, cur):
    """P3: 根据始发终到时间+跨日重新计算 total_duration_minutes"""
    def parse_time(ts):
        h, m = ts.strip().split(":")
        return int(h) * 60 + int(m)

    rows = cur.execute("""
        SELECT rowid, train_code, total_duration_minutes, origin_time, dest_time
        FROM trains
        WHERE total_duration_minutes > 0
          AND origin_time != '' AND dest_time != ''
        ORDER BY train_code
    """).fetchall()

    fixed = 0
    for r in rows:
        ot_min = parse_time(r["origin_time"])
        dt_min = parse_time(r["dest_time"])

        # Compute raw diff
        if dt_min >= ot_min:
            computed = dt_min - ot_min
        else:
            computed = 1440 - ot_min + dt_min

        # Check whether the route spans multiple days — use the stop table
        # If the last stop has arrive_day > 0, add 1440 per day
        last_stop = cur.execute("""
            SELECT arrive_day, depart_day
            FROM stops
            WHERE train_code = ?
            ORDER BY sequence DESC
            LIMIT 1
        """, (r["train_code"],)).fetchone()

        if last_stop:
            max_day = max(last_stop["arrive_day"] or 0, last_stop["depart_day"] or 0)
            # If the first stop is also day>0 (departure on day 1+), shift back
            first_stop = cur.execute("""
                SELECT arrive_day, depart_day
                FROM stops
                WHERE train_code = ?
                ORDER BY sequence ASC
                LIMIT 1
            """, (r["train_code"],)).fetchone()
            min_day = 0
            if first_stop:
                min_day = min(first_stop["arrive_day"] or 0, first_stop["depart_day"] or 0)
            if min_day > 0:
                # Normalize: if first stop already day 1, subtract so day 0 = departure
                max_day -= min_day

            computed += max_day * 1440

            # Stored includes crossing days; if computed + one more day is closer, try that too
            if abs(r["total_duration_minutes"] - (computed + 1440)) < abs(r["total_duration_minutes"] - computed):
                computed += 1440

        diff = abs(r["total_duration_minutes"] - computed)
        if diff > 5:
            cur.execute("""
                UPDATE trains
                SET total_duration_minutes = ?
                WHERE rowid = ?
            """, (computed, r["rowid"]))
            print(f"  P3: {r['train_code']}: {r['total_duration_minutes']}→{computed}min "
                  f"(time {r['origin_time']}→{r['dest_time']}, diff was {diff})")
            fixed += 1

    if fixed == 0:
        print("  P3: 没有需要修正的 total_duration")
    else:
        print(f"  P3: 修正 {fixed} 个车次")
    return fixed


def main():
    print(f"连接数据库: {DB}")
    conn = get_conn()
    cur = conn.cursor()

    print("\n=== P0: 修复伪长停站 ===")
    p0 = fix_p0_pseudo_long_stops(conn, cur)

    print("\n=== P1: 删除重复 stop ===")
    p1 = fix_p1_duplicate_stops(conn, cur)

    print("\n=== P3: 重新计算 total_duration ===")
    p3 = fix_p3_total_duration(conn, cur)

    conn.commit()

    print(f"\n{'='*40}")
    print(f"修复汇总:")
    print(f"  P0: {p0} 处伪长停站 depart_day 修正")
    print(f"  P1: {p1} 行重复 stop 删除")
    print(f"  P3: {p3} 个车次 total_duration 重算")
    print(f"{'='*40}")

    # Verify
    print("\n=== 修复后验证 ===")
    bad = cur.execute("""
        SELECT COUNT(*) FROM stops
        WHERE stop_duration > 150 AND stop_duration < 1440
    """).fetchone()[0]
    print(f"  残留伪长停站（150~1440min）: {bad} 处")

    conn.close()


if __name__ == "__main__":
    main()
