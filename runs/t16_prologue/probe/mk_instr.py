import re, sys
SRC="/media/felix/Data/coding/demoir-fpga/worktrees/T-16-prologue/pynq_specialized_code/ps/pynq/bin_net.py"
DST="/tmp/probe_T16/bin_net_instr.py"
s=open(SRC).read()
marks=0
def rep(old,new,cnt=1):
    global s,marks
    assert s.count(old)==cnt, (old[:60], s.count(old))
    s=s.replace(old,new,cnt); marks+=1

rep('''    def run(self, x0, pipeline=True, nbuf=2, verify=False, arena=None):''',
    '''    def run(self, x0, pipeline=True, nbuf=2, verify=False, arena=None):  # PROBE-INSTR''')

rep('''        t_all = time.time()
        t = np.asarray(x0, np.float32)''',
    '''        t_all = time.time()
        import json as _json
        _PR = dict(mark={}, poll_n=0, loop_it=0)
        PROBE_OUT["pr"] = _PR
        _PR["mark"]["m_enter"] = time.perf_counter()
        t = np.asarray(x0, np.float32)''')

rep('''        g, outs, tin = self.build_graph(t)''',
    '''        _t = time.perf_counter()
        g, outs, tin = self.build_graph(t)
        _PR["mark"]["build_graph"] = time.perf_counter() - _t
        _t = time.perf_counter()''')

rep('''        self._analyze_lazy(g, outs)''',
    '''        _PR["mark"]["pre_analyze"] = time.perf_counter() - _t
        _t = time.perf_counter()
        self._analyze_lazy(g, outs)
        _PR["mark"]["analyze_lazy"] = time.perf_counter() - _t
        _t = time.perf_counter()''')

rep('''        while n_done < n_ops:''',
    '''        _PR["mark"]["setup_to_loop"] = time.perf_counter() - _t
        _t = time.perf_counter()
        while n_done < n_ops:
            _PR["loop_it"] += 1''')

rep('''        dur = time.time() - t_all
        ps_total = t_pro + t_epi + sum(prof.values())''',
    '''        _PR["mark"]["loop"] = time.perf_counter() - _t
        _t = time.perf_counter()
        dur = time.time() - t_all
        ps_total = t_pro + t_epi + sum(prof.values())''')

rep('''        return {k: self._materialize(t.val) for k, t in outs.items()}, self.stats''',
    '''        _PR["mark"]["post_loop_stats"] = time.perf_counter() - _t
        _t = time.perf_counter()
        _r = {k: self._materialize(t_.val) for k, t_ in outs.items()}
        _PR["mark"]["out_materialize"] = time.perf_counter() - _t
        _PR["mark"]["run_total"] = time.perf_counter() - _PR["mark"]["m_enter"]
        return _r, self.stats''')

# poll wrapper inside _PlBackend: count
rep('''    def poll(self, task):
        """非阻塞：done 了就记下时刻（一次 MMIO 读，25 µs）。"""''',
    '''    def poll(self, task):
        """非阻塞：done 了就记下时刻（一次 MMIO 读，25 µs）。"""
        _PR = PROBE_OUT.get("pr")
        if _PR is not None:
            _PR["poll_n"] = _PR.get("poll_n", 0) + 1''')

s = s.replace("from __future__ import annotations\n", "from __future__ import annotations\nPROBE_OUT = {}\n", 1)
open(DST,"w").write(s)
print("marks",marks)
