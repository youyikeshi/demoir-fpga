# 开发环境配置清单（可照着重现）

> 目标：换一台机器 / 换一个人，照着这份清单能把「MaixCAM 采集端 + 电脑接收端」跑起来。
> 实测环境：Ubuntu 22.04.5 LTS (x86_64) + MaixCAM Pro + ALINX AX7015B。

---

## 1. 总览

| 角色 | 硬件 | 系统 | 说明 |
|---|---|---|---|
| 开发/接收机 | x86_64 PC | Ubuntu 22.04.5 LTS | 跑接收端窗口、部署脚本 |
| 采集端 | MaixCAM Pro（SG2002） | Buildroot / Linux 5.10.4 | 跑 MaixPy 应用 |
| 目标板 | ALINX AX7015B（XC7Z015） | PetaLinux | 后续接视频（见 `docs/maixcam-to-zynq-video.md`） |

三者之间：MaixCAM 通过 **USB 虚拟网卡**（NCM + RNDIS）连到 PC，设备 IP 固定为 `10.13.148.1` / `10.13.149.1`。

---

## 2. 电脑端（Ubuntu 22.04）

### 2.1 系统与基础工具

| 项 | 值 / 命令 |
|---|---|
| OS | Ubuntu 22.04.5 LTS，x86_64（**不是 ARM！**） |
| glibc | 2.35 |
| 已装 | `git`、`curl`、`wget`、`python3`(3.10.12)、`xwd`、`wmctrl`、`x11` 桌面 |
| 未装（且不需要） | 系统 `ffmpeg`、`tkinter`、ImageMagick |

> ⚠️ 本机 **Docker Hub 直连被墙**，`/etc/docker/daemon.json` 里配了镜像源
> `dockerproxy.net`、`docker1ms.run`（MaixCDK 交叉编译用到）。

### 2.2 Python 接收端依赖（工作区自包含 venv，不需要 sudo）

```bash
WS=<工作区>
python3 -m venv --without-pip $WS/env/receiver
python3 -m pip install --target $WS/env/receiver/lib/python3.10/site-packages \
    opencv-python==4.10.0.84 numpy -i https://pypi.tuna.tsinghua.edu.cn/simple
```

| 包 | 版本 | 用途 |
|---|---|---|
| `opencv-python` | **4.10.0.84** | 自带 FFMPEG 后端解 RTSP + `imshow` 显示（Qt5） |
| `numpy` | 2.2.6 | 原始像素 reshape |

验证：

```bash
PYTHONPATH=$WS/env/receiver/lib/python3.10/site-packages python3 -c \
 "import cv2;print(cv2.__version__, 'FFMPEG' in cv2.videoio_registry.getBackendName(cv2.CAP_FFMPEG))"
```

> 说明：`python3 -m venv` 在本机缺 `ensurepip`，所以用 `--without-pip` + `--target`
> 装进 venv 的 site-packages；`run.sh` 会自动设 `PYTHONPATH`，不需要 activate。

### 2.3 部署脚本依赖（paramiko）

`apps/deploy.py` 用 SSH/SFTP 部署设备端，需要 `paramiko`：

```bash
python3 -m pip install --target $WS/env/maixpy-completion/lib/python3.10/site-packages \
    paramiko -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2.4 MaixVision（可选，IDE + 代码补全）

| 项 | 值 |
|---|---|
| 版本 | **1.2.3-12 amd64**（`dl.sipeed.com` 上 arm64/amd64 同名，**本机必须下 amd64**） |
| 安装位置 | `/opt/MaixVision`，命令 `maixvision`，桌面条目 `/usr/share/applications/maixvision.desktop` |
| 安装方式 | 浏览器下载 deb 后 `sudo apt install ./maixvision_1.2.3_amd64.deb` |

**代码补全环境**（MaixVision 内置 **pyright**，只做 `.pyi` 静态分析，不需要真的 import）：

```bash
python3 -m venv --without-pip $WS/env/maixpy-completion
python3 -m pip install --target $WS/env/maixpy-completion/lib/python3.10/site-packages \
    MaixPy -i https://pypi.tuna.tsinghua.edu.cn/simple     # 当前 4.12.5，含 59 个 .pyi
