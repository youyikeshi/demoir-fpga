# T-37：把 T-16 的 prologue 三刀合进 main + 判据（离线，板子断电）

## 结论先行（不读成别的）

| 项 | 结果 |
|---|---|
| reject | **0**（`patch -p1`，净树 dry-run 与实打各一遍；**无 `.rej` / 无 `.orig` / 无 fuzz / 无 offset**） |
| 手工解 hunk | **0 处**（20/20 hunk 精确匹配；原因见 §2，非"跳过检查"） |
| N1 全关 = baseline | **rc=0**，A/B/C = `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd` = 冻结值（默认 BLAS 线程口径） |
| N2 三刀 15 臂（hw=64） | **rc=0**，15/15 臂同一组 sha1 = 上面三个冻结值 |
| N2 三刀 15 臂（hw=256） | **rc=0**，15/15 臂 = `392d1ae02761 / 7f68fb00d2b5 / aa03751a2d1c` = 冻结 256² 值（489 s） |
| N3 负例 ×4 | **rc=0**，四条全部 `[OK]`（miss 计数 + 读回寄存器 = 新地址） |
| N4 stdout 不变 | **rc=0**，`verify_lazy_net --hw 64` 归一化后 **diff 为空**（原始 diff 只有 4 行时间） |
| N5 禁区 | **无改动**：`bin_ref_numpy.py` 未动；`_fp_conv`/`conv2d_f32`/`gemm_split_m` 在 diff 的 ± 行里 **0 次出现** |
| commit | **1 个提交、不打 tag**：`feat(ps): 合并 T-16 prologue 三刀（默认关；预计算/q8缓存/f32直读）`（hash 见 §5.5 / `git log -1`） |

**一句话**：三刀是**纯新增分支 + 默认关**，在 main 上不需要手工解 hunk；五条判据全 rc=0，sha1 一个都没变。

---

## 1. 合入过程

### 1.1 补丁本体（先纠正一处台账偏差）

诉求单写的是「1008 行 / 4 文件 / 20 hunk / +807 −29」。**盘上实际是**：

```
runs/t16_prologue/T16_prologue.patch   1077 行   4 文件   20 hunk   +871 −29
  bin_engine.py            +360/−21    7 hunk   (@@1281 @@1293 @@2073 @@2087 @@2185 @@2287 @@3765)
  bin_net.py                +93/−8     6 hunk   (@@651  @@726  @@835  @@1290 @@1689 @@1991)
  qcache.py                 +77/−8     6 hunk   (@@121  @@129  @@141  @@173  @@308  @@321)
  test_t16_ab.py           +370 (新)   1 hunk   (@@-0,0 +1,370)
```

- 文件 mtime 02:29:32，**早于** `REPORT_phase2.md` 的 02:29:43；`mk_patch.sh` 02:09 生成。
- 判断：**+807 是旧一版的计数**（T-16 报告 §1 明说"改完最后两处（`_SegList`/内联重放）后**又跑 6 臂复核**"），
  盘上这份才是最终版，`+64` 行就是那两处修复。**不是内容漂移**——证据见 §1.3：打完与副本 md5 逐一相同，
  而副本正是 T-16 实际跑出 15/15 与 6 臂复核的那份代码。
- 结论：**按盘上 1077 行这份合并**，并以"打出来 == 副本"作为它确实是"被判过的那份"的证明。

### 1.2 净树干跑（`/tmp/T37_clean` = `git archive HEAD`）

```
mkdir -p /tmp/T37_clean && git archive HEAD | tar -x -C /tmp/T37_clean
cd /tmp/T37_clean && patch -p1 --dry-run -i .../T16_prologue.patch   → rc=0，4 文件全 "checking file"，无 reject
cd /tmp/T37_clean && patch -p1          -i .../T16_prologue.patch   → rc=0，4 文件全 patched
find /tmp/T37_clean -name '*.rej' -o -name '*.orig'                  → 空
```
注意：净树缺 `deploy/bin_weights_new`（`deploy/` 是 untracked），跑判据时用 symlink 指回 main 的同名目录
（只读挂链，不改内容）；**patch 判定本身不需要权重**。

