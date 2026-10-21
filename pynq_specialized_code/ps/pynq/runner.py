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


def _np(buf):
    """取 PYNQ 缓冲区的 numpy 视图。

    PYNQ 2.x 的 distribute/allocate 结果带 `.array`；PYNQ 3.x（本板 3.1.0）
    的 PynqBuffer 没有该属性，用 np.asarray 拿共享内存视图。
    """
    arr = getattr(buf, "array", None)
    return arr if arr is not None else np.asarray(buf)

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


# ---------------- 通道拼接零拷贝（cat alias）与内存竞技场 ----------------

def compute_cat_alias(manifest, align=64):
    """把 cat（通道拼接）合并成"一块缓冲区 + 各输入的通道切片视图"。

    网络里 224 个 cat 全是同 H/W 的纯通道拼接，输入又都是卷积/层间算子的
    输出。把它们规划进同一块缓冲区后，运行时不再"整块读一遍再写一遍"：

      · direct：生产者直接把输出写进它对应的通道切片（零拷贝）；
      · copy  ：该输入在本 cat 之后还要被别的算子消费（不能就地改标度），
                保留自己的缓冲区，cat 时把它重定标写进切片。

    拼接结果本身也只是组缓冲的前缀视图，所以链式 cat（cat 输出又当下一个
    cat 的输入）能整条折叠；运行时每个折叠的 cat 只剩"标度不同时的原地
    重标定"（256 项查找表，逐位等价于旧的 float64 公式）。

    硬条件（任一不满足就整条退回复制路径，正确性不受影响）：
      ① 输入通道数之和 == 输出通道数，且 H/W 相同（纯 channel concat）；
      ② 输入都是中间张量（不是网络输入）；
      ③ H*W 必须是 64 的整数倍（引擎地址对齐）；
      ④ 已在组内的张量，在本 cat 之后不能再被消费（原地重标会改坏别人）；
      ⑤ 合并/平移后的叶子切片必须从 0 起首尾相接盖满整块（否则整组回退）。

    返回 dict：groups / member / noop / groups_n / skipped
    """
    ops_all = manifest["ops"]
    bufs = manifest["buffers"]
    io = manifest.get("io", {}) or {}
    t_in = io.get("t_in")

    producers, consumers = {}, {}
    for op in ops_all:
        for t in op.get("out", []):
            producers.setdefault(t, op)
        for t in op.get("in", []):
            consumers.setdefault(t, []).append(op)

    member = {}     # 张量 -> (组名, 偏移)   只放 direct 成员
    groups = {}     # 组名 -> {"total","h","w","members","leaves","cats"}
    noop = {}
    skipped = []

    for op in ops_all:
        if op.get("op") != "cat" or not op.get("needed", True):
            continue
        out, ins = op["out"][0], op["in"]
        h, w = bufs[out]["h"], bufs[out]["w"]
        total = bufs[out]["c"]

        def _bad(why, _name=op["name"]):
            skipped.append((_name, why))

        if sum(bufs[t]["c"] for t in ins) != total:
            _bad("输入通道数之和 != 输出通道数"); continue
        if any(bufs[t]["h"] != h or bufs[t]["w"] != w for t in ins):
            _bad("输入 H/W 不一致"); continue
        if any(t not in producers for t in ins):
            _bad("输入不是中间张量"); continue
        if t_in is not None and t_in in ins:
            _bad("输入含网络输入"); continue
        if (h * w) % align:
            _bad("H*W 不是 %d 的整数倍，切片地址无法对齐" % align); continue

        # 每个输入在拼接结果里的目标偏移
        desired, cur = [], 0
        for t in ins:
            desired.append(cur)
            cur += bufs[t]["c"]

        # 已在组内的输入：同一组要求平移量一致；不同组的各自平移后合并
        touched, clash = {}, False
        for i, t in enumerate(ins):
            if t not in member:
                continue
            g0, off0 = member[t]
            d = desired[i] - off0
            if g0 in touched and touched[g0] != d:
                clash = True
                break
            touched[g0] = d
        if clash:
            _bad("组内输入相对位置不一致"); continue
        # 已在组内的张量不能再被"之后的消费者"读到（原地重标会污染）
        if any(t in member and any(c["id"] > op["id"] for c in consumers.get(t, []))
               for t in ins):
            _bad("组内张量在 cat 之后还有消费者"); continue

        # 合并被触及的组（各自平移）→ 新组以本 cat 输出命名
        grp = {"total": 0, "h": h, "w": w, "members": {}, "leaves": {}, "cats": []}
        for g0, d in touched.items():
            src = groups.pop(g0)
            for t, o in src["members"].items():
                grp["members"][t] = o + d
            for t, (o, c, m) in src["leaves"].items():
                grp["leaves"][t] = (o + d, c, m)
            grp["cats"].extend(src["cats"])
        grp["cats"].append(op["id"])

        # 决定每个输入是直写还是复制
        layout = []
        for i, t in enumerate(ins):
            c = bufs[t]["c"]
            if t in member:                 # 已在组里（上面已平移）
                mode = "direct"
            else:
                later = [c2 for c2 in consumers.get(t, []) if c2["id"] > op["id"]]
                mode = "copy" if later else "direct"
                if mode == "direct":
                    grp["members"][t] = desired[i]
                if t not in grp["leaves"]:
                    grp["leaves"][t] = (desired[i], c, mode)
            layout.append((t, desired[i], mode))

        grp["total"] = total
        grp["members"][out] = 0        # 拼接结果 = 整块缓冲区（前缀视图）
        groups[out] = grp
        for t, o in grp["members"].items():
            member[t] = (out, o)
        noop[op["id"]] = layout

    # 自检：每组的叶子切片必须从 0 起首尾相接盖满 total；不闭合就整组回退
    for gname, grp in list(groups.items()):
        cov = sorted((o, c) for o, c, _m in grp["leaves"].values())
        pos = 0
        ok = bool(cov) and cov[0][0] == 0
        if ok:
            for o, c in cov:
                if o != pos:
                    ok = False
                    break
                pos += c
        if ok and pos != grp["total"]:
            ok = False
        if not ok:
            for cid in grp["cats"]:
                noop.pop(cid, None)
            for t in [t for t, v in member.items() if v[0] == gname]:
                member.pop(t, None)
            groups.pop(gname)
            skipped.append((gname, "布局自检不闭合，已整组回退"))

    return {"groups": groups, "member": member, "noop": noop,
            "groups_n": len(noop), "skipped": skipped}


