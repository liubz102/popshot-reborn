#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`server/cards.py` —— 称号卡片该发谁几张（用户 2026-09-13，V0.3商店）。

★ 这一组里最要紧的是 `TotalScopeTests`：「玩家累计」是**攒够就归零、
重新一轮**（用户 2026-09-13 第三轮原话：「发过奖励后，两个计数器同时归零，
重新开始新一轮计数」）。两条性质缺一不可 ——

    攒够才发          进度 = 现在的累计值 − 上次归零时的值
    发完当场清零      所以一次结算最多发一张，没有循环、也不需要「上限」

这两条都是「错了也不会报错、只会在几个月后变成一堆解释不清的卡片」的那一类。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cards                                                   # noqa: E402
import shopcfg                                                 # noqa: E402
import shopdefaults                                            # noqa: E402


def cond(metric="kills", op=shopcfg.CARD_OP_GE, threshold=1, **kw):
    """一条达成条件，只写关心的那几格。"""
    out = {"scope": shopcfg.CARD_SCOPE_MATCH, "metric": metric, "op": op,
           "threshold": threshold}
    out.update(kw)
    return out


#: 「并且赢了 / 通关了」—— 旧的 `win_only` 开关现在就是这一条条件。
WON = cond("won", shopcfg.CARD_OP_EQ, 1, join=shopcfg.CARD_JOIN_AND)


def rule(card, *conditions, **kw):
    """一条规则。不写条件时给一条「击杀数 ≥ 1」顶着。"""
    out = {"card": card, "listed": True,
           "conditions": list(conditions) or [cond()]}
    out.update(kw)
    return out


def grants(rules, *, mode="pvp", stage=None, difficulty=None,
           match=None, total=None, bases=None):
    give, new_bases, warnings = cards.due_grants(
        rules, mode=mode, stage=stage, difficulty=difficulty,
        match=match or {}, total=total or {}, bases=bases or {})
    return give, new_bases, warnings


class MetricTableTests(unittest.TestCase):
    """指标表本身要自洽 —— 它是画面、校验器和判定三处共同的契约。"""

    def test_every_metric_has_a_chinese_name_and_a_sane_shape(self):
        for key, spec in shopcfg.CARD_METRICS.items():
            label, scopes, weapon, help_text = spec
            self.assertTrue(label, key)
            self.assertTrue(help_text, key)
            self.assertIsInstance(weapon, bool, key)
            for scope in scopes:
                self.assertIn(scope, shopcfg.CARD_SCOPES, key)

    def test_the_dropdown_order_covers_every_metric_exactly_once(self):
        """★ 下拉不是字母序（按 key 排在中文下是乱的）——
        漏登记一个指标不该让它从画面上消失，所以兜底也要验。"""
        keys = shopcfg.card_metric_keys()
        self.assertEqual(sorted(shopcfg.CARD_METRICS), sorted(keys))
        self.assertEqual(len(keys), len(set(keys)))
        for key in shopcfg.CARD_METRIC_ORDER:
            self.assertIn(key, shopcfg.CARD_METRICS, key)

    def test_only_the_counters_that_really_exist_can_be_split_by_weapon(self):
        """★★ 「能按武器细分」那一列的**判据是计数器本身**，不是表里怎么写的。

        `RoomQuest.weapon_stat()` 只记 shots / kills / damage 三样
        （`rpExplode` 里没有武器 id，命中归不到枪上）。第一稿把 `hits` /
        `splash_hits` / `accuracy` 也标成能分武器，可 `hits@族号` **从来
        没被写过** —— 那几条规则配得出来、取到的永远是 0、永远不发卡、
        也不报错。这条用例就是不让它飘回来。
        """
        import gameserver
        counters = set(gameserver.RoomQuest().weapon_stat(0, 110001))
        splittable = {key for key in shopcfg.CARD_METRICS
                      if shopcfg.card_metric_needs_weapon(key)}
        self.assertEqual(counters, splittable)

    def test_the_retired_metrics_are_really_gone_and_say_what_to_use(self):
        """砍掉的 6 条不许还留在表里，而且报错要说得出「改用什么」。"""
        for key, advice in shopcfg.RETIRED_CARD_METRICS.items():
            self.assertNotIn(key, shopcfg.CARD_METRICS, key)
            self.assertTrue(advice.strip(), key)

    def test_a_retired_metric_is_refused_with_an_explanation(self):
        with self.assertRaises(shopcfg.ConfigError) as caught:
            shopcfg.validate_cards(
                {"rules": [rule(60001, cond("perfect_win"))]})
        self.assertIn("死亡次数", str(caught.exception))

    def test_the_only_enum_metric_is_this_match_result(self):
        """★ 「本局结果」是全表唯一的枚举指标：值只有 0 / 1，比较符固定
        「等于」，条件行画成下拉而不是数字框。"""
        self.assertEqual(["won"], sorted(shopcfg.CARD_ENUM_METRICS))
        values = shopcfg.card_metric_values("won")
        self.assertEqual({0, 1}, {value for value, _name in values})
        for _value, name in values:
            self.assertTrue(name.strip())
        self.assertEqual((), shopcfg.card_metric_values("kills"))

    def test_the_match_result_and_the_win_count_share_one_counter(self):
        """★★ 「本局结果」和「胜利 / 通关次数」读的是**同一格** `win`
        —— 存档里不会因为多一个指标就多一个计数器。"""
        self.assertEqual("win", shopcfg.card_metric_stat("won"))
        self.assertEqual("win", cards.stat_key("won"))
        self.assertEqual("kills", shopcfg.card_metric_stat("kills"))
        self.assertEqual((shopcfg.CARD_SCOPE_MATCH,),
                         shopcfg.card_metric_scopes("won"))
        self.assertEqual((shopcfg.CARD_SCOPE_TOTAL,),
                         shopcfg.card_metric_scopes("win"))

    def test_the_weapon_cards_are_the_nine_weapon_families(self):
        import weapondata
        self.assertEqual(tuple(weapondata.WEAPON_ROH), shopcfg.WEAPON_CARDS)
        for roh in shopcfg.WEAPON_CARDS:
            self.assertIn(roh, shopcfg.WEAPON_ROH_ZH)

    def test_the_weapon_names_match_the_design_table(self):
        """武器族的中文名和 `shopdefaults.WEAPON_BASE_ZH` 那一份是同一套说法。"""
        wanted = set(shopdefaults.WEAPON_BASE_ZH.values())
        for name in shopcfg.WEAPON_ROH_ZH.values():
            self.assertIn(name, wanted, name)


class StatKeyTests(unittest.TestCase):

    def test_the_weapon_dimension_folds_into_the_key(self):
        self.assertEqual("kills", cards.stat_key("kills"))
        self.assertEqual("kills@110001", cards.stat_key("kills", 110001))

    def test_no_mode_means_both_modes_added_up(self):
        stats = {"pvp": {"kills": 3}, "quest": {"kills": 4}}
        self.assertEqual(3, cards.stat_of(stats, "pvp", "kills"))
        self.assertEqual(7, cards.stat_of(stats, None, "kills"))

    def test_only_the_ratio_metrics_are_derived(self):
        """★ 除了比率，取值就是「取一格」——「哪些是派生的」只有一处说法。

        多一个派生指标就多一支特判（`stat_of` + `_match_value` 两处），
        所以这一条钉着：`CARD_RATIO_METRICS` 之外的每一个都必须是原始计数。
        """
        stats = {"pvp": {"shots": 40, "hits": 10}}
        for metric in shopcfg.CARD_METRICS:
            if shopcfg.card_metric_is_ratio(metric):
                continue
            got = cards.stat_of(stats, "pvp", metric)
            self.assertEqual(stats["pvp"].get(metric, 0), got, metric)

    def test_a_ratio_is_computed_from_the_two_stored_parts(self):
        """★★ **比率绝不许存**：两局 50% 和 100% 加起来会变成 150%。
        累计里只有 `hits` / `shots`，比率是读的时候才除的。"""
        stats = {"pvp": {"shots": 40, "hits": 10}}
        self.assertEqual(25, cards.stat_of(stats, "pvp", "accuracy"))
        self.assertEqual(0, cards.stat_of({}, "pvp", "accuracy"),
                         "一枪没开也不许除零")
        merged = cards.merge_stats(stats, "pvp", {"shots": 40, "hits": 30})
        self.assertNotIn("accuracy", merged["pvp"])
        self.assertEqual(50, cards.stat_of(merged, "pvp", "accuracy"),
                         "40/10 + 40/30 = 80/40 = 50%，不是 25+75")

    def test_merge_only_keeps_non_zero(self):
        merged = cards.merge_stats({"pvp": {"kills": 2, "deaths": 0}},
                                   "pvp", {"kills": 3, "guards": 0})
        self.assertEqual({"pvp": {"kills": 5}}, merged)

    def test_merge_does_not_touch_the_input(self):
        before = {"pvp": {"kills": 2}}
        cards.merge_stats(before, "pvp", {"kills": 3})
        self.assertEqual({"pvp": {"kills": 2}}, before)


