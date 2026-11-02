# 冻结资产取证（T-107 / v53）

## a) proj_engine / proj_v43base2 在本会话内（23:46 之后）被改过的文件（应为 0）
```
  proj_engine : 0 个
  proj_v43base2 : 0 个
```

## b) lane16 10 个文件的 md5（本会话只合入 1 个：bin_unit_axi_wrap_v50.v）
```
f966667d52c0c48f9d1e1a29b5602c06  lane16/bin_band_buf.v
1b0407bd83056739ebfb8d6b01b60c68  lane16/bin_band_fill_v50.v
e5d7b3b7b4c5791e5a85815d15620c1f  lane16/bin_catpack_v50.v
61568f39a5206e9f1e1c9f3b620dadb3  lane16/bin_conv_core_v50.v
314a6f18e038c9c8cecde09706e1193b  lane16/bin_interp_v50.v
dba8b606acdd9cdd8109a59599ebae71  lane16/bin_rprelu_v50.v
8f242a2758dc715bc6d78a8e198a5a46  lane16/bin_sam_v50.v
266454b87d8b7e4124d1fb1323d78178  lane16/bin_segq.v
9303ad6f61289ec6e7b259210a367bb7  lane16/bin_unit_axi_wrap_bd_v50.v
17164dfc3029ace1cbcb9eaa08af6213  lane16/bin_unit_axi_wrap_v50.v
```

## c) 25 条 v39 基线文件是否被动过（应为 0）
```
  rtl_v50 顶层 .v 共 25 个；本会话 (23:46 后) 被改的：
0
```

## d) BD 产物 md5（未重建，与 v53 构建时同一份）
```
fb9ab3e9d973e633440ee919a87a46cd  /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/vivado/proj_v50/proj_engine.gen/sources_1/bd/design_1/synth/design_1.v
5235764602921d03351e121a81e35613  /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/vivado/proj_v50/proj_engine.gen/sources_1/bd/design_1/hw_handoff/design_1.hwh
97f5e4f2aecd0681b85b78d3cb57cb0e  /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/vivado/proj_v50/proj_engine.gen/sources_1/bd/design_1/ip/design_1_bin_unit_0_18/synth/design_1_bin_unit_0_18.v
c432ed9ef443436a90b4526c92512204  /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/vivado/proj_v50/proj_engine.gen/sources_1/bd/design_1/ip/design_1_axi_ctrl_imp_auto_pc_0/design_1_axi_ctrl_imp_auto_pc_0.dcp
```
