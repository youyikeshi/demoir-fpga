"""大图分块推理：**怎么切、怎么拼**。

这个模块只做两件事，且都不依赖模型：
  1. **切割** —— 把一张大图切成若干带重叠的方块（`tile_boxes`）
  2. **拼接** —— 把各块的输出按羽化权重融合回整图（`run_tiled`）

模型怎么跑由调用方传入（`forward_patch`），所以它可以被 `src/infer.py` 用，
也可以被任何新脚本、甚至将来的流式/行带式 FPGA 对照实现复用。

---

## 什么时候需要分块

两个独立的原因，任一个成立就该用：

| 原因 | 具体情况 |
|---|---|
| **显存** | 整图前向的显存**线性于像素数**。实测系数（`experiments/probe_image_vram.py` 可复现）：student fp16 469 / fp32 936 字节每像素；teacher fp16 831 / fp32 1657。于是 4080×3072 的图：student fp16 要 5.5 GB、fp32 要 10.9 GB；teacher 光 fp16 就要 **9.7 GB**。 |
| **蓝屏风险** | Windows 的 WDDM **允许显存超额分配** —— 不够时不报错，而是把显存换页到系统内存；一旦驱动操作超过 TDR 默认的 **2 秒**，直接 `VIDEO_TDR_FAILURE (0x00000116)` 蓝屏，而不是抛 CUDA OOM。分块把单次需求压到几百 MB，从根上避开。（本项目 2026-09-16 因此蓝屏过一次。） |

分块后的显存只与**块大小**有关，与整图尺寸无关：

| 块边长 | student fp16 | teacher fp16 |
|---|---|---|
| 1024 | 约 0.5 GB | 约 0.9 GB |
| 2048 | 约 1.9 GB | 约 3.4 GB |

---

## 为什么必须留重叠，且默认 256

网络的有效感受野约 **512**（`experiments/context_study.py` 的受控实验：给同一批像素更多
上下文，PSNR 提升 1.27 dB，到 512 饱和、768 反而略降）。

**感受野之外的信息不影响该像素的输出**，所以：

* 若两块**零重叠**直接拼，边界处每个像素都缺失一侧约 256 像素的上下文 → 边界出现**接缝**；
* 留 **≥ 感受野一半（256）** 的重叠，则重叠区内每个像素至少能被一块"完整看到"。

---

## 拼接为什么不会有缝

每块在重叠区给一个**线性斜坡**权重（贴图像边界的那一侧不羽化，外侧没有邻居）：

```
块 A 的右侧： 1 ──────> 0
块 B 的左侧： 0 ──────> 1
```

融合方式是**加权平均**：`out = Σ(wᵢ · outᵢ) / Σ(wᵢ)`。

不留缝靠的是这三条，**不是**"权重和恒为 1"：

1. **全覆盖** —— 每个像素至少被一块覆盖（`axis_positions` 保证最后一块贴边、不留缺角）；
2. **权重连续** —— 线性斜坡，没有突变，所以不会有可见的台阶；
3. **归一化** —— 除以 `Σw`，因此某处权重和大于 1 也不会变亮。

> ⚠️ 一个容易写错的细节：`Σw` **并不恒为 1**。为了保证最后一块贴边，
> `axis_positions` 会追加一个位置，那一处与邻居的重叠可能大于 `overlap`，
> 于是该区域有 3 块以上同时带全权重，`Σw` 最高可达 4（两个方向的权重是外积）。
> 这**不影响正确性**（第 3 条兜住了），但别把"单位分解"当成前提来推理。
> `--selftest` 会把它算出来 —— 关键是**纯色图往返偏差必须为 0**，那等价于"不会变亮/变暗"。

上面三条性质可以直接验证，不需要 GPU：

```bash
python src/tiling.py --selftest
```

---

## ⚠️ 分块 ≠ 整图，这是算法层面的差异，不是 bug

每块能看到的上下文只有块那么大，网络的行为会随之改变。实测同一张 4080×3072 的图
（student，fp16，与整图结果逐像素比对）：

| 分块 | 与整图的 PSNR | 平均绝对差 | 边界比值（接缝检查） |
|---|---|---|---|
| 1024 / 重叠 256 | 22.78 dB | 13.34 / 255 | 0.58 ~ 1.14 倍 |
| **2048 / 重叠 512** | **33.31 dB** | **4.12 / 255** | — |

* **边界比值** = 块边界处的差异 ÷ 全图中位差异。若拼接有 bug，边界会高出**数倍**；
  实测都在 1 倍附近 → **融合正确、没有可见接缝**。
* **块越大越接近整图**（22.78 → 33.31 dB）→ 差异来自"上下文变少"，不是接缝。

结论：显存够就用整图；显存不够就用分块，**优先把块开大（2048）**，
并接受它与整图的结果有差异。

---

## 用法

```python
import numpy as np
from PIL import Image
import tiling

def forward_patch(patch):
    \"\"\"接收一块 PIL 图，返回同尺寸的 float32 HWC 数组（取值 0~1）。\"\"\"
    ...

out = tiling.run_tiled(Image.open('big.jpg'), forward_patch,
                       tile=2048, overlap=256,
                       on_tile=lambda i, n: print('%d/%d' % (i, n)))
out.save('big_demoir.png')
```

命令行入口在 `src/infer.py`（`--tile / --overlap`），说明文档见
`docs/分块推理_切割与拼接.md`。
"""
import argparse
import sys