class MatchScopeTests(unittest.TestCase):
    """「一局内」：达标就给，一局一次机会，固定一张。"""

    def test_reaching_the_threshold_gives_the_card(self):
        give, _b, _w = grants([rule(60001, cond("guards", threshold=30))],
                              match={"guards": 30})
        self.assertEqual({60001: shopcfg.CARD_GRANT_COUNT}, give)

    def test_one_short_gives_nothing(self):
        give, _b, _w = grants([rule(60001, cond("guards", threshold=30))],
                              match={"guards": 29})
        self.assertEqual({}, give)

    def test_a_rule_that_is_switched_off_never_fires(self):
        """★ 这是运营的急刹车：17 张全关掉 = 整个功能停掉，不用重启不用发版。"""
        give, _b, _w = grants([rule(60001, listed=False)],
                              match={"kills": 99})
        self.assertEqual({}, give)

    def test_the_wrong_mode_never_fires(self):
        give, _b, _w = grants([rule(60001, mode="quest")], mode="pvp",
                              match={"kills": 9})
        self.assertEqual({}, give)

    def test_no_mode_means_either_mode(self):
        for mode in ("pvp", "quest"):
            give, _b, _w = grants([rule(60001)], mode=mode,
                                  match={"kills": 9})
            self.assertEqual({60001: 1}, give, mode)

    def test_stage_and_difficulty_narrow_it_down(self):
        only = [rule(60001, mode="quest", stage=3, difficulty=2)]
        give, _b, _w = grants(only, mode="quest", stage=3, difficulty=2,
                              match={"kills": 1})
        self.assertEqual({60001: 1}, give)
        give, _b, _w = grants(only, mode="quest", stage=3, difficulty=1,
                              match={"kills": 1})
        self.assertEqual({}, give)

    def test_the_match_result_is_just_another_condition(self):
        """★ 旧的「必须胜利 / 通关」开关现在是一行条件（用户 2026-09-13）。
        `match_stats` 每局写的那一格 `win` 就是它读的东西。"""
        only = [rule(60001, cond(), WON)]
        self.assertEqual({}, grants(only, match={"kills": 5})[0])
        self.assertEqual({60001: 1},
                         grants(only, match={"kills": 5, "win": 1})[0])
        # 反过来也配得出来：「输了才给」。
        lost = [rule(60001, cond(), cond("won", shopcfg.CARD_OP_EQ, 0,
                                        join=shopcfg.CARD_JOIN_AND))]
        self.assertEqual({60001: 1}, grants(lost, match={"kills": 5})[0])
        self.assertEqual({}, grants(lost, match={"kills": 5, "win": 1})[0])

    def test_the_weapon_dimension_reads_the_weapon_specific_counter(self):
        """「武器」是条件上的维度：指标还是「击杀数」，只是换个统计键。"""
        only = [rule(110001, cond("kills", weapon=110001, threshold=5))]
        self.assertEqual({}, grants(only, match={"kills": 9})[0])
        self.assertEqual({110001: 1},
                         grants(only, match={"kills@110001": 5})[0])

    def test_the_three_comparisons_each_cut_a_different_way(self):
        """三个方向各走两遍 —— 同一个指标、同一个阈值 3，结果两两不同。"""
        cases = (("ge", 2, {}), ("ge", 3, {60001: 1}), ("ge", 4, {60001: 1}),
                 ("le", 2, {60001: 1}), ("le", 3, {60001: 1}), ("le", 4, {}),
                 ("eq", 2, {}), ("eq", 3, {60001: 1}), ("eq", 4, {}))
        for op, deaths, wanted in cases:
            only = [rule(60001, cond("deaths", op, 3))]
            self.assertEqual(wanted, grants(only, match={"deaths": deaths})[0],
                             "%s %d" % (op, deaths))

    def test_zero_is_a_real_condition_not_a_missing_one(self):
        """★ 「一次都没死」= `eq 0`。本局战绩里根本没有 `deaths` 这一格
        （`match_stats` 只留非零项），取不到就是 0 —— 正好成立。"""
        only = [rule(60001, cond("deaths", "eq", 0))]
        self.assertEqual({60001: 1}, grants(only, match={"kills": 3})[0])
        self.assertEqual({}, grants(only, match={"deaths": 1})[0])

    def test_the_old_flag_metrics_are_reproduced_by_the_new_pieces(self):
        """★★ 拆开之后要和旧的**一模一样**。

        旧 `perfect_win` = 赢了且零死亡、旧 `carried` = 赢了且零击杀。
        「赢了」那一半现在是一条 `won eq 1` 的条件 —— 漏了就变成
        「输了没死也给」，这条用例正是钉着那一半。
        """
        perfect = [rule(60001, cond("deaths", "eq", 0), WON)]
        self.assertEqual({60001: 1}, grants(perfect, match={"win": 1})[0])
        self.assertEqual({}, grants(perfect, match={})[0])
        self.assertEqual({}, grants(perfect, match={"win": 1, "deaths": 1})[0])
        carried = [rule(60005, cond("kills", "eq", 0), WON)]
        self.assertEqual({60005: 1}, grants(carried, match={"win": 1})[0])
        self.assertEqual({}, grants(carried, match={"win": 1, "kills": 1})[0])

    def test_an_unknown_metric_is_skipped_with_a_warning(self):
        give, _b, warnings = grants([rule(60001, cond("nonsense"))],
                                    match={"kills": 9})
        self.assertEqual({}, give)
        self.assertTrue(warnings)

    def test_a_rule_with_no_conditions_gives_nothing_and_says_so(self):
        """★ 一条条件都没有 = 说不出「什么时候给」⇒ 不发卡（画面上那句
        写的正是「条件无效」）。手改进来的文件也得兜住。"""
        give, _b, warnings = grants([{"card": 60001, "listed": True,
                                      "conditions": []}], match={"kills": 9})
        self.assertEqual({}, give)
        self.assertTrue(warnings)


class BooleanExpressionTests(unittest.TestCase):
    """★★ 多条件 and / or —— **从上往下依次结合，没有括号**（用户拍板）。"""

    def holds(self, conditions, match):
        return grants([rule(60001, *conditions)], match=match)[0] == {60001: 1}

    def test_and_needs_both(self):
        two = [cond("kills", threshold=5),
               cond("deaths", "eq", 0, join=shopcfg.CARD_JOIN_AND)]
        self.assertTrue(self.holds(two, {"kills": 5}))
        self.assertFalse(self.holds(two, {"kills": 4}))
        self.assertFalse(self.holds(two, {"kills": 5, "deaths": 1}))

    def test_or_takes_either(self):
        two = [cond("kills", threshold=5),
               cond("hits", threshold=30, join=shopcfg.CARD_JOIN_OR)]
        self.assertTrue(self.holds(two, {"kills": 5}))
        self.assertTrue(self.holds(two, {"hits": 30}))
        self.assertFalse(self.holds(two, {"kills": 4, "hits": 29}))

    def test_it_folds_left_to_right_not_and_before_or(self):
        """★★ `A 或者 B 并且 C` 算的是 `(A 或者 B) 并且 C`，**不是**
        程序语言里那个「与优先于或」—— 运营不该被要求知道优先级，
        说明文里也是照这个顺序把括号写出来的。"""
        three = [cond("kills", threshold=5),
                 cond("hits", threshold=30, join=shopcfg.CARD_JOIN_OR),
                 cond("guards", threshold=3, join=shopcfg.CARD_JOIN_AND)]
        # 「与优先于或」的话 A 单独成立就够了；依次结合要求 C 也成立。
        self.assertFalse(self.holds(three, {"kills": 99}))
        self.assertTrue(self.holds(three, {"kills": 99, "guards": 3}))
        self.assertTrue(self.holds(three, {"hits": 30, "guards": 3}))

    def test_the_sentence_puts_the_brackets_where_the_judge_does(self):
        """说明文和判定是**同一个结合顺序** —— 这两处对不上的话，
        页面上写的和真发卡的规则就是两回事。"""
        text = shopcfg.describe_card_rule(rule(
            60001,
            cond("kills", threshold=5),
            cond("hits", threshold=30, join=shopcfg.CARD_JOIN_OR),
            cond("guards", threshold=3, join=shopcfg.CARD_JOIN_AND)))
        opened = shopcfg.CARD_PHRASES["open"]
        closed = shopcfg.CARD_PHRASES["close"]
        self.assertIn(opened, text)
        # 括号扣在前两条上：`（A 或者 B）并且 C`
        self.assertLess(text.index(closed),
                        text.index(shopcfg.CARD_JOIN_ZH[shopcfg.CARD_JOIN_AND]))

    def test_one_kind_of_joiner_needs_no_brackets(self):
        """只有一种连接词时不画括号 —— 那时候括号纯是噪音。"""
        text = shopcfg.describe_card_rule(rule(
            60001, cond("kills", threshold=5),
            cond("hits", threshold=30, join=shopcfg.CARD_JOIN_AND),
            cond("guards", threshold=3, join=shopcfg.CARD_JOIN_AND)))
        self.assertNotIn(shopcfg.CARD_PHRASES["open"], text)


