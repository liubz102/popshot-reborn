#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`server/botmove.py` 的测试 —— **人**怎么走、怎么跳、怎么掉下去（X_Mod §102 / §104 / §105：本人那台客户端）。

两层，和 `test_mapdata.py` 一个路子：

1. **合成地形**：自己造一张小图（平地 / 斜坡 / 高坎 / 悬崖 / 薄板 / 天花板），
   把客户端每一条运动规则单独钉死（地址写在每个用例里）；
2. **真产物**：`bot_mapdata/` 在的话，挑几张真图让人走上几百个 tick，
   断言他**从头到尾都站在合法的地方**（脚下是地、自己那一格是空的）。

★ 坐标口径：客户端的脚在实心第一行的**上面一行**。平地 `floor` 那一行起实心 ⇒ 脚在 `floor − 1`。
★ 图顶是实心（`0x472fe0`，出界四面都是 2），头在脚上 70：合成地形的地面都放在 y ≥ 100，不然人一动头就撞图顶。
★ 这一层不碰协议、不碰房间 —— `botmove` 是纯函数 + 地形，就该这么测。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import botmove                                                 # noqa: E402
import mapdata                                                 # noqa: E402
import chrprops                                                # noqa: E402
import test_mapdata                                            # noqa: E402
from test_mapdata import make_record                           # noqa: E402

f32 = botmove._f32


class Dummy(object):
    """一个只有 `speed` 的假角色（`chrprops.Character` 的最小替身）。"""

    def __init__(self, speed=4.0):
        self.speed = speed


def terrain_from(rows, **extra):
    return mapdata.MapTerrain(make_record(rows, **extra))


def flat(width=64, floor=100, height=120):
    """一张平地：`floor` 那一行起整条实心，上面全空。脚站在 `floor − 1`。"""
    rows = []
    for y in range(height):
        rows.append(("2" if y >= floor else "0") * width)
    return terrain_from(rows)


def columns(heights, height=140):
    """`heights[x]` = 第 x 列实心从哪一行起（越小越高）。"""
    width = len(heights)
    return terrain_from(["".join("2" if y >= heights[x] else "0"
                                 for x in range(width))
                         for y in range(height)])


def run_air(terrain, body, who, ticks=200, **keys):
    """一直推到落地（或者推满），返回 `(落地那一格, 推了几格, 最高点 y)`。"""
    top = body.y
    n = 0
    while not body.on_ground and n < ticks:
        body = botmove.tick(terrain, body, who, **keys)
        top = min(top, body.y)
        n += 1
    return body, n, top


class WalkSpeedTests(unittest.TestCase):
    """启发式那一档（寻路代价、挑站位）：三档倍率全是原版常量。"""

    def test_base_is_chrspeed(self):
        self.assertEqual(7.0, botmove.walk_speed(Dummy(7.0)))

    def test_fast_run_is_one_and_a_half(self):
        self.assertAlmostEqual(10.5, botmove.walk_speed(Dummy(7.0),
                                                        fast_run=True))

    def test_crouch_is_a_third(self):
        self.assertAlmostEqual(7.0 * botmove.CROUCH_FACTOR,
                               botmove.walk_speed(Dummy(7.0), crouched=True))

    def test_a_character_without_speed_still_walks(self):
        """产物缺字段时不许变成 0 —— 那样 bot 会一动不动而且没人知道为什么。"""
        self.assertTrue(botmove.walk_speed(object()) > 0)


class WalkDistanceTests(unittest.TestCase):
    """一帧交给 `0x50d9a7` 的路程（`0x5074ef` ~ `0x507676`），逐步 f32。"""

    def test_base_is_s_times_direction(self):
        self.assertEqual(7.0, botmove.walk_distance(Dummy(7.0), 1))
        self.assertEqual(-7.0, botmove.walk_distance(Dummy(7.0), -1))

    def test_fast_run_and_crouch(self):
        self.assertEqual(10.5, botmove.walk_distance(Dummy(7.0), 1, fast_run=True))
        self.assertEqual(f32(7.0 * f32(1.0 / 3.0)),
                         botmove.walk_distance(Dummy(7.0), 1, crouched=True))

    def test_equipment_bonus_is_a_percentage(self):
        """装备走速（`GetEquipBonus` 第 4 格）：× (100 + x) × 0.01f。"""
        got = botmove.walk_distance(Dummy(7.0), 1, bonus=10)
        self.assertEqual(f32(7.0 * f32(110 * f32(0.01))), got)
        self.assertAlmostEqual(7.7, got, places=5)

    def test_status_scale_multiplies_s(self):
        """S = 状态倍率 × ChrSpeed（`vft+0x128`）：减速 0.3f。"""
        self.assertEqual(f32(7.0 * 0.3),
                         botmove.walk_distance(Dummy(7.0), 1, scale=0.3))


