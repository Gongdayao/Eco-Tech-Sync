# Eco-Tech-Sync-V2 — AI 权重双平台同步器(单向方案)

跨平台权重同步服务:以 **ModelScope(魔塔)为权威主站**,将组织下模型仓库镜像/对齐到
**Modelers(魔乐)**,并顺带维护 **GitCode 纯镜像**。本地 SQLite 全量记录元数据,
做三方对比、差异纠正、删除传播与统计,支持多组织、任务队列、紧急抢占与告警。

> 同步方向与历史: 早期"双向对称同步"方案因魔塔 API 不支持删除而封存(设计文档见 §14)。
> 当前为 **2026-09 定稿的单向方案**: 模型级新增双向、删除以魔塔为主;文件级魔塔为准严格镜像。

---

## 1. 功能特性

- **魔塔为主严格镜像**:魔塔文件增/改/删 → 魔乐对齐;**魔乐独有文件默认一律自动删除**
  (`sync.auto_delete_extra=true`,2026-09 定稿);关闭该开关则降级为人工确认(告警 + `clean` 命令);
- **模型级**:safetensors 准入、双端新增(反向建塔参考魔乐可见性)、魔塔删除传播到魔乐(无护栏)+ GitCode 对齐、魔乐缺失即时补齐;
- **gated(审批)权重三态处理**:魔乐无→静默剔除;魔乐隐藏→完全不管;魔乐**公开**→ critical 告警(请立即删除/隐藏);
- **状态转换检测**:公开/私有 → gated 转换有对应 warn/critical 提醒;gated → 公开/私有 自动回到常规同步;
- **可见性对比**(以魔塔为准):每轮对比,魔乐 API 无法改可见性 → 告警提醒后台人工处理;
- **README 管线**:魔塔 README 变换同步魔乐(front matter/license 映射);init(空/平台模板)不同步;
  魔塔**无 README** 视为空白(不删魔乐 README、不告警);license 双向兜底(魔塔元数据→魔乐建仓,
  魔乐 README front matter→魔塔建仓);
- **任务系统**:SQLite 队列、三档重启制(不设时间上限)、obsolete 作废、紧急抢占(priority=100 中断当前 worker)、崩溃恢复;
- **GitCode 纯镜像**:新增导入(返回驱动确认,对齐 v1)+ pull 开启提醒(仅同步器工作周期内导入的模型)
  + 删除对齐 + 全量补齐(scan_and_fill),平台侧 pull 自同步;
- **可见性(2026-09 补强)**:任务进度落库(`status` 显示"下载 i/N (x/y GB)"与已耗时)、`status --watch`
  实时刷新、对账/强哈希进度落库、每日日志文件、`alerts` 告警查询、`clean` 人工清理;
- **审计与统计**:全量元数据落库(org 隔离)、操作即落库审计(`audit_*` 告警类型)、`status`/`audit`/`alerts` 命令。

## 2. 目录结构

```
Eco-Tech-Sync-V2/
├── server-work.py           # 顶层入口: daemon/once/enqueue/status/alerts/cancel/audit/clean
├── config.yaml              # 多组织配置(${VAR} 注入, 密钥在 .env)
├── .env                     # 密钥(勿提交/外传)
├── requirements.txt         # 锁定依赖
├── env_tools/
│   ├── env_bootstrap.py     # 环境引导(必须在任何 SDK import 前执行)
│   ├── config.py            # config.yaml → OrgContext
│   ├── db.py                # SQLite(WAL/线程本地连接/SCHEMA_VERSION 7; v4 清理隐藏行, v5 rehash_checked_at, v6 tasks.progress/started_at, v7 rehash_fail_count/rehash_next_try_at + set_app_config upsert)
│   ├── tasks.py             # 任务状态机/三档重启/obsolete/抢占重排
│   ├── reconcile.py         # 对账: 模型级矩阵 + 文件级纠正器 + 可见性/gated/采纳
│   ├── transfer.py          # 传输: 只读拉取 + 建仓/上传/批量删除/基线回写
│   ├── pipeline.py          # README 变换/front matter/license_map/init 检测
│   ├── alerts.py            # 告警落库 + webhook + 每日摘要
│   ├── gitcode.py           # GitCode 纯镜像(导入/删除/scan_and_fill)
│   ├── runlog.py            # 每日日志导出: stdout 分流到 log/<YYYY-MM-DD>.log(逐行带时间戳, 跨零点自动换)
│   ├── poison.py            # 毒瘤/隐藏文件: 任一路径段 '.' 开头恒排除; fetch 源头过滤(不返回/不落库)
│   └── task_runner.py       # worker 子进程(预检→按 kind 分发)
└── utils/ratelimit.py       # 全局限速器(6 req/s + 抖动)
```

> 运行时生成(不纳入版本库): `weights/<org>/{updown,compare}_weights/`(权重暂存)、
> `sync.db`(基线库, WAL)、`log/<YYYY-MM-DD>.log`(每日日志)、`__pycache__/`。

## 3. 安装与环境

依赖版本锁定(`requirements.txt`; conda 环境名自定, 下文统一用 `<conda-env>`):

```bash
conda create -n <conda-env> python=3.11 -y && conda activate <conda-env>
pip install -r requirements.txt
# modelscope==1.39.1(仅环境锁定, 代码不使用)
# modelscope-hub==0.2.0(魔塔 API 主力)
# openmind-hub==1.3.0(魔乐 API)
```

## 4. 配置

### 4.1 `.env`(密钥,勿提交)

```bash
# WEIGHTS_PATH 可省略(2026-09): 默认 <项目根>/weights —— 由代码位置推导的绝对路径,
# 项目拷到任何机器都能跑; 仅当想放别的盘(如数据盘)才设绝对路径
SCOPE_TOKEN=...  SCOPE_REPO_NAME=Eco-Tech        # 魔塔
MODELERS_TOKEN=...  MODELERS_REPO_NAME=Eco-Tech  # 魔乐
GITCODE_TOKEN=...  GITCODE_REPO_NAME=Eco-Tech    # GitCode(可选)
ALERT_WEBHOOK_URL=""                             # 告警 webhook(可选)
```

