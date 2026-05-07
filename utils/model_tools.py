import hashlib
import os 
import re
import yaml
import time
from modelscope.hub.api import HubApi
from openmind_hub import OmApi, RepoFile
from utils import model_updown
from utils import gitcode_conn
import traceback
from datetime import datetime as dt 

import logging

def get_model_info_from_scope(model_name:str, scope_api:HubApi,config:dict):
    '''_summary_
    查询一个魔塔的模型信息，返回需要用到的模型信息
    Args:
        model_name (str): _description_
        scope_api (HubApi): _description_
        config (dict): _description_

    Returns:
        str: 模型的返回结果
    '''    
    logger= logging.getLogger(config["global"]['logger_name'])
    
    repo_id= config["modelers_cfg"]['repo_name']+"/"+model_name
    
    try:
        res=scope_api.get_model(repo_id)
    except:
        traceback.print_exc()
        logger.error(f"model_tools, get_model_info_from_scope 从魔塔获取模型{model_name}信息异常。")
        return -1
    
    return res

def get_model_list_from_modelers(modelers_api:OmApi,config:dict):
    '''_summary_
    从modelers中获取组织内所有模型列表
    Args:
        modelers_api (OmApi): 已经登陆的魔乐API
        config (dict): 配置信息
    '''
    author = config['modelers_cfg']['repo_name']
    logger= logging.getLogger(config["global"]['logger_name'])
    
    try:
        res = modelers_api.list_models(
            author= author
        )
    except:
        traceback.print_exc()
        logger.error("model_tools get_model_list_from_modelers() 获取模型魔乐列表失败")
        return -1
    
    modelers__model_name_list=[]
    for m in res:
        modelers__model_name_list.append(m.name)
        
    return modelers__model_name_list
  
  
