# T-163：v57 RTL 原样重建（新 P&R）+ 100 MHz 上板 —— 刀 7 到底有没有真实隐患？

日期 2026-11-03 14:25–15:00。产物 `/media/felix/Data/coding/demoir-fpga/runs/v163_rebuild/`。
**本任务不改任何 RTL**：12 个构建输入逐字节取自已冻结的 `runs/ACCEPT_v57/04_RTL/rtl_v57/`。

## 0. 一句话结论

$$\boxed{\textbf{② 刀 7 有真实隐患} \Rightarrow \text{必须回退到 } \mathtt{28375eec}}$$

- 原脚本原样重建 ⇒ 位流与 v57 **只差 4 字节**（全在文件头时间戳）⇒ **Vivado 在 这份 RTL 上 P&R 是确定性的**，"重跑一次看运气"这条路本来就不存在。
- 只改一个指令（`place_design -directive Explore → ExtraTimingOpt`）得到**真正不同的实现** ⇒ 在 100 MHz **仍然错**（而且错得更狠：挂死 / 金丝雀被越界写 8 B），在 **75 MHz 全对**。
- 同板、同 PS、同工作点：v39（无刀 7）在 100 MHz **4/4 全对**。

⇒ 两个独立实现 + 一个对照，唯一共同变量是刀 7。

## 1. 构建输入（逐字节冻结）

| 文件 | md5 | 说明 |
|---|---|---|
| `rtl/bin_e1e2.v` | `bc28ead39b41e79f18571bff46bbd147` | **刀 7**（本任务的对象）|
| 其余 11 个 `rtl/*.v` | 与 `runs/ACCEPT_v57/04_RTL/build_v57_manifest.txt` **逐行相同** | 未改一字节 |
| BD `design_1.v` / wrapper / 6 个 IP DCP / stub / xdc | 未动（`$bd` 仍指 `pynq_specialized_code/pl/vivado/...`）| 与 v57 同源 |
| hwh | `c1686bd8e080c1c7f46181d980543202` | 与 v39/v57 同一份 |

构建脚本 = `runs/v57_archive/build_final_150v57.tcl` 的副本，**只改 `$rtl`、`$out` 两行**；B 额外只改 `place_design` 一行：

| 臂 | 与原始脚本的差异 | 定时 |
|---|---|---|
| **A** | 仅 `$rtl` / `$out` 路径 | 14:26:57 → 14:38:18（11.4 min）|
| **B** | `$rtl` / `$out` + `place_design -directive ExtraTimingOpt`（`route_design`、两次 `phys_opt`、`opt_design`、`synth_design`、`create_clock 10.000`、catpack false_path 全部逐字相同）| 14:37 → 14:50:55（13.9 min）|

两臂都走 `memgate.sh 5 --reserve 2.5 -- vrun.sh`（`[guard] ✅ 构建闸门通过 lock=held` 已入日志）。

## 2. 构建结果

| 项 | **A**（原样） | **B**（place ExtraTimingOpt） | v57（历史）| v39（基线）|
|---|---|---|---|---|
| bit md5 | `c9e2d1a5726bb65b8767a2614fdee001` | `957d90b661eba483619491c84eaec040` | `5d1cb789…` | `602725b8…` |
| WNS @10.000 ns | **+0.206** | **+0.264** | +0.206 | — |
| TNS | 0.000 | 0.000 | 0 | — |
| 违例端点 | 0 | 0 | 0 | — |
| WHS（hold）| +0.040 | +0.027 | — | — |
| TNS Total Endpoints | 41,273 | 41,273 | 41,273 | 47,805 |
| LUT / FF / BRAM / DSP | **20255 / 18961 / 35 / 101** | **20278 / 18961 / 35 / 101** | 20255 / 18961 / 35 / 101 | — |
| Register as Latch | 0 | 0 | 0 | — |
| 最差路径 | `win_q_reg_9/CLKBWRCLK` → `s1_pc_q_reg[3][6]/D`，9.734 ns，9 级 | `win_q_reg_1/CLKBWRCLK` → `s1_pc_q_reg[0][5]/D`，9.536 ns，8 级 | 同 A（9.734 ns）| — |

### 2.1 ★ A 就是 v57 那颗位流

```
cmp -l A/design_1_150v152.bit runs/v57_archive/design_1_150v57.bit | wc -l   ⇒ 4
offset 118,120,121,124 —— 全部落在 .bit 文件头的日期/时间字符串里
```
WNS/资源/最差路径也逐项相同 ⇒ **同一份实现**。（B 与 v57 差 1,007,957 字节 ⇒ 真换了实现。）

$$\Rightarrow\ \textbf{"重建一次再看运气"在本设计上无效}：\text{同一脚本必然复现同一颗位流。}$$

## 3. 上板（100 MHz 真实值，PS 从 `git archive HEAD` 取）

- PS 快照：`runs/v163_rebuild/stage/`（`git archive HEAD` 解出），`bin_net.py=0990948d`、`bin_engine.py=2fd3d099`，与要求逐字相同；板端 `md5sum -c` 144 个文件全过、`deps_closure --check` rc=0。
- 工作点：`BIN_PACK=e1e2 BIN_LAZY_DEQ=1 BIN_CAT_FUSION=1 BIN_BILINEAR_C=1 BIN_PRECOMP=1 Q8CACHE=1 BIN_F32DIRECT=1 BIN_CAT_ECA_FUSE=1 BIN_EPI_NOINV=1`（T-159 探针内冻结）
- 频率**实测标定**（写→读回）：150.0000→150.0000、100.0000→100.0000、66.6667→68.1818 ⇒ `Clocks.fclk0_mhz` 是 **1:1**，每个实验点都断言读回值（±0.05）。
- 单 net、单 arena、24 惰性槽、每次会话重新部署 PS、测前测后板端 `ps/` md5 逐文件一致（第 140 条）、判 PL 空闲用 `0x310=0` **且** 两 IP `busy=0`。