```

然后把 MaixVision 的 `editor.python` 指过去（`~/.config/maixvision/settings.json`）：

```json
"editor.python": "<工作区>/env/maixpy-completion/bin/python"
```

> ⚠️ **本机 `import maix` 必然失败**，属正常：
> `maix/_maix.so` 要求 **glibc ≥ 2.38**（本机 2.35）且缺 `libopencv_*.so.406`。
> 补全走 pyright 静态分析，不受影响；真要跑代码在设备上跑。

### 2.5 桌面快捷方式（可选）

```bash
cp apps/pc/maixcam-receiver.desktop ~/.local/share/applications/
chmod +x ~/.local/share/applications/maixcam-receiver.desktop
update-desktop-database ~/.local/share/applications
# 桌面上也可放一份：cp ... ~/Desktop/
```

`Exec` 里的 `--host` 写死了，换 IP 要改这个文件。

---

## 3. MaixCAM Pro 设备端

### 3.1 系统

| 项 | 实测值 |
|---|---|
| 型号 | MaixCAM Pro（`sys.device_id()` = `maixcam`） |
| SoC | 算能 SG2002（RISC-V，1GHz） |
| 系统 | **Buildroot**（不是 Ubuntu） |
| 内核 | `5.10.4-tag-` |
| MaixPy | **4.12.5** |
| 内存 | **128 MB total**（规格书写 256MB，Linux 侧只看到一半；available ≈ 80MB） |
| 屏幕 | **640×480** 触摸 |
| 摄像头 | GCORE_GC4653（4M，默认模组；最大 2560×1440） |
| 字体 | `/maixapp/share/font/SourceHanSansCN-Regular.otf`（**画中文必须用它**） |

### 3.2 访问方式

| 项 | 值 |
|---|---|
| USB 网卡 | NCM `enx…` = PC `10.13.148.100/24` → 设备 **`10.13.148.1`**<br>RNDIS `enx…` = PC `10.13.149.100/24` → 设备 **`10.13.149.1`** |
| mDNS | `maixcam-xxxx.local` |
| SSH | `root` / `root`，端口 22（OpenSSH 9.6） |
| 判断在线 | `lsusb` 有 `359f:2120 sipeed maixcam`；`ip -br addr` 有 `enx*` 网卡 |

> 网卡名每次插拔可能变，认 IP 不要认网卡名。

### 3.3 应用安装

设备端应用**不是**用 SSH 起的，而是装进设备菜单：

| 项 | 值 |
|---|---|
| 应用目录 | `/maixapp/apps/<app_id>/`（含 `main.py` + `app.yaml`） |
| 注册表 | `/maixapp/apps/app.info`（INI 格式，每节一个应用） |
| 安装命令 | `./deploy.py install`（自动备份 `app.info.bak-*`） |
| 生效条件 | **重启设备一次**（launcher 只在启动时读 app.info） |
| 运行方式 | 设备菜单点开 → launcher 让出屏幕/摄像头 → 应用运行 |
| 日志 | `/maixapp/tmp/last_run.log`（launcher 记录） |

### 3.4 设备上可用的 USB 网卡驱动（如果要接 USB 转 RJ45）

`.ko` 在 `/mnt/system/ko/`（`/lib/modules` 不存在）：

| 驱动 | 状态 |
|---|---|
| `usbnet.ko` + `ax88179_178a.ko` + `asix.ko` | ✅ 实测 insmod 成功 |
| `cdc_ether` / `cdc_ncm` / `rndis_host` | ✅ 已编译 |
| **`CONFIG_USB_RTL8152`** | ❌ **`is not set`** ⇒ RTL8152/8153 网卡（多数便宜拓展坞）**不能用** |

### 3.5 MaixCDK（如果要改 C++ 或重编 MaixPy，可选）

```bash
git clone https://github.com/sipeed/MaixCDK.git      # 约 414MB
docker pull sipeed/maixcdk-builder                     # 约 2.9GB，走上述镜像源
```

本机缺 `autoconf/automake/libtool/python3-dev`，所以走 **Docker 路线**（镜像里全都有）。
便捷脚本：`scripts/maixcdk-docker.sh`（在工作区，未随本目录提交）。

---

## 4. Zynq 目标板（ALINX AX7015B）

| 项 | 值 |
|---|---|
| 芯片 | XC7Z015-2CLG485，1GB DDR3，8GB eMMC |
| 以太网 | **2 路**千兆 RJ45（JL2121 GPHY）。**1 路接 PS（BANK501，Linux 可用）**，1 路接 PL（BANK35，需自己写 IP 挂 AXI，PetaLinux 用不了） |
| USB | **4 路 USB2.0 HOST（Type-A）**，经 Hub 从 PS 扩展 |
| 其他 | 2 路 SFP 光口（6.25Gb/s）、PCIe x2、2 路 USB 转串口（CP2102）、HDMI 出/入 1080P60 |
| **无线** | **不支持**（手册全文无 WiFi/蓝牙/802.11） |

接视频的推荐路径（三选一，详见 `docs/maixcam-to-zynq-video.md`）：

1. **MaixCAM 的 USB 网卡直插 AX7015B 的 USB HOST** —— 一根 Type-C 线，最省事；
   Zynq 内核开 `CONFIG_USB_NET_CDC_NCM` / `CONFIG_USB_NET_RNDIS_HOST`，配 `10.13.148.100/24` 后拉 RTSP
2. MaixCAM + **AX88179** USB 转 RJ45 → AX7015B 的 **PS 端** RJ45（需外接 5V 供电）
3. MaixCAM 走 WiFi，AX7015B 走有线，接同一台交换机（Zynq 不需要支持无线）

---

## 5. 一键复现 checklist

```bash
# ① 电脑端依赖
python3 -m venv --without-pip env/receiver
python3 -m pip install --target env/receiver/lib/python3.10/site-packages \
    opencv-python==4.10.0.84 numpy
