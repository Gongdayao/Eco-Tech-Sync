#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同步器 v2 — 顶层入口(可运行的完整骨架)
========================================
模式: daemon / once / enqueue / status / cancel / audit

本文件是"全链路可跑的骨架": 结构、参数、调用点齐全, 业务实现按
V2-IMPLEMENTATION-GUIDE.md 的顺序接入:
  §3 config.py → §4 db.py → §5 tasks.py → §6 task_runner.py → §7 本文件
  → §8 reconcile.py → §9 transfer.py → §10 alerts.py → §11 pipeline.py → §12 gitcode.py

当前各业务函数均为占位返回(见各模块 docstring), 但状态机全链路可跑:
  enqueue 入队 → daemon/once 派生子进程 → task_runner 执行占位逻辑 → 状态回写。

用法:
  python server-work.py --help
  python server-work.py status [--org O] [--json] [--watch [N]]
  python server-work.py alerts [--org O] [--level warn|critical] [--since H] [--limit N] [--json]
  python server-work.py enqueue --org Eco-Tech --model xxx --direction to_modelers [--urgent|--queue]
  python server-work.py once [--org O]
  python server-work.py daemon            # 无参启动默认本模式(兼容 v1 run.sh)
  python server-work.py cancel --task-id N
  python server-work.py audit [--org O] [--force]   # 默认受 15min 节流; --force 强制执行
  python server-work.py clean --org O --model X [--yes]   # 清理魔乐独有文件(默认 dry-run)
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config.yaml")

# ---------------------------------------------------------------- 环境引导
# 必须先于任何可能触达 SDK 的 import(逻辑在 env_tools/env_bootstrap.py, 叶子模块)。
sys.path.insert(0, PROJECT_ROOT)              # 保证 import env_tools.* 可用(与 CWD 无关)
from env_tools.env_bootstrap import bootstrap_env  # noqa: E402
bootstrap_env()

import argparse  # noqa: E402
import json      # noqa: E402
import re        # noqa: E402
import signal    # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time      # noqa: E402

from collections import deque  # noqa: E402

from env_tools import db, tasks  # noqa: E402
from env_tools import runlog  # noqa: E402
from env_tools.config import load_config, get_org  # noqa: E402

# 全局运行标志(SIGTERM/SIGINT 置 False → 优雅退出)
_running = True

# 说明: 不设"任务最长执行时间" —— 权重越来越大(2.4T 千兆满速也要 5.6h+),
# 固定超时不可控; 失败/重启次数由 tasks.fail_task 的三档政策控制(见 tasks.py docstring)。

# 空队列轮询 / 任务间停顿(秒)
POLL_IDLE_S = 30
POLL_BETWEEN_TASKS_S = 15


# ---------------------------------------------------------------- 初始化
def _flatten(defaults: dict, prefix: str = "") -> dict:
    """把 config.yaml 的分段配置压平成 app_config 键值(如 sync.model_interval_min)"""
    out: dict = {}
    for k, v in defaults.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def _init(config_path: str | None):
    """加载配置 + 初始化 DB + 种子化 app_config → (raw_cfg, orgs, conn)

    种子化来源(config.yaml sync/transfer/retry/alert 段, 见指南 §13):
    启动即需的进 config.yaml, 运行期可调的进 DB(app_config, 种子化后可改,
    INSERT OR IGNORE 保证运行期修改不被重启覆盖)。
    """
    raw_cfg, orgs = load_config(config_path)
    db.migrate()
    conn = db.get_conn()
    for org in orgs:
        defaults = {}
        for section in ("sync", "transfer", "retry", "alert"):
            defaults.update(_flatten(raw_cfg.get(section, {}) or {}, section))
        db.seed_app_config(conn, org.id, defaults)
    return raw_cfg, orgs, conn


