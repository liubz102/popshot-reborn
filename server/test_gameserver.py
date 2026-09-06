#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import random
import struct
import tempfile
import threading
import time
import unittest

from gameserver import (
    ROOM_SEAT_COUNT,
    SEAT_ACTION_CHANGE_CHARACTER,
    SEAT_ACTION_JOIN,
    SEAT_ACTION_LEAVE,
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
import account_store
from account_store import (BASE_CHARACTER_IDS, EXPERIENCE_STEP, LEVEL_MAX,
                           PREMIUM_CHARACTER_IDS, QUEST_DIFFICULTY_MAX,
                           QUEST_ID_TABLE, AccountStore, character_item_id,
                           character_item_ids, character_unlock_all,
                           experience_bounds, experience_for_level,
                           level_for_experience,
                           owned_characters, quest_cleared_difficulty,
                           quest_difficulty_records)
import gameserver
import shop
import shopcfg
from test_shop import (config_dir, parse_rep_composition_list,
                       parse_rep_equipped_list, parse_rep_inventory,
                       parse_rep_item_info, parse_shop_item_list,
                       recipe_config, shop_config)


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
        # ★ 种子现在是**每局重取一次**的（§228），注入一个常量发号器才好断言。
        handshake = StartGameHandshake(seed_source=lambda: 1234)

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
                # ★ 第 4 个字段 = 随机地图开关（§228）；第 5 个是栈垃圾。
                "random_map": True,
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
                                     team=0, character_id=3,
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
        slot = dict(occupied=True, nickname="测试", team=0,
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

    def test_leave_action_is_the_one_that_destroys_the_seat_model(self):
        # action 1/2 走 0x406676 -> 0x405f8f 销毁座位的角色对象 ——
        # 有人离开/被踢时**正需要**它，否则模型留在房间里（§147）。
        self.assertIn(SEAT_ACTION_LEAVE, (1, 2))
        payload = build_session_member_update(1, SEAT_ACTION_LEAVE,
                                              occupied=False)
        self.assertEqual(bytes([SEAT_ACTION_LEAVE]) + struct.pack("<i", 1),
                         payload[:5])
        self.assertEqual(build_session_slot(occupied=False), payload[5:])

    def test_session_member_update_rejects_unknown_actions(self):
        # 跳表 0x4064f7 只认 0~4，别的码客户端直接丢包 —— 发了等于没发。
        for action in (5, 9, 255):
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
        # ★ 第 1 格是**座位**、第 4 格是**角色 id**（V0.3 §33）。
        payload = build_respawn_character(seat=2, x=100, y=-50,
                                          character_id=7)
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
        for experience in (0, 1, 99, 100, 101, 250, 999, 21000, 176999):
            start, nxt = experience_bounds(experience)
            self.assertLessEqual(start, experience)
            self.assertLess(experience, nxt)
            # 曲线是二次的，每一级的跨度 = EXPERIENCE_STEP × 当前等级。
            level = level_for_experience(experience)
            self.assertEqual(EXPERIENCE_STEP * level, nxt - start)

    def test_experience_bounds_never_divide_by_zero_at_the_cap(self):
        # 满级之后客户端仍然要算 (总经验-起点)/(下一级-起点)，分母不能是 0。
        for experience in (experience_for_level(LEVEL_MAX), 10 ** 9):
            start, nxt = experience_bounds(experience)
            self.assertEqual(experience_for_level(LEVEL_MAX), start)
            self.assertGreater(nxt, start)

    def test_level_tracks_the_experience_curve(self):
        # 升一级要 EXPERIENCE_STEP × 当前等级，累计 50·L·(L-1)。
        self.assertEqual(1, level_for_experience(0))
        self.assertEqual(1, level_for_experience(EXPERIENCE_STEP - 1))
        self.assertEqual(2, level_for_experience(EXPERIENCE_STEP))
        self.assertEqual(2, level_for_experience(EXPERIENCE_STEP * 2))
        self.assertEqual(3, level_for_experience(EXPERIENCE_STEP * 3))
        self.assertEqual(10, level_for_experience(4500))
        self.assertEqual(21, level_for_experience(21000))
        # 旧线性曲线下 300 级的老存档，按新曲线是 24 级（§229 的迁移口径）。
        self.assertEqual(24, level_for_experience(29900))

    def test_level_is_clamped_to_the_badge_sprite_count(self):
        # LevelMark.smf 只有 60 帧，超过就会取到别的图的像素。
        self.assertEqual(LEVEL_MAX, level_for_experience(
            experience_for_level(LEVEL_MAX)))
        self.assertEqual(LEVEL_MAX, level_for_experience(10 ** 9))

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
        # 二次曲线：250 点落在 2 级（本级 100 起、下一级 300）。
        account = {"level": 2, "experience": 250, "money": 64}
        payload = build_rep_money_for(account)
        money, experience, start, nxt = struct.unpack_from("<4i", payload, 4)
        values = build_end_game_values(experience=experience,
                                       next_level_exp=nxt,
                                       level_start_exp=start)
        self.assertEqual(64, money)
        self.assertEqual((250, 100, 300), (values[1], values[3], values[2]))
        # ★ D22 起下发的就是真实等级（以前这里被抬到 4）。
        self.assertEqual(2, struct.unpack_from("<H", payload, 20)[0])

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
        conn.addr = ("::ffff:127.0.0.1", 40000)
        conn.connected_at = time.monotonic()
        conn.online = lambda _msg: None
        conn.online_debug = lambda _msg: None
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
        conn.room = {"session_type": 2}
        conn.settled = True
        conn.quest_score = 64
        conn.quest_success = True
        conn.solo_quest = gameserver.RoomQuest()
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
            self.inventory_sent = 0
            self.shop_equipped_sent = 0
            self.my_seat = 0
            self.quest_score = 64
            self.last_position = (3225.0, 635.0)
            self.room = {"session_type": 2, "arguments": (3, 1)}
            self.start_game = StartGameHandshake()
            # 控制通道的几条命令会走到 `Conn.quest_state()` / `current_quest()`，
            # 两者第一件事都是问「我在哪个大厅房间里」。控制通道推的是单人
            # 协议试探，答案固定是「不在任何房间」。
            self.solo_quest = gameserver.RoomQuest()

        def lobby_room(self):
            return None

        def quest_state(self):
            return self.solo_quest

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

        def send_rep_inventory(self, reason=""):
            self.inventory_sent += 1

        def send_rep_equipped_list(self, reason=""):
            self.shop_equipped_sent += 1

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
        # ★ 第 4 格默认 -1 =「角色维持现状」（`0x493208`）—— 手搓一发调试包
        #   不该顺手把人换成别的角色（V0.3 §33）。
        gameserver.handle_control_command("respawn")
        kind, opcode, payload, _ = self.only_frame()
        self.assertEqual(("game", OP_RESPAWN_CHARACTER), (kind, opcode))
        self.assertEqual((0, 3225, 635, -1), struct.unpack("<4i", payload))

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
        # 数据栏（0x0600）、难度解锁表（0x020c）、角色解锁表（0x030b）
        # 和商店三件套（0x0501 / 0x0601 / 0x0604）都要跟着刷。
        reply = gameserver.handle_control_command("sync-account")
        self.assertTrue(reply.startswith("ok"), reply)
        self.assertEqual((1, 1, 1, 1, 1, 1),
                         (self.conn.reloaded, self.conn.money_sent,
                          self.conn.difficulty_sent, self.conn.equipped_sent,
                          self.conn.inventory_sent,
                          self.conn.shop_equipped_sent))

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
        self.assertIn("level=3", reply)  # 存档里的真实等级（D22 起不再套兼容下限）
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
        conn.addr = ("::ffff:127.0.0.1", 40000)
        conn.connected_at = time.monotonic()
        conn.online = lambda _msg: None
        conn.online_debug = lambda _msg: None
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
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = 0
        conn.quest_success = False
        conn.solo_quest = gameserver.RoomQuest()
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
        conn.addr = ("::ffff:127.0.0.1", 40000)
        conn.connected_at = time.monotonic()
        conn.online = lambda _msg: None
        conn.online_debug = lambda _msg: None
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
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = 0
        conn.quest_success = False
        conn.solo_quest = gameserver.RoomQuest()
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
        # ★ 死亡次数从 0 报起：bug调查/8 之后下发值取自服务端权威计数，
        #   头一次广播就是 1，不再跟着客户端报的「之前死过几次」跳。
        conn = self.make_conn()
        payload = self.hp_zero_payload(handle=0x0010C8FB, seat=3, arg=0xFF,
                                       deaths=0, x=-1500.5, y=820.25)
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
        """★ `+0x00` 是**座位**、`+0x0c` 是**角色 id**（V0.3 §33 的勘误）。

        `+0x0c` 以前被当成「重生点索引」，回显时填了别人的值 —— 客户端
        `0x4931c2` 拿它和 `[char+0x2b0]` 比，不一样就**换角色模型**并往
        聊天框播一行「…캐릭터로 변경하였습니다」。用户实机报的「bot 每次
        复活都换一个角色」就是这么来的。
        """
        info = parse_respawn_request(struct.pack("<iiii", 3, -1500, 820, 2))
        self.assertEqual({"seat": 3, "x": -1500, "y": 820,
                          "character_id": 2}, info)

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
        conn.addr = ("::ffff:127.0.0.1", 40000)
        conn.connected_at = time.monotonic()
        conn.online = lambda _msg: None
        conn.online_debug = lambda _msg: None
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
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = 0
        conn.quest_success = False
        conn.solo_quest = gameserver.RoomQuest()
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
        conn.addr = ("::ffff:127.0.0.1", 40000)
        conn.connected_at = time.monotonic()
        conn.online = lambda _msg: None
        conn.online_debug = lambda _msg: None
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
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = 0
        conn.quest_success = False
        conn.solo_quest = gameserver.RoomQuest()
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
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


class QuestMapDifficultyTests(unittest.TestCase):
    """★★★ 闯关房这一刻在哪张图上 —— **要带难度后缀**（§140 / D100）。

    客户端报上来的（建房参数、`0x0411`）永远是不带后缀的基名
    `Quest03_1`，而它自己加载的是 `Quest03_1#Easy.map`。服务端不补这一手
    就恒退到 `#Normal`，手上那张图和真人屏幕上那张**不是一张** ——
    用户 2026-08-30 实机报的「bot 从树里穿过去 / 在空地上腾空走路」。
    """

    class Quest:
        def __init__(self, entered=()):
            self.maps_entered = list(entered)

    class Room:
        def __init__(self, session_type, arguments, map_name, quest=None):
            self.session_type = session_type
            self.arguments = arguments
            self.map_name = map_name
            self.quest = quest

    def setUp(self):
        import mapdata
        store = mapdata._Store(data_dir="__不存在的目录__")
        store._index = {
            "maps": {"Quest03_1#Easy": {}, "Quest03_1#Normal": {},
                     "Quest03_1#Hard": {}, "Quest03_1#Extreme": {},
                     "Quest03_3#Easy": {}, "Quest03_3#Normal": {},
                     "Quest03_2": {}, "Megatron_b": {}},
            "bases": {},
        }
        self._saved_store = mapdata.STORE
        mapdata.STORE = store
        self.addCleanup(setattr, mapdata, "STORE", self._saved_store)

    def test_the_starting_map_carries_the_rooms_difficulty(self):
        for level, suffix in ((1, "#Easy"), (2, "#Normal"),
                              (3, "#Hard"), (4, "#Extreme")):
            room = self.Room(2, (3, level), "Quest03_1")
            self.assertEqual("Quest03_1" + suffix,
                             gameserver.current_map_name(room))

    def test_maps_entered_mid_quest_carry_it_too(self):
        # 换图那一发 `0x0411` 报的也是基名 —— 同样要补。
        room = self.Room(2, (3, 1), "Quest03_1",
                         self.Quest(["Quest03_2", "Quest03_3"]))
        self.assertEqual("Quest03_3#Easy", gameserver.current_map_name(room))

    def test_a_map_without_variants_is_left_alone(self):
        room = self.Room(2, (3, 1), "Quest03_1", self.Quest(["Quest03_2"]))
        self.assertEqual("Quest03_2", gameserver.current_map_name(room))

    def test_pvp_rooms_are_untouched(self):
        room = self.Room(1, (1, 0, 1), "Megatron_b:NewPvp")
        self.assertIsNone(gameserver.room_difficulty(room))
        self.assertEqual("Megatron_b:NewPvp", gameserver.current_map_name(room))

    def test_a_quest_room_without_arguments_is_not_fatal(self):
        room = self.Room(2, (), "Quest03_1")
        self.assertIsNone(gameserver.room_difficulty(room))
        self.assertEqual("Quest03_1", gameserver.current_map_name(room))


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
        conn.addr = ("::ffff:127.0.0.1", 40000)
        conn.connected_at = time.monotonic()
        conn.online = lambda _msg: None
        conn.online_debug = lambda _msg: None
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
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = 0
        conn.quest_success = False
        conn.solo_quest = gameserver.RoomQuest()
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
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
        conn.addr = ("::ffff:127.0.0.1", 40000)
        conn.connected_at = time.monotonic()
        conn.online = lambda _msg: None
        conn.online_debug = lambda _msg: None
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
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = 100
        conn.quest_success = False
        conn.solo_quest = gameserver.RoomQuest()
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
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
        conn.addr = ("::ffff:127.0.0.1", 40000)
        conn.connected_at = time.monotonic()
        conn.online = lambda _msg: None
        conn.online_debug = lambda _msg: None
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
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = 0
        conn.quest_success = False
        conn.solo_quest = gameserver.RoomQuest()
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
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
        conn.addr = ("::ffff:127.0.0.1", 40000)
        conn.connected_at = time.monotonic()
        conn.online = lambda _msg: None
        conn.online_debug = lambda _msg: None
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
        conn.room = {"session_type": 2}
        conn.settled = False
        conn.quest_score = score
        conn.quest_success = False
        conn.solo_quest = gameserver.RoomQuest()
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
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
        # ★★ 分数 / 经验 / 金币是**三件事**（§227）。以前这三个数一模一样
        #    （都等于关卡分），玩家一眼就看出来不对。
        conn = self.make_conn(score=1289)
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        values = self.result_values(self.sent_with(conn, OP_REP_GAME_RESULT))
        end_values = self.end_game_values(self.sent_with(conn, OP_END_GAME))
        screen_score = sum(end_values[i] for i in END_GAME_SCORE_PARTS)
        # `conn.room` 没带 arguments -> `current_quest()` 取不到 -> 按 1 级关卡、
        # 难度 1 算；`quest_success` 是 False，所以基础奖励打 QUEST_FAILED_RATIO 折。
        want_exp, want_money = gameserver.quest_reward(1, 1, 1289, False)
        self.assertEqual(want_exp, values[GAME_RESULT_EXPERIENCE])
        self.assertEqual(want_money, values[GAME_RESULT_MONEY])
        self.assertEqual(1289, screen_score)
        # 三个数互不相同 —— 这就是这条用例存在的理由。
        self.assertEqual(3, len({want_exp, want_money, screen_score}))

    def test_quest_mode_never_awards_ladder_points(self):
        conn = self.make_conn(score=1289)
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        values = self.result_values(self.sent_with(conn, OP_REP_GAME_RESULT))
        self.assertEqual(0, values[GAME_RESULT_LADDER_POINT])

    def test_a_scoreless_run_only_touches_the_three_known_slots(self):
        # ⚠ §100：除了那三格，其余 9 个业务值**必须**保持 0，
        #    12 个值一次全填非 0 会让客户端 20 毫秒内主动断链。
        #    ★ 0 分不再等于 0 奖励：没通关也给基础奖励的 QUEST_FAILED_RATIO
        #    那一份（§227），打了半天不能一无所获。
        conn = self.make_conn(score=0)
        gameserver.Conn.on_game_packet(conn, gameserver.OP_END_QUEST, b"")
        values = self.result_values(self.sent_with(conn, OP_REP_GAME_RESULT))
        want_exp, want_money = gameserver.quest_reward(1, 1, 0, False)
        self.assertEqual(want_exp, values[GAME_RESULT_EXPERIENCE])
        self.assertEqual(want_money, values[GAME_RESULT_MONEY])
        self.assertEqual(0, values[GAME_RESULT_LADDER_POINT])
        rest = [v for i, v in enumerate(values)
                if i not in (GAME_RESULT_EXPERIENCE, GAME_RESULT_MONEY,
                             GAME_RESULT_LADDER_POINT)]
        self.assertEqual([0] * 9, rest)

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
        # 金币那一格发的是**本局所得**（不是分数，§227）。
        self.assertEqual(gameserver.quest_reward(1, 1, 40, False)[1],
                         values[gameserver.END_GAME_MONEY_GAINED])


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
        """只实现本组测试要用到的那几个方法，行为跟 AccountStore 一致。

        ★ 结算路径上每多一个存档调用，这里就要跟着补一个 —— 否则整组
        `AttributeError`。`test_battle.FakeAccounts` 是同一个道理。
        """

        def __init__(self, account):
            self.account = account
            self.calls = []              # set_quest_cleared 的调用记录
            self.material_calls = []     # add_materials 的调用记录

        def set_quest_cleared(self, username, quest_id, difficulty):
            self.calls.append((username, quest_id, difficulty))
            records = dict(self.account.get("quest_difficulty") or {})
            if difficulty > int(records.get(str(quest_id), 0)):
                records[str(quest_id)] = difficulty
            self.account = dict(self.account, quest_difficulty=records)
            return self.account

        def add_quest_reward(self, username, experience=0, money=0):
            return self.account

        def add_materials(self, username, materials):
            # ★ 单独一个列表：`calls` 被断言成「`set_quest_cleared` 的调用记录」
            #   （`assertEqual([], conn.accounts.calls)`），混进来会当场炸。
            self.material_calls.append((username, dict(materials)))
            # 二元组，和 `AccountStore.add_materials` 一样（它「跳过不抛」，D12）。
            return self.account, []

        def get_account(self, username):
            return username, self.account

    class FakeTickets:
        """把任意票据认成同一个账号。真实实现见 `server/tickets.py`。"""

        def __init__(self, username="tester"):
            self.username = username
            self.bound = []

        def resolve(self, ticket):
            return self.username if ticket else None

        def bind(self, ticket):
            """登录成功后游戏服会调它（重连凭证，§171 / D096）。"""
            self.bound.append(ticket)
            return bool(ticket)

    def make_conn(self, quest=(3, 1), account=None):
        conn = gameserver.Conn.__new__(gameserver.Conn)
        conn.addr = ("::ffff:127.0.0.1", 40000)
        conn.connected_at = time.monotonic()
        conn.online = lambda _msg: None
        conn.online_debug = lambda _msg: None
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
        conn.room = {"session_type": 2, "arguments": quest}
        conn.settled = False
        conn.quest_score = 10
        conn.quest_success = False
        conn.solo_quest = gameserver.RoomQuest()
        conn.items_created = 0
        conn.items_picked = 0
        conn.my_seat = 0
        conn.account_name = "tester"
        conn.account = account if account is not None else {
            "experience": 0, "money": 0, "level": 1,
            "quest_difficulty": {}, "quest_unlock_all": False,
        }
        conn.accounts = self.FakeStore(conn.account)
        conn.tickets = self.FakeTickets(conn.account_name)
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


class PlayerNameTests(unittest.TestCase):
    """「你自己叫什么」：`0x0102`（§222）。

    用户实机报的：**建房时默认房间名是「与一起游戏吧」，昵称没拼进去。**

    格式串是 `Chinese.ini` 的 `与%s一起游戏吧!`，那个 `%s`（`0x43616f`）
    取的是全局 wstring `0x72e328`。全镜像**只有**收 0x0102 的
    `ServerConnection` 虚表槽 17（`0x54f23a`）写它 —— 我们从来没发过，
    所以它恒是空串。
    """

    Args = QuestDifficultyTests.Args
    FakeStore = QuestDifficultyTests.FakeStore
    FakeTickets = QuestDifficultyTests.FakeTickets

    def make_conn(self, account=None):
        conn = QuestDifficultyTests.make_conn(self, account=account)
        conn.channel_code = 0
        conn.channel_index = 0
        conn.args.login_result = 0
        return conn

    def login(self, account=None):
        conn = self.make_conn(account=account)
        gameserver.Conn.on_game_packet(conn, 0x0100, w_wstr("tester"))
        return conn

    @staticmethod
    def names(conn):
        """收到的每一发 0x0102 解回昵称。"""
        out = []
        for frame in conn.sent:
            opcode, body = take_frame(bytearray(frame))[1:3]
            if opcode == gameserver.OP_SET_PLAYER_NAME:
                out.append(gameserver.Reader(body).wstr())
        return out

    # -- 线格式 ------------------------------------------------------------
    def test_the_payload_is_just_one_wstring(self):
        # `0x54f23a` 只调一次 `0x5d5b3a`（u16 字符数 + UTF-16LE），没有别的字段。
        self.assertEqual(w_wstr("阿狗"),
                         gameserver.build_set_player_name("阿狗"))

    def test_an_empty_nickname_is_still_a_valid_packet(self):
        self.assertEqual(w_wstr(""), gameserver.build_set_player_name(None))

    # -- 登录时下发 --------------------------------------------------------
    def test_login_sends_the_nickname(self):
        conn = self.login(account={
            "experience": 0, "money": 0, "level": 1,
            "display_name": "阿狗"})
        self.assertEqual(["阿狗"], self.names(conn))

    def test_the_nickname_falls_back_to_the_account_name(self):
        # 存档没填昵称时，座位里显示的也是用户名，两处必须同一个口径。
        conn = self.login(account={"experience": 0, "money": 0, "level": 1})
        self.assertEqual(["tester"], self.names(conn))

    def test_it_comes_after_the_login_reply(self):
        # `0x54f23a` 要写 `[0x72e2a4]+0x14`，那个全局在登录应答那一路才被填。
        conn = self.login()
        opcodes = [take_frame(bytearray(f))[1] for f in conn.sent]
        self.assertLess(opcodes.index(gameserver.OP_REP_LOGIN),
                        opcodes.index(gameserver.OP_SET_PLAYER_NAME))

    def test_a_rejected_login_gets_no_nickname(self):
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, 0x0100, w_wstr(""))
        self.assertEqual([], self.names(conn))


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
        conn.addr = ("::ffff:127.0.0.1", 40000)
        conn.connected_at = time.monotonic()
        conn.online = lambda _msg: None
        conn.online_debug = lambda _msg: None
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
        conn.solo_quest = gameserver.RoomQuest()
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

    # -- 装备也走这一发（V0.3商店 M3）--------------------------------------
    def test_worn_gear_rides_along_with_the_character_items(self):
        # ★ `0x030b` 是战斗加成的**唯一**来源（V0.3商店 §1）：处理器写的
        #   `[GameSession+0x250+seat*4]` 正是 `GetEquipBonus` 读的那一格。
        conn = self.make_conn(account={
            "level": 1, "experience": 0, "money": 0, "character": 0,
            "inventory": {"1120041": {"count": 1}, "1010015": {"count": 1}},
            "equipped": [1010015, 1120041],
        })
        gameserver.Conn.send_slot_equipped_list(conn)
        _seat, _masks, items = self.parse_equipped(conn.sent[0])
        expected = [character_item_id(c) for c in PREMIUM_CHARACTER_IDS]
        self.assertEqual(expected + [1010015, 1120041], items)

    def test_the_two_id_spaces_do_not_overlap(self):
        # 商城角色是 9 位的 `(id+1)*1e6+400001`，装备是 7 位的 ⇒ 直接接起来
        # 就行，不用去重。这条断言守着「直接接」这个前提。
        character_ids = set(character_item_id(c) for c in PREMIUM_CHARACTER_IDS)
        self.assertTrue(min(character_ids) > 9999999)

    def test_gear_the_player_does_not_own_never_reaches_the_client(self):
        # 手改存档「装备了没买的东西」时就地丢掉 —— 发一个客户端查不到的 id
        # 下去，轻则空格子，重则加成算在别人头上。
        conn = self.make_conn(account={
            "level": 1, "experience": 0, "money": 0, "character": 0,
            "inventory": {}, "equipped": [1010015],
        })
        gameserver.Conn.send_slot_equipped_list(conn)
        _seat, _masks, items = self.parse_equipped(conn.sent[0])
        self.assertEqual([character_item_id(c) for c in PREMIUM_CHARACTER_IDS],
                         items)


class ShopControlCommandTests(unittest.TestCase):
    """`inv` / `give` / `equip` / `unequip`（V0.3商店 M3 的实机验证工具）。

    这四条是**调试命令**，但 M3 那个 spike（「穿上之后属性真的变了吗」）
    只能靠它们做 —— 商店 UI 还没通，没有别的路把装备穿到身上。
    """

    REVOLVER_R1 = 1120041        # 리볼버 R1，武器槽 1
    REVOLVER_R2 = 1120042        # 同一个武器槽的另一把
    TOP_ARMOR = 1010015          # 上衣，和武器不抢槽
    BRONZE_PIPE = 30018          # 材料
    STOCK_ONLY = 1510001         # 只有货架条目，进不了背包

    #: `CharacterUnlockTests.make_conn()` 要它。借那个假 Conn 而不是再造一个
    #: —— 两处要是走样了，这组测试就不再是在测同一条下发路径。
    Args = CharacterUnlockTests.Args

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AccountStore(os.path.join(self.tmp.name, "accounts.json"))
        self.store.register("tester", "pw")
        self.conn = CharacterUnlockTests.make_conn(self)
        self.conn.accounts = self.store
        self.conn.account = self.store.get_account("tester")[1]
        saved = list(gameserver._conns)
        gameserver._conns[:] = [self.conn]
        self.addCleanup(
            lambda: gameserver._conns.__setitem__(slice(None), saved))

    def run_cmd(self, line):
        return gameserver.handle_control_command(line)

    def equipped_now(self):
        return account_store.equipped_items(
            self.store.get_account("tester")[1])

    def test_give_puts_it_in_the_warehouse(self):
        self.assertTrue(self.run_cmd(f"give {self.REVOLVER_R1}").startswith("ok"))
        self.assertTrue(account_store.has_item(
            self.store.get_account("tester")[1], self.REVOLVER_R1))

    def test_give_refuses_an_id_the_client_does_not_know(self):
        reply = self.run_cmd(f"give {self.STOCK_ONLY}")
        self.assertTrue(reply.startswith("err"), reply)

    def test_give_material_refuses_an_id_the_client_does_not_know(self):
        # `add_materials` 是「跳过不抛」的（D12），命令层要自己把它说出来，
        # 否则 `give-material 999` 会回一句 ok 而什么都没发生。
        self.assertTrue(self.run_cmd("give-material 9999999").startswith("err"))
        self.assertTrue(
            self.run_cmd(f"give-material {self.BRONZE_PIPE} 3").startswith("ok"))
        self.assertEqual(
            {self.BRONZE_PIPE: 3},
            account_store.material_counts(self.store.get_account("tester")[1]))

    def test_equip_adds_the_item_when_the_warehouse_is_empty(self):
        # 调试命令的本分是省事 —— 但要在应答里说清楚它替你做了什么。
        reply = self.run_cmd(f"equip {self.TOP_ARMOR}")
        self.assertTrue(reply.startswith("ok"), reply)
        self.assertIn("顺手放一件进去", reply)
        self.assertEqual([self.TOP_ARMOR], self.equipped_now())

    def test_equip_reships_0x030b_immediately(self):
        # ★ 加成只认 `0x030b`：不重发的话客户端手里还是上一份清单。
        self.run_cmd(f"equip {self.TOP_ARMOR}")
        frames = [f for f in self.conn.sent
                  if take_frame(bytearray(f))[1] == OP_SLOT_EQUIPPED_LIST]
        self.assertEqual(1, len(frames))
        items = CharacterUnlockTests.parse_equipped(frames[0])[2]
        self.assertIn(self.TOP_ARMOR, items)

    def test_equipping_the_same_slot_knocks_the_old_one_off(self):
        self.run_cmd(f"equip {self.REVOLVER_R1}")
        reply = self.run_cmd(f"equip {self.REVOLVER_R2}")
        self.assertEqual([self.REVOLVER_R2], self.equipped_now())
        self.assertIn(str(self.REVOLVER_R1), reply)

    def test_unequip_takes_it_off_and_reships(self):
        self.run_cmd(f"equip {self.TOP_ARMOR}")
        self.run_cmd(f"equip {self.REVOLVER_R1}")
        self.conn.sent.clear()
        self.run_cmd(f"unequip {self.TOP_ARMOR}")
        self.assertEqual([self.REVOLVER_R1], self.equipped_now())
        items = CharacterUnlockTests.parse_equipped(self.conn.sent[0])[2]
        self.assertNotIn(self.TOP_ARMOR, items)

    def test_inv_shows_money_gear_and_materials(self):
        self.store.add_quest_reward("tester", money=1234)
        self.run_cmd(f"equip {self.REVOLVER_R1}")
        self.run_cmd(f"give {self.TOP_ARMOR}")
        self.run_cmd(f"give-material {self.BRONZE_PIPE} 5")
        reply = self.run_cmd("inv")
        self.assertTrue(reply.startswith("ok"), reply)
        self.assertIn("1234", reply)
        self.assertIn("★穿着", reply)
        # 没铺配置时退回 `item_name_zh()` 自己翻的那一份（韩文翻不出来才留韩文）。
        self.assertIn(f"{self.BRONZE_PIPE} 青铜管 ×5", reply)
        self.assertIn(str(self.TOP_ARMOR), reply)

    def test_the_item_label_prefers_the_chinese_name_from_the_library(self):
        # ★ 名字有两个来源：**物品库**里的中文名（管理页可改）优先，
        #   退回 `item_name_zh()` 自己翻的那一份（翻不出来才是原版韩文名）。
        #   跑测试时 `shopcfg` 指向空目录（见 `run_tests.py`），
        #   所以这里要自己铺一份配置才测得到「优先」那一路。
        import shopcfg
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        saved = shopcfg.DATA_DIR
        shopcfg.DATA_DIR = tmp.name
        self.addCleanup(shopcfg.invalidate)
        self.addCleanup(setattr, shopcfg, "DATA_DIR", saved)
        shopcfg.write_json(
            shopcfg.path_of(shopcfg.ITEMS_FILENAME, tmp.name),
            {"format": 1, "items": [{"id": self.BRONZE_PIPE,
                                     "name": "我改的管子"}]})
        shopcfg.invalidate()
        self.assertEqual(f"{self.BRONZE_PIPE} 我改的管子",
                         gameserver._item_label(self.BRONZE_PIPE))
        # 物品库里没有的，退回自动翻译；物品表里都没有的只剩一个数字。
        self.assertEqual(f"{self.REVOLVER_R1} 左轮 极速1",
                         gameserver._item_label(self.REVOLVER_R1))
        self.assertEqual("9999999", gameserver._item_label(9999999))

    def test_the_commands_report_their_usage(self):
        for cmd in ("give", "give-material", "equip", "unequip"):
            self.assertTrue(self.run_cmd(cmd).startswith("err 用法"), cmd)

    def test_every_new_command_is_in_the_help_text(self):
        # 命令表是给人看的唯一入口，加了命令不写进去等于没加。
        for cmd in ("inv", "give", "give-material", "equip", "unequip"):
            self.assertIn(cmd, gameserver.CONTROL_HELP, cmd)


class MaterialDropTests(unittest.TestCase):
    """材料掉落的规则（`quest_materials`）和结算包（`build_reward_received`）。

    ★ 规则全部铺成 `prob=100` / `prob=0`，**用例里一点随机性都没有** ——
    掉落是概率的，但「哪条规则命中」是确定的，两件事分开测。
    """

    #: 真实的材料 id：`validate_drops` 会拿 `shopdata.is_material()` 卡一道。
    BRONZE_PIPE = 30018
    BLACK_BEAD = 10001
    GREEN_BEAD = 10003

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved_dir = shopcfg.DATA_DIR
        shopcfg.DATA_DIR = self.tmp.name
        self.addCleanup(shopcfg.invalidate)
        self.addCleanup(setattr, shopcfg, "DATA_DIR", saved_dir)
        shopcfg.invalidate()

    def write_rules(self, *rules):
        shopcfg.write_json(shopcfg.path_of(shopcfg.DROPS_FILENAME, self.tmp.name),
                           {"format": 1, "rules": list(rules)})
        shopcfg.invalidate()

    def rule(self, material, **kw):
        rule = {"mode": "quest", "material": material, "count": 1,
                "prob": 100, "cleared_only": True}
        rule.update(kw)
        return rule

    def drops(self, quest_id=1, difficulty=1, cleared=True, mode="quest"):
        materials, warnings = gameserver.quest_materials(
            quest_id, difficulty, cleared, mode=mode)
        self.assertEqual([], warnings)
        return materials

    # -- 0x041c 的线格式 ----------------------------------------------------
    def test_the_reward_packet_is_four_int32s(self):
        payload = gameserver.build_reward_received(
            2, gameserver.REWARD_SLOT_MATERIAL, self.BRONZE_PIPE, 3)
        # Deserialize `0x54c5d0` 四发「读 4 字节」，**没有 bool** ⇒ 正好 16 字节。
        self.assertEqual(16, len(payload))
        self.assertEqual((2, 0, self.BRONZE_PIPE, 3),
                         struct.unpack_from("<4i", payload, 0))

    def test_the_two_result_columns_have_different_slot_numbers(self):
        # 结算界面两栏共用这一个包，靠线偏移 4 的槽类型分（§3）。
        self.assertEqual(0, gameserver.REWARD_SLOT_MATERIAL)
        self.assertEqual(1, gameserver.REWARD_SLOT_TITLE)
        payload = gameserver.build_reward_received(
            0, gameserver.REWARD_SLOT_TITLE, 560002, 1)
        self.assertEqual(1, struct.unpack_from("<i", payload, 4)[0])

    # -- 规则筛选 -----------------------------------------------------------
    def test_a_rule_without_stage_or_difficulty_applies_everywhere(self):
        self.write_rules(self.rule(self.BRONZE_PIPE, count=2))
        self.assertEqual({self.BRONZE_PIPE: 2}, self.drops(quest_id=7,
                                                           difficulty=3))

    def test_stage_and_difficulty_narrow_a_rule_down(self):
        self.write_rules(self.rule(self.BRONZE_PIPE, stage=4, difficulty=3))
        self.assertEqual({self.BRONZE_PIPE: 1}, self.drops(4, 3))
        self.assertEqual({}, self.drops(4, 2))
        self.assertEqual({}, self.drops(5, 3))

    def test_cleared_only_rules_pay_nothing_when_the_quest_is_failed(self):
        # ★ `drops.json` 里**绝大多数**规则是 cleared_only ⇒ 手动 `endgame`
        #   （打不出通关标志）一个材料都掉不出来，要用 `clear`。
        self.write_rules(self.rule(self.BRONZE_PIPE),
                         self.rule(self.BLACK_BEAD, cleared_only=False))
        self.assertEqual({self.BRONZE_PIPE: 1, self.BLACK_BEAD: 1},
                         self.drops(cleared=True))
        self.assertEqual({self.BLACK_BEAD: 1}, self.drops(cleared=False))

    def test_pvp_rules_and_quest_rules_do_not_leak_into_each_other(self):
        self.write_rules(self.rule(self.BRONZE_PIPE),
                         self.rule(self.BLACK_BEAD, mode="pvp"))
        self.assertEqual({self.BRONZE_PIPE: 1}, self.drops(mode="quest"))
        self.assertEqual({self.BLACK_BEAD: 1}, self.drops(mode="pvp"))

    def test_probability_zero_never_pays_and_hundred_always_does(self):
        self.write_rules(self.rule(self.BRONZE_PIPE, prob=0),
                         self.rule(self.BLACK_BEAD, prob=100))
        for _ in range(20):
            self.assertEqual({self.BLACK_BEAD: 1}, self.drops())

    def test_two_rules_on_the_same_material_add_up(self):
        # 原版基线和我们的扩展规则会撞在同一种材料上（`drops.json` 默认就有
        # 这种情况）—— 撞了要**相加**，不是后一条盖前一条。
        self.write_rules(self.rule(self.BRONZE_PIPE, count=1, stage=1),
                         self.rule(self.BRONZE_PIPE, count=2))
        self.assertEqual({self.BRONZE_PIPE: 3}, self.drops(quest_id=1))

    def test_the_odds_land_where_the_rule_says(self):
        # 概率本身也要测一次，但用**固定种子**，不看单局看分布。
        self.write_rules(self.rule(self.GREEN_BEAD, prob=25))
        rng = random.Random(20260904)
        hits = sum(1 for _ in range(2000)
                   if gameserver.quest_materials(1, 1, True, rng=rng)[0])
        self.assertTrue(400 <= hits <= 600, hits)

    def test_a_broken_drops_file_costs_at_most_this_round_s_materials(self):
        # D10：坏文件保留上一份好的、绝不回写。一次都没读成功过就返回空表
        # ⇒ 最坏是「这一局没掉东西」，不会是「结算卡住」。
        path = shopcfg.path_of(shopcfg.DROPS_FILENAME, self.tmp.name)
        with open(path, "w", encoding="utf-8") as fp:
            fp.write("{ this is not json")
        shopcfg.invalidate()
        materials, warnings = gameserver.quest_materials(1, 1, True)
        self.assertEqual({}, materials)
        self.assertTrue(warnings)

    def test_a_junk_quest_id_does_not_raise(self):
        # `current_quest()` 拿不到参数时会给出各种东西，掉落不该跟着炸。
        self.write_rules(self.rule(self.BRONZE_PIPE))
        self.assertEqual({self.BRONZE_PIPE: 1},
                         self.drops(quest_id=None, difficulty="x"))


class ShopProbeTests(unittest.TestCase):
    """商店 / 合成 / 仓库段（V0.3商店 M4 第 2 步 —— 开始回包）。

    ★ 这一组守的是**「谁回、谁不回」**这条线：下行三发全是静态逆出来的，
    多回一发就可能把界面弄崩，少回一发就是界面空着。哪一发该回、哪一发
    坚决不回（`0x0700` 不是商店专用的），都在这儿钉死。
    """

    Args = CharacterUnlockTests.Args

    def setUp(self):
        # 这一段**故意**把每一发都写进 `logs/online.log`（那份不会被覆盖），
        # 但跑测试时那是纯噪音 —— 把它掐掉，只留 `conn.logged` 供断言。
        saved = gameserver.eventlog.online
        gameserver.eventlog.online = lambda _msg: None
        self.addCleanup(setattr, gameserver.eventlog, "online", saved)

    def make_conn(self, account=None):
        conn = CharacterUnlockTests.make_conn(self, account)
        conn.accounts = None
        return conn

    @staticmethod
    def opcodes_of(conn):
        return [take_frame(bytearray(frame))[1] for frame in conn.sent]

    def test_only_the_mapped_uplink_packets_get_an_answer(self):
        # ★★ 这条一旦红了，说明有人给这段加了 / 减了应答。改之前先想清楚
        #    「多回一发会不会把界面弄崩」—— 下行仍然是 🔍静态（§21 / §23）。
        answered = {}
        for opcode in gameserver.SHOP_PROBE_OPCODES:
            conn = self.make_conn()
            if opcode in (gameserver.OP_REQ_SHOP_ITEM_LIST,
                          gameserver.OP_REQ_COMPOSITION_LIST):
                payload = shop.build_shop_list_request()
            elif opcode in (gameserver.OP_REQ_EQUIP_ITEM,
                            gameserver.OP_REQ_UNEQUIP_ITEM):
                payload = struct.pack("<i", 1120041)
            elif opcode == gameserver.OP_REQ_ITEM_BUY:
                payload = struct.pack("<i", 0)     # 空购物车
            elif opcode == gameserver.OP_REQ_ITEM_INFO:
                # 「这个 id 我不认识」——真的挑一个物品表里有的，否则
                # 回不出定义，这条就退化成「什么都没发」。
                payload = struct.pack("<ii", 1, 1120041) + b"\x02"
            else:
                payload = b""
            gameserver.Conn.on_game_packet(conn, opcode, payload)
            answered[opcode] = self.opcodes_of(conn)
        self.assertEqual(
            # ★ 货架 / 装备清单里**只要有东西**，前面就多一发 `0x0501`
            #   物品定义 —— 客户端的 `ItemInfo` 表开机是空的，不先喂它就弹
            #   「无法从服务器读取道具信息」（§28）。这条用例的 `shop.json`
            #   和假账号的仓库都是空的 ⇒ 两路都没有定义可发。
            {gameserver.OP_REQ_SHOP_ITEM_LIST: [gameserver.OP_REP_SHOP_ITEM_LIST],
             gameserver.OP_REQ_EQUIPPED_LIST: [gameserver.OP_REP_EQUIPPED_LIST],
             gameserver.OP_REQ_GIFT_LIST: [gameserver.OP_REP_GIFT_LIST],
             # ★ `0x0700` = 「给我持有物清单」（§29）。空仓库也要回 ——
             #   不回的话仓库面板等一个永远不来的应答。
             gameserver.OP_REQ_INVENTORY: [gameserver.OP_REP_INVENTORY],
             gameserver.OP_REQ_ITEM_INFO: [gameserver.OP_REP_ITEM_INFO],
             # ★★ 穿 / 脱都回 `0x0604`，**一发不落**（§24）：客户端拿它当
             #    计数器，少回一发仓库界面就永远不刷新。
             gameserver.OP_REQ_EQUIP_ITEM: [gameserver.OP_REP_EQUIPPED_LIST],
             gameserver.OP_REQ_UNEQUIP_ITEM: [gameserver.OP_REP_EQUIPPED_LIST],
             # 空购物车 -> 回一发失败的 0x0502。
             gameserver.OP_REQ_ITEM_BUY: [gameserver.OP_REP_ITEM_BUY],
             # 合成配方清单。这条用例的 `recipe.json` 是空的 ⇒ 前面那发
             # `0x0501` 没有定义可发，只剩清单本身。
             gameserver.OP_REQ_COMPOSITION_LIST:
                 [gameserver.OP_REP_COMPOSITION_LIST],
             gameserver.OP_REQ_SHOP_UNKNOWN_0603: [],
             # 修理本版不做（`0x0604` 上行 = 用扳手，不是装备，§24）。
             gameserver.OP_REQ_REPAIR_ITEM: [],
             # ★ 合成：空载荷解不开，**还是要回一发**失败的 `0x0506` ——
             #   不回的话玩家点了确定界面什么都不发生。
             gameserver.OP_REQ_COMPOSE_ITEM: [gameserver.OP_REP_COMPOSE_ITEM]},
            answered)

    #: 真实的武器槽 1 分类（实机点「武器 → 武器1」发的就是它，§22）。
    WEAPON_SLOT1 = 0x60001

    def shelf_reply(self, category, items, library=()):
        with config_dir(shop=items, items=library):
            conn = self.make_conn()
            gameserver.Conn.on_game_packet(
                conn, gameserver.OP_REQ_SHOP_ITEM_LIST,
                shop.build_shop_list_request(category=category))
        # ★ 货架前面还有一发 `0x0501` 物品定义（§28）—— 挑出货架那一发。
        frames = [take_frame(bytearray(f))[1:3] for f in conn.sent]
        bodies = [body for opcode, body in frames
                  if opcode == gameserver.OP_REP_SHOP_ITEM_LIST]
        self.assertEqual(1, len(bodies), [hex(op) for op, _ in frames])
        return parse_shop_item_list(bodies[0])

    def test_the_shelf_reply_carries_the_listed_items(self):
        """★ 货架上的名字取**物品库**（D31）—— `shop.json` 里没有这个字段。"""
        pages, page, groups = self.shelf_reply(
            self.WEAPON_SLOT1,
            [{"id": 1120041, "listed": True, "price": 3000},
             {"id": 1120051, "listed": False, "price": 999}],
            library=[{"id": 1120041, "name": "左轮 R1", "level": 1},
                     {"id": 1120051, "name": "没上架的", "level": 1}])
        self.assertEqual((1, 0), (pages, page))
        self.assertEqual([[(1120041, "左轮 R1", 3000)]], groups)

    def test_category_zero_is_the_hero_tab_not_a_wildcard(self):
        """★ 2026-09-05 实机 bug：点「人物 → 英雄」列出了一堆武器。

        客户端在那个标签下发的分类就是 `0`（`logs/server.out` 11:56:57）。
        当时服务端把它当「全部」⇒ 把整份货架倒了出来。正确行为是空货架。
        """
        pages, page, groups = self.shelf_reply(
            0, [{"id": 1120041, "listed": True,
                 "price": 3000}])
        self.assertEqual((1, 0, []), (pages, page, groups))

    def test_a_malformed_shelf_request_is_dropped_not_guessed(self):
        # 长度不对就是我们把线格式记错了（§19 记错过一次）。宁可不回 ——
        # 客户端会 10 秒后重发，日志里那一行足够定位。
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_REQ_SHOP_ITEM_LIST,
                                       b"\x00\x01\x02")
        self.assertEqual([], conn.sent)
        self.assertIn("货架请求解不开", "\n".join(conn.logged))

    def test_the_equipped_list_only_carries_items_the_client_knows(self):
        account = {"level": 1, "experience": 0, "money": 0, "character": 0,
                   "inventory": {"1120041": {"count": 1},
                                 "1510001": {"count": 1}},   # 只有货架，进不了背包
                   "equipped": [1120041]}
        conn = self.make_conn(account)
        gameserver.Conn.on_game_packet(conn, gameserver.OP_REQ_EQUIPPED_LIST, b"")
        frames = [take_frame(bytearray(f))[1:3] for f in conn.sent]
        # ★ 定义（`0x0501`）必须排在装备清单前面 —— 客户端查不到定义就弹
        #   「无法从服务器读取道具信息」，而且那一发还会跳过界面刷新（§28）。
        self.assertEqual([gameserver.OP_REP_ITEM_INFO,
                          gameserver.OP_REP_EQUIPPED_LIST],
                         [opcode for opcode, _ in frames])
        self.assertEqual([1120041], parse_rep_equipped_list(frames[1][1]))

    def test_the_inventory_reply_carries_equipment_and_materials(self):
        """★ `0x0700` = 「给我持有物清单」，回 `0x0601`（§29）。

        这一发不发，仓库界面就是空的 —— 2026-09-05 实机报的那个
        「`gs_ctl.py inv` 有东西，仓库里什么都没有」就是它。
        """
        account = {"level": 1, "experience": 0, "money": 0, "character": 0,
                   "inventory": {"1120041": {"count": 1}},
                   "materials": {"30018": 3},
                   "equipped": [1120041]}
        conn = self.make_conn(account)
        gameserver.Conn.on_game_packet(conn, gameserver.OP_REQ_INVENTORY, b"")
        frames = [take_frame(bytearray(f))[1:3] for f in conn.sent]
        # ⚠⚠ 顺序不能换：仓库面板是在收到 `0x0601` 的那一刻建的，那会儿
        #    ItemDB 还空着就建成空的，补发的定义不会让它再刷一次（§29）。
        self.assertEqual([gameserver.OP_REP_ITEM_INFO,
                          gameserver.OP_REP_INVENTORY],
                         [opcode for opcode, _ in frames])
        self.assertEqual([(30018, 3, 0.0, 0), (1120041, 1, 0.0, 0)],
                         parse_rep_inventory(frames[1][1]))
        # 定义两件都得有，否则仓库格子是空的。
        records, purpose = parse_rep_item_info(frames[0][1])
        self.assertEqual([30018, 1120041], sorted(r["id"] for r in records))
        self.assertEqual(shop.ITEM_INFO_FOR_SHELF, purpose)

    def test_an_item_info_request_is_answered_with_the_same_purpose_byte(self):
        """⚠⚠ 用途标志回错 = 人物模型不刷新（`0x44602a` 只认 `2`，§28）。"""
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(
            conn, gameserver.OP_REQ_ITEM_INFO,
            struct.pack("<ii", 1, 1120041) + b"\x02")
        opcode, body = take_frame(bytearray(conn.sent[0]))[1:3]
        self.assertEqual(gameserver.OP_REP_ITEM_INFO, opcode)
        records, purpose = parse_rep_item_info(body)
        self.assertEqual([1120041], [r["id"] for r in records])
        self.assertEqual(shop.ITEM_INFO_FOR_EQUIPPED, purpose)

    def test_the_gift_list_is_answered_empty_not_ignored(self):
        # 不做礼物系统 ≠ 不回：「等一个永远不来的应答」和「收到了空清单」
        # 是两种现象，下次实机看到礼物页转圈时不用再排除我们这一侧。
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_REQ_GIFT_LIST, b"")
        opcode, body = take_frame(bytearray(conn.sent[0]))[1:3]
        self.assertEqual(gameserver.OP_REP_GIFT_LIST, opcode)
        self.assertEqual(b"\x00\x00\x00\x00", body)

    def test_shop_reply_can_switch_one_answer_off_without_a_restart(self):
        # 实机万一界面不对，这条命令是二分工具（改代码要重启，改它不用）。
        self.addCleanup(gameserver.SHOP_REPLY_ENABLED.update,
                        dict(gameserver.SHOP_REPLY_ENABLED))
        reply = gameserver.handle_control_command(
            "shop-reply off %04x" % gameserver.OP_REP_GIFT_LIST)
        self.assertTrue(reply.startswith("ok"), reply)
        conn = self.make_conn()
        gameserver.Conn.on_game_packet(conn, gameserver.OP_REQ_GIFT_LIST, b"")
        self.assertEqual([], conn.sent)
        self.assertIn("shop-reply", "\n".join(conn.logged))

    def test_the_probe_logs_the_payload(self):
        conn = self.make_conn()
        gameserver.Conn.on_shop_probe(conn, gameserver.OP_REQ_ITEM_BUY,
                                      bytes.fromhex("01000000" + "29111100"))
        text = "\n".join(conn.logged)
        self.assertIn("[shop]", text)
        self.assertIn("0x0602", text)
        # 逆出来的形状不一定对 ⇒ 原始的 int32 切分必须照打，别只留一句「解不出来」。
        self.assertIn("int32 切分: [1, 1118505]", text)

    def test_an_empty_payload_does_not_produce_an_empty_hexdump(self):
        conn = self.make_conn()
        gameserver.Conn.on_shop_probe(conn, gameserver.OP_REQ_INVENTORY, b"")
        self.assertIn("载荷 0 字节", conn.logged[0])
        self.assertNotIn("0000  ", conn.logged[0])

    def test_the_entry_requests_are_in_the_order_the_client_sends_them(self):
        # ShopStage ctor `0x444009`~`0x44402c` 是顺序调用的，日志按这个顺序
        # 标「第 N/5 发」，对不上就没法一眼看出客户端是不是五发都发了。
        # ★ 打头那发 `0x0600` 会话 03 漏掉了（它没进这张表，只落在通用包
        #   日志里），害得「货架走哪个 opcode」多悬了一整轮 —— 钉死顺序。
        self.assertEqual((0x0600, 0x0704, 0x0605, 0x0607, 0x0700),
                         gameserver.SHOP_ENTRY_OPCODES)
        conn = self.make_conn()
        for opcode in gameserver.SHOP_ENTRY_OPCODES:
            gameserver.Conn.on_shop_probe(conn, opcode, b"")
        text = "\n".join(conn.logged)
        for index in range(1, len(gameserver.SHOP_ENTRY_OPCODES) + 1):
            self.assertIn(f"进商店第 {index}/5 发", text)

    def test_every_shop_opcode_has_a_name_and_a_note(self):
        # 日志是实机唯一的产出，缺一句说明就等于少采一条数据。
        for opcode in gameserver.SHOP_PROBE_OPCODES:
            self.assertIn(opcode, gameserver.GCP_NAMES, hex(opcode))
            self.assertIn(opcode, gameserver.SHOP_PROBE_NOTES, hex(opcode))

    def test_the_shop_opcodes_do_not_collide_with_another_client_branch(self):
        """这批号被别的**客户端方向**分支占了的话，那个功能会被静默吞掉。

        ★ **服务端方向的同号不算** —— `0x0600` 是 `gspRepMoney`、`0x0604` 是
          `gspRepEquippedList`，方向不同，压根不走同一条分发链
          （`re/packet_api.md` §1.5 同号反向表）。
        """
        mine = tuple(name for name in vars(gameserver)
                     if name.startswith(("OP_REQ_SHOP_", "OP_REQ_ITEM_BUY",
                                         "OP_REQ_ITEM_INFO", "OP_REQ_INVENTORY",
                                         "OP_REQ_EQUIP_ITEM", "OP_REQ_COMPOS",
                                         "OP_REQ_GIFT_LIST", "OP_REQ_UNEQUIP_",
                                         "OP_REQ_REPAIR_ITEM",
                                         "OP_REQ_EQUIPPED_LIST")))
        server_direction = ("OP_REP_MONEY", "OP_REP_EQUIPPED_LIST",
                            "OP_REP_SHOP_ITEM_LIST", "OP_REP_GIFT_LIST",
                            "OP_REP_INVENTORY")
        handled = {value for name, value in vars(gameserver).items()
                   if name.startswith("OP_") and isinstance(value, int)
                   and name not in mine and name not in server_direction}
        for opcode in gameserver.SHOP_PROBE_OPCODES:
            self.assertNotIn(opcode, handled, hex(opcode))


