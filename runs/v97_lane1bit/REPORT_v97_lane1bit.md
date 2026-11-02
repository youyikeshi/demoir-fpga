# T-97 报告：16-lane「每组一个通道系统性 ±1」的数值级定位

**一句话**：用**板上同一份参数/权重/激活**在 xsim 里跑包装层（`bin_unit_axi_wrap_v50`），
`VLANES=16` 与 `VLANES=8` 两趟 **5/5 层逐字节 0 差**（含重压：读延迟 80–250 拍、写压力
停核 24 710 拍）⇒ **任务的三个怀疑（PRM 槽位 / 末块掩码 / 权重 lane 选择）全部否证**，
RTL 逻辑本身不含该 ±1。**我一个 RTL 字节都没改**（`verify_rtl_root` MATCH=35 DIFF=0）。
新证据把矛头指向**硬件/环境下的"共享激活字读错"**，并**顺手量清了吞吐根因**：
v51 每 8 B 写 beat 恒定 ~20 拍（5/5 层），是"1 笔 AW/beat + 单笔在飞"的往返，**不是 FIFO 深度**。

跑点：Vivado 2025.2.1 `xvlog -sv / xelab / xsim`（一次一个进程）；未上板、未 ssh、
未综合/实现；RTL 根 `runs/timing_v50/rtl_v50` + `lane16/*`（v51 位流那份，md5 未变）。

---

## 0. 判据总览

| 判据 | 内容 | 结果 |
|---|---|---|
| **N1** | 5 个真实失败层（板上几何）：`VLANES=16` 一次 vs `VLANES=8` 两趟，逐字节 | **5/5 层 0 差**（327 680 B） |
| **N2** | 其中 2 层用**板上逐字节同一份** act（`default_rng(20261031)`）+ 同一份参数/权重 | **2/2 层 0 差** |
| **N3** | 重压：`RLMIN=80 RLMAX=250` / `+WSTALLN=40`（写压力停核 24 710 拍） | **仍 0 差** |
| **N4** | `VLANES=8` 与 v51 前逐位等价 | 未改 RTL ⇒ 逐位等价（且 5/5 层与 v39 侧 golden 一致） |
| **N5** | 地址级：`runs/v94_wrpath` 的 TB **原样**复跑（v8 / v16） | **65536/65536 PASS**（0 差异/0 越界） |
| **N6** | RTL 根未漂移 | `verify_rtl_root.sh`：**MATCH=35 DIFF=0 EXTRA=0 MISSING=0 → PASS** |
| **T1** | 吞吐：每 8 B 写 beat 的拍数（板上 5 层） | **20.03 / 20.28 / 22.13 / 20.43 / 20.03**（恒定墙） |
| **T2** | 写合并（sim，同形状） | v8 **7.80–7.88 beat/AW**；v16 **1.00 beat/AW**（8192 vs 1049 笔 AW） |
| **T3** | 加深 FIFO 的效果（BF 16→64，sim） | 总拍 41 728 → 41 407（**−0.8%**）；压力挡拍 24 710 → 24 095 |

---

## 1. 数值 TB 是怎么做成"板上同一份数据"的

| 件 | 文件 | 说明 |
|---|---|---|
| 参考模型 | `golden/*/expected_c8.bin` | 由 `bin_ref_numpy.conv_rtl` 生成，**与 T-96 探针用的同一函数**（探针 `BS.conv_rtl` 就是它）；5/5 层与 `bin_ref_numpy.to_c8` 互校 **0 差** |
| 参数/权重 | 直接取 `deploy/bin_weights_new`（`index.json` 的 `off_wgt/off_alpha/...`） | 板上那一份，未经我重算 |
| 激活 | `gen_real_gold.py` 合成位图；`gen_board_act_gold.py` **逐字节复刻探针**：`default_rng(20261031).integers(-128,128,(cin,H,W))` → `binarize_pack` | 后者 = 板上那一份 act |
| 几何（真实层） | cin48/cout64/k3/d1/pad1/sh6；cin112/64/k3/**d2**/pad2/sh7；cin176/64/k3/d1/pad1/sh8；cin240/48/k1/sh4；cin512/192/k1/sh6 | 全部 32×32，取自 blob 的层记录 |
| TB | `tb_v97.v` = `runs/v94_wrpath/tb_v94_wrap.v`（md5 `11461dca…`）**原样拷贝** + 3 处只读探针 + `+WSTALLN` / `-d V97_BF` | 逐字节判据、AXI3 从模型含随机反压都在原 TB 里 |

