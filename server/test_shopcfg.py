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
import collections
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

    def test_creates_every_config_file(self):
        # ★ 拿 `_SPECS` 当判据，不写死一张清单 —— 加一份配置时这条要么
        #   自动跟上，要么在别处红，总之不会悄悄漏掉新文件。
        created = shopcfg.ensure_files(self.dir)
        self.assertEqual(sorted(created), sorted(shopcfg._SPECS))
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
        data["items"] = [{"id": 1120041, "listed": True, "price": 12345}]
        shopcfg.write_json(path, data)

        self.assertEqual([], shopcfg.ensure_files(self.dir))
        shopcfg.invalidate(self.dir)
        parsed, warnings = shopcfg.shop(self.dir)
        self.assertEqual([], warnings)
        self.assertEqual(12345, parsed[1120041]["price"])
        self.assertTrue(parsed[1120041]["listed"])

    def test_generated_files_are_lf_without_bom(self):
        # 铁律 3：.json 一律 LF 无 BOM（服务端包要在 Linux 上跑）。
        shopcfg.ensure_files(self.dir)
        for name in shopcfg._SPECS:
            with open(os.path.join(self.dir, name), "rb") as fp:
                raw = fp.read()
            self.assertNotIn(b"\r", raw, name + " 里有 CR")
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), name + " 有 BOM")

    def test_generated_files_pass_their_own_validators(self):
        # 生成出来的东西必须自己能读回去 —— 否则第一次启动就是坏的。
        shopcfg.ensure_files(self.dir)
        for loader in (shopcfg.items, shopcfg.shop, shopcfg.recipes,
                       shopcfg.drops):
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
        """★ 拿**物品库**试：它是最需要补齐的一份（用户 2026-09-06 报的
        「材料 / 礼包筛出来是 0」就是这份文件少收了一批东西）。"""
        shopcfg.ensure_files(self.dir)
        path = shopcfg.path_of(shopcfg.ITEMS_FILENAME, self.dir)
        data = json.load(open(path, encoding="utf-8"))
        full = len(data["items"])
        dropped = data["items"].pop(0)
        # ★ 用户手改过的那一条必须原样留着 —— 这是整个函数存在的意义。
        data["items"][0] = dict(data["items"][0], name="我改的")
        mine = data["items"][0]["id"]
        shopcfg.write_json(path, data)
        shopcfg.backfill_defaults(self.dir, apply=True)
        after = {e["id"]: e for e in
                 json.load(open(path, encoding="utf-8"))["items"]}
        self.assertIn(dropped["id"], after)                  # 补回来了
        self.assertEqual("我改的", after[mine]["name"])       # 改过的没被盖
        self.assertEqual(full, len(after))

    def test_apply_keeps_entries_the_default_table_never_had(self):
        """★ 用户自己在商店货架里加的东西不能被补齐吃掉（铁律 11）。

        `1120051` 是小表里一把没有系列号的武器 —— 默认表只上架 D/R/F 和
        登记过的特别版，所以它正是「默认表里没有、用户自己加进去卖」的那种条目。
        """
        shopcfg.ensure_files(self.dir)
        path = shopcfg.path_of(shopcfg.SHOP_FILENAME, self.dir)
        data = json.load(open(path, encoding="utf-8"))
        self.assertNotIn(1120051, [e["id"] for e in data["items"]])
        data["items"].append({"id": 1120051, "listed": True, "price": 7})
        shopcfg.write_json(path, data)
        shopcfg.backfill_defaults(self.dir, apply=True)
        after = {e["id"]: e for e in
                 json.load(open(path, encoding="utf-8"))["items"]}
        self.assertEqual(7, after[1120051]["price"])

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

    def test_no_name_here(self):
        """★ D31：中文名的唯一出处是物品库，`shop.json` 里没有这个字段。

        老文件里残留的 `name` 要被**丢掉**，不能原样带出来 —— 带出来的话
        「在物品库里改了名字，商店里还是老名字」又会回来。
        """
        got = self.ok({"id": 1120041, "name": "老文件里的名字"})
        self.assertNotIn("name", got[1120041])

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


