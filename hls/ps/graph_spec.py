"""student 推理图的结构描述 —— 这条线上**唯一的真相源**。

为什么要有这个文件
------------------
`vendor/PDNet/model/nets_student.py` 是 PyTorch 写的，里面的每一条 `torch.cat`、
每一次 `F.interpolate` 都隐含了一次张量物化；而 FPGA 上的算子边界必须**显式**写出来。
所以这里用一层与框架无关的描述把整张图摊平：

    Op(name, kind, inputs, attrs, shape)      shape 一律 (C, H, W)，batch 恒为 1

有了它，下游三件事共用同一份描述，不会各写一遍各自过期：

  * `tools/op_inventory.py`  —— 遍历它算形状/MAC/权重字节，产出 `docs/算子清单.md`
  * `ps/run_ref.py`          —— 按它执行 NumPy 参考（= PS 软基线 + 等价性基准）
  * `tools/dump_golden.py`   —— 按它逐算子取 PyTorch 的 golden 张量

每条 Op 的 `attrs['w'] / attrs['b']` 是 **PyTorch state_dict 的键前缀**，
所以权重导出不需要再写一张映射表。

与 PyTorch 的两个刻意差异（都在 `docs/切分契约.md` 里解释）
----------------------------------------------------------
1. `conv2d` 把紧随其后的 ReLU 吃掉（`relu=True`）—— 原网络里所有 conv 后面接的
   都是 `nn.ReLU(inplace=True)`，融合是恒等变换，且省掉一次 DDR 往返。
2. `nn.Conv2d` 的 `padding` 是 symmetric 补零；这里保留成算子的 `pad` 参数，
   由 PL 侧的行缓冲处理，不物化成一张补齐的张量。
"""
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence, Tuple

Shape = Tuple[int, int, int]        # (C, H, W)

ARCH_STUDENT: Mapping[str, int] = {
    'en_f': 48, 'en_i': 32, 'de_f': 64, 'de_i': 32, 'sam': 1,
}


@dataclass(frozen=True)
class Op:
    """一条算子。`name` 同时是它产出的张量名（DAG 里一算子一张量）。"""
    name: str
    kind: str
    inputs: Tuple[str, ...]
    attrs: Mapping[str, Any]
    shape: Shape


@dataclass(frozen=True)
class Graph:
    ops: Tuple[Op, ...]
    input_name: str
    input_shape: Shape
    output_name: str

    def by_name(self, name: str) -> Op:
        for op in self.ops:
            if op.name == name:
                return op
        raise KeyError(name)

    def consumers(self, name: str) -> List[Op]:
        return [op for op in self.ops if name in op.inputs]


# --------------------------------------------------------------------------- #
# 形状推导（与 PyTorch 的规则逐条对齐，见 docs/切分契约.md 的"形状契约"）
# --------------------------------------------------------------------------- #
def conv_out(size: int, k: int, stride: int, pad: int, dilation: int) -> int:
    return (size + 2 * pad - dilation * (k - 1) - 1) // stride + 1


def _conv1d_out(size: int, k: int, pad: int) -> int:
    return size + 2 * pad - (k - 1)


class _Builder:
    """按 PyTorch 模块的嵌套顺序摊平算子，并在每次 emit 时推导形状/查重。"""

    def __init__(self, h: int, w: int):
        self._shapes: Dict[str, Shape] = {'input': (3, h, w)}
        self._ops: List[Op] = []
        self._seq = 0

    def shape(self, name: str) -> Shape:
        return self._shapes[name]

    def add(self, kind: str, inputs: Sequence[str], shape: Shape, **attrs: Any) -> str:
        self._seq += 1
        name = attrs.pop('name')
        if name == 'input' or name in self._shapes:
            raise ValueError('张量名重复：%s' % name)
        flat = dict(attrs)
        flat['_id'] = self._seq
        self._ops.append(Op(name, kind, tuple(inputs), flat, shape))
        self._shapes[name] = shape
        return name

    def build(self, out_name: str) -> Graph:
        return Graph(tuple(self._ops), 'input', self._shapes['input'], out_name)


