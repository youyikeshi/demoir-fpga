import os,sys
import numpy as np
PS=sys.argv[1]
sys.path.insert(0,os.path.join(PS,'pynq')); sys.path.insert(0,os.path.join(PS,'tools'))
import bin_net as BN
PACK='/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/deploy/bin_weights_new'
net=BN.BinNet(PACK,engine=None,H=256,W=256,verbose=False)
g,outs,tin=net.build_graph(np.zeros((3,256,256),np.float32)); net._analyze_lazy(g,outs)
print("== 机械扫描：全部 cat 的消费者表（取自 g.dependents，不取自记忆）==")
n1=0; n_multi=0
for o in g.ops:
    if o.kind!='cat': continue
    cons=[g.ops[i] for i in g.dependents[o.out.id]]
    kinds=[c.kind for c in cons]
    if net._cat_class.get(o.out.id)=='blocked':
        n1+=1
        assert len(cons)==1 and kinds==['eca'], (o.name,kinds)
        print("  BLOCKED cat %-46s deps=%d out=%s  消费者数=%d %s  ⇒ 唯一消费者是 eca = %s"
              %(o.name[-46:],len(o.deps),tuple(o.out.shape),len(cons),kinds,kinds==['eca']))
    if len(cons)>1: n_multi+=1
print("  blocked(bridge) cat 共 %d 个；全部满足「恰好 1 个消费者且是 eca」；多消费者 cat %d 个"%(n1,n_multi))
# 这 6 个 cat 的叶子是否本来就需要 f32（= 融合不改变任何物化决策）
print("== 这 6 个 cat 的叶子在 _needs_f32 里的情况（应当全在，且原因不是本 cat）==")
for o in g.ops:
    if o.kind!='cat' or net._cat_class.get(o.out.id)!='blocked': continue
    row=[]
    for d in o.deps:
        kinds=sorted({c.kind for c in [g.ops[i] for i in g.dependents[d.id]]})
        row.append((tuple(d.shape), d.id in net._needs_f32, kinds))
    print("  %-46s %s"%(o.name[-46:],row))
