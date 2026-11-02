# v50 · IMP-B（RTL 数据流）交付报告

> 工作目录 `pynq_specialized_code/pl/rtl_hand/_worktrees/v50_impB/`
> 契约：`docs/V50_ARCHITECTURE.md` §2 D1~D6 / §3 契约 A~D / §5 ⑨⑩⑪ / §9 命名
> 基线：`runs/v42e_archive/rtl_v39_exact/`（只读参考，未改一个字节）
> 日志：`runs/v50_impB/`

---

## 0. 交付清单（**8 个文件 / 8 个模块名，照 §9 逐字**）

| 层 | 文件 | module | 行数 | 说明 |
|---|---|---|---:|---|
| 单元 | `bin_unit_axi_wrap_v50.v` | `bin_unit_axi_wrap_v50` | 3168 | 从 v39 2785 行派生，**只增不改**（见 §2）|
| BD 外壳 | `bin_unit_axi_wrap_bd_v50.v` | `bin_unit_axi_wrap_bd_v50` | 170 | FREQ_HZ **150000000**；端口与 v39 外壳逐字相同 |
| band 缓冲 | `bin_band_buf.v` | `bin_band_buf` | 369 | 双带 + halo + 1R1W 仲裁 + 金丝雀 + 容量 `$fatal` |
| 描述符链 | `bin_segq.v` | `bin_segq` | 483 | 链取指 / progress / 出错即停 / 链尾 done / 重启 |
| 小算子 | `bin_sam_v50.v` | `bin_sam_v50` | 85 | 旧口保留 + band 原地口（op_en 选）|
| 小算子 | `bin_rprelu_v50.v` | `bin_rprelu_v50` | 86 | 同上 |
| 小算子 | `bin_interp_v50.v` | `bin_interp_v50` | 82 | 同上 |
| 小算子 | `bin_catpack_v50.v` | `bin_catpack_v50` | 92 | 同上（64-bit 整字写口）|

生成脚本与自测：
- `tools/patch_make_v50_wrap.py`（14 处定点替换，每处断言"恰好命中一次"，不命中直接失败）
- `tools/v50_fuse_block.vh`（融合通路正文，228 行）
- `tools/gen_smallop_v50.py`
- `tb/tb_bin_segq.v`（B3 定向自测，21 条检查）

模块名与 `runs/timing_v50/rtl_v50/` 的 26 个既有模块**零冲突**（逐个比对过）。

---

## 1. 寄存器映射（契约 A + 修订 R1）

```
0x000..0x1FF   v39 原样，一个字节没动
0x200..0x24C   v39 的 E1E2 / catpack / DBG 段，一个字节没动（18 条 case 项逐字保留）
0x250..0x25C   空（契约禁放，仍然空）
0x300 CHAIN_HEAD     RW  描述符链首物理地址（4 KB 对齐）
0x304 PROGRESS       RO  已完成描述符数
0x308 BAND_ROWS      RW  行带高度（复位 = 参数 BAND_ROWS）
0x30C FUSE_MODE      RW  bit0：0 = 旧路径（复位值）/ 1 = 段融合
0x310 CHAIN_STATUS   RO  [0] done [1] err [2] busy [15:8] err_code
0x318 ARENA_LO       RW  segq 越界判据下界（本文件新增，R1 未列）
0x31C ARENA_HI       RW  上界
```
`0x300..0x31C` 由参数 `V50_REG_BASE`（默认 `0x300`）给出，改一行即可整体搬家。
`0x314` 留空未用。

---

## 2. B1：`FUSE_MODE=0` 与 v39 的逐行对照（**只增不改**）

v39 2785 行 → v50 3168 行：**新增 427 行**，**被替换的 v39 行只有 44 行**（14 处）。
完整 diff：`runs/v50_impB/B1_diff_v39_vs_v50_wrap.txt`；逐处说明：`B1_equivalence_summary.txt`。

