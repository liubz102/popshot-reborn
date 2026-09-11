#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
import struct
import tempfile
import unittest

import account_store
from account_store import (ADMIN_ACCOUNTS_KEY, AUTH_BAD_PASSWORD,
                           AUTH_NO_SUCH_USER, AUTH_OK,
                           DEFAULT_ADMIN_NAME, DEFAULT_ADMIN_PASSWORD,
                           EXPERIENCE_PER_LEVEL, EXPERIENCE_STEP, LEVEL_MAX,
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
import savecrypt
from gameserver import build_gsp_rep_login


def sealed(username, fields=None, **plain):
    """造一份 V0.3.2 形状的存档：三项明文 + 一段密文。

    `fields` 是要封进密文的账号字段（可以只写几个 —— 导入是覆盖语义，缺的会回
    默认值）；`plain` 覆盖明文区（`nickname` / `password`）。
    密码默认跟着 `fields["password"]` 走，两边写法和真导出一致。
    """
    fields = dict(fields or {})
    password = plain.pop("password", fields.pop("password", ""))
    nickname = plain.pop("nickname", fields.pop("display_name", ""))
    assert not plain, plain
    return {account_store.SAVE_FORMAT_KEY: account_store.SAVE_FORMAT_VERSION,
            account_store.SAVE_NOTE_KEY: account_store.SAVE_NOTE_TEXT,
            "username": username,
            "nickname": nickname,
            "password": password,
            "data": savecrypt.seal(fields)}


def peek(payload):
    """解开一份存档的密文，拿里面的账号字段做断言用。"""
    return savecrypt.unseal(payload["data"])


def legacy_save(username, account):
    """V0.3.1 及更早那种**明文**存档的形状。现在一律被拒（D86）。"""
    return {"popshot_save": 1, "username": username, "account": dict(account)}


#: 下面这些 id 都来自真的 `shop_items.json`，不是编的。
#: `test_the_fixture_ids_still_mean_what_the_tests_assume` 守着它们的性质
#: —— 哪天物品表换代了，先炸的是那一条，而不是十几条语义不明的断言。
REVOLVER_R1 = 1120041        # 리볼버 R1，武器槽 1（part_flag 1024）
REVOLVER_R2 = 1120042        # 同一个武器槽的另一把
TOP_ARMOR = 1010015          # 上衣（part_flag 1），和武器不抢槽
STOCK_ONLY = 1510001         # ★ 只有 `[Stock-]` 的期限售卖形态，进不了背包（§11）
BRONZE_PIPE = 30018          # 청동파이프 青铜管（材料）
BLACK_BEAD = 10001           # 검은구슬 黑珠（材料）
#: ★ 可堆叠、**但不是材料**的那一档（消耗品 / 礼包 / 钥匙共 20 种）。
#:   它们靠 `add_item()` 进 `inventory`，不进 `materials` —— 「数量有没有
#:   意义」和「住哪个桶」是两件独立的事，卖出那一轮就栽在这上面。
POTION = 210001              # 회복물약 回复药水（consumable，可堆叠）
NO_SUCH_ITEM = 9999999       # 物品表里根本没有
#: 三个基础角色各一套装备槽（§46）：同一个部位、不同角色，**不抢槽**。
TYR_TOP = 1010001            # 泰尔的上衣（part_flag 1，角色 0）
KASIL_TOP = 2010001          # 卡希尔的上衣（角色 1）—— 和 TYR_TOP 同一个部位
KASIL_TOP_2 = 2010002        # 卡希尔的另一件上衣 —— 和 KASIL_TOP 抢槽
BROCK_TOP = 3010001          # 布洛克的上衣（角色 2）
KASIL_GUN = 2120041          # 卡希尔的武器槽 1（和 REVOLVER_R1 同槽、不同角色）
BROCK_GUN = 3120041          # 布洛克的武器槽 1
TYR_DASH, KASIL_DASH, BROCK_DASH = 1060002, 2060002, 3060002   # 突击技（part_flag 32）
TYR_RING, KASIL_RING, BROCK_RING = 1130059, 2130059, 3130059   # 戒指（part_flag 16384）


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

    # -- 下发的等级 = 真实等级（V0.3商店 D22）--------------------------------
    def test_reported_level_is_the_real_level(self):
        # ★ 曾经这里断言的是「1 级号下发 4」（V0.2 D120 的兼容下限）。
        #   D22 把下限删了：客户端的等级显示、原版那几道等级门、以及商店
        #   物品的「穿上」判定读的是**同一个**全局 0x72e338，抬高它就等于
        #   连商店的等级门槛一起放水。等级门改由 bshook 直接 patch。
        account = self.account()
        self.assertEqual(1, account["level"])
        self.assertEqual(1, player_level(account))
        values = struct.unpack_from("<8i", build_gsp_rep_login(account=account), 8)
        self.assertEqual(1, values[0])                     # 登录全局 0x72e338

    def test_reported_level_follows_the_curve(self):
        self.account()
        account = self.store.add_quest_reward(
            "alice", experience=experience_for_level(7))
        self.assertEqual(7, account["level"])
        self.assertEqual(7, player_level(account))

    # -- 管理页那张只读参照表（D72a）----------------------------------------
    def test_the_level_table_is_the_curve_itself_not_a_second_copy(self):
        """★ 管理页照这份表画「等级与经验」，它必须和曲线**同一个出处**。

        自己再套一遍公式（无论在 Python 还是 JS 里）迟早对不上 ——
        这条把「表里的数」和 `experience_for_level` / `level_for_experience`
        逐级对死。
        """
        from account_store import level_table, level_for_experience
        table = level_table()
        self.assertEqual(LEVEL_MAX, len(table))
        for row in table:
            level = row["level"]
            self.assertEqual(experience_for_level(level), row["total"], level)
            # 攒够这一级的累计经验，反查回来就该是这一级。
            self.assertEqual(level, level_for_experience(row["total"]), level)
            if level < LEVEL_MAX:
                # 「升到下一级要挣多少」= 两级累计经验之差，表自己要自洽。
                self.assertEqual(experience_for_level(level + 1) - row["total"],
                                 row["need"], level)
            else:
                # ★ 满级没有「下一级」。`experience_for_level(61)` 确实算得出来，
                #   但那只是 `experience_bounds()` 的除法分母，不是能挣到的一级。
                self.assertIsNone(row["need"])

    def test_the_experience_bar_still_starts_at_zero(self):
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
        # 密码在**明文区**（玩家要能改它）；账号数据整份在密文里，
        # 老格式那个 `account` 键已经没有了。
        self.assertEqual("pw", payload["password"])
        self.assertNotIn("account", payload)

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
        payload = sealed("alice", {"password": "pw", "money": 999})
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
        payload = sealed("alice", {"password": "pw", "money": 999})
        for auth in (("bob", "pw"), ("alice", "nope"), ("", "pw"), ("alice", "")):
            with self.assertRaises(AccountError, msg=repr(auth)) as ctx:
                self.store.import_account(payload, *auth)
            self.assertEqual("bad_password", ctx.exception.code, msg=repr(auth))
            self.assertIn("用户名或密码错误", ctx.exception.message)
        self.assertEqual(0, self.store.get_account("alice")[1]["money"])

    # ★★ 下面五条钉的是 `experience_for_import`（D151「等级说了算」）。
    #    **V0.3.2 之后玩家已经改不到 `level` 了** —— 它和其余游戏数据一起在密文
    #    里（D86），这些用例是直接封一份不自洽的密文来测存档层的规则，
    #    不再对应任何玩家能做的操作。别看着方法名就以为手改存档还通。
    #    真正还在替玩家干活的是
    #    `test_import_keeps_the_experience_when_the_two_fields_agree`：
    #    导出 → 原样导回，本级内攒的经验一分不许掉。
    def test_import_makes_a_hand_raised_level_stick(self):
        # 存档里的 level 是 5、经验是 0（两者矛盾）。等级是由经验推出来的派生
        # 字段，光有 level 一读回来就被打回原形 —— 导入时要把经验补到那一级。
        self.store.register("alice", "pw")
        payload = sealed("alice", {"password": "pw", "level": 5})
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
        payload = sealed("alice", {"password": "pw", "level": 1,
                                   "experience": 9999})
        self.store.import_account(payload, "alice", "pw")
        _, account = self.store.get_account("alice")
        self.assertEqual(1, account["level"])
        self.assertEqual(experience_for_level(1), account["experience"])

    def test_import_lets_the_level_go_down_too(self):
        # 旧规则只能往上抬（「想降级得连经验一起改小」）；现在降级也生效。
        self.store.register("alice", "pw")
        self.store.add_quest_reward("alice", experience=29900)
        payload = sealed("alice", {"password": "pw", "level": 3,
                                   "experience": 29900})
        self.store.import_account(payload, "alice", "pw")
        _, account = self.store.get_account("alice")
        self.assertEqual(3, account["level"])
        self.assertEqual(experience_for_level(3), account["experience"])

    def test_import_clamps_a_hand_written_level_to_the_cap(self):
        # 手写 level: 999 只能得到 60 级，不会算出一个天文数字的经验。
        self.store.register("alice", "pw")
        payload = sealed("alice", {"password": "pw", "level": 999})
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
        self.assertEqual(5, peek(payload)["level"])
        self.assertEqual(1200, peek(payload)["experience"])
        self.store.import_account(payload, "alice", "pw")
        _, account = self.store.get_account("alice")
        self.assertEqual(5, account["level"])
        self.assertEqual(1200, account["experience"])

    def test_import_survives_a_broken_level_field(self):
        # level 缺失 / 是字符串 / 是负数 -> 以经验为准，不许抛异常。
        for bad in ({}, {"level": "abc"}, {"level": -5}):
            fields = {"password": "pw", "experience": 1200}
            fields.update(bad)
            self.store.import_account(sealed("alice", fields), "alice", "pw")
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
            # ★ 这三个写的是**规范形态**：`import_account` 会当场洗一遍
            #   （上传的是人手改过的文件），洗完要和写进去的一模一样。
            "inventory": {str(REVOLVER_R1): {"count": 1, "expires": None}},
            "equipped": [REVOLVER_R1],
            "materials": {str(BRONZE_PIPE): 3},
            # 礼物盒（D76）同样写规范形态：一份完整的礼物 + 不小于最大礼物号的计数器。
            "gifts": [{"id": 3, "item": BRONZE_PIPE, "count": 2, "exp": 0,
                       "money": 0, "sender": "GM", "message": "导入测试",
                       "sent": "2026-09-10 12:00:00", "unread": True}],
            "gift_seq": 3,
        }
        self.assertEqual(sorted(changed), sorted(NEW_ACCOUNT_DEFAULTS),
                         "存档新增字段了？这条用例要跟着补")
        payload = sealed("alice", changed)
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
        payload = sealed("alice", {"password": "pw", "money": 7})
        name, action = self.store.import_account(payload, "alice", "pw")
        self.assertEqual(("alice", "replaced"), (name, action))
        _, account = self.store.get_account("alice")
        self.assertEqual(7, account["money"])
        self.assertEqual(NEW_ACCOUNT_DEFAULTS["experience"], account["experience"])
        self.assertEqual(NEW_ACCOUNT_DEFAULTS["quest_difficulty"],
                         account["quest_difficulty"])

    def test_import_ignores_unknown_fields_in_the_upload(self):
        # 存档是给人手改的，多塞几个键不能让整个导入失败，但也不能被写进存档。
        payload = sealed("carol", {"password": "pw", "money": 5, "cheat": True})
        self.store.import_account(payload)
        _, account = self.store.get_account("carol")
        self.assertNotIn("cheat", account)
        self.assertEqual(5, account["money"])

    def test_import_without_a_password_anywhere_is_refused(self):
        payload = sealed("dave", {"money": 1})     # 明文区的 password 也是空的
        with self.assertRaises(AccountError) as ctx:
            self.store.import_account(payload)
        self.assertEqual("password_required", ctx.exception.code)

    # -- V0.3 商店：仓库 / 穿着 / 材料三件套也要跟着存档走 --------------------
    def test_export_carries_the_item_fields_and_they_land_on_the_other_server(self):
        # 这三个字段是 V0.3 商店加的（D5）。导出要带上、导到另一台原样落地
        # —— 管理页和游戏里看到的仓库就是它们。
        self.store.register("alice", "pw")
        self.store.add_item("alice", REVOLVER_R1)
        self.store.set_equipped("alice", [REVOLVER_R1])
        self.store.add_materials("alice", {BRONZE_PIPE: 3})
        payload = self.store.export_account("alice")
        exported = peek(payload)
        self.assertEqual({str(REVOLVER_R1): {"count": 1, "expires": None}},
                         exported["inventory"])
        self.assertEqual([REVOLVER_R1], exported["equipped"])
        self.assertEqual({str(BRONZE_PIPE): 3}, exported["materials"])

        other = AccountStore(os.path.join(self.tmp.name, "other.json"))
        other.import_account(payload)
        _, moved = other.get_account("alice")
        for key in ("inventory", "equipped", "materials"):
            self.assertEqual(exported[key], moved[key], key)
        # 磁盘上也是这个形状，不是只在内存视图里补出来的。
        with open(other.path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)["accounts"]["alice"]
        for key in ("inventory", "equipped", "materials"):
            self.assertEqual(exported[key], on_disk[key], key)

    def test_import_cleans_dirty_item_fields(self):
        # 脏形状：`"id": 数量` 的简写、数量 0、穿着但仓库里没有的装备、
        # 客户端不认识的 id、写成字符串的数量 —— 全部当场洗干净，和
        # `ensure_item_fields` 启动时洗盘一个口径（不然脏条目要等下次启动才
        # 收敛，中间游戏里发下去的就是脏的）。
        # ★ V0.3.2 之后来源不再是记事本，但这一关还得留着：老版本服务端导出的
        #   存档洗法和现在不一样，拿着密钥自己封一份的人也进得来。
        payload = sealed("alice",
                         {"password": "pw",
                          "inventory": {str(REVOLVER_R1): 2,
                                        "999999999": 1,
                                        str(TOP_ARMOR): 0},
                          "equipped": [REVOLVER_R1, TOP_ARMOR, 999999999],
                          "materials": {str(BRONZE_PIPE): "4",
                                        "999999999": 1, "abc": 2}})
        self.store.import_account(payload)
        _, account = self.store.get_account("alice")
        self.assertEqual({str(REVOLVER_R1): {"count": 2, "expires": None}},
                         account["inventory"])
        self.assertEqual([REVOLVER_R1], account["equipped"])
        self.assertEqual({str(BRONZE_PIPE): 4}, account["materials"])

    def test_import_of_an_old_plaintext_save_is_refused(self):
        """★ V0.3.1 及更早那种明文存档一律拒（D86）。

        这条用例以前测的是「旧存档传回来仓库变空」。现在旧存档根本进不来 ——
        而「缺字段回默认值」那个语义由
        `test_import_resets_fields_missing_from_the_upload` 继续钉着。
        ★ 被拒的时候**磁盘一个字节都不许动**：`parse_save` 在锁外就失败了。
        """
        self.store.register("alice", "pw")
        self.store.add_item("alice", REVOLVER_R1)
        self.store.add_materials("alice", {BRONZE_PIPE: 3})
        old_save = legacy_save("alice", {"password": "pw", "money": 12,
                                         "level": 1, "experience": 0})
        with self.assertRaises(AccountError) as ctx:
            self.store.import_account(old_save, "alice", "pw")
        self.assertEqual("legacy_save", ctx.exception.code)
        self.assertIn("旧版", ctx.exception.message)
        self.assertIn("重新导出", ctx.exception.message)
        _, account = self.store.get_account("alice")
        self.assertEqual(0, account["money"])
        self.assertEqual({str(REVOLVER_R1): {"count": 1, "expires": None}},
                         account["inventory"])
        self.assertEqual({str(BRONZE_PIPE): 3}, account["materials"])

    def test_import_leaves_the_admin_accounts_section_alone(self):
        # V0.3 商店在 accounts.json 顶层加了 `admin_accounts`（D3）。存档转移
        # 只动 `accounts[用户名]` 这一格，顶层别的段一个字都不能碰。
        self.store.register("alice", "pw")
        self.store.ensure_item_fields()          # 键不在 ⇒ 建出默认管理员（D13）
        with open(self.path, "r", encoding="utf-8") as f:
            before = json.load(f)[ADMIN_ACCOUNTS_KEY]
        self.assertTrue(before)
        payload = sealed("bob", {"password": "pw", "money": 1})
        self.store.import_account(payload)
        with open(self.path, "r", encoding="utf-8") as f:
            after = json.load(f)
        self.assertEqual(before, after[ADMIN_ACCOUNTS_KEY])
        self.assertIn("bob", after["accounts"])

    def test_import_rejects_a_file_that_is_not_a_save(self):
        good = sealed("alice", {"password": "pw", "money": 1})
        cases = [
            (None, "bad_save"), ([], "bad_save"),
            ({"hello": "world"}, "bad_save"),
            # ★ 老代码的「退化形状」：整个文件就是账号本身。它现在必须被拒 ——
            #   放行的话就是一次伪装成导入的清档（D86）。
            ({"account": {"money": 1}}, "bad_save"),
            # 版本号必须是**整数**。`"2"` 不行；`true` 更不行 ——
            #   `isinstance(True, int)` 是 True 且 `True == 1`，不专门挡一下
            #   就会被当成「旧版明文存档」。
            (dict(good, popshot_save="2"), "bad_save"),
            (dict(good, popshot_save=True), "bad_save"),
            (dict(good, popshot_save=99), "future_save"),
            # 密文那一段的各种坏法。
            ({k: v for k, v in good.items() if k != "data"}, "bad_save_data"),
            (dict(good, data=None), "bad_save_data"),
            (dict(good, data=123), "bad_save_data"),
            (dict(good, data="不是 base64!!!"), "bad_save_data"),
            (dict(good, data=good["data"][:20]), "bad_save_data"),
            # 用户名不合规（明文区玩家能改它，所以这条门一定要在）。
            (dict(good, username="a"), "bad_save"),
            (dict(good, username="坏名字"), "bad_save"),
            # ★ 明文那两项必须是**字符串**：`"password": null` 不卡的话会被
            #   `str()` 成字符串 "None"，导入提示成功、人却登不进去了。
            (dict(good, password=None), "bad_save"),
            (dict(good, password=12345), "bad_save"),
            (dict(good, nickname=None), "bad_save"),
            (dict(good, nickname=["x"]), "bad_save"),
        ]
        for bad, want in cases:
            with self.assertRaises(AccountError, msg=repr(want)) as ctx:
                self.store.import_account(bad)
            self.assertEqual(want, ctx.exception.code, msg=repr(want))

    # -- V0.3.2：只有用户名 / 昵称 / 密码明文，其余加密（D86）------------------
    def test_export_keeps_only_the_three_fields_in_the_clear(self):
        """★ 需求本身的直接判据：导出的文件里除了那三项什么都看不见。"""
        self.store.register("alice", "pw")
        self.store.add_quest_reward("alice", experience=1200, money=987654)
        self.store.add_item("alice", REVOLVER_R1)
        payload = self.store.export_account("alice")
        self.assertEqual(
            {account_store.SAVE_FORMAT_KEY, account_store.SAVE_NOTE_KEY,
             "username", "nickname", "password", "data"},
            set(payload))
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("987654", text)             # 金币
        self.assertNotIn(str(REVOLVER_R1), text)     # 仓库里那件东西
        self.assertNotIn("1200", text)               # 经验
        # 密文里确实是那些字段（不是「导出的时候就丢了」）。
        self.assertEqual(987654, peek(payload)["money"])

    def test_the_plain_and_secret_key_sets_cover_every_account_field(self):
        """★ 加字段的守卫：新字段必须落进明文或密文其中一边，不能两边都没有。

        `SAVE_SECRET_KEYS` 是从 `NEW_ACCOUNT_DEFAULTS` 反推的，所以这条本来就
        自动成立 —— 它钉的是「以后别有人手写成一张固定表」。
        """
        plain = set(account_store.SAVE_PLAIN_KEYS)
        secret = set(account_store.SAVE_SECRET_KEYS)
        self.assertEqual(set(NEW_ACCOUNT_DEFAULTS), plain | secret)
        self.assertEqual(set(), plain & secret)

    def test_a_player_can_rename_the_account_by_editing_the_plain_username(self):
        """明文可改的**正面**判据：改个名字导入 = 在目标服上建一个新号。"""
        self.store.register("alice", "pw")
        self.store.add_quest_reward("alice", experience=1200, money=77)
        self.store.add_item("alice", REVOLVER_R1)
        payload = self.store.export_account("alice")
        payload["username"] = "alice2"
        other = AccountStore(os.path.join(self.tmp.name, "other.json"))
        self.assertEqual(("alice2", "created"), other.import_account(payload))
        _, moved = other.get_account("alice2")
        self.assertEqual((1200, 77), (moved["experience"], moved["money"]))
        self.assertEqual({str(REVOLVER_R1): {"count": 1, "expires": None}},
                         moved["inventory"])

    def test_a_player_can_change_the_nickname_and_password_in_the_clear(self):
        self.store.register("alice", "pw", display_name="老名字")
        payload = self.store.export_account("alice")
        self.assertEqual("老名字", payload["nickname"])
        payload["nickname"] = "新名字"
        payload["password"] = "newpw"
        # ★ 鉴权用的仍然是**服务器上现有的**密码，文件里那个是「改成什么」。
        self.store.import_account(payload, "alice", "pw")
        _, account = self.store.get_account("alice")
        self.assertEqual("新名字", account["display_name"])
        self.assertEqual(AUTH_OK, self.store.verify("alice", "newpw")[0])
        self.assertEqual(AUTH_BAD_PASSWORD, self.store.verify("alice", "pw")[0])

    def test_a_bad_nickname_in_the_clear_is_refused(self):
        """★ 昵称是我们**主动请玩家去改**的一格，就得在门口挡住。

        控制字符会把包和 JSON 弄坏，补充平面字符会让 `w_wstr` 的长度字段少算。
        """
        self.store.register("alice", "pw")
        payload = self.store.export_account("alice")
        for bad in ("a\x00b", "x" * 100, "\U0001F600"):
            with self.assertRaises(AccountError, msg=repr(bad)) as ctx:
                self.store.import_account(dict(payload, nickname=bad),
                                          "alice", "pw")
            self.assertEqual("invalid_nickname", ctx.exception.code, repr(bad))
        self.assertEqual("alice", self.store.get_account("alice")[1]["display_name"])

    def test_editing_the_ciphertext_is_refused_and_nothing_is_written(self):
        """改一个字符就进不来，**而且磁盘一个字节都没动**。

        ★ 前提是密码填对的 —— 否则证明不了是解密先失败，可能只是被鉴权挡下。
        """
        self.store.register("alice", "pw")
        self.store.add_quest_reward("alice", experience=1200, money=77)
        payload = self.store.export_account("alice")
        with open(self.path, "r", encoding="utf-8") as f:
            before = f.read()
        blob = payload["data"]
        for spoiled in (blob[:-6] + ("A" if blob[-6] != "A" else "B") + blob[-5:],
                        blob[:30] + ("A" if blob[30] != "A" else "B") + blob[31:]):
            with self.assertRaises(AccountError) as ctx:
                self.store.import_account(dict(payload, data=spoiled),
                                          "alice", "pw")
            self.assertEqual("save_tampered", ctx.exception.code)
            self.assertIn("data", ctx.exception.message)
        with open(self.path, "r", encoding="utf-8") as f:
            self.assertEqual(before, f.read())

    def test_a_handwritten_save_can_no_longer_create_an_account(self):
        """以前手写一份 JSON 就能凭空建一个满级号（新建路径完全免鉴权）。"""
        for bad in ({"username": "hacker", "account": {"password": "p",
                                                       "money": 999999}},
                    {"popshot_save": 2, "username": "hacker",
                     "password": "p", "money": 999999}):
            with self.assertRaises(AccountError):
                self.store.import_account(bad)
        self.assertFalse(self.store.has_account("hacker"))

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
        # ★ 三个角色各一套槽（§46）：下面几组是「同一个部位、不同角色」。
        for tyr, kasil, brock in ((TYR_TOP, KASIL_TOP, BROCK_TOP),
                                  (REVOLVER_R1, KASIL_GUN, BROCK_GUN),
                                  (TYR_DASH, KASIL_DASH, BROCK_DASH),
                                  (TYR_RING, KASIL_RING, BROCK_RING)):
            trio = (tyr, kasil, brock)
            self.assertEqual([0, 1, 2], [shopdata.character_of(i) for i in trio])
            self.assertEqual({shopdata.part_flag(tyr)},
                             {shopdata.part_flag(i) for i in trio})
            self.assertTrue(all(shopdata.ownable(i) for i in trio))
            self.assertFalse(shopdata.conflicts(tyr, kasil))
        self.assertTrue(shopdata.conflicts(KASIL_TOP, KASIL_TOP_2))
        # 只有货架条目的塞进背包，客户端查不到定义，仓库里是空格子。
        self.assertFalse(shopdata.ownable(STOCK_ONLY))
        self.assertFalse(shopdata.exists(NO_SUCH_ITEM))
        for material in (BRONZE_PIPE, BLACK_BEAD):
            self.assertTrue(shopdata.is_material(material))
            self.assertTrue(shopdata.ownable(material))
        # ★ `POTION` 的全部价值就在这三条上：**可堆叠**、**不是材料**、
        #   **进得了背包** ⇒ 它必然住在 `inventory` 而不是 `materials`。
        #   哪天物品表把它改成材料了，卖出那几条用例就失去了意义，
        #   让这一条先炸。
        self.assertTrue(shopdata.ownable(POTION))
        self.assertTrue(shopdata.stackable(POTION))
        self.assertFalse(shopdata.is_material(POTION))

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

    def test_one_character_dressing_does_not_undress_the_other_two(self):
        """★★ 2026-09-09 实机：泰尔穿上铠甲，卡希尔和布洛克的铠甲被「顶掉」了。

        槽位是**每个角色一套**（§46 / D54）—— 铠甲、武器、突击技、戒指都一样。
        """
        gear = [TYR_TOP, KASIL_TOP, BROCK_TOP,
                REVOLVER_R1, KASIL_GUN, BROCK_GUN,
                TYR_DASH, KASIL_DASH, BROCK_DASH,
                TYR_RING, KASIL_RING, BROCK_RING]
        for item_id in gear:
            self.store.add_item("alice", item_id)
        for item_id in gear:
            account, dropped = self.store.equip_item("alice", item_id)
            self.assertEqual([], dropped, item_id)
        self.assertEqual(set(gear), set(equipped_items(account)))

    def test_swapping_within_one_character_leaves_the_others_dressed(self):
        for item_id in (TYR_TOP, KASIL_TOP, KASIL_TOP_2, BROCK_TOP):
            self.store.add_item("alice", item_id)
        for item_id in (TYR_TOP, KASIL_TOP, BROCK_TOP):
            self.store.equip_item("alice", item_id)
        account, dropped = self.store.equip_item("alice", KASIL_TOP_2)
        self.assertEqual([KASIL_TOP], dropped)
        self.assertEqual({TYR_TOP, KASIL_TOP_2, BROCK_TOP},
                         set(equipped_items(account)))

    def test_a_hand_edited_save_keeps_every_characters_gear(self):
        # 读的时候就地收敛那一步也得按角色分槽 —— 不然开服一次
        # （`ensure_item_fields`）就把两个角色脱光，还写回磁盘。
        account = {"inventory": {str(TYR_TOP): 1, str(KASIL_TOP): 1,
                                 str(BROCK_TOP): 1},
                   "equipped": [TYR_TOP, KASIL_TOP, BROCK_TOP]}
        self.assertEqual([TYR_TOP, KASIL_TOP, BROCK_TOP],
                         equipped_items(account))

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

    # ------------------------------------------------------------ 合成（M7）
    def stocked(self, money=1000, materials=None):
        """给 alice 铺一份「合得起」的家底。"""
        self.store.add_quest_reward("alice", money=money)
        self.store.add_materials("alice",
                                 materials or {BRONZE_PIPE: 3, BLACK_BEAD: 2})

    def test_compose_deducts_money_and_materials_and_adds_the_item(self):
        self.stocked()
        account = self.store.compose_item(
            "alice", TOP_ARMOR, 400, {BRONZE_PIPE: 2, BLACK_BEAD: 1})
        self.assertEqual(600, player_money(account))
        self.assertEqual({BRONZE_PIPE: 1, BLACK_BEAD: 1},
                         material_counts(account))
        self.assertEqual({TOP_ARMOR: 1}, {k: v["count"] for k, v
                                          in inventory_items(account).items()})
        # 落盘了，不只是内存里的那份。
        saved = self.saved()["accounts"]["alice"]
        self.assertEqual(600, saved["money"])
        self.assertIn(str(TOP_ARMOR), saved["inventory"])

    def test_compose_is_atomic_when_money_is_short(self):
        """★★ 铁律 11：崩在中间老数据还得在。金币不够时**一个字节都不写**
        —— 拿 `spend_money` + `consume_materials` + `add_item` 拼出来的话，
        材料已经没了才发现钱不够。"""
        self.stocked(money=100)
        before = self.raw_bytes()
        with self.assertRaises(AccountError) as caught:
            self.store.compose_item("alice", TOP_ARMOR, 400, {BRONZE_PIPE: 2})
        self.assertEqual("not_enough_money", caught.exception.code)
        self.assertEqual(before, self.raw_bytes())

    def test_compose_is_atomic_when_materials_are_short(self):
        self.stocked()
        before = self.raw_bytes()
        with self.assertRaises(AccountError) as caught:
            self.store.compose_item("alice", TOP_ARMOR, 400, {BRONZE_PIPE: 9})
        self.assertEqual("not_enough_materials", caught.exception.code)
        self.assertEqual(before, self.raw_bytes())

    def test_compose_refuses_when_already_owned(self):
        """原版失败文案 `이미 소지하고 있습니다`（§7）。★ 这道闸在锁里，
        `shop.check_compose` 那一轮只是为了挑错误码。"""
        self.stocked()
        self.store.add_item("alice", TOP_ARMOR)
        before = self.raw_bytes()
        with self.assertRaises(AccountError) as caught:
            self.store.compose_item("alice", TOP_ARMOR, 400, {BRONZE_PIPE: 2})
        self.assertEqual("already_owned", caught.exception.code)
        self.assertEqual(before, self.raw_bytes())

    def test_compose_rejects_ids_the_client_does_not_know(self):
        self.stocked()
        for bad in (STOCK_ONLY, NO_SUCH_ITEM):
            with self.assertRaises(AccountError) as caught:
                self.store.compose_item("alice", bad, 0, {})
            self.assertEqual("unknown_item", caught.exception.code)

    def test_compose_deletes_a_material_slot_when_used_up(self):
        self.stocked(materials={BRONZE_PIPE: 2})
        self.store.compose_item("alice", TOP_ARMOR, 0, {BRONZE_PIPE: 2})
        self.assertNotIn(str(BRONZE_PIPE),
                         self.saved()["accounts"]["alice"]["materials"])

    def test_compose_without_cost_or_materials_still_works(self):
        # 管理页把花费和材料都清成 0 是合法配置，别让它抛。
        account = self.store.compose_item("alice", TOP_ARMOR, 0, {})
        self.assertIn(TOP_ARMOR, inventory_items(account))

    def test_compose_rejects_a_negative_cost(self):
        with self.assertRaises(AccountError) as caught:
            self.store.compose_item("alice", TOP_ARMOR, -1, {})
        self.assertEqual("invalid_amount", caught.exception.code)

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
                  "quest_difficulty": {"3": 2}, "quest_unlock_all": False}
        store = self.write_raw({"schema_version": 2,
                                "accounts": {"old": dict(before)}})
        store.ensure_item_fields()
        after = self.saved()["accounts"]["old"]
        for key, value in before.items():
            self.assertEqual(value, after[key], key)

    # ------------------------------------------- D51：旧的商城角色键搬进仓库
    def test_ensure_item_fields_turns_a_legacy_owned_characters_list_into_cards(self):
        """D51 之前手写的 `owned_characters` 是玩家真持有的角色，不能丢（铁律 11）
        —— 转成仓库里的角色卡；转完两个旧键都删掉。"""
        store = self.write_raw({"schema_version": 2, "accounts": {"old": {
            "password": "pw", "character_unlock_all": False,
            "owned_characters": [100, 104, "x", 7, 104],
            "inventory": {str(REVOLVER_R1): {"count": 1, "expires": None}}}}})
        report = store.ensure_item_fields()
        after = self.saved()["accounts"]["old"]
        self.assertEqual({str(REVOLVER_R1): {"count": 1, "expires": None},
                          "101400001": {"count": 1, "expires": None},
                          "105400001": {"count": 1, "expires": None}},
                         after["inventory"])
        self.assertNotIn("owned_characters", after)
        self.assertNotIn("character_unlock_all", after)
        notes = " ".join(report["accounts"][0]["notes"])
        self.assertIn("101400001", notes)
        self.assertIn("owned_characters", notes)
        # 第二遍什么都不改、文件一个字节不动（幂等，D5）。
        before = self.raw_bytes()
        self.assertEqual([], store.ensure_item_fields()["accounts"])
        self.assertEqual(before, self.raw_bytes())

    def test_ensure_item_fields_drops_the_dead_unlock_all_flag_without_granting_anything(self):
        # 「全开」是服务端白送的，不算持有：开关删掉，仓库还是空的。
        store = self.write_raw({"schema_version": 2, "accounts": {"old": {
            "password": "pw", "character_unlock_all": True, "owned_characters": []}}})
        store.ensure_item_fields()
        after = self.saved()["accounts"]["old"]
        self.assertEqual({}, after["inventory"])
        self.assertNotIn("character_unlock_all", after)
        self.assertNotIn("owned_characters", after)
        self.assertEqual([], account_store.owned_characters(after))

    def test_a_legacy_card_already_in_the_warehouse_is_not_doubled(self):
        store = self.write_raw({"schema_version": 2, "accounts": {"old": {
            "password": "pw", "owned_characters": [100],
            "inventory": {"101400001": {"count": 1, "expires": None}}}}})
        store.ensure_item_fields()
        after = self.saved()["accounts"]["old"]
        self.assertEqual({"101400001": {"count": 1, "expires": None}},
                         after["inventory"])

    # ★ 这里原来有一条 `test_import_converts_a_legacy_owned_characters_list`
    #   （旧版存档里的 `owned_characters` 经导入转成角色卡）。V0.3.2 起走不到了：
    #   v1 明文存档整个被拒，`parse_save` 也不再放行那个旧键（D86）。
    #   线上盘里的旧键仍由启动时的 `ensure_item_fields()` 负责 ——
    #   上面那几条 `test_a_legacy_*` 钉的就是它，那才是真正在管事的路。

    def test_a_character_card_in_the_warehouse_is_the_character(self):
        # D51：`owned_characters()` 只看仓库；`player_character()` 没卡退回 0。
        self.assertEqual(101, account_store.character_id_of_item(102400001))
        self.assertIsNone(account_store.character_id_of_item(101900001))
        self.assertIsNone(account_store.character_id_of_item(REVOLVER_R1))
        self.assertIsNone(account_store.character_id_of_item("abc"))
        self.store.add_item("alice", 102400001)
        _, account = self.store.get_account("alice")
        self.assertEqual([101], account_store.owned_characters(account))
        self.assertEqual([102400001], account_store.character_item_ids(account))
        self.assertEqual(101, player_character(self.store.set_character("alice", 101)))
        self.assertEqual(0, player_character(self.store.set_character("alice", 110)))
        # 边界：角色 99（랜덤）和 111 都不是那 11 个；最后一张是 110。
        self.assertIsNone(account_store.character_id_of_item(100400001))
        self.assertIsNone(account_store.character_id_of_item(112400001))
        self.assertEqual(110, account_store.character_id_of_item(111400001))

    def test_a_character_card_cannot_be_worn(self):
        # 角色卡 `part_flag == 0`：留在 equipped 里会让 0x030b 发两遍、0x0604 把卡
        # 当衣服发。`resolve_equipped` 把不占槽的一律丢掉（D51）。
        self.store.add_item("alice", 102400001)
        account, dropped = self.store.set_equipped("alice", [102400001, REVOLVER_R1])
        self.assertEqual([], equipped_items(account))
        self.assertIn(102400001, dropped)
        self.store.add_item("alice", REVOLVER_R1)
        account, dropped = self.store.equip_item("alice", 102400001)
        self.assertEqual([], equipped_items(account))
        self.assertEqual([102400001], dropped)
        # 手改存档把卡写进 equipped，开服洗掉。
        store = self.write_raw({"schema_version": 2, "accounts": {"old": {
            "password": "pw",
            "inventory": {"102400001": {"count": 1, "expires": None}},
            "equipped": [102400001]}}})
        store.ensure_item_fields()
        self.assertEqual([], self.saved()["accounts"]["old"]["equipped"])

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
        payload = sealed("carol", {
            "password": "pw",
            "inventory": {str(REVOLVER_R1): 1, str(NO_SUCH_ITEM): 1},
            "equipped": [NO_SUCH_ITEM, REVOLVER_R1],
            "materials": {str(BRONZE_PIPE): 4},
        })
        self.store.import_account(payload)
        _, account = self.store.get_account("carol")
        self.assertEqual([REVOLVER_R1], owned_item_ids(account))
        self.assertEqual([REVOLVER_R1], equipped_items(account))
        self.assertEqual({BRONZE_PIPE: 4}, material_counts(account))
        # 脏条目不该落到磁盘上（来源见 `test_import_cleans_dirty_item_fields`）。
        self.assertNotIn(str(NO_SUCH_ITEM),
                         self.saved()["accounts"]["carol"]["inventory"])

    def test_export_carries_the_item_fields(self):
        self.store.add_item("alice", REVOLVER_R1)
        self.store.equip_item("alice", REVOLVER_R1)
        self.store.add_materials("alice", {BRONZE_PIPE: 2})
        payload = peek(self.store.export_account("alice"))
        self.assertIn(str(REVOLVER_R1), payload["inventory"])
        self.assertEqual([REVOLVER_R1], payload["equipped"])
        self.assertEqual({str(BRONZE_PIPE): 2}, payload["materials"])


