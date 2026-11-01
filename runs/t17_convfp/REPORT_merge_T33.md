# T-33 合并报告：把 T-17 的 convfp 内核补丁合进 main 新基线

日期 2026-11-02 ｜ 任务 T-33 ｜ 全程**离线**（板子断电：未 ssh / 未上板 / 未 Vivado / 未 `pkill`）
基线 `git HEAD = 3f510eb`（`baseline-v3` tag 在 `c0e9afe`，其上 +2 docs +1 fix 提交）；
合并期间别人又落了 `5059dfd`（只改 `docs/PLAN_1second.md`，没碰 `pynq_specialized_code/` ⇒ 判据不受影响），
本提交的父提交就是它
合并源：`runs/t17_convfp/T17_convfp.patch`（246 行）+ T-17 副本 `worktrees/T-17-convfp/` 的 8 个新文件

> **结论**：补丁 **0 reject**、与 T-17 副本逐字节对得上；**M1–M4 全绿**。
> 合并期间发现 `BIN_CONVPF_C=1` 在 x86 上会让整网 sha1 变（内核是**串行累加序**，主机 BLAS 不是），
> 按上级决策追加了一条**平台闸门**：非 ARM 默认禁用该内核（`BIN_CONVPF_C_FORCE=1` 逃生）。
> 加闸门后 **C 臂也 rc=0 且等于冻结 sha1**。

## 0 环境与纪律

* 上板门禁：`192.168.7.2` 断电 ⇒ 只跑主机判据；`probe_serial_k.py` **只在主机跑**（见 §4.3）。
* BLAS 线程口径（第 72 条纪律）：**全部判据用默认线程口径**
  （`env -u OMP_NUM_THREADS -u OPENBLAS_NUM_THREADS -u MKL_NUM_THREADS`）。
  未设 `OMP_NUM_THREADS=1`（那组 `24222554c880/…` 与冻结常量不同口径，会自判 FAIL 假警报）。
* 同一时刻只跑一个重活；全程单进程串行跑判据。

## 1 交付保全（先记 md5，再动手）

| 文件 | T-17 声称 | 拷贝前 | 合并后（含 §2.5 改动） | 判 |
|---|---|---|---|---|
| `ps/pynq/convpf_c.c` | `10b31d43578355887ae2cbbcc45d3704` | 同 | **`10b31d43…`（未动）** | ✓ |
| `ps/pynq/convpf_c.py` | `e30f6ff7213449a4d19ddba5f8288635` | 同 | `e251b047…`（**§2.5 授权改动**） | 变更已授权 |
| `ps/pynq/convpf_neon_shim.h` | `00382c57fed47d0645f4a8bc41f1132d` | 同 | **`00382c57…`（未动）** | ✓ |
| `ps/tools/convpf_ab.py` | `805fadc2ae67a23cf1a24cc220427bb1` | 同 | `d6ead48b…`（**§2.5 加 FORCE**） | 变更已授权 |
| `ps/tools/convpf_neon_sim.py` | `f93e1685a08b0f54b307f8be5403a983` | 同 | **`f93e1685…`（未动）** | ✓ |
| `ps/tools/convpf_timing.py` | `c5f100158f4306f1eb7899f0584ee487` | 同 | `4b135578…`（**§2.5 加 FORCE**） | 变更已授权 |
| `ps/tools/convpf_project.py` | `131b7dd7568ea80991b970510694e966` | 同 | **`131b7dd7…`（未动）** | ✓ |
| `ps/tools/probe_serial_k.py` | `e804ad08a39eda9f1b0d0b2def83752a` | 同 | **`e804ad08…`（未动）** | ✓ |

> brief 第 1 条写的 `verify/probe_serial_k.py` 在仓库里实际是 `ps/tools/probe_serial_k.py`
> （另有 `runs/t17_convfp/probe_serial_k.py` 副本，md5 同为 `e804ad08`）—— 没有 `verify/` 目录。

**故意不拷**：`ps/pynq/convpf_c_x86_64.so`、`ps/pynq/convpf_c_neonsim.so`（构建产物，见 §6）。

## 2 补丁：0 reject，且与 T-17 副本逐字节对得上

1. **净 HEAD 副本**：`git archive HEAD | tar -x -C /tmp/T33_clean`（220 MB，与工作树无关联），
   `patch -p1 --dry-run` ⇒ **rc=0，2 个文件，0 reject**。
