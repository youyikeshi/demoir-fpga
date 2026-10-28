# 论文库

> 这个目录只放**论文原文**，不放我们的结论。
> 我们的复现结论、踩坑记录、执行步骤在 [`../训练/`](../训练/)，两者用下面的对照表连起来。
>
> 为什么单独建一个目录：以后要看"这个方法当初是怎么写的、为什么这么设计"，
> 应该直接翻原文，而不是翻我们的二手总结。二手总结会走形，原文不会。

---

## 1. 索引

| 文件 | 论文 | 拿它干什么 | 我们的对应物 |
|---|---|---|---|
| [`2026_BinaryDemoire.pdf`](2026_BinaryDemoire.pdf) | BinaryDemoire: Moiré-Aware Binarization for Image Demoiréing（arXiv 2602.03176，SJTU，2026） | **当前主力参考**：1-bit 权重+激活的去摩尔纹网络 | [`../训练/执行文档_BinaryDemoire复现.md`](../训练/执行文档_BinaryDemoire复现.md) + `vendor/PDNet/model/binary_esdnet.py` |
| [`2026_BinaryDemoire_补充材料.pdf`](2026_BinaryDemoire_补充材料.pdf) | 同上，附录 | MABG 的**施加位置**策略、两组消融、更多可视化 | 执行文档 §5 |

外部资料（不在仓库里，本机路径 `../../../文献_去摩尔纹/`）：同主题另有 9 篇（ESDNet、PDNet、MoiréZero、QuantDemoire、UniDemoiré、DDA、DCID、HDRNet、AIM2019），需要时按同样命名搬进来。

---

## 2. BinaryDemoire 怎么读

### 2.1 一句话

它**不发明新的二值算子**。它先说"直接二值化 ESDNet 会掉 2.76 dB，比分类任务惨得多"，再证明这个损失有两个具体来源，然后各给一个便宜的补丁：
**MABG 补"通道级幅度"**（因为摩尔纹是频率结构化的），**SGRA 补"形状变化处的全精度通路"**（因为去摩尔纹 backbone 是多尺度 U-Net）。

判断一篇论文属于哪一类，看它的 ablation 是"新算子 vs 旧算子"还是"去掉我这个模块会怎样"。这篇是后者——它是一篇**诊断 → 对症下药**的论文，理解这一点，整篇的论证线就顺了。

### 2.2 论证结构（5 节各在干什么）

| 节 | 在做什么 | 读的时候注意 |
|---|---|---|
| §1 Intro | 立问题：摩尔纹是高频、有方向性、跨尺度；二值化是极端压缩；但没人做过去摩尔纹的二值化 | 它把"高频敏感"当成核心痛点，这是后面 MABG 存在的唯一理由 |
| §2 Related | 二值化线（XNOR-Net → BBCU → ReActNet → BiSRNet → Biper → BiMaCoSR）+ 去摩尔纹线 | 记住 BBCU 是它的 Base；BiSRNet 是它 STE 的来源 |
| §3.1 框架 | Eq.(1)(2)(3)：sign 二值化 + 两个实值缩放 α/β + 残差块；STE 训练 | **α 和 β 是卷积之后乘的标量**，这是它能上 FPGA 的全部原因 |
| §3.2 MABG | 单级 DWT 拿频域描述子 + 三个统计量 → 共享 FC → 逐通道门控 β | 成本几乎为零（DWT 是加减法，FC 只有 5 个权重），所以它敢只加在 1/3 的层 |
| §3.3 SGRA | g = gcd(Cin,Cout) 分区投影 + 交错重排 | 动机是"二值网络靠残差活着，但 U-Net 的 shape 一直在变" |
| §4 实验 | 4 数据集；Table 1 四行拆解；Table 2 对 5 个二值 + 4 个全精度 | 见 §2.3 的三个陷阱 |
| §5 结论 | 复述两个模块 + 压缩率 | 没有时延/硬件实验 |

