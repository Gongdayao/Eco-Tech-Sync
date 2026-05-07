from modelscope.hub.api import HubApi
from modelscope import snapshot_download
from modelscope.hub.file_download import model_file_download
from openmind_hub import OmApi
import shutil
import os
import traceback
from utils import model_tools
import logging

from openmind_hub.plugins.openmind.utils._error import GatedRepoError,RepositoryNotFoundError
from modelscope.hub.errors import  NotExistError

    

def modelers2scope_model(model_name:str, modelers_api:OmApi, scope_api:HubApi, config:dict):
    '''_summary_
    模型级别：从魔乐下载一个模型到本地，再从本地上传到魔塔
    Args:
        model_name (str): 模型名
        modelers_api (OmApi): 已登陆的魔乐API
        scope_api (HubApi): 已登陆的魔塔API
        config (dict): 配置信息

    Returns:
        int: 结果状态，成功或正常结束为0，有异常为1
    '''
    logger= logging.getLogger(config["global"]['logger_name'])
    
    ## 判断是否有tensors文件，没有tensors文件的模型不同步，直接正常返回
    res= model_level_saftensors_filter(model_name,config['constant']['MODELERS_NAME'],modelers_api,config)
    if res==-1: 
        logger.info(f"model_updown.modelers2scope_model(), 模型{model_name}没有tensors文件，此模型不同步。")
        return 0
    
    ## 有tensor文件，check repo、上传文件
    
    ## 魔乐下载：
    res= modelers_model_down(model_name,modelers_api,config)
    if res!=0: return -1
    
    ## 魔塔上传
    ## 读取模型可见性
    is_visibility=1
    repo_id= config["modelers_cfg"]['repo_name']+"/"+model_name
    try:
        is_private=modelers_api.model_info(repo_id=repo_id).private
        if is_private==True: is_visibility=1
        elif is_private==False: is_visibility=5
        else: logging.warning(f'模型同步，mdoel_tools modelers2scope_model，魔乐模型 {model_name} 向魔塔同步时，模型卡片可见性权限读取失败，请手动检查')
    except:
        print(f"model_tools, modelers2scope_model 从魔塔获取模型{model_name}信息异常。")
    ## check repo，没有则创建。
    res= scope_model_check_create(model_name,scope_api,config,is_visibility)
    if res==-1: return -1
    ## 上传模型
    res= scope_model_up(model_name,scope_api,config,is_visibility)
    if res!=0: return -1
    return 0

def scope2modelers_model(model_name:str, modelers_api:OmApi, scope_api:HubApi, config:dict):
    '''_summary_
    模型级别：从魔塔下载一个模型到本地，再从本地上传到魔乐
    Args:
        model_name (str): 模型名
        modelers_api (OmApi): 已登陆的魔乐API
        scope_api (HubApi): 已登陆的魔塔API
        config (dict): 配置信息

    Returns:
        int: 结果状态，成功为0，有异常为1
    '''
    logger= logging.getLogger(config["global"]['logger_name'])
    
    # ## 判断是否有tensors文件，没有话的，创建好repo即可返回
    # res= model_level_saftensors_filter(model_name, config['constant']['MODELSCOPE_NAME'], scope_api,config)
    # if res==-1:
    #     logger.info(f"model_updown.modelers2scope_model(), 模型{model_name}没有tensors文件，此模型不同步。")
    #     return 0
    
    ## 有tensor文件，check repo、上传文件
    ## 魔塔下载：
    res= scope_model_down(model_name,scope_api,config)
    if res!=0: return -1
    
    ## 魔乐上传
    ## 读取模型可见性
    is_private=True
    repo_id= config["modelscope_cfg"]['repo_name']+"/"+model_name
    try:
        is_visibility=scope_api.get_model(repo_id)['Visibility']
        if is_visibility==1: is_private=True
        elif is_visibility==5: is_private=False
        else: logging.warning(f'模型同步，mdoel_tools scope2modelers_model，魔塔模型 {model_name} 向魔乐同步时，模型卡片可见性权限读取失败，请手动检查')
    except:
        print(f"model_tools, scope2modelers_model 从魔塔获取模型{model_name}信息异常。")
        
    ## check repo，没有则创建。
    res= modelers_model_check_create(model_name,modelers_api,config,is_private)
    if res==-1: return -1
    ## 上传模型
    res= modelers_model_up(model_name,modelers_api,config,is_private=is_private)
    if res!=0: return -1
    return 0

