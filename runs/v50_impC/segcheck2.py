import json, os, sys
R="/media/felix/Data/coding/demoir-fpga"; WT=R+"/worktrees/v50_impC"
sys.path.insert(0, WT+"/ps/pynq")
import numpy as np, bin_net as BN
pack=R+"/pynq_specialized_code/deploy/bin_weights_new"
segmap=json.load(open(R+"/runs/m56_segments/segments.json"))
net=BN.BinNet(pack, engine=None, H=64, W=64, verbose=False)
g,outs,tin=net.build_graph(np.zeros((3,64,64),np.float32))
cons=[len(v) for v in g.dependents]
segs=[[g.ops[0]]]
for o in g.ops[1:]:
    prev=segs[-1][-1]
    if cons[prev.out.id]==1:
        segs[-1].append(o)
    else:
        segs.append([o])
mine=[set(o.id for o in s) for s in segs]
off=[set(s["op_ids"]) for s in segmap["segments"]]
print("nsegs mine",len(mine),"official",len(off))
print("exact partition equal:", mine==off)
if mine!=off:
    offmap={}
    for i,s in enumerate(off):
        for oid in s: offmap[oid]=i
    mymap={}
    for i,s in enumerate(mine):
        for oid in s: mymap[oid]=i
    bad=[i for i in range(len(mine)) if mymap and off[offmap[sorted(mine[i])[0]]]!=mine[i]]
    print("mismatched segs",len(bad))
    for i in bad[:5]:
        print(" mine",sorted(mine[i])," off",sorted(off[offmap[sorted(mine[i])[0]]]))
# halo/buf formula check
def kinfo(o):
    L=o.layer
    return int(L.k), int(L.W), int(L.cin)
mism=0
for i,s in enumerate(segs):
    halo=bb=0
    for o in s:
        if o.kind=="conv" or o.kind=="convfp":
            if o.kind=="conv":
                k,W,cin=kinfo(o); halo+=k-1; bb+=(k-1)*W*cin
            else:
                k=o.args.get("k",3); W=int(s[0].deps[0].shape[2]); cin=int(s[0].deps[0].shape[0])
    off_s=segmap["segments"][i]
    if halo!=off_s["halo_rows"] or bb!=off_s["buf_bytes"]:
        mism+=1
        if mism<=6:
            print("seg",i,"mine halo/buf",halo,bb,"off",off_s["halo_rows"],off_s["buf_bytes"],off_s["kinds"])
print("halo/buf mismatches",mism,"/",len(segs))
