#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`server/weaponcfg.py` —— 自定义武器数值 + 全武器说明文（X_Mod · X3）。

三件事要钉住：
1. 它是第七份运营配置（`shopcfg._SPECS`）：开服生成、热重载、坏文件退回出厂值、备份回滚同组；
2. 数值只认 9 把自定义武器，原版武器只能改说明文；有效值 = 覆盖 ∪ 参考值；
3. `0x0F01` 的载荷能编能解，两套模式各自带自己的 mask，字段顺序 / 类型和 hook 侧一致。
"""
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for sub in ("server", "test"):
    path = os.path.join(ROOT, sub)
    if path not in sys.path:
        sys.path.insert(0, path)

import shopcfg  # noqa: E402
import shopdata  # noqa: E402
import weaponcfg  # noqa: E402

CUSTOM = 1920001          # 泰尔 1 号「左轮 自定义」
CUSTOM_GRENADE = 1920002  # 泰尔 2 号（有溅射 / 最大初速）
ORIGINAL = 1120011        # 左轮 爆裂1（原版）


class _Case(unittest.TestCase):

    def setUp(self):
        if not shopdata.exists(CUSTOM):
            raise unittest.SkipTest("shop_items.json 里没有自定义武器")
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self._saved = shopcfg.DATA_DIR
        shopcfg.DATA_DIR = self.dir
        shopcfg.invalidate()

    def tearDown(self):
        shopcfg.DATA_DIR = self._saved
        shopcfg.invalidate()
        self.tmp.cleanup()


class RegistrationTests(_Case):

    def test_it_is_the_seventh_operations_config(self):
        self.assertIn(shopcfg.WEAPONS_FILENAME, shopcfg.config_filenames())
        self.assertEqual("自定义武器", shopcfg.config_title(shopcfg.WEAPONS_FILENAME))
        self.assertIsNotNone(shopcfg.validator_of(shopcfg.WEAPONS_FILENAME))
        self.assertNotIn("weapons", shopcfg.SCHEMA)        # 没有配置标签页
        # ★ 称号卡片必须仍然排最末（test_shopcfg / test_backup 拿它当「最新加的那份」）
        self.assertEqual(shopcfg.CARDS_FILENAME, list(shopcfg._SPECS)[-1])

    def test_first_boot_writes_an_empty_table(self):
        created = shopcfg.ensure_files(self.dir)
        self.assertIn(shopcfg.WEAPONS_FILENAME, created)
        self.assertEqual(weaponcfg.default_table(), weaponcfg.load())

    def test_a_missing_or_broken_file_means_no_overrides(self):
        self.assertEqual({}, weaponcfg.load()["custom"])
        with open(shopcfg.path_of(shopcfg.WEAPONS_FILENAME), "w", encoding="utf-8") as fp:
            fp.write("{ this is not json")
        shopcfg.invalidate()
        self.assertEqual({}, weaponcfg.load()["custom"])
        self.assertEqual(weaponcfg.reference(CUSTOM), weaponcfg.effective(CUSTOM, "pvp"))

    def test_nine_custom_weapons_are_known(self):
        self.assertEqual([1920001, 1920002, 1920003, 2920001, 2920002, 2920003,
                          3920001, 3920002, 3920003], weaponcfg.custom_item_ids())
        self.assertTrue(weaponcfg.is_custom(CUSTOM))
        self.assertFalse(weaponcfg.is_custom(ORIGINAL))


class ValidateTests(_Case):

    def test_original_weapons_cannot_have_numbers(self):
        with self.assertRaises(shopcfg.ConfigError):
            weaponcfg.validate({"custom": {str(ORIGINAL): {"pvp": {"damage": 1}}}})
        with self.assertRaises(shopcfg.ConfigError):
            weaponcfg.save_item(ORIGINAL, params={"pve": {}, "pvp": {"damage": 1}})

    def test_unknown_keys_are_dropped_and_ranges_enforced(self):
        table = weaponcfg.validate({"custom": {str(CUSTOM): {"pvp": {"damage": 5, "nope": 1}}}})
        self.assertEqual({"pve": {}, "pvp": {"damage": 5}}, table["custom"][str(CUSTOM)])
        with self.assertRaises(shopcfg.ConfigError):
            weaponcfg.validate({"custom": {str(CUSTOM): {"pvp": {"magazine": 0}}}})
        with self.assertRaises(shopcfg.ConfigError):
            weaponcfg.validate({"custom": {str(CUSTOM): {"pvp": {"damage": "abc"}}}})
        with self.assertRaises(shopcfg.ConfigError):
            weaponcfg.validate({"custom": {str(CUSTOM): {"pvp": {"damage": True}}}})

    def test_desc_rules(self):
        self.assertEqual("a\nb", weaponcfg.validate_desc(" a\r\nb \n"))
        self.assertEqual("x/y", weaponcfg.validate_desc("x|y"))       # `|` 是段分隔符，不许混进去
        with self.assertRaises(shopcfg.ConfigError):
            weaponcfg.validate_desc("1\n2\n3\n4")
        with self.assertRaises(shopcfg.ConfigError):
            weaponcfg.validate_desc("字" * (weaponcfg.DESC_MAX_CHARS + 1))
        with self.assertRaises(shopcfg.ConfigError):
            weaponcfg.validate({"desc": {"10001": "材料不是武器"}})

    def test_missing_modes_are_padded(self):
        table = weaponcfg.validate({"custom": {str(CUSTOM): {}}})
        self.assertEqual({"pve": {}, "pvp": {}}, table["custom"][str(CUSTOM)])


class EffectiveTests(_Case):

    def test_reference_comes_from_the_resource_pack(self):
        ref = weaponcfg.reference(CUSTOM)
        # 抄的是爆裂 3（`[ch00-01D3]`：Damage 6 / HeadDamage 7 / CoolingTime 260 / Velocity 135）
        self.assertEqual(6, ref["damage"])
        self.assertEqual(7, ref["head_damage"])
        self.assertEqual(260, ref["cooling_ms"])
        self.assertEqual(135.0, ref["velocity"])
        self.assertNotIn("splash_damage", ref)           # 左轮没有溅射，键就不在
        self.assertIn("max_velocity", weaponcfg.reference(CUSTOM_GRENADE))

    def test_effective_is_override_over_reference_per_mode(self):
        weaponcfg.save_item(CUSTOM, params={"pve": {"damage": 50}, "pvp": {"velocity": 10}})
        pve = weaponcfg.effective(CUSTOM, "pve")
        pvp = weaponcfg.effective(CUSTOM, "pvp")
        self.assertEqual(50, pve["damage"])
        self.assertEqual(135.0, pve["velocity"])
        self.assertEqual(6, pvp["damage"])
        self.assertEqual(10.0, pvp["velocity"])

    def test_save_bumps_the_serial_and_is_idempotent_on_reload(self):
        first = weaponcfg.save_item(CUSTOM, params={"pve": {}, "pvp": {"damage": 9}})
        second = weaponcfg.save_item(CUSTOM, desc="说明")
        self.assertEqual(first["serial"] + 1, second["serial"])
        self.assertEqual({"pve": {}, "pvp": {"damage": 9}}, weaponcfg.load()["custom"][str(CUSTOM)])
        self.assertEqual("说明", weaponcfg.desc_of(CUSTOM))
        weaponcfg.save_item(CUSTOM, desc="")
        self.assertEqual("", weaponcfg.desc_of(CUSTOM))

    def test_clearing_a_field_returns_to_the_reference(self):
        weaponcfg.save_item(CUSTOM, params={"pve": {}, "pvp": {"damage": 9}})
        weaponcfg.save_item(CUSTOM, params={"pve": {}, "pvp": {}})
        self.assertEqual(6, weaponcfg.effective(CUSTOM, "pvp")["damage"])


class DescriptionTests(_Case):

    def test_custom_weapon_tooltip_shows_pvp_and_the_note(self):
        weaponcfg.save_item(CUSTOM, params={"pve": {"damage": 50}, "pvp": {"damage": 9}})
        text = shopcfg.item_desc_zh(shopdata.get(CUSTOM))
        self.assertTrue(text.startswith(weaponcfg.PVP_ONLY_NOTE + "\n"))
        self.assertIn("伤害 9", text)
        self.assertNotIn("伤害 50", text)
        # 第 1 段（含提示行）不许超过客户端画得下的 5 行
        self.assertLessEqual(len(text.split(shopcfg.DESC_SEPARATOR)[0].split("\n")),
                             shopcfg.ITEM_DESC_MAX_LINES)

    def test_original_weapon_tooltip_is_untouched_until_a_desc_is_set(self):
        before = shopcfg.item_desc_zh(shopdata.get(ORIGINAL))
        self.assertNotIn(weaponcfg.PVP_ONLY_NOTE, before)
        self.assertNotIn(shopcfg.DESC_SEPARATOR, before)
        weaponcfg.save_item(ORIGINAL, desc="第一行\n第二行")
        after = shopcfg.item_desc_zh(shopdata.get(ORIGINAL))
        self.assertEqual(before + shopcfg.DESC_SEPARATOR + "第一行\n第二行", after)

    def test_admin_desc_lists_both_modes(self):
        """管理页浮窗 / 弹窗两套都列（用户 2026-09-19）；游戏内那段只有 PVP。"""
        weaponcfg.save_item(CUSTOM, params={"pve": {"damage": 50}, "pvp": {"damage": 9}}, desc="说明")
        text = weaponcfg.admin_desc(shopdata.get(CUSTOM))
        self.assertIn("【对战模式 PVP】", text)
        self.assertIn("【任务模式 PVE】", text)
        self.assertIn("伤害 9", text)
        self.assertIn("伤害 50", text)
        self.assertTrue(text.endswith(shopcfg.DESC_SEPARATOR + "说明"))
        self.assertNotIn(weaponcfg.PVP_ONLY_NOTE, text)
        # 原版武器和游戏里一样
        self.assertEqual(shopcfg.item_desc_zh(shopdata.get(ORIGINAL)),
                         weaponcfg.admin_desc(shopdata.get(ORIGINAL)))
        view = weaponcfg.admin_view(CUSTOM)
        self.assertIn("伤害 50", "\n".join(view["lines"]["pve"]))
        self.assertIn("伤害 9", "\n".join(view["lines"]["pvp"]))

    def test_names(self):
        self.assertEqual("左轮 自定义", shopcfg.item_name_zh(shopdata.get(CUSTOM)))
        import shopdefaults
        self.assertEqual("左轮手枪 自定义", shopdefaults.name_of(shopdata.get(CUSTOM)))
        items = shopcfg.validate_items(shopcfg.default_items())
        self.assertEqual("左轮手枪 自定义", items[CUSTOM]["name"])
        self.assertEqual(0, items[CUSTOM]["character"])
        # ★ 出厂不上架（用户 2026-09-19）
        self.assertNotIn(CUSTOM, shopcfg.validate_shop(shopcfg.default_shop()))


class HookFrameTests(_Case):

    def test_layout(self):
        self.assertEqual(12, len(weaponcfg.FIELDS))
        self.assertEqual(108, weaponcfg.RECORD_SIZE)
        payload = weaponcfg.build_hook_frame()
        fmt, serial, count = struct.unpack_from("<HIH", payload, 0)
        self.assertEqual((weaponcfg.WIRE_FORMAT, 0, 9), (fmt, serial, count))
        self.assertEqual(8 + 9 * 108, len(payload))

    def test_round_trip_and_masks(self):
        weaponcfg.save_item(CUSTOM, params={"pve": {"damage": 50}, "pvp": {"velocity": 10}})
        payload = weaponcfg.build_hook_frame()
        fmt, serial, records = weaponcfg.parse_hook_frame(payload)
        self.assertEqual(1, serial)
        by_id = dict(records)
        # ★ 帧里的键是**武器 Id**，不是物品 id；从 `shop_items.json` 取，别写死
        #   —— 武器 Id 2026-09-19 改过一次编号（§41 / D31），写死就会烂。
        rec = by_id[shopdata.get(CUSTOM).ammo_id]
        self.assertEqual(50, rec["pve"]["damage"])
        self.assertEqual(6, rec["pvp"]["damage"])
        self.assertEqual(10.0, rec["pvp"]["velocity"])
        self.assertNotIn("splash_damage", rec["pvp"])        # 参考值里没有的格 mask 位为 0
        self.assertIn("splash_damage", by_id[shopdata.get(CUSTOM_GRENADE).ammo_id]["pvp"])

    def test_field_order_matches_the_hook(self):
        """★ hook 侧 `WTAB_FIELD[]` 的顺序 / 类型照这张表写，两边对不上就是写错格。"""
        self.assertEqual(("damage", "head_damage", "legs_damage", "splash_damage", "splash_range",
                          "magazine", "cooling_ms", "reload_ms", "loading_ms",
                          "velocity", "max_velocity", "gravity"), weaponcfg.FIELD_KEYS)
        self.assertEqual([int] * 9 + [float] * 3, [f[4] for f in weaponcfg.FIELDS])


if __name__ == "__main__":
    unittest.main()
