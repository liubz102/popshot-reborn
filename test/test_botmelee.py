"""近身「谁打断谁」（X_Mod §111 / D75）：被推着不出手、挨打打断、伤害段比优先级。

用户 2026-09-26：「我先向 bot 发动近身攻击，明明已经打到 bot 了，但是我的攻击动画还没结束的
时候 bot 也向我发动近身攻击，此时 bot 会打到我，我扣血，而 bot 不会被扣血。」

原版的三条规则（逐指令，X_Mod §111）：
* 冲刺 / 出拳的**推挤段**碰到人发 `0x0017`，被推的人每台机器上都在播受击动作；
* **伤害段**碰到人先比招式优先级（`0x503fde`），不比对方低就把对方的冲刺 / 出拳停掉，
  每台机器都这么判（所以 bot 一旦冲出去，服务端事后怎么判都救不回真人那一招）；
* `Character::OnHit` 里类型 0、`int(伤害) ≥ 10` 的一发也把受害者的招停掉。

会话 45（X_Mod §113 / §114 / D79，用户 22:17「又是我先发却被打」「看着打中了没扣血」）：他先冲出来、那一招碰得到 bot 就先让他；
被推着的 bot 不自己挪；外推真人冲刺照 `ProcessDash` 挪。

会话 46（X_Mod §116 / D81，用户 2026-09-27「bot 近身攻击后我闪现回原位置」「又有一两次我先发却被 bot 打」）：
冲刺那一段心跳朝向锁在冲的方向；`0x0017` 先解出招者自己的约束；bot 冲出去的路上撞得进他先出的那一招就不冲。
"""
from __future__ import annotations

import os
import struct
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import bot                                                     # noqa: E402
import botmove                                                 # noqa: E402
import botsync                                                 # noqa: E402
import chrprops                                                # noqa: E402
import gameserver                                              # noqa: E402
import udpsync                                                 # noqa: E402
from gameserver import OP_PEER_DATA_UP                         # noqa: E402
from test_botsync import (BotFireRoom, HumanShotRoom, TerrainMixin,  # noqa: E402
                          body_of, bot_frames, dash_frames, fire_frames, header,
                          splash_frames, synth_terrain)

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


class BotDashFrameWindowTests(MeleeRoom):
    """bot 的冲刺只在 `CastEndFrame` ≤ 帧 < `DamageEndFrame` 判中（`0x481bba jge`，上界不含，X_Mod §111）。

    以前含上界、多判一帧：11:29:59 和 21:40:16 都是第 11 帧（`Dash00` DamageEnd = 11）打中的。
    """

    def test_the_damage_end_frame_itself_is_never_checked(self):
        move = chrprops.get(self.bot_conn.character_id).dash(bot.BOT_DASH_INDEX)
        asked = []
        original = bot._dash_hits

        def spy(room, swing, x, y, frame, bodies):
            asked.append(frame)
            return None

        bot._dash_hits = spy
        self.addCleanup(setattr, bot, "_dash_hits", original)
        self.bot_conn.battle_pos = (100.0, 100.0)
        now = self.now()
        # 早就冲完了：这一格一口气把整段伤害帧补判完（追赶那一路）。
        self.bot_conn.dash_swing = bot.DashSwing(
            botsync.projectile_handle(self.bot_seat, 0), now - 10.0, 1, move,
            self.bot_conn.character_id)
        bot._advance_dash(self.room, self.bot_conn, now)
        self.assertEqual(list(range(move.cast_end, move.damage_end)), asked)
        self.assertIsNone(self.bot_conn.dash_swing, "整套动作早过完了")


class BotDashGapTests(MeleeRoom):
    """上一下结束（打完或被打断）后隔 `BOT_DASH_GAP_S`（0.3 秒）才能出下一下 —— 用户 2026-09-26 定的，原版没有。"""

    def ready_next_to_alice(self):
        """冲过一下、人还贴着：把那一下收掉、体力补满，只剩「隔多久」这一道门。"""
        self.walk_until_dash()
        self.bot_conn.dash_swing = None
        self.bot_conn.stamina = bot._stamina_cap(self.bot_conn)
        self.bot_conn.stamina_at = self.now()

    def try_dash(self, at):
        return bot._try_dash(self.room, self.bot_conn, self.bot_seat, at, True)

    def test_it_waits_three_tenths_after_the_last_one_ended(self):
        self.ready_next_to_alice()
        ended = self.now()
        self.bot_conn.dash_ended_at = ended
        self.assertFalse(self.try_dash(ended + bot.BOT_DASH_GAP_S - 0.01),
                         "上一下刚收 0.29 秒，不该再冲")
        self.assertIsNone(self.bot_conn.dash_swing)
        self.assertTrue(self.try_dash(ended + bot.BOT_DASH_GAP_S + 0.01),
                        "隔够 0.3 秒、够得着、体力够，就该冲")

    def test_the_first_dash_is_not_held_back(self):
        self.ready_next_to_alice()
        self.bot_conn.dash_ended_at = None
        self.assertTrue(self.try_dash(self.now()))

    def test_a_natural_end_is_stamped_at_its_total_frame(self):
        move = chrprops.get(self.bot_conn.character_id).dash(bot.BOT_DASH_INDEX)
        self.bot_conn.battle_pos = (100.0, 100.0)
        born = self.now() - 10.0
        self.bot_conn.dash_swing = bot.DashSwing(
            botsync.projectile_handle(self.bot_seat, 0), born, 1, move,
            self.bot_conn.character_id)
        bot._advance_dash(self.room, self.bot_conn, self.now())
        self.assertIsNone(self.bot_conn.dash_swing)
        self.assertAlmostEqual(born + move.total_frame * bot.BOT_DASH_FRAME_MS / 1000.0,
                               self.bot_conn.dash_ended_at, places=6)

    def test_a_cut_dash_counts_from_the_cut(self):
        swing = self.walk_until_dash()
        cut = self.now()
        bot._cancel_melee(self.room, self.bot_seat, cut, "test")
        self.assertIsNone(self.bot_conn.dash_swing)
        self.assertEqual(cut, self.bot_conn.dash_ended_at)
        self.assertIsNot(swing, self.bot_conn.dash_swing)


