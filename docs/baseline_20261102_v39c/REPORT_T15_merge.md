# T-15 合并报告：把已验证的 T-14 增量合进 main（干净新基线）

- 日期：2026-11-02 01:27–01:45（Asia/Shanghai）
- 执行：子代理 T-15（`session-6399c6d7`）；主代理 `session-6399c6d7-d929-49c9-8e8e-2d687bffd6ed`
- 仓库根 `R = /media/felix/Data/coding/demoir-fpga`，`git HEAD = 70dcfa1`（分支 `restructure1`，无 tag）
- **全程离线**：板子 `192.168.7.2` 断电，**零 ssh、零上板、零 Vivado、零 pkill**
- **未 commit、未打 tag**：main 目前是"待提交"状态（3 个 tracked 文件 `M` + 1 个新文件 `??`）

---

## 0. 方向性结论（15 行以内）

1. **增量贴成功**。纯 T-14 增量 = **4 文件**（`bin_engine +353 / bin_net +65−2 / board_v39_verify +108 / deps_closure.py 新建 481`），
   与计划登记表 T-15 行 ② 的预期**完全一致**（md5 `1967aa8a/2e077940/903aa8bf/b6b85a4d` 对上）。
2. **冲突 5 处**：`bin_engine` 0、`deps_closure.py` 0（新文件）、**`bin_net` 1 处**、**`board_v39_verify` 4 处**。
   全部按"只保留 T-14 加了什么、把 parked 行剔掉"手工解决（§4 逐处列出）。
3. **C1 停放工作已清除：通过**。5 个 token 在 `ps/pynq/*.py` + `ps/tools/*.py` 上**全为 0**。
4. **C2 T-14 增量在位：通过**。`deps_closure.py` 存在且 `-rwxrwxrwx`；`kernel_deps|BIN_REQUIRE_KERNELS|deps_closure_ok`
   在三个文件上 = **24 / 9 / 24**（均 >0）；`board_v39_verify.py` 有 `[DEPS]` 打印行（L146/148/201）。
5. **C3 依赖闭包两条判据：全 rc=0**。`--gen` rc=0（11 模块/15 文件）；`md5sum -c` rc=0（15/15 成功，**基准目录 = `ps/pynq/`**）；
   `--check` rc=0。原件已另存 `deps_closure_T14_verified.txt`（md5 `56f94491…`）。
6. **C4 合并数值中性：通过**（在冻结跑点上）。纯 HEAD 副本与合并后的 main **各自 A/B/C 三个 sha1 逐位相同且 = 冻结值**
   `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd`，两侧 `verify_lazy_net --hw 64` 均 **PASS / rc=0**。
7. **★ C4 附带发现（需主代理裁决，不影响"合并不改数值"）**：该 sha1 **不是 BLAS 线程无关的**。
   本任务书要求 `OMP_NUM_THREADS=1`，在该设置下两侧稳定得到
   `24222554c880 / 2fd745d93159 / 8ab595fcf7cd`（同样 A=B=C，但 ≠ 冻结常量）⇒ 脚本自判 **FAIL/rc=1**。
   根因是**环境口径**而非代码（同一棵树默认线程跑 3/3 稳定复现冻结值）；详见 §5.3。
8. **C5 工作树干净：通过**。`git diff --stat -- ps` = 3 个文件；`ps/` 下无其它被改的 tracked 文件；
   22 个 untracked（`.bak*`/`*.so`/`__pycache__`/工具脚本）**一个没删**。
9. **C6 归属可证**：`bin_engine` 320 行、`bin_net` 148 行"worktree 有 / merged 无"的差**全部**是 parked patch 的 `+` 行；
   `board_v39` 204/212 是 parked `+` 行 + 8 行"我改写前的原文"。反向：merged 里"worktree 没有"的行，
   `bin_engine`/`bin_net` **100% ⊆ (HEAD ∪ T-14增量)**，`board_v39` 只有 **10 行**是手工适配（已逐行列出）。
10. 交接物：新 `runs/board_ops/20261102/deps_closure.txt`（md5 `69a7c432…`）+ 本报告 + `t15_merge/` 全套证据。
11. **未做（属主代理职责）**：commit / tag / `docs/baseline_20261102_v39c/` 归档 / 更新 `docs/PLAN_TASKS.md` 的 T-15 行。