class FlatGroundTests(unittest.TestCase):

    def setUp(self):
        self.t = flat()
        self.who = Dummy(4.0)
        self.body = botmove.Body(30.0, 99.0)

    def test_standing_still_does_not_move(self):
        self.assertEqual(self.body, botmove.tick(self.t, self.body, self.who))

    def test_one_tick_walks_the_whole_distance_column_by_column(self):
        step = botmove.tick(self.t, self.body, self.who, direction=1)
        self.assertEqual((34.0, 99.0), (step.x, step.y))
        self.assertTrue(step.on_ground)
        self.assertEqual(0.0, step.rest)

    def test_left_is_negative(self):
        step = botmove.tick(self.t, self.body, self.who, direction=-1)
        self.assertEqual(26.0, step.x)

    def test_on_the_ground_the_velocities_stay_zero(self):
        """★ 心跳的口径（§35）：踩地时速度就是 0（`0x50d42d` 每帧清），不能从位移反推。"""
        step = botmove.advance(self.t, self.body, self.who, 4, direction=1)
        self.assertEqual((0.0, 0.0), (step.vx, step.vy))

    def test_the_remainder_carries_to_the_next_tick(self):
        """★★ 余量 `[+0x130]` 跨帧保留：4.5 的走速是 4、5、4、5 一列一列走（`0x50d9d3`）。"""
        body, xs = self.body, []
        for _ in range(4):
            body = botmove.tick(self.t, body, Dummy(4.5), direction=1)
            xs.append(body.x)
        self.assertEqual([34.0, 39.0, 43.0, 48.0], xs)

    def test_the_map_edge_is_a_wall(self):
        """出界 = 实心（`0x472fe0`）—— 走不出去，余量清零。"""
        body = botmove.Body(1.0, 99.0)
        for _ in range(10):
            body = botmove.tick(self.t, body, self.who, direction=-1)
        self.assertEqual(0.0, body.x)
        self.assertEqual(0.0, body.rest)

    def test_a_foot_inside_the_ground_steps_out_on_the_first_column(self):
        """脚埋在实心里（服务端硬置出来的旧口径）：下一列脚那一行不空 ⇒ 往上找空格，一步爬出来。"""
        step = botmove.tick(self.t, botmove.Body(30.0, 100.0), self.who,
                            direction=1)
        self.assertEqual((33.0, 99.0), (step.x, step.y))


class ClientWalkTests(unittest.TestCase):
    """`0x50d9a7`：1 px 一列、上坎 ≤ 20、下坎 ≤ 10、每步花 √(1+dy²)、越过 0 就不走。"""

    def test_a_slope_costs_the_diagonal(self):
        """45° 上坡每列 √2：4 的余量先走 2 列平地，第 3 列上坡花 1.414，剩 0.586 停。"""
        heights = [100 - (x - 10) if x > 10 else 100 for x in range(40)]
        t = columns([max(82, h) for h in heights])
        step = botmove.tick(t, botmove.Body(8.0, 99.0), Dummy(4.0), direction=1)
        self.assertEqual((11.0, 98.0), (step.x, step.y))
        self.assertAlmostEqual(4.0 - 2.0 - 2 ** 0.5, step.rest, places=5)
        self.assertTrue(step.on_ground)

    def test_a_twenty_pixel_step_needs_the_remainder_to_add_up(self):
        """20 px 的坎（上限）花 √401 ≈ 20.02：余量攒到 24 那一帧才迈上去 —— 不是墙。"""
        t = columns([120 if x < 20 else 100 for x in range(40)])
        who = Dummy(4.0)
        body = botmove.Body(19.0, 119.0)
        seen = []
        for _ in range(6):
            body = botmove.tick(t, body, who, direction=1)
            seen.append((body.x, body.y))
        # 第 6 帧余量 24：迈上坎（−20.025）后剩 3.975，平地再走 3 列。
        self.assertEqual([(19.0, 119.0)] * 5 + [(23.0, 99.0)], seen)
        self.assertFalse(botmove.blocked(t, botmove.Body(19.0, 119.0), who, 1))

    def test_twenty_one_pixels_is_a_wall(self):
        """往上找满 20 格还是实心 = 撞墙：余量清零、原地不动（`0x50db3a` / `0x50dd47`）。"""
        t = columns([121 if x < 20 else 100 for x in range(40)])
        who = Dummy(4.0)
        body = botmove.Body(19.0, 120.0)
        step = botmove.tick(t, body, who, direction=1)
        self.assertEqual((19.0, 120.0, 0.0), (step.x, step.y, step.rest))
        self.assertTrue(botmove.blocked(t, body, who, 1))
        self.assertFalse(botmove.blocked(t, body, who, -1))

    def test_down_steps_follow_the_ground(self):
        """往下找 1..10 格第一格非空 i ⇒ 这一步 `(±1, i−1)`：脚最多一列落 9 px 还贴着地。"""
        t = columns([100 if x < 20 else 109 for x in range(40)])
        step, seen = botmove.Body(18.0, 99.0), []
        for _ in range(3):
            step = botmove.tick(t, step, Dummy(4.0), direction=1)
            self.assertTrue(step.on_ground)
            seen.append((step.x, step.y))
        # 下坎那一步同样花 √(1+9²) ≈ 9.055：余量 3、7 都不够，第三帧 11 才迈下去、剩 1.945 再走一列。
        self.assertEqual([(19.0, 99.0), (19.0, 99.0), (21.0, 108.0)], seen)
        t = columns([100 if x < 20 else 110 for x in range(40)])
        step = botmove.tick(t, botmove.Body(18.0, 99.0), Dummy(4.0),
                            direction=1)
        self.assertFalse(step.on_ground, "落 10 px 就找不到了 = 走出崖边")

    def test_walking_off_a_ledge_falls_straight_down(self):
        """★★ 下坎 > 10 = 走出崖边：悬空那几列水平走完、同一帧腾空一步，**vx = 0**
        （上次落地上了锁、没松过键 ⇒ 没有空中操控，X_Mod §102）。"""
        t = columns([100 if x < 20 else 115 for x in range(40)])
        who = Dummy(4.0)
        body = botmove.Body(18.0, 99.0)
        self.assertTrue(botmove.leaves_ground(t, body, who, 1))
        step = botmove.tick(t, body, who, direction=1)
        self.assertFalse(step.on_ground)
        self.assertEqual(22.0, step.x)
        self.assertEqual(0.0, step.reported_vx)
        self.assertEqual(f32(99.0 + botmove.G32), step.y)
        landed, _n, _top = run_air(t, step, who, direction=1)
        self.assertTrue(landed.on_ground)
        self.assertEqual((22.0, 114.0), (landed.x, landed.y), "按着键也竖直掉（锁着）")

    def test_drop_below_measures_the_fall(self):
        t = columns([100 if x < 20 else 115 for x in range(40)])
        self.assertEqual(15.0, botmove.drop_below(t, botmove.Body(18.0, 99.0),
                                                  Dummy(4.0), 1))

    def test_a_bottomless_pit_reports_none(self):
        """★ 掉到图底那一圈出界实心上 = 掉出世界（`CheckFallDown`），不算落地。"""
        rows = [("2" * 20 + "0" * 20) if y >= 100 else "0" * 40
                for y in range(120)]
        t = terrain_from(rows)
        self.assertIsNone(botmove.drop_below(t, botmove.Body(18.0, 99.0),
                                             Dummy(4.0), 1))

    def test_standing_still_never_counts_as_blocked(self):
        t = flat()
        body = botmove.Body(30.0, 99.0)
        self.assertFalse(botmove.blocked(t, body, Dummy(4.0), 0))
        self.assertFalse(botmove.leaves_ground(t, body, Dummy(4.0), 0))

    def test_a_wall_behind_a_ledge_is_reached_by_walking_off(self):
        """★ 檐子两列宽、外面是空的、再往前一堵墙（V0.3 §177）：逐列走到崖口就踩空，不是原地不动。"""
        rows = []
        for y in range(140):
            row = []
            for x in range(40):
                if 20 <= x <= 21 and y == 110:
                    row.append("2")          # 檐子：两列宽、一像素厚
                elif 24 <= x <= 27 and y >= 0:
                    row.append("2")          # 通天高墙
                elif y >= 135:
                    row.append("2")          # 谷底
                else:
                    row.append("0")
            rows.append("".join(row))
        t = terrain_from(rows)
        who = Dummy(4.0)
        body = botmove.Body(21.0, 109.0)
        step = botmove.tick(t, body, who, direction=1)
        self.assertFalse(step.on_ground, "檐口外就是空的 —— 该踩空")
        self.assertEqual(23.0, step.x, "两列悬空照走，第三列撞墙停")
        self.assertFalse(botmove.blocked(t, body, who, 1))
        landed = botmove.settle(t, step, who)
        self.assertTrue(landed.on_ground)
        self.assertEqual(134.0, landed.y)


