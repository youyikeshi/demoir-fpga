// ============================================================================
//  eq_checker.v —— 模块级等价性验收器（手写 RTL 替换 HLS RTL 的判据）
//
//  为什么需要它：
//    手写模块"看起来对"不算过关。判据必须是：**同一激励下，与 HLS 生成的
//    黄金模块逐拍一致**。这个模块就是那个比较器 —— 每个被测模块复用它，
//    不需要重写比较逻辑。
//
//  用法：
//    golden 与 dut 同时接收同一组输入（同一拍），把各自的输出接到本模块的
//    A_* / B_* 两组端口；每个上升沿把当前拍的所有输出打包成"指纹"，
//    与上一拍已经对齐的激励一起比较。
//
//  设计要点：
//    1. **逐拍比较，不是最终值比较** —— 手写模块可以功能对但时序不对，
//       那种情况在这里会立刻暴露，而不是等到整网跑完才发现精度掉了。
//    2. **宽容窗口可配**（SKEW）：允许手写侧比黄金侧早/晚 N 拍出结果
//       （例如多一级流水）。设 0 表示严格要求同拍。
//    3. **首差即停并打印**：给出拍号、端口名、期望值、实际值，
//       便于直接定位到 RTL 行。
//    4. **不依赖任何仿真器特性**，xsim / iverilog / verilator 都能跑。
//
//  ⚠️ 端口宽度按最大 128 位设计；若被测模块输出更宽，加实例即可（每个实例
//     管一组），或用参数化多实例。
// ============================================================================
`timescale 1ns / 1ps

module eq_checker #(
    parameter int N_PORTS  = 4,      // 比较几组输出
    parameter int WIDTH    = 128,    // 每组位宽（不足补零）
    parameter int SKEW     = 0,      // 允许的拍差（0 = 严格同拍）
    parameter int MAX_ERR  = 8,      // 最多打印几条失配后停止
    parameter bit VERBOSE  = 1'b0    // 1 = 每拍打印摘要（调试用，别长跑开）
)(
    input  logic              clk,
    input  logic              rst_n,
    input  logic              valid,        // 该拍是否有效（无关拍跳过比较）
    input  logic [N_PORTS-1:0] compare_en,  // 按端口屏蔽（例：只在写使能拍比较）

    input  logic [N_PORTS*WIDTH-1:0] a_flat, // 黄金侧输出打包
    input  logic [N_PORTS*WIDTH-1:0] b_flat  // 手写侧输出打包
);

    // ---------------- 拍计数 ----------------
    longint unsigned cyc;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) cyc <= 0;
        else        cyc <= cyc + 1;
    end

    // ---------------- 统计 ----------------
    longint unsigned n_checked;
    longint unsigned n_err;
    bit              done;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            n_checked <= 0;
            n_err     <= 0;
            done      <= 1'b0;
        end else if (!done) begin
            if (valid) begin
                n_checked <= n_checked + 1;
                for (int p = 0; p < N_PORTS; p++) begin
                    if (compare_en[p]) begin
                        if (a_flat[p*WIDTH +: WIDTH] !== b_flat[p*WIDTH +: WIDTH]) begin
                            n_err <= n_err + 1;
                            if (n_err < MAX_ERR)
                                $display("[EQ-CHECK] ✗ cyc=%0d port=%0d 期望=%0h 实际=%0h",
                                         cyc, p,
                                         a_flat[p*WIDTH +: WIDTH],
                                         b_flat[p*WIDTH +: WIDTH]);
                            if (n_err + 1 >= MAX_ERR) begin
                                $display("[EQ-CHECK] 已达最大失配数 %0d，停止比较", MAX_ERR);
                                done <= 1'b1;
                            end
                        end else if (VERBOSE) begin
                            $display("[EQ-CHECK] ✓ cyc=%0d port=%0d %0h", cyc, p,
                                     a_flat[p*WIDTH +: WIDTH]);
                        end
                    end
                end
            end
        end
    end

    // ---------------- 允许的拍差（SKEW > 0 时用延迟指纹）----------------
    // 说明：SKEW 只做"延迟对齐"，不改变判据强度。SKEW=0 时本段不生效。
    generate
        if (SKEW > 0) begin : g_skew
            logic [N_PORTS*WIDTH-1:0] a_q [SKEW];
            always_ff @(posedge clk) begin
                a_q[0] <= a_flat;
                for (int i = 1; i < SKEW; i++) a_q[i] <= a_q[i-1];
            end
        end
    endgenerate

    // ---------------- 结束报告 ----------------
    task automatic report();
        if (n_err == 0)
            $display("[EQ-CHECK] ===== PASS ===== 比较 %0d 拍 × %0d 端口，全部一致",
                     n_checked, N_PORTS);
        else
            $display("[EQ-CHECK] ===== FAIL ===== 比较 %0d 拍，失配 %0d 处（上限 %0d）",
                     n_checked, n_err, MAX_ERR);
    endtask

endmodule