class TotalScopeTests(unittest.TestCase):
    """「玩家累计」：攒够就发一张、**计数器同时归零、重新一轮**
    （用户 2026-09-13 第三轮拍板）。"""

    def every(self, threshold=100, **kw):
        return [rule(60001, cond("kills", threshold=threshold,
                                 scope=shopcfg.CARD_SCOPE_TOTAL), **kw)]

    def test_not_enough_yet_gives_nothing(self):
        give, bases, _w = grants(self.every(), total={"pvp": {"kills": 99}})
        self.assertEqual({}, give)
        self.assertEqual({}, bases, "没发卡就不该动计数器")

    def test_reaching_it_gives_one_card_and_resets_the_counter(self):
        give, bases, _w = grants(self.every(), total={"pvp": {"kills": 250}})
        self.assertEqual({60001: 1}, give)
        self.assertEqual({60001: {"kills": 250}}, bases,
                         "基准要挪到**当前值**，下一轮从这儿重新攒")

    def test_the_next_round_starts_from_zero(self):
        """★★ 归零之后进度是 0 ⇒ 再攒满 100 才有下一张。"""
        only = self.every()
        self.assertEqual({}, grants(only, total={"pvp": {"kills": 349}},
                                    bases={"60001": {"kills": 250}})[0])
        self.assertEqual({60001: 1},
                         grants(only, total={"pvp": {"kills": 350}},
                                bases={"60001": {"kills": 250}})[0])

    def test_one_settlement_pays_at_most_one_card(self):
        """★★ 这是「归零」自带的护栏：运营把阈值从 1000 调成 1，
        也**不会**在一局里刷出一堆 —— 没有循环，也就不需要「上限」那一格。"""
        give, _b, _w = grants(self.every(threshold=1),
                              total={"pvp": {"kills": 9999}})
        self.assertEqual({60001: 1}, give)

    def test_two_counters_must_both_fill_up_and_reset_together(self):
        """★★ 用户原话那个例子：「累计击杀每满 100 并且累计对局每满 10」
        —— 对局满了但击杀没满就不给，两个都满才发，发完**一起归零**。"""
        both = [rule(60001,
                     cond("kills", threshold=100,
                          scope=shopcfg.CARD_SCOPE_TOTAL),
                     cond("games", threshold=10,
                          scope=shopcfg.CARD_SCOPE_TOTAL,
                          join=shopcfg.CARD_JOIN_AND))]
        give, bases, _w = grants(both, total={"pvp": {"kills": 40,
                                                      "games": 10}})
        self.assertEqual({}, give, "对局满了、击杀没满 ⇒ 不发")
        give, bases, _w = grants(both, total={"pvp": {"kills": 100,
                                                      "games": 10}})
        self.assertEqual({60001: 1}, give)
        self.assertEqual({60001: {"kills": 100, "games": 10}}, bases,
                         "两个计数器一起归零")
        # 下一轮：从 100 / 10 重新攒。
        self.assertEqual({}, grants(both, total={"pvp": {"kills": 199,
                                                         "games": 19}},
                                    bases={"60001": bases[60001]})[0])
        self.assertEqual({60001: 1},
                         grants(both, total={"pvp": {"kills": 200,
                                                     "games": 20}},
                                bases={"60001": bases[60001]})[0])

    def test_the_baseline_is_taken_even_when_the_other_half_short_circuits(self):
        """★ `A 或者 B`：`A` 成立时 `B` 也要收进基准 —— 不收的话那个
        计数器会一直攒下去，永远清不掉。"""
        either = [rule(60001,
                       cond("kills", threshold=1,
                            scope=shopcfg.CARD_SCOPE_TOTAL),
                       cond("games", threshold=999,
                            scope=shopcfg.CARD_SCOPE_TOTAL,
                            join=shopcfg.CARD_JOIN_OR))]
        give, bases, _w = grants(either, total={"pvp": {"kills": 5,
                                                        "games": 3}})
        self.assertEqual({60001: 1}, give)
        self.assertEqual({"kills": 5, "games": 3}, bases[60001])

    def test_two_cards_sharing_a_condition_count_separately(self):
        """★★ **两张卡片各攒各的**（用户 2026-09-13 追加）。

        卡片1 = 条件 a 并且 b、卡片2 = 条件 a 并且 c：卡片1 达成之后把
        a 清零了，**卡片2 的 a 不能跟着被清掉** —— 不然一张卡的达成会把
        另一张卡的进度偷走，而且谁也看不出来。

        ⇒ 判据：基准是**按卡片存的**（`{卡片: {统计键: 值}}`），不是
        按统计键存一份全局的。`account_store.card_bases` 那一格的形状
        就是为这件事定的。
        """
        shared = cond("kills", threshold=100, scope=shopcfg.CARD_SCOPE_TOTAL)
        first = rule(60001, dict(shared),
                     cond("games", threshold=10,
                          scope=shopcfg.CARD_SCOPE_TOTAL,
                          join=shopcfg.CARD_JOIN_AND))
        second = rule(60002, dict(shared),
                      cond("guards", threshold=999,
                           scope=shopcfg.CARD_SCOPE_TOTAL,
                           join=shopcfg.CARD_JOIN_AND))
        total = {"pvp": {"kills": 100, "games": 10, "guards": 5}}
        give, bases, _w = grants([first, second], total=total)
        # 只有卡片1 达成 ⇒ 只有它的基准被挪走。
        self.assertEqual({60001: 1}, give)
        self.assertEqual({60001: {"kills": 100, "games": 10}}, bases)
        self.assertNotIn(60002, bases, "没发的卡片不许被清零")
        # 卡片2 接着攒：它的 a 还是从 0 起算的那 100，没被卡片1 偷走。
        after = {"60001": bases[60001]}
        total2 = {"pvp": {"kills": 120, "games": 12, "guards": 999}}
        give2, bases2, _w = grants([first, second], total=total2, bases=after)
        self.assertEqual({60002: 1}, give2,
                         "卡片2 的击杀数进度被卡片1 清掉了")
        self.assertEqual({60002: {"kills": 120, "guards": 999}}, bases2)
        # 反过来卡片1 这一轮才攒了 20 / 2，离下一张还早。
        self.assertNotIn(60001, give2)

    def test_a_baseline_ahead_of_the_counter_never_goes_negative(self):
        """运营改过「对局模式」那一格之后，累计值可能比基准小 ——
        进度夹到 0，不许变成负数（那会让条件恒不成立或者恒成立）。"""
        give, _b, _w = grants(self.every(threshold=1),
                              total={"pvp": {"kills": 5}},
                              bases={"60001": {"kills": 999}})
        self.assertEqual({}, give)

    def test_a_rule_without_a_mode_adds_both_modes_up(self):
        only = self.every(threshold=10)
        only[0].pop("mode", None)
        give, _b, _w = grants(only, total={"pvp": {"kills": 6},
                                           "quest": {"kills": 5}})
        self.assertEqual({60001: 1}, give)

    def test_a_pure_match_rule_leaves_no_baseline_behind(self):
        """★ 纯「一局内」的规则不该在存档里留条目 —— 存一堆空壳只会让
        以后翻存档的人以为那条规则带累计条件。"""
        give, bases, _w = grants([rule(60001)], match={"kills": 5})
        self.assertEqual({60001: 1}, give)
        self.assertEqual({}, bases)


class ExplainTests(unittest.TestCase):
    """★ 结算时那一段**调试日志**（用户 2026-09-14）：每张卡为什么给 / 为什么不给。

    ★★ 它和 `due_grants` 走的是**同一套谓词**（`_applies` / `eval_conditions`）
    —— 日志和真发的卡出自两次不同的计算，迟早会出现「日志说该发、玩家没
    收到」那种查不动的事。这一组里最要紧的就是最后那条「两边结论一致」。
    """

    def say(self, rules, *, mode="pvp", stage=None, difficulty=None,
            match=None, total=None, bases=None):
        return "\n".join(cards.explain(
            rules, mode=mode, stage=stage, difficulty=difficulty,
            match=match or {}, total=total or {}, bases=bases or {}))

    def test_a_match_condition_shows_what_it_got_and_what_it_needs(self):
        text = self.say([rule(60001, cond("guards", threshold=30))],
                        match={"guards": 12})
        self.assertIn(cards.NO_MARK + " 不发", text)
        self.assertIn("本局 12（要 30）", text)

    def test_a_granted_card_says_so_and_names_the_reset(self):
        text = self.say([rule(60001, cond("guards", threshold=5,
                                          scope=shopcfg.CARD_SCOPE_TOTAL))],
                        total={"pvp": {"guards": 7}})
        self.assertIn(cards.OK_MARK + " 发 1 张", text)
        # ★ 累计那一档三个数都要写：这一轮 / 阈值 / 从哪个基准起算。
        self.assertIn("这一轮 7/5（累计 7 − 基准 0）", text)
        self.assertIn("计数器归零 → guards=7", text)

    def test_a_baseline_is_spelled_out(self):
        """★★ 「累计 143 却只算 43」一眼看不出来就会被当成掉数 ——
        上一次查「火焰弹少算一次」正是卡在这儿。"""
        text = self.say([rule(60001, cond("kills", threshold=100,
                                          scope=shopcfg.CARD_SCOPE_TOTAL))],
                        total={"pvp": {"kills": 143}},
                        bases={"60001": {"kills": 100}})
        self.assertIn("这一轮 43/100（累计 143 − 基准 100）", text)

    def test_a_switched_off_card_is_one_short_line(self):
        text = self.say([rule(60001, listed=False)], match={"kills": 9})
        self.assertIn("关着", text)
        self.assertNotIn("本局", text, "关着的卡片不该再逐条算一遍")

    def test_a_rule_that_does_not_apply_says_which_cell_missed(self):
        """★ 只说「这一局不算」等于没说 —— 要说出**哪一格**没对上。"""
        only = [rule(60001, mode="quest")]
        self.assertIn("规则限「闯关」，本局是「对战」",
                      self.say(only, mode="pvp"))
        only = [rule(60001, mode="quest", stage=3)]
        self.assertIn("规则限关卡 3，本局是 5",
                      self.say(only, mode="quest", stage=5))
        only = [rule(60001, mode="quest", difficulty=3)]
        self.assertIn("规则限难度 3，本局是 1",
                      self.say(only, mode="quest", difficulty=1))

    def test_the_joiner_is_written_between_the_lines(self):
        text = self.say([rule(60001, cond("kills", threshold=5),
                              cond("deaths", "eq", 0,
                                   join=shopcfg.CARD_JOIN_OR))],
                        match={"kills": 9})
        self.assertIn(shopcfg.CARD_JOIN_ZH[shopcfg.CARD_JOIN_OR], text)

    def test_an_unknown_metric_is_reported_not_swallowed(self):
        text = self.say([rule(60001, cond("nonsense"))], match={"kills": 9})
        self.assertIn("⚠", text)
        self.assertIn("nonsense", text)

    def test_the_log_and_the_actual_grant_never_disagree(self):
        """★★ 这一组的地基：**同一批规则、同一份数据**，日志说「发」的那些
        必须正好是 `due_grants` 真发出去的那些。"""
        rules = shopcfg.validate_cards(shopdefaults.default_cards())
        cases = [
            ({"guards": 30, "win": 1, "kills": 0}, {}),
            ({"deaths": 0, "win": 1, "hits": 40}, {"pvp": {"kills": 900}}),
            ({"kills@110001": 9, "shots": 3}, {"pvp": {"games": 77}}),
        ]
        for match, total in cases:
            give, _bases, _warn = cards.due_grants(
                rules, mode="pvp", stage=None, difficulty=None,
                match=match, total=total, bases={})
            lines = cards.explain(rules, mode="pvp", stage=None,
                                  difficulty=None, match=match, total=total,
                                  bases={})
            said = set()
            for line in lines:
                if (cards.OK_MARK + " 发") in line:
                    said.add(int(line.split("#", 1)[1].split(" ", 1)[0]))
            self.assertEqual(set(give), said, "%r" % (match,))


