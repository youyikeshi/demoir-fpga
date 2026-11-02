// ============================================================================
//  bin_sam.v —— SAM 三分支加权逐元素单元（单主内存口，C8 交错布局）
//
//  1. 语义（与 tools/gen_bin_golden.py::gen_sam 逐位一致）
//
//        acc = (x << shift) + w0[c]*y0 + w2[c]*y2 + w4[c]*y4        // 有符号
//        out = sat8(acc >> shift)                                   // 算术右移
//
//     · x / y0 / y2 / y4 都是 int8；w0 / w2 / w4 是 int16 有符号，逐通道。
//     · '>>' 必须是**算术**右移（负数向 -∞），与 Python 的 'acc >> shift' 一致；
//       Verilog 里只有左操作数是 signed 时 '>>>' 才是算术右移。
//     · sat8：>127→127，<-128→-128，否则取低 8 位。
//     · w=0 时 out == x（x<<shift 再 >>shift），这是自检的快速判据。
//     · 位宽按**真实范围**给（不再用 int64：XC7Z015 的 DSP48E1 只有 25×18，
//       操作数一宽就退化成 LUT 乘法器 / LUT 移位器）：
//           w*y   : 24 bit = 16+8 完整乘积（|w·y| < 2^22）
//           Σw·y  : 32 bit（3 项和 < 3·2^22 = 1.26e7 < 2^24）
//           (x<<shift) 不进 acc：见下面 §1.1 的代数变形
//       前提 P1：cfg_shift <= 15（验收形状 shift=6）。
//
//  1.1 代数变形（本轮把"x 左移"整条路砍掉，省掉一个 24 位桶形移位器）
//
//        v = ((x<<s) + P) >>> s ，P = w0*y0 + w2*y2 + w4*y4
//        因为 x<<s = x·2^s 是 2^s 的整数倍，向下取整可以精确拆开：
//            floor((x·2^s + P) / 2^s) = x + floor(P / 2^s)
//        ⇒ v = x + (P >>> s)，与原来的 acc>>>s **逐位相同**（含负数向 -∞），
//          而 (x<<s) 这一项（8 位左移 15 位 → 24 位，还要 4 级桶形）完全消失。
//        前提：P 不溢出 32 位（|P| < 3·2^22 ✓）、s <= 15（P2/P1）。
//
//  2. 内存映射（严格按 tb/tb_bin_sam.v 头部；地址相对 base_*）
//
//        x/y0/y2/y4  base_* + addr8(c,y,x)      int8   C8 交错
//        w0/w2/w4    base_* + c*2               int16  逐通道
//        out         base_out + addr8(c,y,x)    int8   C8 交错
//        addr8(c,y,x) = (c/8)*H*W*8 + y*W*8 + x*8 + (c%8)
//
//  3. 关键观察：C8 下按 64-bit 字遍历，地址是**线性**的
//
//        令组 cg = c/8、像素 p = y*W + x ∈ [0, H*W)，则
//            addr8(c,y,x) = (cg*H*W + p)*8 + (c%8)     // "字号 = cg*H*W + p"
//        一次 64-bit 读正好拿到**同一像素上 8 个通道**的 int8（字节 i = 通道
//        cg*8+i），而写口是 8-bit，一像素组正好 8 拍写完。
//        ⇒ FSM 只维护 (cg, pix) 两个线性计数器，**把 y/x 彻底消掉**：
//              读偏移 = (cg*H*W + pix) << 3
//              写地址 = base_out + (cg*H*W + pix)*8 + lane
//        这样既不会把 C8 误当连续布局，也不会出现"y/x 解算差一"的错误。
//        前提：C 是 8 的倍数（验收形状 32/64 均满足；golden 的
//        bytearray(C*H*W) 本身也要求这一条），且 H*W > 0。
//
//  4. 权重读取：为什么每 C8 组只读 6 个字
//
//        8 个通道的 int16 = 16 字节 = 2 个 8 字节对齐字；三组权重共 6 个字：
//            p_idx 0/1 → w0 的 cg 组 低/高 4 通道 : base_w0 + cg*16 + {0,8}
//            p_idx 2/3 → w2 同理 : base_w2 + cg*16 + {0,8}
//            p_idx 4/5 → w4 同理 : base_w4 + cg*16 + {0,8}
//        每个字 2 拍（ST_PLP 发地址 / ST_PLW 收数据）⇒ 每组 12 拍。
//        **假设 base_w0/base_w2/base_w4 是 8 字节对齐**（= 该组权重不会被切断）；
//        验收 TB 的 0x40000 / 0x48000 / 0x50000 都满足。若某处 base 只做 2 字节
//        对齐，需要改成按字节拼装，否则读出的 4 个 int16 会跨字错位。
//        int16 lane i 在 mem_rd_data[16*i +: 16]（i=0..3）。
//
//  5. FSM 与流水（1 个 64-bit 读口 + 1 个 8-bit 写口）
//
//        稳态：8 拍一个像素组（8 个通道），即**每拍 1 个输出字节 / 1 个元素**。
//        写口带宽是硬上限：N = C*H*W 个元素至少要 N 拍，任何实现都低不过它。
//
//        主循环就是一张 8 行的相位表（ph = 0..7），计算**塞在写窗口里**，
//        不额外占拍（ST_RUN 的 8 拍同时也是 8 个字节的写窗口）：
//
//          相位   读口（预取下一像素组）          计算流水                写口
//          ----   ---------------------------   ---------------------   ----
//          ph=0   发 x   读                      —                       ob[0]
//          ph=1   发 y0  读   采 x    → xg        —                       ob[1]
//          ph=2   发 y2  读   采 y0   → y0g       —                       ob[2]
//          ph=3   发 y4  读   采 y2   → y2g       prod0 = w0*y0 → prod0_r ob[3]
//          ph=4   —           采 y4   → y4g       prod2 = w2*y2 → prod2_r ob[4]
//          ph=5   —           —                  prod4 = w4*y4;
//                                                 psum = (prod0_r+prod2_r)
//                                                       +prod4 → psum_r
//                                                                       ob[5]
//          ph=6   —           —                  v = x + (psum_r>>>shift)
//                                                 out = sat8(v) → nb      ob[6]
//          ph=7   —           —                  —                       ob[7]
//                                                 （ob<=nb, pix++, wo++, 回 ph=0）
//
//        · 每拍只做**一次** 16x8 乘法（ph=3/4/5），三级流水把
//          "乘 → 累加 → 算术右移+饱和" 拆开，最长路径 ≈ 一个乘法器 + 加法；
//          ph=5 的加树是**两级平衡**的（(p0+p2)+p4），不再是 3 级串联；
//          ph=6 的 x+(psum>>>shift) 用的是 §1.1 的等价变形（s=shift）。
//          若后续要提频，还可以把 ph=6 的 sat8 再拆一级（ph=7 有空闲）。
//        · 读延迟是硬约束：mem_rd_en 在 t 拍拉高、addr 在 t 拍有效，
//          mem_rd_data 在 **t+1** 拍才有效。所以"发地址"和"采数据"必然相邻两拍
//          （见上表 ph=0..4 的错位）。所有采集寄存器都在 t+1 的时钟沿采样。
//
//        相位表之外的三段"非稳态"：
//          ST_PLP / ST_PLW : 每个 C8 组装入 6 个权重字（12 拍）。
//          ST_PRO          : 第一个像素组没有"上一拍预取"，用同一条 8 相位的
//                            流水跑一遍，但写口关闭（ph=0..3 的读是**本组**，
//                            不是下一组），ph=7 把 nb 装进 ob。
//          ST_RUN          : 稳态：写本组 ob、同时预取/计算下一组。
//        组尾：最后一像素组不再预取（!last_pix 门控）；若它不是最后一块，
//        回到 ST_PLP 重装权重，否则进 ST_DONE。
//
//  6. 有符号 / 位宽铁律（本文件重点检查项）
//     · part-select 是**无符号**：'mem_rd_data[8*i +: 8]' 是 0..255 的位模式，
//       必须按"int8"解释。这里由 mul16x8() 在 **24 位**上下文里把两个操作数
//       显式符号扩展（'{{8{w[15]}},w}' × '{{16{y[7]}},y}'），绝不让位模式直接
//       参与加法/乘法。
//     · 乘法的位宽必须**在函数/中间变量里定死**：'prod = w * y' 如果直接赋给
//       32 位寄存器，Verilog 会把两个操作数都扩到 32 位再乘 ⇒ 综合出 32×32
//       乘法器；原来用 sx16/sx8 扩到 64 位更糟（64×64）。包一层 24 位上下文后
//       表达式的**有效位宽**是 16×8（符号扩展位只是常量复制，DSP 推断会吸收），
//       落在 DSP48E1 的 25×18 之内 —— 综合报告里请确认乘法器确实落在 DSP 上。
//     · '>>>' 的左操作数必须是 signed（psum_r / v_c 都是 logic signed），否则是
//       逻辑右移，负数会变成巨大的无符号值 —— 这是本模块最容易翻车的一条。
//     · int16 lane 是**字节偏移**：第 i 个 int16 从 16*i 位开始（不是 8*i）。
//     · shift 只锁存一次，用 shift_r[3:0] 进移位器（前提 P1：shift <= 15）。
//     · sat8 用 or/and-reduce 实现（见 §6.1），不再是两条 32 位比较器。
//
//  6.1 sat8 等价变形（or/and-reduce）
//      记 v 为 W 位有符号数，s = v[W-1]：
//        · v >= 128  ⇔  !s && |v[W-2:7]     （权重>=128 的位里有 1）
//        · v < -128  ⇔  s && !(&v[W-1:7])  （负数在 [-128,-1] 内 ⇔ 高位全 1 且 bit7=1；
//                                            展开即 s && !(hi_and && v[7])）
//     ⇒ 输出三种结果：8'h7F / 8'h80 / v[7:0]，只需一条 24 位 or-reduce + 一条
//       and-reduce，替掉 v>127、v<-128 两条 32 位比较器。
//
//  7. 拍数预算
//        每组 = 6*2(权重) + 8(prologue) + H*W*8(写窗口)
//        sam_32_8x8 (C=32,H=W=8,N=2048) : 4 组 × (12+8+512) = 2128 拍（+1 拍启动）
//        sam_64_8x8 (C=64,H=W=8,N=4096) : 8 组 × (12+8+512) = 4256 拍（+1 拍启动）
//        写带宽下限是 2048 / 4096 拍 ⇒ 效率约 96%（权重与 prologue 开销约 4%）。
// ============================================================================
`timescale 1ns/1ps

module bin_sam (
    input  logic        ap_clk,
    input  logic        ap_rst_n,
    input  logic        ap_ce,           // ★ P3: 同步时钟使能（包装层的 u_ce）
    input  logic        ctrl_start,
    output logic        ctrl_done,
    output logic        ctrl_busy,
    input  logic [15:0] cfg_c, cfg_h, cfg_w,
    input  logic [5:0]  cfg_shift,
    input  logic [31:0] base_x, base_y0, base_y2, base_y4,
    input  logic [31:0] base_w0, base_w2, base_w4, base_out,
    output logic        mem_rd_en,
    output logic [31:0] mem_rd_addr,
    input  logic [63:0] mem_rd_data,
    output logic        mem_wr_en,
    output logic [31:0] mem_wr_addr,
    output logic [7:0]  mem_wr_data
);

    // ========================================================================
    // 0. 常量 / 状态编码
    // ========================================================================
    localparam int NPW = 6;             // 每 C8 组的权重 64-bit 字数：2 w0 + 2 w2 + 2 w4

    localparam logic [2:0] ST_IDLE = 3'd0,
                           ST_PLP  = 3'd1,   // 权重：发读地址
                           ST_PLW  = 3'd2,   // 权重：收数据（上一拍发的地址）
                           ST_PRO  = 3'd3,   // 首个像素组：8 相位流水，写口关闭
                           ST_RUN  = 3'd4,   // 稳态：写本组 8 字节 + 预取/计算下一组
                           ST_DONE = 3'd5;

    // ========================================================================
    // 1. 配置锁存 / 计数器
    // ========================================================================
    logic [2:0]  state;
    logic [31:0] base_x_r, base_y0_r, base_y2_r, base_y4_r;
    logic [31:0] base_w0_r, base_w2_r, base_w4_r, base_out_r;
    logic [31:0] hw_r;                  // H*W：每个 C8 组的像素数
    logic [31:0] ncg_r;                 // C8 组数 = ceil(C/8)
    logic [31:0] cg_r;                  // 当前 C8 组
    logic [31:0] pix_r;                 // 当前组内像素 p = y*W + x
    logic [2:0]  p_idx;                 // 权重字序号 0..NPW-1
    logic [2:0]  ph;                    // 稳态相位 0..7（= 写 burst 内的字节号）
    logic [5:0]  shift_r;               // cfg_shift
    // 字号计数器（替代 cg_r*hw_r 的 32×32 乘法，见 §3）
    logic [31:0] wo;                    // 线性字号 = cg*H*W + pix，恒为自增计数器

    // 每 C8 组 8 个通道的权重（int16 有符号）
    logic signed [15:0] w0_g [0:7];
    logic signed [15:0] w2_g [0:7];
    logic signed [15:0] w4_g [0:7];

    // 预取到的下一像素组 8 个通道的 int8（位模式，用前由 mul16x8 符号扩展）
    logic [7:0] xg  [0:7];
    logic [7:0] y0g [0:7];
    logic [7:0] y2g [0:7];
    logic [7:0] y4g [0:7];

    // 流水寄存器：三路乘积 → 累加（两级平衡加树）→ 饱和输出
    // ⚠ 位宽按真实范围：w(16b) × y(8b) ⇒ 24 bit（这里放 32 位寄存器，符号扩展
    //   在 mul16x8 内部定死在 24 位上下文，不会变成 32×32 乘法器）。
    logic signed [31:0] prod0_r [0:7];  // ph=3 写入
    logic signed [31:0] prod2_r [0:7];  // ph=4 写入
    // psum = Σ w·y（**不含** x<<shift，见文件头 §1.1）：
    //   |psum| <= 3·2^15·2^7 = 3·2^22 = 1.26e7 < 2^24 ⇒ 32 位绰绰有余
    //   （原来是 48 位：64 位加法器 ×3 ×8 lane ≈ 1500 LUT）
    logic signed [31:0] psum_r  [0:7];  // ph=5 写入

    // 写 burst 缓冲：ob = 本像素组待写的 8 字节，nb = 预取算出的下一组
    logic [7:0] ob [0:7];
    logic [7:0] nb [0:7];

    // ========================================================================
    // 2. 定点乘 / 饱和（位宽在函数内定死，避免被赋值上下文撑宽）
    //    part-select 是无符号的，必须先符号扩展再参与算术，见文件头 §6。
    // ========================================================================

    // int16(w) × int8(y) ⇒ 精确的 24 位乘积。
    // 函数返回值 24 位 ⇒ 乘法在 24 位上下文里做（操作数各扩到 24），
    // 真实乘积 |w·y| <= 2^15·2^7 = 2^22 < 2^23，不会截断。
    // 有效位宽只有 16×8，符号扩展位是常量复制 ⇒ DSP 推断可塞进一个 25×18。
    function automatic logic signed [23:0] mul16x8(input logic signed [15:0] w,
                                                   input logic [7:0] y);
        mul16x8 = $signed({{8{w[15]}}, w}) * $signed({{16{y[7]}}, y});
    endfunction

    // sat8：算术右移之后饱和到 [-128, 127]，再取低 8 位。
    // 用 or/and-reduce 代替两条 32 位比较器（等价性见文件头 §6.1）：
    //   hi_or  = |v[30:7]  ⇔ v>=0 时"权重>=128 的位里有 1"（即 v >= 128）
    //   hi_and = &v[30:7]  ⇔ v<0 时这些高位全 1
    //   负数在范围内 ⇔ bits[31:7] 全 1 ⇔ hi_and && v[7]
    function automatic logic [7:0] sat8(input logic signed [31:0] v);
        logic hi_or, hi_and;
        begin
            hi_or  = |v[30:7];
            hi_and = &v[30:7];
            sat8 = (!v[31] && hi_or)             ? 8'h7F :   // +127（v > 127）
                   ( v[31] && !(hi_and && v[7])) ? 8'h80 :   // -128（v < -128）
                   v[7:0];                                     // 在范围内
        end
    endfunction

    // ========================================================================
    // 3. 地址生成（C8 线性字号；见文件头 §3）
    //
    //  ⚠ 原实现 'wo_cur = cg_r * hw_r + pix_r' 是 **32×32 乘法器**，组合直接喂
    //    读地址 ⇒ 约 800~1000 LUT，且是读地址的关键路径。
    //    关键洞察：cg*H*W + pix 在遍历中**恒等于一个自增计数器**——
    //      组内推进 pix→pix+1 ⇒ 字号 +1；
    //      换组 pix 归零、cg→cg+1 ⇒ 字号 = cg*HW 恰好接上 (cg-1)*HW+(HW-1)+1。
    //    所以只要在"推进一个像素组"的那一拍做 wo+1，地址就是纯接线（0 LUT）。
    //    组/像素边界判断仍用 cg_r/pix_r（不参与地址计算）。
    // ========================================================================
    wire wo_step = (state == ST_RUN) && (ph == 3'd7);   // 组尾：推进一个像素组

    wire [31:0] off_cur = {wo[28:0], 3'b000};           // 字号*8（<<3 即接线）
    wire [31:0] off_nxt = off_cur + 32'd8;              // 下一像素组偏移

    wire last_pix = (pix_r == (hw_r  - 32'd1));
    wire last_cg  = (cg_r  == (ncg_r - 32'd1));

    // 权重读地址：6 个 8 字节对齐字，组 cg 占 base + cg*16 + {0,8}
    //   p_idx 0/1 → w0 低/高 4 通道；2/3 → w2；4/5 → w4
    //   用 (cg_r << 4) 显式 32 位算术，避开自决定位宽。
    logic [31:0] w_addr;
    always_comb begin
        case (p_idx)
            3'd0:    w_addr = base_w0_r + (cg_r << 4);
            3'd1:    w_addr = base_w0_r + (cg_r << 4) + 32'd8;
            3'd2:    w_addr = base_w2_r + (cg_r << 4);
            3'd3:    w_addr = base_w2_r + (cg_r << 4) + 32'd8;
            3'd4:    w_addr = base_w4_r + (cg_r << 4);
            3'd5:    w_addr = base_w4_r + (cg_r << 4) + 32'd8;
            default: w_addr = 32'd0;
        endcase
    end

    // ========================================================================
    // 4. 读端口（多路：权重 / 首组 x→y0→y2→y4 / 稳态预取 x→y0→y2→y4）
    //    mem_rd_en 本拍拉高 ⇒ mem_rd_data 下一拍有效（BRAM 语义）
    // ========================================================================
    wire rd_param = (state == ST_PLP);
    // ST_PRO：读的是**本组**（pix_r=0）；ST_RUN：读的是**下一组**（!last_pix 时）
    wire rd_lane  = ((state == ST_PRO) || ((state == ST_RUN) && !last_pix))
                    && (ph <= 3'd3);

    wire [31:0] pix_rd_off = (state == ST_PRO) ? off_cur : off_nxt;

    logic [31:0] pix_rd_addr;
    always_comb begin
        case (ph)
            3'd0:    pix_rd_addr = base_x_r  + pix_rd_off;
            3'd1:    pix_rd_addr = base_y0_r + pix_rd_off;
            3'd2:    pix_rd_addr = base_y2_r + pix_rd_off;
            default: pix_rd_addr = base_y4_r + pix_rd_off;   // ph==3
        endcase
    end

    assign mem_rd_en   = rd_param | rd_lane;
    assign mem_rd_addr = rd_param ? w_addr : (rd_lane ? pix_rd_addr : 32'd0);

    // ========================================================================
    // 5. 写端口（ST_RUN 的 8 拍，一字节/拍；ST_PRO 不写）
    // ========================================================================
    assign mem_wr_en   = (state == ST_RUN);
    assign mem_wr_addr = base_out_r + off_cur + {29'd0, ph};
    assign mem_wr_data = ob[ph];

    assign ctrl_busy   = (state != ST_IDLE) && (state != ST_DONE);

    // ========================================================================
    // 6. 计算流水（全部是**组合**中间量；结果在对应相位的时钟沿进寄存器）
    //
    //    ph=3 : 采到 y2g 的那一拍 → prod0 = w0*y0（y0g 已稳定）
    //    ph=4 : 采到 y4g 的那一拍 → prod2 = w2*y2（y2g 已稳定）
    //    ph=5 : 四个操作数（xg/y0g/y2g/y4g）都已稳定 → prod4，两级平衡加树
    //           得 psum = w0*y0 + w2*y2 + w4*y4
    //    ph=6 : v = x + (psum_r >>> shift) ≡ ((x<<shift)+Σw·y) >>> shift（§1.1）
    //           → sat8 → nb
    // ========================================================================
    logic signed [31:0] prod0_c [0:7];
    logic signed [31:0] prod2_c [0:7];
    logic signed [31:0] prod4_c [0:7];
    logic signed [31:0] s02_c   [0:7];      // 加树第 1 级：prod0_r + prod2_r
    logic signed [31:0] psum_c  [0:7];      // 加树第 2 级：s02_c + prod4_c
    logic signed [31:0] v6_c    [0:7];      // ph=6 的 v

    always_comb begin
        for (int i = 0; i < 8; i++) begin
            // 24 位上下文里算完再赋给 32 位寄存器（见 §6 铁律）
            prod0_c[i] = mul16x8(w0_g[i], y0g[i]);
            prod2_c[i] = mul16x8(w2_g[i], y2g[i]);
            prod4_c[i] = mul16x8(w4_g[i], y4g[i]);
            // 两级平衡加树：原来是 prod0_r + prod2_r + prod4_c + (x<<shift)
            // 一串 3 级 64 位加法器；现在最长的加法链只有 2 级，而且 x<<shift
            // 整条路被 §1.1 的等价变形干掉了。
            s02_c[i]   = prod0_r[i] + prod2_r[i];
            psum_c[i]  = s02_c[i] + prod4_c[i];
            // §1.1：v = x + (psum >>> shift)，与 ((x<<shift)+psum)>>>shift 逐位相同。
            // x 只有 8 位有效 ⇒ 先符号扩展到 32 位再参与加法（不左移，不占桶形移位器）。
            v6_c[i]    = $signed({{24{xg[i][7]}}, xg[i]})
                         + (psum_r[i] >>> shift_r[3:0]);   // 4 级桶形（P1：shift<=15）
        end
    end

    // 只有"存在要计算的像素组"时才推进流水：
    //   ST_PRO 一定算（算的就是当前组）；ST_RUN 只在还有下一组时算。
    wire run_ok = (state == ST_PRO) ||
                  ((state == ST_RUN) && !last_pix);

    // ========================================================================
    // 7. 主 FSM + 流水寄存器
    // ========================================================================
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            state      <= ST_IDLE;
            ctrl_done  <= 1'b0;
            base_x_r    <= 32'd0;
            base_y0_r   <= 32'd0;
            base_y2_r   <= 32'd0;
            base_y4_r   <= 32'd0;
            base_w0_r   <= 32'd0;
            base_w2_r   <= 32'd0;
            base_w4_r   <= 32'd0;
            base_out_r  <= 32'd0;
            hw_r    <= 32'd0;
            ncg_r   <= 32'd0;
            cg_r    <= 32'd0;
            pix_r   <= 32'd0;
            p_idx   <= 3'd0;
            ph      <= 3'd0;
            shift_r <= 6'd0;
            wo      <= 32'd0;
            for (int i = 0; i < 8; i++) begin
                w0_g[i] <= 16'sd0;  w2_g[i] <= 16'sd0;  w4_g[i] <= 16'sd0;
                xg[i]   <= 8'd0;    y0g[i]  <= 8'd0;
                y2g[i]  <= 8'd0;    y4g[i]  <= 8'd0;
                prod0_r[i] <= 32'sd0; prod2_r[i] <= 32'sd0; psum_r[i] <= 32'sd0;
                ob[i]   <= 8'd0;    nb[i]   <= 8'd0;
            end
        end else if (ap_ce && ctrl_start &&
                     ((state == ST_IDLE) || (state == ST_DONE))) begin
            // ---- 统一启动：锁配置、清遍历计数器 ----
            base_x_r    <= base_x;
            base_y0_r   <= base_y0;
            base_y2_r   <= base_y2;
            base_y4_r   <= base_y4;
            base_w0_r   <= base_w0;
            base_w2_r   <= base_w2;
            base_w4_r   <= base_w4;
            base_out_r  <= base_out;
            hw_r        <= {16'd0, cfg_h} * {16'd0, cfg_w};      // 显式 32 位上下文
            ncg_r       <= ({16'd0, cfg_c} + 32'd7) >> 3;        // ceil(C/8)
            shift_r     <= cfg_shift;
            cg_r        <= 32'd0;
            pix_r       <= 32'd0;
            p_idx       <= 3'd0;
            ph          <= 3'd0;
            wo          <= 32'd0;
            ctrl_done   <= 1'b0;
            if ((cfg_c == 16'd0) || (cfg_h == 16'd0) || (cfg_w == 16'd0)) begin
                // 退化形状：没有任何元素要写，直接 done，避免 ncg_r-1 回绕导致的死等
                // （同一 always 块内对同一变量的后一次非阻塞赋值生效，覆盖上面的 0）
                state      <= ST_DONE;
                ctrl_done  <= 1'b1;
            end else begin
                state <= ST_PLP;
            end
        end else if (ap_ce) begin                   // ★ P3: 冻结在 CE 门内
            // ---- 计算流水（与 FSM 并行；相位由 state/ph 组合译码）----
            // 字号计数器：每写完一个像素组（= ST_RUN 的 ph=7）推进 1。换组时
            // pix 归零、cg+1，字号恰好接上，所以这里不需要任何乘法（见 §3）。
            if (wo_step) wo <= wo + 32'd1;

            if (run_ok) begin
                case (ph)
                    3'd1: for (int i = 0; i < 8; i++) xg[i]  <= mem_rd_data[8*i +: 8];
                    3'd2: for (int i = 0; i < 8; i++) y0g[i] <= mem_rd_data[8*i +: 8];
                    3'd3: begin
                        for (int i = 0; i < 8; i++) y2g[i] <= mem_rd_data[8*i +: 8];
                        for (int i = 0; i < 8; i++) prod0_r[i] <= prod0_c[i];
                    end
                    3'd4: begin
                        for (int i = 0; i < 8; i++) y4g[i] <= mem_rd_data[8*i +: 8];
                        for (int i = 0; i < 8; i++) prod2_r[i] <= prod2_c[i];
                    end
                    3'd5: for (int i = 0; i < 8; i++) psum_r[i] <= psum_c[i];
                    3'd6: for (int i = 0; i < 8; i++) nb[i] <= sat8(v6_c[i]);
                    default: ;
                endcase
            end

            // ---- done：最后一块的最后一字节写完的那拍置 1，DONE 保持；
            //      下一次 start 清 0（TB 在 negedge 轮询，保持为高更稳）----
            ctrl_done <= (state == ST_DONE) ||
                         ((state == ST_RUN) && (ph == 3'd7) && last_pix && last_cg);

            case (state)
                ST_IDLE: state <= ST_IDLE;

                ST_PLP: state <= ST_PLW;

                ST_PLW: begin
                    // mem_rd_data 对应上一拍（ST_PLP）发出的权重地址
                    // int16 lane i 在 [16*i +: 16]；$signed 取补码语义
                    case (p_idx)
                        3'd0: begin
                            w0_g[0] <= $signed(mem_rd_data[15:0]);
                            w0_g[1] <= $signed(mem_rd_data[31:16]);
                            w0_g[2] <= $signed(mem_rd_data[47:32]);
                            w0_g[3] <= $signed(mem_rd_data[63:48]);
                        end
                        3'd1: begin
                            w0_g[4] <= $signed(mem_rd_data[15:0]);
                            w0_g[5] <= $signed(mem_rd_data[31:16]);
                            w0_g[6] <= $signed(mem_rd_data[47:32]);
                            w0_g[7] <= $signed(mem_rd_data[63:48]);
                        end
                        3'd2: begin
                            w2_g[0] <= $signed(mem_rd_data[15:0]);
                            w2_g[1] <= $signed(mem_rd_data[31:16]);
                            w2_g[2] <= $signed(mem_rd_data[47:32]);
                            w2_g[3] <= $signed(mem_rd_data[63:48]);
                        end
                        3'd3: begin
                            w2_g[4] <= $signed(mem_rd_data[15:0]);
                            w2_g[5] <= $signed(mem_rd_data[31:16]);
                            w2_g[6] <= $signed(mem_rd_data[47:32]);
                            w2_g[7] <= $signed(mem_rd_data[63:48]);
                        end
                        3'd4: begin
                            w4_g[0] <= $signed(mem_rd_data[15:0]);
                            w4_g[1] <= $signed(mem_rd_data[31:16]);
                            w4_g[2] <= $signed(mem_rd_data[47:32]);
                            w4_g[3] <= $signed(mem_rd_data[63:48]);
                        end
                        3'd5: begin
                            w4_g[4] <= $signed(mem_rd_data[15:0]);
                            w4_g[5] <= $signed(mem_rd_data[31:16]);
                            w4_g[6] <= $signed(mem_rd_data[47:32]);
                            w4_g[7] <= $signed(mem_rd_data[63:48]);
                        end
                        default: ;
                    endcase

                    if (p_idx == NPW - 1) begin
                        p_idx <= 3'd0;
                        pix_r <= 32'd0;
                        ph    <= 3'd0;
                        state <= ST_PRO;
                    end else begin
                        p_idx <= p_idx + 3'd1;
                        state <= ST_PLP;
                    end
                end

                ST_PRO: begin
                    // 首个像素组：8 相位流水，写口关闭
                    if (ph == 3'd7) begin
                        for (int i = 0; i < 8; i++) ob[i] <= nb[i];
                        ph    <= 3'd0;
                        state <= ST_RUN;
                    end else begin
                        ph <= ph + 3'd1;
                    end
                end

                ST_RUN: begin
                    // 稳态：本拍写 ob[ph]；ph=0..3 同时发下一组的读
                    if (ph == 3'd7) begin
                        if (last_pix) begin
                            ph <= 3'd0;
                            if (last_cg) begin
                                state <= ST_DONE;
                            end else begin
                                // 下一 C8 组：重新装 8 通道权重
                                cg_r  <= cg_r + 32'd1;
                                pix_r <= 32'd0;
                                p_idx <= 3'd0;
                                state <= ST_PLP;
                            end
                        end else begin
                            // 本组下一像素组：预取结果 nb 转正
                            for (int i = 0; i < 8; i++) ob[i] <= nb[i];
                            pix_r <= pix_r + 32'd1;
                            ph    <= 3'd0;
                        end
                    end else begin
                        ph <= ph + 3'd1;
                    end
                end

                ST_DONE: state <= ST_DONE;
                default: state <= ST_IDLE;
            endcase
        end
    end

endmodule
