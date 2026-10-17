# 上板操作清单（AX7015B，XC7Z015）

> 前提：PL 侧 v2 已封版（bit/hwh/XSA 均为 2026-10-15 产物）；
> PS 侧部署包已验收（4 图标定，量化 PSNR 31.5 dB / 换图 ≥25.8 dB）。
> 上板包由 `python 03_ps/tools/make_board_bundle.py` 生成（`board_bundle/`）。

## 路线 A：JTAG + 裸机自检（第一次上板，先走这条）

目的：用最小系统确认"PL 电路 + 时钟 + DDR"是活的。不需要 SD/UART/网口。

1. 硬件：AX7015B 上电，JTAG（USB）接电脑；拨码按手册选 JTAG 启动模式。
2. Vitis 2025.2：New Platform Project ← `02_vivado/xsa/design_1.xsa`，
   OS=standalone，CPU=ps7_cortexa9_0；编译平台。
3. New Application（Empty C）→ 加入 `03_ps/baremetal/main.c` → 编译。
4. Debug Configuration：勾 "Program FPGA"（自动跑 ps7_init：
   DDR + FCLK0=150 MHz）。运行。
5. 读结果：Expressions 看 `g_report`，或内存看 `0x3000_0000`：
   - `[0]==0xC0FFEE01`、`[1]==5`（用例数）、`[2]==5`（通过数）→ 完美。
     用例 5 是 v3 扩容后的最坏形状（cin=352/cout=192）——它专门验证
     "大通道层"的电路行为，是本轮修复的核心重点。
   - 若 `[2]<4`：看每个用例 4 元组 {返回码, 不一致数, 轮询圈数}，
     返回码≠0 是参数/握手问题；不一致数>0 是数据问题（缓存/地址）。

预期耗时：编译 5 分钟 + 运行几秒。

## 路线 B：PYNQ 整网（板载 Linux）

前提：板子能启动 PYNQ（厂商镜像）；`board_bundle/` 拷到板子上。

### B1. 单算子自检

```
sudo python3 test_on_board.py
```

- 脚本自动从 `.hwh` 发现两个 AXI 基地址；打印 FCLK 信息。
- 预期：5 个用例（含 relu=0 线性输出用例、352×192 最坏形状用例）
  全部 rc=0、不一致数=0。
- 到这一步为止，"PL 引擎在真实板子上"就算验收完成。

### B2. 整网推理

```
sudo python3 run_network_on_board.py --manifest manifest_640.json \
    --pack weights_pack --input weights_pack/input_q.bin --bit design_1.bit
```

- 预期输出："与部署参考对比: 0/N 不一致"（逐位一致）。
  推理链：上板 RTL = cosim 语义（已验证）= RefEngine 逐位（已验证）。
- 正常用时估计：首次跑报告内存池大小（**~85 MB**，py 侧池规划；请确认
  板子的 CMA ≥ 128 MB，低于此需在 /boot/config.txt 增大 cma=）；
  **整网按 cosim 实测外推约 30 分钟量级**（17.4 拍/单元 @150MHz，见
  `pspl_interface_audit.md` §4），不是几分钟——脚本开头会打印预计耗时与
  "第几个卷积/共几个"的进度，别误判成卡死。
- 超时：驱动默认**按工作量估算**（最贵一层约 700 秒上限），一般不用管；
  想收紧就加 `--timeout 120`（秒）。**不要**再传 5 秒——会在第一层大卷积上误报超时。
- 每个卷积对整个池做一次 cache flush/invalidate（84.8 MB），这部分时间也算在内；
  正确性优先，跑通后再细化到按张量区间同步。

### B3. 失败排查顺序（整网不一致时）

1. 单测（B1）过了没有？没过 → 先回 A / 检查 bit 文件版本。
2. `weights_pack/ref_out.bin` 与板子上的包是否同一版（时间戳）？
3. 内存池地址：确认 `allocate` 返回的物理地址在 0x0000_0000~0x3FFF_FFFF。
4. 缓存：PYNQEngine 每个卷积后 flush/invalidate；若用了自定义 cache 参数
   （如 `allocate(..., cacheable=...)`），先恢复默认。
5. **换回小图定位**：`manifest_64` + 64×64 随机输入，人工比对
   （runner 本机也是同一套层间算子代码）。

### B4. 时序边缘预案（v3 特有的情况）

v3.2 实现的 WNS = +0.107ns（首版 +0.004ns 的时序边缘已通过两处
"装载地址递推化"修复，改善 27 倍；v2 是 +0.217ns）。若板上出现
"多数用例通过、个别形状偶发不一致/超时"，大概率是时序边缘在真实 PVT
下暴露。处理顺序：

1. **降频重试**（PYNQ 路线，1 分钟搞定）：

   ```
   sudo python3 - <<'EOF'
   from pynq.ps import Clocks
   Clocks.fclk0_mhz = 140          # 余量立刻 +0.47ns
   print("FCLK0 =", Clocks.fclk0_mhz)
   EOF
   sudo python3 test_on_board.py
   ```

   降频后通过 → 记录现象，回头做"装载地址递推化"重综合消除边缘。
2. JTAG 裸机路线无法软件降频（FCLK 由 ps7_init 配 150MHz）；若裸机用例 5
   偶发不一致，先用 PYNQ 路线降频验证同一硬件，再决定是否重综合。

## 结果记录（上板后填写）

| 项目 | 预期 | 实测 | 日期 |
|---|---|---|---|
| 裸机自检 5 用例 | 5/5 通过 | | |
| PYNQ 单测 5 用例 | 5/5 通过 | | |
| 整网对拍 | 0 不一致 | | |
| 整网用时 | 1~3 分钟 | | |

## 之后可以做的事（按优先级）

1. 性能优化：PYNQEngine 的整池 flush → 按张量区间 flush；
   卷积引擎参数里 `cout` 一维展开方式与 DDR 带宽的小优化。
2. 换新图重跑：`eval_generalize.py`（本机）先验证，再上板。
3. 若将来要长驻使用：把 runner 做成开机自启 + 网络接口（另立任务）。
