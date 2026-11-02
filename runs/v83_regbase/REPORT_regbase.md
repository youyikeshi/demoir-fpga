# T-83 REPORT — V50_REG_BASE 默认值修正 + BD 外壳路径寄存器落点验证

2026-11-02 · 子代理 T-83 · 产物目录 `runs/v83_regbase/`

---

## 1. 改了什么（**全工程唯一一处**）

| 项 | 内容 |
|---|---|
| 文件 | `runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_bd_v50.v` |
| 行 | **29** |
| 改前 | `    parameter int V50_REG_BASE   = 16'h02E0` |
| 改后 | `    parameter int V50_REG_BASE   = 16'h0300` |
| diff | 1 行 / 1 个 token（见 `bin_unit_axi_wrap_bd_v50.v.before`，`diff -u` 只有这一行） |
| md5 | `8309b8e8456f592a350c0b0160d3eb70` → `0ba1aae3249c4aca59b859ce50028804` |

**方案选了"改默认值"**，理由（已核）：

```
$ grep -n "CONFIG\|create_bd_cell.*bin_unit" pynq_specialized_code/pl/scripts/bd_v50.tcl
130:create_bd_cell -type module -reference bin_unit_axi_wrap_bd_v50 bin_unit_0
$ grep -rn "V50_REG_BASE|defparam|0x2E0" bd_v50.tcl build_final_150v50.tcl
(无命中)
```

⇒ `bd_v50.tcl` **从未** `set_property CONFIG.V50_REG_BASE`，也没有 `defparam`
⇒ BD 综合期拿到的就是这个参数默认值 ⇒ 只有改默认值才能真正修好，改 tcl 是锦上添花（本任务按要求二者取一，选默认值）。
**没有改任何逻辑、端口、X_INTERFACE 属性、tcl。**

---

## 2. 静态追踪：`V50_REG_BASE` → 地址译码（逐级表达式）

> 标注：本节是**静态追踪**，不是仿真；仿真证据在 §3。

```
① bd_v50.tcl:130
     create_bd_cell -type module -reference bin_unit_axi_wrap_bd_v50 bin_unit_0
     （无 CONFIG 覆盖）
        ↓
② bin_unit_axi_wrap_bd_v50.v:29   (L29 = 本次改的那一行)
     parameter int V50_REG_BASE = 16'h0300
        ↓  L126
     .V50_REG_BASE (V50_REG_BASE)
        ↓
③ bin_unit_axi_wrap_v50.v:383
     parameter int V50_REG_BASE = 16'h0300
        ↓  L481-488（localparam，case 项必须是常量才成立）
     RW_CHAIN = V50_REG_BASE[9:0]      = 10'h300   // CHAIN_HEAD
     RW_PROG  = RW_CHAIN + 10'd4       = 10'h304   // PROGRESS   (RO)
     RW_BROWS = RW_CHAIN + 10'd8       = 10'h308   // BAND_ROWS
     RW_FUSE  = RW_CHAIN + 10'd12      = 10'h30C   // FUSE_MODE
     RW_STAT  = RW_CHAIN + 10'd16      = 10'h310   // CHAIN_STATUS (RO)
     RW_MAGIC = RW_CHAIN + 10'd20      = 10'h314   // CHAIN_ID   (RO 常量)
     RW_ALO   = RW_CHAIN + 10'd24      = 10'h318
     RW_AHI   = RW_CHAIN + 10'd28      = 10'h31C
        ↓
④ 总线侧译码（写 / 读对称）
     写：bin_unit_axi_wrap_v50.v:842  wire wr_page = wr_addr[16];      // 1 = 配置页
         :928  if (wr_addr[9:8] == 2'b00) … 0x00..0xFF 旧配置页
         :983  else case (wr_addr[9:0])    … 0x100..0x3FF 扩展页 ⇒ RW_* 在这里
     读：:2897  if (araddr_l[16]) / :2899 if (araddr_l[9:8]==0) / :2940 case (araddr_l[9:0])  ⇒ RW_* 在这里
        ↓
⑤ PS 侧地址（驱动约定 = 配置页 +0x10000，见 k1_engine.py CTRL_PAGE_OFF）
     A = 0x4006_0000 (bin_unit_0 的 BD 基址, 128K) + 0x1_0000 (A[16]=1) + off
     基址低 16 位 = 0 ⇒ A[9:0] == off，A[16] == 1（两种互联语义同解）
     ⇒ 0x4007_0300 … 0x4007_0314 就是 0x300 … 0x314
```

**0x250..0x25C 的路径**：`A[9:8] = 2'b10` ⇒ 落进扩展页 `case (A[9:0])`，该 case 只有
`10'h200/204/208/20C/210/214/230/234/238/23C/240` 与 `RW_*`（0x300/304/308/30C/310/314/318/31C）
⇒ `default:`（读 `rd_mux = 0`，写 `default: ;` 什么都不写）⇒ **读 0、写忽略**。
`0x2E0..0x2F4` 同理，改默认值后那里已经是 `default:` 空白区。

