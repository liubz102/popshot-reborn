#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
relay.py —— 本机 TCP 中继，只出现在**客户端包**里。

选「远程服务器」时，`bshook` 把客户端的 `connect` 改写到本机的中继端口，
中继再按 `server.config` 里的地址转发出去：

```text
BigShot.exe --(IPv4)--> 127.0.0.1:47621 ┐                    ┌─> <server_address>:47611
BigShot.exe --(IPv4)--> 127.0.0.1:27809 ┼── server/relay.py ─┼─> <server_address>:27799
BigShot.exe --(IPv4)--> 127.0.0.1:27808 ┘   getaddrinfo 解析  └─> <server_address>:27798
```

`server.config` 设置了代理时，右边这三条出站连接统一经 SOCKS5 或 HTTP CONNECT
代理建立；没有设置时仍由 `socket.create_connection` 直接连接。`bshook` 选择
「本机服务器」时根本不会连到上面三个本地中继端口，所以本机模式天然不受代理影响。

★ **位置数据的 UDP 旁路（`UdpSyncRelay`）两种模式都走这里**（X_Mod D58，用户 2026-09-23）：
`bshook` 在 HELLO 里说这一轮选的是哪边，本机就转给 `127.0.0.1:27799/udp`、远程就转给
`server_address` —— 只差上游地址，其余同一份代码，本机测到的就是线上跑的。
★ **SOCKS5 代理时远程那条 UDP 也经代理**（X_Mod D71，用户 2026-09-26）：走 RFC 1928 的
UDP ASSOCIATE，由 `_Socks5UdpUpstream` 把每一发数据报套上 / 拆掉 SOCKS5 UDP 头。
HTTP CONNECT 代理转不了 UDP，只有它才让位置数据回退 TCP。

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
import base64
import datetime
import ipaddress
import os
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asynclog
import config as server_config
import crashwatch
import daylog
import udpsync
from netlisten import create_listener, tune_stream
import tzstamp

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

#: 中继只服务本机的客户端，所以**只绑 127.0.0.1**。
#: （服务端那三个口才需要对外开，见 D063。）
LISTEN_HOST = "127.0.0.1"

#: 玩家在登录框里选「**本机服务器**」时，位置数据 UDP 旁路的上游（X_Mod D58）：
#: 本机服务端，端口和远程一样是 `UDP_SYNC_PORT`。**本机和远程只差这一个地址。**
LOCAL_SERVER_HOST = "127.0.0.1"

#: 本地端口 -> 远端端口。见 `server/config.py` 的常量说明。
#:
#: 第三条是**原版 TCP 中继**（里程碑 J.3 / D078 / D079）。它和前两条唯一的
#: 不同是「谁发起」：认证/游戏那两条是客户端自己去连写死的端口，中继这条是
#: 服务端在 `0x0210 gspJoinRelay` 里告诉客户端「连 127.0.0.1:27798」，
#: 再由 `bshook` 按「本机 / 远程」把 27798 映射成 27808 走到这里（§157）。
PORT_MAP = (
    (server_config.RELAY_AUTH_PORT, server_config.AUTH_PORT, "认证"),
    (server_config.RELAY_GAME_PORT, server_config.GAME_PORT, "游戏"),
    (server_config.RELAY_PEER_PORT, server_config.PEER_RELAY_PORT, "中继"),
)

#: 连远端的超时。原版客户端自己等 10 秒才弹「认证服务器失败」，
#: 中继比它先放弃，玩家才能及时看到那个框而不是干等。
CONNECT_TIMEOUT = 6.0

VERBOSE = False
_seq = 0
_seq_lock = threading.Lock()

#: 崩溃日志上传的看门人（`crashwatch.py`）。默认是一个**关着的**占位对象，
#: `main()` 按 `server.config` 换成真的。
#:
#: ★ 它挂在中继上而不是挂在本机服务端上，是因为「玩家选的是远程服务器」这件事
#: 只有中继知道 —— 而且是**由构造知道**的：本机模式下 `bshook` 压根不连中继
#: （见本文件开头那张图），所以中继收到过连接就等价于「这一局是联机」。
#: 反过来让服务端按「连接是不是从 loopback 来的」去猜，V0.2 **D079 明确禁止**。
CRASH_WATCHER = crashwatch.CrashWatcher(host="", port=0, connect=None,
                                        enabled=False)


class ProxyError(OSError):
    """代理 TCP 已连上，但协商或 CONNECT 请求失败。"""


@dataclass(frozen=True)
class ProxySettings:
    """已经校验过、可直接用于建连的代理设置。"""

    kind: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def kind_name(self):
        return "SOCKS5" if self.kind == "socks5" else "HTTP CONNECT"

    @property
    def endpoint(self):
        # 只展示端点，绝不把账号密码带进日志。
        return f"{server_config.http_host(self.host)}:{self.port}"

    @property
    def route(self):
        return f"经 {self.kind_name} 代理 {self.endpoint}"


def proxy_from_config(values):
    """配置字典 -> `ProxySettings | None`。

    `proxy_address` 是唯一开关：为空时其它代理字段全部不参与连接。这样没有
    `proxy_*` 键的旧配置、显式留空的新配置都严格保持原来的直连行为。
    """
    host = server_config.normalize_host(values.get("proxy_address", ""))
    if not host:
        return None

    kind = str(values.get("proxy_type", "socks5") or "").strip().lower()
    aliases = {
        "socks": "socks5",
        "socks5": "socks5",
        "http": "http",
        "http-connect": "http",
        "http_connect": "http",
    }
    kind = aliases.get(kind, "")
    if not kind:
        raise ValueError("proxy_type 只支持 socks5 或 http")

    try:
        port = int(values.get("proxy_port", 1080))
    except (TypeError, ValueError):
        raise ValueError("proxy_port 必须是 1~65535 的端口号") from None
    if not (1 <= port <= 65535):
        raise ValueError("proxy_port 必须是 1~65535 的端口号")

    username = str(values.get("proxy_username", "") or "")
    password = str(values.get("proxy_password", "") or "")
    if password and not username:
        raise ValueError("proxy_password 已设置，但 proxy_username 为空")
    if kind == "socks5" and username:
        user_bytes = username.encode("utf-8")
        password_bytes = password.encode("utf-8")
        if not (1 <= len(user_bytes) <= 255):
            raise ValueError("SOCKS5 的代理用户名必须是 1~255 个 UTF-8 字节")
        if len(password_bytes) > 255:
            raise ValueError("SOCKS5 的代理密码最多 255 个 UTF-8 字节")

    return ProxySettings(kind, host, port, username, password)


def _recv_exact(sock, size):
    """从代理连接精确读取 `size` 字节，提前 EOF 就给出可读错误。"""
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ProxyError("代理在握手完成前关闭了连接")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _socks5_target(host, port):
    """把目标主机编码成 SOCKS5 CONNECT 请求的 ATYP + ADDR + PORT。"""
    host = server_config.normalize_host(host)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            encoded = host.encode("idna")
        except UnicodeError as error:
            raise ProxyError(f"目标域名无法编码: {error}") from error
        if not (1 <= len(encoded) <= 255):
            raise ProxyError("目标域名的 IDNA 编码长度必须是 1~255 字节")
        address_part = b"\x03" + bytes((len(encoded),)) + encoded
    else:
        if address.version == 4:
            address_part = b"\x01" + address.packed
        else:
            address_part = b"\x04" + address.packed
    return address_part + struct.pack("!H", int(port))


