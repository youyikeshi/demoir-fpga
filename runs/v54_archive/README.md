**可上板（10 ns 约束下 routed WNS = +0.103 ns、TNS = 0、违例端点 0）—— 与 v53 的唯一差异 = `lane16/bin_unit_axi_wrap_v50.v`（`17164dfc` → `5646a9f6`）：收窄 `pix_q` 32→17 位 + 把 `npix/plane_delta` 寄存器化。LUT 涨 1634（21496→23130），全部落在合成器把 `g_conv.u_catpack` 那一块解大的 QoR 抖动上（详见 §4）；DSP/BRAM 不变，活数据通路（u_core）只 +48。**

# v54 存档：v53 + T-123 临界路径修复（收窄 pix_q + npix 打一拍）

> **本轮唯一变量**：`runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v`
> （md5 `17164dfc3029ace1cbcb9eaa08af6213` → **`5646a9f6de1a2ef814f3840308629a28`**）。
> 34/34 其余 `.v` 逐字节相同（`find . -name '*.v' | xargs md5sum` 逐条比对，见 §5）。
> 构建配方、BD、地址图、时钟、约束、P&R 指令**一位不动**（`FUSE_EN=0`、`VLANES=16`、
> band 保底 `C_MAX=8/ROWS=1/HALO=0`、`create_clock -period 10.000`、`place Explore` /
> `route AggressiveExplore` / `phys_opt AggressiveExplore ×2`、`synth AreaOptimized_high`
> `-control_set_opt_threshold 8`）。构建闸门 `memgate.sh 5 --reserve 2.5`（rc=0）。
> 运行点 `xc7z015clg485-2` / Vivado 2025.2.1；**未上板、未 ssh**。

## 〇、一句话结论

v118 报的那条共同临界路径（`r_s_reg[12]/C → g_conv.g_wr_split2.pix_q_reg[22]/D`，10.074 ns）
**在 v54 里彻底消失**：routed **WNS +0.103 / TNS 0.000 / 0 违例端点**，最差 20 条**全部**是核的
旧数据锥（`win_q_reg_11 → u_core/s1_pc_q_reg[7][5]/D`，9.793 ns / 10 级），
写通路（`pix_q`/`npix_q`/`g_wr_split2`）命中条数 = **0**。

## 一、改了什么（两处，均在 `lane16/bin_unit_axi_wrap_v50.v` 的 `g_conv` / `g_wr_split2`）

| # | 位置（v54 行号） | 改动 | 效果 |
|---|---|---|---|
| ① | 1686-1730（声明/位宽）+ 1765-1770（判据） | `pix_q` **32 位 → `PIX_W`=17 位**；`localparam int NPIX_MAX = 102400;`、`localparam int PIX_W = $clog2(NPIX_MAX+1)` ⇒ **位宽不写死** | 32 位加法/比较的进位链缩到 17 位：整设计 CARRY4 **2189 → 2185**；`pix_q` 在网表里 **FD 位数 32 → 17（索引 0..16）** |
| ② | 1729-1742（独立 `always_ff`）+ 1782（用点） | `npix_q`(17b) / `plane_delta_q`(29b FD，`<<3` 后低 3 位常 0) **寄存器化**，在 `c_start` 那一拍采样 | 16×16 乘法（DSP48E1 A→P 3.370 ns）**整段移出** pix_q 路径；op/带内它们是常量 ⇒ 每拍语义零变化 |
| ③ | 1343-1346 / 1586-1588 / 1630 / 1740（安全） | `npix > NPIX_MAX` ⇒ `npix_ovf` 置位（粘滞到下一跑），`npix_q` **饱和**在 `NPIX_MAX`，并经 `u_err` 上报 **AP_RETURN[15:8] = ERR_SIZE(2)** | **不静默截断**；合法形状下恒 0（输出与改动前逐位相同），越界时 PS 可见错误码 |

