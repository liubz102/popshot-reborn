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


class PeerEpochTests(unittest.TestCase):
    """转发时的局号处置 —— **按代判定**（§218 / D137）。

    收包入口 `0x4078c4` 拿头里的局号和自己的 `[GameSession+0x3c]` 比，
    不等就整包丢掉。而那个号的每一次变化都是服务端某一发包造成的
    （`0x0400`/`0x0403` 各 +1，登录成功 / `0x0203` 归 -1），所以服务端手里
    本来就有「谁在第几代」这个硬事实：

    * **同一代**、编号不同（有人中途进房，起点不一样）-> 按收件人的编号盖章；
    * **不同代** -> 丢掉，不投递（改写会钉死收件人的队列基线，bug调查/9）。

    没有第三条出口，也没有任何时间阈值。
    """

    def setUp(self):
        self.logged = []
        self.rooms = {}
        self.server = RelayServer(
            members_of=lambda conn: self.rooms.get(conn, []),
            fallback=lambda conn, packet: conn.fallback_got.append(packet),
            logger=lambda msg: self.logged.append(msg))
        self.alice, self.bob = FakeGameConn("alice"), FakeGameConn("bob")
        self.rooms = {self.alice: [self.bob], self.bob: [self.alice]}
        self.gen = relayserver.next_generation()

    def anchor(self, conn, value, gen=None):
        """把一条假连接的局号模型设成 `value` 并锚定到某一代。

        真实路径上 `value` 是 `send()` 认出 `0x0400`/`0x0403` 数出来的，
        `gen` 是建房 / 进房时锚的（`Conn.anchor_epoch`）。
        """
        state = relayserver.epoch_state(conn)
        state.value = value
        state.anchor(self.gen if gen is None else gen)
        return state

    # -- 同一代：编号不同就盖章（bug调查/8_2「别人一动不动」）-----------------
    def test_the_receiver_gets_the_packet_stamped_with_its_own_number(self):
        """老玩家 3 / 中途进房的人 1，**同一代**（同一发 0x0400 换的代）。"""
        self.anchor(self.alice, 3)
        self.anchor(self.bob, 1)
        self.server.deliver(self.alice, udp_packet(sender=0, game_id=3))
        got, = self.bob.fallback_got
        self.assertEqual(1, relayserver.peer_game_id(got))
        # 除了那两个字节，其余**逐字节相同** —— 校验和 / 序列号 / body 都没动
        original = udp_packet(sender=0, game_id=3)
        self.assertEqual(original[:4] + bytes([1, 0]) + original[6:], got)

    def test_matching_numbers_are_forwarded_untouched(self):
        self.anchor(self.alice, 7)
        self.anchor(self.bob, 7)
        packet = udp_packet(sender=0, game_id=7)
        self.server.deliver(self.alice, packet)
        self.assertEqual([packet], self.bob.fallback_got)
        self.assertEqual(0, self.server.restamped_total)

    def test_the_restamp_is_logged_once_not_once_per_packet(self):
        self.anchor(self.alice, 3)
        self.anchor(self.bob, 1)
        for seq in range(50):
            self.server.deliver(self.alice,
                                udp_packet(sender=0, game_id=3, sequence=seq))
        notes = [line for line in self.logged if "同代改写局号" in line]
        self.assertEqual(1, len(notes))
        self.assertEqual(50, self.server.restamped_total)
        self.assertEqual(50, len(self.bob.fallback_got))

    # -- 换代的那一刹那：跨代一律丢（bug调查/9「第二局打不死人」）-------------
    def test_a_packet_from_the_previous_generation_is_dropped(self):
        """收件人已经换代、发送方还在用旧号 —— 现场那一发毒心跳就是这样。

        改写会把上一代的 `0x4001` 喂进刚清空的 `PktQueue`，`FlushTo` 把基线
        钉在旧纪元的序号上（`base` 只进不退），整局事件包全被 `seq < base`
        丢光。原样转发也不行（见下面那条撞号的用例）。**只能丢。**
        """
        self.anchor(self.alice, 0)
        self.anchor(self.bob, 0)
        relayserver.epoch_state(self.bob).advance(relayserver.next_generation())
        self.server.deliver(self.alice, udp_packet(sender=0, game_id=0))
        self.assertEqual([], self.bob.fallback_got)
        self.assertEqual(1, self.server.cross_gen_dropped)
        self.assertEqual(0, self.server.restamped_total)
        self.assertTrue(any("跨代丢弃" in line for line in self.logged))

    def test_a_freshly_changed_sender_is_dropped_too(self):
        """发送方先换代、收件人还没换 —— 对称，同样丢。

        这一路要是放行，收件人会把**新一代**的包收进**旧一代**的队列，
        而那条队列马上就要被 `ResetQueues` 清掉 —— 白投递还可能占槽位。
        """
        self.anchor(self.alice, 0)
        self.anchor(self.bob, 0)
        relayserver.epoch_state(self.alice).advance(
            relayserver.next_generation())
        self.server.deliver(self.alice, udp_packet(sender=0, game_id=1))
        self.assertEqual([], self.bob.fallback_got)
        self.assertEqual(1, self.server.cross_gen_dropped)

    def test_a_stale_number_colliding_with_the_receiver_is_still_dropped(self):
        """★ 这条就是 D134「原样转发让客户端自己丢」挡不住的那一发。

        中途进房的人和老玩家**编号起点不同**，所以「发送方的旧号」完全可能
        正好等于「收件人的当前号」—— 原样转发的话收件人会**照单全收**，
        正是最毒的那一发。按代判定不看数字，直接丢。
        """
        self.anchor(self.alice, 2)          # alice 上一代的号是 2
        self.anchor(self.bob, 1)
        state = relayserver.epoch_state(self.bob)
        state.advance(relayserver.next_generation())
        state.value = 2                     # bob 新一代的号恰好也是 2
        state.gen_of[2] = state.gen
        self.server.deliver(self.alice, udp_packet(sender=0, game_id=2))
        self.assertEqual([], self.bob.fallback_got)
        self.assertEqual(1, self.server.cross_gen_dropped)

    def test_the_cross_generation_note_is_logged_once(self):
        self.anchor(self.alice, 0)
        self.anchor(self.bob, 0)
        relayserver.epoch_state(self.bob).advance(relayserver.next_generation())
        for seq in range(30):
            self.server.deliver(self.alice,
                                udp_packet(sender=0, game_id=0, sequence=seq))
        notes = [line for line in self.logged if "跨代丢弃" in line]
        self.assertEqual(1, len(notes))
        self.assertEqual(30, self.server.cross_gen_dropped)

    def test_delivery_resumes_the_moment_the_last_member_switches(self):
        """★ 「过渡期」= 从服务端发出换代包，到最后一个人换完为止。

        一毫秒不多不少 —— 不需要任何时间阈值。
        """
        self.anchor(self.alice, 0)
        self.anchor(self.bob, 0)
        gen2 = relayserver.next_generation()
        relayserver.epoch_state(self.bob).advance(gen2)      # bob 先换
        self.server.deliver(self.alice, udp_packet(sender=0, game_id=0))
        self.assertEqual([], self.bob.fallback_got)          # 过渡期里：丢
        relayserver.epoch_state(self.alice).advance(gen2)    # alice 也换完了
        packet = udp_packet(sender=0, game_id=1)
        self.server.deliver(self.alice, packet)
        self.assertEqual([packet], self.bob.fallback_got)    # 立刻恢复
        self.assertEqual(0, self.server.restamped_total)     # 而且不用改写

    # -- 模型和自报值对不上 ---------------------------------------------------
    def test_an_unknown_number_during_a_pending_change_is_dropped(self):
        """换代还没被确认，又冒出一个不认识的号 -> 当陈旧包丢，不重锚。

        这段窗口正是毒包所在的地方，宁可丢也不放行。
        """
        self.anchor(self.alice, 0)
        self.anchor(self.bob, 0)
        relayserver.epoch_state(self.alice).advance(self.gen)   # pending
        self.server.deliver(self.alice, udp_packet(sender=0, game_id=99))
        self.assertEqual([], self.bob.fallback_got)
        self.assertEqual(1, self.server.cross_gen_dropped)
        self.assertEqual(0, self.server.epoch_confused)

    def test_an_unknown_number_after_the_change_settled_re_anchors(self):
        """换代已经确认之后再对不上 -> 值重锚到自报值，**代不动**，继续按代转发。"""
        self.anchor(self.alice, 0)
        self.anchor(self.bob, 0)
        self.server.deliver(self.alice, udp_packet(sender=0, game_id=42))
        got, = self.bob.fallback_got
        self.assertEqual(0, relayserver.peer_game_id(got))   # 按 bob 的号盖章
        self.assertEqual(1, self.server.epoch_confused)
        self.assertEqual(42, relayserver.epoch_state(self.alice).value)
        self.assertEqual(self.gen, relayserver.epoch_state(self.alice).gen)
        self.assertTrue(any("换代模型失准" in line for line in self.logged))

    def test_reset_puts_the_model_back_to_minus_one(self):
        """`0x0203` / 登录成功 -> 客户端 `GameSession::Reset`（`0x4054fa`）。"""
        state = self.anchor(self.alice, 5)
        state.reset()
        self.assertEqual(relayserver.EPOCH_UNSET, state.value)
        self.assertIsNone(state.gen)
        self.assertEqual({}, state.gen_of)

    def test_history_lets_an_in_flight_old_number_be_classified(self):
        """换代那一刹那客户端手里还有旧号的包 —— 靠历史表归到上一代。"""
        state = self.anchor(self.alice, 3)
        old_gen = state.gen
        state.advance(relayserver.next_generation())
        self.assertEqual(old_gen, state.generation_of(3))
        self.assertEqual(state.gen, state.generation_of(4))

    def test_the_history_is_bounded(self):
        state = self.anchor(self.alice, 0)
        for _ in range(relayserver.EPOCH_HISTORY * 3):
            state.advance(state.gen)
        self.assertLessEqual(len(state.gen_of), relayserver.EPOCH_HISTORY)

    # -- 补锚 -----------------------------------------------------------------
    def test_an_unanchored_connection_is_anchored_by_the_callback(self):
        """防御性的第二道：万一哪条路径漏了锚定，按它当前房间补一次。"""
        gen = relayserver.next_generation()
        server = RelayServer(
            members_of=lambda conn: self.rooms.get(conn, []),
            fallback=lambda conn, packet: conn.fallback_got.append(packet),
            generation_of=lambda conn: gen,
            logger=lambda msg: self.logged.append(msg))
        packet = udp_packet(sender=0, game_id=relayserver.EPOCH_UNSET & 0xFFFF)
        server.deliver(self.alice, packet)
        self.assertEqual([packet], self.bob.fallback_got)
        self.assertEqual(gen, relayserver.epoch_state(self.bob).gen)
        self.assertTrue(any("换代锚定" in line for line in self.logged))
        # ★ 「还没进过任何一局」的客户端盖的是 0xFFFF = -1（`0x4054fa` 的
        #   `or ..., 0xffffffff`）。模型里存的就是 -1，**不能**当成陌生的 65535
        #   ——否则每条刚进来的连接都会被判「模型失准」。
        self.assertEqual(0, server.epoch_confused)
        self.assertEqual(relayserver.EPOCH_UNSET,
                         relayserver.epoch_state(self.alice).value)

    def test_the_wire_value_is_read_back_as_a_signed_number(self):
        for wire, want in ((0xFFFF, -1), (0, 0), (3, 3), (0x8000, -32768)):
            self.assertEqual(want, relayserver.as_signed_epoch(wire))
        self.assertIsNone(relayserver.as_signed_epoch(None))

    def test_a_client_that_never_entered_a_round_is_delivered_untouched(self):
        """两边都还是 -1（进房前）—— 同代同号，一个字节都不该动。"""
        self.anchor(self.alice, relayserver.EPOCH_UNSET)
        self.anchor(self.bob, relayserver.EPOCH_UNSET)
        packet = udp_packet(sender=0, game_id=0xFFFF)
        self.server.deliver(self.alice, packet)
        self.assertEqual([packet], self.bob.fallback_got)
        self.assertEqual(0, self.server.restamped_total)
        self.assertEqual(0, self.server.epoch_confused)

    def test_a_payload_that_is_not_a_udp_packet_is_never_rewritten(self):
        # 太短、或者魔数不对 —— 两个都原样返回，绝不抛（铁律 1）
        for junk in (b"", bytes([0xFF, 0, 1]), bytes(20)):
            self.assertIsNone(relayserver.peer_game_id(junk))
            self.assertEqual(bytes(junk),
                             relayserver.restamp_peer_game_id(junk, 5))
        packet = udp_packet(game_id=2)
        self.assertEqual(packet, relayserver.restamp_peer_game_id(packet, None))

    def test_a_non_udp_payload_is_still_delivered(self):
        """不是 `UdpPacket` 就谈不上代，照常投递（别把别的东西一起丢了）。"""
        self.anchor(self.alice, 0)
        self.anchor(self.bob, 0)
        junk = bytes([0xFF, 0, 1])
        self.server.deliver(self.alice, junk)
        self.assertEqual([junk], self.bob.fallback_got)



