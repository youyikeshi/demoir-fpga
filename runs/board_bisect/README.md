# 新镜像启动失败：二分测试记录（2026-11-03）

背景见 `runs/incident/20261103_1721_装卡后板子不启动.md`。

## 判据（无需串口）
| 观察 | 含义 |
|---|---|
| 链路 **100 Mbps** | Linux 的 ENET 驱动已接管并配置 PHY ⇒ **系统跑起来了** |
| 链路 **1000 Mbps** 且 ping 不通 | PHY 仅上电默认值 ⇒ **停在 Linux 之前**（FSBL/U-Boot/早期内核） |
| ping 通 + 22 可连 | 完全启动 |

> 该判据由 2026-11-03 的一次互换实验证实：旧镜像 100M、新镜像 1000M。

## 文件三件套（md5）
| 角色 | BOOT.BIN | image.ub | system.dtb |
|---|---|---|---|
| **旧（能跑）** | `c4c3d84e…` | `622dcdf1…` | `8c6a72fc…` |
| **新（挂死）** | `0fffd3fc…` | `a2b902e7…` | `90a8c5fa…` |

$$\Rightarrow\ \text{注意：} \texttt{image.ub}\ \text{是 FIT，}\textbf{自带一份 DTB} \Rightarrow \text{改 DT 必须重打 } \texttt{image.ub}$$
$$\Rightarrow\ \text{所以"只换 } \texttt{system.dtb}\text{"对内核实际生效有限} \Rightarrow \text{A/B 的变量是"}\texttt{image.ub}\text{ 里的内核+DT 整体"与"}\texttt{BOOT.BIN}\text{ 里的 U-Boot"}$$

## 测试矩阵
| # | BOOT.BIN | image.ub | system.dtb | 目的 | 结果 |
|---|---|---|---|---|---|
| 0 | 旧 | 旧 | 旧 | 基线（已验：能起、100M、ping 通） | ✅ 通过 |
| **A** | 旧 | **新** | 旧 | 元凶是否在 `image.ub`（内核+DT） | ✅ **能起**（见下） |
| B | **新** | 旧 | **新** | 元凶是否在新 `BOOT.BIN`（U-Boot 被重建） | 待测 |

### 测试 A 的三种可能结果与推论
| 结果 | 推论 |
|---|---|
| 100M + ping 通 | `image.ub` 无害 ⇒ **元凶是新 `BOOT.BIN`（U-Boot）** ⇒ 转测试 B |
| 100M + ping 不通 | Linux 起来了但网络没通 ⇒ 查 IP/接口名（**不是启动失败**） |
| 1000M + ping 不通 | `image.ub` 有害 ⇒ 元凶在内核或 DT ⇒ 用 `petalinux-build` 做"只回退 cfg / 只回退 DT"两个增量变体 |

## 测试 A 装载记录
- 时间：2026-11-03 17:42
- 卡上三件：`BOOT.BIN c4c3d84e…`(旧) / `image.ub a2b902e7…`(**新**) / `system.dtb 8c6a72fc…`(旧)
- 保留件未动：`boot.scr c63e98a3…` / `uEnv.txt 2db67850…` / `system.dtb.bak 8c6a72fc…`
- 写入后 `sync` + 回读 md5 逐个校验通过

---

## 测试 A 结果：**通过（能起）** —— 2026-11-03 17:43

监视器原始采样（3 s 一次，只在状态变化时打印）：
```
[17:42:35] ping=0 link=down carrier=0      ← 断电/插卡
[17:43:35] ping=0 link=1000M carrier=1     ← 上电，PHY 默认值
[17:43:43] ping=0 link=100M  carrier=1     ← 8 秒后降到 100M ⇒ Linux 的 ENET 接管了
[17:43:46] ping=1 link=100M                ← ✅ 完全启动
```

$$\Rightarrow\ 	ext{上电 } 	o 1000\ 	ext{M（PHY 默认）} 	o \mathbf{100\ M}（	ext{Linux 接管）} 	o 	ext{ping 通}$$
$$\Rightarrow\ 	extbf{这是"Linux 真的跑起来了"的完整指纹} \Rightarrow 	ext{比单看"最终速率"更硬}$$
$$\Rightarrow\ \Rightarrow\ 	extbf{新 } 	exttt{image.ub}（	ext{新内核 + 新 DT}）	extbf{是无害的}$$

## 推论：元凶锁定在 `BOOT.BIN`（或 FAT 上的 `system.dtb`）

| 组合 | BOOT.BIN | image.ub | system.dtb | 结果 |
|---|---|---|---|---|
| 全旧 | 旧 | 旧 | 旧 | ✅ 起 |
| **A** | 旧 | **新** | 旧 | ✅ **起** |
| 全新 | **新** | 新 | **新** | ❌ 挂死 |

$$\Rightarrow\ 	ext{三者相减} \Rightarrow 	ext{差异只剩 } \{ 	exttt{BOOT.BIN}, \ \texttt{system.dtb} \}$$
$$\Rightarrow\ 	ext{而 } 	exttt{system.dtb}\ 	ext{只被 } 	exttt{boot.scr}\ 	ext{fatload 到 } 0x100000\ 	ext{当 } 	exttt{fdtcontroladdr}\ 	ext{用（\ 供 bootargs）}$$
$$\Rightarrow\ \Rightarrow\ 	extbf{最可能是新 } 	exttt{BOOT.BIN}\ 	ext{里的 U-Boot（本次被重建，含新 DT）}$$

## ★ 重要工程结论（先于进一步二分）

$$\Rightarrow\ 	ext{新内核 + 新 DT 已经在}	extbf{旧 } 	exttt{BOOT.BIN}\ 	ext{下跑起来了}$$
$$\Rightarrow\ 	ext{而 USB 需要的 } 	exttt{dr\_mode}/	exttt{usb-phy}/	exttt{phy0}\ 	ext{在 } 	exttt{image.ub}\ 	ext{自带的 DTB 里} \Rightarrow 	extbf{Linux 侧已具备条件}$$
$$\Rightarrow\ \Rightarrow\ 	extbf{不需要新 } 	exttt{BOOT.BIN}\ 	ext{就能测 USB} \Rightarrow 	ext{老 } 	exttt{BOOT.BIN}\ 	ext{继续用}$$
$$\Rightarrow\ 	ext{除非后续要改 FSBL/U-Boot，否则}	extbf{根本不必重打 BOOT.BIN}$$
