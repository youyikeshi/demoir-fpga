# T-87 REPORT —— 契约 R3（§12）落到代码：magic、`0x310` 空闲误报、`H_in`、`w0` 二义

2026-11-02 · 子代理 **T-87** · 仓库 `HEAD=b647ee2`（本节代码未 commit）
产物目录 `runs/v87_r3/` · 契约源 `docs/V50_ARCHITECTURE.md` §12（R3）

---

## 0. 一句话结论

四件事全部落进代码并各自有**可复算的证据**：magic 两边现在都是 `0x5030_0001`；
`0x310` 复位读值 **`0x0403` → `0x00000000`**（xsim 正负对照各一遍）；描述符 `w3`
高 16 现在逐条等于该段 `H_in`（387/387）；`w0` 的二义在 PS 里写成了注释 + 拒绝路径。
`R1/R2/R3/R4` 全绿。

---

## 1. 改动清单（改前 / 改后行号，`git diff -U0` 口径）

| # | 文件 | 改前 | 改后 | 内容 |
|---|---|---|---|---|
| 12.1 | `ps/pynq/bin_engine.py` | L2327 `V50_CHAIN_MAGIC = 0x7635_5000` | **L2372** `V50_CHAIN_MAGIC = 0x5030_0001`（+ L2366–2371 注释） | 常量改值；`BIN_V50_CHAIN_MAGIC` 覆盖与"读不到不走链"**未动** |
| 12.1 | 同上（注释同步） | L1817–1818 / L1821 / L1844 / L2302 | L1817–1818 / L1821 / L1859–1860 / L2335–2336 | 四处 `0x7635_5000` 文字改 `0x5030_0001` |
| 12.2 | `rtl_v50/lane16/bin_band_buf.v` | L167（`shape_bad` 后直接进粘滞块）<br>L318 `if (shape_bad \|\| rd_range_bad \|\| …)` | **L169–185** 新增 `wire cfg_valid = (cfg_c!=0) && (cfg_w!=0)` + 机理注释<br>**L336–339** `if (cfg_valid && (shape_bad \|\| …)) cap_err_q <= 1'b1` | `cap_err` 在"形状未装载"时不许置位 |
| 12.2 | 同上（端口注释） | L83 `cap_err` 一行注释 | L83–84 | 补"§12.2：未装载不置位" |
| 12.2 | `ps/pynq/bin_engine.py` | L1830–1837 `def v50_chain_status(self)` | **L1830–1852** `def v50_chain_status(self, pre_kick=False)`（`pre_kick` 把 `err/code` 压 0，`err_raw` 只留痕）<br>**L1933–1949** `v50_submit_chain` 的 kick 前自检：**只看 busy**，读不到就跳过 | PS 侧"kick 前不读 err"变成机制 |
| 12.3 | `ps/pynq/bin_engine.py` | L2449–2456 `v50_desc(… in_pa=0)`，`w3 = cfg_h` | **L2494–2520** 新增 `h_in=0` 形参，`w3 = (cfg_h & 0xFFFF) \| (h_in<<16)` | 描述符字 3 高 16 = `H_in` |
| 12.3 | `ps/pynq/bin_net.py` | L1206–1254 `v50_seg_descs()` | **L1218–1232** 契约注释 + `h_in<=0 ⇒ raise`；**L1265** `h_in=h_in` | `H_in` 取自 `seg.h_in`（见 §3） |
| 12.5 | `ps/pynq/bin_engine.py` | L2308 `w0 desc 本带输入基址` | **L2342–2346** 二义注释（FUSE_MODE=1 ⇒ 段输入 DDR 源基址 / =0 ⇒ 旧语义）<br>**L2498–2505** `v50_desc` docstring 同义 | 二义写进代码 |
| 12.5 | `ps/pynq/bin_net.py` | L1195–1214 区域 | **L1221–1224** 注释、**L2610–2616** `_v50_exec_segment` 注释 | 说明 `src.pa` 就是源基址、**不许**预加带偏移 |
| 12.3 | `rtl_v50/lane16/bin_unit_axi_wrap_v50.v` | L1338 `wire fuse_m = …` | **L1339–1354** 新增"§12.3 核对"注释块 + **L1354** `wire seg_h_in = seg_h`（命名点） | 高 16 通路核对 + 冲突登记（见 §4） |
| — | `runs/board_ops/20261102/deps_closure.txt` | — | 22 行 | `merge_gate.sh` ①`--gen` 的**设计行为**（重生成清单） |