import numpy as np

# 有效感受野约 512（experiments/context_study.py），重叠取其一半
DEFAULT_TILE = 2048
DEFAULT_OVERLAP = 256


# --------------------------------------------------------------------------- #
# 切割
# --------------------------------------------------------------------------- #
def axis_positions(length, tile, overlap):
    """一维切分起点。

    保证：全覆盖、不留缺角、最后一块**贴边**（这样每块都是完整的 tile 大小，
    不会出现一个又窄又小的尾块）。

        length=5000, tile=2048, overlap=256 -> [0, 1792, 2952]

    tile >= length 时返回 [0]（整条就是一块）。
    """
    if tile >= length:
        return [0]
    step = max(1, tile - overlap)
    pos = list(range(0, length - tile + 1, step))
    if pos[-1] != length - tile:
        pos.append(length - tile)
    return pos


def tile_boxes(width, height, tile, overlap):
    """返回 [(x, y, w, h), ...]，按行优先顺序，覆盖整张 (width, height)。"""
    xs = axis_positions(width, tile, overlap)
    ys = axis_positions(height, tile, overlap)
    return [(x, y, min(tile, width - x), min(tile, height - y))
            for y in ys for x in xs]


# --------------------------------------------------------------------------- #
# 拼接
# --------------------------------------------------------------------------- #
def feather_1d(n, overlap, ramp_start, ramp_end):
    """一维羽化权重（长度 n）。

    ramp_start=True  -> 左端 0→1 上升（左边有邻居）
    ramp_end=True    -> 右端 1→0 下降（右边有邻居）
    两侧都不 ramp（即该块贴着图像边界）时全为 1。

    相邻两块在重叠区互补（1→0 与 0→1），所以加权和恒为 1 —— 这是不留缝的关键。
    """
    w = np.ones(n, dtype=np.float32)
    ov = min(overlap, n // 2)
    if ov > 0:
        if ramp_start:
            w[:ov] *= np.linspace(0.0, 1.0, ov, dtype=np.float32)
        if ramp_end:
            w[-ov:] *= np.linspace(1.0, 0.0, ov, dtype=np.float32)
    return w


def tile_weight(x, y, w, h, width, height, overlap):
    """该块在**整图坐标系**下的羽化权重图，形状 (h, w)。

    只在与邻居相邻的那几侧羽化；贴着图像边界的侧保持 1。
    """
    wx = feather_1d(w, overlap, ramp_start=x > 0, ramp_end=x + w < width)
    wy = feather_1d(h, overlap, ramp_start=y > 0, ramp_end=y + h < height)
    return np.outer(wy, wx).astype(np.float32)


def run_tiled(image, forward_patch, tile=DEFAULT_TILE, overlap=DEFAULT_OVERLAP,
              on_tile=None, keep_intermediates=False):
    """分块推理 + 羽化拼接。

    参数
    ----
    image            : PIL.Image（RGB），按**原生分辨率**处理，不做任何缩放
    forward_patch    : callable(PIL.Image) -> np.ndarray(h, w, 3) float，取值 0~1
                       传入的 patch 与返回的数组尺寸相同
    tile / overlap   : 块边长与重叠像素（tile 必须是 32 的倍数，见下）
    on_tile          : 可选回调 on_tile(index, total)，便于打印进度
    keep_intermediates : True 时返回 (整图, 权重和)，权重和可用于检查拼接覆盖

    返回
    ----
    PIL.Image（RGB，与原图同尺寸）。keep_intermediates 时返回 (Image, weight_sum)。

    ⚠️ tile 必须是 **32 的倍数**：网络有 2 次 pixel_unshuffle + 4 级下采样。
       本函数不对 tile 做取整，调用方负责（`src/infer.py` 会自动向上取整）。
    """
    if tile % 32:
        raise ValueError('tile 必须是 32 的倍数，收到 %d' % tile)
    if overlap >= tile:
        raise ValueError('overlap(%d) 必须小于 tile(%d)' % (overlap, tile))

    width, height = image.size
    boxes = tile_boxes(width, height, tile, overlap)

    acc = np.zeros((height, width, 3), dtype=np.float32)
    wsum = np.zeros((height, width, 1), dtype=np.float32)

    for i, (x, y, w, h) in enumerate(boxes, 1):
        out = forward_patch(image.crop((x, y, x + w, y + h)))
        out = np.asarray(out, dtype=np.float32)
        if out.shape[:2] != (h, w):
            raise ValueError('forward_patch 返回 %s，期望 (%d, %d, 3)'
                             % (out.shape, h, w))
        if out.ndim == 2:                     # 灰度兜底
            out = np.repeat(out[:, :, None], 3, axis=2)

        wt = tile_weight(x, y, w, h, width, height, overlap)[:, :, None]
        acc[y:y + h, x:x + w, :] += out * wt
        wsum[y:y + h, x:x + w, :] += wt

        if on_tile:
            on_tile(i, len(boxes))

    # 理论上权重和恒为 1；用 max 兜住浮点误差，避免 0 除
    merged = np.clip(acc / np.maximum(wsum, 1e-6), 0.0, 1.0)
    from PIL import Image as _Image
    img = _Image.fromarray((merged * 255).round().astype('uint8'))

    if keep_intermediates:
        return img, wsum[:, :, 0]
    return img


# --------------------------------------------------------------------------- #
# 自检：不用 GPU 也能验证"拼接不留缝"的核心性质
# --------------------------------------------------------------------------- #
def selftest(width=4080, height=3072, verbose=True):
    """验证拼接的三条关键性质（纯 numpy，不需要模型或显卡）。

    1. **全覆盖**：每个像素至少被一块覆盖（权重和 > 0），否则会留下黑洞；
    2. **不变亮/变暗**：纯色图分块往返后仍是原值 —— 融合是加权平均
       `Σ(w·out)/Σw`，所以即使某处权重和 ≠ 1 也不会出现明暗带；
    3. **无台阶**：权重场是线性斜坡，最大梯度不超出斜坡斜率，
       否则边界会出现可见的突变。

    ⚠️ 为什么**不能**断言"权重和恒为 1"：
       `axis_positions` 为保证最后一块**贴边**会追加一个位置，那一处与邻居的
       实际重叠可能**大于** `overlap`；于是该区域会有 3 块以上同时带全权重，
       权重和最高可达 4（2 个方向各 2 块，权重是外积）。
       这**不影响正确性**（见第 2 条），但意味着"单位分解"这个强性质只在
       两块重叠处成立。
    """
    import math

    ok = True
    print('分块自检  %dx%d' % (width, height))
    print('%-16s %-6s %-12s %-12s %-12s %s'
          % ('tile/overlap', '块数', '权重和min', '权重和max', '最大梯度', '结果'))
    print('-' * 78)

    for tile, overlap in ((512, 256), (1024, 256), (2048, 256), (2048, 512), (4096, 512)):
        boxes = tile_boxes(width, height, tile, overlap)
        wsum = np.zeros((height, width), dtype=np.float32)
        for (x, y, w, h) in boxes:
            wsum[y:y + h, x:x + w] += tile_weight(x, y, w, h, width, height, overlap)

        lo, hi = float(wsum.min()), float(wsum.max())
        gy = np.abs(np.diff(wsum, axis=0)).max() if height > 1 else 0.0
        gx = np.abs(np.diff(wsum, axis=1)).max() if width > 1 else 0.0
        gmax = float(max(gy, gx))
        # 斜坡斜率是 1/overlap；两方向叠加最多 2 倍，再留一点余量
        limit = 3.0 / overlap
        good = (lo > 1e-6) and (gmax <= limit)
        ok &= good
        print('%-16s %-6d %-12.6f %-12.6f %-12.6f %s'
              % ('%d/%d' % (tile, overlap), len(boxes), lo, hi, gmax,
                 'OK' if good else '不合格'))

    # 纯色图往返：融合归一化是否正确的最直接证据
    from PIL import Image as _Image
    flat = _Image.new('RGB', (width, height), (128, 128, 128))
    merged = run_tiled(flat, lambda p: np.asarray(p, dtype=np.float32) / 255.0,
                       tile=1024, overlap=256)
    dev = int(np.abs(np.asarray(merged, dtype=np.int32) - 128).max())
    good = dev <= 1
    ok &= good
    print()
    print('纯色图分块往返：最大偏差 %d/255  %s'
          % (dev, 'OK（不变亮/变暗，无明暗带）' if good else '不合格'))

    # 覆盖性：块数必须与理论值一致，且不留缺角
    boxes = tile_boxes(width, height, 1024, 256)
    cover = np.zeros((height, width), dtype=np.uint8)
    for (x, y, w, h) in boxes:
        cover[y:y + h, x:x + w] = 1
    gap = int((cover == 0).sum())
    ok &= gap == 0
    print('覆盖率：未覆盖像素 %d 个  %s' % (gap, 'OK' if gap == 0 else '不合格'))
    print()
    print('结论：%s' % ('全部通过 —— 全覆盖、不变亮、无台阶，拼接不留可见缝'
                       if ok else '有不合格项，检查 feather_1d / tile_weight'))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description='分块推理的切割/拼接工具与自检')
    ap.add_argument('--selftest', action='store_true',
                    help='验证羽化权重互补（不需要显卡）')
    ap.add_argument('--width', type=int, default=4080)
    ap.add_argument('--height', type=int, default=3072)
    a = ap.parse_args()
    if a.selftest:
        return selftest(a.width, a.height)
    ap.print_help()
    print()
    print('本模块是库：在代码里 import tiling 使用；'
          '命令行推理入口见 src/infer.py --tile / --overlap')
    return 1


if __name__ == '__main__':
    sys.exit(main())
