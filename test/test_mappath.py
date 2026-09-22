#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""移动平台（X_Mod §73）—— 服务端那份 `Path::Eval` 复现对不对。

钉的是**客户端的算法**，不是「产物现在长什么样」：
位置怎么算是从 `Path::Eval`（`0x548ccd`）+ `PathPoint::Deserialize`（`0x548b81`）
逐指令抄下来的，这里用手算得出来的值把每一步都焊死。

★ 纯标准库，两套运行时都跑。
"""
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import mapdata  # noqa: E402


def _mask_blob(width, height, value=2):
    """整块实心的掩码（2 bit/格），编码成产物里那种 blob。"""
    import base64
    import zlib
    need = ((width * height + 15) // 16) * 4
    raw = bytearray(need)
    for i in range(width * height):
        raw[i >> 2] |= (value & 3) << ((i & 3) * 2)
    return base64.b64encode(zlib.compress(bytes(raw), 9)).decode("ascii")


def _mover(pts, loop=0, x=100.0, y=200.0, rider=(20, 10), scale=(1.0, 1.0),
           t_off=0, rel=0, rider_xy=(0.0, 0.0)):
    return mapdata.Mover({
        "handle": 1, "x": x, "y": y, "loop": loop, "pts": pts,
        "riders": [{"handle": 2, "w": rider[0], "h": rider[1],
                    "sx": scale[0], "sy": scale[1],
                    "x": rider_xy[0], "y": rider_xy[1],
                    "t_off": t_off, "rel": rel,
                    "mask": _mask_blob(rider[0], rider[1])}],
    })


#: 两点、零切线、ease=1 —— 原版那 24 条路径全是这个形状。
TWO_POINT = [[-50.0, 0.0, 4000, 0.0, 0.0, 1.0],
             [50.0, 0.0, 4000, 0.0, 0.0, 1.0]]


class PathEval(unittest.TestCase):
    def test_endpoints(self):
        """t=0 在第一个点上，t=段长 在第二个点上。"""
        mv = _mover(TWO_POINT)
        self.assertEqual((50.0, 200.0), mv.position_at(0))
        self.assertEqual((150.0, 200.0), mv.position_at(4000))

    def test_zero_tangent_is_smoothstep(self):
        """切线是 0 的三次 Hermite 就是 smoothstep：两头慢、正中间恰好一半。"""
        mv = _mover(TWO_POINT)
        self.assertAlmostEqual(100.0, mv.position_at(2000)[0], places=5)
        # u=0.25 -> 2u³−3u²+1 = 0.84375 留在起点，即走了 15.625%
        self.assertAlmostEqual(50.0 + 100.0 * 0.15625,
                               mv.position_at(1000)[0], places=5)
        self.assertAlmostEqual(50.0 + 100.0 * 0.84375,
                               mv.position_at(3000)[0], places=5)

    def test_loop_wraps_back_to_the_first_point(self):
        """loop=0：最后一段接回第 0 个点，一圈是所有段之和。"""
        mv = _mover(TWO_POINT)
        self.assertEqual(8000, mv.total)
        self.assertAlmostEqual(100.0, mv.position_at(6000)[0], places=5)  # 回程中点
        self.assertEqual(mv.position_at(0), mv.position_at(8000))
        self.assertEqual(mv.position_at(1234), mv.position_at(1234 + 8000))

    def test_pingpong(self):
        """loop=1：走到头**原路折回**，周期是两倍。"""
        mv = _mover(TWO_POINT, loop=1)
        # 折回的镜像点是 `2T − t − 1`（客户端 `0x548d27` 就是这么减的）
        self.assertEqual(mv.position_at(1000)[0], mv.position_at(14999)[0])
        self.assertEqual(mv.position_at(0)[0], mv.position_at(15999)[0])
        self.assertEqual(mv.position_at(0), mv.position_at(16000))

    def test_tangent_is_used(self):
        """带切线的点（`kind==1`，如 Desert01）不能再退化成 smoothstep。"""
        straight = _mover([[0.0, 0.0, 1000, 0.0, 0.0, 1.0],
                           [100.0, 0.0, 1000, 0.0, 0.0, 1.0]])
        curved = _mover([[0.0, 0.0, 1000, 0.0, 200.0, 1.0],
                         [100.0, 0.0, 1000, 0.0, 0.0, 1.0]])
        self.assertEqual(straight.position_at(500)[1], 200.0)
        self.assertGreater(curved.position_at(500)[1], 200.0)

    def test_single_point_never_moves(self):
        mv = _mover([[7.0, -3.0, 5000, 0.0, 0.0, 1.0]])
        self.assertEqual((107.0, 197.0), mv.position_at(0))
        self.assertEqual((107.0, 197.0), mv.position_at(123456))

    def test_zero_length_segment_does_not_divide_by_zero(self):
        """`01test` 里真有 `ms=0` 的段。"""
        mv = _mover([[0.0, 0.0, 3000, 0.0, 0.0, 1.0],
                     [40.0, 0.0, 0, 0.0, 0.0, 1.0]])
        # 总时长只有 3000（那个 0 段一格都不占），所以 t=3000 已经绕回起点
        self.assertEqual((100.0, 200.0), mv.position_at(3000))
        self.assertAlmostEqual(140.0, mv.position_at(2999)[0], places=1)


class MoverCollision(unittest.TestCase):
    def test_rider_follows_the_path(self):
        """碰撞跟着平台走：同一个世界点，前半程挡、后半程不挡。"""
        mv = _mover(TWO_POINT, rider=(20, 10))
        self.assertEqual(2, mv.cell_at(50, 200, 0))       # t=0 平台中心
        self.assertEqual(0, mv.cell_at(150, 200, 0))
        self.assertEqual(0, mv.cell_at(50, 200, 4000))
        self.assertEqual(2, mv.cell_at(150, 200, 4000))   # t=4000 走到右端

    def test_rider_respects_scale(self):
        """`sx/sy` 是对象缩放，掩码跟着缩 —— 不缩的话半边打不中。"""
        big = _mover(TWO_POINT, rider=(20, 10), scale=(2.0, 1.0))
        self.assertEqual(2, big.cell_at(50 + 18, 200, 0))
        small = _mover(TWO_POINT, rider=(20, 10), scale=(1.0, 1.0))
        self.assertEqual(0, small.cell_at(50 + 18, 200, 0))


class RiderPhase(unittest.TestCase):
    """每块地形自己的相位偏移 / 相对模式（§74）—— `Mover.rider_center()`。

    钉的是 `PathFollower::GetPos`（`0x549bec`）和 `GetWorldPos`（`0x47c6bf`）：
    `elapsed = Timer() + t_off − t0`；相对模式 = 自身坐标 + (Eval(elapsed) − Eval(0))。
    """

    def test_t_off_shifts_the_phase(self):
        """偏移 1000 ms 的 rider 在 t=0 时已经走到路径 t=1000 的位置。"""
        mv = _mover(TWO_POINT, t_off=1000)
        rider = mv.riders[0]
        self.assertEqual(mv.position_at(1000), mv.rider_center(rider, 0))
        self.assertEqual(mv.position_at(3500), mv.rider_center(rider, 2500))
        plain = _mover(TWO_POINT)
        self.assertEqual(plain.position_at(2500),
                         plain.rider_center(plain.riders[0], 2500))

    def test_relative_mode_adds_the_displacement_to_its_own_position(self):
        """相对模式的基线是 `Eval(0)`，**不是** `Eval(t_off)`（客户端相对分支先算一次 t=0）。"""
        mv = _mover(TWO_POINT, rel=1, rider_xy=(500.0, 600.0))
        rider = mv.riders[0]
        self.assertEqual((500.0, 600.0), mv.rider_center(rider, 0))
        # smoothstep 中点正好走了一半：位移 +50，加在自身坐标上，不是路径点 (100, 200)
        self.assertEqual((550.0, 600.0), mv.rider_center(rider, 2000))
        shifted = _mover(TWO_POINT, rel=1, rider_xy=(500.0, 600.0), t_off=2000)
        self.assertEqual((550.0, 600.0), shifted.rider_center(shifted.riders[0], 0))

    def test_cell_at_follows_the_rider_center(self):
        mv = _mover(TWO_POINT, rel=1, rider_xy=(500.0, 600.0))
        self.assertEqual(2, mv.cell_at(500, 600, 0))
        self.assertEqual(0, mv.cell_at(550, 600, 0))
        self.assertEqual(2, mv.cell_at(550, 600, 2000))
        # 路径点本身在相对模式下**没有**地形
        self.assertEqual(0, mv.cell_at(100, 200, 0))

    def test_negative_time_takes_the_c_modulo(self):
        """客户端 `Path::Eval` 取模是 `cdq/idiv`（`0x548d1a`）：t<0 余数为负，从第 0 段往前外推。"""
        self.assertEqual(-3, mapdata._cmod(-3, 10))
        self.assertEqual(-3, mapdata._cmod(-13, 10))
        self.assertEqual(7, mapdata._cmod(17, 10))
        mv = _mover(TWO_POINT)
        self.assertEqual(mv.position_at(0), mv.position_at(-8000))
        # u = −1 的 Hermite 外推：h00=−4, h01=5 ⇒ x = 100 + (−4·−50 + 5·50) = 550。
        # Python 的 `%` 会把 −4000 变成 4000 ⇒ 落在 P1 = 150，那就和客户端不一样了。
        self.assertAlmostEqual(550.0, mv.position_at(-4000)[0], places=6)


class RealMaps(unittest.TestCase):
    """拿真产物核一遍 —— 提取器和服务端两边的口径要对上。"""

    def setUp(self):
        self.terrain = mapdata.load("Festival02")
        if self.terrain is None:
            self.skipTest("没有 Festival02 的地形产物")

    def test_festival02_has_the_carp(self):
        self.assertEqual(1, len(self.terrain.movers))
        mv = self.terrain.movers[0]
        self.assertEqual(10000, mv.total)
        self.assertEqual(1, len(mv.riders))
        self.assertEqual((252, 435), (mv.riders[0].w, mv.riders[0].h))
        #: 掩码 252 宽 × 缩放 1.1 = 277
        self.assertEqual(277, mv.riders[0].sw)

    def test_carp_swings_between_the_two_path_ends(self):
        """§68 量到的是 x 693 ↔ 982、y≈735。"""
        mv = self.terrain.movers[0]
        left = mv.position_at(0)
        right = mv.position_at(5000)
        self.assertAlmostEqual(693.1, left[0], places=0)
        self.assertAlmostEqual(982.1, right[0], places=0)
        self.assertAlmostEqual(734.8, left[1], places=0)
        self.assertAlmostEqual(left[1], right[1], places=5)

    def test_blocks_bullet_only_when_a_time_is_given(self):
        """★ 不给时间就是老行为（服务端不知道有这条鱼），给了才挡。"""
        mv = self.terrain.movers[0]
        cx, cy = mv.position_at(0)
        x, y = int(cx), int(cy) + 150          # 鱼身在画布下半部
        self.assertEqual(0, self.terrain.cell(x, y))
        self.assertFalse(self.terrain.blocks_bullet(x, y))
        self.assertTrue(self.terrain.blocks_bullet(x, y, 0))
        self.assertTrue(self.terrain.is_solid(x, y, 0))
        # 半个周期之后鱼飘到另一头，同一点就空了
        self.assertFalse(self.terrain.blocks_bullet(x, y, 5000))

    def test_the_carp_is_absolute_with_no_offset(self):
        """庆典三张的挂路径对象 `t_off` / `rel` 全是 0（§74 的全库扫描）。"""
        self.assertEqual(8, mapdata.FORMAT)
        rider = self.terrain.movers[0].riders[0]
        self.assertEqual((0, 0), (rider.t_off, rider.rel))
        self.assertEqual(self.terrain.movers[0].position_at(0),
                         self.terrain.movers[0].rider_center(rider, 0))

    def test_quest_level6_rides_in_relative_mode(self):
        """全库唯一一个 `rel=1`：t=0 时就在自身坐标 (587, 1203) 上，不在路径点上。"""
        terrain = mapdata.load("Quest_level6")
        if terrain is None:
            self.skipTest("没有 Quest_level6 的地形产物")
        riders = [(mv, r) for mv in terrain.movers for r in mv.riders]
        self.assertEqual(1, len(riders))
        mv, rider = riders[0]
        self.assertEqual(1, rider.rel)
        self.assertEqual((587.0, 1203.0), mv.rider_center(rider, 0))

    def test_untitled_carries_a_5000ms_offset(self):
        """全库唯一一个 `t_off≠0`：`Untitled` 那块在 t=0 时已经走到路径 t=5000 的位置。"""
        terrain = mapdata.load("Untitled")
        if terrain is None:
            self.skipTest("没有 Untitled 的地形产物")
        riders = [(mv, r) for mv in terrain.movers for r in mv.riders]
        self.assertEqual(1, len(riders))
        mv, rider = riders[0]
        self.assertEqual((5000, 0), (rider.t_off, rider.rel))
        self.assertEqual(mv.position_at(5000), mv.rider_center(rider, 0))

    def test_every_map_with_movers_parses(self):
        """全库 24 条路径都要能算出位置，不能有除零 / 越界。"""
        seen = 0
        for name in mapdata.available():
            terrain = mapdata.load(name)
            if terrain is None:
                continue
            for mv in terrain.movers:
                seen += 1
                for t in (0, 1, 999, 5000, 123457):
                    px, py = mv.position_at(t)
                    self.assertTrue(math.isfinite(px) and math.isfinite(py),
                                    "%s 的路径 %d 在 t=%d 算出了非有限值"
                                    % (name, mv.handle, t))
        self.assertGreaterEqual(seen, 20, "全库应该有二十几条移动平台")


if __name__ == "__main__":
    unittest.main()