# ---------------------------------------------------------------- worker 子进程
class _OutputPump(threading.Thread):
    """子进程 stdout 泵(2026-09): 实时回显 + 进度节流 + 防管道写满阻塞。

    背景: 旧实现"退出后一次读完回显尾部 20 行" → 长任务(小时级大权重传输)期间
    journal 无任何输出, 且 tqdm 进度刷屏会把 64KB 管道写满 → 子进程写阻塞
    (下载看似卡死)。泵线程常驻读管道, 永不阻塞子进程:
      - 普通日志行(\\n 结尾)→ 排队, tick() 时实时回显(带 "  | " 前缀);
      - 进度条行(\\r 结尾, 含 %| / it/s / B/s / s/it)→ 只留最新一条, ≥8s 节流回显;
      - 全程静默 >120s → 打运行心跳, 便于区分"长任务"与"真卡死"。
    泵线程不碰 DB, 只 print; 子进程退出(EOF)后线程自然结束。
    """
    _PROGRESS_RE = re.compile(r"%\||it/s|B/s|s/it")

    def __init__(self, proc: subprocess.Popen, task_id: int):
        super().__init__(daemon=True, name=f"outpump-{task_id}")
        self.proc = proc
        self.task_id = task_id
        self._buf = bytearray()
        self._log_q: deque[str] = deque()
        self._progress: str | None = None
        self._t0 = time.time()
        self._last_any = self._t0
        self._last_progress_emit = 0.0

    # ---- 泵线程主体: 持续读管道(防写满阻塞), 拆 \n/\r 行 ----
    def run(self) -> None:
        f = self.proc.stdout
        while True:
            chunk = f.read(65536) if f else b""
            if not chunk:                      # EOF: 子进程退出/被杀
                break
            self._buf.extend(chunk)
            self._split()
        self._split(final=True)                # 清残余

    def _split(self, final: bool = False) -> None:
        while True:
            i = -1
            for sep in (b"\n", b"\r"):
                j = self._buf.find(sep)
                if j != -1 and (i == -1 or j < i):
                    i, found = j, sep
            if i == -1:
                if final and self._buf:
                    self._feed(bytes(self._buf), b"\n")
                    self._buf.clear()
                return
            line = bytes(self._buf[:i])
            del self._buf[:i + len(found)]
            self._feed(line, found)

    def _feed(self, line: bytes, sep: bytes) -> None:
        text = line.decode(errors="replace").strip()
        if not text:
            return
        if sep == b"\r" or self._PROGRESS_RE.search(text):
            self._progress = text              # 进度: 只留最新
        else:
            self._log_q.append(text)
            if len(self._log_q) > 1000:        # 防异常刷屏撑爆内存
                self._log_q.popleft()

    # ---- 主循环每 ~2s 调用: 回显日志 / 节流进度 / 静默心跳 ----
    def tick(self, final: bool = False) -> None:
        now = time.time()
        while self._log_q:
            print(f"  | {self._log_q.popleft()}", flush=True)
            self._last_any = now
        if self._progress and (now - self._last_progress_emit >= 8 or final):
            print(f"  | … {self._progress[:140]}", flush=True)
            self._last_progress_emit = now
            self._last_any = now
            self._progress = None
        if not final and now - self._last_any > 120:
            print(f"[worker] task #{self.task_id} 运行中, 已耗时 "
                  f"{int(now - self._t0)}s(静默 120s, 进程仍在传输)", flush=True)
            self._last_any = now


