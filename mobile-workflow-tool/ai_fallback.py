import base64
import os
from openai import OpenAI

class AIFallback:
    def __init__(self, api_key=None, base_url=None):
        """
        初始化 OpenAI 客户端，用于处理 OCR 失败时的异常页面分析
        :param api_key: OpenAI API Key，若不传则尝试从环境变量读取
        :param base_url: 自定义 API 接口地址，若使用中转可填入
        """
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        
        if not self.api_key:
             print("警告: 未提供 OPENAI_API_KEY，AIFallback 模块已禁用。")
             self.client = None
             return
             
        # 可以通过 kwargs 传入自定义 base_url 等参数
        client_kwargs = {"api_key": self.api_key}
        if base_url:
             client_kwargs["base_url"] = base_url
             
        self.client = OpenAI(**client_kwargs)

    def _encode_image(self, image_path):
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')

    def analyze_exception_screen(self, image_path):
        """
        发送截图给 Vision API，让其判断当前处于什么页面，并给出建议操作（如关闭按钮大致位置）
        """
        if not self.client:
             return {"status": "error", "message": "未配置 API Key"}
             
        try:
            base64_image = self._encode_image(image_path)
            
            prompt = """
            请分析这张手机屏幕截图，我正在使用自动化脚本抓取淘宝商品价格，但本地 OCR 未能识别到有效信息。
            请告诉我：
            1. 屏幕上当前显示的是什么内容？（例如：商品详情页、搜索结果列表、弹出了促销广告、遇到滑块验证码、页面加载失败网络提示）
            2. 如果屏幕上有拦截正常操作的弹窗（如广告、提示框等），请评估其“关闭(X)”按钮、或者“取消/忽略”按钮大致在画面的哪个区域（如右上角、左上角、中下部）。
            3. 如果你能直接看到类似商品价格的数字，请告诉我。
            请简明扼要地回答。
            """

            response = self.client.chat.completions.create(
                model="gpt-4o",  # 使用支持视觉的模型
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=300
            )
            
            return {
                "status": "success",
                "analysis": response.choices[0].message.content
            }
        except Exception as e:
            print(f"调用 Vision API 失败: {e}")
            return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    print("测试 OpenAI Vision 兜底模块...")
    # 可以通过环境变量或这直接传 key 测试
    # os.environ["OPENAI_API_KEY"] = "sk-..."
    fallback = AIFallback()
    
    test_img = "test_capture.png"
    if fallback.client and os.path.exists(test_img):
         print("正在提交图像分析...")
         res = fallback.analyze_exception_screen(test_img)
         print(f"分析结果:\n{res.get('analysis', '未返回可用内容')}")
    else:
         print("系统未配置 API Key 或未找到测试截图，跳过演示。")
