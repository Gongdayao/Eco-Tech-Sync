#!/bin/bash
set -euo pipefail

PIDFILE="log/daemon.pid"

if [ "${1:-}" = "stop" ]; then
  if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
      echo "正在停止守护进程 (PID: $PID)..."
      kill -TERM "$PID"
      for i in $(seq 1 30); do
        if ! kill -0 "$PID" 2>/dev/null; then
          echo "守护进程已停止"
          rm -f "$PIDFILE"
          exit 0
        fi
        sleep 1
      done
      echo "超时，强制终止..."
      kill -KILL "$PID" 2>/dev/null || true
      rm -f "$PIDFILE"
    else
      echo "守护进程未运行 (PID 文件残留)"
      rm -f "$PIDFILE"
    fi
  else
    echo "未找到 PID 文件，守护进程可能未运行"
  fi
  exit 0
fi

if [ -f .env ]; then
  set -a && source .env && set +a
fi

: "${WEIGHTS_PATH:?错误: 请在 .env 中设置 WEIGHTS_PATH}"

export HUB_WHITE_LIST_PATHS="${WEIGHTS_PATH%/}/"
export XDG_CACHE_HOME="${WEIGHTS_PATH%/}/.openmind/"
export DEFAULT_REQUEST_TIMEOUT=600

nohup python server-work.py > log/server-std.log 2>&1 &
echo $! > "$PIDFILE"
echo "守护进程已启动 (PID: $(cat $PIDFILE))"
