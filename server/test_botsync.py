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

import math
import os
import struct
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import bot                                                     # noqa: E402
import botsync                                                 # noqa: E402
import gameserver                                              # noqa: E402
import relayserver                                             # noqa: E402
import udpsync                                                 # noqa: E402
import weapondata                                              # noqa: E402
from gameserver import OP_LEAVE_SESSION, OP_PEER_DATA_DOWN, \
    OP_PEER_DATA_UP                                            # noqa: E402
from test_battle import opcodes                                # noqa: E402
from lobby import TEAM_A, TEAM_B                                # noqa: E402
from test_bot import BotBattleRoom, chat_lines                 # noqa: E402

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

    def test_bit_two_says_i_am_standing_on_the_ground(self):
        """★★ bit2 = `[char+0x128]` = 「我踩在地面上」（§35，勘误 §31）。

        它**不是**「我静止着」：语料 67186 发里有 **20341 发**「位置在变、
        bit2=1、速度两格是 0」—— 那正是**在地上走**。反过来「位置在变、
        bit2=1、速度非 0」只有 9 发。
        """
        for on_ground in (True, False):
            state = botsync.character_state(100, 200, on_ground=on_ground)
            self.assertEqual(
                on_ground,
                bool(struct.unpack_from("<i", state, 12)[0]
                     & botsync.HEARTBEAT_BIT_ONGROUND))

    def test_walking_on_the_ground_reports_zero_speed(self):
        """★★★ 踩在地上时速度两格**必须**是 0，哪怕角色正在走（§35）。

        这是「走一步停一下、像在抽搐」那个症状的根因：地上走却报一个非 0
        速度，收方会拿它自己往前推算，和下一发心跳里的坐标当场打架。
        真人的包里从来没有这种组合（67186 发里只有 9 发）。
        """
        state = botsync.character_state(100, 200, vx=9, vy=-7, on_ground=True)
        self.assertEqual((0, 0), struct.unpack_from("<hh", state, 4))
        self.assertTrue(struct.unpack_from("<i", state, 12)[0]
                        & botsync.HEARTBEAT_BIT_ONGROUND)

    def test_in_the_air_the_speed_goes_out_as_given(self):
        """腾空时速度原样上线 —— 那是真人这一段真跳出来的抛体速度。"""
        state = botsync.character_state(100, 200, vx=9, vy=-7, on_ground=False)
        self.assertEqual((9, -7), struct.unpack_from("<hh", state, 4))
        self.assertFalse(struct.unpack_from("<i", state, 12)[0]
                         & botsync.HEARTBEAT_BIT_ONGROUND)

    def test_the_key_mask_lands_on_bytes_twenty_three_and_twenty_four(self):
        """★★★ 六位掩码 = **方向键**，收方摊回 `[char+0x2b8+i*4]`（§39）。

        结构 `+0x10`（= body `+23..24`）。bit0 = 左、bit1 = 上、bit2 = 右、
        bit3 = 下 —— 序列化器 `0x5041b5` 读的是每一格的 **bit0**，
        反序列化器 `0x504316` 置位写 `0x41`、未置写 `0x10`。
        """
        for keys in (0, botsync.KEY_LEFT, botsync.KEY_RIGHT,
                     botsync.KEY_LEFT | botsync.KEY_UP):
            state = botsync.character_state(0, 0, keys=keys)
            self.assertEqual(keys, struct.unpack_from("<H", state, 16)[0])

    def test_the_fast_run_flag_lands_in_bit_three_of_the_bitfield(self):
        """★★ 位域 bit3 = `[char+0x4bc]` = **冲刺**（§40）。

        收方拿它把这个角色整帧的 `dt` 乘 `FastRunRate`（这一版 = 1.5）——
        位移和腿的动画速率一起变快。语料实测：置起时在地上每帧 `|dx|`
        中位数 33、没置起 22。
        """
        for fast_run in (False, True):
            state = botsync.character_state(0, 0, fast_run=fast_run)
            self.assertEqual(
                fast_run,
                bool(struct.unpack_from("<i", state, 12)[0]
                     & botsync.HEARTBEAT_BIT_FASTRUN))

    def test_walk_keys_maps_the_direction_to_the_arrow_key(self):
        """★★ 走路方向 -> 按键：右 -> bit2、左 -> bit0、不动 -> 一个都不按。

        收方 `0x5073c2` 就是这么把按键翻成走路方向 `[char+0x4b4]` 的，
        而 `0x507fb5` 拿走路方向选动画：`0` -> `Stand%02d`（站着）、
        非 0 -> `Run-F/B%02d`（走）。**掩码填 0 = 收方画一个站着的人**。
        """
        self.assertEqual(botsync.KEY_RIGHT,
                         botsync.walk_keys(botsync.FACING_RIGHT))
        self.assertEqual(botsync.KEY_LEFT,
                         botsync.walk_keys(botsync.FACING_LEFT))
        self.assertEqual(0, botsync.walk_keys(0))

    def test_a_real_client_packet_holds_no_key_while_standing(self):
        """基准：真客户端站着不动那一发，掩码是 0（语料里 26744 : 2122）。"""
        self.assertEqual(0, struct.unpack_from(
            "<H", body_of(REAL_HEARTBEAT), 7 + 16)[0])

    def test_the_aim_point_follows_the_facing(self):
        """★ 准星是**世界坐标**（§36），不给就摆在自己正前方。

        填死一个常数 = bot 永远瞄着地图左上角，身体也就永远朝那边 ——
        用户 2026-08-26 实机报的「全是头看向屏幕这个方向」。
        """
        right = botsync.character_state(1000, 500, facing=botsync.FACING_RIGHT)
        left = botsync.character_state(1000, 500, facing=botsync.FACING_LEFT)
        self.assertGreater(struct.unpack_from("<h", right, 18)[0], 1000)
        self.assertLess(struct.unpack_from("<h", left, 18)[0], 1000)

    def test_facing_angle_and_cursor_stay_consistent(self):
        """★ 三个字段说的是同一件事，必须一起算（§37）。

        朝向位跟的是「准星在我哪一侧」（语料 97.8%），角度是**相对面朝
        方向**的仰角（朝左时 x 分量取反）—— 所以朝左瞄斜上方时角度仍是
        一个小的负数，不是 ±180 附近。
        """
        state = botsync.character_state(1000, 500, cursor=(500, 400))
        field = struct.unpack_from("<i", state, 12)[0]
        self.assertEqual(botsync.FACING_LEFT, ((field & 3) ^ 2) - 2)
        angle = struct.unpack_from("<h", state, 10)[0]
        self.assertEqual(round(math.degrees(math.atan2(-100, 500))), angle)

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

    def test_crouch_is_two_bytes_seat_then_down_flag(self):
        """★ `rpCrouch`（`0x000b`）：语料 394 发，`+0` 和发送方座位 100% 一致、
        `+1` 只有 0（181 发）/ 1（213 发）两种（§41）。"""
        self.assertEqual(b"\x03\x01", botsync.crouch_body(3, True))
        self.assertEqual(b"\x03\x00", botsync.crouch_body(3, False))

    def test_jump_is_two_bytes_seat_then_stage(self):
        self.assertEqual(b"\x05\x02", botsync.jump_body(5, 2))

    def test_change_weapon_is_five_bytes_seat_then_weapon_id(self):
        body = botsync.change_weapon_body(2, 1002030)
        self.assertEqual(5, len(body))
        self.assertEqual((2, 1002030), struct.unpack_from("<Bi", body, 0))

    def test_explode_damage_goes_into_the_last_float(self):
        """★ `+24` **就是伤害**（§42）：收方朝零截断成 int 之后原样交给
        `Character::OnHit`，不重算。"""
        body = botsync.explode_body(100002, 200001, 1.0, 2.0,
                                    hit_kind=botsync.HIT_CHARACTER, damage=18)
        self.assertAlmostEqual(18.0, struct.unpack_from("<f", body, 24)[0])
        self.assertEqual(botsync.HIT_CHARACTER,
                         struct.unpack_from("<i", body, 16)[0])


# ---------------------------------------------------------------------------
# ★★★ 对象句柄（§42 / §43）—— M3b 的地基
# ---------------------------------------------------------------------------
class HandleTests(unittest.TestCase):
    """句柄错了的后果是 `rpExplode` 被**静默丢弃**（`0x492750`），
    表现是「子弹飞过去不炸、一滴血不掉」，一局之内不自愈。所以逐条钉住。"""

    def test_character_handle_matches_the_client_formula(self):
        """`0x405f02: imul ecx,seat,0x186a0; add ecx,0x186a1`。
        语料里实测到的 `100001 / 200001 / 300001 / 400001` 就是座位 0~3。"""
        self.assertEqual(100001, botsync.character_handle(0))
        self.assertEqual(200001, botsync.character_handle(1))
        self.assertEqual(300001, botsync.character_handle(2))
        self.assertEqual(400001, botsync.character_handle(3))

    def test_projectile_handles_start_at_base_plus_two(self):
        """★ `ProjectileMgr::Reset` `0x473520` 把每格初值设成 `…002`。
        语料 14 个文件里**自家弹体句柄的最小值恒等于它**，0 例外（§43）。"""
        self.assertEqual(100002, botsync.projectile_handle(0, 0))
        self.assertEqual(100003, botsync.projectile_handle(0, 1))
        self.assertEqual(500002, botsync.projectile_handle(4, 0))

    def test_projectile_handles_never_collide_with_the_character(self):
        """角色占 `+1`、弹体从 `+2` 起 —— 撞上的话
        `rpExplode` 会把「弹体」查成那个角色。"""
        for seat in range(6):
            self.assertNotEqual(botsync.character_handle(seat),
                                botsync.projectile_handle(seat, 0))

    def test_handle_owner_is_the_client_function(self):
        """`0x473e65`：`h < 100000` 一律 20（怪 / 中立），否则
        `(h−100000)/100000 + 10`。"""
        self.assertEqual(10, botsync.handle_owner(100001))
        self.assertEqual(10, botsync.handle_owner(100002))
        self.assertEqual(11, botsync.handle_owner(200001))
        self.assertEqual(14, botsync.handle_owner(500002))
        self.assertEqual(botsync.OWNER_NEUTRAL, botsync.handle_owner(99999))
        self.assertEqual(botsync.OWNER_NEUTRAL, botsync.handle_owner(0))

    def test_owner_round_trips_for_every_seat(self):
        """★ 这条是整套预测的地基：**每个 owner 一格计数器** ——
        所以别人打多少枪都不动 bot 那一格。"""
        for seat in range(6):
            want = botsync.OWNER_SEAT_BASE + seat
            self.assertEqual(want, botsync.handle_owner(
                botsync.character_handle(seat)))
            for shot in (0, 1, 500, 99000):
                self.assertEqual(want, botsync.handle_owner(
                    botsync.projectile_handle(seat, shot)))


