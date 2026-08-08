#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import struct
import threading
import unittest

from gameserver import (
    ROOM_SEAT_COUNT,
    SEAT_ACTION_CHANGE_CHARACTER,
    SEAT_ACTION_JOIN,
    SESSION_STATUS_WAITING,
    OP_SESSION_MEMBER_UPDATE,
    parse_seat_change_request,
    CHANNEL_CODE_GAME_TYPES,
    DESCRIPTOR_READ_ARGUMENT_COUNTS,
    DESCRIPTOR_SENT_ARGUMENT_COUNTS,
    GAME_TYPE_CHANNEL_CODES,
    LOBBY_TAB_GAME_TYPES,
    OP_CHANGE_SESSION,
    OP_COUNT_GAME_READY,
    OP_LADDER_START_GAME,
    OP_END_GAME,
    OP_LOADING_DONE,
    OP_MOVE_CHANNEL_BY_GAME_TYPE,
    OP_QUEST_REACHED_DIFFICULTY,
    OP_RESPAWN_CHARACTER,
    OP_PREPARE_GAME,
    OP_SESSION_MEMBERS,
    OP_REP_COUNT_DOWN,
    OP_REP_MOVE_INTO,
    OP_TRIGGER_COUNT_GAME,
    OP_UPDATE_SESSION,
    StartGameHandshake,
    build_game,
    build_prepare_game,
    build_rep_count_down,
    build_rep_create_session,
    build_rep_move_into,
    build_quest_reached_difficulty,
    build_end_game,
    build_end_game_values,
    build_rep_quest_record_in_pvp,
    build_broadcast_death,
    build_respawn_character,
    parse_report_hp_zero,
    parse_respawn_request,
    build_session_descriptor,
    build_session_member_update,
    build_session_members,
    build_session_slot,
    build_trigger_count_game,
    build_update_session,
    parse_change_session_request,
    parse_create_session_request,
    parse_move_channel_by_game_type,
    parse_create_item,
    build_created_item,
    parse_get_item,
    build_picked_item,
    build_rep_money,
    build_rep_money_for,
    build_rep_game_result,
    build_game_result_tail,
    build_game_result_values,
    parse_first_user_result,
    CREATE_ITEM_SIZE,
    GET_ITEM_SIZE,
    END_GAME_SCORE_PARTS,
    END_GAME_VALUE_COUNT,
    GAME_RESULT_CLEARED,
    GAME_RESULT_EXPERIENCE,
    GAME_RESULT_LADDER_POINT,
    GAME_RESULT_MONEY,
    GAME_RESULT_TAIL_COUNT,
    GAME_RESULT_VALUE_COUNT,
    ITEM_HANDLE_BASE,
    OP_GET_ITEM,
    OP_PICKED_ITEM,
    OP_LEAVE_RESULT,
    OP_LEAVE_SESSION,
    build_rep_leave_session,
    OP_REP_GAME_RESULT,
    OP_REP_MONEY,
    OP_REQ_FIRST_USER_RESULT,
    OP_REQ_CHANGE_TO_NEXT_MAP,
    OP_REP_CHANGE_TO_NEXT_MAP,
    OP_MAP_LOADING_DONE,
    OP_MAP_CHANGE_READY,
    build_rep_change_to_next_map,
    parse_req_change_to_next_map,
    EQUIPPED_SLOT_MASK_COUNT,
    OP_SLOT_EQUIPPED_LIST,
    build_rep_list_session,
    build_slot_equipped_list,
    take_frame,
    w_i32,
    w_wstr,
)
from simple import SimpleCipher
from account_store import (BASE_CHARACTER_IDS, EXPERIENCE_PER_LEVEL,
                           PREMIUM_CHARACTER_IDS, QUEST_DIFFICULTY_MAX,
                           QUEST_ID_TABLE, character_item_id,
                           character_item_ids, character_unlock_all,
                           experience_bounds, level_for_experience,
                           owned_characters, quest_cleared_difficulty,
                           quest_difficulty_records)
import gameserver


