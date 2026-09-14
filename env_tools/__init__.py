# env_tools 包 — 同步器 v2 业务代码(用户自行实现)
#
# ⚠ 铁律: 本文件必须保持"空"(或只含注释/docstring)。
#   任何 `import env_tools.xxx` 都会先执行本文件; 若这里 import 了 SDK
#   (openmind_hub / modelscope_hub 等), 就会在 bootstrap_env() 调用之前
#   触发 SDK 导入 —— 环境引导立刻失效。SDK 导入只允许发生在
#   `bootstrap_env()` 之后的模块里。
