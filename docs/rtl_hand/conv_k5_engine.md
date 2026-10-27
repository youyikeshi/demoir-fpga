# 手写 k=5 卷积引擎 `conv_k5_engine`

> 目标层：`encoder.conv_first`（320×320，cin=12→物理 16，cout=48，k=5，pad=2，stride=1，ReLU）
> 现状：**默认 `OC_PER_PASS=1` → 25 DSP**，整层约 0.40 s（HLS 3.39 s 的 1/8.5）。
> 资源目标：DSP ≤40（整机 160 − HLS 92 − k1 33 = 35）、BRAM ≤40、LUT ≤12,000、Fmax ≥150 MHz。
> 历史基线：`OC_PER_PASS=4` 版本（100 DSP）已通过 30,720/30,720 黄金验收，实测 115,939 拍。
> 本文件记录结构、oc 并行度取舍、拍数推导、资源估算、验证状态和已知限制。
> 代码：
> - RTL（引擎本体）：`pynq_specialized_code/pl/rtl_hand/modules/conv_k5_engine.v`
> - RTL（AXI-Lite 包装层，见 §11）：`pynq_specialized_code/pl/rtl_hand/modules/k5_axi_wrap.v`
>   + `k5_cfg_check.v`（配置校验）
> - RTL（BD 适配层，Verilog-2001）：`pynq_specialized_code/pl/rtl_hand/modules/k5_axi_wrap_bd.v`
> - TB（引擎）：`pynq_specialized_code/pl/rtl_hand/tb/tb_conv_k5_engine.v`（接统一验收设施）
> - TB（包装层）：`pynq_specialized_code/pl/rtl_hand/tb/tb_k5_axi_wrap.v`
> - BD 集成脚本：`pynq_specialized_code/pl/scripts/bd_add_k5.tcl`
> - 参考模型：`pynq_specialized_code/pl/rtl_hand/tools/gen_k5_data.py`

---

## 0. 结论速览

### 0.1 当前默认（`OC_PER_PASS = 1`）

| 项 | 结果 | 状态 |
|---|---|---|
| 结构 | 25 抽头 × **1 oc = 25 DSP**，II=1，5×5 窗口移位寄存器 | 已实现 |
| 计算拍数（320×320 整层） | **58,982,400 拍** @150 MHz = **393 ms** | 推导 |
| 含装载/填充/写回（整层） | **≈59.9 M 拍 ≈ 0.40 s** | 推导 |
| 相对 HLS 3.39 s | **≈8.5×** | 推导 |
| 2×320 条带拍数（golden） | **≈392 k 拍**（旧 115,939 的 3.38×） | 推导，待实测 |
| 资源（**实测过一次，踩到大坑**） | 第一次综合：LUT **70,527** / FF **213,562** / BRAM **0** ❌（2D 数组没进 BRAM）；修复后预期 DSP 25 / BRAM≈10 / LUT≈8 k / FF≈9 k | 见 §5.6 |
| `xvlog --sv` | 0 error / 0 warning | 已跑 |
| `xelab` 参数展开 | 通过（DEPTH=768, OC_PER_PASS=1） | 已跑（未跑 xsim） |
| RTL 功能 golden / fuzz / 实测拍数 | 待父 agent 跑 `run_module.sh` | **未跑（按任务要求不跑仿真）** |

### 0.2 oc 并行度三档（详见 §5.5）

| `OC_PER_PASS` | DSP | 整层计算拍数 | 整层时间（含外围）@150 MHz | 相对 HLS 3.39 s | 结论 |
|---:|---:|---:|---:|---:|---|
| 4（旧默认） | 100 | 14,745,600 | 0.104 s | 32.6× | **超预算**（整机只剩 35） |
| 2 | 50 | 29,491,200 | 0.203 s | 16.7× | 仍超 40 DSP 上限 |
| **1（新默认）** | **25** | **58,982,400** | **0.399 s** | **8.5×** | **满足 ≤40，选用** |

> 关键提醒：本文件把「已实测」「已推导」「待实测」分开写，不把推导当实测。
> 旧版 115,939 拍是实测；新版拍数是由同一套 FSM 时序推导，父 agent 的 `run_module.sh` 会回填真实值。

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
      ┌──────────────────────────────────────────────────────┐
      │ 权重 NOC*25 个 bank（分布式 RAM）                     │
      │ 每 (oc_lane,tap) 一个 NGRP*16 深 × 8（1 oc: 768×8）   │
      │ NOC*25 个组合读口 → 每拍 NOC*25 个权重字节            │
      └──────────────────────────┬───────────────────────────┘
                                 │ NOC*25 个权重字节
              ┌──────────────────▼──────────────────────────┐
              │ 25 tap × NOC 个 oc = 25*NOC 个 DSP48E1      │
              │ 每个 (oc,tap) 在 12 个 ic 上 P += a*b       │
              │ 3 级流水：A/B 寄存器 → M → P                │
              └──────────────────┬──────────────────────────┘
                                 │ 每个 oc 的 25 个抽头和（12 拍后）
              ┌──────────────────▼──────────────────────────┐
              │ NOC 路 25→1 加法树（5 级流水，26-bit）        │
              │ + bias（每 pass 分 NOC 拍读）                │
              │ + >>shift + ReLU + clamp(0,127)              │
              └──────────────────┬──────────────────────────┘
                                 │ NOC 个字节/ pass（byte strobe）
              ┌──────────────────▼──────────────────────────┐
              │ 输出行缓冲 W×64 bit（一个 8-oc 块的一行）     │
              │ NG 个 pass 各写 NOC 字节，拼满 8 字节后写回   │
              └──────────────────┬──────────────────────────┘
                                 │ m_axi_out（AW/W/B，≤256 beat/burst）
