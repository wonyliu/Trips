import os
import json
import base64
import pandas as pd
from flask import Flask, render_template, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

# 引入之前的监控逻辑核心模块
from monitor import run_monitor_task, EXCEL_FILE, CONFIG_FILE

app = Flask(__name__)
# 彻底禁用模板缓存，修改 HTML 后刷新即可生效，无需重启服务器
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.jinja_env.auto_reload = True

import threading
from datetime import datetime

scheduler = BackgroundScheduler()
monitor_lock = threading.Lock()
monitor_cancel_event = threading.Event()

DEFAULT_VLM_WORKFLOW_STEPS = [
    {
        "id": "launch_taobao",
        "name": "启动淘宝 App",
        "action": "launch_taobao",
        "enabled": True
    },
    {
        "id": "tap_my_tab",
        "name": "点击我的",
        "action": "click_vlm",
        "target_desc": "底部导航栏里写着 我的 或 我的淘宝 的按钮",
        "retries": 3,
        "wait_after": 2.5,
        "enabled": True
    },
    {
        "id": "tap_favorites",
        "name": "点击收藏",
        "action": "click_vlm",
        "target_desc": "页面中写着 收藏 或 收藏夹 的入口",
        "retries": 3,
        "wait_after": 3,
        "enabled": True
    },
    {
        "id": "random_product",
        "name": "随机执行",
        "action": "random_execute",
        "enabled": True,
        "children": [
            {
                "id": "open_random_favorite",
                "name": "随机打开一个收藏商品",
                "action": "open_random_favorite",
                "wait_after": 4,
                "enabled": True
            }
        ]
    },
    {
        "id": "swipe_after_open",
        "name": "进入商品后下滑约100像素",
        "action": "swipe",
        "from_coord": "600,1900",
        "to_coord": "600,1800",
        "duration_ms": 350,
        "wait_after": 1.2,
        "enabled": True
    },
    {
        "id": "open_sku_panel",
        "name": "点击共xx款展开 SKU",
        "action": "click_vlm",
        "target_desc": "商品页中写着 共xx款 或 共X款 的规格展开入口",
        "retries": 3,
        "wait_after": 2.5,
        "enabled": True
    },
    {
        "id": "extract_skus_loop",
        "name": "循环翻页识别全部 SKU",
        "action": "extract_skus_loop",
        "from_coord": "600,2050",
        "to_coord": "600,900",
        "duration_ms": 500,
        "wait_after": 1.3,
        "max_scrolls": 8,
        "enabled": True,
        "region_start": "",
        "region_end": ""
    }
]

DEFAULT_VLM_COORD_GROUPS = {
    "favorites_default": []
}

# 全局监控状态对象
MONITOR_STATUS = {
    "is_running": False,
    "cancel_requested": False,
    "total": 0,
    "current": 0,
    "success_count": 0,
    "fail_count": 0,
    "last_finished_time": None,
    "url_results": {}, # {url_idx: {"success": bool, "msg": str}}
    "last_result": None # 存储最近一次 run_monitor_task 的完整返回字典
}

def get_config():
    if not os.path.exists(CONFIG_FILE):
        return {
            "urls": [],
            "interval_hours": 1,
            "is_running": False,
            "vlm_workflow_steps": DEFAULT_VLM_WORKFLOW_STEPS,
            "vlm_coord_groups": DEFAULT_VLM_COORD_GROUPS,
        }
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
    if "vlm_workflow_steps" not in config:
        config["vlm_workflow_steps"] = DEFAULT_VLM_WORKFLOW_STEPS
    if "vlm_coord_groups" not in config:
        config["vlm_coord_groups"] = DEFAULT_VLM_COORD_GROUPS
    return config

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

from datetime import datetime

