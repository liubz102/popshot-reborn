#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""★ 下载日志弹窗的「压缩等级 低 / 高」（X17 追加，用户 2026-09-30，D102）。

用户要的三件事，判据全在 `admin.js` 的几个纯函数 + 一个全局变量里：

    logsLevel(data)            这次用哪一档：自己选过用选的，没选过跟服务端给的默认
    archiveRequest(what, data) 「压缩」那一发的请求体 —— 必须带上 level（服务端必填）
    logsLevelHint(info, level) 小字：所选那档要多少内存 + 这台服务器内存 + 默认怎么来的
    LOGS_LEVEL                 null = 这次进系统还没选过

最容易在日后被「顺手统一掉」的三条：

1. **关弹窗不清 `LOGS_LEVEL`**（用户原话「关掉 popup 时需要保持用户选择的值」）。
   有人觉得「每开一次弹窗一份新状态」更干净、把它挪进 `LOGS` 里的话，这里当场红。
2. **进系统才清**：登录、刷新页面带着会话进来都走 `showLoggedIn` —— 清在那里
   （用户定：刷新也算重新进系统）。清在 `showLoggedOut` 的话刷新页面不回默认。
3. **小字里的数字全来自服务端**（`logshelf.level_info`）：JS 里写死「186 MB」的话，
   估算一改就两头对不上 —— 用例故意喂一组和真值不一样的数，看它是不是照抄。

★ 跑法同 `test_adminpicker`：`vm.runInThisContext` 把**真的** `admin.js` 当一段
  `<script>` 跑，`document` / `window` 用 Proxy 空壳接住。没装 node 的机器跳过。
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

ADMIN_JS = os.path.join(SERVER, "web", "admin.js")
NODE = shutil.which("node")

#: 在 node 里把 `admin.js` 跑起来，然后逐条执行 `payload.probes`，结果原样收进数组。
#:
#: `probe` 的形状：
#:   {"call": "函数名", "args": [...]}   叫一个顶层函数
#:   {"set": ["全局名", 值]}             改一个全局（模拟「点了哪个钮」「弹窗开着」）
#:   {"global": "全局名"}                读回一个全局
#:   {"loggedIn": true}                  叫**真的** `showLoggedIn`（它画外壳的那几发换成空操作）
RUNNER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const payload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

const nodes = {};
function fakeNode(id) {
  if (!nodes[id]) {
    nodes[id] = {id: id, textContent: "", disabled: false, value: "",
                 children: [], style: {},
                 classList: {add() {}, remove() {}, toggle() {},
                             contains() { return false; }},
                 appendChild() {}, removeChild() {}, remove() {},
                 setAttribute() {}, addEventListener() {},
                 querySelector() { return null; },
                 querySelectorAll() { return []; }};
  }
  return nodes[id];
}

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
globalThis.requestAnimationFrame = () => 0;
globalThis.fetch = async () => ({json: async () => ({ok: true,
                                                     logged_in: false})});
require("vm").runInThisContext(src, {filename: process.argv[2]});

// ★ 只能在 `runInThisContext` **之后**换（顶层函数声明会盖掉先换的）。
globalThis.$ = fakeNode;
// 真的 `showLoggedIn` 留一份给探针用；全局那个换成空操作 —— 文件末尾的 IIFE 会在微任务里
// 叫 `showLoggedOut` / `showLoggedIn` 画整页外壳，那和这份用例无关（同 `test_adminpicker`）。
const realShowLoggedIn = showLoggedIn;
globalThis.showLoggedOut = function () {};
globalThis.showLoggedIn = function () {};
// `showLoggedIn` 里画外壳、拉数据的那几发：用例只量「进系统时压缩等级清回默认」这一件事。
globalThis.paintWho = function () {};
globalThis.paintTabChrome = function () {};
globalThis.applyRoleToTabs = function () {};
globalThis.applyReadOnly = function () {};
globalThis.boot = async function () {};
globalThis.toast = function () { return null; };