class DropThroughTests(unittest.TestCase):
    """按 ↓：这一帧末 `[+0x518] = 8`，下一帧起白线不挡（`0x516207` / `0x4fe329`）。"""

    def one_way(self, below=None):
        rows = []
        for y in range(120):
            if y == 90:
                rows.append("1" * 48)
            elif below is not None and 91 <= y < 100:
                rows.append(below * 48)
            elif y >= 105:
                rows.append("2" * 48)
            else:
                rows.append("0" * 48)
        return terrain_from(rows)

    def test_the_press_frame_itself_does_not_move(self):
        t = self.one_way()
        body = botmove.tick(t, botmove.Body(20.0, 89.0), Dummy(4.0),
                            want_drop=True)
        self.assertTrue(body.on_ground)
        self.assertEqual(botmove.DROP_HOLD_FRAMES, body.drop)

    def test_the_next_frame_falls_through_to_the_floor(self):
        t = self.one_way()
        who = Dummy(4.0)
        body = botmove.tick(t, botmove.Body(20.0, 89.0), who, want_drop=True)
        body = botmove.tick(t, body, who)
        self.assertFalse(body.on_ground)
        landed = botmove.settle(t, body, who, ticks=80)
        self.assertTrue(landed.on_ground)
        self.assertEqual(104.0, landed.y)

    def test_down_does_not_pass_through_solid_ground(self):
        t = self.one_way()
        who = Dummy(4.0)
        body = botmove.Body(20.0, 104.0)
        for _ in range(3):
            body = botmove.tick(t, body, who, want_drop=True)
        self.assertTrue(body.on_ground)
        self.assertEqual(104.0, body.y)

    def test_ice_right_under_the_line_catches_the_feet(self):
        """白线底下紧贴着冰（V0.3 §136，`Iceria00` 的窟窿）：脚沉进白线那一行、踩在冰上，进不了冰。"""
        t = self.one_way(below="3")
        who = Dummy(4.0)
        body = botmove.tick(t, botmove.Body(20.0, 89.0), who, want_drop=True)
        for _ in range(6):
            body = botmove.tick(t, body, who)
        self.assertTrue(body.on_ground)
        self.assertEqual(90.0, body.y)


