# S2 前置分析：C8 布局 / cat 语义 / 哪些层能走 PL

> 2026-10-31 12:5x，只读分析（未上板、未改 RTL）。
> 依据：`pl/rtl_hand/modules/bin_e1e2.v`、`pl/rtl_hand/golden/bin_3x32_48_8x8/expected_c8.bin`、
> `deploy/e1e2_desc/desc.bin+frame.json`、`ps/pynq/bin_net.py`、`bin_ref_numpy.py`、
> `ps/tools/gen_e1e2_desc.py`。

---

## 0. 先纠正一个前提（会决定实现边界）

任务书写的是「E1E2 已有 8 个逐通道描述符（64 个）」。
**RTL 实读结论：一个 64 通道字组只有 8 个描述符，一个描述符 = 一条 lane = 8 个连续通道**
（`bin_e1e2.v` §1/§2：`base_desc + (group*64 + i)*8` 只有 `i = lane 0..7`）。

```
word k 的 bit [8k+j]  ⇐  源地址 lane_src[k] + p*8 的第 j 个字节
                          = 逻辑通道 (64*group + 8*k + j) 在像素 p 的值
```

- 逐通道的是 **catpack**（`R_RUN_UNIT=5`，读 f32，`base_desc+(group*64+i)*8`，i=0..63）；
  E1E2（`R_RUN_UNIT=4`，读 int8 C8）是 **8 lane × 8 通道**。
- 所以「把每段的 `(pa,C,H,W)` 展开成逐通道 `src_pa`」在 E1E2 上做不到逐通道；
  能做到的是**逐 lane**：一条 lane 的 8 个通道必须来自**同一段**且段内首通道是 8 的倍数。
- 这不是坏消息：本网 206 个 cat 的**全部**段边界都落在 8 的倍数上（见 §4），
  所以多段直读在本网 **100% 可表达**，不需要动 RTL。

---

## 1. C8 布局的精确公式（带图 + 实测）

### 1.1 定义

设一个 int8 张量 `(C,H,W)`，物理基址 `P`，`cg = ceil(C/8)`，`HW = H*W`。
`bin_conv_core` 的输出布局（`bin_ref_numpy.to_c8` / `from_c8_deq` 互逆对）：

```
内存下标（字节）  off(c, y, x) = P + (c//8)*(H*W*8) + (y*W + x)*8 + (c%8)
                                └──── 组主序 ────┘ └─ 像素主序 ─┘ └ 8通道最快 ┘
```

按 `(cg, H, W, 8)` reshape 后：

```
a[g, y, x, j] = 逻辑通道 (8g + j) 在像素 (y,x) 的 int8
逐通道平面 g 占连续 HW*8 字节；一个像素 8 字节 = 8 个连续通道（C8 交错）
from_c8_deq: a.transpose(0,3,1,2).reshape(cg*8,H,W)[:C] / 2^fs
```

### 1.2 E1E2 一条 lane 的地址

字组 `g`（逻辑通道 `64g..64g+63`）、lane `k`（通道 `c0 = 64g + 8k`），
若这 8 个通道来自「基址 `pa`、在整层里起始通道 `o`」的那一段：

```
lane_src = pa + ((c0 - o) // 8) * H * W * 8          # 一个 8 通道平面
lane_thr = thr_pa + c0 * 2                           # 8 个 int16（c0..c0+7）
desc[k]  = (uint64(lane_thr) << 32) | uint64(lane_src)
bit[8k+j] = (int8 @ lane_src + p*8 + j) + thr[j] > 0，且 c0+j < cin
```

**一个描述符覆盖 8 个通道，不是 1 个。** 这也是为什么段边界必须 8 对齐：
`c0..c0+7` 跨两段时，一个 `lane_src` 表达不了。

### 1.3 实测验证（三个独立来源）

1. **RTL golden**：`golden/bin_3x32_48_8x8/expected_c8.bin`（48×8×8，RTL 实跑输出）
   - 公式索引 `off(c,y,x)` 抽出的张量 == `reshape(cg,H,W,8)[c//8,:,:,c%8]`：**True**
   - `to_c8(抽出) == expected_c8.bin`（逐字节）：**True**
   - 抽样：`c=0,p=0→off 0`、`c=7→off 7`、`c=8→off 512`、`c=47,p=63→off 3071`、
     `c=20,p=5→off 1068`
2. **交付物交叉**：用同一公式重新生成 `deploy/e1e2_desc/desc.bin` 的 5584 个描述符字，
   与交付文件 **逐字节相同**（`np.array_equal == True`）。