const out = [];
payload.probes.forEach((probe) => {
  if (probe.set) {
    globalThis[probe.set[0]] = probe.set[1];
    out.push(null);
  } else if (probe.global) {
    out.push(globalThis[probe.global] === undefined ? null : globalThis[probe.global]);
  } else if (probe.loggedIn) {
    realShowLoggedIn("admin", "system", "");
    out.push(null);
  } else {
    // JSON 往返一次：请求体里的 undefined 字段和真 `fetch` 发出去时一样被丢掉。
    out.push(JSON.parse(JSON.stringify(globalThis[probe.call](...probe.args)) || "null"));
  }
});
process.stdout.write(JSON.stringify(out));
"""


def level_info(default="high", memory_text="7.7 GB", low_memory=12, high_memory=345):
    """`logshelf.level_info()` 的形状。★ 内存估算故意和真值（9 / 186 MB）不一样：
    小字要是照写死的数出来，这里就对不上。"""
    return {
        "default": default,
        "memory": None,
        "memory_text": memory_text,
        "low_memory_text": "800 MB",
        "levels": {
            "low": {"label": "低", "memory": low_memory << 20,
                    "memory_text": "%d MB" % low_memory, "like": "7-Zip「快速压缩」"},
            "high": {"label": "高", "memory": high_memory << 20,
                     "memory_text": "%d MB" % high_memory, "like": "7-Zip「标准压缩」"},
        },
    }


def overview(**kwargs):
    """`/admin/api/logs` 回包里这份用例用得到的那两格。"""
    return {"sevenzip": True, "sevenzip_level": level_info(**kwargs)}


def run(probes):
    with tempfile.TemporaryDirectory() as tmp:
        runner = os.path.join(tmp, "runner.js")
        data = os.path.join(tmp, "payload.json")
        with open(runner, "w", encoding="utf-8") as fp:
            fp.write(RUNNER)
        with open(data, "w", encoding="utf-8") as fp:
            json.dump({"probes": probes}, fp, ensure_ascii=False)
        done = subprocess.run([NODE, runner, ADMIN_JS, data], capture_output=True)
    if done.returncode != 0:
        raise AssertionError("node 跑 admin.js 没跑通：\n"
                             + done.stderr.decode("utf-8", "replace"))
    return json.loads(done.stdout.decode("utf-8"))


@unittest.skipUnless(NODE, "这台机器上没有 node，跳过下载日志弹窗压缩等级的拦网")
class LogsLevelTests(unittest.TestCase):

    def test_until_someone_picks_it_follows_the_server(self):
        """进系统后第一次开弹窗：勾的是服务端按内存给的默认（小内存服务器 = 低）。"""
        got = run([{"global": "LOGS_LEVEL"},
                   {"call": "logsLevel", "args": [overview(default="high")]},
                   {"call": "logsLevel", "args": [overview(default="low")]}])
        self.assertEqual([None, "high", "low"], got)

    def test_a_pick_wins_over_the_default_and_survives_closing_the_popup(self):
        data = overview(default="high")
        got = run([{"set": ["LOGS", {"data": data}]},
                   {"set": ["LOGS_LEVEL", "low"]},            # 点了「低」（onclick 里就这一句）
                   {"call": "logsLevel", "args": [data]},
                   {"call": "closeLogsModal", "args": []},
                   {"global": "LOGS"},
                   {"global": "LOGS_LEVEL"},
                   {"call": "logsLevel", "args": [data]}])     # 再开弹窗
        self.assertEqual("low", got[2])
        self.assertIsNone(got[4])                              # 弹窗那份状态清掉了……
        self.assertEqual("low", got[5])                        # ……压缩等级没清（用户要的）
        self.assertEqual("low", got[6])

    def test_entering_the_system_goes_back_to_the_default(self):
        """登录、刷新页面都走 `showLoggedIn`：选过的清掉，重新跟服务端的默认。"""
        data = overview(default="high")
        got = run([{"set": ["LOGS_LEVEL", "low"]},
                   {"loggedIn": True},
                   {"global": "LOGS_LEVEL"},
                   {"call": "logsLevel", "args": [data]}])
        self.assertEqual([None, None, None, "high"], got)

    def test_the_compress_request_carries_the_level(self):
        data = overview(default="low")
        got = run([{"call": "archiveRequest", "args": [{"kind": "server", "scope": "recent"}, data]},
                   {"set": ["LOGS_LEVEL", "high"]},
                   {"call": "archiveRequest",
                    "args": [{"kind": "client_crash", "sub": "alice_a1b2c3d4_20260909-013642"},
                             data]}])
        self.assertEqual({"kind": "server", "scope": "recent", "level": "low"}, got[0])
        self.assertEqual({"kind": "client_crash", "sub": "alice_a1b2c3d4_20260909-013642",
                          "level": "high"}, got[2])

    def test_the_hint_prints_the_servers_numbers(self):
        info = level_info(default="high", memory_text="7.7 GB")
        got = run([{"call": "logsLevelHint", "args": [info, "high"]},
                   {"call": "logsLevelHint", "args": [info, "low"]}])
        self.assertEqual("高：≈ 7-Zip「标准压缩」，包更小，压得慢一些；压缩时服务器约多占 345 MB 内存。"
                         "这台服务器内存 7.7 GB，低于 800 MB 时默认选「低」。", got[0])
        self.assertIn("低：≈ 7-Zip「快速压缩」，压得快，包大一些；压缩时服务器约多占 12 MB 内存。", got[1])

    def test_the_hint_when_the_server_could_not_tell_its_memory(self):
        info = level_info(default="high", memory_text="")
        (got,) = run([{"call": "logsLevelHint", "args": [info, "low"]}])
        self.assertTrue(got.endswith("查不到这台服务器的内存大小，默认选「高」。"), got)


if __name__ == "__main__":
    unittest.main()
