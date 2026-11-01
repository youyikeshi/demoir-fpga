# 12 · 二值卷积（`bin_conv_core`）上板 Runbook

> 生成：2026-10-30 | 板子：`192.168.7.2`（PYNQ 3.1.0 / Zynq-7000 armv7l）
> 对应 PS 驱动：`ps/pynq/bin_engine.py`（`BinConvEngine`）+ `bin_test_staged.py`
> 对应 PL：`pl/rtl_hand/modules/bin_conv_core.v`，BD 实例名 **`bin_unit_0`（只有一个）**

---

## 0. 这份文档的边界（先读，违反会挂死板子）

本次 PS 侧准备**只做**了：文件传输、环境勘察、纯 Python 导入/数值测试、静态检查、写文档。

**没有做，也不允许做**：

| 禁止项 | 原因 |
|---|---|
| `pynq.Overlay(...)` / `fpga_manager` / `xmutil` / 写 `/sys/class/fpga_manager/*` | 加载 bit 归 PL 负责人；现场 PL 里是旧设计 |
| `pynq.DMA` / `pynq.allocate` 的 DMA 语义 / `register_map` 的 dma 段 | DMA 误配置会写坏 DDR |
| 写 `/dev/mem`、mmap 物理地址 | 同上 |
| `pip install` | 板子可能没网；缺什么只记录（第 6 节） |
| 任何"需要重启才能恢复"的操作 | 用户可能不在板子旁边 |

> 实测：本次全部操作结束后，`/sys/class/fpga_manager/fpga0/state` 仍是 **`unknown`**，
> 即**没有加载任何比特流**。旧的 `demoir_bundle/ demoir_k5/ demoir_new/ demoir_v2/`
> 目录一个字节都没动。

---

## 1. 板子环境快照

### 1.1 硬/软件版本

```
$ uname -a
Linux pynq 6.12.40-xilinx-g31626ef92ff1 #1 SMP PREEMPT Fri Nov 7 15:28:23 UTC 2025 armv7l

CPU         2 × ARMv7 Processor rev 0 (v7l)          # 双核 Cortex-A9
内存        1004 MB total / 873 MB available / 511 MB swap
磁盘        /dev/root  28G  已用 6.8G  可用 20G (26%)
PYNQ        3.1.0        （无 /etc/pynq_version，3.x 用 pynq.Overlay）
Python      3.10.4       （/usr/local/share/pynq-venv/bin/python3）
numpy       1.21.5       ← 注意：< 2.0，没有 np.bitwise_count
scipy       1.8.0
matplotlib  3.5.1
Pillow      9.0.1
torch       **没有**（ModuleNotFoundError）
onnx/opencv **没有**
ssh 22 ✓    Jupyter 9090 ✓（0.0.0.0 监听）
```

### 1.2 PL / overlay 现状

```
$ cat /sys/class/fpga_manager/fpga0/state
unknown                        # ← 干净状态，没有 overlay 被 PYNQ 加载过
$ cat /sys/class/fpga_manager/fpga0/name
Xilinx Zynq FPGA Manager
$ which xmutil
（无）                          # PYNQ 3.1 镜像不带 xmutil，别按 Kria 的流程找它
$ find /home/xilinx -maxdepth 2 -name 'global_pl_state*.json'
（无）                          # 佐证：本次开机后没加载过 overlay
```

> 结论：`fpga_manager` 是空的。**"PL 里现在是什么"只能由 PL 负责人回答**，
> PS 侧无法在不加载 bit 的前提下读出 PL 内容 —— 按铁律，我们没有去读。

### 1.3 板上已有的旧部署（**全部保留，未触碰**）

| 目录 | 内容 | 说明 |
|---|---|---|
| `/home/xilinx/demoir_bundle` | `design_1.bit/.hwh` + 旧 `weights_pack` + manifest | 10-23 的旧部署 |
| `/home/xilinx/demoir_k5` | `design_1.bit` + k1/k3/k5 引擎 + `*.so`(armv7l) | 5-05 的 int8 调试 |
| `/home/xilinx/demoir_new` | 多份 bit/hwh + `weights_pack` + `golden` | 5-05 的 int8 调试 |
| `/home/xilinx/demoir_v2` | `design_1.bit` + `golden/` | 5-06 的 int8 调试 |
| `/home/xilinx/pack64*` `pack320_df` `img_tests/*` | 旧激活/图像包 | 无关 |

