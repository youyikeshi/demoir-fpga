# T-161 金丝雀修复报告（PS 侧，主机验证）

日期：2026-11-04  分支：`restructure1`  工作区：`/media/felix/Data/coding/demoir-fpga`
交付目录：`worktrees/T-161-canary/`（**生产目录 `pynq_specialized_code/ps/pynq/` 一个字节未改**）

---

## 0. 结论摘要

| 项 | 结果 |
|---|---|
| ① 金丝雀紧跟 `out_bytes` | ✅ `[out_bytes, out_bytes+4096)` 填入/检查；放不下自动回落槽尾并打日志 |
| ① 报警带层名/偏移 | ✅ `首偏移=0`（数据边界写者）/ `4088`（槽尾之后写者），带 `out_bytes/start/len/回落` |
| ② 主机单测 U1~U4 | ✅ **4/4 PASS**（纯 numpy，无板、无 PL） |
| ② 变异测试 | ✅ M1（起点回退）U2 变红；M2（删槽尾窗）U3 变红；恢复后全绿 |
| ③ 端到端等价（engine=None 参考路径，256²） | ✅ out1/out2/out3 `np.array_equal` **全真**，sha1 逐字节相同 |
| 生产文件 md5 | 未变（见 §7） |

**核心判断**：旧行为下 227 层里只有 3 层（达到 `max_out=1 572 864 B`）的金丝雀紧贴数据，
其余 224 层即使越界 8 B 也落在最多 512 KB 的空档里，永远抓不到。修复后**每一层**都有
紧贴 `out_bytes` 的 4 KB 金丝雀；同时**保留**槽尾窗，否则"越过整个槽的写者"（desc/scratch/
别的槽）会从可观测变成不可观测（见 §4 M2 变异）。

---

## 1. 改动清单（patch：`T-161-canary.patch`，`git apply --check` 通过）

| 文件 | 行 | 改动 |
|---|---|---|
| `bin_engine.py` | 204–239 | 新增 `BinGuard.canary_spans(buf, n_used, tag)`：返回探测窗 `[(start, kind, fallback), …]`；主窗 `[n_used, n_used+4096)`，放不下 ⇒ 回落槽尾；另保留槽尾窗（与主窗不重叠时） |
| `bin_engine.py` | 241–249 | `fill_canary(buf, n_used=None, tag="?", log=True)`：按窗填 0xA5；回落时打印 `[CANARY] … 金丝雀**回落**到槽尾 …`（带 `out_bytes`/`len(slot)`） |
| `bin_engine.py` | 252–271 | `check_canary(buf, n_used, tag)`：逐窗 `np.flatnonzero(win != CANARY)`，报文含 **层名、`out_bytes`、`start`、`len(slot)`、是否回落、坏字节数、首偏移、前 16 个偏移** |
| `bin_engine.py` | 1601 | `run()` 非流水路径：`fill_canary(out_buf, need_out, tag="<层名> OUT")` |
| `bin_engine.py` | 1619 | `run()` 检查：`check_canary(out_buf, need_out, "<层名> OUT")` |
| `bin_engine.py` | 1865 | `reap()` 流水路径检查：`check_canary(out_buf, L.out_bytes, "<层名> OUT")` |
| `bin_engine.py` | 4964 | 主机自检的一行适配：旧用例用 `len-4096+1` 测"需求超界"，新语义下这属于**回落区**（不抛错），改为 `len+1`（数据本身超出槽）才对应原意 |
| `bin_net.py` | 1026 | 流水 `prepare()` 路径：`fill_canary(outb, L.out_bytes, tag="<层名> OUT")` |

> `bin_net.py` 没有自己的 `check_canary` 调用：流水路径的检查在 `bin_engine.reap()`
> （`bin_net._PlBackend.finish → e.reap`，bin_net.py:1085）。任务书里说的
> "bin_net.py 约 1813 行 reap" 实际是 `bin_engine.py` 的 `reap()`（当前副本为 1865 行，
> 已按任务书上移到 `L.out_bytes`）。两处 fill / 两处 check 已全部改到。

### 1.1 为什么是"两个窗"（U2 与 U3 同时成立的必要条件）

任务书要求 U2（坏在 `[out_bytes, out_bytes+8)`）首偏移 = **0**，U3（坏在 `[len-8, len)`）
首偏移 = **4088**。这两个位置不可能落在同一个 4096 B 窗里（`out_bytes=24576`、
`len=1 576 960` 时相距 1.5 MB）。因此：

