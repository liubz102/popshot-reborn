#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V0.3.3 成就防刷 —— **bot 受限的那一局不计成就**（D127）。

起因：V0.3.3 的成就卡片发出去之后，有人建房 → `/a` 加 bot → `/hold` 把 bot
定住 → 反复打死它刷成就。能把 bot 变成靶子的不止 `/hold`：`/w M` 锁枪、
`/dash` 关近身、`/noboom`（子弹只飞不炸）、`/slow`、`/d 1` 全算。

判据分三层，这个文件把三层各钉一遍：

1. `bot.bot_limit_reason(room)` —— 「**这会儿**房里的 bot 受限没有」的实况判断；
2. `RoomQuest.bot_limit_reason` —— 「这一局里**出现过**没有」的那个**闩**
   （开局时由 `gameserver.new_room_quest()` 填，局中由 `bot.handle_command()` 补）；
3. 结算 —— 有闩就**不并累计、不发卡**，而经验 / 金币 / 材料**照发**。

★ 另有三道**防回归**的网（`LimitTableGuardTests`）：新加一条 bot 命令不归类
当场红、`BOT_FREEDOM_FIELDS` 的字段改名或改默认值当场红、难度表加一档当场红。
漏掉的症状全是「**静默地不生效**」而不是报错，只能靠测试拦（同铁律 13 那套）。

⚠ 只从别的测试模块里借**没有用例的基类**和纯函数（`LobbyIsolated` /
`_CardSettlementCase` / `make_conn` / `chat_lines` …）—— 借一个带用例的类过来，
它那些用例会跟着在本模块里再跑一遍（`test_botbreak` 就是这么多出 5 条的）。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import bot                                                     # noqa: E402
import gameserver                                              # noqa: E402
from test_room import LobbyIsolated, make_conn                 # noqa: E402
from test_bot import chat_lines, FREE_ROOM                     # noqa: E402
from test_battle import _CardSettlementCase, opcodes           # noqa: E402


