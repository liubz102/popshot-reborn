# -*- coding: utf-8 -*-
"""`botnav.py` 的纯物理可达图 / A* 测试。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import botmove                                                 # noqa: E402
import botnav                                                  # noqa: E402
import mapdata                                                 # noqa: E402
from test_botmove import Dummy                                 # noqa: E402
from test_mapdata import make_record                           # noqa: E402


def terrain_from(rows, jump=None):
    return mapdata.MapTerrain(make_record(rows, jump=jump))


def solid_heights(heights, height):
    """`heights[x]` 是实心地面顶；None 表示这一列是无底洞。"""
    return ["".join("0" if top is None or y < top else "2"
                    for top in heights)
            for y in range(height)]


class NeighborTests(unittest.TestCase):

    def setUp(self):
        self.who = Dummy(7.0)

    def test_drop_is_an_explicit_edge_only_on_one_way_ground(self):
        rows = []
        for y in range(160):
            if y == 60:
                rows.append("1" * 240)
            elif y >= 130:
                rows.append("2" * 240)
            else:
                rows.append("0" * 240)
        terrain = terrain_from(rows)
        edges = botnav.neighbors(terrain, botmove.Body(100.0, 60.0), self.who)
        drops = [(body, step) for body, step in edges
                 if step.action == botnav.ACTION_DROP]
        self.assertEqual(1, len(drops))
        self.assertAlmostEqual(130.0, drops[0][0].y)

        solid_edges = botnav.neighbors(
            terrain, botmove.Body(100.0, 130.0), self.who)
        self.assertFalse(any(step.action == botnav.ACTION_DROP
                             for _body, step in solid_edges))

    def test_a_jump_edge_is_kept_only_after_it_lands(self):
        terrain = terrain_from(solid_heights([180] * 420, 220))
        edges = botnav.neighbors(terrain, botmove.Body(80.0, 180.0), self.who)
        jumps = [(body, step) for body, step in edges
                 if step.action == botnav.ACTION_JUMP]
        self.assertTrue(jumps)
        self.assertTrue(all(body.on_ground for body, _step in jumps))


class AStarTests(unittest.TestCase):

    def setUp(self):
        self.who = Dummy(7.0)

    def test_flat_ground_gets_a_finite_route(self):
        terrain = terrain_from(solid_heights([180] * 480, 220))
        path = botnav.plan(terrain, botmove.Body(60.0, 180.0), self.who,
                           (400.0, 180.0))
        self.assertTrue(path)
        self.assertLessEqual(abs(path[-1].x - 400.0), botnav.GOAL_X)
        self.assertLessEqual(abs(path[-1].y - 180.0), botnav.GOAL_Y)

    def test_it_finds_a_jump_onto_a_high_platform(self):
        heights = [200] * 180 + [80] * 300
        terrain = terrain_from(solid_heights(heights, 240))
        path = botnav.plan(terrain, botmove.Body(130.0, 200.0), self.who,
                           (280.0, 80.0))
        self.assertTrue(path, "120 单位高台低于普通跳 167 的顶点，必须可达")
        self.assertTrue(any(step.action == botnav.ACTION_JUMP for step in path))
        self.assertLessEqual(abs(path[-1].y - 80.0), botnav.GOAL_Y)

    def test_it_jumps_across_a_bottomless_gap(self):
        heights = [180] * 150 + [None] * 80 + [180] * 250
        terrain = terrain_from(solid_heights(heights, 220))
        path = botnav.plan(terrain, botmove.Body(100.0, 180.0), self.who,
                           (320.0, 180.0))
        self.assertTrue(path)
        self.assertTrue(any(step.action == botnav.ACTION_JUMP for step in path))
        self.assertGreater(path[-1].x, 230.0)

    def test_it_uses_drop_to_reach_the_floor_below_a_wire(self):
        rows = []
        for y in range(170):
            if y == 60:
                rows.append("1" * 260)
            elif y >= 140:
                rows.append("2" * 260)
            else:
                rows.append("0" * 260)
        terrain = terrain_from(rows)
        path = botnav.plan(terrain, botmove.Body(120.0, 60.0), self.who,
                           (120.0, 140.0))
        self.assertTrue(path)
        self.assertEqual(botnav.ACTION_DROP, path[0].action)
        self.assertAlmostEqual(140.0, path[-1].y)

    def test_a_double_jump_climbs_what_one_jump_cannot(self):
        # 240 高的整面墙**超过**一段跳的顶点（20²/2.4 = 167），
        # 但两段跳（顶点再置一次 v.y = 24）够得着 —— 原版真人就是这么上去的。
        heights = [260] * 150 + [20] * 250
        terrain = terrain_from(solid_heights(heights, 280))
        path = botnav.plan(terrain, botmove.Body(100.0, 260.0), self.who,
                           (260.0, 20.0))
        self.assertTrue(path, "两段跳够得着 240 的高台")
        self.assertTrue(any(step.action == botnav.ACTION_DOUBLE_JUMP
                            for step in path))
        self.assertLessEqual(abs(path[-1].y - 20.0), botnav.GOAL_Y)

    def test_an_unreachable_goal_still_walks_as_close_as_it_can(self):
        """★ 够不着的目标不再空手而归 —— 走到能走到的最近处（会话 41）。

        闯关模式里「bot 卡在最左边不往前走、拖着全队进度」就是老行为
        （`plan()` 返回空 -> 调用方退回「坑前停下」）造成的。
        """
        # 600 高的整面墙：两段跳的极限也只有 407，谁也上不去。
        heights = [620] * 150 + [20] * 250
        terrain = terrain_from(solid_heights(heights, 640))
        start = botmove.Body(60.0, 620.0)
        path = botnav.plan(terrain, start, self.who, (300.0, 20.0))
        self.assertTrue(path, "上不去也该往墙根挪")
        self.assertTrue(all(step.action != botnav.ACTION_DOUBLE_JUMP
                            or step.y > 20.0 for step in path))
        # 落脚点确实比出发点更靠近目标（水平方向朝墙走）。
        self.assertGreater(path[-1].x, start.x)
        self.assertLess(path[-1].x, 150.0)

    def test_it_gives_up_when_it_cannot_get_any_closer(self):
        # 站在一条 1 格宽的柱子上，两边都是无底洞 —— 一步都靠近不了。
        rows = []
        for y in range(200):
            if y >= 100:
                rows.append("0" * 60 + "2" * 8 + "0" * 60)
            else:
                rows.append("0" * 128)
        terrain = terrain_from(rows)
        path = botnav.plan(terrain, botmove.Body(64.0, 100.0), self.who,
                           (1200.0, 100.0))
        self.assertEqual((), path)


class GraphCacheTests(unittest.TestCase):
    """★ 可达图缓存（会话 41）：同一张图 + 同一类角色只算一遍。"""

    def setUp(self):
        self.who = Dummy(7.0)
        self.terrain = terrain_from(solid_heights([180] * 480, 220))

    def test_edges_are_remembered_per_terrain_and_scale(self):
        graph = botnav.graph_of(self.terrain, self.who)
        self.assertEqual({}, graph)
        botnav.plan(self.terrain, botmove.Body(60.0, 180.0), self.who,
                    (400.0, 180.0))
        self.assertTrue(graph, "规划过一次之后图里该有落脚点了")
        # 同一份缓存对象，不是每次新建。
        self.assertIs(graph, botnav.graph_of(self.terrain, self.who))
        # 走速不同的角色是另一张图。
        other = botnav.graph_of(self.terrain, Dummy(8.0))
        self.assertIsNot(graph, other)

    def test_two_terrains_with_the_same_name_do_not_share(self):
        twin = terrain_from(solid_heights([180] * 480, 220))
        self.assertEqual(self.terrain.name, twin.name)
        self.assertIsNot(botnav.graph_of(self.terrain, self.who),
                         botnav.graph_of(twin, self.who))

    def test_warm_fills_the_whole_reachable_component(self):
        seed = botmove.settle(self.terrain, botmove.Body(60.0, 180.0),
                              self.who)
        count = botnav.warm(self.terrain, self.who, [seed])
        self.assertGreater(count, 10)
        graph = botnav.graph_of(self.terrain, self.who)
        self.assertEqual(count, len(graph))
        # 再预热一遍不会多算（幂等）。
        botnav.warm(self.terrain, self.who, [seed])
        self.assertEqual(count, len(graph))


if __name__ == "__main__":
    unittest.main()


class JumpPadRouteTests(unittest.TestCase):
    """★★★ **弹跳台要进路径规划**（用户 2026-08-30）。

    台子本来就在图里（`_pad_edge`），这一批把它钉住，并补上会话 41 新加的
    「台子弹上去之后在顶点再补一段跳」—— 光靠台子够不着的那一层，
    台子 + 二段跳够得着。
    """

    def setUp(self):
        self.who = Dummy(7.0)

    def terrain(self, pad_dy=-590.0, pad_dx=250.0, ledge_top=100):
        """低处一片地（y=650）+ 高处一片台面（x≥500, y=ledge_top）+ 一个台子。

        台面比地面高 550 —— **一段跳 167、两段跳 407 都够不着**，
        只有走上弹跳台才上得去。
        """
        rows = []
        for y in range(700):
            rows.append("".join(
                "2" if (y >= 650 or (500 <= x < 880 and y >= ledge_top))
                else "0" for x in range(900)))
        return terrain_from(rows, jump=[(200.0, 650.0, pad_dx, pad_dy)])

    def test_the_pad_is_the_only_way_up_and_a_star_takes_it(self):
        terrain = self.terrain()
        start = botmove.settle(terrain, botmove.Body(120.0, 650.0), self.who)
        # 先确认「不用台子」真的上不去。
        without = botnav.plan(terrain, start, self.who, (700.0, 100.0))
        self.assertTrue(without, "best-effort 至少该给一条路")
        path = botnav.plan(terrain, start, self.who, (700.0, 100.0))
        self.assertTrue(path)
        self.assertAlmostEqual(100.0, path[-1].y, delta=botnav.GOAL_Y,
                               msg="该真的站上高台，实际落在 y=%.0f"
                                   % path[-1].y)

    def test_without_the_pad_the_ledge_is_unreachable(self):
        """★ 对照组：把台子拿掉，同一张图就上不去了 —— 证明上面那条走的是台子。"""
        rows = []
        for y in range(700):
            rows.append("".join(
                "2" if (y >= 650 or (500 <= x < 880 and y >= 100))
                else "0" for x in range(900)))
        terrain = terrain_from(rows)          # 没有 jump pads
        start = botmove.settle(terrain, botmove.Body(120.0, 650.0), self.who)
        path = botnav.plan(terrain, start, self.who, (700.0, 100.0))
        self.assertTrue(all(step.y > 200.0 for step in path),
                        "没有台子就不该出现落在高台上的边")

    def test_a_pad_edge_can_carry_a_second_jump(self):
        """★★ 台子把人弹到 `dy`，顶点再补一段跳还能再上 240（§124）。

        图：地面 y=650，台子把人弹到 y≈350；
        y=200 那一行是**单向平台**（白线，往上能穿）。
        光靠台子最高只到 350 —— 到不了那条白线；
        顶点再跳一段才翻得上去。
        """
        rows = []
        for y in range(700):
            if y == 200:
                rows.append("0" * 400 + "1" * 480 + "0" * 20)
            elif y >= 650:
                rows.append("2" * 900)
            else:
                rows.append("0" * 900)
        terrain = terrain_from(rows, jump=[(200.0, 650.0, 250.0, -300.0)])
        onpad = botmove.settle(terrain, botmove.Body(200.0, 650.0), self.who)
        edges = botnav.neighbors(terrain, onpad, self.who)
        pads = [step for _body, step in edges
                if step.action == botnav.ACTION_PAD]
        self.assertTrue(any(step.double for step in pads),
                        "该有一条『台子 + 二段跳』的边")
        plain = min(step.y for step in pads if not step.double)
        boosted = min(step.y for step in pads if step.double)
        self.assertAlmostEqual(650.0, plain, delta=4.0,
                               msg="光靠台子只能掉回地面")
        self.assertAlmostEqual(200.0, boosted, delta=4.0,
                               msg="补一段跳该翻上白线，实际落在 y=%.0f" % boosted)

    def test_the_first_tick_of_a_pad_edge_never_presses_jump(self):
        """★ 按了跳人就先离地，台子那一句根本轮不到 —— 台子白站。"""
        terrain = self.terrain()
        onpad = botmove.settle(terrain, botmove.Body(200.0, 650.0), self.who)
        launched = botmove.tick(terrain, onpad, self.who)
        self.assertFalse(launched.on_ground, "什么都不按才会被台子弹出去")
        self.assertLess(launched.vy, -botmove.JUMP_SPEED,
                        "台子的初速要比普通起跳快得多")
        # 按了跳 = 普通起跳（重力已经加过一次），与台子无关。
        jumped = botmove.tick(terrain, onpad, self.who, want_jump=True)
        self.assertAlmostEqual(-botmove.JUMP_SPEED + botmove.GRAVITY,
                               jumped.vy, places=3)
