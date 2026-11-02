# ⚠ RTL 状态：**HEAD 的 RTL 不能产出可用位流**（2026-11-02）

> **任何构建之前先读这一页。** 这是一次真实的踩坑（T-56 11:07 那次构建），代价 22 分钟。

## 事实

$$\Rightarrow\ \text{唯一能跑的位流是 v39（}\texttt{602725b8}\text{），它由 commit } \mathbf{\texttt{b58964e}}\ \text{的 RTL 产出}$$
$$\Rightarrow\ \text{而当前 } \texttt{HEAD}\ \text{的 } \texttt{pl/rtl\_hand/modules/}\ \text{里}\textbf{带着 T-34/T-36 的改动，这些改动已在板端证明致命} $$

| 文件 | b58964e（可用）| HEAD（致命）| 后果 |
|---|---|---|---|
| `bin_unit_axi_wrap.v` | `PF_AHEAD=1`、`PF_DEPTH=4`、`AR_SLOTS=4` | **`PF_AHEAD=2`、`PF_DEPTH=8`、`AR_SLOTS=8`** | v42/v42c/v42e：150 MHz 挂死、100 MHz 数据错（非确定）；`pf_tag_reg` 违例 0→210 |
| `bin_e1e2.v` | — | **+276/−14 行**（T-36 的空 lane + 组自增） | v42 里未单独归因（被预取掩盖），但**从未在板上验过正确性** |

$$\Rightarrow\ \Rightarrow\ \textbf{从 HEAD 直接构建 = 一定得到一颗不可用的位流}$$

## 正确的做法

**⚠⚠ 更正的更正（2026-11-02 12:25，T-60 逐字节查证）：`b58964e` 也不是 v39 的 RTL。**

$$\texttt{b58964e}\ \text{= v39 + v40 的两把时序刀}$$
$$\Rightarrow\ \text{刀7：}\texttt{bin\_e1e2.v}\ \text{把 SWEEP 期组合比较改成寄存倒计数器（自述"消 7,124 条端点"）}$$
$$\Rightarrow\ \text{刀8：}\texttt{bin\_unit\_axi\_wrap.v}\ \text{给 } \texttt{run\_unit\_q/sel\_e1e2\_q/sel\_cp\_q}\ \text{加 } \texttt{max\_fanout=32}$$
$$\Rightarrow\ \text{用 b58964e 构建：10 ns WNS } +0.090（\text{v39 为负}）、150\ \text{MHz } -3.243\ (\text{v39 } -5.331)、\text{N } 9452\ (\text{v39 } 20706)$$
$$\Rightarrow\ \Rightarrow\ \textbf{任何"复现 v39"的任务用 b58964e 都会得到不同的电路}$$

**唯一权威 = 位流自己的 manifest**：`runs/v39_*/build_v39_manifest.txt`（构建时生成，记录实际输入）

| 文件 | v39 manifest（真值）| `b58964e`（v39+v40刀）|
|---|---|---|
| `bin_unit_axi_wrap.v` | **`0cf68b2e31ac106ed03e30bd5591504d`** | `f4ba0655` |
| `bin_e1e2.v` | **`28375eec288f067e9235c0ecd486abb5`** | `bc28ead3` |
| 其余 23 个 `.v` | 与 `b58964e` **逐字节相同** | 相同 |

$$\Rightarrow\ \text{这两个真实 v39 字节存在于 } \texttt{pl/rtl\_hand/.snapshots/verified\_v39/}\ \text{，与 manifest 逐条 MATCH}$$

**正确的做法（复现 v39 的 RTL 根）**
```
# .snapshots/verified_v39/ 的两个文件 + b58964e 的其余 23 个 = 25 文件逐字节 = v39 构建输入
# 构建前硬断言：对 build_v39_manifest.txt 每一条 (file, md5) 比对，25/25 全过才许构建
```

$$\Rightarrow\ \text{v42e 的位流（}\texttt{e08a4e0e}\text{）就是这个 RTL + 只改一行 } \texttt{PF\_AHEAD=2} \Rightarrow \text{它的 RTL 基线是干净的}$$
$$\Rightarrow\ \text{T-48 的 v42d、T-46 的 v42c 也都以 } \texttt{b58964e}\ \text{为基线}$$

## 为什么没有直接 revert

$$\Rightarrow\ \text{T-34/T-36 的工作有存档（} \texttt{runs/t38\_rtl/t38\_rtl.patch}\ \text{37,082 B + } \texttt{worktrees/T-36-e1e2}）\Rightarrow \text{不是丢失}$$
$$\Rightarrow\ \text{但其中预取那一半}\textbf{已被板端判死}（\text{4 颗位流、5 轮上板}）\Rightarrow \text{将来若要做预取，必须}\textbf{重新设计 + 乱序 AXI 仿真门禁}，\text{不能从这份改动续做}$$
$$\Rightarrow\ \text{`bin_e1e2` 那一半（空 lane + 组自增）}\textbf{仍未被公正测试}——\text{要测它，必须用 } \texttt{v42f = b58964e + 只加 e1e2 改动}\ \text{单独一颗}$$

## 判据（写进任何构建任务书）

$$\Rightarrow\ \textbf{构建脚本必须打印它实际用的 RTL 根的 md5}，\text{并与下面三个值比对}$$
$$\quad\ \texttt{bin\_unit\_axi\_wrap.v}\ (\text{v39/b58964e})\ \text{与}\ \texttt{bin\_e1e2.v}\ (\texttt{bc28ead39b41e79f18571bff46bbd147})$$
$$\Rightarrow\ \textbf{若构建任务改动了 RTL 根，必须在报告里显式写出"换掉了哪几个文件、为什么"}$$
$$\quad\ \text{——T-56 那次就是"顺手把 } \texttt{\$rtl}\ \text{指向权威源"，\text{同时动了 BD 与引擎两个变量}}$$

---

## 三次"我以为的权威源其实不是"（2026-11-02）

| # | 我以为 | 实际 | 后果 |
|---|---|---|---|
| 1 | `worktrees/T-51-v42e/.../modules` = v39 | **v39 + `PF_AHEAD=2`**（`6ecbc5dc`）| `v43base` 挂死，实验作废 |
| 2 | `git archive b58964e` = v39 | **v39 + v40 刀7/刀8**（`f4ba0655`/`bc28ead3`）| 时序差异被误记到 BD 上 |
| 3 | `pl/rtl_hand/modules/`（HEAD）= 权威 | **v39 + T-34/T-36 的致命改动** | 第一次构建 WNS −1.471，`pf_tag` 0→210 |

$$\Rightarrow\ \textbf{第 106 条纪律：判据必须是"位流自己的 manifest"，不是 commit 名、不是目录名、不是 HEAD} $$
$$\quad\ \text{位流是最终产物} \Rightarrow \text{记录它输入的是什么的那份 manifest，才是唯一权威}$$
$$\quad\ \Rightarrow\ \text{任何"复现/对比/中性验证"的任务，第一步是把 manifest 里的每一条 md5 逐条比对}$$
$$\quad\ \Rightarrow\ \text{对不上就先解决"输入对不对"，别急着构建}$$
