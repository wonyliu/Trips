import os
import json
import re
import time
import random
from playwright.sync_api import sync_playwright

AUTH_FILE = os.path.join(os.path.dirname(__file__), 'auth.json')
DEBUG_SCREEN = os.path.join(os.path.dirname(__file__), 'debug_screen.png')

def _apply_stealth(context):
    """向浏览器上下文中注入抹除自动化指纹的JS脚本, 并设置逃避检测的基本属性"""
    stealth_js = """
    // 抹除 webdriver 特征
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    
    // 伪装语言和插件
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en', 'en-US'] });
    Object.defineProperty(navigator, 'plugins', { get: () => [
        { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer' },
        { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer' },
        { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer' }
    ] });

    // 伪装 WebGL 特征 (降低指纹一致性)
    const getParameter = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(type, attributes) {
        const ctx = getParameter.apply(this, arguments);
        if (type === 'webgl' || type === 'experimental-webgl') {
            const oldGetParameter = ctx.getParameter;
            ctx.getParameter = function(p) {
                if (p === 37445) return 'Intel Inc.'; // UNMASKED_VENDOR_WEBGL
                if (p === 37446) return 'Intel(R) Iris(R) Xe Graphics'; // UNMASKED_RENDERER_WEBGL
                return oldGetParameter.apply(this, arguments);
            };
        }
        return ctx;
    };
    """
    context.add_init_script(stealth_js)

