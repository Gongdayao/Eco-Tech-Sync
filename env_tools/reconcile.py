# -*- coding: utf-8 -*-
"""env_tools.reconcile — 单向对账(以魔塔为准, 2026-09 定稿, V2-DESIGN.md §2)

模型级: 准入(safetensors)/魔塔新增→魔乐/魔乐独有权重→反向建塔/魔塔删除→魔乐传播(无护栏)/
        魔乐缺失→即时补齐/可见性对比(魔塔为主)/采纳;
文件级: 魔塔当前文件集 = 期望, 魔乐偏离纠正器(缺→补, 不一致→魔塔版覆盖, 独有→删,
        魔塔删→魔乐删); 魔塔拉取失败/为空 → abort(防误删风暴);
GitCode: 纯镜像顺带动作; 30d 强哈希(API 可信度保险)。
"""
from __future__ import annotations

import hashlib
import time

from env_tools import db, tasks
from env_tools.transfer import fetch_remote_models, fetch_remote_files


# ---------------------------------------------------------------- 小工具
def _now() -> int:
    return int(time.time())


def _commit(conn) -> None:
    conn.commit()


def _enqueue(conn, org_id: str, kind: str, model: str, direction: str | None = None,
             priority: int = tasks.PRIORITY_NORMAL) -> bool:
    _, created = tasks.enqueue_task(conn, org_id, kind, model,
                                    direction=direction, created_by="reconcile")
    return created


def _alert_once(conn, org_id: str, model: str, prefix: str, level: str, msg: str) -> None:
    """同类同模型告警去重: alerts 表 error LIKE '前缀%' 已存在则不再重复。"""
    row = conn.execute(
        "SELECT 1 FROM alerts WHERE org=? AND model=? AND error LIKE ? LIMIT 1",
        (org_id, model, prefix + "%")).fetchone()
    if row is None:
        tasks.insert_alert(conn, org_id, None, model, level, f"{prefix}: {msg}")


def _upsert_model(conn, org_id: str, m: dict, now: int) -> None:
    conn.execute(
        """INSERT INTO models(org, platform, repo_id, owner, name, visibility, private,
             gated, login_required, description, downloads, likes, created_at,
             last_modified, license_raw, display_name, file_size, tags_json, tasks_json,
             first_seen_at, last_seen_at)
           VALUES(:org,:platform,:repo_id,:owner,:name,:visibility,:private,
             :gated,:login_required,:description,:downloads,:likes,:created_at,
             :last_modified,:license_raw,:display_name,:file_size,:tags_json,:tasks_json,
             :now,:now)
           ON CONFLICT(org, platform, repo_id) DO UPDATE SET
             owner=excluded.owner, name=excluded.name, visibility=excluded.visibility,
             private=excluded.private, gated=excluded.gated,
             login_required=excluded.login_required, description=excluded.description,
             downloads=excluded.downloads, likes=excluded.likes,
             created_at=excluded.created_at, last_modified=excluded.last_modified,
             license_raw=excluded.license_raw, display_name=excluded.display_name,
             file_size=excluded.file_size, tags_json=excluded.tags_json,
             tasks_json=excluded.tasks_json, missing_since=NULL,
             last_seen_at=excluded.last_seen_at""",
        {**m, "org": org_id, "now": now},
    )


def _grace_seconds(conn, org_id: str, key_min: str, cycles: int) -> int:
    interval = db.get_app_config(conn, org_id, f"sync.{key_min}", 15)
    return int(cycles) * int(interval) * 60


def _has_safetensors(files: list[dict]) -> bool:
    return any(f["path"].endswith(".safetensors") for f in files)


def _mark_excluded(files: list[dict]) -> None:
    from env_tools import poison as poison_mod
    for f in files:
        f["poison"] = poison_mod.classify(f["path"], f["size"])


