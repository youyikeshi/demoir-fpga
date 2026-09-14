"""项目路径的唯一真相源。

设计原则
--------
1. **一切从本文件位置推导**。所以无论你从哪个 cwd 运行脚本，得到的路径都一样。
   不要再在别的脚本里写 `'../../../uhdm_data'` 这种相对路径。

2. **本文件不 import 任何重依赖**（只有 os/sys），因此可以被任何脚本低成本导入，
   包括还没装 torch 的机器。

3. 只放"位置"，不放"配置"。数据集叫什么名、teacher 用哪个权重，
   由 `src/configs/*.yaml` 与 `weights/weights.yaml` 决定。

目录契约
--------
    REPO/
      src/            我们的代码（本文件所在处）
      vendor/         上游原样代码（PDNet / UHDM）
      datasets/       所有数据集                     <- 不含在 git 里
      runs/           训练产物（每次训练一个目录）   <- checkpoint 不含在 git 里
      weights/        已发布的权重 + weights.yaml 注册表
      samples/        输入图片（phone/ 自己拍的，synth/ 脚本生成的）
      experiments/    研究/分析脚本 + out/ 结果
      tools/          运维脚本（下载、划分、搬运）
      docs/           文档
      _hd_transfer/   移动硬盘搬运暂存
"""
import os
import sys

# src/paths.py -> src -> REPO
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SRC = os.path.join(REPO, 'src')
CONFIGS = os.path.join(SRC, 'configs')

VENDOR = os.path.join(REPO, 'vendor')
VENDOR_PDNET = os.path.join(VENDOR, 'PDNet')
VENDOR_UHDM = os.path.join(VENDOR, 'UHDM')

DATASETS = os.path.join(REPO, 'datasets')
RUNS = os.path.join(REPO, 'runs')
WEIGHTS = os.path.join(REPO, 'weights')
SAMPLES = os.path.join(REPO, 'samples')
EXPERIMENTS = os.path.join(REPO, 'experiments')
EXPERIMENTS_OUT = os.path.join(EXPERIMENTS, 'out')
TOOLS = os.path.join(REPO, 'tools')
DOCS = os.path.join(REPO, 'docs')

WEIGHTS_REGISTRY = os.path.join(WEIGHTS, 'weights.yaml')


def dataset_dir(name, split='train'):
    """datasets/<name>/<split>。split 惯例用 train / eval。"""
    return os.path.join(DATASETS, name, split)


def run_dir(run):
    """runs/<run>。"""
    return os.path.join(RUNS, run)


def run_subdirs(run):
    """上游 train.py 会在 <SAVE_PREFIX>/<EXP_NAME>/ 下建这三个目录。

    我们把 SAVE_PREFIX 指向 runs/<run>、EXP_NAME 置空，于是它们直接落在这里。
    目录名保持上游原样，少改上游代码。
    """
    base = run_dir(run)
    return {
        'base': base,
        'logs': os.path.join(base, 'logs'),
        'net_checkpoints': os.path.join(base, 'net_checkpoints'),
        'train_visual': os.path.join(base, 'train_visual'),
    }


def make_dirs(*paths):
    for p in paths:
        os.makedirs(p, exist_ok=True)


def add_vendor_pdnet_to_path():
    """把 vendor/PDNet 放到 sys.path 最前，使 `import model.nets_student` 等可用。

    上游代码内部一律用 `from model.xxx import` / `from utils.xxx import` 这种
    "以 vendor/PDNet 为根"的导入，所以只要这一句就够，不需要 os.chdir。
    """
    if VENDOR_PDNET not in sys.path:
        sys.path.insert(0, VENDOR_PDNET)
    return VENDOR_PDNET


def python_executable():
    """当前解释器。用于起子进程时保证与本次运行同一个环境。"""
    return sys.executable


def pretty(p):
    """仅用于打印：仓库内的路径显示成相对形式，短且跨机器可读。

    ⚠️ 不要用返回值做任何文件操作 —— 它依赖 cwd。要用就用原值（绝对路径）。
    """
    if not p:
        return p
    try:
        r = os.path.relpath(p, REPO)
    except ValueError:          # 不同盘符，无法求相对路径
        return p
    return p if r.startswith('..') else r
