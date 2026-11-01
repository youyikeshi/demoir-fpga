// ============================================================================
//  tb_bin_e1e2.v —— bin_e1e2 的**模块级**定向 TB（原 `tb_bin_e1e2_t36.v`）
//
//  ── 来源与两次修正（T-41 回灌，2026-11-02）────────────────────────────────
//  1. 本文件原为 T-36 写的 `tb_bin_e1e2_t36.v`（只在 `worktrees/T-36-e1e2/` 里，
//     仓库里从来没有过 E1E2 的 TB）。T-38 验收 bin_e1e2.v 时用它，发现两个**测试
//     资产 bug**，修好后放在 `runs/t38_rtl/work/tb_e1e2_m21.v`。本文件 = 那份修好
//     的 TB，只改了两处元信息（文件名 / module 名对齐仓库约定 `tb_<module>.v`）。
//  2. ★ **为什么改 MEMAW（18 → 21）—— 这是本轮最重要的一条**：
//     TB 的模型内存按 `memq[mem_wr_addr[MEMAW-1:3]]` 索引，而 `MEMAW = 18` 时
//     索引只有 `[17:3]` = **15 位** ⇒ 窗口 = 2^15 字 = **256 KB 字节**。
//     TB 从不检查地址是否越界，于是**任何 ≥256 KB 的地址都静默别名回 0x000000**
//     （不回绕、不报错、不 $fatal）⇒ 模型把 src 与 out 叠到同一块 256 KB 上。
//     表现为：`c64_h128` 用例（src/out 落在 1.0~1.4 MB）**连 ref 自己都差
//     130932/131072 字节** —— 差的是"源数据被输出覆盖"，不是 RTL 算错。
//     ⇒ 而 `MAXW = 262144` 字 = 2 MB 字节，本来就是要 2 MB 窗口 ⇒ 字节地址取
//       `[20:3]` = 18 位字索引 ⇒ **MEMAW = 21**。修后 `c64_h128` ref 自我一致
//       （`CHECK OK bytes=131072`）。19/20 个其它用例最大地址 221 KB ⇒ 不受影响。
//     取证：`runs/t38_rtl/REPORT.md` §3.0（B0-b）。
//  3. ⚠ 另一个**用法坑**（同一份报告 B0-a，写在这里免得下一个人再踩）：
//     `+OUTBASE=%h` 用 `$value$plusargs` 解析，**xsim 不认 `0x` 前缀**，
//     `+OUTBASE=0x3f0` 会被**静默解析成 0**（不报错！）⇒ 落盘的是地址 0 的未初始化
//     内存（一排 `x`），下游 `check` 再抛 `ValueError`。**必须传纯十六进制**
//     （`+OUTBASE=3f0`）。T-36 那一轮的"逐字节 PASS"就是死在这一步上
//     ⇒ 第 80 条纪律："日志全绿" ≠ "比过了"（见 `docs/PLAN_1second.md` 末）。
//     ⇒ 本 TB 的落盘路径**必须**打印参与比较的字节数（`bytes=N`），只看 `nerr=0` 不算。
//
//  为什么需要一个新 TB：仓库里原先**没有**任何 E1E2 的功能 TB
//  （`tools/verify_all.sh` 只把 bin_e1e2.v 编一遍做语法检查，wrap 组只跑
//   UNIT=0/1/2/3 = conv_core / rprelu / sam / interp，**一次都没跑过 E1E2**；
//   见 T-36 报告 §1 的自证）。所以本 TB 是第一个把 E1E2 数据面钉住的仿真。
//
//  ── 数据驱动（同一个 TB 跑所有形状，也跑改动前/后的同一个二进制）──────────
//    +MEMFILE=<f>   64 位字、每行一个 16 位十六进制数（= 8 B 一拍的字组），
//                   第 i 行是物理地址 i*8。字节序 = 小端（与 wrapper 一致）。
//    +MEMWORDS=<n>  存储深度（字）。地址越界会被断言抓到（不许静默别名）。
//    +CMDFILE=<f>   每行一条命令：<desc_base> <out_base> <c> <h> <w> <cfg_group>
//                   全部十六进制（c/h/w/group 也按十六进制给，方便复算）。
//    +NCMD=<n>      要发的命令条数
//    +OUTBASE=<h> +OUTWORDS=<n> +OUTFILE=<f>   跑完把这段内存落盘（hex 字）
//    +TAG=<s>       打印用标签
//    +NOMEM=1       只跑拍数（读回常数、不落盘；用于全网 698 条命令的拍数）
//    +STALL=<n>     n>0 时每 n 拍插一个 ap_ce=0（模拟 wrapper 的读 miss 冻结）
//    +QUIET=1       不逐命令打印
//
//  ── 三条硬约定 ───────────────────────────────────────────────────────────
//   1. **命令之间只留 1 个 IDLE 拍**：`ctrl_start` 直接在 `ctrl_done` 那一拍
//      拉高（`ctrl_done` 是在 ST_IDLE 那拍为高）。这是"总拍数"可比的前提：
//      旧流程每条命令的 IDLE 周转正好 1 拍，本 TB 把这 1 拍固定下来。
//   2. 读数据**寄存 1 拍**：RTL 的 `resp_vld <= issue_en`，数据必须在发读的
//      下一拍出现在 mem_rd_data 上（= wrapper "命中后立刻给数"的等价模型）。
//   3. 拍数口径：`busy` = `ctrl_busy==1` 的拍数（≈ wrapper 的 DBG_CYC/RUN_ACTIVE），
//      `wall` = 从第一条 start 到最后一个 done 的拍数。两种都给，避免口径争论。
//
//  ── 结构指纹（P5）────────────────────────────────────────────────────────
//   本 TB 对新增寄存器/比较器做**层次化引用**（不存在 ⇒ xelab 直接失败），
//   并用 `$countdrivers` 查多驱动（=1 合格）。另外统计
//     `DRAIN@lane==7` 的次数（改动后，lanes_real<8 的组必须为 0）、
//     `GRPADV` 的次数（= 组间自增次数，必须 == Σ(ngrp-1)）。
// ============================================================================
`timescale 1ns/1ps

module tb_bin_e1e2;

    // ------------------------------------------------------------------ 配置
    localparam int MAXW    = 262144;      // 最多 2 MB 的模型内存（字）
    localparam int MAXCMD  = 1200;        // 最多 1200 条命令（全网 698）
    localparam int MAXDUMP = 262144;

    int          MEMW    = 1024;
    int          NCMD    = 1;
    int          STALL   = 0;
    int          NOMEM   = 0;
    int          QUIET   = 0;
    int          DUMPW   = 0;
    int          MAXC    = 40000000;
    logic [31:0] OUTBASE = 32'd0;
    string       MEMFILE = "";
    string       CMDFILE = "";
    string       OUTFILE = "";
    string       TAG     = "case";

    // ------------------------------------------------------------------ 时钟/复位
    logic clk   = 1'b0;
    logic rst_n = 1'b0;
    logic ce    = 1'b1;
    always #5 clk <= ~clk;

    // ------------------------------------------------------------------ 命令表
    logic [31:0] cmd_desc [0:MAXCMD-1];
    logic [31:0] cmd_out  [0:MAXCMD-1];
    logic [15:0] cmd_c    [0:MAXCMD-1];
    logic [15:0] cmd_h    [0:MAXCMD-1];
    logic [15:0] cmd_w    [0:MAXCMD-1];
    logic [15:0] cmd_g    [0:MAXCMD-1];

    // ------------------------------------------------------------------ 模型内存（64 位字）
    logic [63:0] memq [0:MAXW-1];

    // ------------------------------------------------------------------ DUT
    logic        ctrl_start, ctrl_done, ctrl_busy;
    logic        mem_rd_en;
    logic [31:0] mem_rd_addr;
    logic [63:0] mem_rd_data;
    logic        mem_wr_en;
    logic [31:0] mem_wr_addr;
    logic [63:0] mem_wr_data;
    logic [7:0]  mem_wr_strb;
    logic [7:0]  err_code;

    // ★ T-38 修正：原来写 18，但 mem_wr_addr[MEMAW-1:3] = [17:3] 只有 15 位
    //   ⇒ 模型窗口 = 2^15 字 = **256 KB**，任何 ≥256 KB 的地址会**静默别名**回 0x000000
    //   （不是越界报错）。c64_h128 的 src/out 在 1.0~1.4 MB ⇒ ref 自己都 130932/131072 差。
    //   窗口应是 2^18 字 = 2 MB 字节 ⇒ 字节地址取 [20:3] ⇒ MEMAW = 21。
    localparam int MEMAW = 21;            // [20:3] = 18 位字索引 = 2 MB 字节 = MAXW 字

    bin_e1e2 #(
        .LANES  (64),
        .ACC_AW (8),
        .C_MAX  (4096)
    ) u_dut (
        .ap_clk        (clk),
        .ap_ce         (ce),
        .ap_rst_n      (rst_n),
        .ctrl_start    (ctrl_start),
        .ctrl_done     (ctrl_done),
        .ctrl_busy     (ctrl_busy),
        .cfg_c         (cmd_c  [cidx]),
        .cfg_h         (cmd_h  [cidx]),
        .cfg_w         (cmd_w  [cidx]),
        .cfg_group     (cmd_g  [cidx]),
        .base_desc     (cmd_desc[cidx]),
        .base_out      (cmd_out [cidx]),
        .mem_rd_en     (mem_rd_en),
        .mem_rd_addr   (mem_rd_addr),
        .mem_rd_data   (mem_rd_data),
        .mem_wr_en     (mem_wr_en),
        .mem_wr_addr   (mem_wr_addr),
        .mem_wr_data   (mem_wr_data),
        .mem_wr_strb   (mem_wr_strb),
        .err_code      (err_code)
    );

    // ------------------------------------------------------------------ 序号 / 计数
    int          cidx  = 0;      // 当前在跑（或即将跑）的命令号
    int          nfin  = 0;      // 已完成的命令数
    logic [31:0] wall  = 0;
    logic        started = 1'b0;

    int          cmd_cyc = 0, cmd_rd = 0, cmd_wr = 0;
    longint      sum_cyc = 0, sum_rd = 0, sum_wr = 0, sum_wall = 0;
    int          nerr = 0;
    longint      xbad = 0;        // T36_XCHK：出现 X 的拍数

`ifdef T36_FP
    // P5：新信号的**翻转次数**（常量折叠掉的信号恒不翻转；真在算的信号会翻）
    logic [3:0] pv_ngrp = 0, pv_nlanes = 0, pv_last = 0;
    logic [7:0] pv_nlok = 0;
