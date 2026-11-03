"""
board_test_staged.py —— 分阶段板端验证（每阶段都能安全中止）

阶段：
  0  overlay 加载
  1  寄存器只读（不访存）
  2  写配置再读回（不访存）
  3  单一整块分配 + 子区划分 + 地址校验（不访存）
  4  **最小 DMA**：8x8 cin8 cout8，输入仅 512 B（武装看门狗）
  5  k1 64x12 golden（武装看门狗）
  6  其余 shapes

任一阶段失败 ⇒ 立即退出，不再往下做。
"""
import sys, json, time
import numpy as np

STAGE = int(sys.argv[1]) if len(sys.argv) > 1 else 6
BIT = '/home/xilinx/demoir_v2/design_1.bit'
GD = '/home/xilinx/demoir_v2/golden'

from pynq import Overlay, allocate
from uni_engine import ConvUniEngine, _Guard
from board_safe import Watchdog


def hdr(s):
    print(f"\n=== {s} ===", flush=True)


def load_golden(tag):
    d = f"{GD}/{tag}"
    m = json.load(open(f"{d}/meta.json"))
    f = lambda n: np.fromfile(f"{d}/{n}", dtype=np.uint8)
    return m, f('input_c8.bin'), f('weight.bin'), f('bias.bin'), f('expected_c8.bin')


# ---------------------------------------------------------------- 阶段 0
hdr("阶段 0：加载 overlay")
ov = Overlay(BIT)
ips = sorted(ov.ip_dict.keys())
print("  IPLIST =", ips, flush=True)
assert 'conv_uni_0' in ips, "overlay 里没有 conv_uni_0"
if STAGE < 1:
    sys.exit(0)

e = ConvUniEngine(ov.conv_uni_0)

# ---------------------------------------------------------------- 阶段 1
hdr("阶段 1：寄存器只读（不启动引擎、不访存）")
print(f"  AP_CTRL  = {e._rd(0x10000):#010x}", flush=True)
print(f"  AP_RETURN= {e._rd(0x10010):#010x}", flush=True)
print(f"  DBG_FLAG = {e._rd(0x100F0):#010x}", flush=True)
if STAGE < 2:
    sys.exit(0)

# ---------------------------------------------------------------- 阶段 2
hdr("阶段 2：写配置再读回（仍不启动）")
e._wr(0x10000 + 0x18, 8)     # H
e._wr(0x10000 + 0x20, 8)     # W
e._wr(0x10000 + 0x48, 1)     # K
got = (e._rd(0x10000 + 0x18), e._rd(0x10000 + 0x20), e._rd(0x10000 + 0x48))
print(f"  读回 H,W,K = {got}", flush=True)
assert got == (8, 8, 1), f"寄存器读写不一致：{got}"
print("  ✓ AXI-Lite 读写正常", flush=True)
if STAGE < 3:
    sys.exit(0)

# ---------------------------------------------------------------- 阶段 3
hdr("阶段 3：单一整块分配 + 地址校验（不启动）")
TOTAL = 1 << 18                       # 256 KB
pool = allocate(shape=(TOTAL,), dtype=np.uint8)
pool[:] = 0xA5                        # 全填金丝雀，任何越界都能看出来
pool.flush()
base_pa = int(pool.physical_address)
print(f"  pool 物理地址 = {base_pa:#x}  虚拟 {pool.virtual_address:#x}  长 {TOTAL}", flush=True)
_Guard.check_addr(base_pa, "POOL")
mv = np.frombuffer(pool, dtype=np.uint8)

def region(off, n, name):
    """从 pool 里切一段，返回 (numpy 视图, 物理地址)。按 64 B 对齐。"""
    off = (off + 63) & ~63
    assert off + n <= TOTAL, f"{name} 超出 pool"
    return mv[off:off + n], base_pa + off

