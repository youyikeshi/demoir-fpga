import json, os, sys
R="/media/felix/Data/coding/demoir-fpga"; WT=R+"/worktrees/v50_impC"
sys.path.insert(0, WT+"/ps/pynq")
import numpy as np, bin_net as BN
pack=R+"/pynq_specialized_code/deploy/bin_weights_new"
segmap=json.load(open(R+"/runs/m56_segments/segments.json"))
net=BN.BinNet(pack, engine=None, H=64, W=64, verbose=False)
g,outs,tin=net.build_graph(np.zeros((3,64,64),np.float32))
cons=[len(v) for v in g.dependents]
offmap={}
for s in segmap["segments"]:
    for oid in s["op_ids"]: offmap[oid]=s["seg_id"]
n=int(sys.argv[1]) if len(sys.argv)>1 else 30
for o in g.ops[:n]:
    print("%3d seg%-4s %-10s cons=%-2d %-52s deps=%s out=%s" % (
        o.id, offmap.get(o.id), o.kind, cons[o.out.id], o.name[:52],
        [d.id for d in o.deps], tuple(o.out.shape)))
