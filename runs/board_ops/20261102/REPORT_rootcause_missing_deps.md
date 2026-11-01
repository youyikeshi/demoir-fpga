# 1.65× 系统性惩罚的根因报告 —— **冻结点漏了三个 C/NEON 依赖模块，静默回落**

**执行者**：系统性能取证工程师（第三棒）
**板端会话**：2026-11-02 00:11–00:24（**同一 boot 内全程未重启**；开工时 `uptime = 4607.7 s`）
**跑点**：位流 v39 `602725b8`；`fclk0` 读回 **150**；槽数 **24**；`Overlay(download=True)` 1.714 s
**代码**：`runs/board_ops/20261102/frozen_code/`（git HEAD `b58964e` 的 7 个文件 + 驱动 `ab_arms.py`）+ 差分 A/B 的依赖闭包
**sha1 判据**：本报告**每一行测量**的输出都是 `9b1940c8955b / f86a7035621b / 025757a83c39`（**3/3**）
**输入**：`x0 sha1 = cfc75bc3f15d306b`（与冻结基线一致）

---

## 0. 一句话结论

$$13.4\ \text{s}\ \textbf{不是板子坏了、不是代码退化、也不是 CMA}\ \text{——而是冻结点目录漏了 } \texttt{q8.py/.c}、\texttt{c8deq.py/.c}、\texttt{bpack.py/.c}\ \text{三个 C/NEON 内核模块，而 } \texttt{bin\_engine}\ \text{对它们\textbf{静默回落}到 numpy 慢路径。}$$

$$\Rightarrow\ \text{同进程、同 arena、同输入、同位流，只把这三个模块补到 } \texttt{sys.path}\ \text{上：} \texttt{prologue } 5.948\to\mathbf{1.611}\ \text{s（}\times 3.69\text{）},\ \texttt{epilogue } 1.189\to\mathbf{0.258}\ \text{s（}\times 4.61\text{）}$$
$$\Rightarrow\ \texttt{total } 11.752\to\mathbf{6.497}\ \text{s}\ \text{（历史 C 臂 } 6.485\ \text{s，差 } +0.19\%\text{）};\ \text{再把 } \texttt{interp}\ \text{切回 numpy：}\mathbf{8.261}\ \text{s}\ \text{（历史 numpy 臂 } 8.058\ \text{/ v39 基线 } 8.096\text{）}$$
$$\Rightarrow\ \Rightarrow\ \textbf{8.096 / 6.485 是"带依赖闭包可复现"的；"不可复现"这个判断今天被推翻。}$$

$$\Rightarrow\ \text{而 } \texttt{prologue}\ \text{的 } \times 3.68\ \text{、} \texttt{epilogue}\ \text{的 } \times 4.60\ \text{与本次隔离测出的 } \mathbf{3.69\times}、\mathbf{4.61\times}\ \text{是同一个数——这是判据，不是巧合。}$$

---

## 1. 结论的证据链（五条，每条都可独立复核）

| # | 证据 | 数字 | 证伪了什么 |
|---|---|---|---|
| ① | 冻结点 `pynq_frozen/` 只有 13 个条目（8 个 `.py`（含驱动 `ab_arms.py`）+ `bilinear_c.c` + 板端编出的 `bilinear_c_armv7l.so` + `MD5SUMS`/`README.md`/`__pycache__`），**没有** `q8.*`/`c8deq.*`/`bpack.*` | 这 6 个文件全 `缺失` | "冻结点 = 一份可跑的代码" |
| ② | 板端实测三个惰性内核加载点**全部回落** | `_q8()=None`、`_deq_kernel()=None`、`_bpack()=None` | "今天跑的是优化后的路径" |
| ③ | 只补依赖、不动一个字节代码 | `prologue ×3.69`、`epilogue ×4.61`、`total −5.255 s` | "环境状态漂移"（H1） |
| ④ | 逐模块隔离（各只补一个） | `q8 ⇒ prologue`、`c8deq ⇒ epilogue`、`bpack ⇒ prologue 小头` | "四个桶一起变，说不清是哪个" |
| ⑤ | 补全后两档历史数字同时回来 | C 臂 `6.497` vs `6.485`（+0.19%）；numpy 臂 `8.261` vs `8.058`（+2.5%） | "8.096 不可复现" |

---

