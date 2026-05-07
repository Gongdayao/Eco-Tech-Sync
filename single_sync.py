import os
import yaml
from dotenv import load_dotenv
from env_yaml import EnvLoader
from utils import model_tools,model_updown
import traceback
from modelscope.hub.api import HubApi
from openmind_hub import OmApi,RepoFile
import time
import hashlib



if __name__=="__main__":
    
    model_list=["Kimi-K2.6-w4a8"]

    load_dotenv()
    with open('./config.yaml', 'r', encoding='utf-8') as f:
        config = yaml.load(f.read(), Loader=EnvLoader)

    scope_api = HubApi()
    scope_api.login(config["modelscope_cfg"]['token'])
    modelers_api= OmApi(token=config["modelers_cfg"]['token'])

    # park_repo='Modelers_Park/DeepSeek-V3.1-Terminus-w8a8-QuaRot'
    # model_name='DeepSeek-V3.1-Terminus-w8a8-mtp-QuaRot'
    
    for model in model_list:
        # res = model_updown.scope2modelers_model(model,modelers_api,scope_api,config)
        res= model_updown.modelers2scope_model(model,modelers_api=modelers_api,scope_api=scope_api,config=config)
        # res=model_updown.modelers_model_up(model_name=model,modelers_api=modelers_api,config=config)
        
        if res==0: print("完成")
        else: print("失败")
