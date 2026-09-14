# -*- coding: utf-8 -*-
"""env_tools.db — SQLite 连接管理与 schema(多组织版, SCHEMA_VERSION 6)

约定(见 V2-SQLITE-DESIGN.md / V2-IMPLEMENTATION-GUIDE.md §4):
- WAL + busy_timeout; 每线程独立连接(check_same_thread=False 且单线程使用), 短事务;
- 所有时间戳 epoch(UTC); models/files/tasks/alerts/license_map 带 org 列;
- token 永不入 DB。

SCHEMA_VERSION 2 变更(与设计文档对齐):
- tasks.status CHECK 增加 'obsolete'(源 repo/文件已消失的任务作废态);
- models 增加 repo_created_by_us / missing_since / gitcode_status / gitcode_checked_at;
- files 增加 is_init(README 行专用: 1=空/平台初始化内容, 不参与同步)。

SCHEMA_VERSION 4 变更(2026-09): 隐藏/毒瘤文件改为【拉取源头过滤】(fetch_remote_files
丢弃, 不落库不比对) → 一次性清理 files 表历史遗留行(poison 标记行 + 路径含隐藏段的旧行)。

SCHEMA_VERSION 5 变更(2026-09): files 增加 rehash_checked_at —— 强哈希周期内"已核验"
标记(强哈希首次全量分批跑 + 30d 复核时, 本周期已核验的行不再重复下载)。

SCHEMA_VERSION 6 变更(2026-09): tasks 增加 progress(执行进度文本)/started_at(领取时刻)
—— status 可直接显示长任务"传到哪了/已耗时", 不再依赖翻 journal(可见性补强)。

SCHEMA_VERSION 7 变更(2026-09-14): files 增加 rehash_fail_count / rehash_next_try_at ——
强哈希下载失败的**退避**记录: 失败不再每轮重选(失败计数 + 下次重试时间, 指数退避上限 7d);
另新增 set_app_config() upsert, 修 `sync.last_reconcile` / `sync.last_forced_rehash` 两个
时间戳误用 seed_app_config(INSERT OR IGNORE)导致"只写一次、之后永不更新"的缺陷。

SCHEMA_VERSION 8 变更(2026-09-14, 对账调度重构): models 增加 files_verified_lm ——
**该侧仓上"已核验过文件树"的 last_modified**。队列生成阶段只拉两侧 model_list,
用 `cur.last_modified != files_verified_lm` 判断该 repo 是否有文件级变动并入队 file_batch;
真正的文件级比对(魔塔 1 次 list_repo_files + 魔乐 1 次 list_repo_tree)放到任务执行时按
单仓做; 核验成功(无差异或同步成功)才回写该值 → 失败自动保持 dirty、下轮重入队。
首次部署 / `audit --force` 仍走全量扫描(此时该列为 NULL = 全部 dirty)。
"""
import os
import sqlite3
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(PROJECT_ROOT, "sync.db")

SCHEMA_VERSION = 8

TASKS_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  org           TEXT NOT NULL,
  dedup_key     TEXT NOT NULL UNIQUE,
  kind          TEXT NOT NULL,
  model         TEXT NOT NULL,
  direction     TEXT,
  file          TEXT,
  priority      INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','claimed','running','succeeded','failed',
                                  'interrupted','obsolete')),
  attempts      INTEGER NOT NULL DEFAULT 0,
  max_attempts  INTEGER NOT NULL DEFAULT 3,
  next_retry_at INTEGER NOT NULL DEFAULT 0,
  created_by    TEXT NOT NULL,
  repo_created_by_us INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  progress      TEXT,                              -- v6: 执行进度文本(status 显示)
  started_at    INTEGER,                           -- v6: 领取时刻(计算已耗时)
  created_at    INTEGER NOT NULL,
  finished_at   INTEGER,
  CHECK (priority IN (0, 10, 100))
);
CREATE INDEX IF NOT EXISTS idx_tasks_pick ON tasks (org, status, priority DESC, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks (org, model, status)
  WHERE status IN ('claimed','running');
"""

SCHEMA_SQL = """
-- ── 结构版本 ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
);

