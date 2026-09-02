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
import chrprops                                                # noqa: E402
import test_mapdata                                            # noqa: E402
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


class DropThroughTests(unittest.TestCase):
    """按 ↓ 穿值-1 平台；实心地面绝不能穿。"""

    def setUp(self):
        rows = []
        for y in range(32):
            if y == 10:
                rows.append("1" * 48)
            elif y >= 25:
                rows.append("2" * 48)
            else:
                rows.append("0" * 48)
        self.t = terrain_from(rows)
        self.who = Dummy(4.0)

    def test_drop_moves_below_the_one_way_band_and_enters_the_air(self):
        body = botmove.Body(20.0, 10.0)
        dropped = botmove.drop_through(self.t, body)
        self.assertFalse(dropped.on_ground)
        self.assertGreater(dropped.y, body.y)
        self.assertFalse(self.t.is_one_way(int(dropped.x), int(dropped.y)))

    def test_want_drop_falls_to_the_next_surface(self):
        body = botmove.tick(self.t, botmove.Body(20.0, 10.0), self.who,
                            want_drop=True)
        self.assertFalse(body.on_ground)
        landed = botmove.settle(self.t, body, self.who, ticks=80)
        self.assertTrue(landed.on_ground)
        self.assertAlmostEqual(25.0, landed.y)

    def test_down_does_not_pass_through_solid_ground(self):
        body = botmove.Body(20.0, 25.0)
        self.assertEqual(body, botmove.drop_through(self.t, body))
        self.assertEqual(body, botmove.tick(self.t, body, self.who,
                                            want_drop=True))

    def test_down_does_nothing_when_a_breakable_plugs_the_hole(self):
        """★★★ 白线底下紧贴着**冰块**时，按 ↓ 纹丝不动（V0.3 §136）。

        `Iceria00` 那两处窟窿就长这样：一根单向平台白线，下面整个塞满
        可破坏物（值 3）。不查「穿出去那一格是不是空的」的话，人会被
        挪到白线下沿 —— 也就是**冰块里面**。
        """
        rows = []
        for y in range(32):
            if y == 10:
                rows.append("1" * 48)
            elif 11 <= y < 20:
                rows.append("3" * 48)       # 白线底下整块冰
            elif y >= 25:
                rows.append("2" * 48)
            else:
                rows.append("0" * 48)
        t = terrain_from(rows)
        body = botmove.Body(20.0, 10.0)
        self.assertEqual(body, botmove.drop_through(t, body))
        self.assertEqual(body, botmove.tick(t, body, self.who,
                                            want_drop=True))


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

    def test_a_jump_carries_the_walking_speed_and_keeps_it(self):
        """★★★ 起跳带走这一刻的走速，腾空之后**方向键改不了它**（§93）。"""
        body = botmove.tick(self.t, self.body, self.who,
                            direction=1, want_jump=True)
        self.assertAlmostEqual(4.0, body.vx)
        # 空中改按左键：水平速度**一点不动**，位移还是 +4。
        moved = botmove.tick(self.t, body, self.who, direction=-1)
        self.assertAlmostEqual(4.0, moved.vx)
        self.assertAlmostEqual(4.0, moved.x - body.x)

    def test_a_standing_jump_goes_straight_up(self):
        """站着起跳是**竖直**的 —— 语料里 11256 发「腾空 + 按着键但 vx=0」。"""
        body = botmove.tick(self.t, self.body, self.who, want_jump=True)
        self.assertAlmostEqual(0.0, body.vx)
        moved = botmove.tick(self.t, body, self.who, direction=1)
        self.assertAlmostEqual(0.0, moved.x - body.x)

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

    def slab(self, rows_of_slab, floor=40, width=40, height=64):
        """一张平地 + 一块**薄天花板**（`rows_of_slab` 那几行整条实心）。"""
        rows = []
        for y in range(height):
            solid = y >= floor or y in rows_of_slab
            rows.append(("2" if solid else "0") * width)
        return terrain_from(rows)

    def rise(self, terrain, body, who, ticks=40):
        top = body.y
        for _ in range(ticks):
            body = botmove.tick(terrain, body, who)
            top = min(top, body.y)
            if body.on_ground:
                break
        return top

    def test_a_thin_ceiling_is_not_tunnelled_through(self):
        """★★★ **一个 tick 跨过整块板**也得撞上（V0.3 §169）。

        板在 25~26、地面在 40 —— 起跳第一个 tick 就从 40 升到 21.2，
        **落点（21）是空气**。只判落点的旧代码于是一声不响地穿过去，
        脚最后停在板**上面**；现在逐格扫，停在板底下沿之下那一格（27）。
        """
        t = self.slab((25, 26))
        who = Dummy(4.0)
        top = self.rise(t, botmove.jump(botmove.Body(20.0, 40.0)), who)
        self.assertGreaterEqual(top, 27.0,
                                "薄天花板被穿过去了（升到了 %.1f）" % top)

    def test_it_rises_all_the_way_up_to_the_ceiling(self):
        """★ 撞天花板不是「原地不动」：该一路升到板底下沿才停。

        只判落点的旧代码撞上时把脚留在**出发点**，一次跳等于白跳；
        收方是一格一格推上去的，挡住之前那一格才是终点。
        """
        t = self.slab((25, 26))
        who = Dummy(4.0)
        top = self.rise(t, botmove.jump(botmove.Body(20.0, 40.0)), who)
        self.assertAlmostEqual(27.0, top, places=3)

    def test_a_ledge_grazed_on_the_way_up_is_still_not_a_ceiling(self):
        """★ 逐格扫**不许**把台阶的上沿当成天花板（§95 那一条要保住）。

        左边地面 40、右边 36（4 像素的坎）；人斜着往上飞过那个坎，
        路上必然掠过右边那个站立面。它是台阶不是板 —— 照旧飞过去。
        """
        rows = []
        for y in range(64):
            rows.append("".join("2" if y >= (36 if x >= 24 else 40) else "0"
                                for x in range(64)))
        t = terrain_from(rows)
        who = Dummy(4.0)
        start = botmove.Body(20.0, 40.0, 4.0, -6.0, on_ground=False)
        body = start
        for _ in range(60):
            body = botmove.tick(t, body, who)
            if body.on_ground:
                break
        self.assertGreater(body.x - start.x, 8.0,
                           "小坎的上沿被当成天花板了（只飞了 %.1f）"
                           % (body.x - start.x))

    def test_a_body_stuck_in_the_ground_can_still_jump_out(self):
        """★★★★ **嵌在地形里的人必须跳得出来**（V0.3 §169 第二轮）。

        `Quest02_1` 岩浆坑左沿是一道缓上坡：地面在 444，而 bot 的身体按
        整数摆在 **453** —— 陷进去 9 个像素。逐格扫如果把头顶那一片
        「它自己陷进去的实心」当成天花板，这一跳就位移 0，下一帧接着跳，
        **一辈子过不了那个坑**（整局单测 `test_it_crosses_the_lava_pit…` 就是
        这么红的）。只判落点的旧代码天然放行 —— 这一条把它保住。
        """
        rows = []
        for y in range(64):
            rows.append("".join("2" if y >= 40 else "0" for _x in range(40)))
        t = terrain_from(rows)
        who = Dummy(4.0)
        buried = botmove.jump(botmove.Body(20.0, 49.0))   # 埋在地面下 9 格
        body = buried
        for _ in range(10):
            body = botmove.tick(t, body, who)
            if body.on_ground:
                break
        self.assertLess(body.y, 49.0,
                        "陷在地里的人跳不出来了（还在 %.1f）" % body.y)

    def test_the_sweep_never_lands_the_feet_inside_a_slab(self):
        """★★ 变异防线：停下来的那一点必须是**空气**，不能嵌在板里。

        板从 25 到 30（6 像素厚），各种初速各扫一遍。
        """
        t = self.slab((25, 26, 27, 28, 29, 30))
        who = Dummy(4.0)
        for vy in range(-24, 0):
            body = botmove.Body(20.0, 40.0, 0.0, float(vy), on_ground=False)
            for _ in range(40):
                body = botmove.tick(t, body, who)
                self.assertFalse(
                    25 <= int(body.y) <= 30,
                    "vy=%d 时脚停在了板里 (%.1f, %.1f)" % (vy, body.x, body.y))
                if body.on_ground:
                    break


