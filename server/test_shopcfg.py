#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""商店 / 合成 / 掉落配置的测试（`server/shopcfg.py`）。

这里守的是三件事，坏一件都很难查：

1. **热重载**（用户明确要求「改完不重启即刻生效」）—— 改了文件下一次读要拿到新值。
2. **坏文件不能把商店冲掉**：用户可能正编辑到一半。解析不了就保留上一份好的，
   而且**任何情况下都不回写**（回写 = 把用户编到一半的内容盖掉）。
3. **`ensure_files` 不覆盖已存在的**：云上升级时用户手改过的价格 / 配方
   必须原样留着（铁律 11 / D7）。

外加一批校验用例。校验的意义是「**别把客户端认不出来的 id 发下去**」
—— 那种 id 在界面上就是个空格子，比报错难查得多。
"""
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import shopcfg                                                 # noqa: E402
import shopdata                                                # noqa: E402
from test_shopdata import SYNTHETIC, make_table                # noqa: E402

import tempfile                                                # noqa: E402


class _CfgCase(unittest.TestCase):
    """临时 data 目录 + 一张合成物品表。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.items_path = os.path.join(self.dir, "shop_items.json")
        with open(self.items_path, "w", encoding="utf-8", newline="\n") as fp:
            json.dump(make_table(SYNTHETIC), fp, ensure_ascii=False)
        self._saved = shopdata.STORE
        shopdata.STORE = shopdata._Store(self.items_path)
        shopcfg.invalidate()

    def tearDown(self):
        shopdata.STORE = self._saved
        shopcfg.invalidate()
        self.tmp.cleanup()

    def write(self, filename, data):
        path = shopcfg.path_of(filename, self.dir)
        shopcfg.write_json(path, data)
        return path


class EnsureFilesTests(_CfgCase):

    def test_creates_three_files(self):
        created = shopcfg.ensure_files(self.dir)
        self.assertEqual(sorted(created),
                         sorted([shopcfg.SHOP_FILENAME, shopcfg.RECIPE_FILENAME,
                                 shopcfg.DROPS_FILENAME]))
        for name in created:
            self.assertTrue(os.path.isfile(os.path.join(self.dir, name)))

    def test_second_run_creates_nothing(self):
        # ★ 幂等：反复启动服务端不会重新生成。
        shopcfg.ensure_files(self.dir)
        self.assertEqual([], shopcfg.ensure_files(self.dir))

    def test_never_overwrites_user_edits(self):
        """★★ 云上升级的命根子：用户改过的价格不能被覆盖（D7 / 铁律 11）。"""
        shopcfg.ensure_files(self.dir)
        path = shopcfg.path_of(shopcfg.SHOP_FILENAME, self.dir)
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        data["items"] = [{"id": 1120041, "name": "我改过的名字",
                          "listed": True, "price": 12345, "level": 7}]
        shopcfg.write_json(path, data)

        self.assertEqual([], shopcfg.ensure_files(self.dir))
        shopcfg.invalidate(self.dir)
        parsed, warnings = shopcfg.shop(self.dir)
        self.assertEqual([], warnings)
        self.assertEqual(12345, parsed[1120041]["price"])
        self.assertEqual("我改过的名字", parsed[1120041]["name"])

    def test_generated_files_are_lf_without_bom(self):
        # 铁律 3：.json 一律 LF 无 BOM（服务端包要在 Linux 上跑）。
        shopcfg.ensure_files(self.dir)
        for name in (shopcfg.SHOP_FILENAME, shopcfg.RECIPE_FILENAME,
                     shopcfg.DROPS_FILENAME):
            with open(os.path.join(self.dir, name), "rb") as fp:
                raw = fp.read()
            self.assertNotIn(b"\r", raw, name + " 里有 CR")
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), name + " 有 BOM")

    def test_generated_files_pass_their_own_validators(self):
        # 生成出来的东西必须自己能读回去 —— 否则第一次启动就是坏的。
        shopcfg.ensure_files(self.dir)
        for loader in (shopcfg.shop, shopcfg.recipes, shopcfg.drops):
            _parsed, warnings = loader(self.dir)
            self.assertEqual([], warnings)