2. **打到 main**：`patch -p1 < runs/t17_convfp/T17_convfp.patch` ⇒ **rc=0**，无 `.rej` / `.orig`，
   结果与 `/tmp/T33_clean` 的 `diff -q` **完全一致**。
3. 打完的 md5 对照：

| 文件 | main 打完 | T-17 副本 | 说明 |
|---|---|---|---|
| `bin_ref_numpy.py` | `103b867e2e158c378421acd06b0a2698` | `103b867e…` | **逐字节相同** ✓（§2.5 后又改了打印前缀 + docstring ⇒ `3711d4fa`） |
| `bin_net.py` | `033d6a00f71b071d1970f170a1274951` | `a85927f3…` | 差 **71 行 `<` + 1 行 `>`**，与 T-17 报告 §0 的预测完全吻合 |

那 1 行 `>` 就是 HEAD 比 T-17 副本多出来的旧写法
`mmio_skipped=getattr(self.engine, "_writes_skipped", 0))` ⇒ 补丁**正确保留**了 `c0e9afe` 加的
§②/§④ 两块，没有回退。**没有手工改任何 hunk**。

## 2.5 追加改动（上级的合并条件，不是新需求）：非 ARM 平台默认禁用 C 内核

**动机**：`convpf_c` 是 **NEON** 内核。非 ARM 平台上（a）没有 NEON 可压 ⇒ 不该更快（主机实测慢 15×），
（b）其**严格串行**累加序与 OpenBLAS 的 K 方向分块/多累加器**必然**差末位 ⇒ 整网 sha1 必变。
让它默认跑起来，唯一后果是给后来人留一个**必然 FAIL 的假警报**（同类教训：第 72 条纪律的 `OMP=1` 假警报）。

| 文件 | 改动 |
|---|---|
| `ps/pynq/convpf_c.py` | `_init()` 开头加**平台闸门**：`ARCH not in ("armv7l","armv8l","aarch64","arm64")` 且未设 `BIN_CONVPF_C_FORCE=1` ⇒ `ok=False`，`why="[CONVPF] 非 ARM 平台（…）⇒ 内核不启用（可用 BIN_CONVPF_C_FORCE=1 强制，仅供门禁测试）"`；新增 `_on()` / `_forced()` + `_ARM_ARCHS`；**头注释**写清"主机上 C 臂 sha1 ≠ 冻结常量是**设计使然**，判据对象是 内核 vs 串行参考 0 差" |
| `ps/pynq/bin_ref_numpy.py` | `_cpf_note` 不再给自带 `[TAG]` 的 why 叠 `[CONVPF_C]` 前缀（避免 `[CONVPF_C] [CONVPF] …`）；`conv2d_f32` docstring 的开关表补上"非 ARM 默认不启用 + FORCE" |
| `ps/tools/convpf_ab.py` | 顶部 `os.environ.setdefault("BIN_CONVPF_C_FORCE","1")`（J1 的判据对象就是该内核，必须强制） |
| `ps/tools/convpf_timing.py` | 同上（保持 T-17 报告里"主机 C 内核 518 ms"可复算，而不是静默变成 numpy 计时） |
| `.gitignore` | 见 §6：把 `ps/tools/_t17_*.json`、`runs_t17_*.json` 纳入忽略 |

**未动**：`convpf_c.c`（内核源码）、`convpf_neon_shim.h`、`probe_serial_k.py`、`convpf_neon_sim.py`、
`convpf_project.py` 保持 T-17 的 md5（`convpf_neon_sim.py` 自己编 `.so` 并直接 ctypes 调用，不经闸门）。
`probe_serial_k.py` 的 C 段本来就"不可用就跳过、不参与判据"，板端（ARM）闸门不触发，所以**不必改**；
主机上想看它那一段的交叉验证：`BIN_CONVPF_C_FORCE=1 python3 ps/tools/probe_serial_k.py`。

**闸门实测**（两个独立进程）：

