import cv2
from workflow_agent import TaobaoWorkflowAgent

def test_sku_parsing(image_path):
    print("加载图片:", image_path)
    img = cv2.imread(image_path)
    if img is None:
        print("图片读取失败")
        return
        
    agent = TaobaoWorkflowAgent()
    print("开始调用 VLM 解析阵列...")
    result = agent._vlm_extract_skus_from_image(img)
    
    print("=================== 解析结果 ===================")
    if result:
        print(f"主标题: {result.get('main_title')}")
        print(f"是否含有截断元素 (需要继续滑动): {result.get('has_more')}")
        skus = result.get("skus", [])
        print(f"共提取到完整 SKU 数量: {len(skus)}")
        for idx, sku in enumerate(skus):
            print(f"  [{idx+1}] 名称: {sku.get('name')} | 价格: {sku.get('price')}")
    else:
        print("解析失败返回 None")

if __name__ == "__main__":
    # 使用用户上传的包含多颜色的粉底液图片(图4)来测试
    # 因为该图在当前会话的 artifacts 中不可用本地直接读取，我将把该图复制到工作区
    test_sku_parsing(r"test_sku_image.png")