# ----------------------------------------------------------------------------
# 一、实况判断：`bot.bot_limit_reason(room)`
# ----------------------------------------------------------------------------
class BotLimitReasonTests(LobbyIsolated):
    """「这会儿房里的 bot 受限没有」—— 纯判断，不开局、不发包。"""

    def setUp(self):
        super().setUp()
        self.host = make_conn("alice")

    def open_room(self, **kwargs):
        params = dict(FREE_ROOM)
        params.update(kwargs)
        room = self.lobby.create_room(self.host, title="来玩",
                                      seat=self.host.seat_snapshot(), **params)
        self.host.sent.clear()
        return room

    def room_with_bots(self, count=1):
        room = self.open_room()
        bot.handle_command(self.host, "/a %d" % count)
        self.assertEqual(count, len(room.bot_seats()))
        return room

    def machines(self, room):
        return [room.seats[i].conn for i in room.bot_seats()]

    # -- 没有 bot 的场合 --------------------------------------------------
    def test_no_room_at_all_is_never_limited(self):
        """单人 / 协议试探那条路（`lobby_room()` 是 None）。"""
        self.assertIsNone(bot.bot_limit_reason(None))

    def test_a_room_without_bots_is_never_limited(self):
        self.assertIsNone(bot.bot_limit_reason(self.open_room()))

    def test_difficulty_one_does_not_matter_without_bots(self):
        """★ 用户 2026-09-15 拍板的那一条：bot 全踢掉就恢复正常计算。

        难度是房间级的一格，bot 走了它还留着 —— 但那时候没东西可刷。
        """
        room = self.room_with_bots()
        bot.handle_command(self.host, "/d 1")
        self.assertIsNotNone(bot.bot_limit_reason(room))
        for index in room.bot_seats():        # 把 bot 全摘掉（= 客户端踢人）
            self.lobby.kick(room, index)
        self.assertEqual([], room.bot_seats())
        self.assertEqual(1, room.bot_difficulty)
        self.assertIsNone(bot.bot_limit_reason(room))

    # -- 难度 -------------------------------------------------------------
    def test_every_difficulty_tier_is_judged(self):
        """★ 防回归：难度表加一档时这条会红，逼作者想「新档算不算」。"""
        room = self.room_with_bots()
        for level in sorted(bot.BOT_DIFFICULTY_PROFILES):
            room.bot_difficulty = level
            reason = bot.bot_limit_reason(room)
            if level < bot.BOT_DIFFICULTY_MIN_FOR_CARDS:
                self.assertIsNotNone(reason, "难度 %d 该算受限" % level)
                self.assertIn("难度", reason)
            else:
                self.assertIsNone(reason, "难度 %d 不该算受限" % level)

    def test_the_default_difficulty_counts(self):
        self.assertGreaterEqual(bot.BOT_DIFFICULTY_DEFAULT,
                                bot.BOT_DIFFICULTY_MIN_FOR_CARDS)
        self.assertIsNone(bot.bot_limit_reason(self.room_with_bots()))

    # -- 五个开关 ---------------------------------------------------------
    def test_each_freedom_field_alone_is_enough(self):
        """★ 五个字段**逐个**各来一遍，别只测 `/hold` 那一个。"""
        for field, (free_value, _why) in bot.BOT_FREEDOM_FIELDS.items():
            with self.subTest(field=field):
                room = self.room_with_bots()
                machine = self.machines(room)[0]
                self.assertIsNone(bot.bot_limit_reason(room))
                setattr(machine, field, _not_free(free_value))
                reason = bot.bot_limit_reason(room)
                self.assertIsNotNone(reason, "%s 没被当成限制" % field)
                self.assertIn(str(room.bot_seats()[0]), reason)
                setattr(machine, field, free_value)
                self.assertIsNone(bot.bot_limit_reason(room))

    def test_one_limited_bot_among_many_is_enough(self):
        """房里另一个自由的 bot 不能替它作保。"""
        room = self.room_with_bots(3)
        self.assertIsNone(bot.bot_limit_reason(room))
        self.machines(room)[-1].holding = True
        self.assertIsNotNone(bot.bot_limit_reason(room))

    def test_a_bot_seat_without_a_connection_does_not_explode(self):
        """调试通道造得出「是 bot 但 `conn is None`」的座位，别让它炸。"""
        room = self.room_with_bots()
        room.seats[room.bot_seats()[0]].conn = None
        self.assertIsNone(bot.bot_limit_reason(room))

    # -- 命令层：哪条命令真的会把它变成靶子 --------------------------------
    def test_every_limiting_command_can_actually_limit(self):
        """★ 防回归：`LIMITING_COMMANDS` 里每条都举得出一次「敲完就受限」。"""
        for line in ("/hold", "/dash", "/noboom", "/slow", "/w 2", "/d 1"):
            with self.subTest(command=line):
                room = self.room_with_bots()
                self.assertIsNone(bot.bot_limit_reason(room))
                bot.handle_command(self.host, line)
                self.assertIsNotNone(bot.bot_limit_reason(room),
                                     "%s 敲完居然还算自由" % line)

    def test_harmless_commands_do_not_limit(self):
        for line in ("/a", "/c 1 2", "/t 1", "/r", "/h", "/w 0", "/d 3"):
            with self.subTest(command=line):
                room = self.room_with_bots()
                bot.handle_command(self.host, line)
                self.assertIsNone(bot.bot_limit_reason(room),
                                  "%s 不该把房间判成受限" % line)

    def test_releasing_a_switch_goes_back_to_free(self):
        """`/hold` 是开关：再敲一次，**实况**回到自由（本局的闩另说）。"""
        room = self.room_with_bots()
        bot.handle_command(self.host, "/hold")
        self.assertIsNotNone(bot.bot_limit_reason(room))
        bot.handle_command(self.host, "/hold")
        self.assertIsNone(bot.bot_limit_reason(room))

    def test_a_weapon_lock_that_failed_leaves_the_bot_free(self):
        """★ 判据是实况、不是「敲过什么命令」的另一个理由：`/w` 可能**失败**。

        角色缺那个槽时 `_apply_gun` 报错、`machine.weapon_slot` 原封不动 ——
        那个 bot 仍然是真自由的，不该被判成受限。
        """
        room = self.room_with_bots()
        machine = self.machines(room)[0]
        machine.weapon_slot = None
        with unittest.mock.patch.object(bot.weapondata, "slot_for",
                                        return_value=None):
            bot.handle_command(self.host, "/w 2")
        self.assertIsNone(machine.weapon_slot)
        self.assertIsNone(bot.bot_limit_reason(room))


def _not_free(free_value):
    """造一个「和自由值不一样」的值 —— 五个开关的自由值分别是 False/None/True。"""
    if free_value is None:
        return 2                        # weapon_slot：锁 2 号槽
    return not free_value


