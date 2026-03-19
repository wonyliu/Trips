import requests
import json
import sys

# 修复 Windows GBK 终端无法输出
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', errors='replace', buffering=1)

api_key = "t3426288262"
api_secret = "8262a3d6"
item_id = "683660193174"

params = {
    'num_iid': item_id,
    'key': api_key,
    'secret': api_secret,
    'is_promotion': 1,
    'cache': 'no'
}

print("Fetching item_get_pro...")
response = requests.get('https://api-gw.onebound.cn/taobao/item_get_pro/', params=params)

with open('pro_result.json', 'w', encoding='utf-8') as f:
    json.dump(response.json(), f, ensure_ascii=False, indent=2)

print("Done, saved to pro_result.json")
