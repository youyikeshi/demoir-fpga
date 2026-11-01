# convfp（float32 卷积）上板验证清单

> 面向：把 `convfp` 从 PS 搬进 PL 之后的**第一次上板**。
> 交付物：`fconv_axi_wrap.v` + `fconv_axi_wrap_bd.v` + `scripts/bd_add_fconv.tcl`
> + `ps/pynq/fconv.py` + `ps/pynq/board_fconv_verify.py`
> 状态：xsim 门禁 ①②⑤ 见 `_worktrees/fconvint/reports/GATES.md`；本清单只写**板上**步骤。

---

## 0. 前置（上板前必须已经完成）

| 项 | 判据 | 命令 |
|---|---|---|
| 位流含 fconv_0 | `.hwh` 里有 `fconv_0` 且基址 `0x40080000` | `grep -A2 fconv_0 design_1.hwh` |
| 地址不冲突 | conv_uni_0=`0x40040000`、bin_unit_0=`0x40060000` 不变 | 同上 |
| 驱动就位 | `/home/xilinx/demoir/ps/pynq/fconv.py` | `python3 -c "import fconv"` |
| 参考模型就位 | `bin_ref_numpy.conv2d_f32` 可 import | `python3 -c "import bin_ref_numpy as r;print(r.conv2d_f32)"` |

```bash
# 生成位流（vivado 空闲时；走 memgate + vrun，不许并发）
cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code
tools/memgate.sh 8 -- bash pl/scripts/vrun.sh \
    pl/scripts/bd_add_fconv.tcl /tmp/bd_fconv.log 1200   # 先只改 BD
# 然后照 build_final_150v41.tcl（= v40 + fconv 的 4 个文件 + BD 网表）跑实现
```

---

## 1. 门禁 A：寄存器/身份自检（30 秒）

```python
from pynq import Overlay
import fconv
ol = Overlay(".../design_1_150v41.bit")
eng = fconv.FconvEngine(ol.fconv_0)
print(eng.nlanes())        # 期望 8（或 4）—— 不对就说明位流/地址有问题，别往下跑
eng.set_mode(0)            # 模式 A：x/w/b 走 DDR，y 写 DDR
print(eng.status())        # 期望 bit0(done)=0 或 1，bit2(err)=0
```

**判据**：`nlanes() ∈ {4,8}` 且 `status() & 0x4 == 0`。
`nlanes()` 读出 0/0xFFFFFFFF ⇒ AXI-Lite 没通（先查 `axi_ctrl/M04_AXI` 与基址）。

---

## 2. 门禁 B：4 个真实层，逐位 0 diff（**主判据**）

```bash
cd /home/xilinx/demoir/ps/pynq
python3 board_fconv_verify.py            # 真实权重；H0=256（网络真实尺寸）
python3 board_fconv_verify.py --random   # 不依赖权重包时的兜底
```

板端 K 串行 golden：`bin_ref_numpy.conv2d_f32`（ATLAS）。**不许用主机 numpy**。

| 层 | cin,cout,k | 输入 (H×W) | 输出元素 | 逐位判据 |
|---|---|---|---|---|
| `encoder.conv_first.0` | 12,48,5 | 128×128 | 786 432 | 0 / 786432 |
| `decoder.decoder_1.conv` | 64,12,3 | 32×32 | 12 288 | 0 / 12288 |
| `decoder.decoder_2.conv` | 64,12,3 | 16×16 | 3 072 | 0 / 3072 |
| `decoder.decoder_3.conv` | 64,12,3 | 8×8 | 768 | 0 / 768 |

> ⚠ **W 必须是 32 的倍数**（core 的 tile 宽 TW=32）。H0=256 时上面 4 个 W 都合法；
> 若换成别的 H0 而 W 不是 32 倍数，`board_fconv_verify.py` 会警告并按 32 对齐后测。

**判据**：每层 `0 diff`，脚本末行 `RESULT: PASS`。

---

## 3. 门禁 C：计时预算（同一次 run 顺手打印）

| lane | 期望 4 层合计 | 逐层期望 (ms) |
|---|---|---|
| 8 | **456.7 ms** | 251.1 / 156.2 / 39.3 / 10.1 |
| 4 | **807.5 ms** | 502.1 / 234.3 / 58.4 / 15.0 |

