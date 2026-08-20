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
import select
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as server_config
import eventlog
import udpsync
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

#: `UdpPacket` 头长度（§151）。转发的载荷就是一整个 `UdpPacket`。
PEER_HEADER_SIZE = 12
#: 头里「会话/局号」那个 u16 的偏移（§151 的 `+4`）。
PEER_GAME_ID_OFFSET = 4

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

#: 中继方向的发送截止时间（秒）。战斗数据 ~8 Hz、每份几十字节，正常客户端
#: 毫秒级就收走了；超过 2 秒还不收包，这条流后面的字节它多半也解不开了
#: （SimpleCipher 是逐字节流密码，发送中途超时后密码状态已经错位）。
#: ★ 所以超时后**不再往这条连接发任何东西**（`send_broken`），投递自动
#:   回退到 `0x040f` 走游戏服连接 —— 玩家的画面靠回退路径恢复，不用断线。
#:   （bug调查/4：中继流一旦错位，客户端表现为「别人一动不动」，而它自己
#:   玩得好好的。）按铁律 1 的精神**不主动关 socket**，只是绕开它。
RELAY_SEND_DEADLINE_S = 2.0

#: 一条已注册的中继连接多久没有任何入站（数据帧或 pong）就算「半死」。
#: 战斗中数据 ~8 Hz、ping 1 Hz 一来一回，20 秒什么都没有 = 对端到我们的
#: 方向已经断了（NAT 超时 / 网络半开），客户端却收不到 FIN 还在傻等。
#: `gameserver.recover_peer_relay()` 拿这个判定要不要重发 `0x0211`+`0x0210`。
STALL_AFTER_S = 20.0

#: ★★ 「还没进过任何一局」的局号（§218）。客户端 `GameSession` 的构造
#: （`0x4050f8`）和 `GameSession::Reset`（`0x4054fa`）的第一句都是
#: `or dword [this+0x3c], 0xffffffff` —— 也就是 **-1**。
#:
#: 盖进 `UdpPacket` 头是 `0xFFFF`，而收包侧 `0x4078c4` 是
#: `movzx eax, word [pkt+4]` / `cmp eax, [GameSession+0x3c]` —— 一个 u16
#: 和 `0xFFFFFFFF` 比**永远不等** ⇒ 这个状态下客户端一发同步包都不收。
#: 下一次换代 `inc` 之后才是 0。
EPOCH_UNSET = -1

#: 每条连接留多少条「局号值 -> 代号」的历史（§218 / D137）。
#:
#: 换代那一刹那客户端手里还有用**旧值**发出的包（现场实测最多晚 426 ms），
#: 留着历史才能把它们正确地归到**上一代**，而不是当成「不认识的值」。
#: 开局前的强制对齐（D138）一次会连推好几格，所以不能只留一格。
EPOCH_HISTORY = 16

#: 代号发号器。代号本身没有语义，只用来判断两条连接**是不是处在同一代**
#: （同一次 `ResetQueues` 之后）。全进程自增而不是每个房间从 0 数，
#: 是为了让「换了房间」也不会和别人的号撞上。
_generation_lock = threading.Lock()
_generation_seq = 0


def next_generation():
    """发一个全进程唯一的「代号」。"""
    global _generation_seq
    with _generation_lock:
        _generation_seq += 1
        return _generation_seq


def peer_game_id(udp_packet):
    """读 `UdpPacket` 头 `+4` 的「会话/局号」。不像一个 `UdpPacket` 就返回 None。

    ## 这个字段为什么要管（bug调查/8_2 §213）

    收包入口 `0x4078c4` 拿它和**自己的** `[GameSession+0x3c]` 比，
    **不等就整包丢掉**（唯一豁免是描述符 type==5，普通房间用不上）。

    ⚠ §213 当时写的「服务端没有任何包能设定它」**是错的**，漏了
    `GameSession::Reset`（`0x4054fa`）那条 `or dword [this+0x3c], -1`。
    完整的真相见 §218 和 `PeerEpoch` 的注释：这个号的**每一次**变化都是
    服务端发的某一发包造成的（`0x0400`/`0x0403` 各 +1，登录成功 /
    `0x0203` / `0x030a` 归 -1），客户端自己不会动它。它不是客户端的私有
    计数器，是**只有服务端能推动的换代号**。

    于是「在同一个房间里多打一局」就会分叉：先来的人每打完一局 +2，
    中途进来的人从头数起。线上实测（`bug调查/8_2`，房 #69 第二局）
    受害者发的是**局号 3**，另外三个人发的都是**局号 1**——
    双向所有同步包互相全丢，症状就是用户报的**「其他人都不会动，
    但对局在正常进行」**（还听得见死亡音效，因为那走的是游戏服 `0x0406`）。

    ## 为什么可以改写

    校验和（头 `+6`）由 `0x5bbdc1` 算，**只覆盖 `+0x0c` 之后的 body**
    （`lea esi,[edx+0xc]`，种子 0x17），不含头里的局号。所以转发时把
    `+4` 换成收件人自己的局号，其余一个字节不动，校验和照样对得上。
    """
    packet = bytes(udp_packet)
    if len(packet) < PEER_HEADER_SIZE or packet[0] != MAGIC:
        return None
    return int.from_bytes(
        packet[PEER_GAME_ID_OFFSET:PEER_GAME_ID_OFFSET + 2], "little")


