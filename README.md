# train_crecc — 芜湖站列车数据维护

从 [crecc.com](https://www.crecc.com/anhui/wuhu/wuhu.html) 抓取芜湖站所有列车信息，保存到本地 SQLite 数据库中。

## 项目目标

- 自动监测芜湖站页面更新，增量获取数据
- 完整保存每个车次的经停站点、时刻、运行时间
- 支持经停城市查询和基于时间范围的可达性分析
- 为后续 web 地图可视化提供数据层

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 全量抓取（首次运行）
python main.py fetch

# 查看数据库状态
python main.py status

# 查询所有从芜湖到杭州的列车
python main.py city 杭州

# 查询芜湖 60 分钟内可达的车站
python main.py reach 60
```

## 命令

| 命令 | 说明 |
|------|------|
| `fetch` | 全量抓取。若页面上次更新时间未变则跳过 |
| `status` | 显示数据库大小和最近抓取时间 |
| `geocode` | 对未编码的车站进行地理编码（经纬度） |
| `directions` | 计算从芜湖出发的 8 方位方向 |
| `reach <min>` | 查询 N 分钟内可达车站 |
| `city <city>` | 查询所有从芜湖到该城市的列车 |

## 数据库结构

5 张表 + 1 视图：

```
cities    → 城市（主数据，跨抓取保留）
stations  → 车站/站点（含 lat/lon/direction）
meta      → 元数据（仅用于 skip-if-unchanged 逻辑）
trains    → 车次（每次抓取全量覆盖，ON DELETE CASCADE）
stops     → 经停（每次抓取全量覆盖）

v_station_reach → 视图：每个车站离芜湖最快的到达分钟数
```

## 数据流

```
crecc.com/芜湖站  ⟶  解析 737 车次
      ↓
对比 meta.last_updated
      ↓
未变 → 跳过
改变 → 抓 761 个详情页（含 10k+ 经停记录）
      ↓
全量写入 SQLite（DELETE + INSERT）
      ↓
更新 meta.last_updated
```

## 设计决策

- **单快照覆盖**：每次运行全量覆盖，不保存历史快照
- **skip-if-unchanged**：通过页面的"更新时间"字符串判断
- **地理编码**：Nominatim 开源地图服务（1 req/s 限速）
- **方向计算**：8 方位从芜湖车站的大圆方位角
- **运行时间保护**：v_station_reach 过滤了非单调的 running_minutes（~2% 车次存在源站数据异常）
- **SQLite**: 单文件，零配置，足够 700+ 车次和 10k+ 经停

## 数据源

- 页面: `https://www.crecc.com/anhui/wuhu/wuhu.html`
- 详情: `https://www.crecc.com/huoche/{code}.html`
- 更新时间: 2026-06-26 02:04:49 (最后抓取值)

## 许可证

MIT
