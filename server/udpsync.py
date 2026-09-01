#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""udpsync.py —— **位置数据的 UDP 旁路**（bug调查/9 的收尾）。

## 为什么要它

`bug调查/9` 逐包配对量出来的事实：客户端**发**得非常准（p50=128 ms、p95=130 ms、
只有 0.06% 超过 250 ms），但同一批 5405 发包到服务器时变成 p95=**432 ms**、
33% 成串到达，**每秒约一次「停 0.43 秒 → 3 发一起到」**。发出 5405、收到 5405，
**一发不丢** —— 所以不是丢包，是 TCP 把丢的那一段重传补回来时的**队头阻塞**：
后面已经到达的字节全被压在内核里等那一段。

客户端不做插值也不做回滚（§187），收到什么位置就画在什么位置，于是这个抖动
100% 变成别人屏幕上的瞬移（停顿结束那一瞬间角色跨过的距离中位数 104 坐标单位，
正常一步 36）。**在 TCP 上无解** —— 队头阻塞是 TCP 自己的事，和内容冗余无关。

所以位置数据额外走一条 UDP：丢了就丢了，0.13 秒后下一发自然补上，
**永远不会因为丢一份而把后面的全卡住**。

## 铁律 1：只有位置走 UDP，其余一律 TCP

    内层 opcode 0x4001（心跳，位置就在它 body 里）   -> UDP + TCP 双发
    内层 opcode < 0x4000（rpFire / rpExplode / …）    -> **只走 TCP**
    0x4002 讨重传 / 0x4003 / 0x4004 / 0x4005          -> **只走 TCP**
    所有 gsp/gcp 包（死亡、结算、换图…）              -> **只走 TCP**

判据就是客户端自己的分发出口（§151）：`< 0x4000` 进每座位的 `PktQueue`（可靠、
按序、丢一发就永久错位），`>= 0x4000` 立刻处理。**能丢的才配走 UDP。**

## 铁律 2：上行**双发**，服务端按索引去重 —— 回退是天然的

`bshook` 看到内层 `0x4001` 就额外从自己那条 UDP socket 发一份、盖上单调递增的
索引；**TCP 那一份照发不误**。服务端两条路都收，谁先到用谁。

⇒ **UDP 完全不通 = 今天的行为，一行 fallback 代码都不需要。**
   被墙、NAT 掐了、服务器没放行 UDP、玩家用的是没更新的客户端 —— 全都自动落回 TCP。

## ★★ 铁律 3：心跳里的 N 决不许越过还没转发的事件包

这一条是本文件最要紧的不变式，写错了会**整局打不死人**（§216 / §217）。

心跳 body 头 2 字节（`UdpPacket +12..13`）是发送方「**下一个事件包**序号 N」。
收方拿它做 `Grow(N-1)`，并且在**队列还没激活时**做 `FlushTo(N)` ——
而 `FlushTo` 会把 `base` 钉到 N 且**只进不退**，之后 `seq < base` 的事件包
在 `PktQueue::Insert` 里被**静默丢弃**，连讨重传都不会讨。

⇒ 如果 UDP 让一发 `N=3` 的心跳比事件 0/1/2 先到，收方就永久丢掉那三发。
   而 `rpFire` 里**没有弹体句柄**、收方得自己按同样顺序分配 —— 少喂一发，
   那台机器上这个座位的句柄分配器**永久错位**，之后每一发 `rpExplode` 的句柄
   都指向不存在的对象 ⇒ 伤害数字照出、血照掉一丝、**就是打不死**（§217 定案）。

所以放行一发 UDP 心跳的判据是两条**都**要满足：

    1. index >= 已转发水位            —— 不是旧的/重复的
    2. N    <= 已转发的事件数         —— ★ 不会越过任何还没转发的事件包

第 2 条在「只跑不开枪」时永远成立（实测同一个 N 最长连续 315 发心跳），
所以绝大部分时间 UDP 该省的都省到了；一旦刚开完枪，那一两发心跳老老实实等 TCP。
**宁可少省一发，也不许错位一局。**

## 铁律 4：一个收件人同一时刻只有一条路

下行（服务端 → 客户端）**绝不**双发：心跳要么走 UDP 要么走 `0x040f`，
由 `relayserver.RelayServer.deliver()` 逐人选一条。原因还是排序 ——
心跳没有任何可用来判新旧的原版字段（`UdpPacket +8` 的序列号对心跳**恒为 0**，
实测 4527 发只有 1 个取值），两条路同时送就一定会有旧的盖掉新的、角色被拉回去。

## 线格式（**我们自己的**，和游戏协议无关）

只在 `bshook` ↔ `server/relay.py` ↔ 本文件之间用。8 字节头：

```text
+0  4  magic  b"PSU\\x01"      最后一字节是版本号，改格式就 +1
+4  1  类型    HELLO / HELLO_ACK / DATA / PING / PONG
+5  1  份数    DATA 时 = 后面跟几组（含当前这一份）
+6  2  保留    0
```

- `HELLO`      : `u16 票据长度 + 票据(UTF-8)`。票据就是登录时那张（`tickets.py`），
                 服务端拿它查回是哪条游戏连接。**不引入新的秘密。**
- `HELLO_ACK`  : `u8 结果码 + u16 说明长度 + 说明(UTF-8)`。
- `DATA`       : 份数 × (`u32 索引 + u16 长度 + 整个 UdpPacket`)，**索引升序**。
- `PING`/`PONG`: `u32 序号`。保活（撑住 NAT 映射）顺带量 RTT。

## 冗余（`udp_sync_redundancy`，默认 2）

