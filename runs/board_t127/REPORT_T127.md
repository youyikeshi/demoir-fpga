# T-127：链式两 op（消费者读生产者 PL 写出的 C8）—— **全对**，层间 C8 交接**未定罪**

**一句话**：把整网 op15/op13 的几何做成**链式两 op**（A 的输出**原地**交给 B，
再用 E1E2 从 A 的 OUT 槽读 C8 打包成 B 的 act），在 $64\times64$（= 128² 基分辨率）
与 $32\times32$（= 64² 基分辨率）上 **A、B 输出与交接字节全部逐字节等于 numpy 参照，
越界 0 B，连跑 2 次同一 sha1**。⇒ **"PL 写出的布局 ≠ 读入的期望布局"在这条单段交接上不成立**。
真正的差别不在这条边上，而在**多段 cat 交接（混合 fs、含 d<0）+ 整网并发**（§5）。

---

## 0. 运行点与资产（一个租约 = 两次 load，见 §0.2）

| 项 | 值 |
|---|---|
| 位流 | `runs/v53_archive/design_1_150v53.bit` md5 **`e19de0326fe657fc8de9ddd241ecc729`** ＋ `.hwh` **`5235764602921d03351e121a81e35613`**（跑后 bit md5 不变 ✓） |
| 频率 | 目标 50 → **读回 50.0 MHz**（150 MHz 单层即崩，本轮不涉） |
| 指纹 | `conv_uni_0`=0x40040000 / `bin_unit_0`=0x40060000 ✓；**magic(0x314)=0x50300001** ✓；**load 后 0x310=0x00000000** ✓；每次 conv 后 0x310=**0x403**（与 T-122 的 26 次读数完全一致 ⇒ 不是异常） |
| E1E2 | `R_RUN_UNIT=4` probe 通过（写 4 读回、写 0 读回）；**本轮的交接就是它做的** |
| 运行点 | `BIN_PACK=e1e2 BIN_LAZY_DEQ=0 BIN_LAZY_SLOTS=24 BIN_CAT_FUSION=1 BIN_BILINEAR_C=1 BIN_PRECOMP=1 Q8CACHE=1 BIN_F32DIRECT=1 BIN_CAT_ECA_FUSE=1 BIN_EPI_NOINV=1`；无任何 `BIN_V50_*`；vlanes=8（= T-124/T-125 逐项相同） |
| PS 门禁 | 板上 `md5sum -c` **158/158 ✓**；`deps_closure --check` **rc=0** ✓；`[DEPS]` q8/c8deq/bpack/e1e2/catpack/safe 全 **fast** ✓ |
| arena | 4 MB @`0x30300000`（够用：本轮只需 ~0.7 MB） |
| 纪律 | `ulimit -c 0`；只按 pid `kill -TERM`（无 `pkill -f`）；收尾只 `sync; sync`；**未 poweroff**（跑后 uptime 2:28、CmaFree 94 MB） |

### 0.2 两次 load 的如实披露
第 1 次（`t127_064359`）**崩在我自己探针的一个辅助函数**：`np.asarray(b'\x1a..', np.int8)`
在 numpy 1.21 上把 `bytes` 当 S 标量 ⇒ `ValueError`。**崩在第一个"比较"上，A 的 conv 已正常跑完**
（load / magic / E1E2 / numpy 参照 / A 单层全部 ok），**与硬件无关**。
修掉 `raw_bytes()` 后重跑（第 2 次 load，**同为 v53、bit md5 不变**）。崩溃那次的日志/JSON 留在
`crash1_bytesbug/`，不掩饰。

---

## 1. 几何与参照（一次定义，两个几何复用）

| | 层 | 几何 | OUT |
|---|---|---|---|
| **A**（生产） | `…b4.ghost_layers.1.primary_conv.conv`（idx 28，cin112 cout32 k1 s1 p0，shift4，**fs_in4 / fs_out5**） | 64×64→64×64 | 131 072 B |
| **B**（消费） | `…b0.ghost_layers.0.cheap_operation.conv`（idx 5，cin32 cout32 k3 s1 p1，shift5，**fs_in5 / fs_out4**） | 64×64→64×64 | 131 072 B |
| 64² 对照 | 同两层，H=W=32 | 32×32→32×32 | 32 768 B |

$\Rightarrow$ 交接的标度差 $d=f_{in,B}-f_{out,A}=5-5=\mathbf{0}$（整数/float32 两路逐字节一致）。
**这不是随手挑的**：整网里真正的 `primary_conv(48→32,k1)`→`cheap_operation(32→32,k3)` 这一对
也是 **fs_out=5 / fs_in=5 ⇒ d=0**（`index.json`，见 §5）。

