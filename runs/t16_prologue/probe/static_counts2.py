import os,sys,json
C="/media/felix/Data/coding/demoir-fpga/worktrees/T-16-prologue/pynq_specialized_code"
sys.path.insert(0,C+"/ps/pynq"); sys.path.insert(0,C+"/ps/tools")
os.environ.update(dict(BIN_PACK="e1e2",BIN_LAZY_DEQ="1",BIN_LAZY_SLOTS="24",
    BIN_CAT_FUSION="1",Q8CACHE="0",BIN_Q8_FUSED="1",BIN_BILINEAR_C="1"))
import numpy as np, bin_engine as BE, bin_net as BN
PACK=C+"/deploy/bin_weights_new"
net=BN.BinNet(PACK,engine=None,H=256,W=256,verbose=False)
g,outs,tin=net.build_graph(np.zeros((3,256,256),np.float32))
net._analyze_lazy(g,outs)
conv=[o for o in g.ops if o.kind=="conv"]
nf=net._needs_f32
cls={}; fb=[]
def kind_of(d):
    if d.producer is None: return "graph_in"
    return d.producer.kind
for o in conv:
    d=o.deps[0]; L=o.layer
    k=kind_of(d)
    if k=="cat": c=net._cat_class.get(d.id,"mixed1")
    elif k=="conv" and d.id not in nf: c="single_i8"
    else: c="single_f32"
    cls[c]=cls.get(c,0)+1
    if c=="single_f32":
        cb=(int(L.cin)+63)//64
        fb.append(dict(name=L.name,cin=int(L.cin),H=int(L.H),W=int(L.W),cb=cb,
                       push_b=cb*L.H*L.W*8, f32_b=4*int(L.cin)*L.H*L.W, k=k,
                       nf=d.id in nf))
print("layer classes:",cls, "sum", sum(cls.values()))
print("fallback n=",len(fb),"push_bytes_total=%.3f MB"%(sum(x["push_b"] for x in fb)/1e6),
      "f32_bytes(sum 4*cin*HW)=%.1f MB"%(sum(x["f32_b"] for x in fb)/1e6))
from collections import Counter
print("producer kinds among fallback:",Counter(x["k"] for x in fb))
print("in _needs_f32 among fallback:",Counter(x["nf"] for x in fb))
for x in sorted(fb,key=lambda z:-z["push_b"])[:8]:
    print("  big:",x["name"][-50:],x["cin"],"%dx%d"%(x["H"],x["W"]),"push %.2f MB"%(x["push_b"]/1e6),x["k"])
# f32 段元素总数（q8 桶的口径）：每层 prepare_segments 的 f32 段 = cin 总和（含被强制物化的 C8 段）
tot_f32_elem=0; tot_f32_seg=0
for o in conv:
    d=o.deps[0]; L=o.layer
    k=kind_of(d)
    if k=="cat":
        arrs=d.producer.deps
        # 叶子：cat 的 dep 里，属于 needs_f32 的叶子、或非 conv 叶子 → f32 段
        for t in arrs:
            if t.producer is None or t.producer.kind!="conv" or t.id in nf:
                tot_f32_elem+=int(t.shape[0])*L.H*L.W; tot_f32_seg+=1
    elif k=="conv" and d.id not in nf:
        pass  # i8 段
    else:
        tot_f32_elem+=int(d.shape[0])*L.H*L.W; tot_f32_seg+=1
print("f32 segs naive(total across layers) =",tot_f32_seg,"elem=%.1f MB"%(tot_f32_elem/1e6*4/1e6))
