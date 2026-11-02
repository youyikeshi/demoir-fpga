# 到 1 秒的可执行计划（2026-11-02 13:45 制定）

> 与 `ROADMAP_to_1s.md`（地图）的区别：这一份是**可派单的执行计划**——每个里程碑有编号、判据、失败模式、依赖。
> 所有起点的数字都是**板端实测**；所有投影都标注来源与区间。

---

## 0. 起点（实测，v5 运行点，tag `baseline-v5`）

$$\textbf{v5} = 6.03\ \text{s}\quad(\text{最好 session } \approx5.83;\ \text{典型 } 6.0\sim6.3)$$

| 组件 | 秒 | 性质 |
|---|---:|---|
| PS `prologue` | **1.14** | 量化/prep（q8 0.604 + aq8 0.172 + q8w 0.120 + …）|
| PS `convfp` | **1.15** | 4 层浮点卷积（CPU 已挖到底，只能搬 PL）|
| PS `interp` | 0.26 | ✅ C 内核，已优化 |
| PS `sam` | 0.245 | 逐元素，带宽地板 |
| PS `epilogue` | 0.22 | ✅ 已削 0.046 |
| PS `eca` / `add` / `cat` / `shuf` | 0.088 / 0.064 / 0.037 / 0.014 | ✅ cat 已削 0.10 |
| PL `pack`（E1E2）| **1.08** | 硬件 0.33 + 软件访问 0.57 + 层间 |
| PL `conv` | **1.12** | 算力地板 0.377 + **残差 0.74（机制未证）** |
| `wait` / `overlap` | 0.6 / — | 依赖链 |

$$\Rightarrow\ \text{PS 侧合计 } \approx3.2\ \text{s（其中 }1.15\ \text{是浮点 conv、}1.14\ \text{是 prologue）}$$
$$\Rightarrow\ \text{真实 PL 工作量 } \approx1.4\ \text{s（其余是等待与重叠）}$$

---

## 1. 三个硬约束（已查清，不再试探）

| # | 约束 | 结论 |
|---|---|---|
| **C1** | **HP 主口只有 2 条**（HP0→bin、HP1→conv_uni），共享因 MI ID 需 7 bit > 6 bit 而**算术不可行** | 每加一个 PL 主设备 ⇒ **必须新开从口**（HP2/HP3）。**BD 已可重生成且证中性**（`v43base2`，上板 ±0.77%）⇒ 这条路通了 |
| **C2** | **无 DVFS**，DDR 实测有效带宽 409~480 MB/s | PS 侧任何"搬字节"的优化都卡在这个数；**算字节就能判一条优化值不值** |
| **C3** | conv 引擎 **512 MAC/拍**（8 lane × 64-bit popcount）= 0.377 s 地板 | conv 现在 1.12 s ⇒ **残差 0.74 s**，机制未证（预取那条已被判死，见 §4） |

---

## 2. 里程碑（按"能拿到多少 / 风险"排序）

### M1 `convfp → PL`（−0.34 s，**进行中**）
$$\Rightarrow\ \text{4-lane fconv}：\text{PL } 807.5\ \text{ms}\ \text{vs PS } 1.155\ \text{s} \Rightarrow \text{净 } \mathbf{-0.34}$$
$$\Rightarrow\ \text{前置已满足：BD 可重建（中性已证）、HP2 可开、fconv RTL 已验逐字节}$$
$$\Rightarrow\ \text{当前阻塞：} \texttt{v44}\ \text{构建在 } \texttt{synth\_design}\ \text{失败——} \texttt{fconv}\ \text{的 module-ref IP 缺 } \texttt{.dcp}（\texttt{synth\_ip}\ \text{对 IPI 内部核不可用 12-3424}）$$
$$\Rightarrow\ \textbf{判据}：\text{既有项 0 变化} + \text{资源 LUT} \le 38{,}000 + \text{G2 五组无 PASS→FAIL} + \text{上板 fconv 逐字节}$$
$$\Rightarrow\ \textbf{失败模式}：\text{HP2 在 stock PYNQ BOOT.BIN 下 AFI 未初始化} \Rightarrow \text{先做 4 KB 冒烟（文档已交付）}$$

### M2 `pack 命令数 698 → 227`（−0.2~0.37 s）
$$\Rightarrow\ \text{471 条是同一层内的重复遍历（} \texttt{E\_DESC += 64}\ \text{等差）};\ \text{每条命令墙钟 } 1.277\ \text{ms}$$
$$\Rightarrow\ \text{RTL（组自增）已在 v42 里编译通过，但 } \textbf{PS 侧契约未改} \Rightarrow \text{需要新寄存器接口 + 驱动改造}$$
$$\Rightarrow\ \textbf{前置}：\text{必须先出 } \texttt{v42f = v39 + 只加 e1e2 组自增}\ \text{的单变量位流，验 sha1 + 测收益}$$
$$\Rightarrow\ \text{⚠ 注意：T-36 的 e1e2 改动}\textbf{从未被单独公正测试过}（v42 里被预取错误掩盖）$$

### M3 `prologue 1.14 → ~0.55`（−0.59 s）
$$\Rightarrow\ \text{三块：} \texttt{q8}\ \text{量化 } 0.604、\texttt{aq8}\ 0.172、\texttt{q8w}\ 0.120$$
$$\Rightarrow\ \text{搬法：}\textbf{conv 写侧直产 E1E2 布局}（\text{免掉"写 f32 再读回量化"两趟）} + \text{量化搬进 PL}$$
$$\Rightarrow\ \text{已评估的代价：}+150\ \text{LUT};\ 64^2\ \text{需 } +8\ \text{RAMB36};\ 128^2\ \text{需 } +32$$
$$\Rightarrow\ \textbf{判据}：\text{prologue } \le 0.6\ \text{s} + \text{逐字节一致} + \text{conv 不退化}$$