def start_job(config=None):
    if config is None:
        config = get_config()
    
    schedule_type = config.get("schedule_type", "interval") # 'interval' 或 'cron'
    interval_hours = config.get("interval_hours", 1)
    schedule_times = config.get("schedule_times", []) # ["09:00", "18:00"]

    scheduler.remove_all_jobs()
    
    if schedule_type == 'cron' and schedule_times:
        print(f"[调度中心] 切换至定点轮询模式: {schedule_times}")
        for idx, t_str in enumerate(schedule_times):
            try:
                hour, minute = map(int, t_str.split(':'))
                scheduler.add_job(
                    func=wrapped_monitor_task,
                    trigger=CronTrigger(hour=hour, minute=minute),
                    id=f'monitor_cron_{idx}',
                    replace_existing=True
                )
            except Exception as e:
                print(f"[调度中心] 解析时间点 {t_str} 失败: {e}")
    else:
        print(f"[调度中心] 间隔轮询模式: {interval_hours} 小时/次")
        scheduler.add_job(
            func=wrapped_monitor_task,
            trigger=IntervalTrigger(hours=interval_hours),
            id='monitor_job',
            next_run_time=datetime.now(),
            replace_existing=True
        )

    if not scheduler.running:
        print("[调度中心] APScheduler 核心已点火启动")
        scheduler.start()
    else:
        print("[调度中心] APScheduler 核心任务配置已完成更新")
    
    # 打印当前加载的任务，用于调试
    current_jobs = scheduler.get_jobs()
    print(f"[调度中心] 当前已装载任务清单 ({len(current_jobs)} 个):")
    for j in current_jobs:
        print(f"  - ID: {j.id}, 触发器: {j.trigger}, 下次运行: {j.next_run_time}")

def wrapped_monitor_task(limit=None):
    """包装监控任务，增加锁控制和状态更新"""
    if not monitor_lock.acquire(blocking=False):
        print("[调度中心] 警告：监控任务已在运行中，跳过本次执行。")
        return
    
    try:
        monitor_cancel_event.clear()
        config = get_config()
        urls = config.get("urls", [])
        
        # 如果没有传入显式 limit (说明是定时任务)，则从配置中读取 test_limit
        if limit is None:
            limit = config.get("test_limit")
            
        api_mode = config.get("api_mode")
        if limit is not None:
            # 只有在非 VLM 模式下才打印这个限制日志，避免混淆
            if api_mode != "vlm":
                urls = urls[:limit]
                print(f"[调度中心] 本次任务执行应用了数量限制: {limit}")
             
        # 初始化状态
        if api_mode == "vlm":
            if limit is not None and limit > 0:
                total = limit
            else:
                total = int(config.get("scan_item_count", 5) or 5)
        else:
            total = len(urls)

        MONITOR_STATUS["is_running"] = True
        MONITOR_STATUS["cancel_requested"] = False
        MONITOR_STATUS["total"] = total
        MONITOR_STATUS["current"] = 0
        MONITOR_STATUS["success_count"] = 0
        MONITOR_STATUS["fail_count"] = 0
        MONITOR_STATUS["url_results"] = {}
        
        def status_callback(url_idx, success, msg):
            MONITOR_STATUS["current"] += 1
            if success:
                MONITOR_STATUS["success_count"] += 1
            else:
                MONITOR_STATUS["fail_count"] += 1
            MONITOR_STATUS["url_results"][str(url_idx)] = {"success": success, "msg": msg}
            
        # 执行实际任务 (传入回调)
        # 注意：需要修改 monitor.py 里的 run_monitor_task 支持 status_callback
        task_result = run_monitor_task(limit=limit, status_callback=status_callback, cancel_event=monitor_cancel_event)
        MONITOR_STATUS["last_result"] = task_result
        if MONITOR_STATUS["total"] and MONITOR_STATUS["current"] > MONITOR_STATUS["total"]:
            MONITOR_STATUS["total"] = MONITOR_STATUS["current"]
         
        MONITOR_STATUS["last_finished_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    finally:
        MONITOR_STATUS["is_running"] = False
        monitor_lock.release()

# --- 路由：前端页面 ---
@app.route('/')
def browser_engine():
    return render_template('browser_engine.html')

@app.route('/legacy_api')
def index_legacy():
    return render_template('index.html')

@app.route('/share/dashboard')
def share_dashboard():
    """公开共享看板（免登录）"""
    return render_template('share_dashboard.html')

# --- 新增的 Browser Engine API ---
@app.route('/api/browser/auth_status', methods=['GET'])
def api_browser_auth_status():
    from playwright_engine import AUTH_FILE
    return jsonify({"authorized": os.path.exists(AUTH_FILE)})
    
@app.route('/api/browser/init_auth', methods=['POST'])
def api_browser_init_auth():
    data = request.json or {}
    keep_open = data.get('keep_open', False)
    from playwright_engine import init_auth
    success = init_auth(keep_open=keep_open)
    if success:
        return jsonify({"success": True})
    return jsonify({"success": False, "msg": "鉴权流程未完成或超时"})

@app.route('/api/browser/test_probe', methods=['POST'])
def api_browser_test_probe():
    data = request.json
    url = data.get('url')
    if not url:
         return jsonify({"success": False, "msg": "无效的商品链接"})
         
    from playwright_engine import fetch_item_data_via_browser
    res = fetch_item_data_via_browser(url, timeout_ms=30000)
    return jsonify(res)

@app.route('/api/vlm/current_screen', methods=['GET'])
def api_vlm_current_screen():
    try:
        from screen_capture import ScreenCapture
        import cv2

        cap = ScreenCapture("scrcpy")
        if not cap.find_window():
            return jsonify({"success": False, "msg": "未检测到 scrcpy 投屏窗口"}), 400

        image = cap.capture()
        if image is None:
            return jsonify({"success": False, "msg": "未能截取当前手机画面"}), 400

        ok, encoded = cv2.imencode('.png', image)
        if not ok:
            return jsonify({"success": False, "msg": "截图编码失败"}), 500

        image_base64 = base64.b64encode(encoded.tobytes()).decode('ascii')
        return jsonify({
            "success": True,
            "image_base64": image_base64,
            "width": int(image.shape[1]),
            "height": int(image.shape[0]),
        })
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 500

@app.route('/api/vlm/test_step', methods=['POST'])
def api_vlm_test_step():
    data = request.json or {}
    step = data.get("step", {})
    if not isinstance(step, dict):
        return jsonify({"success": False, "msg": "step 必须是对象"}), 400

    try:
        from workflow_agent import TaobaoWorkflowAgent

        logs = []
        agent = TaobaoWorkflowAgent(
            status_callback=lambda msg: logs.append(msg),
            workflow_steps=[],
            auto_cleanup=False,
            coord_groups=get_config().get("vlm_coord_groups", {}),
        )
        result = agent.preview_workflow_step(step)
        result["logs"] = logs
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 500

# --- API：获取配置 ---
@app.route('/api/config', methods=['GET'])
@app.route('/api/config/get', methods=['GET'])
@app.route('/api/config/load', methods=['GET'])
def api_get_config():
    config = get_config()
    if "schedule_type" not in config: config["schedule_type"] = "interval"
    if "schedule_times" not in config: config["schedule_times"] = ["09:00"]
    if "vlm_workflow_steps" not in config: config["vlm_workflow_steps"] = DEFAULT_VLM_WORKFLOW_STEPS
    if "vlm_coord_groups" not in config: config["vlm_coord_groups"] = DEFAULT_VLM_COORD_GROUPS
    return jsonify(config)

# --- API：更新运行频率 ---
@app.route('/api/config/schedule', methods=['POST'])
def api_save_schedule():
    try:
        data = request.json or {}
        stype = data.get('schedule_type', 'interval')
        times = data.get('schedule_times', [])
        
        config = get_config()
        config['schedule_type'] = stype
        config['schedule_times'] = times
        save_config(config)
        
        # 如果正在运行，立即重载任务
        if config.get('is_running'):
            start_job(config)
            
        return jsonify({"success": True, "msg": "定时配置已同步至内核"})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)})