class BotStaminaTests(MeleeRoom):
    """体力（`GameProps.ini`）：快跑每格 −1.5、平时每格 +0.25，**冲刺那一段不回也不扣**（X_Mod §112）。

    用户 2026-09-26：「bot 有时甚至可以连续 3 发」「bot 加速跑时是否真的扣了体力」。
    """

    TICK = 1.0 / 31.25          # 一格 32 ms（`ballistics.TICKS_PER_SECOND`）

    def setUp(self):
        super().setUp()
        self.props = chrprops.game()
        self.move = chrprops.get(self.bot_conn.character_id).dash(bot.BOT_DASH_INDEX)
        self.t = self.now()
        self.bot_conn.dash_swing = None

    def fill(self, value):
        self.bot_conn.stamina = float(value)
        self.bot_conn.stamina_at = self.t

    def step(self, ticks=1, **keys):
        for _ in range(ticks):
            self.t += self.TICK
            bot._regen_stamina(self.bot_conn, self.t, **keys)
        return self.bot_conn.stamina

    def test_fast_running_really_costs_stamina(self):
        self.fill(80.0)
        self.step(10, fast_run=True)
        net = self.props.sp_charging - self.props.fast_run_sp_cost     # −1.25
        self.assertAlmostEqual(80.0 + 10 * net, self.bot_conn.stamina, places=3)

    def test_nothing_comes_back_during_a_dash(self):
        """`ProcessMove` 在 `ProcessDash` 返回真时整段跳过（`0x507116`）：回复和快跑扣减都不走。"""
        self.fill(70.0)
        self.bot_conn.dash_swing = bot.DashSwing(
            botsync.projectile_handle(self.bot_seat, 0), self.t, 1, self.move,
            self.bot_conn.character_id)
        self.step(self.move.total_frame)
        self.assertAlmostEqual(70.0, self.bot_conn.stamina, places=3)
        self.step(self.move.total_frame, fast_run=True)
        self.assertAlmostEqual(70.0, self.bot_conn.stamina, places=3, msg="冲刺中快跑位也不扣")
        self.bot_conn.dash_swing = None
        self.step(8)
        self.assertAlmostEqual(70.0 + 8 * self.props.sp_charging,
                               self.bot_conn.stamina, places=3)

    def chain(self, move):
        """满体力起一下接一下地冲，直到体力不够；返回冲了几下（每下之间只隔收招后那一格）。"""
        self.fill(bot._stamina_cap(self.bot_conn))
        chained = 0
        while self.bot_conn.stamina >= move.sp_cost:
            self.bot_conn.stamina -= move.sp_cost
            chained += 1
            self.bot_conn.dash_swing = bot.DashSwing(
                botsync.projectile_handle(self.bot_seat, chained), self.t, 1,
                move, self.bot_conn.character_id)
            self.step(move.total_frame)               # 这一下打完之前不回
            self.bot_conn.dash_swing = None
            self.step(1)                              # 收招后那一格
        return chained

    def test_back_to_back_dashes_regain_only_between_them(self):
        """冲刺那几帧一点不回：连冲 n 下之后 = 上限 − n × 花费 + n × 0.25（以前每下还白回 `TotalFrame × 0.25`）。"""
        chained = self.chain(self.move)
        self.assertGreaterEqual(chained, 1)
        want = (bot._stamina_cap(self.bot_conn) - chained * self.move.sp_cost
                + chained * self.props.sp_charging)
        self.assertAlmostEqual(want, self.bot_conn.stamina, places=3)

    def test_character_zero_chains_three_then_waits(self):
        """角色 0 第 0 式（30 / 上限 100）：100 → 70 → 40 → 10，第 4 下要等两秒多 —— 没装突击技的真人也一样。"""
        self.bot_conn.character_id = 0
        move = chrprops.get(0).dash(0)
        self.assertEqual((30.0, 100.0), (move.sp_cost, bot._stamina_cap(self.bot_conn)))
        self.assertEqual(3, self.chain(move))
        wait = (move.sp_cost - self.bot_conn.stamina) / self.props.sp_charging * self.TICK
        self.assertGreater(wait, 2.0)

    def test_the_cap_follows_the_characters_chrsp(self):
        """上限 = `ChrSp`（`0x50a0aa`：角色表 `+4` + 装备键 6，bot 没装备），不是 `GameProps` 的 `SpMax`。"""
        for character, want in ((0, 100.0), (3, 110.0), (101, 90.0)):
            self.bot_conn.character_id = character
            self.bot_conn.stamina = None
            bot._regen_stamina(self.bot_conn, self.t)
            self.assertEqual(want, self.bot_conn.stamina, "角色 %d 满体力" % character)
            self.step(40)
            self.assertEqual(want, self.bot_conn.stamina, "角色 %d 回满不越上限" % character)

    def test_standing_up_again_refills_it(self):
        """`Respawn` 站起来就补满（`0x503080`）；以前接着死前那个数、按躺着的时长慢慢回。"""
        self.human_heartbeat(self.alice, 100.0, 100.0)   # 有人报过位置，bot 才走得到回体力那一步
        self.bot_conn.holding = True                     # 站住：不冲不跑，体力只剩「回」这一件事
        self.bot_conn.stamina = 5.0
        self.room.quest.respawn_due[self.bot_seat] = (self.now() + 60.0, (100, 100))
        self.advance(1)
        self.assertIsNone(self.bot_conn.stamina, "躺着这段清掉，站起来按上限补")
        self.room.quest.respawn_due.pop(self.bot_seat)
        self.advance(1)
        self.assertEqual(bot._stamina_cap(self.bot_conn), self.bot_conn.stamina)


class ImmuneKnockbackTests(HumanShotRoom):
    """免伤 / 护盾里 `Character::OnHit` 进门就返回：不扣血、不击退、不解约束（X_Mod §112）。"""

    def test_a_shielded_bot_is_not_knocked_back(self):
        self.room.quest.shield_until = {self.bot_seat: time.monotonic() + 8.0}
        self.splash(36, (15.0, -10.0))
        body = self.bot_conn.body
        self.assertTrue(body.on_ground, "护盾里不该被顶飞")
        self.assertEqual((0.0, 0.0), (body.vx, body.vy))
        self.room.quest.shield_until = {self.bot_seat: time.monotonic() - 1.0}
        self.splash(36, (15.0, -10.0))
        self.assertFalse(self.bot_conn.body.on_ground, "护盾过了照常顶飞")

    def test_a_shielded_bot_stays_pushed(self):
        self.send(botsync.OP_DASH, botsync.dash_body(self.alice_seat, 1, 0, 600.0, 150.0))
        self.send(0x0017, struct.pack("<ii", botsync.character_handle(self.bot_seat),
                                      botsync.character_handle(self.alice_seat)))
        self.assertIsNotNone(self.bot_conn.motion_constraint)
        self.room.quest.shield_until = {self.bot_seat: time.monotonic() + 8.0}
        self.splash(36, (15.0, -10.0))
        self.assertIsNotNone(self.bot_conn.motion_constraint, "进不了门，约束也解不掉（`0x4ff47c` 在免伤门后面）")


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


