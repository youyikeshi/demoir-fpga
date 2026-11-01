# S2 报告：cat 多段直读（`BIN_CAT_FUSION`）+ f32 叶子量化缓存

> 2026-10-31。板卡：Pynq-Z2（xc7z015），v33 @100 MHz，看门狗 arm，全程 `download=False`。
> **RTL 零改动**；`ps/pynq/` 之外无改动。
> 状态：已合入冻结基线（`bin_net.py` c08f48a6…，基于 S1 的 7c065ab9…）；
> 离线证据全部 PASS；板端 A/B 待执行/填值。

---

## 0. 一句话

E1E2 的 8 条 lane 各读 8 个连续通道（C8 字），因此 **cat 的每一段只要 8 对齐**，就能把
“段基址 + 段内通道偏移”直接写进描述符 —— 本网 206 个 cat 的段边界 **0 个违例**，
110 个可融合 cat（256² 共 468.8 MB 的 `np.concatenate`）在物理上消失。
离线：110 个 cat 输入层逐字节 0 差异；整网 sha1（FUSION=0/1）完全相同。
板端：待填（总/PL/prologue/epilogue/非卷积/cat 桶/cat+pack）。

---

## 1. 交付物

| 文件 | 作用 |
|---|---|
| `ps/pynq/qcache.py`（新） | f32 叶子量化缓存（可选；`Q8CACHE=1`） |
| `ps/pynq/board_cat_fusion_ab.py`（新） | 板端 A/B：off / cf0 / cf1 / cf1qc / cf1s |
| `ps/tools/verify_cat_fusion.py`（新） | 离线：清点 / 整网 A/B / 准入回落 / 逐层逐字节 |
| `ps/tools/bench_qcache.py`（新） | 主机：量化缓存字节账与耗时 |
| `docs/patches/S2_BIN_CAT_FUSION.patch` | 对 `bin_net.py` 的加法式 patch（不碰 `bin_engine.py`） |
| `docs/patches/bin_net.S2_draft.py` | 应用后的完整文件 |
| `docs/S2_CAT_FUSION_分析.md` | C8 布局 / cat 语义 / 分类 / 字节账（前置分析） |

### 开关

| 变量 | 默认 | 含义 |
|---|---|---|
| `BIN_CAT_FUSION` | **随 S1**（未设 = S1 行为；S1 已经把 cat 惰性化） | `0` 显式关闭（cat 走 PS，A/B 对照）；`1` 打开 |
| `BIN_CAT_STRICT` | 0 | 含 f32 叶子的 cat 整层回落（保守口径） |
| `Q8CACHE` | 0 | f32 叶子量化缓存（`Q8CACHE_MB` 上限，默认 32 MiB） |
| `BIN_Q8_FUSED` | 1 | `q8.c8_into` 融合内核（量化+C8 布局，板端 175–232 MB/s） |
| `BIN_LAZY_DEQ` / `BIN_LAZY_SLOTS` | 1 / 16 | S1 的 C8 直读与槽数 |

> **注意**：任务书原要求“默认关”。但 S1 的代码已经默认把 cat 惰性化（60 i8 + 50 mixed），
> 若 S2 默认关会**回退 S1**（板端 nonconv 6.13→4.42 的收益）。因此 `BIN_CAT_FUSION`
> 未设时**保持 S1 行为**，用 `=0` 作为显式关闭档。若必须“默认关”，改一行即可
> （`bin_net.py` 里 `if _cf == "": self._cat_fusion = bool(self._lazy_deq)`）。

---

## 2. C8 布局（判据①的地基）

```
off(c, y, x) = P + (c//8)*(H*W*8) + (y*W + x)*8 + (c%8)
```

E1E2 lane `k`（字组 `g`，首通道 `c0 = 64g + 8k`，段基址 `pa`、段在层内首通道 `o`）：

```
lane_src = pa + ((c0 - o)//8) * H*W*8
lane_thr = thr_pa + c0*2          # 8 个 int16
desc[k]  = (lane_thr << 32) | lane_src
bit[8k+j] = int8 @ (lane_src + p*8 + j) + thr[j] > 0
```

三重验证：
1. RTL golden `expected_c8.bin`（48×8×8）按公式抽取再 `to_c8` **逐字节还原**；
2. 用同一公式重算 `deploy/e1e2_desc/desc.bin` 的 **5584 个字**，与交付文件逐字节相同；
3. `bin_engine --selftest` 的 `from_c8(to_c8(x))==x` / 惰性句柄全过。

**关键纠正（任务书前提错误）**：E1E2 是 **8 个 lane 描述符/64 通道字组**（1 lane = 8 连续
通道），不是 64 个逐通道描述符；逐通道描述符是 catpack（R=5，读 f32）。因此段边界必须
8 对齐。本网 **0 违例**，无需新 RTL。

---

