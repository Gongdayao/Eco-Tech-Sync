#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""worker 子进程: python -m env_tools.task_runner <task_id>

职责:
1. 读任务 → 校验存在;
2. 按任务.org 构建 OrgContext;
3. 状态 pending → running;
4. 源存在性预检(执行前重验证, 源 repo/文件已消失 → obsolete 作废);
5. 按 kind 分发执行(model_sync / file_batch / repo_delete / gitcode_import);
6. 结果回写 succeeded / failed(三档重启)/ obsolete。

退出码: 0=成功或作废(状态已回写), 1=执行失败(状态已回写),
2=任务不存在/参数错误, 3=配置错误。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠ 环境引导必须先于一切可能 import SDK 的模块:
# daemon 派生子进程时环境已继承, 这里再调一次是幂等的保险(单独运行也自足)。
from env_tools.env_bootstrap import bootstrap_env  # noqa: E402
bootstrap_env()

from env_tools import db, tasks  # noqa: E402
from env_tools.config import load_config, get_org  # noqa: E402


def _precheck_source(conn, org, task) -> tuple[bool, str | None]:
    """执行前预检(强制第一步, 见 V2-DESIGN.md §4 规则 / 指南 §8.4):
    源 repo 存在性 check; repo_delete/gitcode_import 无需源检查。

    返回 (ok, obsolete_reason): ok=False 且 reason 非空 = 源已消失 → 任务作废;
    网络/API 异常向上抛 → 调用方按可重试失败处理。
    """
    if task["kind"] in (tasks.KIND_REPO_DELETE, tasks.KIND_GITCODE_IMPORT):
        return True, None
    direction = task["direction"] or "to_modelers"
    src = "scope" if direction == "to_modelers" else "modelers"
    from env_tools import transfer
    if not transfer.repo_exists(org, src, task["model"]):
        return False, f"源 repo 不存在({src}): {org.scope.repo_name if src=='scope' else org.modelers.repo_name}/{task['model']}"
    return True, None


def _execute(conn, org, task, stub_fail: bool) -> None:
    """按 kind 分发执行(状态回写由调用方 run() 统一处理)。"""
    if not org.scope.configured or not org.modelers.configured:
        raise RuntimeError(f"org '{org.id}' 缺少 scope/modelers 平台配置")
    from env_tools import transfer
    if task["kind"] == tasks.KIND_MODEL_SYNC:
        result = transfer.sync_model(conn, org, task)
    elif task["kind"] == tasks.KIND_FILE_BATCH:
        result = transfer.sync_files(conn, org, task)
    elif task["kind"] == tasks.KIND_REPO_DELETE:
        result = transfer.delete_repo_task(conn, org, task)
    elif task["kind"] == tasks.KIND_GITCODE_IMPORT:
        result = transfer.gitcode_import_task(conn, org, task)
    else:
        raise RuntimeError(f"未知 kind: {task['kind']}")
    if not result.get("ok", False):
        raise RuntimeError(f"{task['kind']} 返回失败: {result}")
    time.sleep(0.1)


def run(task_id: int, stub_fail: bool = False) -> int:
    try:
        raw_cfg, orgs = load_config()
    except Exception as e:
        print(f"[task_runner] 配置加载失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 3

    db.migrate()
    conn = db.get_conn()
    task = tasks.get_task(conn, task_id)
    if task is None:
        print(f"[task_runner] 任务 {task_id} 不存在", file=sys.stderr)
        return 2

    org = get_org(orgs, task["org"])
    if org is None:
        err = f"任务所属组织 '{task['org']}' 不在配置中"
        tasks.fail_task(conn, task_id, err)
        print(f"[task_runner] {err}", file=sys.stderr)
        return 1

    tasks.mark_running(conn, task_id)
    t0 = time.time()
    print(f"[task_runner] 开始: task={task_id} org={org.id} kind={task['kind']} "
          f"model={task['model']} direction={task['direction']}")

    # 测试钩子(仅显式模型名触发, 必须先于预检):
    #   __fail__  → 模拟失败; __sleep__N → 模拟长任务(抢占测试用)
    if stub_fail or task["model"].endswith("__fail__"):
        err = "STUB 模拟失败(测试用)"
        tasks.fail_task(conn, task_id, err)
        print(f"[task_runner] 失败: {err}", file=sys.stderr)
        return 1
    import re as _re
    _m = _re.search(r"__sleep__(\d+)", task["model"])
    if _m:
        secs = int(_m.group(1))
        print(f"[task_runner] 测试钩子: 模拟长任务 {secs}s ...")
        time.sleep(secs)
        tasks.complete_task(conn, task_id)
        return 0

    # 1) 源存在性预检 → 源消失则作废(不占失败次数, 不告警)
    try:
        ok, obsolete_reason = _precheck_source(conn, org, task)
        if not ok:
            tasks.obsolete_task(conn, task_id, obsolete_reason or "源不存在")
            print(f"[task_runner] 作废: task={task_id} {obsolete_reason}")
            return 0
    except Exception as e:
        # 预检本身的异常(网络等)按可重试失败处理
        err = f"预检异常: {type(e).__name__}: {e}"
        tasks.fail_task(conn, task_id, err)
        print(f"[task_runner] 失败: {err}", file=sys.stderr)
        return 1

    # 2) 执行
    try:
        _execute(conn, org, task, stub_fail)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        tasks.fail_task(conn, task_id, err)
        print(f"[task_runner] 失败: {err}", file=sys.stderr)
        return 1

    tasks.complete_task(conn, task_id)
    print(f"[task_runner] 成功: task={task_id} 耗时 {time.time()-t0:.2f}s")
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print("用法: python -m env_tools.task_runner <task_id> [--stub-fail]", file=sys.stderr)
        return 2
    try:
        task_id = int(args[0])
    except ValueError:
        print(f"task_id 非法: {args[0]}", file=sys.stderr)
        return 2
    return run(task_id, stub_fail="--stub-fail" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
