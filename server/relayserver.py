#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""relayserver.py —— **原版的 TCP 中继**（rcp 协议），里程碑 J.3 / 决策 D078。

客户端进房间后每 10 秒会对每个「别人坐着的座位」发一发 `0x0310 gcpStartTcpRelay`
要一条中继通道。游戏服回 `0x0210 gspJoinRelay`（地址 + 三个 int32 的
`RelayAuthData`），客户端就 `new RelayConnection` 连到那个地址，
之后战斗内的同步数据都从这条 TCP 走（`0x408619` 优先选它）。

线格式全部逐指令核对过，见 `.claude/FINDINGS.md` §156 / §157：

```text
帧      = RawPacket 10 字节头，和 27799 **逐字节相同**
加密    = SimpleCipher，两个方向 (0,1) / (5,3)，和 27799 相同
开场白  = **没有**。ServerConnection 才发明文版本号，RelayConnection 上来就 rcpRegister
```

opcode（`0x54bce1` 分发）：

| opcode | 方向 | 载荷 |
|---|---|---|
| `0` | 客户端 → 中继 | `rcpRegister`：12 字节 = `RelayAuthData` 三个 int32 |
| `1` | 客户端 → 中继 | `rcpRepPing`：0 字节 |
| `3` | 客户端 → 中继 | 数据：一个完整的 `UdpPacket` |
| `0` | 中继 → 客户端 | 数据：一个完整的 `UdpPacket` |
| `1` | 中继 → 客户端 | ping：0 字节 |
| `2` | 中继 → 客户端 | 「报一下身份」：0 字节，客户端收到会重发 opcode 0 |

## ★ 两条铁律（都是 §158 / §159 换来的）

1. **绝不主动关一条已注册的连接。** 客户端的 `RelayConnection::OnDisconnected`
   （`0x54be26`）里会调 `0x406191` —— 那是**发 `0x0203` 退出房间**，
   顺带把通道 A 的开关也清 0。中继一断，玩家就被自己的客户端踢出房间，
   连 `0x040e` 回退路径都一起没了。**断了比没有更糟。**
2. **一条游戏连接只回一次 `0x0210`。** 客户端收到就无条件 `new RelayConnection`
   并覆盖全局指针，旧对象既不释放也不关 socket；等它哪天收到 FD_CLOSE，
   `OnDisconnected` 照样触发，把**新**连接一起带走。去重责任 100% 在服务端。

   —— 所以中继连接绑的是**游戏连接**（一条 TCP 一辈子一个），
   房间是每次投递时现查的。换房间不需要重发 `0x0210`，也就不用拆连接。

## 投递

收到 opcode 3，把载荷发给**同房间的其他人**：

* 对方也接上中继了 → rcp opcode 0（原版路径）；
* 对方还没接上（中继连接是异步建的，进房那几秒里必然有这个窗口）
  → 退回 `0x040f` 走它的游戏服连接。

两条路送到客户端都进同一个入口 `0x407869`，而且客户端按序列号去重（§151），
所以「万一两条都送到」也是安全的。
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as server_config
from netlisten import create_listener
from simple import SimpleCipher

#: `RawPacket` 的魔数。和游戏服那层同一个字节（`0x5bb9e7` 写死的）。
MAGIC = 0xFF
#: 头长度。`0x55b9bc` 剥的就是这 10 个字节。
HEADER_SIZE = 10

#: 客户端 → 中继
RCP_REGISTER = 0
RCP_REP_PING = 1
RCP_DATA_UP = 3
#: 中继 → 客户端
RCP_DATA_DOWN = 0
RCP_PING = 1
RCP_WHO_ARE_YOU = 2

#: `rcpRegister` 的载荷长度：`RelayAuthData` 三个 int32（`0x54c453`）。
AUTH_SIZE = 12

