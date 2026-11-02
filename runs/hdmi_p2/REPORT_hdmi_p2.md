# HDMI P2 交付报告：真画面扫描器 + PS 驱动 + 主机自检

> 任务书：HDMI P2（真画面）—— ①`hdmi_fb_scan.v` ②`hdmi_out.py` ③仿真与自检。
> 不建 BD、不出位流、不碰板子。日期 2026-11-02；单实例综合（memgate/vlock）。

## 1. 交付物

| 文件 | 说明 |
|---|---|
| `pynq_specialized_code/pl/rtl_hand/modules/hdmi_fb_scan.v` | 扫描器（AXI 读主 + 行缓冲 + CDC + 欠载诊断） |
| `pynq_specialized_code/pl/rtl_hand/tb/tb_hdmi_fb_scan.v` | xsim TB：6 个用例覆盖判据 ①~⑤ |
| `pynq_specialized_code/pl/rtl_hand/tools/gen_hdmi_scan_golden.py` | 用 `compose()` 生成黄金向量 |
| `pynq_specialized_code/pl/rtl_hand/tools/run_hdmi_scan_sim.sh` | 一键仿真（`rm -rf xsim.dir` + `-debug off`） |
| `pynq_specialized_code/ps/pynq/hdmi_out.py` | PS 驱动（mmap，可注入 `reg_io`，不 import pynq） |
| `tools/hdmi_selftest.py` | 主机自检（4 模式 × 3 输入 × 双缓冲 + 握手 + 诊断） |
| `runs/hdmi_p2/case{1..6}_*` | 小向量（每个 ≤ 18 KB） |

## 2. 判据①：逐字节一致（含 stride ≠ W*3）—— **0 失配**

| case | W×H | stride | W*3 | 行尾 padding | fb_addr | 比对像素 | 失配 |
|---|---|---:|---:|---:|---|---:|---:|
| 1 | 40×30 | 128 | 120 | 8 B | `0x1000_0000` | 3600 | **0** |
| 2 | 64×20 | 192 | 192 | 0 B | `0x1000_8000` | 3840 | **0** |
| 3 | 40×30 | 136 | 120 | 16 B | `0x1000_4000` | 3600 | **0** |
| 4 | 640×4 | 1920 | 1920 | 0 B | `0x1000_0FC0` | 7680 | **0** |
| 5 | 40×30 | 128 | 120 | 8 B | `0x1000_1000` | 4800 | **0**（跨两张图） |

合计 **23520 个黄金比对像素、0 失配、0 坐标错**（case5 含换帧；case6 故意欠载，不参与黄金比对）。黄金 = `compose()` 的原始字节流；
DDR 镜像行尾填毒值 `0xAA`（误读 padding 必然失配）。case2 走 `compose()` 的
高度适配回退分支（左右黑边），其余走宽度适配分支（上下黑边）。

## 3. 判据②③：AXI 行为（同一 TB）

| 项 | case1 | case2 | case3 | case4 |
|---|---:|---:|---:|---:|
| 越界突发（有效区外 / padding / 越行） | 0 | 0 | 0 | 0 |
| 覆盖行数 | 30/30 | 20/20 | 30/30 | 4/4 |
| 每行累计字节（3 帧） | 360=3×120 | 576=3×192 | 360=3×120 | 5760=3×1920 |
| `arlen` 最大值 | 7 | 7 | 7 | 7 |
| `arsize≠8B` / `arburst≠INCR` / 未对齐 / 跨 4KB / 同行回退 | 0/0/0/0/0 | 同 | 同 | 同 |

## 4. 判据④：换帧落在 vsync（case5）

- 布置：buf0 与 buf1 放**两张完全不同的图案**（buf1 在 `FB_ADDR + align64(stride*H)`）；
- 激励：第 2 帧**中途**（第 1800 个像素时）写 `FRAME_ID=1`；
- 实测：`FRAME_ID_LATCHED=1`，期望图在**像素序号 2400 = 2×1200**（正好是帧边界）处切换，
  切换前最后一个像素属于旧帧，4800 个比对像素 **0 失配**（含切换前后）；
- 结论：一帧之内不混两帧 ⇒ 无撕裂；且 PS 在帧中途写入不会立即生效（等到 vsync）。

## 5. 判据⑤：人为欠载 ⇒ 粘滞位 + 计数器（case6）

把 AXI 从机的读延迟拉到 400 拍/突发（每行必被读空）：

| 时刻 | STATUS[2] | UNDERFLOW_CNT(0x2C) |
|---|---:|---:|
| 欠载 3 帧后 | **1** | **58** |
| `CTRL.soft_reset` 后 | **0** | **0** |

⇒ 两条都验：粘滞位（& 可被 `st_clr`/soft_reset 清）与累计计数（只被 soft_reset 清）。

## 6. 综合门禁（OOC `xc7z015clg485-2`，单实例）