`err_show` 的一行扩展（`conv_live`）只为把上面这个运行期错误码露到 AP_RETURN —— 合法形状下
`u_err` 恒 0，输出与 v53 **逐位相同**。

## 二、★ 与派工 brief 的一处事实纠正：上界不是 128×128

brief 写"最大 `Hout=Wout=128` ⇒ `npix_max=16384` ⇒ 15 位够"。实测**不成立**：

| manifest | 最大单 op `Hout*Wout` | 形状 |
|---|---|---|
| `ps/manifest_640.json` | **102400** | 640×640 经 s=2 unshuffle ⇒ 320×320 `encoder.conv_first` |
| `ps/manifest_512.json` | 65536 | 256×256 |
| `ps/manifest_608x416.json` | 63232 | 208×304 |
| `ps/manifest_64.json` | 1024 | 32×32 |

⇒ `NPIX_MAX = 102400`，`PIX_W = $clog2(102401) = **17**`。**按 15 位做会把 65536/102400 这两档
形状算错 4 倍/2 倍**（正是 T-105 修过的那类错）。17 位仍比 32 位少 15 位进位链，够用。

## 三、判据

| 判据 | 结果 |
|---|---|
| **P1** routed WNS / TNS / 违例端点 | **+0.103 ns / 0.000 / 0**（setup/hold/pulse-width 全 0 违例；WHS +0.036、WPWS 3.870）；v53 同配方 +0.041 ⇒ **PASS** |
| **P2** LUT / FF / BRAM / DSP | **23130 (+1634, +7.6%)** / 21828 (+66) / **43** / **126** ⇒ LUT **FAIL**，其余 PASS（见 §4） |
| **P3** Register as Latch / LDCE / LDPE | **0 / 0 / 0**（报告 + 网表双查）⇒ PASS |
| **P4** 网表指纹 | `bin_conv_core_v50`=**1**、8-lane 老核 `bin_conv_core`=**0**、`g_wram[*]`=**16**、`g_wr_split2` 相关 cell=**739** ⇒ PASS |
| **P5** `.hwh` | **`5235764602921d03351e121a81e35613`** = v53 存档同一份（BD 未重建）⇒ PASS |
| **P6** `pix_q` 位宽 / `npix_q` 寄存器化 / 新最差路径 | 17 位 / 是 / `win_q_reg_11 → u_core/s1_pc_q_reg[7][5]/D`（9.793 ns）⇒ 见 §6 |
| **P7** 归档 | 本目录 + `MD5SUMS` | PASS |
| **P8** 主线零改动 | `runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v` md5 仍 `17164dfc`；`ps/pynq`、`proj_engine`、`proj_v43base2` 的 tracked 改动数 = **0** | PASS |

DRC（默认规则集，routed DCP）：**Critical 0 / Error 0 / Warning 434**（v53 是 482 Warning）。

## 四、★ LUT +1634 的归因（P2 唯一不达标项）

为了**证明这不是我的改动"本身"变胖**，本会话另跑了一颗**对照位流**（同一份 tcl，只把 RTL 根
换回主线冻结 v53）：`out_ctl53/`、`timing_v53ctl.rpt`、`util_v53ctl.rpt`、`build_ctl53.log`。

| 项 | v53 对照（本会话） | v54 | 差 |
|---|---|---|---|
| 综合后 cell 数 | **52241** | 54316 | **+2075**（⇒ 增长发生在**综合**，不是 P&R） |
| routed WNS | +0.041（9.768 ns / 10 级） | +0.103（9.793 ns / 10 级） | +0.062 |
| Slice LUT / Logic LUT | 21496 / 20714 | 23130 / 22348 | +1634 / +1634 |
| Slice FF | 21762 | 21828 | +66 |
| BRAM / DSP | 43 / 126 | 43 / 126 | 0 / 0 |
| 最差路径 | `win_q_reg_15 → s1_pc_q_reg[14][6]` | `win_q_reg_11 → s1_pc_q_reg[7][5]` | 同一类 |