---

## 1. 合并前取证（与任务书 §3.1 对应）

### 1.1 main 的脏 `ps/` 到底是什么

```
$ git -C R diff --stat -- pynq_specialized_code/ps      # 合并前
 bilinear_c.py       |  23 +-      bin_ref_numpy.py     |  11 +-
 bin_engine.py       | 391 ++--    board_v39_verify.py  | 275 ++--
 bin_net.py          | 189 ++--    qcache.py            | 181 ++--
 tools/export_network.py | 108 ++--
 7 files changed, 1031 insertions(+), 147 deletions(-)
$ git diff -- ps > /tmp/probe_T15/T15_cur.patch ; md5sum = a3adcbdd9d53be2148f9c92435549736
```

**与 `runs/parked_20261102/parked_ps_work.patch`（md5 `5c174cfe2bc9f327c6340de8520c9eca`）不一致** ⇒ 按任务书另存：

```
runs/parked_20261102/parked_ps_work_reconfirm_20261102_012940.patch   md5 a3adcbdd9d53be2148f9c92435549736
```

**不一致来自哪里（已定位，不是新工作）**：parked patch 是 00:07 的快照，只含 6 个文件（+947/−123）；
当前树多出 `ps/tools/export_network.py` 的 +84/−24（torch 变成可选 import + `pack_weight_container` 的
`tap_pack` 容器化）——是快照之后的**漂移**，与 T-14 无关。

**6 个 parked 文件逐字节复核（机械证据）**：把 parked patch 打到 `git archive HEAD` 出来的干净副本上，
与当时 main 的脏文件逐一对 md5，**6/6 完全一致**：

| 文件 | parked patch 打完后 | 当时 main | 判定 |
|---|---|---|---|
| `pynq/bin_engine.py` | `17fc7f654540484595af80e83bcb68e7` | 同 | ✓ |
| `pynq/bin_net.py` | `9cbc5ddf1404a08c7f1447f760639bab` | 同 | ✓ |
| `pynq/board_v39_verify.py` | `4d6a2011f1f88735f772acbf9a0c38eb` | 同 | ✓ |
| `pynq/qcache.py` | `4157ba3d7e35b8514d71eef29f0ee6a5` | 同 | ✓ |
| `pynq/bilinear_c.py` | `cd43b1d6c26908e94d8b1a2aeab169b6` | 同 | ✓ |
| `pynq/bin_ref_numpy.py` | `a19ba76c56df56f517c8640a62eb6051` | 同 | ✓ |

⇒ 结论：**main 的 `ps/` = parked（6 文件）+ export_network.py 的漂移**；两者都按 §1「必须清掉」处理，
且**都已在 reconfirm patch 里完整保存**（没有丢失任何一行）。

### 1.2 副本与基线的真实关系（关键，决定了"纯增量"能否机械成立）

```
$ git worktree list
/media/felix/Data/coding/demoir-fpga                        70dcfa1 [restructure1]   ← 本任务说的 "main"
/media/felix/Data/coding/demoir-fpga/worktrees/T-14-deps    825cbb9  (detached)
$ git log --oneline -1 825cbb9
825cbb9 sync: mirror main working tree (ps/pynq 6 files + tools/export_network) as T-14 baseline
$ git show 825cbb9:pynq_specialized_code/ps/pynq/bin_engine.py | md5sum   → 17fc7f65…  （= 脏 main 的那个）
```

⇒ T-14 副本的**基线提交 825cbb9 就是"HEAD + parked"**，与脏 main 的 `ps/` **逐字节相同**。
因此 `git -C worktrees/T-14-deps diff 825cbb9 -- pynq_specialized_code/ps` **就是纯 T-14 增量**，
不掺任何 parked 行（这一点后面 C6 会再证一次）。

## 2. 纯 T-14 增量（在清 main 之前生成）

```
$ git -C worktrees/T-14-deps diff 825cbb9 -- pynq_specialized_code/ps > /tmp/probe_T15/t14_pure.patch
 bin_engine.py       | 353 ++++++++++     bin_net.py         |  65 ++-
 board_v39_verify.py | 108 ++++-          deps_closure.py    | 481 ++++++++++
 4 files changed, 1005 insertions(+), 2 deletions(-)
```

