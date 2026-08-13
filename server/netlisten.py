#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建监听 socket 的共用逻辑（认证服 / 游戏服 / 注册页三处都用它）。

规矩只有两条：

1. **默认监听 ``::``，双栈** —— IPv6 和 IPv4 的客户端都能连进来（决策 D063，
   需求原文「监听地址固定为 :: ... 不用设置」）。机器上没开 IPv6 就自动退回
   全网 IPv4，对玩家的行为是一样的。
2. **不开 ``SO_REUSEADDR``** —— 旧进程没退干净时宁可绑定失败报错，
   也不要两个进程同时 LISTEN 同一个端口、连接被谁接走看运气
   （V0.1 会话 04 踩过这个坑）。

另外还有一条给**已经连上的**流用的：`tune_stream()` —— 见它自己的说明。
"""
from __future__ import annotations

import socket


#: 「请用双栈」的写法。其它值一律按字面地址处理。
ANY_HOSTS = ("::", "", None, "*")


def address_family(host):
    """按监听地址挑 `AF_INET` 还是 `AF_INET6`。

    `socket.create_server(('127.0.0.1', p), family=AF_INET6)` 会直接
    `gaierror` —— 家族和地址必须对得上，不能一律按 IPv6 建。
    """
    if host in ANY_HOSTS:
        return socket.AF_INET6
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return socket.AF_INET6
    families = {info[0] for info in infos}
    return socket.AF_INET6 if socket.AF_INET6 in families else socket.AF_INET


def create_listener(host, port, backlog=16):
    """建好并开始 LISTEN 的 socket。返回 socket 对象。"""
    family = address_family(host)
    bind_host = "::" if host in ANY_HOSTS else host
    if family == socket.AF_INET6:
        try:
            return socket.create_server(
                (bind_host, port), family=socket.AF_INET6,
                dualstack_ipv6=True, reuse_port=False, backlog=backlog)
        except (OSError, ValueError):
            if host not in ANY_HOSTS:
                raise
            # 这台机器没开 IPv6：退回全网 IPv4。
            family, bind_host = socket.AF_INET, "0.0.0.0"
    return socket.create_server((bind_host, port), family=family,
                                reuse_port=False, backlog=backlog)


def tune_stream(sock):
    """给一条**已经连上**的 TCP 流关掉 Nagle。返回 sock 本身（方便串起来写）。

    ★ **为什么非关不可**（FINDINGS §182 / 决策 D104）

    战斗内的同步是**事件驱动**的（§181）：没有固定 tick 给我们兜底，链路上多一
    毫秒手感就差一毫秒。而联机时一份同步数据要过**三段我们自己的 Python TCP**：

        BigShot.exe → 127.0.0.1:27808 → relay.py → 服务端:27798 → relayserver.py
                 对方 BigShot.exe ← 127.0.0.1:27808 ← 对方的 relay.py ←

    这些包只有几十字节、而且是间歇发的 —— 正好是 Nagle 最擅长坑人的形态：
    发送方攒着小包等对端 ACK，对端又开着延迟 ACK（Windows 最长 200 毫秒），
    于是凭空多出几十到几百毫秒的**抖动型**停顿。用户报的「局域网也不跟手、
    看着躲开了还是被打中」就是它。

    ★ **这不是在改原版行为，是在补齐它**：客户端自己两条路都关了 Nagle ——
    收到 `0x0410` 时 `0x408703` 顺手 `setsockopt(TCP_NODELAY)`（§150），
    `RelayConnection` 连上也设（§152）。只有我们这几段一直没关。

    ★ **不会加剧 V0.1 §120 那个「两次 sendall 之间被 recv 插进来」的时序 bug**：
    Nagle 开着时第二段小写要等 ACK 才走，两段**更远**；关掉之后两段背靠背发出，
    反而更容易落进客户端同一帧的那次 recv。

    失败一律忽略：这只是调优，socket 已经关了 / 平台不支持 / 是 AF_UNIX，
    都不该让调用方的正常路径炸掉。
    """
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (OSError, AttributeError, NameError):
        pass
    return sock


def describe(host, port):
    """给日志用的「地址:端口」写法（IPv6 补方括号）。"""
    shown = "::" if host in ANY_HOSTS else str(host)
    return f"[{shown}]:{port}" if ":" in shown else f"{shown}:{port}"
