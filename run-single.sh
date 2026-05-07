export HUB_WHITE_LIST_PATHS="/data/disk3/gdy/weights/" &&
export XDG_CACHE_HOME="/data/disk3/gdy/weights/.openmind/" &&
export DEFAULT_REQUEST_TIMEOUT=600 &&
rm -f log/single_sync.log &&
nohup python single_sync.py > log/single_sync.log 2>&1 &
