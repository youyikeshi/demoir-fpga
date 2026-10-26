# conv_k1_engine · 手写 1×1 卷积加速器（k=1, s=1, pad=0, d=1）

> 交付物：`pynq_specialized_code/pl/rtl_hand/modules/conv_k1_engine.v`
> testbench：`pynq_specialized_code/pl/rtl_hand/tb/tb_conv_k1_engine.v`（39 行共享设施模板）
> 诊断生成器：`pynq_specialized_code/pl/rtl_hand/tools/gen_k1_debug_cases.py`
> 契约：`docs/rtl_hand/00_SPEC_接口契约.md` §3
> 依据：`docs/rtl_map/04_mac_array.md`（k=1 现状 16 车道 + 21 拍收尾）、
> `docs/rtl_map/03_weight_path.md`（每 (y,列块,ocb) 重装 = 237 MB/全网）
> 最后更新：2026-10-26 15:30（**资源瘦身改动；未综合、未上板；xsim 由主控跑**）

---

## 0. 结论（先看这段）

| 项 | 状态 |
|---|---|
| **v1 固定形状（cin≤64,cout≤64）** | ✅ 主控实测：39/39 诊断、`k1_64x64` **40960/40960 字节全对**、**82,272 拍** vs HLS 基线 531,437 ⇒ **0.155×（6.5× 提速）** |
| **v2 真实形状（CIN_MAX=352, COUT_MAX=192）** | ✅ 主控实测：**9/9 真实形状全对（440,320/440,320 字节）**，含 (224,64)、(160,64)、(208,48)、(256,96)、(64,192)、(352,32) 与 3 组边界。形状上**覆盖全部 132 个 k=1 层**（cin∈[16,352]、cout∈[16,192]，全 8 倍数）。v1 回归仍 39/39 + 40960/40960，拍数 **82,561**（0.155×） |
| 全网 k=1 估算 | `Σ H·W·⌈cout/32⌉·cin = 5.80e8` 拍 ⇒ **3.87 s @150 MHz / 5.80 s @100 MHz**，对比实测 27.486 s ⇒ **7.1× / 4.7×**（纯计算口径，未含 PS 路由/填充） |
| DSP | **32**（不随 cout 涨；这是"32 车道 + 多 pass"的直接结果） |
| **200 组 fuzz（契约 §5）** | ✅ **200/200 PASS**，8,192,000/8,192,000 字节，0 越界 / 0 unknown |
| **运行期 ReLU（本次）** | ✅ 新增 `ctrl_relu` 输入，收尾按运行期值选 `clamp(v>>shift,0,127)` 或 `clamp(...,-128,127)`；**24 个 `*.conv_proj`（relu=False）现在可路由** ⇒ k=1 覆盖 **132/132 层**（此前 108/132） |
| 综合 | **未做**；资源/时序都是估算，见 §6 |

> v1 最初错 23.3%，根因是**两个 Verilog 自决定位宽 bug**（part-select 基址里的
> `a*b` 只取 max(操作数位宽)）。完整根因 + 35 例逐条对照见 §9.3——这是本模块
> 最有价值的经验，k=3/k=5 请照抄这套"一 case 只放开一个变量"的诊断法。

---

## 1. 能力与覆盖（v2）

网络 132 个 k=1 层的通道分布（`runs/prof20261023_grid/layers_model_final.json`）：

| cout | 层数 | MAC 占比 | 本设计有效 MAC/拍 |
|---:|---:|---:|---:|
| 16 | 108 | 37.25% | 16（32 车道只用 16） |
| 48 | 4 | 15.74% | 24（32 车道用 24） |
| 64 | 12 | 29.98% | 32 |
| 96 | 4 | 9.96% | 32 |
| 192 | 4 | 7.08% | 32 |
| **合计** | **132** | **1.303e10 MAC** | **22.46（加权）** |

- `cin` 最大 352、`cout` 最大 192；两者全部是 8 的倍数（已核对）。
- **覆盖 132/132 层**（形状口径）。`cout` 用 `⌈cout/32⌉` 个 pass 覆盖，车道数
  恒 32 ⇒ DSP 恒 32。
- 明显可做的下一版优化：cout=16 的 108 层只用了 16/32 车道。若让**两个像素**
  共享 32 车道（每 16 车道一个像素），这 37.25% 的 MAC 吞吐翻倍，全网拍数
  可从 5.80e8 降到约 4.6e8（估）。本版没做，先保正确性。

---

## 2. 接口

端口与契约 §3 **逐字一致**（`m_axi_in_*` / `m_axi_w_*` / `m_axi_b_*` /
`m_axi_out_*` 各一组；`ctrl_*` 一组）。参数：

| 参数 | 默认 | 作用 |
|---|---:|---|
| `CIN/COUT/W/H/PAD/STRIDE/DIL/RELU` | 64/64/320/320/0/1/1/1 | 与契约同名；运行期形状走 `ctrl_*` |
| `LANES` | 32 | oc 车道数（= 每 pass 的 oc 数） |
| `CIN_MAX` | **352** | 运行期 cin 上界 ⇒ 每 lane 权重 44 个 64-bit 字 |
| `COUT_MAX` | **192** | 运行期 cout 上界 ⇒ 最多 6 个 pass |
| `NBUF` | 4 | 输入像素缓冲数（预取深度） |
| `OBUF_N` | 4 | 输出块队列深度（每块 4 个 oc 组） |

- 运行期：`ctrl_cin/cout/h/w/shift` 在 `ctrl_start` 上升沿锁存；
- `PAD/STRIDE/DIL` 无运行期寄存器（k=1 恒 0/1/1）；
- **RELU 是编译期参数**（默认 1；线性层需 `RELU=0` 重出一版）。

数据格式（契约 §2）：激活 C8；权重 `weight[oc][ic]`；输出 C8；
`clamp((acc + bias) >>> shift, 0, 127)`；acc **26 位**。

---

## 3. 结构

```
           ┌──────────────── 权重：每 pass 32 lane × cin 字节（44×64b LUTRAM/lane）────────┐
 DDR ─w──► │ 权重装载 FSM（按 pass；每 pass 32 笔 AR，每 oc 一笔 ARLEN=cin/8−1）           │
           │ 单 bank：pass 结束→重装下一 pass 的 32 个 oc；装载 ≈ 32·(cin/8+2) 拍          │
           └───────────────────────────────┬────────────────────────────────────────────────┘
 DDR ─b──► 偏置装载：cout×int32 一次装完（最多 96 拍），每 lane 存 6 个 pass 的 bias
           ┌───────────────────────────────┴────────────────────────────────────────────────┐
 DDR ─in─► │ 像素预取：每像素 ceil(cin/8) 笔 1-beat，4 × 2816b 移存器（C8 组）              │
           └───────────────────────────────┬────────────────────────────────────────────────┘
                                           ▼
     ┌──────────── 计算流水（II=1，3 级）────────────┐
     │ A: p / wmem[ic>>3] / parity / last / pix_off  │
     │ B: 32 × (p × w[ic&7])  8×8 signed → 16b       │
     │ C: 32 × 26b 奇偶双链累加；last 拍快照 + 清 0  │
     └───────────────────────┬───────────────────────┘
                             ▼
     ┌──────── 收尾 3 拍（32 路并行，不是循环）────────┐
     │ E1: ep_e + ep_o (27b)                          │
     │ E2: + bias[pass][lane] (32b)                   │
     │ E3: >>>shift → ReLU → sat8 → 拼 4 个 64b 组    │
     └───────────────────────┬───────────────────────┘
                             ▼
              输出块队列（4 块 × 4 组）──► m_axi_out（8B/笔，AWLEN=0）
```

### 3.1 多 pass 与权重重装

- 每个 pass 算 `oc = pass*32 + lane`（最后一个 pass 可能只用到 `cout−32·pass` 个 lane）；
- pass 内权重常驻（单 bank），pass 结束后由 loader 装下一个 pass 的 32×cin 字节；
- **每层权重总搬运 = cin·cout 字节**（例：(224,64) = 14,336 B），而 HLS 是
  `cin·cout·Hout·⌈Wout/64⌉`（320×320 时 (224,64) = **22.9 MB/层**）⇒ **约 1600× 少搬**；
