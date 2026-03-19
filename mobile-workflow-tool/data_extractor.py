import os
import cv2
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

class ItemParseResult(BaseModel):
    title: str = Field(description="淘宝商品的完整标题名称，尽量完整提取。如果没有找到商品，填 '未找到商品'")
    price: float = Field(description="该商品当前展示的最高或最终售价，仅保留数字。如果没有找到价格，填 0")
    status: str = Field(description="当前页面的状态，如果是正常商品详情页或商品列表，填 'normal'；如果遇到滑块验证、红包弹窗、实名认证等遮挡型弹窗，填 'abnormal'")
    abnormal_reason: str = Field(description="只有当 status 为 abnormal 时填写，简要描述遇到了什么弹窗（如'签到红包', '滑块验证码'），正常则填空字符串")
    close_button_location: str = Field(description="只有当 status 为 abnormal 时填写，描述关闭按钮(X)大致在屏幕的哪个位置，比如 '右上角', '中下部'，正常则填空字符串")

class DataExtractor:
    def __init__(self):
        print("正在初始化 Gemini DataExtractor...")
        # 依赖于系统环境变量 GEMINI_API_KEY
        self.api_key = os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
             print("[警告] 未检测到 GEMINI_API_KEY 环境变量，AI 识别模块将无法工作！")
             self.client = None
        else:
             self.client = genai.Client(api_key=self.api_key)
        print("Gemini 初始化完成。")

    def extract_from_image(self, img_path_or_np):
        """
        向 Gemini 发送图像并要求按 Pydantic Schema 返回结构化 JSON 结果
        """
        if not self.client:
             print("缺少 Gemini API Key, 无法执行视觉提取。")
             return None
             
        # 需要先将 numpy 数组写入临时文件，或由于 gemini SDK 支持 pathlib.Path/File，直接传路径最稳妥
        temp_path = "temp_capture_for_gemini.jpg"
        if isinstance(img_path_or_np, str):
            image_path = img_path_or_np
        else:
            # 如果是 numpy 数组
            cv2.imwrite(temp_path, img_path_or_np)
            image_path = temp_path

        try:
            # 使用较新的 gemini-2.5-flash 获取极致的速度与性价比
            # 如果需要极限推理能力，可改用 gemini-2.5-pro
            model_name = "gemini-2.5-flash"
            
            # 使用 SDK 原生的 upload_file 或直接传 PIL Image
            from PIL import Image
            pil_image = Image.open(image_path)
            
            prompt = """
            你是一个针对电商商品页面的视觉提取AI。
            这是一张PC端窗口截取的手机画面。
            任务：
            1. 请寻找画面中当前被强调或者展示的**商品标题**（尽量选取字号较大或排在主要版块的）。如果你看到类似于唇釉的列表，就选取看起来像商品的完整文字段落。
            2. 提取该商品的**价格**（通常包含￥或者¥符号），请只提取纯数字。
            3. 如果画面中心弹出了干扰视线的模态框（红包、实名认证等遮挡型弹窗），请将 status 置为 abnormal，并在 close_button_location 中描述“X”关闭按钮的大致位置（如：右上角，右下角）。如果只是普通的商品展示或列表，置为 normal。
            """
            
            print("正在请求 Gemini Vision API 进行分析...")
            response = self.client.models.generate_content(
                model=model_name,
                contents=[pil_image, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ItemParseResult,
                    temperature=0.1
                )
            )
            
            # 清理临时文件
            if image_path == temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
                
            # 返回的是 Pydantic 模型自动转字典（或者原本就是 JSON 字符串的话，需要 json.loads）
            import json
            if response.text:
                 return json.loads(response.text)
            return None

        except Exception as e:
            print(f"[Gemini 错误] API 请求失败: {e}")
            return None

    def parse_taobao_item(self, ai_result):
        """
        兼容此前主控的格式包装接口
        """
        if not ai_result:
            return {"price": None, "title": None, "status": "error"}
            
        print(f"\n[AI 诊断] 状态: {ai_result.get('status')}")
        if ai_result.get("status") == "abnormal":
             print(f" -> 异常原因: {ai_result.get('abnormal_reason')}")
             print(f" -> 建议关闭按钮位置: {ai_result.get('close_button_location')}")
             
        return {
            "price": ai_result.get("price"),
            "title": ai_result.get("title"),
            "status": ai_result.get("status")
        }

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv() # 从根目录 .env 加载 key，便于测试
    
    extractor = DataExtractor()
    test_img = "test_capture.png"
    if os.path.exists(test_img) and extractor.client:
        res = extractor.extract_from_image(test_img)
        parsed = extractor.parse_taobao_item(res)
        print("提取结果:", parsed)
    else:
        print("未找到测试图像或 Key。")
