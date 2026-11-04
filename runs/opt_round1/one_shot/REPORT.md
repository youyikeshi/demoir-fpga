# one_shot：v60 数值补丁（R1/R2/R3）+ 三时钟域解耦 —— 改动点 / 仿真 / 构建

- 日期：2026-11-04 ｜ 器件 `XC7Z015-2CLG485I` ｜ Vivado 2025.2.1（全部经 `memgate.sh` 单实例）
- 依据：`runs/opt_round1/ps_numeric/RTL_SPEC.md`（R1/R2/R3 位精确规格）、
  `runs/opt_round1/rtl_coexist/REPORT_v3.md`（扫描器 v3）、`DEPLOY.md`（容器配对铁律）
- 本轮**没有**改 PS 代码（`pynq_specialized_code/ps/`）、没有动 `runs/ACCEPT_v57/`、
  没有动 `runs/hdmi_v58/` 的冻结产物、没有 `git add -A` / push

---

## 0. 结论（三句）

1. **R1（epilogue 四舍五入）按 R1-A 落地、数据通路零改动**：半个 LSB 并进 `gamma`
   （`gh_l = gamma + H`，在进 CMP 的那条边打一拍），规格 17 组向量里**恰好 5 组**
   （第 6/7/8/10/12 行）行为改变，其余 12 组逐位不变 —— 实测量化一致。
2. **R2（阈值 K=6）只改 6 行**：`bit = (sext(x)·2^6 + thr_k > 0)`，16 位加法器不加宽
   （上界 8192+5249=13441），`<<<` 常量移位 0 LUT；512 bit 定向向量的红绿都拿到了背书。
3. **R3(b) 越界贡献 0 + 三时钟域解耦**：越界项用 `pc←0 + n_oob·cin`（0 内存），
   扫描器拆成 AXI-Lite / 扫描 / 像素三个域，cfg 走 4 相 req/ack 整捆握手；
   `slope_shift` 按主代理裁决**回退 8**（12 是净退步），只有这个数字变了，别的一律不动。

---

## 1. 五块改动点（逐块 diff 在 `diff/`，红绿日志在 `logs/`）

| # | 改动 | 文件 | 做法（可逐行核对） | 代价 | diff |
|---|---|---|---|---|---|
| ① | **R1-A 四舍五入** | `bin_conv_core8.v` §8b / S3 / S5；`bin_rprelu.v` §6 | `H=(shift==0)?0:1<<(shift-1)`；**不改 S1~S8**：在 `cmp_init` 那一拍把 `gh_l[k] <= gamma_l[k] + H` 打进 8×32 FF，S3 的 `rp=dd+gh`、S5 的 `neg=sn+gh` 吃它；分支判据仍吃 `dd`（`e2_ddpos_q`）。rprelu 侧同样在 `gamma_g` 装载时 `+half_c` | +8 个 32 位加法器（输入准静态，不在逐像素路径）+256 FF | `01_core8…`、`04_rprelu…` |
| ② | **R2 阈值 K=6** | `bin_e1e2.v:387` | `xs_c[j] = sext8(byte) <<< 6`；`bcmp[j]=en_q[j] && ((xs_c[j]+thr_q[j])>0)`（**严格 `>0`** 不变） | **0 LUT**（常量移位）、加法器仍 16 位、thr 表同槽同大小 | `02_e1e2_R2.diff` |
| ③ | **R3(b) 越界贡献 0** | `bin_conv_core8.v` §10/§11 | `s1_pc_q[k] <= (OOB_ZERO && !r1_rd) ? 0 : pc_c[k]`（每 lane 一个 7-bit AND）；`r1_oobtap = (b==0) && !in_bounds` → `oob_cnt/oob_snap`（**唯一装载点**，见 §7 断言）；`d = 2p − Ntot + n_oob·cin`（`cin_q` 新锁 1 个 16 位） | ≈56 LUT + 1 个 5×16 小乘法 + 16 FF；**0 内存** | `01_core8…` |
| ④ | **slope_shift** | `bin_conv_core8.v` 参数 `SSH`、`bin_rprelu.v` 参数 `SSH`、`bin_unit_axi_wrap.v` 复位/校验 | 按主代理裁决：**回退契约值 8**（曾按规格做过 12 并全部仿真绿，主机 2×2 实测证明 12 是净退步 ⇒ 撤销）；`SSH` 参数化保留 | 0 | `03_wrapper…`、`04_rprelu…` |
| ⑤ | **三时钟域解耦** | `hdmi_fb_scan.v`（+`scan_clk`，新增 §0 桥）、`hdmi_out_top.v`（+1 口）、`hdmi_v60_top.v`（+1 口）、BD、`build_final_75v60.tcl`、`hdmi_v60_clock_groups.tcl` | AXI-Lite 寄存器块留 `s_axi_aclk`（FCLK0）；取数 FSM / AXI4 读主 / 行缓冲写口搬 `scan_clk`（FCLK1）；cfg = **整捆 + 4 相 req/ack**（1 深 shadow ⇒ 不丢写、不撕裂）；`soft_reset`/`st_clr` = toggle 事件 + 3FF + 展宽 8 拍；status = 1 bit 事件/电平 + bundled toggle；`UNDERFLOW_CNT` 计数器整体在 aclk（跨域只剩 1 bit） | +≈60 LUT/FF | `05_fbscan…`、`06_…`、`07_…` |

**两个编译期开关**（`bin_conv_core.v`/`bin_conv_core8.v` 参数，默认全 1 = v60 语义）：
`R1_ROUND`、`OOB_ZERO`。=0 时逐字节回到 v58 行为 —— 变异测试与 A/B 就靠它们（§5）。

---

## 2. S1 扫描器：三档 + 独立扫描域 + 重武装（全绿）

判据：丢行 0、错字节 0、alias 0、`0x2C`=0、`cfg_req` 重武装通。
时钟口径（v60）：`aclk` = FCLK0 真值 **50.79 MHz**（`AXI_HALF_PS=9845`）；
`scan_clk` = FCLK1 真值 **≈100 MHz**（`SCAN_HALF_PS=5000`）。

