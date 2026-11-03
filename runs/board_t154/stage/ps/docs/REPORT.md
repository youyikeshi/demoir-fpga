# REPORT — convfp 1.29 s 攻坚（NEON BLAS / ATLAS 复刻）+ ECA 批量化

日期：2026-10-31 ｜ 板：PYNQ-Z2（Zynq-7020，Cortex-A9 ×2 @ 667 MHz，`armv7l`）
代码改动：`pynq_specialized_code/ps/pynq/bin_net.py`、`pynq_specialized_code/ps/pynq/bin_ref_numpy.py`
新增工具：`ps/tools/{test_eca_batch_ab,bench_blas_ab,probe_atlas_order,probe_convfp_contract}.py`

---

## 0. 结论速览

| # | 结论 | 证据 |
|---|---|---|
| 1 | **板端 ATLAS 的 `sgemm` = K 全串行 + 每步 f32 舍入 + 无 FMA + 与分块无关** | 逐位（uint32）比对，多个真实形状 **0 diff** |
| 2 | ⇒ **PL 可以逐位复刻 float conv**（不是"相近"，是 bit-identical） | `probe_atlas_order.py` / `probe_convfp_contract.py` |
| 3 | 生产 `conv2d_f32` 就是 `alpha=1, beta=0` 的 K 串行 gemm + bias | T1/T2 在 4 个真实层全 `True` |
| 4 | OpenBLAS **能编**，GEMM 0.70→1.76 GFLOP/s（2.5×，2 线程），但**convfp 只快 12%** | §2.3/§2.4 |
| 4b | 换 BLAS 会改位（max 1216 ULP）⇒ 冻结 sha1 会变 | §2.3 |
| 5 | ECA 批量化 **0 ULP 逐字节等价**，板端 168.9 → 70.9 ms（2.38×） | `test_eca_batch_ab.py` |
| 6 | ECA 的 <50 ms 目标**不可达**：地板是 DDR（33 MB 特征图 ⇒ 53 ms） | 计时归因 |
| 7 | convfp 的真瓶颈不是 BLAS：**im2col 固定 25~30% + cout=12 三层 BLAS 效率崩** | §2.4 |

**最重要的一条**：convfp 的最优解不是换 BLAS，是**把 float conv 搬进 PL 并按 K 串行复刻**。
这既是 0.16~0.32 s 的来源，也顺手把"PL vs PS 数值不一致"这个长期风险彻底消除。

---

## 1. 板端环境（探针原始结果，采信不重推）

```
Linux pynq 6.12.40-xilinx armv7l
Features : half thumb fastmult vfp edsp neon vfpv3 tls vfpd32   ← NEON 在
gcc 11.2.0（make/g++/as/ar/cmake/git 齐全，2 核，20 GB 可用，748 MB avail）
numpy 1.21.5  /usr/lib/python3/dist-packages/numpy（Debian 包）
BLAS: ATLAS 3.10.3（alternatives → atlas/libblas.so.3），dpkg 标注 "generic static"
板端网络: 无默认路由、无 DNS ⇒ apt 不可用
numpy 自带 openblas: 整盘 find 为空 ⇒ LD_PRELOAD 白拿不成立
```

宿主机：x86_64、有网，但 **sudo 需密码且没有 arm-linux-gnueabihf-gcc**（apt 无候选）
⇒ 交叉编译不可行 ⇒ 只能"宿主下载源码 + 板上原生编译"。

---

## 2. 任务 A：OpenBLAS for ARMv7

### 2.1 关键否决点：OpenBLAS 32 位 ARM 没有 NEON SGEMM
```
kernel/arm/KERNEL.ARMV7:
    SGEMMKERNEL = sgemm_kernel_4x4_vfpv3.S     ← 标量 VFPv3，不是 NEON
    （kernel/arm/ 里只有 sgemm_kernel_4x2_vfp.S / sgemm_kernel_4x4_vfpv3.S）
Makefile.arm: CORE=ARMV7/CORTEXA9 用 -mfpu=vfpv3（只有 HAVE_NEON 才加 -mfpu=neon）
```
⇒ 即使编成功，SGEMM 走的是**标量 VFP 4×4**内核。A9 上 VFP 与 NEON 的理论 FP32
吞吐同为 ≈2 FLOP/cycle/核（A9 NEON 是 64-bit 数据通路，128-bit 指令 2 拍）
⇒ **单核天花板 ≈1.3 GFLOP/s，双核聚合 ≈2.0~2.2**，不是 2.7。