def init_auth(keep_open=False):
    """
    弹出一个真实的 Chrome 浏览器，让用户扫码登录淘宝。
    一旦登录成功，则抽取全部指纹和 Cookie 本地保存。
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled']
        )
        context = browser.new_context()
        _apply_stealth(context)
        page = context.new_page()
        
        print(">>> 正在启动内置浏览器...")
        print(">>> 请在弹出的窗口中扫码或短信登录淘宝。")
        if keep_open:
             print(">>> [选项] 您启用了常驻模式。浏览器在登陆后将不会自动关闭，您可以在此窗口随心浏览！")
        else:
             print(">>> 登录成功后，程序会自动保存您的登陆凭证并自行关闭浏览器。请勿手动关闭。")
        
        page.goto('https://login.taobao.com/')
        
        try:
            # 登录状态下淘宝不再停留于 login.taobao.com
            page.wait_for_url(lambda url: "login.taobao.com" not in url, timeout=120000)
            print(">>> 检测到页面跳转，可能已登录成功！等待所有安全Cookie落盘...")
            page.wait_for_timeout(3000)
        except Exception as e:
            print(">>> [错误] 等待超期或未检测到登录成功跳转:", e)
            if not keep_open:
                browser.close()
            return False
            
        context.storage_state(path=AUTH_FILE)
        print(f">>> 恭喜！数字人登录凭证已签发保存至: {AUTH_FILE}")
        
        if keep_open:
             print(">>> [常驻模式] 凭证已保存，页面继续为您保留打开状态，您可以继续验证或者关闭页面。此控制台窗口将挂起直至您关闭该浏览器。")
             try:
                 page.wait_for_timeout(86400000) # 挂起24小时或者被主动关闭
             except:
                 pass
        
        browser.close()
        return True

def _extract_from_dom(page):
    """当网络包拦截失败时，尝试从网页 DOM 元素中硬抠标题和标价作为兜底"""
    data = {"is_fallback": True}
    try:
        data['title'] = page.title()
        
        # 1. 专门捕捉红色的活动价/立减价 CSS 选择器 (针对 2025/2026 淘宝 UI)
        price_locators = [
            # 官方立减/券后价常见类名
            "span[class*='Price--priceText']", 
            ".Price--priceText--2nLbVda",
            "span[class*='Price--promoPrice']",
            ".tb-rmb-num", 
            ".sys_displayPrice",
            "p[class*='price'] > span", # 广义价格选择器
            "div[class*='Price--']"     # 强力模糊匹配
        ]
        
        price_text = ""
        for loc in price_locators:
            try:
                elements = page.locator(loc).all()
                for el in elements:
                    if el.is_visible(timeout=500):
                        text = el.inner_text().strip()
                        # 清理掉人民币符号
                        text = re.sub(r'[^\d\.]', '', text)
                        if text and float(text) > 0:
                            price_text = text
                            print(f">>> [降级探针] 通过视觉选择器发现价格: {price_text}")
                            break
                if price_text: break
            except:
                continue
        
        # 2. 如果视觉选择器没中，全文本正则暴力搜索
        if not price_text:
             body_text = page.locator("body").inner_text()
             # 贪婪匹配优惠后的数字
             matches = re.findall(r'￥\s*([\d\.]+)', body_text)
             if matches:
                 # 通常页面上显示的最新活动价数值较小
                 valid_prices = [float(p) for p in matches if float(p) > 0]
                 if valid_prices:
                     price_text = str(min(valid_prices))
                     print(f">>> [降级探针] 通过全文本扫描发现疑似最低价: {price_text}")

        data['price_text'] = price_text
        return data
    except Exception as e:
        print("DOM Fallback 失败:", e)
        return {"is_fallback": True, "title": "页面解析失败", "price_text": ""}

def fetch_single_page(page, url, timeout_ms):
    """处理单个页面的抓取逻辑 (复用相同的 page 上下文)"""
    intercepted_data = {}
    
    def handle_response(response):
        # 拦截淘宝详情页加载时向 mtop 请求的内部全量数据流
        if "mtop.taobao.detail.getdetail" in response.url.lower():
            try:
                text = response.text()
                match = re.search(r'^\s*mtopjsonp\d+\((.*)\)\s*$', text)
                json_str = match.group(1) if match else text
                parsed = json.loads(json_str)
                intercepted_data['raw'] = parsed
                print(f">>> [底层探针] 已成功捕获到底层 mtop 核心数据！({url})")
            except Exception as e:
                pass

    page.on("response", handle_response)
    
    try:
        print(f">>> [数字人行动中] 悄悄空降商品页: {url}")
        
        # 拟人化延迟：进门前先“观望”
        page.wait_for_timeout(random.randint(1000, 2500))

        if "e.tb.cn" in url:
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        else:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        
        # --- 拟人化行为注入 (在等待拦截时进行) ---
        # 1. 模拟人类“犹豫”和“扫视”
        page.wait_for_timeout(random.randint(2000, 4000)) 
        
        # 2. 随机轻微滚动查看详情 (这是骗过风控的关键)
        for _ in range(random.randint(2, 4)):
            scroll_y = random.randint(300, 800)
            # 使用平滑滚动模拟手势
            page.evaluate(f"window.scrollBy({{top: {scroll_y}, behavior: 'smooth'}})")
            page.wait_for_timeout(random.randint(1500, 3000))

        # 等待 mtop 数据包
        start_time = time.time()
        while time.time() - start_time < (10.0): # 多等10秒
            if 'raw' in intercepted_data:
                break
            time.sleep(1.0)
            
        if 'raw' not in intercepted_data:
            # 尝试 SSR 探测 ( window.__ICE_APP_CONTEXT__ )
            try:
                js_code = "() => JSON.stringify(window.__ICE_APP_CONTEXT__?.loaderData?.home?.data?.res || null)"
                res_json_str = page.evaluate(js_code)
                if res_json_str and res_json_str != "null":
                    res_data = json.loads(res_json_str)
                    print(f">>> [SSR探针] 成功捕获到了页面底层的 SSR JSON 数据包！")
                    intercepted_data['raw'] = {"data": res_data}
            except:
                pass
                
        if 'raw' not in intercepted_data:
            page.screenshot(path=DEBUG_SCREEN, full_page=False)
            page_title = page.title()
            if "验证码" in page_title or "登录" in page_title or "安全" in page_title:
                 return {"success": False, "msg": f"淘宝已判定为异常流量，需扫码更新身份。截图已存。"}
            
            print(f">>> [降级行动] 数据包拦截落空。启动视觉 DOM 强行抓取...")
            fallback_data = _extract_from_dom(page)
            return {"success": True, "msg": "使用了页面视觉降级提取 (全量包缺失)", "data": fallback_data, "fallback": True}
                 
    except Exception as e:
        try:
             page.screenshot(path=DEBUG_SCREEN)
        except: pass
        return {"success": False, "msg": f"浏览器加载页面异常: {str(e)}"}
    finally:
        # 清除绑定的监听器，防止在批处理中互相影响
        page.remove_listener("response", handle_response)
        
    if 'raw' in intercepted_data:
        # 成功拿到官方数据包
        return {"success": True, "msg": "抓取并拦截成功", "data": intercepted_data['raw'], "fallback": False}
        
    return {"success": False, "msg": "进入了未知的失败分支。"}

def fetch_item_data_via_browser(url, timeout_ms=30000, show_browser=False):
    """
    （单步调用）使用无头其实浏览器和 Auth 凭证静默拦截请求获取商品全量数据。
    """
    if not os.path.exists(AUTH_FILE):
        return {"success": False, "msg": "系统尚未授权。请先点击[初始化身份与环境]生成鉴权文件。"}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not show_browser,
            args=['--disable-blink-features=AutomationControlled']
        )
        context = browser.new_context(
            storage_state=AUTH_FILE,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        _apply_stealth(context)
        page = context.new_page()
        res = fetch_single_page(page, url, timeout_ms)
        browser.close()
        return res

def batch_fetch_via_browser(urls, timeout_ms=30000, show_browser=False, status_callback=None):
    """
    （批量调度核心）启动一次浏览器生命周期，连续抓取多个商品，极大提升效率并绕过频繁启停的特征追踪。
    """
    if not os.path.exists(AUTH_FILE):
        return [{"url": u, "success": False, "msg": "系统尚未授权。"} for u in urls]
        
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not show_browser,
            args=['--disable-blink-features=AutomationControlled']
        )
        context = browser.new_context(
            storage_state=AUTH_FILE,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        _apply_stealth(context)
        page = context.new_page()
        
        for idx, url in enumerate(urls):
            res = fetch_single_page(page, url, timeout_ms)
            res['url'] = url
            results.append(res)
            
            # 实时回调状态
            if status_callback:
                status_callback(idx + 1, res['success'], res['msg'])
            
            # 显著延长并随机化休息时间，模仿人类阅读不同商品时的差异
            if idx < len(urls) - 1:
                import random
                sleep_time = random.uniform(5.0, 12.0) # 从 1-3 秒提升至 5-12 秒
                print(f">>> [拟人保护] 正在模拟“人类思考与阅读”，冷却 {sleep_time:.2f} 秒...")
                time.sleep(sleep_time)
                
        browser.close()
    return results

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'auth':
        init_auth()
    else:
        test_url = "https://item.taobao.com/item.htm?id=640998957480"
        print(">>> 启动无头游侠测试...")
        res = fetch_item_data_via_browser(test_url)
        if res.get('success'):
             data = res['data']
             print("抓取成功:", res['msg'])
        else:
             print("测试失败:", res['msg'])