m8, i8, w8, b8, o8 = load_golden('uni_k1_8x8_8x8')
IN, IN_PA = region(0, len(i8), 'IN')
WG, WG_PA = region(0x1000, len(w8), 'WGT')
BI, BI_PA = region(0x2000, len(b8), 'BIAS')
OU, OU_PA = region(0x3000, len(o8) + 4096, 'OUT')     # 留 4 KB 金丝雀
IN[:] = i8; WG[:] = w8; BI[:] = b8; OU[:] = 0xA5
pool.flush()
for nm, pa in (('IN', IN_PA), ('WGT', WG_PA), ('BIAS', BI_PA), ('OUT', OU_PA)):
    _Guard.check_addr(pa, nm)
    print(f"  {nm:5s} pa={pa:#010x}  len={len(IN) if nm=='IN' else '':>0}", flush=True)
print("  ✓ 四个子区地址全部通过校验", flush=True)
if STAGE < 4:
    sys.exit(0)

# ---------------------------------------------------------------- 阶段 4
hdr("阶段 4：最小 DMA 跑一次（8x8, cin=8, cout=8）—— 已武装看门狗")
with Watchdog(15) as wdt:
    st, dt, hw = e.run(IN, WG, BI, OU,
                       H=8, W=8, cin=8, cout=8, k=1, s=1, d=1, pad=0,
                       shift=7, relu=1, tap=None, timeout=20.0)
    # tap=None ⇒ 由 k 推导：默认新容器 TAP = 8·ceil(k²/8)（k=1 → 8）。
    # 配旧比特流时用 tap=4（或 padded=False）。送错值 RTL 回 ERR_TAP(2)，不会静默算错。
    wdt.beat()
    print(f"  done t={dt*1e3:.3f} ms  HoutxWout={hw}", flush=True)
    pool.invalidate()
    got = OU[:len(o8)]
    n_ok = int((got == o8).sum())
    print(f"  逐字节一致 {n_ok}/{len(o8)}", flush=True)
    print(f"  dbg = {e.dbg()}", flush=True)
    if n_ok != len(o8):
        diff = np.nonzero(got != o8)[0][:8]
        print(f"  ✗ 首个不符下标 {diff.tolist()}  "
              f"期望 {o8[diff].tolist()} 实际 {got[diff].tolist()}", flush=True)
        sys.exit(3)
print("  ✓ 最小 DMA 通过", flush=True)

if STAGE < 5:
    sys.exit(0)

# ---------------------------------------------------------------- 阶段 5/6
TAGS = ['uni_k1_64x12_8x8', 'uni_k3_64x12_8x8', 'uni_k5_12x48_8x8',
        'uni_k3_d3_32x16', 'uni_k1_s2_192x64', 'uni_k3_nopad_16x8']
if STAGE == 5:
    TAGS = TAGS[:1]
hdr(f"阶段 {'5' if STAGE==5 else '6'}：golden 验证 {len(TAGS)} 个")
for tag in TAGS:
    m, i_, w_, b_, o_ = load_golden(tag)
    IN, IN_PA = region(0, len(i_), 'IN')
    WG, WG_PA = region(0x8000, len(w_), 'WGT')
    BI, BI_PA = region(0x10000, len(b_), 'BIAS')
    OU, OU_PA = region(0x14000, len(o_) + 4096, 'OUT')
    IN[:] = i_; WG[:] = w_; BI[:] = b_; OU[:] = 0xA5
    pool.flush()
    with Watchdog(15) as wdt:
        st, dt, hw = e.run(IN, WG, BI, OU, H=m['H'], W=m['W'], cin=m['cin'],
                           cout=m['cout'], k=m['k'], s=m['s'], d=m['d'],
                           pad=m['pad'], shift=m['shift'], relu=m['relu'],
                           # TAP 由 meta.json 自描述（旧 4/12/28 或新 8/16/32）：
                           # 与比特流不一致时 RTL 直接回 ERR_TAP(2)，不会静默算错。
                           tap=m['tap_stride'], timeout=60.0)
        wdt.beat()
    pool.invalidate()
    got = OU[:len(o_)]
    n_ok = int((got == o_).sum())
    flag = '✓' if n_ok == len(o_) else '✗'
    print(f"  {flag} {tag:22s} {n_ok}/{len(o_)}  t={dt*1e3:.2f} ms  {e.dbg()}", flush=True)
    if n_ok != len(o_):
        sys.exit(4)
print("\n全部通过 ✓", flush=True)
