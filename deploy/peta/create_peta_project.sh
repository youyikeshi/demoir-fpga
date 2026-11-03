#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# deploy/peta/create_peta_project.sh
#
#   1. 校验输入（XSA / AMD 根文件系统 / 离线缓存）
#   2. 建 PetaLinux 工程（petalinux-create --template zynq）
#   3. 导入 XSA（petalinux-config --get-hw-description）
#   4. 改工程配置：ext4 根分区 + FPGA manager + bootargs + 离线镜像
#   5. 覆盖设备树补充文件 system-user.dtsi
#   6. 装入 zocl 配方与三个内核 6.12 适配补丁
#
# 可重复执行：工程已存在时会拒绝覆盖（要重来先手动删掉，见文档"回退"一节）。
#
# 路径全部来自 deploy/env.sh，不要在本脚本里另写路径。
# 分步说明见 docs/部署/流程_全链路构建.md 第 2–4 节。
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# shellcheck disable=SC1091
source "$REPO/deploy/env.sh"

mkdir -p "$PETALINUX_PRJ_ROOT" "$HW_DIR" "$LOG_DIR"
LOG="$LOG_DIR/01_create_project.log"
exec > >(tee "$LOG") 2>&1

echo "########## $(date '+%F %T') 建 PetaLinux 工程 ##########"
echo "构建根 : $PETALINUX_PRJ_ROOT"
echo "工程   : $PRJ_DIR"
echo "XSA    : $XSA_SRC"
echo "日志   : $LOG"
echo

# ── [1/6] 输入校验 ─────────────────────────────────────────────────────────
echo "══ [1/6] 校验输入 ══"
[ -f "$XSA_SRC" ] || { echo "✗ 找不到 XSA：$XSA_SRC"; exit 1; }
[ -f "$PYNQ_DL_DIR/jammy.arm.3.1.0.tar.gz" ] || {
    echo "✗ 找不到 AMD 根文件系统：$PYNQ_DL_DIR/jammy.arm.3.1.0.tar.gz"
    echo "  下载命令与校验和见 docs/部署/流程_全链路构建.md 附录 B"; exit 1; }
[ -d "$OFFLINE_CACHE/harvest/sstate-cache" ] || {
    echo "✗ 找不到离线缓存：$OFFLINE_CACHE/harvest/sstate-cache"
    echo "  没有它也能构建，但需要联网（首次约 40 分钟）"; }
echo "  ✅ 输入齐备"

# ── [2/6] 准备 hardware/ 并建工程 ──────────────────────────────────────────
echo
echo "══ [2/6] 建工程 ══"
if [ -d "$PRJ_DIR" ]; then
    echo "✗ $PRJ_DIR 已存在，先删掉或改名再跑（见文档「回退」一节）"
    exit 1