class KnockedBackOverBumpsTests(unittest.TestCase):
    """★★★ 地形上一个**几像素的小坎**不该把整段击退吃掉（§95）。

    用户 2026-08-28 实机：`Forest_b (581, 651)` 左边一列的地面是 646
    （高 **5** 个像素）、`Domir_Newbie (483, 342)` 左边高 **4** 个像素 ——
    两次强度 15 的击退在日志里都是 **位移 +0**。两处都是同一个型：
    脚升过那个坎的上沿时被当成「撞天花板」/「撞墙」，速度当场清零。
    """

    def bumpy(self, step=5, edge=300, width=600, floor=150, height=220):
        """左边地面在 `floor`，`edge` 往右抬高 `step` 个像素。"""
        rows = []
        for y in range(height):
            rows.append("".join(
                "2" if y >= (floor - step if x >= edge else floor) else "0"
                for x in range(width)))
        return terrain_from(rows)

    def fly(self, terrain, body, who, ticks=200):
        n = 0
        while not body.on_ground and n < ticks:
            body = botmove.tick(terrain, body, who)
            n += 1
        return body, n

    def test_a_five_pixel_step_does_not_eat_the_knockback(self):
        terrain = self.bumpy()
        who = Dummy(7.5)
        start = botmove.Body(290.0, 150.0, 8.0, -12.0, on_ground=False)
        landed, ticks = self.fly(terrain, start, who)
        self.assertTrue(landed.on_ground, "%d 个 tick 还没落地" % ticks)
        self.assertGreater(landed.x - start.x, 60.0,
                           "小坎不该把水平击退清零（只飞了 %.0f）"
                           % (landed.x - start.x))

    def test_a_real_wall_still_blocks(self):
        """★ 对照：够不着的高墙照旧挡得住。"""
        terrain = self.bumpy(step=120)          # 抬高 120 —— 真的是一堵墙
        who = Dummy(7.5)
        start = botmove.Body(290.0, 150.0, 8.0, -12.0, on_ground=False)
        landed, _ticks = self.fly(terrain, start, who)
        self.assertLess(landed.x - start.x, 20.0,
                        "高墙前面不该穿过去（飞了 %.0f）" % (landed.x - start.x))

    def test_a_weak_push_still_rides_up_a_gentle_slope(self):
        """★★★ 第二轮那一条：**抬升还不到坎高**的弱击退也得走得动（§95）。

        实机 `Forest_b` 那一带是缓上坡，强度 8 的击退抬升顶点只有 1.8~3.3
        个像素，够不着前面那个 4 像素的坎 —— 光靠「升过去」永远升不过去，
        必须像走路一样**蹭上坎**。
        """
        terrain = self.bumpy(step=4)
        who = Dummy(7.5)
        # 抬升顶点 2.8²/2g = 3.3 px < 4 px 的坎
        start = botmove.Body(296.0, 150.0, 7.5, -2.8, on_ground=False)
        landed, ticks = self.fly(terrain, start, who)
        self.assertTrue(landed.on_ground, "%d 个 tick 还没落地" % ticks)
        self.assertGreater(landed.x - start.x, 12.0,
                           "升不过去的小坎该蹭上去，不是把人钉住（只飞了 %.0f）"
                           % (landed.x - start.x))

    def test_falling_along_a_cliff_is_not_hoisted_up(self):
        """★ 对照：贴着崖壁往下掉，不许被上面很远的崖顶勾上去。

        判据锚在**出发时**的脚下高度（和 `_walk_tick` 同一条），
        所以崖顶够不着就还是墙。
        """
        terrain = self.bumpy(step=120, edge=300)
        who = Dummy(7.5)
        # 左边地面在 150、右边是一堵顶在 30 的高墙；人贴着墙往下掉。
        start = botmove.Body(296.0, 100.0, 7.5, 6.0, on_ground=False)
        step = botmove.tick(terrain, start, who)
        self.assertAlmostEqual(296.0, step.x, msg="崖壁前面不该被抬上去")
        self.assertGreater(step.y, 100.0, "该继续往下掉")

    def test_the_velocity_survives_one_blocked_tick(self):
        """★ 被挡住的那一 tick **只是不挪**，速度留着 —— 升过去还要接着走。"""
        terrain = self.bumpy(step=40)
        who = Dummy(7.5)
        body = botmove.Body(290.0, 150.0, 8.0, -30.0, on_ground=False)
        first = botmove.tick(terrain, body, who)
        self.assertEqual(8.0, first.vx, "速度不许被清零")


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


