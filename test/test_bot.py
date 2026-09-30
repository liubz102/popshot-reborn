#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V0.3 M1 —— 房间 bot 的测试。

分工：`test_lobby.py` 测房间表本身（座位 / 房主迁移），这里测**命令层和
广播时序** —— 敲一条命令之后房里每个人分别收到了哪几发包、action 对不对。

★ 连接夹具直接从 `test_room.py` 借（`make_conn` 走 `Conn.__new__` 接真线，
不是另写一份假对象）。复制一份的话，`Conn.__init__` 每加一个字段就要在两处
补，迟早有一处漏掉 —— 那正是 CLAUDE.md 铁律 8 要防的。
"""
import os
import struct
import sys
import time
import unicodedata
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")   # 被测代码在隔壁
for _path in (HERE, SERVER):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import account_store                                           # noqa: E402
import bot                                                     # noqa: E402
import botmove                                                 # noqa: E402
import botsync                                                 # noqa: E402
import gameserver                                              # noqa: E402
import weapondata                                              # noqa: E402
from gameserver import (                                       # noqa: E402
    OP_BROADCAST_DEATH, OP_CHANGE_CONTROLLER_SLOT, OP_CHAT,
    OP_COUNT_GAME_READY, OP_END_GAME, OP_END_QUEST, OP_LEAVE_SESSION,
    OP_LOADING_DONE, OP_MAP_CHANGE_READY, OP_MAP_LOADING_DONE,
    OP_REP_CHANGE_TO_NEXT_MAP, OP_REP_GAME_RESULT, OP_REPORT_HP_ZERO,
    OP_REQ_CHANGE_TO_NEXT_MAP, OP_RESPAWN_CHARACTER,
    OP_SESSION_MEMBER_UPDATE, ROOM_SEAT_COUNT,
    SEAT_ACTION_CHANGE_CHARACTER, SEAT_ACTION_JOIN, SEAT_ACTION_LEAVE,
    SEAT_ACTION_RESYNC, SESSION_STATUS_PLAYING, StartGameHandshake,
    Reader, parse_session_slot, w_wstr,
)
from lobby import TEAM_A, TEAM_B                               # noqa: E402
from test_room import LobbyIsolated, frames, make_conn         # noqa: E402
# ★ M2 的战斗夹具直接借 `test_battle.BattleRoom`（真的走一遍开局链，
#   而不是手工把 `room.battle.state` 拨到 IN_GAME）。同一个理由：测的必须
#   是真接线，不然「bot 报没报到」这件事根本测不到。
from test_battle import (                                      # noqa: E402
    BattleRoom, bodies, hp_zero_payload, opcodes,
)
from test_relayserver import udp_packet                        # noqa: E402
from test_botcombat import flat_terrain                        # noqa: E402

#: 组队战 / 个人战 / 闯关三种房间的建房参数（`lobby.team_layout_of` 的三条路）。
TEAMS_ROOM = dict(session_type=1, arguments=(1, 3, 0))
FREE_ROOM = dict(session_type=1, arguments=(0, 3, 0))
COOP_ROOM = dict(session_type=2, arguments=(3, 1))


def seat_updates(conn):
    """这条连接收到的全部 `0x0301`，解成 `(action, 座位号, 座位字段)`。"""
    out = []
    for blob in conn.sent:
        for _kind, opcode, payload in frames(blob):
            if opcode != OP_SESSION_MEMBER_UPDATE:
                continue
            # action 是 **1 字节**（`0x5d5942`），后面才是 int32 座位号。
            action = payload[0]
            reader = Reader(payload[1:])
            seat_index = reader.i32()
            out.append((action, seat_index, parse_session_slot(reader)))
    return out


def chat_lines(conn):
    """这条连接收到的全部聊天正文（含系统提示）。

    `build_receive_chat` 没有配套的解析函数（服务端只发不收），照它的
    线格式反过来读一遍：u16 座位号 + wstr 发言者 + wstr 正文 + int32 类型。
    """
    out = []
    for blob in conn.sent:
        for _kind, opcode, payload in frames(blob):
            if opcode != OP_CHAT:
                continue
            reader = Reader(payload)
            reader.u16()                   # 发言者座位号
            reader.wstr()                  # 发言者（系统提示留空）
            out.append(reader.wstr())
    return out


class BotCommandTests(LobbyIsolated):
    """`/a` `/c` `/t` `/r` 的行为。"""

    def setUp(self):
        super().setUp()
        self.host = make_conn("alice")
        self.guest = make_conn("bob")

    def open_room(self, **kwargs):
        """建一个房间，房主坐 0 号位，返回 `Room`。"""
        params = dict(TEAMS_ROOM)
        params.update(kwargs)
        room = self.lobby.create_room(self.host, title="来玩",
                                      seat=self.host.seat_snapshot(),
                                      **params)
        self.host.sent.clear()
        return room

    def add_guest(self, room):
        result, joined, index = self.lobby.join(
            self.guest, room.room_id, seat=self.guest.seat_snapshot())
        self.assertEqual(0, result)
        self.assertIs(room, joined)
        self.host.sent.clear()
        self.guest.sent.clear()
        return index

    # -- /a ---------------------------------------------------------------
    def test_bot_takes_the_lowest_free_seat_and_is_marked_as_a_bot(self):
        room = self.open_room()
        self.assertTrue(bot.handle_command(self.host, "/a"))
        seat = room.seats[1]
        self.assertIsNotNone(seat)
        self.assertTrue(seat.is_bot)
        self.assertEqual("bot 1", seat.nickname)
        self.assertEqual([1], room.bot_seats())
        self.assertEqual([0], room.human_seats())
        self.assertEqual(1, room.human_count())
        # 房间列表上的人数要把 bot 算进去 —— 客户端自己数空位也是按座位数。
        self.assertEqual(2, room.player_count())

    def test_bot_join_is_announced_to_the_host_too(self):
        # ★ 房主的客户端和别人一样，只认 `0x0301` action 0 那一发才建模型
        #   （`0x405e1c`）。`announce_join()` 用的 `broadcast()` 排除自己，
        #   所以 bot 这条路必须补上房主自己的那一份。
        room = self.open_room()
        self.add_guest(room)
        bot.handle_command(self.host, "/a")
        for who in (self.host, self.guest):
            updates = [u for u in seat_updates(who) if u[1] == 2]
            self.assertEqual(1, len(updates), f"{who.account_name} 没收到座位广播")
            action, _seat_index, slot = updates[0]
            self.assertEqual(SEAT_ACTION_JOIN, action)
            self.assertTrue(slot["occupied"])
            self.assertEqual("bot 2", slot["nickname"])

    def test_bot_seats_alternate_teams_in_a_team_room(self):
        # 组队战里客户端要求两队人数相等才让开局（§8）。从最小空座往下填 +
        # `default_team_for`（座位号奇偶）天然平衡。
        room = self.open_room()
        for _ in range(5):
            bot.handle_command(self.host, "/a")
        teams = [seat.team for seat in room.seats]
        self.assertEqual([TEAM_A, TEAM_B, TEAM_A, TEAM_B, TEAM_A, TEAM_B], teams)
        self.assertIsNone(bot._team_balance_warning(room))

    def test_bot_on_a_full_room_says_so_and_changes_nothing(self):
        room = self.open_room()
        for _ in range(5):
            bot.handle_command(self.host, "/a")
        self.host.sent.clear()
        self.assertTrue(bot.handle_command(self.host, "/a"))
        self.assertEqual(5, len(room.bot_seats()))
        self.assertIn("满", "".join(chat_lines(self.host)))
        # 一发座位广播都不该有：什么都没变。
        self.assertEqual([], seat_updates(self.host))

    def test_a_bot_conn_never_enters_the_online_connection_table(self):
        # ★ `_conns` 是「在线的真人」表：`latest_conn()` 是控制通道不指定
        #   账号时的默认目标，混进 bot 就会对着空气发命令。
        room = self.open_room()
        before = gameserver.all_conns()
        bot.handle_command(self.host, "/a")
        self.assertEqual(before, gameserver.all_conns())
        self.assertIsInstance(room.seats[1].conn, bot.BotConn)

    # -- /a 的个数参数（D56）------------------------------------------------
    def test_a_with_a_count_adds_that_many_bots_at_once(self):
        room = self.open_room()
        self.assertTrue(bot.handle_command(self.host, "/a 3"))
        self.assertEqual([1, 2, 3], room.bot_seats())

    def test_a_without_a_count_still_adds_exactly_one(self):
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        self.assertEqual([1], room.bot_seats())

    def test_a_stops_at_the_last_free_seat_and_says_so(self):
        """★ 要 6 个只坐得下 5 个（房主自己占一格）—— 加成功的照样留下。"""
        room = self.open_room()
        self.assertTrue(bot.handle_command(self.host, "/a 6"))
        self.assertEqual([1, 2, 3, 4, 5], room.bot_seats())
        self.assertIn("房间已经满了", "".join(chat_lines(self.host)))

    def test_a_rejects_a_count_that_is_not_a_positive_number(self):
        room = self.open_room()
        for text in ("/a x", "/a 0", "/a -2"):
            self.host.sent.clear()
            self.assertTrue(bot.handle_command(self.host, text))
            self.assertTrue(chat_lines(self.host), f"{text} 没有给出原因")
        self.assertEqual([], room.bot_seats())

    def test_del_is_gone_and_falls_through_to_normal_chat(self):
        """★ `/del` 整条删掉了（D56）—— 踢 bot 用客户端自带的踢人按钮。

        删掉之后它就是**普通聊天**，不该再被命令层吞掉。
        """
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        self.assertFalse(bot.handle_command(self.host, "/del 1"))
        self.assertEqual([1], room.bot_seats())

    # -- /c --------------------------------------------------------------
    def test_char_maps_panel_index_to_the_real_character_id(self):
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        self.host.sent.clear()
        self.assertTrue(bot.handle_command(self.host, "/c 1 3"))
        # ★ 面板 1/2/3 -> id 0/1/2（D6）。商城角色不给 bot 用（D54）。
        self.assertEqual(2, room.seats[1].character_id)
        self.assertEqual(2, room.seats[1].conn.character_id)

    def test_char_uses_action_3_so_the_client_stays_quiet(self):
        # ★ action 4 会让客户端播一句韩文「…캐릭터로 선택되었습니다.」
        #   （`0x406520`）。bot 换角色的提示我们自己用中文说。
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        self.host.sent.clear()
        bot.handle_command(self.host, "/c 1 2")
        actions = [action for action, seat_index, _ in seat_updates(self.host)
                   if seat_index == 1]
        self.assertEqual([SEAT_ACTION_RESYNC], actions)
        self.assertNotIn(SEAT_ACTION_CHANGE_CHARACTER, actions)
        self.assertEqual(1, room.seats[1].character_id)

    def test_char_rejects_panel_indexes_outside_1_to_14(self):
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        before = room.seats[1].character_id
        for text in ("/c 1 0", "/c 1 15", "/c 1 x", "/c 1"):
            self.host.sent.clear()
            self.assertTrue(bot.handle_command(self.host, text))
            self.assertEqual(before, room.seats[1].character_id)
            self.assertTrue(chat_lines(self.host), f"{text} 没有给出原因")

    def test_char_keeps_team_and_ready_when_it_rebuilds_the_seat(self):
        # 座位快照是整发的：换角色那一发不能顺手把队伍/准备抹成 0。
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        bot.handle_command(self.host, "/r")
        self.host.sent.clear()
        bot.handle_command(self.host, "/c 1 3")
        _action, _seat_index, slot = seat_updates(self.host)[0]
        self.assertTrue(slot["ready"])
        self.assertEqual(room.seats[1].team, slot["team"])

    # -- /t ----------------------------------------------------------------
    def test_tm_toggles_between_1_and_2_in_a_team_room(self):
        room = self.open_room(**TEAMS_ROOM)
        bot.handle_command(self.host, "/a")
        self.assertEqual(TEAM_B, room.seats[1].team)
        self.assertTrue(bot.handle_command(self.host, "/t 1"))
        self.assertEqual(TEAM_A, room.seats[1].team)
        bot.handle_command(self.host, "/t 1")
        self.assertEqual(TEAM_B, room.seats[1].team)

    def test_tm_warns_when_the_two_sides_stop_being_equal(self):
        # 客户端 `0x468495` 数两队人数，不等就拒绝开局（§8）—— 必须当场说。
        room = self.open_room(**TEAMS_ROOM)
        bot.handle_command(self.host, "/a")
        self.host.sent.clear()
        bot.handle_command(self.host, "/t 1")
        self.assertIn("两队人数不等", "".join(chat_lines(self.host)))
        self.assertEqual(TEAM_A, room.seats[1].team)

    def test_tm_is_refused_in_free_for_all_and_coop_rooms(self):
        # ★ 个人战的队伍号必须全是 0、闯关必须全在 1 队：客户端的队伍记录
        #   数组只有两格，别的值会越界写进**别人的**战绩（§8）。
        for params in (FREE_ROOM, COOP_ROOM):
            with self.subTest(params=params):
                self.lobby.reset()
                room = self.open_room(**params)
                bot.handle_command(self.host, "/a")
                before = room.seats[1].team
                self.host.sent.clear()
                self.assertTrue(bot.handle_command(self.host, "/t 1"))
                self.assertEqual(before, room.seats[1].team)
                self.assertIn("换不了队", "".join(chat_lines(self.host)))

    # -- /team：客户端吃掉的那个名字 ----------------------------------------
    def test_team_only_points_at_tm_and_changes_nothing(self):
        # ★ 客户端 `0x54e727` 把 `"/team "` 当队伍聊天的前缀切掉了（§19），
        #   所以 `/team 1` 根本到不了服务端；能到的只有光杆 `/team`
        #   （差那个空格）。它必须只回一行提示，一个座位都不许动。
        room = self.open_room(**TEAMS_ROOM)
        bot.handle_command(self.host, "/a")
        before = room.seats[1].team
        self.host.sent.clear()
        self.assertTrue(bot.handle_command(self.host, "/team"))
        self.assertEqual(before, room.seats[1].team)
        self.assertIn("/t", "".join(chat_lines(self.host)))
        self.assertEqual([], seat_updates(self.host))

    def test_no_command_name_collides_with_a_client_reserved_prefix(self):
        # 起新命令名时的护栏：客户端会吞掉 `/team ` / `/say ` / `/tell ` /
        # `/to ` 和两个韩文前缀，中了就一个字也到不了服务端（§19）。
        reserved = {p.strip().lstrip(bot.COMMAND_PREFIX).lower()
                    for p in bot.CLIENT_RESERVED_PREFIXES}
        live = set(bot.COMMANDS) - {"team"}   # team 留着只为回一行提示
        self.assertEqual(set(), live & reserved)

    # -- /r -------------------------------------------------------------
    def test_ready_marks_every_bot_and_broadcasts_each_seat(self):
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        bot.handle_command(self.host, "/a")
        self.host.sent.clear()
        self.assertTrue(bot.handle_command(self.host, "/r"))
        self.assertTrue(room.seats[1].ready)
        self.assertTrue(room.seats[2].ready)
        # 房主自己那格不动 —— 客户端本来就把房主算成已准备（`0x4696f8`）。
        self.assertFalse(room.seats[0].ready)
        seats = sorted(seat_index for _a, seat_index, _s in seat_updates(self.host))
        self.assertEqual([1, 2], seats)

    def test_ready_without_any_bot_says_so(self):
        self.open_room()
        self.assertTrue(bot.handle_command(self.host, "/r"))
        self.assertIn("一个 bot 都没有", "".join(chat_lines(self.host)))

    def test_ready_again_cancels_every_bot(self):
        """★ 全都准备好了再敲一次 `/r` = **全部取消**（用户 2026-08-28，D56）。

        判据是**当前状态**（「还有没准备好的吗」），不是敲了第几次 ——
        计数器会在别人手动改过之后和事实对不上（铁律 10）。
        """
        room = self.open_room()
        bot.handle_command(self.host, "/a 2")
        bot.handle_command(self.host, "/r")
        self.assertTrue(all(room.seats[i].ready for i in room.bot_seats()))
        self.host.sent.clear()
        self.assertTrue(bot.handle_command(self.host, "/r"))
        self.assertFalse(any(room.seats[i].ready for i in room.bot_seats()))
        self.assertEqual([1, 2],
                         sorted(s for _a, s, _x in seat_updates(self.host)))
        self.assertIn("取消", "".join(chat_lines(self.host)))

    def test_ready_marks_the_rest_when_only_some_are_ready(self):
        """★ 还有一个没准备好 ⇒ 这一发是「全部准备」，不是「全部取消」。"""
        room = self.open_room()
        bot.handle_command(self.host, "/a 2")
        room.seats[1].update(ready=True)
        self.assertTrue(bot.handle_command(self.host, "/r"))
        self.assertTrue(all(room.seats[i].ready for i in room.bot_seats()))

    # -- AI 难度 -----------------------------------------------------------
    def test_a_new_room_defaults_to_three_and_all_five_levels_are_global(self):
        room = self.open_room()
        bot.handle_command(self.host, "/a 2")
        self.assertEqual(3, room.bot_difficulty)
        for level in range(1, 6):
            self.host.sent.clear()
            self.assertTrue(bot.handle_command(self.host, f"/d {level}"))
            self.assertEqual(level, room.bot_difficulty)
            self.assertIn(bot.BOT_DIFFICULTY_LABELS[level],
                          "".join(chat_lines(self.host)))

    def test_d_without_a_number_restores_the_default(self):
        room = self.open_room()
        bot.handle_command(self.host, "/d 5")
        self.assertTrue(bot.handle_command(self.host, "/d"))
        self.assertEqual(bot.BOT_DIFFICULTY_DEFAULT, room.bot_difficulty)

    def test_difficulty_changes_work_during_battle_and_survive_a_new_game(self):
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        machine = room.seats[1].conn
        room.status = SESSION_STATUS_PLAYING
        self.assertTrue(bot.handle_command(self.host, "/d 5"))
        self.assertEqual(5, room.bot_difficulty)
        machine.reset_battle_frame()       # 换图 / 后来新开一局
        self.assertEqual(5, room.bot_difficulty)
        self.assertEqual(bot.BOT_DIFFICULTY_PROFILES[5],
                         bot.difficulty_profile(room))

    def test_the_five_profiles_match_the_requested_error_rates(self):
        self.assertEqual({
            1: {"aim_error": 0.95, "dodge_error": 0.50},
            2: {"aim_error": 0.80, "dodge_error": 0.40},
            3: {"aim_error": 0.60, "dodge_error": 0.30},
            4: {"aim_error": 0.40, "dodge_error": 0.20},
            5: {"aim_error": 0.20, "dodge_error": 0.10},
        }, bot.BOT_DIFFICULTY_PROFILES)

    def test_a_changed_difficulty_refreshes_pending_aim_and_dodge_decisions(self):
        room = self.open_room()
        bot.handle_command(self.host, "/a 2")
        for index in room.bot_seats():
            machine = room.seats[index].conn
            machine.aim_miss_rolled = True
            machine.aim_miss = bot.botaim.Miss(1.0, 2.0)
            machine.intent_tick = 12
            machine.dodge_at = 3
            machine.dodge_signature = (("shell", 100),)
        bot.handle_command(self.host, "/d 1")
        for index in room.bot_seats():
            machine = room.seats[index].conn
            self.assertFalse(machine.aim_miss_rolled)
            self.assertIsNone(machine.aim_miss)
            self.assertIsNone(machine.intent_tick)
            self.assertIsNone(machine.dodge_at)
            self.assertIsNone(machine.dodge_signature)

    def test_d_rejects_invalid_or_per_bot_forms_without_changing_the_level(self):
        room = self.open_room()
        for text in ("/d 0", "/d 6", "/d easy", "/d 1 5"):
            self.host.sent.clear()
            self.assertTrue(bot.handle_command(self.host, text))
            self.assertEqual(3, room.bot_difficulty)
            self.assertIn("/d", "".join(chat_lines(self.host)))
        self.assertIn("不能指定单个 bot", "".join(chat_lines(self.host)))

    # -- 权限 / 时机 --------------------------------------------------------
    def test_a_non_host_command_is_not_swallowed_but_gets_a_hint(self):
        # PLAN M1：别人发的原样当聊天广播出去，不要吞。但也要告诉他为什么
        # 什么都没发生 —— 两件事不矛盾。
        room = self.open_room()
        self.add_guest(room)
        self.assertFalse(bot.handle_command(self.guest, "/a"))
        self.assertEqual([], room.bot_seats())
        self.assertIn("只有房主", "".join(chat_lines(self.guest)))

    def test_commands_outside_a_room_are_consumed_with_a_reason(self):
        self.assertTrue(bot.handle_command(self.host, "/a"))
        self.assertIn("只能在房间里", "".join(chat_lines(self.host)))

    def test_mutating_commands_are_refused_while_the_game_is_running(self):
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        room.status = SESSION_STATUS_PLAYING
        for text in ("/a", "/a 2", "/c 1 2", "/t 1", "/r"):
            self.host.sent.clear()
            self.assertTrue(bot.handle_command(self.host, text))
            self.assertIn("游戏进行中", "".join(chat_lines(self.host)))
        self.assertEqual([1], room.bot_seats())

    def test_help_works_even_while_the_game_is_running(self):
        """★ 战斗中给的是**另一套**（`BATTLE_HELP_LINES`）：房间里那几条
        本来就会被 `MUTATING_COMMANDS` 挡掉，列出来只会占满聊天框那 4 行
        的额度（§20）。"""
        room = self.open_room()
        room.status = SESSION_STATUS_PLAYING
        self.host.sent.clear()
        self.assertTrue(bot.handle_command(self.host, "/help"))
        lines = chat_lines(self.host)
        self.assertEqual(len(bot.BATTLE_HELP_LINES), len(lines))
        self.assertIn("/d", "".join(lines))
        self.assertIn("/w", "".join(lines))

    def test_help_in_the_room_lists_the_room_commands(self):
        self.open_room()
        self.host.sent.clear()
        self.assertTrue(bot.handle_command(self.host, "/help"))
        lines = chat_lines(self.host)
        self.assertEqual(len(bot.HELP_LINES), len(lines))
        self.assertIn("/a", "".join(lines))

    def test_h_help_and_question_mark_show_the_same_help(self):
        self.open_room()
        expected = None
        for text in ("/h", "/help", "/?"):
            self.host.sent.clear()
            self.assertTrue(bot.handle_command(self.host, text))
            lines = chat_lines(self.host)
            self.assertEqual(len(bot.HELP_LINES), len(lines))
            if expected is None:
                expected = lines
            else:
                self.assertEqual(expected, lines)

    def test_help_fits_in_the_four_visible_chat_rows(self):
        # ★ 房间聊天框一次只看得见 4 行，被顶出去的就永远看不到了（§20）。
        #   到边自己折出来的行同样占额度，所以行数和每行宽度都要卡。
        self.assertLessEqual(len(bot.HELP_LINES), 4)
        for line in bot.HELP_LINES + bot.BATTLE_HELP_LINES:
            width = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
                        for c in line)
            self.assertLessEqual(width, 50, f"这行太宽会折行：{line!r}")

    def test_help_lists_every_command_that_is_worth_typing(self):
        # 精简可以，但不能精简掉某条命令 —— 玩家除了这张表没有别的地方能看到。
        table = " ".join(bot.HELP_LINES)
        for name in bot.MUTATING_COMMANDS:
            self.assertIn(f"/{name}", table, f"/{name} 没写进 /?")

    def test_ordinary_chat_and_unknown_slash_words_pass_through(self):
        self.open_room()
        for text in ("你好", "1/2 血了", "/dance", "/s", "/m", "/ 空格", "//"):
            self.assertFalse(bot.handle_command(self.host, text),
                             f"{text!r} 被当成了 bot 命令")

    def test_a_broken_command_handler_never_kills_the_chat_thread(self):
        # bot 命令跑在房主自己的收包线程上：抛出去就是房主掉线。
        room = self.open_room()
        broken = dict(bot.COMMANDS)
        broken["a"] = lambda conn, room_, args: 1 / 0
        with unittest.mock.patch.dict(bot.COMMANDS, broken, clear=True):
            self.assertTrue(bot.handle_command(self.host, "/a"))
        self.assertEqual([], room.bot_seats())
        self.assertIn("出错", "".join(chat_lines(self.host)))


class BotSeatLifecycleTests(LobbyIsolated):
    """房主迁移（D2）和「最后一个真人走了」。"""

    def setUp(self):
        super().setUp()
        self.host = make_conn("alice")
        self.guest = make_conn("bob")

    def open_room(self):
        room = self.lobby.create_room(self.host, title="来玩",
                                      seat=self.host.seat_snapshot(),
                                      **TEAMS_ROOM)
        self.host.sent.clear()
        return room

    def test_host_migration_skips_bot_seats(self):
        # ★ D2：把房主转给 bot = 房间彻底死掉（没人能开局、没人能 /del）。
        room = self.open_room()
        bot.handle_command(self.host, "/a")          # 座位 1
        bot.handle_command(self.host, "/a")          # 座位 2
        self.lobby.join(self.guest, room.room_id,
                        seat=self.guest.seat_snapshot())   # 座位 3
        result = self.lobby.leave(self.host)
        self.assertFalse(result.closed)
        self.assertEqual(3, result.new_host_seat)
        self.assertEqual(3, room.host_seat)
        self.assertIs(self.guest, room.host_conn)

    def test_the_last_human_leaving_takes_every_bot_with_them(self):
        room = self.open_room()
        for _ in range(3):
            bot.handle_command(self.host, "/a")
        room_id = room.room_id
        result = self.lobby.leave(self.host)
        self.assertTrue(result.closed)
        self.assertEqual((1, 2, 3), result.dropped_bots)
        self.assertTrue(room.is_empty())
        self.assertIsNone(self.lobby.get(room_id))
        self.assertEqual([], self.lobby.rooms())
        # bot 的假连接也要从 conn -> room 索引里摘掉，否则它们永远指着一个
        # 已经解散的房间。
        for machine in result.remaining:
            self.assertIsNone(self.lobby.room_of(machine))

    def test_bots_survive_a_non_last_human_leaving(self):
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        self.lobby.join(self.guest, room.room_id,
                        seat=self.guest.seat_snapshot())
        result = self.lobby.leave(self.guest)
        self.assertFalse(result.closed)
        self.assertEqual((), result.dropped_bots)
        self.assertEqual([1], room.bot_seats())

    def test_a_room_with_only_bots_left_never_shows_up_in_the_lobby_list(self):
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        self.lobby.leave(self.host)
        self.assertEqual([], [r.room_id for r in self.lobby.rooms()])
        self.assertEqual(0, room.player_count())

    def test_members_include_bots_but_human_members_do_not(self):
        # ★ 这条正是 §7 那个坑的解法：`sync_peer_relay()` 按
        #   `len(room.members()) >= 2` 判要不要开通道 A，而 bot 有假连接，
        #   所以「1 真人 + N bot」自动算够两个「会动的座位」。
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        members = room.members(exclude=None)
        self.assertEqual(2, len(members))
        self.assertIs(self.host, members[0])
        self.assertIsInstance(members[1], bot.BotConn)
        self.assertEqual([self.host], room.human_members())

    def test_the_client_kick_button_can_also_remove_a_bot(self):
        # bot 在房主的客户端里就是一个普通的占用座位，「踢出」按钮照样点得到。
        # 这条路（`on_kick_out` -> `Lobby.kick` -> `after_someone_left`）会把
        # 一串本来给真人用的收尾动作调到 `BotConn` 头上 —— 它必须全都活得下来。
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        self.host.sent.clear()
        self.host.on_kick_out(gameserver.w_i32(1) + gameserver.w_i32(0))
        self.assertIsNone(room.seats[1])
        self.assertEqual([1], [seat_index
                               for action, seat_index, _s in seat_updates(self.host)
                               if action == SEAT_ACTION_LEAVE])

    def test_a_bot_never_overwrites_its_own_nickname_from_an_account(self):
        # `send_session_members()` 会对**每个房间成员**调 `refresh_seat()`，
        # 而 bot 的 `account` 是 None —— 跑真的那一版就会把昵称刷成空串。
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        machine = room.seats[1].conn
        machine.send_session_members()
        self.assertEqual("bot 1", room.seats[1].nickname)
        self.assertEqual("bot 1", machine.my_nickname())


class CharacterPanelTests(unittest.TestCase):
    """`/c N M` 的 M 是**面板序号**，不是原始角色 id（D6）。"""

    def test_the_panel_has_exactly_fifteen_entries(self):
        # 基础 0/1/2/3 + 商城 100..110。id 98 / 99 客户端放不出来。
        # ★ 爱琳（id 3）是 X_Mod 加的第 4 个基础角色（X1 / D2），
        #   所以商城角色的面板序号整体后移了一位：`/c N 4` 现在是她。
        self.assertEqual(15, len(bot.CHARACTER_PANEL_IDS))
        self.assertEqual((0, 1, 2, 3), bot.CHARACTER_PANEL_IDS[:4])
        self.assertEqual(tuple(range(100, 111)), bot.CHARACTER_PANEL_IDS[4:])

    def test_panel_index_round_trips(self):
        for panel in range(1, 16):
            character = bot.character_for_panel(
                panel, bot.CHARACTER_PANEL_IDS)
            self.assertEqual(panel, bot.panel_for_character(character))

    def test_a_bot_can_only_take_the_three_starter_characters(self):
        """★★ 用户 2026-08-27 拍板：商城角色不给 bot 用（D54）。

        它们的 2/3 号武器里有反弹弹 / 炮台 / 等离子炮，服务端还没有那几类的
        飞行模型（§72）—— 逐个适配的代价和收益不成比例。
        """
        self.assertEqual((0, 1, 2), bot.BOT_CHARACTER_PANEL_IDS)
        for panel in (1, 2, 3):
            self.assertEqual(panel - 1, bot.character_for_panel(panel))
        for panel in (4, 8, 14):
            self.assertIsNone(bot.character_for_panel(panel), panel)

    def test_the_bot_roster_does_not_follow_the_base_roster(self):
        """★★ bot 的名单是**字面量**，故意不跟着 `BASE_CHARACTER_IDS` 走。

        X_Mod 把爱琳（id 3）加成第 4 个基础角色之后，要是这两个常量还写成
        `tuple(BASE_CHARACTER_IDS)`，bot 就会自动开始用她 —— 而她的 2/3 号
        武器是 `SeedBomb` / `TotemLauncher`，服务端同样没有这两类的飞行模型，
        D54 的理由原样适用。给 bot 用爱琳是**另一件事**，得先补武器模型。
        """
        self.assertIn(3, account_store.BASE_CHARACTER_IDS)
        self.assertNotIn(3, bot.BOT_CHARACTER_PANEL_IDS)
        self.assertNotIn(3, bot.BOT_DEFAULT_CHARACTERS)

    def test_out_of_range_and_junk_return_none(self):
        for value in (0, 4, -1, "x", None, ""):
            self.assertIsNone(bot.character_for_panel(value), value)

    def test_default_characters_rotate_so_bots_look_different(self):
        seen = {bot.default_character_for(seat)
                for seat in range(ROOM_SEAT_COUNT)}
        self.assertEqual({0, 1, 2}, seen)


# ----------------------------------------------------------------------------
# M2 · 开局链路
# ----------------------------------------------------------------------------
def player_handle(seat):
    """玩家角色的对象句柄。`0x405f02` 写死的公式，六台机器上一模一样（§11）。"""
    return seat * 100000 + 100001


class BotStartChainTests(BattleRoom):
    """开局链：bot 随 `0x0400` 一起报到（D4）。

    ★ 这个夹具**故意不开局** —— 每个用例自己一步步走，好看清每一步的状态。
    """

    def start_battle(self):
        bot.handle_command(self.alice, "/a")
        self.bot_seat = self.room.bot_seats()[0]
        self.bot_conn = self.room.seats[self.bot_seat].conn

    def ready(self, conn):
        gameserver.Conn.on_game_packet(conn, OP_COUNT_GAME_READY, b"")

    def loaded(self, conn):
        gameserver.Conn.on_game_packet(conn, OP_LOADING_DONE, b"")

    def test_the_bot_is_marked_loaded_the_moment_0x0400_goes_out(self):
        # ★ 判据是「那一发广播出去了」这个事件本身，不是定时器（D4 / 铁律 10）。
        self.ready(self.alice)                       # 0x0401 倒计时
        self.assertNotIn(self.bot_conn, self.room.battle.loaded)
        self.ready(self.alice)                       # 0x0400 准备开局
        self.assertEqual(StartGameHandshake.PREPARING, self.room.battle.state)
        self.assertIn(self.bot_conn, self.room.battle.loaded)
        # 真人一个都还没报到 —— bot 那一发不该顺手把别人也放行了。
        self.assertEqual([self.alice, self.bob],
                         self.room.battle.waiting_for(self.room.members()))

    def test_the_bot_does_not_hold_up_the_start(self):
        """★ 这是 M2 的头号症状：不改的话全房间卡在加载界面。

        服务端会一直等一发 bot 永远不会发的 `0x0403`。
        """
        self.ready(self.alice); self.ready(self.alice)
        self.clear()
        self.loaded(self.alice)
        # 还差 bob；唯一的包是“LoadingStage 已就绪”后确认重画
        # bot 100%，绝不是提前放行 stage 7。
        self.assertEqual([gameserver.OP_PEER_DATA_DOWN], opcodes(self.bob))
        self.loaded(self.bob)                        # 只差这两个真人
        self.assertIn(OP_COUNT_GAME_READY, opcodes(self.alice))
        self.assertIn(OP_COUNT_GAME_READY, opcodes(self.bob))
        self.assertEqual(StartGameHandshake.IN_GAME, self.room.battle.state)

    def test_the_bot_is_marked_loaded_again_on_the_second_round(self):
        """★ `room.battle.reset()` 清 `loaded`，第二局必须重新报一次。"""
        self.ready(self.alice); self.ready(self.alice)
        self.loaded(self.alice); self.loaded(self.bob)
        for conn in (self.alice, self.bob):
            gameserver.Conn.leave_game_result(conn)
        self.assertNotIn(self.bot_conn, self.room.battle.loaded)
        self.clear()
        self.ready(self.alice); self.ready(self.alice)
        self.assertIn(self.bot_conn, self.room.battle.loaded)
        self.loaded(self.alice); self.loaded(self.bob)
        self.assertEqual(StartGameHandshake.IN_GAME, self.room.battle.state)

    def test_the_bot_seats_controller_slots_all_go_to_humans(self):
        """★ §5：bot 分到的控制格没有任何机器在模拟，那批怪从开局就是死的。"""
        self.ready(self.alice); self.ready(self.alice)
        self.clear()
        self.loaded(self.alice); self.loaded(self.bob)
        self.assertNotIn(self.bot_seat, self.room.quest.controllers)
        self.assertEqual({0, 1}, set(self.room.quest.controllers))
        # 交接是**发包**完成的，客户端那张表在它自己手里（§180）。
        for conn in (self.alice, self.bob):
            self.assertIn(OP_CHANGE_CONTROLLER_SLOT, opcodes(conn))

    def test_a_lone_human_takes_every_controller_slot(self):
        """1 个真人 + 一堆 bot：六格全归他，一格都不能留在 bot 手上。"""
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.members = [self.alice]
        for _ in range(4):                          # 夹具已经放了一个
            bot.handle_command(self.alice, "/a")
        self.assertEqual([1, 2, 3, 4, 5], self.room.bot_seats())
        self.ready(self.alice); self.ready(self.alice)
        self.loaded(self.alice)
        self.assertEqual([0] * 6, self.room.quest.controllers)


class BotBattleRoom(BattleRoom):
    """alice（房主，座位 0）+ bob（座位 1）+ 一个 bot（座位 2），已经进了关卡。"""

    def start_battle(self):
        bot.handle_command(self.alice, "/a")
        self.bot_seat = self.room.bot_seats()[0]
        self.bot_conn = self.room.seats[self.bot_seat].conn
        self.bot_handle = player_handle(self.bot_seat)
        super().start_battle()
        # `BattleRoom.setUp` 紧接着就 `clear()`，开局那一段的包留个底。
        self.start_opcodes = {"alice": opcodes(self.alice),
                              "bob": opcodes(self.bob)}
        # ★ 原始字节也留一份：bot 的进度条两头（`0x4005` 的 0 和 100）就发在
        #   这一段里，`clear()` 之后再查是查不到的（V0.3 §38）。
        self.start_sent = {"alice": list(self.alice.sent),
                           "bob": list(self.bob.sent)}


class BotDeathTests(BotBattleRoom):
    """§6 / D3：bot 没有本机，一发合法的 `0x0408` 都不会有 —— 不放宽就打不死。"""

    def test_someone_else_can_report_a_bot_death(self):
        gameserver.Conn.on_game_packet(
            self.bob, OP_REPORT_HP_ZERO,
            hp_zero_payload(handle=self.bot_handle, seat=self.bot_seat,
                            arg=1, deaths=0))
        self.assertEqual([OP_BROADCAST_DEATH], opcodes(self.alice))
        self.assertEqual([OP_BROADCAST_DEATH], opcodes(self.bob))
        self.assertEqual(1, self.room.quest.deaths[self.bot_seat])

    def test_a_real_players_death_reported_by_someone_else_is_still_ignored(self):
        """★ 回归 bug调查/8：放宽**只限 bot 座位**，真人那条判据一个字不许动。

        射手那台算「我炸死他了」、受害者那台算「我躲过去了」的分歧是必然的，
        照单广播就是让客户端对**活着的自己**执行 `Die()`。
        """
        gameserver.Conn.on_game_packet(
            self.alice, OP_REPORT_HP_ZERO,
            hp_zero_payload(handle=player_handle(1), seat=1, arg=0, deaths=0))
        self.assertEqual([], opcodes(self.alice))
        self.assertEqual([], opcodes(self.bob))

    def test_two_machines_reporting_the_same_bot_death_broadcast_once(self):
        """bot 和怪一样是被别人代报的，同一次死亡会被好几台同时报上来。"""
        payload = hp_zero_payload(handle=self.bot_handle, seat=self.bot_seat,
                                  arg=0, deaths=0)
        gameserver.Conn.on_game_packet(self.alice, OP_REPORT_HP_ZERO, payload)
        self.clear()
        gameserver.Conn.on_game_packet(self.bob, OP_REPORT_HP_ZERO, payload)
        self.assertEqual([], opcodes(self.alice))
        self.assertEqual([], opcodes(self.bob))

    def test_a_kill_on_a_bot_still_scores(self):
        gameserver.Conn.on_game_packet(
            self.bob, OP_REPORT_HP_ZERO,
            hp_zero_payload(handle=self.bot_handle, seat=self.bot_seat,
                            arg=1, deaths=0))
        self.assertEqual(1, self.room.quest.kills[1])


class BotRespawnTests(BotBattleRoom):
    """bot 的重生：同一个闩、更短的期限（`BOT_RESPAWN_DELAY_S`）。"""

    def kill_bot(self):
        self.armed_at = time.monotonic()
        gameserver.Conn.on_game_packet(
            self.bob, OP_REPORT_HP_ZERO,
            hp_zero_payload(handle=self.bot_handle, seat=self.bot_seat,
                            arg=1, deaths=0, x=300.0, y=400.0))
        self.clear()

    def test_the_bot_stands_up_after_the_client_respawn_countdown(self):
        self.kill_bot()
        # 5 秒还没到 —— 谁都不该动。
        gameserver.Conn.check_respawn_watchdog(
            self.alice, now=self.armed_at + gameserver.BOT_RESPAWN_DELAY_S - 0.5)
        self.assertEqual([], opcodes(self.alice))
        gameserver.Conn.check_respawn_watchdog(
            self.alice, now=self.armed_at + gameserver.BOT_RESPAWN_DELAY_S + 0.5)
        self.assertEqual([OP_RESPAWN_CHARACTER], opcodes(self.alice))
        self.assertEqual([OP_RESPAWN_CHARACTER], opcodes(self.bob))

    def test_a_real_player_still_waits_for_the_full_watchdog(self):
        """★ 两条期限必须分开：真人那 8 秒是**兜底**，抢跑就会顶掉正常重生。"""
        armed_at = time.monotonic()
        gameserver.Conn.on_game_packet(
            self.alice, OP_REPORT_HP_ZERO,
            hp_zero_payload(handle=player_handle(0), seat=0, deaths=0))
        self.clear()
        gameserver.Conn.check_respawn_watchdog(
            self.alice, now=armed_at + gameserver.BOT_RESPAWN_DELAY_S + 0.5)
        self.assertEqual([], opcodes(self.alice))
        gameserver.Conn.check_respawn_watchdog(
            self.alice, now=armed_at + gameserver.RESPAWN_WATCHDOG_S + 0.5)
        self.assertEqual([OP_RESPAWN_CHARACTER], opcodes(self.alice))

    def test_the_bot_respawn_is_not_logged_as_a_watchdog_alarm(self):
        """★ bot 每次死都走这条路 —— 按看门狗打★报警会把真正的告警淹掉。

        「真人死了不复活」（bug调查/8）就是靠那行★日志抓的。
        """
        self.kill_bot()
        self.alice.logged.clear()
        gameserver.Conn.check_respawn_watchdog(
            self.alice, now=self.armed_at + gameserver.BOT_RESPAWN_DELAY_S + 0.5)
        text = "\n".join(self.alice.logged)
        self.assertIn("0x0419", text)
        self.assertNotIn("[重生看门狗]", text)

    def test_the_bot_respawn_ignores_the_watchdog_kill_switch(self):
        """`--respawn-watchdog 0` 是为了留取证窗口关掉**真人**的兜底 ——
        关掉它不该顺手让 bot 从此躺在地上不起来。"""
        self.alice.args.respawn_watchdog = 0
        self.bob.args.respawn_watchdog = 0
        self.kill_bot()
        gameserver.Conn.check_respawn_watchdog(
            self.alice, now=self.armed_at + gameserver.BOT_RESPAWN_DELAY_S + 0.5)
        self.assertEqual([OP_RESPAWN_CHARACTER], opcodes(self.alice))

    def respawn_body(self):
        """刚刚那一发 `0x0419` 的 4 个 int32。"""
        self.kill_bot()
        gameserver.Conn.check_respawn_watchdog(
            self.alice, now=self.armed_at + gameserver.BOT_RESPAWN_DELAY_S + 0.5)
        body = bodies(self.alice, OP_RESPAWN_CHARACTER)[0]
        return struct.unpack_from("<4i", body, 0)

    def test_the_bot_keeps_its_own_character_when_it_stands_up(self):
        """★★ 用户 2026-08-26 报的「bot 每次复活都换一个角色」（V0.3 §33）。

        `0x0419` 的第 4 格是**角色 id**，不是「重生点索引」——
        客户端 `0x4931c2` 拿它和 `[char+0x2b0]` 比，不一样就把这个座位的
        角色**卸掉重建**成新的那个。以前那版填的是「这张图上最近一个人报的
        重生点索引」，也就是**别人的角色 id**。
        """
        seat, _x, _y, character = self.respawn_body()
        self.assertEqual(self.bot_seat, seat)
        self.assertEqual(self.room.seats[self.bot_seat].character_id, character)

    def test_a_teammates_respawn_does_not_reskin_the_bot(self):
        """真人先复活过一次 —— 他报的角色 id 不许被借给 bot。"""
        gameserver.Conn.on_game_packet(
            self.bob, gameserver.OP_REQ_RESPAWN,
            struct.pack("<4i", 1, 777, 888, 9))
        self.clear()
        _seat, x, y, character = self.respawn_body()
        # 坐标可以借（重生点表整张图共用），角色 id 不行。
        self.assertEqual((777, 888), (x, y))
        self.assertEqual(self.room.seats[self.bot_seat].character_id, character)


class BotQuestLivesTests(BotBattleRoom):
    """★ 闯关模式每人 **3 条命**，用完就该躺着（V0.3 §34）。

    用户 2026-08-26 实机报的「任务模式 bot 3 条命死完了还能继续复活」。
    真人靠客户端自己拦（`Die()` 在 `0x501976` 读剩余生命，为 0 就把
    `[char+0x2d8]` 写成 -1、永不重生），bot 没有客户端 —— 只能服务端拦。
    """

    def kill_and_wait(self, reported):
        """打死 bot 一次，再把时钟拨过它的重生倒计时。返回这一轮的包。"""
        # 两次死亡之间真实世界隔着 5 秒重生倒计时，远长于 bot / 怪那扇
        # 3 秒的代报去重窗（`MONSTER_DEATH_DEDUP_WINDOW_S`）——
        # 用例里直接把窗过掉，不然第二次死亡会被当成重复上报吃掉。
        self.room.quest.last_death_broadcast_at.clear()
        armed_at = time.monotonic()
        gameserver.Conn.on_game_packet(
            self.bob, OP_REPORT_HP_ZERO,
            hp_zero_payload(handle=self.bot_handle, seat=self.bot_seat,
                            arg=1, deaths=reported, x=300.0, y=400.0))
        self.clear()
        gameserver.Conn.check_respawn_watchdog(
            self.alice, now=armed_at + gameserver.BOT_RESPAWN_DELAY_S + 0.5)
        return opcodes(self.alice)

    def test_quest_mode_gives_everyone_three_lives(self):
        """`QuestVictoryCondition` 的构造函数 `0x55e073` 六个座位全写 3。"""
        self.assertEqual(2, self.room.session_type)
        self.assertEqual(gameserver.QUEST_LIVES,
                         gameserver.Conn.max_lives_this_game(self.alice))

    def test_the_bot_stops_respawning_once_its_lives_are_gone(self):
        for reported in range(gameserver.QUEST_LIVES - 1):
            self.assertEqual([OP_RESPAWN_CHARACTER],
                             self.kill_and_wait(reported),
                             f"第 {reported + 1} 次死亡应当照常重生")
        # 第三条命：死亡广播照发（心形要减到 0），但**不再有 0x0419**。
        self.assertEqual([], self.kill_and_wait(gameserver.QUEST_LIVES - 1))
        self.assertEqual(gameserver.QUEST_LIVES,
                         self.room.quest.deaths[self.bot_seat])
        self.assertIn(self.bot_seat, self.room.quest.lives_spent)

    def test_score_modes_still_have_no_life_limit(self):
        """夺分 / 计时是 `0x7fffffff` 条命 —— 这一条不许被顺手改掉。"""
        self.room.session_type = 1
        self.room.arguments = (0, gameserver.PVP_MODE_DEATHMATCH, 0)
        self.assertIsNone(gameserver.Conn.max_lives_this_game(self.alice))


class BotMapChangeTests(BotBattleRoom):
    """换图（闯关）：bot 随 `0x0417` 一起算「新图加载完」（D4）。"""

    def test_the_map_change_does_not_wait_for_the_bot(self):
        gameserver.Conn.on_game_packet(self.alice, OP_REQ_CHANGE_TO_NEXT_MAP,
                                       w_wstr("Quest03_2"))
        # ★ `0x040f` 是 bot 那一发「进度条满了」（D26）—— 换图的加载界面上
        #   也有那几根条，和开局同一个道理。
        self.assertEqual([OP_REP_CHANGE_TO_NEXT_MAP, gameserver.OP_PEER_DATA_DOWN],
                         opcodes(self.alice))
        self.assertIn(self.bot_conn, self.room.quest.map_loaded)
        self.clear()
        # ★ 每个真人自己那发 `0x0412` 都证明「我这台的加载界面建好了」，
        #   各换来一发确认重画的 bot 100（§158，按连接去重）。
        gameserver.Conn.on_game_packet(self.alice, OP_MAP_LOADING_DONE, b"")
        self.assertEqual([gameserver.OP_PEER_DATA_DOWN], opcodes(self.bob))
        gameserver.Conn.on_game_packet(self.bob, OP_MAP_LOADING_DONE, b"")
        both = [gameserver.OP_PEER_DATA_DOWN, gameserver.OP_PEER_DATA_DOWN,
                OP_MAP_CHANGE_READY]
        self.assertEqual(both, opcodes(self.alice))
        self.assertEqual(both, opcodes(self.bob))

    def test_the_bot_is_marked_again_for_the_next_map(self):
        # `begin_map_change` 每次都清 `map_loaded` —— 第二次换图要重新报。
        for name in ("Quest03_2", "Quest03_3"):
            gameserver.Conn.on_game_packet(self.alice,
                                           OP_REQ_CHANGE_TO_NEXT_MAP,
                                           w_wstr(name))
            self.assertIn(self.bot_conn, self.room.quest.map_loaded)
            gameserver.Conn.on_game_packet(self.alice, OP_MAP_LOADING_DONE, b"")
            gameserver.Conn.on_game_packet(self.bob, OP_MAP_LOADING_DONE, b"")
            self.assertIsNone(self.room.quest.pending_map)


class BotSettlementTests(BotBattleRoom):
    """结算：bot 的座位 `account is None`，整条路必须走得下去。"""

    def test_the_bot_gets_its_own_row_in_everyones_settlement(self):
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        for conn in (self.alice, self.bob):
            # 三个在座座位各一份 —— 少发一份，结算界面上那一行就是全 0。
            self.assertEqual(3, len(bodies(conn, OP_REP_GAME_RESULT)))
            self.assertEqual(3, len(bodies(conn, OP_END_GAME)))

    def test_settlement_survives_a_seat_without_an_account(self):
        # ★ 通关那一路会额外调 `record_quest_clear()` —— bot 的 `accounts`
        #   是 None，这一条走不通的话整场结算当场炸在房主的线程上。
        gameserver.Conn.on_game_packet(self.alice,
                                       gameserver.OP_MARK_QUEST_SUCCESS,
                                       gameserver.w_i32(1))
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        self.assertTrue(self.room.quest.settled)
        self.assertTrue(self.bot_conn.settled)
        self.assertIsNone(self.bot_conn.account)

    def test_the_settlement_only_happens_once(self):
        gameserver.Conn.on_game_packet(self.alice, OP_END_QUEST, b"")
        self.clear()
        gameserver.Conn.on_game_packet(self.bob, OP_END_QUEST, b"")
        self.assertEqual([], opcodes(self.alice))


class BotPeerRelayTests(BotBattleRoom):
    """★ 同步转发这条路上多了一个 bot 收件人，绝不能把真人的同步带崩。

    `RelayServer.deliver()` 会对房里**每一个成员**动手（回退投递 / UDP 旁路
    的准入判断都要读收件人的字段），bot 是靠 `BotConn.__init__` 那份
    `Conn.__init__` 镜像撑住的 —— 漏一个字段这里就 `AttributeError`，
    而炸的是**真人**那条线程（D1 说的正是这件事）。
    """

    def test_a_bot_in_the_room_does_not_break_peer_sync(self):
        # 局号必须是**这一代**的那个数，否则 `deliver()` 会按「跨代」整包丢掉
        # （§218 / D137），测出来的就不是 bot 的事了。
        gameserver.Conn.on_game_packet(self.alice, gameserver.OP_PEER_DATA_UP,
                                       udp_packet(game_id=self.room.epoch_value))
        # 真人那一份照常转发过去；bot 的 `send()` 是空操作，收不到也不该炸。
        self.assertIn(gameserver.OP_PEER_DATA_DOWN, opcodes(self.bob))

    def test_the_peer_relay_switch_counts_the_bot_as_a_moving_seat(self):
        """★ §13：1 真人 + N bot 时通道 A **不能**被当成「单人房」关掉。

        `Room.members()` 的判据是 `seat.conn is not None`，D1 选了假连接，
        所以 bot 天然被数成一个会动的座位 —— §7 那个坑因此不存在。
        """
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.assertTrue(self.alice.peer_relay_on)


#: 爱琳 2 号武器（母弹）和它炸出来的蝴蝶；蝴蝶带 `Attribute=4 / 2100 ms`。
IRENE_BOMB = 1003020
IRENE_SPLINTER = 1003520
#: 爱琳 3 号武器放下的那座回血图腾。
IRENE_TOTEM = 1003031


def explode_payload(target_handle, damage=12.0, x=10.0, y=20.0):
    """一发真人打过来的 `0x0003 rpExplode`（28 字节 body，packet_api §5.3）。"""
    return udp_packet(inner=botsync.OP_EXPLODE,
                      body=struct.pack("<iiffiif", 1, target_handle,
                                       x, y, 0, 0, damage))


def totem_payload(seat, group, x, y, ammo=IRENE_TOTEM):
    """一发 `0x001b rpCreateTotem`（22 字节 body，X_Mod §32）。"""
    return udp_packet(inner=botsync.OP_CREATE_TOTEM,
                      body=struct.pack("<BBifffi", seat, group, ammo,
                                       x, y, 0.0, 0))


class BotWeaponAttributeTests(BotBattleRoom):
    """★ 爱琳 2 号武器打中 bot 要**真的减速**（X_Mod §31）。

    线上反馈：「击中 bot 之后，bot 不会被减速」。病根有两条，任一条都足够：
    服务端不知道这把武器带属性（产物里没这两格），而 `machine.slowed_until`
    全工程只有踩胶水一个赋值点。
    """

    def setUp(self):
        super().setUp()
        self.alice.peer_weapon = weapondata.get(IRENE_BOMB)

    def test_the_splinter_slows_the_bot(self):
        before = bot._now()
        bot.note_peer_hit(self.room, self.alice,
                          explode_payload(self.bot_handle))
        self.assertIsNotNone(self.bot_conn.slowed_until)
        # 2100 ms 来自武器的 `AttributeTime`，**不是** `Status.ini[14]` 的 4 秒。
        self.assertAlmostEqual(2.1, self.bot_conn.slowed_until - before,
                               delta=0.5)

    def test_a_human_victim_is_slowed_too(self):
        """★ X_Mod §97：被打的是真人也挂 —— 他自己那台按自己的碰撞挂上了，服务端外推他
        走路要跟着慢（以前只认 bot 受害者）。"""
        bob_seat = self.room.seat_index_of(self.bob)
        bot.note_peer_hit(self.room, self.alice,
                          explode_payload(botsync.character_handle(bob_seat)))
        self.assertIsNotNone(self.bob.slowed_until)
        # 倍率按 f32 算（主线程 24 位精度，X_Mod §105）。
        self.assertEqual(botmove._f32(gameserver.SLOWED_SPEED_RATIO),
                         bot._speed_scale(self.bob, bot._now()))

    def matched(self, ammo):
        """把「按爆点配回的那一发 rpFire」钉成 `ammo` 那一节。"""
        shot = unittest.mock.Mock(weapon=weapondata.get(ammo), poisoned=False)
        return unittest.mock.patch.object(bot, "_match_peer_shot",
                                          return_value=(shot, 0.0, 0.0))

    def test_a_matched_mother_bomb_does_not_slow(self):
        """★ 配得上 rpFire 就按那一发自己的武器判：母弹 `[ch03-02]` 砸中人不挂，
        只有蝴蝶 `[ch03-02a]` 挂（以前手上拿母弹就一律算减速）。"""
        with self.matched(IRENE_BOMB):
            bot.note_peer_hit(self.room, self.alice,
                              explode_payload(self.bot_handle))
        self.assertIsNone(self.bot_conn.slowed_until)
        with self.matched(IRENE_SPLINTER):
            bot.note_peer_hit(self.room, self.alice,
                              explode_payload(self.bot_handle))
        self.assertIsNotNone(self.bot_conn.slowed_until)

    def test_the_slow_actually_reaches_the_walk_speed(self):
        bot.note_peer_hit(self.room, self.alice,
                          explode_payload(self.bot_handle))
        now = bot._now()
        self.assertEqual(botmove._f32(gameserver.SLOWED_SPEED_RATIO),
                         bot._speed_scale(self.bot_conn, now))
        # 到点自己恢复，不靠谁来撤。
        self.assertEqual(1.0, bot._speed_scale(self.bot_conn, now + 9.0))

    def test_nothing_is_broadcast(self):
        """★★★ 和踩胶水**正好相反**：胶水只有「踩上去那台」算，bot 没有本机
        所以必须补广播；而武器属性是 `0x47e0f7` / `0x47ef51` **每台都算**的
        （那两个函数里没有 `IsMine` 门）—— 客户端早就给 bot 挂上了。
        再发一发 `0x040a` 就成了双份，而且 `Item.ini [Slowed]` 走的是
        `Status.ini[14] Time=4.0`，和武器写死的 2.1 秒对不上。"""
        bot.note_peer_hit(self.room, self.alice,
                          explode_payload(self.bot_handle))
        self.assertNotIn(gameserver.OP_ITEM_EFFECT, opcodes(self.bob))
        self.assertNotIn(gameserver.OP_ITEM_EFFECT, opcodes(self.alice))

    def test_a_weapon_without_the_attribute_does_not_slow(self):
        self.alice.peer_weapon = weapondata.get(1000010)      # 泰尔的左轮
        bot.note_peer_hit(self.room, self.alice,
                          explode_payload(self.bot_handle))
        self.assertIsNone(self.bot_conn.slowed_until)

    def test_an_unknown_weapon_does_not_crash(self):
        """没收到过 `rpChangeWeapon` 的人 —— 不知道就当没有，别抛。"""
        self.alice.peer_weapon = None
        bot.note_peer_hit(self.room, self.alice,
                          explode_payload(self.bot_handle))
        self.assertIsNone(self.bot_conn.slowed_until)

    def test_a_splash_hit_does_not_slow(self):
        """★ 溅射（`rpSplashDamaged`）挂不上武器状态（X_Mod §97）：溅射对象由 `0x491702`
        只拿数值建出来，不带弹体的键表 —— 和毒一样只有直接命中才挂（§93）。"""
        body = struct.pack("<iifBff", 1, self.bot_handle, 10.0, 0, 1.0, 1.0)
        body += struct.pack("<ff", 0.0, 0.0)                   # +21 命中点
        body += b"\x00" * (botsync.SPLASH_BODY_SIZE - len(body))
        bot.note_peer_hit(self.room, self.alice,
                          udp_packet(inner=botsync.OP_SPLASH_DAMAGED,
                                     body=body))
        self.assertIsNone(self.bot_conn.slowed_until)

    def test_the_slow_is_extended_never_shortened(self):
        """连着挨两发：留下的是**更远**的那个到期时刻，不是最后一发那个。"""
        bot.note_peer_hit(self.room, self.alice,
                          explode_payload(self.bot_handle))
        first = self.bot_conn.slowed_until
        self.bot_conn.slowed_until = first + 10.0             # 假装踩了胶水
        bot.note_peer_hit(self.room, self.alice,
                          explode_payload(self.bot_handle))
        self.assertEqual(first + 10.0, self.bot_conn.slowed_until)

    def test_the_mapping_is_not_the_status_ini_section_number(self):
        """★ `Attribute=4` 换算出来必须是**属性 14 减速**。
        照 `Status.ini` 小节号读会读成 `[4] 미니비`（缩小），完全是另一回事。"""
        self.assertEqual(14, gameserver.BULLET_ATTRIBUTE_CHAR_ATTR[4])
        self.assertEqual("减速", gameserver.CHAR_ATTR_NAMES[14])
        self.assertEqual(
            (14, 2.1), bot._weapon_attribute(weapondata.get(IRENE_SPLINTER)))
        # 玩家手上拿的是母弹，要顺着 `SliceId` 往下找一层才找得到。
        self.assertEqual(
            (14, 2.1), bot._weapon_attribute(weapondata.get(IRENE_BOMB)))


class BotHealTotemTests(BotBattleRoom):
    """★ 回血图腾：服务端记账 + bot 主动去蹭（X_Mod §32，用户 2026-09-18）。"""

    def setUp(self):
        super().setUp()
        self.machine = self.bot_conn
        self.machine.body = botmove.Body(500.0, 100.0, on_ground=True)
        self.machine.battle_pos = (500.0, 100.0)
        self.group = bot._seat_group(self.room, self.bot_seat)
        self.enemy_group = self.group + 1
        self.ledger = bot._health(self.room)

    def hurt(self, fraction):
        """把 bot 打到剩 `fraction` 成血。"""
        top = bot._seat_max_hp(self.room, self.bot_seat)
        self.ledger.reset(self.bot_seat)
        self.ledger.note_damage(self.bot_seat, top * (1.0 - fraction))

    def place(self, x, y, group=None):
        bot.note_peer_hit(self.room, self.alice,
                          totem_payload(0, self.group if group is None
                                        else group, x, y))
        return self.room.quest.totems[-1]

    # --- 收包 ------------------------------------------------------------
    def test_the_packet_puts_a_totem_on_the_board(self):
        self.place(600.0, 100.0)
        self.assertEqual(1, len(self.room.quest.totems))
        totem = self.room.quest.totems[0]
        self.assertEqual((600.0, 100.0), (totem[0], totem[1]))
        self.assertEqual(IRENE_TOTEM, totem[4])

    def test_an_unknown_ammo_id_is_not_recorded(self):
        """不认得的图腾没有半径可判 —— 记了也用不了，不如不记。"""
        bot.note_peer_hit(self.room, self.alice,
                          totem_payload(0, self.group, 1.0, 2.0, ammo=999999))
        self.assertEqual([], self.room.quest.totems)

    def test_it_expires_on_its_own(self):
        """到期不走任何包：每台机器各自数 `TotemLifeTime`（5 秒）。"""
        totem = self.place(600.0, 100.0)
        now = totem[5]
        self.assertEqual(1, len(bot._live_totems(self.room.quest, now + 4.9)))
        self.assertEqual(0, len(bot._live_totems(self.room.quest, now + 5.1)))
        self.assertEqual([], self.room.quest.totems)   # 当场摘掉，不留垃圾

    # --- 回血 ------------------------------------------------------------
    def test_standing_in_it_heals_the_ledger(self):
        self.hurt(0.5)
        taken = self.ledger.taken_by(self.bot_seat)
        self.place(560.0, 100.0)                       # 距离 60 < 半径 200
        self.assertTrue(bot._stand_in_heal_totem(
            self.room, self.machine, self.bot_seat, bot._now()))
        self.assertEqual(taken - 3, self.ledger.taken_by(self.bot_seat))

    def test_standing_outside_the_radius_heals_nothing(self):
        self.hurt(0.5)
        self.place(900.0, 100.0)                       # 距离 400 > 半径 200
        self.assertFalse(bot._stand_in_heal_totem(
            self.room, self.machine, self.bot_seat, bot._now()))

    def test_an_enemy_totem_heals_nothing(self):
        """`TotemType=1` 要求碰撞排除组相同（`0x488370`）——
        个人战里那一格是「座位 + 1」，所以只有放的人自己吃得到。"""
        self.hurt(0.5)
        self.place(560.0, 100.0, group=self.enemy_group)
        self.assertFalse(bot._stand_in_heal_totem(
            self.room, self.machine, self.bot_seat, bot._now()))

    def test_the_cadence_is_the_originals_proof_time(self):
        """840 ms 一跳（`TotemProofTime`），按**这座图腾 × 这个人**记。"""
        self.hurt(0.5)
        totem = self.place(560.0, 100.0)
        now = totem[5]
        self.assertTrue(bot._stand_in_heal_totem(
            self.room, self.machine, self.bot_seat, now))
        self.assertFalse(bot._stand_in_heal_totem(
            self.room, self.machine, self.bot_seat, now + 0.8))
        self.assertTrue(bot._stand_in_heal_totem(
            self.room, self.machine, self.bot_seat, now + 0.85))

    def test_healing_stops_at_full_health(self):
        """台账下限就是满血（`Ledger.note_heal`）——回过头不会变成负伤害。"""
        self.hurt(0.99)
        totem = self.place(560.0, 100.0)
        now = totem[5]
        for step in range(6):
            bot._stand_in_heal_totem(self.room, self.machine, self.bot_seat,
                                     now + step * 0.9)
        self.assertEqual(1.0, bot._seat_health(self.room, self.bot_seat))

    def test_a_human_standing_in_it_heals_on_the_ledger_too(self):
        """★ X_Mod §91：每台客户端对圈里的**每个**角色都各自回血，真人也一样。

        以前台账只替 bot 记，真人站进去一点不涨 ——[幸运幸存者] 那道
        「剩余 HP < 15」会把回满了的人也当成残血。
        """
        seat = self.room.seat_index_of(self.alice)
        self.alice.sim_body = None
        self.alice.sync_trail.append((560.0, 100.0))    # 距离 60 < 半径 200
        self.ledger.note_damage(seat, 50)
        self.place(500.0, 100.0, group=bot._seat_group(self.room, seat))
        bot._refresh_health(self.room)
        self.assertEqual(47, self.ledger.taken_by(seat))

    def test_the_room_pass_leaves_the_bots_to_their_own_tick(self):
        """bot 那一份在 `_tick_bot()` 里记（`_stand_in_heal_totem`），房间那一遍
        跳过它 —— 两边都记就是一跳回两次。"""
        self.hurt(0.5)
        taken = self.ledger.taken_by(self.bot_seat)
        self.place(560.0, 100.0)
        bot._refresh_health(self.room)
        self.assertEqual(taken, self.ledger.taken_by(self.bot_seat))

    # --- 走位 ------------------------------------------------------------
    def test_full_health_does_not_go_for_it(self):
        self.place(600.0, 100.0)
        self.assertIsNone(bot._totem_goal(self.room, self.machine,
                                          self.bot_seat))

    def test_hurt_and_in_sight_goes_for_it(self):
        self.hurt(0.85)
        self.place(900.0, 100.0)
        goal = bot._totem_goal(self.room, self.machine, self.bot_seat)
        self.assertIsNotNone(goal)
        self.assertEqual((900.0, 100.0), goal[1])
        self.assertAlmostEqual(400.0, goal[0], places=3)

    def test_the_threshold_is_the_one_the_user_gave(self):
        """90% 这个数没有原版出处，是用户 2026-09-18 给的 —— 钉住它。"""
        self.assertEqual(0.90, bot.BOT_TOTEM_HEALTH)
        self.place(900.0, 100.0)
        self.hurt(0.95)
        self.assertIsNone(bot._totem_goal(self.room, self.machine,
                                          self.bot_seat))
        self.hurt(0.89)
        self.assertIsNotNone(bot._totem_goal(self.room, self.machine,
                                             self.bot_seat))

    def test_out_of_the_vision_box_does_not_count(self):
        self.hurt(0.5)
        self.place(500.0 + bot.BOT_VISION_HALF_X + 10.0, 100.0)
        self.assertIsNone(bot._totem_goal(self.room, self.machine,
                                          self.bot_seat))

    def test_already_inside_the_radius_stops_overriding_the_walk(self):
        """★ 用户「回血期间也不要傻站着」：`_walk_to()` 到了目标点返回的
        恰恰是「不动」，所以一进圈就把这一格交还给原来那条链。"""
        self.hurt(0.5)
        self.place(560.0, 100.0)
        self.assertIsNone(bot._totem_goal(self.room, self.machine,
                                          self.bot_seat))

    def test_an_enemy_totem_is_not_a_goal(self):
        self.hurt(0.5)
        self.place(900.0, 100.0, group=self.enemy_group)
        self.assertIsNone(bot._totem_goal(self.room, self.machine,
                                          self.bot_seat))

    def test_an_expired_totem_is_not_a_goal(self):
        self.hurt(0.5)
        totem = self.place(900.0, 100.0)
        totem[5] -= 99.0                               # 假装是 99 秒前放的
        self.assertIsNone(bot._totem_goal(self.room, self.machine,
                                          self.bot_seat))

    def test_the_move_intent_really_picks_the_totem_branch(self):
        """整条走位链走一遍：图腾那一支排在躲子弹之后、捡道具之前。"""
        self.hurt(0.5)
        self.place(900.0, 100.0)
        terrain = flat_terrain()
        self.machine.body = botmove.Body(500.0, 149.0, on_ground=True)
        bot._move_intent(self.room, self.machine, self.bot_seat, terrain,
                         None, bot._now())
        self.assertEqual("去回血图腾", self.machine.diag_src[0])


def splash_payload(target_handle, damage=12.0, flags=0):
    """一发真人打过来的 `0x0004 rpSplashDamaged`（33 字节，`+29` = flags）。"""
    return udp_packet(inner=botsync.OP_SPLASH_DAMAGED,
                      body=botsync.splash_body(7, target_handle, damage,
                                               10.0, 20.0, flags=flags))


class BotLedgerSyncTests(BotBattleRoom):
    """★★ 服务端的血量台账要和**收方真正扣掉的**对上（X_Mod §92）。

    台账错了的后果：bot 以为某人残血 / 满血而进退判反，[幸运幸存者] 那道
    「剩余 HP < 15」掷错。收方 `Character::OnHit` 那几条规矩逐条钉在这儿。
    """

    def setUp(self):
        super().setUp()
        self.ledger = bot._health(self.room)
        # 开局那 2 秒免伤（tick 0 挂的）不是这一组要验的，先摘掉。
        self.ledger.immune.clear()

    def taken(self):
        return self.ledger.taken_by(self.bot_seat)

    # --- 截断 / 格挡 ---------------------------------------------------------
    def test_the_damage_is_truncated_like_the_client(self):
        """分发器拿 `_ftol2` 把 f32 截成整数再扣（`0x491930`）。"""
        bot.note_peer_hit(self.room, self.alice,
                          explode_payload(self.bot_handle, damage=12.9))
        self.assertEqual(12, self.taken())

    def test_a_guarded_hit_lands_a_quarter_plus_one(self):
        """flags 带 0x80 ⇒ `int(GuardDamageRate × 伤害 + 1)`（`0x4ff4dd`）。"""
        body = struct.pack("<iiffiif", 1, self.bot_handle, 10.0, 20.0, 0,
                           bot.EXPLODE_FLAG_GUARD, 20.0)
        bot.note_peer_hit(self.room, self.alice,
                          udp_packet(inner=botsync.OP_EXPLODE, body=body))
        self.assertEqual(int(0.25 * 20 + 1), self.taken())

    def test_the_splash_flags_live_at_29(self):
        """`rpSplashDamaged` 的 flags 在 `+29`（`0x492bf6`），格挡的近身也看它。"""
        bot.note_peer_hit(self.room, self.alice,
                          splash_payload(self.bot_handle, damage=7.0,
                                         flags=bot.EXPLODE_FLAG_GUARD))
        self.assertEqual(int(0.25 * 7 + 1), self.taken())
        self.assertEqual(bot.EXPLODE_FLAG_GUARD, struct.unpack_from(
            "<i", botsync.splash_body(1, 2, 3.0, 0.0, 0.0,
                                      flags=bot.EXPLODE_FLAG_GUARD), 29)[0])

    # --- 免伤：`OnHit` 进门那几道门 ------------------------------------------
    def test_a_shielded_seat_takes_nothing(self):
        """属性 1 护盾：收方只放 Shield00.efx、一滴血不扣。"""
        now = time.monotonic()
        self.room.quest.shield_until[self.bot_seat] = now + 8.0
        bot.note_peer_hit(self.room, self.alice, explode_payload(self.bot_handle))
        self.assertEqual(0, self.taken())
        self.room.quest.shield_until[self.bot_seat] = now - 0.1
        bot.note_peer_hit(self.room, self.alice, explode_payload(self.bot_handle))
        self.assertEqual(12, self.taken())

    def test_using_a_shield_is_recorded(self):
        self.bot_conn.note_area_item(gameserver.SHIELD_ITEM_ID, self.bot_seat,
                                     self.room.quest)
        self.assertGreater(self.room.quest.shield_until[self.bot_seat],
                           time.monotonic() + 7.0)

    def test_the_two_seconds_after_a_respawn(self):
        """「躺 -> 站」那一下 = `Character::Respawn`：满血 + 状态 0 锁 2 秒。"""
        quest = self.room.quest
        quest.respawn_due[self.bot_seat] = (time.monotonic() + 5.0, (0, 0))
        bot._refresh_health(self.room)                 # 记下「躺着」
        self.ledger.note_damage(self.bot_seat, 30)
        del quest.respawn_due[self.bot_seat]
        bot._refresh_health(self.room)                 # 站起来了
        self.assertEqual(0, self.taken(), "复活满血")
        bot.note_peer_hit(self.room, self.alice, explode_payload(self.bot_handle))
        self.assertEqual(0, self.taken(), "这 2 秒打不掉血")
        self.assertFalse(bot._immune(self.room, self.bot_seat,
                                     time.monotonic() + bot.SPAWN_IMMUNE_S))

    def respawn(self, seat, killer):
        """走一遍「被 `killer` 打死 -> 复活」：死亡广播记凶手、闩上、撤闩。"""
        quest = self.room.quest
        quest.last_killer[seat] = killer
        quest.respawn_due[seat] = (time.monotonic() + 5.0, (0, 0))
        bot._refresh_health(self.room)
        del quest.respawn_due[seat]
        bot._refresh_health(self.room)

    def deathmatch(self):
        self.room.session_type = 1
        self.room.arguments = (0, 3, 0)            # 个人战 / 夺分
        for seat in self.room.seats:               # 个人战没有队伍：碰撞组 = 座位 + 1
            if seat is not None:
                seat.team = 0

    def test_killed_by_an_enemy_in_deathmatch_buys_seven_seconds(self):
        """★ 状态 0x10「분노부활」（`Respawn` `0x50311f`）：夺分、凶手是别组的人、
        不是自杀 ⇒ 复活后 7 秒（218 格）整发不扣血。"""
        self.deathmatch()
        now = time.monotonic()
        self.respawn(self.bot_seat, killer=0)
        self.assertTrue(bot._immune(self.room, self.bot_seat, now + 6.5))
        self.assertFalse(bot._immune(self.room, self.bot_seat, now + 7.2))

    def test_no_rage_for_a_suicide_or_outside_deathmatch(self):
        self.deathmatch()
        now = time.monotonic()
        self.respawn(self.bot_seat, killer=self.bot_seat)    # 自杀
        self.assertFalse(bot._immune(self.room, self.bot_seat, now + 3.0))
        self.room.arguments = (0, 0, 0)                      # 生存
        self.respawn(self.bot_seat, killer=0)
        self.assertFalse(bot._immune(self.room, self.bot_seat, now + 3.0))

    def test_entering_the_stage_is_not_a_respawn(self):
        """★ 订正：开局 / 换图**不跑** `Respawn`（只从 `0x0419` 进来），Init 反而把状态 0 撤掉
        ⇒ 进图那一刻没有免伤（X_Mod §94）。"""
        bot._refresh_health(self.room)
        for seat in (0, 1, self.bot_seat):
            self.assertFalse(bot._immune(self.room, seat, time.monotonic()), seat)

    # --- 回血事件（X_Mod §94）------------------------------------------------
    def test_picking_up_a_heart_heals_fifteen(self):
        """心的回血量是写死的 15（`0x5228f9`）；闯关里就是 15。"""
        self.ledger.note_damage(self.bot_seat, 40)
        self.room.quest.heal_events.append(("heart", self.bot_seat, 0, -1))
        bot._refresh_health(self.room)
        self.assertEqual(25, self.taken())

    def test_a_heart_in_deathmatch_heals_forty(self):
        """夺分里捡心 `15 × 2.67` 取整 = 40（`0x52299a`）。"""
        self.deathmatch()
        self.assertEqual(40, bot._heart_heal_amount(self.room))

    def test_the_heartboost_heal_and_its_side_effect(self):
        """10316：回 `量` 一次；顺带把目标的复活免伤撤掉（`Add(状态 0, 0 格)`）。"""
        self.ledger.note_damage(self.bot_seat, 20)
        self.ledger.grant_immunity(self.bot_seat, time.monotonic() + 2.0, state=0)
        self.room.quest.heal_events.append(
            (gameserver.HEART_BOOST_ITEM_ID, self.bot_seat, 5, 0))
        bot._refresh_health(self.room)
        self.assertEqual(15, self.taken())
        self.assertFalse(bot._immune(self.room, self.bot_seat, time.monotonic()))

    def test_the_title_heart_heals_once_per_other_live_teammate(self):
        """10315：目标每有一个「活着、同组、不是发起人」的角色就回一次 `量`
        （`0x508a52`~`0x508b94`，原版就是这么写的）。闯关里大家同组。"""
        self.ledger.note_damage(self.bot_seat, 40)
        groups = {bot._seat_group(self.room, s) for s in (0, 1, self.bot_seat)}
        self.assertEqual(1, len(groups), "这一组要大家同组")
        self.room.quest.heal_events.append(
            (gameserver.TITLE_HEART_ITEM_ID, self.bot_seat, 5, 0))
        bot._refresh_health(self.room)
        # 除了发起人 0 号，活着的同组还有 1 号和 bot 自己 ⇒ 回两次。
        self.assertEqual(40 - 2 * 5, self.taken())

    def test_the_heart_effects_are_queued_by_the_relay(self):
        """`0x040b` → 广播 `0x040a` 的同时往回血事件里排一条。"""
        payload = struct.pack("<iiii", self.bot_seat, self.alice.my_seat,
                              gameserver.HEART_BOOST_ITEM_ID, 5)
        gameserver.Conn.on_game_packet(self.alice, 0x040b, payload)
        self.assertEqual(
            [(gameserver.HEART_BOOST_ITEM_ID, self.bot_seat, 5,
              self.alice.my_seat)],
            list(self.room.quest.heal_events))

    def test_the_quest_master_buys_eight_seconds(self):
        """内层 `0x001a`（属性 0x14）= 闯关达人那 8 秒免伤。"""
        now = time.monotonic()
        body = struct.pack("<bii", 1, bot.QUEST_MASTER_ATTR, 1)
        bot.note_peer_hit(self.room, self.bob,
                          udp_packet(inner=bot.PEER_OP_ADD_ATTR, body=body))
        self.assertTrue(bot._immune(self.room, 1, now + 7.5))
        self.assertFalse(bot._immune(self.room, 1, now + 8.5))

    # --- 中毒（X_Mod §93）---------------------------------------------------
    def advance_poison_to(self, when):
        with bot._tick_clock(when):
            bot._advance_poison(self.room, self.ledger)

    def test_a_poison_is_six_ticks_of_five(self):
        """8 秒 = 250 格，每 47 格一跳：第 0/47/94/141/188/235 格 ⇒ 6 跳 30 点。"""
        t0 = time.monotonic() + 100.0
        with bot._tick_clock(t0):
            bot._poison_seat(self.room, self.bot_seat, "单测")
        self.advance_poison_to(t0)
        self.assertEqual(5, self.taken(), "中毒那一刻就跳第一下")
        self.advance_poison_to(t0 + 20.0)
        self.assertEqual(30, self.taken())
        self.assertFalse(self.ledger.poisoned(self.bot_seat), "8 秒后毒解了")

    def test_a_blocked_tick_is_lost_not_delayed(self):
        """每一跳走 `OnHit`：护盾挡住的那一跳白跳，下一跳照原节奏（`[0x68c]` 照推）。"""
        t0 = time.monotonic() + 100.0
        interval = gameserver.POISON_INTERVAL
        with bot._tick_clock(t0):
            bot._poison_seat(self.room, self.bot_seat, "单测")
        self.advance_poison_to(t0)
        self.room.quest.shield_until[self.bot_seat] = t0 + interval + 0.5
        self.advance_poison_to(t0 + interval + 0.01)        # 第 2 跳被盾吃掉
        self.assertEqual(5, self.taken())
        self.advance_poison_to(t0 + 2 * interval + 0.01)    # 第 3 跳照常
        self.assertEqual(10, self.taken())

    def test_poisoning_again_only_refreshes_the_expiry(self):
        """再中一次：到期续满 8 秒，节奏不重排（`0x401bd6`）。"""
        t0 = time.monotonic() + 100.0
        interval = gameserver.POISON_INTERVAL
        with bot._tick_clock(t0):
            bot._poison_seat(self.room, self.bot_seat, "单测")
        self.advance_poison_to(t0 + 3.0)                    # 第 0、1 跳
        with bot._tick_clock(t0 + 3.0):
            bot._poison_seat(self.room, self.bot_seat, "单测")
        self.advance_poison_to(t0 + 30.0)
        ticks = int((3.0 + gameserver.POISON_SECONDS) // interval) + 1
        self.assertEqual(5 * ticks, self.taken())

    def test_a_poison_shot_from_a_human_poisons_the_bot(self):
        """真人挂着毒弹（`0x040c` 用了 10500）直接命中 bot ⇒ bot 中毒。"""
        self.alice.note_area_item(gameserver.POISON_ITEM_ID, self.alice.my_seat,
                                  self.room.quest)
        self.assertIn(self.alice.my_seat, self.room.quest.poison_magazine)
        bot.note_peer_hit(self.room, self.alice, explode_payload(self.bot_handle))
        self.assertTrue(self.ledger.poisoned(self.bot_seat))

    def test_the_magazine_ends_with_his_0x040d(self):
        """弹匣打完是他自己那台数的，发 `0x040d(座位, 10)` 来说（§200）。"""
        self.room.quest.poison_magazine.add(self.alice.my_seat)
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REMOVE_CHAR_ATTR,
            struct.pack("<ii", self.alice.my_seat,
                        gameserver.POISON_MAGAZINE_ATTR))
        self.assertNotIn(self.alice.my_seat, self.room.quest.poison_magazine)
        bot.note_peer_hit(self.room, self.alice, explode_payload(self.bot_handle))
        self.assertFalse(self.ledger.poisoned(self.bot_seat))

    def test_a_splash_never_poisons(self):
        self.room.quest.poison_magazine.add(self.alice.my_seat)
        bot.note_peer_hit(self.room, self.alice, splash_payload(self.bot_handle))
        self.assertFalse(self.ledger.poisoned(self.bot_seat))

    def test_dying_wipes_every_status(self):
        """`Character::Die` 整张属性表清掉、不发 `0x040d`（X_Mod §93）。"""
        quest = self.room.quest
        seat = self.bot_seat
        far = time.monotonic() + 60.0
        quest.shield_until[seat] = far
        quest.reflect_until[seat] = far
        quest.hp_charges[seat] = far
        self.ledger.poison(seat, time.monotonic(), 8.0)
        self.bot_conn.magazine_attrs = {gameserver.POISON_MAGAZINE_ATTR: 3}
        bot._refresh_health(self.room)
        quest.respawn_due[seat] = (far, (0, 0))           # 死了
        bot._refresh_health(self.room)
        self.assertNotIn(seat, quest.shield_until)
        self.assertNotIn(seat, quest.reflect_until)
        self.assertNotIn(seat, quest.hp_charges)
        self.assertFalse(self.ledger.poisoned(seat))
        self.assertEqual({}, self.bot_conn.magazine_attrs,
                         "bot 复活后不该还是强力 / 毒弹")


class BotMidGameLeaveTests(BotBattleRoom):
    """游戏中有人掉线 —— 房主迁移、控制权、房间解散三条都要跟着走。"""

    def test_the_host_leaving_hands_the_room_to_a_human(self):
        gameserver.Conn.on_game_packet(self.alice, OP_LEAVE_SESSION, b"")
        self.assertEqual(1, self.room.host_seat)
        self.assertIs(self.bob, self.room.host_conn)
        # 走的人扛的控制格必须落到**真人**头上，不能落到 bot 手里。
        self.assertNotIn(self.bot_seat, self.room.quest.controllers)
        self.assertNotIn(0, self.room.quest.controllers)

    def test_the_last_human_leaving_disbands_the_room_and_its_bots(self):
        gameserver.Conn.on_game_packet(self.alice, OP_LEAVE_SESSION, b"")
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.assertEqual([], self.lobby.rooms())
        self.assertIsNone(self.lobby.room_of(self.bot_conn))


class ParseCommandTests(unittest.TestCase):
    def test_command_names_are_case_insensitive_and_trimmed(self):
        self.assertEqual(("char", ["1", "7"]),
                         bot.parse_command("  /CHAR 1 7  "))

    def test_extra_whitespace_between_arguments_is_ignored(self):
        self.assertEqual(("del", ["3"]), bot.parse_command("/del    3"))

    def test_non_commands_return_none(self):
        for text in ("", "   ", "hi", "a/b", "/", "/  "):
            self.assertIsNone(bot.parse_command(text), repr(text))

    def test_every_documented_name_has_a_handler(self):
        for name in bot.COMMAND_NAMES:
            self.assertIn(name, bot.COMMANDS, name)


if __name__ == "__main__":
    unittest.main()