3. **驱动自检**：`bin_engine.py --selftest` 的
   `C8 布局：from_c8(to_c8(x)) == x`、`惰性句柄 materialize == 旧 read_out` 全过。

---

## 2. cat 的语义（轴 / 顺序）

- `bin_net.build_graph()` 里 `cat(xs)` 的输出形状是 `(sum(C), H, W)` ⇒ **通道轴**。
- `_exec_ps` 的 cat 最终落到 `bin_ref_numpy.cat_last(parts)`：
  ```python
  return np.concatenate(parts, 0)      # axis=0，通道优先布局
  ```
  `BIN_CAT_THREADS=2` 只是沿源段切两线程，逐位相同。
- **顺序 = deps 列表顺序 = 源程序 `cat([...])` 的左到右顺序**；嵌套 cat 由 build_graph
  展开，`_exec_ps` 里 `arrays.extend(t.val.arrays)` 保持同一顺序。
- 危险点已核对：`_CatLazy` 的 `arrays` 顺序就是拼接顺序；`_segments()` 再按 arrays 顺序
  生成段表，`_build_desc` 按 `off` 累加 → 与 `np.concatenate(axis=0)` 完全同序。
  离线 227 层逐字节 A/B（`verify_e1e2_direct.py`）差异 0。

---

## 3. 本网 cat 的真实切分（不是 20/20/24）

对 256² 的 206 个 cat（64² 拓扑相同）逐叶子展开：

| 叶子数 | 2 | 3 | 4 | 5 | 7 | 9 | 11 |
|---|---|---|---|---|---|---|---|
| 个数 | 8 | 24 | 6 | 18 | 18 | 18 | 18 |

- 通道切分最常见：`32+32`（45 个）、`48+48`（45 个）、`48+48+64`、`96+64`、
  以及 11 叶子的 `32×10 + 48/96/192`。
- **段边界 mod 8 = 0 的违例数：0**（206 个 cat 全查）。
  所以任务书里的「20/20/24」不是本网的通道切分（本网最大 11 个叶子；
  20/20/24 更可能是别处统计的叶子个数/层数，不是通道数）。
- 110 个 cat 的输出直接喂 conv；6 个只喂 `eca`（必须物化，见 §4）。

---

## 4. 哪些层能走 PL、哪些必须回落

`_needs_f32`（构建期分析）：被 add/interp/sam/eca/convfp/shuffle 消费、网络输出、
或喂给这类消费者的张量必须 f32。其余保持 int8 C8。

227 个 conv 的输入分类（256²）：

| 输入形态 | 层数 | 走法 |
|---|---:|---|
| 单张量，全 int8 C8 | 102 | E1E2 单段直读 |
| cat，全 int8 C8 叶子 | 60 | **E1E2 多段直读（cat 物理消失）** |
| cat，含 f32 叶子（add/interp 残差） | 50 | 同一趟 E1E2：f32 段在 PS 量化进 C8 staging，PL 打包 |
| 单张量 f32（convfp 输出等） | 15 | 同上（PS 量化 + PL 打包） |
| **合计** | **227** | 0 层因「段边界不对齐」回落 |

- 必须物化的 cat：6 个 `sam_blocks.0:cat3`（喂 eca，f32 sigmoid 权重）——
  它们在 `_needs_f32` 里，`_exec_ps` 直接 `np.concatenate`，不参与融合。
- 110 个可融合 cat 若全部物化，256² 下要拷 **468.8 MB**；全部 cat 物化是 501.8 MB。
- 说明「混合」路径：它不是「部分 PL 部分 PS 的不一致」，而是**一次 E1E2 命令**
  完成整层打包，f32 段在 PS 只做 `quantize_int8`（与旧 `pack_input` 同一内核、同一阈值），
  i8 段零拷贝。逐字节等价已由 227 层离线证明。若要求「严格全 PL / 全 PS」，
  可加 `BIN_CAT_STRICT=1` 让含 f32 段的层整层回落（性能另测），默认不启用。

---

## 5. S2 的落点（结论）

1. S1 的代码里已经有 `E1E2Engine.pack_segments()` / `_PlBackend._segments()` /
   `pack_c8()`，即**多段能力已经存在**；当前 `lazy_i8` 分支在 `BIN_LAZY_DEQ=1`
   时自动把 cat 变 `_CatLazy`。
2. S2 要做的是把它**变成受 `BIN_CAT_FUSION` 控制的、可 A/B 的、有统计/有回落原因的**
   功能：默认关 ⇒ cat 仍 `concatenate`（与今天逐字节相同）；打开 ⇒ 110 个 cat 惰性化，
   下一层 `prepare()` 直接建多段描述符；任一结构条件不满足 ⇒ 记录原因并整层物化回落。
