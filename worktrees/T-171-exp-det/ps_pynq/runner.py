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
    import ref_numpy                   # 权重布局标签（k3_plane）/ 金标准 conv
    import i8_fast                     # int8 拷贝/重定标的 C 内核（可选）
except ImportError:                    # 仓库里作为 pynq 包时
    from . import pp_ops
    from . import lut_util
    from . import c8_layout
    from . import ref_numpy
    try:
        from . import i8_fast
    except ImportError:
        i8_fast = None

# 手写 k=1 加速器驱动（2026-10-23）。单独 try：缺这个文件时 runner 仍可用，
# 只是 USE_K1_IP 无法生效（构造 PYNQEngine 时打印警告并回退老引擎）。
try:
    import k1_engine
except ImportError:                    # 仓库里作为 pynq 包时
    try:
        from . import k1_engine
    except ImportError:
        k1_engine = None

# 手写 k=3 / k=5 加速器驱动（2026-10-27）。与 k1 同样的隔离方式：
# 缺文件不影响 runner 本身，只是 USE_K3_IP / USE_K5_IP 无法生效。
try:
    import k3_engine
except ImportError:                    # 仓库里作为 pynq 包时
    try:
        from . import k3_engine
    except ImportError:
        k3_engine = None
try:
    import k5_engine
except ImportError:
    try:
        from . import k5_engine
    except ImportError:
        k5_engine = None


def _env_default_on(name):
    """环境变量开关：**默认开**，只有显式写 0/false/off/no 才关。

    与 ``_env_flag`` 的区别只在"未设置"时：k=3/k=5 的 IP 是部署的硬前提
    （HLS 已经不支持 k=1/k=5，新 bit 里干脆没有 HLS 引擎），所以默认开；
    出问题用 ``USE_K3_IP=0`` / ``USE_K5_IP=0`` 显式回退（那等于承认"本 bit
    的 HLS 还能算这一档"，由操作者负责）。
    """
    v = os.environ.get(name, "")
    s = str(v).strip().lower()
    if s == "":
        return True
    return s not in ("0", "false", "off", "no")



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


def _rescale_pairs(manifest, scales, alias=None):
    """整网会用到重定标的 (s_from, s_to) 对（预热用）。

    口径取 cat/add/sam_sum 三类汇合算子的输入→输出；alias 组里的 direct/copy
    段都按这条口径走，所以这就是 `_req` 一族实际会请求的全部组合。
    """
    pairs = set()
    for op in manifest["ops"]:
        if op["op"] not in ("cat", "add", "sam_sum"):
            continue
        so = scales.get(op["out"][0])
        if so is None:
            continue
        for t in op["in"]:
            st = scales.get(t)
            if st is not None and float(st) != float(so):
                pairs.add((float(st), float(so)))
    return pairs


def _w_b(index, blob, key):
    """取某层的 (权重, 偏置) 视图。

    权重按 `w_shape` 原样 reshape，**不做下标重排**：k=3 的
    `w_layout="k3_plane"`（9 个平面 [ky][kx][oc][ic]）由
    `ref_numpy.conv_ref_numpy(w_layout=...)` 负责解释。
    PL 引擎读的是同一个 buffer 里的同一串字节，所以"参考实现怎么读"必须与
    `conv_ref_rt.cpp` 的下标公式逐字一致（否则就是"两边一起错"）。
    """
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

    def conv_fuse_ok(self, op, out_lut=False, chsum=False):
        """本层的融合出口能不能用（本机假引擎什么都能做，恒 True）。

        真引擎（``PYNQEngine``）会对手写 IP 接走的层返回 False —— 那些 IP
        没有 out_lut/chsum 寄存器。Runner 据此把它们排除在融合规划之外。
        """
        del out_lut, chsum
        return True

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
        e = self.index["layers"][op["weights_key"]]
        y = conv_ref_numpy(src, w, b,
                           op["k"], op["s"], op["d"], op["pad"],
                           e["shift"],
                           relu=bool(op["relu"]),
                           w_layout=ref_numpy.layout_of(e),
                           w_tap=e.get("tap"))
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


def k1_route_check(op, entry, c8_in, c8_out, out_lut=None, chsum=None,
                   relu_capable=1, cin_max=None, cout_max=None):
    """判断一层 conv 能否交给新手写 K1 IP；返回 ``(ok, reason)``。

    纯函数（不碰硬件、不导入 pynq），本机可以对 manifest 全量预演。
    硬约束与 ``k1_engine.check_supported`` **同一份实现**；这里额外补三条
    **容器级**约束：

      1. 权重必须是 ``[cout, cin, 1, 1]`` 的 ``oc_ic_ky_kx`` 连续字节，
         且 ``w_len == cout*cin``（K1 RTL 的下标是 ``weight[oc*cin + ic]``，
         不能有 PAD）；
      2. bias 必须是 ``cout`` 个 int32；
      3. 本层不能带 ``out_lut`` / ``chsum`` 融合（K1 wrapper 没有这两个出口）。

    ``out_lut`` / ``chsum`` 传的是**本层实际要用的**融合缓冲（None = 不用）。
    """
    if k1_engine is None:
        return False, "k1_engine 模块不可用"
    if op.get("op") != "conv":
        return False, "不是 conv"
    # 按核型分流：k=3/k=5 走 k3_route_check / k5_route_check（见下面），
    # 不要让它们掉进"权重容器不匹配"这种噪音原因里（否则每种形状刷一条 WARN）。
    # 统一入口是 conv_route_check() / route_tag()。
    if int(op.get("k", 0)) != 1:
        return False, "k=%s ≠ 1（本 IP 只做 1×1）" % (op.get("k"),)
    if out_lut is not None:
        return False, "本层启用 out_lut 融合（K1 IP 无该出口）"
    if chsum is not None:
        return False, "本层启用 chsum 融合（K1 IP 无该出口）"
    if entry is None:
        return False, "index.json 缺少该层权重记录"
    ws = entry.get("w_shape")
    wl = entry.get("w_layout") or "oc_ic_ky_kx"
    try:
        ws = [int(x) for x in ws]
    except (TypeError, ValueError):
        return False, "w_shape 非法：%r" % (ws,)
    want = [int(op["cout"]), int(op["cin"]), 1, 1]
    if wl != "oc_ic_ky_kx" or ws != want:
        return False, ("权重容器不是 [cout,cin,1,1]/oc_ic_ky_kx"
                       "（w_shape=%r w_layout=%s，应为 %r；K1 按 "
                       "weight[oc][ic] 逐字节读）" % (ws, wl, want))
    try:
        blen = int(entry.get("b_len", -1))
    except (TypeError, ValueError):
        blen = -1
    if blen != 4 * int(op["cout"]):
        return False, ("bias 长度 %r ≠ 4*cout=%d（应为 int32×cout）"
                       % (blen, 4 * int(op["cout"])))
    try:
        wlen = int(entry.get("w_len", -1))
    except (TypeError, ValueError):
        wlen = -1
    if wlen != int(op["cout"]) * int(op["cin"]):
        return False, ("w_len %r ≠ cout*cin=%d（K1 按 weight[oc][ic] 逐字节读，"
                       "不能有 PAD）" % (wlen, int(op["cout"]) * int(op["cin"])))
    extra = {}
    if cin_max is not None:
        extra["cin_max"] = int(cin_max)
    if cout_max is not None:
        extra["cout_max"] = int(cout_max)
    return k1_engine.eligibility(
        op["h"], op["w"], op["cin"], op["cout"], op["k"], op["s"], op["d"],
        op["pad"], entry.get("shift"), relu=bool(op.get("relu", True)),
        c8_in=c8_in, c8_out=c8_out, relu_capable=relu_capable,
        hout=op.get("hout"), wout=op.get("wout"), **extra)


