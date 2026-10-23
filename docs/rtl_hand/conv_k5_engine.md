# 手写 k=5 卷积引擎 `conv_k5_engine`

> 目标层：`encoder.conv_first`（320×320，cin=12→物理 16，cout=48，k=5，pad=2，stride=1，ReLU）
> 目标：3.39 s → ≤0.15 s；DSP ≤140、BRAM ≤40、LUT ≤12,000、Fmax ≥150 MHz。
> 本文件记录结构、拍数推导、资源估算、验证状态和已知限制。
> 代码：
> - RTL：`pynq_specialized_code/pl/rtl_hand/modules/conv_k5_engine.v`
> - TB：`pynq_specialized_code/pl/rtl_hand/tb/tb_conv_k5_engine.v`（接统一验收设施）
> - 参考模型：`pynq_specialized_code/pl/rtl_hand/tools/gen_k5_data.py`

---

## 0. 结论速览

| 项 | 结果 | 状态 |
|---|---|---|
| 结构 | 25 抽头 × 4 oc = **100 DSP**，II=1，5×5 窗口移位寄存器 | 已实现 |
| 计算拍数（320×320 整层） | **14,745,600 拍** @150 MHz = **98.3 ms** | 推导 |
| 含装载/填充/写回（整层） | **≈15.64 M 拍 ≈ 104 ms** | 推导 |
| 相对 3.39 s | **≈32.6×** | 推导 |
| 相对 0.15 s 目标 | **0.69×**（余量 30%） | 推导 |
| 资源（估计） | DSP 100 / BRAM18 ≈22 / LUT ≈7 k / FF ≈15 k | **估计，未综合** |
| `xvlog` 语法 | 0 error / 0 warning | 已跑 |
| 参考模型 vs HLS cosim RTL | 30,720/30,720 字节一致 | 已跑 |
| RTL 功能 golden / fuzz / 实测拍数 | 待 `run_module.sh` 回填 | **未跑（按父 agent 要求）** |

> 关键提醒：本文件把「已实测」「已推导」「待实测」分开写，不把推导当实测。

---

## 1. 接口（统一验收契约）

顶层端口已按 `rtl_hand` 统一契约命名（不是任务书初稿的单 AXI master）：

| 组 | 端口 | 说明 |
|---|---|---|
| 时钟/复位 | `ap_clk`, `ap_rst_n` | 低有效异步复位 |
| 读口 0 | `m_axi_in_ar*(arvalid/arready/araddr/arlen)`, `m_axi_in_r*(rvalid/rready/rdata/rlast)` | 输入张量（C8） |
| 读口 1 | `m_axi_w_ar*`, `m_axi_w_r*` | 权重（PAD28 容器） |
| 读口 2 | `m_axi_b_ar*`, `m_axi_b_r*` | bias（int32） |
| 写口 | `m_axi_out_aw*`, `m_axi_out_w*`, `m_axi_out_b*` | 输出（C8） |
| 控制 | `ctrl_in_addr`, `ctrl_wgt_addr`, `ctrl_bias_addr`, `ctrl_out_addr`, `ctrl_shift`, `ctrl_cin`, `ctrl_cout`, `ctrl_h`, `ctrl_w`, `ctrl_start`, `ctrl_done`, `ctrl_busy` | 字节地址；`ctrl_start` 单拍脉冲；`ctrl_done` 单拍脉冲 |

AXI 约定：
- 64-bit 数据宽，`ARSIZE/AWSIZE=3`，`ARBURST/AWBURST=INCR`（由内部固定；契约端口不引出 size/burst）。
- 读请求内部用「字节地址 + 字节长度」，长度必须 8 的倍数；读引擎自己拆 ≤256 beat 的 burst。
- 写请求内部用「字节地址 + 64-bit 字数」，写引擎拆 ≤256 beat 的 burst，`WLAST` 在最后一个 beat。
- 三个读口同一时刻只有一个在飞（`rd_sel` 按主 FSM 状态选择），因此不需要读口仲裁器。
- `ctrl_cin/cout/h/w` 是契约占位：本模块用编译期参数 `CIN/COUT/H/W` 生成硬件，运行期值不参与寻址；换形状请重新例化参数。

