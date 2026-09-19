#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""★ 管理页的「来源」筛选 + 「选择物品」弹窗的多选（X_Mod · X7，用户 2026-09-20）。

用户要的两件事：① 物品库 / 货架 / 弹窗等处能一键筛出「自定义物品」；
② 三页顶部的「添加」和合成配方的材料格改成一次勾多件。判据全在这几个**纯函数**里：

    sourceMatches(itemId, want)          来源三档
    dropdownsMatch(itemId, want)         角色 × 上架状态 × 来源，三维不互吃
    pickerCanChoose / pickerToggle       多选的上限
    mergeMaterials(current, chosenIds)   整组材料编辑 → 新的材料数组
    makeEntry(which, item) / nextRecipeId()   批量添加造出来的那些条目

★★ **为什么非得是纯函数**：下面那层 Proxy 空壳把 `cell.onclick = fn` 整个吞掉
（`set() { return true; }`），点格子这件事在 node 里根本模拟不了。判据只要写回
onclick 里，这一整份用例就全废了 —— 改的人不会收到任何提示。

这里拦的是**判据**，不是像素（红框 / 灰角标画成什么样得人眼看）。
最容易在日后被「顺手统一掉」的五条：

1. **`sourceMatches` 读 `BYID[].custom`，不读 `itemRuleOf`。** 后者是「物品库这一页
   管理员能改的那几栏」（中文名 / 等级 / 角色限定），`custom` 压根不在里面 ——
   挪过去只会永远拿到 `undefined`，症状是「选了自定义物品，一件都筛不出来」。
   ★ 这和隔壁「角色」那一栏的 D31 口径**正好相反**，所以特别容易被统一。
2. **`mergeMaterials` 必须保序 + 保数量。** `Object.keys(PICKER.chosen)` 对数字型键
   返回的是**升序**不是点击序，照它重排的话，GM 只是想加一种材料，另外三种的
   位置和已经填好的数量全被洗一遍。
3. **取消勾选 = 移除**（用户 2026-09-20 拍板）—— 这是整组编辑器和「补充器」的
   分水岭，也是「换一种材料」能走这条路的前提。
4. **`makeEntry` 每次 new 一个对象。** 共用一个对象的话，脏标记那张 WeakMap 只有
   一个键、`data-index` 全指向同一条、改一格改一片，**而且一句报错都没有**。
5. **`nextRecipeId` 一条算一次。** 先造完再统一 push 的话 N 条同号，保存时
   「配方号 N 出现了两次」，整份文件都进不去。

★ 跑法同 `test_admindirty`：`vm.runInThisContext` 把**真的** `admin.js` 当一段
  `<script>` 跑，`document` / `window` 用 Proxy 空壳接住。`admin.js` 一个字都不用改，
  量的才是真的那份代码。没装 node 的机器跳过。
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

#: 在 node 里把 `admin.js` 跑起来，摆好全局状态，然后逐条执行 `payload.probes`。
#:
#: `probe` 的形状：`{"call": "函数名", "args": [...]}` —— 结果原样收进数组。
#: 另外认两个特殊的：
#:   {"openPicker": {…}}   开一次选择器（options 原样传过去）
#:   {"pickerState": true} 回一份 `{chosen: [...], footDisabled, foot}`
RUNNER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const payload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

// 记下这几个节点被写了什么 —— 底栏那句话是用例要看的东西之一。
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
                 querySelectorAll() { return []; },
                 getBoundingClientRect() { return {bottom: 0}; },
                 focus() {}};
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