# ================================================================ 模型级(单向矩阵)
def model_level(conn, org, now: int | None = None) -> dict:
    now = now or _now()
    stat = {"fetched_scope": 0, "fetched_modelers": 0, "to_modelers": 0, "to_scope": 0,
            "repo_delete": 0, "adopted": 0, "skip_no_weights": 0, "skip_gated": 0,
            "gated_no_modelers": 0, "gated_hidden": 0, "gated_public": 0,
            "gated_converted": 0,
            "dual_upload": 0, "vis_mismatch": 0, "adopted_names": [], "seen": {}}

    if not org.scope.configured:
        return stat
    scope_list = fetch_remote_models(conn, org, "scope")
    modelers_list = fetch_remote_models(conn, org, "modelers") if org.modelers.configured else []
    stat["fetched_scope"], stat["fetched_modelers"] = len(scope_list), len(modelers_list)

    scope_names = {m["name"] for m in scope_list}
    modelers_names = {m["name"] for m in modelers_list}
    # gated(审批)权重一律过滤: 不入队/不告警/文件级不管(用户定稿 2026-09)
    gated_names = {m["name"] for m in scope_list if m.get("gated")}
    stat["skip_gated"] = len(gated_names)
    stat["seen"] = {"scope": scope_names, "modelers": modelers_names}

    scope_by_name = {m["name"]: m for m in scope_list}
    modelers_by_name = {m["name"]: m for m in modelers_list}

    # 上轮状态快照(SELECT 必须先于 upsert —— 矩阵判定/转换检测都基于"进入本轮前"的 DB)
    rows = conn.execute(
        "SELECT platform, repo_id, name, private, gated, visibility, missing_since "
        "FROM models WHERE org=? AND platform IN ('scope','modelers')", (org.id,)).fetchall()
    db_scope = {r["name"]: r for r in rows if r["platform"] == "scope"}
    db_modelers = {r["name"]: r for r in rows if r["platform"] == "modelers"}

    for m in scope_list:
        _upsert_model(conn, org.id, m, now)
    for m in modelers_list:
        _upsert_model(conn, org.id, m, now)
    _commit(conn)

    grace_s = _grace_seconds(conn, org.id, "model_interval_min",
                             db.get_app_config(conn, org.id, "sync.delete_grace_cycles", 3))

    # ---- 0.5) gated(审批)处理: 三态 + 状态转换检测(用户定稿 2026-09) ----
    #   三态: 魔乐无 → 静默剔除; 魔乐隐藏/私有(private=True)→ 完全不管;
    #         魔乐公开 → critical 告警(立即删除或隐藏该镜像);
    #   转换: 上轮非 gated → 本轮 gated:
    #         私有→gated → warn 提醒(权限转申请制);
    #         公开→gated → 由"魔乐公开"critical 覆盖(若魔乐已被隐藏则视为已处理)。
    for name in sorted(gated_names):
        old = db_scope.get(name)
        if old is not None and not old["gated"]:
            stat["gated_converted"] += 1
            if old["private"]:
                _alert_once(conn, org.id, name, "gated_converted", "warn",
                            "魔塔权重权限由【私有】转为申请制(gated), 请知悉; 魔乐侧若公开会自动 critical 提醒")
        mm = modelers_by_name.get(name)
        if mm is None:
            stat["gated_no_modelers"] += 1
        elif mm.get("private"):
            stat["gated_hidden"] += 1
        else:
            stat["gated_public"] += 1
            _alert_once(conn, org.id, name, "gated_public_on_modelers", "critical",
                        "gated(审批)权重在魔乐是公开可见! 请立即删除或隐藏该镜像(避免绕过审批分发)")
    _commit(conn)

    # ---- 1) 魔塔新增(在管名单出现): 有权重、非 gated → 同步魔乐 ----
    for name in sorted(scope_names - modelers_names):
        if name in db_scope:
            continue                    # 曾管理过(魔乐缺失补齐场景见下)
        files = _fetch_check(conn, org, "scope", name)
        if files is None:
            continue                    # 拉取失败本轮略过
        if not _has_safetensors(files):
            stat["skip_no_weights"] += 1
            continue
        if name in gated_names:
            continue                      # gated 一律略过(魔乐已有也不管)
        if _enqueue(conn, org.id, tasks.KIND_MODEL_SYNC, name, "to_modelers"):
            stat["to_modelers"] += 1
    _commit(conn)

    # ---- 2) 魔乐独有(魔塔 list 无): 准入 + 反向新增 / 双端上传告警 ----
    for name in sorted(modelers_names - scope_names):
        if name in db_scope:
            continue
        files = _fetch_check(conn, org, "modelers", name)
        if files is None:
            continue
        if not _has_safetensors(files):
            stat["skip_no_weights"] += 1
            continue
        from env_tools import transfer
        try:
            scope_exists = transfer.repo_exists(org, "scope", name)
        except Exception:
            continue                    # 网络异常本轮略过
        if scope_exists:
            stat["dual_upload"] += 1
            _alert_once(conn, org.id, name, "dual_upload", "warn",
                        "魔乐独有权重但魔塔 repo_exists=True(隐身/初始化中): "
                        "同权重疑似双端上传, 已略过; 待魔塔 list 出现后接管")
            continue
        if _enqueue(conn, org.id, tasks.KIND_MODEL_SYNC, name, "to_scope"):
            stat["to_scope"] += 1
    _commit(conn)

    # ---- 3) 魔塔 repo 消失 → 删除传播(无护栏) / 双侧消失清行 ----
    for name, r in db_scope.items():
        if name in scope_names:
            continue
        if r["gated"]:
            continue                    # gated 不传播删除
        if not _db_had_weights(conn, org.id, name):
            continue                    # 从未有权重 → 不管
        if r["missing_since"] is None:
            conn.execute("UPDATE models SET missing_since=? WHERE org=? AND platform='scope' AND name=?",
                         (now, org.id, name))
            _commit(conn)
            continue
        if now - r["missing_since"] < grace_s:
            continue
        # 宽限满: 魔乐还有 → 删魔乐(无护栏) + GitCode 对齐
        if name in modelers_names or name in db_modelers:
            if _enqueue(conn, org.id, tasks.KIND_REPO_DELETE, name,
                        "to_modelers" if (name in modelers_names or name in db_modelers) else None):
                stat["repo_delete"] += 1
            _alert_once(conn, org.id, name, "scope_delete_manual", "warn",
                        "魔塔 repo 已删除(本体重需人工网页处理), 同步器将删除魔乐/GitCode 镜像")
        else:
            # 两侧都没了 → 清行(最后痕迹); 操作即落库: 审计留痕
            conn.execute("DELETE FROM models WHERE org=? AND name=?", (org.id, name))
            conn.execute("DELETE FROM files WHERE org=? AND repo_id LIKE ?",
                         (org.id, f"%/{name}"))
            tasks.insert_alert(conn, org.id, None, name, "warn",
                               "audit_clear: 模型两侧均已消失, 本地 DB 行已清理(最后痕迹)")
            _commit(conn)
            stat["repo_delete"] += 0

    # ---- 4) 魔乐缺失(魔塔在管)→ 即时补齐(repo_exists 判定, 时效优先) ----
    from env_tools import transfer
    for name in sorted(set(db_scope) & scope_names):
        if name in gated_names:
            continue                    # gated 不补齐
        if name in modelers_names and name in db_modelers:
            continue                    # 魔乐 list 与 DB 都在
        try:
            m_exists = transfer.repo_exists(org, "modelers", name)
        except Exception:
            continue
        if not m_exists:
            if _enqueue(conn, org.id, tasks.KIND_MODEL_SYNC, name, "to_modelers"):
                stat["to_modelers"] += 1
    _commit(conn)

    # ---- 5) 采纳(双侧都有、DB 均无行)→ 建行不动作 ----
    for name in sorted(scope_names & modelers_names):
        if name not in db_scope and name not in db_modelers:
            stat["adopted"] += 1
            stat["adopted_names"].append(name)

    # ---- 6) 可见性对比(魔塔为主, 本轮实时值; 魔乐无修改 API → 告警一次) ----
    for name in sorted(scope_names & modelers_names):
        s, mo = scope_by_name[name], modelers_by_name[name]
        if s.get("gated"):
            continue                      # gated 完全不管(含魔乐侧现状)
        if bool(s.get("private")) != bool(mo.get("private")):
            stat["vis_mismatch"] += 1
            want = "private" if s["private"] else "public"
            _alert_once(conn, org.id, name, "vis_mismatch", "warn",
                        f"魔乐可见性与魔塔不一致(魔塔={want}); 魔乐 API 无法修改, 请到魔乐后台设置")
    _commit(conn)
    return stat


def _fetch_check(conn, org, platform: str, name: str):
    """拉 repo 文件集(标注 poison); 失败返回 None。"""
    try:
        files = fetch_remote_files(conn, org, platform, name)
        _mark_excluded(files)
        return files
    except Exception as e:
        print(f"[reconcile] 准入检查失败 {platform}/{name}: {type(e).__name__}: {e}")
        return None


def _db_had_weights(conn, org_id: str, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM files WHERE org=? AND repo_id LIKE ? AND path LIKE '%.safetensors' LIMIT 1",
        (org_id, f"%/{name}",)).fetchone()
    return row is not None