def model_level_saftensors_filter(model_name:str, web_station_from:str,api,config):
    '''_summary_
    验证每个模型是否需要同步：如果一个模型，其下文件没有.safetensors文件，则过滤此模型，不进行同步
    Args:
        model_name (list): 需要验证的模型名list
        web_station_from (str): 站点标识， 以config里的标识为准
        api (_type_): 站点的api，分开处理
        config (_type_): 配置信息

    Returns:
        -1 or 0: -1表示没有safetensors文件，0 表示有有safetensors文件
    '''
    logger= logging.getLogger(config["global"]['logger_name'])
    if web_station_from == config['constant']['MODELERS_NAME']:
        try:
            res = api.model_info(
                repo_id=config["modelers_cfg"]['repo_name']+"/"+model_name
            )
        except:
            traceback.print_exc()
            logger.error(f"model_upload.model_level_saftensors_filter() 获取魔乐模型{model_name}失败")
            return -1
        if res is not None:
            safetensors_file_list= [file.rfilename for file in res.siblings if file.rfilename.endswith('.safetensors')]
            if len(safetensors_file_list)>0: return 0
        return -1
    elif web_station_from == config['constant']['MODELSCOPE_NAME']:
        res= model_tools.get_model_info_from_scope(model_name,api,config)
        if not res['ModelInfos']: return -1  ## "ModelInfos"字段为空，说明此模型下没有safetensors文件。
        else: return 0
    else: 
        logger.warning(f"model_upload.model_level_saftensors_filter, 传入的站点名{web_station_from}不正确，请检查：")
        return -1

def modelers_model_check_create(model_name:str, modelers_api:OmApi, config:dict,is_private:str=True):
    '''_summary_

    Args:
        model_name (str): 模型名
        modelers_api (OmApi): 魔乐API
        config (dict): 配置文件
        is_private (str, optional): 创建魔乐模型repo时，指定模型repo是否为私有。默认为True.

    Returns:
        int: 执行结果
    '''
    repo_id= config["modelers_cfg"]['repo_name']+"/"+model_name
    logger= logging.getLogger(config["global"]['logger_name'])
    # 查询仓库里是否有此模型，没有此模型需要提前创建
    try:
        repo_info= modelers_api.repo_info(
            repo_id=repo_id,
            repo_type="model",
            #timeout=None,
            token=config["modelers_cfg"]['token'],
            #revision=None,
        )
    except GatedRepoError:
        traceback.print_exc()
        logger.error("model_updown.modelers_model_check_create, repo_info() 魔乐token错误!")
        return -1
    except RepositoryNotFoundError:
        traceback.print_exc()
        # print("INFO: model_updown.modelers_model_check_create, repo_info() repo not found")
        logger.info(f"model_updown.modelers_model_check_create, repo_info() 模型{model_name}创建repo....")
        res= modelers_api.create_repo(repo_id=repo_id,private=is_private)
        if res.startswith('https://modelers.cn/'):
            logger.info(f"model_updown.modelers_model_check_create, create_repo() 创建repo{repo_id}成功")
            return res
        else: 
            logger.error(f"model_updown.modelers_model_check_create, create_repo() 创建repo{repo_id}失败，请检查。此模型上传已终止。")
            return -1 
    return 0
        