class JumpTests(unittest.TestCase):
    """起跳 `0x501d57`：vy = −√(2g·h)（h = 180 / 240）、vx = ±¼S、当场离地、排在一帧最后。"""

    def setUp(self):
        self.t = flat(width=400, floor=300, height=340)
        self.who = Dummy(4.0)
        self.body = botmove.Body(100.0, 299.0)

    def test_launch_speeds_are_the_client_floats(self):
        self.assertAlmostEqual(20.784611, botmove.JUMP_SPEED, places=5)
        self.assertEqual(24.0, botmove.DOUBLE_JUMP_SPEED)
        self.assertEqual(f32(botmove.JUMP_SPEED), botmove.JUMP_SPEED)

    def test_rise_timer_lengths(self):
        """`ftol(|v0 / g|)` 在 24 位精度下 = 17 / 20 ⇒ 之后 16 / 19 帧在计时器里。"""
        self.assertEqual(16, botmove.rise_ticks(botmove.JUMP_SPEED))
        self.assertEqual(19, botmove.rise_ticks(botmove.DOUBLE_JUMP_SPEED))

    def test_apex_matches_the_closed_form(self):
        self.assertAlmostEqual(180.0, botmove.jump_apex(), places=3)

    def test_a_standing_jump_rises_about_180_and_comes_back(self):
        body = botmove.jump(self.body)
        landed, ticks, top = run_air(self.t, body, self.who)
        self.assertTrue(landed.on_ground, "跳起来总得落回地面")
        self.assertEqual(self.body.y, landed.y)
        # 离散的：先加重力再挪 ⇒ Σ(v0 − 1.2k)（k = 1..17）= 169.7，闭式解 180 是上界。
        self.assertTrue(165.0 <= self.body.y - top <= 181.0,
                        "顶点高 %.1f" % (self.body.y - top))
        self.assertTrue(33 <= ticks <= 37, "滞空 %d 个 tick" % ticks)

    def test_jumping_in_the_air_does_nothing(self):
        body = botmove.jump(self.body)
        self.assertIs(body, botmove.jump(body))

    def test_the_takeoff_frame_walks_first_then_leaves(self):
        """★★ 输入在对象 tick 之后（`0x490687`）：这一帧照常走完、帧末才起跳；vx = ¼S。"""
        body = botmove.tick(self.t, self.body, self.who, direction=1,
                            want_jump=True)
        self.assertEqual((104.0, 299.0), (body.x, body.y))
        self.assertFalse(body.on_ground)
        self.assertEqual(1.0, body.vx)
        self.assertEqual(-botmove.JUMP_SPEED, body.vy)
        self.assertEqual(botmove.rise_ticks(botmove.JUMP_SPEED), body.rise)

    def test_air_control_ramps_up_while_the_key_is_held(self):
        """★★ 空中操控（`0x51558f`）：按住右键每帧 +步长（2.0、1.98、1.96 …），钳到 ±S。"""
        body = botmove.tick(self.t, self.body, Dummy(7.0), direction=1,
                            want_jump=True)
        seen = []
        for _ in range(5):
            body = botmove.tick(self.t, body, Dummy(7.0), direction=1)
            seen.append(round(body.reported_vx, 4))
        self.assertEqual([3.75, 5.73, 7.69, 8.75, 8.75], seen)

    def test_without_the_key_the_speed_stays_a_quarter(self):
        body = botmove.tick(self.t, self.body, self.who, direction=1,
                            want_jump=True)
        moved = botmove.tick(self.t, body, self.who)
        self.assertEqual(1.0, moved.reported_vx)
        self.assertEqual(1.0, moved.x - body.x)

    def test_a_standing_jump_can_steer_in_the_air(self):
        """站着起跳是竖直的（vx = 0），起跳解了锁 ⇒ 空中按键照样有操控。"""
        body = botmove.tick(self.t, self.body, self.who, want_jump=True)
        self.assertEqual(0.0, body.vx)
        moved = botmove.tick(self.t, body, self.who, direction=-1)
        self.assertEqual(-2.0, moved.reported_vx)

    def test_want_jump_only_fires_on_the_first_tick(self):
        body = botmove.advance(self.t, self.body, self.who, 8,
                               direction=1, want_jump=True)
        self.assertFalse(body.on_ground)
        self.assertLess(body.y, self.body.y)

    def test_the_double_jump_resets_the_speed_and_the_control(self):
        """第二段：vy 重新置成 −24、vx 按键重算 ¼S、操控复位（`0x4face2`）。"""
        body = botmove.tick(self.t, self.body, Dummy(7.0), direction=1,
                            want_jump=True)
        for _ in range(5):
            body = botmove.tick(self.t, body, Dummy(7.0), direction=1)
        again = botmove.tick(self.t, body, Dummy(7.0), direction=1,
                             want_jump=True)
        self.assertEqual(-24.0, again.vy)
        self.assertEqual(1.75, again.reported_vx)
        self.assertTrue(again.air_jumped)
        self.assertIs(again, botmove.double_jump(again))

    def test_a_wall_beside_the_feet_zeroes_the_takeoff_speed(self):
        """侧边那一格（x±1, y）是 2/3 ⇒ vx = 0（`0x501ecc`）。"""
        t = columns([300 if x != 101 else 200 for x in range(200)], height=340)
        body = botmove.takeoff(t, self.body, self.who, 1)
        self.assertEqual(0.0, body.vx)
        self.assertEqual(-1.0, botmove.takeoff(t, self.body, self.who, -1).vx)


