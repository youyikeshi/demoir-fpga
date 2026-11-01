# ============================================================================
#  place 前的 pblock（两个 IP 用**不同的 mode**，这是实测出来的结论）
#  ----------------------------------------------------------------------------
#  机理差异决定了 mode 不同：
#    k3 的关键路径是  LUT → BRAM 地址线   ⇒ 只钉 DSP（mem_dsp）就够
#    k1 的关键路径是  BRAM → DSP 数据线   ⇒ 必须 BRAM+DSP 一起钉（mem）
#
#  实测（同检查点、同流程、各 3.5 分钟）：
#    k3 mem_dsp          WNS -1.592  route 4.370  10 级
#    k3 mem  (BRAM+DSP)  WNS -1.719  route 4.381  12 级
#    k3 mem_bram(只BRAM) WNS -1.890  比基线还差
#    k1 mem  (BRAM+DSP)  内部路径 route 2.749 → 2.124 (-23%), slack +0.447  ← 必须这个
#    k1 mem_dsp(只 DSP)  WNS -2.033  比不约束还差
#
#  分区（互不重叠，都已过闸门）：
#    k1 → CLOCKREGION_X0Y0      5 BRAM/20, 33 DSP/40  (82.5%)
#    k3 → CLOCKREGION_X1Y0:Y2   0 BRAM（不钉）, 74 DSP/120 (62%)
#
#  ⚠ 过滤必须用 REF_NAME（原语名字来自 RTL 对象，不含 "RAMB"/"DSP" 字样）
#     第一版写成 NAME =~ *RAMB* 只收进 33 个，等于几乎没约束
# ============================================================================
puts "== pblock_pre: k1=DSP+BRAM @X0Y0, k3=DSP @X1Y0:Y2 =="

proc _pb {name region refs nameglob} {
    if {[llength [get_pblocks -quiet $name]]} { delete_pblock [get_pblocks $name] }
    set cells {}
    foreach r $refs {
        foreach c [get_cells -quiet -hier -filter "REF_NAME =~ $r && NAME =~ $nameglob"] {
            lappend cells $c
        }
    }
    if {[llength $cells] == 0} {
        puts "== pblock_pre: $name 未找到单元，跳过（不约束）=="
        return
    }
    create_pblock $name
    add_cells_to_pblock [get_pblocks $name] $cells
    resize_pblock [get_pblocks $name] -add $region
    puts "== pblock_pre: $name 收进 [llength $cells] 个原语 → $region =="
}

# k1：**不再钉**。2026-10-27 实测：k1 mem 与不钉的差别在 ±0.15 ns 噪声内
#     （−1.559 vs −1.694，TNS −2,912 vs −4,320），却在这次构建里造成
#     33 条 net 布线冲突（DRC RTSTAT-6 部分冲突）导致 bitgen 失败。
#     ⇒ 收益不确定、风险确定的约束，去掉。
puts "== pblock_pre: k1 不约束（拥塞源，实测无收益）=="
# k3：只钉 DSP（路径是 LUT→BRAM）
_pb pb_k3_dsp     {CLOCKREGION_X1Y0:CLOCKREGION_X1Y2} {DSP48*} *k3_wrap*