# ================================================================ 文件级(单向纠正器)
def _upsert_file_rows(conn, org_id: str, platform: str, repo_id: str,
                      files: list[dict], now: int, keep_fingerprints: set[str]) -> None:
    """写入/刷新本侧文件行。keep_fingerprints(已变化/待纠正路径): INSERT 写 NULL、UPDATE 保留旧值。
    (基线只在同步成功后由 worker 回写; last_synced_at 由 worker 更新, 本处不动)"""
    for f in files:
        keep = f["path"] in keep_fingerprints
        if keep:
            f = {**f, "sha256": None, "sha256_source": "none", "blob_id": None}
        conn.execute(
            """INSERT INTO files(org, platform, repo_id, path, size, sha256, sha256_source,
                 type, last_modified, is_lfs, blob_id, poison, last_seen_at)
               VALUES(?,?,?,?,?,?,?, 'blob',?,?,?,?,?)
               ON CONFLICT(org, platform, repo_id, path) DO UPDATE SET
                 size=excluded.size,
                 sha256=CASE WHEN ?=1 THEN files.sha256 ELSE excluded.sha256 END,
                 sha256_source=CASE WHEN ?=1 THEN files.sha256_source ELSE excluded.sha256_source END,
                 last_modified=excluded.last_modified, is_lfs=excluded.is_lfs,
                 blob_id=CASE WHEN ?=1 THEN files.blob_id ELSE excluded.blob_id END,
                 poison=excluded.poison,
                 missing_since=NULL, last_seen_at=excluded.last_seen_at""",
            (org_id, platform, repo_id, f["path"], f["size"], f["sha256"], f["sha256_source"],
             f["last_modified"], f["is_lfs"], f["blob_id"], f.get("poison"), now,
             1 if keep else 0, 1 if keep else 0, 1 if keep else 0),
        )