class CardProgressTests(unittest.TestCase):
    """「离下一次拿到还差多少」（用户 2026-09-13 第四轮）。

    ★ 管理页两处弹窗画的就是这一份 —— **同一个出处**，两处长一个样。
    """

    def progress(self, rules, stats=None, bases=None, granted=None):
        return {row["card"]: row for row in cards.card_progress(
            rules, stats=stats or {}, bases=bases or {}, granted=granted or {})}

    def test_a_match_condition_carries_no_numbers(self):
        """★★ 一局内的条件**攒不住** ⇒ 不发 `have` / `need`：前台照这个
        画「每局游戏内计算」，而不是一根永远停在 0 的进度条。"""
        rows = self.progress([rule(60001, cond("kills", threshold=5))])
        one = rows[60001]["conditions"][0]
        self.assertEqual(shopcfg.CARD_SCOPE_MATCH, one["scope"])
        self.assertNotIn("have", one)
        self.assertNotIn("need", one)
        self.assertTrue(one["text"])

    def test_a_total_condition_counts_this_round_not_the_lifetime(self):
        """★★ `have` 是**这一轮**攒的（累计 − 基准）。写成账号总数的话，
        一个拿过五张的人永远看着「已经超了」，而他其实刚归零。"""
        only = [rule(60001, cond("kills", threshold=100,
                                 scope=shopcfg.CARD_SCOPE_TOTAL))]
        rows = self.progress(only, stats={"pvp": {"kills": 143}})
        self.assertEqual({"have": 143, "need": 100}, {
            k: v for k, v in rows[60001]["conditions"][0].items()
            if k in ("have", "need")})
        rows = self.progress(only, stats={"pvp": {"kills": 143}},
                             bases={"60001": {"kills": 100}})
        self.assertEqual(43, rows[60001]["conditions"][0]["have"])

    def test_a_baseline_ahead_of_the_counter_shows_zero_not_a_minus(self):
        rows = self.progress(
            [rule(60001, cond("kills", threshold=10,
                              scope=shopcfg.CARD_SCOPE_TOTAL))],
            stats={"pvp": {"kills": 3}}, bases={"60001": {"kills": 99}})
        self.assertEqual(0, rows[60001]["conditions"][0]["have"])

    def test_the_weapon_dimension_reads_its_own_counter(self):
        rows = self.progress(
            [rule(110001, cond("kills", threshold=50, weapon=110001,
                               scope=shopcfg.CARD_SCOPE_TOTAL))],
            stats={"pvp": {"kills": 900, "kills@110001": 61}})
        self.assertEqual(61, rows[110001]["conditions"][0]["have"])

    def test_both_kinds_of_condition_come_back_together(self):
        """★ 一张卡片混着两档条件时**两种都发** —— 弹窗那两个筛选是
        「含…条件」，选中哪一档都要把整张卡片的条件画全（用户点名的）。"""
        rows = self.progress([rule(
            60004, cond("guards", threshold=30),
            cond("kills", threshold=100, scope=shopcfg.CARD_SCOPE_TOTAL,
                 join=shopcfg.CARD_JOIN_AND))], stats={"pvp": {"kills": 40}})
        scopes = [c["scope"] for c in rows[60004]["conditions"]]
        self.assertEqual([shopcfg.CARD_SCOPE_MATCH,
                          shopcfg.CARD_SCOPE_TOTAL], scopes)

    def test_a_switched_off_card_still_shows_up_and_says_so(self):
        """★ 关着的卡片照样列出来 —— 从名单上消失的话，运营和玩家都会以为
        这张卡不存在（同掉落页那一行「只淡不隐」）。"""
        rows = self.progress([rule(60001, listed=False)])
        self.assertFalse(rows[60001]["listed"])
        self.assertEqual(shopcfg.CARD_PHRASES["off"], rows[60001]["text"])

    def test_it_says_how_many_were_already_earned(self):
        rows = self.progress([rule(60001)], granted={"60001": 4})
        self.assertEqual(4, rows[60001]["granted"])
        self.assertEqual(0, self.progress([rule(60002)])[60002]["granted"])


class MatchStatsTests(unittest.TestCase):
    """`RoomQuest` → 本局战绩。★ 用真的 `RoomQuest`，不是一个手搓的替身。"""

    def quest(self):
        import gameserver
        return gameserver.RoomQuest()

    def test_winning_is_still_counted_even_though_you_cannot_pick_it_per_match(self):
        """★ `win` / `games` 在「一局内」那一档**选不到**（指标表标着只有
        累计才有意义），但**每局照样要写** —— 累计值就是从每局这一笔加出来的。
        """
        quest = self.quest()
        stats = cards.match_stats(quest, 0, won=True, quest_mode=False,
                                  score=0)
        self.assertEqual(1, stats["win"])
        self.assertEqual(1, stats["games"])
        for key in ("win", "games"):
            self.assertEqual((shopcfg.CARD_SCOPE_TOTAL,),
                             shopcfg.card_metric_scopes(key), key)

    def test_losing_clears_the_win_counter_but_still_counts_the_game(self):
        quest = self.quest()
        stats = cards.match_stats(quest, 0, won=False, quest_mode=False,
                                  score=0)
        self.assertEqual(0, stats.get("win", 0))
        self.assertEqual(1, stats["games"])

    def test_kills_is_players_plus_monsters_and_mob_kills_is_not_written_twice(self):
        """★★ `kills` 是杀人 + 杀怪的和。⚠ **不许再单独写一份 `mob_kills`**
        —— 那一格已经加进去了，两边都写以后谁给 `kills` 加一次读时求和
        就会重复计数。"""
        quest = self.quest()
        quest.enemy_kills[0] = 2
        quest.mob_kills[0] = 5
        stats = cards.match_stats(quest, 0, won=True, quest_mode=True, score=0)
        self.assertEqual(7, stats["kills"])
        self.assertNotIn("mob_kills", stats)

    def test_a_quest_clear_with_no_kills_is_not_a_zero_kill_win(self):
        """★ 闯关杀怪也算 `kills` ⇒ 「击杀数为 0 + 只有通关」在闯关里
        不再对任何一次通关都成立（旧的 `carried` 就是栽在这上面，只能
        限定成对战专用）。"""
        quest = self.quest()
        quest.mob_kills[0] = 3
        only = [rule(60005, cond("kills", "eq", 0), WON)]
        stats = cards.match_stats(quest, 0, won=True, quest_mode=True, score=0)
        self.assertEqual({}, grants(only, mode="quest", match=stats)[0])

    def test_the_weapon_counters_fold_into_keys(self):
        quest = self.quest()
        quest.weapon_stat(0, 110002)["kills"] = 4
        quest.weapon_stat(0, 110002)["shots"] = 30
        quest.weapon_stat(1, 110002)["kills"] = 9      # 别人的，不许串
        stats = cards.match_stats(quest, 0, won=False, quest_mode=False,
                                  score=0)
        # ★ 武器维度的键**和主指标同名** —— 武器只是一个筛选维度。
        self.assertEqual(4, stats["kills@110002"])
        self.assertEqual(30, stats["shots@110002"])

    def test_zero_values_are_dropped(self):
        """★ 只留非零项 —— `accounts.json` 不为「运营以后可能用的指标」变胖。"""
        quest = self.quest()
        stats = cards.match_stats(quest, 0, won=False, quest_mode=False,
                                  score=0)
        self.assertNotIn("kills", stats)
        self.assertNotIn("deaths", stats)
        self.assertEqual(1, stats["games"], "场次恒为 1，这一格必须在")


