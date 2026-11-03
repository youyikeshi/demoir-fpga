"""cat 的相位诊断：在**真实 arena 池**（CMA 非缓存映射）上量各条路径的带宽。

目的：回答"cat 的 5.66 s 花在哪"——是 memcpy 带宽不够，还是
`_req`（tobytes → bytes.translate → 赋值）这条"标度重定标"路径本身太慢，
还是 Python 层每次调用的固定开销。

方法：分配一块与真实 cat 段同量级（4.9 MB = 48ch×320×320）的 PYNQ CMA 缓冲，
分别测：
  ① np.copyto / 切片赋值            —— 纯 memcpy 上限
  ② 当前的 _req 实现（translate）    —— 现在 cat 走的就是这条
  ③ i8_fast.c 的 NEON memcpy        —— 自写拷贝内核的上限
  ④ i8_fast.c 的 NEON 查表（原地）  —— 候选优化（cat 的 direct 段重定标）
  ⑤ i8_fast.c 的 NEON 查表（异处）  —— 候选优化（cat 的 copy 段）
另外测 cached(malloc) 缓冲做对照，区分"CMA 非缓存"与"算法本身"的代价。

用法（板上，本文件与 ../pynq/i8_fast.c 一起拷过去）：
    ./board_safe_run.sh 300 cat_phase_diag.py
"""
import ctypes
import os
import subprocess
import time

import numpy as np

MB = 1 << 20
N = 48 * 320 * 320          # 4,915,200 字节 —— 与 encoder_1.sam fuse_cat 的段同量级
REP = 5

HERE = os.path.dirname(os.path.abspath(__file__))


def timed(fn, rep=REP):
    best = 1e9
    for _ in range(rep):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def report(name, nbytes, sec):
    print("%-46s %8.2f ms   %8.1f MB/s" % (name, sec * 1e3, nbytes / MB / sec))


def build_lib():
    """编译仓库里**真实的** i8_fast.c（与板上跑的是同一份源码）。

    板子上板包是扁平目录（i8_fast.c 与本脚本同级），仓库里是 ../pynq/。
    """
    cands = [os.path.join(HERE, "i8_fast.c"),
             os.path.join(HERE, "..", "pynq", "i8_fast.c")]
    src = next((p for p in cands if os.path.exists(p)), None)
    if src is None:
        print("找不到 i8_fast.c，跳过 C 内核对比")
        return None
    src = os.path.abspath(src)
    d = "/tmp/catdiag"
    os.makedirs(d, exist_ok=True)
    so = os.path.join(d, "i8_fast.so")
    err = ""
    for flags in (["-O3", "-mfpu=neon", "-mfloat-abi=hard"],
                  ["-O3"], []):
        r = subprocess.run(["cc", "-std=c99", "-fPIC", "-shared", "-o", so, src]
                           + flags, capture_output=True, text=True)
        if r.returncode == 0:
            print("i8_fast.c 编译成功（%s）" % (" ".join(flags) or "默认"))
            break
        err = r.stderr
    else:
        print("i8_fast.c 编译失败，跳过 C 对比：%s" % err[-300:])
        return None
    lib = ctypes.CDLL(so)
    lib.i8_copy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
    lib.i8_lut.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long,
                           ctypes.c_void_p]
    lib.i8_lut_ip.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
    lib.i8_lut_scalar.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long,
                                  ctypes.c_void_p]
    return lib


def lut_bytes(s_from, s_to):
    """与 runner._lut 同一个表达式（float64 乘法 + rint + clip）。"""
    v = np.arange(-128, 128, dtype=np.float64) * float(s_from) / float(s_to)
    return np.clip(np.rint(v), -128, 127).astype(np.int8).tobytes()


def check_exact(lib, tbl):
    """**硬自检**：C 内核的输出必须与 bytes.translate 逐字节相同。"""
    rng = np.random.default_rng(7)
    src = rng.integers(-128, 128, 1 << 16, dtype=np.int8)
    want = np.frombuffer(src.tobytes().translate(tbl), dtype=np.int8)
    lut_np = np.frombuffer(tbl, dtype=np.int8)
    dst = np.empty_like(src)
    lib.i8_lut(dst.ctypes.data, src.ctypes.data, src.size, lut_np.ctypes.data)
    bad1 = int((dst != want).sum())
    ip = src.copy()
    lib.i8_lut_ip(ip.ctypes.data, ip.size, lut_np.ctypes.data)
    bad2 = int((ip != want).sum())
    print("\n==== 正确性自检（vs bytes.translate）：异处 %d / 原地 %d 不一致 ===="
          % (bad1, bad2))
    assert bad1 == 0 and bad2 == 0, "i8_fast 与 bytes.translate 不一致"