def as_signed_epoch(game_id):
    """把头里那个 u16 还原成**带符号**的局号（§218）。

    客户端那个字段是 dword，`GameSession::Reset`（`0x4054fa`）把它置成
    **-1**（`or ..., 0xffffffff`），而盖进 `UdpPacket` 头的是
    `mov ax, word [GameSession+0x3c]` —— 也就是低 16 位 `0xFFFF`。
    服务端的模型里存的是 -1，所以读回来必须转回去，否则「还没进过任何一局」
    的那些包会被当成「不认识的号 65535」。

    （`0x0303` 那个字段客户端也是按 `movsx` 读的 int16，两边口径一致。）
    """
    if game_id is None:
        return None
    return game_id - 0x10000 if game_id >= 0x8000 else game_id


def restamp_peer_game_id(udp_packet, game_id):
    """把 `UdpPacket` 头里的局号换成 `game_id`，其余字节原样返回。

    不是 `UdpPacket`（或 `game_id` 为 None）就原样返回，绝不抛 ——
    转发这条路上任何异常都会让同步断流（铁律 1）。
    """
    packet = bytes(udp_packet)
    if game_id is None or len(packet) < PEER_HEADER_SIZE or packet[0] != MAGIC:
        return packet
    head = PEER_GAME_ID_OFFSET
    return (packet[:head] + struct.pack("<H", int(game_id) & 0xFFFF)
            + packet[head + 2:])


