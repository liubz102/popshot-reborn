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
import math
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

    def test_it_stores_ms_since_the_origin_per_path(self):
        conn = self._conn()
        conn.note_mover_phase(10000, 99999, [(296, 4000, 0), (131, 4000, 250)],
                              now=50.0)
        self.assertEqual({296: (50.0, 6000, 0, 4000), 131: (50.0, 6000, 250, 4000)},
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

    def test_it_logs_once_per_origin_not_once_per_second(self):
        conn = self._conn()
        conn.note_mover_phase(10000, 9000, [(296, 4000, 0)], now=1.0)
        conn.note_mover_phase(11000, 10000, [(296, 4000, 0)], now=2.0)
        conn.note_mover_phase(12000, 11000, [(296, 4000, 0)], now=3.0)
        self.assertEqual(1, len(conn.logged))
        self.assertIn("路径 296 起点后 6000 ms（t0=4000，偏移 0）", conn.logged[0])
        self.assertIn("游戏时钟 − 墙钟 = 1000 ms", conn.logged[0])
        # 最新那一发照样存了
        self.assertEqual((3.0, 8000, 0, 4000), conn.mover_phase[296])

    def test_the_start_of_the_battle_rebasing_the_origin_is_logged_and_used(self):
        """★ X_Mod §78：开打时 `GameContext::StartGame` 把起点整体重取一遍。

        2026-09-23 18:50 那局：载图时报「起点后 5 ms」（t0=121412）之后，服务端一整局都
        拿它外推，而客户端的鱼是从开打（8.46 s 后）才算起的。现在 hook 每一发现读 t0，
        重取之后的那一发要**被采用**，而且日志要再打一行 —— 会话 33 只打「这一局第一发」，
        起点被重取了在日志里看不出来。
        """
        conn = self._conn()
        conn.note_mover_phase(121417, 0, [(296, 121412, 0), (298, 121413, 0)], now=1.0)
        conn.note_mover_phase(129900, 0, [(296, 129880, 0), (298, 129880, 0)], now=9.5)
        conn.note_mover_phase(130900, 0, [(296, 129880, 0), (298, 129880, 0)], now=10.5)
        self.assertEqual(2, len(conn.logged))
        self.assertNotIn("起点变了", conn.logged[0])
        self.assertIn("起点变了", conn.logged[1])
        self.assertIn("路径 296 起点后 20 ms（t0=129880", conn.logged[1])
        self.assertEqual((10.5, 1020, 0, 129880), conn.mover_phase[296])

    def test_a_path_that_was_not_there_before_counts_as_a_new_origin(self):
        # 闯关中途换图：新图的路径句柄上一发里没有 —— 也是起点变了
        conn = self._conn()
        conn.note_mover_phase(1000, 0, [(296, 900, 0)], now=1.0)
        conn.note_mover_phase(9000, 0, [(131, 8000, 0)], now=9.0)
        self.assertEqual(2, len(conn.logged))
        self.assertIn("路径 131", conn.logged[1])

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

    def test_the_hosts_rebased_origin_drives_the_carp(self):
        """★ §78 端到端：真 `Conn` 先后收两发（载图时 / 开打重取后），坐在真 `Room` 的房主座位上，
        `_mover_clock` 要按**开打重取后**那一发算。按载图那一发外推会早一整段加载等待
        （18:50 那局：t0 从 121412 重取成 129880，差 8.47 s）。"""
        room = self.lobby.Room(0, None)
        conn = self.gs.Conn.__new__(self.gs.Conn)
        conn.account_name = "host"
        conn.online_debug = lambda text: None
        room.seats[0] = self.lobby.Seat(conn, "host")
        room.host_seat = 0
        room.quest = self.gs.RoomQuest()
        conn.note_mover_phase(121417, 0, [(296, 121412, 0)], now=91.5)     # 载图时
        conn.note_mover_phase(129900, 0, [(296, 129880, 0)], now=99.875)   # 开打重取后
        with self.bot._tick_clock(100.0):
            # 起点后 20 ms + 收到之后 125 ms；按载图那一发会是 5 + 8500 = 8505
            self.assertEqual(145, self.bot._mover_clock(room, self.terrain))

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


class BounceOffMoverTests(unittest.TestCase):
    """★ X_Mod §77：手雷撞上鲤鱼之后**往哪弹**，也得把鲤鱼算进去。

    用户 2026-09-23：「苹果雷在鱼上反弹跳起来，跳起来后模型突然消失，然后在鱼背上出现
    炸裂动画」。服务端判「撞上」带了时钟，量「朝向」的三个函数（`_block_facing` /
    `_terrain_facing` / `_nearest_solid`）却只查静态地形 ⇒ 鱼背上一格实心都采不到 ⇒
    速度只减半、方向不动 ⇒ 下一格又扎回鱼身 …… 服务端那颗贴在撞点上等到引信烧完，
    而客户端那颗早弹上天了（客户端 `0x473969` 是连地图对象一起算的）。
    """

    @classmethod
    def setUpClass(cls):
        import ballistics
        import bot
        import mapdata
        import weapondata
        cls.ballistics, cls.bot, cls.mapdata = ballistics, bot, mapdata
        cls.weapon = weapondata.get(1000020)          # 泰尔 2 号：苹果雷（AppleGrenade）

    def setUp(self):
        if self.weapon is None:
            self.skipTest("武器表里没有 1000020")
        self.clock = 0
        real = self.bot._mover_clock
        self.bot._mover_clock = lambda room, terrain: self.clock
        self.addCleanup(setattr, self.bot, "_mover_clock", real)

    @staticmethod
    def _blob(raw):
        import base64
        import zlib
        return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")

    def _flat_mover_terrain(self):
        """400×400 的空图，(200, 300) 上停着一块 100×20 的实心移动平台（路径只有一个点）。"""
        import struct
        width = height = 400
        mask = bytearray((100 * 20 + 3) // 4)
        for i in range(100 * 20):
            mask[i >> 2] |= 2 << ((i & 3) * 2)
        return self.mapdata.MapTerrain({
            "format": self.mapdata.FORMAT, "name": "Tiny", "version": 18,
            "width": width, "height": height,
            "cells": self._blob(bytes((width * height + 3) // 4)),
            "ground_counts": self._blob(struct.pack("<%dH" % width, *([0] * width))),
            "ground_ys": self._blob(b""),
            "movers": [{"handle": 1, "x": 0.0, "y": 0.0, "loop": 0,
                        "pts": [[200.0, 300.0, 1000, 0.0, 0.0, 1.0]],
                        "riders": [{"handle": 2, "w": 100, "h": 20, "sx": 1.0, "sy": 1.0,
                                    "x": 0.0, "y": 0.0, "t_off": 0, "rel": 0,
                                    "mask": self._blob(bytes(mask))}]}],
        })

    def _shell(self, angle, power, x0, y0, speed=None):
        b = self.ballistics
        if speed is None:
            speed = b.speed_for_power(self.weapon, power)
        shot = b.Shot(angle, power, speed, 0.0, b.gravity_per_tick(self.weapon))
        return self.bot.Shell(1, 0, self.weapon, 2, x0, y0, shot, 0.0, 45)

    def test_a_grenade_dropping_onto_a_mover_bounces_up(self):
        terrain = self._flat_mover_terrain()
        self.assertEqual(0, terrain.cell(200, 295))                # 静态地形里什么都没有
        self.assertTrue(terrain.blocks_bullet(200, 295, 0))       # 带上时钟才有平台
        shell = self._shell(math.pi / 2, 0.0, 200.0, 200.0, speed=10.0)
        for _ in range(20):
            self.assertIsNone(self.bot._shell_step(None, shell, terrain, []))
            if shell.bounced:
                break
        self.assertTrue(shell.bounced, "一直没撞上平台")
        self.assertLess(shell.vy, 0.0, "撞上平台之后还在往下走 —— 朝向又没把平台算进去")
        self.assertLess(shell.y, 290.0)                            # 停在平台上沿之上
        # 下一格真的离开了，而不是贴在撞点上
        y = shell.y
        self.assertIsNone(self.bot._shell_step(None, shell, terrain, []))
        self.assertLess(shell.y, y)

    def test_the_facing_vote_counts_the_mover_only_with_a_clock(self):
        terrain = self._flat_mover_terrain()
        self.assertIsNone(self.bot._terrain_facing(terrain, 200, 291))
        sx, sy = self.bot._terrain_facing(terrain, 200, 291, 0)
        self.assertEqual(0.0, sx)
        self.assertGreater(sy, 0.0)                                # 指向实心那一侧 = 下面

    def test_the_20260923_carp_bounce_matches_the_client_log(self):
        """句柄 200084（2026-09-23 云桥，`rpFire` 原字节：角度 -0.25969645380973816、力度 42）。

        鲤鱼放在**客户端那一刻看到的相位**上（开局后 41728 ms 出膛第一格、每格 +32，拿客户端逐帧
        日志离线拟合出来的），撞鱼那一格的落点和弹开速度要和客户端 `PROJ.` 第 34 帧一致：
        (906.00, 829.00)、(7.68, -15.14)。修之前这里算出来的是 (11.02, 12.91) —— 还朝下，
        于是贴在鱼背上等引信。
        ⚠ 服务端那一局自己的时钟（本机模式没有相位上报，按开局估计）比这个晚 32 ms：
          鲤鱼位置取整到像素，差这么一点撞点就挪一格、弹出去的角度就不一样（§77）。
          这条用例钉的是「朝向算对了」，相位准不准是另一件事。
        """
        terrain = self.mapdata.load("Festival02")
        if terrain is None:
            self.skipTest("没有 Festival02 的地形产物")
        shell = self._shell(-0.25969645380973816, 42.0, 199.0, 507.0)
        while not shell.bounced and shell.ticks < 45:
            self.clock = 41728 + 32 * shell.ticks
            self.assertIsNone(self.bot._shell_step(None, shell, terrain, []))
        self.assertEqual(33, shell.ticks)
        self.assertEqual((906.0, 829.0), (shell.x, shell.y))
        self.assertAlmostEqual(7.68, shell.vx, places=2)
        self.assertAlmostEqual(-15.14, shell.vy, places=2)


class HookSourceTests(unittest.TestCase):
    """`hook/bshook.c` 那一侧（X_Mod §78）：周期那一发必须能把起点纠正回来。

    会话 30 的两个毛病，任何一个都让「后续同步」形同虚设：
    ① 每秒那一发报的是**记表时抄下的** t0 —— 开打时 StartGame 重取了起点也照报旧的；
    ② 发包前要求「当前 Stage 和记表时是同一个」—— stage 6（LoadingStage）→ 7（GameStage）
       本来就是两个对象，一开打整局的同步包都被拦了。
    """

    BSHOOK = os.path.join(ROOT, "hook", "bshook.c")

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(cls.BSHOOK):
            raise unittest.SkipTest("不在源码仓库里（缺 hook/bshook.c）")
        with open(cls.BSHOOK, encoding="utf-8") as fp:
            cls.src = fp.read()
        body = cls.src[cls.src.index("static void sync_send_mover_phase(void)"):]
        cls.send_body = body[:body.index("\n}\n")]

    def test_every_report_reads_the_origin_off_the_object(self):
        self.assertIn("MOVER_OBJ_T0_OFF", self.send_body,
                      "发包时没有从对象身上现读 t0 —— 起点变了周期同步也纠正不了")

    def test_a_new_stage_does_not_silence_the_reports(self):
        import re
        self.assertNotIn("g_mover_gc", self.src)
        self.assertEqual([], re.findall(r"\bstage\s*[!=]=", self.send_body),
                         "发包门槛又拿当前 Stage 去比了 —— 换 stage 就是换对象")

    def test_both_origin_writers_feed_the_same_table(self):
        # ① 载图 LinkPath、② 开打重取：同一个记表函数，只差站点号
        for detour, site in (("mover_link_detour", "MOVER_SITE_LINK"),
                             ("mover_start_detour", "MOVER_SITE_START")):
            body = self.src[self.src.index("static __declspec(naked) void %s(void)" % detour):]
            body = body[:body.index("\n}\n")]
            self.assertIn("push %s" % site, body)
            self.assertIn("call mover_note_link", body)


if __name__ == "__main__":
    unittest.main()