### 2.2 构建（必须打的一个补丁）
```bash
# 宿主
curl -sSL -o OpenBLAS.tar.gz https://codeload.github.com/OpenMathLib/OpenBLAS/tar.gz/refs/tags/v0.3.30
scp OpenBLAS.tar.gz xilinx@192.168.7.2:/home/xilinx/
# 板上
tar xmzf OpenBLAS.tar.gz -C obl --strip-components=1     # ★ -m：板钟偏慢，源码 mtime 在未来会搞乱 make
cd obl && nice -n 19 make -j2 TARGET=ARMV7 NOFORTRAN=1 NO_LAPACK=1 \
        USE_THREAD=1 NUM_THREADS=2 shared
```
**补丁（必须）**：`cpuid.S` 尾部的 `.section .note.GNU-stack,"",@progbits` 是 x86 语法，
ARM 的 `as` 直接报 `junk at end of line`，prebuild 阶段就挂。改成：
```c
#if defined(__ELF__) && defined(__linux__) && (defined(__i386__) || defined(__x86_64__))
        .section        .note.GNU-stack,"",@progbits
#endif
```

### 2.3 结果（板端，空闲）
编译产物：`libopenblas_armv7p-r0.3.30.so`；`LD_PRELOAD` 生效证据 = `/proc/self/maps` 里
出现该 .so，且 `LD_DEBUG=bindings` 显示 `sgemm_*` 绑定到它。

| 配置 | GEMM 48×300×16384 | GFLOP/s |
|---|---|---|
| ATLAS（基线，1 线程） | 672.7 ms | **0.70** |
| ATLAS + 2 线程 M 切分（现行） | 378.2 ms | 1.25 |
| OpenBLAS 1 线程 | 522.5 ms | 0.90 |
| **OpenBLAS 2 线程（内部）** | **268.0 ms** | **1.76** ← 达标 |
| OpenBLAS 1 线程 + 2 线程 M 切分 | 301.8 ms | 1.56 |

**ULP 双数（vs ATLAS 参考，2 线程）**：
```
max|Δ|/RMS = 3.292e-06          (rms=17.38)
|ref|>=1%RMS 的 max_ulp = 1216   (780144/786432 个元素参与)
判据 <=15 ULP -> FAIL
```
⇒ **换 BLAS 会改位**（OpenBLAS 的 VFP 内核 K 方向做了多累加器展开，不是纯串行）。
`max|Δ|/RMS=3.3e-6` 数值上无害，但**冻结 sha1 会变**，且违反"≤15 ULP"判据。

### 2.4 ⚠ 但换 BLAS 救不了 convfp —— 这是本节最重要的结论
真实 4 层（CPU，非整网）：

| 配置 | conv_first | dec_1 | dec_2 | dec_3 | **合计** |
|---|---|---|---|---|---|
| ATLAS + 2 线程（现行） | 466.8 | 465.7 | 125.7 | 46.4 | **1104.6 ms** |
| OpenBLAS 内部 2 线程 | 357.4 | 590.9 | 152.8 | 40.3 | 1141.4 ms |
| OpenBLAS 1 线程 + 2 线程切分 | 389.0 | 435.4 | 115.8 | 31.4 | **971.6 ms** |

⇒ **整层 convfp 只快 12~14%（1.29 s → ~1.0 s），远不是"0.5 s"。**

**为什么**（im2col vs gemm 拆分）：
| 层 | im2col | gemm(ATLAS) | gemm(OpenBLAS2) | gemm 占比 |
|---|---|---|---|---|
| conv_first | 69 ms | 650 ms | 264 ms | 90% → 79% |
| decoder_1 | 132 ms | 411 ms | 435 ms | 76% |
| decoder_2 | 36 ms | 103 ms | 109 ms | 74% |
| decoder_3 | 10 ms | 26 ms | 27 ms | 72% |

两个原因：
1. **im2col 固定占 25~30%**，换 BLAS 一分钱也省不掉（`decoder_1` 单层就 132 ms）。
2. **3 个 decoder 层 `cout=12`，BLAS 效率崩到 ~0.5 GFLOP/s，OpenBLAS 一点不快**
   （435 vs 411 ms，反而更慢）——M=12 太小，4×4 微内核的寄存器复用吃不满。
   "算术强度 ≥6 FLOP/B ⇒ 算力受限"这个判断对**conv_first 单独成立**，对整个 convfp 不成立。

⇒ **"GEMM 微基准 ≥1.5 GFLOP/s ⇒ convfp 0.5 s" 这个推理是错的**：
convfp 不是一个大 GEMM，而是 1 个大 GEMM + 3 个"cout 极小"的病态形状 + 固定 im2col 税。
这条同时**加强了 PL 路线**：PL 直接读特征图做卷积（无 im2col），且能针对 cout=12 做固定分块，
把这三块一起省掉。

---

## 3. 任务 A'：ATLAS `sgemm` 的累加顺序（本次最高价值）

