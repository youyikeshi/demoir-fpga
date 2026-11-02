import os,sys,json,time,cProfile,pstats
import numpy as np
HERE='/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/ps'
sys.path.insert(0,os.path.join(HERE,'pynq')); sys.path.insert(0,os.path.join(HERE,'tools'))
os.environ.setdefault('BIN_PS_THREADS','1')
import bin_ref_numpy as RN
rng=np.random.default_rng(3)
S2=json.load(open('/tmp/probe_T18/shapes.json'))
# 预分配（不在计时区内分配输入）
SAM=[]
for d in S2['sam']:
    osh=tuple(d['out']); C=osh[0]
    SAM.append(([rng.standard_normal(osh).astype(np.float32) for _ in range(4)],
                [rng.random(C).astype(np.float32) for _ in range(3)]))
BLK=[]
for d in S2['cat']:
    if d['cls']!='blocked': continue
    BLK.append([rng.standard_normal(tuple(s)).astype(np.float32) for s in d['deps']])
ADD=[]
for d in S2['add']:
    ADD.append((rng.standard_normal(tuple(d['deps'][0])).astype(np.float32),
                rng.standard_normal(tuple(d['deps'][1])).astype(np.float32)))
EV=np.empty(256<<20,np.uint8)
def evict(): EV[:]=3
def run_sam():
    for x,w in SAM: RN.sam_mix_f32(*x,*w)
def run_cateca():
    for a in BLK:
        c=np.concatenate(a,0)
        c.astype(np.float32).mean(axis=(1,2))
def run_add():
    for a,b in ADD: RN.add_f32(a,b)
def run_all():
    run_sam(); run_cateca(); run_add()
def pc(fn,reps=5):
    fn(); evict(); ts=[]
    for _ in range(reps):
        evict(); t=time.perf_counter(); fn(); ts.append(time.perf_counter()-t)
    ts.sort(); return ts[len(ts)//2]
base={'sam':pc(run_sam),'cat+eca':pc(run_cateca),'add':pc(run_add),'ALL':pc(run_all)}
# cProfile：按"self/累计"取每个 numpy 入口的时间
pr=cProfile.Profile(); pr.enable()
for _ in range(3): run_all()
pr.disable()
st=pstats.Stats(pr)
tot_cp=sum(v[3] for v in st.stats.values())/3
per={}
for (f,l,n),(cc,nc,tt,ct,callers) in st.stats.items():
    if n in ('multiply','add','reduce','concatenate','astype','negative','sqrt','_copyto') or 'bin_ref_numpy' in f:
        per.setdefault(n,0.0); per[n]+=tt/3
print("== 方法1 perf_counter（同进程、交替、中位）==")
for k,v in base.items(): print(f"   {k:8} {v*1e3:8.2f} ms")
print("   Σ(sam+cat+eca+add) = %.2f ms  vs  ALL = %.2f ms  (Δ %+.1f%%)"%(1e3*(base['sam']+base['cat+eca']+base['add']),1e3*base['ALL'],100*(base['ALL']-(base['sam']+base['cat+eca']+base['add']))/base['ALL']))
print("== 方法2 cProfile（tottime ÷3 次）==")
print(f"   合计 {tot_cp*1e3:.2f} ms ; perf_counter/3 = {base['ALL']*1e3:.2f} ms ; Δ {100*(tot_cp-base['ALL'])/base['ALL']:+.1f}%")
for k,v in sorted(per.items(),key=lambda kv:-kv[1])[:8]: print(f"   {k:14} {v*1e3:8.2f} ms")
