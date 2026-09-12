#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`server/sellprice.py` —— 卖出价格表和材料五小类（用户 2026-09-12）。

★ 这一组里最要紧的是 `MaterialClassTests`：五个小类的判据**不是**新写的
硬编码清单，而是从 `shopdefaults` 那份掉落设计表推出来的。推得对不对
只有「43 件材料零缺零重」这一条能证。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sellprice                                               # noqa: E402
import shopcfg                                                 # noqa: E402
import shopdata                                                # noqa: E402
import shopdefaults                                            # noqa: E402


class MaterialClassTests(unittest.TestCase):
    """材料 → 小类。★ 这一组读的是**真的**物品表（`shop_items.json`），
    不是合成表 —— 要证的正是「现实里那 43 件都落对了格子」。"""

    def test_every_material_lands_in_exactly_one_class(self):
        """★★ 零缺零重：43 件材料，每件恰好一类，五类互不相交。

        漏一件的症状是它突然变成「不可出售」或者掉进兜底价；
        重一件则是「同一样东西按哪个价卖取决于查表顺序」。两种都不报错。
        """
        groups = sellprice.material_groups()
        self.assertEqual(set(sellprice.MATERIAL_CLASSES), set(groups))
        seen = []
        for bucket in groups.values():
            seen.extend(bucket)
        materials = set(shopdata.ids_of_kind("material"))
        self.assertEqual(len(seen), len(set(seen)), "有材料被分进了两类")
        self.assertEqual(materials, set(seen), "有材料一类都没落进去")

    def test_the_five_buckets_are_the_ones_we_think_they_are(self):
        """件数钉死。改了 `shopdefaults` 的掉落设计表这条会红 ——
        那时要回来确认「是有意改的」，而不是顺手把数字改掉。"""
        sizes = {name: len(ids)
                 for name, ids in sellprice.material_groups().items()}
        self.assertEqual({"bead": 4, "generic": 8, "special_low": 7,
                          "special_high": 7, "card": 17}, sizes)

    def test_the_phoenix_pair_is_special_material_not_generic(self):
        """★★ 唯二不能按 id 段切的两件。

        `30016 不死鸟之羽` / `30017 不死鸟之泪` 的 id 在 `3xxxx` 段（看着
        像矿料），语义上却是第 7 关的低 / 高档特殊材料 —— 它们在
        `shopdefaults.SPECIAL[7]` 里。按 id 开头分类正好会把这两件分错，
        而分错的症状只是「卖便宜了」，没人会报。
        """
        self.assertEqual(sellprice.CLASS_SPECIAL_LOW,
                         sellprice.material_class(30016))
        self.assertEqual(sellprice.CLASS_SPECIAL_HIGH,
                         sellprice.material_class(30017))
        self.assertIn(30016, shopdefaults.LOW_TIER)
        self.assertIn(30017, shopdefaults.HIGH_TIER)

    def test_cards_are_their_own_class_even_though_their_kind_is_material(self):
        # 卡片的 `kind` 也是 "material"，在游戏仓库里却单占「收集品 → 卡片」。
        self.assertEqual("material", shopdata.kind(60001))
        self.assertEqual(sellprice.CLASS_CARD, sellprice.material_class(60001))
        self.assertEqual(sellprice.CLASS_CARD, sellprice.material_class(130003))

    def test_a_non_material_has_no_class(self):
        self.assertIsNone(sellprice.material_class(1010001))
        self.assertIsNone(sellprice.material_class(220003))


