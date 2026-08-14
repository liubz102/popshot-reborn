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
import eventlog
from netlisten import create_listener, tune_stream
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

#: 多久给每条已注册的连接发一发 ping（秒）。
#:
#: ★ 除了「活性记录」，它现在还是**唯一一把量真实延迟的尺子**：这条连接就是
#: 战斗数据走的那条（客户端 → 本机中继 → 这里），所以 ping 的往返时间就是玩家
#: 真正感受到的那个延迟。10 秒一发采不出统计量，改成 1 秒（一帧 10 字节）。
#:
#: ⚠ **收不到回应也绝不断开**（铁律 1：中继一断客户端会自己退出房间，§158）。
PING_INTERVAL = 1.0

#: 一发 ping 等多久还没回就当它丢了。rcp 的 ping 载荷是空的、没有 id 可配对，
#: 所以**同一时刻只允许一发在飞** —— 这样每个 pong 归属哪一发都是确定的，
#: 量出来的数字才可信。超时只是把「在飞」标记清掉，**不动连接**。
PING_TIMEOUT = 5.0

#: 多久往 `[online]` 汇总一次 RTT（秒）。逐发打会把日志淹掉。
RTT_REPORT_INTERVAL = 30.0

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


class RttStats:
    """一串 RTT 样本的统计量（毫秒）。

    做成独立的小类是为了**能单独测**：喂假的毫秒数进去就能验 min/avg/p95，
    不用真的起 socket。

    `p95` 算在**最近 `cap` 个**样本上（1 Hz 下 4096 个 ≈ 68 分钟，一局绰绰有余）；
    `count / min / max / avg` 是全量的，不受 `cap` 影响。
    """

    __slots__ = ("cap", "count", "total", "lo", "hi", "recent")

    def __init__(self, cap=4096):
        self.cap = int(cap)
        self.reset()

    def reset(self):
        self.count = 0
        self.total = 0.0
        self.lo = None
        self.hi = None
        self.recent = []

    def add(self, ms):
        ms = float(ms)
        self.count += 1
        self.total += ms
        if self.lo is None or ms < self.lo:
            self.lo = ms
        if self.hi is None or ms > self.hi:
            self.hi = ms
        self.recent.append(ms)
        if len(self.recent) > self.cap:
            # 一次砍掉一半，别每来一个样本就 pop(0)（那是 O(n)）。
            del self.recent[:len(self.recent) - self.cap]

    @property
    def avg(self):
        return None if not self.count else self.total / self.count

    @property
    def p95(self):
        """第 95 百分位（最近邻取法，样本少时退化成最大值）。"""
        if not self.recent:
            return None
        ordered = sorted(self.recent)
        index = int(round(0.95 * (len(ordered) - 1)))
        return ordered[index]

    def summary(self):
        """给日志的一行。没有样本时返回 ``None``（调用方据此不打这一行）。"""
        if not self.count:
            return None
        return (f"样本={self.count} min={self.lo:.1f}ms "
                f"avg={self.avg:.1f}ms p95={self.p95:.1f}ms max={self.hi:.1f}ms")


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
        self.pings_lost = 0
        self.opened_at = time.time()
        self.last_ping_at = self.opened_at
        #: 正在飞的那一发 ping 的发出时刻；`None` = 现在没有在飞的。
        self.ping_sent_at = None
        #: 本汇总窗口的 RTT（每 `RTT_REPORT_INTERVAL` 打一行然后清零）。
        self.rtt_window = RttStats()
        #: 整条连接的 RTT（断开时打一行）。
        self.rtt_total = RttStats()
        self.last_rtt_report_at = self.opened_at
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
                # ★ 战斗中数据是连续的，上面那个 `recv` **永远不会超时** ——
                #   不在这里也调一次的话，一局打下来一发 ping 都不会发出去，
                #   偏偏那正是最需要量延迟的时候（FINDINGS §182）。
                #   `tick()` 自己按 PING_INTERVAL 节流，多调不会多发。
                self.tick()
        except ConnectionResetError:
            pass
        except ValueError as error:
            # 解密流错位。**不算致命**，但继续读下去没意义了。
            self.log(f"!! 帧解析失败：{error}")
        except OSError:
            pass
        finally:
            self.close()

    def tick(self, now=None):
        """心跳窗口：按 `PING_INTERVAL` 发 ping，并按 `RTT_REPORT_INTERVAL` 汇总。

        `run()` 在**收到数据之后**和**recv 超时时**各调一次，所以战斗中也照走
        （见 `run()` 里的注释）。`now` 只给测试注入用。

        ⚠ 全程**不关连接** —— ping 丢光了也只是把计数加一（铁律 1 / §158）。
        """
        if self.game_conn is None:
            return
        now = time.time() if now is None else now
        self.report_rtt(now)
        if self.ping_sent_at is not None:
            # 上一发还在飞。等它回来或超时，**期间不再发第二发** ——
            # rcp 的 ping 没有 id，两发同时在飞就分不清 pong 是谁的了。
            if now - self.ping_sent_at >= PING_TIMEOUT:
                self.ping_sent_at = None
                self.pings_lost += 1
            return
        if now - self.last_ping_at < PING_INTERVAL:
            return
        self.last_ping_at = now
        self.ping_sent_at = now
        if self.send_frame(RCP_PING):
            self.pings_out += 1
        else:
            self.ping_sent_at = None

    def on_pong(self, now=None):
        """收到 `rcpRepPing`：把这一发的往返时间记下来。"""
        self.pongs_in += 1
        sent, self.ping_sent_at = self.ping_sent_at, None
        if sent is None:
            # 没有在飞的 ping 却收到 pong（超时之后才回来的那一发）。
            # 算不出可信的 RTT，丢掉这个样本比记一个错的强。
            return
        now = time.time() if now is None else now
        rtt_ms = max(0.0, (now - sent) * 1000.0)
        self.rtt_window.add(rtt_ms)
        self.rtt_total.add(rtt_ms)

    def report_rtt(self, now=None, force=False):
        """够 `RTT_REPORT_INTERVAL` 就往 `[online-debug]` 打一行汇总，然后清窗口。

        走 `eventlog` 而不是 `self.log()`：它是**跨连接**的一条时间线，
        和逐包 dump 混在一起就没法一眼看完。

        ★ **调试级**（D112）：每 30 秒一行 × 每条中继连接，是「频率由定时器
        决定」的遥测，和转发耗时同一档。要量延迟就用 `start-debug.bat`
        —— §187 那一轮本来也是这么跑的。
        """
        now = time.time() if now is None else now
        if not force and now - self.last_rtt_report_at < RTT_REPORT_INTERVAL:
            return
        self.last_rtt_report_at = now
        summary = self.rtt_window.summary()
        if summary is None:
            return
        lost = f" 丢={self.pings_lost}" if self.pings_lost else ""
        eventlog.debug(f"中继 RTT [{self.who()}] {summary}{lost}")
        self.rtt_window.reset()

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
            self.on_pong()
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
        # 窗口里没打完的那些样本也要有个去处，不然短连接一个数字都留不下。
        self.report_rtt(force=True)
        rtt = self.rtt_total.summary()
        self.server.log(
            f"- 中继连接结束 [{self.who()}] 在线 {time.time() - self.opened_at:.1f} 秒"
            f"（帧 收 {self.frames_in} / 发 {self.frames_out}；"
            f"数据 收 {self.data_in} / 发 {self.data_out}；"
            f"ping {self.pings_out} 回 {self.pongs_in} 丢 {self.pings_lost}）")
        if rtt is not None:
            eventlog.debug(f"中继 RTT 汇总 [{self.who()}] {rtt}")