class FireBookkeepingTests(unittest.TestCase):
    """`BotSyncStream.fire()`：组包和句柄记账必须**在一次加锁里**做完。"""

    def setUp(self):
        self.stream = botsync.BotSyncStream(FakeBot(seat=2))

    def _fire(self, step=1):
        return self.stream.fire(1002010, 10.0, 20.0, 0.1, 1.0,
                                handle_step=step)

    def test_handles_advance_one_per_shot(self):
        first = self._fire()[1]
        second = self._fire()[1]
        self.assertEqual(botsync.projectile_handle(2, 0), first)
        self.assertEqual(first + 1, second)

    def test_splash_weapons_advance_two_per_shot(self):
        """带 `SplashRange` 的武器爆炸时多创建一个溅射对象，多吃一个句柄
        （语料实测：`1002030` / `1001030` 步进恒 2，§43）。"""
        first = self._fire(step=2)[1]
        self.assertEqual(first + 2, self._fire(step=2)[1])

    def test_unknown_step_is_refused_on_the_spot(self):
        """步进未知的武器**根本不该发** —— 猜错的后果是静默丢弃。"""
        with self.assertRaises(botsync.SyncInvariantError):
            self._fire(step=None)

    def test_step_smaller_than_the_fragment_count_is_refused(self):
        """★ 一发造 3 颗弹体却只记 2 格 = 下一发的句柄一定错位。"""
        with self.assertRaises(botsync.SyncInvariantError):
            self.stream.fire(1001010, 10.0, 20.0, 0.1, 1.0,
                             handle_step=2, shots=3)

    def test_fragment_handles_are_consecutive(self):
        """★★ 散射武器一发造 `shots` 颗，句柄**连号**（§46）——
        调用方按 `base + i` 逐颗发 `rpExplode`。"""
        first = self.stream.fire(1001010, 10.0, 20.0, 0.1, 1.0,
                                 handle_step=3, shots=3)[1]
        second = self.stream.fire(1001010, 10.0, 20.0, 0.1, 1.0,
                                  handle_step=3, shots=3)[1]
        self.assertEqual(first + 3, second)

    def test_the_shot_count_lands_in_the_packet(self):
        """`rpFire +22` 必须是 `SpreadFrags`，不是恒 1（填小了收侧一颗都不造）。"""
        packet = self.stream.fire(1001010, 10.0, 20.0, 0.1, 1.0,
                                  handle_step=3, shots=3)[0]
        body = packet[udpsync.PEER_HEADER_SIZE:]
        self.assertEqual(3, struct.unpack_from("<i", body, 22)[0])

    def test_too_many_shots_is_refused(self):
        """收侧 `0x491f41` 把 `count >= 30` 的整包丢掉 —— 不许发出去。"""
        with self.assertRaises(botsync.SyncInvariantError):
            self.stream.fire(1001010, 10.0, 20.0, 0.1, 1.0,
                             handle_step=60, shots=30)

    def test_fire_also_consumes_one_reliable_sequence(self):
        """`rpFire` 是事件包：句柄和序号是**两本**账，都得往前走。"""
        self.assertEqual(0, self.stream.events)
        self._fire()
        self.assertEqual(1, self.stream.events)

    def test_reset_projectiles_rewinds_to_the_first_handle(self):
        """★ 换图 / 新一局：客户端把计数器清回 `座位×100000+100002`
        （语料实测，§43 第 4 条）。两边必须一起清。"""
        self._fire()
        self._fire()
        self.stream.reset_projectiles()
        self.assertEqual(botsync.projectile_handle(2, 0), self._fire()[1])

    def test_reset_projectiles_does_not_touch_the_event_sequence(self):
        """两本账清零的**时机不一样**：事件序号跟换代走（`_sync_epoch`），
        发弹数跟换图走。混在一起清就会有一边错。"""
        self._fire()
        events = self.stream.events
        self.stream.reset_projectiles()
        self.assertEqual(events, self.stream.events)


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
        """`(x, y, jumped)` 或 `(x, y, jumped, on_ground, vx, vy)` 都收。"""
        return [gameserver.SyncTrailPoint(*point) for point in points]

    def where(self, point):
        """落脚点的 `(x, y, jumped)` —— 只关心走到哪儿的用例用它。"""
        return (point[0], point[1], point[2])

    def test_an_empty_trail_gives_nothing(self):
        self.assertIsNone(bot.trail_point([], 100))

    def test_it_walks_back_along_the_path_by_the_requested_distance(self):
        trail = self.trail((0, 0, 0), (50, 0, 0), (100, 0, 0), (150, 0, 0))
        self.assertEqual((100, 0, 0), self.where(bot.trail_point(trail, 50)))
        self.assertEqual((50, 0, 0), self.where(bot.trail_point(trail, 100)))

    def test_a_short_trail_falls_back_to_its_oldest_point(self):
        """真人才刚进图、还没走几步时就是这种情况。"""
        trail = self.trail((10, 20, 0), (12, 20, 0))
        self.assertEqual((10, 20, 0), self.where(bot.trail_point(trail, 9999)))

    def test_standing_still_keeps_the_bot_where_it_is(self):
        """真人站着不动 -> 轨迹上全是同一个点 -> bot 也停下。"""
        trail = self.trail(*[(7, 8, 0)] * 10)
        self.assertEqual((7, 8, 0), self.where(bot.trail_point(trail, 120)))

    def test_the_motion_facts_come_from_the_point_just_walked_past(self):
        """★★ 「踩地还是腾空、腾空时速度多少」也照抄那一段（§35）。

        和起跳标记同一个口径（插值区间**靠老**的那一端）：bot 是沿着
        老 -> 新重走这条路的，落脚点落在某一段里 = 它这一帧正好越过了
        那一段的老端点。自己从位移反推速度就是「一跳一跳」的根因。
        """
        trail = self.trail((0, 0, 0, True, 0, 0), (100, 0, 0, False, 9, -20),
                           (200, 0, 0, True, 0, 0))
        # 往回 50 -> 落在 (100,0) -> (200,0) 这一段里，靠老那一端是腾空的。
        x, _, _, on_ground, vx, vy, _, _ = bot.trail_point(trail, 50)
        self.assertAlmostEqual(150.0, x)
        self.assertFalse(on_ground)
        self.assertEqual((9, -20), (vx, vy))
        # 再往回一段 -> 越过的是第一个点，那儿是踩在地上的。
        self.assertEqual((True, 0, 0, False, False),
                         bot.trail_point(trail, 150)[3:])

    def test_the_fast_run_flag_comes_from_that_same_point(self):
        """★ 冲刺位（位域 bit3）和上面几格同一个口径（§40）。"""
        trail = self.trail((0, 0, 0, True, 0, 0, False),
                           (100, 0, 0, True, 0, 0, True),
                           (200, 0, 0, True, 0, 0, False))
        self.assertTrue(bot.trail_point(trail, 50)[6])
        self.assertFalse(bot.trail_point(trail, 150)[6])

    def test_an_old_three_tuple_point_counts_as_walking_on_the_ground(self):
        """没有运动信息的老式点：当成**踩在地上**（`_motion_of` 的兜底）。

        反过来（当成腾空）会让收方拿一个假速度往前推算，直接抽搐；
        说踩地最多是少一段抛物线姿势。
        """
        point = bot.trail_point(self.trail((0, 0, 0), (100, 0, 0)), 50)
        self.assertEqual((True, 0, 0, False, False), point[3:])

    def test_the_landing_point_is_interpolated_inside_the_segment(self):
        """★ 落脚点在两个采样点**之间**，不吸附（V0.3 §32）。

        吸附的那一版每帧前进 0 / 1 / 2 个采样点，看着一顿一顿；插值之后
        bot 每帧前进的距离恒等于真人这一帧前进的距离。
        """
        trail = self.trail((0, 0, 0), (100, 0, 0), (200, 0, 0))
        x, y = bot.trail_point(trail, 150)[:2]
        self.assertAlmostEqual(50.0, x)
        self.assertAlmostEqual(0.0, y)

    def test_the_jump_flag_comes_from_the_point_just_walked_past(self):
        """起跳标记取插值区间**靠老**的那一端 —— 那正是这一帧越过的点。"""
        trail = self.trail((0, 0, 0), (30, 40, 2), (60, 0, 0), (90, 0, 0))
        x, y, jumped = self.where(bot.trail_point(trail, 60))
        self.assertEqual(2, jumped)
        self.assertAlmostEqual(42.0, x)      # (30,40) -> (60,0) 上走了 40%
        self.assertAlmostEqual(24.0, y)

    def test_a_jump_further_ahead_is_not_reported_early(self):
        """★ 真人一起跳、还在 120 之外的 bot 不能立刻跟着跳。

        把走过的那一段 OR 起来就会这样 —— 早一秒多，看着像抽搐。
        """
        trail = self.trail((0, 0, 0), (30, 0, 0), (60, 40, 1), (90, 0, 0))
        self.assertEqual(0, bot.trail_point(trail, 60)[2])

    def test_diagonal_distance_counts_the_vertical_part_too(self):
        trail = self.trail((0, 0, 0), (0, 100, 0), (0, 200, 0))
        self.assertEqual((0, 100, 0), self.where(bot.trail_point(trail, 100)))


# ---------------------------------------------------------------------------
# 战斗帧（真房间）
# ---------------------------------------------------------------------------
def peer_frames_in(sent):
    """一串已经发出去的帧里，所有 `0x040f` 携带的那份 `UdpPacket`。"""
    out = []
    for plain in sent:
        if len(plain) >= 10 and plain[0] == gameserver.MAGIC_GAME:
            if struct.unpack_from("<H", plain, 8)[0] == OP_PEER_DATA_DOWN:
                out.append(plain[10:])
    return out


