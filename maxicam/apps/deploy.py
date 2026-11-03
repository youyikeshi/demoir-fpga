#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把设备端应用部署到 MaixCAM Pro 并控制它
======================================

用法
    ./deploy.py start            # 上传并启动（默认动作）
    ./deploy.py stop             # 停止
    ./deploy.py logs             # 看日志
    ./deploy.py status           # 看进程/端口/设备IP
    ./deploy.py install          # 装成设备菜单里的应用（会改 /maixapp/apps/app.info，自动备份）

    # 精简采集服务 cam_shot.py（TCP 8660，§3 协议，不推流、不显示）
    ./deploy.py shot             # 上传并启动（cam_shot.py + cam_core.py）
    ./deploy.py shot-check       # 上机第一件事：--check-crop 裁剪正确性自检
    ./deploy.py shot-status      # 端口 / pid / 最近日志
    ./deploy.py shot-logs        # 看日志
    ./deploy.py shot-stop        # 优雅停止（发 SIGTERM，应用自己关相机）

依赖 paramiko（已在工作区 venv 里）
"""

import argparse
import io
import os
import posixpath
import re
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(WS, "env", "maixpy-completion", "lib", "python3.10", "site-packages"))

import paramiko  # noqa: E402

DEV_HOST = os.environ.get("MAIXCAM_HOST", "10.13.148.1")
DEV_USER = "root"
DEV_PASS = "root"

REMOTE_DIR = "/root/maixcam_streamer"
APP_ID = "maixcam_streamer"
APP_DIR = "/maixapp/apps/" + APP_ID
LOG = posixpath.join(REMOTE_DIR, "app.log")

LOCAL_MAIN = os.path.join(HERE, "maixcam", "main.py")
LOCAL_YAML = os.path.join(HERE, "maixcam", "app.yaml")

# ---- 精简采集服务 cam_shot（§3 协议，TCP 8660）----
SHOT_PORT = 8660
SHOT_DIR = "/root/maxicam_shot"
SHOT_LOG = posixpath.join(SHOT_DIR, "cam_shot.log")
SHOT_PID = posixpath.join(SHOT_DIR, "cam_shot.pid")
LOCAL_SHOT = os.path.join(HERE, "maixcam", "cam_shot.py")
LOCAL_CORE = os.path.join(HERE, "maixcam", "cam_core.py")
LOCAL_NOTIFY = os.path.join(HERE, "maixcam", "board_notify.py")

# ---- 轻量信号服务 signal_service.py（不碰相机，只做 PC -> 板端握手）----
SIGNAL_DIR = "/root/maxicam_signal"
SIGNAL_PORT = 8555
SIGNAL_LOG = posixpath.join(SIGNAL_DIR, "signal_service.log")
SIGNAL_PID = posixpath.join(SIGNAL_DIR, "signal_service.pid")
LOCAL_SIGNAL = os.path.join(HERE, "maixcam", "signal_service.py")


def connect(host=DEV_HOST):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=DEV_USER, password=DEV_PASS, timeout=10,
              allow_agent=False, look_for_keys=False)
    return c


def run(cli, cmd, timeout=45, quiet=False):
    """执行远端命令，带超时兜底。
    注意：这台设备上 `ps aux` / `pgrep` / `pidof` / 遍历 /proc 都会卡死，
    所以一律不用它们，只读 /proc/net/tcp 这类单文件。"""
    _, out, err = cli.exec_command(cmd, timeout=timeout)
    try:
        out.channel.settimeout(timeout)
    except Exception:
        pass
    try:
        o = out.read().decode(errors="replace")
    except Exception:
        o = ""
    try:
        e = err.read().decode(errors="replace")
    except Exception:
        e = ""
    if not quiet and e.strip():
        sys.stderr.write(e)
    return o


def upload(cli, local, remote):
    sftp = cli.open_sftp()
    d = posixpath.dirname(remote)
    try:
        sftp.mkdir(d)
    except Exception:
        pass
    sftp.put(local, remote)
    sftp.close()


def listening_ports(cli):
    """读 /proc/net/tcp 拿 LISTEN(0A) 端口，比 ps/grep 可靠"""
    out = run(cli, "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | awk 'NR>1 && $4==\"0A\"{print $2}'")
    ports = set()
    for line in out.split():
        try:
            ports.add(int(line.split(":")[1], 16))
        except Exception:
            pass
    return sorted(ports)


def do_start(cli, host):
    print(">> 上传应用到 %s" % REMOTE_DIR)
    upload(cli, LOCAL_MAIN, posixpath.join(REMOTE_DIR, "main.py"))
    upload(cli, LOCAL_YAML, posixpath.join(REMOTE_DIR, "app.yaml"))
    upload(cli, LOCAL_NOTIFY, posixpath.join(REMOTE_DIR, "board_notify.py"))

    print(">> 停掉旧实例（先优雅退出，见 graceful_stop 的说明）")
    graceful_stop(cli, host)
    run(cli, "rm -f %s" % LOG, quiet=True, timeout=15)

    # 不要用 -u：MMF 驱动出错时会疯狂刷 stderr，无缓冲会变成大量 SD 卡写入。
    # 应用自己在 log() 里做了 flush()，所以我们的日志照样实时。
    print(">> 启动（后台，日志 %s）" % LOG)
    run(cli, "cd %s && nohup python3 main.py > %s 2>&1 & echo $! > app.pid"
        % (REMOTE_DIR, LOG), timeout=20)
    time.sleep(4)

    print(">> 日志:")
    print(run(cli, "grep -aE '\\[streamer\\]|Traceback' %s | tail -20" % LOG).rstrip()
          or "(日志还是空的，看下面端口判断)")

    listening = listening_ports(cli)
    up = 8555 in listening
    print("\n>> 应用状态: %s" % ("运行中（监听 8555）" if up else "未运行（看上面日志）"))
    print(">> 监听端口: %s" % " ".join(str(p) for p in listening))
    print(">> 推流状态: %s" % ("推流中（监听 8554）" if 8554 in listening else "未推流"))
    print("\n>> 设备 IP:")
    print(run(cli, "ip -4 -br addr show | grep -v ' lo '").rstrip())
    print("\n提示：设备屏幕应已显示「MaixCAM 视频推流」界面。")
    print("      在 PC 上跑:            ./pc/run.sh --host <上面的设备IP>")
    print("      先让设备开始推流:      ./pc/send_cmd.py start --seconds 6")


def graceful_stop(cli, host, wait=8):
    """
    优雅停止设备端应用。

    ⚠️ 为什么不能直接 pkill：
    应用正握着 MMF（摄像头 VI / VPSS / 硬件编码器）资源。硬杀（SIGKILL /
    不经清理的进程终止）会把这些资源留在占用状态，之后 camera.Camera() 会
    一直失败，严重时整个 MMF 驱动卡死 —— 表现为板子 ping 得通、端口在听，
    但 sshd 连 banner 都发不出来，只能给设备断电/按 RST 才能恢复。

    正确做法：
      1) 先通过控制通道发 {"cmd":"quit"}，应用自己 stop_stream() 释放资源再退出
      2) 超时未退才发 SIGTERM —— 应用已接管 SIGTERM，同样是优雅退出
    """
    try:
        s = socket.create_connection((host, 8555), timeout=4)
        s.sendall(b'{"t":"cmd","cmd":"quit"}\n')
        s.close()
        print(">> 已发送 quit，等待应用释放资源并退出…")
    except Exception:
        print(">> 控制通道没响应（应用可能本来就没在跑）")

    deadline = time.time() + wait
    while time.time() < deadline:
        if 8555 not in listening_ports(cli):
            print(">> 应用已优雅退出")
            return True
        time.sleep(0.8)

    print(">> 超时，发 SIGTERM（应用接管了它，仍是优雅退出）")
    run(cli, "pkill -TERM -f 'main.py'", quiet=True, timeout=15)
    time.sleep(2.5)
    ok = 8555 not in listening_ports(cli)
    print(">> %s" % ("已退出" if ok else "仍未退出，建议按 RST 重启设备"))
    return ok


def do_stop(cli, host):
    graceful_stop(cli, host)
    print("已停止设备端应用。")


# --------------------------------------------------------------------------- #
# 精简采集服务（cam_shot.py）
#
# 与 main.py 那套的区别：没有控制通道、没有 RTSP/显示，所以
#   · 停止不用发 quit，直接 SIGTERM（应用自己接管，先关相机再退）
#   · 绝不用 pkill -f（会误伤同名字符串；这里只杀 pid 文件里的那个 pid）
# --------------------------------------------------------------------------- #
def do_shot_start(cli, host, settle=4, native="2560x1440",
                  board_host=None, board_port=None):
    print(">> 上传 cam_shot.py + cam_core.py + board_notify.py 到 %s" % SHOT_DIR)
    upload(cli, LOCAL_SHOT, posixpath.join(SHOT_DIR, "cam_shot.py"))
    upload(cli, LOCAL_CORE, posixpath.join(SHOT_DIR, "cam_core.py"))
    upload(cli, LOCAL_NOTIFY, posixpath.join(SHOT_DIR, "board_notify.py"))

    print(">> 停掉旧实例（SIGTERM -> 应用自己关相机）")
    shot_stop(cli, host)
    run(cli, "rm -f %s" % SHOT_LOG, quiet=True, timeout=15)

    extra = ""
    if board_host:
        extra += " --board-host %s" % board_host
    if board_port:
        extra += " --board-port %d" % board_port

    # 不用 python -u：MMF 出错时刷屏，无缓冲会把 SD 卡写爆（应用自己 flush 日志）
    print(">> 启动（后台，日志 %s）" % SHOT_LOG)
    run(cli, "cd %s && nohup python3 cam_shot.py --port %d --native %s --settle %d%s "
             "> %s 2>&1 & echo $! > %s"
        % (SHOT_DIR, SHOT_PORT, native, settle, extra, SHOT_LOG, SHOT_PID), timeout=20)
    time.sleep(5)

    print(">> 启动日志:")
    print(run(cli, "grep -aE '\\[serve\\]|\\[cam\\]|Traceback|Error' %s | tail -15"
              % SHOT_LOG).rstrip() or "(日志还是空的，看下面端口判断)")

    ports = listening_ports(cli)
    up = SHOT_PORT in ports
    print("\n>> 采集服务: %s" % ("运行中（监听 %d）" % SHOT_PORT if up else "未运行（看上面日志）"))
    if not up:
        print("   常见原因：1) 相机被别的进程占着（先 ./deploy.py shot-stop 再试）")
        print("             2) 2K RGB888 打不开 -> 看日志里 camera 报错")
        return
    print(">> 板子/本机都可以这样要一张图：")
    print("     printf '{\"v\":1,\"cmd\":\"shot\",\"seq\":1,\"w\":256,\"h\":256,"
          "\"crop\":\"center\",\"exp_us\":null,\"gain\":null,\"timeout_ms\":8000}\\n' "
          "| nc %s %d > /tmp/shot.bin" % (host, SHOT_PORT))
    print("   或：python3 apps/pc/test_cam_proto.py  （主机侧自测，不需要相机）")


def shot_stop(cli, host, wait=12):
    """优雅停止：只对 pid 文件里的 pid 发 SIGTERM，等端口消失。绝不 kill -9。"""
    if SHOT_PORT not in listening_ports(cli):
        return True
    pid = run(cli, "cat %s 2>/dev/null" % SHOT_PID, quiet=True, timeout=10).strip()
    if not pid.isdigit():
        print("!! 端口 %d 在听，但 %s 里没有 pid —— 不敢盲杀（这台设备硬杀会锁死 MMF）"
              % (SHOT_PORT, SHOT_PID))
        print("   请先确认是什么进程占着，或给设备断电重启。")
        return False
    print(">> SIGTERM -> pid %s（应用会先关相机再退出）" % pid)
    run(cli, "kill -TERM %s" % pid, quiet=True, timeout=15)
    deadline = time.time() + wait
    while time.time() < deadline:
        if SHOT_PORT not in listening_ports(cli):
            print(">> 已退出（相机已释放）")
            return True
        time.sleep(0.8)
    print("!! %d 秒还没退出：看 ./deploy.py shot-logs，必要时按 RST" % wait)
    return False


def do_shot_stop(cli, host):
    ok = shot_stop(cli, host)
    print("已停止采集服务。" if ok else "停止失败，见上面提示。")


def do_shot_logs(cli):
    print(run(cli, "tail -40 %s" % SHOT_LOG))


def do_shot_status(cli):
    ports = listening_ports(cli)
    pid = run(cli, "cat %s 2>/dev/null" % SHOT_PID, quiet=True, timeout=10).strip()
    print("== 监听端口 == %s" % (" ".join(str(p) for p in ports) or "(无)"))
    print("== 采集服务 == %s" % ("运行中 (8660)" if SHOT_PORT in ports else "未运行"))
    print("== pid      == %s" % (pid or "(无 pid 文件)"))
    print("== 最近日志 ==")
    print(run(cli, "tail -8 %s 2>/dev/null" % SHOT_LOG).rstrip() or "(无)")


def do_shot_check(cli, host, settle=8):
    """上机第一件事：证明 MaixPy 的 img.crop() 没有偷偷 resize。失败就别跑流水线。"""
    if SHOT_PORT in listening_ports(cli):
        print(">> 服务正在跑，先停掉（自检要独占相机）")
        shot_stop(cli, host)
    print(">> 跑 cam_shot.py --check-crop（会取一张 2K 全幅，约 11 MB，几秒钟）")
    out = run(cli, "cd %s && python3 cam_shot.py --check-crop --settle %d; echo \"RC=$?\""
              % (SHOT_DIR, settle), timeout=180)
    print(out.rstrip())
    ok = "RC=0" in out
    print("\n>> 自检结果: %s" % ("PASS（crop 是像素子集，可以继续）" if ok
                                else "FAIL / 没跑起来 —— 先解决，再跑任何流水线"))
    return ok


# --------------------------------------------------------------------------- #
# 轻量信号服务（PC 8555 -> 板端演示握手 9999；不开相机）
# --------------------------------------------------------------------------- #
def do_signal_start(cli, host, board_host=None, board_port=None):
    print(">> 上传 signal_service.py + board_notify.py 到 %s" % SIGNAL_DIR)
    run(cli, "mkdir -p %s" % SIGNAL_DIR, quiet=True, timeout=15)
    upload(cli, LOCAL_SIGNAL, posixpath.join(SIGNAL_DIR, "signal_service.py"))
    upload(cli, LOCAL_NOTIFY, posixpath.join(SIGNAL_DIR, "board_notify.py"))

    print(">> 停掉旧实例")
    signal_stop(cli, host)
    run(cli, "rm -f %s" % SIGNAL_LOG, quiet=True, timeout=15)

    extra = ""
    if board_host:
        extra += " --board-host %s" % board_host
    if board_port:
        extra += " --board-port %d" % board_port
    print(">> 启动（后台，日志 %s）" % SIGNAL_LOG)
    run(cli, "cd %s && nohup python3 signal_service.py%s > %s 2>&1 & echo $! > %s"
        % (SIGNAL_DIR, extra, SIGNAL_LOG, SIGNAL_PID), timeout=20)
    time.sleep(2)

    print(">> 启动日志:")
    print(run(cli, "tail -8 %s" % SIGNAL_LOG).rstrip() or "(空)")
    ports = listening_ports(cli)
    print("\n>> 信号服务: %s" %
          ("运行中（监听 %d）" % SIGNAL_PORT if SIGNAL_PORT in ports else "未运行"))
    if SIGNAL_PORT in ports:
        print(">> PC 端这样测：")
        print("     python3 pc/send_show.py --host %s --idx 1" % host)


def signal_stop(cli, host, wait=6):
    """只按 pid 文件杀（这台设备 ps/killall 会卡 SSH；而且不碰相机）。"""
    if SIGNAL_PORT not in listening_ports(cli):
        print(">> 端口 %d 没在听，无需停止" % SIGNAL_PORT)
        return True
    pid = run(cli, "cat %s 2>/dev/null" % SIGNAL_PID, quiet=True, timeout=10).strip()
    if not pid.isdigit():
        print("!! %d 在听但没有 pid 文件，不盲杀" % SIGNAL_PORT)
        return False
    run(cli, "kill -TERM %s" % pid, quiet=True, timeout=10)
    deadline = time.time() + wait
    while time.time() < deadline:
        if SIGNAL_PORT not in listening_ports(cli):
            print(">> 已退出")
            return True
        time.sleep(0.5)
    print("!! %d 秒还没退出，看 signal-logs" % wait)
    return False


def do_signal_stop(cli, host):
    print("已停止信号服务。" if signal_stop(cli, host) else "停止失败，见上面提示。")


def do_signal_logs(cli):
    print(run(cli, "tail -40 %s" % SIGNAL_LOG))


def do_signal_status(cli):
    ports = listening_ports(cli)
    pid = run(cli, "cat %s 2>/dev/null" % SIGNAL_PID, quiet=True, timeout=10).strip()
    print("== 监听端口 == %s" % (" ".join(str(p) for p in ports) or "(无)"))
    print("== 信号服务 == %s" % ("运行中 (8555)" if SIGNAL_PORT in ports else "未运行"))
    print("== pid      == %s" % (pid or "(无 pid 文件)"))
    print("== 最近日志 ==")
    print(run(cli, "tail -8 %s 2>/dev/null" % SIGNAL_LOG).rstrip() or "(无)")


def do_logs(cli):
    print(run(cli, "tail -40 %s" % LOG))


def do_status(cli):
    # 注意：这台设备上 `ps aux` / `netstat` 会卡住，所以只读 /proc
    ports = listening_ports(cli)
    print("== 监听端口 == %s" % (" ".join(str(p) for p in ports) or "(无)"))
    print("== 应用     == %s" % ("运行中 (8555)" if 8555 in ports else "未运行"))
    print("== 推流     == %s" % ("推流中 (8554)" if 8554 in ports else "未推流"))
    print("== 设备 IP  ==")
    print(run(cli, "ip -4 -br addr show | grep -v ' lo '").rstrip())


def do_install(cli):
    """装成设备菜单里的应用（可选）"""
    print(">> 上传到 %s" % APP_DIR)
    upload(cli, LOCAL_MAIN, posixpath.join(APP_DIR, "main.py"))
    upload(cli, LOCAL_YAML, posixpath.join(APP_DIR, "app.yaml"))
    upload(cli, LOCAL_NOTIFY, posixpath.join(APP_DIR, "board_notify.py"))

    print(">> 备份 app.info 并登记应用")
    print(run(cli, "cp -n /maixapp/apps/app.info /maixapp/apps/app.info.bak-$(date +%s) || true"))
    entry = (
        "\n[%s]\n"
        "name=MaixCAM Streamer\n"
        "name[zh]=视频推流\n"
        "icon=/maixapp/apps/settings/assets/usb.png\n"
        "version=1.0.0\n"
        "exec=main.py\n"
        "author=local\n"
        "desc=RTSP streamer with touch UI\n"
        "desc[zh]=触屏选择分辨率/编码/码率并RTSP推流；USER键短按截图、长按退出。\n"
    ) % APP_ID
    # 先删掉旧条目再追加，避免重复
    run(cli, "python3 - <<'EOF'\n"
             "p='/maixapp/apps/app.info'\n"
             "s=open(p,encoding='utf-8',errors='replace').read()\n"
             "import re\n"
             "s=re.sub(r'\\n\\[%s\\][\\s\\S]*?(?=\\n\\[|$)', '', s)\n"
             "s=s.rstrip()+'\\n'\n"
             "open(p,'w',encoding='utf-8').write(s)\n"
             "EOF" % APP_ID)

    sftp = cli.open_sftp()
    with sftp.open("/maixapp/apps/app.info", "a") as f:
        f.write(entry)
    sftp.close()

    # 回读确认登记成功
    back = run(cli, "grep -A9 '^\\[%s\\]' /maixapp/apps/app.info" % APP_ID, timeout=20).strip()
    if APP_ID in back:
        print(">> 已登记到 app.info:")
        print(back)
    else:
        print("!! 登记貌似失败，请检查 /maixapp/apps/app.info")
        return

    print("")
    print("=" * 62)
    print(" 安装完成。接下来：")
    print("   1) 重启设备一次（launcher 只在启动时读 app.info）")
    print("      按板子上的 RST 键，或拔插一次 Type-C")
    print("   2) 在设备屏幕上点开「视频推流」应用")
    print("      —— 它会自动开始推流，屏幕显示实时预览 + RTSP 地址")
    print("   3) 电脑上跑： ./pc/run.sh --host <设备IP>")
    print("      （它会等最多 60 秒，你先点应用后跑电脑端也行）")
    print("")
    print(" 想卸载：删掉 /maixapp/apps/%s 并去掉 app.info 里的 [%s] 段"
          % (APP_ID, APP_ID))
    print(" app.info 备份在 /maixapp/apps/app.info.bak-*")
    print("=" * 62)


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)   # 避免卡住时输出全丢
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("action", nargs="?", default="start",
                    choices=["start", "stop", "logs", "status", "install",
                             "shot", "shot-check", "shot-status", "shot-logs", "shot-stop",
                             "signal", "signal-stop", "signal-status", "signal-logs"])
    ap.add_argument("--host", default=DEV_HOST)
    ap.add_argument("--settle", type=int, default=4, help="cam_shot: 每张前丢几帧")
    ap.add_argument("--native", default="2560x1440", help="cam_shot: 原生档位")
    ap.add_argument("--board-host", default=None,
                    help="cam_shot: 板子 IP（默认走 cam_shot 内建 10.13.148.100）")
    ap.add_argument("--board-port", type=int, default=None,
                    help="cam_shot: 板端信号端口（默认 8661）")
    a = ap.parse_args()

    cli = connect(a.host)
    try:
        if a.action == "start":
            do_start(cli, a.host)
        elif a.action == "stop":
            do_stop(cli, a.host)
        elif a.action == "logs":
            do_logs(cli)
        elif a.action == "status":
            do_status(cli)
        elif a.action == "install":
            do_install(cli)
        elif a.action == "shot":
            do_shot_start(cli, a.host, settle=a.settle, native=a.native,
                          board_host=a.board_host, board_port=a.board_port)
        elif a.action == "shot-check":
            do_shot_check(cli, a.host)
        elif a.action == "signal":
            do_signal_start(cli, a.host, board_host=a.board_host, board_port=a.board_port)
        elif a.action == "signal-stop":
            do_signal_stop(cli, a.host)
        elif a.action == "signal-status":
            do_signal_status(cli)
        elif a.action == "signal-logs":
            do_signal_logs(cli)
        elif a.action == "shot-status":
            do_shot_status(cli)
        elif a.action == "shot-logs":
            do_shot_logs(cli)
        elif a.action == "shot-stop":
            do_shot_stop(cli, a.host)
    finally:
        cli.close()


if __name__ == "__main__":
    main()
