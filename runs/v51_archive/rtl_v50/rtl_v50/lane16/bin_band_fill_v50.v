// ============================================================================
//  bin_band_fill_v50.v —— v50 band 装载填充路径（E 口 / DMA）
//  owner: T-88（从 IMP-B2 的 _worktrees/v50_impB2 移植进 lane16 并按
//         **契约 B 最终定稿** docs/V50_ARCHITECTURE.md §13 改正字段来源）
// ----------------------------------------------------------------------------
//  0. 本版与 IMP-B2 的差别（**只有字段来源**，其余时间轴/断言逐字保留）
//     T-87 报的冲突：IMP-B2 把 **字 3 低 16 当 `W`（行步长）**。
//     §13 定稿是：`w3` 低 16 = `cfg_h`、`w3` 高 16 = `H_in`、**行宽 `W` 在 `w4`**。
//     ⇒ 本版端口按 §13 重命名并换源：
//          cfg_h  ← w3[15:0]   本带有效输出行数（不含 halo）
//          h_in   ← w3[31:16]  输入张量高度 H_in（pad 判据）
//          w_row  ← w4         行宽 W（行步长 / 行内字数）
//          nband  ← w9[15:0]   本带有效输出行数（与 cfg_h 同值，契约 B 规定）
//     ⚠ **不许**再把 w3 低 16 当 W（IMP-B2 的写法，已删）。
//
//  1. 为什么需要它（R2 §11.3 把 pad 从 conv 挪到填充路径）
//     填充第 r 行（带内行号）时，绝对行号 yr = y0 + r − halo；
//       若 yr < 0 或 yr >= H_in ⇒ **整行写 0、不发起 DMA**；
//       否则按 DDR 行地址搬 1 行（W 个 64-bit 字）进 band。
//     ⇒ conv 读侧**永远不需要判边界**（它读到的 pad 行已经是 0），
//       k ∈ {1,3,5} 共用同一填充器，核里零分支（R2 的收益）。
//
//  2. 地址映射（两个地址空间必须分清楚）
//     · **band 内**：线性字地址 = 装载序号 cnt（本模块只吐 DDR 地址，
//       band 侧写地址仍由 segq 的 ld_wa = cnt*8 给，一个字节都不变）。
//       带内布局（与 bin_band_buf 一致，grp 主序）：
//           word = grp*(rows_fill*W) + r*W + x
//       `rows_fill = cfg_h + 2*halo`（有 conv 级时）—— **由硬件算，PS 不传**
//       （R2 §11.5）；没有 conv 级时 halo=0 ⇒ rows_fill = cfg_h。
//     · **DDR 侧**：输入是**整图**的一个 64 通道块（act 是 64 bit/像素的
//       二值位流），块步长 = H_in*W*8，行步长 = W*8：
//           addr = in_pa + grp*(H_in*W*8) + yr*(W*8) + x*8
//       ⇒ 这里必然要 `H_in`（pad 判据）与 `W`（行步长）。
//
//  3. 时序（与 segq / 单元的"1 拍请求、1 拍数据"契约逐位同构）
//        t   : ld_req=1（segq 的 req_pend 刚清 0）；本模块按当前 (grp,r,x)
//              判 pad：pad ⇒ 不发 DMA；否则 dma_en=1 + dma_addr
//        t+1 : 数据回来 ⇒ ld_q = pad ? 0 : dma_data（segq 用 seg_ld_req_d 采样）
//     pad 判决与 rd_req_d 都在**同一 en 拍**打拍，所以两根永远对齐；
//     而 DDR 未命中时 en=0，两者一起被冻住 ⇒ 对齐关系与延迟无关。
//
//  4. 断言（translate_off，不综合）
//     ① pad 行**绝不**发起 DMA（R2 §11.3 的硬要求）；
//     ② 契约 B：`w3` 低 16（cfg_h）必须 = `w9` 低 16（nband）；
//     ③ 行数必须由硬件按 nband + 2*halo 算（PS 不传，R2 §11.5）；
//     ④ 几何非零（否则空搬）；
//     ⑤ pad 字必须整字写 0。
// ============================================================================
`timescale 1ns / 1ps

module bin_band_fill_v50 (
    input  logic        clk,
    input  logic        rst_n,
    input  logic        en,             // = u_ce && fuse_m（FUSE=0 时整模块冻结）

    // ---- segq 的装载请求（1 拍）----
    input  logic        sop,            // 新描述符开始（S_CHK 状态）⇒ 行列游标归零
    input  logic        ld_req,         // segq: (st==LOAD) && !req_pend

    // ---- 几何（契约 B §13 最终定稿：字 0/3/4/6/9 + 硬件推出来的 halo）----
    input  logic [31:0] in_pa,          // 字0：段输入 DDR **源基址**（R3 §12.5）
    input  logic [15:0] y0,             // 字6：本段第一**有效输出**行（绝对行号）
    input  logic [15:0] cfg_h,          // ★ 字3[15:0]：本带有效输出行数（不含 halo）
    input  logic [15:0] h_in,           // ★ 字3[31:16]：输入高度 H_in（pad 判据）
    input  logic [15:0] w_row,          // ★ 字4：行宽 W（行步长）
    input  logic [15:0] nband,          // 字9[15:0]：本带有效输出行数（= cfg_h）
    input  logic [15:0] halo,           // 单侧 halo（无 conv 级时为 0）

    // ---- DDR 读请求 ----
    output logic        dma_en,
    output logic [31:0] dma_addr,
    input  logic [63:0] dma_data,       // 1 en 拍后的数据（= u_rd_data）

    // ---- 给 segq 的装载字 / 观测 ----
    output logic [63:0] ld_q,
    output logic        ld_pad          // 当前拍的字是 pad（仿真观测）
);

    // ========================================================================
    // 0. 行数（R2 §11.5：**硬件算**，PS 只给 nband 与 halo）
    // ========================================================================
    wire [15:0] rows_fill = cfg_h + {halo[14:0], 1'b0};     // cfg_h + 2*halo

    // ========================================================================
    // 1. 行列游标（不用除法器：按 ld_req 逐字推进）
    // ========================================================================
    logic [15:0] xq, rq, gq;

    wire last_x = (xq + 16'd1 >= w_row);
    wire last_r = (rq + 16'd1 >= rows_fill);

    // yr = y0 + r − halo（可负 ⇒ 必须用有符号 32 位）
    wire signed [31:0] yr = $signed({16'd0, y0}) + $signed({16'd0, rq})
                          - $signed({16'd0, halo});
    wire pad_row = (yr < 0) || (yr >= $signed({16'd0, h_in}));

    // DDR 地址：in_pa + g*(H_in*W*8) + yr*(W*8) + x*8
    wire [63:0] blk_stride = ({32'd0, h_in} * {32'd0, w_row}) << 3;
    wire [63:0] row_stride = {32'd0, w_row} << 3;
    wire [63:0] ddr_addr64 = {32'd0, in_pa}
                           + ({32'd0, gq} * blk_stride)
                           + (yr[31:0] * row_stride)
                           + ({27'd0, xq, 3'd0});

    // 只在"真的在搬一行有效数据"时发 DMA（R2：pad 行**不发起 DMA**）
    assign dma_en   = ld_req && !pad_row;
    assign dma_addr = ddr_addr64[31:0];

    // ========================================================================
    // 2. 打拍：pad 判决与 ld_q 与 segq 的 seg_ld_req_d 同一 en 拍
    // ========================================================================
    logic pad_q;
    assign ld_q   = pad_q ? 64'd0 : dma_data;
    assign ld_pad = pad_q;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            xq <= 16'd0; rq <= 16'd0; gq <= 16'd0; pad_q <= 1'b0;
        end else if (en) begin
            if (sop) begin
                // 新描述符：游标归零（sop 只在 S_CHK 一拍有效）
                xq <= 16'd0; rq <= 16'd0; gq <= 16'd0;
            end
            if (ld_req) begin
                pad_q <= pad_row;
                if (last_x) begin
                    xq <= 16'd0;
                    if (last_r) begin
                        rq <= 16'd0;
                        gq <= gq + 16'd1;
                    end else begin
                        rq <= rq + 16'd1;
                    end
                end else begin
                    xq <= xq + 16'd1;
                end
            end
        end
    end

    // ========================================================================
    // 3. 仿真期断言（C4：三条语义写在代码里，不写在文档里）
    // ========================================================================
    // synthesis translate_off
    always @(posedge clk) begin
        if (rst_n && en) begin
            // ① pad 行绝不发起 DMA（R2 §11.3 的硬要求）
            if (ld_req && pad_row && dma_en)
                $error("bin_band_fill_v50: pad 行却发起了 DMA（yr=%0d）", yr);
            // ② 契约 B §13：w3 低 16（cfg_h）必须 = w9 低 16（nband）
            if (cfg_h != nband)
                $error("bin_band_fill_v50: 契约 B §13 被破坏：w3[15:0]=%0d ≠ w9[15:0]=%0d",
                       cfg_h, nband);
            // ③ 行数必须由硬件按 nband + 2*halo 算，PS 不传（R2 §11.5）
            if (rows_fill != (nband + {halo[14:0], 1'b0}))
                $error("bin_band_fill_v50: rows_fill=%0d ≠ nband(%0d)+2*halo(%0d)",
                       rows_fill, nband, halo);
            // ④ 几何必须非零（否则空搬）；W 来自 w4，不许是 0
            if ((w_row == 16'd0) || (rows_fill == 16'd0) || (h_in == 16'd0))
                $error("bin_band_fill_v50: 几何为 0 (W=w4=%0d rows=%0d H_in=w3[31:16]=%0d)",
                       w_row, rows_fill, h_in);
            // ⑤ pad 字必须整字写 0（R2：整行写 0）
            if (pad_q && (ld_q != 64'd0))
                $error("bin_band_fill_v50: pad 字不是 0");
        end
    end
    // synthesis translate_on

endmodule