class ComparisonGuardrailTests(unittest.TestCase):
    """★★ 比较符的四道护栏 —— 这一组是**推翻 D100 的全部底气**。

    D100 当初不做「≤」，怕的是「阈值填 0 时恒成立、每局白送一张卡、
    而且页面上看着完全正常」。现在换成结构性护栏：配得出来的组合里
    **不存在**恒成立那一档。少一条这一组就不成立。
    """

    def refuse(self, *conditions):
        with self.assertRaises(shopcfg.ConfigError) as caught:
            shopcfg.validate_cards({"rules": [rule(60001, *conditions)]})
        return str(caught.exception)

    def accept(self, *conditions):
        kept = shopcfg.validate_cards({"rules": [rule(60001, *conditions)]})
        return kept[0]["conditions"]

    def test_reaching_zero_is_refused_because_it_always_holds(self):
        """「大于等于 0」对任何一局都成立 ⇒ 每局白送一张，而且没人看得出为什么。"""
        self.refuse(cond("deaths", "ge", 0))

    def test_at_most_zero_and_exactly_zero_are_allowed(self):
        """反过来，「小于等于 0」「等于 0」要的**正是** 0（「一次都没死」）。"""
        for op in ("le", "eq"):
            self.assertEqual(
                0, self.accept(cond("deaths", op, 0))[0]["threshold"], op)

    def test_the_milestone_scope_only_takes_the_reaching_comparison(self):
        """累计条件念的是「每满 N」：刚归零那一刻累计值是 0，
        「小于等于 3」对每一个人都成立 ⇒ 照「对战没有关卡和难度」的先例
        **报错**，不静默吞掉也不静默改写。"""
        said = self.refuse(cond("games", "le", 5,
                                scope=shopcfg.CARD_SCOPE_TOTAL))
        self.assertIn(shopcfg.CARD_EVERY_ZH, said)
        kept = self.accept(cond("games", "ge", 5,
                                scope=shopcfg.CARD_SCOPE_TOTAL))
        self.assertEqual(shopcfg.CARD_OP_GE, kept[0]["op"])

    def test_a_weapon_dimension_only_takes_the_reaching_comparison(self):
        """★★ 取不到的统计键返回 0 ⇒「只算左轮打出的击杀数为 0」对
        **每一个没拿过左轮的人**都成立，而那是绝大多数人。
        0 在武器维度下是常态不是例外，所以这条必须拦死。"""
        for op in ("le", "eq"):
            said = self.refuse(cond("kills", op, 0, weapon=110001))
            self.assertIn("没用过", said, op)
        self.assertEqual(110001,
                         self.accept(cond("kills", "ge", 5,
                                          weapon=110001))[0]["weapon"])

    def test_the_judge_does_not_re_floor_the_threshold(self):
        """★★ 判定层**不许**自己再夹一次 `max(1, …)` —— 夹了的话
        「死亡次数正好等于 0」会被悄悄提成「正好等于 1」（「死一次才给」），
        而页面上、日志里都看不出来。"""
        only = [rule(60001, cond("deaths", "eq", 0))]
        self.assertEqual({60001: 1}, grants(only, match={})[0])
        self.assertEqual({}, grants(only, match={"deaths": 1})[0])

    def test_an_unknown_comparison_is_refused(self):
        self.refuse(cond("deaths", "lt", 1))

    def test_an_enum_metric_only_takes_equals_and_its_own_two_values(self):
        """★ 「本局结果」只有胜 / 负两档，「大于等于 1」这种说法对它没意义。"""
        self.refuse(cond("won", "ge", 1))
        self.refuse(cond("won", "eq", 7))
        self.assertEqual(1, self.accept(cond("won", "eq", 1))[0]["threshold"])

    def test_the_first_condition_must_not_carry_a_joiner(self):
        """第一条前面没有「上一条」⇒ 写了连接词是画面上一个改了也没用的格子。"""
        self.refuse(cond("kills", join=shopcfg.CARD_JOIN_AND))
        # 第二条缺省就是「并且」，补出来。
        kept = self.accept(cond("kills"), cond("deaths", "eq", 0))
        self.assertNotIn("join", kept[0])
        self.assertEqual(shopcfg.CARD_JOIN_AND, kept[1]["join"])

    def test_a_rule_needs_at_least_one_condition(self):
        with self.assertRaises(shopcfg.ConfigError):
            shopcfg.validate_cards(
                {"rules": [{"card": 60001, "listed": True, "conditions": []}]})

    def test_too_many_conditions_are_refused(self):
        many = [cond("kills", join=shopcfg.CARD_JOIN_AND)
                for _ in range(shopcfg.MAX_CARD_CONDITIONS + 1)]
        many[0] = cond("kills")
        self.refuse(*many)

    def test_a_ratio_metric_must_carry_a_sample_floor(self):
        """★★ 这是「命中率」这个指标唯一的意义所在：开一枪中一枪也是 100%，
        不卡样本的「命中率 ≥ 90」就是一张每局白送的卡。

        ★ 第一稿那个专用的 `min_shots` 格子删掉了 —— 它现在就是一条普通
        条件（用户给的例子就是这么写的），护栏也跟着变成「这串条件里有没有它」。
        """
        said = self.refuse(cond("accuracy", "ge", 90))
        self.assertIn(shopcfg.CARD_METRIC_ZH[shopcfg.CARD_SAMPLE_METRIC], said)
        kept = self.accept(cond("accuracy", "ge", 90),
                           cond(shopcfg.CARD_SAMPLE_METRIC, "ge", 20,
                                join=shopcfg.CARD_JOIN_AND))
        self.assertEqual(2, len(kept))

    def test_a_ratio_metric_may_not_sit_next_to_an_or(self):
        """★ 「或者」会让开枪数下限被绕开 —— 那等于没卡。"""
        said = self.refuse(cond("accuracy", "ge", 90),
                           cond(shopcfg.CARD_SAMPLE_METRIC, "ge", 20,
                                join=shopcfg.CARD_JOIN_AND),
                           cond("kills", "ge", 1,
                                join=shopcfg.CARD_JOIN_OR))
        self.assertIn(shopcfg.CARD_JOIN_ZH[shopcfg.CARD_JOIN_OR], said)

    def test_the_sample_floor_looks_at_this_match_not_the_lifetime(self):
        only = [rule(60003, cond("accuracy", "ge", 60),
                     cond("shots", "ge", 20, join=shopcfg.CARD_JOIN_AND))]
        self.assertEqual({}, grants(only, match={"shots": 1, "hits": 1})[0],
                         "开一枪中一枪也是 100%，但样本不够")
        self.assertEqual({60003: 1},
                         grants(only, match={"shots": 20, "hits": 15})[0])

    def test_a_ratio_metric_is_refused_in_the_milestone_scope(self):
        """「玩家累计命中率每满 60」这句话本身就不通（而且比率不能累加）。"""
        self.refuse(cond("accuracy", "ge", 60,
                         scope=shopcfg.CARD_SCOPE_TOTAL))

    def test_a_metric_that_only_makes_sense_in_totals_is_refused_per_match(self):
        """「打完一局 = 1」在一局内只配得出「每局白送一张」⇒ 不给选。
        真想要「每局都给」就明写「玩家累计对局数每满 1」，看得出来。"""
        self.refuse(cond("games", "ge", 1))
        self.assertEqual(1, self.accept(cond("games", "ge", 1,
                                             scope=shopcfg.CARD_SCOPE_TOTAL)
                                        )[0]["threshold"])
        # 反过来：「本局结果」只有一局内那一档有。
        self.refuse(cond("won", "eq", 1, scope=shopcfg.CARD_SCOPE_TOTAL))