class JumpPadTests(unittest.TestCase):
    """★★★ 弹跳平台（V0.3 §99）—— 用户 2026-08-29 报的「bot 站上去不弹」。

    真值全部来自 2026-08-29 那一局 `Iceria_b` 的客户端上行心跳：
    真人站在台子上那一发之后 `vy` 突然变成 −27~−31，而普通跳只有 −17~−20。
    """

    def pad_map(self, pads):
        rows = []
        for y in range(32):
            rows.append(("2" if y >= 20 else "0") * 64)
        return mapdata.MapTerrain(
            test_mapdata.make_record(rows, jump=pads))

    def test_it_launches_and_matches_the_real_shot(self):
        """★ 拿实机那一发对：`Iceria_b` 的台子 `(1743,895,-24,-395)`，
        真人站在 `(1742,904)` ⇒ 心跳报 `v=(0,−31)`。"""
        terrain = self.pad_map([[1743, 895, -24.0, -395.0]])
        body = botmove.Body(1742.0, 904.0)
        got = botmove.jump_pad_launch(terrain, body, chrprops.get(0))
        self.assertIsNotNone(got, "站在台子上必须被弹")
        self.assertFalse(got.on_ground)
        self.assertAlmostEqual(-31.15, got.vy, delta=0.2)
        self.assertAlmostEqual(-0.89, got.vx, delta=0.2)
        # ★ 比普通跳高得多 —— 这正是玩家看到的「飞得很高」。
        self.assertGreater(abs(got.vy), botmove.JUMP_SPEED * 1.4)

    def test_out_of_reach_does_nothing(self):
        """★ 实机最近的一次「没被弹」离台子 51.5 个单位。"""
        terrain = self.pad_map([[1743, 895, -24.0, -395.0]])
        for dx in (40, 52, 80):
            body = botmove.Body(1743.0 - dx, 904.0)
            self.assertIsNone(
                botmove.jump_pad_launch(terrain, body, chrprops.get(0)),
                f"离 {dx} 个单位不该被弹")

    def test_airborne_is_not_launched(self):
        """★ `0x510dfa: cmp byte [char+0x128], 0; je 跳过` —— 必须踩在地上。"""
        terrain = self.pad_map([[1743, 895, -24.0, -395.0]])
        body = botmove.Body(1743.0, 904.0, 0.0, -5.0, on_ground=False)
        self.assertIsNone(
            botmove.jump_pad_launch(terrain, body, chrprops.get(0)))

    def test_tick_launches_a_bot_that_walks_onto_it(self):
        """走过去就该被弹 —— 用户报的正是「bot 站上去不弹」。"""
        terrain = self.pad_map([[30, 19, 0.0, -400.0]])
        body = botmove.Body(10.0, 20.0)
        who = chrprops.get(0)
        for _ in range(40):
            body = botmove.tick(terrain, body, who, direction=1)
            if not body.on_ground:
                break
        self.assertFalse(body.on_ground, "走到台子上必须离地")
        self.assertLess(body.vy, -botmove.JUMP_SPEED)

    def test_no_pads_no_change(self):
        terrain = self.pad_map([])
        body = botmove.Body(30.0, 20.0)
        self.assertIsNone(
            botmove.jump_pad_launch(terrain, body, chrprops.get(0)))


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


