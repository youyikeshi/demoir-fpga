import os,sys,collections
import numpy as np
HERE='/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/ps'
sys.path.insert(0,os.path.join(HERE,'pynq')); sys.path.insert(0,os.path.join(HERE,'tools'))
import bin_net as BN
PACK='/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/deploy/bin_weights_new'
for hw in (64,256):
    net=BN.BinNet(PACK,engine=None,H=hw,W=hw,verbose=False)
    g,outs,tin=net.build_graph(np.zeros((3,hw,hw),np.float32)); net._analyze_lazy(g,outs)
    cls=net._cat_class
    cnt=collections.Counter(cls.values())
    print(f"--- hw={hw}: cat classes {dict(cnt)}")
    blkbytes=0
    for o in g.ops:
        if o.kind=='cat' and cls[o.out.id]=='blocked':
            b=sum(int(np.prod(d.shape))*4 for d in o.deps)
            blkbytes+=b
            print(f"   BLOCKED cat {o.name[-58:]:58} out={tuple(o.out.shape)} deps={[tuple(d.shape) for d in o.deps]} rd={b/1e6:.2f}MB")
    print(f"   blocked rd total = {blkbytes/1e6:.2f} MB")
    # breakdown of all cat bytes by class
    per=collections.defaultdict(float); nper=collections.Counter()
    for o in g.ops:
        if o.kind!='cat': continue
        c=cls[o.out.id]; per[c]+=sum(int(np.prod(d.shape))*4 for d in o.deps); nper[c]+=1
    print("   cat bytes by class:", {k:(nper[k], round(v/1e6,2)) for k,v in per.items()})