---

## 2. 结构图

```
                    ┌──────────────────────────────────────────────────────┐
  m_axi_in  ───────►│ 读引擎（1 个，AR/R 三选一 mux）                       │
  m_axi_w   ───────►│  · 权重 16128 B（2016 beat）                         │
  m_axi_b   ───────►│  · bias 192 B（24 beat）                             │
                    │  · 输入行 2×W beat（每个 8 通道块一行）               │
                    └───────────────┬──────────────────────────────────────┘
                                    │ 64-bit beat
                    ┌───────────────▼──────────────────────────────────────┐
                    │ 5 条行缓冲（每条 2 个 64-bit bank，324 深）            │
                    │   物理列 0..1 = 左 pad；2..W+1 = 真实；W+2..W+3 = 右 pad│
                    │   每拍只读 1 个地址 → 一列 5 行同时吐出               │
                    └───────────────┬──────────────────────────────────────┘
                                    │ 5×128 bit（5 行 × 16 通道）
                    ┌───────────────▼──────────────────────────────────────┐
                    │ 5×5 窗口移位寄存器（25 × 128 bit = 3200 FF）          │
                    │   每输出一个 x 只移入 1 列（5 行）                    │
                    └───────────────┬──────────────────────────────────────┘
                                    │ 25 个像素字节（当前 ic）
      ┌─────────────────────────────┼─────────────────────────────┐
      │ 权重 100 bank（分布式 RAM）  │                             │
      │ 每 (oc_lane,tap) 一个 192×8  │                             │
      │ 100 个组合读口                │                             │
      └──────────────┬──────────────┴──────────────┬──────────────┘
                     │ 100 个权重字节                │
              ┌──────▼───────────────────────────────▼──────┐
              │ 25 tap × 4 oc = 100 个 DSP48E1 累加器        │
              │ 每个 (oc,tap) 在 12 个 ic 上 P += a*b       │
              │ 3 级流水：A/B 寄存器 → M → P                │
              └──────────────────┬──────────────────────────┘
                                 │ 每个 oc 的 25 个抽头和（12 拍后）
              ┌──────────────────▼──────────────────────────┐
              │ 4 路 25→1 加法树（5 级流水，26-bit）          │
              │ + bias（按 oc%4 分 4 拍读）                  │
              │ + >>shift + ReLU + clamp(0,127)              │
              └──────────────────┬──────────────────────────┘
                                 │ 4 字节（s=0）/ 4 字节（s=1）
              ┌──────────────────▼──────────────────────────┐
              │ 输出行缓冲 W×64 bit（一个 8-oc 块的一行）     │
              │ s=0 存低 4 字节，s=1 拼成 8 字节 → 写引擎     │
              └──────────────────┬──────────────────────────┘
                                 │ m_axi_out（AW/W/B，≤256 beat/burst）
```

### 2.1 为什么 II=1

HLS 版 `LOOP_892_47` 的 II=3 不是抽头串行，也不是权重口，而是**一次迭代要 5 个不同列地址、每 bank 只有 2 个读口**（`docs/rtl_map/06_k5_and_primitives.md` §1.5）。本设计把「一列 5 行」做成行缓冲的一次读地址：

- 行缓冲 bank = 行，地址 = 列；5 条行缓冲各 1 个读口 → 一拍读 5 个不同行、同一列。
- 5×5 窗口用 25 个 128-bit 寄存器，每输出一个 x 只移入新 1 列（5 行）。
- 因此每拍都能推进 1 个 `(pixel, ic)`；4-oc 组内 12 个 ic 连续跑，组间无气泡。

### 2.2 循环顺序

