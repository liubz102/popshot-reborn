#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""物品表的测试（`server/shopdata.py` + `tools/shopdata.py`）。

分两层，和 `test_weapondata.py` 一个路数：

1. **合成数据**（`SyntheticTests` / `ResolveEquippedTests`）—— 自己造一张小表，
   把「格式版本不认就当没有数据」「取不到返回 None」「槽位冲突怎么算」这类
   边界钉死。不依赖产物，任何机器都跑得动。
2. **真实产物**（`RealTableTests`）—— `server/shop_items.json` 在的话再跑。
   产物不在就整类跳过（打包机上可能还没跑提取）。

★ 这里最要命的两条：

- **`conflicts` / `resolve_equipped`**：装备槽算错 = 一个人同时穿两件上衣，
  或者换了装备旧的没脱掉。判据只能是 `part_flag` 按位与（D6）。
- **`exists`**：中文版客户端不认识的 id 一旦发下去，界面上就是空格子
  （结算界面那一栏更直接，`0x415a94` 查不到整件跳过，§3）。
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import shopdata                                                # noqa: E402

TOOLS = os.path.join(os.path.dirname(HERE), "tools")


def load_tool():
    """按**路径**加载 `tools/shopdata.py`；不在就返回 None（用例整类跳过）。

    ★★ **绝不能往 `sys.path` 里塞 `tools/`**：那个目录里也有
    `mapdata.py` / `weapondata.py` / `chrprops.py`（离线提取器），
    塞进去之后同一批测试里的 `import mapdata` 会导到工具那份。
    这里用 `importlib` 起一个**独立模块名**，谁都不影响。
    """
    import importlib.util
    path = os.path.join(TOOLS, "shopdata.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("shopdata_tool", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["shopdata_tool"] = module
    spec.loader.exec_module(module)
    return module


TOOL = load_tool()


def make_table(items, fmt=shopdata.FORMAT):
    by_kind = {}
    for key, value in items.items():
        by_kind.setdefault(value.get("kind", "other"), []).append(int(key))
    for ids in by_kind.values():
        ids.sort()
    return {"format": fmt, "source": "test", "count": len(items),
            "bonus_index": {"attack": 1, "defense": 2}, "by_kind": by_kind,
            "items": items, "promotions": []}


#: 一张够用的小表：上衣 / 下装 / 全身套装 / 武器槽1 / 武器槽2 / 材料 / 只有货架的。
SYNTHETIC = {
    "1010001": {"id": 1010001, "kind": "armor", "part_flag": 1, "part": 1,
                "character": 0, "icon": "ch00B0001", "stock": True,
                "ownable": True, "bonus": {"defense": 3}},
    "1020001": {"id": 1020001, "kind": "armor", "part_flag": 2, "part": 2,
                "character": 0, "icon": "ch00L0001", "stock": True,
                "ownable": True},
    "1990001": {"id": 1990001, "kind": "package", "part_flag": 31, "part": 99,
                "character": 0, "icon": "셋트", "name_kr": "셋트",
                "stock": True, "ownable": True},
    "1120041": {"id": 1120041, "kind": "weapon", "part_flag": 1024, "part": 12,
                "character": 0, "icon": "무기_리볼버 R1", "name_kr": "리볼버 R1",
                "stock": True, "ownable": True, "slot": 1, "series": "R",
                "tier": 1, "ammo_id": 1000013,
                "weapon": {"damage": 4, "reload_ms": 700}},
    "1120051": {"id": 1120051, "kind": "weapon", "part_flag": 2048, "part": 12,
                "character": 0, "stock": True, "ownable": True, "slot": 2},
    "2120041": {"id": 2120041, "kind": "weapon", "part_flag": 1024, "part": 12,
                "character": 1, "stock": True, "ownable": True, "slot": 1},
    "30018": {"id": 30018, "kind": "material", "part_flag": 0,
              "icon": "청동파이프", "name_kr": "청동파이프",
              "stock": False, "ownable": True},
    "1510001": {"id": 1510001, "kind": "armor", "part_flag": 1, "part": 1,
                "character": 0, "timed": True, "stock": True,
                "ownable": False},          # ★ 只有货架、不能进背包
}


class _TableCase(unittest.TestCase):
    """把 `shopdata.STORE` 换成一张临时表，用完还原。"""

    items = SYNTHETIC
    fmt = shopdata.FORMAT

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "shop_items.json")
        with open(self.path, "w", encoding="utf-8", newline="\n") as fp:
            json.dump(make_table(self.items, self.fmt), fp, ensure_ascii=False)
        self._saved = shopdata.STORE
        shopdata.STORE = shopdata._Store(self.path)

    def tearDown(self):
        shopdata.STORE = self._saved
        self.tmp.cleanup()


