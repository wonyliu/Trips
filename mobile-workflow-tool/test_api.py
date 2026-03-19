import requests
import json
url = "https://api-gw.onebound.cn/taobao/item_get/"
params = {
    'key': 't3426288262',
    'secret': '8262a3d6',
    'num_iid': '683660193174',
    'is_promotion': 1,
    'cache': 'no'
}
try:
    r = requests.get(url, params=params)
    print(json.dumps(r.json(), ensure_ascii=False, indent=2))
except Exception as e:
    print(e)
