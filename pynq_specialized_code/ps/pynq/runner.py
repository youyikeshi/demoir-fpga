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
try:
    import pp_ops                      # 板上是扁平目录
    import lut_util
    import c8_layout                   # C8 布局判定/分配（2026-10-23）
except ImportError:                    # 仓库里作为 pynq 包时
    from . import pp_ops
    from . import lut_util
    from . import c8_layout


def _env_flag(name):
    """环境变量开关：未设置 / 空 / 0 / false / off 都算关。"""
    v = os.environ.get(name, "")
    return str(v).strip().lower() not in ("", "0", "false", "off", "no")


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

def compute_cat_alias(manifest, align=64, is_c8=None):
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

    2026-10-23（C8）：多一条硬条件——C8 模式下组的**所有成员**都必须是 C8
    （每个段的通道数都是 8 的倍数），否则整组回退（回退后各张量各用各的布局，
    cat 走 ops_c8 的通用路径）。实测 224 个 cat / 454 个段**全部**对齐 8，
    所以 C8 下这条永远不触发。

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
        if is_c8 is not None and any(bool(is_c8.get(t)) or bool(is_c8.get(out))
                                     for t in ins):
            # C8 组的 numpy 视图是 (ceil(total/8),H,W,8)，成员必须是**整块切片**
            # （offset 与通道数都对齐 8），池里的 off_c*H*W 字节偏移公式才成立。
            # 2026-10-23 第二轮起 C8 覆盖**全部** int8 张量（含 3ch/12ch 补零
            # 张量），所以这里不能再拿"两边布局标志相同"当判据（那恒成立），
            # 必须判"段通道数真的是 8 的倍数"。实测 224 个 cat / 454 个段全部
            # 对齐 ⇒ 这条永远不触发；补零张量本来也不出现在 cat 里。
            if not all(bool(is_c8.get(t)) == bool(is_c8.get(out)) for t in ins):
                _bad("C8 布局不一致：组内有成员的布局与输出不同"); continue
            if any(int(bufs[t]["c"]) % 8 for t in ins) or int(total) % 8:
                _bad("C8 组的段通道数必须都对齐 8（补零张量不能做块级切片）")
                continue

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


def _alias_unit_table(manifest, alias, layout=None):
    """张量 → 分配单元（组或自身）；同时给出单元**字节数**。

    2026-10-23（C8）：字节数按 `c8_layout.nbytes` 算（C8 = ceil(C/8)*8*H*W）。
    C%8==0 时两者相等，所以 C8 下池大小不变；`layout=None`（默认）等价于
    全部 CHW，即整合前的算法。
    """
    bufs = manifest["buffers"]
    member = alias["member"] if alias else {}
    groups = alias["groups"] if alias else {}
    is_c8 = layout or {}
    unit_of, unit_size = {}, {}
    for t, meta in bufs.items():
        if t in member:
            u = ("G", member[t][0])
            grp = groups[member[t][0]]
            unit_size[u] = (c8_layout.nbytes(grp["total"], grp["h"], grp["w"],
                                             is_c8.get(member[t][0], False))
                            * c8_layout.dtype_itemsize(meta["dtype"]))
        else:
            u = ("T", t)
            unit_size[u] = c8_layout.bytes_of(meta, is_c8.get(t, False))
        unit_of[t] = u
    return unit_of, unit_size


class NumpyArena:
    """本机后端：每个张量一个独立 numpy 数组；cat 组成员共享同一块缓冲区。

    2026-10-23（C8）：`layout` 给定时按 C8 分配——张量视图 (C/8,H,W,8)、
    cat 组视图 (total/8,H,W,8)，成员 = 组缓冲的**通道块切片**（零拷贝）。
    地址公式与 CHW 完全一致（off_c*H*W == (off_c/8)*H*W*8），所以池偏移
    表一行都不用改，只是 numpy 视图形状变了。
    """

    def __init__(self, manifest, alias=None, layout=None):
        self.layout = (c8_layout.table(manifest) if layout is None else layout)
        if alias is None:
            alias = compute_cat_alias(manifest, is_c8=self.layout)
        self.alias = alias
        member = alias["member"]
        groups = alias["groups"]
        self.t = {}
        for gname, grp in groups.items():
            is8 = bool(self.layout.get(gname, False))
            shape = c8_layout.view_shape(grp["total"], grp["h"], grp["w"], is8)
            # np.zeros：C8 补零通道（若存在）天生就是 0，引擎读它们不会读到脏值。
            self.t[gname] = np.zeros(shape, dtype=np.int8)
            for t, off in grp["members"].items():
                meta = manifest["buffers"][t]
                self.t[t] = c8_layout.ch_slice(self.t[gname], is8, off, meta["c"])
        for name, meta in manifest["buffers"].items():
            if name in member:
                continue
            is8 = bool(self.layout.get(name, False))
            dt = np.int8 if meta["dtype"] == "int8" else np.float32
            self.t[name] = np.zeros(
                c8_layout.view_shape(meta["c"], meta["h"], meta["w"], is8), dtype=dt)

    def view(self, name):
        return self.t[name]

    def is_c8(self, name):
        """该张量是否 C8 布局（引擎/算子按它决定读法）。"""
        return bool(self.layout.get(name, False))

    def addr(self, name):
        return None

    def flush(self):
        pass

    def invalidate(self):
        pass


def plan_pool(manifest, align=64, alias=None, layout=None):
    """按生命周期复用给所有张量分配池内偏移，返回 (池大小, 偏移表)。

    说明（v3 终检修复）：早期版本的空闲块管理不合并相邻块，导致碎片化
    （实测 640 图头部膨胀到 772 MB，而真实活跃峰值只有 ~46 MB）。现在
    释放时合并相邻空闲块、分配尺寸统一按 align 向上取整，头部降到
    与活跃峰值同量级（~50 MB），PYNQ 的 CMA 分配才可行。

    2026-10-21：分配单位从"张量"升级为"单元"——cat 组的成员共享同一块
    缓冲区（见 compute_cat_alias），组内张量的地址 = 组基址 + 通道偏移×HW。

    2026-10-23（C8）：单元大小改由 `c8_layout.nbytes` 给出（C8 时
    ceil(C/8)*8*H*W）。因为布局判据是 C%8==0，C8 的字节数与 CHW **相同**，
    所以池大小、偏移表、以及"组内 off_c*H*W"的字节偏移公式都完全不变。
    `layout=None` 时按环境变量 C8_LAYOUT 建表（默认关 ⇒ 与整合前逐位一致）。
    """
    ops_all = manifest["ops"]
    bufs = manifest["buffers"]
    if layout is None:
        layout = c8_layout.table(manifest)
    if alias is None:
        alias = compute_cat_alias(manifest, align, is_c8=layout)
    unit_of, unit_size = _alias_unit_table(manifest, alias, layout)
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
        nb = c8_layout.bytes_of(meta, layout.get(t, False))
        if t in member:
            g, off_c = member[t]
            grp = groups[g]
            # 字节偏移公式在 C8 下不用改：
            #   off_c*H*W == (off_c/8)*H*W*8（off_c 必为 8 的倍数）
            offs[t] = (o + off_c * grp["h"] * grp["w"], nb)
        else:
            offs[t] = (o, s)
    return top, offs


