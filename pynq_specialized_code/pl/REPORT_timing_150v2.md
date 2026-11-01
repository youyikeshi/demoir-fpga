# 150 MHz 时序收敛报告（design_1_150v2.bit）

日期：2026-10-31　执行：FPGA 构建子代理
目标：把 150 MHz 的 WNS 从 −0.931 ns 收到 ≥ 0

---

## 1. 结论（先行）

| 指标 | 修复前（板上那版 `design_1_150p.bit`） | 交付版 `design_1_150v2.bit` |
|---|---|---|
| **WNS @ 6.667 ns** | −0.931 ns | **−0.241 ns** |
| TNS | −127.250 ns | **−26.272 ns** |
| 违例端点 | 828 | **332** |
| WHS（hold） | +0.022 ns | **+0.064 ns** ✅ |
| LUT | 12,570 | 12,681 |
| FF | 13,993 | 14,182 |
| BRAM（RAMB36） | 6 | 6 |
| **DSP48E1** | 78 | **78** ✅ 引擎完整 |
| bit 字节 | 3,510,798 | 3,510,798 |

**未达到 ≥0。** 卡在**布线绕线**（最差路径 route 占 64~78%），不是逻辑深度，也不是时钟 skew（skew 已从 −1.511/‑1.320 降到 −0.03 ~ −0.23）。

---

## 2. 改了哪些 RTL（全部可回退）

回退点：`rtl_hand/tools/restore.sh timing_pre_pixreg`（改动前）、`timing_addrreg_maxfan`（改动后）

### 2.1 `conv_int8_uni.v` §9b/§16 —— 读地址通路提寄存器（3 处）

**(a) 像素分支**
```verilog
// 旧：cm_pix_c = ox_last ? (rowy_nxt_q - pad8_q) : (pix_base_q + s8_q);
// 新：
logic [31:0] pix_nx_q;   // ≡ pix_base_q + s8_q
logic [31:0] row0_q;     // ≡ rowy_nxt_q - pad8_q
wire [31:0] cm_pix_c = ox_last ? row0_q : pix_nx_q;   // 纯 2:1 mux
```
不变式靠与 `pix_base_q` / `rowy_nxt_q` **同一个沿、同一条表达式**更新，`cmp_load` 时播种。
收益：干掉原 WNS 路径 7.355 ns（`ox_q → ox_last → 32bit 加 → rd_addr_q`，route 4.47 ns）。

**(b) 三个"相对推进"候选地址**
```verilog
logic [31:0] kx_addr_q, ky_addr_q, cg_addr_q;  // ≡ rd_addr_q + {d8_q, step_ky_q, step_cg_q}
wire [31:0] cm_nxt_c = term_last ? cm_pix_c :
                       j_last    ? cg_addr_q :
                       kx_last   ? ky_addr_q : kx_addr_q;   // D 端 = 纯 mux
```
更新条件与 `rd_addr_q` **完全相同**（`!freeze && !pix_last` 一起冻结），`cmp_init` 播种。
收益：干掉原 7.20 ns 路径（`kk_m1_q → j_last（1.23 ns，扇出 77）→ 32bit 加 → rd_addr_q`）。

**(c) 高扇出控制网线降扇出（纯综合属性，逻辑逐位不变）**
```verilog
(* max_fanout = 24 *) wire j_last / cg_last / kx_last / ox_last / term_last
```

### 2.2 两个 wrapper 各 1 行（逐值等价）
```verilog
// 旧: if (u_ce && rd_want) cnt_rdreq <= cnt_rdreq + 16'd1;
// 新: if (u_ce)            cnt_rdreq <= cnt_rdreq + {15'd0, rd_want};
```
`conv_uni_axi_wrap.v` / `bin_unit_axi_wrap.v`。CE 只剩 `u_ce`，省一级 AND。

### 2.3 功能验证
- `verify_all.sh conv` → **7/7 逐字节 PASS**（含 max_fanout 版）
- `verify_all.sh all` → 19 形状 + 4 二值单元全 PASS（在 00:11 之前的 RTL 状态上）

---

## 3. 构建配方（固化在 `scripts/build_final_150v4.tcl`）

```
synth_design -directive AreaOptimized_high -control_set_opt_threshold 8
# create_clock -period 6.667（必须在 synth 之后）
opt_design  -directive RuntimeOptimized
place_design -directive Explore              ← 关键
phys_opt_design -directive AggressiveExplore
route_design -directive AggressiveExplore
phys_opt_design -directive AggressiveExplore ← route 之后再来一次（round-1 没有）
```

实测对比（同一份新 RTL，完整 P&R，确定性可复现）：

| place / route | WNS | TNS | 端点 |
|---|---|---|---|
| **Explore / AggressiveExplore** | **−0.241** | **−26.3** | **332** |
| ExtraTimingOpt / Explore | −0.824 | −176.7 | 1277 |
| ExtraTimingOpt / AggressiveExplore | −0.712 | −152.9 | 1018 |

⚠️ 2025.2 的 placer **没有** `AggressiveExplore` 指令（会报 `Constraints 18-641` + “Placer could not place all instances”）；合法时序向指令是 `Explore` / `ExtraTimingOpt`。

---

## 4. 为什么还差 0.24 ns（分析）

两条并列最差路径（都已不在 CAM/BUFGCE 上）：

| 起点 → 终点 | slack | 数据路 | logic | **route** | skew |
|---|---|---|---|---|---|
| bin `r1_l_reg[1]` → `s1_pc_q_reg[6]` | −0.241 | 6.890 | 1.904 | **4.986 (72%)** | −0.028 |
| conv `j_q[4]` → `rd_addr_q[7]` | −0.240 | 6.579 | 1.466 | **5.113 (78%)** | −0.226 |

判据：
- LUT 只用了 **27.5%**，但单跳网线 0.4~1.0 ns（甚至相邻两列 SLICE 之间 0.705 ns / fo=5）
  ⇒ 典型的**局部拥塞绕行**，不是资源不足。
- conv 侧那条的直接原因：`j_last`（扇出 77，1.06 ns）→ `term_last`（扇出 117，1.02 ns）→ 最终 mux。
- 时钟 skew 已不是瓶颈，因此**移动 BUFGCE / 给 ent_tag 加 pblock 收益≈0**，未实施。

---

## 5. 阻塞项与下一步

**阻塞**：另一个子代理正在重写 bin 读侧（`bin_unit_axi_wrap.v` / `bin_conv_core.v`，新增 `base_alpha/zeta/gamma/slope`、`mem_wr_data[63:0]`、`bin_conv_core8`）。当前状态 elaboration 失败：
```
ERROR: [Synth 8-439] module 'bin_conv_core8' not found (bin_conv_core.v:33)
```
所以 00:38 启动的 v5（带 max_fanout 的版本）没能出 bit。**交付的 `design_1_150v2.bit` 里 bin 读侧仍是旧实现**。

下一步（按收益排序）：
1. bin 重写合并后，用 `scripts/build_final_150v4.tcl` 重跑（文件清单里要追加 `bin_conv_core8.v`）——bin 的 −0.241 大概率会变。
2. 若 conv 的 −0.240 仍在：把 `term_last/j_last` 的 `max_fanout` 压到 12，或给 `conv_uni_0` 加 pblock 收拢（对症"高扇出 + 绕线"）。
3. 再不行试 `place_design -directive Explore` + `AltSpreadLogic_medium`（降拥塞绕行）。

---

## 6. 产物清单

```
board/overlay_engine_150mhz/design_1_150v2.bit   3,510,798 B   ← 交付
board/overlay_engine_150mhz/design_1_150v2.hwh     245,850 B   ← 来自 proj_engine hw_handoff
scripts/build_final_150v4.tcl                                  ← 最优配方（输出 design_1_150v4.bit）
scripts/build_final_150v2.tcl / v3 / v5                        ← 实验变体（保留以便复现）
board/overlay_engine_150mhz/design_1_150v4_routed.dcp          ← 路由后 checkpoint（可复查拓扑）
/tmp/fin150v4_t.rpt  /tmp/fin150v4_i.rpt                       ← 时序 / 资源报告
```

