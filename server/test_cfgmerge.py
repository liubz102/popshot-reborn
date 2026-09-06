#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理页保存时的三方合并（`cfgmerge`，V0.3商店 D36）。

★ 这一组是**纯逻辑**，不起 HTTP —— 走 HTTP 的那一半在
`test_web_admin.AdminConfigConflictTests` 里。两边都要有：
这里钉「合并算得对不对」，那里钉「撞车时到底有没有写盘」。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cfgmerge                                                  # noqa: E402


def shop(item_id, price=100, listed=True):
    return {"id": item_id, "kind": "weapon", "price": price, "listed": listed}


def recipe(rid, result, cost=100, listed=True):
    return {"id": rid, "result": result, "cost": cost, "listed": listed,
            "materials": [{"id": 30018, "count": 1}]}


def drop(material, stage=None, difficulty=None, mode="quest", prob=50):
    rule = {"mode": mode, "material": material, "count": 1, "prob": prob,
            "cleared_only": True}
    if stage is not None:
        rule["stage"] = stage
    if difficulty is not None:
        rule["difficulty"] = difficulty
    return rule


def ids(result, which="shop"):
    """合并结果里的记录，按身份字符串排一下好比。"""
    return [cfgmerge.key_text(key)
            for key, _entry in cfgmerge.index_entries(which, result.entries)]


class KeyTests(unittest.TestCase):

    def test_前三份按_id_认身份(self):
        self.assertEqual((1120041,), cfgmerge.natural_key("shop", shop(1120041)))
        self.assertEqual((7,), cfgmerge.natural_key("recipe", recipe(7, 1010064)))
        self.assertEqual((30018,),
                         cfgmerge.natural_key("items", {"id": 30018}))

    def test_掉落按四元组认身份(self):
        # ★ 用户 2026-09-06 拍板：同一种材料可以有好几条不同关卡 / 难度的规则，
        #   「在什么情况下掉什么」才是运营心里那条规则的身份。
        self.assertEqual(("quest", 5, 3, 20007),
                         cfgmerge.natural_key("drops", drop(20007, 5, 3)))
        self.assertEqual(("quest", None, None, 20007),
                         cfgmerge.natural_key("drops", drop(20007)))

    def test_没写_mode_的老规则和选了闯关的新规则是同一条(self):
        # `validate_drops` 把缺省的 mode 补成 quest —— 两边不一致的话，
        # 老文件里的规则会被当成「对方新加的一条」，凭空多出一行。
        old = {"material": 20007, "count": 1, "prob": 50}
        new = {"mode": "quest", "material": 20007, "count": 1, "prob": 50}
        self.assertEqual(cfgmerge.natural_key("drops", old),
                         cfgmerge.natural_key("drops", new))

    def test_同一个四元组写两条按出现先后配对(self):
        rows = [drop(20007, 5, 3, prob=10), drop(20007, 5, 3, prob=20)]
        keys = [key for key, _e in cfgmerge.index_entries("drops", rows)]
        self.assertEqual([(("quest", 5, 3, 20007), 0),
                          (("quest", 5, 3, 20007), 1)], keys)

    def test_身份能塞进_json_再原样比对(self):
        key = (("quest", None, 3, 20007), 1)
        self.assertEqual("quest||3|20007#1", cfgmerge.key_text(key))


