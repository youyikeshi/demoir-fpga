import os,sys,json,collections
import numpy as np
HERE='/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/ps'
sys.path.insert(0,os.path.join(HERE,'pynq')); sys.path.insert(0,os.path.join(HERE,'tools'))
import bin_net as BN
PACK='/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/deploy/bin_weights_new'
net=BN.BinNet(PACK,engine=None,H=256,W=256,verbose=False)
g,outs,tin=net.build_graph(np.zeros((3,256,256),np.float32)); net._analyze_lazy(g,outs)
D=collections.defaultdict(list)
for o in g.ops:
    if o.kind in ('sam','add','eca','cat','shuffle','unshuffle','interp'):
        D[o.kind].append(dict(name=o.name, out=[int(v) for v in o.out.shape],
                              deps=[[int(v) for v in d.shape] for d in o.deps],
                              cls=net._cat_class.get(o.out.id)))
json.dump(D,open('/tmp/probe_T18/shapes.json','w'),indent=0)
for k,v in D.items(): print(k,len(v))
print('blocked cats:',[c for c in D['cat'] if c['cls']=='blocked'])
print('sam:',D['sam'][0])
print('eca:',D['eca'][0])
