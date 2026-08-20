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
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as server_config
import udpsync
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


def start_udp_sync(target_host, proxy=None, enabled=True, redundancy=2):
    """把位置数据的 UDP 中继拉起来。返回 `UdpSyncRelay | None`。

    **任何一种起不来的情况都只打一行日志、返回 `None`** —— 这条通道从头到尾
    都是「TCP 之外多走一份」，没有它游戏完全正常。
    """
    if not enabled:
        log("位置UDP  已关闭（server.config 的 udp_sync = 0）；位置数据走 TCP")
        return None
    if proxy is not None:
        # SOCKS5 的 UDP ASSOCIATE 要另开通道且未必被代理支持，HTTP CONNECT
        # 根本转不了 UDP。做半套不如不做 —— 走代理就保持今天的行为。
        log("位置UDP  已启用代理，位置数据回退 TCP（代理转不了 UDP）")
        return None
    relay = UdpSyncRelay(target_host, redundancy=redundancy)
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
#: 玩家进游戏时服务端还没起来 —— 都靠它自己接回来。
HELLO_RETRY_S = 2.0

#: 确认之后多久发一发保活（秒）。家用路由器的 UDP 映射常见 30~60 秒超时，
#: 10 秒足够撑住；战斗中本来就有 8 Hz 的数据，保活只在大厅里真的起作用。
KEEPALIVE_S = 10.0

#: 收不到任何回应多久算这条路不通（秒）。到点只打一行日志 —— **不做任何降级**，
#: 因为 TCP 那份从来没停过，UDP 不通对玩家就是「和以前一样」。
UDP_QUIET_WARN_S = 20.0


