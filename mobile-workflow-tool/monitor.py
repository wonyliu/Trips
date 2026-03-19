import requests
import json
import os
import re
import sys
import pandas as pd
from datetime import datetime
import hashlib
import time

# 修复 Windows GBK 终端无法输出 emoji/特殊字符导致崩溃的问题
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', errors='replace', buffering=1)
    sys.stderr = open(sys.stderr.fileno(), mode='w', encoding='utf-8', errors='replace', buffering=1)

CONFIG_FILE = 'config.json'
EXCEL_FILE = 'price_history.xlsx'
API_URL = 'https://api-gw.onebound.cn/taobao/item_get/' 

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def get_item_id_from_url(url):
    """从淘宝链接中提取商品ID，支持短链还原"""
    if not url or not isinstance(url, str):
        return None
        
    def extract_id(text):
        # 兼容标准的 id=...
        match = re.search(r"id=(\d+)", text)
        if match: return match.group(1)
        # 兼容 itemIds=...
        match = re.search(r"itemIds=(\d+)", text)
        if match: return match.group(1)
        # 兼容 /item/12345.htm
        match = re.search(r"item/(\d+)", text)
        if match: return match.group(1)
        # 兼容 num_iid=...
        match = re.search(r"num_iid=(\d+)", text)
        if match: return match.group(1)
        return None

    # 1. 尝试直接从原始尝试提取
    item_id = extract_id(url)
    if item_id: return item_id

    # 2. 如果是短链接，尝试还原
    if "e.tb.cn" in url or "t.tb.cn" in url:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
            resp = requests.head(url, headers=headers, allow_redirects=True, timeout=5)
            final_url = resp.url
            item_id = extract_id(final_url)
            if item_id: return item_id
        except Exception as e:
            print(f">>> [短链解析] 还原请求失败: {e}")
            
    return None

def fetch_item_data_rapidapi(item_id, rapidapi_key):
    """通过 RapidAPI (Taobao Advanced) 获取商品详细数据"""
    url = "https://taobao-advanced.p.rapidapi.com/api"
    querystring = {"num_iid": item_id, "api": "item_detail_new", "area_id": "110100"}
    headers = {
        "x-rapidapi-key": rapidapi_key,
        "x-rapidapi-host": "taobao-advanced.p.rapidapi.com"
    }
    
    try:
        response = requests.get(url, headers=headers, params=querystring, timeout=15)
        data = response.json()
        
        # 深度解析错误状态
        result_node = data.get('result', {})
        status_node = result_node.get('status', {}) if isinstance(result_node, dict) else {}
        
        # 兼容多种可能的 code 字段位置
        code = data.get('code') or status_node.get('code')
        msg = data.get('msg') or status_node.get('msg') or "未知错误"
        
        if code and str(code) != "200":
            print(f">>> [RapidAPI 报错] ID:{item_id} | Code:{code} | Msg:{msg}")
            return []
            
        if 'item' not in result_node and 'item' not in data:
            # 如果没有 code 但也没有 item 数据，视为失败
            print(f">>> [RapidAPI 异常] ID:{item_id} | 响应中未找到商品数据节点")
            return []
            
        # 兼容多种可能的 item 节点位置
        item = result_node.get('item', {}) or data.get('item', {})
        if not item: return []
        
        # 店铺名称优先从 seller 节点取
        seller_node = result_node.get('seller', {}) or data.get('seller', {})
        shop_name = seller_node.get('shop_title') or item.get('nick') or item.get('seller', {}).get('shopName') or '未知店铺'
        item_title = item.get('title', '未知商品')
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        result = []
        
        # 1. 尝试新的 sku_base 结构 (用户提供的真经)
        sku_base = item.get('sku_base', [])
        sku_props = item.get('sku_props', [])
        
        if sku_base:
            # 构建属性 ID 到 属性名 的映射
            vid_to_name = {}
            for prop in sku_props:
                for val in prop.get('values', []):
                    vid_to_name[str(val.get('vid'))] = val.get('name', '')
            
            for sku in sku_base:
                sku_id = str(sku.get('skuId', ''))
                prop_path = sku.get('propPath', '')
                
                # 还原规格名称
                spec_names = []
                for pair in prop_path.split(';'):
                    if ':' in pair:
                        vid = pair.split(':')[1]
                        if vid in vid_to_name:
                            spec_names.append(vid_to_name[vid])
                
                prop_name = " ".join(spec_names) if spec_names else f"规格:{sku_id}"
                
                # 价格逻辑：优先取 promotion_price
                price_val = sku.get('promotion_price') or sku.get('price') or 0
                try: 
                    final_price = float(str(price_val).replace(',', ''))
                except: 
                    final_price = 0.0
                
                row = {
                    "获取时间": now_str,
                    "店铺名称": shop_name,
                    "商品ID": item_id,
                    "商品名称": item_title,
                    "商品规格": prop_name,
                    "当前价格": final_price,
                    "原价": float(sku.get('price', final_price)) or 0.0,
                    "库存": int(sku.get('quantity', 0))
                }
                result.append(row)
        
        # 2. 如果没有 sku_base，尝试旧的 skus.sku 结构
        elif 'skus' in item and isinstance(item['skus'], dict) and 'sku' in item['skus']:
            skus_list = item['skus']['sku']
            promo_map = {}
            for p_item in item.get('promotion_price_list', []):
                sku_id = p_item.get('sku_id')
                if sku_id: promo_map[str(sku_id)] = p_item.get('price')
            
            for sku in skus_list:
                sku_id = str(sku.get('sku_id', ''))
                prop_name = sku.get('properties_name') or '默认规格'
                if ';' in prop_name:
                    prop_name = prop_name.split(';')[-1].split(':')[-1]
                elif ':' in prop_name:
                    prop_name = prop_name.split(':')[-1]

                price = sku.get('price')
                promo_price = promo_map.get(sku_id)
                final_price = float(promo_price) if promo_price else float(price) if price else 0.0
                
                result.append({
                    "获取时间": now_str,
                    "店铺名称": shop_name,
                    "商品ID": item_id,
                    "商品名称": item_title,
                    "商品规格": prop_name,
                    "原价": float(price) if price else 0.0,
                    "当前价格": final_price,
                    "库存": int(sku.get('quantity', 0))
                })
        
        # 3. 最后保底：单规格处理
        if not result:
            skus_summary = item.get('skus', {})
            # 价格可能是一个范围 "108 - 269"
            price_val = skus_summary.get('price') or item.get('price') or 0
            if isinstance(price_val, str) and '-' in price_val:
                price_val = price_val.split('-')[0].strip()
            
            try:
                final_price = float(str(price_val).replace(',', ''))
            except:
                final_price = 0.0
                
            promo_price = item.get('promotion_price')
            if promo_price:
                try: final_price = float(promo_price)
                except: pass

            result.append({
                "获取时间": now_str,
                "店铺名称": shop_name,
                "商品ID": item_id,
                "商品名称": item_title,
                "商品规格": "默认统一规格",
                "原价": final_price, # 单规格就不区分原价了
                "当前价格": final_price,
                "库存": int(item.get('num') or skus_summary.get('quantity') or 0)
            })
            
        return result
    except Exception as e:
        import traceback
        print(f"RapidAPI 数据解析出错 [ID:{item_id}]: {e}\n{traceback.format_exc()}")
        return []