class BottomlessLookaheadTests(unittest.TestCase):
    """★★★★★ 前瞻要覆盖**这份意图的寿命**（V0.3 §151）。

    `drop_below()` 只推一格，而崖边「下一步就踩空」的窗口只有一个走步宽。
    意图是 `BOT_DECISION_TICKS` 格产出一次的 —— 只问一格的话，接近位置的
    相位一变就整个跳过那个窗口，人一步走出崖边，等下一次决策时已经在往下掉。
    """

    def cliff(self, width=200, floor=20, height=40, edge=100):
        """`edge` 右边整条没有地面 —— 一个真正的无底洞。"""
        rows = []
        for y in range(height):
            if y < floor:
                rows.append("0" * width)
            else:
                rows.append("2" * edge + "0" * (width - edge))
        return terrain_from(rows)

    def test_one_tick_of_lookahead_misses_the_edge(self):
        """★ 病灶本身：站在离崖边一步以外时，只问一格的判据是**假**的。"""
        terrain = self.cliff()
        who = Dummy(7.0)
        far = botmove.Body(float(100 - 9), 20.0, on_ground=True)
        self.assertFalse(
            botmove.bottomless_ahead(terrain, far, who, 1, ticks=1),
            "离崖边 9、走速 7 —— 下一步还踩得着地，一格前瞻当然看不见坑")

    def test_lookahead_that_covers_the_intent_sees_it(self):
        """★★★ 同一个位置，前瞻覆盖两格就看见了。"""
        terrain = self.cliff()
        who = Dummy(7.0)
        far = botmove.Body(float(100 - 9), 20.0, on_ground=True)
        self.assertTrue(
            botmove.bottomless_ahead(terrain, far, who, 1, ticks=2),
            "两步之后就踩空了，这份意图要握两格 —— 必须现在就知道")

    def test_a_step_down_is_not_a_pit(self):
        """★ 「掉得到底」的台阶不算坑 —— 真人从一米高的台阶走下去很正常。"""
        rows = []
        for y in range(40):
            if y < 20:
                rows.append("0" * 200)
            elif y < 30:
                rows.append("2" * 100 + "0" * 100)
            else:
                rows.append("2" * 200)
        terrain = terrain_from(rows)
        who = Dummy(7.0)
        body = botmove.Body(95.0, 20.0, on_ground=True)
        self.assertFalse(botmove.bottomless_ahead(terrain, body, who, 1,
                                                  ticks=4))

    def test_a_wall_is_not_a_pit(self):
        """★ 撞墙时**不**报坑：原地不动就不会走出去，再往后推也是原地。"""
        # 右边是一堵**爬不上去**的高墙（比 `speed × CLIMB_SLOPE` = 14 高得多），
        # 墙脚外面是无底洞 —— 撞墙和坑同时成立时，报的必须是撞墙。
        rows = []
        for y in range(40):
            if y < 3:
                rows.append("0" * 200)
            elif y < 20:
                rows.append("0" * 100 + "2" * 100)
            else:
                rows.append("2" * 100 + "0" * 100)
        terrain = terrain_from(rows)
        who = Dummy(7.0)
        body = botmove.Body(96.0, 20.0, on_ground=True)
        self.assertTrue(botmove.blocked(terrain, body, who, 1))
        self.assertFalse(botmove.bottomless_ahead(terrain, body, who, 1,
                                                  ticks=4))

    def test_standing_still_is_never_a_pit(self):
        terrain = self.cliff()
        self.assertFalse(botmove.bottomless_ahead(terrain,
                                                  botmove.Body(99.0, 20.0,
                                                               on_ground=True),
                                                  Dummy(7.0), 0, ticks=4))


