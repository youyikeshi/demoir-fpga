// ============================================================================
//  tb_common_tasks.vh —— 手写 RTL 模块的统一 testbench 骨架
//
//  目标：每个模块的 tb 只写「形状参数 + DUT 例化」两段，其余全部由本文件提供：
//    · 时钟/复位
//    · 4 路 AXI4 从机内存模型（in / w / b 三读 + out 一写，64-bit 数据宽）
//    · 二进制向量加载（契约 §4：input_c8.bin / weight.bin / bias.bin /
//      expected_c8.bin / shift.txt / meta.json）
//    · 以下 task（接口按任务书固定，tb 里直接调）：
//        load_bins(dir)        按契约 §4 读入一组向量
//        drive_start()         拉 ctrl_start，等 ctrl_done，统计拍数
//        check_bytes(n_bad)    逐字节比对 expected_c8
//        report_all(name)      打印统一格式的验收报告
//
//  设计约束：
//    1. **只假设契约 §3 的顶层端口名**（ap_clk/ap_rst_n/ctrl_*/m_axi_*），
//       不假设任何模块内部结构；
//    2. 端口名固定 ⇒ 本文件可以直接驱动 tb 里的同名信号，tb 不需要写 AXI 从机；
//    3. 比对器复用 common/eq_checker.v 里的 eq_byte_checker（见该文件）。
//
//  用法（最小 tb，照抄即可）：
//      `timescale 1ns / 1ps
//      module tb_conv_k5_engine;
//        `define TB_MODULE_NAME "conv_k5_engine"
//        `include "tb_common_tasks.vh"        // 声明时钟/复位/AXI/ctrl/task
//
//        conv_k5_engine #(.CIN(12), .COUT(48), .W(320), .H(2), .PAD(2))
//        dut (
//            .ap_clk(ap_clk), .ap_rst_n(ap_rst_n),
//            .m_axi_in_arvalid(m_axi_in_arvalid), ...  // 直接照契约端口表连
//        );
//      endmodule
//
//  运行（由 tools/run_module.sh 自动传参）：
//      xsim <snapshot> -R +GOLDEN=<abs golden dir>
//      xsim <snapshot> -R +FUZZ_MANIFEST=<manifest.txt> [+TARGET_CYCLES=N]
//
//  重要：本文件是 **include 片段**，必须在 tb 的 module 作用域内、例化 DUT 之前/
//  之后均可（Verilog 不要求声明在前），但一个 tb 只能 include 一次。
// ============================================================================
`ifndef TB_COMMON_TASKS_VH
`define TB_COMMON_TASKS_VH

// ---------------------------------------------------------------------------
// 0. 可覆盖的配置
// ---------------------------------------------------------------------------
`ifndef TB_MAX_CYCLES
  `define TB_MAX_CYCLES 20000000      // 单次 ctrl_start→ctrl_done 的看门狗
`endif
`ifndef TB_CLK_HALF_NS
  `define TB_CLK_HALF_NS 5           // 10 ns 周期 = 100 MHz
`endif
`ifndef TB_DRAIN_CYCLES
  `define TB_DRAIN_CYCLES 8           // done 之后多等几拍，收 AXI 尾拍
`endif
`ifndef TB_MAX_SIM_NS
  // 全局仿真时间兜底：600 s（1 ps 分辨率下 = 6e14 ps，远小于 2^63 ps≈106 天）。
  // 足够 200 组 fuzz × 单组 20M 拍（40 s 仿真时间）；墙钟由 run_module.sh 的
  // timeout（默认 300 s/步）兜底。可用 -d TB_MAX_SIM_NS=... 覆盖。
  `define TB_MAX_SIM_NS 64'd600000000000
`endif

// ---------------------------------------------------------------------------
// 1. 时钟 / 复位 / 控制寄存器
// ---------------------------------------------------------------------------
logic ap_clk = 1'b0;
logic ap_rst_n = 1'b0;
always #(`TB_CLK_HALF_NS) ap_clk = ~ap_clk;

// 内存基址（DUT 只认字节地址，基址由 tb 决定；四块互不重叠）
localparam logic [31:0] TB_IN_BASE   = 32'h0000_0000;
localparam logic [31:0] TB_WGT_BASE  = 32'h1000_0000;
localparam logic [31:0] TB_BIAS_BASE = 32'h2000_0000;
localparam logic [31:0] TB_OUT_BASE  = 32'h3000_0000;

logic [31:0] ctrl_in_addr;
logic [31:0] ctrl_wgt_addr;
logic [31:0] ctrl_bias_addr;
logic [31:0] ctrl_out_addr;
logic [31:0] ctrl_shift;
logic [31:0] ctrl_cin;
logic [31:0] ctrl_cout;
logic [31:0] ctrl_h;
logic [31:0] ctrl_w;
logic        ctrl_start;
logic        ctrl_done;
logic        ctrl_busy;

