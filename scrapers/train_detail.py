"""
Parse a train detail page -> full stop sequence.

Crecc.com detail page layout (example: /huoche/c2392.html):

  Table 0: train info (4 rows, 5 cols) — origin, dest, times, duration
  Table 1: ticket prices (4 rows, 5 cols)
  Table 2: stop sequence (1 header + N data rows, 7 cols):

    Col 0 = 站次 (int, 1..N)
    Col 1 = 车站 (station name, may be a link)
    Col 2 = 车次 (train code)
    Col 3 = 到达时间 ("07:27 (当日)" or "01:23 (次日)" or "----")
    Col 4 = 发车时间 (same format)
    Col 5 = 运行时间 ("1小时33分钟" from origin)
    Col 6 = 停留时间 ("10分钟" or "----")
"""
from __future__ import annotations

import re
from typing import Any

import requests
from bs4 import BeautifulSoup

from config import RAW_HTML_DIR, TRAIN_DETAIL_URL_TEMPLATE
from scrapers.http import Sleeper


# ── Stop table parser ───────────────────────────────────────────────────


def _parse_stop_table(table: Any,
                      train_code: str) -> list[dict[str, Any]]:
    """
    Parse the stop-sequence table.
    Always 7 columns: 站次 | 车站 | 车次 | 到达时间 | 发车时间 | 运行时间 | 停留时间.
    """
    stops = []
    rows = table.find_all("tr")

    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 5:
            continue

        seq_text = cells[0].get_text(strip=True)
        if not seq_text.isdigit():
            continue

        seq = int(seq_text)

        # Station name — may be a link
        station_link = cells[1].find("a")
        if station_link:
            station_name = station_link.get_text(strip=True)
            station_url = station_link.get("href", "")
        else:
            station_name = cells[1].get_text(strip=True)
            station_url = ""

        # Column offsets: always col 3=到达, col 4=发车, col 5=运行, col 6=停留
        arrive_raw = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        depart_raw = cells[4].get_text(strip=True) if len(cells) > 4 else ""
        duration_str = cells[5].get_text(strip=True) if len(cells) > 5 else ""
        stop_str = cells[6].get_text(strip=True) if len(cells) > 6 else ""

        # Time + day embedded in same cell
        arrive_time, arrive_day = _parse_time_day(arrive_raw)
        depart_time, depart_day = _parse_time_day(depart_raw)

        stops.append({
            "train_code": train_code,
            "sequence": seq,
            "station_name": station_name,
            "station_url": station_url,
            "arrive_time": arrive_time,
            "arrive_day": arrive_day,
            "depart_time": depart_time,
            "depart_day": depart_day,
            "stop_duration": _parse_duration(stop_str),
            "running_minutes": _parse_duration(duration_str),
        })

    return stops


def _parse_time_day(raw: str) -> tuple[str, int]:
    """
    '07:27 (当日)' -> ('07:27', 0)
    '01:23 (次日)' -> ('01:23', 1)
    '----'        -> ('', 0)
    """
    raw = raw.strip()
    if not raw or raw == "----":
        return ("", 0)
    m = re.search(r"(\d{2}:\d{2})", raw)
    time_part = m.group(1) if m else ""
    day = 1 if "次" in raw else 0
    return (time_part, day)


# ── Duration helpers ────────────────────────────────────────────────────


def _parse_duration(text: str) -> int:
    """'1小时33分钟' -> 93,  '14分钟' -> 14,  '' -> 0"""
    t = text.strip()
    if not t or t == "----":
        return 0
    total = 0
    h = re.search(r"(\d+)小?[\u65f6\u6642]", t)
    m = re.search(r"(\d+)分[\u949f\u9418]?", t)
    if h:
        total += int(h.group(1)) * 60
    if m:
        total += int(m.group(1))
    return total


# ── Train info table parser ─────────────────────────────────────────────


def _parse_train_info_table(table: Any) -> dict[str, Any]:
    """
    Parse the small info table (Table 0) into a flat dict.

    Expected rows:
      列车信息 | 列车车次 | C2392 | 列车车型 | 城际动车 有空调
      始发站点 | 杭州西 | 始发时间 | 06:50
      到达站点 | 泗县东 | 到达时间 | 当日11:04
      全程时间 | 4小时14分钟
    """
    info = {}
    for row in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]

        for col_idx, cell in enumerate(cells):
            if cell == "列车车次" and col_idx + 1 < len(cells):
                info["train_code"] = cells[col_idx + 1]
            elif cell == "列车车型" and col_idx + 1 < len(cells):
                info["train_class"] = cells[col_idx + 1]
                info["train_class_full"] = cells[col_idx + 1]
            elif cell == "始发站点" and col_idx + 1 < len(cells):
                info["origin_station_name"] = cells[col_idx + 1]
            elif cell == "始发时间" and col_idx + 1 < len(cells):
                m = re.search(r"(\d{2}:\d{2})", cells[col_idx + 1])
                if m:
                    info["origin_time"] = m.group(1)
            elif cell == "到达站点" and col_idx + 1 < len(cells):
                info["dest_station_name"] = cells[col_idx + 1]
            elif cell == "到达时间" and col_idx + 1 < len(cells):
                m = re.search(r"(\d{2}:\d{2})", cells[col_idx + 1])
                if m:
                    info["dest_time"] = m.group(1)
            elif cell == "全程时间" and col_idx + 1 < len(cells):
                info["total_duration_minutes"] = _parse_duration(cells[col_idx + 1])

    return info


def extract_train_info(soup: BeautifulSoup) -> dict[str, Any]:
    """
    Extract train-level info by parsing the first non-stop table.
    """
    tables = soup.find_all("table")

    # Table 0 is the train info table
    if tables:
        info = _parse_train_info_table(tables[0])
        if info.get("train_code") or "始发" in tables[0].get_text():
            return info

    return {}


# ── Public entry ────────────────────────────────────────────────────────


def fetch_train_detail(
    session: requests.Session,
    train_code: str,
    detail_url: str,
    sleeper: Sleeper,
    cache_html: bool = True,
) -> dict[str, Any] | None:
    """
    Fetch and parse one train detail page.

    Returns { train_code, train_class, train_class_full, origin_station_name,
              dest_station_name, origin_time, dest_time,
              total_duration_minutes, stop_count, stops }
    """
    url = TRAIN_DETAIL_URL_TEMPLATE.format(code=train_code.lower())
    sleeper.wait()

    try:
        resp = session.get(url, timeout=30)
        resp.encoding = "utf-8"
        html = resp.text
    except Exception as e:
        print(f"  [fetch] X {train_code}: {e}")
        return None

    if cache_html:
        (RAW_HTML_DIR / f"{train_code.lower()}_snapshot.html").write_text(
            html, encoding="utf-8"
        )

    soup = BeautifulSoup(html, "lxml")

    # --- Parse train info ---
    train_info = extract_train_info(soup)
    train_info.setdefault("train_code", train_code)
    train_info.setdefault("train_class", "")
    train_info.setdefault("train_class_full", "")
    train_info.setdefault("origin_time", "")
    train_info.setdefault("dest_time", "")
    train_info.setdefault("total_duration_minutes", 0)

    # --- Find the stop table ---
    # It's usually the LAST table, but we search by header keywords
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
        print(f"  [parse] X {train_code}: no stop table found")
        return None

    stops = _parse_stop_table(stop_table, train_code)
    if not stops:
        print(f"  [parse] X {train_code}: no stops parsed")
        return None

    train_info["stops"] = stops
    train_info["stop_count"] = len(stops)
    return train_info