def peer_frames(conn):
    """这条连接收到的所有 `0x040f` 里那份 `UdpPacket`。"""
    return peer_frames_in(conn.sent)


def bot_frames_in(sent, seat):
    return [p for p in peer_frames_in(sent) if header(p)["sender"] == seat]


def bot_frames(conn, seat):
    return bot_frames_in(conn.sent, seat)


class BotFrameRoom(BotBattleRoom):
    """alice（房主，座位 0）+ bob（座位 1）+ 一个 bot（座位 2），已进关卡。

    ★ 每个用例自己喂真人的心跳 —— bot 的帧是**被真人的同步包驱动**的（D17），
    不喂就一帧都不该有。
    """

    def human_heartbeat(self, conn, x, y, jumped=0, on_ground=True,
                        velocity=(0, 0), fast_run=False):
        """让 `conn` 发一发带位置的心跳（走真的 `0x040e` 入口）。

        ★ 不用再复位什么闸门：bot 的帧判据是「这个真人报了一个新位置」
        （`sync_trail_seq` 变了），喂一发就走一帧（V0.3 §32）。

        ★ 默认「踩在地上、速度 0」—— 那是真人**走路**时的样子（§35），
        绝大多数用例要的就是它。跳跃的段落显式传 `on_ground=False`。
        """
        if jumped:
            gameserver.Conn.on_game_packet(conn, OP_PEER_DATA_UP, self.jump(
                conn, jumped))
        gameserver.Conn.on_game_packet(
            conn, OP_PEER_DATA_UP,
            self.beat(conn, x, y, on_ground=on_ground, velocity=velocity,
                      fast_run=fast_run))

    def beat(self, conn, x, y, on_ground=True, velocity=(0, 0),
             fast_run=False):
        seat = self.room.seat_index_of(conn)
        state = botsync.character_state(x, y, vx=velocity[0], vy=velocity[1],
                                        on_ground=on_ground,
                                        fast_run=fast_run)
        return botsync.build_peer_packet(
            seat, botsync.OP_HEARTBEAT,
            botsync.heartbeat_body(0, seat, state),
            game_id=self.room.epoch_value)

    def jump(self, conn, stage):
        seat = self.room.seat_index_of(conn)
        return botsync.build_peer_packet(
            seat, botsync.OP_JUMP, botsync.jump_body(seat, stage),
            game_id=self.room.epoch_value, sequence=self.next_seq(conn))

    def human_crouch(self, conn, down):
        """真人按下 / 松开下蹲 —— 发一发 `rpCrouch`（§41）。

        ★ 它是**事件包**，中间的心跳里一个位都没有，所以状态由服务端记着。
        """
        seat = self.room.seat_index_of(conn)
        gameserver.Conn.on_game_packet(conn, OP_PEER_DATA_UP,
                                       botsync.build_peer_packet(
                                           seat, botsync.OP_CROUCH,
                                           botsync.crouch_body(seat, down),
                                           game_id=self.room.epoch_value,
                                           sequence=self.next_seq(conn)))

    def next_seq(self, conn):
        conn.test_seq = getattr(conn, "test_seq", 0) + 1
        return conn.test_seq - 1

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

    def test_only_the_human_it_follows_drives_a_frame(self):
        """两个真人各 8 Hz 也不该让 bot 变成 16 Hz（V0.3 §32）。

        判据是「**我跟的那个人**报了新位置」——别人报的位置和 bot 这一步
        走到哪没有关系。老版本靠 0.125 秒的采样率限流去挡，那个数又恰好
        和真人的心跳同频，抖一抖就丢帧 = 用户看到的「一卡一卡」。
        """
        self.walk(self.alice, [(0, 0), (400, 0)])
        before = len(bot_frames(self.alice, self.bot_seat))
        # bob 在很远的地方报一发：bot 跟的是 alice，这一发不该让它动。
        gameserver.Conn.on_game_packet(self.bob, OP_PEER_DATA_UP,
                                       self.beat(self.bob, 10, 10))
        self.assertEqual(before, len(bot_frames(self.alice, self.bot_seat)))

    def test_every_position_report_of_the_leader_gives_exactly_one_frame(self):
        """★ 逐发对齐：真人报几个位置，bot 就走几帧，一发不多一发不少。"""
        before = len(bot_frames(self.bob, self.bot_seat))
        self.walk(self.alice, [(0, 0), (60, 0), (120, 0), (180, 0), (240, 0)])
        self.assertEqual(before + 5, len(bot_frames(self.bob, self.bot_seat)))

    def test_a_dead_bot_stops_moving(self):
        """躺在地上等重生的那几秒里不发心跳 —— 真人死了也不发。"""
        self.walk(self.alice, [(0, 0), (400, 0)])
        self.room.quest.arm_respawn_watchdog(self.bot_seat, (0, 0), after=5.0)
        before = len(bot_frames(self.alice, self.bot_seat))
        self.walk(self.alice, [(500, 0), (600, 0)])
        self.assertEqual(before, len(bot_frames(self.alice, self.bot_seat)))

    def test_a_bot_out_of_lives_stays_on_the_ground(self):
        """★ 命用完的 bot 不许继续跑（V0.3 §34）。

        看门狗判完「三条命用完」之后**会把闩撤掉**，只看 `respawn_due` 的话
        bot 从那一刻起就以幽灵的姿态继续跟着人走。
        """
        self.walk(self.alice, [(0, 0), (400, 0)])
        self.room.quest.lives_spent.add(self.bot_seat)
        before = len(bot_frames(self.alice, self.bot_seat))
        self.walk(self.alice, [(500, 0), (600, 0)])
        self.assertEqual(before, len(bot_frames(self.alice, self.bot_seat)))


