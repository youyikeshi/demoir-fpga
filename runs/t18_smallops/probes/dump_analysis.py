import os,sys,json,hashlib
import numpy as np
PS=sys.argv[1]; HWP=int(sys.argv[2]); OUT=sys.argv[3]
sys.path.insert(0,os.path.join(PS,'pynq')); sys.path.insert(0,os.path.join(PS,'tools'))
import bin_net as BN
PACK='/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/deploy/bin_weights_new'
net=BN.BinNet(PACK,engine=None,H=HWP,W=HWP,verbose=False)
g,outs,tin=net.build_graph(np.zeros((3,HWP,HWP),np.float32))
net._analyze_lazy(g,outs)
def h(o):
    return hashlib.sha1(json.dumps(o,sort_keys=True,default=str).encode()).hexdigest()[:16]
d=dict(
  ops=len(g.ops), tensors=len(g.tensors),
  needs_f32=sorted(int(i) for i in net._needs_f32),
  nuse={str(k):int(v) for k,v in net._nuse.items()},
  cat_class={str(k):v for k,v in net._cat_class.items()},
  lane_plan={str(k):str(v) for k,v in (net._lane_plan or {}).items()},
  lane_plan_q8={str(k):str(v) for k,v in (net._lane_plan_q8 or {}).items()},
  cat_stats_class_counts={k:sum(1 for v in net._cat_class.values() if v==k) for k in set(net._cat_class.values())},
)
print(json.dumps({k:(v if not isinstance(v,(list,dict)) else len(v)) for k,v in d.items()},ensure_ascii=False))
print("H(needs_f32)=%s H(nuse)=%s H(cat_class)=%s H(lane_plan)=%s H(lane_plan_q8)=%s"%(
    h(d['needs_f32']),h(d['nuse']),h(d['cat_class']),h(d['lane_plan']),h(d['lane_plan_q8'])))
json.dump(d,open(OUT,'w'),sort_keys=True,default=str)
