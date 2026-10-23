#!/usr/bin/env python3
"""收集上板所需文件到一个目录（板子/SD 卡拷贝用）。

产物 board_bundle/：
  design_1.bit / design_1.hwh（PL overlay，v2）
  test_on_board.py、run_network_on_board.py
  runner.py、ops_numpy.py、ref_numpy.py、conv_engine.py
  manifest_640.json
  weights_pack/（weights.bin/index.json/input_q.bin/ref_out.bin）
  BOARD_README.txt（操作单摘要）

用法（在 demoir-pl-lab 根目录）：
  python fpga/ps/tools/make_board_bundle.py [--out fpga/board/board_bundle]
"""
import argparse
import os
import shutil
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FPGA = os.path.abspath(os.path.join(HERE, "..", ".."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(FPGA, "board", "board_bundle"))
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)

    ovl = os.path.join(FPGA, "board", "overlay_engine_150mhz")
    pynq = os.path.join(FPGA, "ps", "pynq")
    pack = os.path.join(FPGA, "deploy", "weights_pack")

    copies = [
        (os.path.join(ovl, "design_1.bit"), "design_1.bit"),
        (os.path.join(ovl, "design_1.hwh"), "design_1.hwh"),
        (os.path.join(pynq, "test_on_board.py"), "test_on_board.py"),
        (os.path.join(pynq, "run_network_on_board.py"), "run_network_on_board.py"),
        (os.path.join(pynq, "runner.py"), "runner.py"),
        (os.path.join(pynq, "ops_numpy.py"), "ops_numpy.py"),
        (os.path.join(pynq, "ref_numpy.py"), "ref_numpy.py"),
        (os.path.join(pynq, "conv_engine.py"), "conv_engine.py"),
        # runner.py 在 import 期就依赖这几个（C8 布局表 + 256 项 LUT + v2 加速器）
        (os.path.join(pynq, "lut_util.py"), "lut_util.py"),
        (os.path.join(pynq, "c8_layout.py"), "c8_layout.py"),
        (os.path.join(pynq, "pp_ops.py"), "pp_ops.py"),
        (os.path.join(pynq, "pp_engine.py"), "pp_engine.py"),
        # int8 快速内核（板端首次使用时用 cc 现场编译；逐位自检不过就回落 numpy）
        (os.path.join(pynq, "i8_fast.py"), "i8_fast.py"),
        (os.path.join(pynq, "i8_fast.c"), "i8_fast.c"),
        (os.path.join(pynq, "sam_sum_fast.c"), "sam_sum_fast.c"),
        # C8 布局/算子支撑模块（C8_LAYOUT=1 时才 import；板上扁平目录）
        (os.path.join(FPGA, "ps", "c8", "c8lib.py"), "c8lib.py"),
        (os.path.join(FPGA, "ps", "c8", "ops_c8.py"), "ops_c8.py"),
        (os.path.join(FPGA, "ps", "baremetal", "main.c"), "main.c"),
        (os.path.join(FPGA, "ps", "tools", "check_consistency.py"),
         "check_consistency.py"),
        (os.path.join(FPGA, "ps", "tools", "check_pack_ref.py"),
         "check_pack_ref.py"),
        (os.path.join(FPGA, "ps", "manifest_640.json"), "manifest_640.json"),
    ]
    for src, dst in copies:
        if not os.path.exists(src):
            raise SystemExit("缺少文件: " + src)
        shutil.copy2(src, os.path.join(out, dst))
        print("copy:", dst)

    pack_dst = os.path.join(out, "weights_pack")
    os.makedirs(pack_dst, exist_ok=True)
    for name in ("weights.bin", "index.json", "input_q.bin", "ref_out.bin",
                 "ref_psnr.txt"):
        src = os.path.join(pack, name)
        if not os.path.exists(src):
            raise SystemExit("缺少文件: " + src)
        shutil.copy2(src, os.path.join(pack_dst, name))
        print("copy: weights_pack/" + name)

    # 溯源记录：新导出的包才有，老包缺了不致命
    info = os.path.join(pack, "build_info.json")
    if os.path.exists(info):
        shutil.copy2(info, os.path.join(pack_dst, "build_info.json"))
        print("copy: weights_pack/build_info.json")

    readme = """demoir PL+PS 上板包（生成于 %s）

文件说明
  design_1.bit / design_1.hwh    PL 卷积引擎 overlay（v3：CIN_MAX=352/
                                 COUT_MAX=192 全网络覆盖，relu_en 旁路）
  main.c                         路线 A：JTAG 裸机自检（拷进 Vitis 工程）
  test_on_board.py               第一步自检：5 个单算子用例（含 v3 最坏形状 352×192）
  run_network_on_board.py        第二步：整网推理 + 与 ref_out.bin 对拍
  runner.py / ops_numpy.py / ref_numpy.py / conv_engine.py   运行库
  c8_layout.py / c8lib.py / ops_c8.py    C8 交错布局（环境变量 C8_LAYOUT=1 启用，
                                默认 0 = 全部 CHW，行为与整合前逐位一致）
  pp_ops.py / pp_engine.py / lut_util.py  v2 层间算子加速器（--pp-ops 启用）
  i8_fast.py / i8_fast.c / sam_sum_fast.c  ARM 上的 C/NEON 精确内核（首次运行时
                                 自动 cc 编译 + 逐位自检；编不出来就回落 numpy）
  check_consistency.py           部署包一致性自检（上板后可复跑）
  check_pack_ref.py              第三步（可选）：用包重算整网与 ref_out.bin 逐位对拍
  manifest_640.json              整网执行清单（517 算子 / 247 卷积）
  weights_pack/                  权重 + 标度 + 参考输入输出

操作（PYNQ 板上）
  1) 确认 FCLK0 = 150 MHz（脚本会打印）
  2) sudo python3 test_on_board.py          预期：5 用例全过
  3) sudo python3 run_network_on_board.py --manifest manifest_640.json \\
         --pack weights_pack --input weights_pack/input_q.bin --bit design_1.bit
     预期：“与部署参考对比: 0/N 不一致”
  4) 若第 3 步不一致：先 sudo python3 check_pack_ref.py --pack weights_pack
     看包本身是否完好（应该 0 差异），再查 PL 与内存池。

换自己的照片（在 PC 上做，**不要 resize**）
  python fpga/ps/tools/prep_input.py --image 你的照片.jpg \\
      --manifest fpga/ps/manifest_640.json --out input_q.bin
  然后板上：run_network_on_board.py --input input_q.bin ...
  画幅必须与 manifest 一致、且是 32 的倍数；resize 会把摩尔纹频率混叠变形
  （上游铁律，docs/训练/说明_交付_20260910.md 3.4 有实测）。

若第 3 步不一致：先看 ref_out.bin 是否随包更新；再查内存池地址与缓存
（详见仓库 fpga/docs/on_board_checklist.md）。
""" % time.strftime("%Y-%m-%d %H:%M")
    with open(os.path.join(out, "BOARD_README.txt"), "w", encoding="utf-8") as f:
        f.write(readme)
    print("copy: BOARD_README.txt")

    total = 0
    for root, _, files in os.walk(out):
        for fn in files:
            total += os.path.getsize(os.path.join(root, fn))
    print("完成: %s（%.1f MB）" % (out, total / 1e6))


if __name__ == "__main__":
    main()
