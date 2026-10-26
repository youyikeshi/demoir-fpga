# HLS 移除 k=1 快路径（2026-10-26）

> **状态**：源码已改，`g++` 语法检查 + 功能回归（23 形状 × 2 构建）已在本机通过。
> **没有跑** `vitis_hls` / csynth / cosim，**没有**综合/实现/出 bit，**没有** `git commit`
> —— 按要求留给主控统一跑。
>
> **目标**：HLS 里的 k=1 通路已被手写 RTL `conv_k1_engine`（BD 里的 `k1_wrap`）
> 完全替代（132/132 层、逐字节一致、6.5×），继续综合它纯粹是浪费 LUT。
> 本次把它从综合范围里拿掉，并且**明确失去 k=1 能力**。

---

## 0. 一句话结论

`conv2d_engine.h` 新增编译期开关 **`K1_IN_HLS`，默认 0**：

| `K1_IN_HLS` | 行为 |
|---|---|
| **0（本次默认）** | k=1 的 16 车道阵列 / 累加器 / 权重 LUTRAM / 权重装载 / 收尾 / 第二段写回全部被预处理掉（**综合零资源**）；函数入口对 `ksize == 1` 返回 **5 = `ERR_K1_UNSUPPORTED`**（报错，不是算错） |
| **1（一键恢复）** | 逐字回到 2026-10-23 第十轮的 16 车道版本（`K1_LANES=16` / `K1_DSP_LANES=8`），语义、资源与改造前相同 |

预估（csynth 报告口径）：**LUT −4.8k、FF −2.3k、DSP −8、BRAM 0**。
按 Vivado 综合报告校准后，BD 上实际大约 **−3.6k LUT**；**仅靠这一刀 BD 仍会 ~50.6k–51.8k > 46,200，布局大概率还是过不了**，见 §5.3。

---

## 1. ⚠️ 部署前提：PS 必须把所有 k=1 层路由到新 IP

这次改动**永久关掉了"HLS 兜底"那条路**。以下三条必须在烧新 bit 之前满足：

1. **`USE_K1_IP=1` 从"可选"变成"必须"**。
   `runner.py` 的现状是：`USE_K1_IP` 默认 **关**，路由不合格只打 `WARN` 然后回退老 HLS 引擎。
   k=1 分支删掉后，回退 = 引擎返回 5 → `RuntimeError: PL 引擎返回错误码 5`，整网停下。
   **这是刻意设计**：宁可整网停，也不能让 k=1 掉进 k=3/k=5 的通用通路（25 抽头谓词循环
   只算到 `kx=ky=0`，输出"看着正常"但整网精度崩，是最难查的一类故障）。
2. **上板前先跑离线预演**：`python3 pynq_specialized_code/ps/tools/test_k1_route.py`
   （用真实 manifest + 权重包统计哪些层会走 K1，不需要板子）。
   必须确认 **132/132 个 k=1 层全部 eligible**。任何一层不合格（`cin%8!=0`、
   `cout>cout_max`、`relu=0` 且 RTL 不支持、非 C8、带 `out_lut`/`chsum` 融合、
   `s!=1`/`d!=1`/`pad!=0`、`HOUT!=H`/`WOUT!=W` …）都会在运行期变成"返回码 5"。
3. **建议先在旧 bit 上做一次"纯路由演练"**：旧 bit 的 HLS 侧还有 k=1 通路，
   即使某层没被 K1 接住也能算完、只是慢。用 `USE_K1_IP=1` 跑完整网，
   检查 `k1_calls == 132`、`k1_skip_reasons` 为空，**确认路由真的 100% 之后**再换新 bit。
   反过来做（先烧新 bit 再查路由）会得到一堆返回码 5。

> 如果网络里存在"K1 IP 目前做不了"的 k=1 层（比如某个非 C8 的 1×1 层），
> 必须在换 bit 之前先扩 K1 IP，或者继续用 `K1_IN_HLS=1` 的构建（§3）。

