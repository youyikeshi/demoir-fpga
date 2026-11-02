# 冻结资产取证（T-92）

## a) proj_engine / proj_v43base2 里**本次会话开始后**(17:40)被改过的文件
```
  proj_engine : 0 个
  proj_v43base2 : 0 个
```

## b) lane16 10 个文件的 md5（T-92 只改 bin_unit_axi_wrap_bd_v50.v 的 2 行属性，父代理授权）
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
0f7dd0ae908eebc8c360c0a6f00a4b8e  lane16/bin_unit_axi_wrap_v50.v
```

## c) 25 条 v39 基线文件是否被动过（应为 0）
```
  rtl_v50 顶层 .v 共 25 个；本次会话 (17:40 后) 被改的：
0
```