class _PriceCase(unittest.TestCase):
    """临时 data 目录。★ 价格表是**第六份运营配置**（`shopcfg._SPECS`，D95），
    读不到文件时退回内置默认值（`_USE_DEFAULT`），所以这里不用先铺一份。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self._saved = shopcfg.DATA_DIR
        shopcfg.DATA_DIR = self.dir
        shopcfg.invalidate()

    def tearDown(self):
        shopcfg.DATA_DIR = self._saved
        shopcfg.invalidate()
        self.tmp.cleanup()

    def write_raw(self, text):
        with open(sellprice.path(), "w", encoding="utf-8", newline="\n") as fp:
            fp.write(text)

    def raw_bytes(self):
        with open(sellprice.path(), "rb") as fp:
            return fp.read()


class LoadSaveTests(_PriceCase):

    def test_a_missing_file_means_the_factory_prices(self):
        """★★ **绝不回空表。** 空表 = 玩家卖东西一分钱拿不到，
        而他东西已经没了 —— 和 `rewards` 那份「打完一局一分不给」同一类事故。
        """
        self.assertFalse(os.path.exists(sellprice.path()))
        self.assertEqual(sellprice.DEFAULTS, sellprice.load())

    def test_the_factory_prices_are_the_ones_the_user_picked(self):
        # 用户 2026-09-12 定的**七个**数（`other_price` 当天先定 100、
        # 下午改口 500）。改它之前先确认是用户又说了话，不是谁顺手调的。
        self.assertEqual(
            {"bead": 100, "generic": 200, "special_low": 300,
             "special_high": 600, "card": 200,
             "equip_percent": 95, "other_price": 500},
            sellprice.DEFAULTS)

    def test_the_factory_table_lives_in_the_design_table(self):
        """★ 出厂值住在 `shopdefaults`，和另外五份一个待遇（D95 / D50）。

        `sellprice.DEFAULTS` 只是个别名 —— 两处各写一份数字的话，
        「改了设计表、开出来的服还是老价」这种事一句报错都不会有。
        """
        self.assertEqual(shopdefaults.SELL_PRICE, sellprice.DEFAULTS)
        self.assertEqual({"format": shopcfg.FORMAT,
                          "prices": sellprice.DEFAULTS},
                         shopcfg.default_sell_price())

    def test_the_factory_table_covers_exactly_the_seven_keys(self):
        """★ 设计表里的键名是**字面量**（那边不能 import 本模块，会绕成环）
        ⇒ 拿这一条钉住「两边说的是同一张表」。"""
        self.assertEqual(
            set(sellprice.MATERIAL_CLASSES)
            | {sellprice.KEY_PERCENT, sellprice.KEY_OTHER},
            set(shopdefaults.SELL_PRICE))

    def test_it_is_registered_as_an_operations_config(self):
        """★★ 和那五份**同一组**（用户 2026-09-12）：开服生成、一起回滚。

        判据全部**现取**，不写死文件名 —— 以后谁把它从 `_SPECS` 里摘出去，
        这一条立刻红。
        """
        self.assertIn(shopcfg.SELL_PRICE_FILENAME, shopcfg.config_filenames())
        self.assertEqual("卖出价格",
                         shopcfg.config_title(shopcfg.SELL_PRICE_FILENAME))
        self.assertIsNotNone(
            shopcfg.validator_of(shopcfg.SELL_PRICE_FILENAME))
        # ★ 但它**没有配置标签页** —— 编辑入口是「装备卖出」页上那个弹窗。
        self.assertNotIn("sell_price", shopcfg.SCHEMA)

    def test_first_boot_writes_the_file_like_the_other_five(self):
        self.assertFalse(os.path.exists(sellprice.path()))
        created = shopcfg.ensure_files(self.dir)
        self.assertIn(shopcfg.SELL_PRICE_FILENAME, created)
        self.assertEqual(sellprice.DEFAULTS, sellprice.load())
        # D7 / 铁律 11：已存在的一律不覆盖 —— 升级不该抹掉运营改过的数。
        sellprice.save(dict(sellprice.DEFAULTS, bead=7))
        self.assertEqual([], shopcfg.ensure_files(self.dir))
        self.assertEqual(7, sellprice.load()["bead"])

    def test_saving_then_loading_round_trips(self):
        wanted = dict(sellprice.DEFAULTS, bead=7, equip_percent=50)
        self.assertEqual(wanted, sellprice.save(wanted))
        self.assertEqual(wanted, sellprice.load())

    def test_the_file_is_lf_without_bom(self):
        # 铁律 3：`.json` 一律 LF 无 BOM（服务端包要在 Linux 上跑）。
        sellprice.save(sellprice.DEFAULTS)
        with open(sellprice.path(), "rb") as fp:
            raw = fp.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\n", raw)

    def test_a_missing_key_is_filled_in_from_the_defaults(self):
        # 老文件加了新字段时不用迁移（铁律 11：读盘时按默认值补齐，幂等）。
        self.write_raw(json.dumps({"format": 1, "prices": {"bead": 5}}))
        loaded = sellprice.load()
        self.assertEqual(5, loaded["bead"])
        self.assertEqual(sellprice.DEFAULTS["card"], loaded["card"])

    def test_a_broken_file_falls_back_but_is_never_rewritten(self):
        """★ 和另外五份一个规矩（D95）：**坏文件原样留着，绝不回写**。

        价格表是人手改出来的 —— 解析失败就当它不存在、下一发写盘顺手抹掉，
        等于把他改过的数悄悄销毁。读的人拿到出厂价（不是空表），
        文件本身一个字节不动，等人自己去看日志里那句警告。
        """
        self.write_raw("{这不是 json")
        before = self.raw_bytes()
        said = []
        self.assertEqual(sellprice.DEFAULTS, sellprice.load(log=said.append))
        self.assertEqual(before, self.raw_bytes())
        self.assertTrue(said, "读坏了要往日志里说一句")

    def test_a_file_that_parses_but_is_illegal_also_falls_back(self):
        self.write_raw(json.dumps({"format": 1, "prices": {"bead": -5}}))
        before = self.raw_bytes()
        self.assertEqual(sellprice.DEFAULTS, sellprice.load())
        self.assertEqual(before, self.raw_bytes())

    def test_a_good_read_keeps_working_after_a_bad_one(self):
        """★ 「坏文件保留上一份好的」那条也一样适用（`shopcfg._load` 的缓存）。"""
        sellprice.save(dict(sellprice.DEFAULTS, bead=7))
        self.assertEqual(7, sellprice.load()["bead"])
        self.write_raw("{坏了")
        self.assertEqual(7, sellprice.load()["bead"])

    def test_a_bad_value_is_refused_and_nothing_is_written(self):
        for bad in ({"bead": -1}, {"bead": "很多"}, {"bead": True},
                    {"equip_percent": sellprice.PERCENT_MAX + 1}):
            with self.assertRaises(ValueError):
                sellprice.save(dict(sellprice.DEFAULTS, **bad))
            self.assertFalse(os.path.exists(sellprice.path()), bad)

    def test_the_percentage_cannot_go_over_a_hundred(self):
        """★★ **卖价不许高过买价**（用户 2026-09-12 拍板，上限从 1000 收到 100）。

        超过 100 的话「买了再卖」「合成了再卖」就是一条净赚的循环，
        玩家挂机就能刷爆金币 —— 那不是「运营的自由」，是一个没人会当场
        发现的经济漏洞（唯一的症状是某天有人金币九位数）。
        """
        self.assertEqual(100, sellprice.PERCENT_MAX)
        self.assertEqual(100, sellprice.save(
            dict(sellprice.DEFAULTS, equip_percent=100))["equip_percent"])
        with self.assertRaises(ValueError):
            sellprice.save(dict(sellprice.DEFAULTS, equip_percent=101))


class QuoteTests(_PriceCase):
    """报价四条分支。★ 这一组用**真的**商店 / 配方数据（从模板生成），
    不然「合成产出按 cost 算」这种事验不出来。"""

    def setUp(self):
        super().setUp()
        shopcfg.ensure_files(self.dir)
        shopcfg.invalidate()
        self.prices = sellprice.load()
        self.shop, self.recipes = sellprice.tables()

    def quote(self, item_id):
        return sellprice.quote(item_id, self.prices, self.shop, self.recipes)

    def test_a_material_is_priced_by_its_class(self):
        got = self.quote(10002)                 # 红色小珠 → 珠子
        self.assertTrue(got["sellable"])
        self.assertEqual(sellprice.SOURCE_MATERIAL, got["source"])
        self.assertEqual(self.prices["bead"], got["unit"])
        self.assertEqual({}, got["materials"])

    def test_a_shop_item_is_priced_off_its_buy_price(self):
        item_id = 1010001
        got = self.quote(item_id)
        self.assertEqual(sellprice.SOURCE_SHOP, got["source"])
        self.assertEqual(self.shop[item_id]["price"] * 95 // 100, got["unit"])

    def test_a_crafted_item_returns_the_recipe_materials(self):
        item_id = 220003                        # 火焰蝙蝠
        recipe = self.recipes[item_id]
        got = self.quote(item_id)
        self.assertEqual(sellprice.SOURCE_RECIPE, got["source"])
        self.assertEqual(recipe["cost"] * 95 // 100, got["unit"])
        self.assertEqual({slot["id"]: slot["count"]
                          for slot in recipe["materials"]}, got["materials"])

    def test_anything_else_falls_back_to_the_other_price(self):
        title_id = sorted(shopdata.ids_of_kind("title"))[0]
        got = self.quote(title_id)
        self.assertEqual(sellprice.SOURCE_OTHER, got["source"])
        self.assertEqual(self.prices["other_price"], got["unit"])

    def test_something_the_client_does_not_know_is_not_sellable(self):
        got = self.quote(999999)
        self.assertFalse(got["sellable"])
        self.assertTrue(got["reason"])

    def test_every_ownable_item_has_a_price(self):
        """★ 808 件可拥有物品**一件不落**都卖得掉 —— 玩家手上可能有任何一件
        （管理员发的、掉落的、以前买的），「这件东西没有价」是个死胡同。"""
        quotes = sellprice.quote_all(self.prices)
        self.assertTrue(all(row["sellable"] for row in quotes.values()))
        owned = {item_id for kind in shopdata.kinds()
                 for item_id in shopdata.ids_of_kind(kind)
                 if shopdata.ownable(item_id)}
        self.assertEqual(owned, set(quotes))

    def test_a_delisted_item_still_has_its_price(self):
        """★ 下架只决定「还买不买得到」—— 不该让玩家手里已经有的东西
        突然卖不掉（那等于把他的东西锁死）。"""
        table, _warnings = shopcfg.shop(self.dir)
        entry = dict(table[1010001], listed=False)
        rows = [dict(row) for row in table.values()]
        for row in rows:
            if row["id"] == 1010001:
                row["listed"] = False
        shopcfg.write_json(shopcfg.path_of(shopcfg.SHOP_FILENAME, self.dir),
                           {"format": shopcfg.FORMAT, "items": rows})
        shopcfg.invalidate()
        shop_table, recipes = sellprice.tables()
        got = sellprice.quote(1010001, self.prices, shop_table, recipes)
        self.assertTrue(got["sellable"])
        self.assertEqual(entry["price"] * 95 // 100, got["unit"])


class BundleTests(_PriceCase):
    """把一份清单算成一笔交易。"""

    def setUp(self):
        super().setUp()
        shopcfg.ensure_files(self.dir)
        shopcfg.invalidate()

    def test_totals_add_up_and_materials_merge(self):
        got = sellprice.bundle([{"id": 220003, "count": 1},
                                {"id": 10002, "count": 5}])
        recipe = sellprice.recipe_index()[220003]
        want = recipe["cost"] * 95 // 100 + 5 * sellprice.DEFAULTS["bead"]
        self.assertEqual(want, got["money"])
        # 配方里的红珠和「直接卖掉的红珠」是两回事，返还那一份照样是红珠。
        self.assertEqual({slot["id"]: slot["count"]
                          for slot in recipe["materials"]}, got["returned"])

    def test_the_same_item_twice_is_merged(self):
        got = sellprice.bundle([{"id": 10002, "count": 2},
                                {"id": 10002, "count": 3}])
        self.assertEqual(1, len(got["plan"]))
        self.assertEqual(5, got["plan"][0]["count"])

    def test_equipment_is_always_one_no_matter_what_was_asked(self):
        """★ 装备的数量那一格客户端根本不读（§28），存档里也只有
        「有 / 没有」—— 前台传什么都不算数。"""
        got = sellprice.bundle([{"id": 1010001, "count": 99}])
        self.assertEqual(1, got["plan"][0]["count"])

    def test_returned_materials_scale_with_the_count(self):
        # 合成装备不可堆叠，所以这一条拿一件可堆叠的假配方来验不了；
        # 改为验「两条不同的合成装备各自的材料会累加」。
        recipes = sellprice.recipe_index()
        first, second = sorted(recipes)[:2]
        got = sellprice.bundle([{"id": first}, {"id": second}])
        want = {}
        for item_id in (first, second):
            for slot in recipes[item_id]["materials"]:
                want[slot["id"]] = want.get(slot["id"], 0) + slot["count"]
        self.assertEqual(want, got["returned"])

    def test_an_empty_order_is_refused(self):
        for bad in ([], None, [{"id": 10002, "count": 0}]):
            with self.assertRaises(ValueError):
                sellprice.bundle(bad)

    def test_a_negative_count_is_refused(self):
        with self.assertRaises(ValueError):
            sellprice.bundle([{"id": 10002, "count": -1}])

    def test_an_unknown_item_is_refused(self):
        with self.assertRaises(ValueError):
            sellprice.bundle([{"id": 999999, "count": 1}])


if __name__ == "__main__":
    unittest.main()