class MeleeFieldRoom(TerrainMixin, MeleeRoom):
    """一块够高的平地（地面 400 那一行，脚在 399）+ 站在 x=700 的 bot（X_Mod §113）。

    外推真人冲刺、预判「他那一招打不打得到」、被推着的 bot 怎么挪 —— 这三样都要地形。
    alice 是角色 0（第 3 式：头一帧 131 px、第 17 帧进伤害段）；bot 是角色 2（第 0 式够到 90）。
    """

    FLOOR_Y = 399.0

    def setUp(self):
        super().setUp()
        self.install_terrain(synth_terrain("flat_tall", floor=400, height=440))
        self.place_bot(700.0, self.FLOOR_Y)

    def alice_stands(self, x, **extra):
        """alice 在 `(x, 399)` 发一发心跳，**不**推格子（紧接着的冲刺和它排在同一帧附近）。"""
        extra.setdefault("ticks", 0)
        self.human_heartbeat(self.alice, x, extra.pop("y", self.FLOOR_Y), **extra)

    def alice_dash_at(self, x, direction=1, index=3, y=None):
        self.peer_event(self.alice, botsync.OP_DASH, botsync.dash_body(
            self.alice_seat(), direction, index, x,
            self.FLOOR_Y if y is None else y))

    def quiet_gun(self):
        """这一批只看近身：把开枪推到很久以后（同 `settle()` 的做法）。"""
        self.bot_conn.next_fire_at = time.monotonic() + 3600.0

    def dash_step(self, frame, direction=1):
        who = chrprops.get(0)
        return botmove.dash_distance(who.dash(3), frame, direction, who.speed)


class HumanDashExtrapolationTests(MeleeFieldRoom):
    """外推真人：冲刺那一段照原版自己挪、不按键走路（`ProcessDash`，X_Mod §113）。

    第 0 帧在他那台发出 `rpDash` 之后第 2 帧才挪（`HUMAN_DASH_START_FRAMES`）。
    """

    def setUp(self):
        super().setUp()
        self.place_bot(1300.0, self.FLOOR_Y)       # 离远点、别打断他
        self.bot_conn.holding = True
        self.quiet_gun()

    def alice_x(self):
        return self.alice.sim_body.x

    def test_a_ground_dash_follows_the_original_profile(self):
        self.alice_stands(300.0)
        self.alice_dash_at(300.0)
        self.advance(1)                              # 硬置成心跳：还没开始挪
        self.assertEqual(300.0, self.alice_x())
        self.advance(1)                              # 他那台 StartDash 那一帧
        self.assertEqual(300.0, self.alice_x())
        self.advance(1)                              # 第 0 帧
        self.assertEqual(300.0 + int(self.dash_step(0)), self.alice_x())
        self.advance(12)                             # 第 1~12 帧
        self.assertAlmostEqual(300.0 + 273.0, self.alice_x(), delta=1.0)
        self.advance(4)
        self.assertAlmostEqual(300.0 + 273.0, self.alice_x(), delta=1.0,
                               msg="MoveFrame 之后不挪，也不照着心跳的键走")

    def test_it_no_longer_walks_with_the_heartbeat_keys_during_a_jab(self):
        self.human_heartbeat(self.alice, 300.0, self.FLOOR_Y, ticks=0)
        self.alice.sync_trail[-1] = self.alice.sync_trail[-1]._replace(
            keys=botsync.KEY_RIGHT)                  # 他按着 →（心跳里的键）
        self.peer_event(self.alice, 0x0008, botsync.dash_body(
            self.alice_seat(), 1, 0, 300.0, self.FLOOR_Y))
        self.advance(4)
        self.assertEqual(300.0, self.alice_x(), "出拳那一段 ProcessMove 整段跳过")

    def test_an_air_dash_rewrites_the_speed_every_frame(self):
        """22:13:57：心跳报 vx 118（第 0 帧），以前服务端拿它当普通腾空速度连积 4 格、把被推的 bot 推飞。"""
        self.alice_stands(600.0, y=200.0, on_ground=False)
        self.alice_dash_at(600.0, y=200.0)
        # 冲刺那一发排在这发心跳之前 3 帧：心跳里已经走完第 0 帧（vx 118），下一格走第 2 帧。
        self.human_heartbeat(self.alice, 718.0, 219.0, on_ground=False,
                             velocity=(118, 19), ticks=0)
        self.alice.motion_grid = self.alice.sync_trail_at - 3 * 0.032
        self.advance(1)                              # 硬置
        self.assertEqual(1, self.alice.sim_dash_frame)
        self.advance(4)
        moved = self.alice_x() - 718.0
        want = sum(f32 * 0.9 for f32 in (self.dash_step(f) for f in range(2, 6)))
        self.assertAlmostEqual(want, moved, delta=3.0)
        self.assertLess(moved, 118.0, "不再按 118 一格格地积")

    def test_the_frame_count_lines_up_on_the_heartbeat_grid(self):
        """帧号 = 这一格离最近那发心跳几格 + 那发心跳离冲刺几格 − 2（同 rpJump 的数法，§85）。"""
        conn = type("Conn", (), {})()
        conn.motion_kind = "dash"
        conn.motion_grid = 100.0
        conn.sim_body_at = 100.0 + 5 * 0.032          # 冲刺之后第 5 帧到的心跳
        self.assertEqual([3, 4, 5], [bot._human_dash_frame(conn, s) for s in (0, 1, 2)])
        conn.sim_body_at = 100.0 - 1 * 0.032          # 冲刺之前那一发
        self.assertEqual([-3, -2, -1, 0],
                         [bot._human_dash_frame(conn, s) for s in range(4)])
        conn.motion_kind = "jab"
        self.assertIsNone(bot._human_dash_frame(conn, 3))


