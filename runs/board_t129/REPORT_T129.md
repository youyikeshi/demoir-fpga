# T-129：**3 个 op 的真实流水排程**（`prepare(N+1)` 与 `op N` 的 PL 重叠）

## 一句话

**流水重叠在 3 op 规模上不能复现 128² 的错**：真重叠确实发生了（`prepare(N+1)` 完整落在
`op N` 的 PL 飞行窗口里，覆盖 30.6~34.2 ms / op 全程 5.6~32 ms），**但 prepare 的 DDR 写区间
与在飞 op 的读/写区间交 0 字节**（`nbuf=2` 槽不同；24 槽 lazy 也不复用），
**300 次 op 判定逐字节全对、每 op 单一 sha1**。⇒ 父代理"prepare(N+1) 覆盖 op N 缓冲"
这条假设**被否**。重叠的唯一可测效应是**性能**：被重叠的 op 的 PL 时间 **×5.6**。

---

## 0. 结论表（同一组 op、同一输入、同一参照，只改排程）

| 臂 | 排程 | 2 跑 | 错字节 | 确定 |
|---|---|---|---|---|
| **S** | 严格串行（`net.run(pipeline=False)`） | ✓ / ✓ | **0 / 0** | ✓ |
| **P1** | 提前 prepare（在线性链上等价 P2） | ✓ / ✓ | **0 / 0** | ✓ |
| **P2** | **整网真实流水**（`net.run(pipeline=True)`，一行不改） | ✓ / ✓ | **0 / 0** | ✓ |
| P2nc | P2 + `config/base` 的 MMIO 写推到 kick 前 | ✓ / ✓ | 0 / 0 | ✓ |
| P2ncan | P2 + OUT 槽尾金丝雀写推到 kick 前 | ✓ / ✓ | 0 / 0 | ✓ |
| P2lazy | P2 + `LAZY_DEQ=1/24 槽`（唯一会真正槽复用的配置） | ✓ / ✓ | 0 / 0 | ✓ |
| **H** | 最高压力：3 个**独立** op，A 的 PL 里连续 prepare B+C，**100 次** | — | **0 / 300 op** | ✓（每 op 1 个 sha1） |

**判据**：每 op 的**物理 C8 裸字节**在 `reap()`（= 真 done）**之后**直接从 OUT 槽 invalidate 读回，
与 numpy 参照逐字节比（= T-124 dump 的同一语义）。

## 1. 真重叠的实测（这才是本任务要的证据）

`op N` 全程 vs `prepare(N+1)` 落在窗口内的时长：

| 臂 | A 的 PL | B 的 PL | C 的 PL | prepare(B) 落在 A 窗口内 | prepare(C) 落在 B 窗口内 |
|---|---|---|---|---|---|
| S | 5.632 ms | 5.637 ms | 5.645 ms | 0（串行） | 0（串行） |
| **P2** | **32.112 ms** | **31.691 ms** | 5.686 ms | **31.57 ms（98%）** | **31.08 ms（98%）** |
| P2lazy | 18.084 ms | 20.822 ms | 12.127 ms | 13.82 ms | 13.87 ms |
| **H（100 次）** | 31.6~33.8 ms | 31.5~34.6 ms | 5.66~5.72 ms | 30.64~34.21 ms | 同量级 |

⇒ **重叠是真的、而且几乎覆盖前一 op 的全过程**（不是"擦边"）。

## 2. 覆盖判据：0 字节（三种都查了）

每个 `N→N+1` 对都算了四个交：
・prepare(N+1) 的 `push_input` 写区间 `[in_pa, in_pa+in_len)` ∩ op N 的**读**区间（IN 槽）
・同上 ∩ op N 的**写**区间（OUT 槽）
・prepare(N+1) 的金丝雀写区间 `[out_pa+out_len, +256)` ∩ 上述两条