class ValidateItemsTests(_CfgCase):
    """物品库 `items.json` —— 等级门槛 + 角色限定的唯一出处（D31）。"""

    BASE = {"id": 1120041, "level": 5}

    def ok(self, **over):
        entry = dict(self.BASE)
        entry.update(over)
        return shopcfg.validate_items({"items": [entry]})

    def bad(self, fragment=None, **over):
        with self.assertRaises(shopcfg.ConfigError) as ctx:
            self.ok(**over)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))

    def test_minimal(self):
        got = self.ok()
        self.assertEqual(5, got[1120041]["level"])
        # 角色限定照**原版数据**（这一把枪是泰尔的），文件里没写也一样。
        self.assertEqual(0, got[1120041]["character"])
        self.assertEqual("weapon", got[1120041]["kind"])

    def test_the_character_lock_comes_from_the_stock_data_only(self):
        """★ D31a（用户 2026-09-06）：角色限定**只认原版**，文件里写了当没看见。

        改宽了让别的角色穿上去，游戏里会闪退 —— 模型和贴图一人一套。
        ⇒ 手改 `items.json` 也没用，**也不报错**（这不是「填错了」，
        是「这一栏本来就轮不到你填」，和管理页上那一栏只读是同一件事）。
        """
        for wrote in (1, 2, 9, -1, None, "泰尔"):
            self.assertEqual(0, self.ok(character=wrote)[1120041]["character"],
                             wrote)

    def test_rejects_a_level_below_one(self):
        self.bad("level", level=0)

    def test_rejects_duplicate(self):
        with self.assertRaises(shopcfg.ConfigError):
            shopcfg.validate_items({"items": [dict(self.BASE),
                                              dict(self.BASE)]})

    def test_things_you_cannot_wear_keep_only_their_name(self):
        """★ 物品库**收全部**能进背包的东西（用户 2026-09-06）—— 材料 / 礼包 /
        消耗品也要有中文名，也要能在这一页上按类别筛出来。

        但「几级能穿 / 谁能穿」对它们没有意义（客户端根本不读那两格），
        所以文件里就算写了也当没看见：等级一律 1，角色一律不限。
        """
        got = shopcfg.validate_items({"items": [
            {"id": 30018, "name": "青铜管", "level": 5, "character": 1}]})
        self.assertEqual("青铜管", got[30018]["name"])
        self.assertEqual(1, got[30018]["level"])
        self.assertIsNone(got[30018]["character"])

    def test_rejects_unknown_and_stock_only_ids(self):
        self.bad("不在 shop_items.json", id=999999)
        self.bad("进不了背包", id=1510001)


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


def _all_item_ids():
    """真产物里的全部 id。★ 走 `ids_of_kind()`，别自己拼 —— 新加一类
    （比如以后收进礼包 / 钥匙）时这里自动跟上。"""
    ids = []
    for kind in shopdata.kinds():
        ids.extend(shopdata.ids_of_kind(kind))
    return sorted(ids)