class YieldToMeleeTests(MeleeFieldRoom):
    """他先冲出来、那一招接下来碰得到 bot：推挤段还没碰到时 bot 也先让他 —— 不冲、不开枪（X_Mod §113 / D79）。

    22:16:56：真人第 3 式冲出去 191 ms 后 bot 才冲，比服务端收到 `0x0017` 早 4 ms，D75 那条来不及挂上；
    bot 第 0 式第 6 帧就进伤害段，按原版把他那一招抢断了。
    """

    #: alice 站的地方：离 bot 250 px —— bot 的第 0 式冲出去（角色 2 二十帧挪 225 px）正好打得到她（对照用例），
    #: 她的第 3 式第 2 帧推挤段也碰得到 bot（X_Mod §115 之后 bot 贴脸不冲，得在它的冲刺够得着的距离上验）。
    ALICE_X = 450.0

    def test_it_does_not_counter_a_dash_that_is_already_coming(self):
        self.quiet_gun()
        self.alice_stands(self.ALICE_X)
        self.alice_dash_at(self.ALICE_X)
        self.clear()
        self.advance(8)
        self.assertEqual([], dash_frames(self.alice, self.bot_seat),
                         "他先冲的、打得到我 —— 不该反手")
        self.assertIsNone(self.bot_conn.dash_swing)
        self.assertEqual((self.alice_seat(), self.alice.motion_action_mark),
                         self.bot_conn.melee_yield, "锁住的是他那一下")

    def test_it_keeps_yielding_after_his_dash_passes_it(self):
        """服务端这份外推里被推那一下还没发生（`0x0017` 在路上），他会「穿过」bot —— 锁着那一下，别在这一格出手。"""
        self.quiet_gun()
        self.alice_stands(560.0)
        self.alice_dash_at(560.0)
        self.advance(6)
        self.assertGreater(self.alice.sim_body.x, self.bot_conn.body.x,
                           "前提：外推里他已经冲过了 bot")
        self.assertIsNone(bot._melee_threat(self.room, self.bot_conn, self.bot_seat,
                                            self.now(), bot._terrain(self.room)),
                          "前提：只看剩下几帧的话已经碰不到了")
        self.assertTrue(bot._yield_to_melee(self.room, self.bot_conn, self.bot_seat,
                                            self.now(), bot._terrain(self.room)))

    def test_the_same_spot_without_a_dash_does_get_a_counter(self):
        """对照：同一个距离她没在出招，bot 照常冲过去。"""
        self.quiet_gun()
        self.alice_stands(self.ALICE_X)
        self.clear()
        self.advance(8)
        self.assertTrue(dash_frames(self.alice, self.bot_seat), "前提：这个距离 bot 会冲")

    def test_it_does_not_shoot_into_a_dash_that_is_already_coming(self):
        self.bot_conn.melee = False
        self.alice_stands(self.ALICE_X)
        self.alice_dash_at(self.ALICE_X)
        self.clear()
        self.advance(8)
        self.assertEqual([], fire_frames(self.alice, self.bot_seat),
                         "贴脸一枪 ≥ 10 点照样在 OnHit 里打断他（`0x4ff669`）")

    def test_a_first_frame_lunge_right_through_it_still_counts(self):
        """第 3 式头一帧挪 131 px：落点的圈（身前 20、半径 40）已经在 bot 身后了，可一路是扫过去的（`0x4814f2`）。"""
        self.quiet_gun()
        self.place_bot(640.0, self.FLOOR_Y)
        self.alice_stands(600.0)
        self.alice_dash_at(600.0)
        self.advance(1)
        landing = 600.0 + int(self.dash_step(0)) + 20.0
        self.assertGreater(landing - 40.0, 640.0 + 18.0, "前提：只看第 0 帧落点的话碰不到")
        self.assertIsNotNone(bot._melee_threat(self.room, self.bot_conn, self.bot_seat,
                                               self.now(), bot._terrain(self.room)))
        self.clear()
        self.advance(8)
        self.assertEqual([], dash_frames(self.alice, self.bot_seat))

    def test_a_dash_going_the_other_way_is_no_threat(self):
        self.alice_stands(560.0)
        self.alice_dash_at(560.0, direction=-1)
        self.advance(1)
        self.assertIsNone(bot._melee_threat(self.room, self.bot_conn, self.bot_seat,
                                            self.now(), bot._terrain(self.room)))

    def test_a_dash_that_falls_short_is_no_threat(self):
        """他那一下连同圈一共够到 273 + 60：从 1000 px 外冲过来碰不到，bot 照常打。"""
        self.alice_stands(100.0)
        self.alice_dash_at(100.0)
        self.advance(1)
        self.assertIsNone(bot._melee_threat(self.room, self.bot_conn, self.bot_seat,
                                            self.now(), bot._terrain(self.room)))

    def test_a_pure_movement_dash_is_no_threat(self):
        """第 4 式（패스트 무브）伤害 0、不推 —— 只是位移。"""
        self.alice_stands(560.0)
        self.alice_dash_at(560.0, index=4)
        self.advance(1)
        self.assertIsNone(bot._melee_threat(self.room, self.bot_conn, self.bot_seat,
                                            self.now(), bot._terrain(self.room)))

    def test_it_acts_again_once_the_damage_frames_are_over(self):
        self.quiet_gun()
        self.alice_stands(560.0)
        self.alice_dash_at(560.0)
        self.advance(1)
        self.assertIsNotNone(bot._melee_threat(self.room, self.bot_conn, self.bot_seat,
                                               self.now(), bot._terrain(self.room)))
        # 第 19 帧（DamageEndFrame）起就只剩收招：硬置那一格是第 −2 帧，再走 21 格。
        self.advance(21)
        self.assertGreaterEqual(self.alice.sim_dash_frame, 19)
        self.assertIsNone(bot._melee_threat(self.room, self.bot_conn, self.bot_seat,
                                            self.now(), bot._terrain(self.room)))
        self.assertFalse(bot._yield_to_melee(self.room, self.bot_conn, self.bot_seat,
                                             self.now(), bot._terrain(self.room)),
                         "锁着的那一下也过了伤害段")
        self.assertIsNone(self.bot_conn.melee_yield)

    def test_a_teammates_dash_is_no_threat(self):
        self.alice_stands(560.0)
        self.alice_dash_at(560.0)
        self.advance(1)
        original = bot._seat_group
        bot._seat_group = lambda room, index: 1        # 同一个碰撞组 = 队友，整个不碰
        self.addCleanup(setattr, bot, "_seat_group", original)
        self.assertIsNone(bot._melee_threat(self.room, self.bot_conn, self.bot_seat,
                                            self.now(), bot._terrain(self.room)))


class PushedBotStaysPutTests(MeleeFieldRoom):
    """被推着的这一段 bot 不自己挪（X_Mod §113 / D79）：推是单边的（`0x50e654` 只管别落到他身后），往推的方向一跑就出了圈。

    22:17:01：bot 被推上 60 ms 就转「拉开距离」按右键跑，真人第 17 帧的伤害段够不着它 —— 看着打中了、没扣血。
    """

    def setUp(self):
        super().setUp()
        self.quiet_gun()
        self.bot_conn.melee = False          # 只看走：推解了之后它自己也会冲出去（X_Mod §115），那是另一件事
        original = bot._move_intent
        # 它一心想往推的方向快跑（保守姿态的「拉开距离」就是这样）。
        bot._move_intent = lambda *args, **kwargs: (1, False, False, True)
        self.addCleanup(setattr, bot, "_move_intent", original)

    def pushed_by_a_dash(self):
        self.alice_stands(560.0)
        self.alice_dash_at(560.0)
        self.alice_pushes_bot()

    def test_it_is_carried_by_the_push_and_nothing_else(self):
        self.pushed_by_a_dash()
        self.advance(22)                             # 他第 13 帧起就不挪了，第 17、18 帧是伤害段
        self.assertLess(self.now(), self.alice.motion_action[1], "前提：还在他那一招里")
        edge = self.alice.sim_body.x + botmove_constraint()
        self.assertLessEqual(self.bot_conn.body.x, edge + 1.0,
                             "只该被推到他身前那条边上，不该自己跑出去")
        self.assertEqual(0, self.bot_conn.press_dir)

    def test_it_stays_inside_the_damage_circle(self):
        """第 17 帧那个圈（圆心在他身前 20、半径 40）得盖得住它 —— 这才是「看着打中了就扣血」。"""
        self.pushed_by_a_dash()
        self.advance(19)                             # 硬置 + 2 格起步 + 第 0~16 帧
        move = chrprops.get(0).dash(3)
        swing = bot.DashSwing(0, self.now(), 1, move, 0)
        body = self.alice.sim_body
        self.assertIsNotNone(bot._dash_hits(self.room, swing, body.x, body.y, 17, [
            (self.bot_seat, self.bot_conn.body.x, self.bot_conn.body.y, False,
             self.bot_conn.character_id)]))

    def test_it_walks_again_once_the_move_is_over(self):
        self.pushed_by_a_dash()
        end = self.alice.motion_action[1]
        while self.now() <= end:
            self.advance(1)
        before = self.bot_conn.body.x
        self.advance(4)
        self.assertGreater(self.bot_conn.body.x, before, "推解了，它想跑就跑")


