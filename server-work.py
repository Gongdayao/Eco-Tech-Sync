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

sync_que= deque()
sync_lock= threading.Lock()

def update_sync_deque(modelers_api: OmApi, scope_api:HubApi, config:dict,logger: Logger):
    works_list= model_tools.get_work_queue(modelers_api,scope_api,config)
    global sync_que
    with sync_lock:
        for work in works_list:
            if work not in sync_que:
                logger.info(f"加入新任务：{work}")
                sync_que.append(work)
    
if __name__=="__main__":
    load_dotenv()
    with open('./config.yaml', 'r', encoding='utf-8') as f:
        config = yaml.load(f.read(), Loader=EnvLoader)
        
    with open('logging_config.yaml', 'r', encoding='utf-8') as f:
        logging_config = yaml.safe_load(f)
        logging.config.dictConfig(logging_config)
        
    logger= logging.getLogger(config["global"]['logger_name'])
        
    # 两个模型网站api登陆
    scope_api = HubApi()
    scope_api.login(config["modelscope_cfg"]['token'])
    modelers_api= OmApi(token=config["modelers_cfg"]['token'])
    
    sched= BackgroundScheduler()
    sched.add_job(update_sync_deque,'interval', hours=12, args=(modelers_api, scope_api, config, logger,))
    sched.start()
    
    MODELERS_NAME=config['constant']['MODELERS_NAME']
    MODELSCOPE_NAME=config['constant']['MODELSCOPE_NAME']
    GITCODE_NAME=config['constant']['GITCODE_NAME']
    
    update_sync_deque(modelers_api, scope_api, config, logger)
    
    FILE_OPTION_UPDATE= config['constant']['FILE_OPTION_UPDATE']
    FILE_OPTION_DELETE= config['constant']['FILE_OPTION_DELETE']
  
    while True:
        if len(sync_que)>0:
            work=sync_que[0]
            logger.info(f"总任务：{len(sync_que)}，当前work：{work}")
            if len(work)>=4:  # 文件级:
                if work[3]==FILE_OPTION_UPDATE:
                    res= model_updown.scope2modelers_file(work[0],work[2],modelers_api,scope_api,config)
                    
                elif work[3]==FILE_OPTION_DELETE:
                    res= model_updown.delete_modelers_file(work[0],work[2],modelers_api,config)                    
                    
            else:  # 模型级     
                if work[1]==MODELSCOPE_NAME:
                    # print(f"INFO：开始同步模型：{work[0]}, 下载到{MODELSCOPE_NAME}")
                    res= model_updown.modelers2scope_model(work[0],modelers_api,scope_api,config)
                elif work[1]==MODELERS_NAME:
                    # print(f"INFO：开始同步模型：{work[0]}, 下载到{MODELERS_NAME}")
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
            time.sleep(60)
                        
        else:
            logger.info(f"当前队列无任务，sleep {30} 分钟后再检查。")
            time.sleep(30 * 60)
    
'''
work结构：
    模型级别：[model_name, web, args]
    问价级别：[model_name, web, file_name, option]
    其中：web表示需要操作的站点，如果是包含下载+上传的同步操作，则web指向需要上传的站点（目标站点）；
        file_name表示需要同步的文件名，对于模型级别的同步，没有file_name和option
        option表示同步操作，当前需求主要分为两种操作：1.  从魔塔下载文件后上传至魔乐； 2. 从魔乐中直接删除某个文件
sync_que结构：[work1,work2, work3.....]
'''    
