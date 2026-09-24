#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逻辑帧时钟（`udpsync.MSG_TICK_CLOCK`）—— hook 每个逻辑帧报、中继转、游戏服记、bot 按帧取（X_Mod §81 / D60）。

出处：模型对了以后（§79 / §80），鲤鱼上的反弹还剩时钟这一半 —— 服务端按「每秒一发的相位 +
墙钟外推」算的鱼，比房主客户端推弹体那一格**实际**撞到的鱼平均早 12 ms、σ 7 ms，而鱼背上
1 px 的窗口只有 ±6 ms。客户端那一格撞到的鱼是两件站在旁边就看得到的事实：

* 逻辑帧是固定 32 ms 的网格（`Stage::Update` 0x42b4c3，`[Stage+0xd4]` 帧号），移动平台的碰撞
  位置只在渲染时按 `Timer()`（`[Stage+0xe0]`）刷新 ⇒ 逻辑帧 m 里撞到的鱼 = 该帧的 Timer − t0；
* 收到的 `rpFire` 在某个逻辑帧的网络泵里建弹体，**同一帧**里推第 1 格。

⇒ hook 在 `GameContext` 逻辑帧入口（0x4904cc）每帧报「帧号 / Timer / 这一帧出膛的远端弹体」，
服务端第 k 格按「出膛帧 + k − 1」那一帧的 Timer 算。