### 1.3 打到 main，以及与 `worktrees/T-16-prologue/` 的逐文件比对

```
cd R && patch -p1 -i runs/t16_prologue/T16_prologue.patch     → rc=0，4 文件，日志里无 fuzz/offset/reject
```

| 文件 | 合并后 md5（= 副本 md5） | 合并前 HEAD md5 |
|---|---|---|
| `ps/pynq/bin_engine.py` | `d916f230568d9f14a92731a8e56f1553` | `eec568b2a507768f93d169b29c95c838`（= baselines.md 台账） |
| `ps/pynq/bin_net.py` | `ee113f5b0061b86207da4c88c187e225` | `033d6a00f71b071d1970f170a1274951` |
| `ps/pynq/qcache.py` | `4a0defde90cd956e9a9d0433b24b5eec` | `d2dc2517f1bad49ab6c61f0c3ae96944`（= 台账） |
| `ps/tools/test_t16_ab.py` | `b77829de9a3d7e26a4502f6f4d3a2405` | （新文件） |

**4/4 SAME**。另用 inode 自证这是两份真文件（不是同一个文件被读两次）：
`pynq/bin_net.py` inode `992451` vs 副本 `951007`。

### 1.4 "没有反向删掉 HEAD 已有内容"的三重证明（T-16 自己踩过的坑）

T-16 第一版 patch 会把 `CONVPF_FUSE` 反向删掉，所以这里不敢只靠"rc=0"：

1. **AST 符号集比对**（HEAD 版 vs 合并后版，覆盖 `def` / `class` / `Class.method`）：
   `bin_engine.py` 201→212（**消失 0**，新增 11 个全是 T-16 的 precomp/arm_cached/seg_cached）、
   `bin_net.py` 91→93（**消失 0**，新增 `_SegList` / `_env_on`）、
   `qcache.py` 21→21（**消失 0**）。⇒ **没有任何符号被删**。
2. **删除行全集 = 29 行**，逐行看过：全部是"同一函数内被替换的旧行"（`return segs` / `return None` /
   旧 `quantize_leaf_c8` 头部 / 旧 fill 分支等），**没有一行属于 convpf**。
3. **正反向 grep**：合并后文件里 `_fp_conv` 出现次数 **2**（= HEAD 的 2，未减）；
   `CONVPF_FUSE` 仍在 `bin_net.py:63`（import）与 `bin_net.py:1369`（relu 就地分支）；
   diff 的 ± 行里 `_fp_conv|CONVPF_FUSE|conv2d_f32|gemm_split_m` 匹配 **0** 次。

## 2. 为什么 0 处冲突（不是"运气"，是可解释的）

T-16 的基线是 `3207a79`，之后被人 `--amend` 成 `613a117`（+convpf 平台闸门/gitignore/`ps/tools` 新脚本）。
把两者相对**被 patch 的三个文件**的改动位置列出来，就看得出不相交：

| 来源 | 位置 | 与 T-16 hunk 的关系 |
|---|---|---|
| T-17（`613a117`） | `bin_net.py:63`（`CONVPF_FUSE` import）、`bin_net.py:1366~1369`（`_fp_conv` 里 relu 就地） | T-16 的 bin_net hunk 结尾在 **1340+15 = 1355**（`@@ -1290,10 +1340,15 @@`）⇒ 隔 11 行，不重叠 |
| T-17（`613a117`） | `bin_ref_numpy.py`（+162/−2）、新增 `ps/pynq/{convpf_c.c,convpf_c.py,...}`、`ps/tools/{convpf_*,probe_serial_k}.py` | T-16 **完全没碰** `bin_ref_numpy.py`，也没有新增/覆盖 `ps/tools/` 里的任何同名脚本 |
| T-16 | `bin_engine.py` 7 处、`bin_net.py` 6 处、`qcache.py` 6 处、`ps/tools/test_t16_ab.py` 新文件 | — |