class BotWalkAnimationTests(BotFrameRoom):
    """★ 「没有走路动画」的根因（V0.3 §35 —— 勘误 §31 —— 和 §32）。"""

    def state_of(self, frame):
        """一发心跳里那 24 字节角色状态结构拆成 `(vx, vy, 位域)`。"""
        body = body_of(frame)
        vx, vy = struct.unpack_from("<hh", body, 7 + 4)
        return vx, vy, struct.unpack_from("<i", body, 7 + 12)[0]

    def keys_of(self, frame):
        """一发心跳里的**方向键掩码**（结构 `+0x10` = body `+23..24`）。"""
        return struct.unpack_from("<H", body_of(frame), 7 + 16)[0]

    def test_a_bot_walking_on_the_ground_reports_zero_speed(self):
        """★★★ 在地上走的那几发：**速度 0、bit2 置起**（§35）。

        这是真客户端的口径（20341 发「位置在变、bit2=1、速度 0」）。
        会话 07 那版按位移反推速度、并把 bit2 清零，等于每一发都在说
        「我在空中，速度 (9,0)」—— 收方拿那个速度自己往前推算，和坐标
        当场打架，就是用户报的「走一下停一下、像在抽搐」，动画也不播。
        """
        self.walk(self.alice, [(0, 0), (200, 0), (400, 0), (600, 0)])
        vx, vy, field = self.state_of(bot_frames(self.alice, self.bot_seat)[-1])
        self.assertEqual((0, 0), (vx, vy))
        self.assertTrue(field & botsync.HEARTBEAT_BIT_ONGROUND)

    def test_a_bot_in_the_air_replays_the_humans_speed(self):
        """★ 真人腾空的那一段，bot 原样抄他的 bit2 和速度。"""
        self.walk(self.alice, [(0, 0), (200, 0)])
        self.human_heartbeat(self.alice, 400, -50, on_ground=False,
                             velocity=(9, -20))
        self.human_heartbeat(self.alice, 600, -50, on_ground=False,
                             velocity=(9, -20))
        vx, vy, field = self.state_of(bot_frames(self.alice, self.bot_seat)[-1])
        self.assertEqual((9, -20), (vx, vy))
        self.assertFalse(field & botsync.HEARTBEAT_BIT_ONGROUND)

    def test_a_standing_bot_stays_on_the_ground(self):
        """真人停下 -> bot 停下 -> 仍然是「踩在地上、速度 0」。"""
        self.walk(self.alice, [(0, 0), (200, 0), (400, 0)])
        self.walk(self.alice, [(400, 0), (400, 0), (400, 0), (400, 0)])
        vx, vy, field = self.state_of(bot_frames(self.alice, self.bot_seat)[-1])
        self.assertEqual((0, 0), (vx, vy))
        self.assertTrue(field & botsync.HEARTBEAT_BIT_ONGROUND)

    def test_the_bot_aims_the_way_it_walks(self):
        """★ 身体朝向跟着准星走（§36 / §37）—— 准星得跟着 bot 挪。

        填死一个常数时，bot 在图上跑了几千个单位、准星还钉在 (512,384)：
        它会「一边往右走、一边扭头看着地图左上角」。
        """
        self.walk(self.alice, [(2000, 500), (2200, 500), (2400, 500),
                               (2600, 500)])
        body = body_of(bot_frames(self.alice, self.bot_seat)[-1])
        x = struct.unpack_from("<h", body, 7 + 0)[0]
        cursor_x = struct.unpack_from("<h", body, 7 + 18)[0]
        field = struct.unpack_from("<i", body, 7 + 12)[0]
        self.assertGreater(cursor_x, x, "往右走 -> 准星应该在自己右边")
        self.assertEqual(botsync.FACING_RIGHT, ((field & 3) ^ 2) - 2)

    def test_a_bot_walking_right_holds_the_right_key(self):
        """★★★ 走路动画的开关（§39）：往右走的那几发要按着**右键**。

        掩码填 0 = 收方 `0x5073c2` 算出走路方向 0 = `0x507fb5` 选
        `Stand%02d`，而且 `0x507660` 也不替它走 —— 位置只被心跳一格一格
        拉过去。那就是用户 2026-08-26 第三轮实机报的
        「还是没有走路动画，看起来是一格一格的平移」。
        """
        self.walk(self.alice, [(0, 0), (200, 0), (400, 0), (600, 0)])
        self.assertEqual(botsync.KEY_RIGHT,
                         self.keys_of(bot_frames(self.alice, self.bot_seat)[-1]))

    def test_a_bot_walking_left_holds_the_left_key(self):
        self.walk(self.alice, [(600, 0), (400, 0), (200, 0), (0, 0)])
        self.assertEqual(botsync.KEY_LEFT,
                         self.keys_of(bot_frames(self.alice, self.bot_seat)[-1]))

    def test_a_standing_bot_holds_no_key(self):
        """站住了就得**松开**按键，否则收方会一直把它往前推。"""
        self.walk(self.alice, [(0, 0), (200, 0), (400, 0)])
        self.walk(self.alice, [(400, 0)] * 4)
        self.assertEqual(0,
                         self.keys_of(bot_frames(self.alice, self.bot_seat)[-1]))

    def test_a_bot_in_the_air_holds_no_key(self):
        """★ 腾空那一段不按键（§39）：动画是 `Jump`（不看掩码），而收方
        `0x507402` 会拿按键**覆写**空中速度，把抄来的抛体速度冲掉。"""
        self.walk(self.alice, [(0, 0), (200, 0)])
        self.human_heartbeat(self.alice, 400, -50, on_ground=False,
                             velocity=(9, -20))
        self.human_heartbeat(self.alice, 600, -50, on_ground=False,
                             velocity=(9, -20))
        self.assertEqual(0,
                         self.keys_of(bot_frames(self.alice, self.bot_seat)[-1]))

    def test_a_bot_that_has_not_moved_yet_holds_no_key(self):
        """★★ 真人在走、bot 还杵在原地的那几帧，**不许**说自己在走。

        轨迹总长还不到 `BOT_FOLLOW_DISTANCE` 时落脚点固定在最老那个采样点
        —— bot 线上的坐标一动没动。这时候照抄真人的按键就会说谎：收方按
        走路速度把它往前推、下一发心跳又把它拉回来，正是 §35 那种抽搐。
        所以按键跟的是 **bot 自己这一帧的位移**，不是真人的。
        """
        self.walk(self.alice, [(0, 0), (10, 0), (20, 0), (30, 0)])
        frames = bot_frames(self.alice, self.bot_seat)
        self.assertTrue(frames)
        self.assertEqual([0] * len(frames), [self.keys_of(f) for f in frames])

    def test_a_bot_following_a_dashing_human_dashes_too(self):
        """★★ 真人按右键冲刺 -> bot 也报冲刺位（§40）。

        不报的话：bot 抄来的坐标是 1.5 倍步长的，收方却只按普通走速替它挪，
        每发心跳再把它拽回来 —— 跟不上 + 拉扯，腿的动画速率也不对。
        """
        self.walk(self.alice, [(0, 0), (200, 0)])
        for x in (400, 600, 800):
            self.human_heartbeat(self.alice, x, 0, fast_run=True)
        _, _, field = self.state_of(bot_frames(self.alice, self.bot_seat)[-1])
        self.assertTrue(field & botsync.HEARTBEAT_BIT_FASTRUN)

    def test_a_standing_bot_never_claims_to_dash(self):
        """★ 站着不能冲刺 —— 原版进冲刺就要求走路方向非 0（语料 1003 : 3）。"""
        for _ in range(4):
            self.human_heartbeat(self.alice, 400, 0, fast_run=True)
        frames = bot_frames(self.alice, self.bot_seat)
        self.assertTrue(frames)
        for frame in frames:
            _, _, field = self.state_of(frame)
            self.assertFalse(field & botsync.HEARTBEAT_BIT_FASTRUN)

    def test_the_bot_step_equals_the_humans_step_that_frame(self):
        """★ bot 这一帧挪多远 = 真人这一帧挪多远（V0.3 §32）。

        跟随距离是常数，所以头往前走多少、120 之后那个点就往前走多少 ——
        **前提是落脚点在两个采样点之间插值**。吸附到采样点的老版本在真人
        速度不匀时会出现 0 / 90 这种跳变，那正是「平移的时候一卡一卡」。
        用例故意让真人 30 / 60 交替地走，匀速走是分不出两版的。
        """
        walk = [0, 30, 90, 120, 180, 210, 270, 300, 360, 420, 480, 540]
        self.walk(self.alice, [(x, 0) for x in walk])
        xs = [udpsync.heartbeat_position(f)[0]
              for f in bot_frames(self.alice, self.bot_seat)]
        self.assertEqual(len(walk), len(xs))
        for index in range(len(walk) - 4, len(walk)):
            human_step = walk[index] - walk[index - 1]
            bot_step = xs[index] - xs[index - 1]
            self.assertLessEqual(
                abs(bot_step - human_step), 1,
                f"第 {index} 帧：真人走了 {human_step}，bot 走了 {bot_step}"
                f"（bot 的轨迹 {xs}）")


def load_frames_in(sent, seat):
    """一串帧里，由 `seat` 发出的 `0x4005` 携带的那个进度值。"""
    return [struct.unpack_from("<i", body_of(p), 0)[0]
            for p in bot_frames_in(sent, seat)
            if udpsync.peer_opcode(p) == botsync.OP_LOAD_PROGRESS]


def load_frames(conn, seat):
    """这条连接收到的、由 `seat` 发出的 `0x4005` 里那个进度值。"""
    return load_frames_in(conn.sent, seat)


class BotLoadProgressTests(BotFrameRoom):
    """★ 「点开始 bot 没有进度条」（V0.3 §30 / §38，做法见 D26）。

    加载界面上那几根条画的是 `0x4005`（一个 int32，0..100）。
    **bot 一进加载界面就报满** —— 它没有客户端、没有一个字节的资源要读，
    永远是房里加载最快的那个（D4 已经定了「广播出去那一刻就算它加载完」）。
    """

    def test_the_bot_reports_a_full_bar_the_moment_loading_starts(self):
        """★★ 开局：`0x0400` 广播完就是一发 100，不跟任何人的进度。"""
        self.assertEqual([botsync.LOAD_PROGRESS_MAX],
                         load_frames_in(self.start_sent["bob"], self.bot_seat))

    def test_the_full_bar_goes_out_before_the_stage_seven_packet(self):
        """★ 100% 得**赶在**把大家推进 stage 7 的那一发 `0x0402` 前面。

        排在后面的话客户端已经切场景了，那一格白画。
        """
        frames = self.start_sent["bob"]
        hundred = next(i for i, plain in enumerate(frames)
                       if load_frames_in([plain], self.bot_seat) == [100])
        stage_seven = next(
            i for i, plain in enumerate(frames)
            if len(plain) >= 10 and plain[0] == gameserver.MAGIC_GAME
            and struct.unpack_from("<H", plain, 8)[0]
            == gameserver.OP_COUNT_GAME_READY)
        self.assertLess(hundred, stage_seven)

    def test_every_bot_in_the_room_gets_its_own_full_bar(self):
        for seat in self.room.bot_seats():
            self.assertEqual([botsync.LOAD_PROGRESS_MAX],
                             load_frames_in(self.start_sent["bob"], seat))

    def test_it_is_reported_once_per_load_not_once_per_packet(self):
        """★ 按**状态翻转**去重（铁律 10 的口径），不是每帧都发一发。"""
        self.clear()
        for _ in range(5):
            gameserver._relay_battle_tick(self.alice)
        self.assertEqual([], load_frames(self.bob, self.bot_seat))

    def test_a_map_change_paints_the_bar_again(self):
        """★ 换图的加载界面上也有那几根条 —— `0x0417` 广播完再报一次满。

        `reset_sync_trails()` 正好在那一刻把「报过了」清掉，所以这一发
        不会被去重挡住。
        """
        self.clear()
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REQ_CHANGE_TO_NEXT_MAP,
            gameserver.w_wstr("Quest03_2"))
        self.assertEqual([botsync.LOAD_PROGRESS_MAX],
                         load_frames(self.bob, self.bot_seat))

    def test_the_progress_packets_pass_the_clients_checksum(self):
        for frame in bot_frames_in(self.start_sent["bob"], self.bot_seat):
            self.assertEqual(header(frame)["checksum"],
                             botsync.udp_checksum(body_of(frame)))

    def test_the_full_bar_does_not_move_the_bot(self):
        """`0x4005` 不带坐标 —— 它一发都不该驱动位置帧。"""
        self.assertEqual([], [f for f in bot_frames_in(self.start_sent["bob"],
                                                       self.bot_seat)
                              if udpsync.is_heartbeat(f)])

    def test_a_humans_progress_report_is_ignored(self):
        """★ bot 不再镜像真人报的百分比（D23 -> D26）：条早就满了。"""
        self.clear()
        seat = self.room.seat_index_of(self.alice)
        gameserver.Conn.on_game_packet(
            self.alice, OP_PEER_DATA_UP,
            botsync.build_peer_packet(seat, botsync.OP_LOAD_PROGRESS,
                                      botsync.load_progress_body(42),
                                      game_id=self.room.epoch_value))
        self.assertEqual([], load_frames(self.bob, self.bot_seat))


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


