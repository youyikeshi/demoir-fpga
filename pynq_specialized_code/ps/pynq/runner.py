#!/usr/bin/env python3
"""整网调度器：按 manifest 逐条执行 249 个卷积与全部层间算子。

引擎后端（依赖注入，同一份调度代码）：
  - RefEngine  —— 本机 numpy 假引擎（conv_ref_numpy，与 PL 引擎语义逐位一致），
                  用于离线联合集成测试（tools/test_runner_local.py）；
  - PYNQEngine —— 上板真引擎（pynq 连续内存池 + Conv2dEngine 寄存器驱动）。
上板时只替换后端，调度逻辑完全不变。

内存：所有中间张量放在一个"竞技场"里（arena）。PYNQ 后端用生命周期复用
规划一个连续池（640x640 图 84.8MB，v3 终检修复后；活跃峰值 46.3MB），
张量的设备地址 = 池基址 + 偏移。

量化标度：index.json 的 scales[张量名]（机器 B 的 export_network.py 写入）。
卷积块内部按导出端假定的输入标度计算；层间算子在汇合点（cat/add/sam_sum）
用 float 重定标到输出张量宣告的标度后再取整回 int8。
"""
import json
import os
import time

import numpy as np

import ops_numpy as ops


def load_pack(pack_dir):
    """读取权重包：返回 (index, blob_bytes)。"""
    with open(os.path.join(pack_dir, "index.json"), "r", encoding="utf-8") as f:
        index = json.load(f)
    with open(os.path.join(pack_dir, "weights.bin"), "rb") as f:
        blob = f.read()
    return index, blob


