#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deps_closure.py —— 冻结点/交付包的「依赖闭包」：**静态生成 + 机械校验**（不 import 被测代码）。

## 为什么需要它（2026-11-02 事故）

`runs/board_ops/20261102/frozen_code/` 是按"我在跑什么"**手挑**的 7 个 `.py`，
漏了 `q8.*` / `c8deq.*` / `bpack.*`。`bin_engine` 对这三个 C/NEON 内核是
`try: import ... except Exception: None` 的**静默回落** ⇒ 同一个 `bin_net`、同一块板、
同一个 boot，跑出 prologue × 3.69、epilogue × 4.63、total 6.497 → 11.752 s 的假数字。

⇒ 冻结点不许再是"文件清单"，必须是**依赖闭包**；而且清单本身必须**可复算**：
   谁改了 import 图而没重生成清单，`--check` 必须报错。

## 两个判据（都必须是机械的）

1. `md5sum -c deps_closure.txt` **rc = 0**（文件齐 + 逐字节一致）
   —— 注意：GNU `md5sum -c` 对**缺失文件**是 rc=1（实测），对 `#` 注释行只是忽略，rc 仍 0。
2. `deps_closure.py --check` rc = 0（结构齐）
   —— 重扫源码 AST，若扫描出的本地依赖**不在**清单里（新增 import 没进闭包），
      或清单里的文件在源码树里已变（没重生成），一律 rc=1。
   —— 这一条堵的是"md5 对得上但根本不是同一份闭包"（例如清单只列了 7 个 .py）。

## 用法

    # 生成（默认写到 runs/board_ops/20261102/deps_closure.txt）
    python3 pynq_specialized_code/ps/tools/deps_closure.py --gen

    # 校验清单（默认对 pynq_specialized_code/ps/pynq）
    python3 pynq_specialized_code/ps/tools/deps_closure.py --check

    # 校验某个冻结点目录（板端 ps/pynq_frozen/ 或 frozen_code/）
    python3 pynq_specialized_code/ps/tools/deps_closure.py --check --dir <冻结点目录>
    #   ⇒ 额外做"文件是否真在这个目录内"的检查（防止又一次"其实 import 到了 ps/pynq"）

## 边界（诚实说明）

· 扫描是**静态**的：`import` 语句 + `ctypes.CDLL(` / `_fast_lib(` 加载点 + `<mod>.c` 字面量。
  它不执行被测代码，也不解析 `__import__(变量)` 这种动态导入（本项目没有）。
· 它只回答"**闭包齐不齐**"，不回答"内核装没装上"——
  后者由 `bin_engine.kernel_deps()`（运行时状态）与 `BIN_REQUIRE_KERNELS=1`（fail-fast）负责。
  两者互补：清单管"文件在不在"，运行时管"装没装上"。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.dirname(HERE)                       # .../ps
PYNQ_DEFAULT = os.path.join(PS, "pynq")
ROOT_DEFAULT = os.path.dirname(os.path.dirname(PS))   # 仓库根
OUT_DEFAULT = os.path.join(ROOT_DEFAULT, "runs", "board_ops", "20261102",
                           "deps_closure.txt")

# ============================================================================
# 1. 闭包根 + 声明表
# ============================================================================
# 根 = 任务书指定的三个（整网驱动 / 打包驱动 / numpy 参考）。
# 其余全部由静态扫描**推出**，不许手挑 —— 这正是事故的教训。
ROOTS = ("bin_net", "bin_engine", "bin_ref_numpy")

