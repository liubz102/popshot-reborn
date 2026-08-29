# -*- coding: utf-8 -*-
"""`bothp` / `botarms` / `botaim` / `botthreat` 的纯算术测试
（V0.3 M5-C / M5-D / M5-E），外加 `botmove` 的二段跳。

这几个模块都不碰包、不碰房间，全是「给一组数、得一个数」，
所以合在一个文件里。端到端（真包 / 真房间）那一批在 `test_botsync.py`。
"""
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ballistics                                              # noqa: E402
import botaim                                                  # noqa: E402
import botarms                                                 # noqa: E402
import bothp                                                   # noqa: E402
import botmove                                                 # noqa: E402
import botthreat                                               # noqa: E402
import mapdata                                                 # noqa: E402
import weapondata                                              # noqa: E402
from test_mapdata import make_record                            # noqa: E402


class FakeWeapon(object):
    """按字段捏一把枪 —— 只实现 `botarms` 用得到的那几格。"""

    def __init__(self, damage=10, shots=1, magazine=None, cooling_ms=None,
                 reload_ms=None, fire_interval_ms=None, splash_range=None,
                 splash_damage=None, power_control=0, size=4.0):
        self.raw = {}
        self._damage = damage
        self.shots = shots
        self.magazine = magazine
        self.cooling_ms = cooling_ms
        self.reload_ms = reload_ms
        self.fire_interval_ms = fire_interval_ms
        self.splash_range = splash_range
        self._splash_damage = damage if splash_damage is None else splash_damage
        self.power_control = power_control
        self.size = size

    def damage_for(self, _region):
        return self._damage

    @property
    def splash_damage(self):
        return self._splash_damage


def shot(ticks=10.0, power=1.0):
    return ballistics.Shot(0.0, power, 100.0, ticks, 0.0)


class LedgerTests(unittest.TestCase):

    def test_damage_accumulates_and_heals_clamp_at_full(self):
        book = bothp.Ledger()
        book.note_damage(2, 30)
        book.note_damage(2, 15)
        self.assertEqual(45.0, book.taken_by(2))
        self.assertEqual(55.0, book.remaining(2, 100))
        book.note_heal(2, 100)
        self.assertEqual(0.0, book.taken_by(2))
        self.assertEqual(1.0, book.fraction(2, 100))

    def test_zero_and_negative_amounts_are_ignored(self):
        book = bothp.Ledger()
        book.note_damage(0, 0)
        book.note_damage(0, -5)
        book.note_heal(0, -5)
        self.assertEqual({}, book.taken)

    def test_the_fraction_never_leaves_zero_to_one(self):
        book = bothp.Ledger()
        book.note_damage(1, 500)
        self.assertEqual(0.0, book.fraction(1, 100))
        self.assertEqual(0.0, book.remaining(1, 100))

    def test_standing_up_is_the_flip_that_resets_the_row(self):
        """★ 判据是**状态翻转**（铁律 10），不是「死后几秒」。"""
        book = bothp.Ledger()
        book.note_damage(3, 80)
        self.assertFalse(book.note_lying(3, False))   # 一直站着 —— 不翻转
        self.assertFalse(book.note_lying(3, True))    # 倒下 —— 也不算翻转
        self.assertTrue(book.note_lying(3, False))    # 躺 -> 站 = 重生
        book.reset(3)
        self.assertEqual(1.0, book.fraction(3, 100))

    def test_clear_wipes_everything(self):
        book = bothp.Ledger()
        book.note_damage(1, 10)
        book.note_lying(1, True)
        book.clear()
        self.assertEqual({}, book.taken)
        self.assertEqual({}, book.lying)


class RateTests(unittest.TestCase):

    def test_a_magazine_weapon_averages_in_its_reload(self):
        """`ch02-01`：14 发 × 140 ms + 1400 ms 换弹 = 3.36 秒 14 发。"""
        weapon = FakeWeapon(magazine=14, cooling_ms=140, reload_ms=1400,
                            fire_interval_ms=140)
        self.assertAlmostEqual(14.0 / 3.36, botarms.shots_per_second(weapon),
                               places=6)

    def test_without_a_magazine_it_is_just_the_interval(self):
        weapon = FakeWeapon(fire_interval_ms=2500)
        self.assertAlmostEqual(0.4, botarms.shots_per_second(weapon))

    def test_charging_time_counts_against_the_rate(self):
        """★ 蓄满一颗手雷要按住 40 个 tick（§73）—— 那也是这一枪的时间。"""
        weapon = FakeWeapon(fire_interval_ms=2000,
                            power_control=ballistics.MODE_CHARGE)
        plain = botarms.shots_per_second(weapon, shot(ticks=10.0, power=80))
        self.assertLess(plain, 0.5)
        self.assertAlmostEqual(
            1000.0 / (2000.0 + 40 * ballistics.TICK_MS), plain, places=6)

    def test_a_splash_weapon_cannot_fire_faster_than_its_flight(self):
        """带溅射的枪要等上一发炸完（`_may_fire()` 的句柄闸门，§43）。"""
        weapon = FakeWeapon(fire_interval_ms=200, splash_range=100)
        rate = botarms.shots_per_second(weapon, shot(ticks=100.0))
        self.assertAlmostEqual(1000.0 / (100.0 * ballistics.TICK_MS), rate)


