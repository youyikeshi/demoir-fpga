# T-156 ★ 把 MMIO 事务数清：**总 0.58 s（不是 1.4 s），轮询策略实测无效**；改掉 pack 的 4 处非事务冗余 ⇒ **5.464 → 5.33 s（−0.15 ~ −0.26 s）**

> 日期 2026-11-03 12:42（主机）｜板端本地时间 15:17｜一个租约 = 一个进程 = **一次 load**
> 位流 v39 `602725b8`（跑前/跑后 md5 均校验）｜@**150 MHz**（读回）｜hw=256²、slots=24、**零护栏**
> 工作点 = `e1e2`+`LAZY_DEQ=1`+`CAT_FUSION=1`+`BILINEAR_C`+`PRECOMP=1`+`Q8CACHE=1`+`F32DIRECT=1`
> +`CAT_ECA_FUSE=1`+`EPI_NOINV=1`+`PROFILE=1`+**T-153 惰性分析缓存**（与 T-151/T-153 逐项相同）
> 部署树 = **`git archive HEAD` + 唯一一份 T-153 `bin_net.py`(0990948d)**，
> 144/144 与 T-153 实测部署逐文件 md5 相同（`stage/`）⇒ 基线就是 5.464 s 那个点
> 判据：256² **5/5**、128² 1/1、64² 1/1 三 sha1 全中（`9b1940c8`/`f86a7035`/`025757a8`；
> `27e25cf6`/`b82b83f3`/`6db4e9bb`；`bfa246d3`/`226bd39e`/`762d84b9`）

---

## 0. 一句话结论

$$\Rightarrow\ \textbf{MMIO 事务总耗时} = 10365\ \text{读}\times36.5 + 6158\ \text{写}\times(17\sim50) = \mathbf{0.62\sim0.69\ s}\ (\approx\mathbf{0.65\ s}) = \mathbf{12\%}\ \text{墙钟}$$
$$\quad (\text{父代理的 }\mathtt{v39}{+}\mathtt{LAZY}\ \text{估算 } \approx1.4\ \text{s}\ \textbf{高估 }\approx2.2\text{ 倍；pack 桶 1.141 s 里事务只占 0.40 s})$$
$$\Rightarrow\ \textbf{三个候选改法(M1/M2/M3)全部实测否证}：\mathtt{BIN\_MMIO\_POLL=tight}\ \text{ABBA} \Rightarrow \mathbf{-0.029\ s}\ (\text{噪声内})$$
$$\Rightarrow\ \textbf{能拿的只有“非事务”那 4 处纯冗余}：\text{实测 } \mathbf{-0.145\ s}\ (\text{pack 桶 }1.165\to1.020，\text{跑间重复性 }0.3\%)$$
$$\quad \text{墙钟同会话 ABBA } \mathbf{5.647\to5.391}\ (\text{中位},\ -0.255)\ ;\ \text{保守（最好臂对最好臂）} \mathbf{5.501\to5.328} = -0.173$$
$$\Rightarrow\ \textbf{可以作为新工作点}（事务 0 变化 + 三尺寸 sha1 全中）；但 $\le 5.0\ \text{s}$ **靠减事务到不了**（全删也只有 0.58 s）$$

---

## 1. 第 1 步：一次整网 256² 的 MMIO 事务分类表（上板实测，`t156a`）

口径：计时钩子只挂在**第 1 跑**（丢弃的 warmup），逐次 `perf_counter` 包住 `_rd`/`_wr`/`_wr_seq`
（`_wr_seq` 的耗时按"真正落总线的字数"摊到各偏移）；A/B 的 4 个计时跑**零钩子**。

