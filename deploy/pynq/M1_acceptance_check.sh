#!/bin/bash
# ============================================================================
# M−1 验收脚本 —— AX7015B 上的 PYNQ 基础功能检查
#
# 用法（在板上，以 root 运行）：
#     cp M1_acceptance_check.sh /root/
#     chmod +x /root/M1_acceptance_check.sh
#     sudo /root/M1_acceptance_check.sh
#
# 依据：docs/部署/流程_全链路构建.md 第 11 节
# 本脚本**只读**，不改动系统（唯一的写操作是在 /tmp 下的临时测试文件）。
# ============================================================================

PASS=0
FAIL=0
WARN=0

ok()   { echo -e "  [\e[32mPASS\e[0m] $1"; PASS=$((PASS+1)); }
bad()  { echo -e "  [\e[31mFAIL\e[0m] $1"; FAIL=$((FAIL+1)); }
warn() { echo -e "  [\e[33mWARN\e[0m] $1"; WARN=$((WARN+1)); }
hint() { echo -e "         \e[2m→ $1\e[0m"; }
# 注意：函数名不能叫 head —— 会覆盖 head(1)，而本脚本多处用到 `head -1`
section() { echo; echo "── $1 ──────────────────────────────────────"; }

echo "============================================================"
echo " M−1 验收：AX7015B + PYNQ"
echo " 时间：$(date)    主机：$(hostname)"
echo "============================================================"

section "0. 系统信息"
echo "  内核：$(uname -r)"
echo "  架构：$(uname -m)   机器：$(uname -n)"
[ -f /etc/os-release ] && echo "  系统：$(. /etc/os-release; echo "$PRETTY_NAME")"
echo "  SD/MMC 设备：$(ls /dev/mmcblk* 2>/dev/null | tr '\n' ' ')"
echo "  根分区挂载：$(findmnt -n -o SOURCE / 2>/dev/null)"
echo "  内存：$(free -h 2>/dev/null | awk '/^Mem:/{print $2" 总 / "$7" 可用"}')"
echo "  磁盘：$(df -h / 2>/dev/null | awk 'NR==2{print $2" 总 / "$4" 可用 ("$5" 已用)"}')"
echo "  内核 cmdline：$(cat /proc/cmdline 2>/dev/null)"
echo "  resizefs 跑过没：$(grep -q 'RESIZED=1' /etc/environment 2>/dev/null && echo '已跑（/etc/environment 有 RESIZED=1）' || echo '未跑')"
echo "  swap：$(swapon --show=NAME,SIZE --noheadings 2>/dev/null | tr '\n' ' ' || true)"

# ---------------------------------------------------------------- 1. SD 启动
section "1. 从 SD 卡启动 + 串口可用"
if [ -b "$(findmnt -n -o SOURCE / 2>/dev/null | sed 's/p\?[0-9]*$//')" ]; then
	DEV=$(findmnt -n -o SOURCE / | sed 's/p\?[0-9]*$//')
	case "$DEV" in
		/dev/mmcblk*) ok "根文件系统在 SD/MMC 上（$DEV）" ;;
		*)            warn "根文件系统在 $DEV，不是 mmcblk —— 可能是 NFS/QSPI 启动" ;;
	esac
else
	bad "无法确定根设备"
fi
[ -c /dev/ttyPS0 ] || [ -c /dev/ttyPS1 ] && ok "PS 串口设备存在" || warn "未找到 /dev/ttyPS0|1（串口可能用于 console）"

# ---------------------------------------------------------------- 2. 网口
section "2. 网口：PHY 链路 → IP"
IFACES=$(ls /sys/class/net/ | grep -v '^lo$' | tr '\n' ' ')
echo "  网络接口：$IFACES"