⚠ 这些目录里的 `design_1.bit` 都是**旧设计**。`bin_test_staged.py` / `bin_net.py`
原本硬编码 `/home/xilinx/demoir_v2/design_1.bit` 作为默认 bit —— 本次已改掉（见第 7 节），
避免上板时手一滑加载了过期比特流。

### 1.4 时间偏差（不影响功能，影响看日志）

```
板子  date → Tue May  6 16:29 UTC 2025
主机  date → Fri Oct 30       2026
```

板子 RTC 没同步（差约 17 个月，且无 NTP）。后果：

- `tar` 解包时刷了一屏 `time stamp ... is in the future` —— **无害**，不是数据错误；
- 板端日志/文件 mtime 不可用于"哪份更新"的判断，比对一律用 **sha256**；
- 若板子重启，时间会更离谱 —— 看日志时以内容为准，别看时间。

---

## 2. 部署清单

### 2.1 目录布局：镜像仓库结构（不是扁平摊在 `ps/` 下）

部署到 **`/home/xilinx/demoir/`**，刻意保持和仓库一致的层级：

```
/home/xilinx/demoir/
├── ps/
│   ├── pynq/                    ← 驱动与测试（*.py，无 .so）
│   └── tools/                   ← check_bin_driver.py 等
├── deploy/
│   ├── bin_weights_new/         ← 二值容器（本轮主角）
│   └── weights_pack_new/        ← int8 权重（备用，给 uni_engine）
└── pl/rtl_hand/golden/          ← 21 MB xsim 黄金向量（供 --selftest / 阶段 5）
```

**为什么不是** `/home/xilinx/demoir/ps/*.py` 扁平放：

- `check_bin_driver.py` 用 `ROOT = <script>/../..` 定位 `deploy/` 和 `pl/rtl_hand/golden`；
- `bin_engine._selftest()` 用 `<ps/pynq>/../..` 定位同一个 `deploy/`；
- `bin_test_staged._default_pack()` 找 `<ps>/../deploy/bin_weights_new`。

镜像结构让这些默认路径**全部天然成立**，不需要在任何脚本里写死板端绝对路径。
扁平布局会让它们全部指到 `/home/xilinx/deploy`（不存在）。

### 2.2 文件与校验

传输方式：主机侧打 tar（21 MB）→ `scp` → 板端解包。用 tar 而不是逐文件 scp，是因为
`pl/rtl_hand/golden` 有 **1401 个小文件**，逐个 scp 会把 1400 次往返的网络抖动放大。

```
主机  tar czf /tmp/demoir_deploy.tar.gz -C <staging> .
      sha256 = 25517bc99df2f5bae14aa0f24d59fef56057448a6ae15a1ca13e88d0f079430c
scp   → /tmp/demoir_deploy.tar.gz
板端  sha256sum /tmp/demoir_deploy.tar.gz
      = 25517bc99df2f5bae14aa0f24d59fef56057448a6ae15a1ca13e88d0f079430c   ✓一致
板端  tar xzf -C /home/xilinx/demoir
```

**逐文件校验**（不是只比压缩包）：主机与板端各跑一遍

```bash
cd /home/xilinx/demoir && find . -type f -print0 | sort -z | xargs -0 sha256sum
```

首次比对结果：**1458 / 1458 文件全部一致**。
（随后补传 `torchless_ckpt.py`、更新 `verify_bin_weights.py` 后，总文件数 1466，
多出的是板端 `__pycache__/*.pyc`。）

关键文件（板端实测值，`sha256sum`）：

