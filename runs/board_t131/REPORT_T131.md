# T-131：128² 整网的非确定性**不是**来自 PS 侧哈希序（`PYTHONHASHSEED` 已排除），布局也不是原因

**一句话**：v53（`e19de032`）在 **50.0 MHz**、128²、`BIN_LAZY_DEQ=0`、24 槽固定运行点上，
把 `PYTHONHASHSEED` 钉死为 **0** 连跑两次，**三 sha1 仍然不同**（`b611d7f1…` vs `5b631078…`），
`seed=1`、`seed=2` 各两次也同样**每次不同**（6 跑 = 6 组互不相同的三 sha1，任意两两不同）。
同时**逐 op 布局 227 行**在 15 个对比对里 **10 对逐字段完全相同**，唯一有差异的那 1 次只有 **2 行**
（`op79`/`op109` 的 slot 0↔1 互换，Δin_pa=±294912、Δout_pa=±397312）。
⇒ **哈希序这条线彻底断了**：不是 op 序、不是 arena 布局、不是"特定布局才触发的潜伏 bug"。
非确定性依然只在 **PL conv 的输出尾巴**上（首个分歧 op = **13 或 15**，`npix=4096 / cout=32`，
前 64 B 六跑逐字节相同 ⇒ 差异在**尾部**，与 T-124 的空间形状一致）。

---

## 0. 运行点、资产、纪律（一个租约 = 三个进程 = 三次 load）

| 项 | 值 |
|---|---|
| 位流 | v53 `runs/v53_archive/design_1_150v53.bit` md5 **`e19de0326fe657fc8de9ddd241ecc729`**（跑前=跑后 ✓）+ `.hwh` `5235764602921d…` |
| 频率 | 目标 50 → **读回 50 MHz**（三次 load 各显式设过一次，load 后默认 125） |
| 指纹 | `conv_uni_0`=0x40040000 / `bin_unit_0`=0x40060000 / **magic(0x314)=0x50300001 ✓**；**每个 run 前后 0x310=0** ✓（第 118/119/120 条，6/6 次） |
| 运行点 | `BIN_PACK=e1e2 BIN_LAZY_DEQ=0 BIN_LAZY_SLOTS=24 BIN_CAT_FUSION=1 BIN_BILINEAR_C=1 BIN_PRECOMP=1 Q8CACHE=1 BIN_F32DIRECT=1 BIN_CAT_ECA_FUSE=1 BIN_EPI_NOINV=1`；**无任何 `BIN_V50_*`**；`vlanes_now=8` |
| 规模 | 395 op / **227 conv**；max_in 0.295 MB / max_out 0.393 MB；npix≥4096 的 op = 31 |
| arena | 16.68 MB / 24 槽，三次进程都 **@0x30100000**（同一进程内两次 run 共用同一 arena） |
| 输入 | 128² `x0` sha1 = `26c9796e18f0e6883680693b805ce8bb7ad51486` ✓（= T-124 冻结口径，6/6 相同） |
| PS 门禁 | 板端 `md5sum -c` **158/158** ✓；依赖清单 20 文件 `md5sum -c` rc=0 ✓；`deps_closure --check` rc=0 ✓；每跑 `[DEPS] q8/c8deq/bpack/e1e2/catpack/safe` 全 **fast** ⇒ `deps_closure_ok=True` |
| load | **3 个进程 / 每进程 1 次 download**（`PYTHONHASHSEED` 是进程级 ⇒ 换种子结构上必须换进程，与 T-124 的"一租约两次 load"同一路径） |
| 纪律 | `ulimit -c 0`；只按 pid `kill -TERM`（无 `pkill -f`）；ssh 2 条（+2 条 scp）；收尾只 `sync; sync`；**未 poweroff** |

有效凭据：`t131_seed{0,1,2}_0741xx.log/.json`、`layout_net128_*.json`×6、`ops_net128_*.json`×6、`t131_cmp.json`。

---

## 1. 第 1 步：钉死哈希种子跑两次 —— **两次仍不同**（假设被否）

| arm | `PYTHONHASHSEED` | `hash('demoir')`（证明种子真生效） | run1 三 sha1 | run2 三 sha1 | 两次相同? |
|---|---|---|---|---|---|
| **seed0** | **0** | `0x2d83ca3a…` | `b611d7f18f57` / `33ada30f2513` / `d34b5186f28d` | `5b631078c359` / `d7baed673a01` / `735703d855a0` | **否** |
| seed1 | 1 | `0xffffffff9c1deca1` | `f8cd7b883ed8` / `acebcbf2f26e` / `ca82ff4149d5` | `a880f66d5ad9` / `381518631a4c` / `d5515ee52a8d` | **否** |
| seed2 | 2 | `0xffffffff95ddf4b0` | `3b4ddb5be23c` / `a299fc7cf1bb` / `d84f28b76dd0` | `13e1eb5c0a76` / `4813814241bd` / `a8501d851d25` | **否** |

- **6 跑 6 组三 sha1，两两全不同**（15 对，同 arm 内 3 对、跨 arm 12 对，全部不同）。
- `seed=1/2` 与 `seed=0` 也不同 —— 但这条**没有判据价值**：同一种子内两次就已经不同
  ⇒ 结果随运行的**时序**变，而不是随哈希序变。
