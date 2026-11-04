import sys, numpy as np
sys.path.insert(0,'/media/felix/Data/coding/demoir-fpga/runs/opt_round1/ps_numeric/p0067_divergence')
import diverge_probe as D
D.install_probes()
import bin_net as BN, bin_ref_numpy as RB
img=D.load_input('/media/felix/Data/coding/demoir-fpga/runs/frozen256/p0067_crop256.npy')
a=D.run_cfg(img,{},'a')['out1']
x=np.nextafter(img,np.float32(np.inf)).astype(np.float32)
b=D.run_cfg(x,{},'b')['out1']
bn_conv=BN.conv2d_f32_np; BN.conv2d_f32_np=BN.conv2d_f32_ref
try:
    c=D.run_cfg(img,{},'c')['out1']
finally:
    BN.conv2d_f32_np=bn_conv
ua,ub,uc=D.u8(a),D.u8(b),D.u8(c)
print('ulp==convref  u8 equal:',bool(np.array_equal(ub,uc)),'diff px:',int((ub.astype(int)-uc.astype(int)!=0).sum()))
print('ulp vs base:',int((ub.astype(int)-ua.astype(int)!=0).sum()),'convref vs base:',int((uc.astype(int)-ua.astype(int)!=0).sum()))
print('f32 exact equal ulp/convref:',bool(np.array_equal(b,c)))
