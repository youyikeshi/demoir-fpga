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
# 修正版 resizefs：原版用 fdisk 重建分区 2，会把起点对齐到 1 MiB，
# 而本镜像 p2 起点不是 1 MiB 对齐 → 分区表平移 → 第二次开机 panic。
# 详见该文件头部注释与 docs/部署/流程_全链路构建.md 第 12 节。
RESIZEFS="$REPO/deploy/pynq/files/resizefs.sh"

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

  # ── 补上 XRT 的环境变量文件（AMD 这份 rootfs 里没有，PYNQ 的 sdbuild 会生成它）──
  # PYNQ 3.1 的 pl_server/__init__.py 是这样判断要不要使用 XRT 后端的：
  #     if os.environ.get("XILINX_XRT", False):
  #         from .embedded_device import EmbeddedDevice
  # 缺了这个变量 → Device.devices 恒为 [] → pynq.allocate() 报 "No Devices Found"。
  # 2026-10-06 上板实测：补上之后 xbutil / pyxrt.device(0) / pynq.allocate 才正常。
  # ⚠️ 整个 fakeroot 块是一个单引号字符串，块里绝不能出现单引号：
  # 一旦出现就会提前闭合、把后面的命令全打断；反引号在闭合后还会真的执行命令。
  # 2026-10-06 实测踩过：原来这行用单引号包住 export 两个字，结果
  # pynq_rootfs.tar.gz 根本没生成。要用引号就用双引号。
  echo "export XILINX_XRT=/usr" > tree/etc/profile.d/xrt_setup.sh
  chmod 644 tree/etc/profile.d/xrt_setup.sh
  echo "  已写入 /etc/profile.d/xrt_setup.sh"

  # ── 替换 PYNQ 原版 resizefs（它会平移分区表，导致第二次开机 panic）──
  install -m 0755 '"$RESIZEFS"' tree/usr/local/bin/resizefs.sh
  echo "  已替换 /usr/local/bin/resizefs.sh（只改大小、不动起点的版本）"

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
grep -qE "profile\.d/xrt_setup\.sh$"   "$LIST" && echo "  ✅ xrt_setup.sh 在" || { echo "  ✗ xrt_setup.sh 不在（pynq.allocate 必挂）"; exit 1; }
# 判据要挑"修正版独有"的特征串：修正版会把当前起点读回来再写回去（CUR_START=...），
# 原版则是 `| fdisk ${TGTDEV}`（删了重建、把起点对齐到 1 MiB）。
# ⚠️ 别拿 "CUR_START, +" 当判据：文件里实际写的是 "${CUR_START}, +"，中间隔着 `}`，
# 那个串**永远匹配不到**，会让这条自检无脑报"还是原版"（2026-10-06 踩过）。
grep -q 'CUR_START=' "$OUT/tree/usr/local/bin/resizefs.sh" \
  && ! grep -q 'fdisk ${TGTDEV}' "$OUT/tree/usr/local/bin/resizefs.sh" \
  && echo "  ✅ resizefs.sh 是修正版（只改大小、不动起点）" \
  || { echo "  ✗ resizefs.sh 还是原版 —— 第二次开机会 panic"; exit 1; }
rm -f "$LIST"

echo "══ [4/4] 完成 ($(date +%H:%M:%S)) ══"
ls -la "$OUT/pynq_rootfs.tar.gz"
echo
echo "下一步：出 SD 镜像 → deploy/pynq/make_sdimage.sh"
echo "########## $(date '+%F %T') 结束 ##########"