def botmove_constraint():
    """约束那条边离出招者多远（`vft+0x7c` = 35，`botmotion.CONSTRAINT_DISTANCE`）。"""
    import botmotion
    return botmotion.CONSTRAINT_DISTANCE


def constrain_frames(conn, seat):
    """这个座位（bot）发出去的 `0x0017`：`[(受约束对象句柄, 出招者句柄)]`。"""
    return [struct.unpack("<ii", body_of(f)) for f in bot_frames(conn, seat)
            if header(f)["opcode"] == botsync.OP_CONSTRAIN]


class BotLungeTests(MeleeFieldRoom):
    """bot 自己冲刺时服务端这份身体跟着冲出去（`ProcessDash`，X_Mod §115，补 V0.3bot §193）。

    以前原地不动：收方那份照动作冲出去 60~100 px 又被心跳拽回来，伤害圈也按原地判。
    """

    def setUp(self):
        super().setUp()
        self.quiet_gun()
        self.move = chrprops.get(self.bot_conn.character_id).dash(bot.BOT_DASH_INDEX)
        self.speed = chrprops.get(self.bot_conn.character_id).speed

    def dash_now(self, alice_x=450.0):
        """alice 站到 bot 冲得着的地方，推到它冲出去那一格为止；返回 `(那一下, 出手前站在哪)`。"""
        self.alice_stands(alice_x)
        for _ in range(8):
            before = self.bot_conn.body
            self.advance(1)
            if self.bot_conn.dash_swing is not None:
                return self.bot_conn.dash_swing, before
        self.fail("前提：bot 该冲出去")

    def travelled(self, frames):
        return sum(botmove.dash_distance(self.move, f, 1, self.speed) for f in range(frames))

    def test_its_body_follows_the_original_profile(self):
        swing, _ = self.dash_now()
        start = swing.origins[0][0]
        self.assertTrue(swing.lunge)
        self.assertAlmostEqual(start - self.travelled(1), self.bot_conn.body.x, delta=1.0,
                               msg="第 0 帧发 rpDash 那一格当场走完（收方一收到就挪）")
        for k in range(1, 12):
            self.advance(1)
            self.assertAlmostEqual(start - self.travelled(k + 1), self.bot_conn.body.x,
                                   delta=1.0, msg="第 %d 帧" % k)
            self.assertEqual(0, self.bot_conn.press_dir, "冲刺那一段不按方向键")

    def test_the_heartbeat_reports_where_it_lunged_to(self):
        """心跳报冲出去的位置 —— 收方那份也在冲，不会再被拽回原地。"""
        swing, _ = self.dash_now()
        start = swing.origins[0][0]
        self.advance(8)
        beat = self.last_beat()
        x = struct.unpack_from("<h", body_of(beat), 7)[0]
        self.assertLess(x, start - 100, "心跳里的位置该是冲出去的")

    def test_the_keys_it_wants_are_ignored_while_dashing(self):
        swing, _ = self.dash_now()
        original = bot._move_intent
        bot._move_intent = lambda *args, **kwargs: (1, True, False, True)   # 想往回跑、还想跳
        self.addCleanup(setattr, bot, "_move_intent", original)
        start = swing.origins[0][0]
        self.advance(6)
        self.assertLess(self.bot_conn.body.x, start - self.travelled(6) + 1.0)
        self.assertTrue(self.bot_conn.body.on_ground, "总闸关着，按不了跳")

    def test_its_damage_is_judged_where_it_lunged_to(self):
        """伤害圈跟着冲到的地方走：打中的那一发，包里的位置在冲出去之后。"""
        swing, _ = self.dash_now()
        self.advance(20)
        hits = [f for f in splash_frames(self.alice, self.bot_seat)
                if struct.unpack_from("<i", body_of(f), 0)[0] == swing.handle]
        self.assertEqual(1, len(hits), "冲到她跟前那一段该打中一下")
        x = struct.unpack_from("<f", body_of(hits[0]), 13)[0]
        self.assertLess(x, swing.origins[0][0] - 100.0, "圈在它冲到的地方，不在原地")

    def test_a_cancelled_dash_stops_the_lunge(self):
        swing, _ = self.dash_now()
        original = bot._move_intent
        bot._move_intent = lambda *args, **kwargs: (0, False, False, False)   # 招停了它就站着
        self.addCleanup(setattr, bot, "_move_intent", original)
        self.advance(2)
        bot._cancel_melee(self.room, self.bot_seat, self.now(), "test")
        stopped = self.bot_conn.body.x
        self.advance(3)
        self.assertEqual(stopped, self.bot_conn.body.x, "招停了就不再往前冲")

    def test_it_no_longer_dashes_at_someone_it_would_overshoot_without_a_push(self):
        """冲出去 225 px、伤害段在第 8~17 帧：贴脸的人要靠推挤段带走；推不动（这里当她正被别人推着）就别冲。"""
        self.alice_stands(640.0)
        self.advance(1)
        self.bot_conn.dash_swing = None
        seat = self.alice_seat()
        original = bot._may_push
        bot._may_push = lambda room, key, now: False
        self.addCleanup(setattr, bot, "_may_push", original)
        self.assertIsNone(bot._dash_target(self.room, self.bot_conn, self.bot_seat, self.move,
                                           bot._terrain(self.room), self.now()))
        bot._may_push = original
        self.assertEqual((seat, -1), bot._dash_target(self.room, self.bot_conn, self.bot_seat,
                                                      self.move, bot._terrain(self.room),
                                                      self.now()),
                         "推得动就照样冲：推挤段贴住她、带到身前，伤害段打中")


