#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""board_relay.py —— 电脑上的 TCP 转发：让 WiFi 上的相机够到只连电脑网口的板子。

为什么需要它
------------
演示握手协议要求 **相机主动连板子 9999**。但现实网络是：

    相机 --WiFi--> 电脑            电脑 --网口--> 板子 192.168.7.2:9999

相机和板子不在同一网段，相机根本路由不到 `192.168.7.2`。这个脚本在电脑上
监听 9999，把相机的连接原样转给板子；板子的 ACK 再原样转回去 —— 对相机
来说就是"板子在电脑的 WiFi 地址上"，协议一个字节都不用改。

    python3 apps/pc/board_relay.py --board-host 192.168.7.2 --board-port 9999

    相机侧：--board-host <电脑 WiFi IP> --board-port 9999

> 等价替代：把相机 USB 插到板子的 USB-A 口，相机填 `10.13.148.100:9999`，
> 就不需要这个转发了（等板端 USB host 镜像）。

注意
----
* 板端"只服务最新一个连接"：这里每条客户端连接对应一条上行连接，语义一致。
* 上行连不上时回一行 `ERR NOBOARD`（协议外的兜底），相机侧会记 `ok:false`，
  比干等 3 秒超时更容易排查。
"""

import argparse
import socket
import sys
import threading

DEF_LISTEN_PORT = 9999
DEF_BOARD_HOST = "192.168.7.2"
DEF_BOARD_PORT = 9999


def pump(src, dst, tag, log):
    try:
        while True:
            d = src.recv(4096)
            if not d:
                break
            dst.sendall(d)
    except Exception:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass


def serve(client, addr, args, log):
    up = None
    try:
        up = socket.create_connection((args.board_host, args.board_port),
                                      timeout=args.timeout)
        up.settimeout(None)
        log("[relay] %s:%d <-> 板子 %s:%d"
            % (addr[0], addr[1], args.board_host, args.board_port))
        threading.Thread(target=pump, args=(client, up, "c->b", log), daemon=True).start()
        pump(up, client, "b->c", log)
    except Exception as e:
        log("[relay] %s:%d 上行连不上 %s:%d：%r"
            % (addr[0], addr[1], args.board_host, args.board_port, e))
        try:
            client.sendall(b"ERR NOBOARD\n")
        except Exception:
            pass
    finally:
        for s in (client, up):
            if s is None:
                continue
            try:
                s.close()
            except Exception:
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="把相机的 9999 转发到板子（跨网段用）")
    ap.add_argument("--listen-host", default="0.0.0.0")
    ap.add_argument("--listen-port", type=int, default=DEF_LISTEN_PORT)
    ap.add_argument("--board-host", default=DEF_BOARD_HOST)
    ap.add_argument("--board-port", type=int, default=DEF_BOARD_PORT)
    ap.add_argument("--timeout", type=float, default=3.0, help="连板子的超时秒数")
    args = ap.parse_args(argv)

    log = lambda m: print(m, flush=True)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((args.listen_host, args.listen_port))
    s.listen(8)
    log("[relay] 监听 %s:%d -> %s:%d"
        % (args.listen_host, args.listen_port, args.board_host, args.board_port))
    try:
        while True:
            c, addr = s.accept()
            threading.Thread(target=serve, args=(c, addr, args, log), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
