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
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gameserver                                              # noqa: E402
from gameserver import (                                       # noqa: E402
    CHAT_NO_SEAT, OP_CHAT, OP_LEAVE_SESSION, OP_LIST_SESSION,
    OP_MOVE_INTO_SESSION, OP_SESSION_MEMBERS, OP_SESSION_MEMBER_UPDATE,
    OP_SLOT_EQUIPPED_LIST, OP_UPDATE_SESSION, ROOM_SEAT_COUNT,
    SEAT_ACTION_CHANGE_CHARACTER, SEAT_ACTION_JOIN, SEAT_ACTION_LEAVE,
    SESSION_STATUS_WAITING, StartGameHandshake,
    Reader, build_game, build_rep_list_session, build_receive_chat,
    build_rep_move_into_session, build_session, build_update_session,
    lobby_game_type, parse_chat_message, parse_kick_out_request,
    parse_list_session_request, parse_move_into_request,
    parse_quick_join_request, read_session_descriptor, take_frame, w_i32,
    w_wstr,
)
from lobby import (Lobby, MOVE_INTO_BAD_PASSWORD, MOVE_INTO_FULL,   # noqa: E402
                   MOVE_INTO_NO_SUCH_ROOM, MOVE_INTO_OK)
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
    conn.vlog = lambda _msg: None
    conn.send = conn.sent.append
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
    conn.maps_entered = []
    conn.map_change_pending = False
    conn.items_created = 0
    conn.items_picked = 0
    conn.next_item_handle = gameserver.ITEM_HANDLE_BASE
    conn.start_game = StartGameHandshake()
    return conn


def create_session_payload(title="来玩", session_type=2, arguments=(3, 1)):
    """客户端方向的 `0x0201` 载荷：三个字符串 + int32 + 描述符。"""
    return (w_wstr(title) + w_wstr("") + w_wstr("")
            + w_i32(0)
            + w_i32(session_type)
            + b"".join(w_i32(v) for v in arguments))


def list_request(game_type=2, start_room=0):
    """客户端方向的 `0x0200` 载荷（12 字节，§139）。"""
    return (struct.pack("<HHH", start_room, 0, 0)
            + struct.pack("<BB", 0, 0)
            + w_i32(game_type))


def move_into_payload(room_id, password="", flag=0):
    return w_i32(room_id) + w_wstr(password) + w_i32(flag)


class LobbyIsolated(unittest.TestCase):
    """每个用例一张干净的大厅表 —— `LOBBY` 是模块级单例，用例之间会互相污染。"""

    def setUp(self):
        self._saved_lobby = gameserver.LOBBY
        gameserver.LOBBY = Lobby()
        self.lobby = gameserver.LOBBY

    def tearDown(self):
        gameserver.LOBBY = self._saved_lobby


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
        self.assertEqual([OP_UPDATE_SESSION, OP_MOVE_INTO_SESSION,
                          OP_SESSION_MEMBERS, OP_SLOT_EQUIPPED_LIST],
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
        self.assertEqual([OP_SESSION_MEMBER_UPDATE, OP_CHAT], got)
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
                          OP_SESSION_MEMBERS, OP_SLOT_EQUIPPED_LIST],
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
        self.assertEqual(1, len(bob.sock.writes))
        plain = SimpleCipher.server_to_client().decrypt(bob.sock.writes[0])
        self.assertEqual([OP_UPDATE_SESSION, OP_MOVE_INTO_SESSION,
                          OP_SESSION_MEMBERS, OP_SLOT_EQUIPPED_LIST],
                         [op for _, op, _ in frames(plain)])


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

    def test_leaving_broadcasts_seat_action_three(self):
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        self.assertEqual([OP_LEAVE_SESSION], opcodes(self.bob))
        payload = [p for blob in self.alice.sent for _, op, p in frames(blob)
                   if op == OP_SESSION_MEMBER_UPDATE][0]
        self.assertEqual(SEAT_ACTION_LEAVE, payload[0])
        self.assertEqual(1, Reader(payload[1:]).i32())
        self.assertIsNone(self.lobby.room_of(self.bob))

    def test_host_leaving_transfers_and_resends_the_seat_snapshot(self):
        # 「谁是房主」只有 0x0300 的第一个 int32 说了算，所以转移必须重发快照。
        gameserver.Conn.on_game_packet(self.alice, OP_LEAVE_SESSION, b"")
        got = opcodes(self.bob)
        self.assertEqual([OP_SESSION_MEMBER_UPDATE, OP_SESSION_MEMBERS, OP_CHAT],
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


if __name__ == "__main__":
    unittest.main()