def fetch_item_data_tmapi(item_id, tmapi_token):
    """通过 tmapi.top (Taobao Advanced v2) 获取商品详细数据"""
    # 接口文档参考: https://tmapi.top/docs/taobao-tmall/advanced-api/get-item-detail-by-id-v2/
    url = "https://api.tmapi.io/taobao/item_detail"
    params = {
        "item_id": item_id,
        "apiToken": tmapi_token
    }
    
    try:
        response = requests.get(url, params=params, timeout=20)
        
        if response.status_code != 200:
            try:
                data = response.json()
                msg = data.get('message') or data.get('msg') or "未知错误"
            except:
                msg = response.text[:100]
            
            # 特殊处理余额不足
            if response.status_code == 439 or "balance" in msg.lower():
                msg = "TMAPI 账户余额不足，请检查账户余额"
            
            print(f">>> [TMAPI 报错] ID:{item_id} | HttpCode:{response.status_code} | Msg:{msg}")
            return []

        data = response.json()
        # 有些接口成功时可能不带 code 字段，只要状态码是 200 且有数据即可判定成功
        # 如果有 code 字段且不等于 200，则报错
        if 'code' in data and data.get('code') not in [200, "200"]:
            msg = data.get('msg', '接口业务逻辑错误')
            print(f">>> [TMAPI 报错] ID:{item_id} | BizCode:{data.get('code')} | Msg:{msg}")
            return []
            
        item_data = data.get('data', {})
        if not item_data:
            print(f">>> [TMAPI 异常] ID:{item_id} | 响应 data 节点为空")
            return []
            
        shop_name = item_data.get('shop_info', {}).get('shop_name') or item_data.get('nick') or 'TMAPI商家'
        title = item_data.get('title', '未知商品')
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        current_data = []
        skus = item_data.get('skus', [])
        
        if not skus:
            # 单规格兜底
            price_info = item_data.get('price_info', {})
            # 优先提取优惠后的价格 (tmapi 通常在 price 字段放的是当前活动价)
            price = float(price_info.get('price') or price_info.get('price_min') or 0)
            origin_price = float(price_info.get('origin_price') or price_info.get('origin_price_min') or price)
            
            is_off_shelf = 0
            if "下架" in title or "不存在" in title:
                is_off_shelf = 1
                
            current_data.append({
                "获取时间": now_str, "店铺名称": shop_name, "商品ID": item_id, "商品名称": title,
                "商品规格": "默认统一规格", "当前价格": price, "原价": origin_price, "库存": int(item_data.get('stock', 0)),
                "URL编号": 0, "是否下架": is_off_shelf
            })
            print(f">>> [TMAPI 成功] ID:{item_id} (单规格) 价格:{price}")
        else:
            # 多规格解析
            for sku in skus:
                spec_str = sku.get('props_names') or f"规格ID:{sku.get('sku_id')}"
                
                # tmapi 的 skus 列表中通常直接包含 price 和 origin_price
                # 这里的 price 应该是促销后的动态价格
                price = float(sku.get('price') or 0)
                origin_price = float(sku.get('origin_price') or price)
                stock = int(sku.get('quantity', 0))
                
                is_off_shelf = 0
                if "下架" in title or "不存在" in title:
                    is_off_shelf = 1
                
                current_data.append({
                    "获取时间": now_str, "店铺名称": shop_name, "商品ID": item_id, "商品名称": title,
                    "商品规格": spec_str, "当前价格": price, "原价": origin_price, "库存": stock,
                    "URL编号": 0, "是否下架": is_off_shelf
                })
            print(f">>> [TMAPI 成功] ID:{item_id} (多规格) 截获 {len(skus)} 个变体")
            
        return current_data
        
    except Exception as e:
        print(f">>> [TMAPI 请求异常] ID:{item_id} | {e}")
        return []