class BotPushTests(MeleeFieldRoom):
    """bot 的推挤段碰到人：服务端替它发 `0x0017`，把人推着带走（X_Mod §115 / D80，用户 2026-09-26 选「推着走（照原版）」）。

    原版这一包只有出招者本机发（`0x481d3c → 0x4935b7`）；bot 没有本机，以前没人发 —— 贴脸冲过了头打不着人。
    """

    def setUp(self):
        super().setUp()
        self.quiet_gun()

    def close_dash(self, alice_x=640.0):
        """alice 贴在 bot 左边 60 px，推到它冲出去那一格；返回那一下。"""
        self.alice_stands(alice_x)
        for _ in range(8):
            self.advance(1)
            if self.bot_conn.dash_swing is not None:
                return self.bot_conn.dash_swing
        self.fail("前提：bot 该冲出去")

    def test_the_push_goes_out_once_with_both_handles(self):
        swing = self.close_dash()
        self.advance(swing.move.cast_end + 2)
        self.assertEqual([(botsync.character_handle(self.alice_seat()),
                           botsync.character_handle(self.bot_seat))],
                         constrain_frames(self.alice, self.bot_seat),
                         "受约束对象 = alice、出招者 = bot，一下只发一发")

    def test_she_is_carried_in_front_of_it_and_then_hit(self):
        swing = self.close_dash()
        self.advance(3)
        self.assertIn(self.alice_seat(), swing.carried)
        edge = self.bot_conn.body.x - botmove_constraint()
        self.assertLessEqual(self.alice.sim_body.x, edge + 1.0, "被推到它朝向那一侧（身前 35）")
        self.advance(20)
        hits = [f for f in splash_frames(self.alice, self.bot_seat)
                if struct.unpack_from("<i", body_of(f), 0)[0] == swing.handle]
        self.assertEqual(1, len(hits), "推着带到身前，伤害段打中")
        self.assertNotIn(self.alice_seat(), swing.carried, "挨打那一下就放开")
        self.assertGreater(self.alice.push_block_until, self.now() - 1.0,
                           "挨打后 10 格不再接受新的推（`[+0x17c]`）")

    def test_a_blocked_or_already_pushed_target_is_not_pushed_again(self):
        self.alice.push_block_until = self.now() + 60.0
        self.alice_stands(640.0)
        self.advance(12)
        self.assertEqual([], constrain_frames(self.alice, self.bot_seat))

    def test_the_carry_ends_with_its_dash(self):
        swing = self.close_dash()
        original = bot._release_carry
        bot._release_carry = lambda *args, **kwargs: None      # 不让「挨打就放开」抢先，只看招收了放不放
        self.addCleanup(setattr, bot, "_release_carry", original)
        self.advance(3)
        self.assertIn(self.alice_seat(), swing.carried, "前提：推上了")
        self.advance(swing.move.total_frame)
        self.assertIsNot(swing, self.bot_conn.dash_swing)
        self.assertIsNone(bot._carrier_of(self.room, self.alice_seat()),
                          "那一下收了，没人推她了")

    def test_its_own_heartbeats_hold_while_she_is_carried(self):
        """推的是服务端这份外推（她自己那台也挂着同一个约束）：心跳一到照样以她报的为准。"""
        swing = self.close_dash()
        self.advance(2)
        self.human_heartbeat(self.alice, 500.0, self.FLOOR_Y, ticks=0)
        self.advance(1)
        self.assertLessEqual(self.alice.sim_body.x, self.bot_conn.body.x - botmove_constraint() + 1.0)

    def test_a_heartbeat_that_contradicts_the_push_lets_her_go(self):
        """推上之后她报来的心跳落在边界里面、超出一帧能走回的距离 = 她那台没挂上这一推（X_Mod §130 / D92）：
        服务端这份不再推她、这一下也不再推她。09-27 19:05:04：以前照推不误，她的外推一格一格被拽在 bot 身前、
        和她自己报的差出几百 px；bot 按这个假位置排下一招再推一发，她那台这回挂上了，她被拽了 393 px。"""
        swing = self.close_dash()
        self.advance(2)
        self.assertIn(self.alice_seat(), swing.carried, "前提：推上了")
        behind = self.bot_conn.body.x + 60.0              # 它朝左冲，她报在它身后 —— 挂上了的话不可能在这儿
        self.human_heartbeat(self.alice, behind, self.FLOOR_Y, ticks=0)
        self.advance(1)
        self.assertNotIn(self.alice_seat(), swing.carried)
        self.assertIn(self.alice_seat(), swing.released, "这一下剩下的推挤段不再推她")
        self.assertGreater(self.alice.sim_body.x, self.bot_conn.body.x, "外推跟着她报的走，不再被拽回它身前")

    def test_a_heartbeat_one_step_inside_the_edge_keeps_the_carry(self):
        """被推着的人每帧先被推到边界、再走这一帧（`0x4fe20c` 在走路之前）⇒ 报在边界里面一步之内照样算挂着。"""
        swing = self.close_dash()
        self.advance(2)
        edge = int(self.bot_conn.body.x - botmove_constraint())
        self.human_heartbeat(self.alice, edge + 5.0, self.FLOOR_Y, ticks=0)
        self.advance(1)
        self.assertIn(self.alice_seat(), swing.carried)

    def test_without_a_new_heartbeat_the_carry_holds(self):
        """推上之后还没来过心跳：没有证据，照推（她那台照常是挂上了的）。"""
        swing = self.close_dash()
        for _ in range(4):
            self.advance(1)
            if self.alice_seat() in swing.carried:
                break
        self.assertIn(self.alice_seat(), swing.carried, "前提：推上了")
        for _ in range(2):
            self.advance(1)
            self.assertIn(self.alice_seat(), swing.carried)
            self.assertLessEqual(self.alice.sim_body.x, self.bot_conn.body.x - botmove_constraint() + 1.0)

    def test_the_push_block_runs_one_frame_past_the_original_ten(self):
        """挨打后不接受推：原版 10 帧从她那台**收到**这一下之后的那一帧数起 ⇒ 服务端多数一帧（X_Mod §130）。
        09-27 19:05:04.400 / 06.512：服务端正好第 10 格推、她那台都还拦着，没挂上。"""
        t = self.now()
        bot._release_carry(self.room, self.alice_seat(), t)
        frame = bot.BOT_DASH_FRAME_MS / 1000.0
        self.assertAlmostEqual(t + 11 * frame, self.alice.push_block_until)
        self.assertFalse(bot._may_push(self.room, self.alice_seat(), t + 10 * frame))
        self.assertTrue(bot._may_push(self.room, self.alice_seat(), t + 11 * frame))


