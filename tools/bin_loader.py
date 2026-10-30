"""二值模型的统一加载器 —— 把"权重放对了但环境变量没设"这个坑堵住。

为什么需要它
------------
`model/binary_esdnet.py` 在 **import 时**读环境变量决定结构：

    DEMOIR_MODE = fp | base | mabg | sgra | full      # 决定有没有 MABG/SGRA 子模块
    DEMOIR_RDB  = 0 | 1                               # 决定用 RDB 还是 Ghost 骨架

于是同一个 `my_model(48,64,64,96,1)`，在 `MODE=base` 下是 227 个二值层，
在 `MODE=full` 下会多出 MABG 的 fc/short 参数 —— **先 import 再设变量就晚了**，
而且报错只是 `load_state_dict` 的一长串 missing keys，很难定位。

所以：权重文件里直接记下它需要的环境变量（`env` 字段），加载器负责
"先设环境变量、再 import、再建网、再 strict 加载"。

用法
----
    from bin_loader import load_binary

    net = load_binary('weights/student/binary_demoire_base_ep150.tar')          # 自动推断
    net = load_binary('runs/bin_base/net_checkpoints/checkpoint_000105.tar')   # 训练中间档也行
    net = load_binary(path, mode='full')                                      # 显式覆盖

命令行自检（不需要显卡）：

    python tools/bin_loader.py --ckpt weights/student/binary_demoire_base_ep150.tar
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(REPO, 'vendor', 'PDNet')
if VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

# 权重文件没写 env 时的兜底（base 档 = 论文之外的对照档，也是当前发布版）
DEFAULT_ENV = {'DEMOIR_BIN': '1', 'DEMOIR_MODE': 'base', 'DEMOIR_RDB': '1'}
# 训练中间档不写 arch，用发布版的通道数兜底（本项目二值模型只有这一套）
DEFAULT_ARCH = {'en': 48, 'en_i': 64, 'de': 64, 'de_i': 96, 'sam': 1}
VALID_MODES = ('fp', 'base', 'mabg', 'sgra', 'full')


def _apply_env(env):
    """把权重声明的环境变量写进 os.environ（不覆盖用户已显式设置的）。"""
    applied = {}
    for k, v in env.items():
        if os.environ.get(k) is None:
            os.environ[k] = str(v)
        applied[k] = os.environ[k]
    return applied


def peek(ckpt):
    """只读元信息，不建网络。返回 dict(arch, env, iters, lr, metrics)。"""
    import torch
    d = torch.load(ckpt, map_location='cpu', weights_only=False)
    return {
        'arch': d.get('arch'),
        'env': d.get('env') or dict(DEFAULT_ENV),
        'iters': d.get('iters'),
        'learning_rate': d.get('learning_rate'),
        'metrics': d.get('metrics'),
        'state_dict': d.get('state_dict', d),
    }


def load_binary(ckpt, arch=None, mode=None, rdb=None, device='cuda', strict=True):
    """按权重自带的 env 建网并加载。

    参数
    ----
    ckpt   : 权重 .tar 路径
    arch   : (en, en_i, de, de_i, sam) 元组或 dict，默认取权重里的 arch
    mode   : 覆盖 DEMOIR_MODE
    rdb    : 覆盖 DEMOIR_RDB
    device : 'cuda' / 'cpu'
    strict : True 时要求键完全匹配（默认，能立刻暴露结构不匹配）

    返回 eval() 模式的 nn.Module。
    """
    import torch

    meta = peek(ckpt)
    env = dict(meta['env'])
    if mode:
        env['DEMOIR_MODE'] = mode
    if rdb is not None:
        env['DEMOIR_RDB'] = str(rdb)

    bad = env.get('DEMOIR_MODE')
    if bad not in VALID_MODES:
        raise SystemExit('DEMOIR_MODE=%r 不合法，只能是 %s' % (bad, '/'.join(VALID_MODES)))

    applied = _apply_env(env)
    # 必须在设置完环境变量之后才 import —— 模块级常量在这里求值
    from model.binary_esdnet import my_model      # noqa: E402

    a = arch or meta['arch'] or DEFAULT_ARCH
    if isinstance(a, dict):
        params = (a['en'], a['en_i'], a['de'], a['de_i'], a.get('sam', 1))
    else:
        params = tuple(a)

    net = my_model(*params)
    r = net.load_state_dict(meta['state_dict'], strict=strict)
    if strict and (r.missing_keys or r.unexpected_keys):
        raise SystemExit('加载不匹配（结构或 MODE 不对）：missing=%d unexpected=%d\n'
                         '  权重声明的 env: %s\n  当前生效: %s'
                         % (len(r.missing_keys), len(r.unexpected_keys), env, applied))
    net = net.to(device).eval()
    net.load_info = {'arch': params, 'env': applied, 'iters': meta['iters'],
                     'metrics': meta['metrics'], 'ckpt': ckpt}
    return net


def main():
    import argparse
    ap = argparse.ArgumentParser(description='二值权重加载自检')
    ap.add_argument('--ckpt', default=os.path.join(REPO, 'weights/student/binary_demoire_base_ep150.tar'))
    ap.add_argument('--mode', default=None)
    args = ap.parse_args()

    meta = peek(args.ckpt)
    print('权重 : %s' % args.ckpt)
    print('arch : %s' % (meta['arch'],))
    print('env  : %s' % (meta['env'],))
    print('iters: %s  lr: %s' % (meta['iters'], meta['learning_rate']))
    print('指标 : %s' % (meta['metrics'],))

    net = load_binary(args.ckpt, mode=args.mode, device='cpu')
    from model.binary_esdnet import param_report
    tot, _bw, eq, nb = param_report(net)
    print('\nstrict 加载 OK')
    print('参数 %.3f M / 二值等价 %.3f M / 二值层 %d 层' % (tot, eq, nb))


if __name__ == '__main__':
    main()
