#!/bin/sh
# 板子重启后的一次性诊断（**只读**，不改任何配置）
# 用法：ssh xilinx@192.168.7.2 'sh -s' < tools/board_recover.sh
# 判读要点见文件末尾的注释。
set -u
SUDO="echo xilinx | sudo -S -p '' "
BIT=/home/xilinx/demo/design_1_v58.bit
HWH=/home/xilinx/demo/design_1_v58.hwh
REF_BIT=555c22cf29a1482f8d6050645190fe92
REF_HWH=7037857451f5ba35020977a253e106a1

echo "########## 0. 基本 ##########"
date "+board=%Y-%m-%d %H:%M:%S"
uptime; cat /proc/uptime
echo "--- 内存/磁盘 ---"; df -h / | tail -1; df -i / | tail -1; free -m | head -2
echo "--- sudo ---"; echo xilinx | sudo -S -p "" true; echo "sudo rc=$?"
echo "--- fpga state ---"; cat /sys/class/fpga_manager/fpga0/state 2>/dev/null || echo "(无)"

echo
echo "########## A. 板上位流/元数据是否完好 ##########"
ls -la "$BIT" "$HWH" 2>/dev/null
MB=$(md5sum "$BIT" 2>/dev/null | cut -d' ' -f1)
MH=$(md5sum "$HWH" 2>/dev/null | cut -d' ' -f1)
echo "bit md5 = ${MB:-缺}   参考 $REF_BIT"
echo "hwh md5 = ${MH:-缺}   参考 $REF_HWH"
if [ "$MB" = "$REF_BIT" ] && [ "$MH" = "$REF_HWH" ]; then
  echo "⇒ A: 板上副本与主机**完全一致**（所以 Overlay 报错不是文件损坏，要查 PYNQ 解析路径）"
else
  echo "⇒ A: **板上副本与主机不一致 ⇒ SD 上的文件被写坏/截断，重传即可**"
fi
echo "--- .hwh 前 3 行 / 结尾 3 行（看是否被截断）---"
head -3 "$HWH" 2>/dev/null; echo "   ..."; tail -3 "$HWH" 2>/dev/null
echo "--- .bit 头（应为 00 09 0f f0 0f f0 0f f0 0f f0 00 00 01 ...）---"
od -An -tx1 -N 16 "$BIT" 2>/dev/null

echo
echo "########## B. 为什么反复重启 ##########"
echo "--- ① 有几次 boot ---"; $SUDO journalctl --list-boots --no-pager 2>/dev/null | tail -8
echo "--- ② 上一次启动最后 40 行 ---"; $SUDO journalctl -b -1 -n 40 --no-pager 2>/dev/null | tail -40
echo "--- ②b 上一次启动里谁要 reboot ---"
$SUDO journalctl -b -1 --no-pager 2>/dev/null | grep -iE "watchdog|reboot.target|OnFailure|logind|Startup finished|demo_player|mingetty|Power" | tail -20
echo "--- ③ watchdog 配置 ---"
systemctl show -p RuntimeWatchdogSec -p ShutdownWatchdogSec -p RebootWatchdogSec 2>/dev/null
echo "--- /etc/systemd/system.conf 里的 watchdog 行 ---"
grep -nE "^#?RuntimeWatchdogSec|^#?ShutdownWatchdogSec|^#?RebootWatchdogSec" /etc/systemd/system.conf 2>/dev/null
echo "--- ④ 失败的单元 ---"; systemctl list-units --state=failed --no-pager 2>/dev/null | head -12
echo "--- ⑤ 谁在自动 reboot（单元文件里出现 reboot）---"
$SUDO grep -rln "reboot" /etc/systemd/system/ 2>/dev/null | head -8
echo "--- ⑥ dmesg：watchdog / thermal / 欠压 ---"
$SUDO dmesg 2>/dev/null | grep -iE "watchdog|thermal|undervolt|brown|over-current|reset" | tail -12
echo "--- ⑦ 温度 ---"
for z in /sys/class/thermal/thermal_zone*; do
  [ -e "$z/temp" ] && echo "  $z: $(cat $z/temp) ($(cat $z/type 2>/dev/null))"
done
echo
echo "########## C. 相机链路（重启后是否还在）##########"
lsusb | grep -iE "359f|0424" || echo "  （没看到相机/hub）"
for d in /sys/class/net/usb*; do
  [ -e "$d" ] || continue
  drv=$(readlink -f "$d/device/driver" 2>/dev/null)
  [ "$drv" = "$(readlink -f /sys/bus/usb/drivers/cdc_ncm 2>/dev/null)" ] && echo "  NCM=$d  ip=$(ip -4 -o addr show $d | awk '{print $4}')  carrier=$(cat $d/carrier 2>/dev/null)"
done
echo "########## 判读 ##########"
echo "A 一致 → 文件没坏，Overlay 报错要查 PYNQ；不一致 → 重传 bit/hwh"
echo "B: RuntimeWatchdogSec 若 < 10min 且 journalctl -b -1 有 'Timed out on watchdog' ⇒ 看门狗复位"
echo "B: 有单元带 OnFailure=reboot 且它 failed ⇒ 是它触发的"
echo "B: dmesg 有 over-current ⇒ 供电；有 thermal ⇒ 过热；都没有且是'有序关机' ⇒ 看门狗/主动 reboot"
