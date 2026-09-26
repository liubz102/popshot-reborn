"""近身「谁打断谁」（X_Mod §111 / D75）：被推着不出手、挨打打断、伤害段比优先级。

用户 2026-09-26：「我先向 bot 发动近身攻击，明明已经打到 bot 了，但是我的攻击动画还没结束的
时候 bot 也向我发动近身攻击，此时 bot 会打到我，我扣血，而 bot 不会被扣血。」

原版的三条规则（逐指令，X_Mod §111）：
* 冲刺 / 出拳的**推挤段**碰到人发 `0x0017`，被推的人每台机器上都在播受击动作；
* **伤害段**碰到人先比招式优先级（`0x503fde`），不比对方低就把对方的冲刺 / 出拳停掉，
  每台机器都这么判（所以 bot 一旦冲出去，服务端事后怎么判都救不回真人那一招）；
* `Character::OnHit` 里类型 0、`int(伤害) ≥ 10` 的一发也把受害者的招停掉。
"""
from __future__ import annotations

import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import bot                                                     # noqa: E402
import botsync                                                 # noqa: E402
import chrprops                                                # noqa: E402
import gameserver                                              # noqa: E402
from gameserver import OP_PEER_DATA_UP                         # noqa: E402
from test_botsync import (BotFireRoom, body_of, dash_frames,   # noqa: E402
                          fire_frames, splash_frames)

#: 真人的冲刺打中人时，包里的推力是常量 `(朝向 × 15, −10)`（`0x481d6e`，语料 1347 发）。
DASH_PUSH = (15.0, -10.0)


class MeleeRoom(BotFireRoom):
    """个人战房 + bot 会近身（`BotFireRoom` 默认把近身关了，这里打开）。"""

    melee = True

    def alice_seat(self):
        return self.room.seat_index_of(self.alice)

    def peer_event(self, conn, opcode, body):
        """真人发一发事件包（走真的 `0x040e` 入口），时刻对齐房间那一格。

        ★ 单测里挂钟几乎不走、模拟时钟一格 32 ms 地往前跑（`now()` 的说明）——
          不对齐的话真人那一招的收尾时刻会落在「过去」，推挤一挂上就解了。
        """
        seat = self.room.seat_index_of(conn)
        packet = botsync.build_peer_packet(seat, opcode, body,
                                           game_id=self.room.epoch_value,
                                           sequence=self.next_seq(conn))
        with bot._tick_clock(self.now()):
            gameserver.Conn.on_game_packet(conn, OP_PEER_DATA_UP, packet)

    def alice_dashes(self, index=0):
        """alice 双击方向键冲了一下（`rpDash`）。"""
        self.peer_event(self.alice, botsync.OP_DASH,
                        botsync.dash_body(self.alice_seat(), 1, index, 100.0, 100.0))

    def alice_jabs(self, index=0):
        """alice 出了一拳（`0x0008`，和 `rpDash` 同一种 11 字节的包）。"""
        self.peer_event(self.alice, 0x0008,
                        botsync.dash_body(self.alice_seat(), 1, index, 100.0, 100.0))

    def alice_pushes_bot(self):
        """alice 那一招的推挤段贴住了 bot（`0x0017`：受约束的 bot ← 出招的 alice）。"""
        self.peer_event(self.alice, 0x0017, struct.pack(
            "<ii", botsync.character_handle(self.bot_seat),
            botsync.character_handle(self.alice_seat())))

    def alice_splashes_bot(self, damage):
        """alice 的一发 `rpSplashDamaged` 打在 bot 身上（她冲刺的伤害就走这一路）。"""
        body = botsync.splash_body(botsync.projectile_handle(self.alice_seat(), 0),
                                   botsync.character_handle(self.bot_seat),
                                   damage, 130.0, 60.0,
                                   push_x=DASH_PUSH[0], push_y=DASH_PUSH[1])
        self.peer_event(self.alice, botsync.OP_SPLASH_DAMAGED, body)

    def walk_until_dash(self):
        """alice 一格一格走近，bot **刚冲出去**那一格就停下，返回那一下（`DashSwing`）。

        `approach()` 一口气推完 16 格，那一下的伤害段早就判完了 —— 要在伤害段之前插手
        （打断它、让真人出拳）就得逐格看。
        """
        for point in ((100.0, 100.0), (120.0, 100.0), (140.0, 100.0)):
            self.human_heartbeat(self.alice, *point, ticks=0)
            for _ in range(gameserver.HEARTBEAT_TICKS):
                self.advance(1)
                if self.bot_conn.dash_swing is not None:
                    return self.bot_conn.dash_swing
        self.fail("走到跟前了 bot 还没冲（前提不成立）")

    def hits_of(self, handle, ticks=60):
        """再推 `ticks` 格，返回伤害源是这一下（`handle`）的那几发 `rpSplashDamaged`。"""
        for _ in range(ticks):
            self.advance(1)
        return [f for f in splash_frames(self.alice, self.bot_seat)
                if struct.unpack_from("<i", body_of(f), 0)[0] == handle]

    def alice_move(self):
        return chrprops.get(self.room.seats[self.alice_seat()].character_id)