| 类别 | 次数 | 累计 | 单次 | 判读 |
|---|---|---|---|---|
| **c) pack 的 done 轮询读**（AP_RET） | 6542 | **229.6 ms** | 35.1 µs | 9.4 次/命令；**不可减**（见 §2） |
| **b) conv reap 的 done 轮询读** | 3192 | **118.9 ms** | 37.2 µs | 14 次/层，与 PL 并行，不是纯开销 |
| conv `kick`（start 写） | 227 | 13.8 ms | 60.8 µs | 必需 |
| `_wait_idle` 读（每层 1 次） | 227 | 10.8 ms | 47.6 µs | **纯冗余**（上条命令已确认 done∧¬busy） |
| epilogue/其它读 | 406 | 19.2 ms | 47.3 µs | 其中 404 是 `read_out` 前后 |
| **a) pack 的配置写** | 3494 | **172.1 ms** | 49.2 µs | 5.0 次/命令（见下） |
| **a) conv 的配置写（precomp 直写 `mmio.write`，绕过 `_wr`/`_wr_seq` ⇒ **钩子看不见**）** | **2433** | **~0.10–0.12 s** | 40–50 µs | 10.7 次/层；T-153 的 `cfg 写配置页+金丝雀` 桶 = 0.122 s |
| 其它写 | 4 | 0.2 ms | — | E_DESC_HI/E_OUT_HI |
| **Σ** | **10365 读 + 6158 写** | **≈0.65 s** | — | **占 5.49 s 的 12%** |

> 口径说明：钩子能看到的写只有 **3725** 次（pack 3494 + start 227 + 其它 4），
> 而 `engine._writes` 一跑是 **6158** 次 ⇒ 差 **2433** 次是 `arm_plan` 预计算表的**直写回放**
> （`_mw = self.mmio.write`，绕过 `_wr`/`_wr_seq`，不经过任何 Python 出口）。
> 这部分的耗时取 T-153 自己的 `cfg 写配置页 + 金丝雀` 桶（0.122 s，含金丝雀 memset）。
> **读侧没有这个盲区**：钩子 10365 vs `engine._reads` 10367（差 2 = 跑前后体检）。

**pack 的 5.0 次写/命令拆开**：`E_OUT 698` + `AP_CTRL(start) 698` + `E_DESC 591` + `E_GROUP 582`
+ `E_RUN_UNIT 925`（= 每命令 1 次 `select(4)` + 每层 1 次强制 `select(0)`）。
`C/H/W` 三个字**一次都没真写**（影子命中：conv 的 `_ensure_cfg` 刚写过同样的值）。

### 1.1 哪些冗余 / 可合并 / 不可动

| 判定 | 项 | 依据 |
|---|---|---|
| **纯冗余（删）** | `_flush(vthr)`+`_flush(vdesc)` 每层 2 次刷**同一块**（`RegionBuffer.flush()` 直接转 owner） | 472 次/跑 × 222 µs |
| **纯冗余（删）** | 直达路径的 `_invalidate(ov)`：产物**只被 PL 当下一次的 IN**，CPU 一个字节都不读 | 270 次/跑 × 211 µs |
| **纯冗余（删）** | `chain_on()` 每层 2 次重解析 `BIN_V50_FUSE` 环境串 | T-154 实测 17 ms |
| **纯冗余（删）** | `run_one` 的 `while left>0` 解析拍数：本网 698 条命令共 **9365 次迭代**，有闭式 | 17+9·hw+50·⌈hw/256⌉ |
| **可合并** | 同一命令的 6 个配置字由 2 次 `_wr_seq` 合成 1 次（**总线字数/顺序不变**） | 少一次 Python 调用 |
| **不可合并** | 698 条 E1E2 命令 → 一条链：**只有 RTL（v50 描述符链）能做**；v39 上一条 start 一个描述符组 | `_run_desc` 的 `for g in range(cb)` |
| **不可减** | pack 的 9.4 次/命令轮询读 | §2 的 A/B |

---

## 2. 第 2 步：三个候选改法，逐个实测判定

### M1 合并等待 ⇒ **不存在可合并的等待**（结构上串行）

$$\mathtt{pack}(L)\ \text{读的是}\ \mathtt{conv}(L{-}1)\ \text{刚写的 OUT 槽} \Rightarrow \text{必须等 } L{-}1\ \text{done}$$
⇒ 循环里 `pack → conv` 是**严格串行**，"交错轮询"这个前提不成立；唯一真冗余是每层 1 次 `_wait_idle`（227 次读，10.8 ms）。