**没动的**：25 条基线 RTL、`bin_conv_core_v50.v`、`bin_segq.v`、`bin_band_seq.v`、
`bin_band_fill_v50.v`、`ps/tools/*`、BD/tcl、`proj_engine`、`proj_v43base2`、
`bin_unit_axi_wrap_bd_v50.v`（md5 与 T-83 交付一致：`0ba1aae3249c4aca59b859ce50028804`）。

`git diff --stat`：4 个源文件 +（清单）+（我先前的 `runs/resguard/resguard.log` 脏文件，非本轮引入）。

---

## 2. R3-12.2 的 xsim 证据（正负对照，同一 TB 同一激励）

TB：`runs/v87_r3/tb_v87_r310.v`（AXI-Lite 单拍事务；m_axi 主口钉"永不受理"）。
一条 `run.sh`：`xvlog` 一次 → `xelab` 三次（`bin_unit_axi_wrap_bd_v50` VLANES=8/16 + TB）→ `xsim` 一次。

```
xvlog -sv  ：rc=0   ERROR 0 行   WARNING 0 行
xelab      ：bin_unit_axi_wrap_bd_v50 VLANES=8  Built=1
             bin_unit_axi_wrap_bd_v50 VLANES=16 Built=1
             tb_v87_r310                       Built=1
```

| 时间点 | 改前（`git show HEAD:` 的 `bin_band_buf.v`） | 改后 |
|---|---|---|
| 复位后 `0x310` | **`0x00000403`**（done=1, **err=1**, code=**4=E_CANARY**） | **`0x00000000`** ✔ |
| 写配置、未 kick | `0x00000403` | `0x00000000` ✔ |
| kick 后 64 拍 err 位累计命中 | **64/64 拍** err=1 | **0/64 拍** ✔ |
| `0x314 CHAIN_ID` | `0x5030_0001` | `0x5030_0001`，写 `0xDEADBEEF` 后仍 `0x5030_0001` |
| 再复位 | `0x00000403` | `0x00000000` ✔ |

日志：`xsim_stdout.log`（改后 `== SUMMARY: PASS (10 checks) ==`）、
`sim_before/xsim_stdout.log`（改前 `3 pass / 7 fail`）。

> ⚠ **本轮最值钱的一条副产物**：改前不只是"空闲态误报"——**kick 之后每一拍 err 都是 1**
> （64/64）。也就是说按旧 RTL，任何一条链在 kick 后 1~2 拍就被假 `E_CANARY` 打死，
> 融合通路**根本跑不起来**。⇒ 12.2 不是"报告不干净"，是**功能阻断**。
> 这同时否掉了"用 kick 做门控"的写法（kick 后头几十拍形状字段仍是 0，用 kick 门控
> 会把这段瞬态重新判成错）⇒ 门控必须取"形状字段已装载"（`cfg_valid`）。

---

## 3. `H_in` 从哪来（12.3）

```
bin_net.make_segment_plan()  L1606  c_in, h_in, w_in = ops[0].deps[0].shape
                            L1659  SegPlan(h_in=h_in, …)
        ↓
bin_net.v50_seg_descs()     L1226  h_in = int(seg.h_in)   ← 本段**输入张量高度**
                            L1228  h_in<=0 ⇒ ValueError（不许静默填 0）
                            L1265  BE.v50_desc(..., h_in=h_in)
        ↓
bin_engine.v50_desc()      L2506  w3 = (cfg_h & 0xFFFF) | ((h_in & 0xFFFF) << 16)
```
RTL 侧（静态核对，非假设）：`bin_segq.v:324 f_h <= dw[1][63:48]` ← **w3 高 16**；
`:325 f_w <= dw[1][47:32]` ← w3 低 16；`:190 d_h = f_h` ⇒ 包装层 `seg_h` = `H_in`。
即**高 16 的 H_in 通路本来就在位且取对了**（IMP-B2 的 filler 端口 `h_img` 注释亦为
"字3 高16：图像高 H_in ⇒ pad 判据"）。