class HotReloadTests(_CfgCase):
    """★ 用户明确要求：改完保存不用重启，即刻生效。"""

    def setUp(self):
        super().setUp()
        self.write(shopcfg.SHOP_FILENAME, {
            "format": shopcfg.FORMAT,
            "items": [{"id": 1120041, "name": "左轮 极速1", "listed": True,
                       "price": 100, "level": 1}]})

    def test_reads_back(self):
        parsed, warnings = shopcfg.shop(self.dir)
        self.assertEqual([], warnings)
        self.assertEqual(100, parsed[1120041]["price"])

    def test_edit_takes_effect_without_restart(self):
        shopcfg.shop(self.dir)
        self.write(shopcfg.SHOP_FILENAME, {
            "format": shopcfg.FORMAT,
            "items": [{"id": 1120041, "name": "左轮 极速1", "listed": True,
                       "price": 999, "level": 1}]})
        # ★ `_reload=True` 只是为了绕开 mtime 的粒度（同一毫秒内写两次），
        #   真实场景里两次编辑之间隔着人的操作，mtime 一定不同。
        parsed, _ = shopcfg.shop(self.dir, _reload=True)
        self.assertEqual(999, parsed[1120041]["price"])

    def test_mtime_change_alone_is_enough(self):
        """不传 `_reload` 也要能发现变化 —— 这才是线上的路径。"""
        shopcfg.shop(self.dir)
        time.sleep(0.01)
        self.write(shopcfg.SHOP_FILENAME, {
            "format": shopcfg.FORMAT,
            "items": [{"id": 1120041, "name": "左轮 极速1", "listed": True,
                       "price": 777, "level": 1},
                      {"id": 1120051, "name": "另一把", "listed": True,
                       "price": 1, "level": 1}]})
        parsed, _ = shopcfg.shop(self.dir)
        self.assertEqual(777, parsed[1120041]["price"])
        self.assertIn(1120051, parsed)

    def test_unchanged_file_is_not_reparsed(self):
        first, _ = shopcfg.shop(self.dir)
        second, _ = shopcfg.shop(self.dir)
        self.assertIs(first, second)      # 命中缓存，同一个对象


class BrokenFileTests(_CfgCase):
    """★★ 用户可能正编辑到一半。坏文件绝不能把商店冲掉，也绝不能被回写。"""

    def setUp(self):
        super().setUp()
        self.path = self.write(shopcfg.SHOP_FILENAME, {
            "format": shopcfg.FORMAT,
            "items": [{"id": 1120041, "name": "好的", "listed": True,
                       "price": 500, "level": 1}]})

    def _break_it(self, text="{ 这不是 JSON"):
        time.sleep(0.01)
        with open(self.path, "w", encoding="utf-8", newline="\n") as fp:
            fp.write(text)

    def test_keeps_last_good_value(self):
        shopcfg.shop(self.dir)
        self._break_it()
        parsed, warnings = shopcfg.shop(self.dir)
        self.assertEqual(500, parsed[1120041]["price"], "坏文件把商店冲掉了")
        self.assertTrue(warnings)

    def test_does_not_rewrite_the_broken_file(self):
        # 回写 = 把用户编到一半的内容盖掉，比商店空着严重得多。
        shopcfg.shop(self.dir)
        self._break_it("{ 我正在编辑")
        shopcfg.shop(self.dir)
        with open(self.path, "r", encoding="utf-8") as fp:
            self.assertEqual("{ 我正在编辑", fp.read())

    def test_broken_from_the_start_is_empty_not_default(self):
        # 一次都没读成功过 -> 空目录。
        # ★ 不是内置默认值：「商店突然多出一堆没上架的东西」比「商店空着」更难查。
        shopcfg.invalidate()
        self._break_it()
        parsed, warnings = shopcfg.shop(self.dir)
        self.assertEqual({}, parsed)
        self.assertTrue(warnings)

    def test_missing_file_keeps_last_good(self):
        shopcfg.shop(self.dir)
        os.remove(self.path)
        parsed, warnings = shopcfg.shop(self.dir)
        self.assertEqual(500, parsed[1120041]["price"])
        self.assertTrue(warnings)

    def test_missing_file_from_the_start_is_empty(self):
        shopcfg.invalidate()
        parsed, warnings = shopcfg.recipes(self.dir)
        self.assertEqual([], parsed)
        self.assertTrue(warnings)

    def test_bad_entry_rejects_the_whole_file(self):
        """一条不对就整份不要 —— 半份配置比没有配置更难查。"""
        shopcfg.shop(self.dir)
        time.sleep(0.01)
        self.write(shopcfg.SHOP_FILENAME, {
            "format": shopcfg.FORMAT,
            "items": [{"id": 1120041, "price": 1, "level": 1},
                      {"id": 999999, "price": 1, "level": 1}]})
        parsed, warnings = shopcfg.shop(self.dir)
        self.assertEqual(500, parsed[1120041]["price"])
        self.assertTrue(warnings)