- 落盘：`runs/board_ops/20261102/t14_pure_increment.patch`（md5 `938a1692b821a7a06e595684b016f29e`）
- `tools/board_repeat.py` **确认不属于 T-14**：T-14 副本里它是 `tools/board_repeat.py`（仓库根 `tools/`，
  `AM` 状态，与 `ps/` 无关，本任务 C1–C6 均不覆盖），`ps/tools/board_repeat.py` **不存在**。
  ⇒ 纯增量就是 **4 个文件**，无第 5 个。

## 3. 清 main + 贴增量

```
$ git checkout -- pynq_specialized_code/ps/          # 连带把 export_network.py 也还原（其改动在 reconfirm patch 里）
$ git apply --3way /tmp/probe_T15/t14_pure.patch
 bin_engine.py            → 直接应用成功（T-14 在 bin_engine 里是纯追加：1 行 import + 文件尾 353 行）
 deps_closure.py          → 新文件，应用成功
 bin_net.py               → **1 处冲突**
 board_v39_verify.py      → **4 处冲突**
```

**⚠ 过程记录（为什么不直接用 `git apply` 的产物）**：`--3way` 在 `deps_closure.py`（索引里不存在的新文件）
上"回落到直接应用"，这一轮**又把整份 patch 直接贴到了已带冲突标记的文件上**（潜在的二次贴入）。
为避免"重复 hunk / 静默丢 hunk"，我把这两个文件**丢弃重做**，改用**规范三方合并**：

```
git merge-file -p --diff3 -L HEAD -L base825cbb9 -L T14  <HEAD版>  <825cbb9版>  <T-14版>
  bin_engine        → rc=0，**零冲突**；输出 md5 eec568b2a507768f93d169b29c95c838
  bin_net           → rc=1，1 处冲突
  board_v39_verify  → rc=4，4 处冲突
```

`bin_engine` 的三方合并结果与被 `--3way` 直接应用出来的文件 **md5 完全相同**（`eec568b2…`）⇒ 两种路径互证。
`bin_net` / `board_v39_verify` 的冲突按 §4 手工解决（脚本留档：`t15_merge/resolve.py`）。

## 4. 冲突逐处解决（每处：T-14 加了什么、parked 是什么、怎么取）

### 4.1 `bin_net.py`（1 处，`_run_pipelined` 收尾；合并后 L1903–1950）

- **parked 部分**（base 有、HEAD 无）：`_dcs` 三行（`desc_cache_stats()`）+ 更早的 `_e1p`（E1E2 `_prof` 桶）读取块。
- **T-14 部分**：`_kd/_dok/_drift` 漂移检测 + `require_kernels()` fail-fast + `_perf/_perf_note` 性能门槛块。
- **取法**：
  - `_dcs` / `desc_cache_stats` / `desc_cache=_dcs` **全部丢弃**（C1 的 5 个 token 与 `desc_cache` 残留现在全 0）；
  - **保留 `_e1p` 读取块**（6 行）：它只读 **HEAD 就有的** `self.engine._e1e2._prof`（`bin_engine.py:2480`，非 parked 符号），
    而 T-14 的门槛是 `if _e1p: _perf = BE.perf_gate(...)` —— 丢掉 `_e1p` 会让 **§④ 性能门槛永久变成死代码**（`perf_gate` 恒空）。
    ⇒ 判据 C2 不含此项，但为保住 T-14 已板端验证的行为，**保留并单独声明**（见 §8 待裁决项 D1）。
  - 同步保留 `e1e2_prof=_e1p,` 一行（T-14 的 `board_v39` 行要读 `st["e1e2_prof"]` 作为门槛输入证据）。
    **这是与副本版的唯一"加回"**，来源是 parked 镜像里的一行 context，但用的是 HEAD 原生符号。

### 4.2 `board_v39_verify.py`（4 处）★ 这里有判据前提问题，必须知情

先给事实（机械可比）：

