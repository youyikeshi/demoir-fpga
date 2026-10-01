#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# deploy/peta/build_peta.sh
#
#   1. 配置自检（bootargs / 离线镜像 / 外设）
#   2. petalinux-build                    ← 内核 + u-boot + 设备树 + 根文件系统
#   3. petalinux-build -c zocl            ← 单独编 PYNQ 要的内核模块
#   4. petalinux-package boot --u-boot --force  ← ★ 必须重打，否则 BOOT.BIN 里是旧 bootargs
#   5. 逐项验证产物（BOOT.BIN / image.ub / system.dtb / zocl.ko）
#
# 有离线缓存时全流程约 5 分钟；没有则首次约 40 分钟（瓶颈是网络）。
# 分步说明见 docs/部署/流程_全链路构建.md 第 5–6 节。
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# shellcheck disable=SC1091
source "$REPO/deploy/env.sh"
ensure_prj_exists

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/02_build.log"
exec > >(tee "$LOG") 2>&1

IMAGES="$PRJ_DIR/images/linux"
ZOCL="$PRJ_DIR/build/tmp/sysroots-components/$MACHINE_DIR/zocl/lib/modules/$KERNEL_VERSION/updates/zocl.ko"

echo "########## $(date '+%F %T') PetaLinux 构建 ##########"
echo "工程 : $PRJ_DIR"
echo "日志 : $LOG"
echo

# ── [1/5] 配置自检 ─────────────────────────────────────────────────────────
echo "══ [1/5] 配置自检 ══"
CFG="$PRJ_DIR/project-spec/configs/config"
grep -q 'CONFIG_SUBSYSTEM_ROOTFS_EXT4=y'            "$CFG" || { echo "✗ 未开 ext4 根分区"; exit 1; }
grep -q '^# CONFIG_SUBSYSTEM_ROOTFS_INITRD is not set' "$CFG" || { echo "✗ initramfs 未关闭"; exit 1; }
grep -q 'CONFIG_SUBSYSTEM_BOOTARGS_GENERATED=.*root=/dev/mmcblk0p2' "$CFG" \
    || { echo "✗ bootargs 里 root= 不是 /dev/mmcblk0p2"; exit 1; }
grep -q 'rw"' "$CFG" || echo "  ⚠ BOOTARGS_GENERATED 末尾可能没有 rw（Jupyter 写盘会失败）"
echo "  ✅ bootargs: $(grep CONFIG_SUBSYSTEM_BOOTARGS_GENERATED "$CFG" | cut -d'"' -f2)"
echo "  离线源码镜像: $(grep '^SOURCE_MIRROR_URL' "$PRJ_DIR/build/conf/plnxtool.conf")"
echo "  离线 sstate : $(grep -m1 'sstate-cache/PATH' "$PRJ_DIR/build/conf/plnxtool.conf" | xargs)"

setup_env
cd "$PRJ_DIR"

# ── [2/5] 全量构建 ─────────────────────────────────────────────────────────
echo
echo "══ [2/5] petalinux-build（$(date '+%T') 开始）══"
petalinux-build
echo "  ✅ 构建完成（$(date '+%T')）"

# ── [3/5] zocl 内核模块 ────────────────────────────────────────────────────
echo
echo "══ [3/5] petalinux-build -c zocl（$(date '+%T') 开始）══"
petalinux-build -c zocl
echo "  ✅ zocl 完成（$(date '+%T')）"

# ── [4/5] 重新打包 BOOT.BIN ────────────────────────────────────────────────
echo
echo "══ [4/5] petalinux-package boot --u-boot --force ══"
# 为什么不加 --fpga：本 XSA 不含 bitstream（PL 尚未实现），加了会报空 bit 文件
petalinux-package boot --u-boot --force

# ── [5/5] 逐项验证 ─────────────────────────────────────────────────────────
echo
echo "══ [5/5] 产物验证 ══"
FAIL=0

echo "--- ① images/linux 三件套 ---"
ls -la "$IMAGES/BOOT.BIN" "$IMAGES/image.ub" "$IMAGES/boot.scr" "$IMAGES/system.dtb"

echo "--- ② BOOT.BIN 注入的 bootargs（关键：不能是 ram0）---"
BA=$(strings "$IMAGES/BOOT.BIN" | grep "root=/dev" || true)
echo "  $BA"
case "$BA" in
    *root=/dev/mmcblk0p2*) echo "  ✅ root 指向 ext4 根分区" ;;
    *) echo "  ✗ BOOT.BIN 里还是旧 bootargs（漏了 --force 重打）"; FAIL=1 ;;
esac

echo "--- ③ image.ub 大小（去掉 ramdisk 后应约 5 MB，不是 20 MB）---"
SZ=$(stat -c%s "$IMAGES/image.ub")
echo "  $SZ 字节"
[ "$SZ" -lt 10000000 ] && echo "  ✅ 已去掉 ramdisk" || { echo "  ✗ 仍含 ramdisk，ext4 配置没生效"; FAIL=1; }

echo "--- ④ system.dtb 里的 PYNQ 节点 ---"
DTC=$(find "$PRJ_DIR/build/tmp/work-shared" -name dtc -type f 2>/dev/null | head -1)
[ -n "$DTC" ] || DTC=$(find "$PRJ_DIR/build/tmp" -path "*scripts/dtc/dtc" -type f 2>/dev/null | head -1)
if [ -n "$DTC" ]; then
    "$DTC" -I dtb -O dts -o /tmp/peta_sys.dts "$IMAGES/system.dtb" 2>/dev/null
    grep -q 'pynq_board = "AX7015B"' /tmp/peta_sys.dts \
        && echo "  ✅ chosen/pynq_board = AX7015B" || { echo "  ✗ 缺 pynq_board（PYNQ 会报 NameError: pyxrt）"; FAIL=1; }
    grep -A2 'zyxclmm_drm' /tmp/peta_sys.dts | grep -q 'xlnx,zocl' \
        && echo "  ✅ zyxclmm_drm compatible = xlnx,zocl" || { echo "  ✗ 缺 zocl 设备节点"; FAIL=1; }
    # 网口：节点块里出现 status = "okay" 即可（不能只取 2 行，属性可能在后面）
    awk '/ethernet@e000b000/{f=1} f&&/status/{print;exit}' /tmp/peta_sys.dts | grep -q 'okay' \
        && echo "  ✅ 网口 okay" || echo "  ⚠ 网口节点状态待确认"
else
    echo "  ⚠ 找不到 dtc，跳过设备树验证"
fi

echo "--- ⑤ zocl.ko ---"
ls -la "$ZOCL"
if [ -f "$ZOCL" ]; then
    AIE=$(nm -u "$ZOCL" | grep -i aie || true)
    [ -z "$AIE" ] && echo "  ✅ 零个未定义 AIE 符号" || { echo "  ✗ 仍有未定义 AIE 符号:"; echo "$AIE"; FAIL=1; }
    echo "  vermagic: $(modinfo "$ZOCL" | awk '/^vermagic/{print $2}')"
else
    echo "  ✗ 找不到 zocl.ko"; FAIL=1
fi

echo
if [ "$FAIL" = 0 ]; then
    echo "✅ 全部通过。下一步：deploy/pynq/build_pynq_rootfs.sh"
else
    echo "✗ 有检查未通过，见上面 ✗ 项"; exit 1
fi
echo "########## $(date '+%F %T') 结束 ##########"