class RealPitCrossingTests(unittest.TestCase):
    """★★★★★ 真图上的跨坑成功率（V0.3 §151）—— 这一条钉的是**那个数**。

    `Quest02_1#Normal`（岩浆巨龙 普通）两个无底洞，逐格跑真物理：
    走速 / 冲刺 × 决策相位 0 / 1 × 190 个接近位置。

    | 前瞻 | 掉坑率 |
    |---|---|
    | 1 格（改之前） | **50.0%** |
    | 2 格（= `BOT_DECISION_TICKS`） | **0.1%** |

    ★ 变异验证就在同一个用例里：`horizon=1` 那一档必须**还是**掉一半，
      否则说明这个用例根本没测到那条路。
    """

    @classmethod
    def setUpClass(cls):
        cls.terrain = mapdata.load("Quest02_1", "Normal")
        if cls.terrain is None:
            raise unittest.SkipTest("没有 Quest02_1 的地形产物")
        cls.who = chrprops.get(0)
        cls.pits = cls.find_pits(cls.terrain)
        if not cls.pits:
            raise unittest.SkipTest("这张图上没找到无底洞")

    @staticmethod
    def find_pits(terrain, least=40):
        """连续没有任何站立面的那几段列 = 无底洞。"""
        pits = []
        x = 1
        while x < terrain.width:
            if not terrain.surfaces(x) and terrain.surfaces(x - 1):
                start = x
                while x < terrain.width and not terrain.surfaces(x):
                    x += 1
                if x < terrain.width and x - start >= least:
                    pits.append((start, x))
            x += 1
        return pits

    def cross(self, start_x, phase, fast_run, horizon, goal, limit=600):
        """照 `bot._walk_to()` 那条兜底跑一遍，返回 `'过去了' / '掉坑' / '停住'`。

        决策每 `BOT_DECISION_TICKS`（2）格一次、相位由 `phase` 定 —— 这正是
        `bot._decide()` 的节奏（座位号决定相位）。物理每格都推。
        """
        terrain, who = self.terrain, self.who
        surfaces = terrain.surfaces(start_x)
        if not surfaces:
            return None
        body = botmove.Body(float(start_x), float(surfaces[0]), on_ground=True)
        intent = (1, False, False, fast_run)
        double = False
        for tick in range(limit):
            if (tick % 2) == phase:
                if body.on_ground:
                    double = False
                    if botmove.bottomless_ahead(terrain, body, who, 1,
                                                fast_run=fast_run,
                                                ticks=horizon):
                        landing = botmove.jump_lands(terrain, body, who, 1,
                                                     fast_run=fast_run)
                        if landing is not None:
                            intent = (1, True, False, fast_run)
                        else:
                            landing = botmove.double_jump_lands(
                                terrain, body, who, 1, fast_run=fast_run)
                            if landing is None:
                                return "停住"
                            double = True
                            intent = (1, True, False, fast_run)
                    else:
                        intent = (1, False, False, fast_run)
                else:
                    intent = (1, double and botmove.at_apex(body), False,
                              fast_run)
            direction, jump, drop, fast = intent
            body = botmove.tick(terrain, body, who, direction=direction,
                                fast_run=fast, want_jump=jump)
            if jump and not body.on_ground:
                intent = (direction, False, drop, fast)
            if body.y >= terrain.height - 1:
                return "掉坑"
            if body.on_ground and body.x > goal:
                return "过去了"
        return "超时"

    def sweep(self, horizon):
        tally = {}
        for start, end in self.pits:
            for fast_run in (False, True):
                for phase in (0, 1):
                    for offset in range(10, 200):
                        got = self.cross(start - offset, phase, fast_run,
                                         horizon, end + 40)
                        if got is not None:
                            tally[got] = tally.get(got, 0) + 1
        return tally

    @staticmethod
    def rate(tally, key):
        total = sum(tally.values())
        return 0.0 if not total else 100.0 * tally.get(key, 0) / total

    def test_one_tick_of_lookahead_falls_in_half_the_time(self):
        """★★ 变异验证：把前瞻改回一格，掉坑率必须**回到一半左右**。"""
        tally = self.sweep(horizon=1)
        self.assertGreater(self.rate(tally, "掉坑"), 40.0,
                           "一格前瞻本来就该掉一半，实际 %r" % (tally,))

    def test_lookahead_of_one_decision_period_crosses_the_pits(self):
        """★★★★★ 前瞻 = 意图的寿命 ⇒ 基本不再掉进去。"""
        import bot                                             # noqa: PLC0415
        tally = self.sweep(horizon=bot.BOT_DECISION_TICKS)
        self.assertLessEqual(self.rate(tally, "掉坑"), 1.0,
                             "掉坑率该 ≤1%%，实际 %r" % (tally,))
        self.assertGreater(self.rate(tally, "过去了"), 90.0,
                           "绝大多数接近位置都该跨过去，实际 %r" % (tally,))