-- ── 模型索引 ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS models (
  org           TEXT    NOT NULL,
  platform      TEXT    NOT NULL,
  repo_id       TEXT    NOT NULL,
  owner         TEXT,
  name          TEXT    NOT NULL,
  visibility    INTEGER,
  private       INTEGER NOT NULL DEFAULT 0,
  gated         INTEGER NOT NULL DEFAULT 0,
  login_required INTEGER,
  description   TEXT,
  downloads     INTEGER,
  likes         INTEGER,
  created_at    INTEGER,
  last_modified INTEGER,
  license_raw   TEXT,
  license_norm  TEXT,
  display_name  TEXT,
  file_size     INTEGER,
  params        INTEGER,
  tasks_json    TEXT,
  tags_json     TEXT,
  model_type    TEXT,
  card_json     TEXT,
  first_seen_at INTEGER,
  last_seen_at  INTEGER NOT NULL,
  -- SCHEMA_VERSION 2 新增:
  repo_created_by_us INTEGER NOT NULL DEFAULT 0,   -- 该 repo 是否由同步器创建(删除传播护栏)
  missing_since INTEGER,                           -- 模型级删除宽限期计时
  gitcode_status TEXT,                             -- NULL/pending/imported/failed/skipped
  gitcode_checked_at INTEGER,
  files_verified_lm INTEGER,                       -- v8: 已核验文件树的那一版仓 last_modified
  PRIMARY KEY (org, platform, repo_id)
);
CREATE INDEX IF NOT EXISTS idx_models_fingerprint ON models (org, platform, last_modified);
CREATE INDEX IF NOT EXISTS idx_models_visibility ON models (org, platform, visibility);
CREATE INDEX IF NOT EXISTS idx_models_missing ON models (org, missing_since)
  WHERE missing_since IS NOT NULL;

-- ── 文件索引 ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS files (
  org           TEXT    NOT NULL,
  platform      TEXT    NOT NULL,
  repo_id       TEXT    NOT NULL,
  path          TEXT    NOT NULL,
  size          INTEGER NOT NULL DEFAULT 0,
  sha256        TEXT,
  sha256_source TEXT    NOT NULL DEFAULT 'none' CHECK (sha256_source IN ('api','local','git','none')),
  type          TEXT    NOT NULL DEFAULT 'blob',
  last_modified INTEGER,
  is_lfs        INTEGER,
  mode          TEXT,
  revision      TEXT,
  committer     TEXT,
  poison        TEXT,
  missing_since INTEGER,
  last_seen_at  INTEGER NOT NULL,
  -- SCHEMA_VERSION 2 新增:
  blob_id       TEXT,                              -- 魔乐非 LFS: git SHA1 变更指纹(每侧变更检测)
  is_init       INTEGER NOT NULL DEFAULT 0,        -- README 行专用: 1=空/平台初始化内容(不同步)
  last_synced_at INTEGER,                          -- v3: 最近成功同步时间(审计/滞后统计)
  rehash_checked_at INTEGER,                       -- v5: 强哈希周期内"已核验"标记
  rehash_fail_count INTEGER NOT NULL DEFAULT 0,    -- v7: 强哈希下载连续失败次数(退避用)
  rehash_next_try_at INTEGER,                      -- v7: 下次重试时间(指数退避, 上限 7d)
  PRIMARY KEY (org, platform, repo_id, path)
);
CREATE INDEX IF NOT EXISTS idx_files_repo ON files (org, platform, repo_id);
CREATE INDEX IF NOT EXISTS idx_files_missing ON files (missing_since) WHERE missing_since IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_files_poison ON files (poison) WHERE poison IS NOT NULL;

-- ── license 归一化映射 ────────────────────────────────────
CREATE TABLE IF NOT EXISTS license_map (
  org       TEXT NOT NULL,
  raw       TEXT,
  norm      TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (org, raw)
);

-- ── 任务队列(含 obsolete; 见 TASKS_SQL)────────────────────
""" + TASKS_SQL + """
-- ── 运行期配置键值表 ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS app_config (
  org        TEXT NOT NULL,
  key        TEXT NOT NULL,
  value      TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (org, key)
);

-- ── 告警台账 ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  org        TEXT NOT NULL,
  task_id    INTEGER,
  model      TEXT,
  level      TEXT NOT NULL CHECK (level IN ('warn','critical')),
  error      TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts (org, created_at);