---

## 3. xsim 证据（**最强证据**）

### 3.1 环境与复现

```bash
cd runs/v83_regbase
./run.sh xvlog    # xvlog -sv tb_bd_v50_regbase.v <25 基线(剔 _selftest_)> <lane16 9 个>
./run.sh xelab    # xelab -debug typical tb_bd_v50_regbase -s regbase
./run.sh xsim     # xsim regbase -runall -log xsim.log
```

日志：`xvlog_pos.log` / `xelab_pos.log` / `xsim_pos.log`（0 个 ERROR）。
TB：`tb_bd_v50_regbase.v` —— 例化 **`bin_unit_axi_wrap_bd_v50`（不加任何参数覆盖）**，
即"BD 外壳 + 默认参数"这条路径本身；主口全钉死（不启动链），只从 `s_axi` 从口发单拍事务。

### 3.2 参数确实传到了译码常量（仿真里读层次）

```
PROBE shell  u_dut.V50_REG_BASE      = 16'h0300
PROBE unit   u_bin_unit.V50_REG_BASE = 16'h0300
PROBE unit   RW_CHAIN / RW_PROG      = 10'h300 / 10'h304
PROBE unit   RW_BROWS / RW_FUSE      = 10'h308 / 10'h30c
PROBE unit   RW_STAT  / RW_MAGIC     = 10'h310 / 10'h314
```

### 3.3 六个地址（每个都发两种地址写法：全地址 / 窗口偏移，结果必须同值）

| PS 绝对地址 | 偏移 | 寄存器 | 读回 | 判定 |
|---|---|---|---|---|
| `0x4007_0300` | `0x300` | CHAIN_HEAD | 复位 `0x00000000` → 写 `0xDEADBEEF` → 读 `0xDEADBEEF` | **可读写 ✔** |
| `0x4007_0304` | `0x304` | PROGRESS | `0x00000000`；写 `0xFFFF_FFFF` 后仍 `0x00000000` | **只读 ✔** |
| `0x4007_0308` | `0x308` | BAND_ROWS | 复位 `0x00000008`(=参数 8) → 写 5 → 读 `0x00000005` | **可读写 ✔** |
| `0x4007_030C` | `0x30C` | FUSE_MODE | 复位 `0x00000000` → 写 1 → 读 `0x00000001` | **可读写 ✔** |
| `0x4007_0310` | `0x310` | CHAIN_STATUS | `0x00000403`；写 `0xFFFF_FFFF` 后**不变** | **只读 ✔**（值见 §7） |
| `0x4007_0314` | `0x314` | CHAIN_ID | **`0x50300001`**；写 `0` / 写 `F` 后仍 `0x50300001` | **只读常量 `0x50300001` ✔** |

```
 RESULT: PASS=34 FAIL=0
 RESULT: PASS
```

`0x314` 读回 **`0x50300001`** —— PS 侧"链引擎在位"判定（`BIN_V50_CHAIN_RTL=1` 探针）现在成立，
且它不可能被"寄存器回声"骗过（写 `0x0000_0000` / `0xFFFF_FFFF` 都读回 `0x50300001`，已实测）。

### 3.4 负对照（证明这份 TB 真的能抓到平移，不是"空过"）

`tb_bd_v50_regbase_neg.v` 与正 TB 逐字相同，**唯一差别**是把参数显式覆盖回旧默认 `16'h02E0`：

```
 PROBE shell V50_REG_BASE = 16'h02e0
 PROBE unit  RW_CHAIN     = 10'h2e0
 PROBE unit  RW_MAGIC     = 10'h2f4
   0x2E8 BAND_ROWS   = 0x00000008      <= 整块寄存器确实被平移到 0x2E0 段
   0x2F0 CHAIN_STATUS= 0x00000403
   0x2F4 CHAIN_ID    = 0x50300001      <= bug signature
   0x314 CHAIN_ID    = 0x00000000      <= T-82 报的"永远判不在位"
 RESULT: PASS=5 FAIL=0
 RESULT: PASS (negative control reproduced the bug)
```

⇒ 改前 0x314 读 0、改后读 `0x50300001`，且两者都在**同一条 BD 外壳路径**上复现 —— 因果闭合。

---

## 4. `0x250..0x25C` 是否仍为空 → **是**

- `0x250 / 0x254 / 0x258 / 0x25C` 读：全部 `0x00000000`（4/4 PASS）
- 往这四个地址写 `0xA5A5_5A5A` / `0xFFFF_FFFF` / `0xFFFF_FFFF` / `0xFFFF_FFFF` 后：
  再读仍全部 `0x00000000`（4/4 PASS），且 `0x300` 的 `0xDEADBEEF` 未被扰动（PASS）