def compute_file_diff(conn, org, model: str, now: int | None = None) -> dict:
    """单模型文件级单向对比(对账与 worker 共用)。

    期望 = 魔塔当前文件集(active, 排除毒瘤/平台托管);
    输出:
      adopt     首见(双侧无文件行)→ 建基线不产生任务;
      abort     魔塔拉取失败/为空 → 本轮跳过(防误删风暴);
      to_modelers  需以魔塔当前版纠正魔乐(魔塔增改 / 魔乐缺 / 魔乐被改 /
                   跨端同名内容不一致, 2026-09)的 path;
      extra        魔乐独有(魔塔期望集无)path → 异常删除;
      missing_scope 魔塔消失 path(基线有、当前无)→ 同轮删除(2026-09 对齐 v1: 无宽限);
      readme      各侧 README 是否存在;
      readme_body_mismatch 两侧 README 剥离 front matter 后正文不一致(2026-09):
                   自动以魔塔版覆盖(加入 to_modelers) + 留痕告警(每轮检查, 仅当两侧
                   README 均存在且都非 init 时下载比对; README 已在 to_modelers 则跳过)。
    """
    now = now or _now()
    scope_repo = f"{org.scope.repo_name}/{model}"
    modelers_repo = f"{org.modelers.repo_name}/{model}"
    out = {"adopt": False, "abort": False, "to_modelers": set(), "extra": set(),
           "missing_scope": set(), "readme": {"scope": False, "modelers": False},
           "readme_body_mismatch": False}

    try:
        cur_scope_raw = {f["path"]: f for f in fetch_remote_files(conn, org, "scope", model)}
    except Exception as e:
        print(f"[reconcile] 魔塔文件集拉取失败 {model}, 本轮跳过: {type(e).__name__}: {e}")
        out["abort"] = True
        return out
    _mark_excluded(list(cur_scope_raw.values()))
    out["readme"]["scope"] = "README.md" in cur_scope_raw
    if not cur_scope_raw:
        out["abort"] = True          # 魔塔侧空/隐身 → 不做任何删除纠正
        return out

    cur_modelers_raw = {f["path"]: f for f in fetch_remote_files(conn, org, "modelers", model)}
    _mark_excluded(list(cur_modelers_raw.values()))
    out["readme"]["modelers"] = "README.md" in cur_modelers_raw

    def _active(d: dict) -> dict:
        from env_tools import poison as poison_mod
        return {p: f for p, f in d.items() if not poison_mod.is_excluded(f.get("poison"))}

    cur_scope = _active(cur_scope_raw)
    cur_modelers = _active(cur_modelers_raw)

    db_s = {r["path"]: r for r in conn.execute(
        "SELECT * FROM files WHERE org=? AND platform='scope' AND repo_id=?", (org.id, scope_repo))}
    db_m = {r["path"]: r for r in conn.execute(
        "SELECT * FROM files WHERE org=? AND platform='modelers' AND repo_id=?", (org.id, modelers_repo))}

    if not db_s and not db_m:
        out["adopt"] = True
        _upsert_file_rows(conn, org.id, "scope", scope_repo, list(cur_scope_raw.values()), now, set())
        _upsert_file_rows(conn, org.id, "modelers", modelers_repo,
                          list(cur_modelers_raw.values()), now, set())
        _commit(conn)
        return out

    keep_scope: set[str] = set()
    keep_modelers: set[str] = set()

    # ---- 期望侧(魔塔): 增/改 → to_modelers; 删 → missing_scope ----
    for path, row in db_s.items():
        # poison 以现场重分类为准(行内旧值可能来自旧规则/历史写入, 如 .gitkeep)
        if row["poison"] or _poison_excluded(path, row["size"] or 0):
            continue
        cur_f = cur_scope.get(path)
        if cur_f is None:
            if path == "README.md":
                # 2026-09 README 规则(用户定稿): 魔塔无 README.md → 视为初始/空白
                # README —— 不传播删除到魔乐(魔乐 README 保留, 无论平台初始还是
                # 用户内容)、不告警; 清掉魔塔侧基线行, 防止每轮 missing_scope 重现。
                conn.execute(
                    "DELETE FROM files WHERE org=? AND platform='scope' AND repo_id=? AND path='README.md'",
                    (org.id, scope_repo))
                continue
            # 2026-09 对齐 v1: 文件级删除无宽限 —— 魔塔已删即入 missing_scope,
            # 由 file_batch 同轮删除(不再维护 missing_since 计时, 列保留兼容旧库)
            out["missing_scope"].add(path)
            continue
        if (row["sha256"] or "") != (cur_f["sha256"] or ""):
            out["to_modelers"].add(path)
            keep_scope.add(path)
    for path in cur_scope:
        if path not in db_s:
            out["to_modelers"].add(path)          # 魔塔新增 → 同步魔乐
        elif path not in db_m and path not in cur_modelers:
            # 存量缺失补缺(2026-09 决策修订): 魔塔基线有行、魔乐基线无行且树上也
            # 没有 → 由文件级同步补齐(典型: 3-8 类整目录缺失 TP1/t2v/optional)。
            # 曾因 GLM-5.3-Flash 强制同步事故撤销自动补; 人工排查确认 3-8 属纯缺失
            # 后恢复 —— 有意差异场景由人工先处理(删仓重建/保留), 不在本规则范围。
            out["to_modelers"].add(path)

    # ---- 现状侧(魔乐): 相对期望偏离 ----
    for path in cur_modelers:
        if path not in cur_scope:
            out["extra"].add(path)                # 魔乐独有 → 异常删除
            continue
        row_m = db_m.get(path)
        if row_m is None:
            out["to_modelers"].add(path)          # 魔乐行缺失(从未同步/上次失败)→ 补
            keep_modelers.add(path)
            continue
        if row_m["poison"] or _poison_excluded(path, row_m["size"] or 0):
            continue
        cur_sha = cur_modelers[path]["sha256"]
        if cur_sha is not None:
            # 当前有内容 sha(LFS)→ 基线 sha 对比(行 sha 缺失时退回 blob_id)
            old_m = row_m["sha256"] or row_m["blob_id"]
            new_m = cur_sha
        else:
            # 当前无内容 sha(魔乐非 LFS)→ 只用 blob_id 判变; 忽略行内 local
            # sha256 基线(那是强哈希给跨端比对/复核用的, 与 blob_id 格式不可比,
            # 混比会每轮误判"被改"→ 假覆盖, 2026-09 修复)
            old_m = row_m["blob_id"]
            new_m = cur_modelers[path]["blob_id"]
        if old_m != new_m:
            out["to_modelers"].add(path)          # 魔乐被改 → 魔塔版覆盖
            keep_modelers.add(path)
    for path, row in db_m.items():
        # 同 db_s: 现场重分类, 防旧规则 poison=NULL 的隐藏行漏网
        if row["poison"] or _poison_excluded(path, row["size"] or 0):
            continue
        if path not in cur_modelers:
            if path in cur_scope:
                out["to_modelers"].add(path)      # 魔乐文件消失(魔塔有)→ 补回
                keep_modelers.add(path)
            else:
                out["extra"].add(path)            # 魔乐基线有、魔塔无 → 独有删除

    # ---- 跨端同名内容一致性(2026-09 用户定稿, 补 v1 语义)----
    # 同路径两侧都有"内容 sha256"且不一致 → 魔塔版覆盖魔乐(魔塔为准):
    #   魔塔侧: API sha256(全文件); 魔乐侧: LFS=lfs.sha256, 非 LFS 需 local 基线
    #   (sha256_source='local', 由强哈希分批建立——建立前无可比 sha, 跳过)。
    # README 除外: 魔乐版是管线变换产物(front matter license), 内容本就不同,
    # 走 README 自己的同步规则。告警 content_mismatch 按模型去重一次。
    mism = []
    for path in sorted(set(cur_scope) & set(cur_modelers)):
        if path == "README.md":
            continue
        row_m = db_m.get(path)
        if row_m is None:
            continue
        s_sha = cur_scope[path].get("sha256") or None
        m_sha = cur_modelers[path].get("sha256") or None
        if not m_sha and row_m["sha256_source"] == "local":
            m_sha = row_m["sha256"] or None       # 魔乐非 LFS: 用强哈希 local 基线
        if not s_sha or not m_sha:
            continue
        if s_sha != m_sha:
            mism.append(path)
            if path not in out["to_modelers"]:
                out["to_modelers"].add(path)
                keep_modelers.add(path)
    if mism:
        _alert_once(conn, org.id, model, "content_mismatch", "warn",
                    f"跨端同名文件内容与魔塔不一致 {len(mism)} 个, 将按魔塔版覆盖: "
                    + ", ".join(sorted(mism)[:5]) + ("..." if len(mism) > 5 else ""))

    # README 特判(魔塔为主, 2026-09 规则修订):
    #  a) 魔乐 README 永不因"魔乐独有"进 extra —— 魔塔无 README 时视为魔塔侧
    #     初始/空白 README(平台可能抽风删除, 如 DeepSeek-V4-Pro-w4a8-mtp 等),
    #     魔乐 README(平台初始或用户内容)保留, 不删除、不告警;
    #  b) 魔塔有 README 而魔乐行 is_init=1(从未同步魔塔版)→ 强制纠正覆盖。
    out["extra"].discard("README.md")
    if "README.md" in cur_scope and out["readme"]["modelers"]:
        row_m = db_m.get("README.md")
        if row_m is not None and row_m["is_init"]:
            # 魔乐行 is_init(从未同步魔塔版)→ 强制纠正覆盖; 但魔塔 README 自身也是
            # init(空/平台模板)时不同步(2026-09 空转修复: 否则每轮拉入 to_modelers
            # → worker 下载后判定 init 跳过 → 空转循环)。
            row_s = db_s.get("README.md")
            if row_s is None or not row_s["is_init"]:
                out["to_modelers"].add("README.md")

    # ---- README 正文一致性(2026-09): 剥掉 front matter 比正文, 不一致 → 以魔塔版覆盖 ----
    # 盲区来源: 首次采纳时两侧 README 就已不一致 → per-side 基线各自为政, 永不触发覆盖;
    # 裸文件哈希又因 front matter/license 归一化被排除(见 2026-09 强哈希修复)。
    # 比较口径(2026-09-14 用户定稿): front matter 块前后空行容忍(split_front_matter),
    # 正文取"第一行非空行 ~ 最后一行非空行"(normalize_body) —— 即只容忍首尾空行/空白;
    # 正文内部空行与排版差异一律视为不一致(不逐行 rstrip, 不折叠内部空行)。
    # 代价: 每模型 2 次小文件下载(仅两侧皆存在且都非 init); 已在 to_modelers 则跳过。
    if ("README.md" in cur_scope and "README.md" in cur_modelers
            and "README.md" not in out["to_modelers"]):
        row_s, row_m = db_s.get("README.md"), db_m.get("README.md")
        s_is_init = bool(row_s["is_init"]) if row_s is not None else False
        m_is_init = bool(row_m["is_init"]) if row_m is not None else False
        if not s_is_init and not m_is_init:
            from env_tools import pipeline as _pl
            s_text = _download_readme_text(conn, org, "scope", model)
            m_text = _download_readme_text(conn, org, "modelers", model)
            if s_text is not None and m_text is not None:
                s_body = _pl.normalize_body(_pl.split_front_matter(s_text)[1])
                m_body = _pl.normalize_body(_pl.split_front_matter(m_text)[1])
                if hashlib.sha256(s_body.encode("utf-8")).hexdigest() != \
                        hashlib.sha256(m_body.encode("utf-8")).hexdigest():
                    out["readme_body_mismatch"] = True
                    out["to_modelers"].add("README.md")
                    keep_modelers.add("README.md")
                    _alert_once(conn, org.id, model, "readme_body_mismatch", "warn",
                                "两侧 README 正文不一致(front matter 之外), 已自动以魔塔版覆盖魔乐; "
                                "如为有意差异请在魔塔侧修改")

    # ---- 基线刷新(变化/纠正路径保留旧指纹, 待 worker 成功回写) ----
    _upsert_file_rows(conn, org.id, "scope", scope_repo, list(cur_scope_raw.values()), now, keep_scope)
    _upsert_file_rows(conn, org.id, "modelers", modelers_repo,
                      list(cur_modelers_raw.values()), now, keep_modelers)
    _commit(conn)
    return out


def _poison_excluded(path: str, size: int = 0) -> bool:
    """隐藏/毒瘤判定(与 transfer.sync_files 删除守卫同规则, 2026-09 全隐藏过滤)。"""
    from env_tools import poison as _p
    return _p.is_excluded(_p.classify(path, size))


