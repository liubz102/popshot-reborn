#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/openweapons.py` 的守卫（X_Mod · X5）：**韩版有的原版武器，中文版一件都不许再缺**。

中文版 `ShopItem-Chn.ini` 当年漏抄了 18 把 3 级武器（1 号槽和 3 号槽的 `D3/R3/F3`），
而它们的数据和美术在客户端里一件不缺 —— `weapon.ini` 不在 `Chinese.ini` 的重定向表里，
网格 / 弹体 / 特效 / 音效 / 图标全都在。X5 用 `tools/openweapons.py` 把韩版那 18 组
`[Item-]` / `[Stock-]` **原样**抄了回来（§45 / D35）。

这里钉三件事：

1. 那 18 条在中文版表里，且**逐字节等于韩版原文**（不是我们自己编的数值）；
2. `[Item-]` 排在 `[Stock-]` **前面** —— `tools/shopdata.py` 的 `Tag` / `PartFlag`
   就靠这个顺序取，反过来写会静默丢掉弹药 id 和装备槽（会话里真踩过）；
3. 工具**幂等**：拿现在这份文件再跑一遍，产出逐字节相同，而且 goldwp 的
   自定义武器块仍然排在我们这一块**后面**（两个工具的重跑顺序才无所谓）。

⚠ 依赖明文资源树 `game_patched/Pack_develop`，发布包里没有，自动跳过。
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "tools")

#: 这 18 件就是「韩版有、中文版没抄」的全部武器。数量变了要么是素材换了版本，
#: 要么是工具的判据变了 —— 两种都该有人看一眼。
EXPECTED_IDS = (
    1120013, 1120033, 1120043, 1120063, 1120073, 1120093,   # 泰尔
    2120013, 2120033, 2120043, 2120063, 2120073, 2120093,   # 卡希尔
    3120013, 3120033, 3120043, 3120063, 3120073, 3120093,   # 布洛克
)


def _load(name, folder=TOOLS):
    """按**路径**加载 `tools/` 下的模块 —— `server/` 下有同名的另一份。"""
    spec = importlib.util.spec_from_file_location(
        "test_openweapons_" + name, os.path.join(folder, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpenWeaponsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.ow = _load("openweapons")
        cls.root = cls.ow.develop_root()
        cls.cn_path = os.path.join(cls.root, "Data", "ShopItem-Chn.ini")
        cls.kr_path = os.path.join(cls.root, "Data", "ShopItem.ini")
        if not os.path.isfile(cls.cn_path) or not os.path.isfile(cls.kr_path):
            raise unittest.SkipTest("没有明文资源树，跳过")
        cls.cn = cls.ow.split_sections(cls.ow.read_text(cls.cn_path))
        cls.kr = cls.ow.split_sections(cls.ow.read_text(cls.kr_path))

    def test_nothing_is_missing_any_more(self):
        """★ 现在中文版物品表里**一件原版武器都不缺**。"""
        left = self.ow.find_candidates(self.root)
        self.assertEqual([], [c.item_id for c in left],
                         "还有原版武器没补进中文版物品表：%s"
                         % [(c.item_id, c.name_kr) for c in left])

    def test_the_eighteen_are_verbatim_from_the_korean_table(self):
        """★★ 抄的是韩版原文，不是我们自己编的 Tag / PartFlag。"""
        for item_id in EXPECTED_IDS:
            for kind in ("item", "stock"):
                mine = self.ow.section_text(self.cn, kind, item_id)
                theirs = self.ow.section_text(self.kr, kind, item_id)
                self.assertIsNotNone(theirs, "韩版没有 [%s-%d]？" % (kind, item_id))
                self.assertIsNotNone(mine, "中文版缺 [%s-%d]" % (kind, item_id))
                self.assertEqual(theirs.rstrip("\n"), mine.rstrip("\n"),
                                 "[%s-%d] 和韩版原文对不上" % (kind, item_id))

    def test_item_section_comes_before_stock(self):
        """★★ `Tag` / `PartFlag` 只写在 `[Item-]` 里，`tools/shopdata.py` 只认先遇到的那一节。

        反过来写的症状是**静默**的：弹药 id 没了、`part_flag=0`（穿不上身），
        管理页照样列得出来，进游戏才发现装不上。
        """
        order = {}
        for index, section in enumerate(self.cn):
            if section["id"] in EXPECTED_IDS and section["kind"] in ("item", "stock"):
                order.setdefault((section["id"], section["kind"]), index)
        for item_id in EXPECTED_IDS:
            self.assertLess(order[(item_id, "item")], order[(item_id, "stock")],
                            "#%d 的 [Stock-] 排到 [Item-] 前面去了" % item_id)

    def test_our_block_sits_before_the_goldwp_block(self):
        """★ goldwp 的 `build_shop_ini()` 会砍掉 `[Item-1920001]` 之后的一切再追加
        —— 我们的块必须在那一刀**上游**，否则重跑 goldwp 就把 18 件带走了。"""
        text = self.ow.read_text(self.cn_path)
        anchor = text.find(self.ow.GOLDWP_ANCHOR)
        if anchor < 0:
            self.skipTest("这份表里没有 goldwp 的自定义武器块")
        for item_id in EXPECTED_IDS:
            where = text.find("[Item-%d]" % item_id)
            self.assertGreater(where, 0, "#%d 不在表里" % item_id)
            self.assertLess(where, anchor, "#%d 跑到 goldwp 的块后面去了" % item_id)

    def test_rerun_changes_nothing(self):
        """★ 幂等：拿现在这份再跑一遍，产出逐字节相同。"""
        _path, text, added = self.ow.rebuild(self.root)
        self.assertEqual([], [c.item_id for c in added])
        self.assertEqual(open(self.cn_path, "rb").read(), self.ow.encode_text(text))


class ExtractedTableTests(unittest.TestCase):
    """补完之后，离线产物 `server/shop_items.json` 里这 18 件要是**完整**的武器。"""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(ROOT, "server", "shop_items.json")
        if not os.path.isfile(path):
            raise unittest.SkipTest("没有 shop_items.json，跳过")
        import json
        with open(path, "r", encoding="utf-8") as fp:
            cls.table = json.load(fp)

    def test_all_eighteen_are_ownable_weapons_with_ammo_and_slot(self):
        for item_id in EXPECTED_IDS:
            entry = self.table["items"].get(str(item_id))
            self.assertIsNotNone(entry, "物品表里没有 #%d" % item_id)
            self.assertEqual("weapon", entry["kind"])
            self.assertTrue(entry["ownable"], "#%d 进不了背包" % item_id)
            self.assertIsNotNone(entry.get("ammo_id"), "#%d 没有弹药 id" % item_id)
            self.assertEqual(3, entry.get("tier"), "#%d 不是 3 级" % item_id)
            self.assertIn(entry.get("part_flag"), (1024, 2048, 4096),
                          "#%d 的装备槽丢了" % item_id)
            self.assertIn("damage", entry.get("weapon") or {}, "#%d 没有伤害" % item_id)


if __name__ == "__main__":
    unittest.main()