```

> 默认 `NOC=1`：DSP 25、权重 bank 25 个、加法树 1 路、每 pass 输出 1 字节。
> `NOC=4` 时与旧版完全等价（100 DSP、100 bank、4 路树、每 pass 4 字节）。

### 2.1 为什么 II=1

HLS 版 `LOOP_892_47` 的 II=3 不是抽头串行，也不是权重口，而是**一次迭代要 5 个不同列地址、每 bank 只有 2 个读口**（`docs/rtl_map/06_k5_and_primitives.md` §1.5）。本设计把「一列 5 行」做成行缓冲的一次读地址：

- 行缓冲 bank = 行，地址 = 列；5 条行缓冲各 1 个读口 → 一拍读 5 个不同行、同一列。
- 5×5 窗口用 25 个 128-bit 寄存器，每输出一个 x 只移入新 1 列（5 行）。
- 因此每拍都能推进 1 个 `(pixel, ic)`；一个 pass 内 12 个 ic 连续跑。
- `OC_PER_PASS=1` 时同一个像素要跑 8 个 pass（s=0..7），**pass 与 pass 之间不换状态、不插气泡**，
  只是 `s_cnt` 加 1、`grp`（权重地址）换一下，所以 **II 仍是 1**；每像素 48 个 pass × 12 拍 = 576 拍。
- 窗口移位只在「一个像素的最后一个 pass」发生（`s_cnt == NG-1`），像素间仍只移 1 列。

### 2.2 循环顺序

```
for y = 0..H-1:                       # 输出行
    fill row y+2                      # 一次读 2 个 8 通道块 × W beat
    for b = 0..NBLK-1:                # 8-oc 输出块（NBLK = COUT/8 = 6）
        preload 5 列窗口
        for x = 0..W-1:
            for s = 0..NG-1:          # 块内 pass：NG = 8/OC_PER_PASS
                for ic = 0..CIN-1:    # 12 拍
                    25 tap × OC_PER_PASS oc MAC
            shift window（s 数满时，每像素一次）
        drain / flush
        write out_buf（W beat，一个 C8 块的一行）
```

> `OC_PER_PASS=1`：每像素 6 块 × 8 pass × 12 拍 = 576 拍；`OC_PER_PASS=4`：6 × 2 × 12 = 144 拍（旧版）。
>
> 命名说明：实现的 **pass 总数 = `NGRP = ceil(COUT/OC_PER_PASS)`**（1 oc 时 48）；
> 外层仍按 **8-oc 输出块 `NBLK = COUT/8`**（6 块）组织，因为写回必须凑满一个 64-bit beat。
> 因此 `for 块 { for pass }` 等价于「48 个 pass 的外层循环」，只是按 beat 分成了 6 组。

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

`cout=48` 已是 8 的倍数；输出按 8-oc 块写 64-bit beat，beat 内字节位置 = oc%8。
每个 pass 产出 `OC_PER_PASS` 个连续 oc，用 byte strobe 写进 beat 的对应字节位置；
`NG = 8/OC_PER_PASS` 个 pass 拼满 8 字节后，整行才写回（写回流量不变）。

---

## 4. 拍数推导（II=1，默认 `OC_PER_PASS=1`）

### 4.1 计算

一个 pass 覆盖 `OC_PER_PASS` 个 oc，在 `CIN` 个 ic 上流水（12 拍，II=1）。
每个 8-oc 输出块要拼满一个 64-bit beat，需要 `NG = 8/OC_PER_PASS` 个 pass。

```
通用：计算拍数 = H × W × (COUT/OC_PER_PASS) × CIN
NOC=1：320×320×48×12 = 58,982,400 拍 @150 MHz = 393.2 ms   <-- 默认
NOC=2：320×320×24×12 = 29,491,200 拍 @150 MHz = 196.6 ms
NOC=4：320×320×12×12 = 14,745,600 拍 @150 MHz =  98.3 ms   （旧版）
```

默认逐行展开：

```
每像素 = (COUT/1) 个 pass × 12 拍 = 48 × 12 = 576
每行   = W × 576 = 320 × 576 = 184,320
整层   = H × 184,320 = 320 × 184,320 = 58,982,400 拍
```

> **II 没有变差**：`for x { for s { for ic } }` 中 s 与 s 之间不换状态，
> 只更新 `s_cnt`/`grp`；每 12 拍仍连续发一次 capture。窗口移位只在 s 数满时做。

### 4.2 额外开销（顺序执行，不重叠）

| 项 | 拍数/行 | 说明 |
|---|---:|---|
| 行填充 | ≈690 | 2 块 × 320 beat + burst 开销 |
| 6 块 × (BLOCK_PRE 1 + PRELOAD 6 + DRAIN 3 + FLUSH 10 + WR_REQ 1) | ≈126 | 每块约 21 拍；**pass 数不增加此开销** |
| 6 块 × 写回 W beat | 1,920 | 每块 320 beat + AW/B |
| 合计 | ≈2,736 | |

```
整层 ≈ 320 × (184,320 + 2,736) + 权重装载 ≈ 18,144
     = 59,876,064 拍 ≈ 0.399 s @150 MHz