def get_model_list_from_scope(scope_api:HubApi,config:dict):
    '''_summary_
    从modelsope中获取组织内所有模型列表,及所有公开的模型列表
    Args:
        scope_api (HubApi): 已经登陆的魔塔API
        config (dict): 配置信息
    '''
    
    logger= logging.getLogger(config["global"]['logger_name'])
    
    group= config['modelscope_cfg']['repo_name']
      
    try:
        res = scope_api.list_models(
            owner_or_group=group,
            page_size=100
        )
    except:
        traceback.print_exc()
        logger.error("model_tools get_model_list_from_scope() 获取魔塔模型列表失败")
        return -1
    
    scope_mlist=res['Models']
    
    
    if int(res['TotalCount'])>100:
        for i in range(int(res['TotalCount'])//100):
            res = scope_api.list_models(
                owner_or_group=group,
                page_number=2+i,
                page_size=100
            )
            scope_mlist.extend(res['Models'])
    
    # 获取所有模型列表，用于与modelers的列表比较    
    scope_model_name_list=[]
    for m in scope_mlist:
        scope_model_name_list.append(m['Name'])
    
    # 获取所有public权限的列表，用于与gitcode列表比较。
    scope_model_visibility_list=[]
    for m in scope_mlist:
        if m['Visibility']==5:
            scope_model_visibility_list.append(m["Name"])
    
    return scope_model_name_list, scope_model_visibility_list

def get_file_level_worklist(models_list:list, modelers_api:OmApi, scope_api:HubApi, config:dict):
    '''_summary_
    获取文件级别需要更新的任务队列,
    如果一个新创建的文件夹，没有safetensors文件，则不进行同步。
    Args:
        models_list (list): _description_
        modelers_api (OmApi): _description_
        scope_api (HubApi): _description_
        config (dict): _description_

    Returns:
        list: _description_
    '''    
    file_work_list=[]
    for model in models_list:
        time.sleep(1) ## 增加延迟，否则请求太快会报错
        file_list=get_file_worklist_per_model(model,modelers_api,scope_api,config)
        if file_list==-1: 
            continue
        file_work_list.extend(file_list)
        
    return file_work_list
                
                     
def get_work_queue(modelers_api:OmApi, scope_api:HubApi, config:dict):
    '''_summary_
    获取两个网站之间需要同步的内容，分别为文件级别和模型级别：
    将每个模型键值对放入list中，其中info为标记下载网站，表示其从下载到上传的方向
    Args:
        modelers_api (OmApi): 已登陆的魔乐API
        scope_api (HubApi): 已登陆的魔塔API
        config (dict): 配置信息
        
    Returns:
        list: 需要同步的所有模型，work[0]=模型名， work[1]=目标站， work[2]=file_name, 有file_name表示文件级别
    '''    
    scope_res, scope_visibility_res= get_model_list_from_scope(scope_api, config)
    modelers_res= get_model_list_from_modelers(modelers_api,config)
    
    works_list=[]
    
    # 文件级别同步列表
    same_models_list= list(set(scope_res).intersection(modelers_res))
    res_list=get_file_level_worklist(same_models_list, modelers_api, scope_api, config)
    works_list.extend(res_list)
    
    
    # 模型级别同步列表
    # 模型级别同步需要注意：如果模型下面的文件没有.safetensors文件，则不同步此模型
    download_from_scope= list(set(scope_res).difference(set(modelers_res)))
    download_from_modelers= list(set(modelers_res).difference(set(scope_res)))
    
    ## gitcode同步
    gitcode_res=gitcode_conn.get_model_list(config=config)
    import_to_gitcode=list(set(scope_visibility_res).difference(set(gitcode_res)))
    
    MODELERS_NAME=config['constant']['MODELERS_NAME']
    MODELSCOPE_NAME=config['constant']['MODELSCOPE_NAME']
    GITCODE_NAME=config['constant']['GITCODE_NAME']
    
    for m in download_from_modelers:
        works_list.append([m, MODELSCOPE_NAME])        
    
    for m in download_from_scope:
        works_list.append([m, MODELERS_NAME])
        
    for m in import_to_gitcode:
        works_list.append([m, GITCODE_NAME]) 
    return  works_list

def get_readme_in_directory(directory):
    """在指定目录中查找 README.md（不进入子目录）"""
    for item in os.listdir(directory):
        item_path = os.path.join(directory, item)
        if os.path.isfile(item_path) and item == 'README.md':
            return item_path
    return None

def get_license_info_from_web(file_path:str, license_value:str, web_to:str, config:dict):
    '''_summary_
    根据下载网站的readme中license关键字，匹配上传网站需要的license关键字，做替换。
    Args:
        file_path (str): 文件地址，此处主要用在logger里
        license_value (str): 前面步骤中获取到的协议名关键字
        web_to (str): 需要上传到哪个网站
        config (dict): 配置信息

    Returns:
        str(None): 正常返回替换并拼接完整的license信息， 若异常返回None
    '''    
    logger= logging.getLogger(config["global"]['logger_name'])
    MODELERS_NAME= config['constant']['MODELERS_NAME']
    SCOPE_NAME= config['constant']['MODELSCOPE_NAME']
    modelers_license_list:list= config["modelers_cfg"]["license"]
    scope_license_list:list= config["modelscope_cfg"]["license"]
    if len(modelers_license_list)!= len(scope_license_list): 
        logger.error("魔乐和魔塔的license列表长度不一致，请检查配置文件。")
        return None
    
    idx= len(modelers_license_list)-1 
    license_info=None
    if web_to==SCOPE_NAME:
        if license_value in modelers_license_list:
            idx= modelers_license_list.index(license_value)
        else:
            logger.warning(f"model_tools, get_license_info_from_web() {file_path}上传至{web_to},未找到匹配license替换，将使用{scope_license_list[idx]},请注意检查。")
        license_info=config["modelscope_cfg"]["license_name"]+": "+ scope_license_list[idx]
        return license_info
    elif web_to==MODELERS_NAME:
        if license_value in scope_license_list:
            idx= scope_license_list.index(license_value)
        else:
            logger.warning(f"model_tools, get_license_info_from_web() {file_path}上传至{web_to},未找到匹配license替换，将使用{modelers_license_list[idx]},请注意检查。")
        license_info=config["modelers_cfg"]["license_name"]+": "+ modelers_license_list[idx]
        return license_info
    else:
        logger.error(f"model_tools, get_license_info_from_web() web_to参数错误，web_to={web_to}与配置中的网站名不匹配，请检查。")
        return None



def add_readme_license(file_path:str, license_value:str, web_to:str, config:dict):
    '''_summary_
    用于从modelscope下载readme.md上传至modelers时，添加license信息。 默认为mit
    Args:
        file_path (str): _description_
        license_value (str): _description_
        config (dict): _description_

    Returns:
        _type_: _description_
    '''   
    logger= logging.getLogger(config["global"]['logger_name'])
    
    license_info= get_license_info_from_web(file_path ,license_value, web_to,config)
    if license_info is None:
        return -1
    
    
    try:
        # 1. 读取文件内容
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 2. 构建严格的YAML Front Matter
        # 格式: 顶格的---行 + 顶格的license_info行 + 顶格的---行 + 换行
        frontmatter = f"---\n{license_info}\n---\n"

        # 3. 直接添加到文件开头
        new_content = frontmatter + content

        # 4. 保存文件
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return 0

    except:
        traceback.print_exc()
        logger.error(f"model_tools, add_readme_license(): license_info写入{file_path}失败")
        return -1 


def rm_readme_yaml_front_matter(file_path:str, config:dict):
    '''_summary_
    删除README文件中的YAML Front Matter并保存，方便后续只计算readme正文的sha256
    Args:
        file_path (str): 文件路径
        config (dict): 配置信息

    Returns:
        int: 结果状态
    '''
    logger= logging.getLogger(config["global"]['logger_name'])
    
    if not os.path.exists(file_path):
        logger.error(f"model_tools, rm_readme_yaml_front_matter() 文件不存在: {file_path}") 
        return -1 , None
    
    try:
        # 1. 读取文件
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 2. 查找YAML Front Matter
        # 支持---前后可能有空格，兼容不同换行符
        pattern = r'^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)'
        match = re.search(pattern, content, re.DOTALL | re.MULTILINE)
        
        if not match:
            return 0, None  # 没有YAML块也算成功
        
        yaml_content = match.group(1)
        full_match = match.group(0)
        
        # 3. 提取license
        scope_license_name=config["modelscope_cfg"]["license_name"]
        modelers_license_name=config["modelers_cfg"]["license_name"]
        license_name_list=[scope_license_name, modelers_license_name]
        license_name_list=list(set(license_name_list))
    
        license_value = None
        try:
            data = yaml.safe_load(yaml_content)
            if data:
                # 检查常见license字段名
                for field in license_name_list:
                    if field in data:
                        license_value = data[field]
                        # if isinstance(license_value, list) and license_value:
                        #     license_value = license_value[0]
                        break
        except:
            traceback.print_exc()
            logger.warning(f"model_tools, rm_readme_yaml_front_matter(): {file_path}提取license异常。")
            pass  # YAML解析失败不影响删除操作
        
        # 4. 删除YAML块并保存
        new_content = content.replace(full_match, '', 1).lstrip('\n\r')
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)    
    except:
        traceback.print_exc()
        logger.error(f"model_tools, rm_readme_yaml_front_matter(): {file_path}删除yaml块或回写失败，请检查")
        return -1, license_value

    return 0, license_value


