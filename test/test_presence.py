#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在场证据（`udpsync.MSG_PRESENCE`）—— 客户端报、中继转、游戏服记。

出处 bug调查/25（§62 / D53）：328800963 连续 18 小时显示「游戏中·任务」。
不是判据写错了，是**单人任务房里服务端是瞎的** —— 房里只有他一个人，客户端
就不发 `0x040e`，挂机判定的「键盘」那条整个不存在，只剩「打中 / 捡到 / 得分」；
而他开着连点器，分数每 1.5 秒涨一次，证据比真人还多。

⇒ 让 `bshook` 把服务端**结构上看不见**的四件事报过来：键盘 / 鼠标 / 这台机器
整机空闲（`GetLastInputInfo`）/ 游戏窗口在不在前台。

★★ **2026-09-21 起这条判据真的接上了**（三步走的第 3 步，用户拍板三档全判）。
所以本模块分两半：前面几个类钉「路通不通、数对不对、日志会不会刷屏」，
`VerdictTests` 钉「谁会被判成挂机」。
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
        conn.account_name = "tester"
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

    def test_the_log_only_touches_attributes_a_bare_conn_has(self):
        """★ 2026-09-20 的真事故：日志写的是 `self.username`，而 `Conn` 上
        没有这个名字 —— `%` 元组在 `eventlog.debug()` **之前**求值，于是每次
        档位翻转抛一次 `AttributeError`，被 `udpsync._on_presence` 的
        `except Exception` 吞掉，「在场证据」那行一行都没写出来过。
        当时单测没发现，正是因为夹具手写了 `conn.username`。

        ⇒ 这条**故意不设 `account_name`**，逼日志只能用裸 `Conn` 真有的属性。
        """
        conn = self.gs.Conn.__new__(self.gs.Conn)   # ★ 一格都不给，全吃类级默认
        lines = []
        real = self.gs.eventlog.debug
        self.gs.eventlog.debug = lambda msg: lines.append(msg)
        try:
            conn.note_presence(500, 500, 500, True)
        finally:
            self.gs.eventlog.debug = real
        self.assertEqual(1, len(lines), "档位翻转了却没写日志")
        self.assertIn("'?'", lines[0], "没名字时该写成 ?")
        # 赋值必须排在日志之前，否则日志一抛，状态就没存下来。
        self.assertEqual("在玩", conn.presence_logged)
        self.assertIsNotNone(conn.presence_since)

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


#: 四档各自的一组 `(kb_ms, mouse_ms, sys_ms, foreground)`。
SAMPLES = {
    "后台": (0, 0, 0, False),                                # 键鼠刚动过也没用
    "人不在": (0, 0, 3_600_000, True),                       # 整机一小时没人碰
    "只有鼠标": (udpsync.PRESENCE_NEVER, 100, 100, True),    # 连点器的形状
    "在玩": (500, 500, 500, True),
}


