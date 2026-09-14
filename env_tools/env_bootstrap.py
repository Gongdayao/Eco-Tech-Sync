#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
环境引导模块(叶子模块, 零依赖原则)
====================================
只允许依赖: 标准库 + python-dotenv。
严禁 import: openmind_hub / modelscope_hub / 任何 SDK / env_tools 其他模块。

为什么必须这样:
  openmind_hub 在 import 时(plugins/openmind/constants.py 模块级代码)就
  一次性读取 HUB_WHITE_LIST_PATHS 并固化进 WHITE_LIST_PATHS ——
  import 之后再设置 os.environ 无效(已实测: before=True / after=False)。
  同理适用于一切"import 时读取"的 SDK 环境变量(MODELSCOPE_ENDPOINT /
  MODELSCOPE_CACHE 等): 一律先设后导入。
  若本模块(或其 import 链)触发了 SDK 导入, 引导就失去了意义。

用法 —— 每个进程入口的最顶部(server-work.py / task_runner.py):

    # server-work.py(项目根):
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from env_tools.env_bootstrap import bootstrap_env
    bootstrap_env()
    # ---- 从这里开始才允许 import SDK / env_tools 其他模块 ----

    # task_runner.py(在 env_tools/ 内, 脚本模式运行时 sys.path[0] 是 env_tools/ 而非项目根):
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from env_tools.env_bootstrap import bootstrap_env
    bootstrap_env()
    # ---- 然后才 import SDK / v2 其他模块 ----

幂等: 可被重复调用 —— 入口必须调一次; env_tools 内 import 了 SDK 的业务模块
(如 tasks.py / task_runner.py)在自己的模块顶部防御性地再调一次也无害,
这样即使某个入口忘了先调, 只要它 import 的是这些"自引导"模块, 仍然安全。
"""
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bootstrap_env() -> None:
    """在每个进程入口、import 任何 SDK 之前调用(幂等, 可重复调用)。"""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=False)

    # 权重根目录: <项目根>/weights/<org_name>/{compare_weights,updown_weights}
    # WEIGHTS_PATH 未显式设置时默认取项目根 weights —— 由 __file__ 推导的绝对路径,
    # 与进程 CWD 无关(服务化 systemd/supervisor 下 CWD 不可控), 项目拷到任何机器
    # 都能跑; .env 里只在想放到别的盘时才设 WEIGHTS_PATH(2026-09 修正:
    # 曾把开发机绝对路径写死在 .env → 生产机路径错乱)。
    weights = os.environ.get("WEIGHTS_PATH") or os.path.join(PROJECT_ROOT, "weights")
    # 回写环境变量: config.yaml 的 ${WEIGHTS_PATH} 替换因此总能解析到一致值
    os.environ.setdefault("WEIGHTS_PATH", weights)
    # 魔乐 SDK 白名单路径: 支持逗号分隔多个; 设到权重根即可覆盖全部组织子目录
    if not os.environ.get("HUB_WHITE_LIST_PATHS"):
        os.environ["HUB_WHITE_LIST_PATHS"] = weights.rstrip("/") + "/"
    # 缓存与超时(可选, 与 v1 run.sh 行为对齐)
    os.environ.setdefault("XDG_CACHE_HOME", os.path.join(weights, ".openmind"))
    os.environ.setdefault("DEFAULT_REQUEST_TIMEOUT", "600")
    # 魔塔端点: 裸 modelscope.cn openapi 突发会被 WAF 403(YUNWAF_CLIENT_UNCLASSIFIED),
    # 必须用 www.modelscope.cn; 同样在 import modelscope_hub 之前设置
    os.environ.setdefault("MODELSCOPE_ENDPOINT", "https://www.modelscope.cn")
