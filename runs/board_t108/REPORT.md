# T-108：v53 上板 —— s=2 组平面门禁（逻辑）+ 整网（关懒解码）对冻结 sha1

**一句话**：v53 的 `s=2` 修复在板上**生效**：op65 原样几何逐层 **0 B 越界 + 24576/24576 全对 + 组平面恒等**；
64² 整网在 `BIN_LAZY_DEQ=0` 下 **连跑 2 次三个 sha1 = 冻结值**，逐 op 395/395 两跑一致。

## 0. 运行点与资产（2026-11-03 00:11–00:13，单 session 单次 load）

| 项 | 值 |
|---|---|
| 位流 | `runs/v53_archive/design_1_150v53.bit` md5 **`e19de0326fe657fc8de9ddd241ecc729`**（跑前=跑后 ✓）+ `.hwh` **`5235764602921d03351e121a81e35613`**（成对 ✓） |
| 指纹 | `conv_uni_0@0x40040000` ✓、`bin_unit_0@0x40060000` ✓、**无 `fconv_*`** ✓、magic(`0x40070314`)=**`0x50300001`** ✓、`0x310`=**0** ✓ |
| 频率 | load 后 fclk0=125 MHz → 设 **50 MHz ⇒ 读回 50 MHz**；改频后重读 magic/`0x310` 均为干净值（第 119 条） |
| PS 闭包 | `ps/` `md5sum -c` **158/158 OK**；`deps_closure.txt` md5 `9de3d60c9abda79ff01b40801f3b809e` 板/镜像/主机三处一致；`md5sum -c` **20/20**；`deps_closure.py --check` **rc=0**（14 模块/20 文件，q8/c8deq/bpack/e1e2/catpack/safe 全 fast） |
| 探针/脚本 | `probe_t108.py` `ed3ecc5c68c4b4d1bea1929307aa90b6`（= probe_t104 只改：`BIN_LAZY_DEQ=0`、CASES 换 3 种几何、逐层 OUT/exp 落盘 + 组平面指纹、逐层不过即停） / `run_t108.sh` `cf0cc9568d0499fffa22812714fe8088` |
| 判据来源 | 逐层参考 = 板端 `bin_ref_numpy.conv_rtl`（真实权重包 + 随机激活，seed 20261031）；整网 = 冻结三件套 `bfa246d3…/226bd39e…/762d84b9…` |

## 1. 第 2 步：`s=2` 逐层门禁（**本轮核心**）

| 几何（全部 H=W 按 op 原样） | 越界 | 不符 | 两次互比 | 组平面 f(g) | sha1 |
|---|---:|---:|---|---|---|
| **op65 原样** cin48 cout96 k3 s2 pad1 **32×32→16×16** | **0 B** | **0/24576** | 逐字节相同 | **`0 1 2 3 4 5 6 7 8 9 10 11`（恒等）** | `71b172ac9d98` |
| **op129 同族** cin96 cout192 k3 s2 **16×16→8×8** | **0 B** | **0/12288** | 逐字节相同 | **`0…23`（恒等）** | `575fb0d5e108` |
| s=1 对照 cin512 cout192 k1 32×32 | **0 B** | **0/196608** | 逐字节相同 | `0…23`（恒等） | `b5ecf17a6697` |

- 主机侧独立复核：`cmp -s layer_*_run1_out.bin layer_*_exp.bin` **三对全部逐字节相同**
  （op65 md5 `95870da5df20a82ac5960f3ca676525e`）。
- 对照 v51 的同几何（xsim，T-105）：**6499/24576 + 4074 B 越界**、`f=(0,2,4,6,1,3,5,7,8,10,STALE,STALE)`
  ⇒ 本轮 0/0 + 恒等，**组平面步长已修好**。
- s=1 对照的 sha1 `b5ecf17a6697` 与 T-104（v51 50 MHz 同一层）**完全相同** ⇒ 逐层 harness/PS 未变。

## 2. 第 3 步：64² 整网（关懒解码，放最后）

运行点 `BIN_PACK=e1e2 BIN_LAZY_DEQ=0 BIN_LAZY_SLOTS=24 BIN_CAT_FUSION=1 BIN_BILINEAR_C=1 BIN_PRECOMP=1
Q8CACHE=1 BIN_F32DIRECT=1 BIN_CAT_ECA_FUSE=1 BIN_EPI_NOINV=1`，**无任何 `BIN_V50_*`**；输入 `x0` sha1 `bbae82e4…`（= 冻结口径）。

| 跑 | sha1 out1 / out2 / out3 | 结果 |
|---|---|---|
| run1 | `bfa246d34681e748311bbcd2f1b87317a56923fe` / `226bd39e522aa373b8fc183acae5280931f8b63f` / `762d84b9618eab8922edd6da4f07ba06de865628` | **= 冻结 ✓** |
| run2 | 同上（逐字节相同） | **= 冻结 ✓** |

- 两跑逐 op：**395 个 op（conv 227）中 0 个不同**；
- 跨位流旁证（同 PS/同 pack/同 x0，对比 T-104 的 v39 控制组 dump）：**op65 `c34c0594bbb1…`、op129 `a1b1605f6b22…` 两者 sha1 完全相同**。
  （其余 op 的裸 sha1 不可比：`LAZY_DEQ=0` 会把 conv 物化成 f32、cat 物化，`n` 全部不同 —— 是表示层差异，不是内容差异。）

## 3. 结论与边界

1. **v53 的 s=2 组平面修复在板上生效**（逐层 0 B / 24576 全对 / 恒等置换；整网 op65、op129 与 v39 逐字节同）。
2. **整网关懒解码后回到冻结 sha1**，且 **确定性**（2/2 相同、逐 op 0 差异）⇒ T-104 记录的 B 类（done/DDR 落地非确定）被绕开，不再是本轮阻塞项。
3. 未覆盖：远高于 50 MHz 的频率扫描（v53 约束 100 MHz、WNS +0.041 余量极薄，建议单独一轮做阶梯）。本次**未跑 150 MHz**。

## 4. 纪律

单次 load（唯一令牌）；`ulimit -c 0`；只 `pkill -x`/按 pid `kill -TERM`；租约内 2 ssh + 2 scp；
逐层全部先跑、整网最后（第 118 条）；改频后重读 magic/`0x310`（第 119 条）；收尾仅 `sync; sync`，
**未 poweroff**，板子仍开机、PL=v53、fclk0=50 MHz、`0x310=0`、magic 仍为 `0x50300001`；无段错误、无看门狗、无 ssh 重试。

## 5. 产物

`runs/board_t108/probe_t108.py`、`run_t108.sh`、
`t104_045926.log/.json`（探针侧日志名沿用了脚本内置 `t104_` 前缀，内容为本次 T-108 运行）、
`layer_*_run1_out.bin` / `layer_*_exp.bin`、`ops_net64_50_run{1,2}.{bin,json}`（各 43.3 MB，逐 op dump）、
`ps_md5_*`、`ps_deps_md5sum_c.txt`、`t108_run.log`、`t108_bit_after.md5`。
