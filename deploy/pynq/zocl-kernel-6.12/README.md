# zocl 移植到 Zynq-7000 + PetaLinux 2025.2

> **状态：✅ 已完成并验证。** `zocl.ko` 在 PetaLinux 2025.2（内核 6.12.40）上编译链接通过。
>
> 验证结果（`nm -u` 产物）：
> ```
> zocl.ko  217,388 B  ELF 32-bit LSB relocatable, ARM, EABI5
> 未定义的 aie_* 符号：0 个
> vermagic = 6.12.40-xilinx-g31626ef92ff1 SMP preempt mod_unload modversions ARMv7 p2v8
> ```
>
> 实测命令：`petalinux-build -c zocl` → `Successfully built zocl`，5478 个任务全部成功。

## 1. 为什么需要自己编 zocl

PYNQ 在 Zynq-7000 上，`pynq.allocate()` 走的是 **XRT**（`pyxrt.bo()`），不是 CMA/xlnk。
XRT 需要一个内核模块 `zocl.ko` 提供 `/dev/dri/renderD*` 和缓冲区管理。

而 PetaLinux 2025.2 自带的 `meta-xilinx` 里，**所有版本的 zocl/xrt recipe 都不支持 zynq**：

```
COMPATIBLE_MACHINE ?= "^$"
COMPATIBLE_MACHINE:zynqmp   = ".*"
COMPATIBLE_MACHINE:versal   = ".*"
COMPATIBLE_MACHINE:versal-net = ".*"
```

（202310 / 202320 / 202410 / 202420 / 202510 / 202520 逐版本核对，**无一含 `zynq`**）

我们的 `MACHINE = zynq-generic`，所以直接被跳过。

PYNQ 自己的做法是**带一份没有机器限制的 recipe**：
`sdbuild/boot/meta-pynq/recipes-xrt/zocl/zocl_git.bb`。
本目录里的 `zocl_git.bb` 就是照搬它，钉在 XRT `2024.1` 分支
（`SRCREV=a75e9843c875bac0f52d34a1763e39e16fb3c9a7`）——
这是 PYNQ 官方发布镜像用的同一组合，属于已验证基线。

## 2. 三个补丁

### `files/0001-zocl-kernel-6.12-api.patch` —— 内核 6.12 API 漂移

XRT 2024.1 的 zocl 是针对内核 6.6 写的，在 6.12 上有两类漂移（16 文件，+37 −21）：

1. **`<linux/vmalloc.h>` 自 ~6.10 起不再被隐式包含**
   → `vmalloc` / `vzalloc` / `vfree` / `vmap` / `vunmap` / `VM_MAP` 变成隐式声明
   → GCC 默认 `-Werror=implicit-function-declaration` 直接报错
   → 给用到的文件补上 include
2. **`platform_driver` 的 `.remove` 回调自 6.11 起必须返回 `void`**（原为 `int`）
   → `-Werror=incompatible-pointer-types`
   → 从 platform_driver 结构体反查函数名，改签名 + 函数体内 `return X;` → `return;`
   → 改了 10 个函数；另有 2 个（`zocl_csr_intc_remove` / `zocl_irq_intc_remove`）
     本来就已是 `void`（属自定义结构体 `zocl_ert_intc_drv_data`，非 platform_driver），无需改

### `files/0002-zocl-aie-arm32-stubs.patch` —— 补齐缺失的 AIE 空桩

详见下一节。

### `files/0003-zocl-fop-unsigned-offset.patch` —— DRM fops 缺 `FOP_UNSIGNED_OFFSET`

**这个是 2026-10-06 首次上板实测才暴露的**：前两个补丁让 zocl 编过、模块加载成功、
`/dev/dri/renderD128` 也建出来了，但**所有 DRM 节点 open 一律返回 `EINVAL`**。
后果是一连串看起来毫不相干的报错：

```text
xbutil examine          →  Devices present: 0 devices found
pyxrt.device(0)         →  RuntimeError: Could not open device with index '0'
pynq.allocate(...)      →  UserWarning: No devices found, is the XRT environment sourced?
                           RuntimeError: No Devices Found
```

根因是内核 6.12 新加的一条检查（`drivers/gpu/drm/drm_file.c`，`drm_open_helper()`）：

```c
	if (WARN_ON_ONCE(!(filp->f_op->fop_flags & FOP_UNSIGNED_OFFSET)))
		return -EINVAL;
```