class BotPushMobTests(MeleeFieldRoom):
    """怪也推得动（`0x0017` 的受约束对象填怪的句柄，控制者那台照推，X_Mod §115）—— 2026-08-30「怪都贴脸了 bot 都不冲」那条别回来。"""

    def test_a_mob_in_its_face_is_pushed_and_hit(self):
        self.quiet_gun()
        self.room.quest.mobs[500123] = [650.0, 370.0, "chase", 1]
        self.alice_stands(100.0)                     # 她在老远：这一下冲的是怪
        swing = None
        for _ in range(30):
            self.advance(1)
            swing = swing or self.bot_conn.dash_swing
        self.assertIsNotNone(swing, "前提：贴脸的怪也该冲")
        self.assertIn((500123, botsync.character_handle(self.bot_seat)),
                      constrain_frames(self.alice, self.bot_seat))
        hits = [f for f in splash_frames(self.alice, self.bot_seat)
                if struct.unpack_from("<i", body_of(f), 4)[0] == 500123]
        self.assertTrue(hits, "推着带到身前，伤害段打中怪")
        row = self.room.quest.mobs.get(500123)
        self.assertIsNotNone(row)
        self.assertLess(row[0], 650.0 - 50.0, "怪物表里那一格跟着被推走了")


def beat_facing(beat):
    """心跳位域低两位的朝向（收方 `0x5042a3` 无条件写进 `[+0x2d0]`）：+1 朝右 / −1 朝左。"""
    flags = struct.unpack_from("<I", body_of(beat), 19)[0]
    return ((flags & 3) ^ 2) - 2


def beats_of(conn, seat):
    return [f for f in bot_frames(conn, seat) if udpsync.is_heartbeat(f)]


class BotDashFacingTests(MeleeFieldRoom):
    """冲刺那一段心跳的朝向锁在冲的方向（X_Mod §116 / D81）。

    00:15:24.721：bot「拉开距离」往左退、回身朝右冲，心跳一路报朝左。收方收心跳无条件写 `[+0x2d0]`，约束 `0x50e654`
    按出招者朝向（`vft+0x80`）把被推的人推到那一侧 ⇒ 她从 x=546 被拽回 429 —— 「bot 近身攻击后我闪现回原位置」。
    """

    def setUp(self):
        super().setUp()
        self.quiet_gun()
        # 没有准星可跟（解不出弹道时就是这样）：朝向只看 `heading`。它站着不朝她走，冲之前一直朝左。
        original_aim, original_intent = bot._fire_target, bot._move_intent
        bot._fire_target = lambda *args, **kwargs: None
        bot._move_intent = lambda *args, **kwargs: (0, False, False, False)
        self.addCleanup(setattr, bot, "_fire_target", original_aim)
        self.addCleanup(setattr, bot, "_move_intent", original_intent)
        self.bot_conn.heading = botsync.FACING_LEFT

    def dash_right(self):
        """alice 在 bot 右边 60 px；推到它冲出去那一格，返回那一下。"""
        self.alice_stands(760.0)
        self.clear()
        for _ in range(8):
            self.advance(1)
            if self.bot_conn.dash_swing is not None:
                return self.bot_conn.dash_swing
        self.fail("前提：bot 该朝她冲出去")

    def test_every_beat_of_the_dash_faces_the_way_it_dashes(self):
        swing = self.dash_right()
        self.assertEqual(1, swing.direction, "前提：朝右冲")
        for _ in range(swing.move.total_frame - 1):
            self.assertIs(swing, self.bot_conn.dash_swing, "前提：这一下还在")
            self.advance(1)
        beats = beats_of(self.alice, self.bot_seat)
        self.assertGreater(len(beats), 5)
        self.assertEqual([1] * len(beats), [beat_facing(b) for b in beats])

    def test_it_faces_the_dash_even_while_aiming_the_other_way(self):
        """准星在身后（还瞄着另一边）：冲刺那一段照样报冲的方向 —— 真人冲刺时总闸关着，准星带不动身子。"""
        bot._fire_target = lambda *args, **kwargs: (self.alice_seat(), (500.0, 380.0), None)
        swing = self.dash_right()
        self.assertEqual(1, swing.direction, "前提：朝右冲")
        self.advance(swing.move.total_frame - 2)
        beats = beats_of(self.alice, self.bot_seat)
        self.assertEqual([1] * len(beats), [beat_facing(b) for b in beats])
        self.advance(4)
        self.assertEqual(-1, beat_facing(self.last_beat()), "收了招就又跟着准星转过去")

    def test_the_dash_tick_already_beats_after_the_dash(self):
        """冲出去那一格：先 `rpDash`、再一发心跳（第 0 帧挪完的位置 + 冲的朝向），推挤段第一发 `0x0017` 到之前它已经转过身。"""
        swing = self.dash_right()
        frames = bot_frames(self.alice, self.bot_seat)
        kinds = ["dash" if header(f)["opcode"] == botsync.OP_DASH
                 else "beat" if udpsync.is_heartbeat(f) else "other" for f in frames]
        self.assertIn("dash", kinds)
        after = kinds[kinds.index("dash") + 1:]
        self.assertEqual("beat", after[0] if after else None, "同一格紧跟一发心跳")
        beat = frames[kinds.index("dash") + 1]
        self.assertEqual(1, beat_facing(beat))
        x = struct.unpack_from("<h", body_of(beat), 7)[0]
        self.assertEqual(int(self.bot_conn.body.x), x, "报的是第 0 帧挪完的位置")
        self.assertGreater(x, swing.origins[0][0])

    def test_it_keeps_facing_that_way_once_the_dash_is_over(self):
        swing = self.dash_right()
        self.advance(swing.move.total_frame + 2)
        self.assertIsNone(self.bot_conn.dash_swing)
        self.assertEqual(1, beat_facing(self.last_beat()), "收完招没准星可跟：接着朝冲的方向，不转回去")

    def test_the_pushed_one_ends_up_in_front_of_it(self):
        """服务端推着她走的那一侧（`swing.direction`）和收方按心跳朝向推的那一侧是同一侧。"""
        swing = self.dash_right()
        self.advance(4)
        self.assertIn(self.alice_seat(), swing.carried, "前提：推上了")
        self.assertGreaterEqual(self.alice.sim_body.x,
                                self.bot_conn.body.x + botmove_constraint() - 1.0)
        self.assertEqual(1, beat_facing(self.last_beat()))


