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
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as server_config
from netlisten import create_listener, tune_stream

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

#: 中继只服务本机的客户端，所以**只绑 127.0.0.1**。
#: （服务端那三个口才需要对外开，见 D063。）
LISTEN_HOST = "127.0.0.1"

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


def _socks5_connect(sock, target_host, target_port, proxy):
    """在已经连到代理的 socket 上完成 SOCKS5 协商和 CONNECT。"""
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

    sock.sendall(b"\x05\x01\x00" + _socks5_target(target_host, target_port))
    version, status, reserved, atyp = _recv_exact(sock, 4)
    if version != 5 or reserved != 0:
        raise ProxyError("SOCKS5 CONNECT 应答格式错误")
    if status != 0:
        reasons = {
            1: "代理服务器内部错误",
            2: "代理规则不允许此连接",
            3: "目标网络不可达",
            4: "目标主机不可达",
            5: "目标拒绝连接",
            6: "连接 TTL 超时",
            7: "代理不支持 CONNECT 命令",
            8: "代理不支持目标地址类型",
        }
        raise ProxyError(f"SOCKS5 CONNECT 失败: {reasons.get(status, f'状态 0x{status:02x}')}")

    # 吃掉代理返回的 BND.ADDR / BND.PORT；值本身对 TCP 隧道没有用。
    if atyp == 1:
        _recv_exact(sock, 4)
    elif atyp == 4:
        _recv_exact(sock, 16)
    elif atyp == 3:
        length = _recv_exact(sock, 1)[0]
        _recv_exact(sock, length)
    else:
        raise ProxyError(f"SOCKS5 CONNECT 应答地址类型未知: 0x{atyp:02x}")
    _recv_exact(sock, 2)


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


def handle(client, addr, target_host, target_port, label, proxy=None):
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
                         args=(client, addr, target_host, target_port, label, proxy),
                         daemon=True).start()


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
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        log("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
