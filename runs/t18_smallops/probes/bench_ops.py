import os,sys,json,time
import numpy as np
HERE='/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/ps'
sys.path.insert(0,os.path.join(HERE,'pynq')); sys.path.insert(0,os.path.join(HERE,'tools'))
os.environ.setdefault('BIN_PS_THREADS','1')
import bin_ref_numpy as RN
S=json.load(open('/tmp/probe_T18/shapes.json'))
rng=np.random.default_rng(7)
EV=np.empty(96<<20,np.uint8)          # L3 驱逐器
def evict():
    EV[:] = 7
def bits(a,b):
    return a.shape==b.shape and a.dtype==b.dtype and a.tobytes()==b.tobytes()

def bench(fn,reps=7):
    fn(); evict(); fn()
    ts=[]
    for _ in range(reps):
        evict(); t=time.perf_counter(); fn(); ts.append(time.perf_counter()-t)
    ts.sort(); return ts[len(ts)//2]

# ---------------- sam ----------------
def sam_cur(x,y0,y2,y4,w0,w2,w4):
    return RN.sam_mix_f32(x,y0,y2,y4,w0,w2,w4)
def sam_tiled(x,y0,y2,y4,w0,w2,w4,slab_bytes=128<<10):
    C,H,W=y0.shape
    out=np.empty(y0.shape,np.float32)
    hw=H*W
    cs=max(1,slab_bytes//(4*hw))          # 通道数/片：每片 (o,t) 两份
    t=None
    for c0 in range(0,C,cs):
        c1=min(C,c0+cs)
        o=out[c0:c1]
        if t is None or t.shape[0]!=(c1-c0): t=np.empty_like(o)
        else: t=t[:c1-c0]
        np.multiply(y0[c0:c1], np.reshape(w0[c0:c1],(-1,1,1)), out=o)
        np.multiply(y2[c0:c1], np.reshape(w2[c0:c1],(-1,1,1)), out=t)
        o+=t
        np.multiply(y4[c0:c1], np.reshape(w4[c0:c1],(-1,1,1)), out=t)
        o+=t
        o+=x[c0:c1]
    return out

print("=== sam (6 ops, 板端 250 ms) ===")
tot_c=tot_t=0.0
for d in S['sam']:
    osh=tuple(d['out']); C,H,W=osh
    x=[rng.standard_normal(osh).astype(np.float32) for _ in range(4)]
    w=[rng.random(3*C).astype(np.float32) for _ in range(1)]
    w0,w2,w4=np.split(w[0],3)
    a=sam_cur(*x,w0,w2,w4); b=sam_tiled(*x,w0,w2,w4)
    ok=bits(a,b)
    # 交替换序
    tc=bench(lambda: sam_cur(*x,w0,w2,w4)); tt=bench(lambda: sam_tiled(*x,w0,w2,w4))
    tot_c+=tc; tot_t+=tt
    print(f"  {osh} cur={tc*1e3:7.2f}ms tiled={tt*1e3:7.2f}ms ratio={tc/tt:5.2f} bitexact={ok}")
print(f"  TOTAL cur={tot_c*1e3:.2f}ms tiled={tot_t*1e3:.2f}ms ratio={tot_c/tot_t:.2f}")

# ---------------- cat3 + eca ----------------
print("=== cat3+eca (6 ops, 板端 cat 158 + eca 78 ms) ===")
def cat_cur(arrs):
    return np.concatenate(arrs,0)
def mean_cur(t):
    return t.astype(np.float32).mean(axis=(1,2))
def mean_nocopy(t):
    return np.asarray(t,np.float32).mean(axis=(1,2))
def mean_leaf(arrs):
    return np.concatenate([np.asarray(a,np.float32).mean(axis=(1,2)) for a in arrs],0)
tot1=tot2=tot3=tot4=0.0
blk=[c for c in S['cat'] if c['cls']=='blocked']
for d in blk:
    arrs=[rng.standard_normal(tuple(s)).astype(np.float32) for s in d['deps']]
    cat=cat_cur(arrs)
    a=mean_cur(cat); b=mean_leaf(arrs)
    ok=bits(a,b)
    t_cat=bench(lambda: cat_cur(arrs))
    t_m1=bench(lambda: mean_cur(cat))
    t_m2=bench(lambda: mean_nocopy(cat))
    t_leaf=bench(lambda: mean_leaf(arrs))
    tot1+=t_cat; tot2+=t_m1; tot3+=t_m2; tot4+=t_leaf
    print(f"  {tuple(d['out'])} cat={t_cat*1e3:6.2f} mean(astype)={t_m1*1e3:6.2f} mean(asarray)={t_m2*1e3:6.2f} leafmean={t_leaf*1e3:6.2f} bitexact={ok}")
print(f"  TOTAL cat={tot1*1e3:.2f} + eca(astype)={tot2*1e3:.2f} = {(tot1+tot2)*1e3:.2f}ms | fused(leafmean)={tot4*1e3:.2f}ms ratio={(tot1+tot2)/tot4:.2f}")

# ---------------- add ----------------
print("=== add (6 ops, 板端 67 ms) ===")
tot=0.0
for d in S['add']:
    a=rng.standard_normal(tuple(d['deps'][0])).astype(np.float32)
    b=rng.standard_normal(tuple(d['deps'][1])).astype(np.float32)
    t=bench(lambda: RN.add_f32(a,b)); tot+=t
    print(f"  {tuple(d['out'])} {t*1e3:6.2f}ms  ({(a.nbytes*3)/t/1e9:.2f} GB/s 单核主机)")
print(f"  TOTAL {tot*1e3:.2f}ms")

# ---------------- shuffle/unshuffle ----------------
print("=== shuffle/unshuffle (板端 19.5 ms) ===")
import ops_numpy as ON
tot=0.0
for d in S['shuffle']+S['unshuffle']:
    a=rng.standard_normal(tuple(d['deps'][0])).astype(np.float32)
    fn=(lambda: ON.pixel_shuffle(a,2)) if 'shuffle'==d.get('kind','') or len(d['out'])==3 and d['out'][0]<d['deps'][0][0] else None
    kind='shuffle' if d['out'][0]<d['deps'][0][0] else 'unshuffle'
    fn=(lambda: ON.pixel_shuffle(a,2)) if kind=='shuffle' else (lambda: ON.pixel_unshuffle(a,2))
    t=bench(fn); tot+=t
    print(f"  {kind} {tuple(d['deps'][0])}->{tuple(d['out'])} {t*1e3:6.2f}ms")
print(f"  TOTAL {tot*1e3:.2f}ms")
