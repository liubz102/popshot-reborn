"""打飞重力 + 格斗房的物理档：服务端这一侧的挂点（X16 C1，X_Mod §120 / §122，D85）。

挨了类型 0、`int(伤害) ≥ 10` 的一发（没挡、不在免伤里），`OnHit 0x4ff669~0x4ff688` 置「打飞」`[+0x514]`：
落地 / 腾空撞上之前每帧重力是 f32(1.2) × 1.5 = 1.8000000715（`0x4feb62`）。服务端以前全程按 1.2 算，
被重击打飞的弧线偏高偏远。用户 2026-09-26：「这个也顺带修复吧」。三处都要认（D85）：

* bot 挨打 —— `_knock_back_seat` 顶飞它的同时挂上 `Body.fly`；
* 服务端当射手打中真人 / 看到真人互打 / bot 冲刺打中真人 —— `_human_knocked_flying`：外推的身体此刻
  多半还踩在地上（他被顶起来的那一发心跳还没到），先记 `sim_fly_pending`，第一发报腾空的心跳挂上去；
* 格斗房（模式 2，`IsMutu`）—— `_seat_shape` / `_character_of` 给出的角色对象挂 `fight`，
  `botmove` 换格斗档（每帧重力 ×1.5、跳高 240 / 300，起跳初速仍按基础 g）。
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
import botmove                                                 # noqa: E402
import botsync                                                 # noqa: E402
import gameserver                                              # noqa: E402
from test_botmelee import MeleeFieldRoom, MeleeRoom           # noqa: E402
from test_botsync import (BotFireRoom, HumanShotRoom,  # noqa: E402
                          TerrainMixin, synth_terrain)

f32 = botmove._f32

#: 打飞那一档每帧加到 vy 上的量：f32(1.2) × 1.5，x87 里乘完才加（f32 `0x3FE66667`）。
FLY_STEP = 1.8000000715255737


class BotFlyRoom(HumanShotRoom):
    """够高的平地（地面 400 那一行，脚在 399）+ 站在 (600, 399) 的 bot。

    缺省那张合成平地总高 180：挨一下 (12, −18) 头先撞图顶（图顶对角色是实心，X_Mod §105），
    验的就不再是打飞那一段弧线了。
    """

    FLOOR_Y = 399.0

    def setUp(self):
        super().setUp()
        self.install_terrain(synth_terrain("flat_tall", floor=400, height=440))
        self.place_bot(600.0, self.FLOOR_Y)
        # alice 站在左边报一发心跳（不推格子）：bot 见过真人的位置才开始自己走（同 `beats()` 的用法）。
        self.human_heartbeat(self.alice, 100.0, self.FLOOR_Y, ticks=0)


class BotKnockedFlyingTests(BotFlyRoom):
    """bot 挨打：`_knock_back_seat` 顶飞它的同时挂上打飞（`0x4ff679`）。"""

    def test_a_heavy_hit_knocks_it_flying(self):
        self.splash(20, (12.0, -9.0))
        body = self.bot_conn.body
        self.assertFalse(body.on_ground)
        self.assertTrue(body.fly)

    def test_exactly_ten_is_heavy_too(self):
        """置打飞看 `int(伤害) ≥ 10`（`0x4ff670`）；「> 10 才给速度」（`0x50f864`）是另一道门。"""
        self.splash(10, (12.0, -9.0))
        body = self.bot_conn.body
        self.assertEqual(bot.KNOCKBACK_MIN_LIFT, body.vy)
        self.assertTrue(body.fly)

    def test_a_light_hit_is_not(self):
        self.splash(9.9, (12.0, -9.0))                      # 截断成 9
        self.assertTrue(self.bot_conn.body.on_ground, "乙档只滑、不离地")
        self.assertFalse(self.bot_conn.body.fly)
        self.bot_conn.body = botmove.Body(600.0, 300.0, 1.0, -2.0, on_ground=False)
        self.splash(9.9, (12.0, -9.0))
        self.assertFalse(self.bot_conn.body.fly, "腾空挨轻的一发也不置")

    def test_a_shielded_bot_is_not(self):
        self.room.quest.shield_until = {self.bot_seat: self.now() + 8.0}
        self.splash(36, (15.0, -10.0))
        self.assertTrue(self.bot_conn.body.on_ground)
        self.assertFalse(self.bot_conn.body.fly)

    def test_its_flight_falls_with_the_heavier_gravity(self):
        """(12, −9) ⇒ 起手 vy −18（push.y ×2）；下一帧 vy += 1.8000000715，以前是 1.2。"""
        self.splash(20, (12.0, -9.0))
        self.assertEqual(-18.0, self.bot_conn.body.vy)
        self.advance(1)
        self.assertEqual(f32(-18.0 + FLY_STEP), self.bot_conn.body.vy)
        self.assertTrue(self.bot_conn.body.fly)

    def test_it_comes_down_when_the_client_says(self):
        """(12, −18) 从平地起：1.8 的弧线 20 帧落地（1.2 要 35 帧）—— 每台客户端上它就是这时候落地的。"""
        self.splash(20, (12.0, -9.0))
        frames = 0
        while not self.bot_conn.body.on_ground and frames < 60:
            self.advance(1)
            frames += 1
        self.assertEqual(20, frames)
        self.assertFalse(self.bot_conn.body.fly, "落地即清")


class HumanFlyRoom(MeleeFieldRoom):
    """alice 站在 (300, 399)；bot 摆远、站住、不开枪（同 `HumanDashExtrapolationTests`）。"""

    def setUp(self):
        super().setUp()
        self.place_bot(1300.0, self.FLOOR_Y)
        self.bot_conn.holding = True
        self.quiet_gun()
        self.alice_stands(300.0)
        self.advance(1)                                   # 外推硬置成这一发心跳

    def heavy_hit(self, damage=23.0, flags=0):
        """服务端看到 alice 挨了一发（bot 打中她 / 别人打中她都走这里，`_hit_breaks_melee`）。"""
        bot._hit_breaks_melee(self.room, self.alice_seat(), damage, flags, "单测",
                              now=self.now())

    def alice_airborne(self, vy=-20, vx=15, y=None, jumped=0):
        """alice 报一发腾空的心跳，推一格让外推硬置成它。"""
        self.alice_stands(300.0 + vx, y=self.FLOOR_Y - 30.0 if y is None else y,
                          on_ground=False, velocity=(vx, vy), jumped=jumped)
        self.advance(1)

    def body(self):
        return self.alice.sim_body

    def pending(self):
        return bool(getattr(self.alice, "sim_fly_pending", False))


class HumanKnockedFlyingTests(HumanFlyRoom):
    """真人挨了重击：外推他那份身体要在他被顶起来那一段换打飞重力（`_human_knocked_flying`）。"""

    def test_on_the_ground_it_waits_for_the_airborne_heartbeat(self):
        self.heavy_hit()
        self.assertTrue(self.pending())
        self.assertFalse(self.body().fly, "外推的身体还踩着地，挂上去下一帧就被踩地清掉")
        self.alice_airborne(vy=-20)
        self.assertTrue(self.body().fly)
        self.assertFalse(self.pending(), "挂上了就不再欠着")
        self.advance(1)
        self.assertEqual(f32(-20.0 + FLY_STEP), self.body().vy)

    def test_it_lasts_across_airborne_heartbeats_until_a_landing(self):
        """打飞不上线：后面几发腾空的心跳沿用外推的那一格；报踩地那一发起就没了，之后走下崖边也不再挂。"""
        self.heavy_hit()
        self.alice_airborne(vy=-20)
        self.alice_airborne(vy=-12, y=self.FLOOR_Y - 60.0)
        self.assertTrue(self.body().fly)
        self.alice_stands(330.0)
        self.advance(1)
        self.assertFalse(self.body().fly)
        self.alice_airborne(vy=2, y=self.FLOOR_Y - 10.0)
        self.assertFalse(self.body().fly)

    def test_a_heartbeat_from_before_the_hit_keeps_it_pending(self):
        """他那台可能还没收到这一发：踩地的心跳说明不了什么，接着欠着（不设时间窗）。"""
        self.heavy_hit()
        self.alice_stands(300.0)
        self.advance(1)
        self.assertTrue(self.pending())
        self.alice_airborne(vy=-20)
        self.assertTrue(self.body().fly)

    def test_already_in_the_air_it_flies_at_once(self):
        self.alice_airborne(vy=-12)
        self.assertFalse(self.body().fly)
        self.heavy_hit()
        self.assertTrue(self.body().fly)
        before = self.body().vy
        self.advance(1)
        self.assertEqual(f32(before + FLY_STEP), self.body().vy)

    def test_without_a_hit_the_air_is_the_old_gravity(self):
        self.alice_airborne(vy=-20)
        self.advance(1)
        self.assertEqual(f32(-20.0 + botmove.G32), self.body().vy)
        self.assertFalse(self.body().fly)

    def test_a_light_hit_does_nothing(self):
        self.heavy_hit(damage=9.9)
        self.assertFalse(self.pending())

    def test_a_guarded_hit_does_nothing(self):
        self.heavy_hit(flags=bot.EXPLODE_FLAG_GUARD)
        self.assertFalse(self.pending())

    def test_an_immune_human_does_nothing(self):
        self.room.quest.shield_until = {self.alice_seat(): self.now() + 8.0}
        self.heavy_hit()
        self.assertFalse(self.pending())

    def test_a_jump_from_the_ground_drops_it(self):
        """从地上起跳 = 那一段（要是有）早落地了 —— 飞得太短、一发心跳都没赶上（落地那一下清了 `[+0x514]`）。"""
        self.heavy_hit()
        self.alice_airborne(vy=-20, vx=0, jumped=1)
        self.assertFalse(self.body().fly)
        self.assertFalse(self.pending())
        self.advance(1)
        self.assertEqual(f32(-botmove.JUMP_SPEED + botmove.G32), self.body().vy)

    def test_a_jump_reported_a_few_frames_late_drops_it_too(self):
        """21:39:28 那一跳（重放）：心跳是起跳后第 3 帧才发的（vy −17，不是 −20）—— 段号 1 照样说明他落过地；
        以前只认起跳那一帧，欠着的打飞挂到了这次普通的跳上，整段每发心跳差 8 px。"""
        self.heavy_hit()
        self.alice_airborne(vy=-17, vx=-7, jumped=1)
        self.assertFalse(self.body().fly)
        self.assertFalse(self.pending())
        self.advance(1)
        self.assertEqual(f32(-17.0 + botmove.G32), self.body().vy)

    def test_lying_down_drops_it(self):
        """复活那一发心跳多半在半空（出生点挂在空中），别把死前那一下挂上去。"""
        self.heavy_hit()
        self.room.quest.respawn_due[self.alice_seat()] = (self.now() + 60.0, (100, 100))
        self.addCleanup(self.room.quest.respawn_due.pop, self.alice_seat(), None)
        self.advance(1)
        self.assertFalse(self.pending())

    def test_a_new_match_drops_it(self):
        self.heavy_hit()
        gameserver.reset_sync_trails(self.room, "单测", new_match=True)
        self.assertFalse(self.pending())

    def test_another_humans_hit_counts(self):
        """bob 的溅射 / 近身打中 alice：服务端转发时看到的这一发，同样记下（`0x4ff669` 在每台机器上都跑）。"""
        bob_seat = self.room.seat_index_of(self.bob)
        self.peer_event(self.bob, botsync.OP_SPLASH_DAMAGED, botsync.splash_body(
            botsync.projectile_handle(bob_seat, 0),
            botsync.character_handle(self.alice_seat()), 23.0, 300.0, 380.0,
            push_x=15.0, push_y=-10.0))
        self.assertTrue(self.pending())


class BotDashKnocksHumanFlyingTests(MeleeRoom):
    """bot 冲刺打中真人：那一支不经 `_hit_breaks_melee`（先 `_cancel_melee` 再算伤害），打飞单独记。"""

    def test_its_dash_hit_marks_the_human(self):
        swing = self.walk_until_dash()
        self.assertGreaterEqual(int(swing.move.damage), bot.MELEE_INTERRUPT_DAMAGE,
                                "前提：这一下够得上重击")
        self.assertTrue(self.hits_of(swing.handle), "前提：打中了")
        self.assertTrue(getattr(self.alice, "sim_fly_pending", False))


class FightRoomTests(TerrainMixin, BotFireRoom):
    """格斗房（모드 2 = 무투전，X_Mod §119）：角色对象挂 `fight`，外推真人 / bot 自己走都换格斗档。"""

    arguments = (0, gameserver.PVP_MODE_FIGHT, 0)
    FLOOR_Y = 399.0

    def setUp(self):
        super().setUp()
        self.install_terrain(synth_terrain("flat_tall", floor=400, height=440))
        self.place_bot(1300.0, self.FLOOR_Y)
        self.bot_conn.holding = True
        self.alice_seat = self.room.seat_index_of(self.alice)

    def test_the_room_is_a_fight_room(self):
        self.assertTrue(bot._fight_mode(self.room))

    def test_every_seat_is_shaped_for_the_fight(self):
        character_id = self.room.seats[self.alice_seat].character_id
        self.assertEqual((character_id, False, False, 0, True),
                         bot._seat_shape(self.room, self.alice_seat))
        who = bot._character_of(self.bot_conn)
        self.assertTrue(who.fight)
        self.assertIs(botmove.fight_physics(), botmove.physics_of(who))
        self.assertEqual(24.0, botmove.launch_speed(1, who))

    def test_a_human_fight_jump_is_recognised(self):
        """他那台一段跳初速 −24（跳高 240，g 仍取 1.2）：心跳报 −24 就是起跳那一帧，计时器 19 帧。"""
        self.human_heartbeat(self.alice, 300.0, self.FLOOR_Y, ticks=0)
        self.advance(1)
        self.human_heartbeat(self.alice, 300.0, self.FLOOR_Y - 24.0, jumped=1,
                             on_ground=False, velocity=(0, -24), ticks=0)
        self.advance(1)
        body = self.alice.sim_body
        self.assertEqual((-24.0, 19), (body.vy, body.rise))
        self.advance(1)
        self.assertEqual(f32(-24.0 + FLY_STEP), self.alice.sim_body.vy,
                         "格斗房每帧重力 ×1.5")


class NormalRoomShapeTests(TerrainMixin, BotFireRoom):
    """对照组：普通对战房（夺分）不挂 `fight`，形状键照旧。"""

    def test_nothing_changes_outside_the_fight_mode(self):
        seat = self.room.seat_index_of(self.alice)
        self.assertFalse(bot._fight_mode(self.room))
        self.assertEqual(self.room.seats[seat].character_id, bot._seat_shape(self.room, seat))
        self.assertFalse(bot._character_of(self.bot_conn).fight)


if __name__ == "__main__":
    unittest.main()
