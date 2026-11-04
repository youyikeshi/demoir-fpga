# 主机侧 int8 整网参照输出（`pack_v60`，对拍用）

**目的**：与板端 `out1` 逐字节对拍，回答"板端算的 out1 == PC 上 int8 模型算的 out1？"
**范围**：纯主机（x86 / numpy 2.2.6），`engine=None` ⇒ 卷积走 `bin_ref_numpy.conv_rtl`。
**未做**：没有 ssh、没有碰板、没有 Vivado/xsim；`pynq_specialized_code/` 与既有 `runs/` 产物**零修改**（本目录是新建的）。

---

## 0. TL;DR

1. 参照已产出：5 张 `pNNNN_out1_u8.npy`（uint8 `(3,256,256)`）+ `pNNNN_out1_f32.npy`（float32 同形），sha1/md5 见 `SHA1SUMS.txt`。
2. **这份参照就是"PC 的 int8 模型"**：`bin_net.BinNet(pack_v60, engine=None)` 是板端 `BinNet` 的同一份代码，唯一差别是卷积后端从 PL 换成 `bin_ref_numpy.conv_rtl`（xsim 黄金向量逐字节校验过的 RTL 复现，`bin_ref_numpy.py check_goldens()`）。
3. **它不代表 RTL 一定对**：`BIN_CONV_OOB0=1` 是"**模型语义**（PyTorch 补 0）"，而 `bin_ref_numpy.oob_zero_on()` **默认是 False**（= 未打补丁的 RTL 现行为）。这一位是主机↔板端"边界像素系统性差异"的第一嫌疑，见 §7.1。
4. 对拍口径**必须用 `out1_u8`**：`net.run()` 返回的是反量化后的 float32，float 层的逐字节比较会被 BLAS/尾位噪声污染，见 §7.4。

---

## 1. 完整 env（**板端照抄这一行**）

```bash
export BIN_PACK=ps BIN_RTL_ROUND=1 BIN_CONV_OOB0=1 BV_FREE=1 \
       BIN_THR_F32=1 BIN_LAZY_DEQ=1 BIN_CAT_FUSION=1 BIN_BILINEAR_C=1 \
       BIN_PRECOMP=1 Q8CACHE=1 BIN_F32DIRECT=1 BIN_CAT_ECA_FUSE=1 \
       BIN_EPI_NOINV=1 BIN_LAZY_SLOTS=24
```

| 变量 | 值 | 谁给的 | 作用 | 数值相关？ |
|---|---|---|---|---|
| `BIN_PACK` | `ps` | 题面 | PS 侧 f32 比较打包（吃容器 `off_thr32` 的**未量化**阈值） | **是**（`e1e2` 会丢亚 LSB 阈值、符号翻错 ~2%） |
| `BIN_RTL_ROUND` | `1` | 题面 | R1 由 RTL/参照做：`sat8((r + (1<<(shift-1))) >>> shift)` | **是**（0 ⇒ 每层 −0.5 LSB） |
| `BIN_CONV_OOB0` | `1` | 题面 | R3(b)：空间越界抽头"不计贡献"（PyTorch 补 0） | **是**（默认 0 = 越界当 xb=−1） |
| `BV_FREE` | `1` | 题面 | 每层释放中间张量 | 否（纯内存） |
| `BIN_THR_F32` | `1` | `RUNPOINT` | 阈值走 f32（`ps` 档前提） | 与 `ps` 同源 |
| `BIN_LAZY_DEQ` | `1` | `RUNPOINT` | 惰性反量化 | 否（实测） |
| `BIN_CAT_FUSION` | `1` | `RUNPOINT` | cat 融合 | 否（实测） |
| `BIN_BILINEAR_C` | `1` | `RUNPOINT` | bilinear 走 C 内核（`-ffp-contract=off`） | 设计上否（实测否） |
| `BIN_PRECOMP` | `1` | `RUNPOINT` | 预计算描述符 | 否 |
| `Q8CACHE` | `1` | `RUNPOINT` | q8 缓存 | 否 |
| `BIN_F32DIRECT` | `1` | `RUNPOINT` | f32 直通 | 否（实测） |
| `BIN_CAT_ECA_FUSE` | `1` | `RUNPOINT` | cat+ECA 融合 | 否（实测） |
| `BIN_EPI_NOINV` | `1` | `RUNPOINT` | epilogue 免求逆 | 否（实测） |
| `BIN_LAZY_SLOTS` | `24` | `RUNPOINT` | 惰性槽位数 | 否（纯内存） |