| 文件 | 字节 | sha256 |
|---|---:|---|
| `deploy/bin_weights_new/bin_weights.bin` | 1,125,152 | `9277bbf4ce10193086bc9d9e54bffa8ca2d76eb2e70203bfba211b49be953ac0` |
| `deploy/bin_weights_new/index.json` | 264,191 | `667c64ad8c935e644ea5baab974279f356d690de2560a065e926f7e00477c50c` |
| `deploy/weights_pack_new/weights.bin` | — | `c9a3cbcc85ce666ac1c110880850c38e999e1b710765d1f2846662ed31e61f19` |
| `deploy/weights_pack_new/index.json` | 113,541 | `72cdd0db74b488a01086b0a288ae23b782ffeba901a26009d92166dff3cbb527` |
| `ps/pynq/bin_engine.py` | 55,916 | `8850348f1cd80859770874f573184784cf6fba88ff828e599861ed7a30254d47` |
| `ps/pynq/bin_test_staged.py` | 28,091 | `b3c29c5a09a2e7525b83c7b15a7af0b61f25e3b045dadd081c2dcd917b6ecc58` |
| `ps/pynq/bin_net.py` | 28,578 | `8cdaf74e9d3c9bb547b7f930e4afdb60202f90c592f7acfe5f7accd0e58ad0cb` |
| `ps/pynq/bin_ref_numpy.py` | 30,638 | `2b863907a636850ce2b40c898dfad51358a83bf4fcc94b817420f2610ed33d08` |
| `ps/tools/check_bin_driver.py` | 8,552 | `8ef20d3b4b71864f392237fdb9982aab03cf2d61cd4b877d99902114d135de53` |
| `ps/tools/verify_bin_weights.py` | 24,217 | `b6e40ec6e192ac92a600f84530111ff53b415fb61770c6c03ba911fb4da831e0` |
| `ps/tools/torchless_ckpt.py` | 8,562 | `f04e8c95286eaa45e8a20150b88248b3c1cadce090789a58d79a9372da29ebe7` |

全量清单：`docs/binary/deploy_manifest.sha256`（1466 行，板端生成后回传）。

**容量**：`du -sb /home/xilinx/demoir` = 36,880,823 B（约 39 MB），
其中 `pl/rtl_hand/golden` 21 MB、`deploy/weights_pack_new` 15 MB、
`deploy/bin_weights_new` 1.9 MB。板子还剩 20 GB，毫无压力。

### 2.3 没传的东西（以及为什么）

| 未传 | 原因 |
|---|---|
| `ps/pynq/*.so`（`i8_fast_x86_64.so` 等） | **x86_64 架构**，在 armv7l 上加载必崩；二值链路是纯 numpy，不需要 .so |
| `ps/pynq/bin_engine.py` 以外的 `*.c` | 二值链路纯 Python |
| `ps/pynq/board_test_staged.py` | 模块级 `Overlay(BIT)` + 硬编码 `demoir_v2` + 走 `allocate`；**import 就会加载旧 bit**，按铁律不部署 |
| `ps/pynq/test_on_board.py` / `test_k1_on_board.py` / `run_network_on_board.py` | int8 旧链路，带 `Overlay`+`allocate`；本轮不用，避免误跑 |
| `deploy/weights_pack_new_ipmix` | 给"手写 IP 的 k=3/5 保留旧 TAP"用；本轮 PL 是新的 `bin_unit_0`，用不到。要跑 int8 时再传 |
| `deploy/weights_pack`（旧） | 按任务要求别动 |

---

## 3. 上板前已完成的验证（不需要 PL、不需要 bit）

### 3.1 主机侧：`check_bin_driver.py` 8 项全过

```bash
python3 pynq_specialized_code/ps/tools/check_bin_driver.py    # rc=0
```

| # | 项目 | 结果 |
|---|---|---|
| 1 | 容器自洽（偏移/尺寸/对齐/sha256/counts/fp_layers） | ✓ 5 项 |
| 2 | 参考实现 vs 5 个 xsim 黄金向量 | ✓ 逐字节 |
| 3 | 与 `verify_bin_weights.int_forward` 交叉对拍 | ✓ 5 层（含 k=1/k=3/s=2/d≥3/cout≥160/cin≥500） |
| 4 | 层间算子 PS 回退 vs golden（rprelu/sam/bilinear） | ✓ 6 个 |
| 5 | RPReLU 融合核对 | ✓ **227/227** |
| 6 | 整网调度形状断言 | ✓ --hw 16/32/64 各 482 op / 227 conv |
| 7 | `bin_engine.py --selftest`（寄存器级 RTL 模型） | ✓ |
| 8 | `bin_test_staged.py --fake`（6 阶段） | ✓ |

### 3.2 板端：纯 Python 导入 + 容器自洽

