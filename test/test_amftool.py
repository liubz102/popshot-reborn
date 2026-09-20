#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/amftool.py` —— `default.amf`（2D 精灵表）读写器的守卫（X_Mod · X3）。

判据只有一条真的硬：**整份原文件 parse → write 逐字节一致**。格式是本轮逆的
（§37），一个字段的宽度记错，写出去的表客户端就整张读歪 —— 所有 2D 弹体一起消失。
"""
import importlib.util
import os
import struct
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _amftool():
    spec = importlib.util.spec_from_file_location("test_amftool_mod", os.path.join(ROOT, "tools", "amftool.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AmfRoundTripTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.amftool = _amftool()
        cls.path = cls.amftool.default_path()
        if not os.path.isfile(cls.path):
            raise unittest.SkipTest("没有明文资源树，跳过")
        with open(cls.path, "rb") as fp:
            cls.blob = fp.read()

    def test_the_real_file_round_trips_byte_for_byte(self):
        version, groups = self.amftool.parse(self.blob)
        self.assertEqual(self.blob, self.amftool.write(version, groups))

    def test_the_bullet_group_has_the_custom_entries(self):
        """`build.py` 追加的 `CHnn_WPk_C` 条目 —— 每条都指向一张存在的精灵。"""
        _version, groups = self.amftool.parse(self.blob)
        root = os.path.dirname(os.path.dirname(self.path))
        names = ("CH00_WP1_C", "CH00_WP2_C", "CH00_WP3_C", "CH01_WP1_C", "CH01_WP3_C",
                 "CH02_WP1_C", "CH02_WP2_C", "CH02_WP3_C")
        for name in names:
            entry = self.amftool.find(groups, "Game/Bullet", name)
            self.assertIsNotNone(entry, name)
            png = os.path.join(root, entry.path.lstrip("/"))
            self.assertTrue(os.path.isfile(png), png)
            self.assertTrue(os.path.isfile(os.path.splitext(png)[0] + ".smf"), png)
            self.assertGreaterEqual(len(entry.frames), 1)

    def test_upsert_replaces_by_name_and_appends_new(self):
        amftool = self.amftool
        _version, groups = amftool.parse(self.blob)
        before = sum(len(g.entries) for g in groups)
        entry = amftool.Entry("CH00_WP1_C", "/Images/Game/x.png", 0, 0, [(0, 100)])
        groups, added = amftool.upsert(groups, "Game/Bullet", entry)
        self.assertFalse(added)
        self.assertEqual(before, sum(len(g.entries) for g in groups))
        self.assertEqual("/Images/Game/x.png", amftool.find(groups, "Game/Bullet", "ch00_wp1_c").path)
        groups, added = amftool.upsert(groups, "Game/Bullet",
                                       amftool.Entry("ZZ_TEST", "/Images/Game/y.png", 0, 0, [(0, 50), (1, 50)]))
        self.assertTrue(added)
        self.assertEqual(before + 1, sum(len(g.entries) for g in groups))
        # 写出去再读回来，追加的那条原样在
        again = amftool.parse(amftool.write(1, groups))[1]
        self.assertEqual([(0, 50), (1, 50)], amftool.find(again, "Game/Bullet", "ZZ_TEST").frames)
        with self.assertRaises(amftool.AmfError):
            amftool.upsert(groups, "No/Such/Group", entry)

    def test_the_header_is_what_we_think(self):
        self.assertEqual(b"ANIM", self.blob[:4])
        version, ngroups = struct.unpack_from("<II", self.blob, 4)
        self.assertEqual((1, 6), (version, ngroups))


if __name__ == "__main__":
    unittest.main()