class SyntheticTests(_TableCase):

    def test_missing_id_returns_none_not_raise(self):
        # 调用方到处在拿玩家发来的 id 查表，抛异常会把整条连接打死。
        self.assertIsNone(shopdata.get(999999))
        self.assertFalse(shopdata.exists(999999))
        self.assertEqual(0, shopdata.part_flag(999999))
        self.assertEqual({}, shopdata.bonus(999999))
        self.assertEqual("other", shopdata.kind(999999))

    def test_garbage_id_returns_none(self):
        for junk in (None, "", "abc", [], {}):
            self.assertIsNone(shopdata.get(junk))

    def test_string_id_works(self):
        # 存档里的 key 是字符串，包里解出来的是 int —— 两边都得认。
        self.assertIsNotNone(shopdata.get("1120041"))
        self.assertIsNotNone(shopdata.get(1120041))

    def test_fields(self):
        item = shopdata.get(1120041)
        self.assertEqual("weapon", item.kind)
        self.assertEqual(1024, item.part_flag)
        self.assertEqual("R", item.series)
        self.assertEqual(1000013, item.ammo_id)
        self.assertEqual(700, item.weapon["reload_ms"])
        self.assertTrue(item.equippable)

    def test_material_is_not_equippable(self):
        item = shopdata.get(30018)
        self.assertTrue(shopdata.is_material(30018))
        self.assertFalse(item.equippable)
        self.assertEqual(0, item.part_flag)

    def test_bonus_is_a_copy(self):
        # 调用方改了返回值不能污染表 —— 商店 tooltip 会就地加工。
        got = shopdata.bonus(1010001)
        got["defense"] = 999
        self.assertEqual({"defense": 3}, shopdata.bonus(1010001))

    def test_usable_by_character(self):
        self.assertTrue(shopdata.usable_by(1120041, 0))
        self.assertFalse(shopdata.usable_by(1120041, 1))
        self.assertTrue(shopdata.usable_by(2120041, 1))
        # 材料不限角色
        self.assertTrue(shopdata.usable_by(30018, 2))

    def test_conflicts_uses_bitwise_and(self):
        self.assertTrue(shopdata.conflicts(1010001, 1010001))     # 同一件
        self.assertFalse(shopdata.conflicts(1010001, 1020001))    # 上衣 vs 下装
        self.assertTrue(shopdata.conflicts(1010001, 1990001))     # 上衣 vs 全身套装
        self.assertTrue(shopdata.conflicts(1020001, 1990001))     # 下装 vs 全身套装
        self.assertFalse(shopdata.conflicts(1120041, 1120051))    # 武器槽 1 vs 2
        self.assertTrue(shopdata.conflicts(1120041, 2120041))     # 都占武器槽 1

    def test_materials_never_conflict(self):
        # part_flag = 0，想拿多少拿多少。
        self.assertFalse(shopdata.conflicts(30018, 30018))
        self.assertFalse(shopdata.conflicts(30018, 1010001))

    def test_ids_of_kind(self):
        self.assertEqual([30018], shopdata.ids_of_kind("material"))
        self.assertIn(1120041, shopdata.ids_of_kind("weapon"))
        self.assertEqual([], shopdata.ids_of_kind("没有这一类"))


class WrongFormatTests(_TableCase):
    """★ 版本对不上就当没有数据 —— 宁可商店空着，也不要按错的布局解。"""

    fmt = shopdata.FORMAT + 1

    def test_everything_is_empty(self):
        self.assertEqual(0, shopdata.count())
        self.assertIsNone(shopdata.get(1120041))
        self.assertEqual([], shopdata.ids_of_kind("weapon"))


class MissingFileTests(unittest.TestCase):
    """产物文件干脆不存在时，服务端也得起得来。"""

    def test_no_file_is_not_fatal(self):
        store = shopdata._Store(os.path.join(HERE, "没有这个文件.json"))
        self.assertEqual(0, store.count())
        self.assertIsNone(store.get(1120041))