一层一层跑（`./run.sh <8|16> <shape>`），每次 xsim 单独一个进程。

### 1.1 结果（全部 `bytes=NNNN/NNNN  status=PASS`）

| 层 | v16 | v8 | 备注 |
|---|---|---|---|
| v97_c48_o64_32x32 | 65536/65536 | 65536/65536 | 板上 1381/1353 B 不符 |
| v97_c112_o64_32x32 | 65536/65536 | 65536/65536 | 板上 24 476/24 856 B 不符 |
| v97_c176_o64_32x32 | 65536/65536 | 65536/65536 | 板上 18 540/19 222 B 不符 |
| v97_c240_o48_32x32 | 49152/49152 | 49152/49152 | 板上 1190/1192 B 不符 |
| v97_c512_o192_32x32 | 196608/196608 | 196608/196608 | 板上 47/43 B 不符 |
| **bact_c112**（板上同 act） | **65536/65536** | — | 决定性：数据也用板上的 |
| **bact_c48**（板上同 act） | **65536/65536** | — | 同上 |

⇒ 同一份 RTL、同一份权重/参数、（其中两层）同一份激活，**仿真里逐字节全对**。

---

## 2. 三个怀疑逐条否证（这是本轮最硬的结论）

1. **PRM 槽位读错邻居**：否证。16-lane 的 32 字帧 `[0,4)alpha [4,12)bias [12,20)zeta
   [20,28)gamma [28,32)slope` 由 `pw[]` 的**常量位切片**取出（`pw[PB_A+(k/4)][16*(k%4)+:16]` 等）。
   若某 lane 取到邻居的 alpha/bias/zeta/gamma/slope，本 TB 会立刻成大错（随机参数时相邻值完全无关）。
   5/5 层 0 差 ⇒ 每个 lane 取到的是自己的参数。
2. **末块补零掩码错一位**：否证。cin112/176/240 三层都是 `cin%64=48`（末块 48 位有效、16 位补零），
   正是最容易被掩码错一位命中的形状；16-lane 结果与参考 0 差，且参考里补零位是**从 popcount 里 mask 掉**
   的（与核内 `pad_or_c`/`wkeep_c` 的 XNOR=0 等价），逐位一致。
3. **权重 BRAM 8→16 块的 wld 地址/lane 选择错一位**：否证。16-lane 的 `gstride=K·8·VLANES`、
   `wl_lane==VLANES-1` 的 `cmp_init` 边界、16 块 `g_wram[*]` 全部在环内；逐字节等价说明权重对。

---

## 3. 板上错误形状告诉我们什么（我把 T-96 的 json 重算了一遍）

| 事实 | 数据 | 推论 |
|---|---|---|
| **非确定性** | 同一层两次 session：1381 vs 1353（c48）、24 476 vs 24 856（c112）、18 540 vs 19 222（c176） | 不是一个"槽位/掩码/lane 映射"式的确定错；是**时序/竞争型** |
| **按"像素"成簇，同像素多通道** | c112 首批 8 个错字节：pix 都是 **12**，通道 1/2/4/6（g=5）、2/4/6（g=6）、2（g=7）；c48 首批集中在 pix 6–7 | 错的是**该像素共享的那份输入**（=64-bit 激活字），不是每通道各错各的 |
| **每像素平均 ~26/64 通道** | 24 476 B ÷ 932 像素 ≈ 26 | 与"一个 64-bit 激活字坏掉、约 40% 的 lane 输出因此改变"吻合 |
| **系统性主格** | c112：chan≡2 (mod 8) 在**全部 8 个 probe 组**各 ~750 次，`0↔-1`/`-1↔-2` 为主 | probe 的 chan = 通道 mod 8 = **C8 平面内字节号**；16-lane 下 = 每组 lane 2 与 lane 10（两个 64-bit 半字的**同一个字节位**） |
| **写 beat 数并不比 v39 多** | 两版每层都是 `ceil(cout/8)·Hout·Wout` 个 8 B beat（8192 / 6144 / 24576） | 所以"16-lane 写得多"不是因子；变的是**顺序**与**AW 笔数**（见 §4） |