## 2. §1 —— 今天 CMA 到底被谁占着（**结论：没有谁"占着"，H1 的前提不成立**）

### 2.1 采样（3 次 × 10 s，同一 boot，uptime 4607.7 s）

| 时刻 | CmaFree | MemFree | MemAvailable | Cached | AnonPages |
|---|---|---|---|---|---|
| 采样 1 | **90.918 MB** | 659.2 | 869.2 | 183.5 | 90.3 |
| 采样 2 | **90.895 MB** | 659.2 | 869.2 | 183.5 | 90.2 |
| 采样 3 | **90.895 MB** | 659.2 | 869.2 | 183.5 | 90.2 |

⇒ **三次逐位不变**（差 0.02 MB 是噪声）⇒ 没有进程在持续吃 CMA，也没有泄漏在长大。
`CmaTotal = 256 MB / MemTotal = 1004 MB`（CMA 占 RAM 的 **25.5%**）。

### 2.2 `CmaFree ≈ 0.5 MB` 到底是怎么来的 —— **是"借走"，不是"被抢走"，而且可逆**

| 阶段 | CmaFree | 说明 |
|---|---|---|
| 开机稳态 | 90.9 MB | 池子里还有 166 MB 不在 free 列表里 |
| 起 python+numpy | 58.8 MB | 与被测无关的常规波动 |
| `Overlay(download=True)` | 32.1 MB | **掉 26.7 MB**：`drm` 从 CMA 拿位流/DMA 缓冲 |
| 建 24 槽 arena（66.22 MB @`0x30100000`） | **0.438 MB** | **一次拿到 66 MB，物理地址 `0x30100000` 与历次完全相同** |
| 进程退出后 | 回涨 | 可逆 |

**机制（有 dmesg 原文支撑，不是推断）**：

```
[  887.297590] [drm] memory 0 is not reserved in device tree. will allocate memory from cma
[ 1127.150437] [drm] memory 0 is not reserved in device tree. will allocate memory from cma
...（6 条，每次 allocate 一条）
```

- Linux 的 CMA 区是 **MOVABLE 内存**：没有 CMA 请求时，普通匿名页/页缓存可以**借**它用，`CmaFree` 因此显示很低；
  真来 CMA 请求时内核把它们迁移出去（`alloc_contig_range`）——所以"`CmaFree` 低"**不代表**"拿不到 CMA"。
- 本次实测就是活证据：开工 `CmaFree 90.9`，Overlay 拿 26.7、arena 拿 66.2，**都当场成功**，
  而且 arena 的 PA 与 10 小时前、与昨天**同一个地址** `0x30100000`。
- `CmaFree 0.438 MB` 是**arena 自己吃掉的**（90.9 − 26.7 − 66.2 ≈ 0），不是别人占的。

### 2.3 三条"不是某服务抢走 190 MB"的直接否证

| 否证 | 数据 |
|---|---|
| 没有进程持有那么多内存 | 全机 85 个进程 RSS 合计 **253.9 MB**，前 18 名 218.0 MB：`jupyter-notebook 48.9`、`smbd 16.8`、`redirect_server 15.3`、`samba-bgqd 13.9`、`unattended-upgr 13.0`。**全加起来也不到 190 MB**，且都是普通 anon 页不是 CMA |
| 没有 named CMA region 可查 | `/sys/kernel/debug/cma` **不存在**（CMA 不是 device-tree 的 named region）⇒ 这条路走不通，`allocated/count` 读不到，**如实记录** |
| 大块连续页不缺 | `cat /proc/buddyinfo` Normal 区 order-10 = **104 块**（≈416 MB 连续），order-9 = 10、order-8 = 25 ⇒ 碎片不是问题 |

$$\Rightarrow\ \textbf{§1 判据不成立}：\text{没有"某个服务开机抓走 } \sim190\ \text{MB CMA"。H1 的第一环（CMA 被长期占用）\textbf{被否掉}。}$$

---

## 3. §2 —— "三态对照"改写成"依赖闭包三态"（**不用重启，同 boot 内完成**）

### 3.1 为什么不需要重启

原计划是「态 A（CmaFree≈0.5）vs 态 B（重启后 CmaFree 充裕）」。
实测发现**今天根本不存在"CmaFree≈0.5 的饥饿态"**：开机稳态就是 90.9 MB，`CmaFree` 低只是 arena 自己吃的。
更要紧的是——**在 CmaFree 90.9 MB、arena 真的拿到 66 MB 连续 CMA 的前提下，prologue 仍然是 5.95 s**。
⇒ 重启只会把一个**已经用不上的变量**再洗一遍，还会丢掉同 boot 内的对照窗口。**故不重启。**