class ShopBuyAndEquipTests(unittest.TestCase):
    """`0x0602` 买 / `0x0702` 穿 / `0x0703` 脱（V0.3商店 M5，协议见 §24）。

    ★ 这一组守的是两件**只在真存档上才成立**的事：钱和东西的账要对得上，
    以及**每一发穿脱都必须回一发 `0x0604`** —— 客户端拿它当计数器。
    """

    REVOLVER_R1 = 1120041        # 武器槽 1
    REVOLVER_R2 = 1120042        # 同一个槽的另一把（抢槽）
    Args = CharacterUnlockTests.Args

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AccountStore(os.path.join(self.tmp.name, "accounts.json"))
        self.store.register("tester", "pw")
        saved = gameserver.eventlog.online
        gameserver.eventlog.online = lambda _msg: None
        self.addCleanup(setattr, gameserver.eventlog, "online", saved)
        self.conn = CharacterUnlockTests.make_conn(self)
        self.conn.accounts = self.store
        self.conn.account = self.store.get_account("tester")[1]

    def account(self):
        return self.store.get_account("tester")[1]

    def give_money(self, amount):
        # `add_quest_reward` 是唯一的加钱入口（服务端只在结算时给钱）。
        self.store.add_quest_reward("tester", money=amount)
        self.conn.account = self.account()

    def send(self, opcode, payload):
        self.conn.sent[:] = []
        gameserver.Conn.on_game_packet(self.conn, opcode, payload)
        return [take_frame(bytearray(f))[1:3] for f in self.conn.sent]

    def buy(self, *item_ids):
        payload = struct.pack("<i", len(item_ids))
        payload += b"".join(struct.pack("<i", i) for i in item_ids)
        return self.send(gameserver.OP_REQ_ITEM_BUY, payload)

    @staticmethod
    def buy_ok(frames):
        for opcode, body in frames:
            if opcode == gameserver.OP_REP_ITEM_BUY:
                return struct.unpack_from("<i", body, 0)[0]
        raise AssertionError("没有回 0x0502")

    @staticmethod
    def buy_reason(frames):
        """`0x0502` 第二格 = 失败原因码（客户端拿它挑「购买失败」框的正文）。"""
        for opcode, body in frames:
            if opcode == gameserver.OP_REP_ITEM_BUY:
                return struct.unpack_from("<i", body, 4)[0]
        raise AssertionError("没有回 0x0502")

    # -- 买 -----------------------------------------------------------------
    def test_buying_takes_the_money_and_gives_the_item(self):
        self.give_money(10000)
        with shop_config([{"id": self.REVOLVER_R1,
                           "listed": True, "price": 3000}]):
            frames = self.buy(self.REVOLVER_R1)
        self.assertEqual(1, self.buy_ok(frames))
        account = self.account()
        self.assertEqual(7000, account_store.player_money(account))
        self.assertTrue(account_store.has_item(account, self.REVOLVER_R1))
        # ★★ **顺序是硬要求**：仓库（`0x0601`）要排在购买结果（`0x0502`）前面。
        #    `0x0502` 的处理器会弹「要不要直接穿上」，点确定时它去**本地背包**
        #    里找刚买的那件（`0x4463f0`），找不到就什么都不做 —— 那正是
        #    2026-09-05 实机看到的「选了装备却没装上」（§30）。
        #    定义（`0x0501`）又必须排在持有物清单前面（§29）。
        self.assertEqual([gameserver.OP_REP_ITEM_INFO,
                          gameserver.OP_REP_INVENTORY,
                          gameserver.OP_REP_EQUIPPED_LIST,
                          gameserver.OP_REP_MONEY,
                          gameserver.OP_REP_ITEM_BUY],
                         [opcode for opcode, _ in frames])

    def test_the_price_comes_from_shop_json_not_from_the_packet(self):
        # 客户端只发 itemId，价格是我们自己查的 —— 管理页改了价，立刻生效。
        self.give_money(10000)
        with shop_config([{"id": self.REVOLVER_R1,
                           "listed": True, "price": 9999}]):
            self.buy(self.REVOLVER_R1)
        self.assertEqual(1, account_store.player_money(self.account()))

    def test_not_enough_money_buys_nothing(self):
        self.give_money(100)
        with shop_config([{"id": self.REVOLVER_R1,
                           "listed": True, "price": 3000}]):
            frames = self.buy(self.REVOLVER_R1)
        self.assertEqual(0, self.buy_ok(frames))
        account = self.account()
        self.assertEqual(100, account_store.player_money(account))
        self.assertFalse(account_store.has_item(account, self.REVOLVER_R1))

    def test_a_whole_cart_is_all_or_nothing(self):
        """★ 一车里有一件买不了，整车都不买 —— 校验全做完才扣钱。

        反过来（扣完钱再发现有一件发不出去）就是「钱扣了东西没有」，
        那是商店最难查的一种账（D12 的同一条道理）。
        """
        self.give_money(10000)
        with shop_config([{"id": self.REVOLVER_R1,
                           "listed": True, "price": 3000},
                          {"id": self.REVOLVER_R2,
                           "listed": False, "price": 3000}]):
            frames = self.buy(self.REVOLVER_R1, self.REVOLVER_R2)
        self.assertEqual(0, self.buy_ok(frames))
        account = self.account()
        self.assertEqual(10000, account_store.player_money(account))
        self.assertFalse(account_store.has_item(account, self.REVOLVER_R1))

    def test_buying_the_same_equipment_twice_is_refused(self):
        # 原版规则（失败文案 `이미 소지하고 있습니다`，§7）。
        self.give_money(10000)
        with shop_config([{"id": self.REVOLVER_R1,
                           "listed": True, "price": 3000}]):
            self.buy(self.REVOLVER_R1)
            frames = self.buy(self.REVOLVER_R1)
        self.assertEqual(0, self.buy_ok(frames))
        self.assertEqual(7000, account_store.player_money(self.account()))

    def test_a_level_gate_is_enforced_server_side(self):
        """★ D31：等级门槛来自**物品库**，不是商店货架。"""
        self.give_money(10000)
        with config_dir(shop=[{"id": self.REVOLVER_R1,
                               "listed": True, "price": 3000}],
                        items=[{"id": self.REVOLVER_R1, "level": 50}]):
            frames = self.buy(self.REVOLVER_R1)
        self.assertEqual(0, self.buy_ok(frames))

    def test_an_empty_cart_still_gets_an_answer(self):
        # 不回的话客户端那个「购买结果」弹窗会一直挂着。
        frames = self.buy()
        self.assertEqual(0, self.buy_ok(frames))

    def test_every_refusal_carries_a_reason_the_client_can_show(self):
        """★ `0x0502` 第二格是**失败原因码**（§30，2026-09-05 实机落锤）。

        填 0 = 玩家看到「未定义的错误」—— 我们明明知道原因却不说。
        兜底也不能是 0，得是 6「内部错误」。
        """
        self.give_money(10000)
        with config_dir(shop=[{"id": self.REVOLVER_R1,
                               "listed": True, "price": 3000},
                              {"id": self.REVOLVER_R2,
                               "listed": False, "price": 3000}],
                        items=[{"id": self.REVOLVER_R1, "level": 50}]):
            self.assertEqual(shop.BUY_REASON_LEVEL_LOW,
                             self.buy_reason(self.buy(self.REVOLVER_R1)))
            self.assertEqual(shop.BUY_REASON_INTERNAL,
                             self.buy_reason(self.buy(self.REVOLVER_R2)))
        with shop_config([{"id": self.REVOLVER_R1,
                           "listed": True, "price": 3000}]):
            self.buy(self.REVOLVER_R1)                 # 第一次买成
            self.assertEqual(shop.BUY_REASON_ALREADY_OWNED,
                             self.buy_reason(self.buy(self.REVOLVER_R1)))
        with shop_config([{"id": self.REVOLVER_R2,
                           "listed": True, "price": 999999}]):   # 买不起
            self.assertEqual(shop.BUY_REASON_NO_PIXEL,
                             self.buy_reason(self.buy(self.REVOLVER_R2)))
        # 空车走的是「认不出的理由」那条路 ⇒ 兜底必须是 6，不是 0。
        self.assertEqual(shop.BUY_REASON_INTERNAL, self.buy_reason(self.buy()))

    def test_you_can_buy_another_characters_weapon(self):
        """★ 2026-09-05 实机 bug：泰尔买布洛克的火箭筒被服务端拦下。

        商店上方那排角色箭头就是给「替别的角色买装备」用的
        （货架本来就按**预览角色**过滤）。「按玩家当前角色拦」是我们自己
        加的规矩，原版没有。
        """
        self.give_money(10000)
        with shop_config([{"id": 2120041,
                           "listed": True, "price": 3000}]):
            frames = self.buy(2120041)          # 假账号的角色是 0（泰尔）
        self.assertEqual(1, self.buy_ok(frames))
        self.assertTrue(account_store.has_item(self.account(), 2120041))

    # -- 穿 / 脱 ------------------------------------------------------------
    def own(self, *item_ids):
        for item_id in item_ids:
            self.store.add_item("tester", item_id)
        self.conn.account = self.account()

    def equip(self, item_id):
        return self.send(gameserver.OP_REQ_EQUIP_ITEM,
                         struct.pack("<i", item_id))

    def unequip(self, item_id):
        return self.send(gameserver.OP_REQ_UNEQUIP_ITEM,
                         struct.pack("<i", item_id))

    def test_equip_and_unequip_change_the_save(self):
        self.own(self.REVOLVER_R1)
        self.equip(self.REVOLVER_R1)
        self.assertEqual([self.REVOLVER_R1],
                         account_store.equipped_items(self.account()))
        self.unequip(self.REVOLVER_R1)
        self.assertEqual([], account_store.equipped_items(self.account()))

    def test_a_new_weapon_pushes_the_old_one_out_of_the_slot(self):
        self.own(self.REVOLVER_R1, self.REVOLVER_R2)
        self.equip(self.REVOLVER_R1)
        self.equip(self.REVOLVER_R2)
        self.assertEqual([self.REVOLVER_R2],
                         account_store.equipped_items(self.account()))

    def test_every_equip_packet_gets_exactly_one_answer(self):
        """★★ §24 那条最要命的：客户端拿 `0x0604` 当计数器。

        换装时它先逐件发 `0x0703` 再发一发 `0x0702`，`[ShopStage+0x13c]`
        数着还差几发。**少回一发，仓库界面就永远不刷新**（玩家看到
        「点了没反应」），多回一发则会提前刷新。所以「一发一答」这条
        不能只在成功路径上成立。
        """
        self.own(self.REVOLVER_R1)
        cases = [
            ("穿上已有的", self.equip, self.REVOLVER_R1),
            ("脱下已有的", self.unequip, self.REVOLVER_R1),
            ("脱下没穿的", self.unequip, self.REVOLVER_R2),
            ("穿上仓库里没有的", self.equip, self.REVOLVER_R2),
            ("穿上客户端不认识的", self.equip, 9999999),
        ]
        for label, action, item_id in cases:
            frames = action(item_id)
            opcodes = [opcode for opcode, _ in frames]
            # ★ 判据是「**正好一发** 0x0604」——多一发和少一发同样坏。
            self.assertEqual(1, opcodes.count(gameserver.OP_REP_EQUIPPED_LIST),
                             "%s: %s" % (label, opcodes))

    def test_a_successful_change_also_resends_the_battle_bonus_packet(self):
        # ★ 战斗加成的唯一来源是 `0x030b`（§1 / §16）；`0x0604` 只管商店界面。
        #   改完装备两发都要发，不然「界面变了、打起来没变」。
        self.own(self.REVOLVER_R1)
        self.assertIn(gameserver.OP_SLOT_EQUIPPED_LIST,
                      [opcode for opcode, _ in self.equip(self.REVOLVER_R1)])

    def test_a_malformed_equip_packet_still_gets_an_answer(self):
        # 解不开也要回 —— 客户端那个计数器不认「服务端没听懂」。
        frames = self.send(gameserver.OP_REQ_EQUIP_ITEM, b"\x01\x02")
        self.assertEqual([gameserver.OP_REP_EQUIPPED_LIST],
                         [opcode for opcode, _ in frames])

    def test_the_equipped_list_reply_carries_only_what_is_worn(self):
        """★ `0x0604` 发的是**穿在身上的**，不是全部持有物（§29）。

        处理器 `0x447278` 把清单里每件可装备的都 `Equip` 进
        `[ShopStage+0x134]` —— 那是左侧人物模型的数据源。发全部仓库的话，
        玩家会看到自己同时穿着所有装备。仓库里「我有什么」走 `0x0601`。
        """
        self.own(self.REVOLVER_R1, self.REVOLVER_R2)
        frames = self.equip(self.REVOLVER_R1)
        bodies = [body for opcode, body in frames
                  if opcode == gameserver.OP_REP_EQUIPPED_LIST]
        self.assertEqual([self.REVOLVER_R1],
                         sorted(parse_rep_equipped_list(bodies[0])))


