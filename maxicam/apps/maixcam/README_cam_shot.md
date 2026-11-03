# cam_shot.py —— MaixCAM 精简采集服务（TCP 8660）

> 板子（Zynq）主动来要图，一问一答：发一行 JSON 请求，收回一行 JSON 头 + **恰好 `nbytes`**
> 字节的 **RGB888 原生像素中心裁剪**（默认 256×256 = 196,608 B）。
>
> 协议权威（唯一）：`demoir-fpga/docs/camera/07_端到端流水线_协议与模块图.md` **§3**（含 §3.5 口径、§3.6 守卫）。
> 本文的字段表与它逐条对齐，**没有自创字段**。
>
> ⚠️ **本支线的相机当前不可达**（`10.13.148.1` / `10.13.149.1` 均不通，`lsusb` 无 `359f:2120`）。
> 因此：**主机侧判据全部已实测**；**凡是真机才能验的部分，本文一律标「待设备」，没有一个数字是编的**。

---

## 1. 它和旧 app 有什么不同（为什么要另写一个）

| | 旧 `apps/maixcam/main.py`（1292 行） | 本服务 `cam_shot.py` |
|---|---|---|
| RTSP 推流 | 有（独占 VI 的 1 路） | **没有** |
| 屏幕显示 | 有（VPSS/VO 另 1 路） | **没有** |
| 采一张的流程 | 停流 → 等 **1.5 s** VI 释放 → 开 2K → 读 → 关流 → 恢复推流 | 读 → 裁 → `to_bytes` |
| 采一张的代价 | 3~4 s（旧代码注释里写的） | **待实测**（见 §8） |
| 相机通道 | MMF_VI_MAX_CHN=2 已被占满 | 只剩自己，一路够用 |

结论：**独占相机**把"停流/等 VI 释放/重建显示对象"这些坑整体消掉了，
所以本服务里没有 `stop_stream()`、没有 `VI_RELEASE_GAP`、也不碰 `display`。

> 旧 app 保留原样（`apps/maixcam/main.py` 一行未改），RTSP 预览仍然能用；
> 但**两者不能同时跑**——会抢相机，后开的那个 `camera.Camera()` 会失败。

---

## 2. 文件构成

| 文件 | 在哪跑 | 作用 |
|---|---|---|
| `apps/maixcam/cam_shot.py` | 相机 | 本服务（maix 薄壳：开门/曝光/取帧/裁剪） |
| `apps/maixcam/cam_core.py` | 两边都能跑 | 协议内核：解析、裁剪窗口、回包、TCP 循环（**不 import maix**） |
| `apps/pc/fake_camera.py` | 主机 | 假相机：§3 全路径 TCP server（板子端没有摄像头也能验客户端） |
| `apps/pc/cam_shot_probe.py` | 主机 | 连发要图 + 逐张核对 + 报 `t_cap_us`（§6.B 判据的可执行版） |
| `apps/pc/test_cam_proto.py` | 主机 | 门禁 1~4 的 59 条判据（不需要相机） |
| `apps/deploy.py` | 主机 | 部署：`shot` / `shot-check` / `shot-status` / `shot-logs` / `shot-stop` |

设备上只需要前两个文件（同目录，`cam_shot.py` 会自己把所在目录加进 `sys.path`）。

---

## 3. 快速开始（部署 → 起 → 验 → 停）

```bash
cd apps

./deploy.py shot             # 上传 cam_shot.py + cam_core.py 并后台启动（默认 8660 / 2560x1440）
./deploy.py shot-status      # 期望：== 采集服务 == 运行中 (8660)
./deploy.py shot-logs        # 看日志

./deploy.py shot-check       # ★ 上机第一件事：裁剪正确性自检（输出末行 PASS/FAIL）

python3 pc/cam_shot_probe.py --host 10.13.148.1 --count 5    # 连发 5 张并逐张核对

./deploy.py shot-stop        # 优雅停止（SIGTERM -> 应用自己关相机再退出）
```

不想用 `deploy.py` 时，手工两步等价：

```bash
ssh root@10.13.148.1 'mkdir -p /root/maxicam_shot'
scp apps/maixcam/cam_shot.py apps/maixcam/cam_core.py root@10.13.148.1:/root/maxicam_shot/
ssh root@10.13.148.1 'cd /root/maxicam_shot && nohup python3 cam_shot.py \
    --port 8660 --native 2560x1440 > cam_shot.log 2>&1 & echo $! > cam_shot.pid'
```