`RUNPOINT` 其余项来自 `pynq_specialized_code/ps/pynq/demo_pipeline.py:63` 与
`board_verify_numfix.py:53`（板端 run100 用的就是这套默认）。

**"哪些 env 真的进数值"——实测（`env_ab_p0000.json`，11 次整网）**：
p0000 上 `{BIN_PACK=ps, BIN_RTL_ROUND=1, BIN_CONV_OOB0=1, BV_FREE=1}` 与**全 14 项**
的 `out1_u8` sha1 **完全相同**（`963eef0631dc…`），且 10 个 `RUNPOINT` 附加键**逐个单独打开**
也全是同一 sha1 ⇒ **只有 4 个显式键参与数值**，其余是性能/内存开关。
（但板端仍建议照抄全量 —— 与 run100 严格同环境，省掉解释成本。）

另：`pipeline=True,nbuf=2` 与 `pipeline=False` 在 p0000 上 **`array_equal=True`**、
sha1 全等 ⇒ 流水不改变数值，本参照用 `pipeline=True, nbuf=2`（与板端一致）。

---

## 2. 参照实现入口与关键函数

| 环节 | 位置 | 说明 |
|---|---|---|
| 入口（本目录脚本） | `host_int8_ref/make_host_ref.py` | 建网 + 跑 5 张 + 落盘 + 指标 |
| 整网 | `pynq_specialized_code/ps/pynq/bin_net.py:3046` `BinNet.run(x, pipeline=True, nbuf=2)` | 与板端**同一份**代码 |
| 卷积后端（无板） | `bin_ref_numpy.py:433` `conv_rtl(...)` | 逐位复现 `bin_conv_core.v` |
| 卷积语义开关 | `bin_ref_numpy.py:355 oob_zero_on()` / `:365 rtl_round_on()` / `:392 rtl_round_of()` / `:386 _half_lsb()` | R3(b) / R1 / 配对自证 |
| 越界补偿 | `bin_ref_numpy.py:416 tap_sum_real()` | `dint(oob_zero=True) = Σ Σ_real xb·wb`（精确，非近似） |
| 饱和 / 分辨率 | `bin_ref_numpy.py:119 sat8()` / `:128 out_hw()` | 与 RTL 同式 |
| PS 打包（`BIN_PACK=ps`） | `bin_net.py:1128-1133` → `bin_engine.py:4085 pack_thresh()` → `bpack.c:34 bpack_f32()` | 判据 `x_f32 > −t_f32`，严格 `>` |
| 反量化（out1 生成处） | `bin_net.py:1156` `y_q.astype(float32) / 2**act_fs_out` | 所以 `out1` 是 **float32**，不是 u8 |
| fp 卷积 ×4（`decoder_1` 等） | `bin_net.py:287 conv2d_f32()` → `convpf_c` C 内核（`-ffp-contract=off`） | 权重在 `pack_v60/fp_layers/*.f32.bin` |
| 板端读取/比较口径 | `board_verify_numfix.py:237` `o = np.asarray(outs["out1"], np.float64)`、`:48 u8()`、`:238 sha1[:12]` | 本目录口径与它逐字对齐 |

`bin_ref_numpy.py` 的自证：`python3 bin_ref_numpy.py golden <pl/rtl_hand/golden>` 用 5 组
xsim 向量逐字节比对 `conv_rtl`（本次未跑，只读引用；`check_goldens()` 在 `:1603`）。

---

## 3. 输出口径（对拍前必须先认同这份定义）

| 项 | 定义 |
|---|---|
| 容器 | `runs/opt_round1/ps_numeric/pack_v60/`，`bin_weights.bin` md5 = `706a1d22dfbbf33cf7af7940b9de54c4`（**已校验**）；`round_q=0` / `slope_shift=8` / `alpha_shift=9` |
| 输入 | `runs/frozen256/pNNNN_crop256.npy`（uint8 `(256,256,3)`，原样）→ `load_input()`：`(3,256,256)`、`/255`、**float32**（`in_sha1` 5/5 命中 `runs/frozen256/index.json`） |
| out1 形状 | **`(3,256,256)`**，C 序（板端 `board_verify_numfix.py:237` 读到 `(3,256,256)`，golden docstring 亦为 `(3,256,256)`） |
| `out1_f32` | `BinNet.run()` 原值，float32 |
| `out1_u8` | `np.rint(np.clip(np.asarray(out1, np.float64), 0.0, 1.0) * 255.0).astype(np.uint8)`——与 `board_verify_numfix.py:48 u8()` **逐字相同** |
| sha1 | `sha1(np.ascontiguousarray(a).tobytes())`，`a` 为 uint8 C 序 `(3,256,256)` |
| md5 | 文件 md5（`.npy` 含头，跨机器同字节即可比） |