class PushedBotHoldsTests(MeleeRoom):
    """被真人的近身招式推着（`0x0017`）的这一段，bot 不冲、不开枪（D75）。"""

    def test_a_pushed_bot_does_not_dash_back(self):
        """16:35:30 那一幕：alice 的招贴住 bot 之后 8 ms，bot 就冲回来抢断了她。"""
        self.alice_dashes()
        self.alice_pushes_bot()
        self.assertIsNotNone(self.bot_conn.motion_constraint)
        self.approach()          # 同一段走位，没被推时 bot 会冲（`BotDashTests`）
        self.assertLess(self.now(), self.alice.motion_action[1],
                        "前提：这一段走位还在 alice 那一招里")
        self.assertEqual([], dash_frames(self.alice, self.bot_seat),
                         "被推着就不该反手冲回来")
        self.assertIsNone(self.bot_conn.dash_swing)

    def test_a_pushed_bot_does_not_shoot_either(self):
        """拦了冲刺，同一格就落到开枪那一支 —— 贴脸一枪 ≥ 10 点照样打断她（`0x4ff669`）。"""
        self.bot_conn.melee = False
        self.alice_dashes()
        self.alice_pushes_bot()
        self.approach()
        self.assertLess(self.now(), self.alice.motion_action[1])
        self.assertEqual([], fire_frames(self.alice, self.bot_seat),
                         "被推着也不该开枪")

    def test_it_acts_again_once_the_move_is_over(self):
        """解除跟着出招者那一招的原版帧数（`TotalFrame`）走，不另设时间阈值。"""
        self.alice_dashes()
        self.alice_pushes_bot()
        self.approach()
        self.assertEqual([], dash_frames(self.alice, self.bot_seat))
        end = self.alice.motion_action[1]
        while self.now() <= end:
            self.advance(1)
        self.approach()
        self.assertTrue(dash_frames(self.alice, self.bot_seat),
                        "alice 那一招收完了，bot 该照常近身")
        self.assertIsNone(self.bot_conn.motion_constraint)

    def test_a_hit_on_the_bot_ends_the_push(self):
        """挨打那一下原版就清约束（`0x4ff47c` / `0x50f954`），哪怕伤害不到 10。"""
        self.alice_dashes()
        self.alice_pushes_bot()
        self.alice_splashes_bot(6.0)
        self.assertIsNone(self.bot_conn.motion_constraint)
        self.assertFalse(bot._held_by_push(self.room, self.bot_conn, self.now()))

    def test_the_push_ends_when_the_pusher_is_hit_hard(self):
        """推它的人自己被打断（`int(伤害) ≥ 10`），`0x50a63c` 不再成立，约束当场解。"""
        self.alice_dashes()
        self.alice_pushes_bot()
        now = self.now()
        self.assertTrue(bot._held_by_push(self.room, self.bot_conn, now))
        bot._hit_breaks_melee(self.room, self.alice_seat(), 23, 0, "test", now=now)
        self.assertLessEqual(self.alice.motion_action[1], now)
        self.assertFalse(bot._held_by_push(self.room, self.bot_conn, now))
        self.assertIsNone(self.bot_conn.motion_constraint)