# ----------------------------------------------------------------------------
# 二、两张单源表的防回归网
# ----------------------------------------------------------------------------
class LimitTableGuardTests(unittest.TestCase):
    """★★ 这三条拦的都是「**静默地不生效**」，不是报错。

    抄的是铁律 13 那套（`server.config` 的键必须归类成客户端侧 / 服务端侧，
    `test_online.test_every_key_is_classified_client_or_server` 钉着）。
    """

    def test_every_command_is_classified(self):
        """新加一条 bot 命令没归类 ⇒ 当场红，逼作者当场决定它限不限制 bot。"""
        self.assertEqual(
            set(bot.COMMANDS),
            bot.LIMITING_COMMANDS | bot.HARMLESS_COMMANDS,
            "有命令没归类：往 LIMITING_COMMANDS 或 HARMLESS_COMMANDS 里加一条")

    def test_the_two_buckets_do_not_overlap(self):
        self.assertEqual(frozenset(),
                         bot.LIMITING_COMMANDS & bot.HARMLESS_COMMANDS)

    def test_every_freedom_field_is_a_real_botconn_default(self):
        """字段改名 / 改默认值 ⇒ 当场红。

        ★ 不改的话症状是 `getattr(machine, field, free_value)` 永远取到兜底的
        自由值 —— 判据静默失效，一条日志都不会有。
        """
        machine = bot.BotConn(0)
        for field, (free_value, why) in bot.BOT_FREEDOM_FIELDS.items():
            with self.subTest(field=field):
                self.assertTrue(hasattr(machine, field),
                                "BotConn 没有 %s 这一格了" % field)
                self.assertEqual(free_value, getattr(machine, field),
                                 "%s 的出厂值不再是「自由」了" % field)
                self.assertTrue(why.strip(), "%s 少一句人话" % field)


# ----------------------------------------------------------------------------
# 三、本局的闩 + 结算
# ----------------------------------------------------------------------------
class _CardFairCase(_CardSettlementCase):
    """`_CardSettlementCase`（对战 · 格挡 3 次发一张卡）+ 房里一个 bot。

    **自己不带用例**（带的话每个子类都会把它们重跑一遍）。
    """

    def start_battle(self):
        bot.handle_command(self.alice, "/a")
        self.bot_seat = self.room.bot_seats()[0]
        self.bot_conn = self.room.seats[self.bot_seat].conn
        super().start_battle()

    def restart(self):
        """再开一局。★ 走 `new_room_quest()` —— 父类那份是直接
        `RoomQuest(...)`，绕过开局快照就测不到闩。"""
        for conn in (self.alice, self.bob):
            conn.sent[:] = []
            conn.settled = False
            conn.account = dict(self.accounts.saved[conn.account_name])
        self.room.quest = gameserver.new_room_quest(
            self.room, [0, 1, self.bot_seat], announce=True)

    def cards_of(self, conn=None, seat=0):
        """这个人收到的**称号卡片**那一栏（`0x041c` 槽 1）。"""
        return [row for row in self.rewards(conn or self.alice)
                if row[0] == seat and row[1] == gameserver.REWARD_SLOT_TITLE]

    def verdict_line(self, conn=None):
        """结算时那一行「成就判定: …」（**普通日志**，不是 --verbose）。"""
        lines = [l for l in (conn or self.alice).logged if "成就判定" in l]
        self.assertEqual(1, len(lines), "一局只该有一行成就判定：%r" % lines)
        return lines[0]


