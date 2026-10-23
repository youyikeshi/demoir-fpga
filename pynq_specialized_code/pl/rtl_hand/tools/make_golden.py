#!/usr/bin/env python3
# ============================================================================
#  make_golden.py —— 黄金向量生成器（契约 §4）
#
#  用 pl/src/conv_ref_rt.cpp（与 RTL 逐位一致的 C 参考）对指定形状生成
#  expected_c8.bin。python 只负责搬运/校验，**不做任何算术**，避免"两边一起错"。
#
#  目录结构（契约 §4）：
#    golden/<shape>/
#      meta.json         {"cin":..,"cout":..,"k":..,"s":..,"d":..,"pad":..,"H":..,"W":..,"relu":..}
#      input_c8.bin      int8 C8，物理通道 ceil(cin/8)*8
#      weight.bin        PAD 容器（k=1:1B, k=3:12B, k=5:28B 每 (oc,ic)）
#      bias.bin          int32 × cout（小端）
#      expected_c8.bin   int8 C8，物理通道 ceil(cout/8)*8
#      shift.txt         一个十进制整数
#
#  三套固定形状来自已有 cosim 数据集（/data/work/tb_c8_*）：
#    k1_64x64  : cin=64 cout=64 k=1
#    k3_32x16  : cin=32 cout=16 k=3
#    k5_12x48  : cin=12 cout=48 k=5
#    （三套均为 H=2 W=320 shift=7，数据集 meta.txt 里写死）
#
#  自检：
#    ① 生成后重读文件，校验字节数；
#    ② 用现有 HLS C-sim 引擎（tb_engine_c8.cpp，C8_LAYOUT=1）独立算一遍，
#       输出转 C8 后与 expected_c8.bin **逐字节**比对（0 差异才算通过）；
#    ③ 把 shape/大小/md5 写进 golden/INDEX.md，作为可复核的证据。
#    说明：现有 cosim 工程（/data/work/hls_c8_cosim/*）没有落盘 RTL 输出，
#    只留了 PASS 日志，所以这里用"同一份引擎的 C-sim 构建"做交叉验证；
#    该构建在 docs/设计/记录_20261023_{C8RTL改造与验证,纯编译期C8与全8对齐}.md
#    里已由 HLS cosim 逐位验证过。
#
#  用法：
#    ./make_golden.py                         # 生成三套固定形状（含自检）
#    ./make_golden.py --shape k5_12x48        # 只做一套
#    ./make_golden.py --no-crosscheck         # 跳过 HLS C-sim 交叉验证
#    ./make_golden.py --random 200            # 每个选中形状生成 200 组 fuzz
#    ./make_golden.py --random 200 --shape k3_32x16 --seed 20261023
#    ./make_golden.py --from-dir /data/work/tb_c8_k5_12x48 \
#        --name my_case --cin 12 --cout 48 --k 5 --s 1 --d 1 --pad 2
#
#  依赖：仅 python3 标准库 + 本机 g++。不需要 pip install。
# ============================================================================
import argparse
import hashlib
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RTL_ROOT = os.path.dirname(HERE)                 # .../pl/rtl_hand
PL_ROOT = os.path.dirname(RTL_ROOT)              # .../pl
SRC_DIR = os.path.join(PL_ROOT, "src")
GOLDEN_ROOT = os.path.join(RTL_ROOT, "golden")
BUILD_DIR = os.path.join(RTL_ROOT, "build")
DRIVER_SRC = os.path.join(HERE, "golden_driver.cpp")
DRIVER_EXE = os.path.join(BUILD_DIR, "golden_driver")
ENGINE_TB = os.path.join(PL_ROOT, "tb", "tb_engine_c8.cpp")
ENGINE_SRC = os.path.join(SRC_DIR, "conv2d_engine.cpp")
REF_SRC = os.path.join(SRC_DIR, "conv_ref_rt.cpp")

C8 = 8
WC = {1: 1, 3: 12, 5: 28}   # 每 (oc,ic) 的权重容器字节数，与 conv2d_engine.h 一致

