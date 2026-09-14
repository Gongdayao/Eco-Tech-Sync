# -*- coding: utf-8 -*-
"""env_tools.alerts — 告警: 落库(alerts 表) + webhook 推送 + 每日摘要 + 心跳检查

触发点(指南 §10): 任务 failed(不可重试)、三档耗尽、紧急任务首次失败、
磁盘不足、心跳超时、GitCode 镜像 pull 未开启(每日提醒)。
webhook URL 读 app_config 'alert.webhook_url'(config.yaml ${ALERT_WEBHOOK_URL} 种子化,
未配置 → 只落库不推送); 推送失败降级只落库, 不重试。

告警类型盘点(2026-09, 供 alerts CLI 分类展示):
  审计留痕(audit_delete/audit_delete_file/audit_clear)  — 同步器操作记录, 无需处理;
  需人工(delete_manual/vis_mismatch/gated_public_on_modelers/gated_converted/
         dual_upload/scope_delete_manual/rehash_mismatch) — 需人工到平台处理;
  任务失败(不可重试/连续失败N次/[紧急任务失败], critical, 带 task_id);
  提醒(GitCode 镜像导入 → 开启 pull 等)。
"""
from __future__ import annotations

import json
import sqlite3
import time

import requests

from env_tools import db, tasks


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


def dispatch_webhook(conn, org_id: str, level: str, error: str, task_id: int | None = None,
                     model: str | None = None) -> bool:
    """告警落库 + webhook 推送(全局默认 URL, per-org 覆盖, 读 app_config)。"""
    tasks.insert_alert(conn, org_id, task_id, model, level, error)
    url = db.get_app_config(conn, org_id, "alert.webhook_url", "") or ""
    if not url:
        return False
    return _push(url, {"org": org_id, "level": level, "error": error[:500],
                       "task_id": task_id, "model": model,
                       "ts": int(time.time())})


def _push(url: str, payload: dict) -> bool:
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code < 400
    except Exception:
        return False


def daily_summary(conn, orgs) -> dict:
    """每日摘要: 按 org 分组统计 24h 任务/告警(供告警 webhook 或日志输出)。"""
    since = int(time.time()) - 86400
    out = {}
    for org in orgs:
        t = conn.execute(
            "SELECT status, COUNT(*) n FROM tasks WHERE org=? AND created_at>=? "
            "GROUP BY status", (org.id, since)).fetchall()
        a = conn.execute(
            "SELECT level, COUNT(*) n FROM alerts WHERE org=? AND created_at>=? "
            "GROUP BY level", (org.id, since)).fetchall()
        out[org.id] = {"tasks": {r["status"]: r["n"] for r in t},
                       "alerts": {r["level"]: r["n"] for r in a}}
    return out


def heartbeat_check(conn, threshold_s: int = 3600) -> dict:
    """心跳检查: 读 heartbeat 表, 超过阈值返回告警建议(供外部 cron/systemd timer)。"""
    hb = conn.execute("SELECT * FROM heartbeat WHERE id=1").fetchone()
    if hb is None:
        return {"ok": False, "reason": "无心跳记录(daemon 未运行过?)"}
    age = int(time.time()) - hb["last_cycle_at"]
    return {"ok": age <= threshold_s, "age_s": age}


def notify_urgent_failure(conn, org_id: str, task_id: int, model: str, error: str) -> None:
    """紧急任务(priority=100)失败: 立即告警(不等三档耗尽), 重试节奏 30s→2min→10min。"""
    dispatch_webhook(conn, org_id, "critical", f"[紧急任务失败] {error}",
                     task_id=task_id, model=model)