def modelers_model_up(model_name:str, modelers_api:OmApi, config:dict,is_private:str=True):
    '''_summary_
    上传模型到modelers组织内；
    注意魔了上传模型前需要先创建模型repo，不能直接上传，否则会报错
    Args:
        model_name (str): 模型名
        modelers_api (OmApi): 已经登陆的魔乐api
        config (dict): 配置信息变量
    Returns:
        res_code: 执行结果状态， 0表示成功执行， -1表示有错误
    '''    
    repo_id= config["modelers_cfg"]['repo_name']+"/"+model_name
    logger= logging.getLogger(config["global"]['logger_name'])
    res = modelers_model_check_create(model_name,modelers_api,config,is_private)
    if res==-1: return -1
        
    local_dir= config["global"]['weights_path']+os.sep+model_name    
    
    # 查询本地是否有此模型
    if not os.path.exists(local_dir) or  len(os.listdir(local_dir)) == 0:
        logger.error(f"model_updown.modelers_model_down, {local_dir} 目录没有模型。")
        return -1
    
    readme_path= model_tools.get_readme_in_directory(local_dir)
    if readme_path is not None:
        res,license_value= model_tools.rm_readme_yaml_front_matter(readme_path,config)
        # print(res, license_value)
        res= model_tools.add_readme_license(readme_path,license_value,config['constant']['MODELERS_NAME'] ,config)
        if res!=0: return -1
    
    try:
        modelers_api.upload_folder(
            folder_path=local_dir,
            repo_id=repo_id,
            token=config["modelers_cfg"]['token'],
            ignore_patterns=".*",
        )
    except:
        traceback.print_exc()
        logger.error("model_updown.modelers_model_down, modelers_api.upload_folder()魔乐上传模型异常")
        return -1
    # else:
    #     print(f"INFO: model_updown.modelers_model_down, modelers_api.upload_folder() 魔乐上传文件{model_name}成功。") 
    
    # 删除本地文件
    res= rm_local_model(local_dir,config)
    if res!=0:
        return -1
    else:
        return 0
        

def modelers_model_down(model_name:str, modelers_api:OmApi, config:dict):
    '''_summary_
    从modeler下载模型到本地
    Args:
        model_name (str): 魔乐社区 组织内的模型名
        modelers_api (OmApi): 已经登陆的魔乐api
        config (dict): 配置信息变量
    Returns:
        res_code: 执行结果状态， 0表示成功执行， -1表示有错误
    '''
    logger= logging.getLogger(config["global"]['logger_name'])
    local_dir= config["global"]['weights_path']+ os.sep+ model_name
    
    repo_id= config["modelers_cfg"]['repo_name']+"/"+model_name
    
    try:
        modelers_api.snapshot_download(
            repo_id=repo_id, 
            token=config["modelers_cfg"]['token'], 
            local_dir=local_dir,
            force_download=True,
            local_dir_use_symlinks=False,
            repo_type="model",
            max_workers=5
        )
    except:
        traceback.print_exc()
        logger.error(f"model_updown.modelers_model_down, modelers_api.snapshot_download()下载魔乐模型{model_name}异常")
        return -1
    # else:
    #     print(f"INFO: model_updown.modelers_model_down, modelers_api.snapshot_download() 下载魔乐模型{model_name}成功。")
        
    # 判断是否需要删除README.md
    # readme_path=local_dir+os.sep+"README.md"
    # if os.path.exists(readme_path):
    #     pass
        
    return 0


