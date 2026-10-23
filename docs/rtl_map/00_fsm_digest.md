# FSM 摘要（自动提取，供后续分析用）

来源: `conv2d_engine.v`（17,588 行，one-hot `ap_CS_fsm` 167 bit，实际用到 82 个状态）

## 状态 → 相位分类

| state | 相位 | 本状态驱动的信号（前 4 个）|
|---:|---|---|
| 1 | LATCH | H_read_reg_7513, Hout_read_reg_7494, W_read_reg_7503, Wout_read_reg_7485 |
| 2 | LATCH | add_ln130_reg_7915 |
| 5 | LATCH | add_ln130_1_reg_7920 |
| 6 | LATCH | add_ln132_reg_7929 |
| 9 | LATCH | add_ln132_1_reg_7934, icmp_ln134_reg_7940, icmp_ln135_reg_7945, reg_4323 |
| 10 | LATCH | empty_103_reg_7953, or_ln137_reg_7962 |
| 11 | LATCH | add_ln140_reg_7966 |
| 14 | LATCH | reg_4319 |
| 15 | LATCH | icmp_ln141_reg_7974 |
| 16 | LATCH | icmp_ln159_reg_7991, span_reg_7981 |
| 17 | LATCH | icmp_ln168_reg_7995 |
| 20 | LATCH | mul_ln169_reg_8008 |
| 21 | LATCH | bwcol_reg_8016 |
| 22 | LATCH | add_ln268_1_reg_8099, gc0_1_fu_1228, grp_conv2d_engine_Pipeline_VITIS_LOOP_257_2_fu_3671_ap_start_reg, icmp_ln474_reg_8095 |
| 23 | LATCH | indvars_iv363_fu_1220 |
| 28 | LATCH | add_ln268_16_reg_8120, empty_108_reg_8125, shl_ln268_reg_8114 |
| 29 | LATCH | icmp_ln286_reg_8135, reg_4327, smax1_reg_8152, smax_reg_8147 |
| 30 | LATCH | icmp_ln244_1_reg_8157, icmp_ln268_reg_8199, p_cast_reg_8184, rdoff_load_reg_8162 |
| 31 | LATCH | add_ln268_3_reg_8269, grp_conv2d_engine_Pipeline_VITIS_LOOP_244_1_fu_3677_ap_start_reg, rdoff_load_1_reg_8225, rdoff_load_2_reg_8237 |
| 32 | LATCH | add_ln268_6_reg_8406, dgc_reg_8284, empty_107_reg_8446, icmp_ln284_reg_8296 |
| 33 | LATCH | add_ln268_14_reg_8477, icmp_ln268_2_reg_8493, retval_3_reg_3626, sub_ln269_reg_8504 |
| 34 | LATCH | icmp_ln273_reg_8552, la_reg_8547, select_ln268_3_reg_8524, select_ln279_1_reg_8557 |
| 35 | LATCH | la_1_reg_8573, select_ln268_4_reg_8562, tmp_43_reg_8591, trunc_ln268_11_reg_8568 |
| 36 | LATCH | add_ln280_1_reg_8612, lb_2_reg_8601, select_ln268_5_reg_8596, trunc_ln341_reg_8607 |
| 37 | LATCH | icmp_ln319_reg_8631, nwide_reg_8624, select_ln279_reg_8635, sub_ln268_9_reg_8617 |
| 38 | LATCH | add_ln279_1_reg_8646 |
| 39 | LATCH | icmp_ln657_reg_8651, select_ln319_reg_8660, sext_ln279_reg_8655 |
| 40 | LATCH | add_ln279_2_reg_8675, and_ln284_reg_8699, need0_reg_8694, y_1_reg_8683 |
| 42 | LATCH | mul_ln280_reg_8710, r_reg_3277 |
| 43 | LATCH | add_ln285_reg_8718, add_ln288_reg_8723, grp_conv2d_engine_Pipeline_VITIS_LOOP_286_6_VITIS_LOOP_287_7_fu_3686_ap_start_reg, trunc_ln293_reg_8733 |
| 45 | LATCH | add_ln304_reg_8741, indvar1858_reg_3355, indvars_iv395_reg_3403, indvars_iv406_reg_3391 |
| 49 | LATCH | ic_1_reg_3310, phi_mul29_reg_3321, trunc_ln304_reg_8760 |
| 50 | LATCH | add_ln314_1_reg_8773, add_ln314_reg_8781, add_ln324_reg_8792, cinb_reg_3343 |
| 51 | LATCH | grp_conv2d_engine_Pipeline_VITIS_LOOP_315_10_fu_3702_ap_start_reg |
| 53 | LATCH | grp_conv2d_engine_Pipeline_VITIS_LOOP_319_11_fu_3716_ap_start_reg |
| 55 | LATCH | add_ln324_3_reg_8800, indvar1835_reg_3289, r_2_reg_3300 |
| 59 | LATCH | mul_ln324_reg_8815 |
| 60 | LATCH | add_ln324_1_reg_8820 |
| 61 | LATCH | grp_conv2d_engine_Pipeline_VITIS_LOOP_372_14_fu_3731_ap_start_reg, trunc_ln1_reg_8831, trunc_ln324_reg_8825 |
| 70 | LATCH | grp_conv2d_engine_Pipeline_VITIS_LOOP_364_13_fu_3746_ap_start_reg |
| 72 | LATCH | add_ln436_4_reg_8849, phi_mul48_reg_3266, row_buf_valid_reg_3253, y_reg_3242 |
| 74 | LATCH | reg_4333 |
| 75 | LATCH | add_ln437_4_reg_8859, icmp_ln437_reg_8869, icmp_ln439_reg_8880, trunc_ln437_reg_8874 |
| 76 | LATCH | add_ln437_1_reg_8902, add_ln437_3_reg_8907, and_ln451_reg_8933, i_1_reg_3447 |
| 77 | LATCH | add_ln517_reg_8945, add_ln566_2_reg_8961, add_ln566_reg_8969, empty_114_reg_8956 |
| 86 | LATCH | add_ln521_reg_8998 |
| 87 | LATCH | trunc_ln528_reg_9008 |
| 88 | LATCH | ap_predicate_pred6180_state89, ap_predicate_pred6249_state89, ap_predicate_pred6318_state89, ap_predicate_pred6387_state89 |
| 89 | WGT | ap_predicate_pred6180_state90, ap_predicate_pred6249_state90, ap_predicate_pred6318_state90, ap_predicate_pred6387_state90 |
| 91 | LATCH | ic_3_reg_3469 |
| 92 | LATCH | add_ln488_1_reg_9084, add_ln488_reg_9092, add_ln491_1_reg_9097 |
| 93 | LATCH | add_ln490_reg_9105, add_ln491_2_reg_9110 |
| 94 | LATCH | gmem0_addr_4_reg_9115, trunc_ln491_reg_9121 |
| 103 | LATCH | ap_predicate_pred6824_state104, ap_predicate_pred6835_state104, ap_predicate_pred6845_state104, ap_predicate_pred6855_state104 |
| 105 | LATCH | ic_5_reg_3504 |
| 108 | LATCH | bound7_reg_9136, grp_conv2d_engine_Pipeline_VITIS_LOOP_475_16_VITIS_LOOP_479_17_fu_3764_ap_start_reg |
| 110 | LATCH | i_4_reg_3538, i_5_reg_3527 |
| 119 | LATCH | add_ln570_reg_9149, add_ln579_reg_9154 |
| 120 | LATCH | trunc_ln580_reg_9164 |
| 121 | LATCH | reg_4388, reg_4401, reg_4414, trunc_ln580_4_reg_9169 |
| 122 | LATCH | trunc_ln580_8_reg_9195 |
| 123 | LATCH | trunc_ln580_11_reg_9200 |
| 124 | LATCH | trunc_ln580_15_reg_9205 |
| 125 | LATCH | reg_4337, reg_4354, reg_4371, trunc_ln580_19_reg_9210 |
| 126 | LATCH | trunc_ln580_23_reg_9215 |
| 127 | LATCH | ic_2_reg_3516 |
| 128 | LATCH | add_ln636_reg_9240, add_ln646_reg_9223, icmp_ln637_reg_9250, icmp_ln647_reg_9233 |
| 129 | LATCH | grp_conv2d_engine_Pipeline_VITIS_LOOP_649_30_fu_3793_ap_start_reg |
| 131 | LATCH | grp_conv2d_engine_Pipeline_VITIS_LOOP_639_28_fu_3807_ap_start_reg |
| 133 | LATCH | add_ln657_reg_9257, mul_ln658_reg_9262, trunc_ln4_reg_9273 |
| 134 | WBUF | add_ln901_3_reg_9285, add_ln901_4_reg_9290, grp_conv2d_engine_Pipeline_VITIS_LOOP_764_38_fu_3829_ap_start_reg, reg_4427 |
| 136 | LATCH | grp_conv2d_engine_Pipeline_VITIS_LOOP_819_41_fu_3935_ap_start_reg |
| 138 | LATCH | grp_conv2d_engine_Pipeline_VITIS_LOOP_684_33_fu_3969_ap_start_reg |
| 140 | LATCH | grp_conv2d_engine_Pipeline_VITIS_LOOP_730_36_fu_4024_ap_start_reg |
| 142 | LATCH | add_ln838_1_reg_9479, add_ln838_reg_9487, grp_conv2d_engine_Pipeline_VITIS_LOOP_849_43_fu_4113_ap_start_reg, grp_conv2d_engine_Pipeline_VITIS_LOOP_865_44_fu_4082_ap_start_reg |
| 144 | LATCH | acc_even_reg_3584, acc_odd_reg_3605 |
| 147 | LATCH | v_reg_9529 |
| 148 | LATCH | tmp_48_reg_9540, v_6_reg_9534 |
| 150 | LATCH | i_6_reg_3561, phi_mul46_reg_3572 |
| 152 | LATCH | grp_conv2d_engine_Pipeline_VITIS_LOOP_945_50_fu_4179_ap_start_reg |
| 158 | LATCH | and_ln963_reg_9550, trunc_ln5_reg_9554 |
| 160 | LATCH | grp_conv2d_engine_Pipeline_VITIS_LOOP_967_51_fu_4195_ap_start_reg |

## 分类统计

| 相位 | 状态数 |
|---|---:|
| LATCH | 80 |
| WBUF | 1 |
| WGT | 1 |