-- ── 心跳 ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS heartbeat (
  id            INTEGER PRIMARY KEY CHECK (id = 1),
  last_cycle_at INTEGER NOT NULL,
  last_task_at  INTEGER,
  pid           INTEGER
);
"""

_thread_local = threading.local()


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    """每线程独立连接; 线程内创建、线程内使用, 绝不跨线程共享"""
    path = db_path or DEFAULT_DB
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _thread_local.conn = conn
    return conn


def close_conn() -> None:
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        conn.close()
        _thread_local.conn = None


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if exists is None:
        return                        # 表不存在(异常/极老库): 跳过, 不炸
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _ensure_known_columns(conn: sqlite3.Connection) -> None:
    """幂等补齐所有"追加列"(2026-09 加固)。

    历史问题: 新库走 SCHEMA_SQL 建表、旧库走 migrate 的条件分支; 若某列只写进升级
    路径(如 v5 的 files.rehash_checked_at), 删库重建的新库会缺列、而版本号已是最新
    → 升级分支被跳过 → 运行期 OperationalError: no such column。
    现改为**每次启动**都按表逐列幂等检查(有则跳过、缺则 ALTER), 与版本号解耦。
    """
    for table, column, ddl in (
        ("models", "repo_created_by_us", "repo_created_by_us INTEGER NOT NULL DEFAULT 0"),
        ("models", "missing_since", "missing_since INTEGER"),
        ("models", "gitcode_status", "gitcode_status TEXT"),
        ("models", "gitcode_checked_at", "gitcode_checked_at INTEGER"),
        ("files", "blob_id", "blob_id TEXT"),
        ("files", "is_init", "is_init INTEGER NOT NULL DEFAULT 0"),
        ("files", "last_synced_at", "last_synced_at INTEGER"),
        ("models", "files_verified_lm", "files_verified_lm INTEGER"),
        ("files", "rehash_checked_at", "rehash_checked_at INTEGER"),
        ("files", "rehash_fail_count", "rehash_fail_count INTEGER NOT NULL DEFAULT 0"),
        ("files", "rehash_next_try_at", "rehash_next_try_at INTEGER"),
        ("tasks", "progress", "progress TEXT"),
        ("tasks", "started_at", "started_at INTEGER"),
    ):
        _ensure_column(conn, table, column, ddl)


def _tasks_needs_rebuild(conn: sqlite3.Connection) -> bool:
    """旧版 tasks 的 CHECK 不含 obsolete → 需要重建(索引随表重建)"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()
    return row is not None and "obsolete" not in (row["sql"] or "")


def migrate(db_path: str | None = None) -> None:
    """幂等建表 + 版本迁移 + 追加列幂等补齐。

    - 全新库: 执行 SCHEMA_SQL 建表 → 再幂等补列(双保险) → 记录 SCHEMA_VERSION;
    - 已有库: **无论版本号**都先 `_ensure_known_columns`(2026-09 加固: 版本号够新但
      列缺失的库能自愈, 例如"删库重建的新库缺 v5 列"这类漂移), 再做版本相关的
      一次性数据迁移(v4 隐藏行清理)与 tasks 表重建; 最后按需写入版本号。
    - tasks 因 CHECK 约束无法 ALTER 时走重建(开发期可接受; 生产如需保全任务数据另行设计)。
    """
    conn = get_conn(db_path)
    with conn:
        has = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        current = 0
        if has:
            cur = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = cur["v"] or 0

        if current == 0:
            # 全新库(或从未记录版本的库)
            conn.executescript(SCHEMA_SQL)
            _ensure_known_columns(conn)
            conn.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(?,?)",
                         (SCHEMA_VERSION, int(time.time())))
            return

        # 已有库: 先幂等补列(与版本号解耦, 防"版本够新但列缺失"漂移)
        _ensure_known_columns(conn)
        if _tasks_needs_rebuild(conn):
            # 旧 tasks 的 CHECK 不含 obsolete/新列 → 重建(索引随表重建)
            conn.execute("DROP TABLE IF EXISTS tasks")
            conn.executescript(TASKS_SQL)
            _ensure_known_columns(conn)
        if current < 4:
            # v4(2026-09): 隐藏/毒瘤文件源头过滤(不落库) → 清理历史遗留行:
            #   poison 已标记(platform/poison)的行 + 旧规则时期 poison=NULL 的
            #   隐藏路径行(任一路径段以 '.' 开头)。一次性, 幂等。
            _cleanup_hidden_rows(conn)
        if current < 8:
            # v8(2026-09-14): 调度重构引入 models.files_verified_lm(队列生成靠它判 dirty)。
            # 升级回填: 已有文件基线的仓 = 旧实现刚全量比对过 → 直接以当前 last_modified
            # 视为"已核验", 避免升级后第一轮把全部仓再扫一遍(无基线的仍为 NULL → dirty)。
            n = _backfill_files_verified(conn)
            if n:
                print(f"[db] v8 回填 models.files_verified_lm: {n} 行(已有文件基线视为已核验)")
        if current < SCHEMA_VERSION:
            conn.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(?,?)",
                         (SCHEMA_VERSION, int(time.time())))


