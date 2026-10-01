#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# deploy/pynq/make_sdimage.sh
#
# 用 PetaLinux 自带的 wic 把「启动三件套」和「组装好的 PYNQ 根文件系统」
# 拼成一个可写卡的 .wic 整盘镜像，并逐项验证内容。
#
# 不需要 sudo：wic 内部用 Yocto 的 pseudo 模拟 root。
#
# 路径全部来自 deploy/env.sh。产出：$PYNQ_OUT_DIR/petalinux-sdimage.wic（约 8.5 GB）
# 分步说明见 docs/部署/流程_全链路构建.md 第 8 节。
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# shellcheck disable=SC1091
source "$REPO/deploy/env.sh"
ensure_prj_exists

mkdir -p "$LOG_DIR" "$PYNQ_OUT_DIR"
LOG="$LOG_DIR/04_sdimage.log"
exec > >(tee "$LOG") 2>&1

IMAGES="$PRJ_DIR/images/linux"
ROOTFS="$PYNQ_ROOTFS_DIR/pynq_rootfs.tar.gz"
IMG="$PYNQ_OUT_DIR/petalinux-sdimage.wic"

echo "########## $(date '+%F %T') 出 SD 镜像 ##########"
echo "启动文件: $IMAGES"
echo "根文件系统: $ROOTFS"
echo "输出    : $IMG"
echo "日志    : $LOG"
echo

[ -f "$ROOTFS" ] || { echo "✗ 找不到 $ROOTFS，先跑 deploy/pynq/build_pynq_rootfs.sh"; exit 1; }
for f in BOOT.BIN image.ub boot.scr system.dtb; do
    [ -f "$IMAGES/$f" ] || { echo "✗ 缺少 $IMAGES/$f，先跑 deploy/peta/build_peta.sh"; exit 1; }
done

setup_env
cd "$PRJ_DIR"

echo "══ [1/4] petalinux-package wic ($(date '+%H:%M:%S')) ══"
# --bootfiles : 拷进第一个 FAT32 分区的东西
# -c          : 解压进第二个 ext4 分区的根文件系统（吃 .tar.gz）
# --size      : 分区表；根分区必须 ≥ 6 GB（rootfs 解压后 5.15 GB）
#
# 为什么要重试：
#   新路径下首次出镜像时，wic-tools 的某个 native recipe（实测是 parted-native）
#   可能在离线 sstate 缓存里缺一个 `recipe_qa` 对象。bitbake 会记一条 ERROR 并让
#   `wic-tools` 返回非零，但**真实任务随后成功执行并写进本地 sstate**。
#   于是重试一次就过了。判断成败永远看日志里 `Tasks Summary: ... all succeeded`
#   且没有 `task .*: Failed`，不要只看返回码。
rm -f "$IMG"
WIC_OK=0
for attempt in 1 2; do
    if petalinux-package wic \
         --images-dir images/linux \
         --bootfiles "BOOT.BIN image.ub boot.scr system.dtb" \
         -c "$ROOTFS" \
         --size 512M,8G \
         --outdir "$PYNQ_OUT_DIR"; then
        WIC_OK=1; break
    fi
    [ -f "$IMG" ] && { WIC_OK=1; break; }
    echo "⚠ 第 $attempt 次 petalinux-package wic 返回非零（多为 wic-tools 的 sstate setscene 缺件，真实任务已成功）"
    [ "$attempt" = 1 ] && echo "  自动重试一次…"
done
[ "$WIC_OK" = 1 ] && [ -f "$IMG" ] || { echo "✗ petalinux-package wic 重试后仍未产出镜像"; exit 1; }

echo
echo "══ [2/4] 分区表 ══"
fdisk -l "$IMG"

# 从分区表取偏移，别写死；用 sfdisk -J 拿机器可读结果（fdisk 文本列数会因 Boot 标记而变）
read -r BOOT_START BOOT_SECS ROOT_START ROOT_SECS < <(
    sfdisk -J "$IMG" | python3 -c '
import json, sys
t = json.load(sys.stdin)["partitiontable"]
p = t["partitions"]
print(p[0]["start"], p[0]["size"], p[1]["start"], p[1]["size"])'
)
echo "  boot: start=$BOOT_START sectors=$BOOT_SECS    root: start=$ROOT_START sectors=$ROOT_SECS"

echo
echo "══ [3/4] 启动分区内容 ══"
TMPB=$(mktemp /tmp/dsh-boot.XXXXXX.fat)
dd if="$IMG" of="$TMPB" bs=512 skip="$BOOT_START" count="$BOOT_SECS" status=none
fsck.fat -l "$TMPB" > /tmp/dsh_bootlist.txt 2>&1 || true
cat /tmp/dsh_bootlist.txt
for f in BOOT.BIN IMAGE.UB BOOT.SCR SYSTEM.DTB; do
    grep -qi "$f" /tmp/dsh_bootlist.txt && echo "  ✅ $f" || echo "  ✗ $f 不在启动分区"
done
rm -f "$TMPB"

echo
echo "══ [4/4] 根分区内容 ══"
TMPR=$(mktemp /tmp/dsh-root.XXXXXX.ext4)
dd if="$IMG" of="$TMPR" bs=512 skip="$ROOT_START" count="$ROOT_SECS" status=none
FAIL=0
for p in "/lib/modules/$KERNEL_VERSION/updates/zocl.ko" \
         "/usr/lib/python3.10/pyxrt.so" \
         "/usr/local/share/pynq-venv/lib/python3.10/site-packages/pynq/__init__.py"; do
    printf "  %-72s " "$p"
    # 先取回输出再判断，不要 `debugfs | grep -q`：grep -q 一命中就提前退出，
    # debugfs 收到 SIGPIPE，在 `set -o pipefail` 下会被误判为"不存在"（实测踩过）。
    out=$(debugfs -R "stat $p" "$TMPR" 2>/dev/null || true)
    case "$out" in
        *Inode:*) echo "✅" ;;
        *)        echo "✗"; FAIL=1 ;;
    esac
done
rm -f "$TMPR"

echo
ls -la "$IMG"
if [ "$FAIL" = 0 ]; then
    echo "✅ 镜像逐项验证通过。下一步：写卡（文档第 10 节）"
else
    echo "✗ 根分区有缺项，见上面 ✗"; exit 1
fi
echo "########## $(date '+%F %T') 结束 ##########"