```
for y = 0..H-1:                       # 输出行
    fill row y+2                      # 一次读 2 个 8 通道块 × W beat
    for b = 0..5:                     # 8-oc 输出块
        preload 5 列窗口
        for x = 0..W-1:
            for s = 0,1:              # 4-oc 子组
                for ic = 0..CIN-1:    # 12 拍
                    25 tap × 4 oc MAC
        drain / flush
        write out_buf（W beat，一个 C8 块的一行）
```

每个 8-oc 块写一次连续 burst，输出地址 `out_base + b*H*W*8 + y*W*8 + x*8`，天然连续。

---

## 3. 数据格式

### 3.1 输入 C8 交错

```
addr(c,y,x) = floor(c/8)*H*W*8 + y*W*8 + x*8 + (c%8)
```

- `cin=12` → 物理 `CPAD=16`，两个 8 通道块；`c=12..15` 恒 0，引擎只读 `ic=0..11`。
- 一个像素的 8 个通道是连续 8 字节，行缓冲一次读 64 bit。

### 3.2 权重 PAD28

```
wgt[(oc*CIN + ic)*28 + ky*5 + kx]     # 前 25 字节 = 5×5 抽头
                                       # 后 3 字节 = 零填充，引擎不读
```

### 3.3 输出

```
out = clamp((sum + bias) >> shift, 0, 127)   # ReLU 固定开
addr(oc,y,x) = floor(oc/8)*Hout*Wout*8 + y*Wout*8 + x*8 + (oc%8)
```

`cout=48` 已是 8 的倍数；输出按 8-oc 块写 64-bit beat，低 4 字节 = oc%8 的 0..3，高 4 字节 = 4..7。

---

## 4. 拍数推导（II=1）

### 4.1 计算

```
每像素每 4-oc 组 = CIN 拍 = 12
每像素 = 12 组 × 12 拍 = 144
每行 = W × 144 = 320 × 144 = 46,080
整层 = H × 46,080 = 320 × 46,080 = 14,745,600 拍
@150 MHz = 98.30 ms
```

### 4.2 额外开销（顺序执行，不重叠）

| 项 | 拍数/行 | 说明 |
|---|---:|---|
| 行填充 | ≈690 | 2 块 × 320 beat + burst 开销 |
| 6 块 × (BLOCK_PRE 1 + PRELOAD 6 + DRAIN 3 + FLUSH 10 + WR_REQ 1) | ≈126 | 每块约 21 拍 |
| 6 块 × 写回 W beat | 1,920 | 每块 320 beat + AW/B |
| 合计 | ≈2,736 | |

```
整层 ≈ 320 × (46,080 + 2,736) = 15,621,120 拍 ≈ 104.1 ms
```

> 纯计算 14,745,600 拍 = 98.3 ms；含外围约 15.62 M 拍 = 104.1 ms ⇒ **1.06×**。
> 目标 0.15 s = 22.5 M 拍，余量约 30%。

### 4.3 2×320 条带（现成 golden `k5_12x48`）

```
计算 = 2 × 46,080 = 92,160 拍
权重装载 = 16,128 拍（1 字节/拍）
bias = 48 拍
行填充/写回/预载 ≈ 5,500 拍
合计 ≈ 113,800 拍
```

- 对 2×320 的纯计算目标 92,160 拍，比值 ≈1.235；**这是测试条带的固定开销**（16 k 权重装载占 14%），不是整层吞吐。
- 对 HLS cosim 基线 2,106,401 拍（`tools/targets.tsv`），本设计约 **18.5×** 更快。
- 整层吞吐验收应以 320×320 为目标；条带用于功能比对。

### 4.4 与 HLS 版对照