class SlowedPredictionTests(unittest.TestCase):
    """★★★ 预测要和**执行**用同一个 `speed_scale`（V0.3 §151）。

    被冻住（倍率 0.0）/ 踩了减速胶水（0.3）的 bot 起跳带走的是**那一刻的
    走速**（§93）。预测不带这一档的话算出来的是一条满速弧线，而真跑出来的
    是原地竖直跳 —— 于是「算着跳得过去」，实际一头栽进坑里。
    """

    def setUp(self):
        self.terrain = flat(width=400, floor=20, height=48)
        self.who = Dummy(7.0)

    def landing_of(self, scale):
        body = botmove.Body(100.0, 20.0, on_ground=True)
        return botmove.jump_lands(self.terrain, body, self.who, 1,
                                  speed_scale=scale)

    def test_the_prediction_matches_what_actually_runs(self):
        for scale in (1.0, 0.3, 0.0):
            predicted = self.landing_of(scale)
            body = botmove.Body(100.0, 20.0, on_ground=True)
            body = botmove.tick(self.terrain, body, self.who, direction=1,
                                want_jump=True, speed_scale=scale)
            for _ in range(80):
                if body.on_ground:
                    break
                body = botmove.tick(self.terrain, body, self.who, direction=1,
                                    speed_scale=scale)
            self.assertIsNotNone(predicted, "倍率 %.1f 该落得住" % scale)
            self.assertAlmostEqual(predicted.x, body.x, places=6,
                                   msg="倍率 %.1f 的预测落点和真跑的对不上"
                                       % scale)

    def test_frozen_jumps_straight_up(self):
        """★ 冻住时倍率是 0 —— 跳起来是**竖直**的，一步都不往前。"""
        self.assertAlmostEqual(100.0, self.landing_of(0.0).x, places=6)
        self.assertGreater(self.landing_of(1.0).x, 100.0)

    def test_double_jump_takes_the_scale_too(self):
        far = self.landing_of(1.0)
        body = botmove.Body(100.0, 20.0, on_ground=True)
        frozen = botmove.double_jump_lands(self.terrain, body, self.who, 1,
                                           speed_scale=0.0)
        self.assertIsNotNone(frozen)
        self.assertAlmostEqual(100.0, frozen.x, places=6)
        self.assertGreater(far.x, frozen.x)