def k3_route_check(op, entry, c8_in, c8_out, out_lut=None, chsum=None,
                   relu_capable=None, cin_max=None, cout_max=None,
                   hw_max=None, cap_b=None):
    """判断一层 conv 能否交给新手写 K3 IP；返回 ``(ok, reason)``。

    纯函数（不碰硬件、不导入 pynq），本机可以对 manifest 全量预演。
    结构与 ``k1_route_check`` 完全一致：**容器级/融合出口级**约束在这里，
    数值能力交给 ``k3_engine.eligibility``（同一份 ``check_supported``）。

    k3 比 k1 多的三条：
      1. 权重必须是 **PAD12** 容器：``w_layout="k3_pad12"``、
         ``w_shape=[cout,cin,12]``、``w_len=12*cout*cin``
         （9 抽头 + 3 零，``offset=(oc*cin+ic)*12+ky*3+kx``）；
      2. 允许 ``stride ∈ {1,2}``、``dilation ∈ {1,2,3}``、``pad == dilation``，
         但**不能**同时非 1；还要过槽容量 ``⌈cin/8⌉·W·(d≠1?2:1) ≤ 4608``；
      3. ReLU 是**运行期**寄存器位，默认不按编译期 RELU 卡
         （``relu_capable=None``；只有未来把 ReLU 综合成常量时才传 0/1）。
    """
    if k3_engine is None:
        return False, "k3_engine 模块不可用"
    if op.get("op") != "conv":
        return False, "不是 conv"
    if int(op.get("k", 0)) != 3:
        return False, "k=%s ≠ 3（本 IP 只做 3×3）" % (op.get("k"),)
    if out_lut is not None:
        return False, "本层启用 out_lut 融合（K3 IP 无该出口）"
    if chsum is not None:
        return False, "本层启用 chsum 融合（K3 IP 无该出口）"
    if entry is None:
        return False, "index.json 缺少该层权重记录"
    cin, cout = int(op["cin"]), int(op["cout"])
    ws = entry.get("w_shape")
    wl = entry.get("w_layout") or "oc_ic_ky_kx"
    try:
        ws = [int(x) for x in ws]
    except (TypeError, ValueError):
        return False, "w_shape 非法：%r" % (ws,)
    want = [cout, cin, 12]
    if wl != k3_engine.W_LAYOUT_K3 or ws != want:
        return False, ("权重容器不是 [cout,cin,12]/k3_pad12"
                       "（w_shape=%r w_layout=%s，应为 %r；K3 按 "
                       "(oc*cin+ic)*12+ky*3+kx 逐字节读，必须是 repack 过的 "
                       "PAD12 包）" % (ws, wl, want))
    try:
        blen = int(entry.get("b_len", -1))
    except (TypeError, ValueError):
        blen = -1
    if blen != 4 * cout:
        return False, ("bias 长度 %r ≠ 4*cout=%d（应为 int32×cout）"
                       % (blen, 4 * cout))
    try:
        wlen = int(entry.get("w_len", -1))
    except (TypeError, ValueError):
        wlen = -1
    if wlen != 12 * cout * cin:
        return False, ("w_len %r ≠ 12*cout*cin=%d（K3 的 PAD12 容器每个 "
                       "(oc,ic) 12 字节）" % (wlen, 12 * cout * cin))
    extra = {}
    if cin_max is not None:
        extra["cin_max"] = int(cin_max)
    if cout_max is not None:
        extra["cout_max"] = int(cout_max)
    if hw_max is not None:
        extra["hw_max"] = int(hw_max)
    if cap_b is not None:
        extra["cap_b"] = int(cap_b)
    return k3_engine.eligibility(
        op["h"], op["w"], op["cin"], op["cout"], op["k"], op["s"], op["d"],
        op["pad"], entry.get("shift"), relu=bool(op.get("relu", True)),
        c8_in=c8_in, c8_out=c8_out, relu_capable=relu_capable,
        hout=op.get("hout"), wout=op.get("wout"), **extra)


def k5_route_check(op, entry, c8_in, c8_out, out_lut=None, chsum=None,
                   ip_cin=None, ip_cout=None, ip_h=None, ip_w=None):
    """判断一层 conv 能否交给新手写 K5 IP；返回 ``(ok, reason)``。

    结构同 ``k1_route_check``/``k3_route_check``。k5 的特殊点：
      * 权重容器是 **PAD28**（``k5_pad28``，``[cout,cin,28]``，
        ``w_len=28*cout*cin``）；
      * 引擎的 ``CIN/COUT/W/H/PAD`` 是**编译期参数**，运行期形状不参与计算
        ⇒ 必须与 IP 的编译期参数逐一相等（由 ``k5_engine.check_supported`` 判）；
      * ReLU 是**硬连线**的 ⇒ ``relu=False`` 的层不可路由。
    """
    if k5_engine is None:
        return False, "k5_engine 模块不可用"
    if op.get("op") != "conv":
        return False, "不是 conv"
    if int(op.get("k", 0)) != 5:
        return False, "k=%s ≠ 5（本 IP 只做 5×5）" % (op.get("k"),)
    if out_lut is not None:
        return False, "本层启用 out_lut 融合（K5 IP 无该出口）"
    if chsum is not None:
        return False, "本层启用 chsum 融合（K5 IP 无该出口）"
    if entry is None:
        return False, "index.json 缺少该层权重记录"
    cin, cout = int(op["cin"]), int(op["cout"])
    ws = entry.get("w_shape")
    wl = entry.get("w_layout") or "oc_ic_ky_kx"
    try:
        ws = [int(x) for x in ws]
    except (TypeError, ValueError):
        return False, "w_shape 非法：%r" % (ws,)
    want = [cout, cin, 28]
    if wl != k5_engine.W_LAYOUT_K5 or ws != want:
        return False, ("权重容器不是 [cout,cin,28]/k5_pad28"
                       "（w_shape=%r w_layout=%s，应为 %r；K5 按 "
                       "(oc*cin+ic)*28+ky*5+kx 逐字节读，必须是 repack 过的 "
                       "PAD28 包）" % (ws, wl, want))
    try:
        blen = int(entry.get("b_len", -1))
    except (TypeError, ValueError):
        blen = -1
    if blen != 4 * cout:
        return False, ("bias 长度 %r ≠ 4*cout=%d（应为 int32×cout）"
                       % (blen, 4 * cout))
    try:
        wlen = int(entry.get("w_len", -1))
    except (TypeError, ValueError):
        wlen = -1
    if wlen != 28 * cout * cin:
        return False, ("w_len %r ≠ 28*cout*cin=%d（K5 的 PAD28 容器每个 "
                       "(oc,ic) 28 字节）" % (wlen, 28 * cout * cin))
    extra = {}
    if ip_cin is not None:
        extra["ip_cin"] = int(ip_cin)
    if ip_cout is not None:
        extra["ip_cout"] = int(ip_cout)
    if ip_h is not None:
        extra["ip_h"] = int(ip_h)
    if ip_w is not None:
        extra["ip_w"] = int(ip_w)
    return k5_engine.eligibility(
        op["h"], op["w"], op["cin"], op["cout"], op["k"], op["s"], op["d"],
        op["pad"], entry.get("shift"), relu=bool(op.get("relu", True)),
        c8_in=c8_in, c8_out=c8_out,
        hout=op.get("hout"), wout=op.get("wout"), **extra)


def route_tag(op):
    """本层该由哪个引擎接：``'k1'`` / ``'k3'`` / ``'k5'`` / ``'hls'`` / None。

    分流只看核型（k）。**能不能真的走**由对应的 ``*_route_check`` 判。
    这是 runner 里"k=3/k=5 是另一条线"的统一入口。
    """
    if not isinstance(op, dict) or op.get("op") != "conv":
        return None
    try:
        k = int(op.get("k", 0))
    except (TypeError, ValueError):
        return "hls"
    return {1: "k1", 3: "k3", 5: "k5"}.get(k, "hls")


def conv_route_check(op, entry, c8_in, c8_out, out_lut=None, chsum=None,
                     k1_caps=None, k3_caps=None, k5_caps=None):
    """**按核型分流**的统一 route check：返回 ``(ok, reason)``。

    k=1 → ``k1_route_check``；k=3 → ``k3_route_check``；k=5 → ``k5_route_check``；
    其它核型 → ``(False, "k=… 没有对应的手写 IP")``（只能走 HLS）。

    ``k1_caps/k3_caps/k5_caps`` 是各自的上界覆盖（例如板端 IP 的 CIN_MAX 与
    默认值不同），传 dict；缺省用模块默认值。
    """
    tag = route_tag(op)
    if tag == "k1":
        return k1_route_check(op, entry, c8_in, c8_out, out_lut, chsum,
                             **(k1_caps or {}))
    if tag == "k3":
        return k3_route_check(op, entry, c8_in, c8_out, out_lut, chsum,
                             **(k3_caps or {}))
    if tag == "k5":
        return k5_route_check(op, entry, c8_in, c8_out, out_lut, chsum,
                             **(k5_caps or {}))
    if tag is None:
        return False, "不是 conv"
    return False, "k=%r 没有对应的手写 IP（只能走 HLS）" % (op.get("k"),)


def _is_missing_hls_error(exc):
    """``Conv2dEngine`` 抛的"overlay 里找不到 conv2d_engine"这一类错误。

    2026-10-27：新 BD（`pl/scripts/bd_remove_hls.tcl`）已经把老 HLS 引擎整块
    删掉，bit 里只剩 k1_wrap + k3_wrap。这时 ``Conv2dEngine`` 的发现逻辑会抛
    ``RuntimeError("overlay 里找不到 conv2d_engine IP，...")``——这不是"接线
    错误"，而是"本 bit 就是这样"，要允许继续（否则整网一段都跑不了）。
    其它异常（缺 pynq、地址冲突、融合偏移非法…）必须原样抛。
    """
    s = str(exc)
    return "conv2d_engine" in s and ("找不到" in s or "not found" in s.lower())