| 臂 | 写IN∩读IN | 写IN∩写OUT | 金丝雀∩读IN | 金丝雀∩写OUT | 同槽 |
|---|---|---|---|---|---|
| S / P2 / P2nc / P2ncan | **0 B** | **0 B** | 0 B | 0 B | **False** |
| P2lazy（24 槽） | **0 B** | **0 B** | 0 B | 0 B | **False**（A/B/C 用槽 0/1/2） |

⇒ 父代理的"目标缓冲与在飞缓冲有交集 ⇒ 稳定覆盖"**在 `nbuf≥2` 下物理上不可能发生**：
`nbuf=1+ 提前准备` 那条路早在 `Net.run()` 里被硬拦（`nbuf<2 ⇒ 强制 2`，注释写的就是这个理由）。

## 3. 唯一可测效应：**重叠让 PL 慢 5.6 倍**（数据不坏）

S 里每个 op 都是 ~317 k 拍 / 5.6 ms；P2 里被重叠的那两个 op 变成 **1.59 M / 1.57 M 拍（32.1 / 31.7 ms）**，
没被重叠的 C 仍是 320 k / 5.69 ms。⇒ 差异只来自 PS 侧 `push_input`（64 KB 写 + cache 维护）与
PL 访存抢同一 AXI/DDR。

**这条能反过来给整网定性**：T-124 实测整网 op15 = **395 036 拍**，而 T-129 隔离测量
"不重叠 = 318 k 拍 / 重叠 = 1.59 M 拍" ⇒ **整网 128² 的 op 实际跑在"不重叠"的量级上（×1.24），
不是"重叠"的量级（×5.0）** ⇒ 整网其实几乎不重叠（线性 DAG 里 `op N+1` 的输入就是 `op N` 的输出，
结构上不可能提前 prepare）。这也解释了为什么整网里从未观测到"重叠导致"的错。

## 4. 顺带否掉的两条旧怀疑

| 臂 | 结果 |
|---|---|
| **F 写回落地时延** | `e.run()` 返回（= done）后，OUT 槽在 **done+0 / +20 / +121 / +422 ms** 四次回读**同一 sha1**（= 参照 `f965e7f452ad`），**nbad 全 0** ⇒ "done 了但尾巴还没落地"在 64² 上**不存在**（T-125 的 30/227 不是数据面问题） |
| **D done 位语义** | kick 之后 AP_RET 在 **+0.00/+0.05/+0.20/+1.00/+2.00/+4.00 ms 全部 done=0 busy=1**，首个 done 在 5.46 ms、真完成 5.66 ms（冷/热两次一致）⇒ **done 由 start 清干净，`_PlBackend.poll()` 不会被上一层的 sticky done 骗** ⇒ 真流水循环能（也确实）重叠，不是"假流水" |

## 5. 铁律与资产

| 项 | 值 |
|---|---|
| 位流 | v53 `e19de0326fe657fc8de9ddd241ecc729`（跑前 = 跑后 ✓），`.hwh` `5235764602921d` |
| 频率 | 目标 50 → **读回 50.0 MHz**；E1E2 R_RUN_UNIT=4 probe 通过（clk=50） |
| 指纹 | `conv_uni_0`=0x40040000 / `bin_unit_0`=0x40060000 / **magic(0x314)=0x50300001** / load 后 0x310=0 |
| 运行点 | `BIN_PACK=e1e2 BIN_LAZY_DEQ=0 BIN_LAZY_SLOTS=24 BIN_CAT_FUSION=1 BIN_BILINEAR_C=1 BIN_PRECOMP=1 Q8CACHE=1 BIN_F32DIRECT=1 BIN_CAT_ECA_FUSE=1 BIN_EPI_NOINV=1 BIN_PROFILE=1`；无任何 `BIN_V50_*`；vlanes=8 |
| PS 门禁 | 板上 `md5sum -c` **158/158 ✓**；`deps_closure --check` rc=0 ✓；`[DEPS]` 六项全 fast ✓ |
| arena | 8 MB @0x30300000（3-op 只需 4.69 MB） |
| 纪律 | `ulimit -c 0`；只按 pid `kill -TERM`（无 `pkill -f`）；收尾只 `sync; sync`；**未 poweroff**（跑后 uptime 3:06、CmaFree 103 MB） |