class HitBreaksBotDashTests(MeleeRoom):
    """bot 挨了类型 0、`int(伤害) ≥ 10` 的一发，它那一下冲刺当场停（`Character::OnHit` `0x4ff669`）。

    每台客户端上它那一下都已经停了；服务端不停的话，后面几帧照样判中、照样发伤害。
    """

    def test_a_heavy_hit_stops_the_dash(self):
        swing = self.walk_until_dash()
        self.assertFalse(swing.hit, "前提：还没进伤害段")
        self.clear()
        self.alice_splashes_bot(36.0)
        self.assertIsNone(self.bot_conn.dash_swing)
        self.assertEqual([], self.hits_of(swing.handle),
                         "停掉的那一下不该再打中人")

    def test_a_light_hit_does_not(self):
        swing = self.walk_until_dash()
        self.alice_splashes_bot(9.9)          # 截断成 9，不到 10
        self.assertIs(swing, self.bot_conn.dash_swing)

    def test_an_immune_bot_keeps_it(self):
        """免伤 / 护盾里 `OnHit` 进门就返回，打断那一步走不到（`_immune`）。"""
        swing = self.walk_until_dash()
        self.room.quest.shield_until = {self.bot_seat: self.now() + 8.0}
        self.alice_splashes_bot(36.0)
        self.assertIs(swing, self.bot_conn.dash_swing)


class BotDashPriorityTests(MeleeRoom):
    """bot 的伤害段碰到人：比优先级（`0x503fde`），打中了就把对方的招停掉（`0x481cc4`）。"""

    def test_its_hit_cuts_the_humans_dash_short(self):
        swing = self.walk_until_dash()
        self.alice_dashes()                    # 双方都在冲：同级（1），谁先进伤害段谁赢
        end = self.alice.motion_action[1]
        self.clear()
        hits = self.hits_of(swing.handle)
        self.assertTrue(hits, "贴着冲，这一下该打中")
        self.assertLess(self.alice.motion_action[1], end,
                        "被打中那一刻 alice 那一招就停了，服务端要跟着收")

    def test_a_jab_in_its_strike_frames_is_not_hit(self):
        """出拳在伤害段是 2，比冲刺的 1 高 ⇒ 碰到她也不算（`0x481cb8 jg`）。"""
        swing = self.walk_until_dash()
        self.alice.jab_until = self.now() + 60.0
        self.alice.jab_strike = (0.0, self.now() + 60.0)
        self.clear()
        self.assertEqual([], self.hits_of(swing.handle),
                         "她在出拳的伤害段，bot 这一下碰到她不算")
        self.assertIn(self.alice_seat(), swing.outranked)

    def test_the_jab_strike_window_follows_the_original_frames(self):
        """伤害段 = ⌊CastEndFrame × 0.625⌋ ≤ 帧 < ⌊DamageEndFrame × 0.625⌋（`0x482d65`）。"""
        move = self.alice_move().move("jab0")
        self.assertIsNotNone(move, "前提：alice 的角色有 Jab00")
        start = self.now()
        self.alice_jabs()
        frame_s = bot.BOT_DASH_FRAME_MS / 1000.0
        cast = int(move["cast_end"] * 0.625)
        until = int(move["damage_end"] * 0.625)
        self.assertLess(cast, until, "前提：伤害段不空")
        strike = self.alice.jab_strike
        self.assertAlmostEqual(start + cast * frame_s, strike[0], places=6)
        self.assertAlmostEqual(start + until * frame_s, strike[1], places=6)
        seat = self.alice_seat()
        self.assertEqual(bot.MELEE_PRIORITY_IDLE,
                         bot._melee_priority(self.room, seat, strike[0] - 0.001),
                         "起手那几帧不算")
        self.assertEqual(bot.MELEE_PRIORITY_JAB_STRIKE,
                         bot._melee_priority(self.room, seat, strike[0]))
        self.assertEqual(bot.MELEE_PRIORITY_IDLE,
                         bot._melee_priority(self.room, seat, strike[1]),
                         "伤害段上界不含")

    def test_a_dashing_human_ranks_one(self):
        self.alice_dashes()
        now = self.now()
        self.assertEqual(bot.MELEE_PRIORITY_DASH,
                         bot._melee_priority(self.room, self.alice_seat(), now))
        self.assertEqual(bot.MELEE_PRIORITY_IDLE,
                         bot._melee_priority(self.room, self.alice_seat(),
                                             self.alice.motion_action[1]))


if __name__ == "__main__":
    unittest.main()