def fetch_item_data_apify(target_url, apify_token):
    """通过 Apify Pizani Taobao Scraper 获取商品数据 (修复连通性与结构解析)"""
    config = load_config()
    actor_id = config.get('apify_actor_id', 'pizani/taobao-product-scraper')
    
    # 官方 API 路径：使用同步获取数据集模式
    api_url = f"https://api.apify.com/v2/acts/{actor_id.replace('/', '~')}/run-sync-get-dataset-items?token={apify_token}"
    
    # 输入协议适配: 经过对成功 Run ID (aKr0YQtksFaaAcgF0) 的逆向工程
    # 发现关键字段是 "product_url"。
    # 注意：移除住宅代理配置，因为免费账户通常无权调用，会触发 403。
    # 缺点是海外 IP 访问会导致淘宝返回英文标题和外币价格。
    payload = {
        "product_url": target_url
    }
    
    try:
        print(f">>> [Apify 调用] 引擎:{actor_id} | 目标: {target_url}")
        # 增加超时设计，同步运行耗时较长
        response = requests.post(api_url, json=payload, timeout=150)
        
        if response.status_code not in [200, 201]:
            msg = response.text[:300]
            if "rent" in msg.lower() or response.status_code == 403:
                msg = f"连接失败(Http {response.status_code})。如果您能手动Run却不能用API，请检查Token权限或是否启用了收费代理。"
            print(f">>> [Apify 报错] URL:{target_url} | HttpCode:{response.status_code} | Msg:{msg}")
            return []
            
        items = response.json()
        if not items or not isinstance(items, list):
            print(f">>> [Apify 异常] URL:{target_url} | 响应数据集为空")
            return []
            
        # 提取第一个结果
        item = items[0]
        
        # 适配 Baxnian 截图中的 JSON 结构: productInfo 嵌套模式
        seller_info = item.get('sellerInfo', {})
        product_info = item.get('productInfo', {})
        
        # 备选：如果不是嵌套结构，则回退到根目录
        if not product_info:
            product_info = item
            
        shop_name = seller_info.get('shopTitle') or item.get('shopName') or '淘宝卖家'
        title = product_info.get('title') or item.get('title') or '未知商品'
        item_id = get_item_id_from_url(target_url)
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        current_data = []
        
        # 提取规格列表 (优先从 options 读取)
        skus = product_info.get('options') or item.get('skus') or item.get('options') or []
        
        if skus and isinstance(skus, list):
            for sku in skus:
                spec_name = sku.get('name') or sku.get('title') or '规格'
                raw_price = sku.get('price')
                
                # 价格清洗
                price = 0.0
                try:
                    if isinstance(raw_price, (int, float)):
                        price = float(raw_price)
                    else:
                        # 兼容字符串中包含符号的情况，如 "$38.1" 或 "¥173"
                        price_match = re.search(r"(\d+(\.\d+)?)", str(raw_price))
                        if price_match: price = float(price_match.group(1))
                except:
                    price = 0.0
                
                if price <= 0:
                    try:
                        price_match = re.search(r"(\d+(\.\d+)?)", str(product_info.get('price', 0)))
                        price = float(price_match.group(1)) if price_match else 0.0
                    except: price = 0.0
                
                current_data.append({
                    "获取时间": now_str,
                    "店铺名称": shop_name,
                    "商品ID": item_id,
                    "商品名称": title,
                    "商品规格": spec_name,
                    "当前价格": price,
                    "原价": price, # Pizani 有时不返回原价，默认与现价一致
                    "库存": 99,   # 通用爬虫可能不返回精确库存，设为占位符
                    "URL编号": 0,
                    "是否下架": 0
                })
        else:
            # 单规格处理
            try:
                price_match = re.search(r"(\d+(\.\d+)?)", str(product_info.get('price', 0)))
                price = float(price_match.group(1)) if price_match else 0.0
            except: price = 0.0
            
            current_data.append({
                "获取时间": now_str,
                "店铺名称": shop_name,
                "商品ID": item_id,
                "商品名称": title,
                "商品规格": "默认规格",
                "当前价格": price,
                "原价": price,
                "库存": 99,
                "URL编号": 0,
                "是否下架": 0
            })
            
        print(f">>> [Apify 成功] 目标:{target_url} | 截获条目:{len(current_data)} | 样价:{current_data[0]['当前价格'] if current_data else 0}")
        return current_data

    except Exception as e:
        print(f">>> [Apify 异常] 连通或解析失败: {e}")
        return []

def fetch_item_data(item_id, api_key, api_secret, mapping=None):
    """请求 API 获取单个商品的明细（店铺、名称、各规格 优惠价格 + 库存），支持动态映射提取"""
    if mapping is None:
        mapping = {}
    params = {
        'num_iid': item_id,
        'key': api_key,
        'secret': api_secret,
        'is_promotion': 1
    }
    try:
        response = requests.get(API_URL, params=params, timeout=15)
        data = response.json()
        if 'item' not in data:
            print(f"API请求失败或商品不存在 [ID:{item_id}]: {data.get('reason', data)}")
            return []
            
        item = data['item']
        shop_name = item.get('nick') or ''
        # 尝试从 seller_info 获取店铺名
        seller_info = item.get('seller_info', {})
        if not shop_name or shop_name == '-1':
            shop_name = seller_info.get('shop_name') or seller_info.get('nick') or '未知店铺'
        if shop_name == '-1':
            shop_name = '未知店铺'
        item_title = item.get('title', '未知商品')
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        
        # 提取商品级全量键值对
        ext_fields = {}
        for k, v in item.items():
            if not isinstance(v, (dict, list)):
                ext_fields[k] = v
                
        result = []
        skus_data = item.get('skus', {})
        if skus_data and 'sku' in skus_data and len(skus_data['sku']) > 0:
            for sku in skus_data['sku']:
                prop_name = sku.get('properties_name', '默认规格').split(':')[-1]
                
                # 捏合该 SKU 的全量键值对
                merged_fields = ext_fields.copy()
                for k, v in sku.items():
                    if not isinstance(v, (dict, list)):
                        merged_fields[f"sku_{k}"] = v
                
                row = {
                    "获取时间": now_str,
                    "店铺名称": shop_name,
                    "商品ID": item_id,
                    "商品名称": item_title,
                    "商品规格": prop_name
                }
                
                main_price = None
                
                # 如果用户在界面选定了探针规则，覆盖提取
                if mapping:
                    for mapping_key, config in mapping.items():
                        label = config.get("label", mapping_key)
                        val = merged_fields.get(mapping_key, "")
                        row[label] = val
                        if config.get("is_main"):
                            try:
                                main_price = float(val) if val else 0.0
                            except (ValueError, TypeError):
                                pass
                                
                if main_price is not None:
                    row["当前价格"] = main_price
                else:
                    # 价格优先级：promotion_price > discount_price > total_price > price
                    total_price = sku.get('total_price', 0)
                    promo_price = sku.get('promotion_price', 0)
                    discount_price = sku.get('discount_price', 0)
                    sku_price = sku.get('price', 0)
                    orginal_price = sku.get('orginal_price', 0)
                    
                    if promo_price and float(promo_price) > 0:
                        final_price = float(promo_price)
                    elif discount_price and float(discount_price) > 0:
                        final_price = float(discount_price)
                    elif total_price and float(total_price) > 0:
                        final_price = float(total_price)
                    else:
                        final_price = float(sku_price) if sku_price else 0.0
                    
                    print(f"[DEBUG] extracted specs: {prop_name} - price:{final_price} (org:{orginal_price}, promo:{promo_price}, total:{total_price})")
                    row["原价"] = float(orginal_price) if orginal_price else 0.0
                    row["当前价格"] = final_price
                    row["库存"] = int(sku.get('quantity', 0))
                
                result.append(row)
        else:
            # 单规格商品
            merged_fields = ext_fields.copy()
            row = {
                "获取时间": now_str,
                "店铺名称": shop_name,
                "商品ID": item_id,
                "商品名称": item_title,
                "商品规格": "默认统一规格"
            }
            main_price = None
            if mapping:
                for mapping_key, config in mapping.items():
                    label = config.get("label", mapping_key)
                    val = merged_fields.get(mapping_key, "")
                    row[label] = val
                    if config.get("is_main"):
                        try:
                            main_price = float(val) if val else 0.0
                        except (ValueError, TypeError):
                            pass
                            
            if main_price is not None:
                row["当前价格"] = main_price
            else:
                promo_price = item.get('promotion_price', 0)
                discount_price = item.get('discount_price', 0)
                total_price = item.get('total_price', 0)
                item_price = item.get('price', 0)
                
                if promo_price and float(promo_price) > 0:
                    final_price = float(promo_price)
                elif discount_price and float(discount_price) > 0:
                    final_price = float(discount_price)
                elif total_price and float(total_price) > 0:
                    final_price = float(total_price)
                else:
                    final_price = float(item_price) if item_price else 0.0
                    
                print(f"[DEBUG] extracted single item - price:{final_price} (org:{item.get('orginal_price', 0)}, promo:{promo_price}, total:{total_price})")
                row["原价"] = float(item.get('orginal_price', 0))
                row["当前价格"] = final_price
                row["库存"] = int(item.get('num', 0))
            
            result.append(row)
            
        return result
    except Exception as e:
        print(f"提取商品 [ID:{item_id}] 详细数据期间出错: {e}")
        return []

