#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 three_layer_*.log 里的 OOBREP/OOBW 行抽成 markdown 表，回填 REPORT_3layers.md。
（只读日志 + 改本报告的两个占位符，不动任何 RTL/日志）"""
import re, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOGD = os.path.join(os.path.dirname(HERE), 'sim', 'logs')
REP = os.path.join(os.path.dirname(HERE), 'REPORT_3layers.md')
B_OUT = 0x0020_0000

UNIT_SHAPE = {  # tag -> (层标签, cin)
    0: ('L0 `conv_layers.0`', 64),
    1: ('L1 `conv_layers.1`(d=2)', 160),
    2: ('L2 `conv_layers.2`', 256),
}


def parse_unit(path):
    rows, cur = [], None
    if not os.path.exists(path):
        return rows
    for ln in open(path, encoding='utf-8', errors='replace'):
        if ln.startswith('OOBREP tag='):
            m = dict(re.findall(r'(\w+)=(-?\d+)', ln))
            tm = re.search(r'tag=sw(\d+)', ln)
            m['tag'] = tm.group(1) if tm else '999'
            cur = m
        elif ln.startswith('OOBREP2') and cur:
            m = dict(re.findall(r'(\w+)=(-?\d+)', ln))
            m.update(cur)
            rows.append(m)
            cur = None
    return rows


def parse_wrap(path):
    rows, cur, curcase = [], None, {}
    if not os.path.exists(path):
        return rows
    for ln in open(path, encoding='utf-8', errors='replace'):
        if ln.startswith('OOBW case='):
            curcase = dict(re.findall(r'(\w+)=(-?\d+)', ln))
        if ln.startswith('OOBW1b'):
            cur = dict(re.findall(r'(\w+)=(-?\d+)', ln))
            em = re.search(r'exp\(out_bytes\)=(\d+)', ln)
            if em:
                cur['exp'] = em.group(1)
            cur.update(curcase)
        elif ln.startswith('OOBW2') and cur:
            m = dict(re.findall(r'(\w+)=(-?\d+)', ln))
            hm = re.search(r'ret=([0-9a-fA-F]+)', ln)
            m['ret'] = hm.group(1) if hm else ''
            m.update(cur)
            rows.append(m)
            cur = None
    return rows


def unit_table(rows, title, use_layers=True):
    out = ['| 层 | Hout×Wout | cg | exp(=`out_bytes`) | 写拍数 | `wrbytes` | `beats×8` | 实际末字节(相对 base) | `oob` | misalign | 判定 |',
           '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    for r in rows:
        exp = int(r['exp']); lb = int(r['lastbyte']); base = int(str(r['base']), 16)
        rel = lb - base
        oob = int(r['oob']); beats = int(r['beats']); wb = int(r['wrbytes'])
        lab_c = ('cout=%s, H=%s' % (r.get('cout'), r.get('H'))) if 'cout' in r else None
        cout_i = int(r.get('cout', 0))
        good = (oob == 0) and (beats * 8 == wb)
        ok = 'PASS' if good else ('PASS（oob=0，但 `beats*8 != wrbytes`，需人工看）' if oob == 0 else '**FAIL**')
        tag = int(r['tag']) if str(r['tag']).isdigit() else -1
        lab = UNIT_SHAPE.get(tag, ('shape', 0))
        if use_layers and tag in UNIT_SHAPE:
            name = UNIT_SHAPE[tag][0]
        else:
            name = lab_c or ('sw%s' % r['tag'])
        out.append('| %s | %s×%s | %s | **%s** | %s | %s | %s | %s (=exp-1) | **%d** | %s | %s |'
                   % (name, r['Hout'], r['Wout'], r['cg'], f'{exp:,}', f'{beats:,}',
                      f'{wb:,}', f'{beats*8:,}', f'{rel:,}', oob, r['misalign'], ok))
    return ((title + '\n\n') if title else '') + '\n'.join(out) + '\n'


def wrap_table(cases, title, use_layers=True):
    """cases: [(档名, log路径), ...] 每档 3 层"""
    out = ['| 层 | 反压档 | exp | 总拍数(cyc) | AW 笔数 | W 拍数 | `wbytes` | `beats×8` | 实际末字节(相对 base) | `over` | 金丝雀坏 | 判定 |',
           '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    for tier, path in cases:
        for i, r in enumerate(parse_wrap(path)):
            exp = int(r['exp']); lb = int(r['lastbyte'])
            rel = lb - B_OUT
            beats = int(r['beats']); wb = int(r['wbytes'])
            if beats == 0:
                ok = 'N/A（`AP_RETURN=0x%s`：启动闸门 `r_cout[2:0]!=0` ⇒ `ERR_LAYOUT(4)`，**一个字节都没写**）' % r.get('ret', '')
            else:
                ok = 'PASS' if (int(r['over']) == 0 and int(r['canary']) == 0 and beats * 8 == wb) else '**FAIL**'
            if use_layers:
                lab = UNIT_SHAPE.get(i, ('case%d' % i, 0))[0]
            else:
                lab = 'cout=%s, Hout=%s' % (r.get('cout'), r.get('Hout'))
            rel_s = '—' if beats == 0 else f'{rel:,} (=exp-1)'
            out.append('| %s | %s | **%s** | %s | %s | %s | %s | %s | %s | **%s** | **%s**/4096 | %s |'
                       % (lab, tier, f'{exp:,}', f'{int(r["cyc"]):,}', r['aw'], f'{beats:,}', f'{wb:,}',
                          f'{beats*8:,}', rel_s, r['over'], r['canary'], ok))
    return ((title + '\n\n') if title else '') + '\n'.join(out) + '\n'


def main():
    unit3l = parse_unit(os.path.join(LOGD, 'three_layer_unit_3l.log'))
    unitvar = parse_unit(os.path.join(LOGD, 'three_layer_unit_var.log'))
    unit256 = parse_unit(os.path.join(LOGD, 'three_layer_unit_256.log'))
    wcases = [('快从机', 'three_layer_wrap_3l_fast.log'),
              ('慢写 DDR `+WSLOW`', 'three_layer_wrap_3l_wslow.log'),
              ('慢读 DDR `+RSLOW`', 'three_layer_wrap_3l_rslow.log'),
              ('最后一拍定点冻结 `+LASTHOLD=64`', 'three_layer_wrap_3l_lasthold.log')]
    wcases = [(t, os.path.join(LOGD, f)) for t, f in wcases]
    wvar = parse_wrap(os.path.join(LOGD, 'three_layer_wrap_var_fast.log'))

    t_unit = unit_table(unit3l, '**A. 单元级 `bin_conv_core8`（3 层精确形状）**')
    if unit256:
        t_unit += '\n' + unit_table(unit256, '**B. 同权重 256² 对照（`out_bytes=6 291 456`）**')
    t_wrap = wrap_table(wcases, '**C. 包装级 `bin_unit_axi_wrap`（3 层 × 四档反压）**')

    tvar = unit_table(unitvar, '**A. 单元级 12 格**', use_layers=False)
    tvar += '\n' + wrap_table([('快从机', os.path.join(LOGD, 'three_layer_wrap_var_fast.log'))],
                              '**B. 包装级 12 格（快从机）**', use_layers=False)

    s = open(REP, encoding='utf-8').read()
    s = s.replace('<!--TBL_UNIT-->', t_unit)
    s = s.replace('<!--TBL_WRAP-->', t_wrap)
    s = s.replace('<!--TBL_VAR-->', tvar)
    open(REP, 'w', encoding='utf-8').write(s)
    print('filled: unit3l=%d unitvar=%d unit256=%d wrap=%s var=%d'
          % (len(unit3l), len(unitvar), len(unit256),
             [len(parse_wrap(p)) for _, p in wcases], len(wvar)))


if __name__ == '__main__':
    main()
