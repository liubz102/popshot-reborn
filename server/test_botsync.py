#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`botsync.py` + bot 战斗帧的用例 —— V0.3 M3。

分两大块：

* **线格式**（不需要房间）：合成出来的字节和真客户端发的是不是同一个形状。
  这一块拿 `bug调查/server_logs/game_003_27799.dec.bin` 里**真包的十六进制**
  当基准（下面的 `REAL_*`）—— 语料文件不进仓库，但抄三发出来当常量是值得的：
  它把「我们以为的布局」和「客户端真的发的字节」钉在一起。
* **战斗帧**（要房间）：bot 到底动没动、序号有没有乱、会不会连累真人。

★ 三条不变式（D5）在这里各有一组用例。它们防的是「伤害数字照出、血照掉
一丝、就是打不死人」那种 bug —— 症状离原因十万八千里，只能在生成的那一刻拦。
"""
from __future__ import annotations

import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import bot                                                     # noqa: E402
import botsync                                                 # noqa: E402
import gameserver                                              # noqa: E402
import relayserver                                             # noqa: E402
import udpsync                                                 # noqa: E402
from gameserver import OP_LEAVE_SESSION, OP_PEER_DATA_DOWN, \
    OP_PEER_DATA_UP                                            # noqa: E402
from test_battle import opcodes                                # noqa: E402
from test_bot import BotBattleRoom                             # noqa: E402

# ---------------------------------------------------------------------------
# 真客户端发出来的三发包（`game_003_27799.dec.bin`，`0x040e` 的载荷）
# ---------------------------------------------------------------------------
#: 座位 1 的一发心跳：局号 0、序号 0、内层 `0x4001`、body 31 字节。
REAL_HEARTBEAT = bytes.fromhex(
    "ff01ff0000009935000001400000010000000196005e01"
    "0000000000390000010000000000c800c1001a00")
#: 同一个座位的下一发（只有 Y 和速度变了）—— 用来核对「哪几格会动」。
REAL_HEARTBEAT_NEXT = bytes.fromhex(
    "ff01ff0000000f44000001400000010000000196005f01"
    "0000010000390000010000000000c800c1001a00")
#: 座位 0 的一发 `rpFire`：局号 1、序号 5、内层 `0x0002`、body 26 字节。
REAL_FIRE = bytes.fromhex(
    "ff00ff000100354b050002000a01244a0f000000f143"
    "00801044f9425abd0000044301000000")


def header(packet):
    """`UdpPacket` 的 12 字节头拆开：magic / 发送方 / 目标 / 填充 / 局号 /
    校验和 / 序号 / 内层 opcode。"""
    magic, sender, target, pad = struct.unpack_from("<BbbB", packet, 0)
    game_id, checksum, sequence, opcode = struct.unpack_from("<HHHH", packet, 4)
    return dict(magic=magic, sender=sender, target=target, pad=pad,
                game_id=game_id, checksum=checksum, sequence=sequence,
                opcode=opcode)


def body_of(packet):
    return packet[udpsync.PEER_HEADER_SIZE:]


class FakeBot:
    """只有「座位号 + 局号」的最小 bot —— 线格式的用例不需要房间。"""

    def __init__(self, seat=2, game_id=0):
        self.my_seat = seat
        self.peer_epoch = relayserver.PeerEpoch()
        self.peer_epoch.value = game_id


# ---------------------------------------------------------------------------
# 校验和
# ---------------------------------------------------------------------------
class ChecksumTests(unittest.TestCase):
    """`0x5bbdc1`：种子 `0x17`、乘数 `0x103`、逐字节**带符号**、只覆盖 body。

    ★ 算错的后果是真客户端在收包入口 `0x4078ab` 就把整包丢掉，连队列都进不去
    —— 表现和「服务端根本没发」完全一样（V0.2 会话 34 实机踩过）。
    """

    def test_it_reproduces_the_checksum_of_three_real_packets(self):
        for packet in (REAL_HEARTBEAT, REAL_HEARTBEAT_NEXT, REAL_FIRE):
            with self.subTest(packet=packet[:12].hex()):
                self.assertEqual(header(packet)["checksum"],
                                 botsync.udp_checksum(body_of(packet)))

    def test_bytes_above_127_are_sign_extended(self):
        """`movsx dx, byte [esi]` —— 无符号地算会得出完全不同的数。"""
        self.assertEqual((0x17 * 0x103 + (0xFF - 256)) & 0xFFFF,
                         botsync.udp_checksum(b"\xff"))

    def test_only_the_body_is_covered(self):
        """`lea esi,[edx+0xc]` —— 头一个字节都不进校验和。

        这正是 `relayserver` 能改写头 `+4` 的局号而不重算校验和的依据。
        """
        one = botsync.build_peer_packet(1, botsync.OP_HEARTBEAT, b"abc",
                                        game_id=7)
        other = botsync.build_peer_packet(4, botsync.OP_HEARTBEAT, b"abc",
                                          game_id=999)
        self.assertEqual(header(one)["checksum"], header(other)["checksum"])


# ---------------------------------------------------------------------------
# 头
# ---------------------------------------------------------------------------
class PeerHeaderTests(unittest.TestCase):

    def setUp(self):
        self.packet = botsync.build_peer_packet(
            3, botsync.OP_HEARTBEAT, b"\x00" * 31, game_id=5, sequence=0)

    def test_the_header_is_twelve_bytes(self):
        self.assertEqual(12, udpsync.PEER_HEADER_SIZE)
        self.assertEqual(12 + 31, len(self.packet))

    def test_it_is_always_a_broadcast(self):
        """三个原版发送点组包时也都写 `0xff`；语料 91526 发一个例外都没有。

        盖成座位号的话收方 `0x4078dd` 会判「这包不是发给我的」整包丢。
        """
        self.assertEqual(-1, header(self.packet)["target"])
        self.assertEqual(0xFF, self.packet[udpsync.PEER_TARGET_OFFSET])

    def test_the_magic_and_the_unknown_pad_match_the_real_client(self):
        self.assertEqual(0xFF, header(self.packet)["magic"])
        # `packet_api.md` 原来把 `+3` 标 ❓未知 —— 语料里 91526 发恒 0。
        self.assertEqual(0, header(self.packet)["pad"])
        self.assertEqual(0, header(REAL_HEARTBEAT)["pad"])

    def test_the_sender_seat_is_signed(self):
        """头 `+1` 是 i8（`0x4078c9` 是 signed 比较，有效范围 -1..5）。"""
        self.assertEqual(3, header(self.packet)["sender"])


# ---------------------------------------------------------------------------
# 心跳 body
# ---------------------------------------------------------------------------
class HeartbeatBodyTests(unittest.TestCase):
    """§24 / §25 的布局。真包摆在旁边，一格一格对。"""

    def test_the_body_is_exactly_thirty_one_bytes(self):
        self.assertEqual(31, botsync.HEARTBEAT_BODY_SIZE)
        self.assertEqual(31, len(body_of(REAL_HEARTBEAT)))
        state = botsync.character_state(0, 0)
        self.assertEqual(31, len(botsync.heartbeat_body(0, 1, state)))

    def test_the_fixed_head_matches_the_real_client(self):
        """`+0..1` N / `+2..5` 恒 1 / `+6` 座位号。"""
        real = body_of(REAL_HEARTBEAT)
        mine = botsync.heartbeat_body(0, 1, botsync.character_state(150, 350))
        self.assertEqual(real[:7], mine[:7])

    def test_the_seat_in_the_body_matches_the_one_in_the_header(self):
        """67186 发实测 100% 一致 —— 对不上收方就把状态写到别人身上了。"""
        self.assertEqual(header(REAL_HEARTBEAT)["sender"],
                         body_of(REAL_HEARTBEAT)[6])
        machine = FakeBot(seat=4)
        packet = botsync.BotSyncStream(machine).heartbeat(
            botsync.character_state(0, 0))
        self.assertEqual(header(packet)["sender"], body_of(packet)[6])

    def test_the_position_lands_on_bytes_seven_to_ten(self):
        """★★ §3 猜的是 `+25..28`，那是错的（§24 两条独立证据）。

        填错的症状是「bot 在别人屏幕上一动不动」，和「包压根没发出去」
        长得一模一样 —— 所以这条用例值一整轮排查。
        """
        packet = botsync.build_peer_packet(
            1, botsync.OP_HEARTBEAT,
            botsync.heartbeat_body(0, 1, botsync.character_state(-1234, 5678)),
            game_id=0)
        self.assertEqual((-1234, 5678), udpsync.heartbeat_position(packet))

    def test_a_real_packet_decodes_to_a_plausible_position(self):
        self.assertEqual((150, 350), udpsync.heartbeat_position(REAL_HEARTBEAT))
        self.assertEqual((150, 351),
                         udpsync.heartbeat_position(REAL_HEARTBEAT_NEXT))

    def test_only_position_and_velocity_move_between_two_real_packets(self):
        """真包相邻两发的 diff：只有位置和速度那几格在动。"""
        a, b = body_of(REAL_HEARTBEAT), body_of(REAL_HEARTBEAT_NEXT)
        moved = {i for i in range(31) if a[i] != b[i]}
        self.assertEqual({9, 13}, moved)          # +9 = Y 低位、+13 = vy 低位

    def test_facing_lands_in_the_low_two_bits_of_the_bitfield(self):
        """位域 `+19..22`：低 2 位是 `[char+0x2d0]`，收方按 2 位有符号还原。"""
        right = botsync.character_state(0, 0, facing=botsync.FACING_RIGHT)
        left = botsync.character_state(0, 0, facing=botsync.FACING_LEFT)
        self.assertEqual(1, struct.unpack_from("<i", right, 12)[0] & 0x03)
        self.assertEqual(3, struct.unpack_from("<i", left, 12)[0] & 0x03)

    def test_bit_two_is_set_like_the_common_real_case(self):
        """bit2 = `[char+0x128]`：它决定收方**插值**还是**硬置**坐标。

        真客户端绝大多数时候是 1（= 插值），我们照着来，别人看 bot 的观感
        才和看真人一致。
        """
        state = botsync.character_state(0, 0)
        self.assertTrue(struct.unpack_from("<i", state, 12)[0]
                        & botsync.HEARTBEAT_BIT_ONGROUND)

    def test_the_two_never_written_pads_are_zero(self):
        """结构 `+0x09` / `+0x16` 序列化器从没写过 —— 收方也不读，填 0。"""
        state = botsync.character_state(1, 2, vx=3, vy=4)
        self.assertEqual(0, state[9])
        self.assertEqual(b"\x00\x00", state[22:24])

    def test_coordinates_use_the_clients_truncate_toward_zero(self):
        """★ `0x5f895c` 是 MSVC 的 `_ftol2` —— **朝零截断**，不是四舍五入。

        `round()` 会给出 `2.7 -> 3`，客户端给的是 `2`。差 1 个单位看不出来，
        但既然逆清楚了就照着来。
        """
        self.assertEqual(2, botsync.clamp_i16(2.5))
        self.assertEqual(2, botsync.clamp_i16(2.7))
        self.assertEqual(-2, botsync.clamp_i16(-2.7))

    def test_coordinates_are_clamped_into_int16(self):
        self.assertEqual(32767, botsync.clamp_i16(1e9))
        self.assertEqual(-32768, botsync.clamp_i16(-1e9))
        state = botsync.character_state(1e9, -1e9)
        self.assertEqual((32767, -32768), struct.unpack_from("<hh", state, 0))


# ---------------------------------------------------------------------------
# 事件包 body
# ---------------------------------------------------------------------------
class EventBodyTests(unittest.TestCase):
    """§23 / `packet_api.md` §5.2~§5.4。长度和抓包实测逐个吻合。"""

    def test_fire_is_twenty_six_bytes_and_starts_with_ten_plus_the_seat(self):
        body = botsync.fire_body(3, 1002020, 482.0, 578.0, -0.05, 132.0)
        self.assertEqual(26, len(body))
        self.assertEqual(botsync.FIRE_SOURCE_PLAYER_BASE + 3, body[0])

    def test_fire_matches_a_real_client_packet_field_for_field(self):
        real = body_of(REAL_FIRE)
        self.assertEqual(26, len(real))
        mine = botsync.fire_body(
            header(REAL_FIRE)["sender"], 1002020, 482.0, 578.0,
            struct.unpack_from("<f", real, 14)[0], 132.0, slot=1, shots=1)
        self.assertEqual(real, mine)

    def test_explode_is_twenty_eight_bytes_and_carries_both_handles(self):
        body = botsync.explode_body(4242, 200001, 10.0, 20.0, hit_kind=2)
        self.assertEqual(28, len(body))
        self.assertEqual((4242, 200001), struct.unpack_from("<ii", body, 0))

    def test_jump_is_two_bytes_seat_then_stage(self):
        self.assertEqual(b"\x05\x02", botsync.jump_body(5, 2))

    def test_change_weapon_is_five_bytes_seat_then_weapon_id(self):
        body = botsync.change_weapon_body(2, 1002030)
        self.assertEqual(5, len(body))
        self.assertEqual((2, 1002030), struct.unpack_from("<Bi", body, 0))


# ---------------------------------------------------------------------------
# ★★ D5 的三条不变式
# ---------------------------------------------------------------------------
class InvariantTests(unittest.TestCase):
    """违反了会怎样：收方那个座位的**弹体句柄分配器永久错位** ——
    伤害数字照出、血照掉一丝、**就是打不死人**，一局之内不会自愈
    （V0.2 §216 / §217）。所以这三条是断言，不是注释。
    """

    def setUp(self):
        self.machine = FakeBot(seat=1, game_id=3)
        self.stream = botsync.BotSyncStream(self.machine)
        self.state = botsync.character_state(0, 0)

    def test_event_sequences_start_at_zero_and_never_skip(self):
        seen = [udpsync.peer_sequence(
            self.stream.event(botsync.OP_JUMP, botsync.jump_body(1)))
            for _ in range(5)]
        self.assertEqual([0, 1, 2, 3, 4], seen)

    def test_the_heartbeat_n_equals_the_number_of_events_already_sent(self):
        """★ N 报大了 = 收方 `FlushTo(N)` 把还没发的号直接判死。"""
        self.assertEqual(0, udpsync.heartbeat_next_event_seq(
            self.stream.heartbeat(self.state)))
        for expected in (1, 2, 3):
            self.stream.event(botsync.OP_JUMP, botsync.jump_body(1))
            self.assertEqual(expected, udpsync.heartbeat_next_event_seq(
                self.stream.heartbeat(self.state)))

    def test_the_heartbeat_never_gets_ahead_of_the_events(self):
        """连发一百发心跳也不许把 N 往前推一格。"""
        self.stream.event(botsync.OP_JUMP, botsync.jump_body(1))
        for _ in range(100):
            self.assertEqual(1, udpsync.heartbeat_next_event_seq(
                self.stream.heartbeat(self.state)))

    def test_the_heartbeat_sequence_field_is_always_zero(self):
        """头 `+8` 对心跳恒 0（语料 67186 发只有这一个取值）。"""
        self.assertEqual(0, udpsync.peer_sequence(
            self.stream.heartbeat(self.state)))

    def test_the_game_id_is_the_bots_own_epoch(self):
        """不变式 3 的前半段：盖自己那一代的号。

        后半段（按收件人重新盖章）是 `relayserver.deliver()` 的活，
        `test_relayserver.PeerEpochTests` 已经钉住了。
        """
        self.machine.peer_epoch.value = 9
        self.assertEqual(9, header(self.stream.heartbeat(self.state))["game_id"])

    def test_a_new_epoch_resets_the_event_counter(self):
        """换代 = 收方 `ResetQueues`，事件序号必须跟着回 0（§216 三）。

        ★ 判据是「局号真的变了」这个事实本身，不是「开局时记得调一下」。
        """
        for _ in range(4):
            self.stream.event(botsync.OP_JUMP, botsync.jump_body(1))
        self.assertEqual(4, self.stream.events)
        self.machine.peer_epoch.value += 1
        self.assertEqual(0, udpsync.peer_sequence(
            self.stream.event(botsync.OP_JUMP, botsync.jump_body(1))))

    def test_an_unchanged_epoch_keeps_counting(self):
        self.stream.event(botsync.OP_JUMP, botsync.jump_body(1))
        self.stream.heartbeat(self.state)
        self.assertEqual(1, udpsync.peer_sequence(
            self.stream.event(botsync.OP_JUMP, botsync.jump_body(1))))

    def test_a_heartbeat_opcode_is_refused_on_the_event_path(self):
        """内层 `>= 0x4000` 不进 `PktQueue`，给它编号是在制造假账。"""
        with self.assertRaises(botsync.SyncInvariantError):
            self.stream.event(botsync.OP_HEARTBEAT, b"")

    def test_a_wrong_length_heartbeat_body_blows_up(self):
        with self.assertRaises(botsync.SyncInvariantError):
            botsync.heartbeat_body(0, 1, b"\x00" * 23)

    def test_the_invariant_error_is_an_assertion_error(self):
        """★ 继承 `AssertionError` 是故意的，但**不用 `assert` 语句** ——
        `python -O` 会把整句删掉，而这三条恰恰不能被优化掉。"""
        self.assertTrue(issubclass(botsync.SyncInvariantError, AssertionError))


class DeliveryBookkeepingTests(unittest.TestCase):

    def setUp(self):
        self.stream = botsync.BotSyncStream(FakeBot())

    def test_it_counts_what_actually_reached_someone(self):
        self.assertEqual(2, self.stream.deliver(b"x", lambda _c, _p: 2))
        self.stream.deliver(b"x", lambda _c, _p: 0)
        self.assertEqual((1, 1), (self.stream.sent, self.stream.dropped))

    def test_a_stream_that_never_sent_anything_has_no_summary(self):
        self.assertIsNone(self.stream.summary())
        self.stream.deliver(b"x", lambda _c, _p: 1)
        self.assertIn("已发 1", self.stream.summary())


# ---------------------------------------------------------------------------
# 轨迹回放
# ---------------------------------------------------------------------------
class TrailPointTests(unittest.TestCase):
    """bot 的落脚点 = 真人走过的某个采样点（D16）。

    ★ 服务端**一点地图几何都没有**（M4 才有）。自己算一条路的话，bot 会
    走进地形里、掉出图外；踩真人刚站过的点则必然合法，连跳跃的抛物线都是
    真人真跳出来的。
    """

    def trail(self, *points):
        return [(x, y, jump) for x, y, jump in points]

    def test_an_empty_trail_gives_nothing(self):
        self.assertIsNone(bot.trail_point([], 100))

    def test_it_walks_back_along_the_path_by_the_requested_distance(self):
        trail = self.trail((0, 0, 0), (50, 0, 0), (100, 0, 0), (150, 0, 0))
        self.assertEqual((100, 0, 0), bot.trail_point(trail, 50))
        self.assertEqual((50, 0, 0), bot.trail_point(trail, 100))

    def test_a_short_trail_falls_back_to_its_oldest_point(self):
        """真人才刚进图、还没走几步时就是这种情况。"""
        trail = self.trail((10, 20, 0), (12, 20, 0))
        self.assertEqual((10, 20, 0), bot.trail_point(trail, 9999))

    def test_standing_still_keeps_the_bot_where_it_is(self):
        """真人站着不动 -> 轨迹上全是同一个点 -> bot 也停下。"""
        trail = self.trail(*[(7, 8, 0)] * 10)
        self.assertEqual((7, 8, 0), bot.trail_point(trail, 120))

    def test_the_jump_flag_comes_from_the_landing_point_itself(self):
        trail = self.trail((0, 0, 0), (30, 40, 2), (60, 0, 0), (90, 0, 0))
        self.assertEqual((30, 40, 2), bot.trail_point(trail, 60))

    def test_a_jump_further_ahead_is_not_reported_early(self):
        """★ 真人一起跳、还在 120 之外的 bot 不能立刻跟着跳。

        把走过的那一段 OR 起来就会这样 —— 早一秒多，看着像抽搐。
        """
        trail = self.trail((0, 0, 0), (30, 0, 0), (60, 40, 1), (90, 0, 0))
        self.assertEqual(0, bot.trail_point(trail, 60)[2])

    def test_diagonal_distance_counts_the_vertical_part_too(self):
        trail = self.trail((0, 0, 0), (0, 100, 0), (0, 200, 0))
        self.assertEqual((0, 100, 0), bot.trail_point(trail, 100))


# ---------------------------------------------------------------------------
# 战斗帧（真房间）
# ---------------------------------------------------------------------------
def peer_frames(conn):
    """这条连接收到的所有 `0x040f` 里那份 `UdpPacket`。"""
    out = []
    for plain in conn.sent:
        if len(plain) >= 10 and plain[0] == gameserver.MAGIC_GAME:
            if struct.unpack_from("<H", plain, 8)[0] == OP_PEER_DATA_DOWN:
                out.append(plain[10:])
    return out


def bot_frames(conn, seat):
    return [p for p in peer_frames(conn) if header(p)["sender"] == seat]


class BotFrameRoom(BotBattleRoom):
    """alice（房主，座位 0）+ bob（座位 1）+ 一个 bot（座位 2），已进关卡。

    ★ 每个用例自己喂真人的心跳 —— bot 的帧是**被真人的同步包驱动**的（D17），
    不喂就一帧都不该有。
    """

    def human_heartbeat(self, conn, x, y, jumped=0):
        """让 `conn` 发一发带位置的心跳（走真的 `0x040e` 入口）。

        ★ 先复位采样率闸门再喂 —— 用例要的是「每喂一发就看一帧」，
        而 `BOT_FRAME_INTERVAL_S` 在真时钟下会把它们全挡掉。
        """
        self.tick_forward()
        if jumped:
            gameserver.Conn.on_game_packet(conn, OP_PEER_DATA_UP, self.jump(
                conn, jumped))
        gameserver.Conn.on_game_packet(conn, OP_PEER_DATA_UP,
                                       self.beat(conn, x, y))

    def beat(self, conn, x, y):
        seat = self.room.seat_index_of(conn)
        return botsync.build_peer_packet(
            seat, botsync.OP_HEARTBEAT,
            botsync.heartbeat_body(0, seat, botsync.character_state(x, y)),
            game_id=self.room.epoch_value)

    def jump(self, conn, stage):
        seat = self.room.seat_index_of(conn)
        return botsync.build_peer_packet(
            seat, botsync.OP_JUMP, botsync.jump_body(seat, stage),
            game_id=self.room.epoch_value, sequence=self.next_seq(conn))

    def next_seq(self, conn):
        conn.test_seq = getattr(conn, "test_seq", 0) + 1
        return conn.test_seq - 1

    def tick_forward(self):
        """让**房里每个 bot** 的采样率限流不挡下一帧（`BOT_FRAME_INTERVAL_S`）。"""
        for index in self.room.bot_seats():
            seat = self.room.seats[index]
            if seat is not None and seat.conn is not None:
                seat.conn.last_frame_at = None

    def walk(self, conn, points):
        for point in points:
            self.human_heartbeat(conn, *point)


class BotHeartbeatTests(BotFrameRoom):
    """bot 到底有没有在别人屏幕上动。"""

    def test_a_bot_says_nothing_until_a_human_reports_a_position(self):
        """★ 没有真人的位置 = 我们不知道地图上哪里能站，一发都不许发。

        胡乱摆一个点的话 bot 会杵在地形里 / 图外，比不动更难看。
        """
        self.assertEqual([], bot_frames(self.alice, self.bot_seat))

    def test_the_bot_starts_moving_once_a_human_has_walked(self):
        self.walk(self.alice, [(0, 100), (200, 100), (400, 100)])
        frames = bot_frames(self.bob, self.bot_seat)
        self.assertTrue(frames)
        self.assertTrue(all(udpsync.is_heartbeat(f) for f in frames))

    def test_the_bot_stands_where_the_human_stood(self):
        """跟在 `BOT_FOLLOW_DISTANCE` 之后的那个采样点上。"""
        self.walk(self.alice, [(0, 50), (120, 50), (240, 50)])
        last = bot_frames(self.alice, self.bot_seat)[-1]
        self.assertEqual((120, 50), udpsync.heartbeat_position(last))

    def test_the_bot_packets_carry_the_bots_own_seat(self):
        self.walk(self.alice, [(0, 0), (300, 0)])
        for frame in bot_frames(self.bob, self.bot_seat):
            self.assertEqual(self.bot_seat, header(frame)["sender"])
            self.assertEqual(self.bot_seat, body_of(frame)[6])

    def test_the_bot_packets_pass_the_clients_checksum(self):
        """算错的话真客户端在收包入口就丢，症状 = 「bot 一动不动」。"""
        self.walk(self.alice, [(0, 0), (300, 0)])
        for frame in bot_frames(self.bob, self.bot_seat):
            self.assertEqual(header(frame)["checksum"],
                             botsync.udp_checksum(body_of(frame)))

    def test_the_frame_rate_is_limited_between_two_human_packets(self):
        """两个真人各 8 Hz 也不该让 bot 变成 16 Hz。"""
        self.walk(self.alice, [(0, 0), (400, 0)])
        before = len(bot_frames(self.alice, self.bot_seat))
        # ★ 故意**不**复位 `last_frame_at`：紧接着这一发应当被采样率挡住。
        gameserver.Conn.on_game_packet(self.bob, OP_PEER_DATA_UP,
                                       self.beat(self.bob, 10, 10))
        self.assertEqual(before, len(bot_frames(self.alice, self.bot_seat)))

    def test_a_dead_bot_stops_moving(self):
        """躺在地上等重生的那几秒里不发心跳 —— 真人死了也不发。"""
        self.walk(self.alice, [(0, 0), (400, 0)])
        self.room.quest.arm_respawn_watchdog(self.bot_seat, (0, 0), after=5.0)
        before = len(bot_frames(self.alice, self.bot_seat))
        self.walk(self.alice, [(500, 0), (600, 0)])
        self.assertEqual(before, len(bot_frames(self.alice, self.bot_seat)))



class TwoBotFrameRoom(BotFrameRoom):
    """两个 bot ——`/bot` 在游戏中是拒绝的，所以必须开局**前**加好。"""

    def start_battle(self):
        bot.handle_command(self.alice, "/bot")
        super().start_battle()
        self.bot_seats = self.room.bot_seats()


class TwoBotTests(TwoBotFrameRoom):

    def test_two_bots_queue_up_behind_the_human(self):
        """两个 bot 不能叠在同一个点上变成一个人（按座位次序排队）。"""
        self.assertEqual(2, len(self.bot_seats))
        self.walk(self.alice, [(0, 0), (120, 0), (240, 0), (360, 0)])
        spots = set()
        for index in self.bot_seats:
            frames = bot_frames(self.alice, index)
            self.assertTrue(frames, f"座位 {index} 一帧都没发")
            spots.add(udpsync.heartbeat_position(frames[-1]))
        self.assertEqual(2, len(spots))

    def test_every_bot_keeps_its_own_sequence_book(self):
        """两条流各记各的账 —— 混一起就是「打不死人」那种错位。"""
        for index in self.bot_seats:
            self.assertEqual(
                0, self.room.seats[index].conn.sync.events)


class BotJumpReplayTests(BotFrameRoom):
    """真人跳过的地方，bot 回放到那儿也跳一下。"""

    def test_the_bot_replays_the_jump_as_a_reliable_event(self):
        self.walk(self.alice, [(0, 0), (60, 0)])
        self.human_heartbeat(self.alice, 120, 80, jumped=1)
        self.walk(self.alice, [(180, 0), (240, 0)])
        jumps = [f for f in bot_frames(self.alice, self.bot_seat)
                 if udpsync.peer_opcode(f) == botsync.OP_JUMP]
        self.assertTrue(jumps, "bot 没有回放那一跳")
        self.assertEqual(self.bot_seat, body_of(jumps[0])[0])

    def test_a_standing_human_does_not_make_the_bot_jump_over_and_over(self):
        """★★ 去重按**状态翻转**，不按次数 / 时间窗（铁律 10）。

        真人跳完就站着不动 -> 轨迹不推进 -> 落脚点一直是那个带起跳标记的点。
        不去重的话 bot 每一帧都补一发 `rpJump`，而那是**事件包** ——
        每发吃掉一个可靠序号，动画上还一直抽。
        """
        self.walk(self.alice, [(0, 0), (60, 0)])
        self.human_heartbeat(self.alice, 120, 80, jumped=1)
        self.walk(self.alice, [(180, 0), (240, 0)])
        # 从这里开始真人一步不动，再喂十发心跳。
        for _ in range(10):
            self.human_heartbeat(self.alice, 240, 0)
        jumps = [f for f in bot_frames(self.alice, self.bot_seat)
                 if udpsync.peer_opcode(f) == botsync.OP_JUMP]
        self.assertEqual(1, len(jumps))

    def test_the_replayed_jump_keeps_the_sequence_bookkeeping_straight(self):
        """★ 事件包和心跳是同一本账：跳完之后心跳的 N 必须跟着 +1。"""
        self.walk(self.alice, [(0, 0), (60, 0)])
        self.human_heartbeat(self.alice, 120, 80, jumped=1)
        self.walk(self.alice, [(180, 0), (240, 0)])
        frames = bot_frames(self.alice, self.bot_seat)
        events = [f for f in frames if udpsync.peer_opcode(f) != 0x4001]
        beats = [f for f in frames if udpsync.is_heartbeat(f)]
        self.assertEqual(list(range(len(events))),
                         [udpsync.peer_sequence(f) for f in events])
        self.assertEqual(len(events),
                         udpsync.heartbeat_next_event_seq(beats[-1]))


class BotEpochTests(BotFrameRoom):
    """★★ bot 的换代模型必须和真人**同一串字节**推出来（§26）。

    对不上的话 `deliver()` 判成跨代，bot 的包一发都投不出去 ——
    症状是「bot 一动不动」，和「包压根没合成出来」长得一模一样。
    """

    def test_the_bot_advances_its_epoch_with_everyone_else(self):
        self.assertEqual(relayserver.epoch_state(self.alice).value,
                         relayserver.epoch_state(self.bot_conn).value)
        self.assertEqual(relayserver.epoch_state(self.alice).gen,
                         relayserver.epoch_state(self.bot_conn).gen)

    def test_the_bots_packets_are_not_dropped_as_cross_generation(self):
        before = gameserver.PEER_RELAY.cross_gen_dropped
        self.walk(self.alice, [(0, 0), (300, 0)])
        self.assertTrue(bot_frames(self.alice, self.bot_seat))
        self.assertEqual(before, gameserver.PEER_RELAY.cross_gen_dropped)

    def test_a_second_round_still_works(self):
        """★ 第二局是这条最容易崩的地方：`0x0403` 和 `0x0400` 各推一格。"""
        self.walk(self.alice, [(0, 0), (300, 0)])
        for conn in (self.alice, self.bob):
            gameserver.Conn.leave_game_result(conn)
        for _ in range(2):
            gameserver.Conn.on_game_packet(
                self.alice, gameserver.OP_COUNT_GAME_READY, b"")
        for conn in (self.alice, self.bob):
            gameserver.Conn.on_game_packet(conn, gameserver.OP_LOADING_DONE,
                                           b"")
        self.assertEqual(relayserver.epoch_state(self.alice).gen,
                         relayserver.epoch_state(self.bot_conn).gen)
        self.clear()
        self.tick_forward()
        self.walk(self.alice, [(0, 0), (300, 0)])
        self.assertTrue(bot_frames(self.alice, self.bot_seat),
                        "第二局 bot 一帧都没发出去")


class BotFrameSafetyTests(BotFrameRoom):
    """★ bot 的帧跑在**真人的转发路径**上 —— 它出问题不能连累真人（D1）。"""

    def test_a_bot_packet_does_not_drive_another_round_of_bot_frames(self):
        """★ 不挡住就是无限递归：tick -> deliver -> tick -> …"""
        self.walk(self.alice, [(0, 0), (300, 0)])
        self.assertTrue(bot_frames(self.alice, self.bot_seat))
        # 真的递归的话上面那一发早就 RecursionError 了；再直接调一次确认判据。
        before = len(bot_frames(self.alice, self.bot_seat))
        gameserver._relay_battle_tick(self.bot_conn)
        self.assertEqual(before, len(bot_frames(self.alice, self.bot_seat)))

    def test_a_broken_bot_frame_never_reaches_the_humans_thread(self):
        self.bot_conn.sync = None            # 下一帧必然 AttributeError
        self.walk(self.alice, [(0, 0), (300, 0)])
        self.assertIn(OP_PEER_DATA_DOWN, opcodes(self.bob))

    def test_bots_stop_when_the_room_is_no_longer_playing(self):
        gameserver.Conn.on_game_packet(self.bob, OP_LEAVE_SESSION, b"")
        for conn in (self.alice,):
            gameserver.Conn.leave_game_result(conn)
        self.clear()
        self.tick_forward()
        gameserver.Conn.on_game_packet(self.alice, OP_PEER_DATA_UP,
                                       self.beat(self.alice, 10, 10))
        self.assertEqual([], bot_frames(self.alice, self.bot_seat))


class BrokenBotIsolationTests(TwoBotFrameRoom):

    def test_a_violated_invariant_stops_only_that_bot(self):
        other_seat = [s for s in self.bot_seats if s != self.bot_seat][0]
        self.bot_conn.sync.broken = True
        self.walk(self.alice, [(0, 0), (200, 0), (400, 0)])
        self.assertEqual([], bot_frames(self.alice, self.bot_seat))
        self.assertTrue(bot_frames(self.alice, other_seat))


class SyncTrailTests(BotFrameRoom):
    """服务端唯一知道「谁站在哪」的地方（心跳 body `+7..10`）。"""

    def test_a_heartbeat_records_a_point(self):
        self.human_heartbeat(self.alice, 111, 222)
        self.assertEqual((111, 222, 0), self.alice.sync_trail[-1])

    def test_a_jump_is_attached_to_the_next_point(self):
        self.human_heartbeat(self.alice, 111, 222, jumped=2)
        self.assertEqual((111, 222, 2), self.alice.sync_trail[-1])
        self.human_heartbeat(self.alice, 120, 200)
        self.assertEqual((120, 200, 0), self.alice.sync_trail[-1])

    def test_a_non_heartbeat_does_not_record_anything(self):
        gameserver.Conn.on_game_packet(
            self.alice, OP_PEER_DATA_UP,
            botsync.build_peer_packet(0, botsync.OP_CHANGE_WEAPON,
                                      botsync.change_weapon_body(0, 1000000),
                                      game_id=self.room.epoch_value,
                                      sequence=self.next_seq(self.alice)))
        self.assertEqual(0, len(self.alice.sync_trail))

    def test_the_trail_is_bounded(self):
        self.assertEqual(gameserver.SYNC_TRAIL_POINTS,
                         self.alice.sync_trail.maxlen)

    def test_a_map_change_throws_the_trail_away(self):
        """★★ 上一张图的坐标放到新图上就是一个随机点。

        不清的话 bot 换图后会先在墙里 / 图外闪一下，直到轨迹被新图的采样点
        顶完为止 —— 闯关模式里每换一张图都看得见。
        """
        self.walk(self.alice, [(100, 100), (300, 100)])
        self.assertTrue(self.alice.sync_trail)
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REQ_CHANGE_TO_NEXT_MAP,
            gameserver.w_wstr("Stage02"))
        self.assertEqual(0, len(self.alice.sync_trail))
        self.assertIsNone(self.bot_conn.battle_pos)

    def test_a_bot_says_nothing_on_a_fresh_map_until_a_human_reports(self):
        self.walk(self.alice, [(100, 100), (300, 100)])
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REQ_CHANGE_TO_NEXT_MAP,
            gameserver.w_wstr("Stage02"))
        self.clear()
        self.tick_forward()
        gameserver._relay_battle_tick(self.alice)
        self.assertEqual([], bot_frames(self.alice, self.bot_seat))

    def test_a_new_round_throws_the_trail_away_too(self):
        self.walk(self.alice, [(100, 100), (300, 100)])
        for conn in (self.alice, self.bob):
            gameserver.Conn.leave_game_result(conn)
        for _ in range(2):
            gameserver.Conn.on_game_packet(
                self.alice, gameserver.OP_COUNT_GAME_READY, b"")
        for conn in (self.alice, self.bob):
            gameserver.Conn.on_game_packet(conn, gameserver.OP_LOADING_DONE,
                                           b"")
        self.assertEqual(0, len(self.alice.sync_trail))


if __name__ == "__main__":
    unittest.main(verbosity=2)