### 3.1 结果矩阵（256²，待判 `9b1940c8 / f86a7035 / 025757a8`）

| 臂 | 频率 | 跑数 | 结果 | sha1（每次）|
|---|---|---|---|---|
| **A**（≡v57 位流）| 100 | 7（两会话 4+3）| **0/7**，**7 次全不相同** | r1 `f7814154/a98f0756/a380891b`；r2 `155b1d4c/7aed739b/4cb616fb`；r3 `90302173/ebc5013a/e2ef2031`；r4 `0af769a0/6f6b6c4e/2980e992`；r5 `e3b480c2/2011d020/d9dd1613`；r6 `6e43c3f2/ee2b6ede/e309444d`；r7 `4be72db4/cb8f740f/f60dde0d` |
| **B**（新 P&R）| 100 | 3 会话 | **0/3，且一次都没跑完** | s1 `TimeoutError: bin_unit 超时 5.0s 未 done`；s2 `金丝雀被写坏 8/4096`；s3 `金丝雀被写坏 8/4096` |
| **B** | **75** | 4 | **4/4 全对** | `9b1940c8955b/f86a7035621b/025757a83c39` ×4（墙钟 6.21~6.26 s）|
| **v39**（无刀 7 对照）| **100** | 4 | **4/4 全对** | 同上 ×4（墙钟 5.77~5.86 s）|

A@100 墙钟 5.74~5.86 s（首跑 8.0 s）；B@75 6.21~6.26 s；v39@100 5.77~5.86 s。

$$\Rightarrow\ \textbf{B 的失效是频率依赖的}（100\ \text{错}/75\ \text{对），\text{不是 hold / 结构错}$$
（B 的 WHS = +0.027 ns 也正值，hold 本身没有违例）

## 4. 判据

$$\text{A 错}\wedge\text{B 错（且 B 100 错 / 75 对）}\wedge\text{v39 100 对} \Rightarrow \textbf{② 刀 7 有真实隐患}$$

- **"P&R 运气不好"被两次否掉**：① 原样重建 = 同一颗位流（4 字节时间戳），运气没有变化空间；② 真正换实现的 B 也错，而且是同一频率签名。
- **B 比 A 更糟**（挂死 / 越界写 8 B vs 只是错数）——两种实现下同一个隐患落点不同，与 T-162 的结论一致：刀 7 让 **2,841 个端点退出分析集**（其中就有 VS 里 21~27 ns 的未定时真实路径），因此 STA 的 `WNS +0.2 / 0 违例` 对两颗位流都**不构成可用性证据**。
- 与 T-160/T-161 无冲突：静态活逻辑枚举（61 条 / 最差 9.734 ns）与 RTL 级变延迟等价都只覆盖"被分析的、时序建模正确的"那一部分；**漏掉的正是 41,273（v39 是 47,805）这个端点集里退出分析的那 6,551 个**里的路径。

## 5. 建议（交主代理决策）

1. **回退到 `28375eec`**（无刀 7 = v39 侧 RTL）。v57/ACCEPT_v57 包维持"作废"，其 RTL 不得用于任何交付。
2. 若日后仍想要刀 7 的面积/时序收益：**必须先把那 6,551 个退出分析的端点重新纳入 STA**（恢复 `set_false_path` 之外的定时），再谈 P&R 指令。
3. 台架改进：`bin_unit` 的 done 超时 → 报错路径应顺带记录"最后完成的层号"，这次只能从 33 s/37 s 的会话总时长推断。

## 6. 板子现状

- PL = **v39 `602725b8` @100 MHz**（读回 100.0000），最后一次 256² 跑三 sha1 全中，收尾 `0x310=0` 且两 IP `busy=0`。
- 板端 `ps/` = 本次 `git archive HEAD` 快照（`bin_net 0990948d` / `bin_engine 2fd3d099`）；只 `sync; sync`，**未 poweroff**；uptime 13:15。
- 板端位流文件：`design_1_150v163.bit`（= v39 内容，md5 `602725b8`）、`design_1_150v163a/b` 未使用（`BIT_ONBOARD` 未透传到板端，三个臂共用同名槽，每会话都重新 md5 校验后才 load）。

## 7. 产物清单

```
runs/v163_rebuild/
├── rtl/                12 个构建输入（md5 与 manifest 逐行相同）
├── build_A.tcl / build_B.tcl     （diff = 2 行路径 + B 的 1 行 place 指令）
├── A/  build_A.log util_v152.rpt timing_v152.rpt *_routed.dcp *.bit *.hwh manifest
├── B/  同上
├── stage/              git archive HEAD 的 ps/ 快照 + 重生成的 deps_closure.txt
├── board/              v163a, v163a2, v163b, v163b2, v163b3, v163b75, v163v39, v163leave
│                       （probe.json / probe.log / run.log / ps_md5_before·after / bit_after.md5）
├── run_v163.sh board_v163.sh probe_v163.py     （T-159 台架副本，只改路径与 tag）
└── REPORT.md / MD5SUMS
```

> `deps_closure.txt` 注意：`git archive HEAD` 里**提交的那份是陈旧的**（对 bin_net/bin_engine 记的是 `5759ed50/6a14b8b9`，与 HEAD 的 ps 文件 `0990948d/2fd3d099` 不符 ⇒ `md5sum -c` 会 FAILED）。已用 `ps/tools/deps_closure.py --gen` 从 HEAD 快照重生成，**非注释部分与 T-159 用的那份逐字相同**。
