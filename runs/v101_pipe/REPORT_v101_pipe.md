# T-101：在 `act_c → xn_c` 之间插一级流水（切开 9.72 ns popcount 锥）

**一句话**：只改 `lane16/bin_conv_core_v50.v` 一个文件，在 T-100 实测的 9.720 ns 锥
（`win_q_reg_15/CLKBWRCLK → s1_pc_q_reg[14][5]/D`）中间插一级 **`act_q`（64 FF）**，
并按 T-100 的三条配套要求补齐 `l_qd`（权重读地址延后一拍）、`r2_*`（控制级）、`ap_ce` 门控。
`VLANES=8` **10/10 形状逐字节 0 差异**、`VLANES=16` **10/10 形状 0 差异**、
核级 **24 组 before/after 日志逐行相同**、**每阶段拍数一字不变**（下方贴数）。

- 产物：`runs/v101_pipe/`
- 基线树：`runs/v101_pipe/rtl_before/`（**= `runs/timing_v50/rtl_v50` 当前 35/35 逐字节相同**，
  也 = v51 位流的实际输入；`verify_rtl_root.sh` → `MATCH=35 DIFF=0`）
- 改动树：`runs/v101_pipe/rtl_after/`（唯一差异 = `lane16/bin_conv_core_v50.v`）
  - before md5 `61568f39a5206e9f1e1c9f3b620dadb3` → after md5 `c04302ce08a80e219233f4b220a26747`
  - 逐行 diff：`runs/v101_pipe/rtl_t101.diff`（122 行，含注释）
- **未改**：`bin_segq.v`、`bin_unit_axi_wrap_v50.v`、`bin_unit_axi_wrap_bd_v50.v`、PS、BD/tcl、
  25 条 v39 基线文件、`proj_engine`、`proj_v43base2`、`bin_band_*`、写通路。

---

## 0. 改动清单（5 处，全部在 `lane16/bin_conv_core_v50.v`）

| # | 位置（after 文件行号） | 内容 |
|---|---|---|
| ① | L244–265（§6 注释 + 声明） | 新增 `r2_vld/r2_last/r2_l0/r2_oa`、`l_qd[LW-1:0]`、`act_q[63:0]` 声明 |
| ② | L286–300（§7 权重 BRAM 读口） | 读地址 `wmem[l_q]` → **`wmem[l_qd]`**；新增 `l_qd <= l_q`（ap_ce 门控） |
| ③ | L386–402（§10 计算期） | 新增 `act_q <= act_c`（**ap_ce 门控**）；`xn_c[k] = ~(act_q ^ wrd[k])`（原为 `act_c`） |
| ④ | L431–447（§10 控制） | 新增 `r2_*` 一级；`s1_*` 改从 `r2_*` 取（`s1_v_q/s1_last_q/s1_l0_q/s1_oa_q` + `if (r2_vld)` 门控写 `s1_pc_q`） |
| ⑤ | L503–505（tail 判定） | `tail_empty` 里加 `r2_vld`（最后那个 term 的 pc 要到 t_last+2 才进 `s1_pc_q`） |

**算术零改动**：`pop64` 树、`p_acc` 累加、`nvalid/wrsel` 掩码、PRM 帧、`arsh` 移位、
epilogue 8 级、写地址/字节使能生成——**一行未动**。

**为什么必须三件配套**（T-100 的要求，逐条对应）：
1. `act_q` 比 `act_c` 晚一拍 ⇒ `wrd`（权重 BRAM 输出，原本与 `act_c` 同拍）必须也晚一拍，
   故读地址用 `l_q` 的延迟副本 `l_qd`；否则权重与激活**错配一个 term**。
2. popcount 的控制位（valid/last/l0/oaddr）必须与数据一起晚一拍 ⇒ 新增 `r2_*`，
   `s1_*` 从 `r2_*` 取（`r1_rd/r1_bm1` **不延后**，它们属于把 `act_c` 生成出来的那一拍）。
3. `act_q` 挂 `ap_ce`：冻结式反压下 `ap_ce=0` 时 `act_q` 不许刷新（否则漏一拍）。

**时序语义**：整体只多 **+1 拍固定延迟**；`ST_CMP` 仍 1 term/拍、每 pixel 仍 K 拍（贴数见 §2）。

---

## 1. E1 / E2：包装层 TB 逐字节等价（`runs/v94_wrpath` 的 TB，golden 同一份）

编译（每个 VLANES × before/after 各一份）：`./build.sh <8|16> <before|after>`
运行：`./matrix.sh <8|16> <before|after>`（10 形状 = v94_wrpath/MATRIX_final.txt 的同一组）

