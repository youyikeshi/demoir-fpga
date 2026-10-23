# 发布：去摩尔纹加速器 v43s（2026-10-23）

> **整网 187.41 s → 43.55 s（−76.8%），输出与 FP32 参考的定点结果逐字节相同（零精度损失）。**

本分支只包含**最终版本的源码、比特流、PS 端代码与关键设计文档**，用于交付与复现。

---

## 1. 交付对（必须成对使用）

| 文件 | 值 |
|---|---|
| 比特流 | `pynq_specialized_code/board/overlay_engine_150mhz/design_1.bit`，md5 **`d388264c…`** |
| 硬件描述 | 同名 `.hwh`（与 bit 成对） |
| 权重包 | `pynq_specialized_code/deploy/weights_pack/`，md5 **`ab33c237…`**（容器格式与 8 车道版**完全相同**） |

> ⚠️ bit 与权重包必须成对：`check_consistency.py` 会拦（PAD12/PAD28 的 bit 只认对应容器）。
> ⚠️ 运行时 `C8_LAYOUT=1` 与 `--c8` 缺一不可，配错会返回 `ERR_LAYOUT(4)` 并抛异常（故意如此）。

## 2. 板上实测

| 项 | 值 |
|---|---|
| 整网 | **43.55 / 43.63 s**（两次） |
| conv | 35.43 s（81.3%） |
| 正确性 | **0 / 1,228,800** |
| 输出 md5 | `97f5a0942006742989cd64a9daad1a16` ≡ `ref_out.bin` |
| 资源 | BRAM 86.5/95（91.1%）、DSP 108/160（70%）、LUT csynth 35,520（76.9%）、Fmax 172.97 MHz |

## 3. 跑了什么

```bash
# 主机侧（HLS → BD → bit）
vivado -mode batch -source pynq_specialized_code/pl/scripts/hls_run_engine.tcl
vivado -mode batch -source pynq_specialized_code/pl/scripts/build_bd_engine.tcl
MAX_THREADS=4 vivado -mode batch -source pynq_specialized_code/pl/scripts/build_bitstream_engine.tcl

# 验证阶梯
python pynq_specialized_code/pl/scripts/fuzz_shapes.py          # 30/30
python pynq_specialized_code/pl/scripts/fuzz_c8_shapes.py       # 23/23
python pynq_specialized_code/pl/scripts/check_k3_pad12.py       # 8/8
python pynq_specialized_code/pl/scripts/check_k5_pad28.py       # 9/9

# 板端
ssh xilinx@192.168.7.2
printf 'xilinx\n' | sudo -S -p '' bash -lc \
  'cd /home/xilinx/demoir_new && C8_LAYOUT=1 ./board_safe_run.sh 900 \
     run_network_on_board.py --manifest manifest_640.json --pack weights_pack \
     --input weights_pack/input_q.bin --bit design_1.bit --c8 --profile'
```

## 4. 优化轨迹（每个节点都有板上实测 + md5 证据）

| 整网 | 手段 | 关键发现 |
|---:|---|---|
| 187.41 | （基线） | |
| 185.0 | 激活改 **C8 交错布局**（全 8 对齐、纯编译期、无运行期 mux） | 资源净赚 DSP 131→104 |
| **144.89** | 层间算子改 **ARM C 内核** | sam_sum/interp/eca 48.7 → 8.7 s |
| **106.69** | 填充与 k=1 权重 **32-bit 读** | `ARLEN=1` 的真实含义是"抓 4 字节只用 1 个" |
| **68.40** | k=3 权重 **PAD12 容器** | **写 bank 号必须是编译期常量** |
| **59.87** | k=5 **PAD28** + cat/interp/add 内核化 | cat 瓶颈是 `tobytes+translate`（42.8 vs memcpy 183 MB/s） |
| **52.40** | 写回改**独立 64-bit 拷贝循环** + 修掉补零白写 | 同一个"宽写"动作，放的位置决定成败（流水内慢 5.6%，独立循环快 52%） |
| **52.22** | 删 pp（退役）+ k=1 乘法**迁 DSP48** | **WNS −0.184 → +0.005（setup 收正）** |
| **43.55** | k=1 上 **16 车道** | 8 DSP + 8 LUT 混搭 ⇒ **DSP +0、BRAM +0、Fmax 不掉** |

