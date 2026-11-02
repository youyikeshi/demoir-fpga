# v77 REVIEW 报告：合并 IMP-C（PS）+ IMP-A（RTL 核落地）+ 门禁 + 契约审查

日期 2026-11-02 15:45　｜　仓库 `/media/felix/Data/coding/demoir-fpga`　｜　HEAD `528be2f`
产物目录 `runs/v77_review/`　｜　缺陷清单 **16 条** → `DEFECTS.md`

---

## 0. 结论（先看这段）

| 项 | 结果 |
|---|---|
| **PS 合并（3 文件）** | ✅ 逐字节命中（`4d5cd86f / 8f77733b / 95636f8c`），另补 `DECL["fconv"]` |
| **merge_gate 三关** | ✅ `--gen` / `md5sum -c` / `--check` **rc = 0 / 0 / 0**（14 模块 / 20 文件） |
| **D1 默认等价（hw64）** | ✅ A/B/C = `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd`；归一化 stdout **0 差异** |
| **D1 hw256** | ✅ A/B/C = `392d1ae02761 / 7f68fb00d2b5 / aa03751a2d1c`（= FakePL 冻结值，见 §3.3） |
| **D2 `BIN_V50_FUSE=1`** | ✅ 跑通、sha1 与默认臂相同、打印 432 条链；⚠ **但链有 45 条幽灵带（D2/D3）** |
| **D2b `CHAIN_RTL=1`** | ✅ 探针假通过→S0 回落→sha1 不变（正是 D9 的证据） |
| **IMP-A RTL 落地** | ✅ 修后核 `4e6a4235`（925 行）已进 `lane16/`，manifest 刷新，25 基线 **MATCH=25** |
| **契约符合性** | ❌ 寄存器仍是 `0x200` 段（D1）、幽灵带（D2）、带基址 8 B/px（D3）、核重名（D4） |
| **能不能 commit** | **可以 commit PS 合并 + DECL + C 我修的 1 行 print**；但 `BIN_V50_FUSE=1` **不得**登记为可用（4 条 🔴） |

---

## 1. IMP-A RTL 落地（第 1 步）

| 动作 | 结果 |
|---|---|
| 源文件 | `_worktrees/v50_impA_fix/bin_conv_core.v`，md5 `4e6a42358a20d6488a508b39073f44fb`，925 行 |
| 落点 | `runs/timing_v50/rtl_v50/lane16/bin_conv_core.v`（md5 同上，逐字节） |
| 旧版处置 | `_worktrees/v50_impA/bin_conv_core.v`（`e2ed125f`，919 行）**未采用**；本轮曾按 15:30 版落地过一次，收到暂停指令后已撤回，留证 `runs/v77_review/quarantine_lane16_bin_conv_core.v.e2ed125f` |
| manifest | 26 条（25 基线 + `lane16/bin_conv_core.v`），md5 `803785cc…`；25 条基线逐行与旧 manifest **逐字节相同**（`diff` 空） |
| `verify_rtl_root.sh` | `MATCH=26 DIFF=0 EXTRA=0 MISSING=0` / `RESULT: PASS` / rc=0（日志 `S1_verify_rtl_root_final.log`） |

**修后核复核**（`IMP_A_fix.diff`，全文 4 个 hunk / 18 行）：
- F1：`hwout8_q <= (hout*wout)<<3` → `plane_stride_q <= Hout*Wout*VLANES`（组平面步长参数化）；
- F2：`nvalid_q <= cout - o_q` → `cout - (o_q+VLANES)`，且 `<0` 夹 0（末组掩码）；
- 其余为注释/复位/引用改名，**端口与参数未动**。

**但落地**≠**可构建**：lane16 里定义的是 `module bin_conv_core`，与基线薄壳
`rtl_v50/bin_conv_core.v` 重名 ⇒ `build_final_150v50.tcl:193` 会硬 `error`
（缺陷 D4）；另外 `bin_unit_axi_wrap_lane16` 仍缺（IMP-B 未交付，D5）。
⇒ 现在 `bd_v50.tcl` / `build_final_150v50.tcl` **仍不能跑**，这是预期的下一步。

## 2. IMP-C PS 合并（第 2 步）

| 文件 | 合并前 md5 | IMP-C 交付 md5 | 合并后 | 判定 |
|---|---|---|---|---|
| `ps/pynq/bin_net.py` | `9235f543` | `4d5cd86f` ✅ | `78dbf19c`（C 修 1 行后） | 命中 |
| `ps/pynq/bin_engine.py` | `4b1b25a7` | `8f77733b` ✅ | `8f77733b` | 命中 |
| `ps/pynq/qcache.py` | `4a0defde` | `95636f8c` ✅ | `95636f8c` | 命中 |