#: 单帧载荷上限。头里的长度是 u16，所以协议上限就是 65535；
#: 真实的 `UdpPacket` 是几十字节，超出这个数量级说明流已经错位了。
MAX_PAYLOAD = 0xFFFF

#: 多久给每条已注册的连接发一发 ping（秒）。**只为留个活性记录** ——
#: 收不到回应也**不会**断开（铁律 1）。
PING_INTERVAL = 10.0

#: 签发出去还没被用掉的票据能活多久（秒）。客户端拿到 `0x0210` 之后
#: 一个 RTT 就连上来了，给足 60 秒纯属宽容。
TICKET_TTL = 60.0


def build_rcp(opcode, payload=b""):
    """一帧 rcp。**和 `gameserver.build_game` 是同一串字节**（§156）。

    两处各写一份是故意的：`relayserver` 不 import `gameserver`（那边要 import
    这边），而这个格式已经被 `0x5bb9e7` 钉死了，不会变。
    `test_relayserver.py` 里有一条用例把两者逐字节对住，防止哪天单边漂了。
    """
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"rcp 载荷 {len(payload)} 字节超过 u16 上限")
    return (bytes([MAGIC, 0]) + struct.pack("<HHHH", len(payload), 0, 0, opcode)
            + payload)


def take_rcp(buf):
    """从已解密的缓冲里取一帧，返回 ``(opcode, payload, 消费字节数)`` 或 ``None``。

    首字节不是 `0xff` 就抛 `ValueError` —— 那说明解密流错位了，
    继续往下读只会读出垃圾，早点报出来比默默乱转发强。
    """
    if len(buf) < HEADER_SIZE:
        if buf and buf[0] != MAGIC:
            raise ValueError(f"帧首字节 0x{buf[0]:02x} 不是 0x{MAGIC:02x}")
        return None
    if buf[0] != MAGIC:
        raise ValueError(f"帧首字节 0x{buf[0]:02x} 不是 0x{MAGIC:02x}")
    size = struct.unpack_from("<H", buf, 2)[0] + HEADER_SIZE
    if len(buf) < size:
        return None
    opcode = struct.unpack_from("<H", buf, 8)[0]
    return (opcode, bytes(buf[HEADER_SIZE:size]), size)


def parse_auth(payload):
    """`rcpRegister` 的载荷 -> 三个 int32。长度不够就抛 `ValueError`。"""
    if len(payload) < AUTH_SIZE:
        raise ValueError(f"rcpRegister 只有 {len(payload)} 字节，要 {AUTH_SIZE}")
    return struct.unpack_from("<iii", payload, 0)


def build_join_relay(host, port, auth):
    """`0x0210 gspJoinRelay` 的 18 字节载荷（§157）。

    `NetAddress` 的 `ip` 是**网络字节序的原始 32 位**（`Connect` 把它直接写进
    `sin_addr`，没有 `htonl`），`port` 是主机序（`Connect` 里才过 `htons`）。
    所以 `inet_aton` 的结果原样拼进去就对，不要再翻转。
    """
    a, b, c = auth
    return (socket.inet_aton(host) + struct.pack("<H", int(port))
            + struct.pack("<iii", int(a), int(b), int(c)))


class _Ticket:
    """一张签发出去、还没被 `rcpRegister` 兑换的票据。"""

    __slots__ = ("game_conn", "room_id", "seat_index", "issued_at")

    def __init__(self, game_conn, room_id, seat_index):
        self.game_conn = game_conn
        self.room_id = int(room_id)
        self.seat_index = int(seat_index)
        self.issued_at = time.time()


