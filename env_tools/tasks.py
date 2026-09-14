# -*- coding: utf-8 -*-
"""env_tools.tasks — 任务仓库: 入队/去重/claim/per-model lease/三档重启/崩溃恢复/告警(见指南 §5)

状态机(失败走【三档重启制】, 不设执行时间上限):
  pending --(claim)--> claimed --(runner)--> running --(成功)--> succeeded
     ^                    |                     |
     |             崩溃/子进程被杀          第 1 次失败 → WARN + 原位重启(不排最后)
     +---(重启: claimed/running 回退 pending)  第 2 次失败 → WARN + 压到队列尾部
                                              达到 max_attempts → ERROR + failed(等人工)
  running --(紧急抢占)--> interrupted --(重做规则)--> pending(原优先级, 不消耗失败次数)

重试政策【三档重启制】:
  为什么不设"最长执行时间": 权重越来越大, 2.4T 权重千兆满速也要 5.6h,
  加上降速与前后处理很可能超 6h —— 固定超时不可控, 所以以【失败次数】为准。
  attempts 由 claim 时 +1(= 第几次拉起), max_attempts 默认 3:
    第 1 次失败 → WARNING + 原位重启(回 pending, next_retry_at=now, 不排到最后);
    第 2 次失败 → WARNING + 压到队列尾部等待重启;
    达到上限   → ERROR + failed + 告警, 不再拉起, 等人工排查。
  不可重试错误(401/403/404/gated/权限) → 直接 failed, 不浪费机会。

本模块为骨架(Phase 1 已完成版): 可直接使用。
"""
import time

import sqlite3

# 状态
PENDING, CLAIMED, RUNNING, SUCCEEDED, FAILED, INTERRUPTED, OBSOLETE = (
    "pending", "claimed", "running", "succeeded", "failed", "interrupted", "obsolete")
ACTIVE = (CLAIMED, RUNNING)
# 终态(不再被 claim / 崩溃恢复触碰)
TERMINAL = (SUCCEEDED, FAILED, INTERRUPTED, OBSOLETE)

# 优先级
PRIORITY_NORMAL, PRIORITY_URGENT_QUEUE, PRIORITY_URGENT_PREEMPT = 0, 10, 100

# 任务类型
KIND_MODEL_SYNC, KIND_FILE_BATCH, KIND_GITCODE_IMPORT, KIND_REPO_DELETE = (
    "model_sync", "file_batch", "gitcode_import", "repo_delete")

# 错误分类(Phase 2 起改为三分: retryable / fatal / obsolete):
#   可重试: 网络/5xx/429/超时 → 三档重启;
#   不可重试(fatal): 401/403/gated/permission/token → 直接 failed + 告警;
#   obsolete(源消失): 源侧 404/not exist → 任务作废, 不占次数不告警, 触发本地清理
#   (见 V2-DESIGN.md §4 / 指南 §5.1; 当前 classify_error 仍是两分 bool, Phase 2 细化)
_NON_RETRYABLE_HINTS = (
    "401", "403", "404", "not exist", "not found", "gated", "permission",
    "authentication", "token", "unauthorized", "forbidden", "不存在", "无可用凭据",
)


def dedup_key(org: str, kind: str, model: str, direction: str | None = None,
              file: str | None = None) -> str:
    return f"{org}:{kind}:{model}:{direction or '*'}:{file or '*'}"


def enqueue_task(conn: sqlite3.Connection, org: str, kind: str, model: str,
                 direction: str | None = None, file: str | None = None,
                 priority: int = PRIORITY_NORMAL, created_by: str = "manual",
                 max_attempts: int = 3) -> tuple[int, bool]:
    """入队去重语义:
      - 同 dedup_key 存在【非终态】(pending/claimed/running)→ 返回已有, 不新建;
      - 同 dedup_key 已是【终态】(succeeded/failed/interrupted/obsolete)→ 删除旧行重建
        (新一轮差异/手动重试必须能重新入队, 否则终态占位会永久阻塞后续同步)。

    返回 (task_id, 是否新建)。
    """
    now = int(time.time())
    dk = dedup_key(org, kind, model, direction, file)
    with conn:
        row = conn.execute("SELECT id, status FROM tasks WHERE dedup_key=?", (dk,)).fetchone()
        if row is not None:
            if row["status"] not in TERMINAL:
                return row["id"], False
            conn.execute("DELETE FROM tasks WHERE id=?", (row["id"],))
        cur = conn.execute(
            "INSERT INTO tasks(org, dedup_key, kind, model, direction, file, "
            "priority, status, attempts, max_attempts, next_retry_at, created_by, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,0,?,0,?,?)",
            (org, dk, kind, model, direction, file, priority, PENDING, max_attempts,
             created_by, now),
        )
    return cur.lastrowid, True