class UdpSyncRelay:
    """位置数据的本机 UDP 中继（`server/udpsync.py` 是它的对端）。

    ```text
    BigShot.exe --(bshook 镜像)--> 127.0.0.1:27809/udp ─┐
                                                        ├─ 本类 ─> <server>:27799/udp
    BigShot.exe:7788/udp <--(下行注入，阶段 2)-----------┘
    ```

    ★ **它不是「把 TCP 换成 UDP」，是在 TCP 之外多走一份。** 客户端那份
    `0x040e` 照发不误，所以这条 UDP 通道**整条不通也没有任何后果** ——
    服务端按索引去重，UDP 没到就用 TCP 那份。

    ★ **代理开着时整条通道禁用**：SOCKS5 的 UDP ASSOCIATE 要另开一条通道、
    还得代理服务器支持，HTTP CONNECT 根本转不了 UDP。与其做半套不如不做 ——
    走代理的玩家保持今天的行为。
    """

    def __init__(self, target_host, target_port=None, local_port=None,
                 redundancy=2):
        self.target_host = target_host
        self.target_port = target_port or server_config.UDP_SYNC_PORT
        self.local_port = local_port or server_config.RELAY_UDP_SYNC_PORT
        self.redundancy = max(0, int(redundancy))
        #: 游戏那个「收位置数据的 UDP 口」bind 成功了没有。
        #: ★ 这个值**不是我们判的，是 `bshook` 告诉我们的** —— 它在游戏进程里
        #: 钩住 `bind`，亲眼看着那一次 bind 返回 0 才置位。所以它是权威的，
        #: 不存在「口被别的程序占着而我们以为是游戏」那种假阳性。
        self.downlink = False
        self.local = None
        self.remote = None
        self.remote_addr = None
        self.hook_addr = None
        self.ticket = ""
        self.acked = False
        self.started_at = 0.0
        self.last_hello_at = 0.0
        self.last_keepalive_at = 0.0
        self.warned_quiet = False
        self.sent = 0
        self.received = 0
        self.injected = 0
        #: 最近几份（含当前）：`[(索引, UdpPacket), …]`，冗余捎带用。
        self.recent = []
        #: 下行闸门：只准前进。**没有它，网络乱序或冗余补发会把角色拉回旧位置。**
        self.downlink_high_water = -1
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # -- 建 socket ----------------------------------------------------------
    def _resolve(self):
        infos = socket.getaddrinfo(self.target_host, self.target_port,
                                   type=socket.SOCK_DGRAM)
        family, _, _, _, sockaddr = infos[0]
        return family, sockaddr

    def start(self):
        """建好两条 socket 并把收发线程拉起来。失败时返回 `False`（不抛）。"""
        try:
            family, self.remote_addr = self._resolve()
        except OSError as error:
            log(f"位置UDP  ✗ 解析不了 {self.target_host}: {error}；"
                f"位置数据继续走 TCP")
            return False
        try:
            self.remote = socket.socket(family, socket.SOCK_DGRAM)
            self.remote.settimeout(0.5)
            self.local = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.local.bind((LISTEN_HOST, self.local_port))
            self.local.settimeout(0.5)
        except OSError as error:
            log(f"位置UDP  ✗ 本机 {LISTEN_HOST}:{self.local_port}/udp 起不来"
                f"（{error}）；位置数据继续走 TCP")
            self.close()
            return False
        self.started_at = time.monotonic()
        for target, name in ((self._pump_local, "udpsync-local"),
                             (self._pump_remote, "udpsync-remote"),
                             (self._pump_timer, "udpsync-timer")):
            threading.Thread(target=target, daemon=True, name=name).start()
        log(f"位置UDP  {LISTEN_HOST}:{self.local_port}/udp → "
            f"{server_config.http_host(self.target_host)}:{self.target_port}/udp"
            f"（冗余 {self.redundancy} 份；只走位置数据，其余照旧 TCP）")
        return True

    def close(self):
        self._stop.set()
        for sock in (self.local, self.remote):
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
            # 两种都原样把票据 + 标志位转告服务端。
            try:
                ticket, flags = udpsync.parse_hello_full(data)
            except udpsync.ProtocolError:
                return
            downlink = bool(flags & udpsync.HELLO_FLAG_DOWNLINK)
            with self._lock:
                # ★ 「新的一条游戏连接」不能靠票据变没变来判 —— 断线重连时
                #   客户端会**原样重放同一张票据**（§171）。判据是标志位从
                #   「已绑」回到「没绑」：`bshook` 每发一次登录包就把它清一次。
                restart = (ticket != self.ticket) or (self.downlink and not downlink)
                self.ticket = ticket
                if restart:
                    # 索引、水位、确认状态全部从头来 —— 服务端那边是一条新的
                    # `Conn`，计数器同样从 0 起，两边这才对得上。
                    self.recent.clear()
                    self.acked = False
                    self.downlink_high_water = -1
                changed = (downlink != self.downlink)
                self.downlink = downlink
            if changed:
                log(f"位置UDP  下行 {'已就绪' if downlink else '未就绪'}"
                    f"（游戏的 UDP {server_config.CLIENT_UDP_PORT} "
                    f"{'已 bind' if downlink else '还没 bind'}）")
            self._send_hello()
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
        if self.remote is None:
            return
        try:
            self.remote.sendto(payload, self.remote_addr)
            self.sent += 1
        except OSError:
            # 发不出去就发不出去 —— TCP 那份照常在跑，玩家察觉不到。
            pass

    def _send_hello(self):
        if not self.ticket:
            return
        self.last_hello_at = time.monotonic()
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
    def _pump_remote(self):
        while not self._stop.is_set():
            try:
                data, _ = self.remote.recvfrom(udpsync.MAX_DATAGRAM * 2)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                # Windows 上对端没监听时会以 WSAECONNRESET 的形式报到**下一次**
                # recvfrom 上，UDP 上这完全正常，继续收。
                continue
            self.received += 1
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
            else:
                self.acked = False
                log(f"位置UDP  服务器没接受这条通道（{note or result}）；"
                    f"位置数据继续走 TCP")
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
                continue
            if not udpsync.is_heartbeat(packet):
                continue                    # 铁律 1：只有位置能走这条路
            self.downlink_high_water = index
            self._inject(packet)

    def _pump_timer(self):
        """重发 HELLO / 保活 / 一次性的「这条路好像不通」提示。"""
        while not self._stop.wait(0.5):
            now = time.monotonic()
            if self.ticket and not self.acked and now - self.last_hello_at >= HELLO_RETRY_S:
                self._send_hello()
            if self.acked and now - self.last_keepalive_at >= KEEPALIVE_S:
                self.last_keepalive_at = now
                self._to_remote(udpsync.build_ping(udpsync.MSG_PING, 0))
            if (not self.warned_quiet and self.ticket and not self.acked
                    and now - self.started_at > UDP_QUIET_WARN_S):
                self.warned_quiet = True
                log(f"位置UDP  ⚠ {UDP_QUIET_WARN_S:.0f} 秒没等到服务器回应 —— "
                    f"多半是服务器没放行 UDP {self.target_port}，"
                    f"或者服务端是旧版。**位置数据继续走 TCP，游戏一切正常**")


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
    start_udp_sync(target, proxy=proxy, enabled=bool(cfg["udp_sync"]),
                   redundancy=cfg["udp_sync_redundancy"])
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        log("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
