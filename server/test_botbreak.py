#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`botbreak.py` —— 可破坏物的血量 / 恢复账（V0.3 §138）。

原版事实（全在 `botbreak` 的文件头里注明了出处）：血量和恢复延迟都写在
`.map` 里，碎了按**本地定时器**长回来，**一发同步包都没有**。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import botbreak                                                # noqa: E402
import mapdata                                                 # noqa: E402
from test_mapdata import BreakableTerrainTests                 # noqa: E402


class LedgerTests(unittest.TestCase):

    def setUp(self):
        self.terrain = mapdata.MapTerrain(
            BreakableTerrainTests.record(BreakableTerrainTests))
        self.ledger = botbreak.Ledger()

    def test_everything_starts_intact(self):
        self.assertEqual(frozenset([0]), self.ledger.alive(self.terrain))

    def test_it_takes_more_than_one_hit(self):
        """★ 血量是地图给的（这件 40 点）—— 一发 22 的手雷打不碎。"""
        self.assertFalse(self.ledger.damage(self.terrain, 0, 22))
        self.assertEqual(frozenset([0]), self.ledger.alive(self.terrain))
        self.assertTrue(self.ledger.damage(self.terrain, 0, 22))
        self.assertEqual(frozenset(), self.ledger.alive(self.terrain))

    def test_it_grows_back_after_the_map_s_own_delay(self):
        """★★★ 「过一段时间后恢复原状」（用户 2026-08-30）。"""
        self.ledger.damage(self.terrain, 0, 100, now=1000.0)
        self.assertEqual(frozenset(), self.ledger.alive(self.terrain,
                                                       now=1000.0))
        # 15 秒差一点还没长回来。
        self.assertEqual(frozenset(), self.ledger.alive(self.terrain,
                                                       now=1014.9))
        # 到点了：满血、原样立回去。
        self.assertEqual(frozenset([0]), self.ledger.alive(self.terrain,
                                                          now=1015.0))
        # 长回来之后要**重新**打两下才碎（血是满的）。
        self.assertFalse(self.ledger.damage(self.terrain, 0, 22))

    def test_the_blast_has_to_actually_touch_it(self):
        """★★★ 判据是「那 11 个采样点碰没碰到它」，不是「离得近」（§139）。

        这件东西占 x=2..5 / y=2..4。溅射半径 0 ⇒ 只有爆点本身一个采样点。
        """
        self.assertEqual([], self.ledger.blast(self.terrain, 10, 3, 0.0, 99))
        self.assertEqual(frozenset([0]), self.ledger.alive(self.terrain))
        hits = self.ledger.blast(self.terrain, 3, 3, 0.0, 99)
        self.assertEqual(1, len(hits))
        item, hurt, where, broke = hits[0]
        self.assertEqual(0, item.index)
        self.assertTrue(broke)

    def test_the_ring_of_samples_reaches_further_than_the_centre(self):
        """半径 4 的那一圈上有采样点落在它身上 —— 爆点自己在外面也算。"""
        # 太远：圆上 10 个点一个都够不着。
        self.assertEqual([], self.ledger.blast(self.terrain, 30, 3, 4.0, 99))
        # 贴边：圆上有点落进 x=2..5 那一块。
        self.assertEqual(1, len(self.ledger.blast(self.terrain, 9, 3, 4.0, 99)))

    def test_the_damage_falls_off_with_distance(self):
        """★ 和打人**同一条**衰减（§90），只是半径换成 (宽+高)/2。"""
        # 半径 = (4+3)/2 = 3.5；爆点就在中心 ⇒ r=0 ⇒ 满额。
        near = self.ledger.blast(self.terrain, 4, 3, 20.0, 41)[0][1]
        self.assertEqual(41, near, "正中心该是满额 int((1-0)*(41-1)+1)")
        self.ledger.clear()
        # 挪开一点：r = 2/(20+3.5) ⇒ int((1-0.0851)*40+1) = 37
        far = self.ledger.blast(self.terrain, 6, 3, 20.0, 41)[0][1]
        self.assertEqual(37, far)

    def test_the_game_mode_multiplier_is_applied_last(self):
        doubled = self.ledger.blast(self.terrain, 4, 3, 20.0, 41, mult=2)
        self.assertEqual(82, doubled[0][1])

    def test_a_broken_one_takes_no_more_damage(self):
        self.ledger.blast(self.terrain, 3, 3, 0.0, 99)
        self.assertEqual([], self.ledger.blast(self.terrain, 3, 3, 0.0, 99))

    def test_a_weapon_without_splash_damage_does_nothing(self):
        """普通子弹（`SplashDamage` = 0）打不碎它 —— 伤害算出来是 0。"""
        self.assertEqual([], self.ledger.blast(self.terrain, 3, 3, 0.0, 0))

    def test_a_broadcast_from_a_human_is_taken_at_face_value(self):
        """★★★ 真人那一发照包里的数扣，一个字都不重算（§139）。"""
        item = self.terrain.breakables[0]
        self.assertEqual((item, False),
                         self.ledger.apply_broadcast(self.terrain,
                                                     item.handle, 20))
        self.assertEqual(20, item.hp - self.ledger.hp[0])
        self.assertEqual((item, True),
                         self.ledger.apply_broadcast(self.terrain,
                                                     item.handle, 20))
        self.assertEqual(frozenset(), self.ledger.alive(self.terrain))

    def test_a_broadcast_for_something_else_is_not_ours(self):
        """不是破坏物的句柄（怪 / 角色）要原样放行给别的分支。"""
        self.assertIsNone(self.ledger.apply_broadcast(self.terrain, 1100419,
                                                      20))

    def test_clear_resets_the_whole_ledger(self):
        self.ledger.blast(self.terrain, 3, 3, 0.0, 99)
        self.ledger.clear()
        self.assertEqual(frozenset([0]), self.ledger.alive(self.terrain))

    def test_a_map_without_breakables_is_free(self):
        plain = mapdata.MapTerrain(
            {"format": mapdata.FORMAT, "name": "T", "version": 18,
             "width": 4, "height": 2,
             "cells": _empty(4, 2), "ground_counts": _counts(4),
             "ground_ys": _blob(b""), "points": {}, "jump": []})
        self.assertEqual(frozenset(), self.ledger.alive(plain))
        self.assertEqual([], self.ledger.blast(plain, 0, 0, 100.0, 99))


def _blob(raw):
    import base64
    import zlib
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def _empty(w, h):
    return _blob(bytes(((w * h + 15) // 16) * 4))


def _counts(w):
    import struct
    return _blob(struct.pack("<%dH" % w, *([0] * w)))


if __name__ == "__main__":
    unittest.main()
