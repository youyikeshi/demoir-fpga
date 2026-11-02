# T-117：v39 位流 + 当前 PS，256² 对照实验（决定性）

**一句话**：**v39 位流 + 当前 PS 在 256² 上仍然正确**（两次都命中冻结三 sha1、两次相同；64² 同样正确）。
$\Rightarrow$ **根因在 RTL（v53），不是 PS 回归。**

## 0. 跑点（一次 load，唯一令牌）

| 项 | 值 |
|---|---|
| 位流 | `pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v39.bit` md5 **`602725b86941864eeeeab3be005e1e81`**（跑前=跑后 ✓）＋ `.hwh` **`c1686bd8e080c1c7f46181d980543202`**（成对 ✓） |
| load | `Overlay(download=True)` 2.731 s，**本 session 唯一一次 download**；IP 清单 `['bin_unit_0','conv_uni_0','ps7']` |
| 基址 | `conv_uni_0` = **0x40040000** ✓、`bin_unit_0` = **0x40060000** ✓ |
| magic | **v39 无 0x314 CHAIN_ID**：按任务书**不读、不判**（不把读失败当故障） |
| 身份 | `alpha_shift=9` / `slope_shift=8` / `ResvPage[0x18]=0` ✓ |
| fclk0 | 目标 50 → **读回 50.0000 MHz** |
| 运行点 | `BIN_PACK=e1e2 BIN_LAZY_DEQ=0 BIN_LAZY_SLOTS=24 BIN_CAT_FUSION=1 BIN_BILINEAR_C=1 BIN_PRECOMP=1 Q8CACHE=1 BIN_F32DIRECT=1 BIN_CAT_ECA_FUSE=1 BIN_EPI_NOINV=1 BIN_PROFILE=1`；**无任何 `BIN_V50_*`** |
| PS 门禁 | 板上 `md5sum -c` 157/157 ✓；`deps_closure --check` rc=0（14 模块/20 文件，清单 md5 `9de3d60c…`）✓；`[DEPS] q8/c8deq/bpack/e1e2/catpack/safe` 全 fast ✓ |
| 输入 | 256² `x0=cfc75bc3…` ✓；64² `x0=bbae82e4…` ✓（与冻结口径逐位一致） |
| 租约 | 1 次 load、`ulimit -c 0`、只按 pid `kill -TERM`、收尾只 `sync; sync`、**未 poweroff**；3 条 ssh/scp |

## 1. 256² @50 MHz（冻结尺寸 arena = 66,224,128 B / 24 槽，exact=True）

| run | out1 / out2 / out3 sha1 | = 冻结？ | 两次相同 | 总 s | PL s | PS s |
|---|---|---|---|---|---|---|
| 1 | `9b1940c8955b` / `f86a7035621b` / `025757a83c39` | ✓ ✓ ✓ | ✓ | 17.997 | 5.225 | 15.522 |
| 2 | `9b1940c8955b` / `f86a7035621b` / `025757a83c39` | ✓ ✓ ✓ | ✓ | 16.681 | 4.359 | 14.283 |

$\Rightarrow$ **v39 在 256² 上现在仍然对，而且确定（两次逐位相同）。**

## 2. 64² @50 MHz 对照（arena = 4,292,608 B / 24 槽，exact=True）

| run | sha1 | = 冻结？ | 总 s | PL s | PS s |
|---|---|---|---|---|---|
| 1 | `bfa246d34681` / `226bd39e522a` / `762d84b9618e` | ✓ | 3.937 | 0.625 | 2.630 |
| 2 | `bfa246d34681` / `226bd39e522a` / `762d84b9618e` | ✓ | 3.831 | 0.609 | 2.520 |

## 3. ★ 对 T-114 两条"旁证指向 PS"的反证

同一个**结果全对**的 v39 256² 跑，呈现与 v53 错跑**完全相同**的两个"异常指纹"：

| 指纹 | v53 256²（错） | **v39 256²（对）** | 结论 |
|---|---|---|---|
| PS 时间 256²/64² | ≈ 14.3 s / 2.5 s = **5.7×**（数据 16×） | 14.283/2.520 = **5.67×**（另一跑 5.90×） | 不是故障指纹 |
| PL 实测/公式拍数比 | 0.48–0.50 | **0.483 / 0.579** | 不是故障指纹 |
| 总时间 | 16.2–16.7 s | 16.7–18.0 s | 同量级 |

$\Rightarrow$ v53 256² 的表现是"**时间账几乎不变、数据错且非确定**" ⇒ 指向 RTL **数据通路/写通路**错误，
而不是"PS 以为 done 了、PL 没干完活"。T-114 的 "sticky done / start 握手竞态" 方向应降权。

## 4. 探针自检伪报警（不影响 sha1 判定）

整网后 `AP_RETURN=0x00000000`：done 位已归 0、**busy=0**。我的判据要求 done=1 才叫"空闲"，
因此日志打了 `（PL 非空闲 ✗✗）`；按 busy=0 判，PL 实际是空闲的。这与 sha1 判定无关。

## 5. 一条待定的独立假设（不属本次结论）

`vlanes_now()=8`（任务书冻结运行点未设 `BIN_VLANES`，本轮也未设）。PS 注释明确：
VLANES=16 时 `PRM_W` 必须抬到 32，否则 lane 8..15 会静默读到相邻参数数组。
若 v53 的"16-lane 核"同时改动 PRM 帧布局，这是一条**独立**的 PS 跑点问题——但它同样会影响 64²，
而 v53 64² 全对，故大概率不是本次 256² 根因。是否单独立项由主代理决定。

## 6. 结论

$$\boxed{\textbf{v39 位流 + 当前 PS 在 256}^2\ \text{上仍对（两次确定）} \;\Rightarrow\; \textbf{根因在 RTL（v53）}}$$

## 7. 产物

`R/runs/board_t117/`：
`probe_t117.py`（一次 load：load+基址/身份 → 256²×2 → 64²×2）、
`run_t117.sh`（体检/杀旧/CMA 审计/双门禁/跑/拉回）、
`t117_045007.log` / `.json`（板端原始日志与结构化结果，含每跑 PL/PS/耗时/AP_RETURN）、
`t117_run.log`、`t117_host.log`、`t117_bit_after.md5`、
`ps_md5_host.txt` / `ps_md5_board_check.txt` / `ps_deps_md5sum_c.txt`（门禁凭据）。