# 声明表：**用途 / 热冷 / 需要的额外文件（.c）**。
# 扫描只负责"依赖关系"，语义（用途、是不是热路径）必须人写，否则无法审。
# 每一行的 key 必须能在扫描结果里找到；扫描找到的本地模块若这里没有 ⇒ 报错。
DECL = {
    "bin_net": dict(
        kind="热（每次整网必经）",
        role="整网调度：227 层 DAG + PS/PL 重叠流水 + CLI",
        src=None),
    "bin_engine": dict(
        kind="热（每次整网必经）",
        role="PL 驱动 + 惰性内核装配（`_q8()/_deq_kernel()/_bpack()` 三个回落点都在这里）",
        src=None),
    "bin_ref_numpy": dict(
        kind="热（每次整网必经）",
        role="numpy 参考内核 + Blob 权重容器 + conv_rtl（主机 FakePL 的算术基线）",
        src=None),
    "q8": dict(
        kind="热（prologue；缺 ⇒ prologue ×3.69）",
        role="f32→int8 量化 + 一趟写进 C8 交错缓冲（NEON）",
        src="q8.c"),
    "c8deq": dict(
        kind="热（epilogue；缺 ⇒ epilogue ×4.63）",
        role="C8 int8 → f32 一趟 burst 反量化（非缓存缓冲上 numpy 跨步视图的替代）",
        src="c8deq.c"),
    "bpack": dict(
        kind="热（prologue 小头；缺 ⇒ +0.45 s）",
        role="f32 → uint64 二值化打包（按 64 通道字）",
        src="bpack.c"),
    "bilinear_c": dict(
        kind="热/opt-in（interp；`BIN_BILINEAR_C=1` 才加载 .so，默认关）",
        role="双线性插值 C 内核（逐字节等价 numpy 版）",
        src="bilinear_c.c"),
    # ★ 2026-11-02（T-41）：T-17 的 `convpf_c` 随 `613a117` 合进 main、被
    #   `bin_ref_numpy.py:859/883`（`try: import convpf_c`，静默回落）依赖，
    #   但 DECL 一直没补 ⇒ `--gen` 直接拒绝生成、`--check` 在 main `b84232c`
    #   上**物理上不可能** rc=0（T-39 板端实测，runs/board_t39/REPORT.md §1.1）。
    #   ⇒ 这是"门禁坏掉"，不是"清单过期"。
    "convpf_c": dict(
        kind="热/opt-in（`BIN_CONVPF_C=1` 且在 ARM 上才启用；缺 ⇒ 静默回落 numpy im2col，结果等价）",
        role="im2col-free 直接卷积 C/NEON 内核（逐字节等价比串行标量参考；T-17）",
        src="convpf_c.c"),
    # ⚠ `convpf_neon_shim.h` **故意不进清单**（不是漏了）：
    #   它只在 `-DCPF_FAKE_NEON` 的**主机测试构建**里被 include（`convpf_c.c` 的
    #   `#if defined(CPF_FAKE_NEON)` 分支，引擎 `ps/tools/convpf_neon_sim.py`），
    #   板端/主机**正式**构建走 `#include <arm_neon.h>` 或标量分支，从不 include 它。
    #   板端 `cc -O3 ... -o convpf_c_armv7l.so convpf_c.c` 只喂一个 .c ⇒ 头文件不在
    #   编译输入里；把它列进 md5 清单只会让"冻结点少拷一个测试头"变成假 FAIL
    #   （判据只该管**板端跑得起来**所需的文件，见本文件 §2 的定位）。
    # ★ 2026-11-02（T-44）：T-43 把 T-23 的改动合进 main（`b2cfd23`），带进了
    #   C 驱动 `mmio_batch`（`bin_engine.py:878` `try: import mmio_batch`，静默回落），
    #   但没同步 DECL、也没重生成清单 ⇒ `md5sum -c` 与 `--check` **两条判据同时挂**。
    #   与 T-41 补 `convpf_c` 是**同一个坑**：合并不代入"重生成闭包清单"这一步
    #   ⇒ 判据坏掉。已由 `ps/tools/merge_gate.sh` 变成机械检查（见 PLAN_1second 第 82 条）。
    "mmio_batch": dict(
        kind="热/opt-in（`BIN_MMIO_C=1` 才启用；**默认关**。缺 ⇒ 静默回落 Python "
             "MMIO 直写路径，结果逐字节等价，只是每次写多一次 struct.pack）",
        role="一批 MMIO 写 + start + 等 done 收进**一次 C 调用**（T-23 第 2 级；"
             "首次使用时 `cc -O2` 现场编译成 mmio_batch_<arch>.so）",
        src="mmio_batch.c"),
    # ⚠ `mmio_batch.c` **没有本地头文件**：`#include` 只有 `<stdint.h>` 与 `<time.h>`
    #   （系统头，不随包）。板端/主机每次都是 `cc -O2 ... -o mmio_batch_<arch>.so
    #   mmio_batch.c` **单文件编译** ⇒ 编译输入只有这一个 .c，清单里就只列它。
    #   判断标准同 T-41 的 `convpf_neon_shim.h`：**只有参与编译输入的文件才进清单**，
    #   把只用于测试的 .h 列进来只会制造"冻结点少拷一个头"的假 FAIL。
    # ★ 2026-11-02（v50 合并 / REVIEW）：IMP-C 的 `bin_net.py`（`_fconv_engine()`，
    #   `try: import fconv`，静默回落）让闭包多出 `fconv` 一个模块，而 `ps/tools`
    #   不在 IMP-C 的可写范围 ⇒ 合并时必须由合并方补这一行。这是**第三次**同一个
    #   坑（前两次 `convpf_c` / `mmio_batch`，见上两段注释），现已由
    #   `ps/tools/merge_gate.sh` 机械兜住：漏写 ⇒ `--gen` / `--check` 当场 rc=1。
    #   与 `convpf_c` 的分工：`convpf_c` 是 CPU 侧 im2col-free 内核，
    #   本项是 **PL** 单元 `fconv_0` 的驱动，两者互补、互不替代。
    #   `fconv.py` 是**纯 Python**（无 `.c`、无 `CDLL`）⇒ `src=None`。
    "fconv": dict(
        kind="热/opt-in（`BIN_CONVPF_PL=1` **且** overlay 里有 `fconv_0` 才启用；"
             "默认关。缺 ⇒ 静默回落 CPU convfp，结果逐字节等价）",
        role="convfp（f32 直接卷积）PL 单元的 PS 侧驱动：fconv_0 寄存器映射、"
             "物理地址四重校验、逐字节判据、尾部 0xA5 金丝雀（IMP-C 只调用不改它）",
        src=None),
    "qcache": dict(
        kind="冷/可选（`Q8CACHE=1`，默认关）",
        role="f32 叶子量化缓存（按 C8 字节常驻，省重复量化）",
        src=None),
    "e1e2_sim": dict(
        kind="冷（`pack_segments`/`write_int8`/`pack_int8` 的 C8 填充 + 自检参考）",
        role="E1E2 描述符/命令的寄存器级模型 + quantize_int8 参考语义",
        src=None),
    "catpack": dict(
        kind="冷（`CATPACK=1` 的 R_RUN_UNIT=5 后端，默认不挂）",
        role="cat 不物化直读引擎（CatPackEngine）",
        src=None),
    "board_safe": dict(
        kind="冷（可选看门狗；缺 ⇒ 只是没有看门狗，不报错）",
        role="PL 看门狗（长卷积超时停喂狗让板子复位）",
        src=None),
}

