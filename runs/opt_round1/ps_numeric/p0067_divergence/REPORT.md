# p0067「板端 vs 主机 int8」差异定位报告（纯主机，第一轮）

**任务**：把 p0067 的板端/主机差异定位到**第一个发散的层**。
**边界**：不 ssh、不碰板、不跑 Vivado/xsim；`pynq_specialized_code/` 与既有 `runs/` 产物**零修改**；
本目录 100% 新增，所有脚本只 **import** 生产代码（`bin_net` / `bin_ref_numpy` / `bin_engine`）。
**时间**：2026-11-04 21:22–21:40（约 18 分钟机时，9 次整网 + 6 次探针跑，单次 20–33 s）。

---

## 0. TL;DR（先看这四行）

1. **三个单变量 A/B 全都不是原因**：只有 `BIN_PACK=ps` + `BIN_RTL_ROUND=1` + `BIN_CONV_OOB0=1`
   这一档与板端在同一量级（**145972/196608 不同，max|Δ|=11，带符号均值 −0.411**，即板端更高 0.411 LSB）；
   换掉任何一个轴 → **196608/196608 全不同**、`mean|Δ|` 19.5 / 103 / 118 LSB、`max|Δ|` 101~207。
   7 个"错档"与板端差 15~100 倍，且它们与 float golden 的距离也同步崩掉（PSNR 25→7.5）。
2. **所以板端的定点语义 = 主机参照（`ps` + R1 + OOB0=1）**，差异不在"哪条定点路径"，而在**同一条路径上的实现细节**。
3. **主机侧已把差异的性质钉死**：`out1` 的 f32 本体就差（max 0.0453 / 均值 +0.00161），不是 u8 取整问题；
   而另外 4 张的 3~7 个像素差**恰好等于 `rint` 半格边界像素数**（p0000: 4↔7，p0021: 6↔6，p0043: 6↔3，p0089: 1↔3）
   ⇒ 那 4 张是"显示取整糊掉了 <1 ULP 的差"，p0067 是**真差**。
4. **级联放大已量化**：给输入加 **1 个 ULP**，p0067 的 `out1` 变 **6262 px / max 4**，p0000 变 **0 px**；
   1-ULP 下的逐层翻转剖面显示 **前 189 层 int8 逐字节相同**，第一次翻 bit 在
   **`decoder.preconv_1.conv`（idx 189，3 个元素）**，之后再放大到 6262 px。
   ⇒ 板端的 145972 px 不是"最后一跳"问题，需要板端逐层 sha1 才能定上界，故出 `BOARD_REQUEST.md`（12 项，先只回 sha1）。

---

## 1. 口径（对拍前必须认同）

| 项 | 值 |
|---|---|
| 容器 | `runs/opt_round1/ps_numeric/pack_v60/`（`bin_weights.bin` md5 `706a1d22dfbbf33cf7af7940b9de54c4`；`round_q=0`、`slope_shift=8`、`alpha_shift=9`、227 层） |
| 输入 | `runs/frozen256/p0067_crop256.npy`（uint8 `(256,256,3)`，`min 75 / max 255 / mean 247.5`）→ `/255` → f32 `(3,256,256)`，与板端 `in_sha1=c13eb436ed6f47…` 同源 |
| 后端 | `bin_net.BinNet(PACK, engine=None)` ⇒ `_RefBackend` ⇒ `bin_ref_numpy.conv_rtl`（RTL 逐位复现） |
| 流水 | `pipeline=True, nbuf=2`（与板端一致） |
| out1 | `(3,256,256)` float32；`u8 = rint(clip(out1,0,1)*255)`，与 `board_verify_numfix.u8()` 逐字相同 |
| 板端 u8 | `/tmp/t165_logs/board_out/p0067_out1_u8.npy`，**HWC `(256,256,3)`** ⇒ 比较前 `transpose(2,0,1)` |
| 板端 f32 | `/tmp/t165_logs/board_out/p0067_out1_f32.npy`，`(3,256,256)` float32 |
| 符号 | 本报告 `带符号均值 = 主机 − 板端`（所以 −0.411 ⇔ **板端比主机亮 0.411 个 u8 级**，与题面 +0.411 同一事实） |