⇒ `patch` 以 **fuzz=0** 精确匹配 20/20 hunk。**因此"手工解了几处"的正确答案是 0，
且这 0 处有 md5 + AST 双证据背书，不是"没检查"。**

### 2.0 一条环境偏差（必须记录）

开工时 brief 给的 `HEAD = 5ed6175`。我执行期间 main 上**并发落了一个 docs-only 提交**
`402784d docs(plan): T-34 找到 conv stall 真机制（预取太晚）+ T-16 三刀结果`
（只改 `docs/PLAN_1second.md` + `docs/PLAN_TASKS.md`，`git show --name-only` 可复核；**不含任何 `pynq_specialized_code/` 文件**）。
补丁实际打在 `402784d` 上；由于被 patch 的三个文件在 `5ed6175` 与 `402784d` 之间**逐字节相同**，
结论与打在 `5ed6175` 上等价。提交也落在 `402784d` 之上（§7）。

## 3. 判据（全部 rc=0；**默认 BLAS 线程口径**，第 72 条纪律）

环境自证：跑前 `env | grep -Ei 'omp_num|openblas|mkl_num|bin_precomp|q8cache|f32direct'` **输出为空**
⇒ 既没设线程口径变量（不落进 `24222554c880` 那组假警报），也没预开任何开关。

### N1 全关 = baseline
```
cd pynq_specialized_code && python3 ps/tools/verify_lazy_net.py --hw 64      # rc=0
```
```
[DEPS] q8=fast c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast  ⇒ deps_closure_ok=True
A 基线（纯 PS 参考）   sha1 = ('104710f69c4d', '2ac6efec2325', '8ab595fcf7cd')
B E1E2 + LAZY_DEQ=1   sha1 = 同 A
C E1E2 + LAZY_DEQ=0   sha1 = 同 A
   冻结 64² 基线（主机 FakePL） ⇒ 一致
== 总结：PASS（4.2 s）==
```
⇒ **A=B=C=冻结值**，且惰性统计与合并前逐字段相同（`pinned 198 / released 168 / starved 30 / direct_layers 200 / deq_skipped 198 / direct_bytes 5389440 / f32_bytes 2640704`）。日志 `/tmp/T37_n1_stdout.log`。

### N2 三刀单开/组合开（T-16 的 15 臂换序序列）

臂序列 `off0,A1,off1,A2,off2,B1,off3,B2,off4,C1,off5,C2,off6,ABC,off7`，每臂新建 engine+BinNet。

**hw=64（rc=0，22 s，`/tmp/T37_n2_hw64.log`）**
```
✓ 全部 15 臂 sha1 逐位相同：('104710f69c4d', '2ac6efec2325', '8ab595fcf7cd')
✓ 等于冻结常量 104710f69c4d（默认 BLAS 线程口径）
== PASS ==
```
结构性旁证（与 T-16 报告一致）：`A2` 的 `arm_hit/miss 227/0`、`seg_hit/miss 200/0`；
**`mmio` 写 A 开/关都是 5927，读 3128→1312**（少 1816 = 227×8 次基址回读）；C 让写 5927→6115。

**hw=256（rc=0，489 s，4.2 GB RSS，`/tmp/T37_n2_hw256.log`）**
```
✓ 全部 15 臂 sha1 逐位相同：('392d1ae02761', '7f68fb00d2b5', 'aa03751a2d1c')
✓ 等于冻结常量 392d1ae02761（默认 BLAS 线程口径）
== PASS ==
```

