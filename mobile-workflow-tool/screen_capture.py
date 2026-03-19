import pygetwindow as gw
import mss
import cv2
import numpy as np
import time
import re

class ScreenCapture:
    def __init__(self, window_title_keyword="scrcpy"):
        self.window_title_keyword = window_title_keyword
        self.window = None
        self.sct = mss.mss()

    def find_window(self):
        """查找设备的真实投屏窗口，而不是 scrcpy 的控制台黑框"""
        windows = gw.getAllWindows()
        
        # 1. 尝试匹配关键字
        for w in windows:
             title = w.title.strip()
             if not title: continue
             
             # 排除已知的非目标窗口
             title_lower = title.lower()
             if "scrcpy.exe" in title_lower or "cmd.exe" in title_lower or "powershell" in title_lower:
                  continue
             
             if self.window_title_keyword and self.window_title_keyword.lower() in title_lower:
                  self.window = w
                  return True

        # 1.5. 尝试匹配 Android 设备型号标题，例如 ELS-AN00
        for w in windows:
             title = w.title.strip()
             if not title:
                  continue

             title_lower = title.lower()
             if "scrcpy.exe" in title_lower or "cmd.exe" in title_lower or "powershell" in title_lower or "chrome" in title_lower:
                  continue

             if re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+", title):
                  self.window = w
                  return True
        
        # 2. 如果没找到，根据比例猜测
        for w in windows:
             title = w.title.strip()
             if not title: continue
             title_lower = title.lower()
             if "scrcpy.exe" in title_lower or "cmd.exe" in title_lower or "manager" in title_lower:
                  continue
             
             # 手机竖屏窗口高度通常明显大于宽度
             if w.height > w.width * 1.3 and w.height > 400:
                  print(f"尝试自动定位到形似手机的窗口: [{title}] (Size: {w.width}x{w.height})")
                  self.window = w
                  return True

        return False

    def activate_window(self):
        """激活并前置窗口"""
        if not self.window:
            self.find_window()
            
        if self.window:
            try:
                if self.window.isMinimized:
                    self.window.restore()
                self.window.activate()
                time.sleep(0.5) # 等待窗口动画完成
                return True
            except Exception as e:
                print(f"激活窗口失败: {e}")
                return False
        return False

    def get_window_rect(self):
        """获取窗口内容的坐标区域"""
        if not self.window:
            return None
            
        TITLE_BAR_HEIGHT = 32
        BORDER_WIDTH = 8
        
        left = self.window.left + BORDER_WIDTH
        top = self.window.top + TITLE_BAR_HEIGHT
        width = self.window.width - (BORDER_WIDTH * 2)
        height = self.window.height - TITLE_BAR_HEIGHT - BORDER_WIDTH
        
        if width <= 0 or height <= 0:
             return None

        return {
            "top": top,
            "left": left,
            "width": width,
            "height": height
        }

    def capture(self):
        """截取窗口图像"""
        if not self.window:
            if not self.find_window():
                print("未找到有效的 scrcpy 窗口，请确保已启动且未最小化。")
                return None
                
        rect = self.get_window_rect()
        if not rect:
            print("未能获取到有效的窗口坐标。")
            return None

        # 使用 mss 截图
        img_qbgra = self.sct.grab(rect)
        # 转换为 numpy 数组 (BGRA)
        img_np = np.array(img_qbgra)
        # 转换为 BGR
        img_bgr = cv2.cvtColor(img_np, cv2.COLOR_BGRA2BGR)
        return self._crop_phone_content(img_bgr)

    def _crop_phone_content(self, img_bgr):
        """裁掉 scrcpy 画面四周的黑边，统一坐标系到手机内容区域"""
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, 8, 255, cv2.THRESH_BINARY)
        points = cv2.findNonZero(mask)
        if points is None:
            return img_bgr

        x, y, w, h = cv2.boundingRect(points)
        img_h, img_w = img_bgr.shape[:2]

        # 只有检测到明显黑边时才裁切，避免深色页面被误裁
        has_side_padding = x > 10 or (x + w) < (img_w - 10)
        has_top_padding = y > 10 or (y + h) < (img_h - 10)
        if has_side_padding or has_top_padding:
            pad = 2
            x1 = max(0, x - pad)
            y1 = max(0, y - pad)
            x2 = min(img_w, x + w + pad)
            y2 = min(img_h, y + h + pad)
            return img_bgr[y1:y2, x1:x2]

        return img_bgr

if __name__ == "__main__":
    print("开始测试抓取 scrcpy 窗口...")
    cap = ScreenCapture("scrcpy")
    
    if cap.find_window():
        print(f"成功找到窗口: {cap.window.title}")
        img = cap.capture()
        if img is not None:
            cv2.imwrite("test_capture.png", img)
            print("截图成功，已保存至 test_capture.png")
        else:
            print("截图失败。")
    else:
        print("未找到窗口。")