class ValidateShopTests(_CfgCase):

    def ok(self, entry):
        return shopcfg.validate_shop({"items": [entry]})

    def bad(self, entry, fragment=None):
        with self.assertRaises(shopcfg.ConfigError) as ctx:
            self.ok(entry)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))

    def test_minimal_entry(self):
        got = self.ok({"id": 1120041})
        self.assertEqual(1, len(got))
        self.assertFalse(got[1120041]["listed"])       # 默认不上架
        self.assertEqual(0, got[1120041]["price"])

    def test_name_defaults_to_translation(self):
        got = self.ok({"id": 1120041})
        self.assertEqual("左轮 极速1", got[1120041]["name"])

    def test_kind_comes_from_shop_items_not_the_file(self):
        # 用户写错 kind 不该影响服务端判断 —— 以原版镜像为准。
        got = shopcfg.validate_shop({"items": [{"id": 1120041, "kind": "乱写"}]})
        self.assertEqual("weapon", got[1120041]["kind"])

    def test_rejects_unknown_id(self):
        self.bad({"id": 999999}, "不在 shop_items.json")

    def test_rejects_stock_only_id(self):
        # ★ 只有货架条目的东西塞进背包，客户端认不出来（§11）。
        self.bad({"id": 1510001}, "进不了背包")

    def test_rejects_duplicate(self):
        with self.assertRaises(shopcfg.ConfigError):
            shopcfg.validate_shop({"items": [{"id": 1120041}, {"id": 1120041}]})

    def test_rejects_negative_price(self):
        self.bad({"id": 1120041, "price": -1}, "price")

    def test_rejects_garbage_price(self):
        self.bad({"id": 1120041, "price": "免费"}, "price")

    def test_rejects_missing_items_list(self):
        with self.assertRaises(shopcfg.ConfigError):
            shopcfg.validate_shop({"format": 1})


class ValidateRecipeTests(_CfgCase):

    BASE = {"id": 1, "result": 1010001,
            "materials": [{"id": 30018, "count": 2}]}

    def ok(self, **over):
        entry = dict(self.BASE)
        entry.update(over)
        return shopcfg.validate_recipes({"recipes": [entry]})

    def bad(self, fragment=None, **over):
        with self.assertRaises(shopcfg.ConfigError) as ctx:
            self.ok(**over)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))

    def test_minimal(self):
        got = self.ok()
        self.assertEqual(1, len(got))
        self.assertEqual(1010001, got[0]["result"])
        self.assertEqual([{"id": 30018, "count": 2}], got[0]["materials"])
        self.assertTrue(got[0]["listed"])              # 配方默认上架

    def test_rejects_more_than_four_materials(self):
        """★ 原版合成界面只有 4 个材料槽，第 5 种玩家根本看不见（§7）。"""
        self.bad("最多 4 种", materials=[{"id": 30018, "count": 1}] * 5)

    def test_four_materials_is_fine(self):
        got = self.ok(materials=[{"id": 30018, "count": 1},
                                 {"id": 1010001, "count": 1},
                                 {"id": 1020001, "count": 1},
                                 {"id": 1120041, "count": 1}])
        self.assertEqual(4, len(got[0]["materials"]))

    def test_rejects_empty_materials(self):
        self.bad("没有材料", materials=[])

    def test_rejects_duplicate_material(self):
        self.bad("出现了两次",
                 materials=[{"id": 30018, "count": 1}, {"id": 30018, "count": 2}])

    def test_rejects_unknown_result(self):
        self.bad("不在 shop_items.json", result=999999)

    def test_rejects_bad_character(self):
        self.bad("character", character=9)

    def test_rejects_zero_count(self):
        self.bad("count", materials=[{"id": 30018, "count": 0}])

    def test_rejects_duplicate_recipe_id(self):
        entry = dict(self.BASE)
        with self.assertRaises(shopcfg.ConfigError):
            shopcfg.validate_recipes({"recipes": [entry, dict(entry)]})


class ValidateDropsTests(_CfgCase):

    BASE = {"mode": "quest", "material": 30018}

    def ok(self, **over):
        entry = dict(self.BASE)
        entry.update(over)
        return shopcfg.validate_drops({"rules": [entry]})

    def bad(self, fragment=None, **over):
        with self.assertRaises(shopcfg.ConfigError) as ctx:
            self.ok(**over)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))

    def test_minimal(self):
        got = self.ok()
        self.assertEqual(1, got[0]["count"])
        self.assertEqual(100, got[0]["prob"])
        self.assertTrue(got[0]["cleared_only"])
        self.assertNotIn("stage", got[0])          # 省略 = 不限

    def test_keeps_stage_and_difficulty(self):
        got = self.ok(stage=7, difficulty=1)
        self.assertEqual(7, got[0]["stage"])
        self.assertEqual(1, got[0]["difficulty"])

    def test_rejects_non_material(self):
        # 掉落只能给材料 —— 掉一件装备出来客户端那栏画不出图标也说不清数量。
        self.bad("不是合成材料", material=1120041)

    def test_rejects_bad_mode(self):
        self.bad("mode", mode="乱来")

    def test_rejects_bad_prob(self):
        self.bad("prob", prob=101)
        self.bad("prob", prob=-1)

    def test_rejects_bad_difficulty(self):
        self.bad("difficulty", difficulty=5)


