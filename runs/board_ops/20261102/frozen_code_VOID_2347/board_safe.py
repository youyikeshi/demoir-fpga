"""
board_safe.py —— 板端 DMA 测试的**安全底座**

2026-10-30 事故：PL 往非法地址写 ⇒ SoC 硬挂 ⇒ 只能人工断电。
本模块把"挂了也没人能救"变成"挂了 15 秒自动重启"。

用法：
    with Watchdog(15):
        ... 任何会让 PL 访存的操作 ...
    主线程正常退出 ⇒ 看门狗被正常关闭；
    主线程卡死/SoC 挂死 ⇒ 没人喂狗 ⇒ 15 秒后硬件复位。

注意：喂狗必须由**独立的、不受 DMA 影响的**线程做，且主线程卡住时要停止喂狗。
这里用「主线程心跳」实现：主线程每完成一个阶段就 set 一次心跳时间戳，
喂狗线程发现心跳超过 timeout/2 没更新，就**停止喂狗**，让看门狗把板子拉回。
"""
import os, threading, time, atexit

WDT = "/dev/watchdog"


class Watchdog:
    def __init__(self, seconds=15, name="wdt"):
        self.seconds = int(seconds)
        self.name = name
        self.fd = None
        self._beat = time.time()
        self._stop = threading.Event()
        self._th = None
        self.armed = False

    def __enter__(self):
        try:
            self.fd = os.open(WDT, os.O_WRONLY)
        except Exception as e:
            print(f"[{self.name}] 打开 {WDT} 失败: {e}  —— 本次无看门狗保护！")
            return self
        # 设置超时（Linux watchdog 标准 ioctl）
        try:
            import fcntl, struct
            WDIOC_SETTIMEOUT = 0xC0045706
            fcntl.ioctl(self.fd, WDIOC_SETTIMEOUT, struct.pack('i', self.seconds))
            got = struct.unpack('i', fcntl.ioctl(self.fd, 0x80045705, b'\x00'*4))[0]
            self.seconds = int(got)
        except Exception as e:
            print(f"[{self.name}] 设超时失败（用内核默认）: {e}")
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()
        self.armed = True
        print(f"[{self.name}] 看门狗已武装，超时 {self.seconds}s —— "
              f"SoC 挂死会自动重启，不再需要人工断电")
        return self

    def beat(self):
        self._beat = time.time()

    def _loop(self):
        w = "*".encode()
        while not self._stop.is_set():
            # 心跳超时 ⇒ 主线程卡住 ⇒ 停止喂狗，交给硬件复位
            if time.time() - self._beat > self.seconds * 0.5:
                print(f"[{self.name}] 心跳超时，停止喂狗，{self.seconds}s 后板子自动复位")
                return
            try:
                os.write(self.fd, w)
            except Exception:
                return
            self._stop.wait(max(0.5, self.seconds / 4.0))

    def __exit__(self, *exc):
        self._stop.set()
        if self._th:
            self._th.join(timeout=2)
        if self.fd is not None:
            try:
                os.write(self.fd, b"V")   # magic close，正常关闭看门狗
                os.close(self.fd)
            except Exception:
                pass
        print(f"[{self.name}] 看门狗已正常关闭")
        return False
