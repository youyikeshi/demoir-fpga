# HLS 再移除 k=5 通路（2026-10-26，第二刀）

> **状态**：源码已改，本机 `g++` 语法检查（8 组开关组合）+ 23 形状功能回归（C8/CHW × 4 种开关组合）
> + 副作用测试 + 预处理逐字对拍**全部通过**。
> **没有跑** `vitis_hls` / csynth / cosim，**没有**综合/实现/出 bit，**没有** `git commit`
> —— 按要求留给主控统一跑（对照构建命令见 §附录 A.5）。
>
> **目标**：k=5 已被手写 RTL `conv_k5_engine` 完全替代（全网唯一 1 个 k=5 层
> `encoder.conv_first`：320²、cin=12→物理 16、cout=48、pad=2；逐字节 30,720/30,720、
> 拍数比 0.055 = 18.2×）。HLS 里那条 25 抽头通用通路只剩资源占用，而 **BRAM 是当前最紧的
> 资源（引擎 IP 86.5/95 RAMB36 = 91%）**。本次把它从综合范围里拿掉，并且**明确失去
> HLS 侧的 k=5 能力**。

---

## 0. 一句话结论

`conv2d_engine.h` 新增编译期开关 **`K5_IN_HLS`，默认 0**（照 `K1_IN_HLS` 的样子）：

| `K5_IN_HLS` | 行为 |
|---|---|
| **0（本次默认）** | k=5 的 `wgt_buf`（**50 块 BRAM18K**）、25 抽头计算、权重装载、`off_ocb28` 递推、PAD28 宽读全部被预处理掉（**综合零资源**）；函数入口对 `ksize == 5` 返回 **6 = `ERR_K5_UNSUPPORTED`**（报错，不是算错） |
| **1（一键恢复）** | 逐字回到 2026-10-23 第十轮的行为；本机已用**预处理器逐字 diff** 证明：`K5_IN_HLS=1` 的翻译单元与改动前**只差一个新枚举值** |

资源账（详见 §5，**两种口径分开写**）：

| 资源 | 报告里逐项可查的归属 | 本次预估（csynth，含上一刀 2.4× 校准） | 折算到 Vivado 综合 / 引擎 IP |
|---|---:|---:|---:|
| LUT | **−4,083** | −6k ~ −10k（中心 −8k） | **−4.5k ~ −7.5k** ⇒ 折算后约 11k–14k（§5.3） |
| FF | **−2,779** | −5k ~ −7k（中心 −6k） | **−3.7k ~ −5.2k** ⇒ 折算后约 7.9k–9.4k |
| DSP | **−25** | −25 ~ −30 | **−25 ~ −30** ⇒ 106 → 76–81 |
| BRAM18K | **−50**（= 25 RAMB36，**几乎精确**） | −50（可能 −50~−59） | **−25 RAMB36** ⇒ 86.5 → **61.5**（91% → 64.7%） |

**这一刀的主收益是 BRAM：−25 RAMB36**。上一刀（删 k=1）的实测是
`175 → 166` BRAM18K，把 k=5 的 50 块减掉正好 `166 − 50 = 116`，
与"从 r9 基线一次减掉 9+50"得到同一个数 —— 两把刀在 BRAM 上**可加**，这是本次最确定的一项。

---

## 1. ⚠️ 部署前提：PS 必须把所有 k=5 层路由到 `conv_k5_engine`

1. **k=5 的"HLS 兜底"这条路被永久关掉了**。改完之后，任何 k=5 层只要没被路由到新 IP，
   就会拿到返回码 **6**，`runner.py` 的 HLS 分支会抛
   `RuntimeError("PL 引擎返回错误码 6（op=…）")`，**整网停下**。
   这是刻意设计（与 k=1 完全同一约定）：宁可整网停，也不能让 k=5 掉进通用通路
   只算出一部分抽头 —— 那种输出"看着正常"但整网精度崩，是最难查的一类。
2. **⚠️ 今天 PS 侧还没有 k=5 的路由器**（这点必须先补上，否则新 bit 一跑就停）：
   `runner.py:597-600` 的注释写着"k=3/k=5 是另一条线（未来 conv_k3/k5）"，
   `k1_route_check()` 对 `k != 1` 直接返回 `"k=%s ≠ 1（本 IP 只做 1×1）"` 并不路由。
   也就是说：**在新 bit 上，`encoder.conv_first` 会原样落到 HLS 引擎 → 返回 6 → 整网停在第一层。**
   所以本次改动要么等 `conv_k5_engine` 接进 BD 并配套一个 `_k5_route()`，要么先别换 bit。
   好消息是 **k=5 只有 1 层**，路由面比 k=1（132 层）小得多：
   `encoder.conv_first`（320×320、cin=12、cout=48、k=5、pad=2、s=1、ReLU）。
   上板前直接断言"`k5_calls == 1` 且该层就是 `encoder.conv_first`"即可。
3. **权重容器不用改**。k=5 的权重包仍是 **PAD28 容器**（28 字节/(oc,ic)，
   25 抽头 + 3 补零 = 7 个 32-bit 字，`48×12×28 = 16,128 B`）。
   `conv_k5_engine` 的接口契约（`docs/rtl_hand/conv_k5_engine.md` §1）读的就是
   同一个 PAD28 容器 ⇒ PS 打包、`check_k5_pad28.py`、`check_consistency.py` **全都不用动**。
   §2.5 也说明了为什么 `K5_PAD28` 这个宏**必须保留为 1**。
4. **旧 bit 上先做一次纯路由演练**：旧 bit 的 HLS 侧还有 k=5 通路，某层没被接住也能算完
   （只是慢）。确认 `k5_calls == 1`、无回退之后再换新 bit。
5. `conv_k5_engine` 是**编译期参数**模块（`CIN/COUT/H/W` 由参数给定，运行期
   `ctrl_cin/cout/h/w` 不参与寻址）。它只覆盖这一组形状；**网络/输入分辨率一变就要重新例化**，
   而 HLS 这边已经没有 k=5 兜底了。

---

## 2. 改了哪些块

改动全部是 **`#if K5_IN_HLS` / `#else` / `#endif` 包裹 + 注释**，没有移动任何一行 k=3 代码。
k=1 的开关与代码原样保留（`K1_IN_HLS` 默认仍是 0）。

