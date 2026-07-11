"""
fill_missing_provinces.py — 给 cities 表中 province 为空的 city 补省份 (2026-07-10)

**问题**

经过 fix_truncated_cities.py 后还剩 470 个 city 没有 province。
这些全是中文 city 名（地名），缺省份上下文。

**修法**

用 Nominatim 查 city_name + "China"，从 display_name 倒数第二个非"中国"部分提取省。
直辖市 (北京/上海/天津/重庆) 自己就是省份名。

对 470 个 city：
- 大部分 Nominatim 一次命中（success rate > 90%）
- 失败：标记为 orphan candidates 但不删除（保留 city 名）
- rate-limit: 1.5s/req（~12 分钟跑完）

用法:
    python3 scripts/fill_missing_provinces.py            # dry-run
    python3 scripts/fill_missing_provinces.py --apply    # 写入
    python3 scripts/fill_missing_provinces.py --only "万安县,东莞"   # 限流子集
"""
import argparse
import os
import sys
import time
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import GEOCODER_RATE_LIMIT  # noqa: E402
from scrapers.geocoder import _build_geocoder  # noqa: E402

DB = os.path.join(os.path.dirname(__file__), "..", "data", "train.db")


def extract_province(display_name: str) -> str | None:
    """从 Nominatim display_name 提取省。

    display_name 格式多变：
      '万安县, 吉安市, 江西省, 中国' → '江西省'
      '上海市, 中国'                → '上海市' (直辖市)
      '一面坡, 大直街, 尚志市, 哈尔滨市, 黑龙江省, 150600, 中国'
                                      → '黑龙江省'
    策略：
      1. 先从后面扫，每个 part 检查是否在已知省份列表中
      2. 跳过纯数字（邮编）和 '中国'
    """
    PROVINCES = {
        # 4 直辖市
        "北京市", "上海市", "天津市", "重庆市",
        # 23 省
        "河北省", "山西省", "辽宁省", "吉林省", "黑龙江省",
        "江苏省", "浙江省", "安徽省", "福建省", "江西省",
        "山东省", "河南省", "湖北省", "湖南省", "广东省",
        "海南省", "四川省", "贵州省", "云南省", "陕西省",
        "甘肃省", "青海省", "台湾省",
        # 5 自治区
        "内蒙古自治区", "广西壮族自治区", "西藏自治区",
        "宁夏回族自治区", "新疆维吾尔自治区",
        # 2 特别行政区
        "香港特别行政区", "澳门特别行政区",
    }
    parts = display_name.split(", ")
    # 从后往前扫，第一个匹配 PROVINCES 的就是
    for p in reversed(parts):
        p = p.strip()
        if not p or p == "中国" or p.isdigit():
            continue
        # 少数民族地区：display_name 部分 包含 蒙/维/藏文
        # e.g. '内蒙古自治区 ᠦᠪᠦᠷ ᠮᠣᠩᠭᠤᠯ ...' → 取前中文部分 '内蒙古自治区'
        # e.g. '新疆维吾尔自治区 شىنجاڭ ئۇيغۇر ئاپتونوم رايونی' → 前部分
        # 先尝试匹配
        if p in PROVINCES:
            return p
        # 取 part 的中文前缀
        import re
        cn_part = re.match(r'^([\u4e00-\u9fff]+)', p)
        if cn_part:
            cn_only = cn_part.group(1)
            if cn_only in PROVINCES:
                return cn_only
            short_map = {
                "内蒙古": "内蒙古自治区",
                "广西": "广西壮族自治区",
                "西藏": "西藏自治区",
                "宁夏": "宁夏回族自治区",
                "新疆": "新疆维吾尔自治区",
                "香港": "香港特别行政区",
                "澳门": "澳门特别行政区",
            }
            if cn_only in short_map:
                return short_map[cn_only]
    return None


def get_missing(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT city_id, city_name FROM cities
        WHERE province IS NULL OR province = ''
        ORDER BY city_name
    """).fetchall()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--only", type=str, default="",
                   help="逗号分隔的 city_name 列表")
    args = p.parse_args()

    only_set = {n.strip() for n in args.only.split(",") if n.strip()}

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    missing = get_missing(conn)
    if only_set:
        missing = [m for m in missing if m["city_name"] in only_set]

    print(f"[fill-prov] {'APPLY' if args.apply else 'DRY-RUN'} — "
          f"{len(missing)} cities to fill\n", flush=True)

    g = _build_geocoder()
    summary = {"filled": [], "failed": []}

    for i, row in enumerate(missing):
        name = row["city_name"]
        loc = g.geocode(f"{name} China", exactly_one=True, timeout=10)
        if loc:
            prov = extract_province(loc.address)
            if prov:
                summary["filled"].append((name, prov))
                print(f"  ✓ {name}: {prov}  ({i+1}/{len(missing)})",
                      flush=True)
            else:
                summary["failed"].append((name, f"parse failed: {loc.address}"))
                print(f"  ✗ {name}: parse failed ({loc.address[:60]})",
                      flush=True)
        else:
            summary["failed"].append((name, "not found"))
            print(f"  ✗ {name}: not found  ({i+1}/{len(missing)})", flush=True)

        if args.apply and loc:
            prov = extract_province(loc.address)
            if prov:
                conn.execute(
                    "UPDATE cities SET province=? WHERE city_id=?",
                    (prov, row["city_id"]))
                conn.commit()

        time.sleep(GEOCODER_RATE_LIMIT)

    print(f"\n[fill-prov] Summary:")
    print(f"  filled: {len(summary['filled'])}")
    print(f"  failed: {len(summary['failed'])}")
    if summary["failed"]:
        print(f"  failed list ({len(summary['failed'])}):")
        for n, why in summary["failed"][:30]:
            print(f"    {n}: {why}")
        if len(summary["failed"]) > 30:
            print(f"    ... ({len(summary['failed'])-30} more)")
    if not args.apply:
        print("\n  (dry-run; pass --apply to write)")


if __name__ == "__main__":
    main()