class RelayConn:
    """一条中继 TCP 连接。注册成功后就绑定到一条**游戏连接**上，直到断开。"""

    def __init__(self, server, sock, addr):
        self.server = server
        self.sock = sock
        self.addr = addr
        self.cin = SimpleCipher.client_to_server()
        self.cout = SimpleCipher.server_to_client()
        self.buf = bytearray()
        self.send_lock = threading.Lock()
        self.game_conn = None       #: 注册成功后指向 `gameserver.Conn`
        self.auth = None            #: 兑换掉的那三个 int32（日志用）
        self.frames_in = 0
        self.frames_out = 0
        self.data_in = 0
        self.data_out = 0
        self.pings_out = 0
        self.pongs_in = 0
        self.opened_at = time.time()
        self.last_ping_at = self.opened_at
        self.closed = False

    # -- 基本信息 -----------------------------------------------------------
    def peer(self):
        return f"{self.addr[0]}:{self.addr[1]}"

    def who(self):
        if self.game_conn is None:
            return f"{self.peer()}（未注册）"
        name = getattr(self.game_conn, "account_name", "") or "?"
        return f"{name}@{self.peer()}"

    def log(self, msg):
        self.server.log(f"[{self.who()}] {msg}")

    # -- 发送 ---------------------------------------------------------------
    def send_frame(self, opcode, payload=b""):
        """发一帧。**失败只记录不抛** —— 上层绝不能因为发送失败去关连接（铁律 1）。"""
        if self.closed:
            return False
        frame = build_rcp(opcode, payload)
        try:
            # 流密码是有状态的，加密和 sendall 必须在同一把锁里，
            # 否则两个线程各加密一半、交错发出去，客户端整条流就废了。
            with self.send_lock:
                self.sock.sendall(self.cout.encrypt(frame))
        except OSError as error:
            self.log(f"中继发送失败（{error!r}）")
            return False
        self.frames_out += 1
        return True

    def send_data(self, udp_packet):
        """把一份同步数据发给这个客户端（rcp opcode 0）。"""
        if self.send_frame(RCP_DATA_DOWN, udp_packet):
            self.data_out += 1
            return 1
        return 0

    # -- 收 -----------------------------------------------------------------
    def run(self):
        self.server.log(f"+ 中继连接 {self.peer()}")
        self.sock.settimeout(1.0)
        try:
            while not self.server.stopping:
                try:
                    data = self.sock.recv(8192)
                except socket.timeout:
                    self.tick()
                    continue
                if not data:
                    break
                self.feed(data)
        except ConnectionResetError:
            pass
        except ValueError as error:
            # 解密流错位。**不算致命**，但继续读下去没意义了。
            self.log(f"!! 帧解析失败：{error}")
        except OSError:
            pass
        finally:
            self.close()

    def tick(self):
        """每秒钟一次的心跳窗口。已注册的连接按 `PING_INTERVAL` 发 ping。"""
        if self.game_conn is None:
            return
        now = time.time()
        if now - self.last_ping_at < PING_INTERVAL:
            return
        self.last_ping_at = now
        if self.send_frame(RCP_PING):
            self.pings_out += 1

    def feed(self, data):
        self.buf += self.cin.decrypt(data)
        while True:
            got = take_rcp(self.buf)
            if got is None:
                break
            opcode, payload, size = got
            del self.buf[:size]
            self.frames_in += 1
            self.on_frame(opcode, payload)

    def on_frame(self, opcode, payload):
        if opcode == RCP_REGISTER:
            self.on_register(payload)
        elif opcode == RCP_REP_PING:
            self.pongs_in += 1
        elif opcode == RCP_DATA_UP:
            self.on_data(payload)
        else:
            # 客户端只会发 0 / 1 / 3。别的号说明我们读错了流，或者对面不是
            # 真客户端。记一行就够，**不要关连接**（铁律 1）。
            self.log(f"!! 不认识的 rcp opcode {opcode}（{len(payload)} 字节），已忽略")

    def on_register(self, payload):
        try:
            auth = parse_auth(payload)
        except ValueError as error:
            self.log(f"!! rcpRegister 解析失败：{error}")
            return
        ticket = self.server.redeem(auth)
        if ticket is None:
            # 票据不认识：可能是过期的，也可能是别人在乱连。既然不知道它是谁，
            # 就再问一次身份（原版的 opcode 2 就是干这个的）。
            self.log(f"!! rcpRegister 票据不认识 {auth}；回 opcode 2 再问一次")
            self.send_frame(RCP_WHO_ARE_YOU)
            return
        self.auth = auth
        self.game_conn = ticket.game_conn
        self.server.bind(self)
        self.log(f"✓ 注册成功（签发时房间 #{ticket.room_id} 座位 {ticket.seat_index}）")

    def on_data(self, payload):
        if self.game_conn is None:
            # 还没报身份就送数据。原版中继的 opcode 2 正是为这一刻存在的。
            self.log("收到数据但还没注册；回 opcode 2 要身份")
            self.send_frame(RCP_WHO_ARE_YOU)
            return
        self.data_in += 1
        self.server.deliver(self.game_conn, payload, via=self)

    # -- 收尾 ---------------------------------------------------------------
    def close(self):
        if self.closed:
            return
        self.closed = True
        self.server.unbind(self)
        try:
            self.sock.close()
        except OSError:
            pass
        self.server.log(
            f"- 中继连接结束 [{self.who()}] 在线 {time.time() - self.opened_at:.1f} 秒"
            f"（帧 收 {self.frames_in} / 发 {self.frames_out}；"
            f"数据 收 {self.data_in} / 发 {self.data_out}；"
            f"ping {self.pings_out} 回 {self.pongs_in}）")