### 4.2 `config.yaml`

- `orgs[]`:每个组织一组 scope/modelers/gitcode 配置 + `weights_subdir`;
- 节奏/传输/重试/告警配置启动时种子化进 DB(`app_config`,运行期可改):
  `sync.auto_delete_extra=true`(严格镜像:魔乐独有文件一律自动删;false=只告警人工确认)、
  `sync.forced_rehash_interval_d` / `sync.forced_rehash_batch`(强哈希复核周期与每轮上限)、
  `sync.delete_grace_cycles`(**仅模型级** repo 删除宽限轮数)、`sync.model_interval_min` 等;
- ⚠ 种子化是 `INSERT OR IGNORE`:改 config.yaml 不影响**已存在**的 DB,需改 `app_config` 或删库重建。

## 5. 操作方法(全量)

### 5.0 环境

```bash
conda activate <conda-env>       # 或 source <conda>/etc/profile.d/conda.sh 后 activate
cd <项目根>
```

所有命令通用参数:

| 参数 | 说明 |
|---|---|
| `mode` | `daemon` / `once` / `enqueue` / `status` / `alerts` / `cancel` / `audit` / `clean`(缺省 = daemon) |
| `--config PATH` | 指定 config.yaml(默认项目根 config.yaml) |
| `--org ID` | 组织过滤(默认全部组织) |
| `--force` | `audit`:忽略 15min 对账节流强制执行(默认受节流; daemon 不适用) |

### 5.1 首次部署流程(建议顺序)

```bash
# ① 首轮: 采纳(建两侧基线) + 强哈希首批 + 模型级/GitCode 缺失入队(不执行任务)
#    逐模型拉双侧文件树 + 分批下载小文件, 可能数十分钟; 有进度输出, 别误判卡死
python server-work.py audit

# ② 查看结果(队列/心跳/运行状态 + 告警分类)
python server-work.py status
python server-work.py alerts
python server-work.py status --json | python -m json.tool   # 美化/校验 JSON(脚本、监控用)

# ③ 启动常驻同步(见 5.8/§9); 第二轮对账会自动入队文件级增删改任务
python server-work.py daemon
```

⚠ 关键时序(2026-09-14 调度重构后):
- **首次部署/空库: 第一轮就是"全量"** —— 逐模型拉双侧文件树建基线(采纳)、写
  `models.files_verified_lm`, 然后强哈希分批建 sha256 基线(每轮上限 `forced_rehash_batch`);
  可能持续数十分钟, 有进度日志与 `status` 运行状态可看(见 5.3 / 5.11), 不是卡死。
  全量只在"库中尚无任何魔塔文件基线"或 `audit --force` 时发生。
- **此后每 15 分钟一轮只做轻量排查**: 拉一次两侧 model_list(魔塔分页 ~4 次 + 魔乐 1 次),
  逐模型比仓级 `last_modified` 与 `files_verified_lm` → 有变动的仓**直接入队 file_batch**;
  日常轮次**不拉任何文件树**(实测 176 个在管模型: 1.5s、0 次文件树请求)。
- **文件级怎么同步由任务执行时决定**: worker 针对该单仓做二次比对
  (魔塔 1 次 `list_repo_files` + 魔乐 1 次 `list_repo_tree`, 含每文件 sha256/blob_id),
  与基线比对后生成增/删/改动作; 魔塔缺/改/删 → 自动同步; 跨端同名不一致 → 以魔塔版覆盖;
  README 正文不一致 → 以魔塔版覆盖; 魔乐独有 → 严格镜像自动删(关闭 `sync.auto_delete_extra`
  时改为 `delete_manual` 告警 + `clean` 人工处理)。
- **魔乐侧有变动 = 该仓文件被改/破坏** → 同样入队, 由 worker 以魔塔版强制恢复(魔乐侧
  只考虑"被破坏需恢复"这一种情况; 用户手操的文件级更新一律引导到魔塔侧做)。
- 任务完成后回写 `files_verified_lm`; 任务失败则不回写 → 保持 dirty, 下一轮自动重新入队。

### 5.2 enqueue — 手动入队

```bash
# 常规: 魔塔 → 魔乐 模型级全量同步
python server-work.py enqueue --org Eco-Tech --model DeepSeek-X --direction to_modelers

# 反向: 魔乐独有权重 → 建到魔塔(可见性参考魔乐)
python server-work.py enqueue --org Eco-Tech --model DeepSeek-X --direction to_scope

# 紧急插队(priority=100, 立即抢占当前 worker)
python server-work.py enqueue --org Eco-Tech --model DeepSeek-X --direction to_modelers --urgent

# 普通插队(priority=10, 排队首但等当前任务结束)
python server-work.py enqueue --org Eco-Tech --model DeepSeek-X --direction to_modelers --queue

# 指定任务类型(kind 默认 model_sync)
python server-work.py enqueue --org Eco-Tech --model X --kind file_batch --direction to_modelers
python server-work.py enqueue --org Eco-Tech --model X --kind repo_delete --direction to_modelers
python server-work.py enqueue --org Eco-Tech --model X --kind gitcode_import
```

| 参数 | 必填 | 取值/说明 |
|---|---|---|
| `--org` | 是 | 组织 id(须在 config.yaml 定义) |
| `--model` | 是 | 模型名(不带组织前缀) |
| `--kind` | 否 | `model_sync`(默认)/ `file_batch` / `gitcode_import` / `repo_delete` |
| `--direction` | 否 | `to_modelers`(默认, 魔塔→魔乐)/ `to_scope`(魔乐→魔塔, 反向新增用) |
| `--urgent` | 否 | 紧急插队 priority=100(与 --queue 互斥) |
| `--queue` | 否 | 普通插队 priority=10 |

**去重语义**:同一 (org, kind, model, direction) 任务若还在 pending/claimed/running 则不会重复入队
(返回原任务 id);已结束(succeeded/failed/obsolete)后再次入队会**重建**新任务——所以想重跑某个
已结束的任务,直接再 enqueue 一次即可。