每个 `DATA` 捎带前 N 份历史心跳。**这不是为了画面更顺** —— 位置是快照，
新的自然覆盖旧的，补发旧位置对渲染毫无意义。它的唯一价值是**把心跳的索引序列
补齐**，让上面铁律 3 的第 1 条判据不会因为丢一份就长期卡住，
以及保住「复位后第一发心跳定基线」那一发（§217）。
"""
from __future__ import annotations

import errno
import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as server_config
from netlisten import ANY_HOSTS

#: 头的魔数 + 版本。**最后一个字节是版本号** —— 线格式一旦改就 +1，
#: 老版本的客户端发过来会被直接丢掉（而不是按新格式解出垃圾）。
MAGIC = b"PSU\x01"
HEADER_SIZE = 8

#: 报文类型。
MSG_HELLO = 1
MSG_HELLO_ACK = 2
MSG_DATA = 3
MSG_PING = 4
MSG_PONG = 5

#: `HELLO_ACK` 的结果码。
ACK_OK = 0
ACK_BAD_TICKET = 1
ACK_DISABLED = 2
#: ★ 「这张票据是我们签发的、还没过期，**但还没有哪条游戏连接认领它**」。
#:
#: 这不是失败，是**时序**：`bshook` 一看到 `0x0100 gcpReqLogin` 从客户端发出去
#: 就立刻发 `HELLO`，而这一刻游戏服那边才刚收到登录包、还没把
#: `login_ticket` 写到连接上。两者之间那 0.1~0.2 秒里，票据表认得这张票，
#: 连接表还查不到人 —— **每一次登录都必然经过这个窗口**。
#:
#: 把它和 `ACK_BAD_TICKET` 分开，中继才有**事件**可依据：
#: 收到本码 = 「还没轮到，安静等下一发」，收到 `ACK_BAD_TICKET` = 「真不认识
#: 这张票（服务端重启过 / 过期 / 被顶号），该告诉玩家一声」。
#: 分不开的话就只能靠「跳过头几发」这种拍脑袋的次数，换台机器就不准。
#:
#: ⚠ 探测面：它比 `ACK_BAD_TICKET` 多泄露一个 bit（「这张票是真的」）。
#: 票据是 128 bit 随机数（`tickets.py`），猜中的概率不值得考虑，而且同一个
#: 区分在游戏服 TCP 登录那边本来就是公开的（认不出票据回 `result=2`）。
ACK_NOT_LOGGED_IN = 3

#: 老服务端不认识 `ACK_NOT_LOGGED_IN`，老中继也不认识它 —— 两个方向都安全：
#: 老中继把它当成「不是 OK」，行为退回今天的样子；新中继连到老服务端时
#: 收到的是 `ACK_BAD_TICKET`，也只是回到「登录时会多打一行」而已。
#: **所以线格式的版本号（`MAGIC` 最后一字节）不用动。**

#: 一组数据的定长部分：`u32 索引 + u16 长度`。
CHUNK_HEADER = struct.Struct("<IH")

#: `UdpPacket` 头长度和几个偏移（§151；和 `relayserver` 里那份是同一套）。
PEER_HEADER_SIZE = 12
#: 内层 opcode（`+10` u16）：`>= 0x4000` 立刻处理，`< 0x4000` 进 `PktQueue`。
PEER_OPCODE_OFFSET = 10
#: 「目标座位」（`+2`）。`0xff` = 广播。
PEER_TARGET_OFFSET = 2
PEER_BROADCAST_SEAT = 0xFF
#: 心跳 body 的头 2 字节 = 发送方「下一个事件包序号 N」（§216）。
PEER_HEARTBEAT_N_OFFSET = PEER_HEADER_SIZE
#: 事件包的序号（`+8` u16）。★ 对心跳**恒为 0**（实测 4527 发只有 1 个取值），
#: 所以它只在 `< 0x4000` 的事件包上有意义 —— 心跳的「新旧」只能靠我们自己的索引。
PEER_SEQUENCE_OFFSET = 8

#: 心跳的内层 opcode。**当前唯一允许走 UDP 的东西。**
OPCODE_HEARTBEAT = 0x4001

#: 心跳 body 里那 24 字节「角色状态结构」的起点（body `+7`，V0.3 §24）。
PEER_HEARTBEAT_STATE_OFFSET = PEER_HEADER_SIZE + 7
#: 角色位置（结构 `+0x00` / `+0x02`，两个 **i16**）= body `+7..10`。
#:
#: ⚠ 早先 V0.3 §3 猜的是 body `+25..28`，**那是错的**（§24 有两条独立证据：
#: 反序列化器 `0x5041e1` 把结构 `+0`/`+2` 写进 `[char+0x34]`/`[char+0x38]`，
#: 而 `+25..28` 是**准星的屏幕坐标**，写进 `[char+0x680]`/`[0x684]`，V0.3 §25）。
PEER_HEARTBEAT_POS = struct.Struct("<hh")

#: 角色状态结构 `+0x04` / `+0x06`（body `+11..14`）= ★ **空中的抛体速度**，
#: 两个 i16（收方 `0x504274` 写进 `[char+0x120]` / `[char+0x124]`）。
#:
#: ★★ **它不是「走路速度」**（V0.3 §35 推翻了 §31）：角色**踩在地上**移动时
#: 这两格恒 0 —— 语料里「位置变了且 bit2=1」的 20341 发里，速度非 0 的只有 9 发。
#: 非 0 只出现在跳跃 / 被击飞 / 下落这种**离地**的帧上。
PEER_HEARTBEAT_VELOCITY = struct.Struct("<hh")
PEER_HEARTBEAT_VELOCITY_OFFSET = PEER_HEARTBEAT_STATE_OFFSET + 4

#: 位域（结构 `+0x0c` = body `+19..22`）在包里的偏移，和它的 **bit2**。
#:
#: ★★ bit2（= `[char+0x128]`，收方 `0x5042b1` 写）是 **「我此刻踩在地面上」**，
#: 不是「我静止着」（V0.3 §35）。判据见那一节的四格表。
PEER_HEARTBEAT_FIELD_OFFSET = PEER_HEARTBEAT_STATE_OFFSET + 12

#: ★★ 角色状态结构 `+0x10` = **方向键掩码**（V0.3 §39）。收方 `0x5073c2` 拿它
#: 算走路方向、`0x507660` 照着**每 32 ms 替远端角色走一步**。服务端逐格外推
#: 真人位置时要读的就是它（D106，`bot._advance_humans`）。
PEER_HEARTBEAT_KEYS_OFFSET = PEER_HEARTBEAT_STATE_OFFSET + 0x10
PEER_HEARTBEAT_BIT_ONGROUND = 0x04

#: 同一个位域的 **bit3**（= `[char+0x4bc]`）= ★ **「我在冲刺」**（V0.3 §40）。
#:
#: 就是按住鼠标右键那个「耗能量、跑得快、脚下扬尘」的状态。收方拿它
#: **把这个角色整帧的 `dt` 乘上 `FastRunRate`**（`0x507594`）—— 走路位移、
#: 空中积分、动画播放速率全都跟着快。语料实测：置起时在地上每帧 `|dx|`
#: 中位数 **33**，没置起是 **22**（正好 1.5 倍，即这一版配置的 `FastRunRate`）。
PEER_HEARTBEAT_BIT_FASTRUN = 0x08

#: 一个 UDP 数据报最大多长。位置包 43 字节 + 我们 8 字节头 + 每份 6 字节，
#: 捎带满 4 份也才 ~260 字节 —— 1400 是留给「以后想多带点」的余量，
#: 同时保证永远不会被 IP 分片（分片一丢就是整报丢，等于白做冗余）。
MAX_DATAGRAM = 1400

#: 多久没收到这条流的任何东西就算它死了（秒）。心跳 8 Hz、保活 10 秒一发，
#: 5 秒什么都没有 = 这条路断了 ⇒ 下行立刻退回 TCP。
DEAD_AFTER_S = 5.0

#: 一条已认出来的流最多留多久没人管（秒）。游戏连接断开时会显式 `forget()`，
#: 这个上限只是兜底，免得异常路径攒垃圾。
ENDPOINT_TTL_S = 300.0

#: UDP 安静多久之后，就允许 TCP **重新以自己为准**（秒）。
#:
#: ★ 这是整套索引机制**唯一**的自愈出口，比任何「检测失配」都可靠。
#: 要防的是这个死法：UDP 把水位推到很前面（正常，它领先 300~400 ms），
#: 然后这条 UDP 路突然断了 —— 此时 TCP 那侧的编号还远在水位后面，
#: 每一发都被当成「影子」丢掉，**位置更新会整个停住**。
#: 两秒（≈16 发心跳）没有任何 UDP 心跳进来，就把水位拉回 TCP 的进度。
#:
#: 它同时兜住了「`bshook` 和服务端的索引起点对不上」这类 bug ——
#: 不管是哪个方向对不上，最多难看两秒就自己回到 TCP 的节奏上。
UDP_QUIET_S = 2.0


class ProtocolError(ValueError):
    """线格式解不开。**一律当成「这不是我们的包」丢掉，不回任何东西** ——
    UDP 上任何人都能往这个端口发字节，回错误信息等于送一个反射放大面。"""


def build_header(kind, count=0):
    return MAGIC + struct.pack("<BBH", kind & 0xFF, count & 0xFF, 0)


def parse_header(data):
    """`bytes -> (类型, 份数)`。魔数/版本对不上就抛 `ProtocolError`。"""
    if len(data) < HEADER_SIZE or not data.startswith(MAGIC):
        raise ProtocolError("magic/version mismatch")
    kind, count, _ = struct.unpack_from("<BBH", data, len(MAGIC))
    return kind, count


#: `HELLO` 的标志位：对方自报「我这边 7788 收得到，可以给我发下行」。
#:
#: ★ `bshook` 发的 `HELLO` **没有**这一位（它只负责把票据交给本机中继），
#: 只有 `relay.py` 自证过之后才会带上 —— 自证方式见它的 `downlink_probe()`。
#: 所以「服务端能不能发下行 UDP」是**对方说了算**，不是我们猜的。
HELLO_FLAG_DOWNLINK = 0x01


def build_hello(ticket, flags=0):
    body = str(ticket or "").encode("utf-8")
    if len(body) > 0xFFFF:
        raise ValueError("ticket too long")
    return (build_header(MSG_HELLO) + struct.pack("<H", len(body)) + body
            + struct.pack("<B", flags & 0xFF))


def parse_hello(data):
    """`bytes -> 票据`。标志位另见 `parse_hello_flags`。"""
    return parse_hello_full(data)[0]


def parse_hello_full(data):
    """`bytes -> (票据, 标志位)`。

    ★ 标志位是**后加的**，老版本（`bshook` 那份）没有这一个字节 ——
    读不到就当 0。线格式加字段时不涨版本号的唯一理由就是这个：
    加在末尾且缺省安全，新旧两边都能互相读。
    """
    kind, _ = parse_header(data)
    if kind != MSG_HELLO:
        raise ProtocolError(f"not a HELLO ({kind})")
    if len(data) < HEADER_SIZE + 2:
        raise ProtocolError("HELLO truncated")
    (size,) = struct.unpack_from("<H", data, HEADER_SIZE)
    end = HEADER_SIZE + 2 + size
    if len(data) < end:
        raise ProtocolError("HELLO ticket truncated")
    flags = data[end] if len(data) > end else 0
    return data[HEADER_SIZE + 2:end].decode("utf-8", "replace"), flags


def build_hello_ack(result=ACK_OK, note=""):
    body = str(note or "").encode("utf-8")
    return (build_header(MSG_HELLO_ACK)
            + struct.pack("<BH", result & 0xFF, len(body)) + body)


def parse_hello_ack(data):
    kind, _ = parse_header(data)
    if kind != MSG_HELLO_ACK:
        raise ProtocolError(f"not a HELLO_ACK ({kind})")
    if len(data) < HEADER_SIZE + 3:
        raise ProtocolError("HELLO_ACK truncated")
    result, size = struct.unpack_from("<BH", data, HEADER_SIZE)
    note = data[HEADER_SIZE + 3:HEADER_SIZE + 3 + size].decode("utf-8", "replace")
    return result, note


def build_ping(kind, seq):
    return build_header(kind) + struct.pack("<I", seq & 0xFFFFFFFF)


def parse_ping(data):
    kind, _ = parse_header(data)
    if kind not in (MSG_PING, MSG_PONG):
        raise ProtocolError(f"not a PING/PONG ({kind})")
    if len(data) < HEADER_SIZE + 4:
        raise ProtocolError("PING truncated")
    (seq,) = struct.unpack_from("<I", data, HEADER_SIZE)
    return kind, seq


def build_data(chunks):
    """`[(索引, UdpPacket), …] -> bytes`。**调用方保证索引升序。**

    超过 `MAX_DATAGRAM` 时**从最老的那一份开始扔** —— 冗余是可选的，
    当前这一份不能丢。
    """
    items = list(chunks)
    while True:
        body = b"".join(CHUNK_HEADER.pack(index & 0xFFFFFFFF, len(pkt)) + pkt
                        for index, pkt in items)
        if len(body) + HEADER_SIZE <= MAX_DATAGRAM or len(items) <= 1:
            break
        items = items[1:]
    return build_header(MSG_DATA, len(items)) + body


def parse_data(data):
    """`bytes -> [(索引, UdpPacket), …]`。"""
    kind, count = parse_header(data)
    if kind != MSG_DATA:
        raise ProtocolError(f"not a DATA ({kind})")
    out = []
    pos = HEADER_SIZE
    for _ in range(count):
        if pos + CHUNK_HEADER.size > len(data):
            raise ProtocolError("DATA chunk header truncated")
        index, size = CHUNK_HEADER.unpack_from(data, pos)
        pos += CHUNK_HEADER.size
        if pos + size > len(data):
            raise ProtocolError("DATA chunk body truncated")
        out.append((index, data[pos:pos + size]))
        pos += size
    return out


def peer_opcode(udp_packet):
    """`UdpPacket` 的内层 opcode（`+10` u16）。短得读不出来就返回 `None`。"""
    if len(udp_packet) < PEER_OPCODE_OFFSET + 2:
        return None
    return int.from_bytes(
        udp_packet[PEER_OPCODE_OFFSET:PEER_OPCODE_OFFSET + 2], "little")


def is_heartbeat(udp_packet):
    """这一份是不是「位置心跳」—— **唯一允许走 UDP 的东西**（铁律 1）。"""
    return peer_opcode(udp_packet) == OPCODE_HEARTBEAT


def peer_sequence(udp_packet):
    """事件包的序号（`+8` u16）。**只对内层 `< 0x4000` 有意义。**"""
    end = PEER_SEQUENCE_OFFSET + 2
    if len(udp_packet) < end:
        return None
    return int.from_bytes(udp_packet[PEER_SEQUENCE_OFFSET:end], "little")


def heartbeat_next_event_seq(udp_packet):
    """心跳里的 N = 发送方「下一个事件包序号」（§216）。不是心跳就返回 `None`。

    ★ 铁律 3 的判据就是拿它和「已经转发了多少发事件」比。
    """
    if not is_heartbeat(udp_packet):
        return None
    end = PEER_HEARTBEAT_N_OFFSET + 2
    if len(udp_packet) < end:
        return None
    return int.from_bytes(udp_packet[PEER_HEARTBEAT_N_OFFSET:end], "little")


def heartbeat_position(udp_packet):
    """心跳里那个**角色位置** `(x, y)`；不是心跳 / 太短就返回 ``None``。

    这是服务端唯一能知道「谁现在站在哪」的地方 —— 位置只在这一发里，
    别的包都没有（`0x0406` 那个是掉落点，只在打死东西时才有）。

    ★ 拿它干什么：bot 的落脚点得是**地图上真实存在的地面**，而服务端一点
    地图几何都没有（M4 才有）。真人此刻站着的位置一定合法，所以 bot 的
    锚点直接跟着真人走（V0.3 D16）。M5 的瞄准也要靠它。

    坐标是 i16（客户端 `0x5f895c` 从 f32 四舍五入来的），**不是** f32。
    """
    if not is_heartbeat(udp_packet):
        return None
    end = PEER_HEARTBEAT_STATE_OFFSET + PEER_HEARTBEAT_POS.size
    if len(udp_packet) < end:
        return None
    return PEER_HEARTBEAT_POS.unpack_from(
        udp_packet, PEER_HEARTBEAT_STATE_OFFSET)


def heartbeat_motion(udp_packet):
    """心跳里的**整套运动状态** `(x, y, on_ground, vx, vy, fast_run)`；
    不是心跳就 ``None``。

    比 `heartbeat_position()` 多出来的四个量，是 bot **回放**这段轨迹时必须
    原样抄的（V0.3 §35 / §40）：

    * `on_ground` —— 位域 bit2。收方拿它分流位置更新（`0x504215`：上一发
      离地、这一发落地 ⇒ **硬置**坐标，否则 0.6/0.4 插值），角色的**姿势**
      也跟着它走。**报错了就没有走路动画。**
    * `vx` / `vy` —— 空中的抛体速度。★ 踩在地上时它俩**必须是 0**：收方会
      拿它自己往前推算，和心跳里的坐标一打架就是「走一步停一下」的抽搐
      （用户 2026-08-26 第二轮实机报的那个症状）。
    * `fast_run` —— 位域 bit3 = **冲刺**（§40）。真人按着右键跑的那一段，
      bot 抄的坐标本来就是 1.5 倍步长的；不跟着报这一位，收方只会按普通
      走速替它挪，心跳再一发发把它拽回来 —— 表现是「跟不上 + 拉扯」，
      腿的动画速率也不对。

    ★ 为什么是「抄真人的」而不是「服务端自己算」：bot 走的就是真人刚走过的
    那条线（D16），真人在那一段是踩地还是腾空是**现成的事实**，服务端手上
    就有。自己算就要连客户端的重力和碰撞一起复刻 —— 那正是 D16 要绕开的。
    """
    if not is_heartbeat(udp_packet):
        return None
    end = PEER_HEARTBEAT_FIELD_OFFSET + 4
    if len(udp_packet) < end:
        return None
    x, y = PEER_HEARTBEAT_POS.unpack_from(
        udp_packet, PEER_HEARTBEAT_STATE_OFFSET)
    vx, vy = PEER_HEARTBEAT_VELOCITY.unpack_from(
        udp_packet, PEER_HEARTBEAT_VELOCITY_OFFSET)
    field = struct.unpack_from("<i", udp_packet,
                               PEER_HEARTBEAT_FIELD_OFFSET)[0]
    return (x, y, bool(field & PEER_HEARTBEAT_BIT_ONGROUND), vx, vy,
            bool(field & PEER_HEARTBEAT_BIT_FASTRUN))


def heartbeat_keys(udp_packet):
    """心跳里的**方向键掩码**（角色状态结构 `+0x10`，§39）；不是心跳就 ``None``。

    ★★ 为什么服务端要读它（D106）：收方对**远端角色**是拿这个掩码
    **每 32 ms 替它走一步**的（`0x507660`），心跳只是每 128 ms 纠一次偏。
    服务端替 bot 判命中时说的「这个人在哪」必须是同一个口径 —— 否则拿
    128 ms 前那一发心跳的坐标去撞此刻的弹体，跳起来 / 被顶飞的那几发
    根本判不准（旧 §96 拿**事后插值**补过这一课，但那要等下一发心跳才算得
    出来，而 `rpExplode` 迟到一格就是永久错账，§147）。
    """
    if not is_heartbeat(udp_packet):
        return None
    end = PEER_HEARTBEAT_KEYS_OFFSET + 2
    if len(udp_packet) < end:
        return None
    return struct.unpack_from("<H", udp_packet, PEER_HEARTBEAT_KEYS_OFFSET)[0]


def as_broadcast(udp_packet):
    """把「目标座位」（`+2`）改回 `0xff` 广播。

    ★ 为什么要有这一步：原版通道 B（UDP 直连）在**每个座位各发一份**之前会把
    `hdr[2]` 改写成那个座位号（§149 的 `packet.hdr[2] = i`），而通道 A 是 `0xff`。
    收方 `0x4078dd` 会按这个字节判「这包是不是发给我的」，盖错了就整包丢。
    我们是一份包发给全房间，所以投递前必须是 `0xff`。

    校验和（`+6`）从 `+0x0c` 起算、**不覆盖这个字节**，所以改它不用重算
    （和 `relayserver.restamp_peer_game_id` 改 `+4` 是同一个道理）。
    """
    if len(udp_packet) <= PEER_TARGET_OFFSET:
        return udp_packet
    if udp_packet[PEER_TARGET_OFFSET] == PEER_BROADCAST_SEAT:
        return udp_packet
    patched = bytearray(udp_packet)
    patched[PEER_TARGET_OFFSET] = PEER_BROADCAST_SEAT
    return bytes(patched)


class HeartbeatOrder:
    """一条同步流的**排序闸门** —— 铁律 3 和铁律 2 的去重都在这里。

    上行（服务端侧）和下行（`relay.py` 侧）用的是同一套规则，所以做成一个类。

    状态：

    * `high_water`  已经放行过的最大索引 + 1；
    * `tcp_seen`    从 TCP 到达的心跳数 —— 它**就是** `bshook` 盖的索引，
                    因为两边数的是同一件事「这条连接上第几发内层 0x4001」，
                    而 TCP 有序、不丢；
    * `events`      **本代**已经放行的事件数（= 已放行的最大 `seq` + 1）。

    ★ 换代时**只清 `events`**，索引那两个计数器一路数到连接断开为止 ——
    `bshook` 那边的索引也是按连接数的，两边必须用同一个起点。发送方
    `ResetQueues` 会把自己的事件序号清回 0，所以 `events` 必须跟着清（§216 三）。
    """

    def __init__(self):
        self.reset()

    def reset(self):
        """整条连接从头开始（建连接时）。"""
        self.high_water = 0
        self.tcp_seen = 0
        self.events = 0
        self.last_udp_at = None
        # 统计用（打进 `[online-debug]`，是判断「UDP 到底有没有在干活」的唯一依据）
        self.udp_taken = 0
        self.tcp_taken = 0
        self.udp_stale = 0
        self.udp_held = 0
        self.reanchors = 0

    def new_epoch(self):
        """换代（局号变了）：发送方的事件序号回到 0，我们的账也要跟着回。"""
        self.events = 0

    # -- 事件包（永远走 TCP，只在这里记账） -------------------------------
    def note_event(self, seq):
        """记下「事件 `seq` 已经转发出去了」。铁律 3 的第 2 条判据靠它。"""
        if seq is None:
            return
        if seq + 1 > self.events:
            self.events = seq + 1

    # -- 心跳 --------------------------------------------------------------
    def take_udp(self, index, udp_packet, now=None):
        """UDP 来的一发心跳该不该放行。

        两条判据**都**要过（本文件开头铁律 3）：旧的不要、会越过事件的不要。
        """
        if index < self.high_water:
            self.udp_stale += 1
            return False
        want = heartbeat_next_event_seq(udp_packet)
        if want is not None and want > self.events:
            # ★ 这一发的 N 比我们转发过的事件数还大 —— 放行就等于让收方
            #   `FlushTo(N)` 把还在 TCP 路上的事件包判死（§217）。让它等 TCP。
            self.udp_held += 1
            return False
        self.high_water = index + 1
        self.udp_taken += 1
        self.last_udp_at = time.monotonic() if now is None else now
        return True

    def take_tcp(self, udp_packet=None, now=None):
        """TCP 来的一发心跳该不该放行。**索引由本侧自己数**。

        ★ 「影子」判定带一个自愈出口：UDP 已经安静 `UDP_QUIET_S` 秒了，
        就说明那条路断了 / 两边索引对不上，此时**以 TCP 为准重新起算** ——
        否则水位会永远停在 UDP 最后跑到的地方，位置更新整个卡死。
        """
        index = self.tcp_seen
        self.tcp_seen += 1
        if index < self.high_water:
            now = time.monotonic() if now is None else now
            if (self.last_udp_at is None
                    or now - self.last_udp_at <= UDP_QUIET_S):
                # UDP 那一份刚刚先送过了，这一份是它的影子，丢掉。
                return False
            self.reanchors += 1
            self.last_udp_at = None
        self.high_water = index + 1
        self.tcp_taken += 1
        return True

    def summary(self):
        """给 `[online-debug]` 用的一行摘要。全 0 时返回 `None`（不打）。"""
        total = self.udp_taken + self.tcp_taken
        if not total:
            return None
        return (f"UDP 抢先 {self.udp_taken}/{total} "
                f"({self.udp_taken * 100.0 / total:.0f}%)"
                f" 过期丢弃 {self.udp_stale} 等事件 {self.udp_held}"
                + (f" ★重锚 {self.reanchors}" if self.reanchors else ""))


class Endpoint:
    """一条**已经认出来是谁**的 UDP 流。"""

    __slots__ = ("game_conn", "addr", "last_seen", "created_at",
                 "downlink_ok", "out_index", "recent", "last_n",
                 "tcp_at", "flips", "flip_gap_min")

    def __init__(self, game_conn, addr, now):
        self.game_conn = game_conn
        self.addr = addr
        self.last_seen = now
        self.created_at = now
        #: 对方自报「我这边 7788 能收」（`HELLO_FLAG_DOWNLINK`）。
        #: 自报之前一律走 TCP —— 这就是下行的全部准入条件。
        self.downlink_ok = False
        #: 下行的索引和最近几份（冗余捎带用）。
        self.out_index = 0
        self.recent = []
        #: `发送方 id -> 已经送给本收件人的最新 N`。铁律 3 的下行版本靠它。
        self.last_n = {}
        #: ★★ 换路倒序的**曝光计量**（V0.3 §154）。`发送方 id -> 上一发被
        #: 逼回 TCP 的时刻`；`flips` = 「TCP 之后紧接着一发走 UDP」发生过几次；
        #: `flip_gap_min` = 这两发之间**最短**的间隔（秒）。
        #:
        #: 危险的形状只有这一种：先发的那一发走了慢的 TCP、后发的那一发走了
        #: 快的 UDP，后发的先到 —— 心跳没有任何可判新旧的原版字段，收方拦不住，
        #: 角色被拉回上一发的位置（用户 2026-09-01：「位置跳来跳去」）。
        #: 间隔比两条路的时延差还小时才可能翻车，所以量的就是这个间隔。
        #: ★ 只计数不改行为：这一版先把事实量出来，够不够危险由数据说了算。
        self.tcp_at = {}
        self.flips = 0
        self.flip_gap_min = None

    def alive(self, now):
        return (now - self.last_seen) <= DEAD_AFTER_S


class UdpSyncServer:
    """位置数据的 UDP 收发端（服务端侧）。

    和 `relayserver.RelayServer` 一样是**模块级单例 + 回调注入**：本文件
    **不 import `gameserver`**，谁是谁、房里有谁全靠注入进来的回调回答。
    """

    def __init__(self, *, conn_for_ticket=None, ticket_known=None, logger=None,
                 redundancy=2, enabled=True):
        #: `票据 -> 游戏连接`。由 `gameserver` 注入。
        self._conn_for_ticket = conn_for_ticket
        #: `票据 -> 这张票是不是我们签发的且还没过期`（**不管有没有人认领**）。
        #: 由 `app.py` 注入（票据表是它建的）。没注入时退化成「查不到连接
        #: 就一律回 `ACK_BAD_TICKET`」= 本条改动之前的行为。
        self._ticket_known = ticket_known
        self._log = logger or (lambda msg: None)
        self.redundancy = int(redundancy)
        self.enabled = bool(enabled)
        self.port = server_config.UDP_SYNC_PORT
        self.sock = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        #: `(ip, port) -> Endpoint`，以及 `游戏连接 -> Endpoint` 的反查。
        self._by_addr = {}
        self._by_conn = {}
        #: 整体计数（启动横幅之外唯一的可观测量）。
        self.datagrams_in = 0
        self.datagrams_out = 0
        self.unknown_in = 0

    # -- 注入 ---------------------------------------------------------------
    def bind_lookup(self, conn_for_ticket=None, logger=None,
                    ticket_known=None):
        """注入「票据 -> 游戏连接」和日志出口（`gameserver` 在 import 时调一次）。

        `ticket_known`（可选，`app.py` 建好票据表之后再补一次）用来把
        「还没登进游戏服」和「根本不认识这张票」分开，见 `ACK_NOT_LOGGED_IN`。
        """
        if conn_for_ticket is not None:
            self._conn_for_ticket = conn_for_ticket
        if logger is not None:
            self._log = logger
        if ticket_known is not None:
            self._ticket_known = ticket_known

    def log(self, msg):
        try:
            self._log(msg)
        except Exception:                      # noqa: BLE001 —— 日志不许弄崩主路
            pass

    # -- 注册表 -------------------------------------------------------------
    def endpoint_for(self, game_conn):
        with self._lock:
            endpoint = self._by_conn.get(id(game_conn))
        # ★ 表是按 `id()` 索引的，而 `id()` 会被回收后重用。多校验一次对象本身，
        #   免得一条早就没了的连接把新连接的位置数据引到旧地址上去。
        if endpoint is not None and endpoint.game_conn is not game_conn:
            return None
        return endpoint

    def ready_for(self, game_conn):
        """这个收件人现在能不能收下行 UDP。

        ★ 三条**都**要成立：通道开着、流还活着、对方自报 7788 收得到。
        任何一条不成立都退回 `0x040f`（TCP）—— 这就是全部的「回退逻辑」。
        """
        if not self.enabled or self.sock is None:
            return False
        endpoint = self.endpoint_for(game_conn)
        if endpoint is None or not endpoint.downlink_ok:
            return False
        return endpoint.alive(time.monotonic())

    def may_send_heartbeat(self, member, sender, udp_packet):
        """★★★ 下行选路的判据（铁律 3 的下行版本）。**改这里前先读完这段。**

        危险在哪：心跳里的 N 会让收方在**队列还没激活时**做 `FlushTo(N)`，
        把 `base` 钉到 N 且只进不退，之后 `seq < base` 的事件包被静默丢弃 ——
        整局零伤害（§217）。UDP 比 TCP 快 300~400 ms，一发心跳要是越过了
        还在 TCP 路上的事件包，就正好构成这个死法。

        判据只有一条，但它把问题变成了**可证明**的：

            **只有「N 和上一发送给他的心跳相同」的心跳才走 UDP。**
            N 一变，那一发必须走 TCP。

        为什么这就够了：

        * N 变大的那一发走 TCP ⇒ 它**排在**推高 N 的那些事件包后面（同一条
          有序流），和今天的行为逐字节相同；
        * 之后 N 不变的那些心跳，`FlushTo(N)` / `Grow(N-1)` 对同一个 N 是
          幂等的 —— 早到晚到、到几次，收方队列的状态**完全一样**。
          所以它们怎么乱序都不可能造成 §217 那个错位。
        * 换代时 N 回到 0（≠ 上一代的值）⇒ 新一代第一发自动走 TCP，
          「复位后第一发心跳定基线」那一发因此永远是有序的那一份。

        代价：实测相邻心跳 N 相同的比例是 **88.3%**，所以约 12% 的心跳
        （刚开完枪的那一两发）退回 TCP，其余照走 UDP。
        **拿 12% 换「可证明不会错位」，这个价值得付。**
        """
        want = heartbeat_next_event_seq(udp_packet)
        if want is None:
            return False
        endpoint = self.endpoint_for(member)
        if endpoint is None:
            return False
        key = id(sender)
        if endpoint.last_n.get(key) == want:
            # ★ 上一发刚被逼回 TCP、这一发走 UDP —— 就是「换路」那一下
            #   （V0.3 §154）。量它，不改行为，见 `Endpoint.tcp_at`。
            since = endpoint.tcp_at.pop(key, None)
            if since is not None:
                gap = time.monotonic() - since
                endpoint.flips += 1
                if endpoint.flip_gap_min is None or gap < endpoint.flip_gap_min:
                    endpoint.flip_gap_min = gap
            return True
        # N 变了：这一发交给 TCP，并记下新值 —— 下一发起就能走 UDP 了。
        endpoint.last_n[key] = want
        endpoint.tcp_at[key] = time.monotonic()
        return False

    def forget(self, game_conn):
        """游戏连接断开时把这条流忘掉。"""
        with self._lock:
            endpoint = self._by_conn.pop(id(game_conn), None)
            if endpoint is not None:
                self._by_addr.pop(endpoint.addr, None)
        if endpoint is not None and endpoint.flips:
            # ★ 换路的曝光量，走的时候报一次（V0.3 §154）。间隔越短越危险 ——
            #   先发的那一发走慢的 TCP、后发的走快的 UDP，后发的先到就会把
            #   角色拉回旧位置，而心跳没有可判新旧的字段，收方拦不住。
            gap = endpoint.flip_gap_min
            self.log(f"位置UDP  这条流一共 {endpoint.flips} 次「TCP 之后紧接着走"
                     f" UDP」，最短间隔 "
                     f"{'—' if gap is None else '%.0f ms' % (gap * 1000.0)}"
                     f"（间隔小于两条路的时延差时，后发的那一发会先到）")

    def _prune(self, now):
        with self._lock:
            dead = [addr for addr, ep in self._by_addr.items()
                    if now - ep.last_seen > ENDPOINT_TTL_S]
            for addr in dead:
                endpoint = self._by_addr.pop(addr, None)
                if endpoint is not None:
                    self._by_conn.pop(id(endpoint.game_conn), None)

    # -- 下行 ---------------------------------------------------------------
    def send_to(self, game_conn, udp_packet):
        """把一份位置数据从 UDP 发给这个收件人。送不出去返回 `False`。

        返回 `False` 时调用方（`RelayServer.deliver`）必须走 TCP 那条路 ——
        **不能两条都走**（铁律 4）。
        """
        endpoint = self.endpoint_for(game_conn)
        if endpoint is None or self.sock is None:
            return False
        packet = as_broadcast(udp_packet)
        with self._lock:
            index = endpoint.out_index
            endpoint.out_index += 1
            endpoint.recent.append((index, packet))
            if len(endpoint.recent) > self.redundancy + 1:
                del endpoint.recent[0:len(endpoint.recent) - (self.redundancy + 1)]
            chunks = list(endpoint.recent)
            addr = endpoint.addr
        try:
            self.sock.sendto(build_data(chunks), addr)
        except OSError:
            # 发不出去（对端 ICMP 不可达之类）**不是错误**：调用方会改走 TCP，
            # 下一发心跳也会因为 `alive()` 过期而自动落回 TCP。
            # ★ 但必须把这一份**从冗余缓冲里撤掉** —— 留着的话它会被后面的
            #   数据报捎带出去，而调用方此刻已经把同一份从 TCP 送走了：
            #   同一发心跳走了两条路，晚到的那份会把角色拉回旧位置（铁律 4）。
            with self._lock:
                endpoint.recent = [item for item in endpoint.recent
                                   if item[0] != index]
            return False
        self.datagrams_out += 1
        return True

    # -- 上行 ---------------------------------------------------------------
    def _on_hello(self, data, addr, now):
        try:
            ticket, flags = parse_hello_full(data)
        except ProtocolError:
            return
        if not self.enabled:
            self._reply(build_hello_ack(ACK_DISABLED, "服务端关掉了 UDP 同步"), addr)
            return
        game_conn = None
        if self._conn_for_ticket is not None:
            try:
                game_conn = self._conn_for_ticket(ticket)
            except Exception as error:         # noqa: BLE001 —— 查不到就是查不到
                self.log(f"!! 票据查连接抛了 {error!r}")
                game_conn = None
        if game_conn is None:
            # ★ 分两种，而且是按**事件**分的，不是按「第几发」分的：
            #   * 票据表认得这张票 -> 登录包还在路上（`bshook` 发 HELLO 的时刻
            #     必然早于游戏服写下 `login_ticket` 的时刻）。**这不是错误**，
            #     回 `ACK_NOT_LOGGED_IN`，中继安静等下一发。
            #   * 票据表也不认得 -> 服务端重启过 / 过期 / 被顶号。这才值得
            #     告诉玩家一声。
            #   没注入 `ticket_known` 时（单独跑 gameserver.py 做协议试探）
            #   退化成原来的行为：一律 `ACK_BAD_TICKET`。
            if self._ticket_known is not None:
                try:
                    pending = bool(self._ticket_known(ticket))
                except Exception as error:     # noqa: BLE001 —— 查不到就是查不到
                    self.log(f"!! 票据查有效性抛了 {error!r}")
                    pending = False
                if pending:
                    self._reply(build_hello_ack(ACK_NOT_LOGGED_IN,
                                                "票据还没登进游戏服"), addr)
                    return
            # ★ 不说「票据不对」以外的任何细节：这个端口谁都能发包，
            #   多说一句就是多一个探测面。
            self._reply(build_hello_ack(ACK_BAD_TICKET, "认不出票据"), addr)
            return
        with self._lock:
            old = self._by_conn.get(id(game_conn))
            if old is not None and old.addr != addr:
                self._by_addr.pop(old.addr, None)
            endpoint = self._by_addr.get(addr)
            if endpoint is None or endpoint.game_conn is not game_conn:
                endpoint = Endpoint(game_conn, addr, now)
            endpoint.last_seen = now
            # ★ 下行只在**对方自证过**之后才开。它自证的方式是去 bind 7788：
            #   绑得上 = 游戏没绑 = 投过去没人收（见 relay.py 的 downlink_probe）。
            #   自证不了就一直走 `0x040f`，和今天完全一样。
            was_ready = endpoint.downlink_ok
            endpoint.downlink_ok = bool(flags & HELLO_FLAG_DOWNLINK)
            self._by_addr[addr] = endpoint
            self._by_conn[id(game_conn)] = endpoint
        self._reply(build_hello_ack(ACK_OK, ""), addr)
        # ★ 只在「第一次认出来」和「下行可用性变了」时打一行。
        #   HELLO 是会重发的（ACK 丢了就 2 秒一发），逐发打会把日志刷爆。
        if old is None or was_ready != endpoint.downlink_ok:
            who = getattr(game_conn, "account_name", None) or "?"
            self.log(f"UDP 同步 ✓ 认出 账号={who!r} 来自 {addr[0]}:{addr[1]}"
                     f"（下行 {'开' if endpoint.downlink_ok else '关，走 TCP'}）")

    def _on_data(self, data, addr, now):
        with self._lock:
            endpoint = self._by_addr.get(addr)
            if endpoint is not None:
                endpoint.last_seen = now
        if endpoint is None:
            # 还没 HELLO 过（或者服务端重启过）。丢掉即可 —— TCP 那一份
            # 从来没停过，玩家什么都不会察觉；对方保活超时后会重发 HELLO。
            self.unknown_in += 1
            return
        try:
            chunks = parse_data(data)
        except ProtocolError:
            return
        conn = endpoint.game_conn
        feed = getattr(conn, "feed_peer_udp", None)
        if feed is None:
            return
        for index, packet in chunks:
            if not is_heartbeat(packet):
                # 铁律 1：非心跳一律不认。**不是**「转发一下也无妨」——
                # 事件包走 UDP 就等于把可靠通道变成不可靠的，那正是 §217 的死法。
                continue
            try:
                feed(index, packet)
            except Exception as error:         # noqa: BLE001 —— 单份坏了不许带崩整条
                self.log(f"!! 喂 UDP 心跳抛了 {error!r}")

    def _reply(self, data, addr):
        if self.sock is None:
            return
        try:
            self.sock.sendto(data, addr)
        except OSError:
            pass

    def _handle(self, data, addr, now):
        self.datagrams_in += 1
        try:
            kind, _ = parse_header(data)
        except ProtocolError:
            # 不是我们的包。**什么都不回** —— 这个端口在公网上，
            # 回任何东西都是一个反射放大面。
            return
        if kind == MSG_DATA:
            self._on_data(data, addr, now)
        elif kind == MSG_HELLO:
            self._on_hello(data, addr, now)
        elif kind == MSG_PING:
            with self._lock:
                endpoint = self._by_addr.get(addr)
                if endpoint is not None:
                    endpoint.last_seen = now
            try:
                _, seq = parse_ping(data)
            except ProtocolError:
                return
            self._reply(build_ping(MSG_PONG, seq), addr)

    # -- 监听 ---------------------------------------------------------------
    def create_socket(self, host="::"):
        """建好 UDP socket。和 `netlisten.create_listener` 同一套双栈规矩（D063）。"""
        if host in ANY_HOSTS:
            try:
                sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                sock.bind(("::", self.port))
                return sock
            except OSError as error:
                # ★★ 端口被占用时**绝不**退回 IPv4 再试 —— 见
                #    `netlisten.create_listener` 里那段同样的说明：
                #    Windows 上双栈 `[::]:P` 和 `0.0.0.0:P` 能同时绑上，
                #    退回等于把「被占用」变成「悄悄起来了但收不到包」。
                #    回退只服务「这台机器没开 IPv6」，那不是 EADDRINUSE。
                if error.errno == errno.EADDRINUSE:
                    raise
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind(("0.0.0.0", self.port))
                return sock
        family = (socket.AF_INET6 if ":" in str(host) else socket.AF_INET)
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.bind((host, self.port))
        return sock

    def serve(self, host="::", ready=None):
        self.sock = self.create_socket(host)
        if ready is not None:
            ready.set()
        last_prune = time.monotonic()
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(MAX_DATAGRAM * 2)
            except OSError:
                if self._stop.is_set():
                    break
                # Windows 上一个 ICMP「端口不可达」会让**下一次** recvfrom 报错
                # （WSAECONNRESET），这在 UDP 上完全正常，继续收就是了。
                continue
            now = time.monotonic()
            try:
                self._handle(data, addr, now)
            except Exception as error:         # noqa: BLE001 —— 收包循环必须不死
                self.log(f"!! 处理 UDP 数据报抛了 {error!r}")
            if now - last_prune > ENDPOINT_TTL_S:
                last_prune = now
                self._prune(now)

    def stop(self):
        self._stop.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass


#: 全进程唯一的一份。`gameserver` 负责注入查连接的回调，`app.py` 负责起监听。
SERVER = UdpSyncServer()