```
HEAD  439 行 ； parked 版(base 825cbb9) 644 行 ； T-14 版 750 行
HEAD 里**根本没有** runpoint / CODE_FILES / md5_of / code_md5 / R["runpoint"] / _ONETIME / step_bilinear
  ≈ parked patch 往这个文件里加了 **205 行**"运行点自记录 + 一次性成本(口径 a/b) + seed"机器
```

**而 T-14 的 board 侧增量是"写在这台 parked 机器里面"的**（8 个 hunk 里 4 个的锚点就是 `CODE_FILES` / `runpoint()` /
`sha_frozen_ok` / `step_bilinear()`）。处置：

| # | T-14 到底加了什么 | 取法 |
|---|---|---|
| 1 | `dump()` 里**再刷一次**内核状态（`R.setdefault("runpoint")`…） | 逐字保留（`R`/`dump` 是 HEAD 原生的） |
| 2 | `kernel_deps()` + `deps_summary()`（**自包含**，自带 `import bin_engine`） | 逐字保留，放在 `dump()` 之后（原锚点 `CODE_FILES` 是 parked 的） |
| 3 | `runpoint()` 里记 `kernel_deps_probe` / `deps_closure_ok` | **不移植 `runpoint()` 本身**（parked），改为在 `main()` 里直接 `R.setdefault("runpoint")[...]` 记同样两个键 |
| 4 | `step_kernels()`（三个 native 内核一次性装载，自带 docstring） | 逐字保留，作为"步骤 0b"插在"步骤 1：load"之前（= 首次 run 之前） |
| 5 | `run_net()` 的 row 增加 `deps_closure_ok / kernel_deps / perf_gate / e1e2_prof / perf_warm / perf_note / run_valid` | 保留；`n_sha_ok` 用 **HEAD 原式** `int(ok)*3,`（parked 的 `... if frozen_cmp else None)` 不引入） |
| 6 | `run_net()` 里 `run_valid=False` 的告警两行 | 逐字保留 |
| 7 | `main()` 里打 `[DEPS]` 一行 + `deps_closure_ok=False` 告警 | 保留（锚点从 parked 的 CODE_FILES 循环改为 `R["env_at_start"]` 之后） |
| 8 | `main()` 里 `step_kernels()` 调用（在 256² 之前） | 保留（原锚点 `step_bilinear()` 是 parked 的） |

**明确没带上来的（= parked，且 HEAD 从来没有）**：`CODE_FILES` / `md5_of` / `code_md5` / `runpoint()` 本体 /
`_ONETIME` 口径 a/b 记账 / `step_bilinear()` / `set_arm()` / seed 参数化。
⇒ 因此**新基线的 `board_v39_verify.py` 不会产出 `runpoint.code_md5` 那套自记录 JSON**（HEAD 本来也没有）；
它只产出 T-14 想要的 `[DEPS]` 行 + 每行 `run_valid/deps_closure_ok/perf_gate`。
**这是"干净基线"与"T-14 board 侧可复现性"的取舍，请你裁决（§8 待裁决项 D2）。**

### 4.3 结果

```
$ git -C R diff --stat -- pynq_specialized_code/ps
 pynq/bin_engine.py       | 353 ++++++++++++++++++++++
 pynq/bin_net.py          |  72 ++++-
 pynq/board_v39_verify.py | 112 ++++++-
 3 files changed, 535 insertions(+), 2 deletions(-)
（+ `pynq_specialized_code/ps/tools/deps_closure.py` 为新文件，untracked）
$ python3 -m py_compile bin_engine.py bin_net.py board_v39_verify.py deps_closure.py   → rc=0
```

## 5. 门禁 C1–C5 的原始命令与输出

### 5.1 C1（停放工作已清除）

```
$ grep -c "BIN_DESC_CACHE\|_DescMemo\|_DESC_MEMO\|BIN_DESC_VERIFY_ALL\|BIN_DESC_MEMO" \
        pynq_specialized_code/ps/pynq/*.py pynq_specialized_code/ps/tools/*.py | grep -v ":0$"
（无输出 ⇒ 每个文件都是 0）
$ grep -rn "desc_cache\|_dcs" ... | head
（无输出 ⇒ 连 parked 的私有符号也没有残留）
```

### 5.2 C2（T-14 增量在位）