def _actionable_split(conn, org, model: str, d: dict, now: int) -> dict:
    """按 worker(transfer.sync_files)的删除保护规则, 拆出"执行时真有动作"的部分。

    返回 {"run": set(worker 本轮会真正执行的 path), "manual": set(仅人工可处理的 extra)}。
    2026-09 空转修复: file_level 曾把 extra 全量计入入队条件, 而 auto_delete_extra=false
    时 worker 对 extra(尤其 .safetensors / >50MB 永远人工)只告警不动手 → 每轮生成
    执行即空转的 file_batch 任务(魔乐侧无任何新增/commit, 任务却一直重建)。
    2026-09 对齐 v1: missing_scope(魔塔已删)无宽限, 直接可执行(与 worker 同规则)。
    """
    auto_extra = bool(db.get_app_config(conn, org.id, "sync.auto_delete_extra", False))
    scope_repo = f"{org.scope.repo_name}/{model}"
    modelers_repo = f"{org.modelers.repo_name}/{model}"
    run = set(d["to_modelers"])
    manual: set[str] = set()
    for pth in d["missing_scope"]:                     # 魔塔已删 → 同轮删(无宽限)
        row = conn.execute(
            "SELECT size FROM files WHERE org=? AND platform='scope' AND repo_id=? AND path=?",
            (org.id, scope_repo, pth)).fetchone()
        if row and _poison_excluded(pth, row["size"] or 0):
            continue                                   # 隐藏/毒瘤: 永不删(与 worker 同规则)
        run.add(pth)
    for pth in d["extra"]:
        row = conn.execute(
            "SELECT size FROM files WHERE org=? AND platform='modelers' AND repo_id=? AND path=?",
            (org.id, modelers_repo, pth)).fetchone()
        if row and _poison_excluded(pth, row["size"] or 0):
            continue                                   # 隐藏/毒瘤: 不删除(与 worker 同规则)
        if auto_extra:
            run.add(pth)                               # 严格镜像: 独有文件一律删(含权重/大文件)
        else:
            manual.add(pth)                            # 关闭严格镜像 → 人工确认
    return {"run": run, "manual": manual}


def file_level(conn, org, now: int | None = None, seen: dict | None = None) -> dict:
    """文件级单向对账: 处理对象 = 魔塔在管 repo(DB 魔塔行 + 魔塔当前 list 在)。

    入队只发生在 worker 真正有动作时(_actionable_split, 2026-09 空转修复):
    受删除保护的 extra 不驱动入队(worker 执行也是空转), 改为对账侧一次性告警
    (delete_manual 前缀, 按模型去重); missing_scope(魔塔已删)无宽限, 直接可执行。
    """
    now = now or _now()
    stat = {"checked": 0, "adopted": 0, "aborted": 0, "to_correct": 0, "extra": 0,
            "file_batch_enq": 0, "to_delete": 0, "manual_extra": 0,
            "readme_body_mismatch": 0}
    if seen is None:
        s = {m["name"] for m in fetch_remote_models(conn, org, "scope")} if org.scope.configured else set()
    else:
        s = seen.get("scope", set())
    db_names = {r["name"] for r in conn.execute(
        "SELECT name FROM models WHERE org=? AND platform='scope' AND gated=0", (org.id,))}
    managed = sorted(s & db_names)

    # 进度可见性(2026-09): 一轮可能几十分钟(逐模型拉双侧文件树) —— 每 20 个模型
    # 打一条进度并刷新心跳(只刷时间不覆盖 pid), 避免 journal 静默 + 心跳停滞误判。
    total = len(managed)
    t0 = _now()
    print(f"[reconcile] {org.id} 文件级开始: 在管模型 {total} 个", flush=True)
    db.set_runtime_state(conn, org.id, "sync.reconcile_progress", f"文件级 0/{total} 开始")
    for i, model in enumerate(managed, 1):
        if i % 20 == 0:
            msg = (f"文件级 {i}/{total} (检查={stat['checked']} 上传={stat['to_correct']} "
                   f"待删={stat['to_delete']} 独有={stat['extra']} 入队={stat['file_batch_enq']}) "
                   f"耗时 {_now() - t0}s")
            print(f"[reconcile] {org.id} {msg}", flush=True)
            db.set_runtime_state(conn, org.id, "sync.reconcile_progress", msg)
            db.touch_heartbeat(conn, None)
        try:
            d = compute_file_diff(conn, org, model, now)
        except Exception as e:
            print(f"[reconcile] {org.id}/{model} 文件对账异常: {type(e).__name__}: {e}")
            continue
        if d["abort"]:
            stat["aborted"] += 1
            continue
        stat["checked"] += 1
        if d["adopt"]:
            stat["adopted"] += 1
            _adopt_readme_check(conn, org, model, d, stat)
            continue
        _readme_isinit_confirm(conn, org, model, d, stat, now)
        act = _actionable_split(conn, org, model, d, now)
        stat["to_correct"] += len(d["to_modelers"])
        stat["extra"] += len(d["extra"])
        if d.get("readme_body_mismatch"):
            stat["readme_body_mismatch"] += 1
        if d["missing_scope"]:
            stat["to_delete"] += len(d["missing_scope"])
        if act["manual"]:
            stat["manual_extra"] += len(act["manual"])
            _alert_once(conn, org.id, model, "delete_manual", "warn",
                        "魔乐独有文件含删除保护(权重/超大/未开 auto_delete_extra), "
                        "同步器不自动删除, 请人工确认后手动处理: "
                        + ", ".join(sorted(act["manual"])[:10])
                        + ("..." if len(act["manual"]) > 10 else "")
                        + f" | 确认后执行: python server-work.py clean --org {org.id} "
                          f"--model {model} --yes")
        if act["run"] and _enqueue(conn, org.id, tasks.KIND_FILE_BATCH, model, "to_modelers"):
            stat["file_batch_enq"] += 1
    _commit(conn)
    done_msg = (f"文件级完成 {stat['checked']}/{total} 耗时 {_now() - t0}s | "
                f"采纳={stat['adopted']} 跳过(拉取失败)={stat['aborted']} 上传={stat['to_correct']} "
                f"待删={stat['to_delete']} 独有={stat['extra']} 人工={stat['manual_extra']} "
                f"README正文不一致={stat['readme_body_mismatch']} 入队={stat['file_batch_enq']}")
    print(f"[reconcile] {org.id} {done_msg}", flush=True)
    db.set_runtime_state(conn, org.id, "sync.reconcile_progress", done_msg)
    return stat


# ================================================================ README is_init(采纳/确认)
def _download_readme_text(conn, org, platform: str, model: str) -> str | None:
    import os
    import shutil
    import tempfile
    from env_tools import transfer
    tmp = tempfile.mkdtemp(prefix="rmchk_")
    try:
        transfer._download_one(
            org, platform,
            f"{org.scope.repo_name if platform == 'scope' else org.modelers.repo_name}/{model}",
            "README.md", tmp)
        return open(os.path.join(tmp, "README.md"), encoding="utf-8").read()
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _set_readme_init(conn, org_id: str, platform: str, repo_id: str, value: int) -> None:
    conn.execute(
        "UPDATE files SET is_init=? WHERE org=? AND platform=? AND repo_id=? AND path='README.md'",
        (value, org_id, platform, repo_id))


