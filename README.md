# demoir-fpga

去摩尔纹（image demoiréing）模型的训练与部署准备。基于 **PDNet / ESDNet** 的
知识蒸馏方案：大的 teacher 模型只用于训练期指导，**最终部署到 FPGA 的只有小的 student**
（1.562 M 参数 / INT8 约 1.5 MB）。

目前状态：student 已用**官方全量训练集（4500 对）跑满 150 epoch**（论文量级），
评估 **Δ +4.70 dB / SSIM 0.7735**，已逼近 teacher 上界（+4.80 / 0.7777）——
用 36% 的参数量达到其 98% 的画质。量化与 FPGA 部署尚未开始。

> 权重随仓库提供：`weights/student/uhdm4k_s640_ep150.tar`（18.3 MB，已提交进 git），
> clone 下来即可直接 `python src/infer.py` 看效果，不必先凑齐移动硬盘上的大文件。

---

## 30 秒上手

```bash
# 1) 环境（Python 3.12；CUDA 版本按自己显卡的架构选）
python -m venv .venv-pdnet
.venv-pdnet\Scripts\activate                       # Windows
pip install torch==2.7.0 torchvision==0.22.0 --index-url https://download.pytorch.org/whl/cu118
pip install -r tools/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2) 大文件（权重、数据集）从移动硬盘还原
python tools/stage_hd_transfer.py --mode verify
python tools/stage_hd_transfer.py --mode restore

# 3) 看看效果 —— 把自己拍的照片丢进 samples/phone/，然后：
python src/infer.py
```

`src/infer.py` 会输出一张 4 列对比图：**原图 | student | teacher | 频率分析**。

> 所有命令都可以**从任意目录**运行。脚本内部一律用绝对路径（见 `src/paths.py`）。

---

## 目录结构

每块只干一件事，不需要读代码就知道该把东西放哪。

```
demoir-fpga/
├─ README.md            ← 你在看的这个，唯一入口
│
├─ src/                 ★ 我们的代码（唯一需要改的地方）
│  ├─ paths.py          所有路径的唯一真相源
│  ├─ vendor_boot.py    跑上游代码的封装：把"名字"解析成绝对路径
│  ├─ train.py          训练入口
│  ├─ eval.py           定量评估（需要配对真值，给 PSNR/SSIM）
│  ├─ infer.py          看效果（自己拍的照片，给可视化+频率分析）
│  └─ configs/          训练配置，一个数据集一个文件
│
├─ vendor/              上游原样代码，不要动
│  ├─ PDNet/            论文 ESDNet/PDNet 官方实现
│  └─ UHDM/             官方数据集评估代码
│
├─ datasets/            数据集                    ← 不进 git
│  └─ uhdm/{raw,pairs,train,eval}/
│
├─ runs/                训练产物，一次训练一个目录
│  ├─ 2026-09-10_uhdm4k_s512/        早期 student（400 张，6000 步，Δ +2.50）
│  └─ 2026-09-14_uhdm4k_s640_ep150/  ★ 当前推荐（4500 张全量，150 epoch，Δ +4.70）
│
├─ weights/             已发布权重 + weights.yaml 注册表
│  ├─ teacher/          论文作者的 teacher（4.396 M）
│  └─ student/          ★ uhdm4k_s640_ep150.tar（1.562 M，随仓库提供）
│
├─ samples/             输入图片
│  ├─ phone/            ★ 自己拍的照片丢这里
│  └─ synth/            脚本生成的合成数据（不进 git）
│
├─ experiments/         研究/分析脚本 + out/ 结果
├─ tools/               运维：下载、划分数据、搬运大文件
├─ docs/                文档
└─ _hd_transfer/        移动硬盘搬运暂存                 ← 不进 git
```

---

## 三条最常用的命令

```bash
# 看效果：自己拍的照片 → 4 列对比图（原图 | student | teacher | 频率分析）
python src/infer.py
python src/infer.py --image samples/phone/xxx.jpg --no-teacher

# 大图 / 显存紧张：分块推理（带显存护栏，避免把驱动撑到蓝屏）
python src/infer.py --image big.jpg --tile 2048 --overlap 512

# 定量评估：在数据集评估集上算 PSNR/SSIM（需要配对真值）
python src/eval.py --role student --limit 60
python src/eval.py --role teacher --limit 100 --crops-per-image 4   # 上界参考
python src/eval.py --role student --dataset aim --split eval        # 换数据集

# 训练 / 续训
python src/train.py --config uhdm_4k_s
python src/train.py --config uhdm_4k_s_resume
python src/train.py --config uhdm_4k_s_ep150        # 全量 4500 对 / 150 epoch
```

