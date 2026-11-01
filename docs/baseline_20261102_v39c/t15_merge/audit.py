# -*- coding: utf-8 -*-
"""C6 归属审计：merged vs T14-worktree 的每个 hunk 是否都能归到 parked patch。"""
import re, subprocess, os
R = "/media/felix/Data/coding/demoir-fpga"
PARKED = os.path.join(R, "runs/parked_20261102/parked_ps_work.patch")

def split_patch(path):
    """→ {relpath: {'+':[...], '-':[...]}}"""
    out, cur = {}, None
    for ln in open(path, encoding="utf-8", errors="replace"):
        if ln.startswith("diff --git "):
            m = re.search(r" b/(.*)$", ln.rstrip("\n"))
            cur = m.group(1); out[cur] = {"+": [], "-": []}
        elif cur and ln.startswith("+++ ") or ln.startswith("--- "):
            continue
        elif cur and ln.startswith("+"):
            out[cur]["+"] .append(ln[1:].rstrip("\n"))
        elif cur and ln.startswith("-"):
            out[cur]["-"].append(ln[1:].rstrip("\n"))
    return out

parked = split_patch(PARKED)
files = ["pynq_specialized_code/ps/pynq/bin_engine.py",
         "pynq_specialized_code/ps/pynq/bin_net.py",
         "pynq_specialized_code/ps/pynq/board_v39_verify.py"]
for f in files:
    wt = os.path.join(R, "worktrees/T-14-deps", f)
    mg = os.path.join(R, f)
    d = subprocess.run(["diff", "-u", wt, mg], capture_output=True, text=True).stdout
    dropped, added, hunks = [], [], []
    for ln in d.splitlines():
        if ln.startswith("@@"):
            hunks.append(ln)
        elif ln.startswith("+++") or ln.startswith("---"):
            continue
        elif ln.startswith("-"):
            dropped.append(ln[1:])
        elif ln.startswith("+"):
            added.append(ln[1:])
    pset = set(parked.get(f, {}).get("+", []))
    pneg = set(parked.get(f, {}).get("-", []))
    ndrop_missing = [x for x in dropped if x not in pset]
    print("=" * 78)
    print("%s\n  hunks=%d  dropped(in worktree only)=%d  added(in merged only)=%d"
          % (f.split("ps/")[-1], len(hunks), len(dropped), len(added)))
    print("  dropped-not-in-parked-additions: %d" % len(ndrop_missing))
    for x in ndrop_missing[:12]:
        print("     ! %r" % x)
    print("  added (merged-only) lines: %d" % len(added))
    for x in added[:12]:
        print("     + %r" % x)
    print("  hunk headers:")
    for h in hunks:
        print("     " + h)