class ResolveEquippedTests(_TableCase):

    def test_keeps_non_conflicting(self):
        kept, dropped = shopdata.resolve_equipped([1010001, 1020001, 1120041])
        self.assertEqual([1010001, 1020001, 1120041], kept)
        self.assertEqual([], dropped)

    def test_first_wins(self):
        # ★ 「玩家刚点的那件」放最前面 ⇒ 换装 = 新的顶掉旧的。
        kept, dropped = shopdata.resolve_equipped([1990001, 1010001, 1020001])
        self.assertEqual([1990001], kept)
        self.assertEqual([1010001, 1020001], dropped)
        kept, dropped = shopdata.resolve_equipped([1010001, 1990001])
        self.assertEqual([1010001], kept)
        self.assertEqual([1990001], dropped)

    def test_drops_unknown_ids(self):
        # 客户端表里没有的 id 发下去只会画出空格子。
        kept, dropped = shopdata.resolve_equipped([999999, 1010001])
        self.assertEqual([1010001], kept)
        self.assertEqual([999999], dropped)

    def test_drops_stock_only_items(self):
        # ★ 纯期限售卖形态只有 [Stock-] 没有 [Item-]，塞进背包客户端认不出来。
        kept, dropped = shopdata.resolve_equipped([1510001])
        self.assertEqual([], kept)
        self.assertEqual([1510001], dropped)

    def test_materials_pass_through(self):
        # part_flag=0 不占槽，多少件都留得下（虽然实际不会拿材料当装备发）。
        kept, _ = shopdata.resolve_equipped([30018, 1010001])
        self.assertEqual([30018, 1010001], kept)

    def test_empty_and_none(self):
        self.assertEqual(([], []), shopdata.resolve_equipped([]))
        self.assertEqual(([], []), shopdata.resolve_equipped(None))