复算入口：`python3 diverge_probe.py --mode ab --tag p0067`（8 组合）、`--mode pack-ab`、
`--mode dump`（逐层落盘）、`--mode margin`、`--mode impl`、`--mode flipchain --eps 1`、`--mode fpdump`。

### 1.1 `BIN_PACK` 轴在主机上必须"等价模拟"（**这是唯一的口径陷阱**）

* `BIN_PACK=e1e2` 的启用点在 `bin_net.py:3116-3124`，**前提 `self.engine is not None`**；
  主机 `engine=None` ⇒ 该环境变量 **纯 no-op**。
  实测（`ab/pack_ab_p0067.json`）：`ps` 与 `e1e2` 的 `sha1(out1_u8)` 都是 `2d699f822df7…`，
  `sha1(out1_f32)` 都是 `2b1cd63cc7ef…`，**逐字节相同**。
* 但 e1e2 的**数值语义**是"阈值落在 int8 网格"（`bin_engine.py:4081 pack_mode()`，
  判据 `[x_int8 + THR > 0]`），而 `ps` 是"f32 阈值"（`bin_engine.py:4085 pack_thresh()`，
  判据 `x > −t_f32`，`t` 来自容器 `off_thr32`，`bin_ref_numpy.py:223 thr_f32()`）。
  ⇒ 用 **`BIN_THR_F32=0`**（`bin_ref_numpy.py:237`：强制退回 int16 `t_q`）**等价模拟 e1e2**；
  这两条判据的差就是"亚 LSB 阈值"，也是 `bin_net.py:1128-1133` 里 `t32` 的唯一用途。
* 因此下表的三轴是：**pack = `BIN_THR_F32`(1=ps / 0=e1e2)**、**round = `BIN_RTL_ROUND`**、**oob0 = `BIN_CONV_OOB0`**。

---

## 2. ① 逐层落盘（主机，227 个 int8 层 + 4 个 f32 层）

```
host/000__encoder.encoder_1.rdb.conv_layers.0.conv.npy   … 227 个 int8 (C,H,W) C 序
host/fp__encoder.conv_first.0.npy                        … 4 个 f32 卷积输出
host/layers_index.json     每层 {i, shape, dtype, file, sha1, fs_out, stats}
host/fp_layers_index.json  4 个 f32 层 {shape, bytes, sha1, min/max/mean/std}
host/out1_f32.npy / out1_u8.npy
```

* 体量：227 层 int8 = **41.2 MB**；f32 4 层 = 4.1 MB。
* 探针实现（`diverge_probe.install_probes()`）：`conv` 层直接截 `bin_ref_numpy.conv_rtl`
  的第一个返回值 = **反量化前的 `sat8` int8**（`bin_ref_numpy.py:508-521` 的 epilogue 输出）；
  非 conv 层截 `BinNet._exec_ps` 的 f32 返回值。两个钩子都不改数值（只读）。
* `stats` 里除 min/max/mean/std 外还记：`n_lo`(=`−128`)/`n_hi`(=`127`)/`n_zero`/`n_clip`
  （`n_clip` = 未饱和前的 `r>>shift` 越出 int8 范围、被 `sat8` 砍掉的元素数）+ `max_pre/min_pre`。
* `host/` 的每层 sha1 就是 `BOARD_REQUEST.md` §3 那 12 行的期望值。

**板端逐层信息**：`/tmp/t165_logs/` 里**没有**任何逐层张量/逐层哈希 ——
`dump5.log` 只有 `out1_u8`/`out1_f32` 的 shape/统计/sha1 与 vs-golden 指标；
`analyze.py`、`frames_*.jsonl` 是内存/时延口径。⇒ 已出 `BOARD_REQUEST.md`（**12 项，先只回 sha1**）。