### M2 合并配置写 ⇒ **硬件不支持，一次 `pack_into` 仍是逐字 AXI-Lite 事务**

6 个字里有空洞：`C 0x18 / H 0x20 / W 0x28`（缺 0x1C、0x24）、`E_GROUP 0x204 / E_DESC 0x208 /
**0x20C 是高字** / E_OUT 0x210`。T-154 已实测：同样 6 个字 `_wr_seq`(Python) 0.108 ms vs
`mmio_batch::write_pairs`(C) 0.109 ms ⇒ **钱全在总线上，换语言/换写法一分不省**。

### M3 减少轮询频率 ⇒ **实测中性（本次 ABBA 再证一次）**

| 臂 | 4 跑（256²，同一 net/arena） | 中位 | AP_RET 读次数 |
|---|---|---|---|
| A 自适应（现状） | 5.490 / 5.669 | **5.580** | 14.6k / 15.0k |
| B `tight`（去掉每次轮询的 `sleep(0)`≈47 µs） | 5.512 / 5.589 | **5.551** | **25.6k / 27.7k** |

$$\Rightarrow \Delta = \mathbf{-0.029\ s\ (-0.5\%)}\ \text{（噪声内）}；\text{pack 桶 } 1.141 \to 1.143\ \text{s 不动}；\text{读次数}\times1.75$$
$$\Rightarrow\ \text{为什么换不回来：读既是“等待”又占“总线”。普查里**每一次** AP_RET 读都在 PL 访存期间，}$$
$$\quad \text{实测 36.5 µs/次（空闲 14.5 µs）} \Rightarrow \text{少睡一次就多读一次，} \sum t\ \text{不变而总线更挤。}$$
$$\quad \text{与 T-23 主机模型、T-154 两次 A/B **三次独立一致**。}$$

### 推荐（已实施）：只拿"非事务"的 4 处纯冗余，**不动任何一次总线访问**

$$\text{预计 } \approx -0.10\sim0.15\ \text{s}；\textbf{风险 } 0\ (\text{写序列可逐条证明等价})；\text{判据 = 三尺寸 sha1 + trace 逐条相同}$$

---

## 3. 第 3 步：实施 + 验证

### 3.1 改了什么（**只 `ps/pynq/bin_engine.py` 一个文件**）

$$\text{md5}\quad \underbrace{\mathtt{48548b8d}}_{\text{改前}}\ \to\ \underbrace{\mathtt{2fd3d099}}_{\text{改后}}\qquad +90/-22\ \text{行；算法 0 行}$$
$$\text{diff: } \mathtt{runs/board\_t156/DIFF\_bin\_engine\_T156.patch}\ (\text{md5 } \mathtt{116b4635})$$
$$\text{旁路开关 } \mathtt{BIN\_T156\_PACK=0}\ \Rightarrow\ \text{逐字节回到改前（A/B 与排查用）}$$

| # | 改动 | 依据 |
|---|---|---|
| ① | `_flush(vthr)` + `_flush(vdesc)` → **一次**（owner 相同才省，不同仍各刷一次） | 每层 2 次刷同一块 |
| ② | 直达路径（`out_buf` 给了）**跳过 `_invalidate`**；`out_buf is None` 仍照旧 | CPU 从不读该产物 |
| ③ | `chain_on()` 按 **fuse 值**进程内缓存（fuse 变了就重算） | 每层 2 次环境串解析 |
| ④ | `run_one` 解析拍数改闭式 `17+9·hw+50·⌈hw/256⌉` | 本网 9365 次循环迭代 |
| ⑤ | 同一命令 6 个配置字 → **1 次** `_wr_seq` | 少一次 Python 调用 |

### 3.2 主机侧门禁（上板**之前**就闭环）：总线访问**逐条**等价

用项目自己的 C1 判据（`ps/tools/verify_t23_mmio.py`，记录点是 `_wr/_wr_seq/_rd` 的 `_trace_log`；
`FakeE1E2Unit` 跑完 **698 条命令** + 4 层 conv）：

