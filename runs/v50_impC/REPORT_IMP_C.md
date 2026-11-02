# IMP-C（v50 PS 侧）报告 — 段计划 + 描述符链 + fconv 接线

工作目录 `worktrees/v50_impC/`（从 `pynq_specialized_code/ps` 拷出，未改 main）。
所有判据在本机跑（无板、无 Vivado/xsim）。运行点：主机 FakePL（`conv_rtl`+`e1e2_sim`），
`--hw 64`，**默认 BLAS 线程（不设 OMP_NUM_THREADS）**。

## 0. 一句话
`BIN_V50_FUSE=0`（默认）**逐字不变**；`BIN_V50_FUSE=1` 能离线生成**合法的整网描述符链**
（223 段 / 432 条 / 1.77 MB / 4 KB 步进 / 尾 next_pa=0），RTL `bin_segq` 未到位时
**逐段回落 v5 且 sha1 不变**。

## 1. 判据结果

| 判据 | 结果 | 证据 |
|---|---|---|
| **D1** 默认等价 | **PASS**：A/B/C = `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd`；归一化 stdout **逐行相同（0 差异）** | `D1_base_stdout.txt` / `D1_after_stdout.txt` / `D1_norm_diff.txt`（空） |
| **D2** `BIN_V50_FUSE=1` 合法链 | **PASS**：395 op → 223 段、432 条描述符，每条 10 字可打印；`next_pa` 4 KB 对齐、链尾 0、`w9` 低 16 位 nband=1；落 arena 链首 `0x3016a000`；**sha1 与默认臂相同** | `D2_fuse1_stdout.txt`、`D4_plan_hw64.txt` |
| **D2b** 链就位探针（`BIN_V50_CHAIN_RTL=1`）| **PASS**：探针通过后按段提交；首段输入是 f32 ⇒ 打印一行原因后**整段回落逐 op**，sha1 不变 | `D2b_fuse1_chainrtl_wiring.txt` |
| **D3** `BIN_CONVPF_PL=1` 可回落 | **PASS**：`没有 overlay` / `overlay 里没有 fconv_0` 各一行原因；另用坏 fconv（抛异常 / 形状错 / 无 launch）三种假件验"逐位等于 CPU" | `D3_convpf_pl_stdout.txt`、`P5_fconv_fallback.txt` |
| **D4** 缓冲预算自检 | **PASS**：hw64 0/223 超预算（max 227 KB）；**hw256 18/223 超 270 KB 并标"不融合"**（S2,S9,S11,S13,S23,…），链只发 1269 条 | `D4_plan_hw64_256.txt` |
| **D5** 改动函数清单 | 见 §5 | `MD5_CHANGED.txt` |
| **D6** 不新增模块文件 | **PASS**：`ps/pynq` 文件清单与 main **逐名相同**（空 diff） | `D6_filelist_diff.txt` |
| **G0** 既有门禁未破 | `bin_engine --selftest` 全过；`check_bin_driver.py` 全过 | `G0_*.txt` |

## 2. P1~P6 完成度

