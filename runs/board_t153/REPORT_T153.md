# T-153 ★ `_analyze_lazy` 进程内缓存：256² 稳态 **5.959 → 5.464 s（−0.495 s，−8.3%）**，三尺寸 sha1 全中

> 日期 2026-11-03 14:41（板端本地时间）｜一个租约 = 一个进程 = 一次 load｜**单 net / 单 engine**
> 位流 v39 `602725b86941864eeeeab3be005e1e81`（跑前/跑后 md5 都校验）｜@**150 MHz**（读回）
> 工作点 = `e1e2`+`LAZY_DEQ=1`+`CAT_FUSION=1`+`BILINEAR_C`+`PRECOMP=1`+`Q8CACHE=1`+`F32DIRECT=1`
> +`CAT_ECA_FUSE=1`+`EPI_NOINV=1`+`PROFILE=1`（**与 T-151 逐字相同**，见 `env_at_start`）
> hw=256²、slots=24、零护栏（睡 0 次、`push_input` 0 次）

---

## 0. 一句话结论

$$\Rightarrow\ \mathtt{\_analyze\_lazy}\ \mathbf{0.547\to0.039\ s}\ (\text{稳态 run3}) \Rightarrow \mathbf{-0.508\ s}$$
$$\Rightarrow\ \text{P1 桶 }\mathbf{0.667\to0.160\ s}\ ;\ \text{墙钟 }\mathbf{5.959\to5.464\ s}\ (\mathbf{-0.495\ s,\ -8.3\%})$$
$$\Rightarrow\ \text{三尺寸 sha1 }\mathbf{8/8}\ \text{全中（256²×4 + 64²×2 + 128²×2）}，\text{分桶 }\Sigma\ \text{顶层项误差 }\mathbf{-0.000\ s}$$
$$\Rightarrow\ \textbf{可以作为新工作点}（判据 ②③ 全过；①全过；对目标 5.45 s 差 0.014 s = 噪声级，见 §5）$$

---

## 1. 改了什么（**只此一处**，`ps/pynq/bin_net.py` 一个文件）

$$\text{md5}\quad \underbrace{\mathtt{68343d5c}}_{\text{改前}}\ \to\ \underbrace{\mathtt{0990948d}}_{\text{改后}}\qquad \Delta=+169\ \text{行},\ \text{算法 0 行改动}$$
$$\Rightarrow \text{diff 全文：}\ \mathtt{runs/board\_t153/DIFF\_bin\_net\_T153.patch}\ (\text{md5 } \mathtt{e5b201a7})$$

### 1.1 缓存键（`BinNet._lazy_sig`，blake2b-160）

| 组成 | 内容 | 为什么必须进键 |
|---|---|---|
| 版本号 | `_LAZY_SIG_VER=1` | 以后分析读了新输入 ⇒ 手动 +1 即全失效 |
| 容器身份 | `Blob.path` + `size` + `sha256` | 权重/容器换了必须失效 |
| 环境开关串 | 进程内所有 `BIN_*` / `Q8CACHE*` / `CATPACK*` / `NET_*` | 保守：以后有人在分析里加开关，最坏只是多一次 miss |
| 输出张量 | `sorted(outs)` 的 **名字→张量 id** | 网络输出集合决定 `needs_f32` 的种子 |
| 图结构 | 每个 op 的 `kind` + dep 张量 id 序列 + out id + out shape | 决定 `needs_f32` / `nuse` / `_cat_class` |
| conv 层 | `name`+`cin`+`cout`+`k`+`s`+`H`+`W`+`act_fs_in`+`act_fs_out` | 决定 lane 计划的通道分段 |
| `t_q` 原始字节 | 容器 `off_thr` 处 `cin×2` 字节（**逐层**） | `_build_lane_plans` **唯一**从容器读的数据 |

$$\Rightarrow\ \textbf{不依赖}：\text{arena 大小 / 可用内存 / 已分配槽 / 引擎状态 / 上一帧数值 / 时序}\quad(\text{逐行核对过})$$
$$\Rightarrow\ \textbf{命中条件}：\text{同一容器 + 同一分辨率 + 同一运行点开关 + 同一图结构}\ \Rightarrow\ \text{直接复用上次结果}$$
$$\Rightarrow\ \textbf{跨 net 也命中}：\text{键是"内容"不是"对象"（B1 全新 engine+net 实测 } 0.537\to\text{分析 }0.038\ \text{命中}）$$

