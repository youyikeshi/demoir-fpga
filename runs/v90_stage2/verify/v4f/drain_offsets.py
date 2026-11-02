import os,sys,numpy as np
ROOT="/media/felix/Data/coding/demoir-fpga"; PS=os.path.join(ROOT,"pynq_specialized_code")
sys.path.insert(0,os.path.join(PS,"ps","pynq")); sys.path.insert(0,os.path.join(PS,"ps","tools"))
os.environ["BIN_V50_FUSE"]="1"
for k in ("BIN_PACK","BIN_LAZY_DEQ","BIN_LAZY_SLOTS"): os.environ.pop(k,None)
os.environ["BV_FREE"]="1"
import bin_net as BN, bin_engine as BE
PACK=os.path.join(PS,"deploy","bin_weights_new")
x0=np.random.default_rng(1).random((3,64,64),dtype=np.float32)
net=BN.BinNet(PACK,engine=None,H=64,W=64,verbose=False); net.run(x0,pipeline=True,nbuf=2)
p=net._segplan
from collections import Counter
c=Counter(); bad=[]
for s in p.segments:
    if not s.fused: continue
    bands=s.bands; facet=(int(s.c_out)+7)//8
    word_per_band=facet*min(p.band_rows,int(s.h_out)-(bands-1)*p.band_rows)*0+0
    ok = (bands==1)          # 单带时 band 行==张量行，facet 间距也恰好相等
    c[(int(s.h_out),bands,facet)]+=1
    if not ok: bad.append((s.seg_id,int(s.h_out),int(s.c_out),bands,facet,int(s.w_in)))
print("fused 段 (h_out, bands, facets) 分布:",dict(c))
print("排空需要带偏移的段（bands>1）:",len(bad),"/",sum(c.values()))
for b in bad[:6]:
    sid,h,co,bands,facet,W=b
    band_words=facet*min(p.band_rows,h)*W
    print("  S%-3d h_out=%d cout=%d bands=%d facets=%d W=%d | 每带 %d 字=%d B, 张量 %d B"%
          (sid,h,co,bands,facet,W,band_words,band_words*8,facet*h*W*8))
