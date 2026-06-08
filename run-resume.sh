#!/bin/bash
set -euo pipefail

if [ -f .env ]; then
  set -a && source .env && set +a
fi

: "${WEIGHTS_PATH:?错误: 请在 .env 中设置 WEIGHTS_PATH}"

export HUB_WHITE_LIST_PATHS="${WEIGHTS_PATH%/}/"
export XDG_CACHE_HOME="${WEIGHTS_PATH%/}/.openmind/"
export DEFAULT_REQUEST_TIMEOUT=600

rm -f log/resume_sync.log
nohup python resume_sync.py > log/resume_sync.log 2>&1 &
