# T-89 第二阶段报告：三边对齐 + 4 条缺口 + arena 窗口（2026-11-02）

## 0. 改动清单（md5 已刷新进 `runs/timing_v50/rtl_v50_manifest.txt`）

| 文件 | 改前 md5 | 改后 md5 |
|---|---|---|
| `lane16/bin_segq.v` | `191bda10…` | **`266454b8…`**（4 行） |
| `lane16/bin_unit_axi_wrap_v50.v` | `0229c186…` | **`0f7dd0ae…`** |
| `lane16/bin_band_fill_v50.v` | `1b0407bd…` | 未改（字段来源本来就对） |
| `ps/pynq/bin_engine.py` | `6a14b8b9…` | **`48548b8d…`** |
| `ps/pynq/bin_net.py` | `5759ed50…` | **`68343d5c…`** |
| `docs/V50_ARCHITECTURE.md` | `e848cdf1…` | **`c277bf8f…`**（§14 定稿 + §15 记录） |

## 1. 八步完成情况

| # | 步骤 | 结果 |
|---|---|---|
| 1 | `bin_segq.v` 4 行 | ✅ 240/244/340/347 的 `dw[1][31:0]`→`dw[0][63:32]`，E_NULL 删掉 `flags≠0`；**重跑四条 FSM 路径 28 ok / 0 fail**（旧 RTL 对照 22/6） |
| 2 | `OP_MASK_SUP = 0x3` | ✅ |
| 3 | 影子描述符 ③ | ✅ `sh_dw[0:4]+sh_beat`（S_HDR 5 拍），`band_words`/填充器/conv 几何全吃影子 |
| 4 | arena 窗口 | ✅ PS 必填 `arena=(lo,hi)` 写 `0x318/0x31C`；RTL 默认值 ⇒ **拒绝启动**（kick 门控 + `u_err=E_RANGE`） |
| 5 | 段边界寄存器 ① | ✅ `cout/k/s/d/pad` 走 `0x70/0x40/0x48/0x50/0x58`，PS 在 `CHAIN_HEAD` 之前写（每段一次） |
| 6 | `band_words` 相位化 ② | ✅ LOAD=`⌈cin/64⌉·rows·W` / DRAIN=`⌈cout/8⌉·nband·W` / CHK=max |
| 7 | 三边按 §14 对齐 | ✅ 另加两条**正确性门禁**（见 §3） |
| 8 | 门禁 | ✅ 全绿（见 §2） |

## 2. 门禁实测

| 判据 | 结果 | 日志 |
|---|---|---|
| segq 四条 FSM 路径 + 3 条新判据 | **28 ok / 0 fail** | `sim/log_NEW.txt`、`sim/xsim_NEW.log` |
| 同 TB 打旧 RTL（证"改前失败"） | 22 ok / **6 fail**（out_pa 落地、flags=0、越界 E_RANGE 三条各 2） | `sim/log_OLD.txt` |
| `xvlog -sv`（15 个文件） | rc=0，**0 error / 0 warning** | `elab/v8/xvlog.log` |
| `xelab VLANES=8` / `VLANES=16` | **Built**（`wrap_v8` / `wrap_v16`） | `elab/v8/xelab.log`、`elab/v16/xelab.log` |
| `FUSE=0` 结构不变 | 全部 `sel_fuse_q ? A : B` 的 **B 分支逐字节未动**；唯一改的 `u_err` 只在 A 侧加 `arena_err` | `/tmp/wrap_t89.diff` 分类 |
| `verify_lazy_net --hw 64`（`taskset -c 6-7`） | sha1 = `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd`（冻结值）；改前 vs 改后 **归一化 stdout 0 差异** | `v3/before.txt`、`v3/after2.txt` |
| `FUSE=1`（FakePL 无链引擎 ⇒ 自动回落） | sha1 与 FUSE=0 **逐位相同** | `v3/fuse1_plan.txt` |
| PS 侧定向单测 | **0 error** | `ps_unit_test.py` |
| `verify_rtl_root` | **MATCH=35 DIFF=0 EXTRA=0 MISSING=0 PASS** | 见下 |

