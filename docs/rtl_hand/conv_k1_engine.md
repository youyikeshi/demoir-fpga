# conv_k1_engine · 手写 1×1 卷积加速器（k=1, s=1, pad=0, d=1）

> 交付物：`pynq_specialized_code/pl/rtl_hand/modules/conv_k1_engine.v`
> testbench：`pynq_specialized_code/pl/rtl_hand/tb/tb_conv_k1_engine.v`（39 行共享设施模板）
> 诊断生成器：`pynq_specialized_code/pl/rtl_hand/tools/gen_k1_debug_cases.py`
> 契约：`docs/rtl_hand/00_SPEC_接口契约.md` §3
> 依据：`docs/rtl_map/04_mac_array.md`（k=1 现状 16 车道 + 21 拍收尾）、
> `docs/rtl_map/03_weight_path.md`（每 (y,列块,ocb) 重装 = 237 MB/全网）
> 最后更新：2026-10-23 21:52（**未综合、未上板；xsim 由主控跑**）

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
- 四个 AXI4 master 原样引到 wrapper 顶层，BD 直接连 DDR。
- `xvlog -sv`（不带 `--relax`）0 warning / 0 error。
