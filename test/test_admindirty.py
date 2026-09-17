#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""★ 管理页「哪一条改过了」的判据（V0.3商店，用户 2026-09-14）。

工具条上那句「有未保存的修改」只说了「有」，不说「在哪」⇒ `admin.js` 给每条
记录留一份基线，改过的那一行整行换个边框（`.edited`）。判据本身全在三个
函数里：`markBase()` / `entryEdited()` / `editCounts()`。

拦的是**判据**，不是像素 —— 画成什么样看 `admin.css`，那个得人眼看。
四条最容易在日后被「顺手优化掉」的：

1. **键序换了不算改过**（`stableJson` 的存在理由）。有人把它换成
   `JSON.stringify` 的话这里当场红 —— 否则症状是「什么都没改，半页都亮着」。
2. **基线按记录对象存、不按下标**。按下标的话删掉第 3 条，后面每一条都会
   被判成「改过了」—— 而画面上看着就是「删一条亮一片」。
3. **可选字段留空 = 删掉那个键**（`fieldNode` 那一支），也得算改过。
4. **改回原值 = 没改过**，不留「一改就永远亮着」的余味。

★ 跑法同 `test_cardtext`：`vm.runInThisContext` 把**真的** `admin.js` 当一段
  `<script>` 跑，`document` / `window` 用 Proxy 空壳接住。`admin.js` 一个字
  都不用改，量的才是真的那份代码。没装 node 的机器跳过。
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

#: 在 node 里把 `admin.js` 跑起来，按 `payload.steps` 一步步动那份模型，
#: 每一步回一份「谁亮着 / 改增删各几条」。
#:
#: `step` 的形状：
#:   {"set": [下标, 键, 值]}      改一格（值为 null = 删掉这个键）
#:   {"reorder": 下标}            键序倒过来重排一遍，**值一个都不动**
#:   {"push": {…}}                新加一条
#:   {"drop": 下标}               删掉一条
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

const which = "shop";
globalThis.CFG[which] = {entries: payload.entries};
markBase(which);

function snap() {
  return {
    edited: CFG[which].entries.map((e) => (entryEdited(e) ? 1 : 0)),
    counts: editCounts(which),
  };
}

const out = [snap()];
payload.steps.forEach((step) => {
  const rows = CFG[which].entries;
  if (step.set) {
    const [at, key, value] = step.set;
    if (value === null) { delete rows[at][key]; } else { rows[at][key] = value; }
  } else if (step.reorder !== undefined) {
    // 同一个对象、同一批值，只把键**倒着**重写一遍 ⇒ `JSON.stringify` 的
    // 结果会变，`stableJson` 的不会。
    const row = rows[step.reorder];
    const keys = Object.keys(row).reverse();
    const copy = {};
    keys.forEach((k) => { copy[k] = row[k]; });
    keys.forEach((k) => { delete row[k]; });
    keys.forEach((k) => { row[k] = copy[k]; });
  } else if (step.push) {
    rows.push(step.push);
  } else if (step.drop !== undefined) {
    rows.splice(step.drop, 1);
  }
  out.push(snap());
});
process.stdout.write(JSON.stringify(out));
"""

#: 三条商店记录，够用了（判据和条数无关）。
ENTRIES = [
    {"id": 1010001, "name": "甲", "kind": "equip", "price": 100, "listed": True},
    {"id": 1010002, "name": "乙", "kind": "equip", "price": 200, "listed": True},
    {"id": 1010003, "name": "丙", "kind": "equip", "price": 300, "listed": False},
]


def run_steps(steps, entries=None):
    """让 `admin.js` 按这些步骤动一遍模型，回每一步的快照。"""
    with tempfile.TemporaryDirectory() as tmp:
        runner = os.path.join(tmp, "runner.js")
        data = os.path.join(tmp, "payload.json")
        with open(runner, "w", encoding="utf-8") as fp:
            fp.write(RUNNER)
        with open(data, "w", encoding="utf-8") as fp:
            json.dump({"entries": entries or ENTRIES, "steps": steps}, fp,
                      ensure_ascii=False)
        done = subprocess.run([NODE, runner, ADMIN_JS, data],
                              capture_output=True)
    if done.returncode != 0:
        raise AssertionError("node 跑 admin.js 没跑通：\n"
                             + done.stderr.decode("utf-8", "replace"))
    return json.loads(done.stdout.decode("utf-8"))


@unittest.skipUnless(NODE, "这台机器上没有 node，跳过管理页脏标记的拦网")
class EditedRowTests(unittest.TestCase):

    def test_a_freshly_loaded_page_marks_nothing(self):
        """一进页面一条都不该亮 —— 亮一片的话这个标记就等于没有。"""
        first = run_steps([])[0]
        self.assertEqual([0, 0, 0], first["edited"])
        self.assertEqual({"changed": 0, "added": 0, "removed": 0},
                         first["counts"])

    def test_only_the_row_you_touched_lights_up(self):
        after = run_steps([{"set": [1, "price", 250]}])[1]
        self.assertEqual([0, 1, 0], after["edited"])
        self.assertEqual(1, after["counts"]["changed"])

    def test_changing_it_back_puts_the_light_out(self):
        """改回原值 = 没改过。不然「一碰就永远亮着」，标记很快没人信。"""
        steps = [{"set": [1, "price", 250]}, {"set": [1, "price", 200]}]
        self.assertEqual([0, 1, 0], run_steps(steps)[1]["edited"])
        self.assertEqual([0, 0, 0], run_steps(steps)[2]["edited"])

    def test_reordering_the_keys_is_not_a_change(self):
        """★ 键序变了不算改过（`stableJson` 就是为这个存在的）。

        改一格常常是「删掉一个可选键再加回来」，键序一变 `JSON.stringify`
        的结果就不一样了 —— 拿它当判据的话，动一格能亮一片。
        """
        after = run_steps([{"reorder": 0}])[1]
        self.assertEqual([0, 0, 0], after["edited"])
        self.assertEqual(0, after["counts"]["changed"])

    def test_clearing_an_optional_field_counts_as_a_change(self):
        """可选字段留空 = 那个键干脆不写（`fieldNode`），一样算改过。"""
        after = run_steps([{"set": [2, "name", None]}])[1]
        self.assertEqual([0, 0, 1], after["edited"])
        self.assertEqual(1, after["counts"]["changed"])

    def test_a_new_row_lights_up_and_counts_as_added(self):
        after = run_steps([{"push": {"id": 1010009, "price": 1}}])[1]
        self.assertEqual([0, 0, 0, 1], after["edited"])
        self.assertEqual(1, after["counts"]["added"])
        self.assertEqual(0, after["counts"]["changed"])

    def test_deleting_a_row_does_not_light_up_the_ones_after_it(self):
        """★ 基线按**记录对象**存，不按下标。

        按下标存的话删掉第 1 条会让后面每一条都对不上基线 —— 画面上就是
        「删一条亮一片」，而那些行根本没人动过。
        """
        after = run_steps([{"drop": 0}])[1]
        self.assertEqual([0, 0], after["edited"])
        self.assertEqual({"changed": 0, "added": 0, "removed": 1},
                         after["counts"])

    def test_delete_and_edit_are_counted_separately(self):
        """删掉的那条在画面上没有行可标 ⇒ 只能靠 `removed` 这个数说话。"""
        after = run_steps([{"drop": 0}, {"set": [0, "price", 999]},
                           {"push": {"id": 1010009}}])[3]
        self.assertEqual([1, 0, 1], after["edited"])
        self.assertEqual({"changed": 1, "added": 1, "removed": 1},
                         after["counts"])


if __name__ == "__main__":
    unittest.main()
