# -*- coding: utf-8 -*-
"""env_tools.transfer — 平台 SDK 封装与同步主流程

只读拉取(fetch_remote_*)在 Phase 2 已实现(真实 SDK, 礼貌限速);
写操作(sync_model / sync_files / delete_repo_task 等)Phase 3 落地。

硬约束(实现时, 见指南 §9.1):
  - 一个上传批次 = 一次 commit(upload_folder 整体调用, 禁止 per-file upload);
  - SDK 内部并发池 max_workers=5(3~5 文件即可打满 1G 带宽, 不做 work 级并发);
  - 上传后 LFS sha256 回读校验通过才标记成功;
  - 下载去掉 force_download=True(本地 sha256 匹配即跳过);
  - 全局限速: utils/ratelimit.global_limiter(6 req/s + 抖动, 勿高并发对抗 WAF)。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone

from utils.ratelimit import global_limiter

from env_tools import poison as _poison


# ---------------------------------------------------------------- 客户端与工具
_SCOPE_APIS: dict[str, object] = {}


def _scope_api(org):
    """魔塔 HubApi 客户端(按 org 缓存, login 一次)"""
    api = _SCOPE_APIS.get(org.id)
    if api is None:
        from modelscope_hub.api import HubApi
        api = HubApi()
        if org.scope.token:
            api.login(org.scope.token)
        _SCOPE_APIS[org.id] = api
    return api


def _to_epoch(v) -> int | None:
    """datetime(naive=UTC)/ISO 字符串/int → UTC epoch; 无法解析返回 None"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return int(v.timestamp())
    if isinstance(v, str):
        s = v.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return int(datetime.fromisoformat(s).timestamp())
        except ValueError:
            return None
    return None


def _get(obj, name, default=None):
    v = getattr(obj, name, default)
    # enum(如 Visibility.PUBLIC)→ 标量
    if hasattr(v, "value") and not isinstance(v, (str, int, float, bool)):
        return v.value
    return v


# ---------------------------------------------------------------- 只读拉取(Phase 2)
def fetch_remote_models(conn, org, platform: str) -> list[dict]:
    """拉平台模型列表 → models 表同构 dict 列表(真实 SDK, 礼貌限速)。

    注意: 魔塔默认分支 master / 魔乐 main; MODELSCOPE_ENDPOINT 已由 bootstrap 设置。
    """
    out: list[dict] = []
    if platform == "scope":
        api = _scope_api(org)
        page = 1
        while True:
            global_limiter.wait()
            res = api.list_repos(owner=org.scope.repo_name, repo_type="model",
                                 page_number=page, page_size=50)
            items = list(res.items) if hasattr(res, "items") else list(res)
            if not items:
                break
            for r in items:
                name = _get(r, "name", "")
                out.append({
                    "platform": "scope",
                    "repo_id": f"{org.scope.repo_name}/{name}",
                    "owner": _get(r, "owner", None) or org.scope.repo_name,
                    "name": name,
                    "visibility": _get(r, "visibility", None),
                    "private": 1 if _get(r, "private", False) else 0,
                    "gated": 1 if _get(r, "gated", False) else 0,
                    "login_required": _get(r, "login_required", None),
                    "description": _get(r, "description", None),
                    "downloads": _get(r, "downloads", None),
                    "likes": _get(r, "likes", None),
                    "created_at": _to_epoch(_get(r, "created_at", None)),
                    "last_modified": _to_epoch(_get(r, "last_modified", None)),
                    "license_raw": _get(r, "license", None),
                    "display_name": _get(r, "display_name", None),
                    "file_size": _get(r, "file_size", None),
                    "tags_json": json.dumps(_get(r, "tags", []) or [], ensure_ascii=False),
                    "tasks_json": json.dumps(_get(r, "tasks", []) or [], ensure_ascii=False),
                })
            if len(items) < 50:      # 实测: page_size 上限 50
                break
            page += 1
    elif platform == "modelers":
        from openmind_hub import list_models
        global_limiter.wait()
        try:
            infos = list(list_models(author=org.modelers.repo_name, token=org.modelers.token))
        except TypeError:
            # 老签名兼容: 部分版本无 author 参数
            infos = list(list_models(token=org.modelers.token))
        for m in infos:
            name = _get(m, "name", "")
            owner = _get(m, "owner", None) or org.modelers.repo_name
            rid = _get(m, "id", "")
            if not isinstance(rid, str) or "/" not in rid:
                rid = f"{owner}/{name}"
            out.append({
                "platform": "modelers",
                "repo_id": rid,
                "owner": owner,
                "name": name,
                "visibility": None,
                "private": 1 if _get(m, "private", False) else 0,
                "gated": 0,
                "login_required": None,
                "description": None,
                "downloads": _get(m, "downloads", None),
                "likes": _get(m, "likes", None),
                "created_at": _to_epoch(_get(m, "created_at", None)),
                "last_modified": _to_epoch(_get(m, "last_modified", None)),
                "license_raw": None,
                "display_name": _get(m, "fullname", None) or name,
                "file_size": None,
                "tags_json": json.dumps(_get(m, "tags", []) or [], ensure_ascii=False),
                "tasks_json": json.dumps([_get(m, "pipeline_tag", "")] or [], ensure_ascii=False),
            })
    else:
        raise ValueError(f"未知平台: {platform}(fetch_remote_models 仅 scope/modelers)")
    return out


def fetch_remote_files(conn, org, platform: str, model: str) -> list[dict]:
    """拉模型文件树 → files 表同构 dict 列表(真实 SDK, 礼貌限速)。

    指纹规则(设计 §4.2/§8.3):
      scope 全文件: API sha256(blob_id == sha256);
      modelers LFS: lfs.sha256(64 位); 非 LFS: blob_id(40 位 git SHA1) 作每侧变更指纹。

    ⚠ 源头过滤(2026-09): 隐藏文件/毒瘤在【拉取阶段】即丢弃(poison.classify),
    不返回、不落库、不比对 —— 全程忽略(.gitattributes/.gitkeep/.DS_Store 等),
    与 v1 各阶段 startswith(".") 过滤一致; 调用方无需再按 poison 过滤。
    """
    out: list[dict] = []
    repo_id = f"{org.scope.repo_name if platform == 'scope' else org.modelers.repo_name}/{model}"
    if platform == "scope":
        api = _scope_api(org)
        global_limiter.wait()
        files = api.list_repo_files(repo_id, repo_type="model", revision="master")
        for f in files:
            if _get(f, "type", "") == "tree":
                continue
            path = _get(f, "path", "")
            size = _get(f, "size", 0) or 0
            if _poison.is_excluded(_poison.classify(path, size)):
                continue                 # 源头过滤: 隐藏/毒瘤不进管线
            sha = _get(f, "sha256", None)
            out.append({
                "platform": "scope",
                "repo_id": repo_id,
                "path": path,
                "size": size,
                "sha256": sha,
                "sha256_source": "api" if sha else "none",
                "blob_id": _get(f, "blob_id", None),
                # 魔塔侧不需要 LFS 区分: FileInfo.sha256 与 lfs 平级, 所有文件
                # (含 safetensors)API 均直接返回内容 sha256 → 指纹一律用 sha256;
                # is_lfs 仅魔乐侧有语义(魔乐 LFS 才有 sha256, 非 LFS 只有 blob_id)。
                "is_lfs": None,
                "last_modified": _to_epoch(_get(f, "last_modified", None)),
            })
    elif platform == "modelers":
        from openmind_hub import list_repo_tree
        global_limiter.wait()
        entries = list_repo_tree(repo_id, recursive=True, revision="main",
                                 token=org.modelers.token)
        for e in entries:
            size = _get(e, "size", None)
            if size is None:          # RepoFolder
                continue
            path = _get(e, "path", "")
            if _poison.is_excluded(_poison.classify(path, size or 0)):
                continue                 # 源头过滤: 隐藏/毒瘤不进管线
            lfs = _get(e, "lfs", None)
            blob = _get(e, "blob_id", None)
            if lfs is not None:
                sha = (lfs.get("sha256") if isinstance(lfs, dict)
                       else _get(lfs, "sha256", None))
                sha = sha or (blob if isinstance(blob, str) and len(blob) == 64 else None)
            else:
                sha = None             # 非 LFS: 只有 git SHA1(blob_id), 无 sha256
            out.append({
                "platform": "modelers",
                "repo_id": repo_id,
                "path": path,
                "size": size or 0,
                "sha256": sha,
                "sha256_source": "api" if sha else "none",
                "blob_id": blob,
                "is_lfs": 1 if lfs is not None else 0,
                "last_modified": _to_epoch((_get(e, "last_commit", None) or {}).get("date")
                                           if isinstance(_get(e, "last_commit", None), dict)
                                           else _get(_get(e, "last_commit", None), "date", None)),
            })
    else:
        raise ValueError(f"未知平台: {platform}(fetch_remote_files 仅 scope/modelers)")
    return out


