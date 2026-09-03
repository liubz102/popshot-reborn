#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
import struct
import tempfile
import unittest

from account_store import (ADMIN_ACCOUNTS_KEY, AUTH_BAD_PASSWORD,
                           AUTH_NO_SUCH_USER, AUTH_OK,
                           DEFAULT_ADMIN_NAME, DEFAULT_ADMIN_PASSWORD,
                           EXPERIENCE_PER_LEVEL, EXPERIENCE_STEP, LEVEL_MAX,
                           MINIMUM_PLAYER_LEVEL,
                           NEW_ACCOUNT_DEFAULTS, QUEST_DIFFICULTY_MAX,
                           QUEST_ID_TABLE, AccountError, AccountStore,
                           equipped_items, experience_bounds,
                           experience_for_level, has_item, inventory_items,
                           level_for_experience, material_count,
                           material_counts, normalize_item_fields,
                           owned_item_ids,
                           player_character, player_level, player_money,
                           quest_cleared_difficulty, quest_difficulty_records,
                           quest_unlock_all, tutorial_state)
from gameserver import build_gsp_rep_login


#: 下面这些 id 都来自真的 `shop_items.json`，不是编的。
#: `test_the_fixture_ids_still_mean_what_the_tests_assume` 守着它们的性质
#: —— 哪天物品表换代了，先炸的是那一条，而不是十几条语义不明的断言。
REVOLVER_R1 = 1120041        # 리볼버 R1，武器槽 1（part_flag 1024）
REVOLVER_R2 = 1120042        # 同一个武器槽的另一把
TOP_ARMOR = 1010015          # 上衣（part_flag 1），和武器不抢槽
STOCK_ONLY = 1510001         # ★ 只有 `[Stock-]` 的期限售卖形态，进不了背包（§11）
BRONZE_PIPE = 30018          # 청동파이프 青铜管（材料）
BLACK_BEAD = 10001           # 검은구슬 黑珠（材料）
NO_SUCH_ITEM = 9999999       # 物品表里根本没有


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
        # 二次曲线：650 点落在 4 级（本级 600 起、下一级 1000）。
        self.assertEqual(600, values[4])     # 本级起点 -> 0x72e340
        self.assertEqual(1000, values[5])    # 下一级所需 -> 0x72e344
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

    # -- 修改密码 / 修改昵称（会话 22）----------------------------------------
    def test_change_password_requires_the_old_password_and_preserves_the_save(self):
        self.store.register("alice", "oldpw", display_name="炮炮")
        self.store.add_quest_reward("alice", experience=250, money=70)
        with self.assertRaises(AccountError) as ctx:
            self.store.change_password("alice", "wrong", "newpw")
        self.assertEqual("bad_password", ctx.exception.code)
        self.assertEqual(AUTH_OK, self.store.verify("alice", "oldpw")[0])

        changed = self.store.change_password("alice", "oldpw", "newpw")
        self.assertEqual("newpw", changed["password"])
        self.assertEqual((250, 70), (changed["experience"], changed["money"]))
        self.assertEqual("炮炮", changed["display_name"])
        self.assertEqual(AUTH_BAD_PASSWORD, self.store.verify("alice", "oldpw")[0])
        self.assertEqual(AUTH_OK, self.store.verify("alice", "newpw")[0])

    def test_change_password_reuses_the_registration_rules(self):
        self.store.register("alice", "oldpw")
        for bad in ("", "x" * 33, "tab\there"):
            with self.assertRaises(AccountError, msg=bad) as ctx:
                self.store.change_password("alice", "oldpw", bad)
            self.assertEqual("invalid_password", ctx.exception.code)
            self.assertEqual(AUTH_OK, self.store.verify("alice", "oldpw")[0])

    def test_account_changes_verify_the_old_password_before_new_value_rules(self):
        self.store.register("alice", "oldpw")
        with self.assertRaises(AccountError) as ctx:
            self.store.change_password("alice", "wrong", "")
        self.assertEqual("bad_password", ctx.exception.code)
        with self.assertRaises(AccountError) as ctx:
            self.store.change_nickname("alice", "wrong", "emoji🎮")
        self.assertEqual("bad_password", ctx.exception.code)

    def test_change_nickname_requires_the_password_and_persists_a_trimmed_name(self):
        self.store.register("alice", "pw", display_name="旧昵称")
        with self.assertRaises(AccountError) as ctx:
            self.store.change_nickname("alice", "wrong", "新昵称")
        self.assertEqual("bad_password", ctx.exception.code)
        self.assertEqual("旧昵称", self.store.get_account("alice")[1]["display_name"])

        changed = self.store.change_nickname("alice", "pw", "  新昵称  ")
        self.assertEqual("新昵称", changed["display_name"])
        self.assertEqual("新昵称", self.store.get_account("alice")[1]["display_name"])
        self.assertEqual(AUTH_OK, self.store.verify("alice", "pw")[0])

    def test_change_nickname_rejects_another_users_name_but_allows_its_own(self):
        self.store.register("alice", "pw", display_name="Boom")
        self.store.register("bob", "pw", display_name="Bob昵称")
        with self.assertRaises(AccountError) as ctx:
            self.store.change_nickname("bob", "pw", " boom ")
        self.assertEqual("duplicate_nickname", ctx.exception.code)
        self.assertEqual("Bob昵称", self.store.get_account("bob")[1]["display_name"])

        # 大小写不同但 owner 仍是自己：这是合法的幂等修改，不应被查重挡住。
        changed = self.store.change_nickname("alice", "pw", " BOOM ")
        self.assertEqual("BOOM", changed["display_name"])

    def test_change_nickname_can_fall_back_to_the_username(self):
        self.store.register("alice", "pw", display_name="旧昵称")
        changed = self.store.change_nickname("alice", "pw", "   ")
        self.assertEqual("alice", changed["display_name"])

    def test_account_changes_report_an_unknown_user_without_creating_one(self):
        for operation in (
                lambda: self.store.change_password("ghost", "pw", "newpw"),
                lambda: self.store.change_nickname("ghost", "pw", "新昵称")):
            with self.assertRaises(AccountError) as ctx:
                operation()
            self.assertEqual("no_such_user", ctx.exception.code)
        self.assertEqual([], self.store.usernames())

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
    def test_reported_level_unlocks_survival_mode(self):
        # 房主在中国区房间里选生存模式时，0x465a2c 会把
        # [0x72e338] < 4 的模式强制改回夺分（§203）。
        account = self.account()
        self.assertEqual(1, account["level"])            # 存档里是真实等级
        self.assertEqual(4, MINIMUM_PLAYER_LEVEL)
        self.assertEqual(MINIMUM_PLAYER_LEVEL, player_level(account))
        self.assertGreaterEqual(player_level(account), 4)
        values = struct.unpack_from("<8i", build_gsp_rep_login(account=account), 8)
        self.assertEqual(4, values[0])                     # 登录全局 0x72e338

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
        self.assertEqual(experience_for_level(5), account["experience"])
        # 重新读一遍还得是 5：经验补上了，等级就不会再被算回去。
        self.assertEqual(5, AccountStore(self.path).get_account("alice")[1]["level"])

    def test_import_lets_the_level_win_when_the_two_fields_disagree(self):
        # ★ D151：**等级说了算**。只改 experience、level 留着旧值时，
        # 两个字段矛盾 -> 认 level，经验被重算回那一级的起点。
        # 注册页上已经写明「只改 experience 没用」。
        self.store.register("alice", "pw")
        payload = {"popshot_save": 1, "username": "alice",
                   "account": {"password": "pw", "level": 1,
                               "experience": 9999}}
        self.store.import_account(payload, "alice", "pw")
        _, account = self.store.get_account("alice")
        self.assertEqual(1, account["level"])
        self.assertEqual(experience_for_level(1), account["experience"])

    def test_import_lets_the_level_go_down_too(self):
        # 旧规则只能往上抬（「想降级得连经验一起改小」）；现在降级也生效。
        self.store.register("alice", "pw")
        self.store.add_quest_reward("alice", experience=29900)
        payload = {"popshot_save": 1, "username": "alice",
                   "account": {"password": "pw", "level": 3,
                               "experience": 29900}}
        self.store.import_account(payload, "alice", "pw")
        _, account = self.store.get_account("alice")
        self.assertEqual(3, account["level"])
        self.assertEqual(experience_for_level(3), account["experience"])

    def test_import_clamps_a_hand_written_level_to_the_cap(self):
        # 手写 level: 999 只能得到 60 级，不会算出一个天文数字的经验。
        self.store.register("alice", "pw")
        payload = {"popshot_save": 1, "username": "alice",
                   "account": {"password": "pw", "level": 999}}
        self.store.import_account(payload, "alice", "pw")
        _, account = self.store.get_account("alice")
        self.assertEqual(LEVEL_MAX, account["level"])
        self.assertEqual(experience_for_level(LEVEL_MAX), account["experience"])

    def test_import_keeps_the_experience_when_the_two_fields_agree(self):
        """★ 导出 -> 原样导回，**本级内攒的经验一分不丢**。

        两个字段本来就自洽时不许按等级重算 —— 否则每次转存都会把玩家
        打回本级起点（5 级、1200 点会被砍成 1000）。
        """
        self.store.register("alice", "pw")
        self.store.add_quest_reward("alice", experience=1200, money=7)
        payload = self.store.export_account("alice")
        self.assertEqual(5, payload["account"]["level"])
        self.assertEqual(1200, payload["account"]["experience"])
        self.store.import_account(payload, "alice", "pw")
        _, account = self.store.get_account("alice")
        self.assertEqual(5, account["level"])
        self.assertEqual(1200, account["experience"])

    def test_import_survives_a_broken_level_field(self):
        # level 缺失 / 是字符串 / 是负数 -> 以经验为准，不许抛异常。
        for bad in ({}, {"level": "abc"}, {"level": -5}):
            fields = {"password": "pw", "experience": 1200}
            fields.update(bad)
            self.store.import_account(
                {"popshot_save": 1, "username": "alice", "account": fields},
                "alice", "pw")
            _, account = self.store.get_account("alice")
            self.assertEqual(1200, account["experience"], bad)
            self.assertEqual(5, account["level"], bad)

    def test_import_updates_every_field_of_an_existing_account(self):
        """导入要能改**每一个**字段，不是只改得动其中几个。"""
        self.store.register("alice", "pw")
        changed = {
            "password": "newpw",
            "display_name": "AliceII",
            "tutorial_completed": True,
            "tutorial_progress": 5,
            "level": 3,
            # ★ D151「等级说了算」：level 和 experience 矛盾时经验按等级重算，
            #   所以这里写自洽的那一对，这条用例才是在测「字段改得动」而不是曲线。
            "experience": experience_for_level(3),
            "money": 777,
            "character": 7,
            "quest_difficulty": {"5": 2},
            "quest_unlock_all": False,
            "character_unlock_all": False,
            "owned_characters": [101, 105],
            # ★ 这三个写的是**规范形态**：`import_account` 会当场洗一遍
            #   （上传的是人手改过的文件），洗完要和写进去的一模一样。
            "inventory": {str(REVOLVER_R1): {"count": 1, "expires": None}},
            "equipped": [REVOLVER_R1],
            "materials": {str(BRONZE_PIPE): 3},
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
        self.store.add_quest_reward("alice", experience=experience_for_level(4))
        # 手工把等级改乱，读的时候应该按经验校回来。
        with open(self.path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        saved["accounts"]["alice"]["level"] = 99
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(saved, f)
        self.assertEqual(4, self.store.get_account("alice")[1]["level"])

    # -- 换曲线之后的存档对齐（§229 / D150）----------------------------------
    def write_raw(self, accounts):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 2, "accounts": accounts}, f)

    def raw_levels(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return {k: v["level"] for k, v in json.load(f)["accounts"].items()}

    def test_realign_rewrites_levels_left_over_from_the_linear_curve(self):
        # 云服上真实存在的局面：旧曲线（每级恒 100）把号刷到了好几百级。
        self.write_raw({
            "veteran": {"password": "p", "level": 300, "experience": 29900},
            "midlevel": {"password": "p", "level": 60, "experience": 5900},
            "fresh": {"password": "p", "level": 1, "experience": 0},
        })
        changed = AccountStore(self.path).realign_levels()
        by_name = {row["username"]: row for row in changed}
        self.assertEqual({"veteran", "midlevel"}, set(by_name))
        self.assertEqual((300, 24), (by_name["veteran"]["old"],
                                     by_name["veteran"]["new"]))
        self.assertEqual((60, 11), (by_name["midlevel"]["old"],
                                    by_name["midlevel"]["new"]))
        # 经验一分不动 —— 这一版的迁移口径就是「按经验重算，该降就降」。
        self.assertEqual(29900, by_name["veteran"]["experience"])
        self.assertEqual({"veteran": 24, "midlevel": 11, "fresh": 1},
                         self.raw_levels())

    def test_realign_flags_the_accounts_pinned_at_the_cap(self):
        self.write_raw({
            "whale": {"password": "p", "level": 4000, "experience": 400000},
            "normal": {"password": "p", "level": 9, "experience": 29900},
        })
        changed = {row["username"]: row for row in
                   AccountStore(self.path).realign_levels()}
        self.assertEqual(LEVEL_MAX, changed["whale"]["new"])
        self.assertTrue(changed["whale"]["capped"])
        self.assertFalse(changed["normal"]["capped"])

    def test_realign_is_idempotent_and_does_not_rewrite_the_file(self):
        self.write_raw({"veteran": {"password": "p", "level": 300,
                                    "experience": 29900}})
        store = AccountStore(self.path)
        self.assertTrue(store.realign_levels())
        mtime = os.path.getmtime(self.path)
        # ★ 跑第二遍必须什么都不改、也不写盘 —— 它每次启动都会跑。
        self.assertEqual([], store.realign_levels())
        self.assertEqual(mtime, os.path.getmtime(self.path))

    def test_realign_survives_a_broken_level_field(self):
        self.write_raw({
            "broken": {"password": "p", "level": "x", "experience": 1200},
            "junk": "not a dict",
        })
        changed = {row["username"]: row for row in
                   AccountStore(self.path).realign_levels()}
        self.assertEqual(5, changed["broken"]["new"])
        self.assertNotIn("junk", changed)

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
        self.assertEqual(MINIMUM_PLAYER_LEVEL, player_level(account))

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


# ==========================================================================
# 仓库 / 装备 / 材料 / 管理员（V0.3商店 M2）
# ==========================================================================

      # 物品表里根本没有


class ItemFieldTests(unittest.TestCase):
    """仓库 / 装备 / 材料三件套 + 幂等补齐。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "accounts.json")
        self.store = AccountStore(self.path)
        self.store.register("alice", "pw")

    def tearDown(self):
        self.tmp.cleanup()

    def saved(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def raw_bytes(self):
        with open(self.path, "rb") as f:
            return f.read()

    def write_raw(self, data):
        """直接铺一份存档（模拟老存档 / 手改过的存档）。"""
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return AccountStore(self.path)

    # ------------------------------------------------------------ 前提校验
    def test_the_fixture_ids_still_mean_what_the_tests_assume(self):
        # 物品表是从原版 ini 提取的产物，换代后这几条性质要是变了，
        # 下面所有用例的含义都会跟着变 —— 让它先炸，别让人去猜。
        import shopdata
        self.assertTrue(shopdata.ownable(REVOLVER_R1))
        self.assertTrue(shopdata.ownable(REVOLVER_R2))
        self.assertTrue(shopdata.conflicts(REVOLVER_R1, REVOLVER_R2))
        self.assertTrue(shopdata.ownable(TOP_ARMOR))
        self.assertFalse(shopdata.conflicts(REVOLVER_R1, TOP_ARMOR))
        # 只有货架条目的塞进背包，客户端查不到定义，仓库里是空格子。
        self.assertFalse(shopdata.ownable(STOCK_ONLY))
        self.assertFalse(shopdata.exists(NO_SUCH_ITEM))
        for material in (BRONZE_PIPE, BLACK_BEAD):
            self.assertTrue(shopdata.is_material(material))
            self.assertTrue(shopdata.ownable(material))

    def test_new_account_starts_with_empty_item_fields(self):
        _, account = self.store.get_account("alice")
        self.assertEqual({}, inventory_items(account))
        self.assertEqual([], equipped_items(account))
        self.assertEqual({}, material_counts(account))
        # 新号注册时就该把三个键写进磁盘，不用等 `ensure_item_fields()`。
        for field in ("inventory", "equipped", "materials"):
            self.assertIn(field, self.saved()["accounts"]["alice"])

    # ---------------------------------------------------------------- 金币
    def test_spend_money_deducts_and_persists(self):
        self.store.add_quest_reward("alice", money=5000)
        account = self.store.spend_money("alice", 3000)
        self.assertEqual(2000, player_money(account))
        self.assertEqual(2000, self.saved()["accounts"]["alice"]["money"])

    def test_spend_money_refuses_when_short_and_writes_nothing(self):
        self.store.add_quest_reward("alice", money=100)
        before = self.raw_bytes()
        with self.assertRaises(AccountError) as caught:
            self.store.spend_money("alice", 101)
        self.assertEqual("not_enough_money", caught.exception.code)
        # ★ 「差一块钱」也要一个字节都不动 —— 扣一半是最难查的那种账。
        self.assertEqual(before, self.raw_bytes())

    def test_spend_money_zero_does_not_rewrite_the_file(self):
        before = self.raw_bytes()
        self.store.spend_money("alice", 0)
        self.assertEqual(before, self.raw_bytes())

    def test_spend_money_rejects_a_negative_amount(self):
        with self.assertRaises(AccountError) as caught:
            self.store.spend_money("alice", -1)
        self.assertEqual("invalid_amount", caught.exception.code)

    def test_spend_money_rejects_unknown_account(self):
        with self.assertRaises(KeyError):
            self.store.spend_money("nobody", 1)

    # ---------------------------------------------------------------- 仓库
    def test_add_item_puts_it_in_the_warehouse(self):
        account = self.store.add_item("alice", REVOLVER_R1)
        self.assertEqual([REVOLVER_R1], owned_item_ids(account))
        self.assertTrue(has_item(account, REVOLVER_R1))
        self.assertFalse(has_item(account, TOP_ARMOR))
        # 键在 JSON 里必须是字符串（对象键只能是字符串）。
        self.assertEqual({"count": 1, "expires": None},
                         self.saved()["accounts"]["alice"]["inventory"][str(REVOLVER_R1)])

    def test_add_item_stacks_the_count(self):
        self.store.add_item("alice", BRONZE_PIPE, count=2)
        account = self.store.add_item("alice", BRONZE_PIPE, count=3)
        self.assertEqual(5, inventory_items(account)[BRONZE_PIPE]["count"])

    def test_add_item_rejects_ids_the_client_does_not_know(self):
        # 只有货架条目的（§11）和压根不存在的，都得在门口挡掉 ——
        # 放进去只会在仓库里变成一个空格子。
        for item_id in (STOCK_ONLY, NO_SUCH_ITEM):
            before = self.raw_bytes()
            with self.assertRaises(AccountError) as caught:
                self.store.add_item("alice", item_id)
            self.assertEqual("unknown_item", caught.exception.code)
            self.assertEqual(before, self.raw_bytes())

    def test_add_item_rejects_a_non_positive_count(self):
        for count in (0, -1):
            with self.assertRaises(AccountError) as caught:
                self.store.add_item("alice", REVOLVER_R1, count=count)
            self.assertEqual("invalid_count", caught.exception.code)

    def test_inventory_reader_accepts_the_shorthand_a_human_would_write(self):
        # 人手写存档最容易写成 `"1120041": 2`，没必要为此把整个背包判成坏的。
        account = {"inventory": {str(REVOLVER_R1): 2}}
        self.assertEqual({REVOLVER_R1: {"count": 2, "expires": None}},
                         inventory_items(account))

    def test_item_readers_tolerate_garbage(self):
        account = {"inventory": {"abc": 1, "-3": 1, str(REVOLVER_R1): 0,
                                 str(TOP_ARMOR): {"count": "x"}},
                   "materials": {"abc": 1, str(BRONZE_PIPE): -2,
                                 str(BLACK_BEAD): "7"},
                   "equipped": "not a list"}
        # 数量解析不出来时退回 1（「有这件东西」比「没有」更接近人的本意）。
        self.assertEqual({TOP_ARMOR: {"count": 1, "expires": None}},
                         inventory_items(account))
        self.assertEqual({BLACK_BEAD: 7}, material_counts(account))
        self.assertEqual([], equipped_items(account))
        self.assertEqual({}, inventory_items(None))
        self.assertEqual({}, material_counts({"materials": []}))

    # ---------------------------------------------------------------- 装备
    def test_equip_replaces_the_one_in_the_same_slot(self):
        self.store.add_item("alice", REVOLVER_R1)
        self.store.add_item("alice", REVOLVER_R2)
        account, _ = self.store.equip_item("alice", REVOLVER_R1)
        self.assertEqual([REVOLVER_R1], equipped_items(account))
        # ★ 刚点的那件排最前面 ⇒ 先到先得 ⇒ 旧的被顶下来。
        account, dropped = self.store.equip_item("alice", REVOLVER_R2)
        self.assertEqual([REVOLVER_R2], equipped_items(account))
        self.assertIn(REVOLVER_R1, dropped)

    def test_equip_keeps_items_in_different_slots(self):
        self.store.add_item("alice", REVOLVER_R1)
        self.store.add_item("alice", TOP_ARMOR)
        self.store.equip_item("alice", REVOLVER_R1)
        account, dropped = self.store.equip_item("alice", TOP_ARMOR)
        self.assertEqual([], dropped)
        self.assertEqual({REVOLVER_R1, TOP_ARMOR}, set(equipped_items(account)))

    def test_cannot_equip_something_not_owned(self):
        account, dropped = self.store.equip_item("alice", REVOLVER_R1)
        self.assertEqual([], equipped_items(account))
        self.assertEqual([REVOLVER_R1], dropped)

    def test_unequip_removes_only_that_one(self):
        self.store.add_item("alice", REVOLVER_R1)
        self.store.add_item("alice", TOP_ARMOR)
        self.store.equip_item("alice", REVOLVER_R1)
        self.store.equip_item("alice", TOP_ARMOR)
        account, _ = self.store.unequip_item("alice", TOP_ARMOR)
        self.assertEqual([REVOLVER_R1], equipped_items(account))

    def test_unequipping_something_not_worn_does_not_rewrite_the_file(self):
        self.store.add_item("alice", REVOLVER_R1)
        self.store.equip_item("alice", REVOLVER_R1)
        before = self.raw_bytes()
        self.store.unequip_item("alice", TOP_ARMOR)
        self.assertEqual(before, self.raw_bytes())

    def test_set_equipped_drops_conflicts_by_first_come_first_served(self):
        for item_id in (REVOLVER_R1, REVOLVER_R2, TOP_ARMOR):
            self.store.add_item("alice", item_id)
        account, dropped = self.store.set_equipped(
            "alice", [REVOLVER_R1, REVOLVER_R2, TOP_ARMOR])
        self.assertEqual([REVOLVER_R1, TOP_ARMOR], equipped_items(account))
        self.assertEqual([REVOLVER_R2], dropped)

    def test_a_hand_edited_save_cannot_wear_conflicting_gear(self):
        # 读的时候就地收敛（不用等启动补齐），因为 `0x030b` 是战斗加成的
        # 唯一来源，发一份抢槽的清单下去后果不可知（§1 / §4）。
        account = {"inventory": {str(REVOLVER_R1): 1, str(REVOLVER_R2): 1},
                   "equipped": [REVOLVER_R2, REVOLVER_R1]}
        self.assertEqual([REVOLVER_R2], equipped_items(account))

    # ---------------------------------------------------------------- 材料
    def test_add_materials_accumulates(self):
        self.store.add_materials("alice", {BRONZE_PIPE: 3})
        account, skipped = self.store.add_materials(
            "alice", {BRONZE_PIPE: 2, BLACK_BEAD: 12})
        self.assertEqual([], skipped)
        self.assertEqual({BRONZE_PIPE: 5, BLACK_BEAD: 12},
                         material_counts(account))
        self.assertEqual(5, material_count(account, BRONZE_PIPE))

    def test_add_materials_skips_unknown_ids_instead_of_failing(self):
        # ★ 这是**故意**和 `add_item` 相反的：调用点是结算发奖，一条配错的
        #   掉落规则不该让整局的结算包发不出去（玩家会卡在结算界面）。
        account, skipped = self.store.add_materials(
            "alice", {BRONZE_PIPE: 1, NO_SUCH_ITEM: 5, STOCK_ONLY: 1, "x": 1})
        self.assertEqual({BRONZE_PIPE: 1}, material_counts(account))
        self.assertEqual({NO_SUCH_ITEM, STOCK_ONLY, "x"}, set(skipped))

    def test_add_materials_ignores_non_positive_counts(self):
        account, skipped = self.store.add_materials(
            "alice", {BRONZE_PIPE: 0, BLACK_BEAD: -3})
        self.assertEqual({}, material_counts(account))
        self.assertEqual([], skipped)

    def test_consume_materials_is_all_or_nothing(self):
        self.store.add_materials("alice", {BRONZE_PIPE: 3, BLACK_BEAD: 1})
        before = self.raw_bytes()
        with self.assertRaises(AccountError) as caught:
            self.store.consume_materials("alice", {BRONZE_PIPE: 1, BLACK_BEAD: 2})
        self.assertEqual("not_enough_materials", caught.exception.code)
        # ★ 扣一半留一半 = 凭空吃掉玩家的青铜管。一个字节都不许动。
        self.assertEqual(before, self.raw_bytes())

    def test_consume_materials_deletes_the_slot_when_used_up(self):
        self.store.add_materials("alice", {BRONZE_PIPE: 3, BLACK_BEAD: 5})
        account = self.store.consume_materials(
            "alice", {BRONZE_PIPE: 3, BLACK_BEAD: 2})
        self.assertEqual({BLACK_BEAD: 3}, material_counts(account))
        # 用光的那一格直接删掉，别在存档里留一堆 0。
        self.assertNotIn(str(BRONZE_PIPE),
                         self.saved()["accounts"]["alice"]["materials"])

    def test_consume_nothing_does_not_rewrite_the_file(self):
        before = self.raw_bytes()
        self.store.consume_materials("alice", {})
        self.assertEqual(before, self.raw_bytes())

    def test_consume_materials_rejects_a_broken_request(self):
        for bad in ({"abc": 1}, {BRONZE_PIPE: -1}):
            with self.assertRaises(AccountError) as caught:
                self.store.consume_materials("alice", bad)
            self.assertEqual("invalid_material", caught.exception.code)

    # ------------------------------------------------------- 幂等补齐（D5）
    def test_ensure_item_fields_backfills_an_old_save(self):
        store = self.write_raw({
            "schema_version": 2,
            "accounts": {"old": {"password": "pw", "money": 500}},
        })
        report = store.ensure_item_fields()
        self.assertEqual(["old"], [row["username"] for row in report["accounts"]])
        saved = self.saved()["accounts"]["old"]
        self.assertEqual({}, saved["inventory"])
        self.assertEqual([], saved["equipped"])
        self.assertEqual({}, saved["materials"])

    def test_ensure_item_fields_keeps_every_pre_existing_field(self):
        # ★ 铁律 11：线上玩家数据一个字节都不能丢。
        before = {"password": "pw", "display_name": "爱丽丝", "money": 8800,
                  "experience": 4200, "level": 9, "tutorial_completed": True,
                  "tutorial_progress": 5, "character": 2,
                  "quest_difficulty": {"3": 2}, "quest_unlock_all": False,
                  "character_unlock_all": False, "owned_characters": [100, 104]}
        store = self.write_raw({"schema_version": 2,
                                "accounts": {"old": dict(before)}})
        store.ensure_item_fields()
        after = self.saved()["accounts"]["old"]
        for key, value in before.items():
            self.assertEqual(value, after[key], key)

    def test_ensure_item_fields_is_idempotent_and_does_not_rewrite_the_file(self):
        store = self.write_raw({
            "schema_version": 2,
            "accounts": {"old": {"password": "pw"}},
        })
        store.ensure_item_fields()
        before = self.raw_bytes()
        report = store.ensure_item_fields()
        self.assertEqual([], report["accounts"])
        self.assertIsNone(report["admin_created"])
        self.assertFalse(report["admin_broken"])
        # 幂等 ⇒ 每次启动都能跑，不需要 schema 版本号（D5）。
        self.assertEqual(before, self.raw_bytes())

    def test_ensure_item_fields_cleans_dirty_entries(self):
        store = self.write_raw({
            "schema_version": 2,
            "accounts": {"bob": {
                "password": "pw",
                "inventory": {str(REVOLVER_R1): 2,          # 简写
                              str(REVOLVER_R2): {"count": 1},
                              str(NO_SUCH_ITEM): {"count": 1}},
                # 抢同一个槽的 + 一件根本没有的
                "equipped": [REVOLVER_R1, REVOLVER_R2, TOP_ARMOR],
                "materials": {str(BRONZE_PIPE): 3, str(BLACK_BEAD): -1,
                              str(NO_SUCH_ITEM): 2, "abc": 5},
            }},
        })
        store.ensure_item_fields()
        saved = self.saved()["accounts"]["bob"]
        self.assertEqual({str(REVOLVER_R1): {"count": 2, "expires": None},
                          str(REVOLVER_R2): {"count": 1, "expires": None}},
                         saved["inventory"])
        self.assertEqual([REVOLVER_R1], saved["equipped"])
        self.assertEqual({str(BRONZE_PIPE): 3}, saved["materials"])

    def test_normalize_says_what_it_changed(self):
        _, _, _, notes = normalize_item_fields({"password": "pw"})
        self.assertEqual(["补上 inventory", "补上 equipped", "补上 materials"],
                         notes)
        _, _, _, notes = normalize_item_fields(
            {"inventory": {}, "equipped": [], "materials": {}})
        self.assertEqual([], notes)

    def test_import_keeps_and_cleans_the_item_fields(self):
        payload = {"popshot_save": 1, "username": "carol", "account": {
            "password": "pw",
            "inventory": {str(REVOLVER_R1): 1, str(NO_SUCH_ITEM): 1},
            "equipped": [NO_SUCH_ITEM, REVOLVER_R1],
            "materials": {str(BRONZE_PIPE): 4},
        }}
        self.store.import_account(payload)
        _, account = self.store.get_account("carol")
        self.assertEqual([REVOLVER_R1], owned_item_ids(account))
        self.assertEqual([REVOLVER_R1], equipped_items(account))
        self.assertEqual({BRONZE_PIPE: 4}, material_counts(account))
        # 上传的是人用记事本改过的文件 —— 脏条目不该落到磁盘上。
        self.assertNotIn(str(NO_SUCH_ITEM),
                         self.saved()["accounts"]["carol"]["inventory"])

    def test_export_carries_the_item_fields(self):
        self.store.add_item("alice", REVOLVER_R1)
        self.store.equip_item("alice", REVOLVER_R1)
        self.store.add_materials("alice", {BRONZE_PIPE: 2})
        payload = self.store.export_account("alice")["account"]
        self.assertIn(str(REVOLVER_R1), payload["inventory"])
        self.assertEqual([REVOLVER_R1], payload["equipped"])
        self.assertEqual({str(BRONZE_PIPE): 2}, payload["materials"])


class AdminAccountTests(unittest.TestCase):
    """管理页的管理员表（D3：明文口令，和玩家账号一个口径）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "accounts.json")
        self.store = AccountStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def saved(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def write_raw(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return AccountStore(self.path)

    def test_default_admin_is_created_once(self):
        self.assertEqual([], self.store.admin_names())
        report = self.store.ensure_item_fields()
        self.assertEqual(DEFAULT_ADMIN_NAME, report["admin_created"])
        self.assertEqual([DEFAULT_ADMIN_NAME], self.store.admin_names())
        # 再跑一遍不该冒出第二个，也不该把改过的口令盖回默认值。
        self.store.admin_set_password(DEFAULT_ADMIN_NAME, "NewPass1")
        self.assertIsNone(self.store.ensure_item_fields()["admin_created"])
        self.assertEqual(AUTH_OK,
                         self.store.admin_verify(DEFAULT_ADMIN_NAME, "NewPass1"))

    def test_an_empty_admin_table_means_the_page_is_off(self):
        # ★ 「键不在」和「键在但是空的」是两回事（D13）：后者是用户主动
        #   关掉了管理页，不该被我们又塞一个弱口令账号回去。
        store = self.write_raw({"schema_version": 2, "accounts": {},
                                ADMIN_ACCOUNTS_KEY: {}})
        self.assertIsNone(store.ensure_item_fields()["admin_created"])
        self.assertEqual([], store.admin_names())

    def test_verify_reports_the_three_states(self):
        self.store.ensure_item_fields()
        self.assertEqual(
            AUTH_OK,
            self.store.admin_verify(DEFAULT_ADMIN_NAME, DEFAULT_ADMIN_PASSWORD))
        self.assertEqual(AUTH_BAD_PASSWORD,
                         self.store.admin_verify(DEFAULT_ADMIN_NAME, "wrong"))
        self.assertEqual(AUTH_NO_SUCH_USER,
                         self.store.admin_verify("nobody", "whatever"))

    def test_add_and_remove(self):
        self.store.ensure_item_fields()
        self.assertEqual([DEFAULT_ADMIN_NAME, "carol"],
                         self.store.admin_add("carol", "SecretPw"))
        self.assertEqual(AUTH_OK, self.store.admin_verify("carol", "SecretPw"))
        self.assertEqual({"password": "SecretPw"},
                         self.saved()[ADMIN_ACCOUNTS_KEY]["carol"])
        self.assertEqual(["carol"], self.store.admin_remove(DEFAULT_ADMIN_NAME))

    def test_add_refuses_a_duplicate(self):
        self.store.ensure_item_fields()
        with self.assertRaises(AccountError) as caught:
            self.store.admin_add(DEFAULT_ADMIN_NAME, "whatever")
        self.assertEqual("admin_exists", caught.exception.code)

    def test_cannot_remove_the_last_admin(self):
        # ★ 拦在存档层，不只拦前端 —— 前端拦得住鼠标，拦不住直接 POST。
        self.store.ensure_item_fields()
        with self.assertRaises(AccountError) as caught:
            self.store.admin_remove(DEFAULT_ADMIN_NAME)
        self.assertEqual("last_admin", caught.exception.code)
        self.assertEqual([DEFAULT_ADMIN_NAME], self.store.admin_names())

    def test_remove_and_set_password_reject_unknown_names(self):
        self.store.ensure_item_fields()
        self.store.admin_add("carol", "SecretPw")
        for call in (lambda: self.store.admin_remove("nobody"),
                     lambda: self.store.admin_set_password("nobody", "x1")):
            with self.assertRaises(AccountError) as caught:
                call()
            self.assertEqual("no_such_admin", caught.exception.code)

    def test_admin_names_follow_the_player_username_rule(self):
        self.store.ensure_item_fields()
        # 管理员名同样是 JSON 的键、也要经表单往返，没理由放得更松。
        for bad in ("x", "a" * 17, "有中文", "bad name"):
            with self.assertRaises(AccountError) as caught:
                self.store.admin_add(bad, "SecretPw")
            self.assertEqual("invalid_username", caught.exception.code)
        with self.assertRaises(AccountError) as caught:
            self.store.admin_add("carol", "")
        self.assertEqual("invalid_password", caught.exception.code)

    def test_a_broken_table_locks_the_page_but_is_never_overwritten(self):
        store = self.write_raw({"schema_version": 2, "accounts": {},
                                ADMIN_ACCOUNTS_KEY: "oops"})
        report = store.ensure_item_fields()
        self.assertTrue(report["admin_broken"])
        self.assertIsNone(report["admin_created"])
        # 玩家一点感觉都没有，只是谁都登不进管理页。
        self.assertEqual([], store.admin_names())
        self.assertEqual(AUTH_NO_SUCH_USER, store.admin_verify("admin", "x"))
        # ★ 不自动修：那一格里可能还留着用户自己加的管理员。
        self.assertEqual("oops", self.saved()[ADMIN_ACCOUNTS_KEY])
        for call in (lambda: store.admin_add("carol", "SecretPw"),
                     lambda: store.admin_set_password("admin", "SecretPw"),
                     lambda: store.admin_remove("admin")):
            with self.assertRaises(AccountError) as caught:
                call()
            self.assertEqual("admin_table_broken", caught.exception.code)

    def test_admin_table_survives_an_unrelated_account_write(self):
        self.store.ensure_item_fields()
        self.store.register("alice", "pw")
        self.store.add_quest_reward("alice", money=10)
        self.assertEqual([DEFAULT_ADMIN_NAME], self.store.admin_names())


if __name__ == "__main__":
    unittest.main()
