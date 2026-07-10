"""
fix_truncated_cities.py — 修复 cities 表中截断 / 拼错的 city_name (2026-07-10)

**问题**

scraper 解析 crecc.com 站名时把一些 city 名截断成单字（'淮'→'淮南'的'淮'），
或错把站名当 city（'无锡新区'/'苏州园区'），或拼音残留（'nanyang'→'南阳'）。
有些是数据 bug：同一个 city_id 被两个完全不同省的 station 引用（如 city 577 '定'
同时被甘肃'定西'和江西'定南'引用）。

**修法**

A. 单字 city：
   - 有引用的：merge 到正确的中文 city（如 73 '淮' → city 501 '淮南'）
     或 rename（如 576 '陇' → '陇西'）
   - orphan（无 station 引用）：DELETE

B. 577 '定' 拆分：
   - station 577 '定西' → city 440 '定西'
   - station 802 '定南' → city 458 '定南'
   - city 577 '定' DELETE

C. 区/园区/新区 → merge 到主 city

每一项都有 sanity check（修完 stations.city_id 一致、UNIQUE 不冲突）。

用法:
    python3 scripts/fix_truncated_cities.py            # dry-run (全跑)
    python3 scripts/fix_truncated_cities.py --apply    # 写入
    python3 scripts/fix_truncated_cities.py --batch A  # 只跑 A
    python3 scripts/fix_truncated_cities.py --batch B  # 只跑 B
    python3 scripts/fix_truncated_cities.py --batch C  # 只跑 C
    python3 scripts/fix_truncated_cities.py --batch D  # 只跑 D (拼音 → 中文)
"""
import argparse
import os
import sqlite3
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(__file__), "..", "data", "train.db")


# ── Operations ────────────────────────────────────────────────────────────
#
# Each op is (description, callable(conn) -> str) that returns a report line.

def op_merge(src_id: int, dst_id: int) -> tuple[str, callable]:
    def _do(conn):
        # 1) 找出 src 引用了哪些 stations
        src_stations = conn.execute(
            "SELECT station_id, station_name FROM stations WHERE city_id=?",
            (src_id,)).fetchall()
        # 2) 找出 dst 已经引用了哪些 stations (避免 UNIQUE(station_name) 冲突)
        dst_stations = conn.execute(
            "SELECT station_id, station_name FROM stations WHERE city_id=?",
            (dst_id,)).fetchall()
        dst_names = {s["station_name"] for s in dst_stations}

        conflicts = []
        for s in src_stations:
            if s["station_name"] in dst_names:
                conflicts.append(s["station_name"])

        if conflicts:
            return f"ABORT — station_name 冲突: {conflicts}"

        # 3) 移动 stations + 删 src city
        n = conn.execute(
            "UPDATE stations SET city_id=? WHERE city_id=?",
            (dst_id, src_id)).rowcount
        conn.execute("DELETE FROM cities WHERE city_id=?", (src_id,))
        return f"moved {n} stations, deleted city {src_id}"
    return f"merge city {src_id} → {dst_id}", _do


def op_rename(old_id: int, new_name: str) -> tuple[str, callable]:
    def _do(conn):
        # 检查新名是否已被其他 city 占用
        clash = conn.execute(
            "SELECT city_id FROM cities WHERE city_name=? AND city_id != ?",
            (new_name, old_id)).fetchone()
        if clash:
            return f"ABORT — city_name {new_name!r} 已被 city_id={clash['city_id']} 占用"
        conn.execute("UPDATE cities SET city_name=? WHERE city_id=?",
                     (new_name, old_id))
        return f"renamed city {old_id} → {new_name!r}"
    return f"rename city {old_id} → {new_name!r}", _do


def op_delete_orphan_city(cid: int) -> tuple[str, callable]:
    def _do(conn):
        used = conn.execute(
            "SELECT COUNT(*) AS n FROM stations WHERE city_id=?", (cid,)).fetchone()["n"]
        if used:
            return f"ABORT — city {cid} 仍被 {used} stations 引用"
        conn.execute("DELETE FROM cities WHERE city_id=?", (cid,))
        return f"deleted city {cid}"
    return f"delete orphan city {cid}", _do