3. 边界条件：段边界非 8 对齐 / 段 C 非 8 倍数 / 形状不一致 / pa=0 ⇒ `pack_segments`
   返回 None ⇒ 该层物化 + 旧 PS 打包（本网 0 例）。

---

## 6. S2 实现（patch 草案，**尚未合入**，等 S1 冻结）

文件：
- `docs/patches/S2_BIN_CAT_FUSION.patch`（对 `ps/pynq/bin_net.py` 的加法式 diff）
- `docs/patches/bin_net.S2_draft.py`（应用后的完整文件，便于对照）
- 新增：`ps/pynq/board_cat_fusion_ab.py`（板端 A/B）
- 新增：`ps/tools/verify_cat_fusion.py`（离线清点 / A/B / 准入回落 / 逐层逐字节）

改动只有 `bin_net.py`（`bin_engine.py` 的 `pack_segments` 已由 S1 实现，本 patch 不动它）：

```python
# 1) 开关（run() 里读）
#    ⚠ S1 的代码已经默认把 cat 惰性化（110 个）⇒ 未设时必须保持 S1 行为，
#      否则合入 S2 会回退 S1 的 nonconv 6.13→4.42 收益。
#      未设 ⇒ 随 S1；=0 ⇒ 显式关闭（A/B 的 "cat 走 PS" 档）；=1 ⇒ 开。
_cf = os.environ.get("BIN_CAT_FUSION", "").strip().lower()
self._cat_fusion = (bool(self._lazy_deq) if _cf == ""
                    else bool(self._lazy_deq and _cf in ("1","on","true","yes")))
self._cat_strict = BIN_CAT_STRICT in ("1","on","true","yes")

# 2) _exec_ps 的 cat：只有 _cat_fusion 打开才惰性化
lazy_i8 = self._cat_fusion and op.out.id not in self._needs_f32
why = self._cat_lazy_ok(arrays, ids, ok, op)   # 8 对齐 / 类型 / strict / ΣC
if why is None:
    return _CatLazy(arrays, ids)               # cat 物理消失
self._cat_fallback(why, op)                     # 否则整层物化回落（记录原因）
return cat_last(v)

# 3) prepare() 里 pack_segments 返回 None / ValueError ⇒ 物化 + 旧 pack_input
```

### 6.1 配套：f32 叶子的量化缓存（`ps/pynq/qcache.py`，独立模块）

- `(tensor 身份, fs_in)` → 带物理地址的 C8 int8 缓冲；命中的 f32 叶子在 `_segments()`
  里直接变成 **i8 段**（有 pa）⇒ `pack_segments` 不再现场量化，连 vin 拷贝都省。
- 键含 f32 池 generation；BV_FREE 判死时 `invalidate()`；单块 arena + 空闲区合并；
  `Q8CACHE=1` 打开（默认关），`Q8CACHE_MB` 上限（默认 32 MiB）。
- 常驻的是 **C8 int8 = 6.3 MB**（不是 26.74 MB 的 f32 输入字节）。
- 主机 `bench_qcache.py`：无缓存 0.048 s → 有缓存 0.076 s（**0.63×**，主机 q8 已 1.9 GB/s）；
  只有 A53 上 quantize 明显慢于 memcpy 时才赚，必须板上 A/B。

`_cat_lazy_ok()` 的前提与 `E1E2Engine._seg_layout()` 逐条一致：
叶子是 `C8Tensor` 或 `(C,H,W) float32`；`off%8==0` 且 `C%8==0`；`ΣC == cat 通道数`。
任何一条不满足 ⇒ **整层回落**（不是部分走 PL），原因写进 `stats["cat"]["fallback_reason"]`。

## 7. 统计口径（回答“60 个全 int8 vs 50 个 f32 叶子分开报”）

patch 在 `stats["cat"]` 里给三类 cat 分开计时（不影响数值路径）：

| 键 | 含义 |
|---|---|
| `ops_by_class = {i8:60, mixed:50, blocked:6}` | 按叶子类型分类的 cat op 数 |
| `cat_s[cls]` | `_exec_ps` 里该类 cat 的墙钟 = **cat 桶本身**（baseline 就是 concatenate 的时间） |
| `pack_s[cls]` | 下一层 conv `prepare()` 里“输入 → 打包字节”的墙钟（PS 量化 + E1E2 同步打包） |
| `fused` / `fallback` / `fallback_reason` | 融合/回落计数与原因（`needs_f32` / `seg_not_c8` / `pack_segments_none` / `prepare_valueerror` / `segments_none`） |