> **两个必须写明的观察，不许拿"全过"盖掉：**
> 1. 标准 15 臂里 `ABC` **只跑一趟**，它的 `seg_hit/miss = 0/227`（arm 仍 227/0 命中）。
>    T-16 报告 §5.2 说"ABC 时 227/227 全命中"，指的是**连跑两趟的第二趟**。
>    我按它的复现命令补跑了 6 臂 `--arms off1,A2,A2,off2,ABC,ABC`（hw=64，rc=0，全臂 = 冻结值）：
>    第 2 趟 `ABC` 变成 `arm 227/0、seg 227/0` ⇒ **跨帧命中确实成立**，两处表述不矛盾，
>    但"15 臂"这一组本身**不能**当跨帧证据用。
> 2. B 臂在 hw=64 上仍是净亏（0.0410 vs off 的 0.0365~0.0417 同量级），与 T-16 §3 的"B 在 64² 不划算"一致；
>    hw=256 上 `q8c 71/41`（命中/未命中）与报告完全对上，`q8_mb` 64²→0.92 MB、256²→14.67 MB。

### N3 负例（`--neg` 四条，rc=0，`/tmp/T37_n3_neg.log`）
```
[OK ] ① 换 OUT 地址 ⇒ 必 miss 且写入新地址  {"miss_before":1,"miss_after":2,
       "out_pa_1":806498496,"out_pa_2":806633728,"expect_out_2":806633728}
[OK ] ② 换 IN  地址 ⇒ 必 miss 且写入新地址  {"miss":1,"in_pa":806465664,"expect_in":806465664}
[OK ] ③ 换段 pa      ⇒ 必 miss 且描述符 src 跟着换
       {"seg_miss":2,"seg_hit":0,"src_a":806498496,"src_b":806633728,"expect_a":806498496,"expect_b":806633728}
[OK ] ④ 同地址换形状 ⇒ 必 miss             {"seg_hit":0,"seg_miss":1}
⇒ 全部通过
```
数字与 T-16 报告 §1 的负例表**逐个相同**（`806498496→806633728` 等）⇒ 失效是结构性的：
键里含喂给被替代函数的全部输入（层 + in_pa + out_pa + 段 pa 列表 + 形状 + fs）。

### N4 stdout 不变（归一化后逐行相同）
合并前参考：`/tmp/T37_base`（= `git archive HEAD` 的独立拷贝，未打 patch）跑同一命令，rc=0、
sha1 = 冻结值、`PASS（4.5 s）`。归一化规则（只归一化时间，不动整数/计数/sha1）：
```
sed -E 's/[0-9]+\.[0-9]+/<T>/g; s/\b[0-9]+ s\b/<T> s/g'
diff -u pre.norm post.norm   → 空   （rc=0）
```
原始 diff 只有 4 行，全是耗时：`总 1.56→1.23 s`、`总 1.50→1.39 s, prologue 0.19→0.06`、
`总 1.39→1.44 s`、`PASS（4.5→4.2 s）`。**文本一行不差、sha1 一行不差、惰性统计字段不差**。

### N5 禁区
- `git status` 里 `bin_ref_numpy.py` / `convpf_c.py`：**未修改（空）**。
- `git diff | grep -E '^[-+].*(_fp_conv|conv2d_f32|gemm_split_m)'`：**0 条**。
- `qcache.py` 的改动全在 `quantize_leaf_c8(out=)` / `QuantLeafCache` / `get_c8`，与 convpf 无交集。

## 4. 静态检查

```
python3 -m py_compile ps/pynq/bin_engine.py ps/pynq/bin_net.py ps/pynq/qcache.py ps/tools/test_t16_ab.py
  → rc=0（无输出）
```
三个开关的**默认值就是"不走预计算/不建缓存/不走直读"**（逐条贴源码）：