"没有移动 k=3 代码"是可核对的：`git diff -U0` 的 hunk 只落在
**153 / 197–198 / 228–230 / 467–494 / 604–654 / 902–1004** 这几段；
k=3 的权重装载器（**549–603**）与 k=3 快路径（**814–901**）**一行都不在 hunk 里**。
唯一碰到的 k=3 相关代码是"藏在 k=5 那个 else 里的死分支"（旧 933–963），
它不属于可达的 k=3 路径（见 §2.4）。

### 2.1 `pynq_specialized_code/pl/src/conv2d_engine.h`（302 → 343 行）

| 改前行 | 改后行 | 内容 |
|---|---|---|
| 266（`#endif // K1_IN_HLS` 之后） | **267–306** | 新增 **`K5_IN_HLS` 开关（默认 0）+ 41 行说明**：为什么删（BRAM 账）、`=0` 时消失什么、怎么恢复、以及"`K5_PAD28` 必须保持 1"的提醒 |

`K5_PAD28`（192–210 行）**没动**：它是**权重容器约定**，与 HLS 是否实现 k=5 无关。

### 2.2 `pynq_specialized_code/pl/src/conv2d_accel.h`（90 → 96 行）

| 改前行 | 改后行 | 内容 |
|---|---|---|
| 79 | 79–85 | 错误码枚举新增 **`ERR_K5_UNSUPPORTED = 6`**（`ERR_K1_UNSUPPORTED = 5` 后面加逗号）。**不新增/不移动任何 AXI-Lite 寄存器**，返回码仍在同一个 32 位 `ap_return` 里 |

### 2.3 `pynq_specialized_code/pl/src/conv2d_engine.cpp`（1076 → 1135 行）

行号为**改动前 → 改动后**（净增 59 行，几乎全是注释与 `#if`/`#else`/`#endif`）：

| # | 改前行 | 改后行 | 块 | 处理 |
|---:|---|---|---|---|
| 1 | 153 之后 | **155–167** | **入口参数校验** | 新增 `#if !K5_IN_HLS if (ksize == 5) return ERR_K5_UNSUPPORTED;` —— 紧跟 k=1 的检查之后、`stride/dil/pad/shift` 校验**之前** |
| 2 | 197–198 | **212–222** | **`wgt_buf[8][352][5][5]` 声明** | 声明收进 `#if K5_IN_HLS`（附 grep 证据的注释：它只服务 k=5） |
| 3 | 229–230 | **253–256** | `wgt_buf` 的两条 pragma（`complete dim=3` / `dim=4`） | 同上 |
| 4 | 468–469 | **494–497** | `off_ocb28` 声明（28 字节容器的块基址递推） | `#if K5_IN_HLS` |
| 5 | 484–487 | **510–517** | `off_b` 的"按需声明"条件 | `(K5_PAD28 == 0)` → `(K5_IN_HLS && (K5_PAD28 == 0))`，避免 K5=0 时给一个没人用的变量 |
| 6 | 491 | **519–521** | `const int off28_b = off_ocb28;` | `#if K5_IN_HLS` |
| 7 | 494 | **524–526** | `off_ocb28 += cin*28*OC_BLOCK;` | `#if K5_IN_HLS` |
| 8 | 604–655 | **637–695** | **k=5 权重装载**（PAD28 的 7 字宽读 + `K5_PAD28=0` 的逐字节基线，两支都在内） | `if (ksize == 1) … else if (ksize == 3) … else { }` 链**一个字没挪**，只把最后一个 `else` 的 body 用 `#if K5_IN_HLS` / `#else`（空注释）/ `#endif` 包住 |
| 9 | 902–1005 | **943–1063** | **k=5 计算通路**（25 抽头 `part` 加法链，csynth 里的 `LOOP_892_47`）**连同同一块里的两条恒不可达死分支**（ksize==1 的一乘一 = `LOOP_849_43`、ksize==3 的 9 抽头 = `LOOP_865_44`） | 外层 `if (ksize==1) … else if (ksize==3) … else {` 的最后一个 `else` body 整体 `#if K5_IN_HLS` 掉 |

**关于"主 FSM 里分派到 k=5 的状态"**：这份 C++ 没有手写 FSM，状态机是 HLS 从循环生成的。
在生成 RTL（`r9_k1_l16`）里：

- `conv2d_engine.v:15938-15946` —— **state 134** 按 `ksize` 三分支：
  `ksize==1 → 138`（k=1 `684_33`）、`ksize==3 → 135`（k=3 `764_38`）、
  **`ksize∉{1,3} → 142`**（k=5）；到 142 的另一条入口是 `state 150 → 142`（装载收尾）。
- **state 142** 里的 8 次 oc 循环（`838_42`）再分三支：`849_43`（ksize==1）、
  `865_44`（ksize==3）、`892_47`（其余 = k=5）—— 见 `conv2d_engine.v:8012-8046`。
- 由于 `ksize ∈ {1,3,5}` 已在上界校验里限定，`ksize∉{1,3}` 就等价于 `ksize==5`，
  所以 **state 142 及其三个子模块全部只服务 k=5**。

删掉 C++ 里那个 `else` 之后，HLS 会重新生成 FSM：**state 142、122–127（`wgt_buf` 的 6 个写相位）
以及 `849_43 / 865_44 / 892_47` 三个子模块一起消失**；state 134 从三分支变两分支。
k=3 的 `764_38 / 819_41` 与 k=1 的 `684_33 / 730_36` 的端口、循环体、pragma、II、latency **都没有理由变化**
（§7.2 说明为什么"没理由"而不是"保证"）。

### 2.4 `wgt_buf` 到底服务谁？——grep + RTL 双向确认

**grep 全文件，`wgt_buf` 一共 8 处引用**（不含注释）：

| 行（改前） | 类别 | 归属 |
|---:|---|---|
| 198 | 声明 | — |
| 229、230 | pragma（dim3/dim4 完全拆分） | — |
| 633、650 | **写**（PAD28 宽读 / 逐字节基线） | k=5 装载器（唯一写点） |
| 918 | 读 | k=5 块内 `if (ksize==1)` 的那条一乘一 —— **恒不可达** |
| 948 | 读 | k=5 块内 `else if (ksize==3)` 的 9 抽头 —— **恒不可达** |
| 975 | 读 | k=5 的 25 抽头循环（唯一可达读点） |

