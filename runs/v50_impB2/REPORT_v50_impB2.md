# v50 · IMP-B2（RTL 数据流续作）交付报告

> 工作目录 `pynq_specialized_code/pl/rtl_hand/_worktrees/v50_impB2/`（从 IMP-B 逐字节拷来）
> 日志/产物 `runs/v50_impB2/`
> 契约：`docs/V50_ARCHITECTURE.md` §10（R1）、**§11（R2：halo/pad/cfg_h）**
> 起点：`runs/v50_impB/REPORT_v50_impB.md`（§8 的未做项 ②④⑤ + §8.3 的 cfg_h 缺口语义）

---

## 0. 交付清单

| 文件 | module | 行数 | 变动 |
|---|---|---:|---|
| `bin_band_seq.v` | `bin_band_seq` | 261 | **新增**：带内串联相位机（RUN 里展开 CONV→PACK→SAM）|
| `bin_band_fill_v50.v` | `bin_band_fill_v50` | 151 | **新增**：E 口填充路径（R2 的 pad / 行寻址）|
| `bin_unit_axi_wrap_v50.v` | `bin_unit_axi_wrap_v50` | 3478 | 由 `tools/patch_make_v50b_wrap.py` 从 **v39 重新生成**（15 处定点替换，每处断言恰好命中一次）|
| `tools/v50b_fuse_block.vh` | — | 483 | 融合通路正文（相对 IMP-B 的 .vh：相位机 + R2 几何 + pack/sam 带内口 + 断言）|
| `tb/elab_opmask4.v` | 4 个顶层 | 99 | C3 的 op_mask 四组合 elaboration 矩阵 |
| `bin_band_buf.v` / `bin_segq.v` / 四个 `*_v50.v` / `bin_unit_axi_wrap_bd_v50.v` | — | — | **逐字节未改**（`cmp` 过，见 §5）|

md5 见 `runs/v50_impB2/md5.txt`。

---

## 1. ① 带内串联序列：RUN 相位机（`bin_band_seq`）

### 1.1 相位展开

segq 的四条路径（推进/链尾/出错/重启）**一个字节没动**；串联只发生在它的 `S_RUN` 里面：

```
segq:  IDLE → HDR → CHK → LOAD →[ RUN ]→ DRAIN → NEXT …        （segq 视角，完全不变）
                              └─ bin_band_seq ─┐
                                 IDLE → FENCE → CONV → FENCE → PACK → FENCE → SAM → FENCE → TAIL
                                        ↑每级：start 一拍 → 等 done → 等写静默 → 换带
```

| segq 相位 | 读带 | 写带 | 谁在用端口 |
|---|---|---|---|
| LOAD | — | **band0** | E 口（填充路径逐行写）|
| CONV | **band0**（含 halo 输入）| **band1**（有效输出行）| A 读 / B 写 |
| PACK | **band1** | **band0** | C 读（band）/ 读口分流到 DDR（阈值）/ B 写 |
| SAM | **band0** | **band1** | D 读 + D 写（读带≠写带）/ 读口分流到 DDR（int16 权重）|
| DRAIN | 最后一级的写带（**锁存值**）| — | E 口 |

`op_mask` 位为 0 ⇒ 该级**不启动**（`nxt()` 直接跳过），不做 done 判定、不换带；三种位为 0 时行为等价于 IMP-B（只有 conv）。

### 1.2 四条不变量（都写成了断言，不是文档约定）

- **⑫ 读带 ≠ 写带**：每一级"读一只、写另一只"⇒ 同一级内读的行范围与写回的行范围**永不重叠**，不可能读到自己刚写的半成品。`$error`：`级 %0d 的读带==写带`。
- **⑬ 换带前必须写静默**：每次带角色变化前插 `FENCE`，等 `wr_all_idle && !wr_split_busy`。这是 `bin_band_buf` 契约 ⑩ 的软件侧，硬件兜底（`arb_conflict` 粘滞 → E_CANARY）**正常永远不会触发**。
- **⑭ 每级完成才进下一级**：CONV 等 `c_done`、PACK 等 `e_done`、SAM 等 `ctrl_done`。**另加一拍 `ign_done_q`**：三个单元的 `ctrl_done` 都是电平（挂到下次 start 才清），不屏蔽这一拍就会"启动同拍就以为跑完"。
- **⑮ 跳过的级真的不启动**：`start_q` 只在 `pend == 当前级` 时放出，且 `nxt()` 只返回被使能的级。

### 1.3 顺带修掉 IMP-B 的一个**真 bug**

IMP-B 的 `bank_wr_sel = (seg_state == 4'd4)` 是 `seg_state` 的**组合函数**：