# ── 2a. PHY 链路。这一步**不需要 DHCP、不需要 /etc/network/interfaces.d/**，
#        所以即使还没修 rootfs 也能做。它直接回答流程文档附录 E 的 #2/#3
#        （PHY 复位 MIO7 的实际电平、phy-mode 该用 rgmii-id 还是 rgmii）。
ETH=$(echo "$IFACES" | tr ' ' '\n' | grep -E '^(eth|en)' | head -1)
if [ -n "$ETH" ]; then
	echo "  ── $ETH 物理链路 ──"
	ip link set "$ETH" up 2>/dev/null
	sleep 3
	if command -v ethtool >/dev/null 2>&1; then
		LINK=$(ethtool "$ETH" 2>/dev/null | awk -F': +' '/Link detected/{print $2}')
		SPD=$(ethtool  "$ETH" 2>/dev/null | awk -F': +' '/Speed/{print $2}')
		DUP=$(ethtool  "$ETH" 2>/dev/null | awk -F': +' '/Duplex/{print $2}')
		echo "    Link detected=$LINK  Speed=$SPD  Duplex=$DUP"
		if [ "$LINK" = "yes" ]; then
			ok "PHY 链路已建立（$SPD / $DUP）—— 附录 E #2/#3 通过"
		else
			bad "PHY 链路未建立 —— 附录 E #2/#3 命中"
			hint "先确认网线插的是【PS 那路】RJ45，且对端是路由器 LAN 口"
			hint "再看下面的 dmesg：phy 没 probe 到 → 检查 MIO7 复位电平；probe 到但没 link → 把 system-user.dtsi 的 phy-mode 从 rgmii-id 改成 rgmii 重编"
		fi
	else
		warn "板上没有 ethtool，跳过链路检查"
	fi
	echo "    dmesg 里的 PHY/GEM 记录："
	dmesg 2>/dev/null | grep -iE "phy|gem|mdio|carlink" | tail -6 | sed 's/^/      /'
else
	bad "没有任何 eth* 接口 —— XSA 里 PCW_EN_ENET0 大概率是 0"
fi

# ── 2b. IP。自动获取需要等一下修 rootfs（/etc/network/interfaces.d/ 现在是空的），
#        所以这里手工 dhclient 一次，只为证明"网络硬件通"，不代表开机自动能用。
NET_OK=0
for i in $IFACES; do
	IP=$(ip -4 -o addr show "$i" 2>/dev/null | awk '{print $4}' | head -1)
	[ -n "$IP" ] && { ok "$i 有 IPv4 地址：$IP"; NET_OK=1; }
done
if [ "$NET_OK" = "0" ] && [ -n "$ETH" ]; then
	hint "没有自动 IP —— 预期如此（rootfs 的 /etc/network/interfaces.d/ 是空的，见文档第 12 节）"
	hint "手工试一次 DHCP，只为验证网络硬件……"
	dhclient -v "$ETH" >/dev/null 2>&1 || dhclient "$ETH" >/dev/null 2>&1
	sleep 3
	IP=$(ip -4 -o addr show "$ETH" 2>/dev/null | awk '{print $4}' | head -1)
	if [ -n "$IP" ]; then
		ok "手工 dhclient 拿到地址：$IP（说明网口硬件 OK，只是缺少开机自动配置）"
		NET_OK=1
	else
		warn "手工 dhclient 也没拿到地址（路由器没开 DHCP / 网线没插对）"
	fi
fi
[ "$NET_OK" = "0" ] && bad "最终没有 IPv4 地址"
# 有地址就 ping 一下网关
if [ "$NET_OK" = "1" ]; then
	GW=$(ip route show default 2>/dev/null | awk '/default/{print $3}' | head -1)
	if [ -n "$GW" ]; then
		ping -c 2 -W 2 "$GW" >/dev/null 2>&1 && ok "能 ping 通网关 $GW" || warn "ping 不通网关 $GW"
	else
		warn "没有默认路由（可能是静态网络，忽略）"
	fi
	[ -n "${IP:-}" ] && echo "  → 有线网络可用后可以：ssh xilinx@${IP%/*}    （密码 xilinx）"
fi

# ------------------------------------------------- 3. pynq_board 设备树节点
section "3. 设备树 chosen/pynq_board 节点（耦合点 3）"
if [ -f /proc/device-tree/chosen/pynq_board ]; then
	BOARD=$(tr -d '\0' < /proc/device-tree/chosen/pynq_board)
	ok "存在，值 = \"$BOARD\""
else
	bad "不存在！"
	hint "没有它 → pynq/ps.py 的 ON_TARGET=False → xrt_device.py 不 import pyxrt"
	hint "报错会是 NameError: name 'pyxrt' is not defined，极难定位"
	hint "构建时由 sdbuild/boot/meta-pynq/.../pynq_bootargs.dtsi 注入；手工构建需自己加"
fi