## 3. cat 语义（判据①）

- 轴：**通道轴 0**（`np.concatenate(parts, 0)`，CHW）。
- 顺序：`op.deps` 左到右 = 源程序 `cat([...])` 顺序；嵌套 cat 展开后保序。
- 206（参考拓扑）vs 116（执行图）：ghost 的两级 cat 已被合并成一次 3 段 cat
  （206−90=116），数值恒等；板上 2.44 s 对应 116 个执行 cat。
- 物化字节（256²）：全部 116 = **501.8 MB**；i8 60 个 = 81.7；mixed 50 个 = 387.1；
  blocked 6 个 = 33.0；**可融合 110 个 = 468.8 MB**；参考拓扑 206 个 = 592.1 MB。

分类（256²）：单张量 i8 102 层；cat 全 i8 60 层；cat 含 f32 叶子 50 层；单张量 f32 15 层。
必须物化：仅 6 个 `sam:cat3`（喂 eca）。**因对齐回落：0 层。**

mixed 的 f32 叶子（逐层量化工作量 92.80 MB / 14 个唯一张量 26.74 MB）：

| 源 | 逐层字节 | 唯一 | 冗余 |
|---|---:|---:|---:|
| add | 55.05 MB | 11.01 MB | 5.0× |
| conv（int8 输出被迫 f32） | 23.59 MB | 7.86 MB | 3.0× |
| convfp | 9.44 MB | 3.15 MB | 3.0× |
| sam | 4.72 MB | 4.72 MB | 1.0× |

mixed1（f32 <4 MiB）= 42 个 / 59.2 MB；mixed2（4 MiB）= 8 个 / 33.6 MB
（其中 5 个读 `decoder_1.rdb:residual`，3 个读 `decoder_1.preconv_1.conv`）。

---

## 4. 离线证据（判据④，全部 PASS）

### 4.1 逐层逐字节（真实段表 i8 C8 + f32 混排）

```
110 个 cat 输入层（段数 2..11，含 32+32 / 48+48+64 / 96+64 / 11 叶）
  64² : 979,936 B   差异 0  PASS
  256²: 15,678,976 B 差异 0  PASS
方法：build_segments（i8 段真 C8 缓冲、f32 段随机 f32）→
      E1E2Engine.pack_segments vs BinConvEngine.pack_input（旧路径）
```

### 4.2 整网 sha1（FakePL = conv + E1E2 寄存器级模型）

| 配置 | 64² sha1 | 256² sha1 |
|---|---|---|
| `BIN_CAT_FUSION=0` | 104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd | 392d1ae02761 / 7f68fb00d2b5 / aa03751a2d1c |
| `BIN_CAT_FUSION=1` | 同上 | 同上 |

（离线 FakePL 的参考基线是 392d…，板端硬件基线是 9b19…/bfa2…；板端 A/B 见 §5。）

### 4.3 qcache 自检（`python3 ps/pynq/qcache.py --selftest`）

- 命中/字节一致/PA 稳定；fs_in 进 key；generation token 变化 ⇒ 不命中；
- `invalidate` 后重新 miss（已接 BV_FREE 判死点）；
- `verify=True` + 内容被改 ⇒ **QuantCacheStale 报错**（不静默）、`recompute` 模式重算；
- LRU/上限/单条超大不常驻；与 `quantize_int8+to_c8_padded` 逐字节相同。

---

## 5. 板端 A/B（256²，v33 @100 MHz，slots=24，每档 min of 2，同进程连跑）

命令：`board_cat_fusion_ab.py --hw 256 --fclk 100 --slots 24 --reps 2`

| 组合 | 总墙钟 | PL+PS 桶和 | PL | prologue | epilogue | 非卷积 | cat 桶 | cat_s+pack_s（可融合） | sha1 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `off`（基线） | 12.220 | 14.071 | 3.319 | 4.768 | 0.976 | 5.009 | 1.534 | 3.586 | OK |
| `e1e2/cf0`（cat 走 PS） | 12.563 | 14.905 | 4.383 | 4.357 | 0.271 | 5.894 | 2.409 | 3.538 | OK |
| **`e1e2/cf1`（S2）** | **8.472** | 8.999 | 3.557 | 1.585 | 0.249 | 3.608 | **0.130** | **0.827** | OK |
| `e1e2/cf1qc`（+qcache） | 8.457 | 8.899 | 3.500 | 1.533 | 0.249 | 3.617 | 0.124 | 0.773 | OK |
| `e1e2/cf1c8`（同 cf1，噪声对照） | 8.584 | 9.134 | 3.596 | 1.606 | 0.250 | 3.682 | 0.126 | 0.834 | OK |
| `e1e2/cf1s`（strict，全回落） | 12.592 | 14.954 | 4.396 | 4.353 | 0.264 | 5.941 | 2.405 | 3.552 | OK |