- `0x2E0 / 0x2E4 / 0x2E8 / 0x2EC / 0x2F0 / 0x2F4` 读：全部 `0x00000000`（6/6 PASS）

---

## 5. `verify_rtl_root.sh`（严格口径）

```
$ bash pynq_specialized_code/pl/scripts/verify_rtl_root.sh \
        runs/timing_v50/rtl_v50_manifest.txt runs/timing_v50/rtl_v50
...
   MATCH    lane16/bin_unit_axi_wrap_bd_v50.v  0ba1aae3249c4aca59b859ce50028804
MATCH=34 DIFF=0 EXTRA=0 MISSING=0
ALLOWED_EXTRA=0  V_TOTAL=34  MANIFEST_RTL=34
RESULT: PASS        (rc=0)
```

- 25 条基线全部 MATCH（**基线一个字节没动**）
- 只刷新了 manifest 里被我改的**那一条** md5（`runs/v83_regbase/rtl_v50_manifest.txt.before` 留证），
  并按该文件既有惯例在头注加 3 行 T-83 说明（`md5sum` 刷新本就写在头注第 5-7 行）
- 日志：`runs/v83_regbase/verify_rtl_root.log`

---

## 6. 产物清单（`runs/v83_regbase/`）

| 文件 | 内容 |
|---|---|
| `tb_bd_v50_regbase.v` | 正 TB（34 项判定） |
| `tb_bd_v50_regbase_neg.v` | 负对照（参数覆盖回 0x02E0） |
| `run.sh` / `run_neg.sh` | 复现脚本（xvlog/xelab/xsim 一次一个） |
| `xvlog_pos.log` `xelab_pos.log` `xsim_pos.log` | 正流程日志（0 ERROR） |
| `xvlog_neg.log` `xelab_neg.log` `xsim_neg.log` | 负对照日志 |
| `bin_unit_axi_wrap_bd_v50.v.before` | 改动前的整文件（取证） |
| `md5_before.txt` / `md5_after.txt` | 该文件改动前后 md5 |
| `rtl_v50_manifest.txt.before` / `verify_rtl_root.log` | manifest 改前副本 + 严格校验日志 |

---

## 7. ⚠ 附带发现（**不在本任务范围，未动任何逻辑**，请转 IMP-B / 主代理裁定）

**`0x310 CHAIN_STATUS` 在"还没 kick"的空闲态读回 `0x00000403`（不是 0）。**

按 RTL 定义 `{16'd0, err_code[7:0], 5'd0, busy, err, done}` 解码：

| bit | 名字 | 实测 |
|---|---|---|
| 0 | done | **1** |
| 1 | err | **1** |
| 2 | busy | 0 |
| 15:8 | err_code | `0x04` = `E_CANARY` |

机理（静态 + 仿真一致）：

```
bin_band_buf.v:165  shape_bad = (cfg_c == 16'd0) || … || (cfg_w == 16'd0) || …
                    复位后 cfg_c=cfg_w=0（还没配置）⇒ shape_bad 恒 1
bin_band_buf.v:318  if (shape_bad | …) cap_err_q <= 1'b1;   // 粘滞，只在 buf_reset 清
bin_unit_axi_wrap_v50.v:1612  band_bad = band_cap_err | band_canary_err | band_arb_conf
                              :1677  .canary_bad(band_bad)   ← 直接进 segq
bin_segq.v:437      if (canary_bad && (st != S_ERR)) st <= S_ERR, err_q_code <= E_CANARY
                    S_ERR: err_q <= 1, done_q <= 1
⇒ 0x310 = {0, 0x04, 0, busy=0, err=1, done=1} = 0x0403（每拍重新锁存，永久如此）
```

**影响**：只要 PS 在 kick 之前读 `0x310` 并判 `err`/`done`，就会误判"链已出错/已结束"；
kick 本身会把 `err_q/done_q` 清零（`bin_segq.v:270-273`），所以 **kick 之后行为正常**。
`0x314`（本任务的主判据）不受影响。
**建议**（不属我改）：`shape_bad`/`cap_err` 在"未开工"时不该计入 `band_bad`，
或在 `seg_band_abort | start_fire` 的 `buf_reset` 之外再加"kick 时清 cap_err"。
本任务铁规是"不许改逻辑"，故只报告，不改。

---

## 8. 一句话结论

`V50_REG_BASE` 默认值 `0x02E0 → 0x0300`（1 行），BD 外壳路径下 6 个寄存器全部落在
`0x300..0x314`（`0x314` 读回 `0x50300001`），`0x250..0x25C` 与旧 `0x2E0..0x2F4` 仍全空，
`verify_rtl_root.sh` = `MATCH=34 DIFF=0 RESULT: PASS`，负对照精确复现了 T-82 的症状。