def _w_b(index, blob, key):
    e = index["layers"][key]
    w = np.frombuffer(blob, np.int8, e["w_len"], e["w_off"]).reshape(e["w_shape"])
    b = np.frombuffer(blob, np.int32, e["b_len"] // 4, e["b_off"])
    return w, b


# ---------------- 内存竞技场 ----------------

class NumpyArena:
    """本机后端：每个张量一个独立 numpy 数组。"""

    def __init__(self, manifest):
        self.t = {}
        for name, meta in manifest["buffers"].items():
            dt = np.int8 if meta["dtype"] == "int8" else np.float32
            self.t[name] = np.zeros((meta["c"], meta["h"], meta["w"]), dtype=dt)

    def view(self, name):
        return self.t[name]

    def addr(self, name):
        return None

    def flush(self):
        pass

    def invalidate(self):
        pass


def plan_pool(manifest, align=64):
    """按生命周期复用给所有张量分配池内偏移，返回 (池大小, 偏移表)。

    说明（v3 终检修复）：早期版本的空闲块管理不合并相邻块，导致碎片化
    （实测 640 图头部膨胀到 772 MB，而真实活跃峰值只有 ~46 MB）。现在
    释放时合并相邻空闲块、分配尺寸统一按 align 向上取整，头部降到
    与活跃峰值同量级（~50 MB），PYNQ 的 CMA 分配才可行。
    """
    ops_all = manifest["ops"]
    bufs = manifest["buffers"]
    last = {}
    for op in ops_all:
        for t in op["in"]:
            last[t] = op["id"]
        for t in op["out"]:
            last.setdefault(t, op["id"])
    offs = {}          # 每个张量名 -> (off, size)；释放后仍保留，供建视图
    alive = set()      # 当前仍占用槽位的张量（释放=移出 alive + 归还 free）
    free = []          # 按地址有序的 (off, size) 空闲块
    top = 0

    def _align(size):
        return (size + align - 1) // align * align

    def _coalesce():
        free.sort()
        merged = []
        for o, s in free:
            if merged and merged[-1][0] + merged[-1][1] == o:
                merged[-1] = (merged[-1][0], merged[-1][1] + s)
            else:
                merged.append((o, s))
        free[:] = merged

    def alloc(size):
        nonlocal top
        for i, (o, s) in enumerate(free):
            if s >= size:
                free.pop(i)
                if s - size >= align:
                    free.append((o + size, s - size))
                    free.sort()
                return o
        o = top
        top += size
        return o

    for op in ops_all:
        for t in op["out"]:
            if t not in offs:
                s = _align(bufs[t]["bytes"])
                offs[t] = (alloc(s), s)
            alive.add(t)
        for t in op["in"]:
            if last.get(t, -1) == op["id"] and t in alive:
                o, s = offs[t]
                alive.discard(t)
                free.append((o, s))
                _coalesce()
    # 兜底：从未出现在任何 op 产出里的张量（如网络输入 t0）。
    # 注意判定必须用 offs（全量记录）；若用 alive 会把"已释放张量"
    # 误判为未分配而重新分配——那是 762MB 膨胀的根因（v3 终检修复）。
    for t in bufs:
        if t not in offs:
            s = _align(bufs[t]["bytes"])
            offs[t] = (alloc(s), s)
    return top, offs


class PYNQArena:
    """上板后端：一个连续池 + 偏移视图（上板联调时启用，本机不导入 pynq）。"""

    def __init__(self, manifest):
        from pynq import allocate
        size, offs = plan_pool(manifest)
        self.size = size
        self.pool = allocate(shape=(size,), dtype=np.uint8)
        self.offs = {}
        self.t = {}
        for name, (o, s) in offs.items():
            meta = manifest["buffers"][name]
            dt = np.int8 if meta["dtype"] == "int8" else np.float32
            n = s // np.dtype(dt).itemsize
            self.t[name] = self.pool.array[o:o + s].view(dt).reshape(n)[
                :meta["c"] * meta["h"] * meta["w"]].reshape(meta["c"], meta["h"], meta["w"])
            self.offs[name] = o

    def view(self, name):
        return self.t[name]

    def addr(self, name):
        return int(self.pool.device_address) + self.offs[name]

    def flush(self):
        self.pool.flush()

    def invalidate(self):
        self.pool.invalidate()


# ---------------- 引擎后端 ----------------

class RefEngine:
    """本机假引擎：直接调用 conv_ref_numpy（与 PL 引擎语义逐位一致）。"""

    def __init__(self, index, blob, arena):
        self.index, self.blob, self.arena = index, blob, arena

    def conv(self, in_t, out_t, op):
        from ref_numpy import conv_ref_numpy
        w, b = _w_b(self.index, self.blob, op["weights_key"])
        y = conv_ref_numpy(self.arena.view(in_t), w, b,
                           op["k"], op["s"], op["d"], op["pad"],
                           self.index["layers"][op["weights_key"]]["shift"],
                           relu=bool(op["relu"]))
        self.arena.view(out_t)[...] = y


class PYNQEngine:
    """上板真引擎：连续内存池 + Conv2dEngine 驱动（上板联调时启用）。"""

    def __init__(self, overlay, index, blob, arena):
        import types
        from pynq import allocate
        from conv_engine import Conv2dEngine
        self.types = types
        self.index, self.arena = index, arena
        self.unit = Conv2dEngine(overlay)
        # 上板时由调用方设置：
        #   n_layers  —— 总共要跑多少个卷积（只用于进度）
        #   timeout_s —— 单次卷积的等待上限；None = 驱动按工作量估算
        self.timeout_s = None
        self.n_calls = 0
        self.n_layers = 0
        self.progress_every = 0      # >0：每 N 个卷积打印一行
        size = max(1, (len(blob) + 63) // 64 * 64)
        self.wpool = allocate(shape=(size,), dtype=np.uint8)
        self.wpool.array[:len(blob)] = np.frombuffer(blob, np.uint8)
        self.wpool.flush()

    def _ns(self, addr):
        return self.types.SimpleNamespace(device_address=int(addr),
                                          physical_address=int(addr))

    def conv(self, in_t, out_t, op):
        e = self.index["layers"][op["weights_key"]]
        waddr = int(self.wpool.device_address) + e["w_off"]
        baddr = int(self.wpool.device_address) + e["b_off"]
        self.n_calls += 1
        if self.progress_every and (self.n_calls % self.progress_every == 0):
            print("  [%d/%d] %-34s H=%d W=%d cin=%d cout=%d k=%d 预计 %.1fs"
                  % (self.n_calls, self.n_layers or self.n_calls, op["name"],
                     op["h"], op["w"], op["cin"], op["cout"], op["k"],
                     self.unit.estimate_seconds(op["h"], op["w"], op["cin"],
                                                op["cout"], op["k"], op["s"])),
                  flush=True)
        self.arena.flush()
        rc, _ = self.unit.run(self._ns(self.arena.addr(in_t)),
                              self._ns(waddr), self._ns(baddr),
                              self._ns(self.arena.addr(out_t)),
                              op["h"], op["w"], op["cin"], op["cout"],
                              op["k"], op["s"], op["d"], op["pad"],
                              e["shift"], relu=bool(op["relu"]),
                              timeout_s=self.timeout_s)
        self.arena.invalidate()
        if rc != 0:
            raise RuntimeError("PL 引擎返回错误码 %d（op=%s）" % (rc, op["name"]))


# ---------------- 调度器 ----------------

class Runner:
    def __init__(self, manifest_path, pack_dir, engine=None, arena=None):
        with open(manifest_path, "r", encoding="utf-8") as f:
            self.m = json.load(f)
        self.index, self.blob = load_pack(pack_dir)
        self.scales = self.index.get("scales", {})
        self.eca = self.index.get("eca", {})
        self.ops = [o for o in self.m["ops"] if o["needed"]]
        self.arena = arena if arena is not None else NumpyArena(self.m)
        self.engine = engine if engine is not None else RefEngine(self.index, self.blob, self.arena)
        self.n_conv = sum(1 for o in self.ops if o["op"] == "conv")
        self.record = None      # 调试用：设成 dict 后记录每个算子的输出快照

    # 标度辅助
    def _s(self, name, dflt=1.0):
        v = self.scales.get(name)
        return float(v) if v else dflt

    def _req(self, t, s_from, s_to):
        """把 int8 张量从 s_from 标度重定标到 s_to（相等时为恒等）。"""
        if s_from == s_to:
            return t
        return np.clip(np.rint(t.astype(np.float64) * (s_from / s_to)),
                       -128, 127).astype(np.int8)

    def run(self, img_int8=None):
        A = self.arena
        t_in = self.m["io"]["t_in"]
        if img_int8 is not None:
            A.view(t_in)[...] = img_int8
        t0 = time.time()
        for op in self.ops:
            k = op["op"]
            o0 = op["out"][0]
            if k == "conv":
                self.engine.conv(op["in"][0], o0, op)
            elif k == "pixel_unshuffle":
                A.view(o0)[...] = ops.pixel_unshuffle(A.view(op["in"][0]), op["factor"])
            elif k == "pixel_shuffle":
                A.view(o0)[...] = ops.pixel_shuffle(A.view(op["in"][0]), op["factor"])
            elif k == "interp":
                A.view(o0)[...] = ops.interp(A.view(op["in"][0]),
                                             op["scale_num"], op["scale_den"])
            elif k == "slice":
                A.view(o0)[...] = A.view(op["in"][0])[:op["channels"]]
            elif k == "cat":
                # 直接把每个输入分段拷进输出视图，避免 concatenate 的临时大数组
                # 再整体赋值一次。只有在输出与任一输入共享内存时才回退到旧路径。
                out = A.view(o0)
                s_out = self._s(o0)
                srcs = [A.view(n) for n in op["in"]]
                if any(np.shares_memory(out, src) for src in srcs):
                    parts = [self._req(src, self._s(n), s_out)
                             for src, n in zip(srcs, op["in"])]
                    out[...] = ops.cat(parts, op.get("channels"))
                else:
                    dst = 0
                    for src, n in zip(srcs, op["in"]):
                        take = min(src.shape[0], out.shape[0] - dst)
                        if take <= 0:
                            break
                        part = self._req(src, self._s(n), s_out)
                        out[dst:dst + take] = part[:take]
                        dst += take
                    if dst != out.shape[0]:
                        raise RuntimeError("cat 输出通道数不匹配: op=%s dst=%d out=%d"
                                           % (op["name"], dst, out.shape[0]))
            elif k == "add":
                s_out = self._s(o0)
                a = self._req(A.view(op["in"][0]), self._s(op["in"][0]), s_out)
                b = self._req(A.view(op["in"][1]), self._s(op["in"][1]), s_out)
                A.view(o0)[...] = ops.add(a, b)
            elif k == "eca":
                e = self.eca[op["weights_key"]]
                fin = A.view(op["in"][0]).astype(np.float32) * self._s(op["in"][0])
                A.view(o0)[...] = ops.eca_attn(fin, e["conv2"], e["conv1"])
            elif k == "sam_sum":
                y0, y2, y4, attn, x = (A.view(n) for n in op["in"])
                s_out = self._s(o0)
                w = attn.reshape(3, -1)
                yf = (w[0][:, None, None] * (y0.astype(np.float64) * self._s(op["in"][0]))
                      + w[1][:, None, None] * (y2.astype(np.float64) * self._s(op["in"][1]))
                      + w[2][:, None, None] * (y4.astype(np.float64) * self._s(op["in"][2])))
                xf = x.astype(np.float64) * self._s(op["in"][4])
                A.view(o0)[...] = np.clip(np.rint((xf + yf) / s_out),
                                          -128, 127).astype(np.int8)
            else:
                raise RuntimeError("未知算子: " + k)
            if self.record is not None:
                self.record[op["name"]] = A.view(o0).copy()
        dt = time.time() - t0
        out = A.view(self.m["io"]["out1"]).copy()
        return out, dt


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="整网 runner 本机冒烟（RefEngine）")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--pack", required=True)
    a = ap.parse_args()
    r = Runner(a.manifest, a.pack)
    h, w = r.m["input_hw"]
    rng = np.random.default_rng(0)
    img = rng.integers(0, 128, size=(3, h, w)).astype(np.int8)
    out, dt = r.run(img)
    print("ops=%d conv=%d 用时=%.1fs out=%s range=[%d,%d]"
          % (len(r.ops), r.n_conv, dt, out.shape, out.min(), out.max()))