- `LOAD→RUN` 那一拍带角色立刻从 (rd0,wr0) 翻成 (rd0,wr1)，而装载的**最后一个 64-bit 字**正好在这一拍落带 ⇒ 它被写进 **band1**（输出带）；
- 同一拍 `bin_band_buf` 的 `role_bad_now = int_sel_chg && (wr_idle_q != 0)` 必为真（装载是背靠背写，`wr_idle_q` 恒 2）⇒ **`arb_conflict` 粘滞 → `band_bad` → segq 报 E_CANARY(4)**。

也就是说：IMP-B 的融合通路只要真的跑一条描述符就会以金丝雀错误停下（B3 的 tb 把 band 全 mock 了，所以没暴露）。
本版用 `FENCE` + 锁存 `drain_bank`（而不是现算）修掉；DRAIN 期间 `run` 已撤销，若"现算"还会把读带换回 band0，同样读错带。

### 1.4 再修一个 IMP-B 的集成 bug：S_CHK 拿到的是**上一条**描述符的几何

`bin_segq` 的 `f_*`（cin/cout/nband/w…）是在 **S_CHK 的时钟沿上**锁存的，而它的 `chk_ok`
（含 `band_words == 0 ⇒ E_SIZE` 和 arena 包含性）是 **S_CHK 当拍组合**判的，其中 `band_words`
由包装层用 `d_*` 算 ⇒ CHK 当拍读到的是上一条描述符的几何。**第一条描述符时全是 0 ⇒ `band_words=0`
⇒ 假 E_SIZE(5)**，链一步都走不动（IMP-B 的 B3 自测把 `band_words` 直接打进去，所以没暴露）。

本版在包装层把 `S_HDR` 的 5 个 64-bit 拍**另存一份影子**（`sh_dw[0:4]` + `sh_beat`），
几何（cin/cout/nband/w/H_in/op_mask）全部取影子 ⇒ CHK 当拍就有正确值，且 LOAD/RUN/DRAIN
与 segq 锁存值逐字段一致（有 `$error` 交叉自检 `R2-0`）。

---

## 2. ② 按契约 R2 对齐 `cfg_h` / halo / pad

### 2.1 三个量（互不重叠，与 §11.1 逐字一致）

| 量 | 来源 | 语义 | 本版处理 |
|---|---|---|---|
| `y0` | 描述符**字 6** | 本段第一**有效输出**行（绝对行号）| 直接用（`seg_band_y0`）|
| `nband` | 描述符**字 9 低 16** | 本段有效输出行数 | `cfg_h = nband` |
| `HALO` | 参数 `BAND_HALO`（按 k 自动推）| 输入侧上下各需的行数 | 只用于算行数/行号 |

### 2.2 `cfg_h` 已改成**不含 halo**

```verilog
wire [15:0] c_cfg_h = fuse_m ? seg_nband : r_h[15:0];   // 字9低16（IMP-B 用的是字3的 h）
```
断言：`if (c_cfg_h != geo_nband) $error("R2: conv 的 cfg_h 必须 = nband（不含 halo）")`。

**由此带来一个必须登记的语义搬迁**：`字 3 高 16` 不再是"带内行数(含 halo)"，而是 **`H_in`（整图高）**——因为 R2 §11.3 的 pad 判据 `yr ≥ H_in` 必须有个来源，描述符里只有这一处。`字 3 低 16` 仍是 `W`（行步长）。→ 这是 R2 的**解释**，需 PS 侧确认（见 C5）。

### 2.3 band 行数由硬件算

```verilog
wire [15:0] geo_rows = geo_conv_en ? (geo_nband + 16'(2*BAND_HALO)) : geo_nband;
wire [15:0] geo_halo = geo_conv_en ? 16'(BAND_HALO) : 16'd0;
```
- 装带字数 `geo_loadw = ceil(cin/64) × (nband+2·HALO) × W`；
- 排空字数 `geo_drnw = 面数 × nband × W`（**只搬 [y0, y0+nband)**，没有 halo 行）；
- `segq` 的 `band_words` 按相位给（S_CHK 用两者取大做 arena 检查）；
- 断言：`rows_fill != nband + 2*halo` ⇒ `$error`（fill 内），`geo_rows != nband+2·BAND_HALO` ⇒ `$error`（包装层）。

### 2.4 pad 已经挪到 E 口（`bin_band_fill_v50`）

装载不再"从 `in_pa` 连续搬 `band_words` 个字"，而是**逐行**：