class PYNQArena:
    """上板后端：一个连续池 + 偏移视图（上板联调时启用，本机不导入 pynq）。

    2026-10-21：cat 组成员共享同一块池内区域（成员 = 组的通道切片视图）。
    2026-10-23（C8）：C8 张量的视图是 (C/8,H,W,8)（元素个数不变，只是 reshape）；
    组内成员 = 组视图的**通道块切片**。C8 模式下整池先清零——引擎会读通道补零区
    （`ceil(cin/8)*8` 的最后一块），那部分必须是 0。
    """

    def __init__(self, manifest, alias=None, layout=None):
        from pynq import allocate
        self.layout = (c8_layout.table(manifest) if layout is None else layout)
        if alias is None:
            alias = compute_cat_alias(manifest, is_c8=self.layout)
        self.alias = alias
        member = alias["member"]
        groups = alias["groups"]
        size, offs = plan_pool(manifest, alias=alias, layout=self.layout)
        self.size = size
        self.pool = allocate(shape=(size,), dtype=np.uint8)
        if any(self.layout.values()):
            # 补零通道未清零 → 引擎读到脏数据（规范 §6 第一条风险）。
            # 整池清零只在 C8 模式下做一次，~50 MB，板端可忽略。
            _np(self.pool)[:] = 0
            self.pool.flush()
        self.offs = {}
        self.t = {}
        for name, (o, s) in offs.items():
            meta = manifest["buffers"][name]
            dt = np.int8 if meta["dtype"] == "int8" else np.float32
            is8 = bool(self.layout.get(name, False))
            n = s // np.dtype(dt).itemsize
            flat = _np(self.pool)[o:o + s].view(dt).reshape(n)
            # C8 视图的元素数 = ceil(C/8)*8*H*W（**含补零通道**），不是 c*h*w：
            # 3ch/12ch 补零后视图比 c*h*w 大，按 c*h*w 切片会 reshape 失败。
            nb = c8_layout.bytes_of(meta, is8) // np.dtype(dt).itemsize
            self.t[name] = flat[:nb].reshape(
                c8_layout.view_shape(meta["c"], meta["h"], meta["w"], is8))
            self.offs[name] = o
        # 组内成员必须是组的切片视图（共享同一段内存，不是各自独立缓冲）
        for gname, grp in groups.items():
            base = self.t[gname]
            is8 = bool(self.layout.get(gname, False))
            for t, off_c in grp["members"].items():
                c = manifest["buffers"][t]["c"]
                view = c8_layout.ch_slice(base, is8, off_c, c)
                assert np.shares_memory(view, base)
                self.t[t] = view

    def view(self, name):
        return self.t[name]

    def is_c8(self, name):
        """该张量是否 C8 布局（引擎据此决定 relu_en 的 bit2/bit3）。"""
        return bool(self.layout.get(name, False))

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

    # 支持卷积融合出口（out_lut 写回查表 / chsum 通道和，本机按 RTL 语义模拟）
    supports_fuse = True
    fuse_out_ok = True          # 本机模拟：两个出口都"存在"
    fuse_sum_ok = True

    def alloc_chsum(self, cout):
        """分配一块 chsum 累加缓冲（每输出通道一个 int32）。

        返回 (buf, view)：buf 原样传给 conv(chsum=...)，view 用来读回结果。
        本机后端里两者是同一个 numpy 数组；真引擎里 buf 是设备缓冲、view 是
        它的共享内存视图（PL 写完后 invalidate 再读）。
        """
        arr = np.zeros(int(cout), dtype=np.int32)
        return arr, arr

    def conv(self, in_t, out_t, op, out_lut=None, chsum=None):
        from ref_numpy import conv_ref_numpy
        w, b = _w_b(self.index, self.blob, op["weights_key"])
        # 布局：本机假引擎必须自己模拟 PL 的"直读/直写"——引擎内部是按 CHW
        # 语义算的参考实现，所以 C8 张量在出入口各做一次布局转换（真机上这两步
        # 由 PL 直接按 C8 地址读写完成，PS 不发生转换，这里只是仿真开销）。
        in8 = c8_layout.arena_is_c8(self.arena, in_t)
        out8 = c8_layout.arena_is_c8(self.arena, out_t)
        # C8 视图的通道数可能是补零后的（cin=12 -> 16 通道位）：只取前 cin 个，
        # 补零通道既没有对应权重、也不该进乘加（与 RTL 的 ic < cin 一致）。
        src = c8_layout.to_chw(self.arena.view(in_t), in8, op["cin"])
        y = conv_ref_numpy(src, w, b,
                           op["k"], op["s"], op["d"], op["pad"],
                           self.index["layers"][op["weights_key"]]["shift"],
                           relu=bool(op["relu"]))
        if out_lut is not None:
            # 与 RTL 逐位等价：v8 = sat8(acc) 之后查表
            y = lut_util.apply_out_lut(y, out_lut)
        if chsum is not None:
            # 与 RTL 逐位等价：chsum[oc] += Σ_{y,x} v8（**累加**、不覆盖，调用方
            # 已清零）。RTL 累加的是写回的 int8 值，所以必须在 out_lut 之后取 y：
            # 同时开两个出口时，"落盘值"和"累加值"同源。
            y64 = y.reshape(y.shape[0], -1).sum(axis=1, dtype=np.int64)
            chsum[...] += y64.astype(np.int32)
        self.arena.view(out_t)[...] = c8_layout.from_chw(y, out8)