def _run_worker(conn, task) -> int:
    """spawn env_tools.task_runner 子进程执行一个任务; 返回子进程退出码。

    状态由子进程自己回写(succeeded/failed); 本函数只负责:
      - 等待子进程结束 —— 不设时间上限(大权重传输可能远超 6h);
      - **紧急抢占**: 等待期间每 2s 检查 priority=100 的 pending 任务,
        若与当前任务不同模型 → terminate 当前 worker → 当前任务
        interrupted + 重排(requeue_interrupted, 不占失败次数) → 返回 -3,
        主循环下一轮自然 claim 到紧急任务(priority DESC 排最前);
      - 子进程异常退出(被外部杀死/崩溃, 未回写状态) → 按一次失败计
        (由 tasks.fail_task 三档政策决定重启/停止);
      - 子进程输出实时回显(_OutputPump: 日志实时, 进度节流 8s, 静默心跳 120s)。
    """
    cmd = [sys.executable, "-m", "env_tools.task_runner", str(task["id"])]
    print(f"[worker] spawn task #{task['id']}: {task['org']} {task['kind']} "
          f"{task['model']} {task['direction'] or ''}")
    try:
        proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        pump = _OutputPump(proc, task["id"])
        pump.start()
        # 轮询等待(可抢占); 输出由泵线程持续读取回显
        while True:
            try:
                rc = proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                pump.tick()
                if not _running:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    pump.tick(final=True)
                    return -4                    # daemon 退出中
                urgent = conn.execute(
                    "SELECT id, model FROM tasks WHERE priority=? AND status=? "
                    "AND next_retry_at<=? ORDER BY created_at ASC LIMIT 1",
                    (tasks.PRIORITY_URGENT_PREEMPT, tasks.PENDING, int(time.time()))
                ).fetchone()
                if urgent is not None and urgent["model"] != task["model"]:
                    print(f"[worker] 紧急任务 #{urgent['id']} {urgent['model']} "
                          f"抢占当前 task #{task['id']} {task['model']}")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    pump.tick(final=True)
                    cur = conn.execute("SELECT status FROM tasks WHERE id=?",
                                       (task["id"],)).fetchone()
                    if cur is not None and cur["status"] in tasks.ACTIVE:
                        tasks.mark_interrupted(conn, task["id"], "紧急抢占")
                        tasks.requeue_interrupted(conn, task["id"])
                        print(f"[worker] task #{task['id']} → interrupted + 重排(抢占不消耗次数)")
                    else:
                        print(f"[worker] task #{task['id']} 被抢占但已自行收尾(status={cur['status'] if cur else '?'})")
                    return -3
        pump.tick(final=True)
        if rc != 0:
            cur = conn.execute("SELECT status FROM tasks WHERE id=?",
                               (task["id"],)).fetchone()
            if cur is not None and cur["status"] in tasks.ACTIVE:
                # worker 被外部杀死/崩溃, 没来得及回写状态 → 计一次失败
                err = f"worker 异常退出(exit={rc}), 未回写状态"
                tasks.fail_task(conn, task["id"], err)
                print(f"[worker] task #{task['id']} {err} → 按失败计")
        return rc
    except Exception as e:
        print(f"[worker] 子进程启动失败: {type(e).__name__}: {e}")
        return -2


# ---------------------------------------------------------------- 信号与睡眠
def _install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)


def _on_signal(signum, frame) -> None:
    global _running
    _running = False
    print(f"[daemon] 收到信号 {signum}, 正在优雅退出...")


def _interruptible_sleep(seconds: float) -> None:
    """分段睡眠, 每 0.5s 检查 _running → SIGTERM 响应延迟 ≤0.5s(指南 §7.2)"""
    deadline = time.time() + seconds
    while _running and time.time() < deadline:
        time.sleep(min(0.5, deadline - time.time()))


# ---------------------------------------------------------------- 命令实现
def cmd_daemon(args) -> int:
    """常驻进程(无参启动默认进入本模式, 兼容 v1 run.sh)。

    主循环(指南 §7.2):
      crash_recovery → 循环: scheduler_tick(对账占位) → claim_task → spawn 子进程
      → 等待(无时间上限) → 异常退出按失败计 → 心跳; 空队列可中断睡眠轮询;
      SIGTERM/SIGINT 优雅退出。
    """
    from env_tools import reconcile

    raw_cfg, orgs, conn = _init(args.config)
    n = tasks.crash_recovery(conn)
    if n:
        print(f"[daemon] 崩溃恢复: {n} 个遗留 claimed/running 任务回退 pending")
    _install_signal_handlers()
    pid = os.getpid()
    db.touch_heartbeat(conn, pid)
    print(f"[daemon] 启动 pid={pid} orgs={[o.id for o in orgs]}")

    while _running:
        # 先取任务(响应优先): 队列非空时先干活, 对账只在空队列空闲时跑
        task = tasks.claim_task(conn, org=args.org)
        if task is not None:
            db.touch_heartbeat(conn, pid, last_task_at=int(time.time()))
            code = _run_worker(conn, task)
            if code == -3:
                continue                      # 被紧急抢占 → 立即回到取任务(紧急优先)
            if _running:
                _interruptible_sleep(POLL_BETWEEN_TASKS_S)  # 任务间停顿
            continue
        # 空队列 → 对账调度(节流 15min, 首轮全量采纳约 1-2 分钟, 属预期)
        try:
            t_rc = time.time()
            for r in reconcile.scheduler_tick(conn, orgs):
                if r.get("skipped"):
                    print(f"[daemon] 对账节流跳过 org={r.get('org')}")
                elif "error" in r:
                    print(f"[daemon] 对账异常 org={r.get('org')}: {r['error']}")
                else:
                    m = r.get("model", {}) or {}
                    q = r.get("queue", {}) or {}
                    f = r.get("file", {}) or {}
                    rh = r.get("rehash", {}) or {}
                    print(f"[daemon] 对账完成 org={r.get('org')} 耗时 {time.time() - t_rc:.0f}s | "
                          f"模型级: 新增→魔乐={m.get('to_modelers')} 反向={m.get('to_scope')} "
                          f"删除={m.get('repo_delete')} 采纳={m.get('adopted')} | "
                          f"队列生成(仅 model_list): 在管={q.get('managed')} 变动={q.get('dirty')} "
                          f"入队={q.get('enq')} 已一致={q.get('clean')} "
                          f"跳过(已有任务)={q.get('skip_pending')} | "
                          f"文件级[{f.get('mode', '-')}]: 检查={f.get('checked')} "
                          f"上传={f.get('to_correct')} 待删={f.get('to_delete')} "
                          f"独有={f.get('extra')} 入队={f.get('file_batch_enq')} "
                          f"已核验回写={f.get('verified')} | "
                          f"强哈希: 核对={rh.get('checked')} 不一致={rh.get('mismatch')} "
                          f"失败={rh.get('failed')} 完成={rh.get('done')}"
                          + (f" 跳过={rh.get('skipped_reason')}" if rh.get("skipped_reason") else ""))
        except Exception as e:
            print(f"[daemon] scheduler_tick 异常: {type(e).__name__}: {e}")
        db.touch_heartbeat(conn, pid)
        _interruptible_sleep(POLL_IDLE_S)      # 空队列轮询

    print("[daemon] 优雅退出")
    return 0