class RiseTimerAndCollisionTests(unittest.TestCase):
    """撞上了**这一步位置不动、只改速度**（`0x502df4`）：计时器在跑 ⇒ ×0.75 / vy 0；否则落地 / 反射 + ×0.3 + 上锁。"""

    def slab(self, rows_of_slab, floor=250, width=60, height=270):
        rows = []
        for y in range(height):
            solid = y >= floor or y in rows_of_slab
            rows.append(("2" if solid else "0") * width)
        return terrain_from(rows)

    def test_a_ceiling_during_the_rise_bumps_and_stops_the_timer(self):
        t = self.slab((150, 151))
        who = Dummy(4.0)
        body = botmove.jump(botmove.Body(20.0, 249.0), 4.0)
        bumped = None
        for _ in range(30):
            result = botmove.frame(t, body, who)
            if result.outcome == botmove.BUMPED:
                bumped = (body, result.body)
                break
            body = result.body
        self.assertIsNotNone(bumped, "头该撞上板")
        before, after = bumped
        self.assertEqual((before.x, before.y), (after.x, after.y), "撞上那一步位置不动")
        self.assertEqual(0.0, after.vy)
        self.assertEqual(f32(4.0 * 0.75), after.vx)
        self.assertEqual(0, after.rise, "停表")
        self.assertFalse(after.on_ground)
        self.assertFalse(after.ctl_lock, "计时器那一支不上锁")

    def test_a_thin_ceiling_is_not_tunnelled_through(self):
        """一个 tick 跨过整块板也得撞上（整数 DDA 逐格扫，V0.3 §169）。"""
        t = self.slab((160, 161))
        body, _n, top = run_air(t, botmove.jump(botmove.Body(20.0, 249.0)),
                                Dummy(4.0))
        self.assertGreaterEqual(top, 162.0 + 70.0, "头穿过了薄板（脚升到 %.1f）" % top)
        self.assertTrue(body.on_ground)

    def test_the_head_is_what_hits_a_low_overhang(self):
        """头圆前沿在脚上 70（腿 12 / 身 13 / 头 10）：板底 205 ⇒ 脚最高到 276。"""
        rows = []
        for y in range(400):
            rows.append(("2" if (y >= 300 or 200 <= y <= 205) else "0") * 200)
        t = terrain_from(rows)
        body, _n, top = run_air(t, botmove.jump(botmove.Body(100.0, 299.0)),
                                chrprops.get(1))
        # 头探针到 205 那一步撞上 ⇒ 那一帧**位置不动**：最高就停在上一帧 299 − 19.585。
        self.assertEqual(f32(299.0 + f32(-botmove.JUMP_SPEED + botmove.G32)), top)
        self.assertGreater(top - 70.0, 205.0)
        self.assertTrue(body.on_ground)

    def test_the_map_top_blocks_characters(self):
        """★ 出界四面都是 2（`0x472fe0`）：头碰到 y < 0 就被挡（X_Mod §105 订正 V0.3 §192）。"""
        t = flat(width=200, floor=300, height=340)
        body = botmove.Body(100.0, 120.0, 0.0, -16.0, on_ground=False)
        _landed, _n, top = run_air(t, body, chrprops.get(1))
        self.assertGreaterEqual(top, 70.0)

    def test_a_one_way_platform_can_be_jumped_through_and_stood_on(self):
        """值 1 的薄板只挡**往下走的脚底**（位集 `0x737ebc`）：往上穿过去、落下来踩在它上面一行。"""
        rows = []
        for y in range(144):
            if y >= 120:
                rows.append("2" * 40)
            elif y == 100:
                rows.append("1" * 40)
            else:
                rows.append("0" * 40)
        t = terrain_from(rows)
        body, _n, top = run_air(t, botmove.jump(botmove.Body(20.0, 119.0)),
                                Dummy(4.0))
        self.assertLess(top, 100.0)
        self.assertEqual(99.0, body.y)

    def test_landing_after_the_timer_locks_the_control(self):
        """落地走 `0x50efd2`，之后 `[+0x5d5] = 1`：踩地的身体都是上锁的。"""
        t = flat(width=200, floor=300, height=340)
        body = botmove.tick(t, botmove.Body(100.0, 299.0), Dummy(4.0),
                            direction=1, want_jump=True)
        self.assertFalse(body.ctl_lock)
        landed, _n, _top = run_air(t, body, Dummy(4.0), direction=1)
        self.assertTrue(landed.on_ground)
        self.assertTrue(landed.ctl_lock)

    def test_too_fast_bounces_instead_of_landing(self):
        """|v| > 35（vft+0xa4）撞地不落地、按 7×7 投票反射（切向 ×0.5、法向 ×−0.2），再 vx × 0.3。"""
        t = flat(width=200, floor=300, height=340)
        body = botmove.Body(100.0, 290.0, 0.0, 40.0, on_ground=False)
        result = botmove.frame(t, body, Dummy(4.0), direction=1)   # 按着键：锁不会被松键清掉
        self.assertEqual(botmove.BOUNCED, result.outcome)
        self.assertEqual((100.0, 290.0), (result.body.x, result.body.y))
        self.assertLess(result.body.vy, 0.0)
        self.assertTrue(result.body.ctl_lock)

    def test_the_sweep_never_leaves_the_feet_inside_a_slab(self):
        t = self.slab((150, 151, 152, 153, 154, 155))
        who = Dummy(4.0)
        for vy in range(-24, 0):
            body = botmove.Body(20.0, 249.0, 0.0, float(vy), on_ground=False)
            for _ in range(40):
                body = botmove.tick(t, body, who)
                self.assertFalse(
                    150 <= int(body.y) - 70 <= 155 or 150 <= int(body.y) <= 155,
                    "vy=%d 时脚停在了板里 (%.1f, %.1f)" % (vy, body.x, body.y))
                if body.on_ground:
                    break


class AirControlLockTests(unittest.TestCase):
    """锁 `[+0x5d5]`：落地置 1（`0x502f93`），起跳（`0x501f01`）或腾空时松开左右键（`0x515f67`）才清。"""

    def setUp(self):
        self.t = columns([100 if x < 20 else 180 for x in range(80)], height=200)
        self.who = Dummy(4.0)

    def test_walking_off_while_holding_the_key_has_no_control(self):
        body = botmove.tick(self.t, botmove.Body(18.0, 99.0), self.who,
                            direction=1)
        for _ in range(5):
            body = botmove.tick(self.t, body, self.who, direction=1)
        self.assertEqual(0.0, body.reported_vx)

    def test_releasing_the_key_in_the_air_unlocks_it(self):
        body = botmove.tick(self.t, botmove.Body(18.0, 99.0), self.who,
                            direction=1)
        body = botmove.tick(self.t, body, self.who)          # 松开一帧
        self.assertFalse(body.ctl_lock)
        body = botmove.tick(self.t, body, self.who, direction=1)
        self.assertEqual(2.0, body.reported_vx)


