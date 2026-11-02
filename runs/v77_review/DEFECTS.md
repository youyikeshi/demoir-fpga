# v77 REVIEW 缺陷清单（2026-11-02 15:45）

> 口径：**只列可复算的问题**（文件 + 函数 + 行号 + 现象 + 建议改法）。
> 行号 = 本轮**合并后**（main）的当前行号；RTL 行号 = 交付文件行号。
> 严重度：🔴 阻断（构建挂 / 数值错）/ 🟠 需在 IMP-B 交付前拍板 / 🟡 记账/口径。
> **本清单不是授权我改别人的设计**：D6 是唯一由我直接修的一条（1 行 print 格式），
> 其余一律留给新代理；IMP-A / IMP-C 已收工，不给它们派活。

## 一览（16 条）

| # | 严重度 | 归属 | 一句话 |
|---|---|---|---|
| D1 | 🔴 | IMP-C | v50 寄存器仍在 `0x200` 段（契约 R1 要求 `0x300`），`CHAIN_STATUS 0x310` 根本没实现 |
| D2 | 🔴 | IMP-C | 45/432 条描述符是"幽灵带"（`band_y0 = h_out`），根因是 `bands` 公式把 halo 加进分子 |
| D3 | 🔴 | IMP-C | 运行时把带输出基址按 **8 B/px** 折算且与 `band_y0` **双重偏移**；与离线链不一致 |
| D4 | 🔴 | IMP-A | lane16 核仍叫 `module bin_conv_core`，与基线薄壳**重名** ⇒ 构建第 2 步硬 error |
| D5 | 🔴 | IMP-B/D | `bd_v50.tcl`/build 断言 `bin_unit_axi_wrap_lane16`，文档 §9 却叫 `bin_unit_axi_wrap_bd_v50` |
| D6 | ✅ 已修 | IMP-C | `BIN_V50_FUSE=1` 在 main 上必崩（`"%d" % (a,b)` TypeError），已修 1 行 print |
| D7 | 🟠 | IMP-C | 契约 B 的 `flags.bit3`（输出 f32）从不置位；`bit1`（物化）恒置位 |
| D8 | 🟠 | IMP-C | "每条描述符占 4 KB、`next_pa` 相隔 4096"只是实现选择，契约 B 没写死 |
| D9 | 🟠 | IMP-C | `BIN_V50_CHAIN_RTL=1` 探针读回声即可骗过（v39 的 `0x208` 就是可读回寄存器） |
| D10 | 🟠 | IMP-C | PS 硬要求 `cout % VLANES == 0`（V>8）；F2 修好后**仍须保留**（PRM 是另一回事） |
| D11 | 🟠 | IMP-C | 4 段 `c_in/c_out = 3` 直接当 `cfg_c` 下传，C8 组掩码责任未定义 |
| D12 | 🟠 | IMP-B | `wr_accept_ok = u_ce \|\| wr_xfer`：冻结期仍吞字节，需"冻结+同时写"仿真覆盖 |
| D13 | 🟡 | IMP-C | 段预算按 int8（1 B/elem）算，arena 却按 4 B/elem 分配；`segments.json:buf_bytes` 是 f32 |
| D14 | 🟡 | IMP-C | `bands` 只按"多一条描述符"记账，报告里仍写 432 条合法 ⇒ D2 判据只看结构不看语义 |
| D15 | 🟡 | 流程 | IMP-C 的 D2 证据是在 `segments.json` **不存在**的目录里跑的（所以没暴露 D6） |
| D16 | 🟡 | IMP-C | `_v50_stats`/错误信息里 `0x200`/`0x208` 的文案与契约 R1 不符（修 D1 时一并改） |

---

## D1 🔴 v50 寄存器没搬家：仍是 `0x200`，且缺 `CHAIN_STATUS`

- 文件：`pynq_specialized_code/ps/pynq/bin_engine.py`
- 行：`2271-2272`

```python
V50_CHAIN_REG = dict(CHAIN_HEAD=0x200, PROGRESS=0x204,
                     BAND_ROWS=0x208, FUSE_MODE=0x20C)
```

