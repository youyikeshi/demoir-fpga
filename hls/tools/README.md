# tools/ —— 只有开发机用（需要 torch）

> 这里的三份脚本**不在板上跑**。它们干的事是"把训练侧的东西翻译成 PS 侧能吃的形式"，
> 以及"从图里实测出预算"。板子上只需要 `build/weights/student.npz` 这一个产物。

## 为什么权重必须"翻译"一遍

训练产出的是 `.tar`（torch 的 `state_dict`，含结构和优化器状态）。
`ps/run_ref.py` 只说"我要 `decoder.preconv_3.conv.0.weight` 这个数组"，
而 PYNQ 上装不了 torch。所以中间必须有一份纯 NumPy 能读的 `.npz`。

## 文件

| 文件 | 干什么 | 产物 | 由谁解释 |
|---|---|---|---|
| `export_weights.py` | `.tar` → `.npz` + 清单。**只导出图上真正用到的张量** | `build/weights/student.npz`、`student_manifest.json` | [`../docs/切分契约.md`](../docs/切分契约.md) §3.3、§6 D2 |
| `op_inventory.py` | 遍历图实测算子清单：形状 / MAC / 权重 / DDR 流量 / 峰值驻留 | `../docs/算子清单.md`、`build/op_inventory.json` | 生成物自己开头 |
| （等价性脚本在 `../tests/`） | | | |

## 用法

```bash
.venv-hls/bin/python tools/export_weights.py                 # 默认 uhdm4k_s640_ep150
.venv-hls/bin/python tools/export_weights.py --student 2026-09-10_uhdm4k_s512
.venv-hls/bin/python tools/op_inventory.py --size 512
```

`--student` 的取值规则跟 `src/infer.py` 一致（weights.yaml 里的名字 / runs 下的运行目录名 /
一个 `.tar` 路径），因为它复用了 `src/vendor_boot.py` 的解析。**不要在这里另写一套路径解析。**

## 为什么"只导出用到的张量"

剪掉 `out_2`/`out_3` 输出头之后，`decoder.decoder_{2,3}.conv.conv.{weight,bias}`
这 4 个键在部署图上永远不会被读。导出全部的话，它们会白白占 DDR 和加载时间。
`export_weights.py` 每次都会打印"图未使用（未导出 N 个）"，**这个数字变了就说明图变了**。

## 为什么算子清单必须"实测"

`GhostModule1` 的 5 个 GhostBlock 入口通道是 48→80→112→144→176，
少算一个就少 30% 的 MAC。人肉展开这套嵌套必然出错，所以 `op_inventory.py`
直接遍历 `graph_spec` 展开出来的 577 条算子，形状在同一次遍历里传播 ——
**清单里的形状必然与图一致，不存在"尺寸表过期"这种问题。**