---

## 3. ② 三个单变量 A/B（p0067，全部主机）

`--mode ab`，8 个组合，每档一次整网（19.6–33.4 s）。
"不同/ max|Δ| / 带符号均值" = 与**板端** `out1_u8` 逐像素；"golden" = 与 `runs/frozen256/golden/p0067_out1_fp.npy`。

| # | pack | R1 | OOB0 | 与**板端**不同 | max\|Δ\| | 带符号均值 | mean\|Δ\| | 与 golden mean\|Δu8\| | PSNR | corr | σ比 |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | **ps** | **1** | **1** | **145972 / 196608（74.25%）** | **11** | **−0.411** | **1.353** | 28.16 | 18.69 | −0.472 | 2.32 |
| 2 | ps | 1 | 0 | 196608（100%） | 101 | −19.53 | 19.53 | 11.55 | 25.04 | −0.445 | 2.88 |
| 3 | ps | 0 | 1 | 196608（100%） | 169 | −118.55 | 118.55 | 90.01 | 9.02 | +0.034 | 2.04 |
| 4 | ps | 0 | 0 | 196608（100%） | 189 | −117.80 | 117.80 | 89.27 | 9.08 | −0.011 | 2.39 |
| 5 | e1e2 | 1 | 1 | 196608（100%） | 160 | −102.81 | 102.81 | 74.27 | 10.66 | −0.506 | 1.96 |
| 6 | e1e2 | 1 | 0 | 196608（100%） | 160 | −103.19 | 103.19 | 74.65 | 10.61 | −0.547 | 2.05 |
| 7 | e1e2 | 0 | 1 | 196608（100%） | 205 | −134.48 | 134.49 | 105.95 | 7.55 | −0.155 | 4.10 |
| 8 | e1e2 | 0 | 0 | 196608（100%） | 207 | −133.22 | 133.22 | 104.68 | 7.66 | −0.241 | 3.94 |

**单个轴的效果（其余两轴固定在 "ps/1/1"）**

| 轴 | 换掉后与板端 | 与板端 mean\|Δ\| 变化 | 结论 |
|---|---|---:|---|
| `BIN_CONV_OOB0` 1→0 | 145972 → 196608 | 1.353 → 19.53（**14×**） | 板端**确实**做了越界补丁（OOB0=1）；否则每层边界一圈全错 |
| `BIN_RTL_ROUND` 1→0 | 145972 → 196608 | 1.353 → 118.55（**88×**） | 板端**确实**做了 R1；容器 `round_q=0` 与之配对正确（无双补偿） |
| `BIN_PACK` ps→e1e2 | 145972 → 196608 | 1.353 → 102.81（**76×**） | 板端**确实**吃的是 f32 阈值（`off_thr32`）；与板端日志 `[DEPS] pack=ps` 一致 |

**判读（这一节就是任务 ① 要的"板端更接近哪个组合"）**：
板端唯一接近的是 **#1 = 主机参照本身（ps + R1 + OOB0=1）**，残差 74.25% / max 11 / +0.411 LSB；
其余 7 档都在 **100% 像素全不同**、幅度大 1~2 个数量级，且**与 golden 的距离同步变差**
（PSNR 18.69 → 7.55~25.04，而板端 vs golden 实测 18.57 dB / mean|Δu8| 28.57，
与 #1 的 18.69 / 28.16 只差 0.12 dB / 0.41 LSB）。
⇒ **板端跑的就是 #1 的定点语义；p0067 的差异不能由这三个开关解释**，三选一这条路到此关闭。

> 顺带钉死一个反向事实：`oob0=0` 与 golden **更近**（11.55 vs 28.16），板端却远得多（19.53）
> ⇒ 说明这份 golden（`bin_fp_ref`，PyTorch 补 0 语义）与 **v60 位流的越界语义**本就不是同一条，
> 拿 golden 当"板端应该长什么样"的判据在边界上会误导；对拍只能用主机 int8 参照。