A/B 报告会给出这张表（baseline vs FUSION=1）：

| 类别 | layers | cat_s 现状 → 融合后 | pack_s 现状 → 融合后 |
|---|---:|---|---|
| i8（全 int8 叶子） | 60 | 大 → ~0 | E1E2 打包（基本不变） |
| mixed（含 f32 叶子） | 50 | 大 → ~0 | 逐段量化 + E1E2 打包（**剩余 PS 成本在这里**） |
| blocked（喂 eca） | 6 | 不变 | 不变（仍物化） |

> 预期：`cat_s[i8]` 与 `cat_s[mixed]` 都降到 ~0；`pack_s[mixed]` 仍有
> 逐段 `quantize_int8` 的成本。若 256² 的 cat 桶只能降到 ~0.8 s，那就是这 50 层
> 的 f32 叶子（interp/add/convfp）在 prepare 里量化，是 S4 的目标。

## 8. 已做 / 待做

- [x] C8 布局公式三重验证（RTL golden / desc.bin / 自检）
- [x] cat 轴与顺序确认（axis=0，deps 左到右，嵌套展开保序）
- [x] 206 个 cat 的清点：0 个 8 偏差；110 个可融合、6 个 blocked
- [x] 离线逐层逐字节：110 个 cat 输入层，979,936 B @64²，差异 0（PASS）
- [x] 离线整网 A/B：FUSION=0/1 sha1 相同（PASS，dev 副本）
- [ ] **合入 `bin_net.py`（等 S1 冻结）**
- [ ] 板端 A/B：`--hw 64/256`，FUSION 0/1（+ strict），cat 桶分项
- [ ] 全量回归：`bin_engine --selftest`、`catpack --selftest`、`bin_net --dry-run --hw 256`
- [ ] 板端整网 sha1 必须 = 冻结基线（9b19…/bfa2…）

---

## 9. 116 vs 206 与 mixed 的字节账（256²）

- **206** = 参考拓扑（`tensor_graph` / `--dry-run` 打印的 cat 数）：ghost 里原来的两级
  `cat([cat([x1,x2]), t_])` 记成 **2 个 cat**。
- **116** = 实际执行的 `BinNet.build_graph`：同一处已合并成一次 3 段 cat
  （90 处 ghost ⇒ 206−90=116，与分辨率无关，数值恒等）。
  板上 2.44 s 对应执行的 116 个。

Σ 每个 cat 的输入字节（= concat 要拷的字节）：

| 口径 | cat 数 | 物化字节 |
|---|---:|---:|
| 执行图全部 | 116 | **501.8 MB** |
| ├ 全 int8（i8） | 60 | 81.7 MB |
| ├ 含 f32 叶子（mixed） | 50 | 387.1 MB |
| └ 喂 eca（blocked） | 6 | 33.0 MB |
| **可融合小计** | **110** | **468.8 MB** |
| 参考拓扑（206，未合并） | 206 | 592.1 MB |

`2.44 s / 468.8 MB ≈ 192 MB/s`。

### 9.1 mixed 的 f32 叶子（逐层量化工作量）

| 源 op | 逐层字节 | 实例 | 唯一张量 | 唯一字节 | 冗余 |
|---|---:|---:|---:|---:|---:|
| add（残差） | 55.05 MB | 30 | 6 | 11.01 MB | 5.0× |
| conv（int8 输出被迫 f32） | 23.59 MB | 15 | 5 | 7.86 MB | 3.0× |
| convfp | 9.44 MB | 3 | 1 | 3.15 MB | 3.0× |
| sam（weighted_sum） | 4.72 MB | 2 | 2 | 4.72 MB | 1.0× |
| **合计** | **92.80 MB** | 50 | **14** | **26.74 MB** | **3.5×** |

- mixed1（42 个，f32 叶子 <4 MiB）：59.2 MB；mixed2（8 个，=4 MiB）：33.6 MB。
- mixed2 的 8 个只读 **2 个唯一张量**：`decoder_1.rdb:residual`（add，5 个 cat 共用）
  与 `decoder.preconv_1.conv`（conv 输出，3 个 cat 共用）。
- ⇒ **S3 最便宜的一刀不是「add 输出 int8」，而是「量化结果缓存」**：按
  `(tensor, fs_in)` 缓存 int8，92.8 → 26.7 MB（省 66 MB ≈ 0.17–0.34 s）；
  之后才是 add int8（11 MB）与「conv 输出同时保留 int8 句柄」（7.9 MB / 逐层 23.6 MB）。