class PYNQEngine:
    """上板真引擎：连续内存池 + 手写 IP / 老 HLS 引擎路由（上板联调时启用）。

    引擎来源（按 ``op["k"]`` 分流，见 ``route_tag()`` / ``conv_route_check()``）：

      =====  ==============================  ============================
      k      引擎                            说明
      =====  ==============================  ============================
      1      ``K1Engine``（``k1_wrap``）     已有、上板验证过
      3      ``K3Engine``（``k3_wrap``）     本次新增（0x4006_0000）
      5      ``K5Engine``（``k5_wrap``）     本次新增（IP 还没进 BD）
      其它   老 HLS ``Conv2dEngine``         **新 bit 里可能没有它**
      =====  ==============================  ============================

    默认开关（环境变量 / 构造参数）：

      · ``USE_K1_IP``  —— 默认**关**（保持整合前逐位一致的已验证行为）
      · ``USE_K3_IP``  —— 默认**开**
      · ``USE_K5_IP``  —— 默认**开**

    为什么 k3/k5 默认开、k1 默认关：新 bit 里老 HLS 引擎**整块被删**，HLS
    源码也对 k=1/k=5 明确返回 5/6 ⇒ 这两档没有退路；而 k1 驱动是上板验证过的
    "已经能工作"的路径，不动它的默认值。**跑新 bit 时必须同时
    ``USE_K1_IP=1``**（``ip_summary()`` 会明确提示这一点）。

    k=3/k=5 的路由失败（IP 没接入 / 本层不满足能力 / 带融合出口）**直接抛错，
    不回退 HLS**：回退只会拿到 HLS 的错误码，或者干脆没有 HLS 引擎。只有显式
    ``USE_K3_IP=0`` / ``USE_K5_IP=0`` 才允许落到 HLS（那是操作者的声明）。

    融合出口（``out_lut`` / ``chsum``，由 bit 的构建宏决定）：**只有老 HLS
    引擎有**。k1/k3/k5 手写 IP 都没有这两个出口，所以 Runner 会用
    ``conv_fuse_ok()`` 把要交给手写 IP 的层**排除**在融合规划之外（它们照旧
    按自己的标度写回，由 cat 做重定标，结果逐位不变，只是少省一次扫描）。
    """

    # 支持卷积融合出口，前提是 bit 里真有对应端口且驱动构造时注入了偏移
    supports_fuse = True

    def __init__(self, overlay, index, blob, arena, fuse_offsets=None,
                 use_k1=None, k1_ip_name=None, use_k3=None, k3_ip_name=None,
                 use_k5=None, k5_ip_name=None, use_hls=None):
        import types
        from pynq import allocate
        from conv_engine import Conv2dEngine
        self.types = types
        # numpy 兜底要用权重包原始字节（2026-10-28）
        self.index, self.arena = index, arena
        self.blob = blob
        # ---- 老 HLS 引擎（2026-10-27 起是**可选**的）----
        # 新 BD 只留 k1_wrap + k3_wrap；没有 HLS 时不能让构造失败（否则一段
        # 都跑不了），但要大声警告：落到 HLS 的层会在 conv() 里明确报错。
        self.unit = None
        self.hls_missing = False
        if use_hls is not False:
            try:
                self.unit = Conv2dEngine(overlay, fuse_offsets=fuse_offsets)
            except Exception as exc:                       # noqa: BLE001
                if use_hls is True or not _is_missing_hls_error(exc):
                    raise
                self.hls_missing = True
                print("WARN: 本 bit 没有老 HLS 卷积引擎（conv2d_engine）—— 新 BD "
                      "已把它删掉。只有 k=1/k=3/k=5 手写 IP 能接活；落到 HLS 的"
                      "层会直接报错（不会静默算错）", flush=True)
        else:
            self.hls_missing = True
            print("INFO: use_hls=False：不构造老 HLS 引擎（任何落到 HLS 的层都会"
                  "在 conv() 里报错）", flush=True)
        if self.unit is None and fuse_offsets is not None:
            raise RuntimeError(
                "传了 fuse_offsets，但本 bit 没有老 HLS 引擎的融合出口"
                "（conv2d_engine 不存在）：融合只能由 HLS 引擎做，"
                "k1/k3/k5 手写 IP 都没有 out_lut/chsum")
        # 支持卷积融合出口，前提是 bit 里真有对应端口且驱动构造时注入了偏移
        self.supports_fuse = bool(self.unit is not None)

        # ---- 手写 k=1 加速器路由（2026-10-23）----
        # 默认关：USE_K1_IP=1（或构造参数 use_k1=True）才尝试接入。
        # 接入失败**只警告**、不阻断整网（全部层回退老 HLS 引擎）。
        # 这样"新 IP 出问题"永远有一条 `USE_K1_IP=0` 的退路。
        self.use_k1 = (_env_flag("USE_K1_IP") if use_k1 is None
                       else bool(use_k1))
        self.k1 = None
        self.k1_calls = 0            # 实际交给 K1 IP 的层数
        self.k1_seconds = 0.0        # K1 IP 上累计的墙钟时间
        self.k1_skip_reasons = {}    # 回退原因 -> 层数
        self.k1_skip_examples = {}   # 回退原因 -> 例（最多 3 个）
        if self.use_k1:
            if k1_engine is None:
                print("WARN: USE_K1_IP=1 但 k1_engine.py 不可导入 → "
                      "整网全部回退老 HLS 引擎", flush=True)
            else:
                try:
                    self.k1 = k1_engine.K1Engine(overlay, ip_name=k1_ip_name)
                    print("K1 IP: 已接入 %s" % self.k1.describe(), flush=True)
                    print("K1 IP: 路由条件 k=1/s=1/d=1/pad=0、HOUT==H/WOUT==W、"
                          "输入输出都是 C8、relu=%d、cin∈[8,%d] 且 %%8==0、"
                          "cout∈[1,%d]、shift∈[1,31]"
                          % (self.k1.relu_capable, self.k1.cin_max,
                             self.k1.cout_max), flush=True)
                    if not any((getattr(self.arena, "layout", None) or {}).values()):
                        print("WARN: 当前 C8_LAYOUT=0，而 K1 IP 是纯 C8 构建 "
                              "→ 所有层都会回退老引擎（先加 --c8 / "
                              "C8_LAYOUT=1 再开 USE_K1_IP）", flush=True)
                except Exception as exc:                   # noqa: BLE001
                    print("WARN: USE_K1_IP=1 但 K1 IP 接入失败（%r）→ "
                          "整网回退老 HLS 引擎" % (exc,), flush=True)
                    self.k1 = None

        # ---- 手写 k=3 / k=5 加速器路由（2026-10-27）----
        # 默认**开**（见类文档）；接入失败只警告，但**该核型的层会在 conv()
        # 里直接报错**（不回退 HLS）——新 bit 根本没有 HLS 可以退。
        self.use_k3 = (_env_default_on("USE_K3_IP") if use_k3 is None
                       else bool(use_k3))
        self.k3 = None
        self.k3_calls = 0
        self.k3_seconds = 0.0
        self.k3_stall_fill = 0
        self.k3_stall_mac = 0
        self.k3_skip_reasons = {}
        self.k3_skip_examples = {}
        self.use_k5 = (_env_default_on("USE_K5_IP") if use_k5 is None
                       else bool(use_k5))
        self.k5 = None
        self.k5_calls = 0
        self.k5_seconds = 0.0
        self.k5_stall_fill = 0
        self.k5_stall_mac = 0
        self.k5_skip_reasons = {}
        self.k5_skip_examples = {}

        if self.use_k3:
            if k3_engine is None:
                print("WARN: USE_K3_IP=1 但 k3_engine.py 不可导入 → 所有 k=3 层"
                      "会在 conv() 里直接报错（不会回退 HLS）", flush=True)
            else:
                try:
                    self.k3 = k3_engine.K3Engine(overlay, ip_name=k3_ip_name)
                    print("K3 IP: 已接入 %s" % self.k3.describe(), flush=True)
                    print("K3 IP: 路由条件 k=3、s∈{1,2}、d∈{1,2,3}、pad==d、"
                          "不能 s≠1 且 d≠1、H/W≤%d、cin∈[8,%d] 且 %%8==0、"
                          "cout∈[1,%d]、shift∈[0,31]、槽容量≤%d、C8 输入输出"
                          % (self.k3.hw_max, self.k3.cin_max, self.k3.cout_max,
                             self.k3.cap_b), flush=True)
                    if not any((getattr(self.arena, "layout", None) or {}).values()):
                        print("WARN: 当前 C8_LAYOUT=0，而 K3 IP 是纯 C8 构建 → "
                              "所有 k=3 层都会在 conv() 里报错（先 --c8 / "
                              "C8_LAYOUT=1）", flush=True)
                except Exception as exc:                   # noqa: BLE001
                    print("WARN: USE_K3_IP=1 但 K3 IP 接入失败（%r）→ 所有 k=3 "
                          "层会在 conv() 里直接报错（新 bit 没有 HLS 可退；"
                          "要显式接受 HLS 回退请设 USE_K3_IP=0）"
                          % (exc,), flush=True)
                    self.k3 = None
        else:
            print("WARN: USE_K3_IP=0：k=3 层会落到老 HLS 引擎。仅当该 bit 的 "
                  "HLS 仍支持 k=3 时才安全（新 BD 里 HLS 已被删除）", flush=True)

        if self.use_k5:
            if k5_engine is None:
                print("WARN: USE_K5_IP=1 但 k5_engine.py 不可导入 → 所有 k=5 层"
                      "会在 conv() 里直接报错（不会回退 HLS）", flush=True)
            else:
                try:
                    self.k5 = k5_engine.K5Engine(overlay, ip_name=k5_ip_name)
                    print("K5 IP: 已接入 %s" % self.k5.describe(), flush=True)
                    print("K5 IP: 路由条件 k=5、s=1、d=1、pad=2、relu=True、"
                          "cin=%d、cout=%d、H/W=%dx%d（**编译期特化**）、"
                          "shift∈[0,31]、C8 输入输出"
                          % (self.k5.ip_cin, self.k5.ip_cout, self.k5.ip_h,
                             self.k5.ip_w), flush=True)
                except Exception as exc:                   # noqa: BLE001
                    print("WARN: USE_K5_IP=1 但 K5 IP 接入失败（%r）→ 所有 k=5 "
                          "层会在 conv() 里直接报错（k5 IP 还没进 BD 时这是预期"
                          "的；要显式接受 HLS 回退请设 USE_K5_IP=0）"
                          % (exc,), flush=True)
                    self.k5 = None
        else:
            print("WARN: USE_K5_IP=0：k=5 层会落到老 HLS 引擎。新 bit 的 HLS 对 "
                  "k=5 明确返回 6，回退只会得到错误 —— 除非该 bit 是旧版", flush=True)

        # 新 bit（无 HLS）+ 默认 USE_K1_IP=0 ⇒ k=1 层没有引擎可接。这里提前
        # 说清楚，别等跑到第 2 层才报错（这是"默认值保持不变"的代价）。
        if self.unit is None and not self.use_k1:
            print("WARN: 本 bit 没有老 HLS 引擎，而 USE_K1_IP=0（默认关）⇒ "
                  "132 个 k=1 层会在 conv() 里直接报错。新 bit 请加 "
                  "--use-k1-ip / USE_K1_IP=1", flush=True)

        # 哪些融合出口真的可用（Runner 据此决定要不要规划对应链路）
        self.fuse_out_ok = bool(getattr(self.unit, "fuse_out_ok", False))
        self.fuse_sum_ok = bool(getattr(self.unit, "fuse_sum_ok", False))
        # 上板时由调用方设置：
        #   n_layers  —— 总共要跑多少个卷积（只用于进度）
        #   timeout_s —— 单次卷积的等待上限；None = 驱动按工作量估算
        self.timeout_s = None
        self.n_calls = 0
        self.n_layers = 0
        self._numpy_fb_noted = False   # numpy 兜底只提示一次
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

    # ---------------- K1 IP 路由（2026-10-23）----------------

    def _k1_route(self, op, c8_in, c8_out, out_lut=None, chsum=None):
        """本层该不该走 K1 IP；返回 ``(bool, reason)``（不碰硬件）。"""
        if not self.use_k1:
            return False, "USE_K1_IP 未启用"
        if self.k1 is None:
            return False, "K1 IP 未接入"
        e = self.index["layers"].get(op.get("weights_key"))
        return k1_route_check(op, e, c8_in, c8_out, out_lut=out_lut,
                              chsum=chsum,
                              relu_capable=self.k1.relu_capable,
                              cin_max=self.k1.cin_max,
                              cout_max=self.k1.cout_max)

    def _k3_route(self, op, c8_in, c8_out, out_lut=None, chsum=None):
        """本层该不该走 K3 IP；返回 ``(bool, reason)``（不碰硬件）。"""
        if not self.use_k3:
            return False, "USE_K3_IP 未启用（显式接受 HLS 回退）"
        if self.k3 is None:
            return False, "K3 IP 未接入（bit 里没有 k3_wrap 或接入失败）"
        e = self.index["layers"].get(op.get("weights_key"))
        return k3_route_check(op, e, c8_in, c8_out, out_lut=out_lut,
                              chsum=chsum,
                              relu_capable=self.k3.relu_capable,
                              cin_max=self.k3.cin_max,
                              cout_max=self.k3.cout_max,
                              hw_max=self.k3.hw_max,
                              cap_b=self.k3.cap_b)

    def _k5_route(self, op, c8_in, c8_out, out_lut=None, chsum=None):
        """本层该不该走 K5 IP；返回 ``(bool, reason)``（不碰硬件）。"""
        if not self.use_k5:
            return False, "USE_K5_IP 未启用（显式接受 HLS 回退）"
        if self.k5 is None:
            return False, "K5 IP 未接入（IP 还没进 BD，或 bit 里没有 k5_wrap）"
        e = self.index["layers"].get(op.get("weights_key"))
        return k5_route_check(op, e, c8_in, c8_out, out_lut=out_lut,
                              chsum=chsum,
                              ip_cin=self.k5.ip_cin, ip_cout=self.k5.ip_cout,
                              ip_h=self.k5.ip_h, ip_w=self.k5.ip_w)

    def _ip_enabled_for(self, k):
        """该核型的手写 IP 是否**被要求**承接（决定"路由失败"要不要抛错）。"""
        k = int(k)
        if k == 3:
            return bool(self.use_k3)
        if k == 5:
            return bool(self.use_k5)
        return False

    def _conv_route(self, op, c8_in, c8_out, out_lut=None, chsum=None):
        """本层走哪个引擎：返回 ``(tag, why)``，tag ∈ {'k1','k3','k5','hls'}。

        纯决策（不碰硬件、不抛错）。k=3/k=5 返回 ``hls`` 时，``conv()`` 会看
        ``_ip_enabled_for()``：IP 被要求承接却没接住 ⇒ **抛错**（不回退）。
        """
        k = int(op.get("k", 1) or 0)
        if k == 1:
            ok, why = self._k1_route(op, c8_in, c8_out, out_lut, chsum)
            return ("k1" if ok else "hls"), why
        if k == 3:
            ok, why = self._k3_route(op, c8_in, c8_out, out_lut, chsum)
            return ("k3" if ok else "hls"), why
        if k == 5:
            ok, why = self._k5_route(op, c8_in, c8_out, out_lut, chsum)
            return ("k5" if ok else "hls"), why
        return "hls", "k=%d 没有对应的手写 IP" % (k,)

    def _note_skip(self, tag, why, op):
        """记录一次"没走成手写 IP"；同一原因只打印第一条（避免刷屏）。

        ``tag`` ∈ {'k1','k3','k5'}：分别累加到 ``k1_skip_reasons`` /
        ``k3_skip_reasons`` / ``k5_skip_reasons``（统计字段与 k1 对齐）。
        """
        reasons = getattr(self, tag + "_skip_reasons")
        examples = getattr(self, tag + "_skip_examples")
        n = reasons.get(why, 0)
        reasons[why] = n + 1
        ex = examples.setdefault(why, [])
        desc = ("%s(H=%d,W=%d,cin=%d,cout=%d,relu=%s)"
                % (op["name"], op["h"], op["w"], op["cin"], op["cout"],
                   bool(op.get("relu", True))))
        if n == 0:
            print("WARN: %s IP 回退老引擎（%s）—— 例: %s；后续同类不再打印"
                  % (tag.upper(), why, desc), flush=True)
        if len(ex) < 3:
            ex.append(desc)

    def _k1_note_skip(self, why, op):
        """记录一次 K1 回退（保留 k1 时代的函数名/语义）。"""
        self._note_skip("k1", why, op)

    def k1_plan(self, ops):
        """跑整网前**预演** K1 路由（不碰硬件、不执行卷积）。"""
        return self._ip_plan("k1", ops)

    def k3_plan(self, ops):
        """跑整网前**预演** K3 路由（不碰硬件、不执行卷积）。"""
        return self._ip_plan("k3", ops)

    def k5_plan(self, ops):
        """跑整网前**预演** K5 路由（不碰硬件、不执行卷积）。"""
        return self._ip_plan("k5", ops)

    def _ip_plan(self, tag, ops):
        """单个手写 IP 的路由预演（``k1_plan`` / ``k3_plan`` / ``k5_plan`` 共用）。

        返回 ``dict``：``routed`` = 会走该 IP 的 op 列表；``reasons`` = 回退原因
        -> 层数；``examples`` = 原因 -> 例。注意：这里还不知道 Runner 的
        conv→cat / conv→eca 融合规划，所以是**上界**（开了融合会再少几层——
        不过 ``conv_fuse_ok()`` 已经把 k3/k5 层排除，融合不会改变它们的路由）。
        """
        from collections import Counter
        routed = []
        reasons = Counter()
        examples = {}
        n_conv = 0
        want_k = {"k1": 1, "k3": 3, "k5": 5}[tag]
        route_fn = {"k1": self._k1_route, "k3": self._k3_route,
                    "k5": self._k5_route}[tag]
        for op in ops:
            if op.get("op") != "conv":
                continue
            n_conv += 1
            if int(op.get("k", 0) or 0) != want_k:
                continue
            c8_in = (c8_layout.arena_is_c8(self.arena, op["in"][0])
                     if op.get("in") else False)
            c8_out = (c8_layout.arena_is_c8(self.arena, op["out"][0])
                      if op.get("out") else False)
            ok, why = route_fn(op, c8_in, c8_out)
            if ok:
                routed.append(op)
            else:
                reasons[why] += 1
                examples.setdefault(why, []).append(op["name"])
        return {"routed": routed, "reasons": dict(reasons),
                "examples": examples, "n_conv": n_conv}

    def ip_plan(self, ops):
        """整网路由预演（**三个手写 IP 一起**），返回一份可直接打印的结构::

            {"n_conv": 247,
             "by_k": {1: 132, 3: 114, 5: 1},
             "k1": {...k1_plan 的返回...},
             "k3": {...}, "k5": {...},
             "unroutable": [ (op, why, tag), ... ]}

        ``unroutable`` 的口径 = **本 bit 上没有任何引擎能接这一层**：

          · k=3 / k=5 且对应 IP 被要求承接（``use_k3/use_k5`` 为真）但路由失败
            ⇒ conv() 会抛错（不是回退）；
          · 任何核型路由失败、且**本 bit 没有老 HLS 引擎**（``self.unit is None``）
            ⇒ 也没有退路；
          · 反过来，若 HLS 存在，则"回退老引擎"不算 unroutable（只是变慢，
            且可能有 HLS 自己的错误码，例如 k=1 的 5 / k=5 的 6）。
        """
        from collections import Counter
        by_k = Counter()
        for op in ops:
            if op.get("op") == "conv":
                by_k[int(op.get("k", 0) or 0)] += 1
        plan = {"n_conv": sum(by_k.values()), "by_k": dict(by_k),
                "k1": self.k1_plan(ops), "k3": self.k3_plan(ops),
                "k5": self.k5_plan(ops), "unroutable": []}
        for op in ops:
            if op.get("op") != "conv":
                continue
            k = int(op.get("k", 0) or 0)
            tag = route_tag(op)
            c8_in = (c8_layout.arena_is_c8(self.arena, op["in"][0])
                     if op.get("in") else False)
            c8_out = (c8_layout.arena_is_c8(self.arena, op["out"][0])
                      if op.get("out") else False)
            if tag == "k1":
                ok, why = self._k1_route(op, c8_in, c8_out)
            elif tag == "k3":
                ok, why = self._k3_route(op, c8_in, c8_out)
            elif tag == "k5":
                ok, why = self._k5_route(op, c8_in, c8_out)
            else:
                ok, why = False, "k=%d 没有对应的手写 IP" % (k,)
            if ok:
                continue
            if tag in ("k3", "k5") and self._ip_enabled_for(k):
                plan["unroutable"].append((op, why, tag))
            elif self.unit is None:
                plan["unroutable"].append(
                    (op, why + "；且本 bit 没有老 HLS 引擎", tag or "hls"))
        return plan

    def _ip_summary(self, tag):
        """单个手写 IP 的跑后小结。

        k1 的措辞**保持整合前的原文**（``test_k1_dispatch.py`` 按字面断言
        "执行 N 层"/"回退老引擎"/"未启用"）。
        """
        use = {"k1": self.use_k1, "k3": self.use_k3,
               "k5": self.use_k5}[tag]
        eng = {"k1": self.k1, "k3": self.k3, "k5": self.k5}[tag]
        calls = {"k1": self.k1_calls, "k3": self.k3_calls,
                 "k5": self.k5_calls}[tag]
        secs = {"k1": self.k1_seconds, "k3": self.k3_seconds,
                "k5": self.k5_seconds}[tag]
        reasons = getattr(self, tag + "_skip_reasons")
        examples = getattr(self, tag + "_skip_examples")
        name = tag.upper()
        if not use:
            return ("%s IP: 未启用（%s=0，该核型的层走老 HLS 引擎）"
                    % (name, "USE_" + name + "_IP"))
        if eng is None:
            return ("%s IP: 已请求但接入失败（该核型的层会在 conv() 里报错，"
                    "不会回退 HLS）" % name)
        n_skip = sum(reasons.values())
        lines = ["%s IP: 执行 %d 层（%s 累计 %.2fs），回退老引擎 %d 层"
                 % (name, calls, name, secs, n_skip)]
        if tag in ("k3", "k5"):
            sf = getattr(self, tag + "_stall_fill")
            sm = getattr(self, tag + "_stall_mac")
            lines.append("  stall: FILL=%d 拍（DDR 供数），MAC=%d 拍（应≈0）"
                         % (sf, sm))
        for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            ex = examples.get(why) or ["-"]
            lines.append("  - %-56s %3d 层  例: %s" % (why, n, ex[0]))
        if calls == 0 and n_skip:
            lines.append("WARN: 一层都没走 %s IP —— 检查 C8_LAYOUT/开关/权重容器，"
                         "不要以为已经提速" % name)
        return "\n".join(lines)

    def k1_summary(self):
        """一次整网跑完后的 K1 IP 路由小结（board 脚本打印用）。"""
        return self._ip_summary("k1")

    def k3_summary(self):
        """一次整网跑完后的 K3 IP 路由小结（含 stall 分类）。"""
        return self._ip_summary("k3")

    def k5_summary(self):
        """一次整网跑完后的 K5 IP 路由小结（含 stall 分类）。"""
        return self._ip_summary("k5")

    def ip_summary(self, plan=None):
        """三个手写 IP 的总路由小结（board 脚本打印用）。

        ``plan`` 给 ``ip_plan()`` 的结果时会额外打印"无 IP 可用"的层清单和
        "新 bit 必须同时开 USE_K1_IP" 的提示。不给就只打执行统计。
        """
        lines = [self._ip_summary("k1"), self._ip_summary("k3"),
                 self._ip_summary("k5")]
        if plan is not None:
            n_un = len(plan.get("unroutable") or [])
            if n_un:
                lines.append("WARN: %d 个卷积**没有任何 IP 能接**（整网会在这里"
                             "停下）：" % n_un)
                for op, why, tag in plan["unroutable"][:12]:
                    lines.append("  - %-46s k=%s  %s"
                                 % (op["name"], op.get("k"), why))
                if n_un > 12:
                    lines.append("  …（另有 %d 层，见 tools/ip_route_coverage.py）"
                                 % (n_un - 12))
        if (self.unit is None and self.k1 is not None
                and not self.use_k1):
            lines.append("提示: 本 bit 没有老 HLS 引擎，而 USE_K1_IP=0 —— "
                         "k=1 层会全部报错；新 bit 请同时设 USE_K1_IP=1")
        return "\n".join(lines)

    def _raise_no_ip(self, op, why):
        """k=3/k=5 被要求由手写 IP 承接、却接不住 ⇒ **硬错**（不回退 HLS）。"""
        k = int(op.get("k", 0) or 0)
        name = "K3" if k == 3 else "K5"
        print("ERROR: %s IP 无法承接 layer=%s\n"
              "       H=%d W=%d cin=%d cout=%d k=%d s=%d d=%d pad=%d relu=%s\n"
              "       原因: %s\n"
              "       **不会回退 HLS**：新 BD 已删除 HLS 引擎，HLS 源码对 k=1/k=5 "
              "也只返回错误码（5/6）。\n"
              "       下一步: ① 修这条路由原因（IP 是否接入 / 权重容器 / C8 / 形状）；"
              "或 ② 显式 USE_%s_IP=0 —— 那等于声明'本 bit 的 HLS 能算这一档'，"
              "只在旧 bit 上有意义。"
              % (name, op.get("name"), op["h"], op["w"], op["cin"], op["cout"],
                 op["k"], op["s"], op["d"], op["pad"],
                 bool(op.get("relu", True)), why, name), flush=True)
        raise RuntimeError(
            "%s IP 无法承接 layer=%s（k=%d, H=%d, W=%d, cin=%d, cout=%d）：%s"
            "；不会回退 HLS（新 bit 已删除 HLS 引擎，HLS 对这些核型也只返回错误码）"
            % (name, op.get("name"), k, op["h"], op["w"], op["cin"], op["cout"],
               why))

    def _run_new_ip(self, tag, op, in_t, out_t, e, c8_in, c8_out,
                    waddr, baddr):
        """把一层交给 k3/k5 手写 IP 并记账（失败时带诊断抛错，**不回退 HLS**）。"""
        eng = self.k3 if tag == "k3" else self.k5
        mod = k3_engine if tag == "k3" else k5_engine
        name = tag.upper()
        try:
            res = eng.run(
                self._ns(self.arena.addr(in_t)),
                self._ns(waddr), self._ns(baddr),
                self._ns(self.arena.addr(out_t)),
                op["h"], op["w"], op["cin"], op["cout"],
                op["k"], op["s"], op["d"], op["pad"],
                e["shift"], relu=bool(op["relu"]),
                c8_in=c8_in, c8_out=c8_out,
                timeout_s=self.timeout_s, layer=op["name"])
            rc, dt = res
        except Exception as exc:                           # noqa: BLE001
            st = getattr(exc, "status", None)
            rcode = getattr(exc, "return_code", None)
            try:
                stxt = mod.status_text(st) if st is not None else "-"
            except Exception:                              # noqa: BLE001
                stxt = "-"
            print("ERROR: %s IP 执行失败 layer=%s H=%d W=%d cin=%d cout=%d "
                  "k=%d s=%d d=%d pad=%d shift=%s relu=%s\n"
                  "       return_code=%r  STATUS=%s\n"
                  "       原因: %r\n"
                  "       下一步: 先看上面的 0xA8/0xAC/0xB0 与 STALL 诊断；"
                  "逐层 A/B 用 USE_%s_IP=0 对照（仅旧 bit 有 HLS 时）"
                  % (name, op["name"], op["h"], op["w"], op["cin"], op["cout"],
                     op["k"], op["s"], op["d"], op["pad"], e["shift"],
                     bool(op["relu"]), rcode, stxt, exc, name), flush=True)
            raise RuntimeError(
                "%s IP 执行失败（layer=%s, H=%d, W=%d, cin=%d, cout=%d, "
                "k=%d, s=%d, d=%d, pad=%d, return_code=%r, STATUS=%s）：%r"
                % (name, op["name"], op["h"], op["w"], op["cin"], op["cout"],
                   op["k"], op["s"], op["d"], op["pad"], rcode, stxt, exc)
            ) from exc
        # 记账（字段与 k1 对齐 + stall 分类）
        setattr(self, tag + "_calls", getattr(self, tag + "_calls") + 1)
        setattr(self, tag + "_seconds",
                getattr(self, tag + "_seconds") + float(dt))
        if tag in ("k3", "k5"):
            setattr(self, tag + "_stall_fill",
                    getattr(self, tag + "_stall_fill")
                    + int(getattr(res, "stall_fill", 0)))
            setattr(self, tag + "_stall_mac",
                    getattr(self, tag + "_stall_mac")
                    + int(getattr(res, "stall_mac", 0)))
        return rc

    def conv(self, in_t, out_t, op, out_lut=None, chsum=None):
        e = self.index["layers"][op["weights_key"]]
        waddr = int(self.wpool.device_address) + e["w_off"]
        baddr = int(self.wpool.device_address) + e["b_off"]
        self.n_calls += 1
        # 布局位（2026-10-23）：relu_en 的 bit2/bit3 = 输入/输出是 C8。
        # 逐张量判定（不能按层硬编码）：encoder.conv_first 是 12ch(CHW) 进 /
        # 48ch(C8) 出，decoder_1.out_conv 是 C8 进 / 12ch(CHW) 出。
        # C8_LAYOUT=0 时两个都是 False ⇒ 只写 bit0，与整合前逐位一致。
        c8_in = c8_layout.arena_is_c8(self.arena, in_t)
        c8_out = c8_layout.arena_is_c8(self.arena, out_t)

        # ---- 路由判定：k1 / k3 / k5 / 老 HLS（纯软件，不碰硬件）----
        # k=1：任何一条不满足 ⇒ 自动回退老引擎并打印一次警告（k1 的旧语义）。
        # k=3/k=5：IP 被要求承接却接不住 ⇒ **抛错**（新 bit 没有 HLS 可退，
        #          HLS 对这些核型也只会返回错误码）。
        k = int(op.get("k", 1) or 0)
        tag, why = self._conv_route(op, c8_in, c8_out,
                                   out_lut=out_lut, chsum=chsum)
        if tag == "hls" and self._ip_enabled_for(k):
            self._raise_no_ip(op, why)

        if self.progress_every and (self.n_calls % self.progress_every == 0):
            est_eng = {"k1": self.k1, "k3": self.k3, "k5": self.k5}.get(tag)
            if est_eng is not None:
                est = est_eng.estimate_seconds(op["h"], op["w"], op["cin"],
                                               op["cout"], op["k"], op["s"])
                ptag = tag.upper()
            elif self.unit is not None:
                est = self.unit.estimate_seconds(op["h"], op["w"], op["cin"],
                                                 op["cout"], op["k"], op["s"])
                ptag = "HLS"
            else:
                est, ptag = 0.0, "无引擎?!"
            print("  [%d/%d] %-34s H=%d W=%d cin=%d cout=%d k=%d %s 预计 %.2fs"
                  % (self.n_calls, self.n_layers or self.n_calls, op["name"],
                     op["h"], op["w"], op["cin"], op["cout"], op["k"], ptag, est),
                  flush=True)
        self.arena.flush()
        lut_buf = self._lut_buf(out_lut) if out_lut is not None else None
        if chsum is not None:
            # 调用方（Runner）已清零并 flush；这里再 flush 一次是廉价的保险：
            # RTL 是 `+=`，若 DDR 里还是旧值，累加结果就是错的。
            f = getattr(chsum, "flush", None)
            if f is not None:
                f()

        skip_noted = False
        if tag == "k1":
            try:
                rc, dt = self.k1.run(
                    self._ns(self.arena.addr(in_t)),
                    self._ns(waddr), self._ns(baddr),
                    self._ns(self.arena.addr(out_t)),
                    op["h"], op["w"], op["cin"], op["cout"],
                    op["k"], op["s"], op["d"], op["pad"],
                    e["shift"], relu=bool(op["relu"]),
                    c8_in=c8_in, c8_out=c8_out,
                    timeout_s=self.timeout_s, layer=op["name"])
                self.k1_calls += 1
                self.k1_seconds += float(dt)
            except k1_engine.K1Unsupported as exc:
                # 纯软件校验失败（**没有碰硬件**）⇒ 可以安全地改走老引擎。
                # 正常情况下路由判定已经挡住，这里只是防御"两处判定漂移"。
                # 但**缓冲区地址非法**（0 / >32 位）不是"换引擎就能好"的：
                # 老引擎用同样的地址只会错得更隐蔽，直接停下。
                if "缓冲区地址" in str(exc):
                    print("ERROR: K1 IP 预检发现非法缓冲区地址（layer=%s）：%r"
                          % (op["name"], exc), flush=True)
                    raise
                print("WARN: K1 IP 软件校验拒绝本层（layer=%s），自动回退老引擎：%r"
                      % (op["name"], exc), flush=True)
                self._k1_note_skip("K1Engine.run 软件校验拒绝（与路由判定不一致）",
                                   op)
                skip_noted = True
                tag = "hls"
            except Exception as exc:                       # noqa: BLE001
                # 硬件路径失败（超时/返回码非 0/AXI 异常）：**不再自动回退**。
                # 失败可能发生在 DMA 中途，PL 也许还在写 DDR；此时再启动老引擎
                # 会让两个 master 同时写同一块内存。宁可整网停下、带上下文退出。
                st = getattr(exc, "status", None)
                rcode = getattr(exc, "return_code", None)
                stxt = k1_engine.status_text(st) if st is not None else "-"
                print("ERROR: K1 IP 执行失败 layer=%s H=%d W=%d cin=%d cout=%d "
                      "k=%d s=%d d=%d pad=%d shift=%s relu=%s\n"
                      "       return_code=%r  STATUS=%s\n"
                      "       原因: %r\n"
                      "       下一步: 先 USE_K1_IP=0 回退整网确认结果，再查 K1 IP"
                      % (op["name"], op["h"], op["w"], op["cin"], op["cout"],
                         op["k"], op["s"], op["d"], op["pad"], e["shift"],
                         bool(op["relu"]), rcode, stxt, exc), flush=True)
                raise RuntimeError(
                    "K1 IP 执行失败（layer=%s, H=%d, W=%d, cin=%d, cout=%d, "
                    "return_code=%r, STATUS=%s）：%r"
                    % (op["name"], op["h"], op["w"], op["cin"], op["cout"],
                       rcode, stxt, exc)) from exc
        elif tag in ("k3", "k5"):
            rc = self._run_new_ip(tag, op, in_t, out_t, e, c8_in, c8_out,
                                  waddr, baddr)

        if tag == "hls":
            if self.use_k1 and self.k1 is not None and not skip_noted and k == 1:
                self._k1_note_skip(why, op)
            # k=3/k=5 走到这里只有一种可能：操作者显式 USE_K3_IP=0/USE_K5_IP=0
            # （否则上面已经抛错）。照样记账，便于 A/B 时看清"到底几层落在 HLS"。
            if k in (3, 5) and not self._ip_enabled_for(k):
                self._note_skip("k3" if k == 3 else "k5", why, op)
            if self.unit is None:
                # ---- numpy 兜底（2026-10-28）--------------------------------
                # 新 bit 已删掉老 HLS 引擎。若某层被**显式禁用**手写 IP
                # （USE_K5_IP=0 之类）或逐层不可路由，就一个引擎都没有。
                # 这里用 ref_numpy 的参考实现兜住，目的是**把 IP 的问题隔离
                # 出来**：整网能跑出正确结果，再单独查那个 IP。
                # 代价是这一层走 ARM（k=5 的单层约几秒），只适合少量层。
                _src = np.asarray(self.arena.t[in_t])
                _chw = c8_layout.to_chw(_src, c8_in, int(op["cin"]))
                _w = np.frombuffer(self.blob, np.int8, int(e["w_len"]),
                                   int(e["w_off"])).reshape(e["w_shape"])
                _b = np.frombuffer(self.blob, np.int32,
                                   int(e["b_len"]) // 4, int(e["b_off"]))
                _res = ref_numpy.conv_ref_numpy(
                    np.asarray(_chw, dtype=np.int8).reshape(
                        int(op["cin"]), int(op["h"]), int(op["w"])),
                    _w, _b, k, int(op["s"]), int(op["d"]), int(op["pad"]),
                    int(e["shift"]), relu=bool(op["relu"]),
                    w_layout=e.get("w_layout", "oc_ic_ky_kx"),
                    w_tap=e.get("tap"))
                self.arena.t[out_t][...] = c8_layout.from_chw(_res, c8_out)
                self.arena.flush()
                if not self._numpy_fb_noted:
                    self._numpy_fb_noted = True
                    print("WARN: layer=%s 走 numpy 兜底（%s）—— 结果正确但很慢，"
                          "仅用于把 IP 问题隔离出来" % (op["name"], why), flush=True)
                return
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

    # ---------------- 融合出口能力（Runner 规划用）----------------

    def conv_fuse_ok(self, op, out_lut=False, chsum=False):
        """本层**会不会**被手写 IP 接走（从而不能用融合出口）。

        k1/k3/k5 的 wrapper 都**没有** out_lut/chsum 寄存器，所以一层只要
        路由到它们，就不能参与 ``conv→cat`` / ``conv→eca`` 融合规划。
        Runner 用这个钩子把这类层排除在融合之外（它们照旧按自己的标度写回，
        由 cat/ECA 做重定标 —— 数值**逐位不变**，只是少省一次扫描）。

        为什么只按"核型 + 开关"判、不按逐层能力判：融合规划发生在**跑之前**，
        这时还没有运行时错误可看；而且 k3/k5 若真的逐层不可路由，``conv()``
        会直接报错，不存在"悄悄多跑一层 HLS"的情况。k=1 保持旧行为（返回
        True）—— 因为 k=1 层的融合本来就该由 HLS 做（k1 IP 没有该出口，
        这些层本来就回退 HLS），改了会动已验证的 k1 路径。
        """
        del out_lut, chsum
        k = int(op.get("k", 1) or 0)
        return not self._ip_enabled_for(k)


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
        self._plans = {}        # 同一对标的“怎么重定标”计划缓存（affine / lut）
        self._fast_on = (i8_fast is not None and i8_fast.available())
        self.n_fast_calls = 0   # 诊断：走了多少次 C 内核
        # 一次性把所有 (s_from,s_to) 的"怎么重定标"批量算好（Q15 拟合 + 256 项
        # 逐项验证）。不预热的话每一对第一次用到时要 4.2 ms，整网 399 对
        # ⇒ 1.7 s 的一次性开销（实测 cat 3.6 s 里有 1.7 s 是它）。
        if self._fast_on:
            self.plan_stat = lut_util.warm_plans(
                _rescale_pairs(self.m, self.scales, self.alias))
        else:
            self.plan_stat = {}
        self._plans = lut_util._PLAN_CACHE if self._fast_on else {}
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
        # ---- SAM 块（cat→eca→sam_sum）三算子融合（C8 专用，2026-10-27）----
        # 见 _plan_sam_block：t84 = cat(y0,y2,y4) 只喂 eca 的通道均值，而均值
        # 只依赖"每段的 LUT 通道和" ⇒ 整块不必物化，sam_sum 复用同一批暂存。
        self._sam_fuse, self._sam_skip, self._handoff_plan = self._plan_sam_block()
        self._sam_staged = {}    # sam_sum op id -> ops.sam_stage() 的 token（运行时）
        self._handoff_live = {}  # 张量名 -> 私有交接缓冲（运行时，仅本轮有效）
        self._sam_fuse_n = 0     # 诊断：本轮实际走了几次融合
        self._sam_handoff_n = 0  # 诊断：本轮有几路走了交接缓冲

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

    def _plan(self, s_from, s_to):
        """重定标计划：('affine', A, S, C) 或 ('lut', 256 字节表)。

        affine 与查表**逐位等价**（`lut_util.fit_affine` 对全部 256 个 int8
        逐项验证过才返回），只是快得多：查表在 A9 上要 8 次 vtbl+选择，
        实测 62 MB/s（算力受限），仿射路线直接跑满内存带宽。
        """
        key = (s_from, s_to)
        p = self._plans.get(key)
        if p is None:
            p = lut_util.rescale_plan(s_from, s_to)
            self._plans[key] = p
        return p

    def _req(self, t, s_from, s_to):
        """把 int8 张量从 s_from 标度重定标到 s_to（相等时为恒等），返回新数组。

        2026-10-21：由"numpy 花式索引查表"改成 `bytes.translate` 快数倍；
        2026-10-23（第六轮）：板端实测 translate 只有 69 MB/s、整条 `_req`
        42.8 MB/s，而同一块内存 memcpy 是 183 MB/s ⇒ 改成 C 内核：
           · affine：y = sat8((x*A+C)>>S)（逐位等价，264 MB/s 量级）
           · 兜底：256 项表查表（同样逐位等价）
        非 ARM / 无 cc 时静默回落原 numpy 路径。
        """
        if s_from == s_to:
            return t
        src = np.ascontiguousarray(t)
        out = np.empty(src.shape, np.int8)
        if self._fast_on and src.flags.c_contiguous:
            self._req_into_buf(out, src, s_from, s_to)
            return out
        tbl = self._lut(s_from, s_to)
        return np.frombuffer(src.tobytes().translate(tbl),
                             dtype=np.int8).reshape(src.shape)

    def _req_into_buf(self, dst, src, s_from, s_to):
        """把 src（C 连续）重定标写进 dst（C 连续、同长）。内核不可用时抛错。"""
        plan = self._plan(s_from, s_to)
        if plan[0] == "q15":
            i8_fast.q15_into(dst, src, plan[1], plan[2])
        elif plan[0] == "affine":
            i8_fast.scale_into(dst, src, plan[1], plan[2], plan[3])
        else:
            i8_fast.lut_into(dst, src, np.frombuffer(plan[1], dtype=np.int8))
        self.n_fast_calls += 1

    def _req_into(self, dst, src, s_from, s_to):
        """重定标并**写进已存在的 dst**（cat 的 copy 段用，省一次临时数组）。

        要求 dst/src 都是 C 连续且元素数相同；否则退回 `dst[...] = _req(...)`。
        """
        if s_from == s_to:
            if self._fast_on and dst.flags.c_contiguous and src.flags.c_contiguous:
                i8_fast.copy_into(dst, src)
                self.n_fast_calls += 1
            else:
                dst[...] = src
            return
        if (self._fast_on and dst.flags.c_contiguous
                and src.flags.c_contiguous and dst.size == src.size):
            self._req_into_buf(dst, src, s_from, s_to)
            return
        dst[...] = self._req(src, s_from, s_to)

    def _req_ip(self, v, s_from, s_to):
        """**原地**重定标（cat 的 direct 段用：数据已经在组缓冲的目标位置上）。"""
        if s_from == s_to:
            return
        if (self._fast_on and v.flags.c_contiguous and v.flags.writeable):
            plan = self._plan(s_from, s_to)
            if plan[0] == "q15":
                i8_fast.q15_ip(v, plan[1], plan[2])
            elif plan[0] == "affine":
                i8_fast.scale_ip(v, plan[1], plan[2], plan[3])
            else:
                i8_fast.lut_ip(v, np.frombuffer(plan[1], dtype=np.int8))
            self.n_fast_calls += 1
            return
        v[...] = self._req(v, s_from, s_to)

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
            # 会被手写 IP 接走的层没有融合出口（2026-10-27）：
            # k1/k3/k5 的 wrapper 都没有 out_lut 寄存器，规划时直接跳过它们。
            # 不跳过的话 conv() 的路由会因为"本层启用 out_lut"拒绝 k3/k5 层，
            # 而 k3/k5 的路由失败是**硬错**（不回退 HLS）⇒ 整网停在这里。
            if not self._engine_fuse_ok(op, out_lut=True):
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

    def _engine_fuse_ok(self, op, out_lut=False, chsum=False):
        """本层能不能用融合出口（引擎说了算；没有该钩子的引擎一律"能"）。

        ``PYNQEngine`` 会对手写 IP 接走的层（k=3/k=5 且开关打开）返回 False，
        因为这些 wrapper 没有 out_lut/chsum 寄存器。这样规划阶段就不会给它们
        挂融合，运行时它们照旧走自己的 IP、按自己的标度写回，由 cat/ECA 重定标
        （数值逐位不变）。
        """
        fn = getattr(self.engine, "conv_fuse_ok", None)
        if fn is None:
            return True
        try:
            return bool(fn(op, out_lut=out_lut, chsum=chsum))
        except TypeError:                  # 老引擎的签名没有关键字参数
            return bool(fn(op))

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
            # 同 _plan_fuse：手写 IP 没有 chsum 出口，规划时跳过（否则 k3/k5
            # 的路由会以"本层启用 chsum"失败 —— 而那是硬错）。
            if not self._engine_fuse_ok(op, chsum=True):
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

    def _plan_sam_block(self):
        """规划 SAM 块的 cat→eca→sam_sum **三算子融合**（C8 专用，默认开）。

        本网络的 6 个 SAM 块都是同一个拓扑（`tools/build_manifest.py` 的 sam()）::

            t84  = cat(y0, y2, y4)          # 唯一消费者是 eca
            attn = eca(t84)                 # 唯一消费者是 sam_sum
            out  = sam_sum(y0, y2, y4, attn, x)

        t84 存在的唯一目的是给 eca 提供**逐通道空间均值**，而均值只依赖通道和：
            mean_c = (Σ_{y,x} LUT_{s(seg)→s(t84)}(v)) × s(t84) / (H·W)
        LUT 是逐字节映射 ⇒ 这组和在**每段自己的数据上**就能精确累出来
        （`_sam_lut_chsum` / `ops.chsum_lut`：查表映射 + 整型累加，无舍入），
        根本不必把 14.7 MB 的 t84 真拼出来再整块扫一遍。

        融合后（每块省 3 趟整块搬运）：
          · cat **整条跳过** —— t84 一次都不写；
          · eca 只做"3 段搬进私有缓冲 + 按段求 LUT 通道和"（非缓存池上少 2 趟）；
          · sam_sum 用 `pre_staged` 复用同一批暂存 ⇒ 它的 5 处 copyto 只剩 2 处
            （x 进、out 出，这两处在非缓存 CMA 上是必需的，见 ops_numpy）；
          · 若某段的唯一消费者就是本 sam_sum 且它是 interp 的产物，则让那个
            interp **直接算进交接缓冲**（连"写回池 + 从池读回"都省掉）。

        硬条件（任一条不满足 ⇒ 整块不融合，行为逐位不变）：
          ① cat/eca/sam_sum 的 op id 连续（cat.id+1 == eca.id == sam_sum.id-1）；
          ② t84 的消费者恰好只有这个 eca；attn 的消费者恰好只有这个 sam_sum；
          ③ sam_sum 的 y0/y2/y4 依次就是 cat 的三个输入、attn 是第 4 个；
          ④ 全是 C8 int8 视图，段的逻辑通道数都对齐 8、总通道数 == t84 的；
          ⑤ 没开 conv 融合出口（FUSE_OUT 会让生产者直接在 cat 目标标度上写池，
             与"按段自身标度求 LUT 和"冲突）；record/hashlog 调试模式在 run()
             里另有一道闸（它们要 t84 的真值）。

        返回 (plan, skip, hand)：
            plan[eca_op_id] = {cat, sam, segs, out, attn}
            skip            = 要跳过的 cat op id 集合
            hand            = 张量名 -> 块内槽位号（0/1/2），可改写成交接缓冲
        """
        plan, skip, hand = {}, set(), {}
        if not self.c8:
            return plan, skip, hand
        if not _env_default_on("SAM_BLOCK_FUSE"):
            return plan, skip, hand
        prod, cons = self._prod_cons_map()
        by_id = {op["id"]: op for op in self.ops}
        bufs = self.m["buffers"]
        for op in self.ops:
            if op["op"] != "eca":
                continue
            cat = by_id.get(op["id"] - 1)
            ss = by_id.get(op["id"] + 1)
            if cat is None or ss is None:
                continue
            if cat["op"] != "cat" or ss["op"] != "sam_sum":
                continue
            if len(cat["out"]) != 1 or len(op["out"]) != 1:
                continue
            t84, attn = cat["out"][0], op["out"][0]
            segs, ins = list(cat["in"]), list(ss["in"])
            if len(segs) != 3 or len(ins) != 5 or ins[:3] != segs or ins[3] != attn:
                continue
            if len(cons.get(t84, [])) != 1 or len(cons.get(attn, [])) != 1:
                continue
            if not all(self._is_c8(t) for t in segs + [t84]):
                continue
            if any(bufs[t]["dtype"] != "int8" for t in segs + [t84]):
                continue
            if any(int(bufs[t]["c"]) % c8_layout.C8 for t in segs):
                continue
            if sum(int(bufs[t]["c"]) for t in segs) != int(bufs[t84]["c"]):
                continue
            # 段不能是 conv 融合出口的写回目标：那种情况下池里存的是**目标标度**
            # 的值（conv 直接把 LUT 应用在写回上），这里再套一次 LUT 就错了。
            # 判据与 _plan_fuse 同源：生产者是 conv 且唯一消费者就是这个 cat。
            # 本网络的 SAM 段都还有 sam_sum 这个第二消费者 ⇒ 恒不触发。
            if any(prod.get(t, {}).get("op") == "conv" and len(cons.get(t, [])) == 1
                   for t in segs):
                continue
            plan[op["id"]] = {"cat": cat["id"], "sam": ss["id"], "segs": segs,
                              "out": t84, "attn": attn}
            skip.add(cat["id"])
        # 交接缓冲：段的消费者只有"被跳过的 cat + 本 sam_sum"，且生产者是 interp。
        # 值 = 块内槽位号（0/1/2）：6 个块在时间上不重叠，所以槽位可以跨块复用
        # ⇒ 只需要"每块最多 3 块缓冲"，而不是"每个张量一块"（省 ~21 MB）。
        for _eid, p in plan.items():
            for slot, t in enumerate(p["segs"]):
                cs = cons.get(t, [])
                if sorted(c["id"] for c in cs) != sorted((p["cat"], p["sam"])):
                    continue
                pr = prod.get(t)
                if pr is None or pr["op"] != "interp":
                    continue
                hand[t] = slot
        return plan, skip, hand

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

    def _sam_lut_chsum(self, tok, segs, s_target, nc):
        """SAM 块融合用：Σ_pixels LUT_{s(seg)→s(target)}(v) 的逐通道 int64 精确和。

        与 ``chsum_c8(cat(segs))`` 的整数和**逐位相同**（LUT 是逐字节映射、
        求和是整数运算，没有舍入），所以 ECA 的 mean/attn 完全不变。

        快路径：用 `_req_into_buf`（i8_fast 的仿射/查表内核，板端 264 MB/s）
        把每段映射到私有 scratch，再用 `chsum_private`（同一个 C 内核，但
        跳过池→私有那趟拷贝）。整条链路都已在私有缓冲里，池上只读一次。
        标度相同的段直接跳过映射（映射是恒等）。i8_fast 不可用时回落到纯
        numpy 的 `ops.chsum_lut`。
        """
        arrays, shapes = tok["arrays"], tok["shapes"]
        if not self._fast_on:
            luts = [self._lut(self._s(t), s_target) for t in segs]
            return ops.chsum_lut(arrays, shapes, luts, nc)
        scratch = ops.sam_scratch(tok["n"])
        sums = np.empty(int(nc), np.int64)
        c0 = 0
        for t, arr, shape in zip(segs, arrays, shapes):
            nseg = int(arr.size)
            sv = self._s(t)
            if sv == s_target:
                buf = np.asarray(arr).reshape(shape)          # 恒等映射
            else:
                self._req_into_buf(scratch[:nseg], arr, sv, s_target)
                buf = scratch[:nseg].reshape(shape)
            k = int(buf.shape[0]) * c8_layout.C8
            sums[c0:c0 + k] = ops.chsum_private(buf, k)
            c0 += k
        return sums

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
    def _sam_sum_c8(out, y0, y2, y4, attn, x, s0, s2, s4, sx, s_out, nc, c8=None,
                    pre_staged=None):
        """sam_sum 的 C8 原地实现（纯逐元素，与 CHW 路径逐位相同）。

        out/y0/y2/y4/x 都是 (C/8,H,W,8) 视图；attn 永远是 fp32 CHW 的 (3C,1,1)。
        10 维广播的坑：权重必须按 ``(块, 1, 1, 8)`` 广播（**最后那一维是块内
        通道 8**，不是新插的一维）——写成 ``[:, None, None, None]`` 会把 8 顶到
        新轴上，得到 (块,块,H,W,8) 而报 broadcast 错（2026-10-23 踩过一次）。
        分块 = 2 个通道块（16 通道），与 CHW 路径的分块宽度一致。

        ``pre_staged``：SAM 块融合的暂存 token（y0/y2/y4 已在私有缓冲里，
        见 ``Runner._plan_sam_block``）。给了就跳过那 3 处 copyto。
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
                                s0, s2, s4, sx, s_out, pre_staged=pre_staged)
            return out
        except Exception:
            pass
        if pre_staged is not None:
            # 融合路径下 y 的**池副本可能是旧的**（上游 interp 直接算进了交接
            # 缓冲，池里那份根本没写）。回落到 numpy 循环前先把暂存内容写回池，
            # 否则会读到自己都没写过的脏数据。
            for src, t8 in zip(pre_staged["arrays"], (y0, y2, y4)):
                np.copyto(t8.reshape(-1), np.asarray(src).reshape(-1))
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
        # SAM 块融合本轮是否生效：record/hashlog 要逐算子的**真值**快照，
        # 而融合会把 t84 整块省掉 ⇒ 调试模式下自动关掉（行为回到旧路径）。
        sam_on = bool(self._sam_fuse) and self.record is None and self.hashlog is None
        if self._sam_fuse and not sam_on:
            print("INFO: record/hashlog 已开 → 本轮关闭 SAM 块融合（要 t84 真值）",
                  flush=True)
        if sam_on:
            print("INFO: SAM 块融合已启用：%d 块（cat 跳过、eca 按段求通道和、"
                  "sam_sum 复用暂存；交接缓冲 %d 路）"
                  % (len(self._sam_fuse), len(self._handoff_plan)), flush=True)
        self._sam_staged = {}
        self._handoff_live = {}
        self._sam_fuse_n = 0
        self._sam_handoff_n = 0
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
                    # C8 直算快路径（C 内核；逐位一致，非 1/8 网格/布局不符则回落）
                    # SAM 块融合：本 interp 的输出只喂融合后的 sam_sum ⇒ 直接算进
                    # 交接缓冲（私有），池里那份一次都不写。下游从这块读。
                    hb = None
                    if sam_on and o0 in self._handoff_plan:
                        # 按**块内槽位**取缓冲：块之间不重叠，槽位可复用
                        hb = ops.handoff_buf("sam_h%d" % self._handoff_plan[o0],
                                             A.view(o0).size)
                    done = False
                    try:
                        ops.interp_c8_into(A.view(o0), A.view(i0),
                                           op["scale_num"], op["scale_den"],
                                           handoff=hb)
                        done = True
                        if hb is not None:
                            self._handoff_live[o0] = hb
                            self._sam_handoff_n += 1
                    except Exception:
                        done = False
                        self._handoff_live.pop(o0, None)   # 回落：池里会是真值
                    if not done:
                        # interp 只动 H/W，但 ops.interp 按 3-D 写死 ⇒ 转 CHW 算再转回
                        A.view(o0)[...] = c8_layout.ops_c8().interp(
                            A.view(i0), self._lay(i0), op["scale_num"],
                            op["scale_den"], out_lay=self._lay(o0))
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
            elif k == "cat" and sam_on and op["id"] in self._sam_skip:
                # SAM 块融合：t84 的唯一消费者是 eca，而 eca 只要逐通道均值 ——
                # 融合路径已经按"每段自己的数据"精确算出来了，整块不必物化
                # （省 2 趟整块搬运：cat 的读+写）。
                pass
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
                            # 2026-10-23 第六轮：直接写进 dst（一次读写），
                            # 不再"造临时数组再整体赋值"（原来 3 遍内存）。
                            self._req_into(dst, A.view(n), sn, s_out)
                        elif sn != s_out:
                            # direct 段：生产者已经把数据写在这个位置上，
                            # 只需**原地**重定标（一次读写）。
                            self._req_ip(A.view(n), sn, s_out)
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
                    # 2026-10-23 第六轮：饱和相加改用 C 内核（VQADD.S8 一条指令），
                    # 原来是 numpy 的 int16 相加 + clip（5 遍内存 + 2 份 int16 临时）。
                    if (self._fast_on and a.flags.c_contiguous
                            and b.flags.c_contiguous):
                        out = A.view(o0)
                        i8_fast.add_into(out, a, b)
                    else:
                        A.view(o0)[...] = ops.add(a, b)
            elif k == "eca" and sam_on and op["id"] in self._sam_fuse:
                # SAM 块融合（见 _plan_sam_block）：cat 那一趟已经不写了，
                # 通道和直接在"3 段 + 各自的 LUT"上精确算 —— 与
                # `chsum_c8(cat(y0,y2,y4))` 的 int64 和**逐位相同**，
                # 但省掉 t84 的整块写 + 整块读，也省掉再搬一次 d 暂存。
                p = self._sam_fuse[op["id"]]
                e = self.eca[op["weights_key"]]
                t84 = p["out"]
                sc = self._s(t84)
                nc = self._logical_c(t84)
                pairs = []
                for t in p["segs"]:
                    hb = self._handoff_live.pop(t, None)
                    if hb is None:
                        pairs.append((A.view(t), False))
                    else:
                        # 上游 interp 直接算进了交接缓冲（池里那份是旧的）
                        pairs.append((hb.reshape(A.view(t).shape), True))
                tok = ops.sam_stage(pairs)
                sums = self._sam_lut_chsum(tok, p["segs"], sc, nc)
                hh, ww = c8_layout.hw(A.view(t84).shape, True)
                hw = float(hh * ww)
                mean_c = (sums.astype(np.float64) * sc / hw).astype(np.float32)
                A.view(o0)[...] = ops.eca_attn_from_mean(mean_c, e["conv2"], e["conv1"])
                self._sam_staged[p["sam"]] = tok
                self._sam_fuse_n += 1
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
                                     self._logical_c(o0),
                                     pre_staged=(self._sam_staged.pop(op["id"], None)
                                                 if sam_on else None))
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