class ShopComposeTests(unittest.TestCase):
    """`0x0605` 配方列表 / `0x0606` 合成（V0.3商店 M7，协议见 §27 / §33）。

    ★ 这一组守的是三件事：

    1. **配方列表前面要有物品定义** —— 产物**和每一种材料**都得喂，
       否则四个材料槽是没名字的空格子（`0x4475ad` 逐条查 ItemDB）；
    2. **合成是一次原子交易** —— 金币、材料、产物要么一起动要么都不动；
    3. **结果码要挑对** —— `0x0506` 只认 0/1/2/4 四档，别的一律
       「未知的错误」，跟没说一样。
    """

    ARMOR = 1010015              # 上衣，能穿、进得了背包
    OTHER_ARMOR = 1020001        # 下装（另一个部位，不抢槽）
    PIPE = 30018                 # 청동파이프 青铜管
    BEAD = 10001                 # 검은구슬 黑珠
    Args = CharacterUnlockTests.Args

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AccountStore(os.path.join(self.tmp.name, "accounts.json"))
        self.store.register("tester", "pw")
        saved = gameserver.eventlog.online
        gameserver.eventlog.online = lambda _msg: None
        self.addCleanup(setattr, gameserver.eventlog, "online", saved)
        self.conn = CharacterUnlockTests.make_conn(self)
        self.conn.accounts = self.store
        self.conn.account = self.store.get_account("tester")[1]

    # -- 夹具 ---------------------------------------------------------------
    def account(self):
        return self.store.get_account("tester")[1]

    def stocked(self, money=10000, materials=None):
        self.store.add_quest_reward("tester", money=money)
        self.store.add_materials("tester",
                                 materials or {self.PIPE: 5, self.BEAD: 5})
        self.conn.account = self.account()

    def recipe(self, result=None, **over):
        entry = {"id": 1, "result": result or self.ARMOR, "cost": 500,
                 "listed": True, "level": 1,
                 "materials": [{"id": self.PIPE, "count": 2},
                               {"id": self.BEAD, "count": 1}]}
        entry.update(over)
        return entry

    def send(self, opcode, payload):
        self.conn.sent[:] = []
        gameserver.Conn.on_game_packet(self.conn, opcode, payload)
        return [take_frame(bytearray(f))[1:3] for f in self.conn.sent]

    #: 「상의 上衣」标签 —— `ARMOR` 的分类，也是玩家真会点的那一格。
    #: ★ 别用 `0` 当「全部」：那是「人物 → 英雄」标签的真 id（§22）。
    TAB = 0x10001

    def ask_list(self, **kw):
        kw.setdefault("category", self.TAB)
        return self.send(gameserver.OP_REQ_COMPOSITION_LIST,
                         shop.build_shop_list_request(**kw))

    def compose(self, item_id):
        return self.send(gameserver.OP_REQ_COMPOSE_ITEM,
                         struct.pack("<i", item_id))

    @staticmethod
    def result_code(frames):
        for opcode, body in frames:
            if opcode == gameserver.OP_REP_COMPOSE_ITEM:
                return struct.unpack("<i", body)[0]
        raise AssertionError("没有回 0x0506")

    @staticmethod
    def rules_of(frames):
        for opcode, body in frames:
            if opcode == gameserver.OP_REP_COMPOSITION_LIST:
                return parse_rep_composition_list(body)[2]
        raise AssertionError("没有回 0x0505")

    # -- 配方列表 -----------------------------------------------------------
    def test_the_recipe_list_comes_back_with_cost_and_materials(self):
        with recipe_config([self.recipe()]):
            frames = self.ask_list()
        self.assertEqual([{"result": self.ARMOR, "cost": 500,
                           "materials": [(self.PIPE, 2), (self.BEAD, 1)],
                           "days": 0}],
                         self.rules_of(frames))

    def test_the_definitions_cover_the_materials_too(self):
        """⚠ 处理器 `0x4475ad` 逐条拿**材料**的 id 去查 ItemDB —— 只喂产物的话
        四个材料槽是没名字的空格子（和 §28 同一条道理）。"""
        with recipe_config([self.recipe()]):
            frames = self.ask_list()
        defined = set()
        for opcode, body in frames:
            if opcode == gameserver.OP_REP_ITEM_INFO:
                defined.update(r["id"] for r in parse_rep_item_info(body)[0])
        self.assertLessEqual({self.ARMOR, self.PIPE, self.BEAD}, defined)
        # ★ 定义必须排在配方前面 —— 面板是在收到配方那一刻建的。
        opcodes = [opcode for opcode, _ in frames]
        self.assertLess(opcodes.index(gameserver.OP_REP_ITEM_INFO),
                        opcodes.index(gameserver.OP_REP_COMPOSITION_LIST))

    def test_a_high_level_recipe_is_still_listed_to_a_low_level_player(self):
        """★★ D27（2026-09-05 实机推翻的那一版）：原版合成**没有等级门** ——
        面板不读玩家等级，`0x0506` 也没有「等级太低」这一档。假账号是 1 级，
        配方上写着 99 也照样列出来；穿不穿得上由客户端在**装备**时判。"""
        self.assertEqual(1, account_store.player_level(self.account()))
        with recipe_config([self.recipe(level=99)]):
            self.assertEqual(1, len(self.rules_of(self.ask_list())))

    def test_a_high_level_recipe_can_still_be_composed(self):
        self.stocked()
        with recipe_config([self.recipe(level=99)]):
            frames = self.compose(self.ARMOR)
        self.assertEqual(shop.COMPOSE_OK, self.result_code(frames))

    def test_an_empty_recipe_table_still_reports_one_page(self):
        # 总页数发 0 会让客户端把当前页夹成 -1（`0x45efb9`）。
        with recipe_config([]):
            frames = self.ask_list()
        for opcode, body in frames:
            if opcode == gameserver.OP_REP_COMPOSITION_LIST:
                self.assertEqual((1, 0, []), parse_rep_composition_list(body))

    def test_a_malformed_list_request_is_not_guessed(self):
        self.assertEqual([], self.send(gameserver.OP_REQ_COMPOSITION_LIST,
                                       b"\x00\x01\x02"))

    def test_category_zero_is_not_a_wildcard(self):
        """⚠⚠ `0` 是「人物 → 英雄」标签的真 id（§22）—— 拿它当通配的后果
        就是 2026-09-05 实机看到的「点英雄结果列出一堆武器」。合成面板同理。"""
        with recipe_config([self.recipe()]):
            self.assertEqual([], self.rules_of(self.ask_list(category=0)))
            self.assertEqual(1, len(self.rules_of(self.ask_list())))

    # -- 合成 ---------------------------------------------------------------
    def test_composing_takes_the_money_and_materials_and_gives_the_item(self):
        self.stocked()
        with recipe_config([self.recipe()]):
            frames = self.compose(self.ARMOR)
        self.assertEqual(shop.COMPOSE_OK, self.result_code(frames))
        account = self.account()
        self.assertEqual(9500, account_store.player_money(account))
        self.assertEqual({self.PIPE: 3, self.BEAD: 4},
                         account_store.material_counts(account))
        self.assertTrue(account_store.has_item(account, self.ARMOR))

    def test_the_result_packet_comes_last(self):
        """⚠⚠ 和买东西同一条理由（§30②）：结果框一弹，玩家眼里这笔交易就
        结束了 —— 那一刻仓库、金币、配方页都得已经是新的。"""
        self.stocked()
        with recipe_config([self.recipe()]):
            self.ask_list()                      # 先让服务端记下当前是哪一页
            frames = self.compose(self.ARMOR)
        opcodes = [opcode for opcode, _ in frames]
        self.assertEqual(gameserver.OP_REP_COMPOSE_ITEM, opcodes[-1])
        for earlier in (gameserver.OP_REP_INVENTORY, gameserver.OP_REP_MONEY,
                        gameserver.OP_REP_COMPOSITION_LIST):
            self.assertIn(earlier, opcodes[:-1])

    def test_the_recipe_page_is_replayed_only_if_the_client_asked_for_one(self):
        """★ 重放的是客户端**真发过**的那一页 —— 没发过就不发，
        别自己猜一页塞给它（玩家会被莫名其妙翻到别的分类去）。"""
        self.stocked()
        with recipe_config([self.recipe()]):
            frames = self.compose(self.ARMOR)     # 没先要过配方列表
        self.assertNotIn(gameserver.OP_REP_COMPOSITION_LIST,
                         [opcode for opcode, _ in frames])

    def test_the_new_item_is_in_the_inventory_packet(self):
        # 「合成成功。请在我的仓库里装备道具。」—— 那一刻仓库里就得有它。
        self.stocked()
        with recipe_config([self.recipe()]):
            frames = self.compose(self.ARMOR)
        bodies = [body for opcode, body in frames
                  if opcode == gameserver.OP_REP_INVENTORY]
        self.assertIn(self.ARMOR, [e[0] for e in parse_rep_inventory(bodies[-1])])

    def test_not_enough_materials_changes_nothing(self):
        self.stocked(materials={self.PIPE: 1})
        with recipe_config([self.recipe()]):
            frames = self.compose(self.ARMOR)
        self.assertEqual(shop.COMPOSE_NO_MATERIAL, self.result_code(frames))
        account = self.account()
        self.assertEqual(10000, account_store.player_money(account))
        self.assertEqual({self.PIPE: 1}, account_store.material_counts(account))
        self.assertFalse(account_store.has_item(account, self.ARMOR))

    def test_not_enough_money_changes_nothing(self):
        self.stocked(money=100)
        with recipe_config([self.recipe()]):
            frames = self.compose(self.ARMOR)
        self.assertEqual(shop.COMPOSE_NO_MONEY, self.result_code(frames))
        self.assertEqual({self.PIPE: 5, self.BEAD: 5},
                         account_store.material_counts(self.account()))

    def test_composing_something_you_already_have_is_refused(self):
        self.stocked()
        self.store.add_item("tester", self.ARMOR)
        self.conn.account = self.account()
        with recipe_config([self.recipe()]):
            frames = self.compose(self.ARMOR)
        self.assertEqual(shop.COMPOSE_ALREADY_OWNED, self.result_code(frames))
        self.assertEqual(10000, account_store.player_money(self.account()))

    def test_a_recipe_the_admin_turned_off_cannot_be_composed(self):
        """⚠ 管理页关掉一条 = 界面上看不到它 —— 那就不该还能靠一发手搓的
        `0x0606` 合出来。"""
        self.stocked()
        with recipe_config([self.recipe(listed=False)]):
            frames = self.compose(self.ARMOR)
        self.assertEqual(shop.COMPOSE_UNKNOWN, self.result_code(frames))
        self.assertFalse(account_store.has_item(self.account(), self.ARMOR))

    def test_an_item_with_no_recipe_at_all_is_refused(self):
        self.stocked()
        with recipe_config([self.recipe()]):
            frames = self.compose(self.OTHER_ARMOR)
        self.assertEqual(shop.COMPOSE_UNKNOWN, self.result_code(frames))

    def test_a_malformed_compose_packet_still_gets_an_answer(self):
        """不回 = 玩家点了确定界面**什么都不发生**，比「未知的错误」更难查
        （购买那边 §30 已经栽过一次）。"""
        frames = self.send(gameserver.OP_REQ_COMPOSE_ITEM, b"\x01\x02")
        self.assertEqual([gameserver.OP_REP_COMPOSE_ITEM],
                         [opcode for opcode, _ in frames])
        self.assertEqual(shop.COMPOSE_UNKNOWN, self.result_code(frames))

    def test_zero_is_success_so_the_fallback_must_not_be_zero(self):
        """★★ 这一发和 `0x0502` 极性相反：那边 0 是「未定义的错误」，
        这边 **0 是「合成成功」** —— 兜底填 0 会让玩家看到「合成成功」
        然后发现仓库里什么都没有。"""
        self.stocked()
        with recipe_config([self.recipe(listed=False)]):
            self.assertNotEqual(shop.COMPOSE_OK,
                                self.result_code(self.compose(self.ARMOR)))

    def test_the_cost_comes_from_recipe_json_not_from_the_packet(self):
        # 上行只有一个产物 id，花费是我们自己查的 ⇒ 管理页改完立刻生效。
        self.stocked()
        with recipe_config([self.recipe(cost=7777)]):
            self.compose(self.ARMOR)
        self.assertEqual(10000 - 7777,
                         account_store.player_money(self.account()))


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

    def make_conn(self):
        conn = gameserver.Conn.__new__(gameserver.Conn)
        conn.addr = ("::ffff:127.0.0.1", 40000)
        conn.connected_at = time.monotonic()
        conn.online = lambda _msg: None
        conn.online_debug = lambda _msg: None
        conn.args = self.Args()
        conn.sock = self.FakeSocket()
        conn.cout = SimpleCipher.server_to_client()
        conn.send_lock = threading.RLock()
        conn.send_queue = None
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

    def writes(self, conn):
        """假 socket 上的写。**先把发送队列排空**（D108：写发生在另一条线程上）。

        ★★ 「一批就是一次写」的断言仍然成立：`send_batch()` 攒出来的那一份
        是 `solo` 的，发送线程**不许**把别的包并进去（V0.3 §166）。
        反过来，几发**独立**的 `send()` 会不会被并成一次写，取决于发送线程
        什么时候醒 —— 那是刻意换来的（§166 / D125），所以下面不再有任何
        「n 发独立的 send 就该有 n 次写」的断言。
        """
        conn.flush_outbox(timeout=5.0)
        return conn.sock.writes

    def decrypted(self, conn):
        """把假 socket 上的所有写按顺序解回明文。"""
        cin = SimpleCipher.server_to_client()
        return cin.decrypt(b"".join(self.writes(conn)))

    def test_a_batch_leaves_the_socket_as_exactly_one_write(self):
        conn = self.make_conn()
        with gameserver.Conn.send_batch(conn):
            conn.send(build_game(0x0200, build_rep_list_session()))
            conn.send(build_game(0x0201, build_rep_create_session(1)))
        self.assertEqual(1, len(self.writes(conn)))
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
        self.assertEqual(1, len(self.writes(batched)))
        self.assertEqual(b"".join(self.writes(one_by_one)),
                         b"".join(self.writes(batched)))

    def test_an_empty_batch_writes_nothing(self):
        conn = self.make_conn()
        with gameserver.Conn.send_batch(conn):
            pass
        self.assertEqual([], self.writes(conn))

    def test_the_batch_is_flushed_even_if_the_block_raises(self):
        # 中途抛异常也要把已经攒下的字节发出去：漏发比早发危险得多
        # （客户端会一直等那个应答）。
        conn = self.make_conn()
        with self.assertRaises(RuntimeError):
            with gameserver.Conn.send_batch(conn):
                conn.send(build_game(0x0200, build_rep_list_session()))
                raise RuntimeError("boom")
        self.assertEqual(1, len(self.writes(conn)))
        self.assertEqual([0x0200], self.frames_of(self.decrypted(conn)))

    def test_a_nested_batch_does_not_flush_early(self):
        conn = self.make_conn()
        with gameserver.Conn.send_batch(conn):
            conn.send(build_game(0x0200, build_rep_list_session()))
            with gameserver.Conn.send_batch(conn):
                conn.send(build_game(0x0201, build_rep_create_session(1)))
            self.assertEqual([], self.writes(conn))
        self.assertEqual(1, len(self.writes(conn)))
        self.assertEqual([0x0200, 0x0201], self.frames_of(self.decrypted(conn)))

    def test_a_plain_send_outside_a_batch_still_goes_out_immediately(self):
        conn = self.make_conn()
        conn.send(build_game(0x0200, build_rep_list_session()))
        self.assertEqual(1, len(self.writes(conn)))

    # -- 真正要保住的两条链 -------------------------------------------------
    def test_the_whole_room_burst_goes_out_in_one_write(self):
        # ★ 这就是「进房间偶尔只剩 3 个角色」的修法本体。
        conn = self.make_conn()
        request = (w_wstr("想和做朋友吗?") + w_wstr("") + w_wstr("")
                   + w_i32(0) + w_i32(2) + w_i32(3) + w_i32(1))
        gameserver.Conn.on_game_packet(conn, 0x0201, request)
        self.assertEqual(1, len(self.writes(conn)))
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
        conn.solo_quest = gameserver.RoomQuest()
        gameserver.Conn.leave_game_result(conn)
        self.assertEqual(1, len(self.writes(conn)))
        opcodes = self.frames_of(self.decrypted(conn))
        self.assertEqual(gameserver.OP_LOADING_DONE, opcodes[0])
        self.assertEqual(OP_SLOT_EQUIPPED_LIST, opcodes[-1])