class HitChanceTests(unittest.TestCase):

    def test_a_standing_target_is_always_reachable(self):
        self.assertEqual(1.0, botarms.hit_chance(shot(), 0.0, 20.0))

    def test_a_slow_bullet_against_a_runner_is_a_coin_toss_at_best(self):
        # 目标 8 单位/tick，弹飞 25 tick -> 可能偏 200；窗口 20 -> 10%
        self.assertAlmostEqual(
            0.1, botarms.hit_chance(shot(ticks=25.0), 8.0, 20.0))

    def test_a_fast_bullet_keeps_the_full_chance(self):
        self.assertEqual(1.0, botarms.hit_chance(shot(ticks=2.0), 8.0, 20.0))


class SplashTests(unittest.TestCase):

    def test_the_falloff_matches_the_reversed_formula(self):
        """§90：`int((1 − r/(range+35)) × (splash − 1) + 1)`。"""
        weapon = FakeWeapon(damage=20, splash_range=100, splash_damage=25)
        self.assertEqual(25.0, botarms.splash_damage_at(weapon, 0.0))
        self.assertEqual(0.0, botarms.splash_damage_at(weapon, 135.0))
        self.assertEqual(float(int((1.0 - 67.5 / 135.0) * 24 + 1)),
                         botarms.splash_damage_at(weapon, 67.5))

    def test_no_splash_range_means_no_splash(self):
        self.assertEqual(0.0, botarms.splash_damage_at(FakeWeapon(), 0.0))


class ScoreTests(unittest.TestCase):

    def test_self_splash_is_charged_against_the_score(self):
        """★ D50 的口径：照打，但把代价如实结算 —— 贴脸扔手雷分更低。"""
        weapon = FakeWeapon(damage=20, splash_range=100, splash_damage=25,
                            fire_interval_ms=1000)
        far = botarms.score(weapon, shot(ticks=5.0), 0.0, 20.0, 400.0)
        near = botarms.score(weapon, shot(ticks=5.0), 0.0, 20.0, 30.0)
        self.assertGreater(far, near)

    def test_a_weapon_that_cannot_reach_has_no_score(self):
        self.assertIsNone(botarms.score(FakeWeapon(), None, 0.0, 20.0, 100.0))

    def test_spread_frags_multiply_the_expected_damage(self):
        one = FakeWeapon(damage=3, shots=1, fire_interval_ms=200)
        three = FakeWeapon(damage=3, shots=3, fire_interval_ms=200)
        self.assertAlmostEqual(
            3.0 * botarms.score(one, shot(), 0.0, 20.0, 400.0),
            botarms.score(three, shot(), 0.0, 20.0, 400.0))

    def test_switching_needs_a_clear_margin(self):
        self.assertFalse(botarms.better(10.0, 11.0))
        self.assertTrue(botarms.better(10.0, 13.0))
        self.assertTrue(botarms.better(None, 1.0))
        self.assertFalse(botarms.better(10.0, None))


class RealWeaponTableTests(unittest.TestCase):
    """拿**真产物**（`bot_weapons.json`）算一遍，验的是「结论说得通」。"""

    def setUp(self):
        if not weapondata.usable_for(2):
            self.skipTest("没有武器表产物")

    def score_of(self, weapon, target_speed, span):
        solved = ballistics.solve(weapon, span, 0.0,
                                  speed=ballistics.max_speed(weapon))
        if solved is None:
            return None
        return botarms.score(weapon, solved, target_speed, 22.0, span)

    def test_against_a_runner_the_machine_gun_beats_the_rocket(self):
        """★ 慢弹打移动目标吃亏 —— 这就是「射速 / 弹速要进权衡」那一条。"""
        table = {w.raw["slot"]: w for w in weapondata.usable_for(2)}
        gun = self.score_of(table[1], 8.0, 600.0)
        rocket = self.score_of(table[3], 8.0, 600.0)
        self.assertGreater(gun, rocket)

    def test_against_a_standing_target_the_rocket_wins(self):
        table = {w.raw["slot"]: w for w in weapondata.usable_for(2)}
        gun = self.score_of(table[1], 0.0, 600.0)
        rocket = self.score_of(table[3], 0.0, 600.0)
        self.assertGreater(rocket, gun)