```bash
/usr/local/share/pynq-venv/bin/python3 -c "
import sys; sys.path.insert(0,'/home/xilinx/demoir/ps/pynq')
import bin_ref_numpy as R
b = R.Blob('/home/xilinx/demoir/deploy/bin_weights_new/bin_weights.bin')
print(b.header(), len(b.layers), b.sha256 == b.sha256_actual())
"
```

实测输出：

```
bin_ref_numpy OK
容器头: {'magic': 'BINWGT01', 'version': 1, 'n_layers': 227}
层数: 227
第一层: encoder.encoder_1.rdb.conv_layers.0.conv
        {'cin': 48, 'cout': 64, 'k': 3, 's': 1, 'd': 1, 'pad': 1, 'cb': 1, 'shift': 6}
容器总字节: 1125152 磁盘: 1125152
SHA_MATCH: True
```

单层纯 numpy 前向（不碰 PL）也跑通：`(64, 7, 9) int8`。
`bin_engine` / `bin_test_staged` / `bin_net` 三个模块在板上 **import 均 OK** ——
`bin_engine` 把 `pynq` 的 import 放在 `try` 里，所以无硬件也能 import，这一点是设计好的。

### 3.3 板端：`check_bin_driver.py`（无硬件跑同一套自检）

```bash
cd /home/xilinx/demoir && /usr/local/share/pynq-venv/bin/python3 ps/tools/check_bin_driver.py
```

板端 8 项中 **7 项直接通过**，第 3 项最初因环境差异失败两次，已修（见 7.3）：

| 失败现象 | 根因 | 处理 |
|---|---|---|
| `No module named 'torchless_ckpt'` | 首次漏传该模块 | 补传，sha256 校验一致 |
| `module 'numpy' has no attribute 'bitwise_count'` | 板端 numpy 1.21.5 < 2.0 | 在 `verify_bin_weights.py` 里加等价 popcount 兜底 |

最终板端结果：**8/8 全过**（容器、2 组参考对拍、层间算子、RPReLU、整网调度、
驱动自检、6 阶段 --fake），全程零 PL 访问。

---

## 4. 分阶段验证（6 阶段）

脚本：`/home/xilinx/demoir/ps/pynq/bin_test_staged.py`
原则：**每个阶段只引入一个新风险**，失败立即 `exit(非 0)`，不往下做。

### 4.0 准备：把**新** bit 放到板子上（由 PL 负责人执行）

```bash
scp design_1.bit xilinx@192.168.7.2:/home/xilinx/demoir/design_1.bit
ssh xilinx@192.168.7.2 'sha256sum /home/xilinx/demoir/design_1.bit'
```

> 默认 `--bit` 会按 `$BIT` → `bin_test_staged.py` 同目录 → `ps/` → `demoir/`
> 依次找 `design_1.bit`，所以放在 `/home/xilinx/demoir/design_1.bit` 即可省略 `--bit`。
> **找不到会直接报错退出，不会再回落到 `demoir_v2` 的旧 bit。**

运行身份：**必须 root**（PYNQ 的 `allocate` 与 `/dev/watchdog` 都要 root）。

```bash
cd /home/xilinx/demoir/ps/pynq
PY=/usr/local/share/pynq-venv/bin/python3
sudo_run() { echo 'xilinx' | sudo -S -p '' "$@"; }
```

### 4.1 阶段总表

| 阶段 | 引入的风险 | 会不会访存/写寄存器 | 看门狗 |
|---|---|---|---|
| 0 | overlay 加载 + 页解码 + 复位指纹 | **不访存**，只读寄存器 | 不武装 |
| 1 | AXI-Lite 写通路 + 寄存器偏移 | 写配置页，**不 start** | 不武装 |
| 2 | RTL 启动前校验（6 个 ERR_*） | 只写配置页，RTL 拒绝启动 | 武装 |
| 3 | 权重搬运（1.07 MB → DDR） | 写 DDR + 回读 | 武装 |
| 4 | 单层真实推理（最小层，8×8） | 读写 DDR | 武装 |
| 5 | golden 扫掠（5 个 xsim 向量） | 读写 DDR | 武装 |

### 4.2 阶段 0：overlay + 身份（不访存）

