import os,sys,json,collections
import numpy as np
HERE='/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/ps'
sys.path.insert(0,os.path.join(HERE,'pynq')); sys.path.insert(0,os.path.join(HERE,'tools'))
import bin_net as BN
PACK=os.path.join('/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code/deploy/bin_weights_new')
def inv(hw):
    net=BN.BinNet(PACK,engine=None,H=hw,W=hw,verbose=False)
    g,outs,tin=net.build_graph(np.zeros((3,hw,hw),np.float32))
    agg=collections.defaultdict(lambda:dict(n=0,rb=0,wb=0,maxrb=0,shapes=[]))
    for o in g.ops:
        k=o.kind
        rb=sum(int(np.asarray(d.shape).prod())*0 if False else int(np.prod(d.shape, dtype=np.int64))*4 for d in o.deps if d.shape is not None)
        wb=int(np.prod(o.out.shape,dtype=np.int64))*4
        a=agg[k]; a['n']+=1; a['rb']+=rb; a['wb']+=wb
        a['maxrb']=max(a['maxrb'],rb)
        if len(a['shapes'])<3: a['shapes'].append((tuple(o.out.shape), [tuple(d.shape) for d in o.deps]))
    print(f"=== hw={hw}  ops={len(g.ops)} ===")
    tot_r=tot_w=0
    for k,a in sorted(agg.items(), key=lambda kv:-(kv[1]['rb']+kv[1]['wb'])):
        tot_r+=a['rb']; tot_w+=a['wb']
        print(f"{k:9} n={a['n']:4d} rd={a['rb']/1e6:9.2f}MB wr={a['wb']/1e6:9.2f}MB tot={(a['rb']+a['wb'])/1e6:9.2f}MB  per-op={(a['rb']+a['wb'])/a['n']/1e6:6.2f}MB  e.g. {a['shapes'][:1]}")
    print(f"{'TOTAL':9} rd={tot_r/1e6:.2f}MB wr={tot_w/1e6:.2f}MB tot={(tot_r+tot_w)/1e6:.2f}MB")
    # needs_f32 conv layers
    nf=net._needs_f32
    conv=[o for o in g.ops if o.kind=='conv']
    print("conv ops:",len(conv),"| conv outs in needs_f32:",sum(1 for o in conv if o.out.id in nf))
    for o in g.ops:
        if o.kind in ('add','sam','eca','convfp','interp','shuffle','unshuffle'):
            pass
    return net,g
for hw in (64,256):
    inv(hw)