# ---------------------------------------------------------------- 写操作(Phase 3)
_OWNER = {}


def _repo_id(org, platform: str, model: str) -> str:
    cfg = org.scope if platform == "scope" else org.modelers
    return f"{cfg.repo_name}/{model}"


def repo_exists(org, platform: str, model: str) -> bool:
    """源/目标 repo 存在性(只读)。网络/API 异常向上抛(可重试); 404 类返回 False。"""
    repo_id = _repo_id(org, platform, model)
    if platform == "scope":
        api = _scope_api(org)
        global_limiter.wait()
        return bool(api.repo_exists(repo_id, "model"))
    from openmind_hub import model_info
    global_limiter.wait()
    try:
        model_info(repo_id, token=org.modelers.token)
        return True
    except Exception as e:
        # 404 类(EntryNotFoundError / RepositoryNotFoundError / 兼容变体)→ 不存在;
        # 其余(网络/5xx)→ 向上抛, 由调用方按可重试失败处理
        low = f"{type(e).__name__}: {e}".lower()
        if "entrynotfound" in low or "repositorynotfound" in low or \
                "not exist" in low or ("404" in low and "not found" in low):
            return False
        raise


def ensure_repo(conn, org, platform: str, model: str, license_kw: str | None = None,
                private: bool = False) -> bool:
    """幂等建仓; 返回是否新建。新建时写 models 行 repo_created_by_us=1(审计)。

    可见性(单向规则 §2.3): 目标=魔乐 → private 参考魔塔; 目标=魔塔 → 参考魔乐。
    """
    repo_id = _repo_id(org, platform, model)
    if repo_exists(org, platform, model):
        return False
    if platform == "scope":
        api = _scope_api(org)
        global_limiter.wait()
        api.create_repo(repo_id, "model", visibility=1 if private else 5,
                        license=license_kw)
    else:
        from openmind_hub import create_repo
        global_limiter.wait()
        create_repo(repo_id, token=org.modelers.token, repo_type="model",
                    exist_ok=True, license=license_kw or "other", private=private)
    # 记录: 我们创建的目标 repo
    import time as _t
    conn.execute(
        """INSERT INTO models(org, platform, repo_id, owner, name, visibility, private,
             gated, first_seen_at, last_seen_at, repo_created_by_us)
           VALUES(?,?,?,?,?,5,0,0,?,?,1)
           ON CONFLICT(org, platform, repo_id) DO UPDATE SET
             repo_created_by_us=1, last_seen_at=excluded.last_seen_at""",
        (org.id, platform, repo_id, org.scope.repo_name if platform == "scope"
         else org.modelers.repo_name, model, _t.time(), _t.time()))
    conn.commit()
    return True


_CACHE_LOGGED = False
_UPLOAD_CACHE_FILE = ".ms_upload_cache"      # 魔塔上传器的本地"未变文件跳过"缓存


def _sdk_caches() -> tuple[str, str]:
    """两个 SDK 的实际缓存目录(仅用于日志与显式传参)。

    env_bootstrap 已把它们统一到 <weights>/.cache 下(SYNC_CACHE_DIR 可覆盖):
      魔塔 MODELSCOPE_CACHE=<cache>/modelscope; 魔乐 XDG_CACHE_HOME=<cache> → <cache>/openmind/hub
    """
    root = os.environ.get("SYNC_CACHE_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "weights", ".cache")
    ms = os.environ.get("MODELSCOPE_CACHE") or os.path.join(root, "modelscope")
    om = os.path.join(os.environ.get("XDG_CACHE_HOME") or root, "openmind", "hub")
    return ms, om


def _log_caches_once() -> None:
    global _CACHE_LOGGED
    if not _CACHE_LOGGED:
        _CACHE_LOGGED = True
        ms, om = _sdk_caches()
        print(f"[transfer] SDK 缓存目录: 魔塔={ms} 魔乐={om}", flush=True)


def _download_one(org, src_platform: str, repo_id: str, path: str, dest_dir: str,
                  sha256: str | None = None) -> None:
    """单文件下载到 dest_dir(保持相对路径); 复用本地/缓存, 不强制重下(2026-10-08)。

    调用方(兜底路径)已把不合格的本地文件删掉, 否则魔塔 `download_file(expected_sha256=None)`
    会因"文件存在"直接命中而跳过(源码 _cache_hit)。传入 sha256 时 SDK 还会做完整性校验。
    """
    if src_platform == "scope":
        api = _scope_api(org)
        global_limiter.wait()
        api.download_file(repo_id, "model", path, local_dir=dest_dir, force=False,
                          expected_sha256=sha256)
        return
    from openmind_hub import om_hub_download
    _ms_cache, om_cache = _sdk_caches()
    global_limiter.wait()
    p = om_hub_download(repo_id, path, revision="main", token=org.modelers.token,
                        cache_dir=om_cache, force_download=False)
    dst = os.path.join(dest_dir, path)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        os.remove(dst)
    try:
        os.link(str(p), dst)              # 同盘: 零拷贝硬链接(缓存 ↔ 暂存)
    except OSError:
        shutil.copyfile(str(p), dst)


def _norm_meta(size, sha, blob) -> tuple:
    """归一化期望元数据 → (size, sha256_64|None, git_sha1_40|None)。"""
    sha64 = sha if (sha and len(sha) == 64) else None
    blob40 = next((c for c in (sha, blob) if c and len(c) == 40), None)
    return (size, sha64, blob40)


def _expected_meta(conn, org, platform: str, model: str, paths: list[str],
                   expect: dict | None = None) -> dict:
    """每文件的期望 (size, sha256_64, git_sha1_40) —— 供下载后自查。

    优先级: 调用方给的**实时文件树**(expect, 最权威) > DB files 表基线。
    两者都没有时 size/sha 均为 None → 自查阶段会拒绝该文件(绝不静默上传不确定内容)。
    """
    repo_id = _repo_id(org, platform, model)
    out = {}
    for p in paths:
        row = conn.execute(
            "SELECT size, sha256, blob_id FROM files WHERE org=? AND platform=? AND repo_id=? AND path=?",
            (org.id, platform, repo_id, p)).fetchone()
        out[p] = _norm_meta((row["size"] if row else None),
                            (row["sha256"] or None) if row else None,
                            (row["blob_id"] or None) if row else None)
    for p, v in (expect or {}).items():
        if p in out and v and any(v[:3]):
            out[p] = _norm_meta(v[0], v[1] if len(v) > 1 else None,
                                v[2] if len(v) > 2 else None)
    return out


def _git_blob_sha1(path: str) -> str:
    """git blob sha1 = sha1("blob <len>\0" + 内容); 魔乐非 LFS blob_id 与魔塔 40 位形态同源。"""
    size = os.path.getsize(path)
    h = hashlib.sha1()
    h.update(f"blob {size}\0".encode())
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_downloaded(dest_dir: str, paths: list[str], meta: dict,
                         src_platform: str) -> list[str]:
    """下载后自查, 返回不合格(缺失/尺寸不符/哈希不符)的 path。

    为什么必须自查(2026-10-08 实测): 魔塔 `download_repo` 对单文件失败**只 warning 不抛错**,
    不自查就会把缺失/半截文件上传。校验强度: size 必查; 内容哈希只在"SDK 无可信期望值"
    (魔塔清单只给 40 位 blob 的少数文件)时自算, 其余交给 SDK 自带的 sha256/etag 校验,
    避免每次重试把 TB 级文件全量读一遍。
    """
    bad = []
    for p in paths:
        fp = os.path.join(dest_dir, p)
        if not os.path.isfile(fp):
            bad.append(p)
            continue
        size, sha64, blob40 = meta.get(p, (None, None, None))
        if not size and not sha64 and not blob40:
            bad.append(p)               # 无任何期望元数据 → 无法校验, 拒绝放行
            continue
        if size and os.path.getsize(fp) != size:
            bad.append(p)
            continue
        if src_platform == "scope" and not sha64 and blob40:
            if _git_blob_sha1(fp) != blob40:
                bad.append(p)
    return bad


def _prune_stage(stage: str, expected: set) -> list[str]:
    """上传前把暂存目录归约成"期望集合", 返回被删清单。

    必要性(2026-10-08 实测): 两个 SDK 都不清理 `<file>.incomplete` 等残留, 而
    upload_folder 会传目录里的全部文件 → 不 prune 就会把半截/无关文件传到目标平台。
    例外: 魔塔上传器的 `.ms_upload_cache` 保留(重试时用于跳过未变文件)。
    """
    removed = []
    for root, _dirs, files in os.walk(stage):
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, stage)
            if rel in expected or fn == _UPLOAD_CACHE_FILE:
                continue
            try:
                os.remove(full)
                removed.append(rel)
            except OSError:
                pass
    for root, dirs, _files in os.walk(stage, topdown=False):
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
            except OSError:
                pass
    return removed