class GameServerPacketTests(unittest.TestCase):
    def test_create_session_success_payload(self):
        payload = build_rep_create_session(7)
        self.assertEqual((0, 7), struct.unpack("<ii", payload))

    def test_create_session_frame_round_trip(self):
        frame = build_game(0x0201, build_rep_create_session(1))
        self.assertEqual(
            ("game", 0x0201, struct.pack("<ii", 0, 1), len(frame)),
            take_frame(frame),
        )

    def test_parse_create_session_modes(self):
        cases = (
            (1, (11, 12, 13), "normal"),
            (2, (21, 22), "quest"),
            (5, (51, 52, 53), "ladder"),
        )
        for session_type, arguments, name in cases:
            with self.subTest(session_type=session_type):
                payload = (
                    w_wstr("title")
                    + w_wstr("map")
                    + w_wstr("password")
                    + w_i32(4)
                    + w_i32(session_type)
                    + b"".join(w_i32(value) for value in arguments)
                )
                self.assertEqual(
                    {
                        "texts": ("title", "map", "password"),
                        "option": 4,
                        "session_type": session_type,
                        "session_type_name": name,
                        "arguments": arguments,
                    },
                    parse_create_session_request(payload),
                )

    def test_parse_create_session_rejects_truncated_or_trailing_payload(self):
        valid = w_wstr("") * 3 + w_i32(1) + w_i32(2) + w_i32(3) + w_i32(4)
        with self.assertRaises(ValueError):
            parse_create_session_request(valid[:-1])
        with self.assertRaises(ValueError):
            parse_create_session_request(valid + b"extra")

    def test_ladder_start_opcode_is_distinct_from_trigger_response(self):
        self.assertEqual(0x0416, OP_LADDER_START_GAME)
        self.assertNotEqual(OP_TRIGGER_COUNT_GAME, OP_LADDER_START_GAME)

    def test_quest_record_in_pvp_has_six_entries(self):
        payload = build_rep_quest_record_in_pvp()
        self.assertEqual((6, 0, 0, 0, 0, 0, 0), struct.unpack("<7i", payload))

    def test_quest_record_in_pvp_rejects_wrong_entry_count(self):
        with self.assertRaises(ValueError):
            build_rep_quest_record_in_pvp([0] * 5)

    def test_quest_record_in_pvp_frame_round_trip(self):
        payload = struct.pack("<7i", 6, 1, 2, 3, 4, 5, 6)
        frame = build_game(0x0311, build_rep_quest_record_in_pvp([1, 2, 3, 4, 5, 6]))
        self.assertEqual(("game", 0x0311, payload, len(frame)), take_frame(frame))

    def test_start_game_packet_payloads_are_single_int32(self):
        self.assertEqual(struct.pack("<i", 0), build_trigger_count_game())
        self.assertEqual(struct.pack("<i", 1), build_rep_count_down(1))
        self.assertEqual(struct.pack("<i", 1234), build_prepare_game(1234))

    def test_single_client_start_game_handshake(self):
        handshake = StartGameHandshake(seed=1234)

        self.assertEqual(
            [(OP_TRIGGER_COUNT_GAME, struct.pack("<i", 0))],
            handshake.on_client_packet(OP_COUNT_GAME_READY, b""),
        )
        self.assertEqual(StartGameHandshake.WAIT_CONFIRM, handshake.state)

        # 第二个 0x0402 只回 0x0400（切 stage 6 = 准备/加载）。
        # 绝不能同时回 0x0402 —— 那是切 stage 7，会把房间的座位角色对象拆掉，
        # 客户端随后在渲染路径解引用 NULL 崩溃（FINDINGS §82/§84）。
        self.assertEqual(
            [(OP_PREPARE_GAME, struct.pack("<i", 1234))],
            handshake.on_client_packet(OP_COUNT_GAME_READY, b""),
        )
        self.assertEqual(StartGameHandshake.PREPARING, handshake.state)

        # 加载完成的轮询（空 0x0403）才轮到 0x0402 = 切 stage 7（游戏本体）。
        self.assertEqual(
            [(OP_COUNT_GAME_READY, b"")],
            handshake.on_client_packet(OP_LOADING_DONE, b""),
        )
        self.assertEqual(StartGameHandshake.IN_GAME, handshake.state)
        # 客户端每 5 秒还会再发，只放行第一发。
        self.assertEqual([], handshake.on_client_packet(OP_LOADING_DONE, b""))

        handshake.reset()
        self.assertEqual(StartGameHandshake.WAIT_START, handshake.state)

    def test_start_game_handshake_never_sends_count_down(self):
        # 0x0412 在 0x54e036 的跳表 @0x54e5ae 里映射到 0x54e546 = 未处理，
        # 客户端收到就丢，服务端不该发。
        handshake = StartGameHandshake()
        sent = []
        for _ in range(3):
            sent += handshake.on_client_packet(OP_COUNT_GAME_READY, b"")
        sent += handshake.on_client_packet(OP_LOADING_DONE, b"")
        opcodes = [opcode for opcode, _ in sent]
        self.assertNotIn(OP_REP_COUNT_DOWN, opcodes)
        self.assertEqual(
            [OP_TRIGGER_COUNT_GAME, OP_PREPARE_GAME, OP_COUNT_GAME_READY],
            opcodes)

    def test_start_game_handshake_rejects_loading_done_before_prepare(self):
        # stage 6 都没进就放 0x0402，会重演 §82 的空指针崩溃。
        handshake = StartGameHandshake()
        self.assertEqual([], handshake.on_client_packet(OP_LOADING_DONE, b""))
        handshake.on_client_packet(OP_COUNT_GAME_READY, b"")
        self.assertEqual([], handshake.on_client_packet(OP_LOADING_DONE, b""))

    def test_start_game_handshake_ignores_early_or_duplicate_packets(self):
        handshake = StartGameHandshake()
        self.assertEqual([], handshake.on_client_packet(OP_PREPARE_GAME, b""))
        handshake.on_client_packet(OP_COUNT_GAME_READY, b"")
        handshake.on_client_packet(OP_COUNT_GAME_READY, b"")
        self.assertEqual([], handshake.on_client_packet(OP_COUNT_GAME_READY, b""))
        self.assertEqual([], handshake.on_client_packet(OP_PREPARE_GAME, b"bad"))

    def test_parse_move_channel_by_game_type(self):
        # 实机抓到的「任务」标签页请求就是这四个字节。
        self.assertEqual(2, parse_move_channel_by_game_type(bytes.fromhex("02000000")))

    def test_parse_move_channel_rejects_truncated_or_trailing_payload(self):
        with self.assertRaises(ValueError):
            parse_move_channel_by_game_type(b"\x02\x00\x00")
        with self.assertRaises(ValueError):
            parse_move_channel_by_game_type(w_i32(2) + b"extra")

    def test_rep_move_into_payload_is_three_int32(self):
        self.assertEqual((1, 7, 0), struct.unpack("<3i", build_rep_move_into(True, 7, 0)))
        self.assertEqual((0, 0, 0), struct.unpack("<3i", build_rep_move_into(False)))

    def test_rep_move_into_frame_round_trip(self):
        payload = build_rep_move_into(True, 7, 0)
        frame = build_game(OP_REP_MOVE_INTO, payload)
        self.assertEqual(
            ("game", OP_REP_MOVE_INTO, payload, len(frame)), take_frame(frame))

    def test_quest_tab_round_trips_through_the_channel_code_tables(self):
        # 客户端标签 1 -> 类型 2；服务端回频道码 7；客户端把 7 翻回类型 2。
        game_type = LOBBY_TAB_GAME_TYPES[1]
        self.assertEqual(2, game_type)
        channel_code = GAME_TYPE_CHANNEL_CODES[game_type]
        self.assertEqual(7, channel_code)
        self.assertEqual(game_type, CHANNEL_CODE_GAME_TYPES[channel_code])

    def test_every_reachable_game_type_has_a_round_tripping_channel_code(self):
        for game_type, channel_code in GAME_TYPE_CHANNEL_CODES.items():
            with self.subTest(game_type=game_type):
                self.assertEqual(game_type, CHANNEL_CODE_GAME_TYPES[channel_code])
        # 类型 6（练习标签）在 0x5545ec 里没有任何频道码能映射回来，
        # 所以服务端不能假装能把客户端移进去。
        self.assertNotIn(6, GAME_TYPE_CHANNEL_CODES)
        self.assertNotIn(6, CHANNEL_CODE_GAME_TYPES.values())

    def test_move_into_and_move_channel_are_different_opcodes(self):
        # 同号的服务端 0x020b 只报失败，成功必须走 0x0701。
        self.assertEqual(0x020b, OP_MOVE_CHANNEL_BY_GAME_TYPE)
        self.assertEqual(0x0701, OP_REP_MOVE_INTO)

    def test_session_descriptor_argument_widths(self):
        # 类型 2（闯关）= 关卡 id + 难度，实机抓到的就是 args=(3, 1)。
        self.assertEqual(
            struct.pack("<3i", 2, 3, 1), build_session_descriptor(2, (3, 1)))
        self.assertEqual(
            struct.pack("<4i", 1, 11, 12, 13),
            build_session_descriptor(1, (11, 12, 13)))
        self.assertEqual(struct.pack("<2i", 0, 7), build_session_descriptor(0, (7,)))

    def test_session_descriptor_rejects_unsafe_types_and_bad_arity(self):
        # 类型 3 / 4 的序列化写 1 个参数、反序列化读 2 个（且第一个覆盖 type），
        # 两侧对不上，服务端不下发。
        for session_type in (3, 4):
            with self.subTest(session_type=session_type):
                self.assertIn(session_type, DESCRIPTOR_SENT_ARGUMENT_COUNTS)
                self.assertNotIn(session_type, DESCRIPTOR_READ_ARGUMENT_COUNTS)
                with self.assertRaises(ValueError):
                    build_session_descriptor(session_type, (1,))
        with self.assertRaises(ValueError):
            build_session_descriptor(2, (3,))
        with self.assertRaises(ValueError):
            build_session_descriptor(2, (3, 1, 0))

    def test_update_session_layout(self):
        payload = build_update_session(2, (3, 1), title="房间")
        self.assertEqual(
            w_i32(SESSION_STATUS_WAITING)
            + w_wstr("房间")
            + w_i32(0)
            + w_wstr("")
            + w_i32(0)
            + struct.pack("<3i", 2, 3, 1)
            + struct.pack("<H", 0),
            payload,
        )

    def test_update_session_status_defaults_to_waiting(self):
        # Session+0x04 != 2 时客户端认为房间「游戏中」，RoomStage 会去建
        # 战斗上下文（闯关房是 GameContextQuest03）而不是待机房间场景，
        # 房间背景就是一片黑（FINDINGS §102）。默认必须是 2。
        self.assertEqual(2, SESSION_STATUS_WAITING)
        self.assertEqual(
            w_i32(2), build_update_session(2, (3, 1))[:4])
        self.assertNotEqual(
            build_update_session(2, (3, 1)),
            build_update_session(2, (3, 1), status=0))

    def test_update_session_map_name_defaults_to_empty(self):
        # 地图名非空会让 0x54f82e 的比较不等，0x54f835 直接 jne 到函数的 ret，
        # 客户端收到建房应答后什么都不做。默认必须是空串。
        self.assertEqual(
            build_update_session(2, (3, 1)), build_update_session(2, (3, 1), map_name=""))
        self.assertNotEqual(
            build_update_session(2, (3, 1)),
            build_update_session(2, (3, 1), map_name="Quest03_1"))

    def test_update_session_frame_round_trip(self):
        payload = build_update_session(2, (3, 1), title="t")
        frame = build_game(OP_UPDATE_SESSION, payload)
        self.assertEqual(
            ("game", OP_UPDATE_SESSION, payload, len(frame)), take_frame(frame))

    def test_update_session_is_a_different_opcode_from_change_session(self):
        # 0x0302 是客户端发来的换房请求，0x0303 是服务端下发的整份 Session。
        self.assertEqual(0x0302, OP_CHANGE_SESSION)
        self.assertEqual(0x0303, OP_UPDATE_SESSION)

    def test_parse_change_session_request(self):
        payload = (
            w_i32(6)
            + w_wstr("想和做朋友吗?")
            + w_wstr("任务")
            + w_i32(1)
            + w_i32(0)
            + w_i32(2)
            + w_i32(3)
            + w_i32(1)
        )
        self.assertEqual(
            {
                "free_slots": 6,
                "texts": ("想和做朋友吗?", "任务"),
                "flags": (1, 0),
                "session_type": 2,
                "session_type_name": "quest",
                "arguments": (3, 1),
            },
            parse_change_session_request(payload),
        )

    def test_parse_change_session_rejects_truncated_or_trailing_payload(self):
        valid = w_i32(0) + w_wstr("") * 2 + w_i32(0) * 2 + w_i32(2) + w_i32(3) + w_i32(1)
        with self.assertRaises(ValueError):
            parse_change_session_request(valid[:-1])
        with self.assertRaises(ValueError):
            parse_change_session_request(valid + b"extra")

    def test_empty_session_slot_is_two_int32(self):
        # 客户端 0x556d9d 见占用标记为 0 就直接跳到末尾那个「关闭」标记，
        # 中间的昵称/等级等字段一个都不读。
        self.assertEqual(struct.pack("<ii", 0, 0), build_session_slot())
        self.assertEqual(struct.pack("<ii", 0, 1),
                         build_session_slot(closed=True))

    def test_occupied_session_slot_field_order(self):
        payload = build_session_slot(occupied=True, nickname="ab", level=7,
                                     unknown_u8=0, character_id=3,
                                     item_ids=(11, 22))
        expected = (
            struct.pack("<i", 1)                       # +0x00 占用
            + struct.pack("<H", 2) + "ab".encode("utf-16le")   # +0x04 昵称
            + struct.pack("<B", 0)                     # +0x08 ★ 只有 1 字节
            + struct.pack("<i", 3)                     # +0x0c 角色 id
            + struct.pack("<iii", 11, 22, 0)           # +0x1c 0 结尾的列表
            + struct.pack("<i", 0)                     # +0x28
            + struct.pack("<H", 0)                     # +0x2c
            + struct.pack("<H", 7)                     # +0x10 ★ 等级
            + struct.pack("<i", 0)                     # +0x2e
            + struct.pack("<H", 0)                     # +0x12
            + struct.pack("<H", 0)                     # +0x30 空串
            + struct.pack("<i", 0)                     # +0x34
            + struct.pack("<i", 0)                     # +0x01 关闭标记
        )
        self.assertEqual(expected, payload)

    def test_session_members_snapshot_layout(self):
        seats = [{"occupied": False}] * ROOM_SEAT_COUNT
        seats[0] = {"occupied": True, "nickname": "testuser", "level": 5}
        payload = build_session_members(host_seat=0, seats=seats)
        # 头两个 int32 = 房主座位号 + 一个未查明字段。
        self.assertEqual((0, 0), struct.unpack_from("<ii", payload))
        head = struct.calcsize("<ii") + len(build_session_slot(**seats[0]))
        # 其余五个座位各占两个 int32 的空座位编码。
        self.assertEqual(struct.pack("<ii", 0, 0) * 5, payload[head:])

    def test_session_members_rejects_wrong_seat_count(self):
        with self.assertRaises(ValueError):
            build_session_members(seats=[{"occupied": False}] * 5)
        with self.assertRaises(ValueError):
            build_session_members(host_seat=ROOM_SEAT_COUNT,
                                  seats=[{"occupied": False}] * ROOM_SEAT_COUNT)

    def test_session_members_frame_round_trip(self):
        seats = [{"occupied": False}] * ROOM_SEAT_COUNT
        seats[0] = {"occupied": True, "nickname": "测试", "level": 1}
        payload = build_session_members(0, seats)
        frame = build_game(OP_SESSION_MEMBERS, payload)
        self.assertEqual(("game", OP_SESSION_MEMBERS, payload, len(frame)),
                         take_frame(frame))

    def test_session_member_update_prefix(self):
        # action 是 1 字节（0x5d5942），座位号是 int32，然后才是 SessionSlot。
        payload = build_session_member_update(2, SEAT_ACTION_JOIN,
                                              occupied=True, nickname="ab",
                                              level=7)
        self.assertEqual(b"\x00" + struct.pack("<i", 2), payload[:5])
        self.assertEqual(
            build_session_slot(occupied=True, nickname="ab", level=7),
            payload[5:])

    def test_parse_seat_change_request_matches_the_observed_payload(self):
        # 实机 59 字节载荷（会话 12 日志 11:17:22，用户连点三个头像）。
        # 客户端方向没有 action 字节，直接是 int32 座位号 + SessionSlot。
        payload = bytes.fromhex(
            "00000000"                     # 座位 0
            "01000000"                     # 占用
            "0800" + "testuser".encode("utf-16le").hex()
            + "00"                         # +0x08 1 字节
            "01000000"                     # ★ 角色 id = 1
            "00000000"                     # 物品列表的 0 结尾
            "00000000"                     # +0x28
            "0000"                         # +0x2c
            "0100"                         # ★ 等级 = 1
            "00000000"                     # +0x2e
            "0000"                         # +0x12
            "0000"                         # +0x30 空串
            "00000000"                     # +0x34
            "00000000"                     # 关闭标记
        )
        self.assertEqual(59, len(payload))
        seat_index, slot = parse_seat_change_request(payload)
        self.assertEqual(0, seat_index)
        self.assertTrue(slot["occupied"])
        self.assertEqual("testuser", slot["nickname"])
        self.assertEqual(1, slot["character_id"])
        self.assertEqual(1, slot["level"])
        self.assertFalse(slot["closed"])

    def test_seat_change_round_trips_through_build_session_slot(self):
        # 收发两侧是同一份 SessionSlot 布局，解出来再组回去必须逐字节相同。
        slot = dict(occupied=True, nickname="测试", unknown_u8=0,
                    character_id=2, item_ids=(11, 22), level=7)
        payload = w_i32(3) + build_session_slot(**slot)
        seat_index, parsed = parse_seat_change_request(payload)
        self.assertEqual(3, seat_index)
        self.assertEqual(2, parsed["character_id"])
        self.assertEqual((11, 22), parsed["item_ids"])
        self.assertEqual(payload, w_i32(seat_index) + build_session_slot(**parsed))

    def test_parse_seat_change_rejects_bad_seat_or_trailing_bytes(self):
        valid = w_i32(0) + build_session_slot(occupied=True, nickname="a")
        with self.assertRaises(ValueError):
            parse_seat_change_request(valid + b"extra")
        with self.assertRaises(ValueError):
            parse_seat_change_request(
                w_i32(ROOM_SEAT_COUNT)
                + build_session_slot(occupied=True, nickname="a"))

    def test_change_character_update_is_action_four(self):
        # 只有 action 4 会走 0x406520 -> 0x406628 重建座位的角色对象，
        # 房间中下那个 3D 预览才会换模型。
        self.assertEqual(4, SEAT_ACTION_CHANGE_CHARACTER)
        payload = build_session_member_update(
            0, SEAT_ACTION_CHANGE_CHARACTER,
            occupied=True, nickname="ab", level=1, character_id=2)
        self.assertEqual(b"\x04" + struct.pack("<i", 0), payload[:5])
        self.assertEqual(
            build_session_slot(occupied=True, nickname="ab", level=1,
                               character_id=2),
            payload[5:])
        frame = build_game(OP_SESSION_MEMBER_UPDATE, payload)
        self.assertEqual(("game", OP_SESSION_MEMBER_UPDATE, payload, len(frame)),
                         take_frame(frame))

    def test_session_member_update_rejects_destructive_actions(self):
        # action 1/2 会走 0x405f8f 销毁座位的角色对象，服务端绝不能发。
        for action in (1, 2):
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    build_session_member_update(0, action)
        with self.assertRaises(ValueError):
            build_session_member_update(ROOM_SEAT_COUNT)

    def test_end_game_payload_is_fourteen_int32(self):
        # 0x54cea3 一共读 14 个 4 字节字段：座位号、成功标志、12 个业务值。
        payload = build_end_game(seat_id=0, success=True)
        self.assertEqual(14 * 4, len(payload))
        self.assertEqual((0, 1), struct.unpack_from("<ii", payload))
        # 未查明的业务值一律 0。+0x1c 在客户端是 `+=`（0x5518c0），
        # 填非 0 会把玩家数据越加越多。
        self.assertEqual(b"\x00" * (12 * 4), payload[8:])

    def test_end_game_rejects_wrong_value_count(self):
        with self.assertRaises(ValueError):
            build_end_game(values=[0] * 11)
        with self.assertRaises(ValueError):
            build_end_game(values=[0] * 13)

    def test_respawn_character_payload_is_four_int32(self):
        # 0x54c5d0 读 4 个 int32；坐标在线上是整数，客户端 fild 转 float。
        payload = build_respawn_character(character_id=2, x=100, y=-50,
                                          unknown=7)
        self.assertEqual((2, 100, -50, 7), struct.unpack("<iiii", payload))

    def test_battle_frames_round_trip(self):
        for opcode, payload in (
            (OP_END_GAME, build_end_game()),
            (OP_RESPAWN_CHARACTER, build_respawn_character()),
        ):
            frame = build_game(opcode, payload)
            self.assertEqual(("game", opcode, payload, len(frame)),
                             take_frame(frame))

    def test_start_game_frames_round_trip(self):
        for opcode, payload in (
            (OP_TRIGGER_COUNT_GAME, build_trigger_count_game()),
            (OP_COUNT_GAME_READY, b""),
            (OP_REP_COUNT_DOWN, build_rep_count_down()),
            (OP_PREPARE_GAME, build_prepare_game()),
        ):
            frame = build_game(opcode, payload)
            self.assertEqual(("game", opcode, payload, len(frame)), take_frame(frame))

    def test_parse_create_item_matches_the_client_serializer(self):
        # 实测的 32 字节载荷（logs/gameserver.out 21:48:26，怪物死后 62 毫秒）：
        # 10202 水炮 @ (2454, 303)，速度 0，尾三格恒为 3 / -1 / -1。
        payload = (bytes.fromhex("da270000" "00601945" "00809743")
                   + struct.pack("<ffiii", 0.0, 0.0, 3, -1, -1))
        self.assertEqual(CREATE_ITEM_SIZE, len(payload))
        self.assertEqual((10202, 2454.0, 303.0, 0.0, 0.0, 3, -1, -1),
                         parse_create_item(payload))

    def test_parse_create_item_rejects_truncated_payload(self):
        with self.assertRaises(ValueError):
            parse_create_item(b"\x00" * (CREATE_ITEM_SIZE - 1))

    def test_created_item_is_the_request_with_a_handle_in_front(self):
        # 反序列化 0x54c523 读 9 个 dword；处理器 0x551a11 把第 1 个当实例句柄
        # （-> [obj+0xd0]），第 2 个当物件 id，3/4 当坐标，5/6 当速度。
        fields = (10101, 100.5, -20.25, 19.0, -52.0, 3, -1, -1)
        body = build_created_item(0x40000005, fields)
        # 9 个 4 字节字段（反序列化器读满 9 个才停），一个不多一个不少。
        self.assertEqual(36, len(body))
        self.assertEqual(0x40000005, struct.unpack_from("<i", body, 0)[0])
        self.assertEqual(fields, struct.unpack_from("<iffffiii", body, 4))

    def test_created_item_rejects_a_wrong_field_count(self):
        with self.assertRaises(ValueError):
            build_created_item(1, (10101, 0.0, 0.0))

    def test_end_game_values_land_in_the_slots_the_client_reads(self):
        # §94：这四个下标的语义是实机确认的，其余 8 个仍未知，必须留 0。
        values = build_end_game_values(experience=250, next_level_exp=300,
                                       level_start_exp=200, money_gained=64)
        self.assertEqual(12, len(values))
        self.assertEqual(250, values[1])   # pkt+0x10 -> [0x72e33c]
        self.assertEqual(300, values[2])   # pkt+0x14 -> [0x72e344]
        self.assertEqual(200, values[3])   # pkt+0x18 -> [0x72e340]
        self.assertEqual(64, values[4])    # pkt+0x1c -> [0x72e330]，客户端 +=
        self.assertEqual([0, 0, 0, 0, 0, 0, 0, 0],
                         [values[i] for i in (0, 5, 6, 7, 8, 9, 10, 11)])

    def test_end_game_values_reproduce_the_observed_probe_display(self):
        # 实机发 101..112 时画面显示「经验值: -2/-1」「200%」「金币: 105」。
        # 客户端算的是绝对值之差，这里照着复现一遍，锁住这个换算关系。
        values = build_end_game_values(experience=102, next_level_exp=103,
                                       level_start_exp=104, money_gained=105)
        current = values[1] - values[3]
        needed = values[2] - values[3]
        self.assertEqual((-2, -1), (current, needed))
        self.assertEqual(105, values[4])

    def test_experience_bounds_bracket_the_total(self):
        # 客户端要的是绝对累计值，不是本级内的差值。
        for experience in (0, 1, 99, 100, 101, 250, 999):
            start, nxt = experience_bounds(experience)
            self.assertLessEqual(start, experience)
            self.assertLess(experience, nxt)
            self.assertEqual(EXPERIENCE_PER_LEVEL, nxt - start)

    def test_level_tracks_the_experience_curve(self):
        self.assertEqual(1, level_for_experience(0))
        self.assertEqual(1, level_for_experience(EXPERIENCE_PER_LEVEL - 1))
        self.assertEqual(2, level_for_experience(EXPERIENCE_PER_LEVEL))
        self.assertEqual(3, level_for_experience(EXPERIENCE_PER_LEVEL * 2))

    def test_rep_money_wire_layout(self):
        # 反序列化 0x54c7c3 读 5 个 int32 + 1 个 u16 + 2 个 int32 = 30 字节。
        # 等级那一格是 u16（0x5d59f1 读 2 字节）—— 写成 int32 后两个字段就错位。
        payload = build_rep_money(money=1234, experience=650,
                                  level_start_exp=600, next_level_exp=700,
                                  level=7)
        self.assertEqual(30, len(payload))
        head = struct.unpack_from("<5i", payload, 0)
        self.assertEqual(0, head[0])        # +0x04 -> 0x72e334，语义未查
        self.assertEqual(1234, head[1])     # +0x08 -> 0x72e330  金币
        self.assertEqual(650, head[2])      # +0x0c -> 0x72e33c  总经验
        self.assertEqual(600, head[3])      # +0x10 -> 0x72e340  本级起点
        self.assertEqual(700, head[4])      # +0x14 -> 0x72e344  下一级所需
        self.assertEqual(7, struct.unpack_from("<H", payload, 20)[0])
        self.assertEqual((0, 0), struct.unpack_from("<2i", payload, 22))

    def test_rep_money_frame_round_trip(self):
        frame = build_game(OP_REP_MONEY, build_rep_money(money=5))
        kind, opcode, payload, consumed = take_frame(frame)
        self.assertEqual(("game", 0x0600, len(frame)), (kind, opcode, consumed))
        self.assertEqual(30, len(payload))

    def test_rep_money_from_account_matches_the_end_game_encoding(self):
        # 同一份存档下，0x0600 和 0x0411 报出的经验三件套必须一致，
        # 否则结算完回大厅进度条会跳。
        account = {"level": 3, "experience": 250, "money": 64}
        payload = build_rep_money_for(account)
        money, experience, start, nxt = struct.unpack_from("<4i", payload, 4)
        values = build_end_game_values(experience=experience,
                                       next_level_exp=nxt,
                                       level_start_exp=start)
        self.assertEqual(64, money)
        self.assertEqual((250, 200, 300), (values[1], values[3], values[2]))
        self.assertEqual(3, struct.unpack_from("<H", payload, 20)[0])

    def test_rep_money_clamps_the_level_into_a_u16(self):
        payload = build_rep_money(level=-1)
        self.assertEqual(0, struct.unpack_from("<H", payload, 20)[0])

    def test_leave_result_opcode(self):
        self.assertEqual(0x0405, OP_LEAVE_RESULT)

    def test_rep_leave_session_is_one_int32_and_zero_means_success(self):
        # 0x54fffe 只读一个 int32：== 0 走 0x552943（切回大厅），
        # != 0 弹「퇴장 실패 / 방에서 나갈수 없습니다.」（§101）。
        self.assertEqual(0x0203, OP_LEAVE_SESSION)
        self.assertEqual(w_i32(0), build_rep_leave_session(0))
        self.assertEqual(4, len(build_rep_leave_session(0)))
        self.assertEqual(w_i32(1), build_rep_leave_session(1))

    def test_rep_leave_session_frame_round_trip(self):
        frame = build_game(OP_LEAVE_SESSION, build_rep_leave_session(0))
        kind, opcode, payload, consumed = take_frame(frame)
        self.assertEqual(("game", 0x0203, len(frame)), (kind, opcode, consumed))
        self.assertEqual(w_i32(0), payload)

    def test_leave_session_is_not_the_same_opcode_as_leave_result(self):
        # 0x0203 是「离开房间」，0x0405 是「结算界面看完了」。会话 08 的 §72
        # 曾经把 0x0203 记成「点游戏开始」，别再混。
        self.assertNotEqual(OP_LEAVE_SESSION, OP_LEAVE_RESULT)

    def test_rep_game_result_wire_layout(self):
        # 实机接受的就是这个 80 字节形状：13 个 int32 + n + n 个 int32（§99）。
        # 两个 bool 字段在线上也是 4 字节（0x5d59de 读 4 存 1）。
        payload = build_rep_game_result(seat_id=0)
        self.assertEqual(80, len(payload))
        self.assertEqual(0, struct.unpack_from("<i", payload, 0)[0])
        self.assertEqual(GAME_RESULT_TAIL_COUNT,
                         struct.unpack_from("<i", payload, 52)[0])

    def test_rep_game_result_keeps_field_order(self):
        values = list(range(1, GAME_RESULT_VALUE_COUNT + 1))
        payload = build_rep_game_result(seat_id=2, values=values,
                                        tail=[7] * GAME_RESULT_TAIL_COUNT)
        self.assertEqual(2, struct.unpack_from("<i", payload, 0)[0])
        self.assertEqual(values, list(
            struct.unpack_from(f"<{GAME_RESULT_VALUE_COUNT}i", payload, 4)))
        self.assertEqual([7] * GAME_RESULT_TAIL_COUNT,
                         list(struct.unpack_from(
                             f"<{GAME_RESULT_TAIL_COUNT}i", payload, 56)))

    def test_rep_game_result_rejects_a_short_tail(self):
        # 0x5521d2 无条件读 6 个 dword；数组更短就读到 vector 外面去了。
        with self.assertRaises(ValueError):
            build_rep_game_result(tail=[0, 0, 0])
        with self.assertRaises(ValueError):
            build_rep_game_result(tail=[])

    def test_rep_game_result_rejects_wrong_value_count(self):
        with self.assertRaises(ValueError):
            build_rep_game_result(values=[0, 0, 0])

    def test_rep_game_result_frame_round_trip(self):
        frame = build_game(OP_REP_GAME_RESULT, build_rep_game_result())
        kind, opcode, payload, consumed = take_frame(frame)
        self.assertEqual(("game", 0x0309, len(frame)), (kind, opcode, consumed))
        self.assertEqual(80, len(payload))

    def test_parse_first_user_result_is_one_int32(self):
        # 序列化 0x404ee8 只写包对象 +0x04 一个 int32。实测上报 4 或 5。
        self.assertEqual(5, parse_first_user_result(w_i32(5)))
        self.assertEqual(4, parse_first_user_result(w_i32(4)))
        self.assertEqual(0x030f, OP_REQ_FIRST_USER_RESULT)

    def test_parse_first_user_result_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            parse_first_user_result(w_i32(5) + b"\x00")
        with self.assertRaises(ValueError):
            parse_first_user_result(b"\x00\x00")