> ⚠️ **不要 `python3 -u`**：MMF 出错时 stderr 会疯狂刷屏，无缓冲会把 SD 卡写爆。
> 应用自己在每行日志后 `flush()`，所以日志照样实时。

---

## 4. 上机验收清单（可照抄）

> **本支线当前无设备，以下步骤全部「未实测」。** 跑完请把 §8 的「待实测」换成真实数字。

```bash
# 0) 网络先通（PC 上）。不通先查 USB NCM 网卡：ENVIRONMENT.md §3.2
ping -c2 10.13.148.1

# 1) 部署并启动
cd apps && ./deploy.py shot
./deploy.py shot-status                 # 期望：运行中 (8660)

# 2) ★ 裁剪正确性自检 —— 不过就别往下走
./deploy.py shot-check
#   期望末行：[check-crop] 结论：**PASS**（3/3 例逐字节相同）⇒ crop 是像素级子集，没有 resize
#   若 FAIL：把打印的首个差异字节偏移 + 行列位置贴出来，先解决，再跑任何流水线
#   （deploy.py 自己不改退出码，判据看这行输出里的 PASS / FAIL 与内部 RC=0 / RC=2）

# 3) 单张 + 连发 5 次（§6.B-1）
python3 pc/cam_shot_probe.py --host 10.13.148.1 --count 5
#   期望：成功 5/5，每张 nbytes=196608，md5 列非空

# 4) 连发 10 次不崩（§6.B-3，相机只有 ~128 MB）
python3 pc/cam_shot_probe.py --host 10.13.148.1 --count 10 --save
./deploy.py shot-logs | grep '\[stat\]'   # mem 列不应持续下降（连发 10 张看趋势）

# 5) 记下**实测** t_cap_us（min/avg/max），回填 §8
python3 pc/cam_shot_probe.py --host 10.13.148.1 --count 10

# 6) 收工
./deploy.py shot-stop
```

---

## 5. 主机侧对测（**不需要相机**，现在就能跑）

```bash
# ① 59 条判据：假相机全路径 + 内核 + 裁剪硬判据（本机实测约 7 s 全过）
python3 apps/pc/test_cam_proto.py -v

# ② 用**真文件** cam_shot.py 起服务，但 grab 换成解析式假图（不需要 maix、不需要相机）
python3 apps/maixcam/cam_shot.py --dry-run --port 8660
python3 apps/pc/cam_shot_probe.py --host 127.0.0.1 --count 3

# ③ 假相机造各种故障（板子端验客户端/验重试逻辑用）
python3 apps/pc/fake_camera.py --pattern checker            # 正常
python3 apps/pc/fake_camera.py --status busy --status-count 1   # 第一张 busy，之后正常
python3 apps/pc/fake_camera.py --status timeout
python3 apps/pc/fake_camera.py --corrupt md5                # md5 错
python3 apps/pc/fake_camera.py --corrupt short              # 少发 1 字节
python3 apps/pc/fake_camera.py --corrupt truncate           # JSON 截断
python3 apps/pc/fake_camera.py --corrupt drop               # 中途断开
python3 apps/pc/fake_camera.py --delay-ms 9000              # 触发客户端自身超时
```

`test_cam_proto.py` 的 59 条判据分五段：

| 段 | 内容 |
|---|---|
| A | 假相机正常回图：nbytes / md5 / `native` / 载荷逐字节 == 解析式子块 |
| B | 坏包注入 4 种 + `busy`/`timeout`，**四种坏包分类互不相同** |
| C | `cam_core` 内核：窗口计算、字段集合、busy 不排队、timeout、bad_req、同连接连发 |
| D | **裁剪硬判据**：2560×1440 已知图，center 256×256 输出 == 全幅中心子块（逐字节） |
| E | `cam_shot.py` 本体 dry-run 链路 + `--check-crop` 自身「能判真伪」 |

---

## 6. 协议字段表（与 §3 **完全一致**）

传输：TCP，端口 **8660**，一条连接可连发多张（**一问一答，不流水线**，同时只允许一个未完成请求）。
请求行上限 4096 字节；单次 `shot` 默认总超时 8 s。

### 6.1 请求（板子 → 相机）：一行 JSON，以 `\n` 结尾

