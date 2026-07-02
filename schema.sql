-- train_crecc schema (SQLite)
-- 5 tables + meta. Single snapshot — full overwrite on each fetch.
-- The "skip if unchanged" logic is in META table.

-- 1. Cities (master data; preserved across runs)
CREATE TABLE IF NOT EXISTS cities (
  city_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  city_name  TEXT UNIQUE NOT NULL,
  province   TEXT
);

-- 2. Stations (master data; preserved across runs)
--    lat/lon + direction are added by post-fetch geocoding/analysis.
CREATE TABLE IF NOT EXISTS stations (
  station_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  station_name  TEXT UNIQUE NOT NULL,
  city_id       INTEGER REFERENCES cities(city_id),
  station_url   TEXT,
  lat           REAL,
  lon           REAL,
  direction     TEXT,                  -- N | NE | E | SE | S | SW | W | NW
  geocoded_at   TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_stations_city ON stations(city_id);
CREATE INDEX IF NOT EXISTS idx_stations_dir  ON stations(direction);

-- 3. Meta (single-row sentinel for skip-if-unchanged)
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 4. Trains (overwritten on each fetch)
CREATE TABLE IF NOT EXISTS trains (
  train_code            TEXT PRIMARY KEY,
  train_class           TEXT,
  train_class_full      TEXT,
  origin_station_id     INTEGER REFERENCES stations(station_id),
  origin_time           TEXT,
  dest_station_id       INTEGER REFERENCES stations(station_id),
  dest_time             TEXT,
  total_duration_minutes INTEGER,
  stop_count            INTEGER,
  detail_url            TEXT,
  relation_to_wuhu      TEXT,           -- origin | destination | passing
  fetched_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trains_origin ON trains(origin_station_id);
CREATE INDEX IF NOT EXISTS idx_trains_dest   ON trains(dest_station_id);
CREATE INDEX IF NOT EXISTS idx_trains_relation ON trains(relation_to_wuhu);

-- 5. Stops (overwritten on each fetch — delete + insert pattern)
CREATE TABLE IF NOT EXISTS stops (
  train_code      TEXT NOT NULL REFERENCES trains(train_code) ON DELETE CASCADE,
  sequence        INTEGER NOT NULL,
  station_id      INTEGER REFERENCES stations(station_id),
  arrive_time     TEXT,
  arrive_day      INTEGER,             -- 0 = 当日, 1 = 次日
  depart_time     TEXT,
  depart_day      INTEGER,
  stop_duration   INTEGER,             -- minutes
  running_minutes INTEGER,             -- minutes from train's origin
  PRIMARY KEY (train_code, sequence)
);
CREATE INDEX IF NOT EXISTS idx_stops_station ON stops(station_id);
CREATE INDEX IF NOT EXISTS idx_stops_train   ON stops(train_code, sequence);

-- 6. View: per-station "fastest from 芜湖" (the query the future frontend will use)
--    Guards against non-monotonic running_minutes in source data (~2% of trains).
DROP VIEW IF EXISTS v_station_reach;
CREATE VIEW v_station_reach AS
SELECT
  s2.station_id,
  s2.station_name,
  s2.city_id,
  c.city_name,
  s2.lat,
  s2.lon,
  s2.direction,
  MIN(s2_stop.running_minutes - s_stop.running_minutes) AS min_minutes,
  MAX(s2_stop.running_minutes - s_stop.running_minutes) AS max_minutes,
  COUNT(DISTINCT s2_stop.train_code) AS train_count
FROM stops s_stop
JOIN stations ws
  ON s_stop.station_id = ws.station_id AND ws.station_name = '芜湖'
JOIN stops s2_stop
  ON s_stop.train_code = s2_stop.train_code
 AND s_stop.sequence  < s2_stop.sequence
 AND s2_stop.running_minutes > s_stop.running_minutes  -- guard: time must increase
JOIN stations s2
  ON s2_stop.station_id = s2.station_id
JOIN cities c
  ON s2.city_id = c.city_id
GROUP BY s2.station_id;
