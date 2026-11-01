# 资源管控说明书（RESOURCE.md）

> 本机：31.7 GB RAM / **SwapTotal = 0** / Committed_AS 常态 57–66 GB（重度 overcommit）
> 根分区 `/`（含 `/tmp`）98 GB · 数据分区 `/media/felix/Data` 452 GB
> CPU：22 逻辑核，其中约 2 核可供 EDA
> Vivado 2025.2.1 · 单个实现峰值 4–16 GB
>
> 所有数字均为 `free` / `df` / `ps` 实测，不含估算。

---

## 1. 阈值表

| 级别 | 触发条件（任一） | 动作 | 位置 |
|---|---|---|---|
| **WARN** | MemAvailable < **6 GB** 或 根分区可用 < **10 GB** | 只记日志，不动手 | `resguard.sh` |
| **CRIT** | MemAvailable < **4 GB** 或 根分区可用 < **5 GB** | ① 杀**孤儿** xsim/xelab ② 跑 `reap.sh` 回收临时目录 ③ 仍低则记录 top5 内存占用者 | `resguard.sh` |
| **EMERG** | MemAvailable < **2.5 GB** 或 根分区可用 < **3 GB** | 清掉全部**非持锁孤儿** xsim 全家桶 + 跑 `reap.sh -H 1` + 日志写大字告警 | `resguard.sh` |
| **拒绝启动** | MemAvailable − 预估峰值 − 4 GB 保留 < 0 | 直接拒跑，说明还差多少 GB | `memgate.sh` |

保留余量 4 GB 是给桌面/IDE/浏览器的。**不要把 4 GB 调小**：IDE 一个 `Isolated` 就吃 5.26 GB。

---

## 2. 升级阶梯

```
编译/仿真前
  └─ memgate.sh <峰值GB> -- <命令>
       ├─ 内存不够 ──────────► exit 2，拒绝启动（等现有构建收尾）
       ├─ 根分区 <10G ───────► exit 4，先跑 reap.sh
       └─ 抢锁超时 ──────────► exit 3，说明别人在跑
                                    │
                                    ▼
                            放行（TMPDIR → /media/felix/Data/tmp）

构建中，resguard.sh 每 20s 采样
  ├─ OK ─── 记录一行 samples.tsv
  ├─ WARN ─ 只记日志
  ├─ CRIT ─ TERM 孤儿 → 等 60s → 仍活才 KILL → 跑 reap.sh
  └─ EMERG─ 同上但放宽到 30s 龄 + 大字告警 + 强制 reap -H 1

人（用户/父代理）介入点
  ├─ 允许牺牲构建 ──► 明确授权后由人降并发
  └─ 机器还活着但慢 ──► 只观察，等构建收尾
```

**杀进程阶梯（硬编码，不可跳过）**：
`SIGTERM` → 写日志（谁/多少 RSS/为什么）→ 等 **60 s** → 仍存活才 `SIGKILL` → 再写日志。
`RESG_DRYRUN=1` 可让整条阶梯只打日志不发信号（验证判定逻辑用）。

---

## 3. 必保进程清单（绝对不许杀）

| 进程 | 实测 RSS | 说明 |
|---|---|---|
| `firefox` / `firefox-bin` | 0.9 GB | 用户浏览器 |
| `node` | 1.7 GB | **DSH 本体**，杀了会话全断 |
| `gnome-shell` | 0.3 GB | 桌面 |
| `wechat` | 0.2 GB | 微信 |
| `java` | 0.2 GB | 用户 Java 应用 |
| `uvicorn` | 0.2 GB | 用户服务 |
| `celery` | 0.2 GB | 用户后台任务 |
| **`Isolated`**（含 `Isolated Web Co`） | **5.3 GB** | **IDE 进程。看着像大胃王，杀了用户会炸** |
| `systemd` / `sshd` / `ssh` | — | 系统和 SSH |
| `Xorg` / `Xwayland` / `dbus-daemon` / `pipewire` / `pulseaudio` | — | 图形栈 |
| `dsh` / 当前 DSH 进程 / 本脚本自身及全部祖先 | — | 会话命脉 |

### ⛔ 第 41 条教训（2026-10-31 血换来的，已硬编码）