对照位流与存档 v53 **逐项相同**（21496 / 21762 / +0.041 / 52241）⇒ 流程是确定性的，差值可归因。
层次表（`hier8_ctl.rpt` vs `hier8_v54.rpt`，深度 8）显示增量几乎全在一个节点：
`g_conv.u_catpack` **2337 → 4003（+1666）**，而 `g_conv.u_core` 只 **+48**、`u_unit` +49。

- `u_catpack` 是 **BIN_CATPACK**（float32 多源打包）。它在 `OP_MASK_SUP=16'h0003` 里是**可选**的
  （bit1=cat），但本项目已对它**功能性否决**（板上实测整网更慢：64² 2.69→3.07、256² 15.94→16.34 s），
  现网只发 conv op、运行时不用它；构建脚本对两个 catpack 实例与 1621 个内部时序 cell 做了
  `through/from/to` **false_path**（这一段一字未动）⇒ 它的 LUT 数对 WNS **零影响**，是纯粹的设备占用。
- 它的 LUT 数对"改别处"极度敏感：**变体 A**（把三只采样寄存器并进原有 `always_ff` 的 `c_start`
  分支、不新增 CE／控制集）综合后 cell **52989**（比 v54 少 1327、比基线仍多 748），
  其 `u_catpack` 综合后 LUT = **3012**（基线 2353）⇒ 剩余部分是**综合器 QoR 抖动**，不是我的逻辑。
- 另外注意：opt 之后网表里存在**层次名串味**（例如 `.../g_conv.u_catpack/g_conv.g_wr_split2.pbase_q[31]_i_1`），
  所以层次表的"归属"只能当线索，**总量 23130 才是判据**。

⇒ 结论：**+1634 LUT 全部躺在已否决的 catpack 区，活数据通路（u_core + 写通路包装）只 +48/+49；
FF +66（17+17+29 只新寄存器 − 32 位计数器省下的）**。是否接受这一项由父代理裁决；
若必须压回 ≤21500，需要动的是 catpack 的存废（超出本任务"只改两处"的边界）。

## 五、"只有这一个文件变了"的证据

```
$ diff <(cd runs/timing_v50/rtl_v50 && find . -name '*.v' | sort | xargs md5sum | sort -k2) \
       <(cd worktrees/T-123-timing/rtl_v50 && find . -name '*.v' | sort | xargs md5sum | sort -k2)
33c33
< 17164dfc3029ace1cbcb9eaa08af6213  ./lane16/bin_unit_axi_wrap_v50.v
---
> 5646a9f6de1a2ef814f3840308629a28  ./lane16/bin_unit_axi_wrap_v50.v
```

`rtl_v50_manifest.txt` 与 `runs/timing_v50/rtl_v50_manifest.txt` 逐行 diff 只差这一行；
构建脚本的 0a 第②条用本目录的 manifest 核对（MATCH=35 DIFF=0），并额外加了 **0d 硬断言**
（wrap md5 + `NPIX_MAX/PIX_W/npix_q/plane_delta_q/npix_ovf` 五个标记必须同时在场，否则拒绝构建）。

## 六、P6：位宽与"高位是否还参与逻辑"的网表证据

`width_cmp_v54.log`（两张 routed DCP 同口径查询，只数 FD\* cell）：

| 信号 | v53 | v54 |
|---|---|---|
| `pix_q_reg*` | **32 位（索引 0..31）** | **17 位（索引 0..16）** |
| `npix_q_reg*` | 0（不存在，npix 是组合线） | **17 位（索引 0..16）⇒ 已寄存器化** |
| `plane_delta_q_reg*` | 0 | **29 位（索引 3..31；`<<3` 的低 3 位是常量 0，被合成器省掉）** |
| `pbase_q_reg*` | 32 位 | 32 位（地址，未变） |
| `npix_ovf_reg` | — | **FDCE ×1（安全位真的进了网表）** |