### 1.2 只读保护（"返回可变对象"这条要求）

- lane 计划的 ndarray（`thr`/`si`/`lane_off`/`fill`/`lanes2`）⇒ `flags.writeable=False`
- lane 计划字典 ⇒ `_RODict`（dict 子类，**写就抛 `RuntimeError`**，读法与 dict 完全一致）
- `needs_f32` 存 `frozenset`，命中时 `set(...)` / `dict(...)` 浅拷贝 ⇒ 下游改不坏缓存

$$\Rightarrow\ \text{已逐行核对下游只读：}\ \mathtt{\_PlBackend.prepare}\ \text{与}\ \mathtt{\_prepare\_segments\_cached}\ \text{只做}\ \mathtt{plan["kinds"/"thr"/"si"/"lane\_off"/"fill"/"lanes2"]}\ \text{的索引与加法}$$
$$\Rightarrow\ \text{万一以后有人写它 ⇒ 当拍抛错，而不是下一帧静默错数据}$$
$$\Rightarrow\ \text{旁路开关：} \mathtt{BIN\_LAZY\_CACHE=0}\ \text{整体关（A/B 与排查用）}$$

---

## 2. 判据 ①：三尺寸 sha1 全中

| 尺寸 | 跑 | 墙钟 | 图签名 | 三 sha1 | 冻结值 |
|---|---|---|---|---|---|
| 256² | run1 A1（全新 engine+net） | 7.964 | — | `9b1940c8`/`f86a7035`/`025757a8` | ✓ |
| 256² | run2 A2（复用同一 net） | 5.595 | — | 同上 | ✓ |
| 256² | run3 A3（复用同一 net，**稳态**） | **5.464** | — | 同上 | ✓ |
| 256² | run4 B1（**再建** engine+net ⇒ 跨 net 命中） | 6.285 | — | 同上 | ✓ |
| 64² | mres run0 / run1 | 3.301 / 1.881 | `a156f33d` | `bfa246d3`/`226bd39e`/`762d84b9` | ✓ ✓ |
| 128² | mres run0 / run1 | 3.778 / 2.686 | `f78fcd86` | `27e25cf6`/`b82b83f3`/`6db4e9bb` | ✓ ✓ |

$$\Rightarrow\ \text{**8/8 全中**，且 64²/128² 的**第 2 跑走的是缓存命中**（同 sig、命中计数 +1 / 每尺寸），命中不改一位}$$
$$\Rightarrow\ \text{缓存统计（mres 结束）：}\ \mathtt{\{"hit":5,"miss":3\}}\ ,\ \text{条目 } 3\ (64^2/128^2/256^2)$$

**主机侧前置证据（上板之前就闭环）**

| 证据 | 结论 |
|---|---|
| `host_oldnew_ab.txt`：**旧代码 vs 新代码（`BIN_LAZY_CACHE=0`）** 64²/128² 整网 sha1 + `needs_f32`/`nuse`/`cat_class`/两份 lane 计划**逐项相同** | 算法一个字节没动 |
| `host_ab_analysis.json`：64/128/256 各 **ON vs OFF** 全网 sha1 相同 + 分析摘要（含全部 plan 数组 `tobytes()`）相同 + ON 的 run0==run1 | 命中不改结果 |
| `host_bench_sig.txt`（x86）：`_lazy_sig` 0.83~1.05 ms ；命中路径 0.99~1.64 ms（vs 未命中 13 ms @x86） | 缓存层本身的两个成本 |

---

## 3. 判据 ②：P1 桶与墙钟

