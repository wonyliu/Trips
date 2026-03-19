import os
import cv2
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

class UIBoundingBox(BaseModel):
    found: bool = Field(description="是否找到了目标元素")
    ymin: int = Field(description="元素边界框的顶部y坐标(0-1000)，从上到下")
    xmin: int = Field(description="元素边界框的左侧x坐标(0-1000)，从左到右")
    ymax: int = Field(description="元素边界框的底部y坐标(0-1000)")
    xmax: int = Field(description="元素边界框的右侧x坐标(0-1000)")

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

def find_element(image_path, target_element_name):
    from PIL import Image
    pil_image = Image.open(image_path)
    prompt = f"Find the bounding box for the UI element: '{target_element_name}'. Return the coordinates scaled from 0 to 1000."
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[pil_image, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=UIBoundingBox,
            temperature=0.1
        )
    )
    return response.text

if __name__ == "__main__":
    import glob
    # Find the latest screen capture
    records = glob.glob("records/screen_*.png")
    if records:
        latest = max(records, key=os.path.getctime)
        print(f"Testing on {latest}")
        print("Finding '我的淘宝':", find_element(latest, "我的淘宝"))
    else:
        print("No image found.")