---

## 2. 改了哪些块

改动全部是 **`#if K1_IN_HLS` / `#else` / `#endif` 包裹**，k=3 / k=5 / dilation 三个方向
的代码**一行没动**（不是"改完再对照"，是字面上没进过 diff）。

### 2.1 `pynq_specialized_code/pl/src/conv2d_engine.h`

| 改前行 | 改后行 | 内容 | 理由 |
|---|---|---|---|
| 212–237 | 212–265 | 新增 **`K1_IN_HLS` 开关**（默认 0）+ 说明；`K1_LANES` / `K1_DSP_LANES` 及其 `#error` 校验整体收进 `#if K1_IN_HLS` | 关掉时这两个宏**不再定义**：任何残留引用会直接编译失败，而不是悄悄取默认值 |

`K1_WIDE_COLS` **没动**（CHW 构建里它仍是 1）。`K1_IN_HLS=0` 时 `wide1` 被写成常量
`false`，所以它自动失效，不需要额外改。

### 2.2 `pynq_specialized_code/pl/src/conv2d_accel.h`

| 改前行 | 改后行 | 内容 | 理由 |
|---|---|---|---|
| 66–74 | 66–80 | 错误码枚举新增 **`ERR_K1_UNSUPPORTED = 5`** | 只加一个枚举值：**不新增/不移动任何 AXI-Lite 寄存器**，返回码仍在同一个 32 位 `ap_return` 里 |

### 2.3 `pynq_specialized_code/pl/src/conv2d_engine.cpp`

行号为**改动前 → 改动后**（当前文件 1076 行，原 996 行；净增 80 行几乎全是注释和 `#if`/`#else`/`#endif`）。

| # | 改前行号 | 改后行号 | 块 | 处理 |
|---:|---|---|---|---|
| 1 | 41–45 | 41–53 | `K1_BLK` 定义 | `#if K1_IN_HLS` 保留原式；`#else` 固定 `K1_BLK 1`（只剩 wbuf / csum_l 的数组上界作用） |
| 2 | 136 之后 | 143–154 | **入口参数校验** | 新增 `#if !K1_IN_HLS if (ksize == 1) return ERR_K1_UNSUPPORTED;` —— 在 same-padding 检查**之前**，保证 k=1 一定拿到 5 而不是 2 |
| 3 | 167 | 185–192 | `wide1`（448 列宽列块） | 关掉时恒 `false` ⇒ `bw`/`bwcol` 和 line_buf 填充里的 `>>6`/`&63` 分支常量折叠（CHW 构建有收益；C8 构建本来就把 `K1_WIDE_COLS` 强制成 0，无变化） |
| 4 | 174–178、203–204 | 199–206、231–234 | **`wgt1_buf[16][352]`（16 bank LUTRAM）+ 它的两条 pragma** | 整体 `#if K1_IN_HLS`；关掉时数组不存在，`ARRAY_PARTITION`/`BIND_STORAGE` 也不会指向不存在的变量 |
| 5 | 239 | 271–275 | **`wbuf` 写回行缓冲** | `#if` 保持 `[16][448]`；`#else` 改成 `[8][64]`（k=1 的 xn 才可能到 448，其余路径 xn ≤ 64）。**k=3/k=5 的写回代码一字未动，只改数组形状** |
| 6 | 435 | 471–474 | `cpadded` | `#if C8_LAYOUT && K1_IN_HLS`：只有 k=1 的第二段 out_copy 用它 |
| 7 | 438–440 | 477–481 | `ocbl1`（本 pass 的 oc 上界） | `#if K1_IN_HLS`；关掉后不存在（FUSE_SUM 的用法在 #15 里另有分支） |
| 8 | 443 | 487–489 | `off_b` | 按需声明（`K1_IN_HLS` 或任一「逐字节基线容器」开关为真时才需要），避免无用变量 |
| 9 | 449–451 | 495–499 | k=1 奇数 OC 块 `continue` | `#if K1_IN_HLS` |
| 10 | 459–494 | 507–548 | **k=1 权重装载**（32-bit 宽读 + `wgt1_buf` 写） | `if (ksize == 1) {` 保留，body 用 `#if K1_IN_HLS` / `#else 留空注释` / `#endif` 包住 —— 这样 `else if (ksize == 3)` 链**一个字都不用挪** |
| 11 | 633–644 | 687–704 | C8 的 k=1 尾部补零（csynth 里是 `LOOP_639_28`，79 LUT / 21 FF） | 同上：`if (ksize==1) {…} else if (ocbl < OC_BLOCK) {` 的分支结构不动，只把 k=1 段 `#if` 掉，`#else` 直接进 `if (ocbl < OC_BLOCK)` |
| 12 | 668–747 | 728–813 | **k=1 十六车道计算 + 收尾**（csynth 里是 `LOOP_684_33` 和 `LOOP_730_36` 两个子模块） | `#if K1_IN_HLS` 保留 `acc0/acc1[K1_LANES]`、8 DSP + 8 LUT 乘法器、16 路并行收尾；`#else` 留空注释（入口已拒，不可达） |
| 13 | 848–859 | 914–932 | **通用分支里的死 1×1 快路径**（csynth 里是 `LOOP_849_43`，401 LUT / 304 FF） | 它本来就被 #12 截走、是死代码，但 HLS 照样综合。`#if` 掉 |
| 14 | 960–978 | 1033–1053 | **k=1 第二段 out_copy**（ocb+8..15 那 8 字节，`LOOP_967_51`） | 整体 `#if K1_IN_HLS`（关掉时 `wbuf[OC_BLOCK+i]` 会越界，必须整段去掉，不能只靠常量折叠） |
| 15 | 985 | 1060–1065 | FUSE_SUM 的通道上界 `ksize==1 ? ocbl1 : ocbl` | 关掉时直接 `i < ocbl`（`ocbl1` 不存在） |