@unittest.skipUnless(os.path.isfile(shopdata.DATA_PATH), "shop_items.json 不在")
class ItemDescTests(unittest.TestCase):
    """物品说明（`item_desc_zh`）—— 守的是「**别再漏掉一种加成**」（§53）。

    火焰蝙蝠 `220003` 的溅射加成在提示框里空了大半年，直到它引发闪退才被发现，
    根因就是这里的翻译表只覆盖了 13 种加成中的 5 种。下面这批用例把
    「13 种一个都不能少」和「客户端那几道显示硬闸」一起钉住。
    """

    #: 客户端认得的 13 种加成（名字表 `0x732c00..0x732c30`）。
    ALL_BONUS_KEYS = (
        "attack", "defense", "critical", "movespd", "hp", "sp",
        "teamdmg", "selfdmg", "antighostcnt", "heartboost",
        "incsplashrange", "dashattack", "teamreflection",
    )

    #: 「玩家能穿 / 能拿在手上」的那几类。★ 判据故意**不用** `part_flag != 0`
    #: —— 角色卡的 `part_flag` 是 0，但它照样出现在仓库格子里；当前上架的
    #: 617 件正好落在前 7 类里。拿类别判而不是拿 `shop.json` 判，是因为测试跑在
    #: 一个**空的** data 目录上（`run_tests` 特意指开的，别让测试跟着运营数据变）。
    #:
    #: ★ `title`（称号）现在**也收进来**：它整类还没上架（D44a），但用户
    #: 2026-09-09 说了将来要上，所以 20 个称号的说明文提前全部查好了（§53 ⑤）。
    EQUIPMENT_KINDS = ("armor", "weapon", "spray", "dash",
                       "character", "pet", "ring", "title")

    def test_every_bonus_the_client_knows_has_a_translation(self):
        # ★ 这一条就是防止「火焰蝙蝠」重演：新加成必须落在三张表之一里，
        #   落不进去就说明有人加了键却忘了写说明文。
        for key in self.ALL_BONUS_KEYS:
            known = (key in shopcfg.BONUS_ZH
                     or key in shopcfg.SPECIAL_BONUS_ZH
                     or key in shopcfg.DEAD_BONUS_KEYS)
            self.assertTrue(known, "加成 %s 没有中文说明" % key)

    def test_the_three_tables_do_not_overlap(self):
        numeric = set(shopcfg.BONUS_ZH)
        special = set(shopcfg.SPECIAL_BONUS_ZH)
        dead = set(shopcfg.DEAD_BONUS_KEYS)
        self.assertEqual(set(), numeric & special)
        self.assertEqual(set(), (numeric | special) & dead)
        # 数值行的排序表必须正好覆盖数值那一档，少一个就会在界面上消失。
        self.assertEqual(numeric, set(shopcfg.BONUS_ORDER))

    def test_no_bonus_key_in_the_real_table_is_unknown(self):
        # 产物里真出现过的键，一个都不能是三张表之外的。
        seen = set()
        for item_id in _all_item_ids():
            item = shopdata.get(item_id)
            seen.update(item.bonus or {})
            seen.update(item.bonus_lua or {})
        self.assertEqual(set(), seen - set(self.ALL_BONUS_KEYS))

    def test_the_hand_written_tables_point_at_real_items(self):
        for table in (shopcfg.SPECIAL_EFFECT_BY_ID, shopcfg.BONUS_LUA_ZH):
            for item_id in table:
                self.assertIsNotNone(shopdata.get(item_id),
                                     "说明表里的 %d 在物品表里不存在" % item_id)

    def test_every_lua_bonus_item_is_translated(self):
        # 12 条条件加成全部要有人话；漏一条就退回「（附带条件加成）」那句废话。
        for item_id in _all_item_ids():
            item = shopdata.get(item_id)
            if item.bonus_lua:
                self.assertIn(item_id, shopcfg.BONUS_LUA_ZH,
                              "%d 的条件加成没翻译" % item_id)

    def test_the_fire_bat_finally_says_what_it_does(self):
        # §47 / D55 那件事的回归护栏。
        self.assertEqual("溅射武器有 15% 概率范围扩大 10%",
                         shopcfg.item_desc_zh(shopdata.get(220003)))

    def test_the_three_exe_only_titles_are_documented(self):
        """★ 三个**任何数据文件里都查不到**的称号（§53 ⑤）。

        它们在 `EquipBonus-Chn.ini` 里连节都没有，效果全写死在 exe 里。
        用户 2026-09-09 说称号将来要上架，所以提前查好钉住 —— 这三条要是
        被人当成「没有加成」删掉，上架当天就会重演火焰蝙蝠那件事。
        """
        self.assertEqual("受到伤害时 50% 概率完全免疫",
                         shopcfg.item_desc_zh(shopdata.get(560004)))
        # ★ 「45」是**伤害点数**不是开枪次数（`0x50b404 sub eax,ecx` 扣的是
        #   翻倍后的伤害值），文案里必须写清楚，用户 2026-09-09 就问岔过一次。
        self.assertEqual("开局起伤害翻倍，累计造成 45 点伤害后失效",
                         shopcfg.item_desc_zh(shopdata.get(560005)))
        self.assertEqual("捡到「心」时 15% 概率让全队各回 5 点生命",
                         shopcfg.item_desc_zh(shopdata.get(560006)))
        # 这三件在物品表里确实一条加成都没有 —— 说明只可能来自手写表。
        for item_id in (560004, 560005, 560006):
            item = shopdata.get(item_id)
            self.assertEqual({}, item.bonus, item_id)
            self.assertEqual({}, item.bonus_lua, item_id)

    def test_every_title_says_what_it_does(self):
        # 20 个称号一个不落（上架前就得全查清，别等上架当天再踩）。
        blank = [i for i in shopdata.ids_of_kind("title")
                 if not shopcfg.item_desc_zh(shopdata.get(i)).strip()]
        self.assertEqual([], blank)

    def test_rare_bonuses_land_in_the_second_segment(self):
        # 防护装置：数值进第 1 段、特效进第 2 段，中间正好一个 `|`。
        desc = shopcfg.item_desc_zh(shopdata.get(220002))
        self.assertEqual(["防御 +2%", "每局可挡下 1 次「幽灵」干扰"],
                         desc.split("|"))

    def test_five_stat_armor_no_longer_loses_a_line(self):
        # 原来上限 4 行 + 字母序 ⇒ 满 5 项的铠甲「体力」被砍掉了。
        desc = shopcfg.item_desc_zh(shopdata.get(1010063))
        self.assertIn("体力", desc)
        self.assertEqual(2, len(desc.split("\n")))     # 3 项 + 2 项，压成两行

    def test_grenades_show_their_splash(self):
        # 榴弹真正的杀伤在溅射上，`weapon.ini` 有这两格但一直没画出来。
        desc = shopcfg.item_desc_zh(shopdata.get(1120022))
        self.assertIn("溅射 28　范围 100", desc)

    def test_cosmetics_say_so_instead_of_going_blank(self):
        # 用户 2026-09-09：留白分不清「真没有」和「漏写了」。
        for item_id, expected in ((1070001, "染色剂"),
                                  (101400001, "角色卡"),
                                  (1060002, "突击技"),
                                  (220007, "宠物")):
            desc = shopcfg.item_desc_zh(shopdata.get(item_id))
            self.assertIn(expected, desc)
            self.assertIn("无属性加成", desc)

    def test_materials_stay_blank(self):
        # 材料不是装备，说明照旧留白（`test_web_admin` 钉着它不带 desc 键）。
        self.assertEqual("", shopcfg.item_desc_zh(shopdata.get(10001)))

    def test_none_is_empty(self):
        self.assertEqual("", shopcfg.item_desc_zh(None))

    def test_nothing_breaks_the_client_side_limits(self):
        """客户端那几道硬闸，全表一件都不能碰到（§53）。

        ① 最多 **2 段**（`0x45c4c9` 的 `cmp i,2` 是循环顶部，`i` 只取 0/1）；
        ② 段内行数 ≤ 各自框子放得下的行数；
        ③ 单段 ≤ 511 字符（`_vsnwprintf` 的 `0x1ff`）、整串 ≤ 999
           （split 的栈缓冲 `wcsncpy 0x3e7`）。
        """
        limits = (shopcfg.ITEM_DESC_MAX_LINES, shopcfg.ITEM_DESC_MAX_LINES_2)
        for item_id in _all_item_ids():
            desc = shopcfg.item_desc_zh(shopdata.get(item_id))
            if not desc:
                continue
            segments = desc.split("|")
            self.assertLessEqual(len(segments), 2, item_id)
            self.assertLess(len(desc), 999, item_id)
            for index, segment in enumerate(segments):
                self.assertTrue(segment, "%d 切出了空段" % item_id)
                self.assertLess(len(segment), 511, item_id)
                self.assertLessEqual(len(segment.split("\n")), limits[index],
                                     item_id)

    def test_every_equippable_item_says_something(self):
        """★ 用户这一轮要的就是这个：能穿能拿的东西**没有一件**是空说明。

        改动前这几类里有 226 件空白（当前上架的 617 件里空 102 件）。

        `ownable` 那道过滤不能省：`1120220` 那批「售卖变体」只有 `[Stock-]`
        节、进不了背包（§11），压根没有提示框，也没有武器数值可写。

        ★ 称号也在里面 —— 20 个一个不落，包括 `560004` / `560005` / `560006`
        那三个**只在 exe 里写死、任何数据文件都查不到**的（§53 ⑤）。
        """
        blank = [item_id
                 for kind in self.EQUIPMENT_KINDS
                 for item_id in shopdata.ids_of_kind(kind)
                 if shopdata.ownable(item_id)
                 and not shopcfg.item_desc_zh(shopdata.get(item_id)).strip()]
        self.assertEqual([], blank)


