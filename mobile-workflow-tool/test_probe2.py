import requests, json

# 通过后端接口测试
resp = requests.post('http://127.0.0.1:5000/api/engine/test_url', json={
    "index": 1,
    "url": "https://item.taobao.com/item.htm?id=683660193174"
}, timeout=30)

data = resp.json()
print("=== Backend response ===")
print(f"success: {data.get('success')}")
print(f"msg: {data.get('msg', 'N/A')}")
print(f"base: {json.dumps(data.get('base', {}), ensure_ascii=False)[:200]}")
skus = data.get('skus', [])
print(f"skus count: {len(skus)}")
for i, s in enumerate(skus[:3]):
    print(f"  SKU #{i}: name={s.get('properties_name','?')[:60]}, raw_keys={list(s.get('raw_data',{}).keys())[:5]}")
