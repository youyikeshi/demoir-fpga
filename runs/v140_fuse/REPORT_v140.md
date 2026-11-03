# REPORT_v140 —— 行带融合（v50 FUSE）现状只读评估

**子代理 T-140｜只读静态审计。未改任何 RTL/PS，未上板/ssh，未起 Vivado/xsim。**
产物目录：`runs/v140_fuse/`（本文件）。

---

## 0. 方法与边界

| 做了什么 | 证据 |
|---|---|
| 复算主线 RTL 根 | `verify_rtl_root.sh runs/timing_v50/rtl_v50_manifest.txt runs/timing_v50/rtl_v50` ⇒ **MATCH=35 DIFF=0 EXTRA=0 MISSING=0 RESULT: PASS**，`V_TOTAL=35` |
| 逐文件 diff 主线 vs impB2 | 11 个文件，见 §2（行数用 `wc -l`、字节用 `diff -q`/md5） |
| 读契约 | `docs/V50_ARCHITECTURE.md` §2 D1/D4、§5 ⑨⑩⑪、§11 R2、§13、§14（全表）、§15 |
| 读历史 | `docs/PLAN_1second.md` §55.3/§57.1（C/M 分解）、§70.58（4 阻断）、§70.64~70.68（T-97/98/99）、`runs/v113_audit/REPORT_v113.md`、`runs/v99_coalesce/REPORT_v99_coalesce.md`、`runs/v50_impB2/REPORT_v50_impB2.md` |
| 读 PS | `ps/pynq/bin_net.py:1182-1265,1606-1680`、`ps/pynq/bin_engine.py:1813-2010,2343-2440,2517-2540` |

**引用纪律**：本报告每个行号都在写之前 `sed -n` 或 `grep -n` 复核过；括号里给的是我**实测**的行号，与旧报告不一致处**以本报告为准**（例：T-113 报 `segq:464`，实测是 **465**）。

**运行点声明**（避免混口径）：下文所有时间数字的 PL 项都带频率。基线 `v39 8-lane @150 MHz 256² = 8.096 s`（PL 3.061 / PS 5.03）；`PL(f) = 153.6×10⁶/f + 2.037 s`（`PLAN_1second.md:3459`，这是 §55.3 的**更正版**，§55.3 原文把 100 MHz 的 1.54 当成了 150 MHz）。

---

## 1. ① 模块地图（融合通路经过哪几个模块）

### 1.1 一句话

$$\text{DDR 张量}\xrightarrow[\text{E 口: } \mathtt{fill\_dma\_addr}]{\mathtt{bin\_band\_fill\_v50}}\text{band（双带 BRAM）}\xrightarrow[\text{A 口}]{\mathtt{bin\_conv\_core\_v50}}\xrightarrow[\text{B 口}]{\text{band 另一只带}}\xrightarrow[\text{C 口}]{\mathtt{bin\_e1e2}\ (\text{pack/cat})}\xrightarrow[\text{E 口}]{\mathtt{bin\_segq.drain}}\text{DDR}$$

整条链的**相位与地址**由 `bin_segq`（描述符链 + LOAD/RUN/DRAIN）与包装层 `bin_unit_axi_wrap_v50` 的融合块（mux + 几何 + 带选择）控制；`bin_band_seq` 负责把 RUN 展开成 CONV→PACK→SAM。

### 1.2 逐模块（主线 25+10 个文件里真正在链上的）

| 模块 | 文件（主线路径 `runs/timing_v50/rtl_v50/…`） | 职责 | 数据流角色 | 在主线? |
|---|---|---|---|---|
| `bin_unit_axi_wrap_v50` | `lane16/bin_unit_axi_wrap_v50.v`（3518 行） | 融合控制总成：`sel_fuse_q` mux、描述符影子、几何、带选择、写串行化、assert | 中枢：所有口都在这 | ✅（**只有 conv 一级**） |
| `bin_segq` | `lane16/bin_segq.v`（486 行） | 描述符链 FSM：S_HDR→S_CHK→S_LOAD→S_RUN→S_DRAIN→S_NEXT/S_TAIL/S_ERR；`progress`；出错即停 | 驱动 E 口（装/排）与 `band_start` | ✅ |
| `bin_band_buf` | `lane16/bin_band_buf.v`（389 行） | 双带 64-bit BRAM，1R1W 仲裁，A/B/C/D/E 五口，金丝雀字 | 片上暂存 | ✅（C/D 口**未接**，见 E2） |
| `bin_band_fill_v50` | `lane16/bin_band_fill_v50.v`（169 行） | E 口**装载**：逐 (grp,r,x) 算 DDR 地址，pad 行整行写 0 且不发 DMA | DDR → band | ✅ |
| `bin_conv_core_v50` | `lane16/bin_conv_core_v50.v`（925 行） | 16/8-lane conv 核；`base_act/base_out` 可置 0 指向 band | band → band | ✅ |
| `bin_e1e2` (pack/cat) | 基线 `bin_e1e2.v`（25 个 v39 文件之一） | 1-bit 打包（8 int8/字 → 64 通道/字） | DDR→DDR（v39 路径）；**带内 C/B 口在 impB2 才有** | ⚠ 只在 v39 路径 |
| `bin_band_seq` | **无**（只在 impB2） | RUN 内展开 CONV→PACK→SAM，FENCE 换带 | 级联控制器 | ❌ **缺失** |
| `bin_sam/interp/rprelu/catpack _v50` | `lane16/*_v50.v` | 融合壳（故意**未例化**，`V50_ARCHITECTURE.md:38`） | — | 空壳 |

### 1.3 数据流的三段（现在**只通第一段**）

| 段 | 走法 | 现状 |
|---|---|---|
| **① 装载** | DDR（**packed 1-bit act**，64 通道/字）→ fill → band0，字序 `g*(rows_fill*W)+r*W+x` | ✅ 通（`wrap:1974-1992`、`fill:87-107`） |
| **② 卷积** | band0 → conv（A 读）→ band1（B 写，经 16-lane 2 拍串行化） | ✅ 通（`wrap:1855`/`1859`） |
| **③ 排空** | band1 → segq DRAIN（E 读）→ DDR | ⚠ **地址缺带偏移**（B2） |
| ④ 带内 pack | conv → band → cat（C 读）→ DDR | ❌ 逻辑**恒假**（E2） |
| ⑤ 带内小算子 | sam/interp/rprelu 原地读写 band（D 口） | ❌ D 口绑 0（`wrap:1868-1874`） |