⇒ 与"PRM/lane 参数"不再一致；与"**某次激活字读到旧/错数据**"一致。
而 §1 已证明：在 xsim 里把读延迟拉到 80–250 拍、把写压力打到停核 24 710 拍，**仍然 0 差**。

**所以：这不再是"改哪一行 RTL"能收敛的问题。** 需要两个一次 load 就能判决的板级实验（建议）：

* **E1（判"硬件非确定"）**：同 session、同数据、同一层**连跑两次**，把两次 PL 输出逐字节比对。
  两次互不相同 ⇒ 硬件非确定（竞争/时序）；完全相同但都与 golden 差 ⇒ 确定性问题（但 §1 说 RTL 不会）。
* **E2（判"时序余量"）**：同数据把 fclk0 从 100 MHz 降到 50 MHz 再跑。**错误消失/大幅减少 ⇒ 时序**。
  依据：v51 的 WNS 只有 **+0.113 ns**、WHS 只有 **+0.038 ns**（`timing_v51.rpt`，100 MHz），
  余量 ~1%，是"板上有、xsim 无、每次还不完全一样"的典型形状。
* 若 E1/E2 都指向时序：**先降频到 75 MHz 复测**（不改 RTL），把"功能对不对"与"收敛不收敛"分开。

---

## 4. 吞吐：**只给数据**（一行都没改）

### 4.1 板上：每 8 B 写 beat 的拍数是个**常数**

`runs/v97_lane1bit/board_cycles_per_beat.txt`（T-96 同一 session 的 json 重算）：

| 层 | beats | v51 cyc | v39 cyc | **v51 cyc/beat** | v39 cyc/beat | 比 |
|---|---:|---:|---:|---:|---:|---:|
| cin48_k3_d1 | 8192 | 164 104 | 117 755 | **20.03** | 14.37 | 1.39× |
| cin112_k3_d2 | 8192 | 166 106 | 229 049 | **20.28** | 27.96 | 0.73× |
| cin176_k3_d1 | 8192 | 181 317 | 342 631 | **22.13** | 41.83 | 0.53× |
| cin240_k1 | 6144 | 125 551 | 40 340 | **20.43** | 6.57 | 3.11× |
| cin512_k1 | 24 576 | 492 334 | 313 210 | **20.03** | 12.74 | 1.57× |

$$\Rightarrow\ \text{v51 的 conv 拍数} \approx 20\times(\text{8 B 写 beat 数})\ \text{（5/5 层，误差 } \pm 5\%\text{）}$$
$$\Rightarrow\ \textbf{写通路把核卡在 1 beat / \sim 20 拍}\ \text{（与 cin/cout/k 全无关）}$$

### 4.2 为什么是 20 拍/beat：**写合并被 2 平面交错打散了**

同形状 xsim（`DBG_WR`）：

| | 写 beat 数 | **AW 笔数** | 平均 beat/AW |
|---|---:|---:|---:|
| `VLANES=8`（c112 / c48） | 8192 | **1049 / 1039** | **7.80 / 7.88** |
| `VLANES=16`（c112 / c48） | 8192 | **8192 / 8192** | **1.00 / 1.00** |

$$\Rightarrow\ \text{8-lane：相邻像素同平面 ⇒ 8 个 beat 串成一笔 AW（每 8 笔省 7 次往返）}$$
$$\Rightarrow\ \text{16-lane：beat0/beat1 在}\textbf{两个相距 }H_{out}W_{out}{\cdot}8\ \text{字节的平面}\text{交替}
\ \Rightarrow\ \textbf{每一拍都断链}\ \Rightarrow\ 1\ \text{beat} = 1\ \text{AW}$$
$$\Rightarrow\ \text{818 倍的 AW 事务（8192 vs 1049）}\times\text{AW}\to\text{W}\to\text{B 往返} \approx 20\ \text{拍/beat}$$

### 4.3 FIFO 要加深到多少 ⇒ **答案：加深没用**

