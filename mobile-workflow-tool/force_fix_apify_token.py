import json
import os

CONFIG_FILE = r'e:\Works\电商数据\淘宝价格监控系统\config.json'

def fix_config():
    if not os.path.exists(CONFIG_FILE):
        print("Config file not found.")
        return
        
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
        
    token = os.getenv("APIFY_API_TOKEN")
    if not token:
        print("APIFY_API_TOKEN is not set.")
        return
    config['apify_token'] = token
    config['api_mode'] = 'apify'
    
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    
    print("Successfully injected apify_token into config.json")
    print(f"Current keys: {list(config.keys())}")

if __name__ == "__main__":
    fix_config()
