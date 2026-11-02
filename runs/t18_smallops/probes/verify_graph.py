import os,sys,collections
import numpy as np
HERE='/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/ps'
sys.path.insert(0,os.path.join(HERE,'pynq')); sys.path.insert(0,os.path.join(HERE,'tools'))
import bin_net as BN
PACK='/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/deploy/bin_weights_new'
net=BN.BinNet(PACK,engine=None,H=256,W=256,verbose=False)
g,outs,tin=net.build_graph(np.zeros((3,256,256),np.float32)); net._analyze_lazy(g,outs)
cons=collections.defaultdict(list)
for o in g.ops:
    for d in o.deps: cons[d.id].append(o)
blk=[o for o in g.ops if o.kind=='cat' and net._cat_class.get(o.out.id)=='blocked']
print("blocked cat → consumers:")
for o in blk:
    print("  ",o.name[-46:], [c.kind for c in cons[o.out.id]], "| out in needs_f32:", o.out.id in net._needs_f32)
eca=[o for o in g.ops if o.kind=='eca']
print("eca deps are all blocked cat3:", all(g.ops and any(cc.kind=='cat' for cc in cons[o.deps[0].id]) for o in eca))
# sam inputs: are they already f32-needed for other reasons?
for o in g.ops:
    if o.kind=='sam':
        for d in o.deps[:4]:
            kinds=sorted({c.kind for c in cons[d.id]})
            print("  sam dep", tuple(d.shape), "consumers:", kinds)
        break
# 统计各桶“必要字节/实现字节”
