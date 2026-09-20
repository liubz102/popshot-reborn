#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""★ 自定义属性弹窗的**前台范围校验**（X_Mod · X4，用户 2026-09-19）。

用户要的四条：范围写到管理页里 / 前台也 check / 超范围标出那一格并给提示 /
保存按钮锁定。判据全在 `admin.js` 的三个纯函数里：

    weaponFieldError(spec, raw)     一格 → `null` 或一句话
    weaponDescError(view, text)     说明文 → 同上
    weaponErrors()                  整窗 → `{fields, desc, count}`

这里拦的是**判据**，不是像素（红框画成什么样得人眼看）。最容易在日后被
「顺手优化掉」的几条：

1. **空 = 用参考值，不算错** —— 判成错的话，一件新武器打开就满屏红、保存锁死。
2. **范围的数只能来自服务端** `weaponcfg.FIELDS`（经 `admin_view()` 下发）。
   前端写死一份的话，两边迟早分叉，而症状是「页面放行、服务端拒收」。
3. **存量的超范围值打开就标红并锁保存**（D34）：读盘放行是为了不丢数据，
   不是为了让它能继续存回去 —— 服务端 `save_item()` 是严格校验的，
   前台放行只会让 GM 白填一屏再被拒。
4. **有错就锁保存**，而且「有改动」也得让位给「填错了」—— 否则按钮按不动、
   却没人告诉你为什么。

★ 跑法同 `test_admindirty`：`vm.runInThisContext` 把**真的** `admin.js` 当一段
  `<script>` 跑，`document` / `window` 用 Proxy 空壳接住。没装 node 的机器跳过。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server")
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import weaponcfg  # noqa: E402

ADMIN_JS = os.path.join(SERVER, "web", "admin.js")
ADMIN_CSS = os.path.join(SERVER, "web", "admin.css")
NODE = shutil.which("node")

#: 在 node 里把 `admin.js` 跑起来，摆好 `WEAPON`，问它三个函数怎么说。
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
globalThis.fetch = async () => ({json: async () => ({ok: true, logged_in: false})});
require("vm").runInThisContext(src, {filename: process.argv[2]});

const view = payload.view;
globalThis.WEAPON = {
  view: view,
  canEdit: payload.canEdit !== false,
  base: JSON.parse(JSON.stringify(payload.edit)),
  edit: JSON.parse(JSON.stringify(payload.edit)),
};
if (payload.dirty) {
  // 让 base 和 edit 不相等 —— 保存键的「有改动」那一半条件得成立，
  // 这样测出来的 disabled 才是「范围」这条在起作用。
  WEAPON.base = JSON.parse(JSON.stringify(payload.base !== undefined
                                          ? payload.base : {pve: {}, pvp: {}, desc: "x"}));
}

const byKey = {};
(view.fields || []).forEach((f) => { byKey[f.key] = f; });