- 单 pass 装载 ≈ `32·(cin/8+2)` 拍（cin=224 时 ≈ 960 拍），相对该 pass 的
  `H·W·cin`（320×320 时 22.9M 拍）是 **0.004%**，所以**不需要 ping-pong**——
  这是相对 v1 有意的简化（v1 的双 bank 只藏 4 KB，收益本来就是 0.004% 量级）；
- 偏置一次装完，每 lane 存 6 个 pass 的值，收尾用 `bias_lane[lane][pass]` 6:1 mux。

### 3.2 为什么收尾是 2 拍

`04_mac_array.md` §4.3：HLS 的 21 拍来自 `LOOP_730_36`（trip=16 的流水循环）。
本版 32 路并行：**最后一块拍直接算 `sum27 = acc_e + acc_o + prod_b`**（两条奇偶链
+ 最后一个乘积一次加完，总数与 parity 无关），下一拍 `+bias`，再组合地
`>>>shift` + **运行期 ReLU/饱和**并推入输出队列。固定 **2 级收尾**，且与下一块重叠
（下一块在上一块最后乘积后 1 拍就开始发射）。

运行期 ReLU 的饱和：
```verilog
relu_run = ctrl_relu;                       // 1: clamp(v>>shift, 0, 127)
if (relu_run && shf[31]) sat = 0;           // 0: 不砍负值，只做有符号饱和
else if (shf > 127)      sat = 127;
else if (shf < -128)     sat = -128;
else                     sat = shf[7:0];
```
代价只有每 lane 一个 2 输入 AND（~32 LUT），DSP/BRAM/FF 不变。

> 2026-10-23 的第二版把原来的 `ep_e/ep_o` 快照 + E1 合并这一级删掉了：
> 省 2×32×26 = 3,328 FF，收尾从 3 级降到 2 级，功能等价（39 例/黄金/200 fuzz
> 会重新验证）。

---

## 4. 每层拍数推导

记 `P = H·W`、`B = ⌈COUT/32⌉`、`L = CIN`。

```
计算：      P × B × L                          （II=1；收尾与下一块重叠）
每 pass 装载：32 × (L/8 + 2) 拍               （与 pass 计算比 ≈ 0.004%）
偏置：      ⌈COUT/2⌉ 拍，一次
像素预取：  每像素 ⌈L/8⌉ 笔 1-beat；4 缓冲预取
排空：      最后一块 E3→write→B ≈ 几十拍
```

### 4.1 真实形状（H=2, W=320，与现有 cosim 数据集同尺寸；**主控实测**）

| 形状 | P | B | 计算拍 | 预测总拍 | **实测拍数** | 偏差 |
|---|---:|---:|---:|---:|---:|---:|
| `k1_224x64`（最大时间块 19.4%） | 640 | 2 | 286,720 | ≈ 288.7k | **288,721** | +0.0% |
| `k1_160x64`（10.6%） | 640 | 2 | 204,800 | ≈ 206.7k | **206,257** | −0.2% |
| `k1_208x48`（10.3%） | 640 | 2 | 266,240 | ≈ 268.2k | **267,667** | −0.2% |
| `k1_256x96`（6.3%） | 640 | 3 | 491,520 | ≈ 494.5k | **494,923** | +0.1% |
| `k1_64x192`（cout 上界，6 pass） | 640 | 6 | 245,760 | ≈ 248.3k | **247,657** | −0.3% |
| `k1_352x32`（cin 上界） | 640 | 1 | 225,280 | ≈ 226.8k | **226,831** | +0.0% |
| (224,64) 全 0/全 127/全 −128 | 640 | 2 | 286,720 | ≈ 288.7k | **288,721** ×3 | +0.0% |

### 4.2 全幅 320×320（未仿，只给公式值）

| 形状 | 计算拍 | 时间 @150 MHz |
|---|---:|---:|
| (224,64) | 45.88 M | 305.8 ms |
| (160,64) | 32.77 M | 218.4 ms |
| (208,48) | 42.60 M | 284.0 ms |
| (256,96) | 78.64 M | 524.3 ms |
| (64,192) | 39.32 M | 262.1 ms |
| (352,32) | 36.04 M | 240.2 ms |

### 4.3 全网 k=1 估算

```
Σ H·W·⌈cout/32⌉·cin = 5.80e8 拍
  @150 MHz = 3.87 s   （实测 27.486 s ⇒ 7.1×）
  @100 MHz = 5.80 s   （⇒ 4.7×）
有效 MAC/拍 = 22.46（cout=16 的 108 层只有 16/32 车道忙）
```

> 口径：纯计算，不含 PS 侧逐层路由、输入/输出 DDR 带宽（每层输入=H·W·cin·B B、
> 输出=H·W·cout B，在 320×320 下都 < 50 MB/层，相对计算可藏）。
> **不是**端到端 27.49 s → 3.87 s 的承诺，PS 侧还没接。

---

## 5. 输入预取

- 每像素 `⌈cin/8⌉` 笔 1-beat（C8 组步长 `H·W·8`），一次 fetch 一个像素；
- 4 个像素缓冲 ring，最多领先 3 个像素；每像素计算 `cin` 拍，
  预取 `2·⌈cin/8⌉` 拍（cin=224 时 56 拍）⇒ DDR 延迟被藏住；
- 像素缓冲是 **4 × 44 × 64-bit 的简单双口 LUTRAM**，地址 `{buffer, group}`：
  写 = `{f_pix, f_rd}`（fetch 每拍写一个 64-bit 组字）；读 = **同步预取** `p_word_q`，
  组内再按 `ic_cur[2:0]` 选字节。**没有 2816-bit 移位、没有环形指针**。
- **读必须放在 always_ff 里（和写同一个 always）**：一开始写成 continuous assign
  （异步读），Vivado 不认成 RAM，展开成 16,384 FF + 256:1 的 64-bit 读 mux
  （第三轮 FF 31,349 / logic LUT 20,741 的根因）。现在 `p_word_q <= px_mem[nxt_addr]`
  是同步读，`p_word_q` 只有 64 FF（32 lane 共用），RAM 才是真 LUTRAM。
- 预取地址 `nxt_addr = {nxt_buf, nxt_grp}`：正常 issue 组不变/字节 7 进下一组；
  最后一块拍切到 `{下一 buffer, 0}`；停顿保持当前组。
- **RAM 输出寄存器必须单一源**：旁路不能写进 `p_ram_q` 的 always（Vivado 的 RAM
  模板不接受一个寄存器两个源，会退化成 16,384 FF + 256:1 读 mux——第三轮实测）。
  正确写法：
  ```verilog
  // 纯模板（p_ram_q 只有这一个赋值源）
  always_ff @(posedge ap_clk) begin
      if (in_r_hs) px_mem[{f_pix[1:0], f_rd}] <= m_axi_in_rdata;
      p_ram_q <= px_mem[nxt_addr];
  end
  // 旁路寄存一拍，计算侧做组合 mux
  bp_hit_q  <= bypass_now;   bp_data_q <= m_axi_in_rdata;
  wire [63:0] p_word = bp_hit_q ? bp_data_q : p_ram_q;
  ```
- **cin=8 的单 beat 旁路**：beat 在 t、首次 issue 在 t+1，t+1 时 `in_r_hs` 已是 0，
  所以旁路**必须寄存**（不能直接在 use 拍组合选 `m_axi_in_rdata`，会晚一拍、数据已失效）。
  `bypass_now = in_r_hs && f_rd==0 && f_pix[1:0]==nxt_buf && nxt_grp==0`；
  多 beat 像素时 group0 早已写入 RAM，`bp_hit_q` 只影响等待拍，行为不变。
- 多 pass 会把输入读 `B` 遍：320×320、(224,64) 时 2×45.9 MB = 92 MB/层，
  以 150 MHz/8B/拍 ≈ 76 ms，相对 306 ms 计算可重叠。

---

## 6. 资源估算（第一轮综合实测 + 修复后预期）

### 6.0 第一轮综合（2026-10-23 20:58，`synth_design -top conv_k1_engine -mode out_of_context`）