RTL 侧的独立证据（同一结论）：

- `wgt_buf` 的 25 个 bank 只有 6 个写相位：`conv2d_engine.v` 里 25 条
  `wgt_buf_*_we0_local = 1'b1` 全部挂在 **state 122–127**（bank 组 0–7 / 8–11 / 12–15 /
  16–19 / 20–23 / 24，与 `docs/rtl_map/03_weight_path.md` §5 的逐 bank 实测记录一致）。
  `wgt1_buf` 的写在 state 104（k=1），`wgt3_buf` 完全不在这些状态里。
- 读点：`LOOP_892_47`（25 bank 全用）、`LOOP_865_44`、`LOOP_849_43`；
  三者都在 state 142 之下，而 state 142 只有 k=5 能进。
- k=3 走 `wgt3_buf`（72 个 LUTRAM bank），
  k=1 走 `wgt1_buf`（16 个 LUTRAM bank）—— **两者都完全不碰 `wgt_buf`**。

> **结论：`wgt_buf` 只服务 k=5，可以整块删。** 它另外两条引用是无害的死代码
> （它们在"只有 k=5 能进"的 else 里），所以本次是把**整个 else** 一起关掉，
> 而不是"只删 k=5 的 25 抽头、留着 wgt_buf 给死代码用" —— 后者会留下全部 50 块 BRAM。

> ⚠️ **顺手纠正 `docs/rtl_map/06_k5_and_primitives.md` §1.4 的一句话**：
> 那里写"这条 25 抽头流水**同时服务 k=1 / k=3 / k=5**"。那是只看了循环体里的
> `ksize>4 / ksize>2` 掩码得出的结论，**没有看主 FSM 的分派**。实际情况是：
> state 134 已经把 k=1 送去 138、k=3 送去 135，`892_47` 只有 k=5 能进；
> 那些掩码是 HLS 无法从 `~(empty==1) & ~(empty==3)` 推出"==5"而留下的尾巴。
> 本次的 23 形状回归（13 个 k=3 形状含 d=2/d=3/s=2/640 宽，逐字节一致）也在功能上证实了这点。

### 2.5 `K5_PAD28` 为什么保留

`K5_PAD28` 是**导出侧/HLS/手写 RTL 三方共享的权重容器约定**，不是"HLS 要不要算 k=5"的开关：

- `=1`：容器 28 字节/(oc,ic)，7 个 32-bit 字；`conv_k5_engine` 按这个容器读（16,128 B）。
- 所以 `K5_IN_HLS=0` 时它**仍然保持 1**，只是 HLS 不再消费它。
- 它现在只在被 `#if K5_IN_HLS` 包住的代码里出现 ⇒ 关掉时是个"定义了但没人用"的宏，无害。

---

## 3. 怎么一键恢复

**方法 A（不改源码，推荐给 csynth 对照）**——`hls_csynth_c8.tcl` 支持 `EXTRA_CFLAGS`：

```bash
EXTRA_CFLAGS=-DK5_IN_HLS=1 C8=1 TAG=c8_k5on PROJ_ROOT=/data/work/hls_c8_csynth \
  /data/xilinx/2025.2/2025.2.1/Vitis/bin/vitis-run --mode hls --tcl \
  pynq_specialized_code/pl/scripts/hls_csynth_c8.tcl
```

**方法 B（改一行源码）** `pynq_specialized_code/pl/src/conv2d_engine.h:305`：

```c
#define K5_IN_HLS 0      // ← 改成 1
```

**恢复后是什么**：与 2026-10-23 第十轮（git `0e521ea`）的 k=5 通路**逐字相同**。
本机证明方式（比"读一遍代码"硬）：把两个版本的翻译单元都 `g++ -E -P` 出来 diff ——

```bash
# HEAD 版本
git show HEAD:pynq_specialized_code/pl/src/conv2d_engine.cpp > /tmp/base/conv2d_engine.cpp
# …头文件同理…
g++ -E -P -std=c++14 -DK1_IN_HLS=1 -DC8_LAYOUT=1 -I /tmp/base /tmp/base/conv2d_engine.cpp > /tmp/head.i
# 新版本，K5=1
g++ -E -P -std=c++14 -DK1_IN_HLS=1 -DK5_IN_HLS=1 -DC8_LAYOUT=1 -I src src/conv2d_engine.cpp > /tmp/new.i
diff /tmp/head.i /tmp/new.i
```

实测结果**只有一处差异**：

```
147c147,148
<     ERR_K1_UNSUPPORTED = 5
---
>     ERR_K1_UNSUPPORTED = 5,
>     ERR_K5_UNSUPPORTED = 6
```

即：`K5_IN_HLS=1` 时生成的代码与改动前**逐字相同**，语义零漂移。

---

## 4. k=5 传入时的错误行为

| 项 | 说明 |
|---|---|
| 触发点 | `conv2d_engine.cpp:167`，在 `ksize ∈ {1,3,5}` 校验之后、`stride/dil/pad/shift` 校验**之前** |
| 返回码 | **6 = `ERR_K5_UNSUPPORTED`**（`conv2d_accel.h:85`） |
| 副作用 | **无**：不读权重、不写输出、不发起任何 `m_axi` 事务；入口直接返回，PS 立刻看到 `ap_done` |
| 与 `ERR_SIZE` 的区别 | k=5 即使 `pad/dil` 写错也返回 **6**，不会被误判成参数越界（本机已测：`k=5, pad=0 → 6`）。**例外**：如果 `Hout/Wout` 本身与 `H/W/s` 对不上，会在更前面的通用尺寸校验拿到 **2** —— k=1 也是同样的顺序，不是本次引入的 |
| AXI-Lite 映射 | **完全不变**：没有新增寄存器、没有移动偏移、端口名/位宽不变；只是 `ap_return` 多了一个取值 |
| 顶层签名 | **不变**（参数列表与默认值一个没动） |
| PS 侧表现 | `runner.py` 的 HLS 分支：`rc != 0` → `RuntimeError("PL 引擎返回错误码 6（op=…）")` |
| csim 侧 | `conv_ref_rt` / csim 的参考实现**仍支持 k=5**（没动），所以 csim 不会替你发现这件事 |

---

## 5. 节省（分资源项）