**组合之间第一个发散的层**（`divergence` 段，12 对两两比较）：
三个轴**每一对**的第一个 sha1 不同层都是 **`encoder.encoder_1.rdb.conv_layers.0.conv`（idx 0）**。
这符合机制：R1 每层都改 `+0.5 LSB`、OOB0 从第一层的边界就改、阈值网格从第一次二值化就改 ⇒ 三个开关都是"从第 0 层起就不同"。
**它反过来说明：板端若也是这三种"从 0 层起就不同"的错档之一，差异必然是 100%/大幅度，而实测不是。**
所以板端的差异**不是**"某层起系统性偏 1 LSB"，而是**晚发、局部**的类型——只能靠板端逐层 sha1 定位。

---

## 4. ③ 差异性质：三个主机侧定量实验

### 4.1 另外 4 张的 3~7 px 是 `rint` 半格 tie，不是实现差异

`out1` 的 f32 本体：`max|Δ|=0.0453`、均值 `+0.00161`（板端更高）、std `0.00685`。
若把它换成 u8，落在 `X*255` 小数 ≈ .5 的像素（`|frac−0.5|<1e-5`）在 5 张上分别是
**4 / 6 / 6 / 7 / 1**，与实测 board−host 的 **7 / 6 / 3 / 3 / 145972** 同量级（4 张"好"的样本）
⇒ 这 4 张的 u8 差就是**显示取整的 tie**，f32 层要么完全相同、要么差 <1 ULP；
而 p0067 是 **145972**，比 tie 数大 2 万倍 ⇒ **p0067 是真的算不同了**。

### 4.2 级联剖面（`--mode flipchain --eps 1`）：1 ULP 输入差在主机上怎么变成 6262 px

给输入加 **1 个 ULP**（`np.nextafter`），逐层比 int8 的 sha1：

| 样本 | out1 不同 | max\|Δ\| | 第一个 int8 翻转层 | 翻转层数 |
|---|---:|---:|---|---|
| **p0067** | **6262（3.18%）** | **4** | **`decoder.preconv_1.conv`（idx 189）** | 36 / 227 |
| **p0000** | **0** | 0 | 无（227 层全同） | 0 / 227 |

p0067 的放大链条（`ab/flipchain_p0067_ulp1.json`）：

```
idx 189  decoder.preconv_1.conv                         3 / 1048576  (0.00%)   ← 第一粒 bit
idx 190  decoder.decoder_1.rdb.conv_layers.0.conv     188 / 1572864 (0.01%)
idx 191  …rdb.conv_layers.1.conv                      602 / 1572864 (0.04%)
idx 192  …rdb.conv_layers.2.conv                     3574 / 1572864 (0.23%)
idx 200  …sam_blocks.0.b0.ghost_layers.1.primary_conv 1237 /  786432 (0.16%)
…                                                                        ↓
out1                                                  6262 / 196608  (3.18%)
```

三条可直接引用的结论：
* **前 188 层（整个 encoder + decoder_3/2）对 1 ULP 完全不敏感**：int8 逐字节相同 ⇒
  上游亚 LSB 的浮点差在 p0067 上是"**潜伏**"的，不会在 encoder 露出；
* 放大全部发生在 **`decoder_1`（最后 38 层，通道 448/96、fs_out=2~3）**，**3 个 bit → 6262 px（≈2000×）**；
* **样本依赖性有量化对照**：同样 1 ULP，p0000 是 **0** px ⇒ "4/5 张几乎逐字节一致、只有 p0067 崩"
  完全可以用放大因子差异解释，**不需要假设板端有两条不同的代码路径**。

### 4.3 把浮点实现换掉（`--mode impl`）：同类扰动同样落在 6262