# ----------------------------------------------------------------------------
# 客户端收包队列的模型 + bug调查/9「最后复现」那一局的字节流回放
# ----------------------------------------------------------------------------
#: 内层 opcode 的名字。`GameSession::ProcessReliableQueue`（`0x407c84`）里那张
#: switch 表每个分支都 push 一个宽字符串，逐个读出来的（FINDINGS §216）。
#: 名字很重要：现场那一局被吃掉的**第一发就是 `rpChangeWeapon`**。
RP_NAMES = {
    0x01: "rpChangeWeapon", 0x02: "rpFire", 0x03: "rpExplode",
    0x04: "rpSplashDamaged", 0x05: "rpSetOnFire", 0x06: "rpJump",
    0x07: "rpDash", 0x0B: "rpCrouch", 0x0C: "rpRespawn",
    0x0D: "rpReqState", 0x0E: "rpRepState", 0x0F: "rpReqDie",
    0x11: "rpAiMsg", 0x18: "rpGuard", 0x1B: "rpCreateTotem",
}


class PktQueue:
    """客户端每座位一条收包队列：`[GameSession + 座位*0x24 + 0x2e4]`（§216）。

    字段和每条分支都是对着 `re/BigShot_22524.img` 抄的，地址写在方法上。
    为什么要在测试里养一份客户端模型：**中毒的队列在受害者那台机器上**，
    而探针只挂得上「打不死人」的那一台（他自己的队列全是好的）。
    现场那一侧唯一能复原的办法，就是把服务端 dump 里那串**真实字节**
    喂进这份模型。
    """

    def __init__(self):
        self.reset()

    def reset(self):                                    # 0x54bade
        self.live = 0           # +0x04 「这个座位的通道已激活」
        self.base = 0           # +0x08 最老的序号，只进不退
        self.high = 0           # +0x0c 上界 = 已分配到的序号 + 1
        self.pending = 0        # +0x10 待收（空槽数）
        self.slots = []         # +0x18 槽位，下标 = seq - base
        self.hole = False       # [GameSession + 座位 + 0x2b0] 空洞标记

    def grow(self, n):                                  # 0x54bb66
        if n < self.high:
            return
        self.pending += (n + 1) - self.high
        want = (n + 1) - self.base
        if want < len(self.slots):
            del self.slots[want:]
        else:
            self.slots.extend([None] * (want - len(self.slots)))
        self.high = n + 1

    def insert(self, seq, item):                        # 0x54bb8c
        if seq < self.base:                             # 太老 -> 静默丢弃
            return False
        self.grow(seq)
        if self.slots[seq - self.base] is not None:     # 收过了 -> 丢弃
            return False
        self.slots[seq - self.base] = item
        self.pending -= 1
        return True

    def flush_to(self, n):                              # 0x54bb1d
        if n < self.base:                               # ★ 比 base 还老就直接回
            return                                      #   （连「已激活」都不置）
        del self.slots[:n - self.base]
        self.base = n
        self.live = 1                                   # 0x54bb60

    def get(self, seq):                                 # 0x54bc0b
        if seq < self.base or seq >= self.high:
            return None
        return self.slots[seq - self.base]