fi
mkdir -p "$HW_DIR"
rm -f "$HW_DIR"/*.xsa
cp -v "$XSA_SRC" "$HW_DIR/"
n=$(ls "$HW_DIR"/*.xsa 2>/dev/null | wc -l)
[ "$n" -eq 1 ] || { echo "✗ hardware/ 里必须正好 1 个 xsa（现在 $n 个）"; exit 1; }

setup_env
cd "$PETALINUX_PRJ_ROOT"
petalinux-create project --name "$PRJ_NAME" --template zynq

cd "$PRJ_DIR"
petalinux-config --get-hw-description "$HW_DIR" --silentconfig
echo "  ✅ 工程已建、XSA 已导入"

# ── [3/6] 改工程配置 ───────────────────────────────────────────────────────
echo
echo "══ [3/6] 应用工程配置 ══"
python3 - "$PRJ_DIR/project-spec/configs/config" "$OFFLINE_CACHE" <<'PYEOF'
import re, sys
path, cache = sys.argv[1], sys.argv[2]

S = [  # (key, value) —— value 就是 config 文件里 = 右边那一整串，字符串值要带双引号
    ("CONFIG_SUBSYSTEM_ROOTFS_EXT4", "y"),            # 根文件系统独立成 ext4 分区
    ("CONFIG_SUBSYSTEM_SDROOT_DEV", '"/dev/mmcblk0p2"'),
    ("CONFIG_SUBSYSTEM_FPGA_MANAGER", "y"),           # PYNQ 靠它加载 .bit
    ("CONFIG_SUBSYSTEM_EXTRA_BOOTARGS",
     '"rootfstype=ext4 devtmpfs.mount=1 clk_ignore_unused '
     'uio_pdrv_genirq.of_id=generic-uio rw"'),
    # 外设选择：导入 XSA 后通常是自动识别对的，这里强制一遍以防被重置
    ("CONFIG_SUBSYSTEM_ETHERNET_PS7_ETHERNET_0_SELECT", "y"),
    ("CONFIG_SUBSYSTEM_ETHERNET_PS7_ETHERNET_0_USE_DHCP", "y"),
    ("CONFIG_SUBSYSTEM_FLASH_PS7_QSPI_0_SELECT", "y"),
    ("CONFIG_SUBSYSTEM_PRIMARY_SD_PS7_SD_0_SELECT", "y"),
]
# 离线构建：源码与 sstate 都指向构建根下的缓存目录。
# ⚠ 这两个是 Kconfig **字符串**值，必须带双引号 —— 少了引号 silentconfig
#   会静默忽略，plnxtool.conf 里 SOURCE_MIRROR_URL 变成空串（实测踩过）。
# ★ 2026-11-01：本机没有离线缓存（offline_cache 从未生成过），此时
#   **绝不写这两行** —— 指向一个不存在的 file:// 镜像会让部分 recipe 直接失败，
#   而不写就只是走网络正常下载。文档 §2.4 说的"留空即可"就是这个意思。
import os as _os
if _os.path.isdir(_os.path.join(cache, "harvest", "sstate-cache")):
    S += [
        ("CONFIG_PRE_MIRROR_URL", '"file://%s/harvest/downloads"' % cache),
        ("CONFIG_YOCTO_LOCAL_SSTATE_FEEDS_URL", '"%s/harvest/sstate-cache"' % cache),
    ]
else:
    print("  ⚠ 无离线缓存 ⇒ 不写 mirror 配置，构建将联网取源码（首次约 40 分钟）")
U = [  # 必须显式关掉的（Kconfig 里是 "is not set" 形式）
    "CONFIG_SUBSYSTEM_ROOTFS_INITRD",
    "CONFIG_SUBSYSTEM_ETHERNET_MANUAL_SELECT",
    "CONFIG_SUBSYSTEM_FLASH_MANUAL_SELECT",
    "CONFIG_SUBSYSTEM_PRIMARY_SD_MANUAL_SELECT",
]

lines = open(path, encoding="utf-8").read().splitlines()
def find(key):
    for i, l in enumerate(lines):
        if re.match(r'^(# )?%s[= ]' % re.escape(key), l):
            return i
    return None

for key, val in S:
    i = find(key)
    if i is None:
        lines.append("%s=%s" % (key, val)); print("  + %s=%s" % (key, val))
    elif lines[i] != "%s=%s" % (key, val):
        print("  ~ %s\n      %s\n    → %s=%s" % (key, lines[i], key, val))
        lines[i] = "%s=%s" % (key, val)
for key in U:
    i = find(key)
    want = "# %s is not set" % key
    if i is None:
        lines.append(want)
    elif lines[i] != want:
        print("  - %s" % key); lines[i] = want

open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("  ✅ config 已更新")
PYEOF

# ── [4/6] 设备树补充 ───────────────────────────────────────────────────────
echo
echo "══ [4/6] 写入设备树补充 ══"
DTSI="$PRJ_DIR/project-spec/meta-user/recipes-bsp/device-tree/files/system-user.dtsi"
cp -v "$HERE/files/system-user.dtsi" "$DTSI"

# ── [5/6] zocl 配方 + 补丁 ─────────────────────────────────────────────────
echo
echo "══ [5/6] 装入 zocl 配方与补丁 ══"
ZSRC="$REPO/deploy/pynq/zocl-kernel-6.12"
ZDST="$PRJ_DIR/project-spec/meta-user/recipes-xrt/zocl"
mkdir -p "$ZDST/files"
cp -v "$ZSRC/zocl_git.bb" "$ZDST/"
cp -v "$ZSRC/files/"*.patch "$ZDST/files/"

BSPCONF="$PRJ_DIR/project-spec/meta-user/conf/petalinuxbsp.conf"
grep -q "^PREFERRED_VERSION_zocl" "$BSPCONF" 2>/dev/null || cat >> "$BSPCONF" <<'EOF'

# ── PYNQ on Zynq-7000 所需 ──────────────────────────────────────────────
# meta-xilinx 自带的 zocl/xrt recipe 只声明 zynqmp/versal，不含 zynq，
# 不锁版本的话 bitbake 会按最高 PV 选中它。背景见
# deploy/pynq/zocl-kernel-6.12/README.md
PREFERRED_VERSION_zocl = "202410.2.17.0"
IMAGE_BOOT_FILES:zynq = "BOOT.BIN image.ub boot.scr system.dtb"
EOF
echo "  ✅ 配方 / 补丁 / bsp.conf 就位"

# ── [5b/6] 内核配置片段（USB Host + CMA 512MB）────────────────────────────
# 依据：2026-11-03 的 USB Host 变更。cfg 片段必须同时出现在 SRC_URI
# （kernel-yocto.bbclass:53 find_sccs 收 .cfg）和 KERNEL_FEATURES（交给 scc 合并），
# 两处缺一都不会生效。
echo
echo "══ [5b/6] 内核 cfg 片段 ══"
KCFGDIR="$PRJ_DIR/project-spec/meta-user/recipes-kernel/linux/linux-xlnx"
KBB="$PRJ_DIR/project-spec/meta-user/recipes-kernel/linux/linux-xlnx_%.bbappend"
mkdir -p "$KCFGDIR"
cp -v "$HERE/files/usb-host-and-cma.cfg" "$KCFGDIR/"
# KERNEL_FEATURES 引用的 .cfg 必须在 WORKDIR 里找得到，缺 bsp.cfg 会 bbfatal
# （"Feature 'bsp.cfg' not found"），没有就先建空文件兜底
[ -f "$KCFGDIR/bsp.cfg" ] || : > "$KCFGDIR/bsp.cfg"
if [ ! -f "$KBB" ]; then
    cat > "$KBB" <<'EOF'
FILESEXTRAPATHS:prepend := "${THISDIR}/${PN}:"

SRC_URI:append = " file://bsp.cfg file://usb-host-and-cma.cfg"
KERNEL_FEATURES:append = " bsp.cfg usb-host-and-cma.cfg"
EOF
elif ! grep -q "usb-host-and-cma.cfg" "$KBB"; then
    cat >> "$KBB" <<'EOF'
SRC_URI:append = " file://usb-host-and-cma.cfg"
KERNEL_FEATURES:append = " usb-host-and-cma.cfg"
EOF
fi
echo "  ✅ 内核片段已装入 $KBB"

# ── [6/6] 让配置生效 ───────────────────────────────────────────────────────
echo
echo "══ [6/6] petalinux-config --silentconfig ══"
petalinux-config --silentconfig

echo
echo "══ 自检 ══"
echo "--- 外设与根文件系统 ---"
grep -E "CONFIG_SUBSYSTEM_(ROOTFS_EXT4|ROOTFS_INITRD|SDROOT_DEV|FPGA_MANAGER|EXTRA_BOOTARGS|BOOTARGS_GENERATED)" \
     "$PRJ_DIR/project-spec/configs/config"
echo "--- 离线镜像（应指向 $OFFLINE_CACHE）---"
grep -E "SOURCE_MIRROR_URL|SSTATE_MIRRORS" "$PRJ_DIR/build/conf/plnxtool.conf" | head -4

echo
echo "########## $(date '+%F %T') 完成，下一步：deploy/peta/build_peta.sh ##########"