| 变体 | 做了什么 | 与 base 的 out1 差 | 第一个 sha1 不同层 |
|---|---|---:|---|
| base | 参照 | — | — |
| `convpf_c` | `bin_ref_numpy.CONVPF_C=True`（直接卷积 C 内核） | 0（x86 上内核拒绝启用，打印 `非 ARM 平台 ⇒ 回落`） | — |
| `interp_np` | `bilinear_f32 → _bilinear_f32_numpy` | **0** | — |
| `conv_ref` | `conv2d_f32_np → bin_net.conv2d_f32_ref`（另一份 f32 卷积实现） | **6262 / max 4** | **`encoder.conv_first.0`** |
| `ulp_in` | 输入 +1 ULP | **6262 / max 4** | `input:pixel_unshuffle(2)` |

**意外的强结果**：上面两种**完全不同来源**的 1-ULP 级扰动（一个改输入、一个换 f32 卷积实现），
产出的 `out1` **逐位相同**（`np.array_equal(ulp, conv_ref) = True`，复算脚本：`eqcheck_ulp_vs_convref.py`）。
⇒ 级联对"扰动来自哪里/方向如何"不敏感，只对"**有没有跨过那一个最贴边的判据**"敏感：
所有 ≥1 ULP 的扰动都会在 `decoder.preconv_1.conv` 翻**同一批** bit。
这也解释了为什么板端/主机差异只有**一种稳定形态**（74%、max 11），而不是随机跳动。

---

## 5. ④ 机制假设（每条都带落点与判定实验）

> 记法：`[H?]` 后面的数字是"支持度"，括号里是**主机侧已有的证据**。
> 三条互不排斥，`BOARD_REQUEST.md` 的 12 项 sha1 一次就能把范围压到其中一条。

### H1（支持度最高）板端与主机在**第一次二值化之前**的 f32 值域上就有亚 LSB 差，p0067 把它放大

* **落点**
  * `bin_ref_numpy.py:1091 conv2d_f32()` —— 4 个 fp 卷积走 im2col + `gemm_split_m`（BLAS）；
    x86 OpenBLAS 与板端 numpy 1.21.5 的参考 BLAS **求和序不同**，末位必不同（1e-7 相对）。
    调用于 `bin_net.py:287-293`（`conv2d_f32_np`）/ `bin_net.py:2326-2330`（`_fp_conv`）。
  * `bin_ref_numpy.py:797 bilinear_f32()` 的 C 内核（`BIN_BILINEAR_C`，默认开）
    与 `:826 _bilinear_f32_numpy()`；`np.exp`（ECA，`bin_ref_numpy.py` `eca_attn_f32*`）
    在 numpy 1.21.5 与 2.2.6 上**都不是逐位同实现**。
  * 判据本身是严格比较：`bin_engine.py:4085 pack_thresh()` → `quant_pack(x, adj)` 的 `x > adj`（f32）。
* **证据**：§4.2/§4.3（1 ULP → 6262 px；p0000 → 0 px）；§4.1（4/5 张只差 rint tie）。
* **证实**（板端，只需第 1 项）：导出 `fp:encoder.conv_first.0`，与 `8b70516715ca` 比。
  **不同** ⇒ 起点在第一次二值化之前，H1 成立；随后用它的差值量级在主机上加等效扰动即可复现。
* **否证**：第 1 项 sha1 **相同** ⇒ 上游 f32 完全一致（至少到 1 ULP 级），转 H2。
* **主机侧可先做**：`BIN_BILINEAR_C=0` / `BIN_CONVPF_C_FORCE=1` 各跑一次（本报告已跑前者=0 差，
  后者在 x86 上被拒），可再叠加 `OPENBLAS_NUM_THREADS=1` 换 gemm 求和分组。

### H2（次高）某个 **int8 层的 epilogue** 在 p0067 的特定值上算出 1 LSB 差，被 `decoder_1` 的 fs_out=2~3 放大

