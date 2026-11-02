# T-18 阶段二：四刀实现 + 逐刀判据（全部 opt-in，默认关）

**日期**：2026-11-02
**基线**：`git HEAD = e72c4cd`（v4）。补丁 `runs/t18_smallops/T18_smallops.patch`（在 e72c4cd 之上 `patch -p1` 已实测打通）。
**工作副本**：`worktrees/T-18-smallops/`（**只改这里**）。**全程没有碰板子**（没有 ssh / 没有 Vivado / 没有 xsim）。
**代码 md5（打完补丁后）**：`bin_engine 4b1b25a7 / bin_net 9235f543 / bin_ref_numpy 337fbe3a / c8deq.c 8cd77f1c / c8deq.py 41c61d97`
**口径**：计时 **`OMP/OPENBLAS/MKL_NUM_THREADS=1` + `taskset -c <单核>`**；门禁 **默认 BLAS 线程**（第 72 条），两条命令分开跑。

---

## 0. 一句话

> 四刀都实现了、都过了 R1 逐字节与 R2 整网 sha1（含"四刀全关 = 冻结常量"）。
> 但**我阶段一给你的 0.29~0.30 是算术错误**（见 §6 自我更正）：
> **①②③ 的诚实投影是 0.45~0.49 s；加上④（若板端 RFO 假设成立）是 0.35~0.38 s**。
> ④ 在主机的 A/B 是 **负收益 0.77×**，所以它**默认关、必须板端单独实测**才决定要不要开。

---

## 1. 四刀实现（函数级；R6）

| 刀 | 开关（默认关） | 改的函数 | 语义 |
|---|---|---|---|
| ① | `BIN_CAT_ECA_FUSE=1` | `bin_net.BinNet._exec_ps`（cat 分支 + eca 分支）、新增 `BinNet._cat_eca_marker`、`_CatEcaChunks`、`_cat_eca_fuse`、`_cat_eca_note`、`eca_from_mean`、`eca_attn_ref_from_mean`；`bin_ref_numpy.eca_attn_f32_from_mean`（`eca_attn_f32` 拆成两段，原入口行为不变） | cat3 不 concat，eca 按叶子求均值 |
| ② | `BIN_SAM_TILE=1`（`BIN_SAM_SLAB_KB`，默认 **96**） | `bin_ref_numpy.sam_mix_f32`（选片函数）、新增 `_sam_slab_tiled`、`_sam_slab_kb`、`_sam_tile_on` | sam 按通道分片，片内 o/t 留 L2 |
| ③ | `BIN_EPI_NOINV=1` | `bin_engine.BinConvEngine.read_out`（惰性分支）、新增 `_epi_noinv`/`_deq_tile`/`_env_on` | 惰性直读层跳过 `_invalidate` |
| ④ | `BIN_DEQ_TILE=1` | `c8deq.c` 新增 `c8deq_chw_t`（旧 `c8deq_chw` 一字未动，`c8deq_version` 1→2）；`c8deq.py` 新增 `deq_tiled`/`deq_into_tiled`、`_wrap` 里可选绑定、`_selftest` 里加 tile 比对；`bin_engine` 新增 `_deq_kernel_tiled`，`from_c8_deq`/`from_c8_deq_into` 各加一条优先分支 | C8→f32 改成 tile 两相（读一趟、按通道整块写） |

**没有新增模块文件** ⇒ `deps_closure.DECL` **不需要动**；`c8deq` 本来就在 DECL 里（13 模块 / 19 文件）。
`bash ps/tools/merge_gate.sh` 在副本里跑过：**①--gen ②md5sum ③--check 全 rc=0（MERGE_GATE PASS）**。
⚠ 唯一要重生成的是清单 `runs/board_ops/20261102/deps_closure.txt`（因为是改文件不是加文件）。

---

## 2. 判据 R1（逐字节 0 差）——每刀单独 + 全开

**图层面**（256²，真实权重、真实形状，同进程交替，逐 `tobytes()` 比）：