```
$ for f in pynq/bin_engine.py pynq/bin_net.py pynq/board_v39_verify.py; do
    grep -c "kernel_deps\|BIN_REQUIRE_KERNELS\|deps_closure_ok" pynq_specialized_code/ps/$f; done
pynq/bin_engine.py : 24
pynq/bin_net.py : 9
pynq/board_v39_verify.py : 24
$ ls -l pynq_specialized_code/ps/tools/deps_closure.py
-rwxrwxrwx 1 felix felix 23174 … deps_closure.py
$ grep -n '\[DEPS\]' pynq_specialized_code/ps/pynq/board_v39_verify.py
146:        return "[DEPS] <读不到：%s>" % d["error"]
148:    return ("[DEPS] " + " ".join("%s=%s" % (k, d[k]["state"]) for k in order
201:        log("  ⚠ 有内核没装上 ⇒ 本跑点 deps_closure_ok 会是 False（见 [DEPS] 行）")
```

另：`bin_engine.py` 里 T-14 的 10 个 API 全部在位
（`kernel_deps/deps_closure_ok/deps_warm/deps_drift/report_deps/perf_gate/perf_alert_text/require_kernels/deps_require_error/load_kernels` 各 1 处 def）。

### 5.3 C3（依赖闭包两条判据）

```
$ cp runs/board_ops/20261102/deps_closure.txt  runs/board_ops/20261102/deps_closure_T14_verified.txt
  （原件 md5 56f944918c7a854233caca9e1ed90e72 —— 它是在"含 parked 的 bin_engine"上生成的）
$ python3 pynq_specialized_code/ps/tools/deps_closure.py --gen
✓ 已写 …/deps_closure.txt（11 模块 / 15 文件）            rc=0
$ cd pynq_specialized_code/ps/pynq && md5sum -c …/deps_closure.txt
15 行全部「成功」                                          rc=0
$ python3 pynq_specialized_code/ps/tools/deps_closure.py --check
✓ deps_closure --check PASS：11 模块 / 15 文件            rc=0
```

**基准目录已核实**（不猜）：清单条目是**裸文件名**（`bin_net.py` 等），工具 docstring 写明
"`cd <目录> && md5sum -c deps_closure.txt`"，源根 = `pynq_specialized_code/ps/pynq` ⇒ `md5sum -c` 必须在 `ps/pynq/` 里跑。

新旧清单只差两行 md5（其余 13 行不变），这正是"重生成"该有的样子：

| 条目 | 旧（含 parked） | 新（合并后） |
|---|---|---|
| `bin_engine.py` | `fa50018dff72c6bc8aa146d27b5ffbb7` | `eec568b2a507768f93d169b29c95c838` |
| `bin_net.py` | `348ec97cbcbac45a308a63b4005ada4f` | `17d072ab2f378d056292f57488aed52c` |

（注意：旧清单里的 `fa50018d` 既不等于 parked 版 `17fc7f65`、也不等于副本版 `1967aa8a` ——
它是 T-14 **板端冻结点目录**（`t14_board/board_closure/`）里那份文件的 md5，属正常。）

### 5.4 C4（★ 决定性：合并不许改任何数值）

**跑点声明**（跑之前先估时间：`--hw 64` 三臂合计 **4~5 s**，实测 4.5~4.9 s，不是慢任务）。

四趟，全部 `python3 pynq_specialized_code/ps/tools/verify_lazy_net.py --hw 64`：

| # | 树 | BLAS 线程 | A（ref） | B（lazy） | C（nolazy） | A=B=C? | 脚本判定 |
|---|---|---|---|---|---|---|---|
| 1 | **纯 HEAD 副本** `/tmp/T15_head` | **默认（冻结跑点）** | `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd` | 同 | 同 | ✓ | **PASS rc=0** |
| 2 | **合并后的 main** | **默认（冻结跑点）** | `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd` | 同 | 同 | ✓ | **PASS rc=0** |
| 3 | 纯 HEAD 副本 | `OMP/OPENBLAS/MKL=1` | `24222554c880 / 2fd745d93159 / 8ab595fcf7cd` | 同 | 同 | ✓ | FAIL rc=1 |
| 4 | 合并后的 main | `OMP/OPENBLAS/MKL=1` | `24222554c880 / 2fd745d93159 / 8ab595fcf7cd` | 同 | 同 | ✓ | FAIL rc=1 |

