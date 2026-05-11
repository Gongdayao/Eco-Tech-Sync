**English** | [中文](README.md)

# Eco-Tech Model Synchronizer

---

魔乐 (Modelers) / 魔塔 (ModelScope) / GitCode 模型自动同步器

A background service that automatically syncs AI model weights across three Chinese model hosting platforms — **Modelers (魔乐社区)**, **ModelScope (魔搭)**, and **GitCode**.

## Features

- **Bidirectional model-level sync** — detects models that exist on one platform but not another, and mirrors them automatically
- **File-level delta sync** — for models present on both platforms, compares individual files by SHA256 and only uploads what changed
- **GitCode mirror creation** — automatically creates GitCode repos by importing from ModelScope
- **Daemon mode** — runs continuously as a background service with a work queue, refreshing periodically
- **Graceful shutdown** — responds to SIGTERM, saves pending queue before exit, auto-resumes on restart
- **Resume support** — parallel SHA256 comparison and selective re-download for broken/partial syncs
- **README/license handling** — strips YAML front matter from READMEs, auto-adds license information on Modelers

## Architecture

```
Modelers (魔乐)  <── bidirectional model sync ──>  ModelScope (魔搭)
                                                      │
                                                      │ (model discovery only)
                                                      ▼
                                                  GitCode
```

- **Model-level sync** is bidirectional between Modelers and ModelScope
- **File-level sync** is one-way: ModelScope → Modelers
- GitCode repos are created via ModelScope `.git` import URLs, with a prompt to enable pull mirror mode

### Work Queue Model

The daemon maintains an in-memory work queue (deque) refreshed every 12 hours:

| Work Item Type | Format | Description |
|---|---|---|
| Model sync | `[model_name, target_platform]` | Sync entire model to target platform |
| File update | `[model_name, platform, file_name, "update"]` | Upload changed file |
| File delete | `[model_name, platform, file_name, "delete"]` | Remove stale file from Modelers |

## Prerequisites

- Python 3.8+
- Linux server with sufficient disk space for model weights (typically hundreds of GB)
- API tokens for **ModelScope**, **Modelers (openMind)**, and **GitCode**

## Installation

```bash
# Clone the repository
git clone <repo-url> 
cd Eco-Tech-Sync

# Install dependencies
pip install -r requirements.txt
```

## Configuration

### 1. Create `.env` (from `.env` template)

```env
WEIGHTS_PATH="your_weight_work_path"
MODELERS_TOKEN="your_modelers_token"
MODELERS_REPO_NAME="YourOrgName"
SCOPE_TOKEN="your_modelscope_token"
SCOPE_REPO_NAME="YourOrgName"
GITCODE_TOKEN="your_gitcode_token"
GITCODE_REPO_NAME="YourOrgName"
```

### 2. Verify `config.yaml`

The config uses `${ENV_VAR}` placeholders resolved from `.env`. Key sections:

| Section | Description |
|---|---|
| `global` | Local weights storage path, logger name |
| `modelscope_cfg` | ModelScope API credentials and supported licenses |
| `modelers_cfg` | Modelers API credentials and supported licenses |
| `gitcode_cfg` | GitCode API credentials and ModelScope base URL for imports |

### 3. Configure `logging_config.yaml`

Default: daily rotating logs in `log/` with 90-day retention.

## Usage

### Start the daemon (recommended for production)

```bash
bash run.sh
```

This launches `server-work.py` as a background process via `nohup`, with PID recorded in `log/daemon.pid`. The daemon:
1. Restores incomplete tasks from the last run (if `.sync_queue_state.json` exists)
2. Generates a work queue by comparing all three platforms
3. Processes sync tasks one at a time (60s pause between tasks)
4. Refreshes the work queue every 12 hours
5. Sleeps 30 minutes when queue is empty before checking again

### Stop the daemon

```bash
bash run.sh stop
# or
bash run-stop.sh
```

Sends SIGTERM signal; the daemon finishes the current task, saves the queue state, then exits. Force-killed after 30s timeout.

### One-shot batch sync

```bash
python static_work.py
```

Computes the full work queue once, processes all items, then reports success/failure.

### Single model sync

```bash
bash run-single.sh
```

Syncs a single specified model. Edit `single_sync.py` to change the target model name.

### Resume / repair sync

```bash
bash run-resume.sh
```

Compares local SHA256 hashes with remote, downloads only changed `.safetensors` files. Uses parallel processing (64 workers) for fast comparison.

## Project Structure

```
model_syn/
├── server-work.py          # Main daemon (continuous background sync)
├── static_work.py          # One-shot batch sync
├── single_sync.py          # Single model sync
├── resume_sync.py          # Resume/repair broken downloads
├── park_sync.py            # Sync from Modelers_Park namespace
│
├── utils/                  # Core library
│   ├── model_tools.py      # Work queue generation, file diff, README/license utils
│   ├── model_updown.py     # All download/upload/sync operations
│   └── gitcode_conn.py     # GitCode API client
│
├── config.yaml             # Main configuration
├── logging_config.yaml     # Logging configuration
├── requirements.txt        # Python dependencies
├── .env                    # Secrets & environment variables (gitignored)
│
├── run.sh                  # Launch/stop daemon
├── run-single.sh           # Launch single sync
├── run-resume.sh           # Launch resume sync
├── run-stop.sh             # Stop daemon
│
└── log/                    # Logs (gitignored)
    ├── info.log            # Application log (daily rotation)
    ├── server-std.log      # Daemon stdout
    └── daemon.pid          # Daemon PID file
```

## Notes

- Only models containing `.safetensors` weight files are synced; non-weight repos are skipped
- Default LICENSE files automatically generated by ModelScope are detected and filtered out during comparison
- The `.env` file containing tokens is excluded from git via `.gitignore`
- Logs and test scripts are excluded from version control
- Shell scripts read `WEIGHTS_PATH` from `.env` to set `HUB_WHITE_LIST_PATHS` and cache directories
- When stopping the daemon, incomplete tasks are saved to `.sync_queue_state.json` and auto-resumed on restart