### 2.3 三个读的时候要盯住的陷阱

1. **Table 1(a) 的 Params/Ops 列是错的。** Base 和 +MABG 都写 0.27M / 0.66G，但 Table 1(b) 里带 MABG 的完整模型是 0.31M / 0.78G。**以 1(b)/1(c) 为准。** 另外 §4.1 的 "PIPS" 是 LPIPS 笔误。
2. **OPs 是约定，不是实测。** 全文用 `Params_b = Params_f/32`、`OPs_b = OPs_f/64` 折算（BiBench 口径）。也就是说"0.78G OPs"没有任何一行是跑出来的。想上板就必须自己算真实位宽和带宽。
3. **"约 1/3 的层"不可复现。** 正文这么写，补充材料说"上/下采样 + 编解码通道调整，dense block / channel attention 不加"。按字面在 ESDNet 里只有 5 层符合。这个矛盾我们没法从论文消解，只能扫描校准——见执行文档 §5。

### 2.4 它**没**做的事（这些是可以做的增量）

| 缺口 | 说明 |
|---|---|
| 没有时延实验 | 标题、abstract、intro 都在强调 deployment，但全文只有 Params/OPs 两个折算量，没有一次真实推理计时 |
| 没有硬件验证 | 二值网络的价值全在 XNOR-popcount 上，论文没给任何硬件或指令级证据 |
| **MABG 强迫你维护全精度特征** | 门控要用 μ/σ/mean-abs 和 DWT 能量，SGRA 捷径也是 FP。所以"1-bit"只省了乘加和权重带宽，**特征图存储一点没省**——上板前必须先把这条数据流算清楚 |
| 只在 UHDM 上做消融 | FHDMi / TIP2018 / LCDMoiré 只有最终数字 |
| LCDMoiré 几乎无区分度 | 所有方法 SSIM 都在 0.99 以上，这个数据集基本只能看 PSNR |

---

## 3. 原文 ↔ 我们的实现 对照

改代码前先看这张表，能省掉一次全文搜索。

| 论文位置 | 我们的实现 |
|---|---|
| Eq.(1) 二值化 + 可学习阈值 `t` | `BinaryConv2d.forward` / `_ActSign` / `_WeightSign` |
| Eq.(2) `α`（XNOR-Net 逐输出通道） | `BinaryConv2d.forward` 里的 `alpha`（detach，不回传） |
| Eq.(2) `β`（MABG 逐输入通道门控） | `BinaryConv2d.forward(gate=...)` |
| Eq.(3) `RPReLU(Xbout) + SGRA(Xf)` | `BoundaryConv.forward` |
| §3.1 训练：tanh 平滑 STE / clipped STE | `_ActSign.backward` / `_WeightSign.backward` |
| Algorithm 1（MABG） | `MABG` + `HaarDWT` |
| §3.2 位置策略（补充材料） | `EncoderLevel.down`、`Decoder.preconv_*`；开关 `DEMOIR_MABG_SCOPE` |
| Eq.(8)(9)（SGRA） | `SGRA.forward`（分组卷积 + transpose 交错） |
| §4.1 训练设置 | `src/configs/uhdm_bin_ep150.yaml` |
| §4.2 Table 1 四行拆解 | 五级阶梯 `DEMOIR_MODE=fp/base/mabg/sgra/full` |
| §4.3 Table 2 | 执行文档附录 A |

---

## 4. 加新论文的约定

1. 命名 `年份_短名.pdf`，补充材料加 `_补充材料` 后缀，和现有文件保持一致；
2. 在上面 §1 的索引表里加一行，写清**拿它干什么**（不是写它讲了什么，那个 abstract 里有）；
3. 如果这篇有对应的实现，在 §3 的表里加映射；没有就留空，别硬凑。

> ⚠️ PDF 会进 git。单篇 20MB 量级还行，但别把整个文献夹（10 篇 ≈ 100MB）无脑搬进来 —— 只搬"我们要动手复现/对照"的那几篇。