| 用例 | aclk | scan_clk | 行数 | 丢行率 | 错字节 | alias | 0x2C | 结论 |
|---|---|---:|---:|---:|---:|---:|---:|---|
| S1a-37.11（单域口径，scan=aclk） | 37.109 MHz | 37.109 MHz | 1200 | **0** | 0 | 0 | 0 | ✅ |
| S1a-50.00（单域口径） | 50.000 | 50.000 | 1200 | **0** | 0 | 0 | 0 | ✅ |
| S1a-66.67（单域口径） | 66.667 | 66.667 | 1200 | **0** | 0 | 0 | 0 | ✅ |
| S1b-scan37.11（独立域） | 50.79 | 37.109 | 1200 | **0** | 0 | 0 | 0 | ✅ |
| S1b-scan50.00（独立域） | 50.79 | 50.000 | 1200 | **0** | 0 | 0 | 0 | ✅ |
| S1b-scan66.67（独立域） | 50.79 | 66.667 | 1200 | **0** | 0 | 0 | 0 | ✅ |
| **S1c 重武装**（enable 0→1） | 50.79 | 100.0 | 3000+ | **0** | 0 | 0 | 0 | ✅ `REARM_PASS` |

日志：`logs/gates_console.log` + `sim/logs/repro_*_v60_s1*.log`。复跑：§9。

> **本轮在 S1 里发现并修掉的两个非 RTL-数值问题**（都写进 §7 的风险）：
> ① 测试台必须从"扫描域真实生效点（`p_en`）"放行视频，而不是数固定 aclk 拍 ——
> 否则在"扫描域更慢"的档位会先放行、首场 vsync 到来时 `p_en` 还是 0、第 0 行从未被请求；
> ② 由此暴露了 v3 的一个**上电空洞**：第一次 vsync 之前 `uf_en=0`，`pix_valid` 会声称
> 有效（读到未写过的 BRAM）。已加 `&& uf_en`（1 个 LUT）补掉。

---

## 3. S2 数值：R1 17 组 / R2 512 bit / R3 越界对照（红绿都有）

### 3.1 R1 的 17 组位级向量（走**真实 RTL 通路**，不是测副本）

做法：把 `alpha=0 / bias=0 / zeta=0 / slope=0`，于是 `dd=0` ⇒ 走负支 ⇒ `r ≡ gamma`，
再用 `cout=8` 一次跑 8 个 r 值 ⇒ 引擎输出恒等于 `sat8((r+H)>>>shift)`，即规格表的 r。
向量与期望全部由 `sim/gen_r1_vectors.py` 生成（`gold_r1vec/_vectors.tsv`）。

| 形状 | shift | r | 期望（新） | 绿 | 红（`R1_ROUND=0`） |
|---|---:|---|---:|:--:|---|
| r1vec_s0 | 0 | 0 / 127 / −128 / 128 | 0 / 127 / −128 / 127 | ✅ | ✅ 同值（shift=0 不加半） |
| r1vec_s1 | 1 | 0 / 1 / −1 | 0 / **1** / **0** | ✅ | ❌ **2 组变红** |
| r1vec_s4 | 4 | 8 / 7 / −8 / −9 | **1** / 0 / **0** / −1 | ✅ | ❌ **2 组变红** |
| r1vec_s6 | 6 | 32 / −33 | **1** / −1 | ✅ | ❌ **1 组变红** |
| r1vec_s8 | 8 | 32512 / 32767 / −32768 / −32769 | 127 / 127 / −128 / −128 | ✅ | ✅ 同值（饱和段） |

⇒ **17 组里恰好 5 组（第 6/7/8/10/12 行）是差异点，且只有这 5 组在变异下变红**：
与 RTL_SPEC §R1 的断言逐条对上。日志 `logs/num_green_r1vec.log` / `num_red_r1vec.log`。

### 3.2 R2：512 bit 定向向量（含 x=0 正负阈值两类）

用例：一条 E1E2 命令（`c=64, h=1, w=8, group=0` ⇒ 8 lane × 8 通道 × 8 像素 = 512 bit），
内存镜像与期望由 `sim/r2_e1e2_test.py` 独立生成（模型就是规格式 `x·2^6 + thr_k > 0`）。
定向项覆盖：

| 情形 | x | thr_k | 期望 bit |
|---|---:|---:|---:|
| ReLU 精确零 + 正阈值 | 0 | 37 | **1** |
| ReLU 精确零 + 负阈值 | 0 | −37 | **0** |
| 阈值恰 0（严格 >0） | 0 | 0 | **0** |
| 恰 −0.5 LSB（tie） | 1 | −64 | **0** |
| 恰 +0.5 LSB（tie） | −1 | 64 | **0** |
| 略过 tie | 1 / −1 | −63 / 63 | **1 / 0** |
| 亚 LSB 阈值 × 小 \|x\| | −2..2 | ±1,±19,±32,±63 | 由模型算 |
| 全量程 | 127/−128 | ±5249 | 由模型算 |

结果：**512/512 一致，`nerr=0`**（`logs/r2_green.log`）；
把 RTL 的 `<<< THR_K` 改成 `<<< 0`（= 老行为）⇒ **RED**（`logs/r2_mutant.log`，
典型：`x=1,thr=−63 → got 0 / exp 1`、`x=127,thr=−5249 → got 0 / exp 1`）。

> ⚠ 一句话写清（主代理要求）：**R2 在 `BIN_PACK=e1e2` 档只把符号翻转从 9.11% 降到
> 2.16%；正确档必须用 `BIN_PACK=ps` 打包**（残差根因是本轮不解决的"f32 张量以什么精度进 PL"）。

### 3.3 R1 + R3（+ slope 口径）整卷积对照：8 个形状全绿 / 变异全红

参考模型 `sim/gen_v60_golden.py`（由仓内 `gen_bin_golden.py` 派生，**独立实现**三处语义：
`r+H`、越界 tap 不计通道且 `+n_oob·cin`、`slope_q=round(slope·2^8)`），生成 6 个真实卷积形状
+ 2 个 shift 边界形状，用真实 `bin_conv_core8` 逐字节比对：

| 形状 | 覆盖点 | 绿（`R1/OOB` 全开） | 红：`R1_ROUND=0` | 红：`OOB_ZERO=0` |
|---|---|---:|---:|---:|
| `bin_1x32_32_8x8` | k=1、pad=0（**无**越界）、cin 非 64 倍数 | ✅ 2048/2048 | ✅ 不受影响 | ✅ 不受影响 |
| `bin_1x64_16x16` | k=1 | ✅ 4096/4096 | ❌ 变红 | ✅ 不受影响 |
| `bin_3x32_48_8x8` | k=3/pad=1（有越界） | ✅ 3072/3072 | ❌ 变红 | ❌ 变红 |
| `bin_3x32_48_d2` | dilation=2/pad=2（越界更多） | ✅ 2048/2048 | ❌ 变红 | ❌ 变红 |
| `bin_3x96_64_8x8` | CB=2（跨 64 通道边界） | ✅ 4096/4096 | ❌ 变红 | ❌ 变红 |
| `v105_c48_o96_32x32_s2` | s=2 跨步、cout=96 多组 | ✅ 24576/24576 | ❌ 变红 | ❌ 变红 |
| `v60_3x64_16_8x8_s0` | **shift=0**（不加半） | ✅ 1024/1024 | ✅ 不受影响 | ❌ 变红 |
| `v60_3x64_16_8x8_s8` | shift=8（最粗） | ✅ 1024/1024 | ❌ 变红 | ❌ 变红 |