def cmd_once(args) -> int:
    """跑一轮全部 pending 任务后退出(替代 v1 static_work 的定位)。

    逻辑: crash_recovery → while claim_task(org=args.org): spawn 子进程执行。
    """
    raw_cfg, orgs, conn = _init(args.config)
    n = tasks.crash_recovery(conn)
    if n:
        print(f"[once] 崩溃恢复: {n} 个遗留任务回退 pending")
    count = 0
    while True:
        task = tasks.claim_task(conn, org=args.org)
        if task is None:
            break
        code = _run_worker(conn, task)
        count += 1
        if code == 2:      # 任务不存在(理论不发生), 停止避免死循环
            print(f"[once] 子进程报告任务不存在, 停止")
            break
    print(f"[once] 完成, 共执行 {count} 个任务")
    return 0


def cmd_enqueue(args) -> int:
    """手动入队(替代 v1 single_sync 的定位)。

    参数: --org(必填) --model(必填) --kind --direction --file
          --urgent(priority=100, 紧急抢占) / --queue(priority=10, 普通插队)
    去重: dedup_key = f"{org}:{kind}:{model}:{direction or '*'}:{file or '*'}"
    """
    raw_cfg, orgs, conn = _init(args.config)
    org = get_org(orgs, args.org)
    if org is None:
        print(f"[enqueue] 组织 '{args.org}' 不存在(配置中: {[o.id for o in orgs]})")
        return 2
    kind = args.kind or tasks.KIND_MODEL_SYNC
    if kind not in (tasks.KIND_MODEL_SYNC, tasks.KIND_FILE_BATCH,
                    tasks.KIND_GITCODE_IMPORT, tasks.KIND_REPO_DELETE):
        print(f"[enqueue] kind 非法: {kind}(可选: model_sync/file_batch/gitcode_import/repo_delete)")
        return 2
    if args.direction not in (None, "to_modelers", "to_scope"):
        print(f"[enqueue] direction 非法: {args.direction}(可选: to_modelers/to_scope)")
        return 2
    if args.urgent and args.queue:
        print("[enqueue] --urgent 与 --queue 不能同时使用")
        return 2
    priority = (tasks.PRIORITY_URGENT_PREEMPT if args.urgent
                else tasks.PRIORITY_URGENT_QUEUE if args.queue
                else tasks.PRIORITY_NORMAL)
    task_id, created = tasks.enqueue_task(
        conn, org.id, kind, args.model,
        direction=args.direction, file=args.file,
        priority=priority, created_by="manual")
    print(f"[enqueue] {'新建' if created else '已存在(去重, 不重复入队)'} "
          f"task_id={task_id} org={org.id} kind={kind} model={args.model} "
          f"direction={args.direction or '-'} priority={priority}")
    return 0


