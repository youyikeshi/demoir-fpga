// ============================================================================
//  k3_cfg_check.v —— conv_k3_engine 的启动前配置校验（组合逻辑，Verilog-2001）
//
//  为什么单独一个模块（主控 2026-10-26 裁决 1）：
//    "凡是每层可能不同的参数，绝不能做成编译期常量" —— RELU 已经吃过一次亏。
//    stride/dilation 也是每层不同 ⇒ 运行期传入 ⇒ **必须校验**，
//    非法值要返回错误码，而不是静默算错（静默算错在黄金向量里表现为"逐字节全错"，
//    很难与"数据通路 bug"区分）。
//
//  用法（BD 适配层里）：
//      k3_cfg_check u_chk(.cin(cfg_cin), ..., .err(cfg_err), .ok(cfg_ok));
//      // cfg_ok=0 时：不拉 ctrl_start，把 cfg_err 写进 Lite 的 ERR 寄存器，
//      // 并让 ctrl_done 自打一拍（PS 侧的"启动→等 done"流程不会挂死）
//
//  错误码（与 docs/rtl_hand/conv_k3_engine_设计.md §18.2 逐条一致）：
//      4'h0  通过
//      4'h1  cin 不是 8 的倍数 / <8 / >192          （C8 布局 + 24 组上限）
//      4'h2  cout==0 / cout>192                     （24 个 pass 上限）
//      4'h3  stride ∉ {1,2}                          （网络只有 1/2）
//      4'h4  dilation ∉ {1,2,3}                      （网络只有 1/2/3）
//      4'h5  H==0 / W==0 / H>320 / W>320             （行缓冲容量）
//      4'h6  (H-1)/S+1 < 1 或 (W-1)/S+1 < 1          （输出尺寸非正）
//      4'h7  shift > 31
//      4'h8  ⌈cin/8⌉ × W × (d≠1 ? 2 : 1) > 4608     （每 bank 槽容量，CAP_B）
//      4'h9  stride≠1 且 dilation≠1                 （不支持的组合，见下）
//
//  关于 4'h9（主控 2026-10-26 裁定）：
//    真实网络里**没有任何一层同时用 s≠1 和 d≠1** ——
//    60 个 d≠1 层全是 s=1；只有 2 个 s=2 层且都是 d=1。
//    这个组合的数据通路（`pp_row` / `pf_cx` 里 S 与 D 同时参与）验证成本最高，
//    而它不在真实路径上 ⇒ **明确拒绝**，绝不静默算错。
//    （"静默算错"在黄金向量里表现为逐字节全错，极难与数据通路 bug 区分 —— 这是本项目最贵的教训）
//
//  注意：same padding 的前提 pad == dilation 不在这里查（pad 由 PS 打包时定死），
//  引擎内部有 $display 断言兜底。
// ============================================================================
`timescale 1ns / 1ps

module k3_cfg_check (
    input  wire [31:0] cfg_cin,
    input  wire [31:0] cfg_cout,
    input  wire [31:0] cfg_h,
    input  wire [31:0] cfg_w,
    input  wire [31:0] cfg_shift,
    input  wire [3:0]  cfg_stride,
    input  wire [3:0]  cfg_dil,
    output reg  [3:0]  err,
    output wire        ok
);

    // ---- 派生量：一律按**物理上界**收窄（时序！2026-10-27 BD 综合的根因）----
    //   ⌈cin/8⌉ ≤ 24（5 位）、W ≤ 320（9 位）⇒ 乘积 ≤ 7680（13 位）、×2 仍 ≤ 14 位。
    //   原来写 `ngrp * cfg_w * (...)` 是两个 32×32 乘法器串联（≈60 级），
    //   加上后面 9 条 if/else 就凑出 100 级、WNS −38ns。
    // 物理位宽：CIN/COUT ≤ 192(8b)、H/W ≤ 320(9b)、SHIFT ≤ 31(5b)
    // 高位的"越界标志"用归约或一次判掉（比 32 位比较便宜得多）
    wire hi_cin  = |cfg_cin[31:8];
    wire hi_cout = |cfg_cout[31:8];
    wire hi_h    = |cfg_h[31:9];
    wire hi_w    = |cfg_w[31:9];
    wire hi_shift= |cfg_shift[31:5];
    wire [7:0]  cin8  = cfg_cin[7:0];
    wire [7:0]  cout8 = cfg_cout[7:0];
    wire [8:0]  h9    = cfg_h[8:0];
    wire [8:0]  w9    = cfg_w[8:0];

    wire [4:0]  ngrp_n   = cin8 >> 3;                               // ⌈cin/8⌉ ≤ 24
    wire [8:0]  w_n      = w9;                                      // 高 320 的情况由 hi_w 判掉
    wire [12:0] ngrp_w   = ngrp_n * w_n;                            // 5×9 → 13 位
    // ⚠ 别写成 `{ngrp_w, dil_bit}`（那是 2x+bit，恒为 ×2）；要的是 "×1 或 ×2"
    wire [13:0] need_cap = (cfg_dil != 4'd1) ? {1'b0, ngrp_w}       // d≠1 ⇒ ×2
                                            : {ngrp_w,  1'b0};      // d==1 ⇒ ×1（低位补 0 对齐位宽）
    // (X-1)/S 且 S∈{1,2} ⇒ 移位代替运行时除法（除法器是另一条极长路径）
    wire [3:0]  stride_sh = cfg_stride - 4'd1;                      // 1→0, 2→1
    wire [9:0]  hout      = (({1'b0, h9} - 10'd1) >> stride_sh) + 10'd1;   // ≤ 320
    wire [9:0]  wout      = (({1'b0, w9} - 10'd1) >> stride_sh) + 10'd1;

    always @(*) begin
        err = 4'h0;
        if (hi_cin || cin8 < 8'd8 || cin8 > 8'd192 || (cin8[2:0] != 3'd0))
            err = 4'h1;
        else if (hi_cout || cout8 == 8'd0 || cout8 > 8'd192)
            err = 4'h2;
        else if (cfg_stride != 4'd1 && cfg_stride != 4'd2)
            err = 4'h3;
        else if (cfg_dil < 4'd1 || cfg_dil > 4'd3)
            err = 4'h4;
        else if (hi_h || hi_w || h9 == 9'd0 || w9 == 9'd0 || h9 > 9'd320 || w9 > 9'd320)
            err = 4'h5;
        else if (hout < 10'd1 || wout < 10'd1)
            err = 4'h6;
        else if (hi_shift)
            err = 4'h7;
        else if (need_cap > 14'd4608)
            err = 4'h8;
        else if (cfg_stride != 4'd1 && cfg_dil != 4'd1)
            err = 4'h9;      // 不支持 s≠1 && d≠1（不在真实路径上，明确拒绝）
    end

    assign ok = (err == 4'h0);

endmodule
