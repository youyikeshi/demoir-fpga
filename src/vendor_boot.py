"""运行 `vendor/` 下的上游代码，同时解除它们对 cwd 的依赖。

为什么要这一层
--------------
上游 `train.py` / `test.py` 依赖两个隐含约定：

  1. **必须在 `PDNet/` 目录下运行** —— 否则 `from config.config import args` 找不到模块；
  2. **配置里的数据集/权重路径是相对 cwd 的字符串** —— 直接交给 `os.walk` / `open`。

我们不去改上游代码，而是：

  * 把配置里的 `DATASET: uhdm` 解析成绝对路径 `datasets/uhdm/{train,eval}`，
    把 `TEACHER: ghost_34large` 解析成 `weights/teacher/...tar`；
  * 写一份**全绝对路径的快照** `runs/<run>/config.yaml`；
  * 用 `subprocess` 在 `vendor/PDNet/` 里跑上游脚本，`--config` 指向那份快照。

于是上游看到的一切都是它熟悉的形态（相对它自己的 cwd），而我们的入口不依赖 cwd。

为什么用 subprocess，而不是 runpy / 直接 import
----------------------------------------------
`config/config.py` 在 **import 时**就执行 `parser.parse_args()`，会去读我们自己的
命令行参数，轻则吞掉参数、重则直接报错退出。子进程让上游完整拥有自己的 `sys.argv`。

附带好处：上游训练历史上出现过进程级崩溃（`0xC0000374` 堆损坏），
子进程崩掉不会带走调用它的脚本。
"""
import datetime
import os
import subprocess
import sys

import yaml

import paths

# --------------------------------------------------------------------------- #
# 配置解析
# --------------------------------------------------------------------------- #
# 键 -> 所属 section。None 表示放在顶层（config.py 支持顶层标量）。
_SECTION = {
    'GPU_ID': 'GENERAL', 'SEED': 'GENERAL', 'WORKER': 'GENERAL',
    'SAVE_PREFIX': 'GENERAL', 'EXP_NAME': 'GENERAL', 'RUN': 'GENERAL',

    'DATA_TYPE': 'DATA', 'DATASET': 'DATA',
    'TRAIN_DATASET': 'DATA', 'TEST_DATASET': 'DATA',

    'EN_FEATURE_NUM': 'MODEL', 'EN_INTER_NUM': 'MODEL',
    'DE_FEATURE_NUM': 'MODEL', 'DE_INTER_NUM': 'MODEL', 'SAM_NUMBER': 'MODEL',

    'TEACHER': 'TEACHER_MODEL', 'T_load_path': 'TEACHER_MODEL',
    'T_en_f': 'TEACHER_MODEL', 'T_en_i': 'TEACHER_MODEL',
    'T_de_f': 'TEACHER_MODEL', 'T_de_i': 'TEACHER_MODEL', 'T_sam': 'TEACHER_MODEL',

    'BATCH_SIZE': 'TRAIN', 'LOADER': 'TRAIN', 'CROP_SIZE': 'TRAIN',
    'RESIZE_SIZE': 'TRAIN', 'SAVE_ITER': 'TRAIN', 'SAVE_EPOCH': 'TRAIN',
    'LOAD_EPOCH': 'TRAIN', 'LAM': 'TRAIN', 'LAM_P': 'TRAIN',

    'TEST_EPOCH': 'TEST', 'SAVE_IMG': 'TEST', 'LOAD_PATH': 'TEST',
    'EVALUATION_METRIC': 'TEST', 'EVALUATION_TIME': 'TEST', 'EVALUATION_COST': 'TEST',

    'EPOCHS': 'SOLVER', 'T_0': 'SOLVER', 'T_MULT': 'SOLVER',
    'ETA_MIN': 'SOLVER', 'BASE_LR': 'SOLVER',

    'USE_TEACHER': None,
}

_ARCH_KEYS = {
    'student': {'en_f': 'EN_FEATURE_NUM', 'en_i': 'EN_INTER_NUM',
                'de_f': 'DE_FEATURE_NUM', 'de_i': 'DE_INTER_NUM',
                'sam': 'SAM_NUMBER'},
    'teacher': {'en_f': 'T_en_f', 'en_i': 'T_en_i',
                'de_f': 'T_de_f', 'de_i': 'T_de_i', 'sam': 'T_sam'},
}