- 合并前原件留证：`runs/v77_review/pre/*.main_pre`。
- `bin_net.py` 的 `78dbf19c` = IMP-C 版 + **REVIEW 的 1 行 print 修复**（D6），
  补丁 `PATCH_bin_net_print.diff`；**未改任何算法**。
- `DECL["fconv"]` 已补（`ps/tools/deps_closure.py`，`src=None`，理由：`fconv.py`
  纯 Python、无 `.c`/`CDLL`；进入闭包是因为 `bin_net._fconv_engine()` 的懒加载
  `import fconv`）。这是第 41/44 条纪律的**第三次**，已在 DECL 注释里写明。
- `merge_gate.sh`：`① rc=0（14 模块/20 文件，含 fconv）`、`② 20 行 0 失败 rc=0`、
  `③ PASS rc=0`；旧清单自动留证 `deps_closure_prev_*.txt`。

## 3. 验收（第 3 步）

### 3.1 D1 默认 `BIN_V50_FUSE=0`，`--hw 64`（第一优先）
- A/B/C = `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd`（三臂相同，= 冻结 64² FakePL）
- **归一化 stdout 与合并前逐行比较：0 差异**。合并前基线不是引用 IMP-C 的日志，而是
  我用 main 的**合并前三个文件**搭了一棵对照树（硬链接 + 换回 pre 版本）现跑出来的
  （`D1_before_hw64.log`）；两边只归一化了 `\d+\.\d+` 形式的耗时数字。
- 运行点：`OMP_NUM_THREADS` **未设**（按要求；设 1 会给 `24222554c880…` 的假失败）。

### 3.2 D1 `--hw 256`
- A/B/C = `392d1ae02761 / 7f68fb00d2b5 / aa03751a2d1c`，三臂相同，
  脚本自检"= 冻结 256²（主机 FakePL）"⇒ 一致。
- **注意**：任务书里的 `9b1940c8…/f86a7035…/025757a8…` 是**板端** 256² 三件套
  （`docs/baselines.md` v4：显式三开 `BIN_PRECOMP/Q8CACHE/BIN_F32DIRECT` 的实测值），
  **不是** FakePL 值。我在合并前对照树上跑出同一组 `392d…`（`D1_hw256_before.log`）
  ⇒ 合并没有改变它，差异只是"运行点不同"。

### 3.3 D2 `BIN_V50_FUSE=1`（hw64 / hw256）
- hw64：`BIN_V50_FUSE=1 BIN_V50_DUMP=1` → A/B/C sha1 与默认臂**逐位相同**；
  打印 `432 条 × 40 B @ 4 KB 步进 = 1.77 MB；链首 0x50800000`，链尾 `next_pa=0`、
  全体 `next_pa` 4 KB 对齐；`segments.json` 交叉校验 `一致（223/223 段逐 op 相同）`；
  运行时落 arena `0x3016a000`。
- hw256：A/B/C = `392d…`（与默认臂相同），rc=0。
- **但没有通过我的语义审查**：链里 45 条描述符 `band_y0 = h_out`（幽灵带，D2），
  运行时段输出基址按 8 B/px 折算且与 `band_y0` 双重偏移（D3）。
  这两条让"D2 = 合法链"这句话**不成立**；我按判据"结构合法"判 PASS，
  同时在缺陷清单里标 🔴。

### 3.4 D2b / D3 附跑
- `BIN_V50_FUSE=1 BIN_V50_CHAIN_RTL=1`：探针"通过"（FakePL 寄存器会回声）→ 打印
  `链模式就位`，随后 `段 S0 链执行失败（段输入是 ndarray…）⇒ 该段回落逐 op`，
  sha1 不变。⇒ 这就是 D9 的**假通过**证据。
- `BIN_CONVPF_PL=1`：`overlay 里没有 fconv_0 ⇒ 回落 CPU 卷积`，三臂 sha1 相同。

## 4. 契约审查（第 4 步，核心）

