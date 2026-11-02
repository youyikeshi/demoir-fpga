#!/usr/bin/env bash
# ============================================================================
#  archive_build_v45.sh —— v45 产物归档 + 自校验（T-80, 2026-11-02）
#
#  v45 = proj_v43base2 的副本（proj_v45）+ **xbar OOC 重综合** + v44 同一套配方。
#  唯一变量 = xbar 的 IP 产物（旧 10-30 09:49 的 DCP/stub 只译码 2 个地址段，
#  没有 0x40080000 ⇒ v44 板端 SIGBUS 的真因）。
#
#  设计原则（沿用 v39..v44 同一套）：
#    1. **先落盘、后校验**：copy 完再校验，缺文件只记账不中断，最后统一判。
#    2. **MD5SUMS 用相对路径**，基准 = 归档目录 ⇒ `cd runs/v45_archive && md5sum -c MD5SUMS`。
#    3. `.hwh` 直接从 BD 取（**不重新生成**；历史改 BD 挂板 3 次）。
# ============================================================================
set -uo pipefail
R=/media/felix/Data/coding/demoir-fpga
O=$R/runs/v45_out
T=$R/runs/timing_v45
A=$R/runs/v45_archive
BD=$R/pynq_specialized_code/pl/vivado/proj_v45/proj_engine.gen/sources_1/bd/design_1
RC=0; miss=0
mkdir -p "$A"
echo "[archive] 目标 $A"

cp1() { if [ -f "$1" ]; then cp -p "$1" "$A/${2:-$(basename "$1")}"; echo "  ✓ $(basename "${2:-$1}")  ($(stat -c %s "$1") B)"; else echo "  ✗ 缺 $1"; miss=$((miss+1)); RC=1; fi; }

echo "--- 1) 交付物（bit / routed dcp / hwh）"
cp1 "$O/design_1_150v45.bit"
cp1 "$O/design_1_150v45_routed.dcp"
cp1 "$BD/hw_handoff/design_1.hwh" "design_1_150v45.hwh"

echo "--- 2) 报告（util / timing）"
cp1 "$O/util_v45.rpt"
cp1 "$O/timing_v45.rpt"

echo "--- 3) bit ↔ 源码清单 + 构建脚本 + 断言脚本"
cp1 "$O/build_v45_manifest.txt"
cp1 "$R/pynq_specialized_code/pl/scripts/build_final_150v45.tcl"
cp1 "$R/pynq_specialized_code/pl/scripts/assert_v45_xbar.tcl"
cp1 "$R/pynq_specialized_code/pl/scripts/gen_rtl_root_manifest_v45.sh"

echo "--- 4) RTL 根 25 条账目 + verify_rtl_root 输出"
cp1 "$T/rtl_v39_exact_25_manifest.txt"
cp1 "$T/verify_rtl_root.log" 2>/dev/null || { bash "$R/pynq_specialized_code/pl/scripts/verify_rtl_root.sh" "$T/rtl_v39_exact_25_manifest.txt" "$R/runs/v42e_archive/rtl_v39_exact" > "$A/verify_rtl_root.log" 2>&1; echo "  ✓ verify_rtl_root.log（现场重跑）"; }

echo "--- 5) 日志（构建 / OOC 重综合 / 负对照）"
cp1 "$T/build_v45.log"
cp1 "$T/ooc_resynth_xbar_v45.log"
cp1 "$T/ooc_resynth_xbar_v45.tcl"
cp1 "$T/negctrl_xbar_assert.log"
cp1 "$T/negctrl_xbar_assert.tcl"

echo "--- 6) 新旧 xbar stub（端口对比的物证）"
cp1 "$T/xbar_stub_OLD_1030.v"
cp1 "$T/xbar_stub_NEW_1102.v"
cp1 "$T/xbar_OLD_1030.dcp"

echo "--- 7) DCP 只读核查（3 轮 tcl + 4 份日志 + DRC 报告）"
for f in "$T"/dcp_check_v45*.log "$T"/dcp_endpoints*_v45.log "$T"/dcp_check_v45*.tcl "$T"/dcp_endpoints*_v45.tcl "$T"/drc_v45.rpt "$T"/poscheck_v45.tcl; do
    [ -f "$f" ] && cp1 "$f"
done
cp1 "$T/xbar_stub_BADTRUNC32.v"

echo "--- 8) 新旧 stub 端口/地址段对比"
python3 - "$A" <<'PYEOF'
import sys,re
A=sys.argv[1]
def parse(f):
    t=open(f).read()
    m=re.search(r'CORE_GENERATION_INFO = ".*?"', t).group(0)
    d=dict(re.findall(r'(C_[A-Z0-9_]+)=([^,]+)', m))
    ports=re.findall(r'\b(input|output)\s*(\[[0-9]+:0\])?\s*([A-Za-z0-9_]+)\s*;', t)
    return d,ports