class BotCrouchTests(BotFrameRoom):
    """★ 蹲下（`rpCrouch`，§41）—— 和 `rpJump` 一样是**事件包**回放，
    但它是**状态**不是一次性动作：按下和松开各一发，中间全靠两边记着。

    蹲这一位在收方管三件事：姿势换成 `Crouch*`、**移动速度 × 1/3**、
    **体力恢复 × 2**。所以它必须和坐标的实际步长对上 —— 真人蹲着挪的是
    1/3 步长，bot 抄了坐标却说自己站着，收方就会按 3 倍速度替它走。
    """

    def crouches(self):
        """bot 发出去的 `rpCrouch` 里那个 0/1，按先后顺序。"""
        return [body_of(f)[1] for f in bot_frames(self.alice, self.bot_seat)
                if udpsync.peer_opcode(f) == botsync.OP_CROUCH]

    def walk_past_the_follow_distance(self):
        """再走够 `BOT_FOLLOW_DISTANCE`，让 bot 的落脚点越过刚才那一段。"""
        self.walk(self.alice, [(400, 0), (600, 0), (800, 0)])

    def test_the_bot_crouches_where_the_human_crouched(self):
        self.walk(self.alice, [(0, 0), (200, 0)])
        self.human_crouch(self.alice, True)
        self.walk_past_the_follow_distance()
        self.assertEqual([1], self.crouches())

    def test_the_bot_stands_up_again(self):
        self.walk(self.alice, [(0, 0), (200, 0)])
        self.human_crouch(self.alice, True)
        self.walk_past_the_follow_distance()
        self.human_crouch(self.alice, False)
        self.walk(self.alice, [(1000, 0), (1200, 0), (1400, 0)])
        self.assertEqual([1, 0], self.crouches())

    def test_it_is_sent_once_per_flip_not_once_per_frame(self):
        """★ 去重按**状态翻转**（铁律 10）：蹲着走十帧也只有那一发。

        每发都补的话，事件包的可靠序号会被白白吃掉一大串 —— 而那本账
        和心跳里的 N 是同一本（D5）。
        """
        self.walk(self.alice, [(0, 0), (200, 0)])
        self.human_crouch(self.alice, True)
        self.walk_past_the_follow_distance()
        for x in range(900, 1500, 60):
            self.human_heartbeat(self.alice, x, 0)
        self.assertEqual([1], self.crouches())

    def test_a_bot_that_never_saw_a_crouch_says_nothing(self):
        self.walk(self.alice, [(0, 0), (200, 0), (400, 0), (600, 0)])
        self.assertEqual([], self.crouches())

    def test_the_crouch_state_is_forgotten_on_a_new_map(self):
        """★★ 换图 / 新一局客户端把角色重建、蹲的状态归零（`0x4ffc4a`）。

        两边的记账必须一起清：不清的话 bot 以为自己还蹲着，于是**不发**
        新图上那一发蹲下的 `rpCrouch`，姿势从此对不上。
        """
        self.walk(self.alice, [(0, 0), (200, 0)])
        self.human_crouch(self.alice, True)
        self.walk_past_the_follow_distance()
        self.assertTrue(self.bot_conn.crouched)
        gameserver.reset_sync_trails(self.room, "测试")
        self.assertFalse(self.bot_conn.crouched)
        self.assertFalse(self.alice.sync_crouch)

    def test_dying_forgets_the_crouch_and_the_bot_crouches_again(self):
        """★★ 客户端一死就把 `[char+0x2b5]` 清掉（死亡处理器 `0x4ffbb7`）。

        服务端这边不跟着清，两边的记账就错开一轮：真人还蹲着 ⇒ bot 看不到
        「翻转」⇒ 重生之后**不发**那一发蹲下，姿势一直是站着的。
        """
        self.walk(self.alice, [(0, 0), (200, 0)])
        self.human_crouch(self.alice, True)
        self.walk_past_the_follow_distance()
        self.assertEqual([1], self.crouches())
        # 死了：躺着等重生的那几帧
        self.room.quest.arm_respawn_watchdog(self.bot_seat, (0, 0), after=5.0)
        self.walk(self.alice, [(900, 0), (1000, 0)])
        self.assertFalse(self.bot_conn.crouched)
        # 重生：真人还蹲着 -> bot 必须**再发一发**
        self.room.quest.respawn_due.pop(self.bot_seat, None)
        self.walk(self.alice, [(1100, 0), (1200, 0), (1300, 0)])
        self.assertEqual([1, 1], self.crouches())

    def test_the_replayed_crouch_keeps_the_sequence_bookkeeping_straight(self):
        """★ 事件包和心跳是同一本账（D5）：蹲完之后心跳的 N 要跟着 +1。"""
        self.walk(self.alice, [(0, 0), (200, 0)])
        self.human_crouch(self.alice, True)
        self.walk_past_the_follow_distance()
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
        self.assertEqual(
            gameserver.SyncTrailPoint(111, 222, 0, True, 0, 0, False),
            self.alice.sync_trail[-1])

    def test_a_heartbeat_records_the_motion_facts_too(self):
        """★ 「踩地还是腾空 / 腾空时速度多少」和坐标一起记（§35）。

        bot 回放这条轨迹时**原样抄**这三个量 —— 服务端自己反推速度就是
        「走一步停一下」那个抽搐。
        """
        self.human_heartbeat(self.alice, 111, 222, on_ground=False,
                             velocity=(9, -20))
        point = self.alice.sync_trail[-1]
        self.assertFalse(point.on_ground)
        self.assertEqual((9, -20), (point.vx, point.vy))

    def test_a_heartbeat_records_the_fast_run_flag_too(self):
        """★ 冲刺位（位域 bit3）也和坐标一起记（§40）。"""
        self.human_heartbeat(self.alice, 111, 222, fast_run=True)
        self.assertTrue(self.alice.sync_trail[-1].fast_run)
        self.human_heartbeat(self.alice, 140, 222)
        self.assertFalse(self.alice.sync_trail[-1].fast_run)

    def test_a_jump_is_attached_to_the_next_point(self):
        self.human_heartbeat(self.alice, 111, 222, jumped=2)
        self.assertEqual(2, self.alice.sync_trail[-1].jumped)
        self.assertEqual((111, 222), self.alice.sync_trail[-1][:2])
        self.human_heartbeat(self.alice, 120, 200)
        self.assertEqual(0, self.alice.sync_trail[-1].jumped)
        self.assertEqual((120, 200), self.alice.sync_trail[-1][:2])

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


# ---------------------------------------------------------------------------
# ★★★ 开火（M3b）
# ---------------------------------------------------------------------------
def fire_frames(conn, seat):
    """这个座位发出去的 `rpFire`。"""
    return [f for f in bot_frames(conn, seat)
            if header(f)["opcode"] == botsync.OP_FIRE]


def explode_frames(conn, seat):
    return [f for f in bot_frames(conn, seat)
            if header(f)["opcode"] == botsync.OP_EXPLODE]


class BotFireRoom(BotFrameRoom):
    """**个人战**房（`TEAM_LAYOUT_FREE`）—— 房里每个真人都是 bot 的敌人。

    ★ `BotFrameRoom` 默认是闯关房（`session_type = 2` = `COOP`），那儿
    大家是队友、bot 不开火 —— M3a 的那一大堆用例正好靠这一点不受影响。
    """

    session_type = 1
    arguments = (0, 3, 0)       # 个人战 / 夺分

    def approach(self, x=100.0, y=100.0, settle=True):
        """把 alice 走到 `(x, y)`，让 bot 跟到它身后并进入射程。

        `settle=True` 时顺手把在飞的子弹**结算掉**（见 `settle()`）——
        绝大多数用例关心的是「打出去的那一发长什么样」，不是它飞了多久。
        """
        self.walk(self.alice, [(x, y), (x + 20, y), (x + 40, y)])
        if settle:
            self.settle()

    def arrive(self):
        """让在飞的子弹**立刻到点**（只动飞行时间，不碰别的闸门）。

        ★ 单测里的一帧和一帧之间只差几微秒，而真实的 bot 帧是 ~125 ms ——
        所以「飞到了没有」这个**物理**判据在单测里得手动拨。
        另一个闸门（那一发 `rpFire` 报出去了没有，§50）**照旧生效**，
        它才是「看得见弹体」的那一条。
        """
        machine = self.bot_conn
        machine.pending_shots = [(0.0,) + tuple(rest)
                                 for (_due, *rest) in machine.pending_shots]

    def settle(self):
        """让在飞的子弹**立刻到点**，再走一帧把 `rpExplode` 发出去。

        ★ M3b-2 之后 `rpExplode` 不再紧跟着 `rpFire`，而是等子弹飞到
        （`ballistics` 按 `Velocity` / `GravityFactor` 算出来的时间）。
        单测不真的去等 —— 把到点时刻拨到过去，等价于「时间到了」。

        ★ 结算这一帧**不许再开枪**：否则「几发 `rpFire` 对几发 `rpExplode`」
        永远差一发，用例读起来会莫名其妙。
        """
        machine = self.bot_conn
        if not machine.pending_shots:
            return
        self.arrive()
        hold = machine.next_fire_at
        machine.next_fire_at = time.monotonic() + 3600.0
        try:
            self.walk(self.alice, [tuple(self.alice.sync_trail[-1][:2])])
        finally:
            machine.next_fire_at = hold


