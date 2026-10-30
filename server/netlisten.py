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

另外还有两条给**已经连上的**流用的：`tune_stream()`、`limit_silence()` —— 见它们自己的说明。
"""
from __future__ import annotations

import errno
import socket
import sys


#: 「请用双栈」的写法。其它值一律按字面地址处理。
ANY_HOSTS = ("::", "", None, "*")

#: Windows 的 `TCP_MAXRT`（`ws2ipdef.h`，Vista / Win7 起都有）：秒，-1 = 永不放弃。
#: Python 的 socket 模块没导出这个名字，只能写数。
#: ★ 只能在 Windows 上用这个数：Linux 上 `IPPROTO_TCP` 的 5 号是 `TCP_KEEPINTVL`。
WIN_TCP_MAXRT = 5

#: Linux 的 `TCP_USER_TIMEOUT`（2.6.37 起）：毫秒，0 = 系统默认。它没有「永不」这个值，
#: 拿 int 能表示的最大毫秒数（≈ 24.8 天）当永不。
LINUX_TCP_USER_TIMEOUT = getattr(socket, "TCP_USER_TIMEOUT", 18)
LINUX_NEVER_MS = 0x7FFFFFFF

#: ★ 服务端这头：发给玩家的字节多久一个 ACK 都收不到，就当他掉线、放掉这条连接（秒）。
#:
#: 这就是「断网的玩家座位留多久」：留短了，十几秒的断网就把人踢出关卡（10-23 那位
#: 玩家，FINDINGS §151）；留长了，真走了的人（断电、拔线，不会有 FIN / RST）会把同房间
#: 的人卡在开局加载那一步。两种系统的重传间隔都是指数退避（Windows 0.3 秒起、Linux
#: 0.2 秒起翻倍）：取 60 秒，断 30 秒上下的网，网恢复后的那一次重传都还落在 60 秒以内；
#: 同房间的人最多等一分钟。
#: ★ 玩家那头（本机中继）是**永不**放弃（`relay.connect_remote`）——「沉默的人留不留」
#:   只由服务端定；服务端放掉之后，玩家网一恢复就收到 RST，照原版流程重连回大厅。
#: ★ 应用层那两条照旧：发送积压超过 `GAME_SEND_DEADLINE_S` / `RELAY_SEND_DEADLINE_S`
#:   写不出去就拆 —— 那要等系统的发送缓冲塞满才碰得到（多人对战里断网断得久）。
PEER_SILENCE_LIMIT_S = 60


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
        except (OSError, ValueError) as error:
            if host not in ANY_HOSTS:
                raise
            # ★★ 「端口已经被占用」**绝不能**退回 IPv4 再试一次。
            #
            # Windows 上实测：有人拿着双栈 `[::]:P` 时，`0.0.0.0:P` **还能绑上**
            # （两个 socket 同时活着，进来的 IPv4 连接归谁完全看运气）。
            # 于是这个回退会把「端口被别人占了」变成「悄悄起来了、但工作不正常」
            # —— 正是最难查的那种故障。占用就要当场炸，让启动脚本把话说清楚。
            #
            # 回退本来只为一件事：**这台机器没开 IPv6**。那种失败不是 EADDRINUSE。
            if isinstance(error, OSError) and error.errno == errno.EADDRINUSE:
                raise
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


def limit_silence(sock, seconds):
    """定死一条**已经连上**的 TCP 流「发出去的字节多久没人确认就放弃」。返回 sock 本身。

    `seconds=None` = 永不放弃：只在对面明说（FIN / RST）时才断。

    ★ **为什么要自己定**（FINDINGS §151 / D109 / D110）：不定就是系统默认，而两种系统
    差了四十多倍 —— Windows 重传 5 次就放弃（20 秒上下，玩家那两次 19~25 秒），Linux 按
    `tcp_retries2` 要 15 分钟上下。10-23 那位玩家断网十几秒，就是被他电脑上 Windows 的这
    二十来秒踢出了关卡。服务端会在 Windows 和 Linux 之间换，同一份代码不能因为跑在哪台
    机器上就是两种玩法。

    ★ 只管「有字节发出去了、对面不回」：两头都没东西要发的空闲连接，这个上限根本不起算，
      和不设一样（空闲连接两种系统都不会超时，没开 keepalive）。
    ★ 这是铁律 10 的例外 —— **物理上等不到事件**：网暂时断了和对面没了，在线路上都是
      一片沉默，能区分两者的只有「沉默了多久」。
    失败一律忽略（同 `tune_stream`）：这是加固，不该让正常路径炸掉。别的系统一个字不设。
    """
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.IPPROTO_TCP, WIN_TCP_MAXRT,
                            -1 if seconds is None else int(seconds))
        elif sys.platform.startswith("linux"):
            sock.setsockopt(socket.IPPROTO_TCP, LINUX_TCP_USER_TIMEOUT,
                            LINUX_NEVER_MS if seconds is None else int(seconds) * 1000)
    except (OSError, AttributeError, NameError):
        pass
    return sock


def describe(host, port):
    """给日志用的「地址:端口」写法（IPv6 补方括号）。"""
    shown = "::" if host in ANY_HOSTS else str(host)
    return f"[{shown}]:{port}" if ":" in shown else f"{shown}:{port}"
