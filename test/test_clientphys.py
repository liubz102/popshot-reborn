#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端角色的腾空物理（X_Mod §87 / §105）：`botmove.client_air_tick` / `client_probes`。

bot 自己走、外推真人、可达图用的都是它（`botmove.frame` 的第 ③ 步）。
钉的是逐指令逆出来的几件事（`0x50d58a` → `0x50e759` → `Character` vf+0xa8 = `0x502df4` → `0x50efd2`）：

1. 探针 = 脚底 + 腿 / 身 / 头各自沿速度方向的前沿点；单向平台只挡脚底、只在往下走时挡；
2. 撞上了**这一格位置不动**，只改速度：往下走且 |v| ≤ 35 ⇒ 落地（落在扫掠的**空点**上，
   往下找不到地就只往下出溜几格）—— 和哪个探针撞上无关；否则按 7×7 投票反射
   （切向 ×0.5、法向 ×−0.2），再 `vx *= 0.3`；上升计时器在跑时另一支（`test_botmove`）；
3. 出界四面都是实心（`0x472fe0`），图顶也挡。

★ 「起跳后那一格只按速度挪、不加重力」是截断的假象（§102），那一支属于弹跳台（`test_botmove`）。
★ 纯标准库，两套运行时都跑。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, HERE)

import botmove  # noqa: E402

f32 = botmove._f32
import chrprops  # noqa: E402
import mapdata  # noqa: E402
from test_mapdata import make_record  # noqa: E402

WHO = chrprops.get(0)               # 腿 12、身 13、头 10：圆心在脚上 12 / 37 / 60
W, H = 200, 200
FLOOR = 150                         # y ≥ 150 实心


def terrain(ceiling=None, wall=None, one_way=None):
    """地面 y ≥ 150；`ceiling`：y < 它的整行实心；`wall`：x ≥ 它的整列实心；
    `one_way`：(y, x0, x1) 一条单向平台。"""
    rows = []
    for y in range(H):
        row = []
        for x in range(W):
            if y >= FLOOR or (ceiling is not None and y < ceiling) \
                    or (wall is not None and x >= wall):
                row.append("2")
            elif one_way is not None and y == one_way[0] \
                    and one_way[1] <= x < one_way[2]:
                row.append("1")
            else:
                row.append("0")
        rows.append("".join(row))
    return mapdata.MapTerrain(make_record(rows, name="ClientPhys"))


def air(x, y, vx, vy):
    return botmove.Body(x, y, vx=vx, vy=vy, on_ground=False)


class ProbeTests(unittest.TestCase):

    def test_feet_then_legs_body_head_leading_points(self):
        """往上走：前沿点就是每个圆的顶；往右走：每个圆的右边。只有脚底挡单向平台。"""
        self.assertEqual(((0, 0, True), (0, -24, False), (0, -50, False),
                          (0, -70, False)),
                         botmove.client_probes(WHO, 0.0, -1.0))
        self.assertEqual(((0, 0, True), (12, -12, False), (13, -37, False),
                          (10, -60, False)),
                         botmove.client_probes(WHO, 5.0, 0.0))

    def test_a_size_only_character_is_stacked_the_same_way(self):
        class Sizes(object):
            size_legs, size_body, size_head = 12.0, 13.0, 10.0
        self.assertEqual(botmove.client_probes(WHO, 3.0, 4.0),
                         botmove.client_probes(Sizes(), 3.0, 4.0))


