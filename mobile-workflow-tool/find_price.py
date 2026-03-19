import json
import os

def check_json():
    with open('user_sample.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    found = []
    def search(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                new_path = f"{path}.{k}" if path else k
                if str(v) in ["173", "216", "173.0", "216.0"]:
                    found.append(f"{new_path}: {v}")
                search(v, new_path)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                search(item, f"{path}[{i}]")
    
    search(data)
    if found:
        print("FOUND MATCHES:")
        for f in found: print(f)
    else:
        print("No matches for 173 or 216 found in JSON.")

if __name__ == "__main__":
    check_json()