class PeerClient:
    """一台客户端的收包侧：局号（= 队列的纪元号）+ 六条队列 + 消费者。"""

    def __init__(self, seat, epoch=0):
        self.seat = seat
        self.epoch = epoch
        self.queues = [PktQueue() for _ in range(6)]
        self.dispatched = []            # [(座位, 序号, 内层 opcode)]
        self.dropped_by_epoch = 0

    def next_round(self):
        """阶段切换（`0x5517a3` / `0x55190b`）。`inc [GameSession+0x3c]` 和
        `ResetQueues`（`0x407678`）在**同一个基本块**里，永远一起发生。"""
        self.epoch += 1
        for queue in self.queues:
            queue.reset()

    def on_peer_packet(self, blob):
        """收包入口 `0x4078a7`。"""
        if len(blob) < 12 or blob[0] != 0xFF:                   # 0x4078ab
            return
        sender, target = struct.unpack_from("<bb", blob, 1)
        game_id, _sum, sequence, inner = struct.unpack_from("<HHHH", blob, 4)
        if game_id != self.epoch:                               # ★ 0x4078c4
            self.dropped_by_epoch += 1                          #   不等 -> 整包丢
            return
        if not -1 <= sender < 6 or not -1 <= target < 6:        # 0x4078c9/0x4078dd
            return
        # 校验和（`0x4078f0`）这里不校：转发只动头里的局号，body 一个字节没变，
        # 所以现场那些包在真客户端上照样过校验（§213）。
        if inner >= 0x4000:
            if inner == 0x4001:                                 # 0x407b94 心跳
                nxt, = struct.unpack_from("<H", blob, 12)
                queue = self.queues[sender]
                queue.grow(nxt - 1)
                if not queue.live:                              # 0x407bb7
                    queue.flush_to(nxt)                         # ★ 基线一次定死
            return
        self.queues[sender].insert(sequence, inner)             # 0x407c01

    def tick(self):
        """`GameSession::ProcessReliableQueue`（`0x407c84`）跑一轮六个座位。"""
        for seat, queue in enumerate(self.queues):
            if not queue.live and seat != self.seat:            # 0x407c94/0x407ca3
                continue
            cursor = queue.base
            while cursor < queue.high:                          # 0x407dd1
                inner = queue.get(cursor)
                if inner is None:                               # 0x407cc9 空洞
                    queue.hole = True                           # 0x407e3c
                    break                                       #   （还会讨重传）
                self.dispatched.append((seat, cursor, inner))
                cursor += 1
            queue.flush_to(cursor)                              # 0x407e4d


