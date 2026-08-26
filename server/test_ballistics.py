#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`ballistics.py` 的单测 —— 服务端算的弹道和客户端走的必须是同一条。

★ 最要紧的一组是 `RecurrenceTests`：闭式解（`position_at`）和**逐 tick 递推**
（`step_by_step`，一句一句照抄 `0x47f603`）对拍。闭式解错了的表现是
「爆炸出现在子弹没到过的地方」，实机上很难一眼看出来。
"""
from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ballistics                                              # noqa: E402
import weapondata                                              # noqa: E402


class FakeWeapon(object):
    """`weapondata.Weapon` 的最小替身 —— 只有 `ballistics` 读的那几格。"""

    def __init__(self, velocity=100.0, gravity=0.0, power_control=0,
                 max_velocity=None, acceleration=0.0):
        self.velocity = float(velocity)
        self.gravity = float(gravity)
        self.power_control = int(power_control)
        self.max_velocity = max_velocity
        self.acceleration = float(acceleration)


def step_by_step(x0, y0, shot, ticks):
    """★ 逐 tick 递推，一句一句照抄 `0x47f603`（弹体 `vft+0x158`）：

        v.y += GetFrameDt() × [弹体+0x314]      ; 先加重力
        pos += v × [弹体+0x274]                 ; 再走一步
    """
    vx = shot.speed * math.cos(shot.angle)
    vy = shot.speed * math.sin(shot.angle)
    x, y = x0, y0
    for _ in range(ticks):
        vy += shot.gravity
        x += vx
        y += vy
    return x, y


class ScaleTests(unittest.TestCase):
    """尺度常量 —— 全部有出处，改之前先去看 §47。"""

    def test_the_tick_is_thirty_two_milliseconds(self):
        """语料回归：慢弹的 `距离 / 时间 / Velocity` 上包络停在 31.2~31.3，
        而 `1000 / 32 = 31.25`（8 个不同 `Velocity` 都对得上）。"""
        self.assertEqual(32.0, ballistics.TICK_MS)
        self.assertAlmostEqual(31.25, ballistics.TICKS_PER_SECOND)

    def test_gravity_is_one_point_two_per_tick(self):
        """`GetFrameDt()`（`0x40a04f`）= `[ctx+0x344](=1.0) × 1.2`。
        语料实测 `a / GravityFactor` = 1.1972 / 1.1546 / 1.1967 / 1.2000。"""
        self.assertEqual(1.2, ballistics.GRAVITY_PER_TICK)
        weapon = FakeWeapon(gravity=0.8)
        self.assertAlmostEqual(0.96, ballistics.gravity_per_tick(weapon))
        self.assertEqual(0.0, ballistics.gravity_per_tick(FakeWeapon()))


class PowerModeTests(unittest.TestCase):
    """三种初速模式（`0x4920a1` 的三岔口）。"""

    def test_mode_zero_uses_velocity_with_power_one(self):
        weapon = FakeWeapon(velocity=130.0, power_control=0)
        self.assertEqual(130.0, ballistics.max_speed(weapon))
        self.assertEqual(1.0, ballistics.power_for_speed(weapon, 130.0))
        self.assertEqual(130.0, ballistics.speed_for_power(weapon, 1.0))

    def test_mode_zero_ignores_max_velocity(self):
        """`0x4920a7` 那一支根本不读 `[weapon+0x28]` —— 别被 ini 里那一格骗了。
        （`ch102-02` 就是 `PowerControl=0` 却填了 `MaxVelocity`。）"""
        weapon = FakeWeapon(velocity=1.0, power_control=0, max_velocity=20.0)
        self.assertEqual(1.0, ballistics.max_speed(weapon))

    def test_mode_one_is_the_power_itself_capped_by_max_velocity(self):
        weapon = FakeWeapon(velocity=10.0, power_control=1, max_velocity=30.0)
        self.assertEqual(30.0, ballistics.max_speed(weapon))
        self.assertEqual(30.0, ballistics.power_for_speed(weapon, 30.0))
        self.assertEqual(30.0, ballistics.speed_for_power(weapon, 999.0))

    def test_mode_two_is_velocity_times_the_charge_curve(self):
        """`speed = Velocity × ((power + 15) × 0.04)`
        （`0x69381c` = 15.0、`0x693bb8` = 0.04）。`power = 10` 时恰好是 `Velocity`。"""
        weapon = FakeWeapon(velocity=10.0, power_control=2, max_velocity=60.0)
        self.assertAlmostEqual(10.0, ballistics.speed_for_power(weapon, 10.0))
        self.assertEqual(60.0, ballistics.max_speed(weapon))
        power = ballistics.power_for_speed(weapon, 60.0)
        self.assertAlmostEqual(135.0, power)
        self.assertAlmostEqual(60.0, ballistics.speed_for_power(weapon, power))

    def test_the_charge_power_stays_in_the_range_the_corpus_saw(self):
        """★ 语料里蓄力武器的 `rpFire +18` 是 8~531（§43）——
        算出来的 `power` 落在这个区间之外就说明公式反了。"""
        for velocity, top in ((10.0, 60.0), (8.0, 60.0), (15.0, 40.0)):
            weapon = FakeWeapon(velocity=velocity, power_control=2,
                                max_velocity=top)
            power = ballistics.power_for_speed(weapon, top)
            self.assertTrue(8.0 <= power <= 531.0, f"power={power}")


class DirectShotTests(unittest.TestCase):
    """直射弹（`GravityFactor = 0`）：一条直线。"""

    def setUp(self):
        self.weapon = FakeWeapon(velocity=100.0)

    def test_the_angle_points_straight_at_the_target(self):
        shot = ballistics.solve(self.weapon, 300.0, -400.0)
        self.assertAlmostEqual(math.atan2(-400.0, 300.0), shot.angle)

    def test_the_flight_time_is_distance_over_speed(self):
        shot = ballistics.solve(self.weapon, 300.0, 400.0)
        self.assertAlmostEqual(500.0 / 100.0, shot.ticks)
        self.assertAlmostEqual(5.0 * 32.0 / 1000.0, shot.seconds)

    def test_it_lands_on_the_target(self):
        shot = ballistics.solve(self.weapon, -250.0, 120.0)
        x, y = ballistics.position_at(1000.0, 500.0, shot, shot.ticks)
        self.assertAlmostEqual(750.0, x, places=4)
        self.assertAlmostEqual(620.0, y, places=4)

    def test_a_zero_distance_has_no_solution(self):
        self.assertIsNone(ballistics.solve(self.weapon, 0.0, 0.0))

    def test_the_path_of_a_direct_shot_is_just_two_points(self):
        shot = ballistics.solve(self.weapon, 600.0, 0.0)
        self.assertEqual(2, len(ballistics.path_points(0.0, 0.0, shot)))


class ArcShotTests(unittest.TestCase):
    """抛物线（手雷那一类）。"""

    def setUp(self):
        # `ch00-02` 荤苞藕：PowerControl=2 / Velocity=10 / MaxVelocity=60 /
        # GravityFactor=0.8。
        self.weapon = FakeWeapon(velocity=10.0, gravity=0.8, power_control=2,
                                 max_velocity=60.0)

    def test_the_arc_actually_lands_on_the_target(self):
        """★★ 闭式解 vs 逐 tick 递推：误差得比一个地形像素还小。"""
        for dx, dy in ((400.0, 0.0), (-400.0, 0.0), (250.0, -180.0),
                       (700.0, 220.0), (-120.0, -300.0)):
            with self.subTest(dx=dx, dy=dy):
                shot = ballistics.solve(self.weapon, dx, dy)
                self.assertIsNotNone(shot, "这个距离应该够得着")
                x, y = ballistics.position_at(0.0, 0.0, shot, shot.ticks)
                self.assertAlmostEqual(dx, x, places=3)
                self.assertAlmostEqual(dy, y, places=3)

    def test_it_prefers_the_flat_arc(self):
        """两个根里取低抛：飞得快、中途撞地形的机会少，也更像人打的。"""
        shot = ballistics.solve(self.weapon, 400.0, 0.0)
        high = math.asin(min(1.0, shot.gravity * shot.ticks / 2.0 / shot.speed))
        self.assertLess(abs(shot.angle), high + 0.2)

    def test_out_of_range_returns_none(self):
        """判别式 < 0 = 这把枪真的够不着（`solve()` 不许瞎给一个角度）。"""
        self.assertIsNone(ballistics.solve(self.weapon, 100000.0, 0.0))

    def test_straight_up_is_handled(self):
        """`dx = 0` 时 `tanθ` 发散 —— 走 `_solve_vertical` 那条路。"""
        shot = ballistics.solve(self.weapon, 0.0, -200.0)
        self.assertIsNotNone(shot)
        _x, y = ballistics.position_at(0.0, 0.0, shot, shot.ticks)
        self.assertAlmostEqual(-200.0, y, places=3)

    def test_the_arc_is_cut_into_enough_segments_to_check_terrain(self):
        shot = ballistics.solve(self.weapon, 700.0, 0.0)
        points = ballistics.path_points(0.0, 0.0, shot)
        self.assertGreaterEqual(len(points), 3)
        # 首尾必须在里面，否则遮挡判定会漏掉两头。
        self.assertAlmostEqual(0.0, points[0][0])
        self.assertAlmostEqual(700.0, points[-1][0], places=3)


class RecurrenceTests(unittest.TestCase):
    """★★★ 闭式解和逐 tick 递推必须逐点一致。"""

    def test_closed_form_matches_the_literal_loop(self):
        for gravity, mode, top in ((0.0, 0, None), (0.8, 2, 60.0),
                                   (1.2, 2, 40.0), (1.0, 1, 30.0)):
            weapon = FakeWeapon(velocity=10.0, gravity=gravity,
                                power_control=mode, max_velocity=top)
            shot = ballistics.solve(weapon, 300.0, -50.0)
            self.assertIsNotNone(shot)
            for ticks in (1, 2, 5, 13, 30):
                with self.subTest(gravity=gravity, ticks=ticks):
                    want = step_by_step(7.0, 11.0, shot, ticks)
                    got = ballistics.position_at(7.0, 11.0, shot, float(ticks))
                    self.assertAlmostEqual(want[0], got[0], places=6)
                    self.assertAlmostEqual(want[1], got[1], places=6)


@unittest.skipIf(not os.path.isfile(weapondata.DATA_PATH),
                 "server/bot_weapons.json 还没生成")
class RealWeaponTests(unittest.TestCase):
    """拿真产物里每一把可用武器跑一遍 —— 有一把解不出来 bot 就会哑火。"""

    PLAYABLE = (0, 1, 2) + tuple(range(100, 111))

    def weapons(self):
        for character in self.PLAYABLE:
            for weapon in weapondata.usable_for(character):
                yield character, weapon

    def test_every_usable_weapon_can_hit_a_target_nearby(self):
        """★ 贴身缠斗的距离，每把枪都得解得出弹道 —— 一把解不出 bot 就哑火。

        ★ 150 单位是**所有**武器都够得着的距离；再远的话
        `ch109-02`（初速 20/tick、重力 1.2）这种小手雷会真的够不着，
        那是合法结论（下一个用例专门测它）。
        """
        for character, weapon in self.weapons():
            for dx, dy in ((150.0, 0.0), (-150.0, -40.0), (100.0, 60.0)):
                with self.subTest(character=character, ammo=weapon.id,
                                  dx=dx, dy=dy):
                    shot = ballistics.solve(weapon, dx, dy)
                    self.assertIsNotNone(shot)
                    x, y = ballistics.position_at(0.0, 0.0, shot, shot.ticks)
                    self.assertAlmostEqual(dx, x, places=2)
                    self.assertAlmostEqual(dy, y, places=2)

    def test_a_small_grenade_really_does_run_out_of_range(self):
        """★ 抛物线武器**有**最大射程（判别式 < 0）—— `solve()` 到那儿必须
        老实返回 `None`，不能瞎给一个打不到的角度。"""
        weapon = weapondata.get(1109020)     # 初速 20/tick、GravityFactor 1.0
        self.assertIsNotNone(ballistics.solve(weapon, 150.0, 0.0))
        self.assertIsNone(ballistics.solve(weapon, 900.0, 0.0))

    def test_accelerating_bullets_start_slow_and_speed_up(self):
        """★ `Acceleration`（§49）：`ch100-03` 初速才 3/tick，但每 tick
        加 2、封顶 300 —— 不建模的话飞 600 单位要 6.4 秒，实机上就是
        「bot 打了半天不掉血」。"""
        weapon = weapondata.get(1100030)
        self.assertEqual(2.0, weapon.acceleration)
        shot = ballistics.solve(weapon, 600.0, 0.0)
        self.assertLess(shot.seconds, 1.0)
        x, _y = ballistics.position_at(0.0, 0.0, shot, shot.ticks)
        self.assertAlmostEqual(600.0, x, places=2)

    def test_the_flight_time_is_never_absurd(self):
        """★ 飞行时间是**延后爆炸**的等待时间 —— 长到几秒就说明尺度错了，
        而且实机上会看成「bot 打了但不掉血」。"""
        for character, weapon in self.weapons():
            shot = ballistics.solve(weapon, 600.0, 0.0)
            with self.subTest(character=character, ammo=weapon.id):
                if shot is None:
                    continue            # 够不着是合法结论，不是错
                self.assertLess(shot.seconds, 3.0,
                                f"{weapon.id} 飞 600 单位要 {shot.seconds:.2f} 秒")

    def test_the_basic_guns_are_fast(self):
        """1 号轻武器（`Velocity` 100~230）：600 单位应该在 1/4 秒内到。
        `Velocity = 100` ⇒ 3125 单位/秒 ⇒ 600 单位 ≈ 0.19 秒。"""
        weapon = weapondata.get(1002010)
        shot = ballistics.solve(weapon, 600.0, 0.0)
        self.assertAlmostEqual(0.192, shot.seconds, places=3)


if __name__ == "__main__":
    unittest.main()