class NameTests(_CfgCase):

    def test_weapon_name(self):
        self.assertEqual("左轮 极速1",
                         shopcfg.item_name_zh(shopdata.get(1120041)))

    def test_material_name(self):
        self.assertEqual("青铜管", shopcfg.item_name_zh(shopdata.get(30018)))

    def test_unknown_name_falls_back_to_korean(self):
        # 翻不出来退回韩文名，别退成空串 —— 空名字在界面上什么都看不见。
        item = shopdata.get(1990001)
        self.assertEqual("셋트", shopcfg.item_name_zh(item))

    def test_no_name_at_all_falls_back_to_id(self):
        item = shopdata.get(1010001)        # 图标是模型编号，没有韩文名
        self.assertEqual("#1010001", shopcfg.item_name_zh(item))

    def test_none_is_empty(self):
        self.assertEqual("", shopcfg.item_name_zh(None))


@unittest.skipUnless(os.path.isfile(shopdata.DATA_PATH), "shop_items.json 不在")
class RealDefaultsTests(unittest.TestCase):
    """拿**真产物**生成一遍默认配置，看内容站不站得住。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        shopcfg.invalidate()

    def tearDown(self):
        shopcfg.invalidate()
        self.tmp.cleanup()

    def test_shop_lists_the_63_weapons(self):
        shop = shopcfg.validate_shop(shopcfg.default_shop())
        listed = [e for e in shop.values() if e["listed"]]
        self.assertEqual(63, len(listed), "上架的应该正好是那 63 件 D/R/F")
        for entry in listed:
            self.assertEqual("weapon", entry["kind"])
            self.assertGreater(entry["price"], 0, "上架的东西不能白送")

    def test_every_shop_name_is_chinese(self):
        shop = shopcfg.validate_shop(shopcfg.default_shop())
        for entry in shop.values():
            self.assertTrue(any("一" <= ch <= "鿿" for ch in entry["name"]),
                            "%d 的名字里没有中文：%s" % (entry["id"], entry["name"]))

    def test_recipes_are_valid_and_bounded(self):
        recipes = shopcfg.validate_recipes(shopcfg.default_recipes())
        self.assertGreaterEqual(len(recipes), 20)
        for recipe in recipes:
            self.assertLessEqual(len(recipe["materials"]), shopcfg.MAX_MATERIALS)
            self.assertGreater(recipe["cost"], 0)
            # 一件装备的材料总量得在「刷十几局能凑齐」的量级，别劝退。
            total = sum(m["count"] for m in recipe["materials"])
            self.assertLessEqual(total, 30, "%s 要 %d 个材料，太肝了"
                                 % (recipe["name"], total))

    def test_recipe_results_are_equippable_armor(self):
        for recipe in shopcfg.validate_recipes(shopcfg.default_recipes()):
            item = shopdata.get(recipe["result"])
            self.assertTrue(item.equippable, "%d 不占装备槽" % item.id)
            self.assertTrue(item.bonus, "%d 一点加成都没有，合它干嘛" % item.id)

    def test_drops_include_the_original_baseline(self):
        rules = shopcfg.validate_drops(shopcfg.default_drops())
        baseline = {(r.get("stage"), r.get("difficulty"), r["material"])
                    for r in rules if r.get("stage") is not None}
        # FINDINGS §12：原版给材料的 4 关（三个角色线去重后）
        self.assertEqual({(7, 1, 30018), (1, 2, 30018),
                          (4, 3, 30019), (1, 3, 30018)}, baseline)

    def test_every_recipe_material_can_actually_drop(self):
        """★ 配方要的材料必须有地方掉，否则那条配方永远合不出来。"""
        droppable = {r["material"] for r in
                     shopcfg.validate_drops(shopcfg.default_drops())}
        for recipe in shopcfg.validate_recipes(shopcfg.default_recipes()):
            for material in recipe["materials"]:
                self.assertIn(material["id"], droppable,
                              "配方「%s」要 %d，但 drops.json 里没有任何规则掉它"
                              % (recipe["name"], material["id"]))


if __name__ == "__main__":
    unittest.main()
