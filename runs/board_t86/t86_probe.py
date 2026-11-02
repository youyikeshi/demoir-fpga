#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runs/board_t86/t86_probe.py —— T-86：v45 上板三段验证（load / 0x40080000 / HP2 冒烟）

判据（顺序固定，任一档不过就停）：
  ① load v45（.bit 与自己的 .hwh 同目录成对）→ 读 alpha_shift / slope_shift /
     Clocks.fclk0_mhz / 三个 IP 基址（conv_uni_0 / bin_unit_0 / fconv_0）
  ② fclk0 = 25 MHz 读回 → 只读 fconv_0 寄存器（先 R_NLANE，期望 4）
       读通   ⇒ v44 的 SIGBUS 已消除（本轮最重要的一条）
       SIGBUS ⇒ 停，不再做冒烟
     对照：0x400A0000（未映射）必须仍然 SIGBUS，否则探针方式本身有问题
  ③ HP2 4 KB 恒等卷积：md5(y) == md5(x) + 4 个 DBG 计数非 0 + DBG_FLAG[0..2] == 0

纪律
  * 本进程**只做一次** Overlay(download=True)
  * 一切可能 SIGBUS 的访问（0x40080000 / 0x400A0000 / 冒烟）都在 fork 出的子进程里做：
    子进程被 SIGBUS 打死不会带走父进程 ⇒ 父进程仍能把判据完整落盘（v44 就是进程直接没了）
  * 每段结果立刻写文件 + fsync

用法（板端，sudo，XILINX_XRT=/usr）：
    python3 t86_probe.py --bit /tmp/probe_T86/design_1_150v45.bit --out /home/xilinx/demoir/t86_out
