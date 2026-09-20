#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""武器表的测试（`server/weapondata.py` + `tools/weapondata.py`）。

分两层，和 `test_mapdata.py` 一个路数：

1. **合成数据**（`SyntheticTests`）—— 自己造一张小表，把「格式版本不认就当
   没有数据」「取不到返回 None」这类边界钉死。不依赖产物，任何机器都跑得动。
2. **真实产物**（`RealTableTests`）—— `server/bot_weapons.json` 在的话再跑：
   每个玩家角色都得有一把能用的枪、可用武器的四个条件逐条成立。
   产物不在就整类跳过（打包机上可能还没跑提取）。

★ 这里最要命的一条是 **`handle_step`**：它决定 bot 每发 `rpFire` 之后把
弹体句柄计数器往前推几格。推错了 `rpExplode` 会被收方**静默丢弃**
（`0x492750` 查不到弹体就整个 return），表现是「子弹飞过去不炸、一滴血
不掉」，而且一局之内不自愈（§42 / D28）。所以「不确定就返回 None、
bot 就不用这把枪」这条口径要有用例守着。
"""
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import weapondata                                              # noqa: E402

TOOLS = os.path.join(os.path.dirname(HERE), "tools")

_SPEC = []


def _cw_spec():
    """`tools/custom-weapon/spec.py` —— 自定义武器的唯一清单（只依赖标准库，便携运行时能跑）。"""
    if not _SPEC:
        import importlib.util
        path = os.path.join(TOOLS, "custom-weapon", "spec.py")
        s = importlib.util.spec_from_file_location("test_cw_spec", path)
        module = importlib.util.module_from_spec(s)
        s.loader.exec_module(module)
        _SPEC.append(module)
    return _SPEC[0]


def load_tool():
    """按**路径**加载 `tools/weapondata.py`；不在就返回 None（用例整类跳过）。

    ★★ **绝不能往 `sys.path` 里塞 `tools/`**：那个目录里也有一个
    `mapdata.py`（离线提取器），塞进去之后同一批测试里的
    `import mapdata` 会导到工具那份，`test_mapdata` 整个跑偏。
    这里用 `importlib` 起一个**独立模块名**，谁都不影响。
    """
    import importlib.util
    path = os.path.join(TOOLS, "weapondata.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("weapondata_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = load_tool()


class SyntheticTests(unittest.TestCase):
    """自己造一张表，钉住加载器的边界行为。"""

    def store(self, table, tmp="_test_weapons.json"):
        # ★ 写进临时目录，**不写进仓库** —— `run_tests.py` 的并行前提之一
        #   就是「没有任何测试往仓库里写文件」。
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        path = os.path.join(tmpdir.name, tmp)
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(table, fp)
        return weapondata._Store(path)

    def test_missing_file_is_not_fatal(self):
        """★ 没有武器表**不该让服务端起不来** —— bot 照样会跑会跳（M3a），
        只是不开枪。"""
        store = weapondata._Store(os.path.join(HERE, "_no_such_file.json"))
        self.assertEqual(0, store.count())
        self.assertIsNone(store.get(1002010))
        self.assertIsNone(store.preferred_for(2))

    def test_unknown_format_is_treated_as_no_data(self):
        """格式版本对不上就当没有 —— 宁可 bot 不开枪，也不要按错的布局
        解出一把参数乱七八糟的武器。"""
        store = self.store({"format": 999, "weapons": {"1": {"id": 1}}})
        self.assertEqual(0, store.count())

    def test_lookup_returns_none_for_unknown_ids(self):
        store = self.store({
            "format": weapondata.FORMAT,
            "weapons": {"7": {"id": 7, "damage": 3, "handle_step": 1}},
            "preferred": {}, "usable": [],
        })
        self.assertEqual(3, store.get(7).damage)
        self.assertIsNone(store.get(8))

    def test_weapon_exposes_raw_fields_as_attributes(self):
        store = self.store({
            "format": weapondata.FORMAT,
            "weapons": {"7": {"id": 7, "damage": 3, "handle_step": 1,
                              "section": "ch07-01", "cooling_ms": 140}},
            "preferred": {"7": 7}, "usable": [7],
        })
        weapon = store.get(7)
        self.assertEqual("ch07-01", weapon.section)
        self.assertEqual(140, weapon.raw["cooling_ms"])
        with self.assertRaises(AttributeError):
            weapon.no_such_field

    def test_preferred_is_looked_up_by_character(self):
        store = self.store({
            "format": weapondata.FORMAT,
            "weapons": {"7": {"id": 7, "damage": 3, "handle_step": 1}},
            "preferred": {"2": 7}, "usable": [7],
        })
        self.assertEqual(7, store.preferred_for(2).id)
        self.assertIsNone(store.preferred_for(3))


@unittest.skipIf(TOOL is None, "tools/weapondata.py 导不进来")
class HandleStepRuleTests(unittest.TestCase):
    """★★ `handle_step_of()` —— 整个 M3b 里最要命的那条判据。"""

    def test_plain_bullet_advances_one(self):
        """语料实测：`1002010` 93/101、`1109010` 76/82、`1000010` 54/60
        都是步进 1（剩下的零头是 `rpExplode` 到达顺序的噪声，§43）。"""
        self.assertEqual(1, TOOL.handle_step_of({"damage": 3}))
        self.assertEqual(1, TOOL.handle_step_of(
            {"damage": 3, "spread_frags": 1}))

    def test_splash_weapons_advance_two(self):
        """带溅射的武器爆炸时额外创建一个 `SplashDamage` 对象，
        多分配一次句柄（`0x484920`）。语料实测恒 2。"""
        self.assertEqual(2, TOOL.handle_step_of(
            {"damage": 22, "splash_range": 100}))

    def test_multi_fragment_weapons_advance_once_per_fragment(self):
        """★ `SpreadFrags = N` 的武器一发**造 N 颗**弹体（§46：收侧
        `OnFire` 的内层循环跑 `SpreadFrags` 轮，每轮注册一个句柄），
        所以句柄一发前进 N 格；再带溅射就是 2N。"""
        self.assertEqual(5, TOOL.handle_step_of(
            {"damage": 3, "spread_frags": 5}))
        self.assertEqual(4, TOOL.handle_step_of(
            {"damage": 15, "spread_frags": 2, "splash_range": 50}))

    def test_shots_is_the_fragment_count(self):
        """★★ `rpFire +22` 必须填满 `SpreadFrags` —— 收侧外层轮数是
        `count / SpreadFrags` 的**整数除法**，填 1 打 3 散弹的枪
        等于 `1 / 3 = 0` 轮，一颗子弹都造不出来（§46）。"""
        self.assertEqual(1, TOOL.shots_of({}))
        self.assertEqual(1, TOOL.shots_of({"spread_frags": 1}))
        self.assertEqual(3, TOOL.shots_of({"spread_frags": 3}))

    def test_fire_interval_falls_back_to_reload_time(self):
        """缺 `CoolingTime` 的武器**同时也没有 `MagazineCount`**
        （打一发装一次），所以装填时间就是它的发射间隔。"""
        self.assertEqual(140, TOOL.fire_interval_of({"cooling_ms": 140,
                                                     "reload_ms": 1400}))
        self.assertEqual(1500, TOOL.fire_interval_of({"reload_ms": 1500}))
        self.assertIsNone(TOOL.fire_interval_of({}))


@unittest.skipIf(not os.path.isfile(weapondata.DATA_PATH),
                 "server/bot_weapons.json 还没生成")
class RealTableTests(unittest.TestCase):
    """真产物：`tools\\weapondata.py` 跑出来的那份。"""

    #: 玩家真的能选的角色（§11 / `account_store.BASE_CHARACTER_IDS` 那一套）。
    PLAYABLE = (0, 1, 2) + tuple(range(100, 111))

    def test_table_is_not_empty(self):
        self.assertGreater(weapondata.count(), 100)
        self.assertGreater(len(weapondata.usable()), 10)

    def test_every_playable_character_has_a_gun(self):
        """★ 每个玩家角色都得有一把 bot 能用的枪 —— 否则用户 `/char` 换到
        那个角色，bot 就哑火了，而且现象和「句柄错了」长得一样。"""
        for character in self.PLAYABLE:
            with self.subTest(character=character):
                self.assertIsNotNone(weapondata.preferred_for(character),
                                     f"角色 {character} 没有可用武器")

    def test_usable_weapons_meet_all_conditions(self):
        """`_is_usable()` 的五个条件（D29 + 会话 14 放宽）逐条复核一遍产物。"""
        for ammo in weapondata.usable():
            weapon = weapondata.get(ammo)
            with self.subTest(ammo=ammo):
                self.assertGreaterEqual(weapon.handle_step, weapon.shots)
                self.assertEqual(weapon.handle_step,
                                 weapon.shots * (2 if weapon.splash_range else 1))
                self.assertIn(weapon.power_control, TOOL.KNOWN_POWER_MODES)
                self.assertGreater(weapon.damage, 0)
                self.assertTrue(weapon.fire_interval_ms)
                self.assertIsNotNone(weapon.raw.get("character"))

    def test_every_playable_character_has_at_least_one_usable_weapon(self):
        """★ 每个角色至少得有一把能用的枪，否则它的 bot 一枪都不放。

        ⚠ 会话 14 这一条曾经是「**三个槽位全可用**」（用户 2026-08-26 报的
        「所有角色应该都有 3 个武器能用」）。会话 19 收紧了（§70），
        会话 21 又放宽回来（§72，见下面那条钉子）。
        """
        for character in self.PLAYABLE:
            slots = sorted(w.raw["slot"]
                           for w in weapondata.usable_for(character))
            with self.subTest(character=character):
                self.assertTrue(slots, f"角色 {character} 一把能用的枪都没有")
                self.assertIn(1, slots, f"角色 {character} 连 1 号枪都不可用")

    def test_every_playable_character_has_a_second_slot(self):
        """★★ 用户 2026-08-27 报的：「很多角色都无法切换 2 号武器」。

        §70 那一版把 `CreatingClass != GeneralBullet` 全剔掉了，10/16 个
        角色的 2 号槽因此消失。§72 把口径改对之后只剩**反弹弹**
        （`BounceBullet`，角色 106 / 110）和**炮台**（`RasTurret`，
        角色 107 / 108）还没回来 —— 那两类的**飞行**服务端确实没有模型。
        """
        missing = [c for c in self.PLAYABLE
                   if 2 not in [w.raw["slot"]
                                for w in weapondata.usable_for(c)]]
        self.assertEqual([106, 107, 108, 110], sorted(missing))

    def test_only_classes_we_have_a_flight_model_for_are_usable(self):
        """★★★ §72 的回归钉子：`CreatingClass` 必须在白名单里。

        判据是「**在别人那台机器上它和 `GeneralBullet` 有没有区别**」：
        每个子类的 `Tick` 都以 `call 0x47de6a`（基类 Tick）开头，飞行一样；
        分裂 / 火墙 / 炮台的创建点全在 `IsMine`（`0x50d294`）门里，而 bot
        的弹体在任何一台上都不是「自己的」⇒ 那些对象一个都不会造出来，
        句柄一个都不会多吃（§72 推翻了 §70 的收紧口径）。

        还在门外的是**飞行**对不上的那几类：`BounceBullet`（弹墙）、
        `RasTurret`（放炮台）、`PlasmaCannon`（自己 `++` 了句柄计数器）。
        """
        allowed = {"GeneralBullet", "AppleGrenade", "SeedBomb", "SliceBullet",
                   "FlamingBottle", "TimeBomb", "SpiralKnife"}
        for ammo in weapondata.usable():
            weapon = weapondata.get(ammo)
            self.assertIn(weapon.raw.get("creating_class"), allowed,
                          f"{ammo}（{weapon.raw.get('section')}）的弹体类"
                          f"服务端没有飞行模型")

    def test_fused_projectiles_carry_their_fuse(self):
        """★ 带引信的那三类必须算得出 `fuse_ticks`（`SliceTime / 32`）。

        引信到点时弹体**在每一台机器上自爆**且不带伤害（§72）——
        算不出来的话服务端不知道该在哪一 tick 之前把 `rpExplode` 发出去。
        """
        fused = {"AppleGrenade", "SeedBomb", "SliceBullet"}
        seen = set()
        for ammo in weapondata.usable():
            weapon = weapondata.get(ammo)
            klass = weapon.raw.get("creating_class")
            if klass not in fused:
                self.assertIsNone(weapon.fuse_ticks, f"{ammo} 不该有引信")
                continue
            seen.add(klass)
            self.assertGreaterEqual(weapon.fuse_ticks, 2, f"{ammo} 引信太短")
            self.assertEqual(weapon.raw["slice_time"] // 32, weapon.fuse_ticks)
        self.assertEqual(fused, seen)

    def test_quest_weapons_carry_their_quest_and_difficulty(self):
        """★★★ 每关的怪 / boss 武器表（§141）：**`(关卡, 难度, id)` 三维查**。

        这些 id 是关卡内局部的（`2003010` 在 Quest02..Quest07 里是不同的
        枪），四个难度各一份、**连弹速都不同** —— 全局按 id 查必然给错的
        数值。载具 boss 替发的 `rpFire` 用这批 id：`note_peer_fire` 解不出
        武器的话整包丢掉，boss 的枪口坐标流（它位置的唯一网络来源）和
        「躲 boss 子弹」一起没。★ 它们**不是任何角色的槽位**，不在主表
        `weapons` 里，永远进不了 `usable`（bot 换不到怪枪）。
        """
        easy = weapondata.get_quest(3003010, 3, 1)   # quest03 Boss-HeadFire
        self.assertIsNotNone(easy, "quest3 难度1 的 3003010 不在表里")
        self.assertAlmostEqual(14.0, easy.velocity, places=3)
        self.assertAlmostEqual(25.0, easy.damage, places=3)
        hard = weapondata.get_quest(3003010, 3, 3)
        self.assertIsNotNone(hard)
        self.assertAlmostEqual(17.0, hard.velocity, places=3,
                               msg="难度之间连弹速都不同（14 -> 17）")
        self.assertIsNone(weapondata.get(3003010),
                          "关卡武器不在主表里，只能带关卡+难度查")
        # 同一个 id 在别的关卡是别的枪。
        self.assertIsNotNone(weapondata.get_quest(2003020, 2, 1))
        self.assertIsNotNone(weapondata.get_quest(2003020, 7, 1))
        usable = {str(w) for w in weapondata.usable()}
        for ammo in ("3003010", "3003020", "2003020"):
            self.assertNotIn(ammo, usable,
                             "%s 是怪/boss 的枪，bot 不许用" % ammo)

    def test_quest_overlays_apply_on_top_of_the_main_definition(self):
        """★★★（二次复审）重号的 8 个怪武器 id：quest 节是主表的**增量覆盖**。

        提取时在原始字段层合并（主表节做底、quest 字段盖上）：
        * Quest03 简单的 `2003010` 是 **弹速 5 / 伤害 6** —— 主表那份是
          弹速 3 / 伤害 8，先查主表的话永远拿不到关卡数值（reviewer 的复现）；
        * quest 节没写的字段**继承主表**（`2003000` 的 Velocity / CreatingClass），
          否则 436 份变体里 188 份没有 `CreatingClass`、84 份弹速会变 0；
        * 主表本身一个字都不动（非闯关上下文的兜底还是它）。
        """
        overlay = weapondata.get_quest(2003010, 3, 1)
        self.assertIsNotNone(overlay)
        self.assertAlmostEqual(5.0, overlay.velocity, places=3,
                               msg="Quest03 简单的 Soldier-Pistol 弹速是 5")
        self.assertAlmostEqual(6.0, overlay.damage, places=3,
                               msg="……伤害是 6，不是主表的 8")
        self.assertEqual("GeneralBullet", overlay.creating_class,
                         "quest 节没写 CreatingClass，从主表那节继承")
        melee = weapondata.get_quest(2003000, 3, 1)
        self.assertAlmostEqual(1.0, melee.velocity, places=3,
                               msg="quest 节没写 Velocity，从主表那节继承")
        self.assertAlmostEqual(10.0, melee.damage, places=3,
                               msg="伤害用 quest 覆盖的 10，不是主表的 5")
        main = weapondata.get(2003010)
        self.assertAlmostEqual(3.0, main.velocity, places=3)
        self.assertAlmostEqual(8.0, main.damage, places=3,
                               msg="主表那份不动 —— 非闯关上下文的兜底")

    def test_the_uppercase_sections_are_not_dropped(self):
        """★ `[CH01-01]` / `[CH03-01]` 这两个大写节的回归钉子（§45）。"""
        for ammo in (1001010, 1003010):
            weapon = weapondata.get(ammo)
            self.assertIsNotNone(weapon, f"{ammo} 不在表里")
            self.assertEqual(1, weapon.raw["slot"])

    def test_preferred_weapons_are_all_usable(self):
        usable = set(weapondata.usable())
        for character in self.PLAYABLE:
            weapon = weapondata.preferred_for(character)
            if weapon is not None:
                self.assertIn(weapon.id, usable)

    def test_every_roh_is_one_of_the_nine_weapon_cards(self):
        """武器族号只有 9 个取值，而且**就是 9 张武器卡片的 id**（V0.3商店 §37）。

        称号卡片的 `weapon_*` 指标整条链压在这上面：卡片 id == `ROH` ==
        客户端 `GetLastBulletROHIdx()` 比的那个数。多出第十个值就说明
        提取口径变了，那几条默认规则会静默失效。
        """
        tagged = 0
        for raw in weapondata.STORE.table()["weapons"].values():
            if "roh" not in raw:
                continue
            tagged += 1
            self.assertIn(raw["roh"], weapondata.WEAPON_ROH, raw["section"])
        # 原版 228 节里带 ROH 的 **127** 节 + 每批自定义武器 11 节（9 主 + 2 子弹药，
        # `ROH` 照抄各自的母本 ⇒ 用自定义左轮打的也算「左轮高手」那一族）。
        # ★ 自定义那部分**按清单算**，不写死：加一批就是 +11，改常量的活儿交给 spec。
        custom = sum(1 + (1 if w.piece_section else 0) for w in _cw_spec().WEAPONS)
        self.assertEqual(127 + custom, tagged)

    def test_roh_of_covers_the_three_base_characters(self):
        """基础三角色的 9 把主武器**一把不落**地映射到自己那张卡片上。

        ⚠ 同时钉住反面：商城角色自带的枪**没有** ROH（原版数据就没写）——
        用商城角色打拿不到武器卡片，这是既成事实，不是回归。
        """
        want = {1000010: 110001, 1000020: 110002, 1000030: 110003,
                1001010: 120001, 1001020: 120002, 1001030: 120003,
                1002010: 130001, 1002020: 130002, 1002030: 130003}
        for ammo, roh in want.items():
            self.assertEqual(roh, weapondata.roh_of(ammo), ammo)
        # D / R / F 变体和 SE 跟着本族走（玩家在商店买的就是这些）。
        self.assertEqual(110001, weapondata.roh_of(1000015))   # 리볼버 D1
        self.assertEqual(110001, weapondata.roh_of(1000011))   # 리볼버 SE
        # 商城角色 / 突击技 / 表里根本没有的 id —— 三条都得是 None，不能抛。
        self.assertIsNone(weapondata.roh_of(1100010))          # ch100-01
        self.assertIsNone(weapondata.roh_of(1003010))          # CH03-01
        self.assertIsNone(weapondata.roh_of(999999))

    def test_known_weapon_matches_the_original_ini(self):
        """`ch02-01`（角色 2 的基础枪）—— 拿它当基准，产物格式变了会炸。
        `Damage=3` 和语料里 `rpExplode +24` 的最小值 3.0 正好对上（§43）。"""
        weapon = weapondata.get(1002010)
        self.assertIsNotNone(weapon)
        self.assertEqual(3, weapon.damage)
        self.assertEqual(100.0, weapon.velocity)
        self.assertEqual(0.0, weapon.gravity)
        self.assertEqual(140, weapon.fire_interval_ms)
        self.assertEqual(1, weapon.handle_step)
        self.assertEqual(80.0, weapon.lockon_range)


@unittest.skipIf(not os.path.isfile(weapondata.DATA_PATH),
                 "server/bot_weapons.json 还没生成")
class IreneOnlyFieldsTests(unittest.TestCase):
    """★ `format` 13 新收的两组字段（X_Mod §31 / §32）。

    两组在**全表 228 节里都只有爱琳的武器有** —— 这既是事实也是判据：
    多出来一条就说明提取器的键名或类型写漏了，会把别的武器也带上。
    """

    #: 蝴蝶（爱琳 2 号武器炸出来的碎片）。
    SPLINTER = 1003520
    #: 回血图腾本体。
    TOTEM = 1003031

    def test_the_splinter_carries_the_slow(self):
        """`Attribute=4 / AttributeTime=2100`。★ `4` 不是 `Status.ini` 的
        小节号，是 exe `0x480f4a` 那张映射的 key，换算出来才是属性 14 减速。"""
        weapon = weapondata.get(self.SPLINTER)
        self.assertIsNotNone(weapon)
        self.assertEqual(4, weapon.attribute)
        self.assertEqual(2100, weapon.attribute_ms)

    def test_the_launcher_points_at_the_totem(self):
        launcher = weapondata.get(1003030)
        self.assertIsNotNone(launcher)
        self.assertEqual("TotemLauncher", launcher.creating_class)
        self.assertEqual(self.TOTEM, launcher.totem_id)

    def test_the_totem_carries_all_six_numbers(self):
        """半径 / 时长 / 间隔 / 每跳量少一格，bot 就判不了「进没进圈」。"""
        totem = weapondata.get(self.TOTEM)
        self.assertIsNotNone(totem)
        self.assertEqual(1, totem.totem_type)        # 1 = 只治同队
        self.assertEqual(200.0, totem.totem_range)
        self.assertEqual(3, totem.totem_value)
        self.assertEqual(5000, totem.totem_life_ms)
        self.assertEqual(840, totem.totem_interval_ms)
        self.assertAlmostEqual(1.35, totem.totem_mode_ratio, places=3)

    def test_nobody_else_has_these_fields(self):
        """全表就这三条 —— 多一条就是提取器把别的武器也带上了。"""
        table = json.load(io.open(weapondata.DATA_PATH, encoding="utf-8"))
        attribute, totem = [], []
        for key, record in table["weapons"].items():
            if "attribute" in record or "attribute_ms" in record:
                attribute.append(key)
            if any(name.startswith("totem_") for name in record):
                totem.append(key)
        self.assertEqual([str(self.SPLINTER)], attribute)
        self.assertEqual(["1003030", str(self.TOTEM)], sorted(totem))

    def test_the_loader_and_the_extractor_agree_on_the_format(self):
        """★★ 两侧的 `FORMAT` 对不上 = `_read()` 返回**空表** = 全房间 bot
        当场不开枪，而且一句报错都没有。产物里那个数也得是同一个。"""
        table = json.load(io.open(weapondata.DATA_PATH, encoding="utf-8"))
        self.assertEqual(weapondata.FORMAT, table["format"])
        if TOOL is not None:
            self.assertEqual(weapondata.FORMAT, TOOL.FORMAT)


if __name__ == "__main__":
    unittest.main()