结论：`R1_ROUND=0` ⇒ **7/8 形状红**；`OOB_ZERO=0` ⇒ **6/8 红**（两个不受影响的是
`pad=0` 的形状 —— 没有越界像素，本来就不该变，这正是"变异只在有越界处生效"的正面证据）。
日志 `logs/num_green.log` / `num_red_r1.log` / `num_red_oob.log`。

### 3.4 R3 的等价性口径（为什么"逐字节"就够）

参考模型与 RTL 走的是**两条不同的式子**：
- RTL：`pc←0`（越界 tap 的 pc 记 0）+ `d = 2p − Ntot + n_oob·cin`；
- 模型：越界 tap 的通道**既不进 p 也不进 Ntot**，等价于 `d = 2p − (Ntot − n_oob·64·C_B)`。
两者恒等，因为 `−64·C_B + cin = −n_pad`，而 `n_pad·k²` 这一项两项都保留（bias 里已抵）。
⇒ 上述 6 个有越界的形状**逐字节 0 差**就是 (b) 路线成立的实证。

---

## 4. S3 跨域：cfg 必达 + status 计数不丢（三档时钟比，全绿）

TB 给两个真正独立的时钟（含非整数比 + 相位差），用例：
8 次快速 `CTRL` 写（间隔 3..42 拍，故意跨过 `fs_busy` 窗口）+ 8 次 `FRAME_ID` 写 +
终值一次，然后判：

| 判据 | 含义 | s3a（scan=100.0） | s3b（scan=74.2，非整数比 1.4646） | s3c（scan=38.5，**scan 比 aclk 慢**） |
|---|---|---:|---:|---:|
| `s_en/p_en` = 1/1 | cfg **必达**扫描域与像素域 | ✅ | ✅ | ✅ |
| `fid_latched` = 最后写入值 | 32 bit 帧号跨域不丢不乱 | ✅ `a50000ff` | ✅ | ✅ |
| 后 3 帧 `cov` = 1.000000 | 终值真的生效 | ✅ | ✅ | ✅ |
| `underflow_cnt` == TB 独立计数 | **计数不丢**（逐行相等） | ✅ 0 == 0 | ✅ | ✅ |
| 相位差 | `SCAN_PHASE_PS` | 1234 ps | 777 ps | 2500 ps |
| 判定 | | `CDC_PASS` | `CDC_PASS` | `CDC_PASS` |

日志 `logs/gates_console.log`（`[CDCWAVE] VERDICT CDC_PASS` ×3）。
`underflow_cnt` 这一项在无欠载时是"0==0"，为防"零测"，§5 的变异用**极端档**（scan=37.11
+ 1 px/拍）把欠载打到 399 行，并验证 DUT 计数 399 == TB 计数 399（见 `MUT_NO_POP2` 日志
`V6_uf_cnt=399`，TB 侧 `underflow: DUT=399 TB独立计数=399`）。

---

## 5. S4 变异：每块删一处 ⇒ 判据变红；恢复 ⇒ 绿

| 块 | 删/关了什么 | 判据 | 红 | 绿 |
|---|---|---|---|---|
| R1 | `R1_ROUND=0` | 17 向量 / 8 卷积形状 | `red_r1vec` 3 形状红（恰好 5 组向量）；`red_r1` 7/8 红 | `green_r1vec` 5/5、`green` 8/8 |
| R2 | `<<< THR_K` → `<<< 0` | 512 bit | `r2_mutant` RED（≥10 处不一致） | `r2_green` 512/512 |
| R3 | `OOB_ZERO=0` | 8 卷积形状 | `red_oob` 6/8 红 | `green` 8/8 |
| 扫描器 | `-d MUT_SINGLE_BUF`（退回单缓冲） | alias / 错字节 | `V3_alias=128000`、`V2_databad=500355` | 同频正常 RTL `alias=0 / bad=0` |
| 扫描器 | `-d MUT_NO_POP2`（退回 1 像素/拍） | 丢行率（scan=37.11） | `LOST_RATE=99.75%`、`uf=399` | 正常 RTL `0.0000%`、`uf=0` |

日志：`logs/num_red_r1.log`、`logs/num_red_oob.log`、`logs/num_red_r1vec.log`、
`logs/r2_mutant.log`、`logs/mut_singlebuf.log`、`logs/mut_pop1.log`、`logs/good37.log`。

---

## 6. 构建（一次综合）——**已跑完**，数字见 §10

构建脚本 `pl/scripts/build_final_75v60.tcl`（由 `build_final_75v59.tcl` 派生，改 7 处：
BD 换 `proj_hdmi_v60`、两个 md5、外壳换 `hdmi_v60_top`、新增 **FCLK1 10.000 ns** 约束、
门禁 2 扩成 FCLK0+FCLK1（FCLK1 另要折算 Fmax ≥133 MHz）、hwh 从设备名、输出目录）。
门禁沿用：LUT ≤ 22000、BRAM ≤ 41（v57 35 + 6）、DSP == 101、latch 0、DRC 0、pix 三档 WNS > 0。

> 状态：**已完成**（2026-11-04 16:25 BD `BD_MERGE_V60_OK` / 16:48 位流落盘）。
> 位流 `runs/hdmi_v60/design_1_v60.bit`（SW_CRC `dd2da11c`）；
> 资源 LUT 21768 / BRAM 35 / DSP 102 / latch 0 / DRC 0；
> 时序 FCLK0 **−1.810**、FCLK1 **+1.705**（Fmax 120.6 MHz）、pix +0.724/+7.454/+20.919。
> **6 条门禁未过的逐条性质见 §10.2/§10.3/§10.4**（2 真 / 2 口径过期 / 1 新目标未达）。
>
> BD 手术过程中三次失败并修掉的原因（eng/ 目录、PS7 的 `PCW_EN_CLK1_PORT`、
> BD 41-237 的 CLK_DOMAIN、v58 的 PCW 红线白名单）以及**构建 RTL 来源必须显式覆盖**
> （否则数值补丁不进网表）都记在 §10.6。

