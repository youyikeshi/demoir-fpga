# T-166 板端回报 —— p0067 逐层 sha1（BOARD_REQUEST 12 项 + 227 层全扫）

板：`xilinx@192.168.7.2`｜位流 v60（`.bit.bin` md5 `37a163f7d32e1b1ff661e36b70561142`）
容器 `pack_clean`（`706a1d22dfbbf33cf7af7940b9de54c4`，只读未改）｜输入 `frozen256/p0067_crop256.npy`
md5 `70043b5a003c48b8bdb1f6db8fa2e48e`（与主机 `runs/frozen256/` 那份**逐字节相同**）

## 0. runpoint 自证（先看这行，否则下面都不算数）

| 项 | 实测 | 判据 |
|---|---|---|
| `[DEPS]` | `q8/c8deq/bpack/e1e2/catpack/safe` 全 `fast`，`deps_closure_ok=True` | 首屏必须 |
| `in_sha1`（f32 CHW） | `c13eb436ed6f4709f16f4422b86aca6f4f4dec7a` | 与请求 §1 **命中** |
| `out1` f32 sha1 | `85d5e42bc9275c9ff77effc2038d3cf204991af4` | **== 历史板端记账**（`runs/opt_round1/perf/t165/board_out/`）⇒ 同一条数值 runpoint |
| H3 排除 | `engine._e1e2=None`、`net._lazy_deq=False`、`_cat_fusion=False` | e1e2/C8 直读**没被启用** |
| 时钟 | FCLK0 报数 75、SLCR `0x00101400`（DIV0+1=16）⇒ **50.79 MHz**；FCLK1 报数 150 | 与口径一致 |
| 单次前向 | 17.27 s（建网 8.2 s；`total=17.27 pl=3.77 ps=9.80`） | 一次只跑一个整网进程 |

> FCLK1 的 SLCR `0xF8000174` 在 v60 / v58 两个状态下**都读回 `0x00000000`**（PYNQ 报数却能读回 150）
> ⇒ 这块板/PYNQ 组合上 FCLK1 不能靠 SLCR 记账，只能以 PYNQ 报数为准。FCLK0 的记账是正常的。

## 1. 12 项对照（BOARD_REQUEST §3）

sha1 = `sha1(np.ascontiguousarray(a).tobytes())`；int8 = `(C,H,W)` C 序，取 `rint(y·2^fs_out)`，
f32 = `(C,H,W)` float32（`_fp_conv` 返回值，**含 relu**，与主机 `fpdump` 同一点）。

| # | 类型 | 层名 | 期望 | 实测 | 一致 |
|---|---|---|---|---|---|
| 1 | f32 | `encoder.conv_first.0` | `8b70516715ca` | `f65e0a04584f` | ✗ |
| 2 | int8 | `encoder.encoder_1.rdb.conv_layers.0.conv` | `7c55b4fec700` | `7c55b4fec700` | ✓ |
| 3 | int8 | `…rdb.conv_layers.1.conv` | `082d44e2eb1e` | `082d44e2eb1e` | ✓ |
| 4 | int8 | `…rdb.conv_layers.2.conv` | `1dae1f7750d0` | `1dae1f7750d0` | ✓ |
| 5 | int8 | `encoder.encoder_1.rdb.conv_post.conv` | `d15c331e3371` | `d15c331e3371` | ✓ |
| 6 | int8 | `…sam_blocks.0.b0.ghost_layers.0.cheap_operation.conv` | `26b0d00cba38` | `26b0d00cba38` | ✓ |
| 7 | int8 | `…sam_blocks.0.b2.ghost_layers.0.primary_conv.conv` | `735b707969a1` | `735b707969a1` | ✓ |
| 8 | int8 | **`encoder.encoder_1.down.conv`** | `46356d5caec9` | **`43bc3158968b`** | **✗** |
| 9 | int8 | `encoder.encoder_2.down.conv` | `215c0d7b6c9c` | `1ee844c86ae0` | ✗ |
| 10 | int8 | `decoder.preconv_3.conv` | `d93a19068bd3` | `4b85a58b3042` | ✗ |
| 11 | int8 | `decoder.preconv_1.conv` | `dc187c674f8d` | `803607a0ae2d` | ✗ |
| 12 | int8 | `decoder.decoder_1.sam_blocks.0.b4.conv.conv` | `2d2d57653d9e` | `624b611c1c79` | ✗ |