> Vivado 的 `parallel_synth_helper` / `-notrace` 子进程，**即使 CPU 计数冻结 25 秒以上，也绝对不许 TERM**。
> 它们是主进程 `vivado -mode batch` 的托管子进程，被 TERM 会让主进程报
> `terminate called without an active exception` → `Abnormal program termination (6)`，
> 整个 25~40 分钟的构建报废。
>
> **"CPU 冻结"不是死亡判据。** 判据只能是：父进程是否还在推进（看它的 CPU 时间和日志增长）。
> 父进程在推进 + 子进程冻结 = 子进程在等管道，**必须留着**。
>
> 杀 vivado 子进程 = 一定毁掉构建；留着最多是 OOM 风险。**两害相权，保住构建。**
> `resguard.sh` 的 `killable_now()` 用两道闸挡死：
> ① comm 为 `parallel_synth*` / `loader` / 含 `-notrace` → 一律不杀
> ② 任何**有 vivado 祖先**的进程 → 一律不杀（结构性兜底，不怕改名）

硬编码在 `resguard.sh` 的 `PROTECTED_RE` 里，另有独立保护：**任何 shell（`bash`/`sh`）一律不动**、**非本用户 uid 的进程不动**、**自身祖先链不动**。

> ⚠️ 特别提醒：`Isolated` 是 OOM killer 的头号目标（RSS 最大的单一进程）。想保 IDE，就必须让 MemAvailable 别掉到个位数——这正是 `memgate.sh` 存在的理由。

---

## 4. 如何安全检查内存（踩过的坑）

**① 别看 `free` 的 `used`，看 `MemAvailable`**
```bash
awk '/MemAvailable:/{printf "%.2f GB\n", $2/1048576}' /proc/meminfo
```
`used` 把可回收的 buff/cache 也算进去了，会吓死人；`MemAvailable` 才是"还能不能再起一个进程"的答案。

**② SwapTotal = 0 ⇒ 没有缓冲，`Committed_AS` 才是真正的雷**
```bash
grep -E 'CommitLimit|Committed_AS|SwapTotal' /proc/meminfo
```
本机实测 `Committed_AS` 能到 66 GB，而 `MemTotal` 只有 31.7 GB、`CommitLimit` 才 16.2 GB。
**一旦 MemAvailable 跌破峰值预估就必然 OOM，不会有 swap 兜底。**

**③ `fuser -m <目录>` 完全没用**
它会返回"整个根分区的所有进程"——因为 `/tmp` 和 `/` 是同一个文件系统，`-m` 按挂载点匹配。
**必须用 `lsof +D <目录>`**（递归查打开句柄），或者对单文件用 `lsof -- <file>`。
本项目 `reap.sh` 全部用 `lsof +D`，一行 `fuser -m` 都没有。

**④ 别用 `ps aux | grep` 判断"有没有构建在跑"**
有"loader 已退出但 vivado 未起"的竞态窗口。用 `flock -n` 探锁才准：
```bash
flock -n 9 9</tmp/dsh_vivado.lock && echo 空闲 || echo 有构建
```

**⑤ 进程"看着像孤儿"不等于它是孤儿**
必须先上溯父链找活 runner。实测正常构建链：
```
xsim → loader → bash(名为 xsim) → sim_core.sh → bash -c cd <项目> → node(dsh) → systemd
```
被 reparent 的孤儿会直接挂到 `node`/`systemd` 上，中间**没有** runner。
`resguard.sh` 的 `is_orphan()` 就是按这条实现，另有「存活 <90 s 不碰」兜底。
**验证方法**：`RESG_DRYRUN=1 resguard.sh --once`，看它是否误报活构建。

**⑥ 波形库落在哪，是磁盘问题的根**
`xelab -debug typical` ⇒ 打开 waveform 能力 ⇒ `xsim` 在**当前工作目录**写 `<snapshot名>.wdb`。
快照名叫 `snap` 时就是 `snap.wdb`。工作目录是 `mktemp -d /tmp/vwrap${U}.XXXXXX` ⇒ 直接落在根分区。
长仿真 + 超时被杀 ⇒ 目录永不清理 ⇒ 单个 `snap.wdb` 涨到 13 GB。
**根治三层**：① `-debug off` ② `TMPDIR` 挪到 Data 分区（`memgate.sh` 做）③ `reap.sh` 定期回收。

---

## 5. 三件工具

| 工具 | 用法 | 说明 |
|---|---|---|
| `tools/memgate.sh` | `memgate.sh <峰值GB> -- <命令>` | 构建前闸门：内存/磁盘/单实例锁三关，放行后 `TMPDIR` 指向大分区 |
| `tools/resguard.sh` | `resguard.sh` / `--status` / `--once` / `--stop` | 20s 采样守护，分级处置，日志 `runs/resguard/resguard.log` 按天切 |
| `tools/reap.sh` | `reap.sh`（dry-run）/ `reap.sh --yes` | 白名单 + `lsof +D` + mtime 三重验证后回收 `/tmp` |