---

## 7. 未解决项 / 风险（明确写死）

1. **R2 的 2.16% 残差**（本轮不解决）：E1E2 吃的是生产者落地的 int8 张量，
   `interp`/`add` 这类 f32 算子产出在进 PL 前已经被量化；PS 打包能直接对 f32 比阈值。
   ⇒ **正确档必须 `BIN_PACK=ps`**；`e1e2` 档只把符号翻转 9.11%→2.16%。
2. **容器必须配套**（不在本轮范围，主代理另派代理）：
   R1 上 RTL 后容器要改回"无偏移"（否则双重补偿，DEPLOY 铁律 1）；
   `slope_shift` 回退 8 后容器需 `pack_slope12` 以外的常规档（即原 8 位档）。
3. **v39→v57 的 `bin_e1e2` 唯一差异（时序刀 7）**：`p<run_len` 组合比较改成
   ST_THR 边界装载的 `rem` 倒计数器 + 寄存使能 `sw_act_q`。
   - 结论：**没有**对时钟比 / 响应乱序 / outstanding / 预取深度敏感的改动，它不碰 AXI，
     也不假设固定延迟（`resp_vld<=issue_en` 的 1 拍假设 v39 就有）。
   - **候选脆点（本轮不动）**：`rem`/`sw_act_q` **只有 ST_THR 一个装载点**
     （已核：全文件 `state <= ST_SWEEP` 仅 1 处，line 584）。任何未来新增"旁路进 SWEEP"
     都会让 `rem` 变陈旧 ⇒ **静默多发/少发读**。改动很小、风险很高，建议另开一轮。
4. **`n_oob` 的单一装载点纪律**（照 3 的教训执行）：`oob_cnt` 只在 §10 的块里被驱动，
   `s1_l0_q`（像素首 term 复位）/ `s1_oobtap_q`（越界 tap 起始 +1）两个条件互斥；
   快照 `oob_snap` 与 `p_snap` **同拍同块**。仿真断言在 §15（见下）。
5. **`hdmi_out_top.v` 的 md5 变了**（v58 冻结模块，`27cda94d…` → **`bf857b14…`**）：
   只加了 1 个 `scan_clk` 端口 + 1 根线，逻辑 0 改动；`hdmi_fb_scan.v`
   `3eefa62f…` → **`32599295…`**；外壳 `hdmi_v58_top.v`(0cd0b065…) →
   `hdmi_v60_top.v`(见 `diff/07`)。三个 md5 已同步进 `build_final_75v60.tcl` 与
   `bd_merge_hdmi_v60.tcl` 的断言。
6. **BD 里端口名变了 ⇒ 必须重建 cell**：`bd_merge_hdmi_v60.tcl` 用的是
   `delete_bd_objs` + `create_bd_cell -type module -reference hdmi_v60_top`，
   创建时从 Verilog 重新推端口（不是原地改 `.bd` 的端口表）⇒ 新端口 `scan_clk` 会被
   一起推出来。这也是本轮坚持"重建而不是 update_ip"的原因。
7. **`FCLK1` 的约束口径**：约束 10.000 ns（硅片真值 ≈100 MHz），而 PYNQ 侧会写 150。
   若硅片上 FCLK1 实际按 150 MHz 跑（除以那个"报数系数"后不是 100），实际可用频率
   是 **133.33 MHz（DIV1+1=8）或 96.97 MHz（÷11）** 这类档，**不是整数 100**；
   门禁因此额外要求折算 Fmax ≥ 133 MHz，把"10 ns 约束太松"的空间收掉。
8. `FCLK0` 约束**保持 10.000 ns 不放**（比真值 50.79 MHz 严 2×），沿 v58 口径。

---

## 7b. v3 遗留缺陷（**不是 v60 引入**，本轮顺手修掉）

1. **上电空洞**：`hdmi_fb_scan` 的 `pix_valid = in_vde && p_en && !uf_now`，
   而 `uf_en` 要等到**第一次 vsync** 才置 1。在"配置已生效、第一个 vsync 还没来"的窗口里，
   像素落在未写过的行缓冲上却仍被声称 `pix_valid=1` ⇒ 首帧第一行读出 `xxx`（1280 字节）。
   v58 之所以没暴露，是因为测试台把视频发生器**从行中间**放行，而 v58 单时钟域下
   "配置提交完成"与"放行时刻"的相对相位恰好让它落在消隐区。
   **修法**：`pix_valid` 再加 `&& uf_en`（1 个 LUT）。判据：S1 三档 + 独立域三档
   所有"丢行/错字节/alias"仍为 0。
2. **测试台用"时间"代替"状态"**：TB 原本按"数 `CFG_H+40` 个 aclk 拍"来等配置生效。
   v60 起配置提交跑在**扫描域**（独立自由时钟），数拍会在"扫描域更慢"的档位提前放行。
   **修法**：改成等 `p_en`（跨域后的真实生效点）。这是"用状态而不是用时间"的写法。

---

## 7c. 下一步候选（本轮**不实现**，等主代理的位宽二分表）

> 决策理由（主代理口径）：目前**没有数据**证明加宽能修 DC 偏置 ⇒ 不拿"+N 很便宜"去赌
> "数值一定变好"。二分结果若证明有效，下一轮做 `v61` 只加这一项，改动面小、可归因。

### 候选 ①：q 域整体加宽 N 位（**0 LUT / 0 DSP / 0 FF**）

| 项 | 内容 |
|---|---|
| 改动点 | ① `bin_conv_core8.v`：`parameter int ASH = 9` → `9 − N`（S2 的 `qsh_c = e1_prod_q >>> ASH`，一行常量）<br>② 容器：`alpha_shift` 从 9 → 9−N（**α 本身不动**，只改 q 的定标）+ 驱动下发的 `shift` 同步 −N（`q_frac_bits` 增加 N，R1 的 `H` 自动跟随）<br>③ 无其它 RTL 改动 |
| 位宽自证 | `|r| ≤ 28332 · 2^N`（实测 `max|r| = 28332`）；32 位有符号加法器余量 11 位 ⇒ **N ≤ 6**；N=6 ⇒ `|r| ≤ 2^20.8`，S3/S5/S6/S7 与 `sat8` **全部不用加宽** |
| 下游乘积 | `pn = dd · slope_q`：`|dd| ≤ 2^20.8`、`|slope_q| ≤ 9990 < 2^14` ⇒ `|pn| ≤ 2^34.1 < 2^48`（`e2_pn_q` 48 位）✔ |
| 代价 | **0**（唯一真正可变的移位是 S7 的输出桶形，与 q 域无关） |
| 风险 | 全在容器重标定与 `shift` 的联动；RTL 侧只有 1 个常量 |