### 3.2 态 A：冻结点（缺依赖）—— 与"13.4 s 那天"逐位复现

| 跑法 | CmaFree 跑时 | total | prologue | epilogue | interp | PL | wait |
|---|---|---|---|---|---|---|---|
| A1（`BIN_PROFILE=0`） | ~0.5 MB | 12.750 | **6.170** | **1.189** | 0.983¹ | 3.851 | 1.359 |
| A2（`BIN_PROFILE=1`） | ~0.3 MB | 11.975 | **6.062** | **1.185** | 0.266 | 3.222 | 1.527 |
| 原函数 `V.run_net`（协议自证） | — | 11.854 | **5.953** | **1.187** | 0.266 | 3.202 | 1.532 |
| 参考：23:55 冻结点跑点 | — | 11.657 | 5.811 | 1.182 | 0.266 | 3.072 | — |

¹ A1 的 `interp=0.983` 是**本 session 首次**调用 bilinear C 内核的自检/首触成本，A2 起 0.266；
`V.run_net` 与薄拷贝的输入/输出**逐位相同**（协议自证 ✓）。

### 3.3 H1 的三条机制性反证（每条都是直接测量，不是间接推断）

| # | H1 的断言 | 实测 | 判读 |
|---|---|---|---|
| ① | "缓冲区不再 CMA 后备，落到普通 heap" | arena `PA=0x30100000`，建它时 `CmaFree` **精确掉 66 MB** | **假**：就是 CMA，且物理连续、地址与历次相同 |
| ② | "`flush/invalidate` 按 cache line 维护" | `pynq 3.1.0` 的 `PynqBuffer.flush` 源码：`if not self.coherent: self.device.flush(...)`；`allocate()` 出来 `coherent=True` | **假**：**根本不执行** cache 维护 |
| ③ | "所以搬运成本数量级上升" | 66.22 MB `flush()` = **med 0.149 ms**、`invalidate()` = **0.155 ms**（= 444 GB/s，物理上不可能） | **假**：flush 成本 ≈ 0，不可能解释 4.2 s |

> 补一条**更精确**的机制（这条留着，它才是真正的物理）：非缓存缓冲的代价**不是**顺序写，而是**跨步逐字节**访问。
> 实测 1 MB **顺序**填充：arena（非缓存）0.508 ms vs heap（缓存）0.417 ms = **仅 1.22×**；
> 而 `bin_engine.py` 自己在 c8deq 段的注释写着：非缓存 DMA 缓冲上 numpy 单趟 `np.multiply(跨步视图)` **5.45 s**，
> C 内核 + 一次 burst memcpy **0.97 s**（≈5.6×）。⇒ 惩罚来自 **"numpy 跨步 × 非缓存"这个组合**，
> 而 `q8`/`c8deq`/`bpack` 正是为消灭这个组合而写的。

---

## 4. §3 —— 逐桶 / 逐模块二分（**这才是根因的直接证明**）

### 4.1 关键发现：`bin_engine` 的三个 C 内核都是**静默回落**

| 加载点 | 代码 | 缺失时行为 | 落在哪个桶 |
|---|---|---|---|
| `_q8()` → `import q8` | `bin_engine.py:2870` | 回落 `quantize_int8` + `np.copyto(dst, q.reshape(...).transpose(0,2,3,1))`：**把量化结果按字节散写进非缓存缓冲** | **prologue**（`_pf["q8"]`、`_pf["f32"]`） |
| `_deq_kernel()` → `from c8deq import deq` | `bin_engine.py:2618` | 回落 numpy 跨步视图反量化 | **epilogue** |
| `_bpack()` → `import bpack` | `bin_engine.py:2595` | 回落 numpy 二值化打包 | prologue（小头） |

三处都是 `try: ... except Exception: 存 None`，**一行日志都不打**。

### 4.2 隔离实验（同进程族、同 arena、同输入、同 11 项开关；每个进程只补一个子集）

口径 b（每组第 2 次起；第 1 次含 `.so` 现场 `cc` 编译，见 §4.4）。**下表一律取"该臂第 2 次（稳态）"那一整行**，不跨行拼数：

