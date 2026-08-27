#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`server/botmove.py` 的测试 —— **人**怎么走、怎么跳、怎么掉下去。

两层，和 `test_mapdata.py` 一个路子：

1. **合成地形**：自己造一张小图（平地 / 斜坡 / 高台 / 悬崖 / 薄板 / 天花板），
   把每一条运动规则单独钉死；
2. **真产物**：`bot_mapdata/` 在的话，随便挑几张真图让人走上几百个 tick，
   断言他**从头到尾都站在合法的站立面上**（不会陷进地形、不会浮空）。

★ 这一层不碰协议、不碰房间 —— `botmove` 是纯函数 + 地形，就该这么测。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import botmove                                                 # noqa: E402
import mapdata                                                 # noqa: E402
from test_mapdata import make_record                           # noqa: E402


class Dummy(object):
    """一个只有 `speed` 的假角色（`chrprops.Character` 的最小替身）。"""

    def __init__(self, speed=4.0):
        self.speed = speed


def terrain_from(rows):
    return mapdata.MapTerrain(make_record(rows))


def flat(width=64, floor=20, height=32):
    """一张平地：`floor` 那一行整条实心，上面全空。"""
    rows = []
    for y in range(height):
        rows.append(("2" if y >= floor else "0") * width)
    return terrain_from(rows)


class WalkSpeedTests(unittest.TestCase):
    """速度的三档倍率 —— 全是原版常量（文件头那张表）。"""

    def test_base_is_chrspeed(self):
        self.assertEqual(7.0, botmove.walk_speed(Dummy(7.0)))

    def test_fast_run_is_one_and_a_half(self):
        self.assertAlmostEqual(10.5, botmove.walk_speed(Dummy(7.0),
                                                        fast_run=True))

    def test_crouch_is_a_third(self):
        self.assertAlmostEqual(7.0 / 3.0,
                               botmove.walk_speed(Dummy(7.0), crouched=True))

    def test_both_multiply(self):
        self.assertAlmostEqual(7.0 * 1.5 / 3.0,
                               botmove.walk_speed(Dummy(7.0), fast_run=True,
                                                  crouched=True))

    def test_a_character_without_speed_still_walks(self):
        """产物缺字段时不许变成 0 —— 那样 bot 会一动不动而且没人知道为什么。"""
        self.assertTrue(botmove.walk_speed(object()) > 0)


class FlatGroundTests(unittest.TestCase):

    def setUp(self):
        self.t = flat()
        self.who = Dummy(4.0)
        self.body = botmove.Body(30.0, 20.0)

    def test_standing_still_does_not_move(self):
        self.assertEqual(self.body, botmove.tick(self.t, self.body, self.who))

    def test_one_tick_is_one_chrspeed(self):
        step = botmove.tick(self.t, self.body, self.who, direction=1)
        self.assertAlmostEqual(34.0, step.x)
        self.assertAlmostEqual(20.0, step.y)
        self.assertTrue(step.on_ground)

    def test_left_is_negative(self):
        step = botmove.tick(self.t, self.body, self.who, direction=-1)
        self.assertAlmostEqual(26.0, step.x)

    def test_on_the_ground_the_velocities_stay_zero(self):
        """★ 心跳的口径（§35）：踩地时速度就该是 0，不能从位移反推。"""
        step = botmove.advance(self.t, self.body, self.who, 4, direction=1)
        self.assertEqual((0.0, 0.0), (step.vx, step.vy))

    def test_advance_is_four_ticks_of_walking(self):
        step = botmove.advance(self.t, self.body, self.who,
                               botmove.TICKS_PER_BEAT, direction=1)
        self.assertAlmostEqual(30.0 + 4 * 4.0, step.x)

    def test_the_map_edge_is_a_wall(self):
        """出界 = 实心（`mapdata` 的 `OUT_OF_BOUNDS`）—— 走不出去。"""
        body = botmove.Body(1.0, 20.0)
        for _ in range(10):
            body = botmove.tick(self.t, body, self.who, direction=-1)
        self.assertTrue(body.x >= 0.0)