def fetch_item_data_tbk(item_id, tbk_key, tbk_secret):
    """请求淘宝客API获取商品基础信息和最低价"""
    url = 'http://gw.api.taobao.com/router/rest'
    params = {
        'method': 'taobao.tbk.item.info.get',
        'app_key': tbk_key,
        'sign_method': 'md5',
        'timestamp': time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        'format': 'json',
        'v': '2.0',
        'num_iids': str(item_id)
    }
    
    sorted_params = sorted(params.items(), key=lambda x: x[0])
    query = tbk_secret
    for k, v in sorted_params:
        query += str(k) + str(v)
    query += tbk_secret
    
    m = hashlib.md5()
    m.update(query.encode('utf-8'))
    params['sign'] = m.hexdigest().upper()
    
    try:
        r = requests.post(url, data=params, timeout=15)
        data = r.json()
        if 'error_response' in data:
            reason = data['error_response'].get('sub_msg', data['error_response'].get('msg'))
            print(f"淘宝客API请求出错或非推广商品 [ID:{item_id}]: {reason}")
            return None # Return None means fallback
            
        results = data.get('tbk_item_info_get_response', {}).get('results', {}).get('n_tbk_item', [])
        if not results:
            print(f"淘宝客API无数据返回 [ID:{item_id}]")
            return None
            
        item = results[0]
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        row = {
            "获取时间": now_str,
            "店铺名称": item.get('nick', '未知店铺'),
            "商品ID": item_id,
            "商品名称": item.get('title', '未知商品'),
            "商品规格": "默认统一规格(淘宝客)"
        }
        
        reserve_price = float(item.get('reserve_price', 0))
        zk_final_price = float(item.get('zk_final_price', 0))
        
        row["原价"] = reserve_price
        row["当前价格"] = zk_final_price if zk_final_price > 0 else reserve_price
        row["库存"] = 0
        
        return [row]
    except Exception as e:
        print(f"提取淘宝客API数据出错 [ID:{item_id}]: {e}")
        return None