* **落点**：`bin_ref_numpy.py:508-521`（`conv_rtl` 的 epilogue）
  `q = (dint*alpha_q) >> alpha_shift`（**截断**右移，负数向 −∞）→ `+bias` → `−zeta`
  → RPReLU 两支 → `sat8((r + half) >> shift)`（R1-B）；RTL 侧同一份数学在
  `pl/rtl_hand/*/bin_conv_core.v` 的 R1-A（`gamma_l <= mem_gamma + half_c`，装载拍）。
  这三处里任何一处的位宽/舍入与主机不同，都会**只在该层**产生 ±1 LSB。
* **证据**：`--mode margin`（`ab/margin_p0067.json`）——p0067 上"判据余量"最薄的层全在 `decoder_1`：
  `decoder_1.rdb.conv_layers.1.conv` 36.1% 的元素余量 <0.1 LSB（16 384 个 <1e-3 LSB）、
  `conv_layers.2` 34.0%、`decoder_2.rdb.conv_layers.2` 33.0%、`decoder_3.rdb.conv_layers.2` 32.7%，
  且这些层输入里 **52~72% 是精确 0**（上一级死区），一个 LSB 的翻转在这类张量里是"成片"的。
* **证实**：板端第 11/12 项（`decoder.preconv_1.conv` / `…sam_blocks.0.b4.conv.conv`）sha1 不同，
  且第 2~10 项相同 ⇒ 起点锁定在 `decoder_1` 段；再按 `BOARD_REQUEST.md` §5 取该层的
  `packed A` + `dint`，主机可把 `conv_rtl` 拆三段对拍，**不用猜**。
* **否证**：若第 5 项（`encoder.encoder_1.rdb.conv_post.conv`，早期唯一饱和层 `n_clip=142 / n_lo=311`）就先不同 ⇒ 起点在 encoder，H2 的"decoder_1 段"版本错，改为查该层 epilogue。

### H3（低，但必须排除）**部分层**实际走了 int8 网格阈值（丢亚 LSB），只在高亮低对比图上翻 bit

* 为什么不直接用 ② 否掉：② 只证明"整网 e1e2"不可能（100% / max 160）；
  但 `BIN_LAZY_DEQ` + C8 直读会让 **C8 直读的那几层**用 `x_int8 + THR > 0`
  （`bin_net.py:3126-3133` 的 `_lazy_deq` 要求 e1e2 引擎，`:1128-1133` 的 `t32` 分叉）。
  只要板端某次 `enable_e1e2()` 被间接调用过，就会出现"少数层换域、其余层不变"的混用，
  其差异形态正是"局部、晚发、1 LSB 级"。
* **落点**：`bin_net.py:3116-3124`（e1e2 启用点）、`bin_net.py:3126-3133`（`_lazy_deq` 判定）、
  `bin_engine.py:4081 pack_mode()`、`bin_engine.py:812-814`（`_pack_env` 在**构造时**缓存）。
* **证实**：板端在 12 项导出那次 run 里打印 `net.engine._e1e2`（应 `None`）与
  `net._lazy_deq`（应 `False`）；并确认 `BIN_PACK` 没有在 `BinNet.__init__` 之后被改写。
* **否证**：两者都是 `None/False` ⇒ H3 排除（这也是 `bo_unrelated` 的正确默认状态）。

### 已排除的假设（写在这里省得下一轮重复论证）

| 假设 | 否证依据 |
|---|---|
| 容器不同（run100 用过被改写的 `bin_weights.bin`） | 若容器不同，5 张都会大面积差；实测 4 张只差 rint tie（§4.1），且 `in_sha1` 5/5 命中 |
| 板端没打 OOB 补丁 | `BIN_CONV_OOB0=0` ⇒ 100%/max 101/DC −19.5，比实测差 14 倍（§3） |
| 板端 R1 双补偿或漏补偿 | `BIN_RTL_ROUND=0` ⇒ 100%/max 169/DC −118.6（§3）；容器 `round_q=0` 与 R1 配对正确 |
| 板端走 e1e2 打包 | 100%/max 160/DC −102.8（§3）+ 板端日志 `pack=ps` |
| 流水/nbuf/其它 RUNPOINT 键 | `host_int8_ref/REPORT.md` §1 已实测：只有 4 个键进数值，`pipeline` 不改变数值 |
| float 显示取整口径不同 | 板端 u8 = `rint(clip*255)` 与主机逐字相同（`board_verify_numfix.py:48`） |

