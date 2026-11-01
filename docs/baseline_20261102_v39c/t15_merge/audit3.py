# -*- coding: utf-8 -*-
import re, subprocess, os
R = "/media/felix/Data/coding/demoir-fpga"
def parked_for(rel):
    out, cur = [], None
    for ln in open(os.path.join(R,"runs/parked_20261102/parked_ps_work.patch"), encoding="utf-8", errors="replace"):
        if ln.startswith("diff --git "):
            cur = re.search(r" b/(.*)$", ln.rstrip("\n")).group(1)
        elif cur == rel and ln.startswith("+") and not ln.startswith("+++"):
            out.append(ln[1:].rstrip("\n"))
    return set(out)
for rel in ["pynq_specialized_code/ps/pynq/bin_engine.py",
            "pynq_specialized_code/ps/pynq/bin_net.py",
            "pynq_specialized_code/ps/pynq/board_v39_verify.py"]:
    head = set(subprocess.run(["git","-C",R,"show","HEAD:"+rel],capture_output=True,text=True).stdout.splitlines())
    ps = parked_for(rel)
    wt = open(os.path.join(R,"worktrees/T-14-deps",rel), encoding="utf-8").read().splitlines()
    mg = open(os.path.join(R,rel), encoding="utf-8").read().splitlines()
    mg_set = set(mg)
    dropped = [x for x in wt if x not in mg_set]
    unexplained = [x for x in dropped if x not in ps and x not in head]
    print("%-16s dropped=%3d  in-parked-additions=%3d  also-in-HEAD=%3d  UNEXPLAINED=%d"
          % (rel.split("ps/")[-1], len(dropped),
             sum(1 for x in dropped if x in ps),
             sum(1 for x in dropped if x in head),
             len(unexplained)))
    for x in unexplained[:5]: print("    !", repr(x))
