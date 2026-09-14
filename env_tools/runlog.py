# -*- coding: utf-8 -*-
"""env_tools.runlog — 每日日志文件导出(2026-09 用户需求, 对齐 v1 行为)

背景: daemon/CLI 全部使用 print 输出(systemd 下进 journald); 需要像 v1 一样
按天留档到 ./log/ 目录, 文件名为日期(如 log/2026-09-07.log)。

做法: 安装一个 Tee 包住 sys.stdout —— 原样写终端/journald, 同时**逐行加时间戳**
(`[YYYY-MM-DD HH:MM:SS] `)追加写当日文件;
跨零点自动切到新日期文件(写时判断日期, 无需定时任务/日志框架重构)。
多线程安全(daemon 的输出泵线程也会 print)。

失败降级: 日志目录/文件不可写 → 打一条警告后只输出到 journald, 绝不影响同步主流程。
子进程(task_runner)的 stdout/stderr 被 daemon 泵线程捕获后回显 → 同样会进日志文件。

叶子模块(仅标准库), 不 import 任何 SDK / env_tools 其他模块。
"""
from __future__ import annotations

import os
import sys
import threading
import time


class DailyFileTee:
    """stdout 分流器: 终端/journald + 当日日志文件(跨零点自动换)。

    文件侧**逐行加时间戳** `[YYYY-MM-DD HH:MM:SS] `(2026-09: 导出的日志无时间戳
    难以溯源); stdout/journald 保持原样 —— journald 自带时间戳, 避免双时间戳。
    按行缓冲: print 的片段可能不含换行, 先累计到完整行再落盘。
    """

    _TS_FMT = "%Y-%m-%d %H:%M:%S"
    _PENDING_MAX = 8192               # 无换行的超长片段(异常情况)强制落盘

    def __init__(self, stream, log_dir: str):
        self._stream = stream
        self._dir = log_dir
        self._lock = threading.Lock()
        self._fh = None
        self._day: str | None = None
        self._warned = False
        self._pending = ""

    # ---- 内部: 按天滚动打开文件 ----
    def _roll_locked(self) -> None:
        day = time.strftime("%Y-%m-%d")
        if self._fh is not None and day == self._day:
            return
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        self._fh = open(os.path.join(self._dir, f"{day}.log"), "a", encoding="utf-8")
        self._day = day

    def _emit_locked(self, line: str) -> None:
        self._fh.write(f"[{time.strftime(self._TS_FMT)}] {line.rstrip(chr(13))}\n")
        self._fh.flush()                  # 逐条落盘: journald 与文件进度一致

    def write(self, data):
        try:
            self._stream.write(data)      # 终端/journald: 原样(自带时间戳)
        except Exception:
            pass
        try:
            with self._lock:
                self._roll_locked()
                self._pending += data
                while True:
                    idx = self._pending.find("\n")
                    if idx < 0:
                        break
                    line = self._pending[:idx]
                    self._pending = self._pending[idx + 1:]
                    self._emit_locked(line)
                if len(self._pending) > self._PENDING_MAX:
                    self._emit_locked(self._pending)
                    self._pending = ""
        except Exception as e:
            if not self._warned:
                self._warned = True
                try:
                    self._stream.write(f"[runlog] 警告: 日志文件写入失败({e}), 仅输出 journald\n")
                except Exception:
                    pass
        return len(data)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:
            pass
        try:
            with self._lock:
                if self._fh is not None:
                    if self._pending:          # 收尾: 无换行的半行也带时间戳落盘
                        self._emit_locked(self._pending)
                        self._pending = ""
                    self._fh.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")

    @property
    def current_path(self) -> str | None:
        with self._lock:
            return os.path.join(self._dir, f"{self._day}.log") if self._day else None


_tee: DailyFileTee | None = None


def install(log_dir: str) -> str | None:
    """安装每日日志分流(幂等); 返回日志目录, 失败返回 None(仅打警告)。

    目录可用性在此校验一次; 之后运行期写入失败只告警一次、不影响主流程。
    """
    global _tee
    if _tee is not None:
        return _tee._dir
    try:
        os.makedirs(log_dir, exist_ok=True)
        probe = os.path.join(log_dir, ".write_probe")
        with open(probe, "a", encoding="utf-8"):
            pass
        os.remove(probe)
    except Exception as e:
        print(f"[runlog] 日志目录不可用({log_dir}: {type(e).__name__}: {e}), 仅输出 journald")
        return None
    _tee = DailyFileTee(sys.stdout, log_dir)
    sys.stdout = _tee
    print(f"[runlog] 每日日志: {log_dir}/<YYYY-MM-DD>.log", file=sys.stderr)
    return log_dir
