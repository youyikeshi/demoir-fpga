# T-128：**真实三段混合尺度 E1E2 打包** —— 全部逐字节对，这条口径**被证伪**

**一句话**：按 `index.json` op15 的真实段表造出 `[i8 32ch@fs5 ‖ i8 32ch@fs4 ‖ f32 48ch]`（段 0 的
$d=fs_{in}-fs_{out}=4-5=\mathbf{-1}$，走 `closed_form_thr_vec` 的 **neg 分支**），在 $64\times64$
（= 128² 输入的基分辨率）上用 v53 打包 + 真实消费者 `b0.ghost_layers.1.primary_conv`
（cin=112/cout=32/k=1/fs_in=4）跑 **5 个 conv op**：L0/L1/L2/L3 四档 + 两次 L3 确定性复跑，
外加 3 个"只打包"变体（neg 饱和端 / 构建期 lane 计划 / q8 缓存段）——
**act 字节 0/65536 错、消费者输出 0/131072 错、越界 0 B、两次同 sha1**。
⇒ **"多段混尺度 + $d=-1$ 的 neg 分支"不是 128² 出错的原因。**

---

## 0. 运行点与资产

| 项 | 值 |
|---|---|
| 位流 | `runs/v53_archive/design_1_150v53.bit` md5 **`e19de0326fe657fc8de9ddd241ecc729`** ＋ `.hwh` `5235764602921d03351e121a81e35613`（**跑后 bit md5 不变 ✓**） |
| 频率 | 目标 50 → **读回 50.0 MHz**（只用 50 MHz；第 119 条：改频后重读 magic/0x310） |
| 指纹 | `conv_uni_0`=0x40040000 / `bin_unit_0`=0x40060000 ✓；**magic(0x314)=0x50300001 ✓**；每次 conv 后 `0x310=0x00000403`（与 T-122/T-127 的 26+ 次读数一致） |
| 运行点 | `BIN_PACK=e1e2 BIN_LAZY_DEQ=0 BIN_LAZY_SLOTS=24 BIN_CAT_FUSION=1 BIN_BILINEAR_C=1 BIN_PRECOMP=1 Q8CACHE=1 BIN_F32DIRECT=1 BIN_CAT_ECA_FUSE=1 BIN_EPI_NOINV=1`；无任何 `BIN_V50_*`；vlanes=8 |
| PS 门禁 | 板上 `md5sum -c` **158/158 ✓**；`deps_closure --check` **rc=0** ✓；`[DEPS]` q8/c8deq/bpack/e1e2/catpack/safe 全 **fast** ✓ |
| arena | 8 MB @`0x30300000`（src112/srcS0/srcS1/actB/outB 各自独立，见 §5 事故②） |
| 纪律 | `ulimit -c 0`；只按 pid `kill -TERM`（无 `pkill -f`）；ssh 4 次/整轮；收尾只 `sync; sync`；**未 poweroff**（跑后 uptime 2:41、CmaFree 93 MB） |

有效日志/结果：`t128_065806.{log,json}`。探针 md5 `09e6f1202fb61ccc6cb622d1048edd78`、run 脚本
`764bb4f3…`、主机自测 `6fdcbabe…`、分析器 `a1ef87c3…`。

### 0.1 载入次数如实披露（**4 次 load，其中 3 次是我自己的探针 bug**）
| 时间 | 结果 | 原因 |
|---|---|---|
| 06:52 | 崩，0 个 op | 我的 `log()` 格式串 **7 参数 / 6 占位符** ⇒ `TypeError` |
| 06:54 | 全部数据已落盘，**汇总打印崩** | `x["pack"]["segs"]` 键不存在 ⇒ `KeyError`（判据在打印前已算好，但 `verdict` 未落盘）|
| 06:56 | L3q **假失败** 16315/65536 | **我的探针把段 1 与段 2 写到同一个缓冲**（同一 pa，第二次写覆盖第一次）⇒ 是我的 bug，不是硬件。修正后 06:58 全 0 |
| **06:58** | **8/8 全对，rc=0** | 有效轮 |

为了不再靠"人眼发现"，事后的修正里加了一条**按段表反算每 lane src** 的断言
（`lane_src_ok`）：段被写到同一缓冲时地址仍"自洽"，只有按段表反算才抓得到——正是这次踩到的坑。

---

## 1. 被测口径（= 整网 op15 的真实段表）

`NetPlan(128,128)` 的 `b0.ghost_layers.1.primary_conv`（`index.json` idx=6，
cin=112 cout=32 k=1 s=1 pad=0 shift=4 **fs_in=4**），`_ghost` 里
`cat_prev = [x1 ‖ x2 ‖ t]`：