| v39 行 | 改动 | 为什么在 FUSE_MODE=0 时逐位不变 |
|---|---|---|
| 299 | 模块改名 | §9 命名契约 |
| 358 | 参数表加 `,` + 追加 9 个参数 | 全部有默认值，不传参 = v39 |
| 929/935 | 启动闸门加 `!fuse_cfg_bad`；错误码加三目 | `r_fuse_mode[0]=0 ⇒ fuse_cfg_bad≡0`；三目在 `vcode_q!=0` 时取原值 |
| 1219 | `c_wr_data/sel` 位宽 → `VLANES*8` / `VLANES` | VLANES=8 ⇒ 64/8，与原等宽 |
| 1229/1243 | 核的 cfg/base 接 `c_*` mux | `fuse_m = sel_fuse_q && …`；`sel_fuse_q≡0` ⇒ 右支 = `r_*` |
| 1234 | 核例化加 `.VLANES(VLANES)`、实例名 `u_core` | 端口名一个没改（C-B1）；`.LANES(LANES)` 仍传 popcount 位宽 |
| 1264/1291 | 核 / E1E2 的 `.mem_rd_data` 接 mux | `fuse_m≡0` ⇒ = `u_rd_data` |
| 1327/1337 | `u_*` → `u_*_i`（原路），写口接 `cw_beat_*` | VLANES≤8 时 `cw_beat_*` 是 `c_wr_*` 的**纯 assign**（同拍同值）|
| 1594 | `wr_wide_b/wr_byte` 的 `u_ce` → `wr_accept_ok` | `wr_xfer=(VLANES>8)&&…≡0` ⇒ `wr_accept_ok≡u_ce` |
| 1620 | `u_ce` 增加 `!wr_split_busy` | VLANES≤8 时 `wr_split_busy_i≡0` |

⇒ **结论**：`FUSE_MODE=0 + VLANES=8` 时，14 处替换的右支与被替换的 v39 表达式逐位相同；
0x000..0x24C 的寄存器语义、`win_q`/AR/预取参数（1/4/4）、写引擎、13 级校验链全部原样。
**注意**：这是"RTL 结构等价 + elaboration 通过"级别的证明；**逐字节仿真回归属 V1（SIM 代理）**，
我不越权宣称。

---

## 3. ② `bin_band_buf.v`

- **布局**：`band[grp][r][x][c%8]`，`字节 = grp*(rows*W*8) + r*(W*8) + x*8 + (c%8)`。
  同一个 64-bit 字 = 同一像素的 8 个通道 ⇒ conv 核的共享读（D2）**零改动**可用。
  ⚠ 契约 C 的 "band[r][c][x] 行主序、通道最内" 若按字面读成 x 最内，会把同像素 8 通道
  拆到 8 个字里 ⇒ 与 D2 冲突。这里取唯一自洽解（通道最内、列次之），措辞歧义登记在 §8。
- **双带宽高**：`ROWS_ALLOC = BAND_ROWS + 2*HALO_ROWS`，两只带 + 每带 1 个金丝雀字。
- **五个端口 / 物理 1R1W**：A 单元读、B 单元写、C pack 读、D 小算子原地读写、E DMA 装载/排空。
  读优先级 `E>C>A>D`，写优先级 `E>B>D`；优先级成立的依据是相位表（LOAD/RUN/DRAIN 互斥），
  真撞了置 `arb_conflict` 粘滞位 + 仿真期 `$error`（不静默）。
- **不变量**：⑩ 带选择由上层按相位给，本模块做硬件兜底（换带那一拍还在写 ⇒ `arb_conflict`）；
  ⑪ `buf_reset` 回确定态（清全部粘滞位 + 重写金丝雀）；⑤ `wr_all_idle` = 两拍无写。
- **容量参数化与 `$fatal`**（B2 已实测，见 §5）：
  `CAP = 2*(NGRP*(BAND_ROWS+2*HALO)*W_MAX + 1)*8`；
  超 `BUDGET_KB` ⇒ **elaboration 期**报错（缺模块名
  `bin_band_buf_CAPACITY_OVER_BUDGET_reduce_BAND_ROWS_or_C_MAX_or_W_MAX` + `initial/$fatal` 双保险）。
  默认 `BAND_ROWS=8 HALO=1 C_MAX=48 W_MAX=256` ⇒ **240 KB ≤ 270 KB**。
  若按 D4 取 halo=2 则 288 KB > 270 ⇒ 会被断言拦下（**必须**改参数，不许静默截断）。

---

## 4. ③ `bin_segq.v` —— 四条路径（B3，已仿真通过）

