#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原版 TCP 中继（rcp 协议）的测试 —— 里程碑 J.3 / D078。

依据全部在 `.claude/FINDINGS.md` §156 / §157 / §158 / §159。

这里测的是 `relayserver.py` **自己**：帧编解码、注册认人、投递与回退、
以及那两条「断了比没有更糟」换来的铁律。`0x0310` -> `0x0210` 的接线
在 `test_room.py` 的 `PeerRelayTests` 里。
"""
import os
import socket
import struct
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gameserver                                              # noqa: E402
import relayserver                                             # noqa: E402
from relayserver import (RCP_DATA_DOWN, RCP_DATA_UP,           # noqa: E402
                         RCP_PING, RCP_REGISTER, RCP_REP_PING,
                         RCP_WHO_ARE_YOU, RelayServer,
                         build_join_relay, build_rcp, parse_auth, take_rcp)
from simple import SimpleCipher                                # noqa: E402


class FakeGameConn:
    """一条假的游戏连接。中继只拿它当 key，外加回退投递的目标。"""

    def __init__(self, name):
        self.account_name = name
        self.fallback_got = []


# ----------------------------------------------------------------------------
# 线格式
# ----------------------------------------------------------------------------
class FrameTests(unittest.TestCase):
    def test_the_frame_is_byte_for_byte_the_game_server_frame(self):
        """§156：`RelayConnection` 和 `ServerConnection` 是同一个基类。

        两处各写了一份 `build_*`（`relayserver` 不 import `gameserver`），
        这条用例就是防止哪天单边漂了。
        """
        for opcode, payload in ((0, b""), (1, b""), (3, bytes(range(40)))):
            self.assertEqual(gameserver.build_game(opcode, payload),
                             build_rcp(opcode, payload))

    def test_the_header_matches_0x5bb9e7(self):
        # +0 = 0xff、+2 = 总长-10、+4 = 0、+6 = 标志(0)、+8 = opcode
        frame = build_rcp(3, b"abcd")
        self.assertEqual(0xFF, frame[0])
        self.assertEqual(4, struct.unpack_from("<H", frame, 2)[0])
        self.assertEqual(0, struct.unpack_from("<H", frame, 4)[0])
        self.assertEqual(0, struct.unpack_from("<H", frame, 6)[0])
        self.assertEqual(3, struct.unpack_from("<H", frame, 8)[0])
        self.assertEqual(b"abcd", frame[10:])

    def test_take_rcp_waits_for_the_whole_frame(self):
        frame = build_rcp(3, b"xyz")
        for cut in range(len(frame)):
            self.assertIsNone(take_rcp(bytearray(frame[:cut])))
        self.assertEqual((3, b"xyz", len(frame)), take_rcp(bytearray(frame)))

    def test_take_rcp_splits_a_coalesced_read(self):
        buf = bytearray(build_rcp(0, b"") + build_rcp(3, b"ab"))
        opcode, payload, size = take_rcp(buf)
        self.assertEqual((0, b""), (opcode, payload))
        del buf[:size]
        self.assertEqual((3, b"ab", 12), take_rcp(buf))

    def test_a_bad_magic_is_reported_not_swallowed(self):
        # 解密流一旦错位，往下读只会读出垃圾并当成同步数据转发出去。
        with self.assertRaises(ValueError):
            take_rcp(bytearray(b"\xfe" + b"\x00" * 12))

    def test_register_payload_is_three_int32(self):
        self.assertEqual((1, 2, 3), parse_auth(struct.pack("<iii", 1, 2, 3)))
        with self.assertRaises(ValueError):
            parse_auth(b"\x01\x02\x03")


class JoinRelayPayloadTests(unittest.TestCase):
    """§157：`NetAddress{int32 ip; u16 port}` + `RelayAuthData{3 x int32}`。"""

    def test_ip_is_raw_network_order_and_port_is_host_order(self):
        blob = build_join_relay("127.0.0.1", 27798, (7, 8, 9))
        self.assertEqual(18, len(blob))
        # `Connect`（0x5bc50d）把这 4 字节**原样**写进 sin_addr，没有 htonl。
        self.assertEqual(socket.inet_aton("127.0.0.1"), blob[:4])
        # 端口反过来 —— Connect 里才过 htons，所以线上是主机序。
        self.assertEqual(27798, struct.unpack_from("<H", blob, 4)[0])
        self.assertEqual((7, 8, 9), struct.unpack_from("<iii", blob, 6))


# ----------------------------------------------------------------------------
# 票据与去重
# ----------------------------------------------------------------------------
class TicketTests(unittest.TestCase):
    def setUp(self):
        self.server = RelayServer()
        self.alice = FakeGameConn("alice")

    def test_a_ticket_carries_room_seat_and_a_nonce(self):
        auth = self.server.issue(self.alice, 4, 2)
        self.assertEqual(4, auth[0])
        self.assertEqual(2, auth[1])
        self.assertNotEqual(0, auth[2])

    def test_a_second_issue_on_the_same_connection_is_refused(self):
        """★★ §159：`0x0210` 回第二次是定时炸弹，去重责任在服务端。"""
        self.assertIsNotNone(self.server.issue(self.alice, 0, 0))
        self.assertIsNone(self.server.issue(self.alice, 0, 0))
        self.assertIsNone(self.server.issue(self.alice, 1, 3))

    def test_nonces_are_unique_across_connections(self):
        bob = FakeGameConn("bob")
        a = self.server.issue(self.alice, 0, 0)
        b = self.server.issue(bob, 0, 1)
        self.assertNotEqual(a[2], b[2])

    def test_redeem_returns_the_ticket_once(self):
        auth = self.server.issue(self.alice, 3, 1)
        ticket = self.server.redeem(auth)
        self.assertIs(self.alice, ticket.game_conn)
        self.assertEqual(3, ticket.room_id)
        self.assertEqual(1, ticket.seat_index)
        # 一张票只能兑一次 —— 兑过之后再报同一份认证数据就认不出了。
        self.assertIsNone(self.server.redeem(auth))

    def test_an_unknown_nonce_is_refused(self):
        self.assertIsNone(self.server.redeem((0, 0, 0x7FFFFFFF)))

    def test_forget_releases_the_ticket_and_the_dedup_slot(self):
        self.server.issue(self.alice, 0, 0)
        self.server.forget(self.alice)
        self.assertFalse(self.server.has_issued(self.alice))
        self.assertIsNotNone(self.server.issue(self.alice, 0, 0))

    def test_a_stale_ticket_is_swept_and_the_slot_reopens(self):
        auth = self.server.issue(self.alice, 0, 0)
        for ticket in self.server._tickets.values():
            ticket.issued_at -= relayserver.TICKET_TTL + 1
        self.assertIsNone(self.server.redeem(auth))
        # 过期不代表这条连接以后不能再要中继。
        self.assertIsNotNone(self.server.issue(self.alice, 0, 0))


# ----------------------------------------------------------------------------
# 一条真的 TCP 中继连接（起监听器、用真 socket + 真 SimpleCipher 说话）
# ----------------------------------------------------------------------------
class FakeRelayClient:
    """假客户端：真 TCP、真加密，但只说 rcp。"""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.cout = SimpleCipher.client_to_server()
        self.cin = SimpleCipher.server_to_client()
        self.buf = bytearray()

    def send(self, opcode, payload=b""):
        self.sock.sendall(self.cout.encrypt(build_rcp(opcode, payload)))

    def recv_frame(self, timeout=3.0):
        """收下一帧；超时返回 ``None``。"""
        deadline = time.time() + timeout
        while True:
            got = take_rcp(self.buf)
            if got is not None:
                opcode, payload, size = got
                del self.buf[:size]
                return (opcode, payload)
            left = deadline - time.time()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not data:
                return None
            self.buf += self.cin.decrypt(data)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class LiveRelayTests(unittest.TestCase):
    """端到端：真监听器 + 真 socket。投递、回退、身份询问都在这里验。"""

    def setUp(self):
        self.rooms = {}                      # game_conn -> [同房间的其他人]
        self.server = RelayServer(
            members_of=lambda conn: self.rooms.get(conn, []),
            fallback=self._fallback,
            port=0)
        ready = threading.Event()
        threading.Thread(target=self.server.serve,
                         kwargs={"host": "127.0.0.1", "ready": ready},
                         daemon=True).start()
        self.assertTrue(ready.wait(timeout=5), "中继监听器没起来")
        self.port = self.server.listener.getsockname()[1]
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.stop()

    def _fallback(self, member, payload):
        member.fallback_got.append(payload)

    def connect(self):
        client = FakeRelayClient(self.port)
        self.clients.append(client)
        return client

    def register(self, game_conn, room_id=0, seat=0):
        auth = self.server.issue(game_conn, room_id, seat)
        client = self.connect()
        client.send(RCP_REGISTER, struct.pack("<iii", *auth))
        # 注册是异步处理的，等它真的绑上再往下走。
        for _ in range(200):
            if self.server.conn_for(game_conn) is not None:
                return client
            time.sleep(0.01)
        self.fail("rcpRegister 一直没被处理")

    def test_data_is_relayed_verbatim_to_the_other_seat(self):
        alice, bob = FakeGameConn("alice"), FakeGameConn("bob")
        self.rooms = {alice: [bob], bob: [alice]}
        a = self.register(alice, 0, 0)
        b = self.register(bob, 0, 1)
        blob = bytes(range(32))
        b.send(RCP_DATA_UP, blob)
        # 中继 -> 客户端的数据是 **opcode 0**（和上行的 3 不同号，§157）。
        self.assertEqual((RCP_DATA_DOWN, blob), a.recv_frame())
        self.assertEqual([], alice.fallback_got)

    def test_the_sender_never_gets_its_own_packet_back(self):
        alice, bob = FakeGameConn("alice"), FakeGameConn("bob")
        self.rooms = {alice: [bob], bob: [alice]}
        self.register(alice, 0, 0)
        b = self.register(bob, 0, 1)
        b.send(RCP_DATA_UP, b"\x01\x02")
        self.assertIsNone(b.recv_frame(timeout=0.5))

    def test_a_peer_without_a_relay_connection_falls_back_to_0x040f(self):
        """★ 中继连接是异步建的，进房那几秒里必然有人还没接上。

        那几秒里同步不能断 —— 没接上的走 `0x040f`（`0x408619` 的 else 分支，
        本来就是原版的回退路径）。
        """
        alice, bob = FakeGameConn("alice"), FakeGameConn("bob")
        self.rooms = {alice: [bob], bob: [alice]}
        b = self.register(bob, 0, 1)          # 只有 bob 接上了中继
        b.send(RCP_DATA_UP, b"\xaa\xbb")
        for _ in range(200):
            if alice.fallback_got:
                break
            time.sleep(0.01)
        self.assertEqual([b"\xaa\xbb"], alice.fallback_got)

    def test_a_send_broken_relay_falls_back_to_0x040f(self):
        """★ 回归 bug调查/4：中继的发送流一旦超时错位（SimpleCipher 流密码，
        半发送之后对面永远解不开），这条连接对**投递**来说就等于没有 ——
        `conn_for()` 绕开它，数据自动回退 `0x040f` 走游戏服连接。

        ★ 不关它的 socket（铁律 1）：客户端那条 TCP 的入站方向可能还是
        好的（我们还能收它的数据），只是上游废了。
        """
        alice, bob = FakeGameConn("alice"), FakeGameConn("bob")
        self.rooms = {alice: [bob], bob: [alice]}
        a = self.register(alice, 0, 0)
        b = self.register(bob, 0, 1)
        relay_a = self.server.conn_for(alice)
        relay_a.send_broken = True            # 模拟一次发送超时之后的状态
        self.assertIsNone(self.server.conn_for(alice))
        b.send(RCP_DATA_UP, b"\xcc\xdd")
        for _ in range(200):
            if alice.fallback_got:
                break
            time.sleep(0.01)
        self.assertEqual([b"\xcc\xdd"], alice.fallback_got)
        # 连接本体还活着（没被关），客户端发上来的数据照收。
        a.send(RCP_REP_PING)
        time.sleep(0.2)
        self.assertFalse(relay_a.closed)

    def test_a_stalled_connection_is_reported(self):
        """`stalled()`：注册着但 `STALL_AFTER_S` 秒没有任何入站 = 半死。

        gameserver 的自愈路径（`recover_peer_relay`）靠它判定要不要
        给客户端换一条新中继。
        """
        alice = FakeGameConn("alice")
        self.rooms = {alice: []}
        client = self.register(alice, 0, 0)
        relay = self.server.conn_for(alice)
        self.assertFalse(self.server.stalled(alice))
        relay.last_inbound_at -= relayserver.STALL_AFTER_S + 1
        self.assertTrue(self.server.stalled(alice))
        # 客户端一发 pong 就不算半死了。
        client.send(RCP_REP_PING)
        for _ in range(200):
            if not self.server.stalled(alice):
                break
            time.sleep(0.01)
        self.assertFalse(self.server.stalled(alice))

    def test_data_before_register_is_answered_with_who_are_you(self):
        # 原版的 opcode 2 就是干这个的：客户端收到会重发 rcpRegister。
        client = self.connect()
        client.send(RCP_DATA_UP, b"\x01")
        self.assertEqual((RCP_WHO_ARE_YOU, b""), client.recv_frame())

    def test_an_unknown_ticket_is_answered_with_who_are_you(self):
        client = self.connect()
        client.send(RCP_REGISTER, struct.pack("<iii", 0, 0, 0x7FFFFFFF))
        self.assertEqual((RCP_WHO_ARE_YOU, b""), client.recv_frame())

    def test_an_unknown_opcode_never_closes_the_connection(self):
        """★★ 铁律 1（§158）：连接一断，客户端会自己退出房间。

        所以「不认识的包」这类小毛病绝不能升级成断连。
        """
        alice = FakeGameConn("alice")
        self.rooms = {alice: []}
        client = self.register(alice, 0, 0)
        client.send(9, b"junk")
        time.sleep(0.2)
        self.assertIsNotNone(self.server.conn_for(alice))
        # 还能正常收发。
        client.send(RCP_REP_PING)
        time.sleep(0.2)
        self.assertIsNotNone(self.server.conn_for(alice))

    def test_ping_is_sent_and_the_reply_is_counted(self):
        alice = FakeGameConn("alice")
        self.rooms = {alice: []}
        saved = relayserver.PING_INTERVAL
        relayserver.PING_INTERVAL = 0.0        # 下一个 tick 就发
        try:
            client = self.register(alice, 0, 0)
            self.assertEqual((RCP_PING, b""), client.recv_frame(timeout=5))
        finally:
            relayserver.PING_INTERVAL = saved
        relay = self.server.conn_for(alice)
        client.send(RCP_REP_PING)
        for _ in range(200):
            if relay.pongs_in:
                break
            time.sleep(0.01)
        self.assertEqual(1, relay.pongs_in)

    def test_a_closed_relay_connection_is_unbound(self):
        alice = FakeGameConn("alice")
        self.rooms = {alice: []}
        client = self.register(alice, 0, 0)
        client.close()
        for _ in range(300):
            if self.server.conn_for(alice) is None:
                break
            time.sleep(0.01)
        self.assertIsNone(self.server.conn_for(alice))

    def test_delivery_after_a_relay_drops_falls_back_again(self):
        """中继掉了不该让同步跟着停 —— 退回 `0x040f` 继续送。"""
        alice, bob = FakeGameConn("alice"), FakeGameConn("bob")
        self.rooms = {alice: [bob], bob: [alice]}
        a = self.register(alice, 0, 0)
        b = self.register(bob, 0, 1)
        a.close()
        for _ in range(300):
            if self.server.conn_for(alice) is None:
                break
            time.sleep(0.01)
        b.send(RCP_DATA_UP, b"\x07\x08")
        for _ in range(200):
            if alice.fallback_got:
                break
            time.sleep(0.01)
        self.assertEqual([b"\x07\x08"], alice.fallback_got)


# ----------------------------------------------------------------------------
# 局号（`UdpPacket` 头 +4）—— bug调查/8_2「其他人都不会动」
# ----------------------------------------------------------------------------
def udp_packet(sender=0, game_id=0, sequence=0, inner=0x4001,
               body=b"\x11\x22\x33\x44\x55\x66\x77\x88"):
    """一份 `UdpPacket`（§151 的 12 字节头 + body）。"""
    return (struct.pack("<BbbB", 0xFF, sender, -1, 0)
            + struct.pack("<HHHH", game_id, 0xBEEF, sequence, inner) + body)


class PeerGameIdTests(unittest.TestCase):
    """收包入口 `0x4078c4` 拿头里的局号和自己的 `[GameSession+0x3c]` 比，
    **不等就整包丢掉**，而那个计数器纯客户端本地、服务端设不了。
    同一个房间里多打一局就会分叉（线上实测：老玩家 3、新进来的 1），
    于是双向同步全被丢，症状就是「别人一动不动」。转发时按收件人自己
    最近自报的号重新盖章即可 —— 校验和只覆盖 body，改这两字节不用重算。
    """

    def setUp(self):
        self.rooms = {}
        self.server = RelayServer(
            members_of=lambda conn: self.rooms.get(conn, []),
            fallback=lambda conn, packet: conn.fallback_got.append(packet),
            logger=lambda msg: self.logged.append(msg))
        self.logged = []
        self.alice, self.bob = FakeGameConn("alice"), FakeGameConn("bob")
        self.rooms = {self.alice: [self.bob], self.bob: [self.alice]}

    def test_the_receiver_gets_the_packet_stamped_with_its_own_game_id(self):
        # bob 先自报一次「我认 1」，alice 再发一发「我是 3」。
        self.server.deliver(self.bob, udp_packet(sender=1, game_id=1))
        self.alice.fallback_got.clear()
        self.server.deliver(self.alice, udp_packet(sender=0, game_id=3))
        got, = self.bob.fallback_got
        self.assertEqual(1, relayserver.peer_game_id(got))
        # 除了那两个字节，其余**逐字节相同** —— 校验和 / 序列号 / body 都没动
        original = udp_packet(sender=0, game_id=3)
        self.assertEqual(original[:4] + b"\x01\x00" + original[6:], got)

    def test_matching_game_ids_are_forwarded_untouched(self):
        self.server.deliver(self.bob, udp_packet(sender=1, game_id=7))
        self.alice.fallback_got.clear()
        packet = udp_packet(sender=0, game_id=7)
        self.server.deliver(self.alice, packet)
        self.assertEqual([packet], self.bob.fallback_got)
        self.assertEqual(0, self.server.restamped_total)

    def test_a_receiver_that_never_spoke_gets_the_packet_verbatim(self):
        # bob 一发都没发过 -> 不知道他认哪个号 -> 只能原样转（尽力而为）
        packet = udp_packet(sender=0, game_id=3)
        self.server.deliver(self.alice, packet)
        self.assertEqual([packet], self.bob.fallback_got)

    def test_the_gap_is_logged_once_not_once_per_packet(self):
        self.server.deliver(self.bob, udp_packet(sender=1, game_id=1))
        for seq in range(50):
            self.server.deliver(self.alice,
                                udp_packet(sender=0, game_id=3, sequence=seq))
        gaps = [line for line in self.logged if "局号分叉" in line]
        self.assertEqual(1, len(gaps))
        self.assertEqual(50, self.server.restamped_total)

    def test_a_payload_that_is_not_a_udp_packet_is_never_rewritten(self):
        # 太短、或者魔数不对 —— 两个都原样返回，绝不抛（铁律 1）
        for junk in (b"", b"\xff\x00\x01", bytes(20)):
            self.assertIsNone(relayserver.peer_game_id(junk))
            self.assertEqual(bytes(junk),
                             relayserver.restamp_peer_game_id(junk, 5))
        packet = udp_packet(game_id=2)
        self.assertEqual(packet, relayserver.restamp_peer_game_id(packet, None))


if __name__ == "__main__":
    unittest.main()