python3 -m pip install --target env/maixpy-completion/lib/python3.10/site-packages paramiko

# ② 确认设备在线
lsusb | grep -i maixcam                 # 359f:2120
ping -c1 10.13.148.1

# ③ 装设备端应用
cd maxicam/apps && ./deploy.py install
#    然后重启设备一次

# ④ 板子菜单点「视频推流」→ 选参数 → 开始推流

# ⑤ 电脑端看画面
./pc/run.sh --host 10.13.148.1

# ⑥ 验证无损截图链路
cd ../.. && python3 maxicam/scripts/test_raw_shot.py
#    期望：收到 640x480 / 921600 字节，生成 *_RGB888.png
```

---

## 6. 版本清单（出问题时对照）

| 组件 | 版本 |
|---|---|
| Ubuntu | 22.04.5 LTS (glibc 2.35) |
| Python (PC) | 3.10.12 |
| opencv-python | 4.10.0.84 |
| numpy | 2.2.6 |
| paramiko | 4.x（`pip` 最新即可） |
| MaixVision | 1.2.3-12 amd64 |
| MaixPy（设备） | 4.12.5 |
| MaixPy（PC 补全 stub） | 4.12.5 |
| 设备内核 | 5.10.4-tag- |
| 设备系统 | Buildroot |
| MaixCDK | main 分支（约 414MB） |
| Docker 镜像 | sipeed/maixcdk-builder（2.87GB） |
| 设备 SSH | OpenSSH 9.6 |