def get_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def _lease_held(conn: sqlite3.Connection, org: str, model: str, exclude_id: int | None) -> bool:
    """per-model lease: 同 (org, model) 是否存在其他 claimed/running 任务"""
    row = conn.execute(
        "SELECT 1 FROM tasks WHERE org=? AND model=? AND status IN ('claimed','running') "
        "AND (? IS NULL OR id != ?) LIMIT 1",
        (org, model, exclude_id, exclude_id),
    ).fetchone()
    return row is not None


def claim_task(conn: sqlite3.Connection, org: str | None = None) -> sqlite3.Row | None:
    """取一个可执行任务并标记 claimed(原子):
    - 全局或按 org 取; priority DESC, next_retry_at ASC;
    - per-model lease: 同 (org, model) 已有 claimed/running 则跳过(租约键 = (org, model))。
    """
    now = int(time.time())
    with conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status='pending' AND next_retry_at<=:now "
            "AND (:org IS NULL OR org=:org) "
            "ORDER BY priority DESC, next_retry_at ASC LIMIT 50",
            {"now": now, "org": org},
        ).fetchall()
        for row in rows:
            if _lease_held(conn, row["org"], row["model"], row["id"]):
                continue
            conn.execute(
                "UPDATE tasks SET status='claimed', attempts=attempts+1, started_at=?, "
                "progress=NULL WHERE id=?",
                (now, row["id"]),
            )
            return conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
    return None


def mark_running(conn: sqlite3.Connection, task_id: int) -> None:
    with conn:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (RUNNING, task_id))


def complete_task(conn: sqlite3.Connection, task_id: int) -> None:
    with conn:
        conn.execute("UPDATE tasks SET status=?, finished_at=?, progress=NULL WHERE id=?",
                     (SUCCEEDED, int(time.time()), task_id))


def classify_error(err: str) -> bool:
    """True=可重试(网络/5xx/429/超时), False=不可重试(直接 failed)"""
    low = (err or "").lower()
    return not any(h in low for h in _NON_RETRYABLE_HINTS)


def fail_task(conn: sqlite3.Connection, task_id: int, error: str,
              tail_backoff_s: int = 0) -> None:
    """失败处理 —— 三档重启制(不设执行时间上限, 见模块 docstring)。

    attempts 由 claim 时 +1, 即"第几次拉起就失败":
      第 1 次            → WARN + 原位重启(回 pending, next_retry_at=now, 不排到最后);
      第 2..max_attempts-1 次 → WARN + 压到队列尾部等待重启(可加 tail_backoff_s 延迟);
      达到 max_attempts  → ERROR + failed + 告警, 不再拉起, 等人工排查。
    不可重试错误(401/403/404/gated/权限) → 直接 failed + 告警, 不浪费机会。
    """
    task = get_task(conn, task_id)
    if task is None:
        return
    now = int(time.time())
    err = error[:500]
    if not classify_error(error):
        with conn:
            conn.execute("UPDATE tasks SET status=?, finished_at=?, last_error=?, progress=NULL "
                         "WHERE id=?",
                         (FAILED, now, err, task_id))
        insert_alert(conn, task["org"], task_id, task["model"], "critical",
                     f"任务不可重试失败(尝试{task['attempts']}次): {err[:300]}")
        print(f"[tasks] ERROR task={task_id} 不可重试失败, 直接 failed: {err}")
        return
    if task["attempts"] >= task["max_attempts"]:
        with conn:
            conn.execute("UPDATE tasks SET status=?, finished_at=?, last_error=?, progress=NULL "
                         "WHERE id=?",
                         (FAILED, now, err, task_id))
        insert_alert(conn, task["org"], task_id, task["model"], "critical",
                     f"任务连续失败{task['attempts']}次, 停止拉起, 等待人工排查: {err[:300]}")
        print(f"[tasks] ERROR task={task_id} 连续失败{task['attempts']}次, "
              f"停止拉起, 等待人工排查: {err}")
        return
    if task["attempts"] == 1:
        # 第 1 次失败: 原位重启 —— next_retry_at=now, 优先级不变, 不排到最后
        with conn:
            conn.execute("UPDATE tasks SET status=?, next_retry_at=?, last_error=? WHERE id=?",
                         (PENDING, now, err, task_id))
        print(f"[tasks] WARN task={task_id} 第 1 次失败, 原位重启: {err}")
    else:
        # 第 2 次及以后: 压到队列尾部等待重启(同优先级内排最后)
        with conn:
            conn.execute("UPDATE tasks SET status=?, next_retry_at=?, last_error=? WHERE id=?",
                         (PENDING, now + tail_backoff_s, err, task_id))
        print(f"[tasks] WARN task={task_id} 第 {task['attempts']} 次失败, "
              f"压到队列尾部等待重启: {err}")


