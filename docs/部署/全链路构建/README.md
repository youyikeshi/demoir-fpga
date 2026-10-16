# 流程：从 PetaLinux 构建到 PYNQ 镜像（AX7015B 全链路）

> **适用板卡**：ALINX AX7015B（XC7Z015，Zynq-7000，ARM32）
> **产出**：`petalinux-sdimage.wic`，`dd` 到 SD 卡即可启动 PYNQ
> **适用环境**：Ubuntu 22.04 · Vivado / Vitis / PetaLinux **2025.2**（赛事硬性要求 ≥ 2025.2）
> **本套覆盖**：从「手上有一个补全过的 XSA」到「写卡后可上板验收」的完整链路，全部手动命令 + 逐步验证。
> **本套不覆盖**：模型量化、HLS 加速器、PS–PL 推理调度（那是 M0–M5，见 [`对接文档_去摩尔纹PS-PL部署.md`](../对接文档_去摩尔纹PS-PL部署.md)）。
>
> ⚠️ **2026-10-06 起，原来那个单文件 `流程_全链路构建.md` 已拆成这个目录下的 8 册**（读哪册见下面 §0.1）。全书**节号统一**（§0…§13、附录 A–H），所以正文里「见第 8 节」这类说法在哪一册都指同一节。
>
> **不知道从哪开始？** 先读 [`../README.md`](../README.md)（部署线入口，按处境指路）。

---

## 0. 这套文档怎么用

三种用法，按你的目的选：

