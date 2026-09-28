"""格斗模式（무투전，对战模式号 2）里的 bot（X16，D84）。

C2（本文件现在的内容）：
* 格斗房里的 bot **不开枪、不换枪**（客户端开火总入口关着，收方却不拦 `rpFire`，X_Mod §119），照样走过去冲刺；
* 挨真人格斗招式的两种反应（`rpSplashDamaged +12` = 2 / 3，X_Mod §122）：类型 2 锁输入 10 帧 + 按 x^(1/10) 曲线
  滑 2·push.x、不改速度；类型 3 打飞、不做 push.y ×2；两类都转向攻击者、不看伤害一律打断招式。
"""
from __future__ import annotations

import contextlib
import os
import struct
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import bot                                                     # noqa: E402
import botfight                                                # noqa: E402
import botmove                                                 # noqa: E402
import botsync                                                 # noqa: E402
import gameserver                                              # noqa: E402
import mutudata                                                # noqa: E402
from test_botmelee import MeleeRoom                            # noqa: E402
from test_botsync import (BotFireRoom, FakeBot, TerrainMixin, body_of, bot_frames,  # noqa: E402
                          dash_frames, fire_frames, header, splash_frames, synth_terrain)

FIGHT = (0, gameserver.PVP_MODE_FIGHT, 0)


def skill_index(character_id, motion):
    """这一招的客户端招式号（`0x0016` 里那个数）—— ini 哈希表的遍历顺序，**不是**文件顺序（X_Mod §127）。
    按节名找（`chNN@<motion>`）：`Jump-P00-K` 那一节的 `Motion` 也是 `MutuJump-P00`。"""
    for skill in mutudata.skills(character_id):
        if skill.name.endswith("@" + motion):
            return skill.index
    raise AssertionError("角色 %s 没有 %s" % (character_id, motion))


def weapon_frames(conn, seat):
    return [f for f in bot_frames(conn, seat)
            if header(f)["opcode"] == botsync.OP_CHANGE_WEAPON]


def mutu_frames(conn, seat, kind=None):
    """bot 发出的 `0x0016`（格斗招式）：`kind` 给了只留出招（2）/ 收招（−1）那一种。"""
    out = []
    for f in bot_frames(conn, seat):
        if header(f)["opcode"] != botsync.OP_MUTU_SKILL:
            continue
        if kind is None or botsync.parse_mutu_skill(body_of(f))[1] == kind:
            out.append(f)
    return out


def guard_frames(conn, seat):
    """bot 发出的 `0x0018`（格挡开 / 关）。"""
    return [f for f in bot_frames(conn, seat) if header(f)["opcode"] == botsync.OP_GUARD]


class SlideCurveTests(unittest.TestCase):
    """`0x4fe24e`：`F(n) = trunc(D · powf(n/10, 1/10))`，γ = `[0x693730]` = 10。"""

    def test_thirty_pixels_go_mostly_in_the_first_frame(self):
        self.assertEqual([23, 2, 1, 1, 0, 1, 0, 1, 0, 1], botfight.slide_steps(30.0))
        self.assertEqual([-23, -2, -1, -1, 0, -1, 0, -1, 0, -1], botfight.slide_steps(-30.0))

    def test_a_suppressed_frame_is_made_up_next_frame(self):
        """按着方向键那一帧不滑，`[+0x550]` 没动 ⇒ 下一帧一口气补上（`F(n′) − F(last)`）。"""
        react = botfight.HitReact(15.0)
        self.assertEqual(23, react.next_frame(0))
        self.assertEqual(0, react.next_frame(1))
        self.assertEqual(3, react.next_frame(0))

    def test_the_lock_is_ten_frames(self):
        react = botfight.HitReact(15.0)
        steps = [react.next_frame(0) for _ in range(12)]
        self.assertEqual([23, 2, 1, 1, 0, 1, 0, 1, 0, 1, 0, 0], steps)
        self.assertFalse(react.locked)

    def test_facing_turns_toward_the_attacker(self):
        self.assertEqual(-1, botfight.facing_after_hit(15.0))
        self.assertEqual(1, botfight.facing_after_hit(-15.0))
        self.assertEqual(1, botfight.facing_after_hit(0.0))


class FightRoomFireTests(BotFireRoom):
    """格斗房：`_decide` 不挑枪 ⇒ 一发 `rpFire`、一发 `rpChangeWeapon` 都没有（普通房同样的走法是会开枪的，见 `BotFireTests`）。"""

    arguments = FIGHT

    def test_it_never_fires_or_switches(self):
        self.assertTrue(bot._fight_mode(self.room))
        self.approach()
        for _ in range(6):
            self.human_heartbeat(self.alice, 140.0, 100.0)
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))
        self.assertEqual([], weapon_frames(self.alice, self.bot_seat))
        self.assertIsNone(self.bot_conn.aim)


class NormalRoomFireControlTests(BotFireRoom):
    """对照组：同一套走法在普通房（夺分）里是开枪的 —— 上面那条不是因为走法本身打不出去。"""

    def test_the_same_approach_fires_in_a_normal_room(self):
        self.approach()
        self.assertTrue(fire_frames(self.alice, self.bot_seat))


class FightRoomDashTests(MeleeRoom):
    """格斗房里 AA/DD 冲刺照样有（原版没有门，X_Mod §119）⇒ bot 还是会走过去冲。"""

    arguments = FIGHT

    def test_it_still_dashes(self):
        swing = self.walk_until_dash()
        self.assertIsNotNone(swing)
        self.assertTrue(dash_frames(self.alice, self.bot_seat))
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))

    def test_the_dash_waits_the_reaction_too(self):
        """格斗房里冲刺也是「出手」：看见够得着之后同样要过 100~200 ms（D91，和 J/K 起手共用一份）。"""
        self.bot_conn.roll_unit = lambda: 0.5             # 150 ms
        seen = None
        for point in ((100.0, 100.0), (120.0, 100.0), (140.0, 100.0), (140.0, 100.0), (140.0, 100.0)):
            self.human_heartbeat(self.alice, *point, ticks=0)
            for _ in range(gameserver.HEARTBEAT_TICKS):
                at = self.now()
                self.advance(1)
                if seen is None and self.bot_conn.opening.seen_tick is not None:
                    seen = at
                if self.bot_conn.dash_swing is not None:
                    self.assertIsNotNone(seen, "冲之前先看见过")
                    self.assertGreaterEqual(at - seen + 1e-6, 0.150)
                    return
        self.fail("走到跟前了 bot 还没冲")

    def test_a_weak_fight_hit_still_stops_its_dash(self):
        """类型 2 / 3 不看伤害一律打断（`0x50a6f8`）；类型 0 要 ≥ 10（`HitBreaksBotDashTests.test_a_light_hit_does_not`）。"""
        swing = self.walk_until_dash()
        self.assertFalse(swing.hit, "前提：还没进伤害段")
        self.peer_event(self.alice, botsync.OP_SPLASH_DAMAGED, botsync.splash_body(
            botsync.projectile_handle(self.alice_seat(), 0),
            botsync.character_handle(self.bot_seat), 3.0, 130.0, 60.0,
            push_x=15.0, push_y=-4.5, kind=botfight.HIT_SLIDE))
        self.assertIsNone(self.bot_conn.dash_swing)


class NormalRoomDashTests(MeleeRoom):
    """对照：普通房的冲刺不走出手反应（D91 只管格斗房）。"""

    def test_it_dashes_without_the_opening(self):
        self.assertIsNotNone(self.walk_until_dash())
        self.assertIsNone(self.bot_conn.opening.seen_tick)


class FightHitRoom(TerrainMixin, BotFireRoom):
    """格斗房 + 够高的平地（地面 400，脚在 399）+ 站在 (600, 399) 的 bot；alice 在左边 x=100 报过一发心跳。"""

    arguments = FIGHT
    FLOOR_Y = 399.0

    def setUp(self):
        super().setUp()
        self.install_terrain(synth_terrain("flat_tall", floor=400, height=440))
        self.place_bot(600.0, self.FLOOR_Y)
        self.alice_seat = self.room.seat_index_of(self.alice)
        self.human_heartbeat(self.alice, 100.0, self.FLOOR_Y, ticks=0)

    def hit(self, damage, push, kind, flags=0):
        """alice 的格斗招式打中 bot（她那台判的，`rpSplashDamaged` 经服务端转发）。"""
        packet = botsync.build_peer_packet(
            self.alice_seat, botsync.OP_SPLASH_DAMAGED, botsync.splash_body(
                botsync.projectile_handle(self.alice_seat, 0),
                botsync.character_handle(self.bot_seat), damage, 600.0, 360.0,
                push_x=push[0], push_y=push[1], flags=flags, kind=kind),
            game_id=self.room.epoch_value, sequence=self.next_seq(self.alice))
        gameserver.Conn.on_game_packet(self.alice, gameserver.OP_PEER_DATA_UP, packet)

    def body(self):
        return self.bot_conn.body


class SlideHitTests(FightHitRoom):
    """类型 2（`0x4ff7a1`）：不改速度、不清踩地位、锁输入 10 帧、滑 2·push.x、转向攻击者。"""

    def test_it_keeps_its_feet_and_speed(self):
        self.hit(3.0, (15.0, -4.5), botfight.HIT_SLIDE)
        body = self.body()
        self.assertTrue(body.on_ground)
        self.assertEqual((600.0, 0.0, 0.0), (body.x, body.vx, body.vy))
        self.assertEqual(-1, self.bot_conn.heading, "被往右推 ⇒ 转向左边的攻击者")

    def test_it_slides_thirty_pixels_along_the_curve_without_walking(self):
        self.hit(3.0, (15.0, -4.5), botfight.HIT_SLIDE)
        xs, keys = [], []
        for _ in range(10):
            self.advance(1)
            xs.append(self.body().x - 600.0)
            keys.append(self.bot_conn.press_dir)
        self.assertEqual([23, 25, 26, 27, 27, 28, 28, 29, 29, 30], [int(x) for x in xs])
        self.assertEqual([0] * 10, keys, "锁输入那 10 帧一个键都不按")
        self.advance(1)
        self.assertIsNone(self.bot_conn.fight_react)
        self.assertEqual(-1, self.bot_conn.press_dir, "锁一松就接着朝左边的 alice 走")

    def test_a_later_knockback_restarts_the_slide_clock(self):
        """原版怪癖：滑到第 3 帧又挨了一下别的击退，`[+0x17c]` 重起、`[+0x550]` 不动 ⇒ 下一帧往回滑 F(1) − F(3)。"""
        self.hit(3.0, (15.0, -4.5), botfight.HIT_SLIDE)
        self.advance(3)
        self.assertEqual(626.0, self.body().x)
        self.hit(5.0, (4.0, -1.0), botfight.HIT_PLAIN)       # 乙档：当场滑 push.x × 3 = 12
        self.assertEqual(638.0, self.body().x)
        self.advance(1)
        self.assertEqual(635.0, self.body().x, "F(1) − F(3) = 23 − 26")

    def strikes(self):
        """它出过的手：冲刺 `rpDash` + 格斗招式 `0x0016`（C3 之后贴脸先出 J / K）。"""
        return dash_frames(self.alice, self.bot_seat) + mutu_frames(self.alice, self.bot_seat)

    def test_it_does_not_strike_while_locked(self):
        self.bot_conn.melee = True
        self.hit(3.0, (15.0, -4.5), botfight.HIT_SLIDE)
        self.human_heartbeat(self.alice, 660.0, self.FLOOR_Y, ticks=0)   # 贴到它跟前
        self.clear()
        self.advance(10)
        self.assertEqual([], self.strikes(), "锁输入 + `[+0x17c]` 那 10 帧：不冲、不出招")
        self.advance(10)
        self.assertTrue(self.strikes(), "锁一松就出手")

    def test_the_same_spot_strikes_at_once_without_the_hit(self):
        """对照组：同一个站位不挨打，10 帧之内就出手了 —— 上面那条不是因为这儿本来就打不着。"""
        self.bot_conn.melee = True
        self.human_heartbeat(self.alice, 660.0, self.FLOOR_Y, ticks=0)
        self.clear()
        self.advance(10)
        self.assertTrue(self.strikes())