class RoundLatchTests(_CardFairCase):
    """`RoomQuest.bot_limit_reason` —— 「这一局里出现过限制没有」那个闩。"""

    def test_a_clean_round_has_no_latch(self):
        self.assertIsNone(self.quest.bot_limit_reason)

    def test_a_command_during_the_round_latches_it(self):
        bot.handle_command(self.alice, "/hold")
        self.assertIsNotNone(self.quest.bot_limit_reason)
        self.assertTrue(self.quest.bot_limit_reason.startswith("局中"))

    def test_releasing_it_later_does_not_clear_the_latch(self):
        """★★ 口径 1 的核心：「定住打五十下、结算前解开」照样不算。"""
        bot.handle_command(self.alice, "/hold")
        latched = self.quest.bot_limit_reason
        bot.handle_command(self.alice, "/hold")
        self.assertIsNone(bot.bot_limit_reason(self.room))   # 实况已经自由
        self.assertEqual(latched, self.quest.bot_limit_reason)

    def test_a_release_only_round_still_latches(self):
        """★ 「命令**之前**」那一扫存在的唯一理由。

        开局就定着、这一局里只敲了一次「解除」—— 只扫命令之后的话，
        这一局会被判成干净。
        """
        self.bot_conn.holding = True
        self.restart()
        self.quest.bot_limit_reason = None      # 假装开局那一扫没赶上
        bot.handle_command(self.alice, "/hold")  # 这一敲是**解除**
        self.assertFalse(self.bot_conn.holding)
        self.assertIsNotNone(self.quest.bot_limit_reason)

    def test_the_latch_survives_into_the_next_round_while_still_held(self):
        """跨局：限制没解除 ⇒ 下一局照样是脏的（开局那一扫记的）。"""
        bot.handle_command(self.alice, "/hold")
        self.restart()
        self.assertIsNotNone(self.quest.bot_limit_reason)
        self.assertTrue(self.quest.bot_limit_reason.startswith("开局时"))

    def test_the_next_round_is_clean_once_the_limit_is_gone(self):
        """★ 用户口径：限制解除了，下一局就重新计。"""
        bot.handle_command(self.alice, "/hold")
        bot.handle_command(self.alice, "/hold")
        self.restart()
        self.assertIsNone(self.quest.bot_limit_reason)

    def test_the_next_round_is_clean_once_the_bots_are_gone(self):
        """★ 用户原话：「下一局如果 bot 全踢掉，也恢复正常计算成就」。"""
        bot.handle_command(self.alice, "/hold")
        self.lobby.kick(self.room, self.bot_seat)
        self.room.quest = gameserver.new_room_quest(self.room, [0, 1])
        self.assertIsNone(self.quest.bot_limit_reason)

    def test_a_harmless_command_does_not_latch(self):
        bot.handle_command(self.alice, "/d 3")
        self.assertIsNone(self.quest.bot_limit_reason)

    def test_a_map_change_does_not_clear_the_latch(self):
        """换图和战绩一样，一整轮算一份（`begin_map_change` 不清它）。"""
        bot.handle_command(self.alice, "/hold")
        latched = self.quest.bot_limit_reason
        self.quest.begin_map_change("map-02")
        self.assertEqual(latched, self.quest.bot_limit_reason)


class UserReportedSequenceTests(_CardFairCase):
    """★★ 用户 2026-09-15 实机走的那一串，逐键复现（`sessions/2026-09-15-04.md`）。

    他在**房间里**（上一局已结算、`room.quest` 还是 None 的时候）依次敲：
    `/hold` 定住 → `/hold` 解除 → `/d 1` → `/d 2`，然后开局，
    结果进游戏还是判「难度只有 1、不计成就」。

    ⚠ **服务端没算错**：日志显示他第四下敲成了 `/b 2`（不认识的命令被当普通
    聊天广播掉，不报错），难度从头到尾都是 1。这一组用例把「敲对了会怎样」
    和「敲错了会怎样」双向钉住 —— 前者必须恢复计成就，后者必须**不**恢复。
    """

    def in_the_room(self):
        """回到「上一局已结算、还没开下一局」那个状态：`room.quest is None`。"""
        self.room.quest = None
        self.clear()

    def start_the_next_round(self):
        self.room.quest = gameserver.new_room_quest(
            self.room, [0, 1, self.bot_seat], announce=True)

    def test_the_sequence_he_meant_to_type_clears_the_limit(self):
        self.in_the_room()
        for line in ("/hold", "/hold", "/d 1", "/d 2"):
            bot.handle_command(self.alice, line)
        self.assertEqual(2, self.room.bot_difficulty)
        self.assertIsNone(bot.bot_limit_reason(self.room))
        self.start_the_next_round()
        self.assertIsNone(self.quest.bot_limit_reason)
        self.end()
        self.assertIn("本局计入成就", self.verdict_line())

    def test_the_sequence_he_actually_typed_still_blocks(self):
        """`/b 2` 不是命令 ⇒ 难度还是 1 ⇒ 判定不计成就**是对的**。"""
        self.in_the_room()
        for line in ("/hold", "/hold", "/d 1", "/b 2"):
            self.assertFalse(
                bot.handle_command(self.alice, line) and line == "/b 2",
                "/b 2 不该被当成 bot 命令吞掉")
        self.assertEqual(1, self.room.bot_difficulty)
        self.start_the_next_round()
        self.assertIsNotNone(self.quest.bot_limit_reason)
        self.end()
        self.assertIn("难度", self.verdict_line())

    def test_the_typo_is_now_visible_because_nothing_says_it_cleared(self):
        """★ 这才是那次真正的毛病：**没有回执**，看不出命令白敲了。

        敲对 ⇒ 房里看到一行「限制已解除」；敲错 ⇒ 一个字都没有。
        """
        self.in_the_room()
        for line in ("/hold", "/hold", "/d 1"):
            bot.handle_command(self.alice, line)
        self.clear()
        bot.handle_command(self.alice, "/b 2")
        self.assertEqual([], self.cleared(self.alice))
        bot.handle_command(self.alice, "/d 2")
        self.assertEqual(1, len(self.cleared(self.alice)))

    def test_commands_in_the_room_do_not_need_a_quest_to_work(self):
        """房间里 `room.quest` 是 None，`note_bot_limit()` 无处可写 ——
        这一局的判定全靠开局那一扫，所以它不能漏。"""
        self.in_the_room()
        bot.handle_command(self.alice, "/hold")
        self.assertIsNone(self.room.quest)
        self.start_the_next_round()
        self.assertIsNotNone(self.quest.bot_limit_reason)
        self.assertTrue(self.quest.bot_limit_reason.startswith("开局时"))

    def cleared(self, conn):
        return [l for l in chat_lines(conn)
                if bot.BOT_LIMIT_CLEARED_NOTICE in l]


