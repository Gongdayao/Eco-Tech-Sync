# -*- coding: utf-8 -*-
"""env_tools.gitcode — GitCode 纯镜像(按组织可选): 新增导入 + 删除对齐 + 全量补齐

定位(见 V2-DESIGN.md §11 / 指南 §12):GitCode 是纯镜像站,镜像内容由
平台侧 pull 同步自动跟随 ModelScope 同名仓库。同步器只做(仅魔塔公开权重):
  1. 新增导入(入 DB 即触发): gitcode_import(返回驱动确认, 对齐 v1)+ pull 开启提醒;
  2. 删除对齐: gitcode_delete(幂等);
  3. 全量补齐: scan_and_fill(首次部署 / audit)。

API(实测/复刻 v1, token 从 URL query 移入 Authorization 头):
  GET  /api/v5/orgs/{org}/repos?type=all&page=&per_page=&repo_type=model  组织仓库列表
  POST /api/v5/orgs/{org}/repos    {name, public, import_url, repository_type:'model'}
  DELETE /api/v5/repos/{owner}/{repo}   删除(幂等)
"""
from __future__ import annotations

import time

import requests

_API = "https://api.gitcode.com"
_TIMEOUT = 30


def _headers(org) -> dict:
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if org.gitcode and org.gitcode.token:
        h["Authorization"] = f"Bearer {org.gitcode.token}"
    return h


def list_org_repos(org, repo_type: str = "model", per_page: int = 100) -> list[str]:
    """GitCode 组织下全部仓库名列表(只读)。"""
    names: list[str] = []
    page = 1
    while True:
        r = requests.get(
            f"{_API}/api/v5/orgs/{org.gitcode.repo_name}/repos",
            headers=_headers(org), timeout=_TIMEOUT,
            params={"type": "all", "page": page, "per_page": per_page,
                    "repo_type": repo_type})
        r.raise_for_status()
        items = r.json()
        if not isinstance(items, list) or not items:
            break
        names.extend(i.get("name") for i in items if isinstance(i, dict) and i.get("name"))
        if len(items) < per_page:
            break
        page += 1
    return names


def _import_confirmed(r, org, model: str) -> tuple[bool, str]:
    """按 V1 方式从 POST 返回确认导入成功: 2xx + 返回体 JSON 含仓库标识。

    2026-09 修正: 曾用 poll_repo_ready 轮询确认 —— 每任务最多白等 120s, 且依赖
    "仓库存在性探测"端点(匿名调用恒 400)→ 轮询永不确认 → 导入成功却无 pull 提醒、
    gitcode_status 滞留 pending(任务却显示 succeeded, 静默降级)。实测 GitCode
    import POST 同步返回完整仓库元数据(200 + {name/full_name/url...}), 返回驱动
    足够 —— 与 v1 gitcode_conn.create_repo 的确认方式一致(2xx + html_url/url 字段)。
    """
    if r.status_code not in (200, 201):
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    try:
        data = r.json()
    except Exception as e:
        return False, f"HTTP {r.status_code} 返回体解析失败: {type(e).__name__}: {e}"
    if not isinstance(data, dict):
        return False, f"HTTP {r.status_code} 返回体非对象: {str(data)[:200]}"
    name = str(data.get("name") or "")
    full = str(data.get("full_name") or "")
    if name == model or full == f"{org.gitcode.repo_name}/{model}" \
            or data.get("url") or data.get("html_url"):
        return True, ""
    return False, f"返回体缺少仓库标识(name/full_name/url): {str(data)[:200]}"


