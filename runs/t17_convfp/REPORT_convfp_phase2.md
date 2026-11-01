# T-17 convfp 第二阶段：实现与判据

日期 2026-11-02 ｜ 副本 `worktrees/T-17-convfp` ｜ **基线 commit `70dcfa1`** ｜ 全程离线，未上板

> ⚠ **基线漂移（重要）**：我按 brief 从 `70dcfa1` 建副本；期间别人把 T-14 依赖闭包/v3 冻结合进了 main，
> 现在 `HEAD = 7e57b42`（`70dcfa1 → c0e9afe → 950a1ff → 7e57b42`）。
> 已核：这三个 commit **没有碰**我改的四个函数（`conv2d_f32` / `gemm_split_m` / `_fp_conv` / `add_f32`，
> 行号都还是 837/899/1358），所以我的 hunk 与它们**不重叠**。
> 已生成 `runs/t17_convfp/T17_convfp.patch`（246 行），**在 `HEAD=7e57b42` 的干净树上 `patch -p1 --dry-run` 通过**。
> ⇒ 合并请用这个 patch，**不要**整文件覆盖 `bin_net.py`（我的副本缺 c0e9afe 加的 §②/§④ 那两块，整覆盖会回退它们）。
> **已验证到"打完补丁 == HEAD + 我的 hunk"**：在 `HEAD=7e57b42` 的树上真打了一遍，
> 结果与我副本的差异只有 **71 行 `<`（HEAD 自己新增的 §②/§④ 块）+ 1 行 `>`**，
> 那 1 行就是 HEAD 把 `mmio_skipped=...))` 改成了 `mmio_skipped=...,` + 追加
> `kernel_deps/deps_closure_ok/perf_gate/run_valid` —— 不是我碰的地方，patch 正确保留了它。

## 1 改动文件清单（合并冲突评估用）

| 文件 | 性质 | md5 | 说明 |
|---|---|---|---|
| `ps/pynq/bin_ref_numpy.py` | 改 | `103b867e2e158c378421acd06b0a2698` | 见下表"改动的函数"|
| `ps/pynq/bin_net.py` | 改 | `a85927f30f1adb6b3c6c6538d7b63ef4` | 2 处：import；`_fp_conv` |
| `ps/pynq/convpf_c.c` | 新增 | `10b31d43578355887ae2cbbcc45d3704` | **内核源码（主机↔板端 md5 必须对齐的就是这个）** |
| `ps/pynq/convpf_c.py` | 新增 | `e30f6ff7213449a4d19ddba5f8288635` | 加载器 + 加载时逐字节自检 + 变体选择 |
| `ps/pynq/convpf_neon_shim.h` | 新增 | `00382c57fed47d0645f4a8bc41f1132d` | **仅主机测试用**（可移植 NEON 子集），板端不 include |
| `ps/tools/convpf_ab.py` | 新增 | `805fadc2ae67a23cf1a24cc220427bb1` | 门禁 A/J1/J1b |
| `ps/tools/convpf_neon_sim.py` | 新增 | `f93e1685a08b0f54b307f8be5403a983` | 门禁 J1c（NEON tile 在主机上的逐字节验证）|
| `ps/tools/convpf_timing.py` | 新增 | `c5f100158f4306f1eb7899f0584ee487` | 交错 A/B 计时（配对差 + 奇偶换序）|
| `ps/tools/convpf_project.py` | 新增 | `131b7dd7568ea80991b970510694e966` | J4 投影（指令计数模型）|
| `ps/tools/probe_serial_k.py` | 新增 | `e804ad08a39eda9f1b0d0b2def83752a` | **J3 板端门禁**（也放在 `runs/t17_convfp/`）|

**改动的函数逐个列**（用来算冲突面）：

