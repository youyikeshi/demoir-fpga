# HDMI P2 阶段一报告：`hdmi_fb_scan.v` 骨架 + 布局逐字节一致

> 任务书：HDMI P2（真画面）—— 阶段一（交付 ① + 判据①），不建 BD、不出位流、不碰板子。
> 时间：2026-11-02 23:0x–23:3x；机器：本机，综合单实例（memgate/vlock）。

## 1. 交付物

| 文件 | 说明 |
|---|---|
| `pynq_specialized_code/pl/rtl_hand/modules/hdmi_fb_scan.v` | 扫描器主体（691 行，含契约/CDC 说明） |
| `pynq_specialized_code/pl/rtl_hand/tb/tb_hdmi_fb_scan.v` | xsim TB（AXI4 从机模型 + 时序发生器 + 逐字节比对） |
| `pynq_specialized_code/pl/rtl_hand/tools/gen_hdmi_scan_golden.py` | 用 `display_layout.compose()` 生成黄金向量（只调用，不改） |
| `pynq_specialized_code/pl/rtl_hand/tools/run_hdmi_scan_sim.sh` | 一键：生成向量 → `rm -rf xsim.dir` → xvlog/xelab(-debug off)/xsim |

## 2. 判据①实测（逐字节一致，含 stride ≠ W*3）

| case | W×H | stride | W*3 | 行尾 padding | fb_addr | 比对像素 | 失配 | 坐标错 |
|---|---|---|---|---:|---:|---|---:|---:|---:|
| 1 | 40×30 | 128 | 120 | 8 B | `0x1000_0000` | 3600 | **0** | 0 |
| 2 | 64×20 | 192 | 192 | 0 B | `0x1000_8000` | 3840 | **0** | 0 |
| 3 | 40×30 | 136 | 120 | 16 B | `0x1000_4000` | 3600 | **0** | 0 |
| 4 | 640×4 | 1920 | 1920 | 0 B | `0x1000_0FC0` | 7680 | **0** | 0 |

- 每 case 跑 **3 整帧**，共 **18720** 个有效像素逐字节比对 ⇒ `mismatches = 0`，`px_coord_bad = 0`。
- 黄金 = `display_layout.compose()` 的原始 `(H,W,3)` 字节流（每像素 `{R,G,B}`）；
  DDR 镜像按 stride 铺行、**行尾填毒值 `0xAA`** ⇒ PL 若误读 padding 必然失配。
- case2 走 `compose()` 的高度适配回退分支（左右黑边 12 px），case1/3 走宽度适配分支（上下黑边 5/5）。
- case4 是真实行宽（640×3=1920 B），基址 `0x...0FC0` 使首个突发正好落在 4 KB 页尾。

## 3. 判据②③⑤（已在同一 TB 里实测，确认后再连④一起做正式门禁）

| 项 | case1 | case2 | case3 | case4 |
|---|---:|---:|---:|---:|
| 越界突发（读有效区外/padding/越行） | 0 | 0 | 0 | 0 |
| 覆盖行数 | 30/30 | 20/20 | 30/30 | 4/4 |
| 每行累计字节（3 帧） | 360 = 3×120 | 576 = 3×192 | 360 = 3×120 | 5760 = 3×1920 |
| 突发数 | 180 | 180 | 180 | 360 |
| `arlen` 最大值 | 7 | 7 | 7 | 7 |
| `arsize≠8B` / `arburst≠INCR` / 未对齐 / 跨 4KB | 0/0/0/0 | 同 | 同 | 同 |
| 同行地址回退 | 0 | 0 | 0 | 0 |
| `STATUS[2] underflow` | 0 | 0 | 0 | 0 |

> 说明：突发统一取 **8 拍 = 64 B**（`arlen=7 ≤ 15`）。契约要求 stride 64 B 对齐 ⇒
> 64 B 对齐突发天然不跨 4 KB，于是省掉整套“到 4 KB 边界还剩几拍”的运算（~30 LUT）；
> 另留一条 14 LUT 的页尾保护，使任意 8 B 对齐 stride 也不会违规。

## 4. 综合门禁（OOC，`xc7z015clg485-2`，单实例）

| 项 | 实测 | 门禁 |
|---|---:|---|
| Slice LUTs | **563** | ≤ 600 ✅ |
| Slice Registers | 927 | — |
| Block RAM Tile | **1.5**（1×RAMB36 + 1×RAMB18） | ≤ 4 ✅ |
| DSP | 0 | 0 ✅ |
| 多驱动 `Synth 8-6859/6858` | 0 | 0 ✅ |
| 组合环 `Synth 8-295`/`LUTLP-1` | 0 | 0 ✅ |
| 推断 latch `Synth 8-327` | 0 | 0 ✅ |
| RAM→寄存器 `Synth 8-4767` | 0 | 0 ✅ |
| `ERROR:` | 0 | 0 ✅ |

（初版 755 LUT → 减到 563 的三步见 §6；563 为最终文件的实测值。）

## 5. 跨时钟域清单（每一位都点名）

| 信号 | 方向 | 手段 |
|---|---|---|
| cfg 快照 addr/stride/w/h/en/fsize | AXI→PIX | bundled-data + toggle 握手（数据先稳定，PIX 侧 3FF 取边沿整捆采样） |
| FRAME_ID(32b) | AXI→PIX | 同上，**vsync 边界**采样 |
| FRAME_ID_LATCHED(32b) | PIX→AXI | 反向同手法 |
| line 请求（行号+奇偶） | PIX→AXI | 同上 + ack 电平回程 |
| 行内写进度(12b) | AXI→PIX | gray 码逐 bit 2FF |
| 行头（行号 + toggle） | AXI→PIX | bundled-data + toggle |
| 行缓冲数据 | AXI→PIX | 真双口 BRAM（A 口写 `s_axi_aclk`、B 口读 `pix_clk`），读写序由 gray 进度保证 |
| soft_reset / 粘滞状态位 | AXI⇄PIX | 单 bit 2FF（边沿检测） |

## 6. LUT 从 755 降到 553 的三步

1. 全域 24-bit 写指针 gray（含 encode/decode/减法）→ **按行进度 12-bit gray + 行头握手**：−85
2. 取数按 **拍**记账（去掉 17-bit 字节计数/加法/减法），突发 16 拍→**8 拍**并删掉 4 KB 计算：−75
3. 缓冲占用改 **增量 5-bit `room`**（去掉 `8B`、`3P` 乘法与 14-bit 减法）、
   underflow 改一次 24-bit 字典序比较、配置改 `cfg_upd`/`fid_upd` 写脉冲：−60

## 7. 已知边界/待办

- **模式 1（1080p60，148.5 Mpx/s = 445 MB/s）在 100 MHz AXI 下会 underflow**
  （写侧稳态 1 像素/`s_axi_aclk` = 300 MB/s）⇒ 需把 `s_axi_aclk` 提到 ≥150 MHz（契约 §1 允许）。
  720p60（223 MB/s）/ 1080p30 / 640×480 在 100 MHz 下余量充足。
- 判据④（换帧落在 vsync）、⑤（人为制造行缓冲不足使 `STATUS[2]` 置位）的激励尚未加进 TB。
- ②`ps/pynq/hdmi_out.py`、③`tools/hdmi_selftest.py` 未开始（按任务书等确认）。