| 路径 | 实现 | 自测结果 |
|---|---|---|
| (a) 推进 | IDLE→HDR(5 拍取 10 字)→CHK→LOAD→RUN→DRAIN→NEXT→HDR…，`progress` 每完成一条 +1 | 2 条链 ⇒ `progress==2` ✅ |
| (b) 链尾 | NEXT 见 `next_pa==0` ⇒ TAIL；TAIL 里**等 `wr_all_idle`** 才置 done | `wr_all_idle=0` 时 done 恒 0、progress 已到 1；放开后 done 才 1 ✅ |
| (c) 出错即停 | 4KB 未对齐 / 越 arena / nband=0 / op_mask 不支持 / 基址 0 / 金丝雀 / 超时 ⇒ ERR：锁 err_code、拉一拍 `band_abort`、**不再发任何请求**、同时置 done 让 PS 轮询能退出 | E_ALIGN(1) / E_RANGE(2) / E_CANARY(4) 全部命中 ✅ |
| (d) 重启 | `kick` 在任意状态有效 ⇒ 清 err/done、progress 归零、从 `chain_head` 重来；`chain_head==0` 视为空链直接 TAIL | err→kick→`err=0, progress=1, done=1` ✅ |

自测日志：`runs/v50_impB/B3_segq_fsm_sim.log`（21 条检查，0 error）。
`en` 就是包装层的 `u_ce`；请求 1 拍、数据 1 拍，与四个单元的 BRAM 语义同一时间轴。

---

## 5. ④ 融合控制（`bin_unit_axi_wrap_v50` §10）

- `op_mask`（描述符字 9 高 16 位）决定该带做哪些算子；本版实现 **bit0 = conv**，
  `OP_MASK_SUP=16'h0001`，其余位 ⇒ `E_OPMASK` 报错（不静默）。
- 数据流：`LOAD(DDR→band0) → RUN(核读 band0、写 band1) → DRAIN(band1→DDR)`，
  带选择由 segq 状态给（`bank_rd_sel/bank_wr_sel`），`seg_band_done = c_done && wr_all_idle && !wr_split_busy`。
- `FUSE_MODE=0`：`sel_fuse_q=0` ⇒ band/segq 全静默，全部 mux 走 v39 原路。

---

## 6. ⑤ 小算子 v50 版（R6）

四个壳的改法**只有两种**：① 旧端口一个不改；② 新增 band 原地口（`op_en=1` 时把原模块
的 `mem_*` 直接接到 band 上）。band 与 DDR 地址语义同构（1 字节 = 1 通道、C8 交错），
所以**原模块就是"在 band 上原地做"的那台机器**，不需要重写任何 requant/插值/打包算术
（那才是字节一致性的风险源）。`op_en=0` ⇒ `mem_*` 原样外露 ⇒ 与 v39 逐位相同。
单字节写口用 `band_wr_sel = 1 << addr[2:0]`；catpack 是 64-bit 整字写口，直接落整字。

---

## 7. B4：elaboration（`runs/v50_impB/B4_elaboration.log`）

```
xvlog --sv  8 个新文件 + 依赖(IMP-A bin_conv_core + v39 的 e1e2/catpack/sam/rprelu/interp)  0 error
xelab  bin_unit_axi_wrap_bd_v50  VLANES=8     Built
xelab  bin_unit_axi_wrap_bd_v50  VLANES=16    Built
xelab  bin_band_buf  默认(240 KB ≤ 270 KB)    Built
xelab  bin_band_buf  BAND_ROWS=16 HALO=2      **ERROR: module ...CAPACITY_OVER_BUDGET... not found**（B2 ✅）
xelab  bin_band_buf  C_MAX=64   HALO=2        **同一断言**（B2 ✅）
xelab  bin_sam_v50 / bin_rprelu_v50 / bin_interp_v50 / bin_catpack_v50   全部 Built
```
未起 Vivado（只用了 xvlog/xelab，串行执行，一次一个工具）。

---

## 8. B5：未实现 / 待就位（**明确清单**）

1. **逐字节回归未跑**：`FUSE_MODE=0 ≡ v39` 目前是"结构等价 + elaboration"级别；
   `LANES=8` 回归与 16-lane 等价性属 V1（SIM 代理），需要 golden/数据集。