# 这三个是 2026-11-02 报告 §8.5 点名的**埋雷**（冷路径，当天没触发，但同样是闭包缺口）。
TRAPS = ("e1e2_sim", "catpack", "board_safe")

# 系统库（不随包、不需要闭包）：只登记，不入 md5 清单。
SYSLIBS = (
    ("libc.so.6", "bin_ref_numpy.py:_libc() → `malloc_trim(0)`（`BIN_MALLOPT`）"),
)

SO_POLICY = "板端 `cc -O3` 现场编译（`<mod>_<arch>.so`，编在模块自己目录），**不随包发布**"


# ============================================================================
# 2. 静态扫描
# ============================================================================
_SO_RE = re.compile(r"CDLL\(|LoadLibrary\(|_fast_lib\(")
_C_RE = re.compile(r"[\"']([A-Za-z_][A-Za-z0-9_]*\.c)[\"']")


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _local_modules(pynq_dir):
    return {f[:-3] for f in os.listdir(pynq_dir)
            if f.endswith(".py") and not f.startswith(".")}


def scan_one(path, local):
    """扫一个 .py：返回 (imports, so_points, c_files)。

    imports: [dict(module=, line=, stmt=, scope=, guarded=bool)]
    so_points: [(lineno, 源码行)] —— `ctypes.CDLL(` / `_fast_lib(` 加载点
    c_files: 该模块引用的 `<mod>.c` 字面量（排序去重）
    """
    src = open(path, encoding="utf-8").read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as ex:                       # 语法错就别装作扫到了
        raise SystemExit("✗ 解析失败 %s: %s" % (path, ex))
    imports, stack, tryd = [], [], [0]

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, n):
            stack.append(n.name)
            self.generic_visit(n)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Try(self, n):
            tryd[0] += 1
            self.generic_visit(n)
            tryd[0] -= 1

        def _add(self, mod, line, stmt):
            top = mod.split(".")[0]
            if top in local:
                imports.append(dict(module=top, line=line, stmt=stmt,
                                    scope=".".join(stack) or "-",
                                    guarded=bool(tryd[0])))

        def visit_Import(self, n):
            for a in n.names:
                self._add(a.name, n.lineno, "import " + a.name)

        def visit_ImportFrom(self, n):
            if n.level == 0 and n.module:
                self._add(n.module, n.lineno,
                          "from %s import %s"
                          % (n.module, ",".join(a.name for a in n.names)))

    V().visit(tree)
    so_points, c_lits = [], []
    for i, line in enumerate(src.splitlines(), 1):
        if _SO_RE.search(line):
            so_points.append((i, line.strip()))
        for m in _C_RE.findall(line):
            c_lits.append((i, m))
    return imports, so_points, sorted({cl[1] for cl in c_lits})