@unittest.skipUnless(TOOL is not None, "tools/shopdata.py 不在")
class ToolTests(unittest.TestCase):
    """离线提取器里那几个「算错了整张表就歪」的纯函数。"""

    def test_bonus_value_parses_as_int_like_wtoi(self):
        # ★★ 客户端用的是 `_wtoi`（`0x413268`）：`5.0` -> 5、`0.5` -> 0。
        #    这里用 float 的话，服务端展示的数字和玩家实际吃到的加成对不上。
        self.assertEqual((5, None), TOOL.parse_bonus_value("5.0"))
        self.assertEqual((0, None), TOOL.parse_bonus_value("0.5"))
        self.assertEqual((-2, None), TOOL.parse_bonus_value("-2.0"))
        self.assertEqual((30, None), TOOL.parse_bonus_value("30.0"))

    def test_bonus_value_keeps_lua_source(self):
        lua = ("if mychr:GetLastBulletROHIdx() == 110001 "
               "then return 15 else return 0 end")
        self.assertEqual((None, lua), TOOL.parse_bonus_value(lua))
        self.assertEqual((None, "return - GetDefaultMaxHp(0) * 0.1"),
                         TOOL.parse_bonus_value("return - GetDefaultMaxHp(0) * 0.1"))

    def test_bonus_value_empty(self):
        self.assertEqual((None, None), TOOL.parse_bonus_value(""))
        self.assertEqual((None, None), TOOL.parse_bonus_value(None))

    def test_weapon_variant(self):
        # D 系槽 1/2/3 的 1~3 级
        self.assertEqual(("D", 1, 1), TOOL.weapon_variant(1120011))
        self.assertEqual(("D", 2, 3), TOOL.weapon_variant(1120023))
        self.assertEqual(("D", 3, 2), TOOL.weapon_variant(1120032))
        self.assertEqual(("R", 1, 1), TOOL.weapon_variant(1120041))
        self.assertEqual(("R", 3, 3), TOOL.weapon_variant(1120063))
        self.assertEqual(("F", 1, 1), TOOL.weapon_variant(1120071))
        self.assertEqual(("F", 3, 3), TOOL.weapon_variant(3120093))

    def test_weapon_variant_rejects_non_drf(self):
        # ★ 只看尾两位的话，SE(…0001~0004) 和 Platinum(…0101) 会被误判成 D 系。
        for item_id in (1120001, 1120002, 1120003, 1120004, 1120101):
            self.assertEqual((None, None, None), TOOL.weapon_variant(item_id))

    def test_classify(self):
        self.assertEqual("weapon", TOOL.classify(1120041, 1024)[0])
        self.assertEqual("armor", TOOL.classify(1010015, 1)[0])
        self.assertEqual("material", TOOL.classify(30018, 0)[0])
        self.assertEqual("material", TOOL.classify(10001, 0)[0])
        self.assertEqual("material", TOOL.classify(110001, 0)[0])
        self.assertEqual("consumable", TOOL.classify(210001, 0)[0])
        self.assertEqual("pet", TOOL.classify(220001, 0)[0])
        self.assertEqual("enchant", TOOL.classify(230001, 0)[0])
        self.assertEqual("title", TOOL.classify(560002, 8192)[0])
        self.assertEqual("character", TOOL.classify(101400001, 0)[0])

    def test_classify_character_and_part(self):
        kind, part, character, timed = TOOL.classify(2010015, 1)
        self.assertEqual(("armor", 1, 1, False), (kind, part, character, timed))
        # ★ 部位码 + 50 = 期限制售卖形态，归到同一个部位、另打标记
        kind, part, character, timed = TOOL.classify(1510001, 1)
        self.assertEqual(("armor", 1, 0, True), (kind, part, character, timed))

    def test_icon_name(self):
        self.assertEqual(("무기_리볼버 R1", "리볼버 R1"),
                         TOOL.icon_name("Images/Shop/무기_리볼버 R1.png"))
        self.assertEqual(("청동파이프", "청동파이프"),
                         TOOL.icon_name("Images/Shop/청동파이프.png"))
        # 模型编号不是名字
        self.assertEqual(("ch00B0015", None),
                         TOOL.icon_name("Images/Shop/ch00B0015.png"))
        self.assertEqual((None, None), TOOL.icon_name(""))

    def test_tag_ammo_id(self):
        self.assertEqual(1000015, TOOL.tag_ammo_id("1000015.0"))
        self.assertIsNone(TOOL.tag_ammo_id(""))
        self.assertIsNone(TOOL.tag_ammo_id("Models/Characters/ch00/ch00B0001.msh"))
        self.assertIsNone(TOOL.tag_ammo_id("FFD90005"))     # 喷漆的 ARGB 颜色

    def test_parse_reward(self):
        self.assertEqual({"kind": "money", "amount": 100},
                         dict(TOOL.parse_reward("0,100")))
        self.assertEqual({"kind": "experience", "amount": 20},
                         dict(TOOL.parse_reward("2,20")))
        self.assertEqual({"kind": "item", "item_id": 30018, "days": 0},
                         dict(TOOL.parse_reward("1,0030018,0")))
        self.assertEqual({"kind": "item", "item_id": 1010009, "days": 30},
                         dict(TOOL.parse_reward("1,1010009,30")))
        # ★ 类型 3 的第三格恒是 100，那是韩版的「永久」写法，不是天数
        self.assertEqual({"kind": "item", "item_id": 1010009, "days": 0},
                         dict(TOOL.parse_reward("3,1010009,100")))
        self.assertIsNone(TOOL.parse_reward(""))
        self.assertIsNone(TOOL.parse_reward("9,1"))