| 闭包子集 | total | **prologue** | **epilogue** | interp | wait | PL |
|---|---|---|---|---|---|---|
| 无（= 今天所有 13.4 s 的跑法） | 11.752 | **5.948** | **1.194** | 0.266 | 1.426 | 3.182 |
| 只补 `bpack` | 11.308 | 5.496 | 1.188 | 0.265 | 1.376 | 3.196 |
| 只补 `c8deq` | 10.746 | 5.877 | **0.255** | 0.261 | 0.597 | 3.106 |
| 只补 `q8` | 8.243 | **2.376** | 1.185 | 0.264 | 1.540 | 2.722 |
| **补齐三件套** | **6.497** | **1.611** | **0.258** | 0.267 | 0.630 | 2.223 |
| 三件套 + numpy interp（`BIN_BILINEAR_C=0`） | **8.261** | 1.625 | 0.259 | 1.938 | 0.732 | 3.219 |

> 判据脚本里用的是"每列取最好值"（`prologue_a=5.9477 / b=1.6106 ⇒ 3.693×`、
> `epilogue_a=1.1891 / b=0.2577 ⇒ 4.614×`），与上面"整行稳态"算出的 `3.692× / 4.628×`
> 在小数第二位一致 ⇒ 比值本身稳，不是拼出来的。

**归因算术（桶级，可复算，全用同一整行）**

- `epilogue`：`1.194 − 0.258 = 0.936 s`，而 c8deq 单独给出 `1.194 − 0.255 = 0.939 s` ⇒ **一对一，误差 0.3%**。
- `prologue`：`5.948 − 1.611 = 4.337 s`；q8 单独 `5.948 − 2.376 = 3.572 s`、bpack 单独 `5.948 − 5.496 = 0.452 s`，
  合计 `4.024 s`，残差 `0.313 s（7.2%）` 属两者叠加的交互项（q8 可用时更多层走 f32 打包路径，bpack 被调用得更多）。
- `interp / convfp / cat`：六种闭包组合下**互差 < 3%**（interp 0.261–0.267、convfp 1.14–1.27、cat 0.13–0.15）
  ⇒ **算法桶完全不受影响**，与"昨天桶级 1.00×"的观测一致。

$$\Rightarrow\ \textbf{prologue 的 } 5.948/1.611 = 3.69\times\ \text{正对上昨天的 } 3.68\times;\ \texttt{epilogue}\ \text{的 } 1.194/0.258 = 4.63\times\ \text{（列最小口径 } 1.189/0.258 = 4.61\times\text{）正对上 } 4.60\times$$
$$\Rightarrow\ \text{两个数字、两个不同方向，同一机制两条独立通道——这不是"有结论"，这是\textbf{可复算的同一性}。}$$

### 4.3 为什么是"今天比昨天慢"，而不是"今天坏"

- `q8.py`/`c8deq.py`/`bpack.py` 的 `.so` 是**板端 `cc -O3` 现场编译**（`q8_armv7l.so` 等），
  **源代码里没有** `.so`（跨架构，不许随包传）。
- 冻结点目录是按"我在跑什么"手挑的 7 个 `.py`，**没有按依赖闭包挑** ⇒ 三个模块在板上"既没 .py 也没 .c"，
  **连编都编不出来**，只能回落。
- 于是：**昨天从 `ps/pynq/` 跑（闭包完整，`.so` 现场编出来）⇒ 8.096/6.485；
  今天从 `pynq_frozen/` 跑（闭包残缺）⇒ 13.4/11.7。** 同一个 boot、同一块板、逐位相同的 `bin_net`/`bin_engine`。
- 这也**同时解释**了"换回 20261101 原码仍是 13.616 s"：`control_20261101/` 目录**同样没有**这三个模块
  ——那场对照实验在设计上就被污染了，它的结论"不是代码的锅"**仍然成立**，但理由从"环境状态"变成了"两份代码用了同一个残缺闭包"。

### 4.4 第一次运行的额外成本（口径 a）

`.so` 不存在时，**首跑**会在计时窗口内现场 `cc` 编译：
`补齐三件套` 的 B1 是 `7.998 s`（epilogue **1.699** s，含编译），B2 才落到 `6.497 s`（epilogue **0.258**）。
⇒ **报点必须区分"首跑（含 cc）"与"稳态"**，否则会把 1.4 s 的编译算进 epilogue。

