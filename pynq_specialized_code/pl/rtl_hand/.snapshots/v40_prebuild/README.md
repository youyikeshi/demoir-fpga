# v40_prebuild —— v39 + 诚实时钟约束 + 两处时序刀（**已过 xsim/综合门禁，未过 P&R**）

状态：**RTL/sim/综合三门已实测通过；P&R 尚未跑**（等批准）。不要把这个目录当成 verified。

## 文件 md5

    bin_e1e2.v               bc28ead39b41e79f...   (v39 28375eec288f + 时序刀 7)
    bin_unit_axi_wrap.v      4c784f73263e6973...   (v39 0cf68b2e31ac + 时序刀 8)
    build_final_150v40.tcl   9af306be9483c589     (v39 f8c531f0984550b6 + 6.667 ns)

## 改了什么（一句话版）

1. **build_final_150v40.tcl**：`create_clock -period 10.000` → **`6.667`**，并删掉
   "约束与实际频率解耦"那段错误注释，改写为"为什么 10.000 是错的 + 实测证据"，
   外加**周期硬断言**（改回宽松值 build 直接失败）。
2. **bin_e1e2.v 时序刀 7**：`issue_en = (p < run_len)` → **边界装载的倒计数器 `rem`
   + 寄存使能 `sw_act_q`**（等价改写，拍数不变），并在 `translate_off` 里加 5 条不变量断言。
3. **bin_unit_axi_wrap.v 时序刀 8**：`run_unit_q / sel_e1e2_q / sel_cp_q / start_fire`
   加 `(* max_fanout = 32 *)`（纯复制，语义零变化）。

## 已跑过的验证（全部本机实测，2026-10-31 19:2x）

    xvlog --sv 8 个文件                0 ERROR
    门禁A E1E2 直接挂单元              106/106 命令、24 层、6,302,720 B、
                                       C 差异 0 / ref 差异 0、
                                       **cycles 7,244,033（与 v39 逐命令相同）**
                                       断言命中 0 条
    门禁B verify_all.sh wrap 12 shapes 12/12 PASS，逐字节 0 差，
                                       cycles = 1429/2151/5329/3788/12713/
                                                4935/7076/3863/2921/5048/7284/2724
                                       （12 个与 v39 基线**逐个相同**）
    synthcheck.sh                      多驱动 0 / 组合环 0 / ERROR 0 /
                                       8-4767 = 4（arq_addr_reg, arq_id_reg,
                                       pf_tag_reg, pw_reg —— 与 v39 同集合）
    bin_e1e2 OOC 综合 A/B              LUT 1085→1066、CARRY4 183→177、FF 677→679、
                                       latch 0、rem/sw_act_q 各就位

## 还没跑（= 这次要请示的）

    v40 完整 P&R（约 25~40 min）→ WNS/TNS/端点、Latch=0、manifest、上板 bit

## 复现命令

    cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code
    tools/memgate.sh 8 -- pl/scripts/vrun.sh \
        pl/scripts/build_final_150v40.tcl /media/felix/Data/coding/demoir-fpga/runs/timing_v40/build_v40.log 5400
