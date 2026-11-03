#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""find_device.py —— 在同一个 WiFi/热点网段里找 MaixCAM（以及它的端口）。

为什么需要它
------------
MaixCAM 连上热点后拿的是 DHCP 地址（`192.168.43.x` / `172.20.10.x` / `192.168.x.x`），
手机热点页面不一定显示、换一次热点就变。与其猜，不如扫一遍：相机在线时
**8555（显示信号）/ 8660（采集）/ 22（SSH）** 至少有一个是开的。

用法
----
    python3 apps/pc/find_device.py                 # 自动取本机 WiFi 网段，扫 /24
    python3 apps/pc/find_device.py --subnet 192.168.43.0/24
    python3 apps/pc/find_device.py --ports 8555,8660,22 --timeout 0.25

输出示例
--------
    [usb ] 10.13.148.1     : 关闭（USB 没插/没枚举）
    [lan ] 扫描 192.168.43.0/24（254 台，端口 8555,8660,22）...
    [hit ] 192.168.43.12   : 8555 开
    建议：--host 192.168.43.12
"""

import argparse
import ipaddress
import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

USB_HOSTS = ("10.13.148.1", "10.13.149.1")   # MaixCAM 的 NCM / RNDIS 固定地址
DEF_PORTS = (8555, 8660, 22)


def local_ipv4():
    """返回 [(网卡名, ip, 前缀长度)]，跳过回环/容器网卡。"""
    out = []
    try:
        txt = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return out
    for line in txt.splitlines():
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+).*?\bdev (\S+)", line)
        if not m:
            continue
        ip, plen, dev = m.group(1), int(m.group(2)), m.group(3)
        if dev == "lo" or dev.startswith(("docker", "veth", "br-", "virbr")):
            continue
        out.append((dev, ip, plen))
    return out


def tcp_open(ip, port, timeout):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def scan(hosts, ports, timeout, workers=128):
    """返回 {ip: [开的端口...]}。"""
    def probe(ip):
        open_ports = [p for p in ports if tcp_open(str(ip), p, timeout)]
        return str(ip), open_ports

    hits = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for ip, open_ports in ex.map(probe, hosts):
            if open_ports:
                hits[ip] = open_ports
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser(description="找同网段的 MaixCAM（扫常见端口）")
    ap.add_argument("--subnet", default=None,
                    help="要扫的网段，如 192.168.43.0/24；不填=自动取本机非回环网卡的 /24")
    ap.add_argument("--ports", default=",".join(str(p) for p in DEF_PORTS))
    ap.add_argument("--timeout", type=float, default=0.35, help="单端口超时秒数")
    ap.add_argument("--max-hosts", type=int, default=1024, help="最多扫多少台（护着点）")
    args = ap.parse_args(argv)

    ports = [int(p) for p in args.ports.split(",") if p.strip()]

    # 1) 先看 USB 那两个固定地址（插着线时最快）
    for ip in USB_HOSTS:
        opens = [p for p in ports if tcp_open(ip, p, args.timeout)]
        print("[usb ] %-15s : %s" % (ip, ("%s 开" % ",".join(map(str, opens))) if opens else "关闭（USB 没插/没枚举）"))

    # 2) 再扫 WiFi 网段
    if args.subnet:
        nets = [ipaddress.ip_network(args.subnet, strict=False)]
        labels = ["--subnet"]
    else:
        nets, labels = [], []
        for dev, ip, plen in local_ipv4():
            # 前缀比 /24 大（比如 /21 校园网）时只扫自己那一段 /24，避免扫 2000 台
            net = ipaddress.ip_network("%s/%d" % (ip, max(24, plen)), strict=False)
            if net not in nets:
                nets.append(net)
                labels.append(dev)
        if not nets:
            print("没找到可用的非回环网卡；请用 --subnet 手动指定")
            return 2

    all_hits = {}
    for net, label in zip(nets, labels):
        hosts = [h for h in net.hosts()][:args.max_hosts]
        print("[lan ] %s 网段 %s（%d 台，端口 %s）..."
              % (label, net, len(hosts), ",".join(map(str, ports))))
        hits = scan(hosts, ports, args.timeout)
        all_hits.update(hits)
        for ip in sorted(hits, key=lambda s: tuple(int(x) for x in s.split("."))):
            print("[hit ] %-15s : %s 开" % (ip, ",".join(map(str, hits[ip]))))

    if not all_hits:
        print("\n没扫到。按顺序查：\n"
              "  1) 相机真的连上热点了吗（设置 -> WiFi 里看有没有 IP）\n"
              "  2) 手机热点是不是开了「客户端隔离」（关了再试）\n"
              "  3) 相机 app 起来了吗（8555/8660 是应用端口，没起应用就只剩 22）\n"
              "  4) 换 2.4G 热点、或手动指定 --subnet")
        return 1

    # 优先报带应用端口的（那才是相机在跑我们的服务）
    best = None
    for ip, opens in all_hits.items():
        if 8555 in opens or 8660 in opens:
            best = ip
            break
    best = best or sorted(all_hits)[0]
    print("\n建议：--host %s" % best)
    print("       ./pc/run.sh --host %s" % best)
    print("       MAIXCAM_HOST=%s ./deploy.py shot" % best)
    return 0


if __name__ == "__main__":
    sys.exit(main())
