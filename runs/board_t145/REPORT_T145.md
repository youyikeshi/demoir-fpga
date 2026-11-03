# T-145：`BIN_LAZY_DEQ=1` 在 256² 上 **连跑 25 次 —— 三 sha1 全同且全中参照**

**一句话**：v39 @150 MHz、旧工作点、**零护栏**、256²，25 跑（1 个进程 1 次 load）
**25/25 命中且互相同**（去重后只有 1 组 sha1），墙钟 min/中位/max = **5.981 / 6.065 / 7.621 s**，
不满意跑 0 次、提前停机 0 次、睡 0 次。**`LAZY_DEQ=1` 在 256² 上的确定性从 11 跑扩到 36 跑。**

---

## 0. 运行点与指纹（1 个租约 = 1 进程 = 1 次真 load）

| 项 | 值 |
|---|---|
| 位流 | **v39** `design_1_150v39.bit` md5 **`602725b8`** / `.hwh` **`c1686bd8`**（冻结值，跑后板端 md5 仍同） |
| 频率 | 目标 150.0 → **读回 150 MHz**（load 后先读到 125，`set_fclk(150)` 后 150） |
| 指纹 | `conv_uni_0`=**0x40040000** / `bin_unit_0`=**0x40060000** ✓；无 `fconv` ✓ |
| magic | `0x314` 读回 `0x00000000` —— **v39 无此寄存器，按豁免**（探针 `MAGIC_TOL=True` 继续） |
| 工作点 | e1e2 + **LAZY_DEQ=1** + CAT_FUSION=1 + BILINEAR_C=1 + PRECOMP=1 + Q8CACHE=1 + F32DIRECT=1 + CAT_ECA_FUSE=1 + EPI_NOINV=1 |
| 形状/槽 | 256²，395 op（conv 227），24 槽，arena 66.22 MB @0x30100000 |
| 护栏 | **零**（`ms=0 / sel=none`）：每跑 `sleep_n=0`、`sleep_secs=0`、`push_input=0`（硬计数，不是"以为没睡"） |
| 依赖闭包 | `[DEPS] q8/c8deq/bpack/e1e2/catpack/safe = fast` ⇒ `deps_closure_ok=True`；`ps/` md5 -c **158/158**；清单三处同 md5 + `--check` rc=0（14 模块/20 文件） |
| 输入 | `x0` sha1 = `cfc75bc3…` = 冻结口径 ✓（每跑核对） |

## 1. 判据结果（25/25）

$$\Rightarrow\ \text{命中参照 } \mathtt{9b1940c8}/\mathtt{f86a7035}/\mathtt{025757a8}：\mathbf{25/25}$$
$$\Rightarrow\ \text{三 sha1 去重后组合数} = \mathbf{1}\ (\text{25 跑两两相同，无一次抖动})$$
$$\Rightarrow\ \text{提前停机原因} = \textbf{无}；\text{有效跑数} = 25/25；\text{abort/error} = 0$$

## 2. 时间分布（秒）

| 口径 | 墙钟 min / 中位 / max | PL min / 中位 / max | PS min / 中位 / max |
|---|---|---|---|
| **全 25 跑** | **5.981 / 6.065 / 7.621** | 2.237 / 2.269 / **3.122** | 3.072 / 3.125 / **4.543** |
| 去掉首跑（冷启动） | 5.981 / 6.059 / **6.358** | 2.237 / 2.268 / 2.440 | 3.072 / 3.124 / 3.333 |

$$\Rightarrow\ \text{首跑 = 冷态（}7.621 = \text{PL }3.122 + \text{PS }4.543），\text{第 2 跑起进入稳态（}6.0\sim6.36）$$
$$\Rightarrow\ \text{对照 T-141 的 LAZY=1 稳态 }6.15/6.18/6.25\ \Rightarrow\ \textbf{同一口径，无性能漂移}$$
$$\Rightarrow\ \text{PL 稳态 }2.24\sim2.44（\text{中位 }2.268）\ \text{与 T-141 的 }2.31\text{ 一致；PS 稳态 }3.07\sim3.33$$

## 3. 无异常项

