# tb/ —— 输出写越界探针（自包含，一键复跑）

两个 TB，都不需要 golden 向量（写地址与数据无关），都不改生产 RTL。

| 文件 | 层级 | 判据 |
|---|---|---|
| `tb_oob_conv8.v` | `bin_conv_core8`（单元） | 逐拍记录 `mem_wr_en/addr/sel`；`lastbyte = max(addr+最高置位 sel 位)`；与 `out_bytes = cg·Hout·Wout·8` 相减 ⇒ `oob` |
| `tb_oob_wrap.v` | `bin_unit_axi_wrap`（整条 AXI 包装层） | AXI3 写从机按 `WSTRB` 逐字节落内存模型，统计 `beats/wrbytes/over`；输出区尾部 4 KB 预填 `0xA5`，跑完扫金丝雀（= 板上 `BinGuard.check_canary` 的仿真版） |

## 跑

```bash
cd pynq_specialized_code          # memgate 的相对路径按这个 cwd 算
bash tools/memgate.sh 8 -- bash ../runs/opt_round1/oob_write/tb/run_unit.sh
bash tools/memgate.sh 8 -- bash ../runs/opt_round1/oob_write/tb/run_wrap.sh -testplusarg SWEEP=1
bash tools/memgate.sh 8 -- bash ../runs/opt_round1/oob_write/tb/run_wrap.sh \
     -testplusarg SWEEP=1 -testplusarg WSLOW=1 -testplusarg RSLOW=1   # DDR 慢模型
```

单元级也可单形状：`-testplusarg SWEEP=0 -testplusarg COUT=96 -testplusarg CIN=64
-testplusarg K=3 -testplusarg PAD=1 -testplusarg H=256 -testplusarg W=256`

## plusarg

* `tb_oob_conv8`：`+SWEEP`(1/0) `+COUT +CIN +K +S +D +PAD +H +W +SHIFT +BASEH +VERBOSE`
* `tb_oob_wrap`：`+SWEEP`(1/0) `+CIN +COUT +K +S +D +PAD +H +W +SHIFT +MAXCYC +WSLOW +RSLOW`

## 输出格式（好抓）

```
OOBREP tag=sw12 cout=96 cin=64 k=1 s=1 d=1 pad=0 H=256 W=256 sh=8
OOBREP2 base=00100000 Hout=256 Wout=256 cg=12 exp=6291456 lastbyte=7340031 oob=0 beats=786432 wrbytes=6291456 misalign=0 timeout=0
```
`oob>0` ⇒ 越界；`beats*8 != wrbytes` ⇒ 有零星字节写；`misalign>0` ⇒ 非 8 字节对齐写。

## 已知坑（别重犯）

1. TB 里 `@(posedge clk)` 之后用**阻塞赋值**驱动 DUT 输入，会与 DUT 的 `always_ff` 同拍竞争
   （`start` 脉冲被吃掉、DUT 永远 IDLE）⇒ 一律用 `<=`。
2. xsim 对 `$display({"...","..."}, args)` 的拼接格式串会打出上千位十进制垃圾
   ⇒ 格式串必须是单个字面量。
3. `$countones()` 赋值给 `longint` 会得到 `n-2^32` ⇒ 手写循环计数。
4. AXI 读从机模型不要用寄存器版 `arready`（会在同一拍把 AR 吞掉丢包，引擎 `rd_hit`
   永不成立 ⇒ 死锁）⇒ 组合 `arready` + 8 项队列；`RID` 必须回送 `ARID`。
5. `xvlog` 要求变量**先声明后使用**（本例踩过：把 LITE 主机的 `l_*` 声明写在了例化之后）。