| 段 | 来源（整网） | 通道 | 源缓冲标度 | $d=fs_{in}-fs_{out}$ |
|---|---|---|---|---|
| 0 | `b0.ghost_layers.0.primary_conv` 的 **OUT 槽**（idx=4，fs_out=**5**） | 32 | i8 C8 | **−1**（右移 + rint，即 neg 分支） |
| 1 | `b0.ghost_layers.0.cheap_operation` 的 OUT 槽（fs_out=**4**） | 32 | i8 C8 | 0 |
| 2 | 残差 add 之后的 **fp32** 张量 `tt` | 48 | f32（PS 侧 q8 量化到 fs_in=4） | 0 |

消费：`conv_rtl`/RTL 一次 cin=112、cout=32、k=1、$64\times64$（cb=2 ⇒ **2 条命令、每命令 8 lane、
每 lane 自带 src/thr**）。判据（每档）：**act 字节 = 参照**、**消费者输出 = 参照**、**金丝雀越界 0 B**、
**打包两次同 sha1**；另用**实际落下的 act** 在板端 numpy 重算 conv ⇒ 把"打包错/conv 错"分开。

### 1.1 八档（四档梯度 + 三个只打包变体 + 一次确定性复跑）

| 档 | 段表 | 逐段 d | act 错 | 消费者 out 错 | 越界 | 打包两次 | 描述符两条路同表 | sha1(act) |
|---|---|---|---|---|---|---|---|---|
| **L0** | 112 | 0 | **0/65536** | **0/131072** | 0 | ✓ | ✓ | `871aa728ff84` |
| **L1** | 56+56 | 0,0 | **0/65536** | **0/131072** | 0 | ✓ | ✓ | `6dd5b0160c32` |
| **L2** | 32+80 | **−1**,0 | **0/65536** | **0/131072** | 0 | ✓ | ✓ | `b397cbe7000a` |
| **L2x** | 32+80（段0 全幅 int8，饱和端） | **−1**,0 | **0/65536** | 不跑 | 0 | ✓ | ✓ | `33b1c66e53c4` |
| **L3** | 32+32+**48(f32)** | **−1**,0,0 | **0/65536** | **0/131072** | 0 | ✓ | ✓ | `60e1431ff62f` |
| **L3p** | 同 L3，走**构建期 lane 计划** | −1,0,0 | **0/65536** | 不跑 | 0 | ✓ | **✓** | `60e1431ff62f` |
| **L3q** | 同 L3，f32 段换 **q8 缓存 i8 段** | −1,0,0 | **0/65536** | 不跑 | 0 | ✓ | **✓** | `60e1431ff62f` |
| **L3b** | 同 L3（整条链第 2 次） | −1,0,0 | **0/65536** | **0/131072** | 0 | ✓ | ✓ | `60e1431ff62f` |

- **确定性**：L3 与 L3b 的 act 与 out **各自只有 1 个 sha1** ⇒ ✓ 确定（不是"每次不同"那种）。
- **L3p 的意义**：整网 `_PlBackend.prepare()` 传给 `prepare_segments` 的是**构建期预计算好的
  lane 计划**（`self.net._lane_plan[op.id]`），不是 `_build_desc()` 现算。这里用**真的**
  `bin_net.BinNet._build_lane_plans()`（最小假图：两个 conv 叶 + 一个 add 叶 + 一个 cat）
  造出计划，实测**两条路产出的描述符表逐位相同**，act 也逐字节相同。
- **L3q 的意义**：`Q8CACHE=1` 命中时 f32 段会被换成带 pa 的 i8 段（`_q8_segments`），
  那时走的是 `_lane_plan_q8`（`lane_off=(lanes-off)//8` 对**所有**段）。实测 act 与 L3
  **同一个 sha1**（= 设计上 f32 段量化后就等于它的 q8 缓存段，交叉判据 `L3_vs_L3q_ref_same=True`）。
- 逐 lane src 全部等于"按段表反算的 $pa+k\cdot HW\cdot8$"（`lane_src_ok=True`，8/8 档），
  包括 L3 里 lane 8..13 落在 **vin** 上（`0x30b40000 + k*0x8000`）。
- 拍数：L0 273 466 / L1 272 083 / L2 273 108 / L3 273 669 / L3b 273 812（$\pm0.6\%$）。

### 1.2 判据链结论
$$\Rightarrow\ \text{L0/L1 对}\ \Rightarrow\ \text{基线成立}$$
$$\Rightarrow\ \text{L2 对}\ \Rightarrow\ \textbf{不是 } d{=}{-1}\ \text{的 neg 分支}$$
$$\Rightarrow\ \text{L3 对}\ \Rightarrow\ \textbf{不是"多段 + f32 混尺度"}$$
$$\Rightarrow\ \textbf{四档全对 ⇒ 这条口径被证伪（不硬凑）}$$