逐描述符判据 `runs/v87_r3/check_desc_w3.py`（跑真 `net.run()`，不在 FakePL 上打补丁）：

```
整网 hw=64：223 段 / 387 条描述符
检出 387 条描述符；w3 高 16 出现过的 H_in 值 = [2, 4, 8, 16, 32, 64]
违规 0 条          == 总结：PASS ==
```
（同时校验 `w3 低 16 == nband == w9 低 16`、`w0 != 0`。）

> 曾加过的候选判据 **"nband ≤ H_in" 是错的，已删除并留注释**：网里有 8 条
> `nband=8 > H_in∈{2,4}` 的描述符（interp 段 h_in=4 → h_out=8 的正常上采样）。
> 高低 16 弄反由 `hin != seg.h_in` 逐段精确抓，不需要经验不等式。

---

## 4. ⚠ 未决冲突（**必须由主代理裁定**，我没有擅自改）

**`w3` 低 16 的定义，两处 RTL 不一致：**

| 来源 | 低 16 | 高 16 |
|---|---|---|
| 契约 R3 §12.3（本轮执行） | `cfg_h`/nband | `H_in` |
| PS `v50_desc`（本轮已按 R3 改） | `cfg_h`/nband | `H_in` |
| IMP-B2 `bin_band_fill_v50.v` L24 注释 + 端口 `w_img` | **`W`（行步长）** | `H_in` |

IMP-B2 的 filler 三角色分工是 `H_in`(pad 判据) / `W`(行步长) / `nband`(w9 低 16)；
契约 R3 把 `W` 放在 `w4 cfg_w`、把 `w3` 低 16 留给 `cfg_h`。**两者不可能同时对**。
影响面：只影响 band 填充器（本 `lane16` 树里 **filler 尚未例化**，所以本轮
`xvlog/xelab/xsim/R1/R2` 都碰不到它），T-88 把 IMP-B2 移植进来时必须先裁定。
已在该冲突点写了注释（`bin_unit_axi_wrap_v50.v:1349–1352`）。

**另一个同类问题（本轮未动，登记）**：本树 `lane16` 的包装层把 `seg_h`(=H_in)
直接喂给 conv 的 `c_cfg_h`（IMP-B 的接线，`:1345`）。band 模式下 conv 要的是
"带内行数"，不是整图高。IMP-B2 已把它改成 `seg_nband`（`w9` 低 16）。
这属于 T-88 的移植范围，我**没有**改——因为本树 band 数据通路从未做过端到端仿真
（`v50_impB` 只证了 `fuse_m=0` 的 v39 等价性），改它等于在没有判据的情况下猜。

---

## 5. 验收