| 资源 | 实测（修复前） | 判定 / 原因 |
|---|---:|---|
| Block RAM Tile | **0** | ✅ `ram_style="distributed"` 生效，44×64 LUTRAM ×32 没有变 BRAM |
| Slice LUTs | 24,530（53.1%） | ❌ 32 个 8×8 乘全落在 LUT（DSP 只有 1） |
| Slice Registers | 39,662（42.9%） | ❌ `px_sh`/`pix_ready` 多驱动 ⇒ 4×2816 被重复计数 |
| DSPs | 1 | ❌ 未加 `use_dsp`，Vivado 判"8×8 太小不值得占 DSP" |

**已修（20:59，未复测）**：
1. `px_sh` 的 fetch 写 + 计算移位合并进**同一个 always_ff**；`pix_ready` 的置位/清除
   也合并进 fetch always。全模块每个 reg 只剩一个驱动。
2. 每个 lane 加 `(* use_dsp = "yes" *) wire signed [15:0] mul_prod = p_a * wbyte`，
   **组合使用 DSP**（不启用内部 A/B/M/P 寄存器）⇒ 延迟仍 1 拍，不需要重排对齐。
3. `wl_done_blk` 前向声明，`xvlog -sv`（不带 `--relax`）0 warning。

**第一轮修复后实测（21:0x，主控）**：CRITICAL WARNING 0、FF **28,420**、DSP **33**、
BRAM 0、LUT **34,057**（反而涨）、Fmax **≈162 MHz**。功能回归 39/39 + 40960/40960 +
82,561 拍 PASS。

**第二轮（LUT 根因，21:1x）**：34,057 LUT 的大头是 `px_sh` 的“整条 2816-bit 移位 +
64-bit 部分写”——每一位都有两个写入源，综合成 ~2–3 LUT/FF。已改为
**`{buffer, group}` 寻址的简单双口 LUTRAM**（见 §5）：0 FF、~256 LUT6、0 BRAM、
异步读无延迟。顺带把收尾从 3 级压到 2 级（§3.2，省 3,328 FF），
push 数据改组合写入（省 384 FF），`OBUF_N` 保持 4（2 深在 cin=16 时会被写侧反压）。

**第二轮预期**：FF 28,420 − 11,264（像素缓冲）− 3,328（ep_e/ep_o）− 384（push 数据）
= **≈13.4k**；LUT 去掉 ~31k 的移位 mux 后应 **≤12k**；DSP **32**；BRAM **0**；
Fmax 预计仍 ≥150 MHz（像素 LUTRAM 的异步读在 p_a 之前，路径 ~1.5 ns）。
**以主控复测为准。**

**第二轮功能回归（21:10，主控）**：39/39 诊断 + 40960/40960 黄金 + **82,559 拍**
（0.155×，比第一轮少 2 拍 = 收尾 2 级）PASS —— 像素 LUTRAM + 2 级收尾行为正确。

**第三轮综合（21:1x，主控）**：LUT **23,493**（logic 20,741 + memory 2,752）、
FF **31,349**、DSP 33、BRAM 0、Fmax 162 MHz、CRITICAL 0。LUT 降了 10.5k，
但 FF 反涨 2.9k——**根因：`px_mem` 的异步读（continuous assign）没被推断成 RAM**，
退化成 16,384 FF + 256:1 读 mux。`LUT as Memory` 两轮都是 2,752 不变，就是证据。

**第四轮（21:2x）**：像素 RAM 改成**纯推断模板**：
- 读寄存器 `p_ram_q` 在专用 always 里**只**由 `px_mem[nxt_addr]` 赋值（写口用
  write-enable，模板允许）；**禁止**把 cin=8 旁路写进这个 always（主控网表证据：
  一个寄存器两个源会让 16,384 FF + 256:1 读 mux 复活）；
- 旁路改成独立寄存器 `bp_hit_q/bp_data_q`（beat 在 t、首次 issue 在 t+1，必须寄存），
  计算侧 `p_word = bp_hit_q ? bp_data_q : p_ram_q`；
- lane 阵列加 `(* keep_hierarchy = "yes" *)` 供分层利用率报告。
预期 FF **≈15.0k**、LUT **≈7–8k**、DSP 33、BRAM 0、Fmax ≥162 MHz。**待主控复测。**

### 6.1 结构估算（修复后）

| 项 | 估算 | 依据 / 风险 |
|---|---:|---|
| 8×8 乘法器 | **32 个 ⇒ 32 DSP48**（`use_dsp="yes"` 组合乘） | 红线 DSP ≤ 60 ✅；**不随 cout 涨**。修复前实测只有 1 DSP |
| 权重 RAM | 32 lane × 44 word × 64 bit = 90,112 bit **分布式 RAM** ≈ **2.0–2.2k LUT** | `ram_style="distributed"`。**若被推成 BRAM：32 个小 RAM ≥ 32 BRAM18，超 30 红线** ⇒ 综合必须确认 |
| 像素缓冲 | **4 × 44 × 64-bit 简单双口 LUTRAM** ≈ **0.25k LUT / 0 FF / 0 BRAM** | `{buffer,group}` 寻址，异步读；不再用移位寄存器 |
| bias | 32 × 6 × 32 = **6,144 FF** + 6:1 mux ≈ 0.3–0.6k LUT | |
| 累加器 | 32 × 2 × 26 = 1,664 FF | |
| 收尾 | 32×(27b 加 + 32b 加 + 移位/饱和) ≈ 3.0–3.5k LUT；~3.5k FF | |
| 权重字 | 32 × 64 = 2,048 FF | |
| 输出队列 | 4 块 × 4 组 × 96 bit = 1,536 FF | |
| **LUT 合计** | **≈ 5–8k**（第四轮预期） | 红线 12,000；第一轮 24,530、第二轮 34,057、第三轮 23,493（logic 20,741 是 256:1 读 mux）；第四轮去掉后待复测 |
| **BRAM18** | **0**（权重走分布式） | 红线 30 ✅（前提同上） |
| **FF** | **≈ 15.0k**（第四轮预期） | 第三轮 31,349（其中 px_mem 异步读退化成 16,384 FF）；同步预取后 −16,320 |
| **Fmax** | **≈162 MHz**（第一轮，未布线）；第二轮待复测 | 关键路径：像素 LUTRAM 读→字节 mux→p_a，或 DSP 乘→prod_b |

> 对主控问题的直接回答：**DSP 仍是 32，没有涨**。cout 192 是靠 6 个 pass，
> 不是 192 个车道。第一轮真正涨的是**权重 LUTRAM（2k LUT）+ 像素缓冲的移位 mux**；
> 第二轮把像素缓冲改成 4×44×64-bit LUTRAM 后，FF 和 LUT 都应大幅下降。

---

## 7. 验证状态

### 7.1 已跑（主控执行）

| 项 | 结果 |
|---|---|
| `k1_64x64` 黄金（v1 原始） | **40,960 / 40,960 字节全对** ✅，82,272 拍 |
| 39 组定向诊断（v1 原始） | **39 / 39 PASS** ✅ |
| **v1 回归（v2 代码）** | **39/39 + 40960/40960 + 82,561 拍**（0.155×）✅ |
| **v2 真实形状 9 组** | **9/9 PASS，440,320/440,320 字节**；拍数见表 §4.1（288,721 / 206,257 / 267,667 / 494,923 / 247,657 / 226,831 ×3 边界）✅ |
| **200 组 fuzz（v1 形状）** | **200/200 PASS，8,192,000/8,192,000 字节**，82,561 拍/组 ✅ |
| **runtime ReLU 回归（relu=1）** | **39/39 + 40960/40960 + 9/9（440320/440320）** 全过 ✅ |
| **relu=0 真实 conv_proj 3 组** | **3/3 PASS，133,120/133,120 字节**：(160,64) 206,255 拍、(224,96) 433,072 拍、(64,48) 82,409 拍 ✅ |
| AXI 越界 / unknown | 0 / 0 ✅ |

### 7.2 未跑（本版 v2 的验收清单）