* **主窗** `[out_bytes, out_bytes+4096)`：偏移 0 = 贴着本层数据边界往外写 ⇒ 修复新增的
  可观测性，直接回答"写者在不在本层数据边界外"；
* **槽尾窗** `[len-4096, len)`：偏移 4088 = 越过整个槽的写者（desc/scratch/别的槽）⇒
  保留旧行为唯一能抓的那类，避免修复把老 bug 变成"不可观测"。

两窗重叠时只查主窗（主窗已经覆盖到槽尾）；`n_used + 4096 > len` 时**回落**为只填/查槽尾，
行为与修复前逐字节等价，并由日志注明"回落"。

### 1.2 生产路径上"回落"是死代码（重要）

`run()` / `guard_layer()` 启动前已有 `avail = len(out_buf) - 4096; need_out > avail ⇒ ValueError`
（副本 bin_engine.py:1581 / 1652），所以真实跑网时 `out_bytes + 4096 <= len(slot)` 恒成立，
**主窗永远放得下**，回落只在直接调用 `BinGuard`（单测/自检/未来别的调用方）时触发。
真实几何下主窗终点 = `out_bytes+4096 <= max_out+4096 = len(slot)`，`out_bytes=max_out`
的 3 层恰好顶到槽尾（此时槽尾窗与主窗重叠、自动去重）。

---

## 2. 主机单测（`test_canary.py`，纯 numpy）

```
T-161 canary unit test
  被测实现: .../worktrees/T-161-canary/pynq_specialized_code/ps/pynq/bin_engine.py
  槽几何:   max_out=1572864 B, len(slot)=max_out+4096=1576960 B
  numpy:    2.2.6
[U1] PASS       ................................   out_bytes=24576 len=1576960 无报警；fill 只碰 8192 个金丝雀字节，数据区 [0,24576) 一个字节没动
[U2] PASS       ................................   报警，首偏移=0（期望 0）
[U3] PASS       ................................   报警，偏移=[4088]（期望含 4088）
[U4] PASS       ................................   out_bytes=8192+4096>len=12188 不崩；日志含'回落'；只改槽尾 [8092,12188)

结果: 4/4 PASS
```

* U1 用真实 `len(slot)=1 576 960`、`out_bytes=24576`：数据写满 `[0,out_bytes)` 后**不误报**，
  并额外断言 `fill_canary` 只碰金丝雀窗、`[0,out_bytes)` **一个字节没动**（零数值影响的最直接证据）。
* U2 `[out_bytes, out_bytes+8)` 写 0x00 ⇒ 报警，`首偏移=0`。
* U3 `[len-8, len)` 写 0x00 ⇒ 报警，槽尾窗 `首偏移=4088`。
* U4 `out_bytes=8192, len=12188`（`out_bytes+4096 > len`）⇒ 不崩、日志出现"回落"、
  只改 `[len-4096, len)`（与旧路径逐字节等价）。

**实际报文样例**（`out_bytes=24576`、`len=1576960`，层名取真实层名）：

```
# U2：贴着数据边界多写 8 B
decoder....conv OUT: 金丝雀被写坏 (out_bytes=24576, len(slot)=1576960, 回落=否)
  紧贴out_bytes窗 start=24576 坏=8/4096 首偏移=0
  前16偏移=[0, 1, 2, 3, 4, 5, 6, 7]
  ⇒ 偏移0=贴着 out_bytes 的越界写；偏移4088=槽尾之后(desc/scratch/别的槽)的写者

# U3：越过整个槽的写者
x OUT: 金丝雀被写坏 (out_bytes=24576, len(slot)=1576960, 回落=否)
  槽尾窗 start=1572864 坏=8/4096 首偏移=4088
  前16偏移=[4088, 4089, 4090, 4091, 4092, 4093, 4094, 4095]
```

## 3. 变异测试（`run_mutation.py`，真实改代码，不打 flag）

**M1 = 任务书要求的那一处变异**：`canary_spans` 起点退回 `len(slot)-4096`（修复前的单槽尾窗）。

```
[U1] PASS  out_bytes=24576 … 无报警；fill 只碰 4096 个金丝雀字节，数据区一个字节没动
[U2] FAIL  没报警 ⇒ 贴着 out_bytes 的越界写被漏掉
[U3] PASS  报警，偏移=[4088]
[U4] FAIL  日志**缺**'回落'
结果: 2/4 PASS
```