class FlyHitTests(FightHitRoom):
    """类型 3（`0x4ff872`）：一律甲档、置打飞；push.y 不 ×2（那一步只给类型 0）。"""

    def test_a_heavy_one_launches_it_with_the_raw_push(self):
        self.hit(20.0, (15.0, -30.0), botfight.HIT_FLY)
        body = self.body()
        self.assertFalse(body.on_ground)
        self.assertEqual((15.0, -30.0), (body.vx, body.vy))
        self.assertTrue(body.fly)
        self.assertEqual(-1, self.bot_conn.heading)
        self.assertIsNone(self.bot_conn.fight_react, "类型 3 不锁输入")

    def test_ten_or_less_only_clamps_but_still_flies(self):
        """伤害 ≤ 10：甲档只把 v.y 夹到 −10（在地上且 v.x == 0，`0x50f8a2`），照样离地、打飞。"""
        self.hit(8.0, (15.0, -12.0), botfight.HIT_FLY)
        body = self.body()
        self.assertEqual((0.0, bot.KNOCKBACK_MIN_LIFT), (body.vx, body.vy))
        self.assertFalse(body.on_ground)
        self.assertTrue(body.fly)

    def test_its_flight_falls_with_the_fight_fly_gravity(self):
        self.hit(20.0, (15.0, -30.0), botfight.HIT_FLY)
        self.advance(1)
        self.assertEqual(botmove._f32(-30.0 + botmove.fight_physics().fly_step), self.body().vy)


class _FakeSkill(object):
    """`mutudata.Skill` 的最小替身：9 个逻辑帧、第 2 / 3 帧挪 7 / 3、Damager 在 k = 3..7（泰尔 P00 的样子）。"""

    name = "假招式"
    frames_total = 9
    move = {2: 7, 3: 3}

    class _Damager(object):
        points = {k: (2 * k, 30.0, -40.0) for k in range(3, 8)}

    damagers = [_Damager()]


class HumanSkillTests(unittest.TestCase):
    """`botfight.HumanSkill`：按他网格上的帧号数招式第几帧、顿帧、Move、Damager 窗口、结束帧。"""

    def setUp(self):
        self.skill = botfight.HumanSkill(_FakeSkill(), 1, (300.0, 399.0), 10.0)

    def test_frame_zero_is_the_frame_after_the_packet(self):
        """✅ B2 实机（X_Mod §127）：发 `0x0016` 之后第 1 帧就是招式第 0 帧（C2 当初照冲刺推成第 2 帧）。"""
        self.assertIsNone(self.skill.frame(0))
        self.assertEqual([0, 1, 2], [self.skill.frame(r) for r in (1, 2, 3)])
        self.assertEqual(8, self.skill.frame(9))
        self.assertIsNone(self.skill.frame(10), "9 帧播完")
        self.assertEqual(10, self.skill.end_raw)

    def test_move_follows_the_track_and_the_facing(self):
        self.assertEqual([0, 0, 7, 3, 0], [self.skill.move_at(r) for r in range(1, 6)])
        left = botfight.HumanSkill(_FakeSkill(), -1, (0.0, 0.0), 10.0)
        self.assertEqual(-7, left.move_at(3))

    def test_the_damager_window(self):
        self.assertEqual([False, False, False, True, True, True, True, True, False],
                         [self.skill.damager_live(r) for r in range(1, 10)])

    def test_a_hitstop_freezes_four_frames_and_pushes_the_end(self):
        """打中在 raw 5 那一格 ⇒ raw 6..9 顿住（✅ B2：之后第 1 帧起，X_Mod §127）：k 停在打中那一帧的 4、不挪、
        Damager 照样在；结束帧往后挪 4。"""
        self.skill.add_hitstop(10.0 + 5 * botfight.FRAME_S)
        self.assertEqual([0, 1, 2, 3, 4, 4, 4, 4, 4, 5, 6, 7, 8, None],
                         [self.skill.frame(r) for r in range(1, 15)])
        self.assertEqual([0, 0, 7, 3, 0, 0, 0, 0, 0], [self.skill.move_at(r) for r in range(1, 10)])
        self.assertTrue(self.skill.damager_live(7), "顿着的时候判定体原地不动、照样在")
        self.assertEqual(14, self.skill.end_raw)
        self.assertAlmostEqual(10.0 + 14 * botfight.FRAME_S, self.skill.end_time())

    def test_it_holds_the_keys_from_the_frame_after_the_press(self):
        """发出那一帧已经走过了；下一帧起键缓冲非空、接着招式对象在，一直到播完。"""
        self.assertEqual([False, True, True, True, False],
                         [self.skill.holds(r) for r in (0, 1, 2, 9, 10)])

    def test_raw_at_rounds_onto_his_grid(self):
        self.assertEqual(0, self.skill.raw_at(10.0))
        self.assertEqual(3, self.skill.raw_at(10.0 + 3 * botfight.FRAME_S + 0.010))


class HumanSkillRoom(TerrainMixin, MeleeRoom):
    """格斗房 + 高平地（脚在 399）；alice 是泰尔（角色 0），站在 (300, 399)；bot 摆远、站住、不开枪。"""

    arguments = FIGHT
    FLOOR_Y = 399.0

    def setUp(self):
        super().setUp()
        self.install_terrain(synth_terrain("flat_tall", floor=400, height=440))
        self.room.seats[self.alice_seat()].character_id = 0
        self.place_bot(1300.0, self.FLOOR_Y)
        self.bot_conn.holding = True
        self.bot_conn.next_fire_at = 1e18
        self.human_heartbeat(self.alice, 300.0, self.FLOOR_Y, ticks=0)

    def skill(self, motion="MutuStand-P00", facing=1, x=300.0, y=None, kind=botsync.MUTU_SKILL_START):
        self.peer_event(self.alice, botsync.OP_MUTU_SKILL, botsync.mutu_skill_body(
            self.alice_seat(), kind, facing, skill_index(0, motion), x, self.FLOOR_Y if y is None else y))

    def xs(self, ticks):
        out = []
        for _ in range(ticks):
            self.advance(1)
            out.append(self.alice.sim_body.x)
        return out


class HumanSkillExtrapolationTests(HumanSkillRoom):
    """外推真人：招式那一段不按键走路、按 Move 曲线挪（P00：第 2 帧 7 px、第 3 帧 3 px）。"""

    def test_the_packet_index_is_the_client_order(self):
        """泰尔发 6 号 = `Stand-P00`（文件里第 0 节）—— 按文件顺序认的话 6 号是 `Jump-P00`（X_Mod §127）。"""
        self.assertEqual(6, skill_index(0, "MutuStand-P00"))
        self.skill()
        self.assertEqual("MutuStand-P00", self.alice.mutu_skill.skill.motion)

    def test_it_moves_along_the_track(self):
        self.skill()
        self.assertIsNotNone(self.alice.mutu_skill)
        # 第 1 格硬置成心跳；之后 raw 1(k0), 2(k1), 3(k2 +7), 4(k3 +3), 5 …（✅ B2：第 0 帧 = 发出后第 1 帧）
        self.assertEqual([300.0, 300.0, 300.0, 307.0, 310.0, 310.0, 310.0], self.xs(7))

    def test_it_does_not_walk_with_the_heartbeat_keys(self):
        state = botsync.character_state(300.0, self.FLOOR_Y, keys=botsync.KEY_RIGHT)
        packet = botsync.build_peer_packet(self.alice_seat(), botsync.OP_HEARTBEAT,
                                           botsync.heartbeat_body(0, self.alice_seat(), state),
                                           game_id=self.room.epoch_value)
        gameserver.Conn.on_game_packet(self.alice, gameserver.OP_PEER_DATA_UP, packet)
        self.skill()
        self.assertEqual([300.0, 300.0, 300.0, 307.0, 310.0, 310.0], self.xs(6)[:6])

    def test_frame_zero_places_him_at_the_packet_coordinates(self):
        self.skill(x=320.0)
        self.assertEqual(320.0, self.xs(2)[-1], "第 0 帧之前收方硬置到包里的 (x, y)（`0x4935a7`）")

    def test_a_retract_packet_ends_it(self):
        self.skill()
        self.skill(kind=botsync.MUTU_SKILL_END)
        self.assertIsNone(self.alice.mutu_skill)
        self.assertEqual([300.0] * 6, self.xs(6))


class HumanSkillPriorityTests(HumanSkillRoom):
    """bot 冲刺碰到正在出招的他：只有活着的 Damager 在（k = 3..7）优先级才是 3（`0x4f88e6`，X_Mod §121）。"""

    def test_priority_is_three_only_inside_the_damage_window(self):
        self.skill()
        skill = self.alice.mutu_skill
        at = lambda raw: skill.grid + raw * botfight.FRAME_S          # noqa: E731
        seat = self.alice_seat()
        ranks = [bot._melee_priority(self.room, seat, at(r)) for r in range(2, 12)]
        self.assertEqual([0, 0, 3, 3, 3, 3, 3, 0, 0, 0], ranks)       # raw = k + 1（X_Mod §127）

    def test_the_bot_dash_is_voided_inside_the_window(self):
        self.skill()
        skill = self.alice.mutu_skill
        swing = type("Swing", (), {"outranked": set(), "handle": 0})()
        inside = skill.grid + 6 * botfight.FRAME_S
        outside = skill.grid + 3 * botfight.FRAME_S
        self.assertTrue(bot._outranks_dash(self.room, self.bot_conn, swing, self.alice_seat(), inside))
        self.assertFalse(bot._outranks_dash(self.room, self.bot_conn, swing, self.alice_seat(), outside))


class HumanSkillPushTests(HumanSkillRoom):
    """格斗招式的推挤体整招全程发 `0x0017`：`_pushed_by` 在招式播完之前一直认这个约束。"""

    def test_the_push_lasts_the_whole_skill(self):
        self.skill("MutuStand-K01")       # 28 帧
        self.alice_pushes_bot()
        now = self.now()
        self.assertEqual(self.alice_seat(), bot._pushed_by(self.room, self.bot_conn, now))
        end = self.alice.mutu_skill.end_time()
        self.assertEqual(self.alice_seat(), bot._pushed_by(self.room, self.bot_conn, end - 0.001))
        self.assertIsNone(bot._pushed_by(self.room, self.bot_conn, end + 0.001))


class HumanSkillEventTests(HumanSkillRoom):
    """顿帧 / 被打断 / 死了。"""

    def test_his_hit_adds_a_hitstop(self):
        self.skill()
        end = self.alice.mutu_skill.end_time()
        self.peer_event(self.alice, botsync.OP_SPLASH_DAMAGED, botsync.splash_body(
            botsync.projectile_handle(self.alice_seat(), 0),
            botsync.character_handle(self.bot_seat), 3.0, 1300.0, 360.0,
            push_x=15.0, push_y=-4.5, kind=botfight.HIT_SLIDE))
        self.assertEqual(botfight.HITSTOP_FRAMES, len(self.alice.mutu_skill.stops))
        self.assertAlmostEqual(end + botfight.HITSTOP_FRAMES * botfight.FRAME_S,
                               self.alice.motion_action[1])

    def test_a_heavy_hit_on_him_cancels_it(self):
        self.skill()
        bot._hit_breaks_melee(self.room, self.alice_seat(), 23.0, 0, "单测", now=self.now())
        self.assertIsNone(self.alice.mutu_skill)

    def test_lying_down_clears_it(self):
        self.skill()
        self.room.quest.respawn_due[self.alice_seat()] = (self.now() + 60.0, (100, 100))
        self.addCleanup(self.room.quest.respawn_due.pop, self.alice_seat(), None)
        self.advance(1)
        self.assertIsNone(self.alice.mutu_skill)

    def test_a_new_match_clears_it(self):
        self.skill()
        gameserver.reset_sync_trails(self.room, "单测", new_match=True)
        self.assertIsNone(self.alice.mutu_skill)


