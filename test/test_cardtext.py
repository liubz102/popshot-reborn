#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""★★ **管理页那句说明和服务端念的必须一字不差**（V0.3商店，2026-09-13）。

一条规则念成的那句话（`shopcfg.describe_card_rule`）有**两个实现**：

    server/shopcfg.py   游戏里的物品提示框 / 管理页浮窗 / 合并冲突提示
    server/web/admin.js 「达成条件」弹窗顶上那句**实时**说明 + 每一行中间那句

为什么要两份：弹窗得在改一格的当场把句子重画出来，来回问服务端太慢。
**词**是单源的（`CARD_PHRASES` 那一套经 `/admin/api/catalog` 发过去），
但**句子的骨架**（范围前缀提不提到最前面、混用连接词时括号扣在哪儿、
枚举指标念「为」而不是「大于等于」）两边各写了一遍。

⇒ 这个文件就是那道拦网：拿 `node` 把**真的** `admin.js` 跑起来，
对同一批规则各念一遍，**逐字相等**才算过。改哪一边忘了改另一边，
这里当场红 —— 而不是几个月后有人发现「页面上写的和游戏里写的不一样」。

★ 没装 node 的机器跳过（服务端包在 Linux 上跑测试时不一定有）。
  开发机 `C:\\SSD\\Program\\nodejs` 是有的，本机改代码就一定会跑到。
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import shopcfg                                                 # noqa: E402
import shopdefaults                                            # noqa: E402

ADMIN_JS = os.path.join(SERVER, "web", "admin.js")
NODE = shutil.which("node")

#: ★ 把每一条分支都点亮的一批规则：不限 / 对战 / 闯关带关卡难度、
#: 一条 / 两条 / 混用连接词、枚举指标、带单位的比率、带武器的、累计档、
#: 两档混着用、关掉的、条件为空的。**少一类就少拦一种走样**。
CASES = [
    {"card": 60001, "listed": True, "mode": "pvp", "conditions": [
        {"scope": "match", "metric": "deaths", "op": "eq", "threshold": 0},
        {"scope": "match", "metric": "won", "op": "eq", "threshold": 1,
         "join": "and"}]},
    {"card": 60002, "listed": True, "conditions": [
        {"scope": "match", "metric": "hits", "op": "ge", "threshold": 30}]},
    {"card": 60003, "listed": True, "mode": "quest", "stage": 3,
     "difficulty": 2, "conditions": [
         {"scope": "match", "metric": "kills", "op": "ge", "threshold": 5}]},
    {"card": 60004, "listed": True, "mode": "pvp", "conditions": [
        {"scope": "match", "metric": "accuracy", "op": "ge", "threshold": 50},
        {"scope": "match", "metric": "shots", "op": "ge", "threshold": 30,
         "join": "and"}]},
    {"card": 60005, "listed": True, "mode": "pvp", "conditions": [
        {"scope": "match", "metric": "kills", "op": "ge", "threshold": 5,
         "weapon": 110001}]},
    {"card": 60006, "listed": True, "conditions": [
        {"scope": "total", "metric": "kills", "op": "ge", "threshold": 100},
        {"scope": "total", "metric": "games", "op": "ge", "threshold": 10,
         "join": "and"}]},
    # 两档混着用 ⇒ 前缀提不到最前面，每一条各写各的
    {"card": 60007, "listed": True, "conditions": [
        {"scope": "match", "metric": "kills", "op": "ge", "threshold": 3},
        {"scope": "total", "metric": "games", "op": "ge", "threshold": 50,
         "join": "and"}]},
    # 连接词混用 ⇒ 括号要扣在同一个地方
    {"card": 60008, "listed": True, "conditions": [
        {"scope": "match", "metric": "kills", "op": "ge", "threshold": 5},
        {"scope": "match", "metric": "hits", "op": "ge", "threshold": 30,
         "join": "or"},
        {"scope": "match", "metric": "guards", "op": "ge", "threshold": 3,
         "join": "and"}]},
    # 四条、两种连接词交替 ⇒ 括号要套两层
    {"card": 110001, "listed": True, "conditions": [
        {"scope": "match", "metric": "kills", "op": "ge", "threshold": 5},
        {"scope": "match", "metric": "hits", "op": "ge", "threshold": 30,
         "join": "or"},
        {"scope": "match", "metric": "guards", "op": "ge", "threshold": 3,
         "join": "and"},
        {"scope": "match", "metric": "deaths", "op": "le", "threshold": 2,
         "join": "or"}]},
    {"card": 110002, "listed": False, "conditions": [
        {"scope": "match", "metric": "kills", "op": "ge", "threshold": 1}]},
    {"card": 110003, "listed": True, "conditions": []},
]

#: ★ 在 node 里把 `admin.js` 原样跑一遍，只问它 `cardRuleText()` 念出来什么。
#:
#: 那个文件是给浏览器写的：最底下有一发 IIFE 会 `wire()` 一整页的 DOM、
#: 还会 `fetch` 一发会话。⇒ 这儿给一个**什么都能点、什么都返回自己**的
#: `document` 替身（Proxy）把它接住 —— 不改 `admin.js` 一个字，
#: 拦网量的才是真的那份代码。
RUNNER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const payload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

