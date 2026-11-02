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

**构建时把 RTL 根指到可用源，不要指 `pl/rtl_hand/modules/`：**

```
# 可用 RTL（= v39 / v42e 的 RTL，逐字节相同）
worktrees/T-51-v42e/pynq_specialized_code/pl/rtl_hand/modules/
# 或直接从 git 取
git archive b58964e pynq_specialized_code/pl/rtl_hand/modules/
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
