// ============================================================================
//  k1_axi_wrap.v —— conv_k1_engine 的 AXI4-Lite 控制包装
//
//  目的：BD 集成时给 PS 一个标准 AXI4-Lite 从机；PS 侧 conv_engine.py
//        的寄存器偏移**零改动**，只换基地址。
//
//  HLS 引擎其实有**两个** AXI-Lite bundle（见 ps/pynq/conv_engine.py）：
//    · s_axi_ctrl  ：H/W/Hout/Wout/cin/cout/ksize/stride/dil/pad/shift/relu
//                    + AP_CTRL(0x00) / AP_RETURN(0x10)
//    · s_axi_control（默认 bundle，offset=slave）：四个 m_axi 基地址
//                    0x10(in) / 0x1C(wgt) / 0x28(bias) / 0x34(out)，各 64 位
//  两个 bundle 的偏移有重叠（例如 0x28 在 ctrl 是 Hout、在 offs 是 bias 基址），
//  所以**一个 64KB 窗口无法同时复现两套**。
//
//  本 wrapper 的做法：**一个 AXI4-Lite 从机，128KB 地址窗口，用 A[16] 分页**。
//    · A[16]=1 → ctrl 页（PS 基地址 0x4001_0000）
//    · A[16]=0 → offs 页（PS 基地址 0x4000_0000）
//  BD 里把本从机的地址范围设成 **128KB @ 0x4000_0000**（0x4000_0000–0x4001_FFFF），
//  PS 的 ctrl/offs 两个基地址都落在窗口内，A[16] 选页 ⇒ conv_engine.py 的
//  R_H/R_CIN/B_IN_FM 等常量一个都不用改。
//
//  寄存器映射（与 HLS 一致）：
//    ctrl 页（A[16]=1）：
//      0x00 AP_CTRL      W: bit0=start（自清）  R: [0]=0,[1]=done,[2]=idle,[3]=ready
//      0x10 AP_RETURN    R: 0=成功；1=ERR_NULL；2=ERR_SIZE；3=ERR_SHIFT
//      0x18 H            0x20 W          0x28 HOUT       0x30 WOUT
//      0x38 CIN          0x40 COUT       0x48 KSIZE      0x50 STRIDE
//      0x58 DIL          0x60 PAD        0x68 SHIFT      0x70 RELU
//      0x94 STATUS       R: [0]done [1]busy [2]err [7:4]err_code [8]idle
//    offs 页（A[16]=0，HLS 默认 bundle）：
//      0x10 IN_ADDR(lo) 0x14 IN_ADDR(hi)
//      0x1C WGT_ADDR    0x20 WGT_ADDR(hi)
//      0x28 BIAS_ADDR   0x2C BIAS_ADDR(hi)
//      0x34 OUT_ADDR    0x38 OUT_ADDR(hi)
//
//  工程约束（与 conv_k1_engine 一致）：
//    · 本 IP 只做 k=1 / stride=1 / dil=1 / pad=0；其它组合在 start 时返回 ERR_SIZE，
//      不启动引擎（避免"跑完但结果是错的"）。HOUT/WOUT 必须等于 H/W。
//    · cin 必须 8..CIN_MAX 且 %8==0；cout 1..COUT_MAX。
//    · RELU 是编译期参数（本构建 RELU=1）；RELU 寄存器只做读写回显，
//      写 0 不会改变硬件行为。C8 声明位 bit2/bit3 不参与任何逻辑（C8-only 构建）。
//    · 四个基地址寄存器是 64 位（低字在偏移处、高字在 +4），与 HLS 一致；
//      本引擎用 32 位地址，start 时高字必须为 0，否则 ERR_SIZE。
//
//  时钟/复位：s_axi_aclk / s_axi_aresetn（低有效）同时驱动引擎的 ap_clk/ap_rst_n。
//
//  对外 AXI4 master 从 4 个减到 2 个（BD 里 smc 的 SI 口 6→4，省 ~2k×2 LUT）：
//    · m_axi_rd_*  ：输入/权重/偏置三个**读**引擎在内部合并成 1 个读口；
//                    引擎侧三个接口一个都不改，只在外层加仲裁（§3.5）。
//    · m_axi_out_* ：输出写口，原样不变。
//  合并读口带 2-bit ARID tag（0=in / 1=w / 2=b），R 通道按锁存的授权回路由，
//  授权从 AR 握手一直保持到该 burst 的 RLAST 握手（burst 内绝不换人）。
// ============================================================================
`timescale 1ns / 1ps

module k1_axi_wrap #(
    parameter int CIN_MAX  = 192,
    parameter int COUT_MAX = 96,
    parameter int LANES    = 32,
    parameter int NBUF     = 4,
    parameter int OBUF_N   = 4,
    parameter bit RELU     = 1'b1
)(
    // ---- AXI4-Lite slave ----
    input  logic        s_axi_aclk,
    input  logic        s_axi_aresetn,
    input  logic [31:0] s_axi_awaddr,
    input  logic        s_axi_awvalid,
    output logic        s_axi_awready,
    input  logic [31:0] s_axi_wdata,
    input  logic [3:0]  s_axi_wstrb,
    input  logic        s_axi_wvalid,
    output logic        s_axi_wready,
    output logic [1:0]  s_axi_bresp,
    output logic        s_axi_bvalid,
    input  logic        s_axi_bready,
    input  logic [31:0] s_axi_araddr,
    input  logic        s_axi_arvalid,
    output logic        s_axi_arready,
    output logic [31:0] s_axi_rdata,
    output logic [1:0]  s_axi_rresp,
    output logic        s_axi_rvalid,
    input  logic        s_axi_rready,

    // ---- AXI4 master：合并后的读（in/w/b 三路仲裁成 1 路；2-bit tag）----
    output logic        m_axi_rd_arvalid,  input logic m_axi_rd_arready,
    output logic [31:0] m_axi_rd_araddr,   output logic [7:0] m_axi_rd_arlen,
    output logic [1:0]  m_axi_rd_arid,
    input  logic        m_axi_rd_rvalid,   output logic m_axi_rd_rready,
    input  logic [63:0] m_axi_rd_rdata,    input logic m_axi_rd_rlast,
    input  logic [1:0]  m_axi_rd_rid,

    // ---- AXI4 master：输出 ----
    output logic        m_axi_out_awvalid, input logic m_axi_out_awready,
    output logic [31:0] m_axi_out_awaddr,  output logic [7:0] m_axi_out_awlen,
    output logic        m_axi_out_wvalid,  input logic m_axi_out_wready,
    output logic [63:0] m_axi_out_wdata,   output logic [7:0] m_axi_out_wstrb,
    output logic        m_axi_out_wlast,
    input  logic        m_axi_out_bvalid,  output logic m_axi_out_bready
);

    // ========================================================================
    // 0. 寄存器
    // ========================================================================
    logic [31:0] r_h, r_w, r_hout, r_wout;
    logic [31:0] r_cin, r_cout, r_ksize, r_stride, r_dil, r_pad, r_shift, r_relu;
    logic [31:0] r_in_lo, r_in_hi, r_wgt_lo, r_wgt_hi;
    logic [31:0] r_bias_lo, r_bias_hi, r_out_lo, r_out_hi;

    logic        start_req;       // 1 拍脉冲，驱动引擎 ctrl_start
    logic        done_sticky;     // 引擎 ctrl_done 锁存
    logic        err_sticky;
    logic [3:0]  err_code;
    logic [31:0] ret_code;

    logic        ctrl_done, ctrl_busy;

    // HLS 错误码（conv2d_accel.h）
    localparam [3:0] ERR_NULL  = 4'd1,
                     ERR_SIZE  = 4'd2,
                     ERR_SHIFT = 4'd3,
                     ERR_LAYOUT= 4'd4;

    // ========================================================================
    // 1. 启动前校验（纯组合，寄存器的值在 start 写时已稳定）
    // ========================================================================
    logic [3:0] vcode;
    always_comb begin
        vcode = 4'd0;
        if ((r_in_hi != 0) || (r_wgt_hi != 0) || (r_bias_hi != 0) || (r_out_hi != 0))
            vcode = ERR_SIZE;                                  // 引擎只用 32 位地址
        else if ((r_in_lo == 0) || (r_wgt_lo == 0) || (r_bias_lo == 0) || (r_out_lo == 0))
            vcode = ERR_NULL;
        else if ((r_h == 0) || (r_w == 0) || (r_h > 32'd4096) || (r_w > 32'd4096))
            vcode = ERR_SIZE;
        else if ((r_hout != r_h) || (r_wout != r_w))            // k=1,s=1,pad=0
            vcode = ERR_SIZE;
        else if ((r_cin < 32'd8) || (r_cin > CIN_MAX) || (r_cin[2:0] != 3'd0))
            vcode = ERR_SIZE;
        else if ((r_cout < 32'd1) || (r_cout > COUT_MAX))
            vcode = ERR_SIZE;
        else if ((r_ksize != 32'd1) || (r_stride != 32'd1) ||
                 (r_dil   != 32'd1) || (r_pad    != 32'd0))
            vcode = ERR_SIZE;                                  // 本 IP 只支持 k=1/s=1/d=1/p=0
        else if ((r_shift < 32'd1) || (r_shift > 32'd31))
            vcode = ERR_SHIFT;
    end

    // ========================================================================
    // 2. AXI4-Lite 写通道
    // ========================================================================
    logic        aw_done, w_done, bvalid;
    logic [31:0] awaddr_l, wdata_l;
    logic [3:0]  wstrb_l;

    wire aw_hs = s_axi_awvalid && s_axi_awready;
    wire w_hs  = s_axi_wvalid  && s_axi_wready;

    assign s_axi_awready = !aw_done && !bvalid;
    assign s_axi_wready  = !w_done  && !bvalid;

    wire do_wr = (aw_done || aw_hs) && (w_done || w_hs) && !bvalid;

    wire [31:0] wr_addr = aw_done ? awaddr_l : s_axi_awaddr;
    wire [31:0] wr_data = w_done  ? wdata_l  : s_axi_wdata;
    wire [3:0]  wr_strb = w_done  ? wstrb_l  : s_axi_wstrb;
    wire        wr_page = wr_addr[16];      // 1=ctrl, 0=offs
    wire [7:0]  wr_off  = wr_addr[7:0];

    function automatic [31:0] bmerge(input [31:0] old, input [31:0] nw, input [3:0] stb);
        bmerge[7:0]   = stb[0] ? nw[7:0]   : old[7:0];
        bmerge[15:8]  = stb[1] ? nw[15:8]  : old[15:8];
        bmerge[23:16] = stb[2] ? nw[23:16] : old[23:16];
        bmerge[31:24] = stb[3] ? nw[31:24] : old[31:24];
    endfunction

    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) begin
            aw_done <= 1'b0; w_done <= 1'b0; bvalid <= 1'b0;
            awaddr_l <= 32'd0; wdata_l <= 32'd0; wstrb_l <= 4'd0;
            r_h <= 32'd0; r_w <= 32'd0; r_hout <= 32'd0; r_wout <= 32'd0;
            r_cin <= 32'd0; r_cout <= 32'd0; r_ksize <= 32'd0; r_stride <= 32'd0;
            r_dil <= 32'd0; r_pad <= 32'd0; r_shift <= 32'd0;
            r_relu <= {31'd0, RELU};   // 未写 0x70 时沿用编译期默认
            r_in_lo <= 32'd0; r_in_hi <= 32'd0; r_wgt_lo <= 32'd0; r_wgt_hi <= 32'd0;
            r_bias_lo <= 32'd0; r_bias_hi <= 32'd0; r_out_lo <= 32'd0; r_out_hi <= 32'd0;
            start_req <= 1'b0; done_sticky <= 1'b0; err_sticky <= 1'b0;
            err_code <= 4'd0; ret_code <= 32'd0;
        end else begin
            // 默认：start 只维持 1 拍
            start_req <= 1'b0;

            if (aw_hs) begin awaddr_l <= s_axi_awaddr; aw_done <= 1'b1; end
            if (w_hs)  begin wdata_l <= s_axi_wdata; wstrb_l <= s_axi_wstrb; w_done <= 1'b1; end

            if (do_wr) begin
                aw_done <= 1'b0;
                w_done  <= 1'b0;
                bvalid  <= 1'b1;

                if (wr_page) begin
                    // ---------------- ctrl 页 ----------------
                    case (wr_off)
                        8'h00: if (wr_data[0] && !ctrl_busy) begin
                            if (vcode == 4'd0) begin
                                start_req   <= 1'b1;
                                done_sticky <= 1'b0;
                                err_sticky  <= 1'b0;
                                err_code    <= 4'd0;
                                ret_code    <= 32'd0;
                            end else begin
                                // 校验失败：不启动，但置 done，让 PS 的轮询能退出
                                done_sticky <= 1'b1;
                                err_sticky  <= 1'b1;
                                err_code    <= vcode;
                                ret_code    <= {28'd0, vcode};
                            end
                        end
                        8'h18: r_h     <= bmerge(r_h,     wr_data, wr_strb);
                        8'h20: r_w     <= bmerge(r_w,     wr_data, wr_strb);
                        8'h28: r_hout  <= bmerge(r_hout,  wr_data, wr_strb);
                        8'h30: r_wout  <= bmerge(r_wout,  wr_data, wr_strb);
                        8'h38: r_cin   <= bmerge(r_cin,   wr_data, wr_strb);
                        8'h40: r_cout  <= bmerge(r_cout,  wr_data, wr_strb);
                        8'h48: r_ksize <= bmerge(r_ksize, wr_data, wr_strb);
                        8'h50: r_stride<= bmerge(r_stride,wr_data, wr_strb);
                        8'h58: r_dil   <= bmerge(r_dil,   wr_data, wr_strb);
                        8'h60: r_pad   <= bmerge(r_pad,   wr_data, wr_strb);
                        8'h68: r_shift <= bmerge(r_shift, wr_data, wr_strb);
                        8'h70: r_relu  <= bmerge(r_relu,  wr_data, wr_strb);
                        default: ;
                    endcase
                end else begin
                    // ---------------- offs 页 ----------------
                    case (wr_off)
                        8'h10: r_in_lo   <= bmerge(r_in_lo,   wr_data, wr_strb);
                        8'h14: r_in_hi   <= bmerge(r_in_hi,   wr_data, wr_strb);
                        8'h1C: r_wgt_lo  <= bmerge(r_wgt_lo,  wr_data, wr_strb);
                        8'h20: r_wgt_hi  <= bmerge(r_wgt_hi,  wr_data, wr_strb);
                        8'h28: r_bias_lo <= bmerge(r_bias_lo, wr_data, wr_strb);
                        8'h2C: r_bias_hi <= bmerge(r_bias_hi, wr_data, wr_strb);
                        8'h34: r_out_lo  <= bmerge(r_out_lo,  wr_data, wr_strb);
                        8'h38: r_out_hi  <= bmerge(r_out_hi,  wr_data, wr_strb);
                        default: ;
                    endcase
                end
            end

            if (bvalid && s_axi_bready) bvalid <= 1'b0;

            // 引擎完成脉冲 → 锁存
            if (ctrl_done) done_sticky <= 1'b1;
        end
    end

    // ⚠ 2026-10-27 上板致命 bug：这里原本**漏了 s_axi_bvalid 的驱动**。
    //   端口在第 75 行声明为 output logic，全文只有内部 bvalid 寄存器的
    //   置位/清零，却没有把它接到端口上 ⇒ 综合成常量 0 ⇒ PS 的每一笔写
    //   都等不到 B 响应 ⇒ CPU 停在 MMIO 写上一去不回（整机卡死，ARP 都不回）。
    //   为什么仿真没抓到：TB 的写任务不等 bvalid；读通路 s_axi_rvalid 是好的，
    //   纯读的冒烟测试也照样通过。k3/k5 的同名 wrapper 都有这一行，只有 k1 漏了。
    assign s_axi_bvalid = bvalid;
    assign s_axi_bresp  = 2'b00;

    // ========================================================================
    // 3. AXI4-Lite 读通道
    // ========================================================================
    logic [31:0] araddr_l;
    logic        rvalid;

    assign s_axi_arready = !rvalid;

    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) begin
            araddr_l <= 32'd0;
            rvalid   <= 1'b0;
        end else begin
            if (s_axi_arvalid && s_axi_arready) begin
                araddr_l <= s_axi_araddr;
                rvalid   <= 1'b1;
            end else if (rvalid && s_axi_rready) begin
                rvalid   <= 1'b0;
            end
        end
    end

    logic [31:0] status_w;
    always_comb begin
        status_w        = 32'd0;
        status_w[0]     = done_sticky;
        status_w[1]     = ctrl_busy;
        status_w[2]     = err_sticky;
        status_w[7:4]   = err_code;
        status_w[8]     = ctrl_busy ? 1'b0 : 1'b1;
    end

    logic [31:0] ap_ctrl_rd;
    always_comb begin
        ap_ctrl_rd    = 32'd0;
        ap_ctrl_rd[0] = 1'b0;                 // start 自清
        ap_ctrl_rd[1] = done_sticky;          // ap_done
        ap_ctrl_rd[2] = ctrl_busy ? 1'b0 : 1'b1; // ap_idle
        ap_ctrl_rd[3] = ctrl_busy ? 1'b0 : 1'b1; // ap_ready
    end

    logic [31:0] rd_mux;
    always_comb begin
        rd_mux = 32'd0;
        if (araddr_l[16]) begin
            // ctrl 页
            case (araddr_l[7:0])
                8'h00: rd_mux = ap_ctrl_rd;
                8'h10: rd_mux = ret_code;
                8'h18: rd_mux = r_h;
                8'h20: rd_mux = r_w;
                8'h28: rd_mux = r_hout;
                8'h30: rd_mux = r_wout;
                8'h38: rd_mux = r_cin;
                8'h40: rd_mux = r_cout;
                8'h48: rd_mux = r_ksize;
                8'h50: rd_mux = r_stride;
                8'h58: rd_mux = r_dil;
                8'h60: rd_mux = r_pad;
                8'h68: rd_mux = r_shift;
                8'h70: rd_mux = r_relu;
                8'h94: rd_mux = status_w;
                default: rd_mux = 32'd0;
            endcase
        end else begin
            // offs 页（HLS 默认 bundle）
            case (araddr_l[7:0])
                8'h00: rd_mux = ap_ctrl_rd;
                8'h10: rd_mux = r_in_lo;
                8'h14: rd_mux = r_in_hi;
                8'h1C: rd_mux = r_wgt_lo;
                8'h20: rd_mux = r_wgt_hi;
                8'h28: rd_mux = r_bias_lo;
                8'h2C: rd_mux = r_bias_hi;
                8'h34: rd_mux = r_out_lo;
                8'h38: rd_mux = r_out_hi;
                default: rd_mux = 32'd0;
            endcase
        end
    end

    assign s_axi_rdata = rd_mux;
    assign s_axi_rresp = 2'b00;
    assign s_axi_rvalid = rvalid;

    // ========================================================================
    // 3.5 三路读 master → 1 路外部读口（合并 in/w/b）
    // ------------------------------------------------------------------------
    //  外部只有 1 个读口 m_axi_rd_*，内部仍保留 3 个独立的“请求→数据”握手。
    //  · **授权在整个 burst 内保持**：从 AR 握手一直占到这拍 RLAST 握手，
    //    中途绝不换人 —— 否则会把两个不同 ARADDR/ARLEN 的 beat 混在一起。
    //    （外部 ARADDR/ARLEN/ARID 全部由锁存的 rd_gnt 一起选，不在 burst 中途变。）
    //  · 优先级 in > b > w：
    //      - 输入预取是 4 缓冲的硬实时流（每像素 cin/8 笔、每 64 拍要一笔），最高；
    //      - 偏置只在 start 后装一次（32 拍），且那时输入还没开始；
    //      - 权重装载改成“pass 启动时后台预装下一 pass”（见 conv_k1_engine §9），
    //        只要 ~1/8 带宽；若给它高优先级，一条 burst 会连续占住读口 2k 拍、
    //        把输入 4 缓冲饿死 ⇒ 计算停顿。所以权重最低。
    //  · 2-bit tag：ARID=0/1/2；R 通道按**锁存的授权**回路由（burst 级无歧义），
    //    同时用 RID 做仿真期一致性检查（从机必须原样回 ARID）。
    // ========================================================================
    localparam logic [1:0] TAG_IN = 2'd0, TAG_W = 2'd1, TAG_B = 2'd2;

    // 引擎侧三路读 master（保持原来的“精简 AXI4 读”接口，一个都没改）
    logic        in_arvalid, in_arready, in_rvalid, in_rready;
    logic [31:0] in_araddr;
    logic [7:0]  in_arlen;
    logic [63:0] in_rdata;
    logic        in_rlast;

    logic        w_arvalid, w_arready, w_rvalid, w_rready;
    logic [31:0] w_araddr;
    logic [7:0]  w_arlen;
    logic [63:0] w_rdata;
    logic        w_rlast;

    logic        b_arvalid, b_arready, b_rvalid, b_rready;
    logic [31:0] b_araddr;
    logic [7:0]  b_arlen;
    logic [63:0] b_rdata;
    logic        b_rlast;

    // ========================================================================
    // 读通道：**多笔在飞 + 按 ID 分流**（2026-10-28 v5）
    // ------------------------------------------------------------------------
    //  旧版：一次只允许一笔 burst 在飞，必须等它的 RLAST 完成握手才发下一个 AR。
    //        而 k1 引擎的输入预取全是 arlen=0 的**单拍**（C8 布局下同一像素各
    //        通道组相隔 H*W*8 字节，连不成 burst），于是每笔单拍都付一次完整
    //        AXI 往返。上板实测每笔 ≈56 拍、且与 cin 无关：
    //          cin=48  → 6 笔×56 ≈ 336 拍/像素（实测额外 339）
    //          cin=144 → 18 笔×56 ≈ 1008 拍/像素（实测额外 1014）
    //        ⇒ 引擎本身 II=1（仿真 issue 占空比 99%），却被拖成 8 拍/通道。
    //  新版：三个通道本来就有各自的 ARID（TAG_IN/TAG_B/TAG_W），这里把它用起来：
    //        允许多笔同时在飞，R 数据用 m_axi_rd_rid 分流回各自通道。
    //        同 ID 的 burst 按序返回（AXI 保证），所以每个通道只需记"在飞 beat 数"。
    // ========================================================================

    logic [11:0] o_in, o_b, o_w;           // 各通道在飞 beat 数（arlen 可到 255）
    logic [10:0] o_tot;
    logic        rr;                       // 轮转优先级，防 wgt/bias 被 in 饿死

    assign o_tot  = {1'b0, o_in} + {1'b0, o_b} + {1'b0, o_w};
    // MAXO=24：实测 12 时仍有 1.5× 开销（单笔往返 56 拍摊不完），24 目标 1.3×
    wire can_ar = (o_tot < 12'd24);

    wire [1:0] ar_tag = rr ? (w_arvalid ? TAG_W  : (b_arvalid ? TAG_B : TAG_IN))
                           : (in_arvalid ? TAG_IN : (b_arvalid ? TAG_B : TAG_W));
    wire       ar_req = (in_arvalid || b_arvalid || w_arvalid) && can_ar;

    assign m_axi_rd_arvalid = ar_req;
    assign m_axi_rd_arid    = ar_tag;
    assign m_axi_rd_araddr  = (ar_tag == TAG_IN) ? in_araddr :
                              (ar_tag == TAG_B)  ? b_araddr  : w_araddr;
    assign m_axi_rd_arlen   = (ar_tag == TAG_IN) ? in_arlen :
                              (ar_tag == TAG_B)  ? b_arlen   : w_arlen;

    // 只有被选中的通道看到 ARREADY（且与外部 ARREADY 同拍）
    assign in_arready = ar_req && (ar_tag == TAG_IN) && m_axi_rd_arready;
    assign b_arready  = ar_req && (ar_tag == TAG_B)  && m_axi_rd_arready;
    assign w_arready  = ar_req && (ar_tag == TAG_W)  && m_axi_rd_arready;

    // ---- R 通道：按 RID 分流 ----
    wire [1:0] rid = m_axi_rd_rid;
    assign in_rvalid = m_axi_rd_rvalid && (rid == TAG_IN) && (o_in != 12'd0);
    assign b_rvalid  = m_axi_rd_rvalid && (rid == TAG_B)  && (o_b  != 12'd0);
    assign w_rvalid  = m_axi_rd_rvalid && (rid == TAG_W)  && (o_w  != 12'd0);

    assign in_rdata = m_axi_rd_rdata;
    assign b_rdata  = m_axi_rd_rdata;
    assign w_rdata  = m_axi_rd_rdata;
    assign in_rlast = m_axi_rd_rlast;
    assign b_rlast  = m_axi_rd_rlast;
    assign w_rlast  = m_axi_rd_rlast;

    assign m_axi_rd_rready = (rid == TAG_IN) ? in_rready :
                             (rid == TAG_B)  ? b_rready  : w_rready;

    // ---- 在飞 beat 计数（同拍可能既有 AR 又有 R，一次赋值内合并）----
    logic [12:0] add_in, add_b, add_w;
    assign add_in = in_arready ? ({3'b000, in_arlen} + 12'd1) : 12'd0;
    assign add_b  = b_arready  ? ({3'b000, b_arlen}  + 12'd1) : 12'd0;
    assign add_w  = w_arready  ? ({3'b000, w_arlen}  + 12'd1) : 12'd0;

    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) begin
            o_in <= 12'd0; o_b <= 12'd0; o_w <= 12'd0; rr <= 1'b0;
        end else begin
            o_in <= o_in + add_in - ((in_rvalid && in_rready) ? 12'd1 : 12'd0);
            o_b  <= o_b  + add_b  - ((b_rvalid  && b_rready)  ? 12'd1 : 12'd0);
            o_w  <= o_w  + add_w  - ((w_rvalid  && w_rready)  ? 12'd1 : 12'd0);
            if (m_axi_rd_arvalid && m_axi_rd_arready) rr <= ~rr;
        end
    end

    // synopsys translate_off
    always @(posedge s_axi_aclk) begin
        if (s_axi_aresetn && m_axi_rd_rvalid &&
            ((rid == TAG_IN) && (o_in == 12'd0) ||
             (rid == TAG_B)  && (o_b  == 12'd0) ||
             (rid == TAG_W)  && (o_w  == 12'd0)))
            $display("[K1-AXI] ERROR: RID=%0d 回来时该通道无在飞请求", rid);
    end
    // synopsys translate_on


    // ========================================================================
    // 4. 引擎例化（三个读引擎的端口与之前完全一致，只是接到内部仲裁器）
    // ========================================================================
    conv_k1_engine #(
        .CIN(64), .COUT(64), .W(320), .H(320), .PAD(0), .STRIDE(1), .DIL(1),
        .RELU(RELU),
        .LANES(LANES), .CIN_MAX(CIN_MAX), .COUT_MAX(COUT_MAX),
        .NBUF(NBUF), .OBUF_N(OBUF_N)
    ) u_engine (
        .ap_clk       (s_axi_aclk),
        .ap_rst_n     (s_axi_aresetn),

        .m_axi_in_arvalid (in_arvalid), .m_axi_in_arready (in_arready),
        .m_axi_in_araddr  (in_araddr),  .m_axi_in_arlen   (in_arlen),
        .m_axi_in_rvalid  (in_rvalid),  .m_axi_in_rready  (in_rready),
        .m_axi_in_rdata   (in_rdata),   .m_axi_in_rlast   (in_rlast),

        .m_axi_w_arvalid  (w_arvalid),  .m_axi_w_arready  (w_arready),
        .m_axi_w_araddr   (w_araddr),   .m_axi_w_arlen    (w_arlen),
        .m_axi_w_rvalid   (w_rvalid),   .m_axi_w_rready   (w_rready),
        .m_axi_w_rdata    (w_rdata),    .m_axi_w_rlast    (w_rlast),

        .m_axi_b_arvalid  (b_arvalid),  .m_axi_b_arready  (b_arready),
        .m_axi_b_araddr   (b_araddr),   .m_axi_b_arlen    (b_arlen),
        .m_axi_b_rvalid   (b_rvalid),   .m_axi_b_rready   (b_rready),
        .m_axi_b_rdata    (b_rdata),    .m_axi_b_rlast    (b_rlast),

        .m_axi_out_awvalid(m_axi_out_awvalid),.m_axi_out_awready(m_axi_out_awready),
        .m_axi_out_awaddr (m_axi_out_awaddr), .m_axi_out_awlen  (m_axi_out_awlen),
        .m_axi_out_wvalid (m_axi_out_wvalid), .m_axi_out_wready (m_axi_out_wready),
        .m_axi_out_wdata  (m_axi_out_wdata),  .m_axi_out_wstrb  (m_axi_out_wstrb),
        .m_axi_out_wlast  (m_axi_out_wlast),
        .m_axi_out_bvalid (m_axi_out_bvalid), .m_axi_out_bready (m_axi_out_bready),

        .ctrl_in_addr   (r_in_lo),
        .ctrl_wgt_addr  (r_wgt_lo),
        .ctrl_bias_addr (r_bias_lo),
        .ctrl_out_addr  (r_out_lo),
        .ctrl_shift     (r_shift),
        .ctrl_cin       (r_cin),
        .ctrl_cout      (r_cout),
        .ctrl_h         (r_h),
        .ctrl_w         (r_w),
        .ctrl_start     (start_req),
        .ctrl_relu      (r_relu[0]),
        .ctrl_done      (ctrl_done),
        .ctrl_busy      (ctrl_busy)
    );

endmodule