def cmd_alerts(args) -> int:
    """查看告警(默认最近 24h, 倒序); --org/--level/--since/--limit/--json。

    分类标签: 审计留痕 / 需人工 / 任务失败 / 提醒(见 env_tools.alerts.classify_alert)。
    """
    from env_tools import alerts as alert_mod

    raw_cfg, orgs, conn = _init_for_output(args)
    since_h = args.since if args.since is not None else 24
    limit = args.limit if args.limit is not None else 20
    rows = alert_mod.list_alerts(conn, org=args.org, level=args.level,
                                 since_h=since_h, limit=limit)

    if args.json:
        out = {"since_h": since_h,
               "alerts": [dict(r) for r in rows]}
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0

    since = int(time.time()) - since_h * 3600
    counts = conn.execute(
        "SELECT level, COUNT(*) n FROM alerts WHERE created_at>=? "
        "AND (? IS NULL OR org=?) GROUP BY level",
        (since, args.org, args.org)).fetchall()
    summary = " ".join(f"{r['level']}={r['n']}" for r in counts) or "(无)"
    print(f"告警(最近{since_h}h, org={args.org or '全部'}): {summary} | 显示 {len(rows)} 条")
    if not rows:
        return 0
    for r in rows:
        tag = alert_mod.classify_alert(r["error"], r["level"])
        ts = time.strftime("%m-%d %H:%M", time.localtime(r["created_at"]))
        print(f"  #{r['id']} [{ts}] {r['level']:>8} [{tag}] org={r['org']} "
              f"model={r['model'] or '-'} task={r['task_id'] or '-'}")
        print(f"      {(r['error'] or '')[:150]}")
    return 0


def _init_for_output(args):
    """初始化; --json 模式下把初始化期间的诊断(stdout)临时改道 stderr, 保证
    stdout 是纯 JSON(否则 [runlog]/[警告] 等行会污染 `--json | json.tool`)。"""
    if getattr(args, "json", False):
        real = sys.stdout
        sys.stdout = sys.stderr
        try:
            return _init(args.config)
        finally:
            sys.stdout = real
    return _init(args.config)


