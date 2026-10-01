# deploy/pynq —— PYNQ 板级支撑文件

> **这是 `deploy/pynq/` 的说明。**
> 上一级入口：[`deploy/README.md`](../README.md)（有什么工具、按什么顺序用）
> 部署线入口：[`docs/部署/README.md`](../../docs/部署/README.md)（按处境指路）
> 全链路流程：[`docs/部署/流程_全链路构建.md`](../../docs/部署/流程_全链路构建.md)
>
> 本目录放**能跑的东西**：脚本、配方、补丁、设备树片段。说明在 `docs/部署/`。

## 这里有什么

| 文件 | 作用 | 由谁解释 |
|---|---|---|
| `build_pynq_rootfs.sh` | 组装 PYNQ 根文件系统（AMD 的 Ubuntu 包 + 我们的 `zocl.ko`，fakeroot 会话） | [`docs/部署/流程_全链路构建.md`](../../docs/部署/流程_全链路构建.md) 第 8 节 |
| `make_sdimage.sh` | 用 `petalinux-package wic` 出整盘 SD 镜像，并逐项验证分区与内容 | 同上 第 9 节 |
| `M1_acceptance_check.sh` | M−1 验收脚本，**拷到板上以 root 运行**（10 项） | 同上 第 11 节 |
| `zocl-kernel-6.12/` | **`zocl.ko` 的 2025.2 移植成果**：配方 + 两个补丁 + 完整记录 | 本目录下 `zocl-kernel-6.12/README.md` |
| `参考/` | PYNQ 官方的 zocl 设备树片段（poll 版 / intc 版），**我们用 poll 版** | 同上 第 5.2 节 |

两个脚本的路径都来自 [`../env.sh`](../env.sh)（构建根 = `<qiansaiFPGA>/peta_prj`），
**不在脚本里写死路径**。

**大文件不在仓库里**（AMD 的 1.92 GiB 根文件系统、8.5 GB 的 SD 镜像、18 GB 离线缓存）。
位置和校验：[`docs/部署/流程_全链路构建.md`](../../docs/部署/流程_全链路构建.md) 附录 B。

## 路线：2025.2 自建（唯一合规路线）

赛事要求工具链 ≥ 2025.2，所以官方 sdbuild（只声明支持 2024.1）不可用。
我们走的是**把 PYNQ 需要的三样东西搬进现有 2025.2 工程**：

| 组件 | 做法 | 状态 |
|---|---|---|
| `zocl.ko` | 见 `zocl-kernel-6.12/README.md` | ✅ 已完成并验证 |
| XRT 用户态 + `pyxrt` | **不用自己编** —— AMD 的 `jammy.arm.3.1.0.tar.gz` 里已预装 XRT 2.17.0（与 zocl 同版本） | ✅ |
| 设备树 | 加 `chosen/pynq_board` + `compatible = "xlnx,zocl"` 节点（`../peta/files/system-user.dtsi`） | ✅ |
| PYNQ rootfs | 用 AMD 的板卡无关 rootfs + 我们 2025.2 的 `BOOT.BIN`/`image.ub`/`system.dtb` | ✅ |

**为什么固定钉 XRT 2024.1**：`zocl.ko` 与 `pyxrt.so` 必须同一 XRT 版本；
AMD 那个 rootfs 包里装的是 2.17.0（出自 XRT 2024.1），所以 zocl 也钉 2024.1。
这与"必须用 2024.1 工具链"是两回事 —— 内核、`BOOT.BIN`、`image.ub`、`system.dtb`
全部由 2025.2 生成，XRT 只是被编译的第三方源码。

## 关键提醒

- **不要用 PYNQ-Z2 的 `BOOT.BIN`**：Z2 的 FSBL 里 PS DDR 是 512 MB/16-bit，
  AX7015B 是 1 GB/32-bit → DDR 初始化失败，连 U-Boot 都进不去。
  社区实证：AX7020 用户直接烧 Z2 镜像，"my board doesn't even boot from it"。
- **若 `import pyxrt` 报 `NameError`，先查设备树有没有 `chosen/pynq_board` 节点**
  （缺它则 `ON_TARGET=False`，pyxrt 根本不会被 import）—— 不是 XRT 没装。
- **向 ALINX 要板级源文件**（设备树、`.spec`、tcl），不要他们的 `.img`：
  源文件与工具链版本无关，拿到后可以用我们的 2025.2 自己重建。