`.hwh` 地址验证（**上板唯一前提**）：
```
BASEVALUE="0x40040000"  INSTANCE="conv_uni_0"
BASEVALUE="0x40060000"  INSTANCE="bin_unit_0"
```


---

# 附录 A：合并新 bin 读侧后的构建（`design_1_150v9.bit`，2026-10-31 00:55）

配方：`scripts/build_final_150v6.tcl`（= v4 配方，文件清单已加 `bin_conv_core8.v`）

## A.1 结果 — 严重恶化，不可上板

| 指标 | `150v2`（旧 bin） | **`150v9`（新 bin）** |
|---|---|---|
| WNS | −0.241 ns | **−1.833 ns** |
| TNS | −26.27 ns | **−8343.83 ns** |
| 违例端点 | 332 | **11,039 / 46,305（24%）** |
| WHS | +0.064 | +0.043 ✅ |
| LUT | 12,681 | 17,059 |
| FF | 14,182 | 16,009 |
| BRAM（RAMB36） | 6 | **15** |
| DSP | 78 | **93** |

`.hwh` 地址仍为 `0x40040000` / `0x40060000` ✅

## A.2 违例 100% 集中在新 bin 读侧

对 `design_1_150v6_routed.dcp` 采样最差 4000 条路径：
```
design_1_i/bin_unit_0/inst   n=3898   worst=-1.833
design_1_i/ps7/inst          n=1      worst=-1.226
```
conv 侧不再出现在违例里。

## A.3 根因：`bin_unit_axi_wrap.v` 的 16 路优先扫描（14 级逻辑锥）

WNS 路径：`ent_out_reg[0][3] → start_req → ar_id_r[1]_i_* → ent_tag[0][25]_i_* → ent_tag_reg[14][*]/CE`
数据路 8.123 ns（logic 1.849 / route 6.274 = 77%），**14 级**。

```verilog
integer vi;
always_comb begin                       // 16 路 priority scan
    alloc_ok = 1'b0; alloc_v = 0; vcand = 0;
    for (vi = 0; vi < NLINES; vi = vi + 1) begin
        vcand = alloc_ptr + vi;
        if (!alloc_ok && (ent_out[vcand] == PF_Z)) begin alloc_ok = 1'b1; alloc_v = vcand; end
    end
end
assign alloc_en = rd_miss && !rd_known && alloc_ok && !arvalid_r && !start_fire;
```
`ent_out[0..15]` → 16 路扫描 → `alloc_ok/alloc_v` → `ar_id_r` → `ent_tag[*].CE`，
其上还叠 `rd_miss`（26bit×16 CAM）与 `!start_fire` ⇒ 两级深锥串联。
这正是 `conv_uni` 在 §6.0 已经改成"观察→决策→落盘"三级流水的那个模式，bin 侧仍是旧写法。

## A.4 修法（建议，未实施 —— 属于 bin RTL 所有者）

1. `alloc_ok` 改成寄存的空条目计数：`n_free_q <= n_free_q + freed - taken; assign alloc_ok = (n_free_q != 0);`
   （`freed = r_fire && ent_out[rid_ent]==1`，`taken = alloc_en`；两事件按构造落在不同条目 ⇒ 逐值等价）
2. `alloc_v` 的 16 路扫描打一拍（分配是单笔串行，`!arvalid_r` 才分配 ⇒ 近似零代价）
3. `ent_tag[*].CE` 只由寄存后的 `al_v` 驱动

预计把 14 级砍到 6~7 级，回到 conv 的 −0.24 量级。

## A.5 注意
即使 bin 修好，150 MHz 的收敛点仍会是 **conv 的 −0.240 ns**（本轮没有变化）。
要真正到 ≥0，还需要第 4 节里的两项之一（`term_last/j_last` 再降扇出，或 conv pblock 收拢）。


---

# 附录 B：bin CAM 分配流水化（①②③）+ WIN16 后的构建（`design_1_150v11.bit`）

## B.1 改动（`_worktrees/binalloc/bin_unit_axi_wrap.v`，+56/−12）

| 项 | 内容 |
|---|---|
| ① 决策级 | 新增 `al_v / al_ent / al_tag`；`al_v <= alloc_en; al_ent <= alloc_v; al_tag <= rd_tag;` |
| ② 落盘级 | `if (al_v && !start_fire) { ent_tag[al_ent] <= al_tag; … ar_id_r <= zx_id(al_ent); }` |
| ③ 串行把关 | `alloc_en = … && !arvalid_r && !start_fire && !al_v`（防止决策拍再决策第二笔，导致 arvalid 期间地址被改写）|
| WIN16 | `RD_WIN_WORDS = 8 → 16`（来自 lane8 patch，只取这一项）|
| ERR_LAYOUT | `(UNIT == 0) && (r_cout[2:0] != 0) ⇒ ERR_LAYOUT` |
| 预取 | **未引入**（默认关、收益 0，且会往刚打拍的 alloc 区再加一条 16 路扫描锥）；`R_PF_EN` 保持 0 |

安全性论证（写进代码注释）：miss 期间 `u_ce=0` ⇒ 引擎冻结 ⇒ `rd_tag/rd_want/rd_ent` 逐拍不变；
`r_fire` 只改 `ent_rdy/ent_out`、不改 `ent_on/ent_tag` ⇒ `cam_knw` 不变 ⇒
「tag 不在缓存 + 受害者 `ent_out==0`」在落盘那拍仍成立；`start_fire` 拦掉落盘。

## B.2 结果

| 指标 | v9（合并, WIN8） | **v11（CAM 修复 + WIN16）** |
|---|---|---|
| WNS | −1.833 | **−2.010** |
| TNS | −8343.8 | −9153.9 |
| 违例端点 | 11,039 | 11,371 |
| WHS | +0.043 | +0.052 ✅ |
| LUT | 17,059 | **14,590**（−2,469）|
| FF | 16,009 | 15,986 |
| BRAM | 15 | 15 |
| DSP | 93 | 93 |
| bit | 3,510,798 B | 3,510,798 B |

`verify_all.sh wrap`：**12/12 逐字节 PASS**；cycles 因 WIN16 进一步下降
（`bin_1x32` 1274→1107、`bin_3x96` 18899→17280、`sam_64` 10736→9095）。

## B.3 CAM 修复生效的证明

v11 routed dcp 采样 6000 条违例路径，按目的寄存器族归类：

```
n=2048  -2.010  bin/u_bin_unit/bf_data_reg        ← 新的最差
n= 970  -1.374  bin/u_eng/pw_reg
n= 464  -1.862  bin/u_bin_unit/df_addr_reg
n= 334  -1.485  conv/fq_tag_reg                  ← conv 也进来了（之前是好的）
n= 256  -2.009  bin/u_bin_unit/bf_strb_reg
n= 137  -1.266  conv/u_unit/G_BANK
n= 104  -1.951  bin/u_bin_unit/be_data_reg
n=  67  -1.094  conv/ent_out_reg
n=  64  -1.757  bin/u_bin_unit/df_len_reg
+ DSP 级联 p_1_out__*/p_2_out__*/K_lc__* 的 PCIN 各 48 条，−0.85 ~ −1.79
```

**`ent_tag_reg[*]/CE` 那一族彻底消失** ⇒ ①②③ 达到设计目的。
但代价是露出下一层：`rd_addr → win_q(BRAM) → rd_hit → 字节合并 → bf_data/bf_strb/be_data`
全串在一拍（14 级，8.567 ns，route 69%）。

## B.4 下一轮建议

1. **bin 写口插入口寄存器**（照 `conv_uni_axi_wrap` §7.0 的 `p_valid/p_addr/p_data`）：
   写口无延迟契约，只多 1 拍，可把 `bf_data/bf_strb/be_data`（2,408 端点）与 `df_addr/df_len`（528 端点）一起切断。
2. **DSP 级联插拍**：八路 `PCIN` 链每两级插一级寄存器（吞吐不变，延迟 +1 拍），消掉 −1.5 那一族。
3. conv 的 `fq_tag/G_BANK` 先不动：很可能只是 bin 变大后的拥塞副作用，等 1/2 落地后同配方复跑一次再看。