def _socks5_handshake(sock, proxy):
    """在已经连到代理的 socket 上完成 SOCKS5 问候 + 认证（CONNECT / UDP ASSOCIATE 共用）。"""
    if proxy.username:
        # 配了账号就只提供用户名/密码认证，避免代理悄悄选「无需认证」。
        sock.sendall(b"\x05\x01\x02")
    else:
        sock.sendall(b"\x05\x01\x00")
    version, method = _recv_exact(sock, 2)
    if version != 5:
        raise ProxyError(f"SOCKS5 握手版本错误: {version}")
    if method == 0xff:
        raise ProxyError("SOCKS5 代理拒绝了客户端提供的认证方式")
    expected = 2 if proxy.username else 0
    if method != expected:
        raise ProxyError(f"SOCKS5 代理选择了未提供的认证方式 0x{method:02x}")

    if method == 2:
        username = proxy.username.encode("utf-8")
        password = proxy.password.encode("utf-8")
        request = (b"\x01" + bytes((len(username),)) + username +
                   bytes((len(password),)) + password)
        sock.sendall(request)
        auth_version, status = _recv_exact(sock, 2)
        if auth_version != 1 or status != 0:
            raise ProxyError("SOCKS5 代理用户名或密码验证失败")


#: SOCKS5 的命令字（RFC 1928 §4）。
SOCKS5_CMD_CONNECT = 1
SOCKS5_CMD_UDP_ASSOCIATE = 3
_SOCKS5_COMMAND_NAMES = {SOCKS5_CMD_CONNECT: "CONNECT",
                         SOCKS5_CMD_UDP_ASSOCIATE: "UDP ASSOCIATE"}


def _socks5_request(sock, command, target_host, target_port):
    """发一条 SOCKS5 命令（CONNECT / UDP ASSOCIATE），返回应答里的 `(BND.ADDR, BND.PORT)`。

    CONNECT 用不着 BND（隧道就在这条连接上）；UDP ASSOCIATE 靠它知道数据报该发到
    代理的哪个口。BND.ADDR 是域名时原样返回字符串，由调用方决定要不要用。
    """
    name = _SOCKS5_COMMAND_NAMES.get(command, f"命令 0x{command:02x}")
    sock.sendall(b"\x05" + bytes((command & 0xFF,)) + b"\x00"
                 + _socks5_target(target_host, target_port))
    version, status, reserved, atyp = _recv_exact(sock, 4)
    if version != 5 or reserved != 0:
        raise ProxyError(f"SOCKS5 {name} 应答格式错误")
    if status != 0:
        reasons = {
            1: "代理服务器内部错误",
            2: "代理规则不允许此连接",
            3: "目标网络不可达",
            4: "目标主机不可达",
            5: "目标拒绝连接",
            6: "连接 TTL 超时",
            7: f"代理不支持 {name} 命令",
            8: "代理不支持目标地址类型",
        }
        raise ProxyError(f"SOCKS5 {name} 失败: {reasons.get(status, f'状态 0x{status:02x}')}")

    if atyp == 1:
        bnd_host = socket.inet_ntop(socket.AF_INET, _recv_exact(sock, 4))
    elif atyp == 4:
        bnd_host = socket.inet_ntop(socket.AF_INET6, _recv_exact(sock, 16))
    elif atyp == 3:
        length = _recv_exact(sock, 1)[0]
        bnd_host = _recv_exact(sock, length).decode("ascii", "replace")
    else:
        raise ProxyError(f"SOCKS5 {name} 应答地址类型未知: 0x{atyp:02x}")
    (bnd_port,) = struct.unpack("!H", _recv_exact(sock, 2))
    return bnd_host, bnd_port


def _socks5_connect(sock, target_host, target_port, proxy):
    """在已经连到代理的 socket 上完成 SOCKS5 协商和 CONNECT。"""
    _socks5_handshake(sock, proxy)
    # BND.ADDR / BND.PORT 对 TCP 隧道没有用，不接。
    _socks5_request(sock, SOCKS5_CMD_CONNECT, target_host, target_port)


def _socks5_udp_associate(sock, proxy):
    """在已经连到代理的 socket 上做 UDP ASSOCIATE，返回数据报该发到的 `(host, port)`。

    `DST.ADDR` / `DST.PORT` 填全零（RFC 1928 §7：还不知道自己会从哪个口发就填零，
    代理从第一发数据报学客户端的地址；我们始终从同一个 socket 发，所以够用）。

    应答的 `BND.ADDR` 有三种不能直接拿来发的写法：`0.0.0.0` / `::`（意思是「就发到
    你连我的这个地址」）、域名（又得本机解析一次）、`::ffff:a.b.c.d`（Windows 的
    AF_INET6 socket 默认 V6ONLY，发不到映射地址）—— 统一换成**控制连接的对端地址**，
    那是已经解析过、已经通了的那个。
    """
    _socks5_handshake(sock, proxy)
    bnd_host, bnd_port = _socks5_request(sock, SOCKS5_CMD_UDP_ASSOCIATE, "0.0.0.0", 0)
    if bnd_port == 0:
        raise ProxyError("SOCKS5 UDP ASSOCIATE 应答的中转端口是 0")
    peer_host = sock.getpeername()[0]
    try:
        address = ipaddress.ip_address(bnd_host)
    except ValueError:
        return peer_host, bnd_port          # 域名：不在本机解析，用控制连接的对端
    if address.is_unspecified:
        return peer_host, bnd_port
    if address.version == 6 and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped), bnd_port
    return str(address), bnd_port


def socks5_udp_header(target_host, target_port):
    """SOCKS5 UDP 数据报的头（RFC 1928 §7）：`RSV(2)=0 FRAG(1)=0 ATYP ADDR PORT`。

    按目标只编一次，每发数据报前面原样加（`socks5_udp_wrap`）。域名走 ATYP 3，
    DNS 交给代理解 —— 和 TCP CONNECT 一个口径，本机一次都不解析目标。
    """
    return b"\x00\x00\x00" + _socks5_target(target_host, target_port)


def socks5_udp_wrap(header, payload):
    return header + payload


def socks5_udp_unwrap(datagram):
    """`代理发来的数据报 -> (载荷, (来源主机, 来源端口))`。

    不是一份完整、不分片的 SOCKS5 UDP 数据报（RSV / FRAG 非 0、地址类型不认识、
    长度不够）就返回 `None` —— 调用方当没收到，继续等下一发。
    来源只给日志看：X_Mod D58 定了不比对来源地址（代理 / NAT 后面对不上是常态）。
    """
    if len(datagram) < 4 or datagram[0] or datagram[1] or datagram[2]:
        return None
    atyp = datagram[3]
    if atyp == 1:
        end = 4 + 4
        if len(datagram) < end + 2:
            return None
        host = socket.inet_ntop(socket.AF_INET, bytes(datagram[4:end]))
    elif atyp == 4:
        end = 4 + 16
        if len(datagram) < end + 2:
            return None
        host = socket.inet_ntop(socket.AF_INET6, bytes(datagram[4:end]))
    elif atyp == 3:
        if len(datagram) < 5:
            return None
        end = 5 + datagram[4]
        if len(datagram) < end + 2:
            return None
        host = bytes(datagram[5:end]).decode("ascii", "replace")
    else:
        return None
    (port,) = struct.unpack_from("!H", datagram, end)
    return bytes(datagram[end + 2:]), (host, port)


