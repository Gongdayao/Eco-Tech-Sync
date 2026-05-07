'''_summary_
静态同步脚本。
'''

import yaml
from dotenv import load_dotenv
from env_yaml import EnvLoader
from utils import model_tools, model_updown
from modelscope.hub.api import HubApi
from openmind_hub import OmApi

def main():
    load_dotenv()
    with open('./config.yaml', 'r', encoding='utf-8') as f:
        config = yaml.load(f.read(), Loader=EnvLoader)
    
    # 魔塔
    scope_api = HubApi()
    scope_api.login(config["modelscope_cfg"]['token'])

    # 魔乐
    modelers_api= OmApi(token=config["modelers_cfg"]['token'])
    
    works_list= model_tools.get_work_queue(modelers_api,scope_api,config)
    print(f"total number to sync: {len(works_list)}")
    print(works_list)
    MODELERS_NAME=config['constant']['MODELERS_NAME']
    MODELSCOPE_NAME=config['constant']['MODELSCOPE_NAME']
    error_list=[]
    succss_list=[]
    idx=1
    for work in works_list:
        print(f"{idx} / {len(works_list)}: 模型{work[0]}开始同步。")
        if work[1]==MODELERS_NAME:
            print(f"INFO：开始同步模型：{work[0]}, 目标站： {MODELERS_NAME} ")
            res= model_updown.scope2modelers_model(work[0],modelers_api,scope_api,config)
        elif work[1]==MODELSCOPE_NAME:
            print(f"INFO：开始同步模型：{work[0]}, 目标站： {MODELSCOPE_NAME} ")
            res= model_updown.modelers2scope_model(work[0],modelers_api,scope_api,config)

        if res==0:
            print(f"INFO：{work[0]}模型同步完成,已上传至 {{work[1]}}")
            succss_list.append(work[0])
        else:
            print(f"ERROR：{work[0]}模型同步失败")
            error_list.append(work[0])
        idx+=1
    
    print("成功的模型：")
    print(succss_list)
    print("失败的模型：")
    print(error_list)
    

if __name__=="__main__":
    main()