- 现象 1：契约 R1（`docs/V50_ARCHITECTURE.md` §10.1）已把 v50 新寄存器**整体搬到
  `0x300/304/308/30C`**，并要求 `0x000..0x1FF` 与 **`0x200..0x20C` 全部保持原样**
  （`0x200..0x20C` 是 E1E2 的 `RUN_UNIT/E_GROUP/E_DESC/E_DESC_HI`）。
  合并进来的 PS 侧**一个字节都没搬**，等于把 E1E2 的寄存器块又改了一次。
- 现象 2：`0x310 CHAIN_STATUS`（bit0 done / bit1 err / bit2 busy / bit8~15 err_code）
  **完全没用**；`v50_chain_ready/submit/poll` 全是靠 `AP_RET(0x94)` + `PROGRESS`
  拼出来的（`bin_engine.py:1836-1842, 1908, 1920-1946`）。
- 现象 3（连带的可用性损失）：为了不踩 `0x200`，`enable_e1e2()` 在
  `BIN_V50_FUSE=1` 时**直接拒绝挂 E1E2**（`bin_engine.py:2149-2153`），
  `E1E2Engine.select()` 也抛（`2763-2769`）、`_run_v50` 里连 `select(0)` 都跳过
  （`3070-3073`）。⇒ 融合跑起来时**PS 侧打包/量化的加速全没了**（D2 实测
  `prologue 0.00 / epilogue 0.00` 就是证据），v50 的性能卖点被这一处抵消。
- 建议改法（新代理，IMP-C 线）：
  1. `V50_CHAIN_REG = dict(CHAIN_HEAD=0x300, PROGRESS=0x304, BAND_ROWS=0x308,
     FUSE_MODE=0x30C)`，新增 `CHAIN_STATUS=0x310`；轮询改读 `0x310`（err_code 也在这儿），
     `AP_RET` 只保留"老路径"用途；
  2. 删掉 `enable_e1e2`/`select`/`_run_v50` 里那三处"0x200 互斥"护栏（搬走后它们
     自动失效，没有存在意义），并允许 FUSE=1 时正常挂 E1E2；
  3. 文案同步（见 D16）。

## D2 🔴 45 条"幽灵带"：`bands` 把 halo 加进分子

- 文件：`pynq_specialized_code/ps/pynq/bin_net.py`
- 行：`1504`（计算）、`1314-1315`（离线建链）、`2479/2486`（运行时建链）

```python
1504:  bands = max(1, (h_out + halo + band_rows - 1) // band_rows)
1314:  y0 = min(b, int(s.bands) - 1) * self.band_rows
1315:  ch = min(self.band_rows + int(s.halo_rows or 0), int(s.h_out or self.band_rows))
2479:  y0 = min(b, int(seg.bands) - 1) * int(seg.band_rows)
2486:  cfg_h = int(seg.band_rows + (seg.halo_rows or 0))      # 运行时连 min 都没有
```

- 现象（实测，`--hw 64`，`BIN_V50_FUSE=1 BIN_V50_DUMP=1`）：
  带 halo=2、`h_out=32`、`band_rows=8` 的段算出 `bands=5`（应为 4）；
  第 5 条描述符 `band_y0 = 32 = h_out`，即**输出张量之外**还写一带、读 10 行输入。
  证据（`D2_fuse1_hw64.log`，S1）：

```
-- S1 conv/cat  bands=5 halo=2 链段 [#4..#8)
[#004] ... 00000000 00000002 ...   y0=0
[#005] ... 00000008 00000002 ...   y0=8
[#006] ... 00000010 00000002 ...   y0=16
[#007] ... 00000018 00000002 ...   y0=24
[#008] ... 00000020 00000003 ...   y0=32  ← 幽灵带
```