### 5.1 依据：`/data/work/hls_c8_csynth/r9_k1_l16`（C8_LAYOUT=1、K1_LANES=16）

该构建的 `conv2d_engine_csynth.rpt` 顶层合计 **LUT 35,520 / FF 23,183 / DSP 108 / BRAM_18K 175**
（`rpt:L94-102`）。逐流水线统计（`Instance` 表 `rpt:L112-133`，三行在 `rpt:L130-132`）：

| 归属 | csynth 实例 | LUT | FF | DSP | BRAM18K |
|---|---|---:|---:|---:|---:|
| **k=5 主计算（25 抽头）** | `LOOP_892_47` | **2,220** | **1,903** | **20** | 0 |
| k=5 块内**死分支**（ksize==3 的 9 抽头） | `LOOP_865_44` | **932** | **572** | **5** | 0 |
| k=5 块内**死分支**（ksize==1 的一乘一） | `LOOP_849_43` | **401** | **304** | 0 | 0 |
| `wgt_buf` 的顶层地址/使能 mux（25 个 `address0` + 25 个 `ce0`） | `Multiplexer` 表 50 行（`rpt:L962-1010`） | **530** | 0 | 0 | 0 |
| `wgt_buf` 本体（25 bank） | `Memory` 表（`rpt:L272-296`） | 0 | 0 | 0 | **50** |
| **可查小计** | | **4,083** | **2,779** | **25** | **50** |
| k=5 权重装载器（内联在顶层）+ `off_ocb28` 递推 + state 142 的控制/比较器 | 报告**没有单列** | 150–400（估） | 60–200（估） | 0–3（估） | 0 |
| **直接归属合计** | | **4.2k–4.5k** | **2.8k–3.0k** | **25–28** | **50** |

`LOOP_892_47` 的 2,220 LUT 内部构成（`rpt892:L72-102`；DSP=20 在 `rpt892:L57`）：

- `Instance` **785** = **20 个 `sparsemux_15_3_8`**（7:1 × 8 bit，29 LUT/个 = 580）
  + **5 个 `mul_8s_8s_16_1_1`**（裸组合乘法器，41 LUT/个 = 205）。
  25 个抽头 = 20 个 DSP MAC + 5 个 LUT 乘法；20 个 mux = 5 行 × 4 列，
  第 5 列靠 mux 一拍两用（`docs/rtl_map/06_k5_and_primitives.md` §1.4）。
- `Expression` 625、`Multiplexer` 362、`Register` 448 LUT + **1,903 FF**（25 抽头加法链的逐级寄存器）。
- II achieved = **3**（`rpt892:L44`），瓶颈是 `line_buf` 一次迭代要 5 个不同列地址、每 bank 只有 2 个读口。

### 5.2 `wgt_buf` 到底占多少 BRAM？—— **50 块 BRAM18K，你的记录是对的**

逐项核实（`rpt:L272-296`，25 行 `wgt_buf_U` … `wgt_buf_24_U`，每行一个 bank）：

| 字段 | 值 |
|---|---|
| 每个 bank 的形状 | `2816 字 × 8 bit = 22,528 bit` = 2,816 B |
| 一块 BRAM18K 的容量 | `18,432 bit` |
| 每个 bank 需要 | `22,528 / 18,432 = 1.22 → **2 块 BRAM18K**`（报告 `BRAM_18K` 列每行就是 `2`） |
| bank 数 | **25**（`Array_PARTITION complete dim=3` × `dim=4` = 5×5） |
| **合计** | `25 × 2 = **50 块 BRAM18K**` = 563,200 bit = **68.8 KB** |
| 占比 | 设计 163 块**存储** BRAM 的 **30.7%**（`rpt:L297` 的 Memory Total 行）；175 块（含 m_axi 的 12）里的 **28.6%** |
| 换算 | **50 BRAM18K = 25 RAMB36 = 器件 95 个 RAMB36 的 26.3%** |

> 为什么是 2816 深：`wgt_buf[OC_BLOCK][CIN_MAX][KMAX][KMAX]`，`oc` 维**没有**拆分
> （所以落进地址：`oc*352 + ic`），深度 = `8 × 352 = 2816`，而 k=5 这层实际只用
> `8 × cin = 8 × 12 = 96`。**深度按 CIN_MAX 分配，实际只用 1/29** —— 这就是它贵的原因。
> （`docs/rtl_map/03_weight_path.md` §1.4 的"50 块，不是 51"与本次逐行核对一致；
> 51 那个数字是把 `bias_buf` 的 1 块算进去了，或者来自 impl 报告口径。）

### 5.3 本次改动口径的合计

**csynth 口径**（相对当前已删 k=1 的构建：`23,974 / 17,711 / 97 / 166`，见 commit `cbb15cc`）：

| | 上一刀后（实测） | 本次预估 | 变化 |
|---|---:|---:|---:|
| LUT | 23,974 | **13.9k–17.9k** | **−6.0k ~ −10.0k（中心 −8k）** |
| FF | 17,711 | **10.7k–12.7k** | **−5.0k ~ −7.0k（中心 −6k）** |
| DSP | 97 | **69–72** | **−25 ~ −28** |
| BRAM18K | 166 | **107–116** | **−50（可能 −50~−59）** |

> 下界 **−4.1k~−4.5k LUT / −2.8k~−3.0k FF / −25 DSP / −50 BRAM** 是"报告里逐项直接归属、删掉一定消失"的部分；
> 上界用上一刀的实测放大系数校准（§5.4）。**真值只能由对照 csynth 给出**（命令见附录 A.5）。

**BD / Vivado 综合口径**（引擎 IP 单独综合，`pl/vivado/proj_engine/proj_engine.runs/design_1_engine_0_synth_1/design_1_engine_0_utilization_synth.rpt`，2026-10-23 16:02）

折算比例沿用 `HLS移除k1分支.md` §5.3 的做法：csynth → Vivado 综合
**LUT ×0.749**（35,520 → 26,598）、**FF ×0.740**（23,183 → 17,161）、BRAM ≈ 1:1
（175 CSYNTH = 86.5 RAMB36）、DSP ≈ 1:1（csynth 108 → Vivado 117，Vivado 会把 LUT 乘法器也塞进 DSP）。