**6/12 一致**：第 2~7 项全中，第 1 项与第 8~12 项不同。

## 2. 全 227 个 int8 层扫（额外收益：精确到层，不再只是区间上界）

同一跑里把 227 个 conv 层的 int8 也各算一个 sha1，与主机 `host/layers_index.json` 逐**名字**比：

```
相同 37 / 227，不同 190；不同的正好是执行序第 37…226 层（连续，无缺口）
```

⇒ **第一个发散的 int8 层 = 第 37 层 `encoder.encoder_1.down.conv`**，
整个 `encoder_1` 阶段（idx 0~36，37 层）**逐字节相同**。

该层差异**极小**（板端 vs 主机 int8）：

| 指标 | 值 |
|---|---|
| 不同元素 | **162 / 393216（0.0412%）** |
| `max|Δ|` | **2 LSB** |
| Δ 分布 | −1: 84，+1: 68，−2: 3，+2: 7（带符号均值 ≈ 0.0000） |
| 形状/dtype | (96,64,64) int8，fs_out=3 |

## 3. 机制：分歧点在「二值化输入」而不是 conv 本身

对 4 个关键层在**板端**同时取两个 sha1：
`x` = `_PlBackend.prepare` 里送进二值化的 f32 张量；`A` = `pack_input` 的返回值（RTL 真正吃的打包字节）。
主机侧用 `_RefBackend` 取同名同定义的两项（`host_cmp37.py`，`out1` 复现 `2b1cd63cc7ef` ✓ `conv_first` 复现 `8b70516715ca` ✓）。

| 层 | 生产者 | x（板端 / 主机） | A（板端 / 主机） | 判读 |
|---|---|---|---|---|
| idx 0 `…rdb.conv_layers.0.conv` | `convfp: encoder.conv_first.0` | `f65e0a04…` **≠** `8b705167…` | `244c2149…` **=** `244c2149…` | 上游 f32 已差，**被二值化吸收** |
| idx 36 `…b4.conv.conv` | `cat: …b4.ghost_layers.4` | `88fa0485…` **≠** `a6a89c2a…` | `0cc14d76…` **=** `0cc14d76…` | 同上，仍被吸收 |
| **idx 37 `encoder.encoder_1.down.conv`** | **`sam: …sam_blocks.0:weighted_sum`** | `a26e7f95…` **≠** `a726e0f7…` | `3f0451e8…` **≠** `cc76cfea…` | **二值化翻 bit ⇒ 首个 int8 分歧** |
| idx 38 `encoder_2.rdb.conv_layers.0.conv` | `conv: idx37` | 不同 | 不同 | 纯下游级联 |

关键点：idx 37 的输入是 **SAM 的 f32 `weighted_sum`**（不是 int8 conv 输出），
而它上游的 int8 层（0~36）**逐字节相同** ⇒ 该层的 f32 输入差**不可能**来自 int8 链，
只能来自 SAM 的 f32 混合（ECA 注意力 `exp` + 加权和 + 上采样）。

### 3.1 零传输 canary：把 f32 算子逐个对拍（同脚本、输入由同一公式生成、输入 sha1 先验证相同）

| 算子 | 主机 numpy 2.2.6 / x86 | 板端 numpy 1.21.5 / ARM | 逐位同？ |
|---|---|---|---|
| `np.exp`（ECA 权重） | `b8930c6b55f023…`，`y[50000]=67.1568756` | `e73dfeb86cc20a…`，`y[50000]=67.156868` | **✗ 差 1 ULP** |
| `A @ v`（64×64 gemv） | `ed846f0d89d5…` | `ed846f0d89d5…` | ✓ |
| 滑窗 `np.dot` ×100（ECA 形态） | `317b4bfb61ce…` | `317b4bfb61ce…` | ✓ |
| `bilinear_f32` scale=2 | （主机回落 numpy） | 板端 numpy 与 C 内核 sha1 相同 | ✓（本口径下） |

⇒ **根因钉在 ECA 的 `np.exp`**：ARM 的 numpy 1.21.5 与 x86 的 numpy 2.2.6 的 `exp`
在**完全相同的 float32 输入**上给出不同末位（1 ULP），BLAS 点积反而逐位相同。
这与 `REPORT.md` §5 **H1**（支持度最高）的第三条落点**完全一致**。

### 3.2 与判读表（§4）的对应

