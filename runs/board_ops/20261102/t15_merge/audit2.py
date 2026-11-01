# -*- coding: utf-8 -*-
"""证明 merged = HEAD + T-14inc：merged 里所有"worktree 没有"的行必须来自 HEAD 或 T-14 增量。"""
import re, subprocess, os
R = "/media/felix/Data/coding/demoir-fpga"
def split_patch(path, want):
    out, cur = {}, None
    for ln in open(path, encoding="utf-8", errors="replace"):
        if ln.startswith("diff --git "):
            cur = re.search(r" b/(.*)$", ln.rstrip("\n")).group(1); out[cur] = []
        elif cur == want and ln.startswith("+") and not ln.startswith("+++"):
            out[cur].append(ln[1:].rstrip("\n"))
    return out[want]

inc = open("/tmp/probe_T15/t14_pure.patch", encoding="utf-8").read()
for f, tag in [("bin_engine", "bin_engine"), ("bin_net", "bin_net"),
               ("board_v39", "board_v39_verify")]:
    rel = "pynq_specialized_code/ps/pynq/%s.py" % tag
    head = subprocess.run(["git", "-C", R, "show", "HEAD:" + rel],
                          capture_output=True, text=True).stdout.splitlines()
    t14add = split_patch_inc = None
    # T-14 additions from the increment patch
    cur = None; t14add = []
    for ln in inc.splitlines():
        if ln.startswith("diff --git "):
            cur = re.search(r" b/(.*)$", ln).group(1)
        elif cur == rel and ln.startswith("+") and not ln.startswith("+++"):
            t14add.append(ln[1:])
    mg = open(os.path.join(R, rel), encoding="utf-8").read().splitlines()
    wt = open(os.path.join(R, "worktrees/T-14-deps", rel), encoding="utf-8").read().splitlines()
    wt_set = set(wt)
    allowed = set(head) | set(t14add)
    unknown = [(i + 1, x) for i, x in enumerate(mg)
               if x not in wt_set and x not in allowed]
    print("%-18s merged=%d lines; lines only-in-merged = %d; of which NOT in HEAD/T14add = %d"
          % (tag, len(mg), sum(1 for x in mg if x not in wt_set), len(unknown)))
    for i, x in unknown[:10]:
        print("   L%-5d %r" % (i, x))