### 3.1 结论：一个确定的、可照抄的公式
```
c = 0.0f
for k = 0 .. K-1:                       # 严格 k 递增，不许重排/分块/多累加器
    c = f32( c + f32( a[k] * b[k] ) )    # 乘积先舍入成 f32，相加再舍入
```
即 `VMLA.F32` 串行链，`alpha=1 / beta=0`（numpy 调用 sgemm 就是这两个值）。

### 3.2 逐位证据（`probe_atlas_order.py`，uint32 视图比对）
| 形状 (M,K,N) | 模型 | 结果 |
|---|---|---|
| 48,300,64 | **K 全串行 / 每步舍入** | **bit-exact, 0/3072** |
| 48,300,64 | 融合 FMA（乘积不舍入） | ✗ 2493/3072，max 3634 ULP |
| 48,300,64 | float64 累加器 | ✗ 2779/3072 |
| 48,300,64 | K 分块 150 / 100 / 96 / 64 / 32 + 块末相加 | ✗ 全错（2760~2793/3072） |
| 48,300,256 | K 全串行 | **bit-exact, 0/12288** |
| 12,576,64 | K 全串行 | **bit-exact, 0/768** |
| 48,300,4096（抽 64 列） | K 全串行 | **bit-exact** |

### 3.3 与分块无关（这条比"猜分块"更硬）
- N=64 vs N=128（同一 A）前 64 列**逐位相同**
- M=48 vs M=96 前 48 行**逐位相同**
- A 用 F-order / B 用 F-order / B 取大步长列切片 / M=1 / N=1 / N=8：**全部与 K 串行模型逐位相同**
⇒ **blocking 对逐元素的累加序列完全透明**，PL 不需要复刻任何分块参数。

### 3.4 一个必须记住的反例（禁止推广）
`np.dot`（1-D 的 `sdot`）**不是**统一串行：

| 长度 | 是否 == 串行累加 |
|---|---|
| **9**（ECA 的窗口） | **是**（48000 次 0 差异） |
| 32 / 64 / 300 / 576 | **否**（134~188 / 200 不同） |

⇒ ATLAS 在 n≥某阈值切到了展开/向量化内核。**"sdot 串行"只对 n=9 成立。**
**纪律**：以后任何 float32 等价结论，必须写明"测的是哪个形状/哪个长度"。

### 3.5 生产路径的门槛验证（`probe_convfp_contract.py`）
用**真实的 cols**（复用生产代码的 `_cols()`，K 顺序 = (cin,ky,kx)）比：
| 层 | M | K | N | 裸 gemm == K串行 | 生产 `conv2d_f32` == K串行 |
|---|---|---|---|---|---|
| conv_first | 48 | 300 | 16384 | ✅ | ✅ |
| decoder_1 | 12 | 576 | 16384 | ✅ | ✅ |
| decoder_2 | 12 | 576 | 4096 | ✅ | ✅ |
| decoder_3 | 12 | 576 | 1024 | ✅ | ✅ |

⇒ **可以进 RTL**。生产入口 `conv2d_f32` = im2col(该 K 顺序) + K 串行 gemm + bias，
逐位可复刻。注意 bias 是 gemm 之后**一次性 f32 加**，PL 必须同序。

### 3.6 对 RTL 的契约（三条）
1. ✅ **沿 M/N（输出元素）任意并行** —— 每个输出元素各自 K 串行，互不影响。
2. ❌ **绝不对 K 并行归约 / 树形相加 / 多累加器** —— 一改顺序就不再 0 ULP。
3. ❌ **不要直接用 DSP 的 FMA 模式** —— 那是 VFMA 语义（上表 ✗ 那一行）。
   若 DSP48 只能 FMA，必须**显式把乘积先舍入到 f32** 再累加；否则 PL vs PS 会差几千 ULP。

### 3.7 关于"PS 串行要 10 s，ATLAS 只要 1.29 s"的疑问
用 numpy 写 K 串行模型**必然**慢一个数量级（板端实测 4 层 29.4 s vs ATLAS 2.5 s，
带负载；空闲时比例类似）。**原因不是 ATLAS 偷偷加速**：
- numpy 每步要把 `M×N` 的 C **整块读+写**一遍 ⇒ 300 步 × 3.1 MB ≈ 0.94 GB ⇒ 纯带宽就 ~1.5 s
- ATLAS 的 4×4 微内核把 16 个累加器**留在寄存器**里，C 只写一次
两者的**逐元素数学顺序完全相同**（§3.2 已逐位证明）。所以 "PS 串行慢" 不能反推 ATLAS 不朴素。

---

## 4. 任务 B：ECA 的 1152 次 `np.dot` → 批量化

### 4.1 改法（`bin_ref_numpy._windowed_dot_exact`）
不做 BLAS 调用，显式按 k=0..8 **串行**累加 float32，用滑窗视图 + 17 次 ufunc。
关键点是**累加顺序与 `sdot` 一致**，所以是"换个写法，不是换个算法"。