```bash
cd pynq_specialized_code/pl/rtl_hand
# 0) v1 回归（v2 代码）：已跑，39/39 + 40960/40960 + 82,561 拍
./tools/run_module.sh conv_k1_engine k1_64x64 --fuzz-manifest /tmp/k1_dbg/manifest.txt
./tools/run_module.sh conv_k1_engine k1_64x64
# 1) v2 真实形状 9 组：已跑，9/9 + 440320/440320
./tools/run_module.sh conv_k1_engine k1_64x64 --fuzz-manifest /tmp/k1_real/manifest.txt
# 2) 200 组 fuzz（v1 形状）：已跑，200/200 + 8192000/8192000
./tools/run_module.sh conv_k1_engine k1_64x64 --fuzz 200
# 3) relu=0（线性 + 有符号饱和）真实 conv_proj 形状：待跑
./tools/run_module.sh conv_k1_engine k1_64x64 --fuzz-manifest /tmp/k1_relu0/manifest.txt
```

> v2 的 9 组真实形状：`k1_224x64 / 160x64 / 208x48 / 256x96 / 64x192 / 352x32`
> 加 3 组边界（(224,64) 全 0 / 全 127 / 全 −128）。所有 `expected_c8.bin`
> 由 `conv_ref_rt.cpp`（shared `golden_driver`）生成，并用独立 numpy 参考复核过。

---

## 8. 已知限制

1. **没有全幅 320×320 的仿真**：golden 都是 H=2,W=320（与现有 cosim 数据集同尺寸）。
   地址/拍数逻辑与 H/W 无关，但"全幅"只能给公式值；完整 6.5 MB golden 的仿真
   留给上板前的一次性长跑。
2. **cin 必须是 8 的倍数、cin ≥ 8**：权重按 `cin/8` 个 64-bit 字整读。全网络
   132 个 k=1 层已核对满足；但不是架构保证。
3. **RELU 现在是运行期**：`ctrl_relu` 输入（1=ReLU，0=线性+有符号饱和）。
   TB 侧由 golden `meta.json` 的 `relu` 字段驱动（共享 TB 读成 `tb_relu`，
   模板里 `ctrl_relu = (tb_relu != 0)`），**不需要新的 plusarg**；
   wrapper 侧是 0x70 寄存器 bit0，复位值 = 编译期参数 `RELU`（默认 1，向后兼容）。
4. **cout 尾块**：`cout` 非 8 倍数时最后组尾部写 0（契约允许 PS 不读）；
   `cout_pad` 之外不写。
5. **单拍 8 B 写**：每个 oc 组一笔 AWLEN=0。320×320 层输出 6.55 MB，
   按 ~4 拍/笔排空 ≈ 22 ms，可被计算重叠；若 `cin` 很小而 `cout` 很大，
   写通道可能成为瓶颈（下一版改 8 像素 × 一组的 64 B burst）。
6. **32 位地址**：`H·W·8·⌈cin/8⌉` 必须 < 2^32。网络 320×320 远小于；4096×4096
   会溢出（契约本身也有 `cin·H·W < 2^31` 的限制）。
7. **资源/时序未综合**：尤其权重 RAM 是否保持 LUTRAM；若变 BRAM 会超 30 块。
8. **cout=16 的 108 层只用 16/32 车道**：吞吐减半，见 §1 的下一版优化。

---

## 9. 验证经验（最有价值的部分）

### 9.0 综合暴露的仿真盲区：多驱动

第一轮综合报了两条 `Synth 8-6859 multi-driven net`：
- `px_sh`：fetch always 写 64-bit slice，另一个 always 做整字移位；
- `pix_ready`：指针 always 清 0，fetch always 置 1。

xsim 允许两个 `always_ff` 驱动同一个 reg（同一时间步后写覆盖），所以 **39 例 + 黄金
+ 200 fuzz 全过也发现不了**；Vivado 却会说"行为不确定"。修法是**把同一 net 的
所有写收敛到一个 always_ff**（本版把 shift 并进 fetch、把 ready 清除并进 fetch）。
教训：**仿真通过 ≠ 综合安全**，多驱动/锁存器/组合环这三类必须在综合报告里清零。

第四个仿真边界：**非阻塞读的“同拍写读”**。同步 RAM 的读是上一拍地址的数据；
如果预取读和 group0 写发生在同一拍（cin=8 单 beat），读拿到的是写之前的旧值。
多 beat 像素天然有 1 拍以上间隔，所以这个 bug 只会在 cin=8 的诊断里出现——
**又是“按维度诊断”抓出来的**。修法是同拍写数据旁路，而不是加气泡。

第三个综合盲区：**RAM 输出寄存器必须单一源**。两次踩坑：
1. `px_mem` 写成 `wire p_word = px_mem[addr]`（continuous assign 异步读）→ 不推断；
2. 改成 `p_word_q <= px_mem[addr]` 后，又把 cin=8 的旁路写成
   `p_word_q <= bypass ? rdata : px_mem[...]` → **一个寄存器两个源**，RAM 模板再次失效，
   退化回 16,384 FF + 256:1 读 mux（第三轮的 FF 31,407 网表证据）。
正确做法：RAM 读寄存器 `p_ram_q` 只由 `px_mem` 赋值；旁路用**独立寄存器**
`bp_hit_q/bp_data_q` 存一拍，计算侧 `p_word = bp_hit_q ? bp_data_q : p_ram_q` 组合选择。
判断方法：看 `LUT as Memory` 是否随存储容量增长——不涨就是没推断成 RAM。

第二个综合盲区：**“整条移位 + 部分写”的像素缓冲**。`px_sh` 是 4×2816 位，
每拍右移 8 位，同时 fetch 又写 64 位 slice；Vivado 为每一位综合出
“移位值 / 新数据”的二选一 mux，34k LUT 里约 31k 来自这里。改成
**按 `{buffer, group}` 寻址的 LUTRAM**（读地址 = ic/8，字节选 = ic%8）后，
0 FF、~256 LUT6、0 BRAM，而且不需要环形指针——**因为计算本来就是按 ic 顺序读的**，
把“移位”换成“地址”是最省的。

### 9.1 方法：一次只放开一个变量

### 9.1 方法：一次只放开一个变量

`tools/gen_k1_debug_cases.py` 生成的 39 组，每组只测一个维度：

| 维度 | case | 通过说明 |
|---|---|---|
| bias → lane/oc/字节序 | `dbg_oc`（bias[oc]=oc，input=weight=0） | 输出必须是 0,1,…,63 |
| bias 数值/符号/bank | `dbg_biasramp`（bias=127−oc） | 输出必须是 127,126,…,64 |
| 输入像素/组地址 | `dbg_pix64`（ch0=p） | 每像素 = p |
| 写路径/饱和/漏写 | `dbg_ones`（全 127） | 全 127；任何 0 = 漏写/清零 |
| 64 个 ic 是否都进 MAC | `dbg_wsum`（cin=64 全 1） | 必须 64 |
| 每个 ic 单独 | `dbg_ic00/04/07/63`、`_c16` | 必须 1 |
| 输入组 g / 权重组 g | `dbg_grp_in0..7`、`dbg_grp_w0..7` | 必须 8 |
| cin 阶梯 | `dbg_wsum_c08..c64` | 必须 cin |
| shift | `dbg_shift` | (1+8)>>3 = 1 |

**为什么有效**：v1 第一轮错 23.3%，日志只给 22 条 mismatch；靠这套表 30 秒内
把"cin=8 全过 / cin=64 全错"→"组>2 丢失"→"两个自决定位宽"钉死，而不是猜。

### 9.2 根因：part-select 基址是自决定表达式

```verilog
w_word_q[gi][sel_a*4'd8 +: 8]   // sel_a(3b) × 4'd8(4b) ⇒ 结果 4 位
                                // 8 → 0/8 交替 ⇒ 永远只读字节 0/1
px_sh[b][f_rd*7'd64 +: 64]      // f_rd(4b) × 7'd64(7b) ⇒ 结果 7 位
                                // 64 → 0/64 交替 ⇒ 8 组只有 group6/7 落进 buffer
```