def _alias_unit_table(manifest, alias):
    """张量 → 分配单元（组或自身）；同时给出单元大小。"""
    bufs = manifest["buffers"]
    member = alias["member"] if alias else {}
    groups = alias["groups"] if alias else {}
    unit_of, unit_size = {}, {}
    for t, meta in bufs.items():
        if t in member:
            u = ("G", member[t][0])
            grp = groups[member[t][0]]
            unit_size[u] = grp["total"] * grp["h"] * grp["w"]
        else:
            u = ("T", t)
            unit_size[u] = meta["bytes"]
        unit_of[t] = u
    return unit_of, unit_size


class NumpyArena:
    """本机后端：每个张量一个独立 numpy 数组；cat 组成员共享同一块缓冲区。"""

    def __init__(self, manifest, alias=None):
        if alias is None:
            alias = compute_cat_alias(manifest)
        self.alias = alias
        member = alias["member"]
        groups = alias["groups"]
        self.t = {}
        for gname, grp in groups.items():
            dt = np.int8
            self.t[gname] = np.zeros((grp["total"], grp["h"], grp["w"]), dtype=dt)
            for t, off in grp["members"].items():
                meta = manifest["buffers"][t]
                self.t[t] = self.t[gname][off:off + meta["c"]]
        for name, meta in manifest["buffers"].items():
            if name in member:
                continue
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