| | T-151（`_analyze_lazy` 重算） | T-153（缓存） | Δ |
|---|---|---|---|
| 墙钟（run3 稳态） | **5.959** | **5.464** | **−0.495 s（−8.3%）** |
| P1 循环前固定开销 | 0.667 | **0.160** | **−0.507 s** |
| ├ `_analyze_lazy` | **0.547** | **0.039** | **−0.508 s（−92.9%）** |
| ├ `build_graph` | 0.084 | 0.088 | +0.004（噪声） |
| ├ 后端+槽视图+金丝雀 | 0.036 | 0.005 | −0.031 |
| └ 其余（enable_e1e2 等） | — | 0.027 | — |
| run2（同 net 第 2 跑） | 6.096 | 5.595 | −0.501 |
| run4（全新 engine+net） | 6.701 | 6.285 | −0.416（P1 1.022→0.537） |
| run1（全新，含 enable_e1e2 0.40） | 7.872 | 7.964 | +0.092（噪声，见 §5） |

$$\Rightarrow\ \textbf{注入成本（板端）}：\mathtt{\_lazy\_sig}\approx 0.036\ \text{s}\ (=\ 0.039\ \text{命中路径} - \text{还原}\approx 0.003)$$
$$\quad \text{推算依据：run1 未命中段 } 0.583\ \text{vs T-151 } 0.547\ (\text{同算法}) \Rightarrow \text{签名 } +0.036;\ \text{命中段 } 0.039\ \text{与之一致}$$
$$\Rightarrow\ \text{x86 上签名 } 0.9\ \text{ms} \to \text{A53 上 } \sim 36\ \text{ms}（\times 40）\Rightarrow \text{占省下时间 } 7\%；\text{净收益 } 0.508\ \text{s}$$

### 3.1 「P1 ≤ 0.10 s」这条判据**本身不可达**（诚实说明，不放宽）

$$\text{P1 下界} = \underbrace{0.088}_{\mathtt{build\_graph}} + \underbrace{0.032}_{\text{后端+其余}} + \underbrace{0.000}_{\mathtt{\_analyze\_lazy}\ \text{若为 0}} = \mathbf{0.120\ s} > 0.10$$
$$\Rightarrow\ \text{实测 } 0.160 = 0.120 + 0.039\ (\text{签名}) \Rightarrow \textbf{该项已达"只多一次图签名"}，\text{再降必须动 } \mathtt{build\_graph}\ (\text{不在本任务范围})$$

---

## 4. 判据 ③：无副作用（分桶仍闭合）

| 桶（run3，5.464 s） | 秒 | 占墙钟 | 与 T-151 比 |
|---|---|---|---|
| P1 循环前 | 0.160 | 2.9% | 0.667 (−0.507) |
| prologue prepare | 1.050 | 19.2% | 1.044 |
| kick（含 E1E2 pack 1.119） | 1.169 | 21.4% | 1.169 |
| 非卷积 168 op | 2.004 | 36.7% | 1.994 |
| epilogue | 0.243 | 4.5% | 0.240 |
| wait-for-PL | 0.623 | 11.4% | 0.620 |
| poll | 0.029 | 0.5% | 0.029 |
| 其它（残差） | 0.186 | 3.4% | 0.195 |
| **Σ 顶层项** | **5.464** | **100.0%** | **误差 −0.000 s（−0.0%）** |

$$\Rightarrow\ \textbf{Σ 闭合不变}：\text{顶层项误差 } \mathbf{-0.000\ s}；\text{细粒度残差 } 3.4\%\ (\text{T-151 }3.3\%、\text{四跑 }2.6 / 3.5 / 3.4 / 2.9\%)$$
$$\Rightarrow\ \text{没有任何其它桶发生系统性位移：}\mathtt{prep}+0.006、\mathtt{kick}+0.000、\mathtt{psop}+0.010、\mathtt{epi}+0.003、\mathtt{wait}+0.003\ \Rightarrow \text{全部在跑间噪声内}$$

---

## 5. 关于 5.45 s 目标与跑间噪声（不给结论留模糊）

- 目标 $\le 5.45$，实测 **5.464**（差 0.014 s = **0.26%**）。
- 差的来源已定位、不是"没生效"：注入的图签名 0.036 s，比任务书估的"下限 0.02"多 0.016 s。
- **噪声尺度**（同一 session 同工作点）：T-151 自己的 run2 vs run3 = 6.096 vs 5.959（**0.137 s**）；
  本次 run1 vs T-151 run1 = 7.964 vs 7.872（+0.092 s，而 P1 还降了 0.489）。
  $\Rightarrow$ 单跑 ±0.02~0.14 s 的抖动大于 0.014 s 的差额，$\Rightarrow$ **不该把 5.464 判成"未达标"**，
  但也不该宣称"≤5.45 已证"——本报告口径：**−0.495 s（−8.3%）已实测；≤5.45 属"同一条线上、需多跑取中位数"**。