def _http_connect(sock, target_host, target_port, proxy):
    """在已经连到代理的 socket 上完成 HTTP CONNECT。"""
    target_host = server_config.normalize_host(target_host)
    try:
        ipaddress.ip_address(target_host)
        wire_host = target_host
    except ValueError:
        try:
            wire_host = target_host.encode("idna").decode("ascii")
        except UnicodeError as error:
            raise ProxyError(f"目标域名无法编码: {error}") from error
    authority = f"{server_config.http_host(wire_host)}:{int(target_port)}"
    lines = [
        f"CONNECT {authority} HTTP/1.1",
        f"Host: {authority}",
        "Proxy-Connection: Keep-Alive",
    ]
    if proxy.username:
        token = base64.b64encode(
            f"{proxy.username}:{proxy.password}".encode("utf-8")).decode("ascii")
        lines.append(f"Proxy-Authorization: Basic {token}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
    sock.sendall(request)

    response = bytearray()
    while b"\r\n\r\n" not in response:
        # 不能一次读 4096：目标服务端可能在 CONNECT 成功后立刻发协议开场白，
        # 代理又可能把它和 HTTP 响应头合进同一个 TCP 段。若在这里过读并丢掉尾巴，
        # 客户端的有状态握手就会从第一个字节开始错位。
        chunk = sock.recv(1)
        if not chunk:
            raise ProxyError("HTTP 代理在 CONNECT 应答完成前关闭了连接")
        response.extend(chunk)
        if len(response) > 65536:
            raise ProxyError("HTTP 代理的 CONNECT 应答头超过 64 KiB")
    header = bytes(response).split(b"\r\n\r\n", 1)[0]
    first_line = header.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
    parts = first_line.split(None, 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise ProxyError(f"HTTP 代理的 CONNECT 状态行无法识别: {first_line!r}")
    status = int(parts[1])
    if not (200 <= status < 300):
        reason = parts[2] if len(parts) >= 3 else ""
        raise ProxyError(f"HTTP CONNECT 失败: {status} {reason}".rstrip())


def connect_remote(target_host, target_port, proxy=None):
    """直连目标，或严格通过指定代理建立到目标的 TCP 隧道。

    配置了代理时任何失败都会向上抛出；这里**没有直连回退**，防止用户明确要求
    代理后，故障路径反而把真实出口暴露给远端。
    """
    endpoint = (target_host, target_port) if proxy is None else (proxy.host, proxy.port)
    sock = socket.create_connection(endpoint, timeout=CONNECT_TIMEOUT)
    # 关 Nagle。走代理时同样要关 —— 隧道里跑的还是那几十字节的小包（D104）。
    tune_stream(sock)
    if proxy is None:
        return sock
    try:
        if proxy.kind == "socks5":
            _socks5_connect(sock, target_host, target_port, proxy)
        else:
            _http_connect(sock, target_host, target_port, proxy)
        return sock
    except BaseException:
        sock.close()
        raise


def ts():
    """和 `gameserver.ts()` 同一个格式（带完整日期 + 时区，见那边的说明）。"""
    return tzstamp.stamp(millis=True)


def log(msg):
    asynclog.emit(f"[{ts()}] [relay] {msg}")


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


def handle(client, addr, target_host, target_port, label, proxy=None,
           local_port=0):
    global _seq
    with _seq_lock:
        _seq += 1
        seq = _seq
    shown = server_config.http_host(target_host)
    try:
        remote = connect_remote(target_host, target_port, proxy)
    except OSError as error:
        # 连不上就把本地连接干脆关掉，让客户端弹它自己的「认证服务器失败」框。
        if proxy is None:
            log(f"#{seq} ✗ {label}服直连 {shown}:{target_port} 失败 —— {error}")
            log(f"      检查：server.config 里的地址对不对？对方防火墙开了 "
                f"{server_config.AUTH_PORT} / {server_config.GAME_PORT} / "
                f"{server_config.PEER_RELAY_PORT} 吗？服务端起了吗？")
        else:
            log(f"#{seq} ✗ {label}服连接 {shown}:{target_port} 失败"
                f"（{proxy.route}）—— {error}")
        if target_port == server_config.PEER_RELAY_PORT:
            # 中继连不上不只是「这条没通」——客户端的 RelayConnection 一失败就
            # 会把玩家踢出房间（FINDINGS §158）。让日志把话说明白。
            log("      ⚠ 中继连不上会让客户端自己退出房间。"
                "服务端加 --no-tcp-relay 可以先绕过（同步退回 0x040e 那条路）。")
        client.close()
        return
    route = "直连" if proxy is None else proxy.route
    log(f"#{seq} ✓ {label}服 {addr[0]}:{addr[1]} → {shown}:{target_port}（{route}）")
    # ★ 「有客户端经中继连出去了」= 玩家选的是**远程服务器**（本机模式下
    #   bshook 压根不连中继）。崩溃日志上传只在这种情况下才发生 ——
    #   这是**由构造保证**的，不是判出来的（D079 禁止按 loopback 猜模式）。
    #   ★★ 这一句必须是 O(1) 的入队：后面紧接着就是 `_pump` 转发游戏字节，
    #      在这里多花的每一微秒都直接压在玩家的延迟上。
    CRASH_WATCHER.note_client(addr, local_port)
    remote.settimeout(None)
    client.settimeout(None)
    # 两个方向都要关 Nagle：`remote` 在 connect_remote 里已经关过，这里补上
    # 面向 BigShot.exe 的那条（下行同步数据全从它出去）。见 D104。
    tune_stream(client)
    tune_stream(remote)
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


def serve_one(local_port, target_host, target_port, label, ready=None, proxy=None):
    listener = create_listener(LISTEN_HOST, local_port)
    if ready is not None:
        ready.set()
    log(f"{label}服中继 {LISTEN_HOST}:{local_port} → "
        f"{server_config.http_host(target_host)}:{target_port}")
    while True:
        client, addr = listener.accept()
        threading.Thread(target=handle,
                         args=(client, addr, target_host, target_port, label,
                               proxy, local_port),
                         daemon=True).start()


def start_udp_sync(target_host, proxy=None, enabled=True, redundancy=2):
    """把位置数据的 UDP 中继拉起来。返回 `UdpSyncRelay | None`。

    **任何一种起不来的情况都只打一行日志、返回 `None`** —— 这条通道从头到尾
    都是「TCP 之外多走一份」，没有它游戏完全正常。
    """
    if not enabled:
        log("位置UDP  已关闭（server.config 的 udp_sync = 0）；位置数据走 TCP")
        return None
    # ★ 代理只管「远程」那条上游（X_Mod D58 / D71）：SOCKS5 走 UDP ASSOCIATE，HTTP CONNECT
    #   根本转不了 UDP ⇒ 只有 HTTP 代理才让远程模式保持 TCP。
    #   「本机服务器」那条是环回，和代理无关，照常走（以前这里整条不起，本机模式跟着没了）。
    relay = UdpSyncRelay(target_host, redundancy=redundancy, proxy=proxy)
    return relay if relay.start() else None


def start(target_host, port_map=PORT_MAP, proxy=None):
    """把全部中继监听器丢进后台线程，返回线程列表。"""
    threads = []
    for local_port, remote_port, label in port_map:
        ready = threading.Event()
        thread = threading.Thread(
            target=serve_one,
            args=(local_port, target_host, remote_port, label, ready, proxy),
            daemon=True, name=f"relay-{local_port}")
        thread.start()
        if not ready.wait(timeout=10):
            raise RuntimeError(f"中继端口 {local_port} 没起来（被占用了？）")
        threads.append(thread)
    return threads


#: HELLO 还没被确认时多久重发一次（秒）。服务端重启过、UDP 包丢了、
#: 玩家进游戏时服务端还没起来、代理撤掉了 UDP 关联（X_Mod D71）—— 都靠它自己接回来。
#: ★ 这是 UDP 上物理等不到事件的地方（铁律 10 的例外）：对端收没收到 HELLO 没有任何回执可等。
HELLO_RETRY_S = 2.0

#: 确认之后多久发一发保活（秒）。家用路由器的 UDP 映射常见 30~60 秒超时，
#: 10 秒足够撑住；战斗中本来就有 8 Hz 的数据，保活只在大厅里真的起作用。
KEEPALIVE_S = 10.0

#: 收不到任何回应多久算这条路不通（秒）。到点只打一行日志 —— **不做任何降级**，
#: 因为 TCP 那份从来没停过，UDP 不通对玩家就是「和以前一样」。
#:
#: ★ 从**本条游戏连接的第一发 HELLO** 起算（`first_hello_at`），不是从中继
#: 进程启动起算；而且只在**一个回应都没收到**时才打。服务端回了「认不出票据」
#: 属于「路是通的、票据不对」，那是 `refused_logged` 那条日志的事。
UDP_QUIET_WARN_S = 20.0


class _Socks5UdpUpstream:
    """经 SOCKS5 代理转发的 UDP 上游 —— **长得和一个 UDP socket 一样**（X_Mod D71）。

    `UdpSyncRelay` 的上游是一对 `(socket, 地址)`：发只调 `sendto(载荷, 地址)`，收只调
    `recvfrom(n)`，关只调 `close()`，换路由时按**对象身份**认「这是不是这一轮的上游」。
    让代理版上游长成同一个形状，那三处一行不用改 —— 直连 / 经代理只差「上游是哪个对象」，
    和 D58「本机 / 远程只差上游地址」是同一个思路。

    RFC 1928 §7：UDP ASSOCIATE 靠一条 TCP **控制连接**活着，它一断代理就撤掉关联、之前的
    中转口作废。所以控制连接要一直握着，并有一条守望线程在它上面阻塞 `recv` ——
    关联建好之后代理不会再往这条连接发有意义的字节，`recv` 返回**只可能是** EOF / 出错，
    那就是「关联没了」的事件：不轮询、不定时，`on_dead(self)` 一次。我们自己 `close()`
    时先置 `_closing`，守望线程看到它就不回调。

    每一发数据报套上 / 拆掉 SOCKS5 UDP 头（`socks5_udp_wrap` / `socks5_udp_unwrap`）；
    目标（游戏服）写在头里，域名交给代理解，本机一次都不解析。
    """

    def __init__(self, proxy, target_host, target_port, on_dead=None):
        self.proxy = proxy
        self.target = (server_config.normalize_host(target_host), int(target_port))
        self._on_dead = on_dead
        self._closing = False
        self.sock = None
        self.control = socket.create_connection((proxy.host, proxy.port),
                                                timeout=CONNECT_TIMEOUT)
        try:
            tune_stream(self.control)
            self.relay_addr = _socks5_udp_associate(self.control, proxy)
            # 关联建好之后这条连接上不再有往来。守望线程要的是「一直阻塞到断开」，
            # 不能带着 create_connection 留下的超时（否则每 6 秒被 socket.timeout 打断一次）。
            self.control.settimeout(None)
            self._header = socks5_udp_header(self.target[0], self.target[1])
            family = socket.AF_INET6 if ":" in self.relay_addr[0] else socket.AF_INET
            # ★ 不 connect：connect 过的 UDP socket 会让内核丢掉「代理从另一个口回」的
            #   数据报；D58 也定了不比对来源地址。
            self.sock = socket.socket(family, socket.SOCK_DGRAM)
            self.sock.settimeout(0.5)
        except BaseException:
            self.close()
            raise
        threading.Thread(target=self._watch, daemon=True,
                         name="udpsync-socks5-watch").start()

    # -- 和 UDP socket 同形 ---------------------------------------------------
    def sendto(self, payload, addr):
        # `addr` 就是 `relay_addr`（调用方按 `(上游, 地址)` 那一对来调，形状和直连一致）。
        return self.sock.sendto(socks5_udp_wrap(self._header, payload), self.relay_addr)

    def recvfrom(self, size):
        while True:
            data, _ = self.sock.recvfrom(size)
            unwrapped = socks5_udp_unwrap(data)
            if unwrapped is not None:
                return unwrapped
            # 坏头 / 分片 / 不是 SOCKS5 UDP 数据报：**不是一发回应**，继续等
            # （否则垃圾会把「20 秒没等到回应」那条提示压掉）。socket.timeout 照常往外抛。

    def fileno(self):
        return -1 if self.sock is None else self.sock.fileno()

    def close(self):
        self._closing = True
        for sock in (self.sock, self.control):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass

    # -- 守望 -----------------------------------------------------------------
    def _watch(self):
        try:
            while self.control.recv(4096):
                pass                        # 代理若发了什么，不是我们要的，吃掉
        except OSError:
            pass
        if self._closing or self._on_dead is None:
            return
        self._on_dead(self)


class UdpSyncRelay:
    """位置数据的本机 UDP 中继（`server/udpsync.py` 是它的对端）。

    ```text
                                                         ┌─ 选「远程服务器」─> <server_address>:27799/udp
    BigShot.exe --(bshook 镜像)--> 127.0.0.1:27809/udp ─┤
                                                本类 ───┤
    BigShot.exe:27807/udp <--(下行注入)-----------------┘─ 选「本机服务器」─> 127.0.0.1:27799/udp
    ```

    ★ **它不是「把 TCP 换成 UDP」，是在 TCP 之外多走一份。** 客户端那份
    `0x040e` 照发不误，所以这条 UDP 通道**整条不通也没有任何后果** ——
    服务端按索引去重，UDP 没到就用 TCP 那份。

    ★★ **本机 / 远程只差上游这一个地址**（X_Mod D58，用户 2026-09-23）。`bshook` 两种模式
    都往同一个口发，这一轮往哪转由 HELLO 里的 `HELLO_FLAG_LOCAL_SERVER` 说了算；冗余捎带、
    下行注入、HELLO 重试 / 保活全是同一份代码 —— 本机测到的就是线上跑的。
    两条上游**各用各的 socket**（`_upstreams`）：换了路由之后，上一条上游迟到的回包落在
    它自己那条 socket 上，`_pump_remote` 直接不认，不用去比对来源地址（远程服务器在
    NAT / 负载均衡后面时，回包的来源地址未必就是我们发去的那个）。

    ★ **代理只管「远程」那条**（X_Mod D71）：SOCKS5 代理经 UDP ASSOCIATE 照走 UDP
    （上游换成 `_Socks5UdpUpstream`，其余一个字不改）；HTTP CONNECT 根本转不了 UDP，
    只有它才让远程模式保持 TCP。「本机」那条是环回，和代理无关，照常走。
    代理撤掉关联（控制连接断了）是一个事件：`_on_upstream_dead` 把这一轮的上游撤掉、
    `acked` 清掉，现成的 HELLO 重发（`_retry_hello`）就会把它重建起来 —— 不加定时器。
    """

    def __init__(self, target_host, target_port=None, local_port=None,
                 redundancy=2, proxy=None, local_target=None):
        self.target_host = target_host
        self.target_port = target_port or server_config.UDP_SYNC_PORT
        self.local_port = local_port or server_config.RELAY_UDP_SYNC_PORT
        #: 选「本机服务器」时的上游 `(host, port)`。参数只给测试留的口 —— 真的
        #: 27799 在测试机上多半正被本机服务端占着。
        self.local_target = tuple(local_target or (LOCAL_SERVER_HOST,
                                                   server_config.UDP_SYNC_PORT))
        #: 远程那条上游经哪个代理（`ProxySettings | None`）。只影响远程那条：
        #: SOCKS5 → UDP ASSOCIATE；HTTP → 转不了 UDP，那条上游不开（本机那条不受影响）。
        self.proxy = proxy
        self.redundancy = max(0, int(redundancy))
        #: 游戏那个「收位置数据的 UDP 口」bind 成功了没有。
        #: ★ 这个值**不是我们判的，是 `bshook` 告诉我们的** —— 它在游戏进程里
        #: 钩住 `bind`，亲眼看着那一次 bind 返回 0 才置位。所以它是权威的，
        #: 不存在「口被别的程序占着而我们以为是游戏」那种假阳性。
        self.downlink = False
        #: ★ 这一轮登录选的是不是「本机服务器」—— **`bshook` 在 HELLO 里说的**
        #:   （`HELLO_FLAG_LOCAL_SERVER`，X_Mod D58），不是我们猜的。还没收到过 HELLO
        #:   时按远程算，和加这一位之前一个字节不差。
        self.route_local = False
        self.local = None
        #: 这一轮的上游 `(socket, 地址)`；`None` = 这一轮不转（远程经 HTTP 代理 / 代理不给
        #: UDP / 解析不了 / 没 `start()` 过）。★ 两格放在**一个**元组里一起换，别的线程读到的
        #: 永远是同一条路由的一对，不会拿新 socket 往旧地址发。
        self._up = None
        #: `route_local -> (socket, 地址)`：每条路由第一次用到时才建，之后一直留着
        #: （经代理那条除外：代理撤掉关联时被 `_on_upstream_dead` 弹掉，下次用到再建）。
        self._upstreams = {}
        #: 「这一轮的上游」上次打日志时是哪条 —— 按状态翻转说话（铁律 10）。
        self._route_said = None
        #: 改 `_upstreams` / `_up` / `_route_said` 时持有。★ 建上游那一段（连代理最多
        #: 几个 CONNECT_TIMEOUT）**不在**它里面 —— 先建好、再换指针。
        self._route_lock = threading.Lock()
        #: 每条路由一把「正在建上游」的锁：两个线程（收 HELLO 的 / 重发 HELLO 的）同时发现
        #: 没上游时只让一个去建，另一个等它建完直接用 —— 否则会开出两条关联，输的那条
        #: 永远不在 `_upstreams` 里、守望线程永远挂着。
        self._open_locks = {False: threading.Lock(), True: threading.Lock()}
        #: 「经代理建不了 UDP 通道」这句话说过没有 —— 按状态翻转去重：说一次，直到真的
        #: 建起来一次才重新允许说（HELLO 每 2 秒重试一次，逐次打就是刷屏）。
        self._proxy_fail_said = False
        self._started = False
        self.hook_addr = None
        self.ticket = ""
        self.acked = False
        #: ★ **本条游戏连接**的第一发 `HELLO` 的时刻（`0` = 还没发过）。
        #: 「这条路好像不通」的提示必须从这里起算，**不能从中继进程启动的时刻起算**
        #: —— 中继是随启动脚本先起来的，玩家点开游戏、输账号、进大厅，
        #: 到发出登录包时早就过了 20 秒，于是那句吓人的警告会在游戏刚连上的
        #: 0.4 秒内立刻打出来、紧接着才是 `✓ 已认出`（§225 第六节）。
        self.first_hello_at = 0.0
        self.last_hello_at = 0.0
        self.last_keepalive_at = 0.0
        self.warned_quiet = False
        #: 「服务器不认这条通道」这句话说过没有。**按状态翻转去重，不按次数**：
        #: 说一次，直到通了（`ACK_OK`）或者换了一条游戏连接才重新允许说。
        #:
        #: ★ 「登录时那一发必然被拒」不靠这里挡 —— 那是服务端用
        #: `ACK_NOT_LOGGED_IN` 明说的**事件**（票据是真的、只是还没登进来），
        #: 见 `_on_remote_datagram`。靠「跳过头 N 发」挡的话，
        #: 换台慢机器、换条慢线路，N 就不对了。
        self.refused_logged = False
        #: **本条游戏连接**收到过几发来自服务器的数据报（`received` 是整个
        #: 进程的累计值，不能拿来判「这一条通不通」——玩家换个服务器 /
        #: 服务端重启成没放行 UDP 的样子，累计值仍然大于 0，提示就哑了）。
        self.replies = 0
        self.sent = 0
        self.received = 0
        self.injected = 0
        #: 最近几份（含当前）：`[(索引, UdpPacket), …]`，冗余捎带用。
        self.recent = []
        #: 下行闸门：只准前进。**没有它，网络乱序或冗余补发会把角色拉回旧位置。**
        self.downlink_high_water = -1
        #: ★ 真乱序（不是冗余捎带）被闸门拦下来的次数，以及「说过了没有」
        #: —— 按状态翻转打日志，不逐发刷屏（铁律 10）。V0.3 §154。
        self.reordered = 0
        self.warned_reorder = False
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # -- 上游：这一轮往哪转 -------------------------------------------------
    @property
    def remote(self):
        """这一轮上游的 socket；`None` = 这一轮不转。"""
        up = self._up
        return None if up is None else up[0]

    @property
    def remote_addr(self):
        up = self._up
        return None if up is None else up[1]

    @property
    def proxied(self):
        """远程那条要经代理（不管哪种）。"""
        return self.proxy is not None

    def _route_name(self, local=None):
        return "本机服务器" if (self.route_local if local is None else local) \
            else "远程服务器"

    def _upstream_desc(self, up):
        """一条上游怎么写进日志：直连写地址；经代理写「目标，经代理（中转口）」。"""
        if isinstance(up[0], _Socks5UdpUpstream):
            target = up[0].target
            return (f"{server_config.http_host(target[0])}:{target[1]}/udp，经 "
                    f"{up[0].proxy.route}（UDP 中转口 "
                    f"{server_config.http_host(up[1][0])}:{up[1][1]}）")
        return f"{server_config.http_host(up[1][0])}:{up[1][1]}/udp"

    def _open_upstream(self, local):
        """建一条上游：`(socket, 地址)`；开不出来返回 `None`（这一轮不转，TCP 照常）。"""
        if local:
            host, port = self.local_target
        elif self.proxy is not None:
            return self._open_proxied_upstream()
        else:
            host, port = self.target_host, self.target_port
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
            family, _, _, _, sockaddr = infos[0]
        except OSError as error:
            log(f"位置UDP  ✗ 解析不了 {server_config.http_host(host)}: {error}；"
                f"选「{self._route_name(local)}」时位置数据继续走 TCP")
            return None
        try:
            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
        except OSError as error:
            log(f"位置UDP  ✗ 建不了到 {server_config.http_host(host)} 的 UDP socket"
                f"（{error}）；选「{self._route_name(local)}」时位置数据继续走 TCP")
            return None
        threading.Thread(target=self._pump_remote, args=(sock,), daemon=True,
                         name="udpsync-remote-local" if local
                         else "udpsync-remote").start()
        return (sock, sockaddr)

    def _open_proxied_upstream(self):
        """远程那条上游经代理（X_Mod D71）：SOCKS5 走 UDP ASSOCIATE；HTTP CONNECT 转不了 UDP。

        ★ 目标地址**不在本机解析**（域名写进每发数据报的头里交给代理）—— 配了代理就不许
          有任何直连动作，DNS 也算。
        ★ 失败只说一次（`_proxy_fail_said`），直到真的建起来一次才重新允许说：这里会被
          HELLO 重发每 2 秒叫一次。不能靠 `_use_route` 的「上游换了才说」去重 —— `start()`
          是 quiet 调的、登录那发 HELLO 又和它同一条路由，两处都会把这句吞掉。
        """
        proxy = self.proxy
        if proxy.kind != "socks5":
            if not self._proxy_fail_said:
                self._proxy_fail_said = True
                log(f"位置UDP  ✗ {proxy.kind_name} 代理转不了 UDP；选「远程服务器」时"
                    f"位置数据继续走 TCP（TCP 照旧经代理）")
            return None
        try:
            upstream = _Socks5UdpUpstream(proxy, self.target_host, self.target_port,
                                          on_dead=self._on_upstream_dead)
        except OSError as error:            # ProxyError 也是 OSError
            if not self._proxy_fail_said:
                self._proxy_fail_said = True
                log(f"位置UDP  ✗ 经 {proxy.route} 建不了 UDP 通道（{error}）；"
                    f"选「远程服务器」时位置数据继续走 TCP（还会随 HELLO 重试，"
                    f"建起来会再打一行）")
            return None
        self._proxy_fail_said = False
        threading.Thread(target=self._pump_remote, args=(upstream,), daemon=True,
                         name="udpsync-remote").start()
        return (upstream, upstream.relay_addr)

    def _use_route(self, local, quiet=False):
        """这一轮登录往哪转（X_Mod D58）。由 HELLO 里的那一位决定，`start()` 先按远程备好。

        每条路由的 socket 第一次用到才建，之后留着复用；没 `start()` 过（单测直接喂报文）
        只记路由、不开 socket。上游换了才打一行 —— 按状态翻转说话。

        ★ 建上游可能阻塞（经代理时最多几个 CONNECT_TIMEOUT）。这段时间里 bshook 可能又发了
          一发 HELLO 把路由切走了（X_Mod D71）—— 所以 `local` 在入口就存成局部量，建完之后
          只有「路由还是我这条」才把它换成当前上游；而同一条路由由 `_open_locks` 保证只有
          一个线程在建，另一个等它建完直接用。
        """
        local = bool(local)
        self.route_local = local
        if not self._started:
            return
        with self._open_locks[local]:
            with self._route_lock:
                up = self._upstreams.get(local)
            if up is None:
                up = self._open_upstream(local)
                if up is not None:
                    with self._route_lock:
                        self._upstreams[local] = up
        with self._route_lock:
            if self.route_local != local:
                return                      # 建的这段时间路由被切走了，那一发已经换过上游
            self._up = up
            said = (local, None if up is None else up[1])
            changed = said != self._route_said
            self._route_said = said
        if not changed or quiet:
            return                          # quiet：start() 那一行已经把两条路由说全了
        if up is not None:
            log(f"位置UDP  这一轮登录选的是「{self._route_name(local)}」→ 上游 "
                f"{self._upstream_desc(up)}")
        elif self.proxy is not None and not local:
            log(f"位置UDP  这一轮登录选的是「远程服务器」，经 {self.proxy.route} ——"
                f" 这条 UDP 通道没建起来，位置数据回退 TCP（原因见上一行 ✗）")

    def _on_upstream_dead(self, upstream):
        """守望线程报「代理撤掉了 UDP 关联」（控制连接 EOF / 出错，X_Mod D71）。

        先把它从 `_upstreams` 弹掉、再关（顺序不能反：先关会让 `_pump_remote` 在它上面
        空转到弹掉为止）。只有它还是**当前**上游才动这一轮的状态 —— 路由已经切到本机时，
        本机那一轮的确认 / 闸门一个都不能碰，也不该打「回退 TCP」。

        ★ 闸门必须清：服务端见到重建后的新来源地址会新建 `Endpoint`、下行索引从 0 起
          （`udpsync._on_hello`），`downlink_high_water` 不清就把之后几分钟的下行全丢掉。
          旧关联的 socket 已关，不可能再有旧包混进来，清是安全的。
        ★ `acked` 清掉是事实（服务端认的是一个已经不存在的端点），于是现成的 HELLO 重发
          （`_retry_hello`）会重建关联并重新 HELLO —— 不加任何新的定时器 / 次数。
        """
        if self._stop.is_set():
            return
        with self._route_lock:
            for key, up in list(self._upstreams.items()):
                if up[0] is upstream:
                    del self._upstreams[key]
            current = self._up is not None and self._up[0] is upstream
            if current:
                with self._lock:
                    self.acked = False
                    self.downlink_high_water = -1
                    self.reordered = 0
                    self.warned_reorder = False
                    # 路换了，「这条路通不通」要重新计量（和换了一条游戏连接时一样）。
                    self.replies = 0
                    self.first_hello_at = 0.0
                    self.warned_quiet = False
                self._route_said = None
                # 最后才撤指针：别的线程一看到「这一轮没上游」，上面那些账已经清好了。
                self._up = None
        upstream.close()
        if current:
            log(f"位置UDP  ✗ 代理关掉了 UDP 通道的控制连接（关联作废，经 "
                f"{upstream.proxy.route}）；位置数据回退 TCP，下一发 HELLO 重建")

    # -- 建 socket ----------------------------------------------------------
    def start(self):
        """建好本机那条 socket 并把收发线程拉起来。失败时返回 `False`（不抛）。

        ★ 上游 socket 按路由各建一条（`_use_route`）。这里先按远程备好 —— 和以前一样
          一启动就解析 `server_address`、解析不了当场说一声；但**解析不了不再让整条
          旁路起不来**：选「本机服务器」那条用不着它。
        """
        try:
            self.local = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.local.bind((LISTEN_HOST, self.local_port))
            self.local.settimeout(0.5)
        except OSError as error:
            log(f"位置UDP  ✗ 本机 {LISTEN_HOST}:{self.local_port}/udp 起不来"
                f"（{error}）；位置数据继续走 TCP")
            self.close()
            return False
        self._started = True
        for target, name in ((self._pump_local, "udpsync-local"),
                             (self._pump_timer, "udpsync-timer")):
            threading.Thread(target=target, daemon=True, name=name).start()
        shown = f"{server_config.http_host(self.target_host)}:{self.target_port}/udp"
        if self.proxy is None:
            remote = shown
        elif self.proxy.kind == "socks5":
            remote = f"经 {self.proxy.route} 转到 {shown}（UDP ASSOCIATE）"
        else:
            remote = f"{self.proxy.kind_name} 代理转不了 UDP，位置数据回退 TCP"
        log(f"位置UDP  {LISTEN_HOST}:{self.local_port}/udp → 选「远程服务器」时 {remote}；"
            f"选「本机服务器」时 {self.local_target[0]}:{self.local_target[1]}/udp"
            f"（冗余 {self.redundancy} 份；只走位置数据，其余照旧 TCP）")
        self._use_route(False, quiet=True)
        return True

    def close(self):
        self._stop.set()
        with self._route_lock:
            socks = [self.local] + [up[0] for up in self._upstreams.values()]
        for sock in socks:
            try:
                if sock is not None:
                    sock.close()
            except OSError:
                pass

    # -- bshook -> 我们 -> 服务器 -------------------------------------------
    def _pump_local(self):
        while not self._stop.is_set():
            try:
                data, addr = self.local.recvfrom(udpsync.MAX_DATAGRAM * 2)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            self.hook_addr = addr
            try:
                self._on_hook_datagram(data)
            except Exception as error:      # noqa: BLE001 —— 收包循环必须不死
                vlog(f"位置UDP  处理 bshook 数据报出错（忽略）: {error!r}")

    def _on_hook_datagram(self, data):
        try:
            kind, _ = udpsync.parse_header(data)
        except udpsync.ProtocolError:
            return
        if kind == udpsync.MSG_HELLO:
            # `bshook` 发来的 HELLO 有两种时机：
            #   * 登录（发出 `0x0100`）—— 票据换了、索引从头数；
            #   * 游戏成功 bind 了收位置数据的 UDP 口 —— 标志位置起来。
            # 两种都把票据 + 下行位转告服务端。★ 「本机服务器」那一位**不转**
            # （`_send_hello` 重新拼的包只带下行位）—— 它只决定我们往哪转（X_Mod D58）。
            try:
                ticket, flags = udpsync.parse_hello_full(data)
            except udpsync.ProtocolError:
                return
            downlink = bool(flags & udpsync.HELLO_FLAG_DOWNLINK)
            local = bool(flags & udpsync.HELLO_FLAG_LOCAL_SERVER)
            with self._lock:
                # ★ 「新的一条游戏连接」不能靠票据变没变来判 —— 断线重连时
                #   客户端会**原样重放同一张票据**（§171）。判据是标志位从
                #   「已绑」回到「没绑」：`bshook` 每发一次登录包就把它清一次。
                #   换了服务器（本机 ↔ 远程）当然也是新的一条。
                restart = ((ticket != self.ticket) or (self.downlink and not downlink)
                           or (local != self.route_local))
                self.ticket = ticket
                if restart:
                    # 索引、水位、确认状态全部从头来 —— 服务端那边是一条新的
                    # `Conn`，计数器同样从 0 起，两边这才对得上。
                    self.recent.clear()
                    self.acked = False
                    self.downlink_high_water = -1
                    self.reordered = 0
                    self.warned_reorder = False
                    # ★ 提示的账也跟着清：新的一条游戏连接要重新计时、
                    #   重新允许打一次日志（否则整个中继进程里只会提示一次，
                    #   玩家中途换服务器 / 服务端重启就再也看不到提示了）。
                    self.first_hello_at = 0.0
                    self.warned_quiet = False
                    self.refused_logged = False
                    self.replies = 0
                changed = (downlink != self.downlink)
                self.downlink = downlink
            # 这一轮往哪转 —— 必须在转发这发 HELLO **之前**定下来。
            self._use_route(local)
            if changed:
                log(f"位置UDP  下行 {'已就绪' if downlink else '未就绪'}"
                    f"（游戏的 UDP {server_config.CLIENT_UDP_PORT} "
                    f"{'已 bind' if downlink else '还没 bind'}）")
            self._send_hello()
            return
        if kind in (udpsync.MSG_PRESENCE, udpsync.MSG_MOVER_PHASE,
                    udpsync.MSG_TICK_CLOCK):
            # ★ 在场证据（bug调查/25）、移动平台相位（X_Mod §74）、逻辑帧时钟（§81 / D60）：
            #   中继**原样转发，一个字节不看**。前者是「键盘 / 鼠标 / 这台机器多久没动过、
            #   游戏在不在前台」，后两个是「挂在路径上的地形各自的 t0 / 偏移 + 客户端的时钟」，
            #   判定都在游戏服那边 —— 那边的阈值 / 取值顺序改了不该要求重发客户端，
            #   更不该要求重发中继。
            #   ★ 老服务端不认识这两个 kind，会在 `UdpHub._handle` 里安静丢掉
            #   ⇒ 新客户端 + 老服务端 = 「没有这条信息」，退回今天的行为。
            self._to_remote(data)
            return
        if kind != udpsync.MSG_DATA:
            return
        try:
            chunks = udpsync.parse_data(data)
        except udpsync.ProtocolError:
            return
        with self._lock:
            added = 0
            for index, packet in chunks:
                # 铁律 1：只有位置心跳能走这条路。`bshook` 那边已经筛过一遍，
                # 这里是纵深防御（也挡住手搓包往这个本地口乱发的情况）。
                if not udpsync.is_heartbeat(packet):
                    continue
                self.recent.append((index, packet))
                added += 1
            # ★ 这一发里一份新的都没有就**什么都不发** —— 照旧发的话等于
            #   把上一批原样重播一遍，纯属浪费上行（服务端那边会当成过期丢掉）。
            if not added:
                return
            keep = self.redundancy + 1
            if len(self.recent) > keep:
                del self.recent[0:len(self.recent) - keep]
            payload = udpsync.build_data(list(self.recent))
        self._to_remote(payload)

    def _to_remote(self, payload):
        up = self._up                       # ★ 一次读出一对，别和换路由的线程撞上
        if up is None:
            return
        try:
            up[0].sendto(payload, up[1])
            self.sent += 1
        except OSError:
            # 发不出去就发不出去 —— TCP 那份照常在跑，玩家察觉不到。
            pass

    def _send_hello(self):
        if not self.ticket:
            return
        self.last_hello_at = time.monotonic()
        if not self.first_hello_at:
            self.first_hello_at = self.last_hello_at
        flags = udpsync.HELLO_FLAG_DOWNLINK if self.downlink else 0
        try:
            self._to_remote(udpsync.build_hello(self.ticket, flags))
        except ValueError:
            pass

    def _inject(self, packet):
        """把一份位置数据投进游戏自己的 UDP 口（`127.0.0.1:7788`）。

        收方入口 `0x407869` 和 `0x040f` 走的是**同一个函数**（§149），
        所以从这里进去和从游戏服连接进去，客户端处理起来一个字节的差别都没有。
        """
        if self.local is None:
            return
        try:
            self.local.sendto(packet,
                              (LISTEN_HOST, server_config.CLIENT_UDP_PORT))
            self.injected += 1
        except OSError:
            pass

    # -- 服务器 -> 我们 -> 游戏 ---------------------------------------------
    def _pump_remote(self, sock):
        """一条上游 socket 的收包循环（本机 / 远程各一条，X_Mod D58）。"""
        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(udpsync.MAX_DATAGRAM * 2)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                if sock.fileno() < 0:
                    # 这条上游已经被撤掉并关闭（代理撤了关联，`_on_upstream_dead`）——
                    # 不是下面那种一次性的错，再收只会在关掉的 socket 上空转。
                    break
                # Windows 上对端没监听时会以 WSAECONNRESET 的形式报到**下一次**
                # recvfrom 上，UDP 上这完全正常，继续收。
                continue
            if self.remote is not sock:
                # ★ 不是这一轮的上游 —— 玩家换了服务器（本机 ↔ 远程），这是上一条
                #   迟到的回包（ACK / 下行）。认了它会把「上一轮的确认」当成这一轮的，
                #   或者把上一台服务器的位置数据投进游戏。
                continue
            self.received += 1
            self.replies += 1
            try:
                self._on_remote_datagram(data)
            except Exception as error:      # noqa: BLE001
                vlog(f"位置UDP  处理服务器数据报出错（忽略）: {error!r}")

    def _on_remote_datagram(self, data):
        try:
            kind, _ = udpsync.parse_header(data)
        except udpsync.ProtocolError:
            return
        if kind == udpsync.MSG_HELLO_ACK:
            result, note = udpsync.parse_hello_ack(data)
            if result == udpsync.ACK_OK:
                if not self.acked:
                    log("位置UDP  ✓ 服务器已认出这条 UDP 通道，位置数据开始走 UDP")
                self.acked = True
                # ★ 连通之后把「被拒」的账清掉：万一之后**又**被拒了
                #   （服务端重启、票据在服务端那边没了），那是一次真正的
                #   状态翻转，值得再打一行 —— 和「下行 已就绪 / 未就绪」
                #   那一对提示是同一个套路：**只在状态变了的时候说话**。
                self.refused_logged = False
                return
            self.acked = False
            if result == udpsync.ACK_NOT_LOGGED_IN:
                # ★ **不是失败，是时序**：`bshook` 一看到 `0x0100` 就发 HELLO，
                #   而游戏服那边还没把 `login_ticket` 写到连接上。
                #   服务端明说了「票据是真的，只是还没登进来」，那就安静等 ——
                #   **每一次登录都必然经过这个窗口**，报出来纯属吓人。
                #   ★ 判据是服务端给的**事件**，不是「跳过头几发」这种次数。
                return
            # 真被拒了（服务端重启过 / 票据过期 / 被顶号 / UDP 同步被关掉）。
            # 只在**状态翻转**的那一次说话：票据真过期时 `HELLO_RETRY_S`
            # 每 2 秒重试一发，逐发打就是 bug调查/udp验证 里那 45 行刷屏（§225）。
            if not self.refused_logged:
                self.refused_logged = True
                log(f"位置UDP  服务器没接受这条通道（{note or result}）；"
                    f"位置数据继续走 TCP（还会继续重试，通了会再打一行）")
            return
        if kind == udpsync.MSG_PONG:
            return
        if kind != udpsync.MSG_DATA:
            return
        try:
            chunks = udpsync.parse_data(data)
        except udpsync.ProtocolError:
            return
        # ★★ 闸门：**只准前进**。
        #   一个数据报里捎带了好几份（冗余），按索引升序逐个投；
        #   已经投过的（索引 <= 水位）一律丢掉。
        #   没有这一道，网络乱序或冗余补发会把别人的角色**拉回旧位置** ——
        #   位置心跳没有任何可判新旧的原版字段（头 `+8` 的序列号对心跳恒为 0），
        #   客户端自己拦不住，只能在这儿拦。
        for index, packet in sorted(chunks, key=lambda item: item[0]):
            if index <= self.downlink_high_water:
                # ★ 冗余捎带天然会命中这里（同一份发 3 遍），所以**只数
                #   真乱序**：索引比水位小得多的那种（V0.3 §154）。
                #   `redundancy + 1` 是一批里捎带几份，超出它就不是冗余了。
                if index < self.downlink_high_water - self.redundancy:
                    self.reordered += 1
                    if not self.warned_reorder:
                        self.warned_reorder = True
                        log(f"位置UDP  ⚠ 收到乱序的位置包（索引 {index} < 水位 "
                            f"{self.downlink_high_water}），已丢弃 —— 这条闸拦住的"
                            f"正是「角色被拉回旧位置」。之后不再逐发提示")
                continue
            if not udpsync.is_heartbeat(packet):
                continue                    # 铁律 1：只有位置能走这条路
            self.downlink_high_water = index
            self._inject(packet)

    def _retry_hello(self):
        """HELLO 还没被确认，再发一发（`HELLO_RETRY_S` 到点）。

        这一轮的上游若没了 —— 代理撤掉了 UDP 关联、或上次根本没建起来 —— 先重建再发
        （X_Mod D71）。重建放在这儿而不是 `_send_hello` 里：收 HELLO 那条线程刚在
        `_use_route` 里建失败，紧接着的 `_send_hello` 不该再阻塞一轮。
        ★ 建上游会阻塞这条线程（经代理最多几个 CONNECT_TIMEOUT）：只在「这一轮没上游」
          时发生，那时也没有东西要保活；阻塞期间本机那条上游照常收发。
        """
        if self._started and self._up is None:
            self._use_route(self.route_local)
        self._send_hello()

    def _pump_timer(self):
        """重发 HELLO / 保活 / 一次性的「这条路好像不通」提示。"""
        while not self._stop.wait(0.5):
            now = time.monotonic()
            if self.ticket and not self.acked and now - self.last_hello_at >= HELLO_RETRY_S:
                self._retry_hello()
            if self.acked and now - self.last_keepalive_at >= KEEPALIVE_S:
                self.last_keepalive_at = now
                self._to_remote(udpsync.build_ping(udpsync.MSG_PING, 0))
            if self._quiet_warning_due(now):
                self.warned_quiet = True
                up = self._up
                where = (self._upstream_desc(up) if up is not None
                         else f"UDP {self.target_port}")
                via_proxy = up is not None and isinstance(up[0], _Socks5UdpUpstream)
                log(f"位置UDP  ⚠ {UDP_QUIET_WARN_S:.0f} 秒没等到服务器回应"
                    f"（「{self._route_name()}」{where}）—— "
                    f"多半是服务器没放行这个 UDP 口，或者服务端是旧版"
                    f"{'，走代理时也可能是代理的出口不转 UDP' if via_proxy else ''}。"
                    f"**位置数据继续走 TCP，游戏一切正常**")

    def _quiet_warning_due(self, now):
        """「这条路好像不通」该不该提示。★ 三条**都**要成立（§225 第六节）：

        1. 还没提示过、有票据、还没被确认；
        2. 从**本条游戏连接的第一发 HELLO** 起算够 `UDP_QUIET_WARN_S` 了 ——
           从中继进程启动起算的话，中继随启动脚本先起来、玩家还要点开游戏
           输账号，等游戏真连上时早就过了 20 秒，这句吓人的话会在
           **游戏刚连上的 0.4 秒内**打出来，紧接着才是 `✓ 已认出`；
        3. **这条游戏连接一个回应都没收到**。服务端回了「认不出票据」说明
           路是通的、只是票据不对，那是 `_on_remote_datagram` 里那条日志的事，
           不该说成「没等到回应」。★ 判据用的是 `replies`（每条游戏连接清零）
           而不是 `received`（整个进程的累计值）。
        4. **这一轮真有上游在发**（X_Mod D58 / D71）：选「远程服务器」又走 HTTP 代理、
           或 SOCKS5 代理不给 UDP、或代理刚撤掉关联还没重建时，这条上游根本不在，
           HELLO 一发都没出去，谈不上「服务器没回应」—— 那种情况 `_open_proxied_upstream`
           / `_on_upstream_dead` 已经明说过「回退 TCP」了。

        抽成一个纯判据是为了能单测 —— `_pump_timer` 是个死循环。
        """
        return (not self.warned_quiet and bool(self.ticket) and not self.acked
                and bool(self.first_hello_at)
                and now - self.first_hello_at > UDP_QUIET_WARN_S
                and self.replies == 0
                and not (self._started and self._up is None))


def main():
    # `relay.out` / `relay.err` 和 `server.out` 一个毛病：启动脚本重定向出来的
    # 文件只增不减，mtime 永远是刚才 ⇒ 保留天数对它一天都不起作用。挂着不关的
    # 客户端能让它跑上好几天。同样交给 daylog 按天切（用户 2026-09-14）。
    # ★ 放在 `main()` 里而不是模块级：`relay` 被 test_online / test_latency /
    #   test_proxy 直接 import，单测里 stdout 必须原样不动。
    daylog.install(stem="relay", banner="中继启动")
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

    cfg, warnings = server_config.load(args.config)
    for warning in warnings:
        log(f"server.config: {warning}")
    target = args.target or cfg["server_address"]
    target = server_config.normalize_host(target)
    try:
        proxy = proxy_from_config(cfg)
    except ValueError as error:
        log(f"!! server.config 的代理配置无效：{error}")
        return 1
    log(f"联机服务器 = {server_config.http_host(target)}")
    if proxy is None:
        log("远程连接方式 = 直连（代理未启用）")
    else:
        auth = "，需要认证" if proxy.username else "，无需认证"
        log(f"远程连接方式 = {proxy.route}{auth}")

    try:
        start(target, proxy=proxy)
    except RuntimeError as error:
        log(f"!! {error}")
        return 1
    start_udp_sync(target, proxy=proxy, enabled=bool(cfg["udp_sync"]),
                   redundancy=cfg["udp_sync_redundancy"])

    # 客户端崩溃后自动上传诊断日志（V0.3商店）。★ 出站连接复用本文件的
    # `connect_remote`，所以 SOCKS5 / HTTP CONNECT 代理自动生效 ——
    # 崩溃包和游戏流量走的是同一条出口。
    global CRASH_WATCHER
    CRASH_WATCHER = crashwatch.CrashWatcher(
        host=target, port=cfg["server_register_port"],
        connect=lambda host, port: connect_remote(host, port, proxy),
        enabled=bool(cfg["crash_upload"]),
        max_bytes=cfg["crash_max_upload_mb"] * 1048576,
        keep_days=cfg["crash_keep_days"], log=log)
    CRASH_WATCHER.start()
    if cfg["crash_upload"]:
        log(f"崩溃上传 开着：客户端闪退时把崩溃现场传到 "
            f"{server_config.http_host(target)}:{cfg['server_register_port']}"
            f"（选「本机服务器」时不传；server.config 的 crash_upload = 0 可关掉）")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        log("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
