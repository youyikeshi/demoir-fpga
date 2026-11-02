# v50_simA —— IMP-A `bin_conv_core.v`（VLANES 参数化）xsim 验收报告

工具：`/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin/{xvlog -sv, xelab, xsim}`（Vivado 2025.2.1）
被测：`pynq_specialized_code/pl/rtl_hand/_worktrees/v50_impA/bin_conv_core.v`（919 行，md5 e2ed125f…）
基准：`runs/v42e_archive/rtl_v39_exact/bin_conv_core8.v`（896 行，md5 4e9887a9…）
**未改动任何 RTL**（`md5sum` 前后一致）。

产物（`runs/v50_simA/`）：`tb_bin_conv_core_v50.v`、`run.sh`、`xvlog.log`、`xelab.log`、
`xsim.log`（24 组全量）、`xsim_dump.log` + `v50_simA.wdb`（波形取证）、本报告。

---

## 0. 判据总览

| 判据 | 内容 | 结果 |
|---|---|---|
| **S1** | `VLANES=8` vs v39 原版，逐 bit（addr+data+sel） | **24/24 组 0 diff，1389 笔写事务全部逐 bit 相同** ✅ |
| **S2** | `VLANES=16` 一次 vs `ceil(cout/8)` 趟 8-lane，逐 lane | **24/24 组 0 diff，9977 个 lane 比较全等** ✅ |
| **S3** | 4 组边界（全 0 / 全 1 / 单 bit / cout=13 非 8 倍数） | **4/4 全过** ✅ |
| **S4** | 错误 + 优化清单 | **2 个真错（F1/F2）+ 5 条优化** ↓ |
| **S5** | `xelab` 对 `VLANES=16` 能否展开 | **能**：`Compiling module work.bin_conv_core(VLANES=16)`，0 error 0 warning ✅ |

**S1 结论（IMP-A 的回归责任）**：`VLANES=8` 的改动是**严格等价**的——包括 `prm_addr` 的
`if-chain` 重写、`pw[0:15]` 索引、`wrsel_q` 的 `nvalid_q[LVW-1:0]`、`prm_i` 位宽。
特别地，`prm_addr` 里 `{27'd0,i} << 3` → `i << 3` 的**窄化没有改变任何参数地址**（否则
`pw` 内容会变，1389 笔写事务不可能全等）。随机输入覆盖 cin∈[57,192]（含 cin%64≠0）、
cout∈{3,5,8,13,16,17,24,32}、k∈{1,3}、s∈{1,2}、d∈{1,2}、pad∈{0,1}、shift∈[0,7]。

**S2 结论**：16 lane 一次 与 8 lane 两趟（o0, o0+8，参数基址按 8 lane 平移、权重按
`8p·K·8` 平移）**数据完全等价**；cout=16 的 6 个 case（cid 7/12/13/19/20/21/22）全 0 diff。
⇒ 16-lane 的写侧 128 位（`mem_wr_data[127:0]`）拼接顺序 = lane0 低字节 … lane15 高字节，
与"两趟 8-lane 拼两个 64-bit 字"一致。

---

## 1. 错误类发现（**不要自己改，报给主代理 / IMP-A**）

### F1【真错 · 16-lane 新增缺陷】组平面步长写死 8 字节/像素

`bin_conv_core.v:695`（v39 同一行 672，但 8 lane 时正好正确）

```verilog
hwout8_q <= ({16'd0, hout_q} * {16'd0, wout_q}) << 3;   // ← 8 = VLANES？不是，是字面量 3
...
756:  ogbase_q <= ogbase_q + hwout8_q;                    // 每组输出平面步长
```

`hwout8_q` 被当**每组输出平面的字节步长**用（`ogbase_q` 累加），它必须是
`Hout·Wout·VLANES` 字节。写死 `<<3`（=8 字节/像素）在 `VLANES=8` 时正确，
**`VLANES=16` 时只有正确值的一半**。

- 触发条件：`cout > VLANES`（多组）。`cout=16` 单组时**不触发**（所以 S2 的 cout=16 全过）。
- 仿真证据（`xsim.log`，cout=17，h=5,w=8 ⇒ P=40）：
  `[S2 cid=1] t=40 ADDR exp=280 act=140 (group=1 pix=0)` —— 第 2 组第 0 像素写到
  0x140 = 40·8，应是 0x280 = 40·16。全量统计：**278 笔地址错**。
- 后果：第 g+1 组从 `g·P·8` 开始写 16 字节/像素，与第 g 组平面**重叠一半** ⇒
  **前一半输出被后一组覆写**（cout=17 时 40 像素 × 16B 的组 0 平面里有 320 B 被组 1 踩掉）。
  推论（未单独仿真）：若 `P·8` 不是 16 的倍数，128 位写还会落到**非 16 字节对齐**地址上，
  包装层做 2 拍 AXI 拆分时会错位。
