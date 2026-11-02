"""P2/P3 单元：链提交/进度/出错即停（用 FakeUnit 的寄存器模型，不上板）。"""
import os, sys
R="/media/felix/Data/coding/demoir-fpga"; WT=R+"/worktrees/v50_impC"
sys.path.insert(0, WT+"/ps/pynq")
os.environ["BIN_V50_FUSE"]="1"; os.environ["BIN_V50_CHAIN_RTL"]="1"
import numpy as np, bin_engine as BE
from bin_engine import FakeUnit, BinConvEngine, BinRunError

for k in ("BIN_PACK","CATPACK","BIN_F32DIRECT"): os.environ.pop(k, None)

class ChainUnit(FakeUnit):
    """只保留"寄存器 + start/done/busy/progress"语义的链模型（不跑 conv）。"""
    def _vcode(self):
        return 0
    def _try_start(self):
        self.run_active, self.busy, self.done, self.err = 1, 1, 0, 0
    def progress(self, n):
        self.reg[0x04] = int(n)
    def finish(self, n, err=0):
        self.reg[0x04] = int(n); self.busy, self.done, self.err = 0, 1, int(err)
    def err_only(self, err):
        self.err = int(err)

unit = ChainUnit(0x4006_0000)
eng = BinConvEngine(unit, pack_dir=R+"/pynq_specialized_code/deploy/bin_weights_new", verbose=False)
# 让 FakeUnit 的 0x208 能回读（它本来就存 reg），探针应通过
print("chain_ready:", eng.v50_chain_ready(), "why:", eng.v50_chain_why())
assert eng.v50_chain_ready(), eng.v50_chain_why()

# arena：一块"注册过的"物理缓冲（FakeArena 有 pa）
arena = BE.FakeArena(1<<20, base_pa=0x2000_0000, unit=unit)
descs = [BE.v50_desc(in_pa=0x2000_1000, out_pa=0x2000_2000, cfg_c=48, cfg_h=8,
                     cfg_w=32, band_y0=8*b, op_mask=1<<0,
                     flags=(1 if b==2 else 0)) for b in range(3)]
view, pa = arena.region(BE.v50_chain_bytes(3), align=4096, name="v50desc")
head, words = BE.v50_chain_store(view, pa, descs)
print("head=%#x 条数=%d next_pa=%s tail=%#x" % (head, len(words), [hex(w[8]) for w in words], words[-1][8]))
assert head % 4096 == 0 and words[-1][8] == 0 and words[0][8] == head+4096

# --- 1) 正常：PROGRESS 从 0 涨到 3，done 置位 ---
eng.v50_submit_chain(head, 3)
print("提交后已写 CHAIN_HEAD?", unit.reg.get(0x00), "busy:", unit.busy, "chain:", bool(eng._v50_chain))
try:
    eng.kick()          # 链在飞 ⇒ 必须拒绝第二次 kick
    raise SystemExit("FAIL: 第二次 kick 没被拒绝")
except RuntimeError as e:
    print("第二次 kick 被拒 ✓:", str(e)[:60])
unit.progress(2)                                              # 未 done
print("poll(2/3) ->", eng.v50_poll_chain())
unit.finish(3)                                                # done
st = eng.v50_wait_chain(timeout=5)
print("wait ->", {k: st[k] for k in ("n","progress","seconds")})
print("chain_stats:", eng.v50_chain_stats())

# --- 2) 出错即停：err=3 ⇒ 抛 BinRunError，清链，poison，拒绝再提交 ---
eng.v50_submit_chain(head, 3)
unit.err_only(3); unit.done = 1
try:
    eng.v50_wait_chain(timeout=5)
    raise SystemExit("FAIL: err 没抛异常")
except BinRunError as e:
    print("err 抛 BinRunError ✓:", str(e)[:70])
print("poison:", eng._v50_poison[:40], "chain:", eng._v50_chain)
try:
    eng.v50_submit_chain(head, 3)
    raise SystemExit("FAIL: poison 后还能提交")
except RuntimeError as e:
    print("poison 后拒绝再 kick ✓")
eng.v50_reset_chain()

# --- 3) 超时：不推进 ⇒ TimeoutError，且不会重试 ---
unit.err = 0; unit.done = 0; unit.progress(0)
eng.v50_submit_chain(head, 3, timeout=0.2)
try:
    eng.v50_wait_chain(timeout=0.2)
    raise SystemExit("FAIL: 超时没抛")
except TimeoutError as e:
    print("超时抛 TimeoutError ✓:", str(e)[:60])
print("ALL CHAIN UNIT CHECKS PASS")