class VelocitySampleTests(unittest.TestCase):

    def test_two_points_one_heartbeat_apart_give_units_per_tick(self):
        self.assertEqual((8.0, -2.0),
                         botaim.sample_velocity([(0.0, 0.0), (32.0, -8.0)]))

    def test_one_point_is_not_a_velocity(self):
        self.assertEqual((0.0, 0.0), botaim.sample_velocity([(0.0, 0.0)]))

    def test_an_absurd_jump_is_thrown_away(self):
        """重生 / 换图那种瞬移不该被当成「他跑得飞快」。"""
        self.assertEqual((0.0, 0.0),
                         botaim.sample_velocity([(0.0, 0.0), (900.0, 0.0)]))


class LeadTests(unittest.TestCase):
    """直射弹（速度 100 / tick）打一个每 tick 走 8 的目标。"""

    def setUp(self):
        self.weapon = FakeWeapon()
        self.muzzle = (0.0, 0.0)

    def solve(self, dx, dy):
        span = math.hypot(dx, dy)
        return ballistics.Shot(math.atan2(dy, dx), 1.0, 100.0, span / 100.0,
                               0.0)

    def test_a_standing_target_is_aimed_at_directly(self):
        point, _shot = botaim.lead_point(self.solve, self.muzzle,
                                         (500.0, 0.0), (0.0, 0.0))
        self.assertEqual((500.0, 0.0), point)

    def test_a_moving_target_is_led_by_the_flight_time(self):
        point, solved = botaim.lead_point(self.solve, self.muzzle,
                                          (500.0, 0.0), (8.0, 0.0))
        self.assertGreater(point[0], 500.0)
        # 收敛点：x = 500 + 8·x/100 -> x = 543.5
        self.assertAlmostEqual(543.5, point[0], places=1)
        self.assertAlmostEqual(5.435, solved.ticks, places=2)

    def test_leading_backwards_works_too(self):
        point, _shot = botaim.lead_point(self.solve, self.muzzle,
                                         (500.0, 0.0), (-8.0, 0.0))
        self.assertLess(point[0], 500.0)


class MissTests(unittest.TestCase):

    def setUp(self):
        self.muzzle = (0.0, 0.0)

    def solve(self, dx, dy):
        span = math.hypot(dx, dy)
        return ballistics.Shot(math.atan2(dy, dx), 1.0, 100.0, span / 100.0,
                               0.0)

    def test_the_dice_respect_the_difficulty(self):
        never = botaim.roll_error(lambda n: n - 1, 0.22)
        always = botaim.roll_error(lambda n: 0, 0.22)
        self.assertIsNone(never)
        self.assertIsNotNone(always)

    def test_a_zero_chance_never_misses(self):
        self.assertIsNone(botaim.roll_error(lambda n: 0, 0.0))

    def test_a_miss_pushes_the_aim_sideways_by_a_near_miss_margin(self):
        """★ 失误要「差一点」，不是朝天上放 —— 偏 1~3 倍命中窗口。"""
        miss = botaim.Miss(1.0, 2.0)
        point, solved = botaim.aim(self.solve, self.muzzle, (500.0, 0.0),
                                   (0.0, 0.0), 20.0, miss)
        self.assertAlmostEqual(40.0, point[1])         # 2 × 20
        self.assertAlmostEqual(500.0, point[0])
        self.assertNotAlmostEqual(0.0, solved.angle)   # ★ 弹道跟着重解了

    def test_a_wrong_lead_is_the_other_half_of_a_miss(self):
        straight, _shot = botaim.aim(self.solve, self.muzzle, (500.0, 0.0),
                                     (8.0, 0.0), 20.0, None)
        wrong, _shot2 = botaim.aim(self.solve, self.muzzle, (500.0, 0.0),
                                   (8.0, 0.0), 20.0, botaim.Miss(0.0, 0.0))
        self.assertGreater(straight[0], 500.0)
        self.assertAlmostEqual(500.0, wrong[0])        # 完全没带提前量

    def test_an_unreachable_skew_falls_back_to_the_honest_shot(self):
        """推歪之后解不出来，宁可打中也不要干脆不开枪。"""
        def picky(dx, dy):
            if abs(dy) > 1.0:
                return None
            return self.solve(dx, dy)
        point, solved = botaim.aim(picky, self.muzzle, (500.0, 0.0),
                                   (0.0, 0.0), 20.0, botaim.Miss(1.0, 3.0))
        self.assertEqual((500.0, 0.0), point)
        self.assertIsNotNone(solved)


