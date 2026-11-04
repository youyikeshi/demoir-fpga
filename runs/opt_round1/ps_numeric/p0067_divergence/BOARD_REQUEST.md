# 板端导出请求 —— p0067 逐层对拍（**12 项**，先只回 sha1 即可）

> 提出方：主机侧调试（T-165 后续，分支 `restructure1`）
> 原因：p0067 的 `out1` 板端 vs 主机 int8 **145972/196608 像素不同（74.25%）、max|Δ|=11、带符号均值 +0.411**，
> 而另外 4 张只有 3~7 个像素不同 ⇒ 差异**不是整网系统性偏移**，必须逐层定位。
> 主机侧已完成（`REPORT.md` §1~§4）：
> * 三个定点语义开关（`BIN_PACK`/`BIN_RTL_ROUND`/`BIN_CONV_OOB0`）**都不能解释**这 145972 个像素
>   （任何一个换掉都会变成 196608/196608、`mean|Δ|` 19~134 LSB，比现在差 15~100 倍）；
> * 主机上给输入加 **1 个 ULP** 就能让 p0067 的 `out1` 变 **6262** 个像素（max 4），
>   而同样的扰动在 p0000 上变 **0** 个像素 ⇒ p0067 是**级联放大**型样本，
>   板端与主机的差异极可能起于**某一次亚 LSB 的浮点差**，且起点在很上游。
> ⇒ 只有板端自己的逐层张量能定"第一个发散的层"。

**纪律**：本请求不 ssh、不碰板、不跑 Vivado；下面是给板端执行人的自包含操作单。
**最小回传**：只回**12 行 sha1**（不用传文件）就能定位第一处发散层；确认后再 scp 那 1~2 个 `.npy`。

---

## 1. 运行条件（与已记录的 run100 完全一致，不要改）

```bash
# 容器：/home/xilinx/demoir/deploy/v60/pack_clean   （= 主机 pack_v60，
#        bin_weights.bin md5 必须 = 706a1d22dfbbf33cf7af7940b9de54c4）
export BIN_PACK=ps BIN_RTL_ROUND=1 BIN_CONV_OOB0=1 BV_FREE=1
export BIN_THR_F32=1 BIN_LAZY_DEQ=1 BIN_CAT_FUSION=1 BIN_BILINEAR_C=1
export BIN_PRECOMP=1 Q8CACHE=1 BIN_F32DIRECT=1 BIN_CAT_ECA_FUSE=1
export BIN_EPI_NOINV=1 BIN_LAZY_SLOTS=24
# 输入：p0067_crop256.npy（uint8 (256,256,3)）
#       板端已记录 in_sha1 = c13eb436ed6f4709f16f4422b86aca6f4f4dec7a  ← 必须命中
```

`BIN_PACK=ps` 必须保留：它同时关掉 C8 直读（`_lazy_deq` 只在 `e1e2` 引擎挂上时为真，
`bin_net.py:3126-3133`）⇒ 每层 `finish` 拿到的都是**已反量化的 float32**，
`rint(y·2^fs_out)` 能**精确**还原 int8（除以 2 的幂是精确运算），不需要读物理内存。

## 2. 采集脚本（复制粘贴，**放你自己的目录**，勿改 `ps/pynq/`）