def build_closure(pynq_dir):
    """从 ROOTS 出发，静态推出整份闭包。返回 (modules, edges)。"""
    local = _local_modules(pynq_dir)
    seen, order, edges = set(), [], []
    queue = list(ROOTS)
    while queue:
        m = queue.pop(0)
        if m in seen:
            continue
        seen.add(m)
        order.append(m)
        p = os.path.join(pynq_dir, m + ".py")
        if not os.path.exists(p):
            raise SystemExit("✗ 闭包根缺文件：%s" % p)
        imps, _so, _cf = scan_one(p, local)
        for e in imps:
            e["path"] = m
            edges.append(e)
            if e["module"] not in seen and e["module"] not in queue:
                queue.append(e["module"])
    return order, edges


# ============================================================================
# 3. 生成
# ============================================================================
def render(pynq_dir, stamp=None):
    import datetime
    modules, edges = build_closure(pynq_dir)

    missing_decl = [m for m in modules if m not in DECL]
    if missing_decl:
        raise SystemExit(
            "✗ 闭包里有 DECL 未声明的模块：%s\n"
            "  ⇒ 请在 %s 的 DECL 里补一行（用途/热冷/是否需要 .c），再重生成。"
            % (missing_decl, os.path.basename(__file__)))
    roots_only = [m for m in DECL if m not in modules]
    if roots_only:
        raise SystemExit("✗ DECL 声明了但扫描不到依赖：%s" % roots_only)

    # 文件清单：模块自身 .py（+ 声明的 .c）
    files = []                                   # [(module, filename, kind)]
    for m in modules:
        files.append((m, m + ".py", "py"))
        src = DECL[m]["src"]
        if src:
            files.append((m, src, "c"))

    for _m, fn, _k in files:
        if not os.path.exists(os.path.join(pynq_dir, fn)):
            raise SystemExit("✗ 闭包缺文件：%s/%s" % (pynq_dir, fn))

    # imports 索引（谁被谁在哪一行加载）
    by_mod = {}
    for e in edges:
        by_mod.setdefault(e["module"], []).append(e)

    # 每个模块的 .so 加载点（native loader 才打印）
    nat = {}
    for m in modules:
        if DECL[m]["src"]:
            _i, so_pts, c_files = scan_one(os.path.join(pynq_dir, m + ".py"),
                                           _local_modules(pynq_dir))
            nat[m] = (so_pts, c_files)

    L = []
    A = L.append
    A("# =============================================================================")
    A("# deps_closure.txt —— 冻结点/交付包的【依赖闭包】（**生成物，不要手改**）")
    A("# =============================================================================")
    A("# 生成: python3 pynq_specialized_code/ps/tools/deps_closure.py --gen")
    try:
        src_disp = os.path.relpath(pynq_dir, ROOT_DEFAULT)
    except ValueError:
        src_disp = pynq_dir
    A("# 源根: %s（md5 与路径无关，冻结点目录里同名文件逐个比）" % src_disp)
    A("# 时间: %s" % (stamp or __import__("time").strftime("%Y-%m-%d %H:%M:%S")))
    A("# 规模: %d 个模块 / %d 个文件（%d 个 .py + %d 个 .c）"
      % (len(modules), len(files),
         sum(1 for f in files if f[2] == "py"), sum(1 for f in files if f[2] == "c")))
    A("#")
    A("# ── 校验（两条都要 rc=0；缺任何一条都不算「闭包完整」）─────────────────────")
    A("#   ① cd <目录> && md5sum -c deps_closure.txt")
    A("#      · 缺文件 ⇒ rc=1（实测；GNU md5sum 会报「没有那个文件」+「N 个列出的文件无法读取」）")
    A("#      · `#` 注释行被忽略，不影响 rc")
    A("#   ② python3 .../ps/tools/deps_closure.py --check --dir <目录>")
    A("#      · 重扫源码 AST：依赖图变了而清单没重生成 ⇒ rc=1（md5 只会告诉你文件变了，")
    A("#        不会告诉你「清单本身不完整」——事故里清单少 6 个文件时 md5 也只能对已列的 7 个负责）")
    A("#      · --dir 同时检查「每个模块都能在该目录内解析到」（防「其实 import 到了 ps/pynq」）")
    A("#")
    A("# ── 为什么是闭包，不是文件清单（2026-11-02 事故）────────────────────────────")
    A("#   frozen_code/ 手挑了 7 个 .py，漏 q8/c8deq/bpack 三对 .py/.c ⇒ bin_engine 静默")
    A("#   回落 numpy ⇒ prologue 5.948→1.611 s（×3.69）、epilogue 1.189→0.258 s（×4.61）、")
    A("#   total 11.752→6.497 s。代码一字节没变，**只是依赖缺了**，就测了两天假数。")
    A("#   ⇒ 判据从此是：① 这份清单 md5sum -c 全过；② 运行时 kernel_deps 全 fast。缺一不可。")
    A("#")
    A("# ── 闭包表 ──────────────────────────────────────────────────────────────────")
    A("# 模块          | 热/冷 | 用途 / 缺了的代价 | 需要文件 | .so | 加载点（file:line）")
    for m in modules:
        d = DECL[m]
        need = m + ".py" + ((" + " + d["src"]) if d["src"] else "")
        where = []
        for e in by_mod.get(m, []):
            where.append("%s:%d%s%s" % (e["path"], e["line"],
                                        "" if e["scope"] == "-" else "@" + e["scope"],
                                        "" if not e["guarded"] else " [try]"))
        so = ("现场编" if d["src"] else "—")
        more = ""
        if len(where) > 4:
            more = " …(+%d)" % (len(where) - 4)
            where = where[:4]
        A("#  %-13s| %s | %s | %s | %s | %s%s"
          % (m, d["kind"], d["role"], need, so,
             " , ".join(where) if where else "（闭包根）", more))
    A("#")
    A("# ── .so 决策（**不随包传**）──────────────────────────────────────────────────")
    A("#   每个 native 模块都是首次使用时 `cc -O3 -std=c99 -fPIC -shared`（x86 还会加")
    A("#   `-mfpu=neon -mfloat-abi=hard` 作首选、失败再退无 NEON 那套）现场编译成")
    A("#   `<mod>_$(uname -m).so`，编在**模块自己所在目录**。")
    A("#   ⇒ 冻结点**必须同时带 .py 和 .c**：只带 .py 编不出来（缺 .c）、只带 .so 跨架构失效。")
    A("#   ⇒ 编译是一次性成本：首跑落在计时里（本报告口径 a），第 2 次起才是稳态（口径 b）。")
    A("#     `board_ops` 的跑点门禁要求：`kernel_deps.*.so` 与 `so_build.built_now` 都要留证。")
    for m in modules:
        if m in nat:
            so_pts, c_files = nat[m]
            A("#     %-12s .c=%s  ⇒ .so=%s_<arch>.so  加载点 %d 处（首个 L%d: %s）"
              % (m, ",".join(c_files) or "?", m, len(so_pts),
                 so_pts[0][0] if so_pts else 0,
                 (so_pts[0][1][:60] if so_pts else "-")))
    A("#")
    A("# ── 系统库（不随包、不入 md5）────────────────────────────────────────────────")
    for lib, why in SYSLIBS:
        A("#   %-12s %s" % (lib, why))
    A("#")
    A("# ── 已知埋雷（冷路径；今天没触发，但**同样是缺口**）──────────────────────────")
    for m in TRAPS:
        if m in modules:
            A("#   %-11s %s" % (m, DECL[m]["role"]))
    A("#   ⇒ 这三个不改变今天的数字，但任何「冻结点只挑热路径文件」的做法都会踩到。")
    A("#")
    A("# ── 文件清单（md5sum -c 用；顺序固定：模块名序，.py 在 .c 前）────────────────")
    for _m, fn, _k in files:
        A("%s  %s" % (_md5(os.path.join(pynq_dir, fn)), fn))
    A("#")
    A("# 清单共 %d 行有效 md5（md5sum -c 的期望输出：全部「成功」，rc=0）" % len(files))
    return "\n".join(L) + "\n", modules, files


