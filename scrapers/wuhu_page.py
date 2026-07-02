"""
Parse the main station page: 3 tables + update time.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

from config import STATION_PAGE_URL, RAW_HTML_DIR


# ── helpers ─────────────────────────────────────────────────────────────


def _extract_time_str(soup: BeautifulSoup) -> str | None:
    """
    Find "芜湖站数据更新时间：2026-06-26 02:04:49" on the page.
    """
    for p in soup.find_all("p"):
        text = p.get_text(strip=True)
        m = re.search(r"更新时间[：:]\s*([\d\-:\s]+)", text)
        if m:
            raw = m.group(1).strip()
            # Normalise whitespace in date
            return re.sub(r"\s+", " ", raw)
    return None


def _parse_time_mm(text: str) -> int:
    """'1小时33分钟' → 93,  '14分钟' → 14,  '5分' → 5"""
    t = text.strip()
    total = 0
    h = re.search(r"(\d+)小?[时時]", t)
    m = re.search(r"(\d+)分[钟鐘]?", t)
    if h:
        total += int(h.group(1)) * 60
    if m:
        total += int(m.group(1))
    return total if total > 0 else 0


def _parse_page_meta(soup: BeautifulSoup) -> tuple[str, str, str]:
    """
    Return (site_name, station_name, data_update_time).
    site_name like "芜湖站", station_name like "芜湖".
    """
    title_tag = soup.find("title")
    site_name = "芜湖站"
    if title_tag and "欢迎访问" in title_tag.text:
        m = re.search(r"欢迎访问(.+?)页面", title_tag.text)
        if m:
            site_name = m.group(1).strip()

    station_name = site_name.replace("站", "").strip()
    data_update_time = _extract_time_str(soup) or "unknown"

    return site_name, station_name, data_update_time


def _infer_city_province_from_url(url: str) -> tuple[str, str]:
    """
    /anhui/wuhu/wuhu.html → ('芜湖', '安徽省')
    /zhejiang/hangzhou/hangzhouxi.html → ('杭州', '浙江省')
    """
    m = re.search(r"/(\w+)/(\w+)/\w+\.html", url)
    if not m:
        return (url.strip("/").split("/")[-1].replace(".html", ""), "")
    province_raw = m.group(1)  # 'anhui'
    city_raw = m.group(2)      # 'wuhu'

    PROVINCES = {
        "anhui": "安徽省", "beijing": "北京市",
        "chongqing": "重庆市", "fujian": "福建省",
        "gansu": "甘肃省", "guangdong": "广东省",
        "guangxi": "广西壮族自治区", "guizhou": "贵州省",
        "hainan": "海南省", "hebei": "河北省",
        "heilongjiang": "黑龙江省", "henan": "河南省",
        "hubei": "湖北省", "hunan": "湖南省",
        "jiangsu": "江苏省", "jiangxi": "江西省",
        "jilin": "吉林省", "liaoning": "辽宁省",
        "neimenggu": "内蒙古自治区", "ningxia": "宁夏回族自治区",
        "qinghai": "青海省", "shandong": "山东省",
        "shanghai": "上海市", "shaanxi": "陕西省",
        "shanxi": "山西省", "sichuan": "四川省",
        "tianjin": "天津市", "xinjiang": "新疆维吾尔自治区",
        "xizang": "西藏自治区", "yunnan": "云南省",
        "zhejiang": "浙江省", "taiwan": "台湾省",
    }
    province = PROVINCES.get(province_raw, province_raw)

    # City name: first char uppercase → Chinese. Use a mapping.
    CITY_MAP = {
        "anqing": "安庆", "bangbu": "蚌埠", "baoji": "宝鸡",
        "beijing": "北京", "changsha": "长沙",
        "changzhou": "常州", "chengdu": "成都",
        "chongqing": "重庆", "chuzhou": "滁州",
        "dalian": "大连", "dandong": "丹东",
        "fuyang": "阜阳", "fuzhou": "福州",
        "guangzhou": "广州", "guiyang": "贵阳",
        "haerbin": "哈尔滨", "haikou": "海口",
        "hangzhou": "杭州", "hefei": "合肥",
        "huaian": "淮安", "huangshan": "黄山",
        "huhehaote": "呼和浩特", "huzhou": "湖州",
        "jinan": "济南", "jiaxing": "嘉兴",
        "jingdezhen": "景德镇", "jinhua": "金华",
        "kashi": "喀什", "kunming": "昆明",
        "lanzhou": "兰州", "lasa": "拉萨",
        "lianyungang": "连云港", "luan": "六安",
        "luoyang": "洛阳", "maanshan": "马鞍山",
        "nanchang": "南昌", "nanjing": "南京",
        "nanning": "南宁", "nantong": "南通",
        "ningbo": "宁波", "ningde": "宁德",
        "pingdingshan": "平顶山", "qingdao": "青岛",
        "qinhuangdao": "秦皇岛", "quzhou": "衢州",
        "rizhao": "日照", "sanmenxia": "三门峡",
        "shanghai": "上海", "shantou": "汕头",
        "shaoxing": "绍兴", "shenyang": "沈阳",
        "shenzhen": "深圳", "shijiazhuang": "石家庄",
        "suzhou_anhui": "宿州", "suzhou_jiangsu": "苏州",
        "taian": "泰安", "taiyuan": "太原",
        "taizhou": "台州", "tangshan": "唐山",
        "tianjin": "天津", "tongling": "铜陵",
        "weifang": "潍坊", "wenling": "温岭",
        "wenzhou": "温州", "wuhu": "芜湖",
        "wuxi": "无锡", "wuzhou": "梧州",
        "xiamen": "厦门", "xiangyang": "襄阳",
        "xian": "西安", "xining": "西宁",
        "xinyu": "新余", "xuzhou": "徐州",
        "yanan": "延安", "yancheng": "盐城",
        "yangzhou": "扬州", "yantai": "烟台",
        "yichang": "宜昌", "yinchuan": "银川",
        "yongzhou": "永州", "yueyang": "岳阳",
        "shanghainan": "上海", "shanghaixi": "上海", "shanghaihongqiao": "上海",
        "yunnan": "云南", "zaozhuang": "枣庄",
        "zhangjiajie": "张家界", "zhangzhou": "漳州",
        "zhanjiang": "湛江", "zhaotong": "昭通",
        "zhengzhou": "郑州", "zhenjiang": "镇江",
        "zhongshan": "中山", "zhuhai": "珠海",
        "zibo": "淄博", "zunyi": "遵义",
    }
    city_name = CITY_MAP.get(city_raw, city_raw)

    # Fallback: if raw name not found, strip directional suffix and retry
    if city_name == city_raw and not city_name:
        # Try with directional suffixes stripped
        base = re.sub(r'(dong|xi|nan|bei|hongqiao)$', '', city_raw)
        if base and base != city_raw:
            city_name = CITY_MAP.get(base, base)

    # Special: sub-stations are the same city as the main station
    for sub_name in ["wanzhinan", "wuhunan", "wuhubei"]:
        if city_name == sub_name:
            city_name = "芜湖"

    return city_name, province


# ── main parser ─────────────────────────────────────────────────────────


def _parse_train_rows(tbody: Tag, relation: str,
                      site_name: str, station_name: str,
                      url_to_station_id: dict[str, int],
                      station_names_encountered: list) -> list[dict[str, Any]]:
    """Parse rows in one tbody (origin / destination / passing)."""
    trains = []
    rows = tbody.find_all("tr")

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 8:
            continue

        seq_cell = cells[0].get_text(strip=True)
        if not seq_cell.isdigit():
            continue  # skip header rows

        # 车次 — may be a link
        link = cells[1].find("a")
        train_code = (link.get_text(strip=True) if link else cells[1].get_text(strip=True)).strip().upper()
        detail_url = link["href"] if link else f"/huoche/{train_code.lower()}.html"
        # Ensure detail_url has leading /
        if not detail_url.startswith("/"):
            detail_url = "/" + detail_url

        # 始发站 / 终到站 — may be links too
        origin_link = cells[2].find("a")
        origin_station = origin_link.get_text(strip=True) if origin_link else cells[2].get_text(strip=True)
        origin_url = origin_link["href"] if origin_link else None

        dest_link = cells[3].find("a")
        dest_station = dest_link.get_text(strip=True) if dest_link else cells[3].get_text(strip=True)
        dest_url = dest_link["href"] if dest_link else None

        train_class = cells[4].get_text(strip=True)
        origin_time = cells[5].get_text(strip=True)
        dest_time = cells[6].get_text(strip=True)
        duration_str = cells[7].get_text(strip=True)

        # Upsert origin/dest stations & cities into the dict
        for st_name, st_url in [(origin_station, origin_url), (dest_station, dest_url)]:
            if st_name not in url_to_station_id:
                city_nm, prov = _infer_city_province_from_url(st_url) if st_url else (st_name, "")
                if st_name not in station_names_encountered:
                    station_names_encountered.append(st_name)
                url_to_station_id[st_name] = {"station_name": st_name, "city": city_nm, "province": prov, "url": st_url}

        trains.append({
            "train_code": train_code,
            "train_class": train_class,
            "train_class_full": train_class,
            "origin_station_name": origin_station,
            "origin_time": origin_time,
            "dest_station_name": dest_station,
            "dest_time": dest_time,
            "total_duration_minutes": _parse_time_mm(duration_str),
            "stop_count": 0,
            "detail_url": detail_url,
            "relation_to_wuhu": relation,
        })

    return trains


def parse_wuhu_page(
    session: requests.Session,
    url_to_station_id: dict[str, dict] | None = None,
    cache_html: bool = True,
) -> dict:
    """
    Fetch and parse the main station page.

    Returns
    -------
    {
        "data_update_time": str,
        "site_name": str,
        "station_name": str,
        "trains": list[dict],   # with station names, not IDs yet
        "new_stations": dict,   # { station_name: {station_name, city, province, url} }
    }
    """
    if url_to_station_id is None:
        url_to_station_id = {}

    resp = session.get(STATION_PAGE_URL, timeout=30)
    resp.encoding = "utf-8"
    html = resp.text

    if cache_html:
        import datetime
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        (RAW_HTML_DIR / f"wuhu_{ts}.html").write_text(html, encoding="utf-8")

    soup = BeautifulSoup(html, "lxml")
    site_name, station_name, data_update_time = _parse_page_meta(soup)

    print(f"[parse] Site: {site_name}  |  Update: {data_update_time}")

    # Find all <table> elements. The DOM structure of the station page is:
    #   <div class="module mod-table">
    #     <div class="hd"><span class="title">芜湖站 始发列车30车次</span></div>
    #     <div class="bd"><div class="table-inner"><table>...</table></div></div>
    #   </div>
    # We walk up from each table to find the enclosing module, then get the header
    # from div.hd span.title.
    tables = soup.find_all("table")
    if not tables:
        tables = soup.select("table.train-table, table.list-table")
    print(f"[parse] Found {len(tables)} table(s) on page")

    all_trains = []
    station_names_encountered = list(url_to_station_id.keys())

    for table in tables:
        # Find header by walking up to enclosing module
        header = None
        module = table.find_parent(lambda tag: tag.name == "div" and tag.get("class") and "module" in tag["class"])
        if module:
            hd = module.find("div", class_="hd")
            if hd:
                title_span = hd.find("span", class_="title")
                if title_span:
                    header = title_span.get_text(strip=True)

        # Fallback: any nearby text containing keywords
        if not header:
            cur = table
            for _ in range(15):
                cur = cur.find_previous()
                if cur and cur.name in ("span", "div", "p", "h3", "h2") and cur.get_text(strip=True):
                    txt = cur.get_text(strip=True)
                    if any(kw in txt for kw in ["始发", "终点", "经停", "途经"]):
                        header = txt
                        break

        # Determine relation from header text
        relation = "passing"
        if header:
            hl = header.lower()
            if "始发" in hl:
                relation = "origin"
            elif "终点" in hl or "终到" in hl:
                relation = "destination"
            elif "经停" in hl or "途经" in hl or "经过" in hl:
                relation = "passing"

        print(f"[parse]   Header: {header or '(auto-detected)'} → {relation}")

        tbody = table.find("tbody")
        if not tbody:
            tbody = table

        parsed = _parse_train_rows(tbody, relation, site_name, station_name,
                                    url_to_station_id, station_names_encountered)
        all_trains.extend(parsed)
        print(f"[parse]   {len(parsed)} trains")

    # Collect new stations encountered during parsing.
    # url_to_station_id now has entries added by _parse_train_rows
    new_stations = {}
    for name in station_names_encountered:
        if name not in url_to_station_id:
            city_nm, prov = _infer_city_province_from_url(
                f"/unknown/{name}.html"
            )
            url_to_station_id[name] = {
                "station_name": name,
                "city": city_nm,
                "province": prov,
                "url": None,
            }
        entry = url_to_station_id[name]
        new_stations[name] = entry

    return {
        "data_update_time": data_update_time,
        "site_name": site_name,
        "station_name": station_name,
        "trains": all_trains,
        "new_stations": new_stations,
    }
