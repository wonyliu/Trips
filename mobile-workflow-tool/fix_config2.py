import json

CONFIG_FILE = 'config.json'

try:
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
    
    urls = config.get('urls', [])
    new_urls = []
    seen_urls = set()
    
    for item in urls:
        raw_url = ""
        if isinstance(item, str):
            if "http" not in item:
                continue # 过滤单纯的数字 id
            raw_url = item
        elif isinstance(item, dict):
            raw_url = item.get("url", "")
            
        if not raw_url or "http" not in raw_url:
            continue
            
        if raw_url not in seen_urls:
            seen_urls.add(raw_url)
            if isinstance(item, str):
                new_urls.append({"url": raw_url, "mapping": {}})
            else:
                new_urls.append(item)
            
    config['urls'] = new_urls
    
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
        
    print(f"Cleaned config.json. Remaining valid urls: {len(new_urls)}")
except Exception as e:
    print(f"Error: {e}")
