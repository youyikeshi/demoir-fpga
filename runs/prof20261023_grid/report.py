# -*- coding: utf-8 -*-
"""最终报告：系数、247 层比对、44.2s 拆解、RTL 建议的预算检查。"""
import json, math
import numpy as np
F=150.0e6
def ceil8(x): return ((x+7)//8)*8
def geom(H,W,cin,cout,k,s,d=1):
    pad=(k-1)*d//2; hout=(H-1)//s+1; wout=(W-1)//s+1; kk=k*k
    span=2*pad+1; bw=32 if s==2 else 64; bwcol=(bw-1)*s+span+2*pad
    px=hout*wout; fill=shift=wgt=zpad=0.0; blocks=0
    gc0=-pad
    for x0 in range(0,wout,bw):
        xn=min(bw,wout-x0); la=min(max(0,-gc0),bwcol); lb=min(max(0,W-gc0-la),bwcol-la)
        rv=False
        for y in range(hout):
            need0=y*s-pad; keep=max(0,span-s)
            if rv and keep>0: shift+=keep*cin*bwcol
            rb=keep if (rv and keep>0) else 0
            for r in range(rb,span):
                iy=need0+r
                if 0<=iy<H: fill+=ceil8(cin)*lb
            zpad+=(la+(bwcol-la-lb))*cin*(span-rb)
            rv=True; blocks+=1; wgt+=cin*cout*kk
        gc0+=bw*s
    cont=1.0 if k==1 else (12/9 if k==3 else 28/25)
    return dict(px=px,fill=fill,shift=shift,zpad=zpad,wgt=wgt*cont,blocks=blocks,
                tail=(8-cout%8)*px if cout%8 else 0.0,
                comp=px*cin*ceil8(cout)*kk, write=px*ceil8(cout),
                batches=px*(ceil8(cout)//8)*kk)
C=json.load(open('model_coef_final.json'))
k1=C['k1']; k3=dict(comp=C['k3']['comp'],wgt=C['k3']['wgt'],shift=C['k3']['shift'],batch=0.0); k5=C['k5']; CONST=C['const']
def predict(H,W,cin,cout,k,s):
    g=geom(H,W,cin,cout,k,s)
    t=CONST+g['fill']*k1['fill']+g['zpad']*k1['zpad']+g['write']*k1['write'] \
      +g['blocks']*k1['blocks']+g['tail']*k1['tail']
    if k==1:
        return t+g['wgt']*k1['wgt']+g['comp']*k1['comp'], g
    if k==3:
        return t+g['wgt']*k3['wgt']+g['comp']*k3['comp']+g['shift']*k3['shift'], g
    return t+g['wgt']*k5['wgt']+g['comp']*k5['comp']+g['shift']*k5['shift'], g

man=json.load(open('../../pynq_specialized_code/ps/manifest_640.json'))
prof=json.load(open('prof_layer_delivered.json'))
meas={l['name']:l['sec'] for l in prof['layers'] if l['op']=='conv'}
rows=[]
for o in man['ops']:
    if o.get('op')!='conv' or o['name'] not in meas: continue
    t,g=predict(o['h'],o['w'],o['cin'],o['cout'],o['k'],o['s'])
    rows.append(dict(name=o['name'],H=o['h'],W=o['w'],cin=o['cin'],cout=o['cout'],k=o['k'],s=o['s'],
                     meas=meas[o['name']],pred=t/F,g=g))
M=np.array([r['meas'] for r in rows]); P=np.array([r['pred'] for r in rows])
rel=np.abs(P-M)/M
print("== 247 层：实测 %.3f s  模型 %.3f s  误差 %+.2f%%"%(M.sum(),P.sum(),100*(P.sum()-M.sum())/M.sum()))
print("   逐层中位 %.1f%% | 75%%分位 %.1f%% | 90%%分位 %.1f%% | 最大 %.1f%% | <5%%: %.0f%% | <10%%: %.0f%%"%(
  100*np.median(rel),100*np.percentile(rel,75),100*np.percentile(rel,90),100*rel.max(),
  100*(rel<0.05).mean(),100*(rel<0.10).mean()))
for kk in (1,3,5):
    sel=[r for r in rows if r['k']==kk]; m=sum(r['meas'] for r in sel); p=sum(r['pred'] for r in sel)
    rr=np.abs(np.array([r['pred'] for r in sel])-np.array([r['meas'] for r in sel]))/np.array([r['meas'] for r in sel])
    print("   k=%d n=%3d 实测 %6.2fs 模型 %6.2fs %+6.2f%%  逐层中位 %4.1f%% 最大 %5.1f%%"%(
        kk,len(sel),m,p,100*(p-m)/m,100*np.median(rr),100*rr.max()))
print("\n== 误差最大的 12 层（都是 20/40/80 的小层）")
for r in sorted(rows,key=lambda r:-abs(r['pred']-r['meas'])/r['meas'])[:12]:
    print("   %-50s %3dx%-3d c%-3d o%-3d k%d  实测 %6.2fms 模型 %6.2fms (%+5.1f%%)"%(
        r['name'][:50],r['H'],r['W'],r['cin'],r['cout'],r['k'],r['meas']*1e3,r['pred']*1e3,
        100*(r['pred']-r['meas'])/r['meas']))

# ---- 拆解 ----
ph=dict()
def add(k,v): ph[k]=ph.get(k,0.0)+v
for r in rows:
    g=r['g']
    add('fill(填充读 DDR)',g['fill']*k1['fill'])
    add('write(写回 DDR)',g['write']*k1['write'])
    add('zpad(部分块右补零)',g['zpad']*k1['zpad'])
    add('tail(尾部补零)',g['tail']*k1['tail'])
    add('blocks(每块固定)',g['blocks']*k1['blocks'])
    add('const(每层启动)',CONST)
    if r['k']==1:
        add('compute(k=1 MAC)',g['comp']*k1['comp']); add('wgt(k=1 权重重装)',g['wgt']*k1['wgt'])
    elif r['k']==3:
        add('compute(k=3 MAC)',g['comp']*k3['comp']); add('wgt(k=3 权重重装)',g['wgt']*k3['wgt'])
        add('shift(k=3 行缓冲下移)',g['shift']*k3['shift']); add('batch(k=3 批开销)',g['batches']*k3['batch'])
    else:
        add('compute(k=5 MAC)',g['comp']*k5['comp']); add('wgt(k=5 权重重装)',g['wgt']*k5['wgt'])
        add('shift(k=5)',g['shift']*k5['shift'])
print("\n== conv 时间拆解（模型 %.2f s，实测 44.21 s）"%(P.sum()))
for n,v in sorted(ph.items(),key=lambda kv:-kv[1]):
    print("   %-26s %6.2f s  %5.1f%%"%(n,v/F,100*v/(P.sum()*F)))
print("   %-26s %6.2f s" % ("合计", sum(ph.values())/F))
json.dump(rows,open('layers_model_final.json','w'),indent=1)
json.dump(ph,open('phase_breakdown.json','w'),indent=1)

# ---- 预算检查 ----
print("\n== 资源预算（delivered impl: BRAM 86.5/95, DSP 115/160, LUT 25913/46200=56%, WNS -0.184@150）")
for name,(dlut,ddsp,dbram) in {
    'A k=1 车道 8->16（LUT 乘法器）':(8*41+450,0,0),
    'B k=1 权重装 4->8 字节读 + 填充 8 字节读':(0,0,0),
    'C OC_BLOCK 8->16（权重重装减半）':(900,0,0),
}.items():
    lut=25913+dlut; dsp=115+ddsp; bram=86.5+dbram
    print("   %-40s LUT %5d (%4.1f%%)  DSP %3d (%4.1f%%)  BRAM %4.1f (%4.1f%%)  %s"%(
        name,lut,100*lut/46200,dsp,100*dsp/160,bram,100*bram/95,
        "OK" if (lut<=0.85*46200 and dsp<=160 and bram<=0.95*95) else "超预算"))