2. **16-lane 写侧** 用 **(b) 2 拍串行化**（IMP-A 建议）：核吐 128 位，包装层拆两拍，
   第二拍地址 = `addr + Hout*Wout*8`（DESIGN_16LANE §7.3 不变量①：两个 C8 plane，**不是** +8）。
   为此在写引擎的接受条件里加了 `wr_accept_ok = u_ce || wr_xfer` —— 否则"停核"会把串行化器
   一起停死（死锁）。`wr_xfer` 还加了 `!wr_fifo_press`，防止绕过压力线冲满 FIFO。
   ⚠ 这条改动对 VLANES=8 恒为 0，但**它是 v50 里唯一碰到写引擎接受条件的改动**，
   请 REVIEW 重点看，并在 V1 里用 VLANES=8 回归确认无差。
3. **band 通路的 `cfg_h` 语义**：融合时核的 `cfg_h` 直接取描述符的 `h`（= 带内行数，含 halo）。
   段边界的真实 padding 怎么叠（描述符 `pad` 与 halo 行的关系）**需要 IMP-A/IMP-C 一起定**，
   本版按"band 就是一张小图、pad 由描述符给"实现，未与 PS 对齐验证。
4. **op_mask 只实现 bit0（conv）**；pack（E1E2 读 band 的口已接好）与 sam/interp/rprelu 的
   **带内串联**（conv→pack→sam 依次在同一带上跑）未实现：需要 band 端口的时分复用仲裁
   + 一条算子序列 FSM。四个 `*_v50` 壳已就位（band 口 + op_en），接上即可。
5. **D 口（小算子原地读写）在包装层恒 0**：等第 4 条做完再接。
6. **catpack 在融合模式下不支持**（`fuse_cfg_bad` 会以 `ERR_UNIT` 拒绝启动）——
   它读 float32 多源、与 band 的 int8 语义不同构，属 R7"正交不动"。
7. **`bin_conv_core` 依赖**：本包装层按 IMP-A 的 `bin_conv_core #(.LANES,.COUT_MAX,.VLANES)`
   （端口名与 `bin_conv_core8` 逐字相同）例化。没有它的核文件时 `xvlog` 能过、`xelab` 不能过；
   `FUSE_MODE=0 + VLANES=8` 的路径也**同样需要**核就位（核是 conv 的实体）。
   `.VLANES(16)` 已按 IMP-A 的说明写对，**没有**用它顶替 `.LANES`。
8. **PRM 帧宽**：包装层不参与参数页，帧宽 `2*VLANES` 字由核的 `PRM_W/PB_*` 决定
   （IMP-A 已参数化），本文件既不硬编码 16 也不重算偏移。

---

## 9. 契约冲突与裁决记录（供 REVIEW / IMP-C 复核）

| # | 冲突 | 处理 |
|---|---|---|
| 1 | 契约 A 原写新寄存器在 `0x200..0x20C`，但 v39 已把 `R_RUN_UNIT/E_GRP/E_DESC/E_HI_BAD/E_OUT` 放在那里 | 已按 **R1 修订** 落在 `0x300..0x310`；`0x200..0x24C` **零改动**（18 条 case 项逐字保留，见 `B1_equivalence_summary.txt`）|
| 2 | 契约 C "band[r][c][x] 行主序、通道最内" 与 D2 共享读冲突 | 取"通道最内、列次之"（= C8 交错），已在 `bin_band_buf.v` 头注写明 |
| 3 | 描述符字 6/7 原留白 | 已按 **R1** 实现：字6 = `band_y0`（绝对行号），字7 = `flags`（bit0 末带 / bit1 需物化 / bit2 shuffle / bit3 输出 f32；**未定义位忽略**）|
| 4 | 权重/PRM 基址与 `shift` 组无处可放（字 6/7 被 R1 定死）| 融合模式下**仍走寄存器**（PS 在段边界写一次）⇒ 一条链一次提交仍然成立 |
| 5 | D4 默认 `BAND_ROWS=8` + halo=2 在 C=48/W=256 下需 288 KB > 270 KB 预算 | 默认取 `HALO=1`（240 KB）；要 halo=2 必须同时降 `BAND_ROWS` 或 `W/C`，否则 elaboration 直接失败 |