⇒ 原 32 位里的 **bit[31:17] 共 15 位在网表里根本不存在**（不是被优化掉，是从未被声明），
17 位全部参与语义（`102400 < 131072`）。整设计 CARRY4 2189→2185，写通路从最差 20 条里**清零**。

⚠ 严格说：**不能**按 brief 的"14/15 位"收窄 —— 320×320 会溢出。

### 6b、逐笔仿真等价（`sim/`，本轮补做）

复用 `runs/v94_wrpath/tb_v94_wrap.v`，**两份 RTL 只有根不同**（v53 主线 / v54 worktree），
同一 TB、同一 golden、同一 plusargs（`T94LOG` 记录每一笔 `(addr,data,sel)`）：

| shape | v53 | v54 | 结论 |
|---|---|---|---|
| `v94_c48_o64_32x32`（npix=1024，k=3，cout=64） | beats **8192**，PASS=21/FAIL=0，bytes=65536/65536，cycles=**55774** | beats **8192**，PASS=21/FAIL=0，bytes=65536/65536，cycles=**55774** | **逐笔完全一致** |
| `v94_c96_o96_8x8`（cout=96 ⇒ 6 组 2 平面） | beats **768**，PASS=21/FAIL=0，cycles=**6153** | beats **768**，PASS=21/FAIL=0，cycles=**6153** | **逐笔完全一致** |

⇒ ① 写地址/数据/字节使能**一拍不差**；② **总拍数完全相同**（连 cycles 都一样）⇒
"npix 打一拍"确实是**零周期代价**（不是"多一拍但看不出来"）；③ 幅度 8192 笔覆盖了
npix=1024 的回绕与 6 组 plane 切换。

局限：这两条形状都是 s=1（TB 的 golden 语料只有 s=1）；s=2 的 `npix=Hout*Wout` 语义由
T-105 负责，本轮没有改它，`inv_lint` 的 I1/I1b 仍 PASS。

## 七、本目录文件

- `design_1_150v54.bit` / `design_1_150v54_routed.dcp` / `design_1_150v54.hwh`
- `util_v54.rpt` / `timing_v54.rpt` / `timing_v54_worst20.rpt` / `drc_v54.rpt` / `oldcone_v54.rpt`
- `dcp_check_v54_build.log`（Z5 指纹）/ `z5_mul_v54.log`（npix 乘法器专查）
- `post_pixq_v54.log` + `width_cmp_v54.log`（P6 的网表证据）/ `post_v54_pixq2.tcl` / `width_v54.tcl`
- `hier8_ctl.rpt` / `hier8_v54.rpt`（§4 的层次对比）
- `util_v53ctl.rpt` / `timing_v53ctl.rpt` / `build_ctl53.log`（同会话 v53 对照位流）
- `build_v54.log` / `build_v54_memgate.out` / `inv_lint_v54.log`（PASS 15 / FAIL 7 / WARN 3，与 v53 完全一致，`I1` PASS）
- `build_final_v54.tcl` / `build_ctl53.tcl` / `drc_v54.tcl` / `sim_equiv.sh`
- `sim/`（§6b：v53 vs v54 的逐笔仿真日志 + 每笔 (addr,data,sel) 记录）
- `build_v54_manifest.txt`（bit ↔ 源码 md5 清单）
- `rtl_v50/` + `rtl_v50_manifest.txt`（本轮的 RTL 快照）
- `MD5SUMS`

## 八、未做 / 待父代理决定

1. **未上板、未 ssh**（另一子代理在用板子）；功能 A/B 未做 ⇒ "可上板"仅指时序/DRC/指纹门禁通过。
2. 功能等价性 = 静态（`inv_lint`）+ 网表（位宽/指纹/时序）+ **逐笔仿真**（`sim/`，两条形状 0 差异）；
   仍未覆盖 s=2 形状与融合（FUSE_MODE=1）路径 —— 那些不属本轮改动面。
3. P2 的 LUT 项（+1634，全在 catpack）需要父代理裁决；压缩它超出本任务边界。
