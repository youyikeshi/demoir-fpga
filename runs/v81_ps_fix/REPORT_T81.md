# T-81 交付报告：PS 侧三条红缺陷（D1/D2/D3）修复

> 基座：`git HEAD = 515cf29`（v50 PS 已合并进 main）。
> 只改了两个文件：`pynq_specialized_code/ps/pynq/bin_engine.py`、
> `pynq_specialized_code/ps/pynq/bin_net.py`（`qcache.py` 未动）。
> 未碰 RTL / BD / tcl / `ps/tools/*`；未上板、未起 Vivado。全部证据在本目录。

---

## 0. 结论（一句话三条）

| 缺陷 | 结论 | 关键数字 |
|---|---|---|
| **D1** 寄存器段错 / 缺 `CHAIN_STATUS` | ✅ 全部搬到 `0x300..0x314`；`0x310` 解析 + `0x314` magic 门；**`0x200` 段一个字节不动**；FUSE=1 与 E1E2 **不再互斥** | 提交写序 `0x308→0x30C→0x300`；链跑期间 `0x200/0x204/0x208/0x20C` 写 0 次；err_code=5 解析正确 |
| **D2** 45 条幽灵带 | ✅ `bands = ceil(h_out/band_rows)`（halo 不进分子）；`band_y0 < h_out` **全部满足** | `--hw 64`：**432 → 387 条**；几何违规 **999 → 0**；`--hw 256`：1184 条、`max(y0)=248 < 256`、0 违规 |
| **D3** `out_pa` 双重偏移 + 8 B/px 写死 | ✅ 只保留描述符 `band_y0` 一种偏移；字节/像素 = **VLANES 参数**（不再字面 8） | S1 四带 `w1` 恒 `0x50004000`（旧公式 `0x50004000/4800/5000/5800`） |

---

## 1. D1 —— v50 寄存器搬到 `0x300` 段 + `CHAIN_STATUS` + magic 门

### 改前 / 改后（行号 = 当前文件行号）

| 项 | 改前（`515cf29`） | 改后（本次） |
|---|---|---|
| 寄存器表 | `bin_engine.py:2271-2272`：`V50_CHAIN_REG = dict(CHAIN_HEAD=0x200, PROGRESS=0x204, BAND_ROWS=0x208, FUSE_MODE=0x20C)` | `bin_engine.py:2323-2325`：`CHAIN_HEAD=0x300 / PROGRESS=0x304 / BAND_ROWS=0x308 / FUSE_MODE=0x30C / CHAIN_STATUS=0x310 / CHAIN_ID=0x314` |
| magic | 无 | `bin_engine.py:2327` `V50_CHAIN_MAGIC = 0x7635_5000`；读取点 `v50_chain_magic()` `:1820`、判据 `v50_chain_ready()` `:1839-1867` |
| `CHAIN_STATUS` | 无 | `v50_chain_status()` `:1830-1837`（bit0 done/bit1 err/bit2 busy/bit8~15 code）；轮询点 `v50_poll_chain()` `:1944` |
| 轮询来源 | `AP_RET` 的 bit8 当 err_code（`v50_poll_chain` 旧 `1940`） | `0x310` 为准，`AP_RET` 只作交叉校验；err_code 来自 `0x310[15:8]` |
| 互斥护栏 ① | `enable_e1e2` 旧 `2149-2153`（FUSE=1 直接 return None） | **删除**（`bin_engine.py:2193-2208`，只剩 probe） |
| 互斥护栏 ② | `E1E2Engine.select` 旧 `2763-2769` 抛 RuntimeError | **删除**（`:2815-2822`） |
| 互斥护栏 ③ | `submit_chain` finally 旧 `3070-3073` 注释"不能 select(0)" | 改为只关 `FUSE_MODE(0x30C)`、不碰 0x200（`:3120-3128`） |
| `BIN_V50_CHAIN_RTL` | 链的就位判据（人工开关，能被寄存器回声骗过 → D9） | **退役**（`v50_chain_rtl_on()` `:2345-2352` 只留兼容 no-op），判据换成 magic |