参照全部在板上用 `bin_ref_numpy` 现算（C8 布局 `(cg,H,W,8)`）：
`refA_c8`、`actB_ref`（交接后的 act 字节）、`refB_c8`（**以交接后的 int8 为输入**）。

---

## 2. 四个口径（每个几何都做；链式每条连跑 2 次）

| 口径 | 做法 | 判据 |
|---|---|---|
| ① A 单层 | PS 喂 act（= T-122 的老口径） | 0 越界 + 与参照逐字节相同 |
| ③ **CTL1** | B 的输入由 **PS/numpy 直喂**（不经 A） | 应全对 |
| ④ CTL2 | E1E2 从 **PS 写进 DDR 的** `refA_c8` 打包 → B（隔离「源缓冲是谁写的」） | 应全对 |
| ② **CHAIN_D** | A 跑完 → **E1E2 直接从 A 的 OUT 槽（物理地址）读 C8**，打包**直写**进 B 的 IN 槽（PS 不碰激活，= `BIN_LAZY_DEQ=1` 的 C8 直读路径） | out+act 全对 |
| ② **CHAIN_P** | A 跑完 → `read_out`(f32) → PS 量化 → E1E2 打包 → `push_input`（**= 整网实际跑点** `BIN_PACK=e1e2 / LAZY_DEQ=0`） | out+act 全对 |

**证据不只是"输出对"**：每个口径都把 **B 的 IN 槽里实际落下的 act 字节**回读出来
（`invalidate()` 之后）与 `actB_ref` 逐字节比 —— 这直接测的是"交接"本身，不是它的下游后果。

---

## 3. 结果（$64\times64$，= 128² 的出错几何）

| 口径 | B 输出错字节 | 交接(act)错字节 | 越界(B 的 IN 尾/B 的 OUT 尾) | sha1(B) |
|---|---|---|---|---|
| A 单层 | — | — | 0 / 0 | `a7d84f6d1f7a`（= 参照 ✓） |
| CTL1 PS 直喂 | **0/131072** | **0/32768** | 0 / 0 | `58bb5250ef6a` |
| CTL2 E1E2(PS 写的源) | **0/131072** | **0/32768** | 0 / 0 | `58bb5250ef6a` |
| CHAIN_D it1 | **0/131072** | **0/32768** | **0** / 0 | `58bb5250ef6a` |
| CHAIN_D it2 | **0/131072** | **0/32768** | **0** / 0 | `58bb5250ef6a` |
| CHAIN_P it1 | **0/131072** | **0/32768** | — / 0 | `58bb5250ef6a` |
| CHAIN_P it2 | **0/131072** | **0/32768** | — / 0 | `58bb5250ef6a` |

- 参照：`refA_c8=a7d84f6d1f7a…` / `actB_ref=94accab21496…` / `refB_c8=58bb5250ef6a…`
- **确定性**：CHAIN_D、CHAIN_P 各 2 次 ⇒ 各自**只有 1 个 sha1**（`58bb5250ef6a`）✓
- **A 的 OUT 槽被 E1E2 读过之后逐字节不变** = True（两次都查）
- 拍数：A = 273 550 / 273 776 / 272 417（同层三次，$\pm0.5\%$ 内）；B 见 JSON

### $32\times32$（= 64² 基分辨率，整网本来就是对的）

同表全 0：CHAIN_D/CHAIN_P 各 2 次均为 `4ccdcbc77d29`（= 参照 `refB_c8=4ccdcbc77d29…`），
交接 0/8192，越界 0。

### 交叉验证（主机侧 `analyze_t127.py`，无 pynq）
用**实际落下的 act 字节**在主机重算 `conv_rtl(32→32,k3)`，与 B 的实际输出逐字节比：
8/8 个 chain/几何组合全部 **0/131072（0/32768）** ⇒ **B 的 conv 完全忠实**，
它算的就是它读到的那些字节。⇒ 交接与 conv 在这条口径上都干净。

（`t127_analysis.txt` 里有完整的逐通道/逐行/逐列/连续尾巴/C8 组映射 f/块级归属/字节车道映射 f 输出，
本轮错误为 0，这些分析器全部走到"逐字节全对 ✓"分支 —— 说明分析器已验证可用，
下一轮一旦出错可直接给空间形状。）

---

## 4. 判据结论（对父代理问题的直接回答）

