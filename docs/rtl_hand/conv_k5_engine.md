# 手写 k=5 卷积引擎 `conv_k5_engine`

> 目标层：`encoder.conv_first`（320×320，cin=12→物理 16，cout=48，k=5，pad=2，stride=1，ReLU）
> 现状：**默认 `OC_PER_PASS=1` → 25 DSP**，整层约 0.40 s（HLS 3.39 s 的 1/8.5）。
> 资源目标：DSP ≤40（整机 160 − HLS 92 − k1 33 = 35）、BRAM ≤40、LUT ≤12,000、Fmax ≥150 MHz。
> 历史基线：`OC_PER_PASS=4` 版本（100 DSP）已通过 30,720/30,720 黄金验收，实测 115,939 拍。
> 本文件记录结构、oc 并行度取舍、拍数推导、资源估算、验证状态和已知限制。
> 代码：
> - RTL：`pynq_specialized_code/pl/rtl_hand/modules/conv_k5_engine.v`
> - TB：`pynq_specialized_code/pl/rtl_hand/tb/tb_conv_k5_engine.v`（接统一验收设施）
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
| 资源（估计） | **DSP 25** / BRAM18 ≈22 / LUT ≈6.4 k / FF ≈6.5 k | 估计，未综合 |
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
| BRAM18 | **≈22** | 行缓冲 5×2 bank×2 = 20；输出行缓冲 2（与旧版相同） |
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

## 6. 验证

### 6.1 已完成的检查

| 检查 | 结果 | 说明 |
|---|---|---|
| 参考模型 vs HLS cosim RTL | **PASS 30,720/30,720 字节一致** | `gen_k5_data.py` 的 `ref_conv` 与 `hls_k5_cosim/.../rtl.conv2d_engine.autotvout_gmem1.dat[8:8+30720]` 逐字节相同 |
| 现成 golden 一致性 | **md5 相同** | `golden/k5_12x48/expected_c8.bin` 与 `tb_c8_k5_12x48/out_golden.bin` md5 都是 `61d0476a…` |
| `xvlog --sv`（旧版 + 本次改动的 1 oc 版） | **0 error / 0 warning** | DUT + TB + `common/eq_checker.v`，按 `run_module.sh` 的宏 |
| `xelab` 参数展开（1 oc 版） | **PASS** | `k5_wbank(DEPTH=768)`、`conv_k5_engine` 例化成功 |
| 旧版 RTL golden（4 oc） | **PASS 30,720/30,720，115,939 拍** | `run_module.sh conv_k5_engine k5_12x48`（2026-10-26 报告） |
| 新版 RTL golden（1 oc） | **未跑（本次任务明确不跑仿真）** | 父 agent 跑 `run_module.sh` 回填 |

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
| 2×320 golden 逐字节 | 30,720/30,720 | 待父 agent 跑 |
| 200 组 fuzz | 200/200 PASS | 待父 agent 跑 |
| 2×320 拍数（1 oc） | **≈392 k**（≤464 k 验收线） | 待回填 |
| 320×320 整层拍数（1 oc） | **≈59.9 M ≈0.40 s** | 待回填 |
| 资源/Fmax | DSP **25** / BRAM 22 / LUT ≈6.5 k / Fmax ≥150 MHz（待确认） | 待综合 |

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