def config_path(cfg):
    """'smoke' -> src/configs/smoke.yaml；也接受直接的路径。"""
    if os.path.isabs(cfg) and os.path.exists(cfg):
        return cfg
    cand = os.path.join(paths.CONFIGS, cfg if cfg.endswith('.yaml') else cfg + '.yaml')
    if os.path.exists(cand):
        return cand
    if os.path.exists(cfg):
        return os.path.abspath(cfg)
    have = sorted(f for f in os.listdir(paths.CONFIGS) if f.endswith('.yaml'))
    raise SystemExit('找不到配置 %r。\n  src/configs/ 里现有：%s\n  也可直接给一个 yaml 路径。'
                     % (cfg, ', '.join(have) or '(空)'))


def read_config(cfg):
    with open(config_path(cfg), 'r', encoding='utf-8') as f:
        return yaml.load(f, Loader=yaml.FullLoader)


def load_weights_registry():
    """读 weights/weights.yaml。文件不存在时返回空表（并给出提示）。"""
    if not os.path.exists(paths.WEIGHTS_REGISTRY):
        return {}
    with open(paths.WEIGHTS_REGISTRY, 'r', encoding='utf-8') as f:
        reg = yaml.load(f, Loader=yaml.FullLoader) or {}
    return reg


def weight_entry(role, name):
    reg = load_weights_registry()
    entry = (reg.get(role) or {}).get(name)
    if not entry:
        have = ', '.join(sorted((reg.get(role) or {}).keys())) or '(空)'
        raise SystemExit('weights.yaml 里没有 %s/%s。\n  现有 %s：%s'
                         % (role, name, role, have))
    return entry


def weight_file(role, name):
    entry = weight_entry(role, name)
    p = entry.get('file')
    if not p:
        raise SystemExit('weights.yaml 的 %s/%s 缺少 file 字段' % (role, name))
    return p if os.path.isabs(p) else os.path.join(paths.WEIGHTS, p)


def _resolve_dataset(cfg, dataset=None, dataset_root=None,
                     train_dataset=None, test_dataset=None):
    """决定 TRAIN_DATASET / TEST_DATASET（绝对路径）。

    优先级：显式 train/test 参数 > --dataset-root > DATA.DATASET 名字 > 配置里原本的值。
    """
    data = cfg.setdefault('DATA', {})

    if train_dataset or test_dataset:
        pair = (train_dataset or data.get('TRAIN_DATASET'),
                test_dataset or data.get('TEST_DATASET'))
    elif dataset_root:
        pair = (os.path.join(dataset_root, 'train'), os.path.join(dataset_root, 'eval'))
    else:
        name = dataset or data.get('DATASET')
        if name:
            pair = (paths.dataset_dir(name, 'train'), paths.dataset_dir(name, 'eval'))
        else:
            pair = (data.get('TRAIN_DATASET'), data.get('TEST_DATASET'))

    for key, val in zip(('TRAIN_DATASET', 'TEST_DATASET'), pair):
        if not val:
            continue
        if not os.path.isabs(val):
            val = os.path.normpath(os.path.join(paths.REPO, val))
        data[key] = val
    return data.get('TRAIN_DATASET'), data.get('TEST_DATASET')


def check_dataset(split_dir, what):
    """给出比 os.walk 静默返回空列表更有用的报错。"""
    if not split_dir:
        return
    if not os.path.isdir(split_dir):
        raise SystemExit(
            '%s 目录不存在：%s\n'
            '  数据集应放在 datasets/<名字>/{train,eval}/，用 --dataset <名字> 指定；\n'
            '  也可以 --dataset-root <任意目录> 直接指向一个含 train/ 与 eval/ 的目录。\n'
            '  各数据集要求的内部布局见 src/configs/README.md'
            % (what, split_dir))


def _resolve_teacher(cfg, teacher=None):
    """TEACHER_MODEL.TEACHER 名字 -> 权重路径与结构参数（来自 weights.yaml）。"""
    tm = cfg.setdefault('TEACHER_MODEL', {})
    name = teacher or tm.get('TEACHER')
    if not name:
        return None
    entry = weight_entry('teacher', name)
    tm['TEACHER'] = name
    tm['T_load_path'] = weight_file('teacher', name)
    arch = entry.get('arch') or {}
    for k, v in arch.items():
        tm[_ARCH_KEYS['teacher'][k]] = v
    return name


def resolve_run_name(cfg, name=None, dataset=None):
    """运行目录名。

    续训必须落到**同一个**目录（否则 LOAD_EPOCH 找不到 checkpoint），
    所以配置里可以用 `GENERAL.RUN: <已存在的运行目录名>` 锁定它。
    """
    if name:
        return name
    general = cfg.setdefault('GENERAL', {})
    if general.get('RUN'):
        return general['RUN']
    ds = dataset or (cfg.get('DATA') or {}).get('DATASET') or 'data'
    crop = (cfg.get('TRAIN') or {}).get('CROP_SIZE')
    stamp = datetime.date.today().isoformat()
    return '%s_%s%s_s' % (stamp, ds, ('_c%d' % crop) if crop else '')


