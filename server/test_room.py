#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""里程碑 I（大厅联机）的协议测试。

`test_lobby.py` 测房间表本身；这里测**线格式和广播时序** ——
组包对不对、包的顺序对不对、该收到的人有没有收到。

依据全部在 `.claude/FINDINGS.md` §137~§142。
"""
import os
import struct
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gameserver                                              # noqa: E402
from gameserver import (                                       # noqa: E402
    CHAT_NO_SEAT, OP_CHAT, OP_LEAVE_SESSION, OP_LIST_SESSION,
    OP_COUNT_GAME_READY, OP_LOADING_DONE,
    OP_JOIN_RELAY, OP_LEAVE_RELAY, OP_START_TCP_RELAY,
    OP_MOVE_INTO_SESSION, OP_PEER_DATA_DOWN, OP_PEER_DATA_UP,
    OP_REQ_USER_LIST, OP_REP_USER_LIST,
    OP_PREPARE_GAME, OP_SEAT_READY, OP_SESSION_MEMBERS,
    OP_SESSION_MEMBER_UPDATE, OP_TRIGGER_COUNT_GAME,
    OP_SLOT_EQUIPPED_LIST, OP_TOGGLE_PEER_RELAY, OP_UPDATE_SESSION,
    ROOM_SEAT_COUNT,
    SEAT_ACTION_CHANGE_CHARACTER, SEAT_ACTION_JOIN, SEAT_ACTION_LEAVE,
    SEAT_ACTION_RESYNC,
    SESSION_STATUS_PLAYING, SESSION_STATUS_WAITING, StartGameHandshake,
    Reader, build_game, build_rep_list_session, build_receive_chat,
    build_rep_move_into_session, build_session, build_session_slot,
    build_update_session,
    lobby_game_type, parse_chat_message, parse_kick_out_request,
    parse_list_session_request, parse_move_into_request,
    parse_quick_join_request, parse_session_slot, parse_user_list_request,
    read_session_descriptor,
    take_frame, w_i32, w_wstr,
)
from lobby import (Lobby, MOVE_INTO_ALREADY_PLAYING, MOVE_INTO_BAD_PASSWORD,
                   MOVE_INTO_FULL, MOVE_INTO_NO_SUCH_ROOM, MOVE_INTO_OK,
                   TEAM_A, TEAM_B, TEAM_LAYOUT_COOP, TEAM_LAYOUT_FREE,
                   TEAM_LAYOUT_TEAMS, TEAM_NONE, default_team)
import relayserver                                                  # noqa: E402
from simple import SimpleCipher                                     # noqa: E402


# ----------------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------------
def frames(blob):
    """把一串连着的帧拆成 `[(kind, opcode, payload), ...]`。

    `send_batch()` 会把好几个包合成**一次** `sendall`（V0.1 §120），
    所以断言必须能看穿这一层，否则「合并了没有」和「顺序对不对」都测不到。
    """
    out = []
    rest = blob
    while rest:
        kind, opcode, payload, consumed = take_frame(rest)
        if kind is None:
            raise AssertionError(f"拆帧失败，剩余 {len(rest)} 字节: {rest[:32]!r}")
        out.append((kind, opcode, payload))
        rest = rest[consumed:]
    return out


def opcodes(conn):
    """某条连接收到的全部 opcode，按顺序（跨批次拉平）。"""
    return [op for blob in conn.sent for _, op, _ in frames(blob)]


class Args:
    hold_lobby = False
    room_burst_delay = 0
    login_result = 0


ACCOUNTS = {
    "alice": {"display_name": "Alice", "level": 3, "experience": 200,
              "money": 10, "character": 0},
    "bob": {"display_name": "Bob", "level": 5, "experience": 400,
            "money": 20, "character": 1},
    "carol": {"display_name": "Carol", "level": 7, "experience": 600,
              "money": 30, "character": 2},
}


def make_conn(username):
    """一条只会把发出去的字节攒进 `sent` 的假连接。

    走 `Conn.__new__` 而不是复制一份逻辑 —— 测的必须是**真的接线**
    （和 `test_gameserver.py` 里那些夹具同一个套路）。
    """
    conn = gameserver.Conn.__new__(gameserver.Conn)
    conn.addr = ("::ffff:127.0.0.1", 40000)
    conn.args = Args()
    conn.sent = []
    conn.logged = []
    conn.log = conn.logged.append
    conn.online = lambda _msg: None
    conn.online_debug = lambda _msg: None
    conn.vlog = lambda _msg: None
    # ★ 走**真的**换代钩子：换代模型的唯一迁移点就在 `Conn.send()` 里
    #   （§218 / D137）。假连接直接把 `send` 换成 `sent.append` 的话，
    #   开局链发出去的 0x0400 / 0x0403 就不会推进模型，测的就不是真接线了。
    def _send(plain):
        gameserver.Conn.note_epoch_from_frame(conn, plain)
        conn.sent.append(plain)

    conn.send = _send
    conn.send_lock = threading.RLock()
    conn.send_queue = None
    conn.batch_delay_ms = 0
    conn.last_packet_at = 0.0
    conn.noisy_seen = set()
    conn.account_name = username
    conn.account = dict(ACCOUNTS[username])
    conn.room = None
    conn.my_seat = 0
    conn.settled = False
    conn.quest_score = 0
    conn.quest_success = False
    conn.items_created = 0
    conn.items_picked = 0
    conn.solo_quest = gameserver.RoomQuest()
    conn.start_game = StartGameHandshake()
    conn.peer_relay_on = False
    conn.peer_data_dumped = False
    conn.peer_data_in = 0
    conn.peer_data_out = 0
    conn.peer_forward_ms = gameserver.relayserver.RttStats()
    conn.peer_gap_ms = gameserver.relayserver.RttStats()
    conn.peer_last_at = None
    conn.peer_report_at = 0.0
    return conn


def create_session_payload(title="来玩", session_type=2, arguments=(3, 1)):
    """客户端方向的 `0x0201` 载荷：三个字符串 + int32 + 描述符。"""
    return (w_wstr(title) + w_wstr("") + w_wstr("")
            + w_i32(0)
            + w_i32(session_type)
            + b"".join(w_i32(v) for v in arguments))


def list_request(game_type=2, start_room=0, waiting_only=0, page_size=10):
    """客户端方向的 `0x0200` 载荷（12 字节，§139 / §170）。

    `waiting_only` 就是第 4 个字段：大厅左下角「全部」发 0、「待机」发 1。
    """
    return (struct.pack("<HHH", start_room, 0, page_size)
            + struct.pack("<BB", waiting_only, 0)
            + w_i32(game_type))


def move_into_payload(room_id, password="", flag=0):
    return w_i32(room_id) + w_wstr(password) + w_i32(flag)


def change_session_payload(session_type=1, arguments=(1, 3, 0), title="来玩",
                           map_name="Festival00:NewPvp"):
    """客户端方向的 `0x0302 gcpChangeSession` 载荷（见 parse_change_session_request）。"""
    return (w_i32(0) + w_wstr(title) + w_wstr(map_name)
            + w_i32(0) + w_i32(0)
            + w_i32(session_type)
            + b"".join(w_i32(v) for v in arguments))


class LobbyIsolated(unittest.TestCase):
    """每个用例一张干净的大厅表 —— `LOBBY` 是模块级单例，用例之间会互相污染。

    中继（`PEER_RELAY`）同理：它记着「哪条游戏连接已经回过 `0x0210` 了」
    这张去重表（§159），跨用例带着走就会让后面的用例莫名其妙收不到包。
    """

    def setUp(self):
        self._saved_lobby = gameserver.LOBBY
        gameserver.LOBBY = Lobby()
        self.lobby = gameserver.LOBBY
        self._saved_relay = gameserver.PEER_RELAY
        gameserver.PEER_RELAY = relayserver.RelayServer(
            members_of=gameserver._relay_room_members,
            fallback=gameserver._relay_fallback,
            on_traffic=gameserver._relay_battle_tick)
        self.relay = gameserver.PEER_RELAY
        self._saved_relay_enabled = gameserver.TCP_RELAY_ENABLED

    def tearDown(self):
        gameserver.LOBBY = self._saved_lobby
        gameserver.PEER_RELAY = self._saved_relay
        gameserver.TCP_RELAY_ENABLED = self._saved_relay_enabled


# ----------------------------------------------------------------------------
# 线格式
# ----------------------------------------------------------------------------
class SessionWireTests(unittest.TestCase):
    def test_update_session_is_a_session_plus_one_u16(self):
        # §137：0x556ed1 = 0x556e80（Session）+ 再读一个 u16。
        session = build_session(SESSION_STATUS_WAITING, 2, (3, 1),
                                title="T", map_name="M", player_count=4)
        full = build_update_session(2, (3, 1), title="T", map_name="M",
                                    status=SESSION_STATUS_WAITING,
                                    player_count=4)
        self.assertEqual(session + struct.pack("<H", 0), full)

    def test_session_fields_are_in_the_documented_order(self):
        blob = build_session(SESSION_STATUS_WAITING, 2, (3, 1),
                             title="标题", map_name="map-01", player_count=4)
        reader = Reader(blob)
        self.assertEqual(SESSION_STATUS_WAITING, reader.i32())
        self.assertEqual("标题", reader.wstr())
        self.assertEqual(4, reader.i32())            # +0x0c 人数分子
        self.assertEqual("map-01", reader.wstr())
        self.assertEqual(0, reader.i32())            # +0x14 语义未知
        self.assertEqual((2, (3, 1)), read_session_descriptor(reader))
        self.assertEqual(0, reader.left())

    def test_update_session_still_defaults_to_the_v01_shape(self):
        # V0.1 起「建房那一次地图名必须为空」是硬约束，默认值不许被改掉。
        blob = build_update_session(2, (3, 1), title="T")
        reader = Reader(blob)
        reader.i32()
        reader.wstr()
        self.assertEqual(0, reader.i32())
        self.assertEqual("", reader.wstr())


class ListSessionWireTests(LobbyIsolated):
    def test_empty_list_is_still_the_four_bytes_v01_shipped(self):
        # V0.1 实测客户端接受 `00 00 00 00`，不许因为重构变形。
        self.assertEqual(struct.pack("<HH", 0, 0), build_rep_list_session())

    def test_list_entry_layout(self):
        conn = make_conn("alice")
        room = self.lobby.create_room(conn, title="房", session_type=2,
                                      arguments=(3, 1), seat=conn.seat_snapshot())
        self.lobby.update_room(room, map_name="map-01")
        reader = Reader(build_rep_list_session([room]))
        self.assertEqual(1, reader.u16())                      # 房间数
        self.assertEqual(SESSION_STATUS_WAITING, reader.i32())
        self.assertEqual("房", reader.wstr())
        self.assertEqual(1, reader.i32())                      # 当前人数
        self.assertEqual("map-01", reader.wstr())
        self.assertEqual(0, reader.i32())
        self.assertEqual((2, (3, 1)), read_session_descriptor(reader))
        self.assertEqual(room.room_id, reader.u16())           # 房间号
        self.assertEqual(ROOM_SEAT_COUNT, reader.take(1)[0])   # 人数分母
        self.assertEqual(0, reader.i32())                      # 语义未定
        self.assertEqual(len([room]), reader.u16())            # 尾部总数
        self.assertEqual(0, reader.left())

    def test_parse_list_request_reads_the_game_type(self):
        request = parse_list_session_request(list_request(game_type=2,
                                                          start_room=7))
        self.assertEqual(2, request["game_type"])
        self.assertEqual("quest", request["game_type_name"])
        self.assertEqual(7, request["start_room"])

    def test_parse_list_request_reads_the_waiting_only_switch(self):
        # 第 4 个字节 = 左下角「全部 / 待机」（§170）。以前当「未定字段」丢掉了。
        self.assertFalse(parse_list_session_request(
            list_request(waiting_only=0))["waiting_only"])
        self.assertTrue(parse_list_session_request(
            list_request(waiting_only=1))["waiting_only"])
        self.assertEqual(10, parse_list_session_request(
            list_request())["page_size"])

    def test_parse_list_request_rejects_trailing_bytes(self):
        with self.assertRaises(ValueError):
            parse_list_session_request(list_request() + b"\x00")

    def test_list_reply_only_contains_rooms_of_the_requested_type(self):
        alice, bob = make_conn("alice"), make_conn("bob")
        gameserver.Conn.on_game_packet(alice, 0x0201,
                                       create_session_payload(session_type=2))
        gameserver.Conn.on_game_packet(bob, 0x0201,
                                       create_session_payload("对战",
                                                              session_type=1,
                                                              arguments=(1, 2, 3)))
        carol = make_conn("carol")
        gameserver.Conn.on_game_packet(carol, OP_LIST_SESSION,
                                       list_request(game_type=2))
        payload = [p for blob in carol.sent for _, op, p in frames(blob)
                   if op == OP_LIST_SESSION][-1]
        self.assertEqual(1, Reader(payload).u16())

    def test_the_waiting_tab_hides_rooms_that_are_playing(self):
        alice, bob = make_conn("alice"), make_conn("bob")
        gameserver.Conn.on_game_packet(alice, 0x0201,
                                       create_session_payload("打着呢",
                                                              session_type=2))
        gameserver.Conn.on_game_packet(bob, 0x0201,
                                       create_session_payload("等人",
                                                              session_type=2))
        self.lobby.room_of(alice).status = gameserver.SESSION_STATUS_PLAYING
        carol = make_conn("carol")

        def titles(waiting_only):
            carol.sent.clear()
            gameserver.Conn.on_game_packet(
                carol, OP_LIST_SESSION,
                list_request(game_type=2, waiting_only=waiting_only))
            payload = [p for blob in carol.sent for _, op, p in frames(blob)
                       if op == OP_LIST_SESSION][-1]
            reader = Reader(payload)
            out = []
            for _ in range(reader.u16()):
                reader.i32()                      # 状态
                out.append(reader.wstr())         # 标题
                reader.i32(), reader.wstr(), reader.i32()
                read_session_descriptor(reader)
                reader.u16(), reader.take(1), reader.i32()
            return out

        self.assertEqual(["打着呢", "等人"], titles(0))   # 「全部」
        self.assertEqual(["等人"], titles(1))             # 「待机」

    def test_list_reply_is_empty_when_no_room_matches(self):
        carol = make_conn("carol")
        gameserver.Conn.on_game_packet(carol, OP_LIST_SESSION,
                                       list_request(game_type=5))
        payload = [p for blob in carol.sent for _, op, p in frames(blob)
                   if op == OP_LIST_SESSION][-1]
        self.assertEqual(struct.pack("<HH", 0, 0), payload)

    def test_a_broken_request_still_gets_a_list_back(self):
        # 空列表和「服务端挂了」在玩家眼里一样，所以解析失败要退回「不过滤」，
        # 而不是干脆不回。
        carol = make_conn("carol")
        gameserver.Conn.on_game_packet(carol, OP_LIST_SESSION, b"\x00\x01")
        self.assertEqual([OP_LIST_SESSION], opcodes(carol))


class MoveIntoWireTests(unittest.TestCase):
    def test_parse_request(self):
        request = parse_move_into_request(move_into_payload(3, "pw", 1))
        self.assertEqual({"room_id": 3, "password": "pw", "flag": 1}, request)

    def test_parse_request_rejects_trailing_bytes(self):
        with self.assertRaises(ValueError):
            parse_move_into_request(move_into_payload(3) + b"\x00")

    def test_reply_is_four_int32(self):
        blob = build_rep_move_into_session(MOVE_INTO_OK, 7, 3)
        self.assertEqual(16, len(blob))
        reader = Reader(blob)
        self.assertEqual([0, 7, 3, 0],
                         [reader.i32(), reader.i32(), reader.i32(), reader.i32()])

    def test_quick_join_payload_is_just_a_descriptor(self):
        payload = w_i32(2) + w_i32(3) + w_i32(1)
        self.assertEqual((2, (3, 1)), parse_quick_join_request(payload))
        with self.assertRaises(ValueError):
            parse_quick_join_request(payload + b"\x00")

    def test_lobby_game_type_mapping(self):
        self.assertEqual(2, lobby_game_type(2))
        self.assertEqual(1, lobby_game_type(1))
        self.assertEqual(5, lobby_game_type(5))
        self.assertEqual(1, lobby_game_type(99))     # 认不出的按普通


class ChatWireTests(unittest.TestCase):
    def test_parse_client_chat(self):
        payload = struct.pack("<B", 3) + w_wstr("你好")
        self.assertEqual((3, "你好"), parse_chat_message(payload))

    def test_parse_client_chat_rejects_trailing_bytes(self):
        with self.assertRaises(ValueError):
            parse_chat_message(struct.pack("<B", 0) + w_wstr("hi") + b"\x00")

    def test_broadcast_chat_layout(self):
        reader = Reader(build_receive_chat("你好", sender="Alice",
                                           seat_index=2, chat_type=1))
        self.assertEqual(2, reader.u16())
        self.assertEqual("Alice", reader.wstr())
        self.assertEqual("你好", reader.wstr())
        self.assertEqual(1, reader.i32())
        self.assertEqual(0, reader.left())

    def test_system_message_leaves_the_sender_empty(self):
        # 显示名为空时客户端只渲染 '%s'，不带「谁 : 」前缀（§141）。
        reader = Reader(build_receive_chat("有人进来了。"))
        self.assertEqual(CHAT_NO_SEAT, reader.u16())
        self.assertEqual("", reader.wstr())
        self.assertEqual("有人进来了。", reader.wstr())


class KickWireTests(unittest.TestCase):
    def test_parse_kick_request(self):
        self.assertEqual((2, 1), parse_kick_out_request(w_i32(2) + w_i32(1)))

    def test_parse_kick_request_rejects_trailing_bytes(self):
        with self.assertRaises(ValueError):
            parse_kick_out_request(w_i32(2) + w_i32(1) + b"\x00")


# ----------------------------------------------------------------------------
# 接线 / 广播时序
# ----------------------------------------------------------------------------
class JoinFlowTests(LobbyIsolated):
    def setUp(self):
        super().setUp()
        self.alice = make_conn("alice")
        gameserver.Conn.on_game_packet(self.alice, 0x0201,
                                       create_session_payload())
        self.room = self.lobby.room_of(self.alice)
        self.alice.sent.clear()
        self.bob = make_conn("bob")

    def test_creating_a_room_registers_it_in_the_lobby(self):
        self.assertIsNotNone(self.room)
        self.assertEqual(0, self.room.room_id)
        self.assertEqual("来玩", self.room.title)
        self.assertEqual(2, self.room.session_type)
        self.assertEqual("Alice", self.room.seats[0].nickname)

    def test_join_sends_the_four_packets_in_the_required_order(self):
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        # ★ 顺序是硬约束（§140 / V0.1 §119）：
        #   0x0303 -> 0x0202 -> 0x0300 -> 0x030b
        # 末尾那发 0x0410 是「房里够两个人了，玩家间同步开」（§150），
        # 必须排在四连发**之后**。
        self.assertEqual([OP_UPDATE_SESSION, OP_MOVE_INTO_SESSION,
                          OP_SESSION_MEMBERS, OP_SLOT_EQUIPPED_LIST,
                          OP_TOGGLE_PEER_RELAY],
                         opcodes(self.bob))

    def test_join_reply_carries_the_room_id_and_seat(self):
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        payload = [p for blob in self.bob.sent for _, op, p in frames(blob)
                   if op == OP_MOVE_INTO_SESSION][0]
        reader = Reader(payload)
        self.assertEqual(MOVE_INTO_OK, reader.i32())
        self.assertEqual(self.room.room_id, reader.i32())
        self.assertEqual(1, reader.i32())
        self.assertEqual(1, self.bob.my_seat)

    def test_the_seat_snapshot_shows_both_players(self):
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        payload = [p for blob in self.bob.sent for _, op, p in frames(blob)
                   if op == OP_SESSION_MEMBERS][0]
        reader = Reader(payload)
        self.assertEqual(0, reader.i32())        # 房主座位
        reader.i32()
        names = []
        for _ in range(ROOM_SEAT_COUNT):
            slot = gameserver.parse_session_slot(reader)
            names.append(slot.get("nickname") if slot["occupied"] else None)
        self.assertEqual(["Alice", "Bob", None, None, None, None], names)

    def test_the_host_is_told_that_someone_joined(self):
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        got = opcodes(self.alice)
        self.assertEqual([OP_SESSION_MEMBER_UPDATE, OP_CHAT,
                          OP_TOGGLE_PEER_RELAY], got)
        payload = [p for blob in self.alice.sent for _, op, p in frames(blob)
                   if op == OP_SESSION_MEMBER_UPDATE][0]
        self.assertEqual(SEAT_ACTION_JOIN, payload[0])
        self.assertEqual(1, Reader(payload[1:]).i32())

    def test_wrong_password_gets_the_password_error_code(self):
        self.lobby.update_room(self.room, password="1234")
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id, "x"))
        payload = [p for blob in self.bob.sent for _, op, p in frames(blob)
                   if op == OP_MOVE_INTO_SESSION][0]
        self.assertEqual(MOVE_INTO_BAD_PASSWORD, Reader(payload).i32())
        # 失败时**只**回一个包，不能顺手把房间数据也发出去。
        self.assertEqual([OP_MOVE_INTO_SESSION], opcodes(self.bob))

    def test_unknown_room_gets_no_such_room(self):
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(999))
        payload = [p for blob in self.bob.sent for _, op, p in frames(blob)
                   if op == OP_MOVE_INTO_SESSION][0]
        self.assertEqual(MOVE_INTO_NO_SUCH_ROOM, Reader(payload).i32())

    def test_full_room_gets_the_full_code(self):
        for i in range(ROOM_SEAT_COUNT - 1):
            other = make_conn("carol")
            other.account_name = f"carol{i}"
            self.lobby.join(other, self.room.room_id, seat=other.seat_snapshot())
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        payload = [p for blob in self.bob.sent for _, op, p in frames(blob)
                   if op == OP_MOVE_INTO_SESSION][0]
        self.assertEqual(MOVE_INTO_FULL, Reader(payload).i32())

    def test_quick_join_replies_with_0x0202_not_0x0205(self):
        # 0x0205 的服务端方向只会弹提示框（处理器 0x55027d），进不了房间。
        gameserver.Conn.on_game_packet(self.bob, 0x0205,
                                       w_i32(2) + w_i32(3) + w_i32(1))
        self.assertEqual([OP_UPDATE_SESSION, OP_MOVE_INTO_SESSION,
                          OP_SESSION_MEMBERS, OP_SLOT_EQUIPPED_LIST,
                          OP_TOGGLE_PEER_RELAY],
                         opcodes(self.bob))
        self.assertIs(self.room, self.lobby.room_of(self.bob))

    def test_quick_join_with_nothing_available_says_no_such_room(self):
        gameserver.Conn.on_game_packet(self.bob, 0x0205,
                                       w_i32(5) + w_i32(1) + w_i32(2) + w_i32(3))
        payload = [p for blob in self.bob.sent for _, op, p in frames(blob)
                   if op == OP_MOVE_INTO_SESSION][0]
        self.assertEqual(MOVE_INTO_NO_SUCH_ROOM, Reader(payload).i32())


class JoinBatchTests(LobbyIsolated):
    """进房四连发必须落成**一次** `sendall`（§120 / D058）。

    上面那些用例把 `conn.send` 换成了 `list.append`，`send_batch()` 对它们
    是透明的 —— 所以「到底合并了没有」只能用真的 `send` + 假 socket 来测，
    和 `test_gameserver.SendBatchTests` 同一个套路。
    """

    class FakeSocket:
        def __init__(self):
            self.writes = []

        def sendall(self, data):
            self.writes.append(bytes(data))

    def make_socket_conn(self, username):
        conn = make_conn(username)
        conn.sock = self.FakeSocket()
        conn.cout = SimpleCipher.server_to_client()
        del conn.send                      # 用回真的 Conn.send
        return conn

    def test_join_merges_the_four_packets_into_one_sendall(self):
        alice = make_conn("alice")
        gameserver.Conn.on_game_packet(alice, 0x0201, create_session_payload())
        room = self.lobby.room_of(alice)

        bob = self.make_socket_conn("bob")
        gameserver.Conn.on_game_packet(bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(room.room_id))
        # 被客户端的 recv 切开会让「人物选择」缩回 3 个头像，这一局回不来。
        # ★ 后面那发 0x0410（玩家间同步开关）是**单独一次** sendall ——
        #   挤进这一批就等于把四连发拉长成五连发，同一条禁忌。
        self.assertEqual(2, len(bob.sock.writes))
        cipher = SimpleCipher.server_to_client()
        plain = cipher.decrypt(bob.sock.writes[0])
        self.assertEqual([OP_UPDATE_SESSION, OP_MOVE_INTO_SESSION,
                          OP_SESSION_MEMBERS, OP_SLOT_EQUIPPED_LIST],
                         [op for _, op, _ in frames(plain)])
        self.assertEqual([OP_TOGGLE_PEER_RELAY],
                         [op for _, op, _ in
                          frames(cipher.decrypt(bob.sock.writes[1]))])


class LeaveFlowTests(LobbyIsolated):
    def setUp(self):
        super().setUp()
        self.alice = make_conn("alice")
        gameserver.Conn.on_game_packet(self.alice, 0x0201,
                                       create_session_payload())
        self.room = self.lobby.room_of(self.alice)
        self.bob = make_conn("bob")
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        self.alice.sent.clear()
        self.bob.sent.clear()

    def test_leaving_broadcasts_a_seat_action_that_destroys_the_model(self):
        # ★ 只有 action 1/2 会走 0x406676 -> 0x405f8f 销毁座位的 3D 模型。
        # 发 3 的话玩家列表里的名字没了、天空那块的模型还杵着（§147）。
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.assertEqual([OP_LEAVE_SESSION], opcodes(self.bob))
        payload = [p for blob in self.alice.sent for _, op, p in frames(blob)
                   if op == OP_SESSION_MEMBER_UPDATE][0]
        self.assertIn(payload[0], (1, 2))
        self.assertEqual(SEAT_ACTION_LEAVE, payload[0])
        self.assertEqual(1, Reader(payload[1:]).i32())
        self.assertIsNone(self.lobby.room_of(self.bob))

    def test_host_leaving_transfers_and_resends_the_seat_snapshot(self):
        # 「谁是房主」只有 0x0300 的第一个 int32 说了算，所以转移必须重发快照。
        gameserver.Conn.on_game_packet(self.alice, OP_LEAVE_SESSION, b"")
        got = opcodes(self.bob)
        # 末尾那发 0x0410 是「房里只剩一个人了，玩家间同步关」（§150）。
        self.assertEqual([OP_SESSION_MEMBER_UPDATE, OP_SESSION_MEMBERS, OP_CHAT,
                          OP_TOGGLE_PEER_RELAY],
                         got)
        payload = [p for blob in self.bob.sent for _, op, p in frames(blob)
                   if op == OP_SESSION_MEMBERS][0]
        self.assertEqual(1, Reader(payload).i32())
        self.assertEqual(1, self.room.host_seat)

    def test_disconnect_frees_the_seat(self):
        # 断线不摘座位的话房间里会留一个永远不动的幽灵玩家。
        gameserver.Conn.leave_room(self.bob, "Bob 断线了。")
        self.assertIsNone(self.lobby.room_of(self.bob))
        self.assertEqual(1, self.room.player_count())
        self.assertIn(OP_SESSION_MEMBER_UPDATE, opcodes(self.alice))

    def test_last_player_leaving_closes_the_room(self):
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        gameserver.Conn.on_game_packet(self.alice, OP_LEAVE_SESSION, b"")
        self.assertEqual([], self.lobby.rooms())

    def test_kick_tells_the_victim_and_the_room(self):
        gameserver.Conn.on_game_packet(self.alice, gameserver.OP_KICK_OUT,
                                       w_i32(1) + w_i32(0))
        self.assertEqual([OP_LEAVE_SESSION], opcodes(self.bob))
        self.assertIsNone(self.lobby.room_of(self.bob))
        self.assertEqual(1, self.room.player_count())
        # 房主那边也要收到座位变更，且 action 必须是会销毁模型的那种（§147）
        # —— 否则被踢的人从玩家列表里消失了，模型还留在房间里。
        payload = [p for blob in self.alice.sent for _, op, p in frames(blob)
                   if op == OP_SESSION_MEMBER_UPDATE][0]
        self.assertIn(payload[0], (1, 2))
        self.assertEqual(1, Reader(payload[1:]).i32())

    def test_a_non_host_cannot_kick(self):
        # 客户端只给房主那个按钮，但改包就能绕过 —— 服务端必须自己判。
        gameserver.Conn.on_game_packet(self.bob, gameserver.OP_KICK_OUT,
                                       w_i32(0) + w_i32(0))
        self.assertEqual([], opcodes(self.alice))
        self.assertEqual(2, self.room.player_count())

    def test_the_host_cannot_kick_itself(self):
        gameserver.Conn.on_game_packet(self.alice, gameserver.OP_KICK_OUT,
                                       w_i32(0) + w_i32(0))
        self.assertEqual(2, self.room.player_count())


class ChatFlowTests(LobbyIsolated):
    def setUp(self):
        super().setUp()
        self.alice = make_conn("alice")
        gameserver.Conn.on_game_packet(self.alice, 0x0201,
                                       create_session_payload())
        self.room = self.lobby.room_of(self.alice)
        self.bob = make_conn("bob")
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        self.alice.sent.clear()
        self.bob.sent.clear()

    def send_chat(self, conn, text, chat_type=0):
        gameserver.Conn.on_game_packet(
            conn, OP_CHAT, struct.pack("<B", chat_type) + w_wstr(text))

    def test_the_speaker_gets_a_copy_too(self):
        # 客户端发完 0x0305 什么都不做，本地不回显（和换角色 §103 同一个套路）。
        self.send_chat(self.bob, "大家好")
        self.assertEqual([OP_CHAT], opcodes(self.bob))
        payload = [p for blob in self.bob.sent for _, _op, p in frames(blob)][0]
        reader = Reader(payload)
        self.assertEqual(1, reader.u16())
        self.assertEqual("Bob", reader.wstr())
        self.assertEqual("大家好", reader.wstr())

    def test_everyone_in_the_room_gets_it_with_the_sender_name(self):
        self.send_chat(self.bob, "大家好")
        payload = [p for blob in self.alice.sent for _, op, p in frames(blob)
                   if op == OP_CHAT][0]
        reader = Reader(payload)
        self.assertEqual(1, reader.u16())         # bob 的座位号
        self.assertEqual("Bob", reader.wstr())
        self.assertEqual("大家好", reader.wstr())

    def test_empty_and_whitespace_only_messages_are_dropped(self):
        self.send_chat(self.bob, "   ")
        self.assertEqual([], opcodes(self.bob))
        self.assertEqual([], opcodes(self.alice))

    def test_chat_outside_a_room_only_echoes_to_the_speaker(self):
        carol = make_conn("carol")
        self.send_chat(carol, "有人吗")
        self.assertEqual([OP_CHAT], opcodes(carol))
        self.assertEqual([], opcodes(self.alice))
        payload = [p for blob in carol.sent for _, _op, p in frames(blob)][0]
        self.assertEqual(CHAT_NO_SEAT, Reader(payload).u16())


class CharacterChangeTests(LobbyIsolated):
    def setUp(self):
        super().setUp()
        self.alice = make_conn("alice")
        gameserver.Conn.on_game_packet(self.alice, 0x0201,
                                       create_session_payload())
        self.room = self.lobby.room_of(self.alice)
        self.bob = make_conn("bob")
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        # 换角色要写存档，给一个真的能记账的假存储。
        for conn in (self.alice, self.bob):
            conn.accounts = self.FakeAccounts(conn)
            conn.sent.clear()

    class FakeAccounts:
        def __init__(self, conn):
            self.conn = conn

        def set_character(self, username, character):
            account = dict(self.conn.account, character=character)
            return account

    def seat_change_payload(self, seat_index, character):
        return (w_i32(seat_index)
                + w_i32(1) + w_wstr("Bob") + struct.pack("<B", 0)
                + w_i32(character) + w_i32(0)
                + w_i32(0) + struct.pack("<H", 0) + struct.pack("<H", 5)
                + w_i32(0) + struct.pack("<H", 0) + w_wstr("") + w_i32(0)
                + w_i32(0))

    def test_character_change_is_broadcast_to_the_whole_room(self):
        gameserver.Conn.on_game_packet(self.bob, OP_SESSION_MEMBER_UPDATE,
                                       self.seat_change_payload(1, 2))
        self.assertEqual([OP_SESSION_MEMBER_UPDATE], opcodes(self.bob))
        self.assertEqual([OP_SESSION_MEMBER_UPDATE], opcodes(self.alice))
        payload = [p for blob in self.alice.sent for _, _op, p in frames(blob)][0]
        self.assertEqual(SEAT_ACTION_CHANGE_CHARACTER, payload[0])
        self.assertEqual(1, Reader(payload[1:]).i32())

    def test_a_lying_seat_index_is_corrected_to_the_real_one(self):
        # 客户端自报座位 0（它建房时的老值），实际坐 1 —— 信它会把广播
        # 打到房主的位置上。
        gameserver.Conn.on_game_packet(self.bob, OP_SESSION_MEMBER_UPDATE,
                                       self.seat_change_payload(0, 2))
        payload = [p for blob in self.alice.sent for _, _op, p in frames(blob)][0]
        self.assertEqual(1, Reader(payload[1:]).i32())
        self.assertEqual(1, self.bob.my_seat)


class TeamAndReadyTests(LobbyIsolated):
    """「变更队伍」和「游戏准备」（FINDINGS §165）。

    两条都是用户 2026-08-12 实机报回来的缺陷：
      1. 点「变更队伍」队伍没变，还冒出一条**换角色**的韩文提示
         （服务端不分青红皂白一律回 action 4）；
      2. 非房主按「游戏准备」只有自己看得见，房主也按不动「开始」
         （服务端根本不认 `0x030e`，没人广播）。
    """

    #: 「组队战」房间：描述符 type 1 + `arguments[0] == 1`
    #: （客户端的 `0x409df1` 读的就是这一格）。
    TEAM_ROOM = dict(session_type=1, arguments=(1, 3, 0))

    def setUp(self):
        super().setUp()
        self.alice = make_conn("alice")
        gameserver.Conn.on_game_packet(
            self.alice, 0x0201, create_session_payload(**self.TEAM_ROOM))
        self.room = self.lobby.room_of(self.alice)
        self.bob = make_conn("bob")
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        # 换角色要写存档，给一个真的能记账的假存储。
        for conn in (self.alice, self.bob):
            conn.accounts = CharacterChangeTests.FakeAccounts(conn)
            conn.sent.clear()

    # -- 座位默认值 --------------------------------------------------------
    def test_seats_are_assigned_alternating_teams(self):
        # 客户端自己填 Dummy 座位就是 `座位号 % 2 + 1`（0x468952）。
        self.assertEqual(TEAM_A, self.room.seats[0].team)
        self.assertEqual(TEAM_B, self.room.seats[1].team)
        self.assertFalse(self.room.seats[0].ready)
        self.assertFalse(self.room.seats[1].ready)

    def test_quest_room_puts_everyone_on_one_team(self):
        # ★ 闯关是合作：队伍号一样 = 打不到队友（`0x4fedfc` 比队伍号，
        #   相同就不结算伤害，而且那一处不分模式）。
        carol = make_conn("carol")
        gameserver.Conn.on_game_packet(carol, 0x0201,
                                       create_session_payload(session_type=2,
                                                              arguments=(3, 1)))
        room = self.lobby.room_of(carol)
        dave = make_conn("alice")
        gameserver.Conn.on_game_packet(dave, OP_MOVE_INTO_SESSION,
                                       move_into_payload(room.room_id))
        self.assertEqual([TEAM_A, TEAM_A],
                         [room.seats[0].team, room.seats[1].team])

    def test_free_for_all_leaves_everyone_unteamed(self):
        # 个人战：**人人 0 = 没分队**。以前发「座位号 + 1」，3 人以上就会让
        # 客户端 `vf34`（`0x55c696`）按 `this + 40*(队伍号-1)` 写出两格的
        # 队伍数组、踩进别人的战绩记录 —— 死亡次数被越写越大，剩余生命
        # `max(0, 3 - 死亡次数)` 归零，于是「活着进观战」+「死了不复活」。
        # 见 lobby.TEAM_LAYOUT_* 的说明（bug调查/8_2 §212）。
        carol = make_conn("carol")
        gameserver.Conn.on_game_packet(
            carol, 0x0201,
            create_session_payload(session_type=1, arguments=(0, 3, 0)))
        room = self.lobby.room_of(carol)
        # ★ 三人以上才是出事的量级（两人局队伍号 1/2 不越界），所以这里
        #   一定要坐满三个以上。
        for name in ("alice", "bob", "carol"):
            other = make_conn(name)
            gameserver.Conn.on_game_packet(other, OP_MOVE_INTO_SESSION,
                                           move_into_payload(room.room_id))
        self.assertEqual([TEAM_NONE] * 4,
                         [room.seats[i].team for i in range(4)])

    def test_no_room_layout_ever_sends_a_team_above_two(self):
        # ★ 铁律：线上出去的队伍号只能是 0 / 1 / 2。客户端的队伍记录数组
        #   只有两格，>= 3 就是越界写。三种口径 × 六个座位全查一遍。
        for layout in (TEAM_LAYOUT_TEAMS, TEAM_LAYOUT_FREE, TEAM_LAYOUT_COOP):
            for seat in range(6):
                team = default_team(seat, layout)
                self.assertIn(team, (TEAM_NONE, TEAM_A, TEAM_B),
                              f"{layout} 座位 {seat} 发了队伍号 {team}")

    def test_session_slot_clamps_an_out_of_range_team(self):
        # 线格式那一处也设了闸：万一哪天有别的路径塞进 3，也不会发出去。
        payload = build_session_slot(occupied=True, nickname="ab", team=3)
        self.assertEqual(0, payload[4 + 2 + 4])     # i32(1) + u16 长度 + "ab"

    def test_switching_the_room_mode_regroups_and_broadcasts(self):
        # 房主在房间里点「个人战」-> 分队口径变了 -> 重排 + 每个变了的座位
        # 补一发 action 3。组队战是 1/2/1，个人战全 0，所以三个座位都变。
        carol = make_conn("carol")
        gameserver.Conn.on_game_packet(carol, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        self.assertEqual(TEAM_A, self.room.seats[2].team)
        for conn in (self.alice, self.bob, carol):
            conn.sent.clear()
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_CHANGE_SESSION,
            change_session_payload(session_type=1, arguments=(0, 3, 0)))
        self.assertEqual(TEAM_LAYOUT_FREE, self.room.team_layout())
        self.assertEqual([TEAM_NONE] * 3, [s.team for s in self.room.seats[:3]])
        # 三个座位都变了，所以别人该收到三发 action 3
        self.assertIn(OP_SESSION_MEMBER_UPDATE, opcodes(self.bob))
        payloads = [p for blob in self.bob.sent for _, op, p in frames(blob)
                    if op == OP_SESSION_MEMBER_UPDATE]
        self.assertEqual(3, len(payloads))
        self.assertEqual({0, 1, 2},
                         {Reader(p[1:]).i32() for p in payloads})
        for payload in payloads:
            self.assertEqual(SEAT_ACTION_RESYNC, payload[0])

    def test_a_manual_team_choice_survives_someone_joining(self):
        gameserver.Conn.on_game_packet(
            self.bob, OP_SESSION_MEMBER_UPDATE,
            self.seat_slot_payload(1, character=1, team=TEAM_A))
        carol = make_conn("carol")
        gameserver.Conn.on_game_packet(carol, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        self.assertEqual(TEAM_A, self.room.seats[1].team)   # 没被冲掉
        self.assertEqual(TEAM_A, self.room.seats[2].team)   # 新人按座位号

    def test_team_and_ready_land_on_the_right_bytes(self):
        # 队伍是 **1 字节**（`0x5d5942`），准备是 int32（`0x5d5956`）——
        # 写错宽度后面所有字段全串位。
        payload = build_session_slot(occupied=True, nickname="ab", team=2,
                                     level=7, ready=True)
        expected = (
            struct.pack("<i", 1)                              # 占用
            + struct.pack("<H", 2) + "ab".encode("utf-16le")  # 昵称
            + struct.pack("<B", 2)                            # ★ 队伍，1 字节
            + struct.pack("<i", 0)                            # 角色 id
            + struct.pack("<i", 0)                            # 物品列表的 0 结尾
            + struct.pack("<i", 0)                            # +0x28
            + struct.pack("<H", 0)                            # +0x2c
            + struct.pack("<H", 7)                            # 等级
            + struct.pack("<i", 1)                            # ★ 准备，int32
            + struct.pack("<H", 0)                            # +0x12
            + struct.pack("<H", 0)                            # 空串
            + struct.pack("<i", 0)                            # +0x34
            + struct.pack("<i", 0)                            # 关闭
        )
        self.assertEqual(expected, payload)

    # -- 变更队伍 ----------------------------------------------------------
    def seat_slot_payload(self, seat_index, *, character, team, nickname="Bob",
                          level=5, ready=False):
        """客户端方向的 `0x0301`：座位号 + 一整个 SessionSlot。"""
        return w_i32(seat_index) + build_session_slot(
            occupied=True, nickname=nickname, team=team,
            character_id=character, level=level, ready=ready)

    def test_team_change_uses_action_3_and_is_broadcast(self):
        # bob 坐 1 号位（默认 2 队），把自己切到 1 队。
        gameserver.Conn.on_game_packet(
            self.bob, OP_SESSION_MEMBER_UPDATE,
            self.seat_slot_payload(1, character=1, team=TEAM_A))
        self.assertEqual(TEAM_A, self.room.seats[1].team)
        for conn in (self.bob, self.alice):
            self.assertEqual([OP_SESSION_MEMBER_UPDATE], opcodes(conn))
            payload = [p for blob in conn.sent for _, _op, p in frames(blob)][0]
            # ★ action 3，不是 4 —— 4 会播「%s님이 %s 캐릭터로…」
            self.assertEqual(SEAT_ACTION_RESYNC, payload[0])
            self.assertEqual(1, Reader(payload[1:]).i32())
            slot = parse_session_slot(Reader(payload[5:]))
            self.assertEqual(TEAM_A, slot["team"])

    def test_team_change_does_not_touch_the_character(self):
        gameserver.Conn.on_game_packet(
            self.bob, OP_SESSION_MEMBER_UPDATE,
            self.seat_slot_payload(1, character=1, team=TEAM_A))
        self.assertEqual(1, self.room.seats[1].character_id)

    def test_character_change_still_uses_action_4(self):
        # 角色变了就是换角色，哪怕队伍那一格也对不上（角色优先）。
        gameserver.Conn.on_game_packet(
            self.bob, OP_SESSION_MEMBER_UPDATE,
            self.seat_slot_payload(1, character=2, team=TEAM_A))
        payload = [p for blob in self.alice.sent
                   for _, _op, p in frames(blob)][0]
        self.assertEqual(SEAT_ACTION_CHANGE_CHARACTER, payload[0])
        self.assertEqual(TEAM_B, self.room.seats[1].team)   # 队伍没被改掉

    def test_character_change_keeps_team_and_ready_on_the_wire(self):
        # 换角色那一发**也要带上队伍和准备状态**，否则会把它们抹成 0。
        self.room.seats[1].ready = True
        gameserver.Conn.on_game_packet(
            self.bob, OP_SESSION_MEMBER_UPDATE,
            self.seat_slot_payload(1, character=2, team=TEAM_B, ready=True))
        payload = [p for blob in self.alice.sent
                   for _, _op, p in frames(blob)][0]
        slot = parse_session_slot(Reader(payload[5:]))
        self.assertEqual(TEAM_B, slot["team"])
        self.assertTrue(slot["ready"])

    def test_host_may_change_someone_elses_team(self):
        # 客户端 0x469f4f 自己就是这么判的：房主动谁都行。
        gameserver.Conn.on_game_packet(
            self.alice, OP_SESSION_MEMBER_UPDATE,
            self.seat_slot_payload(1, character=1, team=TEAM_A))
        self.assertEqual(TEAM_A, self.room.seats[1].team)

    def test_a_guest_may_not_change_someone_elses_team(self):
        gameserver.Conn.on_game_packet(
            self.bob, OP_SESSION_MEMBER_UPDATE,
            self.seat_slot_payload(0, character=0, team=TEAM_B,
                                   nickname="Alice", level=3))
        self.assertEqual(TEAM_A, self.room.seats[0].team)
        self.assertEqual([], opcodes(self.alice))
        self.assertEqual([], opcodes(self.bob))

    def test_a_bogus_team_number_is_refused(self):
        gameserver.Conn.on_game_packet(
            self.bob, OP_SESSION_MEMBER_UPDATE,
            self.seat_slot_payload(1, character=1, team=7))
        self.assertEqual(TEAM_B, self.room.seats[1].team)
        self.assertEqual([], opcodes(self.alice))

    # -- 游戏准备 ----------------------------------------------------------
    def test_ready_is_broadcast_to_everyone(self):
        gameserver.Conn.on_game_packet(self.bob, OP_SEAT_READY, w_i32(1))
        self.assertTrue(self.room.seats[1].ready)
        for conn in (self.bob, self.alice):
            self.assertEqual([OP_SESSION_MEMBER_UPDATE], opcodes(conn))
            payload = [p for blob in conn.sent for _, _op, p in frames(blob)][0]
            self.assertEqual(SEAT_ACTION_RESYNC, payload[0])
            self.assertEqual(1, Reader(payload[1:]).i32())
            slot = parse_session_slot(Reader(payload[5:]))
            self.assertTrue(slot["ready"])

    def test_ready_can_be_taken_back(self):
        gameserver.Conn.on_game_packet(self.bob, OP_SEAT_READY, w_i32(1))
        gameserver.Conn.on_game_packet(self.bob, OP_SEAT_READY, w_i32(0))
        self.assertFalse(self.room.seats[1].ready)
        payload = [p for blob in self.alice.sent
                   for _, _op, p in frames(blob)][-1]
        slot = parse_session_slot(Reader(payload[5:]))
        self.assertFalse(slot["ready"])

    def test_ready_outside_a_room_is_ignored(self):
        loner = make_conn("carol")
        gameserver.Conn.on_game_packet(loner, OP_SEAT_READY, w_i32(1))
        self.assertEqual([], opcodes(loner))

    def test_ready_survives_a_later_full_snapshot(self):
        # 后进来的人靠 0x0300 拿到全量座位表，准备状态必须在里面。
        gameserver.Conn.on_game_packet(self.bob, OP_SEAT_READY, w_i32(1))
        carol = make_conn("carol")
        gameserver.Conn.on_game_packet(carol, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        snapshot = [p for blob in carol.sent for _, op, p in frames(blob)
                    if op == OP_SESSION_MEMBERS][0]
        reader = Reader(snapshot)
        reader.i32()                     # 房主座位号
        reader.i32()                     # ?
        slots = [parse_session_slot(reader) for _ in range(ROOM_SEAT_COUNT)]
        self.assertTrue(slots[1]["ready"])
        self.assertEqual(TEAM_A, slots[0]["team"])
        self.assertEqual(TEAM_B, slots[1]["team"])

    def test_ready_is_cleared_when_the_round_starts(self):
        # 客户端进 stage 6 时自己把六个座位的 +0x2e 全清 0（0x46fc0f）。
        gameserver.Conn.on_game_packet(self.bob, OP_SEAT_READY, w_i32(1))
        self.room.status = gameserver.SESSION_STATUS_PLAYING
        self.room.clear_ready()
        self.assertFalse(self.room.seats[1].ready)


class UserListTests(LobbyIsolated):
    """大厅右侧「玩家列表」= `0x020d` 请求 -> **`0x0212`** 应答（§166 / §169）。

    用户 2026-08-12 报的「大厅右侧玩家列表看不见其他人」：以前一直回同号的
    `0x020d`，而那个包的服务端方向是个弹窗（`0x553c5f`），列表永远是空的。

    同一天报的另外两条（§169 / D095）：
    ① 每行长得一模一样、分不清哪个是自己 —— 等级填进了第一个 int32，
       而那一格是 `P`/`W` 徽章；且自己也在列表里；
    ② 已经进游戏的人还留在「待机玩家」档里 —— 过滤开关被丢掉了。
    """

    def setUp(self):
        super().setUp()
        self._saved_conns = list(gameserver._conns)
        gameserver._conns.clear()
        self.alice = make_conn("alice")
        self.bob = make_conn("bob")
        for conn in (self.alice, self.bob):
            gameserver.register_conn(conn)
            conn.sent.clear()

    def tearDown(self):
        gameserver._conns.clear()
        gameserver._conns.extend(self._saved_conns)
        super().tearDown()

    def request(self, page=0, page_size=18, flag=1):
        return struct.pack("<HHB", page, page_size, flag)

    def reply_of(self, conn):
        blobs = [(op, p) for blob in conn.sent for _, op, p in frames(blob)]
        return [p for op, p in blobs if op == OP_REP_USER_LIST]

    def ask(self, conn, **kw):
        """发一次请求，把应答解成 `(头三个字段, [(昵称, 在打游戏, 等级, 天梯)])`。"""
        conn.sent.clear()
        gameserver.Conn.on_game_packet(conn, OP_REQ_USER_LIST,
                                       self.request(**kw))
        reader = Reader(self.reply_of(conn)[0])
        head = (reader.u16(), reader.u16(), reader.take(1)[0])
        rows = [(reader.wstr(), reader.i32(), reader.i32(), reader.i32())
                for _ in range(reader.i32())]
        self.assertEqual(0, reader.left())
        return head, rows

    def put_in_a_running_game(self, conn):
        """把这条连接塞进一间「游戏中」的房间。"""
        room = self.lobby.create_room(conn, title="打着呢", session_type=2)
        room.status = gameserver.SESSION_STATUS_PLAYING
        return room

    def test_the_request_is_five_bytes(self):
        parsed = parse_user_list_request(self.request(2, 18, 1))
        self.assertEqual({"page": 2, "page_size": 18, "flag": 1}, parsed)

    def test_the_other_players_are_listed(self):
        head, rows = self.ask(self.alice)
        self.assertEqual((0, 18, 1), head)          # 头三个字段原样回显
        self.assertEqual([("Bob", 0, 5, 20)], rows)

    def test_you_are_never_in_your_own_list(self):
        # ★ 客户端那张列表没有任何「这是你」的标记（渲染函数 0x441df5 从头到尾
        #   不比昵称），所以只能靠不列自己来消歧义（D095）。
        self.assertNotIn("Alice", [r[0] for r in self.ask(self.alice)[1]])
        self.assertNotIn("Bob", [r[0] for r in self.ask(self.bob)[1]])

    def test_the_level_goes_in_the_second_int32_not_the_first(self):
        # 第 1 个 int32 是 P/W 徽章，把等级填进去等于每行都是「游戏中」（§169）。
        _, rows = self.ask(self.alice)
        self.assertEqual(0, rows[0][1])             # 待机 -> W
        self.assertEqual(5, rows[0][2])             # 等级 -> LevelMark

    def test_a_player_in_a_running_game_is_flagged_and_hidden_from_waiting(self):
        self.put_in_a_running_game(self.bob)
        # 「待机玩家」档（客户端默认发 1）：看不见他
        self.assertEqual([], self.ask(self.alice, flag=1)[1])
        # 「推荐对手」档（flag 0）：看得见，而且徽章是「游戏中」
        self.assertEqual([("Bob", 1, 5, 20)], self.ask(self.alice, flag=0)[1])

    def test_sitting_in_a_waiting_room_still_counts_as_waiting(self):
        # 房间列表把它显示成「待机中」，玩家列表的口径要一致。
        self.lobby.create_room(self.bob, title="等人", session_type=2)
        self.assertEqual([("Bob", 0, 5, 20)], self.ask(self.alice, flag=1)[1])

    def test_recommended_puts_the_closest_level_first(self):
        carol = make_conn("carol")            # Lv.7；alice 是 Lv.3、bob 是 Lv.5
        gameserver.register_conn(carol)
        self.assertEqual(["Bob", "Carol"],
                         [r[0] for r in self.ask(self.alice, flag=0)[1]])
        # bob 站在 Lv.5：alice(3) 和 carol(7) 一样近，同距按昵称排
        self.assertEqual(["Alice", "Carol"],
                         [r[0] for r in self.ask(self.bob, flag=0)[1]])

    def test_the_reply_is_0x0212_not_0x020d(self):
        # ★ 回同号的 0x020d 只会喂给弹窗处理器，列表永远空着。
        gameserver.Conn.on_game_packet(self.alice, OP_REQ_USER_LIST,
                                       self.request())
        self.assertEqual([OP_REP_USER_LIST], opcodes(self.alice))

    def test_paging_is_echoed_back_and_honoured(self):
        carol = make_conn("carol")
        gameserver.register_conn(carol)
        head, rows = self.ask(self.alice, page=1, page_size=1)
        self.assertEqual((1, 1), head[:2])
        self.assertEqual(["Carol"], [r[0] for r in rows])   # 第 1 页第 1 条

    def test_a_page_past_the_end_is_empty_not_an_error(self):
        self.assertEqual([], self.ask(self.alice, page=9)[1])

    def test_a_connection_without_an_account_is_not_listed(self):
        ghost = make_conn("carol")
        ghost.account_name = ""
        gameserver.register_conn(ghost)
        self.assertEqual(["Bob"], [r[0] for r in self.ask(self.alice)[1]])

    def test_a_garbled_request_still_gets_a_reply(self):
        gameserver.Conn.on_game_packet(self.alice, OP_REQ_USER_LIST, b"\x00")
        self.assertEqual([OP_REP_USER_LIST], opcodes(self.alice))


class PeerRelayTests(LobbyIsolated):
    """战斗内联机：`0x0410` 开关 + `0x040e` -> `0x040f` 转发（§149~§151）。"""

    def setUp(self):
        super().setUp()
        self.alice = make_conn("alice")
        gameserver.Conn.on_game_packet(self.alice, 0x0201,
                                       create_session_payload())
        self.room = self.lobby.room_of(self.alice)
        self.alice.sent.clear()          # 建房那四发和本组用例无关
        self.bob = make_conn("bob")

    def peer_packet(self, sender_seat=1, sequence=7, body=b"\xaa\xbb\xcc",
                    game_id=None):
        """一个像模像样的 12 字节 `UdpPacket` 头 + body（§151）。

        局号默认取**房间当前那个号** —— 进房那一发 `0x0303` 就是这么设的
        （§218 / D138：包尾 u16 -> `[GameSession+0x3c]`），所以真客户端盖的
        就是这个数。全房间同代同号，转发时一个字节都不该动。
        """
        if game_id is None:
            game_id = self.room.epoch_value & 0xFFFF
        return (struct.pack("<BbbB", 0xFF, sender_seat, -1, 0)
                + struct.pack("<HHHH", game_id, 0x1234, sequence, 0x0102)
                + body)

    def join_bob(self):
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        self.alice.sent.clear()
        self.bob.sent.clear()

    def toggles(self, conn):
        """某条连接收到的每一发 0x0410 的 int32 载荷。"""
        return [Reader(p).i32() for blob in conn.sent
                for _, op, p in frames(blob) if op == OP_TOGGLE_PEER_RELAY]

    # -- 开关 ---------------------------------------------------------------

    def test_one_player_alone_never_gets_the_switch_turned_on(self):
        # 一个人的房间开了也没人收，只是让客户端每 128 毫秒白发一发。
        self.assertEqual([], self.toggles(self.alice))
        self.assertFalse(self.alice.peer_relay_on)

    def test_the_second_player_turns_it_on_for_everyone(self):
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        self.assertEqual([1], self.toggles(self.alice))
        self.assertEqual([1], self.toggles(self.bob))

    def test_dropping_back_to_one_player_turns_it_off(self):
        self.join_bob()
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.assertEqual([0], self.toggles(self.alice))
        self.assertFalse(self.alice.peer_relay_on)

    def test_rejoining_sends_the_switch_again(self):
        """★ 回归：客户端退房时**自己**把开关清 0（`0x406191`）。

        服务端不跟着清的话，第二次进房时会以为「已经开着」而不重发，
        表现就是「第一局能联机，退出去再进来就同步不上了」。
        """
        self.join_bob()
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.assertFalse(self.bob.peer_relay_on)
        self.bob.sent.clear()
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        self.assertEqual([1], self.toggles(self.bob))

    def test_the_switch_payload_is_a_four_byte_int32(self):
        # 客户端处理器 0x408703 读的是 0x5d59de = int32 -> bool（§136）。
        self.join_bob()
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        payload = [p for blob in self.alice.sent
                   for _, op, p in frames(blob) if op == OP_TOGGLE_PEER_RELAY][0]
        self.assertEqual(4, len(payload))

    # -- 转发 ---------------------------------------------------------------

    def test_peer_data_is_forwarded_verbatim_to_the_others(self):
        self.join_bob()
        blob = self.peer_packet()
        gameserver.Conn.on_game_packet(self.bob, OP_PEER_DATA_UP, blob)
        got = [(op, p) for b in self.alice.sent for _, op, p in frames(b)]
        self.assertEqual([(OP_PEER_DATA_DOWN, blob)], got)

    def test_peer_data_is_not_echoed_back_to_the_sender(self):
        # 客户端本地已经走过一遍回环（0x407802），回显只会白占带宽。
        self.join_bob()
        gameserver.Conn.on_game_packet(self.bob, OP_PEER_DATA_UP,
                                       self.peer_packet())
        self.assertEqual([], opcodes(self.bob))

    def test_peer_data_alone_in_a_room_goes_nowhere(self):
        gameserver.Conn.on_game_packet(self.alice, OP_PEER_DATA_UP,
                                       self.peer_packet(sender_seat=0))
        self.assertEqual([], opcodes(self.alice))

    def test_peer_data_outside_a_room_is_dropped(self):
        gameserver.Conn.on_game_packet(self.bob, OP_PEER_DATA_UP,
                                       self.peer_packet())
        self.assertEqual([], opcodes(self.bob))
        self.assertEqual([], opcodes(self.alice))

    def test_a_short_or_garbage_payload_still_forwards(self):
        # 转发是纯字节搬运，解析只用来打日志 —— 解不动也绝不能把包吞掉。
        self.join_bob()
        gameserver.Conn.on_game_packet(self.bob, OP_PEER_DATA_UP, b"\x00\x01")
        got = [(op, p) for b in self.alice.sent for _, op, p in frames(b)]
        self.assertEqual([(OP_PEER_DATA_DOWN, b"\x00\x01")], got)

    # -- 中继请求 -----------------------------------------------------------

    def test_start_tcp_relay_is_answered_with_join_relay(self):
        """`0x0310` -> 回 `0x0210 gspJoinRelay`（D078：原版那条路要接上）。

        ★ 这条用例**推翻了 D077 时代那条同名反向的用例**。当时的结论是
        「不实现中继，永远别回 `0x0210`」；用户在 J.2 拍板要原样还原（D078）。
        """
        self.join_bob()
        gameserver.Conn.on_game_packet(self.bob, OP_START_TCP_RELAY,
                                       w_i32(1) + w_i32(0))
        self.assertIn(OP_JOIN_RELAY, opcodes(self.bob))

    def test_join_relay_payload_is_the_documented_eighteen_bytes(self):
        """`NetAddress{int32 ip; u16 port}` + `RelayAuthData{3 x int32}`（§157）。

        ip 是**网络字节序的原始 32 位**（`Connect` 直接写进 `sin_addr`，
        没有 `htonl`），port 是主机序（`Connect` 里才过 `htons`）。
        """
        self.join_bob()
        gameserver.Conn.on_game_packet(self.bob, OP_START_TCP_RELAY,
                                       w_i32(1) + w_i32(0))
        payload = [p for blob in self.bob.sent
                   for _, op, p in frames(blob) if op == OP_JOIN_RELAY][0]
        self.assertEqual(18, len(payload))
        self.assertEqual(b"\x7f\x00\x00\x01", payload[:4])
        self.assertEqual(self.relay.port,
                         struct.unpack_from("<H", payload, 4)[0])
        auth = struct.unpack_from("<iii", payload, 6)
        self.assertEqual(self.room.room_id, auth[0])
        self.assertEqual(1, auth[1])                    # bob 坐 1 号位
        self.assertNotEqual(0, auth[2])                 # 认人的那个 nonce

    def test_join_relay_is_sent_at_most_once_per_connection(self):
        """★★ 回归（§159）：`0x0210` 回第二次是定时炸弹。

        客户端收到就无条件 `new RelayConnection` 并覆盖 `[0x72e290]`，
        旧对象既不释放也不关 socket；等它收到 FD_CLOSE，`OnDisconnected`
        会把**新**连接的指针清 0，再发 `0x0203` 把玩家踢出房间（§158）。
        而 `0x0310` 是每个别人坐着的座位每 10 秒一发，重复请求是常态。
        """
        self.join_bob()
        for _ in range(5):
            gameserver.Conn.on_game_packet(self.bob, OP_START_TCP_RELAY,
                                           w_i32(1) + w_i32(0))
        self.assertEqual(1, opcodes(self.bob).count(OP_JOIN_RELAY))
        # 票据还在兑现宽限期内（客户端可能正在连），也不能拆（0x0211）。
        self.assertNotIn(OP_LEAVE_RELAY, opcodes(self.bob))

    def test_a_dead_relay_connection_is_replaced_on_the_next_0310(self):
        """★ 回归 bug调查/4：中继 TCP 死了之后 `0x0310` 要能换来新通道。

        客户端那边中继半死（NAT 超时收不到 FIN）时会一边继续玩一边每
        10 秒发 `0x0310` 讨通道；服务端这边连接已经没了的话，必须先
        `0x0211` 让客户端干净拆旧对象（不走 `OnDisconnected`，不会被踢
        出房间），再发一张新票据 —— 「他自己能动、别人看他一动不动」
        的自愈路径。铁律 2 防的「两张活票据并存」在这里不成立：
        重发前 `leave_relay()` 已把旧票据作废。
        """
        self.join_bob()
        gameserver.Conn.on_game_packet(self.bob, OP_START_TCP_RELAY,
                                       w_i32(1) + w_i32(0))
        self.assertEqual(1, opcodes(self.bob).count(OP_JOIN_RELAY))
        # 模拟：票据已兑过（客户端连过中继），后来那条连接没了。
        nonce = self.relay._issued[self.bob]
        self.assertIsNotNone(self.relay.redeem((0, 0, nonce)))
        self.assertIsNone(self.relay.conn_for(self.bob))
        self.bob.sent.clear()
        gameserver.Conn.on_game_packet(self.bob, OP_START_TCP_RELAY,
                                       w_i32(1) + w_i32(0))
        got = opcodes(self.bob)
        self.assertIn(OP_LEAVE_RELAY, got)          # 先干净拆旧
        self.assertEqual(1, got.count(OP_JOIN_RELAY))   # 再发一张新票据
        # 换出去的必须是能兑的新票，而且兑完 dedup 表里还是同一条游戏连接。
        auth = [p for b in self.bob.sent for _, op, p in frames(b)
                if op == OP_JOIN_RELAY][0]
        new_nonce = struct.unpack_from("<i", auth, 14)[0]
        self.assertIsNotNone(self.relay.redeem((0, 0, new_nonce)))

    def test_a_stalled_relay_connection_is_replaced_too(self):
        """注册着但长时间没有任何入站（数据/pong 都没有）= 半死，同样换新。

        这是「客户端收不到 FIN 还以为一切正常」的形态 —— 战斗数据
        ~8 Hz、ping 1 Hz，20 秒什么入站都没有只可能是对端方向断了。
        """
        self.join_bob()
        gameserver.Conn.on_game_packet(self.bob, OP_START_TCP_RELAY,
                                       w_i32(1) + w_i32(0))
        fake = relayserver.RelayConn.__new__(relayserver.RelayConn)
        fake.closed = False
        fake.send_broken = False
        fake.game_conn = self.bob
        fake.last_inbound_at = time.time() - relayserver.STALL_AFTER_S - 5
        self.relay.bind(fake)
        self.bob.sent.clear()
        gameserver.Conn.on_game_packet(self.bob, OP_START_TCP_RELAY,
                                       w_i32(1) + w_i32(0))
        got = opcodes(self.bob)
        self.assertIn(OP_LEAVE_RELAY, got)
        self.assertEqual(1, got.count(OP_JOIN_RELAY))

    def test_the_switch_still_goes_out_when_the_relay_is_used(self):
        """走中继也照样要先发 `0x0410` —— `0x408619` 的第一句就是那个开关。"""
        self.join_bob()
        self.bob.peer_relay_on = False       # 装作客户端刚退过房自己清了开关
        gameserver.Conn.on_game_packet(self.bob, OP_START_TCP_RELAY,
                                       w_i32(1) + w_i32(0))
        self.assertEqual([1], self.toggles(self.bob))

    def test_no_tcp_relay_falls_back_to_the_original_0x040e_path(self):
        """D078 的反悔开关：关掉中继，客户端自然走 `0x040e` 回退路径。"""
        gameserver.TCP_RELAY_ENABLED = False
        self.join_bob()
        gameserver.Conn.on_game_packet(self.bob, OP_START_TCP_RELAY,
                                       w_i32(1) + w_i32(0))
        self.assertNotIn(OP_JOIN_RELAY, opcodes(self.bob))
        # 回退路径必须还在
        blob = self.peer_packet()
        gameserver.Conn.on_game_packet(self.bob, OP_PEER_DATA_UP, blob)
        got = [(op, p) for b in self.alice.sent for _, op, p in frames(b)]
        self.assertEqual([(OP_PEER_DATA_DOWN, blob)], got)

    def test_a_player_outside_a_room_gets_no_relay(self):
        # 房间是投递的依据，没房间就没有「对方」，票据白签。
        gameserver.Conn.on_game_packet(self.bob, OP_START_TCP_RELAY,
                                       w_i32(1) + w_i32(0))
        self.assertNotIn(OP_JOIN_RELAY, opcodes(self.bob))
        self.assertFalse(self.relay.has_issued(self.bob))

    def test_leave_relay_sends_0x0211_and_releases_the_ticket(self):
        """`0x0211` 是唯一安全的拆法（走析构，不触发 `OnDisconnected`，§158）。"""
        self.join_bob()
        gameserver.Conn.on_game_packet(self.bob, OP_START_TCP_RELAY,
                                       w_i32(1) + w_i32(0))
        self.bob.sent.clear()
        self.assertTrue(gameserver.Conn.leave_relay(self.bob))
        self.assertEqual([OP_LEAVE_RELAY], opcodes(self.bob))
        self.assertFalse(self.relay.has_issued(self.bob))
        # 拆过之后才允许再签一张。
        self.bob.sent.clear()
        gameserver.Conn.on_game_packet(self.bob, OP_START_TCP_RELAY,
                                       w_i32(1) + w_i32(0))
        self.assertIn(OP_JOIN_RELAY, opcodes(self.bob))

    def test_leave_relay_is_a_no_op_when_nothing_was_issued(self):
        self.join_bob()
        self.assertFalse(gameserver.Conn.leave_relay(self.bob))
        self.assertEqual([], opcodes(self.bob))


class StartGameRoomTests(LobbyIsolated):
    """J.3 第一块：开局链多人化 —— 房主开局、**所有人加载完才放行**。"""

    def setUp(self):
        super().setUp()
        self.alice = make_conn("alice")                     # 房主，座位 0
        gameserver.Conn.on_game_packet(self.alice, 0x0201,
                                       create_session_payload())
        self.room = self.lobby.room_of(self.alice)
        self.bob = make_conn("bob")
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        self.alice.sent.clear()
        self.bob.sent.clear()

    def ready(self, conn):
        gameserver.Conn.on_game_packet(conn, OP_COUNT_GAME_READY, b"")

    def loaded(self, conn):
        gameserver.Conn.on_game_packet(conn, OP_LOADING_DONE, b"")

    def test_the_host_start_is_broadcast_to_everyone(self):
        # 0x0400 只发给房主的话，别人连关卡都不会去加载。
        self.ready(self.alice)
        self.assertEqual([OP_TRIGGER_COUNT_GAME], opcodes(self.alice))
        self.assertEqual([OP_TRIGGER_COUNT_GAME], opcodes(self.bob))
        self.alice.sent.clear(); self.bob.sent.clear()
        self.ready(self.alice)
        self.assertEqual([OP_PREPARE_GAME], opcodes(self.alice))
        self.assertEqual([OP_PREPARE_GAME], opcodes(self.bob))

    def test_everyone_gets_the_same_seed(self):
        # seed 不一样 = 各人生成的关卡不一样。
        self.ready(self.alice); self.alice.sent.clear(); self.bob.sent.clear()
        self.ready(self.alice)
        def prepare(conn):
            return [p for b in conn.sent for _, op, p in frames(b)
                    if op == OP_PREPARE_GAME][0]
        self.assertEqual(prepare(self.alice), prepare(self.bob))

    def test_a_non_host_cannot_start_the_game(self):
        self.ready(self.bob)
        self.assertEqual([], opcodes(self.alice))
        self.assertEqual([], opcodes(self.bob))

    def test_nobody_is_released_until_everyone_has_loaded(self):
        self.ready(self.alice); self.ready(self.alice)
        self.alice.sent.clear(); self.bob.sent.clear()
        self.loaded(self.alice)                 # 只有房主加载完
        self.assertEqual([], opcodes(self.alice))
        self.assertEqual([], opcodes(self.bob))
        self.loaded(self.bob)                   # 收齐了
        # ★ 进 stage 7 后每人还会补一发 0x0410（客户端退房 / 中继断开都会
        #   自己把通道 A 清回 0，服务端缓存的「已开」不可信，见 bug调查/4）。
        self.assertEqual([OP_COUNT_GAME_READY, OP_TOGGLE_PEER_RELAY],
                         opcodes(self.alice))
        self.assertEqual([OP_COUNT_GAME_READY, OP_TOGGLE_PEER_RELAY],
                         opcodes(self.bob))

    def test_the_battle_start_reasserts_the_peer_relay_switch(self):
        """★ 回归 bug调查/4：第二局开始有人「自己能动、别人看他不动」。

        客户端在两局之间可能把通道 A 自己清回 0（退房 / 中继断开都会清），
        服务端若信缓存的「已经开着」就永远不重发 `0x0410` —— 那个人的
        战斗同步从此一个包都不发。开局链必须在进 stage 7 后**无条件**重发。
        """
        self.alice.peer_relay_on = True         # 模拟「上一局已经开过」
        self.bob.peer_relay_on = True
        self.ready(self.alice); self.ready(self.alice)
        self.alice.sent.clear(); self.bob.sent.clear()
        self.loaded(self.alice); self.loaded(self.bob)
        self.assertIn(OP_TOGGLE_PEER_RELAY, opcodes(self.alice))
        self.assertIn(OP_TOGGLE_PEER_RELAY, opcodes(self.bob))

    def test_a_lone_player_still_starts_exactly_like_before(self):
        # 单人房的包序列必须和 V0.1 一模一样，别为了多人把单机弄坏了。
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.alice.sent.clear()
        self.ready(self.alice); self.ready(self.alice); self.loaded(self.alice)
        self.assertEqual([OP_TRIGGER_COUNT_GAME, OP_PREPARE_GAME,
                          OP_COUNT_GAME_READY], opcodes(self.alice))

    def test_someone_leaving_mid_load_releases_the_rest(self):
        """★ 回归：等的人走了，剩下的不能永远卡在加载界面。"""
        self.ready(self.alice); self.ready(self.alice)
        self.loaded(self.alice)
        self.alice.sent.clear(); self.bob.sent.clear()
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.assertIn(OP_COUNT_GAME_READY, opcodes(self.alice))

    def test_a_new_player_resets_a_stale_handshake(self):
        # 不清的话新人不在 loaded 里，房主再开局会等一个没收到过 0x0400 的人。
        self.ready(self.alice)
        carol = make_conn("carol")
        gameserver.Conn.on_game_packet(carol, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        self.assertIsNone(self.room.battle)

    def test_a_joiner_during_loading_is_blocked(self):
        """★ 回归 bug调查/1：加载途中进房会把开局握手作废成死锁。

        0x0400 发出去（PREPARING，全员困在加载界面没法重新按「开始」）
        之后放进新人，`finish_join` 把 `room.battle` 作废，剩下的人
        永远等不齐 0x0403 —— 8-14 晚 dk 在 6 人加载途中进房，全员
        「还在等 6 人」直到散伙。
        """
        self.ready(self.alice); self.ready(self.alice)    # 0x0401 + 0x0400
        self.assertEqual(self.room.battle.state,
                         StartGameHandshake.PREPARING)
        self.assertEqual(self.room.status, SESSION_STATUS_PLAYING)
        carol = make_conn("carol")
        gameserver.Conn.on_game_packet(carol, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        self.assertEqual([p for b in carol.sent for _, op, p in frames(b)
                          if op == OP_MOVE_INTO_SESSION],
                         [build_rep_move_into_session(MOVE_INTO_ALREADY_PLAYING)])
        self.assertIsNone(self.lobby.room_of(carol))      # 确实没进来
        self.assertIsNotNone(self.room.battle)            # 这一轮的握手没被作废
        # 房里两人的加载流程照常走完：收齐 0x0403 就放行。
        self.alice.sent.clear(); self.bob.sent.clear()
        self.loaded(self.alice); self.loaded(self.bob)
        self.assertIn(OP_COUNT_GAME_READY, opcodes(self.alice))

    def test_leaving_mid_load_is_still_noted_for_handover(self):
        """★ 加载期房间已提前标「游戏中」，但退房的人仍要记进补交接名单。

        给 `room_in_battle()` 的回归：挡人用的 `room.status` 和「真开打了」
        是两回事，控制权交接若误判成战斗中，`left_while_loading` 就没人记，
        那一局的怪从开局起就没人模拟（§180 / D103）。
        """
        self.ready(self.alice); self.ready(self.alice)
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.assertEqual(self.room.battle.left_while_loading, [1])
        self.loaded(self.alice)                           # 剩下的人照常放行
        self.assertIn(OP_COUNT_GAME_READY, opcodes(self.alice))
        self.assertEqual(self.room.battle.left_while_loading, [])


# ----------------------------------------------------------------------------
# 换代状态机（局号 = 每座位收包队列的纪元号）
# ----------------------------------------------------------------------------
class EpochGenerationTests(LobbyIsolated):
    """换代状态机的房间侧接线（§218 / D137 / D138）。

    客户端的局号 `[GameSession+0x3c]` 不是它自己的私有计数器 —— 每一次变化
    都是服务端发的某一发包造成的：

    * `0x0303 gspSession` 的**包尾 u16** -> 直接设成那个值（`0x556ed1`）；
    * `0x0400` / `0x0403` -> 各 +1（`0x5517a3` / `0x551900`，同时清六条队列）；
    * 登录成功 / `0x0203` -> 复位成 -1（`0x4054fa`）。

    所以服务端能（也必须）自己把这张表维护起来：进房那一发 `0x0303` 就把
    中途进来的人和全房间对齐，转发时再按「代」判定能不能投递。
    """

    def setUp(self):
        super().setUp()
        self.alice = make_conn("alice")                     # 房主，座位 0
        gameserver.Conn.on_game_packet(self.alice, 0x0201,
                                       create_session_payload())
        self.room = self.lobby.room_of(self.alice)
        self.bob = make_conn("bob")
        gameserver.Conn.on_game_packet(self.bob, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        self.alice.sent.clear()
        self.bob.sent.clear()

    # -- 工具 -----------------------------------------------------------------
    def epoch(self, conn):
        return relayserver.epoch_state(conn)

    def join(self, name):
        conn = make_conn(name)
        gameserver.Conn.on_game_packet(conn, OP_MOVE_INTO_SESSION,
                                       move_into_payload(self.room.room_id))
        return conn

    def session_game_id(self, conn):
        """这条连接收到的最后一发 `0x0303` 里带的局号（包尾 u16）。"""
        payloads = [p for blob in conn.sent for _, op, p in frames(blob)
                    if op == gameserver.OP_UPDATE_SESSION]
        self.assertTrue(payloads, "没收到 0x0303")
        return struct.unpack("<H", payloads[-1][-2:])[0]

    def play_a_round(self, members):
        """房主开局 -> 全员加载完 -> 每人各自看完结算回房间。"""
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        for conn in members:
            gameserver.Conn.on_game_packet(conn, OP_LOADING_DONE, b"")
        for conn in members:
            gameserver.Conn.leave_game_result(conn)
        for conn in members:
            conn.sent.clear()

    # -- 进房：0x0303 直接把局号设对 ------------------------------------------
    def test_the_session_packet_carries_the_room_epoch(self):
        """★ 这一发就是原版的对齐手段：包尾 u16 -> `[GameSession+0x3c]`。"""
        carol = self.join("carol")
        self.assertEqual(self.room.epoch_value, self.session_game_id(carol))
        self.assertEqual(self.room.epoch_value, self.epoch(carol).value)

    def test_everyone_in_the_room_shares_one_generation_and_one_number(self):
        self.assertIsNotNone(self.epoch(self.alice).gen)
        self.assertEqual(self.epoch(self.alice).gen, self.epoch(self.bob).gen)
        self.assertEqual(self.room.epoch_gen, self.epoch(self.bob).gen)
        self.assertEqual(0, self.epoch(self.alice).value)
        self.assertEqual(0, self.epoch(self.bob).value)

    # -- 换代：0x0400 / 0x0403 ------------------------------------------------
    def test_prepare_game_moves_everyone_into_one_new_generation(self):
        before = self.room.epoch_gen
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        self.assertNotEqual(before, self.room.epoch_gen)
        self.assertEqual("battle", self.room.epoch_kind)
        self.assertEqual(1, self.room.epoch_value)
        for conn in (self.alice, self.bob):
            self.assertEqual(self.room.epoch_gen, self.epoch(conn).gen)
            self.assertEqual(1, self.epoch(conn).value)

    def test_going_back_to_the_room_moves_everyone_on_again(self):
        """★ `0x0403` 是**各人看完结算各自触发**的，前后可能差十几秒 ——
        但必须落在**同一个代号**里，否则同一局的人会被判成互相跨代。"""
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        battle_gen = self.room.epoch_gen
        gameserver.Conn.leave_game_result(self.alice)       # 快的那个先回房间
        room_gen = self.room.epoch_gen
        self.assertNotEqual(battle_gen, room_gen)
        self.assertEqual("room", self.room.epoch_kind)
        self.assertEqual(room_gen, self.epoch(self.alice).gen)
        # 慢的那个还在结算界面 —— 还在上一代，这段时间双向都不该投递
        self.assertEqual(battle_gen, self.epoch(self.bob).gen)
        gameserver.Conn.leave_game_result(self.bob)
        self.assertEqual(room_gen, self.epoch(self.bob).gen)
        self.assertEqual(2, self.epoch(self.alice).value)
        self.assertEqual(2, self.epoch(self.bob).value)
        self.assertEqual(2, self.room.epoch_value)

    def test_the_numbers_match_the_field_capture(self):
        """★ 和 2026-08-19 现场（bug调查/9）逐格对住：
        进房 0 -> 第一局 1 -> 回房 2 -> 第二局 3。"""
        self.assertEqual(0, self.epoch(self.bob).value)
        self.play_a_round([self.alice, self.bob])
        self.assertEqual(2, self.epoch(self.bob).value)
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        self.assertEqual(3, self.epoch(self.bob).value)

    # -- 复位 -----------------------------------------------------------------
    def test_leaving_the_room_resets_the_model(self):
        """`0x0203 result=0` -> 客户端 `GameSession::Reset`（`0x4054fa`）。"""
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        self.assertEqual(1, self.epoch(self.bob).value)
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.assertEqual(relayserver.EPOCH_UNSET, self.epoch(self.bob).value)
        self.assertIsNone(self.epoch(self.bob).gen)

    def test_a_failed_reply_does_not_reset(self):
        """结果码非 0 时客户端只弹个错误框，什么都不复位。"""
        state = self.epoch(self.alice)
        state.value = 4
        gameserver.Conn.note_epoch_from_frame(
            self.alice, build_game(OP_LEAVE_SESSION, w_i32(1)))
        self.assertEqual(4, state.value)
        gameserver.Conn.note_epoch_from_frame(
            self.alice, build_game(OP_LEAVE_SESSION, w_i32(0)))
        self.assertEqual(relayserver.EPOCH_UNSET, state.value)

    # -- 中途进房（D138 的正主）----------------------------------------------
    def test_a_late_joiner_is_aligned_by_the_session_packet_alone(self):
        """★ 打完一局之后进来的人：一发 `0x0303` 就和全房间对上，
        **不需要任何补发**（这正是 D131 那套「转发时改写局号」要解决的场景）。
        """
        self.play_a_round([self.alice, self.bob])
        self.assertEqual(2, self.room.epoch_value)
        carol = self.join("carol")
        self.assertEqual(2, self.session_game_id(carol))
        self.assertEqual(2, self.epoch(carol).value)
        self.assertEqual(self.room.epoch_gen, self.epoch(carol).gen)
        # 进房的包序里**没有**任何 0x0400 / 0x0403（不靠补发换代包对齐）
        self.assertNotIn(OP_PREPARE_GAME, opcodes(carol))
        self.assertNotIn(OP_LOADING_DONE, opcodes(carol))

        # 下一局开局：三个人一起 +1，仍然同代同号 -> 转发一发都不用改写
        carol.sent.clear()
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        self.assertEqual([OP_TRIGGER_COUNT_GAME, OP_PREPARE_GAME],
                         opcodes(carol))
        gens = {self.epoch(c).gen for c in (self.alice, self.bob, carol)}
        values = {self.epoch(c).value for c in (self.alice, self.bob, carol)}
        self.assertEqual(1, len(gens))
        self.assertEqual({3}, values)

    def test_a_room_parameter_change_re_asserts_the_epoch(self):
        """改地图 / 改模式那一发 `0x0303` 也带着局号 —— 幂等地再对一次。"""
        self.play_a_round([self.alice, self.bob])
        self.bob.sent.clear()
        gameserver.Conn.on_game_packet(self.alice, 0x0302,
                                       change_session_payload())
        self.assertEqual(self.room.epoch_value, self.session_game_id(self.bob))

    # -- 异常：中途掉线 -------------------------------------------------------
    def test_a_member_leaving_during_loading_does_not_disturb_the_others(self):
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        gameserver.Conn.on_game_packet(self.alice, OP_COUNT_GAME_READY, b"")
        battle_gen = self.room.epoch_gen
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.assertEqual(battle_gen, self.epoch(self.alice).gen)
        self.assertEqual(1, self.epoch(self.alice).value)
        self.assertEqual(battle_gen, self.room.epoch_gen)

    def test_a_reconnect_is_aligned_again_by_the_session_packet(self):
        """掉线重连 = 新连接：登录成功归 -1，进房那一发 `0x0303` 再对齐。"""
        self.play_a_round([self.alice, self.bob])
        again = self.join("bob")
        self.assertEqual(self.room.epoch_value, self.epoch(again).value)
        self.assertEqual(self.room.epoch_gen, self.epoch(again).gen)



class PeerHeaderTests(unittest.TestCase):
    """`describe_peer_header()` 只给日志用，但解错了会误导排查（§151）。"""

    def test_the_twelve_byte_header_is_decoded(self):
        blob = (struct.pack("<BbbB", 0xFF, 2, -1, 0)
                + struct.pack("<HHHH", 9, 0xBEEF, 1234, 0x0102) + b"xyz")
        line = gameserver.describe_peer_header(blob)
        self.assertIn("发送方座位=2", line)
        self.assertIn("目标座位=广播", line)
        self.assertIn("序列号=1234", line)
        self.assertIn("内层opcode=0x0102", line)
        self.assertIn("body=3 字节", line)

    def test_a_unicast_target_is_shown_as_a_seat_number(self):
        blob = (struct.pack("<BbbB", 0xFF, 0, 4, 0)
                + struct.pack("<HHHH", 0, 0, 0, 0))
        self.assertIn("目标座位=4", gameserver.describe_peer_header(blob))

    def test_a_too_short_payload_says_so_instead_of_crashing(self):
        self.assertIn("装不下", gameserver.describe_peer_header(b"\xff\x01"))


    def test_the_inner_opcode_is_named(self):
        """★ 排查同步问题时全靠这个名字看出「丢的是哪一发」（§216）。"""
        blob = (struct.pack("<BbbB", 0xFF, 2, -1, 0)
                + struct.pack("<HHHH", 1, 0, 0, 0x0001))
        self.assertIn("rpChangeWeapon", gameserver.describe_peer_header(blob))
        blob = (struct.pack("<BbbB", 0xFF, 2, -1, 0)
                + struct.pack("<HHHH", 1, 0, 7, 0x0002))
        self.assertIn("rpFire", gameserver.describe_peer_header(blob))

    def test_the_heartbeat_next_sequence_is_decoded(self):
        """心跳里那个 N 是收包队列基线的唯一来源，必须解出来（§216）。"""
        blob = (struct.pack("<BbbB", 0xFF, 2, -1, 0)
                + struct.pack("<HHHH", 1, 0, 0, 0x4001)
                + struct.pack("<H", 31) + bytes(29))
        line = gameserver.describe_peer_header(blob)
        self.assertIn("内层opcode=0x4001", line)
        self.assertIn("N=31", line)

    def test_an_unknown_inner_opcode_is_shown_as_a_question_mark(self):
        blob = (struct.pack("<BbbB", 0xFF, 0, -1, 0)
                + struct.pack("<HHHH", 0, 0, 0, 0x0777))
        self.assertIn("(?)", gameserver.describe_peer_header(blob))

if __name__ == "__main__":
    unittest.main()
