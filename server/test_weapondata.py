#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""武器表的测试（`server/weapondata.py` + `tools/weapondata.py`）。

分两层，和 `test_mapdata.py` 一个路数：

1. **合成数据**（`SyntheticTests`）—— 自己造一张小表，把「格式版本不认就当
   没有数据」「取不到返回 None」这类边界钉死。不依赖产物，任何机器都跑得动。
2. **真实产物**（`RealTableTests`）—— `server/bot_weapons.json` 在的话再跑：
   每个玩家角色都得有一把能用的枪、可用武器的四个条件逐条成立。
   产物不在就整类跳过（打包机上可能还没跑提取）。

★ 这里最要命的一条是 **`handle_step`**：它决定 bot 每发 `rpFire` 之后把
弹体句柄计数器往前推几格。推错了 `rpExplode` 会被收方**静默丢弃**
（`0x492750` 查不到弹体就整个 return），表现是「子弹飞过去不炸、一滴血
不掉」，而且一局之内不自愈（§42 / D28）。所以「不确定就返回 None、
bot 就不用这把枪」这条口径要有用例守着。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import weapondata                                              # noqa: E402

TOOLS = os.path.join(os.path.dirname(HERE), "tools")


def load_tool():
    """按**路径**加载 `tools/weapondata.py`；不在就返回 None（用例整类跳过）。

    ★★ **绝不能往 `sys.path` 里塞 `tools/`**：那个目录里也有一个
    `mapdata.py`（离线提取器），塞进去之后同一批测试里的
    `import mapdata` 会导到工具那份，`test_mapdata` 整个跑偏。
    这里用 `importlib` 起一个**独立模块名**，谁都不影响。
    """
    import importlib.util
    path = os.path.join(TOOLS, "weapondata.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("weapondata_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = load_tool()


class SyntheticTests(unittest.TestCase):
    """自己造一张表，钉住加载器的边界行为。"""

    def store(self, table, tmp="_test_weapons.json"):
        path = os.path.join(HERE, tmp)
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(table, fp)
        self.addCleanup(os.remove, path)
        return weapondata._Store(path)

    def test_missing_file_is_not_fatal(self):
        """★ 没有武器表**不该让服务端起不来** —— bot 照样会跑会跳（M3a），
        只是不开枪。"""
        store = weapondata._Store(os.path.join(HERE, "_no_such_file.json"))
        self.assertEqual(0, store.count())
        self.assertIsNone(store.get(1002010))
        self.assertIsNone(store.preferred_for(2))

    def test_unknown_format_is_treated_as_no_data(self):
        """格式版本对不上就当没有 —— 宁可 bot 不开枪，也不要按错的布局
        解出一把参数乱七八糟的武器。"""
        store = self.store({"format": 999, "weapons": {"1": {"id": 1}}})
        self.assertEqual(0, store.count())

    def test_lookup_returns_none_for_unknown_ids(self):
        store = self.store({
            "format": weapondata.FORMAT,
            "weapons": {"7": {"id": 7, "damage": 3, "handle_step": 1}},
            "preferred": {}, "usable": [],
        })
        self.assertEqual(3, store.get(7).damage)
        self.assertIsNone(store.get(8))

    def test_weapon_exposes_raw_fields_as_attributes(self):
        store = self.store({
            "format": weapondata.FORMAT,
            "weapons": {"7": {"id": 7, "damage": 3, "handle_step": 1,
                              "section": "ch07-01", "cooling_ms": 140}},
            "preferred": {"7": 7}, "usable": [7],
        })
        weapon = store.get(7)
        self.assertEqual("ch07-01", weapon.section)
        self.assertEqual(140, weapon.raw["cooling_ms"])
        with self.assertRaises(AttributeError):
            weapon.no_such_field

    def test_preferred_is_looked_up_by_character(self):
        store = self.store({
            "format": weapondata.FORMAT,
            "weapons": {"7": {"id": 7, "damage": 3, "handle_step": 1}},
            "preferred": {"2": 7}, "usable": [7],
        })
        self.assertEqual(7, store.preferred_for(2).id)
        self.assertIsNone(store.preferred_for(3))


@unittest.skipIf(TOOL is None, "tools/weapondata.py 导不进来")
class HandleStepRuleTests(unittest.TestCase):
    """★★ `handle_step_of()` —— 整个 M3b 里最要命的那条判据。"""

    def test_plain_bullet_advances_one(self):
        """语料实测：`1002010` 93/101、`1109010` 76/82、`1000010` 54/60
        都是步进 1（剩下的零头是 `rpExplode` 到达顺序的噪声，§43）。"""
        self.assertEqual(1, TOOL.handle_step_of({"damage": 3}))
        self.assertEqual(1, TOOL.handle_step_of(
            {"damage": 3, "spread_frags": 1}))

    def test_splash_weapons_advance_two(self):
        """带溅射的武器爆炸时额外创建一个 `SplashDamage` 对象，
        多分配一次句柄（`0x484920`）。语料实测恒 2。"""
        self.assertEqual(2, TOOL.handle_step_of(
            {"damage": 22, "splash_range": 100}))

    def test_multi_fragment_weapons_advance_once_per_fragment(self):
        """★ `SpreadFrags = N` 的武器一发**造 N 颗**弹体（§46：收侧
        `OnFire` 的内层循环跑 `SpreadFrags` 轮，每轮注册一个句柄），
        所以句柄一发前进 N 格；再带溅射就是 2N。"""
        self.assertEqual(5, TOOL.handle_step_of(
            {"damage": 3, "spread_frags": 5}))
        self.assertEqual(4, TOOL.handle_step_of(
            {"damage": 15, "spread_frags": 2, "splash_range": 50}))

    def test_shots_is_the_fragment_count(self):
        """★★ `rpFire +22` 必须填满 `SpreadFrags` —— 收侧外层轮数是
        `count / SpreadFrags` 的**整数除法**，填 1 打 3 散弹的枪
        等于 `1 / 3 = 0` 轮，一颗子弹都造不出来（§46）。"""
        self.assertEqual(1, TOOL.shots_of({}))
        self.assertEqual(1, TOOL.shots_of({"spread_frags": 1}))
        self.assertEqual(3, TOOL.shots_of({"spread_frags": 3}))

    def test_fire_interval_falls_back_to_reload_time(self):
        """缺 `CoolingTime` 的武器**同时也没有 `MagazineCount`**
        （打一发装一次），所以装填时间就是它的发射间隔。"""
        self.assertEqual(140, TOOL.fire_interval_of({"cooling_ms": 140,
                                                     "reload_ms": 1400}))
        self.assertEqual(1500, TOOL.fire_interval_of({"reload_ms": 1500}))
        self.assertIsNone(TOOL.fire_interval_of({}))


@unittest.skipIf(not os.path.isfile(weapondata.DATA_PATH),
                 "server/bot_weapons.json 还没生成")
class RealTableTests(unittest.TestCase):
    """真产物：`tools\\weapondata.py` 跑出来的那份。"""

    #: 玩家真的能选的角色（§11 / `account_store.BASE_CHARACTER_IDS` 那一套）。
    PLAYABLE = (0, 1, 2) + tuple(range(100, 111))

    def test_table_is_not_empty(self):
        self.assertGreater(weapondata.count(), 100)
        self.assertGreater(len(weapondata.usable()), 10)

    def test_every_playable_character_has_a_gun(self):
        """★ 每个玩家角色都得有一把 bot 能用的枪 —— 否则用户 `/char` 换到
        那个角色，bot 就哑火了，而且现象和「句柄错了」长得一样。"""
        for character in self.PLAYABLE:
            with self.subTest(character=character):
                self.assertIsNotNone(weapondata.preferred_for(character),
                                     f"角色 {character} 没有可用武器")

    def test_usable_weapons_meet_all_conditions(self):
        """`_is_usable()` 的五个条件（D29 + 会话 14 放宽）逐条复核一遍产物。"""
        for ammo in weapondata.usable():
            weapon = weapondata.get(ammo)
            with self.subTest(ammo=ammo):
                self.assertGreaterEqual(weapon.handle_step, weapon.shots)
                self.assertEqual(weapon.handle_step,
                                 weapon.shots * (2 if weapon.splash_range else 1))
                self.assertIn(weapon.power_control, TOOL.KNOWN_POWER_MODES)
                self.assertGreater(weapon.damage, 0)
                self.assertTrue(weapon.fire_interval_ms)
                self.assertIsNotNone(weapon.raw.get("character"))

    def test_every_playable_character_has_all_three_slots(self):
        """★★ 用户 2026-08-26 报的那条：「所有角色应该都有 3 个武器能用」。

        会话 14 之前不成立，两个原因叠在一起：`weapon.ini` 里角色 1 / 3 的
        1 号武器节名是**大写** `[CH01-01]`（正则漏了，§45），
        而 2 号武器全是抛物线、当时的 `_is_usable()` 一律排除。
        """
        for character in self.PLAYABLE:
            slots = sorted(w.raw["slot"]
                           for w in weapondata.usable_for(character))
            with self.subTest(character=character):
                self.assertEqual([1, 2, 3], slots,
                                 f"角色 {character} 的可用槽位是 {slots}")

    def test_the_uppercase_sections_are_not_dropped(self):
        """★ `[CH01-01]` / `[CH03-01]` 这两个大写节的回归钉子（§45）。"""
        for ammo in (1001010, 1003010):
            weapon = weapondata.get(ammo)
            self.assertIsNotNone(weapon, f"{ammo} 不在表里")
            self.assertEqual(1, weapon.raw["slot"])

    def test_preferred_weapons_are_all_usable(self):
        usable = set(weapondata.usable())
        for character in self.PLAYABLE:
            weapon = weapondata.preferred_for(character)
            if weapon is not None:
                self.assertIn(weapon.id, usable)

    def test_known_weapon_matches_the_original_ini(self):
        """`ch02-01`（角色 2 的基础枪）—— 拿它当基准，产物格式变了会炸。
        `Damage=3` 和语料里 `rpExplode +24` 的最小值 3.0 正好对上（§43）。"""
        weapon = weapondata.get(1002010)
        self.assertIsNotNone(weapon)
        self.assertEqual(3, weapon.damage)
        self.assertEqual(100.0, weapon.velocity)
        self.assertEqual(0.0, weapon.gravity)
        self.assertEqual(140, weapon.fire_interval_ms)
        self.assertEqual(1, weapon.handle_step)
        self.assertEqual(80.0, weapon.lockon_range)


if __name__ == "__main__":
    unittest.main()