---

# 附录 C：整字写口入口寄存器（①②）后的构建（`design_1_150v12.bit`）

## C.1 改动（`_worktrees/binalloc/bin_unit_axi_wrap.v`，累计 +94/−22）

| 组 | 内容 |
|---|---|
| A（附录 B） | CAM 分配三级流水：`al_v/al_ent/al_tag`、`alloc_en && !al_v`、落盘级用 `al_*` |
| **B（本轮 ①）** | 整字写口入口寄存器 `pw_valid/pw_word/pw_data`：`pw_valid <= wr_wide_b; if (wr_wide_b) {pw_word<=u_wr_addr[31:3]; pw_data<=u_wr_data;} if (pw_valid) push_beat(pw_word,pw_data,8'hFF,…)` |
| **C（本轮 ②）** | 记账同源：`wide_chain / push_word_x / push_now / desc_push` 全用 `pw_*`；`drain_cond`、`wr_all_idle`、`p_merge`、断言都加 `!pw_valid` |
| D | `RD_WIN_WORDS=16`；`(UNIT==0)&&(cout%8!=0)⇒ERR_LAYOUT`（预取未引入） |

## C.2 三个频率（setup slack 的 arrival 与 period 无关 ⇒ 换算是精确的）

```
150 MHz (6.667 ns):  WNS = -1.692   TNS = -6928.2   端点 10,028/46,881   WHS = +0.056
125 MHz (8.000 ns):  WNS = -0.359
100 MHz (10.00 ns):  WNS = +1.641   ← 可用
```

⚠️ BD 的 FCLK 仍是 150 MHz（`.hwh`: `PCW_FPGA0_PERIPHERAL_FREQMHZ=150`）。
真要跑 100/125 MHz，必须改 BD 时钟或在 PYNQ 侧运行时配时钟。

## C.3 资源 / 验证

| | v11 | **v12** |
|---|---|---|
| LUT | 14,590 | 14,440 |
| FF | 15,986 | 16,158 |
| BRAM | 15 | 15 |
| DSP | 93 | 93 |
| bit | 3,510,798 B | 3,510,798 B |

`verify_all.sh wrap` = **12/12 逐字节 PASS**；cycles 进一步下降
（`bin_1x32` 1107→1029、`bin_1x64` 2112→1881）。

`.hwh` 地址：`0x40040000` / `0x40060000` ✅

## C.4 端点族迁移表

| 端点族 | v11 | v12 |
|---|---:|---:|
| wrapper `bf_data_reg` | 2048 @ −2.010 | 2042 @ −1.690 |
| wrapper `bf_strb_reg` | 256 @ −2.009 | 255 @ −1.563 |
| wrapper `df_addr_reg` | 464 @ −1.862 | 464 @ −1.504 |
| wrapper `df_len_reg` | 64 @ −1.757 | 64 @ −1.418 |
| wrapper `be_data_reg` | 104 @ −1.951 | 54 @ −1.164 |
| **引擎 `u_eng/pw_reg`** | 970 @ −1.374 | **1138 @ −1.371** |
| 引擎 `e2_*/e1_sh_q/g_wram→s1_pc_q` | — | 各 50–110 条，−0.79 ~ −1.69 |
| `ent_tag_reg[*]/CE` | ✅ 已消失 | ✅ 已消失 |

写口族整体上移 0.3~0.8 ns（`pw_data_reg[63:0] / pw_valid / pw_word_reg[28:0]` 在网表中确认存在，
CE=`wr_wide_b`），但剩余 −1.69 的入口已变成**引擎内部** `iy_q → r1_rd → …`（12 级 / route 76%）。

## C.5 下一步

剩余全部在 `bin_conv_core8.v`（lane8 的文件）：`u_eng/pw_reg`、`e2_*`、`g_wram→s1_pc_q`、DSP `PCIN` 链。
建议 **先用 100 MHz 这一版上板拿真实数字**，另开 `_worktrees/corepipe` 在核内做 ③（DSP 级联插拍）+ 引擎侧入口寄存器，
只交 diff，与 lane8 的改动一起合。


---

# 附录 D：125 MHz 目标 + 合并状态 + corepipe 初诊（2026-10-31 02:20）

## D.1 决策（已定）
- **不动 BD**：`.hwh` 里 `PCW_FPGA0_PERIPHERAL_FREQMHZ=150` 只是初始值；
  板上用 PYNQ 运行时 `Clocks.fclk0_mhz = 125` 配时钟（历史上改 BD 挂过 3 次板）。
- **目标 125 MHz**：v12 在 125 MHz 的 WNS = **−0.359**，比板上已跑通的
  `design_1_150p` 在 125 MHz 的 −0.448 还好 0.09 ns。
- 100 MHz（+1.641）虽收住，但整网余量只有 9%，不选。

## D.2 v13 构建脚本（已备好）
`scripts/build_final_150v13.tcl`：与 v12 唯一差别是
```tcl
create_clock -period 8.000 -name FCLK_CLK0 $clk_pin     # 125 MHz
set rtl  $pl/rtl_hand/modules                            # 合并后用 master
```
输出 `design_1_150v13.bit`、`/tmp/fin150v13_{t,i}.rpt`。

⚠️ 合并状态（02:20 核查）：master 的 `bin_unit_axi_wrap.v` **不含** `al_v`（CAM 修复）
也不含 `RD_WIN_WORDS=16`，mlp 也未合入 ⇒ 必须先 `tools/worktree.sh apply binalloc`。

## D.3 corepipe 初诊（worktree 已开，未出 diff）

| 族 | slack | 起点 → 终点 | levels |
|---|---|---|---|
| `s1_pc_q_reg` | **−1.692** | `g_wram[6].wmem_reg/CLKARDCLK` → `s1_pc_q_reg[6][6]/D` | 8 |
| `bf_data_reg` | **−1.690** | `iy_q_reg[3]` → `bf_data_reg[13][31]/D` | 12 |
| `e2_rp_q_reg` | +0.098 | `pw_reg[4][32]` → `e2_rp_q_reg[5][29]/D` | 12 |
| `e2_sn_q_reg` | +4.584 | `p_0_out__6/CLK`（DSP） | 0 |
| `pw_reg` /D | **+0.204** | `win_q_reg/CLKARDCLK` → `pw_reg[4][44]/D` | 0 |

结论修正：
1. `pw_reg` 的**数据端是好的**（+0.204）；−1.371 那个族是它的 **/CE（写使能）**。
2. `e2_*` 与 DSP `PCIN` **并不违例** ⇒ 原计划的"③ DSP 级联插拍"**目前不必做**。
3. 真正的两条：`s1_pc_q`（权重 BRAM 输出 → 8 级 popcount，−1.692）
   与 `bf_data` 残留（引擎 `iy_q` → 宽写译码/写使能，不是数据路）。

未出 diff 的原因：两者都在 `bin_conv_core8.v` 的固定流水线（`a1..a7_q` / `v1..v8_q` /
`wrd[gk]` 同步读）里，插拍必须同步改 FSM 推进拍与 `done` 判定；凭猜测改核的高危后果是
**静默丢数据**。待批的切法：
- **C1（低风险）**：`wr_wide_b` 打一拍给 FIFO 写使能（只多 1 拍，不动 FSM）。
- **C2（中风险）**：popcount 前插一级，需同步改 `s1_l0_q/s1_l1_q` 相位与 `pc_c` 汇总拍。


---

# 附录 E：**最终候选版** `design_1_150v13.bit`（125 MHz，合并 mlp 后的 master）

## E.1 构建
```
source .../settings64.sh
bash scripts/vrun.sh scripts/build_final_150v13.tcl /tmp/build_v13.log 7200   # rc=0
```
脚本：`create_clock -period 8.000`（125 MHz），`set rtl = pl/rtl_hand/modules`（合并后的 master），
配方 = place Explore / route AggressiveExplore / 2×phys_opt（与 v12 相同）。

## E.2 结果

