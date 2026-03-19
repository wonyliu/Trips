import time
from playwright.sync_api import sync_playwright

def test_interception():
    url = "https://item.taobao.com/item.htm?id=640998957480"
    urls_seen = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        def handle_res(response):
            if "mtop" in response.url.lower():
                urls_seen.append(response.url)
                print(f"MTOP SEEN: {response.url}")
        
        page.on("response", handle_res)
        print("Navigating...")
        page.goto(url, wait_until="networkidle", timeout=30000)
        print("Done waiting.")
        
        with open("mtop_urls_dump.txt", "w") as f:
            f.write("\n".join(urls_seen))
            
        browser.close()

if __name__ == "__main__":
    test_interception()
