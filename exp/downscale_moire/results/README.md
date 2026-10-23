# 结果目录说明

| 文件 | 内容 |
|---|---|
| `REPORT.md` | **总结报告**（自动生成，数字全部来自各 `*/metrics.json`） |
| `总对比_各算法_1比4.png` | **最直观的一张**：代表图 pair_22（强彩色摩尔纹）降到 1/4 后各算法对比 |
| `总对比_指标_1比4.csv` | 上图对应的指标 |
| `summary.csv` / `SUMMARY.md` / `summary_bars_*.png` | 全部记录的扁平表与折叠能量柱状图（按样本层分开） |
| `pil/` | PIL 五种重采样（nearest/box/bilinear/bicubic/lanczos）× 10 图 × 3 倍率 = 150 条 |
| `codec/` | JPEG 色度子采样 / 质量扫描 / WebP / AVIF / H.264 = 255 条 |
| `filters/` | ImageMagick 各滤波核 + 高斯 σ 扫描 + 理想带限参照 = 471 条（部分，仍在补） |
| `isp/` | Bayer 域 binning / 抽取 / YCbCr 4:2:0 顺序 = 210 条 |

## 复现

```bash
cd exp/downscale_moire
/data/work/venv-demoir/bin/python run_pil.py      # 或 run_codec.py / run_filters.py / run_isp.py
/data/work/venv-demoir/bin/python aggregate.py    # 汇总 CSV/柱状图
/data/work/venv-demoir/bin/python make_report.py  # 生成 REPORT.md
/data/work/venv-demoir/bin/python make_report_sheet.py --factor 4   # 出总对比图
```

## 关键口径（别踩坑）

- **样本要分层**：10 张里只有 4 张真有周期纹（pair_22 频带能量 24%，其余多数 <1%）。
  混在一起平均会把效应稀释成"看起来没影响"。
- **折叠能量优先用 `fold_lf`**：`alias_lf` 会把"过度模糊导致的低频损失"也算成折叠。
- **`moire_ratio` 可以被"糊"刷分**：过度模糊能让它低到 0.05 以下，必须配合 `blur_hf` 一起看。