| 项 | HLS `LOOP_892_47` | 本设计 |
|---|---:|---:|
| II | 3 | **1** |
| 每像素 oc 串行 | 48 次调用 | 6 块 × 2 组，流水连续 |
| 单迭代 MAC/拍 | 8.33 | **100** |
| 权重来源 | 每 (pixel,oc) 读 300 B | 整层预装，内层 0 次 DDR |
| 行缓冲列口瓶颈 | 5 列/2 口 ⇒ II=3 | 一列 5 行/1 地址 ⇒ II=1 |
| 整层时间 | 3.39 s | **≈104 ms（推导）** |

---

## 5. 资源估算（未综合）

> 任务要求不综合；以下是按 RTL 结构 + Xilinx 7 系列原语容量做的**估算**，不是 Vivado 报告。真实数字以父 agent 的 csynth/impl 为准。

| 资源 | 估计 | 依据 |
|---|---:|---|
| DSP48E1 | **100** | 25 tap × 4 oc；`(* use_dsp="yes" *)` 3 级流水 |
| BRAM18 | **≈22** | 行缓冲 5×2 bank×2 = 20；输出行缓冲 2 |
| LUT（含分布式 RAM） | **≈7,000** | 权重 LUTRAM 2,400；窗口字节 mux ≈800；加法树 ≈1,300；控制/AXI ≈2,000；杂项 ≈500 |
| FF | **≈15,000** | 窗口 3,200；p_acc 4,800；加法树 5,408；cap/bias/流水 ≈1,500 |
| Fmax | **估计 ≥150 MHz** | DSP 3 级流水；加法树每级 1 个 26-bit 加法；重量读 192:1 mux 与像素 mux 并联进 DSP A/B 寄存器 |

### 5.1 关键时序路径

1. **权重读**：192:1 分布式 RAM 组合读（约 3 级 LUT6）+ DSP 的 B 寄存器。估计 <2 ns。
2. **像素读**：`win[r][c][ic*8 +: 8]` 的 16:1 mux + DSP A 寄存器。估计 <1.5 ns。
3. **DSP**：8×8 乘 + 48-bit 累加，DSP48E1 内部 3 级。估计 2~3 ns。
4. **加法树**：每级 26-bit 加法 + 寄存器。估计 1 ns/级。
5. **量化**：32-bit 加 + 变量右移 + clamp。估计 2 ns。

> **风险**：权重 192:1 组合读在 150 MHz 下若收敛困难，可改回同步读，并在像素侧加 1 级对齐寄存器（多 1 拍延迟，II 仍为 1）。这是首选的回退方案。

### 5.2 为什么不用 BRAM 存权重

- 整层权重 21.5 KB；如果用 BRAM 且内层每拍要 100 个权重字节，需要 100 个读口，BRAM 做不到。
- 用 100 个 192×8 分布式 RAM（每个 (oc_lane,tap) 一个，地址 = grp*16+ic）可以一拍并行读 100 字节，代价是约 2,400 LUT6。
- BRAM 预算留给行缓冲（20 块）和输出行缓冲（2 块），总 22/40。

---

## 6. 验证

### 6.1 已完成的检查

| 检查 | 结果 | 说明 |
|---|---|---|
| 参考模型 vs HLS cosim RTL | **PASS 30,720/30,720 字节一致** | `gen_k5_data.py` 的 `ref_conv` 与 `hls_k5_cosim/.../rtl.conv2d_engine.autotvout_gmem1.dat[8:8+30720]` 逐字节相同 |
| 现成 golden 一致性 | **md5 相同** | `golden/k5_12x48/expected_c8.bin` 与 `tb_c8_k5_12x48/out_golden.bin` md5 都是 `61d0476a…` |
| `xvlog --sv` | **0 error / 0 warning** | DUT + TB + `common/eq_checker.v`，按 `run_module.sh` 的宏 |
| xelab / xsim | **未跑** | 按父 agent 要求，统一由父 agent 跑 |

### 6.2 testbench

`tb/tb_conv_k5_engine.v` 是统一验收设施的最小模板：

