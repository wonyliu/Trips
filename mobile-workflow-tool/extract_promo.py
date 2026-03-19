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
            
    # 分析 promotions 部分
    promos = data.get('item', {}).get('promotions', {})
    print(json.dumps(promos, ensure_ascii=False, indent=2))
except Exception as e:
    print(f"Error: {e}")