def scope_model_down(model_name:str, scope_api:HubApi, config:dict):
    '''_summary_
    从modelscope下载模型到本地
    Args:
        model_name (str): 魔塔modelscope社区 组织内的模型名
        scope_api (HubApi): 已经成功登陆的魔塔API
        config (dict): 配置信息变量
    Returns:
        res_code: 执行结果状态， 0表示成功执行， -1表示有错误
    '''  
    logger= logging.getLogger(config["global"]['logger_name'])
    
    local_dir= config["global"]['weights_path']+ os.sep+ model_name
    repo_id= config["modelscope_cfg"]['repo_name']+"/"+model_name
    
    try:
        snapshot_download(
            repo_id=repo_id,
            local_dir=local_dir,
            repo_type="model"
        )
    except:
        traceback.print_exc()
        logger.error(f"model_updown.modelers_model_down, snapshot_download()下载魔塔文件{model_name}异常")
        return -1
    # else:
    #     print(f"INFO: model_updown.modelers_model_down, snapshot_download() 下载魔塔文件{model_name}成功。") 
        
    # 判断是否需要删除README.md
    readme_path=local_dir+os.sep+"README.md"
    if os.path.exists(readme_path):
        res= model_tools.get_model_info_from_scope(model_name,scope_api,config)
        if str(res['ReadMeContent']).startswith("### 当前模型的贡献者未提供更加详细的模型介绍。模型文件和权重，可浏览“模型文件”页面获取。") or str(res['ReadMeContent']).endswith("及时完善模型卡片内容。</p>") or \
        str(res['ReadMeContent']).startswith("### You are viewing the default Readme template as no detailed") or str(res['ReadMeContent']).endswith("the model contribution documentation</a>.</p>"): # 过滤中英文版modelscope初始的readme.md
            # logger.info(f"model_updown.modelers_model_down, 删除模型{model_name}中的README.md文件")
            try:
                os.remove(readme_path)
            except:
                traceback.print_exc()
                logger.error(f"model_updown.modelers_model_down 删除模型{model_name}中的README.md异常。")
                return -1
            # else:
            #     logger.info("model_updown.modelers_model_down 删除README.md成功。")
    return 0
def scope_model_check_create(model_name:str, scope_api:HubApi, config:dict,is_visibility:int=1):
    '''_summary_
    上传模型前调用，检查scope仓内是否有此模型，没有则创建模型repo，并同时设定好模型的可见性visibility
    Args:
        model_name (str): _description_
        scope_api (HubApi): _description_
        config (dict): _description_
        is_visibility (int, optional): _description_. Defaults to 1.

    Returns:
        _type_: _description_
    '''
    logger= logging.getLogger(config["global"]['logger_name'])
    repo_id= config["modelscope_cfg"]['repo_name']+"/"+model_name
    try:
        repo_info= scope_api.get_model(model_id=repo_id)
    except Exception as e:
        if str(e).endswith("does not exist!"):
            logger.info(f"model_updown.scope_model_check_create(), 模型{model_name}在modelscope上未创建模型名片，现在开始创建：")
            res= scope_api.create_model(model_id=repo_id,visibility=is_visibility,)
            if res.startswith('https://www.modelscope.cn'):
                logger.info(f'model_updown.scope_modelscope_model_check_create_create(), 模型{model_name}在modelscope上创建成功，URL：{res}')
                return res
            else:
                logger.error(f"model_updown.scope_model_check_create(), 模型{model_name}在modelscope上创建失败，请检查。此模型上传已终止。")
                return -1
        else: 
            logger.error(f"model_updown.scope_model_check_create(), 模型{model_name}在modelscope上创建失败，请检查。此模型上传已终止。")
            return -1
        
    return 0
    