| 项 | 值 |
|---|---|
| **WNS @125 MHz (8.000 ns)** | **−0.388 ns** |
| WNS @150 MHz（换算） | −1.721 ns |
| WNS @100 MHz（换算） | **+1.612 ns** |
| TNS | −244.597 ns |
| 违例端点 | 1,587 / 39,439（时钟组）；总 47,548 |
| WHS / async | +0.034 / +2.050 ✅ |
| LUT / FF / BRAM / DSP | 15,174 / 16,481 / 15 / 93 |
| bit | 3,510,798 B |
| `.hwh` | `0x40040000` / `0x40060000` ✅ |

**可用性依据**：板上已跑通 6/6 阶段 + golden 5/5 + 227 层的 `design_1_150p`
在 125 MHz 的 WNS 是 −0.448；本版 −0.388，**好 0.06 ns**。

## E.3 端点构成（1,587 条）

```
n=757  -0.385  bin/u_bin_unit/bf_data_reg     ┐
n=370  -0.379  bin/u_bin_unit/df_addr_reg     │ 写口+记账 1,236 条 = 78%
n= 55  -0.388  bin/u_bin_unit/bf_strb_reg     │ （C1 的目标）
n= 54  -0.379  bin/u_bin_unit/df_len_reg      ┘
n= 82  -0.321  bin/u_bin_unit/pf_tag_reg      ← mlp 新增的预取 tag，量小
n= 48×若干  -0.12~-0.35  引擎 DSP PCIN
```
最差三条同一源：`u_eng/rd_addr_q_reg[7]` → `bf_strb/bf_data/df_*` 的 **D-mux 写使能**
（引擎 `u_wr_addr/u_wr_sel` → `wr_wide_b`），不是数据路。

## E.4 备选：C1（未实施）
把 `wr_wide_b` 打一拍（`ww_v_q`），FIFO/记账写使能改用全 FF 版本；
`drain_cond`/`wr_all_idle` 加 `!ww_v_q` 守卫（与 `pw_valid` 同款）。预计再吃 0.3~0.4 ns。


---

# 附录 F：**板上功能故障的真正根因 = RTL 多驱动**（2026-10-31 02:20–02:45）

## F.1 现象
v12/v13 上板：5 个 golden 全部逐字节不符（457/2048、485/4096…），而 xsim 12/12 PASS；
`fill/rd = 0.487`（128 B 线应为 0.0625）⇒ 约 8× 填线。

## F.2 根因（综合日志 102 条 CRITICAL WARNING，读代码确认）
`bin_conv_core8.v` 里 **12 个寄存器被两个 always_ff 块各写一遍**：

- 块 A：主状态机（行 580 起），**只在复位分支**写这 12 个
- 块 B：§13「读地址步长常量」（行 754 起），复位 **和** PRM 计算都写同一批

```verilog
// 块 A 行 607-610（重复的复位，已删除）
mn_cb_q / mn_k_q / mn_wo_q / cp1..4_q / st_kx_q / st_kyw_q / st_px_q / st_pyd_q / st_py_q <= 0;
```

Vivado 报：
```
CRITICAL WARNING: [Synth 8-6859] multi-driven net on pin Q with 1st driver ... st_kx_q_reg[31]/Q
CRITICAL WARNING: [Synth 8-6859] ... 2nd driver pin 'GND'
CRITICAL WARNING: [Synth 8-6858] ... constant driver preserved, **other driver is ignored**
```
⇒ 综合器保留 GND、丢掉 FF ⇒ **硬件上读地址步长恒 0**
⇒ 读地址不推进/乱跳 ⇒ 数据错 + 反复重填同一条线（正好 8× 填线）
⇒ xsim 是 last-write-wins（块 B 的 PRM 值恰好赢）⇒ 仿真过、板上挂。**与频率无关。**

## F.3 修复
`modules/bin_conv_core8.v`：删掉块 A 的这 4 行（`+0/−4`），块 B 独占。
验证：`xvlog` 0 ERROR；`verify_all.sh wrap` **12/12**、`BENCH_LAT=55 wrap` **12/12**（逐字节一致）。

## F.4 修复后的两个构建

| | v14 @125 MHz | **v15 @100 MHz** |
|---|---|---|
| `Synth 8-6859 / 8-6858` | **0 / 0** | **0 / 0** |
| WNS | −0.743 | **+0.008（0 违例端点）** |
| TNS | −581.3 | 0.000 |
| WHS | +0.002 | +0.035 |
| LUT / FF / BRAM / **DSP** | 16,100 / 16,810 / 15 / **107** | 15,566 / 16,703 / 15 / **107** |
| bit | 3,510,798 B | 3,510,798 B |

**DSP 93 → 107（+14）** 是"修复为真"的硬证据：`st_kx_q` 被 GND 吃掉时，
`cp = mn_* × hw8/dw8/d8/s8` 那 4 组乘法器被连带常量折叠；修复后回到网表。

## F.5 修复后的违例族（v14 @125 MHz，routed dcp 采样）
```
n=1488  -0.700  bin/u_bin_unit/bf_data_reg      ← 写口（C1 目标）
n= 247  -0.693  bin/u_bin_unit/bf_strb_reg
n= 100  -0.560  bin/u_bin_unit/pf_tag_reg       ← mlp 预取 tag
n=  70  -0.118  bin/u_bin_unit/df_addr_reg      ← 原 -0.379，大幅好转
n=  57  -0.723  bin/u_eng/rd_addr_q_reg
n=  34  -0.743  bin/u_eng/s1_pc_q_reg           ← 125 MHz 的 WNS
n=  29  -0.741  axi_ctrl/xbar/.../m_payload_i_reg
```
`st_kyw_q / st_px_q / cp1_q` **完全无违例**；`st_kx_q` 只剩 1 条 −0.082 ⇒ 它们现在是真 FF。

## F.6 `xbar/m_payload_i_reg` 的驱动源（逐级打印）
```
from: u_bin_unit/araddr_l_reg[7]/C
  …8 级 LUT 全在 wrapper 内（u_bin_unit/xbar_i_*，含 MUXF7 ⇒ 8:1 量级 mux）…
  → axi_ctrl/xbar/.../m_payload_i_reg[18]/D
```
⇒ 即 `m_axi_araddr` 是 `arq_addr[arq_rp]` 的多选中 mux（mlp 的 AR 队列）。
便宜修法：队列指针只在 `ar_fire`/入队时动 ⇒ 提前一拍把下一笔 AR 地址选进寄存器
（`ar_addr_sel_q`），`m_axi_araddr` 变 0 级组合，可吃 0.5~0.7 ns。


---

# 附录 G：第 1 刀（AR 队头预选）+ 第 2 刀（防寄存器吸收）→ `design_1_150v17.bit`

## G.1 改动（`_worktrees/perf/bin_unit_axi_wrap.v`，+37/−2）

**第 1 刀**：`m_axi_araddr/arid` 从"队列表格多选中 mux"改为预选寄存器
```verilog
wire arq_head_new = do_alloc && ((arq_cnt == 0) || ((arq_cnt == 1) && ar_fire));
assign m_axi_araddr = ar_addr_sel_q;    // 原：arq_addr[arq_rp]（8 级 LUT + MUXF7，9.09 ns）
assign m_axi_arid   = ar_id_sel_q;
if (arq_head_new)  { ar_addr_sel_q <= {al_tag,{(IDX_W+3){1'b0}}}; ar_id_sel_q <= zx_id(al_ent); }
else if (ar_fire)  { ar_addr_sel_q <= arq_addr[arq_rp+1];         ar_id_sel_q <= arq_id[arq_rp+1]; }
```
第一版漏了"`cnt==1` 必须同拍 `ar_fire`"，被 §9(3h) 断言抓到
（`ERROR: arvalid 挂住期间被撤销/改地址 addr=00010000->00010080`），修正后通过。

**第 2 刀**：`pw_valid / pw_word / pw_data` 加 `(* equivalent_register_removal = "no" *)`。
原因：opt_design 的 boundary optimization 会把入口寄存器吸收进引擎侧组合锥
（网表里出现 `u_eng/pw_word[28]_i_2`、`u_eng/be_data[31]_i_9`、`u_eng/u_ce`），
`bf_data` 的 D 端又退回 16 级锥。

