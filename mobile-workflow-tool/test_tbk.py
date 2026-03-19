import requests
import hashlib
import time
import json

app_key = '35308218'
app_secret = '1c20b2c1f2ab3f7ae318e21d89c56c78'
url = 'http://gw.api.taobao.com/router/rest'

def get_sign(params, secret):
    # Sort parameters by key
    sorted_params = sorted(params.items(), key=lambda x: x[0])
    
    # Concatenate: secret + key1 + value1 + key2 + value2 ... + secret
    query = secret
    for k, v in sorted_params:
        query += str(k) + str(v)
    query += secret
    
    # Calculate md5
    m = hashlib.md5()
    m.update(query.encode('utf-8'))
    return m.hexdigest().upper()

def test_api():
    params = {
        'method': 'taobao.tbk.item.info.get',
        'app_key': app_key,
        'sign_method': 'md5',
        'timestamp': time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        'format': 'json',
        'v': '2.0',
        'num_iids': '683660193174'
    }
    
    sign = get_sign(params, app_secret)
    params['sign'] = sign
    
    print(f"Sending request with params: {params}")
    r = requests.post(url, data=params)
    print("Response Status Code:", r.status_code)
    try:
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))
    except Exception as e:
        print(r.text)

if __name__ == '__main__':
    test_api()