```python
import os, json, hashlib, numpy as np
import bin_net as BN

# —— 12 项清单：int8 层取 conv 的 sat8 输出（反量化前）；fp 层取 f32 卷积输出 ——
WANT_I8 = [0, 1, 2, 3, 5, 6, 37, 75, 113, 189, 226]          # 11 个 idx（见 §3 表）
WANT_FP = {"encoder.conv_first.0"}                              # 只 1 个 f32

def sha1b(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()

rec = {}
_o_fin = BN._PlBackend.finish
def finish(self, task):
    y, rs, te = _o_fin(self, task)
    L = task.layer
    if L.idx in WANT_I8 and isinstance(y, np.ndarray):
        fs = int(L.d["act_fs_out"])
        q = np.rint(np.asarray(y, np.float64) * (2.0 ** fs)).astype(np.int8)
        rec["i8:" + L.name] = np.ascontiguousarray(q)      # (C,H,W) int8
    return y, rs, te
BN._PlBackend.finish = finish

_o_fp = BN.BinNet._fp_conv
def fp_conv(self, x, name, relu=False, stride=1, pad=0):
    y = _o_fp(self, x, name, relu=relu, stride=stride, pad=pad)
    if name in WANT_FP:
        rec["f32:" + name] = np.ascontiguousarray(np.asarray(y, np.float32))
    return y
BN.BinNet._fp_conv = fp_conv

x = np.load("p0067_crop256.npy").astype(np.float64) / 255.0     # (256,256,3)
x = np.ascontiguousarray(x.transpose(2, 0, 1)[:3], np.float32)  # (3,256,256)
net = BN.BinNet("/home/xilinx/demoir/deploy/v60/pack_clean", engine=<原样>,
                H=256, W=256, verbose=False)
outs, st = net.run(x, pipeline=True, nbuf=2)

os.makedirs("layers_p0067", exist_ok=True)
rows = []
for k, a in rec.items():
    fn = k.replace("/", "_").replace(":", "__") + ".npy"
    np.save(os.path.join("layers_p0067", fn), a)                # 先落盘，sha1 用同一份
    rows.append(dict(key=k, shape=list(a.shape), dtype=str(a.dtype),
                     nbytes=int(a.nbytes), sha1=sha1b(a)))
    print("%-70s %-16s %-8s sha1=%s" % (k, tuple(a.shape), a.dtype, sha1b(a)))
json.dump(rows, open("layers_p0067/index.json", "w"), indent=1)
```

> `L.idx` 若在你的 `bin_ref_numpy.Blob` 版本里不存在，用 `blob.layer(0)["idx"]` 的同一字段，
> 或改成按 `L["name"]` 过滤（§3 表里的名字是权威）。

## 3. 要导出的 12 项（主机侧已落盘，sha1 可直接比）

**主机件**：`runs/opt_round1/ps_numeric/p0067_divergence/host/`（int8 `(C,H,W)` C 序；
f32 层 `(C,H,W)` float32 C 序）。sha1 算法 = `sha1(np.ascontiguousarray(a).tobytes())`。

| # | 类型 | 层名 | 形状 | fs_out | 字节 | 主机 sha1（前 12） | 为什么是它 |
|---|---|---|---|---|---|---|---|
| 1 | **f32** | `encoder.conv_first.0` | (48,128,128) | – | 3145728 | `8b70516715ca` | 全网**最上游**的 f32 张量；若它已不同，整条链从第一层就带偏 |
| 2 | int8 | `encoder.encoder_1.rdb.conv_layers.0.conv` | (64,128,128) | 3 | 1048576 | `7c55b4fec700` | 第一个 int8 层（idx 0） |
| 3 | int8 | `…rdb.conv_layers.1.conv` | (64,128,128) | 2 | 1048576 | `082d44e2eb1e` | idx 1 |
| 4 | int8 | `…rdb.conv_layers.2.conv` | (64,128,128) | 1 | 1048576 | `1dae1f7750d0` | idx 2，fs_out=1（1 LSB = 128 u8 灰阶，最敏感） |
| 5 | int8 | `encoder.encoder_1.rdb.conv_post.conv` | (48,128,128) | 5 | 786432 | `d15c331e3371` | 唯一有饱和的早期层（n_clip=142，n_lo=311） |
| 6 | int8 | `encoder.encoder_1.sam_blocks.0.b0.ghost_layers.0.cheap_operation.conv` | (32,128,128) | 4 | 524288 | `26b0d00cba38` | 20 万精确零（死区） |
| 7 | int8 | `encoder.encoder_1.sam_blocks.0.b2.ghost_layers.0.primary_conv.conv` | (32,64,64) | 5 | 131072 | `735b707969a1` | 第一处 128²→64²（`b2` 支路） |
| 8 | int8 | `encoder.encoder_1.down.conv` | (96,64,64) | 3 | 393216 | `46356d5caec9` | stride=2 形状切换（256²→128² 的第二次降采样） |
| 9 | int8 | `encoder.encoder_2.down.conv` | (192,32,32) | 2 | 196608 | `215c0d7b6c9c` | stride=2（128²→64²） |
| 10 | int8 | `decoder.preconv_3.conv` | (64,32,32) | 2 | 65536 | `d93a19068bd3` | 解码器入口（idx 113） |
| 11 | int8 | `decoder.preconv_1.conv` | (64,128,128) | 3 | 1048576 | `dc187c674f8d` | **主机上 1-ULP 扰动时第一个翻 bit 的层**（idx 189） |
| 12 | int8 | `decoder.decoder_1.sam_blocks.0.b4.conv.conv` | (64,32,32) | 2 | 65536 | `2d2d57653d9e` | **最后一个 int8 层**（idx 226）；它的输出经 SAM 混合 → 最终 f32 卷积 |

