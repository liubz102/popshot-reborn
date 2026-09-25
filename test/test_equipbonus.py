#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`server/equipbonus.py` 的测试 —— 客户端局内 `GetEquipBonus` 的服务端复刻（X_Mod §91）。

服务端替 bot 算伤害时，要按**受害者**身上的装备算防御 15% 和满血。算错了的
后果都在真人身上：防御少算 = bot 打人比真人打人疼；满血少算 = [幸运幸存者]
把没残血的人也当成残血，白白替他挡掉一发。所以这里把三件事钉死：

1. **分桶**：通用桶 + 本角色的桶才算（V0.3商店 §1 / §16）；
2. **两种 Lua**：`560002` / `560003` 的对战模式条件（含 `!=`）、`560001` 的满血 −10%；
3. **真表里的数**：V0.3商店 §16 那一身五件铠甲实测是 `Hp=29 / Defense=3`。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import equipbonus                                              # noqa: E402
import shopdata                                                # noqa: E402

#: V0.3商店 §16 实机那一身（泰尔）：五件铠甲，客户端内存里读出来 Hp=29、Defense=3。
TYR_ARMOR_SET = (1010037, 1020062, 1030058, 1040037, 1050037)
#: 对战房的描述符参数：(组队, 游戏模式, 道具模式)。
SURVIVAL = (0, 0, 0)
FIGHTING = (0, 2, 0)


class BucketTests(unittest.TestCase):
    """`key = itemId / 1e6 − 1`，`itemId < 1e6` 是 −1 通用桶（`0x414e43`）。"""

    def test_titles_and_pets_land_in_the_common_bucket(self):
        self.assertEqual(equipbonus.COMMON_BUCKET, equipbonus.bucket_of(560004))
        self.assertEqual(equipbonus.COMMON_BUCKET, equipbonus.bucket_of(220002))

    def test_gear_lands_in_its_characters_bucket(self):
        self.assertEqual(0, equipbonus.bucket_of(1010037))       # 泰尔
        self.assertEqual(1, equipbonus.bucket_of(2010001))       # 卡希尔
        self.assertEqual(2, equipbonus.bucket_of(3010001))       # 布洛克

    def test_only_the_common_and_own_bucket_count(self):
        """★ 泰尔的铠甲穿在身上、换卡希尔上场 —— 客户端不算它。"""
        self.assertTrue(equipbonus.counts_for(1010037, 0))
        self.assertFalse(equipbonus.counts_for(1010037, 1))
        self.assertTrue(equipbonus.counts_for(560004, 1))
        self.assertTrue(equipbonus.counts_for(560004, 100))      # 商城角色也吃通用桶


class PvpModeTests(unittest.TestCase):
    """`session:GetPvpMode()` = `0x409e0a`。"""

    def test_a_normal_room_reports_its_mode_argument(self):
        self.assertEqual(3, equipbonus.pvp_mode(1, (0, 3, 0)))
        self.assertEqual(0, equipbonus.pvp_mode(1, SURVIVAL))

    def test_ladder_is_always_five(self):
        self.assertEqual(5, equipbonus.pvp_mode(5, (9, 9, 9)))

    def test_anything_else_is_minus_one(self):
        self.assertEqual(-1, equipbonus.pvp_mode(2, (3, 1)))     # 闯关
        self.assertEqual(-1, equipbonus.pvp_mode(1, ()))         # 读不出来