```
第 cnt 个字 → (grp, r, x) = cnt / (rows_fill·W), (cnt / W) % rows_fill, cnt % W
yr = y0 + r − HALO
yr < 0 或 yr ≥ H_in  ⇒  整字写 0、**不发 DMA**   （R2 §11.3）
否则                 ⇒  DMA ← in_pa + grp·(H_in·W·8) + yr·(W·8) + x·8
```
- `conv` 侧：`cfg_pad ≡ 0`、`base_act = 0`；断言 `c_cfg_pad != 0 ⇒ $error`。**读侧零边界分支**（k∈{1,3,5} 共用同一填充器）。
- 断言：`pad 行却发起了 DMA`、`pad 字不是 0`、`rows_fill ≠ nband+2halo`、`几何为 0`。
- 时序：pad 判决与 `seg_ld_req_d` 在同一 `en` 拍打拍 ⇒ 与 DDR 未命中（`u_ce=0`）的延迟无关。
- `segq` 的 `ld_addr` 从此**不参与寻址**（只当"第几个字"的旁证）：读地址由 fill 出。

### 2.5 带内两种布局（这是本版能"零改 band_buf/segq"的关键）

band 缓冲是**扁平 RAM**，所谓"行平面步长"纯粹是寻址约定，于是同一块存储可以有两种布局：

- **输入带（band0，LOAD 写）**：`grp·(rows_fill·W) + r·W + x`，`rows_fill = nband+2·HALO` ⇒ halo 行天然落在平面内，核按 `cfg_h=nband` 读到第 `nband+k-2` 行正好是 halo 行；
- **输出带（band1，CONV 写）**：`grp·(nband·W) + r·W + x`（核自己的 `hwout8 = cfg_h·W·8` 就是这个步长）⇒ **只写 [y0,y0+nband)**；
- 之后 PACK/SAM 都按"输出布局"（步长 `nband·W`）读 → 写，**DRAIN 就是线性 `cnt*8`**，不需要任何重映射。

---

## 3. 验收

### C1 `FUSE_MODE=0` 与 v39 结构等价 —— ✅（结构 + elaboration 级）

`runs/v50_impB2/B1b_equivalence_summary.txt`：v39 2784 行 → v50b 3478 行，**新增 694 行，被替换的 v39 行 51 行 / 15 处**（IMP-B 是 44 行 / 14 处，多出来的是 E1E2 的 cfg 端口 mux）。每处都写成 `fuse_m ? 融合支 : v39 原表达式`，`fuse_m=0` 时右支与 v39 **逐根相同**；`segq.kick = start_fire && fuse_m`，不融合时四条路径一条都不走。
⚠ 与 IMP-B 同一口径：这是"结构等价 + elaboration"，**逐字节仿真回归属 V1**。

### C2 `xvlog -sv` + `xelab`（VLANES=8 / 16） —— ✅

```
xvlog --sv（12 个文件 + 依赖）            0 error / 0 warning
xelab bin_unit_axi_wrap_bd_v50 VLANES=8   Built
xelab bin_unit_axi_wrap_bd_v50 VLANES=16  Built
xelab bin_band_seq / bin_band_fill_v50 / bin_band_buf   各 Built
```
日志 `runs/v50_impB2/B4b_elaboration.log`。只用 xvlog/xelab，串行，一次一个；**没起综合/实现**。

### C3 `op_mask` 四组合 —— ✅（elaboration）

`tb/elab_opmask4.v` 用 `OPMASK_FORCE/OPMASK_TEST` 把组合固定成常数，让级序在 elaboration 期常量折叠：

| op_mask | 级序（折叠后）| xelab |
|---|---|---|
| `0x0001` | CONV | Built |
| `0x0003` | CONV→PACK | Built |
| `0x0005` | CONV→SAM | Built |
| `0x0007` | CONV→PACK→SAM | Built |

`OP_MASK_SUP` 从 `0x0001` 放到 `0x0007`（segq 的**参数**，没改它的代码）；bit3+ 仍报 `E_OPMASK`。
⚠ 边界：这证明的是"四种组合都能展开、都不缺模块/端口/驱动"；**级序时序正确性属 V1 仿真**（本任务铁规只给了 xvlog/xelab）。FSM 的静态推演见 §1.1/§1.2。

### C4 三条语义"注释 + 断言" —— ✅

`runs/v50_impB2/C4_r2_assertions.txt`（逐条列出代码位置）。断言全部在 `translate_off` 里（不综合），
`R2-1` cfg_h=nband、`R2-1b` 行数=nband+2HALO、`R2-2` cfg_pad=0、`R2-3` halo 覆盖 k、`R2-4` 带窗口 < arena_lo，
外加 fill 的 4 条与 seq 的 3 条。

### C5 还差什么 —— 见 §4。

---

## 4. 明确未完成 / 需 PS 与 V1 对齐（C5）