- 出现形态 = 「**#1 不同 且 #2 起沿链不同**」+「**#1~#k 全等、第 k+1 项（= 第 8 项）开始不同**」；
- ⇒ 判读为 **上游 f32 已差但被二值化吸收 37 层，在 `encoder_1.down.conv` 的二值化处首次翻 bit**；
- **H2（int8 epilogue/位宽）否证**：idx 37 的 `A` 已不同、而它之前的 `A` 全同 ⇒ 分歧在二值化之前，
  不在打包/dint/epilogue；
- **H3 排除**：`_e1e2=None`、`_lazy_deq=False`（见 §0）。

### 3.3 下一步建议（不需要再猜）

1. **首选**：把板端的 `np.exp` 换成与主机逐位一致的实现，或在主机侧用 `np.exp` 的板端等价式复算
   ECA 权重；最小验证：主机把 `eca_attn_f32` 的 `exp` 结果**量化到板端那 1 ULP**，看 `down.conv` 的
   `A` 是否变得与板端相同（一跑就定）。
2. 若要看数值差：`encoder.encoder_1.down.conv` 的 **f32 输入**（48×128×128×4 = 3.15 MB）是唯一还没取回来的
   张量；要的话需要再开一次 v60 会话（`probe37.py` 里加一行 `np.save` 即可，不许改生产代码）。
3. `conv_first` 的 f32 差（`max|Δ|=8.3e-6`、相对 2.1e-7 ≈ 1.8 ULP、60% 元素不同）是 BLAS 求和序问题，
   **本次已证明它被二值化完整吸收**（idx 0 的 `A` 相同），**不是** p0067 142k 像素差的近因。

## 4. 收尾（v58 演示态 + 播放态验收）

| 项 | 值 |
|---|---|
| 写回完整帧 | 8 帧，`latch=OK 8/8`，`0x24 = 8/8`，`nonblack=88.9%` |
| `uf`（`0x2C`） | 播放中恒 **0**（`0x2C max=0`），空闲 4 s 采样 `+0` |
| 节奏 | `START→首帧 6200.5 ms`；`mean=6.199938 s`，`max|T−6.200|=0.000435 s` |
| CTRL | 播放 `0x01`，STOP 后回 **`0x15`（彩条）**，`0x24` 8→8 冻结 |
| 9999 | `LISTEN 0 8 0.0.0.0:9999`（等下一次 START） |
| 位流 | `stop → 载 design_1_v58.bin → start`；`fpga_manager state=operating` |
| `/lib/firmware` | `design_1_v58.bin` md5 `65b4f88463a00d76ba19319b8ebe5a3d`；`design_1_v60.bit.bin` md5 **`37a163f7d32e1b1ff661e36b70561142`**（未覆盖） |
| FCLK0 | PYNQ 报数 **100**，SLCR `0x00100F00`（DIV0+1=16）⇒ **66.67 MHz** ✓；fix_fclk 一行 `OK` |
| FCLK1 | PYNQ 报数 **150**（我全程未改它；SLCR `0x174` 恒读 0，见 §0 注） |
| 演示进程 | `demo_player.py --pairs pairs100 --loops 0 --start-delay 6.2 --period 6.2`，与接管前一致 |
| `sync` | rc=0 |

## 5. 产物（主机 `/tmp/probe_t166/`）

| 文件 | 大小 | md5 |
|---|---|---|
| `summary.json`（12 项 + 227 层 diff 明细 + 4 个 f32 + 统计） | 101 637 | `72e1f37b584ff857cd1c0f090a3d07a7` |
| `encoder.conv_first.0.npy`（**#1**，板端 f32 (48,128,128)） | 3 145 856 | `4ab72884fe3b61498eaf0ab3a7efecbe` |
| `encoder.encoder_1.down.conv.npy`（**第一个发散层**，int8 (96,64,64)） | 393 344 | `d385708d3f963ccd13dfb7125e79fe8c` |
| `probe12.py` / `probe37.py` / `canary_f32.py` / `canary2.py` / `host_cmp37.py` / `regs.py` / `run_v60.sh` / `run_v58.sh` | — | 全在 `/tmp/probe_t166/` |

板端原件：`/tmp/probe_t166/out/`（`probe.log`、`probe37.log`、`summary.json`、`probe37.json`、
`layers_p0067/` 16 个 npy，其中包含 idx 34/35/36 这三个"仍相同"的层，便于对照）。