★ 纯标准库 + 服务端模块，两套运行时都跑。
"""
import os
import re
import struct
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import udpsync  # noqa: E402

BSHOOK = os.path.join(ROOT, "hook", "bshook.c")
IMG = os.path.join(ROOT, "re", "BigShot_22524.img")


class WireFormatTests(unittest.TestCase):

    def test_a_round_trip_keeps_every_field(self):
        blob = udpsync.build_tick_clock(1234, 567890, [200002, 200003])
        self.assertEqual((1234, 567890, [200002, 200003]), udpsync.parse_tick_clock(blob))
        self.assertEqual((udpsync.MSG_TICK_CLOCK, 2), udpsync.parse_header(blob))

    def test_a_tick_without_births_is_the_common_case(self):
        blob = udpsync.build_tick_clock(7, 0xFFFFFFF0)
        self.assertEqual((7, 0xFFFFFFF0, []), udpsync.parse_tick_clock(blob))
        self.assertEqual(8 + 8, len(blob))

    def test_short_is_rejected_long_is_accepted(self):
        blob = udpsync.build_tick_clock(1, 2, [3])
        with self.assertRaises(udpsync.ProtocolError):
            udpsync.parse_tick_clock(blob[:-1])
        # 以后往后加字段，老服务端照收
        self.assertEqual((1, 2, [3]), udpsync.parse_tick_clock(blob + b"\x00\x00"))

    def test_the_kind_does_not_collide(self):
        kinds = [udpsync.MSG_HELLO, udpsync.MSG_DATA, udpsync.MSG_PRESENCE,
                 udpsync.MSG_MOVER_PHASE, udpsync.MSG_PING, udpsync.MSG_PONG,
                 udpsync.MSG_HELLO_ACK]
        self.assertNotIn(udpsync.MSG_TICK_CLOCK, kinds)


class _FakeConn(object):
    def __init__(self):
        self.seen = []

    def note_tick_clock(self, tick, timer, births):
        self.seen.append((tick, timer, births))


class HubDispatchTests(unittest.TestCase):
    """`UdpSyncServer` 收到之后交给谁 —— 和移动平台相位同一套：HELLO 之前一律丢。"""

    def setUp(self):
        self.hub = udpsync.UdpSyncServer()
        self.conn = _FakeConn()
        self.addr = ("127.0.0.1", 40003)
        self.hub._by_addr[self.addr] = udpsync.Endpoint(self.conn, self.addr, 0.0)

    def test_it_reaches_the_connection_that_said_hello(self):
        self.hub._handle(udpsync.build_tick_clock(10, 20, [200002]), self.addr, 1.0)
        self.assertEqual([(10, 20, [200002])], self.conn.seen)
        self.assertEqual(1.0, self.hub._by_addr[self.addr].last_seen)

    def test_an_unknown_sender_is_dropped_not_guessed(self):
        before = self.hub.unknown_in
        self.hub._handle(udpsync.build_tick_clock(1, 2), ("127.0.0.1", 49997), 1.0)
        self.assertEqual([], self.conn.seen)
        self.assertEqual(before + 1, self.hub.unknown_in)

    def test_a_connection_without_the_method_or_raising_is_not_a_crash(self):
        self.hub._by_addr[self.addr] = udpsync.Endpoint(object(), self.addr, 0.0)
        self.hub._handle(udpsync.build_tick_clock(1, 2), self.addr, 1.0)

        class Boom:
            def note_tick_clock(self, *a):
                raise RuntimeError("boom")
        self.hub._by_addr[self.addr] = udpsync.Endpoint(Boom(), self.addr, 0.0)
        self.hub._handle(udpsync.build_tick_clock(1, 2), self.addr, 1.0)


class RelayForwardingTests(unittest.TestCase):
    """中继**原样转发**，一个字节不看（取值在游戏服，改了不该要求重发中继）。"""

    def test_the_relay_forwards_it_verbatim(self):
        with open(os.path.join(ROOT, "server", "relay.py"), encoding="utf-8") as fp:
            src = fp.read()
        body = src[src.index("def _on_hook_datagram"):]
        body = body[:body.index("\n    def ")]
        self.assertIn("udpsync.MSG_TICK_CLOCK", body, "中继根本没认这个 kind")
        self.assertNotIn("parse_tick_clock", src)


class GameServerSideTests(unittest.TestCase):
    """游戏服这一侧：只存事实、`tick_latest` 只往前走、有界、发 `0x0400` 清空。"""

    @classmethod
    def setUpClass(cls):
        import gameserver
        cls.gs = gameserver

    def _conn(self):
        conn = self.gs.Conn.__new__(self.gs.Conn)
        conn.account_name = "tester"
        conn.online_debug = lambda *a: None
        return conn

    def test_a_connection_that_never_reported_has_nothing(self):
        conn = self._conn()
        self.assertIsNone(conn.tick_clock)
        self.assertIsNone(conn.tick_latest)
        self.assertIsNone(conn.shell_birth)

    def test_it_stores_ticks_and_births(self):
        conn = self._conn()
        conn.note_tick_clock(100, 5000, [200002, 200003])
        conn.note_tick_clock(101, 5033, [])
        self.assertEqual({100: 5000, 101: 5033}, dict(conn.tick_clock))
        self.assertEqual((101, 5033), conn.tick_latest)
        self.assertEqual({200002: 100, 200003: 100}, dict(conn.shell_birth))

    def test_a_late_datagram_is_kept_but_does_not_move_latest_backwards(self):
        conn = self._conn()
        conn.note_tick_clock(101, 5033)
        conn.note_tick_clock(100, 5000)            # UDP 乱序晚到
        self.assertEqual((101, 5033), conn.tick_latest)
        self.assertEqual(5000, conn.tick_clock[100])

    def test_it_is_bounded(self):
        conn = self._conn()
        keep = self.gs.TICK_CLOCK_KEEP
        for tick in range(keep + 10):
            conn.note_tick_clock(tick, tick * 32, [300000 + tick])
        self.assertEqual(keep, len(conn.tick_clock))
        self.assertEqual(keep, len(conn.shell_birth))
        self.assertNotIn(0, conn.tick_clock)                     # 最老的先挤掉
        self.assertIn(keep + 9, conn.tick_clock)

    def test_preparing_the_next_game_forgets_the_old_clock(self):
        """帧号是每个 Stage 从 0 数的 —— 上一局的一条都不能留到下一局。"""
        conn = self._conn()
        conn.room_generation = lambda kind=None: 1
        conn.note_tick_clock(500, 9000, [200002])
        conn.note_epoch_from_frame(self.gs.build_game(self.gs.OP_PREPARE_GAME, b"\x00" * 4))
        self.assertIsNone(conn.tick_clock)
        self.assertIsNone(conn.tick_latest)
        self.assertIsNone(conn.shell_birth)


class BotClockTests(unittest.TestCase):
    """`bot._mover_clock(room, terrain, shell)`：房主报过这颗弹的出膛帧 ⇒ 逐帧照抄 Timer − t0。"""

    @classmethod
    def setUpClass(cls):
        import bot
        import gameserver
        cls.bot, cls.gs = bot, gameserver

    def _host(self, t0=4000):
        conn = self.gs.Conn.__new__(self.gs.Conn)
        conn.account_name = "host"
        conn.online_debug = lambda *a: None
        conn.note_mover_phase(t0 + 10, 0, [(296, t0, 0)], now=95.0)
        return conn

    def _room(self, conn):
        seat = types.SimpleNamespace(conn=conn)
        return types.SimpleNamespace(seats=[seat], host_seat=0, quest=None)

    @staticmethod
    def _terrain():
        return types.SimpleNamespace(movers=(types.SimpleNamespace(handle=296),))

    @staticmethod
    def _shell(handle, ticks):
        return types.SimpleNamespace(handle=handle, ticks=ticks)

    def test_the_kth_step_reads_the_timer_of_birth_plus_k_minus_1(self):
        conn = self._host(t0=4000)
        conn.note_tick_clock(10, 5000, [200002])       # 第 10 帧出膛、第 1 格
        conn.note_tick_clock(11, 5017, [])             # 渲染和逻辑的相位差是逐帧变的，照抄
        conn.note_tick_clock(12, 5066, [])
        room, terrain = self._room(conn), self._terrain()
        with self.bot._tick_clock(100.0):
            self.assertEqual(1000, self.bot._mover_clock(room, terrain, self._shell(200002, 1)))
            self.assertEqual(1017, self.bot._mover_clock(room, terrain, self._shell(200002, 2)))
            self.assertEqual(1066, self.bot._mover_clock(room, terrain, self._shell(200002, 3)))

    def test_a_tick_not_reported_yet_is_extrapolated_from_the_latest(self):
        # 远程：服务端推这一格时客户端那一帧的包还在路上 ⇒ 最近一帧 + 32·Δ
        conn = self._host(t0=4000)
        conn.note_tick_clock(10, 5000, [200002])
        conn.note_tick_clock(12, 5066, [])
        room, terrain = self._room(conn), self._terrain()
        with self.bot._tick_clock(100.0):
            self.assertEqual(1066 + 32 * 3,
                             self.bot._mover_clock(room, terrain, self._shell(200002, 6)))
            # 丢了的那一帧（11）也按最近一帧外推（往回推）
            self.assertEqual(1066 - 32,
                             self.bot._mover_clock(room, terrain, self._shell(200002, 2)))

    def test_without_a_birth_it_falls_back_to_the_per_second_phase(self):
        conn = self._host(t0=4000)                      # 95.0 收到「起点后 10 ms」
        conn.note_tick_clock(10, 5000, [200002])
        room, terrain = self._room(conn), self._terrain()
        with self.bot._tick_clock(100.0):
            # 句柄对不上（别人开的枪 / 包丢了）：老路 = 10 + (100 − 95)·1000
            self.assertEqual(5010, self.bot._mover_clock(room, terrain, self._shell(999999, 1)))
            # 不带 shell（瞄准、规划那几处）也走老路
            self.assertEqual(5010, self.bot._mover_clock(room, terrain))

    def test_the_timer_wraps_like_a_dword(self):
        conn = self._host(t0=0xFFFFFF00)
        conn.note_tick_clock(3, 0x00000064, [200010])
        room, terrain = self._room(conn), self._terrain()
        with self.bot._tick_clock(100.0):
            self.assertEqual(0x164, self.bot._mover_clock(room, terrain, self._shell(200010, 1)))

    def test_only_the_host_counts(self):
        host = self._host(t0=4000)
        other = self._host(t0=4000)
        other.note_tick_clock(10, 9999, [200002])       # 非房主报的不算
        room = types.SimpleNamespace(seats=[types.SimpleNamespace(conn=host),
                                            types.SimpleNamespace(conn=other)],
                                     host_seat=0, quest=None)
        with self.bot._tick_clock(100.0):
            self.assertEqual(5010, self.bot._mover_clock(room, self._terrain(),
                                                         self._shell(200002, 1)))


def _c_source():
    with open(BSHOOK, encoding="utf-8") as fp:
        return fp.read()


class HookSourceTests(unittest.TestCase):
    """`hook/bshook.c` 那一侧：号对得上、装上了、只在有移动平台的图上发。"""

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(BSHOOK):
            raise unittest.SkipTest("不在源码仓库里")
        cls.src = _c_source()

    def _define(self, name):
        m = re.search(r"#define\s+%s\s+(0x[0-9A-Fa-f]+|\d+)u?" % name, self.src)
        self.assertIsNotNone(m, "%s 没有定义" % name)
        return int(m.group(1), 0)

    def test_the_kind_matches_the_server(self):
        self.assertEqual(udpsync.MSG_TICK_CLOCK, self._define("SYNC_MSG_TICK_CLOCK"))

    def test_the_stage_offsets_are_the_ones_in_findings_81(self):
        self.assertEqual(0xD4, self._define("MOVER_STAGE_TICK_OFF"))
        self.assertEqual(0xE0, self._define("MOVER_STAGE_NOW_OFF"))
        self.assertEqual(0x004904CC, self._define("TICKCLK_VA"))

    def test_it_is_installed_and_births_are_noted_in_the_add_hook(self):
        self.assertIn("static int try_patch_tick_clock(void)", self.src)
        self.assertIn("if (try_patch_tick_clock()) break;", self.src,
                      "定义了但 patch_thread 没调")
        hook = self.src[self.src.index("static void __cdecl proj_add_hook"):]
        hook = hook[:hook.index("\n}\n")]
        self.assertIn("tick_note_birth(", hook)
        # 诊断那一半跟开关走，登记那一半常开
        self.assertIn("g_proj_diag_on", hook)

    def test_it_only_speaks_on_maps_with_movers(self):
        body = self.src[self.src.index("static void __cdecl tick_clock_on_logic"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("g_mover_count", body)
        self.assertLess(body.index("g_mover_count"), body.index("sync_send_raw"))


class PatchSiteTests(unittest.TestCase):
    """镜像那一侧：挂的地方、读的偏移，都是 §81 说的那几句。"""

    @classmethod
    def setUpClass(cls):
        for path in (BSHOOK, IMG):
            if not os.path.isfile(path):
                raise unittest.SkipTest("不在源码仓库里（缺 %s）" % path)
        with open(IMG, "rb") as fp:
            cls.img = fp.read()
        cls.src = _c_source()

    def read(self, va, n):
        return self.img[va - 0x400000:va - 0x400000 + n]

    def dword(self, va):
        return struct.unpack("<I", self.read(va, 4))[0]

    def test_the_signature_is_what_the_image_has_and_is_unique(self):
        m = re.search(r"TICKCLK_SIG\[5\]\s*=\s*\{([^}]*)\}", self.src)
        sig = bytes(int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{2})", m.group(1)))
        self.assertEqual(self.read(0x004904CC, 5), sig)
        self.assertEqual(1, self.img.count(sig))

    def test_it_is_the_logic_tick_of_every_game_context(self):
        # GameContext 虚表 +0x80 就是它；GameStage 的逻辑帧（vft+0x88 = 0x478869）调的是
        # `[[0x72e2dc]] + 0x80`
        self.assertEqual(0x004904CC, self.dword(0x00670B4C + 0x80))
        self.assertEqual(0x00478869, self.dword(0x0066C074 + 0x88))
        self.assertEqual(b"\xff\x90\x80\x00\x00\x00", self.read(0x004788A9, 6))

    def test_stage_update_numbers_the_logic_ticks(self):
        # 0x42b4f9  inc dword [edi+0xd4] ；0x42b505 imul eax,[0x6dc528] ；0x42b513 mov [edi+0xd0],eax
        self.assertEqual(b"\xff\x87\xd4\x00\x00\x00", self.read(0x0042B4F9, 6))
        self.assertEqual(b"\x0f\xaf\x05\x28\xc5\x6d\x00", self.read(0x0042B505, 7))
        self.assertEqual(32, self.dword(0x006DC528))
        self.assertEqual(b"\xff\x90\x88\x00\x00\x00", self.read(0x0042B522, 6))

    def test_the_timer_is_stored_by_the_render_half_and_movers_are_placed_there(self):
        # Stage::Tick：0x42b580 mov [ebx+0xe0], eax（now）
        self.assertEqual(b"\x89\x83\xe0\x00\x00\x00", self.read(0x0042B580, 6))
        # GameContext::Update：挂路径的对象 GetWorldPos → 写 +0x2c / +0x30
        self.assertEqual(b"\x89\x4e\x2c\x89\x46\x30", self.read(0x00490950, 6))


if __name__ == "__main__":
    unittest.main()