class JumpPadTests(unittest.TestCase):
    """★★★ 弹跳台（V0.3 §99 / X_Mod §104）。"""

    def pad_map(self, pads, floor=500, width=64):
        rows = [("2" if y >= floor else "0") * width for y in range(floor + 20)]
        return terrain_from(rows, jump=pads)

    def test_it_matches_the_real_shot(self):
        """实机 `Iceria_b` 的台子 `(1743,895,-24,-395)`，真人脚在 904 ⇒ 心跳报 `v=(0,−31)`；
        偏置减的是 0.25 × (2·腿 + 身)（`0x510e68`）。"""
        terrain = self.pad_map([[1743, 895, -24.0, -395.0]])
        got = botmove.jump_pad_launch(terrain, botmove.Body(1742.0, 904.0),
                                      chrprops.get(0))
        self.assertIsNotNone(got, "站在台子上必须被弹")
        self.assertTrue(got.pad, "台子不清踩地位")
        self.assertTrue(got.reported_on_ground)
        self.assertEqual((0, -31), (int(got.vx), int(got.vy)))
        legs, body = chrprops.get(0).size_legs, chrprops.get(0).size_body
        self.assertEqual(botmove.pad_velocity((1743, 895, -24.0, -395.0),
                                              1742.0, 904.0, chrprops.get(0)),
                         (got.vx, got.vy))
        self.assertEqual(9.25, 0.25 * (2 * legs + body))

    def test_out_of_reach_does_nothing(self):
        terrain = self.pad_map([[1743, 895, -24.0, -395.0]])
        for dx in (40, 52, 80):
            body = botmove.Body(1743.0 - dx, 904.0)
            self.assertIsNone(
                botmove.jump_pad_launch(terrain, body, chrprops.get(0)),
                f"离 {dx} 个单位不该被弹")

    def test_airborne_is_not_launched(self):
        terrain = self.pad_map([[1743, 895, -24.0, -395.0]])
        body = botmove.Body(1743.0, 904.0, 0.0, -5.0, on_ground=False)
        self.assertIsNone(
            botmove.jump_pad_launch(terrain, body, chrprops.get(0)))

    def test_left_right_or_down_held_blocks_it(self):
        """★★ `0x510dd3` / `0x510de0` / `0x510ded`：←/→/↓ 任一按着就不弹；↑ 不拦。"""
        terrain = self.pad_map([[30, 499, 0.0, -300.0]])
        body = botmove.Body(30.0, 499.0)
        for keys in (botmove.PAD_BLOCK_LEFT, botmove.PAD_BLOCK_RIGHT,
                     botmove.PAD_BLOCK_DOWN):
            self.assertIsNone(botmove.jump_pad_launch(terrain, body,
                                                      chrprops.get(0), keys))
        self.assertIsNotNone(botmove.jump_pad_launch(terrain, body,
                                                     chrprops.get(0), 0x02))

    def test_walking_over_it_holding_the_key_is_not_launched(self):
        terrain = self.pad_map([[30, 499, 0.0, -300.0]])
        who = chrprops.get(0)
        body = botmove.Body(10.0, 499.0)
        for _ in range(8):
            body = botmove.tick(terrain, body, who, direction=1)
            self.assertTrue(body.on_ground, "按着 → 走过台子不弹")

    def test_releasing_on_it_launches_and_moves_twice_next_frame(self):
        """松手那一帧台子写速度（踩地位不清）；下一帧先按踩地分支挪 v、脚下空了再腾空一步。"""
        terrain = self.pad_map([[30, 499, 0.0, -300.0]])
        who = chrprops.get(0)
        body = botmove.Body(30.0, 499.0)
        launched = botmove.frame(terrain, body, who)
        self.assertTrue(launched.padded)
        pad = launched.body
        self.assertTrue(pad.pad)
        self.assertEqual((30.0, 499.0), (pad.x, pad.y))
        nxt = botmove.tick(terrain, pad, who)
        self.assertFalse(nxt.on_ground or nxt.pad)
        y1 = f32(499.0 + pad.vy)
        vy2 = f32(pad.vy + botmove.G32)
        self.assertEqual(f32(y1 + vy2), nxt.y)
        self.assertEqual(vy2, nxt.vy)

    def test_no_pads_no_change(self):
        terrain = self.pad_map([])
        self.assertIsNone(botmove.jump_pad_launch(terrain, botmove.Body(30.0, 499.0),
                                                  chrprops.get(0)))


class BodyTests(unittest.TestCase):

    def test_on_the_ground_velocities_are_forced_to_zero(self):
        body = botmove.Body(1.0, 2.0, vx=9.0, vy=-9.0, on_ground=True)
        self.assertEqual((0.0, 0.0), (body.vx, body.vy))

    def test_in_the_air_they_are_kept(self):
        body = botmove.Body(1.0, 2.0, vx=9.0, vy=-9.0, on_ground=False)
        self.assertEqual((9.0, -9.0), (body.vx, body.vy))

    def test_standing_bodies_are_locked_flying_ones_are_not(self):
        self.assertTrue(botmove.Body(1.0, 2.0).ctl_lock)
        self.assertFalse(botmove.Body(1.0, 2.0, on_ground=False).ctl_lock)

    def test_reported_vx_is_base_plus_control(self):
        body = botmove.Body(1.0, 2.0, vx=1.75, vy=-3.0, on_ground=False,
                            ctl=2.0)
        self.assertEqual(3.75, body.reported_vx)

    def test_ticks_for_is_at_least_one(self):
        self.assertEqual(1, botmove.ticks_for(0.0))
        self.assertEqual(4, botmove.ticks_for(0.128))

    def test_no_terrain_means_no_movement(self):
        """地图数据缺失时**原地不动**，不是乱走（fail-safe）。"""
        body = botmove.Body(5.0, 5.0)
        self.assertEqual(body, botmove.tick(None, body, Dummy(), direction=1))

    def test_frozen_ignores_every_key(self):
        """冻住（`0x515639` 跳过读键）：不走、不跳、不按 ↓。"""
        t = flat()
        body = botmove.Body(30.0, 99.0)
        got = botmove.tick(t, body, Dummy(), direction=1, want_jump=True,
                           want_drop=True, frozen=True)
        self.assertEqual(body, got)