class VerdictTests(unittest.TestCase):
    """★★ 三步走的第 3 步（用户 2026-09-21）：在场证据**真的参与判定**。

    钉的是行为，不是源码文本 —— 上一版那条「`conn_is_afk()` 里不许出现
    presence」的断言是按源码字符串写的，现在它的前提被推翻了。
    """

    @classmethod
    def setUpClass(cls):
        import gameserver
        cls.gs = gameserver

    def _conn(self):
        conn = self.gs.Conn.__new__(self.gs.Conn)
        conn.account_name = "tester"
        return conn

    def _report(self, conn, bucket, at):
        kb, mouse, sysidle, fg = SAMPLES[bucket]
        conn.note_presence(kb, mouse, sysidle, fg, now=at)
        self.assertEqual(bucket, conn.presence_logged)      # 夹具自检

    # ------------------------------------------------------------ 三档都要判
    def test_each_afk_bucket_becomes_a_verdict_once_it_holds(self):
        hold = self.gs.PRESENCE_AFK_AFTER_S
        for bucket in self.gs.PRESENCE_AFK_BUCKETS:
            with self.subTest(bucket):
                conn = self._conn()
                self._report(conn, bucket, 100.0)
                self.assertFalse(self.gs.conn_is_afk(conn, now=100.0 + hold),
                                 "线上就判了 —— 阈值是「超过」不是「到达」")
                self.assertTrue(
                    self.gs.conn_is_afk(conn, now=100.0 + hold + 0.001))

    def test_actually_playing_is_never_a_verdict(self):
        conn = self._conn()
        self._report(conn, "在玩", 100.0)
        self.assertFalse(self.gs.conn_is_afk(conn, now=100_000.0))

    def test_no_evidence_at_all_is_never_a_verdict(self):
        """★ 老客户端 / 没中继 / UDP 被挡 / `BSHOOK_NO_PRESENCE=1` 都走这儿。

        「收不到」绝不能当成「他挂机」—— bot 也是靠这一条不受影响的。
        """
        conn = self._conn()
        self.assertIsNone(conn.presence_at)
        self.assertIsNone(self.gs.conn_presence_afk(conn, now=100_000.0))
        self.assertFalse(self.gs.conn_is_afk(conn, now=100_000.0))

    # -------------------------------------------------- 起点是「翻转」这个事件
    def test_the_hold_starts_at_the_flip_not_at_the_last_packet(self):
        # 同一档重复报**不动**起点，所以丢包既不重置也不加速。
        conn = self._conn()
        for at in (100.0, 105.0, 110.0, 115.0):
            self._report(conn, "后台", at)
            self.assertEqual(100.0, conn.presence_since)
        hold = self.gs.PRESENCE_AFK_AFTER_S
        self.assertTrue(self.gs.conn_is_afk(conn, now=100.0 + hold + 0.001))

    def test_coming_back_cancels_the_verdict_at_once(self):
        """★ 撤销是**事件驱动**的：下一发上报一到当场生效，不等定时器。"""
        conn = self._conn()
        hold = self.gs.PRESENCE_AFK_AFTER_S
        self._report(conn, "后台", 100.0)
        self.assertTrue(self.gs.conn_is_afk(conn, now=100.0 + hold + 1.0))
        self._report(conn, "在玩", 100.0 + hold + 2.0)
        self.assertFalse(self.gs.conn_is_afk(conn, now=100.0 + hold + 2.0))
        # 重新锚住了 ⇒ 再翻回「后台」也要重新攒满
        self._report(conn, "后台", 200.0)
        self.assertFalse(self.gs.conn_is_afk(conn, now=200.0 + hold))
        self.assertTrue(self.gs.conn_is_afk(conn, now=200.0 + hold + 0.001))

    # ------------------------------------------------------------ 证据会过期
    def test_stale_evidence_falls_back_to_no_information(self):
        """★ 断流退回「没有证据」，**不冻在最后那一档**。

        玩家正常退出、hook 被杀、防火墙把旁路拦了 —— 冻结的话管理页会永远
        挂着「挂机中」而且再也不会自己消。「收不到」证明不了「他在挂机」。
        """
        conn = self._conn()
        hold = self.gs.PRESENCE_AFK_AFTER_S
        stale = self.gs.PRESENCE_STALE_AFTER_S
        self._report(conn, "后台", 100.0)
        self.assertTrue(self.gs.conn_is_afk(conn, now=100.0 + stale))
        self.assertIsNone(self.gs.conn_presence_afk(conn, now=100.0 + stale + 0.001))
        self.assertFalse(self.gs.conn_is_afk(conn, now=100.0 + stale + 0.001))
        # 过期只在读的时候判，字段一格都没清（清了就是定时器驱动的状态变更）。
        self.assertEqual("后台", conn.presence_logged)
        self.assertEqual(100.0, conn.presence_since)
        del hold

    def test_a_resumed_stream_re_arms_the_hold(self):
        # 断流那一段我们什么都不知道，不能拿断流之前的起点接着数。
        conn = self._conn()
        hold = self.gs.PRESENCE_AFK_AFTER_S
        stale = self.gs.PRESENCE_STALE_AFTER_S
        self._report(conn, "后台", 100.0)
        back = 100.0 + stale + 60.0
        self._report(conn, "后台", back)                    # 同一档，但断过流
        self.assertEqual(back, conn.presence_since, "断流恢复没有重新锚")
        self.assertFalse(self.gs.conn_is_afk(conn, now=back + hold))
        self.assertTrue(self.gs.conn_is_afk(conn, now=back + hold + 0.001))

    # ------------------------------------------------------------ 只补不削
    def test_presence_can_only_add_afk_never_take_it_away(self):
        """D53 的「补客户端证据，不去削弱服务端那条」，行为化。

        服务端那两条钟问的是「他在玩**这一局**吗」；在场证据只能证明
        「他人在机器前按过键」。拿后者推翻前者就是「削弱」。
        """
        conn = self._conn()
        conn.last_input_at = 0.0                            # 钟早就到期了
        self.assertTrue(self.gs.conn_is_afk(conn, now=3600.0))
        for at in range(100, 3600, 5):                      # 一小时的「在玩」
            self._report(conn, "在玩", float(at))
        self.assertTrue(self.gs.conn_is_afk(conn, now=3600.0),
                        "在场证据把服务端判出来的挂机洗白了")

    def test_lying_down_freezes_the_presence_verdict_too(self):
        """躺着那一段整个判定是冻住的（用户 2026-09-14 第四 / 第六轮）。

        死了等复活、命用完整局观战的人本来就按不了键 —— 但他**可能**还好好
        坐在前台看着，所以 presence 也不许把这个闩捅开，两个方向都不许。
        """
        hold = self.gs.PRESENCE_AFK_AFTER_S
        # ① 证据说挂机，但倒下那一刻是「在玩」 ⇒ 维持「在玩」
        conn = self._conn()
        self._report(conn, "后台", 100.0)
        conn.dead_since = 100.0
        conn.afk_when_down = False
        self.assertFalse(self.gs.conn_is_afk(conn, now=100.0 + hold + 100.0))
        # ② 证据说在玩，但倒下那一刻是「挂机」 ⇒ 维持「挂机」
        conn = self._conn()
        self._report(conn, "在玩", 100.0)
        conn.dead_since = 100.0
        conn.afk_when_down = True
        self.assertTrue(self.gs.conn_is_afk(conn, now=100.0 + hold + 100.0))

    # ------------------------------------------------------------ 阈值的来历
    def test_the_thresholds_are_derived_not_invented(self):
        gs = self.gs
        self.assertEqual(gs.AFK_AFTER_S, gs.PRESENCE_AFK_AFTER_S,
                         "又另立了一条线 —— 说好沿用调过六轮的那个数")
        self.assertGreater(gs.PRESENCE_STALE_AFTER_S, gs.PRESENCE_REPORT_S,
                           "过期窗口比上报周期还短 —— 丢一发就当断流了")
        # ★ 「别重复计时」的护栏：保持窗口不该长到能和档位自带的 60 秒回溯
        #   接力着看（那会让人以为要等 80 秒以上）。
        self.assertLess(gs.PRESENCE_AFK_AFTER_S, gs.PRESENCE_IDLE_MS / 1000.0)

    def test_the_afk_buckets_are_sliced_from_the_one_table(self):
        # 不许手抄第二份清单：`在玩` 之外的全算，顺序表就是唯一那份定义。
        self.assertEqual(set(self.gs.PRESENCE_BUCKETS[:-1]),
                         set(self.gs.PRESENCE_AFK_BUCKETS))
        self.assertNotIn("在玩", self.gs.PRESENCE_AFK_BUCKETS)


if __name__ == "__main__":
    unittest.main()
