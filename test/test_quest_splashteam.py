#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闯关叠加表 `Data/Quest/weapon-international.ini` 的 `SplashTeam` 守卫（X_Mod §64 / §89）。

任务模式「不炸队友、不自炸」全靠这份表给武器记录叠上 `SplashTeam=1`（主表一条都没填）。
中文版读的是 international 那份，**不是**韩版 `Quest/weapon.ini`（`0x4a375c` 二选一）。

★★ 子弹药要**单独**填：火墙（`CreatingClass=Flame`）在收方 `0x48254a` 按 `SliceId` 重新查表，
看的是**火墙那一节自己的** `SplashTeam`（往两边铺的子火苗 `0x482728` 也是调它）——
母弹那一节填了只管爆炸那一下。2026-09-24 用户实机：卡希尔自定义 1 / 2 的燃烧弹
在闯关里照样烧自己，就是 `[ch01-02Ca]` / `[ch01-02Pa]` 漏了这一格。

⚠ 依赖明文资源树 `game_patched/Pack_develop`，发布包里没有，自动跳过。
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CW_DIR = os.path.join(ROOT, "tools", "custom-weapon")


def _load(name):
    spec = importlib.util.spec_from_file_location("test_" + name, os.path.join(CW_DIR, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _team_ids(sections):
    """表里 `SplashTeam` 非 0 的武器 Id。"""
    return {fields["Id"] for fields in sections.values()
            if "Id" in fields and fields.get("SplashTeam", "0") not in ("", "0")}


class QuestSplashTeamTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.spec = _load("spec")
        root = cls.spec.develop_root()
        main = os.path.join(root, "Data", "weapon.ini")
        if not os.path.isfile(main):
            raise unittest.SkipTest("没有明文资源树，跳过")
        cls.main = cls.spec.read_weapon_ini(main)
        cls.korean = cls.spec.read_weapon_ini(os.path.join(root, "Data", "Quest", "weapon.ini"))
        cls.intl = cls.spec.read_weapon_ini(os.path.join(root, "Data", "Quest", "weapon-international.ini"))
        cls.intl_team = _team_ids(cls.intl)
        cls.name_of = {fields["Id"]: name for name, fields in cls.main.items() if "Id" in fields}

    def test_every_section_names_the_same_weapon_as_the_main_table(self):
        """节名和 `Id` 指的是同一件武器 —— 下面几条按 Id 查，这一条保证按节名看也是同一个结论。"""
        for name, fields in self.intl.items():
            self.assertIn(name, self.main, "叠加表里的 [%s] 主表里没有" % name)
            self.assertEqual(self.main[name].get("Id"), fields.get("Id"),
                             "[%s] 的 Id 和主表同名那一节对不上" % name)

    def test_every_slice_of_an_exempt_weapon_is_exempt_too(self):
        """★★ 母弹豁免了，它的子弹药（火墙 / 碎片）也必须豁免，否则那一段照样打自己和队友。"""
        missing = []
        for name, fields in self.main.items():
            slice_id = fields.get("SliceId")
            if not slice_id or fields.get("Id") not in self.intl_team:
                continue
            if slice_id not in self.intl_team:
                missing.append("[%s] 的子弹药 [%s] Id=%s"
                               % (name, self.name_of.get(slice_id, "?"), slice_id))
        self.assertEqual([], missing)

    def test_international_keeps_every_korean_exemption(self):
        """§64 那 29 节照韩版补的，一条都不许丢。"""
        lost = sorted(_team_ids(self.korean) - self.intl_team)
        self.assertEqual([], ["[%s] Id=%s" % (self.name_of.get(i, "?"), i) for i in lost])

    def test_custom_weapons_follow_their_mother(self):
        """自定义武器（含子弹药）豁免与否和母本一致 —— 以后加第三批就靠这条提醒去补表。
        泰尔 3 号那种母本本来就不豁免的（原版设计，§64），自定义的也不该豁免。"""
        diff = []
        for w in self.spec.WEAPONS:
            pairs = [(w.section, w.custom_section)]
            if w.piece_section:
                pairs.append((w.piece_section, w.custom_piece_section))
            for mother, custom in pairs:
                want = self.main[mother]["Id"] in self.intl_team
                have = self.main[custom]["Id"] in self.intl_team
                if want != have:
                    diff.append("[%s] 母本 [%s] %s，它 %s"
                                % (custom, mother, "豁免" if want else "不豁免", "豁免" if have else "不豁免"))
        self.assertEqual([], diff)


if __name__ == "__main__":
    unittest.main()