def apply_overrides(cfg, overrides):
    for k, v in (overrides or {}).items():
        if v is None:
            continue
        section = _SECTION.get(k)
        if section is None:
            cfg[k] = v
        else:
            cfg.setdefault(section, {})[k] = v
    return cfg


def resolve_config(cfg_name, run=None, overrides=None, dataset=None,
                   dataset_root=None, train_dataset=None, test_dataset=None,
                   teacher=None, out_root=None):
    """把一份逻辑配置解析成**全绝对路径**，并把快照写到 `runs/<run>/config.yaml`。

    cfg_name 可以是 src/configs/ 下的名字（如 'smoke'），也可以是一个 yaml 路径。
    返回 (snapshot_path, meta)：meta 记录了逻辑来源，便于事后追溯。
    """
    cfg = read_config(cfg_name)
    apply_overrides(cfg, overrides)

    src_config = config_path(cfg_name)
    ds_train, ds_eval = _resolve_dataset(
        cfg, dataset=dataset, dataset_root=dataset_root,
        train_dataset=train_dataset, test_dataset=test_dataset)
    teacher_name = _resolve_teacher(cfg, teacher=teacher)

    run = resolve_run_name(cfg, name=run, dataset=dataset)
    out_root = out_root or paths.RUNS
    base = os.path.join(out_root, run)

    general = cfg.setdefault('GENERAL', {})
    general['SAVE_PREFIX'] = base
    # EXP_NAME 置空 -> 上游的 os.path.join(SAVE_PREFIX, EXP_NAME, 'logs') 正好是
    # <base>/logs，目录名保持上游原样。
    general['EXP_NAME'] = ''
    general['RUN'] = run

    paths.make_dirs(base)
    snapshot = os.path.join(base, 'config.yaml')
    with open(snapshot, 'w', encoding='utf-8') as f:
        yaml.safe_dump(cfg, f, default_flow_style=False,
                       allow_unicode=True, sort_keys=False)

    def _rel(p):
        """仅用于写进 run_info.yaml 供人阅读，不参与程序逻辑。"""
        try:
            r = os.path.relpath(p, paths.REPO)
        except ValueError:
            return p
        return p if r.startswith('..') else r

    # ⚠️ 路径字段一律存**绝对路径**。绝不能存相对形式：
    #    调用方会拿它去做 os.path.isdir 之类的检查，而相对路径依赖 cwd，
    #    于是从别的目录运行就会出现"目录明明存在却报不存在"（曾经踩过）。
    #    需要短路径显示时用 paths.pretty()。
    meta = {
        'run': run,
        'created': datetime.datetime.now().isoformat(timespec='seconds'),
        'source_config': _rel(src_config),
        'dataset': (cfg.get('DATA') or {}).get('DATASET'),
        'train_dataset': ds_train,
        'test_dataset': ds_eval,
        'teacher': teacher_name,
        'crop_size': (cfg.get('TRAIN') or {}).get('CROP_SIZE'),
        'loader': (cfg.get('TRAIN') or {}).get('LOADER'),
        'epochs': (cfg.get('SOLVER') or {}).get('EPOCHS'),
        'use_teacher': cfg.get('USE_TEACHER', True),
        'overrides': {k: v for k, v in (overrides or {}).items() if v is not None},
        'host': os.environ.get('COMPUTERNAME') or os.environ.get('HOSTNAME') or '',
    }
    with open(os.path.join(base, 'run_info.yaml'), 'w', encoding='utf-8') as f:
        yaml.safe_dump(meta, f, default_flow_style=False,
                       allow_unicode=True, sort_keys=False)
    return snapshot, meta


