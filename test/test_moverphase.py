#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""移动平台相位（`udpsync.MSG_MOVER_PHASE`）—— 客户端报、中继转、游戏服记、bot 用（X_Mod §74 / D55）。

出处：bot 往「云桥」的鲤鱼上扔手雷穿过去。两件事叠在一起：

① 会话 29 的 `_mover_clock()` 在 `room` 上找 `started_at`，字段却在 `room.quest` 上 ⇒
   永远 `None`，整套移动平台判定从来没启用过（`RegressionTests` 钉着）；
② 相位原点是每台客户端**自己**载完图那一刻，协议里没有同步包，`Timer()` 也不是墙钟
   ⇒ 让 `bshook` 报事实，服务端按「房主报的 → 开局估计」取（`BotClockTests`；
   房主掉线复用大厅的转移逻辑）。

★ 纯标准库 + 服务端模块，两套运行时都跑。
"""
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import udpsync  # noqa: E402


class WireFormatTests(unittest.TestCase):

    def test_a_round_trip_keeps_every_field(self):
        entries = [(296, 120000, 0), (131, 120000, 5000)]
        blob = udpsync.build_mover_phase(123456, 987654, entries)
        self.assertEqual((123456, 987654, entries), udpsync.parse_mover_phase(blob))
        self.assertEqual((udpsync.MSG_MOVER_PHASE, 2), udpsync.parse_header(blob))

    def test_t0_is_an_unsigned_32_bit_clock(self):
        # 客户端的 Timer() 是 DWORD；靠近回绕点也得原样过线，偏移可以是负的
        blob = udpsync.build_mover_phase(0x10, 0, [(1, 0xFFFFFF00, -250)])
        self.assertEqual([(1, 0xFFFFFF00, -250)], udpsync.parse_mover_phase(blob)[2])

    def test_it_does_not_collide_with_the_other_message_kinds(self):
        kinds = {udpsync.MSG_HELLO, udpsync.MSG_HELLO_ACK, udpsync.MSG_DATA,
                 udpsync.MSG_PING, udpsync.MSG_PONG, udpsync.MSG_PRESENCE}
        self.assertNotIn(udpsync.MSG_MOVER_PHASE, kinds)

    def test_a_longer_datagram_still_parses(self):
        # 以后加字段时老服务端不能因为「长了」就整个丢掉（同在场证据）
        blob = udpsync.build_mover_phase(1, 2, [(3, 4, 5)]) + b"\x99" * 8
        self.assertEqual((1, 2, [(3, 4, 5)]), udpsync.parse_mover_phase(blob))

    def test_a_short_one_is_refused(self):
        blob = udpsync.build_mover_phase(1, 2, [(3, 4, 5)])[:-4]
        with self.assertRaises(udpsync.ProtocolError):
            udpsync.parse_mover_phase(blob)

    def test_other_kinds_are_refused(self):
        with self.assertRaises(udpsync.ProtocolError):
            udpsync.parse_mover_phase(udpsync.build_presence(1, 2, 3, True))

    def test_the_table_cap_matches_the_hook(self):
        # hook 那张表 32 条，多出来的在发送侧就截掉；线上永远不会超过
        blob = udpsync.build_mover_phase(0, 0, [(i, 0, 0) for i in range(40)])
        self.assertEqual(udpsync.MOVER_MAX_ENTRIES,
                         len(udpsync.parse_mover_phase(blob)[2]))

    def test_the_wire_version_did_not_have_to_change(self):
        # 新增一个 kind 不改线格式版本：老服务端在 `_handle` 里认不出，安静丢掉
        self.assertEqual(b"PSU\x01", udpsync.MAGIC)


class _FakeConn:
    def __init__(self):
        self.seen = []

    def note_mover_phase(self, game_now, wall_now, entries):
        self.seen.append((game_now, wall_now, entries))


class HubDispatchTests(unittest.TestCase):
    """`UdpSyncServer` 收到这一发之后交给谁 —— 和在场证据同一套。"""

    def setUp(self):
        self.hub = udpsync.UdpSyncServer()
        self.conn = _FakeConn()
        self.addr = ("127.0.0.1", 40002)
        self.hub._by_addr[self.addr] = udpsync.Endpoint(self.conn, self.addr, 0.0)

    def test_it_reaches_the_connection_that_said_hello(self):
        self.hub._handle(udpsync.build_mover_phase(10, 20, [(296, 4, 0)]),
                         self.addr, 1.0)
        self.assertEqual([(10, 20, [(296, 4, 0)])], self.conn.seen)

    def test_an_unknown_sender_is_dropped_not_guessed(self):
        # HELLO 之前认不出是谁。绝不能「猜一个最近的连接」—— 这个端口在公网上。
        before = self.hub.unknown_in
        self.hub._handle(udpsync.build_mover_phase(1, 2, [(3, 4, 5)]),
                         ("127.0.0.1", 49998), 1.0)
        self.assertEqual([], self.conn.seen)
        self.assertEqual(before + 1, self.hub.unknown_in)

    def test_a_connection_without_the_method_is_not_a_crash(self):
        # 控制通道造的假连接、老版本的 Conn 都没有这个方法。
        self.hub._by_addr[self.addr] = udpsync.Endpoint(object(), self.addr, 0.0)
        self.hub._handle(udpsync.build_mover_phase(1, 2, [(3, 4, 5)]), self.addr, 1.0)

    def test_a_raising_connection_does_not_kill_the_receive_loop(self):
        class Boom:
            def note_mover_phase(self, *a):
                raise RuntimeError("boom")
        self.hub._by_addr[self.addr] = udpsync.Endpoint(Boom(), self.addr, 0.0)
        self.hub._handle(udpsync.build_mover_phase(1, 2, [(3, 4, 5)]), self.addr, 1.0)

    def test_it_counts_as_a_sign_of_life_for_the_endpoint(self):
        self.hub._handle(udpsync.build_mover_phase(1, 2, [(3, 4, 5)]), self.addr, 9.0)
        self.assertEqual(9.0, self.hub._by_addr[self.addr].last_seen)


class RelayForwardingTests(unittest.TestCase):
    """中继必须**原样转发**，不许自作主张（改取值顺序不该要求重发中继）。"""

    def _relay_source(self):
        with open(os.path.join(ROOT, "server", "relay.py"), encoding="utf-8") as fp:
            return fp.read()

    def test_the_relay_forwards_it_verbatim(self):
        src = self._relay_source()
        body = src[src.index("def _on_hook_datagram"):]
        body = body[:body.index("\n    def ")]
        self.assertIn("udpsync.MSG_MOVER_PHASE", body, "中继根本没认这个 kind")
        self.assertIn("self._to_remote(data)", body,
                      "中继没有原样转发（判定在游戏服，中继不该解它）")

    def test_the_relay_does_not_parse_the_payload(self):
        self.assertNotIn("parse_mover_phase", self._relay_source())


class GameServerSideTests(unittest.TestCase):
    """游戏服这一侧：只存事实、按「这一局第一次」打一行、发 `0x0400` 清空。"""

    @classmethod
    def setUpClass(cls):
        import gameserver
        cls.gs = gameserver

    def _conn(self):
        conn = self.gs.Conn.__new__(self.gs.Conn)
        conn.account_name = "tester"
        conn.logged = []
        conn.online_debug = conn.logged.append      # 只数这一行打了几次
        return conn

    def test_a_connection_that_never_reported_has_nothing(self):
        # 老客户端 / 没中继 / UDP 被挡：一律「没这条信息」，bot 退回开局估计
        conn = self._conn()
        self.assertIsNone(conn.mover_phase)
        self.assertIsNone(conn.mover_phase_at)

    def test_it_stores_ms_since_link_per_path(self):
        conn = self._conn()
        conn.note_mover_phase(10000, 99999, [(296, 4000, 0), (131, 4000, 250)],
                              now=50.0)
        self.assertEqual({296: (50.0, 6000, 0), 131: (50.0, 6000, 250)},
                         conn.mover_phase)
        self.assertEqual(50.0, conn.mover_phase_at)

    def test_a_wrapped_clock_still_comes_out_signed(self):
        # Timer() 是 DWORD：回绕点两边差 356 ms 就是 356，不是四十亿
        conn = self._conn()
        conn.note_mover_phase(100, 0, [(1, 0xFFFFFF00, 0)], now=1.0)
        self.assertEqual(356, conn.mover_phase[1][1])
        # t0 在 game_now 之后（载图时那一格还不是同一个时钟，§74 留的口子）：
        # 按有符号读成负数，和客户端 `Path::Eval` 的有符号取模一致
        conn.note_mover_phase(100, 0, [(1, 400, 0)], now=2.0)
        self.assertEqual(-300, conn.mover_phase[1][1])

    def test_an_empty_report_changes_nothing(self):
        conn = self._conn()
        conn.note_mover_phase(1, 2, [], now=1.0)
        self.assertIsNone(conn.mover_phase)
        self.assertEqual([], conn.logged)

    def test_it_logs_once_per_game_not_once_per_second(self):
        conn = self._conn()
        conn.note_mover_phase(10000, 9000, [(296, 4000, 0)], now=1.0)
        conn.note_mover_phase(11000, 10000, [(296, 4000, 0)], now=2.0)
        conn.note_mover_phase(12000, 11000, [(296, 4000, 0)], now=3.0)
        self.assertEqual(1, len(conn.logged))
        self.assertIn("路径 296 载图后 6000 ms", conn.logged[0])
        self.assertIn("游戏时钟 − 墙钟 = 1000 ms", conn.logged[0])
        # 最新那一发照样存了
        self.assertEqual((3.0, 8000, 0), conn.mover_phase[296])

    def test_preparing_the_next_game_forgets_the_old_phase(self):
        """发出 `0x0400`（切 stage 6 开始载图）= 上一局的相位作废；下一局第一发重新打日志。"""
        conn = self._conn()
        conn.room_generation = lambda kind=None: 1     # 裸 Conn 没有大厅
        conn.note_mover_phase(10000, 9000, [(296, 4000, 0)], now=1.0)
        conn.note_epoch_from_frame(
            self.gs.build_game(self.gs.OP_PREPARE_GAME, b"\x00" * 4))
        self.assertIsNone(conn.mover_phase)
        self.assertIsNone(conn.mover_phase_at)
        conn.note_mover_phase(20000, 9000, [(296, 15000, 0)], now=5.0)
        self.assertEqual(2, len(conn.logged))
        # 别的换代包（`0x0403` 结算回房间）不清 —— 清不清都没人在打，留着给日志看
        conn.note_epoch_from_frame(self.gs.build_game(self.gs.OP_LOADING_DONE, b""))
        self.assertIsNotNone(conn.mover_phase)


class BotClockTests(unittest.TestCase):
    """`bot._mover_clock()` 的取值顺序（D55：房主 → 开局估计 → None）。全部用假对象，`_now()` 钉在 100.0。"""

    @classmethod
    def setUpClass(cls):
        import bot
        cls.bot = bot

    @staticmethod
    def _terrain(*handles):
        return types.SimpleNamespace(
            movers=tuple(types.SimpleNamespace(handle=h) for h in handles))

    @staticmethod
    def _human(phase):
        return types.SimpleNamespace(conn=types.SimpleNamespace(mover_phase=phase))

    @staticmethod
    def _bot():
        return types.SimpleNamespace(conn=object())      # BotConn 没有 mover_phase

    @staticmethod
    def _room(*seats, host_seat=0, started_at=None):
        quest = (None if started_at is None
                 else types.SimpleNamespace(started_at=started_at))
        return types.SimpleNamespace(seats=list(seats), host_seat=host_seat,
                                     quest=quest)

    def test_no_movers_means_no_phase_at_all(self):
        room = self._room(self._human({296: (90.0, 1000, 0)}), started_at=97.5)
        with self.bot._tick_clock(100.0):
            self.assertIsNone(self.bot._mover_clock(room, self._terrain()))
            self.assertIsNone(self.bot._mover_clock(room, None))

    def test_the_hosts_report_is_the_one_that_counts(self):
        # 座位 0 房主报的：载图后 500 ms，收到时 95.0 ⇒ 现在 100.0 是 5500；
        # 座位 2 也报了（11000），但他不是房主，不算
        room = self._room(self._human({296: (95.0, 500, 0)}), self._bot(),
                          self._human({296: (90.0, 1000, 0)}), started_at=97.5)
        with self.bot._tick_clock(100.0):
            self.assertEqual(5500, self.bot._mover_clock(room, self._terrain(296)))

    def test_a_new_host_takes_over_the_moment_the_seat_changes(self):
        """房主掉线 → `lobby.leave` 把 `host_seat` 转给下一位 —— 这里每次现读，不自己记人。"""
        room = self._room(self._human({296: (95.0, 500, 0)}), self._bot(),
                          self._human({296: (90.0, 1000, 0)}), started_at=97.5)
        terrain = self._terrain(296)
        with self.bot._tick_clock(100.0):
            self.assertEqual(5500, self.bot._mover_clock(room, terrain))
            room.seats[0] = None                 # 房主走了
            room.host_seat = 2                   # 大厅转给还在的最小真人座位
            self.assertEqual(11000, self.bot._mover_clock(room, terrain))

    def test_a_host_report_about_another_map_is_skipped(self):
        room = self._room(self._human({999: (95.0, 500, 0)}), None,
                          self._human({296: (90.0, 1000, 0)}), started_at=97.5)
        with self.bot._tick_clock(100.0):
            # 房主报的是别的图 ⇒ 不拿别人的凑，退回开局估计
            self.assertEqual(2500, self.bot._mover_clock(room, self._terrain(296)))

    def test_without_a_host_report_it_falls_back_to_the_quest_start(self):
        # 房主是老客户端（没报），别人报了也不算 —— 一颗弹只认一个相位
        room = self._room(self._human(None), self._human({296: (90.0, 1000, 0)}),
                          started_at=97.5)
        with self.bot._tick_clock(100.0):
            self.assertEqual(2500, self.bot._mover_clock(room, self._terrain(296)))
        # 房主座位号越界 / 座位是 bot：同样退回
        odd = self._room(self._bot(), host_seat=5, started_at=97.5)
        with self.bot._tick_clock(100.0):
            self.assertEqual(2500, self.bot._mover_clock(odd, self._terrain(296)))

    def test_without_a_quest_it_stays_out_of_the_way(self):
        room = self._room(self._human(None))
        with self.bot._tick_clock(100.0):
            self.assertIsNone(self.bot._mover_clock(room, self._terrain(296)))


class RegressionTests(unittest.TestCase):
    """★ 会话 29 漏掉的那条：真的 `lobby.Room` + `gameserver.RoomQuest`，`_mover_clock` 必须给数。"""

    @classmethod
    def setUpClass(cls):
        import bot
        import gameserver
        import lobby
        import mapdata
        cls.bot, cls.gs, cls.lobby = bot, gameserver, lobby
        cls.terrain = mapdata.load("Festival02")

    def setUp(self):
        if self.terrain is None:
            self.skipTest("没有 Festival02 的地形产物")

    def test_a_real_room_with_a_quest_yields_a_clock(self):
        room = self.lobby.Room(0, None)
        self.assertIsNone(self.bot._mover_clock(room, self.terrain))      # 还没开局
        room.quest = self.gs.RoomQuest()
        got = self.bot._mover_clock(room, self.terrain)
        self.assertIsInstance(got, int)
        self.assertGreaterEqual(got, 0)

    def test_the_carp_actually_stops_a_shell_when_a_clock_is_given(self):
        """竖着穿过 t=0 时鲤鱼身子那一片（画布 rows 321..418）：给时钟撞得上，不给撞不上。"""
        mv = self.terrain.movers[0]
        rider = mv.riders[0]
        cx, cy = mv.rider_center(rider, 0)
        x = int(cx)
        y0 = int(cy - rider.sh / 2.0) + 300          # 链子那一段，掩码是空的
        y1 = y0 + 100                                # 已经进到鱼身里
        for y in range(y0, y1 + 1):
            self.assertEqual(0, self.terrain.cell(x, y), "静态格 (%d,%d) 不是空气" % (x, y))
        contact = self.bot._terrain_contact
        self.assertEqual((None, None), contact(self.terrain, x, y0, x, y1, 0.0))
        self.assertEqual((None, None), contact(self.terrain, x, y0, x, y1, 0.0, t_ms=None))
        hit, before = contact(self.terrain, x, y0, x, y1, 0.0, t_ms=0)
        self.assertIsNotNone(hit)
        self.assertLess(before, hit)
        # 半个周期后鱼飘到另一头（x≈982），同一条线就空了
        self.assertEqual((None, None), contact(self.terrain, x, y0, x, y1, 0.0, t_ms=5000))


if __name__ == "__main__":
    unittest.main()