class AirTickTests(unittest.TestCase):

    def test_free_flight_adds_gravity_then_moves(self):
        body = botmove.client_air_tick(terrain(), air(100, 100, 3, -10), WHO)
        vy = f32(-10 + botmove.G32)
        self.assertEqual((103.0, f32(100 + vy)), (body.x, body.y))
        self.assertEqual(vy, body.vy)
        self.assertFalse(body.on_ground)

    def test_landing_puts_the_feet_on_the_free_point_above_the_floor(self):
        """★ 落在扫掠的空点（实心第一行的上面一格）、踩地、速度清零 —— 心跳里站着的人
        脚也是比实心区第一行高 1 px（云桥静态地面 229 / 377 发）。"""
        land = terrain()
        body = air(100, 140, 2, 8)
        for _ in range(3):
            body = botmove.client_air_tick(land, body, WHO)
            if body.on_ground:
                break
        self.assertTrue(body.on_ground)
        self.assertEqual(float(FLOOR - 1), body.y)
        self.assertEqual((0.0, 0.0), (body.vx, body.vy))

    def test_falling_faster_than_35_bounces_off_the_floor(self):
        """★ |v| > 35 不算落地：这一格原地不动，平地上 `(0.5·vx·0.3, −0.2·vy)`。"""
        body = botmove.client_air_tick(terrain(), air(100, 120, 4, 40), WHO)
        self.assertEqual((100.0, 120.0), (body.x, body.y), "撞上的那一格位置不动")
        self.assertFalse(body.on_ground)
        self.assertAlmostEqual(-0.2 * 41.2, body.vy, places=5)
        self.assertAlmostEqual(4 * 0.5 * 0.3, body.vx, places=5)

    def test_the_head_hits_the_ceiling_and_the_body_stays_put(self):
        """★★ 往上撞顶：这一格**不挪**（不是挪到贴着顶），速度掉头成 0.2 倍往下。
        V0.3 的 `_air_tick` 是「停在撞之前那一点、v.y 截 0」—— 差出去的就是这一格的上升量
        （2026-09-23 局 200022：客户端脚停在离柱底 ≈ 89，服务端贴到 70）。"""
        body = botmove.client_air_tick(terrain(ceiling=20), air(100, 100, 6, -15),
                                       WHO)
        self.assertEqual((100.0, 100.0), (body.x, body.y))
        self.assertAlmostEqual(0.2 * 13.8, body.vy, places=5)
        self.assertAlmostEqual(6 * 0.5 * 0.3, body.vx, places=5)

    def test_brushing_a_wall_on_the_way_down_slides_down_a_few_pixels(self):
        """★ 往下走、速度不大时撞墙也走「落地」那一支（`[hit+0x20]` 对地形恒 −1）：
        往下找 max(5, vy) 格没找到地 ⇒ 往下出溜这几格、速度清零、仍腾空。"""
        body = botmove.client_air_tick(terrain(wall=150), air(140, 100, 5, 2),
                                       WHO)
        self.assertEqual((140.0, 105.0), (body.x, body.y))
        self.assertEqual((0.0, 0.0), (body.vx, body.vy))
        self.assertFalse(body.on_ground)

    def test_a_one_way_platform_catches_the_feet_on_the_way_down(self):
        land = terrain(one_way=(120, 50, 150))
        body = air(100, 100, 0, 5)
        for _ in range(4):
            body = botmove.client_air_tick(land, body, WHO)
            if body.on_ground:
                break
        self.assertTrue(body.on_ground)
        self.assertEqual(119.0, body.y)

    def test_rising_goes_through_a_one_way_platform(self):
        """脚底这一格从 129 扫到 116，中途穿过 y=120 那条单向平台：往上走不挡。"""
        body = botmove.client_air_tick(terrain(one_way=(120, 50, 150)),
                                       air(100, 130, 0, -15), WHO)
        self.assertEqual(f32(130 + f32(-15 + botmove.G32)), body.y)
        self.assertFalse(body.on_ground)

    def test_the_top_of_the_map_blocks_the_head(self):
        """★ 出界四面都是 2（`0x472fe0`）：头到了 y < 0 就撞上 —— 这一格不挪、往下反弹（X_Mod §105）。"""
        body = botmove.client_air_tick(terrain(), air(100, 60, 0, -15), WHO)
        self.assertEqual(60.0, body.y)
        self.assertGreater(body.vy, 0.0)


if __name__ == "__main__":
    unittest.main()