def _render_status(conn, orgs, args) -> None:
    """打印一次状态快照(status 单次与 --watch 复用)。"""
    st = tasks.stats(conn, args.org)
    hb = conn.execute("SELECT * FROM heartbeat WHERE id=1").fetchone()
    rows = tasks.list_tasks(conn, org=args.org, status=args.status,
                            limit=args.limit or 100)
    targets = [o for o in orgs if not args.org or o.id == args.org]

    if args.json:
        out = {
            "stats": st,
            "heartbeat": dict(hb) if hb else None,
            "runtime": {o.id: {"reconcile": db.get_app_config(
                conn, o.id, "sync.reconcile_progress"),
                "rehash": db.get_app_config(conn, o.id, "sync.rehash_progress")}
                for o in targets},
            "tasks": [dict(r) for r in rows],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return

    print(f"任务统计(--org={args.org or '全部'}): "
          + " ".join(f"{k}={v}" for k, v in st.items()))
    now = int(time.time())
    if hb:
        stale = now - (hb["last_cycle_at"] or 0)
        print(f"心跳: last_cycle_at={hb['last_cycle_at']}({stale}s 前) "
              f"last_task_at={hb['last_task_at']} pid={hb['pid']}")
    for o in targets:
        rp = db.get_app_config(conn, o.id, "sync.reconcile_progress")
        hp = db.get_app_config(conn, o.id, "sync.rehash_progress")
        if rp or hp:
            print(f"运行状态[{o.id}]: 对账={rp or '-'} | 强哈希={hp or '-'}")
    print(f"最近任务({len(rows)} 行):")
    for r in rows:
        line = (f"  #{r['id']} [{r['status']:>10}] prio={r['priority']:>3} "
                f"attempts={r['attempts']}/{r['max_attempts']} {r['org']} "
                f"{r['kind']} {r['model']} {r['direction'] or ''} {r['file'] or ''}")
        if r["status"] == "running":
            if r["started_at"]:
                line += f" 已耗时={int((now - r['started_at']) / 60)}min"
            if r["progress"]:
                line += f" 进度: {r['progress']}"
        if r["last_error"]:
            line += f" err={r['last_error']}"
        print(line[:240])


def cmd_status(args) -> int:
    """队列/任务/心跳/运行进度摘要; --org 过滤; --json; --watch [N] 实时刷新。"""
    raw_cfg, orgs, conn = _init_for_output(args)
    if not args.watch:
        _render_status(conn, orgs, args)
        return 0
    try:
        while True:
            print("\033[2J\033[H", end="")          # 清屏重画(类似 top)
            _render_status(conn, orgs, args)
            print(f"\n(每 {args.watch}s 刷新; Ctrl+C 退出)", flush=True)
            sys.stdout.flush()
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0


def cmd_cancel(args) -> int:
    """取消 pending/claimed 任务(--task-id)。"""
    raw_cfg, orgs, conn = _init(args.config)
    ok = tasks.cancel_task(conn, args.task_id)
    print(f"[cancel] task {args.task_id}: {'已取消' if ok else '不可取消(仅 pending/claimed 可取消)'}")
    return 0 if ok else 1


def cmd_audit(args) -> int:
    """触发全量对账/强哈希(Phase 2 实现: 调度层 org 循环)。

    默认与 daemon 共用 `sync.last_reconcile` 15min 节流(避免手动 audit 与常驻轮次
    重复拉双侧文件树); `--force` 忽略节流强制执行(2026-09-14)。
    """
    from env_tools import reconcile

    raw_cfg, orgs, conn = _init(args.config)
    targets = [get_org(orgs, args.org)] if args.org else orgs
    force = bool(getattr(args, "force", False))
    rc = 0
    for org in targets:
        if org is None:
            print(f"[audit] 组织 '{args.org}' 不存在(配置中: {[o.id for o in orgs]})")
            return 2
        result = reconcile.full_audit(conn, org, force=force)
        if result.get("skipped"):
            print(f"[audit] {org.id}: 被 15min 对账节流跳过(距上次对账不足节流窗口); "
                  f"如需强制执行请加 --force")
        print(f"[audit] {org.id}: {result}")
    return rc


def cmd_clean(args) -> int:
    """删除某模型在魔乐的"独有文件"(魔塔当前文件集没有的 path) —— 人工确认执行入口。

    背景: `delete_manual` 告警列出的魔乐独有文件(旧版本残留等)默认不自动删
    (误删事故保护)。本命令把"人工确认"落地为可审计的一次操作:
      - 默认 dry-run: 只列清单(路径 + 大小 + 合计), 不执行;
      - `--yes` 才执行: 一次 commit 批量删除 + 清双侧 DB 行 + 留 audit_delete_file 审计。
    安全: 魔塔文件集拉取失败/为空(abort) → 拒绝执行(防误删风暴); 隐藏/毒瘤永不删。
    ⚠ 建议先等该模型的 file_batch(魔塔新版上传)成功后再执行, 避免出现"缺文件"窗口。
    """
    from env_tools import poison as _poison
    from env_tools import transfer
    from env_tools.reconcile import compute_file_diff

    raw_cfg, orgs, conn = _init(args.config)
    org = get_org(orgs, args.org)
    if org is None:
        print(f"[clean] 组织 '{args.org}' 不存在(配置中: {[o.id for o in orgs]})")
        return 2
    model = args.model
    repo_m = f"{org.modelers.repo_name}/{model}"

    d = compute_file_diff(conn, org, model)
    if d["abort"]:
        print(f"[clean] 拒绝执行: 魔塔文件集拉取失败/为空({model}), 防误删")
        return 1
    if d["adopt"]:
        # 首见(双侧无基线): 采纳只写基线不算差异 → 再算一轮取真实 diff
        print(f"[clean] {model}: 首次见(已采纳建基线), 重算差异 ...")
        d = compute_file_diff(conn, org, model)
        if d["abort"]:
            print(f"[clean] 拒绝执行: 魔塔文件集拉取失败/为空({model}), 防误删")
            return 1

    items: list[tuple[str, int]] = []
    for p in sorted(d["extra"]):
        if _poison.is_excluded(_poison.classify(p)):
            continue                      # 隐藏/毒瘤: 永不删
        row = conn.execute(
            "SELECT size FROM files WHERE org=? AND platform='modelers' AND repo_id=? AND path=?",
            (org.id, repo_m, p)).fetchone()
        items.append((p, (row["size"] if row else 0) or 0))
    if not items:
        print(f"[clean] {model}: 无魔乐独有文件(已与魔塔对齐)")
        return 0

    total = sum(s for _, s in items)
    print(f"[clean] {model}: 魔乐独有 {len(items)} 个文件, 合计 {total / 1e9:.2f} GB")
    for p, s in items[:50]:
        print(f"   - {p}  ({s / 1e6:.1f} MB)")
    if len(items) > 50:
        print(f"   ... 其余 {len(items) - 50} 个省略")
    print("[clean] 以上路径在魔塔当前文件集中不存在; 请确认是旧版本残留(而非需保留的人工内容)。")
    if not args.yes:
        print("[clean] dry-run: 未执行删除; 确认无误后加 --yes 执行(一次 commit 批量删除)")
        return 0
    print(f"[clean] 执行删除 {len(items)} 个文件 ...")
    res = transfer._delete_files(conn, org, "modelers", model, [p for p, _ in items])
    print(f"[clean] 完成: 删除 {len(res.get('deleted', []))} 个文件(一次 commit); "
          f"双侧 DB 行已清; 已留 audit_delete_file 审计告警")
    return 0


# ---------------------------------------------------------------- 入口
def main() -> int:
    ap = argparse.ArgumentParser(description="同步器 v2(多组织) — 可运行骨架")
    ap.add_argument("mode", nargs="?", default="daemon",
                    choices=["daemon", "once", "enqueue", "status", "alerts",
                             "cancel", "audit", "clean"],
                    help="默认 daemon(兼容 run.sh 无参启动)")
    ap.add_argument("--config", default=None, help=f"配置文件(默认 {DEFAULT_CONFIG})")
    # 通用
    ap.add_argument("--org", default=None, help="组织 id(默认全部)")
    # enqueue / clean
    ap.add_argument("--model", default=None, help="enqueue/clean: 模型名")
    ap.add_argument("--kind", default=None, help="enqueue: model_sync/file_batch/gitcode_import/repo_delete")
    ap.add_argument("--direction", default=None, help="enqueue: to_modelers/to_scope")
    ap.add_argument("--file", default=None, help="enqueue: 单文件(预留)")
    ap.add_argument("--urgent", action="store_true", help="enqueue: 紧急插队(priority=100)")
    ap.add_argument("--queue", action="store_true", help="enqueue: 普通插队(priority=10)")
    # status / alerts / cancel
    ap.add_argument("--status", default=None, help="status: 按任务状态过滤")
    ap.add_argument("--limit", type=int, default=None,
                    help="status/alerts: 最大行数(status 默认 100, alerts 默认 20)")
    ap.add_argument("--level", default=None, choices=["warn", "critical"],
                    help="alerts: 只看 warn/critical")
    ap.add_argument("--since", type=int, default=None,
                    help="alerts: 最近 N 小时(默认 24)")
    ap.add_argument("--task-id", type=int, default=None, help="cancel: 任务 id")
    ap.add_argument("--force", action="store_true",
                    help="audit: 忽略 15min 对账节流强制执行(默认受节流; 仅 audit 使用)")
    ap.add_argument("--yes", action="store_true",
                    help="clean: 真正执行删除(默认 dry-run 只列清单)")
    ap.add_argument("--watch", type=int, nargs="?", const=5, default=None,
                    help="status: 每 N 秒实时刷新(默认 5s, Ctrl+C 退出)")
    ap.add_argument("--json", action="store_true", help="status/alerts: JSON 输出")
    args = ap.parse_args()

    # 每日日志导出(2026-09, 对齐 v1): stdout 同时追加写 <项目根>/log/<YYYY-MM-DD>.log
    # (跨零点自动换文件; 目录不可写只告警不影响运行; 可用 SYNC_LOG_DIR 覆盖目录)
    if not (args.mode == "status" and args.watch):
        runlog.install(os.environ.get("SYNC_LOG_DIR") or os.path.join(PROJECT_ROOT, "log"))

    handlers = {
        "daemon": cmd_daemon,
        "once": cmd_once,
        "enqueue": cmd_enqueue,
        "status": cmd_status,
        "alerts": cmd_alerts,
        "cancel": cmd_cancel,
        "audit": cmd_audit,
        "clean": cmd_clean,
    }
    if args.mode == "enqueue" and (not args.org or not args.model):
        ap.error("enqueue 必填: --org 与 --model")
    if args.mode == "clean" and (not args.org or not args.model):
        ap.error("clean 必填: --org 与 --model")
    if args.mode == "cancel" and not args.task_id:
        ap.error("cancel 必填: --task-id")
    try:
        return handlers[args.mode](args)
    except NotImplementedError as e:
        print(f"[骨架] {e}")
        return 2
    except Exception as e:
        print(f"[错误] {args.mode} 执行异常: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