def calculate_file_sha256(file_path:str, config:dict):  
    """
    计算本地文件的 SHA256 哈希值
    
    Args:
        file_path: 文件路径
        
    Returns:
        str: 文件的 SHA256 哈希值（十六进制字符串）
    """
    logger= logging.getLogger(config["global"]['logger_name'])
    
    sha256_hash = hashlib.sha256()
    
    try:
        with open(file_path, "rb") as f:
            # 逐块读取文件内容并更新哈希值
            # 这里使用 8192 字节的块大小，这是一个比较合理的值
            for chunk in iter(lambda: f.read(8192), b""):
                sha256_hash.update(chunk)
        
        return sha256_hash.hexdigest()
    
    except FileNotFoundError:
        traceback.print_exc()
        logger.error(f"model_tools, calculate_file_sha256() 文件 {file_path} 不存在")
        return None
    except Exception:
        traceback.print_exc()
        logger.error(f"model_tools, calculate_file_sha256() 读取文件 {file_path} 时发生错误")
        return None


def is_modelers_README_init(file_path:str):
    '''_summary_
    判断下载到本地的readme文件是否是魔乐初始化的。
    Args:
        file_path (str): 魔乐下载的README.md的本地文件路径

    Returns:
        bool: 返回判断结果
    '''    
    with open(file_path, 'r', encoding='utf-8') as file:
        content = file.read()
    content=re.sub(r'\n$','',content)
    lis=content.split('\n')
    if lis[0]=="---" and lis[len(lis)-1]=='---': return True
    else: return False


