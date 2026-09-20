#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在场证据（`udpsync.MSG_PRESENCE`）—— 客户端报、中继转、游戏服记。

出处 bug调查/25（§62 / D53）：328800963 连续 14 小时显示「游戏中·任务」。
不是判据写错了，是**单人任务房里服务端是瞎的** —— 房里只有他一个人，客户端
就不发 `0x040e`，挂机判定的「键盘」那条整个不存在，只剩「打中 / 捡到 / 得分」；
而他开着连点器，分数每 1.5 秒涨一次，证据比真人还多。

⇒ 让 `bshook` 把服务端**结构上看不见**的四件事报过来：键盘 / 鼠标 / 这台机器
整机空闲（`GetLastInputInfo`）/ 游戏窗口在不在前台。

★★ **这一版只记不判**（用户 2026-09-20 拍板的三步走的第 2 步）。所以本模块
钉的是「路通不通、数对不对、日志会不会刷屏」，**不是**「谁会被判成挂机」——
那条线要等线上一天的真数据，照 §111 的老规矩在两份语料上量出来再定。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import udpsync  # noqa: E402


class WireFormatTests(unittest.TestCase):

    def test_a_round_trip_keeps_every_field(self):
        blob = udpsync.build_presence(1234, 5678, 90123, True, 0)
        self.assertEqual((1234, 5678, 90123, True, 0),
                         udpsync.parse_presence(blob))

    def test_never_is_distinct_from_just_now(self):
        # ★ 「从来没按过」和「刚刚按过」必须分得开：一个是最强的挂机证据，
        #   一个是最强的反证。混成一个值这个功能就废了。
        never = udpsync.build_presence(udpsync.PRESENCE_NEVER, 0, 0, True)
        just = udpsync.build_presence(0, 0, 0, True)
        self.assertNotEqual(never, just)
        self.assertEqual(udpsync.PRESENCE_NEVER,
                         udpsync.parse_presence(never)[0])
        self.assertEqual(0, udpsync.parse_presence(just)[0])

    def test_the_foreground_flag_survives_both_ways(self):
        for fg in (True, False):
            self.assertEqual(
                fg, udpsync.parse_presence(udpsync.build_presence(0, 0, 0, fg))[3])

    def test_it_does_not_collide_with_the_other_message_kinds(self):
        kinds = {udpsync.MSG_HELLO, udpsync.MSG_HELLO_ACK, udpsync.MSG_DATA,
                 udpsync.MSG_PING, udpsync.MSG_PONG}
        self.assertNotIn(udpsync.MSG_PRESENCE, kinds)

    def test_a_longer_datagram_still_parses(self):
        # 以后加字段时，**老服务端不能因为「长了」就整个丢掉**。
        blob = udpsync.build_presence(1, 2, 3, True, 0) + b"\x99" * 8
        self.assertEqual((1, 2, 3, True, 0), udpsync.parse_presence(blob))

    def test_a_short_one_is_refused(self):
        blob = udpsync.build_presence(1, 2, 3, True)[:-4]
        with self.assertRaises(udpsync.ProtocolError):
            udpsync.parse_presence(blob)

    def test_other_kinds_are_refused(self):
        with self.assertRaises(udpsync.ProtocolError):
            udpsync.parse_presence(udpsync.build_ping(udpsync.MSG_PING, 1))

    def test_the_wire_version_did_not_have_to_change(self):
        # 新增一个 kind 不改线格式版本：老服务端在 `_handle` 里认不出这个
        # kind，什么都不做（安静丢掉）—— 这正是「新客户端 + 老服务端」该有的
        # 行为。改版本号反而会把老客户端的**位置数据**一起废掉。
        self.assertEqual(b"PSU\x01", udpsync.MAGIC)


class _FakeConn:
    def __init__(self):
        self.seen = []

    def note_presence(self, kb, mouse, sysidle, fg, flags):
        self.seen.append((kb, mouse, sysidle, fg, flags))


class HubDispatchTests(unittest.TestCase):
    """`UdpHub` 收到这一发之后交给谁。"""

    def setUp(self):
        self.hub = udpsync.UdpSyncServer()
        self.conn = _FakeConn()
        self.addr = ("127.0.0.1", 40001)
        self.hub._by_addr[self.addr] = udpsync.Endpoint(self.conn, self.addr, 0.0)

    def test_it_reaches_the_connection_that_said_hello(self):
        self.hub._handle(udpsync.build_presence(11, 22, 33, False, 0),
                         self.addr, 1.0)
        self.assertEqual([(11, 22, 33, False, 0)], self.conn.seen)

    def test_an_unknown_sender_is_dropped_not_guessed(self):
        # HELLO 之前认不出是谁。★ 绝不能「猜一个最近的连接」——
        # 这个端口在公网上，猜错等于让任何人给别人塞证据。
        before = self.hub.unknown_in
        self.hub._handle(udpsync.build_presence(1, 2, 3, True),
                         ("127.0.0.1", 49999), 1.0)
        self.assertEqual([], self.conn.seen)
        self.assertEqual(before + 1, self.hub.unknown_in)

    def test_a_connection_without_the_method_is_not_a_crash(self):
        # 控制通道造的假连接、老版本的 Conn 都没有这个方法。
        self.hub._by_addr[self.addr] = udpsync.Endpoint(object(), self.addr, 0.0)
        self.hub._handle(udpsync.build_presence(1, 2, 3, True), self.addr, 1.0)

    def test_a_raising_connection_does_not_kill_the_receive_loop(self):
        class Boom:
            def note_presence(self, *a):
                raise RuntimeError("boom")
        self.hub._by_addr[self.addr] = udpsync.Endpoint(Boom(), self.addr, 0.0)
        self.hub._handle(udpsync.build_presence(1, 2, 3, True), self.addr, 1.0)

    def test_it_counts_as_a_sign_of_life_for_the_endpoint(self):
        # 在场证据也是「这个端点还活着」的证据 —— 否则单人局里玩家不发位置
        # 数据，端点会被 DEAD_AFTER_S 剪掉，下一发又变成「不认识的发送方」。
        self.hub._handle(udpsync.build_presence(1, 2, 3, True), self.addr, 9.0)
        self.assertEqual(9.0, self.hub._by_addr[self.addr].last_seen)


