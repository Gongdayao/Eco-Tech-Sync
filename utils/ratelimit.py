# -*- coding: utf-8 -*-
"""utils.ratelimit — 全局礼貌限速器(设计基线: 4 worker / 6 req/s + 抖动)

当前为骨架: 简单 sleep 限速实现, 已可用; 429 时尊重 Retry-After 的适配
在 Phase 3 接入 SDK 调用时补(可在此类上加 wait_until_retry_after)。

教训: 不要用高并发对抗 WAF(bare modelscope.cn openapi 突发即 403
YUNWAF_CLIENT_UNCLASSIFIED; 已改用 www.modelscope.cn 端点)。
"""
from __future__ import annotations

import random
import time


class RateLimiter:
    """进程级共享限速器: 保证任意两次“放行”间隔 ≥ interval(+ 随机抖动)。"""

    def __init__(self, rps: float = 6.0, jitter: float = 0.2):
        self.interval = 1.0 / rps
        self.jitter = jitter
        self._last = 0.0

    def wait(self) -> None:
        """在每次 API 调用前调用(占位实现: 间隔 + 抖动)。"""
        now = time.monotonic()
        delay = self.interval - (now - self._last)
        if delay > 0:
            time.sleep(delay + random.uniform(0, self.jitter * self.interval))
        self._last = time.monotonic()


# 全局限速器: daemon 内所有平台调用共用一把(含 worker 子进程继承后各自实例,
# 子进程间不共享 —— Phase 3 若需跨进程严格限速, 改用文件锁或由 daemon 代理)。
global_limiter = RateLimiter()