def op_split_577(conn_factory) -> tuple[str, callable]:
    """Special: city 577 '定' → split between 440 '定西' and 458 '定南'."""
    def _do(conn):
        # 577 必须存在且 name == '定'
        c577 = conn.execute("SELECT city_name FROM cities WHERE city_id=577").fetchone()
        if not c577 or c577["city_name"] != "定":
            return "ABORT — city 577 状态已变"
        # 440 '定西' 必须存在
        if not conn.execute("SELECT 1 FROM cities WHERE city_id=440 AND city_name='定西'").fetchone():
            return "ABORT — city 440 '定西' 不存在"
        # 458 '定南' 必须存在
        if not conn.execute("SELECT 1 FROM cities WHERE city_id=458 AND city_name='定南'").fetchone():
            return "ABORT — city 458 '定南' 不存在"

        # 移动 577 引用下的 station
        # 定西 → 440, 定南 → 458
        n1 = conn.execute("""
            UPDATE stations SET city_id=440
            WHERE city_id=577 AND station_name='定西'
        """).rowcount
        n2 = conn.execute("""
            UPDATE stations SET city_id=458
            WHERE city_id=577 AND station_name='定南'
        """).rowcount
        # 删 577
        leftover = conn.execute(
            "SELECT COUNT(*) AS n FROM stations WHERE city_id=577").fetchone()["n"]
        if leftover:
            return f"ABORT — 577 仍有 {leftover} stations 未归类"
        conn.execute("DELETE FROM cities WHERE city_id=577")
        return f"定西→440 ({n1} stations), 定南→458 ({n2} stations), city 577 deleted"
    return "split city 577 '定' → 440/458", _do


# ── Operation lists per batch ─────────────────────────────────────────────

BATCH_A = [
    # 单字 city: 有引用的 8 个
    op_merge(73, 501),       # 淮 → 淮南
    op_merge(248, 47),       # 济 → 济南
    op_rename(331, "莱西"),  # 莱 → 莱西
    op_rename(576, "陇西"),  # 陇 → 陇西
    op_rename(614, "祁东"),  # 祁 → 祁东
    op_merge(706, 414),      # 渭 → 渭南
    op_rename(715, "商南"),  # 商 → 商南
    op_rename(718, "龙南"),  # 龙 → 龙南
    # orphan 单字: 3 个
    op_delete_orphan_city(97),   # 灌
    op_delete_orphan_city(323),  # 潼
    op_delete_orphan_city(528),  # 肥
]

BATCH_B = [
    # city 577 '定' 拆分
    op_split_577(None),
]

BATCH_C = [
    # 区/园区/新区 → 主 city
    op_merge(509, 17),       # 无锡新区 → 无锡
    op_merge(235, 1256),     # 苏州园区 → 苏州
    op_merge(478, 1256),     # 苏州新区 → 苏州
]

BATCH_D_RENAME = [
    # 简单 rename (中文 city 不存在)
    op_rename(775, "安康"),       # ankang
    op_rename(280, "池州"),       # chizhou
    op_rename(641, "大同"),       # datong
    op_rename(616, "桂林"),       # guilin
    op_rename(754, "佳木斯"),     # jiamusi
    op_rename(608, "九江"),       # jiujiang
    op_rename(57,  "丽水"),       # lishui
    op_rename(529, "牡丹江"),     # mudanjiang
    op_rename(5,   "宿迁"),       # suqian
    op_rename(240, "通辽"),       # tongliao
    op_rename(328, "威海"),       # weihai
    op_rename(824, "乌鲁木齐"),   # wulumuqi
    op_rename(8,   "宣城"),       # xuancheng
    op_rename(297, "武汉"),       # wuhan
    op_rename(12,  "上海南"),     # shanghainan
    op_rename(92,  "上海虹桥"),   # shanghaihongqiao
    op_rename(48,  "北京南"),     # beijingnan
    op_rename(594, "北京丰台"),   # beijingfengtai
    op_rename(298, "重庆北"),     # chongqingbei
]

BATCH_D_MERGE = [
    # merge (中文 city 存在，无冲突)
    op_merge(753, 470),    # anyang → 安阳
    op_merge(634, 635),    # baotou → 包头
    op_merge(89,  598),    # bozhou → 亳州
    op_merge(376, 717),    # ganzhou → 赣州
    op_merge(77,  510),    # huaibei → 淮北
    op_merge(63,  501),    # huainan → 淮南
    op_merge(266, 296),    # huanggang → 黄冈
    op_merge(499, 677),    # luohe → 漯河
    op_merge(397, 400),    # shangqiu → 商丘
    op_merge(107, 29),     # shangrao → 上饶
    op_merge(702, 469),    # xinxiang → 新乡
    op_merge(394, 254),    # xinyang → 信阳
    op_merge(314, 300),    # enshi → 恩施
    op_merge(375, 690),    # jian → 吉安
    op_merge(663, 34),     # pingxiang → 萍乡
    op_merge(10,  1256),   # suzhou → 苏州
    op_merge(54,  527),    # shanghai → 上海
]