| 项 | 改前 | 改后 | 判定 |
|---|---|---|---|
| trace 条数 | 10471 | 10471 | ✓ |
| **写记录逐条相同**（off, val, force, sent） | 5585 条 | 5585 条 | **✓ 逐条 `==`** |
| **读记录逐条相同** | 4886 条 | 4886 条 | **✓ 逐条 `==`** |
| 影子跳过 / start 次数 | 2640 / 698 | 2640 / 698 | ✓ |
| pack 输出 sha1（698 命令 / 61.1 MB） | `02a49957` | `02a49957` | ✓ |
| 寄存器页 sha1 / 页写次数 | `1ceaf73d` / 2945 | 同 | ✓ |
| conv 路径 4 层 rows + trace（172 条） | — | 完全相同 | ✓ |

$$\Rightarrow\ \textbf{本次改动一个总线事务都没改}（地址/值/顺序/影子跳过/start 次数全同）$$

### 3.3 板端 A/B（**同一 net / 同一 arena**，ABBA：A A B B A）

| 跑 | 臂 | `T156_PACK` | 墙钟 | pack 桶 | flush | invalidate |
|---|---|---|---|---|---|---|
| run1（warmup+普查，丢弃） | A | off | 7.887 | 1.348 | 467 | 265 |
| run2 | **A 原路径** | off | **5.501** | **1.166** | 470 | 268 |
| run3 | **B 精简** | on | **5.455** | **1.022** | 245 | 44 |
| run4 | **B 精简** | on | **5.328** | **1.019** | 246 | 45 |
| run5 | **A 原路径** | off | **5.792** ⚠ | **1.165** | 475 | 273 |

- **pack 桶（本改动的直接作用点，跑间重复性 0.3%）**：$1.165 \to 1.020 = \mathbf{-0.145\ s}$
- **墙钟**：中位 $5.647\to5.391=\mathbf{-0.255\ s}$；保守（最好臂对最好臂）$5.501\to5.328=\mathbf{-0.173\ s}$
- ⚠ run5 = 5.792 是**环境噪声**（同时段 load average 1.11，`psop 2.126` vs `1.992`、`prep 1.243` vs `1.198` 一起变大），
  不是 B 的副作用；因此**墙钟的诚实结论是 $-0.15\pm0.13\ \text{s}$，pack 桶是 $-0.145\ \text{s}\pm0.003$**。
- flush $472\to245$、invalidate $270\to44$：④ 之外三项按预期落地。

### 3.4 判据

| 尺寸 | 跑数 | 三 sha1 | 判定 |
|---|---|---|---|
| 256² | 5（含普查跑） | `9b1940c8`/`f86a7035`/`025757a8` | **5/5 ✓** |
| 128² | 1（新 net，B 臂） | `27e25cf6`/`b82b83f3`/`6db4e9bb` | ✓ |
| 64² | 1（新 net，B 臂） | `bfa246d3`/`226bd39e`/`762d84b9` | ✓ |

$$\Rightarrow\ \textbf{墙钟 } 5.464 \to \mathbf{5.33}\ (\text{本会话最好臂})\ /\ 5.39\ (\text{中位})\ ;\ \textbf{目标}\le5.0\ \textbf{未达}$$
$$\Rightarrow\ \textbf{可作为新工作点}（事务 0 变化、数值等价、有旁路开关）；\text{收益 }-0.15\sim-0.26\ \text{s}\ (-2.7\%\sim-4.5\%)$$

---

## 4. 瓶颈归因：为什么"减事务"这条路到 5.0 不够

$$\text{pack 桶 } 1.141\ \text{s} = \underbrace{0.32}_{\text{PL 硬件 (DBG\_E\_CYC)}} + \underbrace{0.40}_{\text{MMIO 事务}} + \underbrace{0.42}_{\text{PS Python/刷缓存}}$$
$$\Rightarrow\ \text{事务全删 } \Rightarrow \text{最多 } -0.40\ \text{s}；\text{而事务数**已经贴着结构下限**：}$$