---

## 2. 参照怎么算的（同一套打包公式，主机侧已自测）

1. **逐段各自重标定**：i8 段 `x_q = sat8(rint(x·2^d))`（$d<0$ ⇒ `rint` 半到偶）；f32 段
   `x_q = sat8(rint(tt·2^{fs_in}))`。拼成 (112,H,W) 后 `binarize_pack(x_q, t_q)` → act 字节。
2. **硬件式等价判据**：`THR = closed_form_thr_vec(d_ch, t_q)`，逐通道 `src_byte + THR > 0`
   必须等于 `x_q + t_q > 0`。**板上每档都实测不符 0/458752**（L0~L3q 全 0）。
3. 主机侧自测（`selftest_t128.py`，**无 pynq**，THR/计划生成器都用 `ast` 抽**部署源码**再 exec）：
   - **Q1** 9 个 d（−4..+4）× 256 个 $t_q$ × 256 个 x = **589 824 组合全部一致**
     ⇒ 闭式（含 neg 分支）本身没问题；`|THR|>127` 只出现在 $d<0$ 且 $t_q$ 极端时（$d=-1$ 有 63/256 个 $t_q$ 给出 **THR=+129**），真实层 $t_q\in[-17,4]$ ⇒ 触及不到
   - **Q2** 真实三段拼接的 THR 形式 vs 直接公式：0/7168
   - **Q3** 把 `probe_t128.py` 自己的 `make_levels/build_ref` 抽出来跑（7 档）⇒ 全自洽
   - **Q4** 把 `bin_net._build_lane_plans` 抽出来跑（q8=False/True 两种）⇒ thr/lane_off/kinds 全对
   ⇒ 主机自测全过才上板（T-127 的 `bytes` 标量崩溃之后立的规矩）。

---

## 3. 那 128² 的错还差什么（**只给判断，不自己扩到整网**）

本轮把 T-127 §5 的差别 ①② **都补齐并逐字节排除**。剩下的**只有**：

| # | 差别 | 为什么仍可能致命 | 现状 |
|---|---|---|---|
| **③** | **并发/流水**：整网 `prepare(N+1)` 在 `N` 的 PL 时间里做，E1E2 的 start/wait 推迟到 `kick()`；OUT 槽靠 `nref` 钉住/复用（24 槽）。本轮是**严格串行**（打包 → conv → 等 done） | E1E2 的 done/写回与 conv 的 done 共用 0x310；PL 越界写/时序竞争只在"同时有活"时出现 | 未测（T-115 只证明"写拍 0 丢失"） |
| **④** | **done ≠ 写回落地**（T-125：30/227 op 在 done+2 ms 仍在变；但 174/227 在 done+2 ms 已"稳定且已错"） | 本轮所有回读都在 done 之后且 `invalidate()` 之后 ⇒ 等效"无限等待" | 已由 T-125 部分排除 |
| **⑤** | 3 层以上的累积 + 残差 add / interp / sam 共享同一张量 | 逐 op 放大、首错点漂移（op13 vs op15）更像累积/并发 | 未测 |

⇒ **建议下一枪只打 ③**：在**同一进程**里把"两层 conv 并发（`prepare` 提前 + `kick` 推迟）"
跑成最小复现（2~3 个 op、24 槽、$64\times64$），而不是继续加深单层口径。

---

## 4. 产物（绝对路径 `/media/felix/Data/coding/demoir-fpga/runs/board_t128/`）

| 文件 | 说明 |
|---|---|
| `probe_t128.py` | 探针（8 档；`--side 64`；一次 load）md5 `09e6f120…` |
| `selftest_t128.py` | **主机侧自测 Q1~Q4**（无 pynq；验证要部署的公式/计划代码）md5 `6fdcbabe…` |
| `run_t128.sh` | 一个租约：体检→杀旧→CMA→scp→部署+门禁→一次 load→拉回 md5 `764bb4f3…` |
| `analyze_t128.py` / `t128_analysis.txt` | 主机侧空间形状分析（本轮全 0 ⇒ 分析器已走到"逐字节全对"分支，下次出错可直接给通道/行/列/尾巴/±1） |
| `t128_065806.{log,json}` | **有效轮**（8 档全对，`verdict.all_pass=true`） |
| `t128_065232/065405/065645.*` | 前 3 次（**我的探针 bug**）如实保留，见 §0.1 |
| `L*/{ref_act,act_got,outB,ref_outB}.bin` | 每档参照与实测字节（共 2.8 MB，逐字节可复算） |
| `ps_*` | 部署清单/md5/板上 `md5sum -c` 158/158 与 `deps_closure --check` rc=0 |
| `t128_bit_after.md5` | 跑后位流 md5 = `e19de032…`（**不变**） |