# 固定形状。H/W/shift 以数据集里的 meta.txt 为准（这里只登记通道/核参数）。
PRESETS = {
    "k1_64x64": dict(cin=64, cout=64, k=1, s=1, d=1, pad=0,
                     src="/data/work/tb_c8_k1_64x64"),
    "k3_32x16": dict(cin=32, cout=16, k=3, s=1, d=1, pad=1,
                     src="/data/work/tb_c8_k3_32x16"),
    "k5_12x48": dict(cin=12, cout=48, k=5, s=1, d=1, pad=2,
                     src="/data/work/tb_c8_k5_12x48"),
}


def die(msg):
    print("FAIL: " + msg, file=sys.stderr)
    sys.exit(2)


def info(msg):
    print("[golden] " + msg)


def run(cmd, cwd=None, capture=True):
    """所有子进程都带超时（主控要求：单次黄金生成 ≤120 s，绝不无限等）。"""
    try:
        r = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        die("命令超时（>120s）: " + " ".join(cmd))
    if capture and r.returncode != 0:
        print(r.stdout)
    return r


def ensure_driver():
    """编译 conv_ref_rt 的 driver；源码比可执行新就重编。"""
    os.makedirs(BUILD_DIR, exist_ok=True)
    need = [DRIVER_SRC, REF_SRC, os.path.join(SRC_DIR, "conv2d_engine.h"),
            os.path.join(SRC_DIR, "conv2d_accel.h")]
    if os.path.exists(DRIVER_EXE):
        newest = max(os.path.getmtime(p) for p in need if os.path.exists(p))
        if os.path.getmtime(DRIVER_EXE) >= newest:
            return DRIVER_EXE
    for p in need:
        if not os.path.exists(p):
            die("缺少源码 " + p)
    info("编译 golden_driver（g++ -O2）...")
    r = run(["g++", "-std=c++14", "-O2", "-I", SRC_DIR, DRIVER_SRC, REF_SRC,
             "-o", DRIVER_EXE])
    if r.returncode != 0:
        die("g++ 编译 golden_driver 失败")
    return DRIVER_EXE


def ensure_engine_csim():
    """编译 HLS C-sim 引擎（tb_engine_c8.cpp, C8_LAYOUT=1），用于交叉验证。"""
    exe = os.path.join(BUILD_DIR, "tb_engine_c8_crosscheck.exe")
    need = [ENGINE_SRC, REF_SRC, ENGINE_TB,
            os.path.join(SRC_DIR, "conv2d_engine.h"),
            os.path.join(SRC_DIR, "conv2d_accel.h")]
    if os.path.exists(exe):
        newest = max(os.path.getmtime(p) for p in need if os.path.exists(p))
        if os.path.getmtime(exe) >= newest:
            return exe
    for p in need:
        if not os.path.exists(p):
            return None
    info("编译交叉验证引擎（tb_engine_c8.cpp C8_LAYOUT=1）...")
    r = run(["g++", "-std=c++14", "-O2", "-DC8_LAYOUT=1", "-I", SRC_DIR,
             ENGINE_SRC, REF_SRC, ENGINE_TB, "-o", exe])
    return exe if r.returncode == 0 else None


def read_meta_json(src_dir):
    """从已生成的 golden/<shape>/meta.json 读 H/W/shift（自包含，不依赖 /data/work）。"""
    path = os.path.join(src_dir, "meta.json")
    if not os.path.exists(path):
        return None
    try:
        d = json.load(open(path))
    except Exception:
        return None
    if not all(k in d for k in ("H", "W")):
        return None
    shift = d.get("shift")
    if shift is None:
        sp = os.path.join(src_dir, "shift.txt")
        if os.path.exists(sp):
            shift = int(open(sp).read().split()[0])
    if shift is None:
        return None
    return dict(H=int(d["H"]), W=int(d["W"]), shift=int(shift))


def read_meta_txt(src_dir):
    path = os.path.join(src_dir, "meta.txt")
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        parts = f.read().split()
    if len(parts) < 3:
        die("meta.txt 格式不对: " + path)
    return dict(H=int(parts[0]), W=int(parts[1]), shift=int(parts[2]))