def set_progress(conn: sqlite3.Connection, task_id: int, text: str) -> None:
    """回写任务执行进度文本(2026-09 可见性): status 直接显示, 不必翻 journal。"""
    with conn:
        conn.execute("UPDATE tasks SET progress=? WHERE id=?", (str(text)[:300], task_id))


def mark_interrupted(conn: sqlite3.Connection, task_id: int, reason: str = "紧急抢占") -> None:
    """紧急抢占/超时杀进程 → interrupted; 重做语义由对账/重做规则处理(Phase 3)"""
    with conn:
        conn.execute("UPDATE tasks SET status=?, last_error=? WHERE id=?",
                     (INTERRUPTED, reason, task_id))


def requeue_interrupted(conn: sqlite3.Connection, task_id: int,
                        reason: str = "紧急抢占重做") -> None:
    """被抢占任务重排回 pending(attempts 不 +1 —— 抢占不消耗失败次数)。

    重做语义(指南 §9.3): 中断的 model_sync 重跑即收敛(ensure_repo 幂等,
    上传服务端哈希去重); 中断于上传阶段且 repo_created_by_us=1 的目标 repo
    删除重建由 transfer.sync_model 的幂等路径覆盖(Phase 3 简化: 不删除重建,
    重传去重; 见 delete_repo_task)。
    """
    with conn:
        conn.execute(
            "UPDATE tasks SET status=?, next_retry_at=0, finished_at=NULL, "
            "last_error=? WHERE id=? AND status=?",
            (PENDING, reason, task_id, INTERRUPTED))


def obsolete_task(conn: sqlite3.Connection, task_id: int, reason: str = "源 repo/文件已不存在") -> None:
    """任务作废(obsolete): 执行前预检/执行中发现源 repo 或文件已消失。

    语义(见 V2-DESIGN.md §4 / 指南 §5.1):
      - 不占失败次数(attempts 不变)、不告警(或由调用方决定 info 级记录);
      - 终态, 崩溃恢复不触碰; 后续收敛交给对账三方对比(删除传播分支)。
    """
    with conn:
        conn.execute("UPDATE tasks SET status=?, finished_at=?, last_error=? WHERE id=?",
                     (OBSOLETE, int(time.time()), reason[:500], task_id))


def crash_recovery(conn: sqlite3.Connection) -> int:
    """启动自检: claimed/running 一律回退 pending。

    ⚠ 指南 §5.3 决策点: 这里故意【不】+1 attempts —— claim 时已 +1,
    恢复再 +1 会造成双计(任务还没真正跑就烧掉重试次数)。
    若你决定恢复也要 +1, 改下面 SQL 加 attempts=attempts+1 并更新注释。
    """
    with conn:
        cur = conn.execute(
            "UPDATE tasks SET status='pending' "
            "WHERE status IN ('claimed','running')",
        )
    return cur.rowcount


def insert_alert(conn: sqlite3.Connection, org: str, task_id: int | None,
                 model: str, level: str, error: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO alerts(org, task_id, model, level, error, created_at) VALUES(?,?,?,?,?,?)",
            (org, task_id, model, level, error[:500], int(time.time())),
        )


def cancel_task(conn: sqlite3.Connection, task_id: int) -> bool:
    with conn:
        cur = conn.execute(
            "UPDATE tasks SET status='failed', finished_at=?, last_error='cancelled' "
            "WHERE id=? AND status IN ('pending','claimed')",
            (int(time.time()), task_id),
        )
    return cur.rowcount > 0


def list_tasks(conn: sqlite3.Connection, org: str | None = None,
               status: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
    sql = "SELECT * FROM tasks WHERE 1=1"
    args: list = []
    if org:
        sql += " AND org=?"
        args.append(org)
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY priority DESC, created_at DESC LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()


def stats(conn: sqlite3.Connection, org: str | None = None) -> dict:
    """按状态统计(可 org 过滤), 供 status 命令"""
    out: dict = {}
    for s in (PENDING, CLAIMED, RUNNING, SUCCEEDED, FAILED, INTERRUPTED, OBSOLETE):
        if org:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE org=? AND status=?", (org, s)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status=?", (s,)).fetchone()
        out[s] = row["n"]
    return out
