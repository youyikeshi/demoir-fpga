import os,sys,numpy as np
ROOT="/media/felix/Data/coding/demoir-fpga"; PS=os.path.join(ROOT,"pynq_specialized_code")
sys.path.insert(0,os.path.join(PS,"ps","pynq")); sys.path.insert(0,os.path.join(PS,"ps","tools"))
os.environ["BIN_V50_FUSE"]="1"
for k in ("BIN_PACK","BIN_LAZY_DEQ","BIN_LAZY_SLOTS"): os.environ.pop(k,None)
os.environ["BV_FREE"]="1"
import bin_net as BN, bin_engine as BE
PACK=os.path.join(PS,"deploy","bin_weights_new")
x0=np.random.default_rng(1).random((3,64,64),dtype=np.float32)
net=BN.BinNet(PACK,engine=None,H=64,W=64,verbose=False)
net.run(x0,pipeline=True,nbuf=2)
p=net._segplan
g=net._graph if net._graph is not None else net.build_graph(x0)
V=int(p.vlanes or 8)
ok=[];bad=[]
for s in p.segments:
    if not s.fused: continue
    ops=[g.ops[i] for i in s.op_ids]   # 只读引用
    convs=[o for o in ops if o.kind=="conv"]
    why=None
    if not convs: why="no-conv"
    else:
        ks={int(o.layer.k) for o in convs}; ss={int(o.layer.s) for o in convs}
        dd={int(o.layer.dil) for o in convs}; pp={int(o.layer.pad) for o in convs}
        if len(ks)!=1 or len(ss)!=1 or len(dd)!=1 or len(pp)!=1: why="k/s/d/pad 不一致"
        elif ks.pop()!=1: why="k!=1"
        elif int(s.c_out)%V: why="cout%%%d!=0 (cout=%d)"%(V,int(s.c_out))
    if why: bad.append((s.seg_id,"/".join(s.kinds),why,hex(s.op_mask)))
    else: ok.append(s)
print("fused(plan)=%d  可提交(段边界寄存器齐备)=%d  会被 MissingPiece 拒=%d"%(len(ok)+len(bad),len(ok),len(bad)))
from collections import Counter
print("拒绝原因:",dict(Counter(b[2].split(" (")[0] for b in bad)))
print("被拒段 op_mask 不在 0x3 内的:",sum(1 for b in bad if int(b[3],16)&~0x3))
for b in bad[:6]: print("   S%-3d %-30s %-22s %s"%b)
print("可提交段的 op_mask 集合:",sorted({hex(s.op_mask) for s in ok}))
print("可提交段 cout 集合:",sorted({int(s.c_out) for s in ok}))
print("可提交段 desc 总数:",sum(s.n_desc for s in ok))
