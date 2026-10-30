#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""★★ **晋级任务那句话，管理页和服务端必须一字不差**（X23，2026-10-30）。

一条任务念成的那句话（条件 / 默认名字）有**两个实现**：

    server/missions.py   游戏里任务面板上的名字 / 完成条件、结算聊天
    server/web/admin.js  「晋级任务」页每一行中间那句 + 三个设置窗顶上那句（实时）

和称号卡片那一对（`test_cardtext`）同一个道理：词是单源的
（`missions.admin_text()` 经 `/admin/api/catalog` 发过去），句子骨架两边
各写了一遍 ⇒ 拿 `node` 把**真的** `admin.js` 跑起来，对同一批任务各念一遍，
**逐字相等**才算过。

顺带守一件事：管理页那道「当场看得见哪儿不对」的判据（`missionProblem`）
说有毛病的，服务端校验器也一定拒 —— 不然页面上红着的那条存得进去，
人会以为红字是瞎报的。

★ 没装 node 的机器跳过（同 `test_cardtext`）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import missions                                               # noqa: E402
import shopcfg                                                # noqa: E402
import shopdefaults                                           # noqa: E402
from test_cardtext import catalog_for_text                    # noqa: E402

ADMIN_JS = os.path.join(SERVER, "web", "admin.js")
NODE = shutil.which("node")

#: 一条奖励就够念句子了（句子里没有奖励）；判据那几条另外造。
GOLD = [{"money": 100}]

#: ★ 把每一条分支都点亮的一批任务：对战不限 / 带模式 / **只带「无道具」（值是 0）**、
#: 闯关不限 / 带关卡难度 / 「及以上」/ 表里没有的关卡号、每局达成（单条 / 多条 /
#: 枚举指标）、累计（带武器 / 不带）、专用关卡、自己起了名字的、四种周期。
CASES = [
    {"id": 1, "period": "daily", "line": "pvp", "kind": "win", "target": 3},
    {"id": 2, "period": "weekly", "line": "pvp", "kind": "win", "target": 5,
     "pvp_mode": 0, "item_mode": 1},
    # ★ 「无道具」是 0 —— 前端拿真假判断的话这一截会被吞掉。
    {"id": 3, "period": "monthly", "line": "pvp", "kind": "win", "target": 2,
     "item_mode": 0},
    {"id": 4, "period": "once", "line": "pvp", "kind": "win", "target": 1,
     "pvp_mode": 2},
    {"id": 5, "period": "daily", "line": "quest", "kind": "clear", "target": 1},
    {"id": 6, "period": "daily", "line": "quest", "kind": "clear", "target": 2,
     "stage": 3, "difficulty": 2, "harder": True},
    {"id": 7, "period": "weekly", "line": "quest", "kind": "clear", "target": 4,
     "difficulty": 1},
    {"id": 8, "period": "once", "line": "quest", "kind": "clear", "target": 1,
     "stage": 99},
    {"id": 9, "period": "daily", "line": "pvp", "kind": "stats", "count": "match",
     "target": 10, "conditions": [
         {"scope": "match", "metric": "kills", "op": "ge", "threshold": 5},
         {"scope": "match", "metric": "won", "op": "eq", "threshold": 1,
          "join": "and"}]},
    {"id": 10, "period": "weekly", "line": "quest", "kind": "stats",
     "count": "match", "target": 3, "stage": 2, "conditions": [
         {"scope": "match", "metric": "deaths", "op": "eq", "threshold": 0},
         {"scope": "match", "metric": "hits", "op": "ge", "threshold": 30,
          "join": "or"},
         {"scope": "match", "metric": "guards", "op": "ge", "threshold": 3,
          "join": "and"}]},
    {"id": 11, "period": "monthly", "line": "pvp", "kind": "stats", "count": "sum",
     "target": 500, "metric": "kills", "weapon": 110001, "pvp_mode": 3},
    {"id": 12, "period": "once", "line": "quest", "kind": "stats", "count": "sum",
     "target": 20000, "metric": "damage"},
    {"id": 13, "period": "once", "line": "quest", "kind": "map", "target": 1,
     "map": "QuestLevel01", "script": "QuestLevel01.lua", "difficulty": 1},
    {"id": 14, "period": "daily", "line": "pvp", "kind": "win", "target": 3,
     "name": "  自己起的名字  "},
]


def sentence_cases():
    """上面那批 + 出厂的全部 105 条（校验器洗过的样子，和磁盘上那份一样）。"""
    out = []
    for case in CASES:
        mission = dict(case)
        mission.setdefault("rewards", GOLD)
        mission.setdefault("character", missions.CHAR_ANY)
        out.append(mission)
    return out + missions.validate_missions(shopdefaults.default_missions())


#: ★ 管理页标红的那几类（`missionProblem`）。每一条都要被服务端拒。
BAD = [
    {"rewards": []},
    {"rewards": [{"money": 1}, {"exp": 1}, {"money": 2}, {"exp": 2}]},
    {"rewards": [{"item": 0, "count": 1}]},
    {"rewards": [{"money": 0}]},
    {"rewards": [{"exp": "abc"}]},
    {"target": 0},
    {"target": "3.5"},
    {"name": "名" * (missions.TEXT_LIMITS["name"] + 1)},
    {"info": "说" * (missions.TEXT_LIMITS["info"] + 1)},
    {"kind": "stats", "count": "sum", "metric": "accuracy"},
    {"kind": "stats", "count": "match", "conditions": []},
    {"kind": "stats", "count": "match", "conditions": [
        {"scope": "match", "metric": "kills", "op": "ge", "threshold": 0}]},
]

