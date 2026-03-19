import os
import cv2
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

class UIBoundingBox(BaseModel):
    found: bool = Field(description="是否找到了目标元素")
    ymin: int = Field(description="元素边界框的顶部y坐标(0-1000)，从上到下")
    xmin: int = Field(description="元素边界框的左侧x坐标(0-1000)，从左到右")
    ymax: int = Field(description="元素边界框的底部y坐标(0-1000)")
    xmax: int = Field(description="元素边界框的右侧x坐标(0-1000)")

class VLMNavigator:
    def __init__(self):
        print("正在初始化 Gemini VLM 导航代理...")
        self.api_key = os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
             print("[致命] 缺少 GEMINI_API_KEY")
             self.client = None
        else:
             self.client = genai.Client(api_key=self.api_key)

    def find_element_center(self, img_path_or_np, target_element_desc):
        """
        利用大模型在图像中寻找特定元素的坐标位置，返回 (x, y) 像素居中坐标。
        如果找不到返回 None
        """
        if not self.client:
             return None
             
        # 处理图像输入
        temp_path = "temp_nav_gemini.jpg"
        if isinstance(img_path_or_np, str):
            image_path = img_path_or_np
            # 获取原始图像尺寸以作后续换算
            img_cv = cv2.imread(image_path)
            if img_cv is None:
                print(f"[VLM 错误] 无法读取图像: {image_path}")
                return None
            orig_h, orig_w = img_cv.shape[:2]
        else:
            orig_h, orig_w = img_path_or_np.shape[:2]
            cv2.imwrite(temp_path, img_path_or_np)
            image_path = temp_path

        try:
            from PIL import Image
            pil_image = Image.open(image_path)
            
            prompt = f"""
            你是一个淘宝自动化辅助工具。请在下面这张App截图中寻找UI元素：'{target_element_desc}'。
            请严格按照 Pydantic schema 返回它的边界框。
            注意：返回的坐标必须是被缩小投射到 0-1000 区间的值。即左上角是(0,0)，右下角是(1000,1000)。
            """
            
            print(f"[VLM 寻路] 正在图像中查找: {target_element_desc} ...")
            response = self.client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[pil_image, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=UIBoundingBox,
                    temperature=0.1
                )
            )
            
            if image_path == temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
                
            import json
            if response.text:
                 data = json.loads(response.text)
                 if data.get("found"):
                     # Gemini 返回的是 0-1000 的千分比坐标
                     # 换算为图像的实际像素坐标
                     ymin, xmin, ymax, xmax = data["ymin"], data["xmin"], data["ymax"], data["xmax"]
                     
                     # 计算中心点
                     center_x_1000 = (xmin + xmax) / 2
                     center_y_1000 = (ymin + ymax) / 2
                     
                     real_x = int((center_x_1000 / 1000.0) * orig_w)
                     real_y = int((center_y_1000 / 1000.0) * orig_h)
                     
                     print(f" -> 找到锚点，换算真实像素坐标为: ({real_x}, {real_y})")
                     return (real_x, real_y)
                 else:
                     print(f" -> 未能找到元素: {target_element_desc}")
                     return None
            return None

        except Exception as e:
            print(f"[VLM 错误] 寻路失败: {e}")
            return None

    def find_text_center_by_keyword(self, img_path_or_np, keyword_pattern):
        if not self.client:
             return None

        temp_path = "temp_nav_keyword_gemini.jpg"
        if isinstance(img_path_or_np, str):
            image_path = img_path_or_np
            img_cv = cv2.imread(image_path)
            if img_cv is None:
                print(f"[VLM 错误] 无法读取图像: {image_path}")
                return None
            orig_h, orig_w = img_cv.shape[:2]
        else:
            orig_h, orig_w = img_path_or_np.shape[:2]
            cv2.imwrite(temp_path, img_path_or_np)
            image_path = temp_path

        try:
            from PIL import Image
            import json

            pil_image = Image.open(image_path)
            prompt = f"""
            你是一个手机界面文本定位助手。
            请在截图中查找和关键字模式 `{keyword_pattern}` 匹配的文字区域。

            规则：
            1. `*` 表示任意字符，可跨空格，例如 `共*款` 可以匹配 `共2款`、`共 12 款`。
            2. 只返回截图里真实可见的文字，不要猜测。
            3. 如果有多个匹配，优先返回最像可点击入口的那个。
            4. 请返回该文字区域的边界框。

            请严格按 Pydantic schema 返回，坐标使用 0-1000 缩放坐标。
            """

            print(f"[VLM 关键字定位] 正在查找: {keyword_pattern}")
            response = self.client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[pil_image, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=UIBoundingBox,
                    temperature=0.0
                )
            )

            if response.text:
                data = json.loads(response.text)
                if data.get("found"):
                    ymin, xmin, ymax, xmax = data["ymin"], data["xmin"], data["ymax"], data["xmax"]
                    center_x_1000 = (xmin + xmax) / 2
                    center_y_1000 = (ymin + ymax) / 2
                    real_x = int((center_x_1000 / 1000.0) * orig_w)
                    real_y = int((center_y_1000 / 1000.0) * orig_h)
                    print(f" -> 找到关键字中心点: ({real_x}, {real_y})")
                    return (real_x, real_y)

            print(f" -> 未找到关键字: {keyword_pattern}")
            return None
        except Exception as e:
            print(f"[VLM 错误] 关键字定位失败: {e}")
            return None
        finally:
            if image_path == temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    
    nav = VLMNavigator()
    # 找一下最近的一张图做测试
    import glob
    records = glob.glob("records/screen_*.png")
    if records:
        latest = max(records, key=os.path.getctime)
        print(f"在 {latest} 中寻找 '我的淘宝' 图标...")
        coord = nav.find_element_center(latest, "底部导航栏的 '我的淘宝' 按钮")
        print("最终坐标:", coord)