**关于"主 FSM 里分派到 k=1 的状态"**：这份 C++ **没有手写 FSM**，状态机是 HLS 从循环生成的。
在生成 RTL（`r9_k1_l16` 构建）里，分派 k=1 的是：

- `conv2d_engine.v:15960-15966` —— **state 134** 按 `ksize` 三分支（`=5→142`、`=1→138`、否则 `→135`）
- **state 138** → 启动 `684_33`（`v:7967-7972`）；**state 140** → 启动 `730_36`（`v:7979-7984`）；**state 141** 等 done
- 出处：`docs/rtl_map/04_mac_array.md` §1 / §8（该文档逐行核过 RTL）

C++ 侧对应的就是 `if (ksize == 1)` 这**一个运行期分支**。删掉它之后 HLS 会重新生成 FSM：
state 134 从三分支变两分支，138/140/141 三个状态消失。**k=3/k=5 的子模块（764_38 / 819_41 / 892_47）端口、循环体、II、latency 都不变**（§7.2）。

---

## 3. 怎么一键恢复

**方法 A（不改任何源码，推荐给 csynth 对照）**
`hls_csynth_c8.tcl` 已经支持 `EXTRA_CFLAGS`：

```bash
EXTRA_CFLAGS=-DK1_IN_HLS=1 C8=1 TAG=c8_k1back PROJ_ROOT=/data/work/hls_c8_csynth \
  /data/xilinx/2025.2/2025.2.1/Vitis/bin/vitis-run --mode hls --tcl \
  pynq_specialized_code/pl/scripts/hls_csynth_c8.tcl
```

**方法 B（改一行源码）**
`pynq_specialized_code/pl/src/conv2d_engine.h:235`：

```c
#define K1_IN_HLS 0      // ← 改成 1
```

