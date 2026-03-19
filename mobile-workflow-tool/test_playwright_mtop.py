import sys
from playwright_engine import fetch_item_data_via_browser
import json

def main():
    url = "https://item.taobao.com/item.htm?id=640998957480"
    print(f"Testing URL: {url}")
    res = fetch_item_data_via_browser(url, show_browser=True)
    
    with open("mtop_debug.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
        
    print("Dumped to mtop_debug.json")
    
    if res.get('success'):
        data = res.get('data', {})
        print("Success!")
        print(f"Fallback: {res.get('fallback')}")
        
if __name__ == "__main__":
    main()