- 规模：223 段里 **45 段**受影响，链是 432 条而不是 387 条（`432 - 45 = 387`）。
- 建议改法：`bands = ceil(h_out / band_rows)`（不要加 halo）；末带 `cfg_h =
  min(band_rows + halo, h_out - y0 + halo)`；**离线 `build_chain` 与运行时
  `write_segment_chain` 必须共用同一个构造函数**（现在两份重复实现，运行时段连
  `min` 都没有 —— 这是它能同时逃过 D2 结构判据和 IMP-C 自查的原因）。

## D3 🔴 运行时带输出基址：8 B/px 写死 + 双重偏移

- 文件：`pynq_specialized_code/ps/pynq/bin_net.py`
- 行：`2476`、`2484`

```python
2476:  row_bytes = int(seg.w_out) * 8        # 注释："C8：一行 8 通道组 × W"
2484:  out_pa=out_pa + y0 * row_bytes,        # 同时又把 band_y0 传给 RTL
```

- 现象 1：一行真实字节数 = `w_out * c_out`（契约 C：int8，1 B/通道）。
  写死 `*8` 只在 `c_out=8` 时成立；本网 `c_out ∈ {32,48,64,96,192}` ⇒ 带间地址
  **互相覆盖**（例：`c_out=48` 时第 b 带的偏移只有真值的 1/6）。
- 现象 2：`out_pa` 已经加了 `y0*row_bytes`，描述符里**又**写 `band_y0=y0`。
  若 RTL 按 `band_y0` 自行折算，就变成**双重偏移**；若 RTL 不用 `band_y0`，
  那 D2 契约里的 `band_y0` 就是死字。**两种解释都错**，必须二选一。
- 现象 3：离线 `build_chain` 用的是**未偏移**的 `out_pa`（`1326`）⇒ 离线证据和
  运行时提交的链**不是同一份**（D2 的"合法链"打印来自离线版）。
- 建议改法：`row_bytes = w_out * c_out`；`out_pa` 传**段基址**、偏移只由
  `band_y0` 表达（与离线版一致），并在契约 B 里写明"RTL 由 `band_y0` 折算带基址"。

## D4 🔴 lane16 核与基线模块重名（构建第 2 步必挂）

- 文件：`runs/timing_v50/rtl_v50/lane16/bin_conv_core.v:52`（IMP-A 修后版，md5 `4e6a4235`）
- 现象：lane16 里定义的是 `module bin_conv_core`，而基线
  `runs/timing_v50/rtl_v50/bin_conv_core.v`（v39 薄壳，md5 `acaa8540`）**也叫这个名**。
  `pynq_specialized_code/pl/scripts/build_final_150v50.tcl:193` 会直接：

```
error "lane16/bin_conv_core.v 重定义了基线模块 'bin_conv_core'（已在 bin_conv_core.v 里）—— 新 RTL 必须换新模块名"
```

- 补充：`docs/V50_ARCHITECTURE.md` §9 的表格**自己就矛盾**（"conv 核 =
  `bin_conv_core.v` / `bin_conv_core`" 与"所有新模块名不得与 25 个既有模块重名"同一页）。
- 建议改法：核改名 `bin_conv_core_lane16`（文件可同改或保持），并同步改 §9 表格第三行
  与未来外壳里的例化名；**这一处必须由 RTL 侧改，不能靠构建脚本放行**
  （放行 = 回到 R1 要防的"Vivado 静默择一"）。

## D5 🔴 外壳命名：脚本要 `_lane16`，文档 §9 要 `_bd_v50`

- 文件：`pynq_specialized_code/pl/scripts/bd_v50.tcl:33-36`、
  `build_final_150v50.tcl:205`、`docs/V50_ARCHITECTURE.md:174-190`
- 现象：脚本**硬断言**模块 `bin_unit_axi_wrap_lane16`（BD 的 module-ref 也指向它），
  而 §9（15:35 补的）定的是 `bin_unit_axi_wrap_v50` / `bin_unit_axi_wrap_bd_v50`。
  IMP-B 的 worktree（`_worktrees/v50_impB/`）已经按 §9 命名交付
  （`module bin_unit_axi_wrap_bd_v50`）⇒ **直接放进去构建会找不到模块**。
