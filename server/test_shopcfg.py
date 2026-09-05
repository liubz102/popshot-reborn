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

import mapdata                                                 # noqa: E402
import shop                                                    # noqa: E402
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


class BackfillTests(_CfgCase):
    """`backfill_defaults()` —— 只增不改、幂等（2026-09-05 的 `바지` 事故）。"""

    def test_dry_run_writes_nothing(self):
        shopcfg.ensure_files(self.dir)
        path = shopcfg.path_of(shopcfg.SHOP_FILENAME, self.dir)
        raw = open(path, "rb").read()
        # 先删掉一条，制造「默认表里有、文件里没有」。
        data = json.loads(raw.decode("utf-8"))
        dropped = data["items"].pop(0)
        shopcfg.write_json(path, data)
        before = open(path, "rb").read()
        added = shopcfg.backfill_defaults(self.dir)          # apply 默认 False
        self.assertEqual([dropped["id"]],
                         [e["id"] for e in added[shopcfg.SHOP_FILENAME]])
        self.assertEqual(before, open(path, "rb").read())

    def test_apply_adds_only_what_is_missing(self):
        shopcfg.ensure_files(self.dir)
        path = shopcfg.path_of(shopcfg.SHOP_FILENAME, self.dir)
        data = json.load(open(path, encoding="utf-8"))
        full = len(data["items"])
        dropped = data["items"].pop(0)
        # ★ 用户手改过的那一条必须原样留着 —— 这是整个函数存在的意义。
        data["items"][0] = dict(data["items"][0], price=12345, name="我改的")
        mine = data["items"][0]["id"]
        # ★ 用户自己加的、默认表里根本没有的那一条也不能被吃掉。
        data["items"].append({"id": 1990001, "name": "我加的", "listed": True,
                              "price": 7, "level": 1, "days": 0})
        shopcfg.write_json(path, data)
        shopcfg.backfill_defaults(self.dir, apply=True)
        after = {e["id"]: e for e in
                 json.load(open(path, encoding="utf-8"))["items"]}
        self.assertIn(dropped["id"], after)                  # 补回来了
        self.assertEqual(12345, after[mine]["price"])        # 改过的没被盖
        self.assertEqual("我改的", after[mine]["name"])
        self.assertEqual("我加的", after[1990001]["name"])   # 自己加的还在
        self.assertEqual(full + 1, len(after))

    #: 小物品表里没有成套的铠甲（韩文名带部位后缀的那种），所以
    #: `default_recipes()` 在这儿是空的 —— 配方那两条用例自带一份「默认表」。
    FAKE_RECIPES = {"format": shopcfg.FORMAT, "recipes": [
        {"id": 1, "result": 1010001, "listed": True, "cost": 100,
         "materials": [{"id": 30018, "count": 1}]},
        {"id": 2, "result": 1020001, "listed": True, "cost": 200,
         "materials": [{"id": 30018, "count": 2}]},
    ]}

    def fake_recipe_defaults(self):
        """把 `recipe.json` 的默认生成器换成上面那两条，用完还原。"""
        spec = shopcfg._SPECS[shopcfg.RECIPE_FILENAME]
        patched = (spec[0], lambda: json.loads(json.dumps(self.FAKE_RECIPES)),
                   spec[2])
        shopcfg._SPECS[shopcfg.RECIPE_FILENAME] = patched
        self.addCleanup(shopcfg._SPECS.__setitem__,
                        shopcfg.RECIPE_FILENAME, spec)

    def test_apply_is_idempotent(self):
        self.fake_recipe_defaults()
        shopcfg.ensure_files(self.dir)
        path = shopcfg.path_of(shopcfg.RECIPE_FILENAME, self.dir)
        data = json.load(open(path, encoding="utf-8"))
        data["recipes"].pop(0)
        shopcfg.write_json(path, data)
        self.assertTrue(shopcfg.backfill_defaults(self.dir, apply=True))
        self.assertEqual({}, shopcfg.backfill_defaults(self.dir, apply=True))

    def test_backfilled_recipe_ids_do_not_collide(self):
        """★ 配方号撞车会让**整份文件**判非法（一条都读不出来）。"""
        self.fake_recipe_defaults()
        shopcfg.ensure_files(self.dir)
        path = shopcfg.path_of(shopcfg.RECIPE_FILENAME, self.dir)
        data = json.load(open(path, encoding="utf-8"))
        data["recipes"].pop(0)
        # 剩下那条占掉一个很大的号，逼补齐去接着往下数。
        data["recipes"][0]["id"] = 9000
        shopcfg.write_json(path, data)
        shopcfg.backfill_defaults(self.dir, apply=True)
        parsed, warnings = shopcfg.recipes(self.dir, _reload=True)
        self.assertEqual([], warnings)
        ids = [r["id"] for r in parsed]
        self.assertEqual(2, len(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_apply_leaves_a_backup(self):
        shopcfg.ensure_files(self.dir)
        path = shopcfg.path_of(shopcfg.SHOP_FILENAME, self.dir)
        data = json.load(open(path, encoding="utf-8"))
        data["items"].pop(0)
        shopcfg.write_json(path, data)
        shopcfg.backfill_defaults(self.dir, apply=True)
        backups = [n for n in os.listdir(self.dir)
                   if n.startswith(shopcfg.SHOP_FILENAME + ".bak-")]
        self.assertEqual(1, len(backups), backups)

    def test_a_broken_file_is_skipped_not_overwritten(self):
        # D10 的同一条：读不懂的文件**绝不**拿默认值盖掉。
        path = shopcfg.path_of(shopcfg.SHOP_FILENAME, self.dir)
        with open(path, "w", encoding="utf-8", newline="\n") as fp:
            fp.write("{ 这不是 json")
        raw = open(path, "rb").read()
        shopcfg.backfill_defaults(self.dir, apply=True)
        self.assertEqual(raw, open(path, "rb").read())

    def test_drops_are_not_backfilled(self):
        # 一条掉落规则没有天然主键 ⇒ 补齐只会补出重复。
        self.assertNotIn(shopcfg.DROPS_FILENAME, shopcfg.BACKFILL_KEYS)


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

    def test_rejects_two_recipes_for_the_same_result(self):
        """★★ `0x0606` 上行带的是**产物 itemId**，不是配方号
        （`0x45d738: push [rule+4]`，FINDINGS §27）—— 同产物两条配方，
        服务端根本分不清玩家点的是哪一条。这条不是我们的规矩，是协议的形状。
        """
        with self.assertRaises(shopcfg.ConfigError) as ctx:
            shopcfg.validate_recipes({"recipes": [
                dict(self.BASE, id=1),
                dict(self.BASE, id=2, cost=999)]})
        self.assertIn("一个产物只能有一条配方", str(ctx.exception))

    def test_different_results_are_fine(self):
        got = shopcfg.validate_recipes({"recipes": [
            dict(self.BASE, id=1),
            dict(self.BASE, id=2, result=1020001)]})
        self.assertEqual([1010001, 1020001], [r["result"] for r in got])


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

    def test_the_two_spellings_of_每个部位_are_both_recognised(self):
        """★★ 原版自己就不统一（2026-09-05 实机撞上的）：

        下装在 카실 / 프로코 身上叫 `다리`，在 타이 身上叫 `바지`；
        鞋一律叫 `신발`。漏掉 `바지` 的后果是**泰尔的四条下装一件都不进配方**，
        中文名也翻不出来 —— 用户报的「泰尔只能看到上衣和手套」就是它。

        ⚠ `신발` 也以 `발` 结尾 ⇒ 后缀必须**从长到短**试，否则会被切成
        `…아머 신`，套装名对不上，那一件照样消失（只是换个死法）。
        """
        for item_id, expected in ((1020067, "泰尔 佣兵铠甲·下装"),
                                  (1020064, "泰尔 大师铠甲·下装"),
                                  (2020067, "卡希尔 佣兵铠甲·下装"),
                                  (1040067, "泰尔 佣兵铠甲·鞋"),
                                  (2040064, "卡希尔 大师铠甲·鞋"),
                                  (1010067, "泰尔 佣兵铠甲·上衣")):
            self.assertEqual(expected,
                             shopcfg.item_name_zh(shopdata.get(item_id)),
                             item_id)

    def test_the_default_recipes_cover_每个角色_每个存在的部位(self):
        made = {r["result"] for r in shopcfg.default_recipes()["recipes"]}
        # 泰尔的下装（`바지`）和鞋（`신발`）—— 漏了后缀时这几条一件都没有。
        for item_id in (1020064, 1020065, 1020066, 1020067,
                        1040064, 1040067, 2040064, 2040067):
            self.assertIn(item_id, made, shopcfg.item_name_zh(
                shopdata.get(item_id)))

    def test_every_recipe_result_has_a_tab_in_the_composition_ui(self):
        """★★ 合成界面的标签树只有 8 个（`0x45e42f` 逐条读出来的，§33）——
        **没有武器、没有套装**。产物落在别的分类下 = 玩家在合成面板上永远
        点不到那一格，配方等于不存在（而且不会有任何报错）。
        """
        for recipe in shopcfg.validate_recipes(shopcfg.default_recipes()):
            category = shop.category_of(recipe["result"])
            self.assertIn(category, shop.COMPOSITION_CATEGORIES,
                          "配方「%s」的产物归在 %#x，合成界面里点不到"
                          % (recipe["name"], category))

    def test_every_recipe_material_can_actually_drop(self):
        """★ 配方要的材料必须有地方掉，否则那条配方永远合不出来。"""
        droppable = {r["material"] for r in
                     shopcfg.validate_drops(shopcfg.default_drops())}
        for recipe in shopcfg.validate_recipes(shopcfg.default_recipes()):
            for material in recipe["materials"]:
                self.assertIn(material["id"], droppable,
                              "配方「%s」要 %d，但 drops.json 里没有任何规则掉它"
                              % (recipe["name"], material["id"]))


class SchemaTests(unittest.TestCase):
    """★★ `SCHEMA` 和 `validate_*` **必须对得上**（D16）。

    用户的要求是「以后新增的字段也要同步显示在画面上」。管理页照着 `SCHEMA`
    生成输入框，所以给 validator 加一个字段却忘了登记 ⇒ 那个字段在画面上
    就是个隐形人；反过来登记了不存在的字段 ⇒ 画面上多一个存不进去的框。

    这条用例把「记得改两处」变成「漏改必然报红」—— 别改成宽松匹配。
    """

    CASES = (
        ("shop", shopcfg.default_shop, shopcfg.validate_shop),
        ("recipe", shopcfg.default_recipes, shopcfg.validate_recipes),
        ("drops", shopcfg.default_drops, shopcfg.validate_drops),
    )

    @staticmethod
    def _keys_of(parsed):
        entries = list(parsed.values()) if isinstance(parsed, dict) else parsed
        keys = set()
        for entry in entries:
            keys |= set(entry)
        return keys

    def test_every_validated_key_is_registered_and_vice_versa(self):
        for which, build, validate in self.CASES:
            produced = self._keys_of(validate(build()))
            registered = shopcfg.schema_keys(which)
            self.assertEqual(
                produced, registered,
                "%s：validator 有但没登记的 %s；登记了但 validator 不产出的 %s"
                % (which, sorted(produced - registered),
                   sorted(registered - produced)))

    def test_every_config_has_a_list_key_that_actually_exists(self):
        for which, build, _validate in self.CASES:
            spec = shopcfg.SCHEMA[which]
            self.assertIn(spec["list_key"], build(), which)
            self.assertTrue(spec["title"], which)
            self.assertTrue(spec["help"], which)      # 说明搬到页面上了，不能空

    def test_field_types_are_ones_the_page_can_draw(self):
        # 前台认得的就这几种；写错一个字，那一格会变成空白。
        known = {"item", "text", "int", "bool", "choice", "materials"}
        for which in shopcfg.SCHEMA:
            for field in shopcfg.SCHEMA[which]["fields"]:
                self.assertIn(field["type"], known,
                              "%s.%s" % (which, field["key"]))
                self.assertTrue(field.get("label"), field["key"])
                if field["type"] == "choice":
                    self.assertTrue(field.get("options"), field["key"])

    def test_the_stage_dropdown_offers_exactly_the_seven_real_quests(self):
        # ★ 客户端建房时的关卡下拉框只认静态表 `0x6dc52c` 里那七个 id
        #   （`tools/probe_quest_list.py`）。多一个少一个都会让运营在管理页里
        #   选出一条**永远不会命中**的掉落规则。
        self.assertEqual(list(range(1, 8)), sorted(shopcfg.QUEST_ZH))
        for name in shopcfg.QUEST_ZH.values():
            self.assertTrue(name.strip(), shopcfg.QUEST_ZH)
        field = next(f for f in shopcfg.SCHEMA["drops"]["fields"]
                     if f["key"] == "stage")
        self.assertEqual("choice", field["type"])
        self.assertTrue(field["optional"])          # 留空 = 不限
        self.assertEqual(sorted(shopcfg.QUEST_ZH),
                         [o["value"] for o in field["options"]])
        for option in field["options"]:
            self.assertIn(shopcfg.QUEST_ZH[option["value"]], option["label"])

    def test_the_difficulty_dropdown_shows_the_names_from_the_game(self):
        # ★ 难度下拉里不能再是「难度 1/2/3/4」（用户 2026-09-05）：那四个号
        #   在游戏里各有名字，运营记的是名字不是号码。
        # ★★ 档数不是我们定的 —— 客户端拼地图文件名那一段只认四档
        #   （`mapdata.DIFFICULTY_SUFFIX`），校验器的上限也是 4。三处必须一致，
        #   否则会出现「选得出来但永远命中不了」或者「存不进去」的档。
        self.assertEqual([1, 2, 3, 4], sorted(shopcfg.DIFFICULTY_ZH))
        self.assertEqual(mapdata.DIFFICULTY_SUFFIX.keys(),
                         shopcfg.DIFFICULTY_ZH.keys())
        field = next(f for f in shopcfg.SCHEMA["drops"]["fields"]
                     if f["key"] == "difficulty")
        self.assertEqual("choice", field["type"])
        self.assertTrue(field["optional"])          # 留空 = 不限
        self.assertEqual(sorted(shopcfg.DIFFICULTY_ZH),
                         [o["value"] for o in field["options"]])
        for option in field["options"]:
            self.assertIn(shopcfg.DIFFICULTY_ZH[option["value"]],
                          option["label"])
        # 校验器卡的上限就是下拉的最大值，多一档少一档都要在这里报红。
        raw = {"format": shopcfg.FORMAT, "rules": [
            {"mode": "quest", "difficulty": max(shopcfg.DIFFICULTY_ZH) + 1,
             "material": 10001, "prob": 50}]}
        with self.assertRaises(shopcfg.ConfigError):
            shopcfg.validate_drops(raw)

    def test_the_dropdown_does_not_become_a_validation_rule(self):
        # ★ 下拉是**方便**，不是规矩：关卡有几个是客户端的事，不该由掉落表
        #   来立。手改进来的别的号码照样存得下去（管理页会多画一项显示它）。
        raw = {"format": shopcfg.FORMAT, "rules": [
            {"mode": "quest", "stage": 99, "material": 10001, "prob": 50}]}
        self.assertEqual(99, shopcfg.validate_drops(raw)[0]["stage"])

    def test_the_defaults_no_longer_carry_guidance_keys(self):
        # ★ D16：`_说明` 搬到页面上了（`SCHEMA[...]["help"]`），不再写进文件。
        for which, build, _validate in self.CASES:
            leftovers = [k for k in build() if k.startswith("_")]
            self.assertEqual([], leftovers, which)
            self.assertTrue(shopcfg.SCHEMA[which]["help"], which)


if __name__ == "__main__":
    unittest.main()
