from playwright_engine import fetch_item_data_via_browser
import json
import os

def check_real_price():
    url = "https://item.taobao.com/item.htm?id=683660193174"
    print(f">>> 正在启动浏览器探针，探测链接: {url}")
    
    # 模拟真实采集，获取数据包
    res = fetch_item_data_via_browser(url, timeout_ms=30000)
    
    if not res.get('success'):
        print(f"探测失败: {res.get('msg')}")
        return
    
    data = res.get('data', {})
    
    # 查找特定规格 #180 Rouse (可能是 ID 5248730345229)
    # 我们直接遍历数据包寻找 173
    json_str = json.dumps(data, ensure_ascii=False)
    if "173" in json_str:
        print("🎉 成功！在捕获的数据包中发现了 '173'！探针方案可行。")
    else:
        print("🙁 未在数据包中发现 '173'。")
        
    # 保存数据包以便详细分析价格层级
    output = "playwright_debug_res.json"
    with open(output, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"完整数据包已保存至: {output}")

if __name__ == "__main__":
    check_real_price()