def plan_pool(manifest, align=64, alias=None):
    """按生命周期复用给所有张量分配池内偏移，返回 (池大小, 偏移表)。

    说明（v3 终检修复）：早期版本的空闲块管理不合并相邻块，导致碎片化
    （实测 640 图头部膨胀到 772 MB，而真实活跃峰值只有 ~46 MB）。现在
    释放时合并相邻空闲块、分配尺寸统一按 align 向上取整，头部降到
    与活跃峰值同量级（~50 MB），PYNQ 的 CMA 分配才可行。

    2026-10-21：分配单位从"张量"升级为"单元"——cat 组的成员共享同一块
    缓冲区（见 compute_cat_alias），组内张量的地址 = 组基址 + 通道偏移×HW。
    """
    ops_all = manifest["ops"]
    bufs = manifest["buffers"]
    if alias is None:
        alias = compute_cat_alias(manifest, align)
    unit_of, unit_size = _alias_unit_table(manifest, alias)
    member = alias["member"]
    groups = alias["groups"]

    last = {}
    for op in ops_all:
        for t in op["in"]:
            last[t] = op["id"]
        for t in op["out"]:
            last.setdefault(t, op["id"])
    # 单元的存活区间 = 成员张量存活区间的并集
    last_unit = {}
    for t in bufs:
        u = unit_of[t]
        last_unit[u] = max(last_unit.get(u, -1), last.get(t, -1))

    offs_u = {}        # 单元 -> (off, size)
    alive = set()      # 当前仍占用槽位的单元
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
            u = unit_of[t]
            if u not in offs_u:
                s = _align(unit_size[u])
                offs_u[u] = (alloc(s), s)
            alive.add(u)
        for t in op["in"]:
            u = unit_of[t]
            if (last.get(t, -1) == op["id"] and u in alive
                    and last_unit.get(u, -1) == op["id"]):
                o, s = offs_u[u]
                alive.discard(u)
                free.append((o, s))
                _coalesce()
    # 兜底：从未出现在任何 op 产出里的张量（如网络输入 t0）。
    # 注意判定必须用 offs_u（全量记录）；若用 alive 会把"已释放单元"
    # 误判为未分配而重新分配——那是 762MB 膨胀的根因（v3 终检修复）。
    for t in bufs:
        u = unit_of[t]
        if u not in offs_u:
            s = _align(unit_size[u])
            offs_u[u] = (alloc(s), s)

    offs = {}
    for t, meta in bufs.items():
        o, s = offs_u[unit_of[t]]
        if t in member:
            g, off_c = member[t]
            grp = groups[g]
            offs[t] = (o + off_c * grp["h"] * grp["w"], meta["bytes"])
        else:
            offs[t] = (o, s)
    return top, offs


