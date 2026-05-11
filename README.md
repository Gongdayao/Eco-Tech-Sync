[English](README_en.md) | **中文**

# Eco-Tech 模型同步器

---

魔乐 (Modelers) / 魔搭 (ModelScope) / GitCode 模型自动同步器

一个后台服务，自动在三个国内模型托管平台之间同步 AI 模型权重 —— **Modelers（魔乐社区）**、**ModelScope（魔搭）** 和 **GitCode**。

## 功能特性

- **双向模型级同步** — 检测在一个平台上存在但另一个平台上缺失的模型，并自动镜像同步
- **文件级增量同步** — 对于两个平台上都存在的模型，按 SHA256 逐文件比对，仅上传变更的文件
- **GitCode 镜像创建** — 通过从 ModelScope 导入自动创建 GitCode 仓库
- **守护进程模式** — 作为后台服务持续运行，使用工作队列，定期刷新
- **优雅退出** — 支持 SIGTERM 信号停止，退出前自动保存未完成队列，重启后恢复
- **断点续传支持** — 并行 SHA256 比对和选择性重新下载，用于修复中断或不完整的同步
- **README/许可证处理** — 从 README 中去除 YAML 前置元数据，在 Modelers 上自动添加许可证信息

## 架构

```
Modelers (魔乐)  <── 双向模型同步 ──>  ModelScope (魔搭)
                                                        │
                                                        │ (仅模型发现)
                                                        ▼
                                                    GitCode
```

- **模型级同步** 在 Modelers 和 ModelScope 之间双向进行
- **文件级同步** 为单向：ModelScope → Modelers
- GitCode 仓库通过 ModelScope `.git` 导入 URL 创建，并提示启用拉取镜像模式

### 工作队列模型

守护进程维护一个内存工作队列（双端队列），每 12 小时刷新一次：

| 工作项类型 | 格式 | 说明 |
|---|---|---|
| 模型同步 | `[模型名称, 目标平台]` | 将整个模型同步到目标平台 |
| 文件更新 | `[模型名称, 平台, 文件名, "update"]` | 上传变更的文件 |
| 文件删除 | `[模型名称, 平台, 文件名, "delete"]` | 从 Modelers 删除过期文件 |

## 环境要求

- Python 3.8+
- Linux 服务器，具备足够的磁盘空间存放模型权重（通常数百 GB）
- **ModelScope**、**Modelers (openMind)** 和 **GitCode** 的 API 令牌

## 安装

```bash
# 克隆仓库
git clone <repo-url>
cd Eco-Tech-Sync

# 安装依赖
pip install -r requirements.txt
```

## 配置

### 1. 创建 `.env` 文件（基于 `.env` 模板）

```env
WEIGHTS_PATH="你的权重工作路径"
MODELERS_TOKEN="你的_modelers_令牌"
MODELERS_REPO_NAME="你的组织名称"
SCOPE_TOKEN="你的_modelscope_令牌"
SCOPE_REPO_NAME="你的组织名称"
GITCODE_TOKEN="你的_gitcode_令牌"
GITCODE_REPO_NAME="你的组织名称"
```

### 2. 验证 `config.yaml`

配置文件使用 `${ENV_VAR}` 占位符，由 `.env` 中的值替换。主要配置项：

| 配置项 | 说明 |
|---|---|
| `global` | 本地权重存储路径、日志名称 |
| `modelscope_cfg` | ModelScope API 凭证和支持的许可证 |
| `modelers_cfg` | Modelers API 凭证和支持的许可证 |
| `gitcode_cfg` | GitCode API 凭证和用于导入的 ModelScope 基础 URL |

### 3. 配置 `logging_config.yaml`

默认：每日滚动日志存储在 `log/` 目录，保留 90 天。

## 使用方法

### 启动守护进程（生产环境推荐）

```bash
bash run.sh
```

此命令通过 `nohup` 将 `server-work.py` 作为后台进程启动，PID 记录在 `log/daemon.pid`。守护进程会：
1. 恢复上次未完成的任务（如果存在 `.sync_queue_state.json`）
2. 通过比较三个平台生成工作队列
3. 逐个处理同步任务（任务之间暂停 60 秒）
4. 每 12 小时刷新工作队列
5. 队列为空时休眠 30 分钟再重新检查

### 停止守护进程

```bash
bash run.sh stop
# 或
bash run-stop.sh
```

发送 SIGTERM 信号，守护进程完成当前任务后保存队列状态并退出。若 30 秒内未退出则强制终止。

### 一次性批量同步

```bash
python static_work.py
```

计算一次完整工作队列，处理所有任务后报告成功/失败。

### 单个模型同步

```bash
bash run-single.sh
```

同步单个指定模型。编辑 `single_sync.py` 修改目标模型名称。

### 断点续传/修复同步

```bash
bash run-resume.sh
```

将本地 SHA256 哈希值与远程比对，仅下载变更的 `.safetensors` 文件。使用并行处理（64 个线程）实现快速比对。

## 项目结构

```
model_syn/
├── server-work.py          # 主守护进程（持续后台同步）
├── static_work.py          # 一次性批量同步
├── single_sync.py          # 单个模型同步
├── resume_sync.py          # 断点续传/修复中断的下载
├── park_sync.py            # 从 Modelers_Park 命名空间同步
│
├── utils/                  # 核心库
│   ├── model_tools.py      # 工作队列生成、文件差异对比、README/许可证工具
│   ├── model_updown.py     # 所有下载/上传/同步操作
│   └── gitcode_conn.py     # GitCode API 客户端
│
├── config.yaml             # 主配置文件
├── logging_config.yaml     # 日志配置文件
├── requirements.txt        # Python 依赖
├── .env                    # 密钥和环境变量（已 gitignore）
│
├── run.sh                  # 启动/停止守护进程
├── run-single.sh           # 启动单个模型同步
├── run-resume.sh           # 启动断点续传同步
├── run-stop.sh             # 停止守护进程
│
└── log/                    # 日志（已 gitignore）
    ├── info.log            # 应用日志（每日滚动）
    ├── server-std.log      # 守护进程标准输出
    └── daemon.pid          # 守护进程 PID
```

## 注意事项

- 仅同步包含 `.safetensors` 权重文件的模型；跳过不含权重的仓库
- ModelScope 自动生成的默认 LICENSE 文件在比对时会被检测并过滤
- 包含令牌的 `.env` 文件通过 `.gitignore` 排除在版本控制之外
- 日志和测试脚本被排除在版本控制之外
- Shell 脚本从 `.env` 读取 `WEIGHTS_PATH`，自动设置 `HUB_WHITE_LIST_PATHS` 和缓存目录
- 停止守护进程时，未完成的任务保存至 `.sync_queue_state.json`，重启后自动恢复