| 刀 | 覆盖 | 结果 |
|---|---|---|
| ① | 6 个 `cat3`（含真实 eca 权重 `…fusion.conv1/conv2.weight`） | **6/6 全等** |
| ② | 6 个 sam 形状 × 片大小 {48, 96, 192, 512} KB | **24/24 全等** |
| ④ | 18 个 f32 层真实形状（C8 全 256 种字节值 + fs 0..12，`c8deq` 自检扩到 **c=1..68 / 5 种 HW 含 33×47**） | **旧内核 = tile 内核 = numpy 参考，全等** |
| ③ | 无数值路径（只删一次 cache 维护调用） | 由整网 sha1 兜住 |

**整网**（FakePL，`--hw 64`，6 臂 × 2 轮、**第 2 轮反向**，R4）：

```
off / K1 / K2 / K3 / K4 / ALL   ×2 轮  ⇒ 12 次跑的 out1/out2/out3 sha1 **全部 = 24222554c880/2fd745d93159/8ab595fcf7cd**
判据 R1（整网逐字节）: PASS
```
（该口径是 `OMP=1` 的假警报那一组，只用于**臂间相等**，不与冻结常量比 —— 见 §3。）

---

## 3. 判据 R2（整网 sha1，**默认 BLAS 线程**口径）——每刀单独 + 全开 + 全关

| 臂 | out1 / out2 / out3 | 结论 |
|---|---|---|
| 四刀全关（= 默认） | `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd` | = 冻结常量 ⇒ **PASS** |
| `BIN_CAT_ECA_FUSE=1` | 同上 | PASS |
| `BIN_SAM_TILE=1` | 同上 | PASS |
| `BIN_EPI_NOINV=1` | 同上 | PASS |
| `BIN_DEQ_TILE=1` | 同上 | PASS |
| 四刀全开 | 同上 | **PASS** |

日志：`runs/t18_smallops/probes/gate2_{off,all,BIN_*}.log`。

**附：刀① 的两条结构安全信号**（同一整网、同一进程，`--hw 64`）
```
off:  cat.fused=110  fallback=6   lazy={pinned 198, released 168, starved 30, deq_skipped 198}
K1 :  cat.fused=110  fallback=0   lazy={pinned 198, released 168, starved 30, deq_skipped 198}
跑完后 net._catpins 残留 = 0
```
⇒ ① 不是把回落"藏起来"（`fallback` 6→0 是真融合）、② **没有多钉住任何 OUT 槽**（`starved` 不变，
板端 v4 的 `starved=8` 不会涨）、③ 没有 pin 泄漏。

---

## 4. 判据 R3（默认关 + 静默回落 + stdout 不变）

- 四个开关**全部默认关**；全关时 `verify_lazy_net --hw 64` 的 **stdout 与 HEAD 逐字相同**
  （把计时数字归一化后 `diff` 只剩我自己追加的 `rc=` 一行）。
- 每一刀都有逐级回落，且**只在真的回落时打一行**（`_cat_eca_note`、`_deq_kernel_tiled` 返回 None）：
  ① 扫描前提不满足（消费者≠1、有惰性/非 f32 叶子）⇒ 原路径；
  ② 片 ≥ 整块 ⇒ 直接调 `_sam_slab`（零额外开销）；线程内异常 ⇒ `_run_split` 返回 False ⇒ 整块重算；
  ③ 与不跳过的唯一区别是少一次 `invalidate`；
  ④ 新符号不存在（旧 `.so`）/ 新内核异常 / 自检不过 ⇒ 回落旧内核 ⇒ 回落 numpy。

---

## 5. 逐刀 A/B 与板端投影（R4 / R5）

> 板端基线取 v4 `ABC` 臂 6 点中位：`sam 242.0 / epi 255.0 / cat 127.7 / eca 80.2 / add 69.3 / shuf 19.2 = 793.4 ms`。
> 锚点：内存流 **0.48 GB/s**（`add` 标定）；`convfp` gemm 0.608 GMAC/s（本轮不涉及）。
> **主机 A/B 一律同进程交替**（两次方向相反的轮次取中位），不用"独立冷 cache"绝对值。