> ⚠ `out1_f32` 的 sha1 也算了一列，但**只能当"同环境复现"的旁证**：float32 反量化的
> 尾位依赖 numpy/BLAS 版本，板端 numpy 1.21.5 vs 主机 2.2.6 未必逐位一致。

---

## 4. 5 张统计

### 4.1 `out1_u8`（对拍口径）

| 样本 | min | max | mean | std | `=0` | `=255`（饱和） | sha1[:12] |
|---|---:|---:|---:|---:|---:|---:|---|
| p0000 | 0 | 177 | 54.640 | 25.452 | 0.00% | 0.00% | `963eef0631dc` |
| p0021 | 48 | 251 | 161.262 | 37.800 | 0.00% | 0.00% | `573d2d3b4107` |
| p0043 | 195 | 255 | 239.263 | 7.232 | 0.00% | 0.23% | `8e83ff405d46` |
| p0067 | 188 | 255 | 239.186 | 7.467 | 0.00% | 0.15% | `2d699f822df7` |
| p0089 | 15 | 216 | 123.195 | 42.533 | 0.00% | 0.00% | `0153b868559f` |

### 4.2 `out1_f32`（反量化原值，`[0,1]` 之外即会被 u8 截断）

| 样本 | min | max | mean | std | sha1[:12] |
|---|---:|---:|---:|---:|---|
| p0000 | −0.0203 | 0.6938 | 0.21428 | 0.09980 | `57a1bf161f26` |
| p0021 | 0.1893 | 0.9845 | 0.63240 | 0.14823 | `aacc6d925fbc` |
| p0043 | 0.7649 | 1.0418 | 0.93830 | 0.02837 | `66d9054e6d28` |
| p0067 | 0.7377 | 1.1060 | 0.93800 | 0.02929 | `2b1cd63cc7ef` |
| p0089 | 0.0579 | 0.8484 | 0.48312 | 0.16679 | `3e8982a98f6b` |

完整 sha1/md5 见 `SHA1SUMS.txt`；结构化结果见 `host_ref.json`、`env_ab_p0000.json`。

---

## 5. 与 float golden 的对照（"主机 int8 自己离 float 有多远"）

golden = `runs/frozen256/golden/pNNNN_out1_fp.npy`（float64 `(3,256,256)`）。
`mean|Δu8| / PSNR / corr / Δmean / σ比`，均与 `board_verify_numfix.metrics()` 同式。

| 样本 | golden mean/std | `mean\|Δu8\|` | max`\|Δu8\|` | PSNR | corr | Δmean | σ比 |
|---|---|---:|---:|---:|---:|---:|---:|
| p0000 | 0.2453 / 0.0990 | 9.31 | 66 | 27.27 | +0.954 | −0.031 | 1.008 |
| p0021 | 0.5762 / 0.1790 | 18.81 | 78 | 20.96 | +0.927 | +0.057 | 0.828 |
| p0043 | 0.9986 / 0.0073 | 15.08 | 60 | 23.65 | −0.033 (平坦→无效) | −0.061 | 3.884 |
| p0067 | 0.8280 / 0.0126 | 28.16 | 49 | 18.69 | −0.472 (平坦→无效) | +0.110 | 2.322 |
| p0089 | 0.4847 / 0.1652 | 14.36 | 64 | 23.02 | +0.910 | −0.001 | 1.009 |
| **中位** | | **15.08** | | **23.02** | **+0.927**（3/5 有效） | **0.057** | **1.008** |

**怎么读这一列（把"实现误差"和"量化代价"分开）**：这里量的是"主机 int8 参照 vs 不依赖容器的
float 参考"，也就是**量化代价 + 容器/打包代价**的上界，**不含**主机↔板端的实现差异。
所以：