class FakeCharacter(object):
    """`botthreat` 只用到 `speed` / `size_body` / `center()` 三样。"""

    speed = 7.0
    size_head = 10.0
    size_body = 13.0
    size_legs = 12.0

    def center(self, x, y, crouched=False):
        legs = self.size_legs * (0.5 if crouched else 1.0)
        return (x, y - 2.0 * legs - self.size_body)


def flat_terrain(width=1600, height=240, floor=150):
    rows = ["".join("2" if y >= floor else "0" for _x in range(width))
            for y in range(height)]
    return mapdata.MapTerrain(make_record(rows))


def straight_threat(x, y, speed=25.0, angle=0.0, weapon=None, at=0.0, seat=0):
    weapon = weapon or FakeWeapon(size=8.0)
    shot = ballistics.Shot(angle, 1.0, speed, 0.0, 0.0)
    return botthreat.Threat(seat, weapon, x, y, shot, at, ("t", x, y))


class ThreatGeometryTests(unittest.TestCase):

    def setUp(self):
        self.terrain = flat_terrain()
        self.who = FakeCharacter()

    def centers(self, x, y, count=botthreat.HORIZON):
        return [self.who.center(x, y) for _ in range(count)]

    def test_a_bullet_on_a_collision_course_is_seen_coming(self):
        cx, cy = self.who.center(600.0, 150.0)
        threat = straight_threat(100.0, cy)
        step = botthreat.impact_tick(self.terrain, threat, 0.0,
                                     self.centers(600.0, 150.0), 21.0)
        self.assertIsNotNone(step)
        self.assertAlmostEqual(500.0 / 25.0, step, delta=1.0)

    def test_a_bullet_that_misses_by_a_mile_is_ignored(self):
        threat = straight_threat(100.0, 20.0)
        self.assertIsNone(botthreat.impact_tick(
            self.terrain, threat, 0.0, self.centers(600.0, 150.0), 21.0))

    def test_terrain_between_us_stops_the_prediction(self):
        """★ 「躲在掩体后面」不用单独写规则 —— 弹道自己先撞墙。"""
        rows = []
        for y in range(240):
            row = []
            for x in range(1600):
                if y >= 150 or (300 <= x < 320 and y >= 60):
                    row.append("2")
                else:
                    row.append("0")
            rows.append("".join(row))
        walled = mapdata.MapTerrain(make_record(rows))
        cx, cy = self.who.center(600.0, 150.0)
        threat = straight_threat(100.0, cy)
        self.assertIsNone(botthreat.impact_tick(
            walled, threat, 0.0, self.centers(600.0, 150.0), 21.0))

    def test_splash_makes_the_danger_circle_bigger(self):
        plain = straight_threat(0.0, 0.0, weapon=FakeWeapon(size=8.0))
        boom = straight_threat(0.0, 0.0,
                               weapon=FakeWeapon(size=8.0, splash_range=100))
        self.assertEqual(13.0 + 8.0, plain.danger_radius(13.0))
        self.assertEqual(13.0 + 8.0 + 100.0, boom.danger_radius(13.0))

    def test_a_shot_already_past_us_is_not_a_threat(self):
        cx, cy = self.who.center(600.0, 150.0)
        threat = straight_threat(100.0, cy, at=-20.0)   # 20 秒前打的
        self.assertIsNone(botthreat.impact_tick(
            self.terrain, threat, 0.0, self.centers(600.0, 150.0), 21.0))


