// ============================================================================
//  bin_band_buf.v —— v50 片上 band 缓冲（R3）
//  owner: IMP-B   契约：docs/V50_ARCHITECTURE.md §2 D1/D4、§3 契约 C、§5 ⑨⑩⑪
// ----------------------------------------------------------------------------
//  1. 干什么
//     conv 的输出不再写 DDR，而是写进**片上 band 缓冲**；下一层/下一步
//     （pack = E1E2、小算子）从 band 读。于是中间张量**一次 DDR 都不落**（D1）。
//     双缓冲（计算带 / 输出带）让"第 b 带在算"与"第 b-1 带在排空/被读"重叠（D4）。
//
//  2. 布局（契约 C，与 C8 一致）
//        一个带 = NGRP 个通道组 × (BAND_ROWS + 2*HALO_ROWS) 行 × W 列 × 8 通道
//        字节地址 = grp*(rows*W*8) + r*(W*8) + x*8 + (c%8)
//    即 band[grp][r][x][c%8]：**元素 = int8 = 1 通道**（契约 C），同一个 64-bit
//    字里的 8 个字节 = 同一像素的 8 个通道（C8 交错）——这样 conv 核的 `act_c`
//    （一个字喂全部 lane，D2）可以**零改动**地从 band 取数，只需把 base 设 0、
//    把 cfg_h 设成带内行数。
//    ⚠ 契约 C 的 "band[r][c][x] 行主序、通道最内" 若按字面读成 x 最内，64-bit
//      字就会跨 8 个**列**、同一像素的 8 个通道被拆到 8 个字里 ⇒ 与 D2 的
//      "共享读"、与 C8 布局直接冲突。这里取唯一自洽解："通道最内、列次之"。
//      该措辞歧义已登记到 REPORT 的 B5 待确认项。
//
//  3. 端口（物理上 1 读口 + 1 写口；读 = BRAM 语义，t 拍给地址、t+1 拍出数据，
//     与四个单元的 mem_* 契约逐位同构；写 = 64-bit 字 + 8 位字节使能）
//        A  单元读   （conv 读"计算带" = 上一层输出）
//        B  单元写   （conv 写"输出带"）
//        C  pack 读  （E1E2 从计算带读，取代 DDR 往返）
//        D  小算子原地读写（sam/interp/rprelu 的 band 通路；同带读改写）
//        E  DMA 装载/排空（DDR↔band，由 bin_segq 驱动）
//     读口优先级 E(排空) > C(pack) > A(单元) > D(小算子)
//     写口优先级 E(装载) > B(单元) > D(小算子)
//     优先级成立的前提是**相位互斥**（见 §5 相位表）；真的撞了 ⇒ 粘滞
//     arb_conflict + 仿真期 $error（正常 0）。
//
//  4. 不变量
//     ⑩ 双缓冲切换时不许读到半写带：带选择由上层按相位给（bank_rd_sel /
//        bank_wr_sel），本模块做**硬件兜底**——换带那一拍若写还没静默，置
//        arb_conflict（粘滞）+ 仿真期 $error，绝不静默放过去。
//     ⑪ 出错即停后必须可重启：`buf_reset` 把角色复位成 (rd=带0, wr=带1)、
//        清全部粘滞错误、重写金丝雀 ⇒ 上层拿到的是**确定的**初始态。
//     ⑤ `wr_all_idle` = 本拍与上一拍都没有写被接受（BRAM 写口有一拍出口）。
//
//  5. 相位表（谁在什么时候读写哪个带 —— 仲裁优先级成立的前提）
//        相位    读带0                写带0               读带1          写带1
//        LOAD    —                    E(DMA 装)           —             —
//        RUN     A/C/D（单元/pack）   B/D（单元）         E(排空)        —
//        SWAP    —                    —                   —             —
//     ⇒ "同拍读 0 写 0" 是常态（单元读上一层、写本层）⇒ 1R1W 就够；
//       "同拍读 0 又读 1" 永不发生（E 与 A/C 分属不同带、不同相位）。
//
//  6. 容量（B2 验收）
//     参数化 BAND_ROWS / HALO_ROWS / C_MAX / W_MAX；合计超出 `BUDGET_KB` 时
//     **elaboration 期就报错**（§7 的缺模块名 + initial/$fatal 双保险），
//     绝不静默截断。
// ============================================================================
`timescale 1ns / 1ps

module bin_band_buf #(
    parameter int  BAND_ROWS  = 8,     // 每带计算行数（D4 默认 8）
    parameter int  HALO_ROWS  = 1,     // 单侧 halo 行数（段内 Σ(k_i-1) 的一半）
    parameter int  C_MAX      = 48,    // 通道上限（256² 下 C=48 是最大需求）
    parameter int  W_MAX      = 256,   // 宽上限
    parameter int  BUDGET_KB  = 270,   // BRAM 预算（D4：60 × 36 Kb ≈ 270 KB）
    parameter bit  CANARY_EN  = 1'b1   // 带尾金丝雀字（8 B，0xA5..）
)(
    input  logic        clk,
    input  logic        rst_n,

    // ---- 形状配置（每带实际使用；必须 ≤ 参数上限，否则 cap_err）----
    input  logic [15:0] cfg_c,          // 实际通道数（必须 %8==0，C8 布局）
    input  logic [15:0] cfg_w,          // 实际宽

    // ---- 控制 ----
    input  logic        buf_reset,      // 出错/重启：回可重启态（契约 ⑪）
    // 带选择：由上层按**相位**给（LOAD/RUN/DRAIN），本模块不做 ping-pong 状态机
    //   · LOAD  ：wr=bank0（输入落带0）
    //   · RUN   ：rd=bank0，wr=bank1（读上一层、写本层）
    //   · DRAIN ：rd=bank1（把算好的带排空回 DDR）
    // 契约 ⑩（切换时不许读到半写带）由**上层**保证：RUN 只在 LOAD 完成后开始，
    // DRAIN 只在 RUN 完成且 wr_all_idle 后开始（bin_unit_axi_wrap_v50 §10.5）。
    input  logic        bank_rd_sel,    // 读口看到的带
    input  logic        bank_wr_sel,    // 写口落到的带
    output logic        wr_all_idle,    // 无在飞写（契约 ⑤ 的 band 侧）
    output logic        cap_err,        // 容量/形状越界（粘滞；buf_reset 清；
                                        // ★ 契约 R3 §12.2：形状未装载时**不置位**）
    output logic        canary_err,     // 金丝雀被踩（粘滞）
    output logic        arb_conflict,   // 同拍两个写口真的撞了（粘滞，正常为 0）

    // ---- 端口 A：单元读（conv 读计算带）----
    input  logic        a_rd_en,
    input  logic [31:0] a_rd_addr,
    output logic [63:0] a_rd_data,

    // ---- 端口 B：单元写（conv 写输出带；64-bit + 字节使能）----
    input  logic        b_wr_en,
    input  logic [31:0] b_wr_addr,
    input  logic [63:0] b_wr_data,
    input  logic [7:0]  b_wr_sel,

    // ---- 端口 C：pack 读（E1E2 读计算带）----
    input  logic        c_rd_en,
    input  logic [31:0] c_rd_addr,
    output logic [63:0] c_rd_data,

    // ---- 端口 D：小算子原地读写（同一带）----
    input  logic        d_rd_en,
    input  logic [31:0] d_rd_addr,
    output logic [63:0] d_rd_data,
    input  logic        d_wr_en,
    input  logic [31:0] d_wr_addr,
    input  logic [63:0] d_wr_data,
    input  logic [7:0]  d_wr_sel,

    // ---- 端口 E：DMA（装载 DDR→band / 排空 band→DDR）----
    input  logic        e_rd_en,
    input  logic [31:0] e_rd_addr,
    output logic [63:0] e_rd_data,
    input  logic        e_wr_en,
    input  logic [31:0] e_wr_addr,
    input  logic [63:0] e_wr_data,
    input  logic [7:0]  e_wr_sel
);

    // ========================================================================
    // 0. 容量核算（全部是 elaboration 期常量）
    // ========================================================================
    localparam int     NGRP         = (C_MAX + 7) / 8;             // 通道组数
    localparam int     ROWS_ALLOC   = BAND_ROWS + 2 * HALO_ROWS;   // 带内实际行数
    localparam longint SLOT_WORDS   = longint'(NGRP) * ROWS_ALLOC * W_MAX;
    localparam longint GUARD_WORDS  = CANARY_EN ? 1 : 0;
    localparam longint BANK_STRIDE  = SLOT_WORDS + GUARD_WORDS;    // 每带（含金丝雀）
    localparam longint CAP_BYTES    = 2 * BANK_STRIDE * 8;         // 双缓冲总字节
    localparam longint BUDGET_BYTES = longint'(BUDGET_KB) * 1024;
    localparam longint WORDS_TOTAL  = 2 * BANK_STRIDE;
    localparam int     WIDX_W       = $clog2(WORDS_TOTAL) + 1;     // 存储字索引位宽
    localparam int     BOFF_W       = $clog2(SLOT_WORDS) + 1;      // 带内字偏移位宽

    localparam [WIDX_W-1:0] BANK_STRIDE_C = BANK_STRIDE;
    localparam [WIDX_W-1:0] SLOT_WORDS_C  = SLOT_WORDS;
    localparam [63:0]       CANARY_W      = 64'hA5A5_A5A5_A5A5_A5A5;

    // ========================================================================
    // 1. 存储阵列：2 个带，每个带 SLOT_WORDS 个字 + 1 个金丝雀字
    //    64-bit 读、字节使能写 ⇒ Vivado 推断为 BRAM（不是 FF）
    // ========================================================================
    logic [63:0] mem [0:WORDS_TOTAL-1];

    // ========================================================================
    // 2. 角色寄存器（双缓冲指针）与粘滞错误
    // ========================================================================
    wire         rbank = bank_rd_sel;
    wire         wbank = bank_wr_sel;
    logic        cap_err_q, canary_err_q, arb_conf_q;
    logic [1:0]  wr_idle_q;
    logic        rd_sel_q, wr_sel_q;     // 用于发现"带选择在写没停时就换"

    assign cap_err     = cap_err_q;
    assign canary_err  = canary_err_q;
    assign arb_conflict= arb_conf_q;
    assign wr_all_idle = (wr_idle_q == 2'd0);

    // ========================================================================
    // 3. 形状合法性（每拍检查，粘滞到 buf_reset）
    //    · cfg_c 必须是 8 的倍数（契约 C：1 字节 = 1 通道，C8 布局）
    //    · cfg_c ≤ C_MAX、1 ≤ cfg_w ≤ W_MAX
    // ========================================================================
    wire shape_bad = (cfg_c == 16'd0) || (cfg_c[2:0] != 3'd0)
                  || (cfg_c > 16'(C_MAX)) || (cfg_w == 16'd0) || (cfg_w > 16'(W_MAX));

    // ★ 契约 R3 §12.2（2026-11-02，T-83 实测 0x0403 的根因）：
    //   **"形状还没装载"不是"形状配错了"。**
    //   复位态 `cfg_c=cfg_w=0` 让 `shape_bad` 恒真 ⇒ `cap_err` 粘滞 ⇒
    //   `band_bad` ⇒ `bin_segq` 进 `S_ERR`（err=1, code=4=E_CANARY）⇒
    //   空闲态读 `0x310` 得到 `0x0403`。那次报错是**假**的：
    //   kick 之前根本没有"带"这个东西，形状字段还没被描述符填进来。
    //   ⇒ 用 `cfg_valid` 门控：形状**未装载**（任一边为 0）时一律不置
    //     `cap_err`；一旦两个字段都非 0（= 描述符已装载/正在装载），
    //     所有越界判据**一个不少**地照旧生效。
    //   为什么是"两个都非 0"而不是"第一次 kick 之后"：
    //     kick 后的头几十拍里描述符还没读完（`seg_cin/seg_w` 仍是 0），
    //     用 kick 做门控会把这段瞬态重新判成错 ⇒ 真跑会被假 E_CANARY 打死。
    //     "形状字段非 0"才是物理上"带已存在"的判据。
    //   ⚠ 零形状（cin=0 / W=0）的真实错误由 `bin_segq` 在 S_CHK 独立拦住
    //     （`band_words==0` 或 `w9[15:0]==0` ⇒ E_SIZE），**不是漏检**。
    wire cfg_valid = (cfg_c != 16'd0) && (cfg_w != 16'd0);

    // ========================================================================
    // 4. 读口仲裁（物理 1 个读口）
    //    优先级 E(排空) > C(pack) > A(单元) > D(小算子)
    // ========================================================================
    logic        rd_gnt_e, rd_gnt_c, rd_gnt_a, rd_gnt_d;
    logic [31:0] rd_addr_m;
    logic        rd_en_m;
    logic [1:0]  rd_tag;                // 0=E 1=C 2=A 3=D

    assign rd_gnt_e = e_rd_en;
    assign rd_gnt_c = !e_rd_en && c_rd_en;
    assign rd_gnt_a = !e_rd_en && !c_rd_en && a_rd_en;
    assign rd_gnt_d = !e_rd_en && !c_rd_en && !a_rd_en && d_rd_en;

    always_comb begin
        rd_addr_m = 32'd0;
        rd_en_m   = 1'b1;
        rd_tag    = 2'd0;
        if      (rd_gnt_e) begin rd_addr_m = e_rd_addr; rd_tag = 2'd0; end
        else if (rd_gnt_c) begin rd_addr_m = c_rd_addr; rd_tag = 2'd1; end
        else if (rd_gnt_a) begin rd_addr_m = a_rd_addr; rd_tag = 2'd2; end
        else if (rd_gnt_d) begin rd_addr_m = d_rd_addr; rd_tag = 2'd3; end
        else begin rd_addr_m = 32'd0; rd_en_m = 1'b0; end
    end

    // 读地址合法性：64-bit 字对齐（[2:0]==0），且带内不越界
    wire [BOFF_W-1:0] rd_woff      = rd_addr_m[BOFF_W+2:3];
    wire              rd_align_bad = (rd_addr_m[2:0] != 3'd0);
    wire              rd_range_bad = ({1'b0, rd_woff} >= {1'b0, SLOT_WORDS_C[BOFF_W-1:0]});
    wire [WIDX_W-1:0] rd_base      = rbank ? BANK_STRIDE_C : {WIDX_W{1'b0}};
    wire [WIDX_W-1:0] rd_idx       = rd_base + {{(WIDX_W-BOFF_W){1'b0}}, rd_woff};

    // 读数据：单读口 + 一拍寄存 + tag 流水（= BRAM 语义，t+1 拍数据）
    logic [63:0] rd_data_q;
    logic [1:0]  rd_tag_q;
    logic        rd_data_v_q;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rd_data_q   <= 64'd0;
            rd_tag_q    <= 2'd0;
            rd_data_v_q <= 1'b0;
        end else begin
            rd_data_v_q <= rd_en_m && !rd_range_bad;
            rd_tag_q    <= rd_tag;
            if (rd_en_m) rd_data_q <= rd_range_bad ? 64'd0 : mem[rd_idx];
        end
    end

    // 四个口按 tag 各自取数（组合 mux：延迟 = 1 拍，与单元契约逐位同构）
    assign a_rd_data = (rd_data_v_q && (rd_tag_q == 2'd2)) ? rd_data_q : 64'd0;
    assign c_rd_data = (rd_data_v_q && (rd_tag_q == 2'd1)) ? rd_data_q : 64'd0;
    assign d_rd_data = (rd_data_v_q && (rd_tag_q == 2'd3)) ? rd_data_q : 64'd0;
    assign e_rd_data = (rd_data_v_q && (rd_tag_q == 2'd0)) ? rd_data_q : 64'd0;

    // ========================================================================
    // 5. 写口仲裁（物理 1 个写口）
    //    优先级 E(装载) > B(单元) > D(小算子)
    // ========================================================================
    logic        wr_gnt_e, wr_gnt_b, wr_gnt_d;
    logic [31:0] wr_addr_m;
    logic [63:0] wr_data_m;
    logic [7:0]  wr_sel_m;
    logic        wr_en_m;

    assign wr_gnt_e = e_wr_en;
    assign wr_gnt_b = !e_wr_en && b_wr_en;
    assign wr_gnt_d = !e_wr_en && !b_wr_en && d_wr_en;

    always_comb begin
        wr_addr_m = 32'd0;
        wr_data_m = 64'd0;
        wr_sel_m  = 8'd0;
        wr_en_m   = 1'b1;
        if      (wr_gnt_e) begin wr_addr_m = e_wr_addr; wr_data_m = e_wr_data; wr_sel_m = e_wr_sel; end
        else if (wr_gnt_b) begin wr_addr_m = b_wr_addr; wr_data_m = b_wr_data; wr_sel_m = b_wr_sel; end
        else if (wr_gnt_d) begin wr_addr_m = d_wr_addr; wr_data_m = d_wr_data; wr_sel_m = d_wr_sel; end
        else begin wr_addr_m = 32'd0; wr_data_m = 64'd0; wr_sel_m = 8'd0; wr_en_m = 1'b0; end
    end

    wire [BOFF_W-1:0] wr_woff      = wr_addr_m[BOFF_W+2:3];
    wire              wr_align_bad = (wr_addr_m[2:0] != 3'd0);
    wire              wr_range_bad = ({1'b0, wr_woff} >= {1'b0, SLOT_WORDS_C[BOFF_W-1:0]});
    wire [WIDX_W-1:0] wr_base      = wbank ? BANK_STRIDE_C : {WIDX_W{1'b0}};
    wire [WIDX_W-1:0] wr_idx       = wr_base + {{(WIDX_W-BOFF_W){1'b0}}, wr_woff};

    // 同拍多个写口请求（= 相位表被破坏）
    wire wr_two = (e_wr_en && b_wr_en) || (e_wr_en && d_wr_en) || (b_wr_en && d_wr_en);

    // ---- 金丝雀初始化：buf_reset 后两拍把两只带的哨兵字写成 0xA5.. ----
    logic [1:0]  cinit;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)        cinit <= 2'd0;
        else if (buf_reset) cinit <= 2'd3;
        else if (cinit != 2'd0) cinit <= cinit - 2'd1;
    end
    wire              cinit_wr  = (cinit != 2'd0);
    wire [WIDX_W-1:0] cinit_idx = (cinit == 2'd3) ? SLOT_WORDS_C : (SLOT_WORDS_C + BANK_STRIDE_C);

    // 字节使能写（**不进复位域**：BRAM 写口带复位会拒推断，见 v39 core8:234-259）
    always_ff @(posedge clk) begin
        if (cinit_wr) begin
            mem[cinit_idx] <= CANARY_W;
        end else if (wr_en_m && !wr_range_bad) begin
            for (int i = 0; i < 8; i++) begin
                if (wr_sel_m[i]) mem[wr_idx][i*8 +: 8] <= wr_data_m[i*8 +: 8];
            end
        end
    end

    // ========================================================================
    // 6. 控制：切换 / 复位 / 写静默 / 金丝雀校验
    // ========================================================================
    // 写静默：任何写被接受 ⇒ 归 2；否则每拍 -1 到 0（BRAM 写口有一拍出口）
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) wr_idle_q <= 2'd0;
        else if (wr_en_m) wr_idle_q <= 2'd2;
        else if (wr_idle_q != 2'd0) wr_idle_q <= wr_idle_q - 2'd1;
    end

    // 带选择变更：检测"写还没静默就换带"（= 契约 ⑩ 的违例），并顺带校验哨兵字
    wire int_sel_chg = (rd_sel_q != rbank) || (wr_sel_q != wbank);

    wire [WIDX_W-1:0] canary0_idx = SLOT_WORDS_C;
    wire [WIDX_W-1:0] canary1_idx = SLOT_WORDS_C + BANK_STRIDE_C;
    wire              canary_bad_now = CANARY_EN && int_sel_chg &&
                                       ((mem[canary0_idx] != CANARY_W) ||
                                        (mem[canary1_idx] != CANARY_W));

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rd_sel_q <= 1'b0; wr_sel_q <= 1'b0;
        end else if (buf_reset) begin
            rd_sel_q <= 1'b0; wr_sel_q <= 1'b0;
        end else begin
            rd_sel_q <= rbank;   // 上一拍的带选择（下一拍比较）
            wr_sel_q <= wbank;
        end
    end

    // 契约 ⑩ 的硬件兜底：换带那一拍写还没静默 ⇒ 上层读到了半写带
    wire role_bad_now = int_sel_chg && (wr_idle_q != 2'd0);

    // 粘滞错误（每个信号只在本块驱动）
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cap_err_q <= 1'b0; canary_err_q <= 1'b0; arb_conf_q <= 1'b0;
        end else if (buf_reset) begin
            cap_err_q <= 1'b0; canary_err_q <= 1'b0; arb_conf_q <= 1'b0;
        end else begin
            // ★ 契约 R3 §12.2：`cfg_valid=0`（形状未装载）⇒ 不置 cap_err
            if (cfg_valid && (shape_bad || rd_range_bad || wr_range_bad
                              || rd_align_bad || wr_align_bad))
                cap_err_q <= 1'b1;
            if (canary_bad_now) canary_err_q <= 1'b1;
            if (wr_two || role_bad_now) arb_conf_q <= 1'b1;
        end
    end

    // ========================================================================
    // 7. 容量断言 —— **elaboration 期**报错（B2），不静默截断
    //    · 缺模块名 = 未定义模块 ⇒ xvlog/xelab 在 elaboration 直接失败，错误
    //      信息里自带"该改哪个参数"（比 $fatal 的可读性更好）。
    //    · 另配 initial + $fatal，覆盖"直接跑 xsim 不单独做 elaboration"的流程。
    // ========================================================================
    generate
        if (CAP_BYTES > BUDGET_BYTES) begin : g_cap_over_budget
            bin_band_buf_CAPACITY_OVER_BUDGET_reduce_BAND_ROWS_or_C_MAX_or_W_MAX u_cap_over ();
        end
    endgenerate

    // synthesis translate_off
    initial begin
        if (CAP_BYTES > BUDGET_BYTES) begin
            $display("bin_band_buf: CAPACITY OVER BUDGET: CAP=%0d B  BUDGET=%0d B (%0d KB)",
                     CAP_BYTES, BUDGET_BYTES, BUDGET_KB);
            $display("                BAND_ROWS=%0d HALO_ROWS=%0d C_MAX=%0d W_MAX=%0d NGRP=%0d ROWS_ALLOC=%0d",
                     BAND_ROWS, HALO_ROWS, C_MAX, W_MAX, NGRP, ROWS_ALLOC);
            $fatal(1, "bin_band_buf capacity over budget - fix parameters, do NOT truncate");
        end else begin
            $display("bin_band_buf: CAP=%0d B (%0d KB) <= BUDGET=%0d KB  [NGRP=%0d ROWS_ALLOC=%0d SLOT_WORDS=%0d]",
                     CAP_BYTES, CAP_BYTES/1024, BUDGET_KB, NGRP, ROWS_ALLOC, SLOT_WORDS);
        end
    end
    // synthesis translate_on

    // ========================================================================
    // 8. 仿真期不变量（translate_off，不综合）
    // ========================================================================
    // synthesis translate_off
    always @(posedge clk) begin
        if (rst_n && !buf_reset) begin
            if (wr_two)
                $error("bin_band_buf: 同拍两个写口（相位表被破坏）e=%0b b=%0b d=%0b",
                       e_wr_en, b_wr_en, d_wr_en);
            if (rd_range_bad || wr_range_bad)
                $error("bin_band_buf: 带内地址越界 rd=%h wr=%h", rd_addr_m, wr_addr_m);
            if (rd_align_bad || wr_align_bad)
                $error("bin_band_buf: 64-bit 口地址非 8 字节对齐 rd=%h wr=%h", rd_addr_m, wr_addr_m);
        end
    end
    // synthesis translate_on

endmodule