// 什么属性都返回自己、还能当函数调的空壳。
const sink = new Proxy(function () {}, {
  get(_t, key) {
    if (key === Symbol.toPrimitive || key === "toString") { return () => ""; }
    if (key === "length") { return 0; }
    return sink;
  },
  set() { return true; },
  apply() { return sink; },
  has() { return true; },
});
globalThis.document = sink;
globalThis.window = sink;
globalThis.location = sink;
globalThis.navigator = sink;
globalThis.fetch = async () => ({json: async () => ({ok: true,
                                                    logged_in: false})});

// `admin.js` 是一整个脚本（不是模块），里面的函数都挂在顶层作用域上 ——
// ⚠ 不能用 `require`（那是模块作用域，函数出不来），也别指望 indirect eval
//   （node 的 CJS 包装层下它照样落不进 globalThis）。`vm.runInThisContext`
//   才是「当成一段 <script> 跑」，函数声明这才挂得到 globalThis 上。
require("vm").runInThisContext(src, {filename: process.argv[2]});

globalThis.CAT = payload.catalog;
const out = payload.rules.map((rule) => cardRuleText(rule));
process.stdout.write(JSON.stringify(out));
"""


def catalog_for_text():
    """说明文用得到的那几格 —— 和 `/admin/api/catalog` 里发的是同一份。"""
    return {
        "card_metrics": shopcfg.card_metrics_for_admin(),
        "card_text": {
            "phrases": shopcfg.CARD_PHRASES,
            "modes": shopcfg.CARD_MODE_ZH,
            "scope_prefix": shopcfg.CARD_SCOPE_PREFIX_ZH,
            "ops": shopcfg.CARD_OP_ZH,
            "every": shopcfg.CARD_EVERY_ZH,
            "joins": shopcfg.CARD_JOIN_ZH,
            "quests": {str(k): v for k, v in shopcfg.QUEST_ZH.items()},
            "difficulties": {str(k): v
                             for k, v in shopcfg.DIFFICULTY_ZH.items()},
            "weapons": {str(roh): shopcfg.weapon_roh_label(roh)
                        for roh in shopcfg.WEAPON_CARDS},
        },
        "card_limits": {
            "op_min": shopcfg.CARD_OP_MIN, "op_ge": shopcfg.CARD_OP_GE,
            "op_eq": shopcfg.CARD_OP_EQ, "join_and": shopcfg.CARD_JOIN_AND,
            "join_or": shopcfg.CARD_JOIN_OR,
            "scope_match": shopcfg.CARD_SCOPE_MATCH,
            "scope_total": shopcfg.CARD_SCOPE_TOTAL,
            "sample_metric": shopcfg.CARD_SAMPLE_METRIC,
            "max_conditions": shopcfg.MAX_CARD_CONDITIONS,
            "max_threshold": shopcfg.MAX_CARD_THRESHOLD,
        },
    }


def say_in_node(rules):
    """让 `admin.js` 把这批规则各念一遍。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        runner = os.path.join(tmp, "runner.js")
        data = os.path.join(tmp, "payload.json")
        with open(runner, "w", encoding="utf-8") as fp:
            fp.write(RUNNER)
        with open(data, "w", encoding="utf-8") as fp:
            json.dump({"catalog": catalog_for_text(), "rules": rules}, fp,
                      ensure_ascii=False)
        done = subprocess.run([NODE, runner, ADMIN_JS, data],
                              capture_output=True)
    if done.returncode != 0:
        raise AssertionError("node 跑 admin.js 没跑通：\n"
                             + done.stderr.decode("utf-8", "replace"))
    return json.loads(done.stdout.decode("utf-8"))


@unittest.skipUnless(NODE, "这台机器上没有 node，跳过前后端对词")
class CardSentenceAgreementTests(unittest.TestCase):

    def test_the_page_and_the_server_read_a_rule_the_same_way(self):
        rules = CASES + shopcfg.validate_cards(shopdefaults.default_cards())
        said = say_in_node(rules)
        self.assertEqual(len(rules), len(said))
        for rule, js in zip(rules, said):
            self.assertEqual(shopcfg.describe_card_rule(rule), js,
                             "卡片 %s 两边念得不一样" % rule["card"])

    def test_the_cases_really_cover_every_branch(self):
        """★ 上面那批用例真的把每条分支都点亮了 —— 不点亮就拦不住走样。"""
        joined = "\n".join(shopcfg.describe_card_rule(r) for r in CASES)
        self.assertIn(shopcfg.CARD_PHRASES["off"], joined, "关着的那一档")
        self.assertIn(shopcfg.CARD_PHRASES["bad"], joined, "条件为空那一档")
        self.assertIn(shopcfg.CARD_PHRASES["open"], joined, "混用连接词的括号")
        self.assertIn(shopcfg.CARD_PHRASES["is"], joined, "枚举指标那个「为」")
        self.assertIn(shopcfg.CARD_EVERY_ZH, joined, "累计那个「每满」")
        self.assertIn("%", joined, "带单位的比率")
        self.assertIn(shopcfg.CARD_PHRASES["any_mode"], joined, "不限模式")
        self.assertIn(shopcfg.WEAPON_ROH_ZH[110001], joined, "带武器的")
        for zh in shopcfg.CARD_SCOPE_PREFIX_ZH.values():
            self.assertIn(zh, joined, zh)
        for zh in shopcfg.CARD_JOIN_ZH.values():
            self.assertIn(zh, joined, zh)


if __name__ == "__main__":
    unittest.main()