class SlopeAndWallTests(unittest.TestCase):
    """能爬的坡 vs 爬不上去的墙 —— 分界是语料量的 `CLIMB_SLOPE`。"""

    def rows(self, heights):
        """`heights[x]` = 第 x 列的地面 y（越小越高）。"""
        width, height = len(heights), 32
        return ["".join("2" if y >= heights[x] else "0"
                        for x in range(width)) for y in range(height)]

    def test_a_gentle_slope_is_walkable(self):
        # 每列升 2 个单位 = 坡度 2，正好是真人走得动的上限。
        heights = [20 - (x - 10) * 2 if x > 10 else 20 for x in range(40)]
        heights = [max(4, h) for h in heights]
        t = terrain_from(self.rows(heights))
        body = botmove.Body(5.0, 20.0)
        step = botmove.tick(t, body, Dummy(4.0), direction=1)
        self.assertAlmostEqual(9.0, step.x)
        self.assertTrue(step.on_ground)

    def test_a_step_taller_than_the_climb_is_a_wall(self):
        # 第 20 列起直接高 30 个单位：一个 tick 只能爬 4 × 2 = 8。
        heights = [20 if x < 20 else 20 - 30 for x in range(40)]
        t = terrain_from(self.rows([max(1, h) for h in heights]))
        body = botmove.Body(18.0, 20.0)
        who = Dummy(4.0)
        step = botmove.tick(t, body, who, direction=1)
        self.assertEqual(body.x, step.x, "撞墙就该原地不动")
        self.assertTrue(botmove.blocked(t, body, who, 1))
        self.assertFalse(botmove.blocked(t, body, who, -1))

    def test_walking_off_a_ledge_leaves_the_ground(self):
        heights = [20 if x < 20 else 31 for x in range(40)]
        t = terrain_from(self.rows(heights))
        body = botmove.Body(18.0, 20.0)
        who = Dummy(4.0)
        self.assertTrue(botmove.leaves_ground(t, body, who, 1))
        step = botmove.tick(t, body, who, direction=1)
        self.assertFalse(step.on_ground)
        self.assertAlmostEqual(4.0, step.vx, msg="踩空时保持这一步的走速")
        landed = botmove.settle(t, step, who)
        self.assertTrue(landed.on_ground)
        self.assertAlmostEqual(31.0, landed.y)

    def test_drop_below_measures_the_fall(self):
        heights = [20 if x < 20 else 31 for x in range(40)]
        t = terrain_from(self.rows(heights))
        body = botmove.Body(18.0, 20.0)
        self.assertAlmostEqual(11.0,
                               botmove.drop_below(t, body, Dummy(4.0), 1))

    def test_a_bottomless_pit_reports_none(self):
        """★ 掉不到底的坑返回 `None` —— 决策层靠它区分「台阶」和「陷阱」。"""
        rows = ["0" * 40 for _ in range(32)]
        rows = [("2" * 20 + "0" * 20) if y >= 20 else r
                for y, r in enumerate(rows)]
        t = terrain_from(rows)
        body = botmove.Body(18.0, 20.0)
        self.assertIsNone(botmove.drop_below(t, body, Dummy(4.0), 1))

    def test_standing_still_never_counts_as_blocked(self):
        t = flat()
        body = botmove.Body(30.0, 20.0)
        self.assertFalse(botmove.blocked(t, body, Dummy(4.0), 0))
        self.assertFalse(botmove.leaves_ground(t, body, Dummy(4.0), 0))


class JumpTests(unittest.TestCase):
    """起跳初速 20、重力 1.2 —— 顶点 `v²/2g ≈ 167`（语料中位 170）。"""

    def setUp(self):
        self.t = flat(width=200, floor=200, height=256)
        self.who = Dummy(4.0)
        self.body = botmove.Body(100.0, 200.0)

    def test_apex_matches_the_closed_form(self):
        self.assertAlmostEqual(166.67, botmove.jump_apex(), places=1)

    def test_a_jump_rises_about_the_apex_and_comes_back(self):
        body = botmove.jump(self.body)
        self.assertFalse(body.on_ground)
        top = body.y
        beats = 0
        while not body.on_ground and beats < 200:
            body = botmove.tick(self.t, body, self.who)
            top = min(top, body.y)
            beats += 1
        self.assertTrue(body.on_ground, "跳起来总得落回地面")
        self.assertAlmostEqual(200.0, body.y)
        rise = self.body.y - top
        self.assertTrue(150.0 <= rise <= 175.0, "顶点高 %.1f" % rise)

    def test_the_flight_lasts_about_two_v_over_g(self):
        """滞空 ≈ 2v/g = 33 个 tick ≈ 8 发心跳（语料里往下跳的更久）。"""
        body = botmove.jump(self.body)
        ticks = 0
        while not body.on_ground and ticks < 200:
            body = botmove.tick(self.t, body, self.who)
            ticks += 1
        self.assertTrue(30 <= ticks <= 36, "滞空 %d 个 tick" % ticks)

    def test_jumping_in_the_air_does_nothing(self):
        body = botmove.jump(self.body)
        again = botmove.jump(body)
        self.assertEqual(body, again)

    def test_holding_a_direction_in_the_air_moves_at_one_and_a_half(self):
        body = botmove.tick(self.t, self.body, self.who, want_jump=True)
        moved = botmove.tick(self.t, body, self.who, direction=1)
        self.assertAlmostEqual(4.0 * botmove.AIR_KEY_FACTOR,
                               moved.x - body.x)

    def test_want_jump_only_fires_on_the_first_tick(self):
        body = botmove.advance(self.t, self.body, self.who, 8,
                               direction=1, want_jump=True)
        self.assertFalse(body.on_ground)
        self.assertTrue(body.y < self.body.y)


