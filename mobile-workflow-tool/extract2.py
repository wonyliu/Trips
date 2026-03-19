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
        if str(sku.get('price')) in ['198', '198.0', '183', '183.0'] or str(sku.get('orginal_price')) in ['198', '198.0', '183', '183.0']:
            print(f"Found match! SKU Props: {sku.get('properties_name')}")
            print(f"  Price: {sku.get('price')}, OrgPrice: {sku.get('orginal_price')}")
            print("-" * 40)
except Exception as e:
    print(f"Error: {e}")