```

> 纯计算 393.2 ms；含外围 0.399 s ⇒ **1.015×**。旧版是 0.104 s，新版约 0.40 s。

### 4.3 2×320 条带（现成 golden `k5_12x48`）

```
计算     = 2 × 184,320 = 368,640 拍
权重装载 = 2,016 beat × 9 ≈ 18,144 拍（1 字节/拍，每 beat 1 拍气泡）
bias     = 24 beat × 3 ≈ 72 拍
行填充/写回/预载 ≈ 2 × 2,810 ≈ 5,620 拍
合计 ≈ 392,500 拍
```

- 旧版实测 **115,939 拍**；新版推导 **≈392.5 k 拍 = 3.39×**，比值 0.187
  （`tools/targets.tsv` 的 HLS cosim 基线 2,106,401 拍），满足 ≤0.22（≤464 k 拍）的验收线。
- **为什么不是 4×115,939 = 463,756**：只有计算部分随 pass 数 ×4；
  固定开销（权重装载 18.1 k、bias、写回、preload）几乎不变。
  所以真实值应落在 ~392 k，而不是 464 k。
- 对 2×320 的纯计算 368,640 拍，比值 ≈1.065；条带用于功能比对，整层吞吐按 320×320 推导。

### 4.4 与 HLS 版对照

| 项 | HLS `LOOP_892_47` | 本设计（`OC_PER_PASS=1`） |
|---|---:|---:|
| II | 3 | **1** |
| 每像素 oc 处理 | 48 次调用 | 6 块 × 8 pass，流水连续 |
| 单迭代 MAC/拍 | 8.33 | **25** |
| 权重来源 | 每 (pixel,oc) 读 300 B | 整层预装，内层 0 次 DDR |
| 行缓冲列口瓶颈 | 5 列/2 口 ⇒ II=3 | 一列 5 行/1 地址 ⇒ II=1 |
| 整层时间 | 3.39 s | **≈0.40 s（推导）** |

---

## 5. 资源估算（未综合）

> 任务要求不综合；以下是按 RTL 结构 + Xilinx 7 系列原语容量做的**估算**，不是 Vivado 报告。
> 真实数字以父 agent 的 csynth/impl 为准。

### 5.1 默认 `OC_PER_PASS=1` 的估算

| 资源 | 估计 | 依据 |
|---|---:|---|
| DSP48E1 | **25** | 25 tap × 1 oc；`(* use_dsp="yes" *)` 3 级流水 |
| BRAM | **≈10 块 BRAM36**（20 个 BRAM18 当量） | 行缓冲 10 个 324×64 各 1 块；输出行缓冲 1 块（W×64） |
| 分布式 RAM（LUTRAM） | **≈2,400 LUT6** | 25 bank × 768×8 = 153,600 bit；与旧版 100 × 192×8 **等量** |
| LUT 合计 | **≈6,000 ~ 6,500** | LUTRAM 2,400；窗口字节 mux ≈800；加法树 ≈700（1 路）；控制/AXI ≈2,000；杂项 ≈400 |
| FF | **≈6,500** | 窗口 3,200；a/b/m 流水 ≈800；加法树 ≈1,400；cap/bias/控制 ≈1,100；p_acc 1,200 在 DSP 内 |
| Fmax | **150 MHz 有风险，需综合确认** | 权重读从 192:1 变 768:1，组合 mux 约多 2 级 LUT6 |

对比旧版（100 DSP）：DSP 从 100 → 25（−75）；FF 从 ~15 k → ~6.5 k（−56%）；
LUT 与 BRAM 基本持平（LUTRAM 总量不变，加法树变小，mux 变深）。

### 5.2 关键时序路径

1. **权重读（主要风险）**：768:1 分布式 RAM 组合读。按 7 系列 LUT6 = 64×1 RAM 估算，
   768 深 = 12 个 RAM64 输出再 12:1 mux，约 4 级 LUT6（旧版 192 深约 3 级）。粗估 2.5~3 ns
   到 DSP 的 B 寄存器；150 MHz（6.67 ns）单看这一条有希望，但叠加布线后余量变薄。
2. **像素读**：`win[r][c][ic*8 +: 8]` 的 16:1 mux + DSP A 寄存器，约 1.5 ns。
3. **DSP**：8×8 乘 + 48-bit 累加，DSP48E1 内部 3 级，2~3 ns。
4. **加法树**：每级 1 个 26-bit 加法 + 寄存器，约 1 ns/级（5 级，每级有寄存器）。
5. **量化**：32-bit 加 + 变量右移 + clamp，约 2 ns。

> **权重读收敛不了时的回退顺序**（吞吐仍是 II=1，只多固定延迟）：
> 1. 权重改放 **BRAM**（见 §5.3：13 块 RAMB18 可装下 25 个 768×8 bank，BRAM 22→35），
>    同步读 + 像素侧 1 级对齐寄存器；II 仍 1，但 capture 元数据要多延迟 1 拍（改 `cap_d` 深度）；
> 2. 仍用 LUTRAM，但在 a_r/b_r 前各加 1 级流水寄存器（把 768:1 mux 的输出打一拍），
>    同样只加 1 拍固定延迟；代价是 +800 FF 左右；
> 3. 权重改为**每 pass 从 BRAM 装 300 B**（见 §5.4，LUT 最省但整层多 4.6 MB 流量）。

### 5.3 权重存储介质：LUTRAM（采用）vs BRAM

内层每拍要并行读 25 个权重字节，所以需要 **25 个独立读口**。

| 介质 | 容量/端口 | 资源 | 延迟 | 结论 |
|---|---|---|---|---|
| **分布式 LUTRAM（采用）** | 25 × 768×8 = 153,600 bit | ≈2,400 LUT6，0 BRAM | **组合读，0 拍** | **选用** |
| BRAM（RAMB18 TDP 1K×18 装 2 个 768×8 bank） | 25 bank → 13 块 RAMB18 | BRAM 22 → **35**（≤40），LUT −2,400 | 同步读，**+1 拍** | 备选/时序回退 |
| BRAM 每 bank 独占一块 | 25 块 RAMB18 | BRAM 22 → 47（超 40） | +1 拍 | 不选 |

结论：**LUTRAM**。原因是本设计 LUT 估算 ~6.5 k（预算 12 k）并不紧张，而 BRAM 22/40、
FF ~6.5 k；用 2,400 LUT 换 0 拍延迟，保住了已验证的 MAC 流水对齐（capture 时序一行不用改）。
BRAM 方案确实更省 LUT（−2,400）且 BRAM 也装得下（35/40），但要把权重读打一拍、
并把像素/capture 元数据整体后移 1 拍，属于「时序不收敛时的回退」，不是首选。

> 补充：整层权重容器是 48 oc × 16 ic × 28 B = **21.5 KB**；本设计只存 25 个有效抽头和
> 12 个有效 ic 的地址空间，占 25 bank × 768 深 × 8 bit = 153,600 bit = **19.2 kB**
> （其中有效抽头 48×12×25 = 14,400 B；地址空洞是 ic 12..15，读侧永不访问）。

### 5.4 权重装载方案：整层预装 vs 每 pass 装载（本题要求说明）

每个 pass 需要的权重 = `CIN × 25` 字节 = 12 × 25 = **300 B**。两种做法：

| 方案 | 权重存储 | 装载流量/整层 | 额外开销 | 结论 |
|---|---|---|---:|---|
| **整层预装（采用）** | 25 bank × 768×8 LUTRAM ≈ 2,400 LUT | 16,128 B 容器（占 19.2 KB LUTRAM），仅在启动时一次 | ≈18 k 拍（0.03%） | **选用** |
| 每 pass 装载（循环保持 `row→block→pass`） | 25 bank × 16×8 ≈ 400 B，≈50 LUT | 320 行 × 48 pass × 300 B ≈ **4.6 MB** | 1 B/拍时 ≈4.6 M 拍（约 7.7% 整层时间），且与行填充抢同一个读口 | 不选 |
| 每 pass 装载 + 把 pass 提到 row 外 | 同上 | 48 × 300 B ≈ 14.4 KB | **输入要重读 48 遍 ≈ 78 MB/层** | 不可行 |

结论：**整层预装**。理由：
1. 权重 LUTRAM 总量与旧版（100 × 192×8）完全相同，**没有因为降 oc 并行度而增加 LUT**；
2. 内层 0 次 DDR 权重读，不与行填充/写回争单读口，II 稳定为 1；
3. 每 pass 装载虽然能把权重 LUTRAM 从 2,400 降到 ~50 LUT6，
   但整层多 4.6 MB 流量（约 +7.7% 时间）和 15,360 次 AXI 短 burst，收益不划算。

### 5.5 oc 并行度取舍（DSP vs 时间，本题核心）

整机 DSP 预算 160 = HLS 92 + `conv_k1_engine` 33 = 125，**只剩 35 个**。
k=5 全网只有 1 层（`encoder.conv_first`，1.47 GMAC，约占全网 4.5%），
旧版却吃 100 个 DSP，性价比极低。

| 指标 | NOC=4（旧） | NOC=2 | **NOC=1（新默认）** |
|---|---:|---:|---:|
| DSP | 100 | 50 | **25** |
| 权重 bank / 深度 | 100 × 192×8 | 50 × 384×8 | **25 × 768×8** |
| 每像素拍数 | 144 | 288 | **576** |
| 整层计算拍数 | 14,745,600 | 29,491,200 | **58,982,400** |
| 整层时间 @150 MHz（含外围） | 0.104 s | 0.203 s | **0.399 s** |
| 2×320 条带拍数 | 115,939（实测） | ~208 k（推导） | **~392 k（推导）** |
| 相对 HLS 3.39 s | 32.6× | 16.7× | **8.5×** |
| 是否满足 ≤40 DSP | ✗（100） | ✗（50） | **✓（25）** |

取舍结论：
- **NOC=1（25 DSP）是唯一满足整机 ≤40 DSP 的选择**；NOC=2 的 50 DSP 已超预算。
- 代价是吞吐降到 1/4：该层从 ~0.12 s 变 ~0.40 s，但仍比 HLS 的 3.39 s 快 8.5×，
  且 k=5 只占整网 4.5% MAC，整网时间影响很小。
- 参数默认 1；需要做 DSP/时间对比实验时可例化 `OC_PER_PASS=2/4`，
  结构完全参数化（datapath 里不再有 4 条硬编码 lane）。
- **约束**：`OC_PER_PASS` 必须整除 8（输出按 8-oc beat 拼包）；
  另外 bias 在 capture 后只有 6 拍窗口（`cap_d[5]`）可读，支持 1/2/4；
  `OC_PER_PASS=8` 需要把 bias 读取推迟到 `cap_d[7]`，未实现。

---

## 5.6 【踩坑复盘】二维数组不会被推断成 BRAM ⇒ 213,562 个触发器

**现象（2026-10-27，k5 第一次进 BD 的综合）**：

```
k5_wrap_0:  Slice LUTs 70,527 (152.66%)   Slice Registers 213,562 (231.13%)   Block RAM Tile 0
整机 DRC:   ERROR [DRC UTLZ-1] FDRE over-utilized: requires 223,633 but only 92,850 available
```

**根因**：行缓冲写成了**二维**数组

```verilog
(* ram_style = "block" *) logic [63:0] lb_lo [0:4][0:LB_DEPTH-1];   // ❌ 2D
(* ram_style = "block" *) logic [63:0] lb_hi [0:4][0:LB_DEPTH-1];
```

Vivado 的 RAM 推断**只支持一维 unpacked 数组**；二维数组即使标了 `ram_style="block"`
也**不会**映射成 BRAM，而是把每个比特摊成一个触发器：
5×324×64×2 = 207,360 bit ≈ 213 k FF，正好对上 213,562。
隔离实验（同容量、同标注）：`2D ⇒ RAMB=0 / FDRE=81,984`；`1D ⇒ RAMB=4 / FDRE=0`。

**修法**：拆成 **10 个一维数组**（5 条行缓冲 × lo/hi 两个 bank），每个 324×64 = 20,736 bit
⇒ 1 块 BRAM36，合计 10 块。写口用 `case ({fill_line, fill_blk})` 选一个**静态**数组
（动态基址的 `mem[a][base +: 64]` 推断不出来，所以没合成一个 324×640 的大数组——
那样只要 6 块，但会退回触发器）。

**关键核对：读延迟没变。** 本文件的读口从第一版起就是同步读模板

```verilog
always_ff @(posedge ap_clk) lb_rd[i] <= {lb_hi_i[lb_raddr], lb_lo_i[lb_raddr]};
```

这正是 BRAM 的标准写法（地址打一拍、数据下一拍出）⇒
"寄存器组（0 拍组合读）→ BRAM（1 拍同步读）"**不改变任何相位**：
`lb_rd` 相对 `lb_raddr` 原来晚 1 拍，现在仍晚 1 拍；配套寄存的 `lb_raddr_d1`
（喂 `col_ok`）也天然对齐。改完包装层 TB 仍 **36/36 PASS**（行为不变）。

**通用规律（写进项目教训）**：

> **要进 BRAM/LUTRAM 的存储数组必须是一维。** `ram_style` 只决定"用哪种 RAM"，
> 不决定"是否用 RAM"；二维 `[0:A][0:B]` 在 Vivado 里等价于 A×B 个独立寄存器。
> 一维之后，**读口写法决定延迟**：`assign d = mem[a]` 是 0 拍（分布式 ROM/LUTRAM），
> `always_ff d <= mem[a]` 是 1 拍（BRAM / 带输出寄存的 LUTRAM）。
> 换存储介质前，先数清楚消费侧等了几拍。

**其它二维数组保持不动**（它们本来就该是寄存器，不是存储）：
`win[0:4][0:4]`（25×128 窗口移位寄存器，3,200 FF）、`b_r/m_r/p_acc/mac_wgt`
（DSP 的 A/B/M/P 流水寄存器）、`ts0..ts4`（加法树流水，51×26 ≈1,326 FF）。
它们没有 `ram_style`，综合会正常展开成触发器；拍平对资源/时序零影响，只会增加改错风险。
**FF 预算**（lb 归 0 后）：3,200(win) + 1,326(ts) + 1,200(p_acc，实际落在 DSP 内)
+ 640(lb_rd) + 其它 ≈ **6~9 k**，满足 ≤12 k。

---

## 6. 验证

### 6.1 已完成的检查

| 检查 | 结果 | 说明 |
|---|---|---|
| 参考模型 vs HLS cosim RTL | **PASS 30,720/30,720 字节一致** | `gen_k5_data.py` 的 `ref_conv` 与 `hls_k5_cosim/.../rtl.conv2d_engine.autotvout_gmem1.dat[8:8+30720]` 逐字节相同 |
| 现成 golden 一致性 | **md5 相同** | `golden/k5_12x48/expected_c8.bin` 与 `tb_c8_k5_12x48/out_golden.bin` md5 都是 `61d0476a…` |
| `xvlog --sv`（旧版 + 本次改动的 1 oc 版） | **0 error / 0 warning** | DUT + TB + `common/eq_checker.v`，按 `run_module.sh` 的宏 |
| `xelab` 参数展开（1 oc 版） | **PASS** | `k5_wbank(DEPTH=768)`、`conv_k5_engine` 例化成功 |
| 旧版 RTL golden（4 oc） | **PASS 30,720/30,720，115,939 拍** | `run_module.sh conv_k5_engine k5_12x48`（2026-10-26 报告） |
| 新版 RTL golden（1 oc，**行缓冲拍平之后**） | **PASS 30,720/30,720，392,419 拍，axi_errors=0** | 2026-10-27 实跑（`xvlog/xelab/xsim` + `golden/k5_12x48`，见 §6.4） |

### 6.2 testbench

`tb/tb_conv_k5_engine.v` 是统一验收设施的最小模板：

```verilog
`define TB_MODULE_NAME "conv_k5_engine"
`include "tb_common_tasks.vh"
conv_k5_engine #(.CIN(`TB_CIN), .COUT(`TB_COUT), .W(`TB_W),
                 .H(`TB_H), .PAD(`TB_PAD)) dut ( ... );