---

## 5. §4 —— 跑点字段（从今天起必记，否则"跑点不闭合"）

### 5.1 本次实测到的板端基线状态（**这些值本身就有判据价值**）

| 项 | 实测值 | 判读 |
|---|---|---|
| `CmaTotal` / `CmaFree`（稳态） | **256.0 / 90.9 MB** | CMA 不是饥饿态；建 arena 后 0.44 MB 是**自己吃的** |
| `MemTotal` / `MemAvailable` | 1004.16 / 869.2 MB | 1 GB 板，CMA 占 25.5% |
| `SwapTotal` | **512 MB** | 不是 0（技能 §2.6 里"SwapTotal=0"的前提在这块板上**不成立**，构建内存闸门要按 512 MB swap 算） |
| **`cpufreq`** | **节点不存在**（`/sys/devices/system/cpu/cpu*/cpufreq/` 为空） | Zynq 无 DVFS ⇒ **CPU 频率不是变量**，"降频/热降频"这条候选**直接排除**（这是今天新拿到的一条否定证据） |
| `buddyinfo`（Normal，order0→10） | `424 342 254 256 317 192 101 44 25 10 **104**` | order-10 有 104 块（≈416 MB 连续）⇒ 无碎片问题 |
| 软件栈 | `pynq 3.1.0`、`numpy 1.21.5`、`xrt` python 绑定**不存在**（PYNQ 3.1 embedded 不用） | 记清，否则会照着 XRT 的语义误读 `flush` |

### 5.2 跑点 JSON 必须新增的字段（**这是本次事故的制度产出**）

```jsonc
"runpoint": {
  "env_all":            { /* env | sort 全集：代码实际 consult 36 个键，见下 */ },
  "env_consulted":      { /* 只这 36 个键 + 是否被显式设置 */ },
  "mem":                { "CmaTotal_mb": 256.0, "CmaFree_mb": 90.9,
                          "MemAvailable_mb": 869.2, "MemTotal_mb": 1004.2,
                          "SwapTotal_mb": 512.0 },
  "buddyinfo":          { "Normal": [424, "..."] },
  "cpufreq":            { "available": false },        // ← 显式"不可得"，不许留空
  "kernel_deps": {                                     // ★ 本次事故的核心字段
    "q8":        { "state": "fallback", "module": null, "so": null, "md5": null },
    "c8deq":     { "state": "fallback", "module": null, "so": null, "md5": null },
    "bpack":     { "state": "fallback", "module": null, "so": null, "md5": null },
    "bilinear_c":{ "state": "fast", "module": ".../bilinear_c.py",
                   "so": ".../bilinear_c_armv7l.so", "md5": "..." }
  },
  "deps_closure_ok":    false,                         // ← 门禁：false 的跑点不许进基线表
  "so_build":           { "cc": "arm-linux-gnueabihf-gcc 8.3.0",
                          "built_now": ["q8_armv7l.so"] }
}
```

**`env_consulted` 的 36 个键**（`grep -ohE 'environ(\.get)?\("[A-Z_0-9]+"' frozen_code/*.py` 全量枚举）：

```
BILINEAR_C_DIR BIN_BILINEAR_C BIN_BILINEAR_V2 BIN_BILINEAR_V3 BIN_BILINEAR_V4
BIN_CAT_FUSION BIN_CAT_STRICT BIN_CAT_THREADS BIN_F32POOL BIN_F32_SLOTS
BIN_FAST_ECA BIN_GEMM_THREADS BIN_LAZY_DEBUG BIN_LAZY_DEQ BIN_LAZY_SLOTS
BIN_MALLOPT BIN_MALLOPT_MMAP_MB BIN_MALLOPT_TRIM_MB BIN_NUMPY_NO_MALLOPT
BIN_PACK BIN_PROFILE BIN_Q8_FUSED BIT BV_FREE CATPACK CATPACK_LAZY_MB CC
E1E2_DBG E1E2_DESC NET_TPOLL PACK PL_TAKEOVER Q8_C8_IMPL Q8CACHE
Q8CACHE_MB Q8CACHE_VERIFY
```

> 之前的跑点表只记 6–11 个（`BIN_PACK`/`BIN_LAZY_DEQ`/… 那一组），
> 而**真正决定今天/昨天差异的 `BIN_BILINEAR_C` 与三个内核模块的状态，一个都没记**。

