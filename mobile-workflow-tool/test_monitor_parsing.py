import unittest
from unittest.mock import patch, MagicMock
import json
import os
import sys

# 将当前目录加入路径以便导入 monitor
sys.path.append(os.getcwd())
import monitor

class TestMonitorParsing(unittest.TestCase):
    def test_rapidapi_parsing_new_structure(self):
        # 用户提供的真实 JSON
        mock_response_json = {
          "result": {
            "item": {
              "num_iid": "683660193174",
              "title": "Hourglass固体唇蜜唇釉口红",
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

        with patch('requests.get') as mock_get:
            mock_res = MagicMock()
            mock_res.status_code = 200
            mock_res.json.return_value = mock_response_json
            mock_get.return_value = mock_res
            
            # 执行解析
            results = monitor.fetch_item_data_rapidapi("683660193174", "fake_key")
            
            self.assertTrue(len(results) > 0)
            first = results[0]
            print(f"解析结果: {first}")
            self.assertEqual(first['商品ID'], "683660193174")
            self.assertEqual(first['当前价格'], 269.0)
            self.assertEqual(first['店铺名称'], "香香猪呆呆GO")
            self.assertEqual(first['商品规格'], "注意：用多少转多少")

if __name__ == '__main__':
    unittest.main()
