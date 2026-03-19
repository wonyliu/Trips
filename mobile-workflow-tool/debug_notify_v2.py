import os
import sys

# 将当前目录加入路径以便 import
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

from app import app
import json

with app.test_client() as client:
    print("Sending POST to /api/test/notify...")
    response = client.post('/api/test/notify')
    print(f"Status: {response.status_code}")
    try:
        data = response.get_json()
        if data:
            print(f"JSON Response: {json.dumps(data, indent=2, ensure_ascii=False)}")
        else:
            print(f"Raw Data: {response.data.decode('utf-8')[:500]}...")
    except Exception as e:
        print(f"Error parsing response: {e}")
        print(f"Raw Data: {response.data.decode('utf-8')[:500]}...")