class PeerEpoch:
    """一条游戏连接的**换代状态**（§218 / D137）。

    ## 为什么服务端能、而且必须自己维护它

    客户端那个局号 `[GameSession+0x3c]` 的每一次变化，**全部**由服务端发出的
    某一发包造成 —— 全镜像里改动它的只有三条指令，没有任何一处是客户端自发的：

    | 服务端 -> 客户端 | 客户端处理器 | 对局号 | 同时做的事 |
    |---|---|---|---|
    | `0x0100 gspRepLogin`（成功）| `0x54f2cc` -> 新建 `GameSession` -> `0x4050f8` | **= -1** | 六条队列清零 |
    | `0x0203 gspRepLeaveSession(result=0)` | `0x54fffe` -> `0x550092` -> `0x552943` -> `0x4054fa` | **= -1** | 队列清零、回大厅 |
    | `0x030a`（被踢 / 房间没了）| `0x552880` -> `0x552930` -> `0x552943` -> `0x4054fa` | **= -1** | 同上 |
    | ★ `0x0303 gspSession` | `0x406258` -> `0x406756` -> `0x556ed1` | **= 包尾那个 u16** | 顺带整份会话状态 |
    | `0x0400 gspPrepareGame` | `0x551605` -> `0x5517a3` | **+1** | `ResetQueues`、切 stage 6 |
    | `0x0403`（结算看完回房间）| `0x5518fb` -> `0x551900` | **+1** | `ResetQueues`、切 stage 5 |

    ★★ **`0x0303` 那一行是原版留给服务端的「直接设定」入口**：反序列化
    `0x556ed1` 最后两句是 `movsx eax, ax` / `mov [this+0x3c], eax` ——
    服务端说几就是几（int16，负数也能下发）。中途进房的人就是靠这一发和
    全房间对齐的（进房本来就要发它），不需要「补发若干发换代包」那种花招。

    `0x0400` / `0x0403` 这两条都紧跟着 `GameSession::ResetQueues`（`0x407678`）
    —— 所以这个号的职责是**给每座位收包队列打纪元戳**，不让上一代的包掉进
    刚清空的队列。它不是会话 id、也不是房间号，**是代号**。

    ## 两个来源，分工明确

    - **「代」（`gen`）永远来自我们自己发出去的字节** —— 换代是我们造成的，
      所以「谁在第几代」是硬事实，不猜、也不接受推翻；
    - **「值」（`value`）以模型为准、以客户端自报为校准** —— 只有它可能对不上
      （客户端出现我们不理解的状态），对不上时**值听客户端的、代仍按事件流走**。

    ★ 任何情况下都**不回退**到「无条件改写」（D131）或「原样转发让客户端自己
    丢」（D134）：前者会把跨代的心跳放行、钉死收件人的队列基线（bug调查/9），
    后者在「收件人的号正好和发送方的旧号撞上」时是**误收**，同一个死法。
    """

    __slots__ = ("value", "gen", "gen_of", "_order", "pending", "confused")

    def __init__(self):
        #: 我们认为客户端现在的局号。-1 = 还没进过任何一局。
        self.value = EPOCH_UNSET
        #: 现在属于哪一代（`next_generation()` 发的号）。None = 还没锚定。
        self.gen = None
        #: {局号值 -> 代号}，最近 `EPOCH_HISTORY` 条。在途的旧值包靠它归代。
        self.gen_of = {}
        self._order = []
        #: 最近一次换代还没被客户端自报确认。这段窗口里**绝不放行**任何
        #: 认不出来的值 —— 它就是 bug调查/9 那一发毒心跳所在的窗口。
        self.pending = False
        #: 自报值和模型对不上、被迫重锚的次数（只给 `status` / 日志看）。
        self.confused = 0

    def _remember(self, value, gen):
        if value not in self.gen_of:
            self._order.append(value)
            while len(self._order) > EPOCH_HISTORY:
                self.gen_of.pop(self._order.pop(0), None)
        self.gen_of[value] = gen

    def reset(self):
        """客户端把 `GameSession` 重建/复位了（登录成功 / `0x0203` / `0x030a`）。

        局号回到 -1、六条队列清空、人回到大厅 —— 旧的代号历史全部作废。
        """
        self.value = EPOCH_UNSET
        self.gen = None
        self.gen_of.clear()
        del self._order[:]
        self.pending = False

    def advance(self, gen):
        """我们刚给它发了一发 `0x0400` / `0x0403`：局号 +1，进入 `gen` 这一代。"""
        self.value += 1
        self.gen = gen
        self._remember(self.value, gen)
        self.pending = True

    def assign(self, value, gen):
        """我们刚用 `0x0303 gspSession` **直接把局号设成** `value`。

        这是原版给服务端留的入口（`0x556ed1`），也是「中途进房的人怎么和
        全房间对上」的正解 —— 进房本来就要发这一发。
        """
        self.value = int(value)
        self.gen = gen
        self._remember(self.value, gen)
        self.pending = True

    def anchor(self, gen):
        """进房 / 建房：它当前这个值就属于房间当前这一代。"""
        self.gen = gen
        self._remember(self.value, gen)

    def generation_of(self, value):
        """这个自报值属于哪一代；认不出来返回 ``None``（= 不许投递）。"""
        return self.gen_of.get(value)

    def observe(self, value):
        """客户端自报了一次局号（每发同步数据都带）。返回一个判词：

        * ``"ok"``       —— 和模型一致，换代确认；
        * ``"old"``      —— 是历史里的旧值 = 换代那一刹那还在途的上一代包；
        * ``"stale"``    —— 换代还没确认、又认不出这个值 ⇒ 当陈旧包处理（丢）。
          **这是最危险的窗口，宁可丢也不放行**；
        * ``"reanchor"`` —— 没有待确认的换代却对不上 ⇒ 客户端出现了我们不理解的
          状态：把**值**重锚到自报值（代不动），之后照常按代判定。自愈、不降级。
        """
        if value == self.value:
            self.pending = False
            return "ok"
        if value in self.gen_of:
            return "old"
        if self.pending:
            return "stale"
        self.value = value
        self._remember(value, self.gen)
        self.confused += 1
        return "reanchor"