### 4.2 逐字节验收（板端，6 个真实 ECA 权重 × 真实量级激活）
| ECA 层 | C | 形状 | exact diff | exact ULP |
|---|---|---|---|---|
| encoder_1 | 144 | 128² | **0** | **0** |
| encoder_2 | 288 | 64² | **0** | **0** |
| encoder_3 | 576 | 32² | **0** | **0** |
| decoder_3 | 192 | 32² | **0** | **0** |
| decoder_2 | 192 | 64² | **0** | **0** |
| decoder_1 | 192 | 128² | **0** | **0** |

前提独立验证：板端 `sdot(n=9)` == 串行（6 权重 × 4000 向量 = **48000 次 0 差异**）。

### 4.3 时速（板端空闲，best of 10）
| ECA 层 | C | 形状 | ref ms | exact ms | 加速 |
|---|---|---|---|---|---|
| encoder_1 | 144 | 128² | 25.59 | 17.29 | 1.48× |
| encoder_2 | 288 | 64² | 28.76 | 10.84 | 2.65× |
| encoder_3 | 576 | 32² | 45.01 | 7.71 | **5.84×** |
| decoder_3 | 192 | 32² | 16.40 | 4.87 | 3.37× |
| decoder_2 | 192 | 64² | 19.83 | 8.30 | 2.39× |
| decoder_1 | 192 | 128² | 33.32 | 21.88 | 1.52× |
| **合计** | 1584 | | **168.9 ms** | **70.9 ms** | **2.38×** |

（编译负载下同一 test 得到 194→95 ms / 2.04×，比例接近。）

**为什么没到 <50 ms —— 而且是不可达的**。归因（`C@dims`：mean / dots / pad+sig）：
```
144@128²  mean 15.9  dots 1.1
288@64²   mean  8.7  dots 1.2
576@32²   mean  4.1  dots 1.4
192@32²   mean  1.7  dots 1.2
192@64²   mean  5.5  dots 1.2
192@128²  mean 41.1  dots 1.2      ← mean 占 90%+
```
dot 已被压到 ~1.2 ms/层（不再是瓶颈）。剩下的是 `x.mean(axis=(1,2))`：**必须读完整张特征图**。
6 层合计 33 MB，DDR 实测 621 MB/s ⇒ **光读一遍就是 53 ms 地板**。
⇒ 要到 <50 ms 只能把 mean **融进生产者**（cat/sam 写出时顺带累加通道和），是数据流改动，不在本任务范围。

### 4.4 平台自检开关（重要，保住两个冻结基线）
`bin_net.FAST_ECA` 默认 `"auto"`：import 时跑 96 次 `_sdot_is_sequential()` 自检。
- 板端（ATLAS）：`sdot` 串行 ⇒ 开 ⇒ **0 ULP，`bfa246d34681` 不变**
- x86 主机（OpenBLAS FMA）：`sdot` 不串行 ⇒ 退参考实现 ⇒ **`104710f69c4d` 不变**
  （主机上 batch 版会差 3~4 ULP、15~84/1584 通道 —— 所以**不能**无条件默认开）
`BIN_FAST_ECA=0/1` 可强制。

---

## 5. 复现
```bash
# 板上（代码在 /home/xilinx/eca_ab）
python3 probe_atlas_order.py                # §3 的全部逐位证据
python3 probe_convfp_contract.py conv_pack  # §3.5 的 RTL 门槛
python3 test_eca_batch_ab.py --pack eca_pack --repeat 10   # §4
# 宿主
python3 pynq_specialized_code/ps/tools/bench_blas_ab.py --save /tmp/ref.npy
LD_PRELOAD=/home/xilinx/obl/libopenblas.so.0 python3 ... --compare /tmp/ref.npy
```

## 6. 未做 / 风险 / 待确认
- **PNG 未跑整网**（按约束只做 CPU 微基准，不碰 PL）。
- ECA 的 95 ms 是在编译负载下测的；空闲时应略好，但**带宽地板不变**。
- OpenBLAS 的 ULP 双数已填（§2.3）：max|Δ|/RMS=3.29e-6，max_ulp=1216 ⇒ 判据 FAIL。
- 板端 OpenBLAS 若真要用，最佳组合是 `OPENBLAS_NUM_THREADS=2` + `BIN_GEMM_THREADS=1`
  （971.6 ms）；但仍需接受 sha1 变化。
- RTL 侧需确认：DSP48 能否拆成 `mul → round → add`（决定能否 0 ULP）。
- 板钟偏慢约 6.5 年（`date` 显示 2025-05-07），`tar` 必须带 `-m`，否则 make 行为异常。