| | r9 实测（含 k=1、含 k=5） | 上一刀后（折算） | **本次预估** |
|---|---:|---:|---:|
| LUT | 26,598 | 26,598 − 11,546×0.749 ≈ **17,950** | **10.5k–13.5k** |
| FF | 17,161 | 17,161 − 5,472×0.740 ≈ **13,112** | **7.9k–9.4k** |
| DSP | 117 | 117 − 11 ≈ **106** | **76–81** |
| **RAMB36** | **86.5（91.05%）** | 86.5 − 9×0.5 ≈ **82**（若已体现） | **57** ⇒ *或* 直接从 86.5 减：**61.5** |

> BRAM 这一行的两个数（57 / 61.5）差的就是"上一刀那 9 块 BRAM18K 有没有已经反映在
> 现有综合数里"：`cbb15cc` 实测 csynth 175→166（−9，≈ −4.5 RAMB36），但手上唯一一份
> Vivado 综合报告是 **10-23（k=1 还在）**的 86.5，没有对应的新报告。
> **无论按哪种口径，本次都是 −25 RAMB36**：86.5 → 61.5，或 82 → 57，
> 占用率从 91% 掉到 **60%–65%**。
>
> 这三个新数（10.5k–13.5k LUT / 7.9k–9.4k FF / 76–81 DSP）**只是折算**，误差不小
> （尤其 LUT：折算比例本身是"整个设计"的比例，套到"某一条路径的差值"上只能算数量级）。
> **要以重新综合的引擎 IP 报告为准。**

### 5.4 为什么预估大概率**仍然偏低**（上一刀：预估 −4,785，实测 −11,546）

上一刀的逐项归属与实测对照（用 commit `cbb15cc` 的实测数与 `HLS移除k1分支.md` §5.1 的预估）：

| 资源 | 上一刀"逐项归属"预估 | 上一刀实测 | 放大系数 |
|---|---:|---:|---:|
| LUT | 4,785 | **11,546** | **2.41×** |
| FF | 2,288 | **5,472** | **2.39×** |
| DSP | 8 | **11** | 1.38× |
| BRAM18K | 0 | **9**（175→166） | ∞（0 → 9） |

原因（这也是本次预估只敢写"中心 −8k"的理由）：

1. **`Instance` 表只统计子模块边界内的逻辑**。顶层为这些子模块生成的地址/选择/使能逻辑落在
   `Expression`（8,267 LUT）/ `Multiplexer`（6,095 LUT）/ `Register`（8,264 FF）三栏里，
   报告**不给单路径分摊**。上一刀 `wbuf` 16 bank 的 1,232 LUT mux、`wgt1_buf` 的 720 LUT mux
   就全在这一栏，一个都没进 4,785。
2. **HLS 会共享后绑定（shared binding）**。地址乘法器（`mul_29ns_32s_60` 等 10 个实例，33 DSP）、
   比较器、选择器被多条路径复用。删掉一条路径后，共享实例的输入选择器少一档、甚至整块消失，
   收益**大于**"该路径自报的子模块资源"。
3. **删分支连带删掉状态与条件**。上一刀删掉了 FSM 的 138/140/141 三个状态、16 车道的收尾序列、
   两处补零；本次删的是 state 142 + state 122–127（`wgt_buf` 的 6 个写相位）+ 三个子模块。
   这些在报告里同样是"顶层"的，不给归属。
4. **重新调度（rescheduling）是非线性的**。少一条路径后，剩余循环的 II/latency 约束更松，
   HLS 可能改变共享度与流水级数 —— 方向**通常**有利（所以实测 > 预估），但不可预测，
   也可能出现"局部反而涨"的情况。这就是为什么必须做对照 csynth，而不能只信加法。
5. **存储器相关的东西会被连带重算**。上一刀 BRAM 预估 0，实测 **−9**（`m_axi` 内部 FIFO 跟着缩了）；
   本次也可能出现类似的小尾巴。

反向的、这次特有的不确定性：

- 现在的基线是**已经删掉 k=1 的构建**（23,974 LUT），上一刀那种"一次性收割一大堆共享逻辑"的
  余量已经被吃掉一部分；所以 **2.4× 是上界而不是期望值**。
- k=5 通路**没有**上一刀那种 LUTRAM 形状变化（`wbuf` 16→8 bank、深度 448→64）的连带收益，
  而这部分在上一刀的 4,785→11,546 里占了不少。
- ⇒ 结论：**本次 csynth LUT 节省大概率落在 −6k ~ −10k，中心 −8k；FF −5k ~ −7k**。
  如果实测只掉 4–5k 也不要惊讶（那就是"没有放大"的情形）。

### 5.5 明确没动的资源

- `LOOP_764_38`（k=3 计算）：**DSP 40 / LUT 3,272 / FF 2,300** —— 全留（`rpt:L128`）
- `LOOP_819_41`（k=3 收尾）：**LUT 472 / FF 301** —— 全留（`rpt:L129`）
- `line_buf` 的 **112 块 BRAM18K**（7 bank × 16）—— 全留。它是三条路径共用的
  （k=3 的 3 行 / k=5 的 5 行 / dilation 的 7 行，按 `SPAN_MAX=7` 分配），k=5 走了也缩不了
- `LOOP_945_50`（所有路径共用的 64-bit 宽写回）：LUT 110 / FF 100 —— 全留
- `LOOP_649_30`（`cout%8 != 0` 的补零）：LUT 79 / FF 21 —— 全留（k=3 也要用）
- `rdoff[5]` 及其 mux（57 LUT）：k=3+dilation 还在用它，本次**没动**。
  真要再抠，可以把 `KMAX` 从 5 降到 3 —— 但那是另一刀（会动到 `wgt_buf` 之外的上界约定），
  不属于本次范围

---

## 6. 本机验证（没有 Vitis）

### 6.1 语法：8 组开关组合 × 全过

```bash
cd pynq_specialized_code/pl
for k1 in 0 1; do for k5 in 0 1; do for c8 in 0 1; do
  g++ -std=c++14 -fsyntax-only -Wall -Wextra \
      -DK1_IN_HLS=$k1 -DK5_IN_HLS=$k5 -DC8_LAYOUT=$c8 -I src src/conv2d_engine.cpp
done; done; done
```

**8/8 通过**。警告与改动前**完全同类、同数量**：`FUSE_*=0` 时 `out_lut/chsum` 未使用（2 条），
以及 `OUT_ADDR(ocb + OC_BLOCK, …)` 的宏括号建议（只在 `K1_IN_HLS=1` 时出现，改动前就有）。