def epoch_state(conn):
    """取（必要时建）这条连接的换代状态。

    和 `peer_game_id` 同一个理由挂在连接对象上：`relayserver` 不 import
    `gameserver`，状态跟着连接走，测试里拿个假连接就能单测。
    """
    state = getattr(conn, "peer_epoch", None)
    if state is None:
        state = PeerEpoch()
        try:
            conn.peer_epoch = state
        except AttributeError:      # 只读的假对象：退化成一次性状态
            pass
    return state



def build_rcp(opcode, payload=b""):
    """一帧 rcp。**和 `gameserver.build_game` 是同一串字节**（§156）。

    两处各写一份是故意的：`relayserver` 不 import `gameserver`（那边要 import
    这边），而这个格式已经被 `0x5bcb19` 钉死了，不会变。
    `test_relayserver.py` 里有一条用例把两者逐字节对住，防止哪天单边漂了。
    """
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"rcp 载荷 {len(payload)} 字节超过 u16 上限")
    return (bytes([MAGIC, 0]) + struct.pack("<HHHH", len(payload), 0, 0, opcode)
            + payload)


def send_all_bounded(sock, data, deadline):
    """带截止时间的发送（和 `gameserver.send_all_bounded` 同款，这里按
    「不 import gameserver」的老规矩各自留一份）。超时抛 `socket.timeout`。"""
    deadline = float(deadline)
    if not isinstance(sock, socket.socket):
        # 测试里的假 socket（只实现了 sendall）—— 没法 select，按老路走。
        sock.sendall(data)
        return
    end = time.monotonic() + deadline
    view = memoryview(data)
    while view:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise socket.timeout(
                f"send deadline {deadline:.1f}s exceeded, "
                f"{len(view)} bytes unsent")
        _, writable, _ = select.select([], [sock], [], remaining)
        if not writable:
            continue
        sent = sock.send(view)
        view = view[sent:]


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

    # ★ 类级默认值：`RelayConn.__new__` 造的测试实例（test_latency）不走
    #   `__init__`，这两个标志必须在类上也有一份默认。
    send_broken = False
    last_inbound_at = 0.0

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
        #: 最近一次收到任何入站（数据帧或 pong）的时刻 —— `stalled()` 的依据。
        self.last_inbound_at = self.opened_at
        #: 发送流已废（发送超时后密码流错位，这条 TCP 上游方向不能再用了）。
        #: ★ 不关 socket（铁律 1），只是 `conn_for()` 从此绕开它，
        #:   投递回退到 `0x040f` 走游戏服连接。
        self.send_broken = False
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
        """发一帧。**失败只记录不抛** —— 上层绝不能因为发送失败去关连接（铁律 1）。

        发送带 `RELAY_SEND_DEADLINE_S` 的截止时间：一个不收包的客户端
        （崩溃后在写 dump / 卡在 WER 弹窗 / 网络半开）不能把往它投递的
        别人的线程一起拖死。超时后标记 `send_broken`：这条流对客户端来说
        已经解不开了（SimpleCipher 流密码错位），继续发只是垃圾 ——
        `conn_for()` 会绕开它，投递自动回退 `0x040f`。
        """
        if self.closed or self.send_broken:
            return False
        frame = build_rcp(opcode, payload)
        try:
            # 流密码是有状态的，加密和发送必须锁在一起，
            # 否则两个线程各加密一半、交错发出去，客户端整条流就废了。
            with self.send_lock:
                wire = self.cout.encrypt(frame)
                send_all_bounded(self.sock, wire, RELAY_SEND_DEADLINE_S)
        except OSError as error:
            if not self.send_broken:
                self.send_broken = True
                self.log(f"!! 中继发送失败（{error!r}）—— 这条流已错位，"
                         f"投递改走 0x040f 回退（不关连接，铁律 1）")
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
        if self.send_broken:
            return                     # 上游方向已废，ping 也别再发了
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
        self.last_inbound_at = time.time()
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
        self.last_inbound_at = time.time()
        while True:
            got = take_rcp(self.buf)
            if got is None:
                break
            opcode, payload, size = got
            del self.buf[:size]
            self.frames_in += 1
            self.on_frame(opcode, payload)

    def inbound_idle(self, now=None):
        """距最近一次入站（数据帧或 pong）过了多少秒。"""
        now = time.time() if now is None else now
        return now - self.last_inbound_at

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
        # ★ 走**游戏连接自己的上行入口**，和 `0x040e` 那条完全同一条路。
        #   理由有两条：
        #   1. 位置数据的 UDP 旁路把「UDP 那份」和「TCP 那份」合流的排序闸门
        #      就装在那里（`udpsync` 铁律 2）—— 中继这条路绕过去的话，
        #      同一发心跳会被投递两次，晚到的那份会把角色拉回旧位置；
        #   2. 顺带把中继这条路也纳入转发耗时/到达间隔统计 —— §187 那一轮
        #      「中继 RTT 一行都没有」正是因为这条路从来不记账。
        #   没有这个方法的（`test_relayserver` 里的假连接）照旧直投。
        uplink = getattr(self.game_conn, "on_peer_data", None)
        if uplink is not None:
            uplink(payload)
        else:
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
                 port=None, on_traffic=None, generation_of=None,
                 udp_sender=None):
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
        #: `generation_of(game_conn) -> 代号 | None`，**只用来补锚**：
        #: 正常路径上每条连接在建房 / 进房时就锚定了（`PeerEpoch.anchor`），
        #: 这个回调是防御性的第二道 —— 万一哪条路径漏了锚定，这里按它当前
        #: 房间补一次，而不是让它的同步数据被当成「跨代」全丢掉。
        self._generation_of = generation_of
        #: 位置数据的 UDP 旁路（`udpsync.UdpSyncServer`）。`None` = 不启用，
        #: 投递完全回到「中继 / `0x040f`」这两条原来的路上。
        self._udp_sender = udp_sender
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
        self.delivered_udp = 0
        #: 因为「同一代里两台的局号编号不同」而改写过局号的包数（D131 的那条路，
        #: 有人中途进房才会有）。开局前的强制对齐（D138）正常工作时这个数**应该
        #: 恒为 0** —— 不为 0 就说明对齐没顶平，值得看一眼日志。
        self.restamped_total = 0
        self._restamped_pairs = set()
        #: **跨代丢弃**的包数：发送方那一发属于的代和收件人现在这一代不是同一代
        #: （换代那几百毫秒的竞态，或者有人还卡在结算界面）。这些包无论改写与否
        #: 都不该进收件人的队列 —— 改写会钉死它的基线（bug调查/9），
        #: 原样转发则可能撞号误收。直接丢是唯一安全的处置。
        self.cross_gen_dropped = 0
        self._cross_gen_pairs = set()
        #: 客户端自报的局号和模型对不上、被迫重锚的次数（`PeerEpoch.observe`）。
        #: 正常情况下恒为 0；不为 0 说明有一条我们还不知道的换代路径。
        self.epoch_confused = 0
        self._confused_conns = set()

    # -- 日志 ---------------------------------------------------------------
    def log(self, msg):
        if self._logger is not None:
            self._logger(msg)

    def _epoch_of(self, conn):
        """这条连接的换代状态；还没锚定就用 `generation_of` 回调补一次。"""
        state = epoch_state(conn)
        if state.gen is None and self._generation_of is not None:
            gen = self._generation_of(conn)
            if gen is not None:
                state.anchor(gen)
                who = getattr(conn, "account_name", None) or "?"
                self.log(f"换代锚定：{who} 之前没锚过，按它当前房间补成 "
                         f"代 {gen}（局号 {state.value}）")
        return state

    def _note_restamp(self, sender, receiver, sender_id, receiver_id):
        """同一代里两台的局号编号不同 —— 改写。**每对只报一行**。

        这条不是错误：有人中途进房时，两台客户端从不同的起点开始数
        （局号是「进房之后收到过几发 `0x0400`/`0x0403`」，§218），
        所以同一代里编号本来就会错开。改写之后同步照常。

        ★ 但开局前的强制对齐（D138）会把全房间顶平，正常情况下这一行
        **一次都不该出现** —— 出现了就说明对齐那一步没生效，照着查。
        """
        self.restamped_total += 1
        key = (id(sender), id(receiver), sender_id, receiver_id)
        if key in self._restamped_pairs:
            return
        self._restamped_pairs.add(key)
        from_who = getattr(sender, "account_name", None) or "?"
        to_who = getattr(receiver, "account_name", None) or "?"
        self.log(f"同代改写局号：{from_who} 发的是 {sender_id}，"
                 f"{to_who} 认的是 {receiver_id}（同一代，编号起点不同）"
                 f"—— 不改写收件人会整包丢掉（bug调查/8_2「别人一动不动」）；"
                 f"★ 开局前的强制对齐正常时不该出现这一行")

    def _note_cross_gen(self, sender, receiver, sender_id, send_gen, want):
        """跨代的包：**丢掉**，不投递。每对（两个代号）只报一行。

        这条也不是错误 —— 它恰恰是正确行为：换代和 `ResetQueues` 是同一件事，
        上一代的包进了刚清空的队列就会把基线钉死（bug调查/9「打不死人」）。
        「过渡期」= 从服务端发出换代包，到这条连接自报的局号变成新值为止，
        一毫秒不多不少，不需要任何时间阈值。
        """
        self.cross_gen_dropped += 1
        key = (id(sender), id(receiver), send_gen, want.gen)
        if key in self._cross_gen_pairs:
            return
        self._cross_gen_pairs.add(key)
        from_who = getattr(sender, "account_name", None) or "?"
        to_who = getattr(receiver, "account_name", None) or "?"
        self.log(f"跨代丢弃：{from_who} 那一发局号 {sender_id} 属于 "
                 f"代 {send_gen}，{to_who} 已经在代 {want.gen}"
                 f"（局号 {want.value}）—— 丢掉，不投递也不改写"
                 f"（改写会钉死它的收包队列基线，bug调查/9）")

    def _note_confused(self, conn, value):
        """自报值对不上模型、已经重锚。每条连接只报一行（后面只累加计数）。"""
        self.epoch_confused += 1
        if id(conn) in self._confused_conns:
            return
        self._confused_conns.add(id(conn))
        who = getattr(conn, "account_name", None) or "?"
        self.log(f"!! 换代模型失准（已重锚）：{who} 自报局号 {value}，"
                 f"既不是模型里的值也不在历史里 —— 说明有一条我们还不知道的"
                 f"换代路径。值已按自报值重锚，代不动，转发继续按代判定。")

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

    def issued_at(self, game_conn):
        """当前那张未作废票据的签发时刻；已兑过（票据被取走）返回 ``0.0``，
        从没签发过返回 ``None``。`recover_peer_relay()` 用它区分
        「票据才发出去、客户端还在连」和「早就该连上却没连上」。"""
        with self._lock:
            nonce = self._issued.get(game_conn)
            if nonce is None:
                return None
            ticket = self._tickets.get(nonce)
            return ticket.issued_at if ticket is not None else 0.0

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
        """这条游戏连接现在可用的中继连接；没有 / 已关 / 发送流已废都算没有。

        ★ `send_broken` 的连接在这里返回 `None`，`deliver()` 就会自动把
        同步数据回退到 `0x040f` 走游戏服连接 —— 客户端的画面靠回退恢复，
        不用断线重连（这条 TCP 的上游方向废了，但它的入站方向还可能是好的，
        读线程照常收它的数据）。
        """
        with self._lock:
            relay = self._conns.get(game_conn)
        if relay is None or relay.closed or relay.send_broken:
            return None
        return relay

    def stalled(self, game_conn, threshold=STALL_AFTER_S):
        """这条游戏连接的中继连接是不是「半死」（长时间没有任何入站）。

        战斗中数据 ~8 Hz、ping 1 Hz，`threshold` 秒什么入站都没有说明
        客户端到我们的方向已经断了（NAT 超时 / 网络半开），而客户端收不到
        FIN 还以为一切正常 —— 表现为「他自己玩得好好的，别人看他一动不动」。
        `gameserver.recover_peer_relay()` 据此重发 `0x0211`+新 `0x0210`。
        """
        with self._lock:
            relay = self._conns.get(game_conn)
        if relay is None or relay.closed:
            return False
        return relay.inbound_idle() > threshold

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
        """把一份同步数据发给同房间的其他人。返回**真的送到了**几个人。

        对方接上中继了就走中继（原版路径），没接上就退回 `0x040f`
        —— 中继连接是异步建的，进房那几秒里必然有一个「有人还没接上」的窗口，
        那几秒里不能让同步断掉。

        ## 局号怎么处置：**按代判定，只有两条出口**（§218 / D137）

        头 `+4` 的局号是客户端**每座位收包队列的纪元号**，它的每一次变化都是
        我们某一发 `0x0400`/`0x0403`/`0x0203`/登录包造成的（`PeerEpoch` 的注释里
        有那张表），所以服务端手里本来就有「谁在第几代」这个硬事实：

        * **同一代** -> 按收件人自己的编号盖章（相同则一个字节不动）。
          同一代里编号会不同，是因为有人中途进房、起点不一样（bug调查/8_2）。
        * **不同代** -> ★ **丢掉，不投递**。改写会把上一代的心跳放进刚清空的
          队列、把 `base` 钉死（bug调查/9「第二局打不死人」）；原样转发也不安全
          —— 收件人的号可能正好和发送方的旧号撞上，那就是误收。

        没有第三条出口，也没有任何时间阈值：「过渡期」恰好等于「从我们发出
        换代包，到这条连接自报的局号变成新值为止」。

        校验和（头 `+6`）从 `+0x0c` 起算、不覆盖局号，所以改写这两个字节
        不用重算（`peer_game_id` 的注释里有逐指令依据）。
        """
        sent = 0
        # ★ 转成带符号：客户端「还没进过任何一局」时盖的是 0xFFFF = -1。
        game_id = as_signed_epoch(peer_game_id(udp_packet))
        send_gen = None
        if game_id is not None:
            # 发送方每发一发都自报一次自己的局号（战斗中 ~8 Hz）：拿它确认
            # 我们的模型、并判定这一发属于哪一代。
            sender = self._epoch_of(sender_game_conn)
            if sender.observe(game_id) == "reanchor":
                self._note_confused(sender_game_conn, game_id)
            send_gen = sender.generation_of(game_id)
            try:
                sender_game_conn.peer_game_id = game_id
            except AttributeError:      # 测试里的假连接，不影响转发
                pass
        for member in self._members_of(sender_game_conn):
            if member is sender_game_conn:
                continue
            packet = udp_packet
            if game_id is not None:
                want = self._epoch_of(member)
                if send_gen is None or want.gen is None or send_gen != want.gen:
                    self._note_cross_gen(sender_game_conn, member,
                                         game_id, send_gen, want)
                    continue
                if want.value != game_id:
                    packet = restamp_peer_game_id(udp_packet, want.value)
                    self._note_restamp(sender_game_conn, member,
                                       game_id, want.value)
            # ★ 第三条路：位置数据的 UDP 旁路（`udpsync`，bug调查/9）。
            #   排在最前面，但准入条件很窄 —— 三条**都**要成立：
            #     1. 这一份是位置心跳（内层 0x4001）。开火/命中/伤害走的是
            #        客户端的可靠队列，丢一发就整局错位（§217），永远走 TCP；
            #     2. 收件人自证过它那边 7788 收得到，而且这条流还活着；
            #     3. 这一发的 N 和上一发送给他的相同（`may_send_heartbeat`
            #        里有完整推导：这一条让下行**可证明**不会造成队列错位）。
            #   任何一条不成立就落到下面原来的两条路上，行为和今天一样。
            #   ⚠ 绝不「两条都发」：心跳没有任何可判新旧的原版字段，
            #     晚到的那份会把角色拉回旧位置。
            if (self._udp_sender is not None
                    and udpsync.is_heartbeat(packet)
                    and self._udp_sender.ready_for(member)
                    and self._udp_sender.may_send_heartbeat(
                        member, sender_game_conn, packet)
                    and self._udp_sender.send_to(member, packet)):
                sent += 1
                self.delivered_udp += 1
                continue
            relay = self.conn_for(member)
            if relay is not None:
                sent += relay.send_data(packet)
                self.delivered_relay += 1
            elif self._fallback is not None:
                try:
                    self._fallback(member, packet)
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
        # ★ 三条出路要一起报。`delivered_udp` 现在是**主路** —— `bug调查/udp验证`
        #   那一局 50160 人次里有 97.6% 走的是它（§225），只报「中继 / 回退」
        #   会让人以为位置数据还在走 TCP。顺序按 `deliver()` 里的判定顺序。
        line = (f"中继：在线 {len(conns)} 条，待兑票据 {pending} 张，"
                f"累计注册 {self.registered_total} 次；"
                f"投递 UDP {self.delivered_udp} / 中继 {self.delivered_relay}"
                f" / 回退 {self.delivered_fallback}"
                f"；同代改写局号 {self.restamped_total} 发"
                f"，跨代丢弃 {self.cross_gen_dropped} 发"
                f"，模型失准 {self.epoch_confused} 次")
        for conn in conns:
            rtt = conn.rtt_total.summary()
            if rtt is not None:
                line += f"\n    RTT [{conn.who()}] {rtt}"
        return line