**⚠ 与任务书措辞的偏差（必须说明）**：M1 下 **U2 红、U3 仍是绿的**。原因是 U3 构造的
写者位置正是旧代码唯一能抓到的那个位置（槽尾窗）——把起点退回槽尾，U3 恰好落回它的
"舒适区"。任务书说"U2/U3 必须变红"，只有当修复把槽尾窗**删掉**时才成立，所以我补了 M2
来证明 U3 不是摆设：

**M2 = 删掉槽尾窗、只留紧贴窗**：

```
[U1] PASS  …
[U2] PASS  报警，首偏移=0
[U3] FAIL  没报警 ⇒ 槽尾之后的写者被漏掉
[U4] PASS  不崩；日志含'回落'
结果: 3/4 PASS
```

两个变异体合起来：**U2 只由"紧贴 out_bytes"这一处决定，U3 只由"保留槽尾窗"这一处决定**，
两条断言都不是摆设。恢复（即交付版本）后 **4/4 全绿**（§2）。
完整日志：`logs/mut_baseline.log`、`logs/mut_m1.log`、`logs/mut_m2.log`。

## 4. 端到端等价性（主机、纯 numpy 参考路径）

命令（`engine=None` ⇒ `bin_ref_numpy.conv_rtl`，不碰 PL/板子）：

```bash
python3 e2e_ref.py --pkg <prod>/pynq_specialized_code/ps/pynq \
    --pack runs/opt_round1/ps_numeric/pack_v60 --out e2e/outs_before.npz
python3 e2e_ref.py --pkg <copy>/pynq_specialized_code/ps/pynq \
    --pack runs/opt_round1/ps_numeric/pack_v60 --out e2e/outs_after.npz
python3 compare_e2e.py --before e2e/outs_before.npz --after e2e/outs_after.npz
```

输入 `runs/frozen256/p0000_crop256.npy`（256²，ps 归一化），整网 227 层，参考后端约 19~22 s：

```
键     形状            逐字节相同  sha1(before)     sha1(after)
  out1  3x256x256      YES       494abdfe8c2606c4  494abdfe8c2606c4
  out2  3x128x128      YES       5461e47f944efa3c  5461e47f944efa3c
  out3  3x64x64        YES       66a3db6d1e17a321  66a3db6d1e17a321

端到端等价: PASS（np.array_equal 全真）
```

说明：`engine=None` 时金丝雀代码不在数值路径上，所以这一条严格说证明的是
"**改动只碰 canary 辅助函数、没有触碰任何数值路径**"；"fill 不会改数据区"由 §2 U1 的
强断言（`[0,out_bytes)` 逐字节不变）单独证明。两条合起来即"零数值影响"。

## 5. 复现

```bash
cd worktrees/T-161-canary
python3 test_canary.py            # U1~U4，4/4 PASS，退出码 0
python3 run_mutation.py           # 变异测试，PASS，退出码 0
# 可选端到端：见 §4
```

## 6. 与 `runs/opt_round1/perf/` 里那份并行产物的关系（协调事项）

写报告时发现 `runs/opt_round1/perf/` 下已有一份**同一任务**的产物（`T161_canary.patch`、
`T161_bin_engine.py`、`T161_bin_net.py`、`test_canary_t161.py`，时间戳 18:24~18:27），
实现是"**单窗**紧贴 out_bytes + 回落槽尾"。差异：

* 那份**没有槽尾窗**（它的 U3 对照是用复刻的老逻辑单独做的）⇒ 按本任务书 §① 的 U3 判据
  （`[len-8,len)` 报警、首偏移 4088）它**不满足**；
* 那份回落时 `check_canary` 抛 `ValueError` 且**不打印"回落"** ⇒ 与本任务书 U4
  "不崩 + 日志出现回落"不一致；
* 两份的 `run()/reap()/prepare()` 调用点改法一致（都带层名）。

**建议主代理合并前二选一**，不要两份都并；本目录这份是按任务书 U1~U4 全判据实现的版本
（额外多一个槽尾窗 + 回落日志），并保留了旧自检语义的一行适配。

## 7. md5（修复前 = 当前生产工作树；修复后 = 本副本）

| 文件 | 修复前 md5 | 修复后 md5 |
|---|---|---|
| `pynq_specialized_code/ps/pynq/bin_engine.py` | `f3fca8a60183120e4643784fa3ffba1b` | `8f6717fec239b2d128ad3dac5b5694cd` |
| `pynq_specialized_code/ps/pynq/bin_net.py` | `c95d815447de364b524359d03af9f0ff` | `2c10422131c256eec3a0cf24de391403` |