锁：`memgate.sh` 与 `pl/rtl_hand/tools/vlock.sh` **共用同一把锁** `/tmp/dsh_vivado.lock`（同为 fd 9 + 独占 flock），互斥，不存在两套并发。

---

## 6. 本机并发上限（实测推算）

| 构建类型 | 峰值 | 需要 MemAvailable | 17:28 实测 10.88 GB | 17:33 实测 3.54 GB |
|---|---|---|---|---|
| 小仿真 / 语法检查 | 0.7 GB | 4.7 GB | ✅ | ❌ |
| 常规综合 | 4 GB | 8.0 GB | ✅ 1 个 | ❌ 差 4.5 GB |
| 中型实现 | 8 GB | 12.0 GB | ❌ 差 1.1 GB | ❌ 差 8.5 GB |
| 大型实现 | 16 GB | 20.0 GB | ❌ 差 9.1 GB | ❌ 物理不可达 |

**结论：安全并发 = 1（串行）。**

### 天花板重算（2026-10-31 17:50 实测，推翻早先的 13 GB 估计）

```
MemTotal 30.95 GB − MemAvailable 9.24 GB = 已用 21.72 GB
其中 EDA 进程占 4.29 GB
⇒ 非 EDA 常驻基线实测 = 17.42 GB
```
（早先估的 13 GB 偏低。这 17.42 GB 含 IDE 6.88 + DSH 1.7 + 浏览器 + 桌面 + 不可回收 cache。）

- **EDA 绝对天花板 ≈ 30.95 − 17.42 − 4（保留）= 9.5 GB**
- 一个 8 GB 峰值的实现需要 **12 GB MemAvailable**，本机长期只做到 9–12 GB ⇒ **卡线**
- **16 GB 峰值的实现在这台机器上物理不可达**

判断"现在能不能起一个构建"只看一条实测：`MemAvailable − 峰值 − 4 ≥ 0`。
`memgate.sh` 就是这条判断的自动化，别自己心算。

### IDE（Isolated）内存长期趋势实测

| 时刻 | 最大进程 (pid 2580933) | 全部 Isolated 合计 |
|---|---|---|
| 17:38:48 | 5.14 GB | ~6.90 GB |
| 17:46:51 | 5.14 GB | 6.93 GB |
| 17:49:58 | 5.14 GB | 6.88 GB |

**12 分钟内持平不增长**（8 个 Isolated 进程，主进程 5.14 GB 纹丝不动）。
⇒ IDE 不会随时间无限膨胀，天花板不会被它继续侵蚀。
⇒ 但它是**全机最大单个 RSS**，而 SwapTotal=0 ⇒ OOM killer 头号目标。
⇒ 建议用户定期重启 IDE 的阈值：**`Isolated` 合计 > 8 GB，或主进程 > 6.5 GB** 时重启。
（`resguard.sh` 每轮都把 top5 写进日志，可回溯。）

---

## 7. 授权范围（谁能动手 / 谁绝对不能）

| 对象 | 能否动手 |
|---|---|
| 孤儿 `xsim` / `xsimk`（父链里已无活 runner） | ✅ 可 TERM |
| 父进程已死的 `xvlog` / `xelab` | ✅ 可 TERM |
| **vivado 的任何子进程**（`parallel_synth_helper`、`-notrace`、`loader`） | ⛔ **绝对不许**（第41条） |
| `vivado -mode batch` 主进程 | ⛔ 不许，除非 MemAvailable < **2.0 GB** 真 OOM 边缘，且先在日志写清"我杀了哪个构建" |
| IDE `Isolated` / firefox / node / sshd / DSH | ⛔ 永不 |
| 板子 192.168.7.2 | ⛔ 永不触碰 |

**EMERG 时的正确动作顺序**（`resguard.sh` 已按此实现）：
1. 清孤儿 xsim（最多回收 1~2 GB）
2. 跑 `reap.sh`（救磁盘，救不了内存）
3. 在日志写大字告警 + `--status` 标红
4. **不要动 vivado**，等上层决策（把这件事报给父代理）

验证手段：`resguard.sh --selftest`（8 条合成用例）与 `resguard.sh --audit`（真实进程逐条判定）。
