# tests/ —— 三段等价性验证

> 三段证据链的**为什么**、判据、以及"证明不了什么"，全都写在
> [`../docs/流程_等价性验证.md`](../docs/流程_等价性验证.md)。
> 本文件只说每个脚本跑什么、怎么用。

```text
PyTorch ──①──▶ NumPy ──②──▶ C++/HLS
                 ③ = 能不能综合
```

| 文件 | 段 | 跑法 | 需要 torch |
|---|---|---|---|
| `test_ops_numpy_vs_torch.py` | ① 逐算子 | `.venv-hls/bin/python hls/tests/test_ops_numpy_vs_torch.py` | 是 |
| `test_graph_numpy_vs_torch.py` | ① 整网 | `.venv-hls/bin/python hls/tests/test_graph_numpy_vs_torch.py --size 256` | 是 |
| `run_equiv.py` | ② 逐算子 | `.venv-hls/bin/python hls/tests/run_equiv.py --source graph --size 256` | 否 |

一次跑完：`make -C hls check SIZE=256`（需要 torch）；
没有 torch 的机器用 `make -C hls check-quick`（只跑 ②）。

## 三个脚本的"数据从哪来"

| 脚本 | 输入 | 期望输出 |
|---|---|---|
| `test_ops_numpy_vs_torch` | 随机数（故意用奇数边长、非 2 的幂缩放等网络里没有的组合） | PyTorch 原算子 |
| `test_graph_numpy_vs_torch` | `samples/phone/` 里的一张真实照片 → 补边 | `my_model.forward()`，取 `out_1` |
| `run_equiv` | 默认用**真实网络在该层的真实激活**；`--source random` 换成随机数 | `ps/ops_ref.py` 的对应算子 |

## 用例数据在哪

`../build/equiv/data/`（gitignore）。一个用例三个文件：

```text
t017_conv2d_in0.npy     ← 输入（已成 NHWC）
t017_conv2d_in1.npy     ← 权重（OIHW 原样，不转布局）
t017_conv2d_out.npy     ← op_runner 写出的结果
```

清单在 `../build/equiv/manifest.txt`，格式是 TAB 分隔的 5 列
（`id / kind / 输入文件 / 输出文件 / 参数`），`pl/tb/op_runner.cpp` 读它。
形状**从 .npy 头部读**，不在清单里重复写 —— 少一处会不一致的地方。

## 加用例

改 `run_equiv.py` 顶部的 `MAX_CASES`（每种算子取多少组"形状签名"）。
`SKIP_KINDS` 里的算子不参与 ②。

> 注意：用例是**按图遍历**生成的，不是按 `ps/run_ref.py: DISPATCH` 遍历的。
> 所以"实现了但图上没用到"的算子实现，不会被这里覆盖到 —— 这是刻意的，
> 目的是让"图里有的算子一定被测到"。反面是：删掉图里某个算子的同时留下它的
> C++ 实现，不会被发现。所以 `op_inventory.py` 的清单要一起看。