另外补跑了 4 组"非默认调试构建"：`K5_PAD28=0`（基线容器）× `K3_PAD12=0`、
`FUSE_OUT=1 + FUSE_SUM=1`，× K5=0/1 —— 全过。

额外证明（`g++ -E -P` 预处理后 grep）：

```
K5_IN_HLS=0, K1_IN_HLS=0 的翻译单元：
  wgt_buf 引用数 = 0      ← 数组彻底不存在 ⇒ HLS 不可能给它分配 BRAM
  wgt1_buf 引用数 = 0
  wgt3_buf 引用数 = 11    ← k=3 原样
  "ksize == 5" 出现 1 次  ← 只剩入口那句 return
```

### 6.2 功能：`tb_engine_c8` + 现有 23 形状 golden，逐字节比对

判据：PASS 时不但要比 `conv_ref_rt` 一致，还要与数据集里归档的 `out_11.bin`
（C8 构建）/ `out_chwbuild.bin`（CHW 构建）**逐字节相同**。

| 构建 | k=3（13 个形状） | k=1（6 个） | k=5（4 个） |
|---|---|---|---|
| `K1=0, K5=0`（C8） | **13/13 PASS + 逐字节** | 6/6 → **5** | 4/4 → **6** |
| `K1=1, K5=1`（C8） | 13/13 PASS + 逐字节 | **6/6 PASS + 逐字节** | **4/4 PASS + 逐字节** ⇒ 23/23 |
| `K1=1, K5=0`（C8，混合） | 13/13 PASS | 6/6 PASS | 4/4 → **6** |
| `K1=0, K5=1`（C8，混合） | 13/13 PASS | 6/6 → **5** | 4/4 PASS ⇒ 17/17 |
| `K1=0, K5=0`（CHW） | 13/13 PASS + 逐字节 | 6/6 → 5 | 4/4 → 6 |
| `K1=1, K5=1`（CHW） | **23/23 PASS + 逐字节** | | |

两个混合组合证明了 **K1 / K5 两个开关相互独立、各自可逆**（不是"必须一起动"）。

覆盖的点：`d=1/2/3`、`s=1/2`、`cin=1..352`、`cout=1..192`、640 宽（10 个列块）、
`cin%8!=0` / `cout%8!=0` 的尾部补零、极小图（H/W < 块）、k=5 的 28 字节容器。

### 6.3 副作用：被拒绝时**真的**什么都没做

单独写了个小测试（`/tmp` 下，不进仓库）直接调 `conv2d_engine`：

| 检查 | 结果 |
|---|---|
| `k=5, pad=2` → 返回 6 | ✅ |
| 输出缓冲预填 `0xA5`，调用后**逐字节未变** | ✅ |
| 输入 / 权重 / bias 缓冲**逐字节未变** | ✅ |
| `k=5, pad=0`（参数也错）→ 仍然返回 **6**（错误码先于 same-padding 校验） | ✅ |
| `k=1` → 返回 **5**，输出未变 | ✅ |
| 对照：`k=3` 仍返回 0；`k=3, pad=0` → 2；`k=4` → 2 | ✅ |

### 6.4 可逆性：预处理逐字 diff

见 §3：`K5_IN_HLS=1` 的翻译单元与改动前**只差 `ERR_K5_UNSUPPORTED = 6` 这一行**。

### 6.5 这些验证**不能**替代什么

- 它们是**同一份 C++ 的 g++ 编译**，能证明"k=3 行为没变、k=1/k=5 明确报错、开关可逆、
  没有副作用"，**不能**证明 HLS 的调度、II、资源和时序。
- g++ 不做 `ARRAY_PARTITION` / `BIND_STORAGE`，所以"`wgt_buf` 消失后 BRAM 真的少 50"只能由
  csynth 证明（§5.2 的 50 是**改动前**报告里的实测归属，不是"改动后一定省 50"的实测；
  但存储器实例不共享，这个 1:1 的可信度远高于 LUT/FF 的 2.4× 外推）。

---

## 7. 风险点

### 7.1 PS 路由（最高风险，非本文件能兜住）

见 §1。**在新 bit 上，路由没接上 = 整网停在第一层**（旧 bit 上只是慢，因为旧 HLS 还有 k=5 通路；
所以请先在旧 bit 上演练）。k=5 只有 1 层，但**它是最贵的一层**（3.39 s）。如果路由没接上：
轻则整网停（返回码 6，这是**好**结果），重则如果有人在 PS 侧把 k=5 "当成 k=3 传下去"，
会得到"看着正常"的错误输出 —— 后者是本设计一再强调要避免的，所以**不要**在 PS 侧为
返回码 6 做任何"降级重试"。

### 7.2 主 FSM 删状态会不会影响 k=3 的时序？

**没有理由**，但这次不敢说"保证"，理由与可核对项：

1. k=3 的计算/收尾是**独立子模块**（`764_38` / `819_41`），本次没有改它们的端口、循环体、
   pragma、II、latency；k=3 与 k=5 之间没有共享乘法器/累加器（四条路径各自实例化）。
2. 变化只在**顶层 FSM**：state 134 从三分支变两分支，142 与 122–127 消失，
   顶层组合逻辑**变少**。该分支是 `empty_reg_7546` 的一次比较，不在关键路径上。
3. 但 HLS **会重新编号状态与模块实例号**，也会重新做共享/调度 —— k=3 子模块的
   II/资源**可能**因为共享关系变化而小幅变动（上一刀就有 DSP −8 → 实测 −11 这种）。
   ⇒ 任何"按 `LOOP_xxx_yy` / state 号写死"的脚本（cosim 对拍、`docs/rtl_map` 的行号引用）
   **必须重新生成**，不要拿旧编号去找新 RTL。
4. 本次 k=5 子模块自己的时序估算 4.317 ns（预算 6.667 ns），删掉它不会让 k=3 的关键路径变长。

### 7.3 `pl/src_cosim/conv2d_engine.cpp` 是另一份拷贝（**cosim 前必须同步**）

`pl/src_cosim/conv2d_engine.cpp` 现在是 **997 行**（`src/` 是 1135 行），
它连 `K1_IN_HLS` 都没有（`grep K1_IN_HLS` = 0 命中），也就是**含 k=1 + k=5 的 10-23 版**。
本次**没有**改它（按要求）。