xsim 直接改 `WR_BUF_BEATS`（编译期 `-d V97_BF=`，RTL 一字未动），c240 层：

| BF | 总拍 | press 拍 | 写被压力挡（停核）拍 | peak_bf |
|---:|---:|---:|---:|---:|
| 16（现况） | 41 728 | 33 877 | 24 710 | 13 |
| 24 | 41 634 | 33 656 | 24 547 | 21 |
| 32 | 41 479 | 33 434 | 24 382 | 29 |
| 64 | 41 407 | 33 039 | 24 095 | 61 |

$$\Rightarrow\ \text{BF }16\to64\ (4\times)\ \text{只省 } \mathbf{0.8\%}\ \text{拍；停核拍数几乎不动}$$
$$\Rightarrow\ \textbf{FIFO 深度不是约束}\text{：加深只让核多跑几步就再次填满，**排空速率**才是墙}$$

**要多深才"永不停核"**：产生速率 = 1 beat / $(K/2)$ 拍（每像素 2 beat、每像素耗 $K$ 拍）；
排空速率（板上）= 1 beat / 20 拍 ⇒
$$\text{不停核} \iff \frac{K}{2} \ge 20 \iff K\ge 40 .$$
本网 5 层的 $K=9,18,27,4,8$ **全部 < 40** ⇒
$$\Rightarrow\ \textbf{没有任何有限 FIFO 深度能不停核}\ \text{（产生快于排空，队列必满）}$$
$$\Rightarrow\ \text{唯一方向：提高排空速率——把两个半平面各攒成 burst（8 beat/AW，恢复 8-lane 的合并），}
\ \text{或允许多笔 AW 在飞。}\ \ \text{(按任务书，本轮不动这一处)}$$

---

## 5. 产物 / 复算

```
runs/v97_lane1bit/
  gen_real_gold.py        板上真实参数+合成激活 → golden/（5 层）
  gen_board_act_gold.py   板上**逐字节同一份** act（seed 20261031）→ golden/bact_*
  golden/<shape>/         act_pack / wgt_pack / alpha,bias,zeta,gamma,slope / expected_c8 / shape.env
  tb_v97.v                = runs/v94_wrpath/tb_v94_wrap.v（md5 11461dca…）+ 只读探针 + WSTALLN + V97_BF
  build.sh run.sh         原 v94 脚本（TB 名改 tb_v97.v）
  build_bf.sh run_bf.sh   BF 深度扫描
  v8/ v16/ v16_bf16|24|32|64/   日志与 xsim 库
  board_cycles_per_beat.txt      §4.1 表
```

复算：
```bash
cd runs/v97_lane1bit
python3 gen_real_gold.py            # 5 层
python3 gen_board_act_gold.py       # 板上同 act 的 2 层
./build.sh 16 && ./run.sh 16 v97_c112_o64_32x32
./build.sh 8  && ./run.sh 8  v97_c112_o64_32x32
./build_bf.sh 16 64 && ./run_bf.sh 16 64 v97_c240_o48_32x32
cd ../.. && pynq_specialized_code/pl/scripts/verify_rtl_root.sh \
  runs/v51_archive/rtl_v50_manifest.txt runs/timing_v50/rtl_v50 --allow-extra 'lane16/*'
```

---

## 6. 冻结资产

`lane16/` **10 个文件 md5 与开工前逐个相同**（= `runs/v51_archive/rtl_v50/lane16/*`）；
`verify_rtl_root` MATCH=35 DIFF=0 PASS ⇒ 无需刷新 manifest。
未碰 25 条 v39 基线、PS、BD/tcl、`proj_engine`、`proj_v43base2`、`bin_segq.v`；未上板、未 ssh、未综合。

---

## 7. 加做的"极端环境"扫描（仍然 0 差）

c112 层，`VLANES=16`，逐字节判据不变：