- 建议：二选一并改另一边 —— 要么 `bd_v50.tcl`/`build_final_150v50.tcl` 把
  module-ref 与断言改成 `bin_unit_axi_wrap_bd_v50`，要么 IMP-B 改名（不建议，
  它已按 §9 交付且 §9 有日期更晚）。

## D6 ✅ 已由 REVIEW 修（1 行）：`BIN_V50_FUSE=1` 在 main 上必崩

- 文件：`pynq_specialized_code/ps/pynq/bin_net.py`，原 `2616`（现 `2616-2627`）
- 现象：`segmap_n` 是 `(map段数, 本实现段数)` **二元组**，这行只写一个 `%d` ⇒
  只要 `segments.json` 存在（= main 上恒定成立）就抛
  `TypeError: not all arguments converted during string formatting`，
  `BIN_V50_FUSE=1` 在 `_v50_setup` 里当场 rc=1。**D2 在我合并前根本跑不起来。**
- 修法：占位改成 `%d/%d`（与同文件 `1571` 行的措辞一致），并加注释说明为什么
  IMP-C 的证据没暴露它（见 D15）。补丁：`runs/v77_review/PATCH_bin_net_print.diff`。
- 影响：只动 print 格式，**不动任何算法**；修后 `merge_gate` 三关仍全绿，
  D1 归一化 stdout 仍 0 差异。

## D7 🟠 `flags` bit3 从不置位、bit1 恒置位

- 文件：`bin_net.py:1317-1328`（离线）、`2480-2488`（运行时）
- 现象：契约 R1（§10.2）定义 `bit3 = 输出 f32`、`bit1 = 本带需物化`。
  实现里只见过 `0x1`（末带）、`0x2`（物化）、`0x4`（shuffle）；
  `0x8` **一次都没出现**，而 `0x2` 段段恒置 ⇒ 两个位都退化成常量。
  本网最后一层输出是 f32（3×64×64），若 RTL 真按 bit3 决定要不要反量化，
  这条链就缺一个语义。
- 建议：要么把"段输出 f32"的判定实现出来（例如"段输出是网络输出张量"），
  要么从契约 B 删掉 bit3（现在这种"定义了但永不为 1"最危险：RTL 作者会以为它有用）。

## D8 🟠 4 KB 步进没进契约

- 文件：`bin_engine.py:2270`（`V50_DESC_STRIDE = 4096`）、`2403-2419`
- 现象：契约 B 写"地址必须 4 KB 对齐"，IMP-C 解释成"**每条**描述符占 4 KB、
  `next_pa` 相隔 4096"（432 条 = 1.77 MB）。另一种合理解释是"40 B 紧凑排布、
  只要求链首对齐"。IMP-B 的 `bin_segq` 若按紧凑步进实现，链就走歪。
- 建议：把"描述符步进 = 4 KB"写进契约 B（并注明"只为对齐省事，不省带宽"），
  同时在 `build_final_150v50.tcl` 加一条自检；否则改 PS 用 40 B 紧凑 + 链首对齐。

## D9 🟠 `BIN_V50_CHAIN_RTL=1` 的探针会被"寄存器回声"骗过

- 文件：`bin_engine.py:1819-1846`（`v50_chain_ready`）
- 现象：探针只做"写 `BAND_ROWS`=8 → 读回 8"。而 v39 位流里
  `0x208` 就是 E1E2 的 `r_edesc`，**写什么读回什么**：
  `runs/timing_v50/rtl_v50/bin_unit_axi_wrap.v:900`（写）、`:2563`（读）。
  ⇒ 旧位流也能"探针通过"。`D2b_chainrtl_hw64.log` 在 FakePL（纯回声模型）上
  已经复现了这条假通过（打印 `链模式就位：223 段 / 432 条`）。
  之后 PS 会写 `0x200 = 链首`（v39 的 `run_unit`）⇒ 数值错/超时/整段回落，
  最坏是 E1E2 拿一个非法描述符地址去跑。