def _adopt_readme_check(conn, org, model: str, d: dict, stat: dict) -> None:
    """采纳 README 四分支(魔塔为主语义下: 单侧有效→以魔塔版补魔乐; 魔塔 init 不同步)。"""
    from env_tools import pipeline
    repo_ids = {"scope": f"{org.scope.repo_name}/{model}",
                "modelers": f"{org.modelers.repo_name}/{model}"}
    texts = {}
    for plat in ("scope", "modelers"):
        if d["readme"].get(plat):
            texts[plat] = _download_readme_text(conn, org, plat, model) or ""
    if not texts:
        return
    init = {plat: pipeline.detect_init_content(t) for plat, t in texts.items()}
    if "scope" in texts and not init["scope"]:
        # 魔塔 README 有效 → 魔乐应同步魔塔版(魔乐 init 或 real 都会被覆盖)
        if "modelers" in texts:
            body_s = pipeline.normalize_body(pipeline.split_front_matter(texts["scope"])[1])
            body_m = pipeline.normalize_body(pipeline.split_front_matter(texts["modelers"])[1])
            if body_s == body_m and not init["modelers"]:
                for plat in texts:
                    _set_readme_init(conn, org.id, plat, repo_ids[plat], 0)
                _commit(conn)
                return
        # 需要同步/覆盖: 入队 file_batch(魔塔版 → 魔乐)
        for plat in texts:
            _set_readme_init(conn, org.id, plat, repo_ids[plat],
                             1 if plat == "modelers" and init["modelers"] else 0)
        if _enqueue(conn, org.id, tasks.KIND_FILE_BATCH, model, "to_modelers"):
            stat["file_batch_enq"] += 1
        _commit(conn)
        return
    # 魔塔无 README 或魔塔 init(2026-09 规则): 魔塔无 README 视为初始/空白 ——
    # 魔乐 README(平台初始或用户内容)一律保留: 仅标记 is_init, 不删除、不告警。
    for plat in texts:
        _set_readme_init(conn, org.id, plat, repo_ids[plat], 1 if init[plat] else 0)
    _commit(conn)


def _readme_isinit_confirm(conn, org, model: str, d: dict, stat: dict, now: int) -> None:
    """常规轮: README 在待纠正集时, 以魔塔 README 实际内容做 init 判定:
      - init(空/平台模板)→ 移出 to_modelers(不同步, 防循环), 标 scope is_init=1,
        并把本次 API 指纹刷入基线(平台重写 init 只刷基线不触发任务);
      - 有效正文 → 标 scope is_init=0, 留在 to_modelers(worker 覆盖魔乐)。
    2026-09 空转修复: 原实现仅在 scope 行 is_init=1 时才下载判定; 而 compute_file_diff
    的"魔乐 is_init=1 强制纠正"分支会把魔塔 init README 每轮拉入 to_modelers
    → worker 每轮下载后判定 init 跳过 → 空转。现改为内容为准、每轮判定。
    """
    from env_tools import pipeline
    from env_tools import transfer
    if "README.md" not in d["to_modelers"]:
        return
    repo_id = f"{org.scope.repo_name}/{model}"
    text = _download_readme_text(conn, org, "scope", model)
    if text is None:
        return
    if pipeline.detect_init_content(text):
        d["to_modelers"].discard("README.md")      # init → 不同步
        _set_readme_init(conn, org.id, "scope", repo_id, 1)
        try:
            cur = transfer.fetch_remote_files(conn, org, "scope", model)
            for f in cur:
                if f["path"] == "README.md":
                    from env_tools import poison as _p
                    f["poison"] = _p.classify(f["path"], f["size"])
                    _upsert_file_rows(conn, org.id, "scope", repo_id, [f], now, set())
        except Exception as e:
            print(f"[reconcile] README 基线刷新失败 scope/{model}: {e}")
        _commit(conn)
    else:
        _set_readme_init(conn, org.id, "scope", repo_id, 0)
        _commit(conn)