# ============================================================================
# 4. 校验
# ============================================================================
_MD5LINE = re.compile(r"^([0-9a-f]{32})\s+\*?(\S+)\s*$")


def parse_closure(text):
    return [(m.group(1), m.group(2))
            for m in (_MD5LINE.match(l) for l in text.splitlines()) if m]


def do_check(args):
    pynq_dir = os.path.abspath(args.src)
    tw = getattr(args, "dir", None)
    tgt = os.path.abspath(tw) if tw else pynq_dir
    fail, warn = [], []

    # ① 重新生成一份（内存里），与磁盘上的清单比对 ⇒ 抓「依赖图变了没重生成」
    fresh, modules, files = render(pynq_dir)
    if args.file and os.path.exists(args.file):
        have = open(args.file, encoding="utf-8").read()
        if _strip_stamp(have) != _strip_stamp(fresh):
            fail.append("清单与源码树不一致（依赖图或文件内容变了）⇒ 请重跑 --gen")
        rows = parse_closure(have)
    else:
        rows = parse_closure(fresh)
        if args.file:
            fail.append("清单不存在：%s" % args.file)

    # ② 清单行数必须等于闭包文件数（少一行 = 少一个文件，正是事故形态）
    if len(rows) != len(files):
        fail.append("清单只有 %d 行 md5，闭包应有 %d 个文件" % (len(rows), len(files)))

    # ③ 逐文件在**目标目录**里核对 md5（= md5sum -c 的语义，但会给出可读的原因）
    for want, fn in rows:
        p = os.path.join(tgt, fn)
        if not os.path.exists(p):
            fail.append("缺文件 %s（%s）" % (fn, p))
            continue
        got = _md5(p)
        if got != want:
            fail.append("md5 不符 %s：清单 %s / 实际 %s" % (fn, want, got))

    # ④ 冻结点目录里**每个** .py 都必须是闭包内的（防「漏拷」只能靠 ①，防「多拷了无关
    #    文件」靠这条 —— 多了不会影响正确性，但会让「冻结点是什么」再次变成手挑清单）。
    #    ⚠ 只在 --dir 给了冻结点时才查：源码目录 ps/pynq 当然有很多不在闭包里的模块。
    #    ⇒ 多出来的文件**不影响判据**（真正害人的是"缺"，不是"多"），只打警告：
    #      冻结点里常混着驱动（ab_arms.py / board_v39_verify.py）和历史副本。
    if tw:
        extra = sorted(_local_modules(tgt) - set(modules))
        if extra:
            warn.append("冻结点目录还有闭包外的 .py（%d 个，不影响判据）：%s"
                        % (len(extra), extra))

    # ⑤ 每个模块必须解析到**目标目录内部**（防「其实 import 到了 ps/pynq」）
    if tw:
        for m in modules:
            p = os.path.join(tgt, m + ".py")
            if not p.startswith(tgt + os.sep):
                fail.append("模块 %s 解析到闭包目录之外：%s" % (m, p))

    for x in warn:
        print("   ⚠ " + x)
    if fail:
        print("✗ deps_closure --check FAIL（%d 项）" % len(fail))
        for x in fail:
            print("   · " + x)
        return 1
    print("✓ deps_closure --check PASS：%d 模块 / %d 文件；目录 = %s"
          % (len(modules), len(files), tgt))
    print("  闭包：%s" % ", ".join(modules))
    return 0