- 建议（机制，不靠人记）：RTL 侧加**只读 magic**，例如 `0x314 CHAIN_ID` 恒
  `0x7635_5000`（或 `0x300` 段任意只读字），PS 要求读回相等才认链；
  再加两条运行期自证：提交后 `PROGRESS` 必须单调增、`CHAIN_STATUS.err=0`。
  在 RTL 到位前，`BIN_V50_CHAIN_RTL=1` 应该**只允许在 xsim/FakePL** 打开
  （文档里已承认它是"人工开关"，但这不该是唯一防线）。

## D10 🟠（F2 相关）PS 硬要求 `cout % VLANES == 0` —— 应保留，但要写清为什么

- 文件：`bin_engine.py:279-287`（`BinLayer._validate`）、`2377-2390`（`check_prm_capacity`）
- 现象/结论：IMP-C 在 `VLANES>8` 时对 `cout % VLANES != 0` **直接抛错**。
  SIM 刚证明 RTL 的 F2（末组 `nvalid`）修好后，"非整组"在**写掩码**上是安全的；
  但 PRM 的 5 个参数数组在 blob 里是**逐 oc 连续**的，非整组时最后一组会读到
  相邻数组（alpha 尾巴读成 bias）⇒ **F2 不解除这条约束**。
  当前网络 `cout ∈ {32,48,64,96,192}` 全是 16 的倍数 ⇒ 现状无影响。
- 建议：保留硬校验（这是对的），只在契约 §10.3 补一句"F2 只修写掩码，不修 PRM
  装载；`cout % VLANES == 0` 仍是 16-lane 上板前提"。**别让下一个人以为 F2 修完
  就可以放开。**

## D11 🟠 `cfg_c` 直接下传 `c_in`，4 段是 3 通道

- 文件：`bin_net.py:1327`（离线 `cfg_c=int(s.c_in)`）、`2485`（运行时 `cfg_c=int(seg.c_in)`）
- 现象：实测有 4 段 `c_in` 或 `c_out = 3`（S0 输入 3ch；S147/S185/S222 输出 3ch）。
  契约 C 只写"1 字节 = 1 通道"，**没写** `cfg_c` 非 8 倍数时组内剩余 lane 由谁屏蔽。
  v39 的核有 `nvalid`/`cin` 部分组语义，但 v50 的 band 通路要不要整除 8 没有契约。
- 建议：在契约 B/C 加一句"`cfg_c` = 段输入通道数原值；`c%8 != 0` 时掩码由 RTL
  按剩余 lane 生成，PS 不做 padding"，并要求 IMP-B 的外壳照此实现。
  （这条与 F2 是同一类错误的两个侧面：**非整组必须有人负责掩码**。）

## D12 🟠 IMP-B（未合并，仅静态核）：`wr_accept_ok = u_ce || wr_xfer`

- 文件：`_worktrees/v50_impB/bin_unit_axi_wrap_v50.v:1961`（v50_impB2 同）
- 现象：`wr_xfer` 让写引擎在 `u_ce=0`（停核）时**仍然吞字节**，注释理由是
  "16-lane 2 拍串行化不能被停核一起停住、否则死锁"。这是有意的例外，
  但它直接与"冻结必须冻结所有派生态"的规矩冲突（本项目 §5.3 那一课）。
  现在的正确性论证是"被挡就等，不会丢字节"，属于**论证级**保证，不是机制级。
- 建议（给 IMP-B 的复核项，不是我要改）：SIM 必须补一组"**冻结 + 同时写**"
  的组合测试（只测外部拉低 ce 永远测不出来），以及"上游限速 N 拍 1 beat"梯度；
  并断言 `bf_cnt` 永不越过 `BF_PRESS`（`wr_fifo_press` 已有，但要有覆盖）。

## D13 🟡 预算口径：段缓冲按 int8，arena 按 4 B/元素

- 文件：`bin_net.py:1506-1507`（`band_bytes = 2*band_rows*w_out*c_out`，int8） vs
  `bin_net.py:1555` 附近 `plan._alloc_pa(c*h*w*4)`（4 B/元素）