class DodgeChoiceTests(unittest.TestCase):

    def setUp(self):
        self.terrain = flat_terrain()
        self.who = FakeCharacter()
        self.body = botmove.Body(600.0, 150.0)

    def test_nothing_to_dodge_means_do_not_move(self):
        """★ 打不到我就别乱动 —— 真人 39% 的心跳是站着不动的（§71）。"""
        threat = straight_threat(100.0, 20.0)
        self.assertIsNone(botthreat.choose(
            self.terrain, self.body, self.who, [threat], 0.0))

    def test_no_threats_at_all_means_none(self):
        self.assertIsNone(botthreat.choose(
            self.terrain, self.body, self.who, [], 0.0))

    def test_an_incoming_bullet_makes_it_move(self):
        cx, cy = self.who.center(600.0, 150.0)
        threat = straight_threat(100.0, cy)
        option = botthreat.choose(self.terrain, self.body, self.who,
                                  [threat], 0.0)
        self.assertIsNotNone(option)
        self.assertIsNot(option, botthreat.STAND)

    def test_the_chosen_action_really_gets_out_of_the_way(self):
        cx, cy = self.who.center(600.0, 150.0)
        threat = straight_threat(100.0, cy)
        option = botthreat.choose(self.terrain, self.body, self.who,
                                  [threat], 0.0)
        centers = botthreat.simulate(self.terrain, self.body, self.who, option)
        self.assertIsNone(botthreat.impact_tick(
            self.terrain, threat, 0.0, centers,
            threat.danger_radius(self.who.size_body)))

    def test_a_blind_roll_takes_whatever_it_picked(self):
        """★ 难度掷中「预估失误」时不挑最优 —— 那正是真人躲错的样子。"""
        cx, cy = self.who.center(600.0, 150.0)
        threat = straight_threat(100.0, cy)
        blind = botthreat.Option("left", -1, False, False, False, False)
        self.assertIs(blind, botthreat.choose(
            self.terrain, self.body, self.who, [threat], 0.0,
            blind_pick=blind))

    def test_a_blind_roll_that_picked_standing_does_nothing(self):
        cx, cy = self.who.center(600.0, 150.0)
        threat = straight_threat(100.0, cy)
        self.assertIsNone(botthreat.choose(
            self.terrain, self.body, self.who, [threat], 0.0,
            blind_pick=botthreat.STAND))

    def test_the_dice_follow_the_difficulty(self):
        self.assertIsNone(botthreat.roll_blind(lambda n: n - 1, 0.30))
        self.assertIsNotNone(botthreat.roll_blind(lambda n: 0, 0.30))
        self.assertIsNone(botthreat.roll_blind(lambda n: 0, 0.0))


class DoubleJumpTests(unittest.TestCase):
    """★ `botmove` 的第二段跳（§124）—— 语料量出来的 24.0。"""

    def setUp(self):
        self.terrain = flat_terrain()
        self.who = FakeCharacter()

    def test_the_second_jump_is_stronger_than_the_first(self):
        self.assertGreater(botmove.DOUBLE_JUMP_SPEED, botmove.JUMP_SPEED)

    def test_it_only_works_in_the_air(self):
        ground = botmove.Body(600.0, 150.0)
        self.assertIs(ground, botmove.double_jump(ground))

    def test_it_resets_the_vertical_speed_rather_than_adding(self):
        falling = botmove.Body(600.0, 100.0, 3.0, 12.0, on_ground=False)
        after = botmove.double_jump(falling)
        self.assertAlmostEqual(-botmove.DOUBLE_JUMP_SPEED, after.vy)
        self.assertAlmostEqual(3.0, after.vx, msg="腾空水平速度不该被改（§93）")

    def test_only_once_per_flight(self):
        air = botmove.Body(600.0, 100.0, 0.0, 5.0, on_ground=False)
        once = botmove.double_jump(air)
        self.assertIs(once, botmove.double_jump(once))

    def test_landing_gives_it_back(self):
        air = botmove.Body(600.0, 100.0, 0.0, 5.0, on_ground=False)
        used = botmove.double_jump(air)
        self.assertTrue(used.air_jumped)
        landed = botmove.Body(600.0, 150.0)
        self.assertFalse(landed.air_jumped)

    def test_it_reaches_higher_than_a_single_jump(self):
        # ★ 地图要够高：普通跳一下就升 157（`v²/2g`），
        #   图外算“撞天花板”（`blocks_bullet` 对越界返回 True）。
        tall = flat_terrain(width=240, height=520, floor=440)
        tops = []
        for jumps in ((0,), (0, 10)):
            body = botmove.Body(120.0, 440.0)
            top = body.y
            for step in range(40):
                body = botmove.tick(tall, body, self.who,
                                    want_jump=(step in jumps))
                top = min(top, body.y)
            tops.append(top)
        self.assertLess(tops[1], tops[0] - 100.0,
                        "二段跳该明显更高：%r" % (tops,))


if __name__ == "__main__":
    unittest.main()