| 加重条件 | 总拍 | 写压力挡停核 | 逐字节 |
|---|---:|---:|---|
| 默认（RLAT 1–20） | 81 390 | 0 | 0/65536 |
| `RLMIN=80 RLMAX=250` | 92 757 | 0 | 0/65536 |
| `RLMIN=400 RLMAX=800` | 144 437 | 0 | 0/65536 |
| `MAXOUTSTANDING=1`（只剩 1 笔读在飞） | 81 537 | 0 | 0/65536 |
| `MAXOUTSTANDING=2 RLMIN=200 RLMAX=400` | 107 654 | 0 | 0/65536 |
| `+WSTALLN=40`（wready 停 40 拍） | 92 688 | 24 710 | 0/65536 |
| `+WSTALLN=80` | 133 097 | 51 360 | 0/65536 |

$$\Rightarrow\ \text{读延迟到 }\mathbf{800}\ \text{拍、单笔读在飞、写压力停核 }5.1\text{ 万拍，}\textbf{数据仍然逐字节全对}$$

顺带量了一个**读代码发现、但仿真里从未触发**的潜在缺口（未改 RTL，只报）：
`wr_all_idle`（非融合 done 判据）含 `!p_valid && !pw_valid`，但**不含 `wr_split_pend_i`**
（串行化缓冲 `p1_v||cw_beat_en`）。融合通路的 `seg_band_done` 反而**含**了它
（`bin_unit_axi_wrap_v50.v:1810`）——同一作者在另一处显式加了，说明非融合这条路是漏项。
实测 `unit_done_q && wr_all_idle && wr_split_pend_i` 的拍数 = **0**（c48/c112/c240 三层），
原因是 `wr_all_idle ⇒ wr_accept_ok ⇒ 当拍就被吞`，所以它**不是**这轮 ±1 的原因
（最多 8–16 B、且总在层末）。若将来要补，一行即可、`VLANES=8` 恒等：
`assign wr_all_idle = ... && !wr_split_pend_i;`

## 8. 留给板级的两条判决实验（我不上板，只给判据）

* **E1 非确定性**：同 session 同一层连跑两次，两次 PL 输出互相逐字节比。
  - 两次不同 ⇒ 硬件非确定（竞争/时序）⇒ 与 §1（仿真全对）一致，转 E2；
  - 两次相同但与 golden 差 ⇒ 确定性问题回 RTL/参数上传，届时**必须**先交出这两次的 raw 输出。
* **E2 时序余量**：同数据把 fclk0 降到 50 MHz 再跑。
  - 错误消失/显著减少 ⇒ **时序**。依据：`timing_v51.rpt` 的 **WNS +0.113 ns / WHS +0.038 ns**
    （100 MHz，`check_timing` 无 unconstrained、无 no_clock）——余量只有 1% / 0.4%，
    正好是"板上偶发、xsim 没有、每次还不完全一样"的形状。
  - 仍完全一样 ⇒ 排除时序，转向"PL 的 AXI 主口在真实互连下的行为"（下一步：把这层的
    写 beat 序列用 `DBG_WR`/新加的探针在板上计数，与 xsim 的 8192 笔逐笔对账）。

## 9. 最终一轮全量回归（终版 TB，12 跑，全部 PASS）

| 运行 | shape | 逐字节 | 拍数 | 判据 |
|---|---|---|---|---|
| VLANES=8 | v97_c48_o64_32x32 | 65536/65536 | 76028 | PASS |
| VLANES=8 | v97_c112_o64_32x32 | 65536/65536 | 151076 | PASS |
| VLANES=8 | v97_c176_o64_32x32 | 65536/65536 | 226197 | PASS |
| VLANES=8 | v97_c240_o48_32x32 | 49152/49152 | 26779 | PASS |
| VLANES=8 | v97_c512_o192_32x32 | 196608/196608 | 207734 | PASS |
| VLANES=16 | v97_c48_o64_32x32 | 65536/65536 | 55774 | PASS |
| VLANES=16 | v97_c112_o64_32x32 | 65536/65536 | 81390 | PASS |
| VLANES=16 | v97_c176_o64_32x32 | 65536/65536 | 119698 | PASS |
| VLANES=16 | v97_c240_o48_32x32 | 49152/49152 | 41728 | PASS |
| VLANES=16 | v97_c512_o192_32x32 | 196608/196608 | 168086 | PASS |
| VLANES=16 | bact_c112_o64_32x32 | 65536/65536 | 81390 | PASS |
| VLANES=16 | bact_c48_o64_32x32 | 65536/65536 | 55774 | PASS |