class DefaultRulesTests(unittest.TestCase):
    """出厂那 17 条规则本身要站得住。"""

    def setUp(self):
        self.rules = shopcfg.validate_cards(shopdefaults.default_cards())

    def test_every_card_has_exactly_one_rule(self):
        cards_seen = [r["card"] for r in self.rules]
        self.assertEqual(len(cards_seen), len(set(cards_seen)))
        self.assertEqual(set(shopcfg.ALL_CARDS), set(cards_seen))

    def test_every_default_rule_is_reachable(self):
        """★ 指标只在某一档统计范围下有意义时，条件的范围必须是那一档 ——
        否则那条规则配得出来却**永远命中不了**（D17a 说的正是这种档）。"""
        for entry in self.rules:
            for one in entry["conditions"]:
                limited = shopcfg.card_metric_scopes(one["metric"])
                if limited:
                    self.assertIn(one.get("scope"), limited, entry["card"])

    def test_the_cards_schema_matches_the_validator(self):
        """★★ `SCHEMA["cards"]` 和 `validate_cards` 产出的键**必须对得上**（D16）。

        管理页照 `SCHEMA` 生成输入框：给 validator 加一个字段却忘了登记 ⇒
        那个字段在画面上是个隐形人；登记了 validator 不产出的 ⇒ 多一个
        存不进去的框。

        ★ 出厂那 17 条锁不住这件事（它们从不产出 `stage` / `difficulty`），
        所以这儿用**把每个可选分支都点亮的合成规则**，保持严格相等
        —— 别改成「子集」那种宽松匹配，那就锁不住「登记了却不产出」那一半。
        """
        rules = shopcfg.validate_cards({"rules": [
            dict(card=60001, listed=True,
                 mode="quest", stage=3, difficulty=2,     # 闯关才留得住这两格
                 conditions=[
                     dict(scope="match", metric="kills", op="ge",
                          threshold=1, weapon=110001),    # kills 分得到武器
                     dict(join="and", scope="match", metric="won", op="eq",
                          threshold=1),
                 ]),
        ]})
        self.assertEqual(set(rules[0]), set(shopcfg.schema_keys("cards")))
        # 条件的**子字段表**同一条口径：validator 产出的每个键都要登记。
        sub = set()
        for one in rules[0]["conditions"]:
            sub |= set(one)
        registered = {field["key"] for field in
                      [f for f in shopcfg.SCHEMA["cards"]["fields"]
                       if f["key"] == "conditions"][0]["fields"]}
        self.assertEqual(sub, registered)

    def test_the_lucky_card_is_what_the_user_asked_for(self):
        """用户 2026-09-14 在管理页上定的：对战每局格挡 ≥ 36 次**且获胜**，
        **或者**累计格挡满 166 次，给一张。

        ★ 第三条是**兜底** ——「或者」接在前两条后面（从上往下结合，没有
        括号），少了它这张全表最强的卡就只有「打出一局好的」一条路。
        """
        lucky = [r for r in self.rules if r["card"] == 60004][0]
        self.assertEqual("pvp", lucky["mode"])
        first, second, third = lucky["conditions"]
        self.assertEqual(shopcfg.CARD_SCOPE_MATCH, first["scope"])
        self.assertEqual("guards", first["metric"])
        self.assertEqual(36, first["threshold"])
        self.assertEqual("won", second["metric"])
        self.assertEqual(1, second["threshold"])
        self.assertEqual(shopcfg.CARD_JOIN_AND, second["join"])
        self.assertEqual(shopcfg.CARD_SCOPE_TOTAL, third["scope"])
        self.assertEqual("guards", third["metric"])
        self.assertEqual(166, third["threshold"])
        self.assertEqual(shopcfg.CARD_JOIN_OR, third["join"])

    def test_each_weapon_card_counts_its_own_weapon(self):
        """★ 指标就是普通的「击杀数」，「用哪把枪」由条件里那一格回答。"""
        for roh in shopcfg.WEAPON_CARDS:
            entry = [r for r in self.rules if r["card"] == roh][0]
            one = entry["conditions"][0]
            self.assertEqual("kills", one["metric"])
            self.assertEqual(roh, one["weapon"], roh)
            # 按武器统计时比较符只能是「大于等于」—— 别的方向对没碰过那把枪
            # 的人恒成立（校验器拦着，这儿钉住出厂值）。
            self.assertEqual(shopcfg.CARD_OP_GE, one["op"], roh)

    def test_the_zero_flavoured_rules_also_require_the_win(self):
        """★ 旧的 `perfect_win` / `carried` 自带「赢了」；拆开之后那一半
        是一条 `本局结果 = 胜利` 的条件，出厂值漏了就变成「输了没死也给」。"""
        for card, metric in ((60001, "deaths"), (60005, "kills")):
            entry = [r for r in self.rules if r["card"] == card][0]
            first, second = entry["conditions"]
            self.assertEqual(metric, first["metric"], card)
            self.assertEqual(shopcfg.CARD_OP_EQ, first["op"], card)
            self.assertEqual(0, first["threshold"], card)
            self.assertEqual("won", second["metric"], card)
            self.assertEqual(1, second["threshold"], card)

    def test_the_shared_win_condition_is_not_the_same_object(self):
        """★ 出厂表里那条「并且赢了」是三条规则共用的字面量 ——
        `build_cards()` 必须拷一份，否则运营改一条会三条一起变。"""
        rows = shopdefaults.build_cards()
        winners = [one for row in rows for one in row["conditions"]
                   if one["metric"] == "won"]
        self.assertGreater(len(winners), 1)
        winners[0]["threshold"] = 0
        self.assertEqual([1] * (len(winners) - 1),
                         [w["threshold"] for w in winners[1:]])

    def test_every_rule_reads_as_a_sentence(self):
        """念出来的那句话是三处共用的（管理页浮窗 / 游戏提示框 / 冲突提示）。

        ⚠ 里面**绝不能出现裸 `|`** —— 客户端拿它当说明的分段符
        （`wcstok`，`0x5fa904`），混进去会把一段说明拦腰切开。
        """
        for entry in self.rules:
            line = shopcfg.describe_card_rule(entry, shopcfg.item_name(entry["card"]))
            self.assertTrue(line, entry["card"])
            self.assertNotEqual(shopcfg.CARD_PHRASES["bad"], line,
                                "出厂规则自己就是「条件无效」")
            self.assertNotIn(shopcfg.DESC_SEPARATOR, line, entry["card"])
            self.assertNotIn("None", line, entry["card"])
            for one in entry["conditions"]:
                self.assertNotIn(one["metric"], line,
                                 "指标的英文 key 漏进人话里了")
                self.assertNotIn(one["op"], line,
                                 "比较符的英文 key 漏进人话里了")
                if one.get("weapon"):
                    self.assertIn(shopcfg.WEAPON_ROH_ZH[one["weapon"]], line,
                                  "按武器统计的规则得说出是哪把枪")

    def test_the_sentence_stays_short_enough_for_the_tooltip(self):
        """★ 提示框第 1 段是 234×82 px ≈ 6 行，「获得条件：」占其中一行 ——
        客户端**自己折行**，而行数预算只数 `\\n`，管不住一行有多宽。
        ⇒ 这儿按字数卡一道软上限，免得武器卡那句把「可合成：」挤出预算。

        ★ 提示框念的是**不带尾巴**那一版（`with_tail=False`）：它就停在那张
        卡上弹出来、前面还写着「获得条件：」，再说一遍「获得一张卡片」
        纯占宽度 —— 这条用例量的正是它。

        ★★ 上限**按「占几行」算**：234 px ÷ 10 px（字高 10 的全角字）≈ 23 字
        一行，「获得条件：」自己吃掉 5 个 ⇒ 一行 ≈ 18 字、三行 ≈ 64 字，
        6 行里还剩 3 行给「可合成：」那几行。原来卡在 **2 行（40 字）**，
        用户 2026-09-14 把 `60004` 改成三条条件（带括号）之后那句 56 字
        ⇒ 放宽到 **3 行**。其余 16 句都 ≤ 34 字，还在 2 行里。
        ⚠ 再往上就要挤「可合成：」了，别顺手再放宽 —— 该改的是那条规则。
        """
        for entry in self.rules:
            line = shopcfg.describe_card_rule(entry, with_tail=False)
            self.assertLessEqual(len(line), 64,
                                 "%s 那句太长了：%s" % (entry["card"], line))

    def test_a_switched_off_rule_says_so(self):
        line = shopcfg.describe_card_rule({"card": 60001, "listed": False})
        self.assertEqual("暂时无法获得", line)


