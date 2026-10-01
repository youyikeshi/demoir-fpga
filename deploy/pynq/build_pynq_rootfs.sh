#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# deploy/pynq/build_pynq_rootfs.sh
#
# 把 AMD 的 Ubuntu 根文件系统加上我们自己编的 zocl.ko，重新打包成
# petalinux-package wic 能吃的 pynq_rootfs.tar.gz。
#
# 为什么必须在 fakeroot 会话里做：
#   Ubuntu 包里含 root 属主、setuid 位和设备节点。以普通用户解压会
#   把属主变成 1000、丢掉 setuid、静默跳过设备节点。fakeroot 让这些
#   操作"看起来成功"，并且**解压和重新打包必须在同一个会话里**，
#   否则伪造的属主信息会丢。
#
# 路径全部来自 deploy/env.sh（构建根 = <qiansaiFPGA>/peta_prj）。
# 前置：zocl.ko 已编出（deploy/peta/build_peta.sh）＋ AMD 包已下载（附录 B）。
# 产出：$PYNQ_ROOTFS_DIR/pynq_rootfs.tar.gz（约 2.06 GB）
#
# 分步说明见 docs/部署/流程_全链路构建.md 第 7 节。
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# shellcheck disable=SC1091
source "$REPO/deploy/env.sh"

mkdir -p "$LOG_DIR" "$PYNQ_ROOTFS_DIR"
LOG="$LOG_DIR/03_pynq_rootfs.log"
exec > >(tee "$LOG") 2>&1

SRC="${SRC:-$PYNQ_DL_DIR/jammy.arm.3.1.0.tar.gz}"
OUT="${OUT:-$PYNQ_ROOTFS_DIR}"
ZOCL="${ZOCL:-$PRJ_DIR/build/tmp/sysroots-components/$MACHINE_DIR/zocl/lib/modules/$KERNEL_VERSION/updates/zocl.ko}"

echo "########## $(date '+%F %T') 组装 PYNQ 根文件系统 ##########"
echo "AMD 包 : $SRC"
echo "zocl.ko: $ZOCL"
echo "输出   : $OUT/pynq_rootfs.tar.gz"
echo "日志   : $LOG"
echo

[ -f "$SRC" ]  || { echo "✗ 找不到 $SRC"
                   echo "  下载：curl -4 -L -C - -o $SRC \\"
                   echo "          https://download.amd.com/opendownload/pynq/jammy.arm.3.1.0.tar.gz"; exit 1; }
[ -f "$ZOCL" ] || { echo "✗ 找不到 zocl.ko：$ZOCL"
                   echo "  先跑 deploy/peta/build_peta.sh"; exit 1; }

echo "══ [0/4] 检查输入 ══"
echo "  rootfs : $(stat -c%s "$SRC") 字节"
echo "  zocl.ko: $(stat -c%s "$ZOCL") 字节"
echo "  内核   : $KERNEL_VERSION"

# ── 安全检查：确认 tar 里没有绝对路径或 ../ 逃逸 ──
echo "══ [1/4] 安全检查 ($(date +%H:%M:%S)) ══"
BAD=$(tar tzf "$SRC" | grep -cE '^/|\.\./' || true)
echo "  绝对路径/逃逸条目: $BAD"
[ "$BAD" = "0" ] || { echo "  ✗ 不安全，中止"; exit 1; }
echo "  ✅ 路径安全"

rm -rf "$OUT/tree" "$OUT/pynq_rootfs.tar.gz"
mkdir -p "$OUT" && cd "$OUT"

echo "══ [2/4] fakeroot 解压 + 装 zocl.ko + 重新打包 ($(date +%H:%M:%S)) ══"
fakeroot -- bash -c '
  set -e
  mkdir -p tree

  tar -xzf '"$SRC"' -C tree
  echo "  解压完成，条目数: $(find tree | wc -l)"

  # Ubuntu 的 /lib 是指向 /usr/lib 的软链，写 /lib 会实际落到 /usr/lib
  mkdir -p tree/lib/modules/'"$KERNEL_VERSION"'/updates
  cp '"$ZOCL"' tree/lib/modules/'"$KERNEL_VERSION"'/updates/zocl.ko

  # 两个 warning（modules.order / modules.builtin 缺失）可以忽略：
  # zocl.ko 的 depends 是空的，DRM 等依赖都编进了内核
  depmod -b tree '"$KERNEL_VERSION"' 2>&1 | sed "s/^/    /" || true
  echo "  modules.dep: $(cat tree/lib/modules/'"$KERNEL_VERSION"'/modules.dep)"

  tar -czf pynq_rootfs.tar.gz -C tree .
  echo "  产出: $(stat -c%s pynq_rootfs.tar.gz) 字节"
'

echo "══ [3/4] 验证 ══"
# 只把清单 tar 一遍写成文件再 grep。
# 不要写 `tar tzf ... | grep -q`：grep -q 一命中就退出，tar 收到 SIGPIPE，
# 在 `set -o pipefail` 下整条管道返回 141，脚本会被误判为失败（实测踩过）。
LIST=/tmp/dsh_pynq_rootfs_list.txt
tar tzf pynq_rootfs.tar.gz > "$LIST" 2>/dev/null
tar tzvf pynq_rootfs.tar.gz 2>/dev/null | head -1 | awk '{print "  顶层属主: "$2"  (应为 root/root)"}' || true
grep -qE "updates/zocl\.ko$"           "$LIST" && echo "  ✅ zocl.ko 在"     || { echo "  ✗ zocl.ko 不在"; exit 1; }
grep -qE "modules\.dep$"               "$LIST" && echo "  ✅ modules.dep 在" || echo "  ✗ modules.dep 不在"
grep -qE "python3\.10/pyxrt\.so$"      "$LIST" && echo "  ✅ pyxrt.so 在"    || echo "  ✗ pyxrt.so 不在"
grep -qE "site-packages/pynq/__init__\.py$" "$LIST" && echo "  ✅ pynq 在"   || echo "  ✗ pynq 不在"
rm -f "$LIST"

echo "══ [4/4] 完成 ($(date +%H:%M:%S)) ══"
ls -la "$OUT/pynq_rootfs.tar.gz"
echo
echo "下一步：出 SD 镜像 → deploy/pynq/make_sdimage.sh"
echo "########## $(date '+%F %T') 结束 ##########"