class StuckClientIsolationTests(SendBatchTests):
    """★★★★★ **一个人卡死不许影响别人**（D108）。

    以前 `Conn.send()` 是「加密 + 写 socket」一体的，而写 socket 带
    `GAME_SEND_DEADLINE_S`（8 秒）的截止时间。房间那条 32 ms 循环发一发
    bot 心跳要遍历房里每一个人 —— 排在卡死那个人**后面**的所有人都被一起
    堵住，整个房间的 bot 冻 8 秒，那一段的 `rpExplode` 全部迟到（§147）。

    现在写 socket 是每条连接**自己那条发送线程**的活。
    """

    class BlockingSocket:
        """一个**永远收不走字节**的假 socket（卡在 WER 弹窗上的客户端就是这样）。"""

        def __init__(self):
            self.gate = threading.Event()
            #: 发送线程**进到** `sendall` 了没有 —— 用例拿它当事件等，
            #: 不拿 sleep 猜（铁律 10）。
            self.entered = threading.Event()
            self.writes = []
            self.closed = False

        def sendall(self, data):
            self.entered.set()
            self.gate.wait(10.0)
            self.writes.append(bytes(data))

        def shutdown(self, _how):
            self.closed = True

        def close(self):
            self.closed = True

    class TimeoutSocket:
        """写永远超时的假 socket（`send_all_bounded` 对真 socket 的那条路）。"""

        def __init__(self):
            self.writes = []
            self.closed = False

        def sendall(self, _data):
            raise socket.timeout("send deadline exceeded")

        def shutdown(self, _how):
            self.closed = True

        def close(self):
            self.closed = True

    class ClosableSocket(SendBatchTests.FakeSocket):
        """`make_conn` 那份假 socket 没有 `shutdown`/`close` —— 补上，
        拆流和关连接那两条路才走得通。"""

        def __init__(self):
            super().__init__()
            self.closed = False

        def shutdown(self, _how):
            self.closed = True

        def close(self):
            self.closed = True

    def stuck_conn(self):
        conn = self.make_conn()
        conn.sock = self.BlockingSocket()
        return conn

    def closable_conn(self):
        conn = self.make_conn()
        conn.sock = self.ClosableSocket()
        return conn

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.002)
        return False

    # -- 核心：发送方不再被卡死的对端拖住 ------------------------------------

    def test_send_returns_at_once_even_though_the_client_never_reads(self):
        """★★★ 这条就是问题本身：`send()` 当场返回，不等那个卡死的 socket。"""
        conn = self.stuck_conn()
        started = time.monotonic()
        conn.send(build_game(0x0200, build_rep_list_session()))
        self.assertLess(time.monotonic() - started, 1.0,
                        "send() 被卡死的客户端堵住了")
        self.assertEqual([], conn.sock.writes, "这会儿还没写出去才对")
        conn.sock.gate.set()
        self.assertTrue(self.wait_for(lambda: len(conn.sock.writes) == 1))

    def test_a_stuck_client_does_not_hold_up_anybody_else(self):
        """★★★ A 卡死时给 B 发包，B 立刻就拿得到。"""
        stuck = self.stuck_conn()
        healthy = self.make_conn()
        stuck.send(build_game(0x0200, build_rep_list_session()))
        started = time.monotonic()
        healthy.send(build_game(0x0200, build_rep_list_session()))
        self.assertTrue(self.wait_for(lambda: len(healthy.sock.writes) == 1))
        self.assertLess(time.monotonic() - started, 1.0,
                        "健康的那条被卡死的那条拖住了")
        self.assertEqual([], stuck.sock.writes)
        stuck.sock.gate.set()

    def test_the_order_survives_the_queue(self):
        """★ 排队不许打乱顺序 —— `SimpleCipher` 是逐字节流密码，错一次全废。

        ★ 断言的是**字节流**，不是「写了几次」：后两发会被发送线程并成
        一次写（§166），而那正是要的。
        """
        conn = self.stuck_conn()
        for opcode in (0x0200, 0x0201, 0x0200):
            conn.send(build_game(opcode, build_rep_list_session()
                                 if opcode == 0x0200
                                 else build_rep_create_session(1)))
        conn.sock.gate.set()
        self.assertTrue(self.wait_for(
            lambda: self.frames_of(self.decrypted(conn))
            == [0x0200, 0x0201, 0x0200]))

    # -- 收口：写不出去的连接照旧被拆掉 --------------------------------------

    def test_a_write_that_times_out_still_kills_the_stream(self):
        """★ 处置一个字没改：写超时 = 密码流已错位 ⇒ 拆连接让客户端重连。"""
        conn = self.make_conn()
        conn.sock = self.TimeoutSocket()
        conn.send(build_game(0x0200, build_rep_list_session()))
        self.assertTrue(self.wait_for(lambda: conn.send_broken))
        self.assertTrue(conn.sock.closed, "该把 socket 拆掉")

    def test_a_slow_reader_that_never_catches_up_is_killed_too(self):
        """★★ 每次都勉强写进去几个字节、积压却一直涨的那种连接。

        判据和写超时是**同一个**：「这个客户端 8 秒没把我们的字节收走」——
        不是另拍一个内存上限。
        """
        conn = self.stuck_conn()
        # 第一发被发送线程取走、卡在 `sendall` 里；第二发就留在队列里了。
        conn.send(build_game(0x0200, build_rep_list_session()))
        self.assertTrue(conn.sock.entered.wait(5.0), "发送线程没起来")
        conn.send(build_game(0x0201, build_rep_create_session(1)))
        self.assertTrue(conn.outbox, "第二发该还在队列里")
        # 把「队列从空变非空」的时刻拨到 8 秒之前 = 积压了这么久还没走掉。
        conn.outbox_since -= gameserver.GAME_SEND_DEADLINE_S + 1.0
        conn.send(build_game(0x0200, build_rep_list_session()))
        self.assertTrue(conn.send_broken, "积压太久该拆流")
        conn.sock.gate.set()

    def test_broken_streams_take_nothing_more(self):
        """流已废之后再 `send()` 就是空操作（和 D108 之前一样）。"""
        conn = self.closable_conn()
        conn.kill_stream("用例造的")
        conn.send(build_game(0x0200, build_rep_list_session()))
        self.assertEqual([], conn.sock.writes)

    # -- 收尾：关连接之前那一发要真的发出去 ----------------------------------

    def test_close_now_flushes_what_is_still_queued(self):
        """★★ 顶号 / 踢人 / 版本拒绝都是「先发一发说明，再关连接」——
        队列没排空就关，玩家什么提示都看不到（D108 之前是同步发，天然没这问题）。
        """
        conn = self.closable_conn()
        conn.send(build_game(0x0200, build_rep_list_session()))
        conn.close_now()
        self.assertEqual(1, len(conn.sock.writes), "关之前那一发丢了")


