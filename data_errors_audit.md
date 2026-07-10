# train_crecc 时刻表数据错误审计报告

> 审计时间：2026-07-03
> 数据库：`data/train.db`（last_updated: 2026-06-26 02:04:49）
> 数据量：737 车次、10846 停站记录、718 站点

---

## 🔴 严重错误（数据自相矛盾 / 显然不可能）

### 1. `stop_duration` 出现 1000+ 分钟的"伪超长停车" — **115 处**
典型场景：某站 arrive_time `23:58` → depart_time `00:02`，本应跨日停车 4 分钟，但因 `depart_day` 未正确置 1（仍为 0），计算出的 `stop_duration` = `(0×1440+2) - (0×1440+1438)` 的绝对值 = **1436 分钟**。

例：`K1217` 池州 23:58 → 00:02，`stop_duration=1436`（应为 4）；`K346` 宣城 23:57→00:01，`stop_duration=1436`；`K892` 芜湖 23:57→00:01，`stop_duration=1436`。

**根因**：scraper 把 `arrive_day`/`depart_day` 字段读入时默认填 0，而源数据对跨日情况没有给 day 标记（或 scraper 没正确解析），结果 `stop_duration` 被算成 ~24h。

### 2. stops 内部 `running_minutes` 倒退 — **30 处**
同一车次 sequence 递增但 running_minutes 反而变小（包括跳点、回到起点）。这是 30 处错乱中 70 个车次错乱幅度 ≥ 3 站之一（见 #4）。

例：
- `K4161` 赣州 395min → 阜阳 254min（-141 min，跳省）
- `K4162/K4164` 同上
- `Y76` 芜湖 205min → 南京 77min（-128 min）
- `C9703` 上海南 146min → 芜湖 42min（-104 min，方向反）
- `C3042/C3043` 芜湖 122min → 上海松江 44min（-78 min）

**根因**：scraper 解析时把多趟车的 stops 拼到同一车次 / sequence 顺序错乱 / 同一站多次插入相互覆盖。

### 3. stops 内 `arrive_time < 上一站 depart_time`（同日倒退）— **21 处**
- `C3042/C3043`：sequence 2 盛泽 06:46 → sequence 3 南京南 02:42（时间倒退 4h4min）
- `D5487`：sequence 7 南京南 21:42 depart → sequence 8 南京南 21:32 arrive（同一站、同一车、同一日，时间倒退 10 分钟——这是 #4 的 D5487 南京南重复 stop 的伴生现象）
- `D4836/D4837`：stops 序列里时间来回跳（12:37→05:39→13:22→06:06→14:02→06:25…）

### 4. stops 严重错乱车次（≥10 个 stop 早于起点）— **70 个车次**
- `K1277/K1276`：33 个 bad stops
- `K307/K306`：31 个
- `K348/K345`：29 个
- `Z596/Z593`：28 个
- `K347/K346`：27 个
- `K1558/K1555`：27 个
- `K657/K656`：26 个
- `K1013/K1012`：25 个
- `K308/K305`、`K1051/K1050`：24 个
- ……(共 70)

### 5. 同一车次同一站出现两次（重复 stop）— **4 处**
- `D4836` 汉口（出现在 seq 1 和 seq 14）
- `D4837` 汉口（同上）
- `D5487` 南京南（出现在 seq 7 和 seq 8）
- `D5490` 南京南（同上）

### 6. `total_duration_minutes` 偏差 > 5min（与 origin/dest_time 推算不一致）— **51 个车次**
多数是 day 错乱导致 computed 出现负数（dest_time < origin_time）。例：
- `Z593/Z596` total=2741, computed=-161（差 2902 min）
- `K1555/K1558` total=1834, computed=-1056（差 2890 min）
- `K135/K138` total=2451, computed=-431（差 2882 min）
- `K1011/K1014` total=2886, computed=6（差 2880 min）

