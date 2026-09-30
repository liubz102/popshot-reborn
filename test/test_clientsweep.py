#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""弹体一格扫掠 = 客户端 `0x50e759` 的整数 DDA；破坏物按客户端 `0x51a935` 的口径（X_Mod §79 / §80）。

出处：用户 2026-09-23 夜「大部分对了，偶尔还错位」。19:51 那次运行两局 420 颗逐颗重放，
可见的错位里三类是服务端的**模型**和客户端差了 1 px（不是时钟）：

* 静态墙角 —— 碎片 200070 被客户端的前伸探针在 (340,659) 撞掉，服务端浮点插值擦过去，
  588 px 外才炸；
* 破坏物的边 —— 客户端按 `.map` 里的 f32 坐标截断映射、再九格取 max，服务端按四舍五入
  的整数坐标贴、不取九格：碎片 200130 擦着那一圈被撞掉（服务端 543 px 外才炸），
  主雷 200002 在 #0 边上弹回的速度是 (−10.12, 9.22) 而客户端是 (−13.01, 4.25)；
* 鲤鱼的弯背 —— 撞点差 1 px 投票就换一组，弹向差十几度（62 次真反弹：浮点版逐位复现 23 次，
  整数版 44 次）。

`SweepSemanticsTests` 在合成地形上钉住 DDA 的每一条规则；`RealGameTests` 拿那局 `rpFire` 的
原字节（f32）重放，断言和客户端逐帧日志 `PROJ.` **逐位**一致。