@app.route('/api/config/interval', methods=['POST'])
def api_update_interval():
    data = request.json
    val = int(data.get('interval', 1))
    
    config = get_config()
    config['interval_hours'] = val
    save_config(config)
    
    # 如果正在运行，则平滑重启任务应用新时间
    if config.get('is_running'):
        start_job(config)
        
    return jsonify({"msg": "运行间隔已保存生效"})

# --- API：更新凭证密钥 ---
@app.route('/api/config/keys', methods=['POST'])
def api_update_keys():
    data = request.json or {}
    config = get_config()

    field_mapping = {
        'key': 'api_key',
        'secret': 'api_secret',
        'token': 'pushplus_token',
        'tbk_key': 'tbk_app_key',
        'tbk_secret': 'tbk_app_secret',
        'api_mode': 'api_mode',
        'rapidapi_key': 'rapidapi_key',
        'tmapi_token': 'tmapi_token',
        'apify_token': 'apify_token',
    }
    for payload_key, config_key in field_mapping.items():
        if payload_key in data:
            config[config_key] = data.get(payload_key, '')

    if 'scan_item_count' in data:
        config['scan_item_count'] = int(data.get('scan_item_count', 5) or 5)

    save_config(config)
    return jsonify({"msg": "访问凭证及 VLM 配置已保存"})