- 墙钟稳定（run1 10.6~10.8 s / run2 9.3~9.4 s，run1 慢是首次页/量化缓存冷），
  三 sha1 的差异**不是"跑挂了"**：每跑 `0x310` 归零、magic 正常、227/227 conv 全跑到。

**判据结论**：`PYTHONHASHSEED=0` 两次不同 ⇒ **"非确定性来自 PS 哈希序"这条假设被证伪**。

---

## 2. 第 2 步：逐 op 布局 diff（227 个 PL op / 6 份账本）

每份账本记 `(seq, idx, name, slot, in_pa, out_pa, in_cap, out_cap, in_len, out_len, cin,cout,k,s,H,W,Hout,Wout)`。

| 对比 | 结果 |
|---|---|
| **seed0 run1 vs run2（同种子、同 arena）** | **227/227 行逐字段相同**（不同行 0） |
| seed1 run1 vs run2 | **227/227 相同** |
| seed0/seed1 跨 arm（4 份互比） | **全同** |
| 15 个对比对总计 | **10 对全同 / 5 对不同**；不同行合计 **10**（= 2 行 × 5 对） |
| 唯一的差异 run | **seed2_run2**（它自己的两次 run 里只有这一次偏了） |
| arena 总大小 / max_in / max_out / 槽数 | **六跑全同**：16.68 MB / 0.295 MB / 0.393 MB / 24 |

**差异的两行（seed2_run2 vs 其余 5 份）**：

| op | 名字 | slot | in_pa | out_pa |
|---|---|---|---|---|
| **79** | `…encoder_2.sam_blocks.0.b0.ghost_layers.1.primary_conv.conv` | 1 → **0** | `0x30148000` → `0x30100000` (**Δ −294912 = −max_in**) | `0x301f1000` → `0x30190000` (**Δ −397312 = −(max_out+4096)**) |
| **109** | `…sam_blocks.0.b4.ghost_layers.0.cheap_operation.conv` | 0 → **1** | `0x30100000` → `0x30148000` (**Δ +max_in**) | `0x30190000` → `0x301f1000` (**Δ +(max_out+4096)**) |

⇒ 这是**槽 0 与槽 1 的整体互换**（一对 op 换了同一组 IN/OUT 缓冲），不是"越界/漂移/新地址"。

**关键否证**：
- `seed0`、`seed1` 两个 arm 的两次 run **布局逐字段全同、三 sha1 却不同** ⇒ **布局不决定结果**；
- 唯一布局不同的 `seed2_run2` 与其余 5 跑相比，三 sha1 也没有"更不同"（本来就全不同）
  ⇒ **布局变化与输出差异无相关**；
- 布局互换**与哈希种子无关**（同种子的 seed2_run1 就是正常布局；seed0/seed1 两对都正常）
  ⇒ 它是 **PS 与 PL 的 prepare/finish 交错时序**造成的自由表顺序翻转
  （`_acquire_slot = self._free.pop(0)`、`_release_slot` 在 `finish` 里 append，见 `bin_net.py:817-831`、`bin_net.py:1129`），
  不是哈希序。

---

## 3. 第 3 步：布局无关 ⇒ 分歧点仍在 **PL conv 的尾巴**（逐 op sha1）

每跑还落了 395 个 op 的输出 sha1（`mode=md5`，另存前 64 B）。

| 观测 | 结果 |
|---|---|
| **首分歧 op 的分布（15 对）** | **op13：5 对**；**op15：10 对**；其余 344 对首分歧都落在这两个之一 |
| **op0…op12** | **6 跑全同**（含 op0 unshuffle、op1 convfp、op3/5/7/9/10/11 cat/add/interp 等 **PS 算子**） |
| op13（`…ghost_layers.0.cheap_operation.conv`，k3 cout32 64²） | **2 个变体**：`e9089a23…`×5、`955c0726…`×1（唯一偏离出现在 **最早完成的 run**，t=1.670 s） |
| op15（`…ghost_layers.1.primary_conv.conv`，k1 cin112→cout32 64²） | **6 个变体（每跑一个）** |
| **op13/op15 的前 64 B** | **六跑逐字节相同**（`0000a03e000080bd…` / `0000e83f0000883f…`）⇒ 差异**只在缓冲尾部**，不是从头错 |
| 142 个"有变化的 PS 算子" | **全部在 op13/op15 之后**（op14 cat 起）⇒ 是**继承**的差异，不是 PS 自己算错 |

⇒ 实际机制与 T-124 一致：**PL 输出写回与 done/读取的交接在"尾巴"上非确定**；
本次新增的信息是：**它与哈希序、与 arena 布局都无关**（同布局、同种子仍每次不同）。

---

## 4. 主机侧（离线）静态判据：planner 的 op 序与哈希序无关

`selftest_t131_host.py` 在**三个独立进程**（`PYTHONHASHSEED=0/1/2`）里各建一次 128² 的图：