```

> TB 不显式传 `OC_PER_PASS`，所以默认 1（25 DSP）。要做 DSP/时间对比实验，
> 在 tb 例化里加 `.OC_PER_PASS(4)`（或 2）即可，端口/向量/比对都不用改。

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
# 1 oc：整层计算目标 58,982,400 拍
bash tools/run_module.sh conv_k5_engine --golden <k5_320_dir> \
     --target 58982400 --max-cycles 70000000
```

> 注意：`--target 58982400` 是**整层 `OC_PER_PASS=1` 的计算目标**；
> 2×320 条带的纯计算目标是 368,640，固定 18 k 权重装载使其到 ~392 k（1.065×），
> 不要拿条带判整层吞吐。

需要定位卡死时打开状态转储（默认关闭）：

```bash
bash tools/run_module.sh conv_k5_engine k5_12x48 --define K5_SM_DUMP
```

### 6.4 待回填

| 项 | 期望 | 实际 |
|---|---|---|
| 2×320 golden 逐字节 | 30,720/30,720 | ✅ **30,720/30,720**（0 / 30720 字节不一致） |
| 200 组 fuzz | 200/200 PASS | 待父 agent 跑 |
| 2×320 拍数（1 oc） | **≈392 k**（≤464 k 验收线） | ✅ **392,419 拍**（与推导的 ≈392 k 吻合） |
| 320×320 整层拍数（1 oc） | **≈59.9 M ≈0.40 s** | 推导值（§4.1：58,982,400 计算 + 外围）；条带实测与条带推导一致 ⇒ 可信 |
| 资源/Fmax | DSP **25** / BRAM ≈10 / LUT ≈8 k / FF ≈9 k / Fmax ≥150 MHz（**修掉 2D 数组后**，待复测） | 父 agent 复测 |