class MergeTests(unittest.TestCase):

    def test_改了不同的条目直接合并(self):
        base = [shop(1), shop(2)]
        mine = [shop(1, price=999), shop(2)]
        theirs = [shop(1), shop(2, price=777)]
        result = cfgmerge.merge("shop", base, mine, theirs)
        self.assertTrue(result.clean)
        prices = dict((e["id"], e["price"]) for e in result.entries)
        self.assertEqual({1: 999, 2: 777}, prices)
        # 我改的一条 + 对方改的一条，各自归各自的清单。
        self.assertEqual([((1,), 0)], result.applied)
        self.assertEqual([((2,), 0)], result.adopted)

    def test_改了同一条就撞车(self):
        base = [shop(1)]
        result = cfgmerge.merge("shop", base, [shop(1, price=999)],
                                [shop(1, price=777)])
        self.assertFalse(result.clean)
        self.assertEqual([(((1,), 0), cfgmerge.REASON_BOTH_EDITED)],
                         result.conflicts)

    def test_撞车时算出来的是只提交没撞车那些的结果(self):
        """★ 调用方拿它接着判互斥 —— 两种撞车要在同一个框里一次说完。
        写不写盘看的是 `clean`，不是 `entries` 空不空。"""
        base = [shop(1), shop(2)]
        result = cfgmerge.merge("shop", base,
                                [shop(1, price=999), shop(2, price=888)],
                                [shop(1, price=777), shop(2)])
        self.assertFalse(result.clean)
        prices = dict((e["id"], e["price"]) for e in result.entries)
        self.assertEqual({1: 777, 2: 888}, prices,
                         "撞车那条用对方的，没撞的那条用我的")

    def test_两个人改成一样的不算撞车(self):
        base = [shop(1)]
        same = [shop(1, price=999)]
        result = cfgmerge.merge("shop", base, same, list(same))
        self.assertTrue(result.clean)
        self.assertEqual(999, result.entries[0]["price"])

    def test_对方加的条目我没动就跟着进来(self):
        result = cfgmerge.merge("shop", [shop(1)], [shop(1)],
                                [shop(1), shop(2)])
        self.assertTrue(result.clean)
        self.assertEqual([1, 2], [e["id"] for e in result.entries])
        self.assertEqual([((2,), 0)], result.adopted)

    def test_我加的条目追加在末尾(self):
        result = cfgmerge.merge("shop", [shop(1)], [shop(1), shop(9)],
                                [shop(1), shop(2)])
        self.assertTrue(result.clean)
        # ★ 骨架是磁盘上那份（对方的顺序），我的新增排最后。
        self.assertEqual([1, 2, 9], [e["id"] for e in result.entries])

    def test_两个人各加了一条同身份但不同内容的记录(self):
        result = cfgmerge.merge("shop", [], [shop(9, price=1)],
                                [shop(9, price=2)])
        self.assertEqual([(((9,), 0), cfgmerge.REASON_BOTH_ADDED)],
                         result.conflicts)

    def test_我删的条目对方没动就真的删掉(self):
        result = cfgmerge.merge("shop", [shop(1), shop(2)], [shop(1)],
                                [shop(1), shop(2)])
        self.assertTrue(result.clean)
        self.assertEqual([1], [e["id"] for e in result.entries])

    def test_我删了对方改了是撞车(self):
        result = cfgmerge.merge("shop", [shop(1), shop(2)], [shop(1)],
                                [shop(1), shop(2, price=5)])
        self.assertEqual([(((2,), 0), cfgmerge.REASON_I_DELETED)],
                         result.conflicts)

    def test_对方删了我改了是撞车(self):
        result = cfgmerge.merge("shop", [shop(1), shop(2)],
                                [shop(1), shop(2, price=5)], [shop(1)])
        self.assertEqual([(((2,), 0), cfgmerge.REASON_THEY_DELETED)],
                         result.conflicts)

    def test_两个人都删了同一条不算撞车(self):
        result = cfgmerge.merge("shop", [shop(1), shop(2)], [shop(1)],
                                [shop(1)])
        self.assertTrue(result.clean)
        self.assertEqual([1], [e["id"] for e in result.entries])

    def test_键的顺序变了不算改动(self):
        # 前台改一个可选字段会把键删掉再加回来 —— 顺序不该被当成改动，
        # 否则「我什么都没改」也会一路撞到别人头上。
        base = [{"id": 1, "price": 5, "listed": True}]
        mine = [{"listed": True, "id": 1, "price": 5}]
        theirs = [{"id": 1, "price": 9, "listed": True}]
        result = cfgmerge.merge("shop", base, mine, theirs)
        self.assertTrue(result.clean)
        self.assertEqual(9, result.entries[0]["price"])

    def test_掉落改不同的规则也能合并(self):
        base = [drop(20007, 5, 3), drop(20007, 6, 3)]
        mine = [drop(20007, 5, 3, prob=99), drop(20007, 6, 3)]
        theirs = [drop(20007, 5, 3), drop(20007, 6, 3, prob=11)]
        result = cfgmerge.merge("drops", base, mine, theirs)
        self.assertTrue(result.clean)
        self.assertEqual([99, 11], [r["prob"] for r in result.entries])

    def test_掉落改同一条规则撞车(self):
        base = [drop(20007, 5, 3)]
        result = cfgmerge.merge("drops", base, [drop(20007, 5, 3, prob=99)],
                                [drop(20007, 5, 3, prob=11)])
        self.assertFalse(result.clean)


