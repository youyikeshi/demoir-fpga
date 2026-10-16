# ps/ —— PS 侧：PYNQ 的 ARM 上跑的 Python

> 这个目录里的代码是**控制层**，不是加速层。
> 它负责：读图 / 补边 / 量化 / 下发任务 / 等完成 / 拼结果 / 写盘 / 计时 / 报错。
> **它不负责**：任何"每个输出像素要 k×k×C_in 次乘加"的计算 —— 那些在 `../pl/`。
>
> 为什么边界这么画：见 [`../docs/切分契约.md`](../docs/切分契约.md) §2。

## 只依赖 NumPy

这三份都**不 import torch**。理由有两个，都不是洁癖：

1. PYNQ 的 rootfs 上装不了 torch（体积 + 依赖），但板上必须能跑通结果；
2. 等价性链条的中间环必须能在板上复现 —— 板子出问题时，第一件事是用
   `run_ref.py` 跑一遍，确认是"算法错"还是"加速器错"。

## 文件

| 文件 | 干什么 | 由谁解释 |
|---|---|---|
| `graph_spec.py` | 整张推理图的**唯一真相源**：把 `nets_student.py` 的模块嵌套摊平成 577 条算子，并推导每条的 (C,H,W)、记录权重在 state_dict 里的键 | [`../docs/切分契约.md`](../docs/切分契约.md) §3、§6 |
| `ops_ref.py` | 12 类 NumPy 参考算子（含 NCHW↔NHWC 转换）。**改算法先改这里** | [`../docs/流程_等价性验证.md`](../docs/流程_等价性验证.md) §4 |
| `run_ref.py` | 按图执行整网 + 图像前后处理（补边常数、裁回原尺寸）。可当命令行用 | 同上；补边/裁剪语义见 `../../src/infer.py` 文件头 |
| `schedule.py` | **要给 PL 发的东西**：静态任务表 + DDR 缓冲分配（按生命周期复用）。不碰硬件，现在就能跑 | [`../docs/切分契约.md`](../docs/切分契约.md) §5 |

## 用法

```bash
# 板上（或任何有 NumPy 的机器）：用导出的权重跑一张图
python3 ps/run_ref.py --image samples/phone/xxx.jpg --weights hls/build/weights/student.npz

# 只想看图的形状对不对（不需要权重）
python3 ps/graph_spec.py

# 看 PS 侧的执行计划：要发多少任务、要多少 DDR（不需要板子、不需要 overlay）
python3 ps/schedule.py --size 512
```

## 改这里的注意事项

* **形状不要手算**。`graph_spec` 的 `_Builder.add()` 会推导并在形状不自洽时抛错
  （比如 SAM 的 1/4 分支上采样回来对不上原尺寸）。手算的形状会悄悄过期。
* **权重键不要另写映射表**。`attrs['w']` / `attrs['b']` 直接就是 state_dict 的键，
  `../tools/export_weights.py` 靠它导出，另写一张表必然有一处先过期。
* **补边常数不能改**（`run_ref.PAD_VALUE`）。改了就跟 golden 对不上，
  而且症状是"只有图像边缘几行不对"，很难查。