**规则**：`[expr +: W]` 里的 `expr` 没有上下文，`a*b` 的结果位宽取
**max(操作数位宽)**，不是和。修法：显式拼接 `{sel_a,3'b000}`、`{f_rd,6'b000000}`。
凡是"乘法型索引"都要这么写（本版所有 `+:` 已审计）。

### 9.3 35 例（修复前）逐条对照

| 观察 | 推论 |
|---|---|
| cin=8 的 `dbg_oc/pix64/ones/zeros/biasramp/shift` 全 PASS | bias/地址/写路径/移位都正确 |
| `dbg_wsum`（cin=64 全 1）期望 64、实际 **16** | 只有 2 组 × 8 ic 进了累加 |
| `dbg_grp_in6/in7` PASS、`in0..5` FAIL | 只有 group6/7 真正落进 buffer（f_rd 截断） |
| `dbg_grp_w0/w1` PASS、`w2..7` FAIL | 输入 p 只有 ic0..15 非零，所以只有权重组 0/1 能贡献 |
| `dbg_wsum_c16` PASS、`c24/c32/c48/c64` FAIL | 2 组以内偏移不越界，第 3 组开始回卷 |
| `dbg_ic04/07/15_c16` FAIL、`ic00_c16` PASS | 权重字节选择只到 0/1（sel_a 截断） |
| 黄金 9,531/40,960 错 | host 端把两个 bug 一起重建：**9,491/40,960 = 23.17%**，逐位置吻合 |

### 9.4 修复后

主控实测：**39/39 诊断 + 40960/40960 黄金 + 82,272 拍**。v2 在此基础上只改了
"通道上界 + 多 pass"，计算/收尾/输入通路不变，所以 v2 的回归首要看
"39 例 + 黄金是否仍全绿"，再看 9 组真实形状。

---

## 10. 诊断 / 真实形状生成器

`rtl_hand/tools/gen_k1_debug_cases.py`（自包含，唯一算术来源是
`build/golden_driver` → `conv_ref_rt.cpp`）：

```bash
cd pynq_specialized_code/pl/rtl_hand
python3 tools/gen_k1_debug_cases.py --mode all \
        --out /tmp/k1_dbg --real-out /tmp/k1_real \
        --relu-out /tmp/k1_relu0
# 产出 /tmp/k1_dbg/{dbg_*,manifest.txt}、/tmp/k1_real/{k1_*,manifest.txt}
#     以及 /tmp/k1_relu0/{k1_relu0_*,manifest.txt}（relu=0，3 组真实 conv_proj 形状）
```
`--mode relu0` 单独生成 relu=0 的 3 组：`k1_relu0_160x64`、`k1_relu0_224x96`、
`k1_relu0_64x48`（meta 里 `"relu":0`，期望按 `clamp(v>>shift,-128,127)` 算）。

k=3/k=5 可以照抄这个结构：`dbg_*` 每个 case 只放开一个维度（核 tap 位置、
cin/cout 阶梯、bank、字节序、组号……），失败时按表逐条排除。

---

## 11. 存疑 / 待确认

1. **v2 实拍数**：6 个真实形状 + 3 个边界全部实测，偏差 ≤0.3%（§4.1）。
   计算式 `P·B·cin + 装载/排空` 已被验证，可直接外推全幅 320×320（§4.2）。
2. **权重 RAM 的综合归属**：44×64 LUTRAM ×32。若 Vivado 忽略 `ram_style` 走 BRAM，
   BRAM18 会从 0 涨到 32（超红线 30）。这是 v2 最大的综合风险。
3. **Fmax**：未综合；v1/v2 的计算通路相同，Fmax 差异只来自更宽的 LUTRAM 地址
   （6 位 vs 3 位）和更大的 CIN 循环计数，影响应很小（**推定**）。
4. **全网 7.1×**：是"所有 132 层都路由到本 IP + 100% 计算利用率"的口径；
   PS 侧路由、填充、输入/输出 DDR 带宽还没接，不能当端到端承诺。
5. **cout=16 的利用率**：16/32 车道，已量化（§1），下一版可用"2 像素共享 32 车道"翻倍。

---

## 12. 与契约的偏差清单

| 项 | 偏差 | 理由 |
|---|---|---|
| 运行期形状 | 实现为运行期 `ctrl_cin/cout/h/w`，编译期参数是上界 | 契约 §3 允许；PS 按层选 IP |
| RELU | 新增运行期输入 `ctrl_relu`（契约 §3 原表没有此端口） | 24 个 conv_proj 是 relu=False，必须运行期可配；参数 `RELU` 退化为 wrapper 复位默认值 |
| 输出写 | 单拍 8 B（AWLEN=0），不是 burst | 第一版优先正确性；带宽已重叠，见 §8.5 |
| 权重读 | 只支持 `cin%8==0` | 64-bit 整字装；全网络 k=1 层满足 |
| 权重 ping-pong | v2 改为单 bank + pass 间重装 | 装载只占 pass 的 0.004%；v1 双 bank 的收益同量级，简化后省一半权重 LUTRAM |
| 未做 | 综合、上板、git commit | 任务书明确不做 |

---

## 13. 文件 / 变更记录

```
pynq_specialized_code/pl/rtl_hand/modules/conv_k1_engine.v   # v2
pynq_specialized_code/pl/rtl_hand/tb/tb_conv_k1_engine.v     # 39 行模板
pynq_specialized_code/pl/rtl_hand/tools/gen_k1_debug_cases.py
pynq_specialized_code/pl/rtl_hand/modules/k1_axi_wrap.v      # AXI-Lite 包装（§14）
docs/rtl_hand/conv_k1_engine.md                              # 本文件
```

| 时间 | 变更 |
|---|---|
| 20:14 | 按主控规则停止自跑 xsim；TB 改为 39 行共享设施模板 |
| 20:22 | `wl_lane_lim`：`cout<32` 只装实际存在的 oc |
| 20:33 | v1 第一轮：82,272 拍 / 0.155×（过）；9,531 字节错 |
| 20:36 | 修 `wl_din` 32→64 位 |
| 20:37 | 修块边界 `last_a` 不清 0（潜伏假快照） |
| 20:41 | 13 例诊断：cin=8 全过 / cin=64 全错；all-ones→16、单点 ic→0 |
| 20:42 | **修 part-select 自决定位宽**：`{sel_a,3'b0}` / `{f_rd,6'b0}`；host 模型 9,491≈实测 9,531 |
| 20:44 | 主控：39/39 诊断 + 40960/40960 黄金 + 82,272 拍 **PASS** |
| 20:47 | v2：CIN_MAX=352 / COUT_MAX=192 / 6-pass 重装；真实形状 golden 生成 |
| 20:52 | v2 compile-only 通过 |
| 20:51 | **v2 实测：39/39 + 40960/40960 + 82,561 拍；9/9 真实形状 440320/440320** ✅ |
| 20:55 | **200 组 fuzz：200/200，8192000/8192000 字节，0 越界 / 0 unknown** ✅ |
| 20:58 | 首轮综合：BRAM **0** ✅、LUT 24,530、FF 39,662、DSP 1（多驱动 + 未进 DSP）|
| 20:59 | 修多驱动（px_sh/pix_ready 单 always）、加 `use_dsp` 组合乘；`xvlog -sv` 0 warning |
| 21:0x | 复测：CRITICAL 0、FF 28,420、DSP 33、BRAM 0，但 LUT 34,057（px_sh 移位 mux）|
| 21:1x | **像素缓冲改 `{buffer,group}` LUTRAM**（0 FF/256 LUT/0 BRAM）；收尾 3→2 级、push 数据组合化；预期 FF ≈13.4k、LUT ≤12k |
| 21:10 | 第二轮功能回归：39/39 + 40960/40960 + **82,559 拍** PASS |
| 21:1x | 第三轮综合：LUT 23,493、FF 31,349、DSP 33、BRAM 0、Fmax 162；异步读未推断成 RAM |
| 21:2x | **像素 RAM 改同步读+预取 `p_word_q`**；lane 加 `keep_hierarchy`；预期 FF≈15k、LUT≈7–8k |
| 21:17 | 39 例抓出 **cin=8 单 beat 同拍写读**边界（dbg_oc 31/64、dbg_pix64 32/1024）；加 group0 旁路修复 |
| 21:52 | **RELU 运行期化**：新增 `ctrl_relu`；TB 由 meta 的 `relu` 字段驱动；生成 3 组 relu=0 真实 conv_proj 黄金；覆盖率 132/132 |
| 21:50 | relu=1 回归（39/黄金/9 形状）全过；**relu=0 3/3 全过**（133120/133120）|

