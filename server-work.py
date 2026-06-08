import time
import yaml
from dotenv import load_dotenv
from env_yaml import EnvLoader
import threading
from collections import deque
from modelscope.hub.api import HubApi
from openmind_hub import OmApi
from utils import model_tools,model_updown, gitcode_conn

import logging
import logging.config
from logging import Logger

from apscheduler.schedulers.background import BackgroundScheduler

import signal
import sys
import json
import os

sync_que= deque()
sync_lock= threading.Lock()
running = True

QUEUE_STATE_FILE = ".sync_queue_state.json"

def save_queue_state(logger: Logger):
    with sync_lock:
        works = [list(w) for w in sync_que]
    state = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "queue": works}
    with open(QUEUE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    logger.info(f"队列状态已保存至 {QUEUE_STATE_FILE}，剩余 {len(works)} 个任务")

def load_saved_queue(logger: Logger):
    if os.path.exists(QUEUE_STATE_FILE):
        try:
            with open(QUEUE_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            works = [deque(w, maxlen=len(w)) if isinstance(w, list) else w
                     for w in state["queue"]]
            logger.info(f"从 {QUEUE_STATE_FILE} 恢复 {len(works)} 个任务 "
                        f"(保存时间: {state['timestamp']})")
            os.remove(QUEUE_STATE_FILE)
            return deque(works)
        except (json.JSONDecodeError, KeyError, OSError):
            logger.warning(f"队列状态文件损坏，忽略")
    return deque()

def shutdown_handler(signum, frame, logger: Logger, sched: BackgroundScheduler):
    global running
    if running:
        logger.info(f"收到 {signal.Signals(signum).name} 信号，正在优雅退出...")
        running = False
    else:
        logger.warning("强制退出")
        sched.shutdown(wait=False)
        sys.exit(1)

def update_sync_deque(modelers_api: OmApi, scope_api:HubApi, config:dict,logger: Logger):
    works_list= model_tools.get_work_queue(modelers_api,scope_api,config)
    global sync_que
    with sync_lock:
        for work in works_list:
            if work not in sync_que:
                logger.info(f"加入新任务：{work}")
                sync_que.append(work)
    
def interruptible_sleep(seconds: float, interval: float = 1.0):
    for _ in range(int(seconds / interval)):
        if not running:
            return
        time.sleep(interval)
    remainder = seconds % interval
    if remainder > 0 and running:
        time.sleep(remainder)

if __name__=="__main__":
    load_dotenv()
    with open('./config.yaml', 'r', encoding='utf-8') as f:
        config = yaml.load(f.read(), Loader=EnvLoader)
        
    with open('logging_config.yaml', 'r', encoding='utf-8') as f:
        logging_config = yaml.safe_load(f)
        logging.config.dictConfig(logging_config)
        
    logger= logging.getLogger(config["global"]['logger_name'])
        
    scope_api = HubApi()
    scope_api.login(config["modelscope_cfg"]['token'])
    modelers_api= OmApi(token=config["modelers_cfg"]['token'])
    
    sched= BackgroundScheduler()
    sched.add_job(update_sync_deque,'interval', hours=12, args=(modelers_api, scope_api, config, logger,))
    sched.start()

    saved = load_saved_queue(logger)
    if saved:
        sync_que = saved
    
    signal.signal(signal.SIGTERM, lambda sig, frame: shutdown_handler(sig, frame, logger, sched))
    signal.signal(signal.SIGINT,  lambda sig, frame: shutdown_handler(sig, frame, logger, sched))
    
    MODELERS_NAME=config['constant']['MODELERS_NAME']
    MODELSCOPE_NAME=config['constant']['MODELSCOPE_NAME']
    GITCODE_NAME=config['constant']['GITCODE_NAME']
    
    update_sync_deque(modelers_api, scope_api, config, logger)
    
    FILE_OPTION_UPDATE= config['constant']['FILE_OPTION_UPDATE']
    FILE_OPTION_DELETE= config['constant']['FILE_OPTION_DELETE']
  
    while running:
        if len(sync_que)>0:
            work=sync_que[0]
            logger.info(f"总任务：{len(sync_que)}，当前work：{work}")
            if len(work)>=4:
                if work[3]==FILE_OPTION_UPDATE:
                    res= model_updown.scope2modelers_file(work[0],work[2],modelers_api,scope_api,config)
                    
                elif work[3]==FILE_OPTION_DELETE:
                    res= model_updown.delete_modelers_file(work[0],work[2],modelers_api,config)                    
                    
            else:
                if work[1]==MODELSCOPE_NAME:
                    res= model_updown.modelers2scope_model(work[0],modelers_api,scope_api,config)
                elif work[1]==MODELERS_NAME:
                    res= model_updown.scope2modelers_model(work[0],modelers_api,scope_api,config)
                elif work[1]==GITCODE_NAME:
                    res= gitcode_conn.create_repo(work[0],config)

            if res==0:
                logger.info(f"{work} 完成")
                with sync_lock:
                    sync_que.popleft()
            else:
                logger.error(f"{work} 失败，压回队列")
                with sync_lock:
                    work= sync_que.popleft()
                    sync_que.append(work)
            
            logger.info("当前任务结束，sleep 60 秒后开始下一个任务")
            interruptible_sleep(60)
                        
        else:
            logger.info(f"当前队列无任务，sleep {30} 分钟后再检查。")
            interruptible_sleep(30 * 60)

    logger.info("正在保存队列状态...")
    save_queue_state(logger)
    sched.shutdown(wait=False)
    logger.info("守护进程已退出")
