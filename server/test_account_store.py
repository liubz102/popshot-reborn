#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
import struct
import tempfile
import unittest

from account_store import (AUTH_BAD_PASSWORD, AUTH_NO_SUCH_USER, AUTH_OK,
                           EXPERIENCE_PER_LEVEL, MINIMUM_PLAYER_LEVEL,
                           NEW_ACCOUNT_DEFAULTS, QUEST_DIFFICULTY_MAX,
                           QUEST_ID_TABLE, AccountError, AccountStore,
                           player_character, player_level,
                           quest_cleared_difficulty, quest_difficulty_records,
                           quest_unlock_all, tutorial_state)
from gameserver import build_gsp_rep_login


class AccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "accounts.json")
        self.store = AccountStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def account(self, username="alice", password="pw"):
        """要一个可用账号。已经注册过就直接取回（很多用例会调好几次）。"""
        if self.store.has_account(username):
            return self.store.get_account(username)[1]
        return self.store.register(username, password)

    def test_register_creates_an_editable_account(self):
        account = self.account()
        self.assertFalse(account["tutorial_completed"])
        self.assertEqual(("alice", account), self.store.get_account("alice"))
        with open(self.path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        # V0.1 的单活动账号已经废除，身份靠票据传（D064）。
        self.assertNotIn("active_account", saved)
        self.assertEqual("pw", saved["accounts"]["alice"]["password"])

    def test_register_can_skip_the_tutorial(self):
        # 注册页那个默认勾着的框走的就是这条路（D094）。
        account = self.store.register("bob", "pw", skip_tutorial=True)
        self.assertTrue(account["tutorial_completed"])
        self.assertEqual(3, tutorial_state(account))
        # 进度值是「客户端上报过什么」的保真记录，不许被我们编一个出来。
        self.assertEqual(0, account["tutorial_progress"])
        self.assertTrue(self.store.get_account("bob")[1]["tutorial_completed"])

    def test_register_without_skipping_keeps_the_tutorial(self):
        account = self.store.register("bob", "pw", skip_tutorial=False)
        self.assertFalse(account["tutorial_completed"])
        self.assertEqual(0, tutorial_state(account))

    def test_tutorial_flag_persists(self):
        self.account()
        self.store.set_tutorial_completed("alice", True)
        name, account = self.store.get_account("alice")
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
        self.account()
        account = self.store.set_tutorial_progress("alice", 5)
        self.assertTrue(account["tutorial_completed"])
        self.assertEqual(5, account["tutorial_progress"])
        _, reloaded = self.store.get_account("alice")
        self.assertTrue(reloaded["tutorial_completed"])
        # 下次登录把客户端自己报过的原始值原样发回去。
        self.assertEqual(5, tutorial_state(reloaded))

    # -- 注册 / 校验（V0.2）--------------------------------------------------
    def test_register_rejects_a_duplicate_username(self):
        self.store.register("alice", "pw")
        with self.assertRaises(AccountError) as ctx:
            self.store.register("alice", "other")
        self.assertEqual("duplicate", ctx.exception.code)
        # 原来的密码不能被覆盖掉。
        self.assertEqual(AUTH_OK, self.store.verify("alice", "pw")[0])

    def test_register_rejects_bad_usernames_and_passwords(self):
        for bad in ("", "a", "a" * 17, "有中文", "with space", "semi;colon"):
            with self.assertRaises(AccountError, msg=bad) as ctx:
                self.store.register(bad, "pw")
            self.assertEqual("invalid_username", ctx.exception.code)
        for bad in ("", "x" * 33, "tab\there"):
            with self.assertRaises(AccountError, msg=bad) as ctx:
                self.store.register("bob", bad)
            self.assertEqual("invalid_password", ctx.exception.code)

    # -- 显示昵称（会话 21）--------------------------------------------------
    def test_nickname_defaults_to_the_username(self):
        # 需求原文：「留空时默认昵称为用户名」。空串、全空白、根本不传，三种都算留空。
        for i, blank in enumerate(("", "   ", None)):
            name = f"user{i}"
            account = self.store.register(name, "pw", display_name=blank)
            self.assertEqual(name, account["display_name"])
        self.assertEqual("bob", self.store.register("bob", "pw")["display_name"])

    def test_nickname_is_stored_and_trimmed(self):
        account = self.store.register("alice", "pw", display_name="  炮炮  ")
        self.assertEqual("炮炮", account["display_name"])
        self.assertEqual("炮炮",
                         self.store.get_account("alice")[1]["display_name"])

    def test_nickname_rejects_what_would_corrupt_the_wire_format(self):
        # ★ 补充平面字符（emoji）在 UTF-16 里占两个码元，而 `w_wstr` 写的长度
        #   是 Python 的字符数 —— 放进去客户端从那个包起整条流都解错位。
        for bad in ("a" * 17, "emoji🎮", "tab\there", "nul\x00"):
            with self.assertRaises(AccountError, msg=bad) as ctx:
                self.store.register("bob", "pw", display_name=bad)
            self.assertEqual("invalid_nickname", ctx.exception.code)
        self.assertFalse(self.store.has_account("bob"), "失败不该留下半个账号")

    def test_duplicate_nickname_is_reported_separately_from_duplicate_username(self):
        # 需求原文：「用户名重复和昵称重复需要分别单独 check」。
        self.store.register("alice", "pw", display_name="炮炮")
        with self.assertRaises(AccountError) as ctx:
            self.store.register("bob", "pw", display_name="炮炮")
        self.assertEqual("duplicate_nickname", ctx.exception.code)
        self.assertIn("昵称", ctx.exception.message)
        self.assertFalse(self.store.has_account("bob"))
        # 用户名撞车仍然是另一条路、另一句话。
        with self.assertRaises(AccountError) as ctx:
            self.store.register("alice", "pw", display_name="别的昵称")
        self.assertEqual("duplicate", ctx.exception.code)
        # 换个昵称就能注册。
        self.assertEqual("轰轰",
                         self.store.register("bob", "pw",
                                             display_name="轰轰")["display_name"])

    def test_duplicate_nickname_ignores_case_and_padding(self):
        self.store.register("alice", "pw", display_name="Boom")
        for same in ("boom", "BOOM", "  Boom  "):
            with self.assertRaises(AccountError, msg=same) as ctx:
                self.store.register("bob", "pw", display_name=same)
            self.assertEqual("duplicate_nickname", ctx.exception.code)

    def test_a_blank_nickname_can_still_collide_via_the_username(self):
        # 昵称留空 = 用用户名，所以「叫 bob 的昵称」会挡住用户名 bob 的注册。
        self.store.register("alice", "pw", display_name="bob")
        with self.assertRaises(AccountError) as ctx:
            self.store.register("bob", "pw")
        self.assertEqual("duplicate_nickname", ctx.exception.code)

    def test_an_old_account_without_a_nickname_still_owns_its_username(self):
        # 老存档里 display_name 可能是空的，那时用户名自己就是昵称。
        self.store.register("alice", "pw")
        raw = json.load(open(self.path, encoding="utf-8"))
        raw["accounts"]["alice"]["display_name"] = ""
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        self.assertEqual("alice", self.store.nickname_owner("alice"))
        with self.assertRaises(AccountError) as ctx:
            self.store.register("bob", "pw", display_name="alice")
        self.assertEqual("duplicate_nickname", ctx.exception.code)

    def test_nickname_owner_says_none_when_nobody_has_it(self):
        self.store.register("alice", "pw", display_name="炮炮")
        self.assertIsNone(self.store.nickname_owner("没人用过"))
        self.assertIsNone(self.store.nickname_owner(""))

    def test_verify_tells_the_three_cases_apart(self):
        # 需求：不存在要提示注册，密码错要如实报错 —— 不能合并成一个布尔。
        self.store.register("alice", "pw")
        self.assertEqual(AUTH_OK, self.store.verify("alice", "pw")[0])
        self.assertEqual(AUTH_BAD_PASSWORD, self.store.verify("alice", "PW")[0])
        self.assertEqual(AUTH_NO_SUCH_USER, self.store.verify("nobody", "pw")[0])

    def test_verify_never_creates_an_account(self):
        # V0.1 的假后台会给未知账号自动建号；联机之后那等于谁都能顶别人的名字。
        self.assertEqual(AUTH_NO_SUCH_USER, self.store.verify("ghost", "pw")[0])
        self.assertEqual([], self.store.usernames())

    def test_accounts_are_isolated_from_each_other(self):
        self.store.register("alice", "pw1")
        self.store.register("bob", "pw2")
        self.store.add_quest_reward("alice", experience=250, money=70)
        _, alice = self.store.get_account("alice")
        _, bob = self.store.get_account("bob")
        self.assertEqual((250, 70), (alice["experience"], alice["money"]))
        self.assertEqual((0, 0), (bob["experience"], bob["money"]))
        self.assertEqual(AUTH_BAD_PASSWORD, self.store.verify("bob", "pw1")[0])

    def test_a_v01_save_with_active_account_still_loads(self):
        # 老存档不能让新服务端起不来，但 active_account 下次写盘就该消失。
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1, "active_account": "alice",
                       "accounts": {"alice": {"password": "pw"}}}, f)
        self.assertEqual(AUTH_OK, self.store.verify("alice", "pw")[0])
        self.store.set_character("alice", 1)
        with open(self.path, "r", encoding="utf-8") as f:
            self.assertNotIn("active_account", json.load(f))

    # -- 对战等级限制（需求：默认解除）---------------------------------------
    def test_reported_level_never_equals_one(self):
        # 客户端判的是 [0x72e338] == 1（恰好等于 1）就弹「不符合等级要求」（§83）。
        account = self.account()
        self.assertEqual(1, account["level"])            # 存档里是真实等级
        self.assertEqual(MINIMUM_PLAYER_LEVEL, player_level(account))
        self.assertGreater(player_level(account), 1)

    def test_the_level_floor_does_not_distort_the_experience_bar(self):
        # 经验条两端由 experience_bounds 算，必须按真实等级来，
        # 否则新号一进游戏经验条就是负的。
        from account_store import experience_bounds
        self.assertEqual((0, EXPERIENCE_PER_LEVEL), experience_bounds(0))

    # -- 存档转移助手（里程碑 G 的后端）--------------------------------------
    def test_export_round_trips_through_import(self):
        self.store.register("alice", "pw")
        self.store.add_quest_reward("alice", experience=120, money=30)
        self.store.set_character("alice", 2)
        payload = self.store.export_account("alice")
        self.assertEqual("alice", payload["username"])
        self.assertEqual("pw", payload["account"]["password"])

        other = AccountStore(os.path.join(self.tmp.name, "other.json"))
        name, action = other.import_account(payload)
        self.assertEqual(("alice", "created"), (name, action))
        _, moved = other.get_account("alice")
        self.assertEqual((120, 30, 2),
                         (moved["experience"], moved["money"], moved["character"]))

    def test_export_rejects_an_unknown_account(self):
        with self.assertRaises(AccountError):
            self.store.export_account("nobody")

    def test_import_over_an_existing_account_needs_the_password(self):
        self.store.register("alice", "pw")
        payload = {"popshot_save": 1, "username": "alice",
                   "account": {"password": "pw", "money": 999}}
        with self.assertRaises(AccountError) as ctx:
            self.store.import_account(payload, "alice", "wrong")
        self.assertEqual("bad_password", ctx.exception.code)
        with self.assertRaises(AccountError) as ctx:
            self.store.import_account(payload)          # 一个字都没填
        self.assertEqual("auth_required", ctx.exception.code)
        self.assertEqual(0, self.store.get_account("alice")[1]["money"])

    def test_import_with_wrong_credentials_says_they_are_wrong(self):
        # 「一个字都没填」和「填了但填错」必须是两句不同的话 —— 后者回
        # 「请填入用户名和密码」的话，打错密码的人只会对着填好的框发呆。
        self.store.register("alice", "pw")
        payload = {"popshot_save": 1, "username": "alice",
                   "account": {"password": "pw", "money": 999}}
        for auth in (("bob", "pw"), ("alice", "nope"), ("", "pw"), ("alice", "")):
            with self.assertRaises(AccountError, msg=repr(auth)) as ctx:
                self.store.import_account(payload, *auth)
            self.assertEqual("bad_password", ctx.exception.code, msg=repr(auth))
            self.assertIn("用户名或密码错误", ctx.exception.message)
        self.assertEqual(0, self.store.get_account("alice")[1]["money"])

    def test_import_makes_a_hand_raised_level_stick(self):
        # 玩家把导出的 JSON 里的 level 从 1 改成 5 再传上来。等级是由经验推出来的
        # 派生字段，光改它一读回来就被打回原形 —— 导入时要把经验补到那一级。
        self.store.register("alice", "pw")
        payload = {"popshot_save": 1, "username": "alice",
                   "account": {"password": "pw", "level": 5}}
        self.store.import_account(payload, "alice", "pw")
        _, account = self.store.get_account("alice")
        self.assertEqual(5, account["level"])
        self.assertEqual(EXPERIENCE_PER_LEVEL * 4, account["experience"])
        # 重新读一遍还得是 5：经验补上了，等级就不会再被算回去。
        self.assertEqual(5, AccountStore(self.path).get_account("alice")[1]["level"])

    def test_import_prefers_the_experience_when_only_it_was_raised(self):
        # 另一种常见改法：只改经验、level 留着不动。这时候拿旧的 level 去
        # 覆盖经验会把玩家的改动抹掉，所以经验大的一方说了算。
        self.store.register("alice", "pw")
        payload = {"popshot_save": 1, "username": "alice",
                   "account": {"password": "pw", "level": 1,
                               "experience": EXPERIENCE_PER_LEVEL * 9}}
        self.store.import_account(payload, "alice", "pw")
        _, account = self.store.get_account("alice")
        self.assertEqual(EXPERIENCE_PER_LEVEL * 9, account["experience"])
        self.assertEqual(10, account["level"])

    def test_import_updates_every_field_of_an_existing_account(self):
        """导入要能改**每一个**字段，不是只改得动其中几个。"""
        self.store.register("alice", "pw")
        changed = {
            "password": "newpw",
            "display_name": "AliceII",
            "tutorial_completed": True,
            "tutorial_progress": 5,
            "level": 3,
            "experience": EXPERIENCE_PER_LEVEL * 2 + 50,
            "money": 777,
            "character": 7,
            "quest_difficulty": {"5": 2},
            "quest_unlock_all": False,
            "character_unlock_all": False,
            "owned_characters": [101, 105],
        }
        self.assertEqual(sorted(changed), sorted(NEW_ACCOUNT_DEFAULTS),
                         "存档新增字段了？这条用例要跟着补")
        payload = {"popshot_save": 1, "username": "alice", "account": changed}
        self.store.import_account(payload, "alice", "pw")
        _, account = self.store.get_account("alice")
        for key, want in changed.items():
            self.assertEqual(want, account[key], msg=key)
        # 密码换了就要能用新的登进来、旧的登不进去。
        self.assertEqual(AUTH_OK, self.store.verify("alice", "newpw")[0])
        self.assertEqual(AUTH_BAD_PASSWORD, self.store.verify("alice", "pw")[0])

    def test_import_resets_fields_missing_from_the_upload(self):
        # 需求原文：上传的 json 中没有的字段，要把服务器上的对应字段重置为默认值。
        self.store.register("alice", "pw")
        self.store.add_quest_reward("alice", experience=500, money=500)
        self.store.set_quest_cleared("alice", 3, 2)
        payload = {"popshot_save": 1, "username": "alice",
                   "account": {"password": "pw", "money": 7}}
        name, action = self.store.import_account(payload, "alice", "pw")
        self.assertEqual(("alice", "replaced"), (name, action))
        _, account = self.store.get_account("alice")
        self.assertEqual(7, account["money"])
        self.assertEqual(NEW_ACCOUNT_DEFAULTS["experience"], account["experience"])
        self.assertEqual(NEW_ACCOUNT_DEFAULTS["quest_difficulty"],
                         account["quest_difficulty"])

    def test_import_ignores_unknown_fields_in_the_upload(self):
        # 存档是给人手改的，多塞几个键不能让整个导入失败，但也不能被写进存档。
        payload = {"popshot_save": 1, "username": "carol",
                   "account": {"password": "pw", "money": 5, "cheat": True}}
        self.store.import_account(payload)
        _, account = self.store.get_account("carol")
        self.assertNotIn("cheat", account)
        self.assertEqual(5, account["money"])

    def test_import_without_a_password_anywhere_is_refused(self):
        payload = {"popshot_save": 1, "username": "dave", "account": {"money": 1}}
        with self.assertRaises(AccountError) as ctx:
            self.store.import_account(payload)
        self.assertEqual("password_required", ctx.exception.code)

    def test_import_rejects_a_file_that_is_not_a_save(self):
        for bad in (None, [], {"hello": "world"}, {"account": {"money": 1}}):
            with self.assertRaises(AccountError, msg=repr(bad)) as ctx:
                self.store.import_account(bad)
            self.assertEqual("bad_save", ctx.exception.code)

    def test_tutorial_progress_below_the_threshold_does_not_complete(self):
        self.account()
        account = self.store.set_tutorial_progress("alice", 1)
        self.assertFalse(account["tutorial_completed"])
        self.assertEqual(1, account["tutorial_progress"])
        self.assertEqual(0, tutorial_state(account))

    def test_manually_clearing_the_flag_forces_the_tutorial_again(self):
        # tutorial_completed 是给人编辑的开关，必须压过残留的进度值。
        self.account()
        self.store.set_tutorial_progress("alice", 5)
        self.store.set_tutorial_completed("alice", False)
        _, account = self.store.get_account("alice")
        self.assertEqual(0, tutorial_state(account))

    def test_tutorial_progress_rejects_unknown_account(self):
        with self.assertRaises(KeyError):
            self.store.set_tutorial_progress("nobody", 5)

    def test_reading_realigns_a_hand_edited_level_with_the_experience(self):
        self.account()
        self.store.add_quest_reward("alice",
                                    experience=EXPERIENCE_PER_LEVEL * 3)
        # 手工把等级改乱，读的时候应该按经验校回来。
        with open(self.path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        saved["accounts"]["alice"]["level"] = 99
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(saved, f)
        self.assertEqual(4, self.store.get_account("alice")[1]["level"])

    def test_new_account_default_level_unlocks_the_quest_list(self):
        # 闯关关卡记录的要求等级是 1；等级 0 会让任务下拉框整个空掉。
        account = self.account()
        self.assertGreaterEqual(player_level(account), 1)

    def test_player_level_tolerates_missing_or_bad_values(self):
        self.assertEqual(0, player_level(None))
        self.assertEqual(0, player_level({}))
        self.assertEqual(0, player_level({"level": "abc"}))
        self.assertEqual(0, player_level({"level": -5}))
        self.assertEqual(6, player_level({"level": 6}))

    def test_quest_reward_accumulates_and_persists(self):
        # 结算所得必须落盘，否则玩家一退出就退回原点（D024）。
        self.account()
        self.store.add_quest_reward("alice", experience=30, money=30)
        account = self.store.add_quest_reward("alice", experience=12, money=5)
        self.assertEqual(42, account["experience"])
        self.assertEqual(35, account["money"])
        _, reloaded = self.store.get_account("alice")
        self.assertEqual(42, reloaded["experience"])
        self.assertEqual(35, reloaded["money"])

    def test_quest_reward_raises_the_level_across_the_curve(self):
        self.account()
        account = self.store.add_quest_reward("alice",
                                              experience=EXPERIENCE_PER_LEVEL)
        self.assertEqual(2, account["level"])
        self.assertEqual(2, player_level(account))

    def test_quest_reward_rejects_unknown_account(self):
        with self.assertRaises(KeyError):
            self.store.add_quest_reward("nobody", experience=1)

    def test_selected_character_persists(self):
        # 换角色是客户端报、服务端广播回去才生效的，存下来重登才不会跳回 0。
        self.account()
        self.assertEqual(0, player_character(self.account()))
        account = self.store.set_character("alice", 2)
        self.assertEqual(2, player_character(account))
        _, reloaded = self.store.get_account("alice")
        self.assertEqual(2, player_character(reloaded))

    def test_selected_character_rejects_unknown_account(self):
        with self.assertRaises(KeyError):
            self.store.set_character("nobody", 1)

    # -- 难度解锁（会话 20，§118）-------------------------------------------
    def test_new_account_can_pick_every_difficulty(self):
        # 「只能选简单」是因为服务端从来没下发过这张表，不是玩家没打通。
        account = self.account()
        self.assertTrue(quest_unlock_all(account))
        records = quest_difficulty_records(account)
        self.assertEqual(set(QUEST_ID_TABLE), set(records))
        self.assertEqual({QUEST_DIFFICULTY_MAX}, set(records.values()))

    def test_quest_clear_persists_and_only_moves_up(self):
        self.account()
        account = self.store.set_quest_cleared("alice", 3, 2)
        self.assertEqual(2, quest_cleared_difficulty(account, 3))
        # 再用简单打一遍，不该把已经解锁的普通锁回去。
        account = self.store.set_quest_cleared("alice", 3, 1)
        self.assertEqual(2, quest_cleared_difficulty(account, 3))
        _, reloaded = self.store.get_account("alice")
        self.assertEqual(2, quest_cleared_difficulty(reloaded, 3))

    def test_quest_clear_is_written_with_string_keys(self):
        # JSON 的对象键只能是字符串；写成数字键会在下次读盘时变形。
        self.account()
        self.store.set_quest_cleared("alice", 3, 2)
        with open(self.path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual({"3": 2},
                         saved["accounts"]["alice"]["quest_difficulty"])

    def test_progression_mode_only_unlocks_one_step_ahead(self):
        # quest_unlock_all=False 就是原版行为：通关简单才解锁普通。
        self.account()
        self.store.set_tutorial_completed("alice", True)
        _, account = self.store.get_account("alice")
        account = dict(account, quest_unlock_all=False)
        self.assertEqual({}, quest_difficulty_records(account))
        account["quest_difficulty"] = {"3": 1}
        self.assertEqual({3: 1}, quest_difficulty_records(account))

    def test_quest_clear_rejects_unknown_account(self):
        with self.assertRaises(KeyError):
            self.store.set_quest_cleared("nobody", 1, 1)


if __name__ == "__main__":
    unittest.main()
