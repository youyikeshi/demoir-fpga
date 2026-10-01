#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# deploy/env.sh —— 上板链路的**唯一路径定义**
# ═══════════════════════════════════════════════════════════════════════════
#
# 所有 deploy/ 下的脚本都 source 这个文件，不要在脚本里另写路径。
# 想让整条链路搬到别处，只改这里（或用环境变量覆盖）。
#
# 路径推导规则（不需要手改）：
#   env.sh 在 <仓库>/deploy/ 下 → 仓库根 = 上一级 → qiansaiFPGA 根 = 再上一级
#   构建根 = <qiansaiFPGA 根>/peta_prj
#
# 常见覆盖方式：
#   PETALINUX_PRJ_ROOT=/data/peta_prj bash deploy/peta/build_peta.sh
#
# 完整流程见 docs/部署/流程_全链路构建.md
# ═══════════════════════════════════════════════════════════════════════════

_QIANSAI_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # demoir-fpga
_QIANSAI_ROOT="$(dirname "$_QIANSAI_REPO")"                        # qiansaiFPGA

# ── 构建根：所有构建产物、运行时路径都在这下面（不在 git 里）──
export PETALINUX_PRJ_ROOT="${PETALINUX_PRJ_ROOT:-$_QIANSAI_ROOT/peta_prj}"

# ── 构建根内部布局 ──
export PRJ_NAME="${PRJ_NAME:-ax_peta}"
export PRJ_DIR="$PETALINUX_PRJ_ROOT/$PRJ_NAME"          # PetaLinux 工程
export HW_DIR="$PETALINUX_PRJ_ROOT/hardware"            # 放 XSA（只能有一个）
export OFFLINE_CACHE="$PETALINUX_PRJ_ROOT/offline_cache" # 离线 sstate + 源码
export PYNQ_DL_DIR="$PETALINUX_PRJ_ROOT/downloads/pynq" # AMD 的 Ubuntu 根文件系统
export PYNQ_ROOTFS_DIR="$PETALINUX_PRJ_ROOT/pynq_rootfs" # 组装好的 rootfs
export PYNQ_OUT_DIR="$PETALINUX_PRJ_ROOT/pynq_out"      # SD 镜像输出
export LOG_DIR="$PETALINUX_PRJ_ROOT/logs"

# ── 输入与工具链（可按机器覆盖）──
export XSA_SRC="${XSA_SRC:-$_QIANSAI_ROOT/xilinx_project/moro_v1/design_1_wrapper.xsa}"
export PETALINUX_INSTALL="${PETALINUX_INSTALL:-/opt/pkg/petalinux}"
export VITIS_SETTINGS="${VITIS_SETTINGS:-/home/zheyang/AMD/2025.2/Vitis/settings64.sh}"

# ── 内核版本（改 PetaLinux 版本时必须同步改）──
export KERNEL_VERSION="${KERNEL_VERSION:-6.12.40-xilinx-g31626ef92ff1}"
# MACHINE 名有两种写法，来自 Yocto 自己：
#   work-shared/ 与 plnxtool.conf 里用连字符      → zynq-generic-7z015
#   sysroots-components/ 与 deploy/rpm/ 里用下划线 → zynq_generic_7z015
export MACHINE_NAME="${MACHINE_NAME:-zynq-generic-7z015}"
export MACHINE_DIR="${MACHINE_DIR:-${MACHINE_NAME//-/_}}"

# ── 统一的环境准备：本机代理不通，先去掉；再 source 两套环境 ──
# 用法： source "$(dirname "$0")/env.sh"  之后在需要时调用 setup_env
#
# 注意：PetaLinux / Vitis 的 settings 脚本内部有会返回非零的检查语句，
# 调用方若开着 `set -e`，source 它们会把整个脚本带崩（实测踩过）。
# 所以这里临时关掉 -e / -u，source 完再按原样恢复。
setup_env() {
    unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
    local _errexit=0 _nounset=0 _prc=0
    case "$-" in *e*) _errexit=1 ;; esac
    case "$-" in *u*) _nounset=1 ;; esac
    set +e +u
    # shellcheck disable=SC1090
    source "$PETALINUX_INSTALL/settings.sh" >/dev/null 2>&1
    _prc=$?
    if [ -f "$VITIS_SETTINGS" ]; then
        # shellcheck disable=SC1090
        source "$VITIS_SETTINGS" >/dev/null 2>&1
    fi
    [ "$_errexit" = 1 ] && set -e
    [ "$_nounset" = 1 ] && set -u
    if [ "$_prc" -ne 0 ]; then
        echo "✗ source $PETALINUX_INSTALL/settings.sh 失败（rc=$_prc）" >&2
        return 1
    fi
    echo "环境就绪: PetaLinux $PETALINUX_VER · PETALINUX_PRJ_ROOT=$PETALINUX_PRJ_ROOT"
}

# ── 需要 PetaLinux 环境时，脚本开头调用一次 ──
ensure_prj_exists() {
    [ -d "$PRJ_DIR" ] || { echo "✗ 找不到 PetaLinux 工程：$PRJ_DIR" >&2
        echo "  先跑 deploy/peta/create_peta_project.sh" >&2; return 1; }
}