def _progress(conn, task_id, text: str) -> None:
    """回写任务进度到 DB(status 可见性, 2026-09); 失败不影响同步主流程。"""
    try:
        from env_tools import tasks as _t
        _t.set_progress(conn, task_id, text)
    except Exception:
        pass


def _download_modelers_snapshot(org, repo_id: str, paths: list[str], dest_dir: str,
                                workers: int) -> None:
    """魔乐源: **缓存模式**快照下载, 再把文件硬链接/复制进暂存目录。

    2026-10-08 实测(openmind-hub 1.3.0):
      - `snapshot_download(local_dir=…)` 模式**既不校验本地文件也不写缓存** → 重试必然重下;
      - 去掉 local_dir(缓存模式)+ `force_download=False` → 缓存按 etag 校验并命中(断网可完成);
      - 故这里用缓存模式, 再 os.link(同盘零拷贝)/copyfile 落到暂存目录。
    """
    from openmind_hub import snapshot_download
    _ms_cache, om_cache = _sdk_caches()
    global_limiter.wait()
    snap = snapshot_download(repo_id, repo_type="model", revision="main",
                             allow_patterns=list(paths), cache_dir=om_cache,
                             force_download=False, local_dir_use_symlinks=False,
                             token=org.modelers.token, max_workers=workers)
    for p in paths:
        s_path = os.path.join(str(snap), p)
        d_path = os.path.join(dest_dir, p)
        os.makedirs(os.path.dirname(d_path), exist_ok=True)
        if os.path.exists(d_path):
            os.remove(d_path)
        try:
            os.link(s_path, d_path)
        except OSError:
            shutil.copyfile(s_path, d_path)


def _download_set(conn, org, src_platform: str, model: str, paths: list[str],
                  dest_dir: str, on_file=None, expect: dict | None = None) -> int:
    """整批下载(任务内并发) + **复用/续传** + 下载后自查。

    2026-10-08 定稿(目标: 上传失败重试不再重复下载):
      - 暂存目录由调用方保留(见 _maybe_clean_stage): 完整文件由 SDK 校验后**跳过**,
        半截文件由 SDK **续传**(魔塔实测 `Range: bytes=<已下>-`, `.incomplete`);
      - 魔塔源: `download_repo(local_dir=stage, allow_patterns=全部 paths)`
        SDK 按清单 sha256 逐个校验, 命中即跳过(实测: 断网也能完成);
      - 魔乐源: 缓存模式(见 _download_modelers_snapshot);
      - 下载后自查 size(+必要时 blob sha1): 魔塔 SDK 对单文件失败只 warning 不抛错;
      - 不合格 → 逐文件兜底; 兜底后仍不合格 → 抛错(任务失败, 暂存保留待重试)。
    """
    paths = [p for p in paths if p]
    if not paths:
        return 0
    from env_tools import db as _db
    repo_id = _repo_id(org, src_platform, model)
    n = len(paths)
    meta = _expected_meta(conn, org, src_platform, model, paths, expect=expect)
    if any(not any(meta.get(p, ())[:3] or ()) for p in paths):
        print(f"[transfer] {src_platform}/{model} 警告: 部分文件无期望 size/hash, "
              f"下载后自查会拒绝它们(宁可失败也不静默上传)", flush=True)
    sizes = {p: (meta.get(p, (0, None, None))[0] or 0) for p in paths}
    total = sum(sizes.values())
    workers = int(_db.get_app_config(conn, org.id, "sync.download_workers", 5))
    os.makedirs(dest_dir, exist_ok=True)
    _log_caches_once()
    api_name = "download_repo" if src_platform == "scope" else "snapshot_download(cache)"
    print(f"[transfer] {src_platform}/{model} 下载 {n} 个文件 ({total / 1e9:.2f} GB): "
          f"{api_name}(allow_patterns={n}, max_workers={workers}, 复用+续传)", flush=True)
    if on_file is not None:
        try:
            on_file(0, n, f"下载开始({api_name}, max_workers={workers})")
        except Exception:
            pass
    err = None
    try:
        if src_platform == "scope":
            global_limiter.wait()
            _scope_api(org).download_repo(repo_id, repo_type="model", revision="master",
                                          local_dir=dest_dir, allow_patterns=list(paths),
                                          max_workers=workers)
        else:
            _download_modelers_snapshot(org, repo_id, paths, dest_dir, workers)
    except Exception as e:                     # 网络/接口异常 → 逐文件兜底
        err = e
        print(f"[transfer] {src_platform}/{model} 批量下载异常 → 逐文件兜底: "
              f"{type(e).__name__}: {e}", flush=True)
    bad = _validate_downloaded(dest_dir, paths, meta, src_platform)
    if err is None and not bad:
        print(f"[transfer] {src_platform}/{model} 下载完成(复用跳过): {n}/{n} "
              f"({total / 1e9:.2f} GB)", flush=True)
        if on_file is not None:
            try:
                on_file(n, n, paths[-1])
            except Exception:
                pass
        return n
    if bad:
        print(f"[transfer] {src_platform}/{model} 下载后自查不合格 {len(bad)} 个"
              f"(示例 {bad[:3]}), 逐文件兜底", flush=True)
    for p in list(bad):
        fp = os.path.join(dest_dir, p)
        if os.path.exists(fp):
            os.remove(fp)          # 先删不合格文件, 否则 SDK 可能"存在即命中"跳过
        _download_one(org, src_platform, repo_id, p, dest_dir,
                      sha256=(meta.get(p, (None, None, None))[1]))
    bad2 = _validate_downloaded(dest_dir, paths, meta, src_platform)
    if bad2:
        raise RuntimeError(f"下载校验失败 {len(bad2)} 个文件(示例 {bad2[:3]}); 暂存目录保留待重试")
    print(f"[transfer] {src_platform}/{model} 下载完成(兜底补齐 {len(bad)} 个): {n}/{n}", flush=True)
    if on_file is not None:
        try:
            on_file(n, n, paths[-1])
        except Exception:
            pass
    return n