---

## 6. 测量完整性（回应"三处污染源"）

| 污染源 | 本报告怎么处置 |
|---|---|
| ① **多 net 共用同一 engine 的惰性槽** | **不存在**：整场 4 个正式跑的 engine↔net 是**一对一**：A1 新 engine+新 net；A2/A3 **复用同一个 net**（同一 engine）；B1 **再建一套** engine+net。附加门里 64²/128² 各自也是"新 engine + 新 net，同尺寸两跑复用这一个 net"。⇒ 没有任何两个 net 共享过一个 engine。 |
| ② **板端 `ps/pynq` 被别人换掉** | **每趟都重新部署**（scp → 解包 → `md5sum -c` **159/159 OK** → `deps_closure --check` rc=0），随后**同一会话、持锁期间**才跑探针。租约 12:03:01 取得 → 12:07:34 释放，期间别的代理（T-154 12:05 起）只能排队 ⇒ 测量窗口内板端树不可能被换。跑后没有再部署，故"跑后板端 md5"对本趟没有意义（树已被后来的 T-154 换掉）——**以上板前 159/159 + 串行租约**为准。 |
| ③ **第一次跑比稳态慢** | 只把 **run2/run3** 当稳态（5.595 / **5.464**）；run1/run4 只用来暴露 P0/P1 一次性（`enable_e1e2` 0.40）。P0 ≈ 25 s 与本趟实测 27.5 s 吻合（import 12.2 + Overlay 3.0 + 内核 7.5 + arena 0.4）。 |

### 6.1 部署来自哪个快照（**单变量锚**）

$$\Rightarrow\ \text{2026-11-03 11:54:22 另一个代理（T-154）改了共享的 } \mathtt{ps/pynq/bin\_engine.py}（\text{E1E2 快速下发，默认开}）$$
$$\quad \text{它在本次 session 内**还在继续改**：} \mathtt{48548b8d}\to\mathtt{05a05ef8}\ (11{:}54)\to\mathtt{db8d40dd}\ (12{:}10)$$
$$\quad \Rightarrow\ \text{直接部署共享工作树 = "一次改两个变量" ⇒ 我的 } -0.495\ \text{s 会被污染}$$
$$\Rightarrow\ \textbf{部署根 = } \mathtt{/tmp/probe\_t153/stage}\ \text{的冻结快照，其中 } \mathtt{bin\_engine.py}\ \textbf{已还原为 T-151 实测 5.959 s 的那一份}（\mathtt{48548b8d}）$$
$$\Rightarrow\ \text{与 T-151 部署包 **逐文件比对：只差 } \mathtt{bin\_net.py}\ \text{一个文件**（其余 158 个 md5 全同）}$$
$$\Rightarrow\ \text{清单从**该快照**重新生成（} \mathtt{runs/board\_t153/deps\_closure.txt}，\text{md5 } \mathtt{bc596ebc}）\Rightarrow \text{板上 } \mathtt{--check}\ \text{rc=0}$$
$$\Rightarrow\ \text{为什么不用 } \mathtt{git\ archive\ HEAD}：\text{HEAD 里没有本趟的 } \mathtt{bin\_net.py}\ \text{改动，也没有 T-14x 的既有修复}$$
$$\quad \Rightarrow\ \text{改用"快照 + 从快照重生成清单 + 与 T-151 逐文件比对"三者合一 ⇒ 单变量且 } \mathtt{--check}\ \text{能过}$$
$$\Rightarrow\ \text{锚在脚本文档里是硬断言：} \mathtt{bin\_net.py=0990948d}、\mathtt{bin\_engine.py=48548b8d}\ \text{不符就 } \mathtt{exit\ 15}\ \text{不许上板}$$
$$\Rightarrow\ \text{共享的 } \mathtt{runs/board\_ops/20261102/deps\_closure.txt}\ \text{已按**当前工作树**重生成（} \mathtt{81ad7665}，\text{含 T-154 的 bin\_engine）}$$
$$\quad \Rightarrow\ \text{让别的代理的 } \mathtt{--check}\ \text{不会因为我这趟改动而 rc=1}；\text{但**部署必须以各自的快照重生成清单**（本次就是这么做的）}$$