def get_modelers_files_sha256(model_name:str, modelers_api:OmApi, config:dict):
    '''_summary_
    获取魔乐某个模型的每个文件名及其对应的sha256、时间戳
    Args:
        model_name (str): 模型名
        modelers_api (OmApi): 魔乐api
        config (dict): 配置信息

    Returns:
        _type_: _description_
    ''' 
    # print(f"开始检查模型{model_name}")
    logger= logging.getLogger(config["global"]['logger_name'])
    
    repo_id=config["modelscope_cfg"]['repo_name']+"/"+model_name
    
    try:
        modelers_model_file=modelers_api.list_repo_tree(
            repo_id=repo_id
        )
    except:
        traceback.print_exc()
        logger.error("model_tools.get_modelers_files_sha256(), list_repo_tree()获取魔乐模型文件列表异常")
        return -1
    
    modelers_sha256_filelist={}
    for file in modelers_model_file:
        # print(f"开始检查模型{model_name},文件{file.path}")
        if isinstance(file, RepoFile) and not file.path.startswith("."):
            if file.lfs is not None:
                modelers_sha256_filelist[file.path]=[file.lfs.sha256,dt.timestamp(file.last_commit.date)]
            else:
                # 下载此单文件：
                res= model_updown.modelers_file_down(model_name=model_name,file_name=file.path,modelers_api=modelers_api,config=config)
                if res!=0: 
                    return -1 
                else:
                    file_path= config["global"]['weights_path']+ os.sep+ model_name+ os.sep+file.path
                    ## 单独处理readme
                    if file.path=="README.md":
                        if file.last_commit.title=='Initial commit\n' or is_modelers_README_init(file_path):
                            continue
                        else:
                            res, license_value= rm_readme_yaml_front_matter(file_path,config)
                            if res==-1: return -1
                            res= file_sha256= calculate_file_sha256(file_path, config)
                            if res==None: return -1 
                            modelers_sha256_filelist[file.path]=[file_sha256, dt.timestamp(file.last_commit.date)]
                    else:
                        file_sha256= calculate_file_sha256(file_path, config)
                        modelers_sha256_filelist[file.path]=[file_sha256, dt.timestamp(file.last_commit.date)]
    
    # 删除本地目录:
    dir= config["global"]['weights_path']+ os.sep+ model_name
    res = model_updown.rm_local_model(dir,config)
    if res!=0:
        return -1       
    return modelers_sha256_filelist