## 3. 我加的两条"防静默错"门禁（不在原 4 条缺口里，但不加就会静默错数）

1. **单级约束**：lane16 包装层**没有带内级联序列器**（对比 IMP-B2 的 `seq_stage`），
   一个带只起一次 conv；若把 `conv→cat` 段判成融合，**cat 会被静默跳过**。
   ⇒ PS 只融合 `len(ops)==1 && ops[0].kind=="conv"` 的段。
   实测 223 段里 **融合 90 段 / 140 条描述符**，其余 133 段走 v39 逐 op。
2. **k=1 锁**：`cfg_h` 双用途（§14.7），`k≠1` 段不融合（PS 判 + RTL `$error` 双保险）。

## 4. 越界 `out_pa` 是否报 `E_RANGE`

**是。** segq TB 用例 `(7)`：`out_pa=0x8000 > arena_hi=0x4000`
⇒ 新 RTL：`err=1 / err_code=2 (E_RANGE)`；
⇒ 旧 RTL：**不报**（它拿 `cin=8` 做 in_arena，越界的 out_pa 根本没被检查），链还跑完了。
这一条是"改前必失败、改后必通过"的定向反证。

## 5. `geo_goutw` 更正（我原来的写法 + 实测数值）

```verilog
// 改前：ceil(cout/VLANES) * (VLANES/8)   ← VLANES=8 恰好等价，16-lane 会少一半
// 改后：
wire [31:0] geo_goutw = ({16'd0, c_cfg_cout} + 32'd7) >> 3;   // ceil(cout/8)
```

| cout | 8-lane 排空字/像素 | 16-lane 排空字/像素 | 旧口径在 V16 会得 |
|---|---|---|---|
| 32 | 4 | 4 | 2（一半） |
| 48 | 6 | 6 | 3（一半） |
| 64 | 8 | 8 | 4（一半） |
| 96 | 12 | 12 | 6（一半） |
| 192 | 24 | 24 | 12（一半） |

**同类自查（装侧）**：`geo_loadw` 里的 `⌈cin/64⌉` 的 "64" 是 **act 二值位流的通道/字**
（1 bit/通道 ⇒ 64 通道/64-bit 字，与 `bin_conv_core_v50.v:158` 的 `cb_c=(cin+63)>>6` 同口径），
**不是 lane 数**，也与 VLANES 无关 ⇒ 这一处没有同类问题。
排侧的 "8" 是 **int8 的通道/字**。两者的区别是"每通道几 bit"，不是 lane 数。

## 6. 四条 FSM 路径重跑结果

```
tag=NEW (md5 266454b8…)  PASS_CNT=28  FAIL_CNT=0   B3 PASS
  ① 推进 progress==2 / ② 链尾 done 等 wr_all_idle / ③ 出错即停 E_ALIGN,E_RANGE,E_CANARY / ④ 重启
  + out_pa 落地（st_wa 0x200..0x218）/ flags=0 不误判 / 越界 out_pa ⇒ E_RANGE
tag=OLD (md5 191bda10…)  PASS_CNT=22  FAIL_CNT=6   B3 FAIL
  st_n=8  st_wa 0x08..0x20  ← 输出真的写到 cin 了
```

## 7. 还剩几条阻断 → 能不能出位流

- **位流能出**：结构是 v39 基线 + 新增链通路，`FUSE=0` 路径逐位不变（已证），
  `xvlog` 0 err、`xelab` 两个 VLANES 都 Built。
- **`FUSE=1` 不能上板**，还差三条：
  1. **带级联缺集成仿真**（segq 单模块过了，`fill→conv→band→drain` 没有 TB）；
  2. **级联序列器缺失**（要真吃 `conv→cat`，得移植 IMP-B2 的 `bin_band_seq.v`，45 条段）；
  3. `k≥3` 不可融合（`cfg_h` 双用途，要改核或改契约）。