### M4 `conv 残差 0.74 s`（机制未证，**风险最高**）
$$\Rightarrow\ \text{现状：} \text{conv } 1.12\ \text{s}\ \text{vs 地板 } 0.377 \Rightarrow 3.0\times$$
$$\Rightarrow\ \text{预取那条已判死（4 颗位流 5 轮上板）} \Rightarrow \textbf{需要先能看见}：\text{ILA 或"安全"只读计数器}$$
$$\quad\ (\text{v42d 证明直接监听从数据通路读信号会改时序 ⇒ 必须 } \texttt{KEEP\_DUPLICATE}\ \text{或 ILA})$$
$$\Rightarrow\ \textbf{本里程碑的第一件事不是优化，是}\textbf{取得可信观测量}$$

### M5 `全融合`（−0.8~1.2 s，1 秒的必经之路）
$$\Rightarrow\ \text{描述符链 + 一次启动（消灭 925 条 MMIO 命令）}$$
$$\Rightarrow\ \text{pack 与 conv 合并（写侧直产 E1E2，不落 DDR 再读）}$$
$$\Rightarrow\ \text{sam/interp/eca/add 融进上下游读写窗口}$$
$$\Rightarrow\ \text{技术要求：常驻描述符存 + 缓存一致性协议 + mux 初始状态 + 失败停止行为}（\text{T-19 已给语义坑})$$

### M6 `算力/时钟`（−0.3~0.9 s）
$$\Rightarrow\ \text{16-lane：}0.377\to0.19\ (\text{+~8k LUT})$$
$$\Rightarrow\ \text{或 200 MHz：PL 全线 } -25\%$$
$$\quad\ (\text{v43base2 在 10 ns 下 WNS } -2.140 \Rightarrow \text{离"能上 150 MHz 诚实约束"还差一段，需要专门收时序})$$

---

## 3. 累计算术（诚实版）

| 阶段 | 起点 | 增量 | 到达 | 依据强度 |
|---|---:|---:|---:|---|
| 现在 | — | — | **6.03** | 实测 |
| M1 convfp→PL | 6.03 | −0.34 | **5.69** | 实测 RTL + 待上板 |
| M2 pack 命令 | 5.69 | −0.20~0.37 | **5.3~5.5** | 参数计算 |
| M3 prologue | 5.3~5.5 | −0.59 | **4.7~4.9** | 字节账 |
| M4 conv 残差 | 4.7~4.9 | −0.2~0.5 | **4.2~4.7** | **机制未证** |
| M5 全融合 | 4.2~4.7 | −0.8~1.2 | **3.0~3.9** | 设计目标 |
| M6 算力/时钟 | 3.0~3.9 | −0.5~0.9 | **2.1~3.4** | 设计目标 |

$$\Rightarrow\ \textbf{诚实结论}：\text{按现有五个里程碑走完，落在 }\mathbf{2.1\sim3.4\ s}$$
$$\Rightarrow\ \text{要到 }\mathbf{1.0\ s}\ \text{还需要}\textbf{两件本计划尚未覆盖的事}：$$
$$\quad\ \text{① 把剩余 PS 桶（} \texttt{interp 0.26}、\texttt{sam 0.245}、\texttt{epilogue 0.22}\text{）也搬进 PL}（\text{再 } -0.5）$$
$$\quad\ \text{② 全流水线在 } 200\ \text{MHz 下收时序}（\text{PL } -25\%）$$
$$\Rightarrow\ \text{理想地板（全部在 PL、16 lane、200 MHz）：}\ \text{conv } 0.19 + \text{convfp } 0.34 + \text{其余 } 0.2 \approx \mathbf{0.75\ s}$$
$$\quad\ \Rightarrow\ \textbf{1 s 在原理上可达，但要求整条流水线重写}$$

---

## 4. 永久排除（别再花时间）

| 路线 | 结论 | 证据 |
|---|---|---|
| convfp 换 CPU NEON 内核 | **+0.17 s** | `prof.convfp` 1.155→1.320，两臂复现 |
| 轮询减访问 `POLL=est` | **+0.33 s** | 轮询读在等待窗口内，免费 |
| 预取 4 种参数组合 | **全部致命** | 150 MHz 挂死 / 100 MHz 数据错；4 颗位流 5 轮上板 |
| 只读诊断计数器 | **越界写** | 观测器给读通路加扇出 |
| `sam` 分片 / `deq` tile | +0.030 / +2.4% | A9 上都是负的 |

---

## 5. 执行规则（本轮新增，全部有血的教训）

$$\Rightarrow\ \textbf{R1 RTL 源必须用 manifest 验证}：\text{构建脚本开头逐条比对位流 manifest，25/25 才许构建}$$
$$\Rightarrow\ \textbf{R2 一次只动一个变量}：\text{BD、RTL、参数三者不许同时改}$$
$$\Rightarrow\ \textbf{R3 新位流分档点亮}：\text{load} \to 64^2\times2 \to 256^2\times2 \to \times3；\text{每档"两次 sha1 必须相同"}$$
$$\Rightarrow\ \textbf{R4 金丝雀零容忍}：\text{报警一次即停，不许解释成噪声}$$
$$\Rightarrow\ \textbf{R5 一个目标一个代理}：\text{派活前查归属台账 + 给每个代理独立日志路径}$$
$$\Rightarrow\ \textbf{R6 主工程不许被实验碰到}：\text{脚本第一行必须是副本路径}$$
$$\Rightarrow\ \textbf{R7 判据用语义量不用产物指纹}：\texttt{.hwh}\ \text{不可逐字节复现}$$