#: bug调查/9「最后复现」（2026-08-19 现场）那一局里，dk（座位 2）在局号 1
#: 发出的 **112 发事件包**的内层 opcode，逐发照抄服务端逐连接 dump
#: （`game_032_27799.txt`；序号 0..111 一个不缺，服务端也逐发投出去了）。
#: 第一发就是 `rpChangeWeapon`，前 31 发里还有 9 发 `rpFire`。
FIELD_EVENT_OPCODES = [int(x, 16) for x in (
    "01 02 03 03 06 06 06 06 02 03 03 02 03 03 02 03 03 02 03 03 02 03 03 "
    "02 03 04 03 02 03 03 02 03 03 02 03 03 02 03 03 02 03 03 02 03 03 02 "
    "03 03 02 03 03 02 03 03 02 03 03 02 03 04 03 02 03 04 03 02 03 04 03 "
    "02 03 03 06 06 06 02 03 03 04 04 02 03 03 04 04 01 01 01 02 03 03 02 "
    "03 04 03 02 03 03 02 03 03 02 03 03 02 03 03 04 04 02 03 03"
).split()]

#: 现场那一发毒心跳里的 N —— dk 在**上一纪元**（房间里换了几次角色）
#: 已经发到第 31 号了。服务端 15:03:30.036 把它从局号 0 改写成 1 投了出去。
FIELD_POISON_N = 31