### 5.3 门禁（判定"跑点闭合"的必要条件）

```
① md5sum -c deps_closure.txt  rc=0        （依赖闭包完整）
② runpoint.kernel_deps.*.state 全为 fast   （或显式声明"本跑点故意用 numpy 对照"）
③ runpoint.cpufreq.available 字段存在      （不可得也要写 false，不许缺席）
④ CmaFree 在 arena 建好前后各一次          （证明 arena 真的拿到 CMA）
⑤ 首跑 vs 稳态分开记                       （.so 现场编译成本不许算进 epilogue）
```

---

## 6. §5 —— 机制性修法（按"根治 > 缓解"排序）

### ① 【根治】把"冻结点"从**文件清单**改成**依赖闭包**（本次唯一必须做的）

```bash
# 静态抽出闭包（不改任何被测代码）
grep -ohE '(^|\s)(import|from)\s+[a-zA-Z_][a-zA-Z_0-9]*' \
     pynq_specialized_code/ps/pynq/{bin_net,bin_engine,bin_ref_numpy,bilinear_c,qcache}.py \
  | awk '{print $2}' | sort -u
# 再并上手写清单（这三个是"函数内惰性 import"，静态扫不到依赖关系以外的东西）
#   q8.py q8.c  c8deq.py c8deq.c  bpack.py bpack.c
```

**判据不是"文件都在"，而是下面两条（机制，不是纪律）**：

```python
# ① 每个模块必须解析到**冻结点目录内部**（防止又一次"用 ps/pynq 的"假通过）
import importlib.util as iu
for m in ("bin_net","bin_engine","bin_ref_numpy","bilinear_c","qcache","q8","c8deq","bpack"):
    p = iu.find_spec(m).origin
    assert p.startswith(FROZEN_DIR + "/"), (m, p)      # 闭包外一律拒绝
# ② 三个惰性内核必须**真的装上**（不是"能 import"）
import bin_engine as BE
assert BE._q8() and BE._deq_kernel() and BE._bpack(), "内核没装上"
```

板端**可复算**的一条命令（本次已实测通过）：
```bash
ls /home/xilinx/demoir/ps/pynq_frozen/{q8,c8deq,bpack}.py   # 今天：6 个都不存在
```

### ② 【根治】静默回落必须变成**显式可见 + 可 fail-fast**

- 默认（不改行为）：启动时**打一行**
  `[DEPS] q8=fast c8deq=fast bpack=fallback-numpy bilinear_c=fast  ⇒ deps_closure_ok=False`
  并写进 `runpoint.kernel_deps`（**这正是"测了两天假数"的机制性堵口**）。
- 加开关 `BIN_REQUIRE_KERNELS=1`：任一内核回落就**直接抛错**。
  用在"优化后必须全快路径"的跑点上（今天的 13.4 s 会当场变成异常，而不是一个漂亮的数字）。
- 这一条**不改任何算法行为**，只加"打印 + 一个字段 + 一个可选断言"。

### ③ 【根治】把 `numpy 跨步 × 非缓存缓冲` 这个组合**结构性禁掉**

- 任何"往 arena（非缓存）里写"的路径，都必须走 `q8.c8_into` / `c8deq.deq_into` / `bpack.pack_f32` 这类
  **一趟 burst** 的内核；numpy 跨步写法只允许出现在 `Bpf` 之外的临时**缓存**数组上。
- 判据（新的性能门禁）：`prologue/q8` 与 `epilogue` 若超过历史值 `1.5×`，**直接判跑点无效**，
  而不是"记下来以后再说"。

### ④ 【缓解，与本次无关但仍建议】arena 分配失败要 fail-fast，不要静默退回

`CmaFree` 与"arena 是否真的物理连续"要写进跑点；分配失败/退化必须抛错。
（H1 已被证伪，但"静默降级"是同一类 bug，且它确实让我们测了两天假数。）

### ⑤ 【纪律】不要再用"环境状态漂移"解释无法复现的跑点

今天之前的结论是"8.096 复现不出来，最可能是未记录的会话状态"。
**真实原因是依赖闭包残缺，且它一直写在目录清单里、只是没人看依赖。**

---

## 7. 复现命令（一条不落地）

