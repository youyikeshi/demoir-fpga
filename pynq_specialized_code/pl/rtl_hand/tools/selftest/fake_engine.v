// ============================================================================
//  fake_engine.v —— 验证设施自测用的假模块（**不是**任何真实卷积模块）
//
//  目的：不实现卷积，只按契约 §3 的端口写出一段**已知内容**（全 0），用来
//        验证 tb_common_tasks.vh + run_module.sh 的 PASS/FAIL 判定链路：
//          · INJECT_ERROR=0 → 与 golden/_selftest_zero 的 expected（全 0）一致 → PASS
//          · INJECT_ERROR=1 → 第 12345 字节写成 1 → 必须报 FAIL 并打印该字节
//
//  输出字节数默认 30720 = cout_pad(48) × 2 × 320（k5_12x48 形状）。
//  这是刻意为之的"假数据源"，禁止合入 modules/。
// ============================================================================
`timescale 1ns / 1ps

module fake_engine #(
    parameter int OUT_BYTES    = 30720,
    parameter bit INJECT_ERROR = 1'b0,
    parameter int BAD_BYTE     = 12345
)(
    input  logic        ap_clk,
    input  logic        ap_rst_n,

    output logic        m_axi_in_arvalid,  input logic m_axi_in_arready,
    output logic [31:0] m_axi_in_araddr,   output logic [7:0]  m_axi_in_arlen,
    input  logic        m_axi_in_rvalid,   output logic m_axi_in_rready,
    input  logic [63:0] m_axi_in_rdata,    input logic m_axi_in_rlast,

    output logic        m_axi_w_arvalid,   input logic m_axi_w_arready,
    output logic [31:0] m_axi_w_araddr,    output logic [7:0]  m_axi_w_arlen,
    input  logic        m_axi_w_rvalid,    output logic m_axi_w_rready,
    input  logic [63:0] m_axi_w_rdata,     input logic m_axi_w_rlast,

    output logic        m_axi_b_arvalid,   input logic m_axi_b_arready,
    output logic [31:0] m_axi_b_araddr,    output logic [7:0]  m_axi_b_arlen,
    input  logic        m_axi_b_rvalid,    output logic m_axi_b_rready,
    input  logic [63:0] m_axi_b_rdata,     input logic m_axi_b_rlast,

    output logic        m_axi_out_awvalid, input  logic m_axi_out_awready,
    output logic [31:0] m_axi_out_awaddr,  output logic [7:0] m_axi_out_awlen,
    output logic        m_axi_out_wvalid,  input  logic m_axi_out_wready,
    output logic [63:0] m_axi_out_wdata,   output logic [7:0] m_axi_out_wstrb,
    output logic        m_axi_out_wlast,
    input  logic        m_axi_out_bvalid,  output logic m_axi_out_bready,

    input  logic [31:0] ctrl_in_addr,
    input  logic [31:0] ctrl_wgt_addr,
    input  logic [31:0] ctrl_bias_addr,
    input  logic [31:0] ctrl_out_addr,
    input  logic [31:0] ctrl_shift,
    input  logic [31:0] ctrl_cin,
    input  logic [31:0] ctrl_cout,
    input  logic [31:0] ctrl_h,
    input  logic [31:0] ctrl_w,
    input  logic        ctrl_start,
    output logic        ctrl_done,
    output logic        ctrl_busy
);
    localparam int BEATS = OUT_BYTES / 8;

    typedef enum logic [2:0] {S_IDLE, S_AW, S_W, S_B, S_DONE} state_t;
    state_t st;
    logic [31:0] addr;
    int          left;        // 还没写完的拍数
    int          burst;       // 本 burst 的拍数
    int          beat;
    int          wrote;      // 已写出的总拍数（跨 burst）
    logic        done_q;

    // 假模块完全不读输入/权重/偏置（避免被测 tb 的读通道逻辑被"顺带测试"）
    assign m_axi_in_arvalid  = 1'b0;
    assign m_axi_in_araddr   = 32'h0;
    assign m_axi_in_arlen    = 8'h0;
    assign m_axi_in_rready   = 1'b0;
    assign m_axi_w_arvalid   = 1'b0;
    assign m_axi_w_araddr    = 32'h0;
    assign m_axi_w_arlen     = 8'h0;
    assign m_axi_w_rready    = 1'b0;
    assign m_axi_b_arvalid   = 1'b0;
    assign m_axi_b_araddr    = 32'h0;
    assign m_axi_b_arlen     = 8'h0;
    assign m_axi_b_rready    = 1'b0;

    assign ctrl_done = done_q;
    assign ctrl_busy = (st != S_IDLE) && (st != S_DONE);
    // wlast 必须与本拍数据同拍有效（用组合逻辑，避免 NBA 晚一拍）
    assign m_axi_out_wlast = (st == S_W) && (burst > 0) && (beat == burst - 1);

    // 数据/字节使能用组合逻辑：注入点与拍号严格对齐（测试台自测要求）
    always_comb begin
        m_axi_out_wdata = 64'h0;
        m_axi_out_wstrb = (st == S_W) ? 8'hFF : 8'h00;
        if (INJECT_ERROR && st == S_W && wrote == BAD_BYTE / 8)
            m_axi_out_wdata[(BAD_BYTE % 8) * 8 +: 8] = 8'h01;
    end

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            st                 <= S_IDLE;
            addr               <= 32'h0;
            left               <= 0;
            burst              <= 0;
            beat               <= 0;
            wrote              <= 0;
            done_q             <= 1'b0;
            m_axi_out_awvalid  <= 1'b0;
            m_axi_out_awaddr   <= 32'h0;
            m_axi_out_awlen    <= 8'h0;
            m_axi_out_wvalid   <= 1'b0;
            m_axi_out_bready   <= 1'b0;
        end else begin
            case (st)
                S_IDLE: begin
                    done_q <= 1'b0;
                    if (ctrl_start) begin
                        addr  <= ctrl_out_addr;
                        left  <= BEATS;
                        wrote <= 0;
                        st    <= S_AW;
                        done_q <= 1'b0;
                    end
                end
                S_AW: begin
                    m_axi_out_awvalid <= 1'b1;
                    m_axi_out_awaddr  <= addr;
                    burst             <= (left > 256) ? 256 : left;
                    m_axi_out_awlen   <= ((left > 256) ? 256 : left) - 1;
                    if (m_axi_out_awvalid && m_axi_out_awready) begin
                        m_axi_out_awvalid <= 1'b0;
                        beat              <= 0;
                        st                <= S_W;
                    end
                end
                S_W: begin
                    m_axi_out_wvalid <= 1'b1;
                    if (m_axi_out_wvalid && m_axi_out_wready) begin
                        wrote <= wrote + 1;
                        if (beat == burst - 1) begin
                            m_axi_out_wvalid <= 1'b0;
                            m_axi_out_bready <= 1'b1;
                            st               <= S_B;
                        end else begin
                            beat <= beat + 1;
                            addr <= addr + 32'd8;
                        end
                    end
                end
                S_B: begin
                    if (m_axi_out_bready && m_axi_out_bvalid) begin
                        m_axi_out_bready <= 1'b0;
                        addr <= addr + 32'd8;
                        left <= left - burst;
                        if (left - burst == 0)
                            st <= S_DONE;
                        else
                            st <= S_AW;
                    end
                end
                S_DONE: begin
                    done_q <= 1'b1;
                    // 保持 done 直到下一次 start（fuzz 会先复位）
                end
                default: st <= S_IDLE;
            endcase
        end
    end
endmodule