class LeaveSessionTests(unittest.TestCase):
    """离开房间 `0x0203`：不回这个包，客户端永远出不了房间（§101）。

    直接拿真的 `Conn` 方法跑（绕过 `__init__` 里的 socket / 文件），
    这样测的就是实际接线，而不是复制一份逻辑。
    """

    class Args:
        hold_lobby = False

    def make_conn(self):
        conn = gameserver.Conn.__new__(gameserver.Conn)
        conn.args = self.Args()
        conn.sent = []
        conn.logged = []
        conn.last_packet_at = 0.0
        conn.noisy_seen = set()
        conn.log = conn.logged.append
        conn.send = conn.sent.append
        # send_batch() 只管「这些包合成一次 sendall」，对逐包断言透明：
        # send 被换成 append 之后 send_queue 永远是空的，退出时什么都不发。
        # 真正的合并行为由 SendBatchTests 用带假 socket 的真 Conn 覆盖。
        conn.send_lock = threading.RLock()
        conn.send_queue = None
        conn.batch_delay_ms = 0
        conn.room = {"session_type": 2}
        conn.settled = True
        conn.quest_score = 64
        conn.quest_success = True
        conn.next_item_handle = gameserver.ITEM_HANDLE_BASE
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
        conn.start_game = StartGameHandshake()
        return conn

    def test_leave_session_replies_success_and_clears_the_room(self):
        conn = self.make_conn()
        conn.start_game.on_client_packet(OP_COUNT_GAME_READY, b"")
        gameserver.Conn.leave_session(conn)
        self.assertEqual(1, len(conn.sent))
        kind, opcode, payload, _ = take_frame(conn.sent[0])
        self.assertEqual(("game", OP_LEAVE_SESSION, w_i32(0)),
                         (kind, opcode, payload))
        # 房间没了，跟房间绑定的状态必须一起作废，否则下次建房带着残留。
        self.assertIsNone(conn.room)
        self.assertFalse(conn.settled)
        self.assertEqual(0, conn.quest_score)
        self.assertEqual(StartGameHandshake().state, conn.start_game.state)

    def test_incoming_leave_session_packet_is_routed_to_the_reply(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, OP_LEAVE_SESSION, b"")
        self.assertEqual(1, len(conn.sent))
        _, opcode, payload, _ = take_frame(conn.sent[0])
        self.assertEqual((OP_LEAVE_SESSION, w_i32(0)), (opcode, payload))


class ControlChannelTests(unittest.TestCase):
    """调试控制通道：把命令行翻成真正的游戏包。

    这些测试用一个假 Conn 记下「发出去的是什么帧」，因为控制通道存在的唯一
    目的就是精确控制推给客户端的字节（见 `handle_control_command` 的文档）。
    """

    class FakeConn:
        def __init__(self):
            self.seq = 1
            self.sent = []
            self.logged = []
            self.settled = []
            self.account_name = "testuser"
            self.account = {"level": 3, "experience": 250, "money": 64,
                            "tutorial_completed": True}
            self.reloaded = 0
            self.money_sent = 0
            self.difficulty_sent = 0
            self.equipped_sent = 0
            self.my_seat = 0
            self.quest_score = 64
            self.last_position = (3225.0, 635.0)
            self.room = {"session_type": 2, "arguments": (3, 1)}
            self.start_game = StartGameHandshake()

        def log(self, message):
            self.logged.append(message)

        def send(self, plain):
            self.sent.append(plain)

        def send_end_game(self, success=True):
            self.settled.append(success)

        def reload_account(self):
            self.reloaded += 1

        def send_rep_money(self, reason=""):
            self.money_sent += 1

        def send_quest_reached_difficulty(self, reason=""):
            self.difficulty_sent += 1

        def send_slot_equipped_list(self, seat_index=None, reason=""):
            self.equipped_sent += 1

        def current_quest(self):
            return gameserver.Conn.current_quest(self)

        def respawn_position(self):
            x, y = self.last_position
            return int(x), int(y)

    def setUp(self):
        self.conn = self.FakeConn()
        self._saved = list(gameserver._conns)
        gameserver._conns[:] = [self.conn]
        self.addCleanup(lambda: gameserver._conns.__setitem__(
            slice(None), self._saved))

    def only_frame(self):
        self.assertEqual(1, len(self.conn.sent))
        return take_frame(self.conn.sent[0])

    def test_endgame_probe_sends_distinguishable_values(self):
        # 12 个业务值填成 101..112，实机截图就能一眼看出结算界面哪一格是哪个字段。
        reply = gameserver.handle_control_command("endgame-probe")
        self.assertTrue(reply.startswith("ok"), reply)
        kind, opcode, payload, _ = self.only_frame()
        self.assertEqual(("game", OP_END_GAME), (kind, opcode))
        fields = struct.unpack("<14i", payload)
        self.assertEqual((0, 1), fields[:2])
        self.assertEqual(tuple(range(101, 113)), fields[2:])

    def test_bare_endgame_goes_through_the_real_settlement_path(self):
        # 不带参数的 endgame 必须和客户端打完关卡（0x040f）走同一条路，
        # 否则用它做的验证证明不了正式路径能用。
        reply = gameserver.handle_control_command("endgame")
        self.assertTrue(reply.startswith("ok"), reply)
        self.assertEqual([True], self.conn.settled)
        self.assertEqual([], self.conn.sent)

    def test_endgame_pads_missing_values_with_zero(self):
        gameserver.handle_control_command("endgame 1 0 7 8")
        _, _, payload, _ = self.only_frame()
        self.assertEqual((1, 0, 7, 8) + (0,) * 10, struct.unpack("<14i", payload))

    def test_respawn_without_coordinates_uses_the_reported_position(self):
        # 写死的坐标换了场景就非法，客户端 23ms 后就发 0x0106 告状（§88）。
        gameserver.handle_control_command("respawn")
        kind, opcode, payload, _ = self.only_frame()
        self.assertEqual(("game", OP_RESPAWN_CHARACTER), (kind, opcode))
        self.assertEqual((0, 3225, 635, 0), struct.unpack("<4i", payload))

    def test_respawn_accepts_explicit_coordinates(self):
        gameserver.handle_control_command("respawn 2 -10 20 3")
        _, _, payload, _ = self.only_frame()
        self.assertEqual((2, -10, 20, 3), struct.unpack("<4i", payload))

    def test_raw_sends_the_exact_bytes(self):
        gameserver.handle_control_command("raw 0419 01000000 02000000")
        kind, opcode, payload, _ = self.only_frame()
        self.assertEqual(("game", OP_RESPAWN_CHARACTER), (kind, opcode))
        self.assertEqual(bytes.fromhex("0100000002000000"), payload)

    def test_gameresult_probe_sends_distinguishable_values(self):
        reply = gameserver.handle_control_command("gameresult-probe")
        self.assertTrue(reply.startswith("ok"))
        kind, opcode, payload, _ = self.only_frame()
        self.assertEqual(("game", 0x0309), (kind, opcode))
        self.assertEqual(list(range(201, 213)), list(
            struct.unpack_from("<12i", payload, 4)))
        self.assertEqual(list(range(301, 307)), list(
            struct.unpack_from("<6i", payload, 56)))

    def test_gameresult_defaults_to_zeros_and_my_seat(self):
        gameserver.handle_control_command("gameresult")
        _, _, payload, _ = self.only_frame()
        self.assertEqual(80, len(payload))
        self.assertEqual([0] * 13, list(struct.unpack_from("<13i", payload, 0)))

    def test_sync_account_rereads_the_store_before_sending(self):
        # 顺序要紧：先重读盘上的存档，再按它下发，否则发的还是旧值。
        # 数据栏（0x0600）、难度解锁表（0x020c）和角色解锁表（0x030b）
        # 都要跟着刷。
        reply = gameserver.handle_control_command("sync-account")
        self.assertTrue(reply.startswith("ok"))
        self.assertEqual((1, 1, 1, 1),
                         (self.conn.reloaded, self.conn.money_sent,
                          self.conn.difficulty_sent, self.conn.equipped_sent))

    def test_quest_difficulty_without_arguments_follows_the_save(self):
        reply = gameserver.handle_control_command("quest-difficulty")
        self.assertTrue(reply.startswith("ok"), reply)
        self.assertEqual((1, 1), (self.conn.reloaded, self.conn.difficulty_sent))
        self.assertEqual([], self.conn.sent)

    def test_quest_difficulty_with_arguments_sends_that_exact_table(self):
        reply = gameserver.handle_control_command("quest-difficulty 3 0 1 2")
        self.assertTrue(reply.startswith("ok"), reply)
        kind, opcode, payload, _ = self.only_frame()
        self.assertEqual(("game", OP_QUEST_REACHED_DIFFICULTY), (kind, opcode))
        self.assertEqual((2, 1, 2, 3, 0), struct.unpack("<5i", payload))

    def test_quest_difficulty_rejects_an_odd_argument_count(self):
        reply = gameserver.handle_control_command("quest-difficulty 3")
        self.assertTrue(reply.startswith("err"), reply)
        self.assertEqual([], self.conn.sent)

    def test_status_reports_the_account_figures(self):
        reply = gameserver.handle_control_command("status")
        self.assertIn("level=3", reply)
        self.assertIn("exp=250", reply)
        self.assertIn("money=64", reply)
        self.assertIn("tutorial=3", reply)

    def test_back_to_room_sends_empty_loading_done(self):
        gameserver.handle_control_command("back-to-room")
        kind, opcode, payload, _ = self.only_frame()
        self.assertEqual(("game", OP_LOADING_DONE, b""), (kind, opcode, payload))

    def test_status_and_help_send_nothing(self):
        for line in ("status", "help", ""):
            gameserver.handle_control_command(line)
        self.assertEqual([], self.conn.sent)

    def test_unknown_command_is_reported_and_sends_nothing(self):
        reply = gameserver.handle_control_command("selfdestruct")
        self.assertTrue(reply.startswith("err"), reply)
        self.assertEqual([], self.conn.sent)

    def test_endgame_rejects_too_many_values(self):
        reply = gameserver.handle_control_command(
            "endgame 0 1 " + " ".join(["1"] * 13))
        self.assertTrue(reply.startswith("err"), reply)
        self.assertEqual([], self.conn.sent)

    def test_commands_report_an_error_when_no_client_is_connected(self):
        gameserver._conns[:] = []
        reply = gameserver.handle_control_command("endgame-probe")
        self.assertTrue(reply.startswith("err"), reply)


