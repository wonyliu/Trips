import requests, json

# 直接调用万邦 API 看原始数据结构
config = json.load(open('config.json', 'r', encoding='utf-8'))
params = {
    'num_iid': '683660193174',
    'key': config['api_key'],
    'secret': config['api_secret'],
    'is_promotion': 1,
    'cache': 'no'
}

resp = requests.get('https://api-gw.onebound.cn/taobao/item_get_pro/', params=params, timeout=15)
data = resp.json()

item = data.get('item', {})

# 打印 item 的顶层 keys
print("=== item top-level keys ===")
for k, v in item.items():
    vtype = type(v).__name__
    if isinstance(v, (dict, list)):
        vlen = len(v) if isinstance(v, list) else len(v.keys())
        print(f"  {k}: {vtype}({vlen})")
    else:
        print(f"  {k}: {vtype} = {str(v)[:80]}")

# 看 skus 结构
skus_raw = item.get('skus', None)
print(f"\n=== item['skus'] type: {type(skus_raw).__name__} ===")
if isinstance(skus_raw, dict):
    print(f"  skus keys: {list(skus_raw.keys())}")
    sku_list = skus_raw.get('sku', None)
    print(f"  skus['sku'] type: {type(sku_list).__name__}")
    if isinstance(sku_list, list):
        print(f"  skus['sku'] count: {len(sku_list)}")
        if len(sku_list) > 0:
            print(f"  first sku keys: {list(sku_list[0].keys())}")
            print(f"  first sku sample: {json.dumps(sku_list[0], ensure_ascii=False)[:300]}")
    elif isinstance(sku_list, str):
        print(f"  skus['sku'] is a string: {sku_list[:200]}")
elif isinstance(skus_raw, list):
    print(f"  skus is a list with {len(skus_raw)} items")
    if len(skus_raw) > 0:
        print(f"  first item: {json.dumps(skus_raw[0], ensure_ascii=False)[:300]}")
elif isinstance(skus_raw, str):
    print(f"  skus is a string: {skus_raw[:200]}")
else:
    print(f"  skus raw value: {skus_raw}")

# 也看看 props_list / prop_imgs 等可能含规格信息的字段
for alt_key in ['props_list', 'prop_imgs', 'props', 'props_name', 'sku']:
    val = item.get(alt_key, '__MISSING__')
    if val != '__MISSING__':
        print(f"\n=== item['{alt_key}'] ===")
        if isinstance(val, (dict, list)):
            print(f"  type: {type(val).__name__}, preview: {json.dumps(val, ensure_ascii=False)[:300]}")
        else:
            print(f"  type: {type(val).__name__}, value: {str(val)[:300]}")
