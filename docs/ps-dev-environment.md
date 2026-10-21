# AX7015B PS 端开发环境配置清单

> 适用板卡：ALINX AX7015B（Zynq-7000，XC7Z015-2CLG485I）
> 依据教程：[AX7015B 官方文档](https://ax7015b-20231-v101.readthedocs.io/zh-cn/latest/index.html)（Vivado / Vitis / PetaLinux 2023.1）
> 审计日期：2026-10-20（磁盘部分同日更新：已建成 300 GiB ext4 `/data`）
> 审计主机：Ubuntu 22.04.5 LTS / Intel Core Ultra 7 155H / 30 GiB RAM

---

## 0. 结论速览

### 已经满足

| 项目 | 现状 | 结论 |
|---|---|---|
| 操作系统 | Ubuntu 22.04.5 LTS x86_64 | 满足；官方教程验证的是 22.04.1，22.04.5 属同一 LTS 小版本，通常可用 |
| CPU | Intel Core Ultra 7 155H，16 核 22 线程 | 满足，编译速度快 |
| 内存 | 30 GiB | 满足（官方最低 8 GiB） |
| 基础工具 | gcc、g++、make、git、rsync、python3 3.10 | 已具备 |
| USB 权限组 | 已在 `plugdev` 组 | 对 JTAG 下载器有帮助 |
| 串口芯片驱动 | 内核已带 `cp210x` / `ftdi_sio` | Linux 下不需要装厂商串口驱动 |

### 必须先解决（P0，否则装不上或跑不起来）

1. ~~**没有可用的 ext4 大容量空间。**~~ **已解决（2026-10-20）**：从 `/dev/nvme0n1p3`（E:）尾部划出 300 GiB，新建 `/dev/nvme0n1p5` 并格式化为 ext4（卷标 `325`），挂载到 `/media/felix/325` 并 bind 到 `/data`，已写入 `/etc/fstab`（`UUID=d17eb381-012b-4729-bad5-348ca081eea3`）。Vivado/Vitis/PetaLinux 一律装到 `/data` 下。详见下文 Step 0。
2. **Vivado + Vitis 2023.1 未安装。**（主阻塞项）
3. **PetaLinux 2023.1 未安装**，主机依赖缺 30 余项。
4. **`/bin/sh` 指向 `dash`**，PetaLinux 要求为 `bash`。
5. **当前工程目录在 NTFS 上**（`/media/felix/Data/...`），建议整体迁到 `/data/work/demoir-fpga`（`/data/work` 已建好）。

### 做 Linux PS 开发还需要（P1）

- NFS 服务、TFTP 服务（TFTP 可选）
- 串口终端工具（minicom / picocom）
- 当前用户加入 `dialout` 组
- USB-JTAG 下载器驱动与 udev 规则
- 有线网口静态 IP / 网络连通
- UFW 放行 NFS/TFTP（本机 UFW 处于 active 状态）

### 只做裸机 PS 开发可以暂缓

PetaLinux、NFS、TFTP 可以先不装；但 **Vivado、Vitis、串口、JTAG 四样是必需**。

| 开发路线 | 对应教程 | 必需环境 |
|---|---|---|
| PS 裸机（推荐先跑通） | S1 第 8 章起、S2 全部 | Vivado 2023.1、Vitis 2023.1、JTAG、串口 |
| PS Linux | S4 全部 | 上述 + PetaLinux 2023.1、NFS（TFTP 可选）、有线网口、更大磁盘 |

> 重要概念：**PS 端开发不是只装 Vitis 就行**。PS 的时钟、DDR、UART、GPIO、SD/eMMC 等配置在 Vivado 的 Zynq PS IP 中完成，必须导出 XSA 给 Vitis 或 PetaLinux 使用。

---

## 1. 板卡与官方环境基线

### 1.1 板卡关键参数

| 项目 | 参数 |
|---|---|
| 主芯片 | XC7Z015-2CLG485I（Zynq-7000，工业级 -40~85°C） |
| PS | 双核 ARM Cortex-A9，767 MHz |
| PS 内存 | 1 GB DDR3，32 bit，1066 Mbps（2 片 512 MB） |
| PS 存储 | 8 GB eMMC、32 MB QSPI Flash、SD 卡槽 |
| PS 外设 | 2 路千兆以太网、USB 2.0、UART（CP210x）、GPIO、SD/SDIO 等 |
| PL 资源 | 74K 逻辑单元、46,200 LUT、92,400 FF、160 个乘法器、3.3 Mb BRAM |
| 高速接口 | 4 路 GTP（最高 6.25 Gb/s）、PCIe 2.0 x2、2 路 SFP |
| 视频 | HDMI 输入/输出各 1 路，最高 1080P@60Hz |
| 调试 | 10-pin 2.54mm JTAG、USB-UART（J1） |
| 启动方式 | SD 卡 / QSPI Flash / JTAG |

### 1.2 官方教程使用的版本

| 软件 | 版本 | 说明 |
|---|---|---|
| Ubuntu | 22.04.1 LTS 64-bit | 教程 S4 第 1 章；官方建议保持一致，不要随意升级 |
| Vivado | 2023.1 | S1/S2 使用 |
| Vitis | 2023.1 | 经典 IDE；2023.2 起改为 Unified IDE，教程不适用 |
| PetaLinux | 2023.1 | 必须与 Vivado XSA 版本一致 |
| 虚拟机 | VMware Workstation 12.1.1 | 仅 Windows 主机需要；你已是原生 Ubuntu，不需要 |
| 资源建议 | 8 GB 内存、300 GB 磁盘、桥接网络 | 你的内存足够，磁盘是当前主要缺口 |

官方教程链接：

- 总目录：https://ax7015b-20231-v101.readthedocs.io/zh-cn/latest/index.html
- Vivado 开发环境：S1 第 3 章
- 体验 ARM（第一个 Vitis 裸机工程）：S1 第 8 章
- S2 准备工程及注意事项：S2 第 0 章
- Ubuntu / Vitis / PetaLinux / NFS 安装：S4 第 1~4 章
- PetaLinux 定制 Linux：S4 第 5 章
- Vitis 开发 Linux 程序：S4 第 6 章

---

## 2. 本机环境审计

### 2.1 系统与资源

| 检查项 | 实测结果 | 判定 |
|---|---|---|
| OS | Ubuntu 22.04.5 LTS（jammy） | 通过 |
| 内核架构 | x86_64 | 通过 |
| CPU | Intel Core Ultra 7 155H，16 核 22 线程 | 通过 |
| 内存 | 30 GiB，无 Swap | 通过 |
| 虚拟化 | 原生系统，非虚拟机 | 通过（比教程的 VMware 方案更简单） |
| 根分区 `/` | 98 GB，已用 52 GB，剩余 42 GB | **不通过**（放不下工具链） |
| **`/data`（`/dev/nvme0n1p5`）** | **295 GiB ext4（原生），全新空卷；真实挂载点 `/media/felix/325`，卷标 `325`** | **通过 ✅ 2026-10-20 建成** |
| `/media/felix/892G` | 532 GB NTFS（fuseblk，E:），已用 422 GB，剩余 110 GB | **文件系统不通过**（且已缩容） |
| `/media/felix/Data` | 452 GB NTFS，剩余约 134 GB | **容量和文件系统都不通过** |
| `/media/felix/Windows-SSD` | 501 GB NTFS，剩余约 102 GB | **不适合** |

### 2.2 工具链与配置

| 检查项 | 实测结果 | 判定 |
|---|---|---|
| Vivado / Vitis | 未安装 | **缺失** |
| PetaLinux | 未安装 | **缺失** |
| `xsct` / `bootgen` / `petalinux-create` | 未找到 | **缺失** |
| 交叉编译器 | 系统未装；Vitis 2023.1 自带 `arm-linux-gnueabihf-gcc` | 装 Vitis 后满足 |
| 串口终端 | 无 minicom / picocom / screen / putty | **缺失** |
| 当前用户组 | 有 `plugdev`，**没有 `dialout`** | **需补充** |
| USB-JTAG 驱动 / udev 规则 | 未安装 | **缺失**（随 Vivado 安装） |
| NFS 服务 | 未安装 | **缺失**（Linux 路线需要） |
| TFTP 服务 | 未安装 | **缺失**（可选） |
| `/bin/sh` | 指向 `dash` | **不符合 PetaLinux 要求** |
| Locale | `zh_CN.UTF-8` | 可用，但 PetaLinux 建议构建时用 `en_US.UTF-8` |
| 有线网口 | `enp0s31f6` 状态 `unavailable`（未插网线） | **需配置** |
| 无线网络 | 已连接 `scut-student`，10.197.249.183/21 | 仅用于上网，不建议用于板卡调试 |
| 防火墙 | UFW 处于 active | **需放行 NFS/TFTP** |
| Xilinx License | 未配置 | **缺失** |
| ALINX 资料/工程 | 当前工作目录基本为空 | **需下载** |

### 2.3 缺失的主机软件包

以下按用途分组，均为当前未安装、且 Ubuntu 22.04 源中可获取的包：

**Vitis GUI 依赖**

```text
libncurses5  libtinfo5  libncurses5-dev  libncursesw5-dev
```

**PetaLinux 主机依赖**

```text
autoconf  automake  bison  chrpath  diffstat  flex  gawk
gcc-multilib  g++-multilib
libegl1-mesa  libsdl1.2-dev  libssl-dev  libtool
net-tools  ninja-build  pax
python3-git  python3-jinja2  python3-pip  python3-psutil
python3-subunit  python3-venv
socat  texinfo  tofrodos  xterm  zlib1g-dev
```

**串口 / NFS / TFTP / 调试辅助**

```text
minicom  picocom  nfs-kernel-server  tftpd-hpa  fxload
device-tree-compiler  u-boot-tools
```

**可选**

```text
gparted（图形化分区，创建 ext4 时方便）
```

> 教程 S4 第 2 章中的 `sudo apt install ncurses-compat-libs` 是 RHEL/CentOS 的包名，Ubuntu 22.04 没有这个包，执行会报 `Unable to locate package`。Ubuntu 下用 `libncurses5` + `libtinfo5` 即可。

---

## 3. 配置步骤

### Step 0（P0）：准备 ext4 空间 ✅ 已完成（2026-10-20）

**不要**把 Vivado/PetaLinux 装在 NTFS 上，也不要把 PetaLinux 工程放在 NTFS 上。

#### 已执行的操作（方案 A，已落地）

原 `/dev/nvme0n1p3`（E:，NTFS，831.25 GiB）用 `ntfsresize` 缩到 **531.25 GiB**，尾部空出的 300 GiB 建成 `/dev/nvme0n1p5`（ext4，LABEL=`325`，GPT 分区名同为 `325`）。E: 在 Windows 侧仍作为普通 NTFS 卷可见，剩余约 110 GB 空闲。

最终分区表（`/dev/nvme0n1`，扇区 512 B）：

| 分区 | 起止扇区 | 大小 | 格式 | 挂载点 |
|---|---|---|---|---|
| p1 | 2048 .. 616447 | 300 MiB | vfat | `/boot/efi` |
| p2 | 616448 .. 649215 | 16 MiB | — | — |
| p3 | 649216 .. 1114660351 | 531.25 GiB | ntfs | `/media/felix/892G`（E:） |
| p5 | 1114660864 .. 1743808511 | 300.00 GiB | ext4 | `/media/felix/325` → bind 到 `/data` |
| p4 | 1743808512 .. 1953523711 | 100 GiB | ext4 | `/`（根分区，未动） |

`/etc/fstab` 两行（真实挂载点 + 绑定挂载）：

```text
UUID=d17eb381-012b-4729-bad5-348ca081eea3 /media/felix/325 ext4 defaults,noatime 0 2
/media/felix/325 /data none bind,x-systemd.requires=/media/felix/325 0 0
```

> **为什么真实挂载点不直接用 `/data`：** GNOME 的 GVolumeMonitor 只把**挂载点在 `/media/$USER/` 下**的卷暴露成可浏览卷（`computer:///`）。挂在 `/data` 这类系统路径下时，文件管理器的"计算机"里不会出现它——实测 udisks2 的 `MountPoints` 属性正常、但 GIO 卷列表里没有。改成 `/media/felix/325` 后立刻与 E:/D: 行为一致，"计算机"里出现 "325" 卷。`/data` 通过 bind mount 保留，工具链路径不用改。systemd 会自动生成 `media-felix-325.mount` → `data.mount` 的依赖链，开机自动按序挂载。

复现/核对命令：

```bash
lsblk -f
df -hT /data
findmnt /media/felix/325 /data
cat /etc/fstab | grep -E "d17eb381|325"
gio mount -l | grep -A2 325     # 应能看到 "325 -> file:///media/felix/325"
```

已创建并归属当前用户的目录：

```bash
sudo mkdir -p /data/xilinx/2023.1 /data/petalinux/2023.1 \
             /data/work/{vivado,vitis,petalinux,nfs} /data/tmp
sudo chown -R “$USER”:”$USER” /data
```

> `lost+found` 保持 `root:root`，无需改动。

#### 在文件管理器里找不到 `/data`？

这是 GNOME 的正常行为，不是挂载失败。Nautilus 的"其他位置"→"计算机"列的是 **GVolumeMonitor 认得的卷**，而 GVolumeMonitor 只暴露**挂载点在 `/media/$USER/` 下**的卷（E: `/media/felix/892G`、D: `/media/felix/Data`、Windows-SSD）。挂载点若在 `/data` 这类系统路径下，即使 udisks2 正常报告 `MountPoints`，也不会出现在卷列表里。

现在的布局（已解决）：真实挂载点 `/media/felix/325` + bind 到 `/data`，两者都能用。

确认挂载本身正常：

```bash
findmnt /media/felix/325   # /media/felix/325  /dev/nvme0n1p5  ext4  rw,noatime
findmnt /data               # /data             /dev/nvme0n1p5  ext4  rw,noatime
df -hT /data                # 295G ext4
gio mount -l | grep -A2 325
```

进入方式（任选）：

| 方式 | 操作 |
|---|---|
| 计算机 | "其他位置" → "计算机" → 打开 **Fanxiang S690MQ 1TB** 磁盘 → 里面的 **325** 卷 |
| 地址栏 | `Ctrl+L` → 输入 `/data` 或 `/media/felix/325` |
| 主目录 | 已建软链接 `~/data` → `/data`，在"主文件夹"里直接点 |
| 侧边栏 | "其他位置" → "计算机" → "文件系统"（root.link）→ 进根目录后点 `data` |
| 书签 | 已写入 `~/.config/gtk-3.0/bookmarks`（`file:///data 325`），文件管理器重开后侧边栏出现"325" |

> 改动要点：`/etc/fstab` 原来把 p5 直接挂到 `/data`，导致它在文件管理器里"消失"。现在真实挂载点改为 `/media/felix/325`（与 E:/D: 同一命名规则），`/data` 改为 bind mount，systemd 自动生成 `media-felix-325.mount` → `data.mount` 依赖链，开机自动按序挂载。两条路径内容完全一致（同一设备）。卷标与 GPT 分区名、挂载目录名统一为 `325`。

#### 后续如需再次调整分区

操作脚本留在 `/home/felix/.local/share/demoir-fpga-ops/resize_892g.sh`（分 `prep|info|resize|repartition|mkfs|restore` 阶段），GPT 备份在 `/root/gpt-backup-*.bin`。再次缩容 NTFS 前必须先在 Windows 侧 `powercfg /h off` + `chkdsk E: /f /x` 并重启，否则 `ntfsresize` 会以 “NTFS is inconsistent” 拒绝执行。

#### 方案 B：加一块外置 USB 3.0 SSD（备选，未采用）

500 GB 以上的 USB 3.0/3.1 SSD，格式化为 ext4，挂载到 `/data`。优点是不动现有分区；缺点是编译速度受 USB 接口影响。对 Vivado 安装和 PetaLinux 编译来说可以接受，但建议使用 USB 3.0 以上接口。

#### 方案 C：退回虚拟机方案（教程原方案，不推荐）

如果既不能动分区也不方便加盘，可以按教程装 Ubuntu 22.04.1 虚拟机，给虚拟机分配 300 GB 虚拟磁盘（虚拟磁盘文件放在 NTFS 上没问题，因为虚拟机内部是 ext4）。代价是需要处理 USB-JTAG/串口的虚拟机直通，且编译性能下降。你已经是原生 Ubuntu，方案 A 已落地，无需考虑 B/C。

#### 空间规划建议

| 用途 | 建议空间 |
|---|---|
| Vivado + Vitis 2023.1（仅 Zynq-7000 支持） | 60~100 GB |
| 安装包解压/临时空间 | 80 GB 左右（安装完成后可删除安装包） |
| PetaLinux 2023.1 工具 | 约 10 GB |
| 单个 PetaLinux 工程（含首次编译） | 30~60 GB |
| TMPDIR / 构建缓存 | 50 GB+ |
| **合计建议** | **≥ 200 GB，300 GB 更稳妥** |
| **`/data` 实际可用** | **295 GiB（已就位，满足）** |

目录布局（已创建）：

```text
/data/
├── xilinx/2023.1/
│   ├── Vivado/2023.1/
│   └── Vitis/2023.1/
├── petalinux/2023.1/
├── work/
│   ├── vivado/
│   ├── vitis/
│   ├── petalinux/
│   └── nfs/
└── tmp/
```

> 当前工程 `/media/felix/Data/coding/demoir-fpga` 在 NTFS 上，建议迁到 `/data/work/demoir-fpga`。Vivado/PetaLinux 对符号链接、可执行权限、大小写敏感和长路径都有要求，NTFS + ntfs-3g 容易出问题。

---

### Step 1（P0）：安装主机依赖

```bash
sudo apt update
sudo apt install -y \
  autoconf automake bison chrpath diffstat flex gawk \
  gcc-multilib g++-multilib \
  libegl1-mesa libsdl1.2-dev libssl-dev libtool \
  libncurses5 libtinfo5 libncurses5-dev libncursesw5-dev \
  net-tools ninja-build pax \
  python3-git python3-jinja2 python3-pip python3-psutil \
  python3-subunit python3-venv \
  socat texinfo tofrodos xterm zlib1g-dev \
  minicom picocom nfs-kernel-server tftpd-hpa fxload \
  device-tree-compiler u-boot-tools
```

说明：

- `gparted` 如需要可单独安装：`sudo apt install gparted`。
- 如果你的 ALINX 资料里带有 `host_env_setup.sh`，也可以直接运行它；但建议先核对脚本内容，确认不会改动系统源或安装不需要的包。
- 安装完成后可用以下命令复查（应无输出）：

```bash
for p in autoconf automake bison chrpath diffstat flex gawk gcc-multilib g++-multilib \
         libegl1-mesa libsdl1.2-dev libssl-dev libtool libncurses5 libtinfo5 \
         libncurses5-dev libncursesw5-dev net-tools ninja-build pax python3-git \
         python3-jinja2 python3-pip python3-psutil python3-subunit python3-venv \
         socat texinfo tofrodos xterm zlib1g-dev minicom picocom nfs-kernel-server \
         tftpd-hpa fxload device-tree-compiler u-boot-tools; do
  dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || echo "MISSING: $p"
done
```

---

### Step 2（P0）：切换 `/bin/sh` 为 bash，准备 locale

PetaLinux 明确要求 `/bin/sh` 为 bash：

```bash
sudo dpkg-reconfigure dash
# 弹出 "Configuring dash" 时选择 "No"
ls -l /bin/sh
# 期望：/bin/sh -> bash
```

生成本地化环境（PetaLinux 在非英文 locale 下可能出现警告或构建异常）：

```bash
sudo locale-gen en_US.UTF-8
sudo update-locale LANG=en_US.UTF-8
```

如果不想改变整个桌面的中文环境，可以只在 PetaLinux 构建终端里临时设置：

```bash
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
```

---

### Step 3（P0）：安装 Vivado + Vitis 2023.1

#### 3.1 获取安装包

优先从 ALINX 资料包中获取教程配套的：

```text
Xilinx_Unified_2023.1_0507_1903.tar.gz
```

也可以从 AMD 官网下载 2023.1 的 Unified Installer。注意版本必须是 **2023.1**，不要用 2024.x/2025.x，否则教程的项目流程和界面会对不上。

#### 3.2 解压并安装

```bash
mkdir -p /data/xilinx/installer
cd /data/xilinx/installer
tar -xzf /path/to/Xilinx_Unified_2023.1_0507_1903.tar.gz
cd Xilinx_Unified_2023.1_0507_1903
./xsetup
```

安装选项建议：

- 产品：**Vivado ML Standard + Vitis**（不要选 Enterprise，Enterprise 需要付费 License）
- 器件：只勾选 **Zynq-7000**（可显著减少安装体积）
- 安装路径：Installation Directory 填 `/data/xilinx/2023.1`。安装器会自动在其中生成 `Vivado/2023.1`、`Vitis/2023.1` 等子目录，后面的 `source` 路径都按这个层级写。
- Documentation / ModelSim 等按需，磁盘紧张可全部取消
- 安装程序如果要求登录 AMD 账号，按提示登录；安装完成后可断网使用

> 教程里用 `sudo ./xsetup` 安装到 `/tools/Xilinx`，随后 `chmod 777 -R`。不建议照做：安装到当前用户拥有的目录即可，不需要 root，也不要给整个工具链 777 权限。

如果安装器提示无法写入 `/data/xilinx`（安装目录的父目录），先确保父目录属于当前用户：

```bash
sudo mkdir -p /data/xilinx
sudo chown -R "$USER":"$USER" /data/xilinx
```

#### 3.3 配置 License

- 启动 Vivado：`source /data/xilinx/2023.1/Vivado/2023.1/settings64.sh && vivado`
- `Help → Manage License… → Load License → Copy License`，选择 `.lic` 文件。
- XC7Z015 通常属于免费 WebPACK / Vivado ML Standard 的器件范围，可在 AMD 授权页面申请免费 License；如果 ALINX 资料中附带 `.lic`，直接加载即可。Vitis 做 Zynq-7000 裸机/Linux 应用不需要额外 License。
- 若提示器件未授权，先确认工程器件选的是 `xc7z015clg485-2`，并确认 License 包含 Zynq-7000。

#### 3.4 安装 USB-JTAG 下载器驱动

```bash
cd /data/xilinx/2023.1/Vivado/2023.1/data/xicom/cable_drivers/lin64/install_script/install_drivers
sudo ./install_drivers
```

安装后重新插拔下载器。检查：

```bash
lsusb
# 应能看到 FTDI / Xilinx 相关的下载器设备
```

如果下载器能识别但 Vivado 报权限错误，重新插拔并检查 udev 规则；不要用 root 运行 Vivado。

#### 3.5 验证 Vivado / Vitis / 交叉编译器

```bash
source /data/xilinx/2023.1/Vivado/2023.1/settings64.sh
vivado -version
# 期望：Vivado v2023.1

source /data/xilinx/2023.1/Vitis/2023.1/settings64.sh
which vitis
which arm-linux-gnueabihf-gcc
arm-linux-gnueabihf-gcc -v
# 期望：能找到 Vitis 自带的 arm-linux-gnueabihf-gcc 并打印版本
```

> 教程在“适合 ZYNQ 的交叉编译器”一节里 source 的是 Vivado 的 `settings64.sh`。更准确的做法是 source **Vitis** 的 `settings64.sh`，交叉编译器由 Vitis 提供。

#### 3.6 环境变量脚本（建议分开写）

Vivado、Vitis、PetaLinux 的 `settings` 脚本都会修改 `PATH`，不建议全部塞进 `~/.bashrc`。可以创建三个脚本，按需 source：

`~/env-vivado.sh`

```bash
source /data/xilinx/2023.1/Vivado/2023.1/settings64.sh
```

`~/env-vitis.sh`

```bash
source /data/xilinx/2023.1/Vitis/2023.1/settings64.sh
```

`~/env-petalinux.sh`

```bash
export TMPDIR=/data/tmp
export LANG=en_US.UTF-8
source /data/petalinux/2023.1/settings.sh
```

---

### Step 4（P0，Linux 路线）：安装 PetaLinux 2023.1

#### 4.1 安装

PetaLinux 必须以**非 root 用户**安装。安装包优先使用 ALINX 资料中的：

```text
petalinux-v2023.1-*-installer.run
```

```bash
sudo mkdir -p /data/petalinux
sudo chown -R "$USER":"$USER" /data/petalinux

chmod +x /path/to/petalinux-v2023.1-*-installer.run
/data/petalinux-installer/petalinux-v2023.1-*-installer.run -d /data/petalinux/2023.1
```

安装过程中会显示多个 License：

- 按 `q` 退出协议内容
- 按 `y` 同意
- 依次接受所有协议

#### 4.2 验证

```bash
source /data/petalinux/2023.1/settings.sh
echo "$PETALINUX"
petalinux-util --version
# 期望：PetaLinux 2023.1
```

#### 4.3 常见安装失败原因

| 现象 | 原因 | 处理 |
|---|---|---|
| 提示 `/bin/sh` 不是 bash | 系统默认 dash | 执行 Step 2 |
| 提示缺少 host packages | 依赖未装全 | 重跑 Step 1 的 apt 命令 |
| 提示不能以 root 安装 | 用了 sudo 运行 installer | 用普通用户重新运行 |
| 构建时报 locale 相关错误 | `zh_CN.UTF-8` 环境 | `export LANG=en_US.UTF-8` |
| 构建时磁盘满 | `/data` 空间不足或 `TMPDIR` 在根分区 | `export TMPDIR=/data/tmp`，确保 `/data` 有 50 GB+ |
| 下载源码很慢/失败 | 网络问题 | 配置离线 sstate/downloads，必要时设置代理 |

#### 4.4 离线编译资源（建议国内网络环境配置）

从 AMD 2023.1 下载页获取与 PetaLinux 2023.1 对应的两个离线包：

- `sstate_aarch64_2023.1-*`
- `downloads_2023.1-*`

解压后，在 PetaLinux 工程中执行：

```bash
petalinux-config
```

进入 `Yocto Settings`：

- 取消 `Enable Network sstate feeds`
- `Local sstate feeds settings` 指向解压后的 `aarch64` 目录
- `Add pre-mirror url` 指向解压后的 `downloads` 目录，例如 `file:///data/petalinux/offline/downloads`

> 即使配置了离线包，PetaLinux 构建仍可能从 GitHub 拉取部分源码；完全断网不一定能构建成功。

#### 4.5 第一个 PetaLinux 工程（有 BSP 时）

如果 ALINX 资料提供 BSP：

```bash
petalinux-create -t project -n ax7015b -s /path/to/ax_peta.bsp
cd ax7015b
petalinux-build
petalinux-package --boot --u-boot --fsbl --fpga --force
```

如果没有 BSP，用 Vivado 导出的 XSA 创建工程：

```bash
petalinux-create -t project -n ax7015b --template zynq
cd ax7015b
petalinux-config --get-hw-description=/path/to/xsa_directory
petalinux-build
petalinux-package --boot --u-boot --fsbl --fpga --force
```

> `--fpga` 仅在 XSA 中包含 bitstream 时使用；PS-only 工程可不加。教程里出现的 `–boot` 是排版用的长破折号，实际命令是 `--boot`。

生成物在 `images/linux/`：

```text
BOOT.BIN
boot.scr
image.ub
```

把这三个文件复制到 SD 卡的 FAT32 分区，板卡拨到 SD 启动模式即可。

---

### Step 5（P0）：串口与 JTAG 权限

#### 5.1 加入 dialout 组

```bash
sudo usermod -aG dialout "$USER"
# 注销重新登录，或新开一个终端后执行：
groups | grep dialout
```

#### 5.2 连接串口

- 板卡 UART 接口：`J1`（USB-UART，CP210x）
- 波特率：`115200`，8N1，无流控
- 设备节点通常为 `/dev/ttyUSB0`

检查：

```bash
dmesg | grep -i cp210x
ls -l /dev/ttyUSB0
```

打开终端：

```bash
picocom -b 115200 /dev/ttyUSB0
# 退出：Ctrl-A 然后 Ctrl-X
```

或：

```bash
minicom -D /dev/ttyUSB0 -b 115200
# 退出：Ctrl-A 然后 X
```

> Linux 内核已经包含 `cp210x` 驱动，不需要安装 ALINX 资料里的 `CP210x_Windows_Drivers.zip`（那是 Windows 驱动）。

#### 5.3 JTAG 下载器

完成 Step 3.4 的 `install_drivers` 后：

1. 连接 10-pin JTAG 下载器到板卡。
2. 给板卡上电。
3. 打开 Vivado → `Open Hardware Manager` → `Open Target` → `Auto Connect`。
4. 正常情况下应能识别 `xc7z015`。

如果识别不到：

- `lsusb` 确认下载器是否被系统识别
- 重新插拔下载器
- 检查是否被其他进程占用（例如另一个 Vivado 实例）
- 确认当前用户有 `plugdev` 权限

---

### Step 6（P1，Linux 路线）：网络、NFS、TFTP、防火墙

#### 6.1 有线网口

本机有线网口为 `enp0s31f6`，当前未插网线（`unavailable`）。做板卡调试时：

- 用网线直连板卡 PS 端网口，或接到同一台交换机/路由器
- 不建议走校园 WiFi：客户端隔离可能导致主机和板卡互相 ping 不通
- 确认板卡上哪个 RJ45 是 PS 端（S4 教程使用 PS 端 ETH1，具体以丝印/原理图为准）

直连时给主机配静态 IP（示例网段 192.168.1.0/24，可按需修改）：

```bash
nmcli con add type ethernet ifname enp0s31f6 con-name ax7015b ip4 192.168.1.10/24
nmcli con up ax7015b
ip -br addr show enp0s31f6
```

板卡侧可配 `192.168.1.11/24`，然后互相 ping 验证。

#### 6.2 NFS 服务

```bash
sudo mkdir -p /data/work/nfs
sudo chown -R "$USER":"$USER" /data/work/nfs

echo '/data/work/nfs 192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash)' | sudo tee -a /etc/exports
sudo exportfs -ra
sudo systemctl enable --now nfs-kernel-server
showmount -e localhost
```

板卡端挂载（示例主机 IP 192.168.1.10）：

```bash
mount -t nfs -o nolock 192.168.1.10:/data/work/nfs /mnt
```

> `no_root_squash` 方便调试，但只应限制在实验网段（上面的 `192.168.1.0/24`），不要在公网/校园网大网段开放。

#### 6.3 TFTP 服务（可选）

只有需要从 TFTP 网络启动时才需要：

```bash
sudo mkdir -p /srv/tftp
sudo chown -R "$USER":"$USER" /srv/tftp

sudo sed -i 's|^TFTP_DIRECTORY=.*|TFTP_DIRECTORY="/srv/tftp"|' /etc/default/tftpd-hpa
sudo sed -i 's|^TFTP_OPTIONS=.*|TFTP_OPTIONS="--secure --create"|' /etc/default/tftpd-hpa
sudo systemctl enable --now tftpd-hpa
systemctl status tftpd-hpa --no-pager
```

#### 6.4 UFW 放行

本机 UFW 处于 active。只放行板卡所在网段：

```bash
sudo ufw allow from 192.168.1.0/24 to any port nfs proto tcp
sudo ufw allow from 192.168.1.0/24 to any port 111 proto tcp
sudo ufw allow from 192.168.1.0/24 to any port 111 proto udp
sudo ufw allow from 192.168.1.0/24 to any port 69 proto udp
sudo ufw status numbered
```

- NFSv4 主要用 2049/tcp（`nfs` 服务名）
- 若使用 NFSv3，还需要 111/tcp+udp 和 mountd 的动态端口；实验环境也可临时在板卡网段放行
- TFTP 用 69/udp

---

### Step 7（P0）：获取 ALINX 资料并跑通第一个 PS 工程

当前工作目录还没有 ALINX 的资料/工程。需要从 ALINX 官方下载 AX7015B 的软件包，通常包含：

- `course_s1`：FPGA 基础教程工程
- `course_s2`：Vitis 裸机教程工程（PS 端重点）
- `course_s4`：Linux 教程工程、BSP、根文件系统
- 原理图、PCB、XDC 约束、芯片手册
- `Xilinx_Unified_2023.1_0507_1903.tar.gz`、`petalinux-v2023.1-*` 安装包
- `CP210x_Windows_Drivers.zip`（Windows 用，Linux 不需要）
- `imageUSB.exe`、Debian 镜像等

> 教程 S1 第 1 章的软件包列表里还写着 Ubuntu 16.04.3，这是旧版本资料遗留；S4 正文使用的是 Ubuntu 22.04.1 + Vitis/PetaLinux 2023.1，以正文为准。

#### 7.1 最小裸机路线（建议先做）

1. **Vivado**：新建工程，器件选 `xc7z015clg485-2`。
2. 添加 **Zynq PS IP**，至少配置：
   - UART0 → MIO（对应板卡 UART）
   - DDR3 型号/位宽（板卡为 1 GB DDR3，32-bit，1066 Mbps）
   - 需要的话再开 SD/eMMC/GPIO/以太网
3. 如果只做 PS 裸机，可以只生成 PS 配置；如果用到 PL，则生成 bitstream。
4. `Export Hardware → Include bitstream → XSA`。
5. **Vitis 2023.1**：`Create Platform Project` 选择该 XSA → `Create Application Project` → Hello World。
6. 连接 JTAG，`Run/Debug`，串口 115200 应能看到输出。

对应教程：

- S1 第 8 章《体验 ARM，裸机输出 Hello World》
- S2 第 1 章《PS 端 MIO 操作之点亮 LED 灯》
- S2 第 3 章《PS 端 UART 读写控制》

#### 7.2 Linux 路线

1. 用同一个 XSA 或 ALINX BSP 创建 PetaLinux 工程。
2. `petalinux-config` 配置启动方式、串口、网络。
3. `petalinux-build` 编译。
4. `petalinux-package --boot ...` 生成 `BOOT.BIN`、`boot.scr`、`image.ub`。
5. SD 卡 FAT32 分区放入上述文件，板卡拨到 SD 启动。
6. 串口登录，验证网络、NFS 挂载。
7. 用 Vitis 创建 `Xilinx ARM v8 32 Bit Linux Executable`（教程原文如此，实际是 32-bit ARM Linux 应用），通过 NFS 在板卡上运行。

---

## 4. 验证清单

全部配置完成后，逐项检查：

| 检查项 | 命令 | 期望结果 |
|---|---|---|
| `/bin/sh` | `ls -l /bin/sh` | `-> bash` |
| ext4 空间 | `df -hT /data` | ext4，安装前可用 ≥ 200 GB |
| Vivado | `source /data/xilinx/2023.1/Vivado/2023.1/settings64.sh && vivado -version` | `Vivado v2023.1` |
| Vitis | `source /data/xilinx/2023.1/Vitis/2023.1/settings64.sh && which vitis` | 找到 `vitis` |
| 交叉编译器 | `arm-linux-gnueabihf-gcc -v` | 打印 GCC 版本 |
| PetaLinux | `source /data/petalinux/2023.1/settings.sh && petalinux-util --version` | `PetaLinux 2023.1` |
| 串口权限 | `groups \| grep dialout` | 有 `dialout` |
| 串口设备 | `ls -l /dev/ttyUSB0` | 存在，权限含 `dialout` |
| JTAG | Vivado Hardware Manager → Auto Connect | 识别 `xc7z015` |
| NFS | `showmount -e localhost` | 列出 `/data/work/nfs` |
| TFTP（可选） | `systemctl status tftpd-hpa --no-pager` | `active (running)` |
| 防火墙 | `sudo ufw status numbered` | 已放行板卡网段 |
| 有线网口 | `ip -br addr show enp0s31f6` | 有 192.168.1.10/24 |
| 板卡串口 | `picocom -b 115200 /dev/ttyUSB0` | 上电能看到 U-Boot/内核日志 |

---

## 5. 教程中的坑与修正

1. **`ncurses-compat-libs` 在 Ubuntu 不存在**：用 `libncurses5`、`libtinfo5` 代替；否则 `apt install` 直接报找不到包。
2. **不要 `chmod 777 -R /tools/Xilinx`**：安装到当前用户拥有的目录，避免权限过大和安全风险。
3. **`/opt/pkg/petalinux` 和 `/tools/Xilinx` 在根分区**：本机根分区只有 42 GB 可用，装不下。必须改到 `/data`（`/data/xilinx/2023.1`、`/data/petalinux/2023.1` 已建好）。
4. **PetaLinux 必须非 root 安装**，且 `/bin/sh` 必须是 bash。
5. **PetaLinux 工程不要放 NTFS**：构建过程需要符号链接、可执行权限和大小写敏感；NTFS + ntfs-3g 会出问题。
6. **Vitis 交叉编译器应 source Vitis 的 `settings64.sh`**，而不是只 source Vivado 的。
7. **PetaLinux 版本必须与 Vivado XSA 版本一致**：2023.1 的 XSA 配 2023.1 的 PetaLinux；不要混用 2024.x。
8. **PetaLinux 离线包不等于完全离线**：即使配置了 sstate/downloads，构建仍可能需要从 GitHub 拉取部分源码。
9. **CP210x 驱动是 Windows 才需要**：Linux 内核自带 `cp210x`，只需 `dialout` 权限。
10. **NFS 的 `no_root_squash` 只适合实验网段**：不要对 `*` 或校园网大网段开放。
11. **UFW 默认会挡住 NFS/TFTP**：本机 UFW 是 active，必须显式放行。
12. **Ubuntu 22.04.5 vs 教程 22.04.1**：同属 22.04 LTS，通常可用；如果 Vivado/Vitis/PetaLinux 出现无法解释的兼容问题，优先用 22.04.1 虚拟机复现。
13. **JTAG 下载器不要用 root 运行 Vivado**：用 `install_drivers` 配好 udev 规则即可。
14. **PS 端网口不要走校园 WiFi 调试**：客户端隔离会导致 NFS/TFTP 失败，用网线直连或独立路由器。

---

## 6. 硬件与配件清单

| 物品 | 要求 | 用途 |
|---|---|---|
| 电源适配器 | ALINX 套件配套 | 板卡供电 |
| USB-JTAG 下载器 | 10-pin 2.54mm，ALINX 套件配套 | Vivado 下载/调试 |
| USB 线 | USB-A 转 micro-USB（按板卡接口） | UART 串口（J1） |
| microSD 卡 | ≥ 8 GB，建议 16 GB | Linux 启动 / 裸机 BOOT.bin |
| 读卡器 | USB | 制作 SD 启动卡 |
| 网线 | Cat5e 或以上 | PS 以太网、NFS/TFTP |
| HDMI 显示器 + 线 | 支持 1080P | Debian 桌面 / HDMI 实验 |
| USB 键盘鼠标 | 标准 USB | Debian 桌面操作 |
| 可选：USB Hub | 供电充足 | 同时接 JTAG、UART、键鼠 |
| 可选：SFP 模块、PCIe 主机、摄像头、7 寸屏 | 按实验需求 | 对应 S1/S2/S4 高级实验 |

---

## 7. 建议执行顺序

| 顺序 | 任务 | 预计耗时 |
|---|---|---|
| 1 | 备份数据，规划并创建 ext4 分区（方案 A/B/C） | 0.5~1 天（取决于分区/拷贝） |
| 2 | 安装 Step 1 的主机依赖 | 10~20 分钟 |
| 3 | 切换 bash、生成 en_US.UTF-8 | 5 分钟 |
| 4 | 下载并安装 Vivado + Vitis 2023.1 | 2~4 小时（含下载） |
| 5 | 配置 License、安装 JTAG 驱动、验证交叉编译器 | 30 分钟 |
| 6 | 安装 PetaLinux 2023.1（Linux 路线） | 1~2 小时（含下载） |
| 7 | 配置串口、JTAG、有线网络、NFS/TFTP/UFW | 1 小时 |
| 8 | 跑通第一个裸机 Hello World（S1 第 8 章 / S2 第 1 章） | 1~2 小时 |
| 9 | 构建第一个 PetaLinux 系统并从 SD 启动（S4 第 5 章） | 2~4 小时（首次编译较久） |

---

## 8. 参考资料

- [AX7015B 官方教程总目录](https://ax7015b-20231-v101.readthedocs.io/zh-cn/latest/index.html)
- [S1 第 3 章 Vivado 开发环境](https://ax7015b-20231-v101.readthedocs.io/zh-cn/latest/7015B_S1_RSTdocument_CN/03_Vivado%E5%BC%80%E5%8F%91%E7%8E%AF%E5%A2%83_CN.html)
- [S1 第 8 章 体验 ARM（第一个 Vitis 工程）](https://ax7015b-20231-v101.readthedocs.io/zh-cn/latest/7015B_S1_RSTdocument_CN/08_%E4%BD%93%E9%AA%8CARM_CN.html)
- [S2 第 0 章 准备工程及注意事项](https://ax7015b-20231-v101.readthedocs.io/zh-cn/latest/7015B_S2_RSTdocument_CN/00_%E5%87%86%E5%A4%87%E5%B7%A5%E7%A8%8B%E5%8F%8A%E6%B3%A8%E6%84%8F%E4%BA%8B%E9%A1%B9_CN.html)
- [S4 第 1 章 安装虚拟机和 Ubuntu 系统](https://ax7015b-20231-v101.readthedocs.io/zh-cn/latest/7015B_S4_RSTdocument_CN/01_%E5%AE%89%E8%A3%85%E8%99%9A%E6%8B%9F%E6%9C%BA%E5%92%8CUbuntu%E7%B3%BB%E7%BB%9F_CN.html)
- [S4 第 2 章 Ubuntu 安装 Linux 版 Vitis](https://ax7015b-20231-v101.readthedocs.io/zh-cn/latest/7015B_S4_RSTdocument_CN/02_Ubuntu%E5%AE%89%E8%A3%85Linux%E7%89%88Vitis%E8%BD%AF%E4%BB%B6_CN.html)
- [S4 第 3 章 PetaLinux 工具安装](https://ax7015b-20231-v101.readthedocs.io/zh-cn/latest/7015B_S4_RSTdocument_CN/03_Petalinux%E5%B7%A5%E5%85%B7%E5%AE%89%E8%A3%85_CN.html)
- [S4 第 4 章 NFS 服务软件安装](https://ax7015b-20231-v101.readthedocs.io/zh-cn/latest/7015B_S4_RSTdocument_CN/04_NFS%E6%9C%8D%E5%8A%A1%E8%BD%AF%E4%BB%B6%E5%AE%89%E8%A3%85_CN.html)
- [S4 第 5 章 使用 PetaLinux 定制 Linux 系统](https://ax7015b-20231-v101.readthedocs.io/zh-cn/latest/7015B_S4_RSTdocument_CN/05_%E4%BD%BF%E7%94%A8Petalinux%E5%AE%9A%E5%88%B6Linux%E7%B3%BB%E7%BB%9F_CN.html)
- [S4 第 6 章 使用 Vitis 开发 Linux 程序](https://ax7015b-20231-v101.readthedocs.io/zh-cn/latest/7015B_S4_RSTdocument_CN/06_%E4%BD%BF%E7%94%A8Vitis%E5%BC%80%E5%8F%91Linux%E7%A8%8B%E5%BA%8F_CN.html)
- [ALINX AX7015B 产品页](https://www.alinx.com/detail/269)
- [AMD Xilinx AX7015B 板卡页](https://china.xilinx.com/products/boards-and-kits/1-1bu841n.html)
- [PetaLinux 工具参考指南 UG1144](https://docs.xilinx.com/r/en-US/ug1144-petalinux-tools-reference-guide/Overview)