* 板端若与这份参照**逐字节相同** ⇒ 板端与 PC int8 模型之间**零实现差异**，剩下的差距
  （上面这一列）100% 是量化/容器代价，与 RTL 无关。
* 板端若与这份参照**不同** ⇒ 差值 `Δ(board, host)` 就是**纯实现差异**，可以直接从
  "板端 vs golden"里扣掉主机那一列来定位（p0067 已经出现这种信号，见 §6）。

⚠ p0043/p0067 的 golden 只有 1.9 / 3.2 个 u8 灰阶（`ref_std` 0.0073 / 0.0126）：
这两张的 corr/σ比 在统计上没有定义性（见 `PACK_V60.md` §3.1 同口径注），
但它们恰恰是**逐字节对拍最有价值**的两张——任何 1 LSB 都会放大成巨大的相对误差。

---

## 6. 与板端已记录数字并排（**这就是"是否逐字节"的第一手信号**）

板端 V-D（`v60_vd_results.md`，`BIN_PACK=ps`、冷启动单跑、`in_sha1` 5/5 命中）
vs 本参照（同容器 `pack_v60`、同 env）：

| 样本 | 板端 `mean\|Δu8\|` | 主机 `mean\|Δu8\|` | 板端 PSNR | 主机 PSNR | 判读 |
|---|---:|---:|---:|---:|---|
| p0000 | 9.31 | 9.31 | 27.27 | 27.27 | 2 位小数一致（**不足以证明逐字节**） |
| p0021 | 18.81 | 18.81 | 20.96 | 20.96 | 同上 |
| p0043 | 15.08 | 15.08 | 23.65 | 23.65 | 同上 |
| **p0067** | **28.57** | **28.16** | **18.57** | **18.69** | ⚠ **对不上** ⇒ 该样本上板端与主机 int8 **不是逐字节相同** |
| p0089 | 14.36 | 14.36 | 23.02 | 23.02 | 2 位小数一致 |

⇒ **按现有记录，答案是否定的（至少在 p0067 上）**：板端 ≠ 主机 int8。
4/5 张一致只说明差异很小、且大概率集中在少数像素/边界；要定论必须拿到板端
`out1_u8.npy` 或 `sha1(out1)` 与 `SHA1SUMS.txt` 比对（本任务不许 ssh，故未做）。
p0067 的差异量级（`mean|Δu8|` 差 0.41、PSNR 差 0.12 dB，且该样本 golden 极平）
指向"少量像素整块偏移"而不是全网系统性偏移。

**注意**：`v60_vd_results.md` 同页记录了板端容器在 V-D 段错误后被**改写**过
（`bin_weights.bin` 22931 B 变动）。若 run100 那次用的是被改写的容器，上表的
p0067 差异也可能来自容器而非 RTL——这条必须先排掉，否则会把容器事故算到 RTL 头上。

---

## 7. 与 RTL 语义**可能**不一致的地方（点名，不许含糊）

### 7.1 `BIN_CONV_OOB0=1` 只在"RTL 已打越界补丁"时才对得上 ★最高风险

`bin_ref_numpy.oob_zero_on()`（`:355`）**默认 False**，注释原文：
"默认关：保持与**当前 RTL**（越界 = 激活字 0 = xb −1）逐位一致……RTL 打上'越界补偿'
补丁后再打开它（板端同样要打补丁）"。
⇒ 本参照跑的是 `oob_zero=1`（PyTorch 补 0 语义）。若板端位流没打 R3 补丁，
**每层边界一圈像素会系统性差**，而主机这一侧的 sha1 永远对不上。
判据：差异若集中在图像外围/每层 feature 边缘（且随层数累积变宽），先查这条，不要查卷积核。

### 7.2 R1 的"单一来源"由**容器**自证，env 只是覆盖

`rtl_round_of()`（`:392`）：显式参数 > `BIN_RTL_ROUND` > 容器 `round_q` 字段自证。
`pack_v60` 带 `round_q=0` ⇒ **即使不设 `BIN_RTL_ROUND`，主机也会做 R1**。
反过来，板端若把 `BIN_RTL_ROUND` 设成 `0` 而容器仍是 `pack_v60`，则每层 −0.5 LSB
（整幅发灰）——这是"环境看起来一样但结果差一档"的经典坑，**必须显式写 1**。

### 7.3 `BIN_PACK=ps` 的比较域是 f32，不是 int8

