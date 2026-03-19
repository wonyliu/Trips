import json

CONFIG_FILE = 'config.json'

try:
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
    
    urls = config.get('urls', [])
    updated = False
    new_urls = []
    
    for item in urls:
        if isinstance(item, str):
            new_urls.append({"url": item, "mapping": {}})
            updated = True
        else:
            new_urls.append(item)
            
    if updated:
        config['urls'] = new_urls
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        print("Config updated successfully. Restart your server if needed.")
    else:
        print("Config is already up to date.")

except Exception as e:
    print(f"Error: {e}")