class HumanVictimTests(MeleeRoom):
    """真人挨格斗招式（服务端转发时看到的）：类型 2 / 3 一律打断他的招，只有类型 3 记打飞。"""

    arguments = FIGHT

    def test_a_slide_hit_breaks_his_dash_but_does_not_fly(self):
        self.alice_dashes()
        self.assertIsNotNone(getattr(self.alice, "motion_action", None))
        bot._hit_breaks_melee(self.room, self.alice_seat(), 3.0, 0, "单测",
                              now=self.now(), kind=botfight.HIT_SLIDE)
        self.assertFalse(bot._human_dash_now(self.room, self.alice_seat(), self.alice, self.now()))
        self.assertFalse(getattr(self.alice, "sim_fly_pending", False))

    def test_a_fly_hit_marks_him_flying(self):
        bot._hit_breaks_melee(self.room, self.alice_seat(), 3.0, 0, "单测",
                              now=self.now(), kind=botfight.HIT_FLY)
        self.assertTrue(getattr(self.alice, "sim_fly_pending", False))

    def test_a_guarded_hit_does_neither(self):
        bot._hit_breaks_melee(self.room, self.alice_seat(), 20.0, bot.EXPLODE_FLAG_GUARD, "单测",
                              now=self.now(), kind=botfight.HIT_FLY)
        self.assertFalse(getattr(self.alice, "sim_fly_pending", False))


# ===========================================================================
# C3：bot 自己出招 / 格挡（X_Mod §121 / §122 / §127，D84 / D88）
# ===========================================================================
def named(character_id, motion):
    """按节名找一招（`chNN@<motion>`）。"""
    return mutudata.skills(character_id)[skill_index(character_id, motion)]


