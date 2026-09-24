#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务端外推真人**认移动平台**（X_Mod §85）：`MapTerrain.at()` 地形视图 + `bot._advance_humans`。

钉的是三件客户端事实：

1. 鱼对角色就是**会动的地形格**（`0x473969` 静态格与对象取 max）—— 站、走、落、撞头都认；
2. 渲染时平台把自己这一帧的位移加给「站在它上面、还踩着地」的人（`0x51ab04`，站在谁上面
   由每个逻辑帧 `0x50739a` 按脚下那格刷新）；
3. 第 n 帧按跳 ⇒ 那一帧先走完这一步才离地，第 n + 1 帧才开始往上
   （2026-09-23 云桥 83 次起跳：第 n + d 帧的心跳里只有 d − 1 次空中位移）。

★ 纯标准库，两套运行时都跑。
"""
import collections
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, HERE)

import bot  # noqa: E402
import botmove  # noqa: E402
import botsync  # noqa: E402
import gameserver  # noqa: E402
import mapdata  # noqa: E402
from test_mapdata import make_record  # noqa: E402
from test_mappath import _mask_blob  # noqa: E402

WIDTH, HEIGHT, FLOOR = 400, 320, 300
MOVER = 7
TICK_S = botmove.TICK_MS / 1000.0

_CACHE = {}


def ride_terrain(movers=True):
    """地面在 y=300；一块 60×10 的实心平台，中心 (140,150) ⇄ (260,150)，单程 1000 ms。"""
    key = movers
    if key not in _CACHE:
        rows = ["0" * WIDTH if y < FLOOR else "2" * WIDTH
                for y in range(HEIGHT)]
        record = make_record(rows, name="RideTest")
        if movers:
            record["movers"] = [{
                "handle": MOVER, "x": 200.0, "y": 150.0, "loop": 0,
                "pts": [[-60.0, 0.0, 1000, 0.0, 0.0, 1.0],
                        [60.0, 0.0, 1000, 0.0, 0.0, 1.0]],
                "riders": [{"handle": 8, "w": 60, "h": 10, "sx": 1.0,
                            "sy": 1.0, "x": 0.0, "y": 0.0, "t_off": 0,
                            "rel": 0, "mask": _mask_blob(60, 10)}]}]
        _CACHE[key] = mapdata.MapTerrain(record)
    return _CACHE[key]


def brute_surfaces(view, x):
    out = []
    above = 0
    for y in range(view.height):
        here = view.cell(x, y)
        if here and not above:
            out.append(y)
        above = here
    return out


class TerrainAtTests(unittest.TestCase):
    """地形视图：某一刻的鱼叠在静态格上，`botmove` 问什么都看得见它。"""

    def setUp(self):
        self.terrain = ride_terrain()
        self.view = self.terrain.at(0)

    def test_no_time_or_no_movers_is_the_terrain_itself(self):
        """★ 不知道是哪一刻 / 这张图没有移动平台 —— 一格不差的老行为。"""
        self.assertIs(self.terrain, self.terrain.at(None))
        plain = ride_terrain(movers=False)
        self.assertIs(plain, plain.at(1234))

    def test_the_platform_is_solid_where_it_is_at_that_moment(self):
        self.assertEqual(0, self.terrain.cell(140, 150), "静态格里没有它")
        self.assertEqual(2, self.view.cell(140, 150))
        self.assertEqual(0, self.view.cell(260, 150))
        self.assertEqual(2, self.terrain.at(1000).cell(260, 150), "一秒后到了右端")
        self.assertEqual(2, self.view.cell(140, FLOOR + 5), "静态地面照旧")
        self.assertTrue(self.view.blocks_bullet(140, 150), "值 2 = 头撞得上去")

    def test_surfaces_merge_the_platform_and_the_floor(self):
        """站立面和逐格暴力扫出来的一模一样：平台顶一个，平台底下的地面一个。"""
        for x in range(100, 200):
            self.assertEqual(brute_surfaces(self.view, x),
                             list(self.view.surfaces(x)), x)
        top = self.view.surfaces(140)[0]
        self.assertLess(top, 150)
        self.assertEqual([top, FLOOR], list(self.view.surfaces(140)))
        self.assertEqual((FLOOR,), tuple(self.terrain.surfaces(140)))

    def test_falling_lands_on_the_platform_before_the_floor(self):
        top = self.view.surfaces(140)[0]
        self.assertEqual(top, self.view.ground_below(140, 100))
        self.assertEqual(FLOOR, self.view.ground_below(140, top + 20))

    def test_who_is_under_the_feet(self):
        top = self.view.surfaces(140)[0]
        got = self.view.rider_under(140, top - 1)
        self.assertIsNotNone(got, "脚比实心区第一行高 1 px 也算站在它上面")
        self.assertEqual(MOVER, got[0].handle)
        self.assertIsNone(self.view.rider_under(350, FLOOR - 1), "静态地面")
        self.assertIsNone(self.view.rider_under(140, top - 30), "空中")

    def test_the_coarse_grid_does_not_hide_the_platform(self):
        """★ 静态粗网格担保「这一块空」时，平台可能正好在那儿 —— 不许跟着担保。"""
        self.assertTrue(self.terrain.coarse_clear(130, 140, 150, 160))
        self.assertFalse(self.view.coarse_clear(130, 140, 150, 160))
        self.assertTrue(self.view.coarse_clear(300, 20, 330, 40))


class _Obj(object):
    pass


class HumanRideTests(unittest.TestCase):
    """`bot._advance_humans` 按那一帧的平台外推真人；心跳 / rpJump 走真的 `note_sync_position`。"""

    def setUp(self):
        self.terrain = ride_terrain()
        conn = _Obj()
        conn.sync_trail = collections.deque(maxlen=64)
        conn.sync_trail_seq = 0
        conn.sync_jumped = 0
        conn.sync_jump_ticks = ()
        conn.sync_trail_at = None
        conn.sync_crouch = False
        conn.sim_body = None
        conn.sim_body_mark = None
        conn.sim_step = 0
        # 他自己报的平台相位：t=0 那一刻在起点。
        conn.mover_phase = {MOVER: (0.0, 0, 0, 0)}
        self.conn = conn
        seat = _Obj()
        seat.is_bot = False
        seat.character_id = 0
        seat.conn = conn
        room = _Obj()
        room.seats = [seat, None]
        room.host_seat = None
        room.quest = None
        self.room = room
        self.top = self.terrain.at(0).surfaces(140)[0]
        self.seq = 0

    def beat(self, x, y, at, on_ground=True, velocity=(0, 0)):
        state = botsync.character_state(x, y, vx=velocity[0], vy=velocity[1],
                                        on_ground=on_ground)
        gameserver.Conn.note_sync_position(
            self.conn, botsync.build_peer_packet(
                0, botsync.OP_HEARTBEAT, botsync.heartbeat_body(0, 0, state),
                game_id=1), at)

    def jump(self, at, stage=1):
        self.seq += 1
        gameserver.Conn.note_sync_position(
            self.conn, botsync.build_peer_packet(
                0, botsync.OP_JUMP, botsync.jump_body(0, stage), game_id=1,
                sequence=self.seq), at)

    def advance(self, ticks=1):
        for _ in range(ticks):
            bot._advance_humans(self.room, self.terrain)
        return self.conn.sim_body

    def center(self, t_ms):
        mover = self.terrain.movers[0]
        return mover.rider_center(mover.riders[0], t_ms)

    def test_standing_on_the_platform_is_carried(self):
        """★★ 站着不动也跟着平台走，每一格的位移就是平台那一帧的位移（`0x51ab04`）。"""
        self.beat(140, self.top - 1, at=0.0)
        start = self.advance()                 # 硬置那一格
        self.assertEqual((140.0, self.top - 1.0), (start.x, start.y))
        for step in range(1, 12):
            body = self.advance()
            want = 140.0 + self.center(32 * step)[0] - self.center(0)[0]
            self.assertAlmostEqual(want, body.x, places=6)
            self.assertEqual(self.top - 1.0, body.y, "平台只横着走，人高度不变")
            self.assertTrue(body.on_ground)
        self.assertGreater(body.x, 140.0 + 5.0, "夹具没造对：平台得真的在动")

    def test_without_the_platform_phase_it_is_the_old_static_world(self):
        """拿不到相位（没报过、房间也没开局时刻）就不认平台 —— 穿过它掉到地面上。

        ★ 落在扫掠的**空点**上：实心第一行的上面一格（客户端 `0x50efd2`，X_Mod §87）。
        """
        self.conn.mover_phase = None
        self.beat(140, self.top - 40, at=0.0, on_ground=False,
                  velocity=(0, 5))
        self.advance()
        for _ in range(40):
            body = self.advance()
        self.assertTrue(body.on_ground)
        self.assertEqual(float(FLOOR - 1), body.y, "一路掉到地面上")

    def test_falling_onto_the_platform_lands_on_it_and_rides(self):
        self.beat(140, self.top - 40, at=0.0, on_ground=False,
                  velocity=(0, 5))
        self.advance()
        for _ in range(10):
            body = self.advance()
            if body.on_ground:
                break
        self.assertTrue(body.on_ground, "该落在平台上")
        self.assertLess(body.y, 160.0, "落在平台顶上，不是穿过去掉到地面")
        x0, steps = body.x, self.conn.sim_step
        body = self.advance()
        moved = self.center(32 * (steps + 1))[0] - self.center(32 * steps)[0]
        self.assertAlmostEqual(x0 + moved, body.x, places=6, msg="落上去就跟着走")

    def test_a_jump_right_after_the_heartbeat(self):
        """★★★ 起跳那一帧先走完这一步才离地：第 1 格离地不挪，第 2 格才往上。

        ★ 第 2 格是「起跳后那一格」：只按速度挪、不加重力 —— 正好升 20（X_Mod §87）。
        """
        self.beat(350, FLOOR - 1, at=10.0)
        self.jump(at=10.001)
        self.assertEqual(((0, 1),), self.conn.sync_jump_ticks)
        before = self.advance()                # 硬置
        body = self.advance()
        self.assertFalse(body.on_ground)
        self.assertEqual(before.y, body.y)
        self.assertEqual(-botmove.JUMP_SPEED, body.vy)
        body = self.advance()
        self.assertAlmostEqual(before.y - botmove.JUMP_SPEED, body.y, places=6)
        self.assertEqual(-botmove.JUMP_SPEED, body.vy)
        body = self.advance()
        self.assertAlmostEqual(
            before.y - botmove.JUMP_SPEED - (botmove.JUMP_SPEED - botmove.GRAVITY),
            body.y, places=6)

    def test_a_heartbeat_right_at_the_takeoff_still_lifts_by_the_full_speed(self):
        """★★ 心跳正好落在「刚起跳、还没动」那一格（腾空、vy = −20、上一发还踩地）：
        硬置之后下一格照样按起跳那一步走 —— 2026-09-23 / 24 两次运行 72 个这样的区间，
        这么走 71 个更准（X_Mod §87）。"""
        self.beat(350, FLOOR - 1, at=10.0)
        self.beat(350, FLOOR - 1, at=10.1, on_ground=False,
                  velocity=(0, -int(botmove.JUMP_SPEED)))
        before = self.advance()                # 硬置
        self.assertTrue(self.conn.sim_launch)
        body = self.advance()
        self.assertAlmostEqual(before.y - botmove.JUMP_SPEED, body.y, places=6)

    def test_an_airborne_heartbeat_is_not_a_takeoff(self):
        """同样是 vy = −20，上一发已经在空中（二段跳减到这儿 / 被弹起来）就是普通的腾空。"""
        self.beat(350, 200, at=10.0, on_ground=False, velocity=(0, -24))
        self.beat(350, 180, at=10.1, on_ground=False,
                  velocity=(0, -int(botmove.JUMP_SPEED)))
        before = self.advance()
        self.assertFalse(self.conn.sim_launch)
        body = self.advance()
        self.assertAlmostEqual(before.y - (botmove.JUMP_SPEED - botmove.GRAVITY),
                               body.y, places=6)

    def test_a_jump_two_frames_after_the_heartbeat(self):
        """rpJump 离心跳 2 个逻辑帧 ⇒ 第 3 格离地、第 4 格才往上。"""
        self.beat(350, FLOOR - 1, at=10.0)
        self.jump(at=10.0 + 2 * TICK_S + 0.004)
        self.assertEqual(((2, 1),), self.conn.sync_jump_ticks)
        before = self.advance()
        for _ in range(2):
            self.assertTrue(self.advance().on_ground)
        body = self.advance()
        self.assertFalse(body.on_ground)
        self.assertEqual(before.y, body.y)
        self.assertLess(self.advance().y, before.y)

    def test_a_late_jump_is_not_lost(self):
        """远程抖动时 rpJump 到得比它该生效的那一格还晚 —— 当格补上，不丢。"""
        self.beat(350, FLOOR - 1, at=10.0)
        self.advance(4)
        self.jump(at=10.001)
        self.assertFalse(self.advance().on_ground)
        self.assertEqual((), self.conn.sync_jump_ticks)

    def test_a_heartbeat_consumes_the_scheduled_jumps(self):
        self.beat(350, FLOOR - 1, at=10.0)
        self.jump(at=10.001)
        self.beat(350, FLOOR - 1, at=10.1, on_ground=False,
                  velocity=(0, -19))
        self.assertEqual((), self.conn.sync_jump_ticks)

    def test_jumping_off_the_platform_leaves_it_behind(self):
        """起跳那一帧末已经离地 ⇒ 平台不再驮他；空中水平速度只有起跳那一刻的走速（§93）。"""
        self.beat(140, self.top - 1, at=0.0)
        self.jump(at=0.001)
        self.advance()
        body = self.advance()
        self.assertFalse(body.on_ground)
        self.assertEqual(140.0, body.x, "离地那一帧不驮")
        for _ in range(5):
            body = self.advance()
        self.assertEqual(140.0, body.x, "竖直起跳就竖直上下")

    def test_his_own_platform_phase_wins_over_the_room_clock(self):
        """★ 每台客户端的鱼按它自己的时钟摆 —— 先用他自己报的。"""
        self.conn.sync_trail_at = 5.0
        self.conn.mover_phase = {MOVER: (4.0, 700, 0, 0)}
        self.assertEqual(1700, bot._human_mover_phase(self.room, self.terrain,
                                                      self.conn))
        self.conn.mover_phase = None
        self.assertIsNone(bot._human_mover_phase(self.room, self.terrain,
                                                 self.conn),
                          "房间也没有时钟 ⇒ 不知道")


if __name__ == "__main__":
    unittest.main()