def sizes(shape):
    cin, cout, k = shape["cin"], shape["cout"], shape["k"]
    cin_pad = (cin + C8 - 1) // C8 * C8
    cout_pad = (cout + C8 - 1) // C8 * C8
    hout = (shape["H"] + 2 * shape["pad"] - ((k - 1) * shape["d"] + 1)) // shape["s"] + 1
    wout = (shape["W"] + 2 * shape["pad"] - ((k - 1) * shape["d"] + 1)) // shape["s"] + 1
    if hout <= 0 or wout <= 0:
        die("输出尺寸非正: %dx%d" % (hout, wout))
    wcb = WC.get(k, k * k)
    return dict(cin_pad=cin_pad, cout_pad=cout_pad, hout=hout, wout=wout,
                wcb=wcb,
                n_in=cin_pad * shape["H"] * shape["W"],
                n_w=cout * cin * wcb,
                n_b=cout * 4,
                n_out=cout_pad * hout * wout)


def write_meta(dst, shape):
    meta = {"cin": shape["cin"], "cout": shape["cout"], "k": shape["k"],
            "s": shape["s"], "d": shape["d"], "pad": shape["pad"],
            "H": shape["H"], "W": shape["W"], "relu": shape["relu"]}
    # 固定键顺序、无空格：Verilog 端的 load_bins 用 $sscanf 解析这一格式。
    with open(os.path.join(dst, "meta.json"), "w") as f:
        f.write(json.dumps(meta, separators=(",", ":"), sort_keys=False) + "\n")
    with open(os.path.join(dst, "shift.txt"), "w") as f:
        f.write("%d\n" % shape["shift"])


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_driver(shape, dst):
    s = sizes(shape)
    cmd = [ensure_driver(), dst, str(shape["cin"]), str(shape["cout"]),
           str(shape["k"]), str(shape["s"]), str(shape["d"]), str(shape["pad"]),
           str(shape["H"]), str(shape["W"]), str(shape["shift"]),
           str(shape["relu"])]
    r = run(cmd)
    if r.returncode != 0:
        die("golden_driver 失败（%s）" % dst)
    exp = os.path.join(dst, "expected_c8.bin")
    if not os.path.exists(exp):
        die("driver 没写出 " + exp)
    got = os.path.getsize(exp)
    if got != s["n_out"]:
        die("%s 大小 %d，期望 %d" % (exp, got, s["n_out"]))
    for name, want in (("input_c8.bin", s["n_in"]), ("weight.bin", s["n_w"]),
                       ("bias.bin", s["n_b"])):
        p = os.path.join(dst, name)
        if os.path.getsize(p) != want:
            die("%s 大小 %d，期望 %d" % (p, os.path.getsize(p), want))
    return s