判据在 `bpack.c:34 bpack_f32()`：`bit = (x[c,hw] > adj[c])`，**严格大于**，`adj = −t_f32`
（`bin_engine.py:4085 pack_thresh()`，用容器 `off_thr32` 的**未量化**阈值）。
而 RTL/整数参照的判据是 `x_q + t_q > 0`（`bin_ref_numpy.py:311 binarize_pack()`）。
两者数学上只在 `t_q` 无舍入时等价；本容器带 `off_thr32` ⇒ `ps` 档走的是精确 f32 判据。
**在 `ps` 档下这不是风险**（PS 打包、PL 只吃字，两侧同一份代码），但它解释了
为什么 `e1e2` 档会出现 ~2% 符号翻转、以及为什么改 `fs_in` 会动结果。

### 7.4 `out1` 是 float32 反量化值 ⇒ "逐字节"必须先定在哪一层

`bin_net.py:1156`：`y_q.astype(np.float32) / 2**act_fs_out`。所以：

* 比较 `out1_u8`（本目录主口径）：`rint` 把 ±1 LSB 以内的 float 噪声吸收掉，
  但**恰好落在 .5 边界**的像素仍可能因尾位差翻 1 LSB。
* 比较 float32 的 sha1：直接暴露 BLAS/尾位差异 —— 板端 numpy 1.21.5、x86 BLAS ≠ ARM
  BLAS，**几乎必然不完全相等**，即使 u8 全等。所以**不要**用 float sha1 判"实现对不对"。

### 7.5 fp 卷积 ×4 与 C 内核走的是"同源不同架构"的 .so

`decoder_{1,2,3}.conv` + `conv_first` 是全精度 f32 卷积（`bin_net.py:287 conv2d_f32()`），
主机 `convpf_c_x86_64.so`、板端 `convpf_c_armv7l.so`，同一份 `convpf_c.c` 且编译带
`-ffp-contract=off -fno-fast-math`（`convpf_c.py:59-63`）⇒ 设计上逐位。同理
`bilinear_c` / `bpack` / `q8` / `c8deq`。但**前提是这些 .so 在板上真的加载成功了**
（`[DEPS] q8=fast c8deq=fast bpack=fast …` 这行必须全 `fast`）；任何一项回落 numpy
都可能带进尾位差异，而它只影响 f32 层 → 只通过 int8 舍入边界间接影响 out1。

### 7.6 整数位宽：主机全程 int64，RTL 未必

`conv_rtl()` 用 `np.int64` 累加 `dint`、`dint*alpha_q`，**不可能溢出**；RTL 的累加器/
乘法位宽（以及 `>>> shift` 前会不会饱和）若不足，会得到主机永远看不到的结果。
这类差异的特征是"少数点、大偏差"，与 §7.1 的"边缘系统性偏差"可区分。

---

## 8. 复现

```bash
cd /media/felix/Data/coding/demoir-fpga
python3 runs/opt_round1/ps_numeric/host_int8_ref/make_host_ref.py    # ≈100 s（5 张 × ~19 s）
```

板端对拍（同 env，导出 u8 后传回主机）：

```python
# 板端，import bin_net 之前先 export 上面那行 env
outs, st = net.run(x, pipeline=True, nbuf=2)
u = np.rint(np.clip(np.asarray(outs["out1"], np.float64), 0, 1) * 255).astype(np.uint8)
np.save("p0000_out1_u8_board.npy", u)
print(hashlib.sha1(np.ascontiguousarray(u).tobytes()).hexdigest()[:12])   # 对 SHA1SUMS.txt
```

---

## 9. 交付清单

| 文件 | 内容 |
|---|---|
| `pNNNN_out1_u8.npy` ×5 | uint8 `(3,256,256)`，对拍主口径 |
| `pNNNN_out1_f32.npy` ×5 | float32 `(3,256,256)`，`BinNet.run()` 原值 |
| `SHA1SUMS.txt` | 每张 `in_sha1` / `sha1_out1_u8` / `sha1_out1_f32` / 两个 md5 + 算法一句话 |
| `host_ref.json` | 完整 env、逐张 sha1/指标/耗时、u8 统计、A/B 结论 |
| `env_ab_p0000.json` | env 数值相关性 A/B（core-4 vs full RUNPOINT vs 落盘文件） |
| `make_host_ref.py` | 生成脚本（只读调用 `pynq/`，不改任何生产代码） |
| `REPORT.md` | 本文件 |