class OneShotUpgradeTests(unittest.TestCase):
    """★★ 「给老服务器补 17 条称号配方」是**一次性升级**（用户 2026-09-13）。

    判据是一个**事件**：这一次启动刚刚新建了 `cards.json`（铁律 10）——
    不是标记文件、不是计数器。三条路各验一遍，第三条最要紧：
    **运营后来删掉的配方不许自己回来**（铁律 11）。
    """

    def setUp(self):
        import tempfile
        # ⚠ **不许 import `app`**：那个模块 import 时就 `asynclog.start()`，
        #   把日志切成异步写 —— `test_logs` 里那几条断言 stdout 的用例会集体翻
        #   （踩过一次）。升级的**判断和写盘**因此住在 `shopcfg` 里，
        #   `app` 那边只剩说话。
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = shopcfg.DATA_DIR
        shopcfg.DATA_DIR = self.tmp.name
        self.addCleanup(shopcfg.invalidate)
        self.addCleanup(setattr, shopcfg, "DATA_DIR", saved)
    def recipes_on_disk(self):
        import json
        with open(shopcfg.path_of(shopcfg.RECIPE_FILENAME, self.tmp.name),
                  encoding="utf-8") as fp:
            return json.load(fp)["recipes"]

    def write_recipes(self, rows):
        shopcfg.write_json(
            shopcfg.path_of(shopcfg.RECIPE_FILENAME, self.tmp.name),
            {"format": shopcfg.FORMAT, "recipes": rows})
        shopcfg.invalidate(self.tmp.name)

    def titles_in(self, rows):
        import shopdata
        return {r["result"] for r in rows
                if shopdata.kind(r["result"]) == "title"}

    def test_a_brand_new_server_needs_no_upgrade(self):
        """全新装的：两份配置同时生成 ⇒ `recipe.json` 本来就是全的，空跑。"""
        created = shopcfg.ensure_files(self.tmp.name)
        self.assertIn(shopcfg.CARDS_FILENAME, created)
        before = self.recipes_on_disk()
        added = shopcfg.apply_first_run_upgrades(created, self.tmp.name)
        self.assertEqual(before, self.recipes_on_disk())
        self.assertEqual({}, added)

    def test_an_old_server_gets_the_title_recipes_once(self):
        """老服务器：只有 `cards.json` 是新建的 ⇒ 把缺的 17 条补上。"""
        # 先铺一份「这一版之前」的 recipe.json：把称号那几条去掉。
        full = shopcfg.validate_recipes(shopcfg.default_recipes())
        old = [r for r in full if r["result"] not in self.titles_in(full)]
        self.assertTrue(self.titles_in(full), "默认配方里居然没有称号")
        self.write_recipes(old)
        # 顺手改一条，验「只增不改」。
        old[0]["cost"] = 424242
        self.write_recipes(old)

        created = shopcfg.ensure_files(self.tmp.name)
        self.assertIn(shopcfg.CARDS_FILENAME, created)
        self.assertNotIn(shopcfg.RECIPE_FILENAME, created)
        added = shopcfg.apply_first_run_upgrades(created, self.tmp.name)

        now = self.recipes_on_disk()
        self.assertEqual(self.titles_in(full), self.titles_in(now))
        self.assertEqual(len(old) + len(self.titles_in(full)), len(now))
        kept = [r for r in now if r["result"] == old[0]["result"]][0]
        self.assertEqual(424242, kept["cost"], "运营改过的值被默认值盖掉了")
        self.assertEqual(len(self.titles_in(full)),
                         len(added[shopcfg.RECIPE_FILENAME]))

    def test_the_second_start_never_brings_deleted_recipes_back(self):
        """★★ 这一条是整个「一次性」的意义所在（铁律 11）。

        第二次启动 `cards.json` 已经在了 ⇒ 整个跳过 ⇒ 运营删掉的配方
        **不会自己回来**。`backfill_defaults()` 是幂等的，但它不是「一次性」
        —— 每次启动都叫它的话，删一条回来一条，而且没人看得出为什么。
        """
        created = shopcfg.ensure_files(self.tmp.name)
        added = shopcfg.apply_first_run_upgrades(created, self.tmp.name)
        # 运营在管理页上删掉了一条配方。
        rows = [r for r in self.recipes_on_disk()][:-1]
        self.write_recipes(rows)
        # 第二次启动：`ensure_files` 什么都没建。
        created2 = shopcfg.ensure_files(self.tmp.name)
        self.assertEqual([], created2)
        shopcfg.apply_first_run_upgrades(created2, self.tmp.name)
        self.assertEqual(len(rows), len(self.recipes_on_disk()),
                         "删掉的配方自己回来了")