@app.route('/api/config/vlm_workflow', methods=['GET', 'POST'])
def api_vlm_workflow():
    config = get_config()
    if request.method == 'GET':
        return jsonify({
            "success": True,
            "steps": config.get("vlm_workflow_steps", DEFAULT_VLM_WORKFLOW_STEPS),
            "coord_groups": config.get("vlm_coord_groups", DEFAULT_VLM_COORD_GROUPS),
        })

    data = request.json or {}
    steps = data.get("steps", [])
    coord_groups = data.get("coord_groups")
    if not isinstance(steps, list):
        return jsonify({"success": False, "msg": "steps 必须是数组"}), 400
    if coord_groups is not None and not isinstance(coord_groups, dict):
        return jsonify({"success": False, "msg": "coord_groups 必须是对象"}), 400

    config["vlm_workflow_steps"] = steps
    if coord_groups is not None:
        config["vlm_coord_groups"] = coord_groups
    save_config(config)
    return jsonify({"success": True, "msg": "VLM 流程配置已保存"})

# --- API：管理URL ---
@app.route('/api/config/url', methods=['POST'])
def api_manage_url():
    data = request.json
    action = data.get('action')
    config = get_config()
    urls = config.get('urls', [])
    
    if action == 'add':
        url = data.get('url', '').strip()
        if url:
            urls.append({"url": url, "mapping": {}})
    elif action == 'remove':
        idx = data.get('index')
        if 0 <= idx < len(urls):
            urls.pop(idx)
    elif action == 'clear':
        urls = []
            
    config['urls'] = urls
    save_config(config)
    return jsonify({"msg": "链接管理成功", "count": len(urls)})

@app.route('/api/config/urls/batch', methods=['POST'])
def api_bulk_manage_urls():
    data = request.json
    new_urls_raw = data.get('urls', [])
    config = get_config()
    existing_urls = config.get('urls', [])
    existing_list = [u['url'] if isinstance(u, dict) else u for u in existing_urls]
    
    added_count = 0
    for url in new_urls_raw:
        url = url.strip()
        if not url: continue
        # 简单的合法性检查
        if 'tb.cn' in url or 'taobao.com' in url or 'tmall.com' in url:
            if url not in existing_list:
                existing_urls.append({"url": url, "mapping": {}})
                existing_list.append(url)
                added_count += 1
                
    config['urls'] = existing_urls
    save_config(config)
    return jsonify({"success": True, "added": added_count, "total": len(existing_urls)})

# --- API：引擎开关 ---
@app.route('/api/engine/toggle', methods=['POST'])
def api_toggle_engine():
    data = request.json
    action = data.get('action')
    config = get_config()
    
    if action == 'start':
        if config.get('is_running'):
            monitor_cancel_event.set()
            MONITOR_STATUS["cancel_requested"] = True
            scheduler.remove_all_jobs()
            config['is_running'] = False
            save_config(config)
            return jsonify({"success": True, "msg": "已请求终止当前 VLM 任务并关闭守护"})
        config['is_running'] = True
        save_config(config)
        start_job(config)
        msg = "VLM 守护已启动"
    else:
        config['is_running'] = False
        monitor_cancel_event.set()
        MONITOR_STATUS["cancel_requested"] = True
        scheduler.remove_all_jobs()
        msg = "VLM 守护已停止，并已请求终止当前任务"
        
    save_config(config)
    return jsonify({"msg": msg})


@app.route('/api/engine/jobs', methods=['GET'])
def api_get_jobs():
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "next_run_time": str(job.next_run_time),
            "trigger": str(job.trigger)
        })
    return jsonify({
        "jobs": jobs,
        "scheduler_running": scheduler.running,
        "current_time": str(datetime.now())
    })

# --- API：更新浏览器常驻模式 ---
@app.route('/api/config/browser_mode', methods=['POST'])
def api_update_browser_mode():
    data = request.json
    show_browser = data.get('show_browser', False)
    config = get_config()
    config['show_browser'] = show_browser
    save_config(config)
    return jsonify({"msg": "防封常驻模式已" + ("开启" if show_browser else "关闭")})