### 运行时证据（`P3_probe_regs.log`，16 项全 PASS）

```
V50_CHAIN_REG = {CHAIN_HEAD:0x300, PROGRESS:0x304, BAND_ROWS:0x308,
                 FUSE_MODE:0x30C, CHAIN_STATUS:0x310, CHAIN_ID:0x314}
PASS 全部落在 0x300..0x314        PASS 与 E1E2 的 0x200 段无交集
PASS magic 期望值 = 0x76355000
PASS magic=0 ⇒ 不走链  [CHAIN_ID(magic @0x10314) 期望 0x76355000 读回 0x00000000 ⇒ …没有链引擎…]
PASS magic=0x76355000 ⇒ 认链
PASS FUSE=1 时 enable_e1e2() 不再拒绝
PASS FUSE=1 时 select(4) 可写 R_RUN_UNIT  [0x200=4]
PASS 提交写序 = BAND_ROWS(0x308) → FUSE_MODE(0x30C) → CHAIN_HEAD(0x300)  [['0x308','0x30c','0x300']]
PASS 提交期间没碰 0x200 段  [同上]
PASS CHAIN_STATUS=0x1(done) + PROGRESS=4 ⇒ 完成
PASS err 位 ⇒ 抛 BinRunError(code=5)  [code=5]
```

端到端对照（`D2_before_fuse1_hw64.log` → `P2_after_fuse1_hw64.log`，都 `BIN_V50_FUSE=1`）：

```
改前：⚠ [V50] BIN_V50_FUSE=1：0x200 是 CHAIN_HEAD（契约 A）… ⇒ 不挂 E1E2（回落 PS 打包）
      B E1E2 + LAZY_DEQ=1   prologue 0.00  epilogue 0.00
改后：（无该告警）
      B E1E2 + LAZY_DEQ=1   prologue 0.05  epilogue 0.01
      C E1E2 + LAZY_DEQ=0   prologue 0.21  epilogue 0.01
```

⇒ **FUSE=1 时 E1E2 会真的挂上并且真的在打包**（`prologue` 不再是 0）。
附带修掉一处**记账**缺陷：v50 stats 原来把 `ps_prologue/ps_epilogue` **写死 0.0**
（`bin_net.py` 旧 `2682-2683`），所以即使挂上 E1E2 也打印 0.00 —— REVIEW 的
"prologue 0.00 为证"里有一半是这一行造成的。现在从 `task.pro_s` / `finish()` 的
epilogue 时间**实测累计**（`_v50_exec_one` `:2540-2562`、stats `:2689-2696`）。

### magic 寄存器**期望值与偏移**（给 RTL 侧）

| 项 | 值 |
|---|---|
| 寄存器 | `CHAIN_ID`，**偏移 `0x314`**（相对配置页 `+0x10000` ⇒ 绝对 = `base_pa + 0x1_0314`，代码里就是 `CFG + 0x314`） |
| 期望值 | **`0x7635_5000`**（只读，RTL 里硬写死；`0x7635` = "v50"，`0x5000` = v50 段） |
| 语义 | 读回 ≠ 期望 ⇒ **PS 判定"这条位流没有链引擎"，整跑回落 v5 逐 op**（不报错、不改数字） |
| 期望值覆盖 | `BIN_V50_CHAIN_MAGIC=<int>` 只改**期望值**（RTL 若定别的常量，双方对齐后用它）；**没有任何"跳过读取"的开关** —— 这是机制，不是人工开关 |
| 配套（已实现） | ① `0x308 BAND_ROWS` 写读回作为第二道自检；② `PROGRESS` 必须**单调不减**（倒退 ⇒ 抛 `BinRunError(0xE0)`）；③ `CHAIN_STATUS.err=1` 立即报错并 poison，不再 kick |
| RTL 必须做的 | `0x310` 按 bit0 done/bit1 err/bit2 busy/bit8~15 err_code 实现；`0x314` 返回常量 `0x76355000`；`0x308` 可读回 |

