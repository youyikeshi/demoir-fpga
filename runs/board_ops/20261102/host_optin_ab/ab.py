import os, sys, hashlib, json
d = sys.argv[1]
sys.path.insert(0, d)
import numpy as np
import bilinear_c as bc
import bin_ref_numpy as rn

mode = os.environ.get("BIN_BILINEAR_C", "<unset>")
lib = bc.load()
rec = dict(dir=d, env=mode, loaded=lib is not None, why=bc.why())
if lib is not None:
    rec["selftest_full_29"] = bool(bc.selftest("full"))
    rng = np.random.default_rng(20261102)
    shapes = [(48,32,32,64,64), (16,8,8,16,16), (32,16,16,8,8),
              (3,5,7,11,13), (1,5,7,3,4), (5,17,3,9,11)]
    outs = []
    for (C,H,W,HO,WO) in shapes:
        x = rng.random((C,H,W), dtype=np.float32)
        y = bc.bilinear_f32_c(x, size=(HO,WO))
        nb = rn._bilinear_f32_numpy(x, size=(HO,WO))
        outs.append(dict(shape=[C,H,W,HO,WO],
                         c_md5=hashlib.md5(y.tobytes()).hexdigest()[:12],
                         numpy_md5=hashlib.md5(nb.tobytes()).hexdigest()[:12],
                         same=bool(y.tobytes() == nb.tobytes())))
    rec["shapes"] = outs
    rec["all_same"] = all(o["same"] for o in outs)
print(json.dumps(rec, ensure_ascii=False, indent=1))