$$\Rightarrow\ \textbf{所以今天的"融合"= 把 conv 的输入从 DDR 挪到 band，输出仍写 DDR：DDR 流量一个字节都没省。}$$
$$\Rightarrow\ \text{要真省流量，必须走 ④（conv→cat，契约 §14.9 的 45 条段），那正是 } \mathtt{bin\_band\_seq}\ \text{的活。}$$

---

## 2. ② 逐文件差异表（主线 vs impB2）

主线根：`runs/timing_v50/rtl_v50/lane16/`（10 个 v50 文件）
impB2 根：`pynq_specialized_code/pl/rtl_hand/_worktrees/v50_impB2/`

| 文件 | 主线行 | impB2行 | Δ | 性质（**谁更新**） |
|---|---:|---:|---:|---|
| `bin_unit_axi_wrap_v50.v` | 3518 | 3478 | +40 | **主线新**：T-94 hold-until-accepted、T-105 s=2 平面修复、T-89 影子/几何/段边界寄存器；**impB2 独有**：band_seq + pack/sam 带内口（483 行 `.vh`）。diff `+455/−415` |
| `bin_segq.v` | 486 | 483 | +3 | **主线新**：T-89 四行 `out_pa` 修正（`dw[1][31:0]→dw[0][63:32]`，见下） |
| `bin_band_buf.v` | 389 | 369 | +20 | **主线新**：① `(* ram_style="block" *)`（`:145`，原推断成分布式 RAM ⇒ LUTRAM 83,690/14,400）② §12.2 `cfg_valid` 门控 `cap_err`（`:184`,`:337`） |
| `bin_band_fill_v50.v` | 169 | 151 | +18 | **主线新**：字段来源按 §13 改（w3 低=cfg_h / w3 高=H_in / **W 在 w4**）+ 5 条断言；impB2 把 w3 低 16 当 W |
| `bin_unit_axi_wrap_bd_v50.v` | 170 | 170 | 0（8 行改） | **默认参数相反**：主线 `VLANES=16 FUSE_EN=**0** BAND_ROWS=**1** HALO=0 C_MAX=**8** REG_BASE=0x300`；impB2 `VLANES=8 FUSE_EN=**1** BAND_ROWS=**8** HALO=1 C_MAX=**48** REG_BASE=0x2E0` |
| `bin_band_seq.v` | — | 261 | +261 | **只在 impB2**（新增），md5 `9699ee2d…`（与任务书一致） |
| `bin_conv_core_v50.v` | 925 | — | −925 | **只在主线**（IMP-A 的核；impB2 当时靠 IMP-A 的文件才 elab 得过） |
| `bin_catpack_v50.v` | 92 | 92 | 0 | 无实质差异（md5 `e5d7b3b7` 相同，且**未被例化**） |
| `bin_interp_v50.v` | 82 | 82 | 0 | 同 |
| `bin_rprelu_v50.v` | 85 | 85 | 0 | 同 |
| `bin_sam_v50.v` | 85 | 85 | 0 | 同 |

### 2.1 特征矩阵（谁有哪一处修复）

| 特征（可 grep 的符号） | 主线 | impB2 | 后果 |
|---|:--:|:--:|---|
| `pix_q` / `pbase_q`（T-94 写地址 C8 修正） | ✅ | ❌ | **impB2 的 16-lane 写地址是 T-93 越界写的那一版**（`impB2:1599-1609`：`cw_beat_addr <= c_wr_addr`） |
| 保持到被接受（`beat_go`/`wr_accept_ok` 互锁） | ✅ `wrap:1704-1737` | ❌ 只有 `cw_beat_en<=1'b0` 每拍强清 | impB2 = **T-94 前的丢拍版**（xsim 实测丢 69/512 拍） |
| `w_ss`/`w_hout`/`npix`（T-105 s=2 修复） | ✅ `wrap:1676-1681` | ❌ 只有 `plane_delta=(cfg_h*cfg_w)<<3`（`impB2:1591`） | impB2 在 s=2 层错 4× |
| `dw[0][63:32]` 当 `out_pa`（T-89） | ✅ `segq:243,245,343,350` | ❌ `dw[1][31:0]`（`impB2 segq:240,244,340,347`） | impB2 排空写到 `cin` 当地址 |
| 影子 `sh_dw[0:4]`（S_CHK 当拍可用几何） | ✅ `wrap:1374-1402` | ✅（自己一套 `d_*_sw`） | 等价 |
| `band_seq` + 级 start | ❌ | ✅ `impB2:1701-1723` | 主线只能单级 |
| pack 写 band（B 口）/ 读 band（C 口） | ❌（C 恒假） | ✅ `impB2:1906-1911` | 主线省不了 DDR |
| sam 的 D 口 | ❌（绑 0） | ✅ `impB2:1915-1918` | — |
| 带角色由 seq 给（含 FENCE） | ❌（`seg_state` 组合函数 `wrap:1790-1791`） | ✅ `impB2:1869-1871` | 主线 LOAD→RUN 换带那一拍**可能把最后一个装带字写进输出带**（`band_seq` 头注 `:26-30` 自述） |
| `band_hi` 读口按地址分 band/DDR | ❌ | ✅ `impB2:1671-1690` | pack 读阈值、sam 读 int16 权重走 DDR，需分流 |

$$\Rightarrow\ \textbf{净增量一句话}：\text{impB2 的增量 = 级联序列器 + pack/sam 的带内口（约 483 行）；主线的增量 = T-89/T-94/T-105 三处\textbf{正确性/时序}修复（约 455 行）。}$$
$$\Rightarrow\ \Rightarrow\ \textbf{必须"移植 impB2 的融合块"，不能"拷 impB2 的文件"}（\text{拷过来会把三个已修的 bug 一起带回来）。}$$

### 2.2 impB2 结构上真正的价值：`tools/v50b_fuse_block.vh`

impB2 的 `bin_unit_axi_wrap_v50.v` 不是手写的，是 `tools/patch_make_v50b_wrap.py`（15 处定点替换，每处断言命中一次）从 v39 **重新生成**的；融合正文在 `tools/v50b_fuse_block.vh`（**483 行**）。⇒ 合并的正确形状是"把这份 .vh 装到当前主线的 v39 骨架上"，而主线现在这份融合块（`wrap:1602-2070`）是从 IMP-B 那一版长出来的、没有 seq。**两边融合块的行数相当（约 460 vs 483），手工对齐可行但必须逐段过。**

---

## 3. ③ 阻断项清单（行号 + 真假 + 修法 + 工作量）

> 判定口径：**真阻断** = 会静默错数或让 FUSE=1 根本跑不起来；**性能项** = 不影响逐字节正确；**假警报** = 现状不阻断。

