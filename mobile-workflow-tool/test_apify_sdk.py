from apify_client import ApifyClient
import json
import os

token = os.getenv("APIFY_API_TOKEN")
if not token:
    raise RuntimeError("APIFY_API_TOKEN is not set")

client = ApifyClient(token)

run_input = { "product_url": "https://item.taobao.com/item.htm?id=683660193174" }

try:
    print(">>> 启动 Actor (pizani/taobao-product-scraper)...")
    # call() 会启动并等待任务完成
    run = client.actor("pizani/taobao-product-scraper").call(run_input=run_input)
    
    print(f">>> 启动成功! Run ID: {run['id']}")
    print(f">>> 状态: {run['status']}")
    
    print(">>> 正在抓取数据集结果...")
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    
    print(f">>> 抓取成功! 共有 {len(items)} 条数据。")
    if items:
        print(json.dumps(items[0], indent=2, ensure_ascii=False))
except Exception as e:
    print(f">>> 启动失败! 错误信息: {str(e)}")