# ------------------------------------------------- 4. fpga_manager
section "4. FPGA manager（PL 配置通道）"
if [ -e /sys/class/fpga_manager/fpga0/firmware ]; then
	ok "/sys/class/fpga_manager/fpga0/firmware 存在"
	echo "        当前状态：$(cat /sys/class/fpga_manager/fpga0/state 2>/dev/null)"
else
	bad "/sys/class/fpga_manager/fpga0/ 不存在"
	hint "内核需 CONFIG_FPGA_MGR_ZYNQ_FPGA=y；设备树需 devcfg@f8007000 节点"
	hint "PYNQ 的 EmbeddedDevice._probe_() 就是靠这个路径判断设备存在"
fi

# ------------------------------------------------- 5. zocl
section "5. zocl 内核模块（耦合点 1）"
if lsmod 2>/dev/null | grep -q '^zocl'; then
	ok "zocl 已加载"
	lsmod | grep '^zocl' | sed 's/^/        /'
else
	bad "zocl 未加载"
	hint "先试：modprobe zocl"
	[ -d /lib/modules/$(uname -r) ] && \
		echo "        /lib/modules/$(uname -r)/ 存在" || \
		hint "/lib/modules/$(uname -r)/ 不存在 —— 内核模块目录与内核版本不匹配"
	find /lib/modules -name 'zocl*.ko*' 2>/dev/null | sed 's/^/        找到：/'
	hint "若 'modprobe: FATAL: Module zocl not found' → zocl 没编进镜像"
	hint "若 'invalid module format' → vermagic 不匹配（内核与模块版本不一致）"
fi
if ls /dev/dri/renderD* >/dev/null 2>&1; then
	ok "DRI 设备存在：$(ls /dev/dri/renderD* | tr '\n' ' ')"
else
	bad "/dev/dri/renderD* 不存在（zocl 没正常工作）"
fi
echo "        dmesg 里的 zocl 记录："
dmesg 2>/dev/null | grep -i zocl | tail -5 | sed 's/^/          /' || echo "          （无）"

# ------------------------------------------------- 6. pyxrt
section "6. pyxrt（XRT 用户态绑定，耦合点 2）"
PY=$(command -v python3)
echo "  python3：$PY  ($($PY --version 2>&1))"
$PY - <<'EOF' && ok "import pyxrt 成功" || { bad "import pyxrt 失败"; }
import sys
try:
    import pyxrt
    print("        pyxrt 位置：", getattr(pyxrt, "__file__", "?"))
except Exception as e:
    print("        错误：%s: %s" % (type(e).__name__, e))
    sys.exit(1)
EOF
$PY - <<'EOF' 2>/dev/null || true
import sys
try:
    import pyxrt
    d = pyxrt.device(0)
    print("        pyxrt.device(0) 打开成功")
except Exception as e:
    print("        pyxrt.device(0) 失败：%s: %s" % (type(e).__name__, e))
    print("        → 常见原因：① zocl 未加载 ② XRT 版本不对（2025.1+ 移除了 pyxrt API）")
EOF

# ------------------------------------------------- 7. pynq 包
section "7. PYNQ Python 包"
$PY - <<'EOF' && ok "from pynq import Overlay 成功" || bad "import pynq 失败"
import sys
try:
    import pynq
    print("        pynq 版本：", getattr(pynq, "__version__", "?"))
    print("        pynq 位置：", getattr(pynq, "__file__", "?"))
    from pynq import Overlay
    from pynq import pl_server
    from pynq.ps import ON_TARGET
    print("        ON_TARGET =", ON_TARGET)
    if not ON_TARGET:
        print("        → ON_TARGET=False！pynq_board 节点缺失，pyxrt 不会被 import")
except Exception as e:
    print("        错误：%s: %s" % (type(e).__name__, e))
    sys.exit(1)
EOF
echo "        pl_server 探测到的设备："
$PY -c "
from pynq.pl_server import Device
try:
    print('       ', Device.devices)
except Exception as e:
    print('         探测失败:', e)
" 2>/dev/null | sed 's/^/  /'