def scope_model_up(model_name:str, scope_api:HubApi, config:dict,is_visibility:int=1):
    '''_summary_
    从本地上传模型到modelscope
    Args:
        model_name (str): 魔塔modelscope社区 组织内的模型名
        scope_api (HubApi): 已经成功登陆的魔塔API
        config (dict): 配置信息变量
    Returns:
        res_code: 执行结果状态， 0表示成功执行， -1表示有错误
    '''
    logger= logging.getLogger(config["global"]['logger_name'])
    local_dir= config["global"]['weights_path']+os.sep+model_name
    # # 判断目录是否存在，如果不存在或为空，则报错。
    if not os.path.exists(local_dir) or  len(os.listdir(local_dir)) == 0:
        logger.error(f"model_updown.scope_model_up, {local_dir} 目录不存在或为空。")
        return -1
    
    res= scope_model_check_create(model_name,scope_api,config,is_visibility)
    if res==-1: return -1

    repo_id= config["modelscope_cfg"]['repo_name']+"/"+model_name
    
    
    try:
        scope_api.upload_folder(
            repo_id=repo_id,
            folder_path=local_dir,
            ignore_patterns=".*"
        )
    except:
        traceback.print_exc()
        logger.error(f"model_updown.scope_model_up, scope_api.upload_folder()魔塔上传模型{model_name}异常")
        return -1
    
    # else:
        # logger.info(f"model_updown.scope_model_up, scope_api.upload_folder()魔塔上传文件{model_name}成功")
        
    # 删除本地文件
    res= rm_local_model(local_dir,config)
    if res!=0:
        return -1
    else:
        return 0
    
    
# ————————————————————————————————————————————————————————————————————————————

# 单文件下载、上传、仓中删文件处理：

def scope2modelers_file(model_name:str, file_name: str, modelers_api:OmApi, scope_api:HubApi, config:dict):
    '''_summary_
    集成函数，从魔塔下载单文件，并上传到魔乐
    Args:
        model_name (str): _description_
        file_name (str): _description_
        modelers_api (OmApi): _description_
        scope_api (HubApi): _description_
        config (dict): _description_

    Returns:
        _type_: _description_
    '''
    res= scope_file_down(model_name, file_name, scope_api, config)
    if res!=0: return -1
    
    if file_name.startswith("."): return 0
    
    # 对readme做单独处理：
    if file_name=='README.md':
        readme_path=config["global"]['weights_path']+ os.sep+ model_name+ os.sep+ file_name
        res, license_value= model_tools.rm_readme_yaml_front_matter(readme_path,config)
        if res==0:
            add_res= model_tools.add_readme_license(readme_path,license_value,config['constant']['MODELERS_NAME'],config)
            if add_res==-1:
                return -1
        else:
            return -1
    
    res= modelers_file_up(model_name, file_name, modelers_api, config)
    if res!=0: return -1 
    
    return 0


def delete_modelers_file(model_name:str, file_name: str, modelers_api:OmApi, config:dict):
    '''_summary_
    删除魔乐中指定模型目录下的一个文件
    Args:
        model_name (str): 模型名
        file_name (str): 文件名
        modelers_api (OmApi): 魔乐API
        config (dict): 配置信息

    Returns:
        code: 结果码
    '''    
    logger= logging.getLogger(config["global"]['logger_name'])
    
    repo_id= config["modelers_cfg"]['repo_name']+"/"+model_name
    
    try:
        modelers_api.delete_file(
            path_in_repo=file_name,
            repo_id=repo_id
        )
    except:
        traceback.print_exc()
        logger.error(f"model_updown.delete_modelers_file, modelers_api.delete_file()删除{repo_id}中的单文件{file_name}异常")
        return -1
    
    return 0

def modelers_file_down(model_name:str, file_name: str, modelers_api:OmApi, config:dict):
    '''_summary_
    从魔乐下载单文件到本地
    Args:
        model_name (str): 魔乐社区 组织内的模型名
        file_name (str):  单文件的名字
        modelers_api (OmApi):  已经登陆的魔乐api
        config (dict): 配置信息变量

    Returns:
        res_code: 执行结果状态， 0表示成功执行， -1表示有错误
    '''   
    if file_name.startswith("."): return 0
    
    logger= logging.getLogger(config["global"]['logger_name'])
     
    local_dir= config["global"]['weights_path']+ os.sep+ model_name
    repo_id= config["modelers_cfg"]['repo_name']+"/"+model_name
    
    try:
        modelers_api.om_hub_download(
            repo_id=repo_id,
            repo_type=None,
            filename=file_name,
            local_dir=local_dir
        )
    except:
        traceback.print_exc()
        logger.error(f"model_updown.modelers_file_down, modelers_api.om_hub_download()下载魔乐文件 {model_name}/{file_name} 异常")
        return -1
    
    return 0