```
BIN_CONVPF_C=1                     → ok=False  variant=0  so=None
                                     why='[CONVPF] 非 ARM 平台（x86_64）⇒ 内核不启用（可用 BIN_CONVPF_C_FORCE=1 强制，仅供门禁测试）'
BIN_CONVPF_C=1 BIN_CONVPF_C_FORCE=1 → ok=True  variant=1  so=…/convpf_c_x86_64.so
```
`conv2d_f32` 首次走 C 分支时打印**一行**（只一次，不在 227 层里刷屏）：
```
  [CONVPF] 非 ARM 平台（x86_64）⇒ 内核不启用（可用 BIN_CONVPF_C_FORCE=1 强制，仅供门禁测试） ⇒ 回落 numpy 路径（结果不变）
```
ARM 路径未被误伤：把 `ARCH` 伪造成 `armv7l` 后闸门**放行**，随后照旧落在既有的
"产物没有 NEON ⇒ 不启用（A9 上标量版比现 BLAS 慢）"硬闸门上（x86 编出来的产物本就没有 NEON 段），
说明"真 ARM + 真 NEON 产物"才会启用。

## 3 判据（全部 rc=0）

前缀：`env -u OMP_NUM_THREADS -u OPENBLAS_NUM_THREADS -u MKL_NUM_THREADS`（默认 BLAS 线程口径）。
日志：`runs/t17_convfp/logs_merge_T33/`（`M1_*,M2_*` = 加闸门**前**；`R_*` = 加闸门**后**的最终批）。

| 判据 | 命令 | rc | A / B / C sha1（冻结 `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd`） | 判 |
|---|---|---:|---|---|
| **M1 默认路径不变** | `verify_lazy_net.py --hw 64` | **0** | `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd`（三臂全等且 = 冻结） | **PASS** |
| **M2-a** `BIN_CONVPF_NOPAD=1` | 同上 | **0** | = 冻结 | **PASS** |
| **M2-b** `BIN_CONVPF_FUSE=1` | 同上 | **0** | = 冻结 | **PASS** |
| **M2-c** `BIN_GEMM_POOL=1` | 同上 | **0** | = 冻结 | **PASS** |
| **M2-d** `BIN_CONVPF_C=1` | 同上 | **0** | = 冻结（内核被平台闸门禁用，只打印一行原因） | **PASS** |
| **M2-e** 三种便宜开关全开 | NOPAD+FUSE+POOL | **0** | = 冻结 | **PASS** |
| **M4 开关全关 == baseline** | 四开关显式 `=0` | **0** | = 冻结 | **PASS** |
| **M3 逐字节** | `convpf_ab.py`（A + J1 + J1b） | **0** | A PASS（7 组合全 0 差）/ J1 PASS（4 层 vs 串行参考 0 差）/ J1b PASS（256² 全画幅 4 层） | **PASS** |
| **M3 续** | `convpf_neon_sim.py`（J1c） | **0** | 4 真实层 + **249 形状 × 2 variant** 逐字节 | **PASS** |

## 4 M2-d 曾经改 sha1 的根因（已定性；闸门就是为它加的）

### 4.1 决定性证据：**现存 numpy 路径自己就不是"串行累加序"**

同一层（`cout=12, k=3, pad=1, cin=64, 9×10`，真实量级输入），单进程内三方对拍：

```
A) numpy BLAS vs 串行参考 : equal=False  n_diff=997/1080   maxabs=3.576e-06
B) C 内核   vs 串行参考   : equal=True   n_diff=0
C) C 内核   vs numpy BLAS : equal=False  n_diff=997/1080   maxabs=3.576e-06
```

⇒ C 内核与"K 严格串行"逐字节相同（这就是 J1 的定义）；生产路径的 `np.dot`（OpenBLAS，
K 方向分块/多累加器）**本身**就与串行序差末位。故**任何串行序内核在主机上都不可能复现冻结 sha1**。
`convpf_ab.py` §C（不计判据）同结论：`n_diff=644602/786432`，`maxabs=1.3e-05`。

### 4.2 这不是回归，T-17 原本就把它挂在板端前提上

T-17 报告 §7③：*"整网 sha1（默认线程口径）：**若 J3 PASS**，带 C 的那一行应与冻结 64² 相同"*；
§8 风险 3：*"J3 未跑 ⇒ '内核逐字节等于现存板端 sha1' 这件事还没有任何直接证据"*。
⇒ 闸门只是把"主机上必然假警报"的那条路关掉，板端（ARM）行为一字未变。

### 4.3 旁证（只在主机跑，没有上板）

