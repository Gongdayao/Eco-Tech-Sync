export HUB_WHITE_LIST_PATHS="/data/disk3/gdy/weights/" &&
export XDG_CACHE_HOME="/data/disk3/gdy/weights/.openmind/" &&
export DEFAULT_REQUEST_TIMEOUT=600 &&
rm -f log/resume_sync.log &&
nohup python resume_sync.py > log/resume_sync.log 2>&1 &