// `$` 换成我们这份能读回来的假节点；`el()` 之类照旧走 sink。
// ★ 只能在 `runInThisContext` **之后**换 —— `admin.js` 里的 `function $()`
//   是顶层函数声明，先换会被它盖掉。
globalThis.$ = fakeNode;
// 文件末尾那个 IIFE 会 `await` 一发假 fetch，然后在**微任务**里画整页外壳
// （`showLoggedOut` → `paintTabChrome` → `$("tabs").children`）。那一整段和
// 这份用例要量的判据没有半点关系，而它抛出来的异常会让 node 以非零码退出
// ⇒ 直接换成空操作，别去给页面外壳伪造 DOM。
globalThis.showLoggedOut = function () {};
globalThis.showLoggedIn = function () {};
// 画网格那一发要真 DOM，用例不碰它 —— 换成空操作，`openPicker` 才跑得完。
globalThis.paintPicker = function () { paintPickFoot(); };
globalThis.fillSelect = function () { return sink; };
globalThis.toast = function () { return null; };

globalThis.CAT = payload.CAT;
globalThis.BYID = {};
payload.CAT.items.forEach((item) => { BYID[item.id] = item; });
globalThis.CFG = payload.CFG;
// 中文名：用例只关心「有没有被列出来」，物品表里那份就够。
globalThis.itemName = (id) => (BYID[id] && BYID[id].name) || String(id);
// 「角色限定 / 中文名」那一份（物品库的页面模型）。`custom` **故意不在里面**
// —— 这样 `sourceMatches` 一旦被挪去读它，用例当场红。
globalThis.itemRuleOf = (id) => {
  const found = (CFG.items.entries || []).filter((e) => Number(e.id) === Number(id));
  return found[0] || {};
};

