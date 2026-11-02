import os, sys, json
R="/media/felix/Data/coding/demoir-fpga"; WT=R+"/worktrees/v50_impC"
sys.path.insert(0, WT+"/ps/pynq")
import numpy as np, bin_net as BN, bin_engine as BE
pack=R+"/pynq_specialized_code/deploy/bin_weights_new"
sm=json.load(open(R+"/runs/m56_segments/segments.json"))
for hw in (64, 256):
    net=BN.BinNet(pack, engine=None, H=hw, W=hw, verbose=False)
    g,outs,tin=net.build_graph(np.zeros((3,hw,hw),np.float32))
    plan=BN.make_segment_plan(g, outs, segmap=sm)
    print("=== hw=%d ===" % hw)
    print(plan.report(max_rows=6))
    over=[s for s in plan.segments if s.over_budget]
    print("  超预算段：%d 段 -> %s" % (len(over), [s.seg_id for s in over[:10]]))
    print("  max_seg_buf = %.1f KB" % (plan.summary()["max_seg_buf"]/1024))
    print()
