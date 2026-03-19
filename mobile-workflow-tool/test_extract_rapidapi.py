import json

def test_extract_rapidapi(data):
    # 下面模拟 monitor.py 里的解析逻辑
    try:
        res_obj = data.get('result', {})
        item_info = res_obj.get('item', {})
        seller_info = res_obj.get('seller', {})
        
        title = item_info.get('title', '未知商品')
        shop_name = seller_info.get('shop_title', '未知店铺')
        
        # 这里的关键是 sku_base 和 sku_props
        sku_base = item_info.get('sku_base', [])
        sku_props = item_info.get('sku_props', [])
        
        # 构建 vid 到 name 的映射
        vid_to_name = {}
        for prop in sku_props:
            for val in prop.get('values', []):
                vid_to_name[str(val.get('vid'))] = val.get('name', '')
                
        items_data = []
        import datetime
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        if not sku_base:
            # 处理单规格情况
            skus_summary = item_info.get('skus', {})
            price_val = skus_summary.get('price', '0')
            # 取价格范围中的最小值
            if '-' in str(price_val):
                price_val = price_val.split('-')[0].strip()
            
            try: price_f = float(str(price_val).replace(',', ''))
            except: price_f = 0.0
            
            qty = skus_summary.get('quantity', 0)
            
            items_data.append({
                "获取时间": now_str,
                "店铺名称": shop_name,
                "商品ID": item_info.get('num_iid', 'Unknown'),
                "商品名称": title,
                "商品规格": "默认统一规格",
                "当前价格": price_f,
                "原价": price_f,
                "库存": int(qty)
            })
        else:
            # 处理多规格情况
            for sku in sku_base:
                sku_id = sku.get('skuId')
                prop_path = sku.get('propPath', '')
                
                spec_names = []
                for pair in prop_path.split(';'):
                    if ':' in pair:
                        vid = pair.split(':')[1]
                        if vid in vid_to_name:
                            spec_names.append(vid_to_name[vid])
                spec_str = " ".join(spec_names) if spec_names else f"规格ID:{sku_id}"
                
                # 价格优先取 promotion_price，其次 price
                price_val = sku.get('promotion_price') or sku.get('price') or '0'
                try: price_f = float(str(price_val).replace(',', ''))
                except: price_f = 0.0
                
                qty = sku.get('quantity', 0)
                
                items_data.append({
                    "获取时间": now_str,
                    "店铺名称": shop_name,
                    "商品ID": item_info.get('num_iid', 'Unknown'),
                    "商品名称": title,
                    "商品规格": spec_str,
                    "当前价格": price_f,
                    "原价": price_f,
                    "库存": int(qty)
                })
        
        return items_data
    except Exception as e:
        print(f"解析报错: {e}")
        return []

# 测试数据（简化版，模拟用户提供的结构）
test_json = {
  "result": {
    "item": {
      "num_iid": "683660193174",
      "title": "Hourglass固体唇蜜",
      "sku_base": [
        {
          "skuId": "5069626467622",
          "propPath": "1627207:1673856283",
          "price": "269",
          "promotion_price": "269",
          "quantity": "5"
        }
      ],
      "sku_props": [
        {
          "pid": "1627207",
          "name": "颜色分类",
          "values": [
            {
              "vid": "1673856283",
              "name": "注意：用多少转多少"
            }
          ]
        }
      ]
    },
    "seller": {
      "shop_title": "香香猪呆呆GO"
    }
  }
}

results = test_extract_rapidapi(test_json)
for r in results:
    print(r)