**恢复后是什么**：与 2026-10-23 第十轮（git `0e521ea`）的 k=1 快路径**逐字相同**
（`K1_LANES=16` / `K1_DSP_LANES=8` / `K1_WIDE_COLS` 默认 / `wbuf[16][448]` / 第二段 out_copy 全回来）。
本机已验证：`K1_IN_HLS=1` 构建对 6 个 k=1 形状的输出与归档 golden `out_11.bin` **逐字节一致**。

---

## 4. k=1 传入时的错误行为

| 项 | 说明 |
|---|---|
| 触发点 | `conv2d_engine.cpp:153`，在 `ksize ∈ {1,3,5}` 校验之后、`same padding` 校验**之前** |
| 返回码 | **5 = `ERR_K1_UNSUPPORTED`**（`conv2d_accel.h:79`） |
| 副作用 | **无**：不读权重、不写输出、不发起任何 `m_axi` 事务；PS 立刻看到 `ap_done` |
| 与 `ERR_SIZE` 的区别 | k=1 即使 `pad/dil` 完全正确也返回 **5**，不会被误判成参数越界 |
| AXI-Lite 映射 | **完全不变**：没有新增寄存器、没有移动偏移、端口名/位宽不变；只是 `ap_return` 多了一个取值 |
| 顶层签名 | **不变**（`conv2d_engine(...)` 的参数列表和默认值一个没动） |
| PS 侧表现 | `runner.py` 的 HLS 分支：`rc != 0` → `RuntimeError("PL 引擎返回错误码 5（op=…）")` |
| csim 侧 | `conv_ref_rt` / csim 的参考实现**仍支持 k=1**（没动），所以 csim 不会替你发现这件事；只有"硬件引擎"的行为变了 |

---

## 5. 预估节省

### 5.1 依据：`/data/work/hls_c8_csynth/r9_k1_l16`（C8_LAYOUT=1、K1_LANES=16、K1_DSP_LANES=8）

该构建的 `conv2d_engine_csynth.rpt` 顶层合计 **LUT 35,520 / FF 23,183 / DSP 108 / BRAM_18K 175**，
`Utilization → Detail → Instance` 表里逐项可查：

| 归属 | csynth 实例 | LUT | FF | DSP | BRAM |
|---|---|---:|---:|---:|---:|
| k=1 计算（16 车道） | `LOOP_684_33` | **2,507** | **1,463** | **8** | 0 |
| k=1 收尾（16 路展开） | `LOOP_730_36` | 552 | 320 | 0 | 0 |
| k=1 权重 LUTRAM | `wgt1_buf_U` … `wgt1_buf_15_U`（16 bank × 44/16） | 704 | 256 | 0 | 0 |
| k=1 尾部补零（C8） | `LOOP_639_28` | 79 | 21 | 0 | 0 |
| k=1 第二段宽写 | `LOOP_967_51` | 111 | 100 | 0 | 0 |
| `wbuf` 后 8 个 bank（只服务 k=1 的 ocb+8..15） | `wbuf_8_U`…`wbuf_15_U`（8 × 56/16） | 448 | 128 | 0 | 0 |
| `wbuf` 深度 448→64（8 个 bank，LUTRAM 7→1 个 LUT6/bit） | 推算 | ~384 | 0 | 0 | 0 |
| **小计** | | **4,785** | **2,288** | **8** | **0** |
| 顶层胶合（k=1 权重装载的 32-bit 读 mux / 16 bank 写译码 / `ocbl1` 比较器 / state 138-140-141 的下一状态逻辑） | 落在顶层 FSM 的 Expression/Multiplexer，报告里没有单独一行 | ~100–300（推定） | ~50–150（推定） | 0 | 0 |

`LOOP_684_33` 的 2,507 LUT 内部构成（同报告）：

