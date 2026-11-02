# REPORT_v126 —— 128² 整网 arena 缓冲区间静态计算 + 两两重叠检查

**子代理 T-126｜纯主机侧算术 + 读代码。未上板、未 ssh、未起 Vivado/xsim、未改任何 RTL/PS。**
产物：`/media/felix/Data/coding/demoir-fpga/runs/v126_arena/`
（`REPORT_v126.md` / `arena_v126.py` / `rows_128.json` / `calc_log.txt`）

---

## 0. 结论（先给判据结果）

> **128² 无重叠。** 假设「arena 里两个 op 的缓冲区在 ≥128² 时地址重叠 ⇒ 后写的覆盖前面的」**不成立**。
>
> - `128²`：跨槽（=非法覆盖）重叠 **0 对**；同槽复用 25538 对，全部是**时域串行**（前一个 op `finish` 后才归还槽）。
> - `64²`：同样 **0 对**（不是"64² 干净 / 128² 脏"）。
> - `256²`：同样 **0 对**，且槽位元数据**随尺寸线性放大**（`max_in` 4×、`max_out` 4×），**没有任何按 64² 写死的上界**。
> - 判据「op13 及之后的某个 op 的写区间覆盖 op≤12 的读区间」⇒ **逐字节算过：不成立**（见 §5.3）。
>
> ⇒ 这条假设解释不了"稳定错 / 从 op13 起 / 逐 op 放大"——不是因为它不能解释，而是因为**事实前提不存在**。

**审计模型已被板端数据独立校验**（不是自说自话）：

| 量 | 我算出来的 | 板上 T-124 实测日志 | 结论 |
|---|---|---|---|
| `128²` `max_in` | 294912 B（0.295 MB） | `max_in 0.295 MB` | ✓ 逐位相同 |
| `128²` `max_out` | 393216 B（0.393 MB） | `max_out 0.393 MB` | ✓ |
| `128²` arena | 16678912 B（16.68 MB） | `arena 16.68 MB / 24 槽` | ✓ |
| `64²` arena | 4292608 B | `probe_t124.py:86 FROZEN_ARENA64 = 4292608` | ✓ 精确命中冻结常量 |

⇒ 我算的确实是"板上那块 arena"，不是另一份代码。

---

## 1. 审计对象钉死（哪个函数、哪一行、哪个运行点）

| 项 | 值 / 证据 |
|---|---|
| 出错位流 | `runs/v53_archive/design_1_150v53.bit` md5 `e19de032…`（T-124 §0） |
| 128² 实跑脚本 | `runs/board_t124/probe_t124.py`，整网调用在 **`:606-607`**：`net.run(x0, pipeline=True, nbuf=2, arena=ar["arena"])` |
| **生效槽数 = 2** | `nbuf=2` 传入；`BIN_LAZY_DEQ=0` ⇒ `bin_net.py:2988 if self._lazy_deq:` 为假 ⇒ `BIN_LAZY_SLOTS=24` **不生效**（`:2995-2998` 只在 lazy 时才抬高 nbuf） |
| arena 尺寸公式 | `probe_t124.py:500-501` = `(max_in + max_out + 4096) × max(2,slots) + 64K`；`board_v39_verify.py:281` 同式 |
| 槽的切分 | `bin_net.py:803-809`（`_PlBackend.__init__`）：先 `nbuf` 个 IN（`region(nin, align=64)`），再 `nbuf` 个 OUT（`region(nout+4096, align=64)`） |
| bump 算术 | `bin_engine.py:428-436`（`BinArena.region`）：`off = align_up(_next, align)`，`_next = off + n` |
| 每 op 的读写量公式 | `bin_engine.py:291-298`：`act_bytes = ceil(cin/64)·H·W·8`（二值打包字），`out_bytes = ceil(cout/8)·Hout·Wout·8`（C8 int8） |
| 槽的取/还 | 取：`bin_net.py:898`（`prepare` 里 `_acquire_slot()`）→ `:817-831`（FIFO `pop(0)`，`_free` 初值 `range(nbuf)` 见 `:800`）；还：`bin_net.py:1102`（`finish` 里非 lazy 时 `_release_slot`） |
| 越界护栏（会抛异常的） | `bin_engine.py:1518-1527`（`need_in > len(in_buf)` / `need_out > len(out_buf)-4096` ⇒ `ValueError`）、`:1530-1532`（物理地址白名单）、`:203-219`（金丝雀） |

---