`--config` 直接给 `src/configs/` 下的名字即可（不加 `.yaml`）。
不确定有哪些可选，跑 `python src/train.py --list`。

---

## 换一个数据集重新训练

**只需改一行**：把配置里的 `DATA.DATASET` 换成 `datasets/` 下的目录名。

```yaml
DATA:
  DATA_TYPE: UHDM
  DATASET: uhdm        # -> datasets/uhdm/{train,eval}
```

然后按对应格式把数据放进 `datasets/<名字>/{train,eval}/`。
不同数据集要求的内部分目录结构不一样（UHDM 是扁平配对，AIM 要 `clear/`+`moire/`），
**清单见 `src/configs/README.md`**。现成模板：

| 配置 | 数据集 | 说明 |
|---|---|---|
| `smoke` | 合成数据 | 几分钟跑通链路，**不要用它评估效果** |
| `uhdm_4k_s` | UHDM 4K | 主力配置，原生裁 512（400 张，30 epoch） |
| `uhdm_4k_s_ep150` | UHDM 4K **全量** | **论文量级**：官方 4500 对，crop 640，150 epoch |
| `uhdm_4k_s_resume` | UHDM 4K | 续训 |
| `uhdm_4k_s_noteacher` | UHDM 4K | 关掉蒸馏，省 1.5 倍时间 |
| `aim_1k_s` | AIM2019 | 真拍屏 1024×1024（模板） |
| `tip_256_s` | TIP2018 | 小图 256×256（模板） |

临时试一次不必改配置，直接加参数：

```bash
python src/train.py --config uhdm_4k_s --dataset mydata --crop-size 384 --epochs 50
python src/train.py --config uhdm_4k_s --dataset-root D:/some/pairs_dir
```

---

## 约定

### 大文件一律不进 git

| 内容 | 走哪条路 |
|---|---|
| 代码 / 配置 / 文档 / 小结果图 | **Git** |
| 权重 `*.tar`、数据集 `datasets/`、`_hd_transfer/` | **移动硬盘** |
| Python 虚拟环境、VGG/LPIPS 权重缓存 | **哪都不走**，目标机器重建 |

搬运流程见 `docs/操作流程_双机协作.md`。**改了 `datasets/` 或 `weights/` 的路径就必须重新 stage**
（`_hd_transfer/payload/` 是按仓库路径镜像的，`manifest.txt` 里存了相对路径）。

### 不要改 vendor/

`vendor/` 是上游代码的快照。我们为了在 torch 2.7 / Python 3.12 / 单卡上跑通，
改过 9 处，清单记在 `vendor/README.md`。要跟进上游新版本时对着那份清单重新套用。

### 路径不要写死

新增脚本请 `import paths`，不要写 `'../../../datasets'` 这种相对路径 ——
历史上正因为配置里写相对路径，导致**必须在特定目录下运行**，换机器就崩。

---

## 进一步阅读

| 文档 | 内容 |
|---|---|
| `docs/目录结构说明.md` | 重构后的结构说明 + 从旧结构迁移的对照表 |
| `docs/交付说明_20260910.md` | 主要技术文档：修了哪些 bug、实验结果、四条假设的验证 |
| `docs/本机重训环境搭建_实测.md` | **在本机跑通全量训练**的实测记录：环境补齐、5 个坑、显存/吞吐实测、TDR 蓝屏排查 |
| `docs/操作流程_双机协作.md` | 两台机器之间怎么同步 |
| `docs/数据集获取方案.md` | 数据从哪下（含国内镜像） |
| `docs/交接文档_去摩尔纹训练部署.md` | 最初的技术路线决策 |
| `src/configs/README.md` | 各数据集的目录布局契约 |
| `vendor/README.md` | 上游代码状况 + 我们改过的 9 处 |
| `weights/README.md` | 权重来源与导出方式 |
| `datasets/README.md` | 数据集放法与新增步骤 |
| `runs/*/README.md` | 每次训练用了什么、结果如何 |
