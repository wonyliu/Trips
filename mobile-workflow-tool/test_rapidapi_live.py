import requests
import json
import time
from datetime import datetime

def test_rapidapi_integration():
    # 从 config.json 读取真实的 Key 进行测试
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        rapidapi_key = config.get('rapidapi_key', '')
        if not rapidapi_key:
            print("❌ 未在 config.json 中找到 rapidapi_key，请先在界面设置并保存。")
            return

        # 测试商品 ID (用户提供的示例 ID 或常用 ID)
        item_id = "640998957480" 
        
        print(f"🚀 开始测试 RapidAPI 集成 [ID: {item_id}]...")
        
        # 模拟 monitor.py 中的 fetch_item_data_rapidapi 逻辑
        url = "https://taobao-advanced.p.rapidapi.com/api"
        querystring = {"num_iid": item_id, "api": "item_detail", "area_id": "110100"}
        headers = {
            "x-rapidapi-key": rapidapi_key,
            "x-rapidapi-host": "taobao-advanced.p.rapidapi.com"
        }
        
        start_time = time.time()
        response = requests.get(url, headers=headers, params=querystring, timeout=15)
        elapsed = time.time() - start_time
        
        print(f"⏱️ 响应耗时: {elapsed:.2f}s")
        
        data = response.json()
        if data.get('code') == 200:
            print("✅ 接口响应成功！")
            item = data.get('result', {}).get('item', {})
            print(f"🏷️ 商品标题: {item.get('title')}")
            print(f"🏪 店铺名称: {item.get('nick')}")
            
            skus = item.get('skus', {}).get('sku', [])
            print(f"📊 探测到 SKU 数量: {len(skus)}")
            
            for sku in skus[:3]: # 仅列出前 3 个
                print(f"   - 规格: {sku.get('properties_name')} | 价格: {sku.get('price')} | 库存: {sku.get('quantity')}")
            
            if len(skus) > 3: print("   ... (更多 SKU 已省略)")
        else:
            print(f"❌ 接口报错: {data.get('msg', '未知错误')}")
            
    except Exception as e:
        print(f"💥 测试发生异常: {e}")

if __name__ == "__main__":
    test_rapidapi_integration()