# --------------------------------------------------------------------------- #
# 模块级展开 —— 与 nets_student.py 的 forward() 一一对应
# --------------------------------------------------------------------------- #
def _conv2d(b: _Builder, x: str, p: str, c_out: int, k: int, stride: int = 1,
            pad: int = 0, dilation: int = 1, bias: bool = True, relu: bool = False,
            name: str = None, seq: str = '') -> str:
    """`p` 是 nn.Conv2d 在 state_dict 里的模块路径；`seq` 是 nn.Sequential 的层号。"""
    c_in, h, w = b.shape(x)
    oh = conv_out(h, k, stride, pad, dilation)
    ow = conv_out(w, k, stride, pad, dilation)
    return b.add('conv2d', (x,), (c_out, oh, ow), name=name, k=k, stride=stride, pad=pad,
                 dilation=dilation, bias=bias, relu=relu,
                 w=p + seq + '.weight', b=(p + seq + '.bias') if bias else None)


def _ghost_block(b: _Builder, x: str, p: str, c_in: int, inter: int, d: int,
                 name: str) -> str:
    """GhostBlock：1×1 主卷积 + 3×3 膨胀卷积，通道拼接后截到 `inter`。"""
    init_c = math.ceil(inter / 2)
    new_c = init_c * (2 - 1)                      # ratio=2
    # oup=inter 时 init_c+new_c 恰好等于 inter，`out[:, :oup]` 是恒等切片；
    # 若以后改 ratio 或奇偶 inter，这里必须补一个 crop_c 算子。
    if init_c + new_c != inter:
        raise ValueError('GhostBlock 截断不是恒等：%d+%d != %d' % (init_c, new_c, inter))
    x1 = _conv2d(b, x, p + '.primary_conv', init_c, k=1, pad=0, bias=False, relu=True,
                 name=name + '.primary', seq='.0')
    x2 = _conv2d(b, x1, p + '.cheap_operation', new_c, k=3, pad=d, dilation=d,
                 bias=False, relu=True, name=name + '.cheap', seq='.0')
    return b.add('concat_c', (x1, x2), (inter, *b.shape(x)[1:]), name=name + '.cat')


def _ghost_module(b: _Builder, x: str, p: str, inter: int, d_list: Sequence[int],
                  residual: bool, name: str) -> str:
    """GhostModule(d_list) 的主体；`residual=True` 是 GhostModule，False 是 GhostModule1。"""
    t, c = x, b.shape(x)[0]
    for i, d in enumerate(d_list):
        _t = _ghost_block(b, t, '%s.ghost_layers.%d' % (p, i), c, inter, d,
                          '%s.g%d' % (name, i))
        t = b.add('concat_c', (_t, t), (b.shape(_t)[0] + c, *b.shape(t)[1:]),
                  name='%s.g%d.merge' % (name, i))
        c += inter
    t = _conv2d(b, t, p + '.conv', b.shape(x)[0], k=1, bias=True, name=name + '.proj')
    if residual:
        t = b.add('add', (t, x), b.shape(x), name=name + '.res')
    return t


def _eca(b: _Builder, x: str, p: str) -> str:
    """ECA(3C)：全局平均 → conv1d(k=9) → ReLU → 通道重排 → conv1d(k=9) → 反重排 → sigmoid。"""
    c, h, w = b.shape(x)
    t = b.add('global_avg_pool', (x,), (c, 1, 1), name=p + '.gap')
    # torch 里先走 conv2 再走 conv1（net 里就是这个名字，不要按 forward 顺序改）
    t = b.add('conv1d_c', (t,), (c, 1, 1), name=p + '.conv2', k=9, pad=4, relu=True,
              bias=False, w=p + '.conv2.weight', b=None)
    t = b.add('channel_shuffle', (t,), (c, 1, 1), name=p + '.shuffle', groups=3)
    t = b.add('conv1d_c', (t,), (c, 1, 1), name=p + '.conv1', k=9, pad=4, relu=False,
              bias=False, w=p + '.conv1.weight', b=None)
    t = b.add('channel_unshuffle', (t,), (c, 1, 1), name=p + '.unshuffle', groups=3)
    return b.add('sigmoid', (t,), (c, 1, 1), name=p + '.sigmoid')


