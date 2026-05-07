export HUB_WHITE_LIST_PATHS="/data/disk3/gdy/weights/" &&
export XDG_CACHE_HOME="/data/disk3/gdy/weights/.openmind/" &&
export DEFAULT_REQUEST_TIMEOUT=600 &&
rm -f log/server-std.log &&
nohup python server-work.py > log/server-std.log 2>&1 &