**删除类任务注意**:`repo_delete` 会执行魔塔删除传播(删魔乐 repo + GitCode 对齐 + 清 DB);
`file_batch` 默认**严格镜像**——魔塔已删文件与魔乐独有文件都在**同一任务内先上传后删除**
(一次 commit 批量删, 文件级**无宽限**), 保证魔乐文件集与魔塔严格一致、不出现"新版已传旧版未删"
的下载窗口。关闭 `sync.auto_delete_extra` 后, 魔乐独有降级为 `delete_manual` 告警 + `clean --yes`。

**空转防护(2026-09)**:file_batch **只在 worker 有可执行动作时入队**——关闭严格镜像时,
受保护的魔乐独有文件不再驱动入队(执行也是空转), 改为对账侧按模型去重告警(`delete_manual`);
魔塔 init README(空/模板)永不因"魔乐侧 is_init"强制纠正而入队。

### 5.3 status — 查看队列/状态(运行期间随时可用)

```bash
python server-work.py status                          # 队列统计 + 心跳 + 最近任务
python server-work.py status --status running         # 只看某个状态(pending/claimed/running/
                                                      #   succeeded/failed/interrupted/obsolete)
python server-work.py status --org Eco-Tech           # 按组织过滤
python server-work.py status --limit 50               # 最近 N 条(默认 100)
python server-work.py status --json                   # JSON 输出(脚本/监控)
python server-work.py status --watch                  # 实时刷新(默认 5s, 类似 top; Ctrl+C 退出)
python server-work.py status --watch 10               # 每 10s 刷新
```

输出说明(2026-09 起带进度,不必翻 journal):

```
任务统计(--org=全部): pending=2 claimed=0 running=1 succeeded=9 ...
心跳: last_cycle_at=...(3s 前) last_task_at=... pid=...   ← daemon 是否活着/最近干活时间
运行状态[Eco-Tech]: 对账=文件级 83/173 耗时 412s | 强哈希=分批进行中: 本轮核对 400/800
最近任务(N 行):
  #3 [   running] prio=  0 attempts=1/3 Eco-Tech file_batch DeepSeek-V4-... to_modelers 已耗时=23min 进度: 上传 12/77 (92.00/628.00 GB)
  #4 [   pending] prio=  0 attempts=0/3 Eco-Tech file_batch DeepSeek-V4-... to_modelers
```

- `--json` 输出**纯 JSON**(诊断信息如 `[runlog]`、配置警告走 stderr), 可直接接
  `python -m json.tool` / `jq` 做美化或喂给监控脚本;
- **进度落库**: worker 把 `下载 i/N (x/y GB)`、`上传 …`、`删除 …` 写入 tasks.progress, status 直接显示;
- **运行状态**: 对账轮每 20 个模型刷新 `sync.reconcile_progress`, 强哈希每 50 个文件刷新 `sync.rehash_progress`;
- **对账期间心跳**: 轮内也会刷 `last_cycle_at`(只刷时间不覆盖 pid), 不再表现为"心跳停滞=挂了";

| 状态 | 含义 |
|---|---|
| pending | 排队中,等 worker 取 |
| claimed | 已被 worker 取走(短暂) |
| running | 正在执行(子进程干活) |
| succeeded | 完成 |
| failed | 三档重试耗尽/不可重试 → 等人工(看 last_error) |
| interrupted | 被紧急任务抢占/中断(会自动重排,不消耗次数) |
| obsolete | 执行前预检发现源 repo 已消失,任务作废 |

直接查库(可选): `sqlite3 sync.db "SELECT id,status,kind,model,attempts,last_error FROM tasks WHERE status IN ('pending','running');"`

### 5.4 alerts — 查看告警

```bash
python server-work.py alerts                        # 最近 24h 告警(默认 20 条,倒序)
python server-work.py alerts --level critical       # 只看 critical
python server-work.py alerts --org Eco-Tech         # 按组织
python server-work.py alerts --since 168            # 最近 168 小时
python server-work.py alerts --limit 50             # 条数上限
python server-work.py alerts --json                 # JSON 输出(脚本/监控)
```

输出示例(每条含分类标签):

```
告警(最近24h, org=全部): critical=1 warn=3 | 显示 4 条
  #4 [09-07 09:02]     warn [提醒]   org=Eco-Tech model=GLM-5.2-w4a4c8-mxfp4 task=-
      GitCode 镜像已导入 ..., 请前往 .../setting/mirror 开启 pull 同步
  #2 [09-07 09:02]     warn [需人工] org=Eco-Tech model=Qwen3-VL-... task=-
      delete_manual: 魔乐独有文件含删除保护(权重/超大/未开 auto_delete_extra)...
  #1 [09-07 09:02] critical [任务失败] org=Eco-Tech model=GLM-... task=3
      任务连续失败3次, 停止拉起, 等待人工排查: ...
```

分类标签与告警类型(2026-09):

| 标签 | 类型(告警前缀/特征) | 处理 |
|---|---|---|
| 审计留痕 | `audit_delete` / `audit_delete_file` / `audit_clear` | 同步器操作记录,无需处理 |
| 需人工 | `delete_manual` / `vis_mismatch` / `gated_public_on_modelers`(critical) / `gated_converted` / `dual_upload` / `scope_delete_manual` / `rehash_mismatch` / `rehash_download_failed`(强哈希连续 3 次下载失败, 已按指数退避继续重试) | 需到平台人工处理(删除/可见性/gated; `rehash_download_failed` 多为文件不可下载, 确认后可忽略) |
| 任务失败 | critical: 不可重试失败 / 连续失败 N 次 / [紧急任务失败] | 带 task_id,配合 `status --status failed` 反查 |
| 提醒 | GitCode 镜像导入 → 开启 pull;`content_mismatch`(跨端同名不一致, 已自动以魔塔版覆盖);`readme_body_mismatch`(README 正文不一致, 已自动以魔塔版覆盖) | 知悉即可(会自动收敛) |

task_id 列可与 `python server-work.py status --status failed`(或直接查 tasks 表)联动定位具体任务。

### 5.5 cancel — 取消任务

```bash
python server-work.py cancel --task-id 12
```

只可取消 `pending` / `claimed` 的任务;已 running 的任务需抢占(入队 urgent)或停 daemon。

### 5.6 audit — 摸底/全量对账