- 修法（供 IMP-A 参考，我没动）：`hwout8_q <= ({16'd0,hout_q} * {16'd0,wout_q}) * VLANES;`
  并把名字改成 `plane_stride_q`。
- 归类：**IMP-A 的扩展缺陷**（v39 只有 8 lane，`<<3` 恰好等于 VLANES）。

### F2【真错 · v39 继承缺陷】最后一组的 `nvalid` 用了**旧的 `o_q`** ⇒ 末组掩码恒为全 1

`bin_conv_core.v:758`（v39 同位置 735，**逐字相同**）

```verilog
o_q      <= o_q + VLANES;                       // 755：非阻塞，RHS 里 o_q 仍是旧值
...
nvalid_q <= ({16'd0, cout_q} - o_q > VLANES)    // 758：用了旧 o_q！
            ? VLANES : ({16'd0, cout_q} - o_q);
```

进入 ST_PRM/ST_CMP 的那一组，`nvalid` 应等于 `cout − (o_q + VLANES)`。用旧 `o_q`
算出的值永远是 `cout − o_q_old`，而 `o_q_old + VLANES ≤ cout` 才有多组 ⇒ 结果恒
`> VLANES` ⇒ `nvalid_q = VLANES` ⇒ `mem_wr_sel` 全 1。

- 仿真证据（`xsim.log`，cout=13，8 lane）：
  `[S1 cid=1] MASK(v39) tx80 g=2 got=ff exp=01` / `MASK(new8) tx80 ... got=ff exp=01`
  —— **v39 与新版逐条相同**，证明是继承缺陷、不是本次回归。
  全量：**v39 = 167 处，new8 = 167 处**（完全一致）；16-lane = **186 处**。
  `[S2 cid=1] OVERRUN ... lane=1..15 (oc=17..30 >= cout=17) 写了非法字节 byte=7f/80`
  —— 16-lane 在 cout=17 时第 2 组应只写 1 字节，实际写满 16 字节，**共 2230 个非法字节**。
- 触发条件：`cout % VLANES ≠ 0`（8 lane：cout=3/13/17/24；16 lane：cout=3/13/17/24/…）。
  cout 是 8/16 的整数倍时**不触发**（这也是为什么它一直没被发现——C8 布局通常 padding 到整组）。
- 修法（供参考）：`nvalid_q <= ({16'd0,cout_q} - (o_q + VLANES) > VLANES) ? VLANES
  : ({16'd0,cout_q} - (o_q + VLANES));`
- 归类：**v39 继承缺陷**，但 16-lane 扩大了受害面（cout=17 是 16-lane 最自然的分组）。

---

## 2. 其余检查项（结论：未发现问题）

| 检查项 | 方法 | 结论 |
|---|---|---|
| X 传播到写口 | 每个 `mem_wr_en` 采样 `$isunknown(addr/data/sel)` | 24 组 **0 次** |
| 读地址 X | `$isunknown(mem_rd_addr)` when `mem_rd_en` | **0 次** |
| 未初始化寄存器 | 读码 + 仿真 | `pw`/`p_snap`/`d_c` 上电为 X，但分别被 `state>=ST_CMP`、`ps_vld` 闸住，从未到达写口；`wmem`（BRAM）无复位但每 lane 的 `0..K-1` 全写满后才读 |
| **末块补零双侧消隐** | 结构断言（E3）：末块（`b==C_B-1`）时 `act_c` 补零位必须 = 1、`wdata_c` 补零位必须 = 0 | **act-pad-bad=0，wgt-pad-bad=0** ⇒ 双侧消隐正确，`XNOR(1,0)=0` 贡献为 0 |
| `nvalid` 掩码边界 | 见 F2 | **有错（F2）** |
| `p_acc` 溢出 | 位宽推导 + 仿真 | 无害：`p ≤ 64·K ≤ 64·256 = 16384 < 2^15`，`sum_c` 16 位足够；`d16` 断言（`|d|≤32767`）24 组未触发 |
| ST_CMP 气泡 | 每状态拍数计数 | **无气泡**：`cmp 拍数 ≡ P·K`（CASE 20：648 = 2×36×9；CASE 23：1260 = 2×35×18），像素/term 之间无空拍；空拍只来自越界 term（`oob` 计数） |
| 参数化健壮性（**边界提醒**） | 读码 | `logic [3:0] wl_lane`（:207）与 `cmp_init` 的 `wl_lane == VLANES-1` ⇒ **只支持 VLANES ≤ 16**；写 `VLANES=32` 会**永远进不了 ST_CMP（挂死）**。若将来要 32 lane，这个字段必须参数化 |

---

## 3. 优化点（每条带证据来源 + 确认/推测 + 预计影响）

拍数统计（`xsim.log` 的 `STAT` 行，取 CASE 23：cin=100 ⇒ C_B=2, k=3, K=18, P=35, cout=13）：

