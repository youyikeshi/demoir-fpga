# -*- coding: utf-8 -*-
import io, os, re, sys

MF = "/tmp/probe_T15/mf"
R = "/media/felix/Data/coding/demoir-fpga"

# ---------------- A) bin_net: replace 3-way conflict with T-14 block ----------
src = open(os.path.join(MF, "merged_bin_net.py"), encoding="utf-8").read()
m = re.search(r"<<<<<<< HEAD\n(.*?)\|\|\|\|\|\|\| base825cbb9\n(.*?)=======\n(.*?)>>>>>>> T14\n",
              src, re.S)
assert m, "bin_net conflict not found"
ours, base, theirs = m.group(1), m.group(2), m.group(3)
assert ours.strip() == "", "ours not empty?!"
# theirs = parked _e1p/_dcs block + T-14 block. Split at the T-14 banner.
T14_BANNER = "        # ★ 2026-11-02（§②）：依赖可见性"
i = theirs.index(T14_BANNER)
parked_block, t14_block = theirs[:i], theirs[i:]
assert "desc_cache_stats" in parked_block and "_e1p = None" in parked_block
# keep ONLY the E1E2 prof plumbing (HEAD-native symbol `_prof`), drop `_dcs` (parked desc cache)
keep = """        # ★ E1E2 的 PS 侧分项（`prepare_segments` 内部桶，只在 BIN_PROFILE=1 时有）
        _e1p = None
        if self.engine is not None and getattr(self.engine, "_e1e2", None) is not None:
            _p = getattr(self.engine._e1e2, "_prof", None)
            if _p:
                _e1p = dict(_p)
"""
resolved = keep + t14_block
out = src[:m.start()] + resolved + src[m.end():]
# re-add e1e2_prof=_e1p to the stats dict (T-14's perf-gate input evidence;
# it was a parked-mirror context line, but it is HEAD-native and T-14's gate needs it)
anchor = "                          pack_pl=t_pack_pl,\n"
assert out.count(anchor) == 1, out.count(anchor)
out = out.replace(anchor, "                          e1e2_prof=_e1p,\n" + anchor)
assert "<<<<<<<" not in out and ">>>>>>>" not in out
open(os.path.join(R, "pynq_specialized_code/ps/pynq/bin_net.py"), "w", encoding="utf-8").write(out)
print("bin_net resolved, %d lines" % out.count("\n"))

# ---------------- B) board_v39: HEAD + hand-ported T-14 pieces ----------------
p = os.path.join(MF, "head_board.py")
s = open(p, encoding="utf-8").read()

old_dump = '''def dump(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=1, ensure_ascii=False)
'''
new_dump = '''def dump(path):
    """落盘。★ 这里**再刷一次内核状态**：dump 发生在所有 run 之后 ⇒ 此时
    `q8/c8deq/bpack._STATE` 已是实测结果（权威），覆盖开跑前的 probe 值。"""
    try:
        rp = R.setdefault("runpoint", {})
        d, ok = kernel_deps()
        rp["kernel_deps"] = d
        rp["deps_closure_ok"] = bool(ok)
        rp["kernel_deps_note"] = ("kernel_deps = 跑完后的实测（权威）；"
                                  "kernel_deps_probe = 开跑前的预测")
    except Exception as ex:                            # noqa: BLE001
        R.setdefault("runpoint", {})["kernel_deps_error"] = repr(ex)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=1, ensure_ascii=False)


def kernel_deps():
    """`bin_engine.kernel_deps()` 的薄封装：返回 (dict, deps_closure_ok)。

    ★ **零副作用**：只读模块自己的 `_STATE` + `find_spec`，不会 import 内核模块，
      也不会触发 `cc` 现场编译（否则会把一次性编译成本从 prologue 里挪走）。
    """
    try:
        import bin_engine as _BE
        d = _BE.kernel_deps()
        return d, _BE.deps_closure_ok(d)
    except Exception as ex:                            # noqa: BLE001
        return {"error": repr(ex)}, False


def deps_summary():
    """一行摘要（打日志用）：`q8=fast c8deq=fast ... ⇒ deps_closure_ok=True`。"""
    d, ok = kernel_deps()
    if "error" in d:
        return "[DEPS] <读不到：%s>" % d["error"]
    order = ("q8", "c8deq", "bpack", "e1e2", "catpack", "safe")
    return ("[DEPS] " + " ".join("%s=%s" % (k, d[k]["state"]) for k in order
                                 if k in d)
            + "  ⇒ deps_closure_ok=%s" % bool(ok))
'''
assert s.count(old_dump) == 1
s = s.replace(old_dump, new_dump)