`probe_serial_k.py` 在主机 = **rc=1 / FAIL**（`K=300 M=48` 等 6 组全 FAIL，主机 OpenBLAS 的 K 非串行）。
它自己的结论行写明上限：*"对现存板端 sha1 而言逐字节不可达。按 T-17 约定：停下来上报，不放宽容差。"*
—— 这条 FAIL 同时证明探针**有分辨力**，板子来电后跑它才有意义。

## 5 提交

显式 `git add`（**不用 `-A`**）：

```
.gitignore
pynq_specialized_code/ps/pynq/{bin_ref_numpy.py,bin_net.py,convpf_c.c,convpf_c.py,convpf_neon_shim.h}
pynq_specialized_code/ps/tools/{convpf_ab,convpf_neon_sim,convpf_timing,convpf_project,probe_serial_k}.py
runs/t17_convfp/{T17_convfp.patch,REPORT_convfp_phase2.md,REPORT_merge_T33.md}
runs/t17_convfp/logs_merge_T33/*.log
```

commit message：`feat(ps): 合并 T-17 convfp 内核（默认关；非 ARM 平台不启用）`（**不打 tag**）。

已提交（父提交 `5059dfd`），本报告与判据日志一并入库。

## 6 交付卫生检查

**(a) 本不该进 git 的东西**

| 类别 | 文件 | 处置 |
|---|---|---|
| 构建产物 `.so` | `ps/pynq/convpf_c_x86_64.so`、`convpf_c_neonsim.so` | `.gitignore:145` 的 `…/ps/pynq/*.so` 已覆盖（`git check-ignore -v` 实测命中）；本任务**没拷**它们，main 里的 `.so` 是本地重编译产物 |
| `.o` / `.rej` / `.orig` | 无 | 无风险 |
| **临时 json（真问题）** | `_t17_gate.json`（`convpf_ab.py` 每次跑都写）、`_t17_projection.json`、`runs_t17_timing_*.json` | main 的 `ps/tools/` 里实际只有 `_t17_gate.json` ⇒ **已移走**到 `runs/t17_convfp/`；其余 5 个只存在于 `worktrees/T-17-convfp/ps/tools/`（红线：worktree 只读，未动）。为断根，`.gitignore` 新增 `pynq_specialized_code/ps/tools/_t17_*.json` 与 `runs_t17_*.json` 两条规则 |

**(b) 有没有覆盖 `ps/tools/` 里已有的脚本**：**没有**。
`git ls-tree HEAD pynq_specialized_code/ps/tools/` 的 71 个文件名 vs T-17 的 5 个新脚本，
`convpf_ab.py / convpf_neon_sim.py / convpf_timing.py / convpf_project.py / probe_serial_k.py`
全是新名字，**零重名**（`3f510eb` 纳入 git 的 22 个判据脚本与它们不相交）。

**(c) 已修**：`runs/t17_convfp/` 此前在 HEAD 里**未跟踪**（T-17 的 patch/报告都不在 git 里），本提交纳入。

## 7 复算入口

```bash
cd /media/felix/Data/coding/demoir-fpga
U="env -u OMP_NUM_THREADS -u OPENBLAS_NUM_THREADS -u MKL_NUM_THREADS"
$U python3 pynq_specialized_code/ps/tools/verify_lazy_net.py --hw 64                     # M1 / M4
$U BIN_CONVPF_NOPAD=1 python3 pynq_specialized_code/ps/tools/verify_lazy_net.py --hw 64   # M2-a
$U BIN_CONVPF_C=1     python3 pynq_specialized_code/ps/tools/verify_lazy_net.py --hw 64   # M2-d（闸门 ⇒ rc=0）
$U BIN_CONVPF_C=1 BIN_CONVPF_C_FORCE=1 python3 pynq_specialized_code/ps/tools/verify_lazy_net.py --hw 64
                                                                                          # 强制内核 ⇒ sha1 变（设计使然）
$U python3 pynq_specialized_code/ps/tools/convpf_ab.py                                    # M3（脚本自带 FORCE）
$U python3 pynq_specialized_code/ps/tools/convpf_neon_sim.py                              # M3-J1c
$U python3 pynq_specialized_code/ps/tools/probe_serial_k.py                               # 主机预期 FAIL（板端才是判据）
```