@app.route('/api/config/push_format', methods=['POST'])
def api_update_push_format():
    data = request.json
    push_format = data.get('push_format', 'html')
    config = get_config()
    config['push_format'] = push_format
    save_config(config)
    return jsonify({"success": True})

@app.route('/api/config/push_template', methods=['POST'])
def api_update_push_template():
    data = request.json
    push_template = data.get('push_template', '')
    config = get_config()
    config['custom_push_template'] = push_template
    save_config(config)
    return jsonify({"success": True, "msg": "推送模板已更新"})

@app.route('/api/config/test_limit', methods=['POST'])
def api_update_test_limit():
    data = request.json
    test_limit = data.get('test_limit', 1)
    config = get_config()
    config['test_limit'] = test_limit
    save_config(config)
    return jsonify({"msg": f"试跑限制已保存为 {test_limit}"})

# --- API：保存探针字段映射 ---
@app.route('/api/config/mapping', methods=['POST'])
def api_update_mapping():
    data = request.json
    idx = data.get('index')
    mapping = data.get('mapping')
    
    config = get_config()
    urls = config.get('urls', [])
    if 0 <= idx < len(urls):
        if isinstance(urls[idx], str):
            urls[idx] = {"url": urls[idx], "mapping": mapping}
        else:
            urls[idx]["mapping"] = mapping
        config['urls'] = urls
        save_config(config)
        return jsonify({"msg": "追踪提取范式已保存"})
    return jsonify({"error": "找不到对应下标的任务"})

