# v50 构建阻断取证（T-92, 2026-11-02 18:22:00）

构建日志：runs/timing_v50/build_t92.log
构建在 place_design 前被 DRC 拦死（4 Errors），**未产出 bit / routed DCP**。

## ① 合成时 VLANES = **8**，不是 16（BD 层从没设过）
```
x_ipName=bin_unit_axi_wrap_bd_v50,x_ipVersion=1.0,x_ipCoreRevision=1,x_ipLanguage=VERILOG,x_ipSimLanguage=MIXED,AXI_ID_W=4,VLANES=8,FUSE_EN=1,BAND_ROWS=8,BAND_HALO=1,BAND_C_MAX=48,BAND_W_MAX=256,BAND_BUDGET_KB=270,V50_REG_BASE=0x0300}
197:    .VLANES(8),
  bd_v50.tcl 里对 CONFIG.VLANES 的 set_property 次数: 0
  外壳 bin_unit_axi_wrap_bd_v50.v 的 VLANES 默认值: parameter int VLANES         = 8
  ⇒ 这一版即便建成也是 **8-lane**，Z4（g_lane=16）必然失败
```

## ② 资源超器件（DRC UTLZ-1，4 条 Error）
```
LUT as Distributed RAM over-utilized in Top Level Design (This design requires more LUT as Distributed RAM cells than are available in the target device. This design requires 83624 of such cell types but only 14400 compatible sites are available in the target device. Please analyze your synthesis results and constraints to ensure the design is mapped to Xilinx primitives as expected. If so, please consider targeting a larger device. Please set tcl parameter "drc.disableLUTOverUtilError" to 1 to change this error to warning.
LUT as Logic over-utilized in Top Level Design (This design requires more LUT as Logic cells than are available in the target device. This design requires 62332 of such cell types but only 46200 compatible sites are available in the target device. Please analyze your synthesis results and constraints to ensure the design is mapped to Xilinx primitives as expected. If so, please consider targeting a larger device. Please set tcl parameter "drc.disableLUTOverUtilError" to 1 to change this error to warning.
LUT as Memory over-utilized in Top Level Design (This design requires more LUT as Memory cells than are available in the target device. This design requires 83690 of such cell types but only 14400 compatible sites are available in the target device. Please analyze your synthesis results and constraints to ensure the design is mapped to Xilinx primitives as expected. If so, please consider targeting a larger device. Please set tcl parameter "drc.disableLUTOverUtilError" to 1 to change this error to warning.
RAMD64E over-utilized in Top Level Design (This design requires more RAMD64E cells than are available in the target device. This design requires 83624 of such cell types but only 14400 compatible sites are available in the target device. Please analyze your synthesis results and constraints to ensure the design is mapped to Xilinx primitives as expected. If so, please consider targeting a larger device.

  器件 xc7z015clg485-2 上限：LUT 46,200 / LUT-as-Memory(SLICEM) 14,400 / BRAM 95 / DSP 160
  本次（VLANES=8）：LUT as Logic 62,332（缺 16,132）/ LUT as Memory 83,690（缺 89,690）/ RAMD64E 83,624 / DSP 112
```

## ③ 为什么 LUT-as-Memory 会到 83,624（≈5.35 Mbit 分布式 RAM）
```

    // ========================================================================
    // 1. 存储阵列：2 个带，每个带 SLOT_WORDS 个字 + 1 个金丝雀字
    //    64-bit 读、字节使能写 ⇒ Vivado 推断为 BRAM（不是 FF）
    // ========================================================================
    logic [63:0] mem [0:WORDS_TOTAL-1];

    // ========================================================================
    // 2. 角色寄存器（双缓冲指针）与粘滞错误
    // ========================================================================
    wire         rbank = bank_rd_sel;
  ↑ mem 数组**没有 ram_style 属性** ⇒ Vivado 推断成分布式 RAM，
    且多读口会被复制多份（83,624 / 30,722 ≈ 2.7×，与读口数吻合）。
  WORDS_TOTAL = 2*(NGRP*ROWS_ALLOC*W_MAX + 1) = 2*(6*10*256+1) = 30,722 words × 64 bit = 1.97 Mbit
  若强制 BRAM：1.97 Mbit / 36 Kbit ≈ 55 个 BRAM36（器件有 95）⇒ **带宽这条路本身放得下**
  但 LUT as Logic 62,332 仍超 46,200 —— 这一项与 band buffer 无关，是逻辑本身太大
```