⚠ 给仿真代理：现有 xsim/FakePL 若仍把 v50 寄存器建在 `0x200`，**FUSE=1 的链会被
magic 门直接拒**（这正是设计意图）。要让链跑起来，模型里必须补 `0x300` 段 +
`0x314=0x76355000`。

---

## 2. D2 —— `bands` 把 halo 加进分子（45 条幽灵带）

### 根因与改法

| | 内容 |
|---|---|
| 改前 | `bin_net.py:1504`：`bands = max(1, (h_out + halo + band_rows - 1) // band_rows)` |
| 改后 | `bin_net.py:1614`：`bands = len(v50_band_geoms(h_out, band_rows))`，其中 `v50_band_geoms()`（`:1182-1203`）就是 `ceil(h_out / band_rows)`，**halo 不参与分子** |
| 唯一构造点 | 离线 `build_chain`（旧 `1314-1315`）、`materialize`（旧 `1355-1357`）、运行时 `_v50_exec_segment`（旧 `2479-2487`）三份重复实现 ⇒ 全部改调 `v50_seg_descs()`（`bin_net.py:1206-1250`）。运行时段原来**连 `min()` 都没有**，这是它同时逃过结构判据和自查的原因 |
| `cfg_h` (w3) | 旧 `band_rows + halo_rows`（含 halo，违反 R2 §11.5）⇒ 现 = **本带有效行数 `nband`** |
| `w9 nband` | 旧恒 `1` ⇒ 现 = **本带有效行数**（R2 §11.1；末带可 < BAND_ROWS） |
| 几何断言 | 新增 `check_chain_geometry()`（`bin_net.py:1441-1492`）：`y0 < h_out`、`y0+nband <= h_out`、带输出不越张量；`build_chain` 末尾直接 `raise`，`report()/dump_chain()` 打印判据结果（D14 一并收口） |

### 数值证据（`--hw 64`，`BIN_V50_FUSE=1 BIN_V50_DUMP=1`）

```
改前 D2_before_fuse1_hw64.log：== v50 段计划（P1）: 223 段 / 432 条描述符 ==
  -- S1 conv/cat  bands=5 halo=2 链段 [#4..#8)
     [#004] 50000000 5000c000 00000030 0000000a 00000020 00000000 00000000 00000002 50805000 00030001
     [#005] 50000000 5000c000 00000030 0000000a 00000020 00000000 00000008 00000002 50806000 00030001
     [#006] 50000000 5000c000 00000030 0000000a 00000020 00000000 00000010 00000002 50807000 00030001
     [#007] 50000000 5000c000 00000030 0000000a 00000020 00000000 00000018 00000002 50808000 00030001
     [#008] 50000000 5000c000 00000030 0000000a 00000020 00000000 00000020 00000003 50809000 00030001
                                          w3=0x0a(=band_rows+halo)          w6=0x20 ← 幽灵带 y0=32=h_out

改后 P2_after_fuse1_hw64.log：== v50 段计划（P1）: 223 段 / 387 条描述符 ==
  -- S1 conv/cat  bands=4 halo=2 链段 [#4..#7)
     [#004] 50000000 5000c000 00000030 00000008 00000020 00000000 00000000 00000002 50805000 00030008
     [#005] 50000000 5000c000 00000030 00000008 00000020 00000000 00000008 00000002 50806000 00030008
     [#006] 50000000 5000c000 00000030 00000008 00000020 00000000 00000010 00000002 50807000 00030008
     [#007] 50000000 5000c000 00000030 00000008 00000020 00000000 00000018 00000003 50808000 00030008
         w3=0x08(=nband, 不含 halo)  w6=0..0x18(<h_out)  w9 低16=8（nband）；末带 flags bit0=1
  带几何自检：387 条描述符 / **0 条违规**
```

`432 − 387 = 45` ⇒ **与 REVIEW 的 45 段受影响逐数吻合**。

独立复核（不调 `bin_net` 的自检，`check_chain_geometry.py` 直接解析 dump）：

```
改前：段数 223 / 描述符 432 条 / 违规 999 条 （S1: 描述符 5 条 != ceil(32/8)=4；S0[0] nband=1、cfg_h=8）
改后：段数 223 / 描述符 387 条 / 违规 0 条  结论 PASS
```