TB 是 `runs/v94_wrpath/tb_v94_wrap.v` 的**副本** `tb_v101_wrap.v`，只加了两个**只读探针**
（不改任何判定）：`+T101LOG` 给每次核级写事务打绝对拍号；`TB-T101` 行数核状态机各阶段拍数。

### E1 VLANES=8（判据：10 形状逐字节 0 差异 + term/pixel 拍数不变）

| shape | 字节 | 判定 | PRM/WLD/**CMP(term)**/DRN before | after |
|---|---|---|---|---|
| v94_c48_o64_32x32 | 65536/65536 | PASS | 136/1152/**73728**/104 | 136/1152/**73728**/104 |
| v94_c64_o32_8x8 | 2048/2048 | PASS | 68/576/**2304**/52 | 68/576/**2304**/52 |
| v94_c64_o48_8x8 | 3072/3072 | PASS | 102/864/**3456**/78 | 102/864/**3456**/78 |
| v94_c64_o64_8x8 | 4096/4096 | PASS | 136/1152/**4608**/104 | 136/1152/**4608**/104 |
| v94_c96_o96_8x8 | 6144/6144 | PASS | 204/384/**1536**/156 | 204/384/**1536**/156 |
| v94_c48_o24_8x8 | 1536/1536 | PASS | 51/48/**192**/39 | 51/48/**192**/39 |
| v94_c48_o40_8x8 | 2560/2560 | PASS | 85/80/**320**/65 | 85/80/**320**/65 |
| v94_c48_o16_8x8 | 1024/1024 | PASS | 34/32/**128**/26 | 34/32/**128**/26 |
| v94_c48_o8_8x8 | 512/512 | PASS | 17/16/**64**/13 | 17/16/**64**/13 |
| bin_3x96_64_8x8 | 4096/4096 | PASS | 136/2304/**9216**/104 | 136/2304/**9216**/104 |

**10/10 逐字节 0 差异 + 0 越界（预铺 0xA5 + 8KB 尾窗）+ 21/21 case PASS**；
**四个阶段的 enable 拍数逐形状完全相同**（`CMP` = term 数 = 每 pixel 1 拍/term 的直接证据）。

### E2 VLANES=16

| shape | 字节 | 判定 | PRM/WLD/**CMP(term)**/DRN before | after |
|---|---|---|---|---|
| v94_c48_o64_32x32 | 65536/65536 | PASS | 132/1152/**36864**/52 | 132/1152/**36864**/52 |
| v94_c64_o32_8x8 | 2048/2048 | PASS | 66/576/**1152**/26 | 66/576/**1152**/26 |
| v94_c64_o48_8x8 | 3072/3072 | PASS | 99/864/**1728**/39 | 99/864/**1728**/39 |
| v94_c64_o64_8x8 | 4096/4096 | PASS | 132/1152/**2304**/52 | 132/1152/**2304**/52 |
| v94_c96_o96_8x8 | 6144/6144 | PASS | 198/384/**768**/78 | 198/384/**768**/78 |
| v94_c48_o24_8x8 | 1536/1536 | PASS | 66/64/**128**/26 | 66/64/**128**/26 |
| v94_c48_o40_8x8 | 2560/2560 | PASS | 99/96/**192**/39 | 99/96/**192**/39 |
| v94_c48_o16_8x8 | 1024/1024 | PASS | 33/32/**64**/13 | 33/32/**64**/13 |
| v94_c48_o8_8x8 | 512/512 | PASS | 33/32/**64**/13 | 33/32/**64**/13 |
| bin_3x96_64_8x8 | 4096/4096 | PASS | 132/2304/**4608**/52 | 132/2304/**4608**/52 |

### 写事务级（附加证据）

`cmp.py`：写事务**条数相同**（V8 合计 11328、V16 合计 5760）、**地址序列逐条相同**、
**字节 0 差异**。确定性回放（`matrix_det.sh`：ABP=0 WBP=0 ROOO=0 RL=1..1）下，
无组间握手影响的形状写事务拍号 **Δ≡+1**、写间隔多重集合**完全相同**。
带随机反压时（v94 默认设置）Δ 不恒为 +1，这是 TB 的**逐拍随机反压按绝对拍号对齐**造成的
（同一形状 `press` 拍 = 42/118/176/315 等，见各 run 日志的"[TB-v94] 串行化探针"行），
不是 RTL 差异——判据落在"内容 0 差异 + 阶段拍数不变"上。

---

## 2. E3：核级 24 组（`runs/v50_simA/tb_bin_conv_core_v50.v`，**当前 lane16 核**）