## 5. 关键设计事实（接手必读）

1. **C8 激活布局**：`addr(c,y,x) = ⌊c/8⌋·H·W·8 + y·W·8 + x·8 + (c%8)`，int8 张量一律补齐到 8 的倍数
   （3ch→8、12ch→16），所以没有运行期布局 mux。
2. **PAD12 / PAD28 权重容器**：k=3 的 9 抽头补 3 个零成 3 字，k=5 的 25 抽头补 3 个零成 7 字。
   目的不是"对齐好看"，而是**让 LUTRAM 的 bank 索引保持编译期常量**——一旦变运行期值，
   HLS 既不流水、LUT 还会爆（实测 82% → 91%，或 103% 布不下）。
3. **k=1 的 16 条车道 = 8 条 DSP + 8 条 LUT 乘法器**（每 LUT 乘法器 41 LUT），所以 DSP 总量没涨。
4. 成本模型（24 点网格拟合，回算 247 层误差 −1.74%）：
   $$t/\text{pixel} = 0.488 + 1.7619\,c_{in} + 5.2537\,c_{out} + 0.21097\,c_{in}c_{out}\ \ [\text{拍@150MHz}]$$

## 6. 已被实测否掉的方向（不要重走）

| 方向 | 结论 |
|---|---|
| 深度卷积替代稠密 | 108 层非对角能量占 **94.3%**，裁掉 **−4.18 dB / SSIM −0.052** |
| 激活 int4 | per-tensor **−6.10 dB**；QAT 800 步发散（−11.13 dB） |
| `num_write_outstanding` 16→64 | 13 个微基准逐项一致（AW 请求 FIFO `USER_MAXREQS=4` 空转） |
| `component.xml` 的 `HAS_BURST` | 内核本来就在发 `AWLEN=63` 的 512 B burst，改了零收益 |
| 填充/权重 4→8 字节加宽 | 同 bundle 位宽互相绑架，AR 笔数 201 → 15,416，**净亏 27%** |
| 收尾 8 通道串行 | 13 个微基准相同到 0.01 ms |
| `K1_OC_UNROLL` / `K3_LANES` 等宏 | **空转**，改它们 csynth 一个数都不变 |
| 跨行软件流水 / 2 像素 16 车道 | `line_buf` 是 1R1W 非双读口；乒乓要 +32 BRAM18K 而只剩 17 |

## 7. 工程纪律（踩过的坑）

1. **板端任何 python 必须经 `board_safe_run.sh`**：精确清理残留（不误杀 ssh/bash）、CMA 预检 ≥100 MB、
   `timeout` 兜底、**flock 互斥**（两个任务曾互杀到 150/247 层）。
2. **切 bit 必须 bit+hwh 成对**，且先归档（有过一次删掉旧备份、回退要重跑 25 min）。
3. **板端 `cc` 默认不开 NEON**：标量兜底逐字节访问 CMA 只有 6.4 MB/s（cat 从 5.66 s 变 65.6 s）。
   必须显式 `-mfpu=neon -mfloat-abi=hard` + 运行时自检 `i8_have_neon()`。
4. **CMA 非缓存内存必须按 64 字节成块处理**（16 B/迭代 71 MB/s、64 B/迭代 163 MB/s）。
5. **别用"循环没流水"解释访存慢** —— 去看生成 RTL 的 `ARLEN/AWLEN/WSTRB`；
   而且 `AWLEN` 要看**主状态机**，Pipeline 模块里那个是死输出。
6. **同一个"宽写/展开"动作，放的位置决定成败**（流水内 vs 独立循环，差 52%）。
7. **微基准必须显式设 150 MHz**：板子 FCLK0 默认 125 MHz，不设会全体慢 1.2 倍；
   `Clocks` 必须在 `Overlay()` 之后调用，否则异常被吞。
8. **起 impl 前先看内存**：同一台机器上可能还有训练任务（本次因此被打断两次）。