def _upload_dir(org, dst_platform: str, model: str, folder: str,
                commit_msg: str, conn=None) -> None:
    """整批上传; 按 `sync.upload_max_files` 分块(每块一次 commit), 并发由 SDK 内部池负责。

    2026-10-08(生产事故驱动): 出现过"78 个 LFS 文件全部传完后 commit 端点 500"→ 整批不落地、
    下次重试重传一小时。分块后单块失败只丢该块, 其余块已落地(下一轮对账只补剩余)。
    `upload_max_files=0`(默认)= 不分块(旧行为); 大权重建议设 20 左右。
    接口: 魔塔 `HubApi.upload_folder(..., max_workers=5)`; 魔乐 `openmind_hub.upload_folder(...)`。
    """
    from env_tools import db as _db
    repo_id = _repo_id(org, dst_platform, model)
    flist = sorted(
        os.path.relpath(os.path.join(r, f), folder)
        for r, _d, fs in os.walk(folder) for f in fs
        if f != _UPLOAD_CACHE_FILE)
    if not flist:
        return
    cap = int(_db.get_app_config(conn, org.id, "sync.upload_max_files", 0)) if conn is not None else 0
    chunks = [flist] if cap <= 0 else [flist[i:i + cap] for i in range(0, len(flist), cap)]
    for idx, chunk in enumerate(chunks, 1):
        part = folder
        if len(chunks) > 1:
            # 分块: 硬链接组一个只含本块文件的临时目录(保持相对路径), 上传后删掉
            part = os.path.join(os.path.dirname(folder), f".part_{os.path.basename(folder)}_{idx}")
            shutil.rmtree(part, ignore_errors=True)
            for rel in chunk:
                s_path, d_path = os.path.join(folder, rel), os.path.join(part, rel)
                os.makedirs(os.path.dirname(d_path), exist_ok=True)
                try:
                    os.link(s_path, d_path)
                except OSError:
                    shutil.copyfile(s_path, d_path)
        tag = f"[{idx}/{len(chunks)}]" if len(chunks) > 1 else ""
        try:
            if dst_platform == "scope":
                api = _scope_api(org)
                print(f"[transfer] 魔塔 upload_folder(repo_id={repo_id}, files={len(chunk)}{tag}, "
                      f"max_workers=5, 一次 commit): {commit_msg}", flush=True)
                global_limiter.wait()
                api.upload_folder(repo_id, "model", folder_path=part, max_workers=5,
                                  disable_tqdm=True, commit_message=f"{commit_msg} {tag}".strip(),
                                  ignore_patterns=[".git", "*.tmp", ".*~", _UPLOAD_CACHE_FILE])
            else:
                from openmind_hub import upload_folder
                print(f"[transfer] 魔乐 upload_folder(repo_id={repo_id}, files={len(chunk)}{tag}, "
                      f"一次 commit(并发由 SDK pipeline 负责)): {commit_msg}", flush=True)
                global_limiter.wait()
                upload_folder(repo_id, folder_path=part, token=org.modelers.token,
                              commit_message=f"{commit_msg} {tag}".strip())
        finally:
            if part != folder:
                shutil.rmtree(part, ignore_errors=True)


def _maybe_clean_stage(conn, task, stage: str, err: BaseException | None) -> str:
    """暂存目录生命周期(2026-10-08 定稿): 成功→删; 失败且还会重试→保留; 终态失败→删。

    保留的意义: 下次尝试由 SDK 校验后跳过已下好的文件、半截文件续传(实测), 不再从零重下。
    """
    from env_tools import tasks as _t
    keep = False
    attempts = max_attempts = 0
    if err is not None:
        t = dict(task)
        attempts = int(t.get("attempts") or 0)
        max_attempts = int(t.get("max_attempts") or 3)
        retryable = _t.classify_error(f"{type(err).__name__}: {err}")
        keep = retryable and attempts < max_attempts
    if keep:
        print(f"[transfer] 暂存保留待重试(第 {attempts}/{max_attempts} 次): {stage}", flush=True)
        return "kept"
    shutil.rmtree(stage, ignore_errors=True)
    return "cleaned"


def _reset_file_fail(conn, org, model: str) -> None:
    """同步成功 → 清该模型的失败计数与退避(与 tasks.fail_task 的退避配对)。"""
    conn.execute("UPDATE models SET files_fail_count=0, files_retry_at=NULL "
                 "WHERE org=? AND name=?", (org.id, model))
    conn.commit()


def sweep_stale_stage(conn, org, now: int | None = None) -> dict:
    """清理陈旧暂存与缓存(2026-10-08 fix 4)。

    - `updown_weights/<model>`、`compare_weights/<model>`: 超过 `sync.stage_stale_days`
      (默认 7 天)未改动 → 删(失败任务保留的暂存也受约束, 避免长期占盘);
    - SDK 缓存(`<weights>/.cache/*` 下 models--* 目录): 超过 `sync.cache_stale_days`
      (默认 30 天)未改动 → 删(缓存是复用基础, 阈值放宽; 删除只损失"下次重下")。
    对账只在任务队列为空时执行 → 不会删到正在使用的目录。
    """
    import time as _t
    from env_tools import db as _db
    now = now or int(_t.time())
    stage_days = int(_db.get_app_config(conn, org.id, "sync.stage_stale_days", 7))
    cache_days = int(_db.get_app_config(conn, org.id, "sync.cache_stale_days", 30))
    removed: list[str] = []

    def _newest_mtime(path: str) -> float:
        newest = 0.0
        for r, ds, fs in os.walk(path):
            for x in list(fs) + list(ds):
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(r, x)))
                except OSError:
                    pass
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            pass
        return newest

    def _sweep(root: str, days: int) -> None:
        if not os.path.isdir(root):
            return
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if not os.path.isdir(full):
                continue
            if now - _newest_mtime(full) > days * 86400:
                shutil.rmtree(full, ignore_errors=True)
                removed.append(os.path.relpath(full, org.weights_root))

    for sub in ("updown_weights", "compare_weights"):
        _sweep(os.path.join(org.weights_root, sub), stage_days)
    ms_cache, om_cache = _sdk_caches()
    for cache in (ms_cache, om_cache):
        _sweep(cache, cache_days)
    if removed:
        print(f"[transfer] 陈旧清理({org.id}): 暂存>{stage_days}d / 缓存>{cache_days}d → "
              f"{len(removed)} 个目录: {removed[:5]}", flush=True)
    return {"removed": len(removed), "names": removed[:20]}



def _refresh_baselines(conn, org, model: str, now: int | None = None) -> None:
    """同步成功后回写两侧基线(全量刷新指纹; 变化路径的旧值此刻可安全覆盖)。"""
    import time as _t
    from env_tools.reconcile import _upsert_file_rows
    now = now or int(_t.time())
    for platform in ("scope", "modelers"):
        try:
            files = fetch_remote_files(conn, org, platform, model)
        except Exception as e:
            print(f"[transfer] 基线刷新 {platform} 拉取失败: {e}")
            continue
        repo_id = _repo_id(org, platform, model)
        from env_tools import poison as _p
        for f in files:
            f["poison"] = _p.classify(f["path"], f["size"])
        _upsert_file_rows(conn, org.id, platform, repo_id, files, now, set())
        # 操作即落库: 同步成功顺带刷新该侧 models 行(避免靠下轮对账才更新)
        conn.execute(
            "UPDATE models SET last_seen_at=? WHERE org=? AND platform=? AND repo_id=?",
            (now, org.id, platform, repo_id))
    conn.commit()


