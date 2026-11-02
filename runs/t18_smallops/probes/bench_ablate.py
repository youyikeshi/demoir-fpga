import os,sys,json,time
import numpy as np
HERE='/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/ps'
sys.path.insert(0,os.path.join(HERE,'pynq')); sys.path.insert(0,os.path.join(HERE,'tools'))
os.environ.setdefault('BIN_PS_THREADS','1')
import bin_ref_numpy as RN
S2=json.load(open('/tmp/probe_T18/shapes.json')); rng=np.random.default_rng(5)
SAM=[([rng.standard_normal(tuple(d['out'])).astype(np.float32) for _ in range(4)],
      [rng.random(tuple(d['out'])[0]).astype(np.float32) for _ in range(3)]) for d in S2['sam']]
BLK=[[rng.standard_normal(tuple(s)).astype(np.float32) for s in d['deps']] for d in S2['cat'] if d['cls']=='blocked']
ADD=[(rng.standard_normal(tuple(d['deps'][0])).astype(np.float32),
      rng.standard_normal(tuple(d['deps'][1])).astype(np.float32)) for d in S2['add']]
EV=np.empty(256<<20,np.uint8)
def f_sam():
    for x,w in SAM: RN.sam_mix_f32(*x,*w)
def f_cateca():
    for a in BLK:
        c=np.concatenate(a,0); c.astype(np.float32).mean(axis=(1,2))
def f_add():
    for a,b in ADD: RN.add_f32(a,b)
def make(skip):
    def f():
        if skip!='sam': f_sam()
        if skip!='cat': f_cateca()
        if skip!='add': f_add()
    return f
arms={'ALL':make(None),'-sam':make('sam'),'-cat+eca':make('cat'),'-add':make('add')}
def pc(fn,reps=7):
    EV[:]=3; fn(); ts=[]
    for _ in range(reps): EV[:]=3; t=time.perf_counter(); fn(); ts.append(time.perf_counter()-t)
    ts.sort(); return ts[len(ts)//2]
res={k:pc(v) for k,v in arms.items()}
print("== 消融（同进程、交替、中位）：在整合上下文里的边际成本 ==")
base=res['ALL']
for k in ('-sam','-cat+eca','-add'):
    print(f"   {k:9} {res[k]*1e3:7.2f} ms  ⇒ 边际 = {(base-res[k])*1e3:7.2f} ms")
print(f"   ALL      {base*1e3:7.2f} ms ; Σ边际 = {(3*base-sum(res[k] for k in ('-sam','-cat+eca','-add')))*1e3:.2f} ms (Δ {100*(3*base-sum(res.values()))/base:+.1f}%)")
# 逐桶"独立"测量（每次先驱逐）与边际对比
for name,fn in (('sam',f_sam),('cat+eca',f_cateca),('add',f_add)):
    print(f"   独立 {name:8} {pc(fn)*1e3:7.2f} ms")
