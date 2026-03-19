import time
import os
import sqlite3
import cv2
from datetime import datetime

# 导入前置模块
from screen_capture import ScreenCapture
from adb_driver import ADBDriver
from data_extractor import DataExtractor

from dotenv import load_dotenv
load_dotenv()

class MainController:
    def __init__(self):
        print("初始化系统组件...")
        self.screencap = ScreenCapture("scrcpy")
        self.adb = ADBDriver(adb_path=r"E:\Works\电商数据\scrcpy-win64-v3.3.4\adb.exe")
        self.extractor = DataExtractor()
        self.db_conn = self._init_db()
        
    def _init_db(self):
        """初始化一个简易的本地 SQLite 数据库用于持久化监控数据"""
        db_path = "taobao_price_monitor.db"
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                price REAL,
                check_time TIMESTAMP,
                screenshot_path TEXT,
                status TEXT
            )
        ''')
        conn.commit()
        return conn

    def save_record(self, title, price, img_path, status="success"):
         cursor = self.db_conn.cursor()
         cursor.execute('''
             INSERT INTO price_history (title, price, check_time, screenshot_path, status)
             VALUES (?, ?, ?, ?, ?)
         ''', (title, price, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), img_path, status))
         self.db_conn.commit()

    def run_demo_loop(self):
        """
        运行一个演示级的自动化循环：
        假设当前手机已经停留在某个商品详情页列表（例如搜索结果页）
        """
        print("\n=== 起步检查 ===")
        if not self.screencap.find_window():
             print("致命错误: 找不到 scrcpy 窗口，请确认投屏已开启。")
             return
             
        if not self.adb.screen_size:
             print("致命错误: ADB 未连接或配置异常。")
             return
             
        # 激活窗口置顶
        if self.screencap.activate_window():
             time.sleep(1) # 给窗口置顶的动画留出时间
        
        # 截取当前主屏幕
        print("\n[步骤 1/3] 正在获取当前屏幕视图 (PC端抓屏)...")
        img = self.screencap.capture()
        if img is None:
             print("截图失败，流程终止。")
             return
             
        # 保存一张当前截图作为记录
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        img_path = f"records/screen_{timestamp}.png"
        os.makedirs("records", exist_ok=True)
        cv2.imwrite(img_path, img)
        print(f"视图已获取，保存至: {img_path}")
        
        print("\n[步骤 2/3] OCR 视觉解析...")
        start_time = time.time()
        # AI 视觉引擎处理，直接送全图，不再做裁剪（防止裁掉关键信息）
        ocr_result = self.extractor.extract_from_image(img)
        parsed_data = self.extractor.parse_taobao_item(ocr_result)
        
        print(f"数据解析完成 (耗时 {time.time()-start_time:.2f}s)：")
        print(f" -> 发现商品标题: {parsed_data.get('title', '未知')}")
        print(f" -> 提取价格: ￥{parsed_data.get('price', '未识别到')}")
        
        # 异常处理：模拟如果价格没拿到，需要走 AI 视觉大模型兜底
        if not parsed_data.get("price"):
             print("\n[警告] 常规 OCR 未能识别有效价格，可能处于弹窗或异常状态。")
             print("   >> 此时应调用 OpenAI Vision API 进行复核及纠错。")
             print("   >> TODO: 等待配置 OpenAI API Key 接入...")
             self.save_record("未知", 0.0, img_path, status="failed")
        else:
             # 持久化
             self.save_record(parsed_data.get('title'), parsed_data.get('price'), img_path, status="success")
             print("数据已写入本地 SQLite 数据库。")
             
        print("\n[步骤 3/3] 执行仿生滑动，准备下一个目标...")
        # 从屏幕中央往上滑动 (即浏览页面往下滚动)
        h, w = img.shape[:2]
        start_x, start_y = w // 2, int(h * 0.7)
        end_x, end_y = w // 2, int(h * 0.3)
        self.adb.swipe(start_x, start_y, end_x, end_y)
        print("滑动已发出，进入防风控冷却...")
        # 随机冷却 2~4 秒
        time.sleep(3)
        print("演示循环执行完毕！")


if __name__ == "__main__":
    print("=======================================")
    print(" 淘宝商品比价监控系统 - PC视觉版启动")
    print("=======================================")
    print("重要提示：请确保你此时已在电脑上启动了 scrcpy！\n")
    
    controller = MainController()
    
    # 执行一次演示跑通
    controller.run_demo_loop()
