# -*- coding: utf-8 -*-
"""env_tools.alerts — 告警**查询与分类**(落库与 webhook 推送统一在 tasks.insert_alert)

2026-10-08 减法: 删除 `dispatch_webhook`/`_push`/`daily_summary`/`heartbeat_check`/
`notify_urgent_failure` —— 前两者无人调用(配了 ALERT_WEBHOOK_URL 也不会推)，是"文档承诺
但没接线"; 后三者(每日摘要/心跳检查/紧急任务即时告警)从未接入任何入口。webhook 现由
`tasks.insert_alert` 统一推送(未配置 → 只落库), 心跳状态直接看 `status`。

告警类型盘点(2026-09, 供 alerts CLI 分类展示):
  审计留痕(audit_delete/audit_delete_file/audit_clear)  — 同步器操作记录, 无需处理;
  需人工(delete_manual/vis_mismatch/gated_public_on_modelers/gated_converted/
         dual_upload/scope_delete_manual/rehash_mismatch) — 需人工到平台处理;
  任务失败(不可重试/连续失败N次/[紧急任务失败], critical, 带 task_id);
  提醒(GitCode 镜像导入 → 开启 pull 等)。
"""
from __future__ import annotations

import sqlite3
import time


def classify_alert(error: str | None, level: str) -> str:
    """告警分类标签(展示用): 审计留痕 / 需人工 / 任务失败 / 提醒。"""
    err = error or ""
    head = err.split(":", 1)[0].strip()
    if head in ("audit_delete", "audit_delete_file", "audit_clear"):
        return "审计留痕"
    if head in ("delete_manual", "vis_mismatch", "gated_public_on_modelers",
                "gated_converted", "dual_upload", "scope_delete_manual",
                "rehash_mismatch"):
        return "需人工"
    if level == "critical" or "失败" in err or "紧急" in err:
        return "任务失败"
    return "提醒"


def list_alerts(conn: sqlite3.Connection, org: str | None = None,
                level: str | None = None, since_h: int = 24,
                limit: int = 20) -> list[sqlite3.Row]:
    """查询 alerts(倒序): --org/--level/--since 过滤, limit 截断。"""
    sql = "SELECT * FROM alerts WHERE 1=1"
    args: list = []
    if org:
        sql += " AND org=?"
        args.append(org)
    if level:
        sql += " AND level=?"
        args.append(level)
    if since_h and since_h > 0:
        sql += " AND created_at>=?"
        args.append(int(time.time()) - int(since_h) * 3600)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()
