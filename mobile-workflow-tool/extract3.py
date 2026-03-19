import json
import ast
import sys

sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)

try:
    with open('api_test_new.json', 'r', encoding='gbk', errors='ignore') as f:
        content = f.read()
        try:
            data = json.loads(content)
        except:
            data = ast.literal_eval(content)
            
    # Search for any properties or prices containing 233
    skus = data.get('item', {}).get('skus', {}).get('sku', [])
    print("Searching for 233 in SKUs...")
    for sku in skus:
        if str(sku.get('price')) == '233' or str(sku.get('orginal_price')) == '233' or str(sku.get('promotion_price')) == '233':
            print(f"Match found for 233: {sku.get('properties_name')}")
            print(f"  Price: {sku.get('price')}")
            print(f"  OrgPrice: {sku.get('orginal_price')}")
            print(f"  total_price: {sku.get('total_price')}")
except Exception as e:
    print(f"Error: {e}")
