"""SQLite 存储层：schema 定义与连接助手。

设计原则：
- 所有结果（指标单元格、报表）都是不可变版本，任何重算只追加新版本，绝不覆盖历史行。
- 更正是快照之上的叠加层（overlay），与快照分开存储，因此撤回 = 重新计算时排除该更正，
  而不是"把最新文件覆盖回去"。
"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id   TEXT PRIMARY KEY,
  biz_date      TEXT NOT NULL,
  seq           INTEGER NOT NULL,          -- 同一 biz_date 的第几版快照
  imported_at   TEXT NOT NULL,
  record_count  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS source_records (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id      TEXT NOT NULL,
  biz_date      TEXT NOT NULL,
  channel       TEXT NOT NULL,
  amount        REAL NOT NULL,
  qty           INTEGER NOT NULL DEFAULT 1,
  status        TEXT NOT NULL DEFAULT 'normal',   -- normal | refunded | void
  refund_amount REAL NOT NULL DEFAULT 0,
  snapshot_id   TEXT NOT NULL REFERENCES snapshots(snapshot_id)
);
CREATE INDEX IF NOT EXISTS idx_records_date  ON source_records(biz_date);
CREATE INDEX IF NOT EXISTS idx_records_order ON source_records(order_id);

CREATE TABLE IF NOT EXISTS corrections (
  correction_id TEXT PRIMARY KEY,          -- 业务主键，幂等去重的依据
  type          TEXT NOT NULL,             -- adjust | void | insert
  target_order_id TEXT,
  biz_date      TEXT,                      -- 解析出的影响日期
  payload       TEXT NOT NULL,             -- JSON
  payload_hash  TEXT NOT NULL,
  status        TEXT NOT NULL,             -- pending | active | withdrawn | manual_pending | manual_closed
  received_at   TEXT NOT NULL,
  withdrawn_at  TEXT,
  arrival_count INTEGER NOT NULL DEFAULT 1 -- 到达次数（>1 说明有重复投递）
);

CREATE TABLE IF NOT EXISTS correction_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  correction_id TEXT NOT NULL,
  event         TEXT NOT NULL,             -- received | duplicate | conflict | activated | withdrawn
  detail        TEXT,
  at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_versions (
  rule_version TEXT PRIMARY KEY,
  description  TEXT NOT NULL,
  is_current   INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recalc_runs (
  run_id           TEXT PRIMARY KEY,
  trigger_type     TEXT NOT NULL,          -- snapshot | correction | withdrawal | manual_force
  trigger_refs     TEXT NOT NULL,          -- JSON: {correction_ids:[...], snapshot_id:...}
  rule_version     TEXT NOT NULL,          -- 本次重算使用的规则版本
  status           TEXT NOT NULL,          -- preview | confirmed | cancelled
  affected_dates   TEXT NOT NULL,          -- JSON [date,...]
  affected_reports TEXT NOT NULL,          -- JSON [report_key,...]
  created_at       TEXT NOT NULL,
  confirmed_at     TEXT
);

CREATE TABLE IF NOT EXISTS recalc_run_items (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          TEXT NOT NULL REFERENCES recalc_runs(run_id),
  cell_key        TEXT NOT NULL,           -- date|metric|dim
  biz_date        TEXT NOT NULL,
  metric          TEXT NOT NULL,
  dim             TEXT NOT NULL,
  old_value       REAL,
  old_version_no  INTEGER,
  old_rule_version TEXT,
  new_value       REAL NOT NULL,
  corrections_applied TEXT NOT NULL DEFAULT '[]',  -- JSON: 该单元格新值实际吃到的更正
  new_version_no  INTEGER                  -- 确认后回填
);
CREATE INDEX IF NOT EXISTS idx_run_items ON recalc_run_items(run_id);

CREATE TABLE IF NOT EXISTS cell_versions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  cell_key      TEXT NOT NULL,
  biz_date      TEXT NOT NULL,
  metric        TEXT NOT NULL,
  dim           TEXT NOT NULL,
  version_no    INTEGER NOT NULL,
  value         REAL NOT NULL,
  rule_version  TEXT NOT NULL,
  run_id        TEXT,
  corrections_applied TEXT NOT NULL DEFAULT '[]',
  input_record_ids    TEXT NOT NULL DEFAULT '[]',  -- JSON: 参与聚合的源记录 id
  restored_from_version_id INTEGER,                -- 撤回恢复时指向被恢复的历史版本
  created_at    TEXT NOT NULL,
  UNIQUE(cell_key, version_no)
);
CREATE INDEX IF NOT EXISTS idx_cell_versions ON cell_versions(biz_date);

CREATE TABLE IF NOT EXISTS report_defs (
  report_key  TEXT PRIMARY KEY,            -- monthly:2026-10 | weekly:2026-W45
  report_type TEXT NOT NULL,               -- monthly | weekly
  period      TEXT NOT NULL,
  date_from   TEXT NOT NULL,
  date_to     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_versions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  report_key   TEXT NOT NULL REFERENCES report_defs(report_key),
  version_no   INTEGER NOT NULL,
  status       TEXT NOT NULL,              -- draft | published | superseded
  stale        INTEGER NOT NULL DEFAULT 0, -- 1 = 有更新的草稿或未确认重算，已过期
  run_id       TEXT,                       -- 由哪个重算运行生成（NULL = 手工生成）
  rule_version TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  published_at TEXT,
  UNIQUE(report_key, version_no)
);

CREATE TABLE IF NOT EXISTS report_items (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  report_version_id INTEGER NOT NULL REFERENCES report_versions(id),
  metric            TEXT NOT NULL,
  dim               TEXT NOT NULL,
  value             REAL NOT NULL,
  cell_version_ids  TEXT NOT NULL          -- JSON: 血缘，指向 cell_versions.id
);
CREATE INDEX IF NOT EXISTS idx_report_items ON report_items(report_version_id);

CREATE TABLE IF NOT EXISTS manual_queue (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  correction_id TEXT NOT NULL,
  reason        TEXT NOT NULL,             -- out_of_window | payload_conflict | target_not_found
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending | resolved
  resolution    TEXT,
  created_at    TEXT NOT NULL,
  resolved_at   TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  at      TEXT NOT NULL,
  actor   TEXT NOT NULL DEFAULT 'system',
  action  TEXT NOT NULL,
  detail  TEXT
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def reset(conn):
    """清空全部业务数据（保留表结构与规则版本），供演示重置使用。"""
    for table in (
        "snapshots", "source_records", "corrections", "correction_events",
        "recalc_runs", "recalc_run_items", "cell_versions",
        "report_defs", "report_versions", "report_items",
        "manual_queue", "audit_log",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