def send_pushplus_alert(alerts, config):
    """将价格异动情况发送至微信（支持富文本模板和变量替换）"""
    if not isinstance(config, dict):
        # 降级处理：尝试重新读取配置
        try:
            from monitor import CONFIG_FILE
            import json
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except:
            return "配置加载失败"
            
    token = config.get("pushplus_token", "").strip()
    if not alerts or not token:
        return "未配置推送Token，跳过通知"
        
    url = 'http://www.pushplus.plus/send'
    custom_template = config.get("custom_push_template", "")
    
    # --- [动态模板引擎] ---
    if custom_template:
        import re
        try:
            # 容错：如果用户没写循环标签但想自定义，我们自动帮他包裹
            if "{{item_loop_start}}" not in custom_template:
                custom_template = "{{item_loop_start}}" + custom_template + "{{item_loop_end}}"
                
            # 提取循环体
            loop_match = re.search(r'\{\{item_loop_start\}\}(.*?)\{\{item_loop_end\}\}', custom_template, re.DOTALL)
            if loop_match:
                loop_content = loop_match.group(1)
                all_items_html = ""
                for alert in alerts:
                    item_html = loop_content
                    
                    # 准备数据
                    shop = str(alert.get('店铺名称', ''))
                    title = str(alert.get('商品名称', ''))
                    spec = str(alert.get('商品规格', ''))
                    old_p = str(alert.get('旧价格', ''))
                    new_p = str(alert.get('新价格', ''))
                    diff = alert.get('变化幅度', 0)
                    diff_str = f"{'+' if diff > 0 else ''}{diff:.2f}"
                    diff_color = "#ef4444" if diff > 0 else "#10b981" # 红涨绿跌
                    
                    # 使用正则进行超强鲁棒替换（甚至允许标签内部被 HTML 碎片切断）
                    def robust_replace(tpl, var_name_raw, value):
                        tag_p = r'(?:<[^>]+>)*'
                        def build_p(s):
                            return tag_p.join([re.escape(c) for c in s])
                        
                        if "|" in var_name_raw:
                            sub_parts = var_name_raw.strip("()").split("|")
                            interspersed = "(?:" + "|".join([build_p(p) for p in sub_parts]) + ")"
                        else:
                            interspersed = build_p(var_name_raw)

                        pattern = r'\{\{\s*' + tag_p + interspersed + tag_p + r'\s*\}\}'
                        return re.sub(pattern, str(value), tpl, flags=re.IGNORECASE)

                    item_html = robust_replace(item_html, "店铺名称", shop)
                    item_html = robust_replace(item_html, "商品名称", title)
                    item_html = robust_replace(item_html, "规格|规格名称", spec)
                    item_html = robust_replace(item_html, "旧价格", old_p)
                    item_html = robust_replace(item_html, "新价格|当前价格", new_p)
                    
                    # 幅度（带颜色）
                    colored_diff = f'<span style="color:{diff_color}; font-weight:bold;">{diff_str}</span>'
                    item_html = robust_replace(item_html, "变动幅度|变化幅度", colored_diff)
                    
                    item_html = robust_replace(item_html, "趋势图标", "📈" if diff > 0 else "📉")
                    item_html = robust_replace(item_html, "趋势颜色", diff_color)
                    all_items_html += item_html
                
                # 替换回主模板
                final_content = custom_template.replace(loop_match.group(0), all_items_html)
                # 替换全局变量 (检测时间)
                check_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                tag_p = r'(?:<[^>]+>)*'
                time_p = tag_p.join([re.escape(c) for c in "检测时间"])
                final_content = re.sub(r'\{\{\s*' + tag_p + time_p + tag_p + r'\s*\}\}', check_time, final_content)
                content = final_content
            else:
                content = custom_template # 理论上不会走到这里，因为上面已经包裹过
        except Exception as e:
            print(f"富文本模板渲染出错: {e}")
            content = f"模板渲染失败: {e}"
        
        # 智能判断模板类型 (如果包含 HTML 标签则使用 html 模板)
        template = "html" if ("<" in content and ">" in content) else "markdown"
    elif config.get("push_format", "html") == "html":
        # --- [系统默认表格模式] ---
        content = """
        <table border="1" cellspacing="0" cellpadding="5" style="border-collapse: collapse; width: 100%; font-size: 12px;">
            <tr style="background-color: #f2f2f2;">
                <th>店铺</th>
                <th>规格/价格变动</th>
            </tr>
        """
        for alert in alerts:
            diff = alert['变化幅度']
            trend_color = "#ef4444" if diff > 0 else "#10b981"
            trend_icon = "📈" if diff > 0 else "📉"
            content += f"""
            <tr>
                <td style="font-weight: bold;">{alert['店铺名称']}</td>
                <td>
                    <div style="font-size: 11px; color: #666;">{alert['商品名称']}</div>
                    <div style="margin-top: 4px;">
                        <span style="background: #f0f0f0; padding: 2px 4px; border-radius: 3px;">{alert['商品规格']}</span>
                    </div>
                    <div style="margin-top: 4px;">
                        <s>{alert['旧价格']}</s> → <b style="color: {trend_color};">{alert['新价格']}</b> 
                        ({trend_icon} {abs(diff):.2f})
                    </div>
                </td>
            </tr>
            """
        content += "</table>"
        content += f"<p style='font-size:11px; color:#999; margin-top:10px;'>检测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>"
        template = "html"
    else:
        # 传统 Markdown 模式
        content = ""
        for alert in alerts:
            diff = alert['变化幅度']
            trend = "涨价 📈" if diff > 0 else "降价 📉"
            content += f"🏠 **店铺**: {alert['店铺名称']}\n"
            content += f"📦 **商品**: {alert['商品名称']}\n"
            content += f"🏷️ **规格**: {alert['商品规格']}\n"
            content += f"📊 **变化**: 从 {alert['旧价格']} 元 变为 {alert['新价格']} 元\n"
            content += f"📉 **幅度**: {trend} {abs(diff):.2f} 元\n"
            content += "---------------------------\n"
        template = "markdown"

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Content-Type': 'application/json'
    }
    data = {
        "token": token,
        "title": "💰 淘宝监控发现价格异动！",
        "content": content,
        "template": template
    }
    
    print(f"[推送中心] 准备推送 (Token: {token[:4]}..., 长度: {len(content)})")
    
    try:
        response = requests.post(url, json=data, headers=headers, timeout=15)
        resp_data = response.json() if response.status_code == 200 else {}
        
        if response.status_code == 200 and resp_data.get('code') == 200:
            print(f"=== 已成功发送微信聚合通知 (PushPlus ID: {resp_data.get('data', 'N/A')}) ===")
            return "微信通知发送成功"
        else:
            reason = resp_data.get('msg', f'HTTP {response.status_code}')
            error_log = f"微信通知发送失败: {reason}"
            if response.status_code != 200:
                print(f"[错误] PushPlus 服务器返回 {response.status_code}: {response.text[:200]}")
            else:
                print(f"[业务错误] {error_log}")
            return error_log
            
    except Exception as e:
        err_msg = f"微信通知请求异常: {str(e)}"
        print(f"[异常] {err_msg}")
        return err_msg