## G.2 验证
`wrap` 12/12、`BENCH_LAT=55 wrap` 12/12，**cycles 与 v15 基线逐个相同**
（1345/2791/6123/4122/14307/4635/6871/3470/2762/5304/6792/2176）⇒ 零功能/吞吐影响。

## G.3 结果

| | v14 | **v17** |
|---|---|---|
| WNS @125 | −0.743 | **−0.359** |
| WNS @150 | −2.076 | −2.085 |
| WNS @100 | +1.257 | +1.248 |
| TNS @125 | −581.3 | **−113.7** |
| 违例端点 | 2,318 | **882** |
| LUT / FF / BRAM / DSP | 16,100 / 16,810 / 15 / 107 | 19,249 / 16,716 / 15 / 107 |
| `Synth 8-6859/6858` | 0 / 0 | 0 / 0 |
| bit | 3,510,798 B | 3,510,798 B |

LUT +3,149 是 `equivalent_register_removal=no` 的代价（禁止了一批等价优化）。

## G.4 剩余瓶颈（全在核内）
```
−0.359  g_wram[2].wmem_reg/CLKARDCLK → u_eng/s1_pc_q_reg[2][5]/D   ← C2
−0.340  win_q_reg/CLKARDCLK          → u_eng/s1_pc_q_reg[4][5]/D   ← C2
−0.338  araddr_l_reg[1] → axi_ctrl/xbar/.../m_payload_i_reg        ← AXI-Lite 读地址（非 master AR）
```
`arq_addr → m_axi_araddr` 已从榜上消失 ⇒ 第 1 刀达成目标。
下一刀只能是 C2（`s1_pc_q` 的 popcount 前插一级），需动 `bin_conv_core8.v` 的
`a1..a7_q / v1..v8_q / wrd[gk]` 与 `done` 判定。


---

# 附录 H：linecache vs CAM 的时序对比（四组数，均 @125 MHz）

## H.1 又一例"多驱动"：linecache 的 `lc_tag`（master 曾无法综合）

`bin_unit_axi_wrap.v` 里 `lc_tag[al_idx] <= al_tag;` 同时在
**主状态块（§6.10）** 和 **§6.11 的无复位专用块** 里各写一次 ⇒
`Synth 8-295 found timing loop` + `DRC LUTLP-1 Combinatorial Loop Alert ×1000`
（`lc_tag[103][*]_i_1`）⇒ `place_design` 直接拒绝运行。

修法：删掉主状态块那一行，只保留无复位专用块（RAM 推断需要它）。语义零变化。
（这是**第二次**同类 bug；第一次是 `bin_conv_core8.v` 的 12 个寄存器。
**两次 xsim 都是绿的**，只有综合器能抓到 ⇒ 合并前必须跑 `synth_design`。）

## H.2 四组数（@125 MHz，place Explore / route AggressiveExplore / 2×phys_opt）

| 版本 | 说明 | WNS@125 | 端点 | LUT | DSP |
|---|---|---:|---:|---:|---:|
| v14 | CAM，无刀 | −0.743 | 2,318 | 16,100 | 107 |
| **v17** | **CAM + 两刀** | **−0.359** | **882** | 19,249 | 107 |
| v18 | linecache 纯 | −1.923 | 9,896 | 17,979 | 107 |
| v19 | linecache + 两刀 | −0.946 | 5,762 | 18,328 | 107 |

（v18/v19 均含 `lc_tag` 环修复；致命模式计数 `8-6859/8-6858/8-295/LUTLP-1` 全为 0；
`wrap` 与 `BENCH_LAT=55 wrap` 各 12/12，cycles 与 linecache 基线逐个相同。）

## H.3 结论

1. **linecache 是时序回退**：`v19 − v17 = −0.587 ns`，端点 5,762 vs 882。
   原因：`rd_hit = lc_valid[rd_idx] && (lc_tag[rd_idx]==rd_tag)` 里 `lc_tag` 是
   LC_N 项大阵列，`rd_idx` 由引擎地址散列而来 ⇒ "算散列 → 读阵列 → 比较"这条链
   直接挂在写口 D 端，比 16 项 CAM 更深（LUT 也从 16.1k 涨到 18.0k）。
2. **第 2 刀（`equivalent_register_removal=no`）在两种基线上都有效，linecache 上更值**：
   CAM 基线 +0.384 ns（代价 LUT +3,149）；linecache 基线 **+0.977 ns**（代价 LUT +349）。
3. **125 MHz 的候选是 v17（CAM + 两刀）**；linecache 的价值在性能
   （fill/rd 40,507 → 542、cb=4 那层 20.2×），两者要兼得需把 `lc_tag` 读口 BRAM 化/打拍，
   而 `rd_hit` 不能打拍（§6 有反例）。

## H.4 纪律（已写成工具）
`pl/rtl_hand/tools/synthcheck.sh`：合并前跑 `synth_design -rtl -mode out_of_context`，
grep `Synth 8-6859 / 8-6858 / 8-295 / DRC LUTLP-1 / ^ERROR`。约 4~8 分钟，走 `vrun.sh`。


---

# 附录 I：@100 MHz 上板候选 + E1E2 合并的时序坍塌（2026-10-31 05:20）

## I.1 `lc_tag` / `win_q` 的推断结果（确诊）

```
[Synth] lc_tag_reg | Implied | 512 x 25 | **RAM64M x 144**   ← 分布式 LUTRAM（组合读）
[Synth 8-7052] win_q_reg_* implemented as **Block RAM**（且无输出寄存器可合并）
```
⇒ `rd_hit = lc_valid[rd_idx] && (lc_tag[rd_idx]==rd_tag)` 的链是
**引擎地址 → 散列 → 512 深 LUTRAM 组合读（RAM64M + F7/F8 mux） → 比较 → 写口 D**
⇒ 这是 linecache 比 CAM 慢 0.59 ns 的根因（比较器 16→1，但散列+阵列读两级更深）。

## I.2 `lc_tag` 改 BRAM 为什么不能局部做（重要纠正）

```verilog
assign u_ce = ... || ((!rd_want || rd_hit) && ...)   // rd_hit 在 t 拍组合参与 u_ce
u_rd_data ← win_q[...]                                // BRAM 同步读：t 给地址、t+1 出数据
```
读协议是"**t 拍判命中、t+1 拍给数据**"。若 `lc_tag` 改同步 BRAM，hit 要到 t+1 才有，
t 拍的 `u_ce` 就会用陈旧 hit ⇒ 引擎在 t 拍推进、拿到 t+1 的数据/命中 ⇒ **协议错拍**。
⇒ 必须把引擎的 `mem_rd_en/地址` **提前一拍**发出，那是 `bin_conv_core8.v` 的大改（C2 量级）。

不降 `LC_N`、不动核的可行增量只有：`rd_idx` 的散列**提前一拍**寄存（`rd_idx_q`），
`rd_hit` 仍用当拍 `rd_tag` 比较 —— 可省 1~2 级。

## I.3 两个 @100 MHz 构建

| 版本 | RTL | WNS@100 | 端点 | LUT | BRAM | DSP |
|---|---|---:|---:|---:|---:|---:|
| **v22** | linecache + 两刀（**不含 E1E2**）| **+0.115** | **0** | 18,025 | **30** | 107 |
| v23 | 新 master（linecache + **E1E2**）+ 两刀 | **≈ −5.9**（place −5.727 / route −5.990 / 修后 −5.905）| 大量 | — | — | — |

v23 的路径全落在新结构 `g_conv.u_pack`（`acc_reg[217]` / `lane_reg_*`）⇒
E1E2 那次合并把 wrapper 的 `u_pack` 结构做成了长锥，@100 MHz 都收不住。
⇒ **上板候选 = v22**；E1E2 那份 master 在 `u_pack` 修好前不要用来出 bit。

## I.4 v22 交付
```
board/overlay_engine_150mhz/design_1_150v22.bit   3,510,798 B
board/overlay_engine_150mhz/design_1_150v22.hwh   conv_uni_0=0x40040000 / bin_unit_0=0x40060000
WNS@100 = +0.115  TNS = 0.000  违例端点 = 0  WHS = +0.032
LUT 18,025 / BRAM 30 / DSP 107
致命模式（8-6859/8-6858/8-295/LUTLP-1）= 0/0/0/0
```


