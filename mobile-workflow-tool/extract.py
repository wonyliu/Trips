import json
import ast
import sys

# 修复 Windows 编码问题
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)

try:
    with open('api_test_new.json', 'r', encoding='gbk', errors='ignore') as f:
        content = f.read()
        try:
            data = json.loads(content)
        except:
            data = ast.literal_eval(content)
    
    skus = data.get('item', {}).get('skus', {}).get('sku', [])
    for sku in skus:
        prop = sku.get('properties_name', '')
        if '180' in prop or '105' in prop:
            print(f"SKU: {prop}")
            print(f"  price: {sku.get('price')}")
            print(f"  orginal_price: {sku.get('orginal_price')}")
            print(f"  total_price: {sku.get('total_price')}")
            if 'promotion_price' in sku:
               print(f"  promotion_price: {sku.get('promotion_price')}")
            print("-" * 40)
            
    print("Item level promos:")
    print(data.get('item', {}).get('promotion_price'))
except Exception as e:
    print(f"Error: {e}")