def op_chongqingxi_split() -> tuple[str, callable]:
    """city 600 'chongqingxi' 拆分:
    - 322 '黄水' → 318 '石柱县' (黄水镇位于石柱县)
    - 323 '潼南' → 新建 city '潼南'
    - 600 '重庆西' → rename 为 '重庆西'
    """
    def _do(conn):
        # city 318 '石柱县' 必须存在
        if not conn.execute("SELECT 1 FROM cities WHERE city_id=318 AND city_name='石柱县'").fetchone():
            return "ABORT — city 318 '石柱县' 不存在"
        # city 600 必须是 chongqingxi
        c600 = conn.execute("SELECT city_name FROM cities WHERE city_id=600").fetchone()
        if not c600 or c600["city_name"] != "chongqingxi":
            return "ABORT — city 600 不是 chongqingxi"

        # 1) 把 322 '黄水' 移到 318 '石柱县'
        n_huangshui = conn.execute("""
            UPDATE stations SET city_id=318
            WHERE city_id=600 AND station_name='黄水'
        """).rowcount

        # 2) 给 323 '潼南' 新建 city '潼南'，需要先检查 '潼南' 是否已存在
        #    实际查过不存在
        tongnan_exists = conn.execute(
            "SELECT city_id FROM cities WHERE city_name='潼南'").fetchone()
        if tongnan_exists:
            # 如果已存在直接 merge
            conn.execute(
                "UPDATE stations SET city_id=? WHERE city_id=600 AND station_name='潼南'",
                (tongnan_exists["city_id"],))
            n_tongnan = 1
            tongnan_label = f"merged to existing city {tongnan_exists['city_id']}"
        else:
            cur = conn.execute(
                "INSERT INTO cities (city_name, province) VALUES ('潼南', '重庆市')")
            new_id = cur.lastrowid
            conn.execute(
                "UPDATE stations SET city_id=? WHERE city_id=600 AND station_name='潼南'",
                (new_id,))
            n_tongnan = 1
            tongnan_label = f"new city {new_id} '潼南'"

        # 3) 剩余的就是 重庆西 站
        leftover = conn.execute(
            "SELECT COUNT(*) AS n FROM stations WHERE city_id=600").fetchone()["n"]
        if leftover != 1:
            return f"ABORT — 600 拆分后剩 {leftover} stations，应剩 1 (重庆西)"
        # 4) rename 600 → 重庆西
        conn.execute("UPDATE cities SET city_name='重庆西' WHERE city_id=600")

        return (f"黄水→318 ({n_huangshui} station), "
                f"潼南→{tongnan_label} ({n_tongnan} station), "
                f"600 'chongqingxi' → '重庆西'")
    return "split city 600 'chongqingxi'", _do


BATCH_D_SPLIT = [
    op_chongqingxi_split(),
]


def get_batch(name: str) -> list:
    if name == "A":
        return BATCH_A
    if name == "B":
        return BATCH_B
    if name == "C":
        return BATCH_C
    if name == "D":
        # D 是拼音 city 修复 (rename + merge + chongqingxi 拆分)
        return BATCH_D_RENAME + BATCH_D_MERGE + BATCH_D_SPLIT
    raise ValueError(f"unknown batch {name}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="写入数据库")
    p.add_argument("--batch", default="ABC", help="跑哪几个 batch (默认 ABC)")
    args = p.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print(f"[fix-truncated] {'APPLY' if args.apply else 'DRY-RUN'}  "
          f"batches={args.batch}\n")

    for batch_name in args.batch:
        ops = get_batch(batch_name)
        print(f"── Batch {batch_name} ({len(ops)} ops) ──")
        for desc, fn in ops:
            print(f"  {desc} ... ", end="", flush=True)
            try:
                report = fn(conn)
                print(f"{'OK' if not report.startswith('ABORT') else '✗'} {report}")
                if args.apply and not report.startswith("ABORT"):
                    conn.commit()
            except Exception as e:
                print(f"✗ EXCEPTION: {e}")
                conn.rollback()
                raise

    print("\n[fix-truncated] done.")


if __name__ == "__main__":
    main()