import os,sys,json,time,cProfile,pstats,io
import numpy as np
HERE='/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/ps'
sys.path.insert(0,os.path.join(HERE,'pynq')); sys.path.insert(0,os.path.join(HERE,'tools'))
os.environ.setdefault('BIN_PS_THREADS','1')
import bin_ref_numpy as RN
rng=np.random.default_rng(11)
EV=np.empty(256<<20,np.uint8)
def evict(): EV[:]=3
def bench(fn,reps=5):
    fn(); evict(); ts=[]
    for _ in range(reps):
        evict(); t=time.perf_counter(); fn(); ts.append(time.perf_counter()-t)
    ts.sort(); return ts[len(ts)//2]

def sam_tiled(x,y0,y2,y4,w0,w2,w4,slab=192<<10):
    C,H,W=y0.shape; out=np.empty(y0.shape,np.float32); hw=H*W
    cs=max(1,slab//(4*hw)); t=None
    for c0 in range(0,C,cs):
        c1=min(C,c0+cs); o=out[c0:c1]
        if t is None or t.shape[0]!=(c1-c0): t=np.empty_like(o)
        np.multiply(y0[c0:c1],np.reshape(w0[c0:c1],(-1,1,1)),out=o)
        np.multiply(y2[c0:c1],np.reshape(w2[c0:c1],(-1,1,1)),out=t); o+=t
        np.multiply(y4[c0:c1],np.reshape(w4[c0:c1],(-1,1,1)),out=t); o+=t
        o+=x[c0:c1]
    return out

print("== 放大尺寸（工作集 >> 主机 L3）验证 15S→5S 模型 ==")
for C,H,W in ((192,256,256),(384,128,128)):
    osh=(C,H,W); S=C*H*W*4/1e6
    x=[rng.standard_normal(osh).astype(np.float32) for _ in range(4)]
    w0=rng.random(C).astype(np.float32);w2=rng.random(C).astype(np.float32);w4=rng.random(C).astype(np.float32)
    a=RN.sam_mix_f32(*x,w0,w2,w4); b=sam_tiled(*x,w0,w2,w4)
    ok=a.tobytes()==b.tobytes()
    tc=bench(lambda: RN.sam_mix_f32(*x,w0,w2,w4)); tt=bench(lambda: sam_tiled(*x,w0,w2,w4))
    print(f"  sam {osh} S={S:.1f}MB cur={tc*1e3:.1f}ms({15*S/tc/1e3:.2f}GB/s) tiled={tt*1e3:.1f}ms({5*S/tt/1e3:.2f}GB/s) ratio={tc/tt:.2f} bit={ok}")
    # cat+eca 放大
    arrs=[rng.standard_normal((C,H,W)).astype(np.float32) for _ in range(3)]
    cat=np.concatenate(arrs,0)
    t1=bench(lambda: np.concatenate(arrs,0))
    t2=bench(lambda: cat.astype(np.float32).mean(axis=(1,2)))
    t3=bench(lambda: np.concatenate([a.mean(axis=(1,2)) for a in arrs],0))
    ok2=(cat.astype(np.float32).mean(axis=(1,2)).tobytes()==np.concatenate([a.mean(axis=(1,2)) for a in arrs],0).tobytes())
    print(f"  cat+eca {osh}x3 cat={t1*1e3:.1f}ms mean={t2*1e3:.1f}ms fused={t3*1e3:.1f}ms ratio={(t1+t2)/t3:.2f} bit={ok2}")

print("\n== cProfile 交叉验证（与 perf_counter 比，±2%）==")
S2=json.load(open('/tmp/probe_T18/shapes.json'))
def work():
    tot=0.0
    for d in S2['sam']:
        osh=tuple(d['out']); C,H,W=osh
        x=[rng.standard_normal(osh).astype(np.float32) for _ in range(4)]
        w=[rng.random(C).astype(np.float32) for _ in range(3)]
        RN.sam_mix_f32(*x,*w)
    for d in S2['cat']:
        if d['cls']!='blocked': continue
        arrs=[rng.standard_normal(tuple(s)).astype(np.float32) for s in d['deps']]
        c=np.concatenate(arrs,0)
        c.astype(np.float32).mean(axis=(1,2))
    for d in S2['add']:
        a=rng.standard_normal(tuple(d['deps'][0])).astype(np.float32)
        b=rng.standard_normal(tuple(d['deps'][1])).astype(np.float32)
        RN.add_f32(a,b)
t0=time.perf_counter(); work(); t_pc=time.perf_counter()-t0
pr=cProfile.Profile(); pr.enable(); work(); pr.disable()
s=io.StringIO(); pstats.Stats(pr,stream=s).sort_stats('tottime').print_stats(6)
cum=[l for l in s.getvalue().splitlines() if 'function calls' in l or 'numpy' in l or 'built-in' in l][:6]
tot_cp=sum(st[3] for st in pstats.Stats(pr).stats.values())
print(f"  perf_counter 合计 = {t_pc*1e3:.1f} ms ; cProfile tottime 合计 = {tot_cp*1e3:.1f} ms ; Δ={100*(tot_cp-t_pc)/t_pc:+.1f}%")
print("  (打印下面的 top 项只为确认没有隐藏的 Python 层开销)")
for l in cum: print("   ",l[:150])