# ================================================================ 强哈希(首次全量核验 + 30d 复核)
def forced_rehash(conn, org, now: int | None = None) -> dict:
    """强哈希: 首次部署全量核验, 之后每 forced_rehash_interval_d(默认 30d)复核。

    目的(用户定稿 2026-09): 首次部署/首次 audit 时必须建立"两侧全文件(含魔乐非
    LFS)内容 sha256 一致"的基线 —— 之后检测到魔塔文件更新, 即可信任基线与魔乐
    现状一致, 在魔乐端第一时间做增删改。魔乐非 LFS 的 blob_id 继续保留在库上
    (魔乐端快速与库比对用), sha256 核验只做补充/交叉验证。

    ⚠ README.md 不参与强哈希核对(2026-09 修复): 魔乐侧 README 是 README 管线的
    变换产物(front matter/license 归一化), 与魔塔侧原始 README 的**裸文件 sha256
    必然不同** —— 纳入会让每次全量核验刷出几十条 rehash_mismatch 误报。
    (README 的正文一致性属独立话题; 跨端同名规则同样排除 README, 见 compute_file_diff。)

    处理对象(全部仅魔乐侧行; 魔塔侧 API 直接给内容 sha256, 不做下载):
      A. LFS(is_lfs=1 且 sha256 已有): 与魔塔同路径行 sha256 交叉比对 —— 零下载,
         核验魔塔 API 可信度 + 镜像一致性(魔塔/魔乐同内容 → sha256 必须相等);
      B. 非 LFS 且无本地基线(sha256_source='none')且 ≤50MB: 下载重算 → 写入
         sha256/sha256_source='local' 建立基线 → 与魔塔同路径 sha256 比对;
         >50MB 不下载(大权重不下载原则), 继续以 blob_id 作变更指纹;
      C. 非 LFS 且已有 'local' 基线(此前建立): 重下比对, 核验魔乐文件未被外部改动。

    分批(forced_rehash_batch, 默认 800/轮): 首次全量(数万小文件)分多轮跑完,
    未完成不写 last_forced_rehash(下轮继续); 全部完成后写 last_forced_rehash,
    之后按 interval_d 周期复核。

    2026-09-14 修复(三处):
      ① 时间戳改用 db.set_app_config(upsert): 旧实现用 seed_app_config(INSERT OR IGNORE),
         只有首次写入生效 → 30d 周期无法重置(每轮都进本函数)、15min 对账节流同样失效;
      ② 周期边界用 `rehash_checked_at <= cycle_start`: 旧实现 `< cycle_start` 时,
         首次全量若分多轮完成, 完成后下一轮会把前 n-1 轮已核验的行**全部重下一遍**
         (它们的时间戳 < 完成时刻), 而最后一轮那批(== cycle_start)今后永不复核;
      ③ 失败退避: 下载/核对异常写 rehash_fail_count + rehash_next_try_at(指数退避,
         6h 起、上限 7d), 失败行在退避期内不再被选中(旧实现每轮重选同一批, 最多 800/轮
         长期空转); 连续失败 3 次起每行告警一次, 成功后计数清零。

    ⚠ 修正历史: 旧逻辑按 `platform 任意 AND is_lfs=0 AND sha256 IS NOT NULL`
    筛选, 但魔塔 FileInfo.is_lfs 不可靠(实测对 safetensors 亦返回 False) →
    曾把魔塔大权重当"非 LFS 小文件"全量下载(1.8GB 事故)→ 本函数只碰魔乐侧行。
    """
    now = now or _now()
    stat = {"checked": 0, "mismatch": 0, "ok": 0, "skipped_big": 0, "done": False,
            "failed": 0, "retry_only": False}
    interval_d = int(db.get_app_config(conn, org.id, "sync.forced_rehash_interval_d", 30))
    last = db.get_app_config(conn, org.id, "sync.last_forced_rehash", 0) or 0
    batch = int(db.get_app_config(conn, org.id, "sync.forced_rehash_batch", 800))
    big_limit = 50 * 1024 * 1024           # >50MB 永不下载(大权重不下载原则)
    # 本周期起点: 行 rehash_checked_at <= 它 = 上周期(或首次)已核验 → 本周期需复核。
    # 用 <= 而非 <: 完成时刻写入的标记会与"最后一轮核验的行"时间戳相同, 若用 <
    # 那批行今后永不复核(2026-09-14 修复 ②)。
    cycle_start = int(last) or 0
    fail_base_s = 6 * 3600                 # 失败退避基数 6h, 倍增, 上限 7d
    fail_cap_s = 7 * 86400
    import os
    from utils.ratelimit import global_limiter
    from env_tools import pipeline

    # 两类轮次(2026-09-14 修复 ③ 的必要拆分):
    #   cycle_due=True  → 周期轮: 复核上周期已核验行 + 建 sha256_source='none' 的新基线;
    #   cycle_due=False → 纯重试轮: 30d 闸门内只处理"退避到期"的失败行, 不碰周期标记。
    # 若不拆: 失败行退了 6h 后, `now - last < 30d` 会把整个函数挡在门外 → 退避形同虚设。
    cycle_due = (not last) or (now - int(last) >= interval_d * 86400)

    def _retry_waiting() -> int:
        return conn.execute(
            "SELECT COUNT(*) n FROM files WHERE org=? AND platform='modelers' AND is_lfs=0 "
            "AND size<=? AND path != 'README.md' AND sha256_source='none' "
            "AND rehash_next_try_at IS NOT NULL AND rehash_next_try_at <= ?",
            (org.id, big_limit, now)).fetchone()["n"] or 0

    if not cycle_due and _retry_waiting() == 0:
        return stat
    if cycle_due:
        where_sql = ("(sha256_source='none' OR rehash_checked_at IS NULL "
                     "OR rehash_checked_at <= ?) AND "
                     "(rehash_next_try_at IS NULL OR rehash_next_try_at <= ?)")
        where_params = (cycle_start, now)
    else:
        where_sql = "sha256_source='none' AND rehash_next_try_at <= ?"
        where_params = (now,)

    def _scope_sha(r) -> str | None:
        model = r["repo_id"].split("/")[-1]
        row = conn.execute(
            "SELECT sha256 FROM files WHERE org=? AND platform='scope' AND repo_id=? AND path=?",
            (org.id, f"{org.scope.repo_name}/{model}", r["path"])).fetchone()
        return (row["sha256"] or None) if row else None

    def _alert_mismatch(r, side_detail: str) -> None:
        stat["mismatch"] += 1
        tasks.insert_alert(conn, org.id, None, r["repo_id"].split("/")[-1], "warn",
                           f"rehash_mismatch: 强哈希不一致(modelers/{r['path']}): {side_detail}")

    # B/C) 非 LFS ≤50MB 下载核验: 无基线(none)→ 建 'local' 基线并与魔塔比对;
    #      已有 'local' 但本周期未核验(rehash_checked_at <= cycle_start)→ 重下复核。
    #      每轮最多 batch 个下载; 已核验行写 rehash_checked_at=now, 本轮内不重选;
    #      失败行按 rehash_next_try_at 退避(退避期内不选), 避免每轮重试同一批。
    # 收尾判据用"剩余候选数 == 0"(精确 COUNT, 复用 where_sql): 旧代码用 budget>0
    # (等价于"本轮选中数 < 上限")判断, 候选数恰为 batch 整数倍时会白多跑一轮才写
    # 周期标记(实测 2 行/batch=1 要 3 轮); COUNT 判据一轮即可收尾。
    def _remaining() -> int:
        return conn.execute(
            "SELECT COUNT(*) n FROM files WHERE org=? AND platform='modelers' AND is_lfs=0 "
            "AND size<=? AND path != 'README.md' AND " + where_sql,
            (org.id, big_limit) + where_params).fetchone()["n"] or 0

    rows = conn.execute(
        "SELECT * FROM files WHERE org=? AND platform='modelers' AND is_lfs=0 AND size<=? "
        "AND path != 'README.md' AND " + where_sql + " "
        "ORDER BY COALESCE(rehash_fail_count, 0) ASC, path ASC "
        "LIMIT ?",
        (org.id, big_limit) + where_params + (batch,)).fetchall()
    for r in rows:
        try:
            from openmind_hub import om_hub_download
            global_limiter.wait()
            p = om_hub_download(r["repo_id"], r["path"], revision="main",
                                token=org.modelers.token,
                                local_dir=org.compare_dir(r["repo_id"].split("/")[-1]),
                                force_download=True)
            h = pipeline.hash_file(p)
            os.remove(p)
            stat["checked"] += 1
            had_sha = r["sha256"] is not None
            conn.execute(
                "UPDATE files SET sha256=?, sha256_source='local', rehash_checked_at=?, "
                "rehash_fail_count=0, rehash_next_try_at=NULL "
                "WHERE org=? AND platform='modelers' AND repo_id=? AND path=?",
                (h, now, org.id, r["repo_id"], r["path"]))
            if not had_sha:
                # B) 首次建立本地基线, 并交叉比对魔塔
                s_sha = _scope_sha(r)
                if s_sha and s_sha != h:
                    _alert_mismatch(r, f"魔塔 {s_sha[:16]}… vs 实际 {h[:16]}…")
                else:
                    stat["ok"] += 1
            else:
                # C) 复核已有 local 基线(周期内首次)
                if h != r["sha256"]:
                    _alert_mismatch(r, f"基线 {r['sha256'][:16]}… vs 实际 {h[:16]}…")
                else:
                    stat["ok"] += 1
            if stat["checked"] % 50 == 0:
                pmsg = (f"强哈希分批: 本轮已核对 {stat['checked']} (轮内上限 {batch}), "
                        f"一致={stat['ok']} 不一致={stat['mismatch']} 失败={stat['failed']}")
                print(f"[reconcile] {org.id} {pmsg}", flush=True)
                db.set_runtime_state(conn, org.id, "sync.rehash_progress", pmsg)
                db.touch_heartbeat(conn, None)
        except Exception as e:
            # 失败退避(2026-09-14 修复 ③): 计数 + 指数退避, 退避期内不再被选中
            stat["failed"] += 1
            n_fail = int(r["rehash_fail_count"] or 0) + 1
            backoff = min(fail_base_s * (2 ** (n_fail - 1)), fail_cap_s)
            nxt = now + backoff
            conn.execute(
                "UPDATE files SET rehash_fail_count=?, rehash_next_try_at=? "
                "WHERE org=? AND platform='modelers' AND repo_id=? AND path=?",
                (n_fail, nxt, org.id, r["repo_id"], r["path"]))
            print(f"[reconcile] 强哈希 {r['repo_id']}/{r['path']} 异常"
                  f"(第 {n_fail} 次, 退避 {backoff // 3600}h, 下次 {nxt}): "
                  f"{type(e).__name__}: {e}", flush=True)
            if n_fail == 3:
                tasks.insert_alert(
                    conn, org.id, None, r["repo_id"].split("/")[-1], "warn",
                    f"rehash_download_failed: 强哈希连续 3 次失败(modelers/{r['path']}): "
                    f"{type(e).__name__}: {e}; 已按指数退避继续重试(上限 7d)")
            db.set_runtime_state(
                conn, org.id, "sync.rehash_progress",
                f"强哈希失败退避: {r['path']} 第 {n_fail} 次, 下次重试 +{backoff // 3600}h")
    remaining = _remaining()          # 只算一次(下面两个分支共用)
    if remaining == 0 and cycle_due:
        # 本轮没有更多下载任务(预算未触顶)→ 本周期收尾:
        # A) 全量 LFS 交叉比对(零下载, 每周期一次); 记录 >50MB 非 LFS 计数; 完成。
        for r in conn.execute(
                "SELECT * FROM files WHERE org=? AND platform='modelers' "
                "AND is_lfs=1 AND sha256 IS NOT NULL AND path != 'README.md'",
                (org.id,)).fetchall():
            stat["checked"] += 1
            s_sha = _scope_sha(r)
            if s_sha and s_sha != r["sha256"]:
                _alert_mismatch(r, f"魔塔 {s_sha[:16]}… vs 魔乐 {r['sha256'][:16]}…")
            else:
                stat["ok"] += 1
        big = conn.execute(
            "SELECT COUNT(*) n FROM files WHERE org=? AND platform='modelers' AND is_lfs=0 "
            "AND sha256_source='none' AND size>?", (org.id, big_limit)).fetchone()["n"]
        stat["skipped_big"] = big or 0
        # ① 时间戳 upsert: 周期完成后才能被下一周期正确覆盖(旧实现只写一次, 永不重置)。
        # 失败行即使仍在退避也照常收尾 —— 它们由"纯重试轮"继续跟进(见 cycle_due 注释)。
        db.set_app_config(conn, org.id, "sync.last_forced_rehash", now)
        stat["done"] = True
    elif remaining == 0:
        # 纯重试轮收尾: 不改周期标记、不做 LFS 交叉比对(那是周期轮的事)
        stat["retry_only"] = True
    _commit(conn)
    rh_msg = (f"强哈希{'完成' if stat['done'] else ('重试轮' if stat.get('retry_only') else '分批进行中')}: "
              f"本轮核对={stat['checked']} 一致={stat['ok']} 不一致={stat['mismatch']} "
              f"失败={stat['failed']} 跳过(>50MB)={stat['skipped_big']} 轮内上限={batch}")
    print(f"[reconcile] {org.id} {rh_msg}", flush=True)
    db.set_runtime_state(conn, org.id, "sync.rehash_progress", rh_msg)
    return stat