1. **pack→sam 的数据语义不成立**：E1E2 的输出是**二值位流**（64 通道/字），SAM 的输入是 int8。
   本版把三级按 `op_mask` **机械串联**（读上一级写下的那只带），所以 `0x0007` 时 SAM 会把二值字当 int8 读。
   ⇒ 语义合法的组合是 `conv`、`conv+pack`、`conv+sam`；`conv+pack+sam` 只是机制验证，**PS 不应下发**。
   （真要支持"pack 之后再算"必须在 band 里另开一块区域并让 E1E2 写出 int8，属新需求。）
2. **SAM 的垂直抽头（base_y2/y4=2W/4W）未支持**：它要求"行平面步长 = 带行数（含 halo）"，而 R2 把核的
   `cfg_h` 定死为 `nband`（核的平面步长 = `cfg_h·W·8`）⇒ 两者互斥。本版 `base_y2=base_y4=0`（**点态三分支**），
   要支持垂直抽头需要给核加一个独立的 `row_stride` 参数（不动 `cfg_h` 语义）。
3. **多 act 块（cin > 64）在带模式下块步长不一致**：核的取数块步长 = `cfg_h·W·8 = nband·W·8`，而输入带平面是
   `(nband+2·HALO)·W·8`。默认 `BAND_C_MAX=48` 时 `ceil(cin/64)=1`，不触发；**一旦把 C_MAX 提到 64 以上**就必须
   改成"每 64 通道跑一次核"（run-per-block）或给核加 row-stride。→ 已登记为硬约束，别静默改 C_MAX。
4. **换带栅栏的周期开销未量化**：每次换带多 2~4 个 `en` 拍（等 `wr_all_idle`）+ 每级一次 start/done 往返。
   相对一个带几百~几千拍可忽略，但 **没有实测数字**（V1）。
5. **pack 的阈值/描述符表仍在 DDR**：`base_desc`（8 字表）与 `thr_base` 靠读窗口分流（`addr < band_hi` → band）。
   ⇒ PS 必须：① 把描述符表里的 `src_base` 写成**带内相对偏移**（`k·nband·W·8`）；② 保证 `thr_base ≥ band_hi`；
   ③ `arena_lo ≥ band_hi`（断言 `R2-4` 会看着）。这套约定**未与 PS 实现对齐验证**。
6. **catpack 融合仍不支持**（IMP-B §8.6 未变）；`rprelu/interp` 的带内口未接（op_mask 没有给它们分位）。
7. **16-lane**：`wr_split_busy` 已接进 FENCE，但 16-lane 的排空字数/双平面语义**没有仿真过**（沿用 IMP-A 的 DESIGN_16LANE）。
8. **逐字节回归、时序、上板**：全部未做（本任务只到 elaboration）。原始 IMP-B 的 ②（16-lane 写侧串行化）、
   `FUSE_MODE=0 ≡ v39` 的 L1 回归仍挂在 V1。

---

## 5. 铁规遵守情况

| 铁规 | 情况 |
|---|---|
| 不起综合/实现，只 xvlog/xelab，一次一个 | ✅ 日志只有 xvlog/xelab |
| 不改核文件 `bin_conv_core.v` | ✅ 只读引用 IMP-A 的路径（未写） |
| 不改 PS / BD / tcl | ✅ 未碰 |
| 重写 band 缓冲 / segq | ✅ `bin_band_buf.v`、`bin_segq.v` 与 IMP-B **逐字节相同**（`cmp` 通过）；segq 只改了实例化时的 `OP_MASK_SUP` 参数值 |
| 端口仲裁不够就先报 | 不需要加端口：串联是**时分**的（同一时刻只有一级在跑），1R1W 够用；写口冲突由 FENCE 排除 |
| 卡住先落盘再报 | ✅ 产物/日志已落在 `runs/v50_impB2/` |

---

## 6. 契约解释裁决（供 REVIEW / IMP-C）

| # | 争议点 | 本版裁决 | 依据 |
|---|---|---|---|
| 1 | `H_in`（pad 判据）从哪来？R2 没说 | 描述符**字 3 高 16** | R2 只定义了 `y0/nband/HALO`，H_in 唯一可落处 |
| 2 | 无 conv 级时 band 行数？ | `nband`（不需要 halo 行）| `halo` 只为 conv 的 k 存在 |
| 3 | `op_mask` 位分配 | bit0 conv / bit1 pack(E1E2) / bit2 sam | R5 的"conv/pack/小算子"顺序 + IMP-B 的 bit0 |
| 4 | `op_mask=0`（一级都不跑）| 装什么排什么（`drain = load`，读 band0）| 不静默、不报错、可预期 |
| 5 | 核的平面步长 vs 带平面步长 | 输入/输出**用两套步长**（同一块扁平 RAM）| 见 §2.5；这是零改核/零改 band 的唯一解 |
