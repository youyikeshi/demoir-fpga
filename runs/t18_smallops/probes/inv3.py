import os,sys,collections
import numpy as np
HERE='/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/ps'
sys.path.insert(0,os.path.join(HERE,'pynq')); sys.path.insert(0,os.path.join(HERE,'tools'))
import bin_net as BN
PACK='/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/deploy/bin_weights_new'
hw=256
net=BN.BinNet(PACK,engine=None,H=hw,W=hw,verbose=False)
g,outs,tin=net.build_graph(np.zeros((3,hw,hw),np.float32)); net._analyze_lazy(g,outs)
nf=net._needs_f32
conv=[o for o in g.ops if o.kind=='conv']
f32=[o for o in conv if o.out.id in nf]
print(f"conv op n={len(conv)}, conv outs needing f32 = {len(f32)}")
tot=0
for o in f32:
    nb=int(np.prod(o.out.shape))*4
    cons=[c.kind for c in g.ops if any(d.id==o.out.id for d in c.deps)]
    tot+=nb
    print(f"   {o.name[-52:]:52} out={tuple(o.out.shape)} {nb/1e6:5.2f}MB cons={cons}")
print(f"   f32 read_out bytes total = {tot/1e6:.2f} MB ; deq floor @0.5GB/s = {tot/1e6/0.5:.3f} s")
# per-op bytes for the lazy ones (only invalidate cost)
print("lazy conv count:", len(conv)-len(f32))