> 注：条带实测 392,419 拍与 §4.3 的条带推导（≈392 k）一致；整层请用 §4.1 的
> 58,982,400 + 外围 ≈59.9 M 拍，**不要**拿条带乘 100 去推（条带里权重装载/预载
> 占 4.6%，整层里不到 0.1%）。

---

## 7. 已知限制 / 明确没做到的地方

1. **未综合、未实现、未上板**。资源/Fmax 全是估算；`use_dsp` 能否真的映射成 25 个 DSP48E1，要 csynth 报告确认。
2. **RTL 功能仿真未由作者运行**。按父 agent 的规则，xsim 统一由父 agent 跑；本文不把「参考模型 PASS」当成「RTL PASS」。父 agent 的失败日志贴回后，按拍号/字节号修。
3. **输出写回与计算串行**。每块算完才写 out_buf（单缓冲），写回约 320 拍/块；1 oc 时每行 6 块共 1,920 拍，占整层 ≈1.0%（旧 4 oc 时 ≈4%）。双缓冲可省掉这部分，但会多一块 W×64 的 BRAM。
4. **权重装载 1 字节/拍**。整层约 18 k 拍可忽略；2×320 条带里它占 4.6%（18.1 k / 392 k），条带对纯计算比值 1.065×。若以后要压条带，可改 8 字节/拍并行写 8 个 bank（映射跨 28 字节容器边界，逻辑会复杂一些）。
5. **权重读是组合读**。1 oc 时是 25 个 **768:1** mux（比旧版 192:1 多约 2 级 LUT6），时序是估算；若 150 MHz 不收敛，按 §5.2 的回退顺序处理（同步读 + 像素侧 1 级对齐，II 仍 1）。
6. **形状假设**：`CIN=12`（物理 16，两个 8 通道块）、`COUT` 是 8 的倍数、`PAD=2`、`stride=1`、`dilation=1`、ReLU 固定开；`OC_PER_PASS` 必须整除 8（支持 1/2/4）。`ctrl_cin/cout/h/w` 目前只是契约占位，真正形状是编译期参数。
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
| 8 个位宽变体 MAC + `sext` | 8 个 `mac_muladd_*` | 1 个 48-bit 宏（25 或 NOC*25 份） |
| 25 个权重 bank × 2816 深 | 50 BRAM18K | NOC*25 个 (NGRP*16)×8 分布式 RAM（1 oc: 25×768×8，≈2,400 LUT） |
| 每次 (pixel,oc) 握手 | ~63 拍/次 | 无调用开销，连续流水 |
| 行缓冲列口 II=3 | `ceil(5/2)` | II=1 |

---

## 9. 关键信号速查（读代码用）

| 信号 | 含义 |
|---|---|
| `state` | 主 FSM（IDLE → 权重 → bias → 预载 → 行循环 → 块循环 → 写回） |
| `rd_sel` | 三路读口选择：0=输入，1=权重，2=bias |
| `grp = b_cnt*NG + s_cnt` | 0..NGRP-1 的 pass 号；权重地址 `grp*CPAD+ic_cnt`，bias 地址 `grp*NOC` |
| `win[r][c]` | 窗口第 r 行第 c 列（128-bit = 16 通道） |
| `sel[r]` | 窗口第 r 行 → 行缓冲号（随 y 左旋） |
| `p_acc[l][j]` | 第 l 个 oc lane、第 j 个 tap 的 48-bit 累加器（NOC*25 个；1 oc 时 25 个） |
| `first_d2/last_d3` | 组边界/最后一个 ic 的流水对齐信号 |
| `capture` | 组结束，把 NOC*25 个 P 值拍进 26-bit 加法树 |
| `cap_d[5]` | 加法树 6 拍后的有效标志 |
| `q_byte[0:NOC-1]` | 本 pass 的 NOC 个 oc 量化结果 |
| `obuf` / `obuf_wstrb` | 一个 8-oc 块的一行（W×64 bit）；每 pass 按 byte strobe 写入 |
| `wr_req_*` / `wr_*` | 写引擎：字节地址 + 64-bit 字数 |