⇒ 如果 cosim 还拿那份旧拷贝去跑，会验证到**含 k=1 和 k=5 的旧引擎** → **假绿**
（拍数、资源、行为全对不上，但结果是"对的"）。cosim 前必须：

1. 把 `src/conv2d_engine.cpp` 拷到 `src_cosim/`，只保留 `depth=` 差异；
2. 把 k=1 / k=5 的 testbench 期望改成"返回 5 / 6"；
3. 跑 `python scripts/check_src_sync.py` 确认两边的差异只有 `depth=`。

> `check_src_sync.py` 现在就是 **FAIL**（上一刀之后已经如此，不是本次引入）：
> `conv2d_engine.cpp` / `conv2d_engine.h` / `conv2d_accel.h` 三对都报"逻辑不一致"。

### 7.4 与 `conv_k5_engine` 进 BD 的资源账（跨线风险，**请主控核算**）

这一刀在**引擎 IP 内部**省 25 DSP / 50 BRAM18K / 若干 LUT，但 k=5 层改由手写 IP 承担后，
那块 IP 自己也要吃资源。按 `docs/rtl_hand/conv_k5_engine.md` §0 的**估计**（未综合）：

| 资源 | 本次在引擎 IP 里**释放** | `conv_k5_engine` **成本**（估） | 净值 |
|---|---:|---:|---:|
| BRAM18K | **−50**（= −25 RAMB36） | **+22**（≈11 RAMB36） | **仍赚 −28 BRAM18K（−14 RAMB36）** ✅ |
| LUT | **−4.5k ~ −7.5k**（BD 口径） | +≈7k | **约打平（−0.5k ~ +2.5k）** |
| DSP | **−25 ~ −30** | **+100**（25 tap × 4 oc） | **+70 ~ +75 ❌** |

（引擎 IP 删完 k=5 后的绝对值见 §5.3：LUT 约 11k–14k、FF 约 7.9k–9.4k、DSP 76–81、RAMB36 57–61.5。）

⚠️ **DSP 是这条路线的新墙**：当前 BD 是 `HLS 117 + k1_wrap 33 ≈ 150/160`（2026-10-23 综合报告口径；
`k1_wrap` 瘦身之后未重新综合，DSP 可能略有变化），
`conv_k5_engine` 的 100 DSP 无论怎么腾都放不下。可行方向（都属于另一条线，本次不涉及）：
k5 引擎降 oc lane（4→2 ⇒ ~50 DSP，时间 ×2，仍有 9× 余量）、或复用 HLS 留下的 DSP、
或继续瘦 `k1_wrap`（commit `f7d6a29` 已到 ≈17.2k LUT，下一刀预估 −3.5k~−4.0k LUT）。
**结论：这一刀对 BRAM 是确定的大赚，对 DSP 只是把"缺口"从 +100 变成 +75。**

### 7.5 网络一变，k=5 的"1 层"假设就没了

`conv_k5_engine` 是编译期参数模块，只覆盖 320²/12/48/pad2。若以后网络里有第二个 k=5 层、
或分辨率不再是 320²，HLS 这边**已经没有 k=5 兜底**（要重新例化 RTL，或者临时
`K5_IN_HLS=1` 重新综合）。这条要写进"换网络时的检查清单"。

### 7.6 融合出口（`FUSE_OUT` / `FUSE_SUM`，默认都是 0）

- k=5 分支**自己**那段 `out_lut` / `csum_l[i] +=` 是包在该分支的收尾里的，随 k=5 一起消失；
  `WB_STORE`、`csum_l` 的声明、以及 k=3/k=1 的融合路径都没动，语义不变。
- `FUSE_SUM=1` 时通道上界用的是 `ocbl`（k=1 的那条 `ksize==1 ? ocbl1 : ocbl` 仍被
  `#if K1_IN_HLS` 包着），本次没动。
- `FUSE_*=1` 的构建**只做了语法检查**，没有功能回归（不是本次交付形态）。

### 7.7 累加器上界检查

PS 侧 `check_limits()` 仍按调用参数 `k` 算 `ceil(cin/2)*k²*127*128 < 2^26`；
k=5 已不走 HLS，但这条判定逻辑不需要改（k=3 仍然用它）。

---

## 8. 这次**没有**做的事

- 没有跑 `vitis_hls` / csynth / cosim / 综合 / 实现 / 出 bit（按要求）
- 没有 `git commit`
- 没有改 PS 代码（`runner.py` 的 k=5 路由、`check_consistency.py` 的期望）。**建议后续**：
  把"k=5 回退 HLS"的分支改成硬错误，让问题在上板前暴露，而不是等返回码 6。
- 没有动 `k1_axi_wrap` / `conv_k1_engine` / `conv_k5_engine` 等任何 RTL
- 没有改 `pl/src_cosim/`（见 §7.3）
- 没有清理 `KMAX`：它现在只剩 `rdoff[KMAX]` 的上界在用（k=5 的 25 抽头循环没了），
  降到 3 能省 ~几十 LUT，但会动到"存储上界"与 `conv2d_engine.h` 的约定 —— 留给下一刀。
  `K5_PAD28` **不能删**（权重容器约定，见 §2.5）

---

## 附录 A：命令清单

### A.1 语法（8 组）

```bash
cd pynq_specialized_code/pl
for k1 in 0 1; do for k5 in 0 1; do for c8 in 0 1; do
  echo "=== K1=$k1 K5=$k5 C8=$c8"
  g++ -std=c++14 -fsyntax-only -Wall -Wextra \
      -DK1_IN_HLS=$k1 -DK5_IN_HLS=$k5 -DC8_LAYOUT=$c8 -I src src/conv2d_engine.cpp
done; done; done
```

### A.2 编 4 个功能测试二进制