class BotFireTests(BotFireRoom):
    """★★ 「射手那台机器」的活现在全在服务端（D28）。"""

    def test_the_bot_shoots_at_a_hostile_human(self):
        self.approach()
        self.assertTrue(fire_frames(self.alice, self.bot_seat),
                        "个人战里 bot 该朝真人开火")

    def test_every_shot_is_followed_by_an_explode(self):
        """★★★ 只发 `rpFire` 的话**没有一台机器**会替 bot 算爆炸
        （守卫 `IsMine || IsNeutral` 在 `0x47eb4e`，§42）——
        子弹一直飞、一滴血不掉。两发必须成对。"""
        self.approach()
        fires = fire_frames(self.alice, self.bot_seat)
        explodes = explode_frames(self.alice, self.bot_seat)
        self.assertTrue(fires)
        self.assertEqual(len(fires), len(explodes))

    def test_the_explode_carries_the_handle_the_client_will_hand_out(self):
        """★★★ M3b 全靠这条：收方每收一发 `rpFire` 就从
        `mgr[0x14+owner*4]` 取当前值当句柄再 `++`，初值
        `座位×100000+100002`（§42 / §43）。对不上就**静默丢弃**。"""
        self.approach()
        explodes = explode_frames(self.alice, self.bot_seat)
        self.assertTrue(explodes)
        first = struct.unpack_from("<i", body_of(explodes[0]), 0)[0]
        self.assertEqual(botsync.projectile_handle(self.bot_seat, 0), first)

    def test_the_explode_targets_the_humans_character_handle(self):
        """`rpExplode +4` = 「打中谁」，角色句柄是 `座位×100000+100001`。"""
        self.approach()
        explodes = explode_frames(self.alice, self.bot_seat)
        target = struct.unpack_from("<i", body_of(explodes[0]), 4)[0]
        alice_seat = self.room.seat_index_of(self.alice)
        self.assertEqual(botsync.character_handle(alice_seat), target)

    def test_the_damage_comes_from_the_weapon_table(self):
        """★ 伤害是**射手算好写进包的**，收方照抄（§42）——
        所以这一格必须是 `weapon.ini` 的 `Damage`，不能是我编的数。"""
        self.approach()
        weapon = self.bot_conn.weapon
        self.assertIsNotNone(weapon)
        damage = struct.unpack_from("<f", body_of(
            explode_frames(self.alice, self.bot_seat)[0]), 24)[0]
        self.assertAlmostEqual(float(weapon.damage), damage)

    def test_the_fire_packet_says_ten_plus_the_bot_seat(self):
        """`rpFire +0` 是 `10 + 座位号`，语料 7040 发和头 `+1` 100% 一致。"""
        self.approach()
        body = body_of(fire_frames(self.alice, self.bot_seat)[0])
        self.assertEqual(botsync.FIRE_SOURCE_PLAYER_BASE + self.bot_seat,
                         body[0])
        self.assertEqual(self.bot_conn.weapon.id,
                         struct.unpack_from("<i", body, 2)[0])

    def test_handles_keep_advancing_across_shots(self):
        """连打几发：句柄必须**严格连续**地往前走，一格都不许跳。"""
        for _ in range(6):
            self.bot_conn.next_fire_at = 0.0
            self.approach()
        explodes = explode_frames(self.alice, self.bot_seat)
        self.assertGreater(len(explodes), 2)
        step = self.bot_conn.weapon.handle_step
        got = [struct.unpack_from("<i", body_of(f), 0)[0] for f in explodes]
        want = [botsync.projectile_handle(self.bot_seat, i * step)
                for i in range(len(got))]
        self.assertEqual(want, got)

    def test_the_cooling_time_holds_fire_between_shots(self):
        """★ 开火间隔来自 `weapon.ini` 的 `CoolingTime`（D29）——
        原版这把枪就是这个节奏，不是我拍脑袋的常量。"""
        self.approach()
        self.clear()
        self.approach()          # 冷却还没过，这一轮不该有新的 rpFire
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))

    def test_the_bot_aims_at_its_target(self):
        """★ 准星摆在目标身上 ⇒ 朝向位 / 角度 / `Run-F` vs `Run-B`
        全跟着走（§37 / §39）—— 这个游戏的朝向跟准星，不跟移动方向。"""
        self.approach(x=400.0)
        beats = [f for f in bot_frames(self.alice, self.bot_seat)
                 if udpsync.is_heartbeat(f)]
        cursor_x = struct.unpack_from("<h", body_of(beats[-1]),
                                      7 + 0x12)[0]
        bot_x = udpsync.heartbeat_position(beats[-1])[0]
        alice_x = self.alice.sync_trail[-1][0]
        # bot 跟在真人**后面**，所以准星应该指向前方（真人那一侧）。
        self.assertGreater(cursor_x, bot_x)
        self.assertLessEqual(abs(cursor_x - alice_x), 2)

    def test_a_dead_human_is_not_shot_at(self):
        """躺着等重生的人不该再挨枪 —— 打他一发只会让收方对着尸体算伤害。"""
        self.approach()
        # 和真人真的死掉时一样：广播死亡之后服务端给他上重生闩。
        self.room.quest.arm_respawn_watchdog(
            self.room.seat_index_of(self.alice), (100.0, 100.0))
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.approach()
        self.assertEqual([], fire_frames(self.bob, self.bot_seat))

    def test_a_bot_without_a_usable_weapon_never_fires(self):
        """★ 角色一把可用武器都没有时 bot 只跑不打 ——
        **不许随便挑一把凑合的**：步进猜错 = 子弹飞过去不炸，而且静默。

        ★ 会话 14 之后 14 个玩家角色全都三个槽位齐活（§45 / §47），所以
        这里拿一个**表里根本没有**的角色 id 来造这个局面（真实场景是
        产物缺失 / 版本对不上，`weapondata` 那时整张表都是空的）。
        """
        self.bot_conn.character_id = 4242
        self.assertIsNone(self.bot_conn.weapon)
        self.approach()
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))

    def test_firing_does_not_break_the_heartbeat_sequence(self):
        """★ `rpFire` / `rpExplode` 都是事件包 —— 心跳里的 N 必须跟着涨，
        否则收方 `FlushTo(N)` 会把它俩判死（D5 不变式 2）。"""
        self.approach()
        frames = bot_frames(self.alice, self.bot_seat)
        events = sum(1 for f in frames if not udpsync.is_heartbeat(f))
        self.assertEqual(events, self.bot_conn.sync.events)


def change_weapon_frames(conn, seat):
    return [f for f in bot_frames(conn, seat)
            if header(f)["opcode"] == botsync.OP_CHANGE_WEAPON]


class BotWeaponDeclarationTests(BotFireRoom):
    """★ `rpChangeWeapon` —— 不发的话别人看到的枪和打出来的子弹对不上。

    用户 2026-08-26 报的「bot 好像只会用 1 武器」看到的就是这个：
    客户端建角色时给的是它自己的默认武器，而 bot 的 `rpFire` 里带的是
    `weapondata` 挑的那把（角色 1 / 100 / 103 的首选是 **3 号槽**）。
    """

    def test_the_bot_announces_its_weapon_before_the_first_shot(self):
        self.approach()
        frames = bot_frames(self.alice, self.bot_seat)
        changes = [i for i, f in enumerate(frames)
                   if header(f)["opcode"] == botsync.OP_CHANGE_WEAPON]
        fires = [i for i, f in enumerate(frames)
                 if header(f)["opcode"] == botsync.OP_FIRE]
        self.assertTrue(changes and fires)
        self.assertLess(changes[0], fires[0], "声明必须排在第一发之前")

    def test_the_announced_weapon_is_the_one_it_shoots(self):
        self.approach()
        body = body_of(change_weapon_frames(self.alice, self.bot_seat)[0])
        seat, ammo = struct.unpack_from("<Bi", body, 0)
        self.assertEqual(self.bot_seat, seat)
        self.assertEqual(self.bot_conn.weapon.id, ammo)
        fire_ammo = struct.unpack_from(
            "<i", body_of(fire_frames(self.alice, self.bot_seat)[0]), 2)[0]
        self.assertEqual(ammo, fire_ammo)

    def test_it_is_announced_once_not_every_shot(self):
        """★ 按状态翻转去重（铁律 10）：`rpChangeWeapon` 是**事件包**，
        每发都声明会白吃可靠序号，动画上还会一直抽。"""
        for _ in range(5):
            self.bot_conn.next_fire_at = 0.0
            self.approach()
        self.assertEqual(1, len(change_weapon_frames(self.alice,
                                                     self.bot_seat)))

    def test_a_map_change_makes_it_announce_again(self):
        """换图 / 新一局客户端重建角色，武器回默认 —— 两边的记账必须一起清
        （和 `crouched` 同一个坑，§41）。"""
        self.approach()
        self.assertIsNotNone(self.bot_conn.declared_weapon)
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REQ_CHANGE_TO_NEXT_MAP,
            gameserver.w_wstr("Stage02"))
        self.assertIsNone(self.bot_conn.declared_weapon)
        self.clear()
        self.approach(x=300.0)
        self.assertTrue(change_weapon_frames(self.alice, self.bot_seat))


class BotGunCommandTests(BotFireRoom):
    """`/gun N M` —— 房主手动换枪（自动换是 M5 的事，见 `_cmd_gun` 的注释）。"""

    def gun(self, *args):
        """敲一条 `/gun`，返回房主看到的那几行系统提示。"""
        self.alice.sent.clear()
        self.assertTrue(bot.handle_command(
            self.alice, "/gun " + " ".join(map(str, args))))
        return "".join(chat_lines(self.alice))

    def test_listing_shows_the_usable_slots(self):
        self.assertIn("可用武器槽", self.gun(self.bot_seat))

    def test_switching_changes_the_ammo_and_the_handle_step(self):
        """★ 换到步进 2 的武器之后，句柄必须**跟着每发走 2 格** ——
        步进错了就是「子弹飞过去不炸」，而且静默（§42）。"""
        choices = weapondata.usable_for(self.bot_conn.character_id)
        heavy = [w for w in choices if w.handle_step == 2]
        if not heavy:
            self.skipTest("这个角色没有步进 2 的武器")
        slot = heavy[0].raw["slot"]
        self.gun(self.bot_seat, slot)
        self.assertEqual(heavy[0].id, self.bot_conn.weapon.id)
        self.clear()
        for _ in range(3):
            self.bot_conn.next_fire_at = 0.0
            self.approach()
        got = [struct.unpack_from("<i", body_of(f), 0)[0]
               for f in explode_frames(self.alice, self.bot_seat)]
        want = [botsync.projectile_handle(self.bot_seat, i * 2)
                for i in range(len(got))]
        self.assertEqual(want, got)

    def test_an_unusable_slot_is_refused_with_the_choices(self):
        self.assertIn("没有能用的 9 号武器槽", self.gun(self.bot_seat, 9))

    def test_the_slot_survives_a_map_change(self):
        """★ 房主的指令不是「一图之内的机器状态」，换图不该把它清掉。"""
        choices = weapondata.usable_for(self.bot_conn.character_id)
        slot = choices[-1].raw["slot"]
        self.gun(self.bot_seat, slot)
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REQ_CHANGE_TO_NEXT_MAP,
            gameserver.w_wstr("Stage02"))
        self.assertEqual(slot, self.bot_conn.weapon_slot)

    def test_switching_to_a_slot_the_new_character_lacks_falls_back(self):
        """`/char` 换到一个没有那个槽位的角色 —— 退回首选，**不是**哑火。

        角色 3（아이린，玩家选不到）的 3 号槽是 `TotemLauncher`，
        `Damage=0` 打不动人 ⇒ 不在 `usable` 里，正好当这个局面的样本。
        """
        self.bot_conn.weapon_slot = 1
        self.bot_conn.character_id = 3
        self.assertIsNotNone(self.bot_conn.weapon)
        self.bot_conn.weapon_slot = 3       # 角色 3 的 slot3 伤害 0，不可用
        self.assertEqual(weapondata.preferred_for(3).id,
                         self.bot_conn.weapon.id)


