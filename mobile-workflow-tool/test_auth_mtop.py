import time
import json
import os
from playwright.sync_api import sync_playwright

AUTH_FILE = os.path.join(os.path.dirname(__file__), 'auth.json')

def _apply_stealth(context):
    stealth_js = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = { runtime: {} };
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en', 'en-US'] });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    """
    context.add_init_script(stealth_js)

def test_interception_urls():
    url = "https://item.taobao.com/item.htm?id=640998957480"
    urls = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=['--disable-blink-features=AutomationControlled'])
        context = browser.new_context(
            storage_state=AUTH_FILE,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        _apply_stealth(context)
        page = context.new_page()
        
        def handle_res(response):
            if "mtop" in response.url.lower() or "detail" in response.url.lower():
                urls.append(response.url)
                    
        page.on("response", handle_res)
        print("Navigating to item...")
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(5)
        
        with open("mtop_urls_auth.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(urls))
        print("Dumped to mtop_urls_auth.txt")
        browser.close()

if __name__ == "__main__":
    test_interception_urls()
