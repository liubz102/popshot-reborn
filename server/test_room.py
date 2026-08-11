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
    OP_COUNT_GAME_READY, OP_LOADING_DONE,
    OP_JOIN_RELAY, OP_LEAVE_RELAY, OP_START_TCP_RELAY,
    OP_MOVE_INTO_SESSION, OP_PEER_DATA_DOWN, OP_PEER_DATA_UP,
    OP_PREPARE_GAME, OP_SESSION_MEMBERS, OP_SESSION_MEMBER_UPDATE,
    OP_TRIGGER_COUNT_GAME,
    OP_SLOT_EQUIPPED_LIST, OP_TOGGLE_PEER_RELAY, OP_UPDATE_SESSION,
    ROOM_SEAT_COUNT,
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
    conn.peer_relay_on = False
    conn.peer_data_dumped = False
    conn.peer_data_in = 0
    conn.peer_data_out = 0
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
            fallback=gameserver._relay_fallback)
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

    def peer_packet(self, sender_seat=1, sequence=7, body=b"\xaa\xbb\xcc"):
        """一个像模像样的 12 字节 `UdpPacket` 头 + body（§151）。"""
        return (struct.pack("<BbbB", 0xFF, sender_seat, -1, 0)
                + struct.pack("<HHHH", 3, 0x1234, sequence, 0x0102)
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
        self.assertEqual([OP_COUNT_GAME_READY], opcodes(self.alice))
        self.assertEqual([OP_COUNT_GAME_READY], opcodes(self.bob))

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


if __name__ == "__main__":
    unittest.main()