### 5.1 四次 load 的如实披露（探针自己的两个 bug，与硬件无关）

| load | 结局 | 原因 |
|---|---|---|
| #1 `t129_071011` | 崩在 load 之后、**没跑任何 PL** | 我的 `R` 字典漏了 `"steps"` 键 ⇒ `KeyError`（`step1_load` 末尾） |
| #2 `t129_071228` | 6 臂全跑完，**但每 op 的裸字节是跑到一半的** | 我在 `finish()` 里、`super().finish()`（内含 `reap` 阻塞等 done）**之前**读 OUT 槽 ⇒ 读到首 ~5 KB 新数据 + 其余旧残留（"89% 错"是**探针 bug**） |
| #3 `t129_071839`（`probe_t129b.py`） | **全对**，真重叠首次成立 | 改成 reap 之后读；同时把图换成**独立分支**（线性链永远不可能重叠） |
| #4 `t129_072238`（`probe_t129c.py`） | **全对**，100 次压力 0 错 | 3 个全独立 op + 压力循环 |

上板前的主机自测（防第 5 次同类事故）：`selftest_t129.py`（numpy 公式 44 项）、
`dryrun_t129_host.py`（真 `bin_net` + `_RefBackend` 干跑，参照逐字节对齐）、
`smoke_t129_host.py`（假 `pynq` 把真探针模块 import 起来跑 `pick_layers`/`build_refs`）。

## 6. 几何（= 整网 `op12/op13/op16` 的真实形状）

| | 层 | 几何 | fs | 输入 |
|---|---|---|---|---|
| A | `…b0.ghost_layers.0.primary_conv` | 48→32 k1 p0 64×64 | 4→5 | PS 的 tt（48ch） |
| B | `…b0.ghost_layers.1.primary_conv` | 112→32 k1 p0 64×64 | 4→4 | PS 的 inda（112ch） |
| C | `…b0.ghost_layers.0.cheap_operation` | 32→32 k3 p1 64×64 | 5→4 | **A 的输出**（d=0） |

第 3 轮把 C 换成"同层 + 独立输入"以便三个 op 全独立（压力最大）；B/C 与 A 无依赖 ⇒
真流水循环在 A 的 PL 里连续 prepare B 与 C。

## 7. 产物（`/media/felix/Data/coding/demoir-fpga/runs/board_t129/`）

| 文件 | 说明 |
|---|---|
| `probe_t129.py` / `probe_t129b.py` / `probe_t129c.py` | 三轮探针（逐轮记录改进，不覆盖旧文件） |
| `selftest_t129.py` / `dryrun_t129_host.py` / `smoke_t129_host.py` | 上板前的主机自检（44 + 3 + 62 项） |
| `run_t129.sh` / `run_t129b.sh` / `run_t129c.sh` | 一次租约 = 一次 load 的完整脚本（体检→部署→门禁→load→跑→拉回） |
| `t129_071011.{log,json}` | load #1（探针 `KeyError`） |
| `t129_071228.{log,json}` | load #2（采集早了一拍的无效数据，留证） |
| `t129_071839.{log,json}` | load #3：真重叠首次成立，全对 |
| `t129_072238.{log,json}` | load #4（本轮结论）：S/P1/P2/P2nc/P2ncan/P2lazy + D + F + H(100×) |
| `t129c_run.log` | 板端完整 stdout |
| `b_ref{1,2,3}_c8.bin` | 三份参照（物理 C8 裸字节，128 131072 B） |
| `t129c_bit_after.md5` | 跑后位流 md5 = `e19de032…`（不变） |
| `*_got.bin` | 只有出错时才落盘（第 3/4 轮 **0 个** = 没有错） |
| `load2_invalid_capture/` | load #2 的 30 个产物**已隔离**（采集早了一拍的无效数据，`README.txt` 说明原因），不要当证据 |