#: 在 node 里把 `admin.js` 原样跑一遍（同 `test_cardtext.RUNNER` 那一套空壳），
#: 只问它那几个函数念出来什么。
RUNNER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const payload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

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
require("vm").runInThisContext(src, {filename: process.argv[2]});

globalThis.CAT = payload.catalog;
const out = {
  said: payload.missions.map((m) => ({
    scope: missionScopeText(m),
    condition: missionConditionText(m),
    name: missionDisplayName(m),
    problem: missionProblem(m),
  })),
  bad: payload.bad.map((m) => missionProblem(m)),
};
process.stdout.write(JSON.stringify(out));
"""


def catalog():
    """念句子 + 判据用得到的那几格 —— 和 `/admin/api/catalog` 发的是同一份。"""
    out = catalog_for_text()
    out["mission_text"] = missions.admin_text()
    out["mission_limits"] = missions.admin_limits()
    out["schema"] = {"missions": shopcfg.SCHEMA["missions"]}
    return out


def ask_node(mission_list, bad):
    with tempfile.TemporaryDirectory() as tmp:
        runner = os.path.join(tmp, "runner.js")
        data = os.path.join(tmp, "payload.json")
        with open(runner, "w", encoding="utf-8") as fp:
            fp.write(RUNNER)
        with open(data, "w", encoding="utf-8") as fp:
            json.dump({"catalog": catalog(), "missions": mission_list,
                       "bad": bad}, fp, ensure_ascii=False)
        done = subprocess.run([NODE, runner, ADMIN_JS, data],
                              capture_output=True)
    if done.returncode != 0:
        raise AssertionError("node 跑 admin.js 没跑通：\n"
                             + done.stderr.decode("utf-8", "replace"))
    return json.loads(done.stdout.decode("utf-8"))


def good_base():
    """一条什么毛病都没有的任务（`BAD` 每一条在它上面改一处）。"""
    return {"id": missions.FIRST_NEW_ID + 1, "enabled": True, "period": "daily",
            "character": missions.CHAR_ANY, "line": "pvp", "order": 0,
            "level": 1, "kind": "win", "target": 1, "name": "", "info": "",
            "clear_text": "", "rewards": [{"money": 100}]}


@unittest.skipUnless(NODE, "这台机器上没有 node，跳过前后端对词")
class MissionSentenceAgreementTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.cases = sentence_cases()
        cls.bad = []
        for patch in BAD:
            entry = good_base()
            entry.update(patch)
            cls.bad.append(entry)
        cls.answer = ask_node(cls.cases, cls.bad)

    def test_the_page_and_the_server_read_a_mission_the_same_way(self):
        said = self.answer["said"]
        self.assertEqual(len(self.cases), len(said))
        for mission, js in zip(self.cases, said):
            where = "任务 %s" % mission["id"]
            self.assertEqual(missions.scope_text(mission), js["scope"], where)
            self.assertEqual(missions.condition_text(mission), js["condition"],
                             where)
            self.assertEqual(missions.display_name(mission), js["name"], where)

    def test_the_page_finds_nothing_wrong_with_the_factory_missions(self):
        """出厂那 105 条（和上面那批好的）在管理页上不该是红的。"""
        for mission, js in zip(self.cases, self.answer["said"]):
            self.assertEqual("", js["problem"], "任务 %s" % mission["id"])

    def test_whatever_the_page_marks_red_the_server_refuses(self):
        for entry, why in zip(self.bad, self.answer["bad"]):
            self.assertTrue(why, "管理页没看出毛病：%r" % (entry,))
            with self.assertRaises(shopcfg.ConfigError, msg=why):
                missions.validate_missions({"missions": [entry]})

    def test_the_cases_really_cover_every_branch(self):
        """★ 上面那批用例真的把每条分支都点亮了 —— 不点亮就拦不住走样。"""
        shapes = set((m["kind"], m.get("count")) for m in self.cases)
        for shape in (("win", None), ("clear", None), ("map", None),
                      ("stats", "match"), ("stats", "sum")):
            self.assertIn(shape, shapes, "句子的五种骨架各要有一条")
        joined = "\n".join(missions.condition_text(m) for m in self.cases)
        words = missions.MISSION_PHRASES
        self.assertIn(words["harder"], joined, "及以上")
        self.assertIn(words["filter_sep"], joined, "两个筛选之间那个点")
        self.assertIn(missions.ITEM_MODE_ZH[0], joined, "值为 0 的「无道具」")
        self.assertIn(shopcfg.WEAPON_ROH_ZH[110001], joined, "带武器的累计")
        self.assertIn("99", joined, "表里没有的关卡号")
        names = "\n".join(missions.display_name(m) for m in self.cases)
        for period in missions.PERIODS:
            self.assertIn("[%s]" % missions.PERIOD_NAME_ZH[period], names, period)
        self.assertIn("自己起的名字", names)


if __name__ == "__main__":
    unittest.main()
