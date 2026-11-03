// ============================================================================
// tb_sil9134_i2c.v —— sil9134_init 的 I2C 协议 + 表序列 + 重试验收
//
// 判据（契约 §3.3）：
//   ① START/STOP 合法（每个 START 有且仅有一个 STOP，无重复 START）
//   ② 每字节 8 bit + ACK（每字节 9 个 SCL 上升沿，ACK 由 slave 拉低）
//   ③ 写出的 (dev,reg,data) 序列与 ROM 表逐条一致
//   ④ 上电到 init_done 总耗时 < 100 ms
//   ⑤ SDA 无应答时能重试（第一次地址字节被 NACK，第二次成功）
//
// 说明：TB 内含 I2C slave 模型；用 pullup + 开漏模拟总线。
// 时间单位：1 ns；100 MHz 时钟。
// ============================================================================
`timescale 1ns / 1ps

module tb_sil9134_i2c;

    // ------------------------------------------------------------------ 时钟
    reg clk = 0;
    always #5 clk = ~clk;   // 100 MHz

    reg rst = 1;

    // ------------------------------------------------------------------ 总线
    wire scl, sda;
    wire scl_o, scl_t, sda_o, sda_t;
    pullup (scl);
    pullup (sda);
    assign scl = scl_t ? 1'bz : scl_o;
    assign sda = sda_t ? 1'bz : sda_o;

    wire init_done, busy, error;

    sil9134_init #(
        .CLK_HZ (100_000_000),
        .SCL_HZ (100_000),
        .POR_US (1000)
    ) dut (
        .clk       (clk),
        .rst       (rst),
        .init_done (init_done),
        .busy      (busy),
        .error     (error),
        .i2c_scl_o (scl_o),
        .i2c_scl_t (scl_t),
        .i2c_sda_i (sda),
        .i2c_sda_o (sda_o),
        .i2c_sda_t (sda_t)
    );

    // ------------------------------------------------------------- slave 模型
    reg        slave_low;
    assign sda = slave_low ? 1'b0 : 1'bz;

    reg        mon_en;
    reg        active;
    reg [3:0]  bit_cnt;
    reg [7:0]  shreg;
    reg [7:0]  bytes [0:2];
    integer    byte_idx;

    reg        nack_inject;     // 第一次 ACK 槽不回 ACK（模拟 SDA 无应答）
    reg        nacked;          // 已注入过 NACK

    integer    starts, stops, repeated, aborted, aborted_at_addr;
    integer    acks, bad_len;
    integer    edges_in_tr, nbits_byte;
    integer    n_tr;
    reg [7:0]  tr_dev [0:7];
    reg [7:0]  tr_reg [0:7];
    reg [7:0]  tr_dat [0:7];
    integer    tr_edges [0:7];

    reg        scl_s, sda_s, scl_d, sda_d;

    // 周期测量（ns）
    time       t_last;
    time       min_period, max_period;
    integer    n_periods;

    // 完成时刻
    time       t_reset, t_done;
    integer    n_tr_at_done;

    // ------------------------------------------------------------- 初始化
    initial begin
        slave_low      = 1'b0;
        mon_en         = 1'b0;
        active         = 1'b0;
        bit_cnt        = 4'd0;
        shreg          = 8'd0;
        byte_idx       = 0;
        nack_inject    = 1'b1;
        nacked         = 1'b0;
        starts         = 0;
        stops          = 0;
        repeated       = 0;
        aborted        = 0;
        aborted_at_addr= 0;
        acks           = 0;
        bad_len        = 0;
        edges_in_tr    = 0;
        nbits_byte     = 0;
        n_tr           = 0;
        scl_s = 0; sda_s = 0; scl_d = 0; sda_d = 0;
        t_last         = 0;
        min_period     = 100000000;
        max_period     = 0;
        n_periods      = 0;
        t_reset        = 0;
        t_done         = 0;
        n_tr_at_done   = 0;
    end

    // 输入打拍（100 MHz 采样 100 kHz SCL，足够）
    always @(posedge clk) begin
        scl_s <= scl;
        sda_s <= sda;
        scl_d <= scl_s;
        sda_d <= sda_s;
    end

    // ------------------------------------------------------------- 协议监视
    always @(posedge clk) begin
        if (mon_en) begin
            // ---- SDA 边沿（SCL 高）=> START / STOP
            if ((sda_s != sda_d) && scl_s) begin
                if (sda_s == 1'b0) begin
                    // START
                    starts = starts + 1;
                    if (active) repeated = repeated + 1;
                    active      <= 1'b1;
                    bit_cnt     <= 4'd0;
                    byte_idx    <= 0;
                    shreg       <= 8'd0;
                    slave_low   <= 1'b0;
                    edges_in_tr = 0;
                    nbits_byte  = 0;
                    t_last      = 0;
                end else begin
                    // STOP
                    stops = stops + 1;
                    if (active) begin
                        active    <= 1'b0;
                        slave_low <= 1'b0;
                        if (byte_idx == 3) begin
                            tr_dev[n_tr] = bytes[0];
                            tr_reg[n_tr] = bytes[1];
                            tr_dat[n_tr] = bytes[2];
                            tr_edges[n_tr] = edges_in_tr;
                            n_tr = n_tr + 1;
                        end else begin
                            aborted = aborted + 1;
                            if (byte_idx == 1) aborted_at_addr = aborted_at_addr + 1;
                        end
                        bit_cnt  <= 4'd0;
                        byte_idx <= 0;
                    end
                end
            end

            // ---- SCL 上升沿
            if (scl_s && !scl_d) begin
                if (active) begin
                    if (t_last != 0) begin
                        if (($time - t_last) < min_period) min_period = $time - t_last;
                        if (($time - t_last) > max_period) max_period = $time - t_last;
                        n_periods = n_periods + 1;
                    end
                    t_last = $time;

                    if (byte_idx < 3) begin
                        if (bit_cnt < 8) shreg[7-bit_cnt] <= sda_s;
                        bit_cnt     <= bit_cnt + 4'd1;
                        nbits_byte  = nbits_byte + 1;
                        edges_in_tr = edges_in_tr + 1;
                    end
                end
            end

            // ---- SCL 下降沿
            if (!scl_s && scl_d) begin
                if (active && (byte_idx < 3)) begin
                    if (bit_cnt == 8) begin
                        // 第 9 个时钟：ACK 槽
                        if (nack_inject) begin
                            nack_inject <= 1'b0;
                            slave_low   <= 1'b0;   // 不回 ACK（NACK）
                            nacked      = nacked + 1;
                        end else begin
                            slave_low   <= 1'b1;   // ACK
                            acks        = acks + 1;
                        end
                    end else if (bit_cnt == 9) begin
                        slave_low  <= 1'b0;
                        bit_cnt    <= 4'd0;
                        bytes[byte_idx] <= shreg;
                        byte_idx   <= byte_idx + 1;
                        if (nbits_byte != 9) bad_len = bad_len + 1;
                        nbits_byte = 0;
                    end
                end
            end
        end
    end

    // ------------------------------------------------------------- 记录完成
    always @(posedge init_done) begin
        t_done       = $time;
        n_tr_at_done = n_tr;
    end

    // ------------------------------------------------------------- 主流程
    integer cyc;
    integer k;
    integer fail;
    initial begin
        $display("=== tb_sil9134_i2c (100 MHz clk, target SCL 100 kHz) ===");
        rst = 1'b1;
        #200;
        rst = 1'b0;
        t_reset = $time;
        #100;
        mon_en = 1'b1;

        wait (init_done === 1'b1);
        #2000;

        $display("--------------------------------------------------------------");
        $display("  measured SCL period : min=%0d ns  max=%0d ns  (n=%0d)",
                 min_period, max_period, n_periods);
        $display("  SCL frequency       : %0.2f kHz (target 100.00 kHz)",
                 1000000.0 / ((min_period + max_period) / 2.0));
        $display("  START/STOP          : starts=%0d stops=%0d repeated_start=%0d",
                 starts, stops, repeated);
        $display("  aborted transactions: %0d (at addr byte: %0d)", aborted, aborted_at_addr);
        $display("  ACK count           : %0d   NACK injected: %0d", acks, nacked);
        $display("  bad byte length     : %0d (must be 0; every byte = 8 data + 1 ACK)", bad_len);
        $display("  total time          : %0d ns (%.3f ms, limit 100 ms)",
                 t_done - t_reset, (t_done - t_reset) / 1000000.0);
        $display("  transactions        : %0d", n_tr);
        for (k = 0; k < n_tr; k = k + 1) begin
            $display("    [%0d] dev=0x%02h reg=0x%02h data=0x%02h  scl_rising_edges=%0d",
                     k, tr_dev[k], tr_reg[k], tr_dat[k], tr_edges[k]);
        end
        $display("--------------------------------------------------------------");

        // ---- 判据 ----
        begin
            fail = 0;

            if (n_tr != 2) begin
                $display("  FAIL: transaction count = %0d (expect 2)", n_tr); fail = fail + 1;
            end
            if (n_tr >= 1 && (tr_dev[0] != 8'h72 || tr_reg[0] != 8'h08 || tr_dat[0] != 8'h35)) begin
                $display("  FAIL: entry0 mismatch"); fail = fail + 1;
            end
            if (n_tr >= 2 && (tr_dev[1] != 8'h7A || tr_reg[1] != 8'h2F || tr_dat[1] != 8'h00)) begin
                $display("  FAIL: entry1 mismatch"); fail = fail + 1;
            end
            if (n_tr >= 1 && tr_edges[0] != 27) begin
                $display("  FAIL: entry0 SCL edges = %0d (expect 3*9=27)", tr_edges[0]); fail = fail + 1;
            end
            if (n_tr >= 2 && tr_edges[1] != 27) begin
                $display("  FAIL: entry1 SCL edges = %0d (expect 3*9=27)", tr_edges[1]); fail = fail + 1;
            end
            if (bad_len != 0) begin
                $display("  FAIL: %0d bytes were not 8+ACK", bad_len); fail = fail + 1;
            end
            if (aborted < 1 || aborted_at_addr < 1) begin
                $display("  FAIL: NACK retry not observed"); fail = fail + 1;
            end
            if (nacked < 1) begin
                $display("  FAIL: no NACK injected"); fail = fail + 1;
            end
            if (repeated != 0) begin
                $display("  FAIL: repeated START = %0d", repeated); fail = fail + 1;
            end
            if (starts != stops) begin
                $display("  FAIL: starts(%0d) != stops(%0d)", starts, stops); fail = fail + 1;
            end
            if (error !== 1'b0) begin
                $display("  FAIL: error=1"); fail = fail + 1;
            end
            if ((t_done - t_reset) >= 100000000) begin
                $display("  FAIL: total time >= 100 ms"); fail = fail + 1;
            end
            if (n_tr_at_done != 2) begin
                $display("  FAIL: at init_done n_tr=%0d (expect 2)", n_tr_at_done); fail = fail + 1;
            end
            if ((min_period < 9000) || (max_period > 11000)) begin
                $display("  FAIL: SCL period out of 9~11 us (min=%0d max=%0d)", min_period, max_period);
                fail = fail + 1;
            end

            if (fail == 0) $display("RESULT: PASS  (all I2C criteria met, errors = 0)");
            else           $display("RESULT: FAIL  (errors = %0d)", fail);
            $display("--------------------------------------------------------------");
        end
        $finish;
    end

    // ------------------------------------------------------------- 超时
    initial cyc = 0;
    always @(posedge clk) begin
        if (!init_done) begin
            cyc <= cyc + 1;
            if (cyc == 1000000) begin   // 10 ms
                $display("RESULT: TIMEOUT (init_done never asserted)");
                $finish;
            end
        end
    end

endmodule
