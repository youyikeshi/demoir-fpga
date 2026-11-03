// ============================================================================
// sil9134_init.v —— SIL9134 HDMI 编码芯片 I2C 初始化（PL 侧，约 100 kHz）
//
// 来源与依据：
//   1) 厂商 S1-05 HDMI 输出实验的配置查找表 lut_sil9134.v：
//        course_s1_fpga/02_hdmi_output_test/auto_create_project/src/design/lut_sil9134.v
//        第 39 行: 8'd0 : lut_data <= {8'h72, 24'h000835};
//        第 40 行: 8'd1 : lut_data <= {8'h7a, 24'h002f00};
//        （24 位拆成 16 位寄存器地址 + 8 位数据；厂商 i2c_config 用
//          i2c_addr_2byte=0，即只发 1 字节寄存器地址）
//   2) 厂商 top.v 的 I2C 参数：clk=100 MHz, clk_div_cnt=499,
//      i2c_addr_2byte=0，clk_div_cnt 对应 4 相时钟 => SCL ≈ 100 MHz/(4*500)
//      = 50 kHz。本模块取 100 kHz（契约 §3.1 要求），仍远低于 SIL9134 的
//      400 kHz 上限（SiI9134-DATASHEET.PDF §Microcontroller Slave I2C Interface）。
//   3) 器件地址：SiI9134-DATASHEET.PDF Table 7 "Control of I2C Address with
//      CI2CA Pin"：CI2CA=LOW 时 First=0x72、Second=0x7A（8 位写地址）。
//      ⇒ 表里两条 0x72/0x7a 分别落在 SiI9134 的两个 I2C 页。
//   4) 厂商 14_pcie_hdmi_out 的 i2c_config.v 另有 3 条 {0x60,0x05,0x10} /
//      {0x60,0x08,0x05} / {0x60,0x09,0x01}。0x60 不在 SiI9134 数据手册
//      Table 7 的器件地址表里（0x76/0x72、0x7E/0x7A），且 S1-05 本板例程
//      未使用 ⇒ 本设计不写 0x60，避免对未知器件发写操作。若后续上板证明
//      必须写，再按"新增寄存器需注明来源"的规矩补，并同步改 TB 的期望表。
//
// 模式声明：本初始化表来自厂商 1080p60 例程，**只与 1080p60 配套验证**；
//   720p60 / 1080p30 / 640x480p60 未经厂商验证。要换模式必须按 SiI9134
//   数据手册补"视频模式"寄存器（新增寄存器要注明来源），不能靠试。
//
// 行为：
//   - 复位释放后等待 POR_US（默认 1 ms）再启动，给 SIL9134 上电时间；
//   - 自动按 ROM 表逐条写：START + dev + reg + data + STOP；
//   - 每个字节 8 bit + 1 ACK；任一处 NACK ⇒ 立即 STOP，整表重试 1 次；
//   - 两次都失败 ⇒ error=1 且 init_done 保持 0；成功 ⇒ init_done=1（粘滞）。
//   - SDA 输入经 2FF 同步；I2C 输出为开漏语义（t=1 释放，外部上拉）。
//
// 时序：SCL = CLK_HZ / (4*QUARTER)，50% 占空比。默认 100 MHz/100 kHz。
// ============================================================================
`timescale 1ns / 1ps

module sil9134_init #(
    parameter integer CLK_HZ  = 100_000_000,
    parameter integer SCL_HZ  = 100_000,
    parameter integer POR_US  = 1000
) (
    input  wire clk,             // s_axi_aclk（100 MHz）
    input  wire rst,             // 同步复位，高有效
    output reg  init_done,       // 整表写完且全部 ACK
    output reg  busy,            // 初始化进行中
    output reg  error,           // 两次尝试均失败（粘滞）
    // I2C 三态（t=1 释放/上拉，t=0 驱动低）
    output wire i2c_scl_o, output wire i2c_scl_t,
    input  wire i2c_sda_i,
    output wire i2c_sda_o, output wire i2c_sda_t
);

    // ------------------------------------------------------------------ 参数
    localparam integer QUARTER     = (CLK_HZ / (SCL_HZ * 4) < 1) ? 1 : (CLK_HZ / (SCL_HZ * 4));
    localparam integer POR_CYCLES  = (CLK_HZ / 1_000_000) * POR_US;
    localparam integer N_ENTRIES   = 2;

    // ------------------------------------------------------------- 厂商 ROM 表
    // 每条 = {dev_addr[7:0], reg_addr[7:0], data[7:0]}，逐条对应 lut_sil9134.v
    function [7:0] rom_dev;
        input [3:0] i;
        begin
            case (i)
                // lut_sil9134.v:39  {8'h72,24'h000835} -> dev=0x72 reg=0x08 data=0x35
                4'd0: rom_dev = 8'h72;
                // lut_sil9134.v:40  {8'h7a,24'h002f00} -> dev=0x7A reg=0x2F data=0x00
                4'd1: rom_dev = 8'h7A;
                default: rom_dev = 8'hFF;
            endcase
        end
    endfunction

    function [7:0] rom_reg;
        input [3:0] i;
        begin
            case (i)
                4'd0: rom_reg = 8'h08;   // lut_sil9134.v:39
                4'd1: rom_reg = 8'h2F;   // lut_sil9134.v:40
                default: rom_reg = 8'hFF;
            endcase
        end
    endfunction

    function [7:0] rom_dat;
        input [3:0] i;
        begin
            case (i)
                4'd0: rom_dat = 8'h35;   // lut_sil9134.v:39
                4'd1: rom_dat = 8'h00;   // lut_sil9134.v:40
                default: rom_dat = 8'hFF;
            endcase
        end
    endfunction

    // 当前写事务的第 sel 个字节：0=器件地址 1=寄存器地址 2=数据
    function [7:0] byte_for;
        input [3:0] i;
        input [1:0] s;
        begin
            case (s)
                2'd0:    byte_for = rom_dev(i);
                2'd1:    byte_for = rom_reg(i);
                default: byte_for = rom_dat(i);
            endcase
        end
    endfunction

    // ------------------------------------------------------------------ 状态
    localparam [2:0] S_POR   = 3'd0,
                     S_START = 3'd1,
                     S_BIT   = 3'd2,
                     S_ACK   = 3'd3,
                     S_STOP  = 3'd4,
                     S_GAP   = 3'd5,
                     S_DONE  = 3'd6,
                     S_ERR   = 3'd7;

    reg [2:0]  state;
    reg [1:0]  sub;          // 每状态的 1/4 位相
    reg [15:0] qcnt;         // 1/4 SCL 周期计数
    reg [31:0] por_cnt;
    reg [2:0]  bitcnt;       // 7..0
    reg [7:0]  shreg;
    reg [1:0]  byte_sel;     // 0/1/2
    reg [3:0]  idx;
    reg        attempt;      // 0=第一次, 1=重试
    reg        nack;
    reg        scl_low;      // 1=驱动 SCL 低
    reg        sda_low;      // 1=驱动 SDA 低
    reg [1:0]  sda_sync;

    wire qend = (qcnt == (QUARTER - 1));

    // ------------------------------------------------------------- 输出/同步
    assign i2c_scl_o = 1'b0;
    assign i2c_scl_t = ~scl_low;
    assign i2c_sda_o = 1'b0;
    assign i2c_sda_t = ~sda_low;

    always @(posedge clk) begin
        sda_sync <= {sda_sync[0], i2c_sda_i};
    end

    // ------------------------------------------------------------------ FSM
    always @(posedge clk) begin
        if (rst) begin
            state     <= S_POR;
            sub       <= 2'd0;
            qcnt      <= 16'd0;
            por_cnt   <= 32'd0;
            bitcnt    <= 3'd0;
            shreg     <= 8'd0;
            byte_sel  <= 2'd0;
            idx       <= 4'd0;
            attempt   <= 1'b0;
            nack      <= 1'b0;
            scl_low   <= 1'b0;
            sda_low   <= 1'b0;
            init_done <= 1'b0;
            busy      <= 1'b1;
            error     <= 1'b0;
        end else begin
            case (state)
                // ------------------------------------------------ 上电等待
                S_POR: begin
                    scl_low <= 1'b0;
                    sda_low <= 1'b0;
                    if (por_cnt == (POR_CYCLES - 1)) begin
                        state    <= S_START;
                        sub      <= 2'd0;
                        qcnt     <= 16'd0;
                        idx      <= 4'd0;
                        attempt  <= 1'b0;
                        byte_sel <= 2'd0;
                    end else begin
                        por_cnt <= por_cnt + 32'd1;
                    end
                end

                // ------------------------------------------------ START
                // SCL 高时 SDA 由高变低；随后拉低 SCL
                S_START: begin
                    if (qend) begin
                        qcnt <= 16'd0;
                        case (sub)
                            2'd0: begin scl_low <= 1'b0; sda_low <= 1'b0; sub <= 2'd1; end
                            2'd1: begin sda_low <= 1'b1; sub <= 2'd2; end
                            2'd2: begin scl_low <= 1'b1; sda_low <= 1'b1; sub <= 2'd3; end
                            2'd3: begin
                                state    <= S_BIT;
                                sub      <= 2'd0;
                                bitcnt   <= 3'd7;
                                shreg    <= byte_for(idx, 2'd0);
                                byte_sel <= 2'd0;
                            end
                        endcase
                    end else begin
                        qcnt <= qcnt + 16'd1;
                    end
                end

                // ------------------------------------------------ 8 个数据位
                // 低相位改数据，高相位由从机采样
                S_BIT: begin
                    if (qend) begin
                        qcnt <= 16'd0;
                        case (sub)
                            2'd0: begin scl_low <= 1'b1; sub <= 2'd1; end
                            2'd1: begin sda_low <= ~shreg[7]; sub <= 2'd2; end
                            2'd2: begin scl_low <= 1'b0; sub <= 2'd3; end
                            2'd3: begin
                                scl_low <= 1'b1;
                                if (bitcnt == 3'd0) begin
                                    state <= S_ACK;
                                    sub   <= 2'd0;
                                end else begin
                                    bitcnt <= bitcnt - 3'd1;
                                    shreg  <= {shreg[6:0], 1'b0};
                                    sub    <= 2'd0;
                                end
                            end
                        endcase
                    end else begin
                        qcnt <= qcnt + 16'd1;
                    end
                end

                // ------------------------------------------------ 第 9 拍 ACK
                S_ACK: begin
                    if (qend) begin
                        qcnt <= 16'd0;
                        case (sub)
                            2'd0: begin scl_low <= 1'b1; sda_low <= 1'b0; sub <= 2'd1; end
                            2'd1: begin sub <= 2'd2; end
                            2'd2: begin scl_low <= 1'b0; sub <= 2'd3; end
                            2'd3: begin
                                scl_low <= 1'b1;
                                if (sda_sync[1]) begin          // NACK
                                    nack  <= 1'b1;
                                    state <= S_STOP;
                                    sub   <= 2'd0;
                                end else if (byte_sel == 2'd2) begin
                                    state <= S_STOP;
                                    sub   <= 2'd0;
                                end else begin
                                    byte_sel <= byte_sel + 2'd1;
                                    shreg    <= byte_for(idx, byte_sel + 2'd1);
                                    bitcnt   <= 3'd7;
                                    state    <= S_BIT;
                                    sub      <= 2'd0;
                                end
                            end
                        endcase
                    end else begin
                        qcnt <= qcnt + 16'd1;
                    end
                end

                // ------------------------------------------------ STOP
                // 先拉低 SDA，再把 SCL 拉高，最后 SDA 由低变高
                S_STOP: begin
                    if (qend) begin
                        qcnt <= 16'd0;
                        case (sub)
                            2'd0: begin scl_low <= 1'b1; sda_low <= 1'b1; sub <= 2'd1; end
                            2'd1: begin sub <= 2'd2; end
                            2'd2: begin scl_low <= 1'b0; sub <= 2'd3; end
                            2'd3: begin sda_low <= 1'b0; state <= S_GAP; sub <= 2'd0; end
                        endcase
                    end else begin
                        qcnt <= qcnt + 16'd1;
                    end
                end

                // ------------------------------------------------ 总线空闲 + 决策
                S_GAP: begin
                    if (qend) begin
                        qcnt <= 16'd0;
                        if (sub == 2'd3) begin
                            sub <= 2'd0;
                            if (nack) begin
                                nack <= 1'b0;
                                if (attempt == 1'b0) begin
                                    attempt  <= 1'b1;   // 只重试 1 次
                                    idx      <= 4'd0;
                                    byte_sel <= 2'd0;
                                    state    <= S_START;
                                end else begin
                                    state <= S_ERR;
                                    error <= 1'b1;
                                    busy  <= 1'b0;
                                end
                            end else if (idx == (N_ENTRIES - 1)) begin
                                state     <= S_DONE;
                                init_done <= 1'b1;
                                busy      <= 1'b0;
                            end else begin
                                idx      <= idx + 4'd1;
                                byte_sel <= 2'd0;
                                state    <= S_START;
                            end
                        end else begin
                            sub <= sub + 2'd1;
                        end
                    end else begin
                        qcnt <= qcnt + 16'd1;
                    end
                end

                // ------------------------------------------------ 结束
                S_DONE, S_ERR: begin
                    scl_low <= 1'b0;
                    sda_low <= 1'b0;
                    busy    <= 1'b0;
                end

                default: state <= S_POR;
            endcase
        end
    end

endmodule
