#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
import struct
import tempfile
import unittest

from account_store import (EXPERIENCE_PER_LEVEL, AccountStore, player_character,
                           player_level, tutorial_state)
from gameserver import build_gsp_rep_login


class AccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "accounts.json")
        self.store = AccountStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_login_creates_editable_account(self):
        account = self.store.login("alice", "pw")
        self.assertFalse(account["tutorial_completed"])
        self.assertEqual(("alice", account), self.store.get_account())
        with open(self.path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual("alice", saved["active_account"])
        self.assertEqual("pw", saved["accounts"]["alice"]["password"])

    def test_tutorial_flag_persists(self):
        self.store.login("alice", "pw")
        self.store.set_tutorial_completed("alice", True)
        name, account = self.store.get_account()
        self.assertEqual("alice", name)
        self.assertTrue(account["tutorial_completed"])
        self.assertEqual(3, tutorial_state(account))

    def test_login_packet_encodes_confirmed_level_and_tutorial_state(self):
        payload = build_gsp_rep_login(
            account={"level": 7, "experience": 650,
                     "tutorial_completed": True},
            channel_code=7, channel_index=2)
        # result(4) + 两个空字符串(u16 + u16) 后是 8 个 int32。
        values = struct.unpack_from("<8i", payload, 8)
        self.assertEqual(7, values[0])       # 等级 -> 0x72e338
        self.assertEqual(7, values[1])       # 频道码 -> [conn+0x89c]
        self.assertEqual(2, values[2])       # 频道序号 -> [conn+0x8a0]
        self.assertEqual(650, values[3])     # 总经验 -> 0x72e33c
        self.assertEqual(600, values[4])     # 本级起点 -> 0x72e340
        self.assertEqual(700, values[5])     # 下一级所需 -> 0x72e344
        self.assertEqual(3, values[6])       # 教程状态
        # 只剩 +0x2c 语义未查，按 D019 保持 0。
        self.assertEqual(0, values[7])
        self.assertEqual(48, len(payload))

    def test_login_packet_experience_matches_the_end_game_encoding(self):
        # 三个都必须是绝对累计值，和 gspEndGame 用的是同一组全局（§94/§95）。
        account = {"level": 1, "experience": 0, "tutorial_completed": False}
        values = struct.unpack_from("<8i", build_gsp_rep_login(account=account), 8)
        self.assertEqual((0, 0, EXPERIENCE_PER_LEVEL), values[3:6])
        self.assertEqual(0, values[6])      # 未通过教程 -> 客户端强制走教学

    def test_tutorial_progress_report_marks_the_account_completed(self):
        # 客户端跑完教学后用 0x030f 上报 4 或 5，服务端据此落盘（§95）。
        self.store.login("alice", "pw")
        account = self.store.set_tutorial_progress("alice", 5)
        self.assertTrue(account["tutorial_completed"])
        self.assertEqual(5, account["tutorial_progress"])
        _, reloaded = self.store.get_account("alice")
        self.assertTrue(reloaded["tutorial_completed"])
        # 下次登录把客户端自己报过的原始值原样发回去。
        self.assertEqual(5, tutorial_state(reloaded))

    def test_tutorial_progress_below_the_threshold_does_not_complete(self):
        self.store.login("alice", "pw")
        account = self.store.set_tutorial_progress("alice", 1)
        self.assertFalse(account["tutorial_completed"])
        self.assertEqual(1, account["tutorial_progress"])
        self.assertEqual(0, tutorial_state(account))

    def test_manually_clearing_the_flag_forces_the_tutorial_again(self):
        # tutorial_completed 是给人编辑的开关，必须压过残留的进度值。
        self.store.login("alice", "pw")
        self.store.set_tutorial_progress("alice", 5)
        self.store.set_tutorial_completed("alice", False)
        _, account = self.store.get_account("alice")
        self.assertEqual(0, tutorial_state(account))

    def test_tutorial_progress_rejects_unknown_account(self):
        with self.assertRaises(KeyError):
            self.store.set_tutorial_progress("nobody", 5)

    def test_login_realigns_a_hand_edited_level_with_the_experience(self):
        self.store.login("alice", "pw")
        self.store.add_quest_reward("alice",
                                    experience=EXPERIENCE_PER_LEVEL * 3)
        # 手工把等级改乱，登录时应该按经验校回来。
        with open(self.path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        saved["accounts"]["alice"]["level"] = 99
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(saved, f)
        self.assertEqual(4, self.store.login("alice", "pw")["level"])

    def test_new_account_default_level_unlocks_the_quest_list(self):
        # 闯关关卡记录的要求等级是 1；等级 0 会让任务下拉框整个空掉。
        account = self.store.login("alice", "pw")
        self.assertGreaterEqual(player_level(account), 1)

    def test_player_level_tolerates_missing_or_bad_values(self):
        self.assertEqual(0, player_level(None))
        self.assertEqual(0, player_level({}))
        self.assertEqual(0, player_level({"level": "abc"}))
        self.assertEqual(0, player_level({"level": -5}))
        self.assertEqual(6, player_level({"level": 6}))

    def test_quest_reward_accumulates_and_persists(self):
        # 结算所得必须落盘，否则玩家一退出就退回原点（D024）。
        self.store.login("alice", "pw")
        self.store.add_quest_reward("alice", experience=30, money=30)
        account = self.store.add_quest_reward("alice", experience=12, money=5)
        self.assertEqual(42, account["experience"])
        self.assertEqual(35, account["money"])
        _, reloaded = self.store.get_account("alice")
        self.assertEqual(42, reloaded["experience"])
        self.assertEqual(35, reloaded["money"])

    def test_quest_reward_raises_the_level_across_the_curve(self):
        self.store.login("alice", "pw")
        account = self.store.add_quest_reward("alice",
                                              experience=EXPERIENCE_PER_LEVEL)
        self.assertEqual(2, account["level"])
        self.assertEqual(2, player_level(account))

    def test_quest_reward_rejects_unknown_account(self):
        with self.assertRaises(KeyError):
            self.store.add_quest_reward("nobody", experience=1)

    def test_selected_character_persists(self):
        # 换角色是客户端报、服务端广播回去才生效的，存下来重登才不会跳回 0。
        self.store.login("alice", "pw")
        self.assertEqual(0, player_character(self.store.login("alice", "pw")))
        account = self.store.set_character("alice", 2)
        self.assertEqual(2, player_character(account))
        _, reloaded = self.store.get_account("alice")
        self.assertEqual(2, player_character(reloaded))

    def test_selected_character_rejects_unknown_account(self):
        with self.assertRaises(KeyError):
            self.store.set_character("nobody", 1)


if __name__ == "__main__":
    unittest.main()