`FOP_UNSIGNED_OFFSET` 定义在 `include/linux/fs.h`（`1 << 5`），含义是
"本驱动的 file_operations 使用无符号 offset，不再有 `MAX_NON_LFS` 限制"。
XRT 2024.1 的 `common/zocl_drv.c` 里 `zocl_driver_fops` 没有这个字段，
所以内核**每次 open 都直接拒绝**（我们实测过：`card0` 和 `renderD128`、
读和写，四种组合全是 `EINVAL`）。

修法就是给那个 fops 补一行（带内核版本判断，不影响老内核）：

```c
static const struct file_operations zocl_driver_fops = {
	.owner          = THIS_MODULE,
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 12, 0)
	.fop_flags      = FOP_UNSIGNED_OFFSET,
#endif
	.open           = drm_open,
```

**验证**（换上新 `.ko` 即可，不必重烧卡）：

```bash
python3 -c "import os; print('OPEN OK', os.open('/dev/dri/renderD128', os.O_RDWR))"
xbutil examine | tail -5      # 应从 "0 devices found" 变成列出 [0000:00:00.0] edge
python3 -c "import pyxrt; pyxrt.device(0)"   # 应不再报 Could not open device
```

> ⚠️ **改完补丁后必须确认 Yocto 工作目录里的旧副本被刷新。**
> `file://` 补丁会被复制到 `build/tmp/work/…/zocl/<PV>/`，只改工程里的
> `files/` 而 bitbake 复用旧副本时，会报
> `Hunk #1 succeeded at … with fuzz N` —— 而 `patch-fuzz` 是**致命 QA**，
> 构建直接失败。处理：`rm -rf build/tmp/work/…/zocl build/tmp/stamps/…/zocl`
> 后再编。手写补丁时行号/空白很容易差一点，**建议用真实文件 `diff -u` 生成**。

## 3. AIE 依赖：是什么、怎么修的

### 3.1 依赖是什么

把上面那个 API 补丁打完后，**全部 `.c` 都编译通过**，失败转移到 modpost 链接阶段：

```
ERROR: modpost: "aie_partition_request" / "_release" / "_get_fd" / "_get_freq"
                "_set_freq_req" / "_is_available" / "aie_part_rscmgr_set_static_range"
                "aie_register_error_notification" / "aie_get_errors" / "aie_free_errors"
                [zocl.ko] undefined!
```

这 10 个符号由内核的 AI Engine 驱动导出（`EXPORT_SYMBOL_GPL`，
源码在 `drivers/misc/xilinx-ai-engine/`）。

**AI Engine 是 Versal 专属硬件**，其 Kconfig 为：

```kconfig
config XILINX_AIE
	tristate "Xilinx AI engine"
	depends on ARM64 || COMPILE_TEST
	depends on ZYNQMP_FIRMWARE
```

Zynq-7000 是 **ARM32**，`ARM64` 与 `ZYNQMP_FIRMWARE` 两条都不满足 →
`CONFIG_XILINX_AIE` 永远无法开启 → 这 10 个符号在 Zynq-7000 上没有来源。

> 已核对：**6.6 的 Kconfig 限制完全相同**（`xlnx_rebase_v6.6_LTS/drivers/misc/Kconfig:496`
> 同样是 `depends on ARM64 || COMPILE_TEST` + `depends on ZYNQMP_FIRMWARE`）。
> **这不是 2025.2 新引入的问题**，与工具链版本无关。

### 3.2 怎么修 —— 用 XRT 上游自己的开关

XRT 上游早就为"非 PetaLinux 构建"准备了 `__NONE_PETALINUX__` 开关，全树只有 2 处使用：

| 位置 | 行为 |
|---|---|
| `edge/zocl_aie.c:22` | `#ifndef __NONE_PETALINUX__` → 跳过 `#include <linux/xlnx-ai-engine.h>` |
| `include/zocl_aie.h:52` | `#ifdef __NONE_PETALINUX__` → 自行定义 AIE 类型，并提供 `static inline` **空桩** |

空桩的语义（`zocl_aie.h:97–141`）：请求分区返回 `NULL`，其余返回 `-EINVAL` / `-1` /
`false`。它们**内联进 `zocl.ko` 本身，不产生任何外部符号**，因此 modpost 直接通过。
对没有 AI Engine 的 Zynq-7000 来说，AIE ioctl 返回错误正是正确行为。

XRT 自己的 `build/build.sh:390` 走的就是这条路：
`make -C $ZOCL_SRC EXTRA_CFLAGS=-D__NONE_PETALINUX__`