### 候选 ②：`alpha_q` 整数位宽加宽（**int18 仍 1 个 DSP/lane**）

| 路线 | DSP（8 lane） | 合计 DSP | 相对门禁 102 | 结论 |
|---|---|---|---|---|
| 现状 int16 | 1/lane = 8 | 101 | 余 1 | — |
| **int18**（±131071） | **1/lane**（DSP48E1 是 **25×18**）= 8 | **101** | **余 1** | ✅ **唯一可行** |
| int32 | 2/lane = 16 | 109 | 超 7 | ❌ 不可接受 |

- 其它代价（int18 路线）：PRM 表每组 `pw[0..1]`（2×64b = 8×int16）→ `pw[0..3]`（4×64b = 8×int32）
  ⇒ **PRM 装载 +2 拍/组**（每组 17 拍，另有 2·8·K 拍权重装载，K≥1 ⇒ 相对开销 <1%）；
  参数解码 **+128 FF**；S1 的 B 操作数接 18 位（0 LUT）。
- **口径更正**：`alpha_q` 是 **per-oc（cout 个）不是 per-channel** ⇒ 容器增量
  ~**+2 B/oc**（整网 ~19 K 个 oc ⇒ **+38 KB 量级**），不是 22.5 KB（那个数按 per-channel 估的）。
- `slope_q`：`|·|max = 9990 < 2^14` ⇒ int16 **本来就够，不需要动**。若要 +1 位分辨率
  只需 `SSH 8→9`（常量移位、0 面积），但主机 2×2 已证明"分辨率不是杠杆"（12 是净退步）⇒ 不建议。

---

## 8. 附：q 域加宽的改动点 / 代价评估（**只评估，不实现**，等主代理的位宽二分表）

主代理给的实测约束：`|α_q|max=32721`（int16 到顶）、`|slope_q|max=9990`、
`|bias_q|=18198`、`|ζ_q|=345`、`|γ_q|=424`、`max|r|=28332`（14.8 bit）。

### 8.1 两件不同的事，别混

| 杠杆 | 改什么 | 代价 |
|---|---|---|
| **(A) q 域整体加宽 N 位** | 只是把 S2 的常量右移从 `ASH=9` 改成 `9−N`（容器侧把 α/bias/ζ/γ 同步重标定） | **0 LUT / 0 DSP / 0 FF**（唯一真正可变的移位是 S7 的输出桶形，与 q 域无关） |
| **(B) α 的整数位宽加宽** | `alpha_q` 从 int16 变成更宽（值域 32721 已顶到 int16） | 见 8.3 —— **int18 仍是 1 个 DSP**，int32 才翻倍 |

(A) 的**位宽证明**（用主代理的数）：`|r| ≤ 28332 · 2^N`。32 位有符号加法器余量 11 位
⇒ **N ≤ 5~6 安全**；N=6 时 `|r| ≤ 2^20.8`，S3/S5/S6/S7 的加法器与 `sat8` 全部**不用加宽**。
下游 `pn = dd · slope_q`：`|dd| ≤ 2^20.8`、`|slope_q| ≤ 9990 < 2^14` ⇒ `|pn| ≤ 2^34.8 < 2^48`
（`e2_pn_q` 是 `EW+16 = 48` 位）**仍然够**。⇒ (A) 的代价是 **0**，风险只在容器重标定。

### 8.2 (A) 的改动点（如果主代理要上）

1. `bin_conv_core8.v`：`parameter int ASH = 9` → `9 - N`（或新增 `QSH` 参数，S2 的
   `qsh_c = e1_prod_q >>> ASH` 一行）；
2. 容器：`alpha_q/bias_q/zeta_q/gamma_q` 全体左移 N 位（**值域要重新验**：bias 18198·64
   = 1.16 M ≪ 2^31 ✔；α 32721·64 会溢出 int32 的装载路径吗？α 只乘 d）——
   **注意 α 不能乘 2^N**（那等于改 α 的值域），只有 q 域的*小数*位变；正确做法是
   `alpha_shift` 从 9 改成 9−N，α 本身不动 ⇒ 只有 `q` 的定标变；
3. 驱动 `shift`（输出 shift）要同步 −N（因为 `shift = q_frac_bits − act_fs_out`，
   `q_frac_bits` 增加了 N）；R1 的 `H = 1<<(shift-1)` 自动跟着变；
4. 碰撞检查：`d16`（`|d| ≤ 2^14`）不受影响 ✔；`e1_prod_q` 是 32 位、`d16×α16 ≤ 2^30`
   ✔；S1 乘法不变。

### 8.3 (B) 的代价：**int18 不花 DSP，int32 才花 8 个 DSP**

- `d16`（16 位有符号）× `alpha_q`：DSP48E1 的乘法器是 **25×18**。
  ⇒ `alpha_q` 加宽到 **18 位**（值域 ±131071，足够 32721 再×4）**仍然 1 个 DSP/lane**，
  8 lane **0 增量**；本设计 DSP 现值 101、门禁 ≤102 ⇒ **只有这条路能走**。
- 若必须 int32（值域 ±2^31）：16×32 要 **2 个 DSP**/lane ⇒ +8 DSP = **109 > 门禁 102**，
  必须先从别的引擎腾 DSP 或放宽门禁（都不可接受）。
- α 加宽的**容器/装载侧**代价：α 现在是 per-oc（cout 个，不是 per-channel），
  PRM 表每组的 `pw[0..1]`（2 个 64-bit 字 = 8×int16）要变成 `pw[0..3]`（4 字 = 8×int32）
  ⇒ **PRM 装载 +2 拍/组**（每组目前 17 拍、另有 2·8·K 拍权重装载，K≥1 ⇒ 相对开销 <1%），
  参数解码寄存器 +2×64 = **+128 FF**，S1 的 B 操作数接 18 位（0 LUT）。
  容器侧 `alpha` 每层 +2 B/oc（整网 ~19 K 个 oc ⇒ **+38 KB** 量级，不是 22.5 KB ——
  22.5 KB 那个数看起来是按 **per-channel** 估的，α 实际是 per-oc，请以此为准）。