### 7. `total_duration` 与 stops 推算偏差 > 10min — **3 个车次**
- `C3042` total=64, stops 推算 122（差 -58）
- `C3043` total=64, stops 推算 122（差 -58）
- `C9703` total=42, stops 推算 146（差 -104）

---

## 🟡 中等错误（业务逻辑不一致 / 字段标错）

### 8. `relation_to_wuhu` 与实际 origin/dest 站不符 — **5 个车次**
- `C3287/C3290`：dest=芜湖 但 `relation_to_wuhu='passing'`（应是 `destination`）
- `C9703`：dest=芜湖 但 `relation_to_wuhu='passing'`
- `D5490`：dest=芜湖 但 `relation_to_wuhu='passing'`
- `Y228`：origin=芜湖 但 `relation_to_wuhu='passing'`（应是 `origin`）

### 9. `station.direction` 缺失 — **109 个站**
109 个 station 的 `direction` 字段是空（NULL/''），没有 N/NE/E/... 的方向分类。这些站可能是新加入的，geocoding 时未填方向。

### 10. `station_url` 缺失 — **590 个站**（约 82%）
只有 128 个 station 有 `station_url`（形式如 `/anhui/anqing/anqing.html`），其余 590 个是空字符串。可能 scraper 没回填或不要求此字段。

### 11. 孤儿 cities — **40 个**
cities 表中有 40 个 city 没有被任何 station 引用（可能是 scraper 拉过但最终没匹配到站）。

---

## 🟢 已知正确（不应修）

### 12. `running_minutes` > 1440（24h+）是长途车正常情况
max 3165min（K551/K554 哈尔滨↔温州，52.75h），200+ 个 stop 超过 24h。中国铁路长途车 (K/Z/T) 跑 30+ 小时合理。

### 13. 17 个车次起点是 00:00-04:59 但 `depart_day=0` 是正确的
末站仍在 day 0 当天（如 C2421 02:09 亳州南→04:24 芜湖）。凌晨发车且当天到，day=0 没错。

### 14. 753 个"mid 站 stop_duration=0"是合理的
通过车 / 短暂技术停车（"通过"模式）确实在数据库中 stop_duration=0。该字段在 0 时表示"通过/不停车"，不是错误。

---

## 修复优先级建议

| 优先级 | 项 | 影响 | 工作量 |
|---|---|---|---|
| **P0** | #1 stop_duration ~1440 | 影响 detail panel 显示（车在池州停 24h） | 中：重 scrape + 修正 day 字段 |
| **P0** | #2-#5 stops 严重错乱 | 70 个车次 stops 完全乱序 | 大：scraper 解析逻辑重写 |
| **P1** | #6-#7 total_duration 偏差 | 数据自相矛盾 | 中：cross-check stops 推算 |
| **P1** | #8 relation_to_wuhu | 5 个车次分类错 | 小：直接 UPDATE |
| **P2** | #9 station.direction 缺失 | 109 个站无方向 | 中：按 lat/lon 推算 |
| **P3** | #10 station_url 缺失 | 数据完整度 | 大：补全 / 不重要 |
| **P3** | #11 孤儿 cities | 数据整洁度 | 小：DELETE / 留着 |

---

## 关键 SQL 复现命令

```bash
sqlite3 data/train.db "
-- #1: stop_duration > 1000
SELECT s.train_code, s.sequence, st.station_name, s.stop_duration, s.arrive_time, s.depart_time, s.arrive_day, s.depart_day
  FROM stops s JOIN stations st ON s.station_id=st.station_id
  WHERE s.stop_duration > 1000 LIMIT 10;

-- #4: 70 个严重错乱车次
SELECT s.train_code, COUNT(*) AS bad FROM stops s
  JOIN (SELECT train_code, MIN(depart_time) AS t0 FROM stops WHERE sequence=1 GROUP BY train_code) o
    ON s.train_code=o.train_code
  WHERE s.sequence>1 AND s.arrive_time!='' AND s.arrive_time<o.t0
  GROUP BY s.train_code HAVING bad>=10 ORDER BY bad DESC;
"
```