```bash
sudo_run $PY bin_test_staged.py --stage 0
```

期望输出：

```
IP          = bin_unit_0
物理基址    = 0x40060000（窗口 0x10000）      ← 以 overlay.ip_dict 为准，本设计是 0x4006_0000
配置页      = 0x40070000（= 基址 + 0x10000，A[16]=1）
复位指纹：alpha_shift=9 slope_shift=8  （期望 9 / 8，这是唯一的只读身份指纹）
AP_CTRL=...  AP_RETURN=...  DBG_FLAG=...     ← 只打印，不校验
保留页读 R_C = 0x0（期望 0）
✓ overlay / 页解码 / 复位指纹通过（本轮没有访存，也没写任何寄存器）
```

失败处置：

| 现象 | 含义 / 动作 |
|---|---|
| `overlay 里没有 bin_unit*` | bit 不对（不是这份 BD），或实例改名 |
| `alpha_shift/slope_shift 不是 9/8` | 窗口不是 `bin_unit`：bit 过期、或配置页偏移不对 |
| `保留页读到非 0` | A[16] 分页没生效，`+0x10000` 契约要复查 |
| `基址第 16 位是 1` | BD 地址分配不对（必须 64 KB 对齐且 A[16]=0） |
| `地址窗口只有 ...` | IP 窗口太小，装不下配置页 |

### 4.3 阶段 1：配置页回环（不 start）

```bash
sudo_run $PY bin_test_staged.py --stage 1
```

期望：14 个配置字段 + `B_IN` + "保留页写忽略" 全部符合，末尾

```
✓ 14 个配置字段 + B_IN + 保留页写忽略 全部符合预期（本轮没有 start，PL 没有访存）
```

失败 ⇒ AXI-Lite 写通路或寄存器偏移错位；先怀疑 bit 与驱动版本不匹配，别怀疑算法。

### 4.4 阶段 2：启动前校验（RTL 必须拒绝坏参数）

```bash
sudo_run $PY bin_test_staged.py --stage 2
```

期望（每次只错一个参数，RTL 返回对应错误码、**不启动不访存**）：

| 用例 | 期望 |
|---|---|
| 一个基址都不写 | `ERR_NULL(1)` |
| `base_in + 4`（非 8 字节对齐） | `ERR_ALIGN(5)` |
| `alpha_shift=8` | `ERR_SHIFT(3)` |
| `cin=52`（非 8 的倍数） | `ERR_LAYOUT(4)` |
| `base_in` 高 32 位字 = 1 | `ERR_SIZE(2)` |
| `cout=200`（> 192） | `ERR_SIZE(2)` |

失败 ⇒ 驱动与 RTL 的校验契约不一致。**不要**为了"跑过去"去改 RTL 或绕过校验。

### 4.5 阶段 3：权重搬运

```bash
sudo_run $PY bin_test_staged.py --stage 3
```

期望：

```
搬运 1125152 B（1.13 MB）→ pa=0x...，... ms（... MB/s）
缓存：第二次调用 ... ms（不重搬）✓
板上 sha256 = 9277bbf4...953ac0
磁盘 sha256 = 9277bbf4...953ac0
index.sha256= 9277bbf4...953ac0
✓ 第   0 层 ... wgt ... B 一致
✓ 第 113 层 ... wgt ... B 一致
✓ 第 226 层 ... wgt ... B 一致
✓ 权重搬运可回读、索引切片正确
```

失败处置：

- sha256 不符 ⇒ 先看首个不符偏移：整段都错多半是**物理地址/缓存 flush** 问题，不是权重文件坏了
  （文件 sha256 已在上板前校验过）；
- 金丝雀（输出尾部 `0xA5`）被打穿 ⇒ PL 越界写，**立刻停**，这属于挂死前兆；
- 缓存第二次仍重搬 ⇒ 只是性能问题，可继续。

### 4.6 阶段 4：单层推理（最小 workload 层，8×8）

```bash
sudo_run $PY bin_test_staged.py --stage 4
```

期望：

```
✓ 激活打包：... B 与 np.packbits 独立实现逐字节一致
done t=... ms（墙钟 ... ms，含轮询）  HoutxWout=8x8
✓ <layer.name> 逐字节一致（... B）
```