def _sam(b: _Builder, x: str, p: str, c: int, d_list: Sequence[int], inter: int,
         name: str) -> str:
    """SAM：三个尺度的 GhostModule1 分支 + ECA 加权融合 + 残差。"""
    _, h, w = b.shape(x)
    y = {}
    for tag, scale in (('0', 1.0), ('2', 0.5), ('4', 0.25)):
        src = x if scale == 1.0 else b.add(
            'resize_bilinear', (x,), (c, int(h * scale), int(w * scale)),
            name='%s.down%s' % (name, tag), scale=scale)
        y[tag] = _ghost_module(b, src, '%s.basic_block%s' % (p, '' if tag == '0' else '_' + tag),
                               inter, d_list, residual=False, name='%s.bb%s' % (name, tag))
    for tag, scale in (('2', 2.0), ('4', 4.0)):
        y[tag] = b.add('resize_bilinear', (y[tag],), (c, h, w),
                       name='%s.up%s' % (name, tag), scale=scale)
    cat = b.add('concat_c', (y['0'], y['2'], y['4']), (3 * c, h, w), name=name + '.cat')
    attn = _eca(b, cat, p + '.fusion')
    # chunk 是 torch.chunk(attn, 3) 的**序号** 0/1/2，不是尺度标签 0/2/4
    for i, tag in enumerate(('0', '2', '4')):
        y[tag] = b.add('mul_channel', (attn, y[tag]), (c, h, w),
                       name='%s.attn%s' % (name, tag), chunk=i, chunks=3)
    s = b.add('add', (y['0'], y['2']), (c, h, w), name=name + '.sum02')
    s = b.add('add', (s, y['4']), (c, h, w), name=name + '.sum4')
    return b.add('add', (x, s), (c, h, w), name=name + '.res')


def _encoder_level(b: _Builder, x: str, p: str, level: int, sam_number: int,
                   name: str) -> Tuple[str, str]:
    feat = _ghost_module(b, x, p + '.rdb', ARCH_STUDENT['en_i'], (1, 2, 1),
                         residual=True, name=name + '.rdb')
    for i in range(sam_number):
        feat = _sam(b, feat, '%s.sam_blocks.%d' % (p, i), b.shape(feat)[0],
                    (1, 2, 3, 2, 1), ARCH_STUDENT['en_i'], '%s.sam%d' % (name, i))
    if level < 3:
        c = b.shape(feat)[0]
        down = _conv2d(b, feat, p + '.down', 2 * c, k=3, stride=2, pad=1, relu=True,
                       name=name + '.down', seq='.0')
        return feat, down
    return feat, None


