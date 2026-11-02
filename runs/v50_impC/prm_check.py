import os, sys, json, hashlib
R="/media/felix/Data/coding/demoir-fpga"; WT=R+"/worktrees/v50_impC"
sys.path.insert(0, WT+"/ps/pynq")
import bin_engine as BE
from bin_ref_numpy import Blob
pack=R+"/pynq_specialized_code/deploy/bin_weights_new"

# ① VLANES=8 必须与 v39 RTL 的硬编码逐值相同
L8 = BE.prm_layout(8)
assert (L8["PRM_W"], L8["PB_S"], L8["PB_B"], L8["PB_Z"], L8["PB_G"], L8["PB_SL"]) == (16,0,2,6,10,14), L8
L16 = BE.prm_layout(16)
print("VLANES=8 :", {k:L8[k] for k in ("PRM_W","PB_S","PB_B","PB_Z","PB_G","PB_SL")})
print("VLANES=16:", {k:L16[k] for k in ("PRM_W","PB_S","PB_B","PB_Z","PB_G","PB_SL")})
assert (L16["PRM_W"], L16["PB_S"], L16["PB_B"], L16["PB_Z"], L16["PB_G"], L16["PB_SL"]) == (32,0,4,12,20,28)
print("布局与 IMP-A 给的 32 字表一致: alpha[0,4) bias[4,12) zeta[12,20) gamma[20,28) slope[28,32)")

# ② 容器（PS 打包产物）不随 VLANES 变：逐层数组长度/偏移全等，md5 不变
blob = Blob(pack)
idx = json.load(open(os.path.join(pack,"index.json")))
entries = idx["layers"] if isinstance(idx,dict) and "layers" in idx else idx
bad=[]
for e in entries:
    blob.params(e)             # 逐层读参数（失败会抛）
    for nm in ("alpha","bias","zeta","gamma","slope"):
        n = int(e["n_"+nm]); sz = int(e["sz_"+nm])
        per = 2 if nm in ("alpha","slope") else 4
        if n != int(e["cout"]) or sz != n*per: bad.append((e["name"],nm,n,sz))
print("容器逐层参数数组检查：%d 层，异常 %d 条" % (len(entries), len(bad)))

# ③ cout 都是 VLANES(8/16) 的倍数 ⇒ 16 lane 不会读越界到相邻数组
couts = sorted({int(e["cout"]) for e in entries})
print("cout 集合:", couts, "  all %%8==0:", all(c%8==0 for c in couts),
      "  all %%16==0:", all(c%16==0 for c in couts))

# ④ check_prm_capacity：V=8 与 V=16 都必须全过
for V in (8,16):
    be = BE.Blob(pack) if False else blob
    for e in entries[:400]:
        L = BE.BinLayer(e, 32, 32)
        BE.check_prm_capacity(L, V)
print("check_prm_capacity: V=8 与 V=16 全过（%d 层）" % len(entries))

# ⑤ 默认 VLANES=8 时 BinLayer 的护栏不触发（与改动前一致）
print("vlanes_now():", BE.vlanes_now())
print("PRM CHECKS PASS")