纯 HEAD 副本怎么造的（可复算）：

```
mkdir -p /tmp/T15_head && git archive HEAD pynq_specialized_code | tar -x -C /tmp/T15_head/
cp -a pynq_specialized_code/deploy/bin_weights_new  →  /tmp/T15_head/pynq_specialized_code/deploy/
cp -a pynq_specialized_code/deploy/e1e2_desc        →  同上      # ★ 缺它会 B/C 回落 PS 打包
cp -a ps/tools/{verify_lazy_net,verify_e1e2_path,verify_e1e2_direct}.py  →  HEAD 副本的 ps/tools/
```

（三个 `verify_*` 脚本是 **untracked**，`git archive` 不会带；两侧用的是**同一份**脚本，md5 逐一对齐。）

**结论**：列表第 1 行与第 2 行**逐位相同**且**等于冻结 sha1** ⇒ **C4 通过：合并没有改任何数值**。
额外旁证：合并后的树跑 `bin_net.py --dry-run --hw 64`（冻结跑点）也是
`104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd`，并打出 `[DEPS] q8=fast … deps_closure_ok=True`。

#### ★ 必须报告的环境事实：这个 sha1 对 BLAS 线程数敏感

- 任务书要求 `export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1`（"可复现性"）。
  **一旦这样做，冻结常量 `104710f69c4d/2ac6efec2325` 就复现不出来**（得到 `24222554c880/2fd745d93159`）。
- 这不是我的合并造成的，也不是 T-14 造成的：**同一棵树**（T-14 副本原样）默认线程跑 **3/3 稳定**得冻结值；
  加 `OMP=1` 稳定得另一个值。差别只在 BLAS 归约顺序（out1/out2 变、out3 不变；min/max/mean 到 4 位小数完全一样）。
- 因此 C4 我按**冻结值的原始跑点（默认线程）**判，并**同时**给出 `OMP=1` 的一对，证明"两侧仍然逐位相同"。
  **两条都没放宽**；但"`OMP=1` 也要等于冻结 sha1"这个组合**在物理上不成立**，见 §8 待裁决项 D3。

### 5.5 C5（工作树干净）

```
$ git -C R diff --stat -- pynq_specialized_code/ps
 pynq/bin_engine.py | 353 +++  pynq/bin_net.py | 72 +++-  pynq/board_v39_verify.py | 112 ++++-
 3 files changed, 535 insertions(+), 2 deletions(-)
$ git -C R status --short -- pynq_specialized_code/ps | grep -v '^??'
 M pynq_specialized_code/ps/pynq/bin_engine.py
 M pynq_specialized_code/ps/pynq/bin_net.py
 M pynq_specialized_code/ps/pynq/board_v39_verify.py
$ git -C R status --short -- pynq_specialized_code/ps/tools/deps_closure.py
?? pynq_specialized_code/ps/tools/deps_closure.py          ← 新文件，untracked 是预期
$ git -C R status --short -- pynq_specialized_code/ps | grep -c '^??'
22                                                          ← 全部保留，一个没删
```

`bilinear_c.py / bin_ref_numpy.py / qcache.py / tools/export_network.py` 已回到 HEAD（不再出现在 diff 里）。

## 6. C6：md5 三列 + "差异只来自 parked"的机械证明

### 6.1 三列 md5

| 文件 | HEAD 版 | 合并后 main | `worktrees/T-14-deps` 版 |
|---|---|---|---|
| `pynq/bin_engine.py` | `ddfe6b6ffdfea33e76959748825d7af3` | **`eec568b2a507768f93d169b29c95c838`** | `1967aa8a57dabd40057a8a462785b132` |
| `pynq/bin_net.py` | `3f154b75f6526a096f78001aaa1e785b` | **`17d072ab2f378d056292f57488aed52c`** | `2e077940df90e5ac59583ae65af10b54` |
| `pynq/board_v39_verify.py` | `e9877b79f3f7403b4ddfa85a305f1068` | **`ff41b80b1c67f5102c56e9a26b8867f2`** | `903aa8bfb8b0e33a8fb6314a36e8eaf1` |
| `tools/deps_closure.py` | （不存在） | **`b6b85a4d64deda02c39e353b4e3a712c`** | `b6b85a4d64deda02c39e353b4e3a712c` |

