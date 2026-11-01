#!/usr/bin/env python3
"""静态检查：模块的 output 端口是否被真正驱动。

为什么需要（2026-10-27 上板事故）：
`k1_axi_wrap.v` 的 `s_axi_bvalid` 声明成 output logic 却从未被赋值 ⇒ 综合成
常量 0 ⇒ PS 每笔写等不到 B 响应 ⇒ CPU 停在 MMIO 写上一去不回，整机卡死
（ARP 都不回）。**仿真完全抓不到**：TB 的写任务不等 bvalid，纯读的冒烟测试也过。

判据：把模块端口表按逗号切开，逐条判定方向；对每个 output 端口名，
统计它在注释剥离后的全文出现次数——只有 1 次（即声明本身）就是悬空。
"""
import re, sys, glob

def strip_comments(s):
    s = re.sub(r'/\*.*?\*/', ' ', s, flags=re.S)
    s = re.sub(r'//[^\n]*', ' ', s)
    return s

def module_ports(body):
    """返回 [(direction, name)]，只解析第一个 module 的端口表。"""
    m = re.search(r'\bmodule\s+\w+\s*(?:#\s*\([^;]*?\)\s*)?\((.*?)\)\s*;', body, re.S)
    if not m: return []
    plist = m.group(1)
    out = []
    for item in plist.split(','):
        it = re.sub(r'\(\*.*?\*\)', ' ', item)          # 去属性
        it = re.sub(r'\[[^\]]*\]', ' ', it)             # 去位宽
        toks = it.split()
        if not toks: continue
        if toks[0] in ('input', 'output', 'inout'):
            dirn = toks[0]
            # 端口名 = 最后一个标识符
            nm = None
            for t in reversed(toks):
                if re.fullmatch(r'[A-Za-z_]\w*', t):
                    nm = t; break
            if nm: out.append((dirn, nm))
    return out

fail = 0
files = sorted(glob.glob('modules/*.v') + glob.glob('modules/*.sv'))
for f in files:
    body = strip_comments(open(f, encoding='utf-8', errors='replace').read())
    for dirn, name in module_ports(body):
        if dirn != 'output': continue
        n = len(re.findall(r'(?<![\w.])%s(?![\w])' % re.escape(name), body))
        if n <= 1:
            print('%s: [未驱动] output %s —— 全文只出现 1 次（仅声明）' % (f, name))
            fail += 1

print()
print('检查了 %d 个文件，发现 %d 处未驱动的 output' % (len(files), fail))
sys.exit(1 if fail else 0)