def _decoder_level(b: _Builder, x: str, p: str, feat_num: int, sam_number: int,
                   name: str, want_feat: bool, want_out: bool) -> Tuple[str, str]:
    t = _ghost_module(b, x, p + '.rdb', ARCH_STUDENT['de_i'], (1, 2, 1),
                      residual=True, name=name + '.rdb')
    for i in range(sam_number):
        t = _sam(b, t, '%s.sam_blocks.%d' % (p, i), feat_num, (1, 2, 3, 2, 1),
                 ARCH_STUDENT['de_i'], '%s.sam%d' % (name, i))
    feat = None
    if want_feat:
        # 注意顺序：feat 用的是 SAM 之后的 t（与 torch 里 F.interpolate(x,...) 的 x 同一个）
        _, h, w = b.shape(t)
        feat = b.add('resize_bilinear', (t,), (feat_num, h * 2, w * 2),
                     name=name + '.feat', scale=2.0)
    out = None
    if want_out:
        out = _conv2d(b, t, p + '.conv.conv', 12, k=3, pad=1, name=name + '.head')
        c, h, w = b.shape(out)
        out = b.add('pixel_shuffle', (out,), (c // 4, h * 2, w * 2),
                    name=name + '.shuffle', r=2)
    return out, feat


def _encoder(b: _Builder) -> Tuple[str, str, str]:
    en_f = ARCH_STUDENT['en_f']
    x = b.add('pixel_unshuffle', ('input',), (12, b.shape('input')[1] // 2,
                                              b.shape('input')[2] // 2),
              name='enc.unshuffle', r=2)
    x = _conv2d(b, x, 'encoder.conv_first', en_f, k=5, pad=2, relu=True,
                name='enc.conv_first', seq='.0')
    y1, d1 = _encoder_level(b, x, 'encoder.encoder_1', 1, ARCH_STUDENT['sam'], 'enc1')
    y2, d2 = _encoder_level(b, d1, 'encoder.encoder_2', 2, ARCH_STUDENT['sam'], 'enc2')
    y3, _ = _encoder_level(b, d2, 'encoder.encoder_3', 3, ARCH_STUDENT['sam'], 'enc3')
    return y1, y2, y3


def _decoder(b: _Builder, y1: str, y2: str, y3: str, prune_heads: bool) -> str:
    """`prune_heads=True` 时删掉 out_2 / out_3 的图像输出头 —— 最终只用 out_1，
    而它们的 decoder 主体仍要产 feat_2 / feat_3。见文档 §3。"""
    de_f = ARCH_STUDENT['de_f']
    x3 = _conv2d(b, y3, 'decoder.preconv_3', de_f, k=3, pad=1, relu=True,
                 name='dec.preconv_3', seq='.conv.0')
    _, feat3 = _decoder_level(b, x3, 'decoder.decoder_3', de_f, ARCH_STUDENT['sam'],
                              'dec3', want_feat=True, want_out=not prune_heads)
    x2 = b.add('concat_c', (y2, feat3), (b.shape(y2)[0] + de_f, *b.shape(y2)[1:]),
               name='dec.cat2')
    x2 = _conv2d(b, x2, 'decoder.preconv_2', de_f, k=3, pad=1, relu=True,
                 name='dec.preconv_2', seq='.conv.0')
    _, feat2 = _decoder_level(b, x2, 'decoder.decoder_2', de_f, ARCH_STUDENT['sam'],
                              'dec2', want_feat=True, want_out=not prune_heads)
    x1 = b.add('concat_c', (y1, feat2), (b.shape(y1)[0] + de_f, *b.shape(y1)[1:]),
               name='dec.cat1')
    x1 = _conv2d(b, x1, 'decoder.preconv_1', de_f, k=3, pad=1, relu=True,
                 name='dec.preconv_1', seq='.conv.0')
    out1, _ = _decoder_level(b, x1, 'decoder.decoder_1', de_f, ARCH_STUDENT['sam'],
                             'dec1', want_feat=False, want_out=True)
    return out1


def build_graph(h: int, w: int, prune_heads: bool = True) -> Graph:
    """展开整张 student 推理图。`h/w` 是**补边之后**的输入尺寸（32 的倍数）。"""
    if h % 32 or w % 32:
        raise ValueError('输入尺寸必须是 32 的倍数（补边后），收到 %dx%d' % (w, h))
    b = _Builder(h, w)
    y1, y2, y3 = _encoder(b)
    out1 = _decoder(b, y1, y2, y3, prune_heads)
    return b.build(out1)


if __name__ == '__main__':
    g = build_graph(512, 512)
    kinds: Dict[str, int] = {}
    for op in g.ops:
        kinds[op.kind] = kinds.get(op.kind, 0) + 1
    print('算子总数 %d，种类 %d' % (len(g.ops), len(kinds)))
    for k in sorted(kinds):
        print('  %-18s %d' % (k, kinds[k]))
    print('输出 %s %s' % (g.output_name, g.by_name(g.output_name).shape))
