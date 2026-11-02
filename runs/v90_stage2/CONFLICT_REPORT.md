# T-90 撞车证据（2026-11-02 17:27:47）

## 发现
开工检查（17:22）时 md5 = 清单值：segq 191bda10… / wrap 0229c186…
17:26:44 我复制 before 快照时 md5 已变；随后 17:27:17 wrap 仍在增长。

## 时间线（stat）
2026-11-02 17:24:12.408675100 +0800  23686  runs/timing_v50/rtl_v50/lane16/bin_segq.v
2026-11-02 17:27:17.542135600 +0800  215893  runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v
2026-11-02 17:27:24.068327100 +0800  1383  runs/v89_desc/elab.sh

## 现在（17:27:47）
266454b87d8b7e4124d1fb1323d78178  runs/timing_v50/rtl_v50/lane16/bin_segq.v
3e7b6a49ce466b8a2a96ccb20252e53f  runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v

## 已被对方落盘的六条裁定指纹（grep 行号）
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1359:                for (int i = 0; i < 5; i++) sh_dw[i] <= 64'd0;
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1363:                        sh_dw[sh_beat[2:0]] <= u_rd_data;
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1372:        wire [31:0]  sh_in_pa  = sh_dw[0][31:0];     // 字0  in_pa（装载源）
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1373:        wire [31:0]  sh_out_pa = sh_dw[0][63:32];    // 字1  out_pa（排空基址）★ T-89 修的就是这一处
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1374:        wire [15:0]  sh_cin    = sh_dw[1][15:0];     // 字2 低 = cin / cfg_c
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1375:        wire [15:0]  sh_cfg_h  = sh_dw[1][47:32];    // 字3 低 = cfg_h（本带有效输出行数）
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1376:        wire [15:0]  sh_h_in   = sh_dw[1][63:48];    // 字3 高 = H_in（pad 判据）
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1377:        wire [15:0]  sh_w_row  = sh_dw[2][15:0];     // 字4 低 = W（行宽）
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1378:        wire [15:0]  sh_y0     = sh_dw[3][15:0];     // 字6 低 = band_y0
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1379:        wire [31:0]  sh_flags  = sh_dw[3][63:32];    // 字7    = flags（未定义位忽略）
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1380:        wire [31:0]  sh_next   = sh_dw[4][31:0];     // 字8    = next_pa
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1381:        wire [15:0]  sh_nband  = sh_dw[4][47:32];    // 字9 低 = nband
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1382:        wire [15:0]  sh_omask  = sh_dw[4][63:48];    // 字9 高 = op_mask
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1421:        wire [15:0]  c_cfg_cout  = r_cout[15:0];       // 段边界寄存器（PS 写 0x70）
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1716:        wire [31:0] geo_loadw = geo_gin   * {16'd0, fill_rows} * {16'd0, sh_w_row};
runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:1798:            .OP_MASK_SUP (16'h0003)          // op_mask bit0=conv / bit1=cat
runs/timing_v50/rtl_v50/lane16/bin_segq.v:240:        // ★ T-89：必需基址只有两个 —— 字0 in_pa、字1 out_pa。

## 对方活动目录
总计 36
drwxrwxrwx 1 felix felix     0 17:27:35 elab
-rwxrwxrwx 1 felix felix  1383 17:27:24 elab.sh
drwxrwxrwx 1 felix felix  4096 17:24:22 sim
-rwxrwxrwx 1 felix felix  5613 17:21:24 T89_STAGE1_REPORT.md
-rwxrwxrwx 1 felix felix  2649 17:20:09 desc_field_map.csv
-rwxrwxrwx 1 felix felix 13034 17:19:59 AUTHORITATIVE_DESC_FIELDS.md

## 结论：同一批文件（segq/wrap）正在被 runs/v89_desc 的写者修改 ⇒ T-90 按铁规停手，未写任何目标文件。