```bash
python server-work.py audit                    # 全部组织(受 15min 节流)
python server-work.py audit --org Eco-Tech
python server-work.py audit --force            # 忽略 15min 节流强制执行
python server-work.py audit --org Eco-Tech --force
```

节流说明(2026-09-14):`audit` 与 daemon 共用 `sync.last_reconcile` 节流窗口(`sync.model_interval_min`,
默认 15min), 避免手动 audit 与常驻轮次重复逐模型拉双侧文件树。距上次对账不足窗口时默认
`{'skipped': True}` 并打印提示; `--force` 忽略窗口强制执行(仍会刷新节流标记)。
daemon 本身恒受节流, 不受 `--force` 影响。**被跳过时连 GitCode 全量补齐也一并跳过**
(`--force` 或窗口过期才跑), 避免"跳过"只跳一半。

内容(2026-09-14 调度重构):模型级矩阵 + **队列生成(仅拉两侧 model_list, 按仓级 last_modified 入队 file_batch)** + 文件级全量对账(**仅首次部署/`--force`**, 日常由任务执行时按单仓比对) + 强哈希(首次全量核验: LFS 零下载交叉比对 + 非 LFS ≤50MB 下载建 sha256 基线, 之后每 30d 复核; 分批 forced_rehash_batch, 零大文件下载; 周期标记 `sync.last_forced_rehash`/节流标记 `sync.last_reconcile` 一律走 upsert 写入(2026-09-14 修复: 旧实现用 seed_app_config=INSERT OR IGNORE, 只写一次 → 周期与节流双双失效); 周期边界按 `rehash_checked_at <= cycle_start`(否则分多轮完成时最后一轮那批永不复核、前几轮反被重下); 失败行按 `rehash_fail_count`/`rehash_next_try_at` 指数退避(6h 倍增, 上限 7d; 退避到期由"纯重试轮"跟进, 不受 30d 闸门阻挡);**README.md 不参与强哈希跨端比对**——魔乐版是 README 管线变换产物, 裸 sha256 必然不同; 正文一致性由对账轮单独校验并自动纠正)+
GitCode 全量补齐(scan_and_fill,若有 gitcode 配置)。只读平台 + 入队,不执行任务。

⚠ 空库首轮 audit = 采纳(建基线, 不产生文件级增删改任务); **第二轮**才按差异入队(见 5.1)。

### 5.7 once — 跑一轮后退出(替代 v1 static_work)

```bash
python server-work.py once             # 取完当前 pending 任务即退出(适合手动/定时)
python server-work.py once --org Eco-Tech
```

### 5.8 daemon — 常驻进程

```bash
python server-work.py daemon           # 前台常驻(调试); Ctrl-C = 优雅退出
```

- 无参启动默认进入 daemon(`python server-work.py`)——兼容 v1 run.sh 无参启动;
- 行为:崩溃恢复 → 循环{先取任务 → 空闲时对账(15min 节流)→ 心跳};紧急任务(priority=100)
  会中断当前 worker 并优先执行;SIGTERM/SIGINT 优雅退出(≤0.5s 响应);
- 长任务输出(2026-09): 子进程日志实时回显;下载/上传进度条节流回显(≥8s 一条);
  全程静默 >120s 打运行心跳 —— 长任务期间 journal 不会"没动静";
- 日志(2026-09): stdout 同时写 `<项目根>/log/<YYYY-MM-DD>.log`(每日一个文件, 跨零点自动换,
  **每行带 `[YYYY-MM-DD HH:MM:SS]` 时间戳**, 含 daemon/任务/上传删除清单);
  stdout/journald 保持原样(journald 自带时间戳, 不重复); 目录可用 `SYNC_LOG_DIR` 覆盖;
- 生产部署用 systemd(见 §9): `systemctl start/stop/status eco-sync-v2`、`journalctl -u eco-sync-v2 -f`;
- 手动后台: `nohup python -u server-work.py daemon > log/daemon.log 2>&1 &`
  (停止: `kill -TERM <pid>` 优雅退出)。

### 5.9 常用场景速查

| 场景 | 命令 |
|---|---|
| 首次部署摸底/采纳 | `audit` → 等第二轮(或间隔 ≥15min 再 `audit`)→ `status` / `alerts` |
| 日常巡检 | `status`(队列/心跳/运行状态);`status --watch` 实时盯盘 |
| 查看告警 | `alerts`(分类:审计留痕/需人工/任务失败/提醒) |
| 手动补一个模型同步 | `enqueue --org Eco-Tech --model X --direction to_modelers` |
| 紧急同步某权重 | 上面命令加 `--urgent`(立即抢占) |
| 魔塔网页删了权重后手动对齐 | `enqueue --org Eco-Tech --model X --kind repo_delete --direction to_modelers` |
| 魔乐新增权重反向建到魔塔 | `enqueue --org Eco-Tech --model X --direction to_scope` |
| 重跑已结束的任务 | 直接再 `enqueue`(终态自动重建) |
| 取消误入队任务 | `cancel --task-id N` |
| 查看任务详情/错误 | `status --status failed`(看 last_error) |
| 清理魔乐独有旧文件 | `clean --org O --model X`(dry-run)→ 加 `--yes` 执行 |

### 5.10 clean — 清理魔乐独有文件(人工确认/兜底)

默认**严格镜像**下, 魔乐独有文件由 file_batch 自动删除(见 §6); 本命令用于:
① 关闭 `sync.auto_delete_extra` 时执行人工确认删除; ② dry-run 查看某模型当前"魔乐独有"清单。

```bash
python server-work.py clean --org Eco-Tech --model X            # dry-run: 只列清单(路径+大小+合计)
python server-work.py clean --org Eco-Tech --model X --yes      # 执行删除(一次 commit 批量删)
```

删除会一次 commit 批量完成 + 清双侧 DB 行 + 留 `audit_delete_file` 审计告警;
安全:魔塔文件集拉取失败/为空 → 拒绝执行; 隐藏/毒瘤文件永不删。
**⚠ 顺序要求**:若手动执行, 先确认该模型新版已同步到魔乐, 再删旧文件(避免"缺文件"窗口)。

### 5.11 日志与可观测性(2026-09)