const out = {
  errors: weaponErrors(),
  dirty: weaponDirty(),
  perField: {},
  descProbe: {},
};
// 逐格问一遍（按 payload.probe 给的 [key, 值] 对）
(payload.probe || []).forEach(([key, raw], i) => {
  out.perField[i] = weaponFieldError(byKey[key], raw);
});
(payload.descProbe || []).forEach((text, i) => {
  out.descProbe[i] = weaponDescError(view, text);
});
// 保存键锁不锁：照 paintWeaponDirty() 那一行的判据算
out.saveDisabled = !WEAPON || !WEAPON.canEdit || !weaponDirty() || out.errors.count > 0;
process.stdout.write(JSON.stringify(out));
"""


def _view(reference=None):
    """服务端真发下来的那份字段表（不是手抄的），省得两边分叉。

    `reference` = `{字段: 参考值}`，默认全是 `None`（= 资源包那一节没写这个键）。
    """
    reference = reference or {}
    fields = []
    for key, label, unit, _src, cast, low, high in weaponcfg.FIELDS:
        fields.append({"key": key, "label": label, "unit": unit,
                       "type": "float" if cast is float else "int",
                       "min": low, "max": high, "reference": reference.get(key),
                       "zero_note": weaponcfg.ZERO_MEANS.get(key, "")})
    return {"id": 1920001, "custom": True, "fields": fields,
            "desc_max_lines": weaponcfg.DESC_MAX_LINES,
            "desc_max_chars": weaponcfg.DESC_MAX_CHARS,
            "modes": [{"key": "pve", "label": "任务"}, {"key": "pvp", "label": "对战"}],
            "groups": [{"label": "全部", "keys": list(weaponcfg.FIELD_KEYS)}],
            "homing_rule": {"angle": "homing_angle", "range": "homing_range",
                            "message": weaponcfg.HOMING_RULE_MESSAGE}}


def ask(edit=None, probe=None, desc_probe=None, dirty=True, can_edit=True, view=None):
    if NODE is None:
        raise unittest.SkipTest("这台机器上没有 node")
    payload = {"view": view or _view(),
               "edit": edit or {"pve": {}, "pvp": {}, "desc": ""},
               "probe": probe or [], "descProbe": desc_probe or [],
               "dirty": dirty, "canEdit": can_edit}
    with tempfile.TemporaryDirectory() as tmp:
        runner = os.path.join(tmp, "runner.js")
        data = os.path.join(tmp, "payload.json")
        with open(runner, "w", encoding="utf-8") as fp:
            fp.write(RUNNER)
        with open(data, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False)
        done = subprocess.run([NODE, runner, ADMIN_JS, data], capture_output=True)
    if done.returncode != 0:
        raise AssertionError("node 跑 admin.js 没跑通：\n"
                             + done.stderr.decode("utf-8", "replace"))
    return json.loads(done.stdout.decode("utf-8"))


@unittest.skipUnless(NODE, "这台机器上没有 node，跳过弹窗范围校验的拦网")
class FieldErrorTests(unittest.TestCase):
    """`weaponFieldError()` —— 一格一格地问。"""

    def test_blank_means_use_the_reference_value_and_is_never_an_error(self):
        """★ 空不是错。判成错的话，一件没配过的武器一打开就满屏红、保存锁死。"""
        got = ask(probe=[["magazine", ""], ["magazine", None], ["damage", "  "]])
        self.assertEqual({"0": None, "1": None, "2": None}, got["perField"])

    def test_a_value_inside_the_range_is_fine(self):
        got = ask(probe=[["magazine", 1], ["magazine", 50], ["magazine", 100],
                         ["damage", 0], ["damage", 500], ["gravity", -20], ["gravity", 20]])
        self.assertEqual([None] * 7, [got["perField"][str(i)] for i in range(7)])

    def test_out_of_range_says_what_the_range_is(self):
        """提示里必须带上界下界 —— 只说「不对」等于没说。"""
        got = ask(probe=[["magazine", 101], ["magazine", 0], ["damage", 501]])
        self.assertEqual("要在 1 ~ 100 之间", got["perField"]["0"])
        self.assertEqual("要在 1 ~ 100 之间", got["perField"]["1"])
        self.assertEqual("要在 0 ~ 500 之间", got["perField"]["2"])

    def test_the_range_in_the_message_comes_from_the_server_table(self):
        """★ 前端不许自己写死范围：把服务端那张表改一个数，提示就得跟着变。"""
        view = _view()
        for f in view["fields"]:
            if f["key"] == "magazine":
                f["min"], f["max"] = 3, 7
        got = ask(view=view, probe=[["magazine", 8]])
        self.assertEqual("要在 3 ~ 7 之间", got["perField"]["0"])

    def test_non_numbers_and_non_integers_are_caught_separately(self):
        got = ask(probe=[["magazine", "abc"], ["magazine", "3.5"], ["velocity", "3.5"]])
        self.assertEqual("要填数字", got["perField"]["0"])
        self.assertEqual("要填整数", got["perField"]["1"])   # 弹匣是 int 格
        self.assertIsNone(got["perField"]["2"])              # 初速是 float 格，小数合法

    def test_every_field_rejects_one_past_its_own_upper_bound(self):
        """★ 每一格都要真的拦 —— 漏一格就是一个能填出离谱数的口子。"""
        probe = []
        keys = []
        for key, _label, _unit, _src, cast, low, high in weaponcfg.FIELDS:
            probe.append([key, high + (0.5 if cast is float else 1)])
            keys.append(key)
        got = ask(probe=probe)
        for i, key in enumerate(keys):
            self.assertIsNotNone(got["perField"][str(i)], "%s 的上界没拦住" % key)


@unittest.skipUnless(NODE, "这台机器上没有 node，跳过弹窗范围校验的拦网")
class HomingRuleTests(unittest.TestCase):
    """★★ 追踪那条**跨格**判据（X7）：转向 > 0 就必须有距离。

    和别的判据不一样的地方有两条，都容易写错：
    ① 它判的是**有效值**（填了用填的、留空用参考值），不是「填了什么」；
    ② 它报在**距离**那一格上 —— 错的是「缺了距离」，不是「填了转向」。
    服务端 `weaponcfg.homing_error()` 是同一个口径，`test_weaponcfg` 那边钉着。
    """

    def test_turning_alone_is_flagged_on_the_reach_cell(self):
        got = ask(edit={"pve": {}, "pvp": {"homing_angle": 300}, "desc": ""})
        self.assertEqual({"pvp.homing_range": weaponcfg.HOMING_RULE_MESSAGE},
                         got["errors"]["fields"])
        self.assertEqual(1, got["errors"]["count"])
        self.assertTrue(got["saveDisabled"], "缺了追踪距离，保存键该按不动")

    def test_turning_plus_reach_is_clean(self):
        got = ask(edit={"pve": {}, "pvp": {"homing_angle": 300, "homing_range": 400}, "desc": ""})
        self.assertEqual({}, got["errors"]["fields"])
        self.assertFalse(got["saveDisabled"])

    def test_a_reach_that_comes_from_the_reference_counts(self):
        """★ 卡希尔 3 号槽那两把的参考值自带 `HomingRange=220` ——
        那儿只填转向、距离留空是合法的。判在「填了什么」上就会误杀。"""
        got = ask(view=_view({"homing_angle": 30, "homing_range": 220.0}),
                  edit={"pve": {}, "pvp": {"homing_angle": 600}, "desc": ""})
        self.assertEqual({}, got["errors"]["fields"])

    def test_zeroing_the_reach_on_top_of_a_reference_is_flagged(self):
        """反过来：参考值有距离，但 GM **显式填了 0** —— 那就真的锁不上了。"""
        got = ask(view=_view({"homing_angle": 30, "homing_range": 220.0}),
                  edit={"pve": {}, "pvp": {"homing_range": 0}, "desc": ""})
        self.assertEqual(weaponcfg.HOMING_RULE_MESSAGE,
                         got["errors"]["fields"].get("pvp.homing_range"))

    def test_a_reach_without_turning_is_fine(self):
        """只填距离 = 不追踪，合法。"""
        got = ask(edit={"pve": {}, "pvp": {"homing_range": 500}, "desc": ""})
        self.assertEqual({}, got["errors"]["fields"])

    def test_zero_turning_is_fine(self):
        got = ask(edit={"pve": {"homing_angle": 0}, "pvp": {"homing_angle": 0}, "desc": ""})
        self.assertEqual({}, got["errors"]["fields"])

    def test_both_modes_are_scanned(self):
        got = ask(edit={"pve": {"homing_angle": 30}, "pvp": {"homing_angle": 30}, "desc": ""})
        self.assertEqual({"pve.homing_range": weaponcfg.HOMING_RULE_MESSAGE,
                          "pvp.homing_range": weaponcfg.HOMING_RULE_MESSAGE},
                         got["errors"]["fields"])

    def test_a_plain_range_error_still_wins_on_that_cell(self):
        """一格自己的范围先判 —— 填了 9999 的距离，该说的是「要在 0 ~ 1000 之间」。"""
        got = ask(edit={"pve": {}, "pvp": {"homing_angle": 30, "homing_range": 9999}, "desc": ""})
        self.assertIn("之间", got["errors"]["fields"]["pvp.homing_range"])


@unittest.skipUnless(NODE, "这台机器上没有 node，跳过弹窗范围校验的拦网")
class SaveLockTests(unittest.TestCase):
    """`weaponErrors()` + 保存键。"""

    def test_a_clean_form_does_not_lock_the_button(self):
        got = ask(edit={"pve": {}, "pvp": {"magazine": 20}, "desc": ""})
        self.assertEqual(0, got["errors"]["count"])
        self.assertFalse(got["saveDisabled"])

    def test_one_bad_cell_locks_the_button(self):
        got = ask(edit={"pve": {}, "pvp": {"magazine": 101}, "desc": ""})
        self.assertEqual(1, got["errors"]["count"])
        self.assertEqual("要在 1 ~ 100 之间", got["errors"]["fields"]["pvp.magazine"])
        self.assertTrue(got["saveDisabled"])

    def test_both_modes_are_scanned_not_just_the_visible_one(self):
        """两栏都要扫 —— 只扫 PVP 的话，PVE 那栏填错了会一路走到服务端才被拒。"""
        got = ask(edit={"pve": {"damage": 9999}, "pvp": {"magazine": 101}, "desc": ""})
        self.assertEqual(2, got["errors"]["count"])
        self.assertIn("pve.damage", got["errors"]["fields"])
        self.assertIn("pvp.magazine", got["errors"]["fields"])

    def test_a_stored_legacy_value_is_flagged_the_moment_the_dialog_opens(self):
        """★ D34：读盘放行是为了不丢数据，不是为了让它能原样存回去。

        收紧范围之前存下的 `magazine=500` 照旧读得出来、看得见，但打开弹窗就
        标红、保存锁住 —— 和服务端 `save_item()` 的严格校验一致。
        """
        got = ask(edit={"pve": {}, "pvp": {"magazine": 500}, "desc": ""})
        self.assertEqual(1, got["errors"]["count"])
        self.assertTrue(got["saveDisabled"])
        # 而服务端读盘那条路确实还认它（两件事分得开）
        table = weaponcfg.validate({"custom": {"1920001": {"pvp": {"magazine": 500}}}})
        self.assertEqual(500, table["custom"]["1920001"]["pvp"]["magazine"])

    def test_an_overlong_description_also_locks_the_button(self):
        got = ask(edit={"pve": {}, "pvp": {}, "desc": "字" * (weaponcfg.DESC_MAX_CHARS + 1)})
        self.assertEqual(1, got["errors"]["count"])
        self.assertIsNotNone(got["errors"]["desc"])
        self.assertTrue(got["saveDisabled"])

    def test_too_many_description_lines_are_caught(self):
        got = ask(desc_probe=["a\nb\nc\nd", "a\nb\nc", ""])
        self.assertIsNotNone(got["descProbe"]["0"])
        self.assertIsNone(got["descProbe"]["1"])
        self.assertIsNone(got["descProbe"]["2"])

    def test_a_read_only_visitor_never_gets_an_enabled_button(self):
        got = ask(edit={"pve": {}, "pvp": {"magazine": 20}, "desc": ""}, can_edit=False)
        self.assertTrue(got["saveDisabled"])

    def test_no_changes_means_no_save_either(self):
        got = ask(edit={"pve": {}, "pvp": {"magazine": 20}, "desc": ""}, dirty=False)
        self.assertFalse(got["dirty"])
        self.assertTrue(got["saveDisabled"])


class SourceGuardTests(unittest.TestCase):
    """不跑 node 也能拦住的：前端有没有把范围写死。"""

    def test_the_page_does_not_hardcode_any_limit(self):
        """★ 范围只许从 `spec.min` / `spec.max` 来。前端写死一份 = 两边迟早分叉，
        而症状是「页面放行、服务端拒收」，GM 只会觉得保存坏了。"""
        with open(ADMIN_JS, "r", encoding="utf-8") as fp:
            src = fp.read()
        start = src.index("function weaponFieldError")
        chunk = src[start:src.index("function paintWeaponDirty")]
        self.assertIn("spec.min", chunk)
        self.assertIn("spec.max", chunk)
        for key, _l, _u, _s, _c, low, high in weaponcfg.FIELDS:
            self.assertNotIn(str(high), chunk, "%s 的上限 %s 被写死在 admin.js 里" % (key, high))

    def test_the_range_hint_and_error_row_have_styles(self):
        with open(ADMIN_CSS, "r", encoding="utf-8") as fp:
            css = fp.read()
        for sel in (".weapon-field .lim", ".weapon-field .err",
                    ".weapon-field.has-err .err", "textarea.bad"):
            self.assertIn(sel, css, "%s 没有样式，红框 / 范围提示画不出来" % sel)

    def test_the_cell_is_a_fixed_grid_so_the_inputs_line_up(self):
        """★ 用户 2026-09-19：输入框要上下对齐。

        靠的是「定宽网格 + 每格的孩子数一样」。换回 `flex` 或让某一列变成 `auto`，
        标签一长一短输入框就参差不齐 —— 而那是看得见、却没有任何测试会红的回归。
        """
        with open(ADMIN_CSS, "r", encoding="utf-8") as fp:
            css = fp.read()
        cell = css[css.index(".weapon-field {"):]
        cell = cell[:cell.index("}")]
        self.assertIn("display: grid", cell, "这一格不是 grid，输入框对不齐")
        cols = re.search(r"grid-template-columns:([^;]+);", cell)
        self.assertIsNotNone(cols, "没有写死列宽，输入框对不齐")
        # `minmax(0, 1fr)` 里那个空格不是列的分隔符，先折成一个 token 再数
        widths = re.sub(r"\w+\([^)]*\)", "FN", cols.group(1)).split()
        self.assertEqual(5, len(widths), "列数不是 5（项目名/范围/输入框/单位/参考）：%r" % widths)
        # 输入框那一列必须是定值（px），否则它的左边缘会随内容浮动
        self.assertRegex(widths[2], r"^\d+px$", "输入框那列不是定宽：%r" % widths[2])

    def test_one_row_per_field_so_nothing_shifts_sideways(self):
        """一栏排两列的话，「项目名 + 范围 + 输入框 + 单位 + 参考」放不下会折行，
        折行就谈不上对齐 —— 所以这里钉死单列。"""
        with open(ADMIN_CSS, "r", encoding="utf-8") as fp:
            css = fp.read()
        rows = re.search(r"\.weapon-group \.rows \{([^}]*)\}", css)
        self.assertIsNotNone(rows)
        self.assertIn("minmax(0, 1fr)", rows.group(1))
        self.assertNotIn("auto-fill", rows.group(1), "又排成多列了，输入框会错位")

    def test_the_error_line_sits_under_the_input_and_never_wraps(self):
        """用户 2026-09-19：错误信息写在输入框下面一行，**不要换行**。

        折行会把那一格撑高、把同一行别的格子顶歪，所以 `nowrap` 是必需的；
        `grid-column` 决定它从输入框那一列起头（而不是从项目名那儿）。
        """
        with open(ADMIN_CSS, "r", encoding="utf-8") as fp:
            css = fp.read()
        err = re.search(r"\.weapon-field \.err \{([^}]*)\}", css)
        self.assertIsNotNone(err)
        body = err.group(1)
        self.assertIn("white-space: nowrap", body, "错误信息会折行")
        self.assertIn("grid-column: 3", body, "错误信息没有对齐到输入框那一列")
        self.assertIn("display: none", body, "平时就该不占位置")

    def test_the_two_mode_blocks_read_pve_first_everywhere(self):
        """★ 用户 2026-09-19 第三轮：设置那两栏是「PVE 左 / PVP 右」，
        底下的预览却「PVP 上 / PVE 下」，一处左右一处上下反着来，看着别扭。

        两栏是硬编码的（中间还夹着「复制到另一侧」那一列，不好循环），预览区
        改成照 `view.modes` 画 —— 那就是服务端的 `weaponcfg.MODES`，一处定序三处跟。
        """
        with open(ADMIN_JS, "r", encoding="utf-8") as fp:
            src = fp.read()
        body = src[src.index("function renderWeaponModal"):]
        body = body[:body.index("async function openWeaponModal")]
        self.assertLess(body.index('weaponModeNode("pve")'), body.index('weaponModeNode("pvp")'),
                        "设置那两栏 PVP 跑到 PVE 前面了")
        preview = body[body.index("var preview ="):]
        self.assertNotIn('["pvp", "pve"]', preview, "预览区又把顺序写死成 PVP 在前了")
        self.assertNotIn('["pve", "pvp"]', preview,
                         "预览区不该写死顺序 —— 照 view.modes 画，跟服务端 MODES 走")
        self.assertIn("(view.modes || []).forEach", preview)

    def test_the_hint_line_comes_from_the_server_not_from_the_page(self):
        """★ 「0 代表不追踪」那句灰字的**话在服务端**（`weaponcfg.ZERO_MEANS`）。

        前端写死一份的话，改文案要改两个地方，而漏掉的那一处不会报错。
        """
        with open(ADMIN_JS, "r", encoding="utf-8") as fp:
            src = fp.read()
        body = src[src.index("function weaponFieldNode"):src.index("function weaponModeNode")]
        self.assertIn("spec.zero_note", body, "提示那一行没有用服务端发下来的话")
        for text in weaponcfg.ZERO_MEANS.values():
            self.assertNotIn(text, src, "admin.js 里写死了「%s」，该由服务端发" % text)
        self.assertNotIn(weaponcfg.HOMING_RULE_MESSAGE, src,
                         "跨格判据那句话也写死在页面里了")

    def test_changing_one_cell_repaints_the_whole_window(self):
        """★★ 有了跨格判据（转向 ↔ 距离），只重画自己那一格就会漏 ——
        改「转向」时「距离」那一格的红框得跟着出现 / 消失。"""
        with open(ADMIN_JS, "r", encoding="utf-8") as fp:
            src = fp.read()
        body = src[src.index("function weaponFieldNode"):src.index("function weaponModeNode")]
        self.assertIn("paintWeaponFields();", body, "输入回调还是只重画自己那一格")
        self.assertIn("WEAPON_PAINTERS.push(paintField)", body)
        self.assertIn("WEAPON_PAINTERS = [];",
                      src[src.index("function renderWeaponModal"):],
                      "重建 DOM 时没清空重画表，旧函数会指着丢掉的节点")

    def test_every_cell_has_all_six_children_even_without_a_unit(self):
        """★ 定宽网格靠「孩子数一样」对齐：没单位的字段也得占住单位那一格。

        写成 `if (spec.unit)` 的话，伤害那几行的「参考 N」会整体左移一列 ——
        这正是改成网格之前的写法，别改回去。
        """
        with open(ADMIN_JS, "r", encoding="utf-8") as fp:
            src = fp.read()
        body = src[src.index("function weaponFieldNode"):src.index("function weaponModeNode")]
        self.assertNotIn('if (spec.unit)', body, "单位那一格又变成有条件的了，网格会错位")
        self.assertIn('el("span", "unit", spec.unit || "")', body)
        # 顺序：项目名 → 范围 → 输入框 → 单位 → 参考 → 错误行 → 「0 代表…」那行
        # ★ 最后两个都是 `grid-column: 3 / -1` 的整行，**不占定宽那几列**，
        #   所以它们不影响「输入框上下对齐」这件事（X7 加 hint 时确认过）。
        order = [a or b for a, b in
                 re.findall(r'appendChild\(el\("(?:span|div)", "(\w+)"|appendChild\((\w+)\)', body)]
        self.assertEqual(["lab", "lim", "input", "unit", "ref", "err", "hint"], order,
                         "这一格的排列顺序变了：%r" % (order,))


if __name__ == "__main__":
    unittest.main()