---

# 附录 J：`g_conv.u_pack`（E1E2）−5.9 ns 诊断 —— 类型 C（结构错）

## J.1 结论
`bin_e1e2.v` 的转置缓冲 `acc` = `[63:0] acc[0:255]`（`ACC_AW=8` ⇒ 256 项）被写在
**带异步复位的 always_ff** 里 ⇒ Vivado 无法推断 RAM ⇒ **溶解成 16,384 个 FF 的寄存器堆**，
外加"运行时字节通道写"（8 路字节米克斯）+ 256:1×64bit 读米克斯 ⇒ **WNS ≈ −5.9 ns**。

## J.2 证据（v23 综合日志）
```
WARNING: [Synth 8-4767] Trying to implement RAM 'acc_reg' in registers.
   ... bin_e1e2.v:396
Reason: 1) RAM is sensitive to asynchronous reset signal. this RTL style is not supported.
        2) RAM has too many ports (16). Maximum supported = 16.
```
```verilog
 210: always_ff @(posedge ap_clk or negedge ap_rst_n) begin      ← 带异步复位
 307:     if (resp_vld) acc[resp_p[ACC_AW-1:0]][lane*8 +: 8] <= bcmp;
 318:     if (resp_vld) acc[resp_p[ACC_AW-1:0]][lane*8 +: 8] <= bcmp;
```
而文件第 55-56 行自己写着维护规则「转置缓冲 acc 的 always_ff **不带复位**」——
实现违反了它自己的规则（本项目第 3 次踩 `8-4767` 坑）。

路径名印证：最差路径终点是 `u_pack/acc_reg[229][56]` / `acc_reg[217]_*`（寄存器堆命名）；
`lane_reg_n_0_[1]` 被 phys_opt 复制 6 次（8 路字节写使能扇出）。

## J.3 与 v22 的对比
v22 没有 `u_pack`（E1E2 是新合入的单元）——这段工作在 v22 里是 **PS 做的（4.0 s cat 拷贝）**。
所以不是"并行被串起来"，而是**本该是一块 2 KB 的 SRAM，落成了一片 FF**。

## J.4 修法（给 E1E2 所有者）
1. **把 `acc` 的写挪出主 FSM 块**，放进独立的**无复位** `always_ff @(posedge ap_clk)`。
2. **拆成 8 个字节宽阵列**（每 lane 一个 `[7:0] acc_b[0:ADEPTH-1]`，单写口）：
   ```verilog
   always_ff @(posedge ap_clk) if (resp_vld) acc_b[lane][resp_p] <= bcmp;
   assign acc_rd = {acc_b[7][pend_p], ..., acc_b[0][pend_p]};   // 8 个并行读，0 级米克斯
   ```
   ⇒ 8 × 256×8 = 8 块 RAMB18 / LUTRAM，读口并行。
3. 或保留 64 位单阵列，但用 **固定字节使能向量**（`we = 8'b1 << lane`）而不是
   变量部分选择 `acc[..][lane*8 +: 8]`（后者多数综合器不支持）。

## J.5 顺带核出的 `8-4767` 清单（v23）
```
acc_reg（16k FF，必须修）
lsrc_reg / lthr_reg（bin_e1e2，64 项）
pf_tag_reg / arq_addr_reg / arq_id_reg（wrapper，16~64 项）
fq_ent / fq_rem / fq_slot / fq_tag（conv wrapper）· pw_reg（bin_conv_core8）
⇒ 其余 10 处都是几十项的小阵列，"回到寄存器"可接受
```
其余判据：`8-6859/8-6858 = 0`、`8-295/LUTLP-1 = 0`。

**建议把 `8-4767` 加进 `synthcheck.sh`（对 ≥256 项的阵列报警）**：
这类回退在 xsim 里完全看不出来，但会吃掉几 ns。


---

# 附录 K：v24（acc 修复后 + E1E2）@100 MHz —— acc 解决，但 `u_pack` 还有第二个锥

## K.1 六项

| 项 | 值 |
|---|---|
| WNS @100 MHz | **−3.879**（v23 −5.99 ⇒ acc 修复拿回 2.1 ns）|
| TNS / 端点 | −30,416.6 / 10,941 |
| WHS | +0.032 ✅ |
| `u_pack/acc_b` 在时序报告 | **0 次** ✅ |
| LUT / FF / BRAM / DSP | 18,720 / 18,757 / **34**（RAMB36 30 + RAMB18 8）/ 108 |
| 致命四项 / `8-4767` | 0/0/0/0 ／ **10**（`acc_reg` 已消失）|
| `acc_b` 映射（日志实测） | `acc_b_reg[0..7][0] implemented as a Block RAM` × 8 ✅ |

## K.2 新瓶颈：E1E2 的 `issue_addr` 链（类型 A）

族归类（6000 采样）：
```
n=2042 -3.879 bf_data_reg       n=752 -3.851 u_eng/pw_reg     n=464 -3.833 df_addr_reg
n= 257 -3.255 **u_pack/lsrc_reg**  n=256 -3.859 bf_strb_reg  n=205 -3.255 **u_pack/lthr_reg**
n= 161 -3.414 u_eng/e3_sh_q_reg    n=144 -3.833 df_len_reg   n= 68 -2.957 **u_pack/thr_q_reg**
```
最差路径：`u_pack/tile_q_reg[2]` → **CARRY4 × 10（40 位加法器）** → `MUXF7` →
`RAMD64E`（`lsrc/lthr` 8 项 LUTRAM 查表）→ … → `u_bin_unit/bf_data_reg[*]/D`
**23 级 / 13.596 ns（logic 4.745 / route 8.851 = 65%）**

⇒ E1E2 把「tile → issue_addr（40 位加）→ lsrc/lthr 查表 → 地址」全串在一拍，
再直接进包装层写口 ⇒ 把 bf_data/pw_reg/df_addr 三族一起拖到 −3.8。

## K.3 修法（给 E1E2 所有者，三刀都是"提寄存器"）

1. **`lsrc/lthr` 查表结果提前寄存**（8 项、只在 `lane` 变化时变，每 lane 要跑 2^ACC_AW 拍）
   ```verilog
   always_ff @(posedge ap_clk) if (lane_chg) begin lsrc_q <= lsrc[lane_next]; lthr_q <= lthr[lane_next]; end
   ```
   ⇒ 摘掉 `RAMD64E + MUXF7`（省 2~4 级）
2. **`issue_addr` 打一拍**（`tile_q/lane/ph/p` 都是 FSM 寄存器，地址可提前一拍算）
3. **E1E2 的 `mem_wr_*` 输出打一拍**（写口无延迟契约）⇒ 一次解耦 3,500+ 个端点

预期 23 级 → ~10 级，三族回到 −0.5 以内。

## K.4 当晚的上板选择
`design_1_150v22.bit`（@100 MHz，+0.115 / 0 端点 / 无 E1E2）仍然是当晚唯一可上板的版本；
E1E2 需上面三刀之后才能进 100 MHz。


---

# 附录 L：v25（E1E2 三刀后）@100 MHz —— 三刀各中 0.95~1.12 ns，但 `bf_data` 仍是第一

## L.1 七项

| 项 | 值 |
|---|---|
| WNS @100 MHz | **−3.235**（v24 −3.879，改善 0.64）|
| TNS / 端点 | −19,582.5 / 10,611 |
| LUT / FF / BRAM / DSP | 20,296 / 18,849 / 34 / 108 |
| 致命四项 / `8-4767` | 0/0/0/0 ／ 10 |
| `acc_b` 映射 | `acc_b_reg[0][0] implemented as a Block RAM` ✅ |
| bit / hwh | 3,510,798 B ／ `0x40040000` / `0x40060000` |

## L.2 三刀的量化效果

