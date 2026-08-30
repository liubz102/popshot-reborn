#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""地图地形数据的测试（`server/mapdata.py` + `tools/mapdata.py`）。

分两层：

1. **合成数据**（`SyntheticTests` 起）—— 自己造一张 8x8 的小图，把
   取格、出界、站立面、落地、弹道这几件事逐条钉死。这一层**不依赖**
   `bot_mapdata/` 里那 2 MB 产物，任何机器上都跑得动。
2. **真实产物**（`RealDataTests`）—— 产物在的话再跑：174 张全都能加载、
   站立面满足「本格实心 且 正上方是空」、出生点不在墙里。
   产物不在就整类跳过（打包机上可能还没跑提取）。

★ 最容易写反的一条：**出界是「实心」不是「空」**（客户端 `0x472fe0`
越界返回 2）。写反了 bot 会觉得图外能走，一路走出地图。
"""
import base64
import json
import os
import struct
import sys
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import mapdata                                                 # noqa: E402


# ----------------------------------------------------------------------------
# 造一张小图：和 tools/mapdata.py 的产物同格式
# ----------------------------------------------------------------------------
def pack_cells(rows):
    """`rows` 是一行一个字符串，每个字符是 '0'..'3'。返回 2bit/格 的字节串。"""
    flat = [int(ch) for row in rows for ch in row]
    out = bytearray((len(flat) + 3) // 4)
    for i, v in enumerate(flat):
        out[i >> 2] |= v << ((i & 3) * 2)
    return bytes(out)


def ground_from(rows):
    """站立面：本格非空且正上方是空。y=0 那一行不算（头顶算图外 = 实心）。"""
    height, width = len(rows), len(rows[0])
    per_col = [[] for _ in range(width)]
    for y in range(1, height):
        for x in range(width):
            if rows[y][x] != "0" and rows[y - 1][x] == "0":
                per_col[x].append(y)
    return [len(c) for c in per_col], [y for c in per_col for y in c]


def blob(raw):
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def make_record(rows, name="Tiny", version=18, points=None, jump=None):
    height, width = len(rows), len(rows[0])
    counts, ys = ground_from(rows)
    return {
        "format": mapdata.FORMAT,
        "name": name,
        "version": version,
        "width": width,
        "height": height,
        "cells": blob(pack_cells(rows)),
        "ground_counts": blob(struct.pack("<%dH" % width, *counts)),
        "ground_ys": blob(struct.pack("<%dH" % len(ys), *ys)),
        "points": points or {},
        "jump": jump or [],
    }


#    x= 01234567
ROWS = ["00000000",   # y=0
        "00000000",   # y=1
        "00022000",   # y=2  一块浮空的实心
        "00022000",   # y=3
        "00000000",   # y=4
        "11111111",   # y=5  一整条薄板（值 1）
        "00000000",   # y=6
        "22222222"]   # y=7  地面


class SyntheticTests(unittest.TestCase):

    def setUp(self):
        self.t = mapdata.MapTerrain(make_record(ROWS))

    def test_size_and_name(self):
        self.assertEqual((8, 8), (self.t.width, self.t.height))
        self.assertEqual("Tiny", self.t.name)

    def test_cell_values_round_trip(self):
        for y, row in enumerate(ROWS):
            for x, ch in enumerate(row):
                self.assertEqual(int(ch), self.t.cell(x, y),
                                 "格 (%d,%d) 对不上" % (x, y))

    def test_out_of_bounds_is_solid(self):
        # ★ 照抄客户端 0x472fe0：越界返回 2，不是 0。
        for xy in ((-1, 0), (0, -1), (8, 0), (0, 8), (99, 99)):
            self.assertEqual(mapdata.OUT_OF_BOUNDS, self.t.cell(*xy))
            self.assertTrue(self.t.is_solid(*xy))

    def test_is_solid_counts_one_way_platforms(self):
        # 值 1 是单向平台（游戏里那种细白线），**挡人** —— 站得上去。
        self.assertTrue(self.t.is_solid(0, 5))
        self.assertFalse(self.t.is_solid(0, 4))
        self.assertTrue(self.t.is_one_way(0, 5))
        self.assertFalse(self.t.is_one_way(3, 7))

    def test_one_way_platform_does_not_block_bullets(self):
        # ★ 挡人 ≠ 挡子弹。19 个弹体类的那个开关全是默认 return false，
        #   所以值 1 那条分支永远走不到（§29）。写反了 bot 会觉得
        #   隔着一根白线打不到人。
        self.assertTrue(self.t.is_solid(0, 5))
        self.assertFalse(self.t.blocks_bullet(0, 5))
        # 实心和出界照样挡子弹。
        self.assertTrue(self.t.blocks_bullet(3, 7))
        self.assertTrue(self.t.blocks_bullet(-1, 0))

    def test_a_breakable_blocks_walking_and_bullets(self):
        """★★★ 值 3 = **可破坏物**（冰块 / 木箱，V0.3 §136）。

        没打碎之前它就是一堵墙：挡人、挡子弹、**按 ↓ 穿不过去**。
        用户 2026-08-30 实机：`Iceria00` 上那两处封着冰的窟窿，
        真人上不去下不来，bot 却大摇大摆穿过去 —— 就是因为产物里那一块
        原本是**空的**（客户端把破坏物当独立对象做碰撞，没烘进主网格）。
        """
        rows = ["00000000",
                "00033000",
                "00033000",
                "00000000",
                "22222222"]
        t = mapdata.MapTerrain(make_record(rows, name="Ice"))
        self.assertTrue(t.is_solid(3, 1))
        self.assertTrue(t.blocks_bullet(3, 1))
        self.assertFalse(t.is_one_way(3, 1))
        # 冰块顶上站得住人 —— 那也是站立面。
        self.assertEqual((1, 4), tuple(t.surfaces(3)))
        # 子弹打不穿。
        self.assertTrue(t.line_blocked(3, 0, 3, 4, step=1))

    def test_line_of_fire_passes_through_a_one_way_platform(self):
        # y=5 整行都是单向平台：往下打、往上打都该打得穿。
        self.assertFalse(self.t.line_blocked(0, 4, 0, 6, step=1))
        self.assertFalse(self.t.line_blocked(0, 6, 0, 4, step=1))

    def test_surfaces_are_top_edges(self):
        # x=3 这一列：浮空块顶(y=2)、薄板(y=5)、地面(y=7)。
        self.assertEqual((2, 5, 7), tuple(self.t.surfaces(3)))
        # x=0 没有浮空块。
        self.assertEqual((5, 7), tuple(self.t.surfaces(0)))

    def test_surfaces_out_of_range_column(self):
        self.assertEqual((), tuple(self.t.surfaces(-1)))
        self.assertEqual((), tuple(self.t.surfaces(8)))

    def test_ground_below_picks_first_at_or_under(self):
        self.assertEqual(2, self.t.ground_below(3, 0))
        self.assertEqual(5, self.t.ground_below(3, 3))
        self.assertEqual(7, self.t.ground_below(3, 6))
        self.assertIsNone(self.t.ground_below(3, 8))

    def test_ground_above(self):
        self.assertIsNone(self.t.ground_above(3, 2))
        self.assertEqual(2, self.t.ground_above(3, 3))
        self.assertEqual(5, self.t.ground_above(3, 6))

    def test_first_solid_skips_one_way_platforms(self):
        """★★ 掉落物**穿过**单向平台（§114）—— 站立面不是它的判据。

        x=0 这一列最上面的站立面是 y=5 那条白线，可一件道具会一直掉到
        y=7 的地面上。按站立面刷道具，服务端记的位置就会比屏幕上高一层。
        """
        self.assertEqual(5, self.t.surfaces(0)[0])       # 人站白线上
        self.assertEqual(7, self.t.first_solid(0))       # 东西落到地面上
        # x=3 最上面就是实心浮空块，两个判据一致。
        self.assertEqual(2, self.t.surfaces(3)[0])
        self.assertEqual(2, self.t.first_solid(3))

    def test_first_solid_looks_inside_a_run(self):
        """白线**直接压在**实心上时，那一段只报一个站立面 —— 得往里找。"""
        rows = ["0000",
                "0000",
                "1111",      # 白线
                "2222",      # 紧贴着的实心
                "0000"]
        t = mapdata.MapTerrain(make_record(rows))
        self.assertEqual((2,), tuple(t.surfaces(1)))     # 只有一个站立面
        self.assertEqual(3, t.first_solid(1))            # 东西停在实心那一格

    def test_first_solid_returns_none_for_a_sky_column(self):
        t = mapdata.MapTerrain(make_record(["0000", "0000", "0000"]))
        self.assertIsNone(t.first_solid(2))
        # 整列只有白线的一样没有落点。
        t2 = mapdata.MapTerrain(make_record(["0000", "1111", "0000"]))
        self.assertIsNone(t2.first_solid(2))

    def test_line_blocked_through_the_block(self):
        # 从浮空块左边穿到右边，中间隔着 x=3..4 的实心。
        self.assertTrue(self.t.line_blocked(0, 2, 7, 2, step=1))
        # 同一行往上一格是空的。
        self.assertFalse(self.t.line_blocked(0, 1, 7, 1, step=1))

    def test_line_blocked_ignores_endpoints(self):
        # 枪口和目标常常**站**在实心上，端点自己不该算「被挡」：
        # (3,3) 是浮空块的底、(3,7) 是地面，中间只有一条单向平台。
        self.assertFalse(self.t.line_blocked(3, 3, 3, 7, step=1))
        # 真挡在中间的就要算：(3,1) -> (3,4) 穿过实心的 (3,2)/(3,3)。
        self.assertTrue(self.t.line_blocked(3, 1, 3, 4, step=1))

    def test_points_are_ints(self):
        rec = make_record(ROWS, points={"101": [[10, 20]], "108": [[3, 4]]})
        t = mapdata.MapTerrain(rec)
        self.assertEqual([(10, 20)], t.points[101])
        self.assertEqual([(3, 4)], t.points[108])


class ResolveTests(unittest.TestCase):
    """名字解析：`A:Mode` 切掉模式、基名退到难度版本。"""

    def setUp(self):
        self.store = mapdata._Store(data_dir="__不存在的目录__")
        self.store._index = {
            "maps": {"Megatron_b": {}, "Boss00#Easy": {},
                     "Boss00#Normal": {}, "Quest09#Extreme": {}},
            "bases": {"Boss00": ["Boss00#Easy", "Boss00#Normal"],
                      "Quest09": ["Quest09#Extreme"]},
        }

    def test_exact(self):
        self.assertEqual("Megatron_b", self.store.resolve("Megatron_b"))

    def test_mode_suffix_is_stripped(self):
        self.assertEqual("Megatron_b", self.store.resolve("Megatron_b:NewPvp"))

    def test_base_prefers_normal(self):
        # ★ 四个难度版本不保证是同一份地形，所以顺序写死，别靠字典序。
        self.assertEqual("Boss00#Normal", self.store.resolve("Boss00"))

    def test_base_falls_through_to_any_variant(self):
        self.assertEqual("Quest09#Extreme", self.store.resolve("Quest09"))

    def test_unknown(self):
        self.assertIsNone(self.store.resolve("没有这张图"))
        self.assertIsNone(self.store.resolve(""))
        self.assertIsNone(self.store.resolve(":NewPvp"))

    def test_missing_index_is_not_fatal(self):
        # 没有产物时服务端照样要起得来 —— bot 退回「回放真人轨迹」(D16)。
        store = mapdata._Store(data_dir=os.path.join(HERE, "__没有这个目录__"))
        self.assertEqual([], store.available())
        self.assertIsNone(store.load("Megatron_b"))


class FormatGuardTests(unittest.TestCase):
    """格式版本对不上就当没有数据，不要按错的布局硬解。"""

    def test_wrong_index_format(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "index.json"), "w", encoding="utf-8") as fp:
            json.dump({"format": mapdata.FORMAT + 99, "maps": {"A": {}}}, fp)
        store = mapdata._Store(data_dir=tmp)
        self.assertEqual([], store.available())


class RealDataTests(unittest.TestCase):
    """真产物在的话再跑。打包机上可能还没跑提取 —— 那就整类跳过。"""

    @classmethod
    def setUpClass(cls):
        cls.store = mapdata._Store()
        cls.names = cls.store.available()
        if not cls.names:
            raise unittest.SkipTest(
                "没有 bot_mapdata/ 产物，先跑 tools\\update-mapdata.bat")

    def test_every_map_loads(self):
        bad = []
        for name in self.names:
            terrain = self.store.load(name)
            if terrain is None or terrain.width <= 0 or terrain.height <= 0:
                bad.append(name)
        self.assertEqual([], bad)

    def test_surfaces_are_real_top_edges(self):
        # 抽查若干张：每个站立面都必须「本格实心 且 正上方是空」。
        for name in self.names[::17]:
            terrain = self.store.load(name)
            for x in range(0, terrain.width, 37):
                for y in terrain.surfaces(x):
                    self.assertNotEqual(
                        0, terrain.cell(x, y),
                        "%s (%d,%d) 站立面却是空的" % (name, x, y))
                    self.assertEqual(
                        0, terrain.cell(x, y - 1),
                        "%s (%d,%d) 上方也是实心，不该算上沿" % (name, x, y))

    def test_the_ice_on_iceria00_is_in_the_grid(self):
        """★★★ `Iceria00` 那两处冰块必须在网格里（V0.3 §136）。

        它们在 `.map` 里是三个 `BreakableObj`，位置 (1054,478) / (1507,478)
        / (1280,356)，形状在文件尾部那张掩码表里。产物是 FORMAT 3 才有。
        """
        terrain = self.store.load("Iceria00")
        if terrain is None:
            self.skipTest("产物里没有 Iceria00")
        for x, y in ((1054, 478), (1507, 478), (1280, 400)):
            self.assertEqual(3, terrain.cell(x, y),
                             "(%d,%d) 该是冰块" % (x, y))
            self.assertTrue(terrain.blocks_bullet(x, y))

    def test_no_map_has_a_breakable_free_column_where_ice_used_to_be(self):
        """全 174 张：值 3 只该出现在有破坏物的图上，而且**真的出现了**。"""
        with_ice = [name for name in self.names
                    if any(self.store.load(name).cell(x, y) == 3
                           for x in range(0, self.store.load(name).width, 13)
                           for y in range(0, self.store.load(name).height, 13))]
        # 677 个破坏物分布在 67 张图上；13 像素的抽样必然漏掉一些小件，
        # 所以只钉「明显有」的下限，防止哪天整层又丢了。
        self.assertGreaterEqual(len(with_ice), 40,
                                "带破坏物的图只剩 %d 张了" % len(with_ice))

    def test_spawn_points_are_not_inside_walls(self):
        # 出生点该悬在空中或贴着地面，绝不该埋在实心里。
        inside = []
        for name in self.names:
            terrain = self.store.load(name)
            for otype in (101, 102, 108):
                for (x, y) in terrain.points.get(otype, ()):
                    if 0 <= x < terrain.width and 0 <= y < terrain.height:
                        if terrain.cell(x, y) == 2:
                            inside.append((name, otype, x, y))
        # 174 张图里实测只有个别几个，钉一个上限防止将来整体走样。
        self.assertLessEqual(len(inside), 16, "埋在墙里的出生点变多了：%r"
                             % (inside[:8],))

    def test_index_and_files_agree(self):
        index = self.store.index()
        for name, entry in index["maps"].items():
            path = os.path.join(self.store.data_dir, entry["file"])
            self.assertTrue(os.path.isfile(path), "索引里的 %s 不存在" % path)

    def test_map_name_with_mode_suffix_resolves(self):
        # 房间里的地图串长这样：`Megatron_b:NewPvp`。
        if "Megatron_b" in self.names:
            self.assertIsNotNone(self.store.load("Megatron_b:NewPvp"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class BreakableTerrainTests(unittest.TestCase):
    """★★★ 可破坏物：打碎了要**放行**，过一阵原样长回来（V0.3 §138）。

    用户 2026-08-30：「真人对战时，也是一个人破坏之后，其他人就可以通过了。
    过一段时间后，恢复原状，所有人无法通过，需要再次破坏。」
    """

    #    x= 01234567
    ROWS = ["00000000",   # y=0
            "00000000",   # y=1
            "00000000",   # y=2
            "11111111",   # y=3  一整条**单向平台**（白线）
            "00000000",   # y=4
            "00000000",   # y=5
            "22222222"]   # y=6  地面

    def record(self, alive_mask=("0011110000",)):
        # 一件 4x3 的破坏物，中心 (4, 3) —— 正好罩住白线 x=2..5。
        mask_rows = ["3333", "3333", "3333"]
        rec = make_record(self.ROWS, name="Ice")
        rec["breakables"] = [{
            "x": 4, "y": 3, "w": 4, "h": 3, "hp": 40, "regen": 15000,
            "mask": blob(pack_cells(mask_rows)),
        }]
        return rec

    def setUp(self):
        self.t = mapdata.MapTerrain(self.record())

    def test_intact_it_is_a_wall(self):
        # 中心 (4,3)、4x3 ⇒ 左上角 (2,2)，盖住 x=2..5 / y=2..4。
        for x in range(2, 6):
            for y in range(2, 5):
                self.assertEqual(3, self.t.cell(x, y), "(%d,%d)" % (x, y))
        self.assertTrue(self.t.is_solid(3, 3))
        self.assertTrue(self.t.blocks_bullet(3, 3))
        self.assertFalse(self.t.is_one_way(3, 3))
        # 冰顶上站得住；冰没盖到的地方还是原来那根白线。
        self.assertEqual(2, self.t.surfaces(3)[0])
        self.assertEqual(3, self.t.surfaces(0)[0])

    def test_broken_it_reveals_the_one_way_platform(self):
        broken = self.t.variant([])
        self.assertEqual(0, broken.cell(3, 2))
        self.assertEqual(1, broken.cell(3, 3), "碎了该露出白线")
        self.assertTrue(broken.is_one_way(3, 3))
        self.assertFalse(broken.blocks_bullet(3, 3))
        self.assertEqual(3, broken.surfaces(3)[0])

    def test_the_same_state_gives_the_same_object(self):
        """★ `botnav` 的可达图缓存按地形对象做键 —— 同一状态必须同一对象。"""
        self.assertIs(self.t.variant([]), self.t.variant([]))
        self.assertIs(self.t, self.t.variant([0]), "全都在 = 根那一份")

    def test_the_untouched_columns_are_untouched(self):
        broken = self.t.variant([])
        for x in (0, 1, 6, 7):
            self.assertEqual(tuple(self.t.surfaces(x)),
                             tuple(broken.surfaces(x)), "第 %d 列不该动" % x)

    def test_breakable_fields_come_from_the_map(self):
        item = self.t.breakables[0]
        self.assertEqual(40, item.hp)
        self.assertEqual(15000, item.regen_ms)
        self.assertEqual((2, 2), (item.left, item.top))
        self.assertEqual(0.0, item.distance_to(3, 3))
        self.assertAlmostEqual(2.0, item.distance_to(8, 3))