class PYNQEngine:
    """上板真引擎：连续内存池 + Conv2dEngine 驱动（上板联调时启用）。

    融合出口（由 bit 的构建宏决定，两个开关**独立**）：
      · out_lut —— FUSE_OUT=1；256 项 int8 表，按表内容缓存一块 256B 连续内存；
      · chsum   —— FUSE_SUM=1；每输出通道一个 int32，PL 写回时累加（不覆盖）。
    驱动构造时注入寄存器偏移（见 conv_engine.Conv2dEngine）；只有偏移非 None
    的出口才会被写寄存器，也才会被 Runner 纳入规划。
    """

    # 支持卷积融合出口，前提是 bit 里真有对应端口且驱动构造时注入了偏移
    supports_fuse = True

    def __init__(self, overlay, index, blob, arena, fuse_offsets=None):
        import types
        from pynq import allocate
        from conv_engine import Conv2dEngine
        self.types = types
        self.index, self.arena = index, arena
        self.unit = Conv2dEngine(overlay, fuse_offsets=fuse_offsets)
        # 哪些融合出口真的可用（Runner 据此决定要不要规划对应链路）
        self.fuse_out_ok = bool(getattr(self.unit, "fuse_out_ok", False))
        self.fuse_sum_ok = bool(getattr(self.unit, "fuse_sum_ok", False))
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
        # out_lut 的小块连续内存（按表内容缓存，避免每层重分配）
        self._allocate = allocate
        self._lut_bufs = {}

    def alloc_chsum(self, cout):
        """分配一块每通道 int32 的累加缓冲（清零后返回 (buf, view)）。

        **必须每次清零**：RTL 是 `chsum[oc] += v8`（累加），硬件不会清零。
        """
        cout = int(cout)
        if cout <= 0:
            raise ValueError("chsum 通道数必须为正（cout=%d）" % cout)
        buf = self._allocate(shape=(cout,), dtype=np.int32)
        view = _np(buf).reshape(cout)
        view[...] = 0
        buf.flush()              # 清零结果对 PL 可见（DDR 走 cache）
        return buf, view

    def _lut_buf(self, lut):
        """把 256 项 int8 表放进一块连续内存（同表只分配一次）。"""
        key = np.ascontiguousarray(np.asarray(lut, dtype=np.int8)).tobytes()
        buf = self._lut_bufs.get(key)
        if buf is None:
            buf = self._allocate(shape=(256,), dtype=np.int8)
            _np(buf)[:] = np.frombuffer(key, dtype=np.int8)
            buf.flush()
            self._lut_bufs[key] = buf
        return buf

    def _ns(self, addr):
        return self.types.SimpleNamespace(device_address=int(addr),
                                          physical_address=int(addr))

    def conv(self, in_t, out_t, op, out_lut=None, chsum=None):
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
        lut_buf = self._lut_buf(out_lut) if out_lut is not None else None
        if chsum is not None:
            # 调用方（Runner）已清零并 flush；这里再 flush 一次是廉价的保险：
            # RTL 是 `+=`，若 DDR 里还是旧值，累加结果就是错的。
            f = getattr(chsum, "flush", None)
            if f is not None:
                f()
        # 布局位（2026-10-23）：relu_en 的 bit2/bit3 = 输入/输出是 C8。
        # 逐张量判定（不能按层硬编码）：encoder.conv_first 是 12ch(CHW) 进 /
        # 48ch(C8) 出，decoder_1.out_conv 是 C8 进 / 12ch(CHW) 出。
        # C8_LAYOUT=0 时两个都是 False ⇒ 只写 bit0，与整合前逐位一致。
        c8_in = c8_layout.arena_is_c8(self.arena, in_t)
        c8_out = c8_layout.arena_is_c8(self.arena, out_t)
        rc, _ = self.unit.run(self._ns(self.arena.addr(in_t)),
                              self._ns(waddr), self._ns(baddr),
                              self._ns(self.arena.addr(out_t)),
                              op["h"], op["w"], op["cin"], op["cout"],
                              op["k"], op["s"], op["d"], op["pad"],
                              e["shift"], relu=bool(op["relu"]),
                              c8_in=c8_in, c8_out=c8_out,
                              out_lut=lut_buf, chsum=chsum,
                              timeout_s=self.timeout_s)
        self.arena.invalidate()
        if chsum is not None:
            # PL 通过 AXI 写进 DDR 的累加和，CPU 读之前必须 invalidate
            inv = getattr(chsum, "invalidate", None)
            if inv is not None:
                inv()
        if rc != 0:
            raise RuntimeError("PL 引擎返回错误码 %d（op=%s）" % (rc, op["name"]))


# ---------------- 调度器 ----------------