class FitsTests(unittest.TestCase):
    """★★★★ 「碰撞体塞不塞得下」（V0.3 §152）—— 窄缝陷阱的判据。"""

    def slot(self, gap):
        """两堵墙中间留 `gap` 像素宽的一条缝，缝底有地面。"""
        width, height, floor = 120, 60, 40
        rows = []
        for y in range(height):
            if y >= floor:
                rows.append("2" * width)
            elif y >= 10:
                left = 60 - gap // 2
                rows.append("2" * left + "0" * gap + "2" * (width - left - gap))
            else:
                rows.append("0" * width)
        return terrain_from(rows)

    def test_a_hairline_crack_does_not_fit(self):
        who = chrprops.get(0)
        self.assertFalse(botmove.fits(self.slot(6), 60, 40, who))

    def test_a_wide_corridor_fits(self):
        who = chrprops.get(0)
        self.assertTrue(botmove.fits(self.slot(60), 60, 40, who))

    def test_standing_against_a_wall_is_legal(self):
        """★★ 贴着墙站是合法的 —— 一侧的富余补得上另一侧的不足。"""
        who = chrprops.get(0)
        width, height, floor = 200, 60, 40
        rows = []
        for y in range(height):
            if y >= floor:
                rows.append("2" * width)
            elif y >= 5:
                rows.append("2" * 40 + "0" * (width - 40))
            else:
                rows.append("0" * width)
        terrain = terrain_from(rows)
        self.assertTrue(botmove.fits(terrain, 41, 40, who),
                        "左边紧贴墙、右边一片开阔 —— 人站得住")

    def test_open_ground_fits(self):
        self.assertTrue(botmove.fits(flat(width=200, floor=20, height=40),
                                     100, 20, chrprops.get(0)))

    def test_no_terrain_always_fits(self):
        self.assertTrue(botmove.fits(None, 0, 0, chrprops.get(0)))