---

## 10. 复现/交接

```bash
# 1) 语法（不综合、不仿真）
source /data/xilinx/2025.2/2025.2.1/Vivado/settings64.sh
cd pynq_specialized_code/pl/rtl_hand
xvlog --sv --relax -i tb -i common \
      common/eq_checker.v modules/conv_k5_engine.v tb/tb_conv_k5_engine.v

# 或只做统一设施的编译检查
bash tools/run_module.sh conv_k5_engine k5_12x48 --compile-only

# 2) 统一验收（父 agent）
bash tools/run_module.sh conv_k5_engine k5_12x48
bash tools/run_module.sh conv_k5_engine k5_12x48 --fuzz 200

# 3) 卡死定位（默认关闭的状态转储）
bash tools/run_module.sh conv_k5_engine k5_12x48 --define K5_SM_DUMP
```

> 本文的「待回填」项在父 agent 跑完后更新；如果 fuzz 失败，优先看字节号是否集中在某个 oc 块/某个 s 半区，那通常指向 `grp`/`s_cnt` 或输出拼包，而不是 MAC 本身。

---

## 11. AXI 包装层（`k5_axi_wrap` / `k5_axi_wrap_bd`）

> 状态（2026-10-27）：**RTL/脚本已落盘，`xvlog` 语法检查通过；未跑 xelab/xsim/综合，
> 未进 BD、未上板**。寄存器表与 k3 逐条一致（PS 侧 `ps/pynq/k5_engine.py` 直接可用）。

### 11.1 为什么需要两层（和 k1/k3 完全同构）

| 层 | 文件 | 语言 | 职责 |
|---|---|---|---|
| 包装层 | `k5_axi_wrap.v` | SystemVerilog | AXI4-Lite 从口 + 寄存器表 + 配置校验 + 地址窗口 + **三读合并成一读** + sticky 越界灯 |
| BD 适配层 | `k5_axi_wrap_bd.v` | **Verilog-2001** | 纯引脚级：把 2 个精简 AXI4 master 补成**完整 AXI4**（SIZE/BURST/CACHE/PROT/QOS/ID）并挂 `X_INTERFACE_INFO`，让 BD 推断出 interface pin |
| 配置校验 | `k5_cfg_check.v` | Verilog-2001 | 启动前比对"本次综合的常量"，非法则拒绝启动 |

适配层必须是 Verilog-2001：`create_bd_cell -type module -reference` 的**顶层文件**
不接受 SystemVerilog（`[filemgmt 56-195]` / `[BD 41-1690]`，k1 上实测踩到）。
限制只针对 reference 的顶层文件，内部例化的 SV 模块没问题。

### 11.2 寄存器表（与 k3 完全一致）

| offs 页（A[16]=0） | 寄存器 | 说明 |
|---|---|---|
| 0x10 / 0x14 | IN_ADDR lo/hi | 输入张量（C8 布局）；hi 被丢弃（PS 必须写 0） |
| 0x1C / 0x20 | WGT_ADDR lo/hi | 权重（PAD28 容器） |
| 0x28 / 0x2C | BIAS_ADDR lo/hi | bias（int32 × cout） |
| 0x34 / 0x38 | OUT_ADDR lo/hi | 输出（C8 布局） |