| 不可减的原因 | 数字 |
|---|---|
| 轮询读 = 等待本身 | 9.4 次/命令 × 36.5 µs；`tight` 把读换成更多读（×1.75），`sleep` 过冲 50–300 µs（T-154） |
| 命令数 = 698 = Σ⌈cin/64⌉ | 要再降只有 RTL（v50 一条链），v39 上一条 start 一个描述符组 |
| 配置写 = 3 个真正变化的字/命令 | `E_OUT`/`E_DESC`/`E_GROUP` 每条命令都变，影子缓存已把 `C/H/W` 全吃掉 |

$$\Rightarrow\ \textbf{下一个真正的杠杆不在 PS 事务，而在：}(a)\ \text{非卷积算子 } 2.004\ \text{s}（\text{36.7\%}）,\ (b)\ \texttt{convfp}\ 1.256\ \text{s},\ (c)\ \text{prologue } \texttt{q8\_segments}\ 0.500\ \text{s}$$

---

## 5. 纪律与门禁

| 门禁 | 结果 |
|---|---|
| 部署来源 | `git archive HEAD` + T-153 `bin_net.py`（**不用工作树**；也**不用** `runs/board_t154/stage`——那份被 T-154 实验补丁污染，`bin_engine=db8d40dd`） |
| `ps/` 与 `deps_closure.txt` 同步 | ✓ 两次重新部署；`md5sum -c` **144/144**、`deps_closure --check` **20/20** rc=0 |
| 位流 md5 | ✓ `602725b8` 跑前 + 跑后（`t156a_bit_after.md5` / `t156b_bit_after.md5`） |
| **单 net / 单 arena** | ✓ 256² 五跑同一个 net/engine/arena；128²/64² 各新建 net（同一 engine） |
| 板端 `ps/` md5 前后 | ✓ 部署即 `md5sum -c`；两次会话各校验 144 文件 |
| 单变量 | ✓ 第 1 次会话只切 `engine._poll_mode`（同一份已导入模块）；第 2 次只切 `T156_PACK_ON[0]` |
| 第 131 条 idle 门 | ✓ 每跑前后（`0x310=0` 且两 IP `busy=0`） |
| 旧进程 | 只按 pid `kill -TERM`；未用 `pkill -f`；`ulimit -c 0`；`sync; sync` 收尾，未 poweroff |
| 主线改动 | **无**（`pynq_specialized_code/` 一个字节没动；改动只落在 `runs/board_t156/stage_b/`） |

## 6. 产物

| 文件 | 内容 |
|---|---|
| `probe_t156.py` / `run_t156.sh` | 普查 + ABBA 探针、单租约运行脚本（两次会话共用） |
| `stage/` , `stage_b/` | 基线部署树（HEAD+T-153）、改后部署树（T-156 四项） |
| `DIFF_bin_engine_T156.patch` | 唯一改动的完整 diff（md5 `116b4635`） |
| `t156a_probe.log/.json` | 第 1 步：事务普查 + `tight` ABBA（`runs/board_t156/`） |
| `sess_b/t156b_probe.log/.json` | 第 2 步：T-156 四项 ABBA + 三尺寸 sha1（`runs/board_t156/sess_b/`） |
| `ps_md5_host.txt` / `ps_md5_board_check.txt` / `ps_deps_md5sum_c.txt` | 部署一致性证据（两套会话各一份） |

## 7. 给主代理的三句话

1. **"AXI-Lite 事务是 1.4 s 大头"要修正为 ≈0.65 s（12%）**——pack 桶 1.141 s 里事务 0.40 s、PL 硬件 0.32 s、PS Python 0.42 s。
2. **轮询相关的三种改法（tight / 预估睡 / C 批量）已第四次独立否证**，不要再派任务去降读次数。
3. **可合并入基线的是 T-156 那 4 处非事务冗余**：`-0.145 s`（pack 桶，0.3% 重复性）、墙钟 −0.15~−0.26 s、事务 0 变化、三尺寸 sha1 全中；**≤5.0 s 需要换杠杆**（convfp 1.256 s / 非卷积 2.004 s / q8_segments 0.500 s）。