1. **链式 A→B 在 128² 几何下不错**（两次逐字节相同）⇒ 这一条口径**不能**复现 128² 的故障。
2. **CTL1（PS 直喂）全对** —— 与 T-122 一致，本轮在同 session 内再确认一次。
3. **CTL2 / CHAIN_D / CHAIN_P 也全对** ⇒ 差别不在"源缓冲是 PL 写的还是 PS 写的"，
   也不在"E1E2 从 DDR 读 C8 的布局/阈值/地址算术"。
4. ⇒ **"层间 C8 交接（单段、d=0）"不是 128² 出错的原因**。父亲假设的
   "PL 写出的布局 ≠ 读入的期望布局"**在这条边上被证伪**。

---

## 5. 那还差什么维度（**只给判断，不自己扩到整网**）

从 `NetPlan(128,128)`（482 op / 227 二值卷积）+ `index.json` 逐项对出来，**本轮口径与整网的真实差别**：

| # | 差别 | 为什么它可能致命 | 证据/算式 |
|---|---|---|---|
| **①** | **多段 cat 交接（最可疑）** | 整网 op15（cin=**112**）的输入是 `cat_prev = [x1(32) ‖ x2(32) ‖ tt(48)]`：**一次 E1E2 打包里有 2 个 i8 段（来自两个不同 conv 的 OUT 槽）+ 1 个 f32 段**。`_seg_layout` 的 lane→段映射（`searchsorted`）与"每段各自的 pa/fs"这条路，本轮**一段都没走**（我只打了单段） | `bin_net._ghost`：`x2 = dense(x1, cheap_operation); g=x1+x2; tt=g+tt`；`_sg_layout` |
| **②** | **同一段打包里混合 d（含 d<0）** | x1 的 fs_out=**5**、x2 的 fs_out=**4**、目的层 fs_in=**4** ⇒ 段 0 的 $d=-1$（**右移+rint**）、段 1/2 的 $d=0$。`closed_form_thr_vec` 的 `neg` 分支（`x_t = need·2^k − 2^(k−1)`）**从未上过板**；本轮 d 恒 = 0 | 三条层记录 fs 值（§0 表 + `index.json`） |
| **③** | **并发/流水** | 整网是 `prepare(N+1)` 在 `N` 的 PL 时间里做，E1E2 的 `run_prepared` 在 `kick()` 才发；OUT 槽靠 `nref` 钉住/复用（24 槽）。本轮是**严格串行**：A 收完 → 打包 → B 起 | `bin_net._PlBackend.prepare/kick/finish` |
| **④** | **done ≠ 写回落地** | T-125 已证 30/227 个 op 在 done+2 ms 时 DDR 仍在变。本轮在 `read_out`/`invalidate` 之后才读，等效于"无限等待" | T-125 §1.1 |
| **⑤** | 3 层以上 / 残差 add / interp / sam 共享同一张量 | 逐 op 单调放大、首错点漂移（op13 vs op15）都更像**累积+并发**，不是单边布局 | T-124 §1 |

**最值得下一枪**：把本轮 CHAIN 的交接换成**①+②的那张真实段表**
（`[i8 pa=A1 fs=5 ‖ i8 pa=A2 fs=4 ‖ f32 48ch fs_in]`，目的层 = 真正的 `b0.ghost_layers.1.primary_conv`,
cin=112 且 **fs_in=4**），仍只跑两/三个 op、不碰整网。若那一枪错而本轮对 ⇒ **定罪"多段 cat 交接（混合 d<0）"**；
若也对 ⇒ 只剩 ③④ 的并发维度。

---

## 6. 产物（`runs/board_t127/`，全部绝对路径在 `/media/felix/Data/coding/demoir-fpga/runs/board_t127/`）

| 文件 | 说明 |
|---|---|
| `probe_t127.py` | 探针（`--geoms`；`--no-e1e2` 可只做 PS 口径）；md5 见 MD5SUMS |
| `run_t127.sh` | 一次租约：体检→scp→部署+门禁→一次 load→拉回 |
| `analyze_t127.py` / `t127_analysis.txt` | 主机侧空间形状分析（含"用实际交接字节重算 B"） |
| `t127_064502.{log,json}` | 本轮（有效）日志/结构化结果 |
| `crash1_bytesbug/` | 第 1 次崩溃（探针辅助函数 bug）的日志，如实保留 |
| `g64_*/g32_*.bin` | 每口径的 A/B 输出、B 的 IN 实际 act 字节、三份参照（共 2.3 MB，逐字节可复算） |
| `ps_*` | 部署清单/md5/板端 `md5sum -c` 与 `deps_closure --check` 记录 |
| `t127_bit_after.md5` | 跑后位流 md5 = `e19de032…`（不变） |