注意这里有一层**独立复核**：打包结果没用 `bin_ref_numpy.binarize_pack` 自己验自己，
而是用 `np.packbits` 重写了一遍（`indep_pack_check`）。这一步过不了就是 PS 侧打包错，与 PL 无关。

失败处置：

| 现象 | 动作 |
|---|---|
| 打包与 packbits 不符 | PS 侧 binarize/pack 有 bug；PL 还没参与，改 `bin_ref_numpy` |
| 输出字节不符、但"输出非零个数"和期望差很多 | 多半是基址/位序/`shift`，对照 `index.json` 该层字段 |
| 输出全 0 | PL 没真正启动（`AP_CTRL`/done 握手），回阶段 2 复查 |
| 超时 | 看门狗会停喂并把板子拉回（≤15 s）；先看 `DBG_FLAG` 与预计拍数 |

### 4.7 阶段 5：golden 扫掠（逐字节）

```bash
sudo_run $PY bin_test_staged.py --stage 5      # 或 --stage 6 一次跑完 0..5
```

期望 5 个 xsim 模块级向量全部逐字节一致：

```
bin_1x32_32_8x8    cin= 32 cout= 32 k=1 cb=1  2048/2048
bin_1x64_16x16     cin= 64 cout= 64 k=1 cb=1  4096/4096
bin_3x32_48_8x8    cin= 32 cout= 48 k=3 cb=1  3072/3072
bin_3x32_48_d2     cin= 32 cout= 32 k=3 cb=1  2048/2048   (d=2)
bin_3x96_64_8x8    cin= 96 cout= 64 k=3 cb=2  4096/4096
✓ 5/5 个用例逐字节一致
```

这 5 个向量同时覆盖：k=1 / k=3、cb=1 / cb=2、dilation=2、非 8 倍数 cout。
**只有这一步过了，才能说"PL 引擎在真板子上算对了"。**

### 4.8 错误码速查（`bin_engine.decode_err`）

| code | 名字 | 含义 | 常见原因 |
|---:|---|---|---|
| 0 | `OK` | 正常完成 | — |
| 1 | `ERR_NULL` | 某必需基址为 0 | 忘了 `set_base`，或分配失败拿到 0 |
| 2 | `ERR_SIZE` | 维度/容量越界 | `cout>192`、地址高 32 位非 0、缓冲太小 |
| 3 | `ERR_SHIFT` | 移位参数不是契约固定值 | `alpha_shift≠9` / `slope_shift≠8` |
| 4 | `ERR_LAYOUT` | `cin % 8 != 0` | 通道数没补齐 |
| 5 | `ERR_ALIGN` | 基址非 8 字节对齐 | 手算地址 `+4` 之类 |
| 6 | `ERR_UNIT` | `UNIT` 参数不存在 | `--unit` 给了没有的实例号 |

---

## 5. 风险点与回滚

### 5.1 看门狗（`board_safe.Watchdog`）

实现要点（`ps/pynq/board_safe.py`）：

- 打开 `/dev/watchdog`，`ioctl WDIOC_SETTIMEOUT` 设超时（阶段测试默认 **15 s**）；
- 独立线程每 `timeout/4` 喂一次 `"*"`；
- **主线程心跳**（`beat()`）超过 `timeout/2` 没更新 ⇒ 线程**主动停止喂狗** ⇒ 硬件复位；
- 正常退出时写 magic `"V"` 再 `close`，把看门狗正常关掉（不会误复位）。

`bin_test_staged.py` 从**阶段 2 起**统一在 `with make_wdt(args.wdt, ...)` 里跑，
`e.run(..., wdt=wdt)` 的轮询循环里也会 `beat()`。参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--wdt` | 15.0 | 看门狗超时秒数；长层可调大 |
| `--no-wdt` | 关 | **只在完全清楚后果时用**；关掉就回到"挂死要人工断电" |
| `--pool-mb` | 8.0 | 单块 DDR 池大小；1 GB 板子上 8 MB 很安全 |

手动武装示例（任何"可能让 PL 访存"的自写脚本都该这样写）：

```bash
sudo_run $PY -c "
import sys; sys.path.insert(0,'/home/xilinx/demoir/ps/pynq')
from board_safe import Watchdog
with Watchdog(15) as w:
    ...            # 每次长循环里 w.beat()
