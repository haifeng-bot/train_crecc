# AGENTS.md — train_crecc 心智模型

> 给我自己（同类的 agent）看的项目说明。README 给 GitHub 访客看命令清单，AGENTS.md 给我的是**进入项目能立刻开始干活**的心智模型：架构、状态机、踩过的坑、工作约定。
>
> 任何**功能逻辑变更**都要同步更新本文件（约定见 §7）。

---

## 1. 一句话定位

抓 crecc.com 芜湖站列车数据 → SQLite → 导出 `reach.json` → 前端 Leaflet 地图可视化「从芜湖出发 N 分钟内能到哪些站」。

- 数据源：芜湖站页面 + 761 个车次详情页
- 用户视角：自己住在芜湖，要直观看"40 小时通达圈"
- 部署：前端是 Cloudflare Pages 上的纯静态站（无后端）

---

## 2. 架构 & 数据流

```
crecc.com / 芜湖站页面 + 761 个详情页
       │
       ▼  Python scrapers/  (throttled: 1.5s/req, 5s/50 趟)
   SQLite  data/train.db  (5 表 + 1 视图, 单次 fetch 全量覆盖)
       │
       ▼  python main.py export-reach
   reach.json  ←  frontend/data/reach.json  (Cloudflare Pages 静态托管)
       │
       ▼  Leaflet 前端
   浏览器
```

3 层互相**独立**：scraper 慢、更改少；DB 中间层稳定；**前端是改动最频繁的部分**——下面多着墨。

---

## 3. 关键命令（最常敲的几个）

```bash
cd train_crecc/

# 数据
python main.py fetch              # 全量抓（页面未变则跳过）
python main.py status             # DB 状态 + last_fetch_at
python main.py export-reach       # 出 reach.json 给前端
python main.py reach 60           # 60 分钟内可达站（CLI 调试）
python main.py city 杭州          # 查车次

# 前端本地预览（任何时候）
cd frontend/ && python3 -m http.server 8000
# 浏览器开 http://127.0.0.1:8000/

# 自动 cron 抓数据
scripts/cron_fetch.sh
```

极别忘了 `data_errors_audit.md`——之前排查数据问题时写的笔记，下次撞类似问题先读它。

---

## 4. 核心: 前端 Leaflet 可视化

> **这是改动最频繁的部分**。我大部分工作在这里。

文件三件套都在 `frontend/`：
- `index.html` — 顶层 DOM（topbar / map / sidebar / detail-panel / legend / controls / loading-bar）
- `app.js` — 状态机 + Leaflet 渲染 + slider 联动
- `style.css` — 设计 token (`:root`) + 各组件样式 + **z-index 分层**
- `data/reach.json` — 前端的全部数据；fetch 写到此处后被静态托管

### 状态机

```
init
  │  loadData() 取 reach.json
  ▼
slider=0          ← 全部站点 dimmed，无路线，仅有 hub 脉动点
  │  用户拖滑块（debounce 1s）
  ▼
slider>0          ← 站点染色（按最快车次类型色），polylines 铺出，sidebar 列站点
  │  hover station / polyline
  ▼
hover             ← .station-tooltip 浮出站名 / polyline tooltip 显示时间+车次
  │  click station / polyline
  ▼
selected          ← station: flyTo + openPopup(small) + showDetailPanel(big)
                   polyline: showDetailPanel(big)  无 popup
                   其他站点/路线 dim
  │  ESC / close / 空白点击
  ▼
deselected        ← 回到上一状态，restore 默认颜色与 opacity
```

### `reach.json` 关键字段契约

```ts
{
  hub: { name, lat, lon }                      // 必填
  max_minutes: number                          // 上限，决定 slider 范围
  last_updated: "2026-06-26 02:04:49"          // 可选，进 #data-subtitle
  stations: [
    {
      id: 12,                                  // 内部稳定 id
      name: "芜湖北", city: "芜湖", lat, lon,
      direction: "N" | "NE" | ... | "NW" | null,
      min_minutes: 11,                         // 从芜湖最快的车程
      fastest_train_code: "G302",
      train_count: 5,                          // 经过 N 趟
      route: [ { name, lat, lon, run_min } ]   // 最快车次的完整经停，route[0] = 芜湖
    }
  ]
}
```

⚠️ `route[0]` 永远是 芜湖（API 约定）。前端 polyline 渲染时会把它在屏幕空间外向推 `HUB_PX_OFFSET=2` 像素，避免被 hub marker 吃掉线头——见 `offsetFirstStopFromHub()` + `recomputeRouteOffsets(zoomend)`。

### z-index 分层（设计决策，必须记住）

```
layer                    z-index
───────────────────────────────
#loading-bar             9999   ← 全局最顶（加载提示）
#detail-panel            1200   ← 经停详情面板（左上，可遮挡 legend）
.leaflet-popup-pane      1100   ← station popup / polyline popup
.leaflet-top             1000   ← 默认的 zoom 控件 + 我们的 legend
                            │     （任何想压在 legend 之下的元素必须 ≥ 1100）
.leaflet-bottom           1000   ← attribution
#sidebar                  600   ← 右侧面板，与 legend 不冲突
#topbar / #controls       500   ← 页头/页脚
```