// ---------------------------------------------------------------------------
// 2. AXI4 从机端口（与契约 §3 完全同名；tb 把 DUT 端口直接连到这些信号）
// ---------------------------------------------------------------------------
logic        m_axi_in_arvalid;
logic        m_axi_in_arready;
logic [31:0] m_axi_in_araddr;
logic [7:0]  m_axi_in_arlen;
logic        m_axi_in_rvalid;
logic        m_axi_in_rready;
logic [63:0] m_axi_in_rdata;
logic        m_axi_in_rlast;

logic        m_axi_w_arvalid;
logic        m_axi_w_arready;
logic [31:0] m_axi_w_araddr;
logic [7:0]  m_axi_w_arlen;
logic        m_axi_w_rvalid;
logic        m_axi_w_rready;
logic [63:0] m_axi_w_rdata;
logic        m_axi_w_rlast;

logic        m_axi_b_arvalid;
logic        m_axi_b_arready;
logic [31:0] m_axi_b_araddr;
logic [7:0]  m_axi_b_arlen;
logic        m_axi_b_rvalid;
logic        m_axi_b_rready;
logic [63:0] m_axi_b_rdata;
logic        m_axi_b_rlast;

logic        m_axi_out_awvalid;
logic        m_axi_out_awready;
logic [31:0] m_axi_out_awaddr;
logic [7:0]  m_axi_out_awlen;
logic        m_axi_out_wvalid;
logic        m_axi_out_wready;
logic [63:0] m_axi_out_wdata;
logic [7:0]  m_axi_out_wstrb;
logic        m_axi_out_wlast;
logic        m_axi_out_bvalid;
logic        m_axi_out_bready;

// ---------------------------------------------------------------------------
// 3. 向量/统计状态
// ---------------------------------------------------------------------------
byte in_mem[];          // input_c8.bin   （物理通道 ceil(cin/8)*8）
byte wgt_mem[];         // weight.bin     （PAD 容器）
byte bias_mem[];        // bias.bin       （int32 × cout，小端）
byte exp_mem[];         // expected_c8.bin
byte out_mem[];         // DUT 写出的 C8 输出（初值 8'hxx，漏写会被抓到）

int  tb_cin, tb_cout, tb_k, tb_s, tb_d, tb_pad, tb_h, tb_w, tb_relu, tb_shift;
int  tb_cin_pad, tb_cout_pad, tb_hout, tb_wout;
longint unsigned tb_in_bytes, tb_wgt_bytes, tb_bias_bytes, tb_out_bytes;

int  tb_cycles;             // 最近一组 ctrl_start→ctrl_done 的拍数
int  tb_target_cycles;      // 目标拍数（+TARGET_CYCLES=N，0=未登记）
int  tb_last_bad;
longint tb_total_checked;
longint tb_total_bad;
int  tb_case_count;
int  tb_case_pass;
int  tb_axi_errors;         // 越界读/写次数（任何一次都判 FAIL）
int  tb_rd_oor;
int  tb_wr_oor;
longint tb_unknown_bytes;   // 输出里仍是 X/Z 的字节数（漏写）
string tb_golden_dir;
string tb_shape_name = "unknown";
string tb_dump_out = "";    // +DUMP_OUT=<file>；空=不落盘
bit    tb_is_fuzz = 1'b0;
int    tb_dump_files;

// 逐 c / 逐 x 的失配计数（跨所有 case 累加；单 case 即该 case）
int  tb_c_err_total[];
int  tb_x_err_total[];
string tb_c_top = "";
string tb_x_top = "";

eq_byte_checker #(.MAX_PRINT(200)) tb_byte_chk();

// ---------------------------------------------------------------------------
// 4. 内存访问（按字节；地址超出区域返回 0，写越界计 tb_axi_errors）
// ---------------------------------------------------------------------------
function automatic longint unsigned tb_align8(input longint unsigned n);
    return (n + 7) & ~longint'(7);
endfunction