class EpochRaceFieldReplayTests(unittest.TestCase):
    """把现场那一局的字节流走一遍**真的转发代码**，看受害者队列的下场。

    现场（服务端时间，`bug调查/9/最后复现/logs-server`）：

    ```text
    15:03:29.964  座位0 自己切到局号 1（= ResetQueues，dk 的队列 base=0/未激活）
    15:03:30.036  收到 座位2 内层=0x4001 N=31 —— dk 原本发的是局号 0！
    15:03:30.390  dk 这才切到局号 1（晚 426 ms）
    15:03:46.767  dk 本局第一发事件 seq=0 = rpChangeWeapon
    ```

    两条用例只差一件事：**服务端知不知道这一局换代了**。

    * 知道（现在的做法，D137）：广播 `0x0400` 那一刻两条连接的模型一起进新一代，
      dk 那发用旧号的心跳属于**上一代** -> 丢掉，一个字节都不投；
    * 不知道（D131 的老做法）：服务端只看见「两个数字不一样」，
      于是把它改写成收件人的号放行 -> 基线被钉死 -> 整局零伤害。
    """

    def setUp(self):
        self.logged = []
        self.rooms = {}
        self.server = RelayServer(
            members_of=lambda conn: self.rooms.get(conn, []),
            fallback=lambda conn, packet: conn.fallback_got.append(packet),
            logger=lambda msg: self.logged.append(msg))
        self.dk, self.victim_conn = FakeGameConn("dk"), FakeGameConn("test2")
        self.rooms = {self.dk: [self.victim_conn],
                      self.victim_conn: [self.dk]}
        self.victim = PeerClient(seat=0, epoch=0)
        # 两个人都在房里，同一代、局号都是 0（= 上一局打完回房间之后的状态）。
        self.room_gen = relayserver.next_generation()
        for conn in (self.dk, self.victim_conn):
            state = relayserver.epoch_state(conn)
            state.value = 0
            state.anchor(self.room_gen)

    # -- 回放 ---------------------------------------------------------------
    def _pump(self):
        """把服务端投给受害者的东西喂进客户端模型，每发之后跑一轮消费者。"""
        for blob in self.victim_conn.fallback_got:
            self.victim.on_peer_packet(blob)
            self.victim.tick()
        self.victim_conn.fallback_got.clear()

    def _heartbeat(self, sender, game_id, nxt):
        return udp_packet(sender=sender, game_id=game_id, inner=0x4001,
                          body=struct.pack("<H", nxt) + bytes(29))

    def _replay(self, server_knows_the_round_changed):
        """`False` 等价于 D131 那份服务端：不知道换代，只会比数字。"""
        # ① dk 在房间里（局号 0）已经待了一分多钟，事件发到第 31 号
        self.server.deliver(self.dk, self._heartbeat(2, 0, FIELD_POISON_N))
        self.server.deliver(self.victim_conn, self._heartbeat(0, 0, 1))
        self._pump()

        # ② 服务端广播 0x0400 = 换代。真实的服务端**在发出那一刻**就知道
        #    房里每个人都要 +1 进新一代（`Conn.note_epoch_from_frame`）。
        battle_gen = relayserver.next_generation()
        if server_knows_the_round_changed:
            for conn in (self.dk, self.victim_conn):
                relayserver.epoch_state(conn).advance(battle_gen)

        # ③ 受害者那台先切（清六条队列），并开始用新局号 1 发包
        self.victim.next_round()
        self.server.deliver(self.victim_conn, self._heartbeat(0, 1, 0))
        self.victim_conn.fallback_got.clear()       # 自己发的不会回给自己

        # ④ ★ 现场那一发：dk 晚了 426 ms，还在用旧局号 0，心跳里 N=31
        self.server.deliver(self.dk, self._heartbeat(2, 0, FIELD_POISON_N))
        self._pump()
        poisoned = (self.victim.queues[2].live, self.victim.queues[2].base)

        # ⑤ dk 也切到局号 1，先来的是心跳 N=0（读图那 8 秒只有心跳，没有事件）
        self.server.deliver(self.dk, self._heartbeat(2, 1, 0))
        self._pump()

        # ⑥ 112 发事件，每发之后跟一发 N=seq+1 的心跳（现场就是这个顺序）
        for seq, inner in enumerate(FIELD_EVENT_OPCODES):
            self.server.deliver(self.dk, udp_packet(sender=2, game_id=1,
                                                    sequence=seq, inner=inner))
            self.server.deliver(self.dk, self._heartbeat(2, 1, seq + 1))
            self._pump()
        return poisoned

    # -- D131 的老做法（服务端不知道换代）------------------------------------
    def test_a_blind_server_restamps_and_eats_the_opening_of_the_round(self):
        """只比数字 -> 改写放行那一发 -> `base` 钉在 31 -> 开局 31 发全被吃掉。"""
        live, base = self._replay(server_knows_the_round_changed=False)
        self.assertEqual((1, FIELD_POISON_N), (live, base))     # 一发定死基线
        # 两个方向都被改写：受害者切完后发给 dk 的那一发，和 dk 那发旧号心跳
        # —— 后者就是毒药。老服务端只看得见「数字不一样」，两发一视同仁。
        self.assertEqual(2, self.server.restamped_total)

        seen = [seq for _seat, seq, _inner in self.victim.dispatched]
        self.assertEqual(list(range(FIELD_POISON_N, 112)), seen)

        lost = FIELD_EVENT_OPCODES[:FIELD_POISON_N]
        # ★ 掉的第一发就是本局的 rpChangeWeapon，还带走 9 发 rpFire ——
        #   对象句柄是「每座位一个计数器、两边各自按同一顺序分配」的
        #   （现场每发 rpFire 步长恒定 +4），少喂一发 rpFire 之后
        #   dk 所有 rpExplode 里的句柄在受害者那台就永远对不上 =>
        #   **整局零伤害**，正是「看得见、血剩一丝、就是打不死」。
        self.assertEqual(0x01, lost[0])
        self.assertEqual(9, lost.count(0x02))
        # 消费者从头到尾没撞见空洞（base 前面的号是被 Insert 丢的，不算空洞）
        # —— 所以现场三份 dump 里**一发 0x4002 讨重传都没有**，对得上。
        self.assertFalse(self.victim.queues[2].hole)

    # -- D137 的做法（服务端自己维护换代状态）--------------------------------
    def test_the_generation_model_keeps_the_whole_round_intact(self):
        """服务端在发出 `0x0400` 那一刻就知道换代了 -> 那一发跨代包直接丢。"""
        live, base = self._replay(server_knows_the_round_changed=True)
        self.assertEqual((0, 0), (live, base))          # 队列还是干净的
        self.assertEqual(0, self.server.restamped_total)
        # 只有 dk 那一发旧号心跳被丢；受害者切完之后发给 dk 的那一发是同代的
        # （两边的模型在广播那一刻一起进的新代），照常投递。
        self.assertEqual(1, self.server.cross_gen_dropped)
        # ★ 服务端拦下了 -> 客户端**一发都不用自己丢**（老做法要靠客户端丢，
        #   而收件人的号一旦和发送方的旧号撞上，客户端就丢不掉了）。
        self.assertEqual(0, self.victim.dropped_by_epoch)

        seen = [(seq, inner) for _seat, seq, inner in self.victim.dispatched]
        self.assertEqual(list(enumerate(FIELD_EVENT_OPCODES)), seen)
        self.assertEqual(112, self.victim.queues[2].base)
        self.assertFalse(self.victim.queues[2].hole)



