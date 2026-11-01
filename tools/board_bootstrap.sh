#!/usr/bin/env bash
# 板子重刷/换 SD 卡后的一键恢复 + 自检。
#   用法: tools/board_bootstrap.sh [板子IP，默认 192.168.7.2]
# 做什么：① 本机侧核对资产 ② 传 PS 代码与比特流 ③ 板端四项健康检查 ④ md5 对齐核对
set -u
B="${1:-192.168.7.2}"; U=xilinx
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PS="$ROOT/pynq_specialized_code/ps/pynq"
BO="$ROOT/pynq_specialized_code/board/overlay_engine_150mhz"
D=/home/xilinx/demoir

echo "=== ① 本机资产核对 ==="
for f in "$BO/design_1_150v40.bit" "$BO/design_1_150v40.hwh" "$BO/design_1_150v39.bit" \
         "$PS/bin_net.py" "$PS/bin_engine.py" "$PS/bin_ref_numpy.py" "$PS/q8.py" "$PS/q8.c" \
         "$PS/bilinear_c.py" "$PS/bilinear_c.c"; do
    [ -f "$f" ] && echo "  ✓ $(basename "$f")" || { echo "  ✗ 缺 $f"; exit 2; }
done

echo "=== ② 传文件到板子 ==="
ssh -o BatchMode=yes -o ConnectTimeout=8 "$U@$B" "mkdir -p $D/ps/pynq $D/demo_data" || exit 3
scp -q "$BO/design_1_150v40.bit" "$BO/design_1_150v40.hwh" "$U@$B:$D/" || exit 4
scp -q "$BO/design_1_150v39.bit" "$U@$B:$D/" || exit 4
scp -q "$PS"/*.py "$PS"/*.c "$U@$B:$D/ps/pynq/" || exit 4
echo "  ✓ 已传"

echo "=== ③ 板端四项健康检查（不全过就不要再往下做实验）==="
ssh -o BatchMode=yes "$U@$B" '
  echo "  df -h /   : $(df -h / | tail -1 | awk "{print \$3\" / \"\$4\" (\"\$5\")\"}")"
  echo "  df -i /   : $(df -i / | tail -1 | awk "{print \$5}")"
  echo -n "  sudo true : "; echo xilinx | sudo -S -p "" true >/dev/null 2>&1 && echo OK || echo "**失败**"
  echo -n "  pynq 导入 : "; /usr/local/share/pynq-venv/bin/python3 -c "from pynq import Overlay" >/dev/null 2>&1 && echo OK || echo "**失败**"
  echo -n "  fpga0     : "; cat /sys/class/fpga_manager/fpga0/state 2>/dev/null || echo "?"
  echo "  CmaFree   : $(grep CmaFree /proc/meminfo | awk "{printf \"%.1f MB\", \$2/1024}")"
'

echo "=== ④ md5 对齐核对（主机 vs 板端）==="
LIST="bin_net.py bin_engine.py bin_ref_numpy.py q8.py q8.c bilinear_c.py bilinear_c.c"
h=$(cd "$PS" && md5sum $LIST 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
b=$(ssh -o BatchMode=yes "$U@$B" "cd $D/ps/pynq && md5sum $LIST 2>/dev/null | awk '{print \$1}' | tr '\n' ' '")
if [ "$h" = "$b" ]; then echo "  ✓ 7 个 PS 文件 md5 全部一致"; else
  echo "  ✗ PS 文件不一致（板端可能是旧版）"; echo "    主机: $h"; echo "    板端: $b"; exit 5; fi
echo "  比特流：$(md5sum "$BO/design_1_150v40.bit" | cut -c1-32)（应与 runs/v40_archive/ 一致）"
echo "=== 完成。下一步：一次 load → 最小检查（只读 IP 基址）→ 再跑整网 ==="
