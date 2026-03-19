import time
import json
import re
from playwright.sync_api import sync_playwright

def test_html_dump():
    url = "https://item.taobao.com/item.htm?id=640998957480"
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        print("Navigating...")
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        time.sleep(3) # Wait a bit for react rendering
        
        print("Dumping HTML...")
        html = page.content()
        with open("page_dump.html", "w", encoding="utf-8") as f:
            f.write(html)
            
        print("Checking for embedded mtop/sku data...")
        # Often it's in a window.__INITIAL_DATA__ or similar
        match = re.search(r'mtopjsonp\d+\((.*?)\)$', html, re.M | re.S)
        if match:
             print("Found mtopjson string in text")
             with open("embedded.json", "w", encoding="utf-8") as f:
                 f.write(match.group(1))
        else:
             print("No mtopjsonp found in regex.")
             
        browser.close()

if __name__ == "__main__":
    test_html_dump()
