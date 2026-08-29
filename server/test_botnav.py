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

    def test_an_unreachable_goal_returns_no_route(self):
        # 240 高的整面墙超过普通跳顶点，图里也没有弹跳台或绕路。
        heights = [260] * 150 + [20] * 250
        terrain = terrain_from(solid_heights(heights, 280))
        path = botnav.plan(terrain, botmove.Body(100.0, 260.0), self.who,
                           (260.0, 20.0), max_expansions=300)
        self.assertEqual((), path)


if __name__ == "__main__":
    unittest.main()