- `slope_q`：`|·|max = 9990 < 2^14` ⇒ int16 **本来就够**，不需要动；
  若要 +1 位分辨率，把 `SSH` 8→9 即可（常量移位，0 面积），
  但 2×2 实测已经证明"加分辨率不是杠杆"（12 是净退步）⇒ **不建议**。

### 8.4 一句话建议

`q` 域加宽是**零面积**的（(A)），风险全在容器重标定与 `shift` 的联动；
`α` 加宽若走 **int18** 也是**零 DSP**的（(B)），但 PRM/容器要跟着改。
两条都上 = 动 `bin_conv_core8.v` 的 2 个常数 + 容器重打包，**不动 DSP、不动流水级**。

---

## 9. 复跑（全部经 memgate 单实例）

```bash
cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code
export PATH=/data/xilinx/2025.2/2025.2.1/Vivado/bin:$PATH
OS=../runs/opt_round1/one_shot

# S2 数值（R1 卷积 / R1 17 向量 / 红绿）
tools/memgate.sh 2 -- bash $OS/sim/run_v60_num.sh green
tools/memgate.sh 2 -- bash $OS/sim/run_v60_num.sh red_r1 -d R1_OFF
tools/memgate.sh 2 -- bash $OS/sim/run_v60_num.sh red_oob -d OOB_OFF
GOLD=$PWD/$OS/gold_r1vec SHAPES="r1vec_s0 r1vec_s1 r1vec_s4 r1vec_s6 r1vec_s8" \
  tools/memgate.sh 2 -- bash $OS/sim/run_v60_num.sh green_r1vec

# S2 R2（E1E2 512 bit + 变异）
python3 $OS/sim/r2_e1e2_test.py --rtl $PWD/pl/rtl_hand/modules/bin_e1e2.v \
    --tb $PWD/pl/rtl_hand/tb/tb_bin_e1e2.v --work /tmp/v60_r2/green --tag r2_green

# S1 + S3（扫描器三档 / 重武装 / 跨域）
tools/memgate.sh 2 -- bash $OS/sim/run_gates_v60.sh all

# S4 扫描器变异
DEFS="-d MUT_SINGLE_BUF" NLINES=400 VAR=v60_mut_sb AXI_HALF_PS=9845 SCAN_HALF_PS=7500 \
  CASES="9845:mut_singlebuf" tools/memgate.sh 2 -- bash $OS/sim/run_scan_v60.sh

# BD（FCLK1）+ 一次构建
tools/memgate.sh 4 -- pl/scripts/vrun.sh pl/scripts/bd_merge_hdmi_v60.tcl ../runs/hdmi_v60/bd_merge.log 5400
tools/memgate.sh 6 -- pl/scripts/vrun.sh pl/scripts/build_final_75v60.tcl ../runs/hdmi_v60/build.log 9000
```

---

## 10. 构建结果（v60，**一次综合**；2026-11-04 16:48）

产物：`runs/hdmi_v60/`：`design_1_v60.bit`（3 510 798 B，md5 `db43e55aef20f3028250d749257def26`）、
`design_1_v60.hwh`、`design_1_v60_routed.dcp`、`util.rpt` / `util_hier.rpt` / `timing.rpt` /
`drc.rpt` / `gate_summary.txt` / `build_v60_manifest.txt` / `MD5SUMS`。

### 10.1 数字（口径 = `report_utilization` / `report_timing_summary`，`util.rpt` 的 Slice LUTs 行）

| 项 | v57 | **v60** | delta | 门禁 | 判定 |
|---|---:|---:|---:|---|---|
| **SW_CRC（设计指纹）** | — | **`dd2da11c`** | — | — | — |
| Slice LUTs | 20255 | **21768** | +1513 | ≤ 22000 | ✅ |
| Slice Registers | 18961 | 21760 | +2799 | — | — |
| Block RAM Tile | 35 | **35** | 0 | ≤ 38 | ✅ |
| DSPs | 101 | **102** | +1 | ≤ 102 | ✅（+1 = R3 的 `n_oob·cin` 5×16 小乘法落在 1 个 DSP48E1） |
| Register as Latch | 0 | **0** | 0 | = 0 | ✅（`util.rpt` + LDCE/LDPE 网表双查） |
| DRC Errors | 0 | **0** | 0 | = 0 | ✅ |
| `FCLK_CLK0` WNS @10 ns | +0.206 | **−1.810** | −2.016 | > 0 | ❌ |
| `FCLK_CLK1` WNS @10 ns | —（新） | **+1.705** | — | > 0 | ✅ |
| `FCLK_CLK1` 折算 Fmax | —（新） | **120.6 MHz** | — | ≥ 133（目标） | ❌（目标未达） |
| `clk_pix_a` WNS | +2.951 | **+0.724** | −2.227 | > 0 | ✅ |
| `clk_pix_b` WNS | +9.688 | **+7.454** | −2.234 | > 0 | ✅ |
| `clk_pix_c` WNS | +23.161 | **+20.919** | −2.242 | > 0 | ✅ |
| BUFGCTRL 站点 | 8 | **9** | +1 | == 8（v58 口径） | ❌（**口径过期**：FCLK1 自带 BUFG） |

### 10.2 6 条门禁未过的**性质**（不是"数字不达标"一句话）

| 门禁 | 数字 | 性质 | 说明 |
|---|---|---|---|
| 门禁2 FCLK0 WNS/TNS/端点 | −1.810 / −597.671 / 756 | **真**（不是全准静态 CE） | 见 10.3 |
| 门禁2b FCLK1 Fmax | 120.6 MHz | **真**（目标未达，但 WNS>0） | 见 10.4 |
| 门禁6 BUFGCTRL | 9 | **口径过期** | v58 的"期望 8 = 2 + 6"是在**只有一个** PL 时钟域时写的；v60 的 FCLK1 自带一个 BUFG（`PCW_FCLK_CLK1_BUF=TRUE`）⇒ 正确期望是 9。HDMI 侧 6 个站点一个没多（`util_hier` 的 hdmi 层次 LUT 1073 ≤ 1200 也说明没有重复例化） |
| 门禁8 DSP | 102 | **口径过期** | v58 写的是 `== 101`；主代理本轮给的门禁是 **≤ 102** ⇒ 实为通过（脚本已在事后改成 `∈[101,102]`，见 §10.5） |

### 10.3 FCLK0 的 −1.810 ns 是什么路径（`pl/scripts/audit_v60_paths.tcl`，只读复核 routed DCP）