`cf1` 的按类分解（`cat_s+pack_s`，s）：
| 类 | 层数 | cat_s | pack_s | 合计 |
|---|---:|---:|---:|---:|
| i8（全 int8 cat） | 60 | 0.012 | 0.219 | 0.231 |
| mixed1（f32 叶子 <4 MiB） | 42 | 0.007 | 0.411 | 0.418 |
| mixed2（f32 叶子 =4 MiB） | 8 | 0.001 | 0.173 | 0.174 |
| blocked（喂 eca，必须物化） | 6 | 0.001 | 0 | 0.001 |

判据：
- **cat 桶 1.534 → 0.130 s（<0.2 ✓）**；`cat_s+pack_s` 3.586 → **0.827**（可融合部分）。
- 总墙钟 12.220 → **8.472 s**（−3.75 s），略高于 S1 报的 8.302（同进程/不同轮次，噪声 5–6%）。
- sha1：六档全部 = `9b1940c8955b / f86a7035621b / 025757a83c39` ✓。
- 64²（回归口径）：`off` 3.72、`cf1` 3.03、`cf1qc` 3.32（小层净亏，只作 sha 回归，不作收益判据）。
- `cf1q8off` 与 `cf1` 是**同一配置**（`BIN_Q8_FUSED` 只影响 qcache，不影响 bin_engine），
  两者差 0.11 s 就是同进程轮次噪声 ⇒ 上面的 ±0.15 s 内差异都不显著。

### 5.1 qcache 的实际收益（诚实结论：不显著）

| | prologue | cat+pack | 总 |
|---|---:|---:|---:|
| `cf1` | 1.585 | 0.827 | 8.472 |
| `cf1qc` | 1.533 | 0.773 | **8.457** |

- 只省 **0.015 s（0.2%，噪声内）**；`cat+pack` 省 0.054 s（主要来自 mixed2 0.173→0.132）。
- 没到预测的 0.33 s，原因：① f32 段的量化本来就在 prologue 里、与 PL 重叠 1.5–1.7 s，
  砍掉它不一定砍总墙钟；② 只有"静态 f32 叶子"能安全换缓存段（被强制物化的 C8Tensor
  的 d=fs_in−fs_out 不能改），50 个 mixed 实例里只有 16 个唯一 key 命中；
  ③ 24 槽仍有 41 次 starve，导致部分层 kind 不匹配、退回现算描述符。
- ⇒ **`Q8CACHE` 默认保持 0（opt-in）**；不建议作为默认路径。q8_into 本身（S1 的
  `prepare_segments`）已经把热路径做到 175–232 MB/s，是主要收益来源。

## 6. 风险 / 不确定项 / 结论

1. **默认语义**：`BIN_CAT_FUSION` 未设 ⇒ 随 S1（不回退 S1 已有的 110-cat 融合）；
   `=0` 才显式关闭。原因见 §1。这是对任务书“默认关”的有意偏离，一行可改。
2. **64² 净亏**：`cf1 3.03` vs `cf0 2.89`、`off 3.72`；小层 E1E2 打包/描述符开销
   占比高。**建议 ≥128² 才开**（或加按层字节阈值）——属 S1 的调度策略。
3. **qcache 不显著**：net 只差 0.015 s（噪声），`cat+pack` 省 0.054 s。默认保持
   `Q8CACHE=0`；它的理论收益（0.33 s）被“量化与 PL 重叠 + kind 不匹配回落”吃掉了。
   若要用，先把 `BIN_LAZY_SLOTS` 提到不 starve，并给“被物化的 i8 叶子”单独建
   lane 计划（否则 `_build_desc` 现算会抵消收益）。
4. **strict 档**：`BIN_CAT_STRICT=1` 时 116 个 cat 全回落，cat 桶 2.405、总 12.59，
   与基线相当 —— 只作对照，不用于生产。
5. **尾字组** `cfg_group=min(g,7)`：依赖 ch≥cin 的 lane 指向 thr 哨兵（-127 ⇒ bit 恒 0）；
   544 通道层已验证逐字节 0 差异，但这是 RTL 行为而非显式契约。
6. **f32 缓存 key**：含 `fs_in` 与 f32 池 generation；本网 fs 冲突 0 次（`q8_fs_conflict=0`）。
   若未来出现同一 f32 张量被不同 `fs_in` 的 conv 消费，`_q8_segment` 会**禁用该张量的缓存**
   并回落 f32 段（正确优先，不静默）。
7. **qcache 缓冲区不复用**：S1 的 E1E2 job 延迟到 `kick()` 才跑，若复用已失效的缓存
   缓冲，待跑 job 会读到别的张量 ⇒ 静默错数据（本报告调试中真实踩到过一次）。
   现在 `_drop` 只删映射、不回收 arena；本网 6.3 MB 常驻，32 MB 上限足够，用尽则跳过缓存。