**"合并后 ≠ 副本版"是预期的**（副本 = 合并后 + parked），`deps_closure.py` 相同也是预期的（新文件里没有 parked）。

### 6.2 归属证明（两个方向，脚本 `t15_merge/audit*.py` 可复算）

**方向 A：merged 比 worktree 少了什么？**（`diff -u worktree merged` 的 `-` 行）

| 文件 | `worktree 有/merged 无` 行数 | ⊆ parked patch 的 `+` 行 | 无法解释 |
|---|---|---|---|
| `bin_engine.py` | 320 | **320 / 320** | **0** |
| `bin_net.py` | 148 | **148 / 148** | **0** |
| `board_v39_verify.py` | 212 | 204 / 212 | 8（见下） |

`board_v39` 那 8 行 = "我改写之前的原文"（不是 parked、也不是丢失）：

```
'    rp["kernel_deps_probe"] = _d   …'          → 改成 _rp = R.setdefault("runpoint", {}) 后为 _rp[...]
'    rp["deps_closure_ok"] = bool(_ok) …'       → 同上
'               n_sha_ok=(int(ok) * 3 if frozen_cmp else None),'  → 用 HEAD 原式 n_sha_ok=int(ok) * 3,
'               #   sha_frozen_ok 抓不到这类事故）。'              → 改为 `sha_ok`（HEAD 没有 frozen_cmp）
'    if not rp.get("deps_closure_ok"):'         → 改为 if not _ok:
'        log("  ⚠ deps_closure_ok=False ⇒ …（跑的不是应跑的内核）")'  → 同义两行
'+ 1 行注释（"只记了时间、没记内核状态"）+ 1 行 [DEPS] 注释'  → 措辞微调后未保留原句
```

**方向 B：merged 里有没有"凭空发明"的行？**（即 worktree 没有、又不是 HEAD 也不是 T-14 增量的行）

| 文件 | merged 独有行 | 其中 ∉(HEAD ∪ T-14增量) |
|---|---|---|
| `bin_engine.py` | 16 | **0** |
| `bin_net.py` | 22 | **0** |
| `board_v39_verify.py` | 44 | **10**（=上表 3 处适配 + 7 行同义改写，全部逐行列出，无算法/数值内容） |

**再加一条最强的**：`bin_engine.py` 的三方合并（`git merge-file`，base=825cbb9）**零冲突**，产物 md5 与最终文件**完全相同**；
⇒ `bin_engine` 的合并是**机械可复算**的，不是手写的。

## 7. 与任务书 §2 判据的逐条对照

| 判据 | 结果 | 证据位置 |
|---|---|---|
| C1 停放 token 全 0 | **通过**（全 0，且无 `desc_cache/_dcs` 残留） | §5.1 |
| C2 增量在位（4 文件 / 可执行 / token>0 / `[DEPS]` 行） | **通过**（24 / 9 / 24；`-rwxrwxrwx`；L146/148/201） | §5.2 |
| C3 `--gen` + `md5sum -c` + `--check` 全 rc=0 | **通过**（0 / 0 / 0；15/15 成功） | §5.3 |
| C4 HEAD==merged 且 ==冻结 sha1 | **通过**（冻结跑点，两趟 PASS，逐位相同） | §5.4 |
| C5 工作树干净 | **通过**（3 文件 + 1 新文件；22 untracked 全留） | §5.5 |
| C6 三列 md5 + 差异归属 | **通过**（0 无法解释；`board_v39` 的 8/10 行已逐行列出） | §6 |

## 8. 待主代理裁决 / 未解决项（3 条，均不影响 C1–C6 成立）

