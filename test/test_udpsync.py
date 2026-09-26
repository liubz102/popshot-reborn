#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""位置数据 UDP 旁路的测试（`server/udpsync.py`）。

依据在 `udpsync.py` 开头那四条铁律，以及 FINDINGS §151 / §187 / §216 / §217。

**这里最要紧的不是「UDP 能不能通」，是「排序闸门有没有漏」** ——
放行一发不该放行的心跳，代价是整局打不死人（§217），而那种 bug 在
真机上要两台机器打满一局才看得见。所以判据全部钉在这里。
"""
import errno
import os
import re
import socket
import sys
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import config as server_config                                # noqa: E402
import udpsync                                                 # noqa: E402
from udpsync import (ACK_BAD_TICKET, ACK_NOT_LOGGED_IN,        # noqa: E402
                     ACK_OK, HeartbeatOrder,
                     MSG_DATA, MSG_HELLO, MSG_HELLO_ACK, MSG_PING,
                     MSG_PONG, ProtocolError, UdpSyncServer,
                     as_broadcast, build_data, build_hello,
                     build_hello_ack, build_header, build_ping,
                     heartbeat_next_event_seq, is_heartbeat, parse_data,
                     parse_header, parse_hello, parse_hello_ack, parse_ping,
                     peer_opcode, peer_sequence)


# ----------------------------------------------------------------------------
# 造包：一份真实形状的 UdpPacket（§151 的 12 字节头）
# ----------------------------------------------------------------------------
def make_peer(opcode=0x4001, *, seat=2, target=0xFF, game_id=0,
              sequence=0, next_event=0, body_tail=b"\x00" * 29):
    """一份 `UdpPacket`。

    真实形状取自 bug调查/9 的抓包：
    `ff 02 ff 00 | 00 00 | c7 b1 | 00 00 | 01 40 | 01 00 …`
    —— 心跳 body 的头 2 字节就是「下一个事件包序号 N」（§216）。
    """
    head = bytes((0xFF, seat & 0xFF, target & 0xFF, 0))
    head += game_id.to_bytes(2, "little")
    head += b"\xc7\xb1"                       # 校验和：本模块从不重算，随便填
    head += sequence.to_bytes(2, "little")
    head += opcode.to_bytes(2, "little")
    if opcode >= 0x4000:
        body = next_event.to_bytes(2, "little") + body_tail
    else:
        body = body_tail
    return head + body


def heartbeat(n=0, **kw):
    return make_peer(0x4001, next_event=n, **kw)


def event(seq, opcode=0x0002, **kw):
    return make_peer(opcode, sequence=seq, **kw)


# ----------------------------------------------------------------------------
# 线格式
# ----------------------------------------------------------------------------
class WireFormatTests(unittest.TestCase):
    def test_header_round_trip(self):
        self.assertEqual(parse_header(build_header(MSG_DATA, 3)), (MSG_DATA, 3))

    def test_a_foreign_datagram_is_rejected_not_misparsed(self):
        """★ 这个端口在公网上，谁都能往里发字节。

        魔数/版本对不上必须**当场拒绝**，不能按我们的格式硬解出垃圾来。
        """
        for junk in (b"", b"abc", b"PSU", b"PSU\x02" + b"\x00" * 8,
                     b"\x00" * 16):
            with self.assertRaises(ProtocolError):
                parse_header(junk)

    def test_hello_carries_the_ticket(self):
        data = build_hello("71e893864a140db5589cbf95b6dff820")
        self.assertEqual(parse_header(data)[0], MSG_HELLO)
        self.assertEqual(parse_hello(data), "71e893864a140db5589cbf95b6dff820")

    def test_hello_ack_carries_result_and_note(self):
        result, note = parse_hello_ack(build_hello_ack(ACK_BAD_TICKET, "认不出票据"))
        self.assertEqual(result, ACK_BAD_TICKET)
        self.assertEqual(note, "认不出票据")

    def test_ping_round_trip(self):
        self.assertEqual(parse_ping(build_ping(MSG_PONG, 42)), (MSG_PONG, 42))

    def test_data_round_trip_keeps_every_byte_of_the_packet(self):
        """★ 转发的载荷是**一整个 UdpPacket**，一个字节都不许动（§149）。"""
        chunks = [(7, heartbeat(3)), (8, heartbeat(3)), (9, heartbeat(4))]
        self.assertEqual(parse_data(build_data(chunks)), chunks)

    def test_data_drops_the_oldest_copies_when_it_would_be_fragmented(self):
        """超长时从**最老**的那份开始扔 —— 当前这份任何时候都不能丢。

        IP 分片一丢就是整报丢，那等于把冗余的意义反过来用。
        """
        big = b"\xff" * 500
        chunks = [(i, big) for i in range(10)]
        parsed = parse_data(build_data(chunks))
        self.assertLessEqual(len(build_data(chunks)), udpsync.MAX_DATAGRAM)
        self.assertEqual(parsed[-1][0], 9)          # 当前这份还在
        self.assertLess(len(parsed), 10)            # 老的被扔了

    def test_a_truncated_data_body_is_rejected(self):
        good = build_data([(1, heartbeat())])
        with self.assertRaises(ProtocolError):
            parse_data(good[:-5])


# ----------------------------------------------------------------------------
# UdpPacket 的几个读取器
# ----------------------------------------------------------------------------
class PeerPacketTests(unittest.TestCase):
    def test_inner_opcode_and_heartbeat_detection(self):
        self.assertEqual(peer_opcode(heartbeat()), 0x4001)
        self.assertTrue(is_heartbeat(heartbeat()))
        self.assertFalse(is_heartbeat(event(0)))
        self.assertIsNone(peer_opcode(b"\xff\x02"))

    def test_the_heartbeat_carries_N_in_the_body_not_in_the_header(self):
        """★★ §216：N 在 body 头 2 字节（`+12..13`），**不是**头里的 `+8`。

        实测（bug调查/9 的 4527 发心跳）头 `+8` 的 u16 对心跳**恒为 0**，
        只有 1 个取值 —— 拿它当心跳的新旧判据是错的，这条用例钉住这一点。
        """
        pkt = heartbeat(43)
        self.assertEqual(heartbeat_next_event_seq(pkt), 43)
        self.assertEqual(peer_sequence(pkt), 0)

    def test_events_carry_their_sequence_in_the_header(self):
        self.assertEqual(peer_sequence(event(31)), 31)
        self.assertIsNone(heartbeat_next_event_seq(event(31)))

    def test_as_broadcast_restores_the_target_seat_byte(self):
        """原版通道 B 每座位各发一份、把 `+2` 改成座位号（§149）。

        我们是一份发全房间，投递前必须改回 `0xff`，否则收方
        `0x4078dd` 会判「这包不是发给我的」整包丢掉。
        """
        unicast = heartbeat(target=3)
        fixed = as_broadcast(unicast)
        self.assertEqual(fixed[2], 0xFF)
        # 其余字节一个都不许动（校验和从 +0x0c 起算，不覆盖 +2，所以不用重算）
        self.assertEqual(fixed[:2] + fixed[3:], unicast[:2] + unicast[3:])
        # 已经是广播时原样返回（连拷贝都不做）
        already = heartbeat()
        self.assertIs(as_broadcast(already), already)


# ----------------------------------------------------------------------------
# ★★ 排序闸门 —— 本文件的重点
# ----------------------------------------------------------------------------
class HeartbeatOrderTests(unittest.TestCase):
    def setUp(self):
        self.order = HeartbeatOrder()

    def test_udp_wins_and_the_tcp_shadow_is_dropped(self):
        """铁律 2：上行双发、谁先到用谁，**后到的那份必须丢**。

        两份都转的话，晚到的会把角色拉回旧位置 —— 心跳没有任何可判新旧的
        原版字段（实测 `+8` 恒为 0），客户端自己拦不住。
        """
        self.assertTrue(self.order.take_udp(0, heartbeat()))
        self.assertFalse(self.order.take_tcp(heartbeat()))   # 影子
        self.assertTrue(self.order.take_udp(1, heartbeat()))
        self.assertFalse(self.order.take_tcp(heartbeat()))
        self.assertEqual(self.order.udp_taken, 2)
        self.assertEqual(self.order.tcp_taken, 0)

    def test_tcp_wins_when_udp_never_arrives(self):
        """UDP 整条不通 = 今天的行为。**一行 fallback 代码都不需要。**"""
        for _ in range(5):
            self.assertTrue(self.order.take_tcp(heartbeat()))
        self.assertEqual(self.order.tcp_taken, 5)
        self.assertEqual(self.order.udp_taken, 0)

    def test_a_stale_udp_copy_is_dropped(self):
        self.assertTrue(self.order.take_tcp(heartbeat()))     # 索引 0
        self.assertTrue(self.order.take_tcp(heartbeat()))     # 索引 1
        self.assertFalse(self.order.take_udp(0, heartbeat()))  # 迟到的 0
        self.assertEqual(self.order.udp_stale, 1)

    def test_udp_may_skip_ahead_when_earlier_copies_were_lost(self):
        """位置是**快照** —— 中间几发丢了就丢了，最新的那发就是全部信息。"""
        self.assertTrue(self.order.take_udp(5, heartbeat()))
        # TCP 上迟到的 0..5 全部是影子
        for _ in range(6):
            self.assertFalse(self.order.take_tcp(heartbeat()))
        self.assertTrue(self.order.take_tcp(heartbeat()))      # 索引 6，轮到它了

    # -- ★★★ 铁律 3 ---------------------------------------------------------
    def test_a_udp_heartbeat_may_not_overtake_an_unforwarded_event(self):
        """★★★ 这条是整个模块最要紧的用例（§216 / §217）。

        心跳里的 N 会让收方做 `FlushTo(N)`，把 `base` 钉到 N 且**只进不退**；
        之后 `seq < base` 的事件包在 `PktQueue::Insert` 里被**静默丢弃**，
        连讨重传都不会讨。而 `rpFire` 里没有弹体句柄、收方要自己按同样顺序
        分配 —— 少喂一发，那台机器上这个座位的句柄分配器**永久错位**，
        之后每一发 `rpExplode` 都打在不存在的对象上：
        **伤害数字照出、血照掉一丝、就是打不死，一整局。**

        所以 N 比「已转发的事件数」大的心跳，UDP 上一律不放行，等 TCP。
        """
        # 还一发事件都没转发过 -> 只有 N=0 的心跳能走 UDP
        self.assertTrue(self.order.take_udp(0, heartbeat(0)))
        self.assertFalse(self.order.take_udp(1, heartbeat(1)))  # ★ 越过事件 0
        self.assertEqual(self.order.udp_held, 1)

        # 事件 0 从 TCP 到了、转发了 -> N=1 的心跳这才放行
        self.order.note_event(0)
        self.assertTrue(self.order.take_udp(2, heartbeat(1)))

    def test_the_held_heartbeat_still_gets_through_over_tcp(self):
        """被拦下的那发不是丢了 —— TCP 那份照常送到，只是慢一点。"""
        self.assertFalse(self.order.take_udp(3, heartbeat(2)))
        for _ in range(4):                       # 索引 0..3 从 TCP 走
            self.assertTrue(self.order.take_tcp(heartbeat(2)))
        self.assertEqual(self.order.tcp_taken, 4)

    def test_standing_still_lets_every_heartbeat_take_the_fast_path(self):
        """实测同一个 N 最长连续 315 发心跳（只跑不开枪）。

        ⇒ 绝大部分时间铁律 3 的判据是白给的，该省的都省到了。
        """
        for i in range(315):
            self.assertTrue(self.order.take_udp(i, heartbeat(0)))
        self.assertEqual(self.order.udp_held, 0)
        self.assertEqual(self.order.udp_taken, 315)

    # -- 换代 ---------------------------------------------------------------
    def test_a_new_epoch_resets_the_event_count_but_not_the_indexes(self):
        """★ 换代只清事件账。

        索引那两个计数器是**按连接**数的，`bshook` 那边也一样 ——
        一边清一边不清就直接失配了。而发送方 `ResetQueues` 会把自己的事件
        序号清回 0（§216 三），所以事件账必须跟着清。
        """
        self.order.note_event(30)
        self.assertTrue(self.order.take_udp(0, heartbeat(31)))
        self.order.new_epoch()
        self.assertEqual(self.order.events, 0)
        self.assertEqual(self.order.high_water, 1)      # 索引没被清
        # 新一代第一发心跳 N=0，照样能走 UDP（`FlushTo(0)` 是空操作）
        self.assertTrue(self.order.take_udp(1, heartbeat(0)))
        # 但新一代还没转发过事件时，N=1 的心跳要等
        self.assertFalse(self.order.take_udp(2, heartbeat(1)))

    # -- 自愈 ---------------------------------------------------------------
    def test_tcp_re_anchors_after_udp_goes_quiet(self):
        """★★ UDP 半路断掉时**位置更新绝不能整个卡死**。

        UDP 正常领先 300~400 ms，水位因此跑在 TCP 前面。这条路一断，
        TCP 那侧的编号还落在水位后面 —— 不自愈的话每一发都被当成影子丢掉，
        玩家在别人屏幕上会**彻底定住**（比原来的瞬移还糟）。
        所以安静超过 `UDP_QUIET_S` 就以 TCP 为准重新起算。
        """
        t = 1000.0
        for i in range(20):                       # UDP 跑到索引 19
            self.assertTrue(self.order.take_udp(i, heartbeat(), now=t))
        # UDP 在这里断了。TCP 还在 0 号上，先是影子（正常去重）
        self.assertFalse(self.order.take_tcp(heartbeat(), now=t + 0.1))
        # 安静两秒之后：重新以 TCP 为准
        self.assertTrue(self.order.take_tcp(heartbeat(),
                                            now=t + udpsync.UDP_QUIET_S + 0.1))
        self.assertEqual(self.order.reanchors, 1)
        # 之后 TCP 一路顺畅（不再被当影子）
        for _ in range(5):
            self.assertTrue(self.order.take_tcp(heartbeat(), now=t + 5))
        self.assertIn("重锚", self.order.summary())

    def test_a_udp_only_stream_never_re_anchors(self):
        """UDP 一路都在时不该有任何重锚 —— 重锚是故障出口，不是常态。"""
        t = 1000.0
        for i in range(200):
            self.assertTrue(self.order.take_udp(i, heartbeat(0), now=t + i * 0.128))
        self.assertEqual(self.order.reanchors, 0)
        self.assertEqual(self.order.udp_taken, 200)

    def test_summary_is_none_before_anything_flowed(self):
        self.assertIsNone(HeartbeatOrder().summary())


# ----------------------------------------------------------------------------
# 服务端：认人、投递、以及「什么不许走 UDP」
# ----------------------------------------------------------------------------
class FakeGameConn:
    def __init__(self, name="alice"):
        self.account_name = name
        self.fed = []

    def feed_peer_udp(self, index, packet):
        self.fed.append((index, packet))


class GatedGameConn(FakeGameConn):
    """带真排序闸门的假连接 —— 和 `gameserver.Conn.feed_peer_udp` 同构。"""

    def __init__(self, name="alice"):
        super().__init__(name)
        self.order = HeartbeatOrder()

    def feed_peer_udp(self, index, packet):
        if self.order.take_udp(index, packet):
            self.fed.append((index, packet))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeGameConn()
        self.replies = []
        self.server = UdpSyncServer(
            conn_for_ticket=lambda t: self.conn if t == "good" else None)
        self.server._reply = lambda data, addr: self.replies.append((data, addr))
        self.addr = ("203.0.113.9", 5000)

    def hello(self, ticket="good"):
        self.server._handle(build_hello(ticket), self.addr, time.monotonic())

    def test_a_good_ticket_is_recognised_and_acked(self):
        self.hello()
        self.assertEqual(parse_hello_ack(self.replies[-1][0])[0], ACK_OK)
        self.assertIsNotNone(self.server.endpoint_for(self.conn))

    def test_a_bad_ticket_builds_no_mapping(self):
        """★ 别让 UDP 变成免费的连接劫持面。

        认不出票据就什么都不建，回话里也不多说一个字（多说一句就是多一个探测面）。
        """
        self.hello("nope")
        self.assertEqual(parse_hello_ack(self.replies[-1][0])[0], ACK_BAD_TICKET)
        self.assertIsNone(self.server.endpoint_for(self.conn))

    def test_a_ticket_that_has_not_logged_in_yet_is_not_a_failure(self):
        """★★ 「登录包还在路上」和「根本不认识这张票」必须分得开。

        `bshook` 一看到 `0x0100` 就发 HELLO，而游戏服那边还没把
        `login_ticket` 写到连接上 —— **每一次登录都必然经过这个窗口**。
        服务端手里有票据表，分得清这两件事，所以就该由它明说，
        而不是让中继去猜「头几发不算数」。
        """
        self.server.bind_lookup(ticket_known=lambda t: t == "issued")
        self.hello("issued")
        self.assertEqual(parse_hello_ack(self.replies[-1][0])[0],
                         ACK_NOT_LOGGED_IN)
        self.assertIsNone(self.server.endpoint_for(self.conn))
        # 票据表也不认得的，照旧是「认不出票据」
        self.hello("nope")
        self.assertEqual(parse_hello_ack(self.replies[-1][0])[0], ACK_BAD_TICKET)

    def test_without_the_ticket_table_the_old_answer_is_kept(self):
        """没注入 `ticket_known`（单独跑 gameserver.py 做协议试探）就退化成
        原来的行为 —— 一律 `ACK_BAD_TICKET`，不会因此漏答或答错。"""
        self.hello("issued")
        self.assertEqual(parse_hello_ack(self.replies[-1][0])[0], ACK_BAD_TICKET)

    def test_a_throwing_ticket_table_falls_back_to_bad_ticket(self):
        """查票据抛异常也不能把 HELLO 这条路带崩。"""
        def boom(_ticket):
            raise RuntimeError("票据表炸了")
        self.server.bind_lookup(ticket_known=boom)
        self.hello("issued")
        self.assertEqual(parse_hello_ack(self.replies[-1][0])[0], ACK_BAD_TICKET)

    def test_a_logged_in_ticket_still_wins_over_the_pending_answer(self):
        """已经登进来的连接优先 —— `ticket_known` 只在查不到连接时才问。"""
        self.server.bind_lookup(ticket_known=lambda t: True)
        self.hello("good")
        self.assertEqual(parse_hello_ack(self.replies[-1][0])[0], ACK_OK)

    def test_data_from_an_unknown_address_is_dropped_but_told_so(self):
        """没 HELLO 过就发数据 = 丢掉（TCP 那份没停过，玩家察觉不到），但回一发「不认识你」
        让对方重发 HELLO（D72：代理 / NAT 换了口时这是它唯一能拿到的事件）。
        ★ 回包不比来包长 —— 这个口在公网上，放大不了（`SourceChangeTests` 钉着）。"""
        data = build_data([(0, heartbeat())])
        self.server._handle(data, self.addr, time.monotonic())
        self.assertEqual(self.conn.fed, [])
        self.assertEqual(self.server.unknown_in, 1)
        self.assertEqual(1, len(self.replies))
        reply, addr = self.replies[-1]
        self.assertEqual(self.addr, addr)
        self.assertEqual((udpsync.ACK_UNKNOWN_SOURCE, ""), parse_hello_ack(reply))
        self.assertLessEqual(len(reply), len(data))

    def test_a_recognised_stream_feeds_heartbeats_in_order(self):
        self.hello()
        self.server._handle(build_data([(0, heartbeat()), (1, heartbeat())]),
                            self.addr, time.monotonic())
        self.assertEqual([i for i, _ in self.conn.fed], [0, 1])

    def test_only_heartbeats_are_accepted_from_udp(self):
        """★ 铁律 1：事件包走 UDP = 把可靠通道变成不可靠的，那正是 §217 的死法。

        就算对端（被改过的客户端 / 乱发的脚本）硬塞进来，这里也不认。
        """
        self.hello()
        self.server._handle(
            build_data([(0, event(0)), (1, heartbeat()), (2, event(1))]),
            self.addr, time.monotonic())
        self.assertEqual(len(self.conn.fed), 1)
        self.assertTrue(is_heartbeat(self.conn.fed[0][1]))

    def test_a_junk_datagram_never_gets_a_reply(self):
        """公网 UDP 端口回任何东西都是一个反射放大面。"""
        self.server._handle(b"hello?", self.addr, time.monotonic())
        self.assertEqual(self.replies, [])

    def test_ping_is_answered_with_pong(self):
        self.hello()
        self.replies.clear()
        self.server._handle(build_ping(MSG_PING, 7), self.addr, time.monotonic())
        self.assertEqual(parse_ping(self.replies[-1][0]), (MSG_PONG, 7))

    def test_forget_drops_the_mapping(self):
        self.hello()
        self.server.forget(self.conn)
        self.assertIsNone(self.server.endpoint_for(self.conn))
        self.server._handle(build_data([(0, heartbeat())]), self.addr,
                            time.monotonic())
        self.assertEqual(self.conn.fed, [])

    def test_a_reconnect_from_a_new_address_replaces_the_old_mapping(self):
        self.hello()
        self.addr = ("203.0.113.9", 6001)       # NAT 换了个源端口
        self.hello()
        endpoint = self.server.endpoint_for(self.conn)
        self.assertEqual(endpoint.addr, ("203.0.113.9", 6001))
        self.assertEqual(len(self.server._by_addr), 1)

    # -- 下行 ---------------------------------------------------------------
    def test_downlink_is_off_until_the_client_says_it_can_receive(self):
        """★ 铁律 4 的落点：没自证能收就一直走 TCP。

        阶段 1 里 `relay.py` 根本不报这一条，所以下行恒走 `0x040f` ——
        这正是「先只做上行」想要的效果。
        """
        self.hello()
        self.assertFalse(self.server.ready_for(self.conn))

    def test_send_to_carries_redundancy_and_forces_broadcast(self):
        self.hello()
        sent = []
        self.server.sock = type("S", (), {"sendto": lambda _s, d, a: sent.append((d, a))})()
        self.server.redundancy = 2
        for _ in range(4):
            self.server.send_to(self.conn, heartbeat(target=3))
        chunks = parse_data(sent[-1][0])
        self.assertEqual([i for i, _ in chunks], [1, 2, 3])   # 当前 + 前 2 份
        self.assertTrue(all(p[2] == 0xFF for _, p in chunks))  # 都改回广播了


# ----------------------------------------------------------------------------
# ★ 端口被占用必须**当场炸**，不许悄悄换个地址起来
# ----------------------------------------------------------------------------
class PortBusyTests(unittest.TestCase):
    """Windows 上双栈 `[::]:P` 和 `0.0.0.0:P` 能**同时**绑上（UDP、TCP 都是）。

    所以「IPv6 绑不上就退回 IPv4」那个回退必须把 `EADDRINUSE` 排除掉 ——
    否则端口被别人占着时，我们会「悄悄起来了、但收不到包」：服务端照常运行、
    玩家照常能玩，只是位置数据全部投进黑洞，最后只能看到「别人卡」却查不出
    任何原因。这是本次专门要消灭的那种故障。
    """

    def test_udp_sync_refuses_to_start_on_a_taken_port(self):
        hog = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        hog.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        hog.bind(("::", 0))
        self.addCleanup(hog.close)
        port = hog.getsockname()[1]

        server = UdpSyncServer()
        server.port = port
        with self.assertRaises(OSError) as caught:
            server.create_socket("::")
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)

    def test_the_tcp_listener_refuses_too(self):
        """同一条规矩也钉在 `netlisten.create_listener` 上（认证/游戏/中继都用它）。"""
        import netlisten                                        # noqa: PLC0415
        hog = socket.create_server(("::", 0), family=socket.AF_INET6,
                                   dualstack_ipv6=True, reuse_port=False)
        self.addCleanup(hog.close)
        port = hog.getsockname()[1]
        with self.assertRaises(OSError) as caught:
            netlisten.create_listener("::", port)
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)


# ----------------------------------------------------------------------------
# 真的开一个 socket 跑一遍（收包循环别有低级错误）
# ----------------------------------------------------------------------------
class LiveSocketTests(unittest.TestCase):
    def test_a_real_datagram_round_trip(self):
        conn = FakeGameConn()
        server = UdpSyncServer(conn_for_ticket=lambda t: conn if t == "good" else None)
        server.port = 0                     # 让系统挑个空闲端口
        ready = threading.Event()
        threading.Thread(target=server.serve,
                         args=("127.0.0.1", ready), daemon=True).start()
        self.addCleanup(server.stop)
        self.assertTrue(ready.wait(timeout=5))
        port = server.sock.getsockname()[1]

        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(5)
        self.addCleanup(client.close)
        client.sendto(build_hello("good"), ("127.0.0.1", port))
        data, _ = client.recvfrom(4096)
        self.assertEqual(parse_hello_ack(data)[0], ACK_OK)

        client.sendto(build_data([(0, heartbeat())]), ("127.0.0.1", port))
        deadline = time.monotonic() + 5
        while not conn.fed and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual([i for i, _ in conn.fed], [0])


# ----------------------------------------------------------------------------
# ★★ 下行选路 —— 「本代播一次 TCP 种子，之后整代走 UDP」那条判据（铁律 4）
# ----------------------------------------------------------------------------
class DownlinkRouteTests(unittest.TestCase):
    def setUp(self):
        self.member = FakeGameConn("bob")
        self.sender = FakeGameConn("alice")
        self.server = UdpSyncServer(
            conn_for_ticket=lambda t: self.member if t == "good" else None)
        self.server._reply = lambda data, addr: None
        # `ready_for` 会看 socket 在不在（没监听就谈不上下行）。这里塞一个
        # 只记账的假 socket，把这一层排除掉，专测选路判据本身。
        self.sent = []
        self.server.sock = type(
            "S", (), {"sendto": lambda _s, d, a: self.sent.append((d, a))})()
        self.addr = ("203.0.113.9", 5000)

    def hello(self, downlink=True):
        flags = udpsync.HELLO_FLAG_DOWNLINK if downlink else 0
        self.server._handle(build_hello("good", flags), self.addr,
                            time.monotonic())

    def test_downlink_stays_off_until_the_client_reports_it_can_receive(self):
        """★ 「能不能收」由**对方**说了算，我们不猜。

        `relay.py` 靠「7788 我 bind 得上吗」自证：绑得上 = 游戏没在收 =
        投过去没人要。自证不了就一直走 `0x040f`，和今天完全一样。
        """
        self.hello(downlink=False)
        self.assertFalse(self.server.ready_for(self.member))
        self.hello(downlink=True)
        self.assertTrue(self.server.ready_for(self.member))

    def may(self, n, gen=7, sender=None):
        return self.server.may_send_heartbeat(
            self.member, sender or self.sender, heartbeat(n), gen)

    def test_the_first_beat_of_a_generation_seeds_tcp_then_udp_takes_over(self):
        """★★★ 下行的全部安全性都压在这一条上（推导见 `may_send_heartbeat`）。

        本代第一发走 TCP：它排在本代所有事件包**前面**，给收方队列定基线
        （`FlushTo(N)`，§216）。之后整代固定走 UDP —— 激活之后心跳只做
        `Grow(N-1)`，而 `grow()` 对更小的 n 直接 return，旧包对队列无害。
        """
        self.hello()
        self.assertFalse(self.may(0))     # 种子 -> TCP
        for _ in range(5):
            self.assertTrue(self.may(0))  # 之后整代 UDP

    def test_a_changing_N_does_not_switch_the_route_back_to_tcp(self):
        """★★★★★ 这就是和旧判据的分水岭（V0.3 §185）。

        旧判据「N 一变就退回 TCP」会把同一条位置流劈成两条互相超车的路：
        TCP 那一发被队头阻塞卡住之后，后发的 UDP 先到、旧 TCP 后到，
        角色被拉回旧位置。`bug调查/15` 量到 4182 次这种换路。
        N 是**可靠事件进度**，不是位置版本，不能拿它当选路依据。
        """
        self.hello()
        self.assertFalse(self.may(0))
        for n in (0, 1, 1, 2, 9, 9, 31):
            self.assertTrue(self.may(n), f"N={n} 不该把这条流拽回 TCP")

    def test_a_new_generation_seeds_tcp_again(self):
        """★ 换代要重新播种：那一发定的是收方**新**队列的基线（§217）。"""
        self.hello()
        self.assertFalse(self.may(0))
        self.assertTrue(self.may(3))
        self.assertFalse(self.may(0, gen=8), "新一代的第一发要重新走 TCP")
        self.assertTrue(self.may(0, gen=8))

    def test_an_unknown_generation_never_takes_udp(self):
        """★ 分不清这一发属于哪一代时不冒险 —— 一律 TCP。"""
        self.hello()
        for _ in range(3):
            self.assertFalse(self.may(0, gen=None))

    def test_two_senders_are_tracked_separately(self):
        self.hello()
        other = FakeGameConn("carol")
        self.assertFalse(self.may(5))
        self.assertTrue(self.may(5))
        # 另一个发送方的第一发仍然要走 TCP（各记各的账）
        self.assertFalse(self.may(5, sender=other))
        self.assertTrue(self.may(5, sender=other))

    def test_the_route_switch_is_counted_once_per_generation(self):
        """★ `flips` 是这条改动的回归指标：每代每个发送方**最多 1 次**。"""
        self.hello()
        endpoint = self.server.endpoint_for(self.member)
        self.assertFalse(self.may(0))
        for n in (0, 1, 2, 3, 4, 5):
            self.may(n)
        self.assertEqual(1, endpoint.flips)
        self.assertFalse(self.may(0, gen=8))
        self.may(0, gen=8)
        self.assertEqual(2, endpoint.flips)

    def test_a_failed_send_is_pulled_back_out_of_the_redundancy_buffer(self):
        """★ 发失败 -> 调用方改走 TCP -> 这一份**不能**再被冗余捎带出去。

        否则同一发心跳走了两条路，晚到的那份会把角色拉回旧位置（铁律 4）。
        """
        self.hello()

        class Boom:
            def sendto(self, *_args):
                raise OSError("unreachable")

        self.server.sock = Boom()
        self.assertFalse(self.server.send_to(self.member, heartbeat()))
        endpoint = self.server.endpoint_for(self.member)
        self.assertEqual(endpoint.recent, [])


# ----------------------------------------------------------------------------
# 接线：票据 -> 游戏连接（`gameserver._conn_for_udp_ticket`）
# ----------------------------------------------------------------------------
class TicketLookupTests(unittest.TestCase):
    """UDP 那条流靠登录票据认人。这一层错了，整条通道就是聋的。"""

    def setUp(self):
        import gameserver                                       # noqa: PLC0415
        self.gameserver = gameserver
        self.stub = type("C", (), {"login_ticket": "", "account_name": "x"})()
        gameserver.register_conn(self.stub)
        self.addCleanup(gameserver.unregister_conn, self.stub)

    def test_a_logged_in_connection_is_found_by_its_ticket(self):
        self.stub.login_ticket = "abc123"
        self.assertIs(self.gameserver._conn_for_udp_ticket("abc123"), self.stub)

    def test_a_connection_that_never_logged_in_is_not_reachable(self):
        """★ `login_ticket` 是在登录**成功**那一步才写上的。

        所以拿一张没用过的票据从 UDP 上来查不到任何东西 —— 空票据更不行
        （空串会匹配上所有还没登录的连接，那是个现成的劫持面）。
        """
        self.stub.login_ticket = ""
        self.assertIsNone(self.gameserver._conn_for_udp_ticket(""))
        self.assertIsNone(self.gameserver._conn_for_udp_ticket(None))
        self.assertIsNone(self.gameserver._conn_for_udp_ticket("abc123"))

    def test_the_singleton_is_wired_at_import_time(self):
        """`gameserver` 一被 import 就该把查连接的回调注进 `udpsync.SERVER`。"""
        self.stub.login_ticket = "zzz"
        self.assertIs(udpsync.SERVER._conn_for_ticket("zzz"), self.stub)


# ----------------------------------------------------------------------------
# ★ 接线：`RelayServer.deliver()` 逐人选路（新老客户端混在同一个房间）
# ----------------------------------------------------------------------------
class DeliverRoutingTests(unittest.TestCase):
    """服务端必须同时伺候更新过和没更新的客户端 —— 这是**逐人**选的。"""

    def setUp(self):
        import relayserver                                      # noqa: PLC0415
        self.relayserver = relayserver
        self.alice = FakeGameConn("alice")       # 发送方
        self.newbie = FakeGameConn("newbie")     # 更新过：UDP 收得到
        self.oldie = FakeGameConn("oldie")       # 没更新：只能走 0x040f
        self.room = [self.alice, self.newbie, self.oldie]

        self.udp = UdpSyncServer(conn_for_ticket=lambda t: {
            "new": self.newbie}.get(t))
        self.udp._reply = lambda data, addr: None
        self.udp_sent = []
        self.udp.sock = type("S", (), {
            "sendto": lambda _s, d, a: self.udp_sent.append((d, a))})()
        self.udp._handle(build_hello("new", udpsync.HELLO_FLAG_DOWNLINK),
                         ("203.0.113.9", 5000), time.monotonic())

        self.fallback = []
        self.server = relayserver.RelayServer(
            members_of=lambda conn: [c for c in self.room if c is not conn],
            fallback=lambda member, pkt: self.fallback.append((member, pkt)),
            udp_sender=self.udp,
            logger=lambda _msg: None)
        # 三个人锚在同一代，否则 `deliver` 会按「跨代」全丢（D137）。
        gen = relayserver.next_generation()
        for conn in self.room:
            relayserver.epoch_state(conn).assign(0, gen)

    def deliver(self, packet):
        del self.fallback[:], self.udp_sent[:]
        return self.server.deliver(self.alice, packet)

    def test_a_heartbeat_goes_udp_to_the_new_client_and_tcp_to_the_old_one(self):
        self.deliver(heartbeat(0))                 # 本代第一发 = 种子 -> 都走 TCP
        self.deliver(heartbeat(0))                 # 之后新客户端整代走 UDP
        self.assertEqual([m for m, _ in self.fallback], [self.oldie])
        self.assertEqual(len(self.udp_sent), 1)

    def test_only_the_first_beat_of_a_generation_takes_the_tcp_seed(self):
        """★★★★★ 铁律 4：本代播一次种，之后整代不再换路（§185）。

        旧判据是「N 一变就退回 TCP」，于是同一条位置流被劈成两条会互相超车
        的路（`bug调查/15` 量到 4182 次换路）。N 变了也不许改路 —— 队列在
        本代第一发心跳上就激活了，之后心跳只做 `grow()`，旧包无害。
        """
        self.deliver(heartbeat(0))
        self.assertEqual([], self.udp_sent, "种子那一发必须走 TCP")
        for nxt in (0, 1, 1, 7, 7, 7, 9):          # N 一路在变
            self.deliver(heartbeat(nxt))
            self.assertEqual([m for m, _ in self.fallback], [self.oldie],
                             f"N={nxt} 这一发不该把新客户端拽回 TCP")
            self.assertEqual(1, len(self.udp_sent))

    def test_a_new_generation_seeds_the_tcp_path_again(self):
        """★ 换代要重新播种：那一发定的是收方队列的基线（§217）。"""
        self.deliver(heartbeat(0))
        self.deliver(heartbeat(0))
        self.assertEqual(1, len(self.udp_sent))    # 前提：已经切到 UDP 了
        gen = self.relayserver.next_generation()
        for conn in self.room:
            self.relayserver.epoch_state(conn).assign(1, gen)
        self.deliver(heartbeat(3, game_id=1))
        self.assertEqual([], self.udp_sent, "新一代的第一发要重新走 TCP")
        self.deliver(heartbeat(3, game_id=1))
        self.assertEqual(1, len(self.udp_sent))

    def test_a_bot_uses_the_very_same_route_rule_as_a_human(self):
        """★ bot 不搞特例 —— 它恰恰是最吃这条路的一方。

        收方对**腾空**角色是按速度逐帧外推的（`packet_api §5.6`），而 bot
        因为导航几乎一直在空中、速度又大（弹跳台 −31/帧），一发倒序会被
        放大成 4 帧的错位。整代单路正是为它准备的。
        """
        self.alice.is_bot_conn = lambda: True
        self.deliver(heartbeat(0))
        self.assertEqual([], self.udp_sent)
        self.deliver(heartbeat(0))
        self.assertEqual([m for m, _ in self.fallback], [self.oldie])
        self.assertEqual(1, len(self.udp_sent))

    def test_an_event_always_goes_tcp_to_everyone(self):
        """★ 铁律 1：开火/命中/伤害**任何时候**都不走 UDP。

        它们走的是客户端每座位的可靠队列，丢一发就整局错位（§217）。
        """
        self.deliver(event(0))
        self.assertEqual(sorted(m.account_name for m, _ in self.fallback),
                         ["newbie", "oldie"])
        self.assertEqual(self.udp_sent, [])

    def test_the_old_client_never_sees_udp_no_matter_what(self):
        for _ in range(10):
            self.deliver(heartbeat(0))
        self.assertTrue(all(m is self.oldie for m, _ in self.fallback))

    def test_without_a_udp_sender_everything_behaves_exactly_as_before(self):
        """没有 UDP 旁路时（`--no-udp-sync` / 旧配置）行为逐字节不变。"""
        plain = self.relayserver.RelayServer(
            members_of=lambda conn: [c for c in self.room if c is not conn],
            fallback=lambda member, pkt: self.fallback.append((member, pkt)),
            logger=lambda _msg: None)
        gen = self.relayserver.next_generation()
        for conn in self.room:
            self.relayserver.epoch_state(conn).assign(0, gen)
        del self.fallback[:]
        plain.deliver(self.alice, heartbeat(0))
        self.assertEqual(sorted(m.account_name for m, _ in self.fallback),
                         ["newbie", "oldie"])

    def test_production_wires_the_udp_downlink_to_the_singleton(self):
        """★ 生产接线上下行**两个方向都走 UDP**（§185）。

        接反了不会有任何报错 —— 只是下行悄悄退回纯 TCP，跨境线路上的队头
        阻塞原样吃回来，而且没人看得出来。所以这条接线要有断言看着。
        """
        import gameserver                                       # noqa: PLC0415
        self.assertIs(udpsync.SERVER, gameserver.PEER_RELAY._udp_sender)

    def test_a_route_needs_a_generation_to_be_known(self):
        """★ 分不清这一发属于哪一代时不冒险，一律走 TCP。"""
        self.assertFalse(self.udp.may_send_heartbeat(
            self.newbie, self.alice, heartbeat(0), None))


# ----------------------------------------------------------------------------
# 端到端：bshook（模拟）-> relay.py -> udpsync 服务端
# ----------------------------------------------------------------------------
class EndToEndTests(unittest.TestCase):
    """把整条上行链路真的跑一遍 —— 只有 `bshook` 那一截是模拟的。

    模拟的部分就是「往本机中继的 UDP 口发 `HELLO` / `DATA`」，
    和 `hook/bshook.c` 里 `sync_on_login` / `sync_mirror_peer` 发的字节完全同构。
    """

    def setUp(self):
        import relay                                            # noqa: PLC0415
        self.relay_module = relay
        # ★ 用**真的**排序闸门：`udpsync` 是故意把冗余捎带的那几份原样喂上来的，
        #   去重和排序全在 `HeartbeatOrder` 上（`gameserver.Conn` 就是这么接的）。
        self.conn = GatedGameConn()
        self.server = UdpSyncServer(
            conn_for_ticket=lambda t: self.conn if t == "good" else None)
        self.server.port = 0
        ready = threading.Event()
        threading.Thread(target=self.server.serve,
                         args=("127.0.0.1", ready), daemon=True).start()
        self.addCleanup(self.server.stop)
        self.assertTrue(ready.wait(timeout=5))
        server_port = self.server.sock.getsockname()[1]

        # 本机中继：随便挑个空闲的本地口
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        local_port = probe.getsockname()[1]
        probe.close()
        self.relay = relay.UdpSyncRelay("127.0.0.1", target_port=server_port,
                                        local_port=local_port, redundancy=2)
        self.assertTrue(self.relay.start())
        self.addCleanup(self.relay.close)

        self.hook = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.hook.close)
        self.local_addr = ("127.0.0.1", local_port)

    def hook_send(self, data):
        self.hook.sendto(data, self.local_addr)

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_the_whole_uplink_works_and_carries_redundancy(self):
        self.hook_send(build_hello("good"))
        self.assertTrue(self.wait_for(lambda: self.relay.acked),
                        "中继没等到 HELLO_ACK")

        for i in range(4):
            self.hook_send(build_data([(i, heartbeat())]))
        self.assertTrue(self.wait_for(lambda: len(self.conn.fed) >= 4),
                        f"服务端只收到 {len(self.conn.fed)} 发")
        # 闸门之后：一发不多、一发不少、严格升序
        self.assertEqual([i for i, _ in self.conn.fed], [0, 1, 2, 3])
        # 中继确实捎带了冗余，而且重复的那些被闸门吃掉了。
        # 4 个数据报分别带 [0] / [0,1] / [0,1,2] / [1,2,3]（冗余 2 = 最多 3 份），
        # 其中重复的是 0、0、1、1、2 —— 一共 5 份。
        self.assertEqual(self.conn.order.udp_stale, 5)

    def test_a_lost_datagram_is_recovered_by_the_redundancy(self):
        """★ 方案 C 的**真正**价值：把索引序列补齐。

        不是为了画面更顺（位置是快照，新的自然覆盖旧的），而是为了不让
        「复位后第一发心跳」这种定基线的包丢掉 —— 丢了就是 §217 那个
        整局零伤害。这里模拟「第 1 发在路上丢了」，第 2 发把它捎回来。
        """
        self.hook_send(build_hello("good"))
        self.assertTrue(self.wait_for(lambda: self.relay.acked))

        # 索引 0、1、2 都进了中继的历史，但只把「捎带了 0/1/2 的那一发」发出去
        with self.relay._lock:
            self.relay.recent = [(0, heartbeat()), (1, heartbeat()),
                                 (2, heartbeat())]
            payload = udpsync.build_data(list(self.relay.recent))
        self.relay._to_remote(payload)
        self.assertTrue(self.wait_for(lambda: len(self.conn.fed) >= 3))
        self.assertEqual([i for i, _ in self.conn.fed][:3], [0, 1, 2])

    def test_a_bad_ticket_leaves_the_relay_unacked_and_nothing_breaks(self):
        """★ 服务端认不出票据 = 这条 UDP 通道不成立。

        中继只是一直没被确认，**不报错、不重连、不影响任何 TCP 通道** ——
        玩家那边的表现和「没有这个功能」完全一样。
        """
        self.hook_send(build_hello("nope"))
        self.assertTrue(self.wait_for(lambda: self.relay.received > 0))
        self.assertFalse(self.relay.acked)
        self.hook_send(build_data([(0, heartbeat())]))
        time.sleep(0.2)
        self.assertEqual(self.conn.fed, [])

    # -- 下行闸门 -----------------------------------------------------------
    def test_the_downlink_gate_only_moves_forward(self):
        """★★ 没有这道闸门，乱序或冗余补发会把别人的角色**拉回旧位置**。

        位置心跳没有任何可判新旧的原版字段（头 `+8` 的序列号对心跳恒为 0），
        客户端自己拦不住，只能在注入 7788 之前拦。
        """
        injected = []
        self.relay._inject = injected.append

        self.relay._on_remote_datagram(build_data([(0, heartbeat())]))
        self.relay._on_remote_datagram(build_data([(1, heartbeat())]))
        # 冗余把 0/1 又捎回来一次 + 新的 2：只有 2 该被注入
        self.relay._on_remote_datagram(
            build_data([(0, heartbeat()), (1, heartbeat()), (2, heartbeat())]))
        self.assertEqual(len(injected), 3)
        # 迟到的 1（网络乱序）一律丢
        self.relay._on_remote_datagram(build_data([(1, heartbeat())]))
        self.assertEqual(len(injected), 3)

    def test_the_downlink_injects_a_recovered_copy_in_order(self):
        """索引 1 的数据报丢了，索引 2 那发把它捎了回来 —— 按 1、2 的顺序注入。"""
        injected = []
        self.relay._inject = injected.append
        self.relay._on_remote_datagram(build_data([(0, heartbeat())]))
        self.relay._on_remote_datagram(
            build_data([(0, heartbeat(7)), (1, heartbeat(8)), (2, heartbeat(9))]))
        self.assertEqual([heartbeat_next_event_seq(p) for p in injected],
                         [0, 8, 9])

    def test_the_downlink_refuses_anything_that_is_not_a_heartbeat(self):
        """铁律 1 在注入前再挡一次：事件包绝不从 UDP 进游戏。"""
        injected = []
        self.relay._inject = injected.append
        self.relay._on_remote_datagram(build_data([(0, event(0))]))
        self.assertEqual(injected, [])

    def test_downlink_readiness_comes_from_the_hook_not_from_a_guess(self):
        """★★ 下行的准入依据是 `bshook` 亲眼看着 bind 返回 0 之后报上来的，
        **不是**我们自己去试 bind 猜出来的。

        猜的那种（「我 bind 不上，所以大概是游戏占着」）有个致命的假阳性：
        那个口要是被别的程序占着，位置数据就投进黑洞 —— 而 UDP 没有回执，
        在外面完全看不出来，表现是「所有人在你屏幕上定住」。
        """
        self.assertFalse(self.relay.downlink)          # 还没 bind
        self.hook_send(build_hello("good"))            # 登录：标志位是 0
        self.assertTrue(self.wait_for(lambda: self.relay.acked))
        self.assertFalse(self.relay.downlink)
        # bshook 看着 bind 成功了，再发一发带标志位的 HELLO
        self.hook_send(build_hello("good", udpsync.HELLO_FLAG_DOWNLINK))
        self.assertTrue(self.wait_for(lambda: self.relay.downlink))

    def test_a_new_login_clears_the_downlink_flag(self):
        """换一条游戏连接 = 新的 `GameSession` = 那个 UDP 口要重新 bind。

        标志位不跟着清的话，中继会在游戏还没 bind 好的窗口里告诉服务端
        「可以投了」—— 那段时间的位置数据就白扔了。
        """
        self.hook_send(build_hello("good", udpsync.HELLO_FLAG_DOWNLINK))
        self.assertTrue(self.wait_for(lambda: self.relay.downlink))
        self.hook_send(build_hello("other-ticket"))    # 重登
        self.assertTrue(self.wait_for(lambda: not self.relay.downlink))

    def test_a_reconnect_replaying_the_same_ticket_still_resets_the_indexes(self):
        """★ 断线重连时客户端**原样重放同一张票据**（§171）。

        所以「是不是一条新的游戏连接」不能靠票据判。判据是标志位从「已绑」
        回到「没绑」—— `bshook` 每发一次登录包就把它清一次。
        判错的后果：服务端那边是一条新的 `Conn`、索引从 0 起，而我们这边
        还接着上一条的号往下数，两边永远对不上。
        """
        self.hook_send(build_hello("good", udpsync.HELLO_FLAG_DOWNLINK))
        self.assertTrue(self.wait_for(lambda: self.relay.downlink))
        self.hook_send(build_data([(7, heartbeat())]))
        self.assertTrue(self.wait_for(lambda: len(self.relay.recent) > 0))
        # 重连：同一张票据，但标志位回到 0（新的 GameSession 还没 bind）
        self.hook_send(build_hello("good"))
        self.assertTrue(self.wait_for(lambda: not self.relay.downlink))
        with self.relay._lock:
            self.assertEqual(self.relay.recent, [])
            self.assertEqual(self.relay.downlink_high_water, -1)

    def test_the_relay_ignores_non_heartbeats_from_the_hook(self):
        """铁律 1 在中继这一层再挡一次（纵深防御）。"""
        self.hook_send(build_hello("good"))
        self.assertTrue(self.wait_for(lambda: self.relay.acked))
        self.hook_send(build_data([(0, event(0))]))
        time.sleep(0.2)
        self.assertEqual(self.conn.fed, [])
        with self.relay._lock:
            self.assertEqual(self.relay.recent, [])


# ----------------------------------------------------------------------------
# 本机中继那两行提示的**噪音**判据（§225 第六节）
# ----------------------------------------------------------------------------
class RelayNoticeTests(unittest.TestCase):
    """`relay.py` 打给玩家看的两行提示：什么时候该打、什么时候闭嘴。

    ★ 这两条都不影响游戏（TCP 那份从来没停过），但玩家和我们**就是靠它们**
      判断「UDP 到底通没通」。`bug调查/udp验证` 里两条都失真了：
      「⚠ 20 秒没等到服务器回应」在游戏刚连上的 0.4 秒内就打了出来、
      紧接着才是 `✓ 已认出`；而票据过期那 90 秒里「认不出票据」刷了 45 行。
      误报一次就等于把人引去查一个根本不存在的防火墙问题。

    这里**不开 socket**：`_to_remote` 在 `self.remote is None` 时直接返回，
    所以可以拿一个没 `start()` 过的中继对象直接喂报文。
    """

    def setUp(self):
        import relay                                            # noqa: PLC0415
        self.relay_module = relay
        self.lines = []
        real_log = relay.log
        relay.log = self.lines.append
        self.addCleanup(setattr, relay, "log", real_log)
        self.relay = relay.UdpSyncRelay("127.0.0.1", target_port=1,
                                        local_port=1)

    def hook_hello(self, ticket="tkt", downlink=False):
        """模拟 `bshook` 发来的 HELLO（登录时一发、UDP 口 bind 成功时一发）。"""
        flags = udpsync.HELLO_FLAG_DOWNLINK if downlink else 0
        self.relay._on_hook_datagram(build_hello(ticket, flags))

    def server_says(self, result, note=""):
        """模拟服务器回一发 HELLO_ACK。两个计数器都由 `_pump_remote` 负责加。"""
        self.relay.received += 1
        self.relay.replies += 1
        self.relay._on_remote_datagram(build_hello_ack(result, note))

    def refusals(self):
        return [x for x in self.lines if "没接受这条通道" in x]

    # -- 「认不出票据」：**按服务端给的事件分流，不按第几发** -------------
    def test_the_login_race_is_silent_because_the_server_says_so(self):
        """★ 每次登录都必然经过这个窗口：`bshook` 看到 `0x0100` 就发 HELLO，
        比游戏服写下 `login_ticket` 早 0.1~0.2 秒。服务端此刻**明说**
        「票据是真的、只是还没登进来」（`ACK_NOT_LOGGED_IN`），
        中继据此安静等着 —— 判据是**事件**，不是「跳过头几发」。"""
        self.hook_hello()
        for _ in range(5):                    # 慢机器可能要等好几发，都不该出声
            self.server_says(ACK_NOT_LOGGED_IN, "票据还没登进游戏服")
        self.assertEqual([], self.refusals())
        self.server_says(ACK_OK)
        self.assertEqual([], self.refusals())
        self.assertEqual(1, len([x for x in self.lines if "✓" in x]))

    def test_a_real_refusal_is_reported_on_the_very_first_one(self):
        """★ 反过来也要成立：服务端说的是「真不认识这张票」，
        那就**第一发就报**，不用等第二发 —— 慢机器上等下一发要 2 秒。"""
        self.hook_hello()
        self.server_says(ACK_BAD_TICKET, "认不出票据")
        self.assertEqual(1, len(self.refusals()))
        self.assertIn("认不出票据", self.refusals()[0])

    def test_a_ticket_that_stays_bad_is_logged_exactly_once(self):
        """票据真过期时（玩家停在登录界面）`HELLO_RETRY_S` 每 2 秒重试一次 ——
        不按状态去重就是 `bug调查/udp验证` 里那 45 行刷屏。"""
        self.hook_hello()
        for _ in range(20):
            self.server_says(ACK_BAD_TICKET, "认不出票据")
        self.assertEqual(1, len(self.refusals()))

    def test_success_after_a_long_refusal_still_announces_itself(self):
        self.hook_hello()
        for _ in range(5):
            self.server_says(ACK_BAD_TICKET, "认不出票据")
        self.server_says(ACK_OK)
        self.assertTrue(self.relay.acked)
        self.assertIn("✓", self.lines[-1])

    def test_a_channel_that_goes_bad_again_says_so_again(self):
        """★ 通了之后**又**被拒 = 真正的状态翻转（服务端重启、票据没了），
        该再说一次。和「下行 已就绪 / 未就绪」是同一个套路：只在状态变了时说话。"""
        self.hook_hello()
        self.server_says(ACK_OK)
        for _ in range(10):
            self.server_says(ACK_BAD_TICKET, "认不出票据")
        self.assertEqual(1, len(self.refusals()))

    def test_the_disabled_answer_is_reported_too(self):
        """服务端把 UDP 同步整个关了（`--no-udp-sync`）也是一次真正的拒绝。"""
        self.hook_hello()
        for _ in range(3):
            self.server_says(udpsync.ACK_DISABLED, "服务端关掉了 UDP 同步")
        self.assertEqual(1, len(self.refusals()))

    def test_a_new_game_connection_gets_a_fresh_account(self):
        """★ 「说过了」这个状态要跟着**新的一条游戏连接**清掉，否则整个中继
        进程只提示一次 —— 玩家换服务器 / 服务端重启之后就再也看不到提示了。"""
        self.hook_hello("old")
        for _ in range(5):
            self.server_says(ACK_BAD_TICKET, "认不出票据")
        self.assertEqual(1, len(self.refusals()))
        self.hook_hello("new")                       # 换了票据 = 新的游戏连接
        self.assertFalse(self.relay.refused_logged)
        self.server_says(ACK_BAD_TICKET, "认不出票据")
        self.assertEqual(2, len(self.refusals()))

    # -- 「20 秒没等到服务器回应」-----------------------------------------
    def test_the_quiet_warning_counts_from_the_first_hello(self):
        """★ 从**第一发 HELLO** 起算，不是从中继进程启动起算。

        中继随启动脚本先起来，玩家点开游戏、输账号、进大厅，到发出登录包时
        早就过了 20 秒 —— 按进程启动算的话，这句话会在游戏刚连上的那一瞬
        立刻打出来（`bug调查/udp验证` 的 `logs-client1` 14:41:50.716 就是）。
        """
        # 中继已经起来很久了，但一发 HELLO 都还没发过 -> 永远不提示
        self.assertFalse(self.relay._quiet_warning_due(now=10_000.0))
        self.hook_hello()
        self.relay.first_hello_at = 100.0
        self.assertFalse(self.relay._quiet_warning_due(now=100.0 + 19.0))
        self.assertTrue(self.relay._quiet_warning_due(
            now=100.0 + self.relay_module.UDP_QUIET_WARN_S + 1))

    def test_no_quiet_warning_when_the_server_actually_answered(self):
        """服务器回了「认不出票据」说明**路是通的**，只是票据不对。
        再说一句「没等到服务器回应、多半是防火墙」会把人引到错的方向。"""
        self.hook_hello()
        self.relay.first_hello_at = 100.0
        self.server_says(ACK_BAD_TICKET, "认不出票据")
        self.assertFalse(self.relay._quiet_warning_due(now=100.0 + 600.0))

    def test_the_quiet_warning_stops_once_the_channel_is_up(self):
        self.hook_hello()
        self.relay.first_hello_at = 100.0
        self.server_says(ACK_OK)
        self.assertFalse(self.relay._quiet_warning_due(now=100.0 + 600.0))

    def test_a_later_connection_to_a_silent_server_still_warns(self):
        """★ 「收到过回应」必须按**每条游戏连接**算，不能按进程累计算。

        场景：先连一台好服务器（收到过 ACK），玩家再换到一台没放行 UDP 的
        （或者服务端重启成了 `--no-udp-sync`）。按累计值判的话，
        `received > 0` 会让提示永远哑掉。
        """
        self.hook_hello("first")
        self.server_says(ACK_OK)
        self.hook_hello("second")                    # 换服务器 = 新的游戏连接
        self.relay.first_hello_at = 500.0
        self.assertEqual(0, self.relay.replies)
        self.assertTrue(self.relay._quiet_warning_due(
            now=500.0 + self.relay_module.UDP_QUIET_WARN_S + 1))


# ----------------------------------------------------------------------------
# 本机服务器 / 远程服务器：只差上游这一个地址（X_Mod D58）
# ----------------------------------------------------------------------------
class _TwoServers:
    """两台「服务器」都是裸 UDP socket：只记收到了什么、回不回由用例决定 ——
    这样才看得见「本机那一位有没有被转出去」「迟到的回包认没认」。"""

    #: 远程那台在 `server.config` 里写的地址（经代理的用例会写成域名，让代理去解）。
    TARGET_HOST = "127.0.0.1"

    def make_proxy(self):
        """远程那条上游经哪个代理（`ProxySettings | None`）。在建 relay 之前调 ——
        假代理要 `addCleanup`、要现分配端口，放不进类属性。"""
        return None

    def setUp(self):
        import relay                                            # noqa: PLC0415
        self.relay_module = relay
        self.lines = []
        real_log = relay.log
        relay.log = self.lines.append
        self.addCleanup(setattr, relay, "log", real_log)
        self.remote_srv = self._server()
        self.local_srv = self._server()
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        local_port = probe.getsockname()[1]
        probe.close()
        self.relay = relay.UdpSyncRelay(
            self.TARGET_HOST, target_port=self.remote_srv.getsockname()[1],
            local_port=local_port, redundancy=2, proxy=self.make_proxy(),
            local_target=("127.0.0.1", self.local_srv.getsockname()[1]))
        self.assertTrue(self.relay.start())
        self.addCleanup(self.relay.close)
        self.hook = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.hook.close)
        self.relay_addr = ("127.0.0.1", local_port)

    def _server(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(5.0)
        self.addCleanup(sock.close)
        return sock

    def hello(self, ticket, local, downlink=False):
        """模拟 `bshook` 的 `sync_send_hello`：选本机就带 `HELLO_FLAG_LOCAL_SERVER`。"""
        flags = ((udpsync.HELLO_FLAG_LOCAL_SERVER if local else 0)
                 | (udpsync.HELLO_FLAG_DOWNLINK if downlink else 0))
        self.hook.sendto(build_hello(ticket, flags), self.relay_addr)

    @staticmethod
    def nothing_arrives(server, wait=0.3):
        """★ 「不该收到」没有事件可等，只能看一小段时间里真没来（测试专用）。"""
        server.settimeout(wait)
        try:
            server.recvfrom(65536)
        except socket.timeout:
            return True
        finally:
            server.settimeout(5.0)
        return False

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False


class LocalServerRouteTests(_TwoServers, unittest.TestCase):
    """★ 本机 / 远程**同一条路**（用户 2026-09-23）：`bshook` 两种模式都往同一个中继口发，
    HELLO 里那一位决定中继往哪转；冗余 / 下行 / 重试全是同一份代码。"""

    def test_a_local_login_goes_to_the_local_server(self):
        self.hello("tkt-local", local=True)
        data, _ = self.local_srv.recvfrom(65536)
        self.assertEqual("tkt-local", parse_hello(data))
        self.assertTrue(self.nothing_arrives(self.remote_srv))

    def test_the_local_flag_is_not_passed_on_to_the_server(self):
        """那一位只给中继选上游用；服务端收到的 HELLO 只带下行位。"""
        self.hello("tkt-local", local=True, downlink=True)
        data, _ = self.local_srv.recvfrom(65536)
        _ticket, flags = udpsync.parse_hello_full(data)
        self.assertEqual(udpsync.HELLO_FLAG_DOWNLINK, flags)

    def test_positions_presence_and_mover_phase_ride_the_same_route(self):
        """★ 这一轮之后的所有东西都跟着 HELLO 定下的上游走 —— 移动平台相位
        正是这次要本机也开旁路的原因（没有它，本机测试里 bot 的手雷按开局估计算鲤鱼）。"""
        self.hello("tkt-local", local=True)
        self.local_srv.recvfrom(65536)                          # HELLO
        self.hook.sendto(build_data([(0, heartbeat())]), self.relay_addr)
        data, _ = self.local_srv.recvfrom(65536)
        self.assertEqual(MSG_DATA, parse_header(data)[0])
        for packet in (udpsync.build_presence(1, 2, 3, True),
                       udpsync.build_mover_phase(1000, 2000, [(296, 500, 0)])):
            self.hook.sendto(packet, self.relay_addr)
            data, _ = self.local_srv.recvfrom(65536)
            self.assertEqual(packet, data)                      # 原样转发
        self.assertTrue(self.nothing_arrives(self.remote_srv))

    def test_a_remote_login_after_a_local_one_goes_back_to_the_remote(self):
        self.hello("tkt-local", local=True)
        self.local_srv.recvfrom(65536)
        self.hello("tkt-remote", local=False)
        data, _ = self.remote_srv.recvfrom(65536)
        self.assertEqual("tkt-remote", parse_hello(data))
        self.assertTrue(self.nothing_arrives(self.local_srv))

    def test_a_late_ack_from_the_previous_server_is_ignored(self):
        """★ 换了服务器之后，上一台迟到的 ACK 不能被当成这一轮的确认 ——
        认了的话中继就不再重发 HELLO，这一轮真要是丢了第一发就再也接不上。"""
        self.hello("tkt-local", local=True)
        _, relay_side = self.local_srv.recvfrom(65536)          # 中继「本机」那条上游
        self.hello("tkt-remote", local=False)
        _, remote_side = self.remote_srv.recvfrom(65536)
        self.local_srv.sendto(build_hello_ack(ACK_OK), relay_side)
        time.sleep(0.2)
        self.assertFalse(self.relay.acked)
        self.remote_srv.sendto(build_hello_ack(ACK_OK), remote_side)
        self.assertTrue(self.wait_for(lambda: self.relay.acked))

    def test_the_route_is_logged_only_when_it_changes(self):
        """按状态翻转说话：同一边连登两次只说一次，换边再说。"""
        for ticket in ("a", "b"):
            self.hello(ticket, local=True)
            self.local_srv.recvfrom(65536)
        said = [x for x in self.lines if "这一轮登录选的是" in x]
        self.assertEqual(1, len(said))
        self.assertIn("本机服务器", said[0])
        self.hello("c", local=False)
        self.remote_srv.recvfrom(65536)
        said = [x for x in self.lines if "这一轮登录选的是" in x]
        self.assertEqual(2, len(said))
        self.assertIn("远程服务器", said[1])


class HttpProxyRouteTests(_TwoServers, unittest.TestCase):
    """HTTP CONNECT 代理转不了 UDP ⇒ 只关「远程」那条；本机那条是环回，照常走。
    （SOCKS5 代理走 UDP ASSOCIATE，见下面 `Socks5UdpRouteTests`，X_Mod D71。）"""

    def make_proxy(self):
        return self.relay_module.ProxySettings("http", "127.0.0.1", 1)

    def test_the_remote_route_stays_on_tcp(self):
        self.hello("tkt", local=False)
        self.assertTrue(self.nothing_arrives(self.remote_srv))
        # 一发都没出去，谈不上「服务器没回应」—— 别拿防火墙那句话吓人
        self.relay.first_hello_at = 100.0
        self.assertFalse(self.relay._quiet_warning_due(
            now=100.0 + self.relay_module.UDP_QUIET_WARN_S + 1))
        # 原因说了一次（start() 时），之后每一发 HELLO 都不再重复
        self.hello("tkt2", local=False)
        self.assertTrue(self.nothing_arrives(self.remote_srv))
        said = [x for x in self.lines if "✗ HTTP CONNECT 代理转不了 UDP" in x]
        self.assertEqual(1, len(said))

    def test_the_local_route_is_not_affected_by_the_proxy(self):
        self.hello("tkt", local=True)
        data, _ = self.local_srv.recvfrom(65536)
        self.assertEqual("tkt", parse_hello(data))

    def test_start_udp_sync_no_longer_drops_the_whole_channel(self):
        """以前配了代理 `start_udp_sync` 直接返回 `None` —— 本机模式跟着没了旁路。"""
        captured = {}

        class FakeRelay:
            def __init__(self, target_host, **kwargs):
                captured.update(kwargs)

            def start(self):
                return True

        proxy = self.relay_module.ProxySettings("socks5", "127.0.0.1", 1080)
        real = self.relay_module.UdpSyncRelay
        self.relay_module.UdpSyncRelay = FakeRelay
        try:
            got = self.relay_module.start_udp_sync("example.com", proxy=proxy)
        finally:
            self.relay_module.UdpSyncRelay = real
        self.assertIsNotNone(got)
        self.assertIs(proxy, captured["proxy"])


# ----------------------------------------------------------------------------
# ★ SOCKS5 代理：远程那条经 UDP ASSOCIATE 照走 UDP（X_Mod D71，用户 2026-09-26）
# ----------------------------------------------------------------------------
from test_proxy import FakeProxyMixin, wait_for                 # noqa: E402


class _Socks5Route(FakeProxyMixin, _TwoServers):
    """`_TwoServers` + 一个真的中转 UDP 的假 SOCKS5 代理（`test_proxy.FakeProxyMixin`）。
    `remote_srv` 看到的来源是代理的中转口。"""

    RESOLVE = {"game.popshot.example": "127.0.0.1"}
    PROXY_KW = dict(udp=True, accept_many=True)

    def make_proxy(self):
        self.proxy, self.state, _ = self.start_socks5(resolve=self.RESOLVE, **self.PROXY_KW)
        return self.proxy

    def remote_hello(self, ticket="tkt", ack=True):
        """一发远程 HELLO，回 `(远程服务器看到的来源, HELLO 数据报)`；`ack` 时顺手确认。"""
        self.hello(ticket, local=False)
        data, proxy_side = self.remote_srv.recvfrom(65536)
        self.assertEqual(ticket, parse_hello(data))
        if ack:
            self.remote_srv.sendto(build_hello_ack(ACK_OK), proxy_side)
            self.assertTrue(self.wait_for(lambda: self.relay.acked))
        return proxy_side, data

    def test_the_local_route_does_not_go_through_the_proxy(self):
        self.hello("tkt", local=True)
        data, _ = self.local_srv.recvfrom(65536)
        self.assertEqual("tkt", parse_hello(data))
        self.assertEqual([], self.state.get("udp_targets", []))


class Socks5UdpRouteTests(_Socks5Route, unittest.TestCase):
    """配了 SOCKS5 代理：远程 HELLO / DATA 经代理到服务端，回包经代理注入；本机那条不经代理。"""

    def test_a_remote_login_goes_through_the_proxy_both_ways(self):
        remote_port = self.remote_srv.getsockname()[1]
        proxy_side, _ = self.remote_hello()
        self.assertEqual(self.state["exit_addr"], proxy_side)         # 来源是代理的出口
        self.assertEqual([(1, "127.0.0.1", remote_port)], self.state["udp_targets"])
        # 上行 DATA 经代理到服务端
        self.hook.sendto(build_data([(0, heartbeat())]), self.relay_addr)
        data, _ = self.remote_srv.recvfrom(65536)
        self.assertEqual(MSG_DATA, parse_header(data)[0])
        # 下行 DATA 经代理回来、拆掉头之后注入
        injected = []
        self.relay._inject = injected.append
        self.remote_srv.sendto(build_data([(0, heartbeat(5))]), proxy_side)
        self.assertTrue(self.wait_for(lambda: len(injected) == 1))
        self.assertEqual(5, heartbeat_next_event_seq(injected[0]))
        self.assertTrue(self.nothing_arrives(self.local_srv))
        self.assertNotIn("error", self.state)
        # 日志：start() 的横幅就把「远程经代理」说全了（所以第一次远程登录不再重复），
        # 切到本机再切回远程时那一行要带上代理。
        banner = [x for x in self.lines if "选「远程服务器」时" in x]
        self.assertEqual(1, len(banner))
        self.assertIn(self.proxy.route, banner[0])
        self.assertIn("UDP ASSOCIATE", banner[0])
        self.hello("tkt-local", local=True)
        self.local_srv.recvfrom(65536)
        self.hello("tkt-again", local=False)
        self.remote_srv.recvfrom(65536)
        said = [x for x in self.lines if "这一轮登录选的是「远程服务器」" in x]
        self.assertEqual(1, len(said))
        self.assertIn(self.proxy.route, said[0])
        relay_addr = self.state["relay_addr"]                          # 代理面向我们的中转口
        self.assertIn(f"UDP 中转口 {relay_addr[0]}:{relay_addr[1]}", said[0])

    def test_a_dropped_association_is_rebuilt_by_the_hello_retry(self):
        """★ 代理撤掉关联（控制连接断）是一个事件：这一轮的上游撤掉、`acked` 清掉，
        现成的 HELLO 重发把它重建起来；**下行闸门必须清** —— 服务端见到新来源会新建
        `Endpoint`、索引从 0 起，不清就把之后的下行全丢掉。"""
        proxy_side, _ = self.remote_hello()
        injected = []
        self.relay._inject = injected.append
        self.remote_srv.sendto(build_data([(0, heartbeat()), (1, heartbeat())]), proxy_side)
        self.assertTrue(self.wait_for(lambda: len(injected) == 2))
        self.assertEqual(1, self.relay.downlink_high_water)

        self.state["kill_control"]()                       # 代理撤掉关联
        self.assertTrue(self.wait_for(lambda: self.relay.remote is None))
        self.assertFalse(self.relay.acked)
        self.assertEqual(-1, self.relay.downlink_high_water)
        self.assertEqual(0, self.relay.replies)
        # 那一行日志是事件处理的最后一步，等它落下来
        self.assertTrue(self.wait_for(
            lambda: len([x for x in self.lines if "关掉了 UDP 通道" in x]) == 1))

        self.relay._retry_hello()                          # HELLO 重发那一支
        self.assertIsNotNone(self.relay.remote)
        self.assertEqual(2, self.state["associations"])
        data, proxy_side2 = self.remote_srv.recvfrom(65536)
        self.assertEqual("tkt", parse_hello(data))
        self.assertNotEqual(proxy_side, proxy_side2)       # 新关联 = 新的中转口
        self.remote_srv.sendto(build_hello_ack(ACK_OK), proxy_side2)
        self.assertTrue(self.wait_for(lambda: self.relay.acked))
        # 服务端那边是一条新的流、索引从 0 起 —— 闸门清过了才收得下
        self.remote_srv.sendto(build_data([(0, heartbeat(9))]), proxy_side2)
        self.assertTrue(self.wait_for(lambda: len(injected) == 3))
        self.assertEqual(9, heartbeat_next_event_seq(injected[2]))
        self.assertNotIn("error", self.state)

    def test_a_dead_association_on_the_other_route_does_not_touch_this_one(self):
        """路由已经切到本机时代理才撤关联：本机这一轮的确认一个都不能动、也不打「回退 TCP」。"""
        self.remote_hello()
        self.hello("tkt-local", local=True)
        _, local_side = self.local_srv.recvfrom(65536)
        self.local_srv.sendto(build_hello_ack(ACK_OK), local_side)
        self.assertTrue(self.wait_for(lambda: self.relay.acked))
        self.state["kill_control"]()
        self.assertTrue(self.wait_for(lambda: False not in self.relay._upstreams, timeout=5))
        self.assertTrue(self.relay.acked)
        self.assertIsNotNone(self.relay.remote)
        self.assertEqual([], [x for x in self.lines if "关掉了 UDP 通道" in x])


class Socks5DomainTargetRouteTests(_Socks5Route, unittest.TestCase):
    """`server_address` 是域名：写进每发数据报的头里交给代理解，本机一次都不解析它。"""

    TARGET_HOST = "game.popshot.example"

    def setUp(self):
        patcher = mock.patch("socket.getaddrinfo", wraps=socket.getaddrinfo)
        self.getaddrinfo = patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_the_domain_rides_in_the_header_and_is_never_resolved_here(self):
        remote_port = self.remote_srv.getsockname()[1]
        proxy_side, _ = self.remote_hello()
        self.assertEqual([(3, "game.popshot.example", remote_port)], self.state["udp_targets"])
        looked_up = [call.args[0] for call in self.getaddrinfo.call_args_list]
        self.assertNotIn("game.popshot.example", looked_up)
        injected = []
        self.relay._inject = injected.append
        self.remote_srv.sendto(build_data([(0, heartbeat(5))]), proxy_side)
        self.assertTrue(self.wait_for(lambda: len(injected) == 1))
        self.assertNotIn("error", self.state)


class Socks5RefusingUdpRouteTests(_Socks5Route, unittest.TestCase):
    """代理接 TCP 却不支持 UDP ASSOCIATE（状态 7）：远程回退 TCP、只说一次、本机不受影响。
    ★ 绝不拿直连 UDP 兜底 —— 配了代理就不许有任何直连动作。"""

    PROXY_KW = dict(udp=False, accept_many=True)

    def test_the_remote_route_falls_back_to_tcp_and_says_so_once(self):
        self.assertIsNone(self.relay.remote)               # start() 时就试过、没建起来
        self.hello("tkt", local=False)
        self.assertTrue(self.nothing_arrives(self.remote_srv))
        self.hello("tkt2", local=False)                    # 每一发 HELLO 都会再试
        self.assertTrue(self.nothing_arrives(self.remote_srv))
        self.assertGreaterEqual(self.state["connections"], 2)
        said = [x for x in self.lines if "建不了 UDP 通道" in x]
        self.assertEqual(1, len(said))                     # 说一次，直到建起来一次为止
        self.assertIn("UDP ASSOCIATE", said[0])
        self.relay.first_hello_at = 100.0
        self.assertFalse(self.relay._quiet_warning_due(
            now=100.0 + self.relay_module.UDP_QUIET_WARN_S + 1))


class Socks5UnspecifiedBindRouteTests(Socks5UdpRouteTests):
    """应答 `BND.ADDR = 0.0.0.0` 的代理：数据报发到「连代理用的那个地址」。父类用例全部重跑一遍。"""

    PROXY_KW = dict(udp=True, accept_many=True, bnd_zero=True)


# ----------------------------------------------------------------------------
# ★ 用户 2026-09-26 的第二条要求：bot 的位置发给走代理的真人时也要走 UDP
# ----------------------------------------------------------------------------
class ProxiedBotDownlinkTests(FakeProxyMixin, unittest.TestCase):
    """服务端对 bot 和真人本来就一视同仁（`DeliverRoutingTests.test_a_bot_uses_the_very_same_route_rule_as_a_human`）；
    以前经代理的玩家收不到 UDP，只因为**他那一头**的中继没开远程上游、服务端从没收到过他的
    HELLO。这里把整条链真的跑一遍：假 bot -> `RelayServer.deliver` -> `UdpSyncServer`
    -> 假 SOCKS5 代理 -> `UdpSyncRelay` -> 注入。"""

    def setUp(self):
        import relay                                            # noqa: PLC0415
        import relayserver                                      # noqa: PLC0415
        self.player = FakeGameConn("player")                    # 走代理的真人
        self.bot = FakeGameConn("bot")
        self.bot.is_bot_conn = lambda: True
        self.udp = UdpSyncServer(
            conn_for_ticket=lambda t: self.player if t == "good" else None)
        self.udp.port = 0
        ready = threading.Event()
        threading.Thread(target=self.udp.serve, args=("127.0.0.1", ready),
                         daemon=True).start()
        self.addCleanup(self.udp.stop)
        self.assertTrue(ready.wait(timeout=5))
        server_port = self.udp.sock.getsockname()[1]

        self.proxy, self.state, _ = self.start_socks5(udp=True, accept_many=True)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        local_port = probe.getsockname()[1]
        probe.close()
        self.relay = relay.UdpSyncRelay("127.0.0.1", target_port=server_port,
                                        local_port=local_port, redundancy=2,
                                        proxy=self.proxy)
        self.assertTrue(self.relay.start())
        self.addCleanup(self.relay.close)
        self.injected = []
        self.relay._inject = self.injected.append
        hook = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(hook.close)
        hook.sendto(build_hello("good", udpsync.HELLO_FLAG_DOWNLINK),
                    ("127.0.0.1", local_port))
        self.assertTrue(wait_for(lambda: self.relay.acked))
        self.assertTrue(self.udp.ready_for(self.player))

        self.fallback = []
        self.server = relayserver.RelayServer(
            members_of=lambda conn: [self.player] if conn is self.bot else [self.bot],
            fallback=lambda member, pkt: self.fallback.append((member, pkt)),
            udp_sender=self.udp, logger=lambda _msg: None)
        gen = relayserver.next_generation()
        for conn in (self.player, self.bot):
            relayserver.epoch_state(conn).assign(0, gen)

    def test_bot_heartbeats_reach_the_proxied_player_over_udp(self):
        self.server.deliver(self.bot, heartbeat(0))            # 本代第一发 = 种子 -> TCP
        self.assertEqual([self.player], [m for m, _ in self.fallback])
        self.assertEqual(0, self.server.delivered_udp)
        self.server.deliver(self.bot, heartbeat(0))            # 之后整代走 UDP
        self.assertEqual(1, self.server.delivered_udp)
        self.assertTrue(wait_for(lambda: len(self.injected) == 1))
        self.assertTrue(is_heartbeat(self.injected[0]))
        self.assertEqual([self.player], [m for m, _ in self.fallback])   # 没有双发
        self.assertNotIn("error", self.state)


# ----------------------------------------------------------------------------
# ★ 来源换口（X_Mod D72，用户 2026-09-26）：代理 / NAT 在服务端那一侧换了 UDP 源口
# ----------------------------------------------------------------------------
class SourceChangeTests(unittest.TestCase):
    """服务端侧：同一条游戏连接换了地址就**沿用同一条流**；认不出的来源回「不认识」。"""

    def setUp(self):
        self.member = FakeGameConn("bob")
        self.server = UdpSyncServer(
            conn_for_ticket=lambda t: self.member if t == "good" else None)
        self.sent = []
        self.server.sock = type(
            "S", (), {"sendto": lambda _s, d, a: self.sent.append((d, a))})()
        self.logs = []
        self.server.bind_lookup(logger=self.logs.append)
        self.a = ("203.0.113.9", 5000)
        self.b = ("203.0.113.9", 5001)

    def hello(self, addr):
        self.server._handle(build_hello("good", udpsync.HELLO_FLAG_DOWNLINK), addr,
                            time.monotonic())

    def acks_to(self, addr):
        return [parse_hello_ack(d)[0] for d, a in self.sent
                if a == addr and parse_header(d)[0] == MSG_HELLO_ACK]

    def test_the_stream_follows_the_connection_to_its_new_address(self):
        """★ 新建一条的话索引从 0 起，中继那道只准前进的闸门会把之后几分钟的下行全丢掉。"""
        self.hello(self.a)
        self.assertTrue(self.server.send_to(self.member, heartbeat(1)))
        self.assertTrue(self.server.send_to(self.member, heartbeat(2)))
        endpoint = self.server.endpoint_for(self.member)
        self.hello(self.b)
        self.assertIs(endpoint, self.server.endpoint_for(self.member))   # 同一条流
        self.assertEqual(self.b, endpoint.addr)
        self.assertNotIn(self.a, self.server._by_addr)
        self.assertTrue(self.server.ready_for(self.member))
        del self.sent[:]
        self.assertTrue(self.server.send_to(self.member, heartbeat(3)))
        data, addr = self.sent[-1]
        self.assertEqual(self.b, addr)
        self.assertEqual([0, 1, 2], [i for i, _ in parse_data(data)])   # 索引接着数（捎带前两份）
        self.assertEqual(1, len([x for x in self.logs if "来源从" in x]))
        self.hello(self.b)                                             # 同一地址重发 HELLO：不再说
        self.assertEqual(1, len([x for x in self.logs if "来源从" in x]))

    def test_an_unknown_source_is_told_so_instead_of_being_ignored(self):
        self.hello(self.a)
        del self.sent[:]
        self.server._handle(build_data([(0, heartbeat())]), self.b, time.monotonic())
        self.assertEqual([udpsync.ACK_UNKNOWN_SOURCE], self.acks_to(self.b))
        self.assertEqual(1, self.server.unknown_in)
        self.assertEqual([], self.member.fed)                          # 认不出的一份不喂
        # 保活也一样：认得出的回 PONG，认不出的回「不认识」（大厅里只有保活在走）
        del self.sent[:]
        self.server._handle(build_ping(MSG_PING, 7), self.a, time.monotonic())
        self.assertEqual(MSG_PONG, parse_header(self.sent[-1][0])[0])
        self.server._handle(build_ping(MSG_PING, 7), self.b, time.monotonic())
        self.assertEqual([udpsync.ACK_UNKNOWN_SOURCE], self.acks_to(self.b))
        for packet in (udpsync.build_presence(1, 2, 3, True),
                       udpsync.build_mover_phase(1000, 2000, [(296, 500, 0)]),
                       udpsync.build_tick_clock(5, 6)):
            del self.sent[:]
            self.server._handle(packet, self.b, time.monotonic())
            self.assertEqual([udpsync.ACK_UNKNOWN_SOURCE], self.acks_to(self.b))

    def test_the_answer_is_never_longer_than_the_question(self):
        """反射面：这个口在公网上，回包不许比来包长。"""
        nack = build_hello_ack(udpsync.ACK_UNKNOWN_SOURCE, "")
        short = build_header(MSG_DATA, 0)                 # 8 字节、解得开、份数 0
        self.assertLess(len(short), len(nack))
        self.server._handle(short, self.b, time.monotonic())
        self.assertEqual([], self.sent)
        self.assertEqual(1, self.server.unknown_in)
        self.server._handle(build_data([(0, heartbeat())]), self.b, time.monotonic())
        self.assertEqual(len(nack), len(self.sent[-1][0]))
        self.server._handle(b"\x00" * 64, self.b, time.monotonic())    # 不是我们的包：什么都不回
        self.assertEqual(1, len(self.sent))


class RelaySourceChangeTests(unittest.TestCase):
    """中继侧：收到「这个来源我不认识」翻转那一发立刻重发 HELLO、说一行；之后不逐发重发；闸门不动。"""

    def setUp(self):
        import relay                                            # noqa: PLC0415
        self.lines = []
        real_log = relay.log
        relay.log = self.lines.append
        self.addCleanup(setattr, relay, "log", real_log)
        self.relay = relay.UdpSyncRelay("127.0.0.1", target_port=1, local_port=1)
        self.relay.ticket = "tkt"
        self.sent = []
        self.relay._to_remote = self.sent.append

    def nack(self):
        self.relay._on_remote_datagram(build_hello_ack(udpsync.ACK_UNKNOWN_SOURCE, ""))

    def test_the_first_nack_after_an_ack_triggers_one_hello(self):
        self.relay._on_remote_datagram(build_hello_ack(ACK_OK))
        self.assertTrue(self.relay.acked)
        self.nack()
        self.assertFalse(self.relay.acked)
        self.assertEqual(["tkt"], [parse_hello(d) for d in self.sent])
        self.nack()
        self.nack()                                        # HELLO 在路上：不逐发重发
        self.assertEqual(1, len(self.sent))
        self.assertEqual(1, len([x for x in self.lines if "不认识这条通道现在的来源" in x]))
        self.relay._on_remote_datagram(build_hello_ack(ACK_OK))   # 认回来了
        self.assertTrue(self.relay.acked)
        self.assertEqual(2, len([x for x in self.lines if "✓ 服务器已认出" in x]))
        self.nack()                                        # 又换了一次口：再翻转、再一发
        self.assertEqual(2, len(self.sent))
        self.assertEqual(2, len([x for x in self.lines if "不认识这条通道现在的来源" in x]))

    def test_a_nack_while_not_yet_acked_is_left_to_the_retry(self):
        self.nack()
        self.assertEqual([], self.sent)
        self.assertEqual([], [x for x in self.lines if "不认识" in x])

    def test_the_downlink_gate_is_not_touched_by_a_nack(self):
        """服务端那边同一条流接着发、索引不重来 —— 闸门动了反而会把冗余捎带的旧份再注入一遍。"""
        self.relay._on_remote_datagram(build_hello_ack(ACK_OK))
        injected = []
        self.relay._inject = injected.append
        self.relay._on_remote_datagram(build_data([(0, heartbeat()), (1, heartbeat())]))
        self.nack()
        self.assertEqual(1, self.relay.downlink_high_water)
        self.relay._on_remote_datagram(
            build_data([(0, heartbeat()), (1, heartbeat()), (2, heartbeat())]))
        self.assertEqual(3, len(injected))


class ProxyExitRotationTests(FakeProxyMixin, unittest.TestCase):
    """★ 整条链：代理面向服务端那一侧换了 UDP 源口 → 服务端回「不认识」→ 中继重发 HELLO →
    服务端把同一条流换到新来源、下行索引接着数 → 中继的闸门照收（D72）。"""

    def setUp(self):
        import relay                                            # noqa: PLC0415
        self.player = FakeGameConn("player")
        self.udp = UdpSyncServer(
            conn_for_ticket=lambda t: self.player if t == "good" else None)
        self.udp.port = 0
        self.server_logs = []
        self.udp.bind_lookup(logger=self.server_logs.append)
        ready = threading.Event()
        threading.Thread(target=self.udp.serve, args=("127.0.0.1", ready),
                         daemon=True).start()
        self.addCleanup(self.udp.stop)
        self.assertTrue(ready.wait(timeout=5))
        server_port = self.udp.sock.getsockname()[1]

        self.proxy, self.state, _ = self.start_socks5(udp=True, accept_many=True)
        self.lines = []
        real_log = relay.log
        relay.log = self.lines.append
        self.addCleanup(setattr, relay, "log", real_log)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        local_port = probe.getsockname()[1]
        probe.close()
        self.relay = relay.UdpSyncRelay("127.0.0.1", target_port=server_port,
                                        local_port=local_port, redundancy=2,
                                        proxy=self.proxy)
        self.assertTrue(self.relay.start())
        self.addCleanup(self.relay.close)
        self.injected = []
        self.relay._inject = self.injected.append
        self.hook = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.hook.close)
        self.local_addr = ("127.0.0.1", local_port)
        self.hook.sendto(build_hello("good", udpsync.HELLO_FLAG_DOWNLINK), self.local_addr)
        self.assertTrue(wait_for(lambda: self.relay.acked))
        self.assertTrue(self.udp.ready_for(self.player))

    def test_the_stream_survives_the_proxy_changing_its_exit_port(self):
        first_exit = self.udp.endpoint_for(self.player).addr
        self.assertEqual(self.state["exit_addr"], first_exit)
        self.assertTrue(self.udp.send_to(self.player, heartbeat(1)))
        self.assertTrue(wait_for(lambda: len(self.injected) == 1))

        self.state["rotate_exit"]()                        # 代理换了出口
        self.assertNotEqual(first_exit, self.state["exit_addr"])
        # 下一发上行从新口到服务端 → 「不认识」→ 中继重发 HELLO → 服务端把流换到新口
        self.hook.sendto(build_data([(0, heartbeat())]), self.local_addr)
        self.assertTrue(wait_for(
            lambda: self.udp.endpoint_for(self.player).addr == self.state["exit_addr"]))
        self.assertTrue(wait_for(lambda: self.relay.acked))
        self.assertGreaterEqual(self.udp.unknown_in, 1)
        self.assertEqual(1, len([x for x in self.lines if "不认识这条通道现在的来源" in x]))
        self.assertEqual(1, len([x for x in self.server_logs if "来源从" in x]))
        # 下行接着发：索引 1 从新口回来，闸门（水位 0）照收
        self.assertTrue(self.udp.ready_for(self.player))
        self.assertTrue(self.udp.send_to(self.player, heartbeat(2)))
        self.assertTrue(wait_for(lambda: len(self.injected) == 2))
        self.assertEqual(2, heartbeat_next_event_seq(self.injected[1]))
        self.assertNotIn("error", self.state)


class LocalServerRouteUnitTests(unittest.TestCase):
    def test_an_unstarted_relay_only_remembers_the_route(self):
        """没 `start()` 过（`RelayNoticeTests` 那种直接喂报文的用法）：只记路由，不开 socket。"""
        import relay                                            # noqa: PLC0415
        r = relay.UdpSyncRelay("127.0.0.1", target_port=1, local_port=1)
        r._on_hook_datagram(build_hello("t", udpsync.HELLO_FLAG_LOCAL_SERVER))
        self.assertTrue(r.route_local)
        self.assertIsNone(r.remote)
        self.assertEqual({}, r._upstreams)


class HookSendsInBothModesTests(unittest.TestCase):
    """`bshook` 那一侧：旁路不再只在远程模式下开，HELLO 说清这一轮选了哪边。"""

    BSHOOK = os.path.join(os.path.dirname(HERE), "hook", "bshook.c")

    def _source(self):
        if not os.path.exists(self.BSHOOK):
            raise unittest.SkipTest("不在源码仓库里（缺 hook/bshook.c）")
        with open(self.BSHOOK, encoding="utf-8", errors="replace") as fp:
            return fp.read()

    def _body(self, head):
        src = self._source()
        start = src.index(head)
        return src[start:src.index("\n}\n", start)]

    def test_the_flag_is_the_same_bit_on_both_sides(self):
        m = re.search(r"#define\s+SYNC_FLAG_LOCAL_SERVER\s+(0x[0-9A-Fa-f]+|\d+)",
                      self._source())
        self.assertIsNotNone(m, "bshook.c 里没有 SYNC_FLAG_LOCAL_SERVER")
        self.assertEqual(udpsync.HELLO_FLAG_LOCAL_SERVER, int(m.group(1), 0))

    def test_the_side_channel_is_not_gated_on_remote_mode(self):
        """★ 原来这里有一道「只在远程服务器模式下做」的门 —— 本机测试因此既没有
        在场证据也没有移动平台相位（用户 2026-09-23 要求两边一个样）。"""
        body = self._body("static void sync_on_plain_frame(")
        self.assertNotIn("popshot_online_mode", body)

    def test_hello_says_which_server_was_picked(self):
        body = self._body("static void sync_send_hello(void)")
        self.assertIn("SYNC_FLAG_LOCAL_SERVER", body)
        self.assertIn("popshot_online_mode()", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