class NoisyPacketLoggingTests(unittest.TestCase):
    """高频战斗包的静音（会话 14，§105）。

    静音只影响**记不记日志**，绝不能影响应答 —— 所以每条用例都同时检查
    「日志少了」和「该发的包一个没少」。
    """

    class Args:
        hold_lobby = False

    def make_conn(self):
        conn = gameserver.Conn.__new__(gameserver.Conn)
        conn.args = self.Args()
        conn.sent = []
        conn.logged = []
        conn.last_packet_at = 0.0
        conn.noisy_seen = set()
        conn.last_position = None
        conn.log = conn.logged.append
        conn.send = conn.sent.append
        # send_batch() 只管「这些包合成一次 sendall」，对逐包断言透明：
        # send 被换成 append 之后 send_queue 永远是空的，退出时什么都不发。
        # 真正的合并行为由 SendBatchTests 用带假 socket 的真 Conn 覆盖。
        conn.send_lock = threading.RLock()
        conn.send_queue = None
        conn.batch_delay_ms = 0
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = 0
        conn.quest_success = False
        conn.next_item_handle = gameserver.ITEM_HANDLE_BASE
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
        conn.start_game = StartGameHandshake()
        return conn

    def setUp(self):
        self._verbose = gameserver.VERBOSE
        self.addCleanup(setattr, gameserver, "VERBOSE", self._verbose)

    def feed_drop_packets(self, count):
        conn = self.make_conn()
        # 0x0406 = gcpCreateItem，8 个字段 32 字节（§112 勘误了「6 个 float
        # 的位置同步」那个旧读法）。通关后的金币雨每 ~300 毫秒就来一发。
        payload = struct.pack("<iffffiii", 10101, 3225.0, 635.0, 0.0, 0.0, 3, -1, -1)
        for _ in range(count):
            gameserver.Conn.on_game_packet(conn, gameserver.OP_CREATE_ITEM, payload)
        return conn

    def test_noisy_opcode_is_logged_once_then_muted(self):
        gameserver.VERBOSE = False
        conn = self.feed_drop_packets(20)
        hits = [l for l in conn.logged if "0x0406" in l]
        self.assertEqual(1, len(hits), f"应只报第一次，实得 {len(hits)} 条")
        self.assertIn("高频包", hits[0])

    def test_verbose_logs_every_noisy_packet_with_a_hexdump(self):
        gameserver.VERBOSE = True
        conn = self.feed_drop_packets(20)
        hits = [l for l in conn.logged if "0x0406" in l]
        self.assertEqual(20, len(hits))
        self.assertIn("0000  ", hits[0])   # verbose 才带 hexdump

    def test_muting_does_not_change_what_the_client_receives(self):
        # 同一串包在两种日志级别下必须产生**逐字节相同**的下发。
        gameserver.VERBOSE = False
        quiet = self.feed_drop_packets(5)
        gameserver.VERBOSE = True
        loud = self.feed_drop_packets(5)
        self.assertEqual(loud.sent, quiet.sent)

    def test_ordinary_opcodes_are_never_muted(self):
        gameserver.VERBOSE = False
        conn = self.make_conn()
        for _ in range(3):
            gameserver.Conn.on_game_packet(conn, OP_LEAVE_SESSION, b"")
        self.assertEqual(3, len([l for l in conn.logged if "0x0203" in l]))

    def test_muted_drop_requests_are_still_answered(self):
        # 静音只影响日志：每一件掉落物照样要回一发 0x0404，
        # 而且掉落点照样要记下来（重生坐标的兜底靠它）。
        gameserver.VERBOSE = False
        conn = self.feed_drop_packets(3)
        self.assertEqual([gameserver.OP_CREATED_ITEM] * 3,
                         [take_frame(bytearray(f))[1] for f in conn.sent])
        self.assertEqual((3225.0, 635.0), conn.last_position)


class DeathAndRespawnTests(unittest.TestCase):
    """血量归零 -> 倒下 -> 5 秒后重生 的完整回路（会话 15，§108）。

    在这之前服务端对 `0x0408` / `0x0413` 一个字都不回，于是角色 HP 归零后
    既不死也不重生，只是变成不能操作的活死人 —— 用户实机反馈的正是这个。
    """

    class Args:
        hold_lobby = False
        no_death_reply = False

    def make_conn(self):
        conn = gameserver.Conn.__new__(gameserver.Conn)
        conn.args = self.Args()
        conn.sent = []
        conn.logged = []
        conn.last_packet_at = 0.0
        conn.noisy_seen = set()
        conn.last_position = None
        conn.deaths_broadcast = 0
        conn.respawn_sent = 0
        conn.log = conn.logged.append
        conn.send = conn.sent.append
        # send_batch() 只管「这些包合成一次 sendall」，对逐包断言透明：
        # send 被换成 append 之后 send_queue 永远是空的，退出时什么都不发。
        # 真正的合并行为由 SendBatchTests 用带假 socket 的真 Conn 覆盖。
        conn.send_lock = threading.RLock()
        conn.send_queue = None
        conn.batch_delay_ms = 0
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = 0
        conn.quest_success = False
        conn.next_item_handle = gameserver.ITEM_HANDLE_BASE
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
        conn.start_game = StartGameHandshake()
        return conn

    @staticmethod
    def hp_zero_payload(handle=0x3C08AAD0, seat=0, arg=0xFF, deaths=0,
                        x=3225.0, y=635.0):
        return struct.pack("<IBBiff", handle, seat, arg, deaths, x, y)

    # ---- 0x0408 解析 ----------------------------------------------------

    def test_report_hp_zero_layout_matches_the_client_serializer(self):
        # 线格式来自 0x558f16：int32 / u8 / u8 / int32 / float / float = 18 字节。
        payload = self.hp_zero_payload(arg=7, deaths=0x1234)
        self.assertEqual(18, len(payload))
        info = parse_report_hp_zero(payload)
        self.assertEqual(0x3C08AAD0, info["handle"])
        self.assertEqual(0, info["seat"])
        self.assertEqual(7, info["arg"])
        self.assertEqual(0x1234, info["deaths"])
        self.assertEqual((3225.0, 635.0), (info["x"], info["y"]))

    def test_report_hp_zero_reads_the_seat_byte_as_signed(self):
        # 怪物/NPC 实测报的是 0xff = -1（§86 那串 `06 c9 10 00 ff 00 ...`）。
        info = parse_report_hp_zero(self.hp_zero_payload(seat=0xFF))
        self.assertEqual(-1, info["seat"])

    def test_report_hp_zero_rejects_short_payloads(self):
        with self.assertRaises(ValueError):
            parse_report_hp_zero(b"\x00" * 17)

    # ---- 0x0408 -> 0x0406 ----------------------------------------------

    def test_hp_zero_is_answered_with_a_death_broadcast(self):
        conn = self.make_conn()
        payload = self.hp_zero_payload()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_REPORT_HP_ZERO, payload)
        self.assertEqual(1, len(conn.sent))
        kind, opcode, body, _ = take_frame(bytearray(conn.sent[0]))[:4]
        self.assertEqual("game", kind)
        self.assertEqual(gameserver.OP_BROADCAST_DEATH, opcode)
        self.assertEqual(18, len(body))

    def test_death_broadcast_only_changes_the_death_count(self):
        # ★ 这条是会话 15 那次崩溃的回归测试：当时按客户端结构体的 +0x08 去
        # 改包，而**线上**这一格在偏移 6（序列化器是紧凑写的），结果写坏了 X
        # 并把死亡次数冲成六万多，客户端左下角状态面板拿
        # `剩余生命 = 最大生命 - 死亡次数` 当数组下标，负数直接越界崩。
        # 把回包重新解析一遍，就能钉死「除了死亡次数，别的字段一个都没动」。
        conn = self.make_conn()
        payload = self.hp_zero_payload(handle=0x0010C8FB, seat=3, arg=0xFF,
                                       deaths=1, x=-1500.5, y=820.25)
        gameserver.Conn.on_game_packet(conn, gameserver.OP_REPORT_HP_ZERO, payload)
        body = take_frame(bytearray(conn.sent[0]))[2]
        sent, got = parse_report_hp_zero(payload), parse_report_hp_zero(body)
        self.assertEqual(sent["deaths"] + 1, got["deaths"])
        for field in ("handle", "seat", "arg", "x", "y"):
            self.assertEqual(sent[field], got[field], field)

    def test_death_broadcast_increments_the_death_count(self):
        # ★ HUD 心形 = 最大生命 - [char+0x600]，而 [char+0x600] 只由这一格设定。
        # 照抄回去（曾经的做法）心形就永远是 3 颗，但「死 3 次判负」照常 ——
        # 因为那份计数是客户端 0x48c942 本地加的。用户报的正是这个组合。
        conn = self.make_conn()
        for reported, expected in ((0, 1), (1, 2), (2, 3)):
            conn.sent.clear()
            gameserver.Conn.on_game_packet(
                conn, gameserver.OP_REPORT_HP_ZERO,
                self.hp_zero_payload(deaths=reported))
            body = take_frame(bytearray(conn.sent[0]))[2]
            self.assertEqual(expected, parse_report_hp_zero(body)["deaths"])

    def test_death_count_never_lands_on_the_struct_offset(self):
        # 线格式必须和客户端序列化器 0x558f16 一致：紧凑、无对齐填充。
        # 死亡次数在**线偏移 6**；如果哪天有人手滑改成 8，这条会红。
        payload = self.hp_zero_payload(deaths=0x11223344)
        self.assertEqual(18, len(payload))
        self.assertEqual(0x11223344, struct.unpack_from("<i", payload, 6)[0])

    def test_death_broadcast_counts_monsters_too(self):
        # 怪物报的座位是 0xff = -1，同样要回，否则怪打不死（§108）。
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(
            conn, gameserver.OP_REPORT_HP_ZERO,
            self.hp_zero_payload(handle=0x0010C8FB, seat=0xFF, deaths=0))
        body = take_frame(bytearray(conn.sent[0]))[2]
        got = parse_report_hp_zero(body)
        self.assertEqual(0x0010C8FB, got["handle"])
        self.assertEqual(-1, got["seat"])
        self.assertEqual(1, got["deaths"])

    def test_create_item_request_is_never_echoed_on_the_same_opcode(self):
        # 0x0406 收发同号但语义相反：客户端方向是 gcpCreateItem（掉落请求），
        # 服务端方向是死亡广播。回显 = 随机杀角色，所以应答必须是 0x0404。
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(
            conn, gameserver.OP_CREATE_ITEM,
            struct.pack("<iffffiii", 0x27DA, 3225.0, 635.0, 0.0, 0.0, 3, -1, -1))
        opcodes = [take_frame(bytearray(f))[1] for f in conn.sent]
        self.assertEqual([gameserver.OP_CREATED_ITEM], opcodes)
        self.assertNotIn(gameserver.OP_BROADCAST_DEATH, opcodes)
        self.assertEqual((3225.0, 635.0), conn.last_position)

    def test_no_death_reply_switch_restores_the_old_broken_behaviour(self):
        conn = self.make_conn()
        conn.args.no_death_reply = True
        gameserver.Conn.on_game_packet(conn, gameserver.OP_REPORT_HP_ZERO,
                                       self.hp_zero_payload())
        self.assertEqual([], conn.sent)

    def test_malformed_hp_zero_report_is_logged_and_dropped(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_REPORT_HP_ZERO, b"\x00" * 4)
        self.assertEqual([], conn.sent)
        self.assertTrue(any("解析失败" in line for line in conn.logged))

    # ---- 0x0413 -> 0x0419 ----------------------------------------------

    def test_respawn_request_is_echoed_verbatim(self):
        # 坐标由客户端自己选好（0x4fe70e 的重生点），服务端原样回显就一定合法
        # —— 会话 09 写死 3225/635 把角色传到地图边缘的坑（§88）从根上没了。
        conn = self.make_conn()
        payload = struct.pack("<iiii", 3, -1500, 820, 2)
        gameserver.Conn.on_game_packet(conn, gameserver.OP_REQ_RESPAWN, payload)
        self.assertEqual(1, len(conn.sent))
        kind, opcode, body, _ = take_frame(bytearray(conn.sent[0]))[:4]
        self.assertEqual("game", kind)
        self.assertEqual(OP_RESPAWN_CHARACTER, opcode)
        self.assertEqual(payload, body)

    def test_respawn_request_layout(self):
        info = parse_respawn_request(struct.pack("<iiii", 3, -1500, 820, 2))
        self.assertEqual({"character_id": 3, "x": -1500, "y": 820,
                          "spawn_index": 2}, info)

    def test_respawn_request_rejects_short_payloads(self):
        with self.assertRaises(ValueError):
            parse_respawn_request(b"\x00" * 15)

    def test_malformed_respawn_request_is_logged_and_dropped(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_REQ_RESPAWN, b"\x00" * 8)
        self.assertEqual([], conn.sent)
        self.assertTrue(any("解析失败" in line for line in conn.logged))

    # ---- 整条回路 ------------------------------------------------------

    def test_full_death_to_respawn_round_trip(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_REPORT_HP_ZERO,
                                       self.hp_zero_payload())
        # …客户端倒下，5 秒后自己发 0x0413…
        gameserver.Conn.on_game_packet(conn, gameserver.OP_REQ_RESPAWN,
                                       struct.pack("<iiii", 0, 3225, 635, 1))
        opcodes = [take_frame(bytearray(f))[1] for f in conn.sent]
        self.assertEqual([gameserver.OP_BROADCAST_DEATH, OP_RESPAWN_CHARACTER],
                         opcodes)

    def test_death_broadcast_builder_is_ten_bytes(self):
        # 客户端读侧 0x4938d2 只读 u32 + u8 + u8 + u32（紧凑，共 10 字节）。
        built = build_broadcast_death(0x11223344, 2, 3, 4)
        self.assertEqual(10, len(built))
        self.assertEqual(bytes.fromhex("44332211020304000000"), built)


