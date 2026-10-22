"""融合出口（FUSE_OUT）用的 256 项标度查找表。

卷积引擎的可选出口（`out_lut`）在写回时做：

    v8 = saturate_int8(v)                 # 与旧版完全相同的饱和
    if (out_lut != 0) v8 = out_lut[v8 + 128]
    out[...] = v8

这正是层间算子 `cat` 原来"整遍 DDR 读→重定标→写"所做的事。把这张表交给
卷积写回路径后，`cat` 里标度不同的那一段就不必再单独重定标一遍
（省掉一次全张量 DDR 往返），且**逐位等价**。

本模块只放一个表实现 `rescale_lut_bytes`：`Runner._lut` 也直接调用它，
所以驱动、runner、离线对拍三方永远是同一套浮点表达式，不存在第二套公式。
"""
import numpy as np

LUT_LEN = 256          # int8 全值域


def rescale_lut_bytes(s_from, s_to):
    """标度重定标表（256 字节，`bytes.translate` 的映射表）。

    表项 = clip(rint(int8值 × s_from / s_to), -128, 127)，用 float64 计算。
    按 uint8 位模式排列后，索引正好是 `int8值 + 128`，因此既能给
    `bytes.translate` 用，也能直接给卷积引擎的 `out_lut[v8 + 128]` 用。

    **这是唯一实现**：`Runner._lut` 调用的就是本函数（缓存仍在 Runner 里）。
    """
    vals = np.arange(256, dtype=np.int16).astype(np.int8).astype(np.float64)
    tab = np.clip(np.rint(vals * (s_from / s_to)), -128, 127).astype(np.int8)
    return tab.tobytes()


def make_out_lut(s_from, s_to):
    """生成卷积融合出口用的 256 项 int8 LUT（索引 = sat8(v) + 128）。

    与 `Runner._lut(s_from, s_to)` 返回的字节表是同一份数据，这里只是转成
    numpy int8 数组，便于传给驱动/后端做位模式拷贝。
    """
    return np.frombuffer(rescale_lut_bytes(s_from, s_to),
                         dtype=np.int8).copy()


def apply_out_lut(t, lut):
    """模拟 RTL 的融合出口：v8 = lut[sat8(v) + 128]。

    `t` 已经是 int8（即已完成 relu + 饱和），所以"加 128 变成 uint8 位模式"
    与 `bytes.translate` 的索引完全一致；这里直接用后者，避免在 A9 上做
    numpy 花式索引。返回新数组（不修改入参）。
    """
    lut = np.asarray(lut, dtype=np.int8)
    if lut.size != LUT_LEN:
        raise ValueError("out_lut 必须是 %d 项 int8（实际 %d）" % (LUT_LEN, lut.size))
    tbl = np.ascontiguousarray(lut).tobytes()
    return np.frombuffer(np.ascontiguousarray(t).tobytes().translate(tbl),
                         dtype=np.int8).reshape(t.shape)