| 项 | 实测 | 门禁 |
|---|---:|---|
| Slice LUTs | **587** | ≤ 600 ✅ |
| Slice Registers | 948 | — |
| Block RAM Tile | **1.5**（1×RAMB36 + 1×RAMB18） | ≤ 4 ✅ |
| DSP | 0 | 0 ✅ |
| 多驱动 / 组合环 / **latch** / RAM→寄存器 / ERROR | 0 / 0 / **0** / 0 / 0 | 全 0 ✅ |

## 7. 跨时钟域清单（每一位都点名）

| 信号 | 方向 | 手段 |
|---|---|---|
| cfg 快照（addr/stride/w/h/en/fsize） | AXI→PIX | bundled-data + toggle（数据先稳定，边沿整捆采样） |
| FRAME_ID(32b) | AXI→PIX | 同上，**vsync 边界**采样 |
| FRAME_ID_LATCHED(32b) | PIX→AXI | 反向同手法 |
| line 请求（行号+奇偶） | PIX→AXI | 同上 + ack 电平回程 |
| 行内写进度(12b) | AXI→PIX | gray 逐 bit 2FF |
| 行头（行号 + toggle） | AXI→PIX | bundled-data + toggle |
| 欠载事件（行级 1 bit） | PIX→AXI | toggle 2FF + 边沿检测（行级 ≥1 行间隔，不会并事件） |
| 行缓冲数据 | AXI→PIX | 真双口 BRAM（A 口写 / B 口读），读写序由 gray 进度保证 |
| soft_reset / st_clr / 粘滞位 | AXI⇄PIX | 单 bit 2FF（边沿检测） |

## 8. PS 驱动 `hdmi_out.py`

- `REG` 集中 12 个偏移（含扩容的 `0x2C`），**无散落魔数**；
- `HdmiOut(base, reg_io=None, fb_addr=None, mode=0)`：`reg_io` 可注入 ⇒ 主机单测；
  默认 `MmapRegIo`（`/dev/mem`+mmap，**不 import pynq**）；
- `set_mode / test_pattern / configure / put_frame / wait_frame / status / underflow / frame_latched`；
- 双缓冲：`put_frame` 第 n 次 ⇒ `FRAME_ID=n`，写 `buf=n&1`；`wait_frame` 轮询
  `FRAME_ID_LATCHED`，超时返回 `False`（画面保持旧帧，不撕裂）；
- **参数校验（直接 `ValueError`，不让硬件踩）**：`stride` 必须 64 B 对齐、`stride ≥ W*3`、
  `phys_addr` 64 B 对齐、`w ≤ 2048`、帧形状/dtype；
- `underflow()` = 0x2C 累计值 ⇒ **上板第一眼诊断**。

## 9. 主机自检 `tools/hdmi_selftest.py`（rc=0，PASS，119 条判据）

- 4 模式 × 3 输入（color/checker/photo）× 双缓冲 ⇒ `compose()` → **按行扫描的纯 Python
  模型**（8 拍突发拼字节流、每 3 B 切像素、行尾 padding 一个字节都不碰）→ 逐字节比对；
- 寄存器图 12 个偏移与 fsize/stride 公式（与 RTL 同一套）；
- `put_frame/wait_frame` 握手：帧号单调、buf 交替、**vsync 前不采纳**（防撕裂）；
- `0x2C` 欠载计数/soft_reset 清零 + 5 组参数校验。

## 10. 带宽账（决定模式选择的唯一硬指标）

| 模式 | 像素时钟 | 平均 DDR（W*H*fps*3） | **有效行内瞬时**（pix_clk×3） | 100 MHz AXI（300 MB/s）|
|---|---:|---:|---:|---|
| 720p60 | 74.25 MHz | 166 MB/s | **223 MB/s** | ✅ 余量 26% |
| 1080p30 | 74.25 MHz | 187 MB/s | **223 MB/s** | ✅ |
| 640×480p60 | 25.175 MHz | 55 MB/s | **76 MB/s** | ✅ |
| 1080p60 | 148.5 MHz | 373 MB/s | **445 MB/s** | ❌ 必须提 AXI 时钟 |

⇒ 模式选择**靠 0x2C 实测**：`UNDERFLOW_CNT ≡ 0` 才算余量够。

## 11. 已知边界 / 待办

- 1080p60 在 100 MHz AXI 下必然欠载（见 §10）⇒ 要么 s_axi_aclk ≥150 MHz，要么用 1080p30；
- P1 的 `hdmi_out_top` 必须接上 `cfg_upd` / `fid_upd`（配置与 FRAME_ID 的写脉冲），
  并把 `underflow_cnt` 挂到 AXI-Lite 0x2C、把 `st_clr` 接到 STATUS 读清；
- 帧内容仍由 PS 烘（`compose()`）；PL 不缩放、不懂 letterbox。