def _cleanup_hidden_rows(conn: sqlite3.Connection) -> None:
    """删除 files 表中的历史隐藏/毒瘤行(2026-09 v4 一次性清理)。

    匹配: poison ∈ (platform, poison), 或任一路径段以 '.' 开头(旧规则时期
    poison=NULL 的 .gitkeep/.gitignore 等)。此后 fetch 源头过滤, 不再产生新行。
    files 主键 = (org, platform, repo_id, path), 无 id 列。
    """
    rows = conn.execute(
        "SELECT org, platform, repo_id, path, poison FROM files").fetchall()
    n = 0
    for r in rows:
        path = r["path"] or ""
        hidden = any(seg.startswith(".") for seg in path.split("/") if seg)
        if r["poison"] in ("platform", "poison") or hidden:
            conn.execute(
                "DELETE FROM files WHERE org=? AND platform=? AND repo_id=? AND path=?",
                (r["org"], r["platform"], r["repo_id"], path))
            n += 1
    if n:
        print(f"[db] v4 清理隐藏/毒瘤历史文件行: {n}")


def _backfill_files_verified(conn: sqlite3.Connection) -> int:
    """v8 一次性回填: 有文件基线的 models 行 → files_verified_lm = 当前 last_modified。

    语义 = "这个仓的文件树旧实现已经全量比对过"(升级前那一轮刚扫过), 因此不必再扫一遍;
    没有 files 行的模型保持 NULL → 仍会被判 dirty, 由任务执行时采纳建基线。
    """
    cur = conn.execute(
        "UPDATE models SET files_verified_lm = last_modified "
        "WHERE files_verified_lm IS NULL AND EXISTS ("
        "  SELECT 1 FROM files f WHERE f.org = models.org AND f.platform = models.platform "
        "    AND f.repo_id = models.repo_id)")
    return cur.rowcount or 0


def seed_app_config(conn: sqlite3.Connection, org_id: str, defaults: dict) -> None:
    """把 config.yaml 的运行期配置种子化进 app_config(INSERT OR IGNORE, 运行期修改不被覆盖)"""
    now = int(time.time())
    with conn:
        for key, value in defaults.items():
            import json
            conn.execute(
                "INSERT OR IGNORE INTO app_config(org, key, value, updated_at) VALUES(?,?,?,?)",
                (org_id, key, json.dumps(value, ensure_ascii=False), now),
            )


def set_app_config(conn: sqlite3.Connection, org_id: str, key: str, value) -> None:
    """写入/覆盖 app_config 键值(upsert) —— 用于**运行期会变**的配置与时间戳。

    2026-09-14 新增: 与 seed_app_config(INSERT OR IGNORE, 只做 config.yaml 种子化)区分。
    历史缺陷: `sync.last_reconcile` / `sync.last_forced_rehash` 曾用 seed_app_config 写,
    结果只有首次插入生效、之后永不更新 → 15min 对账节流在首次之后永久失效(每个空转轮都
    跑整轮对账)、30d 强哈希周期无法重置。时间戳/运行期状态一律用本函数。
    """
    import json
    with conn:
        conn.execute(
            "INSERT INTO app_config(org, key, value, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(org, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (org_id, key, json.dumps(value, ensure_ascii=False), int(time.time())),
        )


def set_runtime_state(conn: sqlite3.Connection, org_id: str, key: str, value: str) -> None:
    """写入运行期状态(直接覆盖, 与 seed_app_config 的 INSERT OR IGNORE 不同)。

    用途(2026-09 可见性): 对账/强哈希的实时进度写入 app_config, status 直接读取,
    不必翻 journal。value 存纯文本, get_app_config 解析 JSON 失败会原样返回。
    """
    set_app_config(conn, org_id, key, str(value))


def get_app_config(conn: sqlite3.Connection, org_id: str, key: str, default=None):
    import json
    row = conn.execute("SELECT value FROM app_config WHERE org=? AND key=?", (org_id, key)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return row["value"]


def touch_heartbeat(conn: sqlite3.Connection, pid: int | None = None,
                    last_task_at: int | None = None) -> None:
    """刷新心跳; pid 传 None 表示"只刷时间、保留已有 pid"(2026-09)。

    对账轮(scheduler_tick)可能持续几十分钟(逐模型拉双侧文件树 + 强哈希分批),
    期间由 file_level 周期性调用本函数保持 last_cycle_at 新鲜, 避免 status 心跳
    停滞被误判为 daemon 异常; 不覆盖 pid(CLI audit 不该顶替 daemon 的 pid)。
    """
    now = int(time.time())
    with conn:
        conn.execute(
            "INSERT INTO heartbeat(id, last_cycle_at, last_task_at, pid) VALUES(1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_cycle_at=excluded.last_cycle_at, "
            "last_task_at=COALESCE(excluded.last_task_at, heartbeat.last_task_at), "
            "pid=COALESCE(excluded.pid, heartbeat.pid)",
            (now, last_task_at, pid),
        )
