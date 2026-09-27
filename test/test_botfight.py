"""格斗模式（무투전，对战模式号 2）里的 bot（X16，D84）。

C2（本文件现在的内容）：
* 格斗房里的 bot **不开枪、不换枪**（客户端开火总入口关着，收方却不拦 `rpFire`，X_Mod §119），照样走过去冲刺；
* 挨真人格斗招式的两种反应（`rpSplashDamaged +12` = 2 / 3，X_Mod §122）：类型 2 锁输入 10 帧 + 按 x^(1/10) 曲线
  滑 2·push.x、不改速度；类型 3 打飞、不做 push.y ×2；两类都转向攻击者、不看伤害一律打断招式。
"""
from __future__ import annotations

import os
import sys
import unittest

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
from test_botmelee import MeleeRoom                            # noqa: E402
from test_botsync import (BotFireRoom, TerrainMixin, bot_frames,  # noqa: E402
                          dash_frames, fire_frames, header, synth_terrain)

FIGHT = (0, gameserver.PVP_MODE_FIGHT, 0)


def weapon_frames(conn, seat):
    return [f for f in bot_frames(conn, seat)
            if header(f)["opcode"] == botsync.OP_CHANGE_WEAPON]


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

    def test_a_weak_fight_hit_still_stops_its_dash(self):
        """类型 2 / 3 不看伤害一律打断（`0x50a6f8`）；类型 0 要 ≥ 10（`HitBreaksBotDashTests.test_a_light_hit_does_not`）。"""
        swing = self.walk_until_dash()
        self.assertFalse(swing.hit, "前提：还没进伤害段")
        self.peer_event(self.alice, botsync.OP_SPLASH_DAMAGED, botsync.splash_body(
            botsync.projectile_handle(self.alice_seat(), 0),
            botsync.character_handle(self.bot_seat), 3.0, 130.0, 60.0,
            push_x=15.0, push_y=-4.5, kind=botfight.HIT_SLIDE))
        self.assertIsNone(self.bot_conn.dash_swing)


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

    def test_it_does_not_dash_while_locked(self):
        self.bot_conn.melee = True
        self.hit(3.0, (15.0, -4.5), botfight.HIT_SLIDE)
        self.human_heartbeat(self.alice, 660.0, self.FLOOR_Y, ticks=0)   # 贴到它跟前
        self.clear()
        self.advance(10)
        self.assertEqual([], dash_frames(self.alice, self.bot_seat))
        self.advance(10)
        self.assertTrue(dash_frames(self.alice, self.bot_seat), "锁一松就冲")

    def test_the_same_spot_dashes_at_once_without_the_hit(self):
        """对照组：同一个站位不挨打，10 帧之内就冲了 —— 上面那条不是因为这儿本来就冲不了。"""
        self.bot_conn.melee = True
        self.human_heartbeat(self.alice, 660.0, self.FLOOR_Y, ticks=0)
        self.clear()
        self.advance(10)
        self.assertTrue(dash_frames(self.alice, self.bot_seat))


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

    def test_frame_zero_is_two_frames_after_the_packet(self):
        self.assertIsNone(self.skill.frame(1))
        self.assertEqual([0, 1, 2], [self.skill.frame(r) for r in (2, 3, 4)])
        self.assertEqual(8, self.skill.frame(10))
        self.assertIsNone(self.skill.frame(11), "9 帧播完")
        self.assertEqual(11, self.skill.end_raw)

    def test_move_follows_the_track_and_the_facing(self):
        self.assertEqual([0, 0, 7, 3, 0], [self.skill.move_at(r) for r in range(2, 7)])
        left = botfight.HumanSkill(_FakeSkill(), -1, (0.0, 0.0), 10.0)
        self.assertEqual(-7, left.move_at(4))

    def test_the_damager_window(self):
        self.assertEqual([False, False, False, True, True, True, True, True, False],
                         [self.skill.damager_live(r) for r in range(2, 11)])

    def test_a_hitstop_freezes_four_frames_and_pushes_the_end(self):
        """打中在 raw 5 那一格 ⇒ raw 7..10 顿住：k 停在上一帧的 4、不挪、Damager 照样在；结束帧往后挪 4。"""
        self.skill.add_hitstop(10.0 + 5 * botfight.FRAME_S)
        self.assertEqual([0, 1, 2, 3, 4, 4, 4, 4, 4, 5, 6, 7, 8, None],
                         [self.skill.frame(r) for r in range(2, 16)])
        self.assertEqual([0, 0, 7, 3, 0, 0, 0, 0, 0], [self.skill.move_at(r) for r in range(2, 11)])
        self.assertTrue(self.skill.damager_live(8), "顿着的时候判定体原地不动、照样在")
        self.assertEqual(15, self.skill.end_raw)
        self.assertAlmostEqual(10.0 + 15 * botfight.FRAME_S, self.skill.end_time())

    def test_it_holds_the_keys_from_the_frame_after_the_press(self):
        """发出那一帧已经走过了；下一帧起键缓冲非空、接着招式对象在，一直到播完。"""
        self.assertEqual([False, True, True, True, False],
                         [self.skill.holds(r) for r in (0, 1, 2, 10, 11)])

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

    def skill(self, index=0, facing=1, x=300.0, y=None, kind=botsync.MUTU_SKILL_START):
        self.peer_event(self.alice, botsync.OP_MUTU_SKILL, botsync.mutu_skill_body(
            self.alice_seat(), kind, facing, index, x, self.FLOOR_Y if y is None else y))

    def xs(self, ticks):
        out = []
        for _ in range(ticks):
            self.advance(1)
            out.append(self.alice.sim_body.x)
        return out


class HumanSkillExtrapolationTests(HumanSkillRoom):
    """外推真人：招式那一段不按键走路、按 Move 曲线挪（P00：第 2 帧 7 px、第 3 帧 3 px）。"""

    def test_it_moves_along_the_track(self):
        self.skill()
        self.assertIsNotNone(self.alice.mutu_skill)
        # 第 1 格硬置成心跳；之后 raw 1, 2(k0), 3(k1), 4(k2 +7), 5(k3 +3), 6 …
        self.assertEqual([300.0, 300.0, 300.0, 300.0, 307.0, 310.0, 310.0], self.xs(7))

    def test_it_does_not_walk_with_the_heartbeat_keys(self):
        state = botsync.character_state(300.0, self.FLOOR_Y, keys=botsync.KEY_RIGHT)
        packet = botsync.build_peer_packet(self.alice_seat(), botsync.OP_HEARTBEAT,
                                           botsync.heartbeat_body(0, self.alice_seat(), state),
                                           game_id=self.room.epoch_value)
        gameserver.Conn.on_game_packet(self.alice, gameserver.OP_PEER_DATA_UP, packet)
        self.skill()
        self.assertEqual([300.0, 300.0, 300.0, 300.0, 307.0, 310.0], self.xs(6)[:6])

    def test_frame_zero_places_him_at_the_packet_coordinates(self):
        self.skill(x=320.0)
        self.assertEqual(320.0, self.xs(3)[-1], "第 0 帧之前收方硬置到包里的 (x, y)（`0x4935a7`）")

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
        self.assertEqual([0, 0, 0, 3, 3, 3, 3, 3, 0, 0], ranks)

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
        self.skill(index=4)               # K01：28 帧
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


if __name__ == "__main__":
    unittest.main()