`--hw 256`（只建图 + 建计划，不跑整网）：223 段 / **1184 条** / 0 违规 /
`max(band_y0)=248 < 256`。

---

## 3. D3 —— `out_pa` 双重偏移 + `8 B/px` 写死

| | 内容 |
|---|---|
| 改前 | `bin_net.py:2476` `row_bytes = int(seg.w_out) * 8`（字面 8，且未乘通道组数）；`:2484` `out_pa=out_pa + y0*row_bytes` **且** `:2487` 又传 `band_y0=y0` ⇒ 两种偏移叠加 |
| 改后 | 两条都不留：`out_pa` 传**段输出张量基址**，带偏移只由 `w6 band_y0` 表达（`v50_seg_descs()` `:1231-1246`）；字节/像素 = `vlanes` **参数**（`pix_bytes = max(vlanes,8)`，一行 = `W × VLANES × ceil(cout/VLANES)`），运行时段从 `self._segplan.vlanes` 取（`:2598-2599`） |
| 与离线一致 | 运行时 `_v50_exec_segment` 现在**直接调** `v50_seg_descs`（旧 `2479-2487` 是第四份实现，连 `min()` 都没有）⇒ 离线证据 == 运行时提交的那份链 |

### 证据（`P3_probe_regs.log` B 段）

```
S1   h_out=32 W=32 bands=4  w1(out_pa)=['0x50004000']
     带 y0=[0, 8, 16, 24] nband=[8, 8, 8, 8] cfg_h=[8, 8, 8, 8]
     旧公式会给 out_pa=['0x50004000','0x50004800','0x50005000','0x50005800']（每带不同 ⇒ 双重偏移）
S2 / S6 同型（0x50008000 / 0x50018000 恒定）
PASS 所有融合段的所有带 w1 恒等于段基址
PASS hw=256：223 段 / 1184 条描述符 / 0 违规 / max(y0)=248
```

（`0x4800−0x4000 = 0x800 = y0(8)×W(32)×8` ⇒ 这就是被删掉的那一份偏移量。）

---

## 4. 门禁

### P1 `BIN_V50_FUSE=0`（默认）

```
$ taskset -c 6-7 python3 pynq_specialized_code/ps/tools/verify_lazy_net.py --hw 64
A 基线（纯 PS 参考）  sha1 = ('104710f69c4d', '2ac6efec2325', '8ab595fcf7cd')
B E1E2 + LAZY_DEQ=1   sha1 = ('104710f69c4d', '2ac6efec2325', '8ab595fcf7cd')  prologue 0.07 epilogue 0.05
C E1E2 + LAZY_DEQ=0   sha1 = ('104710f69c4d', '2ac6efec2325', '8ab595fcf7cd')  prologue 0.21 epilogue 0.01
   冻结 64² 基线（主机 FakePL）⇒ 一致      == 总结：PASS ==     rc=0
```

与合并前（`P1_before_hw64.log`）**归一化 stdout 差异 = 0 行**（`P1_norm_diff.txt` 为空；
归一化只抹掉时间数字，sha1 单独逐值比对）。

⚠ **运行口径**：*没有*设 `OMP_NUM_THREADS`。注意"单核 `taskset -c <one>`"会把
OpenBLAS 压成 1 线程，等价于 `OMP_NUM_THREADS=1` ⇒ 复现出 `24222554c880/…` 的
**假失败**（本次实测已复现）。这里用 `taskset -c 6-7`（2 核、BLAS 变量全不设）
⇒ 拿到冻结值。三条线程数实测：1→`2422…`，2/4/不设→`1047…`。

### P2 `BIN_V50_FUSE=1`

见 §2/§3：**387 条**（不是 432）、`band_y0 < h_out` 全部满足（含 `--hw 256`）、
运行时 `out_pa` 恒定（无双重偏移）。完整 dump：`P2_after_fuse1_hw64.log`；
独立复核：`check_chain_geometry.py` → 0 违规。

