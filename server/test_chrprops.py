#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""角色属性表的测试（`server/chrprops.py` + `tools/chrprops.py`）。

和 `test_weapondata.py` / `test_mapdata.py` 同一个路数，分两层：

1. **合成数据** —— 自己造一张小表，把「格式版本不认就退默认」「查不到
   也要给一份能用的尺寸」这类边界钉死，任何机器都跑得动；
2. **真实产物** —— `server/bot_chrprops.json` 在的话再跑：17 个角色的
   三个碰撞圆都得有，而且「三个圆叠起来 ≈ `DisplayHeight`」这条**模型
   自检**要成立（那是本工程唯一能验证圆心模型的旁证，见 `chrprops.py`）。

★ 这里最要命的一条是**碰撞圆**：它决定「bot 打没打中你」。半径给小了
bot 一辈子打不中，给大了就回到用户 2026-08-27 报的那条
「我明明躲开了，身上还有命中效果，还掉血」。
"""
import importlib.util
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import chrprops                                                # noqa: E402

TOOLS = os.path.join(os.path.dirname(HERE), "tools")


def load_tool():
    """按**路径**加载 `tools/chrprops.py`；不在就返回 `None`（整类跳过）。

    ★★ 和 `test_weapondata.py` 一样，**绝不能往 `sys.path` 里塞 `tools/`**
    —— 那个目录里也有一个 `mapdata.py`（离线提取器），塞进去会让
    `test_mapdata` 整个跑偏。这里用 `importlib` 起一个独立模块名。
    """
    path = os.path.join(TOOLS, "chrprops.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("_tool_chrprops", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SyntheticTests(unittest.TestCase):
    """自己造一张表，验加载器的边界。"""

    def store(self, table, tmp="_chrprops_test.json"):
        path = os.path.join(HERE, tmp)
        with open(path, "w", encoding="utf-8", newline="\n") as fp:
            json.dump(table, fp, ensure_ascii=False)
        self.addCleanup(os.remove, path)
        return chrprops._Store(path)

    def test_a_known_character_comes_back_with_its_own_sizes(self):
        store = self.store({"format": chrprops.FORMAT, "characters": {
            "7": {"id": 7, "size_head": 1.0, "size_body": 2.0,
                  "size_legs": 3.0, "size_legs_crouch": 0.5}}})
        one = store.get(7)
        self.assertEqual((1.0, 2.0, 3.0),
                         (one.size_head, one.size_body, one.size_legs))
        self.assertTrue(store.known(7))

    def test_an_unknown_character_falls_back_instead_of_none(self):
        """★ 故意**不返回 `None`**：命中判定那条路上多一个 `if is None`，
        就多一处「静默地谁都打不中」的可能。"""
        store = self.store({"format": chrprops.FORMAT, "characters": {}})
        one = store.get(999)
        self.assertIsNotNone(one)
        self.assertEqual(chrprops.DEFAULT_SIZES["size_body"], one.size_body)
        self.assertFalse(store.known(999))

    def test_a_format_we_do_not_know_is_treated_as_no_data(self):
        store = self.store({"format": chrprops.FORMAT + 99, "characters": {
            "7": {"id": 7, "size_body": 999.0}}})
        self.assertEqual(chrprops.DEFAULT_SIZES["size_body"],
                         store.get(7).size_body)

    def test_a_missing_file_is_not_fatal(self):
        store = chrprops._Store(os.path.join(HERE, "_no_such_file.json"))
        self.assertEqual(0, store.count())
        self.assertIsNotNone(store.get(0))


class GeometryTests(unittest.TestCase):
    """三个圆的几何 —— 这就是「人有多大」。"""

    def setUp(self):
        self.one = chrprops.Character({
            "id": 0, "size_head": 10.0, "size_body": 13.0,
            "size_legs": 12.0, "size_legs_crouch": 7.0,
            "display_height": 75.0})

    def test_the_circles_stack_from_the_feet_upward(self):
        circles = self.one.circles(100.0, 500.0)
        self.assertEqual(["head", "body", "legs"], [c[3] for c in circles])
        head, body, legs = circles
        # 脚底正好贴着腿那个圆的下沿。
        self.assertAlmostEqual(500.0, legs[1] + legs[2])
        # 相邻两个圆相切（上一个的上沿 = 下一个的下沿）。
        self.assertAlmostEqual(legs[1] - legs[2], body[1] + body[2])
        self.assertAlmostEqual(body[1] - body[2], head[1] + head[2])

    def test_the_stack_is_about_as_tall_as_display_height(self):
        """★ 这是圆心模型**唯一**的旁证：ini 只给半径没给圆心，
        而「依次相切」算出来的总高和 `DisplayHeight` 对得上（见 `chrprops.py`）。"""
        circles = self.one.circles(0.0, 0.0)
        top = min(c[1] - c[2] for c in circles)
        self.assertAlmostEqual(-70.0, top)
        self.assertLess(abs(-self.one.display_height - top), 10.0)

    def test_the_aim_point_is_the_body_circle(self):
        body = [c for c in self.one.circles(50.0, 300.0) if c[3] == "body"][0]
        self.assertEqual((body[0], body[1]), self.one.center(50.0, 300.0))

    def test_hit_region_picks_head_body_and_legs_apart(self):
        x, y = 0.0, 0.0
        self.assertEqual("legs", self.one.hit_region(x, y, 0.0, -12.0))
        self.assertEqual("body", self.one.hit_region(x, y, 0.0, -37.0))
        self.assertEqual("head", self.one.hit_region(x, y, 0.0, -60.0))

    def test_a_shot_that_goes_wide_is_a_miss(self):
        self.assertIsNone(self.one.hit_region(0.0, 0.0, 200.0, -37.0))
        self.assertIsNone(self.one.hit_region(0.0, 0.0, 0.0, -200.0))

    def test_the_projectile_radius_widens_the_hit(self):
        """`weapon.ini` 的 `Size`（「데미지 사이즈」）和角色半径相加。"""
        self.assertIsNone(self.one.hit_region(0.0, 0.0, 20.0, -37.0))
        self.assertEqual("body",
                         self.one.hit_region(0.0, 0.0, 20.0, -37.0, radius=8.0))

    def test_crouching_makes_the_target_shorter(self):
        """★ 蹲下（`rpCrouch`，§41）腿那个圆变小，整个人跟着矮下去 ——
        原版「蹲能躲子弹」就是这么来的。"""
        standing = self.one.hit_region(0.0, 0.0, 0.0, -65.0)
        crouched = self.one.hit_region(0.0, 0.0, 0.0, -65.0, crouched=True)
        self.assertEqual("head", standing)
        self.assertIsNone(crouched, "站着是头，蹲下这一发就该从头顶上飞过去")


class MoveTests(unittest.TestCase):
    """冲刺攻击的伤害圈 —— 位置公式是 `ChrProps.ini` 自己写的（§64）。"""

    def setUp(self):
        self.move = chrprops.Move({
            "damage": 23.0, "sp_cost": 30.0, "obj_size": 5.0,
            "cast_end": 6, "damage_end": 11, "total_frame": 25,
            "delta_x": 20.0, "delta_y": -50.0,
            "start_degree": 270.0, "max_degree": 180.0,
            "multi_x": 50.0, "multi_y": 10.0})

    def test_the_offset_follows_the_formula_in_the_ini(self):
        """`degree = Start + frame * Max / (DamageEnd - 1)`；
        `x += cos(deg) * MultiForX`、`y += sin(deg) * MultiForY`。"""
        # frame 0：270° ⇒ cos=0、sin=-1
        x, y = self.move.offset(0)
        self.assertAlmostEqual(20.0, x, places=3)
        self.assertAlmostEqual(-60.0, y, places=3)
        # frame 5：270 + 5*180/10 = 360° ⇒ cos=1、sin=0
        x, y = self.move.offset(5)
        self.assertAlmostEqual(70.0, x, places=3)
        self.assertAlmostEqual(-50.0, y, places=3)

    def test_the_radius_falls_back_to_the_bone_size(self):
        """带 `DamagingObjBone` 的招式没有 `DamageSize`（角色 0 的 `Dash00`
        就是），半径写在 `DamagingObjSize` 里。"""
        self.assertEqual(5.0, self.move.radius)
        raw = dict(self.move.raw, damage_size=30.0)
        self.assertEqual(30.0, chrprops.Move(raw).radius)

    def test_only_the_damage_frames_count(self):
        self.assertEqual([6, 7, 8, 9, 10, 11], list(self.move.frames()))

    def test_the_reach_is_a_melee_range(self):
        reach = self.move.reach()
        self.assertGreater(reach, 20.0)
        self.assertLess(reach, 200.0, "近身就该是近身")


class RealTableTests(unittest.TestCase):
    """真实产物 —— `server/bot_chrprops.json` 不在就整类跳过。"""

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(chrprops.DATA_PATH):
            raise unittest.SkipTest("还没跑 tools\\chrprops.py，没有产物")

    def test_every_playable_character_has_all_three_circles(self):
        table = chrprops.STORE.table()["characters"]
        self.assertGreaterEqual(len(table), 10)
        for cid in table:
            one = chrprops.get(int(cid))
            for name, value in (("头", one.size_head), ("身", one.size_body),
                                ("腿", one.size_legs)):
                self.assertGreater(value, 0.0, "角色 %s 的%s圆没有半径" % (cid, name))

    def test_the_stacked_height_matches_display_height_everywhere(self):
        """★★ 圆心模型的自检：17 个角色**全部**成立才敢拿它判命中。"""
        for cid in chrprops.STORE.table()["characters"]:
            one = chrprops.get(int(cid))
            stacked = 2.0 * (one.size_head + one.size_body + one.size_legs)
            self.assertLessEqual(
                abs(one.display_height - stacked), 10.0,
                "角色 %s：三个圆叠起来 %.0f，DisplayHeight %.0f"
                % (cid, stacked, one.display_height))

    def test_the_dash_move_is_there_with_a_cost(self):
        """冲刺攻击（双击左右方向键，§64）的参数得跟着产物一起来。"""
        one = chrprops.get(0)
        dash = one.move("dash0")
        self.assertIsNotNone(dash)
        self.assertGreater(dash["damage"], 0)
        self.assertGreater(dash["sp_cost"], 0)

    def test_every_character_can_dash(self):
        """★ 一个角色没有 `dash0`，它的 bot 就永远不会近身 —— 而这种
        「少了一半玩法」在实机上根本看不出来，只会觉得「这个角色比较菜」。"""
        for cid in chrprops.STORE.table()["characters"]:
            move = chrprops.get(int(cid)).dash()
            self.assertIsNotNone(move, "角色 %s 没有 dash0" % cid)
            self.assertGreater(move.damage, 0, "角色 %s 的 dash0 没有伤害" % cid)
            self.assertGreater(move.radius, 0.0, "角色 %s 的 dash0 没有半径" % cid)
            self.assertGreater(move.reach(), 0.0)
            self.assertGreater(move.total_frame, move.damage_end)

    def test_the_stamina_constants_come_from_gameprops(self):
        """`GameProps.ini` 的 `SpMax` / `SpCharging` / `FastRunSpCost` ——
        「消耗体力触发」那句话里的三个数，一个都不用自己编。"""
        game = chrprops.game()
        self.assertEqual(100.0, game.sp_max)
        self.assertEqual(0.25, game.sp_charging)
        self.assertEqual(1.5, game.fast_run_sp_cost)


class ToolTests(unittest.TestCase):
    """提取器本身。原版 ini 不在（`Pack_decrypt/` 没进工作副本）就跳过。"""

    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()
        if cls.tool is None:
            raise unittest.SkipTest("没有 tools\\chrprops.py")

    def test_it_strips_trailing_comments(self):
        """★ 这个 ini 用 `#` 当行尾注释（`Dash00-Move=9.0  # 이동력`），
        不截掉的话 `float()` 会把整个字段丢掉。"""
        import io
        import tempfile
        path = tempfile.mktemp(suffix=".ini")
        with io.open(path, "w", encoding="cp949", newline="\n") as fp:
            # ★ 内容全用 ini 里真有的字符：这个文件按 CP949 落盘，
            #   写中文注释会当场 `UnicodeEncodeError`。
            fp.write("[0]\nChrIndex=0\nChrSizeBody=13.0  # 몸통\n")
        self.addCleanup(os.remove, path)
        sections = self.tool.read_ini(path)
        record = self.tool.build_character(sections["0"])
        self.assertEqual(13.0, record["size_body"])

    def test_the_section_name_is_not_the_character_id(self):
        """★ 节名是 0..16 的序号，角色 id 在 `ChrIndex` 里
        （节 `[3]` 的 `ChrIndex` 是 100）。按节名建表会把高级角色全错位。"""
        sections = {"3": {"ChrIndex": "100", "ChrSizeBody": "14.0"}}
        table = self.tool.build_table(sections)
        self.assertIn("100", table)
        self.assertNotIn("3", table)


if __name__ == "__main__":
    unittest.main()