_SCOPE_LM_WARNED = False          # 魔塔单仓 last_modified 接口不可用时只提示一次


def repo_last_modified(org, platform: str, model: str) -> int | None:
    """单仓 last_modified(与各自 model_list 同源同值; epoch 秒), 取不到 → None。

    2026-09-14 调度重构: 队列生成阶段只拉 model_list 并用 last_modified 判"该 repo 是否
    有文件级变动"; 任务执行阶段核验完成后, 用本函数把**该仓当前版本**回写为"已核验"
    (models.files_verified_lm) —— 必须用同一来源的值, 否则两侧永远判不相等。
      魔塔: `get_repo(repo_id, repo_type='model').last_modified`(datetime; 实测与列表值逐秒一致)
            —— 注意: 生产 SDK 是 `modelscope_hub`, **没有** `get_model`;
            `get_model` 只在另一套 `modelscope` 包里存在, 故只作为可选回退。
      魔乐: `model_info(repo_id).last_modified`(epoch; 实测与列表值完全一致)
    异常/取不到 → None: 调用方会回退到 models.last_modified(见 record_files_verified)。
    """
    global _SCOPE_LM_WARNED
    repo_id = _repo_id(org, platform, model)
    try:
        global_limiter.wait()
        if platform == "scope":
            api = _scope_api(org)
            if hasattr(api, "get_repo"):          # modelscope_hub(生产): 正确通道
                ri = api.get_repo(repo_id, repo_type="model")
                return _to_epoch(_get(ri, "last_modified", None))
            if hasattr(api, "get_model"):         # 兼容旧/另一套 SDK
                mo = api.get_model(repo_id, revision="master")
                raw = mo.get("UpdatedAt") if isinstance(mo, dict) else _get(mo, "UpdatedAt", None)
                return _to_epoch(raw)
            if not _SCOPE_LM_WARNED:
                _SCOPE_LM_WARNED = True
                print("[transfer] 魔塔 SDK 无 get_repo/get_model → 单仓 last_modified "
                      "回退 models.last_modified(本轮 model_list 值)", flush=True)
            return None
        from openmind_hub import model_info
        mi = model_info(repo_id, token=org.modelers.token)
        raw = _get(mi, "last_modified", None) if not isinstance(mi, dict) else mi.get("last_modified")
        return _to_epoch(raw)
    except Exception as e:
        if not _SCOPE_LM_WARNED:
            _SCOPE_LM_WARNED = True
            print(f"[transfer] 读取 {platform}/{model} last_modified 失败"
                  f"({type(e).__name__}: {e}); 后续同类失败静默, 回退 models.last_modified",
                  flush=True)
        return None


def record_files_verified(conn, org, model: str, scope_lm: int | None = None,
                          modelers_lm: int | None = None) -> dict:
    """把"该侧文件树已核验"的仓版本写入 models.files_verified_lm(v8, 2026-09-14)。

    **只在核验成功时调用**: 采纳/无差异的 diff 之后, 或同步/删除成功之后。
    取值优先级: 调用方传入(全量扫描时来自 model_list, 零请求) → 单仓接口
    (repo_last_modified) → **models.last_modified 兜底**(每轮 model_list 刷新, 最多
    滞后一个对账周期, 不会永久 dirty)。2026-09-14 生产事故: 魔塔单仓接口取不到时
    只写了魔乐一侧 → 魔塔侧永远 dirty → 同一模型每 15 分钟入队一次空转。
    """
    def _db_lm(plat: str) -> int | None:
        row = conn.execute(
            "SELECT last_modified FROM models WHERE org=? AND platform=? AND repo_id=?",
            (org.id, plat, _repo_id(org, plat, model))).fetchone()
        return (row["last_modified"] if row is not None else None)

    out: dict = {}
    vals = {"scope": scope_lm, "modelers": modelers_lm}
    for plat in ("scope", "modelers"):
        lm = vals[plat]
        if lm is None:
            lm = repo_last_modified(org, plat, model)
        if lm is None:
            lm = _db_lm(plat)                 # 兜底: models.last_modified(本轮 list 值)
        if lm is None:
            continue
        conn.execute(
            "UPDATE models SET files_verified_lm=? WHERE org=? AND platform=? AND repo_id=?",
            (int(lm), org.id, plat, _repo_id(org, plat, model)))
        out[plat] = int(lm)
    conn.commit()
    if out:
        print(f"[transfer] {model} 已核验仓版本回写: "
              + " ".join(f"{k}={v}" for k, v in sorted(out.items())), flush=True)
    return out


