import requests
import json
import os

def test_rapidapi():
    config_file = 'config.json'
    if not os.path.exists(config_file):
        print("config.json not found")
        return

    with open(config_file, 'r', encoding='utf-8') as f:
        config = json.load(f)

    rapidapi_key = config.get('rapidapi_key')
    item_ids = ["683660193174", "742621117320"]
    
    for item_id in item_ids:
        print(f"\n--- 测试商品 ID: {item_id} ---")
        querystring = {"num_iid": item_id, "api": "item_detail", "area_id": "110100"}
        
        url = "https://taobao-advanced.p.rapidapi.com/api"
        headers = {
            "x-rapidapi-key": rapidapi_key,
            "x-rapidapi-host": "taobao-advanced.p.rapidapi.com"
        }

        try:
            response = requests.get(url, headers=headers, params=querystring, timeout=10)
            print(f"状态码: {response.status_code}")
            
            try:
                data = response.json()
                print(f"JSON Keys: {list(data.keys())}")
                if 'result' in data and 'status' in data['result']:
                    status = data['result']['status']
                    print(f"API 状态: {status.get('msg')} | {status.get('sub_code')}")
                
                if 'data' in data:
                    item = data['data'].get('item', {})
                    print(f"成功获取标题: {item.get('title')[:30]}...")
                    skus = data['data'].get('skuBase', {}).get('skus', [])
                    print(f"SKU 数量: {len(skus)}")
                    
                    # 保存其中一个成功的响应
                    output_file = f'rapidapi_response_{item_id}.json'
                    with open(output_file, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as je:
                print(f"解析失败: {je}")
        except Exception as e:
            print(f"请求异常: {e}")

if __name__ == "__main__":
    test_rapidapi()