class BotHoldCommandTests(BotFireRoom):
    """`/hold N` —— 让 bot 站住，好测「隔着墙打不打得到」。"""

    def hold(self, *args):
        self.alice.sent.clear()
        self.assertTrue(bot.handle_command(
            self.alice, "/hold " + " ".join(map(str, args))))
        return "".join(chat_lines(self.alice))

    def test_holding_freezes_the_position_but_keeps_the_heartbeat(self):
        """★ **照常发心跳** —— 真人站着不动时也一直在发，停发反而是异常。"""
        self.approach()
        self.hold()
        self.assertTrue(self.bot_conn.holding)
        frozen = self.bot_conn.battle_pos
        self.clear()
        self.walk(self.alice, [(600, 100), (700, 100), (800, 100)])
        beats = [f for f in bot_frames(self.alice, self.bot_seat)
                 if udpsync.is_heartbeat(f)]
        self.assertTrue(beats, "站住之后心跳不能停")
        self.assertEqual(frozen, self.bot_conn.battle_pos)

    def test_holding_toggles_back(self):
        self.approach()
        self.hold()
        self.hold()
        self.assertFalse(self.bot_conn.holding)

    def test_a_held_bot_stops_walking_in_the_heartbeat(self):
        """站住 = 站姿：按键掩码清零，收方才画 `Stand`（§39）。"""
        self.approach()
        self.hold()
        self.clear()
        self.walk(self.alice, [(600, 100), (700, 100)])
        beats = [f for f in bot_frames(self.alice, self.bot_seat)
                 if udpsync.is_heartbeat(f)]
        for frame in beats:
            self.assertEqual(0, body_of(frame)[7 + 0x10])

    def test_holding_still_lets_it_shoot(self):
        """★ 站住正是为了让房主走开、绕到墙后 —— 开火那一段照跑。"""
        self.approach()
        self.hold()
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(120, 100), (130, 100)])
        self.assertTrue(fire_frames(self.alice, self.bot_seat))


class BotBallisticFireTests(BotFireRoom):
    """★★ M3b-2：弹道 + **延后爆炸**（会话 14，§47 / §48）。"""

    def gun(self, *args):
        self.alice.sent.clear()
        self.assertTrue(bot.handle_command(
            self.alice, "/gun " + " ".join(map(str, args))))
        return "".join(chat_lines(self.alice))

    def test_the_explode_waits_for_the_bullet_to_arrive(self):
        """★ `rpExplode` 不再紧跟着 `rpFire` —— 子弹得先飞到。

        以前那版一出膛就炸，别人屏幕上子弹刚出枪口就爆。
        """
        self.approach(settle=False)
        self.assertTrue(fire_frames(self.alice, self.bot_seat))
        self.assertEqual([], explode_frames(self.alice, self.bot_seat))
        self.assertTrue(self.bot_conn.pending_shots)
        self.settle()
        self.assertTrue(explode_frames(self.alice, self.bot_seat))
        self.assertEqual([], self.bot_conn.pending_shots)

    def test_the_flight_time_comes_from_the_weapon(self):
        """等多久是 `ballistics` 按 `Velocity` 算的**物理量**，
        不是「等 XX 毫秒再说」那种观测阈值（铁律 10 的例外）。"""
        self.approach(settle=False)
        due = self.bot_conn.pending_shots[0][0]
        self.assertGreater(due, 0.0)
        self.assertLess(due - time.monotonic(), 1.0)

    def test_a_pending_explosion_still_goes_out_while_the_bot_is_dead(self):
        """★★ **一发都不能漏**：句柄记账在开火那一刻就推进了，少发一发
        收方那一格就永久错位（§42）。真人死了，他打出去的子弹也照样在飞。"""
        self.approach(settle=False)
        self.assertTrue(self.bot_conn.pending_shots)
        self.room.quest.arm_respawn_watchdog(self.bot_seat, (10.0, 10.0))
        self.clear()
        self.settle()
        self.assertTrue(explode_frames(self.alice, self.bot_seat),
                        "bot 躺着也得把在飞的子弹炸掉")

    def test_a_map_change_drops_the_bullets_in_flight(self):
        """换图时收方的弹体表和句柄计数器整个复位 —— 补发只会拿一个
        查不到的句柄去撞 `0x492750` 那个静默丢弃。"""
        self.approach(settle=False)
        self.assertTrue(self.bot_conn.pending_shots)
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REQ_CHANGE_TO_NEXT_MAP,
            gameserver.w_wstr("Stage02"))
        self.assertEqual([], self.bot_conn.pending_shots)

    def test_the_explosion_lands_where_the_target_is_now(self):
        """★ 子弹飞了几百毫秒，人早走开了 —— 照开火时的老坐标炸的话
        别人会看见爆炸出现在空地上。"""
        self.approach(settle=False)
        self.walk(self.alice, [(900.0, 100.0)])
        self.settle()
        body = body_of(explode_frames(self.alice, self.bot_seat)[-1])
        x = struct.unpack_from("<f", body, 8)[0]
        self.assertAlmostEqual(self.alice.sync_trail[-1][0], x, places=3)

    def test_the_bot_shoots_from_across_the_screen(self):
        """★★★ 用户 2026-08-26 报的那条：「距离远了 bot 就不开枪，
        站在身边才开枪」。射程口径从 `LockonRange`（80~120）换成语料量出来的
        交战距离 1000 之后，隔半个屏幕也该开火（§48）。"""
        self.approach()
        bot.handle_command(self.alice, f"/hold {self.bot_seat}")
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        far = self.bot_conn.battle_pos[0] + 700.0
        self.walk(self.alice, [(far, 100.0), (far + 5.0, 100.0)])
        self.assertTrue(fire_frames(self.alice, self.bot_seat),
                        "700 个单位是原版玩家常见的交战距离，必须开火")

    def test_nothing_is_shot_at_beyond_the_engagement_range(self):
        self.approach()
        bot.handle_command(self.alice, f"/hold {self.bot_seat}")
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        far = self.bot_conn.battle_pos[0] + bot.BOT_ENGAGE_RANGE + 200.0
        self.walk(self.alice, [(far, 100.0), (far + 5.0, 100.0)])
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))

    def test_a_parabolic_weapon_aims_upwards_and_charges(self):
        """★ 用户 2026-08-26 报的另一条：「绝大多数角色 2 号武器是抛物线
        发射，鼠标长按蓄力，松开按抛物线扔出」。

        ⇒ `rpFire` 的角度要**朝上**（y 往下增长，所以是负的），
        `power` 也不能再是那个恒 1.0（蓄力武器靠它定初速，§47）。
        """
        self.approach()
        self.gun(self.bot_seat, 2)
        weapon = self.bot_conn.weapon
        self.assertTrue(weapon.gravity, "2 号槽应该是抛物线武器")
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(300.0, 100.0), (310.0, 100.0)])
        fires = fire_frames(self.alice, self.bot_seat)
        self.assertTrue(fires, "抛物线武器也得打得出去")
        body = body_of(fires[0])
        angle, power = struct.unpack_from("<ff", body, 14)
        self.assertLess(angle, 0.0, "打平地也要抬枪口")
        self.assertNotEqual(botsync.FIRE_POWER_FIXED, power)
        self.assertTrue(8.0 <= power <= 531.0, f"power={power} 超出语料区间")

    def test_a_spread_weapon_explodes_every_fragment(self):
        """★★ `SpreadFrags = N` 一发造 N 颗弹体、吃 N 个连号句柄（§46）。
        少炸一颗，下一发的句柄就错位。"""
        self.bot_conn.character_id = 1        # `CH01-01`：SpreadFrags = 3
        weapon = weapondata.slot_for(1, 1)
        self.assertEqual(3, weapon.shots)
        self.bot_conn.weapon_slot = 1
        self.approach()
        fires = fire_frames(self.alice, self.bot_seat)
        explodes = explode_frames(self.alice, self.bot_seat)
        self.assertEqual(1, len(fires))
        self.assertEqual(3, len(explodes))
        handles = [struct.unpack_from("<i", body_of(f), 0)[0] for f in explodes]
        base = botsync.projectile_handle(self.bot_seat, 0)
        self.assertEqual([base, base + 1, base + 2], handles)
        self.assertEqual(3, struct.unpack_from("<i", body_of(fires[0]), 22)[0])

    def test_a_splash_weapon_holds_fire_until_its_bullet_exploded(self):
        """★★ 溅射那多出来的一个句柄，到底是开火时分配的还是爆炸时分配的，
        语料分不出来 —— 两种假设只有在「上一发炸完再开下一枪」时才同解。
        所以这类武器有一道顺序闸门（`_may_fire`）。"""
        splash = [w for w in weapondata.usable_for(self.bot_conn.character_id)
                  if w.splash_range]
        self.assertTrue(splash, "这个角色应该有带溅射的武器")
        self.bot_conn.weapon_slot = splash[0].raw["slot"]
        self.approach(settle=False)
        self.assertTrue(self.bot_conn.pending_shots)
        self.clear()
        self.bot_conn.next_fire_at = 0.0      # 冷却过了，但上一发还在飞
        self.walk(self.alice, [(200.0, 100.0), (210.0, 100.0)])
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))

    def test_a_plain_bullet_does_not_wait(self):
        """★ 不带溅射的武器 `handle_step == shots`，分配全在开火那一刻 ——
        怎么交错都对，**不设闸**，否则射速会白白掉一半。"""
        plain = [w for w in weapondata.usable_for(self.bot_conn.character_id)
                 if not w.splash_range]
        self.assertTrue(plain)
        self.bot_conn.weapon_slot = plain[0].raw["slot"]
        self.approach(settle=False)
        self.assertTrue(self.bot_conn.pending_shots)
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(200.0, 100.0), (210.0, 100.0)])
        self.assertTrue(fire_frames(self.alice, self.bot_seat))