| 项 | 结果 |
|---|---|
| **第 131 条 idle 门**（跑前 + 跑后各 1 次） | **50/50 全过**：`0x310=0x00000000` **且** `bin_unit_0`/`conv_uni_0` 的 `AP busy=0`、可读 ✓ |
| `AP_RETURN[15:8]` | 跑前/跑后全 0；**跑内钩子共 337,567 次读，非零 0 次**（每跑 13,053~13,785 次） |
| 金丝雀 / 段错误 / SIGALRM / busy 卡死 | **0**；`MemoryError` 0（`BV_FREE=1`） |
| 收尾 `0x310` | 每跑归零 ✓（第 120 条） |
| 板端 .bit md5 | 跑后仍 `602725b8…` ✓（PL 里留的是 **v39**） |

$$\Rightarrow\ \text{唯一"异常"是}\textbf{脚本层退出码}：\mathtt{rc=4}\ \text{来自探针结尾的键名 bug}$$
$$\quad\ \text{（代码查 } \mathtt{r["hit"]}，\text{而账本字段是 } \mathtt{hit\_v39}）\ \Rightarrow\ \textbf{与数据无关}$$
$$\quad\ \text{已修（} \mathtt{probe\_t145.py}\ \text{改为按 } \mathtt{all\_hit\ \&\&\ all\_same\ \&\&\ n\_valid} \text{ 定码），\textbf{未重跑}}$$
$$\quad\ \text{上板实跑的那份探针已从板端取回存档：} \mathtt{probe\_t145\_ASRUN.py}\ \text{md5 } \mathtt{7fd9d876…}$$

## 4. 逐跑账本（摘要）

| run | 墙钟 | PL | PS | 三 sha1（前 8） | 命中 |
|---:|---:|---:|---:|---|---|
| 1 | 7.621 | 3.122 | 4.543 | 9b1940c8/f86a7035/025757a8 | ✓（冷） |
| 2 | 6.235 | 2.310 | 3.254 | 同上 | ✓ |
| 3 | 6.358 | 2.440 | 3.305 | 同上 | ✓ |
| 4 | 6.347 | 2.430 | 3.333 | 同上 | ✓ |
| 5 | 6.231 | 2.316 | 3.276 | 同上 | ✓ |
| 6~19 | 5.981~6.105 | 2.237~2.280 | 3.072~3.163 | 同上 | ✓ |
| 20~21 | 6.193 / 6.214 | 2.381 / 2.412 | 3.151 / 3.162 | 同上 | ✓ |
| 22~25 | 6.018~6.182 | 2.248~2.392 | 3.104~3.141 | 同上 | ✓ |

完整逐跑账本（含 flush 次数、AP 读计数、分桶）：`t145a_run.log` / `t145a_t145a_125158.json`。

## 5. 纪律与板子现状

- **1 个租约 / 1 个进程 / 1 次真 load**；板端连接共 **5 条**（2 ssh + 2 scp + 1 scp 取回探针），无重试风暴；`ulimit -c 0`。
- 旧进程按 **pid `kill -TERM`**（`pgrep -x python3` 过滤 jupyter/redirect_server），**无 `pkill -f`**；陈旧 load 令牌（10:03）先删。
- 收尾 **只 `sync; sync`，未 poweroff**；`RTL`/`ps/` 主线**一字节未改**（本目录只有探针 + 脚本 + 产物）。
- 跑后板子仍在线：`MemFree 810 MB`、`CmaFree 138 MB`、`/` 34% 用量；只剩 `redirect_server`；**PL 里是 v39（`602725b8`）**。

## 6. 产物

| 文件 | 内容 |
|---|---|
| `probe_t145.py` | 探针（**已修退出码**，下轮可用；md5 见 MD5SUMS） |
| `probe_t145_ASRUN.py` | **上板实跑的那份**（md5 `7fd9d876…`，逐字节存档） |
| `run_t145.sh` | 单租约流程（指纹硬校验 / py_compile / ps 与清单同步 / md5 -c / --check / 收尾 sync） |
| `verify_t145.py` + `verify_t145.out` | 主机侧只读复核：**7 组判据全过（0 条失败）**，并独立复算 min/中位/max |
| `summary_t145.json` | 复核汇总（`all_hit=true all_same=true n_hit=25`） |
| `t145a_run.log` / `t145a_t145a_125158.{log,json}` | 板端日志与账本 |
| `ps_md5_host.txt` / `ps_md5_board_check.txt` / `ps_deps_md5sum_c.txt` | 158 文件逐一对齐 + 闭包清单 20 文件 |
| `t145a_bit_after.md5` | 跑后位流指纹 `602725b8…` |
