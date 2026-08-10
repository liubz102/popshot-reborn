#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
relay.py —— 本机 TCP 中继，只出现在**客户端包**里。

选「联机」时，`bshook` 把客户端的 `connect` 改写到本机的中继端口，
中继再按 `server.config` 里的地址转发出去：

```text
BigShot.exe --(IPv4)--> 127.0.0.1:47621 ┐                    ┌─> <server_address>:47611
                                        ├── server/relay.py ─┤
BigShot.exe --(IPv4)--> 127.0.0.1:27809 ┘   getaddrinfo 解析  └─> <server_address>:27799
```

**为什么非有它不可**（决策 D065）：客户端是 2007 年的 32 位程序，
`connect` 的参数是 `sockaddr_in`（**纯 IPv4**），`bshook` 只能把目标改写成另一个
IPv4 地址。需求要求 `server.config` 支持 IPv4 / IPv6 / **域名**三种写法 ——
在 hook 里没法表达，只能在本机加一跳，让 Python 的 `getaddrinfo` 去解析。

**纯字节转发，不碰协议**：认证服那层是 NMCO 的自定义 XOR、游戏服那层是
SimpleCipher，两者都是**有状态的流**，中继一旦拆包重组就全乱了。
这里只做 `recv` -> `sendall`，一个字节都不改。

单独跑（调试用）：

    python server/relay.py --target 192.168.1.100
    python server/relay.py --target popshot.example.com --verbose
"""
from __future__ import annotations

import argparse
import datetime
import os
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as server_config
from netlisten import create_listener

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

#: 中继只服务本机的客户端，所以**只绑 127.0.0.1**。
#: （服务端那三个口才需要对外开，见 D063。）
LISTEN_HOST = "127.0.0.1"

#: 本地端口 -> 远端端口。见 `server/config.py` 的常量说明。
PORT_MAP = (
    (server_config.RELAY_AUTH_PORT, server_config.AUTH_PORT, "认证"),
    (server_config.RELAY_GAME_PORT, server_config.GAME_PORT, "游戏"),
)

#: 连远端的超时。原版客户端自己等 10 秒才弹「认证服务器失败」，
#: 中继比它先放弃，玩家才能及时看到那个框而不是干等。
CONNECT_TIMEOUT = 6.0

VERBOSE = False
_seq = 0
_seq_lock = threading.Lock()


def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(msg):
    print(f"[{ts()}] [relay] {msg}", flush=True)


def vlog(msg):
    if VERBOSE:
        log(msg)


def _pump(src, dst, tag, counter):
    """把 `src` 收到的字节原样倒进 `dst`，直到任意一端断开。"""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            counter[0] += len(data)
            dst.sendall(data)
    except OSError:
        pass
    finally:
        # 单向关闭：让对端看到 EOF，另一半还能把剩下的数据送完。
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        vlog(f"{tag} 方向结束，共 {counter[0]} 字节")


def handle(client, addr, target_host, target_port, label):
    global _seq
    with _seq_lock:
        _seq += 1
        seq = _seq
    shown = server_config.http_host(target_host)
    try:
        remote = socket.create_connection((target_host, target_port),
                                          timeout=CONNECT_TIMEOUT)
    except OSError as error:
        # 连不上就把本地连接干脆关掉，让客户端弹它自己的「认证服务器失败」框。
        log(f"#{seq} ✗ {label}服连不上 {shown}:{target_port} —— {error}")
        log(f"      检查：server.config 里的地址对不对？对方防火墙开了 "
            f"{server_config.AUTH_PORT} 和 {server_config.GAME_PORT} 吗？服务端起了吗？")
        client.close()
        return
    log(f"#{seq} ✓ {label}服 {addr[0]}:{addr[1]} → {shown}:{target_port}")
    remote.settimeout(None)
    client.settimeout(None)
    up, down = [0], [0]
    thread = threading.Thread(target=_pump,
                              args=(client, remote, f"#{seq} 上行", up),
                              daemon=True)
    thread.start()
    _pump(remote, client, f"#{seq} 下行", down)
    thread.join(timeout=5)
    for sock in (client, remote):
        try:
            sock.close()
        except OSError:
            pass
    log(f"#{seq} — {label}服连接结束（上行 {up[0]} / 下行 {down[0]} 字节）")


def serve_one(local_port, target_host, target_port, label, ready=None):
    listener = create_listener(LISTEN_HOST, local_port)
    if ready is not None:
        ready.set()
    log(f"{label}服中继 {LISTEN_HOST}:{local_port} → "
        f"{server_config.http_host(target_host)}:{target_port}")
    while True:
        client, addr = listener.accept()
        threading.Thread(target=handle,
                         args=(client, addr, target_host, target_port, label),
                         daemon=True).start()


def start(target_host, port_map=PORT_MAP):
    """把全部中继监听器丢进后台线程，返回线程列表。"""
    threads = []
    for local_port, remote_port, label in port_map:
        ready = threading.Event()
        thread = threading.Thread(
            target=serve_one,
            args=(local_port, target_host, remote_port, label, ready),
            daemon=True, name=f"relay-{local_port}")
        thread.start()
        if not ready.wait(timeout=10):
            raise RuntimeError(f"中继端口 {local_port} 没起来（被占用了？）")
        threads.append(thread)
    return threads


def main():
    ap = argparse.ArgumentParser(
        description="本机 TCP 中继：把客户端的连接转发到联机服务器")
    ap.add_argument("--target", default=None,
                    help="联机服务器地址（IPv4 / IPv6 / 域名）。"
                         "不填就读 server.config 的 server_address")
    ap.add_argument("--config", default=None, help="server.config 路径")
    ap.add_argument("--verbose", action="store_true", help="打每个方向的字节数")
    args = ap.parse_args()

    global VERBOSE
    VERBOSE = args.verbose

    target = args.target
    if not target:
        cfg, warnings = server_config.load(args.config)
        for warning in warnings:
            log(f"server.config: {warning}")
        target = cfg["server_address"]
    target = server_config.normalize_host(target)
    log(f"联机服务器 = {server_config.http_host(target)}")

    try:
        start(target)
    except RuntimeError as error:
        log(f"!! {error}")
        return 1
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        log("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