| # | 判据 | 结果 | 证据 |
|---|---|---|---|
| **R1** | `BIN_V50_FUSE=0`，`verify_lazy_net.py --hw 64`（`taskset -c 6-7`，**未**设 `OMP_NUM_THREADS`、**未**用单核 taskset） | **PASS**。A/B/C 三个 sha1 全 = `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd`，与冻结基线**逐字节一致**，stdout 0 差异 ⇒ **PASS（4.0 s）** | `R1_after_fuse0.log`（改前基线 `R1_before_fuse0.log` 同值） |
| **R2** | `BIN_V50_FUSE=1` | 链 **387 条**、223 段、链首 `0x50800000`、1.59 MB **未变**；magic 检查**打出了它读到的值**：`期望 0x50300001 读回 0x00000000 ⇒ 没有链引擎 ⇒ 回落 v5`（FakePL 不实现 `0x314`，所以"读不到就不走链"是**正确行为**）；三条 sha1 仍全中 ⇒ PASS | `R2_after_fuse1.log` |
| **R2′** | magic **两边一致**的正面证明（FakePL 喂 RTL 常量） | `check_magic_ps.py` **15/15 PASS**：`PS 常量 == RTL 常量 == 0x5030_0001`；喂 `0x50300001 ⇒ chain_ready True`；喂 `0x0 / 0x76355000 / 0x50300002 ⇒ False` 且理由里带读回值；`BIN_V50_CHAIN_MAGIC` 仍可覆盖；`FUSE=0 ⇒ 一律 False` | `check_magic_ps.py` 输出 |
| **R3** | RTL 编译/elab | `xvlog -sv` **0 error / 0 warning**（23 基线 + 9 lane16）；`xelab` `bin_unit_axi_wrap_bd_v50` **VLANES=8 与 16 都 Built**；TB Built | `xvlog.log` / `xelab_vlanes8.log` / `xelab_vlanes16.log` |
| **R3** | xsim 验"复位后 `0x310` 的 err 位为 0" | **`0x310 = 0x00000000`**（改前 `0x00000403`），10 checks 全 PASS；12.2 的 PS 机制另有 `check_kick_gate.py` **12/12 PASS** | `xsim_stdout.log` / `sim_before/xsim_stdout.log` |
| **R4** | `merge_gate.sh` 三关 | ①`--gen` rc=0 ✓ ②`md5sum -c` rc=0（20 行 / 0 失败）③`--check` rc=0（14 模块 / 20 文件）⇒ **MERGE_GATE PASS（3/3）** | `R4_merge_gate.log` |
| **R5** | 每条改动"改前/改后"行号 | 见 §1 表（`git diff -U0` 口径） | 本文件 |

---

## 6. 已知风险 / 未覆盖

1. **band 数据通路在本树仍不可端到端仿真**（无 filler / 无 `band_seq` 例化）⇒
   `w3` 语义、`cfg_h` 接线、`band_words` 的真实行为只有**静态**结论，需 T-88 移植后
   用 band-mode TB 补。
2. kick 前 busy 自检**每段多 1 次 MMIO 读**（223 段 ≈ +5.6 ms @25 µs/读，占 8 s 的
   0.07%）；读失败只跳过、不挡提交（`check_kick_gate.py` ④）。
3. `cfg_valid` 让"`cfg_c==0`/`cfg_w==0`"不再由 `cap_err` 报 ⇒ 该错误改由 `bin_segq`
   在 `S_CHK` 报 `E_SIZE`（`band_words==0` / `w9[15:0]==0`）⇒ 覆盖**没丢**，但**报的码
   从 `E_CANARY(4)` 变成 `E_SIZE(5)`**：上层/脚本若按 code 分支要知情。
4. `0x310` 的 `err_code` 现在在"真超时"之前恒 0；本次 TB 用"m_axi 永不受理"验证
   了 64 拍内不误报，但 TB 的 2 ms 超时窗口不足以看到 `E_TIMEOUT` 收敛，未验。

---

## 7. 产物

```
runs/v87_r3/
├── REPORT_v87_r3.md          ← 本文件
├── run.sh                    ← xvlog / xelab(8,16,TB) / xsim 一键（一次一个工具，串行）
├── tb_v87_r310.v             ← §12.1+§12.2 的 xsim TB（10 checks）
├── check_magic_ps.py         ← §12.1 PS 判据（15 checks，只读仓库代码）
├── check_desc_w3.py          ← §12.3 逐描述符判据（387/387）
├── check_kick_gate.py        ← §12.2 PS 机制判据（12 checks）
├── R1_before_fuse0.log / R1_after_fuse0.log
├── R2_before_fuse1.log / R2_after_fuse1.log
├── R4_merge_gate.log
├── xvlog.log / xelab_vlanes8.log / xelab_vlanes16.log / xelab_tb.log
├── xsim_stdout.log / xsim.log
├── before_rtl/bin_band_buf.v ← `git show HEAD:` 的原件（负对照用，md5 daff1dd7…）
└── sim_before/               ← 用原件重编重跑的负对照（xvlog/xelab/xsim 日志）
```