## 2. 128² 的槽布局（绝对偏移，相对 arena 基址；板上基址 `0x30100000`）

```
max_in  = 294912 B = 0x48000      max_out = 393216 B = 0x60000
OUT 槽 = max_out + 4096 = 397312 B = 0x61000
arena  = 16678912 B （按 24 槽声明），实跑只切 2 槽 ⇒ 实占 1384448 B（8.3%）
```

| 槽 | 区间（相对基址） | 长度 | 4K 对齐 |
|---|---|---|---|
| IN0 | `[0x00000, 0x48000)` | 294912 | ✓ |
| IN1 | `[0x48000, 0x90000)` | 294912 | ✓ |
| OUT0 | `[0x90000, 0xF1000)` | 397312 | ✓ |
| OUT1 | `[0xF1000, 0x152000)` | 397312 | ✓ |

**相邻即"紧贴、零重叠"**：`IN0.end == IN1.start == 0x48000`，`OUT0.end == OUT1.start == 0xF1000`。
⇒ 整块 arena 是一个**单指针 bump 分配器**，槽与槽之间按定义不可能相交（`bin_engine.py:431-434` 单调递增 + 上界检查）。

---

## 3. 128² 前 25 个 conv op 表（`offset/size` 为两段区间：IN 段 + OUT 段）

`id` 是全局 op id（395 个 op 中 227 个是 conv，所以 id 不连续）；`slot` = 该 op 实际用的槽。

| op_id | 名字（尾 46 字符） | slot | in_off | in_size(槽) | in_used | out_off | out_size(槽) | out_used=out_bytes |
|---:|---|---:|---|---:|---:|---|---:|---:|
| 2 | `encoder.encoder_1.rdb.conv_layers.0.conv` | 0 | 0x0 | 294912 | 32768 | 0x90000 | 397312 | 262144 |
| 4 | `encoder.encoder_1.rdb.conv_layers.1.conv` | 1 | 0x48000 | 294912 | 65536 | 0xF1000 | 397312 | 262144 |
| 6 | `encoder.encoder_1.rdb.conv_layers.2.conv` | 0 | 0x0 | 294912 | 98304 | 0x90000 | 397312 | 262144 |
| 8 | `encoder.encoder_1.rdb.conv_post.conv` | 1 | 0x48000 | 294912 | 131072 | 0xF1000 | 397312 | 196608 |
| 12 | `m_blocks.0.b0.ghost_layers.0.primary_conv.conv` | 0 | 0x0 | 294912 | 32768 | 0x90000 | 397312 | 131072 |
| 13 | `locks.0.b0.ghost_layers.0.cheap_operation.conv` | 1 | 0x48000 | 294912 | 32768 | 0xF1000 | 397312 | 131072 |
| 15 | `m_blocks.0.b0.ghost_layers.1.primary_conv.conv` | 0 | 0x0 | 294912 | 65536 | 0x90000 | 397312 | 131072 |
| 16 | `locks.0.b0.ghost_layers.1.cheap_operation.conv` | 1 | 0x48000 | 294912 | 32768 | 0xF1000 | 397312 | 131072 |
| 18 | `m_blocks.0.b0.ghost_layers.2.primary_conv.conv` | 0 | 0x0 | 294912 | 98304 | 0x90000 | 397312 | 131072 |
| 19 | `locks.0.b0.ghost_layers.2.cheap_operation.conv` | 1 | 0x48000 | 294912 | 32768 | 0xF1000 | 397312 | 131072 |
| 21 | `m_blocks.0.b0.ghost_layers.3.primary_conv.conv` | 0 | 0x0 | 294912 | 131072 | 0x90000 | 397312 | 131072 |
| 22 | `locks.0.b0.ghost_layers.3.cheap_operation.conv` | 1 | 0x48000 | 294912 | 32768 | 0xF1000 | 397312 | 131072 |
| 24 | `m_blocks.0.b0.ghost_layers.4.primary_conv.conv` | 0 | 0x0 | 294912 | 163840 | 0x90000 | 397312 | 131072 |
| 25 | `locks.0.b0.ghost_layers.4.cheap_operation.conv` | 1 | 0x48000 | 294912 | 32768 | 0xF1000 | 397312 | 131072 |
| 27 | `encoder.encoder_1.sam_blocks.0.b0.conv.conv` | 0 | 0x0 | 294912 | 196608 | 0x90000 | 397312 | 196608 |
| 28 | `m_blocks.0.b2.ghost_layers.0.primary_conv.conv` | 1 | 0x48000 | 294912 | 8192 | 0xF1000 | 397312 | 32768 |
| 29 | `locks.0.b2.ghost_layers.0.cheap_operation.conv` | 0 | 0x0 | 294912 | 8192 | 0x90000 | 397312 | 32768 |
| 31 | `m_blocks.0.b2.ghost_layers.1.primary_conv.conv` | 1 | 0x48000 | 294912 | 16384 | 0xF1000 | 397312 | 32768 |
| 32 | `locks.0.b2.ghost_layers.1.cheap_operation.conv` | 0 | 0x0 | 294912 | 8192 | 0x90000 | 397312 | 32768 |
| 34 | `m_blocks.0.b2.ghost_layers.2.primary_conv.conv` | 1 | 0x48000 | 294912 | 24576 | 0xF1000 | 397312 | 32768 |
| 35 | `locks.0.b2.ghost_layers.2.cheap_operation.conv` | 0 | 0x0 | 294912 | 8192 | 0x90000 | 397312 | 32768 |
| 37 | `m_blocks.0.b2.ghost_layers.3.primary_conv.conv` | 1 | 0x48000 | 294912 | 32768 | 0xF1000 | 397312 | 32768 |
| 38 | `locks.0.b2.ghost_layers.3.cheap_operation.conv` | 0 | 0x0 | 294912 | 8192 | 0x90000 | 397312 | 32768 |
| 40 | `m_blocks.0.b2.ghost_layers.4.primary_conv.conv` | 1 | 0x48000 | 294912 | 40960 | 0xF1000 | 397312 | 32768 |
| 41 | `locks.0.b2.ghost_layers.4.cheap_operation.conv` | 0 | 0x0 | 294912 | 8192 | 0x90000 | 397312 | 32768 |