class PushBackReleasesTests(MeleeFieldRoom):
    """`0x0017` 先解**出招者自己**的约束（`0x49361e` → `0x50e636(出招者, 0)`，X_Mod §116 / D81）。

    00:15:54：bot 冲过来推着她走，她被推着时自己也冲、推挤段推上了 bot。服务端以前两个约束一起挂着：bot 推她到它身前、
    她推 bot 到她身前，同一个方向 —— 一格互相顶一次，bot 0.7 秒被顶出去 1000 px（她自己那台早挣脱了，停在 806）。
    """

    def setUp(self):
        super().setUp()
        self.quiet_gun()

    def carried_to_the_left(self):
        """bot（700）朝左冲、推着她（640）走；返回那一下。"""
        self.alice_stands(640.0)
        for _ in range(8):
            self.advance(1)
            swing = self.bot_conn.dash_swing
            if swing is not None and self.alice_seat() in swing.carried:
                return swing
        self.fail("前提：bot 推着她走")

    def test_her_push_frees_her_from_its_push(self):
        swing = self.carried_to_the_left()
        self.alice_dash_at(self.alice.sim_body.x, direction=-1)
        self.alice_pushes_bot()
        self.assertNotIn(self.alice_seat(), swing.carried, "她反手推了人，就挣脱了")
        self.assertIsNotNone(self.bot_conn.motion_constraint, "bot 这回被她推着")
        self.assertIsNone(bot._carrier_of(self.room, self.alice_seat()))

    def test_the_two_no_longer_shove_each_other_away(self):
        swing = self.carried_to_the_left()
        start = self.bot_conn.body.x
        self.alice_dash_at(self.alice.sim_body.x, direction=-1)
        self.alice_pushes_bot()
        self.advance(12)
        # 她那一招最多挪 273 px；bot 被她推着最多到她身前 35（她朝左）。互相顶的话一格就是 35 px 起步。
        self.assertGreater(self.bot_conn.body.x, start - 273.0 - botmove_constraint() - 80.0,
                           "bot 不该被一格一格顶出去")
        self.assertLessEqual(self.bot_conn.body.x,
                             self.alice.sim_body.x - botmove_constraint() + 1.0,
                             "被推到她朝向那一侧（身前 35）")
        self.assertIs(swing, self.bot_conn.dash_swing, "前提：它那一下还没完")

    def test_it_does_not_grab_her_again_in_the_same_dash(self):
        swing = self.carried_to_the_left()
        self.alice_dash_at(self.alice.sim_body.x, direction=-1)
        self.alice_pushes_bot()
        self.advance(swing.move.cast_end + 2)
        self.assertEqual(1, len(constrain_frames(self.alice, self.bot_seat)),
                         "推回来就来回拽：这一下剩下的推挤段不再推她")
        self.assertIn(self.alice_seat(), swing.released)

    def test_its_own_push_frees_it(self):
        """它冲着冲着被她推住了、自己的推挤段又推上了她：每台收方都先把它放开。"""
        self.alice_stands(640.0)
        self.alice_dash_at(640.0, direction=1)
        self.alice_pushes_bot()
        self.assertIsNotNone(self.bot_conn.motion_constraint, "前提：被她推着")
        swing = bot.DashSwing(123, self.now(), -1, chrprops.get(
            self.bot_conn.character_id).dash(bot.BOT_DASH_INDEX), self.bot_conn.character_id,
            lunge=True)
        body = (self.alice_seat(), 640.0, self.FLOOR_Y, False,
                self.room.seats[self.alice_seat()].character_id)
        self.alice.push_block_until = None
        bot._dash_push(self.room, self.bot_conn, swing, (700.0, self.FLOOR_Y),
                       (560.0, self.FLOOR_Y), 0, [body], self.now())
        self.assertIn(self.alice_seat(), swing.carried, "前提：推上了")
        self.assertIsNone(self.bot_conn.motion_constraint)


class CounterDashCourseTests(MeleeFieldRoom):
    """我冲出去的路上会撞进她先出的那一招：不冲（X_Mod §116 / D81）。开枪照旧只看此刻站的地方。

    00:14:38：她背对 bot 朝左冲，bot 在她身后 38 px、晚 26 ms 冲她后背 —— 站着的 bot 她的圈擦不到（服务端判不碰），
    可 bot 一冲就钻进了她第 0 帧那个圈（她那台第 0 帧就推住了 bot），按原版同级谁先进伤害段谁赢，bot 第 6 帧赢了。
    """

    def setUp(self):
        super().setUp()
        self.quiet_gun()

    def her_back_dash(self, x=640.0):
        """她在 bot 左边 60 px、朝左（背对 bot）冲出去。"""
        self.alice_stands(x)
        self.alice_dash_at(x, direction=-1)
        self.clear()

    def test_it_does_not_dash_into_her_move_from_behind(self):
        self.her_back_dash()
        self.advance(1)
        self.assertIsNone(bot._melee_threat(self.room, self.bot_conn, self.bot_seat,
                                            self.now(), bot._terrain(self.room)),
                          "前提：站着的 bot 她那一招碰不到")
        self.advance(8)
        self.assertEqual([], dash_frames(self.alice, self.bot_seat), "冲过去就钻进她的圈了")
        self.assertEqual((self.alice_seat(), self.alice.motion_action_mark),
                         self.bot_conn.dash_yield, "锁住的是她那一下")

    def test_it_may_still_shoot_since_standing_still_is_safe(self):
        self.bot_conn.next_fire_at = 0.0
        self.her_back_dash()
        self.advance(8)
        self.assertEqual([], dash_frames(self.alice, self.bot_seat))
        self.assertIsNone(self.bot_conn.melee_yield, "此刻站的地方她够不着：开枪那条不拦")

    def test_the_same_spot_without_her_dash_gets_a_dash(self):
        """对照：同一个站位她没出招，bot 照冲。"""
        self.alice_stands(640.0)
        self.clear()
        self.advance(8)
        self.assertTrue(dash_frames(self.alice, self.bot_seat))

    def test_it_dashes_again_once_her_damage_frames_are_over(self):
        self.her_back_dash()
        self.advance(4)
        latch = self.bot_conn.dash_yield
        self.assertIsNotNone(latch, "前提：锁上了")
        self.assertTrue(bot._latched_dash_open(self.room, latch, self.now()))
        while self.alice.sim_dash_frame is None or self.alice.sim_dash_frame < 19:
            self.advance(1)
        self.assertFalse(bot._latched_dash_open(self.room, latch, self.now()),
                         "她过了伤害段，锁就开了")
        self.advance(1)
        self.assertIsNone(self.bot_conn.dash_yield, "下一次想冲时重新判、不再锁着")

    def test_a_dash_far_away_does_not_hold_it_back(self):
        """她在老远冲（冲出去也碰不到她的圈）：照常冲 —— 不是「她一出招 bot 就哑火」（D79 否掉的那条）。"""
        self.place_bot(700.0, self.FLOOR_Y)
        self.alice_stands(1300.0)
        self.alice_dash_at(1300.0, direction=1)
        self.advance(1)
        move = chrprops.get(self.bot_conn.character_id).dash(bot.BOT_DASH_INDEX)
        for direction in (-1, 1):
            self.assertFalse(bot._yield_dash_to_melee(
                self.room, self.bot_conn, self.bot_seat, move, direction,
                bot._terrain(self.room), self.now()))
        self.assertIsNone(self.bot_conn.dash_yield)


if __name__ == "__main__":
    unittest.main()
