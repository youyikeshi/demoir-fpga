# v50 BD 重建后 .hwh 四件事核对（T-92, 2026-11-02 17:51:50）

hwh: pynq_specialized_code/pl/vivado/proj_v50/proj_engine.gen/sources_1/bd/design_1/hw_handoff/design_1.hwh
md5: 7984efa8f9556afa2ca8b6c741562664   (重建前 6b552758649abb2ae58547709de0cb4c = v43base2 8-lane 快照)
size: 189748 B

## ① bin_unit 的 module-ref 必须是 bin_unit_axi_wrap_bd_v50（不是 bin_unit_axi_wrap_bd）
```
--- 命中计数 ---
  bin_unit_axi_wrap_bd_v50 : 2
  bin_unit_axi_wrap_bd(裸)  : 0
```

## ② 地址：conv_uni_0 = 0x40040000 / bin_unit_0 = 0x40060000
```
  C_BASEADDR = 0x40060000
  C_BASEADDR = 0x40040000
  C_BASEADDR = 0x00000000
  C_HIGHADDR = 0x4007FFFF
  C_HIGHADDR = 0x4005FFFF
  C_HIGHADDR = 0x3FFFFFFF
```

## ③ 没有 fconv_0（本轮不带 fconv）
  fconv 命中 = 0

## ④ S_AXI_HP2 命中 = 0（v50 本轮不用 HP2）
  PCW_USE_S_AXI_HP0 = 1
  PCW_USE_S_AXI_HP1 = 1
  PCW_USE_S_AXI_HP2 = 0
  PCW_USE_S_AXI_HP3 = 0
