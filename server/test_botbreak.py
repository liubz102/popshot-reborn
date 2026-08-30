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

    def test_a_blast_only_reaches_what_it_covers(self):
        # 这件东西占 x=2..5 / y=2..4。半径 0 的爆炸砸在 (10, 3) 够不着。
        self.assertEqual([], self.ledger.blast(self.terrain, 10, 3, 0.0, 99))
        self.assertEqual(frozenset([0]), self.ledger.alive(self.terrain))
        # 砸在身上就碎。
        self.assertEqual([0], self.ledger.blast(self.terrain, 3, 3, 0.0, 99))

    def test_a_splash_radius_reaches_further(self):
        self.assertEqual([], self.ledger.blast(self.terrain, 12, 3, 4.0, 99))
        self.assertEqual([0], self.ledger.blast(self.terrain, 9, 3, 4.0, 99))

    def test_a_broken_one_takes_no_more_damage(self):
        self.ledger.blast(self.terrain, 3, 3, 0.0, 99)
        self.assertEqual([], self.ledger.blast(self.terrain, 3, 3, 0.0, 99))

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