function automatic byte tb_mem_rd8(input logic [31:0] addr);
    longint unsigned a;
    a = addr;
    // 允许读到区域末尾所在的 8 字节拍（64-bit rdata 的最后一拍可能跨过末尾）。
    if (a >= longint'(TB_IN_BASE) && a < longint'(TB_IN_BASE) + tb_align8(tb_in_bytes))
        return (a < longint'(TB_IN_BASE) + tb_in_bytes) ? in_mem[a - TB_IN_BASE] : 8'h00;
    if (a >= longint'(TB_WGT_BASE) && a < longint'(TB_WGT_BASE) + tb_align8(tb_wgt_bytes))
        return (a < longint'(TB_WGT_BASE) + tb_wgt_bytes) ? wgt_mem[a - TB_WGT_BASE] : 8'h00;
    if (a >= longint'(TB_BIAS_BASE) && a < longint'(TB_BIAS_BASE) + tb_align8(tb_bias_bytes))
        return (a < longint'(TB_BIAS_BASE) + tb_bias_bytes) ? bias_mem[a - TB_BIAS_BASE] : 8'h00;
    tb_axi_errors++;
    tb_rd_oor++;
    if (tb_rd_oor <= 5)
        $display("[TB] WARNING: AXI 读越界 addr=0x%08h（区域大小 in=%0d w=%0d b=%0d）",
                 addr, tb_in_bytes, tb_wgt_bytes, tb_bias_bytes);
    return 8'h00;
endfunction

function automatic logic [63:0] tb_mem_rd64(input logic [31:0] addr);
    logic [63:0] v;
    v = 64'h0;
    for (int i = 0; i < 8; i++)
        v[i*8 +: 8] = tb_mem_rd8(addr + i);
    return v;
endfunction

task automatic tb_mem_wr8(input logic [31:0] addr, input logic [63:0] data,
                          input logic [7:0] strb);
    longint unsigned off;
    off = addr - TB_OUT_BASE;
    for (int i = 0; i < 8; i++) begin
        if (strb[i]) begin
            if (off + i < tb_out_bytes)
                out_mem[off + i] = data[i*8 +: 8];
            else begin
                tb_axi_errors++;
                tb_wr_oor++;
                if (tb_wr_oor <= 5)
                    $display("[TB] WARNING: AXI 写越界 addr=0x%08h off=%0d out_bytes=%0d",
                             addr, off + i, tb_out_bytes);
            end
        end
    end
endtask

// ---------------------------------------------------------------------------
// 5. 三路 AXI4 读从机（in / w / b）
//    行为：arready 空闲时 1；接受 AR 后按 8 字节/拍连续吐数，rlast 由 arlen 决定。
// ---------------------------------------------------------------------------
typedef enum logic {TB_RD_IDLE, TB_RD_DATA} tb_rd_state_t;

tb_rd_state_t in_rd_st, w_rd_st, b_rd_st;
logic [31:0]  in_rd_addr,  w_rd_addr,  b_rd_addr;
int           in_rd_left,  w_rd_left,  b_rd_left;   // 剩余拍数
logic [63:0]  in_rd_data,  w_rd_data,  b_rd_data;
logic         in_rd_last,  w_rd_last,  b_rd_last;

assign m_axi_in_arready = (in_rd_st == TB_RD_IDLE);
assign m_axi_in_rvalid  = (in_rd_st == TB_RD_DATA);
assign m_axi_in_rdata   = in_rd_data;
assign m_axi_in_rlast   = in_rd_last;

assign m_axi_w_arready  = (w_rd_st == TB_RD_IDLE);
assign m_axi_w_rvalid   = (w_rd_st == TB_RD_DATA);
assign m_axi_w_rdata    = w_rd_data;
assign m_axi_w_rlast    = w_rd_last;

assign m_axi_b_arready  = (b_rd_st == TB_RD_IDLE);
assign m_axi_b_rvalid   = (b_rd_st == TB_RD_DATA);
assign m_axi_b_rdata    = b_rd_data;
assign m_axi_b_rlast    = b_rd_last;

always @(posedge ap_clk or negedge ap_rst_n) begin
    if (!ap_rst_n) begin
        in_rd_st   <= TB_RD_IDLE;
        in_rd_addr <= 32'h0;
        in_rd_left <= 0;
        in_rd_data <= 64'h0;
        in_rd_last <= 1'b0;
    end else begin
        case (in_rd_st)
            TB_RD_IDLE: if (m_axi_in_arvalid) begin
                in_rd_addr <= m_axi_in_araddr;
                in_rd_left <= {24'h0, m_axi_in_arlen} + 1;  // 总拍数
                in_rd_data <= tb_mem_rd64(m_axi_in_araddr);
                in_rd_last <= (m_axi_in_arlen == 8'd0);
                in_rd_addr <= m_axi_in_araddr + 32'd8;
                in_rd_left <= {24'h0, m_axi_in_arlen};      // 首拍已准备
                in_rd_st   <= TB_RD_DATA;
            end
            TB_RD_DATA: if (m_axi_in_rready) begin
                if (in_rd_left == 0) begin
                    in_rd_st <= TB_RD_IDLE;
                end else begin
                    in_rd_data <= tb_mem_rd64(in_rd_addr);
                    in_rd_last <= (in_rd_left == 1);
                    in_rd_addr <= in_rd_addr + 32'd8;
                    in_rd_left <= in_rd_left - 1;
                end
            end
            default: in_rd_st <= TB_RD_IDLE;
        endcase
    end
end

always @(posedge ap_clk or negedge ap_rst_n) begin
    if (!ap_rst_n) begin
        w_rd_st   <= TB_RD_IDLE;
        w_rd_addr <= 32'h0;
        w_rd_left <= 0;
        w_rd_data <= 64'h0;
        w_rd_last <= 1'b0;
    end else begin
        case (w_rd_st)
            TB_RD_IDLE: if (m_axi_w_arvalid) begin
                w_rd_addr <= m_axi_w_araddr;
                w_rd_left <= {24'h0, m_axi_w_arlen} + 1;
                w_rd_data <= tb_mem_rd64(m_axi_w_araddr);
                w_rd_last <= (m_axi_w_arlen == 8'd0);
                w_rd_addr <= m_axi_w_araddr + 32'd8;
                w_rd_left <= {24'h0, m_axi_w_arlen};
                w_rd_st   <= TB_RD_DATA;
            end
            TB_RD_DATA: if (m_axi_w_rready) begin
                if (w_rd_left == 0) begin
                    w_rd_st <= TB_RD_IDLE;
                end else begin
                    w_rd_data <= tb_mem_rd64(w_rd_addr);
                    w_rd_last <= (w_rd_left == 1);
                    w_rd_addr <= w_rd_addr + 32'd8;
                    w_rd_left <= w_rd_left - 1;
                end
            end
            default: w_rd_st <= TB_RD_IDLE;
        endcase
    end
end

always @(posedge ap_clk or negedge ap_rst_n) begin
    if (!ap_rst_n) begin
        b_rd_st   <= TB_RD_IDLE;
        b_rd_addr <= 32'h0;
        b_rd_left <= 0;
        b_rd_data <= 64'h0;
        b_rd_last <= 1'b0;
    end else begin
        case (b_rd_st)
            TB_RD_IDLE: if (m_axi_b_arvalid) begin
                b_rd_addr <= m_axi_b_araddr;
                b_rd_left <= {24'h0, m_axi_b_arlen} + 1;
                b_rd_data <= tb_mem_rd64(m_axi_b_araddr);
                b_rd_last <= (m_axi_b_arlen == 8'd0);
                b_rd_addr <= m_axi_b_araddr + 32'd8;
                b_rd_left <= {24'h0, m_axi_b_arlen};
                b_rd_st   <= TB_RD_DATA;
            end
            TB_RD_DATA: if (m_axi_b_rready) begin
                if (b_rd_left == 0) begin
                    b_rd_st <= TB_RD_IDLE;
                end else begin
                    b_rd_data <= tb_mem_rd64(b_rd_addr);
                    b_rd_last <= (b_rd_left == 1);
                    b_rd_addr <= b_rd_addr + 32'd8;
                    b_rd_left <= b_rd_left - 1;
                end
            end
            default: b_rd_st <= TB_RD_IDLE;
        endcase
    end
end

// ---------------------------------------------------------------------------
// 6. AXI4 写从机（out）：AW → W 拍（按 wstrb 写字节）→ B 响应
// ---------------------------------------------------------------------------
typedef enum logic [1:0] {TB_WR_IDLE, TB_WR_DATA, TB_WR_RESP} tb_wr_state_t;

tb_wr_state_t out_wr_st;
logic [31:0]  out_wr_addr;
int           out_wr_left;
int           out_wr_beats;
logic         out_wr_last_seen;

assign m_axi_out_awready = (out_wr_st == TB_WR_IDLE);
assign m_axi_out_wready  = (out_wr_st == TB_WR_DATA);
assign m_axi_out_bvalid  = (out_wr_st == TB_WR_RESP);

always @(posedge ap_clk or negedge ap_rst_n) begin
    if (!ap_rst_n) begin
        out_wr_st        <= TB_WR_IDLE;
        out_wr_addr      <= 32'h0;
        out_wr_left      <= 0;
        out_wr_beats     <= 0;
        out_wr_last_seen <= 1'b0;
    end else begin
        case (out_wr_st)
            TB_WR_IDLE: if (m_axi_out_awvalid) begin
                out_wr_addr  <= m_axi_out_awaddr;
                out_wr_left  <= {24'h0, m_axi_out_awlen} + 1;
                out_wr_beats <= 0;
                out_wr_st    <= TB_WR_DATA;
            end
            TB_WR_DATA: if (m_axi_out_wvalid) begin
                tb_mem_wr8(out_wr_addr, m_axi_out_wdata, m_axi_out_wstrb);
                out_wr_beats <= out_wr_beats + 1;
                out_wr_addr  <= out_wr_addr + 32'd8;
                out_wr_left  <= out_wr_left - 1;
                out_wr_last_seen <= m_axi_out_wlast;
                if (m_axi_out_wlast || out_wr_left == 1) begin
                    if (!m_axi_out_wlast)
                        $display("[TB] WARNING: burst 结束但 wlast=0 (beats=%0d)",
                                 out_wr_beats + 1);
                    if (m_axi_out_wlast && out_wr_left != 1)
                        $display("[TB] WARNING: wlast=1 但剩余 %0d 拍", out_wr_left - 1);
                    out_wr_st <= TB_WR_RESP;
                end
            end
            TB_WR_RESP: if (m_axi_out_bready)
                out_wr_st <= TB_WR_IDLE;
            default: out_wr_st <= TB_WR_IDLE;
        endcase
    end
end

// ---------------------------------------------------------------------------
// 7. 字符串工具
// ---------------------------------------------------------------------------
function automatic string tb_trim(input string s);
    int a, b;
    a = 0;
    b = s.len() - 1;
    while (a <= b && (s.getc(a) == 8'd10 || s.getc(a) == 8'd13 ||
                      s.getc(a) == 8'd32 || s.getc(a) == 8'd9))
        a++;
    while (b >= a && (s.getc(b) == 8'd10 || s.getc(b) == 8'd13 ||
                      s.getc(b) == 8'd32 || s.getc(b) == 8'd9))
        b--;
    if (b < a) return "";
    return s.substr(a, b);
endfunction

// 取 counts[] 里最大的 5 项，返回 "idx:count,idx:count,..."（无空格，便于进 summary）
function automatic string tb_top5(input int counts[]);
    string s;
    bit taken[];
    int best, bestv;
    s = "";
    if (counts.size() == 0) return "";
    taken = new[counts.size()];
    for (int i = 0; i < counts.size(); i++) taken[i] = 1'b0;
    for (int k = 0; k < 5; k++) begin
        best = -1;
        bestv = 0;
        for (int i = 0; i < counts.size(); i++)
            if (!taken[i] && counts[i] > bestv) begin
                best = i;
                bestv = counts[i];
            end
        if (best < 0) break;
        taken[best] = 1'b1;
        if (s.len() > 0) s = {s, ","};
        s = {s, $sformatf("%0d:%0d", best, bestv)};
    end
    return s;
endfunction

// ---------------------------------------------------------------------------
// 8. load_bins(dir) —— 契约 §4
// ---------------------------------------------------------------------------
task automatic load_bins(input string dir);
    int fd, n;
    string line;
    int np;
    longint unsigned n_in, n_w, n_b, n_out;
    int wcb;

    tb_golden_dir = dir;

    // ---- meta.json（固定键顺序，由 tools/make_golden.py 生成）----
    // 契约 §4 只认 meta.json；旧 cosim 数据集的 meta.txt 请先用
    // tools/make_golden.py --from-dir 转成 golden/<shape>/。
    fd = $fopen({dir, "/meta.json"}, "r");
    if (fd == 0)
        $fatal(1, "[TB] FAIL: 打不开 %s/meta.json（契约 §4 要求；用 tools/make_golden.py 生成）", dir);
    n = $fgets(line, fd);
    np = $sscanf(line,
        "{\"cin\":%d,\"cout\":%d,\"k\":%d,\"s\":%d,\"d\":%d,\"pad\":%d,\"H\":%d,\"W\":%d,\"relu\":%d}",
        tb_cin, tb_cout, tb_k, tb_s, tb_d, tb_pad, tb_h, tb_w, tb_relu);
    $fclose(fd);
    if (np != 9)
        $fatal(1, "[TB] FAIL: 解析 %s/meta.json 失败（期望 9 个字段，得到 %0d）。请用 tools/make_golden.py 生成。",
               dir, np);

    // ---- shift.txt（契约把 shift 单独放一个文件）----
    fd = $fopen({dir, "/shift.txt"}, "r");
    if (fd == 0)
        $fatal(1, "[TB] FAIL: 打不开 %s/shift.txt（契约 §4 要求）", dir);
    np = $fscanf(fd, "%d", tb_shift);
    $fclose(fd);
    if (np != 1)
        $fatal(1, "[TB] FAIL: 解析 %s/shift.txt 失败", dir);
    if (tb_shift < 0 || tb_shift > 31)
        $fatal(1, "[TB] FAIL: %s/shift.txt=%0d 超出 [0,31]", dir, tb_shift);

    if (tb_cin <= 0 || tb_cout <= 0 || tb_h <= 0 || tb_w <= 0)
        $fatal(1, "[TB] FAIL: %s 的形状字段非法 cin=%0d cout=%0d H=%0d W=%0d",
               dir, tb_cin, tb_cout, tb_h, tb_w);
    if ((tb_k - 1) * tb_d != 2 * tb_pad)
        $fatal(1, "[TB] FAIL: %s 不满足 same padding: (k-1)*d=%0d != 2*pad=%0d",
               dir, (tb_k - 1) * tb_d, 2 * tb_pad);

    tb_cin_pad  = ((tb_cin  + 7) / 8) * 8;
    tb_cout_pad = ((tb_cout + 7) / 8) * 8;
    tb_hout = (tb_h + 2 * tb_pad - ((tb_k - 1) * tb_d + 1)) / tb_s + 1;
    tb_wout = (tb_w + 2 * tb_pad - ((tb_k - 1) * tb_d + 1)) / tb_s + 1;
    wcb = (tb_k == 3) ? 12 : ((tb_k == 5) ? 28 : tb_k * tb_k);

    n_in  = longint'(tb_cin_pad)  * tb_h * tb_w;
    n_w   = longint'(tb_cout)     * tb_cin * wcb;
    n_b   = longint'(tb_cout)     * 4;
    n_out = longint'(tb_cout_pad) * tb_hout * tb_wout;

    tb_in_bytes   = n_in;
    tb_wgt_bytes  = n_w;
    tb_bias_bytes = n_b;
    tb_out_bytes  = n_out;

    in_mem   = new[int'(n_in)];
    wgt_mem  = new[int'(n_w)];
    bias_mem = new[int'(n_b)];
    exp_mem  = new[int'(n_out)];
    out_mem  = new[int'(n_out)];
    for (int i = 0; i < int'(n_out); i++)
        out_mem[i] = 8'hxx;          // 漏写/漏字节会以 X 暴露，而不是静默 0

    // 逐 c / 逐 x 失配计数：形状不变时跨 fuzz case 累加；形状变化才重建
    if (tb_c_err_total.size() != tb_cout_pad) begin
        tb_c_err_total = new[tb_cout_pad];
        tb_x_err_total = new[tb_wout];
        for (int i = 0; i < tb_cout_pad; i++) tb_c_err_total[i] = 0;
        for (int i = 0; i < tb_wout; i++)    tb_x_err_total[i] = 0;
    end

    fd = $fopen({dir, "/input_c8.bin"}, "rb");
    if (fd == 0) $fatal(1, "[TB] FAIL: 打不开 %s/input_c8.bin", dir);
    n = $fread(in_mem, fd);
    $fclose(fd);
    if (n != n_in)
        $fatal(1, "[TB] FAIL: %s/input_c8.bin 读了 %0d 字节，期望 %0d",
               dir, n, n_in);

    fd = $fopen({dir, "/weight.bin"}, "rb");
    if (fd == 0) $fatal(1, "[TB] FAIL: 打不开 %s/weight.bin", dir);
    n = $fread(wgt_mem, fd);
    $fclose(fd);
    if (n != n_w)
        $fatal(1, "[TB] FAIL: %s/weight.bin 读了 %0d 字节，期望 %0d",
               dir, n, n_w);

    fd = $fopen({dir, "/bias.bin"}, "rb");
    if (fd == 0) $fatal(1, "[TB] FAIL: 打不开 %s/bias.bin", dir);
    n = $fread(bias_mem, fd);
    $fclose(fd);
    if (n != n_b)
        $fatal(1, "[TB] FAIL: %s/bias.bin 读了 %0d 字节，期望 %0d",
               dir, n, n_b);

    fd = $fopen({dir, "/expected_c8.bin"}, "rb");
    if (fd == 0) $fatal(1, "[TB] FAIL: 打不开 %s/expected_c8.bin", dir);
    n = $fread(exp_mem, fd);
    $fclose(fd);
    if (n != n_out)
        $fatal(1, "[TB] FAIL: %s/expected_c8.bin 读了 %0d 字节，期望 %0d",
               dir, n, n_out);

    $display("[TB] 载入 %s：cin=%0d(->%0d) cout=%0d(->%0d) k=%0d s=%0d d=%0d p=%0d H=%0d W=%0d shift=%0d relu=%0d -> %0dx%0d | in=%0d w=%0d b=%0d out=%0d B",
             dir, tb_cin, tb_cin_pad, tb_cout, tb_cout_pad, tb_k, tb_s, tb_d,
             tb_pad, tb_h, tb_w, tb_shift, tb_relu, tb_hout, tb_wout,
             n_in, n_w, n_b, n_out);
endtask

// ---------------------------------------------------------------------------
// 9. 复位 / 驱动 / 比对
// ---------------------------------------------------------------------------
task automatic tb_reset();
    ap_rst_n   <= 1'b0;
    ctrl_start <= 1'b0;
    repeat (8) @(posedge ap_clk);
    ap_rst_n   <= 1'b1;
    repeat (4) @(posedge ap_clk);
endtask

// drive_start()：拉 ctrl_start 一拍，等 ctrl_done，统计拍数
// 拍数口径：ctrl_start 拉高后的第一个 posedge 起，到采样到 ctrl_done 的
// posedge 止（不含 done 之后的 drain）。
task automatic drive_start(input int max_cycles = `TB_MAX_CYCLES);
    int cyc;
    ctrl_start <= 1'b0;
    @(posedge ap_clk);
    ctrl_start <= 1'b1;
    @(posedge ap_clk);
    ctrl_start <= 1'b0;
    cyc = 0;
    // 在 negedge 采样 ctrl_done：模块常用单拍脉冲，posedge 处读会踩 NBA 竞争。
    while (!ctrl_done && cyc < max_cycles) begin
        @(negedge ap_clk);
        cyc++;
    end
    if (!ctrl_done)
        $fatal(1, "[TB] FAIL: 等待 ctrl_done 超时（%0d 拍，看门狗 %0d）",
               cyc, max_cycles);
    tb_cycles = cyc;
    repeat (`TB_DRAIN_CYCLES) @(posedge ap_clk);
endtask

// check_bytes(n_bad)：逐字节比对 expected_c8.bin
task automatic check_bytes(output int n_bad);
    int c, x;
    int nbytes;
    tb_byte_chk.compare(exp_mem, out_mem, tb_golden_dir,
                        tb_cout, tb_hout, tb_wout, n_bad);
    tb_last_bad     = n_bad;
    tb_total_bad   += n_bad;
    tb_total_checked += tb_out_bytes;
    if (n_bad == 0) tb_case_pass++;

    // ---- 逐 c / 逐 x 失配分布（跨 case 累加；与 golden 的 C8 字节序一致）----
    nbytes = int'(tb_out_bytes);
    for (int i = 0; i < nbytes; i++) begin
        if (exp_mem[i] !== out_mem[i]) begin
            c = (i / (tb_hout * tb_wout * 8)) * 8 + (i % 8);
            x = (i / 8) % tb_wout;
            tb_c_err_total[c] = tb_c_err_total[c] + 1;
            tb_x_err_total[x] = tb_x_err_total[x] + 1;
        end
        if (out_mem[i] === 8'hxx || out_mem[i] === 8'hzz)
            tb_unknown_bytes = tb_unknown_bytes + 1;
    end
    tb_c_top = tb_top5(tb_c_err_total);
    tb_x_top = tb_top5(tb_x_err_total);
    $display("[TB-PATTERN] c_top=[%s] x_top=[%s] unknown=%0d",
             tb_c_top, tb_x_top, tb_unknown_bytes);
endtask

// 把 DUT 实际输出按 golden 的 C8 字节序落盘（与 expected_c8.bin 逐字节同构）。
// X/Z（漏写）dump 成 0x00 并已计入 tb_unknown_bytes；需要区分时看 unknown=。
task automatic dump_out(input string path, input int n_bad);
    int fd;
    byte b;
    logic [7:0] ub;
    longint unsigned n;
    string p;
    if (tb_dump_out.len() == 0) return;
    if (tb_is_fuzz)
        p = $sformatf("%s.case_%03d", path, tb_case_count);
    else
        p = path;
    fd = $fopen(p, "w");
    if (fd == 0) begin
        $display("[TB] WARNING: 打不开 DUMP_OUT=%s（不影响验收判定，但拿不到全量数据）", p);
        return;
    end
    n = 0;
    for (int i = 0; i < int'(tb_out_bytes); i++) begin
        b = out_mem[i];
        if (b === 8'hxx || b === 8'hzz) b = 8'h00;
        ub = b;
        $fwrite(fd, "%c", ub);
        n++;
    end
    $fclose(fd);
    tb_dump_files++;
    $display("[TB] DUMP_OUT %s：%0d 字节（%s，n_bad=%0d）", p, n,
             tb_is_fuzz ? "fuzz 单 case" : "单组", n_bad);
endtask

// ---------------------------------------------------------------------------
// 10. 单组/多组执行
// ---------------------------------------------------------------------------
task automatic tb_one_case(input string dir, input int max_cycles);
    int n_bad;
    load_bins(dir);
    tb_reset();
    drive_start(max_cycles);
    check_bytes(n_bad);
    dump_out(tb_dump_out, n_bad);
    tb_case_count++;
    $display("[TB] CASE %0d %s：%0d / %0d 字节不一致，%0d 拍",
             tb_case_count, (n_bad == 0) ? "PASS" : "FAIL", n_bad,
             tb_out_bytes, tb_cycles);
endtask

// report_all(name)：统一格式报告 + 机器可读摘要
task automatic report_all(input string name);
    int fuzz_fail;
    real ratio;
    string status;
    fuzz_fail = tb_case_count - tb_case_pass;
    ratio = (tb_target_cycles > 0)
          ? $itor(tb_cycles) / $itor(tb_target_cycles) : 0.0;
    status = (tb_total_bad == 0 && fuzz_fail == 0 && tb_axi_errors == 0)
           ? "PASS" : "FAIL";

    $display("");
    $display("============================================================");
    $display(" [RTL_HAND] 统一验收报告");
    $display("============================================================");
    $display(" 模块        : %s", name);
    $display(" 形状        : %s  cin=%0d(->%0d) cout=%0d(->%0d) k=%0d s=%0d d=%0d p=%0d H=%0d W=%0d",
             tb_shape_name,
             tb_cin, tb_cin_pad, tb_cout, tb_cout_pad, tb_k, tb_s, tb_d, tb_pad,
             tb_h, tb_w);
    $display(" golden 目录 : %s", tb_golden_dir);
    if (tb_case_count > 0)
        $display(" 逐字节一致  : %0d / %0d 字节（%0d 组用例）",
                 tb_total_checked - tb_total_bad, tb_total_checked, tb_case_count);
    else
        $display(" 逐字节一致  : 0 / 0（没有执行用例）");
    $display(" 用例        : %0d / %0d 通过", tb_case_pass, tb_case_count);
    $display(" 拍数        : %0d", tb_cycles);
    if (tb_target_cycles > 0)
        $display(" 目标拍数    : %0d（本组 实测/目标 = %0.3f）",
                 tb_target_cycles, ratio);
    else
        $display(" 目标拍数    : 未登记（用 run_module.sh --target N 或 tools/targets.tsv 指定）");
    $display(" AXI 越界访问: %0d（读 %0d / 写 %0d）", tb_axi_errors, tb_rd_oor, tb_wr_oor);
    $display(" 错误分布    : c_top=[%s] x_top=[%s] unknown=%0d",
             tb_c_top, tb_x_top, tb_unknown_bytes);
    if (tb_dump_out.len() > 0)
        $display(" DUMP_OUT    : %s（fuzz 模式每 case 一个 .case_XXX）", tb_dump_out);
    $display(" 结论        : %s", status);
    $display("============================================================");
    $display("[TB-SUMMARY] module=%s shape=%s bytes=%0d/%0d cases=%0d/%0d cycles=%0d target=%0d ratio=%0.3f axi_errors=%0d c_top=[%s] x_top=[%s] unknown=%0d status=%s",
             name, tb_shape_name, tb_total_checked - tb_total_bad,
             tb_total_checked, tb_case_pass, tb_case_count, tb_cycles,
             tb_target_cycles, ratio, tb_axi_errors, tb_c_top, tb_x_top,
             tb_unknown_bytes, status);
endtask

// ---------------------------------------------------------------------------
// 11. 入口：tb_run(name)
//     +GOLDEN=<dir>            单组
//     +FUZZ_MANIFEST=<file>    多组（每行一个 golden 目录）
//     +MAX_CYCLES=N            覆盖单组看门狗
//     +TARGET_CYCLES=N         目标拍数（可选）
//     +DUMP_OUT=<file>         把 DUT 输出按 expected_c8.bin 的字节序落盘；
//                              fuzz 模式写 <file>.case_000 / .case_001 ...
// ---------------------------------------------------------------------------
task automatic tb_run(input string name);
    string dir, manifest, line;
    int fd, maxc;
    maxc = `TB_MAX_CYCLES;
    begin
        int dummy;
        dummy = $value$plusargs("MAX_CYCLES=%d", maxc);
        dummy = $value$plusargs("TARGET_CYCLES=%d", tb_target_cycles);
        dummy = $value$plusargs("SHAPE_NAME=%s", tb_shape_name);
        dummy = $value$plusargs("DUMP_OUT=%s", tb_dump_out);
    end

    if ($value$plusargs("GOLDEN=%s", dir)) begin
        tb_is_fuzz = 1'b0;
        tb_one_case(dir, maxc);
    end else if ($value$plusargs("FUZZ_MANIFEST=%s", manifest)) begin
        tb_is_fuzz = 1'b1;
        fd = $fopen(manifest, "r");
        if (fd == 0)
            $fatal(1, "[TB] FAIL: 打不开 fuzz manifest %s", manifest);
        while ($fgets(line, fd) > 0) begin
            line = tb_trim(line);
            if (line.len() > 0)
                tb_one_case(line, maxc);
        end
        $fclose(fd);
        if (tb_case_count == 0)
            $fatal(1, "[TB] FAIL: fuzz manifest %s 是空的", manifest);
    end else begin
        $fatal(1, "[TB] FAIL: 需要 +GOLDEN=<dir> 或 +FUZZ_MANIFEST=<file>");
    end

    report_all(name);
    if (tb_total_bad != 0 || tb_case_pass != tb_case_count ||
        tb_axi_errors != 0)
        $fatal(1, "[TB] FAIL: 验收未通过");
    $finish;
endtask

// ---------------------------------------------------------------------------
// 12. 自动启动（tb 只需 `define TB_MODULE_NAME）
// ---------------------------------------------------------------------------
// 全局仿真时间兜底（绝对时间）。600 s 在 1 ps 分辨率下 = 6e14 ps < 2^63 ps，
// 不会溢出；启动时再断言一次，防止有人覆盖成危险值。
initial begin
    // 2^63 ps / 1000 = 9,223,372,036,854,775 ns；超过它，换算到 1 ps 会溢出。
    // 用除法阈值而不是乘法，避免先溢出再比较。
    if (`TB_MAX_SIM_NS > 64'd9223372036854775)
        $fatal(1, "[TB] FAIL: TB_MAX_SIM_NS=%0d 超出 64 位有符号时间（1 ps 分辨率）",
               `TB_MAX_SIM_NS);
    #(`TB_MAX_SIM_NS);
    $fatal(1, "[TB] FAIL: 全局仿真时间看门狗触发（%0d ns），见 drive_start 的 MAX_CYCLES",
           `TB_MAX_SIM_NS);
end

initial begin
    ctrl_start = 1'b0;
    tb_total_checked = 0;
    tb_total_bad     = 0;
    tb_case_count    = 0;
    tb_case_pass     = 0;
    tb_axi_errors    = 0;
    tb_rd_oor        = 0;
    tb_wr_oor        = 0;
    tb_unknown_bytes = 0;
    tb_dump_files    = 0;
    tb_c_top         = "";
    tb_x_top         = "";
    tb_target_cycles = 0;
    tb_cycles        = 0;
`ifdef TB_MODULE_NAME
    tb_run(`TB_MODULE_NAME);
`else
    $fatal(1, "[TB] FAIL: tb 里缺少 `define TB_MODULE_NAME \"<模块名>\"");
`endif
end

assign ctrl_in_addr   = TB_IN_BASE;
assign ctrl_wgt_addr  = TB_WGT_BASE;
assign ctrl_bias_addr = TB_BIAS_BASE;
assign ctrl_out_addr  = TB_OUT_BASE;
assign ctrl_shift     = tb_shift[31:0];
assign ctrl_cin       = tb_cin[31:0];
assign ctrl_cout      = tb_cout[31:0];
assign ctrl_h         = tb_h[31:0];
assign ctrl_w         = tb_w[31:0];

`endif  // TB_COMMON_TASKS_VH