@unittest.skipUnless(os.path.isfile(shopdata.DATA_PATH), "shop_items.json 不在")
class RealDefaultsTests(unittest.TestCase):
    """拿**真产物**生成一遍默认配置，看内容站不站得住。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        shopcfg.invalidate()

    def tearDown(self):
        shopcfg.invalidate()
        self.tmp.cleanup()

    def test_shop_lists_the_whole_catalogue(self):
        """★ 全量上架（D44 / D44b）：散件在商店买 —— 63 件 D/R/F 一件不少，
        加上特别版武器、散装铠甲、装饰、染色剂、突击技、外观套和强攻套，
        再加 11 张商城角色卡（D51：1000 金币、不限等级）。
        材料 / 消耗品 / 礼包 / 称号不卖（D44 那张表）。"""
        shop = shopcfg.validate_shop(shopcfg.default_shop())
        listed = [e for e in shop.values() if e["listed"]]
        self.assertEqual(495, len(listed), "上架件数变了 —— 改了 shopdefaults 的表就把这个数跟着改")
        kinds = {e["kind"] for e in listed}
        self.assertEqual({"weapon", "armor", "spray", "dash", "character"}, kinds)
        for entry in listed:
            self.assertGreater(entry["price"], 0, "上架的东西不能白送")
        cards = [e for e in listed if e["kind"] == "character"]
        self.assertEqual(11, len(cards))
        self.assertEqual({1000}, {e["price"] for e in cards}, "角色卡统一 1000 金币")
        rules = shopcfg.validate_items(shopcfg.default_items())
        for entry in cards:
            self.assertEqual((1, None), shopcfg.rule_of(rules, entry["id"]),
                             "角色卡不限等级、不限角色")
        for item_id in shopdata.ids_of_kind("weapon"):
            item = shopdata.get(item_id)
            if item.ownable and item.series and item.character is not None:
                self.assertIn(item_id, shop, "D/R/F 武器 %d 没上架" % item_id)

    def test_the_mercenary_tab_of_the_real_shelf_is_exactly_the_eleven_cards(self):
        """★ 用户要的是「商店 → 人物 → 佣兵 里有 11 张卡」（D51）—— 把上架 /
        ownable / 分类 / 角色过滤四道过滤合在一起对真模板算一遍。"""
        import shop
        from account_store import PREMIUM_CHARACTER_IDS, character_item_id
        shopcfg.ensure_files(self.tmp.name)
        expected = [character_item_id(c) for c in PREMIUM_CHARACTER_IDS]
        cards, warnings = shop.shelf_entries(category=0x30001, data_dir=self.tmp.name)
        self.assertEqual([], warnings)
        self.assertEqual(expected, [e["id"] for e in cards])
        # 父标签「人物」也收；预览角色是泰尔（0）时照样全列（卡不限角色）。
        parent, _ = shop.shelf_entries(category=0x30000, data_dir=self.tmp.name)
        self.assertEqual(expected, [e["id"] for e in parent])
        tai, _ = shop.shelf_entries(category=0x30001, character=0,
                                    data_dir=self.tmp.name)
        self.assertEqual(expected, [e["id"] for e in tai])
        # 「人物 → 英雄」= 0 永远是空的（§22）；「道具 → 其他」不再兜住角色卡。
        heroes, _ = shop.shelf_entries(category=0, data_dir=self.tmp.name)
        self.assertEqual([], heroes)
        other, _ = shop.shelf_entries(category=shop.CATEGORY_OTHER,
                                      data_dir=self.tmp.name)
        self.assertEqual([], [e for e in other if e["kind"] == "character"])

    def test_every_item_name_is_chinese(self):
        """★ 中文名的唯一出处是**物品库**（D31），所以这一条查的是它。"""
        items = shopcfg.validate_items(shopcfg.default_items())
        self.assertEqual(808, len(items), "物品库要收全部能进背包的东西")
        for item_id in shopdata.ids_of_kind("weapon"):
            item = shopdata.get(item_id)
            if not item.ownable or not item.series:
                continue
            name = items[item_id]["name"]
            self.assertTrue(any("一" <= ch <= "鿿" for ch in name),
                            "%d 的名字里没有中文：%s" % (item_id, name))

    def test_没有一件东西的默认中文名还是韩文(self):
        """★★ 用户 2026-09-06：管理页上不该再出现韩文物品名。

        **这一条是「翻漏了必然报红」的那道门**（§34 的教训：`NAME_ZH` 的键
        是**观察到的**词汇，不是规范 —— 补完必须反查一遍还剩几件）。
        物品表换一版、多出几件带韩文名的东西，也会在这里当场撞出来。

        ⚠ 原版**根本没给名字**的那 477 件（图标就是 `ch00B0015` 这种模型编号）
        不在管辖范围：它们退回 `#id` 或者原样的模型编号，没有韩文可翻，
        用户 2026-09-06 拍板保持原样。
        """
        hangul = [(entry["id"], entry["name"])
                  for entry in shopcfg.default_items()["items"]
                  if any("가" <= ch <= "힣" for ch in entry["name"])]
        self.assertEqual([], hangul,
                         "这几件还是韩文名 —— 往 shopcfg.NAME_ZH 里补")

    def test_recipes_are_valid_and_bounded(self):
        recipes = shopcfg.validate_recipes(shopcfg.default_recipes())
        self.assertGreaterEqual(len(recipes), 20)
        for recipe in recipes:
            self.assertLessEqual(len(recipe["materials"]), shopcfg.MAX_MATERIALS)
            self.assertGreater(recipe["cost"], 0)
            # 一件装备的材料总量得在「刷十几局能凑齐」的量级，别劝退。
            total = sum(m["count"] for m in recipe["materials"])
            self.assertLessEqual(total, 30, "配方 #%d 要 %d 个材料，太肝了"
                                 % (recipe["id"], total))

    def test_recipe_results_are_equippable(self):
        """产物都占装备槽（铠甲 / 戒指 / 宠物）；铠甲和戒指还得有加成 ——
        宠物里有纯外观的（熊猫），那是原版数据，不是我们漏了。"""
        recipes = shopcfg.validate_recipes(shopcfg.default_recipes())
        self.assertEqual(122, len(recipes), "配方条数变了 —— 改了 shopdefaults 的表就把这个数跟着改")
        kinds = collections.Counter()
        for recipe in recipes:
            item = shopdata.get(recipe["result"])
            kinds[item.kind] += 1
            self.assertTrue(item.equippable, "%d 不占装备槽" % item.id)
            if item.kind != "pet":
                self.assertTrue(item.bonus, "%d 一点加成都没有，合它干嘛" % item.id)
        self.assertEqual({"armor": 109, "ring": 6, "pet": 7}, dict(kinds))

    def test_drops_include_the_original_baseline(self):
        rules = shopcfg.validate_drops(shopcfg.default_drops())
        baseline = {(r.get("stage"), r.get("difficulty"), r["material"])
                    for r in rules if r.get("stage") is not None}
        # FINDINGS §12：原版给材料的 4 关（三个角色线去重后）。★ 第 7 关简单那条
        # 青铜管**故意不在**：用户 2026-09-07 把青铜管定成机械青蛙独占的通用材料，
        # 秘密基地改掉橡皮管（D49 第二轮）。
        for key in ((1, 2, 30018), (4, 3, 30019), (1, 3, 30018)):
            self.assertIn(key, baseline)
        self.assertNotIn((7, 1, 30018), baseline)

    def test_drops_follow_the_original_tiering(self):
        """★★ D49 的口径：每关每档正好两样、简单只通用、普通低档 / 困难高档、
        特殊材料只在本关掉、不进对战 —— `check_tiering` 一条条核。"""
        import shopdefaults
        rules = shopcfg.validate_drops(shopcfg.default_drops())
        self.assertEqual([], shopdefaults.check_tiering(rules))
        recipes = shopcfg.validate_recipes(shopcfg.default_recipes())
        self.assertEqual([], shopdefaults.check_recipes(rules, recipes))
        self.assertEqual(52, len(rules))

    def test_every_material_except_cards_has_a_recipe_and_a_drop(self):
        """材料掉了没人用、或有人用却掉不出来，都是「两张表各自对、合起来不成立」。
        卡片（6xxxx / 11~13xxxx）不算：原版按成就给，本版不上架（D44a）。"""
        droppable = {r["material"] for r in
                     shopcfg.validate_drops(shopcfg.default_drops())}
        used = set()
        for recipe in shopcfg.validate_recipes(shopcfg.default_recipes()):
            used |= {m["id"] for m in recipe["materials"]}
        for material in shopdata.ids_of_kind("material"):
            if material >= 60000:
                continue
            self.assertIn(material, droppable, "%d 没有地方掉" % material)
            self.assertIn(material, used, "%d 掉了但没有配方用它" % material)

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
                          "配方 #%d 的产物归在 %#x，合成界面里点不到"
                          % (recipe["id"], category))

    def test_every_recipe_material_can_actually_drop(self):
        """★ 配方要的材料必须有地方掉，否则那条配方永远合不出来。"""
        droppable = {r["material"] for r in
                     shopcfg.validate_drops(shopcfg.default_drops())}
        for recipe in shopcfg.validate_recipes(shopcfg.default_recipes()):
            for material in recipe["materials"]:
                self.assertIn(material["id"], droppable,
                              "配方 #%d 要 %d，但 drops.json 里没有任何规则掉它"
                              % (recipe["id"], material["id"]))


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
        # ★ 难度下拉里不能再是「难度 1/2/3」（用户 2026-09-05）：那几个号
        #   在游戏里各有名字，运营记的是名字不是号码。
        # ★★ 档数不是我们定的 —— **玩家选得到的只有三档**：引擎里有第 4 档
        #   （`mapdata.DIFFICULTY_SUFFIX` 四个后缀都在，bot 读地形要用），但
        #   中国区客户端选难度时一到 4 就绕回 1（`0x466496`，V0.3商店 §38；
        #   用户 2026-09-06 实机报「游戏里没有极限」）。下拉和校验器只认
        #   选得到的那三档，否则运营会配出一条**永远命中不了**的规则。
        self.assertEqual([1, 2, 3], sorted(shopcfg.DIFFICULTY_ZH))
        self.assertTrue(set(shopcfg.DIFFICULTY_ZH) < set(mapdata.DIFFICULTY_SUFFIX),
                        "下拉里的每一档都得是客户端拼地图文件名认得的那几档之一")
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