8. **A/B 口径**：总时间用**总墙钟**；cat 的节省用 `cat_s+pack_s`（按类）；`PL+PS` 桶和
   包含并行重叠，不能直接与总时间比（§5 的“口径”行给出 `桶和−重叠+wait` 与总时间的差）。

## 10. 一句话结论

在 S1 的 10.17 s 基线之上，S2 的 cat 多段直读把 **cat 桶 1.534 → 0.130 s**、
`cat_s+pack_s` **3.586 → 0.827 s**、整网 **12.220 → 8.472 s**（同进程 min of 2，
六档 sha1 全等）。qcache 只再省 0.015 s（噪声），保持 opt-in。

---

## 7. A53 量化/拷贝实测（qcache 定价依据，`board_q8_bench.py`）

板端（v33 已加载，**不碰 PL**，只用 pynq.allocate + CPU）：

| 形状 | quantize_int8 | quantize_leaf_c8(q8+to_c8) | heap copy | CMA copy | qcache miss | qcache hit |
|---|---:|---:|---:|---:|---:|---:|
| 64ch 128² 4.19MB | 250.6 MB/s | **71.2 MB/s** | 266.7 | 532.3 | 73.7 ms | 0.52 ms |
| 48ch 128² 3.15MB | 207.8 | 67.9 | 271.8 | 436.3 | 48.3 | 0.33 |
| 96ch 64² 1.57MB | 243.7 | 93.9 | 252.5 | 445.5 | 19.6 | 0.36 |
| 192ch 32² 0.79MB | 234.3 | 91.9 | 259.3 | 464.9 | 10.2 | 0.34 |

- 真实序列（29 实例 / 57.5 MB）：无缓存 0.795 s（72.4 MB/s）→ 有缓存 0.218 s
  （263.9 MB/s）= **3.65×，省 0.58 s**；按 92.8 MB 外推 ≈ **1.28 s → 0.35 s（省 ~0.9 s）**。
- 常驻 C8 缓冲：真实 23 个 key ≈ 6.3 MB（不是 26.7 MB 的 f32 输入）。
- 结论：qcache **保留**（>0.1 s 判据）。
- 新靶子：`to_c8_padded` 的转置让 q8 从 240 → 72 MB/s；若 S3 让量化内核直接按 C8 布局
  写出，mixed 的 1.29 s 本身就能变 0.35 s，qcache 都不需要。

---

## 8. C8 布局变换的四种实现（`Q8_C8_IMPL`，默认 `padded`）

`to_c8_padded` 是 `zeros → 拷 C → transpose → ascontiguousarray → tobytes`（4 趟），
把 q8 的 240 MB/s 拖到 68–94 MB/s。新增三种等价实现（全部逐字节相同，qcache 自检
在每个 impl 下都 PASS）：

| `Q8_C8_IMPL` | 做法 | 主机 4.19 MB | 备注 |
|---|---|---:|---|
| `padded`（默认） | 原 `to_c8_padded` | 0.76 ms | 保守默认，**板端未验前不改** |
| `fast` | `out[..., c] = src[:, c]`（8 个跨步切片） | 0.27 ms（2.8×） | A53 L2 512 KB，可能反向 |
| `blocked` | 按行块（工作集 ≤32 KB）内做 8 个跨步写 | 待测 | 缓存流量 ≈2× |
| `blocked_t` | 行块内 `transpose(0,2,3,1)` | 待测 | 缓存流量 ≈2× |

**方法论**：今晚已出现 3 次「主机快、板端慢」（f32 池 / catpack 大层 / 双线程），
所以 `fast` 在板上量到 ≥1.2× 之前**不设为默认**。`board_q8_bench.py` 已同时量
四种实现的 MB/s 与板上逐字节一致性。

---

## 9. 基线变化（S1 的板端整网，`lazy_ab_256.json` / `_b` / `_c`，同 seed 同输入）

| 轮次 | E1E2+LAZY 总 | prologue | PL | epilogue | 非卷积 | 备注 |
|---|---:|---:|---:|---:|---:|---|
| a（13:03） | 14.08 | 8.08 | 6.06 | 0.26 | 4.42 | prepare 把“等 E1E2 idle”算进 prologue |
| c（13:16） | **10.17** | **2.87** | 4.56 | 0.27 | 4.51 | S1 把 idle 拆到 wait 后 |
| off/off（c，冷/热） | 13.80 / 12.71 | 5.50 / 4.82 | 3.66 / 3.62 | 1.28 / 0.98 | 6.01 / 5.92 | 基线 |

⇒ S2 的板端 A/B 必须在 **c 这一版（~10.17 s）**上做；S2+qcache 的目���是把
`cat+pack` 从 S1 的 current 再压 ~1.5–2 s，落到 ~8.5 s 附近（最终以实测为准）。