### B1 `band 通路写合并缺失` —— **性能项（且对排空不成立）**，不是正确性阻断

**代码现状**
$$\Rightarrow\ \text{主线写引擎}\textbf{有}合并：\text{"同 } \mathtt{mem\_wr\_addr[31{:}3]}\text{ 合并成 1 个 beat，相邻 beat }\mathtt{addr+8}\text{ 串成一笔 burst，上限 } \mathtt{WR\_BURST\_MAX}=16"\ (\mathtt{wrap:213-247}\text{ 注释 + } \mathtt{BF}/\mathtt{DF}\text{ FIFO})$$
$$\Rightarrow\ \text{但 16-lane 的 beat0/beat1 \textbf{交替两个相隔 } H_{\text{out}}W_{\text{out}}\times 8\text{ 的平面}\Rightarrow \text{每拍断链}\Rightarrow \textbf{1 beat/AW}（\text{T-97 铁证：}V16\ \text{8192 笔 AW}）\Rightarrow \text{板端 } 20\sim22\ \text{拍/8B beat}$$
$$\Rightarrow\ \text{T-99 的"每平面攒 8 拍"合并器：}\mathbf{\text{未进主线}}（\text{存档 } \mathtt{runs/v99\_coalesce/after\_rtl\_coalescer/bin\_unit\_axi\_wrap\_v50.v}\text{，md5 } \mathtt{26d58123}\text{；收益 } 1.00\to7.93\ \text{beat/AW}\text{，C5 时序未测}）$$

**是否真阻断**：对 **FUSE=1 不阻断**，理由两条（都可复核）：
1. FUSE=1 时 conv 的输出**不写 DDR**，写的是 band 的 B 口（`wrap:1859-1862`，BRAM 字节使能写，与 AXI 无关）；
2. FUSE=1 时唯一的 DDR 写是排空（`segq:463-468 → wrap:1754-1757`），地址是 `st_base+cnt*8`，**严格连续**（唯一跳变在换组，见 B2）⇒ 现有引擎自然攒到 8~16 拍/burst。

**结论**：B1 是 **FUSE=0 + VLANES=16** 的吞吐墙，不是融合的准入条件。**融合反而顺手消灭了它**（不再有平面交替写）。
**修法**：若要提速，把 T-99 存档合并器重新启用（只改 `g_wr_split2`，`g_wr_direct` 一行未动）；**与融合无耦合，可独立排期**。工作量：启用+复验 ≈0.5 天，出位流需 +1 次综合到位（C5 从未做过，约 40 min）。

### B2 `drain 缺 band 偏移` —— **真阻断（静默错数）**，且**修法比 T-113 给的更复杂**

**代码现状（实测行号）**
```verilog
// runs/timing_v50/rtl_v50/lane16/bin_segq.v
350:  st_base <= dw[0][63:32];                  // 字1 = out_pa（T-89 已修）
460:  ld_wa   <= {cnt_q[28:0], 3'd0};           // band 内相对：正确
465:  st_wa   <= st_base + {cnt_q[28:0], 3'd0}; // ← 排空 DDR 地址：**无 y0、无组间距**
```
$$\Rightarrow\ \mathtt{y0}\ \text{全树只有填充器用：}\mathtt{wrap:1981}\ \mathtt{.y0(sh\_y0)}；\text{排空侧}\textbf{零使用}$$
$$\Rightarrow\ \text{PS 侧也刻意不加：}\mathtt{bin\_net.py:1212-1214}\ \text{"}out\_pa\ \text{传段基址，}\textbf{不加}\ y0\cdot row\_bytes\text{"}\Rightarrow \textbf{两边各让一步 = 哪边都没加}$$

**为什么"加 y0 偏移"不够**（这是 T-113 §3-③ 修法的缺口，我算过）：
$$\text{band 字序} = g\cdot(\underbrace{\mathtt{nband}}_{=rows\_fill,\ halo=0}W) + rW + x\qquad (W=\text{行宽},\ g=\lceil c/8\rceil\text{ 通道组})$$
$$\text{DDR 字序} = g\cdot(H_{\text{out}}W) + (y0+r)W + x$$
$$\Rightarrow\ \mathtt{st\_wa}^{\text{应}} = \mathtt{out\_pa} + y0\cdot W\cdot8 + g\cdot\underbrace{(H_{\text{out}}-\mathtt{nband})}_{\text{组间空档}}\cdot W\cdot8 + \underbrace{\mathtt{cnt}\cdot8}_{\text{现有}}$$
$$\Rightarrow\ \textbf{缺的是两项：}\text{① 常数 } y0\cdot W\cdot8；\ \text{② 每组 } (H_{\text{out}}-\mathtt{nband})\cdot W\cdot8\ \text{的空档（cnt 里没有组号，需要计数器或除法）}$$

**量化（与 T-113 的 1/16 一致）**：256² 的 128² 层、`h_out=128`、`W=128`、`cout=96`、`BAND_ROWS=8` ⇒ `bands=16`、`nband=8`、`g_out=12`、每次排空 `12·8·128=12288` 字 = **96 KB**，而 16 条描述符**全写同一段 96 KB**，张量需 1.5 MB ⇒ **只落 1/16**。
$$\Rightarrow\ \text{而且 arena 判据也兜不住：}\mathtt{segq:245-246}\ \text{查的是 } \mathtt{band\_words}\ll3 = 96\ \text{KB，不是 1.5 MB}\Rightarrow \textbf{越界检查恒过}$$

**什么形状不触发**：`bands=1`（即 `BAND_ROWS ≥ h_out`）或 `ceil(cout/8)=1`（cout≤8）——两项同时消失。

**修法（不改状态机的时间轴）**：
1. **描述符加 `H_out`**：`w4` 的高 16 位**当前零读取**（§14.8 悬空字段、`wrap:1397` 只取低 16）⇒ 直接把 `H_out` 放 `w4[31:16]`，PS 在 `bin_net.py:v50_seg_descs` 填，契约 B 与 `v50_desc` 同步。**不动 `w3/w6/w9`，不新增寄存器。**
2. **segq 排空加游标**：把 `cnt_q` 一行拆成 `(gq, rq, xq)`（与填充器 `fill:87-107` 完全同构，无除法器），`st_wa = st_base + y0*W*8 + gq*(H_out-nband)*W*8 + rq*W*8 + xq*8`。需要 `y0`（`dw[3][15:0]`，现成）与 `H_out`。
3. **PS 删掉"不许加偏移"的注释约定**（`bin_net.py:1212-1214`）并加一条断言：多带段的 `out_pa` 必须等于段基址。

**工作量**：segq ≈25 行 + PS ≈6 行 + 契约 1 节 + 定向 TB（3 形状）⇒ **0.5~1 天**。风险低（只动 S_DRAIN 的地址生成，`FUSE=0` 不可达）。

### B3 `bin_band_seq.v` 不在主线 —— **真阻断（功能缺失，不是错数）**

**代码现状**
$$\Rightarrow\ \text{主线 } \mathtt{wrap:1468}\ \mathtt{c\_start = fuse\_m\ ?\ seg\_band\_start : ...}；\mathtt{e\_start/k\_start}\ \text{仍只走 v39 地址选择}$$
$$\Rightarrow\ \text{没有 seq ⇒ RUN 里只起一个单元 ⇒ } \text{契约 §15.4：}\textbf{含 ≥2 级的段若判成融合，后面的级被静默跳过（错数，不报错）}$$
$$\Rightarrow\ \text{PS 侧因此加了门禁：}\mathtt{bin\_net.py:1642}\ \textbf{`len(ops)==1 && ops[0].kind=="conv"`}\ \text{才融合} \Rightarrow \textbf{90/223 段、140 描述符}（\mathtt{V50\_ARCHITECTURE.md:509}）$$
$$\Rightarrow\ \Rightarrow\ \text{而 conv 单级融合}\textbf{省不了 DDR}（\S 1.3）\Rightarrow \textbf{B3 不解决，融合的收益恒为 0}$$

**impB2 的这一份是可用的**（我读过，质量高于主线现状）：
| 它解决什么 | 位置（`impB2/bin_band_seq.v`） |
|---|---|
| RUN 展开 CONV→PACK→SAM，`nxt()` 常量折叠 | `:89-92` 状态、`:109` `nxt()` |
| ⑬ **换带前必须写静默**（FENCE）——修掉主线 `wrap:1790-1791` 的"LOAD→RUN 换带那一拍把最后一个装带字写进输出带" | `:203-218` |
| ⑫ 每级读带≠写带（ping-pong） | `:26-30` 头注 + `rd_bank/wr_bank` |
| 跳过的级真不启动 | `:219-226` |

**修法**：把 `impB2/tools/v50b_fuse_block.vh`（483 行）装到**当前主线骨架**上，需要解决 6 处冲突（全部是可定位的定点对齐，不是重写）：
1. 主线 `sh_*` 影子（`wrap:1374-1402`）保留，impB2 的 `d_*_sw` 别再引一套；
2. 主线 `geo_loadw/geo_drnw/geo_chkw`（`wrap:1826-1831`）要扩成 impB2 的三态（`geo_gp64` / pack 级排空字数）；
3. 主线 `g_wr_split2` 的 T-94/T-105 修复**必须保留**，impB2 那版是坏的（见 §2.1）；
4. `OP_MASK_SUP` 主线已是 `16'h0003`（`wrap:1891`），impB2 也是，一致；
5. 主线的段边界寄存器（cout/k/s/d/pad，`wrap:1441-1445`）保留（impB2 用描述符字段）；
6. impB2 的 `band_hi` 读口地址分流（`impB2:1671-1690`）是**新语义**，必须一起移植，否则 pack 读阈值/sam 读 int16 权重会读到 band（错数）；其中 `b_wr_addr = e_wr_addr - 32'd8`（`impB2:1907`）是**无断言的魔数**，合并时必须写成带注释的显式修正并加断言。

**工作量**：移植+对齐 ≈1.5~3 天（483 行走一遍 + 6 处冲突）；配套 TB ≈1~2 天（B3 与 E4 是同一件事）。

### B4 `k≥3 时 cfg_h 双用途` —— **真阻断**，且**不是"一个 cfg_h 装不下"，是"少了第二个行参数"**

**代码现状（实测）**
```verilog
// bin_conv_core_v50.v
156:  wire [15:0] hout_c = ((cfg_h - 16'd1) >> s_sh_c) + 16'd1;   // 用途①：输出行数
163:  wire [31:0] hw8_c  = ({16'd0,cfg_h} * {16'd0,cfg_w}) << 3;  // 用途②：64-通道块的平面步长
696:  plane_stride_q <= ({16'd0,hout_q} * {16'd0,wout_q}) * VLANES;// 用途①再进 16-lane 组平面步长
805:  cp1_q <= mn_cb_q * hw8_q;                                  // 用途②的消费者（换 64 通道块时跳）
```
$$\Rightarrow\ \text{band 要求块步长} = \mathtt{rows\_fill}\cdot W\cdot8，\text{输出行数要求} = \mathtt{nband}$$
$$\Rightarrow\ k=1\Rightarrow \mathtt{halo}=0\Rightarrow \mathtt{rows\_fill}=\mathtt{nband}\Rightarrow \text{两用途自洽（现有 k=1 锁的全部依据）}$$
$$\Rightarrow\ k=3\Rightarrow \mathtt{halo}=1\ (\text{单侧}，\text{契约 }§11.4\ \sum(k_i-1)/2)，\mathtt{rows\_fill}=\mathtt{nband}+2$$
$$\quad\Rightarrow\ \text{若 } \mathtt{cfg\_h}=\mathtt{nband}：\text{块步长少 2 行}\Rightarrow cin>64\ \text{时第 2 个 64-通道块读错址}$$
$$\quad\Rightarrow\ \text{若 } \mathtt{cfg\_h}=\mathtt{rows\_fill}：\mathtt{hout\_c}=\mathtt{nband}+2\ (\text{多了 2 行输出})，\text{且 } \mathtt{plane\_stride}\ \text{错}$$
$$\Rightarrow\ \Rightarrow\ \textbf{一个 } \mathtt{cfg\_h}\ \text{无论填哪个值都不行，必须给第二个行数}$$

**还有一个隐藏项（T-113/契约都没点明）**：`cfg_pad` 在融合模式下仍是段边界寄存器 `r_pad`（`wrap:1445`，**没有** `fuse_m` 二选一），而契约 §11.3 说"pad 由填充路径做，conv 读侧零边界"。k=3 时若 `r_pad=1`，`hout_c` 会再吃掉 2 行 ⇒ 输出行数变成 `nband+2`。**⇒ k≥3 融合必须同时把 conv 的 `cfg_pad` 强制为 0**（`wrap:1445` 加 `fuse_m ? 16'd0 : r_pad`）。

**修法**：给核加一个输入 `cfg_rows`（默认 `cfg_h`，融合时=`rows_fill`），只把 `:163`/`:805` 的块步长换成它；`cfg_h` 继续只管输出行数。`FUSE=0`/`k=1` 时 `cfg_rows==cfg_h` ⇒ 逐位等价（可当回归判据）。
**工作量**：核 3 行 + 包装 2 行 + 契约 1 节 + 等价回归（v39 三 sha1）⇒ **~1 天**。注意这是 **IMP-A 拥有的文件**，按 §6 冲突规则要先改契约再动。

### B5（本次新发现，**优先级最高**）`LOAD→RUN` 换带必触发 `arb_conflict` ⇒ 每个带都以 `E_CANARY` 结束

**代码现状（全部实测）**
```verilog
// bin_band_buf.v   —— 换带时"写未静默"的探测器
159: assign wr_all_idle = (wr_idle_q == 2'd0);
302: else if (wr_en_m) wr_idle_q <= 2'd2;   else if (wr_idle_q != 0) wr_idle_q <= wr_idle_q - 1;
327: wire role_bad_now = int_sel_chg && (wr_idle_q != 2'd0);
341: if (wr_two || role_bad_now) arb_conf_q <= 1'b1;    // 粘滞
// bin_unit_axi_wrap_v50.v —— 带角色是 seg_state 的**组合函数**
1790: wire bank_rd_sel = (seg_state == 4'd5);   // DRAIN 读 bank1
1791: wire bank_wr_sel = (seg_state == 4'd4);   // RUN  写 bank1
// bin_segq.v —— LOAD→RUN 的出口**不看写静默**
364: if (cnt_q + 32'd1 >= band_words) begin  cnt_q <= 0; st <= S_RUN;  end
376: S_RUN: if (tmo_q == 32'd1) start_q <= 1'b1;   // 进 RUN 后第一个 en 拍就起单元
```
**时序推演**：`ld_we`/`seg_ld_we` 是**打过一拍**的（`segq:458-462`）⇒ 最后一个装载字实际**在 `S_RUN` 第一拍才写进 band**；而 LOAD 期间逐拍背靠背写，`wr_idle_q` 恒为 2。于是 `S_RUN` 第一拍：`bank_wr_sel` 已由 0 变 1，而 `wr_sel_q` 仍是上一拍的 0 ⇒ `int_sel_chg=1`；同拍 `wr_idle_q=2`（非 0）⇒ **`role_bad_now=1`**
$$\Rightarrow\ \mathtt{arb\_conflict}\ \text{粘滞}\to \mathtt{band\_bad}\ (\mathtt{wrap:1833})\to \mathtt{canary\_bad}\to \mathtt{segq}\ \mathtt{S\_ERR}(\mathtt{segq:440-442},\ \text{code } E\_CANARY}$$
$$\Rightarrow\ \Rightarrow\ \textbf{流式装载下每一个带都会在第一次 LOAD→RUN 中止}；\text{与 B2 无关、与 } k\ \text{无关、与 } lane\ \text{数无关}$$
$$\Rightarrow\ \text{只有"最后一笔装载写距换带} \ge 3\ \text{拍"才侥幸不报（正常流式不会出现）；且 } \mathtt{arb\_conflict}\ \text{粘滞到 } \mathtt{buf\_reset}（=\mathtt{seg\_band\_abort|start\_fire}，\mathtt{wrap:1847}）$$

**为什么一直没被发现**：`RUN→DRAIN` 那条边是**安全的**（`seg_band_done` 含 `band_wr_idle`，`wrap:1831`）；而 §15.5 的 28 项门禁跑的是 **segq 单模块 TB**（band 是 mock，`role_bad_now` 根本不参与）⇒ 只有 E4 的带级集成 TB 能抓到。`impB2/bin_band_seq.v:26-30` 的头注写的正是这件事（"IMP-B 版本把 bank_sel 直接接成 seg_state 的组合函数 ⇒ 必然置 arb_conflict"），但主线仍是 IMP-B 那一版。

**修法（两选一）**
1. **最小**：`segq:364` 的出口加 `&& wr_all_idle`（segq 已有这个输入，`segq:109`/`S_TAIL` 在用）⇒ 1 行，代价是每个带多 2 拍。
2. **正确**（随 B3）：用 `bin_band_seq` 的 FENCE 状态做所有换带，`bank_*_sel` 改吃 `seq_rd_bank/seq_wr_bank`（`impB2:1869-1871`）。

**工作量**：方案 1 = 1 行 + 3 个形状的 TB（0.5 天，可立刻让 L1 级跑通）；方案 2 含在 B3。

### 额外发现（本次新查出来的，都在正确性路径上）

| # | 位置 | 问题 | 真假 | 修法 |
|---|---|---|---|---|
| **E1** | `bd_v50.tcl:171` `foreach {_pn _pv} {FUSE_EN 0 BAND_ROWS 1 BAND_HALO 0 BAND_C_MAX 8}`；`build_final_150v50.tcl:157` 同一组硬断言；`lane16/bin_unit_axi_wrap_bd_v50.v:22-26` 默认值同样被改成 `FUSE_EN=0/ROWS=1/C_MAX=8` | **现役 v53/v54 位流的打包层参数是 `VLANES=16, FUSE_EN=0, BAND_ROWS=1, BAND_C_MAX=8`**（`runs/v53_archive/build_t107.log:257-259`、`runs/v54_archive/build_v54.log` 同）⇒ ① `sel_fuse_c=(UNIT==0)&&(FUSE_EN!=0)&&r_fuse_mode[0]`（`wrap:560`）**恒 0** ⇒ 融合物理上不可能跑；② `cfg_c=c_cfg_cin`（`wrap:1845`）而 `shape_bad` 要求 `cfg_c≤C_MAX=8` 且 `%8==0`（`band_buf:166`）⇒ 任何真层（cin≥32）都会 `cap_err`→`E_CANARY` | **真阻断（硬件级）** | 出融合位流必须 `FUSE_EN=1 BAND_ROWS≥2 BAND_HALO≥1 BAND_C_MAX=48`；**同时改这 3 处**（bd tcl / build 硬断言 / bd 外壳默认值），否则构建脚本自己会 `error` 退出 |
| **E2** | `wrap:1864` `.c_rd_en(fuse_m && sel_e1e2_q && e_rd_en)` 与 `wrap:1342` `fuse_m = sel_fuse_q && !sel_e1e2_q && !sel_cp_q` | **代数恒假**（`fuse_m` 与 `sel_e1e2_q` 互斥）⇒ band 的 C 口（pack 读带）**永远不使能**；`.d_rd_en(1'b0)`（`:1868`）同 | **真阻断**（这就是"融合省不了 DDR"的代码原因） | 合并 B3 时按 impB2:1911 改成 `fuse_m && (seq_stage==SQ_PACK) && pack_is_band && e_rd_en` |
| **E3** | `bin_engine.py:1858-1864`（探针只看 `0x314 CHAIN_ID` magic）；`wrap:489-492` 注释"读回的是常量…FUSE_MODE 无关" | **能力探针不足以判"这条位流能不能融合"**：magic `0x5030_0001` 在 `FUSE_EN=0` 的位流上**照样读得出**（v53/v54 就是）⇒ PS 会 `chain_on()=True`，写 `CHAIN_HEAD` 后 `start()`，但 RTL 的 `kick`（`wrap:1896`）被 `fuse_m` 门控 ⇒ `PROGRESS` 恒 0 ⇒ PS 超时；**更糟**：`start()` 会启动 v39 单元，而链只写了 ARENA/BAND_ROWS/FUSE_MODE/CHAIN_HEAD 四个寄存器（`bin_engine.py:1965-1971`），`r_in/r_out/r_c…` 全是上一条命令的残值 ⇒ **可能写坏内存**（`板端事故_内存踩踏_真相.md` 同一失效类） | **真阻断（安全）** | ① RTL 把 magic 改成"`FUSE_EN` 生效才给"（或加第二常数 `0x5030_0002` 只在 `FUSE_EN=1` 时返回）；② PS `chain_on()` 必须读它；③ 或者 kick 后 1 ms 内 `PROGRESS` 不动 ⇒ 立即判"不是融合位流"并 `poison` |
| **E4** | 主线 `wrap:1900` `band_words`、`fill` 的 5 条断言（`fill:145-166`） | 缺**带级集成 TB**：`fill→conv→band→drain` 一次走完的逐字节证据不存在（只有 segq 单模块 TB 22 项、band_buf/conv 各自 TB）。契约 §15.6-① 已把它列为上板前必须 | 真（门禁缺失） | 见 §4 阶梯 0~3 |
| **E5** | `fill:145-166` 断言 + `wrap:1988` `.en(u_ce && fuse_m)` | **仿真期每拍 `$error` 刷屏**：`fuse_m` 一置位、首条描述符还没读完时 `sh_w_row=0` ⇒ 断言④（`fill:159`）逐拍触发（`PLAN_1second.md:5738` 已记，**未修**） | 假警报（但污染日志/门禁） | 断言加 `&& (seg_state>=S_LOAD)` 或 `&& sh_done` 门控 |
| **E6** | `wrap:1790-1791` `bank_rd_sel=(seg_state==5)`、`bank_wr_sel=(seg_state==4)` | 带角色是 `seg_state` 的**组合函数** ⇒ LOAD→RUN 那一拍写带从 bank0 跳到 bank1，最后一个装带字落进输出带 + `arb_conflict`（`band_buf:341`）粘滞 → E_CANARY。`band_seq` 头注 `:26-30` 明说它修的就是这个 | 真（k=1 时也会中，取决于最后一拍的相位） | 随 B3 一起换成 `seq_wr_bank/seq_rd_bank`（`impB2:1869-1871`） |

### 3.1 一张表收口

| 编号 | 名称 | 真阻断? | 错数/不跑/慢 | 修法规模 | 工作量 |
|---|---|---|---|---|---|
| B1 | band 通路写合并 | ❌ 性能项 | 慢（只影响 FUSE=0+V16） | 启用 T-99 存档 | 0.5 天（独立） |
| B2 | drain 缺 band 偏移 | ✅ | 静默错数（多带必错，1/16） | segq 游标 + w4 高 16=H_out | 0.5~1 天 |
| B3 | 缺 bin_band_seq | ✅ | 功能缺失 ⇒ 收益恒 0 | 移植 .vh（6 处冲突） | 1.5~3 天 +TB1~2 天 |
| B4 | k≥3 cfg_h 双用途 | ✅ | 静默错数（cin>64 或行数多 2） | 核加 `cfg_rows` + pad 强制 0 | ~1 天 |
| E1 | 位流 FUSE_EN=0/C_MAX=8 | ✅ | 融合根本跑不起来 | 3 处构建参数 | 0.5 天 + 1 综合 |
| **B5** | **LOAD→RUN 换带必报 E_CANARY** | ✅ **（最先撞上）** | **每个带都中止** | segq 出口加 `wr_all_idle`（1 行）/ 随 B3 的 FENCE | 0.5 天 / 含 B3 |
| E2 | C 口恒假 | ✅ | 收益=0 | 随 B3 | 含在 B3 |
| E3 | 能力探针不判 FUSE_EN | ✅ | 超时 / 可能写坏内存 | RTL magic + PS 探针 | 0.5 天 |
| E6 | 带角色组合函数 | ✅ | **见 B5（同一根因，B5 是它的定量版）** | — | — |
| E4/E5 | 集成 TB / 断言刷屏 | 门禁 | — | TB + 门控 | 1~2 天 |

---

## 4. ④ 最小可验路径

### 4.1 先纠一处口径（任务书里的"两层 k=1 conv 链"在本网络里不存在）

$$\Rightarrow\ \text{band 里的 conv \textbf{输入}是 packed 1-bit（64 通道/字，}\mathtt{fill:98}\text{ 的 } blk\_stride=H_{in}W8\text{），\textbf{输出}是 int8（8 通道/字）}$$
$$\Rightarrow\ \Rightarrow\ \textbf{conv 后面接不了 conv}（\text{类型不匹配，中间必须有 pack}）$$
$$\Rightarrow\ \text{本网络的 2 级段是 }\mathbf{conv\to cat}\ (\mathtt{V50\_OP\_BITS}\ \mathtt{cat}=bit1，\mathtt{bin\_engine.py:2397})，\textbf{45 条}（\mathtt{V50\_ARCHITECTURE.md:462}）$$
$$\Rightarrow\ \Rightarrow\ \text{第一枪应当是 **单级 conv + 1 条描述符**（验 fill→conv→drain 的机械正确性），第二枪 **conv→cat 两级**（验 B3 与"真省 DDR"}$$

### 4.2 阶梯（每级都有"改前必须失败"的对照）

| 级 | 形状（xsim，`bin_unit_axi_wrap_v50`，RTL 默认参数：`FUSE_EN=1 BAND_ROWS=8 HALO=1 C_MAX=48`） | 验证什么 | 判据 |
|---|---|---|---|
| **L0** | `xvlog -sv` 11 个 lane16/基线文件 + `xelab` `VLANES=8/16` | 合并后能不能 elaborate | 0 error；`VLANES=8` 时**不生成** seq/band（`FUSE_EN` 分支） |
| **L1** | `cin=32 cout=32 k=1 s=1 64×64`，**`BAND_ROWS=64`（=h_out ⇒ bands=1）**，`BAND_W_MAX=64`、`BAND_C_MAX=32` | 单带单/多组的**基线**：B2 两项都消失（y0=0、nband=h_out）⇒ 只验机械正确性 | 输出 32 KB **逐字节** = FUSE=0 同形状结果；band 内容也逐字节比；`arb_conflict=0`、`cap_err=0`、`canary` 未动 |
| **L2** | `cin=32 cout=8`（⇒`ceil(cout/8)=1`）`BAND_ROWS=8` ⇒ bands=8 | **单独隔离 B2-①**（缺 `y0` 常数）：单组没有组间距问题 | 必须**失败**（改前）；加 `y0*W*8` 后必须**通过** ⇒ 这条直接给 B2-① 定罪 |
| **L3** | `cin=32 cout=32 BAND_ROWS=8` ⇒ bands=8、4 组 | **B2-②**（组间空档 `(H_out-nband)W8`） | 同 L2：改前错、加组游标后对 |
| **L4** | `conv(k=1)→cat` 两级、1 段 2 级、`BAND_ROWS=8`、64×64 | B3 的级联 + pack 读/写带 + `band_hi` 分流 | 最终输出（packed）逐字节 = v39 两趟结果；**且 DDR 写字节数下降**（计数 `cnt_wbeat` 差值 = conv 输出那张 int8 张量的 2 倍） |

$$\Rightarrow\ \textbf{前置}：\text{L1 之前必须先落 } \textbf{B5 的一行修法}（\text{否则第一级就 } E\_CANARY\text{，}\mathtt{arb\_conflict} \text{ 粘滞且不可恢复}）；\text{L1~L3 用 RTL 默认参数即可，}\textbf{不需要出位流}$$
$$\Rightarrow\ \text{开关：}\mathtt{BIN\_V50\_FUSE=1}\ (\mathtt{bin\_engine.py:2408})、\mathtt{BIN\_V50\_BAND\_ROWS}\ (\mathtt{bin\_engine.py:2428}\text{，默认 8})；\text{RTL 侧 } \mathtt{0x30C[0]=1} + \mathtt{0x308} + \mathtt{0x318/0x31C}\ \text{arena（必填，}\mathtt{bin\_engine.py:1954}\text{）}$$
$$\Rightarrow\ \text{xsim 可复用现成 harness：}\mathtt{worktrees/T-134-lat/tb\_v94\_wrap.v}\ (\text{完整 AXI3 从机 + plusarg 形状 + golden 目录}) + \mathtt{run.sh}\ (\mathtt{xsim\ snap} + \mathtt{+GOLDEN=})$$
$$\quad\ \text{只需加：}\mathtt{+FUSE=1} \Rightarrow \text{① TB 写 } \mathtt{0x318/0x31C/0x308/0x30C}；\text{② 在 TB 的存储器模型里摆 1~2 条 40 B 描述符；③ 写 } \mathtt{0x300}\ \text{后 } \mathtt{start}；\text{④ 轮询 } \mathtt{0x304/0x310}$$
$$\Rightarrow\ \text{判据口径：}\mathbf{\text{与"不融合"逐字节比}，\text{不是与 numpy 比}}（\text{先证明"融合 ≡ v39"}，\text{再单独证 v39 = golden，两者分离才可归因}）$$

### 4.3 硬件侧的最终判据（不属本次，列出以免误判"做完"）

$$\Rightarrow\ \text{PS 级：}\mathtt{verify\_lazy\_net --hw 64}\ \text{的三 sha1 冻结值 } \mathtt{104710f69c4d/2ac6efec2325/8ab595fcf7cd}\ (\mathtt{V50\_ARCHITECTURE.md:519})$$
$$\Rightarrow\ \text{且必须 }\mathtt{BIN\_V50\_FUSE=0}\ \text{与 } \mathtt{=1}\ \mathbf{\text{两次都命中同一组}}（\text{否则只是回落成功，不是融合成功}）$$
$$\Rightarrow\ \text{资源前置（估算，需一次 OOC 综合确认）：真 band（}C_{MAX}=48,ROWS=8,HALO=1,W_{MAX}=256\text{）=245,776 B}\approx 54\ \text{个 BRAM36}$$
$$\quad\ \text{v50 16-lane 实测 } \mathtt{BRAM}=43/95\ (\mathtt{PLAN:5754}\text{，含 stub band 约 2 个})\Rightarrow 41+54=\mathbf{95/95}\ \textbf{正好卡在墙上}$$
$$\quad\ \Rightarrow\ \textbf{16-lane + 真 band 的 BRAM 需要先算：}BAND\_ROWS=4\ \text{或 }BAND\_W\_MAX\ \text{按层收紧，否则直接超器件}$$

---

## 5. 判断：融合能不能把 PL 做到 1 s

### 5.1 硬算式（这是本节的全部依据）

$$\mathtt{PL}(f)=\frac{153.6\times10^{6}}{f}+T_m,\qquad T_m=2.037\ \text{s}\ (\mathtt{PLAN:3458-3459})$$
$$\Rightarrow\ f=150\ \text{MHz}\Rightarrow \underbrace{1.024\ \text{s}}_{\text{纯拍数}} + 2.037 = 3.061\ ✓$$
$$\Rightarrow\ \Rightarrow\ \textbf{1.024 s 是"一个内存周期都不停"的下界}；\text{要 } \mathtt{PL}=1.0\ \text{s} \Rightarrow \text{需 } f>153.6\ \text{MHz 或拍数再降}$$

$$\text{融合后的下界} = \max\left(\underbrace{1.024\ \text{s}}_{\text{算力}},\ \underbrace{\frac{B_{\text{残}}}{413\ \text{MB/s}}}_{\text{实测有效读带宽，}\mathtt{PLAN:3472-3478}}\right)$$

### 5.2 三种口径下的答案

| 口径 | 结论 |
|---|---|
| **今天这条融合**（单级 conv、`op_mask=0x1`、90/223 段） | 收益 **≈ 0**：band 只是把 conv 的输入从 DDR 搬到片上，输出照写 DDR（§1.3）。它能改善的是 conv 读的瀑布（把 miss 冻结换成线性 DMA），**但 2.04 s 的主要项在 pack 侧**（`PLAN:3472-3478`：E1E2 路径读 miss 冻结 46~50%），conv 单级融合碰不到 |
| **把 B2/B3/E2 做完**（conv→cat 45 段真融合） | 每段消灭 `2×cout` B/px（conv 的 int8 输出写 + cat 的 int8 读）。按 1890 MB 现状（`PLAN:129`）估，残量落到 ~600~800 MB ⇒ `T_m` 残 **1.5~1.9 s**，`PL` **≈1.7~2.2 s @150 MHz**（仍 ≥ 算力下界）。⇒ **8.096 s → ~6.8~7.2 s** |
| **要到 1 s** | 需要 `T_m→0` **且** 拍数不变 ⇒ 恰好 1.02 s，**物理上贴着下界**；再往上只能靠 (a) 拍数降 47.9%（mlp 多笔在飞，`PLAN:3356`）⇒ 算力 0.53 s，(b) 时钟 >153.6 MHz（但 T-100 已证 100 MHz 都不稳，`PLAN:5968`）。**⇒ 融合是必要条件，不是充分条件** |

### 5.3 我的判断（一句话）

$$\Rightarrow\ \textbf{"融合能到 1 s" 这个说法不成立}：\text{它能把 } 2.04\ \text{s 的访存}\textbf{砍掉一半左右}，\text{把 PL 从 } 3.06\ \text{推到 } \approx1.7\sim2.2\ \text{s}；$$
$$\Rightarrow\ \text{要 1 s 必须同时拿到 } \textbf{(i) 级联融合（B3）}\text{、}\textbf{(ii) 拍数下降（mlp）}，\text{而 (ii) 与融合正交、现在被"结果正确性"挡着}；$$
$$\Rightarrow\ \textbf{但方向是对的}：\text{今天 }\mathbf{0}\ \text{收益的原因是 } \textbf{E2/B3}\ \text{（单级 conv 不算融合）}，\text{不是融合本身没用。}$$
$$\Rightarrow\ \Rightarrow\ \textbf{优先级建议}：\underbrace{\mathtt{B5}\ (\text{1 行，否则一个带都跑不完})}_{\text{今天就能做，xsim}} \to \underbrace{\mathtt{B2}}_{\text{多带不错数}} \to \underbrace{\mathtt{B3+E2}}_{\text{拿真收益}} \to \underbrace{\mathtt{E1+E3}}_{\text{位流/安全打开}} \to \mathtt{B4}\ (k\ge3) \to \mathtt{B1}$$
$$\Rightarrow\ \textbf{并行度}：\mathtt{B5}/\mathtt{B2}\ (\text{segq+band\_buf 域}) \parallel \mathtt{B3}\ (\text{wrap 融合块域}) \parallel \mathtt{E1}\ (\text{BD/构建域}) \parallel \mathtt{E4}\ (\text{TB 域}) \Rightarrow \text{4 条互不碰文件，可在途；但 } \mathtt{B3}\ \text{与 } \mathtt{E4}\ \text{必须同一人闭环。}$$

---

## 6. 附录：本次复核用到的命令（可复算）

```bash
# 主线 RTL 根
bash pynq_specialized_code/pl/scripts/verify_rtl_root.sh \
     runs/timing_v50/rtl_v50_manifest.txt runs/timing_v50/rtl_v50
#   ⇒ MATCH=35 DIFF=0 EXTRA=0 MISSING=0 RESULT: PASS  V_TOTAL=35

# 逐文件差异
M=runs/timing_v50/rtl_v50/lane16
I=pynq_specialized_code/pl/rtl_hand/_worktrees/v50_impB2
for f in bin_band_buf.v bin_band_fill_v50.v bin_catpack_v50.v bin_conv_core_v50.v \
         bin_interp_v50.v bin_rprelu_v50.v bin_sam_v50.v bin_segq.v \
         bin_unit_axi_wrap_bd_v50.v bin_unit_axi_wrap_v50.v bin_band_seq.v; do
  printf "%-28s main=%s impB2=%s %s\n" "$f" \
    "$([ -f $M/$f ] && wc -l < $M/$f || echo -)" \
    "$([ -f $I/$f ] && wc -l < $I/$f || echo -)" \
    "$( { [ -f $M/$f ] && [ -f $I/$f ] && diff -q $M/$f $I/$f >/dev/null && echo SAME; } || echo DIFF)"
done

# 现役位流参数（证明 FUSE_EN=0）
grep -n "FUSE_EN\|BAND_ROWS\|BAND_C_MAX" pynq_specialized_code/pl/scripts/bd_v50.tcl | sed -n '1,6p'
grep -n "打包层参数全对" runs/v53_archive/build_t107.log runs/v54_archive/build_v54.log
```

**关键 md5**
```
主线 lane16/bin_unit_axi_wrap_v50.v  17164dfc3029ace1cbcb9eaa08af6213  (3518 行)
主线 lane16/bin_segq.v               266454b87d8b7e4124d1fb1323d78178  (486 行)
主线 lane16/bin_band_buf.v           f966667d52c0c48f9d1e1a29b5602c06  (389 行)
impB2 bin_band_seq.v                 9699ee2d73b1741ae0049b27c6b2f5b6  (261 行)
impB2 bin_unit_axi_wrap_v50.v        b9d0b389f686272db20408a2ad9ba2d7  (3478 行)
T-99 存档合并器                      26d58123bb9ff2a3b79cae7fcec339ab  (未进主线)
```

---

## 7. 一句话总结

$$\Rightarrow\ \text{融合的}\textbf{机械部分}（fill/band/segq/drain）\text{在主线里；}\textbf{收益部分}（级联 + pack 带内口）\text{只在 impB2，且 impB2 的写通路是两处已修 bug 的旧版。}$$
$$\Rightarrow\ \text{四个阻断里 } \textbf{B1 是假警报}（\text{对排空不成立}）、\textbf{B2 比记录的更复杂}（\text{缺 y0 **和** 组间空档}）、\textbf{B3 是收益的唯一来源}、\textbf{B4 是"少第二个行参数"而非"一个数双用"}。$$
$$\Rightarrow\ \textbf{另外新查出两条更靠前的}：\textbf{B5}\ LOAD{\to}RUN\ \text{换带必报 } E\_CANARY（\text{主线一个带都跑不完，1 行可修}）；\textbf{E1}\ \text{现役位流 } \mathtt{FUSE\_EN}=0/\mathtt{BAND\_C\_MAX}=8\text{，融合在硬件上根本跑不起来}；\text{而 PS 的能力探针看不出这一点（}magic\ \text{照旧返回）。}$$
$$\Rightarrow\ \Rightarrow\ \textbf{估值}：\text{把 } \mathtt{B5{+}B2{+}B3{+}E2{+}E1}\ \text{做完} \approx \mathbf{4\sim7\ 人日}，\text{换 } \mathtt{PL}\ 3.06\to\approx1.7\sim2.2\ \text{s @150 MHz}；\text{而不是 } 1\ \text{s}。$$