# --- API：执行单页面字段探针测试 (无缓存) ---
@app.route('/api/engine/test_url', methods=['POST'])
def api_test_url():
    import requests
    import re
    data = request.json
    url = data.get('url')
    idx = data.get('index')
    
    config = get_config()
    api_key = config.get('api_key')
    api_secret = config.get('api_secret')
    
    api_mode = config.get('api_mode', 'onebound')
    tbk_key = config.get('tbk_app_key')
    tbk_secret = config.get('tbk_app_secret')
    
    if api_mode in ['onebound', 'fallback'] and (not api_key or not api_secret):
        if api_mode == 'onebound':
             return jsonify({"success": False, "msg": "API Key 或 Secret 未配置，无法探查。"})
        
    # 提取 ID
    from monitor import get_item_id_from_url, fetch_item_data_tbk
    item_id = get_item_id_from_url(url)
    if not item_id:
        return jsonify({"success": False, "msg": "解析 URL 失败，无法提取商品 ID。"})
        
    use_tbk = False
    if api_mode == 'apify':
        from monitor import fetch_item_data_apify
        apify_token = config.get('apify_token')
        if not apify_token:
            return jsonify({"success": False, "msg": "Apify Token 未配置，无法探查。"})
        
        # 调用 Apify 获取数据 (内部已适配 Baxnian 结构)
        apify_results = fetch_item_data_apify(url, apify_token)
        if not apify_results:
            return jsonify({"success": False, "msg": "Apify 探测失败。请确保您在 Apify 官网有对应 Actor 的试用权限，且 Token 正确。"})
        
        # 将 Apify 返回的第一个条目作为基础信息
        item = apify_results[0]
        base_info = {
            "title": item.get('商品名称', '未知商品'),
            "pic_url": "", # Apify 结果有时不含主图链接
            "shop_name": item.get('店铺名称', '未知店铺')
        }
        
        # 构建 SKU 列表，直接传入原始数据供前端映射
        skus_list = []
        for s in apify_results:
            skus_list.append({
                "properties_name": s.get('商品规格', '规格/SKU'),
                "raw_data": s
            })
            
        urls = config.get('urls', [])
        current_mapping = {}
        if 0 <= idx < len(urls):
            u_obj = urls[idx]
            if isinstance(u_obj, dict):
                current_mapping = u_obj.get("mapping", {})
                
        return jsonify({
            "success": True, 
            "base": base_info,
            "skus": skus_list,
            "fields_config": current_mapping,
            "msg": f"Apify 探测成功！已截获 {len(apify_results)} 个数据点。"
        })

    if api_mode in ['tbk', 'fallback']:
        if tbk_key and tbk_secret:
            use_tbk = True
        elif api_mode == 'tbk':
            return jsonify({"success": False, "msg": "淘宝客 App Key/Secret 未配置，无法探查。"})

    if use_tbk:
        # 使用淘宝客 API 探针
        tbk_data_list = fetch_item_data_tbk(item_id, tbk_key, tbk_secret)
        if tbk_data_list:
            tbk_item = tbk_data_list[0]
            base_info = {
                "title": tbk_item.get('商品名称', '未知商品(TBK)'),
                "pic_url": "",
                "shop_name": tbk_item.get('店铺名称', '未知店铺')
            }
            # 伪造一条淘宝客 SKU 数据
            skus_list = [{
                "properties_name": "默认统一规格(淘宝客)",
                "raw_data": {
                    "zk_final_price": str(tbk_item.get('当前价格', 0)),
                    "reserve_price": str(tbk_item.get('原价', 0)),
                    "title": tbk_item.get('商品名称'),
                    "nick": tbk_item.get('店铺名称')
                }
            }]
            urls = config.get('urls', [])
            current_mapping = {}
            if 0 <= idx < len(urls):
                u_obj = urls[idx]
                if isinstance(u_obj, dict):
                    current_mapping = u_obj.get("mapping", {})
            return jsonify({
                "success": True, 
                "base": base_info,
                "skus": skus_list,
                "fields_config": current_mapping,
                "msg": "淘宝客探测成功！由于官方API不含SKU级细节，所以仅显示归一化抓取快照。"
            })
        else:
            if api_mode == 'tbk':
                return jsonify({"success": False, "msg": "淘宝客 API 请求失败或该商品非推广商品。"})
            # Fallback will continue to Onebound below

    # 请求 API (实时无缓存, Onebound)
    params = {
        'num_iid': item_id,
        'key': api_key,
        'secret': api_secret,
        'is_promotion': 1,
        'cache': 'no'
    }
    
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = None
        for attempt in range(3):
            try:
                response = requests.get('https://api-gw.onebound.cn/taobao/item_get/', params=params, timeout=20, verify=False)
                break
            except Exception as retry_err:
                print(f"[WARN] API 请求第 {attempt+1} 次失败: {retry_err}")
                if attempt == 2:
                    raise retry_err
                import time; time.sleep(1)
        res_data = response.json()
        if 'item' not in res_data:
            return jsonify({"success": False, "msg": f"接口请求失败或数据异常: {res_data.get('reason', '未知')}"})
            
        item = res_data['item']
        
        # 提取商品基本信息
        base_info = {
            "title": item.get('title', '未知商品'),
            "pic_url": item.get('pic_url', ''),
            "shop_name": item.get('seller_info', {}).get('shop_name', '未知店铺')
        }
                
        # 提取所有 SKU 的原始数据
        skus_list = []
        skus_raw = item.get('skus', {})
        print(f"[DEBUG] skus_raw type={type(skus_raw).__name__}, keys={list(skus_raw.keys()) if isinstance(skus_raw, dict) else 'N/A'}")
        skus_source = skus_raw.get('sku', []) if isinstance(skus_raw, dict) else []
        print(f"[DEBUG] skus_source type={type(skus_source).__name__}, len={len(skus_source) if isinstance(skus_source, list) else 'N/A'}")
        if not skus_source:
             # 有些商品可能没有 SKU 或者是单规格
             skus_list.append({
                 "properties_name": "默认规格/单规格",
                 "raw_data": {k: v for k, v in item.items() if not isinstance(v, (dict, list))}
             })
        else:
            for s in skus_source:
                skus_list.append({
                    "properties_name": s.get('properties_name', '标准规格'),
                    "raw_data": s
                })
        print(f"[DEBUG] skus_list count={len(skus_list)}")
        # 读取当前 mapping
        urls = config.get('urls', [])
        current_mapping = {}
        if 0 <= idx < len(urls):
            u_obj = urls[idx]
            if isinstance(u_obj, dict):
                current_mapping = u_obj.get("mapping", {})
                
        return jsonify({
            "success": True, 
            "base": base_info,
            "skus": skus_list,
            "fields_config": current_mapping
        })
    except Exception as e:
        return jsonify({"success": False, "msg": f"接口探究调用异常: {str(e)}"})

