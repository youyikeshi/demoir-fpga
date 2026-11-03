// ============================================================================
// hdmi_timing_gen.v —— 参数化 HDMI/DVI 并行视频时序发生器
//
// 需求来源：docs/camera/05_HDMI输出_PS_PL接口契约.md §1 / §3.1
//
// 产生：hs / vs / de / x / y（x,y 仅在 de=1 时有效，其余时间为 0）
// 口径：
//   - 行内计数 hcnt = 0 .. H_TOTAL-1；场计数 vcnt = 0 .. V_TOTAL-1
//   - de  高：hcnt < H_ACTIVE 且 vcnt < V_ACTIVE
//   - 正极性同步（HS_POL=1）：
//       hs 高：hcnt ∈ [H_ACTIVE+H_FP, H_ACTIVE+H_FP+H_SYNC)
//       vs 高：vcnt ∈ [V_ACTIVE+V_FP, V_ACTIVE+V_FP+V_SYNC)
//     HS_POL=0 时输出整段取反（空闲电平为高）
//   - 所有输出与 x/y 在同一级寄存，de 与像素坐标严格对齐
//
// 四种模式参数（契约 §1，HS/VS 均正极性）：
//   MODE 0 720p60    : 1650/1280  750/720    HFP/HBP/HSW=110/220/40 VFP/VBP/VSW=5/20/5
//   MODE 1 1080p60   : 2200/1920 1125/1080   HFP/HBP/HSW= 88/148/44 VFP/VBP/VSW=4/36/5
//   MODE 2 1080p30   : 同上（像素时钟 74.25 MHz）
//   MODE 3 640x480p60:  800/ 640  525/480    HFP/HBP/HSW= 16/ 48/96 VFP/VBP/VSW=10/33/2
// ============================================================================
`timescale 1ns / 1ps

module hdmi_timing_gen #(
    parameter integer H_TOTAL  = 1650,
    parameter integer H_ACTIVE = 1280,
    parameter integer H_FP     = 110,
    parameter integer H_SYNC   = 40,
    parameter integer H_BP     = 220,
    parameter integer V_TOTAL  = 750,
    parameter integer V_ACTIVE = 720,
    parameter integer V_FP     = 5,
    parameter integer V_SYNC   = 5,
    parameter integer V_BP     = 20,
    parameter integer HS_POL   = 1,
    parameter integer VS_POL   = 1
) (
    input  wire        pix_clk,
    input  wire        rst,        // 同步复位，高有效（pix_clk 域）
    output reg         hs,
    output reg         vs,
    output reg         de,
    output reg  [15:0] x,
    output reg  [15:0] y
);

    // ---------------------------------------------------------------- counters
    reg [15:0] hcnt;
    reg [15:0] vcnt;

    wire h_last = (hcnt == (H_TOTAL - 1));
    wire v_last = (vcnt == (V_TOTAL - 1));

    always @(posedge pix_clk) begin
        if (rst) begin
            hcnt <= 16'd0;
            vcnt <= 16'd0;
        end else if (h_last) begin
            hcnt <= 16'd0;
            vcnt <= v_last ? 16'd0 : (vcnt + 16'd1);
        end else begin
            hcnt <= hcnt + 16'd1;
        end
    end

    // ---------------------------------------------------------------- decode
    wire de_c = (hcnt < H_ACTIVE) && (vcnt < V_ACTIVE);

    wire hs_c = (hcnt >= (H_ACTIVE + H_FP)) &&
                (hcnt <  (H_ACTIVE + H_FP + H_SYNC));

    wire vs_c = (vcnt >= (V_ACTIVE + V_FP)) &&
                (vcnt <  (V_ACTIVE + V_FP + V_SYNC));

    // ---------------------------------------------------------------- outputs
    // 同一级寄存：de 与 x/y 同时更新，保证 (x,y) 与 de 逐像素对齐
    always @(posedge pix_clk) begin
        if (rst) begin
            de <= 1'b0;
            hs <= (HS_POL != 0) ? 1'b0 : 1'b1;
            vs <= (VS_POL != 0) ? 1'b0 : 1'b1;
            x  <= 16'd0;
            y  <= 16'd0;
        end else begin
            de <= de_c;
            hs <= (HS_POL != 0) ? hs_c : ~hs_c;
            vs <= (VS_POL != 0) ? vs_c : ~vs_c;
            x  <= de_c ? hcnt : 16'd0;
            y  <= de_c ? vcnt : 16'd0;
        end
    end

endmodule
