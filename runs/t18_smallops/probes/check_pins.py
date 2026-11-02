import os,sys
WT="/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code"
sys.path.insert(0,os.path.join(WT,"ps","pynq")); sys.path.insert(0,os.path.join(WT,"ps","tools"))
import numpy as np, verify_lazy_net as V, bin_net as BN
PACK=os.path.join(WT,"deploy","bin_weights_new")
x0=np.random.default_rng(1).random((3,64,64),dtype=np.float32)
for env,label in ((None,"off"),({"BIN_CAT_ECA_FUSE":"1"},"K1")):
    if env:
        for k,v in env.items(): os.environ[k]=v
    else:
        os.environ.pop("BIN_CAT_ECA_FUSE",None)
    sha,st,unit=V.run_case(PACK,64,"e1e2_lazy",2,10,x0,verbose=False)
    print(label,"sha",sha[0],"lazy",st.get("lazy"))
# 直接跑一次拿 net 对象，检查 _catpins 是否留残
import bin_engine as BE
os.environ["BIN_CAT_ECA_FUSE"]="1"
save={k:os.environ.get(k) for k in ("BIN_PACK","BIN_LAZY_DEQ","BIN_LAZY_SLOTS")}
os.environ.update(BIN_PACK="e1e2",BIN_LAZY_DEQ="1",BIN_LAZY_SLOTS="10")
for k,v in save.items():
    if v is not None: os.environ[k]=v
blob=__import__("bin_ref_numpy").Blob(PACK)
flat=BE.MemBuf(192<<20,0x30000000); unit=V.FakePLUnit(blob); unit.map_mem(0x30000000,flat)
old=BE._pynq_allocate; alloc=__import__("verify_e1e2_direct").SubAlloc(flat,0x30000000,start=0); BE._pynq_allocate=alloc
eng=BE.BinConvEngine(unit,pack_dir=PACK,verbose=False); eng.load_weights()
_orig=BE.BinConvEngine.start
def start(self):
    unit.cur_layer=self.cfg; return _orig(self)
BE.BinConvEngine.start=start
net=BN.BinNet(PACK,engine=eng,H=64,W=64,verbose=False)
outs,st=net.run(x0,pipeline=True,nbuf=2)
BE.BinConvEngine.start=_orig; BE._pynq_allocate=old
print("跑完后 _catpins 残留 =",len(net._catpins), net._catpins)
print("cat stats fused/fallback =",st["cat"]["fused"],st["cat"]["fallback"],st["cat"]["fallback_reason"])