额外整跑一次 `--hw 256`（`BIN_V50_FUSE=1`，`P2_after_fuse1_hw256.log`）：
A/B/C 三件套 = `('392d1ae02761','7f68fb00d2b5','aa03751a2d1c')`，与冻结 256² 基线
**一致**，`PASS`，rc=0，prologue 实测 0.19/1.40（不再是 0.00）。⇒ v50 的回落路径
在 256² 下也不崩、数字逐字节不变。

### P3 `merge_gate.sh`

```
$ bash pynq_specialized_code/ps/tools/merge_gate.sh
① --gen rc=0 ✓   ② md5sum -c rc=0 ✓（清单 20 行 / 失败 0 行）   ③ --check rc=0 ✓（14 模块 / 20 文件）
⇒ MERGE_GATE PASS（3/3 全绿）
```

⚠ 这一步**按脚本自身的设计**用 `--gen` 更新了 `runs/board_ops/20261102/deps_closure.txt`
（只变两类：`bin_net.py` / `bin_engine.py` 的 md5，以及因文件变长而顺移的
`file:line` 引用；旧清单已自动留证
`runs/board_ops/20261102/deps_closure_prev_20261102_155125.txt`）。
**commit 前请把这一行清单改动一起带上**，否则 ②③ 会 FAIL（实测：不重生成时
`✗ bin_net.py / bin_engine.py: 失败`，日志 `P3_merge_gate_shared_nogen.log`）。
若不想动这份共享清单，可只审 `P3_merge_gate_local.log`（同一套三关，写到本目录副本）。

---

## 5. 改动清单 / 风险与遗留

改的文件（行数变化 `117+/63-`、`167+/46-`）：

* `ps/pynq/bin_engine.py` —— D1（寄存器段、`CHAIN_STATUS`、magic、撤三处互斥、`CHAIN_RTL` 退役、文案 D16）
* `ps/pynq/bin_net.py` —— D2/D3（`v50_band_geoms` / `v50_seg_descs` 唯一构造点、
  `bands` 公式、`cfg_h`/`nband` 按 R2、几何自检、v50 prologue/epilogue 实测）
* `runs/board_ops/20261102/deps_closure.txt` —— **merge_gate ①--gen 的生成物**（见上）
* `runs/v81_ps_fix/{probe_v50_regs.py, check_chain_geometry.py, REPORT_T81.md, *.log}` —— 本目录产物

风险 / 遗留（**没有**在这次改，写清以免下一个人误判）：

1. **`in_pa`（w0）没改**：仍是段输入张量基址，不是"本带 band 缓冲基址"。
   §11.2 说 band 缓冲装 `[y0−HALO, y0+nband+HALO)` 且 E 口按行号填充 ⇒ w0 的
   band 缓冲语义要不要带 `y0` 偏移**仍需契约裁定**（D3 只裁了 `out_pa`）。
2. **`w3 cfg_h` / `w9 nband` 语义变了**（`band_rows+halo`→`nband`、`1`→`nband`）：
   这是按 R2 §11.5/§11.1 改的，**IMP-B 的外壳必须照此实现**（若它现在把
   `cfg_h` 当"含 halo 的行数"，链会读错行数）。`nband` 现在带真实有效行数，
   未再恒 1。
3. **链的数值等价没有在主机上跑过**：主机 FakePL 没有 `0x300` 段模型 ⇒ magic 门
   必然拒绝（这是正确行为）。链的正确性仍需 xsim / 板端（RTL 侧 D4/D5 修完 + `0x314`
   落地之后）。
4. `flags.bit3`（输出 f32）仍从不置位（D7，🟠，未在本次范围）；`V50_DESC_STRIDE=4096`
   仍未写入契约（D8，🟠）。
5. 本次没碰 `ps/tools/*`：`verify_lazy_net.py` 的 FakePL 仍只模拟 `0x200` 段 +
   ≥0x230 一律返回计数器的行为 ⇒ 任何"想在主机上跑真链"的尝试都会在 magic 处
   回落（`P2_after_fuse1_hw64.log:1692` 那行告警即是）。
