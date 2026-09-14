# weights/ —— 权重与注册表

## 为什么有 `weights.yaml`

权重文件名本身**不说明**它是 teacher 还是 student，也不说明该用哪套通道数。
多机协作时最容易踩的坑就是"权重放对了、结构参数写错了"，而
`load_state_dict(..., strict=True)` 的报错是一长串 missing/unexpected keys，很难定位。

所以本项目规定：**凡是脚本要用到的权重，都在 `weights.yaml` 里登记**。
配置里只写逻辑名，路径和结构参数由注册表解析。

```yaml
# weights.yaml 片段
teacher:
  ghost_34large:
    file: teacher/ghost_34large_UHDM_ep150.tar
    arch: {en_f: 48, en_i: 48, de_f: 64, de_i: 64, sam: 1}
```

```yaml
# src/configs/uhdm_4k_s.yaml 片段
TEACHER_MODEL:
  TEACHER: ghost_34large        # 写名字，不写路径
```

## 目录

```
weights/
├── weights.yaml                        # 注册表（进 git）
├── README.md                           # 本文件（进 git）
├── teacher/
│   └── ghost_34large_UHDM_ep150.tar    # 论文作者训的 teacher，4.396 M 参数
└── student/
    └── uhdm4k_s512_ep30.tar            # 我们的 student，1.562 M 参数
```

`*.tar` 由根目录 `.gitignore` 排除（二进制、体积大、且有单文件 100MB 的推送限制），
**走移动硬盘搬运**，见 `_hd_transfer/README.md`。

## 两个模型的关系（容易误解，务必先读）

这是**知识蒸馏**，不是对抗训练，代码里没有判别器/GAN。

| | teacher | student |
|---|---|---|
| 参数量 | 4.396 M | 1.562 M |
| 中间通道 | en 48 / de 64 | en 32 / de 32 |
| 谁训练的 | **论文作者**（第 150 epoch） | **我们** |
| 训练时角色 | 冻结，只前向提供软标签 | 唯一被优化的网络 |
| 部署到 FPGA | **永远不会** | **只有它** |

- teacher **不是** student 的放大版或原始版本，而是**另一个独立训练的更大模型**，
  架构不同（通道数不同）。类比：老师不是"学生的早期版本"，而是另一个人。
- teacher 只是训练期的拐杖，最终上 FPGA 的只有 student。

## 加一个新权重

1. 把 `.tar` 放进 `weights/<teacher|student>/`
2. 在 `weights.yaml` 对应角色下加一个条目，写 `file` 与 `arch`
3. 若是自己训练产出的，顺手记 `run: <runs/ 下的目录名>`

`arch` 必须与 checkpoint 严格一致。从自己训练里导出发布版时，
结构参数可以直接抄 `runs/<run>/config.yaml` 的 `MODEL:` 段。

## 自己的训练产物在哪里

训练产物在 `runs/<run>/net_checkpoints/`，不在 `weights/`。
`weights/` 只放"要长期保留、要交给别人用"的权重。

评估/推理脚本两者都能直接吃：

```bash
# 用 runs/ 里的最新 checkpoint（正在训练的模型）
python src/infer.py --image xxx.jpg --student 2026-09-10_uhdm4k_s512

# 用 weights.yaml 里注册过的发布版
python src/infer.py --image xxx.jpg --student uhdm4k_s512_ep30
```