---

## 14. AXI-Lite 包装（`modules/k1_axi_wrap.v`）

目的：给 BD/PS 一个标准 AXI4-Lite 从机，**`conv_engine.py` 的寄存器偏移零改动**。

HLS 引擎其实有**两个** AXI-Lite bundle（PS 用两个基地址）：
`ctrl`（0x18..0x70 + AP_CTRL 0x00 / AP_RETURN 0x10）和默认 `s_axi_control`
（四个 m_axi 基地址 0x10/0x1C/0x28/0x34）。两者的 0x28 分别是 Hout 和 bias 基址，
所以一个 64KB 窗口无法同时复现。本 wrapper 用**一个 AXI4-Lite 从机 + 128KB 窗口**
（**BD 里必须映射成 128KB @ 0x4000_0000**），`A[16]` 选页：

| A[16] | 页 | PS 基址 | 寄存器 |
|---|---|---|---|
| 1 | ctrl | 0x4001_0000 | AP_CTRL 0x00、AP_RETURN 0x10、H 0x18、W 0x20、HOUT 0x28、WOUT 0x30、CIN 0x38、COUT 0x40、KSIZE 0x48、STRIDE 0x50、DIL 0x58、PAD 0x60、SHIFT 0x68、RELU 0x70、**STATUS 0x94** |
| 0 | offs | 0x4000_0000 | IN 0x10/0x14、WGT 0x1C/0x20、BIAS 0x28/0x2C、OUT 0x34/0x38（64 位，低+高） |

- **AP_CTRL**：写 bit0=start（自清）；读 [0]=0、[1]=done、[2]=idle、[3]=ready。
- **AP_RETURN**：0 成功；1 `ERR_NULL`、2 `ERR_SIZE`、3 `ERR_SHIFT`（与 HLS 一致）。
- **STATUS (0x94)**：`[0]done [1]busy [2]err [7:4]err_code [8]idle`。
- **start 校验**：k=1/s=1/d=1/p=0、`HOUT==H`、`WOUT==W`、cin 8..352 且 `%8==0`、
  cout 1..192、shift 1..31、四个地址非 0 且高 32 位为 0。失败**不启动引擎**，
  但置 done 并返回错误码，PS 不会超时。
- 本构建 `RELU=1` 是编译期参数；RELU 寄存器只做回写显示，写 0 不改变硬件。
  C8 声明位 bit2/bit3 不参与逻辑（C8-only）。
- 资源瘦身后对外只剩 **2 个 AXI4 master**：`m_axi_rd`（in/w/b 内部仲裁，2-bit tag）
  + `m_axi_out`；详见 §15。
- `xvlog -sv`（不带 `--relax`）0 warning / 0 error。

---

## 15. 资源瘦身（2026-10-26）：权重进 BRAM + 三读口合并

> 触发：BD 综合 `k1_wrap` = **20,389 LUT**（17,293 logic + 3,096 LUTRAM）、FF 15,746、
> DSP 33、BRAM 0；`smc`（6 个 SI）= 7,876 LUT；合计 **55,390** > 器件 46,200（xc7z015）。
> 本节的两个改动**只动 RTL**，未综合、未仿真（由主控统一跑）。

### 15.1 任务 1：权重存储 LUTRAM → BRAM

#### 15.1.1 为什么不能只把 `ram_style` 改成 `"block"`

旧结构是**每个 lane 一块 44×64 的分布式 RAM**（`g_lane[*].wmem`，synth 里落成
64×64、22 个 RAM64M/lane）。若只改属性，Vivado 会得到 32 块 512×72 的 RAMB36
（每 lane 一块，64-bit 读口至少要 2 个 36-bit 组），即 **+32 Block RAM Tile**；
而芯片上只剩 **8.5 个 Tile**（190 BRAM18 里用了 173）。所以必须换**数据布局**，
不是换属性。

#### 15.1.2 新布局：一块 256-bit × 352 的 SDP BRAM

```
w_mem[ic][lane*8 +: 8] = weight[oc = pass*32+lane][ic]     // 行 = 一个 ic
行宽 WW = LANES*8 = 256 bit；深度 CIN_MAX = 352；总容量 90,112 bit
```

- 所有 lane 在同一个 `ic` 上**同址**读，天然拼成一行；旧布局那 32 个独立读口
  其实是同一个地址，压成一行后只剩 **1 个 256-bit 读口**。
- Vivado 的映射：RAMB36 的 SDP 512×72 里，每 72 bit = 8 个 9-bit 组，
  每个组放 1 个 lane 的 8 bit（空 1 bit）；256 bit 需要 **4 块 RAMB36**
  = **4 Block RAM Tile（8 个 RAMB18）**，深度 352 ≤ 512。
- 写入用**字节使能**：`w_ld_be[wl_lane]` one-hot，每拍只写 1 个 lane 的 1 字节。

RTL（`conv_k1_engine.v` §6）：

```verilog
(* ram_style = "block" *) reg [WW-1:0] w_mem [0:CIN_MAX-1];

// 写口：每拍 1 字节；32 个常量 bit-slice 的 if ⇒ 字节写使能
always_ff @(posedge ap_clk) begin
    if (w_ld_we)
        for (int c = 0; c < LANES; c = c + 1)
            if (w_ld_be[c])
                w_mem[w_ld_addr][c*8 +: 8] <= w_ld_data;
end

// 读口：w_row_q 只有一个赋值源（不能复位/旁路/第二源）
always_ff @(posedge ap_clk) begin
    if (issue_en)
        w_row_q <= w_mem[w_rd_addr];
end
```

**RAM 模板自查**（任务书点名的坑）：
- `w_row_q` 只有上面这一条赋值，没有复位、没有同拍旁路；写口在另一个 always。
- 读地址 `w_rd_addr = row_base_r + ic_cur` 是寄存器/计数器的组合，不带复位。
- 写口没有第二地址，32 个 if 共用 `w_ld_addr`，是标准“宽字 + 字节使能”模板。
- `px_mem` 的模板一个字没动（仍是 `ram_style="distributed"` 的 4×44×64 LUTRAM）。

#### 15.1.3 读延迟怎么补偿（**关键：不需要补拍**）

先把旧时序写清楚：旧版虽然是 LUTRAM，但读**已经是寄存器输出**：

```verilog
if (issue_en) w_word_q[gi] <= wmem[ic_cur[ICW-1:3]];   // 旧：1 拍
```

所以旧版 `w_word_q` 在 issue 拍的**下一拍**才有效，配合同一拍寄存的 `p_a`
（`p_a <= p_byte`）和 `sel_a`（`<= ic_cur[2:0]`）做乘法。**LUTRAM 的异步读特性
根本没有被用到**——读延迟本来就是 1 拍。

新版 BRAM 同步读也是 1 拍，且地址在**同一个 issue 拍**给出：

| 拍 | 旧（32×64 LUTRAM） | 新（256-bit BRAM） |
|---|---|---|
| t（issue） | `w_word_q[gi] <= wmem[ic_cur>>3]`，`p_a <= p_byte`，`sel_a <= ic_cur[2:0]` | `w_row_q <= w_mem[row_base+ic_cur]`，`p_a <= p_byte` |
| t+1 | `p_a * w_word_q[gi][{sel_a,3'b0}+:8]` → ic 的权重字节 | `p_a * w_row_q[gi*8+:8]` → **同一个 ic 的权重字节** |
| t+2 | `prod_b` 进累加 | 同左 |

结论：**读延迟没变（都是 1 拍）、乘法的两个操作数仍在同一拍对齐、II=1 不变**。
不需要“提前一拍给地址”，也没有加流水级。唯一的变化是去掉了每 lane 的 8:1 字节
mux（`sel_a` 那级），权重字节直接是 `w_row_q` 的常量切片。

