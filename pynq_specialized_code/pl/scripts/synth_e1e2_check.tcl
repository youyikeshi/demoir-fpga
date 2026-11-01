# ============================================================================
#  synth_e1e2_check.tcl —— 只综合 bin_e1e2（out_of_context），做 v39/v40 的 A/B 指纹
#
#  目的（不替代完整综合，只为拿三件事）：
#    1. 旧的 p<run_len 比较（issue_en0_carry*）在 v40 里**是不是真的消失了**
#    2. rem / sw_act_q 两个新寄存器在不在网表里
#    3. LUT / FF 的净变化（等价改写不该大幅变面积）
#
#  用法（两个版本各跑一次）：
#    E1E2_SRC=<path>/bin_e1e2.v E1E2_TAG=v39 tools/memgate.sh 4 -- \
#        pl/scripts/vrun.sh pl/scripts/synth_e1e2_check.tcl runs/timing_v40/e1e2_synth_v39.log 900
# ============================================================================
source [file join [file dirname [info script]] _guard_build.tcl]
set_param general.maxThreads 2

set src $::env(E1E2_SRC)
set tag $::env(E1E2_TAG)
puts "== E1E2 out-of-context 综合指纹：tag=$tag  src=$src"
read_verilog -sv $src
synth_design -top bin_e1e2 -mode out_of_context -part xc7z015clg485-2

set nlut [llength [get_cells -hier -quiet -filter {REF_NAME =~ LUT*}]]
set nff  [llength [get_cells -hier -quiet -filter {IS_SEQUENTIAL}]]
set ncarry [llength [get_cells -hier -quiet -filter {REF_NAME =~ CARRY4}]]
set nold [llength [get_cells -hier -quiet -filter {NAME =~ *issue_en0_carry*}]]
set nrem [llength [get_cells -hier -quiet -filter {NAME =~ *rem_reg*}]]
set nsw  [llength [get_cells -hier -quiet -filter {NAME =~ *sw_act_q_reg*}]]
set nlat [llength [get_cells -hier -quiet -filter {REF_NAME == LDCE || REF_NAME == LDPE}]]
puts "FINGERPRINT\ttag=$tag\tLUT=$nlut\tFF=$nff\tCARRY4=$ncarry\tissue_en0_carry=$nold\trem=$nrem\tsw_act_q=$nsw\tlatch=$nlat"
if {$nold == 0 && $nrem > 0 && $nsw > 0} { puts "VERDICT\ttag=$tag\tV40_SHAPE_OK" } else { puts "VERDICT\ttag=$tag\tSHAPE_CHECK" }
puts "== DONE_E1E2_SYNTH =="
