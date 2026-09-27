#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""格斗招式表的测试（`server/mutudata.py` + `tools/mutudata.py`，X_Mod · X16 · B1 / D84）。

三层：

1. **合成表**（`StoreTests`）—— 自己造一份小表，钉读取器：朝左 dx 取反、没这一帧给 None、
   格式对不上 / 文件缺了都退空表不抛异常。任何机器上都跑得动。
2. **真产物**（`RealTableTests`）—— `server/bot_mutu.json` 在就跑：16 角色 × 10 招、句柄数、
   泰尔右直拳那一招的帧窗口 / 位移 / 连段链、`NewMutuConfig.ini` 那几格。
3. **提取器**（`ToolTests`）—— 按文件路径加载 `tools/mutudata.py`（★ 不能 `import`：
   `tools\\` 和 `server\\` 有同名模块），钉帧数公式、位移曲线（指数是 1/γ，§121）、CP949 读取；
   有 numpy 时再重跑一遍提取，和仓库里那份产物逐字节比。
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import mutudata                                                # noqa: E402


def _load_tool():
    spec = importlib.util.spec_from_file_location("tools_mutudata_x16", os.path.join(ROOT, "tools", "mutudata.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SYNTH = {
    "format": mutudata.FORMAT,
    "config": {"GravityFactor": 1.5},
    "guard": {"sp_cost": 0.5, "damage_rate": 0.25},
    "characters": {"0": [{
        "name": "ch00@MutuStand-P00", "index": 0, "motion": "MutuStand-P00", "rate": 1.0,
        "duration": 0.266667, "samples": 18, "timer_ms": 266, "frames_total": 9, "damage": 3.0,
        "kind": 2, "skill_type": 1, "prev": -1, "input": "P", "handles": 2,
        "move": {"dist": 10.0, "gamma": 3.0, "start": 3, "end": 7},
        "move_track": [[2, 7], [3, 3]],
        "damagers": [{"bone": "Bip01_R_Forearm", "size": 18.0, "start": 6, "end": 14,
                      "offset": [0, 0], "bone_missing": False,
                      "track": [[3, 6, 31.0, -47.5], [4, 8, 29.5, -48.0]]}],
        "damagees": []}]},
}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def store(self, table):
        path = os.path.join(self.tmp, "bot_mutu.json")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(table, fp)
        return mutudata._Store(path=path)

    def test_a_skill_reads_back(self):
        skill = self.store(SYNTH).skills(0)[0]
        self.assertEqual((0, "P", 2, 2, 1, -1), (skill.index, skill.input, skill.handles, skill.kind,
                                                 skill.skill_type, skill.prev))
        self.assertEqual({2: 7, 3: 3}, skill.move)
        self.assertEqual(266, skill.timer_ms)

    def test_facing_left_mirrors_dx_only(self):
        hit = self.store(SYNTH).skills(0)[0].damagers[0]
        self.assertEqual((31.0, -47.5), hit.at(3, 1))
        self.assertEqual((-31.0, -47.5), hit.at(3, -1))

    def test_a_frame_outside_the_window_is_none(self):
        hit = self.store(SYNTH).skills(0)[0].damagers[0]
        self.assertIsNone(hit.at(2, 1))
        self.assertIsNone(hit.at(5, 1))

    def test_unknown_character_is_an_empty_list(self):
        self.assertEqual([], self.store(SYNTH).skills(99))

    def test_wrong_format_is_an_empty_table(self):
        table = dict(SYNTH, format=mutudata.FORMAT + 99)
        self.assertEqual([], self.store(table).skills(0))

    def test_a_missing_file_is_not_fatal(self):
        store = mutudata._Store(path=os.path.join(self.tmp, "没有这个文件.json"))
        self.assertEqual([], store.skills(0))
        self.assertEqual({}, store.config())

    def test_config_and_guard(self):
        store = self.store(SYNTH)
        self.assertEqual({"GravityFactor": 1.5}, store.config())
        self.assertEqual({"sp_cost": 0.5, "damage_rate": 0.25}, store.guard())


class RealTableTests(unittest.TestCase):
    """真产物（`tools\\mutudata.py` 提取的）在就跑。"""

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(mutudata.DATA_PATH):
            raise unittest.SkipTest("没有 server/bot_mutu.json，先跑 tools\\mutudata.py")
        cls.store = mutudata._Store()
        with open(mutudata.DATA_PATH, "r", encoding="utf-8") as fp:
            cls.raw = json.load(fp)

    def test_sixteen_characters_ten_skills_each(self):
        chars = self.raw["characters"]
        self.assertEqual(16, len(chars))
        for cid, skills in chars.items():
            self.assertEqual(10, len(skills), "角色 %s 不是 10 招" % cid)
            self.assertEqual(list(range(10)), [s["index"] for s in skills])

    def test_the_base_characters_have_every_bone(self):
        # bot 用得到的角色（0~2，外加爱琳 3）判定骨都得找得到 —— 找不到就退回脚底，打不着人。
        for cid in ("0", "1", "2", "3"):
            for s in self.raw["characters"][cid]:
                for d in s["damagers"]:
                    self.assertFalse(d["bone_missing"], "角色 %s 的 %s 找不到骨 %s" % (cid, s["motion"], d["bone"]))
                    self.assertTrue(d["track"], "角色 %s 的 %s 一帧判定都没有" % (cid, s["motion"]))

    def test_handles_are_damagers_plus_damagees_plus_one(self):
        for cid, skills in self.raw["characters"].items():
            for s in skills:
                self.assertEqual(len(s["damagers"]) + len(s["damagees"]) + 1, s["handles"],
                                 "角色 %s 的 %s" % (cid, s["motion"]))

    def test_every_track_frame_is_inside_its_window(self):
        for cid, skills in self.raw["characters"].items():
            for s in skills:
                for d in s["damagers"] + s["damagees"]:
                    for k, f, _dx, _dy in d["track"]:
                        self.assertTrue(d["start"] <= f <= d["end"], (cid, s["motion"], k, f))
                        self.assertLess(k, s["frames_total"])

    def named(self, cid, motion):
        """按节名（`chNN@<motion>`）找 —— 不按 `Motion` 键：`Jump-P00-K` 那一节的动作也是 `MutuJump-P00`。"""
        return next(s for s in self.store.skills(cid) if s.name.endswith("@" + motion))

    def test_tai_right_jab(self):
        # 泰尔 `MutuStand-P00`：D = 0.2667 s ⇒ N = 18、计时器 266 ms、9 个逻辑帧；
        # Damager1 起止 4..8 ⇒ 引擎帧 6..14 ⇒ 第 3~7 个逻辑帧；MoveDist 10 / γ 3 / [3, 7)。
        jab = self.named(0, "MutuStand-P00")
        self.assertEqual(6, jab.index, "客户端招式号（X_Mod §127），不是文件顺序的 0")
        self.assertEqual(("MutuStand-P00", 18, 266, 9, 2, 2), (jab.motion, jab.samples, jab.timer_ms,
                                                               jab.frames_total, jab.handles, jab.kind))
        hit = jab.damagers[0]
        self.assertEqual(("Bip01_R_Forearm", 18.0, 6, 14), (hit.bone, hit.size, hit.start, hit.end))
        self.assertEqual([3, 4, 5, 6, 7], sorted(hit.points))
        dx, dy = hit.at(3, 1)
        self.assertGreater(dx, 20.0)               # 往前伸出去了（朝右为正）
        self.assertLess(dy, -30.0)                 # 在脚底上方（屏幕 y 朝下）
        self.assertEqual({2: 7, 3: 3}, jab.move)   # 合计 = MoveDist
        self.assertEqual(10, sum(jab.move.values()))

    def test_the_jab_chain_links_by_prev(self):
        names = ["MutuStand-P00", "MutuStand-P01", "MutuStand-P02", "MutuStand-P03"]
        chain = [self.named(0, n) for n in names]
        self.assertEqual([-1] + [s.index for s in chain[:3]], [s.prev for s in chain])
        self.assertEqual([mutudata.STAND] * 4, [s.skill_type for s in chain])

    def test_the_order_is_what_the_client_sends(self):
        """B2 探针（X_Mod §127）：记录地址 = 基址 + 招式号 × 0x50，五个角色的内存顺序逐个核过。"""
        seen = {
            0: ["Jump-P00-K", "Crouch-K00", "Jump-P00", "Stand-K02", "Stand-K01",
                "Stand-P01", "Stand-P00", "Stand-P03", "Stand-P02", "Crouch-P00"],
            1: ["Stand-K01", "Stand-K00", "Jump-P00", "Crouch-K00", "Stand-P00",
                "Stand-P01", "Stand-P02", "Stand-P03", "Jump-P00-K", "Crouch-P00"],
            2: ["Jump-P00-K", "Crouch-K00", "Stand-K00", "Stand-K01", "Stand-P03",
                "Stand-P02", "Stand-P01", "Stand-P00", "Crouch-P00", "Jump-P00"],
            3: ["Stand-P02", "Stand-P03", "Stand-P00", "Stand-P01", "Stand-K01",
                "Stand-K00", "Crouch-K00", "Jump-P00-K", "Jump-P00", "Crouch-P00"],
            104: ["Jump-P00", "Crouch-P00", "Stand-K00", "Stand-K01", "Stand-P03",
                  "Stand-P02", "Stand-P01", "Stand-P00", "Jump-P00-K", "Crouch-K00"],
        }
        for cid, order in seen.items():
            # 按节名比（`Jump-P00-K` 那一节的 `Motion` 也是 `MutuJump-P00`）。
            self.assertEqual(["ch%02d@Mutu%s" % (cid, m) for m in order],
                             [s.name for s in self.store.skills(cid)], "角色 %d" % cid)

    def test_tracks_carry_the_model_scale(self):
        """判定体 = 骨骼点 × 模型缩放（`0x5050ff`：卡希尔 0.75，其余 0.85，X_Mod §127）。"""
        units = self.raw["units"]
        self.assertEqual(0.85, units["model_scale"])
        self.assertEqual({"1": 0.75}, units["model_scale_by_character"])
        # 泰尔右直拳第 3 帧：模型里 z ≈ −31、y ≈ 47.5 ⇒ ×0.85 ≈ (26.4, −40.4)。
        dx, dy = self.named(0, "MutuStand-P00").damagers[0].at(3, 1)
        self.assertAlmostEqual(26.4, dx, delta=1.5)
        self.assertAlmostEqual(-40.4, dy, delta=1.5)

    def test_config_comes_from_newmutuconfig(self):
        self.assertEqual({"GravityFactor": 1.5, "FirstJumpHeight": 240.0, "SecondJumpHeight": 300.0,
                          "DamagedFlyGravityFactor": 1.5, "DamageBounceVectorMultiplierX": 1.0,
                          "DamageBounceVectorMultiplierY": 1.5}, self.store.config())
        self.assertEqual({"sp_cost": 0.5, "damage_rate": 0.25}, self.store.guard())


class ToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = _load_tool()

    def test_sample_count(self):
        self.assertEqual(18, self.tool.sample_count(0.266667))
        self.assertEqual(54, self.tool.sample_count(0.866667))

    def test_engine_frame_is_clamped_to_the_last_sample(self):
        self.assertEqual(0, self.tool.engine_frame(0.0, 0.266667, 18))
        self.assertEqual(6, self.tool.engine_frame(0.096, 0.266667, 18))
        self.assertEqual(17, self.tool.engine_frame(5.0, 0.266667, 18))

    def test_move_curve_uses_one_over_gamma(self):
        # ★ `0x5ce3a0(x, γ)` = pow(x, 1/γ)（§121 亲手核）。f 走 0,2,4,6,8：
        #   f=4 ⇒ ⌊10·(2/4)^(1/3)⌋ = 7；f=6 ⇒ ⌊10·1⌋ − 7 = 3。
        self.assertEqual([[2, 7], [3, 3]], self.tool.move_steps(10.0, 3.0, 3, 7, [0, 2, 4, 6, 8]))
        # 若误用 γ 当指数，f=4 那一步会是 ⌊10·(1/2)^3⌋ = 1。
        self.assertNotEqual(1, self.tool.move_steps(10.0, 3.0, 3, 7, [0, 2, 4, 6, 8])[0][1])

    def test_move_curve_stops_before_the_end_frame(self):
        self.assertEqual([], self.tool.move_steps(10.0, 3.0, 3, 7, [0, 1, 2, 7, 9]))

    def test_ini_hash_folds_case(self):
        """`0x402c57` 逐码元先 `towupper`（只折 a~z）—— 大小写不同的节名进同一个桶。"""
        self.assertEqual(self.tool.ini_hash("ch00@MutuStand-P00"), self.tool.ini_hash("CH00@mutustand-p00"))
        self.assertNotEqual(self.tool.ini_hash("ch00@MutuStand-P00"), self.tool.ini_hash("ch00@MutuStand-P01"))
        self.assertEqual(0, self.tool.ini_hash(""))

    def test_same_bucket_puts_the_later_one_first(self):
        """同一个桶里后插的在前（`0x5d45c6` 插链头）；桶按号从小到大走。"""
        names = ["a%d" % i for i in range(400)]
        order = self.tool.client_section_order(names)
        self.assertEqual(sorted(names), sorted(order), "一个都不能丢（400 > 193 会扩容一次）")
        small = names[:150]
        order = self.tool.client_section_order(small)
        size = self.tool._table_size(self.tool.INI_TABLE_HINT)
        self.assertEqual(193, size)
        buckets = [self.tool.ini_hash(n) % size for n in order]
        self.assertEqual(sorted(buckets), buckets)
        for b in set(buckets):
            chain = [n for n in order if self.tool.ini_hash(n) % size == b]
            self.assertEqual(sorted(chain, key=small.index, reverse=True), chain)

    def test_model_scale(self):
        self.assertEqual(0.75, self.tool.model_scale(1))
        self.assertEqual(0.85, self.tool.model_scale(0))
        self.assertEqual(0.85, self.tool.model_scale(104))

    def test_read_ini_decodes_cp949(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "NewMutu.ini")
        text = "# 타이\r\n[ch00@MutuStand-P00]\r\nChrIdx=0\r\nMotion=MutuStand-P00\t\t# 오른손 쨉\r\n"
        with open(path, "wb") as fp:
            fp.write(text.encode("cp949"))
        sections = self.tool.read_ini(path)
        self.assertEqual("ch00@MutuStand-P00", sections[1][0])
        self.assertEqual("MutuStand-P00", sections[1][1]["Motion"])

    def _probe_log(self, facing, scale=1.0):
        """照 bshook 探针（`BSHOOK_MUTU_DIAG=1`）的格式拿 SYNTH 那一招造一份日志：判定体偏移 = 表 × 朝向 × scale。"""
        lines = ["[10:00:00.000 UTC+9] MUTU=   招式表 0AB1C2D0 座位 0 MoveDist 10.000 γ 3.000 起 3 止 7 判定体 1 受击体 0"]
        for _k, f, dx, dy in SYNTH["characters"]["0"][0]["damagers"][0]["track"]:
            lines.append("[10:00:00.%03d UTC+9] MUTU.   座位 0 招式 0BAD0000 表 0AB1C2D0 帧 %d 朝 %+d 脚 (300.00, 399.00)"
                         " 判定 0:(%.2f,%.2f) 受击" % (f, f, facing, dx * facing * scale, dy * scale))
        lines.append("[10:00:00.999 UTC+9] MUTU.   座位 0 招式 0BAD0000 表 0AB1C2D0 帧 12 朝 %+d 脚 (300.00, 399.00)"
                     " 判定 0:(1.00,2.00) 受击" % facing)
        return "\n".join(lines) + "\n"

    def _check(self, text):
        import io
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        log, table = os.path.join(tmp, "bshook.log"), os.path.join(tmp, "t.json")
        with open(log, "w", encoding="utf-8") as fp:
            fp.write(text)
        with open(table, "w", encoding="utf-8") as fp:
            json.dump(SYNTH, fp, ensure_ascii=False)
        out = io.StringIO()
        self.assertEqual(0, self.tool.check(log, table, {}, 0, out=out))
        row = [line for line in out.getvalue().splitlines() if line.startswith("ch00@MutuStand-P00")]
        self.assertEqual(1, len(row), out.getvalue())
        return row[0].split()

    def test_check_matches_a_log_made_from_the_table(self):
        """B2 比对工具：朝右 / 朝左（dx 取反）各造一份，两点全对上、比例 1、表里没有的那一帧记「只日志」。"""
        for facing in (1, -1):
            _name, n, mid, _worst, mirror, scale, only_log, only_table = self._check(self._probe_log(facing))
            self.assertEqual(("2", "0.00", "1.000", "1", "0"), (n, mid, scale, only_log, only_table))
            self.assertGreater(float(mirror), 50.0, "镜像那一列应当明显更差")

    def test_check_reports_the_scale(self):
        cols = self._check(self._probe_log(1, scale=1.25))
        self.assertEqual("1.250", cols[5])

    def test_check_tells_twins_apart_by_address(self):
        """参数一模一样的两招（泰尔 K01 / K02 那种）：按「基址 + 招式号 × 0x50」认，不再都认成第一个（X_Mod §127）。"""
        import copy
        import io
        table = copy.deepcopy(SYNTH)
        twin = copy.deepcopy(table["characters"]["0"][0])
        twin["name"], twin["index"] = "ch00@MutuStand-P00-孪生", 1
        twin["damagers"][0]["track"] = [[3, 6, -5.0, -20.0], [4, 8, -6.0, -21.0]]
        table["characters"]["0"].append(twin)
        head = "MoveDist 10.000 γ 3.000 起 3 止 7 判定体 1 受击体 0"
        lines = ["[10:00:00.000 UTC+9] MUTU=   招式表 0AB1C2D0 座位 0 " + head,
                 "[10:00:00.000 UTC+9] MUTU=   招式表 0AB1C320 座位 0 " + head]
        for addr, skill in (("0AB1C2D0", table["characters"]["0"][0]), ("0AB1C320", twin)):
            for _k, f, dx, dy in skill["damagers"][0]["track"]:
                lines.append("[10:00:00.100 UTC+9] MUTU.   座位 0 招式 0BAD0000 表 %s 帧 %d 朝 +1 脚 (300.00, 399.00)"
                             " 判定 0:(%.2f,%.2f) 受击" % (addr, f, dx, dy))
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        log, path = os.path.join(tmp, "bshook.log"), os.path.join(tmp, "t.json")
        with open(log, "w", encoding="utf-8") as fp:
            fp.write("\n".join(lines) + "\n")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(table, fp, ensure_ascii=False)
        out = io.StringIO()
        self.tool.check(log, path, {}, 0, out=out)
        rows = dict((line.split()[0], line.split()) for line in out.getvalue().splitlines()
                    if line.startswith("ch00@"))
        self.assertEqual({"ch00@MutuStand-P00", "ch00@MutuStand-P00-孪生"}, set(rows), out.getvalue())
        for cols in rows.values():
            self.assertEqual(("2", "0.00"), (cols[1], cols[2]), out.getvalue())

    def test_a_fresh_extraction_matches_the_committed_table(self):
        if importlib.util.find_spec("numpy") is None:
            self.skipTest("这个 Python 没装 numpy（便携运行时就没有）—— 用 C:\\Python314 跑")
        pack = os.path.join(ROOT, "game_patched", "Pack_develop")
        if not os.path.isdir(os.path.join(pack, "Models", "Characters")) or not os.path.isfile(mutudata.DATA_PATH):
            self.skipTest("不在源码仓库里（缺明文资源树或产物）")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        out = os.path.join(tmp, "bot_mutu.json")
        self.tool.main(["--pack", pack, "--out", out, "--quiet"])
        with open(out, "rb") as fa, open(mutudata.DATA_PATH, "rb") as fb:
            self.assertEqual(fb.read(), fa.read(), "产物过期了 —— 重跑 tools\\mutudata.py")


if __name__ == "__main__":
    unittest.main()
