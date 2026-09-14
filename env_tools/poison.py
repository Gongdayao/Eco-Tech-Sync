# -*- coding: utf-8 -*-
"""env_tools.poison — 毒瘤/平台文件分类(设计 §4.5 / 指南 §8.5)

分类结果(写入 files.poison 列):
  'platform' → 平台托管/隐藏文件: 恒排除 —— 不比对、不同步、不删除;
  'poison'   → P0 毒瘤: 不参与同步方向判断、本地绝不产生上传;
  None       → 正常文件, 参与同步。

判定规则(P0):
  - 无名/乱码名(空名、U+FFFD、控制字符);
  - SDK/工具残留(.ms_upload_* / .msc / .mv);
  - 空内容 + 临时命名模式(tmp*、*.tmp、*.temp、.*~);

隐藏文件规则(2026-09, 与 v1 对齐): 任一路径段以 '.' 开头 → 'platform' 恒排除。
  v1 在 scope2modelers_file / modelers_file_up / modelers_file_down / scope_file_down
  均有 `file_name.startswith(".")` 守卫, 整仓上传还带 ignore_patterns=".*";
  v2 原只排除 .gitattributes, 现扩展为全部隐藏项 —— 避免 .gitkeep/.gitignore/
  .DS_Store 等进入 extra/删除候选(魔乐侧存量隐藏文件也不删, 保守保留)。
"""
from __future__ import annotations

PLATFORM_MANAGED = {".gitattributes"}


def _is_hidden(path: str) -> bool:
    return any(seg.startswith(".") for seg in path.split("/") if seg)


def classify(path: str, size: int = 0) -> str | None:
    """返回 'platform' / 'poison' / None"""
    name = path.split("/")[-1]
    if path in PLATFORM_MANAGED or name == ".gitattributes":
        return "platform"
    if not name.strip() or "\ufffd" in name:
        return "poison"
    low = name.lower()
    if low.startswith(".ms_upload") or low in (".msc", ".mv", ".ms_upload_cache",
                                               ".ms_upload_progress"):
        return "poison"
    if size == 0:
        if low.startswith("tmp") or low.endswith((".tmp", ".temp")) \
                or (low.startswith(".") and low.endswith("~")):
            return "poison"
    if _is_hidden(path):
        return "platform"          # 全隐藏过滤(2026-09, 与 v1 对齐)
    return None


def is_excluded(reason: str | None) -> bool:
    """platform/poison 都不参与对比与任务"""
    return reason in ("platform", "poison")