def gitcode_import(conn, org, model: str) -> dict:
    """通过 import_url(modelscope .git)创建镜像仓库; 返回驱动确认 + pull 开启提醒。

    scope_base_url 在 config.yaml 里配置于 gitcode 段(v1 同款: gitcode_cfg.scope_base_url)。
    2026-09 事故: 曾误读 org.scope.scope_base_url(魔塔段, 恒为空) → import_url 拼成
    "Eco-Tech/<model>.git" 这种无协议头残缺 URL, GitCode 检查器报 "check url unreachable"。
    现从 org.gitcode 读取并兜底, 杜绝残缺 URL。

    成功语义(2026-09 修正, 对齐 v1): POST 2xx 且返回体确认仓库标识 → 立即标记
    imported 并插入 pull 开启提醒, 不做轮询(轮询曾导致"导入成功却无提醒")。
    """
    base = ((org.gitcode.scope_base_url if org.gitcode else "") or org.scope.scope_base_url
            or "https://www.modelscope.cn/models/")
    import_url = f"{base}{org.scope.repo_name}/{model}.git"
    payload = {
        "name": model,
        "has_issues": False,
        "has_wiki": False,
        "can_comment": True,
        "public": 1,
        "import_url": import_url,
        "repository_type": "model",
    }
    r = requests.post(f"{_API}/api/v5/orgs/{org.gitcode.repo_name}/repos",
                      headers=_headers(org), json=payload, timeout=_TIMEOUT)
    ok, err = _import_confirmed(r, org, model)
    if not ok:
        _mark(conn, org, model, "failed")
        return {"ok": False, "error": err, "repo": f"{org.gitcode.repo_name}/{model}"}
    _mark(conn, org, model, "imported")
    tasks = __import__("env_tools.tasks", fromlist=["insert_alert"])
    tasks.insert_alert(conn, org.id, None, model, "warn",
                       f"GitCode 镜像已导入 {model}, 请前往 "
                       f"https://ai.gitcode.com/{org.gitcode.repo_name}/{model}/setting/mirror "
                       f"开启 pull 同步")
    return {"ok": True, "imported": True, "repo": f"{org.gitcode.repo_name}/{model}"}


def gitcode_delete(conn, org, model: str) -> dict:
    """删除对齐(幂等): 目标不存在(404)视为已删除。"""
    r = requests.delete(f"{_API}/api/v5/repos/{org.gitcode.repo_name}/{model}",
                        headers=_headers(org), timeout=_TIMEOUT)
    if r.status_code in (200, 204, 404):
        _mark(conn, org, model, None)          # 已删除 → 清状态
        return {"ok": True, "deleted": r.status_code != 404,
                "repo": f"{org.gitcode.repo_name}/{model}"}
    return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}",
            "repo": f"{org.gitcode.repo_name}/{model}"}


def scan_and_fill(conn, org) -> dict:
    """全量补齐: GitCode 组织仓库列表 vs 魔塔公开模型(DB models), 缺失入队导入。

    首次部署 / audit 命令调用; gitcode_status 分组计数即同步情况报表。
    """
    stat = {"checked": 0, "missing": 0, "existing": 0, "enqueued": 0,
            "gitcode_repos": 0}
    try:
        gc_names = set(list_org_repos(org))
    except Exception as e:
        return {**stat, "error": f"{type(e).__name__}: {e}"}
    stat["gitcode_repos"] = len(gc_names)
    rows = conn.execute(
        "SELECT name, gitcode_status FROM models WHERE org=? AND platform='scope' AND "
        "visibility=5 AND gated=0", (org.id,)).fetchall()   # 魔塔公开非 gated
    from env_tools import tasks as _t
    now = int(time.time())
    for r in rows:
        name = r["name"]
        stat["checked"] += 1
        exists = name in gc_names
        if exists:
            stat["existing"] += 1
            # 存量已导入的仓库只更新状态, 不提醒(2026-09 用户定稿: 老模型/首次
            # 部署前已存在的镜像不打扰; "开启 pull 同步"提醒只在 gitcode_import
            # 任务【本同步器工作周期内】导入成功时发一次)。
            conn.execute(
                "UPDATE models SET gitcode_status='imported', gitcode_checked_at=? "
                "WHERE org=? AND platform='scope' AND name=?", (now, org.id, name))
        else:
            stat["missing"] += 1
            conn.execute(
                "UPDATE models SET gitcode_status='pending', gitcode_checked_at=? "
                "WHERE org=? AND platform='scope' AND name=?", (now, org.id, name))
            _, created = _t.enqueue_task(conn, org.id, _t.KIND_GITCODE_IMPORT, name,
                                         created_by="reconcile")
            if created:
                stat["enqueued"] += 1
    conn.commit()
    return stat


def _mark(conn, org, model: str, status: str | None) -> None:
    conn.execute(
        "UPDATE models SET gitcode_status=?, gitcode_checked_at=? "
        "WHERE org=? AND platform='scope' AND name=?",
        (status, int(time.time()), org.id, model))
    conn.commit()