# --------------------------------------------------------------------------- #
# 跑上游脚本
# --------------------------------------------------------------------------- #
def run_vendor(script, snapshot, extra_args=None, log_path=None):
    """在 vendor/PDNet 下用当前解释器跑一个上游脚本。

    cwd 设成 vendor/PDNet 只是为了与上游原本的用法完全一致；
    因为配置里已全是绝对路径，**调用方从哪里运行都无所谓**。
    """
    cmd = [paths.python_executable(), '-u',
           os.path.join(paths.VENDOR_PDNET, script), '--config', snapshot]
    if extra_args:
        cmd += [str(x) for x in extra_args]

    print('运行 vendor 脚本：')
    print('  cwd : %s' % paths.VENDOR_PDNET)
    print('  cmd : %s' % ' '.join(cmd))
    if log_path:
        print('  log : %s' % log_path)
    print()

    if log_path:
        paths.make_dirs(os.path.dirname(log_path))
        # 子进程强制 UTF-8 输出：否则 tqdm 的进度条字符按 GBK 写出，
        # 我们按 UTF-8 读会变成乱码（日志里全是替换字符）。
        env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        with open(log_path, 'w', encoding='utf-8', buffering=1) as f:
            proc = subprocess.Popen(cmd, cwd=paths.VENDOR_PDNET,
                                    env=env,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    bufsize=1, universal_newlines=True,
                                    encoding='utf-8', errors='replace')
            for line in proc.stdout:
                sys.stdout.write(line)
                f.write(line)
            return proc.wait()

    return subprocess.call(cmd, cwd=paths.VENDOR_PDNET)


# --------------------------------------------------------------------------- #
# checkpoint 定位
# --------------------------------------------------------------------------- #
def run_checkpoints(run):
    d = paths.run_subdirs(run)['net_checkpoints']
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith('.tar'))


def latest_checkpoint(run):
    p = os.path.join(paths.run_subdirs(run)['net_checkpoints'], 'checkpoint_latest.tar')
    return p if os.path.exists(p) else None


def resolve_student(spec):
    """把 `--student` 的取值解析成 (ckpt_path, arch_dict, 描述)。

    接受三种写法：
      * `uhdm4k_s512_ep30`  —— weights.yaml 里注册过的学生权重
      * `2026-09-10_uhdm4k_s512` —— 一个运行目录名（用它的 checkpoint_latest.tar）
      * 一个 .tar 文件的路径（结构参数取 weights.yaml 的默认学生结构）
    """
    if not spec:
        cands = [r for r in sorted(os.listdir(paths.RUNS))
                 if os.path.isdir(paths.run_dir(r))] if os.path.isdir(paths.RUNS) else []
        raise SystemExit('需要 --student。可选：\n'
                         '  * weights.yaml 里注册的学生权重名\n'
                         '  * 一个运行目录名（现有：%s）\n'
                         '  * 一个 .tar 文件路径' % (', '.join(cands) or '(无)'))

    reg = load_weights_registry()
    default_arch = dict(en_f=48, en_i=32, de_f=64, de_i=32, sam=1)

    entry = (reg.get('student') or {}).get(spec)
    if entry:
        return weight_file('student', spec), entry.get('arch') or default_arch, \
            '%s（weights.yaml）' % spec

    if os.path.exists(spec):
        return os.path.abspath(spec), default_arch, os.path.basename(spec)

    p = latest_checkpoint(spec)
    if p:
        cfg_file = os.path.join(paths.run_dir(spec), 'config.yaml')
        arch = default_arch
        if os.path.exists(cfg_file):
            with open(cfg_file, 'r', encoding='utf-8') as f:
                snap = yaml.load(f, Loader=yaml.FullLoader) or {}
            m = snap.get('MODEL') or {}
            arch = {k: m.get(v, default_arch[k]) for k, v in _ARCH_KEYS['student'].items()}
        return p, arch, 'runs/%s/checkpoint_latest.tar' % spec

    all_students = sorted((reg.get('student') or {}).keys())
    runs = sorted(r for r in os.listdir(paths.RUNS)) if os.path.isdir(paths.RUNS) else []
    raise SystemExit('找不到学生权重 %r。\n  weights.yaml 里的：%s\n  runs/ 目录：%s\n'
                     '  或直接给一个 .tar 路径' % (spec, ', '.join(all_students) or '(无)',
                                              ', '.join(runs) or '(无)'))


def resolve_teacher(spec):
    """把 `--teacher` 的取值解析成 (ckpt_path, arch_dict, 描述)。"""
    default_arch = dict(en_f=48, en_i=48, de_f=64, de_i=64, sam=1)
    reg = load_weights_registry()
    entry = (reg.get('teacher') or {}).get(spec)
    if entry:
        return weight_file('teacher', spec), entry.get('arch') or default_arch, \
            '%s（weights.yaml）' % spec
    if os.path.exists(spec):
        return os.path.abspath(spec), default_arch, os.path.basename(spec)

    names = sorted((reg.get('teacher') or {}).keys())
    raise SystemExit('找不到 teacher 权重 %r。\n  weights.yaml 里的：%s\n'
                     '  或直接给一个 .tar 路径；也可加 --no-teacher 只跑 student'
                     % (spec, ', '.join(names) or '(无)'))