# --- API：手动立即触发一次提取 ---
@app.route('/api/engine/run_now', methods=['POST'])
def api_run_now():
    if not monitor_lock.acquire(blocking=False):
        monitor_cancel_event.set()
        MONITOR_STATUS["cancel_requested"] = True
        return jsonify({"success": True, "msg": "已请求终止当前 VLM 测试任务。"})
    
    # 因为调用的是同步阻塞函数，为了能立刻给前端返回并在后台跑，我们起一个新线程
    monitor_lock.release() # 立即释放刚才拿到的锁，交给线程内的 wrapped 处理
    
    try:
        data = request.json or {}
        limit = data.get('limit', None)
        if limit is not None:
            limit = int(limit)
            
        # 起新线程执行，防止 API 响应超时
        thread = threading.Thread(target=wrapped_monitor_task, kwargs={"limit": limit})
        thread.start()
        
        return jsonify({"success": True, "msg": "后台采集任务已下发，请观察进度条。"})
    except Exception as e:
        return jsonify({"success": False, "msg": f"执行失败: {str(e)}"})

@app.route('/api/engine/cancel', methods=['POST'])
def api_cancel_engine():
    monitor_cancel_event.set()
    MONITOR_STATUS["cancel_requested"] = True
    stop_scheduler = bool((request.json or {}).get("stop_scheduler", False))
    if stop_scheduler:
        config = get_config()
        config["is_running"] = False
        save_config(config)
        scheduler.remove_all_jobs()
    return jsonify({"success": True, "msg": "已请求终止当前任务"})

@app.route('/api/engine/status', methods=['GET'])
def api_engine_status():
    """返回当前监控引擎的运行状态及最后一次结果"""
    return jsonify(MONITOR_STATUS)

# --- API：测试微信通知 ---
@app.route('/api/test/notify', methods=['POST'])
def api_test_notify():
    try:
        from monitor import send_pushplus_alert
        config = get_config()
        test_alerts = [{
            "店铺名称": "测试店铺",
            "商品名称": "测试商品 - 价格监控系统通知测试",
            "商品规格": "测试规格",
            "旧价格": 100.00,
            "新价格": 88.00,
            "变化幅度": -12.00
        }]
        notify_result = send_pushplus_alert(test_alerts, config)
        success = "成功" in (notify_result or "")
        return jsonify({"success": success, "msg": notify_result or "未知结果"})
    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(f"[严重错误] 微信测试接口崩溃: {error_msg}")
        return jsonify({"success": False, "msg": f"接口内部错误: {str(e)}", "traceback": error_msg}), 500

