// ============================================================================
//  k5_cfg_check.v —— conv_k5_engine 的启动前配置校验（组合逻辑，Verilog-2001）
//
//  为什么要有这个模块（与 k3 同一理由，2026-10-26 主控裁决 1）：
//    "凡是每层可能不同的参数，绝不能做成编译期常量" —— k5 引擎反过来：
//    CIN/COUT/H/W/PAD 全是**编译期参数**（`conv_k5_engine` 顶部），
//    `ctrl_cin/cout/h/w` 只是契约占位、不参与寻址。于是"PS 写错形状"不会
//    报错、而是拿固定的 320×320×12→48 去算 —— 输出"看着正常"但整网崩。
//    这正是本项目最贵的教训（静默算错在黄金向量里表现为逐字节全错）。
//    所以包装层必须在启动前**逐项比对"本次综合的常量"**，不一致就拒绝启动。
//
//  与 k3_cfg_check 的关系：
//    · 结构/接口/错误码表**照抄** k3（PS 侧 k5_engine.py 已按这张表写好）；
//    · 判定从"范围"改成"等于参数"（k5 引擎形状不可变）；
//    · 合法集合本身**是参数**（CIN_EXP/COUT_EXP/...），换一次综合就换一组值，
//      不是把 12/48/320 写死在比较里。
//
//  错误码（沿用 k3 语义；k5_engine.py 的 ERR_MEANING 按这张表）：
//      4'h0  通过
//      4'h1  cin   ≠ CIN_EXP（本版 12；逻辑 12 → 物理 16，不是 8 的倍数也合法）
//      4'h2  cout  ≠ COUT_EXP（本版 48）
//      4'h3  stride≠ STRIDE_EXP（本版 1）
//      4'h4  dilation ≠ DIL_EXP（本版 1）
//      4'h5  H/W/pad/ksize 与本次综合的常量不一致（本版 320/320/2/5）
//            ★ 与 k3 的细微差异：k3 的 5 只管 H/W；k5 把 **pad 与 ksize** 也归到 5，
//              因为它们和 H/W 同属"综合期常量"这一桶，而错误码表里没有更细的码位。
//              PS 侧看到 5 时，应把 0x18..0x70 的形状寄存器全部核对一遍。
//      4'h6  (H-1)/S+1 < 1 或 (W-1)/S+1 < 1（输出尺寸非正 —— 本版恒不命中，保留码位）
//      4'h7  shift > 31（32-bit 算术右移的物理上界）
//      4'h8  ⌈cin/8⌉·W·(d≠1 ? 2 : 1) > CAP_B（槽容量保守上界）
//            ★ k5 的行缓冲没有 k3 那种"每 bank 槽"结构（LB_DEPTH = W+2·PAD，地址
//              端口 10 位 ⇒ W+2·PAD ≤ 1024 是编译期常量，综合时就会判掉）。
//              这条运行期检查保留是为了**错误码表齐整 + 将来改参数时兜底**；
//              真实层 ⌈12/8⌉×320×1 = 640 ≤ 4608，恒过。
//      4'h9  stride≠1 且 dilation≠1（k5 两者都固定为 1 ⇒ 恒不命中，保留码位）
//      4'hA  由包装层给：四个区没有整体落在 [WIN_LO, WIN_HI] 内（不在这里判）
//
//  没查的东西（明确写出来，别当成"查过了"）：
//    · **RELU 位（0x70 bit0）不参与判定**：k5 引擎的 ReLU 是硬连线的（收尾
//      `if (q_v<0) 0`），寄存器只做读写回显 —— 与 k1_axi_wrap 的做法一致。
//      relu=False 的层由 PS 侧 eligibility() 拒绝；本层不加码位，避免出现
//      PS 的 ERR_MEANING 里没有的码。
//    · C8 位（0x70 bit2/bit3）同理，只回显。
//    · 64 位地址的高 32 位（PS 写 0x14/0x20/0x2C/0x38 的值被丢弃，必须为 0）。
//
//  时序：本模块是纯组合，输出只驱动包装层里的寄存器（cfg_err_r/cfg_ok_r），
//        绝不直接驱动引擎的 ctrl_start —— 见 k5_axi_wrap.v §2 的两级启动。
// ============================================================================
`timescale 1ns / 1ps

module k5_cfg_check #(
    // 本次综合的引擎常量（换一次综合就换这组值，不是把 12/48/320 写死）
    parameter integer CIN_EXP   = 12,
    parameter integer COUT_EXP  = 48,
    parameter integer H_EXP     = 320,
    parameter integer W_EXP     = 320,
    parameter integer PAD_EXP   = 2,
    parameter integer KSIZE_EXP = 5,
    parameter integer STRIDE_EXP = 1,
    parameter integer DIL_EXP    = 1,
    // 槽容量保守上界（见文件头 4'h8 的说明）
    parameter integer CAP_B     = 4608
)(
    input  wire [31:0] cfg_cin,
    input  wire [31:0] cfg_cout,
    input  wire [31:0] cfg_h,
    input  wire [31:0] cfg_w,
    input  wire [31:0] cfg_ksize,
    input  wire [31:0] cfg_pad,
    input  wire [31:0] cfg_shift,
    input  wire [3:0]  cfg_stride,
    input  wire [3:0]  cfg_dil,
    output reg  [3:0]  err,
    output wire        ok
);

    // ---- 参数落到常量表达式（不能对 parameter 直接做位选）----
    localparam [31:0] CIN_V  = CIN_EXP;
    localparam [31:0] COUT_V = COUT_EXP;
    localparam [31:0] H_V    = H_EXP;
    localparam [31:0] W_V    = W_EXP;
    localparam [31:0] PAD_V  = PAD_EXP;
    localparam [31:0] K_V    = KSIZE_EXP;
    // 注：不要写 STRIDE_EXP[3:0] —— `parameter integer` 没有显式位宽，位选在
    //     Verilog-2001 里不保证合法；赋值给 [3:0] 的 localparam 会自动截断。
    localparam [3:0]  STRIDE_V = STRIDE_EXP;
    localparam [3:0]  DIL_V    = DIL_EXP;
    localparam [31:0] CAP_V    = CAP_B;

    // ---- 派生量：一律按**物理上界**收窄（时序；k3 §18.2d 的教训）----
    //   ⌈cin/8⌉ ≤ 24（5 位）、W ≤ 320（9 位）⇒ 乘积 ≤ 7680（13 位）、×2 仍 ≤ 14 位。
    //   写成 32 位乘法器串联会得到 ~60 级组合路径（k3 上实测 WNS −38ns）。
    //   k5 的 cin 不是 8 的倍数也合法（12 → 物理 16）⇒ 这里必须用**向上取整**。
    wire [8:0]  cin9      = cfg_cin[8:0];
    wire [4:0]  ngrp_n    = (cin9 + 9'd7) >> 3;      // ⌈cin/8⌉ ≤ 24（5 位足够）
    wire [8:0]  w_n       = cfg_w[8:0];              // W ≤ 320（9 位）
    wire [12:0] ngrp_w    = ngrp_n * w_n;            // 5×9 → 13 位：≤ 24×320 = 7,680
    // ⚠ 别写成 {ngrp_w, dil_bit}（那是 2x+bit，恒为 ×2）；要的是 "×1 或 ×2"
    wire [13:0] need_cap  = (cfg_dil != 4'd1) ? {1'b0, ngrp_w}    // d≠1 ⇒ ×2
                                              : {ngrp_w,  1'b0};  // d==1 ⇒ ×1（低位补 0 对齐位宽）

    // (X-1)/S+1：S∈{1,2} ⇒ 用移位代替运行时除法（除法器是另一条极长路径）
    wire [3:0]  stride_sh = cfg_stride - 4'd1;                          // 1→0, 2→1
    wire [9:0]  hout      = (({1'b0, cfg_h[8:0]} - 10'd1) >> stride_sh) + 10'd1;  // ≤ 320
    wire [9:0]  wout      = (({1'b0, cfg_w[8:0]} - 10'd1) >> stride_sh) + 10'd1;

    always @(*) begin
        err = 4'h0;
        if (cfg_cin   != CIN_V)                                  err = 4'h1;
        else if (cfg_cout != COUT_V)                             err = 4'h2;
        else if (cfg_stride != STRIDE_V)                         err = 4'h3;
        else if (cfg_dil    != DIL_V)                            err = 4'h4;
        else if (cfg_h != H_V || cfg_w != W_V ||
                 cfg_pad != PAD_V || cfg_ksize != K_V)           err = 4'h5;
        else if (hout < 10'd1 || wout < 10'd1)                   err = 4'h6;
        else if (cfg_shift[31:5] != 27'd0)                       err = 4'h7;
        else if (need_cap > CAP_V[13:0])                         err = 4'h8;
        else if (cfg_stride != 4'd1 && cfg_dil != 4'd1)          err = 4'h9;
    end

    assign ok = (err == 4'h0);

endmodule