class RealMapTests(unittest.TestCase):
    """真产物在的话再跑：在真图上走几百个 tick，人必须一直站得住。"""

    @classmethod
    def setUpClass(cls):
        cls.store = mapdata._Store()
        cls.names = cls.store.available()
        if not cls.names:
            raise unittest.SkipTest(
                "没有 bot_mapdata/ 产物，先跑 tools\\update-gamedata.bat")

    def walkable_start(self, terrain):
        """找一个真站得住的起点：某列最下面那个站立面的上一行。"""
        for x in range(terrain.width // 4, terrain.width, 7):
            surfaces = terrain.surfaces(x)
            if surfaces:
                return botmove.Body(float(x), float(surfaces[-1] - 1))
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
                            0, terrain.cell(int(walker.x), int(walker.y) + 1),
                            "%s: 脚下是空的 (%.0f, %.0f)"
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
        landed = botmove.settle(terrain, botmove.jump(body), who, ticks=400)
        self.assertTrue(landed.on_ground)


class BottomlessLookaheadTests(unittest.TestCase):
    """★★★★★ 前瞻要覆盖**这份意图的寿命**（V0.3 §151）。"""

    def cliff(self, width=200, floor=100, height=120, edge=100):
        """`edge` 右边整条没有地面 —— 一个真正的无底洞。"""
        rows = []
        for y in range(height):
            if y < floor:
                rows.append("0" * width)
            else:
                rows.append("2" * edge + "0" * (width - edge))
        return terrain_from(rows)

    def test_one_tick_of_lookahead_misses_the_edge(self):
        terrain = self.cliff()
        far = botmove.Body(float(100 - 9), 99.0)
        self.assertFalse(
            botmove.bottomless_ahead(terrain, far, Dummy(7.0), 1, ticks=1),
            "离崖边 9、走速 7 —— 下一步还踩得着地，一格前瞻当然看不见坑")

    def test_lookahead_that_covers_the_intent_sees_it(self):
        terrain = self.cliff()
        far = botmove.Body(float(100 - 9), 99.0)
        self.assertTrue(
            botmove.bottomless_ahead(terrain, far, Dummy(7.0), 1, ticks=2),
            "两步之后就踩空了，这份意图要握两格 —— 必须现在就知道")

    def test_a_step_down_is_not_a_pit(self):
        rows = []
        for y in range(120):
            if y < 100:
                rows.append("0" * 200)
            elif y < 108:
                rows.append("2" * 100 + "0" * 100)
            else:
                rows.append("2" * 200)
        terrain = terrain_from(rows)
        body = botmove.Body(95.0, 99.0)
        self.assertFalse(botmove.bottomless_ahead(terrain, body, Dummy(7.0), 1,
                                                  ticks=4))

    def test_a_wall_is_not_a_pit(self):
        """撞墙时**不**报坑：通天墙（往上 20 格找不到空）脚下外面是无底洞。"""
        rows = []
        for y in range(120):
            if y < 100:
                rows.append("0" * 100 + "2" * 100)
            else:
                rows.append("2" * 100 + "0" * 100)
        terrain = terrain_from(rows)
        body = botmove.Body(99.0, 99.0)
        self.assertTrue(botmove.blocked(terrain, body, Dummy(7.0), 1))
        self.assertFalse(botmove.bottomless_ahead(terrain, body, Dummy(7.0), 1,
                                                  ticks=4))

    def test_standing_still_is_never_a_pit(self):
        terrain = self.cliff()
        self.assertFalse(botmove.bottomless_ahead(
            terrain, botmove.Body(99.0, 99.0), Dummy(7.0), 0, ticks=4))


class RealPitCrossingTests(unittest.TestCase):
    """★★★★★ 真图上的跨坑兜底（V0.3 §151）—— `Quest02_1#Normal` 两个无底洞，逐格跑真物理。

    这里测的是 A\\* 找不到路时 `bot._walk_to()` 那层**兜底**（可达图里两个坑都有跨过去的边）。
    客户端的空中水平速度是 ¼S 起步、按住才攒到 1¼S（X_Mod §102），二段跳在顶点按会撞顶减速，
    兜底能跨过去的接近位置比旧模型少；**钉死的是安全那一条**：前瞻覆盖意图寿命 ⇒ 几乎不掉坑。

    | 前瞻 | 掉坑率 |
    |---|---|
    | 1 格 | 一半上下（变异对照） |
    | 2 格（= `BOT_DECISION_TICKS`） | ≤ 1% |
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
        """照 `bot._walk_to()` 那条兜底跑一遍，返回 `'过去了' / '掉坑' / '停住'`。"""
        terrain, who = self.terrain, self.who
        surfaces = terrain.surfaces(start_x)
        if not surfaces:
            return None
        body = botmove.Body(float(start_x), float(surfaces[0] - 1))
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
                    intent = (1, False, False, fast_run)
            if not body.on_ground and double and botmove.at_apex(body):
                intent = (intent[0], True, intent[2], intent[3])
            direction, jump, drop, fast = intent
            body = botmove.tick(terrain, body, who, direction=direction,
                                fast_run=fast, want_jump=jump)
            if jump and not body.on_ground:
                intent = (direction, False, drop, fast)
            if botmove.out_of_world(terrain, body):
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

    def test_one_tick_of_lookahead_falls_in_often(self):
        """★★ 变异验证：把前瞻改回一格，掉坑率必须明显上去。"""
        tally = self.sweep(horizon=1)
        self.assertGreater(self.rate(tally, "掉坑"), 20.0,
                           "一格前瞻本来就该经常掉，实际 %r" % (tally,))

    def test_lookahead_of_one_decision_period_does_not_fall_in(self):
        import bot                                             # noqa: PLC0415
        tally = self.sweep(horizon=bot.BOT_DECISION_TICKS)
        self.assertLessEqual(self.rate(tally, "掉坑"), 1.0,
                             "掉坑率该 ≤1%%，实际 %r" % (tally,))
        self.assertGreater(self.rate(tally, "过去了"), 30.0,
                           "兜底也该过得去一部分，实际 %r" % (tally,))


class SlowedPredictionTests(unittest.TestCase):
    """★★★ 预测要和**执行**用同一个 `speed_scale`（V0.3 §151）。"""

    def setUp(self):
        self.terrain = flat(width=400, floor=200, height=240)
        self.who = Dummy(7.0)

    def landing_of(self, scale):
        body = botmove.Body(100.0, 199.0)
        return botmove.jump_lands(self.terrain, body, self.who, 1,
                                  speed_scale=scale)

    def test_the_prediction_matches_what_actually_runs(self):
        for scale in (1.0, 0.3, 0.0):
            predicted = self.landing_of(scale)
            body = botmove.Body(100.0, 199.0)
            body = botmove.tick(self.terrain, body, self.who, direction=1,
                                want_jump=True, speed_scale=scale)
            for _ in range(80):
                if body.on_ground:
                    break
                body = botmove.tick(self.terrain, body, self.who, direction=1,
                                    speed_scale=scale)
            self.assertIsNotNone(predicted, "倍率 %.1f 该落得住" % scale)
            self.assertEqual(predicted.x, body.x,
                             "倍率 %.1f 的预测落点和真跑的对不上" % scale)

    def test_zero_speed_jumps_straight_up(self):
        """★ 倍率 0：¼S 和操控上限都是 0 —— 竖直跳。"""
        self.assertEqual(100.0, self.landing_of(0.0).x)
        self.assertGreater(self.landing_of(1.0).x, 100.0)

    def test_double_jump_takes_the_scale_too(self):
        far = self.landing_of(1.0)
        frozen = botmove.double_jump_lands(self.terrain, botmove.Body(100.0, 199.0),
                                           self.who, 1, speed_scale=0.0)
        self.assertIsNotNone(frozen)
        self.assertEqual(100.0, frozen.x)
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
        self.assertFalse(botmove.fits(self.slot(6), 60, 39, chrprops.get(0)))

    def test_a_wide_corridor_fits(self):
        self.assertTrue(botmove.fits(self.slot(60), 60, 39, chrprops.get(0)))

    def test_standing_against_a_wall_is_legal(self):
        """★★ 贴着墙站是合法的 —— 一侧的富余补得上另一侧的不足。"""
        width, height, floor = 200, 60, 40
        rows = []
        for y in range(height):
            if y >= floor:
                rows.append("2" * width)
            elif y >= 5:
                rows.append("2" * 40 + "0" * (width - 40))
            else:
                rows.append("0" * width)
        self.assertTrue(botmove.fits(terrain_from(rows), 41, 39, chrprops.get(0)),
                        "左边紧贴墙、右边一片开阔 —— 人站得住")

    def test_open_ground_fits(self):
        self.assertTrue(botmove.fits(flat(width=200, floor=20, height=40),
                                     100, 19, chrprops.get(0)))

    def test_no_terrain_always_fits(self):
        self.assertTrue(botmove.fits(None, 0, 0, chrprops.get(0)))


class RealTrapNodeTests(unittest.TestCase):
    """★★★★★ `Iceria03` 上那几个**只进不出**的陷阱点（V0.3 §152）。"""

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
            for foot in (y, y - 1):
                self.assertFalse(botmove.fits(self.terrain, x, foot, self.who),
                                 "(%d, %d) 是缝，碰撞体塞不进去" % (x, foot))

    def test_it_does_not_reject_the_whole_map(self):
        """★ 误伤面要小：绝大多数落脚点照旧可用。"""
        good = bad = 0
        for x in range(2, self.terrain.width - 2, 8):
            for y in self.terrain.surfaces(x):
                if botmove.fits(self.terrain, x, y - 1, self.who):
                    good += 1
                else:
                    bad += 1
        total = good + bad
        self.assertGreater(total, 200, "采样太少，这个断言没意义")
        self.assertLess(100.0 * bad / total, 15.0,
                        "拒掉 %d/%d 个落脚点，过滤面太大了" % (bad, total))


class IceSpikePocketTests(unittest.TestCase):
    """★★★★★ `Iceria03` 冰塔尖尖那个 **1 像素夹层**（V0.3 §177）：列 1214 `857 实心 / 858 空 / 859 实心`。

    客户端的脚在夹层那一格（858）；塞不下人（`fits` 为假），可达图不进去。
    客户端走路：往左那一列脚下空了就从檐口掉下去 —— 这是出去的路。
    """

    @classmethod
    def setUpClass(cls):
        cls.terrain = mapdata.load("Iceria03")
        if cls.terrain is None:
            raise unittest.SkipTest("没有 Iceria03 的地形产物")
        cls.who = chrprops.get(1)

    def test_the_pocket_is_still_a_pocket(self):
        self.assertIn(859, self.terrain.surfaces(1214))
        self.assertEqual(0, self.terrain.cell(1214, 858))
        self.assertGreaterEqual(self.terrain.cell(1214, 857), 2)
        self.assertFalse(botmove.fits(self.terrain, 1214.0, 858.0, self.who))

    def test_flying_at_it_does_not_end_in_the_pocket(self):
        """三圆扫掠：身子 26 宽，飞向那道冰檐时身圆先撞上冰体。"""
        body = botmove.Body(1244.0, 902.0, -10.0, -14.0, on_ground=False)
        for _ in range(8):
            body = botmove.tick(self.terrain, body, self.who)
        self.assertNotEqual((1214.0, 858.0), (body.x, body.y))

    def test_walking_off_the_eaves_gets_out(self):
        body = botmove.Body(1214.0, 858.0)
        step = botmove.tick(self.terrain, body, self.who, direction=-1)
        self.assertFalse(step.on_ground, "檐口左边就是空的 —— 该踩空")
        landed = botmove.settle(self.terrain, step, self.who)
        self.assertTrue(landed.on_ground)
        self.assertTrue(botmove.fits(self.terrain, landed.x, landed.y, self.who),
                        "掉下去要落在塞得下的地方，不能换一个坑")


if __name__ == "__main__":
    unittest.main()
