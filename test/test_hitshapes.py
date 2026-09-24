#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""弹体撞人的判定照抄客户端（X_Mod §86）：`bot._character_hit` / `_shell_step`。

钉的是三件逐指令逆出来的事实：

1. 圆按**腿 → 身 → 头**的顺序试，第一个扫到的就算（`0x50f410` 两层循环，命中就返回），
   不是「谁的 t 更早」；
2. 武器的 `PassObjCollBlockFlags`（武器定义 `+0x20`）和圆的掩码相交就穿过去：腿带 4、头带 2，
   `-03` 那 21 节武器填了 4 ⇒ 打不到腿；
3. 相对扫掠（`0x50bd67`）：弹体和人**各按自己的速度**走一格；踩地的人速度恒 0。

★ 纯标准库，两套运行时都跑。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import ballistics  # noqa: E402
import bot  # noqa: E402
import botmove  # noqa: E402
import chrprops  # noqa: E402
import weapondata  # noqa: E402

WHO = chrprops.get(0)               # 腿 12、身 13、头 10：脚上 12 / 37 / 60


class _Obj(object):
    pass


class ShapeTableTests(unittest.TestCase):

    def test_the_client_order_is_legs_body_head(self):
        shapes = WHO.hit_shapes(100.0, 500.0)
        self.assertEqual(["legs", "body", "head"], [s[3] for s in shapes])
        self.assertEqual([4, 0, 2], [s[4] for s in shapes])
        self.assertEqual(sorted(WHO.circles(100.0, 500.0)),
                         sorted(s[:4] for s in shapes), "还是那三个圆")

    def test_the_minus_03_weapons_carry_the_leg_flag(self):
        self.assertEqual(4, weapondata.get(1000030).pass_coll_flags, "ch00-03 T1")
        self.assertEqual(0, weapondata.get(1000020).pass_coll_flags, "苹果雷没填")


class CharacterHitTests(unittest.TestCase):
    """人站在 (100, 500)：腿心 488、身心 463、头心 440。"""

    def hit(self, ax, ay, bx, by, vx=0.0, vy=0.0, radius=3.0, flags=0):
        return bot._character_hit(WHO, 100.0, 500.0, False, vx, vy,
                                  ax, ay, bx, by, radius, flags)

    def test_legs_win_even_when_the_body_is_touched_first(self):
        """★ 往下扎的一格先穿身、再穿腿 —— 客户端报的是**腿**（先试腿，扫到就算）。"""
        t, region = self.hit(100.0, 450.0, 100.0, 495.0)
        self.assertEqual("legs", region)
        # 旧的「谁的 t 最早」会报身体：
        body_t = bot._segment_circle_t(100.0, 450.0, 100.0, 495.0,
                                       100.0, 463.0, 13.0 + 3.0)
        self.assertLess(body_t, t)

    def test_a_leg_passing_weapon_goes_through_the_legs(self):
        """★ `-03` 那族（`PassObjCollBlockFlags=4`）打在腿上直接穿过去。"""
        self.assertEqual("legs", self.hit(60.0, 490.0, 140.0, 490.0)[1])
        self.assertIsNone(self.hit(60.0, 490.0, 140.0, 490.0, flags=4))
        self.assertEqual("body", self.hit(60.0, 463.0, 140.0, 463.0, flags=4)[1])

    def test_the_head_flag_lets_it_through_the_head(self):
        self.assertIsNone(self.hit(60.0, 440.0, 140.0, 440.0, flags=2))
        self.assertEqual("head", self.hit(60.0, 440.0, 140.0, 440.0)[1])

    def test_a_rising_target_meets_a_shell_passing_above(self):
        """★★ 人正往上跳（这一格上升 20）：弹体从他头顶上方横着掠过 —— 静止的圆
        擦不到，两边一起动就撞上（`0x50bd67` 的相对扫掠）。"""
        above = 440.0 - 10.0 - 3.0 - 8.0             # 头圆 + 弹体半径之外再 8 px 的一条线
        self.assertIsNone(self.hit(60.0, above, 140.0, above))
        got = self.hit(60.0, above, 140.0, above, vy=-20.0)
        self.assertIsNotNone(got)
        self.assertEqual("head", got[1])

    def test_overlapping_at_the_start_is_t_zero(self):
        self.assertEqual((0.0, "body"), self.hit(100.0, 463.0, 140.0, 463.0))


class ShellStepBodyVelocityTests(unittest.TestCase):
    """`_shell_step` 拿到的「人这一格挪多少」：腾空取外推的速度，踩地恒 0。"""

    def room_with(self, body):
        conn = _Obj()
        conn.sim_body = body
        seat = _Obj()
        seat.is_bot = False
        seat.conn = conn
        seat.character_id = 0
        room = _Obj()
        room.seats = [seat]
        room.quest = None
        room.host_seat = None
        return room

    def shoot(self, room, y):
        weapon = weapondata.get(1000010)
        shot = ballistics.Shot(0.0, 1.0, 80.0, 4, 0.0)
        shell = bot.Shell(0, 0, weapon, 2, 60.0, y, shot, 0.0, 4)
        return bot._shell_step(room, shell, None, [(0, 100.0, 500.0, False, 0)])

    def test_a_grounded_target_does_not_move_in_the_sweep(self):
        room = self.room_with(botmove.Body(100.0, 500.0, on_ground=True))
        self.assertEqual((0.0, 0.0), bot._body_air_velocity(room, 0))
        above = 440.0 - 10.0 - 12.0
        self.assertIsNone(self.shoot(room, above))

    def test_an_airborne_target_rises_into_it(self):
        room = self.room_with(botmove.Body(100.0, 500.0, vx=0.0, vy=-20.0,
                                           on_ground=False))
        self.assertEqual((0.0, -20.0), bot._body_air_velocity(room, 0))
        above = 440.0 - 10.0 - 12.0
        landed = self.shoot(room, above)
        self.assertIsNotNone(landed)
        self.assertEqual((0, "head"), landed[1:])


if __name__ == "__main__":
    unittest.main()