class PYNQArena:
    """上板后端：一个连续池 + 偏移视图（上板联调时启用，本机不导入 pynq）。

    2026-10-21：cat 组成员共享同一块池内区域（成员 = 组的通道切片视图）。
    """

    def __init__(self, manifest, alias=None):
        from pynq import allocate
        if alias is None:
            alias = compute_cat_alias(manifest)
        self.alias = alias
        member = alias["member"]
        groups = alias["groups"]
        size, offs = plan_pool(manifest, alias=alias)
        self.size = size
        self.pool = allocate(shape=(size,), dtype=np.uint8)
        self.offs = {}
        self.t = {}
        for name, (o, s) in offs.items():
            meta = manifest["buffers"][name]
            dt = np.int8 if meta["dtype"] == "int8" else np.float32
            n = s // np.dtype(dt).itemsize
            self.t[name] = _np(self.pool)[o:o + s].view(dt).reshape(n)[
                :meta["c"] * meta["h"] * meta["w"]].reshape(meta["c"], meta["h"], meta["w"])
            self.offs[name] = o
        # 组内成员必须是组的切片视图（共享同一段内存，不是各自独立缓冲）
        for gname, grp in groups.items():
            base = self.t[gname]
            for t, off_c in grp["members"].items():
                c = manifest["buffers"][t]["c"]
                view = base[off_c:off_c + c]
                assert np.shares_memory(view, base)
                self.t[t] = view

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
        _np(self.wpool)[:len(blob)] = np.frombuffer(blob, np.uint8)
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
        self.alias = compute_cat_alias(self.m)
        if arena is not None:
            self.arena = arena
        else:
            self.arena = NumpyArena(self.m, alias=self.alias)
        self.engine = engine if engine is not None else RefEngine(self.index, self.blob, self.arena)
        self.n_conv = sum(1 for o in self.ops if o["op"] == "conv")
        self.record = None      # 调试用：设成 dict 后记录每个算子的输出快照
        self.profile = None     # 计时用：设成 dict 后按算子类型累计 [秒, 次数]
        self.profile_ops = None # 计时用：设成 list 后逐算子记录 (id, name, op, 秒)
        self.hashlog = None     # 调试用：设成 list 后逐算子记录 (name, op, shape, sha1)
        self._luts = {}         # 标度重定标查找表缓存：键 = (s_from, s_to)

    # 标度辅助
    def _s(self, name, dflt=1.0):
        v = self.scales.get(name)
        return float(v) if v else dflt

    def _lut(self, s_from, s_to):
        """标度重定标用的 256 项查找表（int8 全值域穷举，逐位等价于浮点公式）。

        原来每次重定标都要 t→float64→乘→rint→clip→int8，在 640 图上
        单张要过 ~290 MB，临时数组是 int8 的 8 倍。因为映射只依赖 int8 的
        256 个可能取值，直接把这 256 个结果算出来做成表即可。
        表项用与旧实现完全相同的表达式（float64 乘法 + rint + clip）算出，
        所以结果逐位一致；且从此不再有跨平台浮点差异。
        返回 256 字节的 bytes（供 `bytes.translate` 用，见 _req）。
        """
        key = (s_from, s_to)
        lut = self._luts.get(key)
        if lut is None:
            vals = np.arange(256, dtype=np.int16).astype(np.int8).astype(np.float64)
            tab = np.clip(np.rint(vals * (s_from / s_to)), -128, 127).astype(np.int8)
            lut = tab.tobytes()          # 按 uint8 位模式排列，正好是 translate 表
            self._luts[key] = lut
        return lut

    def _lut_np(self, s_from, s_to):
        """同一个表的 numpy 视图（个别地方需要数组形式）。"""
        return np.frombuffer(self._lut(s_from, s_to), dtype=np.int8)

    def _req(self, t, s_from, s_to):
        """把 int8 张量从 s_from 标度重定标到 s_to（相等时为恒等）。

        2026-10-21：由"numpy 花式索引查表"改成 `bytes.translate`——后者是
        CPython 的 C 实现（字节映射循环），在 Cortex-A9 上比 numpy 的 gather
        快数倍（板端 cat 的重定标原本 27 s）。
        """
        if s_from == s_to:
            return t
        tbl = self._lut(s_from, s_to)
        return np.frombuffer(np.ascontiguousarray(t).tobytes().translate(tbl),
                             dtype=np.int8).reshape(t.shape)

    def run(self, img_int8=None):
        A = self.arena
        t_in = self.m["io"]["t_in"]
        if img_int8 is not None:
            A.view(t_in)[...] = img_int8
        t0 = time.time()
        for op in self.ops:
            k = op["op"]
            o0 = op["out"][0]
            t_op = time.time() if self.profile is not None else 0.0
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
                lay = self.alias["noop"].get(op["id"])
                if lay is not None:
                    # 零拷贝：direct 输入已由生产者按通道偏移写在同一块缓冲区里
                    # （见 compute_cat_alias），这里只剩"标度不一致时的原地重标"；
                    # copy 输入（之后还要被别的算子消费）才真正搬一次。
                    # 重标定用 256 项查找表，逐位等价于原来的 float64 公式。
                    s_out = self._s(o0)
                    out = A.view(o0)
                    for n, off, mode in lay:
                        sn = self._s(n)
                        if mode == "copy":
                            c = A.view(n).shape[0]
                            out[off:off + c] = self._req(A.view(n), sn, s_out)
                        elif sn != s_out:
                            v = A.view(n)
                            v[...] = np.frombuffer(v.tobytes().translate(
                                self._lut(sn, s_out)), dtype=np.int8).reshape(v.shape)
                # 直接把每个输入分段拷进输出视图，避免 concatenate 的临时大数组
                # 再整体赋值一次。只有在输出与任一输入共享内存时才回退到旧路径。
                elif any(np.shares_memory(A.view(o0), A.view(n)) for n in op["in"]):
                    out = A.view(o0)
                    s_out = self._s(o0)
                    parts = [self._req(A.view(n), self._s(n), s_out) for n in op["in"]]
                    out[...] = ops.cat(parts, op.get("channels"))
                else:
                    out = A.view(o0)
                    s_out = self._s(o0)
                    dst = 0
                    for n in op["in"]:
                        src = A.view(n)
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
                # 2026-10-21：按通道分块算空间均值。原来把整块（144×320×320）
                # astype 成 float32（59 MB 临时）再求均值；分块后临时数组只有
                # 32 通道那么大，且逐通道均值与分块无关 → 结果逐位不变。
                e = self.eca[op["weights_key"]]
                v = A.view(op["in"][0])
                sc = self._s(op["in"][0])
                nc = v.shape[0]
                mean_c = np.empty(nc, dtype=np.float32)
                for c0 in range(0, nc, 32):
                    sl = slice(c0, min(c0 + 32, nc))
                    fin = v[sl].astype(np.float32)
                    fin *= sc
                    mean_c[sl] = fin.mean(axis=(1, 2), dtype=np.float32)
                A.view(o0)[...] = ops.eca_attn_from_mean(mean_c, e["conv2"], e["conv1"])
            elif k == "sam_sum":
                # 按通道分块算（原来是整块 float64，单张 5 个张量 ≈ 200 MB 临时数组）。
                # 分块后逐元素运算与分块前完全一致，结果逐位不变。
                y0, y2, y4, attn, x = (A.view(n) for n in op["in"])
                s_out = self._s(o0)
                s0, s2, s4 = (self._s(op["in"][i]) for i in (0, 1, 2))
                sx = self._s(op["in"][4])
                w = attn.reshape(3, -1)
                out = A.view(o0)
                for c0 in range(0, out.shape[0], 16):
                    sl = slice(c0, min(c0 + 16, out.shape[0]))
                    yf = (w[0][sl, None, None] * (y0[sl].astype(np.float64) * s0)
                          + w[1][sl, None, None] * (y2[sl].astype(np.float64) * s2)
                          + w[2][sl, None, None] * (y4[sl].astype(np.float64) * s4))
                    xf = x[sl].astype(np.float64) * sx
                    out[sl] = np.clip(np.rint((xf + yf) / s_out),
                                      -128, 127).astype(np.int8)
            else:
                raise RuntimeError("未知算子: " + k)
            if self.record is not None:
                self.record[op["name"]] = A.view(o0).copy()
            if self.profile_ops is not None:
                self.profile_ops.append((op["id"], op["name"], k,
                                         (time.time() - t_op) if self.profile is not None else 0.0))
            if self.profile is not None:
                p = self.profile.setdefault(k, [0.0, 0])
                p[0] += time.time() - t_op
                p[1] += 1
            if self.hashlog is not None:
                import hashlib
                arr = np.ascontiguousarray(A.view(o0))
                self.hashlog.append((op["name"], k, "%d,%d,%d" % arr.shape,
                                     hashlib.sha1(arr.tobytes()).hexdigest()[:16]))
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
