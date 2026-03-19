import requests
import json

def debug_rapidapi():
    item_id = "683660193174"
    api_key = "fded1f8068mshc9857328c98b78ep1f8476jsn912f429eeb58"
    
    url = "https://taobao-advanced.p.rapidapi.com/api"
    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": "taobao-advanced.p.rapidapi.com"
    }
    
    # 尝试多种 API 模式
    api_modes = ["item_detail_new", "item_detail", "item_get"]
    
    for mode in api_modes:
        print(f"\n>>> [RapidAPI 调用] 模式: {mode}, ID: {item_id}")
        params = {"num_iid": item_id, "api": mode, "area_id": "110100"}
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
            print(f"Status Code: {response.status_code}")
            data = response.json()
            
            filename = f"rapidapi_{mode}_dump.json"
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            # 搜索 173
            json_str = json.dumps(data)
            if "173" in json_str:
                print(f"!!! FOUND '173' in {mode} response !!!")
            else:
                print(f"Did NOT find '173' in {mode} response.")
                
            # 打印一些关键价格字段看看
            result = data.get('result', {})
            item = result.get('item', {})
            print(f"Title: {item.get('title')}")
            print(f"Price: {item.get('price')}")
            print(f"Promotion Price: {item.get('promotion_price')}")
            
        except Exception as e:
            print(f"Request failed for {mode}: {e}")

if __name__ == "__main__":
    debug_rapidapi()