class RealTrapNodeTests(unittest.TestCase):
    """★★★★★ `Iceria03` 上那几个**只进不出**的陷阱点（V0.3 §152）。

    用户 2026-09-01 实机（`Iceria03:NewPvp`，00:19 那一局）：座位 2 在
    (1174, 864) 杵了 **58.9 秒**，紧接着座位 3 在**同一个像素**上杵了
    **13.0 秒**（从 `logs/game_003_27799.txt` 的心跳坐标反解出来的）。
    那里是一条 6 像素宽的斜裂缝，而角色身圆直径 26。
    """

    #: 全图正/反向可达差集跑出来的 8 个只进不出的点（净空 1~7 像素）。
    TRAPS = ((1174, 864), (1176, 867), (686, 1038), (694, 1050),
             (1246, 1105), (1174, 1112), (1176, 1116), (696, 1548))

    @classmethod
    def setUpClass(cls):
        cls.terrain = mapdata.load("Iceria03")
        if cls.terrain is None:
            raise unittest.SkipTest("没有 Iceria03 的地形产物")
        cls.who = chrprops.get(0)

    def test_every_known_trap_is_rejected(self):
        for x, y in self.TRAPS:
            self.assertFalse(botmove.fits(self.terrain, x, y, self.who),
                             "(%d, %d) 是缝，碰撞体塞不进去" % (x, y))

    def test_it_does_not_reject_the_whole_map(self):
        """★ 误伤面要小：绝大多数落脚点照旧可用（实测这张图 5.8% 被拒）。"""
        good = bad = 0
        for x in range(2, self.terrain.width - 2, 8):
            for y in self.terrain.surfaces(x):
                if botmove.fits(self.terrain, x, y, self.who):
                    good += 1
                else:
                    bad += 1
        total = good + bad
        self.assertGreater(total, 200, "采样太少，这个断言没意义")
        self.assertLess(100.0 * bad / total, 15.0,
                        "拒掉 %d/%d 个落脚点，过滤面太大了" % (bad, total))


if __name__ == "__main__":
    unittest.main()