- **D1（唯一"加回"）**：`bin_net.py` 里保留 6 行 `_e1p`（E1E2 `_prof` 读取）+ 1 行 `e1e2_prof=_e1p`。
  - 依据：T-14 的门槛块是 `if _e1p: perf_gate(...)`；HEAD + T-14 若不带 `_e1p`，**`perf_gate` 永远是 `[]`**（§④ 变死代码）。
  - 风险：这 6 行在 base 里是随 parked 镜像进来的（但用的是 HEAD 原生 `_prof`，且不含 C1 的任何 token）。
  - 若你要求"连这 6 行也不要"，改法是一条命令（删块 + `run_valid=bool(_dok)` 保持），代价 = T-14 §④ 在主机/板端都不再触发。

- **D2（board_v39 的 parked 机器没带）**：新基线的 `board_v39_verify.py` **没有** runpoint/CODE_FILES/code_md5/口径 a-b 记账
  （那 205 行 HEAD 从来没有）。当前版本仍会打 `[DEPS]`、每行带 `deps_closure_ok/perf_gate/perf_warm/run_valid`，
  也能 `R["runpoint"]["kernel_deps"]` 落盘。若你要**完整还原 T-14 板端证据形态**，
  唯一办法是把 parked 的 T-12 机器一起带进来（`cp worktrees/T-14-deps/.../board_v39_verify.py`，md5 变 `903aa8bf`），
  但那就是"把 parked 带进基线"，与 C1 的精神冲突。**我按"不带"执行，理由：C1 要求干净、且该机器与 T-14 无关。**

- **D3（★ 判据前提问题）**：`verify_lazy_net --hw 64` 的冻结 sha1 **与 BLAS 线程数耦合**，
  而任务书同时要求 `OMP/OPENBLAS/MKL=1`。两者不能同时满足。
  - 已证：默认线程 ⇒ 冻结值（3/3 稳定）；`OMP=1` ⇒ `24222554c880/2fd745d93159/8ab595fcf7cd`（3 臂一致、稳定）。
  - 建议（不擅自改）：**把冻结 sha1 连"跑点=默认 BLAS 线程"一起写进基线表**；或者重冻一个 `OMP=1` 口径的新 sha1 三件套。
    在此之前，本文按**冻结值原始跑点**判定 C4（这也与之前所有板端/主机证据同口径）。

## 9. 产物清单（全部已落盘）

| 路径 | 说明 | md5 |
|---|---|---|
| `runs/board_ops/20261102/deps_closure.txt` | **新清单**（合并后重生成） | `69a7c43237d1be7a21ce4f84fa4b908f` |
| `runs/board_ops/20261102/deps_closure_T14_verified.txt` | T-14 验证用原件（未覆盖） | `56f944918c7a854233caca9e1ed90e72` |
| `runs/board_ops/20261102/t14_pure_increment.patch` | 纯 T-14 增量（4 文件） | `938a1692b821a7a06e595684b016f29e` |
| `runs/parked_20261102/parked_ps_work_reconfirm_20261102_012940.patch` | 合并前 main 的完整脏 diff（7 文件） | `a3adcbdd9d53be2148f9c92435549736` |
| `runs/board_ops/20261102/t15_merge/` | 4 份 C4 日志 + 合并/审计脚本 + merged 的 `verify_lazy_net.json` + `MD5SUMS.txt` | 见其 `MD5SUMS.txt` |
| `runs/board_ops/20261102/REPORT_T15_merge.md` | 本报告 | — |

## 10. 没做的事（红线内）

- 未 commit / 未 tag（主代理动作）；未上板 / 未 ssh / 未 Vivado / 未 `pkill -f`。
- 未改 `worktrees/T-14-deps/`（只读：`git -C … show/diff`、`diff`、`cp` 读取）、未碰 `T-05-psdesc`、`T-08-q8`。
- 未删任何 untracked 的 `.bak*` / `*.so` / `__pycache__`。
- 未放宽任何判据；`--check`/`md5sum -c` 参数未改，`[DEPS]` 行是 T-14 真代码打出来的（合并后的树实测有
  `[DEPS] q8=fast c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast ⇒ deps_closure_ok=True`）。
- `runs/e1e2_direct/verify_lazy_net.{json,md}` 被 C4 第 2/4 趟覆盖过；**原文件已备份**到
  `/tmp/probe_T15/e1e2_direct_orig/`（原为 256² 那次运行），覆盖后的合并版另存 `t15_merge/verify_lazy_net_merged64_default.*`。