（全 227 条见 `rows_128.json`；`in_used = act_bytes`，`out_used = out_bytes`。）

**注意两件事**：
1. 槽只有 **0 / 1 两个**——24 槽的 `BIN_LAZY_SLOTS=24` 在 `BIN_LAZY_DEQ=0` 下**没生效**（§1 第 2 条）。16.68 MB 里只有 1.38 MB 被切过，**其余 15.3 MB 一次都没被写过**（256² 更夸张：66.22 MB 里只用 5.51 MB）。
2. 一个 op 的 `in_used` 可以远小于槽（32768 vs 294912），**"小 op 用满槽"这件事根本不存在**——这是后面金丝雀盲区的根。

---

## 4. 重叠检查（三个尺寸，两种口径）

### 4.1 口径 A：槽级（物理区间是否相交）

`32 个槽区间`（2 IN + 2 OUT；nbuf=24 时 48 个）两两求交：**64² / 128² / 256² 全部 0 对**。
这不用算也知道——bump 分配器 `_next` 单调，`off ≥ _next`（`bin_engine.py:431`），所以**结构上不可能相交**；脚本仍逐对算了一遍以防我读错对齐。

### 4.2 口径 B：op 级（真实读写跨度，不是整个槽）

把每个 op 的 `[in_off, in_off+act_bytes)` 与 `[out_off, out_off+out_bytes)` 当区间，`227×226/2` 对逐对求交：

| 尺寸 | 相交对数 | 其中【跨槽】= 非法覆盖 | 其中【同槽】= 时域复用 |
|---|---:|---:|---:|
| 64² | 25538 | **0** | 25538 |
| **128²** | 25538 | **0** | 25538 |
| 256² | 25538 | **0** | 25538 |

**"同槽复用"为什么合法（举证）**：槽的取/还是严格串行的——

* `prepare(i)` 里取槽（`bin_net.py:898`），`finish(i)` 里还槽（`bin_net.py:1102`，`BIN_LAZY_DEQ=0` ⇒ 不走 `:1093-1099` 的钉住分支）。
* 主循环 `bin_net.py:3118-3139`：同一时刻只有 1 个 PL 作业在飞（`pl_task`），且**只有 `poll()` 为真（即上一 op 已 done）才 kick(下一 op)**（`:3106-3116`）。⇒ 归还必然早于复用。
* 实际序列就是 `0,1,0,1,…`（FIFO `pop(0)`，`:821`）。同槽对都是"隔一个 op"，例如
  `op2(op2)↔op6  in 32768 B / out 262144 B`、`op8↔op13 in 32768 / out 131072`、`op12↔op15 in 32768 / out 131072` ——
  这些字节数**不是覆盖，是同一个槽在两代之间被重新租用**。要判"非法"，必须证明**两个 op 同时在飞**；这里不可能。