def run_monitor_task(limit=None, status_callback=None, cancel_event=None):
    """执行监控任务，返回执行结果摘要字典。
    status_callback: 进度回调(url_idx, success, msg)"""
    config = load_config()
    logs = []
    result = {
        "success": False,
        "canceled": False,
        "sku_count": 0,
        "change_count": 0,
        "stock_change_count": 0,
        "notify_status": "",
        "changes": [],
        "stock_changes": [],
        "logs": logs,
        "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    logs.append(f"[{result['time']}] 触发执行监控器...")
    print(f"[{result['time']}] 触发执行监控器...")

    def is_cancelled():
        return cancel_event is not None and cancel_event.is_set()
    
    # --- 启动防检测时间抖动 (Jitter) ---
    import random
    import time 
    import os # Ensure os is imported
    if limit is None and os.environ.get("SKIP_JITTER") != "1": # 非手动调试模式下开启抖动
        # 如果是 VLM 模式，用户明确要求大幅度抖动（30分钟内随机）
        jitter_max = 30 if config.get("api_mode") == "vlm" else 15
        jitter_mins = random.uniform(0, jitter_max)
        jitter_msg = f"[防检测] 启用随机启动时间抖动 (Jitter): 将延迟 {jitter_mins:.2f} 分钟后开始..."
        print(jitter_msg)
        logs.append(jitter_msg)
        sleep_secs = int(jitter_mins * 60)
        for _ in range(sleep_secs):
            if is_cancelled():
                logs.append("任务在启动抖动阶段被终止")
                result["canceled"] = True
                return result
            time.sleep(1)
        logs.append(f"抖动结束，正式开始于: {datetime.now().strftime('%H:%M:%S')}")

    # (Original line 828 removed here)

    api_mode = config.get("api_mode", "onebound")

    # limit=0: 非 VLM 模式下的模拟测试
    if api_mode != 'vlm' and limit is not None and limit == 0:
        logs.append("模拟测试模式: 不调用API，使用模拟数据")
        result["success"] = True
        result["sku_count"] = 3
        result["change_count"] = 2
        result["stock_change_count"] = 1
        result["notify_status"] = "模拟模式，未实际发送通知"
        result["changes"] = [
            {"sku": "模拟规格A", "old": 100.00, "new": 88.00, "diff": -12.00},
            {"sku": "模拟规格B", "old": 200.00, "new": 220.00, "diff": 20.00}
        ]
        result["stock_changes"] = [
            {"sku": "模拟规格A", "old": 50, "new": 30, "diff": -20}
        ]
        return result
    
    urls = config.get("urls", [])
    if is_cancelled():
        logs.append("任务在开始前被终止")
        result["canceled"] = True
        return result
    
    if not urls and api_mode != 'vlm':
        print(">>> [警告] 目标嗅探池为空，任务终止。请先添加商品链接。")
        result["success"] = False
        result["msg"] = "目标池为空"
        return result
    
    # 针对 VLM 模式增加友好提示
    if not urls and api_mode == 'vlm':
        logs.append("[提示] 嗅探池为空，VLM 将按设置直接扫描手机端收藏夹。")
        
    api_key = config.get("api_key")
    api_secret = config.get("api_secret")
    pushplus_token = config.get("pushplus_token")
    
    api_mode = config.get("api_mode", "onebound")
    rapid_key = config.get("rapidapi_key", "")
    tmapi_token = config.get("tmapi_token", "")
    apify_token = config.get("apify_token", "")
    
    all_urls = config.get("urls", [])
    if limit is not None and limit > 0:
        if api_mode == 'vlm' and not all_urls:
            # VLM 模式下如果没填 URL，不显示“前 N 个链接”的混淆日志
            urls_to_fetch = []
        else:
            urls_to_fetch = all_urls[:limit]
            logs.append(f"限制模式: 仅处理前 {limit} 个链接 (总数 {len(all_urls)})")
    else:
        urls_to_fetch = all_urls

    current_data = []
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if api_mode in ['onebound', 'rapidapi', 'tmapi', 'apify']:
        logs.append(f"API 注入模式启动: {api_mode}")
        for i, url_obj in enumerate(urls_to_fetch, 1):
            mapping = {}
            url = ""
            if isinstance(url_obj, dict):
                url = url_obj.get("url", "")
                mapping = url_obj.get("mapping", {})
            else:
                url = url_obj
            
            if not url: continue
            item_id = get_item_id_from_url(url)
            
            try:
                if api_mode == 'onebound':
                    items_data = fetch_item_data(item_id, api_key, api_secret, mapping=mapping)
                elif api_mode == 'rapidapi':
                    items_data = fetch_item_data_rapidapi(item_id, rapid_key)
                elif api_mode == 'tmapi':
                    items_data = fetch_item_data_tmapi(item_id, tmapi_token)
                else: # apify
                    items_data = fetch_item_data_apify(url, apify_token)
                
                if items_data:
                    for d in items_data:
                        d['URL编号'] = i
                    current_data.extend(items_data)
                    logs.append(f"成功 [{i}/{len(urls_to_fetch)}] ID:{item_id} (API:{api_mode})")
                    if status_callback: status_callback(i, True, "成功")
                else:
                    logs.append(f"失败 [{i}/{len(urls_to_fetch)}] ID:{item_id}: 无数据返回")
                    if status_callback: status_callback(i, False, "无数据返回")
            except Exception as e:
                logs.append(f"异常 [{i}/{len(urls_to_fetch)}] ID:{item_id}: {str(e)}")
                if status_callback: status_callback(i, False, str(e))
            
            # API 模式下的频率保护
            time.sleep(1)
        
        # 标记 API 任务已完成，后续循环将跳过 batch_results 处理（因为 batch_results 为空）
        batch_results = []
    elif api_mode == 'vlm':
        # === [VLM 仿生视觉工作流] 基于手机 App 采集模式 ===
        logs.append("VLM 核心引擎加载中...")
        try:
            import importlib
            import adb_driver
            import screen_capture
            import vlm_navigator
            import data_extractor
            import workflow_agent
            
            # 强制执行级联重载
            importlib.reload(adb_driver)
            importlib.reload(screen_capture)
            importlib.reload(vlm_navigator)
            importlib.reload(data_extractor)
            importlib.reload(workflow_agent)
            
            from workflow_agent import TaobaoWorkflowAgent
            # 注入日志回调，让 Agent 的动作能实时显示在 Web 终端
            agent = TaobaoWorkflowAgent(
                status_callback=lambda msg: logs.append(msg),
                progress_callback=status_callback,
                cancel_event=cancel_event,
                workflow_steps=config.get("vlm_workflow_steps", []),
                coord_groups=config.get("vlm_coord_groups", {}),
            )

            scan_count = limit if limit is not None and limit > 0 else config.get("scan_item_count", 5)
            vlm_result = agent.scrape_all_favorites_randomly(max_items=scan_count)

            result["success"] = bool(vlm_result.get("success"))
            result["canceled"] = bool(vlm_result.get("canceled"))
            result["sku_count"] = int(vlm_result.get("sku_count", 0) or 0)
            result["change_count"] = 0
            result["stock_change_count"] = 0
            logs.append("VLM 采集流水已全部同步至 Excel。")
            if result["canceled"]:
                logs.append("VLM 任务已被用户终止。")
            if not result["success"]:
                logs.append("VLM 工作流未完成，请检查手机投屏、Gemini Key 或页面识别结果。")
            return result
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            print(f">>> [VLM 崩溃记录]\n{error_trace}")
            logs.append(f"❌ VLM 执行失败: {str(e)}")
            result["success"] = False
            return result
    else:
        # === [架构重构] 纯 Playwright 数字人引擎批量抓取 ===
        from playwright_engine import batch_fetch_via_browser
        show_browser = config.get("show_browser", False)
        print(f">>> [选项] 引擎切换模式: {api_mode} (可见模式: {show_browser})")
        
        pure_urls = []
        for url_obj in urls_to_fetch:
            if isinstance(url_obj, dict):
                u = url_obj.get("url")
                if u: pure_urls.append(u)
            else:
                if url_obj: pure_urls.append(url_obj)

        batch_results = batch_fetch_via_browser(pure_urls, show_browser=show_browser, status_callback=status_callback)
    
    # 接续处理 Playwright 结果（如有）
    for idx, res in enumerate(batch_results, 1):
        url_idx = idx
        item_id = get_item_id_from_url(res['url'])
        # 移除此处多余的 status_callback 调用，因为 batch_fetch_vial_browser 内部已经实时调用了
        if not res['success']:
            logs.append(f"失败 [{item_id}]: {res['msg']}")
            continue
            
        data = res['data']
        
        # >> 降级 DOM 解析分支
        if res.get("fallback") or data.get("is_fallback"):
            price_str = data.get("price_text", "0")
            try:
                price = float(price_str)
            except:
                price = 0.0
            current_data.append({
                "获取时间": now_str,
                "店铺名称": "未知归属(降级)",
                "商品ID": item_id,
                "商品名称": data.get("title", f"未知商品 {item_id}"),
                "商品规格": "纯视觉DOM提取(无SKU细分)",
                "当前价格": price,
                "原价": price,
                "库存": 0
            })
            logs.append(f"成功(DOM降级) [{item_id}]: {price} 元")
            continue
            
        # >> Mtop 官方全量结构解析分支
        try:
            # Mtop 的响应通常包在一个 "data" 字段里，如果存在且内部有 item，则解包一层
            inner_data = data.get('data', data) if 'data' in data and isinstance(data['data'], dict) and ('item' in data['data'] or 'skuBase' in data['data'] or 'skuCore' in data['data']) else data
            
            item_info = inner_data.get('item', {})
            seller_info = inner_data.get('seller', {})
            
            # SSR 模式下 item/seller 可能直接在 inner_data 根部，也可能在 property 里
            title = item_info.get('title') or inner_data.get('title') or f"未知商品 {item_id}"
            shop_name = seller_info.get('shopName') or seller_info.get('nick') or inner_data.get('shopName') or '未知店铺'
            
            sku_base = inner_data.get('skuBase', {})
            skus = sku_base.get('skus', [])
            props = sku_base.get('props', [])
            
            vid_to_name = {}
            for prop in props:
                for val in prop.get('values', []):
                    vid_to_name[str(val.get('vid'))] = val.get('name', '')
                    
            core_data = inner_data.get('core', {})
            sku2info = core_data.get('sku2info', {})
            
            # 关键修复：SSR 模式下 skuCore 直接在顶级或在 loaderData 里
            if not sku2info:
                # 尝试顶级 skuCore (SSR 常用)
                sku_core = inner_data.get('skuCore', {})
                sku2info = sku_core.get('sku2info', {})
            
            if not sku2info:
                # 尝试旧版 mockData
                mock_data = inner_data.get('mockData', "{}")
                if isinstance(mock_data, str):
                    try: mock_data = json.loads(mock_data)
                    except: mock_data = {}
                sku_mock = mock_data.get('skuCore', {}).get('sku2info', {})
                if sku_mock: sku2info = sku_mock
                
            if not sku2info:
                # 如果还是没拿到，记录一下以便排查
                print(f">>> [警告] 捕获到了数据包但无法解析价格 (sku2info 空): {item_id}")
                print(f">>> [调试] inner_data 所有的键: {list(inner_data.keys())}")
            else:
                print(f">>> [调试] 成功获得 sku2info，长度: {len(sku2info)}")

            if not skus:
                # 单规格处理
                price_info = sku2info.get('0', {})
                price_val = price_info.get('price', {}).get('priceText', '0')
                if not price_val or price_val == '0':
                    price_val = mock_data.get('price', {}).get('price', {}).get('priceText', '0')
                try: price_f = float(price_val.replace(',', ''))
                except: price_f = 0.0
                qty = price_info.get('quantity', 0)
                
                is_off_shelf = 0
                if "下架" in title or "不存在" in title:
                    is_off_shelf = 1

                current_data.append({
                    "获取时间": now_str, "店铺名称": shop_name, "商品ID": item_id, "商品名称": title,
                    "商品规格": "默认统一规格", "当前价格": price_f, "原价": price_f, "库存": int(qty),
                    "URL编号": url_idx, "是否下架": is_off_shelf
                })
                logs.append(f"成功(单规格) [{item_id}]: {price_f} 元")
            else:
                # 多规格处理
                for sku in skus:
                    sku_id = sku.get('skuId')
                    prop_path = sku.get('propPath', '')
                    
                    spec_names = []
                    for pair in prop_path.split(';'):
                        if ':' in pair:
                            vid = pair.split(':')[1]
                            if vid in vid_to_name:
                                spec_names.append(vid_to_name[vid])
                    spec_str = " ".join(spec_names) if spec_names else f"规格ID:{sku_id}"
                    
                    price_info = sku2info.get(str(sku_id), {})
                    # 探究多种可能的价格字段 (优先提取展示价、促销价、活动价)
                    price_val = (
                        price_info.get('price', {}).get('priceText') or 
                        price_info.get('extraPrice', {}).get('priceText') or
                        price_info.get('promotionPrice', {}).get('priceText') or
                        price_info.get('subPrice', {}).get('priceText') or 
                        price_info.get('priceMoney') or 
                        price_info.get('price', {}).get('price') or
                        '0'
                    )
                    qty = price_info.get('quantity', 0)
                    try: 
                        # 处理带逗号或￥的价格文本
                        clean_price = re.sub(r'[^\d\.]', '', str(price_val))
                        price_f = float(clean_price)
                    except: 
                        price_f = 0.0
                    
                    is_off_shelf = 0
                    if "下架" in title or "不存在" in title:
                        is_off_shelf = 1

                    current_data.append({
                        "获取时间": now_str, "店铺名称": shop_name, "商品ID": item_id, "商品名称": title,
                        "商品规格": spec_str, "当前价格": price_f, "原价": price_f, "库存": int(qty),
                        "URL编号": url_idx, "是否下架": is_off_shelf
                    })
                logs.append(f"成功(多规格) [{item_id}]: 截获 {len(skus)} 个细分变体")
                
        except Exception as parse_e:
            logs.append(f"解析异常 [{item_id}]: {str(parse_e)}")
            continue

    if not current_data:
        logs.append("全部链接抓取完毕，但均未获取到有效数据流，退出流转。")
        print("未获取到有效数据，退出。")
        return result
    
    result["sku_count"] = len(current_data)
    logs.append(f"共获取 {len(current_data)} 条SKU记录")
        
    df_current = pd.DataFrame(current_data)
    
    # 构建当前记录的指纹
    df_current['商品记录指纹'] = df_current['店铺名称'] + "|" + df_current['商品ID'].astype(str) + "|" + df_current['商品规格']
    
    df_history = pd.DataFrame()
    if os.path.exists(EXCEL_FILE):
        try:
            df_history = pd.read_excel(EXCEL_FILE)
            logs.append(f"读取历史记录: {len(df_history)} 条")
            print(f"成功读取历史数据引擎：已包含 {len(df_history)} 条历史时刻记录。")
        except Exception as e:
            logs.append(f"读取历史数据异常: {e}")
            print(f"读取历史数据异常，将会建立全新基准。 {e}")
    else:
        logs.append("无历史数据，本次为首次抓取基准")
            
    price_alerts = []
    stock_changes_log = []
    
    if not df_history.empty:
        # 补全历史的指纹方便分组
        if '商品记录指纹' not in df_history.columns:
            df_history['商品记录指纹'] = df_history['店铺名称'] + "|" + df_history['商品ID'].astype(str) + "|" + df_history['商品规格']
        if '获取时间' not in df_history.columns:
            df_history['获取时间'] = '1970-01-01 00:00:00'
        # 兼容旧数据：如果历史中没有"库存"列，补0
        if '库存' not in df_history.columns:
            df_history['库存'] = 0
        if '原价' not in df_history.columns:
            df_history['原价'] = 0
        
        df_history = df_history.sort_values(by='获取时间')
        latest_history = df_history.drop_duplicates(subset=['商品记录指纹'], keep='last')
        
        history_price_map = pd.Series(latest_history['当前价格'].values, index=latest_history['商品记录指纹']).to_dict()
        history_stock_map = pd.Series(latest_history['库存'].values, index=latest_history['商品记录指纹']).to_dict()
        
        for _, row in df_current.iterrows():
            fp = row['商品记录指纹']
            new_price = row['当前价格']
            new_stock = row['库存']
            
            # === 价格比对 ===
            if fp in history_price_map:
                old_price = history_price_map[fp]
                if old_price != new_price:
                    diff = new_price - old_price
                    price_alerts.append({
                        "店铺名称": row["店铺名称"],
                        "商品名称": row["商品名称"],
                        "商品规格": row["商品规格"],
                        "旧价格": old_price,
                        "新价格": new_price,
                        "变化幅度": diff
                    })
                    
            # === 库存比对（不触发微信通知）===
            if fp in history_stock_map:
                old_stock = int(history_stock_map[fp])
                if old_stock != int(new_stock):
                    stock_diff = int(new_stock) - old_stock
                    stock_changes_log.append({
                        "店铺名称": row["店铺名称"],
                        "商品名称": row["商品名称"],
                        "商品规格": row["商品规格"],
                        "旧库存": old_stock,
                        "新库存": int(new_stock),
                        "变化量": stock_diff
                    })

    result["change_count"] = len(price_alerts)
    result["stock_change_count"] = len(stock_changes_log)
    
    # === 价格变动 → 微信推送 ===
    if price_alerts:
        logs.append(f"发现 {len(price_alerts)} 个价格异动!")
        print(f"[!] 发现 {len(price_alerts)} 个价格异动!")
        for a in price_alerts:
            trend = "涨" if a['变化幅度'] > 0 else "降"
            result["changes"].append({
                "sku": str(a.get('商品规格', ''))[:30],
                "old": float(a['旧价格']),
                "new": float(a['新价格']),
                "diff": round(float(a['变化幅度']), 2)
            })
            try:
                print(f"  >> {a['商品名称'][:10]}... | {a['商品规格']} | {a['旧价格']} -> {a['新价格']}")
            except Exception:
                print(f"  >> (商品名含特殊字符) | {a['旧价格']} -> {a['新价格']}")
        notify_result = send_pushplus_alert(price_alerts, config)
        result["notify_status"] = notify_result or ""
        logs.append(f"通知状态: {result['notify_status']}")
    else:
        logs.append("未发现价格变化")
        result["notify_status"] = "无需通知"
        print("未发现任何规格存在较前一次的价格异常波动。")
    
    # === 库存变动 → 仅记录日志，不推送 ===
    if stock_changes_log:
        logs.append(f"发现 {len(stock_changes_log)} 个库存变动 (仅记录，不推送)")
        for s in stock_changes_log:
            direction = "增加" if s['变化量'] > 0 else "减少"
            result["stock_changes"].append({
                "sku": str(s.get('商品规格', ''))[:30],
                "old": int(s['旧库存']),
                "new": int(s['新库存']),
                "diff": int(s['变化量'])
            })
            try:
                print(f"  [库存] {s['商品名称'][:10]}... | {s['商品规格']} | {s['旧库存']} -> {s['新库存']} ({direction}{abs(s['变化量'])})")
            except Exception:
                pass
    else:
        logs.append("库存未发现变化")

    # 持久化
    df_current.drop(columns=['商品记录指纹'], inplace=True, errors='ignore')
    
    if "商品记录指纹" in df_history.columns:
        df_history.drop(columns=['商品记录指纹'], inplace=True, errors='ignore')
        
    if not df_history.empty:
        df_final = pd.concat([df_history, df_current], ignore_index=True)
    else:
        df_final = df_current
    
    # === 关键修复：防止因并发或逻辑瑕疵导致的记录重复 ===
    if not df_final.empty:
        # 基于时间戳、商品指纹去重，保留最新的一条。
        # 指纹是通过 店铺|ID|规格 构建的
        df_final['tmp_fp'] = df_final['店铺名称'].astype(str) + "|" + df_final['商品ID'].astype(str) + "|" + df_final['商品规格'].astype(str)
        count_before = len(df_final)
        df_final = df_final.drop_duplicates(subset=['获取时间', 'tmp_fp'], keep='last')
        df_final.drop(columns=['tmp_fp'], inplace=True, errors='ignore')
        count_after = len(df_final)
        if count_before > count_after:
            print(f">>> [去重] 存盘前检测并移除了 {count_before - count_after} 条重复冲突记录。")
        
    try:
        df_final.to_excel(EXCEL_FILE, index=False)
        logs.append(f"数据已保存至 {EXCEL_FILE}")
        print(f"[OK] 时间戳版本的新增价格记录已成功追加至 {EXCEL_FILE}")
    except Exception as e:
        logs.append(f"保存失败: {e}")
        print(f"[ERR] 追加写入 {EXCEL_FILE} 失败，可能文件被独占锁定: {e}")
    
    result["success"] = True
    return result

if __name__ == "__main__":
    run_monitor_task()