const out = [];
payload.probes.forEach((probe) => {
  if (probe.openPicker) {
    openPicker(probe.openPicker);
    out.push(null);
  } else if (probe.pickerState) {
    // 真页面上每翻一格就重画一次底栏（`paintPicker` 的 onclick 末尾），
    // 这儿照做 —— 否则量到的永远是开窗那一刻那句话。
    paintPickFoot();
    out.push({
      chosen: Object.keys(PICKER.chosen).map(Number),
      preset: PICKER.preset,
      max: PICKER.max,
      ownedLabel: PICKER.ownedLabel,
      foot: nodes.pickChosen ? nodes.pickChosen.textContent : "",
      footDisabled: nodes.pickConfirm ? nodes.pickConfirm.disabled : null,
    });
  } else if (probe.global) {
    out.push(globalThis[probe.global]);
  } else if (probe.entries) {
    out.push(CFG[probe.entries].entries);
  } else if (probe.distinct) {
    // 批量添加出来的那几条**两两不是同一个对象**吗
    const rows = CFG[probe.distinct].entries;
    let same = false;
    for (let a = 0; a < rows.length; a += 1) {
      for (let b = a + 1; b < rows.length; b += 1) {
        if (rows[a] === rows[b]) { same = true; }
      }
    }
    out.push(!same);
  } else {
    out.push(globalThis[probe.call](...probe.args));
  }
});
process.stdout.write(JSON.stringify(out));
"""

#: 一张够用的物品表。★ `1920001` 是自定义武器（`custom`），别的都是原版。
ITEMS = [
    {"id": 1010001, "kind": "armor", "name": "泰尔的帽子", "character": 0},
    {"id": 1120011, "kind": "weapon", "name": "左轮手枪", "character": 0},
    {"id": 2120011, "kind": "weapon", "name": "卡希尔的枪", "character": 1},
    {"id": 1920001, "kind": "weapon", "name": "左轮手枪 自定义1",
     "character": 0, "custom": True},
    {"id": 10003, "kind": "material", "name": "黑色小珠"},
    {"id": 10004, "kind": "material", "name": "白色小珠"},
    {"id": 30019, "kind": "material", "name": "青铜管"},
    {"id": 40011, "kind": "material", "name": "龙之泪"},
    {"id": 40012, "kind": "material", "name": "凤之羽"},
]

#: 字段表只留这次用得到的三页，形状照 `shopcfg.SCHEMA`。
SCHEMA = {
    "shop": {"title": "商店货架", "fields": [
        {"key": "id", "label": "物品", "type": "item"},
        {"key": "kind", "label": "类别", "type": "text", "readonly": True},
        {"key": "price", "label": "价格", "type": "int", "min": 0},
        {"key": "listed", "label": "上架", "type": "bool"}]},
    "recipe": {"title": "合成配方", "fields": [
        {"key": "id", "label": "配方号", "type": "int", "min": 1,
         "readonly": True},
        {"key": "result", "label": "产物", "type": "item"},
        {"key": "materials", "label": "材料", "type": "materials", "max": 4,
         "fields": [{"key": "id", "type": "item", "kinds": ["material"]},
                    {"key": "count", "type": "int", "min": 1, "max": 800}]},
        {"key": "cost", "label": "花费", "type": "int", "min": 0},
        {"key": "listed", "label": "上架", "type": "bool"}]},
    "drops": {"title": "材料掉落", "fields": [
        {"key": "mode", "label": "模式", "type": "choice",
         "options": [{"value": "quest", "label": "闯关"},
                     {"value": "pvp", "label": "对战"}]},
        {"key": "stage", "label": "关卡", "type": "choice", "optional": True},
        {"key": "material", "label": "材料", "type": "item",
         "kinds": ["material"]},
        {"key": "count", "label": "数量", "type": "int", "min": 1, "max": 800},
        {"key": "prob", "label": "概率", "type": "int", "min": 0, "max": 100},
        {"key": "cleared_only", "label": "只有通关才给", "type": "bool"}]},
}


def _run(probes, cfg=None):
    """把 `probes` 丢进 node 里跑一遍，回一份结果数组。"""
    payload = {
        "CAT": {"items": ITEMS, "schema": SCHEMA, "max_materials": 4,
                "characters": {"0": "泰尔", "1": "卡希尔", "2": "布洛克"},
                "kinds": {}, "series": {}},
        "CFG": cfg or {"items": {"entries": []},
                       "shop": {"entries": []},
                       "recipe": {"entries": []},
                       "drops": {"entries": []}},
        "probes": probes,
    }
    with tempfile.TemporaryDirectory() as box:
        runner = os.path.join(box, "runner.js")
        data = os.path.join(box, "payload.json")
        with open(runner, "w", encoding="utf-8") as fp:
            fp.write(RUNNER)
        with open(data, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False)
        done = subprocess.run([NODE, runner, ADMIN_JS, data],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if done.returncode != 0:
        raise AssertionError("node 跑挂了：\n" + done.stderr.decode("utf-8",
                                                                   "replace"))
    return json.loads(done.stdout.decode("utf-8"))


@unittest.skipUnless(NODE, "这台机器没装 node，跳过前台判据用例")
class SourceFilterTests(unittest.TestCase):
    """「来源」三档：全部 / 原版 / 自定义（用户 2026-09-20）。"""

    def test_empty_lets_everything_through(self):
        # 下拉第一项「全部物品」= 不筛。★ 这一档还负责「一次 BYID 都不查」，
        # 弹窗里每敲一个字都要重画，别让它去扫全表。
        got = _run([{"call": "sourceMatches", "args": [1120011, ""]},
                    {"call": "sourceMatches", "args": [1920001, ""]},
                    {"call": "sourceMatches", "args": [999999, ""]}])
        self.assertEqual([True, True, True], got)

    def test_custom_only_matches_the_ones_we_added(self):
        got = _run([{"call": "sourceMatches", "args": [1920001, "custom"]},
                    {"call": "sourceMatches", "args": [1120011, "custom"]},
                    {"call": "sourceMatches", "args": [1010001, "custom"]}])
        self.assertEqual([True, False, False], got)

    def test_stock_is_exactly_the_complement(self):
        got = _run([{"call": "sourceMatches", "args": [1920001, "stock"]},
                    {"call": "sourceMatches", "args": [1120011, "stock"]},
                    {"call": "sourceMatches", "args": [1010001, "stock"]}])
        self.assertEqual([False, True, True], got)

    def test_an_id_the_table_does_not_know_counts_as_stock(self):
        """★ 物品表里没有的 id 算**原版** —— 「不是我加的」对它成立。
        判成自定义的话，一筛「自定义物品」就会冒出一堆查无此物的空格子。"""
        got = _run([{"call": "sourceMatches", "args": [999999, "custom"]},
                    {"call": "sourceMatches", "args": [999999, "stock"]}])
        self.assertEqual([False, True], got)

    def test_the_dropdown_offers_exactly_the_two_the_user_asked_for(self):
        """用户要的三档是「全部物品 / 原版物品 / 自定义物品」，而第一档
        **是 `fillSelect` 自己加的那一项**（空值 = 不筛）⇒ 选项表里只有两条。
        ★ 多写一条「全部」的话会出现一个非空的「全部」值，`sourceMatches`
        认不得它，症状是「选了全部，反而一件都没有」。"""
        got = _run([{"global": "SOURCE_FILTER_OPTIONS"}])
        self.assertEqual([{"value": "stock", "label": "原版物品"},
                          {"value": "custom", "label": "自定义物品"}], got[0])


@unittest.skipUnless(NODE, "这台机器没装 node，跳过前台判据用例")
class DropdownsMatchTests(unittest.TestCase):
    """角色 × 上架状态 × 来源，三维**互不相吃**（D68 + 用户 2026-09-20）。"""

    CFG = {
        "items": {"entries": [{"id": 1920001, "character": 0},
                              {"id": 1120011, "character": 0},
                              {"id": 2120011, "character": 1}]},
        "shop": {"entries": [{"id": 1920001, "listed": True, "price": 100}]},
        "recipe": {"entries": []},
        "drops": {"entries": []},
    }

    def test_each_axis_can_reject_on_its_own(self):
        got = _run([
            # 三维全空 = 谁都过
            {"call": "dropdownsMatch", "args": [1920001, {}]},
            # 只有角色对不上
            {"call": "dropdownsMatch",
             "args": [1920001, {"character": "1", "listing": "", "source": ""}]},
            # 只有上架状态对不上（它上架在商店，却要「未上架」）
            {"call": "dropdownsMatch",
             "args": [1920001, {"character": "", "listing": "none",
                                "source": ""}]},
            # 只有来源对不上
            {"call": "dropdownsMatch",
             "args": [1920001, {"character": "", "listing": "",
                                "source": "stock"}]},
        ], cfg=self.CFG)
        self.assertEqual([True, False, False, False], got)

    def test_all_three_together_still_let_the_right_one_through(self):
        got = _run([
            {"call": "dropdownsMatch",
             "args": [1920001, {"character": "0", "listing": "shop",
                                "source": "custom"}]},
            # 同一套条件，换成原版那把 —— 只有来源这一维把它挡下
            {"call": "dropdownsMatch",
             "args": [1120011, {"character": "0", "listing": "none",
                                "source": "custom"}]},
            {"call": "dropdownsMatch",
             "args": [1120011, {"character": "0", "listing": "none",
                                "source": "stock"}]},
        ], cfg=self.CFG)
        self.assertEqual([True, False, True], got)

    def test_source_does_not_read_the_page_model(self):
        """★★ `sourceMatches` 必须读**原版数据** `BYID[].custom`。

        这份 `CFG.items` 里一条 `custom` 都没有（真实的 `items.json` 也没有，
        那一页只存中文名 / 等级 / 角色限定）—— 判据一旦被挪进 `itemRuleOf`，
        自定义那把就再也筛不出来，这条当场红。
        """
        got = _run([{"call": "dropdownsMatch",
                     "args": [1920001, {"character": "", "listing": "",
                                        "source": "custom"}]}],
                   cfg=self.CFG)
        self.assertEqual([True], got)


@unittest.skipUnless(NODE, "这台机器没装 node，跳过前台判据用例")
class PickerLimitTests(unittest.TestCase):
    """多选的上限：合成配方最多 4 种材料（用户 2026-09-20）。"""

    def test_it_stops_at_the_limit_and_says_so_in_the_foot(self):
        got = _run([
            {"openPicker": {"multi": True, "max": 4,
                            "chosen": [10003, 10004, 30019]}},
            {"pickerState": True},
            {"call": "pickerToggle", "args": [40011]},     # 第 4 种，收
            {"call": "pickerToggle", "args": [40012]},     # 第 5 种，拒
            {"pickerState": True},
        ])
        state0, ok4, ok5, state1 = got[1], got[2], got[3], got[4]
        self.assertEqual([10003, 10004, 30019], sorted(state0["chosen"]))
        self.assertTrue(ok4)
        self.assertFalse(ok5, "到 4 种还能再勾 —— 上限没拦住")
        self.assertEqual([10003, 10004, 30019, 40011],
                         sorted(state1["chosen"]))

    def test_unchecking_frees_a_slot_again(self):
        """★ 取消勾选**永远允许**，否则满 4 格之后整个弹窗就动不了了
        —— 而「换一种材料」正是靠「取消旧的 + 勾上新的」走通的。"""
        got = _run([
            {"openPicker": {"multi": True, "max": 4,
                            "chosen": [10003, 10004, 30019, 40011]}},
            {"call": "pickerToggle", "args": [40012]},     # 满了，拒
            {"call": "pickerToggle", "args": [10003]},     # 取消一个，收
            {"call": "pickerToggle", "args": [40012]},     # 现在能勾了
            {"pickerState": True},
        ])
        self.assertEqual([False, True, True], got[1:4])
        self.assertEqual([10004, 30019, 40011, 40012], sorted(got[4]["chosen"]))

    def test_the_foot_lists_the_names_when_there_is_a_limit(self):
        """★ 网格上只画过了筛选的那些 —— 搜一个字就能把已勾的全筛没。
        底栏光写「已选 3」而一个 ✓ 都看不见的话，人不知道发生了什么。"""
        got = _run([
            {"openPicker": {"multi": True, "max": 4, "chosen": [10003, 40011]}},
            {"pickerState": True},
        ])
        foot = got[1]["foot"]
        self.assertIn("已选 2 / 上限 4 件", foot)
        self.assertIn("黑色小珠", foot)
        self.assertIn("龙之泪", foot)

    def test_without_a_limit_the_foot_is_the_old_wording(self):
        # 「修改仓库」/「发送奖励」那两处不设上限，文案一个字都不该变。
        got = _run([
            {"openPicker": {"multi": True, "owned": {}}},
            {"call": "pickerToggle", "args": [10003]},
            {"pickerState": True},
        ])
        self.assertEqual("已选 1 件", got[2]["foot"])
        self.assertIsNone(got[2]["max"])

    def test_owned_can_never_be_chosen(self):
        # 已在货架上 / 已有配方的那些画成灰角标，勾了也只会被服务端拒收。
        got = _run([
            {"openPicker": {"multi": True, "owned": {"10003": True},
                            "ownedLabel": "已上架"}},
            {"call": "pickerCanChoose", "args": [10003]},
            {"call": "pickerCanChoose", "args": [10004]},
            {"pickerState": True},
        ])
        self.assertEqual([False, True], got[1:3])
        self.assertEqual("已上架", got[3]["ownedLabel"])

    def test_a_hand_edited_over_long_recipe_cannot_be_saved_back(self):
        """★ 手改出来的 5 材料配方：第 5 种在卡片上根本看不见（只画 4 格），
        进了这个弹窗才现形。确认键**必须是灰的** —— 读盘放行是为了不丢数据，
        不是为了让它能继续存回去（同 D34 的口径）。"""
        got = _run([
            {"openPicker": {"multi": True, "max": 4,
                            "chosen": [10003, 10004, 30019, 40011, 40012]}},
            {"pickerState": True},
            {"call": "pickerToggle", "args": [40012]},     # 去掉一个
            {"pickerState": True},
        ])
        self.assertTrue(got[1]["footDisabled"], "5 种材料居然还能按确认")
        self.assertFalse(got[3]["footDisabled"])

    def test_the_clear_button_is_only_for_the_plain_batch_mode(self):
        """★ 整组编辑器里「清空」的意思不是「取消我这次的勾选」，而是
        「把这条配方的材料全删了」—— 而那件事卡片上有一颗看得见的「移除」。
        判据挂在 `preset`（给没给预勾选）上，不是挂在 `max` 上。"""
        got = _run([
            {"openPicker": {"multi": True, "max": 4, "chosen": [10003]}},
            {"pickerState": True},
            {"openPicker": {"multi": True, "owned": {}}},
            {"pickerState": True},
        ])
        self.assertTrue(got[1]["preset"])
        self.assertFalse(got[3]["preset"])


@unittest.skipUnless(NODE, "这台机器没装 node，跳过前台判据用例")
class MergeMaterialsTests(unittest.TestCase):
    """整组材料编辑 → 新的材料数组（用户 2026-09-20）。"""

    def test_it_keeps_the_order_and_the_counts(self):
        """★★ 这是这份文件里最重要的一条。`Object.keys` 对数字型键返回**升序**，
        照它重建的话，GM 只是想加一种材料，另外几种的位置和已经填好的数量
        全被洗一遍 —— 而且页面上看着「就是自己刚点的那几种」，不会觉得有问题。"""
        got = _run([{"call": "mergeMaterials",
                     "args": [[{"id": 40011, "count": 7},
                               {"id": 10003, "count": 2}],
                              [10003, 40011]]}])
        self.assertEqual([{"id": 40011, "count": 7},
                          {"id": 10003, "count": 2}], got[0])

    def test_new_ones_land_at_the_end_with_count_one(self):
        got = _run([{"call": "mergeMaterials",
                     "args": [[{"id": 40011, "count": 7}], [40011, 10003]]}])
        self.assertEqual([{"id": 40011, "count": 7},
                          {"id": 10003, "count": 1}], got[0])

    def test_unchecking_removes_it(self):
        # 用户 2026-09-20 拍板：取消勾选 = 移除这种材料。
        got = _run([{"call": "mergeMaterials",
                     "args": [[{"id": 40011, "count": 7},
                               {"id": 10003, "count": 2}],
                              [10003]]}])
        self.assertEqual([{"id": 10003, "count": 2}], got[0])

    def test_swapping_one_keeps_the_other_counts(self):
        """「换一种材料」= 取消旧的 + 勾上新的 —— 这是材料格也走多选的前提
        （用户 2026-09-20 第二轮）。剩下那几种的数量一个都不许动。"""
        got = _run([{"call": "mergeMaterials",
                     "args": [[{"id": 40011, "count": 7},
                               {"id": 10003, "count": 2},
                               {"id": 30019, "count": 5}],
                              [40011, 30019, 10004]]}])
        self.assertEqual([{"id": 40011, "count": 7},
                          {"id": 30019, "count": 5},
                          {"id": 10004, "count": 1}], got[0])

    def test_empty_in_empty_out(self):
        got = _run([{"call": "mergeMaterials", "args": [[], []]}])
        self.assertEqual([], got[0])


@unittest.skipUnless(NODE, "这台机器没装 node，跳过前台判据用例")
class BatchAddTests(unittest.TestCase):
    """「添加」一次加多条（用户 2026-09-20）。"""

    def test_it_lays_out_the_same_defaults_as_before(self):
        # 抽 `makeEntry` 是为了给批量复用 —— 铺出来的默认值一个字段都不该变。
        got = _run([
            {"call": "makeEntry", "args": ["shop", {"id": 1920001,
                                                    "kind": "weapon"}]},
            {"call": "makeEntry", "args": ["drops", {"id": 10003,
                                                     "kind": "material"}]},
        ])
        self.assertEqual({"id": 1920001, "kind": "weapon", "price": 0,
                          "listed": True}, got[0])
        # ★ `optional` 的（关卡）一律先不写；`prob` 的 min 是 0 所以默认 0%
        #   —— 这是既有行为，改它要另提一条需求。
        self.assertEqual({"mode": "quest", "material": 10003, "count": 1,
                          "prob": 0, "cleared_only": True}, got[1])

    def test_every_row_is_its_own_object(self):
        """★★ 共用一个对象的话：脏标记那张 WeakMap 只有一个键、`data-index`
        全指向同一条、改一格改一片、删一条删错行 —— **一句报错都没有**。

        跨进程比不了引用，所以在 node 里当场两两 `===` 一遍（`distinct` 探针），
        再回一个布尔。**连同一件物品加两次也必须是两个对象。**
        """
        got = _run([
            {"call": "eval", "args": [
                "[1010001, 1010001, 1120011].forEach(function (id) {"
                "  CFG.shop.entries.push(makeEntry('shop',"
                "    {id: id, kind: 'armor'})); })"]},
            {"distinct": "shop"},
            {"entries": "shop"},
        ])
        self.assertTrue(got[1], "批量加出来的几条是同一个对象")
        self.assertEqual([1010001, 1010001, 1120011],
                         [row["id"] for row in got[2]])

    def test_recipe_ids_do_not_collide_when_added_in_a_batch(self):
        """★ `nextRecipeId` 扫的是 `CFG.recipe.entries` 的最大值 ⇒ 必须
        **一条算一次、算完就 push**。先造完再统一 push 的话 N 条同号，
        保存时「配方号 N 出现了两次」，整份文件都进不去。"""
        cfg = {"items": {"entries": []}, "shop": {"entries": []},
               "drops": {"entries": []},
               "recipe": {"entries": [{"id": 7, "result": 1010001,
                                       "materials": [{"id": 10003,
                                                      "count": 1}],
                                       "cost": 0, "listed": True}]}}
        probes = [{"call": "nextRecipeId", "args": []}]
        # 模拟批量：算号 → 写进去 → push → 再算下一个
        for _ in range(3):
            probes.append({"call": "nextRecipeId", "args": []})
            probes.append({"call": "eval", "args": [
                "CFG.recipe.entries.push({id: nextRecipeId(), result: 0,"
                " materials: [], cost: 0, listed: true})"]})
        probes.append({"entries": "recipe"})
        got = _run(probes, cfg=cfg)
        ids = [row["id"] for row in got[-1]]
        self.assertEqual([7, 8, 9, 10], ids)
        self.assertEqual(len(ids), len(set(ids)), "配方号撞车了")

    def test_shop_and_recipe_grey_out_what_is_already_there(self):
        """服务端拒收重复（`validate_shop` / `validate_recipes`）⇒ 弹窗里就该
        点不动。★ **掉落页返回空** —— 掉落规则可以重复，在那儿灰掉才是错的。"""
        cfg = {"items": {"entries": []},
               "shop": {"entries": [{"id": 1010001}, {"id": 1120011}]},
               "recipe": {"entries": [{"id": 1, "result": 1920001}]},
               "drops": {"entries": [{"material": 10003}, {"material": 10003}]}}
        got = _run([{"call": "listedAlready", "args": ["shop"]},
                    {"call": "listedAlready", "args": ["recipe"]},
                    {"call": "listedAlready", "args": ["drops"]}], cfg=cfg)
        self.assertEqual({"1010001": True, "1120011": True}, got[0])
        self.assertEqual({"1920001": True}, got[1])
        self.assertEqual({}, got[2])


if __name__ == "__main__":
    unittest.main()
