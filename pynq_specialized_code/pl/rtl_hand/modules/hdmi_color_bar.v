// ============================================================================
// hdmi_color_bar.v —— 8 色竖条 + 1 px 白边框 + 移动竖线（纯组合）
//
// 需求来源：docs/camera/05_HDMI输出_PS_PL接口契约.md §3.1
//   8 色竖条：白、黄、青、绿、紫、红、蓝、黑（与厂商 color_bar.v 顺序一致）
//   1 px 白边框：x==0 / x==H_ACTIVE-1 / y==0 / y==V_ACTIVE-1
//   移动竖线：宽 LINE_W 像素，位置 line_x 由 top 在 pix_clk 域每帧 +8 更新；
//             线内做 RGB 反色（哪一条彩条上都可见）；motion_en=0 时关闭。
//             CTRL[4] 控制 motion_en（默认开），CTRL[2] 控制整个彩条。
//   de=0 时 rgb 输出 0（契约：rgb 在 DE 之外输出 0）。
//
// 注意：本模块只做组合，不产生帧计数器；line_x 由 top 维护（见 hdmi_out_top.v）。
// ============================================================================
`timescale 1ns / 1ps

module hdmi_color_bar #(
    parameter integer H_ACTIVE = 1280,
    parameter integer V_ACTIVE = 720,
    parameter integer LINE_W   = 4
) (
    input  wire        de,
    input  wire [15:0] x,
    input  wire [15:0] y,
    input  wire [15:0] line_x,      // 移动竖线左边界（0 .. H_ACTIVE-1）
    input  wire        motion_en,
    output reg  [23:0] rgb
);

    // 每条的宽度（四种模式均可被 8 整除）
    localparam integer BAR_W = H_ACTIVE / 8;

    // 8 色（RGB888），顺序与厂商 color_bar.v 一致
    localparam [23:0] C_WHITE   = 24'hFF_FF_FF;
    localparam [23:0] C_YELLOW  = 24'hFF_FF_00;
    localparam [23:0] C_CYAN    = 24'h00_FF_FF;
    localparam [23:0] C_GREEN   = 24'h00_FF_00;
    localparam [23:0] C_MAGENTA = 24'hFF_00_FF;
    localparam [23:0] C_RED     = 24'hFF_00_00;
    localparam [23:0] C_BLUE    = 24'h00_00_FF;
    localparam [23:0] C_BLACK   = 24'h00_00_00;

    // ---------------------------------------------------------------- 竖条索引
    // 用比较链而不是除法，避免在 148.5 MHz 下出现常数除法长路径
    reg [2:0] bar_idx;
    always @(*) begin
        if      (x < (1 * BAR_W)) bar_idx = 3'd0;
        else if (x < (2 * BAR_W)) bar_idx = 3'd1;
        else if (x < (3 * BAR_W)) bar_idx = 3'd2;
        else if (x < (4 * BAR_W)) bar_idx = 3'd3;
        else if (x < (5 * BAR_W)) bar_idx = 3'd4;
        else if (x < (6 * BAR_W)) bar_idx = 3'd5;
        else if (x < (7 * BAR_W)) bar_idx = 3'd6;
        else                      bar_idx = 3'd7;
    end

    reg [23:0] bar_rgb;
    always @(*) begin
        case (bar_idx)
            3'd0: bar_rgb = C_WHITE;
            3'd1: bar_rgb = C_YELLOW;
            3'd2: bar_rgb = C_CYAN;
            3'd3: bar_rgb = C_GREEN;
            3'd4: bar_rgb = C_MAGENTA;
            3'd5: bar_rgb = C_RED;
            3'd6: bar_rgb = C_BLUE;
            default: bar_rgb = C_BLACK;
        endcase
    end

    // ---------------------------------------------------------------- 边框/竖线
    wire border = (x == 16'd0) || (x == (H_ACTIVE - 1)) ||
                  (y == 16'd0) || (y == (V_ACTIVE - 1));

    wire in_line = (x >= line_x) && (x < (line_x + LINE_W));

    always @(*) begin
        if (!de) begin
            rgb = 24'h00_00_00;
        end else if (border) begin
            rgb = C_WHITE;
        end else if (motion_en && in_line) begin
            rgb = ~bar_rgb;              // 反色竖线：任何彩条背景上都可见
        end else begin
            rgb = bar_rgb;
        end
    end

endmodule
