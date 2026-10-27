#!/usr/bin/env python3
"""check_concat_width.py —— 查"拼接内的自决定位宽"陷阱（2026-10-27 踩到，导致 hw8=1024 而非 5120）

LRM：concatenation 的操作数是 **self-determined**，所以 `{pad, (a*b), pad}` 里的
乘法按操作数位宽求值，自然位宽被截断，而 LHS 的上下文宽度**不会**传播进去。

同理危险：`{pad, (a + b), pad}`、`{pad, (a >> n), pad}`。
安全写法：先赋给显式位宽的 wire，再拼接。

用法：python3 tools/check_concat_width.py modules/*.v
"""
import re, sys, glob

def scan(path):
    src = open(path, encoding='utf-8').read()
    src = re.sub(r'//[^\n]*', '', src)          # 去行注释
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    bad = []
    for m in re.finditer(r'\{[^{}]*\}', src):
        body = m.group(0)
        # 先去掉位选 [..]（AW-1 里的 '-' 不是算术）与括号分组
        t = re.sub(r'\[[^\]]*\]', 'X', body)
        # 拼接内出现二元算术（* / + - << >>）
        if re.search(r'X|\w', t) and (re.search(r'[*]', t) or
           re.search(r'\w\s*[-+]\s*\w', t) or re.search(r'<<|>>', t) or
           re.search(r'/\s*\w', t)):
            ln = src[:m.start()].count('\n') + 1
            bad.append((ln, body.strip()[:110]))
    return bad

total = 0
for pat in sys.argv[1:] or ['modules/*.v', 'tb/*.v']:
    for f in sorted(glob.glob(pat)):
        for ln, body in scan(f):
            print(f"{f}:{ln}: 拼接内含算术 ⇒ 先赋显式位宽 wire: {body}")
            total += 1
print(f"[check_concat_width] {'FAIL' if total else 'PASS'} ({total} 处)")
sys.exit(1 if total else 0)