- 现象：两个数字都叫"字节"，一个是契约 C/D4 的 int8 带宽，一个是 arena 的 f32 预留；
  270 KB 预算判的是前者。数值不矛盾（后者只是多留），但**同名不同义**，
  正是"名字带 `_bytes` 就必须是字节"那条纪律要防的形态。
- 另：`runs/m56_segments/segments.json` 的 `buf_bytes` 是 **f32（4 B/元素）**，
  与它自己的文档（×1 B）矛盾（223/223 段实测都是 4×）。
- 建议：`band_bytes` → `band_bytes_i8`（或加 `_i8` 后缀），arena 侧改 `arena_bytes_f32`；
  `segments.json` 加 `"unit": "f32"` 字段。

## D14 🟡 报告把"结构合法"写成"链合法"

- 文件：`bin_net.py:1297-1299`（`SegmentPlan.report`）
- 现象：`描述符链：432 条 × 40 B @ 4 KB 步进 ...` 只统计条数；`v50_format_chain`
  只检查 `next_pa` 对齐与链尾 0。**没有一条判据检查 `band_y0 < h_out`、
  `cfg_h + y0 <= h_in`、`out_pa + 带字节 <= 张量字节`** ⇒ D2 的"合法链"打印
  在 D2/D3 两个 🔴 存在的情况下依然是绿的。
- 建议：把这三条加进 `build_chain` 的断言（违规就 `raise`），并让 D2 判据从
  "打印出链"升级成"链的几何自检 0 违规"。

## D15 🟡 证据环境缺口（流程）

- 现象：IMP-C 的 D2 证据是在 `worktrees/v50_impC/` 里跑的；该目录的同级
  `worktrees/runs/m56_segments/` **不存在** ⇒ `_load_segmap_hint()` 返回 None
  ⇒ `segmap_ok=None` ⇒ `bin_net.py:2616` 那行**从未执行** ⇒ D6 的崩溃与
  "223/223 一致"这句话在它的证据里都不存在。
- 建议：验收命令必须**在仓库根**跑（或显式 `BIN_V50_SEGMAP=` 指到
  `runs/m56_segments/segments.json`），并把"segmap 命中（非 None）"写进 D2 判据。

## D16 🟡 文案与契约 R1 不符

- 文件：`bin_engine.py:1839`（`"0x208 不通"`）、`2145-2152`、`2246-2253`、
  `2763-2769`；`bin_net.py` 侧只引用常量，无字面量
- 现象：搬迁后这些字面量全部指向错地址，会把人引到错的寄存器上排查。
- 建议：修 D1 时把文案一起改成 `0x308` 等（或统一改成 `V50_CHAIN_REG["BAND_ROWS"]`
  的格式化，别再写死）。

---

## 附：与 SIM 两条 RTL bug 的关系（我已复核，**不再重复计入条数**）

| SIM 编号 | 位置 | 我的复核 |
|---|---|---|
| F1 | 原 `bin_conv_core.v:695` `hwout8_q <= (hout*wout) << 3` | 确认：同段 `gstride_q` 已经是 `(K<<3)*VLANES`，只有组平面步长漏了 `*VLANES`；修后版改成 `plane_stride_q = Hout*Wout*VLANES`（`_worktrees/v50_impA_fix/bin_conv_core.v:186/693`）✅ |
| F2 | 原 `:757-758` `nvalid_q <= cout_q - o_q` | 确认：`o_q` 上面是非阻塞赋值，读到旧值；条件行用的却是 `o_q+VLANES`（两行不一致）。修后版改成 `cout-(o_q+VLANES)` 并在 `<0` 时夹 0 ✅ |
| F2 的连带 | 冻结 sha1 | 当前网络 `cout` 全为 8/16 倍数 ⇒ F1/F2 修后**逐字节不变**；`verify_lazy_net` 的冻结常量（64² = `104710f6…`、256² = `392d1ae0…`）不受影响（FakePL 走 numpy `conv_rtl`，不经 RTL）。SIM 的隔离实验已给"B vs C 只有 21 条不同且全在 `cout%VLANES≠0`"的证据，我这份 D1 复核与它自洽 ✅ |