> 再强调一次：`p_word_q`/`bp_hit_q`（像素侧）与权重读**没有任何关系**，这次没动；
> 它们仍然是“issue 拍读、下一拍用”的同一套对齐。

#### 15.1.4 装载：1 字节/拍 + “行槽”后台预装

写口 1 字节/拍 ⇒ 一个 64-bit beat 要 8 拍（原来是 1 拍）。一个 pass 的装载量
`32·cin` 字节，45nm 下就是 `32·cin` 拍。为了不把它加到关键路径上：

- 一个 pass 占 `cin` 行；下一 pass 装到 `[row_base+cin, row_base+2·cin)`。
  只要 `2·cin ≤ CIN_MAX(352)`（即 **cin ≤ 176**），两块行槽不重叠 ⇒
  **本 pass 计算期间 loader 在后台写下一槽**，pass 结束时早写完了。
- `pass_go`（本 pass 启动）就发下一 pass 的装载请求；`row_base_r` 在
  ST_PASS→ST_NEXT 时切到下一槽。代码：

```verilog
// §2：行槽够不够
wire [ICW:0] row_base_d   = {1'b0, row_base_r} + {1'b0, cin_r};
wire         preload_ok   = (row_base_d + {1'b0, cin_r}) <= CIN_MAX;
wire [ICW-1:0] next_row_base = preload_ok ? row_base_d[ICW-1:0] : 0;

// pass_go 当拍：预装下一 pass
if (pass_go && ((blk_r + 1) < blk_cnt_r) && preload_ok) begin
    wl_req_pulse <= 1'b1;
    wl_req_blk   <= blk_r + 1;
    wl_req_row   <= next_row_base;
end
```

- `cin > 176`（352/cin < 2，塞不下两个行槽）时 `preload_ok=0`：退回原来的
  “pass 算完再串行装”。这时才多花装载时间；H=2 的真实形状约 +3~4%，
  全幅 320×320 可忽略（装载/计算 = 32/(H·W)）。
- **与输入预取的带宽协调**：后台装载只在 `in_lead = f_next - pix_cur ≥ 2`
  （输入已领先 2 像素）时才发 AR；且 `wl_ar_seen` 把 ARVALID 锁住到握手
  （AXI 要求 valid 不得中途撤销）。配合 `k1_axi_wrap` 里“输入最高优先级”，
  权重 burst 不会把 4 像素输入缓冲饿死。

装载 FSM 的关键点（`WL_IDLE/AR/R/WR`）：收一个 beat 后连写 8 拍；第 8 拍
（`wl_bcnt==7`）若还有下一 beat，就在**同一拍**收下（`rready` 只在 R 和
WR 最后一拍为高），所以是 8 拍/beat 而不是 9。每 lane 最后一个 beat 由
`wl_last_q` 记住，写完 8 字节才切下一 lane。

一个 32-lane pass 的装载拍数（对 TB 模型实测）：`lanes·(cin+2)`：

| cin | 8 | 16 | 64 | 160 | 224 | 352 |
|---|---:|---:|---:|---:|---:|---:|
| 装载拍（32 lane） | 320 | 576 | 2,112 | 5,184 | 7,232 | 11,328 |

#### 15.1.5 拍数影响（黄金口径）

`k1_64x64`（cin=64，2 pass，80,920 计算拍 + 原 639 装载/启动）：

- 初始装载从 `32·10 = 320` 变成 `32·66 = 2,112`（+1,792）；
- 第 2 个 pass 的行槽够（2×64=128 ≤ 352）⇒ **完全藏在 pass 0 的 40,960 拍里**，
  原来的 320 拍装载也省掉；
- 预测总拍 ≈ 81,920 + 2,112 + 32(bias) − 1 ≈ **84,06x**（+1.8%），
  在 `+2%`（≤84,210）以内。**以主控实测为准**。
- 其它形状：cin=64/160 的行槽 ≥2，装载基本藏住；cin=208/224/256/352 一个行槽
  ⇒ 每次 pass 重装暴露，H=2 形状 +3~4%，但只影响 HLS 基线 1.2× 的宽松口径。

### 15.2 任务 2：三路读 master → 1 路（`m_axi_rd`）+ 输出写不变

`k1_axi_wrap.v` 内部新增仲裁器，外部端口从 4 个 master 变 2 个：

| 旧 | 新 |
|---|---|
| `m_axi_in_*` / `m_axi_w_*` / `m_axi_b_*` / `m_axi_out_*` | **`m_axi_rd_*`（合并读，64-bit，2-bit ARID）** + `m_axi_out_*`（不变） |

引擎侧三个读接口（`m_axi_in/w/b_*`）**一个端口都没改**，只是接到内部仲裁器。
BD 的 `smc` SI 从 6 个降到 **4 个**（S00/S01 原有 + S02=rd + S03=out）。

#### 15.2.1 授权保持（burst 内绝不换人）

```verilog
localparam logic [1:0] TAG_IN = 2'd0, TAG_W = 2'd1, TAG_B = 2'd2;
logic [1:0] rd_gnt;    // 当前 burst 的主人（锁存）
logic       rd_ar_ph;  // 1=等 AR 握手，0=R 数据阶段
logic       rd_busy;   // 1=有 burst 在飞（一直保持到 RLAST 握手）

always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
  if (!s_axi_aresetn) begin rd_gnt<=TAG_IN; rd_ar_ph<=0; rd_busy<=0; end
  else if (!rd_busy) begin                 // 只在空闲时选人
      if      (in_arvalid) begin rd_gnt<=TAG_IN; rd_ar_ph<=1; rd_busy<=1; end
      else if (b_arvalid)  begin rd_gnt<=TAG_B;  rd_ar_ph<=1; rd_busy<=1; end
      else if (w_arvalid)  begin rd_gnt<=TAG_W;  rd_ar_ph<=1; rd_busy<=1; end
  end else if (rd_ar_ph) begin             // AR 握手 → 进数据阶段
      if (m_axi_rd_arvalid && m_axi_rd_arready) rd_ar_ph<=1'b0;
  end else begin                           // R 数据阶段：只有 RLAST 才放授权
      if (m_axi_rd_rvalid && m_axi_rd_rlast && m_axi_rd_rready) rd_busy<=1'b0;
  end
end

// AR 地址/长度/ID 全由同一个 rd_gnt 一起选，burst 中途不会变
assign m_axi_rd_araddr = (rd_gnt==TAG_IN) ? in_araddr :
                         (rd_gnt==TAG_B)  ? b_araddr  : w_araddr;
assign m_axi_rd_arlen  = ...同左...;
assign m_axi_rd_arid   = rd_gnt;            // 2-bit tag: 0=in 1=w 2=b
```

- **一个 burst 内不切换**：`rd_gnt` 只有 `!rd_busy` 时才更新；`rd_busy` 从 AR 握手
  一直保持到 `RVALID && RLAST && RREADY`。所以同一 burst 的所有 R beat 只会回给
  同一个通道，ARADDR/ARLEN/ARID 也不会在 burst 中途变。
- **R 回路由**：按锁存的 `rd_gnt` 选 `in/w/b` 的 `rdata/rlast/rvalid`，
  `m_axi_rd_rready` 选对应通道的 `rready`；另外两路 `rvalid=0`。
- **tag**：外部 ARID = grant；从机回来的 RID 不做路由（单 burst 在飞，
  grant 就是唯一 owner），但仿真期会检查 `RID === rd_gnt`，从机回错会打印 ERROR。

#### 15.2.2 优先级：`in > b > w`（与任务书建议相反，有理由）

任务书建议“权重 > 偏置 > 输入”，前提是“权重装载是启动阻塞点”。改成**行槽后台
预装**后，这个前提不成立了：

- 启动阶段（ST_LOAD）输入预取**还没开始**，权重自然独占读口——不需要优先级帮忙；
- 计算阶段（ST_PASS）权重是后台任务，一条 8-beat burst 因 1 字节/拍写口会占住
  读口 **64 拍**；若权重优先，它会连续 32 条 burst 占满 ~2k 拍，把输入 4 像素
  缓冲（≈256 拍）饿死 ⇒ 计算停顿，反而多花拍数；