### 刀① `cat3 → eca` 逐叶均值 —— 最干净的一刀
- 主机 A/B（256²，真实权重）：`concat+eca` 8.21 ms → `逐叶均值+eca` 5.25 ms = **1.56×**（6/6 逐字节全等）。
- 板端投影：**−127.7 ms**（`cat` 桶整块归零；`eca` 的 33 MB 读不变）。
  依据：cat 桶自身 66.36 MB / 127.7 ms = **0.52 GB/s**（就在锚点上）⇒ 删掉这 66.36 MB 就删掉这 127.7 ms。
- 主机比率若硬套只有 −75 ms（因为主机上 concat 只占这一对的 40%，板端占 62%）⇒ **这一刀以字节账为准，不以主机比率为准**。

### 刀② `sam` 分片单趟
- 主机 A/B（256²，6 形状，驱逐 cache）：`8.11 → 6.64 ms` = **1.22×**（片 96/192 KB；24/24 全等）。
- 板端投影：**−127~161 ms**（`242 → 81~115`）。
  依据：15S→5S（165→55 MB）；按 sam 自己的 0.68 GB/s = 81 ms，按锚点 0.48 = 115 ms。
- ⚠ 主机只给 1.22×，**这一刀主机看不出来**：板端一次 3.15 MB 的 pass 是 4.6 ms，而分片只多 96 次 numpy 调用≈0.5 ms ⇒ 板端由字节主导；主机是调用开销主导（18 GB/s，pass 只要 0.17 ms）。
- 片大小默认 **96 KB**（保守：板端 2 线程共享 512 KB L2，片内 o+t 两份）。可 `BIN_SAM_SLAB_KB` 调。

### 刀③ 惰性层跳过 `invalidate`
- 板端投影：**−53.5 ms**（`198/227 × 0.27 ms`；0.27 ms 是 epi256 在同板上对同一 `buf.invalidate()` 的实测）。
- 依据补充：arena 是 **66.2 MB**（`t40_ab.json: arena_mb`），`RegionBuffer.invalidate()` 会作废**整块** arena；
  而同一份文档里 `flush()` 66 MB 实测 0.149 ms（444 GB/s）⇒ **这条调用基本是 PYX 调用开销，不是 cache 维护**。
- 安全性（不是"大概没事"）：本网 CPU 读 OUT 字节**只有** `read_out(lazy=False)` 一条路（`C8Tensor.materialize()` → 它第一行就 `_invalidate`），
  PL 走 AXI 读 DDR、与 CPU cache 无关；我又核对过 `qcache.quantize_leaf_c8` 吃的是 **f32**、`C8Tensor.raw()` 只在自检里用。
  ⇒ 跳过它不会读到旧影子。
- 这刀**主机无法验证**（无 pynq、无 DMA 缓冲），只能板端看 `epilogue` 桶。

### 刀④ `deq_into` tile 化 —— **主机是负收益，必须板端定**
- 主机 A/B（18 个真实形状，驱逐 cache）：旧 3.07 ms → tile 4.00 ms = **0.77×（慢 30%）**。
  原因：主机 L3 足够大，旧内核的"8 条交错写流"本来就不付 RFO；tile 版多了一遍 scratch 往返。
- 板端假设（**未验证**）：A9/PL310 是 write-allocate，旧内核每个像素写 8 个相距 HW·4 B 的 f32 ⇒ 每条 line 都"半写" ⇒ 写 33.3 MB 实际要 **+33.3 MB 的 RFO 读**；
  tile 版按通道整块写（整 line 覆盖）⇒ 41.66 MB 就是 41.66 MB ⇒ 194 → **87 ms（−107）**。