```verilog
`define TB_MODULE_NAME "conv_k5_engine"
`include "tb_common_tasks.vh"
conv_k5_engine #(.CIN(`TB_CIN), .COUT(`TB_COUT), .W(`TB_W),
                 .H(`TB_H), .PAD(`TB_PAD)) dut ( ... );
```

共享头文件负责：
- 时钟/复位；
- 三路读 AXI 从机 + 一路写 AXI 从机；
- `load_bins` 读 `input_c8.bin` / `weight.bin` / `bias.bin` / `expected_c8.bin` / `shift.txt` / `meta.json`；
- `drive_start` 拉 `ctrl_start`、等 `ctrl_done`、统计拍数；
- `check_bytes` 逐字节比对；
- `report_all` 打印 `[TB-SUMMARY]`；
- `MAX_CYCLES` 单组看门狗 + 全局拍数/时间看门狗。

### 6.3 运行命令（父 agent）

```bash
cd pynq_specialized_code/pl/rtl_hand

# 功能 golden（2×320 条带）
bash tools/run_module.sh conv_k5_engine k5_12x48

# 200 组随机 fuzz（含全 0 / 全 127 / 全 -128 边界）
bash tools/run_module.sh conv_k5_engine k5_12x48 --fuzz 200

# 只做语法检查
bash tools/run_module.sh conv_k5_engine k5_12x48 --compile-only
```

整层 320×320 吞吐（可选）：用 `tools/make_golden.py` 生成一个 H=320、W=320 的 golden，然后：

```bash
bash tools/run_module.sh conv_k5_engine --golden <k5_320_dir> \
     --target 14745600 --max-cycles 20000000
