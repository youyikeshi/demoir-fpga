// ============================================================================
// tb_hdmi_timing.v —— hdmi_timing_gen 四模式时序判据自检
//
// 判据（docs/camera/05_HDMI输出_PS_PL接口契约.md §3.3）：
//   ① hs 周期 = H_TOTAL 个 pix_clk
//   ② de 高电平宽度 = H_ACTIVE 个 pix_clk
//   ③ vs 周期 = V_TOTAL 个行周期（同时核对 = V_TOTAL*H_TOTAL 个 pix_clk）
//   ④ de 行数 = V_ACTIVE
//   ⑤ 首像素 (x,y)=(0,0)；末像素 (x,y)=(H_ACTIVE-1,V_ACTIVE-1)
// 四种模式（720p60 / 1080p60 / 1080p30 / 640x480p60）同时各跑一帧。
//
// 运行：rtl_hand/tools/run_hdmi_p1_sim.sh timing
// ============================================================================
`timescale 1ps / 1ps

// ---------------------------------------------------------------- checker
module hdmi_timing_check #(
    parameter integer H_TOTAL  = 1650,
    parameter integer H_ACTIVE = 1280,
    parameter integer V_TOTAL  = 750,
    parameter integer V_ACTIVE = 720,
    parameter [8*16-1:0] MODE_NAME = "mode"
) (
    input  wire        pix_clk,
    input  wire        hs,
    input  wire        vs,
    input  wire        de,
    input  wire [15:0] x,
    input  wire [15:0] y,
    output reg         done,
    output reg  [31:0] errs
);

    reg        hs_d, vs_d, de_d;
    reg        armed, hs_meas_armed, frame_done, de_seen;
    reg [31:0] pixcnt, last_hs_pix, last_vs_pix;
    reg [31:0] de_width, de_rows, line_cnt;
    reg [31:0] hs_period_meas, de_width_meas, vs_cycles_meas;
    reg [31:0] line_cnt_meas, de_rows_meas;
    reg [15:0] first_x, first_y, last_x, last_y;
    reg [15:0] first_x_meas, first_y_meas, last_x_meas, last_y_meas;
    integer    errs_i;

    initial begin
        hs_d = 0; vs_d = 0; de_d = 0;
        armed = 0; hs_meas_armed = 0; frame_done = 0; de_seen = 0;
        pixcnt = 0; last_hs_pix = 0; last_vs_pix = 0;
        de_width = 0; de_rows = 0; line_cnt = 0;
        hs_period_meas = 0; de_width_meas = 0; vs_cycles_meas = 0;
        line_cnt_meas = 0; de_rows_meas = 0;
        first_x = 0; first_y = 0; last_x = 0; last_y = 0;
        first_x_meas = 0; first_y_meas = 0; last_x_meas = 0; last_y_meas = 0;
        errs_i = 0;
        done = 0;
        errs = 0;
    end

    always @(posedge pix_clk) begin
        hs_d <= hs;
        vs_d <= vs;
        de_d <= de;

        if (!frame_done) begin
        pixcnt <= pixcnt + 32'd1;

        // ------------------------------------------------ de 宽度 / 末像素
        if (de) begin
            de_width <= de_width + 32'd1;
            last_x   <= x;
            last_y   <= y;
        end else if (de_d) begin
            if (armed && de_seen && (de_width != H_ACTIVE)) begin
                errs_i = errs_i + 1;
                $display("  [%0s] ERROR  de width = %0d clk (expect %0d)",
                         MODE_NAME, de_width, H_ACTIVE);
            end
            de_width_meas <= de_width;
            de_width      <= 32'd0;
        end

        // ------------------------------------------------ de 行数 / 首像素
        if (de && !de_d && armed) begin
            de_rows <= de_rows + 32'd1;
            if (!de_seen) begin
                de_seen <= 1'b1;
                first_x <= x;
                first_y <= y;
            end
            if ((x >= H_ACTIVE) || (y >= V_ACTIVE)) begin
                errs_i = errs_i + 1;
                $display("  [%0s] ERROR  de pixel out of active area x=%0d y=%0d",
                         MODE_NAME, x, y);
            end
        end

        // ------------------------------------------------ hs 周期 = H_TOTAL
        if (hs && !hs_d) begin
            if (armed) begin
                if (hs_meas_armed) begin
                    if ((pixcnt - last_hs_pix) != H_TOTAL) begin
                        errs_i = errs_i + 1;
                        $display("  [%0s] ERROR  hs period = %0d clk (expect %0d)",
                                 MODE_NAME, pixcnt - last_hs_pix, H_TOTAL);
                    end
                    hs_period_meas <= pixcnt - last_hs_pix;
                end else begin
                    hs_meas_armed <= 1'b1;
                end
                line_cnt <= line_cnt + 32'd1;
            end
            last_hs_pix <= pixcnt;
        end

        // ------------------------------------------------ 场边界
        if (vs && !vs_d) begin
            if (!armed) begin
                armed         <= 1'b1;
                hs_meas_armed <= 1'b0;
                last_vs_pix   <= pixcnt;
                last_hs_pix   <= pixcnt;
                line_cnt      <= 32'd0;
                de_rows       <= 32'd0;
                de_width      <= 32'd0;
                de_seen       <= 1'b0;
                first_x       <= 16'd0;
                first_y       <= 16'd0;
                last_x        <= 16'd0;
                last_y        <= 16'd0;
                $display("  [%0s] frame start detected, measuring ...", MODE_NAME);
            end else begin
                // 实测值（阻塞，保证本拍打印的就是本拍判定值）
                vs_cycles_meas = pixcnt - last_vs_pix;
                line_cnt_meas  = line_cnt;
                de_rows_meas   = de_rows;
                first_x_meas   = first_x;
                first_y_meas   = first_y;
                last_x_meas    = last_x;
                last_y_meas    = last_y;

                // ③ vs 周期 = V_TOTAL 行 = V_TOTAL*H_TOTAL 个 pix_clk
                if (vs_cycles_meas != (V_TOTAL * H_TOTAL)) begin
                    errs_i = errs_i + 1;
                    $display("  [%0s] ERROR  vs period = %0d clk (expect %0d = %0d*%0d)",
                             MODE_NAME, vs_cycles_meas, V_TOTAL * H_TOTAL, V_TOTAL, H_TOTAL);
                end
                if (line_cnt_meas != V_TOTAL) begin
                    errs_i = errs_i + 1;
                    $display("  [%0s] ERROR  lines per frame = %0d (expect %0d)",
                             MODE_NAME, line_cnt_meas, V_TOTAL);
                end

                // ④ de 行数 = V_ACTIVE
                if (de_rows_meas != V_ACTIVE) begin
                    errs_i = errs_i + 1;
                    $display("  [%0s] ERROR  de rows = %0d (expect %0d)",
                             MODE_NAME, de_rows_meas, V_ACTIVE);
                end

                // ⑤ 首/末像素
                if ((first_x_meas != 16'd0) || (first_y_meas != 16'd0)) begin
                    errs_i = errs_i + 1;
                    $display("  [%0s] ERROR  first pixel = (%0d,%0d), expect (0,0)",
                             MODE_NAME, first_x_meas, first_y_meas);
                end
                if ((last_x_meas != (H_ACTIVE - 1)) || (last_y_meas != (V_ACTIVE - 1))) begin
                    errs_i = errs_i + 1;
                    $display("  [%0s] ERROR  last pixel = (%0d,%0d), expect (%0d,%0d)",
                             MODE_NAME, last_x_meas, last_y_meas, H_ACTIVE - 1, V_ACTIVE - 1);
                end

                $display("  [%0s] hs_period=%0d clk (exp %0d) | de_width=%0d clk (exp %0d) | vs_period=%0d clk (exp %0d) | lines=%0d (exp %0d) | de_rows=%0d (exp %0d) | first=(%0d,%0d) | last=(%0d,%0d) | errs=%0d",
                         MODE_NAME,
                         hs_period_meas, H_TOTAL,
                         de_width_meas, H_ACTIVE,
                         vs_cycles_meas, V_TOTAL * H_TOTAL,
                         line_cnt_meas, V_TOTAL,
                         de_rows_meas, V_ACTIVE,
                         first_x_meas, first_y_meas,
                         last_x_meas, last_y_meas,
                         errs_i);

                errs <= errs_i[31:0];
                frame_done <= 1'b1;
                done       <= 1'b1;
            end
        end
        end
    end

endmodule

// ---------------------------------------------------------------- testbench
module tb_hdmi_timing;

    localparam integer HALF_7425  = 6734;   // 74.25 MHz  (ps)
    localparam integer HALF_1485  = 3367;   // 148.5 MHz  (ps)
    localparam integer HALF_25175 = 19861;  // 25.175 MHz (ps)

    reg clk0 = 0, clk1 = 0, clk2 = 0, clk3 = 0;
    reg rst0 = 1, rst1 = 1, rst2 = 1, rst3 = 1;

    wire        hs0, vs0, de0, hs1, vs1, de1, hs2, vs2, de2, hs3, vs3, de3;
    wire [15:0] x0, y0, x1, y1, x2, y2, x3, y3;

    wire        done0, done1, done2, done3;
    wire [31:0] err0, err1, err2, err3;

    // 720p60
    always #HALF_7425 clk0 = ~clk0;
    // 1080p60
    always #HALF_1485 clk1 = ~clk1;
    // 1080p30 (74.25)
    always #HALF_7425 clk2 = ~clk2;
    // 640x480p60 (25.175)
    always #HALF_25175 clk3 = ~clk3;

    // ---- DUT x4 ----
    hdmi_timing_gen #(
        .H_TOTAL(1650), .H_ACTIVE(1280), .H_FP(110), .H_SYNC(40), .H_BP(220),
        .V_TOTAL(750),  .V_ACTIVE(720),  .V_FP(5),   .V_SYNC(5),  .V_BP(20),
        .HS_POL(1), .VS_POL(1)
    ) dut0 (
        .pix_clk(clk0), .rst(rst0), .hs(hs0), .vs(vs0), .de(de0), .x(x0), .y(y0)
    );

    hdmi_timing_gen #(
        .H_TOTAL(2200), .H_ACTIVE(1920), .H_FP(88), .H_SYNC(44), .H_BP(148),
        .V_TOTAL(1125), .V_ACTIVE(1080), .V_FP(4),  .V_SYNC(5),  .V_BP(36),
        .HS_POL(1), .VS_POL(1)
    ) dut1 (
        .pix_clk(clk1), .rst(rst1), .hs(hs1), .vs(vs1), .de(de1), .x(x1), .y(y1)
    );

    // 1080p30 与 1080p60 行列参数相同，像素时钟 74.25 MHz
    hdmi_timing_gen #(
        .H_TOTAL(2200), .H_ACTIVE(1920), .H_FP(88), .H_SYNC(44), .H_BP(148),
        .V_TOTAL(1125), .V_ACTIVE(1080), .V_FP(4),  .V_SYNC(5),  .V_BP(36),
        .HS_POL(1), .VS_POL(1)
    ) dut2 (
        .pix_clk(clk2), .rst(rst2), .hs(hs2), .vs(vs2), .de(de2), .x(x2), .y(y2)
    );

    hdmi_timing_gen #(
        .H_TOTAL(800), .H_ACTIVE(640), .H_FP(16), .H_SYNC(96), .H_BP(48),
        .V_TOTAL(525), .V_ACTIVE(480), .V_FP(10), .V_SYNC(2),  .V_BP(33),
        .HS_POL(1), .VS_POL(1)
    ) dut3 (
        .pix_clk(clk3), .rst(rst3), .hs(hs3), .vs(vs3), .de(de3), .x(x3), .y(y3)
    );

    // ---- checkers x4 ----
    hdmi_timing_check #(
        .H_TOTAL(1650), .H_ACTIVE(1280), .V_TOTAL(750), .V_ACTIVE(720),
        .MODE_NAME("720p60")
    ) chk0 (
        .pix_clk(clk0), .hs(hs0), .vs(vs0), .de(de0), .x(x0), .y(y0),
        .done(done0), .errs(err0)
    );

    hdmi_timing_check #(
        .H_TOTAL(2200), .H_ACTIVE(1920), .V_TOTAL(1125), .V_ACTIVE(1080),
        .MODE_NAME("1080p60")
    ) chk1 (
        .pix_clk(clk1), .hs(hs1), .vs(vs1), .de(de1), .x(x1), .y(y1),
        .done(done1), .errs(err1)
    );

    hdmi_timing_check #(
        .H_TOTAL(2200), .H_ACTIVE(1920), .V_TOTAL(1125), .V_ACTIVE(1080),
        .MODE_NAME("1080p30")
    ) chk2 (
        .pix_clk(clk2), .hs(hs2), .vs(vs2), .de(de2), .x(x2), .y(y2),
        .done(done2), .errs(err2)
    );

    hdmi_timing_check #(
        .H_TOTAL(800), .H_ACTIVE(640), .V_TOTAL(525), .V_ACTIVE(480),
        .MODE_NAME("640x480p60")
    ) chk3 (
        .pix_clk(clk3), .hs(hs3), .vs(vs3), .de(de3), .x(x3), .y(y3),
        .done(done3), .errs(err3)
    );

    // ---- reset release ----
    initial begin
        $display("=== tb_hdmi_timing: 4 modes in parallel ===");
        #200000;   // 200 ns
        rst0 = 0;
        rst1 = 0;
        rst2 = 0;
        rst3 = 0;
    end

    // ---- finish ----
    initial begin
        wait ((done0 == 1'b1) && (done1 == 1'b1) && (done2 == 1'b1) && (done3 == 1'b1));
        #100000;
        $display("--------------------------------------------------------------");
        if ((err0 + err1 + err2 + err3) == 0) begin
            $display("RESULT: PASS  (4 modes, total errors = 0)");
        end else begin
            $display("RESULT: FAIL  (4 modes, total errors = %0d)", err0 + err1 + err2 + err3);
        end
        $display("--------------------------------------------------------------");
        $finish;
    end

    // 超时用"1080p60 像素时钟计数"，避免 64-bit 时间字面量在 xsim 里被截断
    integer to_cnt = 0;
    always @(posedge clk1) begin
        if (!done1) begin
            to_cnt <= to_cnt + 1;
            if (to_cnt == 32'd12000000) begin
                $display("RESULT: TIMEOUT (done = %b %b %b %b)", done0, done1, done2, done3);
                $finish;
            end
        end
    end

endmodule