class QuestScoreTests(unittest.TestCase):
    """`0x0410 -> 0x0415`：右上角战绩面板「分数」列的唯一数据源（§109）。"""

    class Args:
        hold_lobby = False

    def make_conn(self):
        conn = gameserver.Conn.__new__(gameserver.Conn)
        conn.args = self.Args()
        conn.sent = []
        conn.logged = []
        conn.last_packet_at = 0.0
        conn.noisy_seen = set()
        conn.last_position = None
        conn.log = conn.logged.append
        conn.send = conn.sent.append
        # send_batch() 只管「这些包合成一次 sendall」，对逐包断言透明：
        # send 被换成 append 之后 send_queue 永远是空的，退出时什么都不发。
        # 真正的合并行为由 SendBatchTests 用带假 socket 的真 Conn 覆盖。
        conn.send_lock = threading.RLock()
        conn.send_queue = None
        conn.batch_delay_ms = 0
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = 0
        conn.quest_success = False
        conn.next_item_handle = gameserver.ITEM_HANDLE_BASE
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
        conn.start_game = StartGameHandshake()
        return conn

    def test_score_report_is_echoed_back_with_the_seat(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_UPDATE_QUEST_SCORE,
                                       w_i32(64))
        self.assertEqual(64, conn.quest_score)
        kind, opcode, body, _ = take_frame(bytearray(conn.sent[0]))[:4]
        self.assertEqual("game", kind)
        self.assertEqual(gameserver.OP_REP_QUEST_SCORE, opcode)
        self.assertEqual(w_i32(0) + w_i32(64), body)

    def test_score_is_cumulative_not_incremental(self):
        # 客户端 0x4a414a 先把增量加到 [ctx+0x3b4] 再发新的累计值，所以直接覆盖。
        conn = self.make_conn()
        for total in (4, 12, 20, 64):
            gameserver.Conn.on_game_packet(conn,
                                           gameserver.OP_UPDATE_QUEST_SCORE,
                                           w_i32(total))
        self.assertEqual(64, conn.quest_score)
        bodies = [take_frame(bytearray(f))[2] for f in conn.sent]
        self.assertEqual([w_i32(0) + w_i32(t) for t in (4, 12, 20, 64)], bodies)

    def test_malformed_score_report_does_not_reply(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_UPDATE_QUEST_SCORE,
                                       b"\x00\x00")
        self.assertEqual([], conn.sent)
        self.assertEqual(0, conn.quest_score)


class MapTransitionTests(unittest.TestCase):
    """关卡内换图：`0x0411 -> 0x0417` 和 `0x0412 -> 0x0418`（会话 16，§111）。

    在这之前服务端对这两个包一个字都不回，于是玩家走到地图最右边之后
    角色卡住不能操作、鼠标变成沙漏（客户端 `0x4083e1` 把
    `[LobbyStage+0x3f9]` 置 1 之后就一直等服务端）—— 和 §108 的
    「血量归零不死」是同一类病。
    """

    class Args:
        hold_lobby = False

    def make_conn(self):
        conn = gameserver.Conn.__new__(gameserver.Conn)
        conn.args = self.Args()
        conn.sent = []
        conn.logged = []
        conn.last_packet_at = 0.0
        conn.noisy_seen = set()
        conn.last_position = None
        conn.log = conn.logged.append
        conn.send = conn.sent.append
        # send_batch() 只管「这些包合成一次 sendall」，对逐包断言透明：
        # send 被换成 append 之后 send_queue 永远是空的，退出时什么都不发。
        # 真正的合并行为由 SendBatchTests 用带假 socket 的真 Conn 覆盖。
        conn.send_lock = threading.RLock()
        conn.send_queue = None
        conn.batch_delay_ms = 0
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = 0
        conn.quest_success = False
        conn.next_item_handle = gameserver.ITEM_HANDLE_BASE
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
        conn.maps_entered = []
        conn.map_change_pending = False
        conn.start_game = StartGameHandshake()
        return conn

    # ---- 0x0411 解析 ----------------------------------------------------

    def test_request_payload_is_a_single_wide_string(self):
        # 序列化 0x404f49 -> 0x5d5a5a：u16 字符数 + UTF-16LE，没有结尾 NUL。
        payload = w_wstr("Quest03_2")
        self.assertEqual(2 + len("Quest03_2") * 2, len(payload))
        self.assertEqual("Quest03_2", parse_req_change_to_next_map(payload))

    def test_request_with_an_empty_name_is_rejected(self):
        # 客户端只在从地图目录里查到下一张地图时才发包（0x40842e 的分支是
        # 「查不到就根本不发」），空名字说明我们把包读错了。
        with self.assertRaises(ValueError):
            parse_req_change_to_next_map(w_wstr(""))

    def test_request_rejects_a_truncated_payload(self):
        with self.assertRaises(ValueError):
            parse_req_change_to_next_map(b"\x05\x00ab")

    # ---- 0x0411 -> 0x0417 ------------------------------------------------

    def test_map_request_is_echoed_back_verbatim(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, OP_REQ_CHANGE_TO_NEXT_MAP,
                                       w_wstr("Quest03_2"))
        self.assertEqual(1, len(conn.sent))
        kind, opcode, body, _ = take_frame(bytearray(conn.sent[0]))[:4]
        self.assertEqual("game", kind)
        self.assertEqual(OP_REP_CHANGE_TO_NEXT_MAP, opcode)
        # 原样回显：地图名只有客户端的地图目录知道（D046 的同一条理由）。
        self.assertEqual(w_wstr("Quest03_2"), body)
        self.assertEqual(["Quest03_2"], conn.maps_entered)
        self.assertTrue(conn.map_change_pending)

    def test_map_request_is_never_answered_with_the_same_opcode(self):
        # ★ 服务端方向的 0x0411 是 gspEndGame（结算）。回显 = 在关卡中途
        # 把玩家踢进结算界面。
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, OP_REQ_CHANGE_TO_NEXT_MAP,
                                       w_wstr("Quest03_2"))
        opcodes = [take_frame(bytearray(f))[1] for f in conn.sent]
        self.assertNotIn(gameserver.OP_END_GAME, opcodes)
        self.assertEqual([OP_REP_CHANGE_TO_NEXT_MAP], opcodes)

    def test_non_ascii_map_names_survive_the_round_trip(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, OP_REQ_CHANGE_TO_NEXT_MAP,
                                       w_wstr("용암굴_2"))
        body = take_frame(bytearray(conn.sent[0]))[2]
        self.assertEqual("용암굴_2", parse_req_change_to_next_map(body))

    def test_malformed_map_request_is_logged_and_dropped(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, OP_REQ_CHANGE_TO_NEXT_MAP,
                                       b"\xff\xff")
        self.assertEqual([], conn.sent)
        self.assertEqual([], conn.maps_entered)
        self.assertFalse(conn.map_change_pending)
        self.assertTrue(any("0x0411 解析失败" in line for line in conn.logged))

    # ---- 0x0412 -> 0x0418 ------------------------------------------------

    def test_loading_poll_is_answered_with_an_empty_go_packet(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, OP_MAP_LOADING_DONE, b"")
        kind, opcode, body, _ = take_frame(bytearray(conn.sent[0]))[:4]
        self.assertEqual("game", kind)
        self.assertEqual(OP_MAP_CHANGE_READY, opcode)
        # 处理器 0x406302 只置一个字节的标志位，不反序列化任何字段。
        self.assertEqual(b"", body)

    def test_go_packet_is_only_sent_after_the_client_polls(self):
        # ★ 加载循环 0x47961d 是**前置**判断：标志位要是在进循环前就被置 1，
        # 客户端会跳过整段加载等待，而后台加载线程还在跑（D035 的老规矩）。
        # 所以回完 0x0417 那一刻绝不能顺手把 0x0418 一起发出去。
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, OP_REQ_CHANGE_TO_NEXT_MAP,
                                       w_wstr("Quest03_2"))
        opcodes = [take_frame(bytearray(f))[1] for f in conn.sent]
        self.assertNotIn(OP_MAP_CHANGE_READY, opcodes)

    def test_full_map_change_round_trip(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, OP_REQ_CHANGE_TO_NEXT_MAP,
                                       w_wstr("Quest03_2"))
        gameserver.Conn.on_game_packet(conn, OP_MAP_LOADING_DONE, b"")
        self.assertEqual([OP_REP_CHANGE_TO_NEXT_MAP, OP_MAP_CHANGE_READY],
                         [take_frame(bytearray(f))[1] for f in conn.sent])
        self.assertFalse(conn.map_change_pending)

    def test_repeated_polls_are_all_answered(self):
        # 客户端每 5 秒重发一次，直到 [LobbyStage+0x3fa] 变非 0。丢一发就白等。
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, OP_REQ_CHANGE_TO_NEXT_MAP,
                                       w_wstr("Quest03_2"))
        for _ in range(3):
            gameserver.Conn.on_game_packet(conn, OP_MAP_LOADING_DONE, b"")
        self.assertEqual(3, [take_frame(bytearray(f))[1]
                             for f in conn.sent].count(OP_MAP_CHANGE_READY))

    def test_several_map_changes_in_one_quest_are_all_recorded(self):
        conn = self.make_conn()
        for name in ("Quest03_2", "Quest03_3"):
            gameserver.Conn.on_game_packet(conn, OP_REQ_CHANGE_TO_NEXT_MAP,
                                           w_wstr(name))
            gameserver.Conn.on_game_packet(conn, OP_MAP_LOADING_DONE, b"")
        self.assertEqual(["Quest03_2", "Quest03_3"], conn.maps_entered)

    def test_map_state_is_cleared_when_leaving_the_room(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, OP_REQ_CHANGE_TO_NEXT_MAP,
                                       w_wstr("Quest03_2"))
        gameserver.Conn.leave_session(conn)
        self.assertEqual([], conn.maps_entered)
        self.assertFalse(conn.map_change_pending)


class ItemDropTests(unittest.TestCase):
    """掉落物：`0x0406 gcpCreateItem -> 0x0404 gspCreatedItem`（会话 17，§112）。

    在这之前服务端把客户端方向的 `0x0406` 当「位置同步」只记不回，于是
    打死怪不掉东西、通关后也没有原版那阵金币雨 —— 和 §108（血量归零不死）、
    §111（换图卡住）完全同一个形状：判定在服务端，而我们没接那条链。
    """

    class Args:
        hold_lobby = False

    def make_conn(self):
        conn = gameserver.Conn.__new__(gameserver.Conn)
        conn.args = self.Args()
        conn.sent = []
        conn.logged = []
        conn.last_packet_at = 0.0
        conn.noisy_seen = set()
        conn.last_position = None
        conn.log = conn.logged.append
        conn.send = conn.sent.append
        # send_batch() 只管「这些包合成一次 sendall」，对逐包断言透明：
        # send 被换成 append 之后 send_queue 永远是空的，退出时什么都不发。
        # 真正的合并行为由 SendBatchTests 用带假 socket 的真 Conn 覆盖。
        conn.send_lock = threading.RLock()
        conn.send_queue = None
        conn.batch_delay_ms = 0
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = 0
        conn.quest_success = False
        conn.next_item_handle = ITEM_HANDLE_BASE
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
        conn.maps_entered = []
        conn.map_change_pending = False
        conn.start_game = StartGameHandshake()
        return conn

    @staticmethod
    def request(item_id=10101, x=3924.86, y=265.46, vx=0.0, vy=0.0):
        return struct.pack("<iffffiii", item_id, x, y, vx, vy, 3, -1, -1)

    def test_drop_request_is_answered_with_created_item(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_CREATE_ITEM,
                                       self.request())
        self.assertEqual(1, len(conn.sent))
        kind, opcode, body, _ = take_frame(bytearray(conn.sent[0]))
        self.assertEqual(("game", gameserver.OP_CREATED_ITEM), (kind, opcode))
        self.assertEqual(36, len(body))

    def test_reply_carries_the_client_fields_verbatim(self):
        # 服务端手上没有任何物件数据：掉什么、掉在哪、初速多少全是客户端
        # 算好报上来的，原样发回去就对（同 D046）。
        conn = self.make_conn()
        payload = self.request(item_id=10103, x=489.5, y=398.15, vx=19.0, vy=-52.0)
        gameserver.Conn.on_game_packet(conn, gameserver.OP_CREATE_ITEM, payload)
        body = take_frame(bytearray(conn.sent[0]))[2]
        self.assertEqual(parse_create_item(payload),
                         struct.unpack_from("<iffffiii", body, 4))

    def test_each_drop_gets_a_fresh_handle(self):
        # 句柄落进 [obj+0xd0]，World::Add 拿它当 map 的 key —— 重复就会互相覆盖。
        conn = self.make_conn()
        for _ in range(5):
            gameserver.Conn.on_game_packet(conn, gameserver.OP_CREATE_ITEM,
                                           self.request())
        handles = [struct.unpack_from("<i", take_frame(bytearray(f))[2], 0)[0]
                   for f in conn.sent]
        self.assertEqual(5, len(set(handles)))
        self.assertEqual(list(range(ITEM_HANDLE_BASE, ITEM_HANDLE_BASE + 5)),
                         handles)

    def test_handles_stay_clear_of_the_levels_own_object_handles(self):
        # 关卡自己的物件句柄实测在 0x0010c9xx 一带（死亡上报包里的同一个字段）。
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_CREATE_ITEM,
                                       self.request())
        handle = struct.unpack_from("<i", take_frame(bytearray(conn.sent[0]))[2], 0)[0]
        self.assertGreater(handle, 0x00FFFFFF)
        self.assertLess(handle, 0x7FFFFFFF)

    def test_malformed_drop_request_is_logged_and_dropped(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_CREATE_ITEM,
                                       b"\x00" * 8)
        self.assertEqual([], conn.sent)
        self.assertTrue(any("解析失败" in line for line in conn.logged))

    def test_drop_position_is_remembered_for_the_respawn_fallback(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_CREATE_ITEM,
                                       self.request(x=3225.0, y=635.0))
        self.assertEqual((3225.0, 635.0), conn.last_position)

    def test_coin_shower_after_a_clear_is_answered_one_by_one(self):
        # 通关后 0x4a546e 的循环每 ~300 毫秒发一件 10101（金币×1），
        # 直到 [ctx+0x58c] 追上 [ctx+0x588]。每一件都要单独回一发。
        conn = self.make_conn()
        for _ in range(12):
            gameserver.Conn.on_game_packet(conn, gameserver.OP_CREATE_ITEM,
                                           self.request(item_id=10101))
        opcodes = [take_frame(bytearray(f))[1] for f in conn.sent]
        self.assertEqual([gameserver.OP_CREATED_ITEM] * 12, opcodes)
        self.assertEqual(12, conn.items_created)