生产目录的两个文件 md5 仍是"修复前"值 ⇒ **未修改**。

## 8. 交付物清单

```
worktrees/T-161-canary/
├── T161-final.patch                    # ★ 最终 patch（md5 06aa5294…，--check exit 0）
├── T-161-canary.patch                  # 同上（内容一致的别名）
├── pynq_specialized_code/ps/pynq/
│   ├── bin_engine.py                   # 修复副本
│   ├── bin_net.py                      # 修复副本
│   └── bin_ref_numpy.py                # 未改（仅为 import 完整性复制）
├── test_canary.py                      # U1~U4
├── run_mutation.py                     # M1/M2 变异
├── mutants/                            # 由 run_mutation.py 生成（真实改写的变异体）
├── e2e_ref.py / compare_e2e.py         # ③ 端到端等价
├── e2e/outs_{before,after}.npz
├── logs/                               # 单测/变异/E2E 原始输出
└── REPORT.md

../runs/opt_round1/perf/T161_SUPERSEDED.md   # 声明旧单窗方案作废，指向本目录
```

## 9. 未做 / 边界（按纪律）

* 没有 ssh、没有上板、没有 Vivado/xsim；
* 没有改 `pynq_specialized_code/ps/pynq/`、没有动 `runs/ACCEPT_v57`/`hdmi_v58`/`hdmi_v60`；
  没有 `git add`/commit/push；
* 没有跑 `bin_engine.py --selftest` 的 FakeUnit 整层 golden（需要 xsim golden 依赖；
  本任务书要求的 4 个用例 + 变异 + E2E 已完成）。自检里唯一受影响的一行（§1 表 4964 行）
  已按新语义适配，其余自检用例语义不变（尾部被写坏 → 仍抛 RuntimeError）。

---

## 10. 板端上板验证步骤（照抄给板端代理；主机侧只出步骤，未上板）

> 前提：主代理批准后，把 `T161-final.patch` 应用到生产工作树（或直接用本副本的两个
> 整文件）。**只覆盖 2 个文件**：`bin_engine.py`、`bin_net.py`。不动
> `bin_ref_numpy.py` / `demo_pipeline.py` / 权重容器 / 位流。
> 注：`runs/opt_round1/perf/` 里那份旧的 T-161 单窗方案已作废（见
> `runs/opt_round1/perf/T161_SUPERSEDED.md`），**不要用**。

### 10.1 备份 + 覆盖 + 校验 md5

```bash
cd /home/xilinx/demoir/ps/pynq
B=/home/xilinx/demoir/backup_pre_T161_$(date +%Y%m%d_%H%M)

# 0) 备份（回滚就靠它）
mkdir -p "$B"
cp bin_engine.py bin_net.py "$B"/
md5sum "$B"/*.py > "$B/md5_before.txt"

# 1) 覆盖前 md5（应与 v60 部署包一致）
md5sum bin_engine.py bin_net.py
#   期望(前): f3fca8a60183120e4643784fa3ffba1b  bin_engine.py
#             c95d815447de364b524359d03af9f0ff  bin_net.py

# 2) 覆盖（路径按主机实际仓库/挂载改）
cp <主机>/pynq_specialized_code/ps/pynq/bin_engine.py .
cp <主机>/pynq_specialized_code/ps/pynq/bin_net.py    .

# 3) 覆盖后 md5（**必须**逐字节一致，否则别跑）
md5sum bin_engine.py bin_net.py
#   期望(后): 8f6717fec239b2d128ad3dac5b5694cd  bin_engine.py
#             2c10422131c256eec3a0cf24de391403  bin_net.py
```

### 10.2 单帧命令（v60 契约；`BV_FREE=1` 必须有，256² 否则会被 726 MB 中间张量拖进换页）

```bash
cd /home/xilinx/demoir/ps/pynq
BV_FREE=1 python3 board_verify_numfix.py \
  --pack   /home/xilinx/demoir/deploy/bin_weights_new \
  --input  /home/xilinx/demo/frozen256/p0000_crop256.npy \
  --golden /home/xilinx/demo/frozen256/golden/p0000_out1_fp.npy \
  --bit    /home/xilinx/demoir/design_1_v60.bit
```

开头必须有（缺一行就停，说明环境/配对不对，不是金丝雀问题）：