| # | 内容 | 完成度 | 落点 |
|---|---|---|---|
| **P1** | `make_segment_plan()` → `SegmentPlan` | **100%**：分段规则与 M56 `segments.json` **逐 op 完全一致（223/223）**；每段带 op 列表/形状/halo/bands/缓冲/PRM/融合判定 | `bin_net.py: SegPlan/SegmentPlan/make_segment_plan` |
| **P2** | `prepare_segments()` → 描述符链 | **90%**：链的生成/落 arena/4 KB 步进/next_pa 链式全部完成并被 `v50_chain_store` 与 E1E2 整层一次 kick 覆盖；`prepare_segments` 增加 `n_desc/chain/pl_quant` 元数据。**缺口**：E1E2 的输出地址只有运行时才知道 ⇒ 逐带 `out` 字段在提交前重写（`SegmentPlan.write_segment_chain`），不是 prepare 期定稿 | `bin_engine.py: v50_*`、`E1E2Engine.chain_descs/submit_chain` |
| **P3** | `run_one()/kick()` 新语义 | **100%**：`run_one(chain=[...])` 只登记描述符不发 MMIO；`v50_submit_chain` 一次写 BAND_ROWS/FUSE_MODE/CHAIN_HEAD + start；`v50_poll_chain` 轮询 PROGRESS；**err 置位 ⇒ BinRunError + 清链 + poison（禁止第二次 kick）**；超时同样 poison 不重试 | `P3_chain_unit.txt`（4 项全过）、`P2P3_e1e2_onekick.txt`（cb=2 ⇒ **start 次数 1**，v5 是 2） |
| **P4** | `_exec_ps()` 只处理段边界 | **85%**：`_run_v50` 按段驱动（融合段一次提交整链，非融合段逐 op），段边界处物化 fan-out；`_exec_ps` 对"段内 op"**直接报错**（不许 PS 再驱动）。**缺口**：没有 `bin_segq` 时融合段必然回落（首段输入 f32 ⇒ 需先物化，故实际只在 RTL 到位后才真正跑链） | `bin_net.py: _run_v50/_v50_exec_segment/_v50_is_interior` |
| **P5** | `_fp_conv` 接 `FconvEngine` | **100%**：`BIN_CONVPF_PL=1` 走硬条件表 → `FconvEngine.launch` → 失败/形状错/异常**静默回落 CPU（一行原因）**；`fconv.py` **零改动**；新增 `convpf_pl_status()` 与 `convpf_c_status()` 并存 | `bin_net.py: _fp_conv/_convpf_pl_ok/_fp_conv_pl/convpf_pl_status` |
| **P6** | q8/qcache 对齐段计划 | **80%**：`prepare_segments` 在 `BIN_V50_PLQ8=1` 且链就绪时**跳过 CPU 量化**；qcache 键加 `plan_sig`（默认 None ⇒ 键不变）；`precomp` 在 PL 量化时绕过缓存。**q8.py 不需要改**（跳过点在调用处） | `bin_engine.py: prepare_segments`、`qcache.py: key/get_c8`、`bin_net.py: _q8_segment` |

## 3. PRM 参数帧（父代理追加的硬前提）

**打包位置找到了**：PRM 帧**不是** PS 侧的连续 16 字块 —— 它是 RTL 内部 `pw[0:PRM_W-1]`
寄存器行，由 `prm_addr(i,o0)` 从 DDR 的 **5 个逐 oc 连续数组**按 8 B 步长拉。
PS 侧的"打包"= `ps/tools/pack_bin_weights.py:388-395` 把
`alpha_q int16[cout] / bias_q int32[cout] / zeta_q int32[cout] / gamma_q int32[cout] / slope_q int16[cout]`
写进每层 blob（偏移记在 `index.json: off_alpha/off_bias/…`），读侧是
`bin_ref_numpy.Blob.params()` / `bin_engine.BinLayer`。

⇒ **PS 侧是按 VLANES 定义帧宽与段偏移的唯一位置**（新增 `bin_engine.prm_layout()`）：

| VLANES | PRM_W | alpha | bias | zeta | gamma | slope |
|---:|---:|---|---|---|---|---|
| 8 | **16** | [0,2) | [2,6) | [6,10) | [10,14) | [14,16) |
| 16 | **32** | [0,4) | [4,12) | [12,20) | [20,28) | [28,32) |

- **VLANES=8 时逐值等于 RTL 原硬编码 16 / 0,2,6,10,14**（`P6_prm_layout.txt` 第 1 行）。
- 公式：每 lane 字数 alpha=slope=V/4、bias=zeta=gamma=V/2 ⇒ `PRM_W=2V`。
- **容器字节不变**：DDR 数组是逐 oc 连续的，16 lane 只是每组多读 16 字；
  227 层 `cout∈{32,48,64,96,192}` 全是 16 的倍数 ⇒ **不需要 padding、不会读进相邻数组**。
  新增 `check_prm_capacity(L, vlanes)` 把这条变成硬校验（V=8 不触发 ⇒ 默认行为不变）。