```json
{"v":1,"cmd":"shot","seq":123,"w":256,"h":256,"crop":"center","exp_us":400,"gain":null,"timeout_ms":8000}
```

| 字段 | 类型 | 必填 | 含义 | 本服务不合法时的处理 |
|---|---|---|---|---|
| `v` | int | 是 | 协议版本，固定 `1` | `bad_req` |
| `cmd` | str | 是 | `shot` | 其它值 → `unknown_cmd` |
| `seq` | int | 是 | 单调递增；回包**原样带回** | 非整数 → `bad_req`（回包 `seq:null`） |
| `w`/`h` | int | 是 | **裁剪尺寸（原生像素）** | 非正整数 / `w*h` 超限 → `bad_req` |
| `crop` | `"center"` 或 `{"x":..,"y":..}` | 否 | `center` = 正中间；对象 = 原生坐标左上角。缺省按 `center` | 越界 → `bad_req`（**不偷偷夹到边界**） |
| `exp_us` | int / null | 否 | 曝光（µs）；`null` = 不干预 | 非整数 → `bad_req` |
| `gain` | int / null | 否 | 增益；`null` = 不干预 | 非整数 → `bad_req` |
| `timeout_ms` | int | 否 | 相机侧自身的采集超时，缺省 `8000` | `<=0`/非整数 → 按缺省；上限 120000 |

裁剪窗口取整（与旧 app 一致，**纯整数运算、绝无缩放**）：

```
crop:"center" →  x = (native_w - w) // 2 ,  y = (native_h - h) // 2
2560×1440 取 256×256  ⇒  x=1152, y=592
```

### 6.2 回包（相机 → 板子）：先一行 JSON，再紧跟**恰好 `nbytes`** 字节