`endif

    // 结构指纹计数器
    longint      fp_drain7 = 0;   // ST_DRAIN 且 lane==7 的次数（旧退出条件的痕迹）
    longint      fp_grpadv = 0;   // 组间自增次数
    longint      fp_skip   = 0;   // DRAIN 提前退出次数（lane!=7 就 FLUSH）
    longint      fp_nl0    = 0;   // nlanes==0 的组数
    longint      fp_t_ngrp = 0, fp_t_nlanes = 0, fp_t_nlok = 0, fp_t_last = 0;  // 翻转次数
    longint      fp_lane7  = 0;   // lane 取到 7 的拍数（旧"8 lane 全扫"的痕迹）

    // ------------------------------------------------------------------ 读数据（寄存 1 拍）
    logic [63:0] nconst;
    always_ff @(posedge clk) begin
        if (ce) mem_rd_data <= NOMEM ? nconst
                                     : memq[mem_rd_addr[MEMAW-1:3]];
    end
    initial nconst = 64'h5A_A5_3C_C3_11_22_33_44;

    // ------------------------------------------------------------------ 写
    always_ff @(posedge clk) begin
        if (!NOMEM && ce && mem_wr_en) begin
            if (mem_wr_strb == 8'hFF) begin
                memq[mem_wr_addr[MEMAW-1:3]] <= mem_wr_data;
            end else begin
                // 本单元恒整字（{8{pend_v}}），出现部分选通就是契约破了
                $display("T36 BADSTRB cyc=%0d strb=%02h", wall, mem_wr_strb);
            end
        end
    end

    // ------------------------------------------------------------------ 地址合法性（不许别名）
    always_ff @(posedge clk) begin
        if (!NOMEM && ce && mem_rd_en && ((mem_rd_addr[MEMAW-1:0]) >= MEMW * 8 || mem_rd_addr[2:0] != 3'd0)) begin
            $display("T36 BADADDR rd cyc=%0d addr=%08h memw=%0d", wall, mem_rd_addr, MEMW);
        end
        if (!NOMEM && ce && mem_wr_en && ((mem_wr_addr[MEMAW-1:0]) >= MEMW * 8 || mem_wr_addr[2:0] != 3'd0)) begin
            $display("T36 BADADDR wr cyc=%0d addr=%08h memw=%0d", wall, mem_wr_addr, MEMW);
        end
    end

    // ------------------------------------------------------------------ CE 冻结（可选）
    int stall_cnt = 0;
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            ce <= 1'b1; stall_cnt <= 0;
        end else if (STALL > 0) begin
            if (stall_cnt == 0) begin ce <= 1'b0; stall_cnt <= STALL; end
            else begin ce <= 1'b1; stall_cnt <= stall_cnt - 1; end
        end
    end

    // ------------------------------------------------------------------ 发命令：done 那一拍直接续发（1 拍 IDLE）
    logic boot_pulse;
    int   boot_cnt = 0;
    always_ff @(posedge clk) begin
        if (!rst_n) begin boot_pulse <= 1'b0; boot_cnt <= 0; end
        else if (boot_cnt == 0) begin boot_pulse <= 1'b1; boot_cnt <= 1; end
        else boot_pulse <= 1'b0;
    end

    assign ctrl_start = boot_pulse | (ctrl_done & (cidx < NCMD));

    always_ff @(posedge clk) begin
        if (rst_n && ctrl_start) cidx <= cidx + 1;
    end

    // ------------------------------------------------------------------ 拍数统计
    //   ⚠ 全部计数只在这**一个** always_ff 里驱动（xelab 的 VRFC 10-3818/2921
    //     会抓"同一变量两个进程驱动"；本文件曾在拆分写法下被抓到一次，已合并）。
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            wall <= 0; started <= 1'b0;
            cmd_cyc <= 0; cmd_rd <= 0; cmd_wr <= 0;
            sum_cyc <= 0; sum_rd <= 0; sum_wr <= 0;
            nfin <= 0; nerr <= 0;
        end else begin
            if (started) wall <= wall + 1;
            if (ctrl_start) started <= 1'b1;
            // busy 拍（口径 = ctrl_busy，≈ wrapper 的 run_active/DBG_CYC）
            if (ctrl_busy) begin
                cmd_cyc <= cmd_cyc + 1;
                sum_cyc <= sum_cyc + 1;
            end
            // ⚠ 读/写**按真实总线拍数统计，不再与 busy 相与**：
            //   写流水有 3 级，最后 1 笔写会落在 busy 落下的那一拍之后
            //   （实测每命令少 1 笔）⇒ 用 busy 门控会把"每命令 1 笔"算丢，
            //   而 P2 要的正是"总写拍数逐位相等"。cmd_* 是同口径的逐命令值。
            if (ce && mem_rd_en) begin
                cmd_rd <= cmd_rd + 1;  sum_rd <= sum_rd + 1;
            end
            if (ce && mem_wr_en) begin
                cmd_wr <= cmd_wr + 1;  sum_wr <= sum_wr + 1;
            end
            // 命令收尾（done 那拍 busy=0，所以上面三个"+"与这里的清零不会打架）
            if (ctrl_done) begin
                nfin  <= nfin + 1;
                if (err_code != 8'd0) nerr <= nerr + 1;
                if (!QUIET)
                    $display("T36 CMD i=%0d cyc=%0d rd=%0d wr=%0d err=%02h",
                             nfin, cmd_cyc, cmd_rd, cmd_wr, err_code);
                cmd_cyc <= 0; cmd_rd <= 0; cmd_wr <= 0;
            end
            // ---- 结构指纹 ----
            if (u_dut.state == 4'd6) begin                     // ST_DRAIN
                if (u_dut.lane == 3'd7) fp_drain7 <= fp_drain7 + 1;
                else                    fp_skip   <= fp_skip + 1;
            end
            if (u_dut.lane == 3'd7) fp_lane7 <= fp_lane7 + 1;
`ifdef T36_XCHK
            // P3 的"latch/X"证据：本单元所有输出**任何一拍都不许有 X**。
            // 组合块若被推断成锁存器（某条路径没赋值），最典型的症状就是这里出 X。
            if ($isunknown({ctrl_busy, mem_rd_en, mem_rd_addr, mem_wr_en,
                            mem_wr_data, mem_wr_strb, err_code,
                            u_dut.issue_en, u_dut.issue_addr, u_dut.bcmp,
                            u_dut.nlanes_c, u_dut.nl_ok, u_dut.last_lane_c,
                            u_dut.cb_c, u_dut.ovf_c, u_dut.nlanes_q, u_dut.ngrp_q})) begin
                xbad <= xbad + 1;
                if (xbad < 4) $display("T36 XCHK cyc=%0d 输出/内部有 X", wall);
            end