class PktQueueModelTests(unittest.TestCase):
    """上面那份客户端模型自己的用例 —— 会话 34 在**真客户端**上逐条测过的
    那张表（FINDINGS §216「会话 34 实机验证」），照抄成断言，防止模型漂了。
    """

    def setUp(self):
        self.client = PeerClient(seat=1, epoch=0)
        self.queue = self.client.queues[0]

    def feed(self, blob):
        self.client.on_peer_packet(blob)
        self.client.tick()

    def event(self, seq, inner=0x02):
        # 局号跟着客户端当前的走 —— 不然换局之后自己就被收包门丢掉了
        return udp_packet(sender=0, game_id=self.client.epoch,
                          sequence=seq, inner=inner)

    def beat(self, nxt):
        return udp_packet(sender=0, game_id=self.client.epoch, inner=0x4001,
                          body=struct.pack("<H", nxt) + bytes(29))

    def test_events_before_the_first_heartbeat_only_queue_up(self):
        # 「队列没激活」时事件包只入队不派发：上界/槽位涨，base 和已激活不动
        for seq in range(7):
            self.feed(self.event(seq))
        self.assertEqual((0, 0, 7), (self.queue.live, self.queue.base,
                                     self.queue.high))
        self.assertEqual([], self.client.dispatched)

    def test_the_first_heartbeat_pins_the_baseline_and_activates(self):
        self.feed(self.beat(500))
        self.assertEqual((1, 500, 500), (self.queue.live, self.queue.base,
                                         self.queue.high))
        self.assertEqual([], self.queue.slots)
        # 之后比 base 老的号静默丢弃，一个字节都不变
        before = (self.queue.base, self.queue.high, len(self.queue.slots))
        for seq in (2, 3):
            self.feed(self.event(seq))
        self.assertEqual(before, (self.queue.base, self.queue.high,
                                  len(self.queue.slots)))

    def test_the_consumer_stalls_on_a_hole_and_asks_for_a_resend(self):
        self.feed(self.beat(500))
        self.feed(self.beat(501))               # 已激活了 -> 只 Grow，不再定基线
        for seq in (501, 502):
            self.feed(self.event(seq))
        self.assertEqual(500, self.queue.base)          # 停在永远不会来的 500
        self.assertEqual(503, self.queue.high)
        self.assertTrue(self.queue.hole)
        self.assertEqual([], self.client.dispatched)

    def test_a_small_poison_heals_as_soon_as_that_number_shows_up(self):
        """★ 会话 34 那条「自己恢复了」—— 也是 §216 当时对不上现场的地方：
        队列本身确实会在 seq == base 那一发恢复，可**游戏状态不会**
        （句柄分配器已经错位），所以现场还是整局零伤害。
        """
        self.feed(self.beat(4))
        for seq in range(4):
            self.feed(self.event(seq))
        self.assertEqual([], self.client.dispatched)     # 0..3 全丢
        self.feed(self.event(4))
        self.assertEqual([(0, 4, 0x02)], self.client.dispatched)
        self.assertEqual(5, self.queue.base)

    def test_a_reset_wipes_everything_and_the_next_beat_re_seeds_it(self):
        self.feed(self.beat(9))
        self.assertEqual((1, 9), (self.queue.live, self.queue.base))
        self.client.next_round()                        # 阶段切换
        self.assertEqual((0, 0, 0), (self.queue.live, self.queue.base,
                                     self.queue.high))
        self.feed(self.beat(0))                         # 新纪元第一发心跳
        self.assertEqual((1, 0), (self.queue.live, self.queue.base))

    def test_my_own_seat_is_consumed_even_before_any_heartbeat(self):
        # 消费者只对**别人**的座位要求「已激活」；自己那条一直跑（0x407ca3）
        own = self.client.queues[self.client.seat]
        self.client.on_peer_packet(udp_packet(sender=1, game_id=self.client.epoch,
                                             sequence=0, inner=0x02))
        self.client.tick()
        self.assertEqual([(1, 0, 0x02)], self.client.dispatched)
        self.assertEqual(1, own.base)

    def test_a_duplicate_sequence_number_is_swallowed(self):
        # §151：局域网里 UDP 那一路和 0x040f 会各送一份，靠这条去重
        self.feed(self.beat(0))
        self.feed(self.event(0))
        self.feed(self.event(0))
        self.assertEqual([(0, 0, 0x02)], self.client.dispatched)