- **段计划表示 PRM 帧宽**：每段 `prm_words = 2*VLANES`、`prm_bytes = ceil(c_out/V)*(2V)*4`，
  报告里打成 `prm=896B×17`（帧字节 × 每段的 PRM 装载次数 = bands，因为每带重新装载一次）。

## 4. 关键发现 / 风险（要 REVIEW 拍板）

1. ⚠⚠ **契约 A 与 v39 的 E1E2 寄存器块撞车**：`E1E2_REG` 的 `RUN_UNIT=0x200 /
   E_GROUP=0x204 / E_DESC=0x208 / E_DESC_HI=0x20C` 正好是契约 A 的
   `CHAIN_HEAD/PROGRESS/BAND_ROWS/FUSE_MODE`。已在 PS 侧做了互斥（`BIN_V50_FUSE=1`
   时不挂 E1E2、`select()` 抛错），但**RTL 侧必须给 E1E2 整体搬家**，否则两块功能互斥。
2. ⚠ **契约 B 的 w6/w7 是留白**（"cfg_group/…"）。本实现定：w6=`band_y0`、
   w7=flags（bit0 末带 / bit1 段输出物化 / bit2 含 shuffle / bit3 f32 输出）。
   **需要 REVIEW 把这两个字写进契约 B**，否则 IMP-B 的 `bin_segq` 会按别的语义解。
3. ⚠ **M56 的 `segments.json: buf_bytes` 与它自己的文档矛盾**：文档写 `×1 B`，
   JSON 实际是 **f32（4 B/element）**（223/223 段实测都是 4×）。本实现按契约 D4
   用 int8 算预算，并在报告里同时给 `halo_b`（int8）——**max 227 KB(64²) / 908 KB(256²)**。
4. ⚠ `BIN_V50_CHAIN_RTL=1` 是"人工承认 RTL 在位"的开关：FakePL 的寄存器会**回声**，
   探针无法区分真假 `bin_segq`。已在文档里写明；误开该开关 + 旧位流 = 数值错。
5. **deps_closure 需要补一行 DECL**：`bin_net` 新增 `import fconv`（懒加载），
   `deps_closure.py --check` 因此 rc=1（main 上是 rc=0）。**ps/tools 不在我的可写范围**，
   请在 `DECL` 里加：
   ```python
   "fconv": dict(kind="热/opt-in（BIN_CONVPF_PL=1 且有 fconv_0 时才用）",
                 role="convfp 的 PL 驱动（寄存器映射与 RTL 一一对应；IMP-C 只调用不改它）",
                 src=None),
   ```
   证据：`D6_deps_closure.txt`（唯一失败项就是 `['fconv']`）。
6. `BIN_V50_FUSE=1` 下 `pipeline` 走 `_run_v50`（串行按段）；`pipeline=False` 仍走 v5
   `_run_serial`（不建段计划收益，但数值等价）。

## 5. 改动函数清单（合并冲突评估用）

**`ps/pynq/bin_net.py`**（+863 行，md5 `4d5cd86ff6d13036d54f0d152603b31a`）
- 新增模块级：`_cpf_pl_note / convpf_pl_status / _to_dev`、`v50_on / v50_vlanes`、
  `V50_VPA_BASE / V50_SEGMAP_HINT`、`SegPlan`、`SegmentPlan`（`build_chain/materialize/
  write_segment_chain/dump_chain/sig/sig_of_op/boundary_of/report`）、
  `_load_segmap_hint`、`make_segment_plan`
- 改：`BinNet.__init__`（+9 个字段，默认全空）、`run()`（v50 段计划/链/分支，3 处插入）、
  `_exec_ps()`（段内 op 护栏）、`_fp_conv()`（P5 分支）、
  `_q8_segment()`（plan_sig）