| 渠道 | 内容 |
|---|---|
| `status` / `status --watch` | 队列统计、心跳(带停滞秒数)、运行状态(对账 `i/N`、强哈希 `x/上限`)、running 任务**已耗时 + 进度** |
| `alerts` | 告警查询与分类(审计留痕 / 需人工 / 任务失败 / 提醒) |
| `journalctl -u eco-sync-v2 -f` | 全量运行日志(daemon/任务/子进程输出/进度条节流回显) |
| `<项目根>/log/<YYYY-MM-DD>.log` | **每日日志文件**, **逐行带 `[YYYY-MM-DD HH:MM:SS]` 时间戳**(跨零点自动换; `SYNC_LOG_DIR` 可改目录; 与 journald 同时写) |
| 提交清单日志 | 每次上传/删除 commit 列出文件清单(≤20 全列 + 计数 + 总量), 如 `[sync] X file_batch 删除(一次 commit): 75 个文件 (314.63 GB)` |
| 逐文件进度 | 下载逐文件一行 `[sync] X 下载 3/77 (18.40/628.00 GB): path`; 上传/删除为批次粒度(一次 commit) |
| 进度落库 | `tasks.progress/started_at`; 对账/强哈希进度写 `app_config` 的 `sync.reconcile_progress` / `sync.rehash_progress` |
| stdout / stderr 约定 | stdout = 命令结果(JSON 模式为纯 JSON);stderr = 诊断(日志目录提示、配置警告、SDK 告警), 便于管道处理 |

> systemd 部署务必设 `Environment=PYTHONUNBUFFERED=1`(见 §9.4), 否则 Python stdout 块缓冲会让
> 日志滞留在内存里、journal 长时间不刷新(2026-09 实测)。


## 6. 同步规则速览

| 域 | 规则 |
|---|---|
| 准入 | repo 至少一个 `.safetensors` 才管理;魔塔 list 为准(魔塔对非权重 repo 隐身) |
| 模型级新增 | 魔塔新增(有权重非 gated)→ 同步魔乐;魔乐独有权重 → 反向建塔(可见性参考魔乐);魔塔隐身存在 → 略过+告警一次 |
| 模型级删除 | 魔塔 repo 消失(**模型级宽限 3 轮**, DB 曾有权重)→ 删魔乐 repo(无护栏)+ GitCode 对齐 + 清双侧行;魔塔本体人工网页删;魔乐缺 → 即时补齐 |
| 文件级 | 调度(2026-09-14):队列生成只拉两侧 model_list, 仓级 `last_modified` 与 `models.files_verified_lm` 不一致即入队 file_batch(零文件树请求); 单仓的逐文件比对在任务执行时做。魔塔为准单向:增/改/魔乐缺(有基线行)→整批同步魔乐;**魔塔删 → 同轮整批删(无宽限, 对齐 v1: 先上传后删除)**;魔乐独有→**严格镜像自动删**(`auto_delete_extra=true`;关闭后为人工确认);不一致→魔塔版覆盖(**含跨端同名内容不一致**, 2026-09: 魔塔 sha256 vs 魔乐 sha256/LFS/local 基线, README 除外, 自动以魔塔版覆盖 + content_mismatch 告警)。存量缺失(魔乐基线无行且树上无)2026-09 起自动补(人工排查确认属纯缺失, 含整目录 TP1/t2v/optional; 子目录路径保真同步) |
| gated | 三态:魔乐无→静默;魔乐隐藏→不管;魔乐公开→critical 告警;转换检测(私有→gated warn 等) |
| 可见性 | 魔塔为主,每轮对比;魔乐 API 无法改 → 告警人工 |
| README | 2026-09 规则:魔塔**无 README** = 空白 → 不同步、不删魔乐 README、不告警;**不参与跨端同名内容比对与强哈希核对**(front matter/license 归一化导致裸哈希必然不同);**正文一致性单独校验**:每轮剥离 front matter 比正文, 不一致 → 自动以魔塔版覆盖魔乐 + `readme_body_mismatch` 留痕告警(每模型下载两侧 README 小文件比对; 已在待同步集则跳过);比较口径(2026-09-14):front matter 块**前后空行容忍**(兼容首行空行/BOM/CRLF)、正文取"第一行非空行 ~ 最后一行非空行"(**首尾空行容忍**), **正文内部空行与排版差异不容忍**(不逐行 rstrip、不折叠内部空行)——避免"首行空行致剥头失败→每轮空转"与"元数据被当正文写进目标卡片"两类问题;魔塔 init(空/模板)不同步;real → 变换同步魔乐(双向模型级上传前判 init, init 不传);魔乐 README 永不因独有删除;license 由建仓参数从魔塔元数据兜底传递 |
| GitCode | 纯镜像:魔塔公开新增→导入(**返回驱动确认**, 对齐 v1);导入成功提示开启 pull(**仅同步器工作周期内导入的模型**, 非每日);魔塔删除确认→删除对齐;`scan_and_fill` 全量补齐 |

## 7. 任务与状态机

- 状态:`pending → claimed → running → succeeded/failed/interrupted/obsolete`;
- **三档重启制**:attempts=claim+1;第 1 次失败原位重启、第 2 次压队尾、达上限 failed+告警;
  **不设执行时间上限**(大权重传输可能超 6h,以失败次数为准);
- **obsolete**:执行前预检源 repo 消失 → 作废(不占次数不告警);
- **紧急抢占**:`enqueue --urgent`(priority=100)→ daemon 中断当前 worker → 重排(不消耗次数)优先执行;
- 去重:同 dedup_key 非终态去重;终态任务可重建(新一轮差异可重新入队);
- **per-model 租约**:同一 `(org, model)` 同时只允许一个 claimed/running 任务(跨 kind),
  防止模型级与文件级并发互踩(排队任务等它结束);
- **进度可见**(2026-09):执行进度写 `tasks.progress`, 领取时刻 `tasks.started_at`, `status` 直接显示。

## 8. 本地 DB