class CeilingAndPlatformTests(unittest.TestCase):

    def test_a_ceiling_stops_the_rise(self):
        rows = []
        for y in range(64):
            if y >= 40:
                rows.append("2" * 40)
            elif y in (20, 21):
                rows.append("2" * 40)
            else:
                rows.append("0" * 40)
        t = terrain_from(rows)
        body = botmove.jump(botmove.Body(20.0, 40.0))
        who = Dummy(4.0)
        top = body.y
        for _ in range(40):
            body = botmove.tick(t, body, who)
            top = min(top, body.y)
            if body.on_ground:
                break
        self.assertTrue(top >= 21.0, "撞了天花板就不该再上去（到了 %.1f）" % top)

    def test_a_one_way_platform_can_be_jumped_through(self):
        """★ 值 1 的薄板往上跳穿得过去（§29），落下来的时候踩得住。"""
        rows = []
        for y in range(64):
            if y >= 40:
                rows.append("2" * 40)
            elif y == 20:
                rows.append("1" * 40)
            else:
                rows.append("0" * 40)
        t = terrain_from(rows)
        who = Dummy(4.0)
        body = botmove.jump(botmove.Body(20.0, 40.0))
        top = body.y
        for _ in range(60):
            body = botmove.tick(t, body, who)
            top = min(top, body.y)
            if body.on_ground:
                break
        self.assertTrue(top < 20.0, "单向平台不该挡住上升（只到了 %.1f）" % top)
        self.assertAlmostEqual(20.0, body.y, msg="落下来该踩在薄板上")


class BodyTests(unittest.TestCase):

    def test_on_the_ground_velocities_are_forced_to_zero(self):
        body = botmove.Body(1.0, 2.0, vx=9.0, vy=-9.0, on_ground=True)
        self.assertEqual((0.0, 0.0), (body.vx, body.vy))

    def test_in_the_air_they_are_kept(self):
        body = botmove.Body(1.0, 2.0, vx=9.0, vy=-9.0, on_ground=False)
        self.assertEqual((9.0, -9.0), (body.vx, body.vy))

    def test_ticks_for_is_at_least_one(self):
        self.assertEqual(1, botmove.ticks_for(0.0))
        self.assertEqual(4, botmove.ticks_for(0.128))

    def test_no_terrain_means_no_movement(self):
        """地图数据缺失时**原地不动**，不是乱走（fail-safe）。"""
        body = botmove.Body(5.0, 5.0)
        self.assertEqual(body, botmove.tick(None, body, Dummy(), direction=1))


class RealMapTests(unittest.TestCase):
    """真产物在的话再跑：在真图上走几百个 tick，人必须一直站得住。"""

    @classmethod
    def setUpClass(cls):
        cls.store = mapdata._Store()
        cls.names = cls.store.available()
        if not cls.names:
            raise unittest.SkipTest(
                "没有 bot_mapdata/ 产物，先跑 tools\\update-mapdata.bat")

    def walkable_start(self, terrain):
        """找一个真站得住的起点：某列的第一个站立面。"""
        for x in range(terrain.width // 4, terrain.width, 7):
            surfaces = terrain.surfaces(x)
            if surfaces:
                return botmove.Body(float(x), float(surfaces[-1]))
        return None

    def test_walking_across_real_maps_stays_on_the_ground(self):
        who = Dummy(7.0)
        checked = 0
        for name in self.names[::23]:
            terrain = self.store.load(name)
            body = self.walkable_start(terrain)
            if body is None:
                continue
            checked += 1
            for direction in (1, -1):
                walker = body
                for _ in range(240):
                    walker = botmove.tick(terrain, walker, who,
                                          direction=direction)
                    if walker.on_ground:
                        self.assertNotEqual(
                            0, terrain.cell(int(walker.x), int(walker.y)),
                            "%s: 站在空气里 (%.0f, %.0f)"
                            % (name, walker.x, walker.y))
                    self.assertTrue(0 <= walker.x < terrain.width,
                                    "%s: 走出图外 x=%.0f" % (name, walker.x))
        self.assertTrue(checked >= 3, "至少要真跑过几张图")

    def test_jumping_on_a_real_map_comes_back_down(self):
        who = Dummy(7.0)
        terrain = self.store.load(self.names[0])
        body = self.walkable_start(terrain)
        if body is None:
            self.skipTest("这张图找不到站立面")
        flying = botmove.jump(body)
        landed = botmove.settle(terrain, flying, who, ticks=400)
        self.assertTrue(landed.on_ground)


if __name__ == "__main__":
    unittest.main()