class FairSettlementTests(_CardFairCase):
    """结算：脏局不并累计、不发卡；经验 / 金币 / 材料照发。"""

    def test_a_clean_round_still_ships_the_card(self):
        """★ 防「一有 bot 就全不算」的过度拦截。"""
        self.guard(0, 3)
        self.end()
        self.assertEqual([(0, gameserver.REWARD_SLOT_TITLE, self.CARD, 1)],
                         self.cards_of())
        self.assertIn("本局计入成就", self.verdict_line())

    def test_a_limited_round_ships_no_card(self):
        bot.handle_command(self.alice, "/hold")
        self.guard(0, 3)
        self.end()
        self.assertEqual([], self.cards_of())
        self.assertEqual({}, self.accounts.saved["alice"].get("card_grants", {}))

    def test_a_limited_round_does_not_move_the_cumulative_stats(self):
        """★★ 只拦发卡是不够的：累计类条件占了一半（D111），
        不拦累计的话「脏房间攒满、干净房间领卡」这条路还开着。"""
        before = dict(self.accounts.saved["alice"].get("battle_stats") or {})
        bot.handle_command(self.alice, "/hold")
        self.guard(0, 9)
        self.end()
        self.assertEqual(before,
                         self.accounts.saved["alice"].get("battle_stats") or {})

    def test_a_clean_round_does_move_the_cumulative_stats(self):
        self.guard(0, 9)
        self.end()
        self.assertTrue(self.accounts.saved["alice"].get("battle_stats"),
                        "干净局的累计战绩不该是空的")

    def test_the_stats_never_even_reach_the_save_layer(self):
        """★ 上一条看的是结果，这一条看的是**入口**：脏局连 `stats_mode` /
        `stats_gained` 都不传给 `apply_battle`（存档层那句
        `if stats_mode and stats_gained:` 才接得住）。

        分两条是因为它们各自拦得住不同的写法：只把 `after_stats` 冻住、
        照旧把 `gained_stats` 递下去的话，上一条照样绿。
        """
        seen = []
        real = self.accounts.apply_battle

        def spy(username, **kwargs):
            seen.append((username, kwargs))
            return real(username, **kwargs)

        self.accounts.apply_battle = spy
        bot.handle_command(self.alice, "/hold")
        self.guard(0, 9)
        self.end()
        mine = [kw for name, kw in seen if name == "alice"]
        self.assertEqual(1, len(mine))
        self.assertIsNone(mine[0]["stats_mode"])
        self.assertIsNone(mine[0]["stats_gained"])
        self.assertEqual({}, mine[0]["cards"])
        # ★ 经验 / 金币 / 材料三个参数**一个字不动** —— 这才是「只影响成就」。
        self.assertGreater(mine[0]["experience"], 0)
        self.assertGreater(mine[0]["money"], 0)

    def test_the_settlement_rescan_catches_a_latch_that_never_got_set(self):
        """★ 结算那一刻的**兜底扫描**：闩没上、可 bot 就在眼前定着。

        正常路径上闩不掉（开局一扫 + 每条命令前后各一扫），这一条扫的是
        「将来有人绕开那两处改了 bot 状态」。判据是实况，绕不过去。
        """
        self.bot_conn.holding = True
        self.quest.bot_limit_reason = None
        self.guard(0, 3)
        self.end()
        self.assertEqual([], self.cards_of())
        self.assertIn("结算时", self.verdict_line())

    def test_experience_and_money_are_untouched(self):
        """★ 用户拍板：受限**只**影响成就，经验 / 金币 / 材料一分不少。

        两局对着比，看的是**增量** —— 数值本身要查 `rewards.json`，
        写死一个数就成了「奖励表改一次这条就红」。
        """
        clean = self._round_gain()
        self.restart()
        bot.handle_command(self.alice, "/hold")
        dirty = self._round_gain()
        self.assertEqual(clean, dirty)
        self.assertGreater(clean[0], 0)     # 真发了东西，不是两边都 0

    def _round_gain(self):
        """打一局，返回这一局**多出来**的 (经验, 金币, 材料)（卡片不算）。"""
        before = self._wallet()
        self.guard(0, 3)
        self.end()
        after = self._wallet()
        keys = set(before[2]) | set(after[2])
        return (after[0] - before[0], after[1] - before[1],
                {k: after[2].get(k, 0) - before[2].get(k, 0)
                 for k in keys if int(k) != self.CARD})

    def _wallet(self):
        account = self.accounts.saved["alice"]
        return (int(account["experience"]), int(account["money"]),
                dict(account.get("materials") or {}))

    def test_the_verdict_line_says_why_it_was_skipped(self):
        """★ 用户点名：每局 log 都要写清楚计算或不计算的**原因**。"""
        bot.handle_command(self.alice, "/hold")
        self.end()
        line = self.verdict_line()
        self.assertIn("本局不计成就", line)
        self.assertIn("/hold", line)
        self.assertIn("经验", line)         # 说清楚哪几样不受影响

    def test_the_verdict_line_says_why_it_counted(self):
        self.end()
        line = self.verdict_line()
        self.assertIn("本局计入成就", line)
        self.assertIn("bot", line)
        self.assertIn("难度", line)

    def test_a_room_without_bots_says_so(self):
        self.lobby.kick(self.room, self.bot_seat)
        self.end()
        self.assertIn("房里没有 bot", self.verdict_line())

    def test_the_debug_log_skips_the_whole_rule_table(self):
        """不计成就时**不去跑** `cards.explain()` —— 结论已经定了。"""
        self.addCleanup(setattr, gameserver, "VERBOSE", gameserver.VERBOSE)
        gameserver.VERBOSE = True
        bot.handle_command(self.alice, "/hold")
        self.guard(0, 3)
        self.end()
        text = "\n".join(self.alice.vlogged)
        self.assertIn("本局不计成就", text)
        self.assertIn("全部跳过", text)
        self.assertNotIn("✓", text)         # 一条规则的判定都没打

    def test_the_bot_never_gets_a_verdict_line_of_its_own(self):
        """那一行是**房间级**的事实，一局只打一次。

        ★ 结算是**逐座位**循环，而 `settlement_seats()` 把 bot 座位也算进来
        （日志里那些「本局战绩 座位2」就是它）。判定那一行要是写进循环里，
        六个座位就各来一份、bot 那几份还没人看。
        """
        caught = []
        self.bot_conn.log = caught.append
        bot.handle_command(self.alice, "/hold")
        self.end()
        self.assertEqual([], [l for l in caught if "成就判定" in l])
        self.assertTrue([l for l in caught if "本局战绩" in l],
                        "夹具没接上：bot 连一行结算日志都没打")