class OnlyTests(unittest.TestCase):
    """「单独提交未冲突物品」那一轮。"""

    def test_只提交点名的那几条(self):
        base = [shop(1), shop(2)]
        mine = [shop(1, price=999), shop(2, price=888)]
        theirs = [shop(1), shop(2, price=777)]        # 对方改了 2
        only = {cfgmerge.key_text(((1,), 0))}
        result = cfgmerge.merge("shop", base, mine, theirs, only=only)
        self.assertTrue(result.clean)
        prices = dict((e["id"], e["price"]) for e in result.entries)
        # 1 是我的，2 用对方的 —— 我对 2 的改动被丢掉了（就是我点确定的意思）。
        self.assertEqual({1: 999, 2: 777}, prices)

    def test_点名的那条这时候被第三个人改了要重新报冲突(self):
        """★ 用户 2026-09-06 明确要求：单独提交前**重新检测一次**。"""
        base = [shop(1)]
        mine = [shop(1, price=999)]
        theirs = [shop(1, price=555)]                 # 第三个人插进来了
        only = {cfgmerge.key_text(((1,), 0))}
        result = cfgmerge.merge("shop", base, mine, theirs, only=only)
        self.assertFalse(result.clean)


class ListingConflictTests(unittest.TestCase):
    """商店 ⇄ 合成互斥 —— 我在商店上架，对方同时在合成上架。"""

    def test_对方刚在合成里上架了同一件东西(self):
        my_base = [shop(1010064, listed=False)]
        merged = [shop(1010064, listed=True)]         # 我这次让它上架
        cross_base = [recipe(1, 1010064, listed=False)]
        cross_theirs = [recipe(1, 1010064, listed=True)]   # 对方这次让它上架
        rows = cfgmerge.listing_conflicts("shop", my_base, merged,
                                          cross_base, cross_theirs)
        self.assertEqual([(((1010064,), 0), 1010064)], rows)

    def test_我本来就上着架的不算(self):
        # ★ 窄口径：「我 base 里就已经在商店上架、这次没动它」不该拦我
        #   保存别的东西 —— 那是对方造成的不一致。
        my_base = [shop(1010064, listed=True)]
        merged = [shop(1010064, listed=True)]
        rows = cfgmerge.listing_conflicts(
            "shop", my_base, merged,
            [recipe(1, 1010064, listed=False)], [recipe(1, 1010064)])
        self.assertEqual([], rows)

    def test_对方本来就上着架的走前台那道确认框(self):
        # 我手上的 recipe 副本已经显示它在合成上架了 ⇒ D33 的 `listingClash()`
        # 会弹「自动下架合成」，不该在这儿再报一次冲突。
        rows = cfgmerge.listing_conflicts(
            "shop", [shop(1010064, listed=False)], [shop(1010064, listed=True)],
            [recipe(1, 1010064, listed=True)], [recipe(1, 1010064, listed=True)])
        self.assertEqual([], rows)

    def test_合成那一边同理(self):
        rows = cfgmerge.listing_conflicts(
            "recipe", [recipe(1, 1010064, listed=False)],
            [recipe(1, 1010064, listed=True)],
            [shop(1010064, listed=False)], [shop(1010064, listed=True)])
        # ★ 配方按**配方号**认身份，但撞的是**产物**那件东西。
        self.assertEqual([(((1,), 0), 1010064)], rows)

    def test_物品库和掉落没有另一边(self):
        self.assertEqual([], cfgmerge.listing_conflicts(
            "items", [], [], [], []))


class LabelTests(unittest.TestCase):
    """提示框里那一行字 —— 名字一律走 `shopcfg.item_name()`（D31）。"""

    def test_商店条目写名字和_id(self):
        label = cfgmerge.label_of("shop", ((1120041,), 0), shop(1120041))
        self.assertIn("#1120041", label)
        self.assertIn("左轮", label)

    def test_配方条目还要写配方号(self):
        label = cfgmerge.label_of("recipe", ((7,), 0), recipe(7, 1120041))
        self.assertIn("配方 #7", label)
        self.assertIn("左轮", label)

    def test_掉落规则写清楚是哪一档(self):
        label = cfgmerge.label_of("drops", (("quest", 5, 3, 30018), 0),
                                  drop(30018, 5, 3))
        self.assertIn("青铜管", label)
        self.assertIn("闯关", label)
        self.assertIn("黑骑士", label)          # 关卡 5
        self.assertIn("困难", label)            # 难度 3

    def test_不限关卡不限难度也说得出来(self):
        label = cfgmerge.label_of("drops", (("pvp", None, None, 30018), 0),
                                  drop(30018, mode="pvp"))
        self.assertIn("对战", label)
        self.assertIn("不限关卡", label)
        self.assertIn("不限难度", label)


if __name__ == "__main__":
    unittest.main()
