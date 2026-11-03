#!/usr/bin/env python3
"""torchless_ckpt.py —— 不装 PyTorch 也能读 torch.save 出来的 checkpoint

为什么需要它：导出机（机器 B）要求 PyTorch，但这台机器上 `import torch` 直接
ModuleNotFoundError。而 checkpoint 本质是 **zip + pickle + 裸字节**：

    <prefix>/data.pkl            顶层 dict 的 pickle
    <prefix>/data/<key>          每块 storage 的裸字节（小端）
    <prefix>/byteorder           'little'

所以只要给 pickle 提供两个"假 torch"符号，就能把 state_dict 还原成 numpy 数组：

    torch._utils._rebuild_tensor_v2(...)   把 storage 切片 + as_strided
    torch.FloatStorage / HalfStorage / ...  persistent_id 里出现的 storage 类型

**只读**，不写任何 checkpoint。读出来的是 numpy 视图（零拷贝 or 一次 frombuffer），
调用方自己做 `.astype(np.float32)` 即可。

用法：
    from torchless_ckpt import load_ckpt
    obj = load_ckpt("runs/.../checkpoint_latest.tar")   # 顶层 dict
    sd  = obj["state_dict"]                             # {name: np.ndarray}

    # 命令行自检（打印顶层键 + state_dict 形状，并校验 dtype/字节序）
    python3 torchless_ckpt.py runs/bin_base/net_checkpoints/checkpoint_latest.tar
"""
import os
import pickle
import sys
import types
import zipfile

import numpy as np

# pickle 里的 storage 类型名 → numpy dtype（小端；byteorder 文件另查）
_STORAGE_DTYPES = {
    "DoubleStorage": "<f8", "FloatStorage": "<f4", "HalfStorage": "<f2",
    "BFloat16Storage": None,                       # 需要手工解释
    "LongStorage": "<i8", "IntStorage": "<i4", "ShortStorage": "<i2",
    "CharStorage": "i1", "ByteStorage": "u1", "BoolStorage": "?",
    "ComplexFloatStorage": "<c8", "ComplexDoubleStorage": "<c16",
}


class _Storage:
    """一块 storage：key 指向 zip 里的 <prefix>/data/<key>。"""

    __slots__ = ("key", "numel", "_buf", "_zip", "_prefix", "_dtype")

    def __init__(self, zipf, prefix, key, dtype):
        self.key, self.numel = str(key), 0
        self._zip, self._prefix = zipf, prefix
        self._dtype = dtype
        self._buf = None

    def buf(self):
        if self._buf is None:
            raw = self._zip.read("%s/data/%s" % (self._prefix, self.key))
            if self._dtype is None:                # bf16：按 uint16 读，后面转 float32
                arr = np.frombuffer(raw, dtype="<u2")
                up = arr.astype(np.uint32) << 16
                self._buf = up.view(np.float32)
            else:
                self._buf = np.frombuffer(raw, dtype=self._dtype)
        return self._buf


class _FakeTorch:
    """把 pickle 需要的 `torch.*` 符号注册成真模块，避免 import 真的 torch。"""

    def __init__(self, zipf=None, prefix=None):
        self._zip, self._prefix = zipf, prefix

        torch = types.ModuleType("torch")
        utils = types.ModuleType("torch._utils")
        nn = types.ModuleType("torch.nn")
        param = types.ModuleType("torch.nn.parameter")
        serial = types.ModuleType("torch.serialization")

        for name, dt in _STORAGE_DTYPES.items():
            cls = type(name, (), {"_dtype": dt, "_module": "torch"})
            setattr(torch, name, cls)

        utils._rebuild_tensor_v2 = self._rebuild_tensor_v2
        utils._rebuild_tensor = self._rebuild_tensor_v2
        utils._rebuild_parameter = self._rebuild_parameter
        param.Parameter = self._Parameter
        serial._rebuild_tensor_v2 = self._rebuild_tensor_v2

        torch._utils, torch.nn = utils, nn
        nn.parameter = param
        torch.serialization = serial
        torch.Tensor = np.ndarray

        self.modules = {
            "torch": torch, "torch._utils": utils, "torch.nn": nn,
            "torch.nn.parameter": param, "torch.serialization": serial,
        }

    # ---- pickle 钩子
    def persistent_load(self, pid):
        tag = pid[0]
        if tag == "storage":
            # v2: ('storage', storage_type, key, location, numel)
            stype, key, numel = pid[1], pid[2], pid[4]
            s = _Storage(self._zip, self._prefix, key, stype._dtype)
            s.numel = int(numel)
            return s
        raise pickle.UnpicklingError("torchless_ckpt: 不认识的 persistent_id %r" % (tag,))

    def _rebuild_tensor_v2(self, storage, storage_offset, size, stride,
                           requires_grad=False, backward_hooks=None, *a, **k):
        flat = storage.buf()
        off = int(storage_offset)
        if off:
            flat = flat[off:]
        shape = tuple(int(x) for x in size)
        if not shape:                                  # 0 维标量
            return flat[:1].reshape(())
        strides = tuple(int(s) * flat.itemsize for s in stride)
        return np.lib.stride_tricks.as_strided(flat, shape=shape, strides=strides,
                                              writeable=False)

    def _rebuild_parameter(self, data, requires_grad, backward_hooks=None, *a, **k):
        return data

    class _Parameter(np.ndarray):
        def __array_finalize__(self, obj):
            pass