class Runner:
    def __init__(self, manifest_path, pack_dir, engine=None, arena=None,
                 fuse_conv=None):
        with open(manifest_path, "r", encoding="utf-8") as f:
            self.m = json.load(f)
        self.index, self.blob = load_pack(pack_dir)
        self.scales = self.index.get("scales", {})
        self.eca = self.index.get("eca", {})
        # v2 层间算子加速器（PPEngine）。None = 全部走 numpy（本机/无硬件时）。
        # pp_ops_en: 允许走加速器的算子名集合（默认空 = 全部走 numpy，保逐位一致）
        self.pp = None
        self.pp_ops_en = set()
        # 卷积融合出口（RTL FUSE_OUT=1 才有）：默认关，保证交付 bit 行为逐位不变。
        # fuse_conv=None 时读环境变量 PP_FUSE_CONV（1/true/on 才开）。
        self.fuse_conv = (_env_flag("PP_FUSE_CONV") if fuse_conv is None
                          else bool(fuse_conv))
        # ---- C8 交错布局总开关（2026-10-23）----
        # 环境变量 C8_LAYOUT，**默认关**。关时缓冲按 CHW 分配、引擎 relu_en 只写
        # bit0、层间算子全走 ops_numpy ⇒ 与整合前逐位一致（验收项）。
        # 开时：int8 且 C%8==0 的张量按 C8 交错布局分配/读写（规范 §1），
        # 层间算子走 ps/c8/ops_c8.py。
        self.c8 = c8_layout.enabled()
        self.ops = [o for o in self.m["ops"] if o["needed"]]
        # 布局表：编译期算一次（name -> bool），运行期只查表（规范 §4）。
        layout = c8_layout.table(self.m, on=self.c8)
        alay = getattr(arena, "layout", None) if arena is not None else None
        if arena is not None and alay is None:
            if self.c8:
                raise RuntimeError(
                    "C8_LAYOUT=1 但传入的 arena 没有布局表（它是按 CHW 分配的）："
                    "用 NumpyArena/PYNQArena(manifest) 构造，或显式传 layout=")
            self.layout = layout
        elif alay is not None:
            if dict(alay) != dict(layout):
                diff = [t for t in layout if bool(alay.get(t)) != bool(layout[t])]
                raise RuntimeError(
                    "C8_LAYOUT 与 arena 的布局表不一致（%d 个张量不同，例：%s）"
                    "：arena 与环境变量必须同源" % (len(diff), diff[:3]))
            self.layout = alay
        else:
            self.layout = layout
        self.is_c8 = self.layout     # 名字 -> bool（运行期查表用）
        self.alias = compute_cat_alias(self.m, is_c8=self.layout)
        if arena is None:
            self.arena = NumpyArena(self.m, alias=self.alias, layout=self.layout)
        else:
            self.arena = arena
        self.engine = engine if engine is not None else RefEngine(self.index, self.blob, self.arena)
        self.n_conv = sum(1 for o in self.ops if o["op"] == "conv")
        self.record = None      # 调试用：设成 dict 后记录每个算子的输出快照
        self.profile = None     # 计时用：设成 dict 后按算子类型累计 [秒, 次数]
        self.profile_ops = None # 计时用：设成 list 后逐算子记录 (id, name, op, 秒)
        self.hashlog = None     # 调试用：设成 list 后逐算子记录 (name, op, shape, sha1)
        self._luts = {}         # 标度重定标查找表缓存：键 = (s_from, s_to)
        self._fuse_skips = 0    # 调试/验证用：cat 分支实际跳过重定标的段数
        self._fuse_sums = 0     # 调试/验证用：ECA 分支实际用 chsum 代替扫描的次数
        self._fuse_plan_n = 0   # 调试/验证用：conv→cat 配对数
        self._fuse_sum_plan_n = 0   # 调试/验证用：conv→eca 配对数
        self._chsum = {}        # 待消费的通道和缓冲：张量名 -> (buf, view)
        # 板端 bring-up 用：True 时在 ECA 里拿特征图重算通道和并与 chsum 对拍
        # （会让该段退化回扫描，只用于验证接线/清零语义，不用于提速）
        self.verify_chsum = False
        self._chsum_verify_bad = 0
        # 板端 bring-up 用：给指定 conv（op id 或名字）挂一次 chsum，跑完立刻拿
        # 特征图重算通道和来对拍。**真实权重/真实形状**下验证 bit 的 chsum 端口
        # （寄存器偏移、int32 累加语义、PS 清零）——即使本网络没有 conv→eca 配对。
        self.chsum_probe = None
        self._chsum_probe_n = 0
        self._chsum_probe_bad = 0

    # 标度辅助
    def _s(self, name, dflt=1.0):
        v = self.scales.get(name)
        return float(v) if v else dflt

    # 布局辅助（2026-10-23）
    def _is_c8(self, name):
        """该张量是否 C8 交错布局（查编译期算好的表）。"""
        return bool(self.layout.get(name, False))

    def _lay(self, name):
        """布局标签 'c8' / 'chw'（ops_c8 的入参约定）。"""
        return c8_layout.name(self._is_c8(name))

    def _logical_c(self, name):
        """逻辑通道数（坑：C8 视图的 shape[0] 是通道块数，不是通道数）。"""
        meta = self.m["buffers"][name]
        return int(meta["c"])

    def _lut(self, s_from, s_to):
        """标度重定标用的 256 项查找表（int8 全值域穷举，逐位等价于浮点公式）。

        原来每次重定标都要 t→float64→乘→rint→clip→int8，在 640 图上
        单张要过 ~290 MB，临时数组是 int8 的 8 倍。因为映射只依赖 int8 的
        256 个可能取值，直接把这 256 个结果算出来做成表即可。
        表项用与旧实现完全相同的表达式（float64 乘法 + rint + clip）算出，
        所以结果逐位一致；且从此不再有跨平台浮点差异。
        返回 256 字节的 bytes（供 `bytes.translate` 用，见 _req）。

        2026-10-22：表的具体计算搬进 `lut_util.rescale_lut_bytes`，
        与融合出口的 `make_out_lut` 共用同一份实现（Runner 只保留缓存）。
        """
        key = (s_from, s_to)
        lut = self._luts.get(key)
        if lut is None:
            lut = lut_util.rescale_lut_bytes(s_from, s_to)
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

    def _plan_fuse(self):
        """规划"卷积出口直接写目标标度"（FUSE_OUT 融合出口）。

        只有同时满足下面全部条件的 conv 才纳入（任一条不满足就完全走旧路径，
        行为逐位不变）：

          ① 该 conv 的输出张量是某个 cat 别名组的 **direct** 成员
             （生产者本来就直接写进组缓冲的通道切片）；
          ② 该张量的消费者集合**恰好只有那个 cat**。若在 cat 之前还被别的
             算子读过（例如 Ghost 的 primary 输出同时喂 cheap_operation），
             把它写成 cat 的目标标度会改坏那次读取，所以必须排除；
          ③ 该 cat 确实在 alias["noop"] 里（零拷贝路径没有被整组回退）。

        返回 (plan, skip)：
          plan[conv_op_id] = (张量名, cat_op_id, s_from, s_to, lut_int8)
          skip[张量名]     = cat_op_id（cat 分支据此跳过该段重定标）
        """
        _prod, cons = self._prod_cons_map()

        # 张量 -> [(cat_op_id, mode)]（只记零拷贝布局里的成员）
        lay_of = {}
        for cid, lay in self.alias["noop"].items():
            for t, _off, mode in lay:
                lay_of.setdefault(t, []).append((cid, mode))

        plan, skip = {}, {}
        for op in self.ops:
            if op["op"] != "conv":
                continue
            t = op["out"][0]
            if t not in self.alias["member"]:
                continue
            cs = cons.get(t, [])
            if len(cs) != 1 or cs[0].get("op") != "cat":
                continue
            cat = cs[0]
            if cat["id"] not in self.alias["noop"]:
                continue
            if [m for cid, m in lay_of.get(t, []) if cid == cat["id"]] != ["direct"]:
                continue
            s_from = self._s(t)
            s_to = self._s(cat["out"][0])
            plan[op["id"]] = (t, cat["id"], s_from, s_to,
                              lut_util.make_out_lut(s_from, s_to))
            skip[t] = cat["id"]
        return plan, skip

    def _prod_cons_map(self):
        """张量 → 生产者 / 张量 → [消费者算子]（惰性建表，两条融合规划共用）。"""
        if not hasattr(self, "_prod_cons"):
            prod, cons = {}, {}
            for op in self.m["ops"]:
                for t in op.get("out", []):
                    prod.setdefault(t, op)
                for t in op.get("in", []):
                    cons.setdefault(t, []).append(op)
            self._prod_cons = (prod, cons)
        return self._prod_cons

    def _plan_fuse_sum(self):
        """规划"卷积写回时顺便累加每通道和"（FUSE_SUM=1 的 chsum 出口）。

        ECA 前半段要的是每个通道的空间均值，而均值只依赖通道和：
            mean_c = Σ_{y,x} out[c,y,x] × s / (H·W)
        所以让引擎在写回时顺手把 int8 值累加出来，ECA 就不必再整块扫一遍
        （原本 144×320×320 的张量要 astype(float32) 再求均值）。

        条件（与 out_lut 那条一样严格，任一条不满足就完全走旧路径）：
          ① 该 conv 的输出张量恰好只有 **一个** 消费者；
          ② 那个消费者是 **eca**，且它的输入正是这个张量；
          ③ 该 eca 本轮真的要执行（needed）；
          ④ int32 累加不会溢出：127·H·W < 2^31（RTL 里 chsum 是 int32）。

        注意（2026-10-22 实测）：本网络 6 个 ECA 的输入都是 `fuse_cat`
        （`cat(y0, up2(y2), up4(y4))`，见 tools/build_manifest.py 的 sam()），
        **不是** 任何一个 conv 的直接输出，所以当前拓扑下本规划恒为空表。
        机制保留：它只依赖"唯一消费者是 eca"这一条数据流事实，拓扑一变
        （或 RTL 把 ECA 直接接在 conv 后面）就能生效。

        返回 plan[conv_op_id] = (张量名, cout)
        """
        _prod, cons = self._prod_cons_map()
        bufs = self.m["buffers"]
        needed = set(op["id"] for op in self.ops)
        plan = {}
        for op in self.ops:
            if op["op"] != "conv":
                continue
            t = op["out"][0]
            cs = cons.get(t, [])
            if len(cs) != 1:
                continue
            eca = cs[0]
            if eca.get("op") != "eca" or eca["id"] not in needed:
                continue
            if not eca.get("in") or eca["in"][0] != t:
                continue
            cout = int(op.get("cout", bufs[t]["c"]))
            if cout != bufs[t]["c"]:
                continue
            if 127 * int(op["h"]) * int(op["w"]) >= (1 << 31):
                continue        # RTL 的 chsum 是 int32，宁可回落也不溢出
            plan[op["id"]] = (t, cout)
        return plan

    def _verify_chsum(self, sums, tname, tag):
        """把 chsum 与"拿特征图重算的通道和"逐位对拍，返回不一致的通道数。

        只用于 bring-up 自检：重算本身就是 ECA 原本要做的那次扫描。
        """
        v = self.arena.view(tname)
        is8 = self._is_c8(tname)
        # C8 下重算通道和必须先转 CHW；按**逻辑**通道数切掉补零通道
        v = c8_layout.to_chw(v, is8, self._logical_c(tname))
        nc = v.shape[0]
        sums = np.asarray(sums)
        if sums.shape[0] != nc:
            raise RuntimeError("chsum 长度 %d != 张量 %s 的通道数 %d"
                               % (sums.shape[0], tname, nc))
        ref = v.reshape(nc, -1).sum(axis=1, dtype=np.int64)
        got = sums.astype(np.int64)
        bad = int(np.count_nonzero(ref != got))
        if bad:
            c0 = int(np.argmax(ref != got))
            print("WARN: chsum 与特征图重算不一致 %d/%d 通道"
                  "（首处 c=%d chsum=%d 重算=%d，%s）"
                  % (bad, nc, c0, int(got[c0]), int(ref[c0]), tag), flush=True)
        return bad

    def _resolve_probe(self):
        """把 chsum_probe（op id / 卷积名，可逗号分隔多个）解析成 op id 集合。"""
        p = self.chsum_probe
        if p is None:
            return set()
        items = p if isinstance(p, (list, tuple, set)) else str(p).split(",")
        convs = [o for o in self.ops if o["op"] == "conv"]
        by_name = {}
        for o in convs:
            by_name[o["name"]] = o["id"]
        ids = set()
        for raw in items:
            s = str(raw).strip()
            if not s:
                continue
            if s.isdigit():
                pid = int(s)
                if not any(o["id"] == pid for o in convs):
                    raise ValueError("chsum_probe=%s 不是本轮执行的卷积 op id" % pid)
                ids.add(pid)
            elif s in by_name:
                ids.add(by_name[s])
            else:
                raise ValueError("chsum_probe=%r 找不到同名卷积（例：%s）"
                                 % (s, convs[0]["name"] if convs else "（无卷积）"))
        if not ids:
            raise ValueError("chsum_probe=%r 没解析出任何卷积" % (p,))
        return ids

    @staticmethod
    def _sam_sum_c8(out, y0, y2, y4, attn, x, s0, s2, s4, sx, s_out, nc, c8=None):
        """sam_sum 的 C8 原地实现（纯逐元素，与 CHW 路径逐位相同）。

        out/y0/y2/y4/x 都是 (C/8,H,W,8) 视图；attn 永远是 fp32 CHW 的 (3C,1,1)。
        10 维广播的坑：权重必须按 ``(块, 1, 1, 8)`` 广播（**最后那一维是块内
        通道 8**，不是新插的一维）——写成 ``[:, None, None, None]`` 会把 8 顶到
        新轴上，得到 (块,块,H,W,8) 而报 broadcast 错（2026-10-23 踩过一次）。
        分块 = 2 个通道块（16 通道），与 CHW 路径的分块宽度一致。
        """
        C8 = int(c8 or c8_layout.C8)
        if int(nc) % C8:
            # 补零张量（3ch/12ch）不能走这条"按通道块整体处理"的快路径：
            # 最后一块里混着补零通道，按块算会把它们当成真实通道算进去。
            # 本网络的 sam_sum 全是 8 对齐张量 ⇒ 永远不触发，这里只是防呆。
            raise ValueError("sam_sum 的 C8 直算要求通道数 8 对齐（nc=%r）" % (nc,))
        blocks = nc // C8
        # C 内核快路径（与下面 numpy 循环逐元素同序；自检不过会抛异常回落）
        try:
            ops.sam_sum_c8_into(out, y0, y2, y4, attn, x,
                                s0, s2, s4, sx, s_out)
            return out
        except Exception:
            pass
        w = attn.reshape(3, -1).reshape(3, blocks, C8)
        step = 2 if blocks >= 2 else 1
        for b0 in range(0, blocks, step):
            bs = slice(b0, min(b0 + step, blocks))
            # (块,1,1,8) 广播到 (块,H,W,8)
            w0 = w[0][bs][:, None, None, :]
            w1 = w[1][bs][:, None, None, :]
            w2 = w[2][bs][:, None, None, :]
            yf = (w0 * (y0[bs].astype(np.float64) * s0)
                  + w1 * (y2[bs].astype(np.float64) * s2)
                  + w2 * (y4[bs].astype(np.float64) * s4))
            xf = x[bs].astype(np.float64) * sx
            out[bs] = np.clip(np.rint((xf + yf) / s_out),
                              -128, 127).astype(np.int8)
        return out

    def run(self, img_int8=None):
        A = self.arena
        t_in = self.m["io"]["t_in"]
        if img_int8 is not None:
            # 输入张量若走 C8（本网络 t0 是 3ch，不会），这里顺带转换
            A.view(t_in)[...] = c8_layout.from_chw(img_int8, self._is_c8(t_in))
        # v2 层间算子加速器（pp_ops）是 **CHW 地址语义**（它按 (C,H,W) 拆形状、
        # 按 c*H*W 步长做 DMA），C8 下不能用 ⇒ C8 模式自动关掉它，层间算子全走
        # ops_c8（正确性优先；这是规范 §4 的"转换开销 vs 收益"取舍点）。
        ppen = set() if self.c8 else self.pp_ops_en
        if self.c8 and self.pp_ops_en:
            print("INFO: C8 布局已启用 → 层间算子全部走 ops_c8（v2 加速器是 CHW "
                  "地址语义，C8 下自动禁用）", flush=True)
        fuse_plan, fused, sum_plan = {}, {}, {}
        self._chsum = {}
        if self.fuse_conv:
            if not getattr(self.engine, "supports_fuse", False):
                raise RuntimeError(
                    "PP_FUSE_CONV=1 需要支持融合出口的引擎（RefEngine / PYNQEngine）")
            # 两个出口独立：只有驱动真的注入了该寄存器的偏移才规划对应链路，
            # 否则会拿着一个不存在的端口去写寄存器（= 往别人地址写）。
            if getattr(self.engine, "fuse_out_ok", True):
                fuse_plan, fused = self._plan_fuse()
            else:
                print("INFO: 未声明 out_lut 偏移 → 跳过 conv→cat 融合规划", flush=True)
            if getattr(self.engine, "fuse_sum_ok", True):
                sum_plan = self._plan_fuse_sum()
            else:
                print("INFO: 未声明 chsum 偏移 → 跳过 conv→eca 融合规划", flush=True)
            if fuse_plan or sum_plan:
                print("INFO: 卷积融合出口已启用：conv→cat %d 个（直接写目标标度），"
                      "conv→eca %d 个（写回时累加通道和）"
                      % (len(fuse_plan), len(sum_plan)), flush=True)
                # chsum 缓冲：每个配对一块（cout 个 int32），先清零。
                # 按张量名索引，ECA 分支据此取用（用完即 pop，生命周期到 eca 为止）。
                for _cid, (tname, cout) in sum_plan.items():
                    buf, view = self.engine.alloc_chsum(cout)
                    view[...] = 0
                    f = getattr(buf, "flush", None)
                    if f is not None:
                        f()
                    self._chsum[tname] = (buf, view)
        fused_cats = set(fused.values())
        probe_ids = self._resolve_probe()
        self._fuse_skips = 0
        self._fuse_sums = 0
        self._chsum_verify_bad = 0
        self._chsum_probe_n = 0
        self._chsum_probe_bad = 0
        self._fuse_plan_n = len(fuse_plan)      # conv→cat 配对数（诊断输出用）
        self._fuse_sum_plan_n = len(sum_plan)   # conv→eca 配对数（诊断输出用）
        t0 = time.time()
        for op in self.ops:
            k = op["op"]
            o0 = op["out"][0]
            t_op = time.time() if self.profile is not None else 0.0
            if k == "conv":
                fp = fuse_plan.get(op["id"])
                sp = sum_plan.get(op["id"])
                cbuf = self._chsum[sp[0]][0] if sp is not None else None
                cview = self._chsum[sp[0]][1] if sp is not None else None
                if op["id"] in probe_ids and cbuf is None:
                    # 一次性自检：给这个 conv 单独挂一块 chsum 缓冲
                    cbuf, cview = self.engine.alloc_chsum(op["cout"])
                    cview[...] = 0
                    f = getattr(cbuf, "flush", None)
                    if f is not None:
                        f()
                if fp is not None:
                    # 写回时直接应用 256 项 LUT（本层标度 -> 目标张量标度）
                    self.engine.conv(op["in"][0], o0, op, out_lut=fp[4], chsum=cbuf)
                elif cbuf is not None:
                    # 写回时顺便累加每通道和（ECA 前半段用它替代整块扫描）
                    self.engine.conv(op["in"][0], o0, op, chsum=cbuf)
                else:
                    self.engine.conv(op["in"][0], o0, op)
                if op["id"] in probe_ids:
                    self._chsum_probe_n += 1
                    self._chsum_probe_bad += self._verify_chsum(
                        cview, o0, "probe:" + op["name"])
            elif k == "pixel_unshuffle":
                i0 = op["in"][0]
                if self.c8:
                    # 这两个算子是唯一会碰到补零张量的（t0 3ch -> t1 12ch、
                    # t520 12ch -> t521 3ch），必须把**逻辑**通道数传下去：
                    # 输入按 c_in 切掉补零通道再重排，输出按 c_out 补零后放回。
                    A.view(o0)[...] = c8_layout.ops_c8().pixel_unshuffle(
                        A.view(i0), self._lay(i0), op["factor"],
                        out_lay=self._lay(o0),
                        c_in=self._logical_c(i0), c_out=self._logical_c(o0))
                else:
                    A.view(o0)[...] = ops.pixel_unshuffle(A.view(i0), op["factor"])
            elif k == "pixel_shuffle":
                i0 = op["in"][0]
                if self.c8:
                    A.view(o0)[...] = c8_layout.ops_c8().pixel_shuffle(
                        A.view(i0), self._lay(i0), op["factor"],
                        out_lay=self._lay(o0),
                        c_in=self._logical_c(i0), c_out=self._logical_c(o0))
                else:
                    A.view(o0)[...] = ops.pixel_shuffle(A.view(i0), op["factor"])
            elif (k == "interp" and "interp" in ppen
                    and self.pp is not None
                    and pp_ops.run_interp(self.pp, A, op, A.view, self._s)):
                pass                                  # v2 加速器完成（纯搬运，逐位一致）
            elif k == "interp":
                i0 = op["in"][0]
                if self.c8:
                    # interp 只动 H/W，但 ops.interp 按 3-D 写死 ⇒ 转 CHW 算再转回
                    A.view(o0)[...] = c8_layout.ops_c8().interp(
                        A.view(i0), self._lay(i0), op["scale_num"], op["scale_den"],
                        out_lay=self._lay(o0))
                else:
                    A.view(o0)[...] = ops.interp(A.view(i0),
                                                 op["scale_num"], op["scale_den"])
            elif k == "slice":
                i0 = op["in"][0]
                if self.c8:
                    A.view(o0)[...] = c8_layout.ops_c8().slice_channels(
                        A.view(i0), self._lay(i0), op["channels"], 0,
                        out_lay=self._lay(o0))
                else:
                    A.view(o0)[...] = A.view(i0)[:op["channels"]]
            elif k == "cat":
                lay = self.alias["noop"].get(op["id"])
                if lay is not None:
                    # 零拷贝：direct 输入已由生产者按通道偏移写在同一块缓冲区里
                    # （见 compute_cat_alias），这里只剩"标度不一致时的原地重标"；
                    # copy 输入（之后还要被别的算子消费）才真正搬一次。
                    # 重标定用 256 项查找表，逐位等价于原来的 float64 公式。
                    s_out = self._s(o0)
                    out = A.view(o0)
                    out8 = self._is_c8(o0)
                    if ("cat" in ppen and self.pp is not None
                            and op["id"] not in fused_cats
                            and pp_ops.run_cat_rescale(self.pp, A, op, A.view,
                                                       self._s, lay)):
                        lay = []                          # 已由 v2 完成，跳过逐段重标
                    for n, off, mode in lay:
                        if fused.get(n) == op["id"]:
                            # 该段的生产者 conv 已用 out_lut 直接写成目标标度
                            # （见 _plan_fuse），这里不能再重定标一次。
                            self._fuse_skips += 1
                            continue
                        sn = self._s(n)
                        if mode == "copy":
                            # 通道切片：CHW 是 out[off:off+c]，C8 是通道块切片
                            # out[off/8:(off+c)/8]（后者要求 off/c 都对齐 8，
                            # C8 别名组已保证——见 compute_cat_alias）
                            c = self._logical_c(n)
                            dst = c8_layout.ch_slice(out, out8, off, c)
                            dst[...] = self._req(A.view(n), sn, s_out)
                        elif sn != s_out:
                            v = A.view(n)
                            v[...] = np.frombuffer(v.tobytes().translate(
                                self._lut(sn, s_out)), dtype=np.int8).reshape(v.shape)
                # C8：非别名路径统一交给 ops_c8.cat（段通道数都对齐 8 时是块级
                # 拼接，只搬字节、零通道重排；有不对齐的段则由它回退 CHW）。
                elif self.c8:
                    s_out = self._s(o0)
                    # 块级拼接的快路径要求**每一段的逻辑通道数都对齐 8**：
                    # 补零张量（3ch/12ch）的最后一块混着补零通道，按块拼会把
                    # 补零通道一并搬进去、后面的段整体串位。实测 454/454 段
                    # 全部对齐，这里只是防呆（宁可响亮报错，不要静默错位）。
                    unaligned = sorted({n for n in op["in"]
                                        if self._logical_c(n) % c8_layout.C8})
                    if unaligned:
                        raise RuntimeError(
                            "cat 的段含非 8 对齐张量（%s），C8 块级拼接不成立"
                            % ",".join(unaligned))
                    segs = [(c8_layout.as_layout(
                                self._req(A.view(n), self._s(n), s_out),
                                self._is_c8(n), self._is_c8(o0)),
                             self._lay(o0)) for n in op["in"]]
                    A.view(o0)[...] = c8_layout.ops_c8().cat(
                        segs, self._logical_c(o0), self._lay(o0), op.get("channels"))
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
                if ("add" in ppen and self.pp is not None
                        and pp_ops.run_add(self.pp, A, op, A.view, self._s)):
                    pass                                  # v2 加速器完成（逐位一致）
                else:
                    s_out = self._s(o0)
                    a = self._req(A.view(op["in"][0]), self._s(op["in"][0]), s_out)
                    b = self._req(A.view(op["in"][1]), self._s(op["in"][1]), s_out)
                    if self.c8:
                        # 逐元素，布局无关；只要求两个操作数与输出同布局
                        out8 = self._is_c8(o0)
                        a = c8_layout.as_layout(a, self._is_c8(op["in"][0]), out8)
                        b = c8_layout.as_layout(b, self._is_c8(op["in"][1]), out8)
                    A.view(o0)[...] = ops.add(a, b)
            elif k == "eca":
                # 2026-10-21：按通道分块算空间均值。原来把整块（144×320×320）
                # astype 成 float32（59 MB 临时）再求均值；分块后临时数组只有
                # 32 通道那么大，且逐通道均值与分块无关 → 结果逐位不变。
                # 2026-10-22：若该输入张量是某个 conv 的**唯一消费者产物**，
                # 通道和已经由 FUSE_SUM 的 chsum 出口在写回时累加好了 → 直接
                # 用和算均值，pp/分块扫描两条路都不必走（见 _plan_fuse_sum）。
                e = self.eca[op["weights_key"]]
                v = A.view(op["in"][0])
                sc = self._s(op["in"][0])
                in8 = self._is_c8(op["in"][0])
                # 逻辑通道数（坑：C8 视图的 shape[0] 是通道块数）
                nc = self._logical_c(op["in"][0])
                hh, ww = c8_layout.hw(v.shape, in8)
                hw = float(hh * ww)
                mean_c = None
                ent = self._chsum.pop(op["in"][0], None)
                if ent is not None:
                    _cbuf, sums = ent
                    if self.verify_chsum:
                        # 板端 bring-up 用：直接拿特征图重算一次和来对拍
                        # （会让这一段退化回扫描，只用于验证，不用于提速）
                        self._chsum_verify_bad += self._verify_chsum(
                            sums, op["in"][0], "eca:" + op["name"])
                    # 与下面 pp/回退路径同一个公式：mean = 精确和 × s / (H·W)
                    mean_c = (np.asarray(sums).astype(np.float64) * sc / hw).astype(np.float32)
                    self._fuse_sums += 1
                if mean_c is None and "eca" in ppen and self.pp is not None:
                    sums, ok = pp_ops.run_eca_mean(self.pp, A, A.view, self._s,
                                                   op["in"][0], nc, hh, ww)
                    if ok:
                        # 均值 = (int64 精确和) × 标度 / (H·W)；比 numpy 的
                        # float32 逐步累加更准，差异只在最后几位（attn 层面）
                        mean_c = (sums.astype(np.float64) * sc / hw).astype(np.float32)
                if mean_c is None and in8 and nc % 8 == 0:
                    # C8 且 pp 被旁路：用 C 内核（8 字节一组顺序扫 + 私有缓冲
                    # 暂存）直接算 int64 通道和，再走与 pp 完全相同的均值公式。
                    # 板端 6 个 eca 从 5.3 s 降到 ~0.6 s；attn 最后量化到 2^-12，
                    # 与 CHW/pp 路径的 ulp 级差异不影响结果（已整网 0 对拍）。
                    try:
                        sums8 = ops.chsum_c8(v, nc)
                        mean_c = (sums8.astype(np.float64) * sc / hw).astype(np.float32)
                    except Exception:
                        mean_c = None
                if mean_c is None:
                    # **ECA 必须转 CHW**：numpy 的 float32 空间均值对内存布局敏感
                    # （C8 上按 (Cb,H,W,8) 走带步长归约，尾数与 CHW 的连续块
                    # pairwise 求和不同），差一个 ulp 就会雪崩到 59% 像素。
                    # 按 32 通道分块转换 ⇒ 临时数组与旧 CHW 路径同量级，
                    # 且每块的均值公式与旧代码**逐字相同**（分块与归约顺序无关）。
                    mean_c = np.empty(nc, dtype=np.float32)
                    for c0 in range(0, nc, 32):
                        c1 = min(c0 + 32, nc)
                        # CHW 时 chw_chunk 就是 v[c0:c1]（零拷贝），逐位不变
                        fin = np.asarray(c8_layout.chw_chunk(v, in8, c0, c1)).astype(
                            np.float32)
                        fin *= sc
                        mean_c[c0:c1] = fin.mean(axis=(1, 2), dtype=np.float32)
                A.view(o0)[...] = ops.eca_attn_from_mean(mean_c, e["conv2"], e["conv1"])
            elif (k == "sam_sum" and "sam_sum" in ppen
                    and self.pp is not None
                    and pp_ops.run_sam_sum(self.pp, A, op, A.view, self._s)):
                pass
            elif k == "sam_sum":
                # 按通道分块算（原来是整块 float64，单张 5 个张量 ≈ 200 MB 临时数组）。
                # 分块后逐元素运算与分块前完全一致，结果逐位不变。
                y0, y2, y4, attn, x = (A.view(n) for n in op["in"])
                s_out = self._s(o0)
                s0, s2, s4 = (self._s(op["in"][i]) for i in (0, 1, 2))
                sx = self._s(op["in"][4])
                out = A.view(o0)
                if self.c8:
                    # C8 直接算（纯逐元素、无归约 ⇒ 逐位相同；已由软件侧报告
                    # 对拍过 0 不一致）。分块单位 = 2 个通道块 = 16 通道，
                    # 与 CHW 路径的分块宽度一致。见 _sam_sum_c8（可单测）。
                    out8 = self._is_c8(o0)
                    y0 = c8_layout.as_layout(y0, self._is_c8(op["in"][0]), out8)
                    y2 = c8_layout.as_layout(y2, self._is_c8(op["in"][1]), out8)
                    y4 = c8_layout.as_layout(y4, self._is_c8(op["in"][2]), out8)
                    x = c8_layout.as_layout(x, self._is_c8(op["in"][4]), out8)
                    self._sam_sum_c8(out, y0, y2, y4, attn, x,
                                     s0, s2, s4, sx, s_out,
                                     self._logical_c(o0))
                else:
                    # CHW：逐位一致前提下的分块快路径（ops.sam_sum_into）。
                    # 旧写法每个 16 通道块要分配 ~10 份 float64 全图临时数组，
                    # 板端 6 个 sam_sum 共 24.7 s；新写法只保留两份 ~64 KB
                    # 缓冲，逐元素运算顺序不变（主机对拍 0 差异）。
                    ops.sam_sum_into(out, y0, y2, y4, attn, x,
                                     s0, s2, s4, sx, s_out)
            else:
                raise RuntimeError("未知算子: " + k)
            if self.record is not None:
                # 调试快照一律存成 CHW（C8 下从 C8 转回），这样两路可逐算子对比
                self.record[op["name"]] = np.ascontiguousarray(
                    c8_layout.to_chw(A.view(o0), self._is_c8(o0),
                                     self._logical_c(o0))).copy()
            if self.profile_ops is not None:
                self.profile_ops.append((op["id"], op["name"], k,
                                         (time.time() - t_op) if self.profile is not None else 0.0))
            if self.profile is not None:
                p = self.profile.setdefault(k, [0.0, 0])
                p[0] += time.time() - t_op
                p[1] += 1
            if self.hashlog is not None:
                import hashlib
                # 同样统一成 CHW（形状串与 sha1 都与布局无关）⇒ 可以拿 CHW 与
                # C8 两路的 hashlog 直接 diff，定位第一个发散的算子。
                arr = np.ascontiguousarray(
                    c8_layout.to_chw(A.view(o0), self._is_c8(o0),
                                     self._logical_c(o0)))
                self.hashlog.append((op["name"], k, "%d,%d,%d" % arr.shape,
                                     hashlib.sha1(arr.tobytes()).hexdigest()[:16]))
        dt = time.time() - t0
        # 网络输出按 CHW 返回（本网络 out1 是 3ch，本来就是 CHW；若某个 manifest
        # 的输出走 C8，这里转回来，与部署包 ref_out.bin 的格式保持一致）
        o1 = self.m["io"]["out1"]
        out = np.ascontiguousarray(
            c8_layout.to_chw(A.view(o1), self._is_c8(o1),
                             self._logical_c(o1))).copy()
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