| 开关 | 判定表达式 | 未设时 |
|---|---|---|
| `BIN_PRECOMP` | `bin_engine.py:4083` `_ON_VALS.get(os.environ.get("BIN_PRECOMP", "").lower(), False)` | `""` 不在 `{1,on,true,yes}` ⇒ **False** |
| `Q8CACHE` | `bin_net.py:1759` `os.environ.get("Q8CACHE", "0")` 且要求 `in ("1","on","true","yes")` | 默认 `"0"` ⇒ **不建 `QuantLeafCache`** |
| `BIN_F32DIRECT` | `bin_net.py:1749` `os.environ.get("BIN_F32DIRECT", "").lower() in ("1","on","true","yes")` | `""` ⇒ **False** |

`grep -c` 补充：`precomp_on()` 是全仓唯一入口（定义 `bin_engine.py:4081`，判定行 `:4083`，`_ON_VALS` 在 `:4078`），`_PlBackend.prepare` 的 C 分支是
`elif`（三分支不变），`Q8CACHE_MULTIFS` 虽然默认 `"1"`，但**只有 `_qcache is not None` 时才有代码读它**
⇒ 关掉 B 就不可达，不构成"默认开"。另外 `precomp_on()` 关时 `arm()`/`prepare_segments()` 走原函数体，
`_pack_views` 的"进程级复用"分支也只在 `precomp_on()` 时进 ⇒ **关掉 = 原路径，与 N1/N4 的实测吻合**。

## 5. 顺带审计：patch 有没有把不该进 git 的东西带进来

- **没有。** patch 里 `---/+++` 只有 4 个 `.py`；`grep -E '^(---|\+\+\+) .*(\.so|__pycache__|\.pyc|\.json|\.log|\.o$)'` **0 命中**。
- 副本 `worktrees/T-16-prologue/` 里**确实有** `ps/pynq/bilinear_c_x86_64.so`、`sam_sum_fast_x86_64.so`、
  多个 `__pycache__/`——但它们是**既有 untracked 产物**，`mk_patch.sh` 只 diff 三个源文件 + 一个新脚本，
  **一个都没带进来**。`__pycache__/` 也已被 `.gitignore:14` 拦住（`git check-ignore -v` 已验）。
- **重名覆盖：没有。** `git ls-tree HEAD ps/tools/ | grep test_t16` 为空 ⇒ `test_t16_ab.py` 是纯新文件；
  副本 `ps/` 与 main 的 `diff -rq` 只剩 5 个"只在 main"的文件（`convpf_ab/neon_sim/project/timing.py`、
  `probe_serial_k.py`，都是 T-17 的），**副本缺它们、patch 不涉及它们**，合并后 main 一个不少。
- 三个 `.py` 的合并后 md5 与副本逐一相同（§1.3）⇒ patch 没有顺手把别的文件"带回旧版"。

## 5.5 提交内容

**只有一个提交、不打 tag**，标题行 `feat(ps): 合并 T-16 prologue 三刀（默认关；预计算/q8缓存/f32直读）`，
落在 `402784d`（并行 docs-only 提交）之上。显式清单如下（**没有用 `git add -A`**）：

- 代码 4 个：`ps/pynq/bin_engine.py`、`ps/pynq/bin_net.py`、`ps/pynq/qcache.py`、`ps/tools/test_t16_ab.py`
- 本报告 + T-16 的 `T16_prologue.patch` / `mk_patch.sh` / `REPORT_phase2.md` / `REPORT_attribution.md`
- T-16 证据：`probe/`（8 文件）、`probe2/`（9 文件）、本次 `logs_merge_T37/`（9 文件）
- **未纳入**：`runs/resguard/resguard.log`（并行任务在写，与本计划无关）、
  `pynq_specialized_code/runs/`（`verify_lazy_net` 的 OUTDIR 落盘）、`deploy/`、`pl/` 等全部 untracked 构建产物

## 6. 未验项（**必须在 commit 正文里留痕**）

