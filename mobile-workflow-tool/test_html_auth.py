import time
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

def dump_html_with_auth():
    url = "https://item.taobao.com/item.htm?id=640998957480"
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=['--disable-blink-features=AutomationControlled'])
        context = browser.new_context(
            storage_state=AUTH_FILE,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        _apply_stealth(context)
        page = context.new_page()
        
        print("Navigating to item...")
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(3) # Wait for execution
        html = page.content()
        
        with open("page_dump_auth.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("Done dumped to page_dump_auth.html")
        browser.close()

if __name__ == "__main__":
    dump_html_with_auth()