★ 纯标准库 + 服务端模块，两套运行时都跑。
"""
import base64
import math
import os
import struct
import sys
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import ballistics  # noqa: E402
import bot  # noqa: E402
import mapdata  # noqa: E402
import weapondata  # noqa: E402


def _blob(raw):
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def _terrain(width, height, solid=(), breakables=()):
    """合成地形：`solid` 里的格子值 2，`breakables` 原样塞进记录。"""
    cells = bytearray((width * height + 3) // 4)
    for x, y in solid:
        i = y * width + x
        cells[i >> 2] |= 2 << ((i & 3) * 2)
    return mapdata.MapTerrain({
        "format": mapdata.FORMAT, "name": "Tiny", "version": 18,
        "width": width, "height": height,
        "cells": _blob(bytes(cells)),
        "ground_counts": _blob(struct.pack("<%dH" % width, *([0] * width))),
        "ground_ys": _blob(b""),
        "breakables": list(breakables),
    })


class SweepSemanticsTests(unittest.TestCase):
    """`bot._client_sweep()` 逐条对 `0x50e759`。半径 0 ⇒ 只有一个 (0, 0) 探针，好推算。"""

    def test_the_minor_axis_uses_truncating_integer_division(self):
        # 起点 (10.7, 20.2)、速度 (5.9, 2.3) ⇒ 端点取整 (10,20)→(16,22)，Δ=(6,2)，x 主轴。
        # 第 i 步 y = 20 + (2i)/6（截断）：i=1,2 → 20；i=3,4,5 → 21；i=6 → 精确终点 22。
        # 浮点插值在 i=2 时是 20.2 + 2.3·2/6 = 20.97 → 20，i=3 时 21.35 → 21 —— 这一条两边
        # 一致；把实心放在 (13, 20)：整数版第 3 步问的是 (13, 21)，**不撞**。
        terrain = _terrain(64, 64, solid=[(13, 20)])
        self.assertIsNone(bot._client_sweep(terrain, 10.7, 20.2, 16.6, 22.5, 0.0))
        # 放在 (13, 21)：第 3 步撞上，free 是第 2 步的线点 (12, 20)
        terrain = _terrain(64, 64, solid=[(13, 21)])
        hit = bot._client_sweep(terrain, 10.7, 20.2, 16.6, 22.5, 0.0)
        self.assertEqual((13, 21), (hit.cell_x, hit.cell_y))
        self.assertEqual((12, 20), (hit.free_x, hit.free_y))
        self.assertAlmostEqual(3.0 / 6.0, hit.t)

    def test_the_end_point_is_asked_and_free_is_the_previous_line_point(self):
        # Δ=(3,2)：第 1 步 (11,20)、第 2 步 (12,21)、第 3 步就是终点 (13,22)。
        # 撞在终点上 ⇒ free 是第 2 步的线点，用的是同一个截断公式（(2·2)/3 = 1）。
        terrain = _terrain(64, 64, solid=[(13, 22)])
        hit = bot._client_sweep(terrain, 10.0, 20.0, 13.0, 22.0, 0.0)
        self.assertEqual((13, 22), (hit.cell_x, hit.cell_y))
        self.assertEqual((12, 21), (hit.free_x, hit.free_y))
        self.assertEqual(1.0, hit.t)

    def test_the_start_counts_only_when_every_probe_is_blocked(self):
        """★ `0x50eabf`：起点那一步要**所有**探针都挡住才算「起点就撞」。

        旧版是「任一探针挡住就当场撞」—— 贴着地面 / 鱼背滚的雷在这一格两边就分叉：
        正下方探针压在地面上、前伸探针在空中，客户端照样往前扫。
        """
        # 半径 8：探针 (0, 8) 和前伸 (8, 0)（水平飞）。起点 (20, 20)：(20, 28) 实心、(28, 20) 空。
        terrain = _terrain(64, 64, solid=[(20, 28)])
        self.assertIsNone(bot._client_sweep(terrain, 20.0, 20.0, 24.0, 20.0, 8.0))
        # 两个都挡住：t=0，free = 起点，cell = 起点 + off[0]
        terrain = _terrain(64, 64, solid=[(20, 28), (28, 20)])
        hit = bot._client_sweep(terrain, 20.0, 20.0, 24.0, 20.0, 8.0)
        self.assertEqual(0.0, hit.t)
        self.assertEqual((20, 20), (hit.free_x, hit.free_y))
        self.assertEqual((20, 28), (hit.cell_x, hit.cell_y))

    def test_a_shell_that_does_not_move_a_whole_pixel_looks_one_pixel_down(self):
        # `0x50eda8`：两轴取整后都是 0 ⇒ Δy 当 1，问的是正下方那一格
        terrain = _terrain(64, 64, solid=[(20, 21)])
        hit = bot._client_sweep(terrain, 20.3, 20.4, 20.6, 20.9, 0.0)
        self.assertEqual((20, 21), (hit.cell_x, hit.cell_y))
        self.assertEqual((20, 20), (hit.free_x, hit.free_y))
        # 真的一动不动（|v| == 0）就不扫（`0x50e798`）
        self.assertIsNone(bot._client_sweep(terrain, 20.0, 20.0, 20.0, 20.0, 0.0))

    def test_moving_left_and_up_steps_with_negative_indices(self):
        # Δ=(−6,−2)：第 −3 步 y = 20 + (−2·−3)/−6 = 20 − 1 = 19
        terrain = _terrain(64, 64, solid=[(7, 19)])
        hit = bot._client_sweep(terrain, 10.2, 20.9, 4.9, 18.9, 0.0)
        self.assertEqual((7, 19), (hit.cell_x, hit.cell_y))
        self.assertEqual((8, 20), (hit.free_x, hit.free_y))

    def test_above_the_top_of_the_map_is_open(self):
        # V0.3 §83：弹体飞出图顶又落回来 —— 图顶上面一律当空（出界的左右下仍是实心）
        terrain = _terrain(64, 64)
        self.assertIsNone(bot._client_sweep(terrain, 30.0, 3.0, 36.0, -5.0, 0.0))
        self.assertIsNotNone(bot._client_sweep(terrain, 60.0, 30.0, 66.0, 30.0, 0.0))

    def test_a_zero_vote_reflects_like_flat_ground(self):
        # 客户端拿 (0, 0) 照样算 atan2 = 0 ⇒ 按平地反射（vy 取反），不是「方向不动」
        self.assertEqual((5.0, -10.0), bot._reflect_velocity(10.0, 20.0, (0, 0)))


class BreakableBulletShapeTests(unittest.TestCase):
    """破坏物的客户端形状（`Breakable.bullet_rows`，X_Mod §80）—— §105 起角色和弹体共用这一份。"""

    @staticmethod
    def _square(fx, fy, w=6, h=4):
        mask = bytearray((w * h + 3) // 4)
        for i in range(w * h):
            mask[i >> 2] |= 3 << ((i & 3) * 2)
        return {"handle": 7, "x": int(round(fx)), "y": int(round(fy)),
                "fx": fx, "fy": fy, "w": w, "h": h, "hp": 10, "regen": 15000,
                "mask": _blob(bytes(mask))}

    def test_integer_positions_keep_the_same_block(self):
        # (20, 20) 的 6×4 全实心：两份都占 x 17..22、y 18..21。九格取 max 夹在**掩码矩形内**，
        # 全实心的方块矩形内没有空格可填；X=16 那一列局部是 −1，越界。
        terrain = _terrain(64, 64, breakables=[self._square(20.0, 20.0)])
        for x, y, want in ((17, 18, 3), (16, 18, 0), (22, 21, 3), (23, 21, 0), (20, 22, 0)):
            self.assertEqual(want, terrain.cell(x, y), (x, y))
            self.assertEqual(want, terrain.bullet_cell(x, y), (x, y))

    def test_the_client_truncates_the_raw_float_position(self):
        """f32 坐标 (20.3, 20.4)，四舍五入是 (20, 20)（旧的 `rows` 那份：x 17..22、y 18..21）。

        客户端 `局部 = ftol(f32(X − 20.3) + 3)`：X=17 → −0.3 → **向零截断成 0**，X=18 → 0.7 → 0，
        … X=23 → 5.7 → 5（还在 6 列之内）⇒ x 17..23，右边多一列；y 同理下边多一行。
        角色（`cell`）和弹体（`bullet_cell`）问的是同一个 `0x473969`（X_Mod §105）。
        """
        terrain = _terrain(64, 64, breakables=[self._square(20.3, 20.4)])
        for probe in (terrain.cell, terrain.bullet_cell):
            self.assertEqual(3, probe(23, 19))              # 多出来的那一列
            self.assertEqual(3, probe(20, 22))              # 多出来的那一行
        self.assertEqual(0, terrain.bullet_cell(24, 19))
        self.assertEqual(0, terrain.bullet_cell(16, 19))     # −1.3 → −1，越界

    def test_blocks_bullet_and_the_coarse_grid_use_the_bullet_shape(self):
        # 放到 x 从 26 起：角色那份止于 31（粗网格第 1 块），弹体那份多出的 x=32 落进第 2 块 ——
        # 粗网格按角色那份标脏的话，第 2 块会被当成「保证是空的」一步跳过去。
        terrain = _terrain(64, 64, breakables=[self._square(29.3, 20.4)])
        self.assertEqual(3, terrain.cell(32, 19))
        self.assertTrue(terrain.blocks_bullet(32, 19))
        self.assertFalse(terrain.coarse_empty(32, 18, 40, 20))
        self.assertFalse(terrain.coarse_clear(32, 18, 40, 20))
        # 碎了就一起放行
        broken = terrain.variant(())
        self.assertFalse(broken.blocks_bullet(32, 19))
        self.assertTrue(broken.coarse_clear(32, 18, 40, 20))
        self.assertEqual(0, broken.bullet_cell(29, 19))

    def test_festival02_number_0_grows_a_ring_and_loses_nothing(self):
        terrain = mapdata.load("Festival02")
        if terrain is None:
            self.skipTest("没有 Festival02 的地形产物")
        b = terrain.breakables[0]
        self.assertEqual((821.7313232421875, 652.22021484375),
                         (terrain_raw(b)))
        differ = 0
        for y in range(b.top - 3, b.top + b.height + 3):
            for x in range(b.left - 3, b.left + b.width + 3):
                differ += terrain.cell(x, y) != terrain.bullet_cell(x, y)
        self.assertEqual(0, differ, "角色和弹体是同一份格子（X_Mod §105）")
        # 左边那一圈里的一格（重放里 200002 就是贴着它弹回去的那一侧）：旧的四舍五入形状里是空的。
        self.assertEqual(3, terrain.cell(802, 602))


def terrain_raw(breakable):
    """产物里存的 f32 原始坐标（FORMAT 9）：直接从记录里取，别从 `Breakable` 反推。"""
    import json
    with open(os.path.join(ROOT, "server", "bot_mapdata", "Festival02.json"),
              encoding="utf-8") as fp:
        record = json.load(fp)
    for item in record["breakables"]:
        if int(item["handle"]) == breakable.handle:
            return (item["fx"], item["fy"])
    return None


class RealGameTests(unittest.TestCase):
    """2026-09-23 19:51 那次运行局 0：`rpFire` 原字节重放，对客户端 `PROJ.` 逐帧日志。

    鲤鱼都不参与（离得远），所以不带时钟 —— 这里钉的是**地形模型**。
    """

    @classmethod
    def setUpClass(cls):
        cls.terrain = mapdata.load("Festival02")
        if cls.terrain is None:
            raise unittest.SkipTest("没有 Festival02 的地形产物")

    def _shell(self, ammo, x, y, angle, power):
        weapon = weapondata.get(ammo)
        if weapon is None:
            self.skipTest("武器表里没有 %d" % ammo)
        speed = ballistics.speed_for_power(weapon, power)
        shot = ballistics.Shot(angle, power, speed, 0.0, ballistics.gravity_per_tick(weapon))
        return bot.Shell(1, 0, weapon, 2, x, y, shot, 0.0, 45)

    def _run(self, shell, terrain, until):
        landed = None
        while shell.ticks < until and landed is None:
            landed = bot._shell_step(None, shell, terrain, [])
        return landed

    def test_200002_bounces_off_breakable_0_like_the_client(self):
        """主雷第 27 格撞 #0 的边：客户端第 28 帧 (797.00, 597.00) v(−13.01, 4.25)、
        第 29 帧 (783.99, 602.21) v(−13.01, 5.21)。旧模型弹回速度是 (−10.12, 9.22)。"""
        shell = self._shell(1000020, 199.0, 507.0, -0.42145925760269165, 46.0)
        self.assertIsNone(self._run(shell, self.terrain, 27))
        self.assertTrue(shell.bounced)
        self.assertEqual((797.0, 597.0), (shell.x, shell.y))
        self.assertAlmostEqual(-13.01, shell.vx, places=2)
        self.assertAlmostEqual(4.25, shell.vy, places=2)
        self.assertIsNone(bot._shell_step(None, shell, self.terrain, []))
        self.assertAlmostEqual(783.99, shell.x, places=2)
        self.assertAlmostEqual(602.21, shell.y, places=2)

    def test_200070_dies_on_the_static_wall_corner_at_tick_10(self):
        """碎片（1000500，撞地就炸）：客户端第 10 帧之后就没了 —— 前伸探针在 (340,659) 撞墙角。
        旧的浮点插值擦过去，一路飞到 (112, 1198) 才炸（588 px）。那时 #0 碎着。"""
        terrain = self.terrain.variant(range(1, len(self.terrain.breakables)))
        shell = self._shell(1000500, 421.4409484863281, 669.0961303710938,
                            -2.460914134979248, 10.0)
        landed = self._run(shell, terrain, 45)
        self.assertIsNotNone(landed)
        self.assertEqual(10, shell.ticks)
        self.assertEqual((348.0, 656.0), landed[0])

    def test_200130_dies_on_the_ring_of_breakable_1_at_tick_18(self):
        """碎片擦着 #1 的那一圈：客户端第 18 帧之后就没了；旧模型 543 px 外才炸。"""
        shell = self._shell(1000500, 394.67877197265625, 514.52734375,
                            -0.296705961227417, 10.0)
        landed = self._run(shell, self.terrain, 45)
        self.assertIsNotNone(landed)
        self.assertEqual(18, shell.ticks)
        self.assertEqual((561.0, 619.0), landed[0])


if __name__ == "__main__":
    unittest.main()