# --- API：按SKU分组的价格变化追踪 ---
@app.route('/api/price_changes', methods=['GET'])
def api_price_changes():
    if not os.path.exists(EXCEL_FILE):
        return jsonify({"error": "暂无记录，请确认已执行过抓取"})
    
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 30))
        
        df = pd.read_excel(EXCEL_FILE)
        df = df.fillna('')
        
        if df.empty:
            return jsonify({"sku_groups": [], "last_fetch_time": None})
        
        # 构建指纹
        df['指纹'] = df['店铺名称'].astype(str) + "|" + df['商品ID'].astype(str) + "|" + df['商品规格'].astype(str)
        
        # 兼容旧脏数据：获取时间列可能混有数字和字符串
        if '获取时间' in df.columns:
            df['_sort_time'] = pd.to_datetime(df['获取时间'], errors='coerce')
            df = df.sort_values(by=['_sort_time', '获取时间'], ascending=[True, True], na_position='last')
        
        # 获取最后一次抓取时间
        last_fetch_time = str(df['获取时间'].iloc[-1]) if '获取时间' in df.columns and not df.empty else None
        
        # 按指纹分组
        sku_groups = []
        for fingerprint, group in df.groupby('指纹', sort=False):
            group = group.sort_values(by='获取时间')
            
            timeline = []
            prev_price = None
            prev_stock = None
            for _, row in group.iterrows():
                price = float(row['当前价格']) if row['当前价格'] != '' else 0.0
                stock = int(row['库存']) if '库存' in row and row['库存'] != '' else 0
                
                price_diff = None
                if prev_price is not None and prev_price != price:
                    price_diff = round(price - prev_price, 2)
                elif prev_price is not None and prev_price == price:
                    price_diff = 0.0
                
                stock_diff = None
                if prev_stock is not None and prev_stock != stock:
                    stock_diff = stock - prev_stock
                elif prev_stock is not None and prev_stock == stock:
                    stock_diff = 0
                
                timeline.append({
                    "time": str(row.get('获取时间', '')),
                    "price": price,
                    "diff": price_diff,
                    "stock": stock,
                    "stock_diff": stock_diff
                })
                prev_price = price
                prev_stock = stock
            
            # 判断最后一次是否有变化
            has_latest_change = False
            has_latest_stock_change = False
            if len(timeline) >= 2:
                last_diff = timeline[-1]["diff"]
                if last_diff is not None and last_diff != 0.0:
                    has_latest_change = True
                last_stock_diff = timeline[-1]["stock_diff"]
                if last_stock_diff is not None and last_stock_diff != 0:
                    has_latest_stock_change = True
            
            first_row = group.iloc[0]
            last_entry = timeline[-1] if timeline else {}
            
            # 判断是否下架 (只要对应的 URL 下架了，所有 SKU 都标记为下架)
            is_off_shelf = False
            if '是否下架' in row and any(group['是否下架'] == 1):
                is_off_shelf = True

            # 找到原始 URL (基于 URL编号)
            url_no = first_row.get('URL编号')
            source_url = ""
            parsed_url_no = None
            if url_no:
                config = get_config()
                urls = config.get("urls", [])
                try:
                    parsed_url_no = int(url_no)
                    idx = parsed_url_no - 1
                    if 0 <= idx < len(urls):
                        u_obj = urls[idx]
                        source_url = u_obj.get("url") if isinstance(u_obj, dict) else u_obj
                except: pass

            sku_groups.append({
                "shop": str(first_row.get('店铺名称', '')),
                "product": str(first_row.get('商品名称', '')),
                "product_id": str(first_row.get('商品ID', '')),
                "sku": str(first_row.get('商品规格', '')),
                "url_no": parsed_url_no,
                "source_url": source_url,
                "latest_price": last_entry.get("price", 0.0),
                "latest_stock": last_entry.get("stock", 0),
                "has_latest_change": has_latest_change or is_off_shelf, # 下架也视为变化
                "is_off_shelf": is_off_shelf,
                "has_latest_stock_change": has_latest_stock_change,
                "timeline": timeline
            })
        
        # 将有最新变价的排在前面
        sku_groups.sort(key=lambda x: (not x['has_latest_change'], x['shop'], x['product']))
        
        total_groups = len(sku_groups)
        start_idx = (page - 1) * limit
        end_idx = start_idx + limit
        
        # 分页切片
        paged_groups = sku_groups[start_idx:end_idx]
        
        # 计算去重后的 URL 总数
        all_url_nos = {g['url_no'] for g in sku_groups if g.get('url_no') is not None}
        url_total = len(all_url_nos)

        return jsonify({
            "sku_groups": paged_groups, 
            "last_fetch_time": last_fetch_time,
            "url_total": url_total,
            "total": total_groups,
            "page": page,
            "limit": limit,
            "has_more": end_idx < total_groups
        })
    except Exception as e:
        return jsonify({"error": f"处理价格变化数据失败: {str(e)}"})

# --- API：读取历史 Excel ---
@app.route('/api/history', methods=['GET'])
def api_get_history():
    if not os.path.exists(EXCEL_FILE):
        return jsonify({"error": "暂无记录，请确认已执行过抓取"})
        
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 30))
        
        df = pd.read_excel(EXCEL_FILE)
        df = df.fillna('')
        
        if '获取时间' in df.columns:
            df['_sort_time'] = pd.to_datetime(df['获取时间'], errors='coerce')
            df = df.sort_values(by=['_sort_time', '获取时间'], ascending=[False, False], na_position='last')
            
        total_count = len(df)
        start_idx = (page - 1) * limit
        end_idx = start_idx + limit
        
        # 分页切片
        paged_df = df.iloc[start_idx:end_idx]
        if '_sort_time' in paged_df.columns:
            paged_df = paged_df.drop(columns=['_sort_time'])
        records = paged_df.to_dict('records')
        
        return jsonify({
            "records": records,
            "total": total_count,
            "page": page,
            "limit": limit,
            "has_more": end_idx < total_count
        })
    except Exception as e:
        return jsonify({"error": f"读取Excel分页失败: {str(e)}"})

# 启动时恢复状态
if __name__ == '__main__':
    # 强制不使用重载器防止定时任务重复实例化执行
    config = get_config()
    if config.get("is_running", False):
        start_job(config)
        
    print("\n\n" + "="*50)
    print(" [OK] Web 后台已启动!")
    print(" 请在浏览器访问: http://127.0.0.1:5000")
    print("="*50 + "\n\n")
    
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