`coretb/run_core.sh before|after 24`：把 `bin_conv_core_v50.v` 的**模块名**改成 `bin_conv_core`
（TB 按旧名例化；逻辑零差异，scratch 副本 md5 见 §6），与 v39 `bin_conv_core8` 同台对比。

| 项 | before | after |
|---|---|---|
| S1 org(v39) vs new(8) | 16/24 cases 0-diff, 1389 write-tx | **16/24, 1389**（相同） |
| S2 new(16) vs ceil(c/8)×8 | **24/24** lane-0-diff, 9977 lane | **24/24, 9977** |
| F1 plane-stride addr-diff | 0 | 0 |
| F2 last-group mask（v39/new8/n16） | 167 / 0 / 0 | **167 / 0 / 0** |
| F2 overrun bytes (n16) | 0 | 0 |
| E3 padding mask / rd-addr-X | 0/0/0 | 0/0/0 |
| 24 个 CASE 行 + 48 行 STAT8/STAT16 | — | **与 before 逐行相同** |

`STAT8` 里就是每阶段的拍数（`prm/wld/cmp/drn`）；**24 组 × 2 种 lane 配置全部逐条相等**
（例：CASE 1 `prm=51 wld=432 cmp=1080 drn=39`，before/after 一字不差）。

> ⚠ S1 的 8 组 FAIL 是**基线自带**的 F2（`cout∈{13,17}` 末组掩码：v39 有 167 个掩码错，
> 只有 SEL 位不同、ADDR/DATA 0 差异），`runs/v50_simA_fix/REPORT_fix.md` §40–46 已定过罪；
> **before/after 完全一致**，与本次改动无关。

---

## 3. E4：编译

| 配置 | xvlog -sv | xelab |
|---|---|---|
| VLANES=8 before | rc=0, ERROR=0, WARNING=1※ | `Built simulation snapshot` |
| VLANES=8 after | rc=0, ERROR=0, WARNING=1※ | Built |
| VLANES=16 before | rc=0, ERROR=0, WARNING=1※ | Built |
| VLANES=16 after | rc=0, ERROR=0, WARNING=1※ | Built |
| 核级 TB（after，单文件 + core8） | rc=0, ERROR=0, **WARNING=0** | Built |

※ WARNING=1 = `VRFC 10-3609 overwriting previous definition of module 'fake_engine'`
（`_selftest_ok.v`/`_selftest_bad.v` 重复定义，**v51 基线同样存在**，见
`runs/v99_coalesce/REPORT_v99_coalesce.md` C1 行）。改动**没有引入任何新 warning**。

---

## 4. E6：`verify_rtl_root.sh`

| 树 | manifest | 结果 |
|---|---|---|
| `rtl_before/` | `manifest_before.txt`（= v51 位流 manifest，35 条） | `MATCH=35 DIFF=0 EXTRA=0 MISSING=0` **PASS** |
| `rtl_after/` | `manifest_after.txt`（**刷新 1 条**：`lane16/bin_conv_core_v50.v` → `c04302ce…`） | `MATCH=35 DIFF=0 EXTRA=0 MISSING=0` **PASS** |
| 线上根 `runs/timing_v50/rtl_v50`（未被我改动） | 线上 manifest | `MATCH=35 DIFF=0` **PASS** |

`rtl_before/` 与线上根 **35/35 逐字节相同**（已 `md5sum` 全量比对）⇒ 合入只需拷 1 个文件 + 刷 1 条 md5。

---

## 5. E5 / E7：place（synth + opt + place + phys_opt，**不 route**）

同一份 tcl（`build_place.tcl`，派生自 v51 正式构建，约束/指令/器件**逐字相同**：
10.000 ns、`AreaOptimized_high`、`control_set_opt_threshold 8`、`place Explore` +
`phys_opt AggressiveExplore`），只把 `$rtl` / `$out` / manifest 换成 before / after。
产物：`viv_before/`、`viv_after/`（`t101_placed.dcp`、`util_t101.rpt`、`timing_t101*.rpt`、
`t101_oldcone.rpt`、`t101_newcone.rpt`、日志）；只读复核：`sta/cone_before.rpt` / `sta/cone_after.rpt`。

### 5.1 时序（place 阶段，10 ns ⇒ 100 MHz）