合计 ≈ 9.5 MB。

> **不需要**导出 `decoder.decoder_1.conv`：`out1 = pixel_shuffle(conv_fp(...), 2)`
> （`bin_net.py:2545-2550` 的 `decoder_level`），所以板端已经 scp 回来的
> `p0067_out1_f32.npy` **就是**那个 f32 卷积的输出（只差 `pixel_shuffle` 的通道/空间重排）。**先只回 sha1**：任何一项与上表不同 ⇒ 那一层就是"第一个发散层"的上界，
随即只 scp 该项 + 它的前一层即可（其余 10 项可不再传）。

## 4. 判读规则（主机侧直接照此执行）

按"**从上游往下游**"扫，第一个 sha1 与上表不符的项就是"第一个发散层的上界"：

| 观测形态 | 判读 | 下一步 |
|---|---|---|
| 12 项**全等** | 差异在 out1 那一跳之外：`decoder_1` 的 SAM 混合（`sam_mix_f32`/ECA `exp`）或最终 f32 卷积（`conv2d_f32` 的 gemm 求和序），或 u8 `rint(clip)` 口径 | 复查板端 `u8()` 实现；再导出 `sam_blocks.0` 的 4 个分支 f32（`b0/b2_up/b4_up/weighted_sum`） |
| **#1 不同**（f32 `conv_first`），#2~#11 相同 | v60 上游 f32 已差、但被二值化吸收 ⇒ 与主机 1-ULP 剖面完全同构（主机上 #1 差、前 188 层 int8 全同） | 量 #1 的差值：`max|Δ|`,有几个像素 >0；据此在主机上加等效扰动复现 145972 |
| **#1 不同 且 #2 起沿链不同** | 起点在上游 f32 且未被吸收：`bin_ref_numpy.py:1091 conv2d_f32()`（im2col+gemm，ARM BLAS 求和序 vs x86）或 `_fp_conv` 的 relu 口径 | 主机上换 `conv2d_f32_ref` / 关 `BIN_BILINEAR_C` 复现（本报告 §4.3 已给 6262 px 的对照量级） |
| **#1~#k 全等，第 k+1 项开始不同** | **第一个发散的 int8 层 = 第 k+1 项**（这就是本任务的答案） | 按 §5 取该层 `packed A` + `dint`，主机把 `conv_rtl` 拆"打包→dint→epilogue"三段对 |
| 只有 **#11/#12** 不同 | 起点在 `decoder_1`（最后 38 层，通道 448/96、`fs_out=2~3`） | 同上一行，只是范围小；优先查 `bin_ref_numpy.py:508-521` 的 `(dint*alpha_q)>>alpha_shift` 与 `>>shift` 舍入 |
| 只有 **#2/#3/#4** 不同 | 起点在 encoder 头部；#5（`conv_post`，早期唯一饱和层）一起不同 ⇒ 先查饱和 | 查该层 `n_clip` 附近的 `sat8` 与 `gamma_q/slope_q` |

## 5. 附：一旦确认发散层，需要的第二份材料（**现在不要传**）

* 该层的 **packed 二值输入** `A`（`(cb, H, W)` uint64，`bin_ref_numpy.binarize_pack` 的输出）
  或等价物：该层输入的 int8 `x_q`（`sat8(rint(x·2^fs_in))`）+ 逐通道 `t_q`/`t_f32`。
* 该层的 `dint`（`(cout,Ho,Wo)` int32/int64，未乘 `alpha_q` 的那个累加和）。

有了这两样，主机侧能直接把 `conv_rtl` 拆成"打包 → dint → epilogue"三段逐段对，
不必再猜 RTL 位宽/舍入。