class QuestClearFlagTests(unittest.TestCase):
    """结算界面的「完成 / 未完成」标签（会话 17，§112）。

    标签**不在** `0x0411 gspEndGame` 的 `success` 里（§99 实测发 True 也照样
    写「未完成」），而在 `0x0309 gspRepGameResult` 的**尾部数组**里：
    它落进 `[GameContext + 座位*4 + 0x184]`，结算界面 `0x4a4ba9` 判
    `vf_10c(座位) == 1` 才画「完成」那一帧。
    """

    class Args:
        hold_lobby = False
        accounts = None

    def make_conn(self):
        conn = gameserver.Conn.__new__(gameserver.Conn)
        conn.args = self.Args()
        conn.sent = []
        conn.logged = []
        conn.last_packet_at = 0.0
        conn.noisy_seen = set()
        conn.last_position = None
        conn.log = conn.logged.append
        conn.send = conn.sent.append
        # send_batch() 只管「这些包合成一次 sendall」，对逐包断言透明：
        # send 被换成 append 之后 send_queue 永远是空的，退出时什么都不发。
        # 真正的合并行为由 SendBatchTests 用带假 socket 的真 Conn 覆盖。
        conn.send_lock = threading.RLock()
        conn.send_queue = None
        conn.batch_delay_ms = 0
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = 100
        conn.quest_success = False
        conn.next_item_handle = ITEM_HANDLE_BASE
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
        conn.maps_entered = []
        conn.map_change_pending = False
        conn.account_name = None
        conn.account = {"experience": 0, "money": 0, "level": 1}
        conn.start_game = StartGameHandshake()
        return conn

    @staticmethod
    def result_tail(frame):
        """从一发 0x0309 帧里取出尾部数组。"""
        body = take_frame(bytearray(frame))[2]
        offset = 4 * (1 + GAME_RESULT_VALUE_COUNT)
        count = struct.unpack_from("<i", body, offset)[0]
        return list(struct.unpack_from(f"<{count}i", body, offset + 4))

    def test_tail_marks_only_my_seat(self):
        self.assertEqual([0, 0, GAME_RESULT_CLEARED, 0, 0, 0],
                         build_game_result_tail(2, cleared=True))

    def test_tail_is_all_zero_when_not_cleared(self):
        self.assertEqual([0] * GAME_RESULT_TAIL_COUNT,
                         build_game_result_tail(2, cleared=False))

    def test_tail_ignores_an_out_of_range_seat(self):
        # 座位有效性由客户端 0x4045f9 把关；这里只保证不越界写。
        self.assertEqual([0] * GAME_RESULT_TAIL_COUNT,
                         build_game_result_tail(9, cleared=True))

    def test_mark_quest_success_is_recorded_and_never_echoed(self):
        # ★ 服务端方向的同号 0x0417 是换图放行，回显 = 关卡结束时触发一次换图。
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_MARK_QUEST_SUCCESS,
                                       w_i32(1))
        self.assertTrue(conn.quest_success)
        self.assertEqual([], conn.sent)

    def test_malformed_mark_quest_success_falls_back_to_not_cleared(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_MARK_QUEST_SUCCESS,
                                       b"\x01")
        self.assertFalse(conn.quest_success)
        self.assertEqual([], conn.sent)

    def test_clearing_the_quest_makes_the_result_screen_say_cleared(self):
        # 实测顺序：boss 倒下 -> 43 毫秒后 0x0417 -> 30 秒金币雨 -> 0x040f。
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_MARK_QUEST_SUCCESS,
                                       w_i32(1))
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        result = next(f for f in conn.sent
                      if take_frame(bytearray(f))[1] == OP_REP_GAME_RESULT)
        self.assertEqual([GAME_RESULT_CLEARED, 0, 0, 0, 0, 0],
                         self.result_tail(result))

    def test_dying_out_still_reports_not_cleared(self):
        # 生命耗尽那条路是 EndQuest() 之后才 vf_e4(0)，赶不上结算 —— 正好也就是
        # 「未完成」。服务端不许自作主张把它当通关。
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        result = next(f for f in conn.sent
                      if take_frame(bytearray(f))[1] == OP_REP_GAME_RESULT)
        self.assertEqual([0] * GAME_RESULT_TAIL_COUNT, self.result_tail(result))

    def test_end_game_success_flag_follows_the_client_report(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_MARK_QUEST_SUCCESS,
                                       w_i32(1))
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        end = next(f for f in conn.sent
                   if take_frame(bytearray(f))[1] == gameserver.OP_END_GAME)
        body = take_frame(bytearray(end))[2]
        self.assertEqual(1, struct.unpack_from("<i", body, 4)[0])

    def test_result_packet_still_precedes_end_game(self):
        # §99 的硬约束：0x0309 解引用 GameContext，关卡一结束它就变 0。
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        opcodes = [take_frame(bytearray(f))[1] for f in conn.sent]
        self.assertLess(opcodes.index(OP_REP_GAME_RESULT),
                        opcodes.index(gameserver.OP_END_GAME))

    def test_clear_flag_is_cleared_when_leaving_the_room(self):
        # 不清的话打通一次之后每一局结算都会挂着「完成」。
        conn = self.make_conn()
        conn.quest_success = True
        conn.items_created = 7
        gameserver.Conn.leave_session(conn)
        self.assertFalse(conn.quest_success)
        self.assertEqual(0, conn.items_created)


class ItemPickupTests(unittest.TestCase):
    """拾取：`0x0407 gcpGetItem -> 0x0405`（会话 18，§115）。

    会话 17 把掉落物做出来了（`0x0406 -> 0x0404`），但走过去捡不起来。
    缺的是**下一环**：客户端碰到物件后发 `0x0407` 等放行，而
    `Character::CheckItemPickup`（`0x5154d3`）发完就把 `[item+0x2a8]` 置 1
    —— 服务端不回，那件东西这一局就再也不会被报第二次。
    """

    class Args:
        hold_lobby = False

    def make_conn(self):
        conn = gameserver.Conn.__new__(gameserver.Conn)
        conn.args = self.Args()
        conn.sent = []
        conn.logged = []
        conn.last_packet_at = 0.0
        conn.noisy_seen = set()
        conn.last_position = None
        conn.log = conn.logged.append
        conn.send = conn.sent.append
        # send_batch() 只管「这些包合成一次 sendall」，对逐包断言透明：
        # send 被换成 append 之后 send_queue 永远是空的，退出时什么都不发。
        # 真正的合并行为由 SendBatchTests 用带假 socket 的真 Conn 覆盖。
        conn.send_lock = threading.RLock()
        conn.send_queue = None
        conn.batch_delay_ms = 0
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = 0
        conn.quest_success = False
        conn.next_item_handle = ITEM_HANDLE_BASE
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
        conn.maps_entered = []
        conn.map_change_pending = False
        conn.start_game = StartGameHandshake()
        return conn

    @staticmethod
    def request(seat_id=0, handle=ITEM_HANDLE_BASE + 3):
        return struct.pack("<ii", seat_id, handle)

    def test_request_is_two_int32(self):
        # 序列化 0x558e9a 写 [esi] / [esi+4]；实测载荷 00000000 03000040。
        self.assertEqual(8, GET_ITEM_SIZE)
        self.assertEqual((0, ITEM_HANDLE_BASE + 3),
                         parse_get_item(bytes.fromhex("0000000003000040")))

    def test_request_rejects_short_payloads(self):
        with self.assertRaises(ValueError):
            parse_get_item(b"\x00" * 7)

    def test_pickup_request_is_answered(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, OP_GET_ITEM, self.request())
        self.assertEqual(1, len(conn.sent))
        kind, opcode, body, _ = take_frame(bytearray(conn.sent[0]))
        self.assertEqual(("game", OP_PICKED_ITEM), (kind, opcode))
        self.assertEqual(8, len(body))

    def test_reply_echoes_the_seat_and_handle_verbatim(self):
        # 处理器 0x551d35 拿第一个字段查 [LobbyStage+座位*4+0x1d0] 取角色、
        # 第二个字段查 World 取物件；服务端不查距离也不判归属（同 D046）。
        conn = self.make_conn()
        payload = self.request(seat_id=2, handle=ITEM_HANDLE_BASE + 0x1f)
        gameserver.Conn.on_game_packet(conn, OP_GET_ITEM, payload)
        body = take_frame(bytearray(conn.sent[0]))[2]
        self.assertEqual(payload, body)

    def test_level_owned_handles_survive_the_round_trip(self):
        # 地图上摆好的宝箱句柄实测在 0x0010c9xx 一带，不是我们分配的那一段。
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, OP_GET_ITEM,
                                       self.request(handle=0x0010C963))
        body = take_frame(bytearray(conn.sent[0]))[2]
        self.assertEqual((0, 0x0010C963), struct.unpack("<ii", body))

    def test_every_pickup_is_answered_separately(self):
        # 金币雨一局能掉一百多件，每件都要单独放行一次。
        conn = self.make_conn()
        for i in range(20):
            gameserver.Conn.on_game_packet(
                conn, OP_GET_ITEM, self.request(handle=ITEM_HANDLE_BASE + i))
        opcodes = [take_frame(bytearray(f))[1] for f in conn.sent]
        self.assertEqual([OP_PICKED_ITEM] * 20, opcodes)
        self.assertEqual(20, conn.items_picked)

    def test_malformed_pickup_request_is_logged_and_dropped(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, OP_GET_ITEM, b"\x00\x00")
        self.assertEqual([], conn.sent)
        self.assertTrue(any("解析失败" in line for line in conn.logged))

    def test_pickup_is_never_answered_on_the_same_opcode(self):
        # 服务端方向的 0x0407 在跳表 0x54e5ae 里落到默认分支（0x54e546）——
        # 没有处理器，回显只是白发一帧。放行必须走 0x0405。
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, OP_GET_ITEM, self.request())
        self.assertNotIn(OP_GET_ITEM,
                         [take_frame(bytearray(f))[1] for f in conn.sent])

    def test_drop_then_pick_up_the_same_handle(self):
        # 整条链：客户端报掉落 -> 服务端分句柄 -> 客户端踩到 -> 服务端放行。
        conn = self.make_conn()
        drop = struct.pack("<iffffiii", 10101, 700.0, 560.0, 0.0, 0.0, 3, -1, -1)
        gameserver.Conn.on_game_packet(conn, gameserver.OP_CREATE_ITEM, drop)
        handle = struct.unpack_from("<i", take_frame(bytearray(conn.sent[0]))[2], 0)[0]
        gameserver.Conn.on_game_packet(conn, OP_GET_ITEM, self.request(handle=handle))
        body = take_frame(bytearray(conn.sent[1]))[2]
        self.assertEqual((0, handle), struct.unpack("<ii", body))

    def test_pickup_counter_is_cleared_when_leaving_the_room(self):
        conn = self.make_conn()
        conn.items_picked = 9
        gameserver.Conn.leave_session(conn)
        self.assertEqual(0, conn.items_picked)

    def test_builder_layout(self):
        self.assertEqual(bytes.fromhex("0200000063c91000"),
                         build_picked_item(2, 0x0010C963))


class ResultScreenNumbersTests(unittest.TestCase):
    """结算界面的四格数值（会话 18，§116）。

    在这之前只有「完成 / 未完成」是对的，「分数 / 生命」「经验值」「金币」
    「竞技场分数」全是 0 / +0 —— 因为两个包的业务值我们一直按 D019 全填 0。

    数值来自**两个不同的包**，别混：

        分数        <- 0x0411 的业务值 5 + 6 + 7（结算表 idx5/6/7，0x4a4e40 相加）
        经验/金币/  <- 0x0309 的业务值 9 / 10 / 11
        竞技场分数     （落进 [GameContext + 座位*4 + 0x2c / 0x5c / 0x44]）
    """

    class Args:
        hold_lobby = False
        accounts = None

    def make_conn(self, score=0):
        conn = gameserver.Conn.__new__(gameserver.Conn)
        conn.args = self.Args()
        conn.sent = []
        conn.logged = []
        conn.last_packet_at = 0.0
        conn.noisy_seen = set()
        conn.last_position = None
        conn.log = conn.logged.append
        conn.send = conn.sent.append
        # send_batch() 只管「这些包合成一次 sendall」，对逐包断言透明：
        # send 被换成 append 之后 send_queue 永远是空的，退出时什么都不发。
        # 真正的合并行为由 SendBatchTests 用带假 socket 的真 Conn 覆盖。
        conn.send_lock = threading.RLock()
        conn.send_queue = None
        conn.batch_delay_ms = 0
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = score
        conn.quest_success = False
        conn.next_item_handle = ITEM_HANDLE_BASE
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
        conn.maps_entered = []
        conn.map_change_pending = False
        conn.account_name = None
        conn.account = {"experience": 0, "money": 0, "level": 1}
        conn.start_game = StartGameHandshake()
        return conn

    @staticmethod
    def result_values(frame):
        body = take_frame(bytearray(frame))[2]
        return list(struct.unpack_from(f"<{GAME_RESULT_VALUE_COUNT}i", body, 4))

    @staticmethod
    def end_game_values(frame):
        body = take_frame(bytearray(frame))[2]
        return list(struct.unpack_from(f"<{END_GAME_VALUE_COUNT}i", body, 8))

    def sent_with(self, conn, opcode):
        return next(f for f in conn.sent if take_frame(bytearray(f))[1] == opcode)

    def test_result_value_builder_only_touches_the_three_known_slots(self):
        # ⚠ §100：12 个值一次全填非 0 会让客户端 20 毫秒内主动断链。
        values = build_game_result_values(experience=11, money=22, ladder_point=33)
        self.assertEqual(GAME_RESULT_VALUE_COUNT, len(values))
        self.assertEqual(11, values[GAME_RESULT_EXPERIENCE])
        self.assertEqual(22, values[GAME_RESULT_MONEY])
        self.assertEqual(33, values[GAME_RESULT_LADDER_POINT])
        rest = [v for i, v in enumerate(values)
                if i not in (GAME_RESULT_EXPERIENCE, GAME_RESULT_MONEY,
                             GAME_RESULT_LADDER_POINT)]
        self.assertEqual([0] * 9, rest)

    def test_known_slots_land_on_the_expected_wire_offsets(self):
        # 结构体里 +0x24/+0x25 是两个挨着的 bool，所以「线序」比结构体偏移
        # 少 6 字节。这三格在**线上**是第 9/10/11 个业务值（§116）。
        payload = build_rep_game_result(
            0, values=build_game_result_values(experience=7, money=8,
                                               ladder_point=9))
        self.assertEqual(7, struct.unpack_from("<i", payload, 4 + 4 * 9)[0])
        self.assertEqual(8, struct.unpack_from("<i", payload, 4 + 4 * 10)[0])
        self.assertEqual(9, struct.unpack_from("<i", payload, 4 + 4 * 11)[0])

    def test_end_game_score_goes_into_the_first_of_the_three_parts(self):
        # 界面显示的是 idx5+idx6+idx7 之和，全放第一格即可。
        values = build_end_game_values(score=1289)
        self.assertEqual(1289, values[END_GAME_SCORE_PARTS[0]])
        self.assertEqual([0, 0], [values[i] for i in END_GAME_SCORE_PARTS[1:]])

    def test_settlement_reports_the_quest_score_as_the_screen_score(self):
        conn = self.make_conn(score=1289)
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        values = self.end_game_values(self.sent_with(conn, OP_END_GAME))
        self.assertEqual(1289, sum(values[i] for i in END_GAME_SCORE_PARTS))

    def test_settlement_reports_experience_and_money_gains(self):
        conn = self.make_conn(score=1289)
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        values = self.result_values(self.sent_with(conn, OP_REP_GAME_RESULT))
        self.assertEqual(1289, values[GAME_RESULT_EXPERIENCE])
        self.assertEqual(1289, values[GAME_RESULT_MONEY])

    def test_quest_mode_never_awards_ladder_points(self):
        conn = self.make_conn(score=1289)
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        values = self.result_values(self.sent_with(conn, OP_REP_GAME_RESULT))
        self.assertEqual(0, values[GAME_RESULT_LADDER_POINT])

    def test_a_scoreless_run_still_sends_all_zeroes(self):
        conn = self.make_conn(score=0)
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        values = self.result_values(self.sent_with(conn, OP_REP_GAME_RESULT))
        self.assertEqual([0] * GAME_RESULT_VALUE_COUNT, values)

    def test_the_clear_tail_still_rides_along(self):
        # 数值和「完成」标签在同一发包里，加了值不能把标签挤掉。
        conn = self.make_conn(score=500)
        gameserver.Conn.on_game_packet(conn, gameserver.OP_MARK_QUEST_SUCCESS,
                                       w_i32(1))
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        body = take_frame(bytearray(self.sent_with(conn, OP_REP_GAME_RESULT)))[2]
        offset = 4 * (1 + GAME_RESULT_VALUE_COUNT)
        count = struct.unpack_from("<i", body, offset)[0]
        tail = list(struct.unpack_from(f"<{count}i", body, offset + 4))
        self.assertEqual([GAME_RESULT_CLEARED, 0, 0, 0, 0, 0], tail)

    def test_experience_globals_are_still_absolute_totals(self):
        # 0x0411 的经验三件套是**绝对累计值**（§94），加了「分数」那三格
        # 不能把它们碰坏 —— 右上角数据栏的进度条全靠它们做减法。
        conn = self.make_conn(score=40)
        conn.account = {"experience": 4983, "money": 0, "level": 50}
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        values = self.end_game_values(self.sent_with(conn, OP_END_GAME))
        self.assertEqual(4983, values[gameserver.END_GAME_EXPERIENCE])
        self.assertEqual(40, values[gameserver.END_GAME_MONEY_GAINED])