class RelayForwardingTests(unittest.TestCase):
    """中继必须**原样转发**，不许自作主张。"""

    def test_the_relay_forwards_it_verbatim(self):
        src = open(os.path.join(ROOT, "server", "relay.py"),
                   encoding="utf-8").read()
        body = src[src.index("def _on_hook_datagram"):]
        body = body[:body.index("\n    def ")]
        self.assertIn("udpsync.MSG_PRESENCE", body, "中继根本没认这个 kind")
        self.assertIn("self._to_remote(data)", body,
                      "中继没有原样转发（判定在游戏服，中继不该解它）")

    def test_the_relay_does_not_parse_the_payload(self):
        # 中继解它 = 以后加字段要同时重发中继。判定在游戏服，中继只管搬。
        src = open(os.path.join(ROOT, "server", "relay.py"),
                   encoding="utf-8").read()
        self.assertNotIn("parse_presence", src)


class GameServerSideTests(unittest.TestCase):
    """游戏服这一侧：存下来、分档、按状态翻转打日志，**不改判定**。"""

    @classmethod
    def setUpClass(cls):
        import gameserver
        cls.gs = gameserver

    def _conn(self):
        conn = self.gs.Conn.__new__(self.gs.Conn)
        conn.cid = 1
        conn.username = "tester"
        return conn

    def test_a_connection_that_never_reported_has_no_bucket(self):
        # ★ 「没收到证据」绝不能当成「他挂机」：老客户端、没中继、UDP 被防火墙
        #   挡掉，全都会走到这儿。
        self.assertIsNone(self.gs.presence_bucket(self._conn()))

    def test_the_background_case_wins_over_everything(self):
        conn = self._conn()
        conn.note_presence(0, 0, 0, False)          # 键盘鼠标刚动过，但在后台
        self.assertEqual("后台", self.gs.presence_bucket(conn))

    def test_nobody_at_the_machine_is_recognised(self):
        conn = self._conn()
        conn.note_presence(0, 0, 3_600_000, True)
        self.assertEqual("人不在", self.gs.presence_bucket(conn))

    def test_mouse_only_is_recognised(self):
        # 连点器的典型形状：鼠标一直在响，键盘从来没动过。
        conn = self._conn()
        conn.note_presence(udpsync.PRESENCE_NEVER, 100, 100, True)
        self.assertEqual("只有鼠标", self.gs.presence_bucket(conn))

    def test_someone_actually_playing_is_recognised(self):
        conn = self._conn()
        conn.note_presence(500, 500, 500, True)
        self.assertEqual("在玩", self.gs.presence_bucket(conn))

    def test_the_verdict_itself_is_untouched_in_this_version(self):
        # ★★ 三步走的第 2 步：**只记不判**。`conn_is_afk()` 里一个字都不该
        #    提到 presence —— 线要等线上真数据才定（§111 的老规矩）。
        src = open(os.path.join(ROOT, "server", "gameserver.py"),
                   encoding="utf-8").read()
        for name in ("def conn_is_afk", "def conn_afk_clock_expired"):
            body = src[src.index(name):]
            body = body[:body.index("\n\n\ndef ")]
            self.assertNotIn("presence", body,
                             "%s 里出现了 presence —— 这一版说好只记不判" % name)

    def test_the_log_is_deduped_by_state_not_by_count(self):
        # 一条连接一天能收上万发，按次数或时间窗去重要么刷屏要么漏掉翻转。
        conn = self._conn()
        lines = []
        real = self.gs.eventlog.debug
        self.gs.eventlog.debug = lambda msg: lines.append(msg)
        try:
            for _ in range(5):
                conn.note_presence(500, 500, 500, True)     # 同一档，报 5 次
            self.assertEqual(1, len(lines), "同一档重复报也在写日志")
            conn.note_presence(500, 500, 500, False)        # 翻到「后台」
            self.assertEqual(2, len(lines), "档位翻转了却没写日志")
            for _ in range(5):
                conn.note_presence(500, 500, 500, False)
            self.assertEqual(2, len(lines))
        finally:
            self.gs.eventlog.debug = real
        self.assertIn("后台", lines[-1])

    def test_the_age_text_tells_never_apart_from_zero(self):
        self.assertEqual("从没有过",
                         self.gs.presence_age_text(udpsync.PRESENCE_NEVER))
        self.assertEqual("0 秒前", self.gs.presence_age_text(0))
        self.assertEqual("?", self.gs.presence_age_text(None))


if __name__ == "__main__":
    unittest.main()