⇒ **"合法共享"与"非法覆盖"的分界点**：本运行的 arena 里**没有任何一块被两个 op 同时持有**。所以"覆盖"这条物理机理在 PS 侧不存在。

### 4.3 判据逐条核算（这就是任务书 §3 的"判据"）

**op13**（`...ghost_layers.0.cheap_operation.conv`，slot 1）：
`IN [0x48000, 0x50000)`、`OUT [0xF1000, 0x111000)`（131072 B）。
**op ≤ 12** 的全部读写跨度（逐个算到末尾，不用上限代替）：
slot 0：`IN` 最大 `[0x0, 0x18000)`（op6，98304 B）、`OUT` 最大 `[0x90000, 0xD0000)`（op2/op6，262144 B）；
slot 1：`IN` 最大 `[0x48000, 0x68000)`（op8，131072 B）、`OUT` 最大 `[0xF1000, 0x131000)`（op4，262144 B）。

* `[0xF1000, 0x111000)` 与 slot 0 的 `[0x90000, 0xD0000)` **不相邻**：中间空档 `0xD0000→0xF1000` = **135168 B**
* 与 slot 1 内 op4 的 `[0xF1000, 0x131000)` 有 **131072 B** 相交 ⇒ **同槽、op4 早已 finish、槽已归还**（§4.2 举证）
* 与 op8（同 slot 1）的 `IN [0x48000,0x68000)` 有 32768 B 相交 ⇒ **op8 早已 finish、槽已归还**（§4.2 举证）

⇒ 判据「op13 以后某 op 的写覆盖 op≤12 的读」**不成立**。op15/16/18 同理（全表逐对算过，`rows_128.json`）。

---

## 5. 任务书 §2 五个怀疑点，逐条给答案

### ① 单槽容量是怎么算的？按最大 op 估，还是写死了上界？
**按实际最大 op 算，没有写死上界。**
`bin_net.py:3011-3012`：`max_in = max(o.layer.act_bytes)`、`max_out = max(o.layer.out_bytes)`（遍历全部 227 个 conv）；
`:3019` 用它算 arena 需求，`:3029` 传给 `_PlBackend`，`:805/:808` 每个槽都切**这个尺寸**。
三尺寸实测（算术结果，非估计）：

| | max_in | max_out | arena(24) | 实切 2 槽占 |
|---|---:|---:|---:|---:|
| 64² | 73728 | 98304 | 4292608 | 352256 |
| 128² | 294912 | 393216 | 16678912 | 1384448 |
| 256² | 1179648 | 1572864 | 66224128 | 5513216 |

**每档都是精确 4×**（`act_bytes ∝ H·W`、`out_bytes ∝ Hout·Wout`）⇒ 不存在"按 64² 估的上界"。
唯一的固定常数是 `+4096`（金丝雀尾）和 `+65536`（arena 尾巴），它们**不随尺寸变化，但也从不参与裁定逻辑**。

### ② 槽内 in/out 的分界有没有固定常数？
**没有。** IN 与 OUT 根本不在一个"槽"里：`_PlBackend.__init__`（`bin_net.py:803-809`）先把 `nbuf` 个 IN 连续切完，**再**切 `nbuf` 个 OUT。所以
`OUT_i 起点 = nbuf·align64(max_in) + i·align64(max_out+4096)`，纯由 `max_in/max_out` 决定：

| | OUT0 相对偏移 | OUT1 相对偏移 | OUT 槽步长 |
|---|---:|---:|---:|
| 64² | 0x24000 (147456=2×73728) | 0x3D000 (249856) | 102400 |
| 128² | 0x90000 (589824=2×294912) | 0xF1000 (987136) | 397312 |
| 256² | 0x240000 (2359296=2×1179648) | 0x3C1000 (3936256) | 1576960 |

⇒ 没有 64K/128K 这类常数；offset 完全由尺寸推出来，且**随尺寸同步放大**（这就是"单层/小尺寸全对"时不变的那个量在 128² 也没变坏的原因）。

### ③ nbuf=2 的步长按什么算？
IN 步长 = `align64(max_in)`，OUT 步长 = `align64(max_out+4096)`（`bin_net.py:805/808` + `bin_engine.py:431`）。**不写死**，见 §②表。
运行点里 nbuf 实际是 **2**（`probe_t124.py:607`），`BIN_LAZY_SLOTS=24` 在 `BIN_LAZY_DEQ=0` 下不生效（`bin_net.py:2988/2995`）。
我把 nbuf=24 也跑了一遍：仍是 **0 对跨槽重叠**（只是槽更多）。