_STAMP_RE = re.compile(r"^# 时间: .*$", re.M)
_SRCROOT_RE = re.compile(r"^# 源根: .*$", re.M)


def _strip_stamp(text):
    """比对时忽略**环境相关**的头部行（时间戳 + 源根路径）。

    ★ 2026-11-02（第二棒，最小增量修正）：原来只忽略时间戳，于是
      `--check --src <另一个目录> --dir <同一个目录>`（**冻结点/板端**的标准用法）
      必然报「清单与源码树不一致」——因为 `# 源根:` 那行记的是生成时的相对路径。
      实测：把 15 文件闭包整体拷到 `/home/xilinx/demoir/ps/pynq_deps/` 后，
      在板端跑 `--check --src . --dir .` ⇒ 永远 FAIL ⇒ 这条判据在板端等于没有。
      ⇒ 把 `源根` 也归为"元信息"：真正判据是**模块图（①）** + **逐文件 md5（③）**，
        两者都与路径无关。少列/多列/依赖图变了照样 FAIL（见 do_check 的 ①②③）。
    """
    text = _STAMP_RE.sub("# 时间: <忽略>", text)
    return _SRCROOT_RE.sub("# 源根: <忽略>", text)


# ============================================================================
# 5. CLI
# ============================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="冻结点依赖闭包：静态生成 + 机械校验")
    ap.add_argument("--gen", action="store_true", help="生成清单")
    ap.add_argument("--check", action="store_true", help="校验清单（结构 + md5）")
    ap.add_argument("--scan", action="store_true", help="只打印静态扫描结果")
    ap.add_argument("--src", default=PYNQ_DEFAULT, help="源码根（默认 ps/pynq）")
    ap.add_argument("--dir", default=None, help="校验时核对的目标目录（冻结点）")
    ap.add_argument("--file", default=OUT_DEFAULT, help="清单路径")
    ap.add_argument("--out", default=None, help="--gen 的输出路径（默认同 --file）")
    a = ap.parse_args(argv)

    if a.scan:
        modules, edges = build_closure(os.path.abspath(a.src))
        print("闭包根：%s" % ", ".join(ROOTS))
        for e in edges:
            print("  %-13s L%-5d %-11s %-46s scope=%-20s%s"
                  % (e["path"], e["line"], e["module"], e["stmt"], e["scope"],
                     "  [try]" if e["guarded"] else ""))
        print("闭包 %d 模块：%s" % (len(modules), ", ".join(modules)))
        return 0
    if a.gen:
        text, modules, files = render(os.path.abspath(a.src))
        out = a.out or a.file
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print("✓ 已写 %s（%d 模块 / %d 文件）" % (out, len(modules), len(files)))
        return 0
    if a.check:
        return do_check(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