- `sync.db`(首次自动创建,WAL;当前 **SCHEMA_VERSION 6**):`models/files/tasks/alerts/app_config/license_map/heartbeat`;
- 关键列:`tasks.progress/started_at`(执行进度/领取时刻)、`files.blob_id`(魔乐非 LFS 变更指纹)、
  `files.rehash_checked_at`(强哈希周期内已核验标记)、`files.rehash_fail_count`/
  `files.rehash_next_try_at`(强哈希失败计数与下次重试时间, 指数退避)、`files.sha256_source`(`api`/`local`/`none`)、
  `files.last_synced_at`(同步滞后)、`models.files_verified_lm`(v8: 该侧仓"已核验文件树"的
  last_modified, 队列生成据此判 dirty; 任务成功后回写, 失败保持 dirty 下轮重入队);
- 运行状态键:`app_config` 的 `sync.reconcile_progress` / `sync.rehash_progress`(供 `status` 展示);
- 隐藏/毒瘤文件在 fetch 源头过滤、不落库(v4 迁移已清理历史遗留行);
- **启动即 `migrate()`**(幂等): 补齐缺失的追加列(与版本号解耦)、按需重建 tasks、执行一次性数据迁移;
  新库建表 SQL 与升级路径保持一致(2026-09 加固, 防"版本够新但列缺失"的运行期报错);
- **基线规则**:指纹只在同步成功后回写;魔塔人工操作由下一轮对账感知(轮询边界);
- 操作即落库审计:`audit_delete` / `audit_delete_file` / `audit_clear` 等告警类型留痕。

## 9. 部署(单机自用)

> 形态:一台能访问魔塔/魔乐/GitCode 的 Linux 机器 + systemd 常驻。
> 下文占位符: `<repo-url>` / `<project-dir>` / `<conda-env>` / `<user>` / `<ORG>`。

### 9.1 获取代码与环境

```bash
git clone <repo-url> <project-dir> && cd <project-dir>
conda create -n <conda-env> python=3.11 -y && conda activate <conda-env>
pip install -r requirements.txt
```

出网要求:`www.modelscope.cn` / `modelers.cn` / `api.gitcode.com` / `gitcode.com` 可达;
走代理时在 systemd unit 里加 `Environment=HTTPS_PROXY=...`。

### 9.2 配置与密钥

```bash
cd <project-dir>
vi .env          # 各平台 TOKEN / REPO_NAME(可选 ALERT_WEBHOOK_URL), 见 §4.1
chmod 600 .env
vi config.yaml   # orgs[]: 组织 id / repo_name / weights_subdir, 见 §4.2
```

`.env` 不进版本库, 各机器单独创建;权重目录默认 `<项目根>/weights`(无需配置),
要放数据盘再设 `WEIGHTS_PATH`。

### 9.3 首次启动验证

```bash
python server-work.py audit     # 首轮采纳 + 强哈希首批(逐模型拉树, 可能数十分钟, 有进度输出)
python server-work.py status    # 队列/心跳/运行状态
python server-work.py alerts    # 告警分类
```

### 9.4 systemd 常驻

`/etc/systemd/system/eco-sync-v2.service`:

```ini
[Unit]
Description=Eco-Tech Sync v2
After=network-online.target

[Service]
User=<user>
WorkingDirectory=<project-dir>
ExecStart=<conda-env>/bin/python server-work.py daemon
Restart=always
RestartSec=10
# 必须: 关掉 Python stdout 块缓冲, 否则 journal 日志会滞留(2026-09 实测)
Environment=PYTHONUNBUFFERED=1
# 走代理时:
# Environment=HTTPS_PROXY=http://proxy:port

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now eco-sync-v2      # 开机自启 + 立即启动
systemctl status eco-sync-v2            # active (running)
journalctl -u eco-sync-v2 -f            # 实时日志
```

自愈:进程被杀 → systemd 自动拉起 → 启动先 `crash_recovery`(claimed/running 回 pending)→ 续跑。

### 9.5 更新与回滚

```bash
cd <project-dir>
git pull && systemctl restart eco-sync-v2                       # 更新
git checkout <tag|commit> && systemctl restart eco-sync-v2      # 回滚
```

- 代码含 schema 迁移时, 启动 `db.migrate()` 自动处理;回滚到不支持新 schema 的旧代码前,
  先备份 `sync.db`;
- **只跑单实例**:双实例会互相 `crash_recovery` 抢任务。

## 10. 日常操作手册

> 日常只需记三条:`status`(看状态)→ `alerts`(看告警)→ `log/<日期>.log` / `journalctl`(看细节)。
> 所有命令在项目根、激活 conda 环境后执行(见 §5.0)。

### 10.1 巡检(每天/随时)

```bash
python server-work.py status              # 队列 + 心跳 + 运行状态 + running 任务进度
python server-work.py status --watch 5    # 实时盯盘(类似 top, Ctrl+C 退出)
python server-work.py alerts              # 最近 24h 告警(分类标签: 审计留痕/需人工/任务失败/提醒)
python server-work.py alerts --level critical --since 168   # 近 7 天 critical
tail -f log/$(date +%F).log               # 当天详细日志(逐文件进度、提交清单)
journalctl -u eco-sync-v2 -f              # systemd 全量日志
```

判读要点:

| 现象 | 含义 |
|---|---|
| `心跳: ... (3s 前)` | daemon 活着;数值很大且无 running 任务 → 可能异常 |
| `运行状态: 对账=文件级 83/173 ...` | 正在整轮对账(逐模型拉双侧文件树), 属正常 |
| `#3 [running] ... 已耗时=23min 进度: 上传 12/77 (92/628 GB)` | 长任务进行中, 单 worker 串行, pending 排队正常 |
| `pending=n running=0` 且心跳新鲜 | 若长时间不动 → 检查 daemon 是否在跑/是否双实例/CLI 与 service 是否同一 `sync.db` |

### 10.2 魔塔更新权重后会发生什么(全自动)

1. 下一轮对账(默认 15min 节流)发现差异 → 入队 `file_batch`;
2. 执行顺序:**先一次 commit 上传**(新增 + 同名覆盖)→ **再一次 commit 删除**(魔塔已删 + 魔乐独有,
   严格镜像);因此不会出现"新版已传、旧版未删"的下载窗口;