"""
import argparse
import hashlib
import json
import os
import signal
import sys
import time

# ---------------------------------------------------------------- 常量
FCLK_MHZ = 25.0            # 按 R8：v45 WNS −15.745 ns @10 ns ⇒ 真实最差周期 25.745 ns ⇒ 25 MHz
BASE_CONV = 0x40040000
BASE_BIN = 0x40060000
BASE_FCONV = 0x40080000
BASE_UNMAPPED = 0x400A0000

# fconv 寄存器（与 ps/pynq/fconv.py 一致）
CFG = 0x10000
OFF_NLANE = 0x78
OFF_APCTRL = 0x00
OFF_APRET = 0x10
OFF_STATUS = 0x94
OFF_MODE = 0x08

STATUS = {}


def log(msg=""):
    print(msg, flush=True)
    try:
        sys.stdout.flush()
    except Exception:                                     # noqa: BLE001
        pass


def jwrite(path, obj):
    """原子写 JSON + fsync（板子随时可能挂，证据不能丢）。"""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def md5_file(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_child(fn, res_path, timeout, tag):
    """fork 子进程跑 fn()；返回子进程结局（不抛异常）。

    子进程被 SIGBUS/SIGSEGV 打死时，父进程只看 waitpid 的 status ⇒ 判据可区分。
    """
    try:
        os.remove(res_path)
    except OSError:
        pass
    pid = os.fork()
    if pid == 0:                                          # ── 子进程
        try:
            r = fn()
            jwrite(res_path, {"ok": True, "result": r})
            os._exit(0)
        except BaseException as e:                        # noqa: BLE001
            try:
                jwrite(res_path, {"ok": False, "error": repr(e)})
            except Exception:                             # noqa: BLE001
                pass
            os._exit(3)
    t0 = time.time()
    st = None
    while time.time() - t0 < timeout:
        wpid, status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            st = status
            break
        time.sleep(0.05)
    out = {"tag": tag, "elapsed_s": round(time.time() - t0, 3)}
    if st is None:
        out.update(timeout=True, signaled=False, exitcode=None)
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        time.sleep(0.5)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        log("  [%s] ✗ 子进程超时 %.1fs，已终止" % (tag, out["elapsed_s"]))
    else:
        out["timeout"] = False
        if os.WIFSIGNALED(st):
            sig = os.WTERMSIG(st)
            try:
                nm = signal.Signals(sig).name
            except ValueError:
                nm = str(sig)
            out.update(signaled=True, signal=sig, signal_name=nm, exitcode=None)
            log("  [%s] ✗ 子进程被信号打死：%s (%d)" % (tag, nm, sig))
        else:
            out.update(signaled=False, exitcode=os.WEXITSTATUS(st))
            log("  [%s] 子进程退出码 %d" % (tag, out["exitcode"]))
    try:
        with open(res_path) as f:
            out["child"] = json.load(f)
    except Exception:                                     # noqa: BLE001
        out["child"] = None
    return out


# ---------------------------------------------------------------- ③ 冒烟体
def hp2_smoke_body(x, w, y):
    """在子进程里跑 HP2 4 KB 恒等卷积，返回判据字典（全部在子进程内完成比较）。"""
    import numpy as np
    from fconv import FconvEngine

    class _Shim:
        """把 pynq.MMIO 包成 fconv.FconvEngine 要的 .read/.write 接口。"""

        def __init__(self, mm):
            self.mm = mm

        def read(self, off):
            return self.mm.read(int(off)) & 0xFFFFFFFF

        def write(self, off, v):
            self.mm.write(int(off), int(v) & 0xFFFFFFFF)

    from pynq import MMIO
    mm = MMIO(BASE_FCONV, 0x20000)
    eng = FconvEngine(_Shim(mm))

    H = W = 32
    # 1) 输入 x：4096 B 已知图案（避开 0/NaN/Inf）
    xv = np.frombuffer(x, dtype=np.float32)
    i = np.arange(xv.size, dtype=np.int64)
    xv[:] = ((i % 251) + 1).astype(np.float32) / np.float32(7.0)
    # 2) 恒等权重：k=3、cin=cout=1 ⇒ 9 tap，中心 = 1.0，其余 0
    wv = np.frombuffer(w, dtype=np.float32)
    wv[:] = 0.0
    wv[4] = 1.0
    # 3) 输出缓冲：前 4096 B 不清（PL 写），尾部 4 KB 金丝雀
    yv = np.frombuffer(y, dtype=np.uint8)
    yv[4096:8192] = 0xA5

    x.flush()
    w.flush()
    y.flush()

    t0 = time.time()
    py = eng.launch(x, w, None, y=y, cin=1, cout=1, k=3, stride=1, pad=1,
                    H=H, W=W, relu=0, bias_en=0, timeout=30.0)
    dt = time.time() - t0
    y.invalidate()

    dbg = eng.dbg()
    st = eng.status()
    apret = eng._rd(CFG + OFF_APRET)
    yb = np.frombuffer(y, dtype=np.uint8)[:4096]
    xb = np.frombuffer(x, dtype=np.uint8)[:4096]
    mdx = hashlib.md5(bytes(xb)).hexdigest()
    mdy = hashlib.md5(bytes(yb)).hexdigest()
    nbad = int((yb != xb).sum())
    # 参考：核心输出（f32）与 x 的 float32 视图逐位比
    yf = np.frombuffer(y, dtype=np.float32, count=H * W)
    xf = np.frombuffer(x, dtype=np.float32, count=H * W)
    nbad32 = int((yf.view(np.uint32) != xf.view(np.uint32)).sum())
    return dict(
        H=H, W=W, cin=1, cout=1, k=3, pad=1, relu=0, bias_en=0, mode=0,
        seconds=round(dt, 4),
        md5_x=mdx, md5_y=mdy, equal_md5=bool(mdx == mdy),
        n_byte_diff=nbad, n_f32_bit_diff=nbad32,
        dbg=dbg,
        dbg_counts_nonzero=dict(
            hit=int(dbg["hit"]) > 0, rdreq=int(dbg["rdreq"]) > 0,
            nAR=int(dbg["nAR"]) > 0, fills=int(dbg["fills"]) > 0,
            nW=int(dbg["nW"]) > 0, nAW=int(dbg["nAW"]) > 0,
            nR=int(dbg["nR"]) > 0, stall=int(dbg["stall"]) > 0),
        dbg_flag_low3=int(dbg["flag"]) & 0x7,
        status=st, ap_return=apret,
        status_done=int(st) & 1, status_busy=(int(st) >> 1) & 1,
        status_err=(int(st) >> 2) & 1,
        canary_ok=bool(int((np.frombuffer(y, dtype=np.uint8)[4096:8192] != 0xA5).sum()) == 0),
        y_head=[int(v) for v in np.frombuffer(y, dtype=np.uint8)[:32]],
        x_head=[int(v) for v in np.frombuffer(x, dtype=np.uint8)[:32]],
    )


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fclk", type=float, default=FCLK_MHZ)
    ap.add_argument("--only-load", action="store_true", help="只做第 1 段（load + 指纹）")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    res_path = os.path.join(args.out, "t86_result.json")
    RES = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "bit": args.bit,
           "fclk_target_mhz": args.fclk, "steps": {}, "fatal": None}
    jwrite(res_path, RES)

    log("=== T-86 §1 load v45 ===")
    bit = args.bit
    hwh = os.path.splitext(bit)[0] + ".hwh"
    md5_bit = md5_file(bit)
    md5_hwh = md5_file(hwh) if os.path.exists(hwh) else None
    log("  bit  = %s  md5=%s" % (bit, md5_bit))
    log("  hwh  = %s  md5=%s" % (hwh, md5_hwh))
    RES["md5_bit"] = md5_bit
    RES["md5_hwh"] = md5_hwh
    if md5_bit != "4e9853fbc69642b27943e34ba9481e1d":
        RES["fatal"] = "bit md5 不符（期望 4e9853fbc69642b27943e34ba9481e1d）"
        jwrite(res_path, RES)
        log("  ✗ %s ⇒ 停" % RES["fatal"])
        return 2

    import numpy as np
    import pynq
    from pynq import Overlay, Clocks, MMIO

    # 清 PYNQ 残留状态（不先清，Overlay 会认为已加载同一 overlay）
    st_json = os.path.join(os.path.dirname(os.path.abspath(pynq.__file__)),
                           "pl_server", "global_pl_state_.json")
    for p in (st_json, os.path.join(os.path.dirname(st_json), "_current_metadata.pkl")):
        try:
            os.remove(p)
            log("  已清 %s" % p)
        except OSError as e:
            log("  (没清掉 %s：%s)" % (p, e))

    t0 = time.time()
    try:
        ov = Overlay(bit, download=True)
    except Exception as e:                                # noqa: BLE001
        import traceback
        traceback.print_exc()
        RES["fatal"] = "Overlay(download=True) 失败：%r" % (e,)
        RES["steps"]["1_load"] = "failed"
        jwrite(res_path, RES)
        log("  ✗ %s ⇒ 停" % RES["fatal"])
        return 8
    dt_load = time.time() - t0
    log("  ✓ load 完成（Overlay(download=True)）用时 %.2f s" % dt_load)
    RES["load_seconds"] = round(dt_load, 2)

    # ---- 三个 IP 基址（从 hwh 解析出的 ip_dict 读，不 mmap ⇒ 不会 SIGBUS）
    ips = {}
    for name in ("conv_uni_0", "bin_unit_0", "fconv_0"):
        ent = ov.ip_dict.get(name)
        if ent is None:
            cand = [k for k in ov.ip_dict if k.startswith(name)]
            ent = ov.ip_dict[cand[0]] if cand else None
        if ent is None:
            ips[name] = {"missing": True}
        else:
            ips[name] = {"phys_addr": int(ent.get("phys_addr", -1)),
                         "addr_range": int(ent.get("addr_range", -1))}
    for name, want in (("conv_uni_0", BASE_CONV), ("bin_unit_0", BASE_BIN),
                       ("fconv_0", BASE_FCONV)):
        got = ips[name].get("phys_addr")
        ips[name]["expected"] = want
        ips[name]["match"] = bool(got == want)
        log("  IP %-11s base=%s  期望=%#x  %s"
            % (name, ("%#x" % got) if got is not None and got >= 0 else "缺失", want,
               "✓" if got == want else "✗"))
    RES["ip_bases"] = ips

    # ---- 复位指纹（bin_unit_0 @0x68/0x6C；RW 寄存器，裸载读 0 属正常，不当失败）
    try:
        mmb = MMIO(BASE_BIN, 0x1000)
        fp = {"alpha_shift": mmb.read(0x68) & 0xFFFFFFFF,
              "slope_shift": mmb.read(0x6C) & 0xFFFFFFFF,
              "ap_ctrl": mmb.read(0x00) & 0xFFFFFFFF}
    except Exception as e:                                # noqa: BLE001
        fp = {"error": repr(e)}
    log("  bin_unit_0 复位指纹：alpha_shift=%s  slope_shift=%s  AP_CTRL=%s"
        % (fp.get("alpha_shift"), fp.get("slope_shift"), fp.get("ap_ctrl")))
    log("  （alpha_shift 是 RW 寄存器：裸载读 0 正常，不作为失败判据）")
    RES["fingerprint"] = fp

    # ---- 时钟（load 后默认值，第 2 段再设 25）
    try:
        cur = float(Clocks.fclk0_mhz)
    except Exception as e:                                # noqa: BLE001
        cur = None
        log("  ⚠ 读 Clocks.fclk0_mhz 失败：%r" % (e,))
    log("  Clocks.fclk0_mhz 读回（load 后默认） = %s" % cur)
    RES["fclk0_after_load"] = cur
    RES["steps"]["1_load"] = "ok"
    jwrite(res_path, RES)

    if args.only_load:
        log("  --only-load ⇒ 第 1 段结束")
        return 0

    # ---------------------------------------------------------------- §2
    log("")
    log("=== T-86 §2 fclk0=%.0f MHz + 0x40080000 读通性 ===" % args.fclk)
    try:
        Clocks.fclk0_mhz = args.fclk
        time.sleep(0.3)
        rb = float(Clocks.fclk0_mhz)
    except Exception as e:                                # noqa: BLE001
        rb = None
        log("  ✗ 设 fclk0 失败：%r" % (e,))
    log("  Clocks.fclk0_mhz 目标 %.0f → 读回 %s  %s"
        % (args.fclk, rb, "✓" if rb is not None and abs(rb - args.fclk) < 0.5 else "✗"))
    RES["fclk0_set"] = rb
    RES["fclk0_ok"] = bool(rb is not None and abs(rb - args.fclk) < 0.5)

    def fconv_read_body():
        from fconv import FconvEngine

        class _Shim:
            def __init__(self, mm):
                self.mm = mm

            def read(self, off):
                return self.mm.read(int(off)) & 0xFFFFFFFF

            def write(self, off, v):
                self.mm.write(int(off), int(v) & 0xFFFFFFFF)

        mm = MMIO(BASE_FCONV, 0x20000)
        eng = FconvEngine(_Shim(mm))
        nl = eng.nlanes()                     # 先读 R_NLANE（期望 4）
        st = eng.status()
        apret = eng._rd(CFG + OFF_APRET)
        mode = eng._rd(CFG + OFF_MODE)
        bx = eng._rd(0x10)                    # 基址页 B_X（RW，裸载任意值）
        return dict(R_NLANE=nl, STATUS=st, AP_RETURN=apret, R_MODE=mode,
                    B_X=bx, expect_nlane=4)

    r2 = run_child(fconv_read_body, os.path.join(args.out, "t86_child_fconv.json"), 30.0,
                   "fconv")
    RES["fconv_probe"] = r2
    if r2.get("signaled"):
        log("  ✗✗ 0x40080000 仍然 %s ⇒ v44 的 SIGBUS **未消除**，按判据停（不做冒烟）"
            % r2.get("signal_name"))
        RES["sigbus_fconv"] = True
        RES["steps"]["2_fconv"] = "SIGBUS"
        jwrite(res_path, RES)
        return 3
    if r2.get("timeout") or not (r2.get("child") or {}).get("ok"):
        log("  ✗ 0x40080000 读失败（非信号）：%r" % (r2.get("child"),))
        RES["steps"]["2_fconv"] = "error"
        jwrite(res_path, RES)
        return 4
    fr = r2["child"]["result"]
    log("  ✓ 0x40080000 读通了（v44 的 SIGBUS 已消除）：R_NLANE=%d（期望 4）%s"
        % (fr["R_NLANE"], " ✓" if fr["R_NLANE"] == 4 else " ✗"))
    log("    STATUS=%#010x  AP_RETURN=%#010x  R_MODE=%#010x  B_X=%#010x"
        % (fr["STATUS"], fr["AP_RETURN"], fr["R_MODE"], fr["B_X"]))
    RES["sigbus_fconv"] = False
    RES["steps"]["2_fconv"] = "read_ok"
    RES["nlanes_ok"] = bool(fr["R_NLANE"] == 4)
    jwrite(res_path, RES)

    # ---- 对照：未映射地址必须仍 SIGBUS
    def unmapped_body():
        mm = MMIO(BASE_UNMAPPED, 0x1000)
        val = mm.read(0x00)
        return dict(read_back=int(val))

    r2c = run_child(unmapped_body, os.path.join(args.out, "t86_child_unmapped.json"),
                    20.0, "0x400A0000")
    RES["unmapped_control"] = r2c
    if r2c.get("signaled"):
        log("  ✓ 对照 0x400A0000 仍 %s（探针方式正确）" % r2c.get("signal_name"))
        RES["unmapped_sigbus"] = True
    else:
        log("  ⚠ 对照 0x400A0000 **没有** SIGBUS（exit=%s）⇒ 探针方式有问题，读数不可信"
            % r2c.get("exitcode"))
        RES["unmapped_sigbus"] = False
    jwrite(res_path, RES)

    # ---------------------------------------------------------------- §3
    log("")
    log("=== T-86 §3 HP2 4 KB 恒等卷积冒烟 ===")
    if not RES.get("nlanes_ok"):
        log("  ✗ R_NLANE != 4 ⇒ 先不冒烟（资源档位不对，结论会混）")
        RES["steps"]["3_hp2"] = "skipped(nlanes)"
        jwrite(res_path, RES)
        return 5

    # 缓冲在父进程分配并 flush 干净（CLK 行不脏），内容在子进程里填
    n = 8192
    try:
        x = pynq.allocate(4096, dtype=np.uint8)
        w = pynq.allocate(4096, dtype=np.uint8)
        y = pynq.allocate(n, dtype=np.uint8)
    except Exception as e:                                # noqa: BLE001
        log("  ✗ CMA 分配失败：%r ⇒ 冒烟跳过（HP2 未被验证）" % (e,))
        RES["steps"]["3_hp2"] = "skipped(cma)"
        RES["hp2_cma_error"] = repr(e)
        jwrite(res_path, RES)
        return 9
    np.frombuffer(x, dtype=np.uint8)[:] = 0
    np.frombuffer(w, dtype=np.uint8)[:] = 0
    np.frombuffer(y, dtype=np.uint8)[:4096] = 0
    np.frombuffer(y, dtype=np.uint8)[4096:] = 0xA5
    x.flush(); w.flush(); y.flush()
    log("  缓冲：x pa=%#x  w pa=%#x  y pa=%#x（y 尾 4 KB 金丝雀）"
        % (int(x.physical_address), int(w.physical_address), int(y.physical_address)))
    RES["buffers"] = {"x_pa": int(x.physical_address), "w_pa": int(w.physical_address),
                      "y_pa": int(y.physical_address)}

    r3 = run_child(lambda: hp2_smoke_body(x, w, y),
                   os.path.join(args.out, "t86_child_hp2.json"), 120.0, "hp2")
    RES["hp2"] = r3
    ch = (r3.get("child") or {})
    if r3.get("signaled"):
        log("  ✗✗ 冒烟子进程被 %s 打死 ⇒ 停" % r3.get("signal_name"))
        RES["steps"]["3_hp2"] = "crashed:%s" % r3.get("signal_name")
        jwrite(res_path, RES)
        return 6
    if not ch.get("ok"):
        log("  ✗ 冒烟失败：%r" % (ch.get("error") or r3,))
        RES["steps"]["3_hp2"] = "error"
        jwrite(res_path, RES)
        return 7
    hr = ch["result"]
    log("  md5(x) = %s" % hr["md5_x"])
    log("  md5(y) = %s   %s" % (hr["md5_y"], "✓ 相等" if hr["equal_md5"] else "✗ 不等"))
    log("  字节差异 %d / 4096   f32 逐位差异 %d / 1024" % (hr["n_byte_diff"], hr["n_f32_bit_diff"]))
    d = hr["dbg"]
    log("  DBG hit=%d rdreq=%d nAR=%d fills=%d nW=%d nAW=%d nR=%d stall=%d flag=%#x"
        % (d["hit"], d["rdreq"], d["nAR"], d["fills"], d["nW"], d["nAW"], d["nR"],
           d["stall"], d["flag"]))
    log("  STATUS=%#010x  AP_RETURN=%#010x  用时 %.4f s"
        % (hr["status"], hr["ap_return"], hr["seconds"]))
    log("  x 头 32B = %s" % hr["x_head"])
    log("  y 头 32B = %s" % hr["y_head"])
    c = hr["dbg_counts_nonzero"]
    ok = (hr["equal_md5"] and hr["dbg_flag_low3"] == 0 and hr["status_err"] == 0
          and c["rdreq"] and c["nAR"] and c["nAW"] and c["nR"])
    RES["steps"]["3_hp2"] = "ok" if ok else "failed"
    log("  ⇒ §3 %s" % ("✓ 通过" if ok else "✗ 未通过（见上表判读）"))
    jwrite(res_path, RES)
    log("")
    log("T-86 结束：steps=%s" % RES["steps"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