```bash
# 0) 板子：探针（三个都在板端 /tmp/probe_cma/，本次全程未重启板子）
H='192.168.7.2'; PY=/usr/local/share/pynq-venv/bin/python3
SUDO() { echo xilinx | sudo -S -p '' env XILINX_XRT=/usr "$@"; }

# 1) 态 A（冻结点，缺依赖）—— 期望 prologue≈6.0 / epilogue≈1.19
tools/boardlock.sh ssh $H "$(declare -f SUDO); SUDO $PY -u /tmp/probe_cma/probe_cma.py --download"

# 2) 决定性 A/B（同进程只换 sys.path）—— 期望 prologue→1.61 / epilogue→0.26
tools/boardlock.sh ssh $H "SUDO $PY -u /tmp/probe_cma/probe_deps.py --download"

# 3) 逐模块隔离（每个一条命令，各自独立进程）
tools/boardlock.sh ssh $H "SUDO $PY -u /tmp/probe_cma/probe_arm.py --deps /tmp/probe_cma/deps_q8     --label q8only --download"
tools/boardlock.sh ssh $H "SUDO $PY -u /tmp/probe_cma/probe_arm.py --deps /tmp/probe_cma/deps_c8deq  --label c8deqonly"
tools/boardlock.sh ssh $H "SUDO $PY -u /tmp/probe_cma/probe_arm.py --deps /tmp/probe_cma/deps_bpack  --label bpaconly"

# 4) numpy 臂补齐（复现 8.096 那一档）—— 期望 8.26 s
tools/boardlock.sh ssh $H "SUDO $PY -u /tmp/probe_cma/probe_np.py"
```

---

## 8. 诚实边界（没做的事，不许当做过）

1. **没有重启板子**（这是判断，不是省事）：H1 的前提"CmaFree 饥饿态"在实测中不存在，
   重启无法再制造对照；决定性变量在 `sys.path`，与 boot 无关。
2. **没有改任何被测代码**：`ps/pynq/` 与 `frozen_code/` **一个字节都没动**；
   依赖是从 `ps/pynq/` **只读拷出**到 `/tmp/probe_cma/deps*/`，`.so` 也在 `/tmp` 里编译。
   （`ps/pynq/` 的 md5 未被触碰；本次也没有写入冻结目录。）
3. **没有拿到 `_pf` 亚项（q8/f32/desc/views）**：在 `BIN_PROFILE=1` 已生效的前提下，
   本次 `eng._prof` 与 `net._prep_prof` **都是 `{}`** ⇒ `prepare_segments` 里的
   `_pf["q8"]/["f32"]/["desc"]/["views"]` 打点没有落到 `self._prof`
   （最可能是 LAZY_DEQ 直读那条分支在打点之前就 `return None` 了；**我没有做代码级确认**）。
   ⇒ 所以本报告**不给**"q8 604→? ms"这一级的数字，归因改用**逐模块隔离**
   （更硬：它直接给出"补哪个模块 ⇒ 哪个桶回落"，一对一误差 0.3%），而不是靠亚项猜。
4. **没有验 C 臂的 numpy 组合全矩阵**：numpy 臂只补了"无依赖 / 全依赖"两点，没做 numpy×{q8,c8deq,bpack} 8 格。
5. **`e1e2_sim.py` / `catpack.py` / `board_safe.py` 也是闭包缺口**（冷路径，本次未触发）：
   静态扫描发现 `bin_engine` 还有 `from e1e2_sim import to_c8_padded`（`pack_segments`/`write_int8`）、
   `from catpack import CatPackEngine`（catpack 后端）、`from board_safe import Watchdog`（可选看门狗）。
   这三条**在今天的跑法里没被走到**（所以没造成额外偏差），但它们**同样是冻结点缺口**，属于埋着的雷。
6. **8.096 与 8.261 差 +2.0%**：不宣称"逐位复现"。差异来源未分离（`bin_ref_numpy` 版本不同 / 槽数记录不一致 /
   会话噪声），但方向与量级已闭合（`prologue 1.58 vs 1.625`、`epilogue 0.257 vs 0.259`、`interp 1.899 vs 1.938` 均在 1–3%）。

---

## 9. 事故档案（第二份：一次成功的取证与两次被否的间接推断）

### 9.1 被否掉的假设，以及它错在哪

