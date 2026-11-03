# MaixCAM 采集服务真机验证报告（2026-11-03 19:30）

> 环境：MaixCAM Pro 直连 PC（`10.13.148.1`，NCM 口），固件 Buildroot / MaixPy 4.12.5。
> 服务：`apps/maixcam/cam_shot.py` 1.1.0-preflight，2K 原生档（`--native 2560x1440 --settle 4`）。

## 0. 结论先说

| 项 | 结论 |
|---|---|
| **裁剪是像素级子集（无 resize）** | ✅ **PASS 3/3 逐字节相同**（两次独立验证：重启前后各一次） |
| **端到端取图（2K 原生 → 256² → 传输）** | ✅ **358~373 ms，avg 364 ms，10/10 成功** |
| **连续 10 张内存** | ✅ **完全不变**（`MemFree` 41→41 MB，`MemAvailable` 66→66 MB）⇒ 无泄漏 |
| **图像真实性** | ✅ 10/10 md5 互不相同；`mean≈98`、`std≈45`；PNG 肉眼确认为真实场景 |
| **两次"卡死"的真实原因** | ✅ **OOM killer 杀掉 launcher（UI）**，不是内核挂死 |
| **曝光参数 `exp_us`** | ⚠️ 有作用但**方向与直觉相反**，未定性 ⇒ 不可用于定量控制 |

---

## 1. 裁剪正确性（这条是整条链路的前提）

`cam_shot.py --check-crop`：取一张全幅，用 `img.crop()` 与"全幅切片"分别取同一窗口，逐字节比较。

```
[check-crop] 全幅 to_bytes 11059200 B (10.5 MB) 167 ms
[check-crop] PASS  center 256x256             rect=(1152,592,256,256) 196608 B 21.15 ms
[check-crop] PASS  center 512x512             rect=(1024,464,512,512) 786432 B  6.89 ms
[check-crop] PASS  explicit(777,333) 320x180  rect=(777,333,320,180) 172800 B  1.26 ms
[check-crop] 结论：PASS（3/3 例逐字节相同）⇒ crop 是像素级子集，没有 resize
RC=0
```

窗口计算自洽：`(2560-256)//2 = 1152`、`(1440-256)//2 = 592`。

**意义**：去摩尔纹的输入必须是"原生像素子集"。若 MaixPy 在这里偷偷做插值/缩放，
摩尔纹的频率成分会被改掉，数据集与算法输入分布就不一致 ⇒ 整个算法结果无效。
**这条 PASS 之后，"256² 输入 = 原生像素子集"可以当已证事实使用。**

---

## 2. 端到端时延（10 张，2K 原生，256² center）

| # | 耗时 ms | md5(前12) | mean | std | MemFree |
|---:|---:|---|---:|---:|---:|
| 1 | 373 | `0f67bef21bd2` | 98.06 | 44.64 | 41 |
| 2 | 368 | `8d58bae7f66a` | 98.41 | 44.99 | 41 |
| 3 | 364 | `1aed41f8e6df` | 98.90 | 45.32 | 41 |
| 4 | 363 | `d3fe131c3bc8` | 98.69 | 45.17 | 41 |
| 5 | 365 | `2b395ff388a4` | 97.43 | 44.67 | 41 |
| 6 | 362 | `0b54c252c89d` | 97.52 | 44.74 | 41 |
| 7 | 364 | `2f28990b1bd9` | 98.62 | 45.05 | 41 |
| 8 | 363 | `8630174683ef` | 98.95 | 45.14 | 41 |
| 9 | 358 | `6dfb78836961` | 98.41 | 45.03 | 41 |
| 10 | 361 | `7548ce3970e9` | 98.52 | 45.27 | 41 |

- **min/avg/max = 358 / 364 / 373 ms**
- **不同 md5 数 = 10/10**（活体相机，非缓存）
- 首末 `MemFree` **41 → 41 MB**、`MemAvailable` **66 → 66 MB** ⇒ **零漂移，无泄漏**
- 其中 `settle=4`（丢 4 帧等 AEC/AWB）约占 **130 ms** ⇒ 真正的取图+裁剪+发送约 **230 ms**

$$\Rightarrow\ \text{协议文档原先的「}0.1\sim0.5\ \text{s」推测}\Rightarrow \text{实测落在区间内且偏中间}$$
$$\Rightarrow\ \textbf{结论：364 ms/张，可用}$$

若去掉 settle：预计 ~230 ms，但 AEC/AWB 收敛变差 —— 未测，不声称。

---

## 3. ★ 两次"卡死"的真实原因：OOM 杀掉 launcher，不是内核挂死

### 3.1 决定性证据（`dmesg`）
```
[  267.492852] udhcpc invoked oom-killer: gfp_mask=...(GFP_KERNEL_ACCOUNT|__GFP_ZERO), order=2
[  267.493897] Out of memory: Killed process 297 (launcher) total-vm:224432kB, anon-rss:82612kB
[  350.404667] cut invoked oom-killer: gfp_mask=... order=0
[  350.406034] Out of memory: Killed process 1655 (launcher) total-vm:223532kB, anon-rss:82040kB
```
$$\Rightarrow\ \text{被杀的是 } \texttt{launcher}\ (\text{UI})\ \text{，}\textbf{anon-rss 82 MB}\ (\text{占 128 MB 的 }64\%)$$
$$\Rightarrow\ \text{UI 死掉} \Rightarrow \text{屏幕报 } \texttt{run app failed: code:-1} \Rightarrow \text{看起来"设备卡死"}$$
$$\Rightarrow\ \textbf{而 SSH 与内核全程活着}$$