3. 过程中 `status` 显示 `下载 i/N (x/y GB)`、`上传/删除 …`;日志有逐文件行与 commit 清单;
4. 完成后该模型无 pending;删除动作留 `audit_delete_file` 审计告警。

想立即触发(不等 15min):`python server-work.py audit --force`(忽略节流)或 `systemctl restart eco-sync-v2`
(重启后先 claim 队列, 对账稍后)。

### 10.3 手动补同步 / 定向操作

```bash
# 模型级全量(魔塔→魔乐): 建仓+下载+上传(幂等, 服务端哈希去重)
python server-work.py enqueue --org <ORG> --model X --direction to_modelers

# 反向: 魔乐独有权重 → 建到魔塔(可见性与 license 参考魔乐)
python server-work.py enqueue --org <ORG> --model X --direction to_scope

# 只做文件级纠正(执行时重算 diff, 适合"只补差异文件")
python server-work.py enqueue --org <ORG> --model X --kind file_batch --direction to_modelers

# 魔塔网页删了 repo 后, 让魔乐/GitCode 对齐删除
python server-work.py enqueue --org <ORG> --model X --kind repo_delete --direction to_modelers

# 紧急插队(priority=100, 立即中断当前 worker, 不消耗失败次数)
python server-work.py enqueue --org <ORG> --model X --direction to_modelers --urgent
```

### 10.4 魔乐独有文件(严格镜像 vs 人工)

- **默认严格镜像**(`sync.auto_delete_extra=true`):魔乐独有文件自动删(含权重/大文件),
  一次 commit 完成, 日志与 `audit_delete_file` 可溯源;
- **临时改人工把关**:把开关改成 `false`(见 10.7), 之后独有文件只报 `delete_manual` 告警,
  确认后用 CLI 删除:

```bash
python server-work.py clean --org <ORG> --model X            # dry-run: 列清单(路径+大小+合计)
python server-work.py clean --org <ORG> --model X --yes      # 执行(一次 commit 批量删)
```

- ⚠ 手动删除顺序:先确认新版已同步到魔乐(该模型 file_batch 成功), 再删旧文件;
- `clean` 的安全底线:魔塔文件集拉取失败/为空 → 拒绝执行;隐藏/毒瘤文件永不删。

### 10.5 告警处理对照表

| 告警(前缀) | 含义 | 处理 |
|---|---|---|
| `delete_manual` | 关闭严格镜像时的魔乐独有文件 | `clean --yes`, 或把 `auto_delete_extra` 改回 true |
| `vis_mismatch` | 魔乐可见性与魔塔不一致(魔乐 API 不可改) | 到魔乐后台改 |
| `gated_public_on_modelers`(critical) | 审批权重在魔乐公开可见 | 立即删除/隐藏该魔乐镜像 |
| `gated_converted` | 魔塔权限 私有→申请制 | 知悉即可 |
| `dual_upload` | 魔乐独有权重但魔塔 `repo_exists=True` | 人工判断保留哪端 |
| `scope_delete_manual` | 魔塔 repo 已删, 同步器将删魔乐/GitCode | 知悉;若误删可先 `cancel` |
| `content_mismatch` | 跨端同名内容不一致 | **已自动以魔塔版覆盖**, 知悉即可 |
| `rehash_mismatch` | 强哈希复核不一致 | 排查魔乐文件是否被外部改动/平台异常 |
| 任务失败(critical) | 三档重试耗尽或不可重试 | `status --status failed` 看 `last_error`, 处理后重新 `enqueue` |
| GitCode 导入提醒 | 同步器新导入的镜像 | 去 `ai.gitcode.com/<org>/<model>/setting/mirror` 开启 pull |

### 10.6 任务排查 / 取消 / 重跑

```bash
python server-work.py status --status failed       # 失败任务与 last_error
python server-work.py status --status running      # 当前任务 + 进度 + 已耗时
python server-work.py status --status pending      # 排队情况
python server-work.py cancel --task-id N           # 取消 pending/claimed(running 需抢占或停服)
python server-work.py enqueue ...                  # 重跑(终态任务会自动重建)
```

- 长任务看起来"没动静":看 `status` 的 running 进度、`ps aux | grep task_runner`、网卡流量;
- 崩溃/重启后遗留的 claimed/running 会自动回 pending(不消耗失败次数);
- 三档重启制:第 1 次失败原位重启、第 2 次压队尾、第 3 次 failed + critical 告警(**无执行超时限制**)。

### 10.7 查看/修改运行期配置(DB 优先)

```bash
# 任务/告警/文件基线速查
sqlite3 sync.db "SELECT id,status,kind,model,attempts,last_error FROM tasks ORDER BY id DESC LIMIT 20;"
sqlite3 sync.db "SELECT level,COUNT(*) FROM alerts GROUP BY level;"
sqlite3 sync.db "SELECT platform,COUNT(*) FROM files WHERE repo_id LIKE '%/X' GROUP BY platform;"

# 查看/修改运行配置(app_config; 优先级高于 config.yaml 的种子值)
sqlite3 sync.db "SELECT key,value FROM app_config WHERE org='<ORG>' ORDER BY key;"
sqlite3 sync.db "UPDATE app_config SET value='false' WHERE org='<ORG>' AND key='sync.auto_delete_extra';"
```

常用键:

| 键 | 默认 | 说明 |
|---|---|---|
| `sync.model_interval_min` | 15 | 对账节流(分钟) |
| `sync.auto_delete_extra` | true | 严格镜像:魔乐独有文件自动删;false=人工确认 |
| `sync.forced_rehash_interval_d` | 30 | 强哈希复核周期(天) |
| `sync.forced_rehash_batch` | 800 | 强哈希每轮处理上限(首次全量分多轮) |
| `sync.delete_grace_cycles` | 3 | **仅模型级** repo 删除宽限轮数 |
| `alert.webhook_url` | 空 | 告警 webhook(不配则只落库) |

> 注意:`config.yaml` 的种子化是 `INSERT OR IGNORE`——改 yaml 只对**新库**生效,
> 已存在的库要按上面的 SQL 改 `app_config`。

### 10.8 备份与迁移