@unittest.skipUnless(os.path.isfile(shopdata.DATA_PATH), "shop_items.json 不在")
class RealTableTests(unittest.TestCase):
    """真产物的体检。产物不在就整类跳过。"""

    def test_format_matches(self):
        with open(shopdata.DATA_PATH, "r", encoding="utf-8") as fp:
            raw = json.load(fp)
        self.assertEqual(shopdata.FORMAT, raw["format"])

    def test_has_items(self):
        self.assertGreater(shopdata.count(), 1000)

    def test_bonus_index_matches_client_name_table(self):
        # ★ 顺序来自客户端 0x732bfc 的名字表，动了就说明表和客户端脱节了（§2）。
        with open(shopdata.DATA_PATH, "r", encoding="utf-8") as fp:
            index = json.load(fp)["bonus_index"]
        self.assertEqual(1, index["attack"])
        self.assertEqual(2, index["defense"])
        self.assertEqual(5, index["hp"])
        self.assertEqual(12, index["dashattack"])

    def test_three_series_are_complete(self):
        """★ 中文版实际有 63 件 D/R/F（韩版 81，砍了 3 级的 18 件）。

        M5 的商店就卖这一批。数量对不上说明提取口径或素材版本变了。
        """
        found = {}
        for item_id in shopdata.ids_of_kind("weapon"):
            item = shopdata.get(item_id)
            if item.series and not item.timed:
                found.setdefault((item.character, item.series), []).append(item_id)
        self.assertEqual(9, len(found), "应当是 3 个角色 × 3 个系列")
        for key, ids in found.items():
            self.assertEqual(7, len(ids), "%s 系列的件数不对：%s" % (key, ids))
        self.assertEqual(63, sum(len(v) for v in found.values()))

    def test_weapon_slot_matches_part_flag(self):
        """id 推出来的槽位必须和 `PartFlag` 一致 —— 这是 `0x030b` 的依据。"""
        for item_id in shopdata.ids_of_kind("weapon"):
            item = shopdata.get(item_id)
            if item.slot is None:
                continue
            self.assertEqual(shopdata.WEAPON_SLOT_FLAGS[item.slot - 1],
                             item.part_flag, "武器 %d 槽位和 PartFlag 对不上" % item_id)

    def test_r_series_reloads_faster_than_d(self):
        """★ 「极速 R 换弹更快」是商店的卖点，也是 M3 实机验的判据（§2）。"""
        d1 = shopdata.get(1120011)      # 리볼버 D1
        r1 = shopdata.get(1120041)      # 리볼버 R1
        f1 = shopdata.get(1120071)      # 리볼버 F1
        self.assertEqual(720, d1.weapon["reload_ms"])
        self.assertEqual(700, r1.weapon["reload_ms"])
        self.assertEqual(710, f1.weapon["reload_ms"])
        self.assertLess(r1.weapon["reload_ms"], d1.weapon["reload_ms"])

    def test_every_ownable_weapon_has_ammo_and_stats(self):
        """★ 判据是 `ownable`，不是 `timed`。

        只有 `[Item-]` 节的条目才有 `Tag`（= 弹药 id）。原版有两批
        **只有 `[Stock-]` 的售卖形态**：部位码 +50 的期限版（`timed`），
        以及榴弹类的 `1120220/221/222` 这种「基础 id + 变体位」的 54 条。
        它们进不了背包，也就没有弹药 id —— 这是原版数据的形状，不是缺失。
        """
        checked = 0
        for item_id in shopdata.ids_of_kind("weapon"):
            item = shopdata.get(item_id)
            if not item.ownable:
                continue
            checked += 1
            self.assertIsNotNone(item.ammo_id, "武器 %d 没有弹药 id" % item_id)
            self.assertIn("damage", item.weapon, "武器 %d 没有伤害" % item_id)
        self.assertGreaterEqual(checked, 63, "能进背包的武器至少有那 63 件 D/R/F")

    def test_stock_only_weapons_are_never_ownable(self):
        """★ 这一条是 M5 的护栏：货架形态**不能**塞进玩家背包。"""
        for item_id in shopdata.ids_of_kind("weapon"):
            item = shopdata.get(item_id)
            if item.ammo_id is None:
                self.assertFalse(item.ownable,
                                 "武器 %d 没有弹药 id 却能进背包" % item_id)

    def test_quest_materials_are_real_items(self):
        """★ 结算界面要画它们的图标 —— id 必须在表里，否则整件跳过（§3）。"""
        seen = 0
        for promo in shopdata.promotions():
            for reward in promo.get("rewards", ()):
                if reward["kind"] != "item" or reward["item_id"] >= 1000000:
                    continue
                seen += 1
                item_id = reward["item_id"]
                self.assertTrue(shopdata.exists(item_id),
                                "任务奖励的材料 %d 不在物品表里" % item_id)
                self.assertTrue(shopdata.ownable(item_id))
                self.assertTrue(shopdata.is_material(item_id))
        self.assertEqual(12, seen, "原版给材料的任务是 12 条（3 个角色线 × 4 关）")

    def test_every_material_is_ownable_and_slotless(self):
        for item_id in shopdata.ids_of_kind("material"):
            item = shopdata.get(item_id)
            self.assertTrue(item.ownable, "材料 %d 进不了背包" % item_id)
            self.assertEqual(0, item.part_flag, "材料 %d 居然占槽位" % item_id)
            self.assertIsNotNone(item.name_kr, "材料 %d 没有名字" % item_id)


if __name__ == "__main__":
    unittest.main()