class LimitNoticeTests(_CardFairCase):
    """玩家提示：第一次触发时一行 + 脏局每次开局一行（用户口径 5）。"""

    def notices(self, conn):
        return [l for l in chat_lines(conn) if "本局不计成就" in l]

    def cleared(self, conn):
        return [l for l in chat_lines(conn)
                if bot.BOT_LIMIT_CLEARED_NOTICE in l]

    def free_the_bot(self):
        """把 bot 恢复成完全自由，并清掉已经发出去的包。

        ★ 提示是按**状态翻转**去重的 —— 不先恢复自由的话，第二条限制命令
        本来就不该再说话，那条用例会误判成「文案漏发」。
        ⚠ 别在用例里重新调 `setUp()` 来「换一张干净的桌子」：`BattleRoom.setUp`
        会把**当前**的 `gameserver.PEER_RELAY` 存成待还原值，再跑一次就把
        还原链套娃了 —— 于是这一片跑完之后 `PEER_RELAY` 停在一个夹具对象上，
        同一个进程里后面的 `test_udpsync.DeliverRoutingTests` 当场红。
        """
        self.room.bot_difficulty = bot.BOT_DIFFICULTY_DEFAULT
        for field, (free_value, _why) in bot.BOT_FREEDOM_FIELDS.items():
            setattr(self.bot_conn, field, free_value)
        self.assertIsNone(bot.bot_limit_reason(self.room))
        self.clear()

    def test_the_first_limiting_command_tells_the_whole_room(self):
        bot.handle_command(self.alice, "/hold")
        for who in (self.alice, self.bob):
            self.assertEqual(1, len(self.notices(who)),
                             "房里每个人都该看到一行（含房主自己）")

    def test_a_second_limiting_command_does_not_repeat_it(self):
        """★ 按**状态翻转**去重：已经受限了就不再刷屏。"""
        bot.handle_command(self.alice, "/hold")
        self.clear()
        bot.handle_command(self.alice, "/noboom")
        self.assertEqual([], self.notices(self.alice))

    def test_it_speaks_again_after_a_release_and_a_new_limit(self):
        bot.handle_command(self.alice, "/hold")
        bot.handle_command(self.alice, "/hold")     # 解除
        self.clear()
        bot.handle_command(self.alice, "/hold")
        self.assertEqual(1, len(self.notices(self.alice)))

    def test_a_harmless_command_says_nothing(self):
        bot.handle_command(self.alice, "/d 3")
        self.assertEqual([], self.notices(self.alice))

    def test_a_dirty_round_is_announced_again_at_the_start(self):
        bot.handle_command(self.alice, "/hold")
        self.clear()
        self.restart()
        for who in (self.alice, self.bob):
            self.assertEqual(1, len(self.notices(who)))

    def test_a_clean_round_start_says_nothing(self):
        self.clear()
        self.restart()
        self.assertEqual([], self.notices(self.alice))

    # -- 解除时也要有回执（用户 2026-09-15 第二轮）------------------------
    def test_lifting_the_limit_tells_the_whole_room(self):
        """★ 只说「受限了」不说「解除了」的话，房主敲完命令屏幕上什么都不动，
        分不清是「解除成功」还是「命令压根没生效」。"""
        bot.handle_command(self.alice, "/hold")
        self.clear()
        bot.handle_command(self.alice, "/hold")
        for who in (self.alice, self.bob):
            self.assertEqual(1, len(self.cleared(who)))

    def test_lifting_it_during_a_round_spells_out_that_this_round_is_lost(self):
        """★ 局中解除：这一局的闩不撤 ⇒ 文案要**点明「本局不生效」**
        （用户 2026-09-15 第三轮）。"""
        bot.handle_command(self.alice, "/hold")
        bot.handle_command(self.alice, "/hold")
        line = self.cleared(self.alice)[0]
        self.assertIn("下一局", line)
        self.assertIn(bot.BOT_LIMIT_CLEARED_IN_ROUND, line)
        self.assertIsNotNone(self.quest.bot_limit_reason)

    def test_lifting_it_in_the_room_leaves_the_parenthesis_off(self):
        """★ 在房间里解除：压根没有「本局」可言，那个括号只会让人多想。

        判据是**这一局上没上闩**（回房间时 `room.quest` 被置 None），
        不是「房间状态是不是游戏中」。
        """
        bot.handle_command(self.alice, "/hold")
        self.room.quest = None              # = 看完结算回到房间
        self.clear()
        bot.handle_command(self.alice, "/hold")
        line = self.cleared(self.alice)[0]
        self.assertIn("下一局", line)
        self.assertNotIn(bot.BOT_LIMIT_CLEARED_IN_ROUND, line)

    def test_swapping_one_limit_for_another_says_nothing(self):
        """放开 `/hold` 但难度还是 1 ⇒ 状态没翻转，一句都不说。"""
        bot.handle_command(self.alice, "/d 1")
        bot.handle_command(self.alice, "/hold")
        self.clear()
        bot.handle_command(self.alice, "/hold")
        self.assertEqual([], self.cleared(self.alice))
        bot.handle_command(self.alice, "/d 3")
        self.assertEqual(1, len(self.cleared(self.alice)))

    def test_a_clean_command_never_says_it_was_cleared(self):
        bot.handle_command(self.alice, "/d 3")
        self.assertEqual([], self.cleared(self.alice))

    def test_both_cleared_notices_fit_the_chat_box(self):
        """★ 带括号那一句是最长的，单量不带括号那句拦不住它。"""
        for tail in (bot.BOT_LIMIT_CLEARED_IN_ROOM,
                     bot.BOT_LIMIT_CLEARED_IN_ROUND):
            line = bot.BOT_LIMIT_CLEARED_NOTICE + tail
            with self.subTest(tail=tail):
                self.assertLessEqual(
                    _chat_width(line), 50,
                    "太长会折行：%r（%d 宽）" % (line, _chat_width(line)))


    def test_every_notice_is_short_enough_for_the_chat_box(self):
        """★★ 聊天框一次只看得见 4 行，折出来的行同样吃额度（§20）。

        ⚠ **每一种原因各量一次**，别只量 `/hold` 那一条 —— 第一版
        `/noboom` / `/slow` 的人话带着括号解释，57 / 59 宽，只量 `/hold`（44）
        的话一条都拦不住。难度那一档也要量：它走的是另一支文案。
        """
        for line in ("/hold", "/w 2", "/dash", "/noboom", "/slow", "/d 1"):
            with self.subTest(command=line):
                self.free_the_bot()
                bot.handle_command(self.alice, line)
                said = self.notices(self.alice)
                self.assertEqual(1, len(said), "%s 没提示" % line)
                self.assertLessEqual(
                    _chat_width(said[0]), 50,
                    "太长会折行：%r（%d 宽）" % (said[0], _chat_width(said[0])))