---

## 6. 需要板端做什么（**12 项，先只回 sha1**）

见 **`BOARD_REQUEST.md`**：清单 = `fp:encoder.conv_first.0` + **11 个** int8 conv 层
（idx 0/1/2/3/5/6/37/75/113/189/226），总 **9.5 MB**；**先说 sha1（12 行）就能定第一处发散层**，之后只需 scp 那 1~2 个 `.npy`。
选层原则：idx 0（第一层）、fs_out 最小的早期层（idx 2，1 LSB=128 灰阶）、
早期唯一饱和层（idx 3）、死区最重的层（idx 5）、三处形状切换（idx 6/37/75/113）、
**主机 1-ULP 剖面的第一个翻转层（idx 189）**、最后一个 int8 层（idx 226）。
（`fp:decoder_1.conv` 不用导出：`out1 = pixel_shuffle(它, 2)`，板端已回传的 `out1_f32` 就是它。）

---

## 7. 复跑

```bash
cd runs/opt_round1/ps_numeric/p0067_divergence
python3 diverge_probe.py --mode ab        --tag p0067   # 8 组合 ≈160 s
python3 diverge_probe.py --mode pack-ab   --tag p0067   # 证明 BIN_PACK 在主机上是 no-op
python3 diverge_probe.py --mode dump      --tag p0067   # 227 层 int8 + 4 层 f32 ≈21 s / 45 MB
python3 diverge_probe.py --mode margin    --tag p0067   # 逐层判据余量 ≈29 s
python3 diverge_probe.py --mode impl      --tag p0067   # 实现换血 / 输入 1 ULP ≈106 s
python3 diverge_probe.py --mode flipchain --tag p0067 --eps 1   # 逐层翻转剖面 ≈45 s
python3 diverge_probe.py --mode fpdump    --tag p0067   # 4 个 f32 层 ≈21 s
```

产物：`ab/ab_p0067.json`、`ab/pack_ab_p0067.json`、`ab/margin_p0067.json`、`ab/impl_p0067.json`、
`ab/flipchain_p0067_ulp1.json`、`ab/flipchain_p0000_ulp1.json`、`ab/pk*_out1_{f32,u8}.npy` ×8、
`host/`（227+4 层 + index）、本报告、`BOARD_REQUEST.md`。

## 8. 没做 / 限制

* **没有板端逐层数据** ⇒ 本节所有"第一个发散的层"都是**主机侧对照物**（组合之间 / 1-ULP 扰动之间），
  **不是** board-vs-host 的第一发散层。后者需要 `BOARD_REQUEST.md` 的 12 个 sha1。
* 主机 x86 上 `CONVPF_C` 被内核自身拒绝（`非 ARM 平台 ⇒ 回落`），所以"另一份 f32 卷积实现"用的是
  `bin_net.conv2d_f32_ref`（im2col+fancy-index 老路径），不是真的 ARM NEON 内核。
* 未测 BLAS 线程数/亲和性对 gemm 求和序的影响（`OPENBLAS_NUM_THREADS=1` 需在 import numpy 前设，
  本轮时间盒内没做）。
* 未对 `pl/rtl_hand/` 的 `bin_conv_core.v` 做位宽审计（`dint` 累加位宽、`alpha_q` 乘法位宽）——
  这是 H2 若被板端 sha1 支持后的下一步（**纯读 RTL，不需要跑 xsim/Vivado**）。