- ⇒ **默认关**。若板端也无收益，本刀作废（那是第 84 条的老课：内核快慢是平台相关的命题）。
- 板端验法（不需要整网，5 分钟）：
  ```
  pynq 上拿一块真 DMA 缓冲，对 29 层形状各跑 c8deq.deq 与 c8deq.deq_tiled，
  两次结果先 tobytes() 比一次（必须全等），再各计时 5 次取中位。
  ```

### 汇总投影

| 组合 | 板端投影 | 依据 |
|---|---:|---|
| 现状 v4 | 0.793 s | 实测 |
| +① | 0.666 | −127.7（cat 桶归零）|
| +①+② | **0.51~0.54** | 再 −127~161（sam 15S→5S）|
| +①+②+③ | **0.45~0.49** | 再 −53.5（198×0.27 ms）|
| +①+②+③+④（若 RFO 假设成立） | **0.34~0.38** | 再 −107（deq 0.215→0.48 GB/s）|
| 若④ 在板端也是 0.77× | 0.51~0.55（**④ 会倒赔 ~54 ms**） | 按同一比率坏的方向算 |

---

## 6. ⚠ 自我更正：阶段一的 0.29~0.30 是算术错误（这一条请优先看）

阶段一我写的是"三刀后 0.40 s，加第四刀 0.29~0.30 s"。**这两句都不成立**：

1. **三刀不是 0.40**：793 − 128（①）− 160（②）− 53（③）= **452 ms**，我当时写成 400，
   于是第四刀 0.40 − 0.107 = 0.29 也跟着错。正确值是 **0.45~0.49**。
2. **第四刀的 −107 是"条件收益"**：它成立的前提是"deq 的 194 ms 里有 107 ms 是 RFO 多搬的字节"。
   阶段一我按"名义字节 41.66 MB / 0.48 GB/s 应只要 87 ms"反推，**没把这个前提写清楚**；
   本轮主机实测又是 0.77×（负），所以现在只能标**待板端实测**，不能算进预算。

⇒ **≤0.35 s 用这四刀是"刚好卡线"（0.34~0.38），没有余量**。要留余量只有两条（都超出本轮授权，请你定）：
- **第五刀**：deq 不落到 f32，而是并进消费者（29 层里 12 层喂 `add`、12 层喂 `interp`、6 层喂 `cat`/`sam`）
  ⇒ 省 33.3 MB 的 f32 写 + 33.3 MB 的 f32 读，换成 8.3 MB 的 int8 读 ⇒ **−70~100 ms**（且不依赖 RFO 假设）。
  代价：要动 `_needs_f32`/`_analyze_lazy`（我阶段一承诺不碰的那两张表）+ `add`/`interp` 的调用点。
- 或 **sam 把 `b2/b4` 的上采样并进来**（55 → 36.5 MB，−25~35 ms），代价是碰 `bilinear`。

---

## 7. 复核用命令（都在副本里，单核）

```bash
cd /media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code
# 门禁（默认 BLAS 线程；四刀全关 / 全开都必须 PASS）
python3 ps/tools/verify_lazy_net.py --hw 64
BIN_CAT_ECA_FUSE=1 BIN_SAM_TILE=1 BIN_EPI_NOINV=1 BIN_DEQ_TILE=1 python3 ps/tools/verify_lazy_net.py --hw 64
# 六臂 A/B（换序 2 轮；R1）
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 taskset -c 3 \
  python3 ../../runs/t18_smallops/probes/ab_knives.py --hw 64
# 逐刀 op 级 A/B（256² 真实形状 + 真实权重；R1 + R4）
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 taskset -c 3 \
  python3 ../../runs/t18_smallops/probes/op_ab.py
# 图论证据（消费者表 + 分析表 md5）
python3 ../../runs/t18_smallops/probes/scan_cat3.py <ps 目录>
python3 ../../runs/t18_smallops/probes/dump_analysis.py <ps 目录> 256 /tmp/ana.json
# 合并门禁（改的是已有文件 ⇒ 只需重生成清单）
bash ps/tools/merge_gate.sh
```