- `Instance` 328 LUT = 8 个 **LUT 乘法器** `mul_8s_8s_16_1_1`（**41 LUT/个**，即 `K1_DSP_LANES=8` 之外的 8 条车道）
- `Instance` 8 DSP = 前 8 条车道（`BIND_OP impl=DSP`）
- `Expression` 1,782 LUT = 16 路乘积/累加的归约与选择
- `Multiplexer` 333 LUT、`Register` 1,463 FF = 32 条 26 位累加器（`2×16×26 = 832`）+ 5 级流水寄存器

### 5.2 合计（csynth 报告口径）

| | 改前 | 改后（预估） | 变化 |
|---|---:|---:|---:|
| LUT | 35,520（76.9%） | **30,435–30,735（65.9–66.5%）** | **−4.8k ~ −5.1k** |
| FF | 23,183（25.1%） | **20,745–20,895（22.5–22.6%）** | **−2.3k** |
| DSP | 108（67.5%） | **100（62.5%）** | **−8** |
| BRAM_18K | 175（92.1%） | **175（92.1%）** | **0** |

BRAM 一点不省是必然的：`line_buf` 的 112 块是三条路径共用的（k=3 的 3 行 / k=5 的 5 行 / dilation 的 7 行），
k=1 只用第 0 行但无法因此缩小；`wgt1_buf` / `wbuf` 本来就是 LUTRAM。

### 5.3 ⚠️ BD 口径校准：这一刀**不够**

csynth 的 LUT 数是**高估**的。同一份 r9 构建导出成 IP 后，Vivado 综合报告
（`pl/vivado/proj_engine/proj_engine.runs/design_1_engine_0_synth_1/…_utilization_synth.rpt`，2026-10-23 16:02）是：

| | csynth 报告 | Vivado 综合（更接近 BD） |
|---|---:|---:|
| LUT | 35,520 | **26,598**（LUT as Logic 18,491 + LUT as Memory 8,107） |
| FF | 23,183 | **17,161** |
| DSP | 108 | **117** |
| BRAM | 175 × 18K | **86.5 RAMB36**（≈173 × 18K，一致） |

按 LUT 比例（26,598/35,520 = 0.749）折算，k=1 在 BD 上的实际节省约 **3.6k LUT**（区间 3.5k–4.8k）。
于是：

- **BD 55,390 − 3.6k ≈ 51.8k**（−4.8k 的乐观口径 ≈ **50.6k**）
- 器件 **46,200** ⇒ 仍 **109.5%–112.1%**，**还超 4.4k–5.6k**
- **结论：移除 HLS k=1 是必要的（它确实是纯冗余），但单靠这一刀不会让 place 通过。**

顺带一个规模事实（同目录综合报告）：新手写 IP `design_1_k1_wrap_0` 自己占
**LUT 20,390 / FF 15,746 / DSP 33 / BRAM 0**。它现在是 BD 里最大的单块之一；
HLS 引擎（26,598）+ k1_wrap（20,390）= **46,988 LUT**，已经超过整颗器件。
**下一步收益最大的方向是 k1_wrap 本身（33 DSP / 20.4k LUT）或 k=3 的 72 乘法器阵列（3,272 LUT + 40 DSP），
不是继续在 HLS 里抠。**

### 5.4 明确没动的资源

- `LOOP_764_38`（k=3 计算）：DSP 40 / LUT 3,272 / FF 2,300 —— **全留**
- `LOOP_819_41`（k=3 收尾）：LUT 472 / FF 301 —— **全留**
- `LOOP_892_47`（k=5 通用）：DSP 20 / LUT 2,220 / FF 1,903 —— **全留**
- `LOOP_865_44`（k=3 死路径，932 LUT）：**没动**（不属于 k=1；要砍另开一刀）

---

## 6. 本机验证（没有 Vitis）

### 6.1 语法