class BotSkillTests(unittest.TestCase):
    """`botfight.BotSkill`：发包那一格 k = −1、下一格第 0 帧；顿帧 k 不走、计时器照走；接招窗口按计时器。"""

    def setUp(self):
        self.jab = named(0, "MutuStand-P00")           # 9 帧、计时器 266 ms、Move {2: 7, 3: 3}

    def test_frame_zero_is_the_tick_after_the_packet(self):
        skill = botfight.BotSkill(self.jab, 1, 300002)
        self.assertFalse(skill.started)
        self.assertEqual([0, 1, 2, 3], [skill.tick() and skill.k for _ in range(4)])

    def test_it_finishes_after_frames_total(self):
        skill = botfight.BotSkill(self.jab, 1, 300002)
        for _ in range(self.jab.frames_total):
            self.assertTrue(skill.tick())
        self.assertFalse(skill.finished)
        self.assertFalse(skill.tick(), "第 frames_total 格：动画时刻 ≥ D，播完")
        self.assertTrue(skill.finished)

    def test_a_hitstop_freezes_four_ticks_but_not_the_timer(self):
        skill = botfight.BotSkill(self.jab, 1, 300002)
        for _ in range(4):
            skill.tick()                                 # k = 3（打中那一格）
        skill.add_hitstop()
        frozen = [skill.tick() for _ in range(4)]
        self.assertEqual([False] * 4, frozen)
        self.assertEqual(3, skill.k)
        self.assertEqual(0, skill.move_step(), "顿着不挪")
        self.assertEqual(7, skill.elapsed, "计时器照走（逻辑帧计时器，`0x5d5e37`）")
        self.assertTrue(skill.tick())
        self.assertEqual(4, skill.k)

    def test_move_steps_follow_the_curve_and_the_facing(self):
        right = botfight.BotSkill(self.jab, 1, 0)
        left = botfight.BotSkill(self.jab, -1, 0)
        steps_r, steps_l = [], []
        for _ in range(5):
            right.tick()
            left.tick()
            steps_r.append(right.move_step())
            steps_l.append(left.move_step())
        self.assertEqual([0, 0, 7, 3, 0], steps_r)
        self.assertEqual([0, 0, -7, -3, 0], steps_l)

    def test_damagers_are_mirrored_when_facing_left(self):
        right = botfight.BotSkill(self.jab, 1, 0)
        left = botfight.BotSkill(self.jab, -1, 0)
        for _ in range(4):
            right.tick()
            left.tick()
        (i, dx, dy, r), = right.damagers()
        (_, ldx, ldy, _), = left.damagers()
        self.assertEqual((0, 18.0), (i, r))
        self.assertGreater(dx, 20.0)
        self.assertEqual((-dx, dy), (ldx, ldy))

    def test_the_combo_window_opens_with_300ms_left(self):
        """刺拳计时器 266 ms ⇒ 一出手窗口就开着；泰尔 K01 计时器 933 ms ⇒ 走过 (933 − 300) / 32 = 19.8 格才开。"""
        jab = botfight.BotSkill(self.jab, 1, 0)
        self.assertFalse(jab.window_open(), "还没走第 0 帧")
        jab.tick()
        self.assertTrue(jab.window_open())
        heavy = named(0, "MutuStand-K01")
        skill = botfight.BotSkill(heavy, 1, 0)
        opened = None
        for n in range(heavy.frames_total):
            skill.tick()
            if skill.window_open():
                opened = skill.elapsed
                break
        self.assertEqual(int(-(-(heavy.timer_ms - botfight.COMBO_WINDOW_MS) // botfight.FRAME_MS)), opened)

    def test_the_window_counts_the_hitstop(self):
        """计时器是逻辑帧计时器、顿帧照走（`0x5d5e37`）：顿过 4 格的重拳，窗口比按动画帧算早开 4 帧。"""
        heavy = named(0, "MutuStand-K01")
        opens_at = int(-(-(heavy.timer_ms - botfight.COMBO_WINDOW_MS) // botfight.FRAME_MS))
        skill = botfight.BotSkill(heavy, 1, 0)
        for _ in range(6):
            skill.tick()
        skill.add_hitstop()
        while not skill.window_open():
            skill.tick()
        self.assertEqual(opens_at, skill.elapsed)
        self.assertEqual(opens_at - botfight.HITSTOP_FRAMES, skill.k)

    def test_it_owns_its_handles(self):
        heavy = named(0, "MutuStand-K01")                # 1 判定体 + 1 受击体 + 推挤体 = 3 个
        skill = botfight.BotSkill(heavy, 1, 300010)
        self.assertEqual(3, heavy.handles)
        self.assertEqual([False, True, True, True, False],
                         [skill.owns(h) for h in (300009, 300010, 300011, 300012, 300013)])
        self.assertEqual(300010, skill.damager_handle(0))


class PressTests(unittest.TestCase):
    """`botfight.press`：按一个键客户端出哪一招（`0x495ca1`：接续招得分高，同分取招式号小的）。"""

    def test_idle_keys(self):
        skills = mutudata.skills(0)
        self.assertEqual("ch00@MutuStand-P00", botfight.press(skills, "P", mutudata.STAND).name)
        self.assertEqual("ch00@MutuStand-K01", botfight.press(skills, "K", mutudata.STAND).name)
        self.assertEqual("ch00@MutuCrouch-P00", botfight.press(skills, "P", mutudata.CROUCH).name)
        self.assertEqual("ch00@MutuJump-P00", botfight.press(skills, "P", mutudata.AIR).name)
        self.assertEqual("ch00@MutuJump-P00-K", botfight.press(skills, "K", mutudata.AIR).name)

    def test_the_link_wins_inside_a_skill(self):
        skills = mutudata.skills(0)
        jab = named(0, "MutuStand-P00")
        self.assertEqual("ch00@MutuStand-P01", botfight.press(skills, "P", mutudata.STAND, jab).name,
                         "刺拳里再按 J 是二连，不会又出一记刺拳")
        self.assertEqual("ch00@MutuStand-K01", botfight.press(skills, "K", mutudata.STAND, jab).name,
                         "起手招任何时候都能排（`0x495cb5`）")
        heavy = named(0, "MutuStand-K01")
        self.assertEqual("ch00@MutuStand-K02", botfight.press(skills, "K", mutudata.STAND, heavy).name)


class FirstHitTests(unittest.TestCase):
    """`botfight.first_hit`：够不够得着（假设对方不动）；推挤体一碰到就把人约束在身前（`0x50e654`）。"""

    CIRCLES = [(600.0, 321.0, 10.0), (600.0, 349.0, 18.0), (600.0, 383.0, 16.0)]   # 站在 (600, 399) 的泰尔

    def test_a_jab_reaches_him_right_in_front_and_not_behind(self):
        jab = named(0, "MutuStand-P00")
        self.assertIsNotNone(botfight.first_hit(jab, 1, 560.0, 399.0, self.CIRCLES))
        self.assertIsNone(botfight.first_hit(jab, -1, 560.0, 399.0, self.CIRCLES), "朝反方向出拳")
        self.assertIsNone(botfight.first_hit(jab, 1, 400.0, 399.0, self.CIRCLES), "隔 200 px 够不着")

    def test_a_lunge_needs_the_push_to_land(self):
        """泰尔 K01 先冲 90 px 再踢：从 660 冲到 570 已经越过站在 600 的人 —— 推挤体把他推在身前，踢才打得到。"""
        heavy = named(0, "MutuStand-K01")
        who = bot.chrprops.get(0)
        self.assertIsNone(botfight.first_hit(heavy, -1, 660.0, 399.0, self.CIRCLES))
        self.assertIsNotNone(botfight.first_hit(heavy, -1, 660.0, 399.0, self.CIRCLES,
                                                push=botfight.push_offset(who)))

    def test_applied_skips_the_step_already_taken(self):
        heavy = named(0, "MutuStand-K01")
        # 第 4 帧那 57 px 已经走过了（外推真人先于 bot）：从 643 起再算一遍第 4 帧的话会多挪 57、踢空。
        self.assertIsNotNone(botfight.first_hit(heavy, -1, 643.0, 399.0, self.CIRCLES, from_k=4,
                                                applied=True))


class GuardStateTests(unittest.TestCase):
    """`botfight.GuardState`：别的机器判它在挡 = 开关 XOR 过渡计时器在跑（3 帧，`0x50a0ea`）。"""

    def test_raising_takes_three_frames(self):
        guard = botfight.GuardState()
        self.assertTrue(guard.set(True))
        effective = []
        for _ in range(4):
            effective.append(guard.effective)
            guard.tick()
        self.assertEqual([False, False, False, True], effective)
        self.assertFalse(guard.set(True), "没翻就不发")

    def test_dropping_by_a_skill_is_immediate(self):
        guard = botfight.GuardState()
        guard.set(True)
        for _ in range(3):
            guard.tick()
        guard.drop()
        self.assertEqual((False, False), (guard.on, guard.effective))


class MutuPacketTests(unittest.TestCase):
    def test_a_skill_eats_d_plus_e_plus_one_handles(self):
        stream = botsync.BotSyncStream(FakeBot(seat=1))
        stream.projectiles = 5
        packet, base = stream.mutu_skill(botsync.MUTU_SKILL_START, 1, 4, 600.0, 399.0, handles=3)
        self.assertEqual(botsync.projectile_handle(1, 5), base)
        self.assertEqual(8, stream.projectiles)
        self.assertEqual((1, 2, 1, 4, 600.0, 399.0), botsync.parse_mutu_skill(body_of(packet)))
        _packet, _base = stream.mutu_skill(botsync.MUTU_SKILL_END, 1, 0, 600.0, 399.0)
        self.assertEqual(8, stream.projectiles, "收招不吃句柄")

    def test_guard_body(self):
        self.assertEqual(b"\x01\x01", botsync.guard_body(1, True))
        self.assertEqual(b"\x02\x00", botsync.guard_body(2, False))


class BotStrikeRoom(FightHitRoom):
    """格斗房：bot 和 alice 都是泰尔（角色 0）；bot 的冲刺关掉（只看 J / K），骰子掷 0.99（不走「随便按一个」那一支）。"""

    def setUp(self):
        super().setUp()
        self.room.seats[self.bot_seat].character_id = 0
        self.room.seats[self.alice_seat].character_id = 0
        self.bot_conn.character_id = 0
        self.bot_conn.melee = False
        self.bot_conn.next_fire_at = 1e18
        self.bot_conn.roll_unit = lambda: 0.99

    @contextlib.contextmanager
    def sim_clock(self):
        """alice 的包按**房间的模拟时钟**到达：单测里挂钟几乎不走、模拟时钟一格 32 ms 地跑，不对齐的话她的招在网格上的
        时刻（`sync_event_grid`，按到达的 `time.monotonic()` 记）和 bot 的「此刻」差出好几格 —— 机器越慢差越多（3.8 上 10 格），
        反应时间从她出手那一刻算，就会被这个差推迟到判定窗过完。实机两边是同一个单调时钟，没有这回事。"""
        now = self.now()
        with bot._tick_clock(now), mock.patch.object(gameserver.time, "monotonic", lambda: now):
            yield

    def alice_at(self, x, ticks=0, **extra):
        with self.sim_clock():
            self.human_heartbeat(self.alice, x, self.FLOOR_Y, ticks=0, **extra)
        if ticks:
            self.advance(ticks)

    def starts(self):
        return [botsync.parse_mutu_skill(body_of(f))
                for f in mutu_frames(self.alice, self.bot_seat, botsync.MUTU_SKILL_START)]

    def until(self, predicate, limit=60):
        for _ in range(limit):
            self.advance(1)
            if predicate():
                return True
        return False

    def alice_sends(self, opcode, body):
        packet = botsync.build_peer_packet(self.alice_seat, opcode, body,
                                           game_id=self.room.epoch_value,
                                           sequence=self.next_seq(self.alice))
        with self.sim_clock():
            gameserver.Conn.on_game_packet(self.alice, gameserver.OP_PEER_DATA_UP, packet)


#: 等 bot 起手最多推几格：看见机会之后要过 100~200 ms 的出手反应（D91，`BotStrikeRoom` 的骰子 0.99 ⇒ 199 ms ≈ 7 格），再留余量。
STRIKE_WAIT_TICKS = 12


class BotStrikeTests(BotStrikeRoom):
    """够得着就出招：`0x0016` 字段、句柄、第 0 帧、Move、判中发 `rpSplashDamaged`、顿帧、一人一次。"""

    def setUp(self):
        super().setUp()
        self.alice_at(640.0)
        self.alice.sent.clear()
        self.assertTrue(self.until(lambda: self.bot_conn.fight_skill is not None, STRIKE_WAIT_TICKS),
                        "前提：贴脸就出招（过了反应时间）")
        self.skill = self.bot_conn.fight_skill

    def test_the_packet_is_a_jab_toward_him_from_where_it_stands(self):
        (seat, kind, facing, index, x, y), = self.starts()
        self.assertEqual((self.bot_seat, botsync.MUTU_SKILL_START, 1), (seat, kind, facing))
        self.assertEqual(skill_index(0, "MutuStand-P00"), index, "离他 40 px：最快打到的是刺拳")
        self.assertEqual((600.0, self.FLOOR_Y), (x, y))

    def test_it_ate_the_handles(self):
        self.assertEqual(botsync.projectile_handle(self.bot_seat, 0), self.skill.base)
        self.assertEqual(self.skill.skill.handles, self.bot_conn.sync.projectiles)

    def test_frame_zero_is_the_next_tick_and_it_moves_by_the_curve(self):
        self.assertEqual(-1, self.skill.k, "发包那一格还没走")
        xs = []
        for _ in range(4):
            self.advance(1)
            xs.append(self.bot_conn.body.x)
        self.assertEqual([600.0, 600.0, 607.0, 610.0], xs)
        self.assertEqual(0, self.bot_conn.press_dir, "出招中不按方向键")

    def test_the_heartbeat_faces_the_skill(self):
        self.bot_conn.heading = -1
        self.assertEqual((1, None), bot._heartbeat_facing(self.bot_conn, (0.0, 0.0)))

    def test_a_hit_is_a_type_2_splash_from_its_damager_and_it_freezes(self):
        self.assertTrue(self.until(lambda: splash_frames(self.alice, self.bot_seat), 12))
        body = body_of(splash_frames(self.alice, self.bot_seat)[0])
        src, target, damage, kind, px, py = __import__("struct").unpack_from("<iifBff", body, 0)
        self.assertEqual(self.skill.base, src, "源 = 判定体 0 的句柄")
        self.assertEqual(botsync.character_handle(self.alice_seat), target)
        self.assertEqual(botfight.HIT_SLIDE, kind)
        self.assertEqual(botfight.hit_push(self.skill.skill.damage, 1, mutudata.config()), (px, py))
        k = self.skill.k
        for _ in range(botfight.HITSTOP_FRAMES):
            self.advance(1)
            self.assertEqual(k, self.skill.k, "之后 4 格顿住")
        self.advance(1)
        self.assertEqual(k + 1, self.skill.k)

    def test_one_hit_per_person_per_skill(self):
        for _ in range(self.skill.skill.frames_total + 6):
            self.advance(1)
        mine = [f for f in splash_frames(self.alice, self.bot_seat)
                if self.skill.owns(__import__("struct").unpack_from("<i", body_of(f), 0)[0])]
        self.assertEqual(1, len(mine))

    def test_its_damagers_rank_three(self):
        self.assertTrue(self.until(lambda: self.skill.live_damagers, 10))
        self.assertEqual(botfight.MUTU_PRIORITY, bot._melee_priority(self.room, self.bot_seat, self.now()))

    def test_the_combo_goes_on_to_the_link_right_after_the_last_frame(self):
        jab = self.skill
        last = None
        for _ in range(40):
            self.advance(1)
            if jab.k == jab.skill.frames_total - 1:
                last = self.loop().done
            if len(self.starts()) >= 2:
                break
        self.assertEqual(2, len(self.starts()))
        self.assertEqual(skill_index(0, "MutuStand-P01"), self.starts()[1][3], "窗口里按 J = 二连")
        self.assertEqual(last + 1, self.loop().done, "最后一帧的下一格就出（`0x49597e`）")

    def test_presses_are_at_least_100ms_apart(self):
        """D88（用户定的）：两次 J / K「按键」—— 起手、排下一招 —— 至少隔 100 ms。刺拳一出手窗口就开着，不加会下一格就排。"""
        presses = [self.bot_conn.fight_press_at]
        for _ in range(80):
            self.advance(1)
            if self.bot_conn.fight_press_at != presses[-1]:
                presses.append(self.bot_conn.fight_press_at)
        self.assertGreaterEqual(len(presses), 3)
        gaps = [b - a for a, b in zip(presses, presses[1:])]
        self.assertTrue(all(g >= botfight.BOT_PRESS_GAP_S - 1e-9 for g in gaps), gaps)

    def test_out_of_reach_it_retracts(self):
        self.bot_conn.fight_press_at = self.now()        # 这一格排不了
        self.alice_at(1100.0)                           # 他跳开了
        for _ in range(self.skill.skill.frames_total + 4):
            self.advance(1)
        self.assertEqual(1, len(self.starts()), "够不着不排下一招")
        self.assertEqual(1, len(mutu_frames(self.alice, self.bot_seat, botsync.MUTU_SKILL_END)),
                         "播完没排 ⇒ 发收招（−1）")
        self.assertIsNone(self.bot_conn.fight_skill)

    def test_a_hit_on_it_cancels_the_skill_and_owes_one_retract(self):
        self.hit(3.0, (-15.0, -4.5), botfight.HIT_SLIDE)
        self.assertIsNone(self.bot_conn.fight_skill)
        self.assertEqual([], mutu_frames(self.alice, self.bot_seat, botsync.MUTU_SKILL_END),
                         "打断那一下不发（原版 `0x50a6f8` 只删本地）")
        self.advance(20)
        # ★ 会话 56 改口径（X_Mod §139 / D98）：以前钉的是「不补收招」—— 先收到打中、后收到出招包的那台会一直留着这一招。
        self.assertEqual(1, len(mutu_frames(self.alice, self.bot_seat, botsync.MUTU_SKILL_END)),
                         "挨打那 10 帧走完补一发收招（`BotRetractOwedTests`）")


class BotStrikeSpacingTests(BotStrikeRoom):
    """用户 2026-09-27：「bot 会一直走到和我的角色完全重叠的位置，然后就不动了」。"""

    def test_it_strikes_from_where_it_first_reaches_instead_of_walking_into_him(self):
        self.place_bot(300.0, self.FLOOR_Y)
        self.alice.sent.clear()
        for _ in range(120):
            self.alice_at(600.0)
            self.advance(1)
            if self.starts():
                break
        self.assertTrue(self.starts(), "走过去之后出手了")
        self.assertGreaterEqual(600.0 - self.bot_conn.body.x, 40.0, "够得着就出手，没有贴进他身上")

    def test_standing_in_reach_it_does_not_walk(self):
        self.bot_conn.fight_press_at = 1e18               # 出不了手（只看走位）
        self.alice_at(640.0)
        self.advance(8)
        self.assertEqual(600.0, self.bot_conn.body.x, "够得着就站住，不再往他身上走")

    def test_hold_keeps_it_quiet(self):
        """`/hold`（房主让它站住）：不冲，也不出招。"""
        self.bot_conn.holding = True
        self.alice_at(640.0)
        self.alice.sent.clear()
        self.advance(10)
        self.assertEqual([], self.starts())

    def test_with_no_stamina_it_still_fights(self):
        """冲刺把体力打空也不会贴着人干等（会话 48 查的根因）：格斗招式不花体力。"""
        self.bot_conn.melee = True
        self.bot_conn.stamina = 0.0
        self.alice_at(605.0)                             # 几乎重叠
        self.alice.sent.clear()
        self.assertTrue(self.until(lambda: self.starts(), STRIKE_WAIT_TICKS))


class FightActRoom(BotStrikeRoom):
    """直接问一格 `_fight_act` / `_advance_fight`（不推房间循环），好把「这一格」的某一道门单拎出来看。"""

    AIR_Y = 330.0

    def clock(self):
        return bot._tick_clock(self.now())

    def act(self, turned=False):
        """这一格按什么（`_fight_act`，格号 = 房间下一格）。发了包返回 True。"""
        with self.clock():
            return bot._fight_act(self.room, self.bot_conn, self.bot_seat, self.now(), True, False, False,
                                  bot._terrain(self.room), tick=self.loop().done, turned=turned)

    def ready_opening(self):
        """出手反应早就过了（上一格就看见了、到点了）—— 只看别的门。"""
        self.bot_conn.opening.seen_tick = self.loop().done - 1
        self.bot_conn.opening.ready_at = 0.0

    def advance_fight(self):
        with self.clock():
            bot._advance_fight(self.room, self.bot_conn, self.bot_seat, self.now())

    def start(self, motion, facing=1):
        with self.clock():
            bot._start_fight_skill(self.room, self.bot_conn, self.bot_seat, named(0, motion), facing,
                                   self.now(), "单测")
        return self.bot_conn.fight_skill

    def take_off(self, x=600.0):
        """把 bot 摆到半空（往上飞一点点），`_advance_fight` 这一格就认它腾空了。"""
        self.bot_conn.body = botmove.Body(x, self.AIR_Y, 0.0, -2.0, on_ground=False)
        self.bot_conn.battle_pos = (x, self.AIR_Y)

    def alice_in_air(self, x):
        with self.sim_clock():
            self.human_heartbeat(self.alice, x, self.AIR_Y, on_ground=False, ticks=0)


class BotReactionTests(BotStrikeRoom):
    """出手反应（D91，用户 2026-09-27 定 100~200 ms 随机）：看见机会那一格起等够才起手；机会断一格就重掷。
    连段里排下一招不等（`BotStrikeTests.test_the_combo_goes_on_to_the_link_right_after_the_last_frame` 钉着）。"""

    def press_delay(self, roll, x=640.0):
        """骰子掷 `roll`：bot 头一回看见机会的那一格，到它按下去的那一格，隔了多少秒。"""
        self.bot_conn.roll_unit = lambda: roll
        self.alice_at(x)
        self.alice.sent.clear()
        seen = None
        for _ in range(STRIKE_WAIT_TICKS + 4):
            at = self.now()
            self.advance(1)
            if seen is None and self.bot_conn.opening.seen_tick is not None:
                seen = at
            if self.starts():
                self.assertIsNotNone(seen, "按之前先看见过")
                return at - seen
        self.fail("一直没出手")

    def test_the_fastest_reaction_is_100ms(self):
        took = self.press_delay(0.0)
        self.assertGreaterEqual(took + 1e-6, botfight.ATTACK_REACT_MIN_S, "反应时间之前不按")
        self.assertLess(took, botfight.ATTACK_REACT_MIN_S + botfight.FRAME_S)

    def test_the_slowest_reaction_is_200ms(self):
        took = self.press_delay(0.999)
        want = botfight.ATTACK_REACT_MIN_S + 0.999 * (botfight.ATTACK_REACT_MAX_S - botfight.ATTACK_REACT_MIN_S)
        self.assertGreaterEqual(took + 1e-6, want)
        self.assertLess(took, want + botfight.FRAME_S)

    def test_losing_the_opening_rolls_again(self):
        """机会断了（他跳开）再回来就是新机会：从回来那一格重新数满，不接着上一次剩下的那几格。"""
        self.bot_conn.roll_unit = lambda: 0.99            # 199 ms ≈ 7 格
        self.alice_at(640.0)
        self.advance(4)                                   # 看见了、还没反应过来
        self.assertIsNotNone(self.bot_conn.opening.ready_at)
        self.assertIsNone(self.bot_conn.fight_skill)
        self.alice_at(1100.0, ticks=2)                    # 他跳开了：这两格没有机会
        took = self.press_delay(0.99, x=self.bot_conn.body.x + 40.0)
        want = botfight.ATTACK_REACT_MIN_S + 0.99 * (botfight.ATTACK_REACT_MAX_S - botfight.ATTACK_REACT_MIN_S)
        self.assertGreaterEqual(took + 1e-6, want)

    def test_opening_see_is_per_consecutive_tick(self):
        opening = botfight.Opening()
        self.assertFalse(opening.see(10, 1.000, lambda: 0.5))       # 新机会：掷 150 ms
        self.assertFalse(opening.see(11, 1.100, lambda: 0.0))       # 同一个机会，不重掷
        self.assertTrue(opening.see(12, 1.150, lambda: 0.0))
        opening.reset()
        self.assertFalse(opening.see(14, 1.200, lambda: 0.0))
        self.assertFalse(opening.see(16, 1.290, lambda: 0.0), "断了一格（15 没看见）：重掷，从 16 起数")
        self.assertTrue(opening.see(17, 1.390, lambda: 0.0))

    def test_waited_counts_from_the_first_sighting(self):
        """日志「看见机会 N ms 后按」（X_Mod §131）：从这个机会头一回看见算，重掷就从重掷那一格算。"""
        opening = botfight.Opening()
        opening.see(10, 1.000, lambda: 0.0)
        opening.see(12, 1.064, lambda: 0.0)                         # 断了一格：新机会
        self.assertTrue(opening.see(13, 1.170, lambda: 0.0))
        self.assertAlmostEqual(106.0, opening.waited_ms(1.170))
        opening.reset()
        self.assertIsNone(opening.waited_ms(1.2))


class BotFacingTests(FightActRoom):
    """起手只按当前朝向出（X_Mod §129）：人在身后先转身，转身那一格不按。"""

    def test_it_does_not_strike_someone_behind_it(self):
        self.alice_at(560.0, ticks=1)                     # 在它身后 40 px，刺拳本来够得着
        self.bot_conn.heading = 1
        self.ready_opening()
        self.assertFalse(self.act())
        self.assertIsNone(self.bot_conn.fight_skill, "背对着他不出手")
        self.assertEqual(-1, bot._fight_in_reach(self.room, self.bot_conn, self.bot_seat, self.now()),
                         "走位知道他在身后够得着 ⇒ 「够得着·先转身」")

    def test_no_press_on_the_turn_tick(self):
        """真人这一帧才转过来，按的 J 下一帧才发得出去 ⇒ bot 转身那一格不起手。"""
        self.alice_at(640.0, ticks=1)
        self.bot_conn.heading = 1
        self.ready_opening()
        self.assertFalse(self.act(turned=True))
        self.assertIsNone(self.bot_conn.fight_skill)
        self.assertTrue(self.act(turned=False))
        self.assertIsNotNone(self.bot_conn.fight_skill)

    def test_it_turns_then_strikes_the_other_way(self):
        self.bot_conn.roll_unit = lambda: 0.0
        self.bot_conn.heading = 1
        self.alice_at(560.0)
        self.alice.sent.clear()
        self.assertTrue(self.until(lambda: self.starts(), STRIKE_WAIT_TICKS + 4))
        (_seat, _kind, facing, _index, x, _y), = self.starts()
        self.assertEqual(-1, facing, "转过来朝他出")
        self.assertLess(x, 600.0, "先朝他按了方向键（转身走了一点）")

    def test_someone_on_its_back_needs_no_turn(self):
        """贴在背后 11 px（身子叠着，09-27 19:04:35.941 那一幕）：朝现在这边出手，推挤体先把他推到身前再打中 —— 不转身
        （X_Mod §131）；离远了才转（上面 40 px 那两条）。"""
        self.alice_at(589.0, ticks=1)
        self.bot_conn.heading = 1
        self.assertEqual(1, bot._fight_in_reach(self.room, self.bot_conn, self.bot_seat, self.now()),
                         "当前朝向就够得着 ⇒ 「够得着·站住出招」")
        self.ready_opening()
        self.assertTrue(self.act())
        self.assertEqual(1, self.bot_conn.fight_skill.facing, "照现在的朝向出")

    def test_a_backswing_reaching_behind_still_turns_first(self):
        """布洛克刺拳第一个判定帧拳头还在身后 31 px（r30）：朝前出也扫得到身后 40 px 的她 —— 那不是「贴在背上」（推挤体碰不到），
        照样先转身，别背对着人把他打中（X_Mod §131）。"""
        self.room.seats[self.bot_seat].character_id = 2
        self.bot_conn.character_id = 2
        self.alice_at(560.0, ticks=1)
        self.bot_conn.heading = 1
        circles = [(cx, cy, r) for cx, cy, r, _region in bot.chrprops.get(0).circles(560.0, self.FLOOR_Y, False)]
        self.assertIsNotNone(botfight.first_hit(named(2, "MutuStand-P00"), 1, 600.0, self.FLOOR_Y, circles),
                             "前提：朝前出也扫得到身后的她")
        self.assertEqual(-1, bot._fight_in_reach(self.room, self.bot_conn, self.bot_seat, self.now()))

    def test_it_does_not_twitch_left_and_right_on_top_of_him(self):
        """09-27 19:04:35.951~36.207：他冲到它背后 11 px，它「先转身」那一步（决策两格 ≈ 16 px）跨过了他，下一次决策他又在身后
        —— 朝向每 64 ms 翻一次（19:04:12 那段也是）。当前朝向够得着就不转（X_Mod §131）。"""
        self.bot_conn.heading = -1
        self.alice_at(611.0)
        self.alice.sent.clear()
        headings = [self.bot_conn.heading]
        for _ in range(STRIKE_WAIT_TICKS + 4):
            self.advance(1)
            headings.append(self.bot_conn.heading)
            if self.starts():
                break
        self.assertTrue(self.starts(), "照样出手")
        self.assertEqual([], [i for i, (a, b) in enumerate(zip(headings, headings[1:])) if a != b], headings)


class BotAirActionTests(FightActRoom):
    """`[+0x5d4]`（X_Mod §129）：一次腾空最多一个空中动作；挨重击后落地前不出空中招；踩地每格清。"""

    def test_the_window_does_not_queue_a_second_air_skill(self):
        """09-27 17:37:52.927 / 53.438 那一幕：空中招的接招窗口里又排了一个 Jump-P00 —— 真人按了也发不出去（`0x495a22`）。"""
        self.alice_in_air(630.0)
        self.take_off()
        self.advance_fight()                              # 这一格认它腾空了
        skill = self.start("MutuJump-P00")
        self.assertTrue(self.bot_conn.fight_air_used, "出了空中招就置 `[+0x5d4]`")
        for _ in range(3):
            skill.tick()
        self.assertTrue(skill.window_open(), "前提：接招窗口开了")
        self.bot_conn.fight_press_at = None
        self.act()
        self.assertIsNone(skill.queued, "这次腾空用过了：不排")
        self.bot_conn.fight_air_used = False              # 对照：没有这一位（会话 48 的样子）就会再排一个
        self.act()
        self.assertIsNotNone(skill.queued)
        self.assertEqual(mutudata.AIR, skill.queued.skill_type, "又排了一个空中招（J 是 Jump-P00、K 是 Jump-P00-K）")

    def test_a_queued_air_link_is_not_sent(self):
        self.alice_in_air(630.0)
        self.take_off()
        self.advance_fight()
        skill = self.start("MutuJump-P00")
        skill.queued = named(0, "MutuJump-P00")          # 硬塞一个（AI 不会这么按）
        self.alice.sent.clear()
        for _ in range(skill.skill.frames_total + 1):
            self.advance_fight()
        self.assertEqual([], self.starts(), "不发排好的那招")
        self.assertEqual(1, len(mutu_frames(self.alice, self.bot_seat, botsync.MUTU_SKILL_END)), "照没排算：发收招")

    def test_a_heavy_hit_blocks_air_skills_until_it_lands(self):
        self.alice_in_air(630.0)
        self.take_off()
        self.advance_fight()
        self.assertFalse(self.bot_conn.fight_air_used)
        self.hit(3.0, (-15.0, -4.5), botfight.HIT_SLIDE)
        self.assertTrue(self.bot_conn.fight_air_used, "挨重击置 `[+0x5d4]`（`0x4ff6ad`）")
        self.bot_conn.fight_block_left = 0                # 只看这一位：击退那 10 帧另有门
        self.bot_conn.fight_react = None
        self.ready_opening()
        self.assertFalse(self.act())
        self.assertIsNone(self.bot_conn.fight_skill, "落地前不出空中招")
        self.bot_conn.body = botmove.Body(600.0, self.FLOOR_Y)
        self.advance_fight()
        self.assertFalse(self.bot_conn.fight_air_used, "踩地那一格清")

    def test_the_flag_clears_on_every_grounded_tick(self):
        """不只是「落地那一下」—— 在地上每格都清（`0x5155d0`）：在地上挨了一下也不会留到下一跳。"""
        self.advance(1)
        self.assertTrue(self.bot_conn.fight_grounded)
        self.bot_conn.fight_air_used = True
        self.advance_fight()
        self.assertFalse(self.bot_conn.fight_air_used)

    def test_leaving_the_ground_drops_a_queued_link(self):
        """着地状态一变清队列，不分空中地面招（`0x495bf5`）；地面招本身照走。"""
        self.advance(1)
        skill = self.start("MutuStand-P00")
        skill.queued = named(0, "MutuStand-P01")
        self.take_off(self.bot_conn.body.x)
        self.advance_fight()
        self.assertIs(skill, self.bot_conn.fight_skill)
        self.assertIsNone(skill.queued)


class BotRetractTimingTests(FightActRoom):
    """收招之后照真人那台的帧序（X_Mod §129）：收招那一格不起手 / 不冲 / 不挡；那一格和下一格不走、不转身。"""

    def retract(self):
        skill = self.start("MutuStand-P00")
        self.bot_conn.fight_skill = None
        with self.clock():
            bot._end_fight_skill(self.bot_conn, skill)
        self.assertEqual(0, self.bot_conn.fight_settle)

    def test_no_press_on_the_retract_tick(self):
        self.alice_at(640.0, ticks=1)
        self.retract()
        self.bot_conn.fight_press_at = None
        self.ready_opening()
        self.assertFalse(self.act())
        self.assertIsNone(self.bot_conn.fight_skill, "收招那一格：招式对象还在，按的下一帧才发")
        self.advance_fight()
        self.assertEqual(1, self.bot_conn.fight_settle)
        self.ready_opening()
        self.assertTrue(self.act(), "下一格同方向就能出")

    def test_no_dash_on_the_retract_tick(self):
        self.alice_at(640.0, ticks=1)
        self.bot_conn.melee = True
        self.bot_conn.stamina = 100.0
        self.retract()
        self.ready_opening()
        with self.clock():
            self.assertFalse(bot._try_dash(self.room, self.bot_conn, self.bot_seat, self.now(), True,
                                           tick=self.loop().done))
        self.advance_fight()
        self.ready_opening()
        with self.clock():
            self.assertTrue(bot._try_dash(self.room, self.bot_conn, self.bot_seat, self.now(), True,
                                          tick=self.loop().done))

    def test_no_guard_on_the_retract_tick(self):
        self.alice_at(700.0, ticks=1)
        self.retract()
        guard = self.bot_conn.guard
        threat = (("mutu", self.alice_seat, 0.0), 0.0)
        guard.seen, guard.react_at = threat[0], 0.0       # 早就反应过来了
        with self.clock():
            self.assertFalse(bot._fight_guard(self.room, self.bot_conn, self.bot_seat, self.now(), threat, False))
        self.advance_fight()
        with self.clock():
            self.assertTrue(bot._fight_guard(self.room, self.bot_conn, self.bot_seat, self.now(), threat, False))

    def test_it_holds_still_for_two_ticks_after_a_retract(self):
        self.bot_conn.roll_unit = lambda: 0.0
        self.alice_at(640.0)
        self.assertTrue(self.until(lambda: self.bot_conn.fight_skill is not None, STRIKE_WAIT_TICKS))
        self.bot_conn.fight_press_at = 1e18               # 不排下一招
        self.alice_at(1100.0)                             # 他走远了：播完就收招，然后朝他走
        xs, ended = [], None
        for i in range(40):
            self.advance(1)
            xs.append(self.bot_conn.body.x)
            if ended is None and mutu_frames(self.alice, self.bot_seat, botsync.MUTU_SKILL_END):
                ended = i
            if ended is not None and i >= ended + 2:
                break
        self.assertIsNotNone(ended, "播完发了收招")
        n = ended
        self.assertEqual(xs[n - 1], xs[n], "收招那一格不走")
        self.assertEqual(xs[n], xs[n + 1], "下一格也不走（回环才删招式对象，这一帧的输入才读方向）")
        self.assertGreater(xs[n + 2], xs[n + 1], "第 2 格走得动了")


def constrain_frames(conn, seat):
    """bot 发出去的 `0x0017`：`[(受约束对象句柄, 出招者句柄)]`。"""
    return [__import__("struct").unpack("<ii", body_of(f)) for f in bot_frames(conn, seat)
            if header(f)["opcode"] == botsync.OP_CONSTRAIN]


class BotPushDesyncTests(FightActRoom):
    """09-27 19:05:04 用户报的「瞬移」（X_Mod §130 / D92）：卡希尔 K00 朝右出，她站在它左后方 11 px，推挤体照样碰得到 → 替它发
    `0x0017`；她那台没挂上、她照走她的（往左跑远了）。以前服务端照推不误，每格把她的外推拽回 bot 右边 35 px；K00 一收接出 P00，
    推挤体碰上这个假位置又推一发 —— 她那台这回挂上了，她从 584 被拽到 977。这里 bot 是泰尔（没有 K00），拿同样朝前冲的 K01 演。"""

    def setUp(self):
        super().setUp()
        self.bot_conn.fight_press_at = 1e18             # AI 不按键：招式由用例自己出
        self.alice_at(589.0, ticks=1)                  # bot 在 600，她在它左后方 11 px

    def pushed_by_heavy(self):
        skill = self.start("MutuStand-K01", facing=1)
        self.assertTrue(self.until(lambda: self.alice_seat in skill.carried, 4), "前提：推挤体碰到了她")
        return skill

    def test_her_heartbeats_win_over_a_push_she_did_not_take(self):
        skill = self.pushed_by_heavy()
        self.alice_at(570.0, ticks=1)                  # 她那台没挂上：照往左走
        self.assertNotIn(self.alice_seat, skill.carried)
        self.assertIn(self.alice_seat, skill.released)
        self.assertLess(self.alice.sim_body.x, 600.0, "外推跟着她报的走，不在 bot 右边")

    def test_until_her_next_heartbeat_the_push_holds(self):
        """推上那一刻已经到了的心跳（她还在它身后）不算证据：推上之后她那台还没来过心跳，就照原版推她到 bot 身前。"""
        skill = self.pushed_by_heavy()
        self.advance(2)
        self.assertIn(self.alice_seat, skill.carried)
        self.assertGreaterEqual(self.alice.sim_body.x,
                                int(self.bot_conn.body.x + bot.botmotion.CONSTRAINT_DISTANCE) - 1.0)

    def test_one_step_inside_the_edge_still_counts_and_more_does_not(self):
        """她那台每帧先把她推到边界、再走这一帧（`0x4fe20c` 在走路之前）⇒ 报在边界里面一步之内照样算挂着；远了就不算。"""
        skill = self.pushed_by_heavy()
        edge = int(skill.carry_seen[self.alice_seat][1] + bot.botmotion.CONSTRAINT_DISTANCE)   # 推上以来最宽松的边
        self.alice_at(edge - 5.0, ticks=1)
        self.assertIn(self.alice_seat, skill.carried, "差 5 px：她这一帧走回来的")
        reach = bot._human_frame_reach(self.room, self.alice_seat, self.alice, self.alice.sync_trail[-1], self.now())
        self.alice_at(edge - reach - 2.0, ticks=1)
        self.assertNotIn(self.alice_seat, skill.carried, "差出一步以上：她那台没挂着")

    def test_the_next_skill_does_not_push_her_from_afar(self):
        skill = self.pushed_by_heavy()
        x = 570.0
        while self.bot_conn.fight_skill is skill:      # 她一路往左跑，每 4 格一发心跳
            self.alice_at(x, ticks=4)
            x -= 30.0
        self.alice.sent.clear()
        self.start("MutuStand-P00", facing=1)           # 19:05:03.984 那一招
        self.advance(6)
        self.assertEqual([], constrain_frames(self.alice, self.bot_seat),
                         "她在老远：不再替 bot 推她（以前这一发把她拽了 393 px）")


class BotLimbTests(BotStrikeRoom):
    """真人打中 bot 伸出去的手脚：包里受害者是那个受击体的句柄（`0x480f06`），服务端映射回 bot（`0x4f9a4b`）。"""

    def test_a_hit_on_its_limb_lands_on_it(self):
        heavy = named(0, "MutuStand-K01")
        self.bot_conn.fight_press_at = 1e18
        with bot._tick_clock(self.now()):
            bot._start_fight_skill(self.room, self.bot_conn, self.bot_seat, heavy, 1, self.now(), "单测")
        skill = self.bot_conn.fight_skill
        limb = skill.base + len(heavy.damagers)            # 受击体 0 的句柄
        mobs = dict(getattr(self.room.quest, "mobs", {}) or {})
        self.alice_sends(botsync.OP_SPLASH_DAMAGED, botsync.splash_body(
            botsync.projectile_handle(self.alice_seat, 0), limb, 3.0, 640.0, 360.0,
            push_x=-15.0, push_y=-4.5, kind=botfight.HIT_SLIDE))
        self.assertIsNone(self.bot_conn.fight_skill, "打在它身上：招被打断")
        self.assertIsNotNone(self.bot_conn.fight_react, "滑退 / 锁输入")
        self.assertEqual(mobs, dict(getattr(self.room.quest, "mobs", {}) or {}), "没被当成怪记账")


class BotRetractOwedTests(BotStrikeRoom):
    """X_Mod §139 / D98：bot 的招被打断 —— 原版各台只删本地、不发收招，先收到打中、后收到它出招包的那台客户端会一直
    留着这一招（复活后躺着、冲刺动作循环）。服务端替它在挨打那 10 帧走完补一发原版收招；躺着就当场补。"""

    PUSH_X = 15.0                                        # alice 在左边：把它往右推 ⇒ 它转向左

    def start_heavy(self):
        heavy = named(0, "MutuStand-K01")
        self.bot_conn.fight_press_at = 1e18              # 只看补发：它自己别再按
        with bot._tick_clock(self.now()):
            bot._start_fight_skill(self.room, self.bot_conn, self.bot_seat, heavy, 1, self.now(), "单测")
        return heavy

    def hit_body(self, flags=0):
        self.alice_sends(botsync.OP_SPLASH_DAMAGED, botsync.splash_body(
            botsync.projectile_handle(self.alice_seat, 0), botsync.character_handle(self.bot_seat),
            3.0, 600.0, 360.0, push_x=self.PUSH_X, push_y=-4.5, flags=flags, kind=botfight.HIT_SLIDE))

    def ends(self):
        return [botsync.parse_mutu_skill(body_of(f))
                for f in mutu_frames(self.alice, self.bot_seat, botsync.MUTU_SKILL_END)]

    def test_it_pays_one_retract_on_the_tick_the_hit_block_runs_out(self):
        self.start_heavy()
        self.hit_body()
        self.assertIsNone(self.bot_conn.fight_skill, "招被打断")
        self.assertTrue(self.bot_conn.fight_retract_owed)
        self.assertEqual([], self.ends(), "打断那一刻不发（原版也不发，要等那 10 帧）")
        paid_at = None
        for tick in range(1, 3 * bot.FIGHT_BLOCK_FRAMES):
            self.advance(1)
            if paid_at is None and self.ends():
                paid_at = tick
                self.assertEqual(0, self.bot_conn.fight_block_left, "补在那 10 帧走完的那一格")
        self.assertEqual(bot.FIGHT_BLOCK_FRAMES, paid_at)
        self.assertEqual(1, len(self.ends()), "只补一发")
        self.assertFalse(self.bot_conn.fight_retract_owed)

    def test_the_retract_is_a_plain_original_one(self):
        self.start_heavy()
        self.hit_body()
        self.assertTrue(self.until(lambda: self.ends(), limit=3 * bot.FIGHT_BLOCK_FRAMES))
        seat, kind, facing, index, x, y = self.ends()[0]
        self.assertEqual((self.bot_seat, botsync.MUTU_SKILL_END, 0), (seat, kind, index), "类型 −1、招式号 0（同 `0x495a87`）")
        self.assertEqual(-1, facing, "朝向照它此刻的：挨打后转向左边的攻击者")
        body = self.bot_conn.body
        self.assertEqual((body.x, body.y), (x, y), "坐标是补发那一格它自己的（滑退已经走完）")
        self.assertGreater(x, 600.0 + 25, "那 10 帧的滑退走完了才发")
        self.assertIsNone(self.bot_conn.fight_settle, "不置收招后那几格：手上早就没招了，照常能走能按")

    def test_it_goes_out_before_a_new_skill_pressed_on_the_same_tick(self):
        self.start_heavy()
        self.hit_body()
        jab = named(0, "MutuStand-P00")

        def eager(room, machine, seat_index, now, *args, **kwargs):
            # 一有机会就出招：锁一放开的那一格就按（比真 AI 急，专门撞「同一格」）
            if machine is self.bot_conn and machine.fight_block_left <= 0 and machine.fight_skill is None:
                bot._start_fight_skill(room, machine, seat_index, jab, machine.heading, now, "单测")
                return True
            return False

        with mock.patch.object(bot, "_fight_act", eager):
            self.assertTrue(self.until(lambda: self.starts()[1:], limit=3 * bot.FIGHT_BLOCK_FRAMES))
        kinds = [botsync.parse_mutu_skill(body_of(f))[1] for f in mutu_frames(self.alice, self.bot_seat)]
        # 头一个出招是 start_heavy 的那一招；之后必须先收招、再出新招 —— 反过来收方会把新招当成它删掉。
        self.assertEqual([botsync.MUTU_SKILL_START, botsync.MUTU_SKILL_END, botsync.MUTU_SKILL_START], kinds[:3])

    def test_a_lying_bot_pays_right_away(self):
        self.start_heavy()
        self.hit_body()
        body = self.bot_conn.body
        self.room.quest.arm_respawn_watchdog(self.bot_seat, (body.x, body.y), after=5.0)
        self.assertGreater(self.bot_conn.fight_block_left, 1)
        self.advance(1)
        self.assertEqual(1, len(self.ends()), "躺着不等那 10 帧（`Die()` 不删客户端上的招式对象）")
        self.assertFalse(self.bot_conn.fight_retract_owed)
        self.advance(2 * bot.FIGHT_BLOCK_FRAMES)
        self.assertEqual(1, len(self.ends()), "躺着的每一格不重发")

    def test_no_skill_no_debt(self):
        self.hit_body()
        self.assertFalse(self.bot_conn.fight_retract_owed)
        self.advance(2 * bot.FIGHT_BLOCK_FRAMES)
        self.assertEqual([], self.ends())

    def test_a_guarded_hit_does_not_break_the_skill(self):
        self.start_heavy()
        self.hit_body(flags=bot.EXPLODE_FLAG_GUARD)
        self.assertIsNotNone(self.bot_conn.fight_skill, "挡住的那一下不打断（`0x4ff4ac`）")
        self.assertFalse(self.bot_conn.fight_retract_owed)

    def test_a_new_game_forgets_the_debt(self):
        self.start_heavy()
        self.hit_body()
        self.bot_conn.reset_battle_frame()
        self.assertFalse(self.bot_conn.fight_retract_owed, "换图 / 新一局角色重建，客户端上已经没有可删的")


#: 等 bot 举挡最多推几格。单测里真人事件的网格时刻按挂钟对齐（`human_heartbeat` 不走模拟时钟），和房间的模拟时钟差几格、
#: 机器越慢差得越多 —— 用例都相对他这一波的起点（`chain_start`）比，只是等的上限要给够。
GUARD_WAIT_TICKS = 120


class BotGuardTests(BotStrikeRoom):
    """格挡（D88）：看见他出手 → 等 200~400 ms（随机）的反应时间 → 这一下还打得到自己就举挡；过去了放下。"""

    def setUp(self):
        super().setUp()
        self.bot_conn.fight_press_at = 1e18               # 只看挡，不出招
        self.alice_at(700.0)

    def alice_heavy(self):
        """alice 在 700 朝左出 K01：先冲 90 px 到 610，再踢到站在 600 的 bot。返回这一波开始出手的时刻。"""
        self.alice_sends(botsync.OP_MUTU_SKILL, botsync.mutu_skill_body(
            self.alice_seat, botsync.MUTU_SKILL_START, -1, skill_index(0, "MutuStand-K01"), 700.0, self.FLOOR_Y))
        return self.alice.mutu_skill.chain_start

    def guard_times(self, ticks=None):
        ticks = GUARD_WAIT_TICKS if ticks is None else ticks
        ons = []
        for _ in range(ticks):
            before = self.bot_conn.guard.on
            self.advance(1)
            if self.bot_conn.guard.on != before:
                ons.append((self.bot_conn.guard.on, self.now() - botfight.FRAME_S))
        return ons

    def raised_after(self, roll):
        """骰子掷 `roll`：他出手之后第一次举挡是哪一刻（相对这一波开始出手）。"""
        self.bot_conn.roll_unit = lambda: roll
        start = self.alice_heavy()
        flips = self.guard_times()
        self.assertTrue(flips and flips[0][0], flips)
        return flips[0][1] - start

    def test_the_fastest_reaction_is_200ms(self):
        took = self.raised_after(0.0)
        self.assertGreaterEqual(took + 1e-6, botfight.GUARD_REACT_MIN_S, "反应时间之前不举")
        self.assertLess(took, botfight.GUARD_REACT_MIN_S + 2 * botfight.FRAME_S)

    def test_the_slowest_reaction_is_400ms(self):
        took = self.raised_after(0.999)
        self.assertGreaterEqual(took + 1e-6, 0.999 * botfight.GUARD_REACT_MAX_S
                                + 0.001 * botfight.GUARD_REACT_MIN_S)
        self.assertLess(took, botfight.GUARD_REACT_MAX_S + 2 * botfight.FRAME_S)

    def test_a_lunge_through_it_is_a_threat(self):
        """她从 660 出 K01：先冲 90 px 冲过 bot 的位置 —— 推挤体会把 bot 推在她身前，踢照样打到（`first_hit(push=…)`）。"""
        self.alice_at(660.0, ticks=1)                     # 推一格：服务端外推的她站到 660
        self.assertEqual(660.0, self.alice.sim_body.x)
        self.alice_sends(botsync.OP_MUTU_SKILL, botsync.mutu_skill_body(
            self.alice_seat, botsync.MUTU_SKILL_START, -1, skill_index(0, "MutuStand-K01"), 660.0, self.FLOOR_Y))
        self.assertIsNotNone(bot._mutu_threat(self.room, self.bot_conn, self.bot_seat, self.now()))

    def test_the_threat_does_not_flicker_while_she_lunges(self):
        """外推她先于 bot（`_advance_humans`）：这一帧的位移已经在她身上，预测不能再加一遍（`first_hit(applied=True)`）——
        加了的话她冲出 57 px 那一帧会被估成冲过头、威胁断一格，反应时间跟着被重掷。"""
        self.alice_heavy()
        last = max(max(d.points) for d in self.alice.mutu_skill.skill.damagers)   # 判定体最后一个逻辑帧
        seen = []
        for _ in range(GUARD_WAIT_TICKS):
            self.advance(1)
            skill = self.alice.mutu_skill
            if skill is None:
                break
            k = skill.frame(bot._human_skill_raw(self.alice, skill, self.now()))
            if k is not None and k >= last:
                break
            seen.append(bot._mutu_threat(self.room, self.bot_conn, self.bot_seat, self.now()) is not None)
        self.assertGreater(len(seen), 5)
        self.assertTrue(all(seen), seen)

    def test_it_sends_on_and_off(self):
        self.bot_conn.roll_unit = lambda: 0.0
        self.alice_heavy()
        flips = self.guard_times(GUARD_WAIT_TICKS + 40)
        self.assertEqual([True, False], [on for on, _at in flips])
        frames = guard_frames(self.alice, self.bot_seat)
        self.assertEqual([b"\x01", b"\x00"], [body_of(f)[1:2] for f in frames])

    def test_guarding_costs_stamina_and_stops_regen(self):
        self.bot_conn.roll_unit = lambda: 0.0
        self.alice_heavy()
        self.assertTrue(self.until(lambda: self.bot_conn.guard.on, GUARD_WAIT_TICKS))
        before = self.bot_conn.stamina
        self.advance(4)
        self.assertAlmostEqual(before - 4 * bot.chrprops.game().guard_sp_cost, self.bot_conn.stamina)

    def test_it_breaks_when_stamina_runs_out(self):
        self.bot_conn.roll_unit = lambda: 0.0
        self.alice_heavy()
        self.assertTrue(self.until(lambda: self.bot_conn.guard.on, GUARD_WAIT_TICKS))
        self.bot_conn.stamina = 1.2
        self.advance(3)
        self.assertFalse(self.bot_conn.guard.on)
        self.assertTrue(self.bot_conn.guard.broken, "打破了：这一波过去之前不再举")

    def test_a_guarded_hit_stuns_but_does_not_cut_anything(self):
        self.bot_conn.guard.set(True)                     # 不推格：没有威胁的话 AI 下一格就自己放下了
        ledger = bot._health(self.room)
        before = ledger.remaining(self.bot_seat, bot._seat_max_hp(self.room, self.bot_seat))
        self.hit(20.0, (-15.0, -30.0), botfight.HIT_FLY, flags=bot.EXPLODE_FLAG_GUARD)
        self.assertIsNotNone(self.bot_conn.fight_react, "硬直 10 帧 + 滑 2·push.x（`0x4ff4ac`）")
        self.assertTrue(self.bot_conn.body.on_ground, "不打飞")
        after = ledger.remaining(self.bot_seat, bot._seat_max_hp(self.room, self.bot_seat))
        self.assertEqual(int(0.25 * 20 + 1), before - after)

    def test_a_guarded_hit_right_after_lowering_is_still_a_block(self):
        """她那台判「在挡」= 开关 XOR 3 帧过渡（`0x50a0ea`）：它刚放下挡的那 3 帧里她照样带 0x80 打过来。原版它自己那台
        （`0x4ff486`）只看这一位和「正面」，不看自己的开关 ⇒ 仍走格挡那一支（X_Mod §131；09-27 两局七下，全在放下后 19~93 ms）。"""
        self.bot_conn.guard.set(True)
        self.bot_conn.guard.set(False)                    # 刚放下（过渡还在跑）
        ledger = bot._health(self.room)
        before = ledger.remaining(self.bot_seat, bot._seat_max_hp(self.room, self.bot_seat))
        self.hit(12.0, (-15.0, -18.0), botfight.HIT_FLY, flags=bot.EXPLODE_FLAG_GUARD)
        self.assertIsNotNone(self.bot_conn.fight_react, "硬直 + 滑 2·push.x")
        self.assertTrue(self.bot_conn.body.on_ground, "不打飞")
        after = ledger.remaining(self.bot_seat, bot._seat_max_hp(self.room, self.bot_seat))
        self.assertEqual(int(0.25 * 12 + 1), before - after)

    def test_striking_drops_the_guard(self):
        self.bot_conn.guard.set(True)                     # 不推格：没有威胁的话 AI 下一格就自己放下了
        self.alice.sent.clear()
        with bot._tick_clock(self.now()):
            bot._start_fight_skill(self.room, self.bot_conn, self.bot_seat,
                                   named(0, "MutuStand-P00"), -1, self.now(), "单测")
        self.assertFalse(self.bot_conn.guard.on, "收方收到 0x0016 清格挡位")
        self.assertEqual([], guard_frames(self.alice, self.bot_seat), "不另发 0x0018")


# ===========================================================================
# D94：被连招压着（用户 2026-09-27「只要我一直不停进攻，bot 就不还手也不逃跑」，选「看情况」）
# ===========================================================================
class PressureRoom(FightActRoom):
    """alice（泰尔）站在 bot 左边 38 px 朝右连招，bot 朝左对着她。她那台判的打中 / 挡住走真的 `0x0004` 入口（模拟时钟对齐）。"""

    ALICE_X = 562.0

    def setUp(self):
        super().setUp()
        self.bot_conn.heading = -1
        self.alice_at(self.ALICE_X, ticks=1)
        self.alice.sent.clear()

    def alice_skill(self, motion, x=None):
        x = self.ALICE_X if x is None else x
        self.alice_sends(botsync.OP_MUTU_SKILL, botsync.mutu_skill_body(
            self.alice_seat, botsync.MUTU_SKILL_START, 1, skill_index(0, motion), x, self.FLOOR_Y))
        return self.alice.mutu_skill

    def alice_retracts(self):
        self.alice_sends(botsync.OP_MUTU_SKILL, botsync.mutu_skill_body(
            self.alice_seat, botsync.MUTU_SKILL_END, 1, 0, self.ALICE_X, self.FLOOR_Y))

    def alice_hits(self, damage=3.0, kind=botfight.HIT_SLIDE, flags=0):
        """她这一招打中 bot（她那台判的，源 = 她自己的判定体句柄）。"""
        with self.sim_clock():
            self.hit(damage, (15.0, -1.5 * damage), kind, flags=flags)

    def unlock(self):
        """推到挨打的锁输入 / 不能出招那 10 帧过去。"""
        conn = self.bot_conn
        self.assertTrue(self.until(lambda: not (conn.fight_react is not None and conn.fight_react.locked)
                                   and conn.fight_block_left <= 0, 14), "锁输入该解了")

    def set_health(self, fraction):
        ledger = bot._health(self.room)
        ledger.reset(self.bot_seat)
        ledger.note_damage(self.bot_seat, bot._seat_max_hp(self.room, self.bot_seat) * (1.0 - fraction))

    def jumps(self):
        return [f for f in bot_frames(self.alice, self.bot_seat) if header(f)["opcode"] == botsync.OP_JUMP]

    @contextlib.contextmanager
    def she_hits_first(self):
        """她下一次判定马上就到（反击抢不过）—— 只看跳开那一支。"""
        with mock.patch.object(bot, "_his_next_contact", lambda *a: self.now()):
            yield

    def two_hits(self):
        self.alice_skill("MutuStand-P00")
        self.alice_hits()
        self.unlock()
        self.alice_skill("MutuStand-P01", x=self.bot_conn.body.x - 38.0)
        self.alice_hits()
        self.alice.sent.clear()


class MutuPushTests(PressureRoom):
    """她格斗招式的推挤体推着 bot（`0x0017`）：照原版不锁输入（§111）—— 以前冻到她那一招收完（D75 / D79 只该管冲刺）。"""

    def test_her_fighting_move_pushing_it_does_not_freeze_it(self):
        self.alice_skill("MutuStand-P00")
        self.alice_sends(0x0017, struct.pack("<ii", botsync.character_handle(self.bot_seat),
                                             botsync.character_handle(self.alice_seat)))
        self.assertIsNotNone(self.bot_conn.motion_constraint, "前提：推上了（位置照样约束）")
        with self.clock():
            self.assertFalse(bot._held_by_push(self.room, self.bot_conn, self.now()))


class ThreatAfterHitTests(PressureRoom):
    """一招对一人只中一次（`CanHit` / `MarkHit`）：打中过它（挡住的也算）的那一招不再让它举挡。"""

    def test_a_move_that_already_hit_it_is_no_threat(self):
        skill = self.alice_skill("MutuStand-P00")
        with self.clock():
            self.assertIsNotNone(bot._mutu_threat(self.room, self.bot_conn, self.bot_seat, self.now()),
                                 "前提：她这一记刺拳打得到它")
        self.alice_hits(flags=bot.EXPLODE_FLAG_GUARD)
        self.assertIn(self.bot_seat, skill.hit_seats)
        with self.clock():
            self.assertIsNone(bot._mutu_threat(self.room, self.bot_conn, self.bot_seat, self.now()))


class PressureStateTests(PressureRoom):
    """压制计数：她这一串打中（含挡住）几下；反应从挨打那一刻数；清掉全看事件。"""

    def test_a_hit_counts_and_the_reaction_starts_there(self):
        self.alice_skill("MutuStand-P00")
        self.alice_hits()
        pressure = self.bot_conn.fight_pressure
        self.assertEqual((self.alice_seat, 1), (pressure.attacker, pressure.count))
        self.assertGreaterEqual(pressure.ready_at - pressure.seen_at + 1e-9, botfight.ATTACK_REACT_MIN_S)
        self.assertLessEqual(pressure.ready_at - pressure.seen_at, botfight.ATTACK_REACT_MAX_S + 1e-9)

    def test_her_seamless_next_move_keeps_it(self):
        self.alice_skill("MutuStand-P00")
        self.alice_hits()
        self.alice_skill("MutuStand-P01")
        with self.clock():
            self.assertIsNotNone(bot._fight_pressure(self.room, self.bot_conn, self.now()), "无缝接下一招：还压着")
        self.alice_hits()
        self.assertEqual(2, self.bot_conn.fight_pressure.count)

    def test_retracting_ends_it(self):
        self.alice_skill("MutuStand-P00")
        self.alice_hits()
        self.alice_retracts()
        with self.clock():
            self.assertIsNone(bot._fight_pressure(self.room, self.bot_conn, self.now()), "她收招了：这一串断了")


class BreakFreeTests(PressureRoom):
    """能动的那一格：抢得过她下一次判定就反击；抢不过、被连着打中两下（血少于 1/4 时一下）就朝背对她那边跳开。"""

    def test_two_hits_then_it_jumps_away_from_her(self):
        with self.she_hits_first():
            self.two_hits()
            self.assertTrue(self.until(lambda: self.jumps(), 16), "锁一解就跳")
        self.assertEqual(1, self.bot_conn.heading, "背对她（她在左边）")
        start = self.bot_conn.body.x
        self.assertTrue(self.until(lambda: self.bot_conn.fight_escape is None, 80), "落地就完了")
        self.assertTrue(self.bot_conn.body.on_ground)
        self.assertGreater(self.bot_conn.body.x, start + 60.0, "跳开了一大段")
        self.assertIsNone(self.bot_conn.fight_pressure)

    def test_it_lets_go_of_the_guard_to_jump(self):
        with self.she_hits_first():
            self.two_hits()
            self.bot_conn.guard.set(True)
            self.assertTrue(self.until(lambda: self.jumps(), 16))
        self.assertFalse(self.bot_conn.guard.on)
        self.assertEqual(b"\x00", body_of(guard_frames(self.alice, self.bot_seat)[-1])[1:2], "起跳就是松开 L")

    def test_one_hit_is_not_enough_at_full_health(self):
        with self.she_hits_first():
            self.alice_skill("MutuStand-P00")
            self.alice_hits()
            self.alice.sent.clear()
            self.advance(20)
        self.assertEqual([], self.jumps())

    def test_at_low_health_one_hit_is_enough(self):
        self.set_health(0.2)
        with self.she_hits_first():
            self.alice_skill("MutuStand-P00")
            self.alice_hits()
            self.alice.sent.clear()
            self.assertTrue(self.until(lambda: self.jumps(), 16))

    def test_it_counters_when_it_lands_before_her_next_hit(self):
        with mock.patch.object(bot, "_his_next_contact", lambda *a: self.now() + 1.0):   # 她一秒内都打不到它
            self.alice_skill("MutuStand-P00")
            self.alice_hits()
            self.unlock()
            self.alice_at(self.bot_conn.body.x - 38.0)    # 她跟上来了：刺拳够得着
            self.alice.sent.clear()
            self.assertTrue(self.until(lambda: self.starts(), 3), "锁一解就反击（反应在挨打时就数完了）")
        (_seat, _kind, facing, index, _x, _y), = self.starts()
        self.assertEqual((-1, skill_index(0, "MutuStand-P00")), (facing, index), "朝她、出最快那一招")
        self.assertIsNone(self.bot_conn.fight_pressure)
        self.assertEqual([], self.jumps())

    def test_no_counter_when_she_hits_first(self):
        with self.she_hits_first():
            self.alice_skill("MutuStand-P00")
            self.alice_hits()
            self.unlock()
            self.alice_at(self.bot_conn.body.x - 38.0)
            self.alice.sent.clear()
            self.advance(8)
        self.assertEqual([], self.starts(), "抢不过就不硬出（压着的时候也不走「够得着就起手」那一支）")

    def test_no_jump_into_a_pit(self):
        self.install_terrain(synth_terrain("fight_pit_right", floor=400, height=440, pits=((700, 1400),)))
        with self.she_hits_first():
            self.two_hits()
            self.advance(16)
        self.assertEqual([], self.jumps(), "那边落不下来就不跳，接着挡")
        self.assertTrue(self.bot_conn.body.on_ground)

    def test_no_jump_into_a_wall(self):
        self.install_terrain(synth_terrain("fight_wall_right", floor=400, height=440, walls=((670, 1400, 100),)))
        with self.she_hits_first():
            self.two_hits()
            self.alice_at(self.bot_conn.body.x - 38.0)    # 她贴着：跳也跳不出她的刺拳
            self.advance(16)
        self.assertEqual([], self.jumps(), "背后是墙：跳了照样挨打，不跳")

    def test_a_hit_in_the_air_cancels_it(self):
        with self.she_hits_first():
            self.two_hits()
            self.assertTrue(self.until(lambda: self.jumps(), 16))
            self.advance(2)
            self.assertIsNotNone(self.bot_conn.fight_escape, "前提：还在跳")
            self.alice_hits(kind=botfight.HIT_FLY, damage=12.0)
        self.assertIsNone(self.bot_conn.fight_escape)


class HisNextContactTests(PressureRoom):
    """`_his_next_contact`：她这一招还打得到它 ⇒ 那一帧；已经打中过 ⇒ 她这一招播完、下一招最快的判定帧。"""

    def test_her_live_jab_is_a_few_frames_away(self):
        self.alice_skill("MutuStand-P00")
        with self.clock():
            at = bot._his_next_contact(self.room, self.bot_conn, self.bot_seat, self.alice_seat, self.alice, self.now())
            self.assertGreater(at, self.now())
            self.assertLess(at, self.now() + 0.3)

    def test_after_it_landed_she_needs_her_next_move(self):
        skill = self.alice_skill("MutuStand-P00")
        self.alice_hits()
        with self.clock():
            at = bot._his_next_contact(self.room, self.bot_conn, self.bot_seat, self.alice_seat, self.alice, self.now())
        want = skill.end_time() + (botfight.HUMAN_SKILL_START_FRAMES + bot._fastest_damager_frame(0)) * botfight.FRAME_S
        self.assertAlmostEqual(want, at)


class PressureReplayTests(FightActRoom):
    """09-27 21:15:15.003 那一串原样重放（布洛克 vs 108 号的 J 四连 P00→P01→P02→P03，她那台判的四下打中 / 挡住）：
    以前 bot 整串只举挡、两串之间 300 ms 空档一动不动；现在第二下之后硬直一解就跳开或反击（D94）。"""

    EVENTS = [(0, "MutuStand-P00"), (96, (5.0, 2, 0)), (479, "MutuStand-P01"), (575, (9.0, 2, 0)),
              (1088, "MutuStand-P02"), (1375, (9.0, 2, 0x80)), (1567, "MutuStand-P03"), (1727, (15.0, 3, 0x80))]

    def test_it_breaks_free_instead_of_standing_there(self):
        self.room.seats[self.bot_seat].character_id = 2
        self.bot_conn.character_id = 2
        self.room.seats[self.alice_seat].character_id = 108
        self.bot_conn.heading = -1
        self.bot_conn.roll_unit = lambda: 0.5
        self.alice_at(562.0, ticks=1)
        self.alice.sent.clear()
        t0 = self.now()
        pending = list(self.EVENTS)
        acted = None
        while pending or acted is None:
            now = self.now()
            if now - t0 > 2.4:
                break
            while pending and t0 + pending[0][0] / 1000.0 <= now + 1e-9:
                _at, ev = pending.pop(0)
                if isinstance(ev, str):
                    self.alice_sends(botsync.OP_MUTU_SKILL, botsync.mutu_skill_body(
                        self.alice_seat, botsync.MUTU_SKILL_START, 1, skill_index(108, ev),
                        self.bot_conn.body.x - 38.0, self.FLOOR_Y))
                else:
                    with self.sim_clock():
                        self.hit(ev[0], (15.0, -1.5 * ev[0]), ev[1], flags=ev[2])
            self.advance(1)
            if acted is None and (self.jumps() or self.starts()):
                acted = self.now() - t0
        self.assertIsNotNone(acted, "整串打下来它得跳开或者反击一次")
        self.assertLess(acted, 1.1, "第二下（575 ms）挨完、硬直一解就动（以前到下一串都没动）")

    def jumps(self):
        return [f for f in bot_frames(self.alice, self.bot_seat) if header(f)["opcode"] == botsync.OP_JUMP]


if __name__ == "__main__":
    unittest.main()
