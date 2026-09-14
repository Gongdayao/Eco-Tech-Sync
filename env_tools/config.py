# -*- coding: utf-8 -*-
"""env_tools.config — 配置加载: config.yaml(orgs 组织结构体, .env 注入) → OrgContext

配置形态见 V2-DESIGN.md §12.1 / V2-IMPLEMENTATION-GUIDE.md §3:
- 全局路径、组织结构体(各平台 repo_name/token)、基础节奏留在 config.yaml;
- 绝大多数运行期配置进 DB(app_config 表, 启动时种子化);
- token 永不进 DB, 只存在于 config.yaml(.env 注入)。

本模块为骨架(Phase 1 已完成版): 业务函数已实现, 可直接使用。
"""
import os
import re
import sys
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config.yaml")

_ENV_PAT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# 缺失时需告警的变量(WEIGHTS_PATH 已改为可选: 缺省即项目根 weights, 见 load_config;
# 其余如 ALERT_WEBHOOK_URL 缺失只静默保留占位)
_WARN_KEYS = ("TOKEN", "REPO_NAME")


def _substitute_env(obj, missing: set):
    """递归替换 ${VAR} 占位符; 未定义变量原样保留并记录"""
    if isinstance(obj, str):
        def repl(m):
            k = m.group(1)
            v = os.environ.get(k)
            if v is None:
                missing.add(k)
                return m.group(0)
            return v
        return _ENV_PAT.sub(repl, obj)
    if isinstance(obj, dict):
        return {k: _substitute_env(v, missing) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_env(x, missing) for x in obj]
    return obj


@dataclass
class PlatformCfg:
    """一个组织在一个平台的配置(≈ v1 的 modelscope_cfg / modelers_cfg / gitcode_cfg 单块)"""
    name: str                       # 'scope' | 'modelers' | 'gitcode'
    repo_name: str = ""
    token: str = ""
    license_name: str = "license"
    license: list = field(default_factory=list)
    scope_base_url: str = ""        # 仅 gitcode 使用

    @property
    def configured(self) -> bool:
        return bool(self.repo_name)


@dataclass
class OrgContext:
    """一个组织的全部上下文: id + 各平台配置 + 本地目录; 对账/传输/worker 均通过它工作"""
    id: str
    scope: PlatformCfg
    modelers: PlatformCfg
    gitcode: PlatformCfg | None
    weights_subdir: str
    weights_root: str               # <权重根>/<subdir>(默认 <项目根>/weights/<subdir>)

    @property
    def has_gitcode(self) -> bool:
        return self.gitcode is not None and self.gitcode.configured

    def compare_dir(self, model: str) -> str:
        return os.path.join(self.weights_root, "compare_weights", model)

    def updown_dir(self, model: str) -> str:
        return os.path.join(self.weights_root, "updown_weights", model)


def _load_dotenv() -> None:
    """token 从项目根 .env 读取(与 v1 一致); 已存在的环境变量优先"""
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=False)


def _build_platform(name: str, raw: dict | None) -> PlatformCfg:
    raw = raw or {}
    cfg = PlatformCfg(
        name=name,
        repo_name=str(raw.get("repo_name", "") or ""),
        token=str(raw.get("token", "") or ""),
        license_name=str(raw.get("license_name", "license")),
        license=list(raw.get("license", []) or []),
        scope_base_url=str(raw.get("scope_base_url", "") or ""),
    )
    return cfg


def load_config(path: str | None = None) -> tuple[dict, list[OrgContext]]:
    """加载 config.yaml v2, 返回 (原始 dict, OrgContext 列表)

    - 单组织旧配置兼容: 无 orgs 列表时视为单元素(org id 取 modelscope repo_name);
    - 缺失 token 只告警不失败(组织仍可构建, 传输阶段会因无凭据失败并告警)。
    """
    _load_dotenv()
    path = path or DEFAULT_CONFIG
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f.read())
    missing: set = set()
    raw = _substitute_env(raw, missing)
    warn = {k for k in missing if any(t in k.upper() for t in _WARN_KEYS)}
    if warn:
        print(f"[警告] 环境变量未定义: {sorted(warn)}(.env 缺失; 相关 token 将为空)",
              file=sys.stderr)

    global_cfg = raw.get("global", {})
    # 权重根目录(2026-09 可移植修正): 默认 <项目根>/weights —— 由 __file__ 推导的
    # 绝对路径, 与机器/CWD 无关; config.yaml 的 ${WEIGHTS_PATH} 已由 bootstrap
    # setdefault 兜底; 这里再防一手未解析占位符(直接跑 config 未走 bootstrap 的场景)
    wp = str(global_cfg.get("weights_path", "") or "")
    if not wp or wp.startswith("${"):
        weights_path = os.path.join(PROJECT_ROOT, "weights")
    else:
        weights_path = wp

    orgs_raw = raw.get("orgs")
    if not orgs_raw:
        # 兼容: 旧单组织结构(modelscope_cfg/modelers_cfg/gitcode_cfg 平铺)
        legacy_id = raw.get("modelscope_cfg", {}).get("repo_name") or "default"
        orgs_raw = [{
            "id": legacy_id,
            "scope": raw.get("modelscope_cfg", {}),
            "modelers": raw.get("modelers_cfg", {}),
            "gitcode": raw.get("gitcode_cfg"),
        }]

    orgs: list[OrgContext] = []
    for o in orgs_raw:
        oid = str(o.get("id") or "").strip()
        if not oid:
            continue
        subdir = str(o.get("weights_subdir") or oid)
        org = OrgContext(
            id=oid,
            scope=_build_platform("scope", o.get("scope")),
            modelers=_build_platform("modelers", o.get("modelers")),
            gitcode=_build_platform("gitcode", o.get("gitcode")) if o.get("gitcode") else None,
            weights_subdir=subdir,
            weights_root=os.path.join(weights_path, subdir),
        )
        missing = [p.name for p in (org.scope, org.modelers, org.gitcode)
                   if p is not None and p.configured and not p.token]
        if missing:
            print(f"[警告] org '{oid}' 平台 {missing} 未配置 token(仅可读公开信息, 传输将失败)")
        orgs.append(org)

    if not orgs:
        raise ValueError(f"config 中未定义任何组织: {path}")
    return raw, orgs


def get_org(orgs: list[OrgContext], org_id: str) -> OrgContext | None:
    for o in orgs:
        if o.id == org_id:
            return o
    return None