| 族 | v24 | v25 | Δ |
|---|---:|---:|---:|
| `u_pack/lsrc_reg` | −3.255 | −2.137 | **+1.12** |
| `u_pack/lthr_reg` | −3.255 | −2.308 | +0.95 |
| `u_eng/pw_reg` | −3.851 | −2.826 | +1.03 |
| `df_addr_reg` | −3.833 | −2.858 | +0.98 |
| **`bf_data_reg`** | −3.879 | **−3.235** | +0.64（仍第一）|

## L.3 `bf_data` 为什么消不掉：C1 从未实施

v25 最差路径：
```
run_len_reg[3] (u_pack) → issue_en0_carry (CARRY4×4) → … → RAMD64E → … → bf_data_reg[15][1]/D
17 级 / 12.930 ns（logic 3.403 / route 9.527 = 74%）
```
`bf_data` 的 D 端把**写使能**折进去了（`D = CE ? data : Q`，CE 含 `wr_wide_b` ← `u_wr_sel/u_wr_addr`，
也就是 E1E2 的锥）。v17 只做了「防寄存器吸收」，**C1（把 `wr_wide_b` 打拍成 `ww_v_q`）一直没做**。

### 建议：C1+（单元输出捕捉级，wrapper 内一刀解耦四族）
```verilog
always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn)
  if (!s_axi_aresetn) uw_v <= 1'b0;
  else if (u_ce) begin uw_v <= u_wr_en; uw_a <= u_wr_addr; uw_d <= u_wr_data; uw_s <= u_wr_sel; end
// 之后所有下游（wr_take / u_wr_wide_w / p_data / pw_data / push_* / 记账）只用 uw_*
```
写口无延迟契约 ⇒ 多 1 拍无害；五族的源都变成 wrapper 自己的 FF。

## L.4 其余残余
- `lsrc_q + (tile_q<<3)` 的 FF→FF 加法（E1E2 已预告）
- `e2_sn_q / e2_rp5_q / e2_r_q`（220~252 条，−2.0~−2.5）：`bin_conv_core8` 内部寄存器，
  在 v22（≈+1.64@100）里**不违例** ⇒ 现在是被 E1E2 挤出的拥塞/绕线连带


---

# 附录 M：C1+（单元输出捕捉级）—— 功能全绿，但**时序净负**，建议不合并

## M.1 改动（`_worktrees/c1p`，+39/−8）
```verilog
logic uw_v, uw_wide; logic [31:0] uw_a; logic [63:0] uw_d; logic [7:0] uw_s;
always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
    if (!s_axi_aresetn) {uw_v,uw_wide,uw_a,uw_d,uw_s} <= 0;
    else if (start_fire) uw_v <= 1'b0;
    else if (u_ce) begin uw_v<=u_wr_en; uw_wide<=u_wr_wide_w; uw_a<=u_wr_addr; uw_d<=u_wr_data; uw_s<=u_wr_sel; end
    else uw_v <= 1'b0;                       // ⚠ 反压冻结时必须清零，不能保持
end
assign wr_wide_b = uw_v && uw_wide && !start_fire;
assign wr_byte   = uw_v && !uw_wide && !start_fire;
wire [BF_W:0] bf_cnt_pend = bf_cnt + {{BF_W{1'b0}}, uw_v};   // 压力阈值含在飞 beat
assign wr_fifo_press = (bf_cnt_pend >= BF_PRESS) || (df_cnt >= DF_PRESS);
```
守卫变更：
- `wr_all_idle` **加** `!uw_v`（必须：否则最后一个字刚吞下就报 done）
- `p_merge` **不加** `!uw_v`（加了会堵死字节流：uw_v 每拍为 1 ⇒ 全部 got=0）
- `drain_cond` **不加** `!uw_v`（与已有 `!wr_byte/!wr_wide_b` 等价，加了会让小形状 FIFO 只进不出）
- `u_ce` 的两个 `wr_take` 项**保持当拍 `u_wr_en`**（反压语义）

## M.2 功能验收：**全绿**
`wrap` 12/12、`BENCH_LAT=55 wrap` 12/12、`verify_all.sh all` 全绿（核心 5/3/2/2）。

**cycles 不是逐个相同**（这是加 1 级流水的固有结果，无法避免）：
```
bin_1x32 983→918（更快）  bin_1x64 1680→1590（更快）  其余 +1（sam 的两个 +3）
```

## M.3 时序：**净负**
| 版本 | WNS@100 | 端点 | LUT | 族第一名 |
|---|---:|---:|---:|---|
| v25（master）| −3.235 | 10,611 | 20,296 | `bf_data` 2048 |
| **v26 = C1+（留属性）** | **−4.648** | 11,233 | 22,242 | `u_eng/pw_reg` 1468 |
| **v27 = C1+（去属性）** | **−4.648** | 11,233 | 22,242 | 同上（属性对结果无影响）|

v26 族归类：
```
n=1468 -4.645 u_eng/pw_reg        ← engine 内部（bin_conv_core8）
n= 512 -4.567 u_pack/lsrc_reg     ← 反而变差（v25 −2.137）
n= 512 -4.567 u_pack/lthr_reg     ← 反而变差（v25 −2.308）
n= 256 -4.242 u_eng/e2_r_q_reg
n= 253 -4.502 u_eng/e2_sn_q_reg
n= 251 -4.502 u_eng/e2_rp5_q_reg
```
**`bf_data` 从第一（2048, −3.235）彻底消失** —— C1+ 的目标达成；
但 LUT +1,946（44%→48%）带来拥塞，把 `u_eng/pw_reg`/`u_pack/lsrc|lthr`/`e2_*` 一起推到 −4.5。

## M.4 结论与建议
1. **C1+ 不合并**：它是正确的（功能全绿），但时序净负 1.4 ns，且 cycles 无法保持。
2. `equivalent_register_removal=no`（v27 对比）**对结果无影响** ⇒ 这个属性现在可以拿掉省 LUT。
3. 真正的剩余瓶颈已全部转移：
   - `u_eng/pw_reg` / `e2_*`（`bin_conv_core8` 内部）—— 在 v22（无 E1E2）里不违例，是**拥塞连带**
   - `u_pack/lsrc|lthr`（E1E2）—— 第 1 刀只把查表挪成 `lsrc_q`，`lsrc_q + (tile_q<<3)` 的 FF→FF 加法仍在
   ⇒ 下一步应该是 **布局/拥塞治理**（pblock `bin_unit_0`）或 E1E2 侧继续切，而不是再加流水级。


---

# 附录 N：v28（master 3c68d39f：catpack + 去属性）@100 MHz —— catpack 的写口没打拍

## N.1 七项

| 项 | 值 |
|---|---|
| WNS @100 MHz | **−5.766**（TNS −55,450.6 / 端点 14,079）|
| LUT / FF / BRAM / DSP | **23,448（50.75%）** / 20,313 / 34 / **109** |
| 族前 5 | `bf_data 2004 (−5.766)` · `u_pack/lsrc 491` · `u_pack/lthr 489` · `df_addr 464` · `u_eng/pw_reg 283` |
| 致命四项 / `8-4767` | 0/0/0/0 ／ 10 |
| `bin_catpack` 是否进综合 | **是**（日志 237 行 `synthesizing module 'bin_catpack'`；路径里出现 176 次）|
| bit / hwh | 3,510,798 B ／ `0x40040000` / `0x40060000` |

## N.2 根因：catpack 的写输出是纯组合的（与 E1E2 修前同病）

```verilog
// bin_catpack.v:251-254
assign mem_wr_en    = (state == ST_FLUSH);
assign mem_wr_addr  = out_q + ((p0_q + {16'd0, wp}) << 3);   // 16 位加法 + 移位
assign mem_wr_data  = wdata_c;
assign mem_wr_strb  = {8{state == ST_FLUSH}};
```
v28 最差路径：`u_catpack/nb_q_reg[2]_replica → u_bin_unit/bf_data_reg[*]/D`（还有 `/CE`），−5.766。
⇒ 与 E1E2 修前的 −5.9 同构；E1E2 的第 3 刀（`mem_wr_*` 输出打一拍）catpack 也要补。

## N.3 LUT 没有回到 17,300
`v25（有属性、无 catpack）20,296` → `v28（无属性、有 catpack）23,448`（+3,152）。
两个变量同时变化，无法拆分；但 **catpack 自身至少 ~4,000 LUT**（自称 1,049），LUT 首次过半（50.75%）。

