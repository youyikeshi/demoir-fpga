# deploy —— 上板用的工具

> 这个目录放**能跑的东西**：脚本、配方、补丁、设备树片段、验收程序。
> **不放说明。** 每样工具的"为什么"和"怎么用"都在 `docs/部署/` 里。
>
> **要理解整条上板流程，先读 [`../docs/部署/流程_全链路构建.md`](../docs/部署/流程_全链路构建.md)。**
> 本文件只负责告诉你：这里有什么、什么时候用它、它由谁解释。

---

## 1. 和 `docs/部署/` 的分工

```text
docs/部署/  ──照做──▶  deploy/  ──产出──▶  能写卡的 SD 镜像
（知识：为什么、         （工具：脚本、
  怎么做、坑）             配方、补丁）

     ▲                                          │
     └──────────  工具失败时回来查机理  ──────────┘
```

**规矩**：这个目录里每个子目录和每个脚本，都必须有一份文档解释它（下表最后一列）。
新加工具时一起补。

---

## 2. 唯一的路径定义：`env.sh`

**所有脚本都 source [`env.sh`](env.sh)，不要在脚本里另写路径。**
它从自身位置推导构建根：`deploy/` 的上一级是仓库，再上一级是 `qiansaiFPGA`，
构建根默认是 `<qiansaiFPGA>/peta_prj`。

```bash
source deploy/env.sh          # 之后可用 $PETALINUX_PRJ_ROOT / $PRJ_DIR / $PYNQ_OUT_DIR ...
```

**要换构建根，只改这一处**（或临时覆盖）：

```bash
PETALINUX_PRJ_ROOT=/data/peta_prj bash deploy/peta/build_peta.sh
```

`env.sh` 同时提供 `setup_env`（去掉本机不通的代理 + source PetaLinux/Vitis 环境）
和 `ensure_prj_exists`（检查工程在不在），脚本开头调一次即可。

---

## 3. 这里有什么

| 工具 | 干什么 | 什么时候用 | 由谁解释 |
|---|---|---|---|
| `env.sh` | **唯一路径定义**：构建根、工程目录、输出目录、内核版本 | 所有脚本自动 source；手工敲命令前也可以先 source | 本文件第 2 节 |
| **`peta/`** | PetaLinux 侧的两个脚本 + 设备树源文件 | 建工程、构建镜像时 | 本文件第 3 节各行 |
| `peta/create_peta_project.sh` | 建工程 → 导入 XSA → 改配置（ext4/网口/离线镜像）→ 写设备树 → 装 zocl 配方 | **所有事情的第一步** | [`../docs/部署/流程_全链路构建.md`](../docs/部署/流程_全链路构建.md) 第 3–6 节 |
| `peta/build_peta.sh` | 配置自检 → `petalinux-build` → `-c zocl` → 重打 `BOOT.BIN` → 四项验证 | 工程建好后 | 同上 第 7 节 |
| `peta/files/system-user.dtsi` | **设备树补充的权威文件**（PHY 绑定 + `pynq_board` + `zocl` 节点，带逐条注释） | 要改设备树时改它，别只改工程里的副本 | 同上 第 5 节 |
| `pynq/build_pynq_rootfs.sh` | 把 AMD 的 Ubuntu 包加上 `zocl.ko`，打成 `pynq_rootfs.tar.gz`（fakeroot 会话） | 出 SD 镜像之前 | 同上 第 8 节 |
| `pynq/make_sdimage.sh` | `petalinux-package wic` 出整盘镜像，并逐项验证分区与内容 | 最后一步 | 同上 第 9 节 |
| `pynq/M1_acceptance_check.sh` | 板上跑 10 项验收，每项失败都给诊断提示 | **接上板子第一件事就跑它** | 同上 第 11 节 |
| `pynq/zocl-kernel-6.12/` | 把 `zocl.ko` 移植到内核 6.12 —— 配方 + 两个补丁 | 要编 `zocl.ko`，或者它编不过时 | [`pynq/zocl-kernel-6.12/README.md`](pynq/zocl-kernel-6.12/README.md) |
| `pynq/参考/` | PYNQ 官方的 zocl 设备树片段（poll 版 / intc 版），**我们用 poll 版** | 要改设备树时对答案 | [`../docs/部署/流程_全链路构建.md`](../docs/部署/流程_全链路构建.md) 第 5.2 节 |
| `xsa/ax7015b_ps7_complete.tcl` | 补全 PS7 配置（SD 卡 / 网口 / QSPI / GPIO / USB），重新导出 XSA | 要重建 XSA 或改 PS 配置时 | [`xsa/README.md`](xsa/README.md) |

---

## 4. 按什么顺序用

```text
① deploy/peta/create_peta_project.sh
   建工程 + 导入 XSA + 配置 + 设备树 + zocl 配方
      │
      ▼
② deploy/peta/build_peta.sh
   petalinux-build + zocl + 重打 BOOT.BIN + 验证
      │
      ▼
③ deploy/pynq/build_pynq_rootfs.sh
   组装 PYNQ 根文件系统
      │
      ▼
④ deploy/pynq/make_sdimage.sh
   出 SD 镜像并验证
      │
      ▼
⑤ 写卡（手工，需要 sudo）→ 上板
      │
      ▼
⑥ deploy/pynq/M1_acceptance_check.sh（拷到板上跑）
```

一次跑完：

```bash
cd /home/zheyang/SRP/qiansaiFPGA/demoir-fpga
bash deploy/peta/create_peta_project.sh
bash deploy/peta/build_peta.sh
bash deploy/pynq/build_pynq_rootfs.sh
bash deploy/pynq/make_sdimage.sh
```

日志落在 `<构建根>/logs/`（`01_create_project.log` … `04_sdimage.log`）。

**前置**：见 [`../docs/部署/流程_全链路构建.md`](../docs/部署/流程_全链路构建.md) 第 2 节
（工具链、已补全的 XSA、AMD 根文件系统、离线缓存、本地工具）。

---

## 5. 大文件不在这里

AMD 的 1.92 GiB 根文件系统、8.5 GB 的 SD 镜像、18 GB 的离线缓存，
都在构建根里（不在 git 里）。位置、下载命令、校验和、清理顺序：
[`../docs/部署/流程_全链路构建.md`](../docs/部署/流程_全链路构建.md) 第 2.3/2.4 节与第 13.4 节。

---

## 6. 这个目录可以整体删掉吗

可以，但会失去可复现性：

| 删掉 | 后果 |
|---|---|
| `env.sh` | 所有脚本失效（路径定义没了） |
| `peta/` | 要手工敲建工程/配置/设备树/zocl 五步，且设备树内容要重写一遍 |
| `pynq/build_pynq_rootfs.sh` | 要手工敲 fakeroot 那段 |
| `pynq/make_sdimage.sh` | 要手工敲 wic 与三处验证 |
| `xsa/` | 要手工在 Vivado 里点一遍 PS7 配置（约 26 个属性，还有两个顺序坑） |
| `pynq/zocl-kernel-6.12/` | `zocl.ko` 编不出来（AIE 符号问题要重新解一遍） |
| `pynq/M1_acceptance_check.sh` | 板上验收要手工敲 10 项检查 |

**结论**：留着。这些脚本都是"趟过坑之后固化下来的"，重做一遍的成本远高于占用的空间。