# step_kernels() — inserted as "步骤 0b"，紧跟 cfg()
step_kernels = '''# ---------------------------------------------------------------- 步骤 0b：内核装载
def step_kernels():
    """`q8/c8deq/bpack` 的**一次性装载成本**：单独计时，不进稳态。

    ★ 为什么非要有这一步（2026-11-02 12:13 板端实测）：
      三个内核都是"首次使用时才 load()"（`.so` 已编好也要 load + 逐位自检
      1.4~1.9 s/个）。这块成本落在**首个整网的 prologue/epilogue 桶里**：

        | 桶        | 首跑（含装载） | 第二次跑（热态） |
        | prologue  | 3.572 s        | 1.613 s          |
        | epilogue  | 1.632 s        | 0.257 s          |

      而 `PERF_GATE` 的门槛是热态值（q8 0.61 s / epi 0.26 s）⇒ 不先把装载挪出去，
      每次 session 首跑都会被判 `run_valid=False`（误报），报警就没人看了。
      这与 `step_bilinear()` 对 `bilinear_c` 的处理**完全同构**：一次性成本显式计时，
      并且**不偷偷加进 total**（口径 a/b 都打印）。
    """
    hdr("步骤 0b：q8/c8deq/bpack 一次性装载（单独计时，不进稳态）")
    rec = BE.load_kernels()
    rp = R.setdefault("runpoint", {})
    rp["kernel_load"] = rec
    tot = 0.0
    for k, v in rec.items():
        tot += float(v.get("seconds") or 0.0)
        log("  %-6s load=%.3f s  loaded=%s  why=%s"
            % (k, v.get("seconds") or 0, v.get("loaded"), v.get("why")))
    rp["kernel_load_seconds"] = round(tot, 4)
    log("  **一次性装载合计 = %.3f s**（从这一行起，后面的 prologue/epilogue 才是热态）"
        % tot)
    if not all(v.get("loaded") for v in rec.values()):
        log("  ⚠ 有内核没装上 ⇒ 本跑点 deps_closure_ok 会是 False（见 [DEPS] 行）")
    return rec


'''
anchor_step1 = "# ---------------------------------------------------------------- 步骤 1：load\n"
assert s.count(anchor_step1) == 1
s = s.replace(anchor_step1, step_kernels + anchor_step1)

# run_net row: append T-14 keys
old_row = '''               sha1=sh, expect=FROZEN[hw], sha_ok=bool(ok),
               n_sha_ok=int(ok) * 3)
'''
new_row = '''               sha1=sh, expect=FROZEN[hw], sha_ok=bool(ok),
               n_sha_ok=int(ok) * 3,
               # ★ 2026-11-02（§④）：这一跑的**内核状态**与**性能门槛**都进行。
               #   `run_valid=False` 的行不许进基线表（回落数值等价 ⇒ sha1 全过，
               #   `sha_ok` 抓不到这类事故）。
               deps_closure_ok=st.get("deps_closure_ok"),
               kernel_deps={k: v.get("state") for k, v in
                            (st.get("kernel_deps") or {}).items()},
               perf_gate=st.get("perf_gate") or [],
               # ★ 门槛的**输入**也要留证：`perf_gate` 空 ≠ 没判，判过要知道数是多少
               #   （`e1e2_prof.q8` 就是 `prologue.q8` 桶；只有 BIN_PROFILE=1 才出）。
               e1e2_prof=st.get("e1e2_prof"),
               # ★ 热态（口径 a/b）：False ⇒ 这一跑含一次性内核装载，桶不是热态值
               #   ⇒ 基线表只收 perf_warm=True 的行（2026-11-02 板端实测：
               #   首跑 epilogue 1.640 s vs 热态 0.257 s）。
               perf_warm=st.get("perf_warm"),
               perf_note=st.get("perf_note"),
               run_valid=st.get("run_valid"))
'''
assert s.count(old_row) == 1
s = s.replace(old_row, new_row)

