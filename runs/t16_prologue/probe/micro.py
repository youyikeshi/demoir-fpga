import os,sys,time,json
C="/media/felix/Data/coding/demoir-fpga/worktrees/T-16-prologue/pynq_specialized_code"
sys.path.insert(0,C+"/ps/pynq"); sys.path.insert(0,C+"/ps/tools")
os.environ.update(dict(BIN_PACK="e1e2",BIN_LAZY_DEQ="1",BIN_LAZY_SLOTS="24",
    BIN_CAT_FUSION="1",Q8CACHE="0",BIN_Q8_FUSED="1",BIN_BILINEAR_C="1"))
import numpy as np, bin_engine as BE, bin_net as BN, time as T
PACK=C+"/deploy/bin_weights_new"
net=BN.BinNet(PACK,engine=None,H=256,W=256,verbose=False)
g,outs,tin=net.build_graph(np.zeros((3,256,256),np.float32))
# 1) _analyze_lazy 拆分
acc={"lp":0.0}
orig=BN.BinNet._build_lane_plans
def lp(self,gg,needs,q8=False):
    t=T.perf_counter(); r=orig(self,gg,needs,q8=q8); acc["lp"]+=T.perf_counter()-t; return r
BN.BinNet._build_lane_plans=lp
t=T.perf_counter(); net._analyze_lazy(g,outs); tot=T.perf_counter()-t
print("analyze_lazy total %.2f ms, _build_lane_plans %.2f ms (%d calls) => 纯 Python 部分 %.2f ms"
      %(tot*1e3, acc["lp"]*1e3, 2, (tot-acc["lp"])*1e3))
# 2) build_graph
t=T.perf_counter(); g2,o2,t2=net.build_graph(np.zeros((3,256,256),np.float32)); print("build_graph %.2f ms"%(1e3*(T.perf_counter()-t)))
# 3) _pack_views 首调（698 命令扫描）
from verify_e1e2_path import FakeE1E2Unit
from verify_e1e2_direct import SubAlloc
flat=BE.MemBuf(64<<20,0x30000000); unit=FakeE1E2Unit(); unit.map_mem(0x30000000,flat)
BE._pynq_allocate=SubAlloc(flat,0x30000000,0)
eng=BE.BinConvEngine(unit,pack_dir=PACK,verbose=False); eng.load_weights()
t=T.perf_counter(); ee=eng.enable_e1e2(); t1=T.perf_counter()-t
print("enable_e1e2 (E1E2Engine ctor + probe) %.2f ms"%(t1*1e3))
n=len(ee.program.commands)
ee._pmax=None
t=T.perf_counter(); ee._pack_views(need_in=0,need_out=0,need_thr=0,need_desc=0); t2=T.perf_counter()-t
print("_pack_views 首调（含 %d 条命令扫描+分配）%.3f ms -> %.2f us/cmd"%(n,t2*1e3,t2/n*1e6))
t=T.perf_counter()
for _ in range(50): ee._pack_views()
t3=(T.perf_counter()-t)/50
print("_pack_views 稳态 %.1f us/次"%(t3*1e6))
# 4) t_q 取用 + _seg_layout 级别的小操作
blob=BE.Blob(PACK); L=eng.layer(0,256,256)
t=T.perf_counter()
for _ in range(200): blob.t_q(L.d)
print("blob.t_q %.1f us/次"%((T.perf_counter()-t)/200*1e6))
print("cpu:", open("/proc/cpuinfo").read().split("model name")[1].split(":")[1].strip() if "model name" in open("/proc/cpuinfo").read() else json.load(open("/proc/cpuinfo")) if 0 else "n/a")