**判据**：合计落在期望 ±15% 内（AXI 版比 xsim 的直连 BRAM 多一次 DDR 往返；
门禁 ⑤ 在 xsim 里控的是 core 拍数，板上以实测为准）。
超 1.3× 时看 `eng.last_dbg`：
- `stall` 大 ⇒ 读预取没盖住（`DBG[MODE].bit1` 关掉预取对比一次，见 §6 排障）
- `nAR/fills` 远大于需求量（每层窗口元素/8）⇒ 预取过量，把 `PF_AHEAD` 调小
- `flag & 0x4` ⇒ 写 burst 撞 4 KB 边界（合法拆分，不是错）

---

## 4. 门禁 D：整网 sha1 三件套不变（**回归**）

convfp 进 PL 后，整网输出必须与 v39/v40 完全一致（因为 4 层都是 0 diff 的纯搬移）。

```bash
cd /home/xilinx/demoir/ps/pynq
python3 board_v39_verify.py --hw 256     # 跑整网
```

**判据**（脚本里 `FROZEN[256]` / `FROZEN[64]`，必须逐字相同）：

| 尺寸 | out1 | out2 | out3 |
|---|---|---|---|
| 256 | `9b1940c8955be53ccffd7905e333deffcea31f0a` | `f86a7035621bed133374a1bef42a27db204e7019` | `025757a83c3993c8f592e31219b6a97906020363` |
| 64 | `bfa246d34681e748311bbcd2f1b87317a56923fe` | `226bd39e522aa373b8fc183acae5280931f8b63f` | `762d84b9618eab8922edd6da4f07ba06de865628` |

> 任何一个 sha1 变了 ⇒ **不是**性能问题，是数值变了，立刻停（先查是不是把
> convfp 接到了错误的层，或 pad/relu/bias 传错）。

---

## 5. 门禁 E：整网时间（本次的收益）

预期：PS 侧 4 层 1.15 s → PL 0.457 s（8 lane），**净省 0.68 s**。
用 `board_v39_verify.py` 的 `min 总` 与 v40 基线对比；`--random` 不计时。

---

## 6. 排障对照表

| 现象 | 最可能原因 | 动作 |
|---|---|---|
| `nlanes()` = 0xFFFFFFFF | AXI-Lite 未通（M04_AXI 没接 / 基址错） | 查 `.hwh` 里 `fconv_0` 的 MEMRANGE |
| `launch` 抛 `errcode=4` | 某个基址不是 8 字节对齐 | `pynq.allocate` 默认 4 KB 对齐，检查是否传了切片数组 |
| `errcode=5` | 基址高 32 位非 0（传了 >4 GB 的地址） | 用 CMA 缓冲，不要用 `ctypes` 手算地址 |
| `errcode=2` | `k` 不是 3/5，或 pad≠(k-1)/2 | 驱动 `config()` 会先拦，绕过它就会撞 |
| `errcode=3` | `cin·k² > 1024` | 拆 ic 或换层 |
| 超时（不再重发 start） | 读通道没数据（AR 没出去/HP 口没通） | 读 `DBG_FLAG`：bit3=AR 队列非空 ⇒ AR 卡住；bit0=RID 越界 |
| 金丝雀被写坏 | 引擎越界写 y | 输出缓冲比 `cout·H·W·4` 小，或 H/W 传错 |
| 逐位有 diff 但形状对 | golden 用错（主机 OpenBLAS） | 必须在板上跑 `bin_ref_numpy.conv2d_f32` |
| 逐位有 diff 且固定在前几个 ox | 地址/tile 边界 | 报给 RTL（xsim 门禁①② 是 0，说明是集成/驱动侧） |

---

## 7. 模式 B（融合形态）的预留

第一版只验模式 A。模式 B 的口已引出（`s_axis_x_*` / `m_axis_y_*`），
BD 里当前绑 0（`xlconst_1b` / `xlconst_64b`）。融合时：
1. 上游 PL 单元的输出接到 `s_axis_x_tdata/tvalid`，`s_axis_x_tready` 回给上游；
2. `m_axis_y_*`（含 `m_axis_y_addr` 地址 sideband）接到下游单元；
3. 驱动侧 `eng.set_mode(1)`。
xsim 里模式 B 与模式 A 已经**逐字节 0 diff**（门禁②），所以这一改动只需要重新连线。
