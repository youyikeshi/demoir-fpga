import os,sys,numpy as np
ROOT="/media/felix/Data/coding/demoir-fpga"; PS=os.path.join(ROOT,"pynq_specialized_code")
sys.path.insert(0,os.path.join(PS,"ps","pynq")); sys.path.insert(0,os.path.join(PS,"ps","tools"))
os.environ["BIN_V50_FUSE"]="1"
for k in ("BIN_PACK","BIN_LAZY_DEQ","BIN_LAZY_SLOTS"): os.environ.pop(k,None)
os.environ["BV_FREE"]="1"
import bin_net as BN, bin_engine as BE
PACK=os.path.join(PS,"deploy","bin_weights_new")
x0=np.random.default_rng(1).random((3,64,64),dtype=np.float32)
net=BN.BinNet(PACK,engine=None,H=64,W=64,verbose=False); net.run(x0,pipeline=True,nbuf=2)
p=net._segplan; g=net._graph if net._graph is not None else net.build_graph(x0)
from collections import Counter
bad=[]; ss=Counter(); rows=[]
for s in p.segments:
    if not s.fused: continue
    o=g.ops[s.op_ids[0]]; L=o.layer
    ss[(int(L.s),int(L.k),int(L.dil))]+=1
    # conv 输出：Hout=((nband-1)>>s)+1 仅当 nband 是整段且 s 对齐；检查 Wout==W_in
    Hout=((int(s.h_out)-1)//int(L.s))+1; Wout=((int(s.w_in)-1)//int(L.s))+1
    if Wout!=int(s.w_in) or Hout!=int(s.h_out):
        bad.append((s.seg_id,int(L.s),int(s.h_out),Hout,int(s.w_in),Wout))
print("fused (s,k,d) histogram:",dict(ss))
print("Wout!=W_in 或 Hout!=h_out 的融合段:",len(bad))
for b in bad[:10]: print("  S%-3d s=%d h_out=%d Hout=%d w_in=%d Wout=%d"%b)
