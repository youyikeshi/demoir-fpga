"""P2/P3：E1E2 打包的"整层一次 kick"验证（用 ChainUnit 计数 start 次数）。"""
import os, sys
R="/media/felix/Data/coding/demoir-fpga"; WT=R+"/worktrees/v50_impC"
sys.path.insert(0, WT+"/ps/pynq")
os.environ["BIN_V50_FUSE"]="1"; os.environ["BIN_V50_CHAIN_RTL"]="1"
import numpy as np, bin_engine as BE
from bin_engine import FakeUnit, BinConvEngine, E1E2Engine, FakeArena

class ChainUnit(FakeUnit):
    starts = 0
    def _vcode(self): return 0
    def _try_start(self):
        type(self).starts += 1
        self.run_active, self.busy, self.done, self.err = 1, 1, 0, 0
    target = 2
    def progress(self, n): self.reg[0x04] = int(n)
    def finish(self, n, err=0):
        self.reg[0x04] = int(n); self.busy, self.done, self.err = 0, 1, int(err)
    def read(self, off):
        off = int(off); page, o = (off >= 0x10000), (off - 0x10000) if off >= 0x10000 else off
        o &= 0xFF
        if page and o == 0x04:                 # PROGRESS：每读一次推进一条
            n = min(int(self.reg.get(0x04, 0)) + 1, type(self).target)
            self.reg[0x04] = n
            if n >= type(self).target:
                self.busy, self.done = 0, 1
        return super().read(off)

unit = ChainUnit(0x4006_0000)
core = BinConvEngine(unit, pack_dir=R+"/pynq_specialized_code/deploy/bin_weights_new", verbose=False)
assert core.v50_chain_ready()
e2 = E1E2Engine(core, desc_dir=None, verbose=False)

arena = FakeArena(1<<20, base_pa=0x2000_0000, unit=unit)
vin,  pa_in  = arena.region(4096, align=64)
vout, pa_out = arena.region(4096, align=64)
vthr, pa_thr = arena.region(4096, align=64)
vdesc,pa_dsc = arena.region(BE.v50_chain_bytes(2), align=64)

cin, H, W = 96, 8, 8        # cb = 2 组
descs = e2.chain_descs(cin, H, W, out_pa=pa_out, desc_base_pa=pa_dsc)
print("cb=2 ⇒ 描述符条数:", len(descs), " last flags:", hex(descs[-1][7]))
starts0 = ChainUnit.starts
e2.submit_chain(descs, out_len=len(vout), bufs=(vin, vout, vthr, vdesc),
                restore=False, timeout=5)
print("submit_chain 期间 start 次数:", ChainUnit.starts - starts0, "（v5 是 2 次：每组一次）")

# 检查链真的落在 4 KB 步进上、next_pa 链式
import numpy as np
words = st["words"] if False else None
raw = np.frombuffer(np.asarray(vdesc, np.uint8), np.uint8)
b = int(getattr(vdesc, "physical_address"))
off = ((b + 4095)//4096*4096) - b
w0 = np.frombuffer(raw[off:off+40], np.uint32)
w1 = np.frombuffer(raw[off+4096:off+4096+40], np.uint32)
print("desc0:", " ".join("%08x"%x for x in w0))
print("desc1:", " ".join("%08x"%x for x in w1))
print("next_pa 链式:", w0[8] == (b+off+4096) and w1[8] == 0, " nband:", w0[9]&0xFFFF, w1[9]&0xFFFF)
assert w0[8] == b+off+4096 and w1[8] == 0
assert ChainUnit.starts - starts0 == 1
print("E1E2 整层一次 kick ✓")