def chw_to_c8(raw, cout, hout, wout, cout_pad):
    out = bytearray(cout_pad * hout * wout)
    for c in range(cout):
        for y in range(hout):
            for x in range(wout):
                out[(c // C8) * hout * wout * C8 + y * wout * C8 + x * C8 + (c % C8)] = \
                    raw[(c * hout + y) * wout + x]
    return bytes(out)


def crosscheck_engine(shape, dst, exe):
    """用 HLS C-sim 引擎独立算一遍，expect 逐字节比对。返回 (bad, total)。

    tb_engine_c8.cpp 的输入约定是 <dir>/meta.txt（H/W/shift 三行），而契约 §4
    的 golden 目录用 meta.json + shift.txt。这里搭一个临时目录做适配，
    不改 tb、也不往 golden 目录里塞合同之外的文件。
    """
    s = sizes(shape)
    tmp = tempfile.mkdtemp(prefix="rtl_hand_xcheck_")
    try:
        for name in ("input_c8.bin", "weight.bin", "bias.bin"):
            os.symlink(os.path.join(dst, name), os.path.join(tmp, name))
        with open(os.path.join(tmp, "meta.txt"), "w") as f:
            f.write("%d\n%d\n%d\n" % (shape["H"], shape["W"], shape["shift"]))
        dump = os.path.join(BUILD_DIR, "_crosscheck_chw.bin")
        env = os.environ.copy()
        env["TB_DUMP_OUT"] = dump
        try:
            r = subprocess.run([exe, tmp, str(shape["cin"]), str(shape["cout"]),
                                str(shape["k"]), str(shape["s"]), str(shape["d"]),
                                str(shape["pad"]), "1"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, env=env, timeout=120)
        except subprocess.TimeoutExpired:
            die("交叉验证引擎超时（>120s）: " + dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if r.returncode != 0:
        print(r.stdout)
        die("交叉验证引擎运行失败（%s）" % dst)
    with open(dump, "rb") as f:
        chw = f.read()
    if len(chw) != shape["cout"] * s["hout"] * s["wout"]:
        die("引擎 dump 大小 %d 不符" % len(chw))
    eng_c8 = chw_to_c8(chw, shape["cout"], s["hout"], s["wout"], s["cout_pad"])
    with open(os.path.join(dst, "expected_c8.bin"), "rb") as f:
        ref = f.read()
    bad = sum(1 for a, b in zip(ref, eng_c8) if a != b)
    bad += abs(len(ref) - len(eng_c8))
    return bad, len(ref)


def copy_bins(src, dst):
    for name in ("input_c8.bin", "weight.bin", "bias.bin"):
        p = os.path.join(src, name)
        if not os.path.exists(p):
            die("缺少 " + p)
        shutil.copyfile(p, os.path.join(dst, name))


def build_fixed(name, args, index_rows):
    p = PRESETS[name]
    src = p["src"]
    if not os.path.isdir(src):
        die("数据集目录不存在: " + src)
    mt = read_meta_txt(src)
    if mt is None:
        die(src + " 缺少 meta.txt")
    shape = dict(cin=p["cin"], cout=p["cout"], k=p["k"], s=p["s"], d=p["d"],
                 pad=p["pad"], H=mt["H"], W=mt["W"], shift=mt["shift"],
                 relu=1)
    dst = os.path.join(GOLDEN_ROOT, name)
    os.makedirs(dst, exist_ok=True)
    info("=== %s  cin=%d cout=%d k=%d  H=%d W=%d shift=%d ===" %
         (name, shape["cin"], shape["cout"], shape["k"], shape["H"],
          shape["W"], shape["shift"]))
    copy_bins(src, dst)
    write_meta(dst, shape)
    s = run_driver(shape, dst)
    info("expected_c8.bin  %d B  md5=%s" %
         (s["n_out"], md5_of(os.path.join(dst, "expected_c8.bin"))))

    ck = None
    if not args.no_crosscheck:
        exe = ensure_engine_csim()
        if exe is None:
            print("!! 跳过交叉验证：无法编译 tb_engine_c8.cpp")
        else:
            bad, total = crosscheck_engine(shape, dst, exe)
            info("交叉验证（HLS C-sim 引擎 C8 输出 vs expected）: %d/%d 不一致" %
                 (bad, total))
            if bad != 0:
                die("黄金输出与 HLS C-sim 引擎不一致（%d 处），不要改黄金迁就！" % bad)
            ck = "0/%d" % total
    index_rows.append((name, shape, s, md5_of(os.path.join(dst, "expected_c8.bin")),
                       ck))


def gen_random_case(dst, shape, rng, mode):
    """mode: 'zero' / 'pos' / 'neg' / 'rand'"""
    s = sizes(shape)
    os.makedirs(dst, exist_ok=True)
    n_in = s["n_in"]
    n_w = s["n_w"]

    def in_val():
        if mode == "zero":
            return 0
        if mode == "pos":
            return 127
        if mode == "neg":
            return -128       # 负边界：输入全 -128，权重取 +127 ⇒ 输出走负饱和/ReLU=0
        return rng.randint(-128, 127)

    def w_val():
        if mode == "zero":
            return 0
        if mode == "pos":
            return 127
        if mode == "neg":
            return 127
        return rng.randint(-128, 127)

    inp = bytes(bytearray((in_val() & 0xFF) for _ in range(n_in)))
    if mode == "rand":
        wgt = bytearray()
        for _ in range(shape["cout"] * shape["cin"]):
            for t in range(WC.get(shape["k"], shape["k"] ** 2)):
                if t < shape["k"] ** 2:
                    wgt.append(rng.randint(-128, 127) & 0xFF)
                else:
                    wgt.append(0)     # PAD 容器的尾部补零
        wgt = bytes(wgt)
    else:
        taps = shape["k"] ** 2
        wcb = WC.get(shape["k"], taps)
        wgt = bytes(bytearray(((w_val() & 0xFF) if t < taps else 0)
                              for _ in range(shape["cout"] * shape["cin"])
                              for t in range(wcb)))
    bias = b"".join(struct.pack("<i", 0 if mode != "rand"
                                else rng.randint(-100000, 100000))
                    for _ in range(shape["cout"]))
    with open(os.path.join(dst, "input_c8.bin"), "wb") as f:
        f.write(inp)
    with open(os.path.join(dst, "weight.bin"), "wb") as f:
        f.write(wgt)
    with open(os.path.join(dst, "bias.bin"), "wb") as f:
        f.write(bias)
    sh = shape.copy()
    if mode == "rand":
        sh["shift"] = rng.randint(1, 12)
    else:
        sh["shift"] = 7
    write_meta(dst, sh)
    run_driver(sh, dst)
    return dst, sh


def build_random(args, shape_names):
    for name in shape_names:
        p = PRESETS[name]
        src = p["src"]
        mt = read_meta_json(os.path.join(GOLDEN_ROOT, name))
        if mt is None and os.path.isdir(src):
            mt = read_meta_txt(src)
        if mt is None:
            die("fuzz 需要 golden/%s/meta.json（先跑一次默认生成）或 %s/meta.txt" % (name, src))
        base = dict(cin=p["cin"], cout=p["cout"], k=p["k"], s=p["s"], d=p["d"],
                    pad=p["pad"], H=mt["H"], W=mt["W"], shift=7, relu=1)
        outdir = os.path.join(GOLDEN_ROOT, "fuzz", name)
        if os.path.isdir(outdir):
            shutil.rmtree(outdir)
        os.makedirs(outdir)
        dirs = []
        modes = ["zero", "pos", "neg"]
        rng = random.Random(args.seed)
        info("=== fuzz %s：%d 组（种子 %d）===" % (name, args.random, args.seed))
        for i in range(args.random):
            mode = modes[i] if i < len(modes) else "rand"
            dst = os.path.join(outdir, "case_%03d" % i)
            d, sh = gen_random_case(dst, base, rng, mode)
            dirs.append(d)
        manifest = os.path.join(outdir, "manifest.txt")
        with open(manifest, "w") as f:
            for d in dirs:
                f.write(d + "\n")
        info("manifest: %s（%d 组）" % (manifest, len(dirs)))


def write_index(index_rows):
    path = os.path.join(GOLDEN_ROOT, "INDEX.md")
    lines = ["# 黄金向量索引（由 tools/make_golden.py 生成）", "",
             "| shape | cin | cout | k | H×W | shift | expected 字节 | md5(expected_c8.bin) | 交叉验证 |",
             "|---|---:|---:|---:|---|---:|---:|---|---|"]
    for name, sh, s, md5, ck in sorted(index_rows):
        lines.append("| `%s` | %d | %d | %d | %d×%d | %d | %d | `%s` | %s |" %
                     (name, sh["cin"], sh["cout"], sh["k"], sh["H"], sh["W"],
                      sh["shift"], s["n_out"], md5, ck or "未做"))
    lines += ["", "交叉验证 = 黄金输出与 `tb_engine_c8.cpp`（HLS C-sim 引擎，",
              "`C8_LAYOUT=1`）独立算出的 C8 结果逐字节比对的不一致数。",
              "0 才算通过；该引擎已在 HLS cosim 中逐位验证。", ""]
    with open(path, "w") as f:
        f.write("\n".join(lines))
    info("索引写入 " + path)


def main():
    ap = argparse.ArgumentParser(
        description="生成契约 §4 的黄金向量（conv_ref_rt.cpp 为唯一算术来源）")
    ap.add_argument("--shape", action="append", choices=sorted(PRESETS),
                    help="只处理指定固定形状，可重复；默认三套都做")
    ap.add_argument("--random", type=int, metavar="N",
                    help="为选中形状生成 N 组随机 fuzz（前 3 组为全 0/+127/-128）")
    ap.add_argument("--seed", type=int, default=20261023, help="fuzz 随机种子")
    ap.add_argument("--no-crosscheck", action="store_true",
                    help="跳过 HLS C-sim 引擎交叉验证")
    ap.add_argument("--zeros", action="store_true",
                    help="生成一套全 0 的固定用例（自测用，配合 --name/--shape）")
    ap.add_argument("--from-dir", help="从已有数据集目录取 input/weight/bias/meta.txt")
    ap.add_argument("--name", help="--from-dir 时的输出 shape 名")
    ap.add_argument("--cin", type=int)
    ap.add_argument("--cout", type=int)
    ap.add_argument("--k", type=int)
    ap.add_argument("--s", type=int, default=1)
    ap.add_argument("--d", type=int, default=1)
    ap.add_argument("--pad", type=int)
    ap.add_argument("--H", type=int)
    ap.add_argument("--W", type=int)
    ap.add_argument("--shift", type=int)
    ap.add_argument("--relu", type=int, default=1)
    args = ap.parse_args()

    os.makedirs(GOLDEN_ROOT, exist_ok=True)

    # ---- 单案例模式 ----
    if args.from_dir:
        if not (args.name and args.cin and args.cout and args.k and args.pad):
            die("--from-dir 需要 --name/--cin/--cout/--k/--pad")
        mt = read_meta_txt(args.from_dir)
        if mt is None:
            die(args.from_dir + " 缺少 meta.txt")
        shape = dict(cin=args.cin, cout=args.cout, k=args.k, s=args.s,
                     d=args.d, pad=args.pad, H=args.H or mt["H"],
                     W=args.W or mt["W"],
                     shift=args.shift if args.shift is not None else mt["shift"],
                     relu=args.relu)
        dst = os.path.join(GOLDEN_ROOT, args.name)
        os.makedirs(dst, exist_ok=True)
        copy_bins(args.from_dir, dst)
        write_meta(dst, shape)
        s = run_driver(shape, dst)
        info("expected_c8.bin  %d B  md5=%s" %
             (s["n_out"], md5_of(os.path.join(dst, "expected_c8.bin"))))
        return

    shapes = args.shape or sorted(PRESETS)

    # ---- 全 0 自测用例 ----
    if args.zeros:
        if len(shapes) != 1 or not args.name:
            die("--zeros 需要且仅需要一个 --shape 和 --name")
        name = shapes[0]
        p = PRESETS[name]
        mt = read_meta_json(os.path.join(GOLDEN_ROOT, name)) or read_meta_txt(p["src"])
        if mt is None:
            die("需要 golden/%s/meta.json 或 %s/meta.txt" % (name, p["src"]))
        base = dict(cin=p["cin"], cout=p["cout"], k=p["k"], s=p["s"], d=p["d"],
                    pad=p["pad"], H=mt["H"], W=mt["W"], shift=7, relu=1)
        dst = os.path.join(GOLDEN_ROOT, args.name)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        rng = random.Random(0)
        d, sh = gen_random_case(dst, base, rng, "zero")
        info("全 0 用例写入 %s（expected_c8 全 0，%d 字节）" %
             (d, sizes(sh)["n_out"]))
        return

    # ---- fuzz 模式 ----
    if args.random is not None:
        if args.random < 1:
            die("--random 必须 ≥1")
        build_random(args, shapes)
        return

    # ---- 固定形状 ----
    index_rows = []
    for name in shapes:
        build_fixed(name, args, index_rows)
    if index_rows:
        write_index(index_rows)


if __name__ == "__main__":
    main()