| 文件 | 函数/符号 | 改动性质 | 默认（开关全关）行为 |
|---|---|---|---|
| bin_ref_numpy.py | `conv2d_f32` | 函数体改写（加 NOPAD 分支 / C 内核分支 / `out=` 就地 bias） | **逐字节不变**（已证，见 §3） |
| bin_ref_numpy.py | `gemm_split_m` | 加 `if GEMM_POOL:` 分支（走池）；老路径原样保留 | **逐字节不变** |
| bin_ref_numpy.py | `_gemm_pool` / `_gemm_slice` / `_GEMM_EX*` | 新增 | 不执行 |
| bin_ref_numpy.py | `_env_on` / `CONVPF_NOPAD,FUSE,C` / `GEMM_POOL` | 新增模块级常量 | 全 False |
| bin_ref_numpy.py | `_convpf_c_try` / `_cpf_note` / `_cpf_why` / `convpf_c_status` | 新增 | 不执行 |
| bin_net.py | `from bin_ref_numpy import (...)` | 加 `CONVPF_FUSE` | 无影响 |
| bin_net.py | `BinNet._fp_conv` | relu 就地（`np.maximum(...,out=y)`）| **逐字节不变** |

`.so` 不进 git：仓库根 `.gitignore` 第 145 行已有 `pynq_specialized_code/ps/pynq/*.so`，
产物名 `convpf_c_<arch>.so`；另有一个测试产物 `convpf_c_neonsim.so`（同样的规则覆盖）。

## 2 开关（全部 opt-in，默认关）

| 开关 | 默认 | 作用 | 不合法/异常时 |
|---|---|---|---|
| `BIN_CONVPF_NOPAD=1` | 关 | 改动 1：`np.pad` 不物化，边界 tap 直接把 `+0.0` 写进 cols | 形状/stride 不满足 ⇒ 走原路径 |
| `BIN_CONVPF_FUSE=1` | 关 | 改动 2：bias `out=`、relu `out=` 就地 | — |
| `BIN_GEMM_POOL=1` | 关 | 改动 3：常驻线程池代替每次新建 Thread | 池异常 ⇒ 落回老路径 |
| `BIN_CONVPF_C=1` | 关 | 改动 4：im2col-free 直接卷积 C 内核 | **打印一行原因**后回落 numpy |
| `BIN_CONVPF_VARIANT=0/1` | 自动 | 强制内核变体（0=vmul+vadd，1=vmla） | 自检不过 ⇒ 禁用并打印 |
| `BIN_CONVPF_THREADS` | `min(2,ncpu)` | 内核按输出行切几线程 | 出错 ⇒ 整块串行重算 |

## 3 判据结果

| 判据 | 结果 | 证据 |
|---|---|---|
| **A：改动 1/2/3 逐字节** | **PASS** | `convpf_ab.py`：4 层真实输入/权重 × **7 种开关组合**（逐个 + 全开）+ 全关基准，`array_equal` 且 `tobytes()` 相同 |
| **J2：整网 sha1（默认 BLAS 线程口径）** | **PASS** | `verify_lazy_net.py --hw 64`：全关 / 单开 NOPAD / 单开 FUSE / 单开 POOL / 三开 五种配置，A/B/C 三臂**全部** = `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd` |
| **J1：内核 vs 串行标量参考** | **PASS** | 4 层全覆盖（k=3/5、pad=1/2、cin=12/64、cout=12/48），`tobytes()` 0 差 |
| **J1b：全画幅 向量 vs C 标量** | **PASS** | 256² 真实形状**逐像素**（48×128×128 / 12×128×128 / 12×64×64 / 12×32×32），0 差 |
| **J1c：NEON tile 逻辑在主机上验证** | **PASS** | `-DCPF_FAKE_NEON` + `convpf_neon_shim.h`：4 真实层全画幅 + **249 个随机形状** × **2 个 variant**，0 差（覆盖 NR=8 尾块 0~7、pad 0~3、k=1/3/5、掺精确 0） |
| **J3：板端 sgemm 是否串行** | **未跑（板子断电）** | 探针已就绪，`probe_serial_k.py`；**已在主机验证它有分辨力**：主机 OpenBLAS 报 FAIL（57~58/64 元素差 1~10 ulp），同时在主机上 C 内核 vs 串行参考 PASS |
| **J4：板端 ≤350 ms** | **压线（模型 229~386 ms）** | 见 §6；**不能宣称 PASS，必须板端实测** |
| J5：口径分离 | 遵守 | 计时命令 = `OMP/OPENBLAS/MKL=1` + `taskset -c 4`；门禁命令 = `env -u OMP_NUM_THREADS ...` 默认线程 |