```bash
cd pynq_specialized_code/pl
g++ -std=c++14 -O2 -DK1_IN_HLS=0 -DK5_IN_HLS=0 -DC8_LAYOUT=1 -I src \
    src/conv2d_engine.cpp src/conv_ref_rt.cpp tb/tb_engine_c8.cpp -o /tmp/tb_k1off_k5off
g++ -std=c++14 -O2 -DK1_IN_HLS=1 -DK5_IN_HLS=1 -DC8_LAYOUT=1 -I src \
    src/conv2d_engine.cpp src/conv_ref_rt.cpp tb/tb_engine_c8.cpp -o /tmp/tb_k1on_k5on
g++ -std=c++14 -O2 -DK1_IN_HLS=0 -DK5_IN_HLS=0 -DC8_LAYOUT=0 -I src \
    src/conv2d_engine.cpp src/conv_ref_rt.cpp tb/tb_engine_c8.cpp -o /tmp/tb_chw_k1off_k5off
g++ -std=c++14 -O2 -DK1_IN_HLS=1 -DK5_IN_HLS=1 -DC8_LAYOUT=0 -I src \
    src/conv2d_engine.cpp src/conv_ref_rt.cpp tb/tb_engine_c8.cpp -o /tmp/tb_chw_k1on_k5on
```

### A.3 单形状手动跑（示例：`conv_first`，k=5）

```bash
# 期望：FAIL: conv2d_engine 返回错误码 6，退出码 2
TB_DUMP_OUT=/tmp/o.bin /tmp/tb_k1off_k5off build/fuzz_c8_data/c006 12 48 5 1 1 2 13
# 一键恢复后：期望 PASS，且 /tmp/o.bin 与归档 golden 逐字节相同
TB_DUMP_OUT=/tmp/o.bin /tmp/tb_k1on_k5on    build/fuzz_c8_data/c006 12 48 5 1 1 2 13
cmp /tmp/o.bin build/fuzz_c8_data/c006/out_11.bin && echo 逐字节一致
```

### A.4 23 形状全量回归

参数表（与 `scripts/fuzz_c8_shapes.py` 的 `cases` 完全一致，期望值由 `k` 与开关状态决定）：

```bash
# 23 个形状：cin cout k s dil H W
cases="12 12 3 1 1 5 6
3 12 3 1 1 5 5
1 1 1 1 1 3 4
2 95 3 2 1 7 7
113 191 1 1 1 3 5
351 95 3 1 1 4 6
12 48 5 1 1 5 5
352 192 1 1 1 4 640
16 16 3 1 1 8 640
96 192 3 2 1 8 8
8 8 5 2 1 9 9
16 8 3 2 1 9 9
24 32 3 1 2 7 7
24 32 3 2 2 7 7
32 8 3 1 3 5 5
32 16 3 2 3 5 5
40 24 5 2 1 6 7
48 40 1 1 1 2 5
56 48 1 2 1 4 33
144 96 3 1 1 3 3
192 64 3 1 1 6 6
17 17 5 1 1 5 5
7 3 1 1 1 2 2"
i=0
echo "$cases" | while read cin cout k s dil H W; do
  d=build/fuzz_c8_data/$(printf 'c%03d' $i)
  pad=$(( (k-1)*dil/2 ))
  TB_DUMP_OUT=/tmp/o_$i.bin /tmp/tb_k1off_k5off $d $cin $cout $k $s $dil $pad 13
  echo "  #$i k=$k rc=$?"
  i=$((i+1))
done
```

> 完整判据（13 个 k=3 形状必须 PASS 且与 `out_11.bin` 逐字节一致；6 个 k=1 → 5；
> 4 个 k=5 → 6；`K1=K5=1` 时 23/23 PASS）由本次的临时脚本检查，
> 逻辑就是"`rc==0 && 输出含『全部一致』 && dump == 归档 out_11.bin`"，
> 或"`rc==2 && 输出含『返回错误码 5/6』`"。

### A.5 对照 csynth（**需要主控跑**，本次按要求没跑）

```bash
cd /media/felix/Data/coding/demoir-fpga

# ① 基线：K5=1（K1 默认 0）。应当**复现**当前的 23,974 / 17,711 / 97 / 166
EXTRA_CFLAGS=-DK5_IN_HLS=1 C8=1 TAG=c8_k5on PROJ_ROOT=/data/work/hls_c8_csynth \
  /data/xilinx/2025.2/2025.2.1/Vitis/bin/vitis-run --mode hls --tcl \
  pynq_specialized_code/pl/scripts/hls_csynth_c8.tcl

# ② 新默认：K5=0
TAG=c8_k5off PROJ_ROOT=/data/work/hls_c8_csynth C8=1 \
  /data/xilinx/2025.2/2025.2.1/Vitis/bin/vitis-run --mode hls --tcl \
  pynq_specialized_code/pl/scripts/hls_csynth_c8.tcl

# ③ 逐项对差（四个表都要看，不要只看 Total）
R=/data/work/hls_c8_csynth
for t in c8_k5on c8_k5off; do
  echo "===== $t"
  grep -E "^\|(Total|Utilization)" $R/$t/solution1/syn/report/conv2d_engine_csynth.rpt
  grep -E "LOOP_(892_47|865_44|849_43)|wgt_buf" \
      $R/$t/solution1/syn/report/conv2d_engine_csynth.rpt | head -8
done
# 期望：c8_k5off 里这四条全部**不存在**
#   LOOP_892_47 / LOOP_865_44 / LOOP_849_43 / wgt_buf_U…wgt_buf_24_U（以及 Memory 表的 50 块 BRAM）
```

### A.6 可逆性预处理 diff

见 §3 的命令（`g++ -E -P` 两版对比，期望只差 `ERR_K5_UNSUPPORTED = 6`）。

---

## 附录 B：改动文件

- `pynq_specialized_code/pl/src/conv2d_engine.cpp`（1076 → 1135 行）
- `pynq_specialized_code/pl/src/conv2d_engine.h`（302 → 343 行）
- `pynq_specialized_code/pl/src/conv2d_accel.h`（90 → 96 行，只加错误码）

相关文档：

- `docs/部署/HLS移除k1分支.md`（第一刀，同一套方法论）
- `docs/rtl_map/03_weight_path.md`（`wgt_buf` 的 25 bank / 50 BRAM 与写相位）
- `docs/rtl_map/06_k5_and_primitives.md`（`LOOP_892_47` 逐段拆解；⚠️ §1.4 有一句
  "同时服务 k=1/k=3/k=5" 需按本文 §2.4 更正）
- `docs/rtl_hand/conv_k5_engine.md`（手写 k=5 引擎的契约与资源**估计**）
- `docs/部署/k1_IP接入.md`（k=1 的路由/寄存器接入，k=5 将来照同一模式接）