## N.4 C1+ 的利弊重新评估
写口上现在有**三个单元**（8-lane 引擎 / E1E2 / catpack）共用 `bf_data/df_addr/be_data/bf_strb`。
每个新单元都会把 `bf_data` 打回第一（E1E2 修完 → catpack 又打回）⇒ C1+ 是"一次解耦三个"的唯一位置，
且功能已验证全绿。两条路：
- **A（先做）**：catpack 补"输出打拍"一刀 ⇒ `bf_data` 回到 −3 量级（同 v25），改动最小
- **B（明天）**：合并 C1+ 并同时治拥塞（pblock / 精简 catpack），代价 +~1,900 LUT

## N.5 上板版本
仍为 `design_1_150v22.bit`（+0.115 / 0 端点 / 无 E1E2、无 catpack）。


---

# 附录 O：v29/v30（catpack 写口打拍）@100 MHz

## O.1 v29 七项
| 项 | 值 |
|---|---|
| WNS @100 | **−5.858**（TNS −56,546.1 / 端点 13,681；v28 −5.766，基本没动）|
| LUT | **20,187（43.69%）**（v28 23,448 ⇒ **−3,261**）|
| FF / BRAM / DSP | 20,418 / 34 / **109** |
| 族前 5 | `bf_data 2019` · `u_eng/pw_reg 573` · `u_pack/lsrc 512` · `u_pack/lthr 512` · `df_addr 464` |
| 致命四项 / `8-4767` | 0/0/0/0 ／ 10 |
| catpack 写口是否还在路径 | **是**：`u_catpack/nb_q_reg[0] → bf_data_reg[7][16]/D` |

## O.2 结论：写口打拍省 LUT，但没切断路径
最差路径 **26 级 / 15.575 ns**，`CARRY4=11 + RAMD64E + MUXF7×2` ⇒ 同一拍里还塞着
catpack 的**写地址加法** `out_q + ((p0_q + wp) << 3)`、**读地址 `issue_addr`** 与 `lsrc/lthr` 查表、
以及 wrapper 的字节路合并米克斯 ⇒ `opt_design` 的 boundary optimization 把单元输出寄存器与 `pw_*` 一起合并了。

## O.3 v30 实验：恢复 `equivalent_register_removal=no` = 零效果
| 版本 | WNS | LUT |
|---|---:|---:|
| v29（无属性）| −5.858 | 20,187 |
| v30（恢复 3 处属性）| **−5.858** | **20,187** |

⇒ 该属性现已完全失效（结果逐位相同）；拿掉它是纯赚，但救不了时序。
真正能切断的是 **C1+（wrapper 级捕捉级）** —— 它当时确实把 `bf_data` 从榜上拿掉了。

## O.4 下一步
1. catpack 补「读地址 `raddr_q`（每拍 +8）」⇒ 26 级里 11 个 CARRY4 应能去掉一半
2. 中期：`bf_data/df_addr/bf_strb` 已被 E1E2、catpack 的锥反复打回三次
   ⇒ C1+ 是唯一"一次解耦所有单元"的位置（LUT 从 43.69% 到约 46%）


---

# 附录 P：v31（catpack 第二刀：地址寄存器化 + keep）@100 MHz

## P.1 七项
| 项 | 值 |
|---|---|
| WNS @100 | **−5.105**（v29 −5.858，改善 0.75；TNS −47,035.2 / 端点 13,534）|
| LUT | **22,674（49.08%）**（v29 20,187 ⇒ +2,487）|
| BRAM / DSP | 34 / **109** |
| 族（按终点）| `u_eng(core) 1736 (−5.098)` · `u_catpack 556` · `u_pack(e1e2) 537` · `df_addr 464` |
| 致命四项 / `8-4767` | 0/0/0/0 ／ 10 |
| **`u_catpack` post-opt FF** | **1,678**（OOC 1,681 ⇒ 少 3 个）⇒ **`keep` 生效，未被吸收** ✅ |

## P.2 最差路径换源了
```
v29: u_catpack/nb_q_reg[0] → bf_data_reg[7][16]/D    −5.858  (26 级 / 11 CARRY4)
v31: win_q_reg_0/CLKBWRCLK → bf_data_reg[8][12]/D    −5.105  ← 读侧
```
⇒ catpack 写数据锥掉出 top ✅；第一名变成 `win_q → bf_data`（既有读侧路径），
而它在 v22（LUT 18,025）里**不违例**。

## P.3 LUT 账
| 版本 | catpack LUT（wrapper 内）| 总 LUT |
|---|---:|---:|
| v29（写口打拍）| ~2,002 | 20,187 |
| v31（`raddr_q/waddr_q/mask_q/nch_q` + `keep`×8）| **5,110** | **22,674** |

⇒ 第二刀用 LUT 换深度（+3,100 LUT），叠加 `keep` 禁止优化 ⇒ LUT 逼近 50%。

## P.4 结论：已转入拥塞域
证据：① 最差路径 `win_q → bf_data` 在 v22 不违例；② `u_eng(core)` 1736 条违例（v22 干净）；
③ LUT 18,025 → 22,674（+26%），绕线占 61~65%；④ 三个单元的锥都已打拍，再加流水级无收益。
⇒ 下一步是 **pblock/分区**（先软约束看结果，再决定是否硬约束/C1+）。


---

# 附录 Q：v22 可复现性 —— **已证实**（自包含快照 + 逐项吻合的重建）

## Q.1 问题
v22（04:49 构建，linecache + 两刀，不含 E1E2/catpack）是唯一经过 227 层 + 5 golden 验证、
能上板、WNS +0.115 的版本。原构建树 `_worktrees/perf2/rtl` 里只有 wrapper 是副本，
其余 22 个文件是**软链到 modules/**，会随 master 漂移。

（我一度判断"不可复现"，这个判断被 mtime 核对推翻：v22 读过的文件 mtime 全部早于 04:49，
晚于它的只有 `bin_unit_axi_wrap.v`（v22 用的是 perf2 副本）、以及**新增**的
`bin_e1e2.v` / `bin_catpack.v`（v22 本来就没有）。）

## Q.2 快照
```
pl/rtl_hand/.snapshots/V22_REPRODUCIBLE/
├── README.md     配方 / 判据 / 构建脚本命名坑
└── rtl/          10 个实体文件（无软链）
      conv_uni_axi_wrap_bd.v  conv_uni_axi_wrap.v  conv_int8_uni.v
      bin_unit_axi_wrap_bd.v  bin_unit_axi_wrap.v (md5 e4bb53cd)
      bin_conv_core.v  bin_conv_core8.v  bin_rprelu.v  bin_sam.v  bin_interp.v
```
⚠️ 不含、也不许含 `bin_e1e2.v` / `bin_catpack.v`。

## Q.3 重建验证（2026-10-31 10:23）
| 项 | v22（原版）| **v22b（快照重建）** |
|---|---|---|
| WNS @100 MHz | +0.115 | **+0.115** |
| TNS / 端点 | 0.000 / 0 | **0.000 / 0** |
| LUT / BRAM / DSP | 18,025 / 30 / 107 | **18,025 / 30 / 107** |
| 致命四项 | 0 | **0** |
| **Register as Latch** | — | **0** |

⇒ 连资源数都逐项相同 ⇒ 构建确定 ⇒ 可长期作为回退基准。
产物：`board/overlay_engine_150mhz/design_1_150v22b.bit`。

## Q.4 两条新纪律
1. **判"拥塞"必须看 `report_design_analysis -congestion`**，不能只看"LUT 占比 + 绕线百分比"
   （高 LUT + 高绕线也可能只是扇出大的逻辑网；两者处方不同：前者改结构，后者动布局）。
   —— 本次 v32 pblock 实验即被"`No congestion windows above level 5`"否掉。
2. **每版都报 `Register as Latch`（必须 0）**：本次 master 上的 64 个 LDCE 是
   `keep="true"` 钉住本该正规化的结构造成的 ⇒ `keep` 既花 LUT 又可能退化成锁存器。