- 新增方法：`_fconv_engine / _convfp_pl_ok / _fp_conv_pl`、
  `_v50_seg_of / _v50_is_interior / _v50_exec_one / _v50_exec_segment /
   _v50_chain_probe / _v50_materialize / _v50_setup / _v50_stats / _run_v50`

**`ps/pynq/bin_engine.py`**（+607 行，md5 `8f77733b2e35f91c7a6ef172877f3d8a`）
- 新增模块级：`V50_DESC_WORDS/STRIDE/BUDGET/CHAIN_REG/OP_BITS`、`_v50_env_on`、
  `v50_fuse_on/v50_chain_rtl_on/v50_pl_quant_on/v50_band_rows`、`vlanes_now`、
  **`prm_layout/prm_words_per_group/v50_prm_bytes/check_prm_capacity`**、
  `v50_desc/v50_chain_words/v50_flat_words/v50_chain_bytes/v50_chain_store/v50_format_chain`
- 改：`BinConvEngine.__init__`（+5 字段）、`BinConvEngine.kick()`（链在飞拒绝二次 kick）、
  `BinLayer._validate()`（`cout%VLANES` 护栏，仅 VLANES>8 生效）、
  `BinConvEngine.enable_e1e2()`（v50 互斥）、`E1E2Engine.select()`（0x200 护栏）、
  `E1E2Engine.run_one()`（`chain=` 参数）、`E1E2Engine._run_desc()`（整层一次 kick 分支）、
  `E1E2Engine.prepare_segments()`（P6 跳过 CPU 量化 + 链元数据 + precomp 绕过）
- 新增方法：`v50_chain_ready/why/submit_chain/poll_chain/wait_chain/reset_chain/chain_stats`、
  `_v50_strict_progress`、`E1E2Engine.chain_on/chain_descs/submit_chain`

**`ps/pynq/qcache.py`**（+17 行，md5 `95636f8cad44ca2434e978468a70b38e`）
- 改：`QuantLeafCache.key(data, fs_in, plan_sig=None)`、`get_c8(..., plan_sig=None)`

**未改**：`q8.py`、`fconv.py`、`bin_ref_numpy.py`、`ps/tools/*`、任何 RTL/`bd_*.tcl`/`build_final_*.tcl`。

## 6. 复现命令

```bash
cd worktrees/v50_impC
python3 ps/tools/verify_lazy_net.py --hw 64                                  # D1（默认）
BIN_V50_FUSE=1 BIN_V50_DUMP=1 python3 ps/tools/verify_lazy_net.py --hw 64    # D2（打印全链）
BIN_CONVPF_PL=1 python3 ps/tools/verify_lazy_net.py --hw 64                  # D3
python3 ../../runs/v50_impC/plancheck.py                                     # P1/P4 报告
python3 ../../runs/v50_impC/d4_plan256.py                                    # D4（64/256 预算）
python3 ../../runs/v50_impC/chain_unit.py                                    # P3 链语义
python3 ../../runs/v50_impC/e1e2_chain.py                                    # P2 一次 kick
python3 ../../runs/v50_impC/prm_check.py                                     # PRM 帧宽
```
（worktree 内 `deploy -> ../../pynq_specialized_code/deploy`、`pl -> .../pl` 两个软链，
只读复用，未改内容。）

## 7. 未覆盖 / 交给下游

- `bin_segq`（R4）未到位 ⇒ **融合段的链路没有一次真跑**；本报告只证明
  "链的生成/落址/提交/进度/出错即停"与"未就位时逐段回落"。
- 段输出的 C8 读回格式（契约 C 缺口）按"段尾 conv 的 `fs_out` + `read_out`"实现，
  待 SIM/REVIEW 定契约。
- 16-lane 只在 PS 侧参数化（PRM 帧 32 字 / `cout%VLANES` 护栏），未跑 xsim。