| 你的目的 | 怎么做 |
|---|---|
| **把整条链路跑一遍**（最快） | 按[第 2 节](流程_前置条件.md#2-前置条件动手之前手上得有什么)备齐前置，然后依次跑 `deploy/peta/create_peta_project.sh` → `deploy/peta/build_peta.sh` → `deploy/pynq/build_pynq_rootfs.sh` → `deploy/pynq/make_sdimage.sh`。四个脚本串联起来就是第 3–9 节 |
| **接板子做第一次上板实测**（没有路由器，用 USB 转网口直连） | **[第 10 节](流程_写卡与首启.md#10-写卡与首次启动)**：连线 + 拨码 → 宿主机准备 → 串口 → 板上静态 IP → 六项开机自检，跑完接[第 11 节](说明_板上验收.md#11-板上验收)验收 |
| **理解每一步在干什么 / 手工重做某一步** | 读第 1–10 节。每一节既是说明也是可照敲的命令，节号即步骤号 |
| **出了问题** | 先看[第 12 节](说明_故障排查.md#12-故障排查)故障排查；机制层面的"为什么"在[附录 C](说明_机理与实测.md#附录-c为什么这样配机制) |
| **文里的术语看不懂**（`carrier` / PHY / 分区表 / 超级块 / fops …） | **[附录 H](说明_术语速查.md#附录-h术语速查白话版)：术语速查（白话版）** |

**规矩**：本套里出现的每一个路径，第一次出现时都会交代"是什么、哪来的、谁生成的"（见第 1 节的全局地图）。你如果决定把构建根放到别处，**只需要改 `deploy/env.sh` 一处**，所有脚本跟着走。

### 0.1 这套文档有哪几册、什么时候读哪册

| 册 | 文件 | 包含 | 什么时候读它 |
|---|---|---|---|
| **入口** | [`README.md`](README.md)（本册） | §0 怎么用、**§1 全局地图**、**附录 A 一键脚本**、§0.2 全流程一页纸 | 第一次打开时 |
| 2 | [`流程_前置条件.md`](流程_前置条件.md) | §2 前置条件、附录 B（AMD 那个 Ubuntu 包是什么）、附录 D（版本与产物清单） | **动手之前**清点家当：工具链、XSA、AMD 包、离线缓存、磁盘空间 |
| 3 | [`流程_构建镜像.md`](流程_构建镜像.md) | §3–§9：建工程 → 改配置 → 补设备树 → 装 zocl 配方 → 构建 → 组装根文件系统 → 出 SD 镜像 | **要动手做镜像时**，照着敲 |
| 4 | [`流程_写卡与首启.md`](流程_写卡与首启.md) | §10：写卡 → 连线拨码 → 宿主机准备 → 串口 console → 板上静态 IP → 六项开机自检 | **第一次让板子跑起来时**（无路由器、USB 转网口直连的做法在这册） |
| 5 | [`说明_板上验收.md`](说明_板上验收.md) | §11：M−1 的 10 项检查、首轮哪三项本来就该红、`sudo` 环境陷阱 | 板子能开机联网之后，跑验收前/后 |
| 6 | [`说明_故障排查.md`](说明_故障排查.md) | §12 故障排查（按症状查）、§13 回退与清理（按"删了什么会怎样"查） | **某一步失败了**，或者想清盘重来 |
| 7 | [`说明_机理与实测.md`](说明_机理与实测.md) | 附录 C 为什么这样配（四个耦合点）、**附录 E 上板实测结论与遗留问题**、附录 F 路线选择、附录 G 文档沿革 | 想知道"**为什么**"，或者"**还有什么坑没填**" |
| 8 | [`说明_术语速查.md`](说明_术语速查.md) | 附录 H 白话术语表 | **正文里某个词看不懂时**（`carrier` / PHY / 超级块 / vermagic / fops …） |
| **相关** | [`../流程_登录与远程访问.md`](../流程_登录与远程访问.md) | **不属于本书**：板子跑起来之后的日常访问方式 —— SSH / Jupyter / Samba / `ssh -X` / VNC / NFS | §10 做完、板子能开机联网之后 |

> **全文节号是统一的**（§0…§13、附录 A–H）。跨册引用都带链接；你在任何一册看到「见第 8 节」，
> 它指的都是全书第 8 节，不会因为换册而改变含义。

### 0.2 全流程一页纸

一张表看完整条链路：**每一步产出什么、怎么判定成功、有没有一键脚本**。
（"步"列就是全书节号，点进去是该步的详细命令。）

| 步 | 做什么 | 产物 / 成功判据 | 一键脚本 | 在哪册 |
|---|---|---|---|---|
| §2 | 清点前置：工具链、XSA、AMD 包、离线缓存 | 五条 `ls` 全过；XSA 自检要看到 `PCW_EN_SDIO0/ENET0/QSPI/USB0` | — | [前置条件](流程_前置条件.md) |
| §3 | 建 PetaLinux 工程 + 导入 XSA | 构建根下出现 `ax_peta/` | `deploy/peta/create_peta_project.sh` | [构建镜像](流程_构建镜像.md) |
| §4 | 改工程配置（ext4 根分区、网口、离线缓存） | `config` 里 `ROOTFS_EXT4=y`、`ROOTFS_INITRD` 关掉 | 同上 | [构建镜像](流程_构建镜像.md) |
| §5 | 补设备树（PHY 绑定 + `pynq_board` + `zocl` 节点） | `system.dtb` 里三个节点都在 | 同上 | [构建镜像](流程_构建镜像.md) |
| §6 | 装 zocl 配方 + 三个内核 6.12 适配补丁 | `meta-user/recipes-xrt/zocl/` 有配方和 3 个 `.patch` | 同上 | [构建镜像](流程_构建镜像.md) |
| §7 | 构建：内核 + u-boot + 设备树 + `zocl.ko` | 日志里 `Tasks Summary: … all succeeded`；`image.ub` 从 20 MB 掉到 **约 5 MB** | `deploy/peta/build_peta.sh` | [构建镜像](流程_构建镜像.md) |
| **§7.2** | ★ **重打 `BOOT.BIN`**（最容易漏的一步） | `strings BOOT.BIN \| grep "root=/dev"` 得到 `root=/dev/mmcblk0p2`（**不是 `ram0`**） | `deploy/peta/build_peta.sh` | [构建镜像](流程_构建镜像.md) |
| §8 | 组装 PYNQ 根文件系统（fakeroot 会话） | `pynq_rootfs.tar.gz` **约 2.06 GB**；六个文件都在（`zocl.ko` / `modules.dep` / `pyxrt.so` / `pynq` / `xrt_setup.sh` / `resizefs.sh`） | `deploy/pynq/build_pynq_rootfs.sh` | [构建镜像](流程_构建镜像.md) |
| §9 | 出 SD 镜像（`wic` 自动分区 + 格式化 + 填充） | `petalinux-sdimage.wic` **约 8.5 GB**；分区表 `FAT32 512M` + `ext4 8G` | `deploy/pynq/make_sdimage.sh` | [构建镜像](流程_构建镜像.md) |
| §10.1 | 把镜像写进 SD 卡 | 卡上出现 `boot` 和 `root` 两个分区 | — | [写卡与首启](流程_写卡与首启.md) |
| §10.2–10.4 | 连线 + 拨码 + 开串口看 | 串口刷出 U-Boot → `Starting kernel ...` → 登录提示符 | — | [写卡与首启](流程_写卡与首启.md) |
| §10.5 | 给板子配静态 IP（无路由器场景**必做**） | `carrier=1` 且 `ip addr` 里有 `192.168.7.2/24`。**若 carrier=0 先做 §10.5.1**（把链路降到 100M） | — | [写卡与首启](流程_写卡与首启.md) |
| §10.6 | 六项开机自检 | `mmcblk0` / 根是 `ext4 rw` / `pynq_board=AX7015B` / `carrier=1` / 有 `zocl` / 有 `/dev/dri/renderD*` | — | [写卡与首启](流程_写卡与首启.md) |
| §11 | 板上验收 | `12 通过 / 2 警告 / 1 失败`（唯一的 FAIL 是 §11 里说明的 ⑧，要等第一个 overlay） | `deploy/pynq/M1_acceptance_check.sh` | [板上验收](说明_板上验收.md) |

> **整条链路本机实测约 15 分钟**（全新工程 → 可写卡镜像，有离线缓存）；没有离线缓存首次约 1 小时。逐项耗时见 §1.3。

---

## 1. 全局地图：路径与依赖链

### 1.1 构建根

**本机约定的构建根**（所有构建产物、运行时路径都在它下面，且**不在 git 里**）：

```text
/home/zheyang/SRP/qiansaiFPGA/peta_prj/          ← 构建根（本文简称「构建根」）
├── hardware/
│   └── design_1_wrapper.xsa                     ← 输入：Vivado 导出的、已补全 PS7 的 XSA
├── ax_peta/                                     ← PetaLinux 工程（petalinux-create 建的）
│   ├── project-spec/configs/config              ← 工程配置（本文第 4 节改它）
│   ├── project-spec/meta-user/...               ← 设备树补充、zocl 配方（第 5、6 节）
│   ├── components/yocto/                        ← PetaLinux 自带的 Yocto 层快照
│   ├── build/                                   ← 全部编译中间产物（约 14 GB）
│   └── images/linux/                            ← 启动三件套产出到这（第 7 节）
│         BOOT.BIN · image.ub · boot.scr · system.dtb
├── downloads/pynq/
│   └── jammy.arm.3.1.0.tar.gz                   ← AMD 的 Ubuntu 根文件系统（1.92 GiB，下载得到）
├── offline_cache/                               ← 离线构建缓存（sstate + 源码，约 18 GB）
│   ├── harvest/sstate-cache/                    ← Yocto sstate 缓存
│   └── harvest/downloads/                       ← 所有源码/压缩包（含 XRT git 镜像）
├── pynq_rootfs/
│   ├── tree/                                    ← 解压中间态（5.15 GB，可随时删）
│   └── pynq_rootfs.tar.gz                       ← 装好 zocl.ko 的根文件系统（第 8 节产出）
├── pynq_out/
│   └── petalinux-sdimage.wic                    ← ★ 最终产物，写卡用这个（第 9 节产出）
└── logs/                                        ← 四个脚本的运行日志

/home/zheyang/SRP/qiansaiFPGA/peta_prj/_history_old_peta_prj/   ← 历史证据（229 MB，不参与构建）
└── _backup_20260928_185558/                     含「修复前的坏 XSA」+ 旧镜像 + 旧构建日志
```

**为什么放在 `qiansaiFPGA/peta_prj/` 而不是 `~/peta_prj/`**：这是一个刻意的约定 —— 工程文件集中在课题目录下，便于统一管理与备份。路径由 `deploy/env.sh` 从脚本自身位置**自动推导**（`deploy/` 的上一级是仓库，再上一级是 `qiansaiFPGA`），所以整个课题目录可以整体搬走而不用改脚本。

```text
qiansaiFPGA/
├── demoir-fpga/          ← git 仓库（代码、文档、deploy/ 脚本）
├── peta_prj/             ← 构建根（本文，不在 git 里）
└── xilinx_project/       ← Vivado 工程与 XSA
```

### 1.2 六个产物谁生成谁

```text
① XSA（Vivado 导出，含 PS7 全外设）
   │  petalinux-config --get-hw-description
   ▼
② PetaLinux 工程（内核 6.12.40）
   │
   ├─ petalinux-build ──▶ ③ 启动三件套 ──────────────┐
   │                       BOOT.BIN / image.ub        │
   │                       boot.scr / system.dtb      │
   │                                                  │
   └─ petalinux-build -c zocl ──▶ ④ zocl.ko ──┐      │
                                              │      │
   ⑤ AMD 的 Ubuntu 根文件系统（下载） ─────────┤      │
                                              ▼      ▼
                                    ⑥ petalinux-sdimage.wic（第 9 节）
```

| # | 产物 | 谁产出 | 在构建根里的位置 | 对应本文 |
|---|---|---|---|---|
| ① | XSA | Vivado | `hardware/design_1_wrapper.xsa` | 第 2.2 节 |
| ② | PetaLinux 工程 | `petalinux-create` | `ax_peta/` | [第 3 节](流程_构建镜像.md#3-建-petalinux-工程并导入-xsa) |
| ③ | 启动三件套 | `petalinux-build` | `ax_peta/images/linux/` | [第 7 节](流程_构建镜像.md#7-构建) |
| ④ | `zocl.ko` | `petalinux-build -c zocl` | `ax_peta/build/tmp/sysroots-components/.../zocl/` | [第 7 节](流程_构建镜像.md#7-构建) |
| ⑤ | Ubuntu 根文件系统 | AMD（下载，不自造） | `downloads/pynq/jammy.arm.3.1.0.tar.gz` | 第 2.3 节 |
| ⑥ | SD 镜像 | `petalinux-package wic` | `pynq_out/petalinux-sdimage.wic` | [第 9 节](流程_构建镜像.md#9-出-sd-镜像) |

### 1.3 时间与空间预算（本机实测，2026-09-30）

| 阶段 | 本机实测 | 无离线缓存（联网首次） | 新增占用 |
|---|---|---|---|
| 建工程 + 导入 XSA | 24 秒 | 同 | 约 200 MB |
| `petalinux-build`（全新工程首次） | **8 分 01 秒** | 约 40 分钟 | 约 14 GB |
| `petalinux-build`（再次运行，缓存全命中） | 55 秒 | — | — |
| `petalinux-build -c zocl` | 35–57 秒 | 约 5 分钟 | 小 |
| 组装 PYNQ 根文件系统 | 约 4 分钟 | 同 | 2 GB（+5.15 GB 中间态） |
| 出 SD 镜像 | 约 2 分钟（可能需重试一次，见[第 12 节](说明_故障排查.md#12-故障排查)） | 同 | 8.5 GB |
| **合计（全新工程 → 可写卡镜像）** | **约 15 分钟** | **约 1 小时** | **约 32 GB** |

> 建工程和 `petalinux-build` 会从 `offline_cache/` 取源码与 sstate。**没有离线缓存也能做**，
> 只是首次要联网从 AMD 公开镜像拉约 7 GB（约 40 分钟）。
>
> 「全新工程首次」之所以要 8 分钟（而不是几十秒）：sstate 签名里含若干**绝对路径**
> （`plnxtool.conf` 的 `SYSCONFIG_DIR`、`EXTRA_FILESLIST` 等），工程换目录后
> 内核与 u-boot 需要真编一次；编完就进本地 sstate，再跑只要 55 秒。

---

## 附录 A：一键脚本

全部在 [`deploy/`](../../../deploy/)（入口 [`deploy/README.md`](../../../deploy/README.md)）。
路径统一由 [`deploy/env.sh`](../../../deploy/env.sh) 定义 —— **要换构建根只改它**。

| 顺序 | 脚本 | 对应本文 | 做什么 |
|---|---|---|---|
| 1 | `deploy/peta/create_peta_project.sh` | 第 3–6 节 | 建工程 + 导入 XSA + 改配置 + 写设备树 + 装 zocl 配方 |
| 2 | `deploy/peta/build_peta.sh` | [第 7 节](流程_构建镜像.md#7-构建) | 配置自检 → build → zocl → 重打 BOOT.BIN → 验证 |
| 3 | `deploy/pynq/build_pynq_rootfs.sh` | [第 8 节](流程_构建镜像.md#8-组装-pynq-根文件系统) | fakeroot 组装 PYNQ 根文件系统 |
| 4 | `deploy/pynq/make_sdimage.sh` | [第 9 节](流程_构建镜像.md#9-出-sd-镜像) | 出 SD 镜像并逐项验证 |

```bash
cd /home/zheyang/SRP/qiansaiFPGA/demoir-fpga
bash deploy/peta/create_peta_project.sh
bash deploy/peta/build_peta.sh
bash deploy/pynq/build_pynq_rootfs.sh
bash deploy/pynq/make_sdimage.sh
```

日志落在 `peta_prj/logs/`。

---