**改 z-index 的硬规则**：在 `style.css` 末尾写 override，不要写在原 `#xxx` 选择器块**前面**——cascade 顺序会让你的新值被原值覆盖（这是我 2026-07-09 踩过的坑，看 §8 那条 commit）。

**改任何 z-index 前先 grep**：`grep -n "z-index" frontend/style.css`，统一规划分层再下笔。

### 关键 Leaflet API 习惯

- 所有 station marker 用 `L.divIcon({ html: '<div class="station-marker">…' })` 而不是 `L.Icon`，方便 CSS 完全控制外观
- polyline 三件套（shadow / visible / hit）—— hit 是不可见但可点击的胖线，专门接 `bindTooltip` 和 `click`
- polyline 的颜色由最快车次的车次号首字母决定（G/D/C/Z/T/K），见 `trainTypeColor()`
- fit-bounds 用 `L.latLngBounds(hub + reachable stations)` + 5% 屏幕 padding

---

## 5. 核心: Python 抓取 pipeline

- **入口**：`main.py` 8 个 subcommand，最常用 `fetch` / `status` / `reach` / `city` / `export-reach`
- **节流**：`INTER_REQUEST_SLEEP=1.5s` / `INTER_BATCH_SLEEP=5s/50 趟`，对 crecc 礼貌
- **skip-if-unchanged**：通过页面顶部"更新时间"字符串对比 `meta.last_updated`——变了才真抓
- **覆盖策略**：每次 fetch 是 `DELETE + INSERT` 的全量覆盖，不留历史快照
- **v_station_reach 视图**：所有前端可见的可达性数据走这个 view，对源站数据里 ~2% 的 `running_minutes` 非单调问题做了保护

### 抓相关的边界

- 大约 737 车次 → 761 个详情页 → 1 万+ stops
- 单次 fetch 完整流程大约 15-20 分钟（节流导致）
- 如果改 scraper，**先看 `scrapers/wuhu_page.py` 和 `train_detail.py` 的解析函数**——crecc 站点的 HTML 结构是隐式契约

---

## 6. 数据模型（速记）

5 表 + 1 视图（schema.sql 全文）：

- `cities` / `stations` — 主数据，跨抓取保留
- `meta` — 单行 sentinel，存 `last_updated` 等
- `trains` / `stops` — 每次抓取全量覆盖
- `v_station_reach` — view，给前端供"每个站到芜湖的最短时间 + 最快车次"——前端 reach.json 实际上就是这个 view 的 export

---

## 7. 工作约定（2026-07-09 海峰明示）

1. **细微改动不自己测、不截图**：直接改 + commit + push。**除非用户明确要求**做端到端测试。
2. **功能逻辑变更必须更新本文件**对应章节（架构、状态机、数据契约等）。改完顺手做。
3. **改 CSS / z-index 必须先 grep**：见 §4 末尾硬规则。
4. **改前端前先看 schema**：reach.json 字段是契约，改 app.js 之前先确认 reach.json 长这样。
5. **任何 commit 前检查 `.gitignore`**：`data/raw/`、`*.db`、`*.bak`、`*.png`（截图）—— 一律不能入仓。
6. **commit 风格**：`feat/fix/refactor/docs(<scope>): 中文主题`。commit 完**立刻 push**（MEMORY.md 里的"推送铁律"——所有改动都是 agent 自己做的，不需要 watcher）。
7. **没有任何自动化测试**——`tests/` 不存在。如果哪天需要，写在 `tests/` 下，用 Playwright + pytest，跟 reach.json 的 schema 一起作为快速回归。

---

## 8. 变更日志（重要改动留痕）

| 日期 | 改动 | commit |
|---|---|---|
| 2026-07-09 | (1) 站点悬停 tooltip；(2) 经停 popup/面板现在覆盖 legend（之前被遮挡） | `8feab77` |
| 2026-07-04~08 | preset buttons 折行、piecewise slider、legend 折叠 | `d195cf2` / `282de78` / `c1e599f` |
| 2026-07-03 | 修了 K4161/K4164 损坏记录；audit 数据问题 | `22d2749` |
| 2026-07 之前 | 全量抓管道 + Cloudflare Pages 部署 | (历史 commits) |

**新改动加在最上面一行的上面**——保持时序最优先、最新在前。

---

## 9. 一次踩坑教训（给自己看的）

- ✅ 进项目**先搜 AGENTS.md / CONTRIBUTING / CONVENTIONS**（如果之前有，可能写在 README 上半部）
- ✅ 改 z-index **先 grep**所有 z-index，再写 override
- ⚠️ z-index override **永远写在被 override 的选择器之后**，否则被 cascade 吃掉（详细见 `style.css` 末尾注释）
- ⚠️ 不要在 workspace 顶层建 `.git`——workspace 是容器，每个子项目是独立 git