class QuestDifficultyTests(unittest.TestCase):
    """难度解锁：`0x020c gspQuestReachedDifficulty`（会话 20，§118）。

    房间里选「普通 / 困难」按开始，客户端弹「无法进行的难度，请降低难度」
    （韩文原串 `0x66a758`）。准入校验 `0x468176` 的闯关分支 `0x4683ba` 拿
    `min(map[关卡 id] + 1, 4)` 当上限，而那张 map（`[0x72e35c]`）**只有
    服务端的 `0x020c` / `0x0416` 能写**。我们一个都没发过，所以它恒空、
    上限恒为 1 —— 只有「简单」能开局。
    """

    class Args:
        hold_lobby = False
        accounts = None

    class FakeStore:
        """只实现本组测试要用到的两个方法，行为跟 AccountStore 一致。"""

        def __init__(self, account):
            self.account = account
            self.calls = []

        def set_quest_cleared(self, username, quest_id, difficulty):
            self.calls.append((username, quest_id, difficulty))
            records = dict(self.account.get("quest_difficulty") or {})
            if difficulty > int(records.get(str(quest_id), 0)):
                records[str(quest_id)] = difficulty
            self.account = dict(self.account, quest_difficulty=records)
            return self.account

        def add_quest_reward(self, username, experience=0, money=0):
            return self.account

    def make_conn(self, quest=(3, 1), account=None):
        conn = gameserver.Conn.__new__(gameserver.Conn)
        conn.args = self.Args()
        conn.sent = []
        conn.logged = []
        conn.last_packet_at = 0.0
        conn.noisy_seen = set()
        conn.last_position = None
        conn.log = conn.logged.append
        conn.send = conn.sent.append
        # send_batch() 只管「这些包合成一次 sendall」，对逐包断言透明：
        # send 被换成 append 之后 send_queue 永远是空的，退出时什么都不发。
        # 真正的合并行为由 SendBatchTests 用带假 socket 的真 Conn 覆盖。
        conn.send_lock = threading.RLock()
        conn.send_queue = None
        conn.batch_delay_ms = 0
        conn.room = {"session_type": 2, "arguments": quest}
        conn.settled = False
        conn.quest_score = 10
        conn.quest_success = False
        conn.next_item_handle = ITEM_HANDLE_BASE
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
        conn.maps_entered = []
        conn.map_change_pending = False
        conn.account_name = "tester"
        conn.account = account if account is not None else {
            "experience": 0, "money": 0, "level": 1,
            "quest_difficulty": {}, "quest_unlock_all": False,
        }
        conn.accounts = self.FakeStore(conn.account)
        conn.start_game = StartGameHandshake()
        return conn

    @staticmethod
    def parse_records(frame):
        """把一发 0x020c 帧解回 `{关卡 id: 已达成难度}`。"""
        opcode, body = take_frame(bytearray(frame))[1:3]
        assert opcode == OP_QUEST_REACHED_DIFFICULTY, hex(opcode)
        count = struct.unpack_from("<i", body, 0)[0]
        pairs = struct.unpack_from(f"<{count * 2}i", body, 4)
        assert len(body) == 4 + count * 8, len(body)
        return dict(zip(pairs[0::2], pairs[1::2]))

    def sent_with(self, conn, opcode):
        return [f for f in conn.sent if take_frame(bytearray(f))[1] == opcode]

    # -- 线格式 ------------------------------------------------------------
    def test_wire_format_is_a_counted_vector_of_pairs(self):
        payload = build_quest_reached_difficulty({3: 2, 1: 1})
        self.assertEqual(4 + 2 * 8, len(payload))
        self.assertEqual(2, struct.unpack_from("<i", payload, 0)[0])
        self.assertEqual((1, 1, 3, 2),
                         struct.unpack_from("<4i", payload, 4))

    def test_an_empty_table_is_still_a_valid_packet(self):
        # 客户端处理器先清空 map 再灌，条目数 0 是合法的「全部锁死」。
        self.assertEqual(w_i32(0), build_quest_reached_difficulty({}))

    # -- 存档 -> 下发 ------------------------------------------------------
    def test_unlock_all_fills_every_quest_in_the_client_table(self):
        records = quest_difficulty_records({"quest_unlock_all": True})
        self.assertEqual(set(QUEST_ID_TABLE), set(records))
        self.assertEqual({QUEST_DIFFICULTY_MAX}, set(records.values()))

    def test_unlock_all_off_only_sends_what_was_actually_cleared(self):
        account = {"quest_unlock_all": False, "quest_difficulty": {"3": 2}}
        self.assertEqual({3: 2}, quest_difficulty_records(account))

    def test_a_dirty_save_entry_never_kills_the_whole_table(self):
        # 存档是给人手改的：一条垃圾不能让整张表发不出去。
        account = {"quest_unlock_all": False,
                   "quest_difficulty": {"3": 2, "oops": "x", "4": None}}
        self.assertEqual({3: 2}, quest_difficulty_records(account))

    def test_recorded_difficulty_is_clamped_to_the_client_ceiling(self):
        # 客户端把「已达成 + 1」夹到 4，发再大的数也没有额外含义。
        account = {"quest_unlock_all": False, "quest_difficulty": {"3": 99}}
        self.assertEqual({3: QUEST_DIFFICULTY_MAX},
                         quest_difficulty_records(account))

    # -- 登录时下发 --------------------------------------------------------
    def test_login_sends_the_difficulty_table(self):
        conn = self.make_conn()
        conn.channel_code = 0
        conn.channel_index = 0
        conn.accounts = self.FakeStore(conn.account)
        conn.accounts.resolve_game_login = lambda ticket="": ("tester", conn.account)
        conn.args.login_result = 0
        gameserver.Conn.on_game_packet(conn, 0x0100, w_wstr("tester"))
        frames = self.sent_with(conn, OP_QUEST_REACHED_DIFFICULTY)
        self.assertEqual(1, len(frames))

    def test_login_table_comes_after_the_login_reply(self):
        conn = self.make_conn(account={
            "experience": 0, "money": 0, "level": 1,
            "quest_difficulty": {"3": 2}, "quest_unlock_all": False})
        conn.channel_code = 0
        conn.channel_index = 0
        conn.accounts = self.FakeStore(conn.account)
        conn.accounts.resolve_game_login = lambda ticket="": ("tester", conn.account)
        conn.args.login_result = 0
        gameserver.Conn.on_game_packet(conn, 0x0100, w_wstr("tester"))
        opcodes = [take_frame(bytearray(f))[1] for f in conn.sent]
        self.assertLess(opcodes.index(gameserver.OP_REP_LOGIN),
                        opcodes.index(OP_QUEST_REACHED_DIFFICULTY))
        self.assertEqual({3: 2}, self.parse_records(
            self.sent_with(conn, OP_QUEST_REACHED_DIFFICULTY)[0]))

    # -- 通关解锁 ----------------------------------------------------------
    def test_clearing_a_quest_records_the_difficulty(self):
        conn = self.make_conn(quest=(3, 1))
        gameserver.Conn.on_game_packet(conn, gameserver.OP_MARK_QUEST_SUCCESS,
                                       w_i32(1))
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        self.assertEqual([("tester", 3, 1)], conn.accounts.calls)
        self.assertEqual({3: 1}, self.parse_records(
            self.sent_with(conn, OP_QUEST_REACHED_DIFFICULTY)[0]))

    def test_the_new_table_is_pushed_before_the_settlement_packets(self):
        # 结算完客户端直接回房间，那时的 map 必须已经是新的，否则玩家看到
        # 「完成」却还是选不了下一个难度。
        conn = self.make_conn(quest=(3, 1))
        gameserver.Conn.on_game_packet(conn, gameserver.OP_MARK_QUEST_SUCCESS,
                                       w_i32(1))
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        opcodes = [take_frame(bytearray(f))[1] for f in conn.sent]
        self.assertLess(opcodes.index(OP_QUEST_REACHED_DIFFICULTY),
                        opcodes.index(OP_REP_GAME_RESULT))

    def test_dying_out_unlocks_nothing(self):
        conn = self.make_conn(quest=(3, 1))
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        self.assertEqual([], conn.accounts.calls)
        self.assertEqual([], self.sent_with(conn, OP_QUEST_REACHED_DIFFICULTY))

    def test_replaying_an_easier_difficulty_does_not_lock_anything_back(self):
        conn = self.make_conn(quest=(3, 1), account={
            "experience": 0, "money": 0, "level": 1,
            "quest_difficulty": {"3": 3}, "quest_unlock_all": False})
        gameserver.Conn.on_game_packet(conn, gameserver.OP_MARK_QUEST_SUCCESS,
                                       w_i32(1))
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        self.assertEqual([], conn.accounts.calls)
        self.assertEqual([], self.sent_with(conn, OP_QUEST_REACHED_DIFFICULTY))

    def test_a_normal_room_never_records_a_quest_clear(self):
        # 描述符 type=1 的普通房参数是三个，头两个不是 (关卡 id, 难度)。
        conn = self.make_conn()
        conn.room = {"session_type": 1, "arguments": (1, 2, 3)}
        self.assertIsNone(gameserver.Conn.current_quest(conn))
        gameserver.Conn.on_game_packet(conn, gameserver.OP_MARK_QUEST_SUCCESS,
                                       w_i32(1))
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        self.assertEqual([], conn.accounts.calls)

    def test_a_half_parsed_room_does_not_crash_the_settlement(self):
        conn = self.make_conn()
        conn.room = {"session_type": 2}
        gameserver.Conn.on_game_packet(conn, gameserver.OP_MARK_QUEST_SUCCESS,
                                       w_i32(1))
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        self.assertEqual([], conn.accounts.calls)
        self.assertTrue(self.sent_with(conn, OP_REP_GAME_RESULT))

    def test_unlock_all_saves_the_clear_but_keeps_sending_the_full_table(self):
        conn = self.make_conn(quest=(3, 1), account={
            "experience": 0, "money": 0, "level": 1,
            "quest_difficulty": {}, "quest_unlock_all": True})
        gameserver.Conn.on_game_packet(conn, gameserver.OP_MARK_QUEST_SUCCESS,
                                       w_i32(1))
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        self.assertEqual([("tester", 3, 1)], conn.accounts.calls)
        records = self.parse_records(
            self.sent_with(conn, OP_QUEST_REACHED_DIFFICULTY)[0])
        self.assertEqual(set(QUEST_ID_TABLE), set(records))

    # -- 别的包没被碰坏 ----------------------------------------------------
    def test_the_settlement_packets_are_unchanged(self):
        conn = self.make_conn(quest=(3, 1))
        gameserver.Conn.on_game_packet(conn, gameserver.OP_MARK_QUEST_SUCCESS,
                                       w_i32(1))
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        opcodes = [take_frame(bytearray(f))[1] for f in conn.sent]
        self.assertLess(opcodes.index(OP_REP_GAME_RESULT),
                        opcodes.index(OP_END_GAME))