| 查询（`sta_cone.tcl`，两份 placed DCP 同脚本） | **before** | **after** |
|---|---|---|
| **① 整体 WNS（setup）** | **+0.202** ns，11 级<br>`win_q_reg_15 → s1_pc_q_reg[5][5]/D` | **+0.515 ns**，8 级<br>`g_wram[15].wmem_reg → s1_pc_q_reg[15][5]/D` |
| ② **T-100 旧锥** `win_q_reg* → s1_pc_q_reg*/D` | +0.202（**就是 WNS**） | **无路径**（被 act_q 切断）✅ |
| ③ 任何起点 → `s1_pc_q_reg*/D`（pop 输出侧） | +0.202 | **+0.515** |
| ④ `wmem`（权重 BRAM DO）→ `s1_pc_q_reg*/D` | +0.594，7 级（**第二族**） | **+0.515**，8 级（成了新最差） |
| ⑤ `win_q_reg* → act_q_reg*/D`（新加的寄存级） | —（该级不存在） | **+4.710**，2 级 |
| ⑥ `act_q_reg* → *`（pop 树本体） | — | **+1.963**，9 级 |
| WHS（hold） | −1.479 | −1.452 |

**结论**：
1. **旧锥已彻底摘掉**：`win_q_reg → s1_pc_q` 在 after 网表里**没有路径**；它被切成
   ⑤ +4.710 ns 与 ⑥ +1.963 ns 两段（T-100 预估"≈5.2 + ≈5.0"，实测 4.710 / 1.963，
   因为 2.093 ns 的两条高扇出网随 `act_c` 只驱动 64 个 FF 一起消失）。
2. **popcount 锥的新 slack = +0.515 ns ≥ +0.5 ✓**（判据达成，余量很薄）。新最差落在
   **权重 BRAM 那一侧**（`wmem → XOR → pop64`），它是改动前的**第二族**（+0.594），
   被这次改动"顶上来"成 WNS——即"新 WNS = 第二族"这一预期成立（数值比 T-100 估的 +0.39 更好）。
3. **整体 WNS = +0.515**（place）。⚠ 公允换算：基线 place→route 的代价实测为
   **−0.089 ns**（v51 routed DCP 上同一条锥 = +0.113，本机 place = +0.202）
   ⇒ **route 后预计 ≈ +0.42 ~ +0.45 ns**（相对 +0.113 是 **约 4×**）。
   若要求 route 后仍 ≥ +0.5，需要再切权重侧那一级（`xn_c`/pop 树输入前再打一拍）——
   **本次未做**（T-100 方案只切激活侧，且本轮红线是"不许做别的优化"）。

### 5.2 资源（`report_utilization`，都是 place 阶段）

| 项 | before | after | Δ |
|---|---|---|---|
| Slice LUTs | 21873 | **21524** | **−349** |
| LUT as Logic | 21091 | 20742 | −349 |
| Slice Registers | 21822 | **21805** | **−17** |
| LUT as Memory | 782 | 782 | 0 |
| Block RAM Tile | 43 | 43 | 0 |
| DSPs | 126 | 126 | 0 |
| Register as Latch | 0 | **0** | 0 |

**为什么 FF 反而少了 17**：`u_core` 内部时序 cell **7556 → 7663（+107）**
（`act_q 64` + `r2_vld 1` + `l_qd_reg_rep 8` + 扇出复制/吸收差），
但 `act_c` 那条 16 lane × 64 bit 的高扇出网不再需要 LUT/FF 复制
⇒ 全设计 LUT **−349**、FF 净 **−17**。**没有新增 BRAM/DSP，Latch = 0。**

### 5.3 网表指纹（防止"改了 RTL 但综合吃掉"）

| 单元 | before | after |
|---|---|---|
| `act_q_reg` | **0** | **64** |
| `r2_vld_reg` | 0 | **1** |
| `l_qd_reg`（含 `_rep`） | 0 | **8** |
| `u_core` 时序 cell | 7556 | 7663 |
| BRAM 推断 | `256x64 WRITE_FIRST/READ_FIRST, Port A and B` | **逐字相同** |

## 6. 复现命令

```bash
cd runs/v101_pipe
./build.sh 8 after && ./matrix.sh 8 after          # E1（golden 用 runs/v94_wrpath/golden）
./build.sh 16 after && ./matrix.sh 16 after        # E2
(cd coretb && ./run_core.sh after 24)              # E3
python3 cmp.py 8  / python3 cmp.py 16              # 写事务逐条对比
V101_RTL=$PWD/rtl_after V101_OUT=$PWD/viv_after V101_MAN=$PWD/manifest_after.txt \
  bash ../pynq_specialized_code/tools/memgate.sh 9 --wait 7200 -- \
  /media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin/vivado -mode batch -source build_place.tcl  # E5
```

scratch 副本 md5（核级 TB 用，仅模块名不同）：`coretb/before/bin_conv_core.v c0bbde58…`、
`coretb/after/bin_conv_core.v 783e7deb…`。