class BotMagazineTests(BotFireRoom):
    """★★ 弹匣：连打 `MagazineCount` 发，打空停 `ReloadTime`（会话 15）。

    用户 2026-08-26：「2 号角色，1 号武器没有 CD，开枪太频繁了，
    一会儿就把我秒死了。这个游戏里所有武器都有 CD 才对。」
    """

    def test_the_magazine_empties_then_reloads(self):
        """`CH01-01`：`CoolingTime=200 / MagazineCount=2 / ReloadTime=1200`
        ⇒ 打两发之后那一发的间隔必须是 **1200**，不是 200。"""
        weapon = weapondata.slot_for(1, 1)
        self.assertEqual(2, weapon.magazine)
        machine = self.bot_conn
        machine.rounds_left = None
        first = bot._reload_after_shot(machine, weapon, 0.0)
        self.assertAlmostEqual(weapon.cooling_ms / 1000.0, first)
        self.assertEqual(1, machine.rounds_left)
        second = bot._reload_after_shot(machine, weapon, 0.0)
        self.assertAlmostEqual(weapon.reload_ms / 1000.0, second)
        self.assertIsNone(machine.rounds_left, "换完弹匣要是满的")

    def test_the_sustained_rate_matches_the_original(self):
        """★ 一个完整循环 = `MagazineCount` 发 + 一次换弹。
        只看 `CoolingTime` 的话角色 1 的 1 号枪快 5 倍（还是三连散弹）。"""
        weapon = weapondata.slot_for(1, 1)
        machine = self.bot_conn
        machine.rounds_left = None
        clock = 0.0
        for _ in range(weapon.magazine):
            clock = bot._reload_after_shot(machine, weapon, clock)
        cycle = (weapon.cooling_ms * (weapon.magazine - 1)
                 + weapon.reload_ms) / 1000.0
        self.assertAlmostEqual(cycle, clock)

    def test_a_weapon_without_a_magazine_keeps_the_old_rhythm(self):
        """榴弹那一类打一发装一次 —— `fire_interval_ms` 本来就对。"""
        weapon = weapondata.slot_for(0, 2)
        self.assertIsNone(weapon.magazine)
        gap = bot._reload_after_shot(self.bot_conn, weapon, 0.0)
        self.assertAlmostEqual(weapon.fire_interval_ms / 1000.0, gap)

    def test_switching_weapons_starts_from_a_full_magazine(self):
        """换枪 = 换弹匣（真人切枪也是这样）。"""
        self.approach()
        self.bot_conn.rounds_left = 1
        self.bot_conn.weapon_slot = 3
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(200.0, 100.0), (210.0, 100.0)])
        self.assertNotEqual(1, self.bot_conn.rounds_left)

    def test_a_map_change_reloads(self):
        self.approach()
        self.bot_conn.rounds_left = 1
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REQ_CHANGE_TO_NEXT_MAP,
            gameserver.w_wstr("Stage02"))
        self.assertIsNone(self.bot_conn.rounds_left)


class BotVisibleBulletTests(BotFireRoom):
    """★★★ 「看得见弹体」—— `rpExplode` 必须排在**下一个 N** 里（§50）。

    用户 2026-08-26：「所有武器现在看不见弹体，bot 有开枪动画，
    但是看不见子弹，过一会儿我就凭空被打中了。」

    根因不在弹道，在**收方的可靠队列**：事件包要等一发带更大 N 的心跳
    `FlushTo(N)` 才会被执行。`rpFire` 和 `rpExplode` 挤在同一个 N 里的话，
    收方会在同一瞬间造出弹体又把它炸掉。
    """

    def fire_once(self):
        """走一帧、正好打一发，返回那一发 `rpFire` 的事件序号。"""
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        before = self.bot_conn.sync.events
        self.walk(self.alice, [(140.0, 100.0)])
        self.assertTrue(fire_frames(self.alice, self.bot_seat), "这一帧该开火")
        return before

    def test_the_explode_waits_for_a_heartbeat_to_announce_the_fire(self):
        self.approach()
        fire_seq = self.fire_once()
        # 到点了 —— 但那一发 rpFire 还没被任何一发心跳报出去。
        self.bot_conn.pending_shots = [(0.0,) + tuple(rest)
                                       for (_due, *rest)
                                       in self.bot_conn.pending_shots]
        self.assertLessEqual(self.bot_conn.sync.announced, fire_seq)
        self.clear()
        self.bot_conn.next_fire_at = time.monotonic() + 3600.0
        self.walk(self.alice, [(150.0, 100.0)])
        self.assertEqual([], explode_frames(self.alice, self.bot_seat),
                         "开火那一发还没露面，这时候炸 = 别人看不见子弹")
        # 这一帧的心跳把它报出去了，下一帧才轮到爆炸。
        self.assertGreater(self.bot_conn.sync.announced, fire_seq)
        self.clear()
        self.walk(self.alice, [(160.0, 100.0)])
        self.assertTrue(explode_frames(self.alice, self.bot_seat))

    def test_the_fire_and_the_explode_never_share_one_flush(self):
        """★★ 判据钉在**发出去的字节**上：每一发 `rpFire` 和它的 `rpExplode`
        之间，必须夹着一发 **N > 开火序号** 的心跳。

        那一发心跳就是收方 `FlushTo(N)` 造出弹体的那一刻；爆炸排在它后面，
        弹体才有一整个心跳的寿命。夹不住 = 别人屏幕上看不见子弹。
        """
        self.approach()
        self.clear()
        x = 200.0
        for _ in range(8):
            self.bot_conn.next_fire_at = 0.0
            x += 30.0
            self.walk(self.alice, [(x, 100.0)])   # 这一帧开火
            self.arrive()          # 子弹瞬间到点（单测不等真实时间）
            for _step in range(2):                # 后面两帧才轮到爆炸
                x += 30.0
                self.walk(self.alice, [(x, 100.0)])
        frames = bot_frames(self.alice, self.bot_seat)
        fires = [i for i, f in enumerate(frames)
                 if header(f)["opcode"] == botsync.OP_FIRE]
        explodes = [i for i, f in enumerate(frames)
                    if header(f)["opcode"] == botsync.OP_EXPLODE]
        self.assertTrue(fires)
        self.assertTrue(explodes)
        for start in fires:
            seq = header(frames[start])["sequence"]
            after = [i for i in explodes if i > start]
            if not after:
                continue                       # 还在飞，这一局没炸完
            beats = [i for i in range(start + 1, after[0])
                     if udpsync.is_heartbeat(frames[i])
                     and struct.unpack_from("<H", body_of(frames[i]), 0)[0] > seq]
            self.assertTrue(beats,
                            f"第 {seq} 号 rpFire 和它的爆炸之间没有心跳")


class BotCoopNoFireTests(BotFrameRoom):
    """闯关房（`TEAM_LAYOUT_COOP`）：**一枪都不许开**。

    那儿真人是队友，该打的是怪 —— 而怪的句柄服务端手里没有（它们由
    「控制者」那台机器模拟），要打得等 M5 把控制格那条路接起来。
    """

    def test_the_bot_never_shoots_its_teammates(self):
        self.walk(self.alice, [(100, 100), (120, 100), (140, 100)])
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))
        self.assertEqual([], explode_frames(self.alice, self.bot_seat))


class BotTeamFireTests(BotFrameRoom):
    """组队战（`TEAM_LAYOUT_TEAMS`）：只打**对面**那一队。"""

    session_type = 1
    arguments = (1, 0, 0)       # 组队战

    def test_the_bot_holds_fire_against_its_own_team(self):
        """把 bot 挪到和 alice 同一队 —— 一枪都不该有。"""
        alice_seat = self.room.seat_index_of(self.alice)
        self.room.seats[self.bot_seat].team = self.room.seats[alice_seat].team
        self.walk(self.alice, [(100, 100), (120, 100), (140, 100)])
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))

    def test_the_bot_shoots_the_other_team(self):
        alice_seat = self.room.seat_index_of(self.alice)
        alice_team = self.room.seats[alice_seat].team
        self.room.seats[self.bot_seat].team = (
            TEAM_B if alice_team == TEAM_A else TEAM_A)
        self.walk(self.alice, [(100, 100), (120, 100), (140, 100)])
        self.assertTrue(fire_frames(self.alice, self.bot_seat))


class BotFireHandleResetTests(BotFireRoom):
    """★★ 换图 / 新一局：客户端把弹体计数器清回 `座位×100000+100002`
    （语料实测，§43 第 4 条）。两边不一起清，第二张图上全打不中。"""

    def test_a_map_change_rewinds_the_projectile_counter(self):
        for _ in range(4):
            self.bot_conn.next_fire_at = 0.0
            self.approach()
        self.assertGreater(self.bot_conn.sync.projectiles, 0)
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REQ_CHANGE_TO_NEXT_MAP,
            gameserver.w_wstr("Stage02"))
        self.assertEqual(0, self.bot_conn.sync.projectiles)

    def test_the_ctl_nextmap_command_takes_the_real_path(self):
        """★★ `nextmap` 是**对战房里验换图的唯一手段**（原版只有闯关会换图，
        而闯关里 bot 一枪不开）。所以它必须和客户端 `0x0411` 走同一段代码 ——
        **全房间广播 + `reset_sync_trails()`**。

        它要是只给自己发一发 `0x0417`（改之前就是那样），验出来的会是一个
        **真实游戏里不存在**的故障：客户端那边的弹体计数器清了、服务端这边
        没清，于是「换图后打不中」。工具本身成了陷阱。
        """
        for _ in range(3):
            self.bot_conn.next_fire_at = 0.0
            self.approach()
        self.assertGreater(self.bot_conn.sync.projectiles, 0)
        # 控制通道按账号找连接（`all_conns()`），而测试里的假连接不走
        # `register_conn()` —— 这里临时挂进去，跑完摘掉。
        gameserver.register_conn(self.alice)
        self.addCleanup(gameserver.unregister_conn, self.alice)
        reply = gameserver.handle_control_command(
            "nextmap Stage02 --user " + self.alice.account_name)
        self.assertTrue(reply.startswith("ok"), reply)
        self.assertEqual(0, self.bot_conn.sync.projectiles)
        self.assertEqual(0, len(self.alice.sync_trail))
        self.assertIsNone(self.bot_conn.declared_weapon)
        # ★ 广播到位：房里**每个人**都收到了 0x0417，不只发起的那一个。
        self.assertIn(gameserver.OP_REP_CHANGE_TO_NEXT_MAP, opcodes(self.bob))

    def test_the_first_shot_on_the_new_map_starts_from_the_base_handle(self):
        for _ in range(3):
            self.bot_conn.next_fire_at = 0.0
            self.approach()
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REQ_CHANGE_TO_NEXT_MAP,
            gameserver.w_wstr("Stage02"))
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.approach(x=300.0)
        explodes = explode_frames(self.alice, self.bot_seat)
        self.assertTrue(explodes)
        self.assertEqual(botsync.projectile_handle(self.bot_seat, 0),
                         struct.unpack_from("<i", body_of(explodes[0]), 0)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