| 假设 | 提出依据 | 否证方式 | 教训 |
|---|---|---|---|
| **H1：缓冲区不再 CMA 后备 ⇒ cache 维护变贵** | `prologue×3.68 / epilogue×4.60` 是"搬运侧"，而算法桶 1.00× ⇒ 指向 cache 维护 | ① arena PA `0x30100000` + `CmaFree` 精确掉 66 MB；② `flush()` 在 `coherent=True` 下是**空操作**（66 MB / 0.149 ms）；③ 非缓存缓冲**顺序**写只贵 1.22× | **"看起来像搬运" ≠ "搬运机制"**。看了一眼 `PynqBuffer.flush` 的源码，整条假设 3 分钟就死了 —— 而在此之前它已经被当成"最强假设"写进任务书 |
| 主代理的"CMA 被某个服务开机抓走 190 MB" | `CmaFree` 0.5 MB vs 昨天 168 MB | 全机进程 RSS 合计 253.9 MB；`CmaFree` 三次采样逐位不变；arena 一次拿到 66 MB | **`CmaFree` 低 ≠ CMA 不够**。CMA 是可被借用的 MOVABLE 内存，读一次 `CmaFree` 就下结论，等于把"页缓存借了它"误读成"有人抢走了" |
| "8.096 不可复现，可能是未记录的会话状态" | 换代码仍 13.6 s | 补齐依赖闭包后 6.497 / 8.261 | **"不可复现"应该先翻译成"缺了什么"**，而不是"环境里有什么不知道的东西"。前者可枚举，后者不可证伪 |

### 9.2 这次为什么能很快收敛（可复用的方法）

1. **先量"反证态"再谈重启**：一上来先读 `CmaFree` 的**多次**采样与 `flush` 的**实际耗时**，
   而不是照任务书去重启。（重启是不可逆操作，会毁掉同 boot 对照窗口。）
2. **读源码里那一行"是否真的发生"**：`if not self.coherent:` 这一行让 4.2 s 的假设当场归零。
3. **用"补什么就回落什么"的隔离实验代替"猜是哪段代码"**：`q8⇒prologue`、`c8deq⇒epilogue`，
   两条一对一，误差 0.3%，比任何 `perf`/`strace` 都直接。
4. **判据用比值，不用差值**：`3.69×` 对上了历史 `3.68×`、`4.61×` 对上了 `4.60×`
   —— 两个独立方向的比值同时命中，几乎不可能是巧合。

### 9.3 制度产出（必须落地，否则下次还会发生）

- 冻结点 = **依赖闭包**（`deps_closure.txt` + `md5sum -c` 全过 + `find_spec` 必须落在冻结点内）；
- **每个惰性内核的 `fast/fallback` 状态进跑点 JSON**，并打一行 `[DEPS] ...`；
- `BIN_REQUIRE_KERNELS=1` 的 fail-fast 开关；
- 跑点门禁：`kernel_deps` 有 fallback ⇒ 该跑点**不许进基线表**。

---

## 10. 产物

```
runs/board_ops/20261102/
├── REPORT_rootcause_missing_deps.md     ← 本文件
└── probe_cma/
    ├── probe_cma.py                     ← ①CMA 取证 + 立网测量（态 A）
    ├── probe_deps.py                    ← ②决定性 A/B（同进程只换 sys.path）
    ├── probe_np.py                      ← ③numpy 臂补齐（复现 8.096 档）
    ├── probe_arm.py                     ← ④单臂单进程（逐模块隔离）
    ├── stateA.log  probe_cma_stateA.json    ← 态 A 全部原始数据（含 env/CMA/buddyinfo/pagemap/flush 微基准）
    ├── deps.log    probe_deps.json          ← A/B 原始数据（含依赖清单 md5 + verdict）
    ├── np.log                               ← numpy 臂 8.261 s
    ├── arm_split.log                        ← q8-only / c8deq-only
    ├── arm_bpack.log                        ← bpack-only
    ├── deps_closure.txt                     ← 依赖闭包清单（md5sum -c 可核）
    └── MD5SUMS                              ← 本目录自校验
```

**板端**（重启后不保留）：`/tmp/probe_cma/{probe_*.py, deps/, deps_q8/, deps_c8deq/, deps_bpack/, *.json}`
**未改动**：`pynq_specialized_code/ps/pynq/`（只读拷贝）、`runs/board_ops/20261102/frozen_code/`（一个字节未动）