```json
{"v":1,"ok":true,"seq":123,"w":256,"h":256,"fmt":"RGB888","nbytes":196608,
 "md5":"<32位小写hex>","native":"2560x1440","exp_us":400,"gain":312,
 "t_cap_us":180000,"t_send_us":9000}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `v` | int | 固定 `1` |
| `ok` | bool | `true` 时后面紧跟载荷；`false` 时**没有载荷** |
| `seq` | int | 原样带回请求里的 `seq` |
| `w`/`h` | int | 本次实际裁剪尺寸（= 请求的 `w`/`h`） |
| `fmt` | str | 固定 `"RGB888"`（每像素 3 字节、行优先、左上原点、**无行尾 padding**） |
| `nbytes` | int | 载荷字节数，恒等于 `w*h*3`（256×256 → 196,608） |
| `md5` | str | **载荷**的 md5（32 位小写 hex，**不含 JSON 行**） |
| `native` | str | 原生档位，如 `"2560x1440"`（取自本张实测帧尺寸） |
| `exp_us` | int / null | **实际生效**的曝光（读回值，不是请求值的回显） |
| `gain` | int / null | **实际生效**的增益（读回值） |
| `t_cap_us` | int | 本张采集（`grab`）耗时，µs |
| `t_send_us` | int | **上一张**载荷的 socket 写耗时，µs；服务起来第一张为 `0` |

> ### ★ `t_send_us` 是「上一张」的（§3.5 ①）
> JSON 头必须先发、载荷紧跟其后 ⇒ **本张的发送耗时在写头那一刻还不存在**。
> 这里不编造字段、也不填假值：它报的是上一张实测值。
> **板子侧要算端到端时延，请用 `t_cap_us` + 自己掐表，不要用 `t_send_us` 当本张。**

> 字段顺序与 §3.2 表格一致；但客户端**必须按 key 解析**，不要依赖顺序。
> 本服务 `ok:true` 回包**恰好这 13 个字段**（`test_cam_proto.py` 的 C2 断言字段集合相等，多一个少一个都算失败）。

### 6.3 失败回包

```json
{"v":1,"ok":false,"seq":123,"err":"busy"}
```

| `err` | 含义 | 板子侧动作（§3.5 ②） |
|---|---|---|
| `busy` | 上一张还没采完（**不排队**，立刻回） | 等 200 ms 重试，≤3 次 |
| `timeout` | 相机侧采集超时（含 `camera.read()` 抛异常） | 重试一次 |
| `bad_req` | 请求字段非法（`w/h`/`crop` 越界/`v` 不是 1/JSON 不合法…） | **不重试**（这是程序 bug） |
| `unknown_cmd` | `cmd` 不认识 | **不重试** |
| `internal` | **必复现**的内部错：裁剪尺寸 ≠ `w*h`、`to_bytes` 长度 ≠ `w*h*3` | **不重试**，打日志查原因 |

> 「每次都会失败」的错不许伪装成 `timeout`——否则板子白重试、真问题被噪声掩盖。
> 所以 `internal` 单独一类：看到它就说明**协议/格式层面**有硬伤，先跑 `--check-crop`。

### 6.4 板子侧必须做的五条校验（§3.3，缺一不可）

1. JSON 可解析，且 `v==1`、`seq` 与请求一致
2. `nbytes == w*h*3`，且 `w`/`h` 与请求一致
3. **读满恰好 `nbytes` 字节**（`recv_until` 语义，处理短读）
4. **md5 校验**通过（不符 ⇒ 丢弃 + 同 `seq` 重发一次；仍失败则报错）
5. 单次 `shot` 总超时（默认 8 s）

任何一条不满足 ⇒ **不许把半张图喂给算法**，否则算出来的错图无法归因。

> 参考实现：`apps/pc/test_cam_proto.py` 的 `client_shot()`（这五条逐条对应），
> `apps/pc/cam_shot_probe.py` 直接复用它；板子端 `cam_client.py` 可对照它写。

---

## 7. 日志怎么读

```
[serve] listen on 0.0.0.0:8660 native=2560x1440
[cam] open 2560x1440 fmt=RGB888 812 ms mem_avail=78.4 MB
[serve] 就绪：TCP 0.0.0.0:8660  原生 2560x1440  settle=4  mem_avail=78.1 MB
[shot] seq=7 w=256 h=256 crop=1152,592 bytes=196608 md5=9f2c1ab4e0d35f7c81b6e2a4d0c3f815 native=2560x1440 cap_us=184213 send_us=9124 total_us=193337 exp_us=400 gain=312
[req ] bad seq=8 err=bad_req (crop 窗口 (2500,0)+256x256 超出原生帧 2560x1440) '{"v":1,...}'
[stat] grabs=10 ok=9 busy=0 timeout=0 bad_req=0 unknown_cmd=0 internal=0 conn=10 drop=0 mem=78.2 MB
[serve] 收到信号 15 -> 优雅退出（关相机、释放 VI）
[serve] 退出：ok=10 busy=0 timeout=0 ...
```

| 前缀 | 含义 |
|---|---|
| `[serve]` | 生命周期：监听/就绪/收到信号/退出汇总 |
| `[cam]` | 相机：开门（档位/格式/耗时/可用内存）、AE/AWB 切换、关闭 |
| `[shot]` | **每张一行**：`seq` / `w` `h` / `crop`(x,y) / `bytes` / `md5` / `native` / `cap_us`(采集) / `send_us`(上一张发送) / `total_us`(收请求→回包写完) / `exp_us` / `gain` |
| `[req ]` | 被拒的请求（原因带窗口/尺寸细节） |
| `[stat]` | 每 `--stat-every` 张（默认 10）一行计数 + 可用内存 |
| `[check-crop]` | 自检的每一例 PASS/FAIL |

常用 grep：

```bash
grep '\[shot\]' cam_shot.log | tail -5           # 最近 5 张
grep -c 'err=busy' cam_shot.log                   # busy 次数
grep '\[stat\]' cam_shot.log | tail -3            # 内存有没有往下掉
grep -E 'FAIL|internal|Traceback' cam_shot.log    # 只看问题
```

> `[stat]` 里的 `ok` 会比 `grabs` 慢一拍：`ok` 是**回包写完**之后才 +1，
> 而这一行在主线程 `grab` 结束时就打了。看单张是否成功请以 `[shot]` 行为准。

---

## 8. 时间预算：**已实测**（2026-11-03 19:3x，PC 直连 NCM）

| 阶段 | 实测值 | 性质 |
|---|---|---|
| **一张 2K 原生 → 256² center → 传输（端到端）** | **358~373 ms（avg 364 ms，10/10）** | ✅ 本服务实测（含 settle≈130 ms） |
| `camera.Camera(2560x1440,RGB888)` 打开 | **2236~2836 ms**（三次：2236 / 2369 / 2836） | ✅ 实测；**只在服务启动时付一次** |
| 全幅 `to_bytes`（10.5 MB，仅 `--check-crop` 用） | **167~349 ms** | ✅ 实测 |
| 全幅取帧 + crop 三例（check-crop 内） | 21.15 / 6.89 / 1.26 ms（256²/512²/320×180） | ✅ 实测 |
| `settle` 帧开销 | 约 130 ms（`settle=4`，2K 30 fps） | 推算，与总量自洽 |

- 协议文档里原先写的「0.1~0.5 s」**偏乐观，已用实测取代**
- 真实是 **364 ms** （其中 settle 约占 130 ms）
- 要更快可把 `--settle` 降到 1~2 ⇒ 但 AEC/AWB 收敛会变差 ⇒ 自己权衡

### 8.1 内存实测（这条比时间更关键）
| 场景 | MemFree | MemAvailable |
|---|---:|---:|
| 刚开机（launcher 在跑） | **6~13 MB** | 54~70 MB |
| 停掉 launcher 后 | **41~44 MB** | 66~84 MB |
| 连取 10 张期间/之后 | **41 MB 恒定** | **66 MB 恒定** |

- **10 张前后内存完全不变** ⇒ 没有泄漏
- **但 **`MemFree` 只有 41 MB 时，**launcher（**82 MB anon RSS）必须停掉 ⇒ 否则 OOM

### 8.2 ★ 必须在起服务前停 launcher（否则 UI 被 OOM 杀掉）
实测 `dmesg`：
```
[267.49] udhcpc invoked oom-killer ... Killed process 297 (launcher) anon-rss:82612kB
[350.40] cut    invoked oom-killer ... Killed process 1655 (launcher) anon-rss:82040kB
```
- 现象：屏幕 `run app failed: code:-1` ⇒ 看起来"设备卡死"
- 实际：**OOM 杀了 launcher（UI），SSH 与内核都活着**
- **修法：起服务前 ** `kill -TERM` 掉 `launcher` 与 `launcher\_daemon` （无 GUI，headless 运行）
- 并先 `sync; echo 3 > /proc/sys/vm/drop\_caches` ⇒ `MemFree` 13 42 MB

### 8.3 曝光参数：**有作用但方向与直觉相反，未定性**
- `exp\_us=100` ⇒ `mean`=91.27 、`std`=38.21
- `exp\_us=20000` ⇒ `mean`=52.77 、`std`=17.83
- 长"曝光"反而更暗更平 ⇒ **不能按"值越大越亮"使用**
- 可能原因：参数语义不是时间、或与 AEC 互相覆盖 ⇒ **要定性得单独做一轮实验**
- 当前结论：**不要依赖 ** `exp\_us` 做定量控制

## 9. 已知限制与坑

1. **`--check-crop` 必须 PASS 才能跑流水线**。主机侧的逐字节判据只证明了「窗口计算 + 协议链路」无重采样，
   **不能**证明 MaixPy 的 `img.crop()` 内部没有插值 —— 那是真机自检要回答的问题。
   每张还有一道运行时守卫：`img.crop()` 结果尺寸 ≠ `w×h` ⇒ 直接回 `internal`。
2. **128 MB 内存**：2K 原生帧一帧 11 MB；服务每张后主动 `gc`（`--gc-every 1`），
   不要随便把它关掉，也不要把 `--settle` 调得很大。
3. **相机只允许主线程读**：`grab` 只在 `cam_core.ShotServer.step()` 里被调用，
   而主循环在主线程；socket 收发包全在后台线程。改动这个结构 = 回到旧坑。
4. **绝不能 `kill -9` / 不要 `pkill -f`**：应用握着 MMF（VI/VPSS）。本服务接管
   `SIGTERM`/`SIGINT` → 先关相机再退出；`deploy.py shot-stop` 只对 pid 文件里的 pid 发 `TERM`。
   硬杀会让后续 `camera.Camera()` 一直失败，严重时整机要断电/RST。
5. **2K RGB888 开不出来时默认直接失败**（`RuntimeError`）。要退 `NV21 + to_format` 必须显式
   `--allow-yuv-fallback`，而且日志会打红字警告：**那条路的像素值不等于原生 RGB，不许用来采数据集**。
6. **`t_send_us` 首张为 0**，且永远是「上一张」；别拿它算本张时延（§6.2）。
7. **busy 不排队**：同时只允许一个未完成请求（128 MB 内存不允许积压）。
8. **`--dry-run` 的图是解析式的假图**，`t_cap_us` 只反映 Python 生成耗时，**不代表真机**。
9. **`--check-crop` 模式内存峰值约 11 MB 以上**（全幅 `to_bytes` 一份 11 MB，外加 MMF 侧那一帧），跑完会 `gc`。
10. **与旧 app 抢相机**：先 `./deploy.py stop`（停 RTSP 那个），再 `./deploy.py shot`。
11. `cam_shot_probe.py --save` 存的是**原始 RGB888**（`.rgb`），不是 PNG；默认写 `runs/`（已被 `.gitignore` 忽略）。

---

## 10. 验证状态（诚实账本）

| 项 | 状态 | 证据 |
|---|---|---|
| 主机侧 59 条判据（A/B/C/D/E） | ✅ **本机实测** | `python3 apps/pc/test_cam_proto.py` → 59/59 通过（本机约 7 s） |
| 裁剪硬判据：center 256×256 == 全幅中心子块（逐字节） | ✅ **本机实测** | D 段：2560×1440 已知图，首个差异字节 `None` |
| 坏包 4 种分别识别 | ✅ **本机实测** | `md5_mismatch` / `short_read` / `json_truncated` / `mid_stream_close` |
| `cam_shot.py --dry-run` 走完整 TCP | ✅ **本机实测** | E 段：载荷逐字节 == 解析式子块 |
| `--check-crop` 自检**能判真伪** | ✅ **本机实测** | E3：正确实现 → PASS(0)；偷偷重采样 → FAIL(2) 并给出偏移 |
| 有照相机的 `camera.Camera(2560,1440,RGB888)` | ⏳ **待设备** | — |
| 真机 `--check-crop` | ⏳ **待设备** | 未实测 |
| 真机 `shot 256×256 center` ×5（nbytes/md5） | ⏳ **待设备** | 未实测 |
| 真机连发 10 次不崩 | ⏳ **待设备** | 未实测 |
| 真机 `t_cap_us` | ⏳ **待设备** | 未实测，**§8 的 0.1~0.5 s 仍是推测** |
| 真机 busy / timeout 行为 | ⏳ **待设备** | 未实测 |

---

## 11. 排障

| 现象 | 先看什么 |
|---|---|
| `deploy.py shot` 后端口没起 | `./deploy.py shot-logs`：多半是 `camera open failed`（相机被占了）或 `2K RGB888 打不开` |
| 日志 `camera open failed` | 旧 app/launcher 还占着相机：`./deploy.py stop`；还不行就按 RST 再试 |
| 板子端总收到 `busy` | 上一张还没采完就来了下一张：板子侧按 §3.4 等 200 ms、≤3 次 |
| 板子端收到 `internal` | 裁剪尺寸或字节数不对：立刻跑 `./deploy.py shot-check`，别重试 |
| `--check-crop` FAIL | 把「偏移 N 字节（第 r 行第 c 列）」贴出来；先解决再跑流水线 |
| `nbytes` 对但图是花的 | 板子侧字节序/行序解析错；用 `--pattern checker` + 逐字节比对定位 |
| `md5` 一直不符 | 先确认板子算的是**载荷**的 md5（不含 JSON 行），再确认读满 `nbytes` |

---

## 12. 手工要一张图（不写客户端也能试）

```bash
# 在相机自己身上（有 python3 即可）
python3 - <<'EOF'
import socket, json, hashlib
req = {"v":1,"cmd":"shot","seq":1,"w":256,"h":256,"crop":"center",
       "exp_us":None,"gain":None,"timeout_ms":8000}
s = socket.create_connection(("127.0.0.1", 8660), timeout=10)
s.sendall((json.dumps(req)+"\n").encode())
buf = b""
while b"\n" not in buf: buf += s.recv(4096)
head, rest = buf.split(b"\n", 1)
hdr = json.loads(head); n = hdr["nbytes"]
while len(rest) < n: rest += s.recv(1<<20)
print(hdr)
print("md5 ok:", hashlib.md5(rest[:n]).hexdigest() == hdr["md5"], "n:", len(rest[:n]))
open("/tmp/shot.rgb","wb").write(rest[:n])
EOF
```