`g++ -std=c++14 -fsyntax-only -Wall -Wextra`，4 组 `K1_IN_HLS × C8_LAYOUT` 全过；
另外 12 组 `FUSE_OUT / FUSE_SUM` 组合（× 2 个 K1 状态 × 2 个布局）也全过。
仅剩**改动前就有**的两类警告：`FUSE_*=0` 时 `out_lut/chsum` 未使用；`OUT_ADDR(ocb + OC_BLOCK, …)` 的宏括号建议。

### 6.2 功能（`tb/tb_engine_c8.cpp` + `build/fuzz_c8_data` 的 23 个形状）

C8 构建（`-DC8_LAYOUT=1`），输出与数据集里归档的 `out_11.bin` **逐字节比对**：

| 构建 | k=3/k=5（17 个形状） | k=1（6 个形状） |
|---|---|---|
| `K1_IN_HLS=0` | **17/17 PASS**，且与归档 golden 逐字节一致 | **6/6 返回 5**（`FAIL: conv2d_engine 返回错误码 5`）—— 预期行为 |
| `K1_IN_HLS=1` | 17/17 PASS（同 golden） | **6/6 PASS**，且与归档 golden 逐字节一致 ⇒ **一键恢复有效** |

覆盖的点：`d=1/2/3`、`s=1/2`、`cin=1..352`、`cout=1..192`、640 宽（10 个列块）、
`cin%8!=0` / `cout%8!=0` 的尾部补零、极小图（H/W < 块）、`k=5` 的 28 字节容器。

CHW 构建（`-DC8_LAYOUT=0`）另跑 4 个代表形状（k=3 d=1/d=3、k=5、s=2）：
`K1_IN_HLS=0` 与 `K1_IN_HLS=1` 输出逐字节一致，且与归档 `out_chwbuild.bin` 一致。

> 这些验证跑的是**同一份 C++ 的 g++ 编译**，能证明"k=3/k=5 行为没变、k=1 明确报错、开关可逆"，
> **不能**代替 csynth/cosim 对拍 HLS 的调度与资源。

---

## 7. 风险点

### 7.1 PS 路由（最高风险，非本文件能兜住）

见 §1。特别提醒：`k1_engine.eligibility()` 的**硬约束**（`cin ∈ [8,352]` 且 `cin%8==0`、
`cout ∈ [1,192]`、`s=1`、`d=1`、`pad=0`、`HOUT==H`、`WOUT==W`、必须 C8、`relu` 支持位、
**不支持 `out_lut`/`chsum` 融合**）现在每一条都变成"不满足就整网失败"。
上线前必须用 `test_k1_route.py` 预演到 132/132。

### 7.2 主 FSM 删状态会不会影响 k=3/k=5 的时序？

**不会**，理由是可核对的：

1. k=3/k=5 的计算/收尾是**独立子模块**（`764_38` / `819_41` / `892_47`），本次没有改它们的
   端口、循环体、pragma、II、latency；它们与 k=1 的 `684_33`/`730_36` 之间没有共享乘法器/累加器
   （`docs/rtl_map/04_mac_array.md` §1 已逐条核对：四条路径的乘法器组是**各自实例化、互不复用**的）。
2. 变化只在**顶层 FSM**：state 134 从三分支变两分支，138/140/141 消失，顶层组合逻辑**变少**。
   该分支在 RTL 里是 `empty_reg_7546`（`ksize`）的一次比较，**不在任何关键路径上**。
3. k=1 两个子模块自己的时序估算 4.492 ns / 4.718 ns（预算 6.667 ns），本来就不是 WNS 瓶颈；
   删掉它们不会让 k=3/k=5 的关键路径变长。
4. **但**：HLS 会重新编号状态、重新分配模块实例号。任何"按 `LOOP_xxx_yy` / state 号写死"的
   脚本（cosim 对拍、`docs/rtl_map` 的行号引用）都要重新生成，不要拿旧编号去找新 RTL。

### 7.3 `wbuf` 形状变化（仅 C8 构建）