### ④ cat 的虚拟拼接有没有被算进重叠检查？
**这个运行点里 arena 里没有虚拟张量，所以不涉及。**
`BIN_CAT_FUSION=1` 要生效需要 `_lazy_deq` 或 `CATPACK`（`bin_net.py:2948-2955`、`:2925-2930`）；本运行点 `BIN_LAZY_DEQ=0` 且未设 `CATPACK` ⇒ cat 走 `_exec_ps`（`bin_net.py:2404/2413`），输出是**宿主 numpy**，不落 arena；
它的输出随后被 `pack_input`（`bin_engine.py:2106`）打包、`push_input`（`:1614-1618`）拷进 IN 槽。所以"合法共享"这一情形在本运行点**不出现**，也就不存在被检查漏掉的合法重叠。

### ⑤ 对齐（64 B / 4 KB）
* **offset 全部 64 B 对齐**：227/227 个 op 的 `in_off/out_off` 都是 64 的倍数（脚本逐条核过，`align=64` 在 `bin_net.py:805/808`）。
* **本尺寸下 4 个槽基址恰好都 4 KB 对齐**（128²：`max_in=294912=72×4096`、`max_out+4096=397312=97×4096`；64²/256² 同样整除）。**但这是巧合，不是保证**——代码只保证 64 B。⇒ 若将来出现 64B 对齐而 4K 不对齐的槽基址，**IN 段的 AXI 4 KB 跨界数会变成"跨槽"**，那时才需要在意。本尺寸不用。
  *（真要说 4 KB：128² 下 **206/227** 个 op 的 OUT 跨度跨 4 KB 边界，但它们是在**槽内部**跨，与"地址分配"无关；且同样几何的单层测试全对 ⇒ 不是 size 差异的解释项。）*

---

## 6. 这条假设为什么"能解释四条观察"却仍然不是根因

| 观察（T-124/T-125） | 重叠假设能否解释 | 事实 |
|---|---|---|
| ① 稳定错（不是抖动） | 能 | 但 PS 侧没有"两个 op 同时持有同一区间"，所以没有第二个写入者 |
| ② 从 op13 起 | 能（"前面还没撞上"） | 而 op13 的写区间 `[0xF1000,0x111000)` 与 op≤12 的任何跨度都不相交（§4.3） |
| ③ 逐 op 放大（9955→55431→56034→82408 B） | 能（传播） | 但"放大"同时意味着错的 op 越来越多，而每一代都换槽 ⇒ 与"固定槽位碰撞"矛盾 |
| ④ 错的是输出缓冲的**连续尾巴**、坏块在参照里找不到 | 能 | 与本假设无关——这是"没写进去/写偏了"，而不是"被别的东西盖了"（被盖会留下**另一个 op 的合法数据**，就能在参照里找到） |

⇒ ④ 是最硬的一条反向证据：**被覆盖的字节应该等于"覆盖者"的合法输出**，在参照里必然能找到；T-124 明确报告"错块在参照里找不到"。

---

## 7. 其它可能（一句话，按要求不展开）

> 既然 PS arena 被静态排除，剩下能"跨 op 累积"的量只有 **PL 内部跨 op 的收笔/状态**（done 与 W 通道排空、逐 op 递增的地址/拍计数）或 **PS 侧跨层复用的计划缓存**（`_ARM_PLANS` / `_PVIEWS_SHARED` / `_SEG_PLANS`，`bin_engine.py:1693/3339/3648`（定义在 `:5334-5336`）），不是 PS arena 的地址重叠。

---

## 8. 修法

**没有需要修的代码** —— 因为不存在非法重叠，改任何 offset 公式都是无的放矢（改了反而会把现在的"紧贴、零重叠"破坏掉）。

如果要把这条假设**彻底钉死**、或把同类事故以后变成"必然被抓住"，我给三条**只在方案层面**的加固（**未改任何代码**）：

1. **把 OUT 金丝雀挪到 `out_bytes` 紧后面，并给 IN 槽也加一条**
   * 现状：`BinGuard.fill_canary`（`bin_engine.py:203-205`）只涂**槽最后 4096 B**；而 `guard_layer` 只保证 `out_bytes ≤ len(out_buf)-4096`（`:1523`）。
   * 后果：小 op（128² 下 32768 B 的 op 对 397312 B 的槽）**最多可越界 ~360 KB 而不被任何护栏发现**。⇒ 现有金丝雀**无法**为"覆盖假设"提供否定证据，只能为"最大 op 越界"提供。
   * 改法：`fill_canary(buf, n_used)` / `check_canary` 改为从 `n_used` 处涂 4096 B；`guard_layer:1542` 传 `need_out`；`push_input`（`:1614`）同样给 IN 加尾金丝雀。**纯诊断，不改数值路径。**