class GiftBoxTests(unittest.TestCase):
    """礼物盒（V0.3商店 D76）：管理页批量发的奖励先落在 `gifts` 里，玩家在游戏里
    领了才进仓库 / 加经验金币。守的是：礼物号永不复用、领取是一把锁里的原子
    交易、装备已拥有不放第二件、老存档幂等补齐。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "accounts.json")
        self.store = AccountStore(self.path)
        self.store.register("alice", "pw")

    def gifts(self):
        return account_store.pending_gifts(self.store.get_account("alice")[1])

    def test_a_new_account_has_an_empty_gift_box(self):
        account = self.store.get_account("alice")[1]
        self.assertEqual([], account["gifts"])
        self.assertEqual(0, account["gift_seq"])
        self.assertEqual([], account_store.pending_gifts(account))

    def test_add_gifts_gives_each_reward_its_own_number(self):
        _account, created = self.store.add_gifts(
            "alice", [{"item": BRONZE_PIPE, "count": 3}, {"item": TOP_ARMOR, "count": 5},
                      {"exp": 500}, {"money": 3000}], message="测试")
        self.assertEqual([1, 2, 3, 4], [gift["id"] for gift in created])
        self.assertEqual([1, 2, 3, 4], [gift["id"] for gift in self.gifts()])
        by_id = {gift["id"]: gift for gift in created}
        self.assertEqual((BRONZE_PIPE, 3, 0, 0),
                         (by_id[1]["item"], by_id[1]["count"], by_id[1]["exp"], by_id[1]["money"]))
        # ★ 装备类数量没有意义（§28）：写 5 也只存 1。
        self.assertEqual(1, by_id[2]["count"])
        self.assertEqual((0, 500, 0), (by_id[3]["item"], by_id[3]["exp"], by_id[3]["money"]))
        self.assertEqual((0, 0, 3000), (by_id[4]["item"], by_id[4]["exp"], by_id[4]["money"]))
        for gift in created:
            self.assertEqual("GM", gift["sender"])
            self.assertEqual("测试", gift["message"])
            self.assertTrue(gift["unread"])
            self.assertRegex(gift["sent"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_gift_numbers_are_never_reused(self):
        self.store.add_gifts("alice", [{"exp": 1}, {"exp": 2}])
        self.store.discard_gift("alice", 2)
        self.store.claim_gift("alice", 1)
        self.assertEqual([], self.gifts())
        _account, created = self.store.add_gifts("alice", [{"exp": 3}])
        # 两份都没了，新的还是 3 号 —— 客户端手里可能还捏着旧清单。
        self.assertEqual(3, created[0]["id"])

    def test_add_gifts_refuses_bad_specs_and_writes_nothing(self):
        for bad in ({"item": NO_SUCH_ITEM}, {"item": STOCK_ONLY},
                    {"item": BRONZE_PIPE, "count": 0}, {"exp": 0}, {}, {"money": -5}):
            with self.assertRaises(AccountError, msg=bad):
                self.store.add_gifts("alice", [{"exp": 10}, bad])
        self.assertEqual([], self.gifts(), "有一条坏的就一份都不写")

    def test_claiming_a_material_gift_lands_in_the_material_table(self):
        self.store.add_gifts("alice", [{"item": BRONZE_PIPE, "count": 3}])
        account, gift, note = self.store.claim_gift("alice", 1)
        self.assertEqual(BRONZE_PIPE, gift["item"])
        self.assertEqual({BRONZE_PIPE: 3}, material_counts(account))
        self.assertEqual([], account_store.pending_gifts(account))
        self.assertIn("材料", note)

    def test_claiming_an_equipment_gift_lands_in_the_warehouse_once(self):
        self.store.add_gifts("alice", [{"item": TOP_ARMOR}, {"item": TOP_ARMOR}])
        account, _gift, _note = self.store.claim_gift("alice", 1)
        self.assertEqual({TOP_ARMOR: {"count": 1, "expires": None}},
                         inventory_items(account))
        # 第二份照样算领掉，但仓库里不会出现 ×2（客户端根本不读那个数）。
        account, _gift, note = self.store.claim_gift("alice", 2)
        self.assertEqual(1, inventory_items(account)[TOP_ARMOR]["count"])
        self.assertIn("已经拥有", note)
        self.assertEqual([], account_store.pending_gifts(account))

    def test_claiming_exp_and_money_gifts_credits_the_account(self):
        self.store.add_gifts("alice", [{"exp": 500}, {"money": 3000}])
        account, _gift, _note = self.store.claim_gift("alice", 1)
        self.assertEqual(500, account["experience"])
        self.assertEqual(level_for_experience(500), account["level"])
        account, _gift, _note = self.store.claim_gift("alice", 2)
        self.assertEqual(3000, player_money(account))
        self.assertEqual({}, inventory_items(account), "凭证不进仓库")

    def test_open_marks_the_gift_read_and_discard_removes_it(self):
        self.store.add_gifts("alice", [{"exp": 5}])
        account, gift = self.store.open_gift("alice", 1)
        self.assertFalse(gift["unread"])
        self.assertFalse(account_store.pending_gifts(account)[0]["unread"])
        account, gift = self.store.discard_gift("alice", 1)
        self.assertEqual(5, gift["exp"])
        self.assertEqual([], account_store.pending_gifts(account))
        self.assertEqual(0, account["experience"], "丢弃不发钱")

    def test_actions_on_a_missing_gift_raise_and_write_nothing(self):
        self.store.add_gifts("alice", [{"exp": 5}])
        with open(self.path, "rb") as f:
            before = f.read()
        for action in (self.store.open_gift, self.store.claim_gift, self.store.discard_gift):
            with self.assertRaises(AccountError):
                action("alice", 99)
        with open(self.path, "rb") as f:
            self.assertEqual(before, f.read())

    def test_ensure_item_fields_backfills_and_stays_idempotent(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 2,
                       "accounts": {"old": {"password": "pw"},
                                    "dirty": {"password": "pw", "gift_seq": 1,
                                              "gifts": [
                                                  {"id": 7, "exp": 5},      # 号比计数器大
                                                  {"id": 8, "item": NO_SUCH_ITEM},
                                                  "垃圾", {"id": 0, "exp": 1}]}}},
                      f, ensure_ascii=False)
        store = AccountStore(self.path)
        report = store.ensure_item_fields()
        notes = {row["username"]: row["notes"] for row in report["accounts"]}
        self.assertIn("补上 gifts", notes["old"])
        self.assertIn("补上 gift_seq", notes["old"])
        self.assertIn("丢掉礼物盒里坏掉的 3 份礼物", notes["dirty"])
        with open(self.path, "r", encoding="utf-8") as f:
            saved = json.load(f)["accounts"]
        self.assertEqual([], saved["old"]["gifts"])
        self.assertEqual(0, saved["old"]["gift_seq"])
        self.assertEqual([7], [gift["id"] for gift in saved["dirty"]["gifts"]])
        self.assertEqual(7, saved["dirty"]["gift_seq"], "计数器不小于最大礼物号")
        with open(self.path, "rb") as f:
            before = f.read()
        self.assertEqual([], store.ensure_item_fields()["accounts"])
        with open(self.path, "rb") as f:
            self.assertEqual(before, f.read(), "第二遍一个字节都不写")


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

    def test_a_player_can_be_taken_in_as_an_operator(self):
        """`admin_add_from_player`：用户名和明文密码原样照搬（D40）。"""
        self.store.register("alice", "pw1")
        self.assertEqual(["alice"], self.store.admin_add_from_player("alice"))
        self.assertEqual("operator", self.store.admin_role("alice"))
        self.assertEqual(AUTH_OK, self.store.admin_verify("alice", "pw1"))
        # 玩家那一边一个字节都没动。
        self.assertEqual(AUTH_OK, self.store.verify("alice", "pw1")[0])

    def test_taking_in_a_player_twice_or_a_stranger_is_refused(self):
        self.store.register("alice", "pw1")
        self.store.admin_add_from_player("alice")
        with self.assertRaises(AccountError):
            self.store.admin_add_from_player("alice")
        with self.assertRaises(AccountError):
            self.store.admin_add_from_player("nobody")
        # 两次都失败了 ⇒ 表里还是只有那一个。
        self.assertEqual(["alice"], self.store.admin_names())

    def test_add_and_remove(self):
        self.store.ensure_item_fields()
        self.assertEqual([DEFAULT_ADMIN_NAME, "carol"],
                         self.store.admin_add("carol", "SecretPw"))
        self.assertEqual(AUTH_OK, self.store.admin_verify("carol", "SecretPw"))
        # ★ 不传 role = 系统管理员（和「老存档里没有 role 这个键」同一个
        #   口径，D34）—— 所以下一句删掉默认管理员才允许。
        self.assertEqual({"password": "SecretPw", "role": "system"},
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


class AdminRoleTests(unittest.TestCase):
    """管理员权限两档：系统管理员 / 运营（D34，用户 2026-09-06 拍板）。"""

    SYSTEM = account_store.ADMIN_ROLE_SYSTEM
    OPERATOR = account_store.ADMIN_ROLE_OPERATOR

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "accounts.json")
        self.store = AccountStore(self.path)
        self.store.ensure_item_fields()

    def saved(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_a_record_without_a_role_is_a_system_admin(self):
        """★★ 老存档里一个 `role` 都没有 —— 当成运营的话，第一件事就是
        **没人进得去「管理员账号」页**，连改回来的入口都没有了。"""
        self.assertEqual(self.SYSTEM, account_store.admin_role_of({}))
        self.assertEqual(self.SYSTEM,
                         account_store.admin_role_of({"password": "x"}))

    def test_a_role_we_do_not_recognise_is_an_operator(self):
        """★ 最小权限：`"sysadmin"` 这种手滑要是当成系统管理员，
        一个拼写错误就等于全放行。"""
        for bad in ("sysadmin", "admin", "", None, 7, []):
            self.assertEqual(self.OPERATOR,
                             account_store.admin_role_of({"role": bad}), bad)
        # 大小写和空格是手滑，不是另一种权限。
        self.assertEqual(self.SYSTEM,
                         account_store.admin_role_of({"role": " System "}))

    def test_add_records_the_role_and_list_reports_it(self):
        self.store.admin_add("carol", "SecretPw", self.OPERATOR)
        self.assertEqual({"password": "SecretPw", "role": self.OPERATOR},
                         self.saved()[ADMIN_ACCOUNTS_KEY]["carol"])
        self.assertEqual([{"name": DEFAULT_ADMIN_NAME, "role": self.SYSTEM},
                          {"name": "carol", "role": self.OPERATOR}],
                         self.store.admin_list())
        self.assertEqual(self.OPERATOR, self.store.admin_role("carol"))
        self.assertIsNone(self.store.admin_role("nobody"))

    def test_add_refuses_a_role_it_does_not_know(self):
        with self.assertRaises(AccountError) as caught:
            self.store.admin_add("carol", "SecretPw", "boss")
        self.assertEqual("bad_role", caught.exception.code)
        self.assertEqual([DEFAULT_ADMIN_NAME], self.store.admin_names())

    def test_set_role_promotes_and_demotes(self):
        self.store.admin_add("carol", "SecretPw", self.OPERATOR)
        self.assertEqual(self.SYSTEM,
                         self.store.admin_set_role("carol", self.SYSTEM))
        self.assertEqual(self.SYSTEM, self.store.admin_role("carol"))
        self.assertEqual(self.OPERATOR,
                         self.store.admin_set_role("carol", self.OPERATOR))
        self.assertEqual(self.OPERATOR, self.store.admin_role("carol"))

    def test_set_role_rejects_unknown_names_and_roles(self):
        with self.assertRaises(AccountError) as caught:
            self.store.admin_set_role("nobody", self.SYSTEM)
        self.assertEqual("no_such_admin", caught.exception.code)
        with self.assertRaises(AccountError) as caught:
            self.store.admin_set_role(DEFAULT_ADMIN_NAME, "boss")
        self.assertEqual("bad_role", caught.exception.code)

    def test_the_last_system_admin_cannot_be_demoted(self):
        """★ 和「不能删掉最后一个」是同一条不变式：运营看不到这一页，
        降完就没人能改回来了。"""
        self.store.admin_add("carol", "SecretPw", self.OPERATOR)
        with self.assertRaises(AccountError) as caught:
            self.store.admin_set_role(DEFAULT_ADMIN_NAME, self.OPERATOR)
        self.assertEqual("last_system_admin", caught.exception.code)
        self.assertEqual(self.SYSTEM, self.store.admin_role(DEFAULT_ADMIN_NAME))
        # 有了第二个系统管理员之后就放行。
        self.store.admin_set_role("carol", self.SYSTEM)
        self.store.admin_set_role(DEFAULT_ADMIN_NAME, self.OPERATOR)
        self.assertEqual(self.OPERATOR,
                         self.store.admin_role(DEFAULT_ADMIN_NAME))

    def test_operators_do_not_count_towards_the_last_system_admin(self):
        """★★ 用户 2026-09-06 明说的：「运营权限的人排除在数量统计外」。

        一屋子运营 + 一个系统管理员，那个系统管理员照样删不掉。
        """
        self.store.admin_add("carol", "SecretPw", self.OPERATOR)
        self.store.admin_add("dave", "SecretPw", self.OPERATOR)
        with self.assertRaises(AccountError) as caught:
            self.store.admin_remove(DEFAULT_ADMIN_NAME)
        self.assertEqual("last_admin", caught.exception.code)
        # 运营随便删，几个都行。
        self.assertEqual([DEFAULT_ADMIN_NAME, "dave"],
                         self.store.admin_remove("carol"))
        self.assertEqual([DEFAULT_ADMIN_NAME], self.store.admin_remove("dave"))

    def test_a_second_system_admin_makes_the_first_removable(self):
        self.store.admin_add("carol", "SecretPw", self.SYSTEM)
        self.assertEqual(["carol"], self.store.admin_remove(DEFAULT_ADMIN_NAME))

    def test_the_default_admin_is_created_as_a_system_admin(self):
        # 新装的服务器第一次跑起来就得有人能进「管理员账号」页。
        self.assertEqual(self.SYSTEM,
                         self.saved()[ADMIN_ACCOUNTS_KEY][DEFAULT_ADMIN_NAME]
                         ["role"])


class SellItemsTests(unittest.TestCase):
    """`sell_items()` —— 一把锁里 扣物品 + 脱装备 + 加返还材料 + 加金币。

    ★ 这一组钉的是**交易性**（要么全成、要么一个字节都不写），定价那一半
    在 `test_sellprice` 里；两边分开是因为它们会各自被改坏。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "accounts.json")
        self.store = AccountStore(self.path)
        self.store.register("alice", "pw")
        self.store.admin_update_account(
            "alice", money=1000,
            inventory={REVOLVER_R1: 1, TOP_ARMOR: 1},
            materials={BLACK_BEAD: 5})

    def tearDown(self):
        self.tmp.cleanup()

    def raw_bytes(self):
        with open(self.path, "rb") as f:
            return f.read()

    def account(self):
        return self.store.get_account("alice")[1]

    def line(self, item_id, count=1, money=0, materials=None):
        return {"id": item_id, "count": count, "money": money,
                "materials": materials or {}}

    def test_money_and_items_move_together(self):
        _account, receipt = self.store.sell_items(
            "alice", [self.line(BLACK_BEAD, 2, 300)])
        self.assertEqual(1000, receipt["money_before"])
        self.assertEqual(1300, receipt["money_after"])
        self.assertEqual(300, receipt["gained"])
        account = self.account()
        self.assertEqual(3, account_store.material_count(account, BLACK_BEAD))
        self.assertEqual(1300, account_store.player_money(account))

    def test_a_material_used_up_loses_its_slot(self):
        self.store.sell_items("alice", [self.line(BLACK_BEAD, 5, 1)])
        self.assertNotIn(str(BLACK_BEAD), self.account()["materials"])

    def test_equipment_is_removed_outright(self):
        self.store.sell_items("alice", [self.line(TOP_ARMOR, 1, 40)])
        self.assertFalse(account_store.has_item(self.account(), TOP_ARMOR))

    def test_returned_materials_land_in_the_bucket(self):
        _account, receipt = self.store.sell_items(
            "alice", [self.line(REVOLVER_R1, 1, 50,
                                {BLACK_BEAD: 3, BRONZE_PIPE: 2})])
        self.assertEqual({BLACK_BEAD: 3, BRONZE_PIPE: 2}, receipt["returned"])
        account = self.account()
        # 原来就有 5 颗黑珠 —— 累加，不是覆盖。
        self.assertEqual(8, account_store.material_count(account, BLACK_BEAD))
        self.assertEqual(2, account_store.material_count(account, BRONZE_PIPE))

    def test_selling_something_worn_takes_it_off(self):
        """★ 「穿着的每一件都在仓库里」是 `set_equipped()` 的不变式。
        破了它以后每一次换装都会把这一件悄悄丢掉，查起来莫名其妙。"""
        self.store.equip_item("alice", TOP_ARMOR)
        self.store.equip_item("alice", REVOLVER_R1)
        _account, receipt = self.store.sell_items(
            "alice", [self.line(TOP_ARMOR, 1, 40)])
        self.assertEqual([TOP_ARMOR], receipt["unequipped"])
        self.assertEqual([REVOLVER_R1],
                         list(account_store.equipped_items(self.account())))

    def test_not_enough_writes_nothing(self):
        before = self.raw_bytes()
        with self.assertRaises(account_store.AccountError) as caught:
            self.store.sell_items("alice", [self.line(BLACK_BEAD, 6, 600)])
        self.assertEqual("not_enough_items", caught.exception.code)
        self.assertEqual(before, self.raw_bytes())

    def test_one_bad_line_in_a_batch_writes_nothing(self):
        """★ 存量**先全部校验完再动手** —— 半单成交比整单失败难查得多。"""
        before = self.raw_bytes()
        with self.assertRaises(account_store.AccountError):
            self.store.sell_items("alice", [
                self.line(BLACK_BEAD, 2, 200),
                self.line(TOP_ARMOR, 1, 40),
                self.line(BRONZE_PIPE, 1, 100),      # 一个都没有
            ])
        self.assertEqual(before, self.raw_bytes())

    def test_selling_the_same_equipment_twice_in_one_order_is_refused(self):
        # 两条都指着同一件（前台合并漏了 / 有人直接 POST）——
        # 第二条没得扣，整单必须失败，而不是「白送一份钱」。
        before = self.raw_bytes()
        with self.assertRaises(account_store.AccountError):
            self.store.sell_items("alice", [self.line(TOP_ARMOR, 2, 80)])
        self.assertEqual(before, self.raw_bytes())

    def test_money_is_capped_at_int32(self):
        """★ `0x0600` 的金币那一格是 int32（`re/packet_api.md` §3.5）——
        超了 `struct.pack("<i")` 当场抛，把那条在线连接一起带走。"""
        self.store.admin_update_account(
            "alice", money=account_store.MONEY_MAX - 10)
        _account, receipt = self.store.sell_items(
            "alice", [self.line(BLACK_BEAD, 1, 1000)])
        self.assertEqual(account_store.MONEY_MAX, receipt["money_after"])
        self.assertEqual(990, receipt["capped"])
        self.assertEqual(account_store.MONEY_MAX,
                         account_store.player_money(self.account()))

    def test_an_empty_order_is_refused(self):
        for bad in ([], None, [self.line(BLACK_BEAD, 0, 0)]):
            with self.assertRaises(account_store.AccountError):
                self.store.sell_items("alice", bad)

    def test_a_negative_price_is_refused(self):
        with self.assertRaises(account_store.AccountError):
            self.store.sell_items("alice", [self.line(BLACK_BEAD, 1, -1)])

    def test_a_returned_material_the_client_does_not_know_is_refused(self):
        # 坏配方不该把仓库写脏（客户端不认得的 id 进了仓库会怎样，没人知道）。
        before = self.raw_bytes()
        with self.assertRaises(account_store.AccountError):
            self.store.sell_items(
                "alice", [self.line(TOP_ARMOR, 1, 10, {NO_SUCH_ITEM: 1})])
        self.assertEqual(before, self.raw_bytes())

    # -------------------------------------- 可堆叠、却住在 `inventory` 里的那些
    def test_a_stackable_item_that_lives_in_the_inventory_can_be_sold(self):
        """★★ 消耗品 / 礼包 / 钥匙：`stackable()` 为真，住的却是 `inventory`。

        「数量有没有意义」和「住哪个桶」是**两件独立的事**：
        `add_materials()`（掉落）进 `materials`，`add_item()`（买 / 合成 /
        领礼物 / 控制通道 `give`）一律进 `inventory`。全物品表里有 20 种
        `stackable()` 为真、`is_material()` 为假的东西，它们必然住在
        `inventory` 里 —— 按 `stackable()` 去 `materials` 找永远找不到。
        症状不是报错，是「管理页上明明摆着，一按确定就说东西不够」。
        """
        self.store.add_item("alice", POTION, count=3)
        account = self.account()
        self.assertEqual(3, account_store.inventory_items(account)[POTION]["count"])
        self.assertEqual(0, account_store.material_count(account, POTION))

        _account, receipt = self.store.sell_items(
            "alice", [self.line(POTION, 2, 200)])
        self.assertEqual(200, receipt["gained"])
        account = self.account()
        # 只扣掉卖的那两个，剩下的一个还在**原来那个桶**里。
        self.assertEqual(1, account_store.inventory_items(account)[POTION]["count"])
        self.assertEqual(1200, account_store.player_money(account))

    def test_selling_the_last_one_removes_the_inventory_row(self):
        # 减到 0 的格子整条删掉，别在存档里留一个 `0`。
        self.store.add_item("alice", POTION, count=2)
        self.store.sell_items("alice", [self.line(POTION, 2, 200)])
        self.assertNotIn(POTION, account_store.inventory_items(self.account()))

    def test_a_stackable_inventory_item_short_by_one_fails_the_whole_order(self):
        # 存量校验也得看对桶 —— 看错桶的话它会把「有 3 个」误判成「一个没有」。
        self.store.add_item("alice", POTION, count=1)
        before = self.raw_bytes()
        with self.assertRaises(account_store.AccountError) as caught:
            self.store.sell_items("alice", [self.line(POTION, 2, 200),
                                            self.line(BLACK_BEAD, 1, 100)])
        self.assertEqual("not_enough_items", caught.exception.code)
        self.assertEqual(before, self.raw_bytes())


if __name__ == "__main__":
    unittest.main()