`[16][448]` → `[8][64]`。风险点：k=3/k=5 在 `s=1` 时 `xn` 最大 64、`s=2` 时 32，
64 深的 bank 足够；23 形状回归（含 640 宽 = 10 个列块）已覆盖。
如果以后有人把 `bw` 改大（比如 128 列块），必须同步检查 `wbuf` 的第二维。

### 7.4 `src_cosim/conv2d_engine.cpp` 是另一份拷贝

`pl/src_cosim/conv2d_engine.cpp` 与 `pl/src/conv2d_engine.cpp` **当前已经不同**（本次没改它）。
如果 cosim 还拿那份旧拷贝去跑，会验证到**含 k=1 的旧引擎** → 假绿。
cosim 前必须先同步该文件，并把 k=1 的 testbench 期望改成"返回 5"。

### 7.5 融合出口（`FUSE_OUT` / `FUSE_SUM`，默认都是 0）

- `FUSE_SUM=1`：上界已按 `#if K1_IN_HLS` 分支处理（§2 #15），g++ 12 组组合全过。
- `FUSE_OUT=1`：k=1 收尾里那一段随 k=1 一起没了，其余路径不变。
- 这两个构建**没有**做功能回归（默认关闭，且不是本次交付形态）。

### 7.6 累加器上界检查

PS 侧 `check_limits()` 仍按调用参数 `k` 算 `ceil(cin/2)*k²*127*128 < 2^26`。k=1 已不走 HLS，
这条对 k=3/k=5 的判定逻辑不变，无需改。

---

## 8. 这次**没有**做的事

- 没有跑 `vitis_hls` / csynth / cosim / 综合 / 实现 / 出 bit（按要求）
- 没有 `git commit`
- 没有改 PS 代码。**建议的后续（需要另开任务）**：把 `runner.py` 里"k=1 回退 HLS"的分支改成
  硬错误（现在只是运行期返回 5 才报错，路由层仍会静默跳过并回退），这样问题在上板前就暴露。
- 没有清理 `K1_WIDE_COLS` / `K1_OC_UNROLL` / `K3_*` 这些死宏（`K1_IN_HLS=0` 下它们已无害，
  但留着会让后来人以为还能调）。

---

## 附录：改动文件与验证命令

```bash
# 语法（4 组）
cd pynq_specialized_code/pl
for k1 in 0 1; do for c8 in 0 1; do
  g++ -std=c++14 -fsyntax-only -Wall -Wextra -DK1_IN_HLS=$k1 -DC8_LAYOUT=$c8 \
      -I src src/conv2d_engine.cpp
done; done

# 功能（沿用现有 tb 与数据集）
g++ -std=c++14 -O2 -DK1_IN_HLS=0 -DC8_LAYOUT=1 -I src \
    src/conv2d_engine.cpp src/conv_ref_rt.cpp tb/tb_engine_c8.cpp -o /tmp/tb_k1off
g++ -std=c++14 -O2 -DK1_IN_HLS=1 -DC8_LAYOUT=1 -I src \
    src/conv2d_engine.cpp src/conv_ref_rt.cpp tb/tb_engine_c8.cpp -o /tmp/tb_k1on
#   TB_DUMP_OUT=/tmp/o.bin /tmp/tb_k1off build/fuzz_c8_data/c000 12 12 3 1 1 1 13
```

改动文件：

- `pynq_specialized_code/pl/src/conv2d_engine.cpp`
- `pynq_specialized_code/pl/src/conv2d_engine.h`
- `pynq_specialized_code/pl/src/conv2d_accel.h`（只加错误码）

相关文档：`docs/rtl_map/04_mac_array.md`（RTL 侧逐路径归属与 FSM 状态）、
`docs/部署/k1_IP接入.md`（新 IP 的接入与寄存器）、
`docs/部署/k1_IP上板验证清单.md`（上板步骤）。