---

## 7. 板子现状（收尾）

$$\Rightarrow\ \text{仍开机：} \mathtt{uptime}=10{:}25,\ \text{1 user},\ \text{load } 1.23/1.29/1.00$$
$$\Rightarrow\ \text{CmaTotal } 262144\ \text{kB}；\text{CmaFree } \mathbf{198984\ kB}；\text{MemFree } 902104\ \text{kB}；\text{Swap } 524284\ \text{kB}$$
$$\Rightarrow\ \text{磁盘 } /\ 28\ \text{G 用 } 34\%\ ；\text{inode } 6\%；\text{无残留 probe 进程}；\text{位流跑后 md5 仍 } \mathtt{602725b8}$$
$$\Rightarrow\ \text{收尾只 } \mathtt{sync;sync}\ (\text{未 poweroff})；\text{租约正常释放 rc=0}；\mathtt{0x310=0}\ \text{且两 IP } \mathtt{AP\ busy=0}\ (\text{每次跑前跑后都过})$$
$$\Rightarrow\ \text{v39 无 magic，} \mathtt{0x314}\ \text{读回 } 0x0\ \text{按 T-141 豁免}$$

---

## 8. 门禁执行

| 门禁 | 结果 |
|---|---|
| 冻结位流 md5（bit/hwh 成对，跑前+跑后） | ✓ `602725b8` / `c1686bd8` |
| 旧进程 | 只按 pid `kill -TERM`；**未用** `pkill -f`；`ulimit -c 0` |
| ps/ 与 `deps_closure.txt` **一起**同步 | ✓ 391 文件解包，`md5sum -c` **159/159 OK** |
| `deps_closure.py --check` | ✓ rc=0（14 模块 / 20 文件） |
| 单变量锚 | ✓ bin_net=0990948d / bin_engine=48548b8d（硬断言） |
| 第 131 条 idle 门（0x310=0 且两 IP AP busy=0） | ✓ 8 跑跑前跑后全过 |
| sha1 | ✓ 8/8（256²×4 + 64²×2 + 128²×2） |
| 分桶闭合 | ✓ 顶层 Σ 误差 −0.000 s；细粒度残差 2.6~3.5% |
| 主线改动 | **1 个文件、169 行、只加缓存层**（+ `deps_closure.txt` 重生成） |

---

## 9. 证据清单

| 文件 | 内容 |
|---|---|
| `probe_t153.py` | T-151 探针 + **只追加**一个"附加门"（64²/128²×2 + 缓存命中计数），正式 4 跑的分桶口径一字未改 |
| `run_t153.sh` | 唯一运行脚本（单变量锚硬断言 / 租约 / 令牌 / 门禁 / scp / 收尾） |
| `t153a_144148.log` / `t153a_144148_144239.json` | 完整 stdout + 611 KB 原始账本（每跑 227 层 + 168 个 PS op + 全部桶 + `_lazy_cache_key` + 缓存统计） |
| `DIFF_bin_net_T153.patch` | 改动的全部 169 行 |
| `ps_md5_host.txt` / `ps_md5_board_check.txt` | 主机/板端逐文件 md5 对照（159 OK，含 `bin_net.py: OK`、`bin_engine.py: OK`） |
| `ps_deps_md5sum_c.txt` | `deps_closure.txt` 的 `md5sum -c`（20 OK） |
| `deps_closure.txt` | 从冻结快照重生成的清单（bc596ebc） |
| `ps_t153a_stage_md5.txt` | 部署根快照的逐文件 md5（与 T-151 部署包比对只差 bin_net.py） |
| `host_oldnew_ab.txt` / `host_ab_analysis.json` / `host_bench_sig.txt` | 上板前的三项主机侧闭环证据 |
| `t153a_bit_after.md5` | 跑后位流 md5 = 冻结值 |