class CharacterUnlockTests(unittest.TestCase):
    """角色解锁：`0x030b gspSlotEquippedList`（会话 21，§119）。

    房间右下角的「人物选择」原本只有 3 个头像，而 `Data/ChrProps.ini` 里
    有 14 个可玩角色、`Models/Characters/` 里 15 套模型。缺的不是资源：
    客户端 `0x40713a` 数按钮时对 100..110 逐个问 `0x4070c2`，最终落到
    `0x55853c` 去背包 `vector<int32>` 里找 `(角色 id + 1) * 1000000` 起的
    那一段物品 —— 而那份背包只有 `0x030b` 能填，我们从来没发过。
    """

    class Args:
        hold_lobby = False
        accounts = None
        login_result = 0

    def make_conn(self, account=None):
        conn = gameserver.Conn.__new__(gameserver.Conn)
        conn.args = self.Args()
        conn.sent = []
        conn.logged = []
        conn.last_packet_at = 0.0
        conn.noisy_seen = set()
        conn.log = conn.logged.append
        conn.send = conn.sent.append
        # send_batch() 只管「这些包合成一次 sendall」，对逐包断言透明：
        # send 被换成 append 之后 send_queue 永远是空的，退出时什么都不发。
        # 真正的合并行为由 SendBatchTests 用带假 socket 的真 Conn 覆盖。
        conn.send_lock = threading.RLock()
        conn.send_queue = None
        conn.batch_delay_ms = 0
        conn.my_seat = 0
        conn.account_name = "tester"
        conn.account = account if account is not None else {
            "level": 1, "experience": 0, "money": 0, "character": 0,
        }
        return conn

    @staticmethod
    def parse_equipped(frame):
        """把一发 0x030b 帧解回 `(座位, 掩码三元组, [物品 id])`。"""
        opcode, body = take_frame(bytearray(frame))[1:3]
        assert opcode == OP_SLOT_EQUIPPED_LIST, hex(opcode)
        head = struct.unpack_from(f"<{1 + EQUIPPED_SLOT_MASK_COUNT}i", body, 0)
        offset = 4 * (1 + EQUIPPED_SLOT_MASK_COUNT)
        count = struct.unpack_from("<i", body, offset)[0]
        items = list(struct.unpack_from(f"<{count}i", body, offset + 4))
        assert len(body) == offset + 4 + count * 4, len(body)
        return head[0], tuple(head[1:]), items

    def sent_with(self, conn, opcode):
        return [f for f in conn.sent if take_frame(bytearray(f))[1] == opcode]

    # -- 物品 id ------------------------------------------------------------
    def test_item_ids_match_the_real_shop_entries(self):
        # ShopItem.ini 的 [Item-101400001] … [Item-111400001] 就是这 11 个。
        self.assertEqual(101400001, character_item_id(100))
        self.assertEqual(111400001, character_item_id(110))

    def test_item_id_lands_inside_the_range_the_client_accepts(self):
        # 客户端 0x55851f 认的是 [(id+1)*1e6, (id+2)*1e6) 这个左闭右开区间。
        for character_id in PREMIUM_CHARACTER_IDS:
            item_id = character_item_id(character_id)
            low = (character_id + 1) * 1000000
            self.assertTrue(low <= item_id < low + 1000000, item_id)

    def test_base_characters_are_never_shipped_as_items(self):
        # 0/1/2 在 0x55853c 里 `cmp eax,3 / jl -> return true`，白送。
        self.assertEqual((0, 1, 2), BASE_CHARACTER_IDS)
        self.assertEqual(set(), set(BASE_CHARACTER_IDS)
                         & set(owned_characters({"character_unlock_all": True})))

    # -- 存档 -> 下发 -------------------------------------------------------
    def test_unlock_all_is_the_default(self):
        self.assertTrue(character_unlock_all({}))
        self.assertEqual(list(PREMIUM_CHARACTER_IDS), owned_characters({}))
        self.assertEqual(11, len(character_item_ids({})))

    def test_unlock_all_off_only_ships_what_the_save_lists(self):
        account = {"character_unlock_all": False, "owned_characters": [102, 100]}
        self.assertEqual([100, 102], owned_characters(account))
        self.assertEqual([character_item_id(100), character_item_id(102)],
                         character_item_ids(account))

    def test_a_dirty_save_entry_never_kills_the_whole_list(self):
        account = {"character_unlock_all": False,
                   "owned_characters": [100, "oops", None, 100, 7, 999]}
        self.assertEqual([100], owned_characters(account))

    def test_unlock_all_off_with_nothing_owned_ships_an_empty_list(self):
        account = {"character_unlock_all": False, "owned_characters": []}
        self.assertEqual([], character_item_ids(account))

    # -- 线格式 -------------------------------------------------------------
    def test_wire_format_is_seat_masks_then_a_counted_vector(self):
        payload = build_slot_equipped_list(2, [101400001, 111400001])
        self.assertEqual(4 + 12 + 4 + 2 * 4, len(payload))
        self.assertEqual((2, 0, 0, 0, 2, 101400001, 111400001),
                         struct.unpack_from("<7i", payload, 0))

    def test_an_empty_list_is_still_a_valid_packet(self):
        # 空清单 = 原版「只有 3 个角色」的状态，客户端照样收得下。
        payload = build_slot_equipped_list(0, [])
        self.assertEqual(4 + 12 + 4, len(payload))
        self.assertEqual(0, struct.unpack_from("<i", payload, 16)[0])

    def test_slot_masks_default_to_all_zero(self):
        # 那 12 个字节是「哪几个装备槽被占了」，下发全 0 = 一个槽都没占。
        payload = build_slot_equipped_list(0, [])
        self.assertEqual((0,) * EQUIPPED_SLOT_MASK_COUNT,
                         struct.unpack_from("<3i", payload, 4))

    def test_an_out_of_range_seat_is_refused(self):
        with self.assertRaises(ValueError):
            build_slot_equipped_list(ROOM_SEAT_COUNT, [])
        with self.assertRaises(ValueError):
            build_slot_equipped_list(-1, [])

    def test_a_wrong_number_of_masks_is_refused(self):
        with self.assertRaises(ValueError):
            build_slot_equipped_list(0, [], slot_masks=(0, 0))

    # -- 建房时下发 ---------------------------------------------------------
    def test_creating_a_room_ships_the_equipped_list(self):
        conn = self.make_conn()
        gameserver.Conn.send_slot_equipped_list(conn)
        frames = self.sent_with(conn, OP_SLOT_EQUIPPED_LIST)
        self.assertEqual(1, len(frames))
        seat, masks, items = self.parse_equipped(frames[0])
        self.assertEqual((0, (0, 0, 0)), (seat, masks))
        self.assertEqual([character_item_id(c) for c in PREMIUM_CHARACTER_IDS],
                         items)

    def test_the_equipped_list_follows_my_seat(self):
        conn = self.make_conn()
        conn.my_seat = 3
        gameserver.Conn.send_slot_equipped_list(conn)
        self.assertEqual(3, self.parse_equipped(conn.sent[0])[0])

    def test_the_equipped_list_comes_after_the_seat_snapshot(self):
        # ★ 顺序是硬约束：持有判定 0x4070da 第一步查「我的座位已占用吗」，
        # 那个标记只有 0x0300 会写。反过来发，11 个角色一个都出不来。
        conn = self.make_conn()
        conn.room = None
        conn.start_game = StartGameHandshake()
        conn.accounts = None
        gameserver.Conn.on_game_packet(conn, 0x0201, b"")
        opcodes = [take_frame(bytearray(f))[1] for f in conn.sent]
        self.assertIn(OP_SESSION_MEMBERS, opcodes)
        self.assertIn(OP_SLOT_EQUIPPED_LIST, opcodes)
        self.assertLess(opcodes.index(OP_SESSION_MEMBERS),
                        opcodes.index(OP_SLOT_EQUIPPED_LIST))

    def test_the_room_packets_are_otherwise_unchanged(self):
        conn = self.make_conn()
        conn.room = None
        conn.start_game = StartGameHandshake()
        conn.accounts = None
        gameserver.Conn.on_game_packet(conn, 0x0201, b"")
        opcodes = [take_frame(bytearray(f))[1] for f in conn.sent]
        self.assertEqual([0x0201, OP_SESSION_MEMBERS, OP_SLOT_EQUIPPED_LIST],
                         opcodes)

    # -- 控制通道 -----------------------------------------------------------
    def test_control_command_can_ship_an_explicit_list(self):
        conn = self.make_conn()
        saved = list(gameserver._conns)
        gameserver._conns[:] = [conn]
        self.addCleanup(lambda: gameserver._conns.__setitem__(slice(None), saved))
        reply = gameserver.handle_control_command("equipped 1 101400001")
        self.assertTrue(reply.startswith("ok"), reply)
        self.assertEqual((1, (0, 0, 0), [101400001]),
                         self.parse_equipped(conn.sent[0]))

    def test_returning_from_the_result_screen_reships_the_list(self):
        # 防御性重发：`0x406e4e`（把座位清单重建成空的）有三个调用点在切
        # stage 的路上，没有逐条读到底。实测这条路清单没被清，但整份替换是
        # 幂等的，漏了的代价是「人物选择」缩回 3 个头像。
        conn = self.make_conn()
        conn.quest_score = 0
        conn.quest_success = False
        conn.settled = False
        conn.items_created = 0
        conn.items_picked = 0
        conn.next_item_handle = ITEM_HANDLE_BASE
        conn.maps_entered = []
        conn.map_change_pending = False
        conn.room = None
        conn.start_game = StartGameHandshake()
        conn.accounts = None
        gameserver.Conn.leave_game_result(conn)
        opcodes = [take_frame(bytearray(f))[1] for f in conn.sent]
        self.assertIn(OP_SLOT_EQUIPPED_LIST, opcodes)
        self.assertLess(opcodes.index(gameserver.OP_LOADING_DONE),
                        opcodes.index(OP_SLOT_EQUIPPED_LIST))
        seat, _, items = self.parse_equipped(
            self.sent_with(conn, OP_SLOT_EQUIPPED_LIST)[0])
        self.assertEqual(0, seat)
        self.assertEqual(len(PREMIUM_CHARACTER_IDS), len(items))

    def test_control_command_can_empty_the_list(self):
        # 复现「只有 3 个角色」的原始状态用。
        conn = self.make_conn()
        saved = list(gameserver._conns)
        gameserver._conns[:] = [conn]
        self.addCleanup(lambda: gameserver._conns.__setitem__(slice(None), saved))
        gameserver.handle_control_command("equipped 0")
        self.assertEqual([], self.parse_equipped(conn.sent[0])[2])


class SendBatchTests(unittest.TestCase):
    """`send_batch()`：把一组包合成**一次** `sendall`（会话 22，§120）。

    这不是性能优化。客户端每帧 recv 一次、把收到的包全分发完，
    `ChangeStage` 只是记下工厂函数，`RoomStage` 构造函数下一帧才跑，
    它**一次性**建出「人物选择」的头像按钮、之后不会重建。
    所以 `0x0201`（触发换 stage）和 `0x030b`（角色清单）一旦被客户端的
    recv 切成两帧，房间就用空清单建 UI —— 头像缩回 3 个，这一局回不来。

    这里用真的 `Conn.send` / `Conn.send_batch` + 一个记账用的假 socket，
    因为要测的正好是「落到 socket 上的是几次写、字节是什么」。
    """

    class FakeSocket:
        def __init__(self):
            self.writes = []

        def sendall(self, data):
            self.writes.append(bytes(data))

    class Args:
        hold_lobby = False
        accounts = None
        login_result = 0
        room_burst_delay = 0

    def make_conn(self):
        conn = gameserver.Conn.__new__(gameserver.Conn)
        conn.args = self.Args()
        conn.sock = self.FakeSocket()
        conn.cout = SimpleCipher.server_to_client()
        conn.send_lock = threading.RLock()
        conn.send_queue = None
        conn.batch_delay_ms = 0
        conn.logged = []
        conn.log = conn.logged.append
        conn.vlog = lambda _msg: None
        conn.last_packet_at = 0.0
        conn.noisy_seen = set()
        conn.my_seat = 0
        conn.room = None
        conn.start_game = StartGameHandshake()
        conn.accounts = None
        conn.account_name = "tester"
        conn.account = {"level": 1, "experience": 0, "money": 0, "character": 0}
        return conn

    @staticmethod
    def frames_of(blob):
        """把一段明文里的帧全部取出来，返回 opcode 列表。"""
        opcodes, rest = [], bytearray(blob)
        while rest:
            _, opcode, _, consumed = take_frame(rest)
            opcodes.append(opcode)
            del rest[:consumed]
        return opcodes

    def decrypted(self, conn):
        """把假 socket 上的所有写按顺序解回明文。"""
        cin = SimpleCipher.server_to_client()
        return cin.decrypt(b"".join(conn.sock.writes))

    def test_a_batch_leaves_the_socket_as_exactly_one_write(self):
        conn = self.make_conn()
        with gameserver.Conn.send_batch(conn):
            conn.send(build_game(0x0200, build_rep_list_session()))
            conn.send(build_game(0x0201, build_rep_create_session(1)))
        self.assertEqual(1, len(conn.sock.writes))
        self.assertEqual([0x0200, 0x0201], self.frames_of(self.decrypted(conn)))

    def test_batching_does_not_change_a_single_byte_on_the_wire(self):
        # SimpleCipher 是逐字节推进的流密码，encrypt(a+b) == encrypt(a)+encrypt(b)。
        # 合并只改变「写了几次」，不改变写出去的字节 —— 这条必须钉死，
        # 否则客户端的解密流会从这里开始永久错位。
        packets = [build_game(0x0200, build_rep_list_session()),
                   build_game(0x0201, build_rep_create_session(1)),
                   build_game(OP_SLOT_EQUIPPED_LIST,
                              build_slot_equipped_list(0, [101400001]))]
        one_by_one = self.make_conn()
        for packet in packets:
            one_by_one.send(packet)
        batched = self.make_conn()
        with gameserver.Conn.send_batch(batched):
            for packet in packets:
                batched.send(packet)
        self.assertEqual(3, len(one_by_one.sock.writes))
        self.assertEqual(1, len(batched.sock.writes))
        self.assertEqual(b"".join(one_by_one.sock.writes),
                         b"".join(batched.sock.writes))

    def test_an_empty_batch_writes_nothing(self):
        conn = self.make_conn()
        with gameserver.Conn.send_batch(conn):
            pass
        self.assertEqual([], conn.sock.writes)

    def test_the_batch_is_flushed_even_if_the_block_raises(self):
        # 中途抛异常也要把已经攒下的字节发出去：漏发比早发危险得多
        # （客户端会一直等那个应答）。
        conn = self.make_conn()
        with self.assertRaises(RuntimeError):
            with gameserver.Conn.send_batch(conn):
                conn.send(build_game(0x0200, build_rep_list_session()))
                raise RuntimeError("boom")
        self.assertEqual(1, len(conn.sock.writes))
        self.assertEqual([0x0200], self.frames_of(self.decrypted(conn)))

    def test_a_nested_batch_does_not_flush_early(self):
        conn = self.make_conn()
        with gameserver.Conn.send_batch(conn):
            conn.send(build_game(0x0200, build_rep_list_session()))
            with gameserver.Conn.send_batch(conn):
                conn.send(build_game(0x0201, build_rep_create_session(1)))
            self.assertEqual([], conn.sock.writes)
        self.assertEqual(1, len(conn.sock.writes))
        self.assertEqual([0x0200, 0x0201], self.frames_of(self.decrypted(conn)))

    def test_a_plain_send_outside_a_batch_still_goes_out_immediately(self):
        conn = self.make_conn()
        conn.send(build_game(0x0200, build_rep_list_session()))
        self.assertEqual(1, len(conn.sock.writes))

    def test_room_burst_delay_puts_the_broken_behaviour_back(self):
        # `--room-burst-delay` 是复现开关（同 D047）：退回一包一次 sendall，
        # 客户端的 recv 就又能插进缝里了。字节仍然一个不差。
        conn = self.make_conn()
        conn.args.room_burst_delay = 1
        with gameserver.Conn.send_batch(conn):
            conn.send(build_game(0x0200, build_rep_list_session()))
            conn.send(build_game(0x0201, build_rep_create_session(1)))
        self.assertEqual(2, len(conn.sock.writes))
        self.assertEqual([0x0200, 0x0201], self.frames_of(self.decrypted(conn)))

    # -- 真正要保住的两条链 -------------------------------------------------
    def test_the_whole_room_burst_goes_out_in_one_write(self):
        # ★ 这就是「进房间偶尔只剩 3 个角色」的修法本体。
        conn = self.make_conn()
        request = (w_wstr("想和做朋友吗?") + w_wstr("") + w_wstr("")
                   + w_i32(0) + w_i32(2) + w_i32(3) + w_i32(1))
        gameserver.Conn.on_game_packet(conn, 0x0201, request)
        self.assertEqual(1, len(conn.sock.writes))
        self.assertEqual([OP_UPDATE_SESSION, 0x0201,
                          OP_SESSION_MEMBERS, OP_SLOT_EQUIPPED_LIST],
                         self.frames_of(self.decrypted(conn)))

    def test_returning_to_the_room_also_goes_out_in_one_write(self):
        # 结算看完回房间同样会重建 RoomStage，同一个竞态。
        conn = self.make_conn()
        conn.quest_score = 0
        conn.quest_success = False
        conn.settled = False
        conn.items_created = 0
        conn.items_picked = 0
        conn.next_item_handle = ITEM_HANDLE_BASE
        conn.maps_entered = []
        conn.map_change_pending = False
        gameserver.Conn.leave_game_result(conn)
        self.assertEqual(1, len(conn.sock.writes))
        opcodes = self.frames_of(self.decrypted(conn))
        self.assertEqual(gameserver.OP_LOADING_DONE, opcodes[0])
        self.assertEqual(OP_SLOT_EQUIPPED_LIST, opcodes[-1])


if __name__ == "__main__":
    unittest.main()