def _stage_readme(conn, org, dst_platform: str, model: str, src_text: str,
                  stage_dir: str) -> None:
    """README 走管线变换后落盘到上传暂存目录。"""
    from env_tools import pipeline
    out = pipeline.transform_readme(conn, org, src_text, dst_platform)
    import os
    os.makedirs(stage_dir, exist_ok=True)
    with open(os.path.join(stage_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write(out)


def _set_row_is_init(conn, org_id: str, platform: str, repo_id: str, value: int) -> None:
    conn.execute(
        "UPDATE files SET is_init=? WHERE org=? AND platform=? AND repo_id=? AND path='README.md'",
        (value, org_id, platform, repo_id))


def _license_for_scope(conn, org, model: str) -> str | None:
    """魔乐 README front matter license → 魔塔 create_repo license 关键字(2026-09 反向对齐)。

    魔乐模型元数据不暴露 license(实测 model_info 无 cardData/license 字段、tags 为空),
    只能从魔乐 README front matter 读取(v1/v2 同步写入的 `license: <kw>`);
    平台 init README / 无 README / 无 front matter → None(魔塔建仓用平台默认)。
    返回魔塔 License 枚举值字符串(如 'Apache-2.0'), 词表外 → None。
    """
    from env_tools import pipeline
    from env_tools.reconcile import _download_readme_text
    text = _download_readme_text(conn, org, "modelers", model)
    if not text or pipeline.detect_init_content(text):
        return None
    fm, _body = pipeline.split_front_matter(text)
    raw_lic = None
    if fm:
        try:
            import yaml as _yaml
            parsed = _yaml.safe_load(fm)
            if isinstance(parsed, dict):
                raw_lic = parsed.get("license") or parsed.get(org.modelers.license_name)
        except Exception:
            raw_lic = None
    if not raw_lic:
        return None
    norm = pipeline.normalize_license(conn, org.id, str(raw_lic))
    try:
        from modelscope_hub import License as _Lic
    except Exception:
        return None
    for member in _Lic:
        if pipeline.canonical_license(str(member.value)) == norm:
            return str(member.value)
    return None


def _license_kw_for_modelers(conn, org, model: str) -> str | None:
    """魔塔模型元数据 license → 魔乐建仓 license 关键字(2026-09 license 对齐)。

    模型级建仓(to_modelers)时把魔塔卡片 license 带给魔乐 create_repo 的 license
    参数; 魔塔无 license 记录 → None(保持原默认)。README init/缺失不同步时,
    license 不再依赖 README front matter 传递, 由建仓参数兜底(V1 曾靠 README
    front matter 传 license, 删 README 即丢; 见用户 2026-09 提示)。
    """
    from env_tools import pipeline
    row = conn.execute(
        "SELECT license_raw FROM models WHERE org=? AND platform='scope' AND repo_id=?",
        (org.id, f"{org.scope.repo_name}/{model}")).fetchone()
    if row is None or not row["license_raw"]:
        return None
    norm = pipeline.normalize_license(conn, org.id, row["license_raw"])
    # 词表外: license_for_platform 返回 'other' 并告警; 'other' 恒在魔乐词表, 安全
    return pipeline.license_for_platform(org.modelers, norm)


def _source_private(conn, org, platform: str, model: str) -> bool:
    """建仓可见性参考: 源平台 models 行的 private(对账每轮 upsert; 无行默认 public)。"""
    repo_id = _repo_id(org, platform, model)
    row = conn.execute("SELECT private FROM models WHERE org=? AND platform=? AND repo_id=?",
                       (org.id, platform, repo_id)).fetchone()
    return bool(row and row["private"])


def _mark_synced(conn, org_id: str, platform: str, repo_id: str,
                 paths: list[str], now: int) -> None:
    """同步成功后回写 last_synced_at(仅本次成功路径)。"""
    if not paths:
        return
    marks = ",".join("?" for _ in paths)
    conn.execute(
        f"UPDATE files SET last_synced_at=? WHERE org=? AND platform=? AND repo_id=? "
        f"AND path IN ({marks})", [now, org_id, platform, repo_id, *paths])
    conn.commit()


def _readme_download_and_stage(conn, org, src: str, dst: str, model: str,
                               stage_dir: str) -> bool:
    """下载源侧 README → 变换暂存(供整批上传)。

    守卫(设计 §10.3): 源正文为空/平台初始化内容 → 不同步(各显示各的),
    标记源侧 is_init=1 并返回 False; 有效正文 → 变换暂存, 目标侧 is_init=0。
    """
    import os
    import shutil
    import tempfile
    from env_tools import pipeline
    tmp = tempfile.mkdtemp(prefix="readme_")
    try:
        _download_one(org, src, _repo_id(org, src, model), "README.md", tmp)
        text = open(os.path.join(tmp, "README.md"), encoding="utf-8").read()
        if pipeline.detect_init_content(text):
            _set_row_is_init(conn, org.id, src, _repo_id(org, src, model), 1)
            conn.commit()
            return False
        _stage_readme(conn, org, dst, model, text, stage_dir)
        _set_row_is_init(conn, org.id, dst, _repo_id(org, dst, model), 0)
        conn.commit()
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def sync_model(conn, org, task) -> dict:
    """model_sync 复合任务: 预检(调用方已做)→ 建仓 → 下载 → README 变换 → 上传 → 校验 → 回写基线。

    方向: to_modelers = 源魔塔→目标魔乐; to_scope = 源魔乐→目标魔塔。
    紧急抢占中断: worker 被杀后任务重做 = 本流程重跑(ensure_repo 幂等,
    上传服务端哈希去重, 收敛)。
    """
    import os
    import shutil
    import time as _t
    direction = task["direction"] or "to_modelers"
    src = "scope" if direction == "to_modelers" else "modelers"
    dst = "modelers" if direction == "to_modelers" else "scope"
    model = task["model"]
    now = int(_t.time())
    stage = org.updown_dir(model)
    # 2026-10-08: 不再每次清空暂存 —— 失败重试时由 SDK 校验后跳过已下好的文件、
    # 半截文件续传(实测断网也能走完"全部跳过"), 避免 TB 级权重反复重下。
    os.makedirs(stage, exist_ok=True)

    # 1) 源文件集合(排除毒瘤/平台托管; README 单独走管线)
    files = fetch_remote_files(conn, org, src, model)
    from env_tools import poison as _p
    normal = [f for f in files if not _p.is_excluded(_p.classify(f["path"], f["size"]))]
    readme = next((f for f in files if f["path"] == "README.md"), None)

    # 2) 目标建仓(幂等): 可见性参考源侧(魔塔为主=参考魔塔; 反向=参考魔乐);
    #    license 传递(2026-09): to_modelers 从魔塔模型元数据 → 魔乐 create_repo;
    #    to_scope 从魔乐 README front matter → 魔塔 create_repo(魔乐元数据不暴露 license)
    if dst == "modelers":
        license_kw = _license_kw_for_modelers(conn, org, model)
    elif dst == "scope":
        license_kw = _license_for_scope(conn, org, model)
    else:
        license_kw = None
    created = ensure_repo(conn, org, dst, model, license_kw,
                          private=_source_private(conn, org, src, model))
    print(f"[transfer] model_sync {model} {src}→{dst} 建仓={created} 文件数={len(normal)}")

    # 3) 下载 + README 变换
    paths = [f["path"] for f in normal if f["path"] != "README.md"]
    sizes = {f["path"]: (f["size"] or 0) for f in normal}
    total_bytes = sum(sizes.get(p, 0) for p in paths)
    task_id = task["id"]

    def _on_dl(i: int, n: int, path: str) -> None:
        done = sum(sizes.get(q, 0) for q in paths[:i])
        _progress(conn, task_id, f"下载 {i}/{n} ({done / 1e9:.2f}/{total_bytes / 1e9:.2f} GB)")
        print(f"[sync] {model} model_sync {src}→{dst} 下载 {i}/{n} "
              f"({done / 1e9:.2f}/{total_bytes / 1e9:.2f} GB): {path}", flush=True)

    if paths:
        # 期望元数据取源侧实时文件树(最权威) → 下载后自查(魔塔 SDK 会吞掉单文件失败)
        _expect = {f["path"]: (f.get("size"), f.get("sha256"), f.get("blob_id"))
                   for f in normal}
        _download_set(conn, org, src, model, paths, stage, on_file=_on_dl, expect=_expect)
    if readme is not None:
        _readme_download_and_stage(conn, org, src, dst, model, stage)

    # 3.5) 上传前 prune: 只保留"期望集合"(本次 paths + README)
    #      实测两个 SDK 都不清理 <file>.incomplete 等残留, 而 upload_folder 会全传上去
    expected = set(paths) | ({"README.md"} if readme is not None else set())
    pruned = _prune_stage(stage, expected)
    if pruned:
        print(f"[transfer] {model} 上传前清理暂存残留 {len(pruned)} 个: {pruned[:5]}", flush=True)

    # 4) 上传(一次 commit)
    if os.listdir(stage):
        _progress(conn, task_id, f"上传 {len(paths)} 个文件 ({total_bytes / 1e9:.2f} GB, 一次 commit)")
        print(f"[sync] {model} model_sync 开始批量上传 {len(paths)} 个文件 "
              f"({total_bytes / 1e9:.2f} GB, 一次 commit) ...", flush=True)
        _upload_dir(org, dst, model, stage, f"sync v2: {model} {src}→{dst}", conn=conn)

    # 5) 校验 + 6) 回写基线(含 last_synced_at 标记目标侧)
    _refresh_baselines(conn, org, model, now)
    # 模型级同步完成 = 该仓文件树此刻已同步 → 回写"已核验仓版本"(v8, 见 record_files_verified)
    record_files_verified(conn, org, model)
    synced = [p for p in paths]
    if readme is not None:
        synced.append("README.md")
    _mark_synced(conn, org.id, dst, _repo_id(org, dst, model), synced, now)
    _maybe_clean_stage(conn, task, stage, None)      # 成功 → 删暂存(2026-10-08 policy A)
    _reset_file_fail(conn, org, model)               # 成功 → 清失败退避
    _log_list(f"{model} model_sync {src}→{dst} 上传(一次 commit)", synced,
              total_bytes=sum(sizes.get(p, 0) for p in synced))
    return {"ok": True, "uploaded": len(paths) + (1 if readme is not None else 0),
            "direction": direction}


def _log_list(title: str, paths: list[str], total_bytes: int | None = None,
              head: int = 20) -> None:
    """打印文件清单日志(2026-09: 每次 commit 上传/删除了哪些文件, 列清楚)。

    ≤head 个全列; 更多则列前 head 个 + 计数与总量, 具体清单仍可在平台 commit 里查。
    """
    n = len(paths)
    size = ""
    if total_bytes:
        size = f" ({total_bytes / 1e9:.2f} GB)" if total_bytes >= 1e9 else f" ({total_bytes / 1e6:.1f} MB)"
    print(f"[sync] {title}: {n} 个文件{size}")
    for p in paths[:head]:
        print(f"[sync]   - {p}")
    if n > head:
        print(f"[sync]   ... 其余 {n - head} 个(完整清单见平台 commit)")


def _alert_delete_manual(conn, org_id: str, model: str, paths: list[str]) -> None:
    """删除保护告警(权重/超大文件需人工确认, 去重: 同 model 只告警一次)。"""
    row = conn.execute(
        "SELECT 1 FROM alerts WHERE org=? AND model=? AND error LIKE 'delete_manual%' LIMIT 1",
        (org_id, model)).fetchone()
    if row is None:
        from env_tools import tasks as _t
        _t.insert_alert(conn, org_id, None, model, "warn",
                        "delete_manual: 魔乐独有文件含权重/超大文件, 已跳过自动删除, "
                        "请人工确认后手动处理: " + ", ".join(paths[:10])
                        + ("..." if len(paths) > 10 else "")
                        + f" | 确认后执行: python server-work.py clean --org {org_id} "
                          f"--model {model} --yes")
        conn.commit()


def sync_files(conn, org, task) -> dict:
    """file_batch 任务(单向, 魔塔为准): 执行时重算 diff →
    删除集(魔乐独有 extra 按保护规则 + 魔塔已删文件同轮删除, 无宽限)→ 魔乐整批删除;
    上传集(to_modelers: 魔塔增改/魔乐缺/魔乐被改)→ 以魔塔当前版纠正魔乐(README 走管线)。"""
    import os
    import shutil
    import time as _t
    from env_tools import db as _db
    direction = task["direction"] or "to_modelers"
    model = task["model"]
    now = int(_t.time())
    if direction != "to_modelers":
        return {"ok": True, "note": f"file_batch 忽略方向 {direction}(单向仅 to_modelers)"}

    from env_tools.reconcile import compute_file_diff
    d = compute_file_diff(conn, org, model, now)
    if d["abort"]:
        return {"ok": True, "note": "魔塔文件集拉取失败/为空, 本轮跳过(防误删)"}
    if d["adopt"]:
        # 采纳模式(首次为该仓建两侧基线): 先跑 README 四分支(建 is_init 标记, 可能判定
        # "魔乐 init README 需用魔塔版覆盖"), 再**重算一次 diff** —— 此时基线已落库,
        # 走正常差异流程(README 覆盖在本任务内完成), 而不是把活留给下一轮
        # (2026-09-14 调度重构: 本任务很可能就是"无基线"入队的那一个)。
        from env_tools import reconcile as _rc
        _rc._adopt_readme_check(conn, org, model, d, {"file_batch_enq": 0})
        d = compute_file_diff(conn, org, model, now)
        if d["abort"]:
            return {"ok": True, "note": "采纳后复核: 魔塔文件集拉取失败/为空, 本轮跳过"}
        if d["adopt"]:
            record_files_verified(conn, org, model)
            return {"ok": True, "note": "采纳模式, 无任务动作"}

    # 1) 上传集(魔塔当前版纠正魔乐): 新增 + 同名覆盖 → 一次 commit 上传
    #    (先上传后删除 —— 任务执行窗口内魔乐只会"多文件"不会"缺文件")
    ups = sorted(p for p in d["to_modelers"] if p != "README.md")
    stage = org.updown_dir(model)
    os.makedirs(stage, exist_ok=True)    # 2026-10-08: 不再清空(失败重试由 SDK 校验跳过/续传)
    uploaded: list[str] = []
    task_id = task["id"]
    _ups_sizes: dict[str, int] = {}
    for p in ups:
        row = conn.execute(
            "SELECT size FROM files WHERE org=? AND platform='scope' AND repo_id=? AND path=?",
            (org.id, _repo_id(org, "scope", model), p)).fetchone()
        _ups_sizes[p] = (row["size"] if row else 0) or 0
    _total_up = sum(_ups_sizes.values())

    def _on_dl(i: int, n: int, path: str) -> None:
        done = sum(_ups_sizes.get(q, 0) for q in ups[:i])
        _progress(conn, task_id, f"下载 {i}/{n} ({done / 1e9:.2f}/{_total_up / 1e9:.2f} GB)")
        print(f"[sync] {model} 下载 {i}/{n} ({done / 1e9:.2f}/{_total_up / 1e9:.2f} GB): {path}",
              flush=True)

    if ups:
        _expect = {p: (r.get("size"), r.get("sha256"), r.get("blob_id"))
                   for p, r in (d.get("cur_scope") or {}).items()}
        _download_set(conn, org, "scope", model, ups, stage, on_file=_on_dl, expect=_expect)
    if "README.md" in d["to_modelers"]:
        _readme_download_and_stage(conn, org, "scope", "modelers", model, stage)
    # 上传前 prune: 只保留"期望集合"(本次 ups + README)
    expected = set(ups) | ({"README.md"} if "README.md" in d["to_modelers"] else set())
    _pruned = _prune_stage(stage, expected)
    if _pruned:
        print(f"[transfer] {model} 上传前清理暂存残留 {len(_pruned)} 个: {_pruned[:5]}", flush=True)
    if os.listdir(stage):
        _progress(conn, task_id, f"上传 {len(ups)} 个文件 ({_total_up / 1e9:.2f} GB, 一次 commit)")
        print(f"[sync] {model} 开始批量上传 {len(ups)} 个文件 ({_total_up / 1e9:.2f} GB, 一次 commit)",
              flush=True)
        _upload_dir(org, "modelers", model, stage,
                    f"sync v2 file_batch: {model} →modelers", conn=conn)
        uploaded = ups + (["README.md"] if "README.md" in d["to_modelers"] else [])
        _progress(conn, task_id, f"上传完成 {len(uploaded)} 个文件")
    # 2026-10-08: 成功 → 删暂存 + 复位失败退避; 失败 → 暂存保留待重试(task_runner 决定终态清理)
    _maybe_clean_stage(conn, task, stage, None)
    _reset_file_fail(conn, org, model)
    if uploaded:
        _log_list(f"{model} file_batch 上传(一次 commit)", uploaded,
                  total_bytes=sum(_ups_sizes.get(p, 0) for p in uploaded))

    # 2) 删除集(2026-09 对齐 v1: 文件级删除无宽限, 检测到即删, 同轮收束):
    #    - missing_scope(魔塔已删的同步文件)→ 自动删(一次 commit);
    #    - extra(魔乐独有)→ 严格镜像(auto_delete_extra=true, 默认)一律自动删
    #      (含 .safetensors/>50MB, 避免"新版已传旧版未删"的共存窗口);
    #      auto_delete_extra=false 时降级为人工确认(delete_manual 告警 + clean 命令);
    #    - 隐藏/毒瘤文件永远不删(2026-09 全隐藏过滤; 防历史 poison=NULL 行漏网)。
    from env_tools import poison as _poison
    auto_extra = bool(_db.get_app_config(conn, org.id, "sync.auto_delete_extra", False))
    manual = set(d["extra"])
    dels = set()
    for pth in d["missing_scope"]:
        row = conn.execute(
            "SELECT size FROM files WHERE org=? AND platform='scope' "
            "AND repo_id=? AND path=?",
            (org.id, _repo_id(org, "scope", model), pth)).fetchone()
        if row and _poison.is_excluded(_poison.classify(pth, row["size"] or 0)):
            continue                 # 隐藏/毒瘤: 永不删除
        dels.add(pth)
    for pth in list(manual):
        row = conn.execute(
            "SELECT size FROM files WHERE org=? AND platform='modelers' AND repo_id=? AND path=?",
            (org.id, _repo_id(org, "modelers", model), pth)).fetchone()
        if row and _poison.is_excluded(_poison.classify(pth, row["size"] or 0)):
            manual.discard(pth)
            continue                 # 隐藏/毒瘤: 永远不删
        if auto_extra:
            dels.add(pth)
            manual.discard(pth)
    if manual:
        _alert_delete_manual(conn, org.id, model, sorted(manual))
        _log_list(f"{model} 魔乐独有(严格镜像关闭, 待人工确认)", sorted(manual))
    if dels:
        sizes = {}
        for pth in dels:
            row = conn.execute(
                "SELECT size FROM files WHERE org=? AND platform='modelers' AND repo_id=? AND path=?",
                (org.id, _repo_id(org, "modelers", model), pth)).fetchone()
            sizes[pth] = (row["size"] if row else 0) or 0
        _progress(conn, task_id, f"删除 {len(dels)} 个文件 ({sum(sizes.values()) / 1e9:.2f} GB, 一次 commit)")
        print(f"[sync] {model} 开始删除 {len(dels)} 个魔乐独有/已删文件 "
              f"({sum(sizes.values()) / 1e9:.2f} GB, 一次 commit) ...", flush=True)
        _delete_files(conn, org, "modelers", model, sorted(dels))
        _log_list(f"{model} file_batch 删除(一次 commit)", sorted(dels),
                  total_bytes=sum(sizes.values()))

    _refresh_baselines(conn, org, model, now)
    # 文件级同步成功 → 该仓两侧文件树此刻与基线一致 → 回写"已核验仓版本"(v8):
    # 队列生成阶段据此判 dirty, 失败路径不会走到这里(异常向上抛 → 任务失败 → 保持 dirty)
    record_files_verified(conn, org, model)
    if uploaded:
        _mark_synced(conn, org.id, "modelers", _repo_id(org, "modelers", model), uploaded, now)
    return {"ok": True, "deleted": len(dels), "uploaded": len(uploaded)}


def _delete_files(conn, org, platform: str, model: str, paths: list[str]) -> dict:
    """目标侧批量删除文件(单向规则 §5.3)。

    魔乐: create_commit + 多个 CommitOperationDelete = 一次 commit 批量删(实测可行);
    单 commit 失败 → 回退逐个 OmApi.delete_file;
    魔塔: api.delete_files 批量(同步器不主动用, 魔塔删除人工; 保留供未来)。
    删除成功 → 清 DB 双侧对应行。
    """
    repo_id = _repo_id(org, platform, model)
    if not paths:
        return {"ok": True, "deleted": []}
    if platform == "scope":
        api = _scope_api(org)
        print(f"[transfer] 魔塔 delete_files(repo_id={repo_id}, files={len(paths)}, 一次 commit)",
              flush=True)
        global_limiter.wait()
        api.delete_files(repo_id, "model", list(paths),
                         commit_message=f"sync v2 删除 {len(paths)} 文件")
    else:
        from openmind_hub import create_commit, CommitOperationDelete
        print(f"[transfer] 魔乐 create_commit(删除 {len(paths)} 个文件, 一次 commit)",
              flush=True)
        try:
            global_limiter.wait()
            create_commit(repo_id,
                          operations=[CommitOperationDelete(path_in_repo=p) for p in paths],
                          token=org.modelers.token,
                          commit_message=f"sync v2 批量删除 {len(paths)} 文件")
        except Exception:
            # 回退: 逐个单文件删除
            from openmind_hub.plugins.openmind import om_api as _oma
            print(f"[transfer] 魔乐 create_commit 删除失败 → 回退 OmApi.delete_file × {len(paths)}"
                  f"(逐个 commit)", flush=True)
            for p in paths:
                global_limiter.wait()
                _oma.OmApi().delete_file(path_in_repo=p, repo_id=repo_id,
                                         token=org.modelers.token, commit_message="sync v2 删除")
    # 清 DB 双侧该 path 行
    marks = ",".join("?" for _ in paths)
    for plat in ("scope", "modelers"):
        rid = _repo_id(org, plat, model)
        conn.execute(f"DELETE FROM files WHERE org=? AND platform=? AND repo_id=? AND path IN ({marks})",
                     [org.id, plat, rid, *paths])
    conn.commit()
    # 操作即落库: 审计留痕(按批一条)
    from env_tools import tasks as _t
    _t.insert_alert(conn, org.id, None, model, "warn",
                    f"audit_delete_file: {platform} 删除 {len(paths)} 个文件: "
                    + ", ".join(sorted(paths)[:6]) + ("..." if len(paths) > 6 else ""))
    return {"ok": True, "deleted": list(paths)}


def delete_repo_task(conn, org, task) -> dict:
    """repo_delete 任务(单向, 魔塔删除传播执行端)。

    规则(2026-09 定稿): 魔塔删除确认后【不设 repo_created_by_us 护栏】直接删魔乐 repo;
    目标=魔塔的分支保留人工提示(魔塔删除仅网页, API 401);
    成功后: 清 DB 双侧行 + GitCode 删除对齐(org.has_gitcode 时)。
    """
    direction = task["direction"] or "to_modelers"
    model = task["model"]
    if direction == "to_scope":
        raise RuntimeError(
            "401 魔塔删除仅限网页控制台(API restricted): 请人工在 "
            f"https://www.modelscope.cn/models/{_repo_id(org, 'scope', model)} 删除, "
            "再清 DB 行")
    repo_id = _repo_id(org, "modelers", model)
    # 魔乐: SDK delete_repo 缺陷(不发 JSON body → 400 EOF), 先 SDK 后裸请求回退
    from openmind_hub import delete_repo
    try:
        global_limiter.wait()
        delete_repo(repo_id, token=org.modelers.token, repo_type="model", missing_ok=True)
    except Exception as e:
        if "bad_request_body" not in f"{type(e).__name__}: {e}":
            raise
        import requests as _req
        from openmind_hub import model_info
        global_limiter.wait()
        info = model_info(repo_id, token=org.modelers.token)
        global_limiter.wait()
        r = _req.delete(f"https://modelers.cn/api/v1/model/{info.id}",
                        headers={"Authorization": f"Bearer {org.modelers.token}",
                                 "Content-Type": "application/json"},
                        json={}, timeout=60)
        if r.status_code not in (200, 204):
            raise RuntimeError(f"魔乐裸 DELETE 失败 HTTP {r.status_code}: {r.text[:200]}")
    # 清 DB 双侧行
    for plat in ("modelers", "scope"):
        rid = _repo_id(org, plat, model)
        conn.execute("DELETE FROM files WHERE org=? AND repo_id=?", (org.id, rid))
        conn.execute("DELETE FROM models WHERE org=? AND repo_id=?", (org.id, rid))
    conn.commit()
    print(f"[transfer] repo_delete 完成: modelers/{model}")
    # 操作即落库: 审计留痕(repo_created_by_us 行已删, 靠此条留存审计信息)
    from env_tools import tasks as _t
    _t.insert_alert(conn, org.id, None, model, "warn",
                    f"audit_delete: 魔塔删除传播已执行, 魔乐 repo {repo_id} 已删除, "
                    "双侧 DB 行已清理; 魔塔本体若未删请人工网页处理")
    # GitCode 删除对齐(幂等)
    if org.has_gitcode:
        from env_tools import gitcode
        try:
            gitcode.gitcode_delete(conn, org, model)
        except Exception as e:
            print(f"[transfer] GitCode 删除对齐失败 {model}: {type(e).__name__}: {e}")
    return {"ok": True, "deleted": repo_id}


def gitcode_import_task(conn, org, task) -> dict:
    """gitcode_import 任务执行入口: 转调 env_tools.gitcode.gitcode_import。"""
    from env_tools import gitcode
    return gitcode.gitcode_import(conn, org, task["model"])