- 756 个违例端点：**CE=331 / D=177 / 其它=248** ⇒ **不属于**"全部落在准静态寄存器 CE 引脚"的可容忍类别。
- 最差路径（−1.810 ns，22 级逻辑，9×CARRY4，数据通路 11.549 ns，其中走线 7.003 ns = 60%）：
  `bin_unit_0/…/g_conv.u_pack/desc_q_reg[3]_replica/C`（**E1E2 的描述符基址寄存器**）
  → `bin_unit_0/…/u_bin_unit/pf_tag_reg[*]/CE`、`pf_cnt_reg[*]/D`（**包装层预取 tag/计数**）
  即"描述符→读地址→包装层的 tag 比较→预取使能"这条**既有**长链。
- **这条链的 RTL 本轮一个字节都没改**（R2 只改 `bin_e1e2` 的 `bcmp` 比较级；包装层只改了
  `slope_shift` 的常量比较）。v58 在同一约束下只有 **+0.206 ns**（本来就是"贴着线过"），
  v60 +2799 FF / +1513 LUT 把余量吃掉 ⇒ 变成 −1.81。
- **在真实运行点（FCLK0 真值 50.79 MHz = 19.69 ns）**：slack = −1.81 + 9.69 = **+7.88 ns**。
  板上 PYNQ 设 `fclk0_mhz=75`（真值 50.79）⇒ 这个位流在实际工作点是安全的。
- 若要"10 ns 口径也过"，可行且便宜的一处（下一轮，**本轮不做**）：把 E1E2 的 `issue_addr`
  （`desc_q + …`）**打一拍寄存器**再进读地址口 —— 但它是 v57 冻结构造，改它要重跑
  106 命令逐字节对拍，风险/收益不对等；或者按项目纪律 §4.1 把 FCLK0 的约束改成**真值 19.69 ns**。

### 10.4 FCLK1 的 120.6 MHz

WNS **+1.705 ns @10 ns** ⇒ 折算 Fmax **120.6 MHz**，比"目标 133 MHz"低 9.3%，但比
"真值 100 MHz"高 20.6%。板上可用的 PS7 档位是 133.33 / 96.97 MHz 这个量级（不是整数 100），
⇒ **板端请把 `fclk1_mhz` 设到 96.97（或 PYNQ 报数 150 对应的真值 ≈100）这一档**，
不要设到 133.33 档。

### 10.5 两次口径修正（**没有放宽任何质量门禁**，都是"门禁定义随设计变化"）

1. `门禁8d DSP`：`== 101` → `∈[101, 102]`。理由：主代理本轮明确给的是 **DSP ≤ 102**，
   而 +1 来自 R3 新增的 `n_oob·cin` 小乘法（5×16 落在 1 个 DSP48E1，非引擎乘法器变化）。
   下界仍守 101（少于 101 说明引擎被优化掉）。
2. `门禁6 BUFGCTRL`：v60 应为 **9**（多了 FCLK1 自己的 BUFG）。此条**未在做出的这份构建里生效**
   （脚本在构建启动时已载入），所以 `gate_summary.txt` 里仍是"期望 8 / 实测 9"的红色记录 ——
   **如实保留**，不粉饰。

### 10.6 构建链路上五个"差点白跑"的点（逐条可复现）

1. **`hdmi_v60/rtl/eng/` 目录**：两个引擎 wrapper 必须放在 `rtl/eng/` 下（脚本按
   `$RTL_H60/eng/<w>.v` 找），放在 `rtl/` 根下 ⇒ BD 在 `add_src` 处崩。
   md5 必须与 v58 的同名文件一致（`bin_unit_v57_wrap b7fc4cf8…` / `conv_uni_v57_wrap b33a6751…`）。
2. **PS7 的 FCLK1 使能参数**：不存在 `PCW_USE_FCLK1`。正确是
   **`PCW_EN_CLK1_PORT=1` + `PCW_FPGA1_PERIPHERAL_FREQMHZ=150` + `PCW_FCLK_CLK1_BUF=TRUE`**
   （从 `design_1_ps7_0.xml` 里查出来的，别再猜）。
3. **BD 41-237 CLK_DOMAIN 不匹配**：外壳的 `s_axi_aclk` 必须**去掉** `ASSOCIATED_BUSIF` 里的
   `m_axi`，并把新的 `scan_clk` 声明成 `ASSOCIATED_BUSIF m_axi` ⇒ BD 才会把 `m_axi` 的
   `CLK_DOMAIN` 推成 FCLK1，与 `pc_conv_hdmi/S_AXI`、`ps7/S_AXI_HP2` 对上。
4. **v58 的 PCW 红线**：`ps7 除 HP2 外 0 变化` 这条白名单要把 `*CLK1*` / `*FPGA1*` 放进去
   （FCLK1 一改，PS7 会连带刷新 `CLK1_FREQ` / `FCLK1_DIVISOR0,1` /
   `ACT_FPGA1_PERIPHERAL_FREQMHZ` / `FCLK_CLK1_BUF` / `FPGA_FCLK1_ENABLE` 共 8 个派生量）。
5. ★ **构建的 RTL 来源（最要命）**：v57 的 12 个引擎 RTL 默认从
   `runs/ACCEPT_v57/04_RTL/rtl_v57/`（**冻结副本**）读。照旧读的话，**本轮的 R1/R2/R3/slope
   一处都进不了网表**（只有 HDMI 侧生效），而所有门禁仍然全绿 ⇒ 典型的"绿灯假货"。
   `build_final_75v60.tcl` 里已加 5 条**显式覆盖 + md5 硬断言**（= 打过 S2 的字节）：
   `bin_conv_core8 fcd3611b…` / `bin_conv_core 4e618e41…` / `bin_e1e2 2835fc49…` /
   `bin_rprelu 503374af…` / `bin_unit_axi_wrap d93fd0cc…`；其余 7 个仍对 v57 manifest 逐字节全中。
   ⇒ **验收方式**：构建日志里必须有 5 行 `v60 覆盖：<file> <- …/modules（md5 …）`。

### 10.7 bit ↔ 源码 manifest 校验（纪律 §8.2）

```bash
cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code
md5sum -c ../runs/hdmi_v60/build_v60_manifest.txt      # 32 条全 OK（2 条是注释行）
```
（注意：manifest 里的相对路径以 `pynq_specialized_code/` 为基准 —— 与 v58 同口径。）

---

## 11. 按实际工作点重报（主代理裁决：只重报、不重建）+ 位流含新 RTL 的网表证据