class _Unpickler(pickle.Unpickler):
    def __init__(self, fh, fake):
        super().__init__(fh)
        self._fake = fake

    def find_class(self, module, name):
        m = self._fake.modules.get(module)
        if m is not None and hasattr(m, name):
            return getattr(m, name)
        if not module.startswith("torch"):
            return super().find_class(module, name)      # collections/OrderedDict 等
        # torch.dtype / 其它标量类型：给个惰性占位，不要炸整个反序列化
        return type(name, (), {"__module__": module})

    def persistent_load(self, pid):
        return self._fake.persistent_load(pid)


def load_ckpt(path):
    """返回 checkpoint 顶层对象（dict）。state_dict 里的张量是 numpy 数组。"""
    path = os.path.abspath(path)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        pkls = [n for n in names if n.endswith("/data.pkl") or n == "data.pkl"]
        if not pkls:
            raise ValueError("%s 不是 torch(>=1.6) zip 格式 checkpoint（没有 data.pkl）"
                             % path)
        pkl = pkls[0]
        prefix = pkl[:-len("/data.pkl")] if pkl.endswith("/data.pkl") else ""
        bo = prefix + "/byteorder" if prefix else "byteorder"
        if bo in names:
            order = zf.read(bo).decode().strip()
            if order != "little":
                raise ValueError("%s 的字节序是 %r，本工具只处理 little" % (path, order))
        fake = _FakeTorch(zf, prefix)
        with zf.open(pkl) as fh:
            obj = _Unpickler(fh, fake).load()
    return obj


def _describe(obj, depth=0, max_items=12):
    if isinstance(obj, dict):
        print("  " * depth + "dict(%d)" % len(obj))
        for i, (k, v) in enumerate(obj.items()):
            if i >= max_items:
                print("  " * depth + "  ...")
                break
            if isinstance(v, (dict, list)):
                print("  " * depth + "  %r:" % (k,))
                _describe(v, depth + 2, max_items)
            elif isinstance(v, np.ndarray):
                print("  " * depth + "  %r: ndarray%r %s" % (k, v.shape, v.dtype))
            else:
                print("  " * depth + "  %r: %r" % (k, v))
    elif isinstance(obj, list):
        print("  " * depth + "list(%d)" % len(obj))
        for i, v in enumerate(obj[:max_items]):
            if isinstance(v, (dict, list)):
                _describe(v, depth + 1, max_items)
            else:
                print("  " * depth + "  - %r" % (v,))


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    obj = load_ckpt(argv[1])
    print("顶层对象：")
    _describe(obj)
    sd = obj.get("state_dict", obj) if isinstance(obj, dict) else obj
    if isinstance(sd, dict):
        tot = sum(int(np.asarray(v).size) for v in sd.values()
                  if isinstance(v, np.ndarray))
        print("\nstate_dict: %d 个张量, %.3f M 元素" % (len(sd), tot / 1e6))
        bad = [k for k, v in sd.items() if not isinstance(v, np.ndarray)]
        if bad:
            print("非 ndarray 条目（%d）：%r" % (len(bad), bad[:5]))
        rng = [(k, float(np.nanmax(np.abs(np.asarray(v, np.float64)))),
                np.asarray(v).dtype) for k, v in list(sd.items())[:8]]
        for r in rng:
            print("  %-64s |max|=%.4g %s" % r)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