- 必须保留:`.env`、`config.yaml`、`sync.db`(基线/队列/告警);
- 可丢弃:`weights/`(暂存)、`log/`(日志)、`__pycache__/`;
- 迁移:新机器 `git clone` + 装依赖 + 放回上述三个文件 + 起服务即可;
  权重目录默认跟随项目位置(要放数据盘设 `WEIGHTS_PATH`)。

### 10.9 常见问题

| 现象 | 原因/处理 |
|---|---|
| `systemctl start` 后 journal 只有 Started | unit 缺 `Environment=PYTHONUNBUFFERED=1`(见 §9.4) |
| audit/对账长时间无输出 | 正常:逐模型拉双侧文件树 + 强哈希分批;看 `status` 运行状态或日志 `文件级进度 i/N` |
| pending 长时间不动 | daemon 未运行 / 正在跑长任务(单 worker) / 双实例 / CLI 与 service 目录不同(两个 `sync.db`) |
| 任务失败 3 次 | 看 `last_error`;修正后重新 `enqueue`(终态自动重建) |
| 魔乐文件树偶发 500 `context canceled` | 平台侧瞬时故障, 该模型本轮跳过、下轮自动重试 |
| 魔乐非 LFS 文件没有 sha256 | 平台 API 限制, 变更检测用 `blob_id`;内容 sha256 由强哈希下载建立(`sha256_source='local'`) |
| 想换 token | 改 `.env` → `systemctl restart eco-sync-v2` |
| 想清空重来 | 停服 → 删 `sync.db*` 与 `weights/` → 起服(首轮采纳, 见 §5.1) |

## 11. 平台限制与已知问题(实测 2026-09)

1. **魔塔删除受限**:`delete_repo` 与 `delete_files` API 均 401(仅网页控制台)→ 魔塔侧删除/文件删除只能人工,同步器负责魔乐对齐与提醒;
2. **魔塔 list 过滤**:无 safetensors 的 repo 不进 list/文件树 API 404(初始化中/非权重仓)→ 准入过滤的依据;曾出现文件树 API 瞬时返回空的暂态(同步器有 abort 保护);
3. **GitCode 镜像导入**:曾持续报 `url unreachable` —— 根因(2026-09 已修)是 `gitcode.py` 误读 `org.scope.scope_base_url`(恒为空), import_url 拼成无协议头残缺 URL;现从 `gitcode` 段读取并兜底默认值,失败任务重入队即可;`scan_and_fill` 会补齐。另: 导入确认曾用轮询探测(匿名端点恒 400)→ 任务成功却无 pull 开启提醒、状态滞留 pending;已改**返回驱动确认(对齐 v1: 2xx + 返回体 name/full_name/url 字段)**, 成功即提醒, 不再轮询; 提醒只在【同步器工作周期内】gitcode_import 成功时发, 存量已导入(含首次部署前已存在)不提醒(2026-09 用户定稿);
4. **魔乐 SDK `delete_repo` 缺陷**:不发 JSON body → 400 EOF,已用裸 DELETE + `{}` 回退;
5. **魔乐可见性不可改**:无修改 API,不一致只能后台人工;
6. **魔乐平台 init README** 会自动生成,视为平台托管不删(防死循环);
7. **魔乐文件树接口偶发 500**(`"context canceled"`,平台内部 gitea 调用被取消)→ 对账按模型
   跳过该轮、下轮重试,不产生任务、不消耗失败次数;
8. **魔乐非 LFS 文件 API 不提供 sha256**:变更检测用 `blob_id`(git SHA1);内容 sha256 由强哈希
   下载重算为 `sha256_source='local'`(≤50MB;>50MB 不下载);
9. **文件级删除无宽限**(对齐 v1):魔塔文件树**瞬时部分缺失**会导致"删了又传"的抖动 commit,
   不会永久丢数据(魔塔为权威源, diff 每轮重算自愈);整树为空/拉取失败仍有 abort 保护。

## 12. 安全与删除保护

- 密钥只在 `.env`,token 永不进 DB;
- **严格镜像(2026-09 定稿)**:魔乐独有文件**默认自动删除**(`sync.auto_delete_extra=true`,
  含权重/大文件), 保证与魔塔文件集严格一致、避免"新版已传旧版未删"的下载窗口;
  关闭该开关则降级为 `delete_manual` 告警 + `clean --yes` 人工确认;
- **仍保留的底线保护**:魔塔文件集拉取失败/为空 → 整轮跳过(abort, 防误删风暴);
  隐藏/毒瘤文件永不删;README.md 永不因"独有"删除;**模型级** repo 删除保留 3 轮宽限;
- 历史教训:2026-09-02 曾发生魔乐独有内容被自动删除的事故(存量与魔塔布局不同)——
  改为严格镜像后,"魔乐独有"即视为应删内容, 请确保魔塔侧保有权威文件集;
- 对真实平台的破坏性测试只允许 `-test` 后缀仓库,测完精准删除(魔乐侧自动,魔塔侧需网页人工)。

## 13. 测试与开发

- 测试钩子(仅显式模型名触发):模型名以 `__fail__` 结尾模拟失败;含 `__sleep__N` 模拟 N 秒长任务(抢占测试);
- 单测/沙箱:对账可对真实平台**只读**+本地 DB 验证(入队不执行);worker 写操作只对 `-test` 仓执行;
- 设计/验收文档见 §14(工作区文档目录)。

## 14. 文档索引

| 文档 | 说明 |
|---|---|
| `V2-DESIGN.md` | 单向总体设计(规则矩阵/文件级语义/任务模型) |
| `V2-IMPLEMENTATION-GUIDE.md` | 实现指南(模块划分/验收标准/部署) |
| `V2-SQLITE-DESIGN.md` | SQLite 表结构与迁移说明 |
| `V2-MODELSCOPE-SDK-EXPLORE.md` / `V2-MODELERS-SDK-EXPLORE.md` | 两侧 API 实测事实(字段/行为/坑) |
| `docs-archive-bidirectional-2026-09/` | 早期双向方案(封存; 魔塔开放 API 删除后可恢复) |

> 上述设计文档位于**工作区文档目录**(与本仓库同级), 未随代码一起开源; 仓库内以本 README 为准。