### 11.1 FCLK0 按真值 19.6875 ns 重报（`open_checkpoint` routed DCP，未重跑实现）

脚本 `pl/scripts/report_at_runpoint.tcl`；报告 `runs/hdmi_v60/timing_at_19p6875ns.rpt`；
日志 `runs/hdmi_v60/runpoint.log`。

| 口径 | 周期 | WNS | TNS | 违例端点 | 判定 |
|---|---:|---:|---:|---:|---|
| **工作点（真值 50.79 MHz）** | **19.6875 ns** | **+7.878 ns** | **0.000** | **0** | ✅ **PASS** |
| 10 ns 严格口径（**保留、不删**） | 10.000 ns | −1.810 ns | −597.671 | 756 | ❌ FAIL（**已知项**：待 E1E2 `issue_addr` 打拍） |

两行都已写进 `runs/hdmi_v60/gate_summary.txt`（`gate2b_fclk0_at_runpoint_*` 与
`gate2_fclk0_at_10ns_wns_ns`）——**10 ns 那行不许删**，它会一直显示为已知项，直到修掉。

同一份 DCP 复核其余三点：
`FCLK1 +1.705 ns（Fmax 120.6 MHz）`、`clk_pix_a +0.724`、`clk_pix_b +7.454`、`clk_pix_c +20.919`。

### 11.2 ★ 位流里真的含 v60 新 RTL —— 三条证据

**① 构建输入 md5（`build_v60_manifest.txt` + 构建日志的 5 行 `v60 覆盖`）**

```
cd pynq_specialized_code && md5sum -c ../runs/hdmi_v60/build_v60_manifest.txt
  → 32 条全 OK
pl/rtl_hand/modules/bin_conv_core8.v     fcd3611b247b9e25230dcd9959b93001
pl/rtl_hand/modules/bin_conv_core.v      4e618e4193c09698606ce582721fe94a
pl/rtl_hand/modules/bin_e1e2.v           2835fc49805fc0ae95a50ea39ae1e937
pl/rtl_hand/modules/bin_rprelu.v         503374af8640673c0f3b8600cbfbbe75
pl/rtl_hand/modules/bin_unit_axi_wrap.v  d93fd0cc42bb6218d5b3e90b38d4d433
```
这 5 个 md5 = **跑过 S2（红绿都有）的同一批字节**；其余 7 个对 v57 manifest 全中。
（构建脚本对每个覆盖项做硬断言，改一个字节就拒绝构建。）

**② R3 的越界逻辑在网表里（`runs/hdmi_v60/runpoint.log`）**

```
*d:\...\u_bin_unit\g_conv.u_unit\u_eng\gh_l_reg[0][0..31]  ── R1-A 的 320 个触发器
*oob_cnt*      11 个 cell      *oob_snap*    12 个 cell
*r1_oobtap*     1 个 cell      *s1_oobtap_q*  1 个 cell
```
`oob_cnt` / `oob_snap` / `r1_oobtap` / `s1_oobtap_q` 这几个名字**在本轮之前不存在**
⇒ R3(b) 的计数/快照链路确实进了网表。
R1-A 的 `gh_l_reg*` 共 **320** 个触发器（8 lane × 32 bit = 256，其余为高扇出复制）。

**③ R2 的证据边界（必须说清，不能糊）**

`xs_c = sext(x) <<< 6` 在综合后**就是接线**（这正是"0 LUT"的由来）⇒ 网表里**没有**它的专属
cell，`*xs_c*` net 数 = 0 是**预期**结果，不是证据缺失。R2 的证据链是：
- (a) 构建字节 md5 = S2 验过的字节（上文 ①）；
- (b) 8 条 `bcmp` net + 132 条 `thr_q` net 仍在 `u_pack` 里（结构未被改坏）；
- (c) 行为：S2 的 512 bit 定向向量（含 x=0 正/负阈值两类、±0.5 LSB tie、亚 LSB 阈值）
  在同一份字节上 **512/512 一致**；把 `<<<` 去掉 ⇒ **红**。
⇒ "常量移位改动"的网表级证据**结构上不可得**；用 (a)+(c) 覆盖，是本轮能做到的最强证据。

### 11.3 上板产物（交接用）

| 文件 | md5 / 说明 |
|---|---|
| `runs/hdmi_v60/design_1_v60.bit` | `db43e55aef20f3028250d749257def26`（3 510 798 B）**SW_CRC `dd2da11c`** |
| `runs/hdmi_v60/design_1_v60.bit.bin` | `37a163f7d32e1b1ff661e36b70561142`（3 510 688 B）**板端 sysfs/fpga_manager 要喂 .bin** |
| `runs/hdmi_v60/design_1_v60.hwh` | 292 721 B（PYNQ Overlay 用） |
| 时钟 | `FCLK0` PYNQ 报 75（真值 **50.79**）、`FCLK1` 报 150（真值 **≈96.97**，**不要 200**） |
| 容器 / PS | `pack_v60`（`706a1d22…`）/ `deploy_v60/ps_pynq/*`（**不是本轮产物**，主代理另派） |
| 验证 | `board_verify_numfix.py --vd-aggregate` + 五张冻结样本 |

> `.bin` 生成命令（已执行）：
> `bootgen -image v60.bif -arch zynq -process_bitstream bin -o design_1_v60.bit.bin`

### 11.4 ⚠ 一处纪律违规（已发现、已恢复、已防复发）

**事实**：`bd_merge_hdmi_v60.tcl` 派生自 v58 时把输出目录 `set OUT` 留在了
`$REPO/runs/hdmi_v58`，于是本轮 BD 跑完后**往冻结目录写了 3 个文件**：
`bd_snapshot_pre_v58.txt`(16:20) / `design_1_v58.hwh`(16:21) / `bd_snapshot_post_v58.txt`(16:25)。

**恢复**：`git checkout --` 这 3 个文件；复核
`cd runs/hdmi_v58 && md5sum -c MD5SUMS` ⇒ **17 条全 OK**、`git status --short runs/hdmi_v58` ⇒ **空**。
⇒ 冻结产物逐字节复原。

**防复发**：脚本的 `set OUT` 已改为 `runs/hdmi_v60`，快照/hwh 文件名同步改成 `*_v60`。
**教训**：派生构建脚本时，"输出目录"和"文件名里的版本号"必须和 `set PROJ_DST` 一起改 ——
只改工程名不改 `OUT`，就会静默污染上一版冻结目录（而且门禁全绿看不出来）。