"
```

### 5.2 板子挂死了怎么恢复

| 情形 | 恢复方式 |
|---|---|
| 脚本超时/异常退出 | 看门狗 `__exit__` 写 `"V"` 关闭，板子不会复位；重跑即可 |
| 主线程卡住（PL 不返回 done） | 看门狗停止喂狗 ⇒ **≤15 s 后 SoC 自动复位**，SSH 断线后等它起来 |
| 硬挂（DDR 被写坏、SoC 锁死）且**当时没武装看门狗** | 只能**断电重上电**（用户不在场时不要制造这种局面） |
| 只想放弃本次部署 | `ssh xilinx@192.168.7.2 'rm -rf /home/xilinx/demoir'`，无系统副作用 |

判断"板子还在不在"：

```bash
ping -c 3 192.168.7.2
ssh -o ConnectTimeout=5 xilinx@192.168.7.2 'cat /sys/class/fpga_manager/fpga0/state'
```

> 复位后 `fpga_manager` 会回到 `unknown`，PL 里的旧设计也没了 —— 这是正常的，
> 重新加载 bit 由 PL 负责人做。

### 5.3 回滚

本次部署**只新增**了 `/home/xilinx/demoir/`，没有改系统文件、没有装包、没有动旧目录：

```bash
# 完整回滚（幂等，可反复执行）
ssh xilinx@192.168.7.2 'rm -rf /home/xilinx/demoir /tmp/demoir_deploy.tar.gz'
```

主机侧回滚：本次对源码的修改都在 git 里，`git diff` 可查（第 7 节逐条列出）。

### 5.4 上板时最容易犯的 4 个错

1. 用 `python3` 而不是 `/usr/local/share/pynq-venv/bin/python3` ⇒ `import pynq` 失败；
2. 不用 `sudo` ⇒ `allocate` 拿不到 CMA、`/dev/watchdog` 打不开（会打印"本次无看门狗保护"，
   然后继续跑 —— 这是最危险的情况）；
3. `--bit` 指向 `demoir_v2/design_1.bit` 这类旧目录 ⇒ 加载了旧设计（现已改为不再默认回落）；
4. 在 `--no-wdt` 下跑阶段 4/5 ⇒ 挂了没人救。

---

## 6. 已知未解决的问题（如实记录）

| # | 问题 | 影响 | 现状 |
|---:|---|---|---|
| 1 | **BD 里只有一个 `bin_unit_0`**（不是 4 个） | 无法并行 4 个二值单元；`--unit 1..3` 会报 `ERR_UNIT(6)` | 驱动已按 `bin_unit_<id>` 自动找实例，多实例时无需改代码 |
| 2 | **`index.json` 不含 H/W**（分辨率运行期定） | 同一份权重可跑任意尺寸，但"板上跑 320/640"没有预置形状断言 | 运行期由 `BinLayer(entry, H, W)` 现算；`bin_net --hw` 支持 16/32/64 已验证 |
| 3 | **`bin_sam`/`bin_interp`/`bin_rprelu` 没有 PS 驱动，BD 也没有实例** | 整网里 SAM/插值只能走 PS numpy（慢）；RPReLU 已融进 conv 收尾，**不需要**单独单元 | `bin_net.capabilities()` 里逐条标注；227/227 层 RPReLU 融合已核对 |
| 4 | **整网数值未验证**（没有参考输出图） | 单层逐字节对得上 ≠ 整网输出正确 | `bin_net --dry-run` 能跑通形状/调度，但无 ground truth 可比 |
| 5 | **int8 残差定标未决策**（`y+x` 两个张量 `fs_out` 可能不同） | 一旦两端定标不同，`y+x` 结果会偏；二值链路目前不涉及，int8 链路涉及 | 待定；`weights_pack_new` 里没有对应方案 |
| 6 | **权重不是新训练的**：`bin_weights_new` 是从现有 `runs/bin_base` 重排的；新训练的二值网在 `runs/bin_base` 但**没打进容器** | 精度上限受旧 checkpoint（iters=337500/150 epoch）限制 | 重打包需要 `pack_bin_weights.py` + 新 ckpt；容器格式无需改 |

其它限制（环境层面，非设计问题）：

- 板端 **numpy 1.21.5**：任何用 numpy 2.x 新 API（如 `np.bitwise_count`）的脚本都得加兜底；
- 板端**没有 torch / onnx / opencv**：不能板上做浮点参考或读 ckpt（除纯 numpy 的 `torchless_ckpt`）；
- 板端与主机**时钟差 17 个月**：日志时间不可信，比对只用 sha256；
- `ps/pynq/*.so` 是 x86_64，**不能**在板上用；armv7l 版 .so 只存在于旧目录
  （`demoir_k5/conv_ref_fast_armv7l.so` 等），本轮二值链路不需要。

---

## 7. 本次为"能上板"改了什么

### 7.1 去掉硬编码比特流路径（**安全项**）

`ps/pynq/bin_test_staged.py`、`ps/pynq/bin_net.py` 原来：

```python
ap.add_argument("--bit", default=os.environ.get("BIT", "/home/xilinx/demoir_v2/design_1.bit"))
```

→ `demoir_v2` 是**旧部署**，默认值等于"手一滑就加载旧设计"。改为：

```python
def _default_bit():
    # $BIT > 脚本旁 design_1.bit > 上一级 > 上两级；都没有就返回 None
    ...
ap.add_argument("--bit", default=os.environ.get("BIT") or _default_bit())
```

并在真正 `Overlay()` 前加了显式检查：没有 bit / 文件不存在 ⇒ 打印明确错误并 `exit`。

### 7.2 默认权重目录支持板上布局

`bin_test_staged._default_pack()` / `bin_net._default_pack()` 增加
`<ps>/../deploy/bin_weights_new`（板端）这一档，原来只有"脚本旁"和"`../../deploy`"（本机源码布局）。

`bin_ref_numpy.Blob` 现在也接受**直接传 `bin_weights.bin` 文件路径**（自动取 dirname），
因为板上排查时很自然会写出 `Blob('.../bin_weights_new/bin_weights.bin')` —— 原实现会去找
`bin_weights.bin/index.json` 然后报 `NotADirectoryError`。

### 7.3 numpy 1.21 兼容（板端才能跑交叉对拍）

`ps/tools/verify_bin_weights.py` 用 `np.bitwise_count`（numpy ≥ 2.0）。板端 1.21.5 没有。
加 `_popcount()`：有就用原函数，没有就**按字节查表**求和（等价实现，已对
`uint8/uint16/uint64/int64[含负数]` 与 `np.bitwise_count` 逐元素比对一致）。

> 为什么不是 `np.unpackbits(axis=-1)`：它沿轴是 **8 倍展开**，不是逐元素；
> 而且这里的 XNOR 是 **uint64 字**，按 uint8 解会只数低 8 位。两个坑都踩过了。

### 7.4 回归验证

改完立刻重跑主机侧 `check_bin_driver.py`：**8/8 全过，rc=0**
（尤其第 3 节交叉对拍 5 层仍一致，说明 `_i16` 等辅助函数没被误删）。

---

## 8. 一页纸清单（上板当天照着做）

```bash
# 0) 确认板子活、PL 状态干净
ping -c 2 192.168.7.2
ssh xilinx@192.168.7.2 'cat /sys/class/fpga_manager/fpga0/state'

# 1) 传新 bit（PL 负责人）
scp design_1.bit xilinx@192.168.7.2:/home/xilinx/demoir/design_1.bit

# 2) 阶段 0（只读，最安全）
cd /home/xilinx/demoir/ps/pynq
echo 'xilinx' | sudo -S -p '' /usr/local/share/pynq-venv/bin/python3 \
    bin_test_staged.py --stage 0

# 3) 阶段 1（写配置、不 start）
... --stage 1

# 4) 阶段 2（RTL 校验，看门狗开始武装）
... --stage 2

# 5) 阶段 3（搬权重）
... --stage 3

# 6) 阶段 4（单层，8×8）
... --stage 4

# 7) 阶段 5（5 个 golden 扫掠）
... --stage 5
```

任何一步 `exit != 0` ⇒ **停**，把该阶段输出整段留档，按第 4 节对应小节排查；
**不要**带着失败往下跑，也不要为了通过而 `--no-wdt`。