2. **给 IN 槽留 4096 尾巴**（`bin_net.py:805` 改成 `region(nin+4096)`，与 OUT 的 `:808` 对称）。现状 IN 槽紧贴下一个 IN 槽，IN 侧越界在物理上直接落进邻居的 IN 区且无护栏。
3. **arena 尺寸按实际槽数算**（`bin_net.py:3019` / `probe_t124.py:501`）：`BIN_LAZY_DEQ=0` 时实际只用 2 槽，却按 24 槽分配 ⇒ 128² 浪费 15.3 MB、256² 浪费 60.7 MB CMA（T-124 已记 `CmaFree 90 MB`）。建议 `need` 用**生效的 nbuf**，或把 `slots` 的声明与 nbuf 绑定，避免"声明的槽数"与"实切的槽数"脱钩这种隐性不一致。

---

## 9. 产物

| 文件 | 内容 |
|---|---|
| `arena_v126.py` | 全部计算脚本（只 import 纯 Python 规划部分 + `FakeArena`，**不 import pynq**） |
| `rows_128.json` | 128² 全 227 个 conv op 的 `(op_id,name,slot,cin,cout,k,H,W,Ho,Wo,in_off,in_size,out_off,out_size,act,outb)` |
| `calc_log.txt` | 64²/128²/256² × nbuf=2/24 的完整布局 + 重叠统计原始输出 |
| `REPORT_v126.md` | 本报告 |

### 引用行号核对表（每条都在本次会话里 `sed -n Np` 打出来确认过内容）

| 引用 | 内容 |
|---|---|
| `bin_net.py:800` | `self._free = list(range(self.nbuf))` |
| `bin_net.py:805` / `:808` | `arena.region(nin, align=64, ...)` / `arena.region(nout + 4096, align=64, ...)` |
| `bin_net.py:821` | `return self._free.pop(0)` |
| `bin_net.py:898` / `:1102` | `slot = self._acquire_slot()` / `self._release_slot(task.slot)` |
| `bin_net.py:2988` / `:2995` | `if self._lazy_deq:` / `if _want > nbuf:`（BIN_LAZY_SLOTS 只在这条支路生效） |
| `bin_net.py:3011` / `:3012` / `:3019` / `:3029` | max_in / max_out / need / `_PlBackend(...)` |
| `bin_engine.py:293` / `:298` | `act_bytes` / `out_bytes` 公式 |
| `bin_engine.py:431` | `off = (self._next + align - 1) // align * align` |
| `bin_engine.py:1519` / `:1523` | IN 尺寸护栏 / `avail = len(out_buf) - CANARY_BYTES` |
| `bin_engine.py:203-219` | `fill_canary` / `check_canary` |
| `bin_engine.py:3320` | `need_in = max(need_in, pm[0])` |
| `bin_engine.py:3400` / `:3410` | `_pack_views()`（E1E2 打包小 arena，**不是** net arena）/ 打包缓冲尺寸自检 |
| `probe_t124.py:501` / `:607` | arena 尺寸公式 / `net.run(..., nbuf=2, arena=...)` |
| `probe_t124.py:86` | `FROZEN_ARENA64 = 4292608`（= 我算的 64²，独立校验） |
| `board_v39_verify.py:281` | `need = (max_in + max_out + 4096) * max(2, slots) + (1 << 16)` |

### 附：E1E2 交付程序的静态 arena（顺带核过，与本 bug 无关）
`deploy/e1e2_desc/frame.json`：`hw=128`、arena `61,148,224 B`、**683 个 buffer 逐对求交 0 重叠**（`desc`/`thr`/227×`int8`/227×`packed`/`thr_const`），最大 buffer 末尾**正好等于** arena 末尾。
⇒ 连"全量程序模式"那张静态表也是零重叠。它的打包小 arena（`_pack_views`，`bin_engine.py:3297-3369`）in/out/thr/desc 四块也是顺序切、**互不相交**（`:3328` `need = al(in)+al(out)+al(thr)+al(desc)`，`:3356-3361` 偏移按 `al(n)` 累加）。