class RelayServer:
    """rcp 中继。一个监听器 + 一张「游戏连接 -> 中继连接」的表。

    和 `lobby.py` 一样**不 import `gameserver`** —— 反过来才对。
    要往游戏连接上回退投递、要查房间成员，都靠构造时注入的两个回调，
    这样这个模块可以单独测（不需要真的起游戏服）。
    """

    def __init__(self, *, members_of=None, fallback=None, logger=None,
                 port=None):
        #: `members_of(game_conn) -> [同房间的其他游戏连接]`
        self._members_of = members_of or (lambda conn: [])
        #: `fallback(game_conn, udp_packet) -> None`，走 `0x040f`
        self._fallback = fallback
        self._logger = logger
        self.port = int(port if port is not None
                        else server_config.PEER_RELAY_PORT)
        self._lock = threading.RLock()
        self._tickets = {}          #: nonce -> _Ticket
        self._issued = {}           #: game_conn -> nonce（§159 的去重表）
        self._conns = {}            #: game_conn -> RelayConn
        self._nonce = 0
        self.listener = None
        self.stopping = False
        #: 统计，给控制通道的 `status` 用。
        self.registered_total = 0
        self.delivered_relay = 0
        self.delivered_fallback = 0

    # -- 日志 ---------------------------------------------------------------
    def log(self, msg):
        if self._logger is not None:
            self._logger(msg)

    # -- 票据 ---------------------------------------------------------------
    def issue(self, game_conn, room_id, seat_index):
        """签发一张票据，返回 `0x0210` 里要带的三个 int32。

        **同一条游戏连接只签一次** —— 已经签过就返回 ``None``，
        调用方据此**不重发 `0x0210`**（§159：重发是定时炸弹）。
        """
        with self._lock:
            if game_conn in self._issued:
                return None
            self._sweep_unlocked()
            self._nonce += 1
            # 三个 int32 的语义**完全由我们定**（客户端一个字节都不解释，§157）。
            # 房间号和座位只为日志好看；真正认人的是第三个 nonce。
            nonce = self._nonce
            auth = (int(room_id), int(seat_index), nonce)
            self._tickets[nonce] = _Ticket(game_conn, room_id, seat_index)
            self._issued[game_conn] = nonce
            return auth

    def has_issued(self, game_conn):
        with self._lock:
            return game_conn in self._issued

    def redeem(self, auth):
        """`rcpRegister` 拿三个 int32 来兑换。认不出返回 ``None``。"""
        with self._lock:
            self._sweep_unlocked()
            ticket = self._tickets.pop(int(auth[2]), None)
            if ticket is not None:
                self.registered_total += 1
            return ticket

    def _sweep_unlocked(self):
        """清掉签发出去太久还没人来兑的票据。"""
        deadline = time.time() - TICKET_TTL
        stale = [n for n, t in self._tickets.items() if t.issued_at < deadline]
        for nonce in stale:
            ticket = self._tickets.pop(nonce)
            # 票据过期不代表这条游戏连接以后不能再要中继了，把去重表也放开。
            if self._issued.get(ticket.game_conn) == nonce:
                self._issued.pop(ticket.game_conn, None)

    # -- 连接表 -------------------------------------------------------------
    def bind(self, relay_conn):
        with self._lock:
            old = self._conns.get(relay_conn.game_conn)
            self._conns[relay_conn.game_conn] = relay_conn
        if old is not None and old is not relay_conn:
            # 同一条游戏连接上来了第二条中继连接。不该发生（§159 的去重表挡着），
            # 真发生了就让旧的那条自己收尾，别去关它的 socket（铁律 1）。
            old.log("!! 被同一个玩家的新中继连接顶替")

    def unbind(self, relay_conn):
        with self._lock:
            if self._conns.get(relay_conn.game_conn) is relay_conn:
                self._conns.pop(relay_conn.game_conn, None)

    def conn_for(self, game_conn):
        with self._lock:
            relay = self._conns.get(game_conn)
        return None if relay is None or relay.closed else relay

    def forget(self, game_conn):
        """游戏连接没了：作废它的票据、松开去重表。

        **不关中继 socket** —— 游戏连接一断，客户端那条中继连接自己也会走掉。
        真要拆连接请用 `gameserver` 那边的 `0x0211`（§157 唯一安全的方式）。
        """
        with self._lock:
            nonce = self._issued.pop(game_conn, None)
            if nonce is not None:
                self._tickets.pop(nonce, None)
            relay = self._conns.pop(game_conn, None)
        return relay

    # -- 投递 ---------------------------------------------------------------
    def deliver(self, sender_game_conn, udp_packet, via=None):
        """把一份同步数据发给同房间的其他人。返回送到了几个人。

        对方接上中继了就走中继（原版路径），没接上就退回 `0x040f`
        —— 中继连接是异步建的，进房那几秒里必然有一个「有人还没接上」的窗口，
        那几秒里不能让同步断掉。
        """
        sent = 0
        for member in self._members_of(sender_game_conn):
            if member is sender_game_conn:
                continue
            relay = self.conn_for(member)
            if relay is not None:
                sent += relay.send_data(udp_packet)
                self.delivered_relay += 1
            elif self._fallback is not None:
                try:
                    self._fallback(member, udp_packet)
                except OSError:
                    continue
                sent += 1
                self.delivered_fallback += 1
        return sent

    # -- 监听 ---------------------------------------------------------------
    def serve(self, host="::", ready=None):
        self.listener = create_listener(host, self.port)
        if ready is not None:
            ready.set()
        while not self.stopping:
            try:
                sock, addr = self.listener.accept()
            except OSError:
                if self.stopping:
                    break
                raise
            conn = RelayConn(self, sock, addr)
            threading.Thread(target=conn.run, daemon=True,
                             name=f"relay-{addr[0]}:{addr[1]}").start()

    def stop(self):
        self.stopping = True
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass

    def status(self):
        """一行给控制通道 / 日志用的现状。"""
        with self._lock:
            live = len(self._conns)
            pending = len(self._tickets)
        return (f"中继：在线 {live} 条，待兑票据 {pending} 张，"
                f"累计注册 {self.registered_total} 次；"
                f"投递 中继 {self.delivered_relay} / 回退 {self.delivered_fallback}")
