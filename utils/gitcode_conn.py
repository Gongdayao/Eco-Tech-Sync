import http.client
import json

import logging
import traceback

def get_model_list(config:dict,per_page=100):
    '''_summary_
        获取gicode组织下所的所有项目名列表
    Args:
        config (dict): _description_
        per_page (int, optional): _description_. Defaults to 100.

    Returns:
        _type_: _description_
    '''
    logger= logging.getLogger(config["global"]['logger_name'])
    
    org_name=config['gitcode_cfg']['repo_name']
    access_token=config['gitcode_cfg']['token']
    
    
    conn = http.client.HTTPSConnection("api.gitcode.com")
    payload = ''
    headers = {'Accept': 'application/json'}

    model_list=[]
    page=1
    while True:
        path=f"/api/v5/orgs/{org_name}/repos?access_token={access_token}&type=all&page={page}&per_page={per_page}&repo_type=model"
        conn.request("GET", path, payload, headers)
        res = conn.getresponse()
        data = res.read()
        # 将字符串解析为 Python 列表（每个元素是一个字典）
        try:
            repos_list = json.loads(data.decode("utf-8"))
            if not repos_list:
                break
        except json.JSONDecodeError as e:
            logger.error("gitcode_conn.get_model_list() 解析 JSON 失败:", e)
            logger.error("原始响应:", data.decode("utf-8"))
        
        model_list.extend(repos_list)
        page+=1
        
    models_name_list=[model['name'] for model in model_list]
    
    return models_name_list



def create_repo( model_name:str, config:dict,public:int=1):
    '''_summary_
    通过导入方式创建gitcode模型
    Args:
        model_name (str): _description_
        import_url (str): _description_
        config (dict): _description_
        public (int, optional): _description_. Defaults to 1.

    Returns:
        _type_: _description_
    '''
    
    logger= logging.getLogger(config["global"]['logger_name'])
    
    scope_org_name=config['modelscope_cfg']['repo_name']
    org_name=config['gitcode_cfg']['repo_name']
    access_token=config['gitcode_cfg']['token']
    import_url=config['gitcode_cfg']['scope_base_url']+scope_org_name+"/"+model_name+".git"
    
    
    payload = json.dumps({
        "name": model_name,
        "has_issues": True,
        "has_wiki": True,
        "can_comment": True,
        "public": public,
        "import_url": import_url,
        "repository_type": "model"
    })
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }
    path=f"/api/v5/orgs/{org_name}/repos?access_token={access_token}"
    
    conn=None
    try:
        conn = http.client.HTTPSConnection("api.gitcode.com")
        conn.request("POST", path, payload, headers)
        res = conn.getresponse()
        status = res.status
        data = res.read().decode("utf-8")
        if status not in (200, 201):
            logger.error(f"gitcode导入模型 {model_name} 失败，HTTP {status}。响应内容：{data}")
            return -1
        
        try:
            response_json = json.loads(data)
        except json.JSONDecodeError as e:
            traceback.print_exc()
            logger.error(f"GitCode 响应解析失败，状态码 {status}，原始数据：{data}，错误：{e}")
            return -1
            
        repo_url = response_json.get("html_url") or response_json.get("url")
        if repo_url:
            logger.info(
                f"GitCode 导入模型 {model_name} 成功，"
                f"请及时前往 https://ai.gitcode.com/Eco-Tech/{model_name}/setting/mirror 开启仓库镜像 pull 模式。"
            )
        else:
            logger.warning(
                f"GitCode 导入模型 {model_name} 成功，但响应中未包含 URL。"
            )
        return 0
    
    except http.client.HTTPException as e:
        traceback.print_exc()
        logger.error(f"HTTP 连接异常：{e}")
        return -1
    except Exception as e:
        traceback.print_exc()
        logger.error(f"创建仓库时发生未预期错误：{e}")
        return -1
    finally:
        if conn:
            conn.close()