# ================================================================ 调度入口
def scheduler_tick(conn, orgs, now: int | None = None, force: bool = False) -> list[dict]:
    """daemon 每轮调用(节流 sync.model_interval_min; 每 org 独立容错)。

    force=True(仅手动 `audit --force` 使用)忽略 15min 节流强制执行; daemon 恒用默认 False。
    """
    now = now or _now()
    results = []
    for org in orgs:
        interval_min = db.get_app_config(conn, org.id, "sync.model_interval_min", 15)
        interval_s = int(interval_min) * 60
        last = db.get_app_config(conn, org.id, "sync.last_reconcile", 0) or 0
        throttled = bool(last) and now - int(last) < interval_s
        if throttled and not force:
            results.append({"org": org.id, "skipped": True})
            continue
        if throttled and force:
            print(f"[reconcile] {org.id} 强制对账(--force): 忽略 {interval_min}min 节流"
                  f"(距上次 {now - int(last)}s)", flush=True)
        try:
            t_tick = _now()
            db.set_runtime_state(conn, org.id, "sync.reconcile_progress", "模型级对账中 ...")
            seen = {}
            r_model = model_level(conn, org, now)
            seen.update(r_model.get("seen", {}))
            r_file = file_level(conn, org, now, seen=seen)
            r_rehash = forced_rehash(conn, org, now)
            # 时间戳 upsert(2026-09-14 修复 ①): 旧实现用 seed_app_config(INSERT OR IGNORE)
            # 只有首次写入生效 → 15min 节流在首次对账后永久失效(每个空转轮都跑整轮对账)
            db.set_app_config(conn, org.id, "sync.last_reconcile", now)
            idle_msg = f"空闲(上轮对账完成, 耗时 {_now() - t_tick}s)"
            db.set_runtime_state(conn, org.id, "sync.reconcile_progress", idle_msg)
            results.append({"org": org.id, "model": r_model, "file": r_file, "rehash": r_rehash})
        except Exception as e:
            db.set_runtime_state(conn, org.id, "sync.reconcile_progress",
                                 f"对账异常: {type(e).__name__}: {e}")
            results.append({"org": org.id, "error": f"{type(e).__name__}: {e}"})
    return results


def full_audit(conn, org, now: int | None = None, force: bool = False) -> dict:
    """手动 audit 全流程(model_level + file_level + forced_rehash [+ GitCode 补齐])。

    force=True → 忽略 15min 对账节流(对应 CLI `audit --force`); 默认 False 与 daemon 同规则。
    """
    now = now or _now()
    r = scheduler_tick(conn, [org], now, force=force)[0]
    if r.get("skipped"):
        return r                      # 节流跳过时连 GitCode 补齐一起跳过(否则"跳过"只跳一半)
    if org.has_gitcode:
        from env_tools import gitcode
        try:
            r["gitcode_fill"] = gitcode.scan_and_fill(conn, org)
        except Exception as e:
            r["gitcode_fill"] = {"error": f"{type(e).__name__}: {e}"}
    return r