### 3.2 为什么第一次会 ping 也不通
两次故障是同一条因果链的两个阶段：

| 阶段 | 现象 | 说明 |
|---|---|---|
| 19:14~19:16 | ping 0% 丢包、RTT 1.89 ms；但 TCP 22/21/1901/7899 **accept 后不发 banner** | 内核栈活着、**用户态饿死** |
| 19:16~19:18 | ping 也开始 100% 丢包 | 内存持续紧张，连网络处理都发不出来 |

### 3.3 量化：为什么必须停 launcher

| 场景 | MemFree | MemAvailable |
|---|---:|---:|
| 刚开机（launcher 在跑，无 swap） | **6~13 MB** | 54~70 MB |
| `kill -TERM` launcher + `drop_caches` 后 | **41~44 MB** | 66~84 MB |

$$\Rightarrow\ \text{MMF 启相机要}\textbf{连续物理块}：\text{日志 } \texttt{BlkSize 5652480}\times3\ (5.4\ \text{MB}\times3)$$
$$\Rightarrow\ \text{可用 }70\ \text{MB 但 } \texttt{MemFree}\ \text{只有 }6\ \text{MB} \Rightarrow \text{连续块拿不到} \Rightarrow \text{内核回收 page cache} \Rightarrow \text{长停顿}$$
$$\Rightarrow\ \texttt{drop\_caches}\ \text{后 } \texttt{MemFree}\ 13 \to 42\ \text{MB} \Rightarrow \text{相机一次打开成功（}2369\ \text{ms）}$$

**⇒ 绑定约束不是 `MemAvailable`，而是 `MemFree` 与连续可分配性。**

### 3.4 处置（已验证有效）
```bash
sync; echo 3 > /proc/sys/vm/drop_caches          # MemFree 13 → 42 MB
kill -TERM <launcher_pid> <launcher_daemon_pid>  # 释放 82 MB
./deploy.py shot                                  # 之后 10/10 稳定
```
停 launcher 后**没有重启自拉起**（daemon 也 TERM 掉了），服务与 SSH 均正常。

---

## 4. 图像真实性（防"md5 对但图是垃圾"）

`png/shot_01..05.png` 已保存。肉眼确认：**真实照片**（有传感器噪声与渐变），
不是纯色/全黑/花屏。`mean≈98`、`std≈45` 与"有明暗层次的场景"一致。

$$\Rightarrow\ \text{当前相机对着的是}\textbf{没有屏幕的平面} \Rightarrow \text{图里}\textbf{没有摩尔纹}$$
$$\Rightarrow\ \textbf{真机 demo 前必须把相机对准显示器} \Rightarrow \text{这是下一次的事，不是缺陷}$$

---

## 5. 曝光参数：有作用，但方向反直觉（未定性）

| 参数 | mean | std |
|---|---:|---:|
| `exp_us=100` | 91.27 | 38.21 |
| `exp_us=20000` | 52.77 | 17.83 |
| 差 | **−38.50** | −20.38 |

$$\Rightarrow\ \text{长"曝光"反而}\textbf{更暗更平} \Rightarrow \textbf{不能按"值越大越亮"使用}$$
$$\Rightarrow\ \text{可能原因：参数语义不是时间 / 与 AEC 互相覆盖 / 未等收敛}$$
$$\Rightarrow\ \textbf{结论：不要依赖 } \texttt{exp\_us}\ \text{做定量控制} \Rightarrow \text{要定性需单独一轮实验}$$

---

## 6. 与之前报告的不一致处（如实列出）

| 我/主代理当时的说法 | 实测/修正 |
|---|---|
| "设备挂死了（内核级）" | ❌ 是 **OOM 杀掉 launcher**，内核与 SSH 活着；只有第二阶段才 ping 不通 |
| "建议 `fuser -v /dev/video*` 查占用" | ❌ MaixCAM **没有 `/dev/video*`**（走 MMF/VI 不走 V4L2） |
| "重启后设备是干净的" | ❌ 当时服务已由代理重新启动（pid 1286），不是干净状态 |
| "取图 0.1~0.5 s" | ⚠️ 猜测；实测 **364 ms** |
| `shot.log` 0 字节 | ✅ 第一版确实无日志；当前版本已有 `[boot]` banner + `[preflight]` 内存门禁 |

---

## 7. 对板端（AX7015B）的直接影响

$$\Rightarrow\ \text{板上 } 1\ \text{GB 内存，比相机多 }8\times \Rightarrow \text{压力小很多}$$
$$\Rightarrow\ \textbf{但同样无 swap} \Rightarrow \text{连续分配问题一样存在（板侧是 } \texttt{CMA}\text{）}$$
$$\Rightarrow\ \Rightarrow\ \text{上板时必须看 } \texttt{CmaFree}\ \text{而不是 } \texttt{MemAvailable} \text{（契约 §8.19 同类教训）}$$
$$\Rightarrow\ \text{板侧已设 } \texttt{CMA}=192\ \text{MB}，\text{而本轮相机侧实测"取图+裁剪"只占稳态 } 0\ \text{MB 增长} \Rightarrow \text{不需要额外 CMA}$$
$$\Rightarrow\ \text{建议板上起服务前也 } \texttt{drop\_caches}\ \text{并检查 } \texttt{CmaFree} \ge 32\ \text{MB}$$

---

## 8. 数据文件
| 文件 | 内容 |
|---|---|
| `shots.json` | 10 张逐张的耗时/md5/mean/std/MemFree |
| `png/shot_01..05.png` | 前 5 张原图（256²） |
| `png/exp_100us.png` / `png/exp_20000us.png` | 曝光对比两张 |