class SendCoalescingTests(StuckClientIsolationTests):
    """★★★★★ 发送线程**醒一次就把排着的一起写出去**（V0.3 §166 / D125）。

    以前一份入队一次写，一次写要两次系统调用、两次重抢 GIL。进程里只要有
    一条纯 Python 的计算线程在跑（`botplan` 翻到没算过的破坏物变体时一次
    `plan_result()` 实测 832 ms），发送线程就只能每 30~50 ms 走掉一个包；
    而战斗中光 bot 心跳就 40 包/秒 —— 实机量到投递延迟 p99 803 ms、
    最大 847 ms，客户端那头一次只收到一个 48/53 字节的包。

    这里用「卡住的 socket」把时序钉死：第一发被发送线程取走卡在 `sendall`
    里，后面几发就都排在队列上；放开之后它们必须**一次**写出去。
    """

    def queued_behind_the_first(self, conn, sends):
        """第一发卡在 `sendall` 里时，把 `sends` 全排进队列。"""
        self.assertTrue(conn.sock.entered.wait(5.0), "发送线程没起来")
        for send in sends:
            send()
        self.assertEqual(len(sends), len(conn.outbox), "该都还排着")

    def test_everything_queued_behind_a_slow_write_goes_out_in_one_write(self):
        conn = self.stuck_conn()
        conn.send(build_game(0x0200, build_rep_list_session()))
        self.queued_behind_the_first(conn, [
            lambda: conn.send(build_game(0x0201, build_rep_create_session(1))),
            lambda: conn.send(build_game(0x0200, build_rep_list_session())),
            lambda: conn.send(build_game(0x0201, build_rep_create_session(1))),
        ])
        conn.sock.gate.set()
        self.assertTrue(self.wait_for(lambda: len(conn.sock.writes) == 2),
                        "排在后面那三份该并成一次写")
        self.assertEqual([0x0200, 0x0201, 0x0200, 0x0201],
                         self.frames_of(self.decrypted(conn)))

    def test_merging_does_not_change_a_single_byte(self):
        """★ 合并只改变「写了几次」——`SimpleCipher` 是逐字节流密码，
        字节错一个客户端就再也解不回来了。"""
        packets = [build_game(0x0200, build_rep_list_session()),
                   build_game(0x0201, build_rep_create_session(1)),
                   build_game(OP_SLOT_EQUIPPED_LIST,
                              build_slot_equipped_list(0, [101400001]))]
        expected = SimpleCipher.server_to_client().encrypt(b"".join(packets))
        conn = self.stuck_conn()
        conn.send(packets[0])
        self.queued_behind_the_first(
            conn, [lambda: conn.send(packets[1]), lambda: conn.send(packets[2])])
        conn.sock.gate.set()
        self.assertTrue(self.wait_for(
            lambda: b"".join(conn.sock.writes) == expected))

    def test_a_batch_is_never_merged_with_its_neighbours(self):
        """★★★ D058 要的是「整批一次写」，不是「往批里再掺东西」。

        变异验证：把 `send_batch()` 那一份的 `solo=True` 去掉，这一条当场红。
        """
        conn = self.stuck_conn()
        conn.send(build_game(0x0200, build_rep_list_session()))
        self.assertTrue(conn.sock.entered.wait(5.0), "发送线程没起来")
        with gameserver.Conn.send_batch(conn):
            conn.send(build_game(0x0201, build_rep_create_session(1)))
            conn.send(build_game(OP_SLOT_EQUIPPED_LIST,
                                 build_slot_equipped_list(0, [101400001])))
        conn.send(build_game(0x0200, build_rep_list_session()))
        conn.sock.gate.set()
        self.assertTrue(self.wait_for(lambda: len(conn.sock.writes) == 3),
                        "批该自己占一次写，前后都不许并进来")
        self.assertEqual([0x0200, 0x0201, OP_SLOT_EQUIPPED_LIST, 0x0200],
                         self.frames_of(self.decrypted(conn)))

    # -- 诊断：判据是「躺了多久」，不是「并走几份」（§184）-------------------

    @staticmethod
    def waits_of(conn):
        return [line for line in conn.logged if "发送队列滞留新高" in line]

    @staticmethod
    def waited_ms(line):
        return int(line.split("躺了 ")[1].split(" ms")[0])

    def age_the_queue(self, conn, ages):
        """把还排着的那几份的入队时刻**往前挪** `ages` 秒（队首在前）。

        挪时刻而不是真等：用例的结论不该取决于这台机器跑得多快（铁律 10）。
        顺带把纪录和日志复位 —— 第一份出队时发送线程刚起来，那点启动开销
        不是积压。

        ⚠ 下面的断言一律**留出取整余量**（挪 50 只断言 ≥40）：报出来的数是
          `int(秒差 × 1000)`，向下取整 + 浮点误差 ⇒ 挪 0.120 可能量成 119。
          用例要钉的是**量级**（几十毫秒 vs 零），不是那一个毫秒。
        """
        with conn.outbox_cv:
            self.assertEqual(len(ages), len(conn.outbox), "该都还排着")
            now = time.monotonic()
            for i, ((wire, solo, _), age) in enumerate(zip(conn.outbox, ages)):
                conn.outbox[i] = (wire, solo, now - age)
        conn.logged.clear()
        conn.send_wait_max_ms = 0

    def test_a_burst_queued_in_one_instant_is_not_reported_as_backlog(self):
        """★★★★★ 这次 bug 的回归（V0.3 §184）：**份数不是积压的证据**。

        实机日志里报出来的三次「新高」，字节数一个不差地对上了三处
        **同一毫秒里连着 `send()`** 的批量下发 ——
        局末结算 12 份 936 字节（6 份 `gspRepGameResult` 90 + 6 份
        `gspEndGame` 66）、`/a` 一次加 5 个 bot 11 份 676 字节
        （5 ×（座位加入 64 + 系统提示 44）+ 汇总提示 136）、登录补发 4 份。
        这些包在队里**一份都没躺过**，发送线程一醒来就一次全走 ——
        那正是 D125 想要的行为，不是积压。

        变异验证：把 `_note_send_wait` 换回按份数记纪录，这一条当场红。
        """
        conn = self.closable_conn()
        conn._note_send_wait(time.monotonic(), 12, 936)
        conn._note_send_wait(time.monotonic(), 11, 676)
        self.assertEqual([], self.waits_of(conn))

    def test_a_single_packet_that_sat_in_the_queue_is_reported(self):
        """★★★ 老诊断的**漏报**：§166 的原始现象是「一次只走掉一个包、
        间隔 30~50 ms」—— 份数恒为 1，只看份数的诊断对它一声不吭。

        变异验证：把 `_note_send_wait` 挪回「并走 >1 份才记」，这一条当场红。
        """
        conn = self.stuck_conn()
        conn.send(build_game(0x0200, build_rep_list_session()))
        self.queued_behind_the_first(conn, [
            lambda: conn.send(build_game(0x0201, build_rep_create_session(1))),
        ])
        self.age_the_queue(conn, [0.050])
        conn.sock.gate.set()
        self.assertTrue(self.wait_for(lambda: len(conn.sock.writes) == 2))
        waits = self.waits_of(conn)
        self.assertEqual(1, len(waits), waits)
        self.assertGreaterEqual(self.waited_ms(waits[0]), 40)
        self.assertIn("1 份", waits[0])

    def test_the_send_wait_high_water_mark_is_logged_once_per_record(self):
        """★ 诊断按**刷新纪录**去重（铁律 10：状态翻转，不是次数 / 时间窗）。"""
        conn = self.closable_conn()
        conn._note_send_wait(time.monotonic() - 0.050, 2, 120)
        conn._note_send_wait(time.monotonic() - 0.010, 9, 700)   # 更浅，不打
        conn._note_send_wait(time.monotonic() - 0.120, 1, 53)    # 更深，打
        waits = self.waits_of(conn)
        self.assertEqual(2, len(waits), waits)
        self.assertGreaterEqual(self.waited_ms(waits[1]), 100)

    def test_the_wait_is_measured_from_the_head_of_the_queue(self):
        """★★ 接线：量的是**队首**那份的入队时刻 —— FIFO ⇒ 它躺得最久。

        队首挪早 200 ms、后面那份只挪早 20 ms：报出来的必须是 200 那个。

        变异验证：改成拿 `time.monotonic()` 当入队时刻、或者取并走的最后
        一份，这一条都当场红。
        """
        conn = self.stuck_conn()
        conn.send(build_game(0x0200, build_rep_list_session()))
        self.queued_behind_the_first(conn, [
            lambda: conn.send(build_game(0x0201, build_rep_create_session(1))),
            lambda: conn.send(build_game(0x0200, build_rep_list_session())),
        ])
        self.age_the_queue(conn, [0.200, 0.020])
        conn.sock.gate.set()
        self.assertTrue(self.wait_for(lambda: len(conn.sock.writes) == 2))
        waits = self.waits_of(conn)
        self.assertEqual(1, len(waits), waits)
        self.assertGreaterEqual(self.waited_ms(waits[0]), 150)   # 队尾那份是 20
        self.assertIn("2 份", waits[0])     # 份数还在，只是降级成旁证


if __name__ == "__main__":
    unittest.main()