| 契约项 | 结论 | 证据 |
|---|---|---|
| 寄存器 `0x300/304/308/30C/310` | ❌ **整段没搬**，仍 `0x200/204/208/20C`；`0x310 CHAIN_STATUS` 不存在 | `bin_engine.py:2271-2272` |
| `0x200..0x20C` 零改动 | ❌ 被当成 v50 寄存器；且因此 `FUSE=1` 时**强制不挂 E1E2** | `bin_engine.py:2149-2153, 2763-2769, 3070-3073` |
| 描述符 10 字 / 40 B / 4 KB 对齐 | ✅ 结构符合（`v50_desc` / `v50_chain_words` / `v50_chain_store`） | `bin_engine.py:2393-2464` |
| `next_pa` 链尾 = 0 | ✅ 离线与运行时都 0；实测末条 `w8=0` | `D2_fuse1_hw64.log:887` |
| 字 6 = `band_y0`（绝对行号） | ✅ 语义对（`b*band_rows`）；❌ 但算出了 `h_out` 这个越界值 | D2 |
| 字 7 = `flags` | ⚠ bit0/1/2 有，**bit3 从不置位**（契约定义了但不用） | D7 |
| 字 9 = `nband(低16) | op_mask(高16)` | ✅ 实测 `01720001`（nband=1, mask 0x172） | D2 dump |
| §5 ① `p_acc[15:0]` | ✅ 未变 | `bin_conv_core.v:354` |
| §5 ② `cout % 8` 未收紧 | ✅ 核里没有 `%8` 类布局假设 | 全文无 `% 8` |
| §5 ⑤ done 等 `wr_all_idle` | ✅（在 wrapper 层）`bin_unit_axi_wrap.v:960`；核只报 `unit_done` | 需在 lane16 外壳里复刻 |
| §5 ⑥ nvalid 掩码 | ❌ 修前用旧 `o_q`（F2，已由 SIM 修好并复核） | `IMP_A_fix.diff` |
| 禁区 `0x250..0x25C` | ✅ PS 三个文件**零处**触碰（grep 空）；核不含寄存器解码 | — |
| 预取参数 1/4/4 | ✅ 核里没有预取参数（都在 wrapper）⇒ 天赋不动；待 lane16 外壳交付时复核 | — |
| `segments.json:buf_bytes` 单位 | ⚠ f32（4 B/elem）vs 文档 1 B；IMP-C 按 int8 算（与契约 C 一致） | D13 |
| `BIN_V50_CHAIN_RTL=1` 误开 | ⚠ **确认会错**：v39 `0x208` 是 `r_edesc`（可读回）⇒ 探针假通过；建议加只读 magic + PROGRESS 单调校验 | D9 |
| `cout % VLANES == 0` 假设 | ⚠ PS 对 V>8 **硬抛错**（应保留：F2 只修写掩码，不修 PRM 逐 oc 连续装载） | D10 |
| `cfg_c` 非 8 倍数（4 段 = 3ch） | ⚠ 契约没定义组掩码责任方 | D11 |

## 5. 文件清单（本轮改动）

```
M pynq_specialized_code/ps/pynq/bin_net.py        （IMP-C 合并 + 1 行 print 修复）
M pynq_specialized_code/ps/pynq/bin_engine.py     （IMP-C 合并，逐字节）
M pynq_specialized_code/ps/pynq/qcache.py         （IMP-C 合并，逐字节）
M pynq_specialized_code/ps/tools/deps_closure.py  （+DECL["fconv"]） 
M runs/board_ops/20261102/deps_closure.txt        （merge_gate --gen 重生成）
?? runs/timing_v50/rtl_v50/lane16/bin_conv_core.v （IMP-A 修后核 4e6a4235）
?? runs/timing_v50/rtl_v50_manifest.txt           （26 条：25 基线逐字节不变 + lane16）
?? runs/v77_review/                               （本报告 + 缺陷清单 + 全部证据）
```

**没动**：IMP-A 的 RTL 逻辑、IMP-C 的三个 PS 文件算法、任何 BD/tcl、板子/ssh/Vivado。

## 6. 证据索引（`runs/v77_review/`）

| 文件 | 内容 |
|---|---|
| `DEFECTS.md` | 16 条缺陷（文件+函数+行号+现象+建议） |
| `S1_verify_rtl_root_final.log` | 26 条 MATCH / PASS |
| `S2_merge_gate.log`、`S2_merge_gate_final.log` | 三关 3/3 绿（合并后 / 修 print 后） |
| `D1_before_hw64.log` / `D1_after_hw64*.log` / `D1_norm_diff*.txt` | D1 归一化逐行对比（0 差异） |
| `D1_hw256.log` / `D1_hw256_before.log` | 256² 两个状态同值 |
| `D2_fuse1_hw64.log` / `D2_fuse1_hw256.log` | 链打印 + 幽灵带证据 |
| `D2b_chainrtl_hw64.log` | 探针假通过（D9） |
| `D3_convpfpl_hw64.log` | fconv 回落 |
| `IMP_A_fix.diff` | F1/F2 修法全文 |
| `PATCH_bin_net_print.diff` | REVIEW 唯一的代码改动 |
| `pre/*.main_pre` | 合并前三个 PS 文件原件（可回滚） |
