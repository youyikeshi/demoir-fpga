# 复现步骤

## 0. 前置

- 器件：XC7Z015-2（AX7015B）
- Vivado：2025.2.1
- 板子：PYNQ 3.1.0，`192.168.7.2`
- 主线 RTL 根：`runs/timing_v50/rtl_v50/`（`verify_rtl_root.sh` 应 MATCH=35 DIFF=0）

## 1. 重建 v57 位流

v57 与 v39 只差一个文件。**不需要重建 BD**（`.hwh` 沿用 v39 的 `c1686bd8`）。

```bash
cd /media/felix/Data/coding/demoir-fpga

# ① 从 v39 的 RTL 树起（v39 的 12 个构建输入）
# ② 只替换 bin_e1e2.v 为刀 7 版：
#    源文件在 runs/ACCEPT_v57/04_RTL/rtl_v57/ 里，或从 runs/v57_archive/rtl/ 取
#    目标 md5 必须是 bc28ead3

# ③ 构建（Vivado 非工程模式批处理）
tools/memgate.sh 5 --reserve 2.5          # 内存门禁，peak ≈5 GB
vivado -mode batch -source runs/v57_archive/build_final_150v57.tcl \
       -log runs/v57_archive/build_v57.log
```

**验收判据**（构建后必须逐项对）：

| 项 | 期望值 |
|---|---|
| bit md5 | `5d1cb78969f948d45f379c7845873717` |
| WNS @10.000 ns | `+0.206` |
| TNS | `0` |
| 违例端点 | `0` |
| LUT / FF / BRAM / DSP | `20255 / 18961 / 35 / 101` |
| Latch（报告 + LDCE/LDPE）| `0 / 0 / 0` |
| DRC | `0 Errors` |

> ⚠ bit md5 不保证逐次一致（时间戳等），**以时序与资源判据为准**。

## 2. 功能等价验证（不需板子）

```bash
# 模块级：v39 与 v57 的 bin_e1e2 对照
runs/v152_timing/../../  # ab_e1e2.sh 的位置见 06_EVIDENCE/ab_e1e2_v39_t152.log 抬头

# 整网 12 shape：同一 TB、同一黄金向量，比较两棵树
# 期望：12/12 逐字节一致，且 cycles 逐 shape 相同
#   1429/2151/5329/3788/12713/4935/7076/3863/2921/5048/7284/2724
```

## 3. 上板

### 3.1 加载位流与频率

```python
from pynq import Overlay, Clocks
ol = Overlay("/path/design_1_150v57.bit")   # .hwh 用 v39 的 c1686bd8

# ⚠ PYNQ 的 Clocks.fclk0_mhz 口径是真实值的 1.5 倍
#    要真实 100 MHz ⇒ 写 150
Clocks.fclk0_mhz = 150
assert abs(Clocks.fclk0_mhz - 100) < 1, "读回不是 100 MHz！"
```

**必须确认读回 100 MHz。** v57 的 10 ns 余量只有 0.206 ns，跑 150 MHz 会真错数。

### 3.2 基址

| IP | 基址 |
|---|---|
| `conv_uni_0` | `0x40040000` |
| `bin_unit_0` | `0x40060000` |
| `fconv_0` | **不存在**（v39 没有这个 IP）|

v57 基于 v39，**没有 magic 寄存器**（`0x314` 读失败属正常）。

### 3.3 部署 PS 代码

```bash
# 必须是这两个 md5
md5sum pynq_specialized_code/ps/pynq/bin_net.py     # 0990948de490eb6ba6d4aaefbcafa355
md5sum pynq_specialized_code/ps/pynq/bin_engine.py  # 2fd3d0990c47151fe3bc06544b8adb64

# 一起同步 ps/ 与闭包清单
rsync -a pynq_specialized_code/ps/ xilinx@192.168.7.2:/home/xilinx/jupyter_notebooks/demoir/ps/
scp runs/board_ops/20261102/deps_closure.txt xilinx@192.168.7.2:/home/xilinx/jupyter_notebooks/demoir/
ssh xilinx@192.168.7.2 'cd /home/xilinx/jupyter_notebooks/demoir && md5sum -c deps_closure.txt'
```

### 3.4 运行点开关

```
BIN_PACK=e1e2
BIN_LAZY_DEQ=1
BIN_CAT_FUSION=1
BIN_BILINEAR_C=1
BIN_PRECOMP=1
Q8CACHE=1
BIN_F32DIRECT=1
BIN_CAT_ECA_FUSE=1
BIN_EPI_NOINV=1
```

### 3.5 期望 sha1

| 尺寸 | sha1 三件套 |
|---|---|
| 256² | `9b1940c8` / `f86a7035` / `025757a8` |
| 128² | `27e25cf6` / `b82b83f3` / `6db4e9bb` |
| 64² | `bfa246d3` / `226bd39e` / `762d84b9` |

## 4. 板端纪律（血泪换来的）

| # | 规则 | 为什么 |
|---|---|---|
| 1 | **单 net** | 多 net 共用同一个 engine 的 24 个惰性槽 ⇒ 污染测量、甚至偶发 sha1 变 |
| 2 | **每次重新部署**，不用工作树 | 板端 `ps/` 是共享的，别人几分钟内会换掉 |
| 3 | 测量前后各记板端 `ps/` md5 | 不一致则该次数据作废 |
| 4 | **只报第 2/3 跑** | 第 1 跑慢 24.3%（import / .so / Overlay 进程级一次性 ≈25 s）|
| 5 | 一次 session 可信窗口只有前 5~6 跑 | 长进程会自减速 |
| 6 | 杀进程用 `pkill -x` 或按 pid `kill -TERM` | **禁 `pkill -f`**（会误杀）|
| 7 | 收尾只 `sync; sync` | **绝不 `poweroff`** |
| 8 | 判 PL 空闲要同时看 `0x310=0` **且** 两 IP `AP busy=0` | 单看 `0x310` 会在 PL 还在跑时误判 |