# run_net: run_valid warning after the "── tag hw=" line
old_log = '''    log("  ── %s hw=%d ──" % (tag or "net", hw))
    log("     总 %.3f s（墙钟 %.3f） PL %.3f（其中 E1E2 %.3f）| PS %.3f = "
'''
new_log = '''    log("  ── %s hw=%d ──" % (tag or "net", hw))
    if row.get("run_valid") is False:
        log("     ⚠⚠ run_valid=False（deps_closure_ok=%s, perf_gate=%s）"
            " ⇒ 本行数字**不许进基线表**"
            % (row.get("deps_closure_ok"),
               [a.get("bucket") for a in row.get("perf_gate") or []]))
    log("     总 %.3f s（墙钟 %.3f） PL %.3f（其中 E1E2 %.3f）| PS %.3f = "
'''
assert s.count(old_log) == 1
s = s.replace(old_log, new_log)

# main(): runpoint probe + [DEPS] line  (anchored after env_at_start)
old_env = '''    R["env_at_start"] = {k: os.environ.get(k, "") for k in _KEYS}
'''
new_env = '''    R["env_at_start"] = {k: os.environ.get(k, "") for k in _KEYS}
    # ★ 2026-11-02（§② 依赖可见性）：跑点必须能回答"这一跑跑在什么内核上"。
    #   2026-11-02 那次 1.65× 假数字，直接原因就是 JSON 只记了时间、没记内核状态
    #   （prologue/epilogue 从 C 内核换成 numpy 回落，而看 JSON 完全看不出来）。
    #   ⇒ 从今天起：`deps_closure_ok=False` 的跑点**不许进基线表**。
    _d, _ok = kernel_deps()
    _rp = R.setdefault("runpoint", {})
    _rp["kernel_deps_probe"] = _d           # 开跑前（零副作用预测；不触发 cc 编译）
    _rp["deps_closure_ok"] = bool(_ok)      # dump() 时会被实测值覆盖
    _rp["stamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
    log("  " + deps_summary())
    if not _ok:
        log("  ⚠ deps_closure_ok=False ⇒ 本次跑点**不许进基线表**"
            "（跑的不是应跑的内核）")
'''
assert s.count(old_env) == 1
s = s.replace(old_env, new_env)

# main(): step_kernels() before the 256 run
old_256 = '''
    # 256²（最重要）
    r256 = step_net(ov, 256, a.slots, Clocks.fclk0_mhz)
'''
new_256 = '''
    # ★ 2026-11-02（§④）：三个 native 内核的装载也必须在这里**一次性做掉**，
    #   否则首个 256² 的 prologue/epilogue 桶会被 load+自检抬高
    #   （板端实测首跑 epi 1.640 s vs 热态 0.257 s）⇒ 门槛误报。
    step_kernels()
    dump(a.json)

    # 256²（最重要）
    r256 = step_net(ov, 256, a.slots, Clocks.fclk0_mhz)
'''
assert s.count(old_256) == 1
s = s.replace(old_256, new_256)

open(os.path.join(R, "pynq_specialized_code/ps/pynq/board_v39_verify.py"), "w",
     encoding="utf-8").write(s)
print("board_v39 resolved, %d lines" % s.count("\n"))
