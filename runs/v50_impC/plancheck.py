import os, sys, json
R="/media/felix/Data/coding/demoir-fpga"; WT=R+"/worktrees/v50_impC"
sys.path.insert(0, WT+"/ps/pynq")
import numpy as np, bin_net as BN, bin_engine as BE
pack=R+"/pynq_specialized_code/deploy/bin_weights_new"
net=BN.BinNet(pack, engine=None, H=64, W=64, verbose=False)
g,outs,tin=net.build_graph(np.zeros((3,64,64),np.float32))
sm=json.load(open(R+"/runs/m56_segments/segments.json"))
plan=BN.make_segment_plan(g, outs, segmap=sm)
print(plan.report(max_rows=8))
print()
print("segmap_ok:", plan.segmap_ok, plan.segmap_n)
print("summary:", json.dumps(plan.summary(), ensure_ascii=False)[:600])
print()
print(plan.dump_chain(max_desc=4, max_seg=3))
# check chain legality
w=plan.chain
print()
print("chain n:", len(w), "tail next_pa:", w[-1][8],
      "all aligned:", all(x[8]==0 or x[8]%4096==0 for x in w),
      "w9 nband:", {x[9]&0xFFFF for x in w}, "opmask:", {x[9]>>16 for x in w})