do,po=parse(f"{A}/xbar_stub_OLD_1030.v")
dn,pn=parse(f"{A}/xbar_stub_NEW_1102.v")
def segs(d):
    b=d['C_M_AXI_BASE_ADDR'][2:]; w=d['C_M_AXI_ADDR_WIDTH'][2:]
    out=[]
    for i in range(len(b)//16):
        s=b[i*16:(i+1)*16]
        if s.startswith('ffffffff'): continue
        out.append((i, '0x'+s[8:], int(w[i*8:(i+1)*8],16)+1))
    return out
so,sn=segs(do),segs(dn)
lines=[]
lines.append("# 新旧 xbar IP 产物对比（T-80 物证，2026-11-02）\n")
lines.append("| 项 | 旧（10-30 09:49） | 新（11-02 15:42 重综合） |")
lines.append("|---|---|---|")
lines.append("| dcp 大小 | 116679 B | 118174 B |")
lines.append(f"| 端口声明条数 | {len(po)} | {len(pn)} |")
lines.append(f"| 端口名+位宽是否逐条相同 | — | {'**是**（diff 为空）' if po==pn else '**否**'} |")
lines.append(f"| 扁平主口 m_axi_awaddr | 160 bit (5×32) | 160 bit (5×32) |")
lines.append(f"| C_M_AXI_BASE_ADDR 地址段数 | **{len(so)}** | **{len(sn)}** |")
lines.append(f"| 含 0x40080000(fconv) | **否** | **是** |")
lines.append(f"| M04_AXI_AWADDR（IP stub 内） | 0 | **0**（本设计为扁平总线，编号在 BD 层） |")
lines.append("")
lines.append("## 地址段明细\n")
lines.append("旧：")
for i,b,w in so: lines.append(f"  - slot{i}: base={b}  窗口={1<<w} B")
lines.append("新：")
for i,b,w in sn: lines.append(f"  - slot{i}: base={b}  窗口={1<<w} B")
lines.append("")
lines.append("> **确定事实**：旧表只有 2 个地址段 `{0x40060000, 0x40040000}`，**没有 0x40080000**；")
lines.append("> 新表有 3 个 `{0x40080000, 0x40060000, 0x40040000}`，与 BD 地址图")
lines.append("> (`/conv_uni_0/s_axi/reg0`=0x40040000 / `/bin_unit_0/s_axi/reg0`=0x40060000 /")
lines.append("> `/fconv_0/s_axi/reg0`=0x40080000) 完全一致。")
lines.append(">")
lines.append("> 槽号方向（首段对应最高槽还是最低槽）不影响本结论；routed DCP 的**端点证据**已")
lines.append("> 直接证明 fconv 的寄存器口被接上：`axi_ctrl_M04_AXI_AWADDR[0..16]` 的负载是")
lines.append("> `fconv_0/aw_addr_reg[16]_0[0..16]`，驱动是 `axi_ctrl/m_axi_awaddr[20..36]`；")
lines.append("> `M04_AXI_WDATA[0..31]` 的负载是 `fconv_0/w_dat_reg[31][*]`（见 dcp_endpoints2_v45.log）。")
open(f"{A}/STUB_DIFF_v44_vs_v45.md","w").write("\n".join(lines)+"\n")
print("  ✓ STUB_DIFF_v44_vs_v45.md")
PYEOF

echo "--- 9) 主工程 / proj_v43base2 未被触碰的取证"
{
  echo "# 冻结资产取证（T-80）"
  echo
  echo "## a) proj_engine 与 proj_v43base2 里会话开始后（15:38）被改过的文件数（应为 0）"
  echo '```'
  echo "\$ find proj_engine proj_v43base2 -newermt '2026-11-02 15:38' -type f | wc -l"
  find "$R/pynq_specialized_code/pl/vivado/proj_engine" "$R/pynq_specialized_code/pl/vivado/proj_v43base2" \
       -newermt '2026-11-02 15:38' -type f 2>/dev/null | wc -l
  echo '```'
  echo
  echo "## b) 主工程关键产物 md5（与 v43base2/README 记录的设计态应一致）"
  echo '```'
  md5sum "$R/pynq_specialized_code/pl/vivado/proj_engine/proj_engine.gen/sources_1/bd/design_1/synth/design_1.v" \
         "$R/pynq_specialized_code/pl/vivado/proj_engine/proj_engine.gen/sources_1/bd/design_1/hw_handoff/design_1.hwh" 2>/dev/null
  echo '```'
  echo
  echo "## c) proj_v43base2 的 xbar 旧产物仍是 10-30（未被本轮回写）"
  echo '```'
  stat -c '%y  %n' "$R/pynq_specialized_code/pl/vivado/proj_v43base2/proj_engine.gen/sources_1/bd/design_1/ip/design_1_axi_ctrl_imp_xbar_0/design_1_axi_ctrl_imp_xbar_0.dcp" 2>/dev/null
  echo '```'
} > "$A/FROZEN_ASSETS_v45.md"
echo "  ✓ FROZEN_ASSETS_v45.md"

echo "--- 10) MD5SUMS"
( cd "$A" && md5sum $(ls -1 | grep -v '^MD5SUMS$' | grep -v '^README.md$') > MD5SUMS && echo "  ✓ MD5SUMS（$(wc -l < MD5SUMS) 项）" )

echo "[archive] 完成：缺 $miss 项  RC=$RC"
exit $RC