def modelers_file_up(model_name:str, file_name: str, modelers_api:OmApi, config:dict):
    '''_summary_
    从本地上传单文件到魔乐的魔个模型目录下，上传完成后删除本地模型目录
    Args:
        model_name (str): 模型名
        file_name (str): 文件名
        modelers_api (OmApi): 魔乐API
        config (dict): 配置信息

    Returns:
        code: 结果状态码
    '''    
    if file_name.startswith("."): return 0
    
    logger= logging.getLogger(config["global"]['logger_name'])
    
    local_dir= config["global"]['weights_path']+ os.sep+ model_name
    local_file_path= local_dir+ os.sep+file_name
    repo_id= config["modelers_cfg"]['repo_name']+"/"+model_name
    
    #判断本地此路径下是否有此文件
    if not os.path.exists(local_file_path):
        logger.error(f"model_updown.modelers_file_up, 文件{local_file_path}不存在，魔乐单文件上传失败")
        return -1 
    try:
        modelers_api.upload_file(
            token=config["modelers_cfg"]['token'],
            path_or_fileobj=local_file_path,
            repo_id=repo_id,
            path_in_repo=file_name
        )
    except:
        traceback.print_exc()
        logger.error(f"model_updown.modelers_file_up, modelers_api.upload_file()上传魔乐文件 {model_name} / {file_name} 异常")
        return -1
    
    #上传完之后连带模型根目录一起删除
    # 删除本地文件
    res= rm_local_model(local_dir,config)
    if res!=0:
        return -1
    else:
        return 0
    
def scope_file_down(model_name:str, file_name: str, scope_api:HubApi, config:dict):
    '''_summary_
    从魔塔下载指定模型中的指定文件
    Args:
        model_name (str): 模型名
        file_name (str): 文件名
        scope_api (HubApi): 魔塔API
        config (dict): 配置信息

    Returns:
        code: 结果状态， 0为下载成功，-1为下载异常
    '''   
    if file_name.startswith("."): return 0
    
    logger= logging.getLogger(config["global"]['logger_name'])
     
    local_dir= config["global"]['weights_path']+ os.sep+ model_name
    repo_id= config["modelscope_cfg"]['repo_name']+"/"+model_name
    try:
        model_file_download(
            model_id=repo_id,
            file_path=file_name,
            local_dir=local_dir
        )
    except:
        traceback.print_exc()
        logger.error(f"model_updown.scope_file_down, model_file_download()下载魔塔文件 {model_name} / {file_name}异常")
        return -1
    
    return 0
    
    

# ————————————————————————————————————————————————————————————————————————————
# 删除本地模型目录
def rm_local_model(local_dir:str,config:dict):
    '''_summary_
    删除本地模型权重目录。
    Args:
        local_dir (str): 模型本地路经

    Returns:
        _type_: 0：成功， -1：异常
    '''    
    logger= logging.getLogger(config["global"]['logger_name'])
    
    # 先判断是否有此目录
    if not os.path.exists(local_dir):
        # logger.info(f"model_updown.rm_local_model(), {local_dir} 目录不存。")
        return 0
    
    # 有则删除
    try:
        shutil.rmtree(local_dir)
    except:
        traceback.print_exc()
        logger.error(f"model_updown.model_modelers2scope, shutil.rmtree() 删除{local_dir} 目录异常。")
        return -1
    # else:
    #     print(f"INFO: model_updown.model_modelers2scope, shutil.rmtree() 删除{local_dir} 目录成功。")
    
    return 0