# ------------------------------------------------- 8. 内存分配
section "8. pynq.allocate 连续内存分配（最终目标）"
[ "$(id -u)" != "0" ] && warn "当前不是 root —— PYNQ 的 mmap 需要 root，本节结果不可信"
$PY - <<'EOF' && ok "pynq.allocate 成功" || bad "pynq.allocate 失败"
import sys
try:
    from pynq import allocate
    import numpy as np
    # 从小往大试，报告实际能分配多少
    for mb in (1, 16, 64, 128, 256):
        try:
            n = mb * 1024 * 1024 // 4
            buf = allocate(shape=(n,), dtype='u4')
            addr = getattr(buf, 'device_address', None)
            buf[0] = 0xDEADBEEF
            buf[n-1] = 0xCAFEBABE
            okk = (buf[0] == 0xDEADBEEF and buf[n-1] == 0xCAFEBABE)
            print("        %4d MB OK  物理地址=0x%x  回读%s" % (
                mb, addr or 0, "一致" if okk else "**不一致**"))
            del buf
        except Exception as e:
            print("        %4d MB 失败：%s" % (mb, e))
            if mb == 1:
                sys.exit(1)
            break
except Exception as e:
    print("        致命错误：%s: %s" % (type(e).__name__, e))
    sys.exit(1)
EOF

# ------------------------------------------------- 9. xclbinutil
section "9. xclbinutil（Overlay 加载时用）"
if command -v xclbinutil >/dev/null 2>&1; then
	ok "xclbinutil 可用：$(command -v xclbinutil)"
else
	bad "xclbinutil 不在 PATH"
	hint "PYNQ 下载 bitstream 时会用它临时生成一个只含 mem_topology 的 xclbin"
	hint "它属于 XRT 用户态，检查 XILINX_XRT 环境变量：${XILINX_XRT:-（未设置）}"
fi
[ -f /etc/profile.d/xrt_setup.sh ] && ok "/etc/profile.d/xrt_setup.sh 存在" || warn "/etc/profile.d/xrt_setup.sh 缺失（内容应为 export XILINX_XRT=/usr）"

# ------------------------------------------------- 10. Overlay 加载
section "10. Overlay 加载（.bit + .hwh）"
BIT=$(find /home /root /usr/share /opt /lib/firmware /boot -maxdepth 6 \
          -name '*.bit' 2>/dev/null | head -1)
if [ -z "$BIT" ]; then
	warn "板上没找到任何 .bit 文件，跳过（把测试 overlay 拷上来再跑）"
else
	echo "  用测试 bitstream：$BIT"
	HWH="${BIT%.bit}.hwh"
	[ -f "$HWH" ] && ok "同名 .hwh 存在" || warn "缺少同名 .hwh（Overlay 需要它解析寄存器映射）"
	$PY - "$BIT" <<'EOF' && ok "Overlay 加载成功" || bad "Overlay 加载失败"
import sys
from pynq import Overlay
try:
    ol = Overlay(sys.argv[1])
    print("        ip_dict 条目数：", len(ol.ip_dict))
    for k in list(ol.ip_dict)[:5]:
        print("          -", k)
except Exception as e:
    print("        错误：%s: %s" % (type(e).__name__, e))
    sys.exit(1)
EOF
fi

# ---------------------------------------------------------------- 汇总
echo
echo "============================================================"
echo -e " 汇总：\e[32m$PASS 通过\e[0m   \e[33m$WARN 警告\e[0m   \e[31m$FAIL 失败\e[0m"
if [ "$FAIL" = "0" ]; then
	echo -e " \e[32mM−1 验收通过。\e[0m"
else
	echo -e " \e[31mM−1 未通过。\e[0m 按上面 FAIL 项对应的提示逐条排查。"
	echo " 排查顺序建议：3(pynq_board) → 5(zocl) → 6(pyxrt) → 8(allocate)"
	echo
	echo " 注：如果本轮是「只用串口、还没修 rootfs」的首轮验证，两项可预期为红 ——"
	echo "     第 2 项的【自动 IP】：rootfs 缺 /etc/network/interfaces.d/ 配置，"
	echo "       但 2a 的 PHY 链路 + 手工 dhclient 能证明网口硬件是好的；"
	echo "     第 10 项 Overlay：PL 还没有任何逻辑，手上没有真实 .bit/.hwh，"
	echo "       等 M2 做出加速器再补。这两项都不影响 3/5/6/8 的有效性。"
fi
echo "============================================================"
echo
echo "把完整输出回贴，便于定位。"
exit $([ "$FAIL" = "0" ] && echo 0 || echo 1)
