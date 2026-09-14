# -*- coding: utf-8 -*-
"""env_tools.pipeline — README / License 管线(两段式设计, V2-DESIGN.md §10)

检测(改没改)由对账哈希完成(零下载); 本模块只在【同步执行】与【采纳检查】时介入:
- split_front_matter / join_front_matter: `---` YAML 界定(严禁固定字节差);
  normalize_body: 正文比较/落盘口径(容忍首尾空行/空白, 内部逐字节不容忍);
- canonical_license / normalize_license: 原始关键字 → 规范值(license_map 表, org 隔离, 可学习);
- license_for_platform: 规范值 → 目标平台关键字(平台 license 列表 = 词表);
- transform_readme: 源侧真实 README → 目标侧版本(正文保留, 只动 front matter 的 license);
- detect_init_content: 空正文 / 平台默认模板 → 初始化内容(不参与同步);
- hash_file: 分块 sha256(大文件安全)。

license 权威源 = 真实 README.md front matter 的 license 字段(魔塔/魔乐一致语义);
禁用 openapi.get_model().readme(平台替换逻辑, 见设计 §10.1)。
"""
from __future__ import annotations

import hashlib
import re
import time

import yaml

# 平台默认 README 模板特征(中英文, 沿用 v1 特征串)
_INIT_MARKERS = (
    "当前模型的贡献者未提供更加详细的模型介绍",
    "you are viewing the default readme template",
    "this model card is the default",
)

# 原始关键字 → 规范值(缺失时用 lower 兜底并写入 license_map 学习)
_CANON = {
    "apache license 2.0": "apache-2.0", "apache-2.0": "apache-2.0", "apache 2.0": "apache-2.0",
    "mit": "mit", "mit license": "mit",
    "gpl-2.0": "gpl-2.0", "gpl-2.0-only": "gpl-2.0", "gpl-2.0+": "gpl-2.0",
    "gpl-3.0": "gpl-3.0", "gpl-3.0-only": "gpl-3.0", "gpl-3.0+": "gpl-3.0",
    "lgpl-2.1": "lgpl-2.1", "lgpl-3.0": "lgpl-3.0",
    "cc-by-4.0": "cc-by-4.0", "cc-by-nc-4.0": "cc-by-nc-4.0",
    "bsd-3-clause": "bsd-3-clause", "bsd-2-clause": "bsd-2-clause",
    "other": "other", "": "other",
}


# ---------------------------------------------------------------- front matter
# 2026-09-14 兼容口径(用户定稿):
#   front matter: 跳过块前空行后从 `---` 行到下一个 `---` 行; 块前后空行容忍, 兼容 BOM/CRLF;
#   正文: "第一行非空行 ~ 最后一行非空行"为比较/落盘区间, 首尾空行与首尾空白容忍;
#   正文内部: 逐字节比较, 内部空行与排版差异**不**容忍(不逐行 rstrip、不折叠内部空行)。
_FM_RE = re.compile(
    r"\A(?:[ \t\r]*\n)*"            # 块前空行
    r"---[ \t]*\r?\n"               # 开标记
    r"(.*?)"                        # YAML
    r"\r?\n---[ \t]*(?:\r?\n|\Z)"   # 闭标记(允许文件末尾无换行)
    , re.S)


def split_front_matter(text: str) -> tuple[str | None, str]:
    """剥离 YAML front matter: (fm_yaml|None, body)。无头或头不闭合 → (None, 全文去 BOM)。

    2026-09-14 修复(空转根因): 旧实现要求文本以 `---` 开头(`text.startswith`), 魔塔侧
    README 一旦首行为空行(手改/平台保存常见), 剥头即失败 → 整段元数据被当成正文参与
    "正文一致性"比较 → 每轮误判不一致、每轮入队空转; 同一函数被 transform_readme 复用,
    还会把元数据重复写进目标侧正文并把 license 退化成 other。现改为容忍块前空行/BOM/CRLF。
    """
    t = text.lstrip("\ufeff")
    m = _FM_RE.match(t)
    if m:
        return m.group(1), t[m.end():]
    return None, t


def normalize_body(text: str) -> str:
    """正文比较/落盘口径: 裁掉首尾空行与首尾空白, 内部逐字节保留。

    容忍: 正文首尾空行、首尾行首/行尾空白(front matter 前后空行由 split_front_matter 容忍);
    不容忍: 正文内部空行与排版差异 —— 内部换行/空白一律原样比较(不做逐行 rstrip)。
    """
    return text.strip()


def join_front_matter(fm_yaml: str | None, body: str) -> str:
    if fm_yaml:
        return f"---\n{fm_yaml.rstrip()}\n---\n{body}"
    return body


# ---------------------------------------------------------------- license
def canonical_license(raw: str | None) -> str:
    k = (raw or "").strip().lower()
    return _CANON.get(k, k or "other")


def normalize_license(conn, org_id: str, raw: str | None) -> str:
    """原始关键字 → 规范值; license_map(org, raw→norm) 裁决, 未知值学习落库"""
    raw = (raw or "").strip()
    row = None
    if raw:
        row = conn.execute("SELECT norm FROM license_map WHERE org=? AND raw=?",
                           (org_id, raw)).fetchone()
    if row:
        return row["norm"]
    norm = canonical_license(raw)
    conn.execute(
        "INSERT OR IGNORE INTO license_map(org, raw, norm, updated_at) VALUES(?,?,?,?)",
        (org_id, raw, norm, int(time.time())))
    return norm


def license_for_platform(cfg, norm: str) -> str:
    """规范值 → 目标平台关键字(平台 license 列表 = 词表); 找不到 → 'other' + 警告"""
    for kw in (cfg.license or []):
        if canonical_license(kw) == norm:
            return kw
    print(f"[pipeline] 警告: 规范 license '{norm}' 不在平台 {cfg.name} 词表中, 回落 'other'")
    return "other"


# ---------------------------------------------------------------- README 变换
def transform_readme(conn, org, src_text: str, target_platform: str) -> str:
    """源侧真实 README → 目标侧版本。

    规则(设计 §10.3): 正文保留; license 取源 front matter license 值 → 归一化 → 目标关键字;
    目标侧魔乐生成最小 front matter; 魔塔不要求 front matter → 纯正文。
    (目标侧已有 front matter 的字段级保留 = Phase 5 增强, 当前为生成式最小集)

    2026-09-14: 正文按 normalize_body 口径落盘(裁首尾空行/空白), 与对账比较口径一致,
    避免把源侧首尾空行写进目标侧; 首行空行不会再让 front matter 漏剥(见 split_front_matter)。
    """
    fm, body = split_front_matter(src_text)
    body = normalize_body(body)
    lic_raw = None
    if fm:
        try:
            parsed = yaml.safe_load(fm)
            if isinstance(parsed, dict):
                lic_raw = parsed.get("license")
        except Exception:
            lic_raw = None
    norm = normalize_license(conn, org.id, lic_raw)
    if target_platform == "modelers":
        kw = license_for_platform(org.modelers, norm)
        head = f"license: {kw}\n"
        return join_front_matter(head, body)
    # scope: 不要求 front matter → 纯正文
    return body


def detect_init_content(text: str) -> bool:
    """空正文 / 平台默认模板 → True(初始化内容, 不参与同步)"""
    fm, body = split_front_matter(text)
    if not body.strip():
        return True
    low = body.strip().lower()
    return any(marker in low for marker in _INIT_MARKERS)


# ---------------------------------------------------------------- 哈希
def hash_file(path: str) -> str:
    """分块 sha256(1MB 块)"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
