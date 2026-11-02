# IMP-A（重派）交接：bin_conv_core8.v → bin_conv_core.v（8/16 lane 参数化）

产物：`pynq_specialized_code/pl/rtl_hand/_worktrees/v50_impA/bin_conv_core.v`
源：`runs/v42e_archive/rtl_v39_exact/bin_conv_core8.v`（896 行，md5 4e9887a9…）
结果：919 行（+23 行，其中 12 行是新增 localparam/if-chain，其余为注释）

## 0. 先决勘误（重要，和任务书假设不同）

1. 文件**本来就已经**是用 `VLANES` 参数化的（78 处引用），不是硬编码 8。
   所以"30 处 lane 行"里绝大部分已经是参数，真正**写死 8** 的只有 4 处（见 §2）。
2. `LANES` 这个名字**已被占用**：`parameter int LANES = 64`（= popcount 位宽），
   且 `bin_conv_core.v`(包装层) 第 33 行按名绑定 `.LANES(LANES)`。
   ⇒ 按任务书再写一个 `parameter int LANES = 8` 会**直接顶掉包装层的绑定**，
     包装层会得到 64 lane / 512-bit `mem_wr_data`，与它的 64-bit 端口冲突。
   ⇒ 处置：**lane 数继续用 `VLANES`**（默认 8，改成 16 即 16 lane），`LANES` 原样保留。
3. 任务书路径前缀 `R/` 是多余的；真实路径在仓库根（`runs/…`、`pynq_specialized_code/…`）。

## 2. 改动清单（新文件行号）

| 行 | 原文 | 新写法 | 为什么这个 8 是 lane 数 |
|---|---|---|---|
| 52 | `module bin_conv_core8 #(` | `module bin_conv_core #(` | 改名，端口列表未动 |
| 89-99 | 无 | `LVW/NVA/NVB/PRM_W/PRMW/PB_*` 派生常量 | lane 数决定的位宽/帧长/参数段基址 |
| 205 | `logic [4:0] prm_i;` | `logic [PRMW-1:0] prm_i;` | PRM 帧字数随 lane 数 16→32，5 bit 装不下 32 |
| 276-286 | `pw[0:15]` | `pw[0:PRM_W-1]` | 每 lane 5 个 epilogue 参数，8 lane = 16 字 |
| 288-310 | `case(i)` 硬编码 0/1,2-5,6-9,10-13,14-15 | 按 `PB_*` 阈值的 if-chain | 参数段边界 = NVA/NVB 的函数 |
| 320-324 | `pw[(k/4)]`、`pw[14+(k/4)]`、`pw[2+(k/2)]`… | `pw[PB_A+(k/4)]`、`pw[PB_S+(k/4)]`… | 基址 0/2/6/10/14 正是 8 lane 的段边界 |
| 334 | `prm_i < 5'd16` | `prm_i < PRM_W` | 16 = 8 lane 的帧字数 |
| 598 | `<< nvalid_q[2:0]` | `<< nvalid_q[LVW-1:0]` | 3 bit 只够 0..7 个 lane；16 lane 要 4 bit |
| 701 | `prm_i < 5'd16` | `prm_i < PRM_W` | 同上 |

**刻意没动的 8（与 lane 无关）**：`VLANES*8`（每 lane 1 字节 = 8 bit）、`8*k +: 8`（同上）、
`pop64` 的 16/8/4/2 级树、`rd_addr_q + 32'd8`（每权重字 8 **字节**）、
`cfg_* << 3`（字节↔元素）、`p_acc[15:0]` 位宽与 `[0:VLANES-1]` 深度、`cout % 8` 类布局假设、预取 `PF_AHEAD/PF_DEPTH/AR_SLOTS`。

## 3. 等价性怎么保证（可复算）

`runs/v50_impA/l1_equiv_proof.py`（ALL PASS）逐项数值比对：
- 派生常量在 VLANES=8 时退化：LVW=3、NVA=2、NVB=4、PRM_W=16、PRMW=5、基址 0/2/6/10/14。
- lane 0..7 的 (pw 字号, 字内 bit 偏移)：新旧逐 lane 相同。
- `prm_addr(i,o0)`：i=0..16（含越界 dummy 拍）、o0∈{0,8,16,192,4096,65535} 全部相同。
- 写字节使能：`(1<<nvalid[2:0])-1 ≡ (1<<nvalid[LVW-1:0])-1`（nvalid 0..7，nvalid≥8 走全 1 保护）。

## 4. xvlog

本机 Vivado：`/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin/xvlog`
**必须带 `-sv`**（文件是 SystemVerilog；裸跑会因 `signed`/`always_comb` 报一串假错）：

```
xvlog -sv bin_conv_core.v
INFO: [VRFC 10-2263] Analyzing SystemVerilog file ".../bin_conv_core.v" into library work
INFO: [VRFC 10-311] analyzing module bin_conv_core
```
无 WARNING / 无 ERROR。未跑 xelab / xsim / Vivado（遵守红线）。

## 5. 端口列表

`)(` 到 `);` 共 20 行，与原文 `diff` **完全一致**（仅参数块加了注释）。

## 6. LANES=16 还差什么（核心之外，未做）

- **激活读宽度不变**：一个 64-bit 激活字喂**全部** lane（原文 line 337/343 就如此，无"每 lane 各自读"）。
  ⇒ 16 lane **不需要** 128-bit 激活通路；共享读结论与 DESIGN_16LANE 一致，无需复核。
- 128-bit 只出现在**写侧**：`mem_wr_data[VLANES*8-1:0]` = 16 lane × 1 byte = 128 bit（= 任务书说的两个 64-bit 字），
  已由 `[8*k +: 8]` 平铺构成。核心内部**没有**加 NBLK 拍循环：拆 2 拍属包装层串行化
  （`bin_conv_core8_compat.v` 做的就是这个），在核心里加会改写时序+端口，撞红线。
- **必须有人接着做（都在红线段外）**：
  1. 16 lane 时 PRM 帧是 32 字，**参数打包端**（生成 alpha/bias/zeta/gamma/slope 的那一侧）要按
     `[0,4) alpha [4,12) bias [12,20) zeta [20,28) gamma [28,32) slope` 给 32 字；给 16 字会读错。
  2. `bin_unit_axi_wrap` 的 `mem_wr_data/mem_wr_sel` 是 64-bit / 8-bit，VLANES=16 时需 128/16 位或 2 拍串行化。
  3. 权重 BRAM 会从 8 块变 16 块（generate 已自动展开，但资源/时序需重评）。
- 未做：VLANES=16 的 xelab 展开（红线禁止）、上板、时序。
