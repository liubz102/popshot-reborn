#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""晋级任务（X_Mod X23）—— `server/missions.py` 的纯函数 + 结算端到端。

要守住的（PLAN X23 · 第 7 步）：

* **周期键**：按「服务器本地时间 − 4 小时」—— 凌晨 4 点前后、周一 4 点前后、
  月初 4 点前后各一刀；上一期攒的进度到点清零，上一期的完成不算本期；
* **能做** = 启用 ∧ 等级够 ∧ 角色对得上（不限角色的爱琳也算）∧ 本期没完成；
* **所有能做的任务同时推进**，难度默认**恰好**（勾了「更高难度也算」才按至少）；
* 每人一张表：去掉本期已完成的、等级不够的照样带、不限角色的三个页签各一份、
  每条链按 (order, id) 排、最多 59 条；客户端 id = 任务 id × 10 + 页签 + 1；
* 生成的客户端 ini 用**客户端加载器的 Python 镜像**读得回来（原版那份也读得出 105 条）；
* `0x0F03` 分片拼得回来、坏片整份丢弃；`0x02e0` 72 字节 / `0x02e1` 24 字节；
* 结算：一局打完同时完成好几条、奖励进礼物箱（发送人 GM）、只给本人的聊天、
  回大厅先补完成弹窗再推新表；不计奖励的那一局一条都不推进。