def get_scope_files_sha256(model_name:str, scope_api:HubApi, config:dict):
    '''_summary_

    Args:
        model_name (str): 模型名
        scope_api (HubApi): 魔塔API
        config (dict): 配置文件

    Returns:
        _type_: _description_
    '''    
    repo_id=config["modelscope_cfg"]['repo_name']+"/"+model_name

    scope_model_files= scope_api.get_model_files(
        model_id=repo_id
    )

    scope_sha256_filelist={}
    for file in scope_model_files:
        if file["Type"] == "blob" and not str(file["Name"]).startswith(".") :
            if file["Name"]=='README.md':
                res= get_model_info_from_scope(model_name,scope_api,config)
                if res==-1 or str(res['ReadMeContent']).startswith("### 当前模型的贡献者未提供更加详细的模型介绍。模型文件和权重，可浏览“模型文件”页面获取。") or str(res['ReadMeContent']).endswith("及时完善模型卡片内容。</p>") or \
                str(res['ReadMeContent']).startswith("### You are viewing the default Readme template as no detailed") or str(res['ReadMeContent']).endswith("the model contribution documentation</a>.</p>"): # 过滤中英文版modelscope初始的readme.md
                    # print("INFO: scope中readme为网站初始化， 忽略")
                    continue
                    # scope_sha256_filelist[file["Name"]] = [None,file["CommittedDate"]]
                else:
                    model_updown.scope_file_down(model_name,"README.md",scope_api,config)
                    readme_path=config["global"]['weights_path']+ os.sep+ model_name+ os.sep+"README.md"
                    res, license_value= rm_readme_yaml_front_matter(readme_path,config)
                    if res== -1: continue
                    file_sha256= calculate_file_sha256(readme_path,config)
                    scope_sha256_filelist[file["Name"]] = [file_sha256,file["CommittedDate"]]
            else:
                scope_sha256_filelist[file["Name"]] = [file.get("Sha256", "None"),file["CommittedDate"]]
            
    return scope_sha256_filelist


def get_file_worklist_per_model(model_name:str, modelers_api:OmApi, scope_api:HubApi, config:dict):
    '''_summary_
    为每个模型生成文件级需要同步的文件队列
    注：本同步器中，文件级同步只遵循单向同步原则： 只考虑魔塔向魔乐同步的情况。
    Args:
        model_name (str): _description_
        modelers_api (OmApi): _description_
        scope_api (HubApi): _description_
        config (dict): _description_

    Returns:
        _type_: _description_
    '''
    scope_dict= get_scope_files_sha256(model_name,scope_api,config)
    scope_model_list=scope_dict.keys()
    # print(len(scope_model_list))
    modelers_dict=get_modelers_files_sha256(model_name,modelers_api,config)
    if modelers_dict==-1: return -1
    # 如果scope_dict中没有readme，那么modelers_dict也不应该有readme
    readme_need= scope_dict.get('README.md',None)
    if readme_need is None:
        res= scope_dict.pop("README.md",None)
        # print("此时，如果modelers_dict中有readme，需剔除")
        res= modelers_dict.pop("README.md",None)
        # print(res)
        
    modelers_model_list=modelers_dict.keys()
    # print(len(modelers_model_list))

    intersection_list=list(set(scope_model_list).intersection(set(modelers_model_list)))

    FILE_OPTION_UPDATE= config['constant']['FILE_OPTION_UPDATE']
    FILE_OPTION_DELETE= config['constant']['FILE_OPTION_DELETE']

    work_list=[]
    for inter in intersection_list:
        if modelers_dict[inter][0]!=scope_dict[inter][0]:
            # print(f"INFO： 修改同步：魔塔文件{inter}向魔乐同步")
            work_list.append([model_name, config['constant']['MODELERS_NAME'], inter, FILE_OPTION_UPDATE])

    ## 需要向魔乐同步的文件：
    add_list= list(set(scope_model_list).difference(set(modelers_model_list)))
    # print("INFO：增量同步：", add_list)
    for l in add_list:
        work_list.append([model_name, config['constant']['MODELERS_NAME'], l, FILE_OPTION_UPDATE])
    #魔乐中需要删除的文件：
    del_list = list(set(modelers_model_list).difference(set(scope_model_list)))
    # print("INFO: 删减同步：", del_list)
    for l in del_list:
        work_list.append([model_name, config['constant']['MODELERS_NAME'], l, FILE_OPTION_DELETE])
        
    return work_list
    
    