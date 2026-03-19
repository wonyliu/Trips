import requests
import json

def debug_rapidapi():
    item_id = "683660193174"
    api_key = "fded1f8068mshc9857328c98b78ep1f8476jsn912f429eeb58"
    
    url = f"https://taobao-advanced.p.rapidapi.com/item_get"
    headers = {
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": "taobao-advanced.p.rapidapi.com"
    }
    params = {"num_iid": item_id}
    
    print(f">>> [RapidAPI 调用] 正在请求 ID: {item_id}")
    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)
        print(f"Status Code: {response.status_code}")
        data = response.json()
        
        # 保存完整 JSON 以供分析
        with open("rapidapi_full_dump.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        print("Success! Full JSON dumped to rapidapi_full_dump.json")
        
        # 简单搜索 173
        json_str = json.dumps(data)
        if "173" in json_str:
            print("FOUND '173' in the response! Analyzing location...")
            # 找到 173 出现的具体位置
        else:
            print("Did NOT find '173' in the raw response.")
            
    except Exception as e:
        print(f"Request failed: {e}")

if __name__ == "__main__":
    debug_rapidapi()