```
8-lane : prm=34(rd32) wld=576(rd288) cmp=1260(rd988) drn=26 oob=272
16-lane: prm=33(rd32) wld=576(rd288) cmp=630 (rd494) drn=13 oob=136
```

| # | 建议 | 证据来源 | 确认/推测 | 预计影响 |
|---|---|---|---|---|
| **O1** | **权重装载从 2 拍/字改成 1 拍/字**（当前读端口在 WLD 期恒为 50% 占空比） | **波形/计数**：`wld=576` 拍只有 `rd=288`；代码 :716-741 的 `wl_ph` 抖动；反面证据 :699 `pw[prm_i-1] <= mem_rd_data` 证明**同一条读口在 PRM 期就是 1 字/拍**（wmem 写口与读口独立，不存在结构冲突） | **确认**（代码+计数） | 组装载时间 **−50%**。小图显著：CASE 23 每（组）累计 948 拍里装载占 288 ⇒ **−15% 总拍数**；128×128 (P=16384) 时 <1% |
| **O2** | **`wrsel_q` 寄存器化**：`(1<<nvalid)-1` 是 16 位变量桶形（4 级 LUT）+ 减法，直出 `mem_wr_sel`，在写口组合路径上；而 `nvalid` 每组恒定 | 代码 :595-598 | 推测（逻辑级数需综合确认） | 写口组合深度 **−4~5 级** |
| **O3** | **PRM 帧合并/常驻**：每组重读 32 字（16-lane），末尾还有 1 拍空转（prm 33 拍 / rd 32） | 计数 `prm=33(rd32)`；代码 :688-706（`prm_i == PRM_W` 的空转拍） | 确认（计数） | 每组 −33 拍（CASE 23 ≈ 总拍数 3.5%）；cout 分组多（192/16=12 组）时累计 −396 拍 |
| **O4** | **越界 term 跳拍**：k=3、pad=1 时边界像素的越界 term 仍各占满 1 拍（只是不发读） | 计数 `oob=272/1260 = 22%`（CASE 23）、`136/648 = 21%`（CASE 20）；6×6 小图实测 42% | 确认（比例随尺寸变） | 小特征图 CMP 段最多 **−40%**；112×112 只有 ~3.5% ⇒ **优先级低**，但深度 layer（k=3 多、图小）值得做。改动大：地址链是单一累加器，跳拍要按行重算 |
| **O5** | **`prm_addr` 组合 if-chain 换成段内 `+8` 累加器**：它每次 PRM 拍都串在 `rd_addr_q` 的更新路径上（4 级比较 + 加法 + 两次移位） | 代码 :286-311；PRM 期有 1 拍空转可用来算段边界 | 推测 | 读地址路径 −2~3 级 |
| (O6) | 若实际 `cfg_shift ≤ 15`，`SHW` 从 6 降到 4，省 2 级桶形 × VLANES | 代码 :137-150；参数默认 6（支持 0..63） | 推测（要确认契约） | epilogue S7 组合深度 −2 级 |
| — | **无需优化**：`ST_DRN` 12 拍（`DRN_MIN`）与流水线尾深（`r1/s1/ps/v1..v8` = 11 级）基本吻合，不是浪费 | 代码 :451-454 + 计数 `drn=13` | 确认 | — |

关键路径相关备注：epilogue 的 S2（`>>>9`）与 S5（`>>>8`）已是**静态移位**（0 LUT），
唯一的变量桶形是输出 `cfg_shift`（S7）⇒ VLANES=16 时是 **16 条 6 级桶形并行**，这是
16-lane 面积/时序的主要增量（推测，未综合）。

---

## 4. S5：xelab 对 VLANES=16 的展开（IMP-A 未做，本报告首次）

```
$ xelab -debug typical tb_bin_conv_core_v50 -s simA
Starting static elaboration ... Completed
Compiling module work.bin_conv_core8_default
Compiling module work.bin_conv_core_default
Compiling module work.bin_conv_core(VLANES=16)     ← 16 lane 首次展开成功
Built simulation snapshot simA
```
0 ERROR / 0 WARNING（除一条与本设计无关的 `XSIM 43-3431` 环境变量提示）。
⇒ `$clog2`、`generate for (gk<VLANES)`、`for (int k<VLANES)` 在 16 lane 下全部合法，
16 块 `wmem`（256×64）与 128 位写口正常展开。

---

## 5. 复现

```bash
cd runs/v50_simA && ./run.sh            # 24 组全量（约 2 s）
cd runs/v50_simA && ./run.sh dump 1     # 1 组 + 波形 v50_simA.wdb
```

TB 说明：4 个 DUT 共用一块 byte RAM（输入区只读、输出平面按实例分开），
`mem_rd_data` 1 拍延迟、写按字节使能；S2 的"两趟 8-lane"用参数基址/权重基址平移实现
（= 包装层 o0/o0+8 串行化的等价物）。日志自动统计 S1/S2 diff、F1 地址错、F2 掩码错、X 计数。