---

## 8. 刀① 的"删掉一个图节点"专项证据（按你的要求，机械证据）

**(a) 唯一消费者** —— 取自 `g.dependents`（不是取自记忆），256²：
```
encoder.encoder_1.sam_blocks.0:cat3  deps=3 out=(144,128,128) 消费者数=1 ['eca']
encoder.encoder_2.sam_blocks.0:cat3  deps=3 out=(288, 64, 64) 消费者数=1 ['eca']
encoder.encoder_3.sam_blocks.0:cat3  deps=3 out=(576, 32, 32) 消费者数=1 ['eca']
decoder.decoder_3.sam_blocks.0:cat3  deps=3 out=(192, 32, 32) 消费者数=1 ['eca']
decoder.decoder_2.sam_blocks.0:cat3  deps=3 out=(192, 64, 64) 消费者数=1 ['eca']
decoder.decoder_1.sam_blocks.0:cat3  deps=3 out=(192,128,128) 消费者数=1 ['eca']
⇒ blocked cat 共 6 个，全部"恰好 1 个消费者且是 eca"；多消费者 cat 84 个（都是 i8/mixed，本来就不物化）
```
运行期还有第二道同款检查（`_cat_eca_marker` 自己再查一遍），不满足就**不融合**——所以这张表错了也不会错数。

**(b) 两张分析表前后逐项相同** —— HEAD 版 vs 打完补丁的版本，各跑一次 `build_graph + _analyze_lazy`，导出并比 md5：

| hw | 文件 md5（两侧相同） |
|---|---|
| 64  | `needs_f32 fa26916a… / nuse 0980413… / cat_class fd7181e… / lane_plan f876dbc… / lane_plan_q8 5a5ca41…` |
| 256 | 整个导出 JSON md5 = `5a6a11645e6287c67dac2d73bfb8879b`（两侧**逐字节相同**）|

**(c) 为什么"删掉它"不会改变任何物化决策** —— 这 6 个 cat 的 18 个叶子**全部**已经在 `_needs_f32` 里，
而且原因是 `sam`（`sam` 要 f32）而不是这个 cat：
```
encoder_1.sam_blocks.0:cat3  [(48,128,128) in needs=True 消费者=[cat,sam]] × 3
（其余 5 个同形）
```
⇒ 叶子本来就要物化；融合只删掉"把它们再拷成一份 cat"的那一步。

**(d) 逐字节** —— 见 §2（6/6 图级 + 整网 sha1 12 次全等）。

---

## 9. 已知风险 / 未覆盖（不许当已解决）

1. **④ 的收益完全是板端未验证假设**（主机 0.77×）。默认关，请板端单独 A/B 后再决定。
2. **② 的收益依赖"板端一趟 pass ≈ 4.6 ms"这个实测事实**（0.68 GB/s × 3.15 MB）。若板端 sam 有一部分本来就命中 cache，
   实际收益小于 −127；**这一刀主机 A/B 只能给方向（1.22×），给不了幅度**。
3. **③ 只在"CPU 不读那片 OUT"的前提下成立**；本网已核对只有一条 CPU 读路，但**换网络结构要重新核**（`C8Tensor.raw()` 若被新代码调用，必须补 `_invalidate`）。
4. `_cat_stats` 在融合时会少记 6 个 `fused`（那是 `--profile` 口径，不是正常 stdout）；板端 `cat_fb` 应从 **6 → 0**，这是**开着刀①的最硬信号**。
5. 刀① 后 `prof["cat"]` 会塌到 ~0、`prof["eca"]` 会略涨（叶子均值读在里面）——**桶会搬家，别把它当"没省下来"**（总账看 `total` 和 `ps_s`）。
6. 本阶段**没有跑 `--hw 256` 整网门禁**（FakePL 256² 单核要十几分钟，且你只要求 64² 口径）；256² 的字节账是构图算出来的，不是跑出来的。