class LuaTests(unittest.TestCase):
    """中文表里受害者一侧用得到的两种脚本（V0.3商店 §53④）。"""

    FIGHTER = ("if session:GetGameType() == 1 and session:GetPvpMode() == 2 "
               "then return 2 else return 0 end")
    SHOOTER = ("if session:GetGameType() == 1 and session:GetPvpMode() != 2 "
               "then return 2 else return 0 end")
    PERFECT = "return - GetDefaultMaxHp(mychr:GetChrIdx()) * 0.1"

    def test_the_mode_condition_needs_both_halves(self):
        self.assertEqual(2, equipbonus.lua_value(self.FIGHTER, 1, FIGHTING, 100))
        self.assertEqual(0, equipbonus.lua_value(self.FIGHTER, 1, SURVIVAL, 100))
        # 闯关房：GetGameType() 不是 1，前半句就不成立。
        self.assertEqual(0, equipbonus.lua_value(self.FIGHTER, 2, (3, 1), 100))

    def test_not_equal_is_the_other_half(self):
        """★ `!=` 不是标准 Lua 5.0，但这份词法器认它（`0x5b3dce`：`!` 和 `~` 同一个 case）。"""
        self.assertEqual(2, equipbonus.lua_value(self.SHOOTER, 1, SURVIVAL, 100))
        self.assertEqual(0, equipbonus.lua_value(self.SHOOTER, 1, FIGHTING, 100))
        tilde = self.SHOOTER.replace("!=", "~=")
        self.assertEqual(2, equipbonus.lua_value(tilde, 1, SURVIVAL, 100))

    def test_the_max_hp_share_truncates_toward_zero(self):
        """`pop<int>` 走 `_ftol`：−9.000…002 → −9，不是 −10。"""
        self.assertEqual(-10, equipbonus.lua_value(self.PERFECT, 1, SURVIVAL, 100))
        self.assertEqual(-9, equipbonus.lua_value(self.PERFECT, 1, SURVIVAL, 90))
        self.assertEqual(-13, equipbonus.lua_value(self.PERFECT, 1, SURVIVAL, 130))

    def test_an_unknown_script_counts_as_nothing(self):
        """武器称号那 9 格是射手一侧的事，受害者这边按 0。"""
        code = ("if mychr:GetLastBulletROHIdx() == 110001 "
                "then return 15 else return 0 end")
        self.assertEqual(0, equipbonus.lua_value(code, 1, SURVIVAL, 100))


class RealTableTests(unittest.TestCase):
    """真实物品表（`server/shop_items.json`）上的数。"""

    def setUp(self):
        if shopdata.get(1010037) is None:
            self.skipTest("没有 shop_items.json")

    def test_the_measured_armor_set(self):
        """★ V0.3商店 §16 实机读出来的：五件 Hp 8+6+3+7+5 = 29、Defense 3。"""
        self.assertEqual(29, equipbonus.seat_bonus(TYR_ARMOR_SET, 0, equipbonus.HP))
        self.assertEqual(3, equipbonus.seat_bonus(TYR_ARMOR_SET, 0,
                                                  equipbonus.DEFENSE))

    def test_the_same_set_does_nothing_for_another_character(self):
        self.assertEqual(0, equipbonus.seat_bonus(TYR_ARMOR_SET, 1, equipbonus.HP))

    def test_a_pet_counts_for_everybody(self):
        self.assertEqual(2, equipbonus.seat_bonus((220002,), 1, equipbonus.DEFENSE))

    def test_the_conditional_titles(self):
        # [射击达人] 在普通对战里（模式不是 2）防御 +2，闯关里 0。
        self.assertEqual(2, equipbonus.seat_bonus(
            (560003,), 0, equipbonus.DEFENSE, 1, SURVIVAL, 100))
        self.assertEqual(0, equipbonus.seat_bonus(
            (560003,), 0, equipbonus.DEFENSE, 2, (3, 1), 100))
        # [完美胜利者]：生命 −10%（按这个人自己角色的基础满血）。
        self.assertEqual(-9, equipbonus.seat_bonus(
            (560001,), 1, equipbonus.HP, 1, SURVIVAL, 90))

    def test_an_item_the_table_does_not_know_is_ignored(self):
        self.assertEqual(0, equipbonus.seat_bonus((999999,), 0, equipbonus.DEFENSE))


if __name__ == "__main__":
    unittest.main()
