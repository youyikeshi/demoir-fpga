"""D3 加强：BIN_CONVPF_PL=1 在没有/坏 fconv 时静默回落，数值逐位等于 CPU 路径。"""
import os, sys
R="/media/felix/Data/coding/demoir-fpga"; WT=R+"/worktrees/v50_impC"
sys.path.insert(0, WT+"/ps/pynq")
os.environ["BIN_CONVPF_PL"]="1"
import numpy as np, bin_net as BN
pack=R+"/pynq_specialized_code/deploy/bin_weights_new"
net=BN.BinNet(pack, engine=None, H=64, W=64, verbose=False)
print("convpf_pl_status:", BN.convpf_pl_status())

names=sorted(net.plan.fp.keys())
print("fp layers:", names)
x=np.random.default_rng(3).random((12,32,32),np.float32)

class Bad1:
    def launch(self,*a,**k): raise RuntimeError("假 fconv：寄存器读不回")
class Bad2:
    def launch(self,*a,**k): return np.zeros((1,1,1),np.float32)   # 形状错
class Bad3: pass                                                   # 没有 launch

for tag, eng in (("launch 抛异常", Bad1()), ("形状错", Bad2()), ("没有 launch", Bad3())):
    net._fconv = eng
    y = net._fp_conv(x, "encoder.conv_first.0", relu=True, pad=2)  # pad 本来就不满足
    os.environ.pop("BIN_CONVPF_PL", None)
    y_ref = net._fp_conv(x, "encoder.conv_first.0", relu=True, pad=2)
    os.environ["BIN_CONVPF_PL"]="1"
    same = np.array_equal(y.view(np.uint32), y_ref.view(np.uint32))
    print("  [%s] 形状=%s 与 CPU 逐位相同=%s" % (tag, y.shape, same))
    assert same, tag

# 挑一个满足硬条件的 fp 层，走 launch 路径（此次故意让 launch 抛错）
for nm in names:
    f=net.plan.fp[nm]; w=net.blob.fp_layer(nm)[0]
    cout,cin,k = w.shape[0], w.shape[1], w.shape[2]
    pad = (k-1)//2
    H=W=32
    if W%32 or k not in (3,5) or not(1<=cin<=64) or not(1<=cout<=48) or cin*k*k>1024: continue
    print("走 launch 的层:", nm, "cin=%d cout=%d k=%d pad=%d"%(cin,cout,k,pad), "bias?", f.get("bias") is not None)
    x2=np.random.default_rng(5).random((cin,H,W),np.float32)
    net._fconv = Bad1()
    y=net._fp_conv(x2, nm, relu=False, pad=pad)
    os.environ.pop("BIN_CONVPF_PL", None); y_ref=net._fp_conv(x2, nm, relu=False, pad=pad); os.environ["BIN_CONVPF_PL"]="1"
    print("  回落结果与 CPU 逐位相同:", np.array_equal(y.view(np.uint32), y_ref.view(np.uint32)))
    assert np.array_equal(y.view(np.uint32), y_ref.view(np.uint32))
    break
print("FALLBACK CHECKS PASS")