- 输入是 1-beat burst（ARLEN=0），让它最高只会挡权重 8~16 拍，权重有整个 pass
  的 4 万拍可用；
- 偏置只在 start 后装一次（32 拍），那时输入还没开始，放中间无影响。

#### 15.2.3 BD 适配层 / tcl

- `k1_axi_wrap_bd.v`（仍是 **Verilog-2001**）：只留下 `s_axi` / `m_axi_rd` /
  `m_axi_out` 三个 interface。`m_axi_rd` 的 `X_INTERFACE_PARAMETER` 里
  **ID_WIDTH 2**（与内部 2-bit tag 对齐），`m_axi_out` 保持 ID_WIDTH 1。
  `m_axi_rd` 的写通道恒空闲、`m_axi_out` 的读通道恒空闲（保持完整 AXI4 外形，
  SmartConnect 不用为 SI 换配置）。
- `scripts/bd_add_k1.tcl`：`SMC_N 6→4`，配对改
  `m_axi_rd→S02_AXI / m_axi_out→S03_AXI`，接口检查/FREQ_HZ/地址映射/汇总同步改；
  若旧 BD 是 6 个 SI，脚本会先删掉旧 `k1_wrap` 再缩 `NUM_SI`（旧 cell 本来就要重建）。

### 15.3 预期资源变化（**未综合，静态估算**）

综合前的基线（主控 BD 实测，`design_1_k1_wrap_0_synth_1`）：

| 项 | 值 | 说明 |
|---|---:|---|
| Slice LUT | **20,390** | 17,294 logic + 3,096 LUTRAM |
| LUTRAM 明细 | 3,096 | 权重 `wmem` = **2,816**（704 RAM64M×4），像素 `px_mem` = 280 |
| FF | 15,746 | 其中 `w_word_q` 32×64 = 2,048 |
| DSP / BRAM | 33 / 0 | |

任务 1 的增减（逐项）：

| 项 | Δ LUT | Δ FF | Δ BRAM |
|---|---:|---:|---:|
| 权重 LUTRAM `wmem` 移除 | **−2,816** | — | — |
| 每 lane 8:1 权重字节 mux 移除（`mul_prod_i_*` 实测 512 个 LUT cell） | **−512** | — | — |
| `w_word_q`(32×64) → `w_row_q`(256) | — | **−1,792** | — |
| `sel_a` 移除 | — | −3 | — |
| 字节写 FSM/地址/one-hot/beat 寄存器 | ≈ +80 | +80 | — |
| 权重 RAM | — | — | **+4 Tile（+8 RAMB18）** |

任务 2 的增减：wrapper 里仲裁器 ≈ **+80 LUT / +20 FF**；BD 的 `smc` SI 6→4，
按其“每端口 2,000–2,500 LUT”口径 **−4,000~−5,000 LUT**（这是 smc 的数字，
不算在 k1_wrap 里）。

**预期** `k1_wrap` ≈ 20,390 − 2,816 − 512 + 80 + 80 ≈ **17.2k LUT**
（BRAM 4 Tile、FF ≈14.05k）；`smc` ≈ 2.9–3.9k。合计 ≈ **47.2–48.2k**。

> ⚠ **诚实结论：这两个任务达不到 ≤15,000。** 权重路径总共只有 3,328 LUT
> （2,816 LUTRAM + 512 mux），而目标要减 5,389。差 ≈2.2k 的硬缺口，
> `smc` 的节省不改变 `k1_wrap` 的计数。**不要为了凑 15k 把 BRAM 再拆回 LUTRAM。**
>
> 下一刀应该砍 `k1_wrap` 里最大的非权重项（对旧网表按信号名统计的 LUT cell）：
>
> | 组 | LUT cell | 内容 |
> |---|---:|---|
> | `ob_*` | **4,190** | 输出块队列：4 槽 × 4 组 × (32b 地址 + 64b 数据) FF 阵列，4 写口 mux + 16:1 读 mux |
> | `v32` | 3,492 | E2 的 32b 加 + bias 6:1 mux |
> | `bias_lane` | 3,288 | 6 个 pass 的 bias FF（6,144 FF）+ 6:1 mux |
> | `sum27` | 2,535 | 27b 三加 |
> | `acc_e/acc_o` | 3,409 | 两条 26b 奇偶链 |
>
> 最划算的一刀：输出队列改成 **16×96 的简单双口 LUTRAM**（push 的 4 组本来
> 就每 `cin` 拍才来一次，可以串行 4 拍写进去；队列有 4×cin 拍余量），
> 预计 **−3.5~−4.0k LUT、−1.1k FF**（只 +~100 LUTRAM）。加上它 `k1_wrap`
> 可到 **13.2–13.7k**，配合 smc 4 SI 总量约 **43.5–44.5k**，才真正进 46,200。
> 但这是**第三个任务**，本次按任务书只做前两个，没有动输出队列。

### 15.4 自查记录（本地已做）

- `xvlog --sv`：`conv_k1_engine.v`、`k1_axi_wrap.v` 0 error / 0 warning；
- `xvlog`（Verilog-2001）：`k1_axi_wrap_bd.v` 0 error / 0 warning；
- `xelab k1_axi_wrap_bd` 静态展开通过（端口名/位宽/例化一致）；
- **装载器逐拍模型**（Python，复刻 FSM + TB 从机）：cin=8/16/64/160/224/352
  全部 `w_mem[ic][lane] == weight[oc][ic]`，0 错；拍数与上表一致；
- RAM 模板自查：`w_row_q` 单赋值源、写/读分 always、无复位/旁路；
- **未做**：xsim、综合、上板、git commit（按要求由主控统一跑）。

### 15.5 主控执行清单（验证 + 综合）

```bash
# ---------- 0. 环境 ----------
cd pynq_specialized_code/pl
source /data/xilinx/2025.2/2025.2.1/Vivado/settings64.sh     # 本机路径，按实际

# ---------- 1. 只做语法/展开检查（不跑 xsim）----------
xvlog --sv -i rtl_hand/modules \
      rtl_hand/modules/conv_k1_engine.v rtl_hand/modules/k1_axi_wrap.v
xvlog       -i rtl_hand/modules rtl_hand/modules/k1_axi_wrap_bd.v   # 必须 Verilog-2001

# ---------- 2. 单模块验收（既有 5 套，全部用 conv_k1_engine）----------
cd rtl_hand
# 若 /tmp 的 manifest 已丢，先重建（golden_driver 需已存在）：
#   python3 tools/gen_k1_debug_cases.py --mode all --out /tmp/k1_dbg --real-out /tmp/k1_real
bash tools/run_module.sh conv_k1_engine k1_64x64 --fuzz-manifest /tmp/k1_dbg/manifest.txt
bash tools/run_module.sh conv_k1_engine k1_64x64                                  # 黄金
bash tools/run_module.sh conv_k1_engine k1_64x64 --fuzz-manifest /tmp/k1_real/manifest.txt
bash tools/run_module.sh conv_k1_engine k1_64x64 --fuzz-manifest /tmp/k1_relu0/manifest.txt
bash tools/run_module.sh conv_k1_engine k1_64x64 --fuzz 200

# 拍数硬口径：黄金报告 cycles ≤ 84,210（82,559 × 1.02），预期 ≈84.0–84.1k
grep -h "cycles=" reports/conv_k1_engine_*.md | tail -5

# ---------- 3. BD 重建 + 综合（在 pl/ 下）----------
cd ..
vivado -mode batch -source scripts/bd_add_k1.tcl      # 末尾必须打印 BD_INTEGRATION_OK
# 然后按既有流程综合/实现；重点看两行：
#   k1_wrap : LUT ≈17.2k、LUTRAM ≈280、FF ≈14.05k、BRAM36 = 4
#   smc     : LUT 应比 6-SI 版本少 ~4–5k
# 若 Vivado 报 w_mem 没推断成 BRAM（LUTRAM 仍 ~2.8k），先查“RAM 输出寄存器多源”
# 和写口是否被拆成多地址；模板见 §15.1.2。
```