class StaleNameRefreshTests(unittest.TestCase):
    """★★ 「出厂名改过」的那几件 —— 判据是**盘上那个名字还等于我们当初发出去的**。

    这不是「拿默认值盖掉用户的设置」，是「这一格他从来没碰过，而我们换了
    出厂值，替他带过去」。**运营自己改过的名字一个都不碰**（铁律 11）。
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = shopcfg.DATA_DIR
        shopcfg.DATA_DIR = self.tmp.name
        self.addCleanup(shopcfg.invalidate)
        self.addCleanup(setattr, shopcfg, "DATA_DIR", saved)

    def write_items(self, names):
        """铺一份 `items.json`：`names` 是 `{id: 名字}` 的覆盖。"""
        doc = shopcfg.default_items()
        for entry in doc["items"]:
            if entry["id"] in names:
                entry["name"] = names[entry["id"]]
        shopcfg.write_json(
            shopcfg.path_of(shopcfg.ITEMS_FILENAME, self.tmp.name), doc)
        shopcfg.invalidate(self.tmp.name)

    def names_on_disk(self):
        import json
        with open(shopcfg.path_of(shopcfg.ITEMS_FILENAME, self.tmp.name),
                  encoding="utf-8") as fp:
            return {e["id"]: e["name"] for e in json.load(fp)["items"]}

    def test_the_table_actually_describes_a_rename(self):
        """表里每一条都得真的换过名字 —— 不然那一条是死的，只会让人以为它管用。"""
        import shopdata
        import shopdefaults
        self.assertTrue(shopcfg.RENAMED_DEFAULT_NAMES)
        for item_id, was in shopcfg.RENAMED_DEFAULT_NAMES.items():
            item = shopdata.get(item_id)
            self.assertIsNotNone(item, item_id)
            self.assertNotEqual(was, shopdefaults.name_of(item), item_id)

    def test_an_untouched_old_name_gets_carried_forward(self):
        self.write_items(dict(shopcfg.RENAMED_DEFAULT_NAMES))
        changed = shopcfg.refresh_stale_names(self.tmp.name, apply=True)
        self.assertEqual(sorted(shopcfg.RENAMED_DEFAULT_NAMES),
                         sorted(i for i, _w, _n in changed))
        names = self.names_on_disk()
        for item_id, was in shopcfg.RENAMED_DEFAULT_NAMES.items():
            self.assertNotEqual(was, names[item_id], item_id)

    def test_a_name_the_operator_changed_is_never_touched(self):
        """★★ 这一条是整条判据的意义所在（铁律 11）。"""
        mine = dict((i, "我自己起的名字%d" % i)
                    for i in shopcfg.RENAMED_DEFAULT_NAMES)
        self.write_items(mine)
        self.assertEqual([], shopcfg.refresh_stale_names(self.tmp.name,
                                                         apply=True))
        names = self.names_on_disk()
        for item_id, wanted in mine.items():
            self.assertEqual(wanted, names[item_id], item_id)

    def test_it_is_idempotent(self):
        """★ 判据自带幂等：刷完盘上的名字已经不等于旧名了，再跑就是空的
        —— 所以它不需要「跑过没有」的标记。"""
        self.write_items(dict(shopcfg.RENAMED_DEFAULT_NAMES))
        self.assertTrue(shopcfg.refresh_stale_names(self.tmp.name, apply=True))
        self.assertEqual([], shopcfg.refresh_stale_names(self.tmp.name,
                                                         apply=True))

    def test_a_dry_run_writes_nothing(self):
        self.write_items(dict(shopcfg.RENAMED_DEFAULT_NAMES))
        before = self.names_on_disk()
        self.assertTrue(shopcfg.refresh_stale_names(self.tmp.name))
        self.assertEqual(before, self.names_on_disk())

    def test_nothing_else_in_the_file_moves(self):
        """只动名字那一格 —— 等级 / 角色限定 / 别的 807 件一个字都不许变。"""
        self.write_items(dict(shopcfg.RENAMED_DEFAULT_NAMES))
        before = self.names_on_disk()
        shopcfg.refresh_stale_names(self.tmp.name, apply=True)
        after = self.names_on_disk()
        self.assertEqual(len(before), len(after))
        moved = {i for i in before if before[i] != after[i]}
        self.assertEqual(set(shopcfg.RENAMED_DEFAULT_NAMES), moved)

    def test_a_missing_or_broken_file_is_left_alone(self):
        """读不懂就别动它（D10）—— 也不许抛。"""
        self.assertEqual([], shopcfg.refresh_stale_names(self.tmp.name,
                                                         apply=True))
        with open(shopcfg.path_of(shopcfg.ITEMS_FILENAME, self.tmp.name),
                  "w", encoding="utf-8") as fp:
            fp.write("{ 这不是 json")
        self.assertEqual([], shopcfg.refresh_stale_names(self.tmp.name,
                                                         apply=True))

    def test_the_first_run_upgrade_does_it_too(self):
        """开服那一次性升级里也带着它 —— 云上不用有人记得去跑命令。"""
        self.write_items(dict(shopcfg.RENAMED_DEFAULT_NAMES))
        created = shopcfg.ensure_files(self.tmp.name)
        self.assertIn(shopcfg.CARDS_FILENAME, created)
        shopcfg.apply_first_run_upgrades(created, self.tmp.name)
        names = self.names_on_disk()
        for item_id, was in shopcfg.RENAMED_DEFAULT_NAMES.items():
            self.assertNotEqual(was, names[item_id], item_id)


class CardTooltipTests(unittest.TestCase):
    """★ 卡片的物品提示框（用户 2026-09-13）——**游戏里和管理页同一个出处**。

    `shopcfg.item_desc_zh()` 的结果既进 `ItemInfo +0x18`（客户端提示框下半），
    又进管理页 catalog 的 `desc`（`paintTip` 照着画）。改一个函数两边同时亮。
    """

    def setUp(self):
        import shopdata
        import tempfile
        self.shopdata = shopdata
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = shopcfg.DATA_DIR
        shopcfg.DATA_DIR = self.tmp.name
        self.addCleanup(shopcfg.invalidate)
        self.addCleanup(setattr, shopcfg, "DATA_DIR", saved)
        # ★ 全量测试把 `DATA_DIR` 指到一个**空目录**上（`run_tests.py` 的注释
        #   说了为什么），所以要验说明就得自己铺一份配置。
        shopcfg.ensure_files(self.tmp.name)
        shopcfg.invalidate(self.tmp.name)

    def desc(self, item_id):
        return shopcfg.item_desc_zh(self.shopdata.get(item_id))

    def test_every_card_says_how_to_get_it_and_what_it_makes(self):
        for card in shopcfg.ALL_CARDS:
            text = self.desc(card)
            self.assertIn("获得条件：", text, card)
            self.assertIn(shopcfg.CARD_CRAFT_PREFIX, text, card)  # 能合成什么
            self.assertIn("金币", text, card)          # 要花多少

    def test_the_title_effect_comes_along(self):
        """★ 用户 2026-09-13 追加：**关联显示对应称号的加成效果**。

        效果那句话和「穿上身之后提示框里写的」**必须是同一句**
        —— 两处都走 `_bonus_lines` + `_effect_lines`。
        """
        text = self.desc(60004)
        self.assertIn(shopcfg.title_effect_zh(560004), text)
        self.assertIn("50%", text, "[幸运幸存者] 那句 exe 硬编码的效果没带上")
        # 武器称号的条件加成（Lua）也要带上。
        self.assertIn(shopcfg.title_effect_zh(610001), self.desc(110001))

    def test_a_card_with_no_recipe_says_nothing_about_crafting(self):
        """★ 没有配方就不写那一段（用户 2026-09-13）。"""
        rows = [r for r in shopcfg.validate_recipes(shopcfg.default_recipes())
                if r["result"] != 610001]
        self.write_recipes(rows)
        text = self.desc(110001)
        self.assertIn("获得条件：", text)
        self.assertNotIn(shopcfg.CARD_CRAFT_PREFIX, text)
        self.assertNotIn(shopcfg.DESC_SEPARATOR, text, "空段也不该切出来")

    def test_a_recipe_that_needs_several_cards_lists_them_all(self):
        """★ 一个称号要好几种卡片时**每一种都要写出来**（用户 2026-09-13）。

        只写「500 张」会让人以为攒够这一种就合得出来。
        ★ 本卡片排最前面 —— 提示框是停在它身上弹出来的。
        """
        rows = shopcfg.validate_recipes(shopcfg.default_recipes())
        for entry in rows:
            if entry["result"] == 560004:
                entry["materials"] = [{"id": 60004, "count": 500},
                                      {"id": 60006, "count": 50}]
        self.write_recipes(rows)
        line = [l for l in self.desc(60004).split("\n")
                if l.startswith(shopcfg.CARD_CRAFT_PREFIX)][0]
        self.assertIn("幸运卡片×500", line)
        self.assertIn("红心卡片×50", line)
        self.assertLess(line.index("幸运卡片"), line.index("红心卡片"))
        # 反过来停在红心卡片上时，红心排最前面。
        other = [l for l in self.desc(60006).split("\n")
                 if l.startswith(shopcfg.CARD_CRAFT_PREFIX)
                 and "幸运幸存者" in l][0]
        self.assertLess(other.index("红心卡片"), other.index("幸运卡片"))

    def test_a_card_used_by_several_titles_lists_them_all(self):
        """★ 一张卡片能合成好几个称号时，一个一行（用户 2026-09-13）。"""
        rows = shopcfg.validate_recipes(shopcfg.default_recipes())
        for entry in rows:
            if entry["result"] in (560005, 560006):
                entry["materials"] = [{"id": 60004, "count": 20}]
        self.write_recipes(rows)
        text = self.desc(60004)
        for title in ("[幸运幸存者]", "[手下留情]", "[红心达人]"):
            self.assertIn(title, text, title)
        # 三个称号 = 1 行条件 + 3 行，还在第 1 段 5 行的预算里 ⇒ 不该截断。
        self.assertNotIn("另有", text)
        segments = text.split(shopcfg.DESC_SEPARATOR)
        self.assertLessEqual(len(segments[0].split("\n")),
                             shopcfg.ITEM_DESC_MAX_LINES)
        self.assertLessEqual(len(segments[1].split("\n")),
                             shopcfg.ITEM_DESC_MAX_LINES_2)

    def test_every_title_that_uses_the_card_shows_its_own_effect(self):
        """★ **几个称号就写几条效果，每条行首是称号名**（用户 2026-09-13）。

        用户的实际配置就是这一型：`[队内间谍]` 要两种卡片，而其中一种
        `完美卡片` 同时又是 `[完美胜利者]` 的材料 —— 停在那张卡上时
        **两个称号的效果都得写出来**，只写一条人分不清写的是哪个。
        """
        rows = shopcfg.validate_recipes(shopcfg.default_recipes())
        for entry in rows:
            if entry["result"] == 560007:
                entry["materials"] = [{"id": 60007, "count": 20},
                                      {"id": 60001, "count": 1}]
        self.write_recipes(rows)
        effects = self.desc(60001).split(shopcfg.DESC_SEPARATOR)[1]
        lines = effects.split("\n")
        self.assertEqual(2, len(lines), effects)
        for title in ("[完美胜利者]", "[队内间谍]"):
            line = [l for l in lines if l.startswith(title)]
            self.assertEqual(1, len(line), title)
            self.assertIn(shopcfg.title_effect_zh(
                560001 if title == "[完美胜利者]" else 560007), line[0])

    def test_the_effect_line_carries_the_title_name_even_when_alone(self):
        """★ 只有一个称号时**也**写名字（用户 2026-09-13）。

        不写的话那一行看上去像是**卡片自己**的效果 —— 卡片穿不上身，
        它没有效果。
        """
        effects = self.desc(60004).split(shopcfg.DESC_SEPARATOR)[1]
        self.assertTrue(effects.startswith("[幸运幸存者]"), effects)

    def test_too_many_titles_get_truncated_but_still_say_so(self):
        """放不下的那些至少要说一声有 —— 不说的话玩家会以为只有这几个。"""
        rows = shopcfg.validate_recipes(shopcfg.default_recipes())
        for entry in rows:
            if entry["result"] in (560001, 560002, 560003, 560005, 560006,
                                   560007, 560008):
                entry["materials"] = [{"id": 60004, "count": 20}]
        self.write_recipes(rows)
        text = self.desc(60004)
        self.assertIn("另有", text)
        segments = text.split(shopcfg.DESC_SEPARATOR)
        self.assertLessEqual(len(segments[0].split("\n")),
                             shopcfg.ITEM_DESC_MAX_LINES)
        self.assertLessEqual(len(segments[1].split("\n")),
                             shopcfg.ITEM_DESC_MAX_LINES_2)

    def write_recipes(self, rows):
        shopcfg.write_json(
            shopcfg.path_of(shopcfg.RECIPE_FILENAME, self.tmp.name),
            {"format": shopcfg.FORMAT, "recipes": rows})
        shopcfg.invalidate(self.tmp.name)

    def test_the_description_never_contains_a_bare_separator_in_the_text(self):
        """⚠ `|` 是客户端的**分段符**（`wcstok`，`0x5fa904`）。

        说明正文里混进一个就会把一段拦腰切开，而且**客户端不会报错** ——
        画面上只会少半句话。所以段数必须 ≤2（一个分隔符）。
        """
        for card in shopcfg.ALL_CARDS:
            self.assertLessEqual(
                self.desc(card).count(shopcfg.DESC_SEPARATOR), 1, card)

    def test_each_segment_fits_the_client_box(self):
        """客户端**只画前 2 段**，第 1 段 ≈5 行、第 2 段 ≈3 行（§31 / §53）。"""
        for card in shopcfg.ALL_CARDS:
            segments = self.desc(card).split(shopcfg.DESC_SEPARATOR)
            self.assertLessEqual(len(segments), 2, card)
            self.assertLessEqual(len(segments[0].split("\n")),
                                 shopcfg.ITEM_DESC_MAX_LINES, card)
            if len(segments) > 1:
                self.assertLessEqual(len(segments[1].split("\n")),
                                     shopcfg.ITEM_DESC_MAX_LINES_2, card)

    def test_an_ordinary_material_still_has_no_description(self):
        """★ 只有卡片是例外：珠子 / 矿料照旧留白（`KIND_USAGE_ZH` 故意不收）。"""
        self.assertEqual("", self.desc(10001))       # 黑色小珠
        self.assertEqual("", self.desc(30018))       # 青铜管

    def test_a_card_that_cannot_be_earned_says_so(self):
        rules = shopcfg.validate_cards(shopdefaults.default_cards())
        for entry in rules:
            entry["listed"] = False
        shopcfg.write_json(
            shopcfg.path_of(shopcfg.CARDS_FILENAME, self.tmp.name),
            {"format": shopcfg.FORMAT, "rules": rules})
        shopcfg.invalidate(self.tmp.name)
        self.assertIn("暂时无法获得", self.desc(60004))

    def test_changing_the_rule_changes_the_description(self):
        """★ 改完保存即刻生效（服务端这一侧）—— 说明是**现算**的，不是快照。"""
        self.assertIn(shopcfg.CARD_OP_ZH[shopcfg.CARD_OP_GE] + "36",
                      self.desc(60004))
        rules = shopcfg.validate_cards(shopdefaults.default_cards())
        for entry in rules:
            if entry["card"] == 60004:
                entry["conditions"][0]["threshold"] = 7
        shopcfg.write_json(
            shopcfg.path_of(shopcfg.CARDS_FILENAME, self.tmp.name),
            {"format": shopcfg.FORMAT, "rules": rules})
        shopcfg.invalidate(self.tmp.name)
        self.assertIn(shopcfg.CARD_OP_ZH[shopcfg.CARD_OP_GE] + "7",
                      self.desc(60004))

    def test_the_fight_master_title_warns_that_it_never_fires(self):
        """⚠ `560002` 的加成要求格斗模式，而中国区客户端根本选不到那个模式
        —— 说明里必须写明白，不然玩家攒 50 张卡换一个空壳。"""
        self.assertIn("不会触发", self.desc(560002))


if __name__ == "__main__":
    unittest.main()