**逐字节证据链**（缺一环都不算）：
```
Python 串行参考 ──J1(4 层)── C 标量 convpf_direct_s
                              │
                              ├──J1b(256² 全画幅 逐像素)── C 向量（主机原生构建=标量，故 J1b 在主机上验的是分支/边界）
                              └──J1c(249 形状 × 2 variant, 仿真 NEON)── C 向量 tile 逻辑
板端：C 标量 ──加载时自检（convpf_c.py `_selfcheck`）── C 向量（真 NEON）   ← 首次使用自动跑
```

## 4 改动 1–3 的实测（主机交错 A/B，配对差 median，41 趟，奇偶换序）

| 配置 | 配对Δ median | 分项（B 更快的趟数/41） | 结论 |
|---|---:|---|---|
| `NOPAD` | **−0.64 ms**（−2.0%） | conv_first −0.10(27) dec_3 −0.21(22) dec_2 −0.44(21) dec_1 +0.10(18) | 真实（机制=省掉 pad 的 12.87 MB 流量；主机 12.87 MB / 0.64 ms = 20 GB/s ✓ 与主机内存速率一致） |
| `FUSE` | **−0.14 ms**（噪声内） | — | **≈0，见下"我第一阶段估错了"** |
| `POOL` | **−1.13 ms**（−3.4%） | conv_first −0.25(29) dec_3 −0.23(21) dec_2 −0.51(22) dec_1 −0.14(24) | 真实（机制=8 次线程创建） |
| 三开 | **−1.26 ms** | — | 三者不严格可加（都在同一段代码的边角上） |

**板端投影（1157.2 ms 起点）**

| 改动 | 板端投影 | 依据 | 置信度 |
|---|---:|---|---|
| 1 `NOPAD` | **−25 ms** | 省 12.87 MB；板端内存速率 0.50 GB/s（与归因报告同一个标定）⇒ 25.7 ms；多出的 Python 分支 ≈ 0.1 ms | 中高 |
| 2 `FUSE` | **≈0 ms** | 见下 | 高 |
| 3 `POOL` | **−1 ~ −2 ms** | 8 次 pthread_create；主机实测 −1.13 ms，A9 只会更慢不会更快 | 中 |
| **合计** | **−26 ~ −28 ms ⇒ 1129 ~ 1131 ms** | | |

> ### ⚠ 我第一阶段把改动 2 估错了，必须更正
> 第一阶段我写"FUSE 省 14.7 MB 流量 ⇒ −25~−30 ms"。**这是错的**：
> `y = y + bb` 与 `y += bb` 的**读写流量完全一样**（都要读 y、写结果），
> 就地只省下**两次 5 MB 的分配**，而 `MALLOPT_OK` 已经把这种大块分配做成 arena 复用 ⇒ 几乎为 0。
> 主机实测 −0.14 ms（噪声内）与这个解释一致。
> ⇒ **三件便宜改动的合计是 −27 ms 左右，达不到你给的 `1157 → ≤1115 ms` 目标（差 ~13 ms）**。
> 缺口的来源就是改动 2。要想补上，下一个最便宜的靶子是 §3 表里 `im2col` 的 **276.8 ms**（但那属于大内核的活）。

## 5 改动 4：im2col-free 直接卷积内核

**形态**：`ps/pynq/convpf_c.c`（单文件，264 行，同一份源码两边各自现场编译）。
主机 `-O3 -std=c99 -fPIC -shared -ffp-contract=off -fno-fast-math`；
板端同上再加 `-mfpu=neon -mfloat-abi=hard`（`cc` 编译失败会自动退回通用参数；**ARM 上若产物没有 NEON，
加载器直接禁用内核**，因为标量版在 A9 上比现 BLAS 还慢）。