⚠ 这些用例里的时间一律显式传 `now`（本地时间构造），**不睡、不等**（铁律 10）。
"""
import os
import struct
import sys
import time
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import account_store                                          # noqa: E402
import gameserver                                             # noqa: E402
import missions                                               # noqa: E402
import shopcfg                                                # noqa: E402
import shopdefaults                                           # noqa: E402
from test_battle import (BattleRoom, bodies, opcodes)         # noqa: E402

ROOT = os.path.dirname(HERE)
ORIGINAL_INI = os.path.join(ROOT, "game_patched", "Pack_develop", "Data",
                            "Promotion-chn.ini")

BRONZE_PIPE = 30018        # 材料：能进背包、可叠加
SHIRT = 1010001            # 装备：能进背包、不可叠加（数量固定 1）


def local(year, month, day, hour=12, minute=0):
    """本地时间 → 时间戳（周期键就是按本地时间算的）。"""
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


#: 一个普通的工作日中午（不在任何 4 点边界附近）。
NOON = local(2026, 10, 30, 12)


def mission(mid, **over):
    """一条过得了校验的任务（默认：每日 · 不限角色 · 对战胜利 1 次 · 100 金币）。"""
    entry = {"id": mid, "enabled": True, "period": "daily",
             "character": missions.CHAR_ANY, "line": "pvp", "order": 0,
             "level": 1, "kind": "win", "target": 1,
             "rewards": [{"money": 100}]}
    entry.update(over)
    return missions.validate_missions({"missions": [entry]})[0]


# ===========================================================================
# 周期键
# ===========================================================================

class PeriodKeyTests(unittest.TestCase):

    def test_once_has_no_period(self):
        self.assertEqual("", missions.period_key("once", NOON))

    def test_a_day_turns_over_at_four_in_the_morning(self):
        self.assertEqual("2026-10-29",
                         missions.period_key("daily", local(2026, 10, 30, 3, 59)))
        self.assertEqual("2026-10-30",
                         missions.period_key("daily", local(2026, 10, 30, 4, 0)))
        self.assertEqual("2026-10-30",
                         missions.period_key("daily", local(2026, 10, 31, 3, 59)))

    def test_a_week_turns_over_on_monday_at_four(self):
        # 2026-11-09 是周一。
        sunday_night = local(2026, 11, 9, 3, 59)
        monday = local(2026, 11, 9, 4, 0)
        self.assertEqual(time.localtime(monday).tm_wday, 0)
        self.assertNotEqual(missions.period_key("weekly", sunday_night),
                            missions.period_key("weekly", monday))
        self.assertEqual(missions.period_key("weekly", monday),
                         missions.period_key("weekly", local(2026, 11, 16, 3, 59)))
        self.assertEqual("2026-W46", missions.period_key("weekly", monday))

    def test_a_month_turns_over_on_the_first_at_four(self):
        self.assertEqual("2026-11",
                         missions.period_key("monthly", local(2026, 12, 1, 3, 59)))
        self.assertEqual("2026-12",
                         missions.period_key("monthly", local(2026, 12, 1, 4, 0)))

    def test_an_unknown_period_is_an_error(self):
        with self.assertRaises(ValueError):
            missions.period_key("yearly", NOON)


# ===========================================================================
# 存档那一格
# ===========================================================================

class StateTests(unittest.TestCase):

    def test_garbage_normalizes_to_an_empty_state(self):
        for raw in (None, [], "x", 3, {"done": [], "progress": "?"}):
            self.assertEqual({"done": {}, "progress": {}},
                             missions.normalize_state(raw), raw)

    def test_bad_entries_are_dropped_and_good_ones_kept(self):
        state = missions.normalize_state({
            "done": {"7": {"p": "2026-10-30", "at": "x", "times": 2},
                     "abc": {"p": ""}, "-1": {"p": ""}, "8": "oops",
                     "9": {"p": None, "times": "lots"}},
            "progress": {"7": {"p": "2026-10-30", "n": 3}, "8": {"n": 0},
                         "x": {"n": 1}, "10": {"n": "two"}, "11": None}})
        self.assertEqual({"7", "9"}, set(state["done"]))
        self.assertEqual({"p": "2026-10-30", "at": "x", "times": 2}, state["done"]["7"])
        self.assertEqual({"p": "", "at": "", "times": 1}, state["done"]["9"])
        self.assertEqual({"7": {"p": "2026-10-30", "n": 3}}, state["progress"])

    def test_yesterdays_progress_and_completion_do_not_count_today(self):
        m = mission(1, target=3)
        state = {"done": {"1": {"p": "2026-10-29", "at": "", "times": 1}},
                 "progress": {"1": {"p": "2026-10-29", "n": 2}}}
        self.assertFalse(missions.is_done(m, state, NOON))
        self.assertEqual(0, missions.progress_of(m, state, NOON))
        self.assertEqual(1, missions.times_done(m, state))
        today = {"done": {"1": {"p": "2026-10-30", "at": "", "times": 1}},
                 "progress": {}}
        self.assertTrue(missions.is_done(m, today, NOON))

    def test_pruning_drops_stale_progress_and_unknown_missions_but_keeps_done(self):
        rules = [mission(1, target=5), mission(2, target=5)]
        state = {"done": {"3": {"p": "", "at": "", "times": 1}},
                 "progress": {"1": {"p": "2026-10-29", "n": 2},
                              "2": {"p": "2026-10-30", "n": 4},
                              "3": {"p": "", "n": 1}}}
        pruned = missions.prune_state(state, rules, NOON)
        self.assertEqual({"2": {"p": "2026-10-30", "n": 4}}, pruned["progress"])
        self.assertEqual(state["done"], pruned["done"])


# ===========================================================================
# 能做 / 一局怎么计
# ===========================================================================

class DoableTests(unittest.TestCase):

    def test_all_four_gates(self):
        empty = missions.empty_state()
        m = mission(1, level=5, character=1)
        ok = dict(level=5, character=1, state=empty, now=NOON)
        self.assertTrue(missions.doable(m, **ok))
        self.assertFalse(missions.doable(dict(m, enabled=False), **ok))
        self.assertFalse(missions.doable(m, **dict(ok, level=4)))
        self.assertFalse(missions.doable(m, **dict(ok, character=0)))
        done = {"done": {"1": {"p": "2026-10-30", "at": "", "times": 1}},
                "progress": {}}
        self.assertFalse(missions.doable(m, **dict(ok, state=done)))

    def test_irene_only_matches_missions_for_anyone(self):
        anyone = mission(1)
        tyr = mission(2, character=0)
        self.assertTrue(missions.char_matches(anyone, 3))
        self.assertFalse(missions.char_matches(tyr, 3))


class MatchIncrementTests(unittest.TestCase):

    def gain(self, m, **game):
        base = {"line": m["line"], "won": True}
        base.update(game)
        return missions.match_increment(m, **base)

    def test_a_win_counts_one_and_a_loss_counts_nothing(self):
        m = mission(1)
        self.assertEqual(1, self.gain(m))
        self.assertEqual(0, self.gain(m, won=False))
        self.assertEqual(0, self.gain(m, line="quest"), "线不对")

    def test_pvp_filters(self):
        m = mission(1, pvp_mode=0, item_mode=1)
        self.assertEqual(1, self.gain(m, pvp_mode=0, item_mode=True))
        self.assertEqual(0, self.gain(m, pvp_mode=3, item_mode=True))
        self.assertEqual(0, self.gain(m, pvp_mode=0, item_mode=False))
        no_items = mission(2, item_mode=0)
        self.assertEqual(1, self.gain(no_items, pvp_mode=3, item_mode=False))
        self.assertEqual(0, self.gain(no_items, pvp_mode=3, item_mode=True))

    def test_quest_difficulty_is_exact_unless_harder_is_ticked(self):
        exact = mission(1, line="quest", kind="clear", stage=3, difficulty=2)
        harder = mission(2, line="quest", kind="clear", difficulty=2, harder=True)
        for diff, want_exact, want_harder in ((1, 0, 0), (2, 1, 1), (3, 0, 1)):
            self.assertEqual(want_exact, self.gain(exact, stage=3, difficulty=diff), diff)
            self.assertEqual(want_harder, self.gain(harder, stage=5, difficulty=diff), diff)
        self.assertEqual(0, self.gain(exact, stage=4, difficulty=2), "关卡不对")
        self.assertEqual(0, self.gain(exact, stage=3, difficulty=None), "没有难度")

    def test_map_missions_never_count_in_settlement(self):
        m = mission(1, line="quest", kind="map", map="QuestLevel01", script="x",
                    id=101)
        self.assertEqual(0, self.gain(m))

    def test_sum_adds_the_metric_and_can_be_split_by_weapon(self):
        kills = mission(1, kind="stats", count="sum", metric="kills", target=50)
        rifle = mission(2, kind="stats", count="sum", metric="kills", weapon=110001,
                        target=50)
        match = {"kills": 7, "kills@110001": 3}
        self.assertEqual(7, self.gain(kills, match=match))
        self.assertEqual(3, self.gain(rifle, match=match))
        self.assertEqual(7, self.gain(kills, match=match, won=False), "累计不看输赢")
        self.assertEqual(0, self.gain(rifle, match={"kills": 7}))

    def test_match_conditions_use_the_card_engine(self):
        m = mission(1, kind="stats", count="match", conditions=[
            {"scope": "match", "metric": "kills", "op": "ge", "threshold": 5},
            {"scope": "match", "metric": "deaths", "op": "eq", "threshold": 0,
             "join": "and"}])
        self.assertEqual(1, self.gain(m, match={"kills": 5, "deaths": 0}))
        self.assertEqual(0, self.gain(m, match={"kills": 5, "deaths": 1}))
        self.assertEqual(0, self.gain(m, match={"kills": 4, "deaths": 0}))


class SettleTests(unittest.TestCase):

    def settle(self, rules, state=None, **game):
        base = dict(level=10, character=0, line="pvp", won=True, now=NOON)
        base.update(game)
        return missions.settle(rules, state or missions.empty_state(), **base)

    def test_every_doable_mission_advances_at_once(self):
        rules = [mission(1), mission(2, target=3), mission(3, pvp_mode=2),
                 mission(4, level=99), mission(5, line="quest", kind="clear")]
        state, events = self.settle(rules, pvp_mode=0)
        self.assertEqual([(1, 0, 1, True), (2, 0, 1, False)],
                         [(e["mission"]["id"], e["before"], e["after"], e["done"])
                          for e in events])
        self.assertEqual({"1"}, set(state["done"]))
        self.assertEqual("2026-10-30", state["done"]["1"]["p"])
        self.assertEqual({"2": {"p": "2026-10-30", "n": 1}}, state["progress"])

    def test_completion_caps_at_the_target_and_clears_the_progress(self):
        rules = [mission(1, kind="stats", count="sum", metric="kills", target=10)]
        state, _ = self.settle(rules, match={"kills": 6})
        state, events = self.settle(rules, state, match={"kills": 9})
        self.assertEqual((6, 10, True), (events[0]["before"], events[0]["after"],
                                         events[0]["done"]))
        self.assertEqual({}, state["progress"])
        self.assertEqual(1, state["done"]["1"]["times"])

    def test_a_done_mission_is_skipped_until_the_next_period(self):
        rules = [mission(1)]
        state, _ = self.settle(rules)
        _, events = self.settle(rules, state)
        self.assertEqual([], events, "同一天不能再完成一次")
        tomorrow = local(2026, 10, 31, 12)
        state2, events = self.settle(rules, state, now=tomorrow)
        self.assertEqual([True], [e["done"] for e in events])
        self.assertEqual(2, state2["done"]["1"]["times"])

    def test_a_once_mission_is_done_for_good(self):
        rules = [mission(1, period="once")]
        state, _ = self.settle(rules)
        _, events = self.settle(rules, state, now=local(2027, 3, 1, 12))
        self.assertEqual([], events)

    def test_progress_from_yesterday_starts_over(self):
        rules = [mission(1, target=3)]
        state, _ = self.settle(rules, now=local(2026, 10, 30, 3, 0))
        self.assertEqual("2026-10-29", state["progress"]["1"]["p"])
        state, events = self.settle(rules, state, now=local(2026, 10, 30, 5, 0))
        self.assertEqual((0, 1), (events[0]["before"], events[0]["after"]))

    def test_the_input_state_is_not_touched(self):
        rules = [mission(1, target=2)]
        state = {"done": {}, "progress": {"1": {"p": "2026-10-30", "n": 1}}}
        frozen = repr(state)
        self.settle(rules, state)
        self.assertEqual(frozen, repr(state))

    def test_irene_advances_missions_for_anyone_only(self):
        rules = [mission(1), mission(2, character=0)]
        _, events = self.settle(rules, character=3)
        self.assertEqual([1], [e["mission"]["id"] for e in events])

    def test_complete_map_records_the_completion(self):
        m = mission(101, line="quest", kind="map", map="QuestLevel01", script="x",
                    period="once")
        state, event = missions.complete_map(m, missions.empty_state(), now=NOON)
        self.assertTrue(event["done"])
        self.assertTrue(missions.is_done(m, state, NOON))


class RewardTests(unittest.TestCase):

    def test_gift_specs_one_gift_per_reward(self):
        m = mission(1, name="打赢一局", rewards=[{"money": 100}, {"exp": 20},
                                              {"item": BRONZE_PIPE, "count": 3}])
        self.assertEqual([{"money": 100}, {"exp": 20},
                          {"item": BRONZE_PIPE, "count": 3}], missions.gift_specs(m))
        self.assertEqual("完成晋级任务「打赢一局」的奖励", missions.gift_message(m))

    def test_reward_lines_for_the_client(self):
        self.assertEqual("[金币] +100", missions.reward_text({"money": 100}))
        self.assertEqual("[经验值] +20", missions.reward_text({"exp": 20}))
        self.assertTrue(missions.reward_text({"item": BRONZE_PIPE, "count": 3})
                        .startswith("[材料] "))
        self.assertTrue(missions.reward_text({"item": BRONZE_PIPE, "count": 3})
                        .endswith(" ×3"))
        self.assertEqual("0,100", missions.reward_wire({"money": 100}))
        self.assertEqual("2,20", missions.reward_wire({"exp": 20}))
        self.assertEqual("1,%d,0" % BRONZE_PIPE,
                         missions.reward_wire({"item": BRONZE_PIPE, "count": 3}))


# ===========================================================================
# 每个玩家自己那张表
# ===========================================================================

class ClientIdTests(unittest.TestCase):

    def test_round_trip_on_every_tab(self):
        for mid in (1, 201, 10001, missions.MAX_ID):
            for tab in missions.TABS:
                value = missions.client_id(mid, tab)
                self.assertGreater(value, 0)
                self.assertLessEqual(value, 2 ** 31 - 1)
                self.assertEqual((mid, tab), missions.split_client_id(value))

    def test_garbage_ids_are_refused(self):
        for value in (0, -2, -1, 2010, 2014, 2019, "x", None):
            self.assertIsNone(missions.split_client_id(value), value)


class ChainTests(unittest.TestCase):

    def test_done_missions_leave_the_table_but_low_level_ones_stay(self):
        rules = [mission(1, character=0), mission(2, character=0, level=99),
                 mission(3, character=0, enabled=False)]
        state = {"done": {"1": {"p": "2026-10-30", "at": "", "times": 1}},
                 "progress": {}}
        chains = missions.player_chains(rules, state, now=NOON)
        self.assertEqual([2], [m["id"] for m in chains[("pvp", 0)]])
        self.assertEqual([], chains[("pvp", 1)])

    def test_missions_for_anyone_show_on_every_tab_in_order(self):
        rules = [mission(5, order=2), mission(3, order=1), mission(4, order=1),
                 mission(9, character=2, order=0)]
        chains = missions.player_chains(rules, missions.empty_state(), now=NOON)
        self.assertEqual([3, 4, 5], [m["id"] for m in chains[("pvp", 0)]])
        self.assertEqual([3, 4, 5], [m["id"] for m in chains[("pvp", 1)]])
        self.assertEqual([9, 3, 4, 5], [m["id"] for m in chains[("pvp", 2)]])

    def test_a_chain_holds_at_most_fifty_nine(self):
        rules = [mission(i) for i in range(1, 80)]
        chains = missions.player_chains(rules, missions.empty_state(), now=NOON)
        self.assertEqual(missions.MAX_RANK, len(chains[("pvp", 0)]))
        self.assertEqual(59, missions.MAX_RANK)

    def test_heads_follow_the_wire_order_and_respect_pins(self):
        rules = [mission(1, character=0, target=3), mission(2, character=0),
                 mission(3, character=0, level=50),
                 mission(4, character=1, line="quest", kind="clear")]
        state = {"done": {}, "progress": {"2": {"p": "2026-10-30", "n": 0},
                                          "1": {"p": "2026-10-30", "n": 2}}}
        chains = missions.player_chains(rules, state, now=NOON)
        heads = missions.chain_heads(chains, state, level=10, now=NOON)
        # 线序：页签 0 (对战, 闯关)、页签 1 (对战, 闯关)、页签 2 (对战, 闯关)。
        self.assertEqual([(0, 11, 2), (0, 0, 0), (0, 0, 0), (0, 42, 0),
                          (0, 0, 0), (0, 0, 0)], heads)
        pinned = missions.chain_heads(chains, state, level=10, now=NOON,
                                      pins={("pvp", 0): 2})
        self.assertEqual((0, 21, 0), pinned[0])
        # 钉住的那条等级不够 ⇒ 不算，退回第一条等级够的。
        too_high = missions.chain_heads(chains, state, level=10, now=NOON,
                                        pins={("pvp", 0): 3})
        self.assertEqual((0, 11, 2), too_high[0])

    def test_status_and_popup_payload_sizes(self):
        heads = [(0, 11, 2)] * 6
        payload = missions.build_status(heads)
        self.assertEqual(72, len(payload))
        self.assertEqual(heads, missions.parse_status(payload))
        with self.assertRaises(ValueError):
            missions.build_status(heads[:5])
        popup = missions.build_complete(1, "quest", 42, 0, 0, True)
        self.assertEqual(24, len(popup))
        self.assertEqual((1, 2, 42, 0, 0, 1), struct.unpack("<6i", popup))
        self.assertEqual(42, missions.parse_request_id(struct.pack("<i", 42)))
        self.assertIsNone(missions.parse_request_id(b"\x01\x02"))


# ===========================================================================
# 客户端 ini：生成 → 客户端加载器镜像读回
# ===========================================================================

class ClientIniTests(unittest.TestCase):

    def test_the_factory_table_round_trips_through_the_loader_mirror(self):
        rules = missions.validate_missions(shopdefaults.default_missions())
        chains = missions.player_chains(rules, missions.empty_state(), now=NOON)
        data = missions.build_client_ini(chains)
        self.assertEqual(b"\xff\xfe", data[:2])
        records = missions.parse_client_ini(data)
        self.assertEqual(105, len(records))
        by_id = dict((m["id"], m) for m in rules)
        for rec in records:
            parsed = missions.split_client_id(rec["PromotionID"])
            self.assertIsNotNone(parsed, rec["section"])
            mid, tab = parsed
            self.assertEqual(rec["tab"], tab)
            self.assertEqual(rec["CharacterLimit"], tab)
            m = by_id[mid]
            self.assertEqual(missions.sanitize(missions.display_name(m)), rec["Name"])
            self.assertEqual(int(m["level"]), rec["UserLevelLimit"])
            self.assertLess(rec["UserLevelLimit"], 1000, "1011~1016 那批要 % 1000")
            self.assertEqual(missions.LINE_CONTEXT[m["line"]],
                             rec["PromotionGameContextType"])

    def test_ranks_start_at_one_and_follow_the_chain_order(self):
        rules = [mission(7, order=5), mission(3, order=9)]
        chains = missions.player_chains(rules, missions.empty_state(), now=NOON)
        records = [r for r in missions.parse_client_ini(missions.build_client_ini(chains))
                   if r["tab"] == 0]
        self.assertEqual([(71, 1), (31, 2)],
                         [(r["PromotionID"], r["PromotionLevel"]) for r in records])

    def test_text_cannot_break_the_ini_or_the_printf_status(self):
        m = mission(1, name="#一 %d 二\n[假小节]", info="说明 # 后半句",
                    clear_text="100%")
        chains = missions.player_chains([m], missions.empty_state(), now=NOON)
        data = missions.build_client_ini(chains)
        records = missions.parse_client_ini(data)
        self.assertEqual(3, len(records), "一节都不能多也不能少")
        rec = records[0]
        self.assertEqual("＃一 ％d 二 [假小节]", rec["Name"])
        self.assertEqual("说明 ＃ 后半句", rec["TextMissionInfo"])
        self.assertEqual("100％", rec["TextClearCondition"])
        self.assertEqual("今日 %d/1胜", rec["TextStatus"])
        self.assertEqual(1, rec["TextStatus"].count("%"))

    def test_status_text_per_period_and_kind(self):
        self.assertEqual("今日 %d/3胜", missions.status_text(
            {"period": "daily", "kind": "win", "target": 3}))
        self.assertEqual("本周 %d/500", missions.status_text(
            {"period": "weekly", "kind": "stats", "count": "sum", "target": 500}))
        self.assertEqual("%d/1次", missions.status_text(
            {"period": "once", "kind": "clear", "target": 1}))

    def test_sanitize(self):
        self.assertEqual("a＃b％c d e", missions.sanitize("  a#b%c\n\td\x7f e  "))
        self.assertEqual("", missions.sanitize(None))

    @unittest.skipUnless(os.path.isfile(ORIGINAL_INI), "缺原版 Promotion-chn.ini")
    def test_the_original_ini_reads_105_records(self):
        with open(ORIGINAL_INI, "rb") as fp:
            records = missions.parse_client_ini(fp.read())
        self.assertEqual(105, len(records))
        levels = sorted(set(r["UserLevelLimit"] for r in records))
        self.assertIn(1011, levels, "国服表那批 1011~1016 还在（原版里永远不出现）")

    def test_the_loader_mirror_rejects_what_the_client_rejects(self):
        good = ("﻿[타이_PVP_레벨1]\r\nPromotionID=11\r\nPromotionLevel=1\r\n"
                "CharacterLimit=0\r\nPromotionGameContextType=1\r\nPromotionType=2\r\n")
        self.assertEqual(1, len(missions.parse_client_ini(good.encode("utf-16-le"))))
        missing = good.replace("PromotionType=2\r\n", "")
        with self.assertRaises(missions.ClientRejects):
            missions.parse_client_ini(missing.encode("utf-16-le"))
        preceding = good + "PrecedingPromotionNumber=3\r\n"
        with self.assertRaises(missions.ClientRejects):
            missions.parse_client_ini(preceding.encode("utf-16-le"))
        no_id = good.replace("PromotionID=11\r\n", "")
        self.assertEqual([], missions.parse_client_ini(no_id.encode("utf-16-le")))


class TableFrameTests(unittest.TestCase):

    def test_round_trip_and_chunking(self):
        for size in (0, 1, missions.TABLE_CHUNK, missions.TABLE_CHUNK + 1, 100000):
            data = bytes((i * 7) & 0xFF for i in range(size))
            frames = missions.build_table_frames(data, 9)
            self.assertEqual(max(1, -(-size // missions.TABLE_CHUNK)), len(frames))
            for frame in frames:
                self.assertLessEqual(len(frame), missions.TABLE_HEADER.size
                                     + missions.TABLE_CHUNK)
                self.assertLess(len(frame), 0xFFFF, "帧载荷长是 u16")
            self.assertEqual((9, data), missions.parse_table_frames(frames))
            head = missions.TABLE_HEADER.unpack_from(frames[0], 0)
            self.assertEqual(zlib.crc32(data) & 0xFFFFFFFF, head[3])

    def test_broken_frames_are_refused(self):
        data = bytes(range(256)) * 400
        frames = missions.build_table_frames(data, 3)
        self.assertEqual(4, len(frames))
        cases = {
            "没有第 0 片": frames[1:],
            "少一片": frames[:-1],
            "片号跳": [frames[0], frames[2], frames[3]],
            "数据坏了": frames[:-1] + [frames[-1][:-1] + b"\x00"],
            "格式号": [struct.pack("<H", 2) + frames[0][2:]] + frames[1:],
            "片头不完整": [frames[0][:10]],
        }
        for name, bad in cases.items():
            with self.assertRaises(ValueError, msg=name):
                missions.parse_table_frames(bad)

    def test_a_new_index_zero_restarts_the_table(self):
        first = missions.build_table_frames(b"A" * 70000, 1)
        second = missions.build_table_frames(b"B" * 50000, 2)
        self.assertEqual((2, b"B" * 50000),
                         missions.parse_table_frames(first[:1] + second))


# ===========================================================================
# 校验
# ===========================================================================

class ValidateTests(unittest.TestCase):

    def bad(self, **over):
        entry = {"id": 1, "line": "pvp", "kind": "win", "rewards": [{"money": 1}]}
        entry.update(over)
        with self.assertRaises(shopcfg.ConfigError, msg=repr(over)):
            missions.validate_missions({"missions": [entry]})

    def test_defaults_fill_in(self):
        m = missions.validate_missions({"missions": [
            {"id": 1, "line": "pvp", "kind": "win", "rewards": [{"money": 1}]}]})[0]
        self.assertEqual((False, "once", missions.CHAR_ANY, 0, 1, 1, "", "", ""),
                         (m["enabled"], m["period"], m["character"], m["order"],
                          m["level"], m["target"], m["name"], m["info"],
                          m["clear_text"]))

    def test_shape_errors(self):
        for raw in (None, [], {"missions": "x"}, {"missions": ["x"]}):
            with self.assertRaises(shopcfg.ConfigError):
                missions.validate_missions(raw)
        with self.assertRaises(shopcfg.ConfigError):
            missions.validate_missions({"missions": [
                {"id": 1, "line": "pvp", "kind": "win", "rewards": [{"money": 1}]},
                {"id": 1, "line": "pvp", "kind": "win", "rewards": [{"money": 1}]}]})

    def test_field_errors(self):
        self.bad(id=0)
        self.bad(id=missions.MAX_ID + 1)
        self.bad(period="yearly")
        self.bad(character=3)
        self.bad(line="arena")
        self.bad(kind="kill")
        self.bad(line="quest", kind="win")
        self.bad(kind="clear")
        self.bad(target=0)
        self.bad(level=0)
        self.bad(pvp_mode=1)
        self.bad(item_mode=2)
        self.bad(name="名" * (missions.TEXT_LIMITS["name"] + 1))
        self.bad(name=5)

    def test_kind_specific_errors(self):
        self.bad(line="quest", kind="clear", stage=99)
        self.bad(line="quest", kind="clear", difficulty=9)
        self.bad(line="quest", kind="map", map="QuestLevel01", script="x", id=10001)
        self.bad(line="quest", kind="map", map="", script="x")
        self.bad(kind="stats", count="avg")
        self.bad(kind="stats", count="sum", metric="accuracy")
        self.bad(kind="stats", count="sum", metric="deaths", weapon=110001)
        self.bad(kind="stats", count="sum", metric="kills", weapon=12345)
        self.bad(kind="stats", count="match", conditions=[])
        self.bad(kind="stats", count="match", conditions=[
            {"scope": "total", "metric": "kills", "op": "ge", "threshold": 5}])

    def test_reward_errors(self):
        self.bad(rewards=[])
        self.bad(rewards=[{"money": 1}] * 4)
        self.bad(rewards=[{"money": 0}])
        self.bad(rewards=[{"money": 1, "exp": 1}])
        self.bad(rewards=[{"item": 999999}])
        self.bad(rewards=["x"])

    def test_equipment_is_always_one_and_stacks_keep_their_count(self):
        m = missions.validate_missions({"missions": [
            {"id": 1, "line": "pvp", "kind": "win",
             "rewards": [{"item": SHIRT, "count": 5},
                         {"item": BRONZE_PIPE, "count": 5}]}]})[0]
        self.assertEqual([{"item": SHIRT, "count": 1},
                          {"item": BRONZE_PIPE, "count": 5}], m["rewards"])

    def test_filters_of_the_other_line_are_dropped(self):
        m = missions.validate_missions({"missions": [
            {"id": 1, "line": "pvp", "kind": "win", "stage": 3, "difficulty": 2,
             "rewards": [{"money": 1}]}]})[0]
        self.assertNotIn("stage", m)
        self.assertNotIn("difficulty", m)

    def test_next_id_never_reuses(self):
        self.assertEqual(10001, missions.next_id([]))
        self.assertEqual(10001, missions.next_id([{"id": 5}]))
        self.assertEqual(12001, missions.next_id([{"id": 12000}, {"id": 3}]))


class FactoryDefaultTests(unittest.TestCase):

    def test_the_105_original_missions(self):
        notes = []
        rules = missions.validate_missions(shopdefaults.default_missions(notes.append))
        self.assertEqual(105, len(rules))
        self.assertEqual([], notes, "原版 105 条一条都不该被丢 / 截断")
        self.assertEqual({"once"}, set(m["period"] for m in rules))
        self.assertTrue(all(m["enabled"] for m in rules))
        self.assertTrue(all(m["level"] < 1000 for m in rules), "等级要 % 1000")
        self.assertFalse(any(m.get("harder") for m in rules), "难度一律恰好")
        self.assertTrue(all(1 <= len(m["rewards"]) <= missions.MAX_REWARDS
                            for m in rules))
        kinds = {}
        for m in rules:
            kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
        self.assertEqual({"win": 21, "clear": 63, "map": 21}, kinds)
        self.assertTrue(all(m["id"] < missions.FIRST_NEW_ID for m in rules))

    def test_shopcfg_serves_the_factory_file(self):
        self.assertEqual(105, len(shopcfg.default_missions()["missions"]))


# ===========================================================================
# 结算端到端（照 `test_battle._CardSettlementCase` 的地基）
# ===========================================================================

class _MissionSettlementCase(BattleRoom):
    """一份临时 `missions.json` + 闯关房（关卡 3 · 难度 1，alice = 泰尔、bob = 卡希尔）。"""

    RULES = ()

    def setUp(self):
        import tempfile
        # ★ 运营事件日志掐掉（同 `test_gameserver` / `test_web_admin`）：回大厅那一发
        #   `0x0700` 会记一行，落点是**真实的** `logs/online.log`。
        saved_online = gameserver.eventlog.online
        gameserver.eventlog.online = lambda _msg: None
        self.addCleanup(setattr, gameserver.eventlog, "online", saved_online)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved_dir = shopcfg.DATA_DIR
        shopcfg.DATA_DIR = self.tmp.name
        self.addCleanup(shopcfg.invalidate)
        self.addCleanup(setattr, shopcfg, "DATA_DIR", saved_dir)
        self.write_missions(list(self.RULES))
        super().setUp()
        self.chats = {}
        for conn in (self.alice, self.bob):
            said = self.chats.setdefault(conn.account_name, [])
            conn.send_system_chat = said.append

    def write_missions(self, rules):
        shopcfg.write_json(shopcfg.path_of(shopcfg.MISSIONS_FILENAME, self.tmp.name),
                           {"format": 1, "missions": list(rules)})
        shopcfg.invalidate()

    def clear_quest(self):
        gameserver.Conn.on_game_packet(self.alice, gameserver.OP_MARK_QUEST_SUCCESS,
                                       gameserver.w_i32(1))

    def end(self):
        gameserver.Conn.on_game_packet(self.alice, gameserver.OP_END_QUEST, b"")

    def saved(self, name):
        return account_store.mission_state(self.accounts.saved[name])

    def gifts(self, name):
        return [g for g in self.accounts.saved[name].get("gifts") or ()]


def raw(mid, **over):
    entry = {"id": mid, "enabled": True, "period": "daily", "line": "quest",
             "kind": "clear", "target": 1, "rewards": [{"money": 100}]}
    entry.update(over)
    return entry


class MissionSettlementTests(_MissionSettlementCase):

    RULES = (
        raw(1, name="通关一次"),                                  # 不限
        raw(2, stage=3, difficulty=1, rewards=[{"exp": 30},
                                               {"item": BRONZE_PIPE, "count": 2}]),
        raw(3, stage=2),                                           # 关卡不对
        raw(4, target=2),                                          # 进度 1/2
        raw(5, character=1),                                       # 只有 bob
        raw(6, level=99),                                          # 等级不够
        raw(7, difficulty=2),                                      # 难度恰好：不中
        raw(8, line="pvp", kind="win"),                            # 线不对
    )

    def test_a_clear_completes_every_matching_mission_at_once(self):
        self.clear_quest()
        self.end()
        alice = self.saved("alice")
        self.assertEqual({"1", "2"}, set(alice["done"]))
        self.assertEqual({"4": 1}, dict((k, v["n"]) for k, v in alice["progress"].items()))
        bob = self.saved("bob")
        self.assertEqual({"1", "2", "5"}, set(bob["done"]))

    def test_the_rewards_go_into_the_gift_box_one_gift_each(self):
        self.clear_quest()
        self.end()
        gifts = self.gifts("alice")
        self.assertEqual([(0, 0, 100, 0), (0, 0, 0, 30), (BRONZE_PIPE, 2, 0, 0)],
                         [(g["item"], g["count"], g["money"], g["exp"]) for g in gifts])
        self.assertEqual({"GM"}, set(g["sender"] for g in gifts),
                         "发送人必须是 GM —— 别的名字客户端会走「答谢」流程（D117）")
        self.assertEqual("完成晋级任务「通关一次」的奖励", gifts[0]["message"])
        self.assertIn(gameserver.OP_GIFT_ARRIVED, opcodes(self.alice))

    def test_only_the_player_hears_about_their_missions(self):
        self.clear_quest()
        self.end()
        alice = "\n".join(self.chats["alice"])
        self.assertIn("「通关一次」完成", alice)
        self.assertIn("进度 1/2", alice)
        self.assertNotIn("#5", alice)
        bob = "\n".join(self.chats["bob"])
        self.assertEqual(3, bob.count("完成"))

    def test_a_status_frame_follows_the_settlement(self):
        self.clear_quest()
        self.end()
        status = bodies(self.alice, gameserver.OP_PROMO_STATUS)
        self.assertTrue(status, "结算后要发 0x02e0 对齐面板")
        self.assertEqual(72, len(status[-1]))
        self.assertEqual([], bodies(self.alice, gameserver.OP_PROMO_COMPLETE),
                         "完成弹窗要等回大厅再补，结算界面上不弹")
        self.assertEqual(2, len(self.alice.promo_popups))

    def test_a_failed_quest_advances_no_clear_mission(self):
        self.end()
        self.assertEqual({"done": {}, "progress": {}}, self.saved("alice"))
        self.assertEqual([], self.gifts("alice"))
        self.assertEqual([], self.chats["alice"])

    def test_back_in_the_lobby_the_popups_come_before_the_new_table(self):
        self.clear_quest()
        self.end()
        self.alice.hook_promo_crc = 0          # 登录时推过一张表
        self.alice.reload_account = lambda: None
        self.alice.account = dict(self.accounts.saved["alice"])
        self.alice.sent.clear()
        gameserver.Conn.on_game_packet(self.alice, gameserver.OP_REQ_INVENTORY, b"")
        ops = [op for op in opcodes(self.alice)
               if op in (gameserver.OP_PROMO_COMPLETE, gameserver.OP_HOOK_PROMO_TABLE,
                         gameserver.OP_PROMO_STATUS)]
        self.assertEqual([gameserver.OP_PROMO_COMPLETE] * 2, ops[:2])
        self.assertIn(gameserver.OP_HOOK_PROMO_TABLE, ops[2:])
        self.assertEqual(gameserver.OP_PROMO_STATUS, ops[-1])
        tab, context, done_id, _cur, _prog, completed = struct.unpack(
            "<6i", bodies(self.alice, gameserver.OP_PROMO_COMPLETE)[0])
        self.assertEqual((0, 2, missions.client_id(1, 0), 1),
                         (tab, context, done_id, completed))
        # 新表里没有完成的那两条。
        frames = bodies(self.alice, gameserver.OP_HOOK_PROMO_TABLE)
        _serial, data = missions.parse_table_frames(frames)
        ids = set(missions.split_client_id(r["PromotionID"])[0]
                  for r in missions.parse_client_ini(data))
        self.assertNotIn(1, ids)
        self.assertNotIn(2, ids)
        self.assertIn(4, ids)
        self.assertIsNone(self.alice.promo_popups or None)

    def test_the_same_day_pays_only_once(self):
        self.clear_quest()
        self.end()
        before = len(self.gifts("alice"))
        for conn in (self.alice, self.bob):
            conn.sent.clear()
            conn.settled = False
            conn.account = dict(self.accounts.saved[conn.account_name])
        self.room.quest = gameserver.RoomQuest(seats=[0, 1])
        self.clear_quest()
        self.end()
        after = self.gifts("alice")
        # 第二局只多了 #4 那一条（1/2 → 2/2）的那一份。
        self.assertEqual(before + 1, len(after))
        self.assertEqual({"1", "2", "4"}, set(self.saved("alice")["done"]))


class RewardBlockTests(_MissionSettlementCase):
    """★ 不计奖励的那一局（X19 的 bot 受限闩）任务一条都不推进 —— 任务奖励也是奖励。"""

    RULES = (raw(1),)

    def test_a_blocked_game_advances_nothing(self):
        # 本局的闩（开局 / 局中敲命令时记下的那一个）。闯关房平时恒为 None ——
        # 这里直接把它拨上，验的是「闩上了就一条都不推进」这一支。
        self.room.quest.bot_limit_reason = "开局时 测试"
        self.clear_quest()
        self.end()
        self.assertEqual({"done": {}, "progress": {}}, self.saved("alice"))
        self.assertEqual([], self.gifts("alice"))
        self.assertEqual([], bodies(self.alice, gameserver.OP_PROMO_STATUS))

    def test_the_same_game_unblocked_does_advance(self):
        """对照组：同一份规则不闩就完成 —— 上面那条红不是因为规则本身不中。"""
        self.clear_quest()
        self.end()
        self.assertEqual({"1"}, set(self.saved("alice")["done"]))


if __name__ == "__main__":
    unittest.main()