```

> 注意：`--target 14745600` 是**整层 II=1 计算目标**；2×320 条带的纯计算目标只有 92,160，固定 16 k 权重装载会让比值到 1.23×，不要拿条带判整层吞吐。

### 6.4 待回填

| 项 | 期望 | 实际 |
|---|---|---|
| 2×320 golden 逐字节 | 30,720/30,720 | 待父 agent 跑 |
| 200 组 fuzz | 200/200 PASS | 待父 agent 跑 |
| 2×320 拍数 | ≈114 k | 待回填 |
| 320×320 整层拍数 | ≈15.6 M（1.06×） | 待回填 |
| 资源/Fmax | DSP 100 / BRAM 22 / LUT ≈7 k / Fmax ≥150 MHz | 待综合 |

---

## 7. 已知限制 / 明确没做到的地方

1. **未综合、未实现、未上板**。资源/Fmax 全是估算；`use_dsp` 能否真的映射成 100 个 DSP48E1，要 csynth 报告确认。
2. **RTL 功能仿真未由作者运行**。按父 agent 的规则，xsim 统一由父 agent 跑；本文不把「参考模型 PASS」当成「RTL PASS」。父 agent 的失败日志贴回后，按拍号/字节号修。
3. **输出写回与计算串行**。每块算完才写 out_buf（单缓冲），写回约 320 拍/块，占整层 ≈4%。双缓冲可省掉这 4%，但会多一块 W×64 的 BRAM。
4. **权重装载 1 字节/拍**。整层 16,128 拍可忽略；但 2×320 条带里它占 14%，导致条带对纯计算比值 1.23×。若以后要压条带，可改 8 字节/拍并行写 8 个 bank（映射跨 28 字节容器边界，逻辑会复杂一些）。
5. **权重读是组合读**。100 个 192:1 mux 的时序是估算；若 150 MHz 不收敛，回退到同步读 + 像素侧 1 级对齐寄存器（II 仍 1）。
6. **形状假设**：`CIN=12`（物理 16，两个 8 通道块）、`COUT` 是 8 的倍数、`PAD=2`、`stride=1`、`dilation=1`、ReLU 固定开。`ctrl_cin/cout/h/w` 目前只是契约占位，真正形状是编译期参数。
7. **`WGT_BYTES` 计数器 16-bit**：`COUT*CIN*28` 必须 < 65,536；48×12×28=16,128 没问题。换大层要加宽。
8. **2×320 条带 vs 320×320 整层**：现成 golden 是 H=2、W=320 的条带（30,720 输出），不是整层。整层吞吐是推导值；父 agent 若要实测，需要生成 H=320 的 golden。
9. **测试时钟 100 MHz**：共享头文件默认 100 MHz；拍数与时钟频率无关，150 MHz 下的时间 = 拍数/150 MHz。
10. **`ctrl_busy` 的语义**：`state != S_IDLE`；`ctrl_done` 是单拍脉冲。与共享设施的 `drive_start`（negedge 采样）对齐。

---

## 8. 与 HLS 版的差异清单（手写省掉了什么）

| HLS 结构 | 现在 | 本设计 |
|---|---|---|
| 3 态 PP FSM + 9 个 iter 使能 | `P892:L2862-L2891` | 1 个主 FSM + 嵌套计数器 |
| 20 个 7:1 行 mux | `P892:L828-L1523` | 0（行号编译期已知，5 条行缓冲直接读） |
| 4 层 ksize 旁路 mux | `P892:L2965-L2977` | 0（k=5 专用） |
| `acc_even/acc_odd` 奇偶分半 | `P892:L2895-L2903` | 1 个 48-bit 累加器/DSP |
| 8 个位宽变体 MAC + `sext` | 8 个 `mac_muladd_*` | 1 个 48-bit 宏（100 份） |
| 25 个权重 bank × 2816 深 | 50 BRAM18K | 100 个 192×8 分布式 RAM（≈2,400 LUT） |
| 每次 (pixel,oc) 握手 | ~63 拍/次 | 无调用开销，连续流水 |
| 行缓冲列口 II=3 | `ceil(5/2)` | II=1 |

---

## 9. 关键信号速查（读代码用）

| 信号 | 含义 |
|---|---|
| `state` | 主 FSM（IDLE → 权重 → bias → 预载 → 行循环 → 块循环 → 写回） |
| `rd_sel` | 三路读口选择：0=输入，1=权重，2=bias |
| `grp = {b_cnt,s_cnt}` | 0..11 的 4-oc 组号；权重地址 `grp*16+ic_cnt` |
| `win[r][c]` | 窗口第 r 行第 c 列（128-bit = 16 通道） |
| `sel[r]` | 窗口第 r 行 → 行缓冲号（随 y 左旋） |
| `p_acc[l][j]` | 第 l 个 oc、第 j 个 tap 的 48-bit 累加器（100 个） |
| `first_d2/last_d3` | 组边界/最后一个 ic 的流水对齐信号 |
| `capture` | 组结束，把 100 个 P 值拍进 26-bit 加法树 |
| `cap_d[5]` | 加法树 6 拍后的有效标志 |
| `q_byte[0:3]` | 4 个 oc 的量化结果 |
| `obuf` | 一个 8-oc 块的一行（W×64 bit） |
| `wr_req_*` / `wr_*` | 写引擎：字节地址 + 64-bit 字数 |

---

## 10. 复现/交接

```bash
# 1) 语法
source /data/xilinx/2025.2/2025.2.1/Vivado/settings64.sh
cd pynq_specialized_code/pl/rtl_hand
xvlog --sv --relax -i tb -i common \
      common/eq_checker.v modules/conv_k5_engine.v tb/tb_conv_k5_engine.v

# 2) 统一验收（父 agent）
bash tools/run_module.sh conv_k5_engine k5_12x48
bash tools/run_module.sh conv_k5_engine k5_12x48 --fuzz 200
```

> 本文的「待回填」项在父 agent 跑完后更新；如果 fuzz 失败，优先看字节号是否集中在某个 oc 块/某个 s 半区，那通常指向 `grp`/`s_cnt` 或输出拼包，而不是 MAC 本身。