class SettlementNoticeTests(_CardFairCase):
    """★ 结算界面上也说一句「这一局没算成就」（用户 2026-09-15 第三轮）。"""

    def notices(self, conn):
        return [l for l in chat_lines(conn) if "不结算成就" in l]

    def test_a_limited_round_tells_everyone_at_the_result_screen(self):
        bot.handle_command(self.alice, "/hold")
        self.clear()
        self.end()
        for who in (self.alice, self.bob):
            self.assertEqual(1, len(self.notices(who)),
                             "房里每个人都该在结算界面看到一行")
        self.assertIn("bot 限制", self.notices(self.alice)[0])

    def test_a_clean_round_says_nothing_at_the_result_screen(self):
        """★ 用户点名：正常结算一个字都不发。"""
        self.guard(0, 3)
        self.end()
        self.assertEqual([], self.notices(self.alice))

    def test_it_comes_after_the_settlement_packets(self):
        """★ 结算界面是第一发 `0x0411` 弹出来的 —— 这行字必须排在它之后，
        否则落在还没弹出来的界面后面。"""
        bot.handle_command(self.alice, "/hold")
        self.clear()
        self.end()
        seen = opcodes(self.alice)
        self.assertIn(gameserver.OP_END_GAME, seen)
        self.assertIn(gameserver.OP_CHAT, seen)
        self.assertGreater(seen.index(gameserver.OP_CHAT),
                           seen.index(gameserver.OP_END_GAME))

    def test_it_is_said_once_per_round_not_once_per_seat(self):
        """结算是逐座位循环（bot 座位也在里面），这一句是**房间级**的。"""
        bot.handle_command(self.alice, "/hold")
        self.clear()
        self.end()
        self.assertEqual(1, len(self.notices(self.alice)))

    def test_it_fits_the_chat_box(self):
        bot.handle_command(self.alice, "/hold")
        self.clear()
        self.end()
        line = self.notices(self.alice)[0]
        self.assertLessEqual(_chat_width(line), 50,
                             "太长会折行：%r（%d 宽）" % (line, _chat_width(line)))

def _chat_width(text):
    """聊天框里的显示宽度：中文和全角标点算 2，ASCII 算 1（§20 的口径）。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in text)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