1. **B 的 32 MB CMA 容量**：`Q8CACHE` 每帧要新分配一块 `Q8CACHE_MB=32` 的 CMA（`run_net` 每帧新建 engine），
   与 66 MB arena 同时在场 ≈ 98 MB。板端 `CmaFree` 曾报 0.5 MB 却仍分配成功 66 MB ⇒ **只能上板实测**。
   本次宿主侧 `q8_mb` 实测 14.67 MB（256²）/0.92 MB（64²），**不能**用来推板端 CMA。
2. **A 的跨帧地址稳定性**：只在主机 `SubAlloc` 模型上验过（Node：hw=64 15 臂 + 6 臂复核 `arm 227/227、seg 227/227`）。
   板端每帧新建 engine ⇒ 要靠 `BIN_PRECOMP=1` 的"进程级复用 + 键含分配器身份"来保证地址稳定；
   **板端第一个要看的数就是 `BIN_PRECOMP_STATS=1` 的 `[PRECOMP] arm hit/miss / seg hit/miss`**。
- 另：C 的收益是**板端特有**（`to_c8_padded` 在 A53 上慢），主机 64²/256² 都测不出收益 ⇒ 本次只有 sha1 等价性，
  没有性能旁证。**板端投影 −590 ms（1.595→≈1.005 s）仍是投影，不是实测。**

## 7. 复现（全部离线）

```bash
R=/media/felix/Data/coding/demoir-fpga
# 0) 净树干跑
rm -rf /tmp/T37_clean && mkdir -p /tmp/T37_clean && git -C $R archive HEAD | tar -x -C /tmp/T37_clean
cd /tmp/T37_clean && patch -p1 --dry-run -i $R/runs/t16_prologue/T16_prologue.patch
# 1) N1
cd $R/pynq_specialized_code && python3 ps/tools/verify_lazy_net.py --hw 64
# 2) N2（hw=64 约 22 s；hw=256 约 489 s / 4.2 GB）
python3 ps/tools/test_t16_ab.py --hw 64
python3 ps/tools/test_t16_ab.py --hw 256
# 3) N3
python3 ps/tools/test_t16_ab.py --hw 64 --neg
# 4) 跨帧命中（额外补跑，第 2 趟 ABC 应变 227/227）
python3 ps/tools/test_t16_ab.py --hw 64 --arms off1,A2,A2,off2,ABC,ABC
```
**第 72 条纪律**：以上 sha1 全在**默认 BLAS 线程口径**下取（未设 `OMP/OPENBLAS/MKL_NUM_THREADS`）。

## 8. 产物

| 路径 | 内容 |
|---|---|
| `/tmp/T37_dryrun.log` | 净树 dry-run（4 文件 checking，0 reject） |
| `/tmp/T37_apply_main.log` | 打到 main 的日志（4 文件 patched，0 fuzz） |
| `/tmp/T37_pre_stdout.log` / `/tmp/T37_n1_stdout.log` | N4 的合并前 / 合并后 stdout |
| `/tmp/T37_pre_norm.log` / `/tmp/T37_n1_norm.log` / `/tmp/T37_n4_diff.txt` | 归一化与空 diff |
| `/tmp/T37_n2_hw64.log` / `T37_n2_hw64.json` | N2 hw=64 15 臂 |
| `/tmp/T37_n2_hw256.log` / `T37_n2_hw256.json` | N2 hw=256 15 臂 |
| `/tmp/T37_n3_neg.log` / `T37_n3_neg.json` | N3 负例四条 |
| `/tmp/T37_headsrc/*.py` | HEAD 侧三个源文件（AST 比对用） |

`/tmp` 是临时目录，**已归档的判据日志在 `runs/t16_prologue/logs_merge_T37/`**（N1/N2 hw64/N2 hw256/N3/N4 前后/stdout diff/6 臂跨帧/dry-run/apply，9 个文件），随提交一起入库 ⇒ 后人不需要 `/tmp` 也能复算。