**上游的空桩清单不全**：`zocl_aie_freqscale()` 调用了
`aie_partition_get_freq()` 与 `aie_partition_set_freq_req()`，但这两个函数没有对应空桩，
所以只加 `-D__NONE_PETALINUX__` 仍会剩 2 个未定义符号。
`0002` 补丁按同款风格补齐（签名取自内核头文件，与调用点类型一致）：

```c
static inline int aie_partition_get_freq(struct device *dev, u64 *freq) { return -EINVAL; }
static inline int aie_partition_set_freq_req(struct device *dev, u64 freq) { return -EINVAL; }
```

配方侧只需一行，位于 `zocl_git.bb`：

```bitbake
EXTRA_OEMAKE += "cflags_zocl=-D__NONE_PETALINUX__"
```

（`zocl` 的 Makefile 第 29 行本来就读取 `cflags_zocl` 并追加到 `ccflags-y`，
这是官方预留的注入点。）

### 3.3 产物验证

```bash
K=/home/zheyang/SRP/qiansaiFPGA/peta_prj/ax_peta/build/tmp/sysroots-components/zynq_generic_7z015/zocl/\
lib/modules/6.12.40-xilinx-g31626ef92ff1/updates/zocl.ko

nm -u $K | grep -i aie     # → 无输出（关键验收点）
nm $K    | grep aie_partition  # → 无输出（空桩已内联优化掉）
modinfo $K                 # → vermagic 与目标内核一致；alias 含 of:N*T*Cxlnx,zocl
```

`modinfo` 里的 `alias: of:N*T*Cxlnx,zocl` 说明设备树 compatible 需为 `xlnx,zocl`
（Zynq-7000 的 poll 模式节点），这是下一步接设备树时的依据。

## 4. 怎么构建

```bash
# 1) recipe 与三个补丁装进 PetaLinux 工程
P=/home/zheyang/SRP/qiansaiFPGA/peta_prj/ax_peta/project-spec/meta-user/recipes-xrt/zocl
mkdir -p $P/files
cp zocl_git.bb $P/
cp files/*.patch $P/files/

# 2) petalinuxbsp.conf 里必须有这行（否则 bitbake 按最高 PV 选中 meta-xilinx
#    的 202520.2.20.0，而它不兼容 zynq）
#    PREFERRED_VERSION_zocl = "202410.2.17.0"

# 3) 编译
cd /home/zheyang/SRP/qiansaiFPGA/peta_prj/ax_peta
unset http_proxy https_proxy all_proxy   # 本机代理 127.0.0.1:7897 不通，拉码会失败
source /opt/pkg/petalinux/settings.sh
petalinux-build -c zocl
```

构建耗时约 3 分钟（有 sstate 缓存）。

## 5. 怎么回退

```bash
rm -rf /home/zheyang/SRP/qiansaiFPGA/peta_prj/ax_peta/project-spec/meta-user/recipes-xrt
# 并把 petalinuxbsp.conf 里的 PREFERRED_VERSION_zocl 注释掉
```

回退不影响已构建好的镜像（`images/linux/` 里那份 SD+网口可用的产物与本模块无关，
zocl 是独立 recipe，不进 `petalinux-image-minimal`）。

## 6. 环境事实

| 项 | 值 |
|---|---|
| 内核 | `6.12.40-xilinx-g31626ef92ff1`（linux-xlnx v2025.2） |
| MACHINE | `zynq-generic`（器件 xc7z015） |
| XRT 源码 | branch `2024.1`，`SRCREV=a75e9843c875bac0f52d34a1763e39e16fb3c9a7`，PV `202410.2.17.0` |
| 产物 | `build/tmp/deploy/rpm/zynq_generic_7z015/kernel-module-zocl-*.rpm`（101,503 B） |
| 编译日志 | `build/tmp/work/zynq_generic_7z015-amd-linux-gnueabi/zocl/202410.2.17.0/temp/log.do_compile` |

## 7. 教训

1. **"符号缺失"先看有没有上游预留的开关，再考虑改代码。**
   `__NONE_PETALINUX__` 一开始就在源码里，我却在设计"把 AIE 从 zocl 里摘掉"的方案，
   差点做了数天的工作量去解决一个上游已经解决的问题。
2. **不要用"官方只测过 X 版本"推断"只有 X 版本可行"。**
   PYNQ 声明支持 2024.1 是测试结论，不是架构限制。真正的限制写在 Kconfig 里，
   应该直接去读它。
