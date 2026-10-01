# 取自 PYNQ v3.1：sdbuild/boot/meta-pynq/recipes-xrt/zocl/zocl_git.bb
#
# 为什么不用 meta-xilinx 自带的 zocl_*.bb：
#   2025.2 里所有版本的 zocl/xrt recipe 都是
#       COMPATIBLE_MACHINE:zynqmp / versal / versal-net
#   **不含 zynq**，而我们的 MACHINE 是 zynq-generic，所以会被跳过。
#   PYNQ 这份 recipe 没有任何 COMPATIBLE_MACHINE 限制，可直接编译。
#
# 为什么固定从 XRT 的 2024.1 分支取源码：
#   PYNQ 官方发布镜像（含 image_v3.1.3）用的就是这一支、这一 SRCREV，
#   是 PYNQ 在 Zynq 上唯一经过验证的组合。换分支需重新验证 ABI。
SUMMARY  = "Xilinx Runtime(XRT) driver module"
DESCRIPTION = "Xilinx Runtime driver module provides memory management and compute unit schedule"

LICENSE = "GPLv2 & Apache-2.0"
LIC_FILES_CHKSUM = "file://LICENSE;md5=7d040f51aae6ac6208de74e88a3795f8"

BRANCH ?= "2024.1"
REPO ?= "git://github.com/Xilinx/XRT.git;protocol=https"
BRANCHARG = "${@['nobranch=1', 'branch=${BRANCH}'][d.getVar('BRANCH', True) != '']}"
SRC_URI = "${REPO};${BRANCHARG} \
           file://0001-zocl-kernel-6.12-api.patch \
           file://0002-zocl-aie-arm32-stubs.patch \
          "

PV = "202410.2.17.0"
SRCREV ?= "a75e9843c875bac0f52d34a1763e39e16fb3c9a7"

S = "${WORKDIR}/git/src/runtime_src/core/edge/drm/zocl"

inherit module

# ARM32（Zynq-7000）没有 AI Engine：内核 CONFIG_XILINX_AIE 因
# `depends on ARM64 || COMPILE_TEST` 永远无法开启，zocl 链接期的 aie_*
# 符号因此没有来源。XRT 上游为此提供了 __NONE_PETALINUX__ 开关：跳过
# 内核 AIE 头文件，改用 include/zocl_aie.h 里的 static inline 空桩
# （内联进 zocl.ko，不产生外部符号）。这正是 XRT 自己的 build/build.sh
# 对非 PetaLinux 构建所走的路。配套补丁 0002 补齐了上游缺失的 2 个空桩。
EXTRA_OEMAKE += "cflags_zocl=-D__NONE_PETALINUX__"

pkg_postinst_ontarget_${PN}() {
  #!/bin/sh
  echo "Unloading old XRT Linux kernel modules"
  ( rmmod zocl || true ) > /dev/null 2>&1
  echo "Loading new XRT Linux kernel modules"
  modprobe zocl
}
