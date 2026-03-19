import subprocess
import time
import random
import re

class ADBDriver:
    def __init__(self, device_id=None, adb_path="adb"):
        self.device_id = device_id
        self.adb_path = adb_path
        self.screen_size = self._get_screen_size()
        if not self.screen_size:
             print("警告: 无法获取手机屏幕分辨率，请检查 ADB 连接。")

    def _run_adb_cmd(self, cmd):
        """执行 ADB 命令并返回输出"""
        base_cmd = [self.adb_path]
        if self.device_id:
            base_cmd.extend(["-s", self.device_id])
        
        full_cmd = base_cmd + cmd
        try:
            # print(f"Executing: {' '.join(full_cmd)}") # 调试用
            result = subprocess.run(full_cmd, capture_output=True, text=True, check=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            print(f"ADB 命令执行失败: {' '.join(full_cmd)}\n错误信息: {e.stderr}")
            return None

    def _get_screen_size(self):
        """获取物理设备屏幕分辨率"""
        output = self._run_adb_cmd(["shell", "wm", "size"])
        if output:
            # output 格式通常为: Physical size: 1080x2400
            match = re.search(r'Physical size: (\d+)x(\d+)', output)
            if match:
                width = int(match.group(1))
                height = int(match.group(2))
                return (width, height)
        return None

    def map_coordinates(self, pc_x, pc_y, pc_width, pc_height):
        """
        将 PC 端 scrcpy 窗口的相对坐标映射为手机物理坐标
        :param pc_x: 目标在 scrcpy 窗口内的 X 坐标
        :param pc_y: 目标在 scrcpy 窗口内的 Y 坐标
        :param pc_width: scrcpy 窗口的实际渲染宽度
        :param pc_height: scrcpy 窗口的实际渲染高度
        """
        if not self.screen_size:
            return None
            
        phys_width, phys_height = self.screen_size
        
        # 计算比例
        ratio_x = phys_width / pc_width
        ratio_y = phys_height / pc_height
        
        # 映射真实物理坐标 (注意边界限制)
        target_x = min(int(pc_x * ratio_x), phys_width - 1)
        target_y = min(int(pc_y * ratio_y), phys_height - 1)
        target_x = max(0, target_x)
        target_y = max(0, target_y)
        
        return (target_x, target_y)

    def tap(self, x, y):
        """点击指定的物理坐标"""
        # 添加一点随机偏移，防止完全精确的点按风控
        offset_x = random.randint(-5, 5)
        offset_y = random.randint(-5, 5)
        final_x = max(0, x + offset_x)
        final_y = max(0, y + offset_y)
        
        self._run_adb_cmd(["shell", "input", "tap", str(final_x), str(final_y)])

    def swipe(self, start_x, start_y, end_x, end_y, duration_ms=None):
        """
        执行滑动操作 (含贝塞尔/随机缓动逻辑防风控)
        """
        # 随机滑动时长，模拟人类不均匀的速度 (通常 300ms 到 1200ms)
        if duration_ms is None:
            duration_ms = random.randint(300, 1000)
            
        # 起点和终点加一点随机偏移
        sx = max(0, start_x + random.randint(-15, 15))
        sy = max(0, start_y + random.randint(-15, 15))
        ex = max(0, end_x + random.randint(-20, 20))
        ey = max(0, end_y + random.randint(-20, 20))
        
        self._run_adb_cmd(["shell", "input", "swipe", str(sx), str(sy), str(ex), str(ey), str(duration_ms)])

    def start_app(self, package_name="com.taobao.taobao", activity_name="com.taobao.tao.TBMainActivity"):
        """唤起目标 App"""
        self._run_adb_cmd(["shell", "am", "start", "-n", f"{package_name}/{activity_name}"])

    def stop_app(self, package_name="com.taobao.taobao"):
        """强杀目标 App"""
        self._run_adb_cmd(["shell", "am", "force-stop", package_name])

    def back(self):
        """执行返回键"""
        self._run_adb_cmd(["shell", "input", "keyevent", "4"])

    def get_foreground_package(self):
        """获取当前前台应用包名"""
        output = self._run_adb_cmd(["shell", "dumpsys", "window"])
        if not output:
            return None

        match = re.search(r"mCurrentFocus=.*? ([A-Za-z0-9_.]+)/", output)
        if match:
            return match.group(1)

        match = re.search(r"mFocusedApp=.*? ([A-Za-z0-9_.]+)/", output)
        if match:
            return match.group(1)

        return None

if __name__ == "__main__":
    print("测试 ADB 控制能力...")
    driver = ADBDriver()
    if driver.screen_size:
        print(f"成功获取设备分辨率: {driver.screen_size}")
        
        print("测试唤起淘宝...")
        driver.start_app()
        time.sleep(3)
        
        # 测试在屏幕中央稍微偏下位置进行上滑 (向下滚动列表)
        w, h = driver.screen_size
        start_x, start_y = w // 2, int(h * 0.7)
        end_x, end_y = w // 2, int(h * 0.3)
        
        print("测试滑动...")
        driver.swipe(start_x, start_y, end_x, end_y)
        
        # print("测试强杀淘宝 (取消注释后运行)...")
        # time.sleep(2)
        # driver.stop_app()
    else:
        print("ADB 未连接或获取分辨率失败。")