**累加语义（逐字节的物理定义，写在文件头）**
```
acc = +0.0f
for ci, ky, kx            ← K = cin*k*k，这个字典序，绝不分块、绝不重排
    v   = (越界 ? +0.0f : x[...])
    acc = round32(acc + round32(w * v))     ← 乘、加各自舍入（不许合成 FMA）
y = round32(acc + bias)
```
* 只沿 **N（输出像素）** 做 4 路 SIMD、沿 **M（cout）** 分块、沿**输出行**切线程 ⇒ K 序一字未变。
* 越界项**不跳过**，取 `+0.0f` 照旧参与乘加（保留 `w*±0` 的符号位，否则整条点积为 ±0 时会差符号）。
* 两个变体：`variant=1` 用 `vmlaq_n_f32`（一条指令，**仅当本品 VMLA 乘后不舍入**才逐字节对）；
  `variant=0` 用 `vmulq_n_f32`+`vaddq_f32`（两步各自舍入，与串行参考同构，任何平台都对）。
  **加载时自检自动选**：先试 1，不过退 0，再不过整体禁用并打印原因。

**结构（决定性能的三个数）**
* `NR=8`（tile 宽 8 输出像素 = 2 个 Q 寄存器）、`MR=4`（4 个 co）⇒ 8 个累加器 + 2 个 v = 10 个 Q 寄存器
  ⇒ **3.20 MAC / (向量装载+vmla)**，A9 上 = 1.6 MAC/cycle/核 = **核峰值的 80%**
  （NR=4 只有 1.78 MAC/op = 44%，2 线程也压不到 350 ms ⇒ 这就是为什么要 NR=8）
* **边界 tile 也走向量**：行首/行尾 tile 在**栈上**拼一段 `sc[NR+k-1]` 的 padded 行（越界填 +0.0f），
  再正常 vld。**不做的话**，Wo=128 时边缘 tile 占 **16/128 = 12.5% 的像素**，回落标量 ≈ +150 ms（板端投影），直接把判据顶破。
  代价：每 (ci,ky) 填 ≤15 个 float，实测占投影的 **1.6 ms**。
* 线程只切**输出行**范围（`convpf_direct_rows`），ctypes 调用期间释放 GIL ⇒ 真并行；任何线程出错 ⇒ 整块串行重算。

**主机上的速度（只为记录，不作为判据）**：x86 走**标量**路径，4 层合计 **518 ms** vs numpy 34.7 ms（慢 15×）。
这是预期内的（主机 BLAS 有 AVX，0.9 vs 20 GMAC/s）；板端才是这个内核的战场（板端 BLAS 只有 0.61 GMAC/s）。

## 6 J4 板端投影（**模型，不是实测**；假设全部写死在 `convpf_project.py` 文件头）

模型 A1~A7：A9 NEON 128-bit 浮点 **0.5 Q-op/cycle/核**（2 cycle/op，64-bit 数据通路）⇒ 单核 2 MAC/cycle；
**667 MHz**（Zynq-7020 常规值，本仓库无实测记录 ⇒ **假设**，投影与之线性）；双核按行切；
内存 0.50 GB/s（同归因报告的板端标定）；标量装载按「全重叠 ↔ 全串行」两个包络。

| 项 | variant=1 (vmla) | variant=0 (mul+add) |
|---|---|---|
| 计算（装载全重叠 ~ 全串行） | **181.8 ~ 253.9 ms** | 325.9 ~ 398.0 ms |
| 同上 × 1/0.75（现实效率） | 242.4 ~ 338.5 ms | 434.6 ~ 530.7 ms |
| 内存流（10.47 MB 有效 + weights） | 42.2 ms（含 scratch 1.6） | 42.2 ms |
| Python / 查表 | 5 ms | 5 ms |
| **合计投影** | **229 ~ 386 ms** | **373 ~ 578 ms** |
| 判据 350 ms | **压线**（下界过，上界不过） | **FAIL** |
| 有效速率 | 1.00 ~ 1.68 GMAC/s（峰值的 37%~63%） | 0.67 ~ 1.03（25%~39%） |

**达到 350 ms 需要 ≥1.27 GMAC/s = A9 NEON 峰值的 48%。** 一台正常写对的 NEON GEMM 能到 50~70%，
所以**可达但没有余量**，而且**完全取决于两个离线判不了的事实**：

1. **板上 VMLA.F32 是否融合**（ARMv7 ARM 说否 ⇒ 能用 variant=1 ⇒ 229~386；若融合 ⇒ 只能 variant=0 ⇒ 373~578 **FAIL**）。
2. **标量权重装载能否与 NEON 算术重叠**（决定落在区间下半还是上半）。

