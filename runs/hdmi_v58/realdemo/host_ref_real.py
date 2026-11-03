import os, sys, time, hashlib, json
import numpy as np
sys.path.insert(0, 'pynq_specialized_code/ps/pynq')
os.environ["BV_FREE"] = "1"
os.environ.pop("BIN_PACK", None)
from bin_net import BinNet
real = np.load('runs/hdmi_v58/realdemo/real_in_0000_256.npy')
x = np.ascontiguousarray(real.transpose(2,0,1), dtype=np.float32) / np.float32(255.0)
t0=time.time()
net = BinNet('pynq_specialized_code/deploy/bin_weights_new', engine=None, H=256, W=256)
outs, st = net.run(x, pipeline=False)
o = outs["out1"]
print("host ref out1 sha1=%s min=%.4f max=%.4f mean=%.4f  run_s=%.1f"
      % (hashlib.sha1(np.ascontiguousarray(o).tobytes()).hexdigest(), o.min(), o.max(), o.mean(), time.time()-t0), flush=True)
np.save('/tmp/probe_v58_real_r1/host_ref_out1.npy', np.ascontiguousarray(o, np.float32))
u8 = (np.clip(o,0,1).transpose(1,2,0)*np.float32(255.0)).round().astype(np.uint8)
print("host ref u8 sha1=%s mean=%.2f" % (hashlib.sha1(u8.tobytes()).hexdigest(), u8.mean()), flush=True)