def main():
    lib = build_lib()
    s_from, s_to = 0.03655245616155294, 0.00708162797670168
    tbl = lut_bytes(s_from, s_to)
    lut_np = np.frombuffer(tbl, dtype=np.int8)
    if lib is not None:
        check_exact(lib, tbl)

    try:
        from pynq import allocate
        has_pynq = True
    except Exception as e:                                   # noqa: BLE001
        print("没有 pynq（主机跑？）：%s" % e)
        has_pynq = False

    print("\n==== A. CMA（pynq.allocate，非缓存）====")
    if has_pynq:
        a = allocate(shape=(N,), dtype=np.int8)
        b = allocate(shape=(N,), dtype=np.int8)
        av, bv = np.asarray(a), np.asarray(b)
        rng = np.random.default_rng(0)
        av[:] = rng.integers(-128, 128, N, dtype=np.int8)
        a.flush()
        b.flush()

        t = timed(lambda: np.copyto(bv, av))
        report("np.copyto(dst, src)  [memcpy 上限]", N, t)

        def req_inplace():
            bv[:] = np.frombuffer(bv.tobytes().translate(tbl), dtype=np.int8)

        t = timed(req_inplace, 3)
        report("当前 _req：tobytes+translate+赋值", N, t)

        t = timed(lambda: av.tobytes(), 3)
        report("  其中 tobytes() 一次性", N, t)
        buf = bv.tobytes()
        t = timed(lambda: buf.translate(tbl), 3)
        report("  其中 bytes.translate 一次性", N, t)

        if lib is not None:
            t = timed(lambda: lib.i8_copy(bv.ctypes.data, av.ctypes.data, N))
            report("i8_fast NEON memcpy", N, t)
            t = timed(lambda: lib.i8_lut_ip(bv.ctypes.data, N,
                                            lut_np.ctypes.data))
            report("i8_fast NEON 查表（原地，CMA）", N, t)
            t = timed(lambda: lib.i8_lut(bv.ctypes.data, av.ctypes.data, N,
                                         lut_np.ctypes.data))
            report("i8_fast NEON 查表（异处，CMA）", N, t)
            t = timed(lambda: lib.i8_lut_scalar(bv.ctypes.data, av.ctypes.data, N,
                                                lut_np.ctypes.data))
            report("i8_fast 标量查表（异处，CMA）", N, t)

        del a, b
        print("（释放 CMA）")

    print("\n==== B. cached（普通 numpy 缓冲，对照）====")
    x = np.random.default_rng(1).integers(-128, 128, N, dtype=np.int8)
    y = np.empty(N, np.int8)
    t = timed(lambda: np.copyto(y, x))
    report("np.copyto", N, t)
    if lib is not None:
        t = timed(lambda: lib.i8_copy(y.ctypes.data, x.ctypes.data, N))
        report("i8_fast NEON memcpy", N, t)
        t = timed(lambda: lib.i8_lut_ip(y.ctypes.data, N, lut_np.ctypes.data))
        report("i8_fast NEON 查表（原地）", N, t)
        t = timed(lambda: lib.i8_lut(y.ctypes.data, x.ctypes.data, N,
                                     lut_np.ctypes.data))
        report("i8_fast NEON 查表（异处）", N, t)

    print("\n==== C. 小数组（cat 小段的 224 次调用）====")
    for n in (256 << 10, 1 << 20):
        z = np.zeros(n, np.int8)
        t = timed(lambda: np.copyto(z, x[:n]), 20)
        report("np.copyto %d KB" % (n >> 10), n, t)
        if lib is not None:
            t = timed(lambda: lib.i8_copy(z.ctypes.data, x.ctypes.data, n), 20)
            report("i8_fast memcpy %d KB" % (n >> 10), n, t)
            t = timed(lambda: lib.i8_lut_ip(z.ctypes.data, n, lut_np.ctypes.data), 20)
            report("i8_fast 查表 %d KB" % (n >> 10), n, t)


if __name__ == "__main__":
    main()