| ctrl 页（A[16]=1） | 寄存器 | 读写 | k5 里做什么 |
|---|---|---|---|
| 0x00 | AP_CTRL | W: bit0=start（自清） R: [1]done [2]idle [3]ready | 写 1 → `start_pend` |
| 0x10 | AP_RETURN | R | 0=成功；1..9=`k5_cfg_check` 错误码；0xA=窗口越界 |
| 0x18 / 0x20 | H / W | RW | **参与校验**（必须 =320） |
| 0x28 / 0x30 | HOUT / WOUT | RW | **只回显**（Hout/Wout 由 H/W 派生，stride=1） |
| 0x38 / 0x40 | CIN / COUT | RW | **参与校验**（必须 =12 / =48） |
| 0x48 | KSIZE | RW | **参与校验**（必须 =5，归到错误码 5） |
| 0x50 / 0x58 | STRIDE / DIL | RW | **参与校验**（必须 =1 / =1） |
| 0x60 | PAD | RW | **参与校验**（必须 =2，归到错误码 5） |
| 0x68 | SHIFT | RW | **参与校验**（≤31）并下传引擎 |
| 0x70 | RELU | RW | **只回显**（k5 的 ReLU 是硬连线；与 k1 的做法一致） |
| 0x94 | STATUS | R | [0]done [1]busy [2]err [7:4]err_code [8]idle [9]sticky_oob |
| 0x98 / 0x9C | WIN_LO / WIN_HI | RW | 四区地址窗口（默认 0 / 0xFFFF_FFFF） |
| 0xA0 / 0xA4 | STALL_FILL / STALL_MAC | R | **总线反压拍数**（语义与 k3 不同，见 11.4-2） |
| 0xA8 | 自检 | R | {26'd0, win_ok[5], ok[4], err[3:0]} |
| 0xAC | 四区窗口 | R | {28'd0, ok_ou[3], ok_bi[2], ok_wg[1], ok_in[0]} |
| 0xB0 | bytes_in | R | ⌈cin/8⌉·8·H·W·8 = **13,107,200**（0x00C8_0000） |

`bytes_*` 的口径（与 `k5_cfg_check` / 引擎记账逐条对齐）：

| 量 | 公式 | 真实层（12/48/320/320） |
|---|---|---|
| `bytes_in` | ⌈cin/8⌉·8·H·W·8 | 16·320·320·8 = 13,107,200 |
| `bytes_wgt` | cout·cin·28（PAD28） | 48·12·28 = 16,128 |
| `bytes_bias` | cout·4 | 192 |
| `bytes_out` | ⌈cout/8⌉·8·Hout·Wout·8 | 48·320·320·8 = **39,321,600** |

> ⚠ 这三个公式里的 `·8` 是 **k1/k3/k5 共同的记账口径**（PS 侧 `buffer_bytes()` 逐字同式），
> 所以窗口/钳制用的上界比真实张量大 8 倍。它**必须与 PS 保持一致**（PS 用同一组数算
> `win_lo/win_hi` 并分配缓冲区），不要"顺手修正"成真实字节数——那样窗口检查会全拒。

### 11.3 错误码（沿用 k3 语义）

| 码 | 含义 | k5 本版会不会命中 |
|---|---|---|
| 0x1 | cin ≠ 12 | 会（PS 写错） |
| 0x2 | cout ≠ 48 | 会 |
| 0x3 | stride ≠ 1 | 会 |
| 0x4 | dilation ≠ 1 | 会 |
| 0x5 | **H/W/pad/ksize** 与本次综合的常量不一致 | 会（★ 比 k3 多含 pad/ksize） |
| 0x6 | Hout/Wout 非正 | 恒不命中（H/W 已被 5 固定；码位保留） |
| 0x7 | shift > 31 | 会 |
| 0x8 | 槽容量保守上界 ⌈cin/8⌉·W·(d≠1?2:1) > CAP_B | 恒不命中（真实层 640 ≤ 4608；码位保留 + 改参数时兜底） |
| 0x9 | stride≠1 且 dilation≠1 | 恒不命中（两者都固定 1；码位保留） |
| 0xA | 四区没有整体落在 [WIN_LO, WIN_HI] 内 | 会 |

### 11.4 与 k3 包装层的差异清单（每条都是"k5 引擎端口不同"逼出来的）

| # | k3 | k5 | 为什么 |
|---|---|---|---|
| 1 | 引擎有 `ctrl_dil/ctrl_relu` 端口 | k5 引擎**没有** `ctrl_dil/ctrl_relu/ctrl_pad/ctrl_ksize` | k5 的 d/ReLU/pad/ksize 是编译期常量。寄存器仍存在（PS 契约不变），但只喂 `k5_cfg_check` 做拒绝判定 —— "PS 写 d=2 而硬件按 d=1 算"必须报错，不能静默算错 |
| 2 | `dbg_stall_fill/dbg_stall_mac` 由引擎导出（等填充/等抽头分类） | 引擎**没有**这两个端口 ⇒ 包装层在**总线边界**统计 | k5 FSM 顺序执行、无填充 FIFO/抽头仲裁，取不到内部分类。k5 的 STALL_FILL = 读 AR 未被接受 或 R 该给数没给 的拍数；STALL_MAC = 写 AW/W 未被接受的拍数。**语义是"互连/DDR 反压"，不能与 k3 的数值直接比较**，但判读方式一样（与时间线性增长 ⇒ 供数不足）。⚠ 底噪：每个读 burst 的固有 AR→首拍 R 延迟（1~3 拍）会计入 STALL_FILL，整层约几千拍（相对 59 M 拍 < 0.1%），**不要期望它恰好为 0** |
| 3 | 参数 `(LANES, CAP_B)` | 参数 `(CIN, COUT, W, H, PAD, OC_PER_PASS)` + `CAP_B` | 这 6 个原样下传引擎，同时作为 `k5_cfg_check` 的"合法集合"（不另设一份常量，避免两处不一致） |
| 4 | `HOUT/WOUT` 由 `(X-1)>>(S-1)+1` 派生（有移位器） | 直接 `Hout=H, Wout=W` | stride 被校验固定为 1 ⇒ 不需要移位器，也避开"移位量写成 `stride[0]`"那个已踩过的坑（k3 §18.2c 修复 #4） |
| 5 | `bytes_wgt` 用 `cout·cin·12`（PAD12） | `cout·cin·28`（PAD28），用 `x·32 − x·4` 两条移位 + 一次减法 | 28 字节/ic 是 PAD28 容器；用移位避免乘法器抢 DSP |
| 6 | 三路 `rlast` 恒接 `1'b0`（引擎不消费） | 同 k3：恒接 `1'b0` | k5 引擎按自己的 beat 计数收 burst（`rd_rlast` 只被赋值、从未被读），所以包装层不需要把 rlast 接进引擎；合并读口的 burst 结束判据用**从机的** RLAST（与 k3 逐行相同） |

**相同（刻意照抄、逐行未改）**：Lite 读写时序与分页、`start_pend → cfg_settle[3] → eng_start`
两级启动、`bytes_*` 三级流水与位宽纪律、四区 33 位窗口比较、`ok_in/ok_wg/ok_bi/ok_ou` 钳制 +
`sticky_oob`、合并读的 2-bit tag 仲裁与 R 通道路由、`0xA8/0xAC/0xB0` 自检口径。

### 11.5 为什么启动要走"两级"（时序关键，别改回去）

```verilog
// ❌ 曾经（k3 上的 BD 综合 WNS −43 ns / 101 级逻辑的根因）
.ctrl_start (do_start && cfg_ok && cfg_win_ok)     // 配置检查+窗口比较 ≈70 级组合
// ✅ 现在
start_pend → (等 cfg_settle[3]，≥8 拍) → eng_start（单拍脉冲，寄存器输出）
```

原则：**给 IP 的每个输入都应该是寄存器输出**。配置检查/地址窗口这类"慢逻辑"
只能驱动寄存器，不能直接驱动被包装模块的控制端口。代价是 PS 看到 start 晚 2 拍
（13 ns），而 PS 本来就在轮询 done ⇒ 语义不变。

### 11.6 BD 里怎么接（`scripts/bd_add_k5.tcl`）

| 项 | 值 |
|---|---|
| BD cell 名 | `k5_wrap`（`create_bd_cell -type module -reference k5_axi_wrap_bd`） |
| AXI-Lite | `axi_ctrl/M04_AXI → k5_wrap/s_axi`，**128KB @ 0x4008_0000**（A[16] 分页：offs 页 0x4008_0000、ctrl 页 0x4009_0000） |
| 合并读 | `k5_wrap/m_axi_rd → smc/S06_AXI`（in/wgt/bias 在包装层内部合并，2-bit tag） |
| 输出写 | `k5_wrap/m_axi_out → smc/S07_AXI` |
| 时钟/复位 | `ps7/FCLK_CLK0`（= engine/ap_clk）、`rst_0/peripheral_aresetn`（= engine/ap_rst_n），150 MHz 口径 |
| 地址空间 | 2 个 master 各映射 DDR 低 1GB @ 0x0 |

三条硬约束（脚本里逐条注释）：
1. **绝不缩 `smc` 的 NUM_SI**（已是 8；缩了会重排 SI 编号，k1 的 S02/S03、k3 的 S04/S05 全错位）。
2. **扩 `axi_ctrl` 的 NUM_MI 到 5 后，必须接 `M04_ACLK` / `M04_ARESETN`**，否则 `validate` 报 BD 41-758。
3. **`delete_bd_objs` 只删 cell，绝不删 net** —— 删 net 会连带删掉 `FCLK_CLK0` 等共用网络，
   之后 validate 报一片时钟悬空。上一版脚本在连 master 前"无条件删该 pin 上的 net"，
   实跑证明会踩这个坑；本版改成只对**悬空**的 interface net 调 `disconnect_bd_intf_net`，
   并在连完后断言时钟/复位网仍在。

### 11.7 包装层的 TB（`tb_k5_axi_wrap.v`）

T1..T5 与 `tb_k3_axi_wrap.v` 同构，但 DUT 用**小形状**（CIN=12/COUT=8/W=8/H=2；CIN 必须让 CPAD=16，原因见 RTL 注释）：

| 用例 | 内容 | 期望 |
|---|---|---|
| T1 | Lite 寄存器回环（形状 + 4 组 64 位地址 + 窗口） | 全部读回一致 |
| T2 | 拒绝 cin=16 / H=1 / stride=2 / shift=32 | AP_RETURN = 1 / 5 / 3 / 7，done 自打，**引擎 0 次启动** |
| T3 | 抬高 WIN_LO 使**只有 in 区**越界 | AP_RETURN = 0xA；0xAC = 0xE（ok_in=0） |
| T4 | 正常启动（H=2/W=8/cin=12/cout=8） | AP_RETURN=0、done=1、`eng_start` 恰好 1 次、7 个 AR、16 个写 beat |
| T5 | 运行期不变量 | STATUS[9]（sticky_oob）恒 0 |

为什么用小形状：k5 引擎形状是**编译期参数**，真实层整层 5,900 万拍，仿真跑不动；
小形状走的是同一套包装逻辑（被测对象），而 H=2 与真实 golden 条带 `k5_12x48` 行数一致
（pad 行也走到），`cout=8 + OC_PER_PASS=1` 的 8 个 pass 刚好拼满一个 64-bit beat
⇒ 输出写回路径也被覆盖。

**实跑结果（2026-10-27，本文件作者跑）**：`xvlog → xelab → xsim` **36/36 PASS**，
T4 全程 5,110 拍（`S_COMPUTE` 768 拍 = 8 像素 × 8 pass × 12 ic，与理论一致），
7 个读 AR（权重 256+80 两段、bias 4 拍、行填充 4 段）+ 2 个写 burst（每行 8 拍）：

```
[TB-DBG] AR id=1 addr=0x10020000 len=255     # 权重第 1 段（256 beat）
[TB-DBG] AR id=1 addr=0x10020800 len=79      # 第 2 段：地址 +2048、段长按剩余重算
[TB-DBG] AR id=2 addr=0x10040000 len=3       # bias 4 beat = 32 B
[TB-DBG] AR id=0 addr=0x10000000 len=7       # 行填充：row0 blk0
[TB-DBG] AR id=0 addr=0x10000080 len=7       #           row0 blk1（+BLK_BYTES=128）
[TB-DBG] AR id=0 addr=0x10000040 len=7       #           row1 blk0（+ROW_BYTES=64）
[TB-DBG] AR id=0 addr=0x100000c0 len=7       #           row1 blk1
[TB-DBG] AW addr=0x10060000 len=7            # 写回 row0（8 beat）
[TB-DBG] AW addr=0x10060040 len=7            # 写回 row1（+ROW_BYTES）
```

> ⚠ **TB 的从机模型踩过的坑（2026-10-27，值一条复盘）**：读从机的 `RLAST` 一开始写成
> 寄存器 `rd_rlast <= (rs_cnt == rs_len)`，比数据**晚一拍** ⇒ RLAST 只出现在"最后一个
> beat 之后的那一拍"（那拍 RVALID 已掉）。后果：包装层的 `rd_busy` 靠
> `rvalid && rready && rlast` 清零，于是一直为 1、后续 AR 全被挡住、引擎卡在
> `S_FILL_DATA`（busy=1 / done=0 / 零写 beat）。
> **为什么只有行填充挂、权重/bias 反而"侥幸跑过"**：权重/bias 的 `rd_ready = !w_word_valid`
> 会周期性反压，最后一个 beat 被**拖住一拍**，晚一拍的 RLAST 恰好赶上；
> 行填充 `rd_ready=1` 恒接受，末拍立刻走完 ⇒ RLAST 永远赶不上。
> 修法：`assign rd_rlast = rs_busy && (rs_cnt == rs_len);`（组合、与**本拍**给出的 beat 对齐）。
> 教训与 k3 §18.2c「门控要跟着数据延几级」同源：**握手侧的控制信号必须与数据同拍，
> 不能用"晚一拍的寄存器"去标末拍**。同一个从机模型在 `tb_k3_axi_wrap.v` 里也存在
> （那个 TB 写好后从未跑过），本文不改 k3 的文件，**建议父 agent 顺手同步修掉**。

### 11.8 验证状态（诚实版）

| 项 | 状态 |
|---|---|
| `xvlog`（`k5_axi_wrap.v` / `k5_axi_wrap_bd.v` / `tb_k5_axi_wrap.v`，含/不含 `-d K5_WRAP_DBG`） | ✅ 0 error / 0 warning |
| `k5_axi_wrap_bd.v` + `k5_cfg_check.v` 按 Verilog-2001 单独分析（不加 `--sv`） | ✅ 通过 |
| `xelab` / `xsim`（`tb_k5_axi_wrap`） | ✅ **36/36 PASS**（2026-10-27，修掉从机 RLAST bug 后；见 §11.7 的复盘） |
| `bd_add_k5.tcl` | ⏳ 未执行（会改 BD 工程；`tclsh info complete` 通过、与 k3 的命令集 diff 无新增） |
| 综合/Fmax/资源 | ⏳ 未跑（约束：包装层 + 引擎一起综合才作数，见 k3 §18.2d） |
| 上板 | ⏳ 未做 |

调试：`xvlog -d K5_WRAP_DBG` 后 TB 会逐次打印引擎状态迁移
（`[K5-SM] cyc=? S_IDLE->S_WGT_REQ ... busy/done/AR/R/W/y/b/x/s/ic/wr/lbv`），
并在**连续 2000 拍没有任何握手**时打印 `⚠` 告警——死锁定位靠这两行就够。
