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


def describe(host, port):
    """给日志用的「地址:端口」写法（IPv6 补方括号）。"""
    shown = "::" if host in ANY_HOSTS else str(host)
    return f"[{shown}]:{port}" if ":" in shown else f"{shown}:{port}"
