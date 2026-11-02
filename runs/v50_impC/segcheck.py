import json, os, sys
R="/media/felix/Data/coding/demoir-fpga"
WT=R+"/worktrees/v50_impC"
sys.path.insert(0, WT+"/ps/pynq")
import numpy as np
import bin_net as BN
os.environ.setdefault("BV_FREE","1")
pack=R+"/pynq_specialized_code/deploy/bin_weights_new"
segmap=json.load(open(R+"/runs/m56_segments/segments.json"))
net=BN.BinNet(pack, engine=None, H=64, W=64, verbose=False)
x0=np.zeros((3,64,64),np.float32)
g,outs,tin=net.build_graph(x0)
ops=g.ops
print("ops",len(ops),"tensors",len(g.tensors))
cons={i:len(v) for i,v in enumerate(g.dependents)}
# official
off=[set(s["op_ids"]) for s in segmap["segments"]]
print("map segs",len(off))

def seg_r1(ops, cons):
    segs=[[ops[0]]]
    for o in ops[1:]:
        prev=segs[-1][-1]
        ok = (len(o.deps)>=1 and o.deps[0].id==prev.out.id and cons.get(prev.out.id,0)==1)
        if ok and tuple(o.deps[0].shape)==tuple(prev.out.shape):
            segs[-1].append(o)
        else:
            segs.append([o])
    return segs

def cmp(segs, tag):
    mine=[set(o.id for o in s) for s in segs]
    print(tag,"nsegs",len(mine))
    # compare as partition by first op
    offmap={}
    for i,s in enumerate(off):
        for oid in s: offmap[oid]=i
    mymap={}
    for i,s in enumerate(mine):
        for oid in s: mymap[oid]=i
    diff=[oid for oid in mymap if offmap.get(oid)!=mymap[oid]]
    print("  differ ops:",len(diff))
    # show first few
    mism=[(i, sorted(mine[i]), sorted(off[offmap[sorted(mine[i])[0]]])) for i in range(len(mine)) if mine[i]!=off[offmap[sorted(mine[i])[0]]]]
    print("  seg mismatches",len(mism))
    for m in mism[:6]: print("   mine",m[0],m[1][:8],"...","off",m[2][:8],"...")
    return mymap, offmap
cmp(seg_r1(ops,cons),"R1")