⇒ **J4 我不能宣称 PASS。** 结论是"可行、但必须板端实测；若落在上界，需要再拿 15%（把权重装载从每次 tap 提到每行一次，
或 MR=6/NR=8 把 MAC/op 提到 3.43）"。

## 7 板端待办（板子来电后，按序，前两条合计 <5 分钟）

```bash
cd /home/xilinx/demoir
# ① J3：决定大内核能否上板（<1 分钟；必须是默认线程口径，不要加 OMP=1）
python3 ps/tools/probe_serial_k.py
# ② 内核本体的板端 A/B（256²；热态取第 2 趟）
python3 ps/tools/convpf_timing.py --mode layers --reps 9                    # 基准
BIN_CONVPF_C=1 python3 ps/tools/convpf_timing.py --mode layers --reps 9     # 内核
# ③ 整网 sha1（默认线程口径）：若 J3 PASS，带 C 的那一行应与冻结 64² 相同
python3 ps/tools/verify_lazy_net.py --hw 64
BIN_CONVPF_C=1 python3 ps/tools/verify_lazy_net.py --hw 64
# ④ 三件便宜改动（板端口径）
BIN_CONVPF_NOPAD=1 BIN_CONVPF_FUSE=1 BIN_GEMM_POOL=1 python3 ps/tools/convpf_timing.py --mode layers --reps 9
```
板端首次启用 `BIN_CONVPF_C=1` 会自动：编译 .so（~2 s）→ 跑 4 个形状的逐字节自检 → 打印选了哪个 variant。
**若自检不过它会自己禁用并打印一行原因**，不会错数据。

## 8 风险与未覆盖

1. **J4 只有模型**：两个关键未知量（VMLA 语义、装载重叠）离线判不了；投影区间 229~386 ms 横跨判据。
2. **NEON 路径没在真 ARM 上跑过**（无交叉编译器、无 qemu、板子断电）。已用可移植 shim 把 tile 的
   **索引/边界/尾块/两个 variant** 全部在主机逐字节验过（249 形状），板端还有一次加载时自检兜底。
3. **J3 未跑**：主机上它报 FAIL（主机 BLAS 不串行），这既证明探针有分辨力，也说明
   **"内核逐字节等于现存板端 sha1"这件事还没有任何直接证据**。
4. 三件便宜改动合计 −27 ms，**未达 −42 ms 目标**；根因是我第一阶段对改动 2 的误估（已更正）。
5. 未做：`row_tile` 路线（板端已证更慢）、`BIN_GEMM_THREADS=1` 的影响、`FAST_NONCONV=False` 路径、
   `BIN_CONVPF_THREADS=1` 下的板端对比。
6. **测量方法学教训（值得进文档）**：第一版 A/B 让 B 永远紧跟 A 跑，B 白拿缓存预热，
   于是**每个开关都显得"有用 ~1.5 ms"**（连不碰那两层的 FUSE 也让 dec_2 "快 0.84 ms"）。
   改成奇偶趟换序后这些假收益全部消失。**任何"A/B 相邻跑"的测量都必须换序。**

## 9 复算入口

```
ps/tools/convpf_ab.py          # A + J1 + J1b（默认线程/单核都行，纯数值）
ps/tools/convpf_neon_sim.py    # J1c（主机上验 NEON tile，249 形状）
ps/tools/convpf_timing.py      # 交错 A/B 计时（配对差 + 奇偶换序）
ps/tools/convpf_project.py     # J4 投影（假设在文件头）
ps/tools/probe_serial_k.py     # J3（板端；主机跑应 FAIL）
runs/t17_convfp/probe_serial_k.py    # J3 的副本（便于直接拷到板子）
runs/t17_convfp/T17_convfp.patch     # 246 行补丁，可干净 apply 到 HEAD=7e57b42
```
计时统一前缀：`export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1; taskset -c 4 ...`
门禁统一前缀：`env -u OMP_NUM_THREADS -u OPENBLAS_NUM_THREADS -u MKL_NUM_THREADS ...`
