import os
import yaml
from dotenv import load_dotenv
from env_yaml import EnvLoader
from utils import model_tools,model_updown
import traceback
from modelscope.hub.api import HubApi
from openmind_hub import OmApi,RepoFile
import os
import time
import hashlib
from tqdm import tqdm

from concurrent.futures import ProcessPoolExecutor

def compare_file_sha256(model_path, file_list:list):
    
    # print(file_path+os.sep+file_list[0])
    file_path= model_path+os.sep+file_list[0]
    sha256=file_list[1]
    if os.path.isfile(file_path):
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            # 分块读取，每次读取 8KB（可根据需要调整）
            for byte_block in iter(lambda: f.read(8192), b""):
                sha256_hash.update(byte_block)
        if sha256_hash.hexdigest() == sha256: return [file_list[0],True]
        
    return [file_list[0],False]

if __name__=="__main__":
    max_workers=8
    
    org_name="Eco-Tech"
    model_list=["GLM-5.1-w8a8"]
    
    load_dotenv()
    
    with open('./config.yaml', 'r', encoding='utf-8') as f:
        config = yaml.load(f.read(), Loader=EnvLoader)
    scope_api = HubApi()
    scope_api.login(config["modelscope_cfg"]['token'])
    modelers_api= OmApi(token=config["modelers_cfg"]['token'])
    
    local_weight= config["global"]['weights_path']
    
    down_list=[]
    for model_name in model_list:
        repo= org_name+os.sep+model_name
        files_list=[]
        # 获取线上 model 中safetensors权重列表
        repo_tree= modelers_api.list_repo_tree(
            repo_id=repo,
            recursive=True
        )
        for file in repo_tree:
            if isinstance(file,RepoFile):                
                # 只记录safatensors的ha256
                if file.path.endswith(".safetensors"):
                    sha256= file.lfs.sha256
                    files_list.append([file.path,file.lfs.sha256])
                elif not file.path.startswith("."):
                    down_list.append(file.path)  
        # print(files_list)
        # print(len(files_list))
    
    
        # 与本地目录model中safatensors比较sha256值：
        local_path_list=[local_weight+os.sep+model_name] * len(files_list)
        with ProcessPoolExecutor(max_workers=64) as executor:
            results = list(tqdm(executor.map(compare_file_sha256,local_path_list, files_list),total=len(files_list)))
        
        # print(list(results))
        
        tmp=[name[0] for name in list(results) if name[1]==False]
        down_list.extend(tmp)
        print(down_list)
        
        modelers_api.snapshot_download(
            repo_id=repo,
            local_dir=local_weight+os.sep+model_name,
            allow_patterns=down_list,
            force_download=True,
            local_dir_use_symlinks=False,
            repo_type="model"
        )
        
        print("完成！")