class RelayServer:
    """rcp 中继。一个监听器 + 一张「游戏连接 -> 中继连接」的表。

    和 `lobby.py` 一样**不 import `gameserver`** —— 反过来才对。
    要往游戏连接上回退投递、要查房间成员，都靠构造时注入的两个回调，
    这样这个模块可以单独测（不需要真的起游戏服）。
    """

    def __init__(self, *, members_of=None, fallback=None, logger=None,
                 port=None, on_traffic=None):
        #: `members_of(game_conn) -> [同房间的其他游戏连接]`
        self._members_of = members_of or (lambda conn: [])
        #: `fallback(game_conn, udp_packet) -> None`，走 `0x040f`
        self._fallback = fallback
        #: `on_traffic(sender_game_conn) -> None`，**每投递一份同步数据调一次**。
        #:
        #: ★ 为什么挂在这里：`deliver()` 是**两条传输通道唯一的汇合点** ——
        #: `0x040e` 走 `Conn.on_peer_data` 调它，原版中继走 `RelayConn.on_data`
        #: 也调它。中继一旦建起来，`0x040e` 整局就不再出现一发了（§160），
        #: 所以任何「每帧要问一次」的房间级判断（对战判胜负、道具模式刷道具）
        #: 都必须挂在这儿，挂在 `on_peer_data` 上会在中继模式下彻底不触发。
        self._on_traffic = on_traffic
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
        # 房间级的「每帧问一次」挂在这儿（见 `_on_traffic` 的说明）。
        # ★ 排在转发**之后**，而且绝不许让它把同步数据带崩 —— 中继连接一断
        #   客户端会自己退房（§158），转发这条路必须比任何附加逻辑更硬。
        if self._on_traffic is not None:
            try:
                self._on_traffic(sender_game_conn)
            except Exception as error:      # noqa: BLE001 —— 主路优先
                self.log(f"!! 战斗节拍回调抛了 {error!r}，已忽略")
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
            # ★ 战斗数据的**主路**就是这条连接，关 Nagle 的收益全在这儿（D104）。
            tune_stream(sock)
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
            conns = list(self._conns.values())
            pending = len(self._tickets)
        line = (f"中继：在线 {len(conns)} 条，待兑票据 {pending} 张，"
                f"累计注册 {self.registered_total} 次；"
                f"投递 中继 {self.delivered_relay} / 回退 {self.delivered_fallback}")
        for conn in conns:
            rtt = conn.rtt_total.summary()
            if rtt is not None:
                line += f"\n    RTT [{conn.who()}] {rtt}"
        return line