| 检查 | 结果 |
|---|---|
| 字符串 hash 三种子确实不同（探针有效性） | ✓（`0xe41be406…` / `0xc3b55a98…` / `0x695b4df4…`） |
| **395 个 op 的 `(id, kind, name)` 序列** | **三种子逐 op 相同**（`n_ops=395, n_conv=227`） |
| max_in / max_out | 三种子同（`294912 / 393216`） |
| 张量 id 类型 | **全是 `int`**（int 的 hash 与 `PYTHONHASHSEED` 无关） |
| `_needs_f32` 集合内容 | 三种子同（本运行点为 0 个） |
| `layout_rec()` 假对象自测 | 字段齐全、pa/slot/长度正确、空对象不抛、指纹对 offset +8 敏感 ✓ |

代码依据：`build_graph` 的 op 序来自 `deploy/bin_weights_new/index.json` 的**顺序列表**；
拓扑调度用 `heapq` 按 `o.id`（int）取最靠前（`bin_net.py:3057-3066`）；
布局的唯一来源是 `_PlBackend.__init__` 里按 `nbuf` 顺序切 IN/OUT 槽（`bin_net.py:803-809`）
+ `_free.pop(0)`（`bin_net.py:817-831`），**没有一处 `for … in <str-keyed dict/set>` 决定顺序**。
`in_tids` 虽是 `set`（`bin_net.py:1692`），但元素是 **int** ⇒ 迭代序与哈希种子无关，
且它只用于 V50 的虚拟地址表（`BIN_V50_*` 全关，本运行点不参与）。

---

## 5. 结论（对父代理的直接影响）

1. **非确定性不是来自 PS 侧哈希序**：`PYTHONHASHSEED=0` 连跑两次三 sha1 仍不同；
   `seed=1/2` 不提供额外信息（同种子内已不同）。T-131 任务书里的"最强候选"**排除**。
2. **也不是 arena 布局**：同种子两跑布局 227 行逐字段全同、输出仍不同；
   唯一一次布局差异（`seed2_run2` 的槽 0↔1 互换）**与输出差异无相关**，且**与哈希种子无关**
   ⇒ 布局这条线可以关掉（`nbuf=24` + `LAZY_DEQ=0` 下布局近乎恒定，"布局漂移"只在 1/6 的跑上出现 2 行）。
3. **仍然指向 v53 的 PL 输出写回/完成交接**：首分歧 op 恒为 `op13`/`op15`
   （`npix=4096 / cout=32`），差异**只在缓冲区尾部**（前 64 B 六跑全同），
   PS 侧在其上游逐字节确定。⇒ 下一步只有两条路值得花钱：
   （a）**RTL**：`done` 之后加写回排空 fence / 或在最后一次行缓存 flush 后给一个"写回完成"计数；
   （b）**纯软件取证**：同一 OUT 槽在 `done` 后立刻连读两次（间隔 ~1 ms），若两次不同即定案（T-124 §4-②）。
4. **对 T-124 的补充**：T-124 的"首个不一致 op 漂移（13 vs 15）"不是 PS 随机性造成的，
   而是这两个 op 各自的 PL 写回竞争有各自的触发概率（op15 6/6 必错、op13 1/6）。

---

## 6. 板子现状

三次进程都在同一租约内完成，收尾只 `sync; sync`，**未 poweroff**：
`uptime 3:27`、`MemFree 782316 kB`、`MemAvailable 629832 kB`、
`CmaTotal 262144 kB / CmaFree 84096 kB`（跑完已释放）、`SwapTotal 524284 kB`；
v53 bit md5 跑后**不变**（`e19de032…`）；三个进程各自 `0x310=0` 归零、magic 正常；
无残留 python 进程（收尾前已按 pid `kill -TERM` 清过）。

---

## 7. 产物（`runs/board_t131/`）

| 文件 | 说明 |
|---|---|
| `probe_t131.py` | 板端探针：三 sha1 + 逐 op 布局（`_PlBackend.prepare` 只读钩子）+ 逐 op sha1（`finish`/`_exec_ps` 钩子） |
| `run_t131.sh` | 一次租约、三个进程（`PYTHONHASHSEED=0/1/2`）、部署+门禁+3×2 跑+拉回 |
| `selftest_t131_host.py` | 主机自测：`layout_rec()` 假对象 + 三个种子下的 op 序/形状静态比对 |
| `cmp_t131.py` | 主机侧判据：三 sha1 矩阵 + 布局逐行 diff + 首分歧 op |
| `t131_seed{0,1,2}_0741xx.{log,json}` | 三臂的运行记录（load/指纹/arena/每跑 sha1+布局指纹） |
| `layout_net128_seed*_run{1,2}.json` | 6 份**逐 op 布局账本**（227 行/份） |
| `ops_net128_seed*_run{1,2}.json` | 6 份**逐 op 输出 sha1 账本**（395 op/份，含前 64 B） |
| `t131_cmp.json` | 全部对比对的结构化判据 |
| `ps_*` | 板端 `md5sum -c` / 依赖闭包凭据；`t131_bit_after.md5` |

`cd runs/board_t131 && md5sum -c MD5SUMS`
