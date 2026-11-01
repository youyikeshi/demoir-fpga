import random
# 逐拍模型：只建模 §13/§14 的 valid 链与累加（popcount 值当作外部给定）
# old: ps_vld/p_acc 直接吃 r1_*;   new: 中间插 s1_*_q
def run(terms, new):
    # terms: list of (vld, last, l0, oaddr, pc) 对应 r1_* 在每一拍的值
    P=[];S=[];O=[];V=[]
    p_acc=0; p_snap=0; osnap=0; ps_vld=0
    s_pc=0; s_v=0; s_last=0; s_l0=1; s_oa=0
    for (vld,last,l0,oa,pc) in terms:
        # 本拍末值
        P.append(p_acc);S.append(p_snap);O.append(osnap);V.append(ps_vld)
        if new:
            ns=(p_acc if not s_l0 else 0)+s_pc
            n_ps = s_last
            n_pacc = ns if s_v else p_acc
            n_psnap = ns if (s_v and s_last) else p_snap
            n_osnap = s_oa if (s_v and s_last) else osnap
            # 寄存器
            s_pc = pc if vld else s_pc
            s_v  = vld
            s_last = vld and last
            s_l0 = l0 if vld else s_l0
            s_oa = oa if vld else s_oa
        else:
            ns=(p_acc if not l0 else 0)+pc
            n_ps = vld and last
            n_pacc = ns if vld else p_acc
            n_psnap = ns if (vld and last) else p_snap
            n_osnap = oa if (vld and last) else osnap
        p_acc=n_pacc; p_snap=n_psnap; osnap=n_osnap; ps_vld=n_ps
    P.append(p_acc);S.append(p_snap);O.append(osnap);V.append(ps_vld)
    return P,S,O,V

random.seed(7)
bad=0
for trial in range(200):
    nterms=random.randint(1,40)
    terms=[]
    l=0
    for i in range(nterms):
        terms.append((1, i==nterms-1, l==0, 0x1000+8*(i//3), random.randint(0,64)))
        l=l+1
        if random.random()<0.2: l=0
    # 真实 CMP 之后第一拍 DRN：r1_vld 仍为 1（state==CMP 时登记的），随后为 0
    terms.append((1, terms[-1][1], terms[-1][2], terms[-1][3], random.randint(0,64)))
    terms += [(0,1,0,0,0)]*6   # DRN 里 r1_vld=0（r1_last 等保持旧值）
    Po,So,Oo,Vo = run(terms, False)
    Pn,Sn,On,Vn = run(terms, True)
    # new 应等于 old 整体右移 1 拍（ps_vld/p_snap/osnap/p_acc 稳态相同）
    for k in range(1, len(terms)):
        if Vo[k]!=Vn[k+1] or So[k]!=Sn[k+1] or Oo[k]!=On[k+1] or Po[k]!=Pn[k+1]:
            bad+=1
            print("MISMATCH trial",trial,"k",k,(Vo[k],So[k],Oo[k],Po[k]),(Vn[k+1],Sn[k+1],On[k+1],Pn[k+1]))
            break
print("trials=200 mismatches=",bad)
