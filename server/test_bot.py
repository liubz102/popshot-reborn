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
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import bot                                                     # noqa: E402
import gameserver                                              # noqa: E402
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
    def test_a_new_room_defaults_to_medium_and_the_three_commands_are_global(self):
        room = self.open_room()
        bot.handle_command(self.host, "/a 2")
        self.assertEqual("medium", room.bot_difficulty)
        for text, level in (("/s", "easy"), ("/h", "hard"), ("/m", "medium")):
            self.host.sent.clear()
            self.assertTrue(bot.handle_command(self.host, text))
            self.assertEqual(level, room.bot_difficulty)
            self.assertIn(bot.BOT_DIFFICULTY_LABELS[level],
                          "".join(chat_lines(self.host)))

    def test_difficulty_changes_work_during_battle_and_survive_a_new_game(self):
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        machine = room.seats[1].conn
        room.status = SESSION_STATUS_PLAYING
        self.assertTrue(bot.handle_command(self.host, "/h"))
        self.assertEqual("hard", room.bot_difficulty)
        machine.reset_battle_frame()       # 换图 / 后来新开一局
        self.assertEqual("hard", room.bot_difficulty)
        self.assertEqual(bot.BOT_DIFFICULTY_PROFILES["hard"],
                         bot.difficulty_profile(room))

    def test_the_two_error_probabilities_decrease_with_difficulty(self):
        easy = bot.BOT_DIFFICULTY_PROFILES["easy"]
        medium = bot.BOT_DIFFICULTY_PROFILES["medium"]
        hard = bot.BOT_DIFFICULTY_PROFILES["hard"]
        self.assertGreater(easy["aim_error"], medium["aim_error"])
        self.assertGreater(medium["aim_error"], hard["aim_error"])
        self.assertGreater(easy["dodge_error"], medium["dodge_error"])
        self.assertGreater(medium["dodge_error"], hard["dodge_error"])

    def test_old_s_with_a_seat_argument_points_at_hold_without_freezing(self):
        room = self.open_room()
        bot.handle_command(self.host, "/a")
        machine = room.seats[1].conn
        self.host.sent.clear()
        self.assertTrue(bot.handle_command(self.host, "/s 1"))
        self.assertFalse(machine.holding)
        self.assertIn("/hold", "".join(chat_lines(self.host)))

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
        self.assertIn("/s", "".join(lines))
        self.assertIn("/w", "".join(lines))

    def test_help_in_the_room_lists_the_room_commands(self):
        self.open_room()
        self.host.sent.clear()
        self.assertTrue(bot.handle_command(self.host, "/help"))
        lines = chat_lines(self.host)
        self.assertEqual(len(bot.HELP_LINES), len(lines))
        self.assertIn("/a", "".join(lines))

    def test_help_has_two_aliases_and_h_is_reserved_for_hard(self):
        self.open_room()
        for text in ("/help", "/?"):
            self.host.sent.clear()
            self.assertTrue(bot.handle_command(self.host, text))
            self.assertEqual(len(bot.HELP_LINES), len(chat_lines(self.host)))
        self.host.sent.clear()
        self.assertTrue(bot.handle_command(self.host, "/h"))
        self.assertEqual(1, len(chat_lines(self.host)))

    def test_help_fits_in_the_four_visible_chat_rows(self):
        # ★ 房间聊天框一次只看得见 4 行，被顶出去的就永远看不到了（§20）。
        #   到边自己折出来的行同样占额度，所以行数和每行宽度都要卡。
        self.assertLessEqual(len(bot.HELP_LINES), 4)
        for line in bot.HELP_LINES:
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
        for text in ("你好", "1/2 血了", "/dance", "/ 空格", "//"):
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

    def test_the_panel_has_exactly_fourteen_entries(self):
        # 基础 0/1/2 + 商城 100..110。id 3 / 98 / 99 客户端放不出来。
        self.assertEqual(14, len(bot.CHARACTER_PANEL_IDS))
        self.assertEqual((0, 1, 2), bot.CHARACTER_PANEL_IDS[:3])
        self.assertEqual(tuple(range(100, 111)), bot.CHARACTER_PANEL_IDS[3:])

    def test_panel_index_round_trips(self):
        for panel in range(1, 15):
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