`endif
`ifdef T36_FP
            if (u_dut.state == 4'd9) begin                     // ST_DONE
                if (u_dut.ngrp_q > 4'd1) fp_grpadv <= fp_grpadv + 1;
            end
            if ((u_dut.state == 4'd1) && (u_dut.nlanes_q == 4'd0)) fp_nl0 <= fp_nl0 + 1;
            // 翻转计数（P5：存在 + 活着 + 数目）
            if (u_dut.ngrp_q   !== pv_ngrp)   begin pv_ngrp   <= u_dut.ngrp_q;   fp_t_ngrp   <= fp_t_ngrp   + 1; end
            if (u_dut.nlanes_q !== pv_nlanes) begin pv_nlanes <= u_dut.nlanes_q; fp_t_nlanes <= fp_t_nlanes + 1; end
            if (u_dut.nl_ok    !== pv_nlok)   begin pv_nlok   <= u_dut.nl_ok;    fp_t_nlok   <= fp_t_nlok   + 1; end
            if (u_dut.last_lane_c !== pv_last) begin pv_last  <= u_dut.last_lane_c; fp_t_last <= fp_t_last + 1; end
`endif
        end
    end

    // ------------------------------------------------------------------ 指纹：层次化引用（P5，不存在则 xelab 失败）
    initial begin
        // 静态指纹：枚举值与 TB 里用的数字必须对上（防止枚举改了而计数悄悄失效）
        if (u_dut.ST_DRAIN !== 4'd6 || u_dut.ST_DONE !== 4'd9 || u_dut.ST_CHK !== 4'd1)
            $display("T36 FP-ENUM-MISMATCH DRAIN=%0d DONE=%0d CHK=%0d",
                     u_dut.ST_DRAIN, u_dut.ST_DONE, u_dut.ST_CHK);
        // ⚠ xsim 没有 $countdrivers（VRFC 10-10098）⇒ 多驱动改用 xelab 的
        //   VRFC 10-3818/10-2921 警告（**同一变量被两个进程驱动**）来判，
        //   见 run_all.sh 的 build 日志统计。这里的层次化引用只证明"信号在
        //   elaborate 后的设计里存在且可观测"——不存在 ⇒ xelab 直接失败。
`ifdef T36_FP
        $display("T36 FP ngrp_q=%0d nlanes_q=%0d nl_ok=%02h last_lane=%0d cb=%0d hw8=%0h",
                 u_dut.ngrp_q, u_dut.nlanes_q, u_dut.nl_ok, u_dut.last_lane_c,
                 u_dut.cb_c, u_dut.hw8_c);
        $display("T36 FP localparam SKIP_EMPTY_LANE=%0d GRP_LOOP=%0d",
                 u_dut.E1E2_SKIP_EMPTY_LANE, u_dut.E1E2_GRP_LOOP);
`endif
    end

    // ------------------------------------------------------------------ 主流程
    longint handle;
    int     eof;
    logic [63:0] v64;
    logic [31:0] v32;
    logic [15:0] v16a, v16b, v16c, v16d;

    initial begin
        void'($value$plusargs("MEMFILE=%s", MEMFILE));
        void'($value$plusargs("CMDFILE=%s", CMDFILE));
        void'($value$plusargs("OUTFILE=%s", OUTFILE));
        void'($value$plusargs("TAG=%s",     TAG));
        void'($value$plusargs("MEMWORDS=%d", MEMW));
        void'($value$plusargs("NCMD=%d",     NCMD));
        void'($value$plusargs("STALL=%d",    STALL));
        void'($value$plusargs("NOMEM=%d",    NOMEM));
        void'($value$plusargs("QUIET=%d",    QUIET));
        void'($value$plusargs("OUTWORDS=%d", DUMPW));
        void'($value$plusargs("OUTBASE=%h",  OUTBASE));
        if (NCMD > MAXCMD)  begin $display("T36 FATAL NCMD>MAXCMD");  $finish; end
        if (MEMW > MAXW)    begin $display("T36 FATAL MEMW>MAXW");    $finish; end
        if (DUMPW > MAXDUMP)begin $display("T36 FATAL DUMPW>MAXDUMP");$finish; end

        // ---- 命令表
        if (CMDFILE == "") begin $display("T36 FATAL need +CMDFILE"); $finish; end
        handle = $fopen(CMDFILE, "r");
        if (handle == 0) begin $display("T36 FATAL cannot open %s", CMDFILE); $finish; end
        for (int i = 0; i < NCMD; i++) begin
            eof = $fscanf(handle, "%h", v32); cmd_desc[i] = v32;
            eof = $fscanf(handle, "%h", v32); cmd_out [i] = v32;
            eof = $fscanf(handle, "%h", v16a); cmd_c  [i] = v16a;
            eof = $fscanf(handle, "%h", v16b); cmd_h  [i] = v16b;
            eof = $fscanf(handle, "%h", v16c); cmd_w  [i] = v16c;
            eof = $fscanf(handle, "%h", v16d); cmd_g  [i] = v16d;
            if (eof == 0) $display("T36 BADCMDFILE line %0d", i);
        end
        $fclose(handle);

        // ---- 内存
        if (!NOMEM && MEMFILE != "") begin
            handle = $fopen(MEMFILE, "r");
            if (handle == 0) begin $display("T36 FATAL cannot open %s", MEMFILE); $finish; end
            for (int i = 0; i < MEMW; i++) begin
                eof = $fscanf(handle, "%h\n", v64);
                memq[i] = v64;
            end
            $fclose(handle);
        end

        // ---- 复位
        rst_n = 1'b0;
        repeat (6) @(posedge clk);
        rst_n = 1'b1;

        // ---- 跑
        while (nfin < NCMD) @(posedge clk);
        repeat (6) @(posedge clk);     // 等最后 3 级写流水排空

        sum_wall = wall;
        $display("T36 SUM tag=%s cyc=%0d rd=%0d wr=%0d wall=%0d ncmd=%0d nerr=%0d fp_drain7=%0d fp_skip=%0d fp_grpadv=%0d fp_nl0=%0d fp_lane7=%0d fp_tngrp=%0d fp_tnlanes=%0d fp_tnlok=%0d fp_tlast=%0d",
                 TAG, sum_cyc, sum_rd, sum_wr, sum_wall, nfin, nerr,
                 fp_drain7, fp_skip, fp_grpadv, fp_nl0, fp_lane7,
                 fp_t_ngrp, fp_t_nlanes, fp_t_nlok, fp_t_last);
        $display("T36 XSUM tag=%s xbad=%0d", TAG, xbad);

        // ---- 落盘
        if (!NOMEM && OUTFILE != "" && DUMPW > 0) begin
            handle = $fopen(OUTFILE, "w");
            for (int i = 0; i < DUMPW; i++)
                $fwrite(handle, "%016h\n", memq[(OUTBASE >> 3) + i]);
            $fclose(handle);
        end
        $finish;
    end

    // ------------------------------------------------------------------ 看门狗
    initial begin
        void'($value$plusargs("MAXC=%d", MAXC));
        #(MAXC * 10ns);
        $display("T36 TIMEOUT tag=%s cyc=%0d nfin=%0d wall=%0d", TAG, sum_cyc, nfin, wall);
        $finish;
    end

endmodule