class StatusLineTests(unittest.TestCase):
    """`status()` 那一行必须把**三条**出路都报出来（§225 第六节）。

    ★ `delivered_udp` 是**主路**：`bug调查/udp验证` 那一局 50160 人次
      投递里 97.6% 走的是位置数据的 UDP 旁路。只报「中继 / 回退」会让人
      以为位置数据还在走 TCP，正好把排查引到反方向。
    """

    def test_the_status_line_reports_all_three_delivery_routes(self):
        server = RelayServer()
        server.delivered_udp = 7
        server.delivered_relay = 3
        server.delivered_fallback = 1
        line = server.status()
        self.assertIn("UDP 7", line)
        self.assertIn("中继 3", line)
        self.assertIn("回退 1", line)

    def test_the_control_channel_can_actually_ask_for_it(self):
        """★ 以前 `status()` **一个调用者都没有** —— 修好了也没人看得见。"""
        reply = gameserver.handle_control_command("relay")
        self.assertTrue(reply.startswith("ok "), reply)
        self.assertIn("投递 UDP", reply)



class StuckRelayClientTests(unittest.TestCase):
    """★★★ **一个卡死的中继客户端不许拖住别人**（D108）。

    中继的投递是一个人一个人挨着发的，而 `send_frame()` 以前是「加密 + 写
    socket」一体的，写带 `RELAY_SEND_DEADLINE_S`（2 秒）的截止时间 ——
    排在卡死那个人后面的所有人跟着一起停。D106 之后那就是整个房间的 bot。

    ★ 中继超时后的**处置一个字没改**（铁律 1）：只标 `send_broken`、
    不关连接，投递自动回退 `0x040f`。
    """

    class StuckSocket:
        def __init__(self):
            self.gate = threading.Event()
            self.entered = threading.Event()
            self.writes = []

        def sendall(self, data):
            self.entered.set()
            self.gate.wait(10.0)
            self.writes.append(bytes(data))

    def make_conn(self, sock):
        conn = relayserver.RelayConn.__new__(relayserver.RelayConn)
        conn.sock = sock
        conn.cout = SimpleCipher.server_to_client()
        conn.send_lock = threading.Lock()
        conn.closed = False
        conn.send_broken = False
        conn.frames_out = 0
        conn.data_out = 0
        conn.addr = ("127.0.0.1", 40000)
        conn.log = lambda _msg: None
        return conn

    def test_send_frame_returns_at_once_even_when_the_client_never_reads(self):
        sock = self.StuckSocket()
        conn = self.make_conn(sock)
        self.addCleanup(sock.gate.set)
        started = time.monotonic()
        self.assertTrue(conn.send_data(b"\xff" * 43))
        self.assertLess(time.monotonic() - started, 1.0,
                        "send_data() 被卡死的中继客户端堵住了")
        self.assertEqual([], sock.writes, "这会儿还没写出去才对")
        sock.gate.set()
        deadline = time.monotonic() + 5.0
        while not sock.writes and time.monotonic() < deadline:
            time.sleep(0.002)
        self.assertEqual(1, len(sock.writes))

    def test_a_stuck_relay_client_does_not_hold_up_anybody_else(self):
        stuck_sock = self.StuckSocket()
        stuck = self.make_conn(stuck_sock)
        self.addCleanup(stuck_sock.gate.set)
        healthy_sock = self.StuckSocket()
        healthy_sock.gate.set()             # 这条一直收得走
        healthy = self.make_conn(healthy_sock)

        stuck.send_data(b"\xff" * 43)
        self.assertTrue(stuck_sock.entered.wait(5.0))
        started = time.monotonic()
        healthy.send_data(b"\xee" * 43)
        deadline = time.monotonic() + 5.0
        while not healthy_sock.writes and time.monotonic() < deadline:
            time.sleep(0.002)
        self.assertEqual(1, len(healthy_sock.writes))
        self.assertLess(time.monotonic() - started, 1.0,
                        "健康的那条被卡死的那条拖住了")
        self.assertEqual([], stuck_sock.writes)

    def test_a_backlog_that_never_drains_marks_the_stream_broken(self):
        """★ 积压太久 ⇒ `send_broken`（**不关连接**，投递回退 `0x040f`）。"""
        sock = self.StuckSocket()
        conn = self.make_conn(sock)
        self.addCleanup(sock.gate.set)
        conn.send_data(b"\xff" * 43)
        self.assertTrue(sock.entered.wait(5.0), "发送线程没起来")
        conn.send_data(b"\xff" * 43)        # 这一份留在队列里
        conn.outbox_since -= relayserver.RELAY_SEND_DEADLINE_S + 1.0
        self.assertEqual(0, conn.send_data(b"\xff" * 43))
        self.assertTrue(conn.send_broken)
        self.assertFalse(conn.closed, "铁律 1：不关连接，只绕开它")

if __name__ == "__main__":
    unittest.main()
