# vendor/ —— 上游代码快照

这两个目录是**别人写的代码**，我们基本不动。放这里是为了：

1. 一眼看出"哪些是我们写的、哪些是抄来的"；
2. 日后要跟进上游新版本时，知道自己改过哪几处（下面有清单）。

| 目录 | 来源 | 说明 |
|---|---|---|
| `PDNet/` | https://github.com/Cyvhee/PDNet （论文 ESDNet 官方实现） | 训练/模型/损失/数据加载 |
| `UHDM/` | UHDM 数据集官方仓库 | 官方评估协议、`figures/QR.jpg` 等资源 |

> 原始仓库里这两层是 `refer_code/PDNet/PDNet/` 和 `refer_code/UHDM/`（PDNet 套了两层同名目录），
> 重构时去掉了一层冗余嵌套。

---

## 我们改过的 9 处

上游代码在 **torch 2.7 / Python 3.12 / 单卡 / 非 4K 数据**下**无法运行**。
以下全部是**最小化修改**，改完都能跑通。跟进上游新版本时对着这份清单重新套用即可。

| # | 文件 | 问题 | 修复 |
|---|---|---|---|
| 1 | `train.py` | 硬编码 `CUDA_VISIBLE_DEVICES="0,1"`，teacher 固定在 `cuda:1` | 按实际卡数判定；单卡时 teacher 与 student 同卡 |
| 2 | `test.py` | import 了 **3 个不存在的模块**（`model_Distillation1` / `nets_student4` / `loss_util_Distillation1`） | 指向真实文件 |
| 3 | `utils/common.py` | vendored 调度器用 `verbose` 形参，torch ≥ 2.0 已移除 | `try/except` 回退 |
| 4 | `utils/common.py` | 调用了 `print_lr()`，torch ≥ 2.5 已删除 | `hasattr` 保护 |
| 5 | `utils/common.py` | `thop` 依赖 `distutils`，**Python 3.12 已移除** | 延迟导入 + 优雅降级 |
| 6 | `model/model.py` | `model_fn_decorator` 的 `device1` 是必填位置参数，但 `test.py` 不传 | 给默认值 `None` |
| 7 | `config/config.py` | `open()` 用系统 locale（中文 Windows 是 GBK），含中文注释的配置报 `UnicodeDecodeError` | 强制 `encoding='utf-8'` |
| 8 | `dataset/load_data.py` | `crop` 模式把 4K 尺寸写死 `4032×3024`（官方 test 实为 `3840×2160`）→ 裁剪越界补黑边 | 改读图片实际尺寸，并校验 `CROP_SIZE` |
| 9 | `utils/loss_util.py` | 蒸馏分支遍历 student 特征再索引 `T_feature_map[i]`，无 teacher 时 `IndexError` | 加 `if T_feature_map:` 守卫 |

### 新增的可选开关：`USE_TEACHER`

`train.py` 新增了 `USE_TEACHER`（默认 `True`，向后兼容）。设为 `False` 时不用 teacher，
损失退化成 `多尺度 L1 + VGG 感知`，约快 1.5 倍。实现是三处配合：

1. `train.py`：`USE_TEACHER=False` → `T_model = None`，跳过加载 teacher
2. `model/model.py`：`T_model is None` 时把 student 输出 `.detach()` 填进 teacher 槽位
   → 损失里 `l1_t == l1_s` → `a = 0` → 蒸馏项自动为零
3. `utils/loss_util.py`：加 `if T_feature_map:` 守卫（**必须**，否则空列表 `IndexError`）

### 一处**已实测证伪**的猜想，因此没改

原以为 teacher 前向漏了 `torch.no_grad()` 会浪费显存。**实测显存完全相同、速度差 0.5%（噪声内）**
—— teacher 参数冻结且输入不参与梯度，PyTorch 根本不建图。**原代码此处没问题，不要"优化"它。**

---

## 还有哪些是上游的、但不属于我们改动

| 文件 | 说明 |
|---|---|
| `PDNet/config/*.yaml` | 上游给的 9 份配置。里面是**作者机器的绝对路径**（`/home/data/...`），不能直接用；我们自己的配置在 `src/configs/` |
| `PDNet/scripts/download_*.sh` | 官方下载脚本（走 Google Drive） |
| `PDNet/environment.yaml` | 上游的 conda 环境（torch 老版本，与本机不同） |

> ⚠️ `PDNet/config/` 里那 9 份配置的数据路径**仍是作者的 `/home/data/...`**。
> 换数据集请用 `src/configs/` 下的配置 + `src/train.py`，不要改这里。
