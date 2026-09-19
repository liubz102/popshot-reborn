#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`server/weaponcfg.py` —— 自定义武器数值 + 全武器说明文（X_Mod · X3）。

三件事要钉住：
1. 它是第七份运营配置（`shopcfg._SPECS`）：开服生成、热重载、坏文件退回出厂值、备份回滚同组；
2. 数值只认 9 把自定义武器，原版武器只能改说明文；有效值 = 覆盖 ∪ 参考值；
3. `0x0F01` 的载荷能编能解，两套模式各自带自己的 mask，字段顺序 / 类型和 hook 侧一致。
"""
import os
import re
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

CUSTOM = 1920001          # 第一批（C · 爆裂 3 母本）泰尔 1 号「左轮手枪 自定义1」
CUSTOM2 = 1930001         # 第二批（P · 复合 3 母本）泰尔 1 号「左轮手枪 自定义2」
CUSTOM_GRENADE = 1920002  # 泰尔 2 号（有溅射 / 最大初速）
ORIGINAL = 1120011        # 左轮 爆裂1（原版）

_SPEC = []


def _goldwp_spec():
    """`tools/goldwp/spec.py` —— 自定义武器编号的唯一源头（只依赖标准库）。"""
    if not _SPEC:
        import importlib.util
        path = os.path.join(ROOT, "tools", "goldwp", "spec.py")
        s = importlib.util.spec_from_file_location("test_wc_goldwp_spec", path)
        module = importlib.util.module_from_spec(s)
        s.loader.exec_module(module)
        _SPEC.append(module)
    return _SPEC[0]


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

    def test_every_custom_weapon_is_known(self):
        """两批自定义武器（X3 的 `C` · X6 的 `P`）全部认得出来。

        ★ 期望值**从 `tools/goldwp/spec.py` 现取**：那儿是编号的唯一源头，
          加第三批时这条自动跟着走，不用改常量。
        """
        self.assertEqual(sorted(w.item_id for w in _goldwp_spec().WEAPONS),
                         weaponcfg.custom_item_ids())
        self.assertEqual(18, len(weaponcfg.custom_item_ids()))   # 两批 × 9
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


class RangeTests(_Case):
    """管理页能填的范围（2026-09-19 收紧）vs 读盘放行的历史范围。

    两句话：**新填的必须合理，旧的照样读得出来。**
    后者是铁律 11 —— `shopcfg._load()` 对校验不过的文件是「整份退回出厂值」，
    要是读盘也按新范围拦，一格存量的离谱数值就能让 GM 配过的全部数值一起消失。
    """

    def test_the_admin_ranges_are_the_tightened_ones(self):
        """钉住用户 2026-09-19 拍板的那几个数（弹匣 1~100 是原话）。"""
        limits = {f[0]: (f[5], f[6]) for f in weaponcfg.FIELDS}
        self.assertEqual((1, 100), limits["magazine"])
        self.assertEqual((0, 500), limits["damage"])
        self.assertEqual((0, 500), limits["head_damage"])
        self.assertEqual((0, 500), limits["legs_damage"])
        self.assertEqual((0, 500), limits["splash_damage"])
        self.assertEqual((0, 1000), limits["splash_range"])
        self.assertEqual((1, 10000), limits["cooling_ms"])
        self.assertEqual((0, 10000), limits["reload_ms"])
        self.assertEqual((0, 10000), limits["loading_ms"])
        self.assertEqual((0.0, 1000.0), limits["velocity"])
        self.assertEqual((0.0, 1000.0), limits["max_velocity"])
        self.assertEqual((-20.0, 20.0), limits["gravity"])

    def test_every_field_has_a_legacy_range_that_is_not_narrower(self):
        """读盘的范围必须**包住**管理页的范围，否则存不进去的值反而读得出来，反了。"""
        self.assertEqual(set(weaponcfg.FIELD_KEYS), set(weaponcfg.LEGACY_LIMITS))
        for key, _label, _unit, _src, _cast, low, high in weaponcfg.FIELDS:
            llo, lhi = weaponcfg.LEGACY_LIMITS[key]
            self.assertLessEqual(llo, low, "%s 的读盘下限比管理页还高" % key)
            self.assertGreaterEqual(lhi, high, "%s 的读盘上限比管理页还低" % key)

    def test_the_admin_range_really_covers_every_weapon_in_the_pack(self):
        """★ 范围是照原版 `weapon.ini` 的分布定的 —— 反过来，资源包里**每一把**枪
        的每一格都必须填得进管理页，否则「照着原版数值抄一份」这件事都做不到。"""
        import weapondata
        weapons = weapondata.STORE.table().get("weapons") or {}
        if not weapons:
            raise unittest.SkipTest("bot_weapons.json 里一把枪都没有")
        checked = 0
        for ammo_id in weapons:
            weapon = weapondata.get(int(ammo_id))
            if weapon is None:
                continue
            for key, label, _unit, src, cast, low, high in weaponcfg.FIELDS:
                raw = weapon.raw.get(src)
                if raw is None:
                    continue
                value = cast(raw)
                self.assertLessEqual(value, high,
                                     "资源包里 %s 的 %s=%s 比管理页上限 %s 还大"
                                     % (ammo_id, label, value, high))
                # ★ 下限只核对到 `cooling_ms` 为止：原版有 13 节 `CoolingTime=0`
                #   （爱琳的种子炸弹那类「不靠连射节奏」的枪，加上 8 个怪物的碰撞
                #   伤害），但管理页**一直**只让填 ≥1 —— 那是防「射速间隔 0 =
                #   每帧一发」的有意保护，不是这次收紧范围带来的，别顺手放开。
                if key != "cooling_ms":
                    self.assertGreaterEqual(value, low,
                                            "资源包里 %s 的 %s=%s 比管理页下限 %s 还小"
                                            % (ammo_id, label, value, low))
                checked += 1
        self.assertGreater(checked, 100, "只核对了 %d 格，这条守卫等于没跑" % checked)

    def test_the_admin_page_refuses_values_outside_the_new_range(self):
        for bad in ({"magazine": 101}, {"magazine": 0}, {"damage": 501},
                    {"velocity": 1000.1}, {"gravity": -20.1}, {"cooling_ms": 10001}):
            with self.assertRaises(shopcfg.ConfigError, msg="%r 该被拒" % bad):
                weaponcfg.save_item(CUSTOM, params={"pve": {}, "pvp": bad}, data_dir=self.dir)

    def test_the_admin_page_accepts_the_new_upper_bounds(self):
        weaponcfg.save_item(CUSTOM, params={"pve": {}, "pvp": {"magazine": 100, "damage": 500}},
                            data_dir=self.dir)
        self.assertEqual(100, weaponcfg.overrides_of(CUSTOM, "pvp", data_dir=self.dir)["magazine"])

    def test_a_stored_legacy_value_survives_a_reload(self):
        """★ 收紧范围之前存下的值不许因此丢失 —— 整份表也不许被拖下水。"""
        legacy = {"custom": {str(CUSTOM): {"pve": {}, "pvp": {"magazine": 500, "damage": 20}}},
                  "desc": {}, "serial": 3}
        table = weaponcfg.validate(legacy)           # 读盘这条路
        self.assertEqual(500, table["custom"][str(CUSTOM)]["pvp"]["magazine"])
        self.assertEqual(20, table["custom"][str(CUSTOM)]["pvp"]["damage"])

    def test_values_beyond_even_the_legacy_range_are_still_rejected(self):
        with self.assertRaises(shopcfg.ConfigError):
            weaponcfg.validate({"custom": {str(CUSTOM): {"pvp": {"magazine": 1000}}}})
        with self.assertRaises(shopcfg.ConfigError):
            weaponcfg.validate({"custom": {str(CUSTOM): {"pvp": {"magazine": 0}}}})

    def test_saving_one_weapon_does_not_trip_over_another_ones_legacy_value(self):
        """GM 改 A 的时候，B 那格存量的离谱值不能让保存整个失败。"""
        path = shopcfg.path_of(weaponcfg.FILENAME, self.dir)
        shopcfg.write_json(path, {"format": weaponcfg.FORMAT, "serial": 1, "desc": {},
                                  "custom": {str(CUSTOM_GRENADE): {"pve": {},
                                                                   "pvp": {"damage": 9999}}}})
        shopcfg.invalidate(self.dir)
        weaponcfg.save_item(CUSTOM, params={"pve": {}, "pvp": {"damage": 42}}, data_dir=self.dir)
        self.assertEqual(42, weaponcfg.overrides_of(CUSTOM, "pvp", data_dir=self.dir)["damage"])
        self.assertEqual(9999,
                         weaponcfg.overrides_of(CUSTOM_GRENADE, "pvp", data_dir=self.dir)["damage"])

    def test_the_admin_view_hands_the_tightened_range_to_the_page(self):
        """弹窗里 input 的 min/max 就是 `FIELDS` 那套（`admin.js` 直接往 input 上写）。"""
        view = weaponcfg.admin_view(CUSTOM, data_dir=self.dir)
        row = {f["key"]: f for f in view["fields"]}
        self.assertEqual((1, 100), (row["magazine"]["min"], row["magazine"]["max"]))
        self.assertEqual((0, 500), (row["damage"]["min"], row["damage"]["max"]))


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

    def test_admin_desc_puts_pve_first_like_everywhere_else(self):
        """★ 顺序：**PVE 在前**（用户 2026-09-19 第三轮）。

        弹窗里两栏是「PVE 在左、PVP 在右」，浮窗和预览区却反着来 —— 一处左右、
        一处上下，看着别扭。三处统一跟 `weaponcfg.MODES` 走。
        """
        self.assertEqual((weaponcfg.MODE_PVE, weaponcfg.MODE_PVP), weaponcfg.MODES)
        weaponcfg.save_item(CUSTOM, params={"pve": {"damage": 50}, "pvp": {"damage": 9}},
                            data_dir=self.dir)
        text = weaponcfg.admin_desc(shopdata.get(CUSTOM), data_dir=self.dir)
        self.assertLess(text.index("【任务模式 PVE】"), text.index("【对战模式 PVP】"),
                        "浮窗里 PVP 排到了 PVE 前面")
        # 弹窗那边也按同一张表发顺序（`admin.js` 的预览区照 `view.modes` 画）
        view = weaponcfg.admin_view(CUSTOM, data_dir=self.dir)
        self.assertEqual(["pve", "pvp"], [m["key"] for m in view["modes"]])

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
        """两批的默认名按韩文名后缀字母分开：`C` -> 自定义1、`P` -> 自定义2（用户 2026-09-19）。"""
        import shopdefaults
        self.assertEqual("左轮 自定义1", shopcfg.item_name_zh(shopdata.get(CUSTOM)))
        self.assertEqual("左轮手枪 自定义1", shopdefaults.name_of(shopdata.get(CUSTOM)))
        self.assertEqual("左轮手枪 自定义2", shopdefaults.name_of(shopdata.get(CUSTOM2)))
        items = shopcfg.validate_items(shopcfg.default_items())
        self.assertEqual("左轮手枪 自定义1", items[CUSTOM]["name"])
        self.assertEqual("左轮手枪 自定义2", items[CUSTOM2]["name"])
        self.assertEqual(0, items[CUSTOM]["character"])
        # ★ 出厂不上架（用户 2026-09-19），两批都是
        shop = shopcfg.validate_shop(shopcfg.default_shop())
        self.assertNotIn(CUSTOM, shop)
        self.assertNotIn(CUSTOM2, shop)

    def test_every_batch_has_a_chinese_word(self):
        """★ 后缀字母 -> 中文词的表必须覆盖 `spec.BATCHES` 的每一批 —— 漏一批，
        那 9 把的名字会静默退回韩文（`weapon_name_zh` 的兜底分支）。"""
        for letter in _goldwp_spec().BATCHES:
            self.assertIn(letter, shopcfg.CUSTOM_WEAPON_ZH_BY_SUFFIX, letter)
        self.assertEqual(len(_goldwp_spec().BATCHES),
                         len(set(shopcfg.CUSTOM_WEAPON_ZH_BY_SUFFIX.values())),
                         "两批不能叫同一个名字")


class HookFrameTests(_Case):

    def test_layout(self):
        self.assertEqual(12, len(weaponcfg.FIELDS))
        self.assertEqual(108, weaponcfg.RECORD_SIZE)
        self.assertEqual(9, weaponcfg.HEADER_SIZE)
        n = len(weaponcfg.custom_item_ids())                 # 两批 = 18 条
        payload = weaponcfg.build_hook_frame()
        fmt, serial, count, mode = struct.unpack_from("<HIHB", payload, 0)
        self.assertEqual((weaponcfg.WIRE_FORMAT, 0, n), (fmt, serial, count))
        self.assertEqual(weaponcfg.HOOK_MODE_NONE, mode)     # 默认不施加
        self.assertEqual(9 + n * 108, len(payload))
        # ★ hook 侧 `WTAB_MAX` 是 32：条数超了整份包会被丢弃（bshook.c 的硬拦截）。
        self.assertLessEqual(n, 32, "条数超过 bshook 的 WTAB_MAX，客户端会静默丢包")

    def test_hook_mode_rides_in_the_header(self):
        """★ 模式那一格：默认 `NONE`（只送数据、下一局生效），开局那一发才带真模式。

        hook 侧 `WTAB_MODE_*` / `WTAB_HEADER_BYTES` 照这张表写，对不上就整份丢弃。
        """
        for wanted in (weaponcfg.HOOK_MODE_PVE, weaponcfg.HOOK_MODE_PVP,
                       weaponcfg.HOOK_MODE_NONE):
            payload = weaponcfg.build_hook_frame(hook_mode=wanted)
            fmt, serial, mode, records = weaponcfg.parse_hook_frame(payload)
            self.assertEqual(2, fmt)                         # 格式 2 = 带模式位
            self.assertEqual(wanted, mode)
            self.assertEqual(len(weaponcfg.custom_item_ids()), len(records))

    def test_round_trip_and_masks(self):
        weaponcfg.save_item(CUSTOM, params={"pve": {"damage": 50}, "pvp": {"velocity": 10}})
        payload = weaponcfg.build_hook_frame()
        fmt, serial, mode, records = weaponcfg.parse_hook_frame(payload)
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


class HookSourceStaysInSyncTests(unittest.TestCase):
    """★★ `0x0F01` 的线格式是**跨语言**的：服务端 `weaponcfg` 和 `hook/bshook.c`
    两边各写一份常量。漂了不会报错，只会整份被客户端**静默丢掉**
    （`WTAB    !! 0x0F01 格式对不上（…），丢弃`），实机才看得出来。
    所以拿源码核一遍 —— 这条守卫的成本是零，漏掉的代价是一轮实机。
    """

    def setUp(self):
        path = os.path.join(ROOT, "hook", "bshook.c")
        if not os.path.isfile(path):          # 发布包里没有 hook 源码
            raise unittest.SkipTest("没有 hook/bshook.c")
        with open(path, "rb") as handle:
            self.text = handle.read().decode("utf-8", "replace")

    def _define(self, name):
        found = re.search(r"^#define\s+%s\s+(0x[0-9A-Fa-f]+|\d+)" % name,
                          self.text, re.M)
        self.assertIsNotNone(found, "bshook.c 里找不到 #define %s" % name)
        return int(found.group(1), 0)

    def test_format_and_header(self):
        self.assertEqual(weaponcfg.WIRE_FORMAT, self._define("WTAB_FORMAT"))
        self.assertEqual(weaponcfg.HEADER_SIZE, self._define("WTAB_HEADER_BYTES"))
        self.assertEqual(len(weaponcfg.FIELDS), self._define("WTAB_FIELDS"))

    def test_mode_codes(self):
        self.assertEqual(weaponcfg.HOOK_MODE_PVE, self._define("WTAB_MODE_PVE"))
        self.assertEqual(weaponcfg.HOOK_MODE_PVP, self._define("WTAB_MODE_PVP"))
        self.assertEqual(weaponcfg.HOOK_MODE_NONE, self._define("WTAB_MODE_NONE"))
        # PVE / PVP 必须和 `MODES` 里 PVE 在前的顺序一致 —— 载荷里两个块就是按这个序排的。
        self.assertEqual((weaponcfg.MODE_PVE, weaponcfg.MODE_PVP), weaponcfg.MODES)
        self.assertEqual(0, weaponcfg.HOOK_MODE_PVE)


if __name__ == "__main__":
    unittest.main()