```
[numfix] 容器配对：v60（round_q=[0] slope_shift=[8] thr32=有）
[DEPS] pack=ps（PS 打包，吃容器里的 f32 阈值 off_thr32）
```

跑完若金丝雀没坏，数值结果应与打本 patch 之前**完全一致**（本 patch 只动护栏，
`BIN_*` 开关一个没加；主机端 `np.array_equal` 已证）。5 张连跑（~100 s）用
`PACK_V60.md` §5.2 的 for 循环 + `--vd-aggregate`。

### 10.3 判读表（板端只看这一张）

| 观测 | 说明什么 | 下一步 |
|---|---|---|
| **跑完不报错**（V-D/DC 判据与 patch 前相同） | 本帧没有踩到任何金丝雀窗：既没有"贴着某层 out_bytes"的越界，也没有"越过整个槽"的写者 | 单帧通过 ≠ 无害；继续 5 张连跑 + 多帧长跑（旧 bug 是"偶发"） |
| **报错，首偏移 = +0**（`<层名> OUT: … 紧贴out_bytes窗 start=… 首偏移=0`） | 写者就在**该层数据边界外** 8 B 起往外写 ⇒ 引擎/RTL 对**该层**多打了数据（`out_bytes` 少算 / 写指针越界 / C8 布局多一组） | 记 **层名 + 形状(cin/cout/k/s/d/H×W) + 坏字节数** 给 RTL 代理仿真该层；这是修复新增的可观测性，旧代码 224 层看不到 |
| **报错，首偏移 = +4088**（`<层名> OUT: … 槽尾窗 start=… 首偏移=4088`） | 写者越过**整个 OUT 槽**（槽尾之后）⇒ 不是该层数据越界，而是 desc / scratch / 相邻槽 / arena 切分方向的写者 | 查 desc 区、scratch、槽复用边界、arena 对齐；与层数值无关（旧代码也抓得到，属"老已知"类） |
| **报错且两窗都坏**（报文两段） | 数据边界 + 槽尾同时被动，或一次大越界跨过两窗 | 先按 **+0** 归因，再按 +4088 查槽尾 |
| **224 个"小层"里也报**（层名不是那 3 个 `max_out` 层） | ⚠ **范围扩大**：8 B 越界不是 `max_out` 三层专属；之前它们被 512 KB 空档掩盖，现在暴露 | 统计报警层名/形状/首偏移分布 → 交 RTL 代理系统归因；**不要**关金丝雀、不要改回槽尾 |
| 报错但 `len(slot)`/`out_bytes` 明显不对（≥ max_out+4096 或 0） | 调用方传错缓冲/层对象 | 先查 `out_buf` 来源与 `L.out_bytes`，不是 RTL 问题 |

补充：那 3 个 `max_out` 层（`decoder.decoder_1.rdb.conv_layers.{0,1,2}.conv`）在主窗
恰好顶到槽尾，新旧行为**等价**——它们报警是老代码本来也能抓到的；修复的增量价值是
**其余 224 层**能报警了。

### 10.4 回滚

```bash
cd /home/xilinx/demoir/ps/pynq
cp "$B/bin_engine.py" "$B/bin_net.py" .
md5sum bin_engine.py bin_net.py   # 应回到 f3fca8a6… / c95d8154…
```

## 11. 最终 patch（可直接应用到生产工作树）

| 项 | 值 |
|---|---|
| 文件 | `worktrees/T-161-canary/T161-final.patch`（= `T-161-canary.patch`，内容相同） |
| 基准 | 生产工作树 `bin_engine.py` `f3fca8a60183120e4643784fa3ffba1b`、`bin_net.py` `c95d815447de364b524359d03af9f0ff` |
| patch md5 | `06aa52945bf06fd99a353c61bb2c9eac` |
| `git apply --check` 退出码 | **0**（在干净副本上实测；有"100644/100755"模式 warning，不影响应用） |
| 应用后 md5 | `bin_engine.py` `8f6717fec239b2d128ad3dac5b5694cd`、`bin_net.py` `2c10422131c256eec3a0cf24de391403`（已在 `/tmp` 干净副本上 apply 后逐字节比对） |
| 规模 | 2 文件 / 6 hunk / +63 −16 行 |
| 应用命令 | `cd <repo> && git apply worktrees/T-161-canary/T161-final.patch` |

**未执行**：patch 未应用到生产目录（等主代理批准）。
