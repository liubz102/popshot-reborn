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
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ballistics                                              # noqa: E402
import bot                                                     # noqa: E402
import botnav                                                  # noqa: E402
import botplan                                                 # noqa: E402
import botsync                                                 # noqa: E402
import botthreat                                               # noqa: E402
import chrprops                                                # noqa: E402
import botmove                                            # noqa: E402
import test_mapdata                                            # noqa: E402
import gameserver                                              # noqa: E402
import lobby                                                   # noqa: E402
import mapdata                                                 # noqa: E402
import relayserver                                             # noqa: E402
import roomclock                                               # noqa: E402
import udpsync                                                 # noqa: E402
import weapondata                                              # noqa: E402
from gameserver import OP_LEAVE_SESSION, OP_PEER_DATA_DOWN, \
    OP_PEER_DATA_UP                                            # noqa: E402
from test_battle import bodies, opcodes                        # noqa: E402
from lobby import TEAM_A, TEAM_B                                # noqa: E402
from test_bot import BotBattleRoom, chat_lines                 # noqa: E402
from test_mapdata import make_record                           # noqa: E402

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
#: ★ **座位 1** 的一发 `rpFire`（`bug调查/8/server-logs/game_003_27799.txt`
#: 11:14:38.419 那一发）。留着它就为了钉死 body `+1`：
#: 座位 0 那一发是 `0a 01`、这一发是 `0b 02` —— 同一把枪 `1001010`，
#: 换了个人打就换了个数 ⇒ 它**不可能是武器槽**，是碰撞排除组（§63）。
REAL_FIRE_SEAT1 = bytes.fromhex(
    "ff01ff000100048613000200"
    "0b0232460f0000008b4400000044911e1dc00000803f03000000")


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
            struct.unpack_from("<f", real, 14)[0], 132.0, shots=1)
        self.assertEqual(real, mine)

    def test_fire_body_plus_one_is_the_collision_group_not_a_weapon_slot(self):
        """★★★ §63：`+1` 是发射者的**碰撞排除组**。

        同一把枪 `1001010`：座位 0 打出来是 `0a 01`、座位 1 打出来是
        `0b 02` —— 换个人就换个数，所以它不可能是「武器槽」。
        以前这一格被写死成 1，结果个人战里座位 0 那个人**身上一发都撞不着**
        （组号相同 ⇒ 收方 `0x48042e` 直接跳过碰撞），而服务端自己发的
        `rpExplode` 照样扣血 = 用户 2026-08-27 报的「明明躲开了还掉血」。
        """
        for packet, seat, group in ((REAL_FIRE, 0, 1), (REAL_FIRE_SEAT1, 1, 2)):
            real = body_of(packet)
            self.assertEqual(botsync.FIRE_SOURCE_PLAYER_BASE + seat, real[0])
            self.assertEqual(group, real[1])
            self.assertEqual(group, botsync.fire_group(seat))
            mine = botsync.fire_body(
                seat, struct.unpack_from("<i", real, 2)[0],
                *struct.unpack_from("<ffff", real, 6),
                shots=struct.unpack_from("<i", real, 22)[0])
            self.assertEqual(real, mine, "逐字节和真包对不上")

    def test_the_group_is_the_team_in_a_team_room(self):
        """★ 组队 / 闯关房里同一队的人共用一个组 ⇒ 子弹从队友身上穿过去
        （原版「没有友军伤害」就是这么实现的）。语料里同一局出现过
        「座位 1 和座位 2 都发 1」，正是这种情况。"""
        self.assertEqual(1, botsync.fire_group(0, team=1))
        self.assertEqual(1, botsync.fire_group(2, team=1))
        self.assertEqual(2, botsync.fire_group(1, team=2))
        self.assertEqual(2, botsync.fire_group(3, team=2))
        # 个人战（`TEAM_NONE`）各是各的。
        self.assertEqual([1, 2, 3, 4, 5, 6],
                         [botsync.fire_group(s, team=0) for s in range(6)])

    def test_dash_is_eleven_bytes_seat_direction_and_index(self):
        """`0x0007 rpDash`（§64）：语料 4394 发，`+0` 和发送方 100% 一致、
        `+1` 只有 ±1、`+2` 恒 0。"""
        body = botsync.dash_body(3, botsync.DASH_LEFT, 0, 120.0, 480.0)
        self.assertEqual(11, len(body))
        self.assertEqual((3, -1, 0), struct.unpack_from("<Bbb", body, 0))
        self.assertEqual((120.0, 480.0), struct.unpack_from("<ff", body, 3))
        self.assertEqual(1, botsync.dash_body(0, 1, 0, 0.0, 0.0)[1])

    def test_splash_damaged_is_thirty_three_bytes(self):
        """`0x0004 rpSplashDamaged`（§67）：`+0` 伤害源句柄、`+4` 受害者的
        角色句柄、`+8` 伤害；`+12` 和 `+29` 语料 13160 发恒 0。"""
        body = botsync.splash_body(200034, botsync.character_handle(0),
                                   12.0, 125.0, 603.0)
        self.assertEqual(33, len(body))
        self.assertEqual((200034, 100001), struct.unpack_from("<ii", body, 0))
        self.assertAlmostEqual(12.0, struct.unpack_from("<f", body, 8)[0])
        self.assertEqual(0, body[12])
        self.assertEqual(0, struct.unpack_from("<i", body, 29)[0])

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

    ★★ **帧由房间那条 32 ms 循环推**（D106，废止 D17）。单测里
    `gameserver.ROOM_LOOP_THREADED` 是关着的（只有 `app.py` 打开），
    所以拍子在这儿手动数 —— 跑的是**同一段** `RoomLoop.run_tick()`，
    不是给测试开的后门。

    `human_heartbeat()` 喂一发真人心跳之后顺手推 `HEARTBEAT_TICKS` 格，
    于是「一发真人心跳 ≈ 一发 bot 心跳」这个老口径继续成立；要逐格看的
    用例直接调 `advance(n)`。
    """

    #: ★★ 「进图 / 复活之后 2 秒不能动手」那道锁（§74）默认**放开**。
    #:
    #: 不放开的话这一大批用例全废：它们在几微秒里喂完整局心跳，而真实的
    #: 2 秒锁会把每一发 `rpFire` 都挡掉。锁本身有 `BotActionLockTests`
    #: 单独验（同 `BotFireRoom.melee` 的路数）。
    action_lock = False

    #: ★★ 瞄准失误（M5-D）默认**钉成不失误**。
    #:
    #: `BotConn.roll` 默认是 `random.randrange`，而 M5-D 之后中等难度有 22%
    #: 的概率把这一发打歪 —— 那会让「打得中、扣得掉血」这一大批用例
    #: **随机变红**。这里把骰子钉成「永远掷到最大值」= 永不失误；
    #: 专门验失误的用例自己改 `bot_conn.roll`。
    #: ⚠ 同一颗骰子还管碎片角度（`_slice_angles`）和重生点挑选 ——
    #: 那两处本来就是随机的，用例要钉就自己钉（好几处已经这么做了）。
    pin_roll = staticmethod(lambda n: n - 1)

    def setUp(self):
        super().setUp()
        if not self.action_lock:
            self.unlock_bots()
        if self.pin_roll is not None:
            for index in self.room.bot_seats():
                conn = self.room.seats[index].conn
                if isinstance(conn, bot.BotConn):
                    conn.roll = self.pin_roll

    def unlock_bots(self):
        """把房里每个 bot 的那道 2 秒锁提前解掉。

        ★ 直接写 `0.0` 而不是 `None`：`None` 的语义是「这一局还没上过锁」，
        `_note_action_lock()` 下一帧会补上一把新的。
        ★ 进图那道「连走都不许走」的锁（§94）一起解掉。
        """
        for index in self.room.bot_seats():
            conn = self.room.seats[index].conn
            if isinstance(conn, bot.BotConn):
                conn.act_lock_until = 0.0
                conn.enter_lock_until = 0.0

    def human_heartbeat(self, conn, x, y, jumped=0, on_ground=True,
                        velocity=(0, 0), fast_run=False, ticks=None):
        """让 `conn` 发一发带位置的心跳（走真的 `0x040e` 入口）。

        ★★ 喂完之后把房间的 32 ms 循环推 `HEARTBEAT_TICKS` 格（D106）——
        真人心跳 ~128 ms 一发、收方的物理步长 32 ms，四格正好一发。
        bot 的帧**不再**由这一发驱动（那正是 `rpExplode` 迟到的病根，§147），
        这里推格子只是把真实节奏搬进单测。

        ★ 默认「踩在地上、速度 0」—— 那是真人**走路**时的样子（§35），
        绝大多数用例要的就是它。跳跃的段落显式传 `on_ground=False`。

        ★ `action_lock = False` 时**每一发之前都放一次锁**：换图 / 复活
        都会重新上锁（§74），逐发放开才不用在每个用例里记着这件事。

        ★★ 每发之前先让**后台规划线程**把手上的单子算完（§137）。
        A\\* 从会话 42 起不在游戏线程上跑了，节奏是「这一帧递单、下一帧
        取结果」；这一句把真实节奏原样搬进单测 —— 不是给测试开同步后门。
        """
        botplan.PLANNER.settle()
        if not self.action_lock:
            self.unlock_bots()
        if jumped:
            gameserver.Conn.on_game_packet(conn, OP_PEER_DATA_UP, self.jump(
                conn, jumped))
        gameserver.Conn.on_game_packet(
            conn, OP_PEER_DATA_UP,
            self.beat(conn, x, y, on_ground=on_ground, velocity=velocity,
                      fast_run=fast_run))
        self.advance(gameserver.HEARTBEAT_TICKS if ticks is None
                     else ticks)

    def loop(self):
        """房间那条 32 ms 循环（`gameserver.RoomLoop`）；没有就是 ``None``。"""
        return gameserver.room_loop(self.room, create=False)

    def now(self):
        """**下一格**的绝对时刻 —— bot 眼里的「现在」（D106）。

        ★ 单测里挂钟几乎不走，而模拟时钟一格 32 ms 地往前跑，两者会拉开
        好几秒。要和 bot 的冷却 / 蓄力比时刻，就得用这个，别用挂钟。
        """
        loop = self.loop()
        if loop is None:
            return time.monotonic()
        return roomclock.deadline_of(loop.t0, loop.done)

    def last_beat(self, conn=None):
        """这条连接收到的 bot 的**最后一发心跳**。

        ★ 不能直接拿 `bot_frames(...)[-1]`：心跳每 4 格才一发，而事件包
        （`rpFire` / `rpJump` / `rpCrouch`）在**发生的那一格**就发，
        所以最后一份包经常不是心跳（D106）。
        """
        beats = [f for f in bot_frames(conn or self.alice, self.bot_seat)
                 if udpsync.is_heartbeat(f)]
        self.assertTrue(beats, "一发心跳都没有")
        return beats[-1]

    def change_map(self, name="Stage02"):
        """走完整条换图握手（`0x0411` -> `0x0412` ×N -> `0x0418`）。

        ★ D106 之后换图会**停掉**房间那条 32 ms 循环（真人在加载画面里，
        世界还没建起来），放行 `0x0418` 时才起新的一代 —— 所以用例不能
        只发一发 `0x0411` 就接着喂心跳，那样 bot 一格都不会走。
        """
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REQ_CHANGE_TO_NEXT_MAP,
            gameserver.w_wstr(name))
        for conn in self.room.human_members():
            gameserver.Conn.on_game_packet(
                conn, gameserver.OP_MAP_LOADING_DONE, b"")

    def advance(self, ticks=1):
        """把房间的 32 ms 循环往前推 `ticks` 格，返回真的跑了几格。"""
        loop = self.loop()
        if loop is None:
            return 0
        return loop.advance(ticks)

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

    def expected_full_bars(self):
        """一轮加载里 bot 该报几发 100：立即那一发 + 每个真人各确认一发。

        ★ 确认那几发按**连接**去重（§158）：加载界面是每台客户端各自建的，
          界面建得比第一个人晚、自己又没发过进度包的那一个否则一直是 0%。
        """
        return 1 + len(self.room.human_seats())

    def test_the_bot_reports_a_full_bar_the_moment_loading_starts(self):
        """开局立即 100，每个真人的界面就绪后再各确认一发（关中继回归）。"""
        self.assertEqual([botsync.LOAD_PROGRESS_MAX] * self.expected_full_bars(),
                         load_frames_in(self.start_sent["bob"], self.bot_seat))

    def test_the_full_bar_goes_out_before_the_stage_seven_packet(self):
        """★ 100% 得**赶在**把大家推进 stage 7 的那一发 `0x0402` 前面。

        排在后面的话客户端已经切场景了，那一格白画。
        """
        frames = self.start_sent["bob"]
        hundreds = [i for i, plain in enumerate(frames)
                    if load_frames_in([plain], self.bot_seat) == [100]]
        stage_seven = next(
            i for i, plain in enumerate(frames)
            if len(plain) >= 10 and plain[0] == gameserver.MAGIC_GAME
            and struct.unpack_from("<H", plain, 8)[0]
            == gameserver.OP_COUNT_GAME_READY)
        self.assertEqual(self.expected_full_bars(), len(hundreds))
        self.assertLess(max(hundreds), stage_seven)

    def test_every_bot_in_the_room_gets_its_own_full_bar(self):
        for seat in self.room.bot_seats():
            self.assertEqual(
                [botsync.LOAD_PROGRESS_MAX] * self.expected_full_bars(),
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

    def test_a_late_humans_progress_report_is_ignored(self):
        """stage 7 之后的迟到进度不再补画：这轮加载已经结束。"""
        self.clear()
        seat = self.room.seat_index_of(self.alice)
        gameserver.Conn.on_game_packet(
            self.alice, OP_PEER_DATA_UP,
            botsync.build_peer_packet(seat, botsync.OP_LOAD_PROGRESS,
                                      botsync.load_progress_body(42),
                                      game_id=self.room.epoch_value))
        self.assertEqual([], load_frames(self.bob, self.bot_seat))

    def send_progress(self, conn, value):
        """替某个真人发一发自己的 `0x4005`（加载界面已经建好的硬证据）。"""
        gameserver.Conn.on_game_packet(
            conn, OP_PEER_DATA_UP,
            botsync.build_peer_packet(
                self.room.seat_index_of(conn), botsync.OP_LOAD_PROGRESS,
                botsync.load_progress_body(value),
                game_id=self.room.epoch_value))

    def test_each_humans_first_progress_confirms_once(self):
        """★★ 去重按**连接**走：每个真人的第一发进度各补一个 100。

        整房只补一次的话，加载界面建得比第一个人晚、自己又因为加载太快
        没发过进度包的那一个仍然是 0%（§158）。同一个人的后续 1Hz 进度
        不刷屏 —— 那才是「状态翻转去重」要挡的。
        """
        self.room.battle.host.state = gameserver.StartGameHandshake.PREPARING
        self.bot_conn.load_progress_confirmed = frozenset()
        self.clear()
        for value in (1, 42, 99):
            self.send_progress(self.alice, value)
        self.assertEqual([botsync.LOAD_PROGRESS_MAX],
                         load_frames(self.bob, self.bot_seat),
                         "alice 的第一发补一个 100，后两发不再补")
        self.clear()
        for value in (5, 60):
            self.send_progress(self.bob, value)
        self.assertEqual([botsync.LOAD_PROGRESS_MAX],
                         load_frames(self.bob, self.bot_seat),
                         "bob 自己的界面就绪时也要补得到")
        self.clear()
        self.send_progress(self.alice, 100)
        self.send_progress(self.bob, 100)
        self.assertEqual([], load_frames(self.bob, self.bot_seat),
                         "两个人都补过了就不再补")


class TwoBotFrameRoom(BotFrameRoom):
    """两个 bot ——`/bot` 在游戏中是拒绝的，所以必须开局**前**加好。"""

    def start_battle(self):
        bot.handle_command(self.alice, "/a")
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

    #: ★★ 这一批用例验的是**开枪**，所以默认把近身攻击关掉（§64）。
    #:
    #: 不关的话它们全废：`approach()` 把 bot 放在离人几十个单位的地方，
    #: 而冲刺攻击够得着 70~100 个单位 —— bot 会**先冲再打**（原版真人也是，
    #: 那一下会占住整个角色 25 帧），于是那一帧一发 `rpFire` 都没有。
    #: 近身本身有 `BotDashTests` 单独验。
    melee = False

    def setUp(self):
        super().setUp()
        self.bot_conn.melee = self.melee

    def approach(self, x=100.0, y=100.0, settle=True):
        """把 alice 走到 `(x, y)`，让 bot 跟到它身后并进入射程。

        `settle=True` 时顺手把在飞的子弹**结算掉**（见 `settle()`）——
        绝大多数用例关心的是「打出去的那一发长什么样」，不是它飞了多久。
        """
        self.walk(self.alice, [(x, y), (x + 20, y), (x + 40, y)])
        if settle:
            self.settle()

    def approach_far(self, settle=True):
        """把 bot 钉在原地，再让 alice 走远 —— 拉出一段**真的要飞**的距离。

        ★ 和 `approach()` 的区别只有**距离**：那个只走 40 个单位，bot 落在
        轨迹最老的那点上，离得比一个客户端 tick 还近 —— 那种贴脸射击是
        **当场结算**的（§52），根本没有「在飞」这个状态。要验延后爆炸就得
        真的隔开一段。

        ★★ 为什么要 `holding` 而不是「多走几步」（会话 18 改）：bot 跟随时
        **永远**只落后 `BOT_FOLLOW_DISTANCE`（120）个单位，而枪口还在身前
        43 个单位（`BOT_MUZZLE_FORWARD`，§62）—— 枪口到目标最多 77 个单位，
        初速 100 的枪连一个客户端 tick 都飞不满。跟随状态下根本拉不出
        「在飞」这个状态，只能先让它站住。
        """
        self.walk(self.alice, [(0.0, 100.0), (200.0, 100.0)])
        self.bot_conn.holding = True
        # ★ 站住之前那几步是**贴脸**打的，当场就结算了（§52）——
        #   连同它们的 `rpExplode` 一起丢掉，否则「还没炸」那条断言一开始就红。
        self.settle()
        self.clear()
        # ★ 单测里一帧和一帧只差几微秒，弹匣 / 冷却是按**真实时钟**走的
        #   —— 不把它拨回去的话，站住之后这一整段一发都打不出来。
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(400.0, 100.0), (600.0, 100.0),
                               (800.0, 100.0)])
        if settle:
            self.settle()

    def charge(self):
        """★ 把「手指按下去的那一刻」拨到很久以前 = 蓄力已经满了（§73）。

        单测里一帧和一帧只差几微秒，而蓄力是按**真实流逝的时间**算的
        （蓄满 80 要 1.28 秒）—— 不拨的话蓄力武器一发都打不出来。
        ★ 只对 `PowerControl=2` 的武器有意义，别的武器点击即发。
        """
        for index in self.room.bot_seats():
            conn = self.room.seats[index].conn
            if isinstance(conn, bot.BotConn) and conn.charge_at is not None:
                conn.charge_at -= 3600.0

    def arrive(self):
        """让在飞的子弹**把整条弹道跑完**（把出膛那一格拨到很久以前）。

        ★★ D106 之后弹体按**格子**走（`tick − born_tick`），所以拨的是
        `born_tick`，不是挂钟 —— 拨挂钟一格都不会动。`born` 跟着一起拨，
        碎片 / 火墙的诞生时刻是拿它换算的（`_tick_moment`）。
        """
        for shell in self.bot_conn.pending_shots:
            back = shell.max_ticks + 1
            shell.born_tick -= back
            shell.born -= back / ballistics.TICKS_PER_SECOND

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
        # ★ 夹具是夺分模式（args[1] = 3）：伤害 ×2（§87），再因为
        #   「目标踩在地上」×0.75（§89）—— alice 的心跳默认 on_ground=True。
        #   两个人贴在一起，所以没有「离得远」那一条。
        self.assertAlmostEqual(int(int(weapon.damage) * 2 * 0.75), damage)

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
        self.bot_conn.next_fire_at = 0.0
        self.clear()
        self.advance(1)
        self.assertTrue(fire_frames(self.alice, self.bot_seat),
                        "这一格该打一发")
        # ★ 刚打完那一发，`CoolingTime`（几百毫秒）还没走完 —— 一格才 32 ms。
        self.assertGreater(self.bot_conn.next_fire_at, self.now())
        self.clear()
        self.advance(1)
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
        self.change_map()
        self.assertIsNone(self.bot_conn.declared_weapon)
        self.clear()
        self.approach(x=300.0)
        self.assertTrue(change_weapon_frames(self.alice, self.bot_seat))


class BotGunCommandTests(BotFireRoom):
    """`/w [N] M` —— 房间级自动/锁定武器。"""

    def gun(self, *args):
        """敲一条 `/w`，返回房主看到的那几行系统提示。"""
        self.alice.sent.clear()
        self.assertTrue(bot.handle_command(
            self.alice, ("/w " + " ".join(map(str, args))).strip()))
        return "".join(chat_lines(self.alice))

    def test_listing_shows_the_usable_slots(self):
        """★ 不带参数 = 列出每个 bot 的可用槽（一个参数已经改成「全换」）。"""
        listed = self.gun()
        self.assertIn(f"{self.bot_seat}: ", listed)
        self.assertIn("（当前）", listed)

    def test_one_argument_switches_every_bot(self):
        """★ 用户 2026-08-29 要的：`/w 2` = 所有 bot 一起换成 2 号武器。"""
        slots = sorted(w.raw["slot"] for w in
                       weapondata.usable_for(self.bot_conn.character_id))
        other = [s for s in slots if s != self.bot_conn.weapon.raw["slot"]]
        self.assertTrue(other, "这个角色得有第二个可用槽，用例才有意义")
        self.gun(other[0])
        self.assertEqual(other[0], self.bot_conn.weapon_slot)
        self.assertEqual(other[0], self.room.bot_weapon_slot)
        self.assertEqual(weapondata.slot_for(self.bot_conn.character_id,
                                             other[0]).id,
                         self.bot_conn.weapon.id)

    def test_one_argument_reaches_every_bot_in_the_room(self):
        """两个 bot 都得换 —— 「全换」的整个意义就在这儿。"""
        # ★ `/a` 在战斗中会被 `MUTATING_COMMANDS` 挡掉，直接调底层加一个。
        index, error = bot._add_one_bot(self.alice, self.room)
        self.assertIsNotNone(index, error)
        machines = [self.room.seats[i].conn for i in self.room.bot_seats()]
        self.assertEqual(2, len(machines))
        slot = sorted(w.raw["slot"] for w in
                      weapondata.usable_for(machines[0].character_id))[-1]
        self.gun(slot)
        for machine in machines:
            self.assertEqual(slot, machine.weapon_slot)

    def test_a_bot_added_later_inherits_the_room_weapon_lock(self):
        self.gun(2)
        index, error = bot._add_one_bot(self.alice, self.room)
        self.assertIsNotNone(index, error)
        self.assertEqual(2, self.room.seats[index].conn.weapon_slot)

    def test_w_zero_restores_auto_for_every_bot_and_the_room(self):
        self.gun(2)
        index, error = bot._add_one_bot(self.alice, self.room)
        self.assertIsNotNone(index, error)
        self.gun(0)
        self.assertEqual(0, self.room.bot_weapon_slot)
        for seat_index in self.room.bot_seats():
            machine = self.room.seats[seat_index].conn
            self.assertIsNone(machine.weapon_slot)
            self.assertIsNotNone(machine.weapon)

    def test_a_room_weapon_lock_can_be_set_before_any_bot_joins(self):
        # 把当前 bot 摘掉，验证配置不是“遍历现有 bot 时顺便写”的副作用。
        gameserver.LOBBY.remove_bot(self.room, self.bot_seat)
        self.gun(3)
        self.assertEqual(3, self.room.bot_weapon_slot)
        index, error = bot._add_one_bot(self.alice, self.room)
        self.assertIsNotNone(index, error)
        self.assertEqual(3, self.room.seats[index].conn.weapon_slot)

    def test_one_argument_with_an_unusable_slot_is_refused(self):
        before = self.bot_conn.weapon.id
        self.assertIn("没有能用的 9 号武器槽", self.gun(9))
        self.assertEqual(before, self.bot_conn.weapon.id)

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
        self.gun(slot)
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REQ_CHANGE_TO_NEXT_MAP,
            gameserver.w_wstr("Stage02"))
        self.assertEqual(slot, self.bot_conn.weapon_slot)
        self.assertEqual(slot, self.room.bot_weapon_slot)

    def test_a_locked_slot_never_silently_falls_back(self):
        """锁定槽位在新角色上不可用时宁可不开火，也不能暗中换别的枪。

        角色 3（아이린，玩家选不到）的 3 号槽是 `TotemLauncher`，
        `Damage=0` 打不动人 ⇒ 不在 `usable` 里，正好当这个局面的样本。
        """
        self.bot_conn.weapon_slot = 1
        self.bot_conn.character_id = 3
        self.assertIsNotNone(self.bot_conn.weapon)
        self.bot_conn.weapon_slot = 3       # 角色 3 的 slot3 伤害 0，不可用
        self.assertIsNone(self.bot_conn.weapon)


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
            self.alice, "/w " + " ".join(map(str, args))))
        return "".join(chat_lines(self.alice))

    def test_the_explode_waits_for_the_bullet_to_arrive(self):
        """★ `rpExplode` 不再紧跟着 `rpFire` —— 子弹得先飞到。

        以前那版一出膛就炸，别人屏幕上子弹刚出枪口就爆。
        """
        self.approach_far(settle=False)
        self.assertTrue(fire_frames(self.alice, self.bot_seat))
        self.assertEqual([], explode_frames(self.alice, self.bot_seat))
        self.assertTrue(self.bot_conn.pending_shots)
        self.settle()
        self.assertTrue(explode_frames(self.alice, self.bot_seat))
        self.assertEqual([], self.bot_conn.pending_shots)

    def test_the_flight_time_comes_from_the_weapon(self):
        """飞多久是 `ballistics` 按 `Velocity` 算的**物理量**，
        不是「等 XX 毫秒再说」那种观测阈值（铁律 10 的例外）。"""
        self.approach_far(settle=False)
        shell = self.bot_conn.pending_shots[0]
        # 解出来的弹道说它要飞好几个客户端 tick，而且不到一秒就到。
        self.assertGreater(shell.shot.ticks, 1.0)
        self.assertLess(shell.shot.seconds, 1.0)
        # ★★ D106：弹体走到第几格 = **这一格减出膛那一格**，一格不多一格不少
        #    （收方也是这么数的，§147）。出膛那一格本身**不推**。
        self.assertEqual(min(self.loop().done - 1 - shell.born_tick,
                             shell.max_ticks), shell.ticks)

    def test_a_pending_explosion_still_goes_out_while_the_bot_is_dead(self):
        """★★ **一发都不能漏**：句柄记账在开火那一刻就推进了，少发一发
        收方那一格就永久错位（§42）。真人死了，他打出去的子弹也照样在飞。"""
        self.approach_far(settle=False)
        self.assertTrue(self.bot_conn.pending_shots)
        self.room.quest.arm_respawn_watchdog(self.bot_seat, (10.0, 10.0))
        self.clear()
        self.settle()
        self.assertTrue(explode_frames(self.alice, self.bot_seat),
                        "bot 躺着也得把在飞的子弹炸掉")

    def test_a_map_change_drops_the_bullets_in_flight(self):
        """换图时收方的弹体表和句柄计数器整个复位 —— 补发只会拿一个
        查不到的句柄去撞 `0x492750` 那个静默丢弃。"""
        self.approach_far(settle=False)
        self.assertTrue(self.bot_conn.pending_shots)
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REQ_CHANGE_TO_NEXT_MAP,
            gameserver.w_wstr("Stage02"))
        self.assertEqual([], self.bot_conn.pending_shots)

    def test_the_explosion_lands_on_the_trajectory(self):
        """★★★ 爆炸点必须落在**弹道上**，不是「目标此刻站的地方」（§65）。

        会话 18 之前是后者 —— 于是用户看见「火箭飞了一段打在地上，
        爆炸效果却出现在我身上，我还掉血」（2026-08-27 实机报的）。
        """
        self.approach_far(settle=False)
        shell = self.bot_conn.pending_shots[0]
        self.walk(self.alice, [(900.0, 100.0)])       # 人跑到别处去
        self.settle()
        body = body_of(explode_frames(self.alice, self.bot_seat)[-1])
        x, y = struct.unpack_from("<ff", body, 8)
        # 爆炸点在这条弹道上（取最近的那个 tick 落点比一格还近）。
        best = min(
            (math.hypot(x - px, y - py)
             for px, py in (shell.position(t)
                            for t in range(0, shell.max_ticks + 1))),
            default=None)
        self.assertIsNotNone(best)
        self.assertLess(best, shell.shot.speed,
                        "爆炸点得落在弹道上（误差不到一个 tick 的行程）")

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
        self.assertTrue(fires)
        # ★ 只看**第一发**那三颗：D106 之后一次 `approach()` 是实打实的
        #   384 ms，冷却过得去就会再打一发。
        base = botsync.projectile_handle(self.bot_seat, 0)
        handles = [struct.unpack_from("<i", body_of(f), 0)[0] for f in explodes]
        self.assertEqual([base, base + 1, base + 2], handles[:3])
        self.assertEqual(3, struct.unpack_from("<i", body_of(fires[0]), 22)[0])

    def test_a_splash_weapon_holds_fire_until_its_bullet_exploded(self):
        """★★ 溅射那多出来的一个句柄，到底是开火时分配的还是爆炸时分配的，
        语料分不出来 —— 两种假设只有在「上一发炸完再开下一枪」时才同解。
        所以这类武器有一道顺序闸门（`_may_fire`）。"""
        splash = [w for w in weapondata.usable_for(self.bot_conn.character_id)
                  if w.splash_range]
        self.assertTrue(splash, "这个角色应该有带溅射的武器")
        self.bot_conn.weapon_slot = splash[0].raw["slot"]
        # ★ 必须拉开距离，但**理由和溅射无关**：贴脸那一发是当场结算的
        #   （§52），压根没有「上一发还在飞」这个状态，闸门就无从谈起。
        self.approach_far(settle=False)
        self.assertTrue(self.bot_conn.pending_shots)
        self.clear()
        self.bot_conn.next_fire_at = 0.0      # 冷却过了，但上一发还在飞
        self.walk(self.alice, [(820.0, 100.0), (830.0, 100.0)])
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))

    def test_a_plain_bullet_does_not_wait(self):
        """★ 不带溅射的武器 `handle_step == shots`，分配全在开火那一刻 ——
        怎么交错都对，**不设闸**，否则射速会白白掉一半。"""
        plain = [w for w in weapondata.usable_for(self.bot_conn.character_id)
                 if not w.splash_range]
        self.assertTrue(plain)
        self.bot_conn.weapon_slot = plain[0].raw["slot"]
        self.approach_far(settle=False)
        self.assertTrue(self.bot_conn.pending_shots)
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(800.0, 100.0), (810.0, 100.0)])
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
        """榴弹那一类打一发装一次 —— `fire_interval_ms` 本来就对。

        ★ 会话 19：原来用的是角色 0 的 2 号槽（사과탄），
        §70 收紧之后它不再可用（`AppleGrenade` 会分裂、多吃句柄）。
        换成角色 2 的 3 号槽（바주카，同样没有弹匣）。
        """
        weapon = weapondata.slot_for(2, 3)
        self.assertIsNotNone(weapon)
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


class HumanFireLogTests(BotFireRoom):
    """★ 诊断日志：真人本图第一发 `rpFire` / `rpExplode` 要原样打出来（§53）。

    拿它和 bot 那行 `开火:` 并排比 —— 语料是 15 年前别人的对局，
    **同一局同一张图**里真人自己发的那一发才是真正的对照组。
    """

    def fire_packet(self):
        return botsync.build_peer_packet(
            0, botsync.OP_FIRE,
            botsync.fire_body(0, 1001010, 100.0, 80.0, 0.5, 1.0, shots=3),
            game_id=1, sequence=7)

    def test_the_first_human_shot_is_logged_with_its_fields(self):
        self.alice.human_fire_logged = set()
        lines = []
        self.alice.log = lambda text: lines.append(text)
        self.alice.note_human_fire(self.fire_packet())
        self.assertTrue(lines, "真人第一发 rpFire 得打一行")
        text = lines[0]
        self.assertIn("真人rpFire", text)
        self.assertIn("ammo 1001010", text)
        self.assertIn("count 3", text)

    def test_it_only_logs_the_first_one_per_map(self):
        """按状态翻转去重（铁律 10）—— 逐发打会把日志刷爆。"""
        self.alice.human_fire_logged = set()
        lines = []
        self.alice.log = lambda text: lines.append(text)
        for _ in range(5):
            self.alice.note_human_fire(self.fire_packet())
        self.assertEqual(1, len(lines))

    def test_a_class_level_default_does_not_blow_up(self):
        """★ `BotConn` 不跑 `Conn.__init__`（D1），拿到的是类级 `frozenset`
        —— 照着 `.add()` 会炸到真人那条线程上。"""
        self.alice.human_fire_logged = gameserver.Conn.human_fire_logged
        self.alice.log = lambda text: None
        self.alice.note_human_fire(self.fire_packet())
        self.assertIn(botsync.OP_FIRE, self.alice.human_fire_logged)


class BotGunDeclaresAtOnceTests(BotFireRoom):
    """★ `/w` 敲完**当场**发 `rpChangeWeapon`（用户 2026-08-27 报的）。

    以前 `_declare_weapon()` 只挂在 `_try_fire()` 上 —— 房主敲完命令盯着看，
    bot 手里那把枪半天不动，等它下一次开火才突然跳变。
    """

    def test_the_weapon_model_changes_on_the_command_not_on_the_next_shot(self):
        self.approach()
        slots = sorted(w.raw["slot"] for w in
                       weapondata.usable_for(self.bot_conn.character_id))
        other = [s for s in slots if s != self.bot_conn.weapon.raw["slot"]]
        self.assertTrue(other, "这个角色得有第二个可用槽，用例才有意义")
        self.clear()
        # ★ 把开火挡住，确保发出去的那一发只可能来自命令本身。
        self.bot_conn.next_fire_at = time.monotonic() + 3600.0
        self.assertTrue(bot.handle_command(
            self.alice, f"/w {self.bot_seat} {other[0]}"))
        frames = [f for f in bot_frames(self.alice, self.bot_seat)
                  if header(f)["opcode"] == botsync.OP_CHANGE_WEAPON]
        self.assertTrue(frames, "敲完命令就该把新枪声明出去")
        seat, weapon_id = struct.unpack_from("<Bi", body_of(frames[0]), 0)
        self.assertEqual(self.bot_seat, seat)
        self.assertEqual(self.bot_conn.weapon.id, weapon_id)

    def test_it_says_nothing_in_the_room(self):
        """房间里（没开局）bot 还没有同步流 —— 别在那儿组包。"""
        room = self.room
        self.assertTrue(room.is_playing())
        # 直接验命令层不会因为「房间里」这条路炸掉：把状态拨回准备中。
        room.battle.host.state = gameserver.StartGameHandshake.PREPARING
        self.clear()
        self.assertTrue(bot.handle_command(
            self.alice, f"/w {self.bot_seat}"))


class BotLoadingWindowTests(BotFireRoom):
    """★★★ `0x0400` 到 `0x0402` 之间 bot **一动不许动**（§56）。

    用户 2026-08-27 的实机日志抓到的：`0x0400` 一广播 bot 就被标记成
    「加载完」（D4），而真人**还在读图**、这段时间照发心跳 ⇒ bot 的帧被驱动
    起来了。可 `reset_sync_trails()` 挂在 `IN_GAME` 上（要等 `0x0402`），
    于是 bot 拿着**上一局残留的轨迹和句柄计数器**开枪 —— 日志里开局 37 ms
    就打出一发，弹体句柄还是上一局的 `200062`。收方那边换图时
    `ForceReloadTerrain` 已经把计数器清成 `200002`，两边从此对不上号。
    """

    def test_nothing_goes_out_while_someone_is_still_loading(self):
        self.approach()
        self.clear()
        self.room.battle.host.state = gameserver.StartGameHandshake.PREPARING
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(200.0, 100.0), (220.0, 100.0)])
        self.assertEqual([], bot_frames(self.alice, self.bot_seat),
                         "还没进 stage 7，一发都不许发")

    def test_it_resumes_once_everyone_is_in(self):
        self.approach()
        self.room.battle.host.state = gameserver.StartGameHandshake.PREPARING
        self.walk(self.alice, [(200.0, 100.0)])
        self.clear()
        self.room.battle.host.state = gameserver.StartGameHandshake.IN_GAME
        self.walk(self.alice, [(220.0, 100.0), (240.0, 100.0)])
        self.assertTrue(bot_frames(self.alice, self.bot_seat),
                        "进了 stage 7 就该照常动")

    def test_a_room_without_a_battle_still_ticks(self):
        """控制通道造的假房间没有 `battle` —— 别把那条测试路径挡死。"""
        self.approach()
        self.room.battle = None
        self.clear()
        self.walk(self.alice, [(200.0, 100.0), (220.0, 100.0)])
        self.assertTrue(bot_frames(self.alice, self.bot_seat))


class BotSlowBulletTests(BotFireRoom):
    """`/slow` —— 初速降到 1/10（诊断用，配合 `/noboom` 看轨迹）。"""

    def slow(self, *args):
        self.alice.sent.clear()
        self.assertTrue(bot.handle_command(
            self.alice, "/slow " + " ".join(map(str, args))))
        return "".join(chat_lines(self.alice))

    def test_the_power_field_carries_the_slowdown(self):
        """★ 收方的初速是 `power × Velocity`（`0x4920a7`，`PowerControl=0`）
        —— 所以降速这件事**必须落在包里那一格**上，光改服务端算的
        飞行时间没有任何用。"""
        self.approach()
        self.slow()
        self.assertTrue(self.bot_conn.slow_bullet)
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(160.0, 100.0)])
        frames = fire_frames(self.alice, self.bot_seat)
        self.assertTrue(frames)
        power = struct.unpack_from("<f", body_of(frames[0]), 18)[0]
        self.assertAlmostEqual(bot.BOT_SLOW_FACTOR, power, places=5)

    def test_it_toggles_back_to_full_power(self):
        self.approach()
        self.slow()
        self.slow()
        self.assertFalse(self.bot_conn.slow_bullet)
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(160.0, 100.0)])
        frames = fire_frames(self.alice, self.bot_seat)
        self.assertTrue(frames)
        power = struct.unpack_from("<f", body_of(frames[0]), 18)[0]
        self.assertAlmostEqual(botsync.FIRE_POWER_FIXED, power, places=5)

    def test_the_flight_time_stretches_too(self):
        """飞得慢就该飞得久 —— 不然爆炸会跑到子弹前面去。"""
        weapon = self.bot_conn.weapon
        shot = ballistics.solve(weapon, 300.0, 0.0)
        slow = bot._slow_shot(weapon, shot)
        self.assertAlmostEqual(shot.speed * bot.BOT_SLOW_FACTOR, slow.speed,
                               places=4)
        self.assertGreater(slow.ticks, shot.ticks)


class BotNoBoomCommandTests(BotFireRoom):
    """`/noboom` —— 诊断开关：只发 `rpFire`、不发 `rpExplode`。

    §42：不发爆炸的话弹体**一直飞不消失、一滴血不掉**。用它分清
    「看不见子弹」到底是弹体没造出来，还是爆炸发得太早。
    """

    def noboom(self, *args):
        self.alice.sent.clear()
        self.assertTrue(bot.handle_command(
            self.alice, "/noboom " + " ".join(map(str, args))))
        return "".join(chat_lines(self.alice))

    def test_it_still_fires_but_never_explodes(self):
        self.approach()
        self.noboom()
        self.assertTrue(self.bot_conn.no_explode)
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(160.0, 100.0)])
        self.assertTrue(fire_frames(self.alice, self.bot_seat),
                        "开火照旧 —— 关掉的只有爆炸")
        self.settle()
        self.assertEqual([], explode_frames(self.alice, self.bot_seat),
                         "/noboom 开着的时候一发爆炸都不许出去")

    def test_the_handle_step_drops_the_splash_slot(self):
        """★★★ 用户 2026-08-27 实机踩到的：关掉 `/noboom` 之后**再也打不中**。

        带溅射的武器每发多吃一个句柄，而那一个是**爆炸那一刻**收方创建
        `SplashDamage` 时分配的（§54）。不发爆炸 ⇒ 收方少分配一个 ⇒
        服务端照旧 `+handle_step` 就会永久错开。
        """
        self.approach()
        weapon = self.bot_conn.weapon
        self.noboom()
        before = self.bot_conn.sync.projectiles
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(160.0, 100.0)])
        self.settle()
        self.assertEqual(before + weapon.shots,
                         self.bot_conn.sync.projectiles,
                         "/noboom 开着时只该推进「造出来的弹体」那几个")
        self.assertEqual([], self.bot_conn.pending_shots,
                         "队列也得清干净，不然堆到天上去")

    def test_the_normal_path_still_pays_for_the_splash_slot(self):
        """关掉开关就回到完整步进 —— 别把正常路径也改坏了。"""
        self.approach()
        weapon = self.bot_conn.weapon
        before = self.bot_conn.sync.projectiles
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(160.0, 100.0)])
        self.settle()
        self.assertEqual(before + weapon.handle_step,
                         self.bot_conn.sync.projectiles)

    def test_it_toggles_back(self):
        self.approach()
        self.noboom()
        self.noboom()
        self.assertFalse(self.bot_conn.no_explode)
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(160.0, 100.0)])
        self.settle()
        self.assertTrue(explode_frames(self.alice, self.bot_seat),
                        "关掉之后爆炸得回来")


class BotPointBlankTests(BotFireRoom):
    """★★★ 贴脸射击：`rpExplode` 和 `rpFire` **同一帧**发出去（§52）。

    用户 2026-08-26 报「看不见子弹、凭空掉血」，会话 15 以为根因是收方的
    可靠队列要等心跳才 flush（旧 §50），于是让爆炸多等一发心跳（旧 D37）。
    **那条结论是错的**：收方每帧都 flush，包一入队就把队列上界抬起来了。
    多等的唯一后果是弹体白飞 —— 每 tick 推进一步，等 125 ms 就飞过目标
    390 个单位，爆炸特效在目标身上、弹体在几百单位开外，两头对不上。

    ⇒ 现在的口径回到物理：**飞到了就炸**；飞行时间不足客户端一个 tick 的
    （= 收方连一步都推不完），当场结算，不占用下一帧。
    """

    def test_a_point_blank_shot_explodes_in_the_same_frame(self):
        """★ 贴着打（`approach()` 的距离只有几十个单位）时，一帧之内
        `rpFire` 和 `rpExplode` 都发出去了。"""
        self.approach(settle=False)
        frames = bot_frames(self.alice, self.bot_seat)
        fires = [i for i, f in enumerate(frames)
                 if header(f)["opcode"] == botsync.OP_FIRE]
        self.assertTrue(fires, "贴脸这一帧该开火")
        explodes = [i for i, f in enumerate(frames)
                    if header(f)["opcode"] == botsync.OP_EXPLODE]
        self.assertTrue(explodes, "飞行时间不足一个 tick，爆炸该当场发")
        self.assertEqual([], self.bot_conn.pending_shots,
                         "当场结算完就不该还有在飞的子弹")
        self.assertGreater(explodes[0], fires[0],
                           "爆炸得排在开火后面")

    def test_a_shot_that_really_flies_waits_for_the_flight_time(self):
        """★★ 反过来：真的要飞一段的，开火那一帧**不许**炸。

        这一条钉的是「爆炸时刻 = 子弹**真的撞上**的那一刻」——
        `_advance_shells()` 只认「推到第几个 tick、那一段撞上什么」。
        """
        # ★★ 会话 18：**跟随状态下拉不出这个用例**。bot 永远只落后
        #   `BOT_FOLLOW_DISTANCE`（120），枪口还在身前 `BOT_MUZZLE_FORWARD`
        #   （43，§62）—— 枪口到目标最多 77 个单位，初速 100 的枪连一个客户端
        #   tick 都飞不满，走的是当场结算那条路。所以先让它站住再拉开。
        self.approach_far(settle=False)
        self.assertTrue(fire_frames(self.alice, self.bot_seat), "这一帧该开火")
        self.assertGreater(self.bot_conn.pending_shots[0].shot.ticks, 1.0,
                           "这一发得真的要飞一会儿，用例才有意义")
        self.assertEqual([], explode_frames(self.alice, self.bot_seat),
                         "子弹还在飞，这时候炸 = 爆炸跑到子弹前面去了")
        self.assertTrue(self.bot_conn.pending_shots, "该记在「在飞」队列里")
        self.arrive()
        self.clear()
        self.bot_conn.next_fire_at = time.monotonic() + 3600.0
        self.walk(self.alice, [(620.0, 0.0)])
        self.assertTrue(explode_frames(self.alice, self.bot_seat),
                        "飞到了就该炸")


def splash_frames(conn, seat):
    return [f for f in bot_frames(conn, seat)
            if header(f)["opcode"] == botsync.OP_SPLASH_DAMAGED]


class BotHitDetectionTests(BotFireRoom):
    """★★★ **真的判命中**（§65，会话 19）。

    用户 2026-08-27 实机报的三条，根子都在「服务端根本没判」：

    * 「有的时候我看见自己躲开了，但我身上还会有命中效果，也会掉血」
    * 「火箭可以看见它飞了一段后打在地上了，但是爆炸效果会显示在我身上」
    * 「有没有做真正的命中判定？似乎都是 100% 命中」

    以前 `_impact_point()` 一律把爆炸点搬到「目标此刻站的地方」并报命中 ——
    那就是百发百中。现在 `_advance_shells()` 逐 tick 跑真弹道，
    撞到人 / 撞到地形 / 飞出图外才炸，炸在**真的撞上的那一点**。
    """

    def last_explode(self):
        frames = explode_frames(self.alice, self.bot_seat)
        self.assertTrue(frames, "总得有一发爆炸（句柄记账不许漏）")
        body = body_of(frames[-1])
        handle, target = struct.unpack_from("<ii", body, 0)
        x, y = struct.unpack_from("<ff", body, 8)
        kind, _flags, damage = struct.unpack_from("<iif", body, 16)
        return {"handle": handle, "target": target, "x": x, "y": y,
                "kind": kind, "damage": damage}

    def test_the_fire_packet_carries_the_collision_group(self):
        """★★ 个人战里 bot 的组是**座位 + 1**，不是写死的 1（§63）。"""
        self.approach()
        fires = fire_frames(self.alice, self.bot_seat)
        self.assertTrue(fires)
        for frame in fires:
            self.assertEqual(self.bot_seat + 1, body_of(frame)[1])

    def test_a_shot_at_a_standing_target_hits_it(self):
        self.approach_far()
        shot = self.last_explode()
        self.assertEqual(botsync.character_handle(0), shot["target"])
        self.assertEqual(botsync.HIT_CHARACTER, shot["kind"])
        self.assertGreater(shot["damage"], 0)

    def test_a_target_that_got_out_of_the_way_is_a_miss(self):
        """★★★ 这一条就是「我躲开了就不该掉血」。"""
        self.approach_far(settle=False)
        self.clear()
        self.bot_conn.next_fire_at = time.monotonic() + 3600.0
        self.walk(self.alice, [(800.0, -400.0)])      # 跳到弹道上方老远
        self.settle()
        shot = self.last_explode()
        self.assertEqual(0, shot["target"], "人不在弹道上，目标句柄必须是 0")
        self.assertEqual(botsync.HIT_NONE, shot["kind"])
        self.assertEqual(0.0, shot["damage"])

    def test_a_miss_still_pays_the_handle_bill(self):
        """★★ 打空也**必须**发那一发 `rpExplode` —— 句柄记账在开火那一刻
        就推进了，少发一发收方那一格就永久错位，从此打不掉血（§42）。"""
        self.approach_far(settle=False)
        self.clear()
        self.bot_conn.next_fire_at = time.monotonic() + 3600.0
        self.walk(self.alice, [(800.0, -400.0)])
        self.settle()
        self.assertEqual(1, len(explode_frames(self.alice, self.bot_seat)))
        self.assertEqual([], self.bot_conn.pending_shots)

    def test_the_damage_follows_the_part_that_got_hit(self):
        """★ `weapon.ini` 的三档伤害（`Damage` / `HeadDamage` / `LegsDamage`）
        对应 `ChrProps.ini` 的三个碰撞圆。收方不重算，填多少掉多少（§42）。"""
        weapon = weapondata.get(1002010)
        self.assertNotEqual(weapon.damage, weapon.head_damage)
        self.assertEqual(weapon.head_damage, weapon.damage_for("head"))
        self.assertEqual(weapon.legs_damage, weapon.damage_for("legs"))
        self.assertEqual(weapon.damage, weapon.damage_for("body"))

    def test_a_teammate_is_not_even_collidable(self):
        """★ 组队房里队友和 bot **共用一个组** ⇒ 收方直接跳过碰撞，
        服务端这边也必须跳过，否则「客户端看着穿过去了、服务端说打中了」。"""
        for index, seat in enumerate(self.room.seats):
            if seat is not None:
                seat.team = lobby.TEAM_A
        group = bot._seat_group(self.room, self.bot_seat)
        self.assertEqual(lobby.TEAM_A, group)
        self.assertEqual([], bot._battle_bodies(self.room, self.bot_seat, group))

    def test_terrain_stops_the_bullet(self):
        """★ 撞地形就在**那一点**炸 —— 火箭打在地上，爆炸就该在地上。

        用 `blocks_bullet()` 那一路：单向平台（值 1）挡人不挡子弹（§29）。
        """
        terrain = mapdata.load("Beginner")
        if terrain is None:
            self.skipTest("没有地形产物")
        # 找一列有站立面的地方，从它上方水平打过去打不着东西；
        # 改成朝下打，必然撞地。
        column = None
        for x in range(200, min(terrain.width, 1200), 8):
            surfaces = terrain.surfaces(x)
            if surfaces:
                column = (x, surfaces[0])
                break
        self.assertIsNotNone(column, "这张图总该有一块地面")
        x, ground = column
        t = bot._terrain_stop_t(terrain, x, ground - 200.0, x, ground + 40.0)
        self.assertIsNotNone(t, "朝地面打下去必须被挡住")
        self.assertTrue(0.0 < t <= 1.0)


class BotSplashTests(BotFireRoom):
    """★★ 溅射：`rpExplode` 之外还得补 `rpSplashDamaged`（§67）。

    收方处理 `rpExplode` 时确实会替带 `SplashRange` 的武器建一个
    `SplashDamage` 对象（§54 那个多出来的句柄），但**算伤害的是射手那台
    机器**（`0x47eb4e` 的守卫）—— bot 没有本机，不补这一发，火箭炸在人
    脚边一滴血都不掉。
    """

    def rocket_shell(self, weapon_id=1002030, splash=True):
        weapon = weapondata.get(weapon_id)
        self.assertEqual(splash, bool(weapon.splash_range))
        shot = ballistics.solve(weapon, 400.0, 0.0)
        return bot.Shell(300002, 0, weapon, 3, 0.0, 0.0, shot,
                         time.monotonic(), 100)

    def test_someone_standing_next_to_the_blast_takes_reduced_damage(self):
        shell = self.rocket_shell()
        character = chrprops.get(2)
        # 站在爆点旁边半个溅射半径的地方。
        near = shell.weapon.splash_range * 0.5
        bodies = [(0, near, 0.0, False, 2)]
        point = character.center(0.0, 0.0)
        hits = bot._splash_targets(self.room, shell, point, None, bodies)
        self.assertEqual(1, len(hits))
        seat, damage, _where, _push = hits[0]
        self.assertEqual(0, seat)
        self.assertGreater(damage, 0)
        # ★ 夺分模式（夹具默认 args[1] = 3）整条伤害 ×2（§87）。
        self.assertLess(damage,
                        shell.weapon.splash_damage * bot._damage_scale(self.room),
                        "离中心越远伤害越小（weapon.ini 自己写的）")

    def test_outside_the_radius_nobody_gets_splashed(self):
        shell = self.rocket_shell()
        far = shell.weapon.splash_range * 3.0
        bodies = [(0, far, 0.0, False, 2)]
        self.assertEqual([], bot._splash_targets(
            self.room, shell, (0.0, 0.0), None, bodies))

    def test_the_direct_victim_is_not_double_counted(self):
        shell = self.rocket_shell()
        bodies = [(0, 10.0, 0.0, False, 2)]
        self.assertEqual([], bot._splash_targets(
            self.room, shell, (0.0, 0.0), 0, bodies))

    def test_splash_does_not_care_about_teams(self):
        """★★★ 用户 2026-08-27：「组队战不能**直接**伤害友军没错，
        但是有些武器能溅射，比如手雷，溅射后可以伤害友军。」

        出处（§69）：收方给溅射对象设碰撞组的那一句（`0x48254a`）外面套着
        `cmp byte [weapondef+0x54], 0` —— 那一格是 **`SplashTeam`**，
        而 **228 个武器节里一个都没填** ⇒ 溅射对象的组恒为 0 = 撞所有人。
        """
        self.approach()                       # 先让大家有个位置
        for index, seat in enumerate(self.room.seats):
            if seat is not None:
                seat.team = lobby.TEAM_A          # 全员同队
        group = bot._seat_group(self.room, self.bot_seat)
        # 弹体：队友一个都撞不着。
        self.assertEqual([], bot._battle_bodies(self.room, self.bot_seat, group))
        # 溅射：谁都算，连自己。
        everyone = bot._battle_bodies(self.room, self.bot_seat, include_self=True)
        seats = sorted(b[0] for b in everyone)
        self.assertIn(0, seats, "队友（alice）必须在溅射名单里")
        self.assertIn(self.bot_seat, seats, "射手自己也在（语料 1513 发自伤）")

    def test_the_bot_still_fires_a_splash_weapon_point_blank(self):
        """★★ **回归钉子（D50）：贴脸开溅射武器是允许的，别再加禁令。**

        会话 19 加过一条 `_in_own_blast()`「不往自己的爆炸半径里开炮」，
        用户 2026-08-27 否掉了：真人对局里两个人贴近了互相开枪、把自己
        一起炸死是**常态** —— 真人是自己权衡风险收益，不是守着一条
        「近了不许开」的禁令。这种权衡归以后真正的 AI，现在不替它拍板。
        """
        splash = [w for w in weapondata.usable_for(self.bot_conn.character_id)
                  if w.splash_range]
        self.assertTrue(splash, "这个角色应该有带溅射的武器")
        self.bot_conn.weapon_slot = splash[0].raw["slot"]
        self.clear()
        self.approach(settle=False)           # 贴脸（40 个单位）
        self.assertTrue(fire_frames(self.alice, self.bot_seat),
                        "贴脸也要开枪 —— 自伤是真人也会踩的坑，不是禁令")

    def test_a_weapon_without_splash_sends_nothing(self):
        shell = self.rocket_shell(1002010, splash=False)   # 没有 SplashRange
        bodies = [(0, 5.0, 0.0, False, 2)]
        self.assertEqual([], bot._splash_targets(
            self.room, shell, (0.0, 0.0), None, bodies))


class BotLobTests(BotFireRoom):
    """★★ 抛物线武器**不再拉满力气**（§66）。

    用户 2026-08-27：「2 号武器手榴弹扔出去的速度太快了，实际真人对战的
    时候手榴弹飞得是很慢的。」根因是 `_fire_target()` 一律用
    `max_speed()` + 低抛解 —— 手雷成了贴着地平线的直球。
    """

    def test_a_grenade_is_thrown_with_just_enough_force(self):
        weapon = weapondata.get(1002020)         # ch02-02：PowerControl=1
        self.assertTrue(weapon.gravity, "这把该是抛物线武器")
        speed = bot._lob_speed(weapon, 400.0, 0.0)
        self.assertLess(speed, ballistics.max_speed(weapon),
                        "中近距离不该把力气拉满")
        lob = ballistics.solve(weapon, 400.0, 0.0, speed=speed)
        flat = ballistics.solve(weapon, 400.0, 0.0)
        self.assertIsNotNone(lob)
        self.assertGreater(lob.seconds, flat.seconds,
                           "抛出来的该飞得更久（看得见弧线）")
        self.assertGreater(abs(lob.angle), abs(flat.angle), "仰角该更高")

    def test_at_the_edge_of_the_range_it_does_pull_full_power(self):
        """★ 「刚好够得着」这个口径会**自然地**在射程边缘拉满力气。"""
        weapon = weapondata.get(1002020)
        top = ballistics.max_speed(weapon)
        far = top * top / ballistics.gravity_per_tick(weapon)   # 约等于最大射程
        self.assertAlmostEqual(top, bot._lob_speed(weapon, far, 0.0), places=3)

    def test_a_straight_weapon_is_untouched(self):
        """直射武器没有蓄力这回事（`power` 恒 1.0）—— 别顺手改坏了。"""
        for weapon_id in (1002010, 1002030):
            weapon = weapondata.get(weapon_id)
            self.assertFalse(weapon.gravity)
            self.assertEqual(ballistics.max_speed(weapon),
                             bot._lob_speed(weapon, 400.0, 0.0))


class BotFuseTests(BotFireRoom):
    """★★ **引信**（§72）—— 分裂类弹体到点会在每一台机器上自爆。

    用户 2026-08-27：「用 gun 命令，很多角色都无法切换 2 号武器」。
    §70 那一版把 `CreatingClass != GeneralBullet` 全剔了，10/16 个角色的
    2 号槽因此消失。§72 把口径改对之后它们回来了，代价是要照顾引信：
    `AppleGrenade` / `SeedBomb` / `SliceBullet` 的弹体在第 `fuse_ticks`
    个 tick 上自爆（**不带伤害**），之后再发 `rpExplode` 会被收方按句柄
    查不到而整包丢掉（§42 第 4 条）。
    """

    def test_the_fuse_comes_from_the_original_slice_time(self):
        weapon = weapondata.get(1000020)          # ch00-02，SliceTime=1500
        self.assertEqual("AppleGrenade", weapon.raw["creating_class"])
        self.assertEqual(1500 // 32, weapon.fuse_ticks)

    def test_a_shot_that_arrives_in_time_is_allowed(self):
        weapon = weapondata.get(1000020)
        shot = ballistics.solve(weapon, 400.0, 0.0,
                                speed=bot._lob_speed(weapon, 400.0, 0.0))
        self.assertLess(shot.ticks, weapon.fuse_ticks - 1)
        self.assertFalse(bot._outlives_fuse(weapon, shot))

    def test_a_shot_that_would_blow_up_on_the_way_is_refused(self):
        """★ 「够不着」和「弹道解不出来」是同一类事实，不是新规则。"""
        weapon = weapondata.get(1110030)          # ch110-03，SliceTime=500
        shot = ballistics.solve(weapon, 800.0, 0.0,
                                speed=bot._lob_speed(weapon, 800.0, 0.0))
        self.assertGreaterEqual(shot.ticks, weapon.fuse_ticks - 1)
        self.assertTrue(bot._outlives_fuse(weapon, shot))


class BotSliceTests(BotFireRoom):
    """★★ **苹果雷炸开的那几片碎片**（§81）。

    用户 2026-08-28：「1 号角色的 2 号武器苹果弹，真人玩的时候能看见敌人
    扔出去后炸裂开的几个碎片，现在看不到 bot 的炸裂碎片。」

    根子和火墙、溅射是同一个（§72）：造碎片那一段套在 `IsMine` 门里
    （`0x47c96e`），bot 的弹体在任何一台上都不是「自己的」⇒ 一片都不会生。
    """

    def apple(self):
        """把 bot 换成角色 0 的 2 号槽（사과탄，`CreatingClass=AppleGrenade`）。"""
        self.room.seats[self.bot_seat].character_id = 0
        self.bot_conn.character_id = 0
        self.bot_conn.weapon_slot = 2
        self.bot_conn.declared_weapon = None
        weapon = self.bot_conn.weapon
        self.assertEqual(1000020, weapon.id)
        return weapon

    def test_the_fan_matches_the_original_formula(self):
        """★★★ 四片的角度 = `Base + Angle × i / (n−1) + rand % R − R/2`。

        三个 `SliceAngle*` 苹果雷一个都没写 ⇒ 走解析器里的缺省值
        **160 / 30 / 30**（`0x48984d` / `0x489887` / `0x4898c5` 那三条
        `mov ebx`）。语料 1992 组碎片量到的四档取值范围
        `[15,44] [68,97] [121,150] [175,204]` 和这个公式一位不差。
        """
        weapon = weapondata.get(1000020)
        slice_weapon = weapondata.get(1000500)
        self.assertEqual(4, weapon.slice_count)
        self.assertEqual((160, 30, 30), (slice_weapon.slice_angle,
                                         slice_weapon.slice_angle_base,
                                         slice_weapon.slice_angle_random))
        lows = [round(math.degrees(-a))
                for a in bot._slice_angles(weapon, slice_weapon, lambda n: 0)]
        highs = [round(math.degrees(-a))
                 for a in bot._slice_angles(weapon, slice_weapon,
                                            lambda n: n - 1)]
        self.assertEqual([15, 68, 121, 175], lows)
        self.assertEqual([44, 97, 150, 204], highs)

    def test_the_bot_fires_four_fragments_when_it_misses(self):
        """★ 碎片是 `SliceCount` 发**真的** `rpFire`，句柄跟着走。"""
        self.apple()
        self.bot_conn.roll = lambda n: 0
        self.bot_conn.holding = True
        # ★★ D106：一发真人心跳是实打实的 128 ms（4 格）—— 不把枪口封住的话，
        #    母弹在**扔出去的那一发心跳里**就飞完、炸开了，碎片的 `rpFire`
        #    会被后面那句 `clear()` 一起吃掉。整段封死，只放开一格。
        self.bot_conn.next_fire_at = time.monotonic() + 3600.0
        self.walk(self.alice, [(0.0, 100.0), (600.0, 100.0)])
        self.walk(self.alice, [(600.0, 100.0)])        # 手指按下去，开始蓄力
        self.charge()
        self.bot_conn.next_fire_at = 0.0
        self.advance(1)                               # 松手，就这一格
        self.bot_conn.next_fire_at = self.now() + 3600.0
        shots = list(self.bot_conn.pending_shots)
        self.assertTrue(shots, "该有一颗苹果雷在飞")
        before = self.bot_conn.sync.projectiles
        for shell in self.bot_conn.pending_shots:
            shell.max_ticks = min(shell.max_ticks, 20)
        self.clear()
        self.walk(self.alice, [(3000.0, 100.0)])       # 躲开 —— 别被砸中
        self.settle()
        frags = [f for f in fire_frames(self.alice, self.bot_seat)
                 if struct.unpack_from("<i", body_of(f), 2)[0] == 1000500]
        self.assertEqual(4, len(frags))
        # ★★★ 母弹的爆炸对象 1 个 + 四片各 1 个（§86）——
        #   四片多出来的那一个是**各自爆炸时**才分配的，不在这儿。
        self.assertEqual(before + 1 + 4, self.bot_conn.sync.projectiles)

    def test_the_fragments_hit_everyone_and_come_from_the_shooter(self):
        """★ 碰撞组恒 255（`0x47ca0f: or eax, 0xffffffff`），owner 是射手。

        语料 7968 发碎片 `rpFire` 的 `(owner, 组, 颗数)` 一个例外都没有。
        """
        self.apple()
        self.bot_conn.roll = lambda n: 0
        weapon = self.bot_conn.weapon
        shell = bot.Shell(1, 0, weapon, 3, 0.0, 0.0,
                          ballistics.launch(weapon, 0.0, 15.0), 0.0, 10)
        self.clear()
        bot._split_shell(self.room, self.bot_conn, shell, (100.0, 50.0), None, 0)
        frags = fire_frames(self.alice, self.bot_seat)
        self.assertEqual(4, len(frags))
        for frame in frags:
            body = body_of(frame)
            self.assertEqual(botsync.FIRE_SOURCE_PLAYER_BASE + self.bot_seat,
                             body[0])
            self.assertEqual(botsync.FIRE_GROUP_EVERYONE, body[1])
            self.assertEqual(1000500, struct.unpack_from("<i", body, 2)[0])
            # 出生点就是母弹的爆点（语料 7948 组偏移恒 (0, 0)）。
            self.assertEqual((100.0, 50.0),
                             struct.unpack_from("<ff", body, 6))
            # 力度 = 碎片那一节的 `Velocity`（语料恒 10.0）。
            self.assertAlmostEqual(10.0,
                                   struct.unpack_from("<f", body, 18)[0],
                                   places=3)
            self.assertEqual(1, struct.unpack_from("<i", body, 22)[0])

    def test_a_direct_hit_never_splits(self):
        """★★ 砸中角色的那一发弹体当场就没了，**不分裂**（§81）。

        语料：「没打中角色」的 1837 发全部跟着 4 片碎片（飞了 12 发心跳
        ≈ `SliceTime`），「打中角色」的 401 发一片都没有。
        """
        self.apple()
        weapon = self.bot_conn.weapon
        shell = bot.Shell(1, 0, weapon, 3, 0.0, 0.0,
                          ballistics.launch(weapon, 0.0, 15.0), 0.0, 10)
        self.clear()
        bot._split_shell(self.room, self.bot_conn, shell, (100.0, 50.0), 0, 0)
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))

    def test_the_fragments_survive_the_frame_that_created_them(self):
        """★★★ 碎片是在 `_resolve_shell()` **里面**挂进队列的 ——
        `_advance_shells()` 收尾时不能把它们连同旧队列一起冲掉。

        冲掉的后果是这 4 片一发 `rpExplode` 都不发，句柄从此永久错开、
        之后每一发都被静默丢弃（§42）：**子弹照飞、一滴血不掉**。
        """
        self.apple()
        self.bot_conn.roll = lambda n: 0
        self.walk(self.alice, [(600.0, 100.0)])
        weapon = self.bot_conn.weapon
        # ★ `fire_seq` 要比当前事件序号小 —— 否则 `_advance_shells()` 开头
        #   那道「上一代的记录」保险会先把它丢掉。
        shell = bot.Shell(1, self.bot_conn.sync.events - 1, weapon, 3,
                          0.0, 0.0, ballistics.launch(weapon, 0.0, 15.0),
                          time.monotonic() - 3600.0, 3)
        self.bot_conn.pending_shots = [shell]
        self.clear()
        bot._advance_shells(self.room, self.bot_conn, time.monotonic())
        self.assertEqual(4, len(self.bot_conn.pending_shots),
                         "碎片被 _advance_shells 的收尾吞掉了")
        for piece in self.bot_conn.pending_shots:
            self.assertEqual(1000500, piece.weapon.id)

    def test_group_255_shells_can_hit_the_shooter_itself(self):
        """★ 碎片的组是 255 = **撞所有人**，「所有人」里含射手自己
        （和溅射、火墙同一个口径，§69 / D50 —— 自伤照结算）。"""
        self.walk(self.alice, [(600.0, 100.0)])
        seats = [s for s, *_ in bot._battle_bodies(
            self.room, self.bot_seat, botsync.FIRE_GROUP_EVERYONE,
            include_self=True)]
        self.assertIn(self.bot_seat, seats)
        seats = [s for s, *_ in bot._battle_bodies(
            self.room, self.bot_seat, botsync.FIRE_GROUP_EVERYONE)]
        self.assertNotIn(self.bot_seat, seats)

    def test_a_weapon_without_a_slice_count_never_splits(self):
        """★ 火焰弹也有 `SliceId`，但它没有 `SliceCount` —— 那条走火墙。"""
        self.assertIsNone(bot._slice_weapon_of(weapondata.get(1001020)))
        self.assertIsNone(bot._slice_weapon_of(weapondata.get(1000010)))
        self.assertIsNotNone(bot._slice_weapon_of(weapondata.get(1000020)))

    def test_every_fragment_gets_its_own_explosion(self):
        """★★★ 每一片都是收方一个真的弹体 —— 少发一发 `rpExplode`，
        句柄记账就永久错开，从此「子弹照飞、一滴血不掉」（§42）。"""
        self.apple()
        self.bot_conn.roll = lambda n: 0
        self.walk(self.alice, [(600.0, 100.0)])
        weapon = self.bot_conn.weapon
        shell = bot.Shell(1, 0, weapon, 3, 0.0, 0.0,
                          ballistics.launch(weapon, 0.0, 15.0), 0.0, 10)
        self.bot_conn.pending_shots = []
        bot._split_shell(self.room, self.bot_conn, shell, (100.0, 50.0), None, 0)
        self.assertEqual(4, len(self.bot_conn.pending_shots))
        self.clear()
        for piece in self.bot_conn.pending_shots:
            piece.max_ticks = min(piece.max_ticks, 12)
        self.settle()
        self.assertEqual(4, len(explode_frames(self.alice, self.bot_seat)))
        self.assertEqual([], self.bot_conn.pending_shots)

    def test_the_shell_queue_never_outlives_the_fuse(self):
        weapon = weapondata.get(1110030)          # ch110-03，SliceTime=500
        shot = ballistics.solve(weapon, 300.0, 0.0)
        self.assertEqual(weapon.fuse_ticks - 1,
                         bot._shell_max_ticks(None, shot, weapon))

    def test_a_weapon_without_a_fuse_is_untouched(self):
        weapon = weapondata.get(1002010)          # ch02-01，普通直射枪
        shot = ballistics.solve(weapon, 300.0, 0.0)
        self.assertIsNone(weapon.fuse_ticks)
        self.assertFalse(bot._outlives_fuse(weapon, shot))
        self.assertEqual(bot._shell_max_ticks(None, shot),
                         bot._shell_max_ticks(None, shot, weapon))

    def test_a_grenade_character_can_still_shoot(self):
        """★ 别修过头：角色 0 换到 2 号槽照样打得出 `rpFire`（蓄够之后）。"""
        seat = self.room.seats[self.bot_seat]
        seat.character_id = 0
        self.bot_conn.character_id = 0
        self.bot_conn.weapon_slot = 2
        self.bot_conn.declared_weapon = None
        self.assertEqual(1000020, self.bot_conn.weapon.id)
        self.approach()
        self.bot_conn.charge_at = None      # 手指重新按下去（同上）
        self.clear()
        self.advance(1)
        self.assertEqual([], fire_frames(self.alice, self.bot_seat),
                         "手指才刚按下去，一发都不该有（§73）")
        self.charge()
        self.bot_conn.next_fire_at = 0.0     # 上一发的冷却不算数
        self.approach(x=120.0)
        self.assertTrue(fire_frames(self.alice, self.bot_seat))


def fire_wall_frames(conn, seat):
    return [f for f in bot_frames(conn, seat)
            if header(f)["opcode"] == botsync.OP_SET_ON_FIRE]


class BotFireWallTests(BotFireRoom):
    """★★ **地面燃烧**（`rpSetOnFire`，§75）—— 火焰弹炸在地上留下的火墙。

    用户 2026-08-27：「2 号角色，2 号武器扔在地上是会持续燃烧一会儿的，
    现在 bot 的没有燃烧。」

    和 `rpExplode` 同一个根子（§42 / §72）：原版这一发是**射手那台机器**
    在 `IsMine` 门里发的，bot 没有本机 ⇒ 没有一台会替它铺火。
    """

    def flame_thrower(self):
        """把 bot 换成角色 1 的 2 号槽（화염탄，`CreatingClass=FlamingBottle`）。"""
        self.room.seats[self.bot_seat].character_id = 1
        self.bot_conn.character_id = 1
        self.bot_conn.weapon_slot = 2
        self.bot_conn.declared_weapon = None
        weapon = self.bot_conn.weapon
        self.assertEqual(1001020, weapon.id)
        return weapon

    def dodge(self, x=3000.0, y=100.0):
        """把 alice 挪出弹道，让那一发**什么都打不中**。

        ★ 火墙只有「没打中角色」的那一发才铺（§79）—— 砸中人的那一发在
        原版里根本走不到铺火那一段（`0x4829d7` 的 `cmp [esp+8], 0`）。
        """
        self.walk(self.alice, [(x, y)])

    def throw(self):
        """站远了蓄满扔一颗 -> **躲开** -> 结算（火墙是**爆炸那一刻**才铺的）。

        ★ 必须扔远的：贴脸那一发在收方的第一个 tick 里就砸在人身上，
        当场结算（§52），来不及躲 —— 而砸中人的那一发**不铺火墙**（§79）。
        """
        self.bot_conn.holding = True
        # ★★ D106：一发真人心跳是实打实的 128 ms，蓄力说不定走到一半就够了
        #    —— 整段先把枪口封死，只在下面那**一格**上放开，`throw()` 才是
        #    「恰好扔一颗」。不封的话火墙那本句柄账当场翻倍。
        self.bot_conn.next_fire_at = time.monotonic() + 3600.0
        self.walk(self.alice, [(0.0, 100.0), (600.0, 100.0)])
        self.clear()
        self.walk(self.alice, [(600.0, 100.0)])      # 手指按下去，开始蓄力
        self.charge()
        self.bot_conn.next_fire_at = 0.0
        self.advance(1)                             # 松手，就这一格
        self.bot_conn.next_fire_at = self.now() + 3600.0
        self.dodge()
        # ★ 这个房间没有地形数据（`_current_map` 解不出图），弹体永远落不了地
        #   —— 手动把上界收到 20 个 tick，等价于「这一刻撞到地面了」，
        #   火墙就铺在弹道上那一点。不收的话它会一路掉到 y 十几万去。
        for shell in self.bot_conn.pending_shots:
            shell.max_ticks = min(shell.max_ticks, 20)
        self.settle()

    def test_a_flame_bottle_sets_the_ground_on_fire(self):
        self.flame_thrower()
        self.throw()
        frames = fire_wall_frames(self.alice, self.bot_seat)
        self.assertTrue(frames, "火焰弹炸完该补一发 rpSetOnFire")

    def test_the_packet_says_the_flame_ammo_and_hits_everyone(self):
        self.flame_thrower()
        self.throw()
        body = body_of(fire_wall_frames(self.alice, self.bot_seat)[0])
        self.assertEqual(14, len(body))
        self.assertEqual(botsync.FIRE_SOURCE_PLAYER_BASE + self.bot_seat,
                         body[0])
        # ★ `SplashTeam` 一个武器都没填 ⇒ 组恒为 255 = 撞所有人（§69）。
        self.assertEqual(botsync.FIRE_GROUP_EVERYONE, body[1])
        self.assertEqual(1001500, struct.unpack_from("<i", body, 10)[0])

    def test_the_fire_wall_eats_two_n_plus_one_handles(self):
        """★★★ `2 × SpawnCount + 1`（`0x4924a9`）—— 数错了之后每一发
        `rpExplode` 都被静默丢弃（§42 / §75）。

        `ch01-02a` 的 `SpawnCount` 是 4 ⇒ 9，正是 §70 量到的残差主峰。
        """
        self.assertEqual(9, botsync.fire_wall_handles(4))
        self.assertEqual(17, botsync.fire_wall_handles(8))
        self.flame_thrower()
        before = self.bot_conn.sync.projectiles
        self.throw()
        step = self.bot_conn.weapon.handle_step
        self.assertEqual(before + step + 9,
                         self.bot_conn.sync.projectiles)

    def test_the_next_shot_uses_the_handle_after_the_fire_wall(self):
        """★ 下一颗手雷的句柄要把火墙那 9 个算进去。"""
        self.flame_thrower()
        self.throw()
        want = botsync.projectile_handle(self.bot_seat,
                                         self.bot_conn.sync.projectiles)
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.approach(x=140.0)
        self.charge()
        self.approach(x=160.0)
        self.settle()
        self.assertTrue(fire_frames(self.alice, self.bot_seat))
        booms = explode_frames(self.alice, self.bot_seat)
        self.assertTrue(booms)
        self.assertEqual(want, struct.unpack_from("<i", body_of(booms[0]), 0)[0])

    def test_a_plain_weapon_never_sets_anything_on_fire(self):
        self.approach()
        self.settle()
        self.assertEqual([], fire_wall_frames(self.alice, self.bot_seat))

    def test_standing_in_the_fire_hurts(self):
        """★★ 用户 2026-08-28：「现在有火焰了，但是站在火焰上没有伤害。」

        算伤害的还是「射手那台机器」（§42 的守卫），bot 没有本机 ⇒
        不补 `rpSplashDamaged` 就一滴血都不掉（§78）。
        """
        self.flame_thrower()
        self.throw()
        self.assertTrue(self.bot_conn.fires, "该留下一道还在烧的火")
        wall = self.bot_conn.fires[0]
        # 把 alice 摆到第一团火上，再让时间往前走。
        spot = wall.spots[0]
        self.clear()
        # ★ 快进：几道墙现在共用一条**绝对** tick 轴（§85），所以往回拨的是
        #   `born_tick`，不是 `born`。拨 20 个 tick = 这一帧要补烧 20 个 tick。
        wall.born_tick -= bot.BOT_FIRE_REBURN_TICKS
        self.walk(self.alice, [(spot[0], spot[1])])
        burns = [f for f in bot_frames(self.alice, self.bot_seat)
                 if header(f)["opcode"] == botsync.OP_SPLASH_DAMAGED]
        self.assertTrue(burns, "站在火上该掉血")
        body = body_of(burns[0])
        self.assertEqual(botsync.character_handle(
            self.room.seat_index_of(self.alice)),
            struct.unpack_from("<i", body, 4)[0])
        self.assertAlmostEqual(
            weapondata.get(1001500).damage * bot._damage_scale(self.room),
            struct.unpack_from("<f", body, 8)[0], places=3)

    def test_the_same_person_is_not_burnt_every_tick(self):
        """★ 两次挨烧之间至少隔 `BOT_FIRE_REBURN_TICKS` 个 tick（§78 语料量的）。

        一道火墙活 `SpawnLifeTime + SpawnCount × SpawnInterval` = 76 个 tick，
        除以 20 ⇒ 最多烧 **4 次** —— 和语料实测的上限一样。
        """
        flame = weapondata.get(1001500)
        self.assertEqual(76, bot._fire_wall_ticks(flame))
        alice_seat = self.room.seat_index_of(self.alice)
        want = botsync.character_handle(alice_seat)
        # ★ 现造一道火墙摆在 alice 脚下，起点干净（不掺 `throw()` 那几道）。
        self.walk(self.alice, [(400, 100)])
        spot = self.alice.sync_trail[-1][:2]
        life = bot._fire_wall_ticks(flame)
        wall = bot.FireWall(
            botsync.projectile_handle(self.bot_seat, 0), flame,
            [bot.Flame(botsync.projectile_handle(self.bot_seat, 0),
                       float(spot[0]), float(spot[1]), 0, life)],
            time.monotonic(), life)
        # ★ 快进整条命：绝对 tick 轴上把出生时刻往回拨 `life + 4` 个 tick，
        #   这一帧就得把 1..life 全补完（§85）。
        wall.born_tick -= life + 4
        self.bot_conn.fires = [wall]
        self.clear()
        self.walk(self.alice, [tuple(spot)])
        burns = [f for f in bot_frames(self.alice, self.bot_seat)
                 if header(f)["opcode"] == botsync.OP_SPLASH_DAMAGED
                 and struct.unpack_from("<i", body_of(f), 4)[0] == want]
        self.assertEqual(4, len(burns))

    def test_the_fire_burns_teammates_and_the_bot_itself(self):
        """★ 火墙的碰撞组是 255 = 撞所有人（§69）—— 不按碰撞组过滤。"""
        self.flame_thrower()
        self.throw()
        wall = self.bot_conn.fires[0]
        root = wall.flames[0]
        self.assertIsNotNone(
            bot._fire_touch(chrprops.get(1), root.x, root.y, False,
                            wall.flames, wall.flame.size, 0))

    # -- ★★ 会话 24 改的三条（§79）-----------------------------------------
    def test_a_direct_hit_never_sets_the_ground_on_fire(self):
        """★★★ 直接砸中人的那一发**不铺火墙**（§79）。

        原版铺火那一段前面有一道 `cmp dword [esp+8], 0 ; jne 出口`
        （`0x4829d7`），`[esp+8]` 就是撞上的那个角色 —— 非空就整段跳过。
        语料 1079 发 `rpSetOnFire` 无一例外都跟在「什么都没打中」的爆炸后面。

        用户 2026-08-28：「我自己扔出去后如果直接命中别人……之后就不会再有
        持续伤害了；而 bot 扔出去的手雷，即便是直接命中我，我身上的火焰
        也会持续造成伤害。」
        """
        self.flame_thrower()
        self.approach()
        self.charge()
        self.approach(x=120.0)          # ★ 不躲 —— 这一发砸在 alice 身上
        booms = explode_frames(self.alice, self.bot_seat)
        self.assertTrue(booms)
        self.assertTrue(any(struct.unpack_from("<i", body_of(f), 4)[0]
                            for f in booms), "这一轮该有一发打中了人")
        self.assertEqual([], fire_wall_frames(self.alice, self.bot_seat))
        self.assertEqual([], self.bot_conn.fires)

    def test_the_wall_is_two_n_plus_one_flames_spreading_outwards(self):
        """★★ 一道墙是 `2n+1` 团火，从爆点往两边一格一格铺开（§79）。

        收方 `OnSetOnFire`（`0x492471`）只造**根火**那一团；之后每一团在
        自己出生 `SpawnInterval` 个 tick 后往外再生一团，根火左右各生一路
        （`Flame::Tick` `0x482696` + `0x4827de` 的 −1/+1 循环）。
        ⇒ 团数正好等于 §75 那个句柄数 `2 × SpawnCount + 1`。
        """
        flame = weapondata.get(1001500)          # SpawnCount=4 Distance=30
        flames = bot._fire_wall_flames(None, flame, (500.0, 300.0), 200000)
        self.assertEqual(botsync.fire_wall_handles(4), len(flames))
        # 根火在爆点上（y 减了 1：`0x492486` 的 `fsub [0x693720]`）。
        self.assertEqual((500.0, 299.0), (flames[0].x, flames[0].y))
        self.assertEqual(0, flames[0].born)
        # 左右各 4 格，一格 30 —— 整道墙 ±120。
        self.assertEqual(sorted(f.x for f in flames),
                         [380.0, 410.0, 440.0, 470.0, 500.0,
                          530.0, 560.0, 590.0, 620.0])
        # 越往外出生越晚：第 k 格在第 `k × SpawnInterval` 个 tick 上。
        self.assertEqual({0: 0, 30.0: 4, 60.0: 8, 90.0: 12, 120.0: 16},
                         {abs(f.x - 500.0): f.born for f in flames})
        # 句柄按收方的创建顺序：根、左1、右1、左2、右2……
        self.assertEqual(list(range(200000, 200009)),
                         [f.handle for f in flames])

    def test_a_flame_only_burns_while_it_is_alive(self):
        """★ 还没生出来 / 已经烧完的那一团不算数（§79）。"""
        flame = weapondata.get(1001500)
        flames = bot._fire_wall_flames(None, flame, (0.0, 0.0), 200000)
        far = max(flames, key=lambda f: abs(f.x))
        character = chrprops.get(1)
        # 那一团在第 16 个 tick 才生出来，活到第 76 个。
        self.assertIsNone(bot._fire_touch(character, far.x, far.y, False,
                                          flames, flame.size, 0))
        self.assertIsNotNone(bot._fire_touch(character, far.x, far.y, False,
                                             flames, flame.size, far.born))
        self.assertIsNone(bot._fire_touch(character, far.x, far.y, False,
                                          flames, flame.size, far.dies))


class BotActionLockTests(BotFireRoom):
    """★★ **进图 / 复活之后那 2 秒只能跑，不能动手**（§74）。

    用户 2026-08-27 报的两条「抢跑」：

    * 「正常进游戏后画面会显示『预备』『开始』，这两个词显示完之后真人才
      可以行动，现在 bot 在显示这两个词的时候就开枪了」；
    * 「真人被打死复活后有大概两三秒不能开枪，只能移动。但是 bot 在复活后
      的一瞬间就开枪了」。

    根子是同一个：原版 `Character::Respawn`（`0x502fca`）给刚放进图里的角色
    挂了一个 **2000 ms** 的状态 0（`0x5030a0`），而开火输入（`0x516471`）和
    近身输入（`0x515acc`）进门第一件事就是查它。
    """

    action_lock = True                 # ★ 这一批要的就是那道锁

    def test_the_first_frame_arms_the_lock(self):
        self.walk(self.alice, [(100, 100)])
        left = self.bot_conn.act_lock_until - time.monotonic()
        self.assertGreater(left, bot.BOT_ACTION_LOCK_S - 0.5)
        self.assertLessEqual(left, bot.BOT_ACTION_LOCK_S)

    def test_it_holds_fire_while_the_lock_is_on(self):
        self.approach()
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))
        self.assertEqual([], dash_frames(self.alice, self.bot_seat))

    def test_it_keeps_sending_heartbeats_while_the_lock_is_on(self):
        """★ 锁住的是**动作**，心跳照发 —— 停发反而是异常状态。"""
        self.walk(self.alice, [(0, 100), (200, 100), (400, 100)])
        self.assertTrue(bot_frames(self.alice, self.bot_seat))


    def test_it_shoots_once_the_lock_expires(self):
        self.approach()
        self.unlock_bots()
        self.approach(x=120.0)
        self.assertTrue(fire_frames(self.alice, self.bot_seat))

    def test_standing_up_from_the_dead_arms_it_again(self):
        """★ 判据是**状态翻转**（躺着 -> 站起来），不是定时器（铁律 10）。"""
        self.approach()
        self.unlock_bots()
        self.approach(x=120.0)
        self.clear()
        quest = self.room.quest
        quest.respawn_due[self.bot_seat] = (time.monotonic() + 5.0, (0, 0))
        self.walk(self.alice, [(140, 100)])          # 躺着这一帧
        quest.respawn_due.pop(self.bot_seat)
        self.walk(self.alice, [(160, 100)])          # 站起来的那一帧
        self.assertGreater(self.bot_conn.act_lock_until, time.monotonic())
        self.bot_conn.next_fire_at = 0.0
        self.approach(x=180.0)
        self.assertEqual([], fire_frames(self.alice, self.bot_seat),
                         "刚复活的两秒里不许开枪")

    def test_a_map_change_arms_it_again(self):
        self.unlock_bots()
        self.approach()
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REQ_CHANGE_TO_NEXT_MAP,
            gameserver.w_wstr("Stage02"))
        self.assertIsNone(self.bot_conn.act_lock_until)
        self.clear()
        self.approach(x=300.0)
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))


class BotChargeTests(BotFireRoom):
    """★★ **蓄力**（`PowerControl=2`）—— 长按鼠标左键才扔得远（§73）。

    用户 2026-08-27：「真人操作的时候，手雷是需要长按鼠标左键蓄力的……
    现在 bot 的手雷仿佛不需要蓄力，直接就扔出去了，真人扔这么快的话，
    相当于不蓄力直接扔，这种情况下真人扔不远的，几乎就是直接掉在自己脚下。」

    蓄力计数器每个逻辑 tick `+2`、封顶 80（`0x516694` / `0x51669d`），
    松手时夹进 `[15, 80]`（`0x5167f1` / `0x516802`）。
    """

    def grenade(self):
        """把 bot 换成角色 0 的 2 号槽（苹果雷，`PowerControl=2`）。"""
        self.room.seats[self.bot_seat].character_id = 0
        self.bot_conn.character_id = 0
        self.bot_conn.weapon_slot = 2
        self.bot_conn.declared_weapon = None
        return self.bot_conn.weapon

    def test_the_finger_goes_down_when_there_is_a_target(self):
        self.grenade()
        self.approach()
        self.assertIsNotNone(self.bot_conn.charge_at)

    def test_it_does_not_throw_before_the_charge_is_ready(self):
        """★ 蓄力按**时间**长（`0x516694` 每个 tick +2）—— 手指刚按下去
        的那一格力气还不够（D106 之后一格就是 32 ms，所以只推一格）。"""
        self.grenade()
        self.approach()
        self.bot_conn.charge_at = None      # 手指重新按下去
        self.bot_conn.next_fire_at = 0.0    # 只留蓄力这一道闸
        self.clear()
        self.advance(1)
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))

    def test_it_throws_once_the_charge_is_ready(self):
        self.grenade()
        self.approach()
        self.charge()
        self.approach(x=120.0)
        self.assertTrue(fire_frames(self.alice, self.bot_seat))

    def test_throwing_lets_go_of_the_button(self):
        """`0x51685c: and [char+0x594], 0` —— 扔完蓄力清零，下一颗重按。"""
        self.grenade()
        self.approach()
        self.charge()
        self.approach(x=120.0)
        self.assertTrue(fire_frames(self.alice, self.bot_seat))
        self.clear()
        # ★ 冷却拨掉，只留蓄力这一道闸：手指是从零重按的，所以还是打不出来。
        #   ★ D106：只推**一格**（32 ms）—— 推满一帧的话力气就攒够了。
        self.bot_conn.next_fire_at = 0.0
        self.bot_conn.charge_at = None       # 手指从零重按
        self.advance(1)
        self.assertEqual([], fire_frames(self.alice, self.bot_seat),
                         "第二颗手雷也得重新蓄力")

    def test_the_power_is_one_of_the_values_a_human_can_produce(self):
        self.grenade()
        self.approach()
        self.charge()
        self.approach(x=120.0)
        frames = fire_frames(self.alice, self.bot_seat)
        self.assertTrue(frames)
        power = struct.unpack_from("<f", body_of(frames[0]), 18)[0]
        self.assertTrue(
            power == ballistics.POWER2_MIN
            or (ballistics.POWER2_MIN < power <= ballistics.POWER2_MAX
                and int(power) % 2 == 0), f"power={power}")

    def test_the_heartbeat_shows_the_charge_building_up(self):
        """★ 心跳 `+15` 就是蓄力计数器（`[char+0x594]`，packet_api §5.5）——
        报了它，别人屏幕上才看得见 bot 在攒力气。"""
        self.grenade()
        self.approach()
        self.clear()
        # 手指按下去一会儿了：下一发心跳该带着蓄力值。
        self.bot_conn.charge_at -= 0.5
        self.walk(self.alice, [(120, 100)])
        beats = [f for f in bot_frames(self.alice, self.bot_seat)
                 if udpsync.is_heartbeat(f)]
        self.assertTrue(beats)
        charge = body_of(beats[-1])[7 + 8]
        self.assertTrue(ballistics.POWER2_FLOOR <= charge
                        <= ballistics.POWER2_MAX, charge)
        self.assertEqual(0, charge % 2, "蓄力值只会是偶数")

    def test_a_click_to_fire_weapon_never_charges(self):
        """★ 3 号角色的 2 号武器（`ch02-02`，`PowerControl=1`）**点击即发** ——
        真人看得见它的弹道预测线，不用蓄力（`0x5164c8` 那个分岔）。"""
        self.room.seats[self.bot_seat].character_id = 2
        self.bot_conn.character_id = 2
        self.bot_conn.weapon_slot = 2
        self.bot_conn.declared_weapon = None
        weapon = self.bot_conn.weapon
        self.assertEqual(1002020, weapon.id)
        self.assertEqual(ballistics.MODE_DIRECT_POWER, weapon.power_control)
        self.approach()
        self.assertIsNone(self.bot_conn.charge_at)
        self.assertTrue(fire_frames(self.alice, self.bot_seat))


def dash_frames(conn, seat):
    return [f for f in bot_frames(conn, seat)
            if header(f)["opcode"] == botsync.OP_DASH]


class BotDashTests(BotFireRoom):
    """★★ **近身冲刺攻击**（§64）—— 真人双击左右方向键、消耗体力的那一下。

    用户 2026-08-27：「真人对战时，双击左右移动键时可以消耗体力触发近距离
    攻击动作，这一点 bot 还没实现呢。」

    包的布局是从 4394 发真人 `rpDash` 反推 + `0x492d83` / `0x51515c` /
    `0x515b03` 三处逐指令核对来的；**一发吃 1 个弹体句柄**是从语料量出来的。
    """

    melee = True

    def test_it_dashes_when_the_enemy_is_within_reach(self):
        self.approach()
        frames = dash_frames(self.alice, self.bot_seat)
        self.assertTrue(frames, "贴到跟前就该来一下近身")
        body = body_of(frames[0])
        self.assertEqual(11, len(body))
        seat, direction, index = struct.unpack_from("<Bbb", body, 0)
        self.assertEqual(self.bot_seat, seat)
        self.assertIn(direction, (botsync.DASH_LEFT, botsync.DASH_RIGHT))
        self.assertEqual(0, index, "语料里真人只打第 0 式")

    def test_the_dash_eats_exactly_one_projectile_handle(self):
        """★★★ 收方处理 `rpDash` 时会建一个 `DashDamage` 对象，
        **和弹体共用同一个句柄计数器**（`0x502229`）。服务端不跟着走，
        之后每一发 `rpExplode` 都对不上号、被静默丢弃（§42）。"""
        before = self.bot_conn.sync.projectiles
        self.approach()
        self.assertTrue(dash_frames(self.alice, self.bot_seat))
        self.assertEqual(before + 1, self.bot_conn.sync.projectiles)

    def test_it_costs_stamina_and_will_not_spam(self):
        """★ 体力是原版的闸门：`DashNN-SpCost` 花掉、`SpCharging` 慢慢回。
        收方**不替远端角色算体力**，所以这个约束得 bot 自己上。"""
        move = chrprops.get(self.bot_conn.character_id).dash()
        self.assertIsNotNone(move)
        full = chrprops.game().sp_max
        self.approach()
        # ★ 花掉 `SpCost`，之后按 `SpCharging` 一格一格回。D106 之后
        #   `approach()` 是实打实的 384 ms，回的量不再可以忽略 ——
        #   所以只断言这个区间：花过、而且没白花。
        self.assertLess(self.bot_conn.stamina, full)
        self.assertGreaterEqual(self.bot_conn.stamina, full - move.sp_cost)
        # 体力不够就不冲了。
        self.bot_conn.stamina = move.sp_cost - 1.0
        self.bot_conn.dash_swing = None
        self.clear()
        # ★ D106：只推一格 —— 多推几格体力就按 `SpCharging` 回上来了。
        self.advance(1)
        self.assertEqual([], dash_frames(self.alice, self.bot_seat),
                         "体力不够就不该冲")

    def test_a_connecting_dash_reports_the_damage(self):
        """★ `rpDash` 里**没有伤害** —— 判中和扣血是射手那台机器的活（D28），
        补一发 `rpSplashDamaged`（§67）。"""
        self.approach()
        self.assertTrue(dash_frames(self.alice, self.bot_seat))
        # ★ D106：动作一格一格推（32 ms 一帧动画），而 `rpDash` 包里**没有**
        #   句柄 —— 逐格看 `dash_swing`，把这几下的句柄都攒起来再对账。
        self.clear()
        handles = set()
        for _ in range(60):
            swing = self.bot_conn.dash_swing
            if swing is not None:
                handles.add(swing.handle)
            self.advance(1)
        hits = [f for f in splash_frames(self.alice, self.bot_seat)
                if struct.unpack_from("<i", body_of(f), 0)[0] in handles]
        self.assertTrue(hits, "贴着打这一下该打中")
        body = body_of(hits[0])
        source, victim = struct.unpack_from("<ii", body, 0)
        damage = struct.unpack_from("<f", body, 8)[0]
        self.assertEqual(botsync.character_handle(0), victim)
        move = chrprops.get(self.bot_conn.character_id).dash()
        # ★ 夺分模式（夹具默认 args[1] = 3）伤害 ×2（§87）。
        self.assertAlmostEqual(float(move.damage) * bot._damage_scale(self.room),
                               damage, places=3)
        self.assertIn(source, handles, "伤害源就是那一下的句柄")

    def test_one_dash_damages_at_most_once(self):
        self.approach()
        dashes = dash_frames(self.alice, self.bot_seat)
        self.assertTrue(dashes)
        handle = struct.unpack_from("<i", body_of(dashes[0]), 0)[0]
        for _ in range(3):
            self.walk(self.alice, [tuple(self.alice.sync_trail[-1][:2])])
        # ★ 按**这一下的句柄**数：D106 之后时间真的在走，一次 `approach()`
        #   之后 bot 会再冲第二下 —— 那是另一下，不算重复伤害。
        same = [f for f in splash_frames(self.alice, self.bot_seat)
                if struct.unpack_from("<i", body_of(f), 0)[0] == handle]
        self.assertLessEqual(len(same), 1)

    def test_it_does_not_shoot_while_dashing(self):
        """原版那一下会占住整个角色（`TotalFrame` 帧），真人也开不了枪。"""
        self.approach()
        self.assertTrue(dash_frames(self.alice, self.bot_seat))
        self.assertIsNotNone(self.bot_conn.dash_swing)
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(160.0, 100.0)])
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))

    def test_the_dash_command_turns_it_off(self):
        """`/dash` 是实机上的排除手段：万一句柄不是吃 1 个，
        表现会是「子弹照飞、一滴血不掉」，关掉就能当场分清是不是它。"""
        self.assertTrue(bot.handle_command(self.alice, "/dash"))
        self.assertFalse(self.bot_conn.melee)
        self.clear()
        self.approach()
        self.assertEqual([], dash_frames(self.alice, self.bot_seat))
        self.assertTrue(fire_frames(self.alice, self.bot_seat),
                        "不近身就该老老实实开枪")

    def test_a_map_change_clears_the_swing_and_the_stamina(self):
        """换图时收方把角色重建、句柄计数器复位 —— 这边的记账要一起清。"""
        self.approach()
        self.assertIsNotNone(self.bot_conn.dash_swing)
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REQ_CHANGE_TO_NEXT_MAP,
            gameserver.w_wstr("Stage02"))
        self.assertIsNone(self.bot_conn.dash_swing)
        self.assertIsNone(self.bot_conn.stamina)

    def test_a_holding_bot_does_not_dash(self):
        """`/hold` 的意思是「站住别动」—— 那就别冲出去。"""
        self.bot_conn.holding = True
        self.approach()
        self.assertEqual([], dash_frames(self.alice, self.bot_seat))


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

    def setUp(self):
        super().setUp()
        # ★ 同 `BotFireRoom`：这一批验的是开枪，近身会抢在开枪前面（§64）。
        self.bot_conn.melee = False

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


class BotVersusBotTests(TwoBotFrameRoom):
    """★★ **bot 也是敌人**（用户 2026-08-27 实机报的）。

    他那一局是「真人 + 一个 bot 队友 vs 两个 bot 敌人」，看到的是
    **只有敌方 bot 在打真人**：我方 bot 一枪不放，敌方 bot 也从不打我方 bot。
    根因是 `_hostile_humans` 只扫 `room.human_seats()` —— 敌方全是 bot 的
    那一边于是「没有敌人」。原版里座位上坐的是谁跟该不该打毫无关系。

    这里把两个真人都放进 A 队，复现那个形状：我方 bot 的敌人**只剩**
    对面那个 bot。
    """

    session_type = 1
    arguments = (1, 0, 0)       # 组队战

    def setUp(self):
        super().setUp()
        alice_seat = self.room.seat_index_of(self.alice)
        bob_seat = self.room.seat_index_of(self.bob)
        self.team_a = self.room.seats[alice_seat].team
        self.room.seats[bob_seat].team = self.team_a
        friends = [i for i in self.bot_seats
                   if self.room.seats[i].team == self.team_a]
        foes = [i for i in self.bot_seats if i not in friends]
        self.assertTrue(friends and foes, "夹具没把两个 bot 分到两边")
        self.friend_seat, self.foe_seat = friends[0], foes[0]
        for index in self.bot_seats:
            # ★ 同 `BotFireRoom`：这一批验的是开枪，近身会抢在前面（§64）。
            self.room.seats[index].conn.melee = False

    def test_a_friendly_bot_counts_the_enemy_bot_as_a_target(self):
        self.walk(self.alice, [(100, 100), (120, 100), (140, 100)])
        seats = [t[0] for t in bot._hostile_targets(self.room,
                                                    self.friend_seat)]
        self.assertIn(self.foe_seat, seats)

    def test_an_enemy_bot_counts_the_friendly_bot_as_a_target(self):
        self.walk(self.alice, [(100, 100), (120, 100), (140, 100)])
        seats = [t[0] for t in bot._hostile_targets(self.room, self.foe_seat)]
        self.assertIn(self.friend_seat, seats)

    def test_a_bot_never_targets_its_own_team_or_itself(self):
        self.walk(self.alice, [(100, 100), (120, 100), (140, 100)])
        seats = [t[0] for t in bot._hostile_targets(self.room,
                                                    self.friend_seat)]
        self.assertNotIn(self.friend_seat, seats)
        self.assertEqual([self.foe_seat], seats)

    def test_the_friendly_bot_opens_fire_although_no_human_is_hostile(self):
        """★ 这就是用户看到的那条：我方 bot 一枪不放。"""
        self.walk(self.alice, [(100, 100), (120, 100), (140, 100)])
        self.assertTrue(fire_frames(self.alice, self.friend_seat))

    def test_a_dead_bot_is_not_a_target(self):
        self.walk(self.alice, [(100, 100), (120, 100), (140, 100)])
        self.room.quest.respawn_due[self.foe_seat] = time.monotonic() + 5.0
        self.assertEqual([], bot._hostile_targets(self.room, self.friend_seat))


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
        self.change_map()
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.approach(x=300.0)
        explodes = explode_frames(self.alice, self.bot_seat)
        self.assertTrue(explodes)
        self.assertEqual(botsync.projectile_handle(self.bot_seat, 0),
                         struct.unpack_from("<i", body_of(explodes[0]), 0)[0])


# ---------------------------------------------------------------------------
# ★★★ 自己走位（M5）
# ---------------------------------------------------------------------------
#: 合成地形建一次要跑几十万次循环，按 key 存下来全套用例共用。
_TERRAIN_CACHE = {}


def synth_terrain(key, floor=150, width=1400, height=180, walls=(), pits=(),
                  points=None):
    """造一张平地，可选地插几段**高台**和几段**无底洞**。

    `walls` / `pits` 都是 `(x0, x1, ...)` 的区间（左闭右开）。
    `points` 是产物里那张**出生点表**（`{类型号: [(x, y), …]}`，§91）。
    """
    if key in _TERRAIN_CACHE:
        return _TERRAIN_CACHE[key]
    heights = [floor] * width
    for x0, x1, top in walls:
        for x in range(x0, min(x1, width)):
            heights[x] = top
    holes = set()
    for x0, x1 in pits:
        holes.update(range(x0, min(x1, width)))
    rows = []
    for y in range(height):
        rows.append("".join(
            "0" if (x in holes or y < heights[x]) else "2"
            for x in range(width)))
    terrain = mapdata.MapTerrain(make_record(rows, points=points))
    _TERRAIN_CACHE[key] = terrain
    return terrain


class _FakeBreakable(object):
    """只带 `handle` 一格的假破坏物 —— `_is_breakable_handle` 只读它。"""

    def __init__(self, handle):
        self.handle = handle


class TerrainMixin(object):
    """把一张合成地形挂到这个房间当前那张图的名下（用完摘掉）。"""

    def install_terrain(self, terrain):
        name = "ZZ_test_terrain"
        self.room.map_name = name
        quest = getattr(self.room, "quest", None)
        if quest is not None and getattr(quest, "maps_entered", None):
            quest.maps_entered[-1] = name
        mapdata.STORE.index()["maps"][name] = {"file": "-"}
        mapdata.STORE._cache[name] = terrain
        self.addCleanup(mapdata.STORE.index()["maps"].pop, name, None)
        self.addCleanup(mapdata.STORE._cache.pop, name, None)
        return terrain

    def place_bot(self, x, y=150.0):
        """把 bot 直接摆在某个落脚点上（省掉「先跟真人锚一帧」那一步）。"""
        self.bot_conn.battle_pos = (x, y)
        self.bot_conn.body = bot.botmove.Body(x, y)

    def beats(self, count, x, y=150.0):
        """真人在 `(x, y)` 站着发 `count` 发心跳 —— bot 跟着走 `count × 4` 格。

        ★ D106 之后不用再拨任何时刻：一发心跳就是实打实的 4 个 32 ms 格子，
        `advance()` 推的就是它们。以前那个 `move_at` 累加器没有了。
        """
        for _ in range(count):
            self.human_heartbeat(self.alice, x, y)


class BotOwnMovementTests(TerrainMixin, BotFireRoom):
    """★★★ **bot 自己走位**（M5 / §71）—— 不再只回放真人的轨迹（D16）。

    规则只有两条，都不是我们发明的（D50）：**打得到就站住打**、
    **打不到就朝最近的敌人走过去**（§48 量出来的真人交战距离）。
    地形只回答「这一步走不走得成」。
    """

    melee = False                       # 近身会抢在开枪前面，这批用例不验它

    def force_no_shot(self):
        """让这一批只验“到不了就寻路”，不被射程判定提前截停。"""
        original = bot._fire_target
        bot._fire_target = lambda *_args, **_kwargs: None
        self.addCleanup(setattr, bot, "_fire_target", original)

    def test_it_walks_toward_the_enemy_when_it_has_no_shot(self):
        """★ 隔着一整张图（> `BOT_ENGAGE_RANGE`）时朝对方挪。"""
        self.install_terrain(synth_terrain("flat"))
        self.place_bot(200.0)
        self.beats(4, 1300.0)
        self.assertTrue(self.bot_conn.body.x > 200.0,
                        "打不到就该走过去，实际停在 %.1f" % self.bot_conn.body.x)
        self.assertTrue(self.bot_conn.body.on_ground)

    def test_the_wire_position_is_its_own_not_the_humans_trail(self):
        """★★ 这就是「站到人身上」那个毛病的根：以前心跳里报的是**真人
        走过的点**，现在报的是 bot 自己算出来的落脚点。"""
        self.install_terrain(synth_terrain("flat"))
        self.place_bot(200.0)
        self.beats(3, 1300.0)
        frames = [f for f in bot_frames(self.alice, self.bot_seat)
                  if udpsync.is_heartbeat(f)]
        self.assertTrue(frames)
        x, y = udpsync.heartbeat_position(frames[-1])
        self.assertEqual((int(self.bot_conn.body.x), int(self.bot_conn.body.y)),
                         (x, y))
        self.assertTrue(abs(x - 1300) > 500, "不该被拽到真人身边")

    def test_it_holds_its_ground_when_it_can_shoot(self):
        """★ 打得到就打 —— 语料里真人 39% 的心跳是站着不动的。"""
        self.install_terrain(synth_terrain("flat"))
        self.place_bot(700.0)
        self.beats(4, 1000.0)
        self.assertAlmostEqual(700.0, self.bot_conn.body.x)
        self.assertTrue(fire_frames(self.alice, self.bot_seat),
                        "站住是为了打，不是发呆")

    def test_a_step_it_cannot_climb_makes_it_jump(self):
        """前面是爬不上去的坎就跳 —— 真人卡在墙根时也是这么干的。

        ★ M5-C 之后必须先 `force_no_shot()`：会自动换枪的 bot 走到 620 就
        **换成抛物线武器隔着这堵墙把手雷吊过去**（实测 `1002010 -> 1002020`），
        于是站住开枪、根本走不到墙根。那是想要的行为，不是回归 —— 这一条
        验的是「走不过去就跳」，得先把开枪这条路关掉。
        """
        self.force_no_shot()
        self.install_terrain(synth_terrain(
            "wall", walls=((900, 960, 90),)))
        self.place_bot(200.0)
        self.beats(40, 1300.0)
        jumps = [f for f in bot_frames(self.alice, self.bot_seat)
                 if header(f)["opcode"] == botsync.OP_JUMP]
        self.assertTrue(jumps, "撞上 60 个单位高的坎就该起跳")

    def test_it_does_not_walk_into_a_bottomless_pit(self):
        """★ 判据是「脚下有没有路」（跳得过去就跳），**不是**「离远点」。"""
        self.install_terrain(synth_terrain("pit", pits=((400, 900),)))
        self.place_bot(300.0)
        self.beats(20, 1300.0)
        body = self.bot_conn.body
        self.assertTrue(body.on_ground, "不该掉进无底洞")
        self.assertTrue(body.x < 400.0,
                        "该停在坑边上，实际走到了 %.1f" % body.x)

    def test_it_executes_the_planned_jump_onto_a_high_platform(self):
        """★★ A* 算出的高台边必须真变成跳跃帧，并最终落到高台上。"""
        self.force_no_shot()
        self.install_terrain(synth_terrain(
            "nav_runtime_high", floor=200, height=240,
            walls=((680, 1400, 80),)))
        self.place_bot(560.0, 200.0)
        self.beats(36, 900.0, 80.0)
        body = self.bot_conn.body
        self.assertTrue(body.on_ground)
        self.assertGreaterEqual(body.x, 680.0)
        self.assertAlmostEqual(80.0, body.y)
        jumps = [f for f in bot_frames(self.alice, self.bot_seat)
                 if header(f)["opcode"] == botsync.OP_JUMP]
        self.assertTrue(jumps, "高台路线必须实际发出跳跃帧")

    def test_it_executes_the_planned_jump_across_a_gap(self):
        """★★ 有对岸的坑要按规划跳过去，而不是沿用无底坑前停步兜底。"""
        self.force_no_shot()
        self.install_terrain(synth_terrain(
            "nav_runtime_gap", floor=180, height=220,
            pits=((640, 720),)))
        self.place_bot(560.0, 180.0)
        self.beats(36, 900.0, 180.0)
        body = self.bot_conn.body
        self.assertTrue(body.on_ground)
        self.assertGreater(body.x, 720.0)
        jumps = [f for f in bot_frames(self.alice, self.bot_seat)
                 if header(f)["opcode"] == botsync.OP_JUMP]
        self.assertTrue(jumps, "跨坑路线必须实际发出跳跃帧")

    def test_it_presses_down_to_leave_a_one_way_platform(self):
        """★★ 目标在细绳下方时，身体下落且心跳键位明确带 KEY_DOWN。"""
        self.force_no_shot()
        rows = []
        for y in range(220):
            if y == 80:
                rows.append("1" * 1400)
            elif y >= 180:
                rows.append("2" * 1400)
            else:
                rows.append("0" * 1400)
        self.install_terrain(mapdata.MapTerrain(make_record(rows)))
        self.place_bot(700.0, 80.0)
        self.beats(16, 705.0, 180.0)
        body = self.bot_conn.body
        self.assertTrue(body.on_ground)
        self.assertAlmostEqual(180.0, body.y)
        heartbeats = [f for f in bot_frames(self.alice, self.bot_seat)
                      if udpsync.is_heartbeat(f)]
        keys = [struct.unpack_from("<H", body_of(frame), 7 + 16)[0]
                for frame in heartbeats]
        self.assertTrue(any(value & botsync.KEY_DOWN for value in keys),
                        "穿过单向平台的那一帧必须真的按下 ↓")

    def test_without_terrain_data_it_falls_back_to_the_human_trail(self):
        """没有地形产物的图上退回 D16 那条老路 —— 少走两步好过走进墙里。"""
        self.room.map_name = "没有这张图"
        self.walk(self.alice, [(0.0, 50.0), (120.0, 50.0), (240.0, 50.0)])
        self.assertIsNone(self.bot_conn.body)
        self.assertEqual((120, 50),
                         udpsync.heartbeat_position(self.last_beat()))


class BotWeaponChoiceTests(TerrainMixin, BotFireRoom):
    """★★ M5-C：按此刻的局面自己换枪；房主锁了 / 捡到枪了就轮不到 AI。"""

    def test_a_wall_makes_it_pick_the_lobbing_weapon(self):
        """★ 直射被墙挡住、抛物线吊得过去 ⇒ 换成抛物线那把。

        这不是「近战用手雷」那种偏好表：直射武器在 `_engagement()` 里被
        `_path_blocked()` 判成**打不到**，压根不进候选。
        """
        self.install_terrain(synth_terrain("wall", walls=((900, 960, 90),)))
        self.place_bot(560.0)
        self.beats(12, 1300.0)
        weapon = self.bot_conn.weapon
        self.assertGreater(weapon.gravity, 0.0,
                           "隔着墙就该改用抛物线武器，实际是 %s" % weapon.id)

    def test_the_host_lock_beats_the_ai(self):
        """`/w N` 锁住之后，局面再怎么变也不许换（用户要的「只能用指定武器」）。"""
        self.install_terrain(synth_terrain("wall", walls=((900, 960, 90),)))
        self.room.bot_weapon_slot = 1
        self.bot_conn.weapon_slot = 1
        self.place_bot(560.0)
        self.beats(12, 1300.0)
        self.assertEqual(1, self.bot_conn.weapon.raw["slot"])
        self.assertIsNone(self.bot_conn.auto_weapon_id)

    def test_a_picked_up_gun_beats_the_ai(self):
        """地上捡来的枪压过 AI 的选择（§115 / §223 的原版口径）。"""
        self.install_terrain(synth_terrain("flat"))
        self.place_bot(560.0)
        picked = weapondata.get(gameserver.PVP_WEAPON_GIVES[10200])
        self.bot_conn.item_weapon = picked
        self.bot_conn.item_weapon_shots = picked.force_count
        self.beats(6, 900.0)
        self.assertEqual(picked.id, self.bot_conn.weapon.id)
        self.assertIsNone(self.bot_conn.auto_weapon_id)

    def test_it_does_not_flip_flop_between_two_close_options(self):
        """★ 换枪要丢半个弹匣，所以要有迟滞 —— 一局里不该来回横跳。"""
        self.install_terrain(synth_terrain("flat"))
        self.place_bot(300.0)
        self.beats(24, 900.0)
        changes = change_weapon_frames(self.alice, self.bot_seat)
        self.assertLessEqual(len(changes), 2,
                             "24 帧里换了 %d 次枪" % len(changes))


class BotStanceTests(TerrainMixin, BotFireRoom):
    """★★ M5-C：按双方血量决定逼近还是拉开（用户 2026-08-29 的要求）。"""

    def setUp(self):
        super().setUp()
        self.install_terrain(synth_terrain("flat"))
        self.alice_seat = self.room.seat_index_of(self.alice)

    def hurt(self, seat, fraction):
        """把这个座位打掉 `fraction` 的血。

        ★ 按**比例**而不是写死的数字：角色的满血各不相同
        （`ChrProps.ini` 的 `Hp`，角色 0 是 100、角色 2 是 130）。
        """
        top = bot._seat_max_hp(self.room, seat)
        bot._health(self.room).note_damage(seat, top * fraction)

    def test_a_healthy_bot_presses_in(self):
        self.place_bot(200.0)
        self.beats(6, 1300.0)
        self.assertEqual("press", self.bot_conn.stance)
        self.assertGreater(self.bot_conn.body.x, 200.0)

    def test_a_badly_hurt_bot_backs_away(self):
        """★ 血被打掉大半 ⇒ 「照这样打下去我先倒」⇒ 往后退。"""
        self.place_bot(700.0)
        self.beats(2, 900.0)
        self.hurt(self.bot_seat, 0.9)
        before = self.bot_conn.body.x
        self.beats(8, 900.0)
        self.assertEqual("retreat", self.bot_conn.stance)
        self.assertLess(self.bot_conn.body.x, before,
                        "该往远离敌人的方向退，实际停在 %.1f"
                        % self.bot_conn.body.x)

    def test_it_comes_back_when_the_enemy_is_the_hurt_one(self):
        self.place_bot(700.0)
        self.beats(2, 900.0)
        book = bot._health(self.room)
        self.hurt(self.bot_seat, 0.9)
        self.beats(4, 900.0)
        self.assertEqual("retreat", self.bot_conn.stance)
        book.reset(self.bot_seat)
        self.hurt(self.alice_seat, 0.9)
        self.beats(4, 900.0)
        self.assertEqual("press", self.bot_conn.stance)

    def test_retreating_still_shoots(self):
        """拉开距离不等于不开枪 —— 真人也是一边退一边打（§37 的朝向口径）。"""
        self.place_bot(700.0)
        self.beats(2, 900.0)
        self.hurt(self.bot_seat, 0.9)
        # ★ 先把在飞的那一发结算掉：带溅射的枪要等上一发炸完
        #   才能开下一枪（`_may_fire()` 的句柄闸门，§43）。
        self.settle()
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.beats(8, 900.0)
        self.assertEqual("retreat", self.bot_conn.stance)
        self.assertTrue(fire_frames(self.alice, self.bot_seat),
                        "退的时候也该还手")


class BotAimLeadTests(BotFireRoom):
    """★★ M5-D：对着**动的人**要算提前量；失误时要真的打偏。"""

    def fired_angle(self):
        frames = fire_frames(self.alice, self.bot_seat)
        self.assertTrue(frames)
        return struct.unpack_from("<f", body_of(frames[-1]), 14)[0]

    def cursor_x(self):
        beats = [f for f in bot_frames(self.alice, self.bot_seat)
                 if udpsync.is_heartbeat(f)]
        self.assertTrue(beats)
        return struct.unpack_from("<h", body_of(beats[-1]), 7 + 0x12)[0]

    def test_it_leads_a_target_running_away(self):
        """★ 目标朝右跑 ⇒ 瞄准点在他前面，准星 x **大于**他此刻的 x。"""
        self.walk(self.alice, [(600.0, 100.0), (632.0, 100.0),
                               (664.0, 100.0)])
        self.bot_conn.holding = True
        self.clear()
        self.walk(self.alice, [(696.0, 100.0), (728.0, 100.0)])
        self.assertGreater(self.cursor_x(), self.alice.sync_trail[-1][0],
                           "朝右跑的人要往他前面瞄")

    def test_a_standing_target_gets_no_lead(self):
        """站着不动的人，提前量退化成 0 —— M5-D 不该改变老行为。"""
        self.approach(x=400.0)
        self.assertLessEqual(
            abs(self.cursor_x() - self.alice.sync_trail[-1][0]), 2)

    def test_a_rolled_miss_really_bends_the_shot(self):
        """★★ 失误必须**连弹道一起重解** —— 否则包里的角度还是准的。"""
        self.approach(x=400.0)
        honest = self.fired_angle()
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.bot_conn.roll = lambda n: 0            # 必失误（提前量 ×−0.5）
        bot._reroll_aim_miss(self.bot_conn)
        self.approach(x=400.0)
        self.assertNotAlmostEqual(honest, self.fired_angle(), places=4)

    def test_the_difficulty_is_the_only_knob(self):
        """三档只改两个概率，物理一格都不动（M5-A 的口径）。"""
        self.room.bot_difficulty = "easy"
        easy = bot._aim_error_chance(self.room, self.bot_conn, self.bot_seat)
        self.room.bot_difficulty = "hard"
        hard = bot._aim_error_chance(self.room, self.bot_conn, self.bot_seat)
        self.assertGreater(easy, hard)

    def test_a_hud_jam_makes_it_much_worse(self):
        """★ 别人放糊屏 ⇒ 失误概率明显上去（§121，用户的要求）。"""
        self.room.bot_difficulty = "medium"
        plain = bot._aim_error_chance(self.room, self.bot_conn, self.bot_seat)
        self.room.quest.hud_jam_until[self.bot_seat] = time.monotonic() + 8.0
        jammed = bot._aim_error_chance(self.room, self.bot_conn, self.bot_seat)
        self.assertGreater(jammed, plain + 0.3)


class BotEntryLockMovementTests(TerrainMixin, BotFireRoom):
    """★★★ 进图那一档**连走都不许走**（§94）—— 复活那一档只拦动手。

    用户 2026-08-28：「一开始我还不能动呢，我就看见 bot 已经向我这边
    跑来了」。M5 之前 bot 回放真人轨迹，真人不动它就不动，所以这条天然
    成立；自己会走之后就得显式挡住。
    ⚠ 分界线是用户自己给的另一句：「被打死复活后有大概两三秒不能开枪，
    **只能移动**」（2026-08-27）。
    """

    action_lock = True                  # ★ 这一批要的就是那道锁
    melee = False

    def test_it_stands_still_during_the_entry_lock(self):
        self.install_terrain(synth_terrain("flat"))
        self.place_bot(200.0)
        self.beats(6, 1300.0)           # 真人在很远的地方，正常会走过去
        self.assertGreater(self.bot_conn.enter_lock_until, time.monotonic())
        self.assertEqual(200.0, self.bot_conn.body.x,
                         "预备 / 开始那两秒 bot 不该迈腿")

    def test_it_walks_once_the_entry_lock_expires(self):
        self.install_terrain(synth_terrain("flat"))
        self.place_bot(200.0)
        self.unlock_bots()
        self.beats(6, 1300.0)
        self.assertGreater(self.bot_conn.body.x, 200.0)

    def test_the_respawn_lock_still_lets_it_walk(self):
        self.install_terrain(synth_terrain("flat"))
        self.place_bot(200.0)
        self.unlock_bots()              # 进图那一档已经过去了
        quest = self.room.quest
        quest.respawn_due[self.bot_seat] = (time.monotonic() + 5.0, (0, 0))
        self.beats(1, 1300.0)           # 躺着这一帧
        quest.respawn_due.pop(self.bot_seat)
        self.beats(6, 1300.0)           # 站起来之后接着走
        self.assertGreater(self.bot_conn.act_lock_until, time.monotonic(),
                           "复活那一刻动手的锁要重新挂上")
        self.assertGreater(self.bot_conn.body.x, 200.0,
                           "★ 复活之后照样能走")


class BotCoopMovementTests(TerrainMixin, BotFrameRoom):
    """★★ 闯关房从 M5-G 起**自己走**；会话 42 起目标是「最靠前那个真人」。

    以前这儿是纯轨迹回放（D16）：节奏天然对，可**一步都躲不开**。
    M5-G 把跟随点当寻路目标，中间那段路自己走 —— 躲子弹 / 跳坑 / 打怪
    全接得上。会话 42 又改了两处（用户 2026-08-30 实机「有 bot 一直在
    屏幕最左边拖进度」）：盯的是**推进得最远**那个真人的**当前位置**
    （不再是「最近的人的轨迹」），而且掉了队就**冲刺**追。
    """

    def test_it_takes_over_its_own_walking(self):
        self.install_terrain(synth_terrain("flat"))
        self.walk(self.alice, [(0.0, 150.0), (120.0, 150.0), (240.0, 150.0)])
        self.assertIsNotNone(self.bot_conn.body, "闯关房也该自己走")

    def test_it_keeps_pace_with_the_party(self):
        """★ 「不能太慢拖进度、也不能太快甩太远」—— 由**活动带**保证（D114）。

        判据就是用户的原话：走完之后 bot 要落在「带头真人的后 1/4 屏 ~
        前 1/3 屏」这个范围里。**不是**落在某个固定点位附近。
        """
        self.install_terrain(synth_terrain("flat"))
        self.walk(self.alice, [(0.0, 150.0), (120.0, 150.0), (240.0, 150.0)])
        for _ in range(24):
            self.human_heartbeat(self.alice, 240.0, 150.0)
        lag = bot._coop_lag(self.room, self.bot_conn,
                            mapdata.load(self.room.map_name))
        self.assertIsNotNone(lag)
        self.assertTrue(bot._coop_in_band(lag[0]),
                        "该待在活动带里，实际落后 %.0f（带是 %.0f ~ %.0f）"
                        % (lag[0], -bot.BOT_COOP_AHEAD_LIMIT,
                           bot.BOT_COOP_LEASH))

    def test_without_a_leader_it_falls_back_to_the_old_replay(self):
        self.install_terrain(synth_terrain("flat"))
        self.assertIsNone(bot._coop_goal(
            self.room, self.bot_conn, self.bot_seat,
            mapdata.load(self.room.map_name)))

    def test_forward_comes_from_the_spawn_points(self):
        """★ 「前方」是每张图自己的出生点说的，不是「都往右」。"""
        right = synth_terrain("fwd_right", points={101: [(100, 40)]})
        self.assertEqual(1, bot._quest_forward(right))
        left = synth_terrain("fwd_left", points={101: [(1300, 40)]})
        self.assertEqual(-1, bot._quest_forward(left))
        # 一个出生点都没有的图不猜，默认 +x。
        self.assertEqual(1, bot._quest_forward(synth_terrain("fwd_none")))

    def test_it_follows_the_most_advanced_human_not_the_nearest(self):
        """★★★ 跟**最靠前**那个真人（用户 2026-08-30「bot 拖进度」）。

        以前跟「离自己最近的」：bot 掉队之后最近的往往是**后面**那个人，
        于是整队互相锚着，一起停在图的左边。
        """
        terrain = self.install_terrain(
            synth_terrain("coop_lead", points={101: [(100, 40)]}))
        # alice 掉在后面、bob 冲在前面；bot 就站在 alice 旁边。
        self.walk(self.alice, [(0.0, 150.0), (200.0, 150.0)])
        self.walk(self.bob, [(600.0, 150.0), (900.0, 150.0)])
        self.bot_conn.battle_pos = (210.0, 150.0)
        self.assertIs(self.bob,
                      bot._coop_leader(self.room, bot._quest_forward(terrain)),
                      "该跟最靠前的 bob，不是离得最近的 alice")
        # 以前那条「跟最近的」正好会挑中 alice —— 这就是拖进度的根。
        self.assertIs(self.alice, bot._follow_target(self.room, self.bot_conn))

    def test_it_sprints_when_it_has_fallen_behind(self):
        """★★★ 掉队就**冲刺**追 —— 真人是按着右键推进的，走速追不上。"""
        self.install_terrain(synth_terrain("coop_sprint", width=2400,
                                           points={101: [(100, 40)]}))
        self.walk(self.alice, [(0.0, 150.0), (200.0, 150.0)])
        self.bot_conn.battle_pos = (200.0, 150.0)
        self.bot_conn.body = bot.botmove.Body(200.0, 150.0)
        # 真人一下子跑到很远的前面。
        self.walk(self.alice, [(1800.0, 150.0), (2000.0, 150.0)])
        terrain = mapdata.load(self.room.map_name)
        intent = bot._coop_intent(self.room, self.bot_conn, self.bot_seat,
                                  terrain, None)
        self.assertEqual(1, intent[0], "该朝前走")
        self.assertTrue(intent[3], "掉这么远还不冲刺")

    def test_it_does_not_sprint_once_it_has_caught_up(self):
        self.install_terrain(synth_terrain("coop_close",
                                           points={101: [(100, 40)]}))
        self.walk(self.alice, [(0.0, 150.0), (400.0, 150.0)])
        self.bot_conn.battle_pos = (330.0, 150.0)
        self.bot_conn.body = bot.botmove.Body(330.0, 150.0)
        terrain = mapdata.load(self.room.map_name)
        intent = bot._coop_intent(self.room, self.bot_conn, self.bot_seat,
                                  terrain, None)
        self.assertFalse(intent[3], "已经跟上了就别一直冲刺，体力要留着")

    # -- ★★★ D103：活动范围 = 带头的人后方 1/4 屏 ~ 前方 1/3 屏 -------------
    #
    # 用户 2026-08-30 第五轮：「bot 的活动范围限定在第一个真人的后方 1/4
    # 屏幕到前方 1/3 屏幕这个范围内，超过就要尽快回去」+「即便已经在允许
    # 的范围内，也要尽量往前走，前面比后面好，不要总停在最后面的界限边缘」。

    def test_the_goal_is_the_leader_himself(self):
        """★★★ 目标就是带头的人本人 —— **没有按名次排的固定点位**（D114）。

        用户 2026-09-01：「我希望不要跟随固定点位，我原话说的是 bot 要在
        带头真人的后 1/4 屏 ~ 前 1/3 屏范围内。」以前这里是
        `他 + (前界 − 120 × 名次)`，于是 bot 超过自己那个点、又没到全局
        前界的那一段会**掉头往回走** —— 走回它刚跳过去的岩浆坑。
        """
        self.install_terrain(synth_terrain("coop_band",
                                           points={101: [(100, 40)]}))
        self.walk(self.alice, [(0.0, 150.0), (400.0, 150.0)])
        goal = bot._coop_goal(self.room, self.bot_conn, self.bot_seat,
                              mapdata.load(self.room.map_name))
        self.assertEqual((400.0, 150.0), goal)

    def test_the_band_is_the_quarter_screen_behind_to_a_third_ahead(self):
        """★ 活动带的两条边就是用户说的那两个数，名次一点都不参与。"""
        self.assertTrue(bot._coop_in_band(0.0))
        self.assertTrue(bot._coop_in_band(bot.BOT_COOP_LEASH))
        self.assertTrue(bot._coop_in_band(-bot.BOT_COOP_AHEAD_LIMIT))
        self.assertFalse(bot._coop_in_band(bot.BOT_COOP_LEASH + 1.0))
        self.assertFalse(bot._coop_in_band(-bot.BOT_COOP_AHEAD_LIMIT - 1.0))

    def test_inside_the_band_it_stops_adjusting(self):
        """★★★ 「只要进入了这个范围内，bot 就没必要再继续调整身位」。

        ★ 走位停住而已 —— 打怪 / 躲子弹排在 `_coop_intent()` 前面，照旧。
        """
        terrain = self.install_terrain(
            synth_terrain("coop_inband", width=4000,
                          points={101: [(100, 40)]}))
        self.walk(self.alice, [(0.0, 150.0), (400.0, 150.0)])
        for lag in (0.0, bot.BOT_COOP_LEASH - 8.0,
                    -bot.BOT_COOP_AHEAD_LIMIT + 8.0):
            where = 400.0 - lag
            self.bot_conn.battle_pos = (where, 150.0)
            self.bot_conn.body = bot.botmove.Body(where, 150.0)
            intent = bot._coop_intent(self.room, self.bot_conn, self.bot_seat,
                                      terrain, None)
            self.assertEqual((0, False, False, False), intent,
                             "落后 %.0f 已经在带里，不该再挪（实际 %r）"
                             % (lag, intent))

    def test_ahead_of_the_band_it_never_walks_back(self):
        """★★★ 超前出带 = 站住等。**往回走那一步经常就是走回刚跳过的坑。**"""
        terrain = self.install_terrain(
            synth_terrain("coop_overshoot", width=4000,
                          points={101: [(100, 40)]}))
        self.walk(self.alice, [(0.0, 150.0), (400.0, 150.0)])
        # 只超前一点点：以前这一档正好落在「过了自己的跟随点、没到全局
        # 前界」的窗口里，bot 会掉头。
        for over in (bot.BOT_COOP_AHEAD_LIMIT + 8.0,
                     bot.BOT_COOP_AHEAD_LIMIT + 400.0):
            where = 400.0 + over
            self.bot_conn.battle_pos = (where, 150.0)
            self.bot_conn.body = bot.botmove.Body(where, 150.0)
            intent = bot._coop_intent(self.room, self.bot_conn, self.bot_seat,
                                      terrain, None)
            self.assertEqual(0, intent[0],
                             "超前 %.0f 该站住等，不该往回走（方向 %d）"
                             % (over, intent[0]))

    def test_too_far_ahead_it_stops_and_waits(self):
        """★★ 冲过前界 = **停下来等**，不是往回走。"""
        self.install_terrain(synth_terrain("coop_ahead", width=4000,
                                           points={101: [(100, 40)]}))
        self.walk(self.alice, [(0.0, 150.0), (400.0, 150.0)])
        far = 400.0 + bot.BOT_COOP_AHEAD_LIMIT + 200.0
        self.bot_conn.battle_pos = (far, 150.0)
        self.bot_conn.body = bot.botmove.Body(far, 150.0)
        intent = bot._coop_intent(self.room, self.bot_conn, self.bot_seat,
                                  mapdata.load(self.room.map_name), None)
        self.assertEqual(0, intent[0],
                         "超过前界该站住等，不该往回走（实际方向 %d）"
                         % intent[0])
        self.assertFalse(intent[3])


class BotCoopLeashTests(TerrainMixin, BotFrameRoom):
    """★★★★ 闯关**牵引绳**（D99）—— 落后 3/4 屏就无条件追，追不上就瞬移。

    用户 2026-08-30 第三轮实机：「还是总有 bot 不停的躲在画面最左边阻挡
    进度，每次都走不动太烦了」。这一条是他下的产品决定，优先级压过一切。
    """

    def leash_room(self, **kwargs):
        kwargs.setdefault("width", 4000)
        kwargs.setdefault("points", {101: [(100, 40)]})
        terrain = self.install_terrain(synth_terrain(**kwargs))
        self.walk(self.alice, [(0.0, 150.0), (200.0, 150.0)])
        return terrain

    def test_within_the_leash_nothing_changes(self):
        terrain = self.leash_room(key="leash_near")
        near = bot.BOT_COOP_LEASH / 2.0        # 后界以内 = 牵引绳不插手
        self.place_bot(200.0)
        self.walk(self.alice, [(200.0 + near - 100.0, 150.0),
                               (200.0 + near, 150.0)])
        self.assertIsNone(
            bot._coop_leash_intent(self.room, self.bot_conn, self.bot_seat,
                                   terrain),
            "落后 %.0f 还没到后界（%.0f），牵引绳不该插手"
            % (near, bot.BOT_COOP_LEASH))
        self.assertFalse(self.bot_conn.leash_lagging)

    def test_past_the_leash_it_chases_at_a_sprint(self):
        terrain = self.leash_room(key="leash_far")
        self.place_bot(200.0)
        self.walk(self.alice, [(1500.0, 150.0), (1600.0, 150.0)])
        intent = bot._coop_leash_intent(self.room, self.bot_conn,
                                        self.bot_seat, terrain)
        self.assertIsNotNone(intent, "落后 1400 早过了后界")
        self.assertEqual(1, intent[0], "该朝前追")
        self.assertTrue(intent[3], "该按着右键冲刺")
        self.assertTrue(self.bot_conn.leash_lagging)

    def test_it_outranks_dodging(self):
        """★★★ 「无论发生什么」—— 连躲子弹都要往后排。"""
        terrain = self.leash_room(key="leash_dodge")
        self.place_bot(200.0)
        self.walk(self.alice, [(1500.0, 150.0), (1600.0, 150.0)])
        original = bot._dodge_intent
        bot._dodge_intent = lambda *_a, **_k: (-1, True, False, False)
        self.addCleanup(setattr, bot, "_dodge_intent", original)
        # 先确认这张「一定会躲」的桩在对战房里确实会被采纳。
        intent = bot._move_intent(self.room, self.bot_conn, self.bot_seat,
                                  terrain, None)
        self.assertEqual(1, intent[0],
                         "掉队时该往前追，不该按躲子弹那一套往回走")

    def test_it_gives_way_back_once_it_has_caught_up(self):
        terrain = self.leash_room(key="leash_back")
        self.place_bot(200.0)
        self.walk(self.alice, [(1500.0, 150.0), (1600.0, 150.0)])
        bot._coop_leash_intent(self.room, self.bot_conn, self.bot_seat,
                               terrain)
        self.assertTrue(self.bot_conn.leash_lagging)
        self.place_bot(1550.0)
        self.assertIsNone(bot._coop_leash_intent(
            self.room, self.bot_conn, self.bot_seat, terrain))
        self.assertFalse(self.bot_conn.leash_lagging)
        self.assertIsNone(self.bot_conn.leash_mark)

    def test_an_impassable_wall_ends_in_a_warp(self):
        """★★★ 「实在走不过去就瞬移传送也行」（用户 2026-08-30）。

        判据是**规划器的判决**，不是等了多久：墙对面够不着 ⇒ A\\* 泛洪完
        回空 ⇒ 当场瞬移。这也正是「真人停下来等 bot」那个死锁的出口 ——
        两边都不动的时候，只有这条判据还会产生结论。

        （墙是**通天**的：`walls` 的第三格是这一段的地面高度，写 0 就等于
        从图顶实到图底，跳不过去也钻不过去。）
        """
        terrain = self.leash_room(key="leash_wall", width=4000,
                                  walls=((1000, 1200, 0),))
        self.place_bot(200.0)
        self.walk(self.alice, [(3000.0, 150.0), (3200.0, 150.0)])
        warped = []
        original = bot._leash_warp
        bot._leash_warp = lambda *a, **k: (warped.append(a[4])
                                           or original(*a, **k))
        self.addCleanup(setattr, bot, "_leash_warp", original)
        for _ in range(60):
            self.beats(1, 3200.0)
            if warped:
                break
        self.assertTrue(warped, "坑对面够不着，A* 回空之后就该瞬移；"
                                "bot 停在 %.0f" % self.bot_conn.body.x)
        self.assertGreater(self.bot_conn.body.x, 2000.0,
                           "瞬移之后该落在带头的人身后，实际在 %.0f"
                           % self.bot_conn.body.x)
        self.assertTrue(self.bot_conn.body.on_ground, "瞬移的落点必须站得住")

    def test_the_warp_lands_on_real_ground(self):
        """★ 落点得**掉到地面上**，不能停在半空里。

        跟随点的 y 是带头那个真人报的高度 —— 往后错开 120 之后那一列的
        地面高度完全可能是另一个数。这里直接给一个悬空的落点，
        瞬移之后必须站在 y=150 的地面上。
        """
        terrain = self.leash_room(key="leash_land")
        self.place_bot(200.0)
        self.walk(self.alice, [(3000.0, 150.0), (3200.0, 150.0)])
        self.assertTrue(bot._leash_warp(
            self.room, self.bot_conn, self.bot_seat, terrain,
            (3080.0, 40.0), 2800.0, "单测"))
        self.assertEqual(150.0, self.bot_conn.body.y,
                         "该掉到地面上，实际停在 %.0f" % self.bot_conn.body.y)
        self.assertTrue(self.bot_conn.body.on_ground)

    def test_a_crossable_gap_is_walked_not_warped(self):
        """★ 走得过去就**别**瞬移 —— 这一条是给「瞬移滥用」上的闸。"""
        terrain = self.leash_room(key="leash_walk", width=4000)
        self.place_bot(200.0)
        self.walk(self.alice, [(1600.0, 150.0), (1700.0, 150.0)])
        warped = []
        original = bot._leash_warp
        bot._leash_warp = lambda *a, **k: warped.append(a[4])
        self.addCleanup(setattr, bot, "_leash_warp", original)
        start = self.bot_conn.body.x
        self.beats(20, 1700.0)
        self.assertFalse(warped, "平地上走得到，不该瞬移")
        self.assertGreater(self.bot_conn.body.x, start + 200.0,
                           "该自己一路走过去")

    def test_pvp_rooms_never_grow_a_leash(self):
        """对战房没有「队伍推进」这回事 —— 一个字都不该改。"""
        self.assertEqual(bot.lobby_module.TEAM_LAYOUT_COOP,
                         self.room.team_layout())
        self.room.session_type = 1
        self.room.arguments = (0, 0, 0)
        self.assertNotEqual(bot.lobby_module.TEAM_LAYOUT_COOP,
                            self.room.team_layout())
        terrain = self.install_terrain(synth_terrain("leash_pvp", width=4000))
        self.place_bot(200.0)
        self.walk(self.alice, [(3000.0, 150.0), (3200.0, 150.0)])
        bot._move_intent(self.room, self.bot_conn, self.bot_seat, terrain,
                         None)
        self.assertFalse(self.bot_conn.leash_lagging)

    # -- §141：滞回 + 「追不上就瞬移」 ---------------------------------------
    def test_a_lagging_bot_keeps_chasing_until_half_a_screen(self):
        """★★★ 触发线和释放线是两个数 —— 别钉在线上抖（§141 / D103）。

        实机第四轮：三只 bot 全程钉在触发线上、每 130ms 翻转一次「掉队 /
        归队」。掉队中的 bot 要**追过头一截**（追进释放线）才算归队。
        """
        terrain = self.leash_room(key="leash_hysteresis", width=4000)
        lead = 1600.0
        between = (bot.BOT_COOP_LEASH + bot.BOT_COOP_LEASH_RELEASE) / 2.0
        inside = bot.BOT_COOP_LEASH_RELEASE / 2.0
        self.place_bot(200.0)
        self.walk(self.alice, [(lead - 100.0, 150.0), (lead, 150.0)])
        bot._coop_leash_intent(self.room, self.bot_conn, self.bot_seat,
                               terrain)
        self.assertTrue(self.bot_conn.leash_lagging)
        self.place_bot(lead - between)   # 过了触发线的「里侧」，
        intent = bot._coop_leash_intent(  # 但还没进释放线
            self.room, self.bot_conn, self.bot_seat, terrain)
        self.assertIsNotNone(intent, "落后 %.0f 还没进释放线（%.0f），该接着追"
                                     % (between, bot.BOT_COOP_LEASH_RELEASE))
        self.assertEqual(1, intent[0])
        self.assertTrue(self.bot_conn.leash_lagging)
        self.place_bot(lead - inside)    # 进释放线了，撒手
        self.assertIsNone(bot._coop_leash_intent(
            self.room, self.bot_conn, self.bot_seat, terrain))
        self.assertFalse(self.bot_conn.leash_lagging)

    def test_an_unchasable_lead_ends_in_a_warp(self):
        """★★★ 「他跑满一整屏我还是掉着队」也是「走不过去」（§141）。

        双方极速相同（都是 `FastRunRate` 1.5 倍），奔跑中差距**冻结** ——
        bot 一直在卖力追、`leash_mark` 一直在涨，旧的两条判据（A\* 回空 /
        一步没挪）永远不成立，可差距就是缩不回去。带头的人又跑满一整屏
        还掉着队 = 追不上，瞬移归队（用户明示过可以）。
        """
        terrain = self.leash_room(key="leash_chase", width=6000)
        self.place_bot(200.0)
        self.walk(self.alice, [(1500.0, 150.0), (1600.0, 150.0)])
        bot._coop_leash_intent(self.room, self.bot_conn, self.bot_seat,
                               terrain)
        self.assertTrue(self.bot_conn.leash_lagging)
        anchor = self.bot_conn.leash_anchor
        self.assertGreaterEqual(anchor, 1500.0)
        # bot 也卖力追了 1000+（`leash_mark` 跟着涨，`stuck_by_fact`
        # 不成立）；路是平的（`stuck_by_plan` 不成立）。
        # 他再跑过 anchor+一整屏 —— 能开口的只有「追不上」这一条。
        self.place_bot(1250.0)
        self.bot_conn.leash_mark = 1250.0     # 锁住「一直在往前挪」这个事实
        self.walk(self.alice, [(2600.0, 150.0), (2650.0, 150.0)])
        self.assertGreater(self.bot_conn.body.x, 2400.0,
                           "该瞬移到带头的人身后，实际在 %.0f"
                           % self.bot_conn.body.x)
        self.assertTrue(self.bot_conn.body.on_ground, "落点必须站得住")

    def test_a_chase_within_one_screen_is_still_just_a_chase(self):
        """★ 他还没跑满一整屏就别瞬移 —— 给「瞬移滥用」上的闸。"""
        terrain = self.leash_room(key="leash_chase_gate", width=6000)
        self.place_bot(200.0)
        self.walk(self.alice, [(1500.0, 150.0), (1600.0, 150.0)])
        bot._coop_leash_intent(self.room, self.bot_conn, self.bot_seat,
                               terrain)
        anchor = self.bot_conn.leash_anchor
        # 他只又跑了不到一整屏（< 1024）：这一刻还轮不到瞬移。
        self.place_bot(1250.0)
        self.bot_conn.leash_mark = 1250.0
        self.walk(self.alice, [(1900.0, 150.0), (anchor + 900.0, 150.0)])
        self.assertLess(self.bot_conn.body.x, 1400.0,
                        "没跑满一整屏，不该瞬移（实际在 %.0f）"
                        % self.bot_conn.body.x)


class BotBossRoomTests(TerrainMixin, BotFrameRoom):
    """★★★ boss 房（§141）—— 牵引绳停用、只管打 boss。

    用户 2026-08-30 第四轮实机的两条：
    ① 「bot 进入 boss 房间后，就没必要再有推进进度的限制了，因为
       boss 房间内只需要打 boss，不需要推进进度」；
    ② 顺着 ① 的病根 —— boss 房里 bot 一发不开（boss 不广播坐标，
       见 `BotQuestCombatTests` 里那三条），修好之后走位目标也该换成 boss。
    """

    def setUp(self):
        super().setUp()
        self.terrain = self.install_terrain(
            synth_terrain("boss_room", width=2400))
        self.alice_seat = self.room.seat_index_of(self.alice)

    def human_packet(self, opcode, body):
        return botsync.build_peer_packet(
            self.alice_seat, opcode, body,
            game_id=self.room.epoch_value, sequence=self.next_seq(self.alice))

    def send(self, opcode, body):
        gameserver.Conn.on_game_packet(
            self.alice, OP_PEER_DATA_UP, self.human_packet(opcode, body))

    def ai_message(self, handle, **fields):
        text = "".join("%s=%s\r\n" % (k, v) for k, v in sorted(fields.items()))
        raw = text.encode("ascii")
        self.send(botsync.OP_AI_MSG,
                  struct.pack("<ii", handle, len(raw)) + raw)

    def enter_boss_room(self):
        self.ai_message(1100002, fileName="data/quest/quest03/"
                                        "quest03S6boss.ini", type="start")
        self.assertTrue(self.room.quest.boss_room)

    def spot_boss(self, x=2200.0, y=150.0, handle=1100276):
        """真人打中 boss 一发 —— 命中建表（§141），boss 从此看得见。"""
        self.send(botsync.OP_EXPLODE, botsync.explode_body(
            100002, handle, x, y,
            hit_kind=botsync.HIT_CHARACTER, damage=16.0))
        return handle

    def boss_fires(self, x, y, ammo=3003010):
        """控制者机器替 boss 发的一发 `rpFire`（`body+0 == 20`）——
        枪口坐标就是部件的位置（§141）。弹药用 quest03 的 boss 武器表
        （难度 1 那份 —— id 是关卡内局部的，见 `weapondata.get_quest`）。"""
        weapon = weapondata.get_quest(ammo, 3, 1)
        body = botsync.fire_body(
            self.alice_seat, weapon.id, x, y, 0.0,
            ballistics.power_for_speed(weapon, weapon.velocity))
        body = bytes([bot.MOB_FIRE_SOURCES[0]]) + body[1:]
        self.send(botsync.OP_FIRE, body)

    def test_the_bosss_own_gunfire_marks_where_it_is(self):
        """★★★ boss 替发的 `rpFire` 枪口 = 它的位置（§141）。

        还没建行时存进 `quest.boss_gun` —— 开不了枪（没有句柄），
        但 bot 的走位目标先跟上，不再傻站。
        """
        self.enter_boss_room()
        self.place_bot(300.0)
        intent = bot._boss_fight_intent(self.room, self.bot_conn,
                                        self.bot_seat, self.terrain, None)
        self.assertEqual((0, False, False, False), intent,
                         "什么都不知道时站住")
        self.boss_fires(2200.0, 150.0)
        self.assertEqual((2200.0, 150.0), self.room.quest.boss_gun)
        intent = bot._boss_fight_intent(self.room, self.bot_conn,
                                        self.bot_seat, self.terrain, None)
        self.assertEqual(1, intent[0], "该朝 boss 的枪口走过去")

    def test_gunfire_keeps_a_known_boss_row_fresh(self):
        """★ 建行之后，boss 每开一枪都把**最近的行**挪到枪口 ——
        载具的部件跟着车走，枪口永远是最新的。"""
        self.enter_boss_room()
        self.spot_boss(x=2200.0, y=150.0)          # 真人打中，命中建表
        self.boss_fires(2260.0, 150.0)             # 同一台车上的部件
        self.assertEqual([(2260.0, 150.0, 1100276)], bot.live_mobs(self.room))

    def test_boss_bullets_are_rebuilt_with_the_rooms_difficulty(self):
        """★★★ 关卡武器表带 (关卡, 难度) 查 —— 难度间连弹速都不同
        （Boss-HeadFire 14 → 17），用错难度躲闪预测就歪了（复审抓的）。"""
        self.room.arguments = (3, 1)
        self.room.quest.maps_entered.append("Quest03_6")
        self.enter_boss_room()
        self.place_bot(300.0)
        self.boss_fires(2200.0, 150.0)
        threats = bot._threats_against(self.room, self.bot_conn, self.bot_seat)
        self.assertEqual(1, len(threats), "boss 的子弹重建得出来才躲得开")
        self.assertAlmostEqual(14.0, threats[0].weapon.velocity, places=3)
        # 换难度 3 再来一发：同一把 id，弹速 17。
        self.room.arguments = (3, 3)
        self.boss_fires(2200.0, 150.0)
        threats = bot._threats_against(self.room, self.bot_conn, self.bot_seat)
        self.assertAlmostEqual(17.0, threats[-1].weapon.velocity, places=3)

    def test_a_shared_mob_weapon_takes_the_quest_numbers_first(self):
        """★★★（二次复审）和主表**重号**的小怪武器：闯关里先取当前关卡 /
        难度的覆盖值，离开闯关上下文才退主表。

        reviewer 的复现：Quest03 简单的 `2003010` 弹速 5 / 伤害 6，
        主表那份是 3 / 8 —— 先查主表的话这 8 个重号 id 永远拿错数值。
        """
        self.room.arguments = (3, 1)
        self.room.quest.maps_entered.append("Quest03_6")
        self.enter_boss_room()
        self.place_bot(300.0)
        self.boss_fires(2200.0, 150.0, ammo=2003010)
        threats = bot._threats_against(self.room, self.bot_conn, self.bot_seat)
        self.assertEqual(1, len(threats))
        self.assertAlmostEqual(5.0, threats[0].weapon.velocity, places=3,
                               msg="quest03 简单的 Soldier-Pistol，不是主表的 3")
        # 同一把枪、不在闯关图上（地图名不带 QuestNN）：退主表兜底。
        self.room.quest.maps_entered.append("ZZ_test_terrain")
        self.boss_fires(2200.0, 150.0, ammo=2003010)
        threats = bot._threats_against(self.room, self.bot_conn, self.bot_seat)
        self.assertAlmostEqual(3.0, threats[-1].weapon.velocity, places=3,
                               msg="非闯关上下文退主表那份")

    def test_the_leash_is_suspended_in_a_boss_room(self):
        """★★★ boss 房里落后再远也不追人 —— 没有进度可推（§141）。"""
        self.enter_boss_room()
        self.place_bot(200.0)
        self.walk(self.alice, [(2000.0, 150.0), (2200.0, 150.0)])
        intent = bot._move_intent(self.room, self.bot_conn, self.bot_seat,
                                  self.terrain, None)
        self.assertFalse(self.bot_conn.leash_lagging,
                         "boss 房里牵引绳一个字都不该说")
        self.assertEqual((0, False, False, False), intent,
                         "还不知道 boss 在哪，该站住等，不是去追人")

    def test_it_walks_toward_the_boss_when_out_of_range(self):
        self.enter_boss_room()
        self.spot_boss(x=2200.0)
        self.place_bot(300.0)
        intent = bot._move_intent(self.room, self.bot_conn, self.bot_seat,
                                  self.terrain, None)
        self.assertEqual(1, intent[0], "boss 在射程外，该朝它走过去")
        # 走进射程、弹道也解得开：这一帧 `_fire_target` 有结果，
        # 传给它就是「站住打」—— 判据和真正开火是同一个来源。
        self.place_bot(1300.0)
        target = bot._fire_target(self.room, self.bot_conn, self.bot_seat,
                                  self.bot_conn.weapon)
        self.assertIsNotNone(target, "平地上这个位置该有得打")
        intent = bot._move_intent(self.room, self.bot_conn, self.bot_seat,
                                  self.terrain, target)
        self.assertEqual((0, False, False, False), intent,
                         "打得着 boss 就站住打，不往它身上撞")

    def test_a_boss_behind_cover_is_not_stood_at(self):
        """★★★ 「站住」的判据必须是**真的有得打**，不是「够近 + 看得见」
        （复审抓的：隔着掩体站着，距离再近也永远开不了枪，还站着不动）。

        boss 在射程内、屏幕内，但中间隔一堵通天的墙 —— `_fire_target`
        解不出（弹道被挡），这一帧该**挪过去**，不是站桩。
        """
        terrain = self.install_terrain(synth_terrain(
            "boss_cover", width=2400, walls=((1600, 1800, 0),)))
        self.enter_boss_room()
        self.spot_boss(x=2200.0)
        self.place_bot(1400.0)          # 射程内（800 < 1000）、看得见
        target = bot._fire_target(self.room, self.bot_conn, self.bot_seat,
                                  self.bot_conn.weapon)
        self.assertIsNone(target, "墙挡着弹道，这一发打不出去")
        intent = bot._move_intent(self.room, self.bot_conn, self.bot_seat,
                                  terrain, target)
        self.assertNotEqual((0, False, False, False), intent,
                            "打不到还站住就是永远开不了枪")

    def test_a_non_boss_script_keeps_the_leash(self):
        """★ 分流要准：普通关卡的 `type=start` 不许把牵引绳停掉。"""
        self.ai_message(1100002, fileName="data/quest/quest03/"
                                        "quest03S1Enemy.ini", type="start")
        self.assertFalse(self.room.quest.boss_room)
        self.place_bot(200.0)
        self.walk(self.alice, [(1500.0, 150.0), (1600.0, 150.0)])
        bot._move_intent(self.room, self.bot_conn, self.bot_seat,
                         self.terrain, None)
        self.assertTrue(self.bot_conn.leash_lagging,
                        "普通关卡里落后 1400，牵引绳照旧该管")


#: 一张带出生点的合成图：101（1 队）两个点、102（2 队）两个点，
#: 都挂在地面（y=150）上方，和原版 `.map` 一样（角色是掉下去站住的）。
SPAWN_POINTS = {101: [(200, 40), (300, 40)], 102: [(900, 40), (1000, 40)]}


class BotSpawnPointTests(TerrainMixin, BotFireRoom):
    """★★★ **进图站哪、重生站哪**（§91）—— 和真人同一套规则。

    进图那次是**确定的**（`0x405e1c` -> `0x473cb2`）：

        表  = 组队 ? (队伍 1 -> type 101 / 队伍 2 -> type 102) : 101+102
        名次 = 同队里座位号比我小、且有人的座位数
        点  = 表[名次 % 表长]

    重生那次是**随机的**（`0x4fe832` -> `0x473d2f`，队伍号传 0）：
    101+102 拼起来的全表里抽一个。
    """

    def terrain(self):
        return self.install_terrain(
            synth_terrain("spawn", points=SPAWN_POINTS))

    def test_a_free_for_all_bot_uses_both_lists(self):
        """个人战（`arguments[0] != 1`）走的是 101+102 拼起来那张表。"""
        terrain = self.terrain()
        self.assertEqual(0, bot._spawn_team(self.room, self.bot_seat))
        self.assertEqual([(200, 40), (300, 40), (900, 40), (1000, 40)],
                         bot._spawn_points(terrain, 0))

    def test_the_index_counts_occupied_seats_before_me(self):
        """名次 = 座位号比我小、且**有人**的座位数（`0x405e6f` 那个循环）。"""
        # 夹具：座位 0 = alice、1 = bob、2 = bot（`bot_seat`）。
        self.assertEqual(2, bot._spawn_index(self.room, self.bot_seat))
        self.assertEqual(0, bot._spawn_index(self.room, 0))
        self.assertEqual(1, bot._spawn_index(self.room, 1))

    def test_the_bot_spawns_on_a_map_point_not_next_to_the_human(self):
        """★★★ 用户 2026-08-28 报的那条：bot 不该出生在真人身边。"""
        self.terrain()
        self.walk(self.alice, [(700.0, 150.0)])
        self.assertIsNotNone(self.bot_conn.body)
        # 名次 2 -> 全表第 3 个点 (900, 40)，掉到地面 y=150。
        self.assertEqual(900.0, self.bot_conn.body.x)
        self.assertEqual(150.0, self.bot_conn.body.y)
        self.assertTrue(self.bot_conn.body.on_ground)

    def test_a_map_without_spawn_points_falls_back_to_the_trail(self):
        """★ 没有出生点对象的图退回 D16 —— 总比杵在地形里强。

        ⚠ 这条路要**两帧**：第一帧 `battle_pos` 还是 `None`，只能先走
        `trail_point()` 把锚定下来，第二帧才接管。有出生点的图不用等。
        """
        self.install_terrain(synth_terrain("flat"))
        # ★ D106 之后一「帧」= 4 格，所以这里逐**格**看：第一格
        #   `battle_pos` 还是 None，只能先走 `trail_point()` 把锚定下来。
        self.human_heartbeat(self.alice, 700.0, 150.0, ticks=1)
        self.assertIsNone(self.bot_conn.body)
        self.advance(1)
        self.assertIsNotNone(self.bot_conn.body)
        self.assertEqual(700.0, self.bot_conn.body.x)

    def test_the_respawn_point_is_drawn_from_the_whole_table(self):
        """重生**不看队伍**：全表随机抽一个（68 组语料两类点都落过）。"""
        terrain = self.terrain()
        picked = [bot.respawn_point(self.room, self.bot_seat, terrain,
                                    roll=lambda n, i=i: i % n)
                  for i in range(4)]
        self.assertEqual([(200.0, 40.0), (300.0, 40.0),
                          (900.0, 40.0), (1000.0, 40.0)], picked)

    def test_picking_a_respawn_point_settles_it_and_arms_the_bot(self):
        """★ 挑点这一发要**同时**做两件事：落到地面 + 记进 `pending_spawn`。"""
        self.terrain()
        self.bot_conn.roll = lambda n: 3
        point = bot.pick_respawn_point(self.room, self.bot_seat)
        self.assertEqual((1000.0, 150.0), point)
        self.assertEqual((1000.0, 150.0), self.bot_conn.pending_spawn)

    def test_the_next_frame_moves_the_body_to_the_respawn_point(self):
        """★★ 站起来那一帧身体要跟着搬 —— 不搬就被心跳拽回死亡地点。"""
        self.terrain()
        self.walk(self.alice, [(700.0, 150.0)])
        self.bot_conn.roll = lambda n: 0
        bot.pick_respawn_point(self.room, self.bot_seat)
        self.walk(self.alice, [(700.0, 150.0)])
        self.assertIsNone(self.bot_conn.pending_spawn)
        self.assertEqual((200.0, 150.0), self.bot_conn.battle_pos)
        last = bot_frames(self.alice, self.bot_seat)[-1]
        self.assertEqual((200, 150), udpsync.heartbeat_position(last))

    def test_the_watchdog_sends_the_map_point_not_the_humans(self):
        """★★ 端到端：看门狗补的那发 `0x0419` 填的是**地图出生点**。

        旧行为是 `respawn_point_for()` —— 那本账里只有真人客户端自报过的
        重生点，bot 借来用就是「在房主刚重生的地方站起来」。
        """
        self.terrain()
        self.walk(self.alice, [(700.0, 150.0)])
        self.bot_conn.roll = lambda n: 2                # -> (900, 40)
        quest = self.room.quest
        # 真人上一次在 (33, 44) 重生过 —— 旧代码会借这个点。
        quest.remember_respawn_point(self.room.seat_index_of(self.alice),
                                     33, 44)
        armed = time.monotonic()
        quest.arm_respawn_watchdog(self.bot_seat, (700.0, 150.0), now=armed,
                                   after=1.0)
        self.clear()
        gameserver.Conn.check_respawn_watchdog(self.alice, now=armed + 2.0)
        self.assertIn(gameserver.OP_RESPAWN_CHARACTER, opcodes(self.alice))
        self.assertEqual((900.0, 150.0), self.bot_conn.pending_spawn)

    def test_a_dead_human_no_longer_drags_the_bot_over(self):
        """★★★ 用户 2026-08-28 报的「我一死 bot 就瞬移过来然后抽搐」。

        真人一死，`_hostile_targets()` 就空了 —— 旧代码在那一刻退回
        `trail_point(真人轨迹)`，bot 被拽到他身边。

        ★ 会话 41 起 bot **不再是站着不动**（用户 2026-08-30：「敌人死后
        bot 就停下不动了，我希望它会自己走位」）—— 它会朝敌方出生点挪。
        所以这条要钉的两件事变成：**不会被拽到真人身上**、
        **每帧最多挪一步**（不瞬移）。
        """
        self.terrain()
        self.walk(self.alice, [(700.0, 150.0)])
        here = self.bot_conn.battle_pos
        self.assertIsNotNone(here)
        # 真人躺下（服务端上闩 = `_lying_dead` 为真）
        self.room.quest.arm_respawn_watchdog(
            self.room.seat_index_of(self.alice), (700.0, 150.0), after=5.0)
        self.assertEqual([], bot._hostile_targets(self.room, self.bot_seat))
        previous = here
        for _ in range(4):
            self.walk(self.alice, [(700.0, 150.0)])
            now = self.bot_conn.battle_pos
            self.assertNotEqual((700.0, 150.0), now, "不许被拽到真人身上")
            step = math.hypot(now[0] - previous[0], now[1] - previous[1])
            self.assertLess(step, 200.0, f"一帧挪了 {step:.0f}，这是瞬移")
            previous = now


class BotCoopFrontRespawnTests(TerrainMixin, BotFrameRoom):
    """任务模式的 bot 复活在推进最靠前真人附近的有效站立面。"""

    def setUp(self):
        super().setUp()
        self.terrain = self.install_terrain(synth_terrain("coop_respawn",
                                                          width=2200))
        self.walk(self.alice, [(1400.0, 150.0)])
        self.walk(self.bob, [(700.0, 150.0)])

    def test_it_anchors_to_the_frontmost_human(self):
        point = bot.pick_respawn_point(self.room, self.bot_seat)
        self.assertIsNotNone(point)
        self.assertLess(abs(point[0] - 1400.0), 100.0)
        self.assertGreater(abs(point[0] - 700.0), 500.0)

    def test_the_point_is_standable_and_does_not_overlap_the_leader(self):
        point = bot.pick_respawn_point(self.room, self.bot_seat)
        who = bot._character_of(self.bot_conn)
        self.assertTrue(botmove.fits(self.terrain, point[0], point[1], who))
        self.assertFalse(bot._spawn_overlaps(
            self.room, self.bot_seat, point, who))
        self.assertEqual(point, self.bot_conn.pending_spawn)


class BotCoopRespawnScanTests(TerrainMixin, BotFrameRoom):
    """★★★ 复活点扫描跑在**游戏主线程**上，只许看锚点附近那一小片。"""

    def setUp(self):
        super().setUp()
        # 现有最宽的一张任务图：11400 列 / 15029 个站立面。
        self.terrain = self.install_terrain(mapdata.load("Quest03_1"))

    def naive(self, anchor):
        """朴素的全图逐列扫 —— 发散扫必须给出和它一模一样的答案。"""
        who = bot._character_of(self.bot_conn)
        ax, ay = anchor
        best = None
        for x in range(self.terrain.width):
            for y in self.terrain.surfaces(x):
                point = (float(x), float(y))
                dx, dy = point[0] - ax, point[1] - ay
                rank = (dx * dx + dy * dy, abs(dy), abs(dx), point[0], point[1])
                if best is not None and rank >= best[0]:
                    continue
                if not botmove.fits(self.terrain, point[0], point[1], who):
                    continue
                if bot._spawn_overlaps(self.room, self.bot_seat, point, who):
                    continue
                best = (rank, point)
        return None if best is None else best[1]

    def test_it_matches_a_full_scan(self):
        for anchor in ((200.0, 400.0), (6000.0, 400.0), (11000.0, 400.0)):
            self.assertEqual(
                self.naive(anchor),
                bot._nearest_front_spawn(self.room, self.bot_seat,
                                         self.terrain, self.bot_conn, anchor),
                f"锚点 {anchor}")

    def test_the_columns_come_out_nearest_first(self):
        """发散扫能提前 `break` 的**前提**：产出按 |x − center| 单调不减。"""
        got = list(bot._columns_by_distance(3.4, 9))
        self.assertEqual(sorted(range(9), key=lambda x: abs(x - 3.4)), got)
        self.assertEqual(list(range(9)), sorted(got), "一列都不能漏")

    def test_it_does_not_scan_the_whole_map(self):
        """★ 判据是 `botmove.fits()` 被调了几次，不是挂钟。

        从 x=0 往右扫的旧写法在越过锚点之前 `best` 一路都在改善、剪枝一格
        都剪不掉：这张图上实测一次调用 **99.5 ms、`fits()` 5982 次**，
        等于一次复活吞掉三格房间循环（§137 把 A* 挪去后台就是治这个病）。
        """
        real = botmove.fits
        calls = []

        def counted(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        botmove.fits = counted
        try:
            bot._nearest_front_spawn(self.room, self.bot_seat, self.terrain,
                                     self.bot_conn, (6000.0, 400.0))
        finally:
            botmove.fits = real
        self.assertLess(len(calls), 100, "只该看锚点附近那一小片")


class BotCoopRespawnCrowdingTests(TerrainMixin, TwoBotFrameRoom):
    """同一格到期的两个 bot 不会挑中同一个前线空位。"""

    def test_pending_spawn_reserves_the_first_bots_point(self):
        self.install_terrain(synth_terrain("coop_respawn_two", width=2200))
        self.walk(self.alice, [(1400.0, 150.0)])
        points = [bot.pick_respawn_point(self.room, seat)
                  for seat in self.bot_seats]
        self.assertNotEqual(points[0], points[1])
        first = self.room.seats[self.bot_seats[0]].conn
        second = self.room.seats[self.bot_seats[1]].conn
        circles_a = bot._character_of(first).circles(*points[0], False)
        circles_b = bot._character_of(second).circles(*points[1], False)
        self.assertTrue(all(math.hypot(ax - bx, ay - by) >= ar + br
                            for ax, ay, ar, _ in circles_a
                            for bx, by, br, _ in circles_b))


class BotBreakableShortcutTests(TerrainMixin, BotFireRoom):
    """截图真图 `CamelCulvert04:NewPvp`：绕路和打罐子的捷径同时存在。"""

    def setUp(self):
        super().setUp()
        self.terrain = self.install_terrain(mapdata.load("CamelCulvert04"))
        self.place_bot(353.0, 988.0)

    def test_the_background_planner_selects_the_first_vase_on_the_shortcut(self):
        who = bot._character_of(self.bot_conn)
        goal = (1605.0, 937.0)
        botplan.forget(self.bot_conn)
        botplan.ask(self.bot_conn, self.terrain, self.bot_conn.body, who, goal,
                    open_terrain=self.terrain.variant(()))
        self.assertTrue(botplan.PLANNER.settle())
        choice = botplan.take_result(self.bot_conn, self.bot_conn.body, goal)
        self.assertTrue(choice.shortcut)
        self.assertTrue(choice.reached)
        self.assertEqual(55, choice.blocker)
        self.assertEqual(1, len(choice.prefix))
        self.assertEqual((526, 840),
                         (self.terrain.breakables[choice.blocker].x,
                          self.terrain.breakables[choice.blocker].y))

    def test_it_switches_to_a_weapon_that_really_hits_the_vase(self):
        self.place_bot(493.0, 988.0)
        self.bot_conn.path_breakable = 55
        with bot._tick_clock(self.now()):
            bot._choose_weapon(self.room, self.bot_conn, self.bot_seat)
            target = bot._fire_target(self.room, self.bot_conn, self.bot_seat,
                                      self.bot_conn.weapon)
        self.assertEqual(1002030, self.bot_conn.weapon.id)
        self.assertIsNotNone(target)
        self.assertEqual(bot.BREAKABLE_SEAT, target[0])
        option = bot._breakable_option(
            self.room, self.bot_conn, self.bot_conn.weapon, self.terrain,
            bot._solver_for(self.bot_conn.weapon), 493.0, 988.0)
        self.assertEqual(20, option[1])       # 40 HP，两发打碎

    def test_breaking_it_clears_the_objective_and_replans(self):
        self.bot_conn.path_breakable = 55
        self.room.quest.bot_terrain = self.terrain
        ledger = bot._breakables(self.room)
        self.assertTrue(ledger.damage(self.terrain, 55, 999))
        bot._refresh_breakables(self.room)
        self.assertIsNone(self.bot_conn.path_breakable)
        self.assertNotIn(55, bot._terrain(self.room).alive)

    def test_a_breakable_flip_does_not_start_a_preheat(self):
        """★★★★★ 战斗中翻罐子**不许**再整图预热（V0.3 §163 / D124）。

        它和 `botplan` 那条线程做的是同一份活（`Esperan03` 实测 981 vs
        931 ms），却每翻一次就抢着多做几遍 —— 代价是占住 GIL、饿死每条连接
        那条发送线程：下行积压 1~2.5 秒，`rpExplode` 迟到就被收方静默丢弃，
        句柄从此永久错开（§42），表现是「bot 打我没有伤害」。
        """
        jobs = []
        real = bot._submit_warm
        bot._submit_warm = jobs.append
        self.addCleanup(setattr, bot, "_submit_warm", real)
        self.room.quest.bot_terrain = self.terrain
        ledger = bot._breakables(self.room)
        self.assertTrue(ledger.damage(self.terrain, 55, 999))
        bot._refresh_breakables(self.room)
        self.assertEqual([], jobs, "翻罐子不该再排预热活儿")
        # 开局那一次照旧要预热 —— 那时候 CPU 是空着的。
        bot.warm_navigation(self.room, "开局")
        self.assertTrue(jobs, "开局 / 换图仍然要预热")

    def test_being_pinned_inside_a_breakable_beats_crack_unsticking(self):
        """★ 被罐子裹住时锁定它、瞄上之后站住打，而不是在缝里乱蹦。

        判据是两句 `fits()` 的地形差（`_breakable_pinning_body`）：这儿塞
        不下、拿掉破坏物就塞得下 ⇒ 压住我的是它。第一格只锁定并照旧走
        `_unstick_intent()`（挪出去也是一条出路），瞄上它的那一格才站住。
        """
        item = self.terrain.breakables[55]
        self.place_bot(float(item.x), float(item.y))
        self.bot_conn.path_breakable = None
        self.assertIsNotNone(
            bot._breakable_pinning_body(self.bot_conn, self.terrain))
        bot._move_intent(self.room, self.bot_conn, self.bot_seat,
                         self.terrain, None)
        self.assertEqual(item.index, self.bot_conn.path_breakable)
        target = (bot.BREAKABLE_SEAT, (float(item.x), float(item.y)), None)
        self.assertEqual((0, False, False, False),
                         bot._move_intent(self.room, self.bot_conn,
                                          self.bot_seat, self.terrain, target))

    def test_a_visible_enemy_outranks_the_vase(self):
        """★★ 人在眼前时不打罐子、也不为罐子换枪 —— 三处同一个门。

        走位（`_breakable_move_intent`）、开火（`_engagement`）、换枪
        （`_choose_weapon`）全走 `_breaking_now()`。判得不一样就会出现
        「走位站着等打罐子、开火却去打别的」那种谁也动不了的僵局。
        """
        self.place_bot(493.0, 988.0)
        self.bot_conn.path_breakable = 55
        self.assertIsNotNone(bot._breaking_now(self.room, self.bot_conn,
                                               self.bot_seat, self.terrain))
        self.walk(self.alice, [(560.0, 988.0)])
        self.assertIsNone(bot._breaking_now(self.room, self.bot_conn,
                                            self.bot_seat, self.terrain))
        self.assertIsNone(bot._breakable_move_intent(
            self.room, self.bot_conn, self.bot_seat, self.terrain, None))

    def test_an_exhausted_prefix_does_not_freeze_ordinary_walking(self):
        """★★★★★ 锁着挡路物、安全前缀又走空 —— 正常走位不许被钉住（§160）。

        用户 2026-09-01 22:53 实机：整张图的 bot 一起不动，一分钟后又一起
        恢复。病根就是 `_route_intent()` 里那句「前缀走完 = 站住打挡路物」
        对**所有**调用者生效，而且排在规划分支前面：一旦锁上，这个 bot
        每一帧都回「不动」，**连规划单都不再递**，只有挡路物自己翻转
        （`_refresh_breakables` 一次清全房间）才放得开。

        「站住」只属于破障那个调用者 —— 它前面有 `_breaking_now()` 那道门。
        """
        self.place_bot(493.0, 988.0)
        who = bot._character_of(self.bot_conn)
        goal = (1605.0, 937.0)
        self.bot_conn.path_breakable = 55
        self.bot_conn.path_breakable_prefix = []
        self.bot_conn.nav_path = []
        self.bot_conn.nav_goal = None      # `_clear_navigation()` 之后的样子
        routed = bot._route_intent(self.bot_conn, self.terrain, who, goal)
        self.assertNotEqual((0, False, False, False), routed)
        held = bot._route_intent(self.bot_conn, self.terrain, who, goal,
                                 hold_at_breakable=True)
        self.assertEqual((0, False, False, False), held)

    def test_a_latched_blocker_still_lets_the_bot_walk_towards_a_target(self):
        """★★★★★ 同一件事走到 `_move_intent()` 这一层：腿要迈得出去。

        `_breakable_move_intent()` 自己的注释写着「安全前缀走完了、这一枪
        又不是冲着它去的 —— **不站在那儿干等**，放行给正常走位」。放行之后
        走的就是 `_walk_to()` -> `_route_intent()`，而那一句把它又钉回去了。
        """
        self.place_bot(493.0, 988.0)
        self.walk(self.alice, [(900.0, 700.0)])
        self.place_bot(493.0, 988.0)
        self.bot_conn.path_breakable = 55
        self.bot_conn.path_breakable_prefix = []
        self.bot_conn.nav_path = []
        self.bot_conn.nav_goal = None
        with bot._tick_clock(self.now()):
            intent = bot._move_intent(self.room, self.bot_conn, self.bot_seat,
                                      self.terrain, None)
        self.assertNotEqual((0, False, False, False), intent)
        self.assertEqual(55, self.bot_conn.path_breakable)   # 锁本身还留着

    def test_dodging_outranks_breaking_a_blocker(self):
        """★★★ 脱困 / 牵引绳 / 躲子弹都排在破障**前面**。

        锁住一件挡路物之后连躲都不躲的话，手雷飞脸上 bot 也站着不动
        （§152「卡在缝里排在一切之前」/ D99「牵引绳排在最前面」/
        M5-E「躲子弹排在一切之前」三条会被同时绕过）。罐子不会跑，
        躲完这一发再打完全来得及。
        """
        self.place_bot(493.0, 988.0)
        self.bot_conn.path_breakable = 55
        target = (bot.BREAKABLE_SEAT, (526.0, 840.0), None)
        self.assertEqual((0, False, False, False),
                         bot._move_intent(self.room, self.bot_conn,
                                          self.bot_seat, self.terrain, target))
        real = bot._dodge_intent
        bot._dodge_intent = lambda *args, **kwargs: (-1, True, False, True)
        try:
            self.assertEqual((-1, True, False, True),
                             bot._move_intent(self.room, self.bot_conn,
                                              self.bot_seat, self.terrain,
                                              target))
        finally:
            bot._dodge_intent = real

    def test_an_unidentified_blocker_falls_back_to_the_intact_route(self):
        """★★★ 认不出挡路的是**哪一件**时，绝不能把开放地形那条路交出去。

        那条路是「假定罐子都碎了」画出来的，拿到真地形上执行就是一路撞墙。
        这种时候当作「没有捷径」，退回完整地形那条已经验证过的答案。
        """
        who = bot._character_of(self.bot_conn)
        goal = (1605.0, 937.0)
        intact = botnav.plan_result(self.terrain, self.bot_conn.body, who, goal)
        real = botnav.first_breakable_on_path
        botnav.first_breakable_on_path = lambda *args, **kwargs: (None, ())
        try:
            botplan.forget(self.bot_conn)
            botplan.ask(self.bot_conn, self.terrain, self.bot_conn.body, who,
                        goal, open_terrain=self.terrain.variant(()))
            self.assertTrue(botplan.PLANNER.settle())
            choice = botplan.take_result(self.bot_conn, self.bot_conn.body,
                                         goal)
        finally:
            botnav.first_breakable_on_path = real
        self.assertFalse(choice.shortcut)
        self.assertIsNone(choice.blocker)
        self.assertEqual(intact.path, choice.path)

    def test_standing_beside_a_vase_is_not_being_pinned(self):
        """★★ 站在罐子**旁边**不算被困住。

        `Breakable.hit()` 抄的是客户端 `HitTest`，自带 3×3 邻域 —— 拿碰撞圆
        的边缘点去问它，「贴着罐子站」和「被罐子裹住」返回同一个答案：实测
        这张图 3514 个可站落脚点里 **571 个（16.2%）**会被这么误判，bot 于是
        站住打一件根本不挡路的罐子。`(156, 990)` 就是其中一个 —— 身圆右缘
        `(169, 953)` 落在罐 #10 的掩码里，可人明明站得好好的。
        """
        self.place_bot(156.0, 990.0)
        self.bot_conn.path_breakable = None
        who = bot._character_of(self.bot_conn)
        self.assertTrue(botmove.fits(self.terrain, 156.0, 990.0, who))
        self.assertIsNone(
            bot._breakable_pinning_body(self.bot_conn, self.terrain))
        bot._move_intent(self.room, self.bot_conn, self.bot_seat,
                         self.terrain, None)
        self.assertIsNone(self.bot_conn.path_breakable)


class BotKnockbackLadderTests(unittest.TestCase):
    """★★★ 击退强度阶梯 + 方向公式（§92，`0x481003`）。

    语料 13160 发 `rpSplashDamaged` 逐档对得死：伤害 1~9 恒 4.0（5637 发）、
    10~19 恒 8.0（3908 发）、20 起 15.0。
    """

    def test_the_ladder_matches_the_five_fcom_steps(self):
        for damage, want in ((0, 4.0), (1, 4.0), (9, 4.0),
                             (10, 8.0), (19, 8.0),
                             (20, 15.0), (39, 15.0),
                             (40, 20.0), (79, 20.0),
                             (80, 40.0), (159, 40.0),
                             (160, 1.0), (999, 1.0)):
            self.assertEqual(want, bot.knockback_strength(damage),
                             f"伤害 {damage}")

    def test_the_vector_is_lifted_by_zero_point_seven(self):
        """★ 归一化 -> `y −= 0.7` -> 再归一化 -> ×强度（`0x481017`）。"""
        push = bot.knockback_vector(1.0, 0.0, 5)        # 正右方飞来
        want = math.hypot(1.0, -0.7)
        self.assertAlmostEqual(4.0 * 1.0 / want, push[0], places=4)
        self.assertAlmostEqual(4.0 * -0.7 / want, push[1], places=4)
        self.assertAlmostEqual(4.0, math.hypot(*push), places=4)

    def test_a_zero_direction_throws_straight_up(self):
        """★ 来向是零向量时原版把 `dy` 强置 −1（`0x485805`）。"""
        push = bot.knockback_vector(0.0, 0.0, 25)
        self.assertAlmostEqual(0.0, push[0], places=4)
        self.assertAlmostEqual(-15.0, push[1], places=4)

    def test_the_length_is_always_the_step_strength(self):
        for dx, dy in ((3.0, 4.0), (-7.0, 0.0), (0.0, 9.0), (1.0, -1.0)):
            for damage, want in ((5, 4.0), (12, 8.0), (30, 15.0)):
                self.assertAlmostEqual(
                    want, math.hypot(*bot.knockback_vector(dx, dy, damage)),
                    places=4, msg=f"({dx},{dy}) 伤害 {damage}")


class BotDealsKnockbackTests(BotFireRoom):
    """★★ bot 打人时 `rpSplashDamaged +13/+17` 得**填上**击退矢量（§92）。

    原来那三处（溅射 / 地面燃烧 / 近身）全填的 0，于是「真人的手雷把人顶飞、
    bot 的同一颗一动不动」。
    """

    def test_the_splash_packet_carries_a_push(self):
        weapon = weapondata.get(1000020)
        shell = bot.Shell(1, 0, weapon, 9, 0.0, 0.0,
                          ballistics.launch(weapon, 0.0, 30.0), 0.0, 40)
        character = chrprops.get(0)
        lift = 2.0 * character.size_legs + character.size_body
        bodies = [(0, 30.0, lift, False, 0)]        # 身体圆心在 (30, 0)
        hits = bot._splash_targets(self.room, shell, (0.0, 0.0), None, bodies)
        self.assertEqual(1, len(hits))
        _seat, damage, _where, push = hits[0]
        # 强度按**没乘模式倍率**的那一档查（§92）。
        base = damage // bot._damage_scale(self.room)
        self.assertAlmostEqual(bot.knockback_strength(base),
                               math.hypot(*push), places=4)
        self.assertLess(push[1], 0.0, "★ 击退永远带一点向上的分量")

    def test_the_fire_and_dash_pushes_are_the_hard_coded_ones(self):
        """★ 火 `(0, −8)`（语料 1164 发）、近身 `(±15, −10)`（1347 发）。"""
        self.assertEqual((0.0, -8.0), bot.FIRE_KNOCKBACK)
        self.assertEqual((15.0, -10.0), bot.DASH_KNOCKBACK)


class HumanShotRoom(TerrainMixin, BotFireRoom):
    """平地 + 一个站在 `(600, 150)` 的 bot，外加「让真人发一发同步包」的手脚架。

    ★ 抽出来是为了让击退 / 血量台账 / 闪避三批用例共用它，
      而**不用互相继承** —— 继承会把上一批的用例整个再跑一遍。
    """

    def setUp(self):
        super().setUp()
        self.install_terrain(synth_terrain("flat"))
        self.place_bot(600.0)
        self.alice_seat = self.room.seat_index_of(self.alice)

    def human_packet(self, opcode, body):
        return botsync.build_peer_packet(
            self.alice_seat, opcode, body,
            game_id=self.room.epoch_value, sequence=self.next_seq(self.alice))

    def send(self, opcode, body):
        gameserver.Conn.on_game_packet(
            self.alice, OP_PEER_DATA_UP, self.human_packet(opcode, body))

    def splash(self, damage, push):
        self.send(botsync.OP_SPLASH_DAMAGED, botsync.splash_body(
            100002, botsync.character_handle(self.bot_seat), damage,
            600.0, 150.0, push_x=push[0], push_y=push[1]))


class BotTakesKnockbackTests(HumanShotRoom):
    """★★★ **bot 挨打也要被顶飞**（§92）—— 用户 2026-08-28 报的那条。

    bot 的位置是服务端说了算的：客户端各自把它的模型顶飞，服务端这边不动
    的话下一发心跳当场拽回去，看着就是「只是原地跳一下」。
    """

    def test_a_human_splash_pushes_the_bot_off_the_ground(self):
        self.assertTrue(self.bot_conn.body.on_ground)
        self.splash(20, (12.0, -9.0))
        body = self.bot_conn.body
        self.assertFalse(body.on_ground, "甲档一定离地（0x50f947）")
        self.assertAlmostEqual(12.0, body.vx, places=4)
        self.assertAlmostEqual(-9.0, body.vy, places=4)

    def test_exactly_ten_damage_only_clamps_the_upward_speed(self):
        """★ `伤害 > 10` 才真给速度；正好 10 只把 `v.y` 夹到 −10（`0x50f8a2`）。"""
        self.splash(10, (12.0, -9.0))
        body = self.bot_conn.body
        self.assertFalse(body.on_ground)
        self.assertAlmostEqual(0.0, body.vx, places=4)
        self.assertAlmostEqual(bot.KNOCKBACK_MIN_LIFT, body.vy, places=4)

    def test_a_weak_hit_only_slides_along_the_ground(self):
        """★ 乙档（伤害 < 10）在地上时**不离地**，只横着滑 `push.x × 3`。"""
        before = self.bot_conn.body.x
        self.splash(6, (4.0, -1.0))
        body = self.bot_conn.body
        self.assertTrue(body.on_ground, "乙档不离地")
        self.assertAlmostEqual(before + 4.0 * bot.KNOCKBACK_SLIDE, body.x,
                               places=4)

    def test_a_weak_hit_in_the_air_pushes_nothing_at_all(self):
        """★ 伤害 < 10 且**腾空中**走的是甲档，而甲档里「<= 10」那一路
        要求「在地上且 v.x == 0」—— 两条都不成立 ⇒ 速度一点不动（§92 修正）。
        """
        self.bot_conn.body = bot.botmove.Body(600.0, 100.0, 1.0, -2.0,
                                              on_ground=False)
        self.splash(6, (4.0, -1.0))
        body = self.bot_conn.body
        self.assertAlmostEqual(1.0, body.vx, places=4)
        self.assertAlmostEqual(-2.0, body.vy, places=4)
        self.assertFalse(body.on_ground)

    def test_a_direct_hit_uses_the_velocity_reconstructed_from_the_fire(self):
        """★★ `rpExplode` **不带**击退矢量 —— 服务端按 `rpFire` 反推（§92）。"""
        weapon = weapondata.get(1000030)             # T1 狙击，直射弹
        self.send(botsync.OP_FIRE, botsync.fire_body(
            self.alice_seat, weapon.id, 0.0, 150.0, 0.0,
            ballistics.power_for_speed(weapon, weapon.velocity)))
        self.send(botsync.OP_EXPLODE, botsync.explode_body(
            100002, botsync.character_handle(self.bot_seat), 600.0, 150.0,
            hit_kind=botsync.HIT_CHARACTER, damage=20.0))
        body = self.bot_conn.body
        self.assertFalse(body.on_ground)
        # 正右方直飞过来 -> 击退是「右上」，长度按伤害 20 那一档 = 15。
        self.assertGreater(body.vx, 0.0)
        self.assertLess(body.vy, 0.0)
        self.assertAlmostEqual(15.0, math.hypot(body.vx, body.vy), places=3)

    def test_an_unmatched_explode_leaves_the_bot_alone(self):
        """★ 配不上开火记录就**不给击退** —— 宁可少顶一下，也不要乱甩。"""
        self.send(botsync.OP_EXPLODE, botsync.explode_body(
            100002, botsync.character_handle(self.bot_seat), 600.0, 150.0,
            hit_kind=botsync.HIT_CHARACTER, damage=20.0))
        self.assertTrue(self.bot_conn.body.on_ground)

    def test_the_bot_really_flies_away_over_the_next_frames(self):
        """★★★ 用户 2026-08-28 第二轮报的那条：**打 bot 它得真的飞出去**。

        光把速度写进 `machine.body` 不够 —— 下一帧 `_own_step()` 还要把它
        推出去。以前 `botmove._air_tick()` 会拿「朝着真人的方向键」把腾空
        的水平速度覆写成 `走速 × 1.5`（§93 证明那条是错的），于是 bot
        **朝开枪的人飘过去**，看着就是「原地跳一下」。
        """
        # ★ 挑不到目标 ⇒ `_move_intent()` 会一路朝真人按方向键 —— 这正是
        #   出问题的那一种（挑得到目标时它返回「站住」，键是 0，撞不上）。
        original = bot._fire_target
        bot._fire_target = lambda *args, **kwargs: None
        self.addCleanup(setattr, bot, "_fire_target", original)
        before = self.bot_conn.body.x
        self.splash(30, (12.0, -9.0))    # 真人在左边，击退朝右
        self.beats(4, 100.0)             # 真人站在 x=100，bot 走 4 帧
        self.assertGreater(self.bot_conn.battle_pos[0], before + 100.0,
                           "击退该把 bot 往**远离**真人的方向甩出去")

    def test_a_hit_on_someone_else_is_ignored(self):
        self.send(botsync.OP_SPLASH_DAMAGED, botsync.splash_body(
            100002, botsync.character_handle(self.alice_seat), 30,
            600.0, 150.0, push_x=12.0, push_y=-9.0))
        self.assertTrue(self.bot_conn.body.on_ground)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class BotHomingTests(BotFireRoom):
    """★★ **追踪火箭**（`ch01-03`，`HomingRange=200 HomingAngle=30`，§77）。

    用户 2026-08-28：「角色 2 的 3 号武器，总感觉飞行轨迹和最终爆炸动画的
    地点不一样，看着火箭飞着撞墙了，却在墙的附近另一处出现了爆炸动画。」

    根因是服务端把它当直射弹算，而收方锁上目标之后每 tick 把速度矢量朝
    目标转 `HomingAngle / 7` 度 —— 客户端逐帧日志实测**恒 4.286°**。
    """

    def rocket(self):
        self.room.seats[self.bot_seat].character_id = 1
        self.bot_conn.character_id = 1
        self.bot_conn.weapon_slot = 3
        self.bot_conn.declared_weapon = None
        weapon = self.bot_conn.weapon
        self.assertEqual(1001030, weapon.id)
        return weapon

    def shell(self, weapon, x, y, angle):
        shot = ballistics.Shot(angle, 1.0, weapon.velocity, 0.0,
                               ballistics.gravity_per_tick(weapon),
                               ballistics.accel_per_tick(weapon),
                               weapon.max_velocity or 0.0)
        return bot.Shell(1, 0, weapon, 9, x, y, shot, 0.0, 200)

    def test_the_turn_rate_is_the_homing_angle_over_seven(self):
        """★ `0x47e53a` 的 `fmul [0x693c34]`（= 1/7）+ 客户端实测 4.286°。"""
        weapon = self.rocket()
        self.assertEqual(30.0, weapon.homing_angle)
        shell = self.shell(weapon, 0.0, 0.0, 0.0)          # 朝右直飞
        bodies = [(0, 0.0, -150.0, False, 0)]              # 正上方，射程内
        before = math.atan2(shell.vy, shell.vx)
        bot._homing_step(self.room, shell, bodies)
        turned = math.degrees(abs(math.atan2(shell.vy, shell.vx) - before))
        self.assertAlmostEqual(30.0 / 7.0, turned, places=3)

    def test_it_does_not_lock_beyond_the_homing_range(self):
        weapon = self.rocket()
        shell = self.shell(weapon, 0.0, 0.0, 0.0)
        bodies = [(0, 0.0, -900.0, False, 0)]              # 远在射程外
        bot._homing_step(self.room, shell, bodies)
        self.assertIsNone(shell.locked)
        self.assertAlmostEqual(0.0, shell.vy, places=6)

    def test_once_locked_it_stays_locked(self):
        """★ 原版 `[proj+0x328]` 一旦不是 −1 就不再选目标（`0x47e36b`）。"""
        weapon = self.rocket()
        shell = self.shell(weapon, 0.0, 0.0, 0.0)
        bot._homing_step(self.room, shell, [(3, 0.0, -100.0, False, 0)])
        self.assertEqual(3, shell.locked)
        # 换一个更近的进来也不该改锁。
        bot._homing_step(self.room, shell, [(3, 0.0, -100.0, False, 0),
                                            (4, 10.0, -10.0, False, 0)])
        self.assertEqual(3, shell.locked)

    def test_the_speed_never_changes_only_the_direction(self):
        weapon = self.rocket()
        shell = self.shell(weapon, 0.0, 0.0, 0.0)
        bodies = [(0, -50.0, -120.0, False, 0)]
        for _ in range(6):
            bot._homing_step(self.room, shell, bodies)
            self.assertAlmostEqual(weapon.velocity,
                                   math.hypot(shell.vx, shell.vy), places=3)

    def test_a_plain_rocket_flies_straight(self):
        """★ 别改坏了：不带 `HomingAngle` 的武器一点不该拐弯。"""
        weapon = weapondata.get(1002030)                   # ch02-03 바주카
        self.assertEqual(0.0, weapon.homing_angle)
        shell = self.shell(weapon, 0.0, 0.0, 0.0)
        for tick in range(1, 5):
            want = ballistics.position_at(0.0, 0.0, shell.shot, tick)
            got = shell.position(tick)
            self.assertAlmostEqual(want[0], got[0], places=6)

    # -- ★★ 会话 24：目标跑出射程就**永久**不再追（§80）--------------------
    def test_losing_the_target_stops_the_homing_for_good(self):
        """★★ 收方每 tick 复查一次射程（`0x47e45b`），出圈就把锁定格写成
        **−2**（`0x47e411`），而选目标那一段只认 −1（`0x47e36b`）
        ⇒ 这颗火箭从此走直线，**目标再回到射程里也不追了**。

        用户 2026-08-28：「爆炸点和飞行动画基本吻合了，但是偶尔还是会出现
        不一样的时候」—— 剩下的那点差就是这条。
        """
        weapon = self.rocket()
        shell = self.shell(weapon, 0.0, 0.0, 0.0)
        near = [(0, 0.0, -100.0, False, 0)]
        bot._homing_step(self.room, shell, near)
        self.assertEqual(0, shell.locked)
        # 目标跑远了 —— 这一 tick 就判丢。
        bot._homing_step(self.room, shell, [(0, 0.0, -900.0, False, 0)])
        self.assertEqual(bot.HOMING_LOST, shell.locked)
        # 再靠回来也没用：从此不拐弯。
        before = (shell.vx, shell.vy)
        bot._homing_step(self.room, shell, near)
        self.assertEqual(bot.HOMING_LOST, shell.locked)
        self.assertAlmostEqual(before[0], shell.vx, places=6)
        self.assertAlmostEqual(before[1], shell.vy, places=6)

    def test_a_target_that_disappears_also_counts_as_lost(self):
        """★ 目标死了 / 换图了（句柄查不到）走的是同一条 −2（`0x47e40d`）。"""
        weapon = self.rocket()
        shell = self.shell(weapon, 0.0, 0.0, 0.0)
        bot._homing_step(self.room, shell, [(2, 0.0, -100.0, False, 0)])
        self.assertEqual(2, shell.locked)
        bot._homing_step(self.room, shell, [])
        self.assertEqual(bot.HOMING_LOST, shell.locked)

    def test_not_finding_anyone_yet_is_not_the_same_as_losing(self):
        """★ 一个都没扫到时锁定格停在 −1，**下一 tick 接着扫**（别写成 −2）。"""
        weapon = self.rocket()
        shell = self.shell(weapon, 0.0, 0.0, 0.0)
        bot._homing_step(self.room, shell, [(0, 0.0, -900.0, False, 0)])
        self.assertIsNone(shell.locked)
        bot._homing_step(self.room, shell, [(0, 0.0, -100.0, False, 0)])
        self.assertEqual(0, shell.locked)


class BotBuriedMuzzleTests(TerrainMixin, BotFireRoom):
    """★★★ **枪口埋进地形里的那一发根本打不出去**（§76）。

    用户 2026-08-28 实机报了两条，是同一件事的两个面：

    * 「偶尔会看不到扔出去的手雷，但是过一会儿在旁边出现了爆炸动画和火焰」；
    * 「明明看着自己躲开了，最终还是打到我身上了」。

    客户端逐帧日志（`bshook` 的 `PROJ.`）把它钉死了：bot 站在斜坡脚下
    往上打时枪口落在山体里，收方的弹体**位置一步不动**、速度每帧对折
    反向 —— 玩家根本看不见；而服务端按闭式解一路飞下去，十几个 tick
    之后在别处炸开，还带着半径 100 的溅射。
    """

    def test_a_muzzle_inside_terrain_counts_as_blocked(self):
        terrain = synth_terrain("wallcol", floor=200, height=260,
                                walls=((100, 300, 0),))
        weapon = weapondata.get(1000020)
        shot = ballistics.solve(weapon, 400.0, 0.0,
                                speed=bot._lob_speed(weapon, 400.0, 0.0))
        self.assertTrue(terrain.blocks_bullet(200, 100), "夹具没造对")
        self.assertTrue(bot._path_blocked(terrain, 200, 100, shot),
                        "枪口埋在墙里就该算被挡住")
        self.assertFalse(bot._path_blocked(terrain, 500, 100, shot),
                         "枪口在空地上、弹道又通，不该算被挡")

    def test_a_shell_born_inside_terrain_resolves_on_the_spot(self):
        """★ 收方是**原地卡住**的，服务端也得炸在那儿，不能当它飞走了。"""
        terrain = synth_terrain("wallcol", floor=200, height=260,
                                walls=((100, 300, 0),))
        self.assertEqual(0.0, bot._terrain_stop_t(terrain, 200, 100,
                                                  260, 60))
        self.assertIsNone(bot._terrain_stop_t(terrain, 500, 100, 560, 60))

    def test_it_will_not_fire_from_a_buried_muzzle(self):
        """★ 端到端：枪口埋墙时一发 `rpFire` 都不该有。"""
        self.install_terrain(synth_terrain(
            "buried", floor=900, width=2000, height=1000,
            walls=((0, 700, 0),)))
        # 把 bot 摆在墙里（枪口 = 脚 + 前 43 + 上 57，整个都在墙里）。
        self.bot_conn.battle_pos = (400.0, 880.0)
        self.bot_conn.holding = True
        self.walk(self.alice, [(900, 880), (920, 880), (940, 880)])
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))


class ShellJumpPadTests(unittest.TestCase):
    """★★★ 弹跳台把**弹体**也弹起来（V0.3 §103）。

    用户 2026-08-29：「我 100% 肯定手雷一定会在弹跳台上弹跳，这也是这个游戏
    的特色……在完全垂直的弹跳台上不停的扔手雷可以达到封路的效果。」

    真值来自那一局的客户端逐帧日志：真人的手雷句柄 **100048** 在第 6 帧
    位置 `(771.30, 877.91)` 上速度从 `(22.66, 34.30)` 突变成 `(2.43, −30.43)`。
    """

    def pad_terrain(self, pads):
        rows = ["0" * 64 for _ in range(32)]
        return mapdata.MapTerrain(test_mapdata.make_record(rows, jump=pads))

    def shell_at(self, weapon, x, y, vx, vy):
        shot = ballistics.Shot(math.atan2(vy, vx), 0.0,
                               math.hypot(vx, vy), 46, 0.96)
        shell = bot.Shell(1, 0, weapon, 2, x, y, shot, 0.0, 46)
        shell.x, shell.y = x, y
        shell.bounced = True
        shell.vx, shell.vy = vx, vy
        return shell

    def test_it_reproduces_the_real_shot(self):
        terrain = mapdata.load("Iceria_b")
        if terrain is None or not terrain.jump_pads:
            self.skipTest("没有 Iceria_b 的地形产物")
        shell = self.shell_at(weapondata.get(1000020),
                              771.30, 877.91, 22.66, 34.30)
        self.assertTrue(bot._jump_pad_shell(shell, terrain))
        self.assertAlmostEqual(2.43, shell.vx, places=2)
        self.assertAlmostEqual(-30.43, shell.vy, places=2)

    def test_the_test_is_swept_not_a_point(self):
        """★ 触发那一刻弹体圆心离台心还有 36.5（> 20 + 8）——
        判定的是**这一步扫过去的那一段**（`0x50f410` 把速度一起传下去）。"""
        terrain = self.pad_terrain([[792, 908, 41.0, -416.0]])
        weapon = weapondata.get(1000020)
        far = self.shell_at(weapon, 771.30, 877.91, 0.0, 0.0)
        self.assertFalse(bot._jump_pad_shell(far, terrain),
                         "站着不动就该够不着")
        moving = self.shell_at(weapon, 771.30, 877.91, 22.66, 34.30)
        self.assertTrue(bot._jump_pad_shell(moving, terrain),
                        "扫过去的那一段穿过台子，就该被弹")

    def test_it_switches_to_the_integrated_path(self):
        """弹完闭式解就不作数了（和撞地形弹开同一条路，§84）。"""
        terrain = self.pad_terrain([[792, 908, 41.0, -416.0]])
        shell = self.shell_at(weapondata.get(1000020),
                              771.30, 877.91, 22.66, 34.30)
        shell.bounced = False
        bot._jump_pad_shell(shell, terrain)
        self.assertTrue(shell.bounced)

    def test_no_pads_no_launch(self):
        terrain = self.pad_terrain([])
        shell = self.shell_at(weapondata.get(1000020),
                              771.30, 877.91, 22.66, 34.30)
        self.assertFalse(bot._jump_pad_shell(shell, terrain))

    def test_a_plain_bullet_is_not_launched(self):
        """★★★ 撞地形当场炸的那一类**不会被台子弹起来**（§112）。

        客户端逐帧日志：`ch02-02`（`BulletObj`）扫过 `Iceria_b` 那个台子
        17 次、一次都没被弹；`ch00-02`（`AppleGrenade`）每次都弹。
        用户 2026-08-29 报的「炮弹飞到弹跳台就消失了，过一会儿在台子上方
        才出现爆炸动画」就是服务端这边多弹了这一下。
        """
        terrain = self.pad_terrain([[792, 908, 41.0, -416.0]])
        plain = weapondata.get(1002020)                       # ch02-02
        self.assertFalse(bot._bounces_off_terrain(plain))
        shell = self.shell_at(plain, 771.30, 877.91, 22.66, 34.30)
        self.assertFalse(bot._jump_pad_shell(shell, terrain))
        self.assertAlmostEqual(22.66, shell.vx, places=2)
        self.assertAlmostEqual(34.30, shell.vy, places=2)

    def test_it_throws_much_higher_than_the_shot_itself(self):
        """★ 用户说的「弹得很高，自己手动扔都扔不了那么高」。"""
        terrain = self.pad_terrain([[792, 908, 41.0, -416.0]])
        shell = self.shell_at(weapondata.get(1000020),
                              771.30, 877.91, 22.66, 34.30)
        bot._jump_pad_shell(shell, terrain)
        apex = shell.vy * shell.vy / (2.0 * botmove.GRAVITY)
        self.assertGreater(apex, 350.0, "台子给的高度应该有好几百")


class BotItemTests(BotFireRoom):
    """★★★ 道具模式：bot 捡得到、用得掉、也踩得中别人的雷（V0.3 §100 / §101）。

    用户 2026-08-29：「道具模式里，bot 似乎捡不到道具。我用减速胶水道具后，
    bot 踩上去似乎没有什么效果，不会被减速。」

    真因是同一个形状：拾取和道具效果**整条链都是客户端发起**的
    （`Character::CheckItemPickup` 只跑本机玩家），bot 没有本机 ⇒ 一件都
    捡不到、一个效果都吃不到。
    """

    def drop(self, item_id, x, y):
        """在 `(x, y)` 刷一件道具，返回句柄。"""
        quest = self.room.quest
        handle = quest.allocate_item() & 0xFFFFFFFF
        quest.items_on_map.add(handle)
        quest.remember_item(handle, item_id)
        quest.items_at[handle] = (float(x), float(y))
        return handle

    def stand(self, x=400.0, y=300.0):
        self.bot_conn.body = bot.botmove.Body(x, y)
        self.bot_conn.battle_pos = (x, y)

    def test_it_picks_up_what_it_steps_on(self):
        self.stand()
        handle = self.drop(10300, 400.0, 280.0)     # Shield
        got = bot._item_pickups(self.room, self.bot_conn, self.bot_seat)
        self.assertEqual([(handle, 10300)], got)
        self.assertEqual([10300], self.room.quest.item_slots[self.bot_seat])
        self.assertNotIn(handle, self.room.quest.items_at)

    def test_a_far_item_is_left_alone(self):
        self.stand()
        self.drop(10300, 900.0, 280.0)
        self.assertEqual([], bot._item_pickups(self.room, self.bot_conn,
                                               self.bot_seat))

    def test_the_pickup_is_broadcast_so_it_vanishes_on_every_screen(self):
        """★ `0x0405` 必须广播 —— 不广播的话那件东西在所有人屏幕上都还在。"""
        self.stand()
        self.clear()
        handle = self.drop(10300, 400.0, 280.0)
        bot._item_pickups(self.room, self.bot_conn, self.bot_seat)
        self.assertIn(gameserver.OP_PICKED_ITEM, opcodes(self.alice),
                      "拾取放行没广播出去")

    def test_it_never_sends_the_slot_packet(self):
        """★★ `0x040b` 是「往**收包的那个人**的槽里塞一件」——
        替 bot 发等于凭空给真人多一件道具。"""
        self.stand()
        self.clear()
        self.drop(10300, 400.0, 280.0)
        bot._item_pickups(self.room, self.bot_conn, self.bot_seat)
        self.assertNotIn(gameserver.OP_GRANT_ITEM, opcodes(self.alice))

    def test_it_uses_what_it_holds(self):
        self.stand()
        self.drop(10301, 400.0, 280.0)              # SpeedUp
        bot._item_pickups(self.room, self.bot_conn, self.bot_seat)
        self.clear()
        self.assertTrue(bot._use_held_item(self.room, self.bot_conn,
                                           self.bot_seat))
        self.assertEqual([], self.room.quest.item_slots[self.bot_seat])
        self.assertIn(gameserver.OP_ITEM_EFFECT, opcodes(self.alice))

    def test_an_empty_hand_uses_nothing(self):
        self.assertFalse(bot._use_held_item(self.room, self.bot_conn,
                                            self.bot_seat))

    def test_a_ground_weapon_changes_the_gun_server_side(self):
        """★ 那三把枪真人是**客户端自己**换的（`vf_11c` 只认本机玩家，§223），
        bot 只能服务端换。"""
        self.stand()
        self.drop(10201, 400.0, 280.0)              # 火焰喷射器
        bot._item_pickups(self.room, self.bot_conn, self.bot_seat)
        self.assertEqual(1900000, self.bot_conn.weapon.id)
        # 死一次就换回自己那把。
        self.bot_conn.drop_item_weapon()
        self.assertNotEqual(1900000, self.bot_conn.weapon.id)

    def test_an_item_below_the_platform_is_left_alone(self):
        """★★ 「bot 走到道具枪的**正上方**就把它捡起来了」（V0.3 §114）。

        横坐标对上不算数 —— 纵坐标差着一层平台的时候，三个碰撞圆一个都够
        不着。这条钉的是判定本身；刷新点为什么会错开一层见
        `test_battle.GroundItemSpawnTests`。
        """
        self.stand()                                 # 脚在 y=300
        handle = self.drop(10201, 400.0, 460.0)      # 下面那层地面
        self.assertEqual([], bot._item_pickups(self.room, self.bot_conn,
                                               self.bot_seat))
        self.assertIn(handle, self.room.quest.items_at)
        self.assertIsNone(self.bot_conn.item_weapon)

    def _mine(self, x, y, age=None):
        """在 `(x, y)` 放一摊胶水，`age` = 已经放了多久（缺省 = 刚布好）。"""
        if age is None:
            age = gameserver.SLOW_MINE_ARM_SECONDS + 0.01
        self.room.quest.slow_mines.append(
            (x, y, 0, time.monotonic() - age))

    def test_stepping_on_a_slow_mine_actually_slows_it(self):
        self.stand()
        self._mine(400.0, 285.0)
        now = time.monotonic()
        self.assertTrue(bot._step_on_slow_mine(self.room, self.bot_conn,
                                               self.bot_seat, now))
        # ★ 胶水是**留在地上**的一摊，不是踩一次就没的雷（§105）。
        self.assertEqual(1, len(self.room.quest.slow_mines))
        self.assertAlmostEqual(gameserver.SLOWED_SPEED_RATIO,
                               bot._speed_scale(self.bot_conn, now))
        # 到点自己恢复。
        self.assertAlmostEqual(
            1.0, bot._speed_scale(self.bot_conn,
                                  now + gameserver.SLOWED_SECONDS + 0.01))

    def test_stepping_on_a_slow_mine_tells_everybody(self):
        """★★ §109：不广播 `0x040a` 的话别人屏幕上 bot 照旧满速走
        —— 收方的角色是靠心跳里那六位方向键自己走的（§39）。"""
        self.stand()
        self._mine(400.0, 285.0)
        before = len(self.alice.sent)
        bot._step_on_slow_mine(self.room, self.bot_conn, self.bot_seat,
                               time.monotonic())
        blob = b"".join(self.alice.sent[before:])
        self.assertIn(struct.pack("<i", gameserver.STATUS_ITEM_SLOWED), blob,
                      "该给房里每个人发一发「bot 减速了」")

    def test_a_mine_that_has_not_armed_yet_does_nothing(self):
        """★ 原版那一摊要等 3 秒才生效（`0x523e12`，§108）。"""
        self.stand()
        self._mine(400.0, 285.0, age=0.5)
        self.assertFalse(bot._step_on_slow_mine(self.room, self.bot_conn,
                                                self.bot_seat,
                                                time.monotonic()))

    def test_a_mine_past_its_life_is_gone(self):
        """★ 15 秒之后 `SlowMineObject::Tick` 自毁（`0x523df8`，§108）——
        服务端也得把它从表里摘掉，不然「放下去半分钟还粘人」。"""
        self.stand()
        self._mine(400.0, 285.0,
                   age=gameserver.SLOW_MINE_LIFE_SECONDS + 0.01)
        self.assertFalse(bot._step_on_slow_mine(self.room, self.bot_conn,
                                                self.bot_seat,
                                                time.monotonic()))
        self.assertEqual([], self.room.quest.slow_mines)

    def test_a_far_mine_does_nothing(self):
        self.stand()
        self._mine(900.0, 285.0)
        self.assertFalse(bot._step_on_slow_mine(self.room, self.bot_conn,
                                                self.bot_seat,
                                                time.monotonic()))

    def test_a_freeze_burst_in_range_stops_it(self):
        """★ `Item.ini` 的 `[Freezer] Range=300`，`Status.ini` 第 12 条 2 秒。"""
        self.stand(400.0, 300.0)
        now = time.monotonic()
        self.room.quest.freeze_bursts.append((400.0, 320.0, 0, now))
        self.assertTrue(bot._take_freeze(self.room, self.bot_conn,
                                         self.bot_seat, now))
        self.assertEqual(0.0, bot._speed_scale(self.bot_conn, now),
                         "冻住就是一步都走不了")
        self.assertAlmostEqual(
            1.0, bot._speed_scale(self.bot_conn,
                                  now + gameserver.FROZEN_SECONDS + 0.01))

    def test_a_freeze_burst_out_of_range_misses(self):
        self.stand(400.0, 300.0)
        now = time.monotonic()
        self.room.quest.freeze_bursts.append((400.0 + 400.0, 300.0, 0, now))
        self.assertFalse(bot._take_freeze(self.room, self.bot_conn,
                                          self.bot_seat, now))
        self.assertEqual(1.0, bot._speed_scale(self.bot_conn, now))

    def test_a_freeze_burst_is_settled_once(self):
        self.stand(400.0, 300.0)
        now = time.monotonic()
        self.room.quest.freeze_bursts.append((400.0, 320.0, 0, now))
        bot._take_freeze(self.room, self.bot_conn, self.bot_seat, now)
        self.assertEqual([], self.room.quest.freeze_bursts)

    def test_smoke_hides_the_enemy_from_the_bot(self):
        """★ D67（我们定的）：别人放了烟，云里的人 bot 挑不中。"""
        self.walk(self.alice, [(500.0, 300.0)])
        seat = self.room.seat_index_of(self.alice)
        before = [s for s, *_ in bot._hostile_targets(self.room, self.bot_seat)]
        self.assertIn(seat, before)
        self.room.quest.smokes.append(
            (500.0, 300.0, time.monotonic() + gameserver.SMOKE_SECONDS))
        after = [s for s, *_ in bot._hostile_targets(self.room, self.bot_seat)]
        self.assertNotIn(seat, after, "云里的人不该还被挑中")

    def test_smoke_expires(self):
        self.walk(self.alice, [(500.0, 300.0)])
        seat = self.room.seat_index_of(self.alice)
        self.room.quest.smokes.append((500.0, 300.0, time.monotonic() - 1.0))
        after = [s for s, *_ in bot._hostile_targets(self.room, self.bot_seat)]
        self.assertIn(seat, after, "散掉的烟不该还挡着")
        self.assertEqual([], self.room.quest.smokes, "散了就该清掉")

    def test_the_slow_really_shortens_the_step(self):
        """★ 减速要真的落到**走出去多远**上 —— 只挂个标记等于没做。"""
        who = chrprops.get(self.bot_conn.character_id)
        full = bot.botmove.walk_speed(who)
        slow = bot.botmove.walk_speed(
            who, scale=gameserver.SLOWED_SPEED_RATIO)
        self.assertAlmostEqual(full * 0.3, slow)


class BotGhostItemTests(BotItemTests):
    """★★★ **已经在玩家屏幕上消失**的道具，bot 不许再捡（V0.3 §118）。

    用户 2026-08-29 17:50：「地上有一个胶水道具，bot 走过去之后，它捡到后
    突然变成了特殊武器，道具识别是不是不对。」—— **识别没问题**：那一格
    的确是胶水，可 13 个单位外还躺着一把**已经在地上 95 秒**的核弹发射器，
    玩家屏幕上早没了，服务端还当它在，于是 bot 一步踩到两件。
    17:58 那条「闪烁消失之后还能捡到」是同一个根因。
    """

    def drop_aged(self, item_id, x, y, age):
        handle = self.drop(item_id, x, y)
        quest = self.room.quest
        quest.items_born[handle] = time.monotonic() - age
        return handle

    def test_an_expired_item_is_not_picked_up(self):
        self.stand()
        handle = self.drop_aged(10201, 400.0, 280.0,
                                gameserver.ITEM_LIFE_SECONDS + 1.0)
        self.assertEqual([], bot._item_pickups(self.room, self.bot_conn,
                                               self.bot_seat))
        self.assertNotIn(handle, self.room.quest.items_at)
        self.assertIsNone(self.bot_conn.item_weapon, "捡了一把鬼枪")

    def test_a_fresh_item_is_still_picked_up(self):
        self.stand()
        handle = self.drop_aged(10300, 400.0, 280.0,
                                gameserver.ITEM_LIFE_SECONDS - 1.0)
        got = bot._item_pickups(self.room, self.bot_conn, self.bot_seat)
        self.assertEqual([(handle, 10300)], got)

    def test_the_ghost_next_door_no_longer_gets_swept_up(self):
        """★ 用户那一幕：脚下是新鲜的胶水，13 个单位外是一把过期的枪。"""
        self.stand()
        glue = self.drop_aged(10400, 400.0, 280.0, 1.0)
        self.drop_aged(10200, 413.0, 266.0, 95.0)     # 躺了 95 秒的核弹发射器
        got = bot._item_pickups(self.room, self.bot_conn, self.bot_seat)
        self.assertEqual([(glue, 10400)], got, "只该捡到胶水")
        self.assertIsNone(self.bot_conn.item_weapon)

    def test_the_pickup_radius_is_the_reversed_one(self):
        """★ 18 是 `0x51f2e2` 写死的 `18.0f`，不再是 §100 估的 20。"""
        self.assertEqual(18.0, bot.BOT_ITEM_RADIUS)


class BotReflectShieldTests(BotFireRoom):
    """★★★ 有反射护盾的人，bot 的弹体该被**弹开**而不是炸掉（V0.3 §119）。

    用户 2026-08-29：「我捡了反射道具，我有反射效果时，bot 扔的苹果弹到我
    身上还是能直接炸掉，应该反弹才对。」

    原版判定在 `BulletObj` 撞人那条路上（`0x47f09c` 的 `HasAttr(属性 3)`）：
    命中前先看目标身上有没有 `반사`，有就把弹体从半径 50 的圆里退出来、
    对圆的法线镜像速度、接着飞。真人之间各机器各算，**bot 的弹体是服务端
    算的** ⇒ 服务端不记这本账就照旧炸在人身上。
    """

    def straight_shell(self, x, y, vx, vy):
        weapon = weapondata.get(1000010)              # ch00-01，直射、无重力
        shot = ballistics.launch(weapon, 0.0, 1.0)
        shell = bot.Shell(1, 0, weapon, 9, x, y, shot, 0.0, 200)
        shell.bounced = True                          # 逐 tick 积分
        shell.vx, shell.vy = vx, vy
        return shell

    def target(self, x=500.0, y=300.0, seat=0):
        return [(seat, x, y, False, self.alice_character())]

    def alice_character(self):
        return self.room.seats[0].character_id

    def shield(self, seat=0, seconds=None):
        seconds = (gameserver.REFLECT_SECONDS if seconds is None else seconds)
        self.room.quest.reflect_until[seat] = time.monotonic() + seconds

    def run_shell(self, shell, bodies, ticks=12):
        for _ in range(ticks):
            hit = bot._shell_step(self.room, shell, None, bodies)
            if hit is not None:
                return hit
        return None

    def test_without_a_shield_it_just_hits(self):
        bodies = self.target()
        centre = chrprops.get(bodies[0][4]).center(500.0, 300.0, False)
        shell = self.straight_shell(380.0, centre[1], 20.0, 0.0)
        self.assertIsNotNone(self.run_shell(shell, bodies), "没护盾就该打中")

    def test_a_shield_bounces_it_back(self):
        self.shield()
        bodies = self.target()
        centre = chrprops.get(bodies[0][4]).center(500.0, 300.0, False)
        shell = self.straight_shell(380.0, centre[1], 20.0, 0.0)
        self.assertIsNone(self.run_shell(shell, bodies), "有护盾就不该结算")
        self.assertLess(shell.vx, 0.0, "该被弹回去")
        self.assertAlmostEqual(20.0, math.hypot(shell.vx, shell.vy), places=3,
                               msg="护盾只换方向、不减速（那两个 ×0.5 是地形的）")

    def test_it_stops_on_the_circle_not_inside_the_body(self):
        self.shield()
        bodies = self.target()
        centre = chrprops.get(bodies[0][4]).center(500.0, 300.0, False)
        shell = self.straight_shell(380.0, centre[1], 20.0, 0.0)
        self.run_shell(shell, bodies)
        span = math.hypot(shell.x - centre[0], shell.y - centre[1])
        self.assertAlmostEqual(gameserver.REFLECT_RADIUS + shell.radius, span,
                               delta=0.5)

    def test_it_wears_off(self):
        self.shield(seconds=-0.1)                     # 已经到点了
        bodies = self.target()
        centre = chrprops.get(bodies[0][4]).center(500.0, 300.0, False)
        shell = self.straight_shell(380.0, centre[1], 20.0, 0.0)
        self.assertIsNotNone(self.run_shell(shell, bodies),
                             "8 秒过了就该照旧打中")
        self.assertEqual({}, self.room.quest.reflect_until, "到点的该摘掉")

    def test_somebody_elses_shield_does_not_protect_this_one(self):
        self.shield(seat=3)
        bodies = self.target(seat=0)
        centre = chrprops.get(bodies[0][4]).center(500.0, 300.0, False)
        shell = self.straight_shell(380.0, centre[1], 20.0, 0.0)
        self.assertIsNotNone(self.run_shell(shell, bodies))


class BotItemWeaponBudgetTests(BotFireRoom):
    """★★★ 捡来的枪**用完就还原**（V0.3 §115）。

    用户 2026-08-29：「bot 捡到道具枪后，过一段时间后，道具枪应该恢复成
    正常枪的，但是 bot 从此以后一直拿着道具枪，不会自动恢复。」

    真因和 §100 / §109 同形：额度记在**持枪那台机器**上
    （`[持枪器+0x30/0x34/0x38]`，每帧由 `0x48be09` 结算），bot 没有本机
    ⇒ 没有一台会替它数。`weapon.ini` 里的两个键就是额度本身：
    `ch-nuke ForceCount=3`、`ch-flamer ForceTime=15000`、
    `ch-water ForceTime=10000`。
    """

    def take(self, item_id):
        """让 bot 捡到地上那把枪，返回它的 `weapondata.Weapon`。"""
        bot._take_weapon_item(self.bot_conn, self.bot_seat, item_id)
        return self.bot_conn.item_weapon

    def expire(self, now):
        return bot._expire_item_weapon(self.room, self.bot_conn,
                                       self.bot_seat, now)

    def test_the_nuke_is_metered_in_shots(self):
        weapon = self.take(10200)                    # 迷你核弹发射器
        self.assertEqual(1900020, weapon.id)
        self.assertEqual(3, weapon.force_count)
        self.assertEqual(3, self.bot_conn.item_weapon_shots)
        self.assertIsNone(self.bot_conn.item_weapon_until, "它不限时")

    def test_the_throwers_are_metered_in_seconds(self):
        for item_id, ammo, seconds in ((10201, 1900000, 15.0),
                                       (10202, 1900030, 10.0)):
            self.bot_conn.drop_item_weapon()
            weapon = self.take(item_id)
            self.assertEqual(ammo, weapon.id)
            self.assertEqual(seconds, weapon.force_ms / 1000.0)
            self.assertIsNone(self.bot_conn.item_weapon_shots, "它不限发数")
            self.assertIsNotNone(self.bot_conn.item_weapon_until)

    def test_it_keeps_the_nuke_until_the_last_shot_is_spent(self):
        self.take(10200)
        for spent in range(3):
            self.assertFalse(self.expire(time.monotonic()),
                             f"才打了 {spent} 发就还原了")
            bot._spend_item_weapon_shot(self.bot_conn)
        self.assertEqual(0, self.bot_conn.item_weapon_shots)
        self.assertTrue(self.expire(time.monotonic()))
        self.assertIsNone(self.bot_conn.item_weapon)

    def test_it_keeps_a_thrower_until_the_time_is_up(self):
        self.take(10201)                             # 15 秒
        deadline = self.bot_conn.item_weapon_until
        self.assertFalse(self.expire(deadline - 0.5))
        self.assertEqual(1900000, self.bot_conn.weapon.id)
        self.assertTrue(self.expire(deadline))
        self.assertIsNone(self.bot_conn.item_weapon)

    def test_a_thrower_never_runs_out_of_shots(self):
        """★ 限时的两把 `ForceCount` 是 0 —— 原版那一跳减不到它们头上。"""
        self.take(10202)
        for _ in range(50):
            bot._spend_item_weapon_shot(self.bot_conn)
        self.assertIsNone(self.bot_conn.item_weapon_shots)
        self.assertFalse(self.expire(self.bot_conn.item_weapon_until - 0.1))

    def test_the_switch_back_is_announced_to_everyone(self):
        """★★ 不发 `rpChangeWeapon` 的话，别人屏幕上那把枪不会变回去
        —— 收方那条路只认包，自己不带额度。"""
        self.take(10200)
        self.clear()
        for _ in range(3):
            bot._spend_item_weapon_shot(self.bot_conn)
        self.assertTrue(self.expire(time.monotonic()))
        frames = change_weapon_frames(self.alice, self.bot_seat)
        self.assertTrue(frames, "还原没有广播出去")
        seat, ammo = struct.unpack_from("<Bi", body_of(frames[-1]), 0)
        self.assertEqual(self.bot_seat, seat)
        self.assertEqual(self.bot_conn.weapon.id, ammo)
        self.assertNotEqual(1900020, ammo)

    def test_firing_spends_a_shot(self):
        """★ 扣次数挂在**开火那一刻**（原版 `0x48bade` 就在造弹体的前一步）。"""
        self.take(10200)
        before = self.bot_conn.item_weapon_shots
        self.bot_conn.next_fire_at = 0.0
        self.approach()
        self.assertLess(self.bot_conn.item_weapon_shots, before)

    def test_dying_drops_the_budget_with_the_gun(self):
        """★ 三格要一起清：留着余额的话，下一把枪会带着上一把的账。"""
        self.take(10200)
        bot._spend_item_weapon_shot(self.bot_conn)
        self.bot_conn.drop_item_weapon()
        self.assertIsNone(self.bot_conn.item_weapon_shots)
        self.assertIsNone(self.bot_conn.item_weapon_until)
        self.assertEqual(3, self.take(10200).force_count)
        self.assertEqual(3, self.bot_conn.item_weapon_shots)


class BotMagazineStatusTests(BotFireRoom):
    """★★★ 「打几发就结束」的那三条状态，服务端得**自己数**（V0.3 §117）。

    用户 2026-08-29：「bot 捡到加强道具后，苹果弹模型会变大，这个没问题，
    但是变大的苹果弹应该打几次之后就恢复的，但是 bot 的苹果弹一直不恢复。」

    `Status.ini` 里绝大多数状态有 `Time`，客户端各自倒计时、自己撤掉；
    强力射击 `[7]` / 三重射击 `[6]` / 毒弹 `[10]` **只有 `Magazine=3`**，
    时长是 −1（无限），真正的结束条件是「持有者打完 3 发」——
    只有他那台机器数得出来，数完发一发 `0x040d`（§200）。
    bot 没有本机 ⇒ 没有一台会替它数 ⇒ 永远是加强状态。
    """

    def use(self, item_id):
        """让 bot 用掉一件道具（和 `_item_pickups` 之后那一步同一条路）。"""
        self.room.quest.grant_item(self.bot_seat, item_id)
        self.clear()
        return bot._use_held_item(self.room, self.bot_conn, self.bot_seat)

    def shoot(self, times=1):
        for _ in range(times):
            bot._spend_magazine_shots(self.room, self.bot_conn, self.bot_seat)

    def removals(self):
        return bodies(self.alice, gameserver.OP_REMOVE_CHAR_ATTR)

    def test_using_it_starts_the_count(self):
        self.assertTrue(self.use(10307))
        self.assertEqual({7: 3}, self.bot_conn.magazine_attrs)

    def test_the_other_two_magazine_items_count_too(self):
        self.use(10306)                              # 三重射击
        self.assertEqual({6: 3}, self.bot_conn.magazine_attrs)
        self.bot_conn.magazine_attrs = {}
        self.use(10500)                              # 毒弹
        self.assertEqual({10: 3}, self.bot_conn.magazine_attrs)

    def test_a_timed_item_is_left_to_the_clients(self):
        """★ 有 `Time` 的（护盾 8 秒…）客户端自己会撤，服务端一格都不记。"""
        self.use(10300)
        self.assertEqual({}, self.bot_conn.magazine_attrs)

    def test_it_survives_the_first_two_shots(self):
        self.use(10307)
        self.clear()
        self.shoot(2)
        self.assertEqual({7: 1}, self.bot_conn.magazine_attrs)
        self.assertNotIn(gameserver.OP_REMOVE_CHAR_ATTR, opcodes(self.alice),
                         "才打两发就撤掉了")

    def test_the_third_shot_ends_it_and_tells_everybody(self):
        """★★ 不广播 `0x040d` 的话，别人屏幕上那个效果永远不会结束（§200）。"""
        self.use(10307)
        self.clear()
        self.shoot(3)
        self.assertEqual({}, self.bot_conn.magazine_attrs)
        self.assertIn(gameserver.OP_REMOVE_CHAR_ATTR, opcodes(self.alice))
        seat, attr = struct.unpack_from("<ii", self.removals()[-1], 0)
        self.assertEqual((self.bot_seat, 7), (seat, attr))

    def test_a_power_shot_really_is_twice_as_big_and_twice_as_hard(self):
        """★★ `SizeRatio=2.0` 非跟不可：每台客户端都把 bot 那颗放大了，
        服务端还按原半径判地形 / 判命中就又不是同一颗弹了（§116 的形状）。"""
        self.use(10307)
        self.bot_conn.next_fire_at = 0.0
        self.approach_far(settle=False)
        shells = self.bot_conn.pending_shots
        self.assertTrue(shells, "这一下应该开火了")
        shell = shells[0]
        self.assertEqual(2.0, shell.size_ratio)
        self.assertEqual(2.0, shell.damage_ratio)
        self.assertEqual(shell.weapon.size * 2.0, shell.radius)

    def test_a_plain_shot_is_unchanged(self):
        self.bot_conn.next_fire_at = 0.0
        self.approach_far(settle=False)
        shell = self.bot_conn.pending_shots[0]
        self.assertEqual(1.0, shell.size_ratio)
        self.assertEqual(shell.weapon.size, shell.radius)

    def test_dying_clears_it_without_a_packet(self):
        """★ 死一次属性表就空了（`Character::Reset`），每台客户端自己拆 ——
        这边跟着清就行，**不用**补 `0x040d`。"""
        self.use(10307)
        self.clear()
        self.bot_conn.magazine_attrs = {}            # `_tick_bot` 死亡分支干的事
        self.shoot(3)
        self.assertNotIn(gameserver.OP_REMOVE_CHAR_ATTR, opcodes(self.alice))


class BotShellHitNowTests(BotFireRoom):
    """★★★ 判命中用的是「**此刻已知**的位置」（D106，换掉了 §96 的事后插值）。

    以前一帧要把弹体推 4 个 tick（收方 32 ms 一步、真人心跳 128 ms 一发），
    于是「128 ms 前那一 tick 的弹体」被拿去撞「此刻的人」。§96 当时的补法是
    **在两帧之间插值** —— 那要等到**下一发**心跳到了才算得出来，也就是说
    这一帧的爆炸得往后拖，而弹体的 `rpExplode` 迟到一格就是永久错账（§147）。

    D106 之后一格就是一格：弹体每 32 ms 推一步，判命中用的就是那一刻手上
    最新的那份位置事实 —— 不再需要、也不允许等未来的心跳回头插值。
    """

    def shoot_through(self, group, y, tick=1):
        """从 `(0, y)` 横着打一发**极快**的弹（第 1 格就到 x=500）。

        返回这一发有没有打中人。
        """
        weapon = self.bot_conn.weapon
        shot = ballistics.Shot(0.0, 1.0, 500.0, 4, 0.0)
        shell = bot.Shell(1, self.bot_conn.sync.events - 1, weapon, group,
                          0.0, y, shot, time.monotonic(), 4, born_tick=0)
        self.bot_conn.pending_shots = [shell]
        bot._advance_shells(self.room, self.bot_conn, tick)
        frames = explode_frames(self.alice, self.bot_seat)
        self.assertTrue(frames, "每一颗都必须恰好发一发 rpExplode（§42）")
        return struct.unpack_from("<i", body_of(frames[0]), 4)[0] != 0

    def body_center_at(self, foot_y):
        return chrprops.get(
            self.room.seats[self.room.seat_index_of(self.alice)].character_id
        ).center(500.0, foot_y)[1]

    def setup_target(self, y):
        self.walk(self.alice, [(500.0, y)])
        self.settle()
        self.clear()
        return bot._seat_group(self.room, self.bot_seat)

    def test_it_hits_the_target_where_it_is_now(self):
        """★ 人此刻站在哪，弹体就该撞在哪。"""
        group = self.setup_target(900.0)
        self.assertTrue(self.shoot_through(group,
                                           self.body_center_at(900.0)))

    def test_it_misses_where_the_target_no_longer_is(self):
        """★ 反过来：人已经不在那儿了，就不该判成命中。

        ★ 推到 `max_ticks`：什么都没撞上的那一发要**飞到头**才结算
        （在最后那一点炸掉，句柄账一发都不许漏，§42）。
        """
        group = self.setup_target(900.0)
        self.assertFalse(self.shoot_through(group,
                                            self.body_center_at(100.0),
                                            tick=4))

    def test_a_shell_fired_this_tick_does_not_move_yet(self):
        """★★★ 出膛那一格**一步都不推** —— 收方也是下一帧才推第一格（§147）。

        这一条是整套时序的地基：`rpFire` 到 `rpExplode` 的间隔必须恰好是
        `k × 32 ms`，早一格晚一格都会让收方那份弹体对不上号。
        """
        group = self.setup_target(900.0)
        weapon = self.bot_conn.weapon
        shot = ballistics.Shot(0.0, 1.0, 500.0, 4, 0.0)
        shell = bot.Shell(1, self.bot_conn.sync.events - 1, weapon, group,
                          0.0, self.body_center_at(900.0), shot,
                          time.monotonic(), 4, born_tick=7)
        self.bot_conn.pending_shots = [shell]
        bot._advance_shells(self.room, self.bot_conn, 7)
        self.assertEqual(0, shell.ticks)
        self.assertEqual([], explode_frames(self.alice, self.bot_seat))
        bot._advance_shells(self.room, self.bot_conn, 8)
        self.assertEqual(1, shell.ticks)
        self.assertTrue(explode_frames(self.alice, self.bot_seat))


class BotShellGirthTests(TerrainMixin, BotFireRoom):
    """★★★ **弹体是有粗细的**（§83）——「擦到平台边」那一类穿模。

    用户 2026-08-28：「bot 扔的手雷有几次是空中飞一半突然消失了，
    过了一会儿在右边地图边缘出现了爆炸动画。」

    实机对上的证据（客户端 `PROJ.` 逐帧日志，Forest_b）：句柄 200072 那一发
    火焰弹在 `(1070, 369)` 就被收方停住了，而那一点离最近的实心格还有
    **8.06** 个单位 —— 正好是 `ch01-02` 的 `Size = 8`。服务端那时候只查
    圆心，一路放行，1.1 秒后在 `(1644.8, 582.3)` 才炸。
    """

    def ledge(self):
        """一条平地 + 一段高台，高台的**左边沿**就在 x = 700。"""
        return synth_terrain("girth", floor=400, width=1400, height=440,
                             walls=((700, 1400, 300),))

    def test_a_shell_grazing_a_ledge_stops_there(self):
        """★ 圆心还在台子上方 8 个单位、边缘已经蹭上台子 = 撞上了。"""
        terrain = self.ledge()
        self.assertTrue(terrain.blocks_bullet(700, 300), "夹具没造对")
        self.assertFalse(terrain.blocks_bullet(700, 293))
        # 圆心从台子上方 7 个单位横着扫过去：只看圆心一路通畅，
        # 半径 8 的圆盘会蹭上台子。
        self.assertIsNone(
            bot._terrain_stop_t(terrain, 600, 293, 800, 293),
            "只看圆心的话这一条是通的（这就是老口径）")
        self.assertIsNotNone(
            bot._terrain_stop_t(terrain, 600, 293, 800, 293, 8.0),
            "带上 Size = 8 就该蹭上台子边")

    def test_the_contact_point_backs_off_one_sample(self):
        """★ 反弹要用「撞上之前」那一点，否则弹体贴在地形里原地卡死。"""
        terrain = self.ledge()
        hit, free = bot._terrain_contact(terrain, 600, 293, 800, 293, 8.0)
        self.assertIsNotNone(hit)
        self.assertLess(free, hit)

    def test_the_sky_above_the_map_never_blocks(self):
        """★★ 图顶上面（`y < 0`）**不算实心**（§83 的实机反证）。

        `TerrainData::Get` 对出界一律返回 2，可实机日志里弹体是从图顶
        飞出去又落回来的（句柄 200048 第 36 帧在 `(1323.48, 0.68)`）。
        算成实心的话高抛的手雷会在半空中被判撞墙，实测差了 400~495 个单位。
        """
        terrain = self.ledge()
        self.assertIsNone(bot._terrain_stop_t(terrain, 100, 6, 300, 6, 8.0),
                          "贴着图顶飞不该被判撞上")
        self.assertIsNotNone(
            bot._terrain_stop_t(terrain, 100, 200, 100, 420, 8.0),
            "往下撞地板还是该撞上")

    def test_the_left_and_right_edges_still_block(self):
        """★ 左右两边照旧算实心 —— 实机那两发就停在图边内侧 7 个单位。"""
        terrain = self.ledge()
        self.assertIsNotNone(
            bot._terrain_stop_t(terrain, 1360, 100, 1399, 100, 8.0))
        self.assertIsNotNone(
            bot._terrain_stop_t(terrain, 40, 100, 1, 100, 8.0))


class BotShellProbeShapeTests(TerrainMixin, BotFireRoom):
    """★★★ 弹体那个「粗细」是**朝前的**，不是一个圆盘（V0.3 §116）。

    用户 2026-08-29：「bot 扔了一个苹果弹，从平台上面扔到下层地面，
    反弹过后落点和炸点位置不一致。」

    真因：§83 把形状拟合成半径 `Size` 的**实心圆盘**，而原版的采样点是
    `形状中心 + 半径 × 单位速度矢量`（`0x50e98c`）再加一个 `(0, 半径)`
    （`0x50e904` 取 `vft+0x104`）—— **朝前一个点 + 正下方一个点**。
    圆盘是各向同性的，于是「贴着侧面飞过去」被误判成撞上：那一发
    （句柄 200193）在服务端第 7 tick 撞上右边 6.7 个单位外的斜坡当场弹开，
    客户端那颗从旁边擦过去继续下落，爆炸时两颗差了 **264 个单位**。
    """

    def wall_on_the_right(self):
        """左边一条深谷，x ≥ 300 是一堵从天到地的墙。"""
        return synth_terrain("probe_side", floor=430, width=600, height=440,
                             walls=((300, 600, 0),))

    def test_the_offsets_are_the_two_the_client_uses(self):
        # 正右方飞：鼻尖在 +x、另一个恒在正下方。
        self.assertEqual(((0, 8), (8, 0)),
                         bot.shell_probe_offsets(8.0, 10.0, 0.0))
        # 正下方落：两个点重合成一个方向，都在下面。
        self.assertEqual(((0, 8), (0, 8)),
                         bot.shell_probe_offsets(8.0, 0.0, 5.0))

    def test_a_wall_beside_it_does_not_stop_a_falling_shell(self):
        """★★★ 就是用户那一发：墙在**侧面** 7 个单位，直着往下落**不该停**。"""
        terrain = self.wall_on_the_right()
        self.assertTrue(terrain.blocks_bullet(300, 200), "夹具没造对")
        self.assertIsNone(
            bot._terrain_stop_t(terrain, 293, 100, 293, 200, 8.0),
            "墙在侧面，擦过去不该被判撞上（圆盘那一版就是死在这儿）")

    def test_the_same_wall_stops_it_head_on(self):
        """★ 同一堵墙，**朝它飞**就该停 —— 停在离墙一个 `Size` 的地方。"""
        terrain = self.wall_on_the_right()
        hit = bot._terrain_stop_t(terrain, 200, 100, 320, 100, 8.0)
        self.assertIsNotNone(hit)
        stop_x = 200 + (320 - 200) * hit
        self.assertAlmostEqual(292.0, stop_x, delta=1.5)

    def test_a_ledge_under_it_still_stops_it(self):
        """★ 正下方那个采样点还在 —— §83 「擦到台子上沿」那一类照旧停住。"""
        terrain = synth_terrain("girth", floor=400, width=1400, height=440,
                                walls=((700, 1400, 300),))
        self.assertIsNotNone(
            bot._terrain_stop_t(terrain, 600, 293, 800, 293, 8.0))

    def test_the_bounce_lands_on_an_integer_cell(self):
        """★★ 收方逐**整数格**扫掠，弹开之后的落点一定是整数
        （客户端逐帧日志里每一次反弹都是 `(123, 835)` 这种整点）。"""
        terrain = synth_terrain("girth", floor=400, width=1400, height=440,
                                walls=((700, 1400, 300),))
        weapon = weapondata.get(1000020)
        shell = bot.Shell(1, 0, weapon, 9, 400.0, 100.0,
                          ballistics.launch(weapon, 1.2, 30.0), 0.0, 200)
        for _ in range(60):
            bot._shell_step(self.room, shell, terrain, [])
            if shell.bounced:
                break
        self.assertTrue(shell.bounced, "这一发应该会落地弹起来")
        self.assertEqual(float(int(shell.x)), shell.x)
        self.assertEqual(float(int(shell.y)), shell.y)


class BotHighLobTicksTests(BotFireRoom):
    """★★★ 高抛的手雷不许在半空中被「飞到头了」结算掉（§83）。

    `_shell_max_ticks()` 原来除的是**出膛**速度，而抛物线在顶点只剩水平
    分量。实机那一发（句柄 200048）：`speed = 35.6`、上界算出 56 tick，
    服务端在第 56 tick 把它炸在半空；客户端那时候还在第 57 帧继续往下飞。
    """

    def test_a_high_lob_gets_enough_ticks_to_come_back_down(self):
        weapon = weapondata.get(1001020)
        terrain = synth_terrain("lobticks", floor=700, width=1800, height=800)
        shot = ballistics.launch(weapon, math.radians(-70.0),
                                 ballistics.power_for_speed(weapon, 35.6))
        # 抛物线回到出膛高度要 2·|vy| / g 个 tick。
        vy = abs(shot.speed * math.sin(shot.angle))
        need = int(math.ceil(2.0 * vy / shot.gravity))
        self.assertGreater(need, 60, "夹具没造成高抛")
        self.assertGreaterEqual(bot._shell_max_ticks(terrain, shot, weapon),
                                need)

    def test_a_flat_shot_is_still_bounded_by_the_map(self):
        weapon = weapondata.get(1001010)
        terrain = synth_terrain("lobticks", floor=700, width=1800, height=800)
        shot = ballistics.launch(weapon, 0.0, 1.0)
        self.assertLessEqual(bot._shell_max_ticks(terrain, shot, weapon),
                             bot.BOT_SHELL_TICK_CEILING)


class BotBounceTests(TerrainMixin, BotFireRoom):
    """★★★ **带引信的弹体撞地形是弹开，不是当场炸**（§84）。

    用户 2026-08-28：「真人对局时，苹果弹直接扔到地上会弹跳，
    过一会儿之后才会炸开。炸开的碎片别人也能看到。」

    语料对得上：2353 发苹果雷里「没打中角色」的那些，爆炸时刻压在
    **11~13 发心跳**（p50 = 12 ≈ `SliceTime` 1500 ms ÷ 128 ms）——
    撞地就炸的话这个分布该是散开的。火焰弹（没有 `SliceTime`）落空的
    p50 只有 6 发心跳、分布很散，那才是「撞上什么炸在那儿」。
    """

    def flat(self):
        return synth_terrain("bounce", floor=400, width=1600, height=440)

    def shell(self, weapon, x, y, angle, speed):
        shot = ballistics.launch(weapon, angle,
                                 ballistics.power_for_speed(weapon, speed))
        return bot.Shell(1, 0, weapon, 9, x, y, shot, 0.0,
                         bot._shell_max_ticks(self.flat(), shot, weapon))

    def test_the_apple_grenade_bounces_instead_of_exploding(self):
        weapon = weapondata.get(1000020)
        self.assertTrue(weapon.fuse_ticks, "苹果雷该有引信")
        self.assertTrue(bot._bounces_off_terrain(weapon))
        terrain = self.flat()
        shell = self.shell(weapon, 200.0, 300.0, math.radians(-20.0), 25.0)
        landed = None
        while shell.ticks < shell.max_ticks and landed is None:
            landed = bot._shell_step(self.room, shell, terrain, [])
        self.assertIsNone(landed, "撞地形不该结算")
        self.assertTrue(shell.bounced, "该弹起来过")
        self.assertEqual(shell.max_ticks, shell.ticks,
                         "一路飞到引信到期才算完")

    def test_the_bounce_halves_the_speed(self):
        """★ 客户端逐帧实测：撞上之后 `|v|` 正好是撞上之前的一半。"""
        weapon = weapondata.get(1000020)
        terrain = self.flat()
        shell = self.shell(weapon, 200.0, 300.0, math.radians(-20.0), 25.0)
        before = None
        while shell.ticks < shell.max_ticks:
            was = bot._shell_velocity(shell)
            bot._shell_step(self.room, shell, terrain, [])
            if shell.bounced:
                before = (was[0], was[1] + shell.shot.gravity)
                break
        self.assertIsNotNone(before)
        self.assertAlmostEqual(
            0.5 * math.hypot(*before),
            math.hypot(shell.vx, shell.vy), places=3)

    def test_a_flat_floor_flips_the_vertical_component(self):
        """★ 平地的法线朝上 ⇒ 竖直分量取反、水平分量不动，整体再减半。"""
        weapon = weapondata.get(1000020)
        terrain = self.flat()
        shell = self.shell(weapon, 200.0, 300.0, math.radians(-20.0), 25.0)
        while shell.ticks < shell.max_ticks and not shell.bounced:
            bot._shell_step(self.room, shell, terrain, [])
        self.assertTrue(shell.bounced)
        self.assertGreater(shell.vx, 0.0, "还该往右走")
        self.assertLess(shell.vy, 0.0, "该被地面弹起来（y 往上是负）")

    def test_a_vertical_wall_flips_the_horizontal_component_exactly(self):
        """★★ 竖直面是**唯一能对死**的一类（§88）：x 取反、y 不动、整体减半。

        客户端实测三发（图边）：`v=(22.28, 12.50)` → `(-11.14, 6.25)`、
        `v=(22.16, 13.42)` → `(-11.08, 6.71)`、
        `v=(-26.76, -0.45)` → `(13.38, -0.23)` —— 一位不差。
        """
        weapon = weapondata.get(1000020)
        terrain = self.flat()
        # 贴着图的**右边界**平飞过去：图外算实心，那就是一面竖直的墙。
        shell = self.shell(weapon, terrain.width - 60.0, 200.0, 0.0, 25.0)
        vin = None
        while shell.ticks < shell.max_ticks and not shell.bounced:
            vin = bot._shell_velocity(shell)
            bot._shell_step(self.room, shell, terrain, [])
        self.assertTrue(shell.bounced, "该撞上图的右边界")
        want = (vin[0], vin[1] + shell.shot.gravity)
        self.assertAlmostEqual(-want[0] * bot.BOUNCE_RESTITUTION,
                               shell.vx, places=2)
        self.assertAlmostEqual(want[1] * bot.BOUNCE_RESTITUTION,
                               shell.vy, places=2)

    def test_the_flame_bomb_still_explodes_on_contact(self):
        """★ 火焰弹（`FlamingBottle`）撞**平地**照旧当场炸（§111 的 2 档）。

        ⚠ 判据不是「有没有引信」（§84 那条已被 §111 推翻）：它走
        `[vft+0x160] == 2` 那一档 —— `2×|sx| ≤ sy` 就炸。平地上 `sx ≈ 0`，
        所以永远炸；撞陡壁才弹。
        """
        weapon = weapondata.get(1001020)
        self.assertEqual(bot.BLOCKED_BOUNCE_IF_STEEP,
                         bot._blocked_mode(weapon))
        terrain = self.flat()
        shell = self.shell(weapon, 200.0, 300.0, math.radians(-20.0), 25.0)
        landed = None
        while shell.ticks < shell.max_ticks and landed is None:
            landed = bot._shell_step(self.room, shell, terrain, [])
        self.assertIsNotNone(landed, "火焰弹撞地就该炸")
        self.assertIsNone(landed[1], "撞的是地形，不是人")

    def test_a_bounced_shell_still_hits_people(self):
        """★ 弹起来之后照样撞人（碰撞判定不能只对没弹过的弹体有效）。"""
        weapon = weapondata.get(1000020)
        terrain = self.flat()
        shell = self.shell(weapon, 200.0, 300.0, math.radians(-20.0), 25.0)
        while shell.ticks < shell.max_ticks and not shell.bounced:
            bot._shell_step(self.room, shell, terrain, [])
        self.assertTrue(shell.bounced)
        # 把一个人摆在弹体正前方一步远的地方。
        target = (0, shell.x + shell.vx, shell.y + shell.vy, False, 0)
        landed = bot._shell_step(self.room, shell, terrain, [target])
        self.assertIsNotNone(landed)
        self.assertEqual(0, landed[1])


class BotFireProofTimeTests(BotFireRoom):
    """★★★ 火烧的账记在**人**身上，几道墙共用一本（§85）。

    用户 2026-08-28：「实机看下来燃烧弹的火焰伤害还是一直 10。」——
    逐指令读下来**这就是对的**，会话 24 那句「每团火各记各的账」是错的：

        Flame 的 [vft+0x140]（0x485e7a）把自己和 [flame+0x300] = 20
        交给**受害者**的 [vft+0xcc]，最后落到 0x50f7a7：
            add ecx, 0x160     ; 时刻戳记在角色身上，一个人只有一格
            cmp eax, [ecx]     ; 没到点就不算这一发
            add eax, [esp+8]   ; 到点了就顺延 20 个 tick

    ⇒ 不管站在几团火里、几道墙叠在一起，一个人 20 个 tick 之内
    **只会掉一次 `Damage`**，火伤永远是 10（`ch01-02a` 的 `Damage`）。
    """

    def wall_at(self, spot, handle):
        flame = weapondata.get(1001500)
        life = bot._fire_wall_ticks(flame)
        wall = bot.FireWall(
            handle, flame,
            [bot.Flame(handle, float(spot[0]), float(spot[1]), 0, life)],
            time.monotonic(), life)
        wall.born_tick -= life + 4          # 快进整条命（绝对 tick 轴）
        return wall

    def burns(self):
        want = botsync.character_handle(self.room.seat_index_of(self.alice))
        return [f for f in bot_frames(self.alice, self.bot_seat)
                if header(f)["opcode"] == botsync.OP_SPLASH_DAMAGED
                and struct.unpack_from("<i", body_of(f), 4)[0] == want]

    def test_two_overlapping_walls_do_not_double_burn(self):
        """★★ 两道火墙叠在同一个人脚下，掉的血还是一份。"""
        self.walk(self.alice, [(400, 100)])
        spot = self.alice.sync_trail[-1][:2]
        self.bot_conn.fires = [
            self.wall_at(spot, botsync.projectile_handle(self.bot_seat, 0)),
            self.wall_at(spot, botsync.projectile_handle(self.bot_seat, 40)),
        ]
        self.clear()
        self.walk(self.alice, [tuple(spot)])
        burns = self.burns()
        self.assertEqual(4, len(burns), "一道墙烧 4 次，两道叠着也还是 4 次")
        for frame in burns:
            self.assertAlmostEqual(
                weapondata.get(1001500).damage * bot._damage_scale(self.room),
                struct.unpack_from("<f", body_of(frame), 8)[0], places=3)

    def test_the_burn_ledger_is_per_person_not_per_wall(self):
        self.walk(self.alice, [(400, 100)])
        spot = self.alice.sync_trail[-1][:2]
        self.bot_conn.fires = [
            self.wall_at(spot, botsync.projectile_handle(self.bot_seat, 0))]
        self.clear()
        self.walk(self.alice, [tuple(spot)])
        self.assertIn(self.room.seat_index_of(self.alice), self.bot_conn.burnt)

    def test_the_ledger_is_cleared_between_maps(self):
        """★ 换图 / 新一局收方把角色重建，`[角色+0x160]` 跟着归零。"""
        self.bot_conn.burnt[0] = 12345
        self.bot_conn.reset_battle_frame()
        self.assertEqual({}, self.bot_conn.burnt)


class ReceiverLedger(object):
    """★★★ 收方那本**弹体句柄账**的最小复刻（§42 / §86）。

    收方只有一个计数器，谁创建对象谁 `++`：

    * `rpFire`  —— 当场造 `shots` 颗弹体，各占 1 个（`0x49231e` 的内层循环）；
    * `rpExplode` —— **句柄查得到**才处理；带溅射的武器会再造一个溅射对象，
      又占 1 个（§54 的 `/noboom` 实验钉死的）。查不到就**静默丢弃**；
    * `rpSetOnFire` —— 一道火墙 `2n+1` 团火（§75）；
    * `rpDash` —— 1 个（§64）。

    `dropped` 里只要有东西，实机上就是「子弹照飞、一滴血不掉，
    换武器也救不回来」。
    """

    def __init__(self, seat, start=0):
        self.seat = seat
        self.counter = start
        self.live = {}          # 句柄 -> 武器 id
        self.dropped = []

    def _take(self, n):
        base = botsync.projectile_handle(self.seat, self.counter)
        self.counter += n
        return base

    def feed(self, packet):
        opcode = header(packet)["opcode"]
        body = body_of(packet)
        if opcode == botsync.OP_FIRE:
            ammo = struct.unpack_from("<i", body, 2)[0]
            shots = struct.unpack_from("<i", body, 22)[0]
            base = self._take(shots)
            for i in range(shots):
                self.live[base + i] = ammo
        elif opcode == botsync.OP_EXPLODE:
            handle = struct.unpack_from("<i", body, 0)[0]
            ammo = self.live.pop(handle, None)
            if ammo is None:
                self.dropped.append(handle)
                return
            self._take(weapondata.get(ammo).explode_step)
        elif opcode == botsync.OP_SET_ON_FIRE:
            slice_id = struct.unpack_from("<i", body, 10)[0]
            self._take(botsync.fire_wall_handles(
                weapondata.get(slice_id).raw.get("spawn_count")))
        elif opcode == botsync.OP_DASH:
            self._take(1)


class BotHandleLedgerTests(BotSliceTests):
    """★★★★★ 服务端预测的句柄和收方**实际**分配的必须一格不差（§86）。

    用户 2026-08-28：「1 号角色，一开始打我有伤害，但是扔了几个手雷之后
    他的子弹就没有伤害了……之后我再给他换成其他武器，也全都没伤害了。」

    实机日志把它钉死了（最后一局，客户端 `PROJ+` 的句柄 vs 服务端 `开火:`）：

    ```text
    苹果雷 A   服务端 200020   收方 200020   ✓
    苹果雷 B   服务端 200022   收方 200022   ✓   ← 第一次分裂就在这一发之后
    苹果雷 C   服务端 200032   收方 200030   ✗ 差 2
    苹果雷 D   服务端 200042   收方 200036   ✗ 差 6
    苹果雷 E   服务端 200052   收方 200041   ✗ 差 11   ← 越差越多
    ```

    根因：`_split_shell()` 给每片碎片按 `handle_step`（**总数** 2）记账，
    于是四片排成 `base, base+2, base+4, base+6`；而收方在开火那一刻每片
    只分配 1 个（连号），另一个是**各自爆炸时**才创建的溅射对象。
    """

    def ledger_run(self, rounds=6):
        """让 bot 连打几发苹果雷，把它发出去的每一帧喂给收方账本。"""
        led = ReceiverLedger(self.bot_seat)
        self.apple()
        self.bot_conn.roll = lambda n: 0
        self.bot_conn.holding = True
        self.walk(self.alice, [(0.0, 100.0), (600.0, 100.0)])
        self.settle()
        seen = 0
        for _ in range(rounds):
            self.bot_conn.next_fire_at = 0.0
            self.walk(self.alice, [(600.0, 100.0)])
            self.charge()
            self.walk(self.alice, [(600.0, 100.0)])
            self.walk(self.alice, [(3000.0, 100.0)])   # 躲开 —— 让它落空分裂
            for shell in self.bot_conn.pending_shots:
                shell.max_ticks = min(shell.max_ticks, 20)
            self.settle()
            frames = bot_frames(self.alice, self.bot_seat)
            for packet in frames[seen:]:
                led.feed(packet)
            seen = len(frames)
        return led

    def test_the_receiver_never_drops_an_explosion(self):
        led = self.ledger_run()
        self.assertEqual([], led.dropped,
                         "收方查不到句柄 = 静默丢弃 = 从此打不掉血（§42）")

    def test_the_server_and_the_receiver_agree_on_the_next_handle(self):
        led = self.ledger_run()
        self.assertEqual(botsync.projectile_handle(self.bot_seat,
                                                   self.bot_conn.sync.projectiles),
                         botsync.projectile_handle(self.bot_seat, led.counter))

    def test_the_four_fragments_get_consecutive_handles(self):
        """★ 四片必须是**连号** —— 这就是上面那条差 2 的直接来源。

        收方在开火那一刻每片只造一颗弹体、只占 1 个句柄；多出来的那一个
        是**各自爆炸时**创建的溅射对象（§54 / §86）。
        """
        weapon = self.apple()
        self.bot_conn.roll = lambda n: 0
        self.bot_conn.pending_shots = []
        shell = bot.Shell(botsync.projectile_handle(self.bot_seat, 0), 0,
                          weapon, 9, 100.0, 100.0,
                          ballistics.launch(weapon, 0.0, 30.0), 0.0, 40)
        bot._split_shell(self.room, self.bot_conn, shell, (100.0, 100.0), None,
                         0)
        frags = list(self.bot_conn.pending_shots)
        self.assertEqual(4, len(frags), "该有四片碎片在飞")
        handles = sorted(s.handle for s in frags)
        self.assertEqual(list(range(handles[0], handles[0] + 4)), handles)


class BotNavWarmIsSingleThreadedTests(unittest.TestCase):
    """★★★★★ 可达图预热**全进程只有一条线程**（V0.3 §163）。

    用户 2026-09-01 23:59：「所有 bot 都卡几秒钟，后突然又换了个坐标恢复……
    bot 的位置有突变，空中的子弹也会突然显示出来，仿佛积压了几秒钟的网络包
    突然挤在一起爆发出来一样。」

    实机对上的那一次：破坏物 247 在 23:59:34.762 碎了 -> `_refresh_breakables()`
    起预热 -> 两条线程分别在 23:59:37.05（2255 ms）和 23:59:37.4（2611 ms）
    算完 -> 客户端在 **23:59:37.408~37.412 的 4 毫秒里一次收到 5.6 KB**
    （350/236/255/209/262/260/156/2048/991 …）。中间那两秒下行是**积压**的：
    客户端到达侧最大间隔只有 0.26 s，服务端 `→ 发出` 也一直在打 ——
    卡住的是每条连接那条**发送线程**（D108），它每写一个包都要重抢一次 GIL。

    离线复现（32 ms 醒一次的探针线程 + `Iceria00` 真图）：

    ```text
    什么都不跑        中位  0.3 ms   p95   0.3 ms   最大   0.3 ms
    两条预热并行      中位 16.9 ms   p95  76.1 ms   最大 132.0 ms   ← 现状
    串成一条          中位  7.1 ms   p95  14.5 ms   最大  15.4 ms   ← 改成这样
    ```

    总耗时一模一样（2357 vs 2371 ms）—— 并行一点没赚到，只是把延迟放大了
    一个数量级。
    """

    def test_many_jobs_share_one_worker(self):
        seen = []
        real = bot._warm_navigation_now
        gate = threading.Event()

        def slow(terrain, who, seeds, label):
            seen.append(threading.current_thread().name)
            gate.wait(5.0)

        bot._warm_navigation_now = slow
        self.addCleanup(setattr, bot, "_warm_navigation_now", real)
        try:
            for i in range(6):
                bot._submit_warm((None, None, (), f"job{i}"))
            # 头一份会被那条线程认领，剩下的必须**排队**，不许各起一条。
            deadline = time.monotonic() + 5.0
            while len(seen) < 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(1, len(seen), "同时最多只许有一份在算")
            workers = [t for t in threading.enumerate()
                       if t.name == "botnav-warm"]
            self.assertEqual(1, len(workers), "预热线程全进程只许有一条")
        finally:
            gate.set()
        self.assertTrue(bot.warm_settle(10.0))
        self.assertEqual(6, len(seen), "排队的那几份最后都要算掉")
        self.assertEqual({"botnav-warm"}, set(seen))

    def test_the_worker_survives_a_job_that_blows_up(self):
        """★ 一份炸了不许把线程带走 —— 带走了之后的图就再也不预热了。"""
        done = []
        real = bot._warm_navigation_now

        def boom(terrain, who, seeds, label):
            if label == "bad":
                raise RuntimeError("炸一个")
            done.append(label)

        bot._warm_navigation_now = boom
        self.addCleanup(setattr, bot, "_warm_navigation_now", real)
        bot._submit_warm((None, None, (), "bad"))
        bot._submit_warm((None, None, (), "good"))
        self.assertTrue(bot.warm_settle(10.0))
        self.assertEqual(["good"], done)


class BotShellFallsOutOfTheWorldTests(TerrainMixin, BotFireRoom):
    """★★★★★ 掉出下边界的弹体：**不发爆炸、不记句柄**（V0.3 §161）。

    用户 2026-09-01：「23:02 结束的那局……快结束之前的一分钟左右，bot3 打我
    没有伤害。」「23:14 那一局，后来 bot3 扔的苹果弹没有爆炸动画，也没有伤害。」
    「出问题的几局，好像都是有岩浆的地图。」

    实机对账（客户端 `PROJ+` 的句柄 vs 服务端 `开火:` 预测的句柄）：

    ```text
    22:53 Iceria00   FallDown 否   五个 bot 的最终偏差 0 0 0 0 0
    22:58 Esperan00  FallDown 是                     13 6 20 0 0
    23:03 Esperan00  FallDown 是                     19 41 101 0 0
    23:08 Forest00   FallDown 否                     0 0 0 0 0
    23:13 Esperan00  FallDown 是                     188 138 125 196 142
    ```

    23:14:14 那一次逐帧钉死了：苹果雷炸成四片（客户端 400023..400026），
    其中 400024 竖直往下飞，第 42 帧还在 `(955, 2022)`、竖直速度 29.4，
    下一帧越过图高 2048 —— 客户端**把它删掉了，一个爆炸对象都没建**；
    另外三片各自在落地那一帧之后 32 ms 建出了 400027/400028/400029。
    而服务端照旧当「撞到实心」结算，`explode(spawns=1)` 替一个不存在的
    对象记了一格 ⇒ 下一发 `rpFire` 服务端给 400031、客户端给 400030，
    从此每一发 `rpExplode` 都被静默丢弃（§42）。
    """

    def setUp(self):
        super().setUp()
        self.terrain = self.install_terrain(mapdata.load("Esperan00"))
        self.set_fall_down(True)

    def set_fall_down(self, value):
        props = mapdata.STORE.index().setdefault("props", {})
        props[self.room.map_name] = {"fall_down": bool(value)}
        self.addCleanup(props.pop, self.room.map_name, None)

    def shell_at(self, x, y, ammo=1000020):
        weapon = weapondata.get(ammo)
        shell = bot.Shell(handle=botsync.projectile_handle(self.bot_seat, 0),
                          fire_seq=0, weapon=weapon,
                          group=bot._seat_group(self.room, self.bot_seat),
                          x0=x, y0=y,
                          shot=ballistics.launch(weapon, 0.0, 30.0),
                          born=time.monotonic(), max_ticks=200)
        shell.x, shell.y = x, y
        return shell

    def resolve(self, y):
        """在 `(500, y)` 结算一颗弹体，返回 `(吃掉几个句柄, 发了几发包)`。"""
        shell = self.shell_at(500.0, y)
        self.clear()
        before = self.bot_conn.sync.projectiles
        bot._resolve_shell(self.room, self.bot_conn, shell, (500.0, y),
                           None, None, 0)
        return (self.bot_conn.sync.projectiles - before,
                len(bot_frames(self.alice, self.bot_seat)))

    def test_a_shell_that_leaves_the_bottom_edge_costs_nothing(self):
        """★ 收方把它静默删掉了 —— 这边一个句柄都不许记，一发包都不许发。"""
        spent, frames = self.resolve(float(self.terrain.height))
        self.assertEqual(0, spent, "收方根本没建爆炸对象，记了就永久错开")
        self.assertEqual(0, frames, "收方那颗弹体已经没了，发了也是被丢弃")

    def test_the_collision_radius_counts(self):
        """★ 拦住它的是**外缘探针**，落点因此比图高早一个半径（`Size`）。"""
        shell = self.shell_at(500.0, 100.0)
        self.assertGreater(shell.radius, 0.0, "苹果雷是有碰撞半径的")
        edge = float(self.terrain.height) - shell.radius
        self.assertTrue(bot._shell_fell_out_of_the_world(
            self.room, shell, (500.0, edge)))
        self.assertFalse(bot._shell_fell_out_of_the_world(
            self.room, shell, (500.0, edge - 1.0)))

    def test_a_map_without_falldown_still_explodes_normally(self):
        """★★ 判据是这张图的 `FallDown`，和角色那条（§143）同一套。

        没有 `FallDown` 的图底下就是实心地面，收方照常造爆炸对象 ——
        这一条要是把它们也吞了，反过来又是永久错开。
        """
        self.set_fall_down(False)
        spent, frames = self.resolve(float(self.terrain.height))
        self.assertGreaterEqual(spent, weapondata.get(1000020).explode_step)
        self.assertTrue(frames)

    def test_a_normal_hit_inside_the_map_is_untouched(self):
        spent, frames = self.resolve(400.0)
        self.assertGreaterEqual(spent, weapondata.get(1000020).explode_step)
        self.assertTrue(frames)

    def test_a_grenade_that_falls_out_does_not_split(self):
        """★ 删掉的弹体跑不出 `AppleGrenade::Tick`，一片碎片都不生（§81）。"""
        shell = self.shell_at(500.0, float(self.terrain.height))
        self.bot_conn.pending_shots = []
        bot._resolve_shell(self.room, self.bot_conn, shell,
                           (500.0, float(self.terrain.height)), None, None, 0)
        self.assertEqual([], self.bot_conn.pending_shots)

    def test_the_receiver_and_the_server_still_agree(self):
        """★★★★★ 端到端：收方那本账（`ReceiverLedger`）一格都不许差。

        收方这一侧照实机加一条：**掉出下边界的弹体它自己删掉**，
        不等 `rpExplode`、也不建爆炸对象。
        """
        led = ReceiverLedger(self.bot_seat)
        weapon = weapondata.get(1000020)
        bottom = float(self.terrain.height)
        # ① 正常落地的一发；② 掉出下边界的一发（收方自己删掉，不等爆炸包）。
        for y, vanished in ((400.0, False), (bottom, True)):
            self.clear()
            self.bot_conn.pending_shots = []
            packet, handle = self.bot_conn.sync.fire(
                weapon.id, 500.0, y, 0.0, 30.0,
                handle_step=weapon.fire_step, shots=weapon.shots,
                group=bot._seat_group(self.room, self.bot_seat))
            led.feed(packet)
            if vanished:
                led.live.pop(handle, None)      # ★ 收方那颗已经没了
            shell = self.shell_at(500.0, y)
            shell.handle = handle
            bot._resolve_shell(self.room, self.bot_conn, shell, (500.0, y),
                               None, None, 0)
            for frame in bot_frames(self.alice, self.bot_seat):
                led.feed(frame)
        self.assertEqual([], led.dropped,
                         "收方查不到句柄 = 静默丢弃 = 从此打不掉血（§42）")
        self.assertEqual(self.bot_conn.sync.projectiles, led.counter,
                         "服务端和收方的下一个句柄必须一格不差")


class BotGameModeDamageTests(BotFireRoom):
    """★★★★ **夺分模式伤害翻倍**（§87）。

    用户 2026-08-28：「我用火焰自己烧自己试出结果了，生存模式就是正常的
    10，夺分模式会变成 2 倍伤害，而 bot 扔的火焰无论什么模式都是 10。」

    原版是射手那台机器在把数字塞进包之前做的，`0x4806bf` 开头三句：

    ```asm
    004806d8  call 0x409e0a          ; 游戏模式 = 房间描述符 arguments[1]
    004806dd  cmp eax, 3 ; je        ; 夺分
    004806e9  cmp eax, 5 ; jne
    004806f1  shl dword ptr [eax], 1 ; ★ ×2
    ```

    这个函数是**所有伤害的必经之路**：直接命中（`0x47ec5b` 在
    `Projectile::OnHit` 里）、溅射（`0x481dfd`）、地面燃烧（`0x480e52`）
    都要过它。
    """

    def set_mode(self, mode):
        args = list(self.room.arguments or (0, 0, 0))
        while len(args) < 3:
            args.append(0)
        args[1] = mode
        self.room.arguments = tuple(args)

    def test_the_scale_is_two_only_in_deathmatch_and_mode_five(self):
        for mode, want in ((0, 1), (1, 1), (2, 1), (3, 2), (4, 1), (5, 2)):
            self.set_mode(mode)
            self.assertEqual(want, bot._damage_scale(self.room),
                             f"模式 {mode}")

    def test_an_unreadable_room_never_doubles(self):
        """★ 读不出模式一律**不翻倍** —— 闯关房的 `arguments` 不是这套含义。"""
        self.room.arguments = ()
        self.assertEqual(1, bot._damage_scale(self.room))
        self.room.arguments = (1,)
        self.assertEqual(1, bot._damage_scale(self.room))

    def damage_of_one_shot(self):
        self.bot_conn.next_fire_at = 0.0
        self.clear()
        self.approach()
        booms = explode_frames(self.alice, self.bot_seat)
        self.assertTrue(booms)
        return max(struct.unpack_from("<f", body_of(f), 24)[0] for f in booms)

    def test_a_direct_hit_doubles_in_deathmatch(self):
        """★ 夺分是 ×2，**但紧接着还有一条 ×0.75**（目标踩在地上，§89）。

        alice 的心跳默认 `on_ground=True`，所以这里量到的是
        `int(int(生存值 × 2) × 0.75)`。
        """
        self.set_mode(gameserver.PVP_MODE_SURVIVAL)
        plain = self.damage_of_one_shot()
        self.assertGreater(plain, 0.0)
        self.set_mode(gameserver.PVP_MODE_DEATHMATCH)
        self.assertAlmostEqual(int(int(plain * 2) * 0.75),
                               self.damage_of_one_shot(), places=3)

    def test_the_fire_wall_doubles_in_deathmatch(self):
        """地面燃烧的伤害也走 `_damage_scale()`（§87）。

        ⚠ **火墙和再烧账本要一起装、装完立刻量**，中间不许再走一帧
        （会话 38 修的偶发红）：

        * `fires` —— 上一轮那道墙活 76 个 tick（≈2.4 秒），这一轮开头
          还在烧；
        * `burnt` —— 再烧的账记在**人**身上（§85）。上一轮那道墙只要在
          这一轮的站位那一步上再烧一次，就把 alice 的免伤时刻戳按到
          「此刻」，新墙第 1 个 tick 那一发就被 20 tick 的冷却吃掉，
          `burns` 空 ⇒ 红。

        ★★ 而它烧不烧，取决于 `time.monotonic()` 在这中间**有没有跨过一个
        tick 边界** —— `_advance_fires()` 的 `end = _clock_tick(now)` 是之后
        才读的挂钟，一个 tick 32 ms，而单测一整轮才几十微秒，所以是几百次
        里翻一次（3.14 上 200 次红 2 次，Win7 的 3.8 上跑全量偶尔命中）。
        判据落在真实时间上就是铁律 10 说的那件事，只不过踩在测试里。

        ⇒ 把这两样都**在墙装好之后**清掉，判定就只剩「墙推到第 1 个 tick、
        账本是空的」，`start = born_tick + 1` 恒 ≤ `end`，和挂钟无关。
        """
        flame = weapondata.get(1001500)
        for mode, want in ((gameserver.PVP_MODE_SURVIVAL, flame.damage),
                           (gameserver.PVP_MODE_DEATHMATCH, flame.damage * 2)):
            self.set_mode(mode)
            self.walk(self.alice, [(400, 100)])
            spot = self.alice.sync_trail[-1][:2]
            life = bot._fire_wall_ticks(flame)
            wall = bot.FireWall(
                botsync.projectile_handle(self.bot_seat, 0), flame,
                [bot.Flame(botsync.projectile_handle(self.bot_seat, 0),
                           float(spot[0]), float(spot[1]), 0, life)],
                time.monotonic(), life)
            # ★ 往回退 20 个 tick = 这道墙「已经烧了一会儿」，这一帧才有
            #   tick 可推（`start = born_tick + ticks + 1` 得 ≤ `end`）。
            wall.born_tick -= bot.BOT_FIRE_REBURN_TICKS
            # ★★ 墙和账本一起装，装完立刻量 —— 见上面那段。
            self.bot_conn.fires = [wall]
            self.bot_conn.burnt = {}
            self.clear()
            self.walk(self.alice, [tuple(spot)])
            burns = [f for f in bot_frames(self.alice, self.bot_seat)
                     if header(f)["opcode"] == botsync.OP_SPLASH_DAMAGED]
            self.assertTrue(burns, f"模式 {mode} 该掉血")
            self.assertAlmostEqual(
                float(want), struct.unpack_from("<f", body_of(burns[0]), 8)[0],
                places=3, msg=f"模式 {mode}")

    def test_the_splash_doubles_in_deathmatch(self):
        weapon = weapondata.get(1000020)
        shell = bot.Shell(1, 0, weapon, 9, 0.0, 0.0,
                          ballistics.launch(weapon, 0.0, 30.0), 0.0, 40)
        bodies = [(0, weapon.splash_range * 0.5, 0.0, False, 0)]
        self.set_mode(gameserver.PVP_MODE_SURVIVAL)
        plain = bot._splash_targets(self.room, shell, (0.0, 0.0), None, bodies)
        self.set_mode(gameserver.PVP_MODE_DEATHMATCH)
        doubled = bot._splash_targets(self.room, shell, (0.0, 0.0), None, bodies)
        self.assertEqual(1, len(plain))
        self.assertEqual(1, len(doubled))
        # ★ 倍率乘在**衰减之后**（`0x480e52` 那一发 `[vft+0x128]` 排在
        #   `[vft+0x134]` 后面，§90）—— 所以是**整整两倍**，没有取整误差。
        self.assertEqual(plain[0][1] * 2, doubled[0][1])


class BotDeathmatchPenaltyTests(BotFireRoom):
    """★★★ 夺分模式**直接命中**独有的两条 ×0.75（§89）。

    出处 `0x47e618`（`Projectile` 虚表槽 `+0x128`，`Projectile::OnHit`
    在 `0x47ec5b` 调它）：

        0047e66d  cmp eax, 3 ; jne 出口         ; 只有模式 3
        0047e6cd  fild [视口+0x30]              ; = 1024
        0047e6df  or [ebp+0xc], 8 ; fmul 0.75   ; 目标离得远
        0047e6f5  cmp byte [目标+0x128], 0      ; 目标踩在地上
        0047e700  or [ebp+0xc], 4 ; fmul 0.75

    用户 2026-08-28 那一局的实机证据：他自己那发苹果弹的 `rpExplode` 是
    `flags 4 伤害 30`（= 20 × 2 × 0.75），bot 打他是 40 —— 差的就是这一条。
    """

    def penalty_damage(self, region="body"):
        weapon = weapondata.get(1000020)
        return bot._direct_hit_damage(self.room, self.bot_conn, weapon,
                                      region, self.alice_seat)

    def setUp(self):
        super().setUp()
        self.alice_seat = self.room.seat_index_of(self.alice)
        self.walk(self.alice, [(100.0, 100.0)])
        self.bot_conn.battle_pos = (100.0, 100.0)

    def test_a_grounded_target_takes_three_quarters(self):
        """目标踩在地上 -> ×0.75（用户实机那一发就是这条）。"""
        self.assertEqual(int(20 * 2 * 0.75), self.penalty_damage())

    def test_an_airborne_target_takes_the_full_doubled_damage(self):
        """腾空的目标**不减** —— `[char+0x128]` 是 0。"""
        self.walk(self.alice, [(100.0, 100.0)], )
        self.human_heartbeat(self.alice, 100.0, 60.0, on_ground=False,
                             velocity=(0, -10))
        self.assertEqual(20 * 2, self.penalty_damage())

    def test_a_far_target_takes_another_three_quarters(self):
        """超过**一个视口宽**（1024）再 ×0.75，两条累乘、各自截断。"""
        self.bot_conn.battle_pos = (100.0 + bot.BOT_LONG_SHOT_RANGE + 1.0,
                                    100.0)
        self.assertEqual(int(int(20 * 2 * 0.75) * 0.75), self.penalty_damage())

    def test_exactly_one_viewport_away_is_not_far(self):
        """门槛是**严格大于**（`fcompp` + `jne`）—— 正好 1024 不减。"""
        self.bot_conn.battle_pos = (100.0 + bot.BOT_LONG_SHOT_RANGE, 100.0)
        self.assertEqual(int(20 * 2 * 0.75), self.penalty_damage())

    def test_no_penalty_outside_deathmatch(self):
        """★ 只有模式 3。模式 5 照样 ×2（§87），但**不减**。"""
        for mode in (0, 1, 2, 4, 5):
            args = list(self.room.arguments)
            args[1] = mode
            self.room.arguments = tuple(args)
            scale = 2 if mode == 5 else 1
            self.assertEqual(20 * scale, self.penalty_damage(),
                             f"模式 {mode}")

    def test_an_unknown_stance_never_gets_the_penalty(self):
        """★ 读不出「踩没踩地」就**不减** —— 宁可少扣也不要凭空扣。"""
        self.alice.sync_trail.clear()
        self.assertEqual(20 * 2, self.penalty_damage())

    def test_the_splash_has_no_such_penalty(self):
        """★ 溅射走 `0x4806bf`，**不经过** `0x47e618` —— 一条都不减。"""
        weapon = weapondata.get(1000020)
        shell = bot.Shell(1, 0, weapon, 9, 0.0, 0.0,
                          ballistics.launch(weapon, 0.0, 30.0), 0.0, 40)
        # 贴着爆点 = 衰减那一项是 0，剩下的就是 `SplashDamage × 2`。
        bodies = [(self.alice_seat, 0.0, 0.0, False, 0)]
        hits = bot._splash_targets(self.room, shell, (0.0, -37.0), None, bodies)
        self.assertEqual(1, len(hits))
        self.assertEqual(int(weapon.splash_damage) * 2, hits[0][1])


class BotSplashFalloffTests(BotFireRoom):
    """★★★ 溅射的衰减公式（§90）—— `SplashDamage::vft+0x134`（`0x4857aa`）。

        r = 距离 / (SplashRange + 35)      ; 35 = 角色的溅射半径，写死
        r > 1                              -> 一点伤害都没有
        伤害 = int((1 − r) × (SplashDamage − 1) + 1)   ; 朝零截断
        ×2（夺分）在这之后

    语料实证只看**自伤**（受害者就是射手，位置有他自己的心跳）：
    `ch02-03` 5/5、`ch105-02` 3/3 一位不差。
    """

    def hits(self, weapon, span):
        shell = bot.Shell(1, 0, weapon, 9, 0.0, 0.0,
                          ballistics.launch(weapon, 0.0, 30.0), 0.0, 40)
        # 身体圆心正好落在 `(span, 0)` 上：`center()` 把落脚点往上抬。
        character = chrprops.get(0)
        lift = 2.0 * character.size_legs + character.size_body
        bodies = [(0, span, lift, False, 0)]
        return bot._splash_targets(self.room, shell, (0.0, 0.0), None, bodies)

    def test_the_edge_still_does_one_point_before_the_mode_scale(self):
        """★ 公式的常数项是 **+1** —— 作用半径边缘上掉的是 1（夺分 2），
        不是 0。旧的线性式在那儿是 0，整整少一档。"""
        weapon = weapondata.get(1000020)
        reach = weapon.splash_range + bot.SPLASH_BODY_RADIUS
        hits = self.hits(weapon, reach - 0.5)
        self.assertEqual(1, len(hits))
        self.assertEqual(1 * bot._damage_scale(self.room), hits[0][1])

    def test_the_reach_includes_the_targets_own_radius(self):
        """★ 作用半径是 `SplashRange + 35`，不是 `SplashRange`。"""
        weapon = weapondata.get(1000020)
        # 只按 `SplashRange` 算的话这一点早就出圈了。
        self.assertTrue(self.hits(weapon, weapon.splash_range + 10.0))
        self.assertEqual(
            [], self.hits(weapon,
                          weapon.splash_range + bot.SPLASH_BODY_RADIUS + 1.0))

    def test_the_curve_matches_the_reversed_formula(self):
        """整条曲线逐点对：`int((1 − r)(D − 1) + 1) × 倍率`。"""
        weapon = weapondata.get(1000020)
        reach = weapon.splash_range + bot.SPLASH_BODY_RADIUS
        scale = bot._damage_scale(self.room)
        for span in (0.0, 20.0, 50.0, 90.0, 120.0):
            want = int((1.0 - span / reach)
                       * (int(weapon.splash_damage) - 1) + 1) * scale
            hits = self.hits(weapon, span)
            self.assertEqual(1, len(hits), f"距离 {span}")
            self.assertEqual(want, hits[0][1], f"距离 {span}")

    def test_the_centre_takes_the_full_splash_damage(self):
        """爆点上是整整 `SplashDamage`（`(1−0)(D−1)+1 = D`）。"""
        weapon = weapondata.get(1000020)
        hits = self.hits(weapon, 0.0)
        self.assertEqual(int(weapon.splash_damage) * bot._damage_scale(self.room),
                         hits[0][1])

class BotHealthLedgerTests(HumanShotRoom):
    """★★ M5-C 的血量台账（`bothp`）—— 和每台客户端记的是同一份账（§42）。"""

    def ledger(self):
        return bot._health(self.room)

    def test_a_humans_splash_lands_in_the_ledger(self):
        self.splash(30, (12.0, -9.0))
        self.assertEqual(30.0, self.ledger().taken_by(self.bot_seat))

    def test_a_humans_direct_hit_lands_in_it_even_without_knockback(self):
        """★ 配不上 `rpFire` 的那一发**不给击退**，但血照扣（伤害在包里）。"""
        self.send(botsync.OP_EXPLODE, botsync.explode_body(
            100002, botsync.character_handle(self.bot_seat), 600.0, 150.0,
            hit_kind=botsync.HIT_CHARACTER, damage=20.0))
        self.assertTrue(self.bot_conn.body.on_ground, "这一发不该给击退")
        self.assertEqual(20.0, self.ledger().taken_by(self.bot_seat))

    def test_damage_to_a_third_party_is_tracked_too(self):
        """真人打真人服务端也看得见 —— bot 判「谁血多」要用它。"""
        self.send(botsync.OP_SPLASH_DAMAGED, botsync.splash_body(
            100002, botsync.character_handle(self.alice_seat), 25,
            600.0, 150.0, push_x=1.0, push_y=-1.0))
        self.assertEqual(25.0, self.ledger().taken_by(self.alice_seat))

    def test_a_new_round_wipes_the_whole_book(self):
        self.splash(30, (12.0, -9.0))
        for index in self.room.bot_seats():
            self.room.seats[index].conn.load_progress = 0
        bot.report_bots_loaded(self.room, "单测：新一局")
        self.assertEqual({}, self.ledger().taken)
class BotDodgeTests(HumanShotRoom):
    """★★ M5-E：真人朝 bot 打一发，它得**真的躲开**。

    共用 `HumanShotRoom` 那套手脚架：平地 + bot 站在 (600, 150)，
    `send()` 走真的 `0x040e` 入口。
    """

    def aim_at_bot(self, weapon_id=1002030, from_x=100.0):
        """真人在 `from_x` 处朝 bot 的身体圆心平射一发。"""
        weapon = weapondata.get(weapon_id)
        who = chrprops.get(self.room.seats[self.bot_seat].character_id)
        body = self.bot_conn.body
        cx, cy = who.center(body.x, body.y, False)
        mx, my = from_x, body.y - bot.BOT_MUZZLE_HEIGHT
        angle = math.atan2(cy - my, cx - mx)
        self.send(botsync.OP_FIRE, botsync.fire_body(
            self.alice_seat, weapon.id, mx, my, angle,
            ballistics.power_for_speed(weapon, weapon.velocity)))
        return weapon

    def threats(self):
        return bot._threats_against(self.room, self.bot_conn, self.bot_seat)

    def test_a_humans_shot_becomes_a_threat(self):
        self.aim_at_bot()
        threats = self.threats()
        self.assertEqual(1, len(threats))
        self.assertEqual(self.alice_seat, threats[0].seat)

    def test_it_gets_out_of_the_way(self):
        self.aim_at_bot()
        before = self.bot_conn.body.x
        self.beats(3, 100.0)
        self.assertNotAlmostEqual(before, self.bot_conn.body.x,
                                  msg="子弹飞过来了还站着不动")

    def test_the_action_it_picks_actually_avoids_the_shot(self):
        """★ 判据不是「动了」而是「这条弹道真的打不到我了」。"""
        self.aim_at_bot()
        terrain = mapdata.load(bot._current_map(self.room))
        who = chrprops.get(self.room.seats[self.bot_seat].character_id)
        threats = self.threats()
        now = time.monotonic()
        option = botthreat.choose(terrain, self.bot_conn.body, who,
                                  threats, now)
        self.assertIsNotNone(option)
        centers = botthreat.simulate(terrain, self.bot_conn.body, who, option)
        self.assertIsNone(botthreat.impact_tick(
            terrain, threats[0], now, centers,
            threats[0].danger_radius(who.size_body)))

    def test_a_shot_that_misses_anyway_is_not_dodged(self):
        """★ 打不到我就别乱动（真人 39% 的心跳是站着不动的，§71）。"""
        weapon = weapondata.get(1002030)
        body = self.bot_conn.body
        self.send(botsync.OP_FIRE, botsync.fire_body(
            self.alice_seat, weapon.id, 100.0, body.y - 400.0, 0.0,
            ballistics.power_for_speed(weapon, weapon.velocity)))
        before = self.bot_conn.body.x
        # ★ 也不能被「打不到就走过去」那条带跑 —— 把开火目标关掉隔离掉它。
        original = bot._fire_target
        bot._fire_target = lambda *a, **k: None
        self.addCleanup(setattr, bot, "_fire_target", original)
        self.bot_conn.stance = "press"
        self.beats(1, body.x)               # 真人站在 bot 脚下 -> 不用走
        self.assertAlmostEqual(before, self.bot_conn.body.x)

    def test_a_blind_roll_means_it_guesses_wrong(self):
        """★ `dodge_error` 掷中 ⇒ 随便挑一个，可能就是站着挨打。"""
        self.bot_conn.roll = lambda n: 0     # 必失误，且挑到第 0 个 = 站着
        self.aim_at_bot()
        self.beats(1, 100.0)
        self.assertIsNotNone(self.bot_conn.dodge_blind)
        self.assertIs(botthreat.STAND, self.bot_conn.dodge_blind)

    def test_the_dice_are_rolled_once_per_wave_not_per_frame(self):
        """★ 判据是「威胁集合变了没有」，不是每一帧重掷（铁律 10）。"""
        self.aim_at_bot()
        self.beats(1, 100.0)
        first = self.bot_conn.dodge_signature
        self.assertTrue(first)
        self.beats(2, 100.0)
        self.assertEqual(first, self.bot_conn.dodge_signature)
        self.aim_at_bot(from_x=120.0)        # 又来一发 -> 集合变了
        self.beats(1, 100.0)
        self.assertNotEqual(first, self.bot_conn.dodge_signature)

    def test_a_teammates_shot_is_not_dodged(self):
        """碰撞排除组相同的弹体压根撞不着自己（§63），躲它是白躲。"""
        self.room.seats[self.alice_seat].team = TEAM_A
        self.room.seats[self.bot_seat].team = TEAM_A
        original = self.room.team_layout
        self.room.team_layout = lambda: lobby.TEAM_LAYOUT_TEAMS
        self.addCleanup(setattr, self.room, "team_layout", original)
        self.aim_at_bot()
        self.assertEqual([], self.threats())

    def test_it_reports_the_second_jump_as_segment_two(self):
        """★ `rpJump` 的段号：地上那一下是 1，空中补的那一下是 2（§124）。"""
        machine = self.bot_conn
        machine.body = bot.botmove.Body(600.0, 150.0)
        terrain = mapdata.load(bot._current_map(self.room))
        who = chrprops.get(self.room.seats[self.bot_seat].character_id)
        body = bot.botmove.tick(terrain, machine.body, who, want_jump=True)
        self.assertFalse(body.on_ground)
        self.assertFalse(body.air_jumped)
        again = bot.botmove.tick(terrain, body, who, want_jump=True)
        self.assertTrue(again.air_jumped)
class BotItemHuntTests(HumanShotRoom):
    """★★ M5-F：**主动**去捡地上的道具，而不是只能顺路蹭到（§100 的另一半）。"""

    def drop_item(self, x, y=150.0, handle=900001, item_id=10300):
        quest = self.room.quest
        quest.items_at[handle] = (float(x), float(y))
        quest.item_handles[handle] = item_id
        quest.items_born[handle] = time.monotonic()
        quest.items_on_map.add(handle)
        return handle

    def no_shot(self):
        original = bot._fire_target
        bot._fire_target = lambda *a, **k: None
        self.addCleanup(setattr, bot, "_fire_target", original)

    def test_it_walks_to_an_item_on_the_way(self):
        self.no_shot()
        self.drop_item(500.0)                 # bot 在 600，敌人在 1300
        self.beats(3, 1300.0)                 # ★ 捕到“走过去”那一段；
        #   再多几帧它就把东西捡起来、又转头朝敌人走了。
        self.assertLess(self.bot_conn.body.x, 600.0,
                        "该先绕去捡道具，实际走到了 %.1f" % self.bot_conn.body.x)

    def test_an_item_further_than_the_enemy_is_not_worth_a_trip(self):
        self.no_shot()
        self.drop_item(100.0)                 # 比敌人（1300）还远
        self.beats(6, 800.0)
        self.assertGreater(self.bot_conn.body.x, 600.0)

    def test_it_does_not_leave_a_shot_to_grab_one(self):
        """★ 打得到人的时候不去蹲地上那件 —— 真人也不会当着枪口去捡。"""
        self.drop_item(500.0)
        self.beats(4, 800.0)
        self.assertAlmostEqual(600.0, self.bot_conn.body.x)

    def test_walking_onto_it_picks_it_up(self):
        self.no_shot()
        handle = self.drop_item(520.0)
        self.beats(12, 1300.0)
        self.assertNotIn(handle, self.room.quest.items_at)
        # ★ 拿到手就用掉了（D65），所以道具格里是空的 ——
        #   “谁捡走的”这本账在 `items_taken` 里。
        self.assertEqual(self.bot_seat,
                         self.room.quest.items_taken.get(handle))

    def test_an_expired_item_is_not_chased(self):
        """地上的东西只躺 13 秒（§118）—— 过期的不该再去追。"""
        self.no_shot()
        handle = self.drop_item(500.0)
        self.room.quest.items_born[handle] = time.monotonic() - 60.0
        self.beats(6, 1300.0)
        self.assertGreater(self.bot_conn.body.x, 600.0)


class BotSmokeFireTests(HumanShotRoom):
    """★★ M5-F：别人放了烟 ⇒ 挑不中人，但要**朝云团乱射**（用户 2026-08-29）。"""

    def smoke_over(self, x, y=150.0):
        self.room.quest.smokes.append(
            (float(x), float(y), time.monotonic() + gameserver.SMOKE_SECONDS))

    def test_someone_in_smoke_cannot_be_picked_as_a_target(self):
        """D67 那一半：云里的人挑不中。"""
        self.walk(self.alice, [(900.0, 150.0)])
        self.smoke_over(900.0)
        self.assertEqual([], bot._hostile_targets(self.room, self.bot_seat))

    def test_it_still_shoots_at_the_cloud(self):
        """★ 挑不中不等于不还手 —— 朝云团里一个随机点放。"""
        self.walk(self.alice, [(900.0, 150.0)])
        self.smoke_over(900.0)
        self.bot_conn.next_fire_at = 0.0
        target = bot._fire_target(self.room, self.bot_conn, self.bot_seat,
                                  self.bot_conn.weapon)
        self.assertIsNotNone(target, "有人躲在烟里就该朝烟乱放")
        point = target[1]
        self.assertLessEqual(
            math.hypot(point[0] - 900.0, point[1] - 150.0),
            gameserver.SMOKE_RADIUS * 1.5)

    def test_the_scatter_changes_from_shot_to_shot(self):
        """★ 「乱射」= 每一发换一个落点（偏移在打完一发之后重掷）。"""
        self.walk(self.alice, [(900.0, 150.0)])
        self.smoke_over(900.0)
        seen = set()
        for value in (0, 40, 90, 150):
            self.bot_conn.roll = lambda n, v=value: min(v, n - 1)
            bot._reroll_aim_miss(self.bot_conn)
            target = bot._fire_target(self.room, self.bot_conn, self.bot_seat,
                                      self.bot_conn.weapon)
            if target is not None:
                seen.add((round(target[1][0]), round(target[1][1])))
        self.assertGreater(len(seen), 1, "每一发都打在同一个点上就不叫乱射")

    def test_no_smoke_means_no_blind_fire(self):
        self.walk(self.alice, [(900.0, 150.0)])
        self.room.quest.arm_respawn_watchdog(self.alice_seat, (900.0, 150.0))
        self.assertIsNone(bot._smoke_aim(self.room, self.bot_conn,
                                         self.bot_seat, time.monotonic()))


class BotItemUseTests(HumanShotRoom):
    """★★ M5-F：**正确**使用道具 —— 回血药是唯一「满血先别喝」的那件。"""

    def hold(self, *item_ids):
        self.room.quest.item_slots[self.bot_seat] = list(item_ids)

    def test_a_full_health_bot_saves_the_medkit(self):
        self.hold(gameserver.HP_CHARGE_ITEM_ID)
        self.assertFalse(bot._use_held_item(self.room, self.bot_conn,
                                            self.bot_seat))
        self.assertEqual([gameserver.HP_CHARGE_ITEM_ID],
                         self.room.quest.item_slots[self.bot_seat])

    def test_it_uses_the_other_item_first(self):
        self.hold(gameserver.HP_CHARGE_ITEM_ID, 10300)
        self.assertTrue(bot._use_held_item(self.room, self.bot_conn,
                                           self.bot_seat))
        self.assertEqual([gameserver.HP_CHARGE_ITEM_ID],
                         self.room.quest.item_slots[self.bot_seat])

    def test_a_hurt_bot_drinks_it(self):
        bot._health(self.room).note_damage(self.bot_seat, 20)
        self.hold(gameserver.HP_CHARGE_ITEM_ID)
        self.assertTrue(bot._use_held_item(self.room, self.bot_conn,
                                           self.bot_seat))
        self.assertEqual([], self.room.quest.item_slots[self.bot_seat])

    def test_full_slots_beat_the_saving(self):
        """格子满了还留着就是把后面捡的全丢掉（`grant_item` 会返回 False）。"""
        self.hold(*([gameserver.HP_CHARGE_ITEM_ID]
                    * gameserver.ITEM_SLOT_COUNT))
        self.assertTrue(bot._use_held_item(self.room, self.bot_conn,
                                           self.bot_seat))

    def test_the_medkit_really_heals_the_ledger(self):
        """★ `Status.ini[8]`：8 秒 × 每秒 10 点。台账不跟着回，
        「谁先倒」就会一直按残血算（§122）。"""
        book = bot._health(self.room)
        book.note_damage(self.bot_seat, 50)
        self.hold(gameserver.HP_CHARGE_ITEM_ID)
        bot._use_held_item(self.room, self.bot_conn, self.bot_seat)
        charge = self.room.quest.hp_charges[self.bot_seat]
        self.assertEqual(8, charge[1])
        charge[0] = time.monotonic() - 0.001      # 把第一跳拨到过去
        bot._refresh_health(self.room)
        self.assertEqual(40.0, book.taken_by(self.bot_seat))

    def test_a_bots_smoke_is_registered_too(self):
        """★ 以前 bot 放的烟 / 冰冻 / 糊屏**一件都没登记过**（读的是
        `sync_trail`，bot 没有）—— 已经改成 `area_effect_origin()`。"""
        self.hold(gameserver.SMOKE_ITEM_ID)
        bot._use_held_item(self.room, self.bot_conn, self.bot_seat)
        self.assertTrue(self.room.quest.smokes)
        x, y, _until = self.room.quest.smokes[-1]
        self.assertAlmostEqual(self.bot_conn.battle_pos[0], x)

    def test_a_bots_hud_jam_lands_on_the_enemies(self):
        self.hold(gameserver.HUD_JAM_ITEM_ID)
        bot._use_held_item(self.room, self.bot_conn, self.bot_seat)
        self.assertIn(self.alice_seat, self.room.quest.hud_jam_until)
        self.assertNotIn(self.bot_seat, self.room.quest.hud_jam_until)
class BotQuestCombatTests(TerrainMixin, BotFrameRoom):
    """★★ M5-G：闯关房里也要**躲怪的子弹、打怪、打 boss**。

    ★★★ 怪的位置是**控制者广播的**（`rpAiMsg` 的 `setState`，§125）——
    和别人的客户端拿到的是同一份信息。会话 40 上半场曾经错误地断言
    「怪的位置一个包都不广播」，是用户 2026-08-29 的反问纠正的：
    「真人可以合作，其他人全都能看见怪的位置和血量，bot 应该也能」。
    """

    def setUp(self):
        super().setUp()
        self.install_terrain(synth_terrain("flat"))
        self.place_bot(600.0)
        self.alice_seat = self.room.seat_index_of(self.alice)
        self.bot_conn.act_lock_until = 0.0
        self.bot_conn.enter_lock_until = 0.0

    def human_packet(self, opcode, body):
        return botsync.build_peer_packet(
            self.alice_seat, opcode, body,
            game_id=self.room.epoch_value, sequence=self.next_seq(self.alice))

    def send(self, opcode, body):
        gameserver.Conn.on_game_packet(
            self.alice, OP_PEER_DATA_UP, self.human_packet(opcode, body))

    def ai_message(self, handle, **fields):
        """控制者替一只怪广播一发 `rpAiMsg`（明文 `key=value`）。"""
        text = "".join("%s=%s\r\n" % (k, v) for k, v in sorted(fields.items()))
        raw = text.encode("ascii")
        self.send(botsync.OP_AI_MSG,
                  struct.pack("<ii", handle, len(raw)) + raw)

    def spot_mob(self, handle=500123, x=880.0, y=120.0, state="chase"):
        self.ai_message(handle, msgType="setState", state=state,
                        posX="%.6f" % x, posY="%.6f" % y, targetChrSlot=1)
        return handle

    def mob_fires(self, x=900.0, y=120.0, ammo=1002010):
        """控制者那台机器替怪发的一发 `rpFire`（`body+0 == 20`，§23）。"""
        weapon = weapondata.get(ammo)
        angle = math.atan2(93.0 - y, 643.0 - x)
        body = botsync.fire_body(
            self.alice_seat, weapon.id, x, y, angle,
            ballistics.power_for_speed(weapon, weapon.velocity))
        body = bytes([bot.MOB_FIRE_SOURCES[0]]) + body[1:]
        self.send(botsync.OP_FIRE, body)
        return weapon

    # -- 怪物表 -------------------------------------------------------------
    def test_a_setstate_puts_the_mob_on_the_table(self):
        self.spot_mob()
        self.assertEqual([(880.0, 120.0, 500123)], bot.live_mobs(self.room))

    def test_the_table_also_keeps_what_it_is_doing(self):
        self.spot_mob(state="attack")
        row = self.room.quest.mobs[500123]
        self.assertEqual("attack", row[2])
        self.assertEqual(1, row[3], "targetChrSlot = 它在追谁")

    def test_a_death_message_takes_it_off(self):
        """★ 判据是**控制者报的那一发**，不是超时（铁律 10）。"""
        self.spot_mob()
        self.ai_message(500123, msgType="setState", state="death")
        self.assertEqual([], bot.live_mobs(self.room))

    def test_a_state_without_coordinates_keeps_the_last_known_spot(self):
        """boss 的几个阶段只报 state，不报坐标 —— 别把它挪到 (0,0)。"""
        self.spot_mob()
        self.ai_message(500123, msgType="setState", state="idle")
        self.assertEqual([(880.0, 120.0, 500123)], bot.live_mobs(self.room))

    def test_an_action_state_message_is_animation_only(self):
        self.ai_message(500123, msgType="setActionState", actionState=7)
        self.assertEqual([], bot.live_mobs(self.room))

    def test_a_spawn_point_is_not_a_mob(self):
        """`type=spawn` 是**刷怪点**在报（语料 26 个句柄只有 4 个后来是怪）。"""
        self.ai_message(700001, type="spawn", xpos="1410.000000",
                        ypos="81.000000", group="", bySpawn=0)
        self.assertEqual([], bot.live_mobs(self.room))

    def test_a_hit_refines_a_mob_we_already_know(self):
        self.spot_mob()
        self.send(botsync.OP_EXPLODE, botsync.explode_body(
            100002, 500123, 870.0, 118.0,
            hit_kind=botsync.HIT_CHARACTER, damage=12.0))
        self.assertEqual([(870.0, 118.0, 500123)], bot.live_mobs(self.room))

    def test_a_hit_on_a_breakable_invents_no_mob(self):
        """★ 破坏物的世界句柄在产物里（§136 / §139）—— 命中它**不**建怪物表。

        §125 当年「凭 `rpExplode` 建表会把箱子当怪」的顾虑，如今靠这张
        句柄表分流掉了；boss 房里剩下的非玩家句柄才是 boss 的部件。
        """
        self.ai_message(1100002, fileName="data/quest/quest03/"
                                        "quest03S6boss.ini", type="start")
        terrain = self.install_terrain(synth_terrain("hit_breakable"))
        terrain.breakables = (_FakeBreakable(0x30e00),)
        self.send(botsync.OP_EXPLODE, botsync.explode_body(
            100002, 0x30e00, 300.0, 140.0,
            hit_kind=botsync.HIT_CHARACTER, damage=12.0))
        self.assertEqual([], bot.live_mobs(self.room))

    def test_a_hit_outside_a_boss_room_invents_nothing(self):
        """★★★ 建表**只在 boss 房里放行**（复审抓的：普通关卡里命中的
        非玩家句柄还混着机关 / 场景物，建出来全是鬼目标）。

        普通关卡的怪全部走 AI 流（§125），用不着凭命中建表。
        """
        self.send(botsync.OP_EXPLODE, botsync.explode_body(
            100002, 1100276, 810.0, 120.0,
            hit_kind=botsync.HIT_CHARACTER, damage=16.0))
        self.assertEqual([], bot.live_mobs(self.room))

    def test_a_direct_hit_on_the_boss_builds_the_table(self):
        """★★★ boss 从不广播坐标（§141）—— 直接命中是它唯一的位置来源。

        实机 2026-08-30 第四轮：boss（AI 句柄 1100275）整场 72 发
        `rpAiMsg` 一发 posX 都没有；旧逻辑因此永远不给它建表，
        bot 进 boss 房一发不开。打中它的句柄是 1100276（AI 句柄 +1，
        收方扣血用的就是它）。
        """
        self.ai_message(1100002, fileName="data/quest/quest03/"
                                        "quest03S6boss.ini", type="start")
        self.send(botsync.OP_EXPLODE, botsync.explode_body(
            100002, 1100276, 810.0, 120.0,
            hit_kind=botsync.HIT_CHARACTER, damage=16.0))
        self.assertEqual([(810.0, 120.0, 1100276)], bot.live_mobs(self.room))
        # 再打一发，位置跟着采样走。
        self.send(botsync.OP_EXPLODE, botsync.explode_body(
            100003, 1100276, 830.0, 124.0,
            hit_kind=botsync.HIT_CHARACTER, damage=16.0))
        self.assertEqual([(830.0, 124.0, 1100276)], bot.live_mobs(self.room))

    def test_a_boss_seen_by_hit_is_shot_at(self):
        """建了表的 boss 就是打得着的目标（和普通怪同一条路）。"""
        self.ai_message(1100002, fileName="data/quest/quest03/"
                                        "quest03S6boss.ini", type="start")
        self.send(botsync.OP_EXPLODE, botsync.explode_body(
            100002, 1100276, 880.0, 120.0,
            hit_kind=botsync.HIT_CHARACTER, damage=16.0))
        target = bot._fire_target(self.room, self.bot_conn, self.bot_seat,
                                  self.bot_conn.weapon)
        self.assertIsNotNone(target, "看得见 boss 就该朝那儿打")
        self.assertEqual(bot.MOB_SEAT, target[0])
        self.assertAlmostEqual(880.0, target[1][0], places=3)

    def test_a_boss_script_start_marks_the_room(self):
        """★★ 进 boss 房那一刻控制者会广播 `fileName=…boss.ini`（§141）。"""
        self.ai_message(1100002, fileName="data/quest/quest03/"
                                        "quest03S1Enemy.ini", type="start")
        self.assertFalse(self.room.quest.boss_room,
                         "普通关卡的脚本不该置 boss 房")
        self.ai_message(1100002, fileName="data/quest/quest03/"
                                        "quest03S6boss.ini", type="start")
        self.assertTrue(self.room.quest.boss_room)
        # 换图跟着清：是不是 boss 房要等新图自己那一发再说。
        self.room.quest.begin_map_change("Quest03_1")
        self.assertFalse(self.room.quest.boss_room)

    def test_a_mobs_gun_refreshes_its_own_row(self):
        """★ 怪开枪那刻的枪口坐标是一次它的位置采样（§141）。"""
        self.spot_mob(x=880.0, y=120.0)
        self.mob_fires(x=900.0, y=120.0)
        self.assertEqual([(900.0, 120.0, 500123)], bot.live_mobs(self.room))

    def test_a_muzzle_far_from_any_row_moves_nothing(self):
        """★ 枪口离哪只怪都超过 `MOB_GUN_REACH`（300）就不挪 ——
        归属是「同一台车上的部件」这个几何事实，不是乱吸。"""
        self.spot_mob(x=880.0, y=120.0)
        self.mob_fires(x=1500.0, y=120.0)
        self.assertEqual([(880.0, 120.0, 500123)], bot.live_mobs(self.room))

    def test_a_mob_hit_is_not_charged_to_a_player_seat(self):
        self.spot_mob()
        self.send(botsync.OP_EXPLODE, botsync.explode_body(
            100002, 500123, 880.0, 120.0,
            hit_kind=botsync.HIT_CHARACTER, damage=12.0))
        self.assertEqual({}, bot._health(self.room).taken)

    # -- 怪的子弹 -----------------------------------------------------------
    def test_a_mob_shot_does_not_overwrite_the_humans_weapon(self):
        """★ 怪的枪是队友那台机器替它发的 —— 别把它记成「他现在用这把」。"""
        self.assertIsNone(getattr(self.alice, "peer_weapon", None))
        self.mob_fires()
        self.assertIsNone(getattr(self.alice, "peer_weapon", None))

    def test_a_mobs_bullet_is_dodged_even_though_a_teammate_sent_it(self):
        """★ 队友的子弹撞不着自己（§63），**怪的子弹撞得着** —— 得躲。"""
        self.mob_fires(x=100.0, y=93.0)
        threats = bot._threats_against(self.room, self.bot_conn, self.bot_seat)
        self.assertEqual(1, len(threats))

    def test_a_real_teammate_bullet_is_still_ignored(self):
        weapon = weapondata.get(1002010)
        self.send(botsync.OP_FIRE, botsync.fire_body(
            self.alice_seat, weapon.id, 100.0, 93.0, 0.0,
            ballistics.power_for_speed(weapon, weapon.velocity)))
        self.assertEqual([], bot._threats_against(
            self.room, self.bot_conn, self.bot_seat))

    # -- 打怪 ---------------------------------------------------------------
    def test_it_shoots_at_a_known_mob(self):
        self.spot_mob()
        target = bot._fire_target(self.room, self.bot_conn, self.bot_seat,
                                  self.bot_conn.weapon)
        self.assertIsNotNone(target, "看得见怪就该朝那儿打")
        self.assertEqual(bot.MOB_SEAT, target[0])
        self.assertAlmostEqual(880.0, target[1][0], places=3)

    def test_a_dead_mob_is_not_shot_at(self):
        self.spot_mob()
        self.ai_message(500123, msgType="setState", state="death")
        self.assertIsNone(bot._fire_target(
            self.room, self.bot_conn, self.bot_seat, self.bot_conn.weapon))

    def test_the_explosion_names_the_mob_handle_so_the_damage_lands(self):
        """★★ 收方按**句柄**扣血（§42）—— 填错就是「子弹飞过去不掉血」。"""
        self.spot_mob()
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.beats(2, 300.0)
        # ★ 把在飞的那几发推到头（`BotFireRoom.settle()` 的手法，
        #   这一批继承的是闯关房那个基类，没那个助手）。
        for shell in self.bot_conn.pending_shots:
            shell.born -= 10.0
        bot._advance_shells(self.room, self.bot_conn, time.monotonic())
        explodes = explode_frames(self.alice, self.bot_seat)
        self.assertTrue(explodes, "该打出一发并结算")
        targets = {struct.unpack_from("<i", body_of(f), 4)[0]
                   for f in explodes}
        self.assertIn(500123, targets)

    def test_a_new_map_wipes_the_table(self):
        self.spot_mob()
        self.room.quest.begin_map_change('ZZ_next')
        self.assertEqual([], bot.live_mobs(self.room))


# ---------------------------------------------------------------------------
# ★★★ 会话 41：视野 / 换枪冷却 / 没有敌人时的走位 / 闯关近身与记分
# ---------------------------------------------------------------------------
class BotVisionTests(TerrainMixin, BotFireRoom):
    """★★★ **屏幕外的人 bot 看不见**（§127）。

    用户 2026-08-30：「离得很远时 bot 都能朝我的方向准确开枪，这不合理。
    真人只能看见自己屏幕内的人。……为防止所有人都在范围外导致 bot 找不到人
    而不知道干什么，可以告诉 bot 敌人的粗略方位，上下左右这个粗略程度就够了。」

    视野框是**语料量出来的**（144 万发心跳里 `|准星 − 角色|` 的 p95），
    见 `bot.BOT_VISION_HALF_X` 的注释。
    """

    def setUp(self):
        super().setUp()
        self.install_terrain(synth_terrain("vision", width=4000))
        self.place_bot(200.0)

    def test_a_target_inside_the_box_is_visible(self):
        self.assertEqual((1024.0, 768.0),
                         (bot.BOT_VISION_HALF_X, bot.BOT_VISION_HALF_Y))
        self.assertTrue(bot._in_sight(self.bot_conn, 200.0 + 900.0, 150.0))
        self.assertTrue(bot._in_sight(self.bot_conn, 200.0, 150.0 - 500.0))

    def test_a_target_outside_the_box_is_not(self):
        self.assertFalse(
            bot._in_sight(self.bot_conn, 200.0 + bot.BOT_VISION_HALF_X + 1.0,
                          150.0))
        self.assertFalse(
            bot._in_sight(self.bot_conn, 200.0,
                          150.0 - bot.BOT_VISION_HALF_Y - 1.0))

    def test_it_does_not_shoot_someone_off_screen(self):
        """★★ 这就是用户报的那条：隔半张图也弹无虚发。"""
        far = 200.0 + bot.BOT_VISION_HALF_X + 400.0
        self.walk(self.alice, [(far, 150.0)])
        self.place_bot(200.0)
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.assertEqual([], bot._visible_targets(self.room, self.bot_conn,
                                                  self.bot_seat))
        self.assertIsNone(bot._fire_target(self.room, self.bot_conn,
                                           self.bot_seat,
                                           self.bot_conn.weapon))

    def test_a_target_that_walks_into_the_box_becomes_shootable(self):
        self.walk(self.alice, [(200.0 + 600.0, 150.0)])
        self.place_bot(200.0)
        self.bot_conn.next_fire_at = 0.0
        self.assertTrue(bot._visible_targets(self.room, self.bot_conn,
                                             self.bot_seat))

    def test_out_of_sight_leaves_only_a_rough_bearing(self):
        """★ 只剩上下左右：给出来的点在**视野边缘**上，不是敌人的真坐标。"""
        far = 200.0 + bot.BOT_VISION_HALF_X + 400.0
        self.walk(self.alice, [(far, 40.0)])
        self.place_bot(200.0)
        spot = bot._rough_bearing(self.room, self.bot_conn, self.bot_seat)
        self.assertIsNotNone(spot)
        self.assertEqual(200.0 + bot.BOT_VISION_HALF_X, spot[0])
        self.assertEqual(150.0, spot[1], "只能给右，不能泄露成右上对角线")
        self.assertNotEqual(far, spot[0], "不许把精确坐标喂给它")

    def test_the_same_direction_does_not_leak_distance(self):
        """同样在右边，超出 1 像素和超出 2000 单位得到的点完全相同。"""
        points = []
        for extra in (1.0, 2000.0):
            far = 200.0 + bot.BOT_VISION_HALF_X + extra
            self.alice.sync_trail.clear()
            self.alice.sync_trail.append((far, 150.0, 0))
            points.append(bot._rough_bearing(
                self.room, self.bot_conn, self.bot_seat))
        self.assertEqual(points[0], points[1])

    def test_vertical_bearing_has_no_horizontal_component(self):
        self.alice.sync_trail.clear()
        self.alice.sync_trail.append(
            (200.0, 150.0 + bot.BOT_VISION_HALF_Y + 100.0, 0))
        self.assertEqual((200.0, 150.0 + bot.BOT_VISION_HALF_Y),
                         bot._rough_bearing(self.room, self.bot_conn,
                                            self.bot_seat))

    def test_it_walks_toward_the_bearing(self):
        far = 200.0 + bot.BOT_VISION_HALF_X + 400.0
        self.walk(self.alice, [(far, 150.0)])
        self.place_bot(200.0)
        self.beats(4, far)
        self.assertGreater(self.bot_conn.body.x, 200.0,
                           "看不见也该朝那个方向挪")


class BotBlindHealthTests(TerrainMixin, BotFireRoom):
    """框内没敌人时的 25% / 50% 滞回与粗方向掩体。"""

    def setUp(self):
        super().setUp()
        self.terrain = self.install_terrain(synth_terrain("blind_hp", width=4000))
        self.place_bot(600.0)
        self.far = 600.0 + bot.BOT_VISION_HALF_X + 500.0
        self.walk(self.alice, [(self.far, 150.0)])
        self.place_bot(600.0)

    def set_health(self, fraction):
        ledger = bot._health(self.room)
        ledger.reset(self.bot_seat)
        maximum = bot._seat_max_hp(self.room, self.bot_seat)
        ledger.note_damage(self.bot_seat, maximum * (1.0 - fraction))

    def intent(self):
        return bot._blind_intent(self.room, self.bot_conn, self.bot_seat,
                                 self.terrain)

    def test_below_twenty_five_percent_hides(self):
        self.set_health(0.24)
        self.bot_conn.stance = "press"
        intent = self.intent()
        self.assertEqual("retreat", self.bot_conn.stance)
        self.assertLessEqual(intent[0], 0, "敌人在右边，低血不该继续往右送")

    def test_above_fifty_percent_presses(self):
        self.set_health(0.51)
        self.bot_conn.stance = "retreat"
        intent = self.intent()
        self.assertEqual("press", self.bot_conn.stance)
        self.assertEqual(1, intent[0])

    def test_middle_band_preserves_either_previous_stance(self):
        self.set_health(0.40)
        self.bot_conn.stance = "retreat"
        self.intent()
        self.assertEqual("retreat", self.bot_conn.stance)
        self.bot_conn.stance = "press"
        self.bot_conn.retreat_goal = None
        self.intent()
        self.assertEqual("press", self.bot_conn.stance)

    def test_exact_thresholds_do_not_flip(self):
        for fraction, stance in ((0.25, "press"), (0.50, "retreat")):
            self.set_health(fraction)
            self.bot_conn.stance = stance
            self.bot_conn.retreat_goal = None
            self.intent()
            self.assertEqual(stance, self.bot_conn.stance)

    def test_a_vertical_bearing_does_not_freeze_the_bot(self):
        """★★★ 上下方位不许把 bot 定在原地（用户 2026-09-01）。

        竖直粗方位是 `(自己的 x, y ± 视野高)`，`_walk_to()` 那句
        `direction = 0 if abs(delta_x) < 1.0` 于是恒为 0；A\\* 再解不出
        上下层的路（一张图的可达分量往往只覆盖一半，§137），就正好是
        用户报的「傻站着不动」。真人这时候是**横着走去找上去的那条路**。
        ★ 粗方位本身一个字没变 —— 泄露出去的仍然只有「上/下」。
        """
        above = 150.0 - bot.BOT_VISION_HALF_Y - 300.0
        for conn in (self.alice, self.bob):
            conn.sync_trail.clear()
            conn.sync_trail.append((600.0, above, 0))
        self.set_health(1.0)
        self.bot_conn.stance = "press"
        rough = bot._rough_bearing_raw(self.room, self.bot_conn, self.bot_seat)
        self.assertEqual(600.0, rough[0][0], "仍然只给上下，不泄露 x")
        self.assertEqual(0, rough[1])
        self.assertNotEqual(0, self.intent()[0],
                            "上下方位也得横着走去找上去的路")

    def test_a_boss_room_never_hides(self):
        """★ boss 房的门要**打死 boss 才开** —— 血再少也不许躲（§141）。"""
        self.set_health(0.05)
        self.bot_conn.stance = "retreat"
        intent = bot._blind_intent(self.room, self.bot_conn, self.bot_seat,
                                   self.terrain, may_hide=False)
        self.assertEqual("press", self.bot_conn.stance)
        self.assertEqual(1, intent[0], "血再少也要朝 boss 那个方向压过去")

    def test_low_health_uses_a_real_cover_when_available(self):
        self.terrain = self.install_terrain(synth_terrain(
            "blind_cover", width=4000, walls=((480, 520, 0),)))
        self.place_bot(600.0)
        self.set_health(0.10)
        self.bot_conn.stance = "press"
        self.intent()
        self.assertIsNotNone(self.bot_conn.retreat_goal)
        self.assertLess(self.bot_conn.retreat_goal[0], 520.0,
                        "应该走到左边、让墙挡在自己和右方敌人之间")


class BotIdleRepositionTests(TerrainMixin, BotFireRoom):
    """★★ 敌人全躺下时**不站着发呆**，去敌方出生点等（§128）。

    用户 2026-08-30：「敌人死后，bot 就停下不动了。我希望改成像真人一样，
    会自己走位，提前寻找有利地形。」
    """

    def setUp(self):
        super().setUp()
        self.install_terrain(synth_terrain(
            "idle_spawn", width=2000,
            points={bot.SPAWN_TYPE_TEAM_A: [(1500, 40)],
                    bot.SPAWN_TYPE_TEAM_B: [(1700, 40)]}))
        self.place_bot(200.0)

    def kill_everyone(self):
        for conn in (self.alice, self.bob):
            self.room.quest.arm_respawn_watchdog(
                self.room.seat_index_of(conn), (300.0, 150.0), after=99.0)

    def test_the_watch_spot_is_an_enemy_spawn_point(self):
        spot = bot._spawn_watch_spot(self.room, self.bot_conn, self.bot_seat,
                                     mapdata.load(self.room.map_name))
        self.assertIn(spot, [(1500.0, 40.0), (1700.0, 40.0)])

    def test_it_keeps_moving_after_everyone_is_down(self):
        self.kill_everyone()
        self.assertEqual([], bot._hostile_targets(self.room, self.bot_seat))
        start = self.bot_conn.body.x
        self.beats(6, 300.0)
        self.assertGreater(self.bot_conn.body.x, start,
                           "敌人全躺着也该走位，不该杵在原地")


class BotWeaponCooldownTests(BotFireRoom):
    """★★★ 换枪之后要**上膛**，而且每把枪的冷却是各算各的（§126）。

    用户 2026-08-30：「bot 换枪后立刻就能开枪，这不合理。……第一个 cd 是
    固定的 cd，更换武器后有 1 秒左右无法开枪，第二个 cd 是武器原本的 cd。
    每个角色的 3 种武器的 cd 是单独计算的，切换到别的武器后上一个武器的
    cd 进度就会暂停，下次再切换回这个武器后会继续走完之前的 cd。」

    原版实现见 `bot._switch_weapon_clock()` 的 docstring（逐指令）。
    """

    def machine(self):
        return self.bot_conn

    def test_loading_time_is_the_original_per_weapon_number(self):
        self.assertEqual(200, weapondata.get(1000010).loading_ms)
        self.assertEqual(300, weapondata.get(1000020).loading_ms)
        self.assertEqual(400, weapondata.get(1000030).loading_ms)

    def test_switching_arms_the_loading_time(self):
        machine = self.machine()
        machine.declared_weapon = 1000010
        machine.next_fire_at = 0.0
        now = time.monotonic()
        bot._switch_weapon_clock(machine, 1000010, weapondata.get(1000030),
                                 now)
        self.assertAlmostEqual(now + 0.400, machine.next_fire_at, places=3)

    def test_the_old_weapon_cooldown_freezes_and_resumes(self):
        """★★★ 切走暂停、切回来接着走完 —— 用户描述的那条。"""
        machine = self.machine()
        now = time.monotonic()
        machine.declared_weapon = 1000010
        machine.next_fire_at = now + 1.0          # 1 号枪还剩 1 秒冷却
        bot._switch_weapon_clock(machine, 1000010, weapondata.get(1000020),
                                 now)
        self.assertAlmostEqual(1.0, machine.weapon_cd[1000010], places=3)
        # 过了 10 秒（1 号枪没在手上，那 1 秒**一点都没走**）
        later = now + 10.0
        bot._switch_weapon_clock(machine, 1000020, weapondata.get(1000010),
                                 later)
        # 剩下的 1 秒 + 1 号枪自己的上膛 0.2 秒
        self.assertAlmostEqual(later + 1.2, machine.next_fire_at, places=3)

    def test_the_magazine_travels_with_the_weapon(self):
        machine = self.machine()
        now = time.monotonic()
        machine.declared_weapon = 1000010
        machine.rounds_left = 3
        bot._switch_weapon_clock(machine, 1000010, weapondata.get(1000020),
                                 now)
        self.assertIsNone(machine.rounds_left, "新枪是满弹匣")
        bot._switch_weapon_clock(machine, 1000020, weapondata.get(1000010),
                                 now)
        self.assertEqual(3, machine.rounds_left, "切回来还是剩 3 发")

    def test_a_switch_cannot_fire_in_the_same_frame(self):
        """★★ 端到端：刚换完枪那一帧一发都打不出去。"""
        machine = self.machine()
        self.approach()
        self.clear()
        machine.next_fire_at = 0.0
        # ★ D106：声明武器要在**这一格的时刻**上算（`_now()`），否则拿的是
        #   挂钟 —— 单测里挂钟比模拟时钟慢好几秒，`LoadingTime` 一上来就过期了。
        with bot._tick_clock(self.now()):
            bot._declare_weapon(machine, self.bot_seat,
                                weapondata.get(1000030))
        self.assertGreater(machine.next_fire_at, self.now())
        self.advance(1)
        self.assertEqual([], fire_frames(self.alice, self.bot_seat))


class BotQuestMeleeTests(BotQuestCombatTests):
    """★★ 闯关房里近身招式也该对**怪**发得出来（§129）+ 打怪要记分（§130）。

    用户 2026-08-30：「闯关模式下，bot 似乎不会用近战招式，怪都贴脸了，
    bot 都不发动近战招式。」「bot 打怪后，右上角 bot 没有分数。」
    """

    def setUp(self):
        super().setUp()
        self.bot_conn.melee = True
        self.bot_conn.stamina = 100.0

    def test_a_mob_next_to_it_is_a_melee_target(self):
        self.spot_mob(x=640.0, y=150.0)          # bot 站在 600
        bodies = bot._melee_bodies(self.room, self.bot_conn,
                                   self.bot_seat, targeting=True)
        self.assertTrue(any(isinstance(row[0], tuple) for row in bodies),
                        "怪该进近身名单")
        move = chrprops.get(self.bot_conn.character_id).dash(bot.BOT_DASH_INDEX)
        self.assertIsNotNone(
            bot._dash_target(self.room, self.bot_conn, self.bot_seat, move))

    def test_a_mob_off_screen_is_not(self):
        self.spot_mob(x=600.0 + bot.BOT_VISION_HALF_X + 100.0, y=150.0)
        self.assertEqual(
            [], [row for row in bot._melee_bodies(self.room, self.bot_conn,
                                                  self.bot_seat,
                                                  targeting=True)
                 if isinstance(row[0], tuple)])

    def test_hitting_a_mob_scores(self):
        """★ 分数 = 打在怪身上的伤害（语料：加分增量众数就是武器伤害）。"""
        before = int(self.bot_conn.quest_score)
        bot._score_quest_damage(self.room, self.bot_conn, 22)
        self.assertEqual(before + 22, self.bot_conn.quest_score)

    def test_the_score_is_broadcast_so_the_panel_shows_it(self):
        self.clear()
        bot._score_quest_damage(self.room, self.bot_conn, 18)
        codes = opcodes(self.alice)
        self.assertIn(gameserver.OP_REP_QUEST_SCORE, codes)

    def test_a_pvp_room_does_not_use_the_quest_score(self):
        """对战房记的是杀敌数（§167），这一格不该动。"""
        room = self.room
        original = room.team_layout
        room.team_layout = lambda: lobby.TEAM_LAYOUT_FREE
        self.addCleanup(setattr, room, "team_layout", original)
        before = int(self.bot_conn.quest_score)
        bot._score_quest_damage(room, self.bot_conn, 22)
        self.assertEqual(before, self.bot_conn.quest_score)


class BotPlanBudgetTests(TerrainMixin, BotFireRoom):
    """★★★ A* 一次都不在游戏线程上跑（会话 42 / §137）。

    卡顿的根：bot 的帧挂在**真人的同步转发路径**上，A* 在那儿跑多久，
    真人屏幕就卡多久。会话 41 用边缓存把「够得着的目标」压到 0.14 ms，
    但「够不着的目标」仍然要泛洪整片可达分量（`Quest03_1` 实测 22 ms，
    ×3 个 bot ×每一帧）。会话 42 整个搬到后台线程（`botplan`）。
    """

    def setUp(self):
        super().setUp()
        self.install_terrain(synth_terrain(
            "budget", width=2400, walls=((1200, 2400, 40),)))
        self.place_bot(200.0)

    def test_the_game_thread_never_runs_a_star(self):
        """★★★ 游戏线程上 `botnav.plan()` **一次都不许被调到**。"""
        here = threading.current_thread()
        offenders = []
        original = bot.botnav.plan

        def watched(*args, **kwargs):
            if threading.current_thread() is here:
                offenders.append(1)
            return original(*args, **kwargs)

        bot.botnav.plan = watched
        self.addCleanup(setattr, bot.botnav, "plan", original)
        self.beats(6, 2200.0, 40.0)
        self.assertEqual([], offenders,
                         "A* 又跑回真人的转发线程上了")

    def test_one_frame_asks_for_a_route_at_most_once(self):
        """一帧最多递**一张**单子 —— 逐 tick 问意图不该变成逐 tick 递单。"""
        asked = []
        original = bot.botplan.ask

        def counted(*args, **kwargs):
            asked.append(1)
            return original(*args, **kwargs)

        bot.botplan.ask = counted
        self.addCleanup(setattr, bot.botplan, "ask", original)
        self.beats(1, 2200.0, 40.0)
        self.assertLessEqual(len(asked), 1,
                             f"一帧递了 {len(asked)} 张单子")

    def test_decisions_are_capped_at_a_human_reaction_rate(self):
        """★★★ **不逐格**重新决策（用户 2026-08-30）。

        「真人也不可能脑内计算那么快，每秒 10 到 20 次就够了」。
        D106 之后物理是 32 ms 一格（收方的步长，改不得），而 AI 还是
        `BOT_DECISIONS_PER_SECOND` 那个节奏 —— 判据一个都没改，只是问得
        没那么密。
        """
        self.assertLessEqual(10.0, bot.BOT_DECISIONS_PER_SECOND)
        self.assertLessEqual(bot.BOT_DECISIONS_PER_SECOND, 20.0)
        calls = []
        original = bot._move_intent

        def counted(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        bot._move_intent = counted
        self.addCleanup(setattr, bot, "_move_intent", original)
        ticks = 16
        self.human_heartbeat(self.alice, 2200.0, 40.0, ticks=ticks)
        ceiling = -(-ticks // bot.BOT_DECISION_TICKS)
        self.assertLessEqual(len(calls), ceiling,
                             f"{ticks} 格里决策了 {len(calls)} 次")

    def test_one_tick_is_one_tick(self):
        """★★★ 一格就是一格：推一格，身体最多挪一格的步长（D106）。

        以前这里防的是「服务端卡住之后把攒下的位移一次性推出去」——
        那个累加器（`move_at`）随 D106 一起没了：房间循环落后时是**逐格
        补跑**的，一格一格都真的走过，不存在「一次挪一大步」。
        """
        self.beats(1, 2200.0, 40.0)
        before = self.bot_conn.body
        self.advance(1)
        after = self.bot_conn.body
        ceiling = botmove.walk_speed(
            chrprops.get(self.bot_conn.character_id), fast_run=True) + 1.0
        self.assertLessEqual(abs(after.x - before.x), ceiling,
                             f"一格横着挪了 {abs(after.x - before.x):.0f} 个单位")


class BotDoubleJumpNavTests(TerrainMixin, BotFireRoom):
    """★★★ **两段跳才上得去的高台**（会话 41 / §131）。

    用户 2026-08-30：「我在的平台位置比较高时，bot 不会自己找附近的弹跳
    平台跳上去，只会在我下面的平台自己来回跳，看起来有点笨。」

    根因：`botnav` 的可达图里只有**一段**跳（顶点 `20²/2.4 = 167`），
    比它高的平台在图里就是「不可达」。实测 `Iceria02` 上从任何一个出生点
    泛洪，y 一路只到 396 —— 上面那一整层（出生点表里写着 y=255）
    bot 一辈子上不去。加上二段跳边之后泛洪到 y=244。
    """

    def setUp(self):
        super().setUp()
        self.original_fire = bot._fire_target
        bot._fire_target = lambda *a, **k: None
        self.addCleanup(setattr, bot, "_fire_target", self.original_fire)

    def test_a_platform_above_one_jump_is_still_reachable(self):
        # 台面比地面高 240 —— 一段跳（167）够不着，两段跳（≈407）够得着。
        self.install_terrain(synth_terrain(
            "nav_double", floor=300, height=340, walls=((700, 1400, 60),)))
        self.place_bot(560.0, 300.0)
        self.beats(60, 900.0, 60.0)
        body = self.bot_conn.body
        self.assertTrue(body.on_ground)
        self.assertAlmostEqual(60.0, body.y,
                               msg="两段跳该把它送上高台，实际停在 y=%.0f"
                                   % body.y)

    def test_the_second_stage_is_reported_as_stage_two(self):
        """★ `rpJump` 的段号要报 2（§124），别人屏幕上才画得出第二段。"""
        self.install_terrain(synth_terrain(
            "nav_double", floor=300, height=340, walls=((700, 1400, 60),)))
        self.place_bot(560.0, 300.0)
        self.beats(60, 900.0, 60.0)
        stages = [body_of(f)[1] for f in bot_frames(self.alice, self.bot_seat)
                  if header(f)["opcode"] == botsync.OP_JUMP]
        self.assertIn(2, stages, "应该发过一发第 2 段的 rpJump")


class BotQuestSplashTests(BotQuestCombatTests):
    """★★★ **闯关模式里溅射 / 地面燃烧也该打得到怪**（V0.3 §134）。

    用户 2026-08-30：「你说的闯关模式溅射打怪问题也要修。」

    根因：`_splash_targets()` / `_advance_fires()` 的名单只有
    `_battle_bodies()`（= 座位），而原版溅射对象和火墙的碰撞组是
    「撞所有人」—— 怪当然也在里面。于是 bot 的手雷炸在一堆怪中间
    一滴血都不掉，燃烧瓶铺完火一只怪都烧不到。
    """

    def shell_at(self, x, y, ammo=1002020):
        """在 `(x, y)` 造一颗**已经飞到头**的弹体（只用来验结算）。"""
        weapon = weapondata.get(ammo)
        shot = ballistics.solve(weapon, 100.0, 0.0)
        shell = bot.Shell(handle=300099, fire_seq=0, weapon=weapon,
                          group=bot._seat_group(self.room, self.bot_seat),
                          x0=x, y0=y, shot=shot, born=time.monotonic(),
                          max_ticks=200)
        shell.x, shell.y = x, y
        return shell

    def test_splash_reaches_a_mob(self):
        self.spot_mob(handle=500123, x=880.0, y=120.0)
        self.clear()
        shell = self.shell_at(880.0, 120.0)
        bot._resolve_shell(self.room, self.bot_conn, shell,
                           (900.0, 120.0), None, None, 0)
        targets = {struct.unpack_from("<i", body_of(f), 4)[0]
                   for f in splash_frames(self.alice, self.bot_seat)}
        self.assertIn(500123, targets, "手雷炸在怪旁边该溅到它")

    def test_the_directly_hit_mob_is_not_splashed_twice(self):
        self.spot_mob(handle=500123, x=880.0, y=120.0)
        self.clear()
        shell = self.shell_at(880.0, 120.0)
        bot._resolve_shell(self.room, self.bot_conn, shell,
                           (880.0, 120.0), ("mob", 500123), None, 0)
        targets = [struct.unpack_from("<i", body_of(f), 4)[0]
                   for f in splash_frames(self.alice, self.bot_seat)]
        self.assertNotIn(500123, targets,
                         "直接命中的那只已经吃过 rpExplode 那一档了")

    def test_splashing_a_mob_scores(self):
        self.spot_mob(handle=500123, x=880.0, y=120.0)
        before = int(self.bot_conn.quest_score)
        shell = self.shell_at(880.0, 120.0)
        bot._resolve_shell(self.room, self.bot_conn, shell,
                           (900.0, 120.0), None, None, 0)
        self.assertGreater(self.bot_conn.quest_score, before)

    def test_a_mob_far_from_the_blast_is_untouched(self):
        self.spot_mob(handle=500123, x=880.0, y=120.0)
        self.clear()
        shell = self.shell_at(880.0, 120.0)
        bot._resolve_shell(self.room, self.bot_conn, shell,
                           (3000.0, 120.0), None, None, 0)
        targets = [struct.unpack_from("<i", body_of(f), 4)[0]
                   for f in splash_frames(self.alice, self.bot_seat)]
        self.assertNotIn(500123, targets)

    def test_ground_fire_burns_a_mob(self):
        """★ 燃烧瓶铺的那道火墙同理（火墙的碰撞组是 255 = 撞所有人）。"""
        self.spot_mob(handle=500123, x=880.0, y=150.0)
        weapon = weapondata.get(1001020)          # ch01-02 燃烧瓶
        bot._set_ground_on_fire(self.room, self.bot_conn, weapon,
                                (880.0, 150.0), time.monotonic())
        self.assertTrue(self.bot_conn.fires, "该铺出一道火墙")
        self.clear()
        self.bot_conn.burnt = {}
        for wall in self.bot_conn.fires:
            wall.born_tick -= 40
        bot._advance_fires(self.room, self.bot_conn, time.monotonic())
        targets = {struct.unpack_from("<i", body_of(f), 4)[0]
                   for f in splash_frames(self.alice, self.bot_seat)}
        self.assertIn(500123, targets, "站在火里的怪该掉血")

    # -- ★★★ 反过来：闯关房里**角色一个都不该挨打**（§142） --------------
    #
    # 用户 2026-08-30：「任务模式中真人之间是没有队友伤害的，包括溅射伤害和
    # 燃烧弹的火墙……但我发现 bot 的溅射和火墙会对队友造成伤害，屏幕上甚至
    # 还会提示 bot1 击杀 bot2。」
    #
    # 语料实证：8 个闯关局的「怪 AI 广播密集时段」里，玩家弹体发出的
    # 3092 发 `rpSplashDamaged` + 2034 发 `rpExplode`，受害者**没有一发**
    # 是角色句柄（连自伤都没有）；打到角色的 1337 发全部是怪的弹。

    def human_at(self, x, y):
        """把真人摆到 `(x, y)`（心跳走真入口，和 `_seat_body()` 同一口径）。"""
        self.human_heartbeat(self.alice, x, y)
        return botsync.character_handle(self.alice_seat)

    def test_a_quest_room_has_no_bodies_to_damage(self):
        """那道门本身：闯关房里「碰得着谁」一个角色都不返回。"""
        self.human_at(880.0, 150.0)
        self.assertEqual(lobby.TEAM_LAYOUT_COOP, self.room.team_layout())
        self.assertEqual([], bot._battle_bodies(
            self.room, self.bot_seat, include_self=True),
            "任务模式没有任何角色间伤害 —— 溅射 / 火墙 / 弹体全问这一个函数")

    def test_splash_spares_a_human_teammate(self):
        handle = self.human_at(880.0, 150.0)
        self.clear()
        shell = self.shell_at(880.0, 150.0)
        bot._resolve_shell(self.room, self.bot_conn, shell,
                           (890.0, 150.0), None, None, 0)
        targets = {struct.unpack_from("<i", body_of(f), 4)[0]
                   for f in splash_frames(self.alice, self.bot_seat)}
        self.assertNotIn(handle, targets, "任务模式的溅射伤不到队友")

    def test_splash_spares_the_shooter_itself(self):
        """★ 自伤也没有 —— 闯关房里射手自己也是「队友」。"""
        self.human_at(200.0, 150.0)
        self.clear()
        shell = self.shell_at(600.0, 150.0)
        bot._resolve_shell(self.room, self.bot_conn, shell,
                           (600.0, 150.0), None, None, 0)
        mine = botsync.character_handle(self.bot_seat)
        targets = {struct.unpack_from("<i", body_of(f), 4)[0]
                   for f in splash_frames(self.alice, self.bot_seat)}
        self.assertNotIn(mine, targets, "炸在自己脚下也不该掉血")

    def test_ground_fire_spares_a_human_teammate(self):
        """★ 火墙照铺、照烧怪，站在火里的**人**一点都不掉（§142）。"""
        handle = self.human_at(880.0, 150.0)
        weapon = weapondata.get(1001020)          # ch01-02 燃烧瓶
        bot._set_ground_on_fire(self.room, self.bot_conn, weapon,
                                (880.0, 150.0), time.monotonic())
        self.assertTrue(self.bot_conn.fires, "火墙照铺 —— 少的只是角色伤害")
        self.clear()
        self.bot_conn.burnt = {}
        for wall in self.bot_conn.fires:
            wall.born_tick -= 40
        bot._advance_fires(self.room, self.bot_conn, time.monotonic())
        targets = {struct.unpack_from("<i", body_of(f), 4)[0]
                   for f in splash_frames(self.alice, self.bot_seat)}
        self.assertNotIn(handle, targets, "任务模式的火墙烧不到队友")


class BotGapJumpTests(TerrainMixin, BotFrameRoom):
    """★★★ 坑太宽时**兜底也要会二段跳**（§144）。

    用户 2026-08-30：「经过岩浆时，bot 似乎不会用二段跳来跳到对面平台，
    只会用一段跳，然后反复掉进岩浆。」

    二段跳在 A\* 那边一直是有边的（§124），缺的是**兜底那条路**：A\* 在
    后台线程上跑（§137），算好之前 / 说「到不了」时走的都是
    `_walk_to()` 末尾那几行，而它以前只问过一段跳。
    ★ 量出来的跨度（floor=700 的合成图，够高才不会撞天花板）：
    一段跳 264、二段跳 **488** —— 差不多两倍。
    """

    def gap_room(self, key, gap):
        """左平台 + 宽 `gap` 的无底洞 + 右平台；bot 站在洞左边一步远。"""
        terrain = self.install_terrain(synth_terrain(
            key, floor=700, width=1600, height=800, pits=((700, 700 + gap),)))
        # ★ 贴着洞口站：`drop_below()` 只看**下一步**踩不踩得空，站远了
        #   这一帧的答案是「脚下还是平地」，根本走不到跳那一段。
        self.place_bot(697.0, 700.0)
        return terrain

    def walk_east(self, terrain):
        return bot._walk_to(self.room, self.bot_conn, terrain,
                            (1500.0, 700.0), False)

    def test_a_narrow_gap_is_a_plain_jump(self):
        terrain = self.gap_room("gap_narrow", 180)
        intent = self.walk_east(terrain)
        self.assertEqual((1, True), intent[:2], "一段跳够得着就一段跳")
        self.assertFalse(self.bot_conn.nav_double_jump,
                         "够得着就别白按第二段")

    def test_a_wide_gap_uses_the_second_jump(self):
        terrain = self.gap_room("gap_wide", 300)
        self.assertIsNone(
            botmove.jump_lands(terrain, self.bot_conn.body,
                               chrprops.get(self.bot_conn.character_id), 1),
            "这个宽度一段跳本来就过不去（前提没了测试就不成立）")
        intent = self.walk_east(terrain)
        self.assertEqual((1, True), intent[:2], "该起跳")
        self.assertTrue(self.bot_conn.nav_double_jump,
                        "一段跳过不去 -> 这一跳要在顶点补第二段")

    def test_an_impossible_gap_still_stops_at_the_edge(self):
        """★ 两段都过不去就**别走** —— 这条闸不能被新逻辑拆掉。"""
        terrain = self.gap_room("gap_huge", 900)
        intent = self.walk_east(terrain)
        self.assertEqual(0, intent[0], "过不去就站住，别往坑里走")
        self.assertFalse(intent[1])

    def test_the_second_stage_is_pressed_at_the_apex(self):
        """★★ 起跳之后：升段不按、**顶点**按一次、按过就不再按。"""
        terrain = self.gap_room("gap_apex", 300)
        self.walk_east(terrain)
        self.assertTrue(self.bot_conn.nav_double_jump)
        who = chrprops.get(self.bot_conn.character_id)
        body = botmove.tick(terrain, self.bot_conn.body, who,
                            direction=1, want_jump=True)
        pressed = []
        for _ in range(40):
            if body.on_ground:
                break
            self.bot_conn.body = body
            intent = self.walk_east(terrain)
            pressed.append(bool(intent[1]))
            body = botmove.tick(terrain, body, who, want_jump=intent[1])
        self.assertEqual(1, pressed.count(True),
                         "第二段只该按一次，实际按了 %d 次" % pressed.count(True))
        self.assertFalse(pressed[0], "刚离地还在往上升，这时候按是浪费")

    # -- ★★★ §146：「目标在上面」不等于「现在就该跳」 --------------------

    def test_a_higher_goal_alone_does_not_trigger_a_jump(self):
        """★★★ 用户 2026-08-30：「经常跳早了，导致跳不过去。」

        `_walk_to()` 最后那条兜底原来是**无条件**起跳的：跟随点只要比自己
        高一点（对岸台子高 75 就够），bot 就在**离坑还有 150 的平地上**
        起跳，弧线飞到对岸时早已低于台面 —— 一头栽进岩浆。
        ⇒ 先问一句「这一跳落得住吗」，落不住就接着走。
        """
        terrain = self.gap_room("gap_high_goal", 300)
        self.place_bot(500.0, 700.0)          # 离坑边（700）还有 200
        intent = bot._walk_to(self.room, self.bot_conn, terrain,
                              (1500.0, 500.0), False)   # 目标在**上方**
        self.assertEqual(1, intent[0], "该继续往前走")
        self.assertFalse(intent[1],
                         "离坑还有 200 就起跳 = 跳早了，弧线一定栽进坑里")

    def test_a_wall_in_the_way_still_gets_a_jump(self):
        """★ 「该跳的还得跳」—— 上面那条闸不能顺手把墙根跳也关掉。

        （墙根跳走的是 `blocked` 那一支，和「目标在上面」那条无关；
        这里守的是「改一条兜底别把另一条带塌」。）
        """
        terrain = self.install_terrain(synth_terrain(
            "gap_wall", floor=700, width=1600, height=800,
            walls=((600, 1600, 560),)))       # 前方一道 140 高的坎
        self.place_bot(598.0, 700.0)          # 贴着坎站，走一步撞上去
        who = chrprops.get(self.bot_conn.character_id)
        self.assertTrue(
            botmove.blocked(terrain, self.bot_conn.body, who, 1),
            "这道坎该是走不上去的（前提没了测试就不成立）")
        intent = bot._walk_to(self.room, self.bot_conn, terrain,
                              (900.0, 560.0), False)
        self.assertEqual(1, intent[0])
        self.assertTrue(intent[1], "撞墙了就该起跳")

    def test_it_crosses_the_lava_pit_on_the_real_map(self):
        """★★★★ 端到端回归：**岩浆巨龙**那张图第一个坑（宽 327）。

        用户实机报的就是这里：bot 一遍遍跳早、一遍遍掉进岩浆
        （服务端日志 `★掉出地图: (3009, 767)` 那一串）。对岸比这岸高 75，
        所以「目标在上面」那条兜底会在 x≈2700 就让它起跳。
        """
        terrain = mapdata.load("Quest02_1", difficulty=2)
        if terrain is None:
            self.skipTest("没有 Quest02_1 的地形产物")
        self.install_terrain(terrain)
        self.walk(self.alice, [(3400.0, 378.0), (3500.0, 378.0)])
        self.place_bot(2700.0, 453.0)         # 坑左沿再往回 150
        for _ in range(40):
            self.beats(1, 3500.0, 378.0)
            body = self.bot_conn.body
            self.assertLess(body.y, 700.0,
                            "掉进岩浆了：(%.0f, %.0f)" % (body.x, body.y))
            if body.x > 3177.0 and body.on_ground:
                break
        else:
            self.fail("40 帧还没跨过去，停在 (%.0f, %.0f)"
                      % (self.bot_conn.body.x, self.bot_conn.body.y))

    def test_landing_clears_the_flag(self):
        """★ 落地之后旗子要清 —— 不然下次从台阶走下去会白跳一段。"""
        terrain = self.gap_room("gap_clear", 300)
        self.walk_east(terrain)
        self.assertTrue(self.bot_conn.nav_double_jump)
        self.place_bot(400.0, 700.0)          # 重新站到平地中间
        bot._walk_to(self.room, self.bot_conn, terrain, (405.0, 700.0), False)
        self.assertFalse(self.bot_conn.nav_double_jump)

    # -- ★★★★ V0.3 §151：腾空途中不许把二段跳那面旗抹掉 ------------------

    def test_clearing_navigation_in_mid_air_keeps_the_second_jump(self):
        """★★★★ 飞在坑上方时被 `_clear_navigation()` 一抹，第二段就没了。

        `_move_intent()` 里有一串**不看在不在空中**的早退分支都会调它
        （躲子弹 / 打得到就站住 / 后撤 / 闯关那几条）。命中任何一条 ⇒
        一段跳掉进岩浆。判据改成「在不在地上」这个事实。
        """
        terrain = self.gap_room("gap_midair_clear", 300)
        self.walk_east(terrain)               # 起跳，挂上「这一段要补第二跳」
        self.assertTrue(self.bot_conn.nav_double_jump)
        who = chrprops.get(self.bot_conn.character_id)
        self.bot_conn.body = botmove.tick(terrain, self.bot_conn.body, who,
                                          direction=1, want_jump=True)
        self.assertFalse(self.bot_conn.body.on_ground, "这会儿人该在天上")
        bot._clear_navigation(self.bot_conn)
        self.assertTrue(self.bot_conn.nav_double_jump,
                        "腾空中路线可以作废，但这一段飞行欠的第二跳不能作废")

    def test_clearing_navigation_on_the_ground_still_clears_it(self):
        """★ 反过来：脚踩着地时照旧清 —— 那面旗说的是「正在进行的这一段」。"""
        terrain = self.gap_room("gap_ground_clear", 300)
        self.walk_east(terrain)
        self.assertTrue(self.bot_conn.nav_double_jump)
        self.assertTrue(self.bot_conn.body.on_ground)
        bot._clear_navigation(self.bot_conn)
        self.assertFalse(self.bot_conn.nav_double_jump)

    def test_dodging_in_mid_air_does_not_eat_the_second_jump(self):
        """★★★★ 端到端：飞越坑的途中触发躲子弹，第二段照样补得上。"""
        terrain = self.gap_room("gap_dodge_midair", 300)
        tick = self.loop().done
        # 先按兜底那条真起跳（旗子跟着挂上）。
        self.bot_conn.intent = self.walk_east(terrain)
        self.assertTrue(self.bot_conn.nav_double_jump)
        bot._own_step(self.room, self.bot_conn, self.bot_seat, terrain,
                      self.now(), tick)
        self.assertFalse(self.bot_conn.body.on_ground, "这一格该离地了")
        # 人已经在天上了，这时候装一张「一定会躲」的桩 —— 它会让
        # `_move_intent()` 每一格都早退，`_walk_to()` 再也轮不到。
        original = bot._dodge_intent
        bot._dodge_intent = lambda *_a, **_k: (1, False, False, False)
        self.addCleanup(setattr, bot, "_dodge_intent", original)
        for step in range(1, 40):
            bot._decide(self.room, self.bot_conn, self.bot_seat, terrain,
                        self.now(), tick + step)
            bot._own_step(self.room, self.bot_conn, self.bot_seat, terrain,
                          self.now(), tick + step)
            if self.bot_conn.body.on_ground:
                break
        self.assertTrue(self.bot_conn.body.air_jumped,
                        "一路被「躲子弹」打断，第二段跳还是得按下去")

    # -- ★★★★ V0.3 §152：卡在塞不进去的缝里要自己出来 --------------------

    def crack_room(self, key="crack"):
        """左边一片开阔平地，右边一条 6 像素宽的深缝（缝底有地面）。"""
        width, height, floor, lip = 1200, 800, 700, 560
        rows = []
        for y in range(height):
            if y >= floor:
                rows.append("2" * width)
            elif y >= lip:
                rows.append("0" * 800 + "2" * 100 + "0" * 6
                            + "2" * (width - 906))
            else:
                rows.append("0" * width)
        terrain = mapdata.MapTerrain(make_record(rows, name=key))
        return self.install_terrain(terrain)

    def test_a_bot_wedged_in_a_crack_walks_out(self):
        """★★★★★ 实机 `Iceria03` (1174, 864)：两个 bot 先后卡在同一个像素上，
        一个 **58.9 秒**、一个 **13.0 秒**。对战模式一条脱困都没有。"""
        terrain = self.crack_room("crack_out")
        who = chrprops.get(self.bot_conn.character_id)
        self.place_bot(903.0, 700.0)          # 缝底
        self.assertFalse(botmove.fits(terrain, 903.0, 700.0, who),
                         "这条缝该是塞不进去的（前提没了测试就不成立）")
        intent = bot._unstick_intent(self.room, self.bot_conn, terrain)
        self.assertIsNotNone(intent, "卡住了就该有脱困动作")
        self.assertNotEqual((0, False, False, False), intent,
                            "站着不动不是脱困")

    def test_it_outranks_everything_else(self):
        """★★★ 塞不进去的时候躲子弹 / 打怪都无从谈起 —— 先出去。"""
        terrain = self.crack_room("crack_first")
        self.place_bot(903.0, 700.0)
        # ★ 桩故意选一个脱困**造不出来**的组合（按 ↓ + 冲刺），
        #   免得两边碰巧撞成同一个元组，断言变成永真。
        dodge = (0, False, True, True)
        original = bot._dodge_intent
        bot._dodge_intent = lambda *_a, **_k: dodge
        self.addCleanup(setattr, bot, "_dodge_intent", original)
        intent = bot._move_intent(self.room, self.bot_conn, self.bot_seat,
                                  terrain, None)
        self.assertNotEqual(dodge, intent, "脱困该排在躲子弹前面")

    def test_it_costs_nothing_when_not_stuck(self):
        """★ 没卡住时它只花一次 `fits()`，直接返回 `None`。"""
        terrain = self.crack_room("crack_free")
        self.place_bot(400.0, 700.0)
        self.assertIsNone(bot._unstick_intent(self.room, self.bot_conn,
                                              terrain))

    def test_it_does_not_walk_into_a_crack(self):
        """★★★ 主动往缝里蹭也要挡掉 —— 站住比卡住好。"""
        terrain = self.crack_room("crack_avoid")
        who = chrprops.get(self.bot_conn.character_id)
        self.place_bot(898.0, 700.0)          # 缝口左边，再走一步就掉进去
        step = botmove.tick(terrain, self.bot_conn.body, who, direction=1)
        self.assertFalse(botmove.fits(terrain, step.x, step.y, who),
                         "下一步该正好落在缝里（前提没了测试就不成立）")
        intent = bot._walk_to(self.room, self.bot_conn, terrain,
                              (1100.0, 700.0), False)
        self.assertEqual(0, intent[0], "不该往缝里走")


class BotFallDownTests(TerrainMixin, BotFrameRoom):
    """★★★ **掉出地图下边界 = 死**（§143）—— `map.ini` 的 `FallDown`。

    用户 2026-08-30：「bot 掉到岩浆里不会死亡，并且还会在空中不停的上下
    抽搐，然后游戏进度卡住」+「这个问题不局限于任务模式，对战模式也一样」。

    根因：`mapdata` 里**出界返回 2**（照抄客户端，免得 bot 觉得图外能走）
    ⇒ 掉下去的 bot 被图外那圈虚拟实心接住，悬在最后一行既不死也回不来；
    而「掉出去要判死」这一条服务端整条没有 —— 它在客户端是每帧一次的
    `Character::CheckFallDown`（`0x50d520`），玩家角色那一份直接发 `0x0408`。
    """

    def enable_fall_down(self, value=True):
        """给这张（合成的）图挂上 `map.ini` 的 `FallDown` 那一格。"""
        props = mapdata.STORE.index().setdefault("props", {})
        if value:
            props[self.room.map_name] = {"fall_down": True}
        else:
            props.pop(self.room.map_name, None)
        self.addCleanup(props.pop, self.room.map_name, None)

    def test_the_flag_comes_from_the_real_products(self):
        """★ 真产物里 `FallDown` 只有那 14 张图有 —— 别把它当默认值。"""
        self.assertTrue(mapdata.falls_out_of_the_world("Esperan00"),
                        "에스페란 용암동굴（岩浆洞）是 FallDown=1 的图")
        self.assertTrue(mapdata.falls_out_of_the_world("Esperan00:NewPvp"),
                        "带玩法后缀的完整串也要查得到")
        self.assertFalse(mapdata.falls_out_of_the_world("Quest03_1"))
        self.assertFalse(mapdata.falls_out_of_the_world(""))

    def test_a_bot_at_the_bottom_of_the_map_has_fallen_out(self):
        terrain = self.install_terrain(synth_terrain("falldown", width=1200))
        self.enable_fall_down()
        self.place_bot(600.0, terrain.height - 1.0)
        self.assertTrue(bot._fell_out_of_the_world(
            self.room, self.bot_conn, terrain))

    def test_normal_ground_is_not_a_fall(self):
        terrain = self.install_terrain(synth_terrain("falldown_ok",
                                                     width=1200))
        self.enable_fall_down()
        self.place_bot(600.0, 150.0)
        self.assertFalse(bot._fell_out_of_the_world(
            self.room, self.bot_conn, terrain))

    def test_without_the_map_flag_nothing_happens(self):
        """★ 没有 `FallDown` 的图掉到底也不死 —— 原版就是这样。"""
        terrain = self.install_terrain(synth_terrain("falldown_off",
                                                     width=1200))
        self.enable_fall_down(False)
        self.place_bot(600.0, terrain.height - 1.0)
        self.assertFalse(bot._fell_out_of_the_world(
            self.room, self.bot_conn, terrain))

    def test_falling_out_reports_a_death_and_arms_the_respawn(self):
        """★★ 判死走的是真人那条路：广播 `0x0406` + 上重生闩。"""
        terrain = self.install_terrain(synth_terrain("falldown_die",
                                                     width=1200))
        self.enable_fall_down()
        self.place_bot(600.0, terrain.height - 1.0)
        self.clear()
        bot._report_fall_death(self.room, self.bot_conn, self.bot_seat)
        codes = opcodes(self.alice)
        self.assertIn(gameserver.OP_BROADCAST_DEATH, codes,
                      "该广播死亡，实际发了 %s" % codes)
        self.assertIn(self.bot_seat, self.room.quest.respawn_due,
                      "该上重生闩 —— 5 秒后自己站起来")

    def test_a_second_fall_is_reported_again(self):
        """★ 第二次掉下去还得报得出来（死亡次数要报服务端的权威计数）。"""
        terrain = self.install_terrain(synth_terrain("falldown_twice",
                                                     width=1200))
        self.enable_fall_down()
        self.place_bot(600.0, terrain.height - 1.0)
        bot._report_fall_death(self.room, self.bot_conn, self.bot_seat)
        quest = self.room.quest
        quest.respawn_due.pop(self.bot_seat, None)
        handle = botsync.character_handle(self.bot_seat)
        # 时间窗（3 秒）是给「几台机器同时代报」用的，把它拨过去。
        quest.last_death_broadcast_at[handle] = (
            time.monotonic() - gameserver.MONSTER_DEATH_DEDUP_WINDOW_S - 1.0)
        self.clear()
        bot._report_fall_death(self.room, self.bot_conn, self.bot_seat)
        codes = opcodes(self.alice)
        self.assertIn(gameserver.OP_BROADCAST_DEATH, codes,
                      "第二次掉下去也要报死，实际发了 %s" % codes)


def ice_terrain(key, floor=150, width=1400, height=180,
                pits=((640, 720),), ice=((680, 96, 60),)):
    """一张平地 + 一个坑，坑上罩着**可破坏物**（V0.3 §138）。

    `ice` 是 `[(中心x, 宽, 高), …]`；中心 y 取坑的上沿附近。
    """
    if key in _TERRAIN_CACHE:
        return _TERRAIN_CACHE[key]
    holes = set()
    for x0, x1 in pits:
        holes.update(range(x0, min(x1, width)))
    rows = ["".join("0" if (x in holes or y < floor) else "2"
                    for x in range(width))
            for y in range(height)]
    record = make_record(rows)
    record["breakables"] = [
        {"handle": 900 + i, "x": cx, "y": floor + h // 2, "w": w, "h": h,
         "hp": 40, "regen": 15000,
         "mask": test_mapdata.blob(
             test_mapdata.pack_cells(["3" * w] * h))}
        for i, (cx, w, h) in enumerate(ice)]
    terrain = mapdata.MapTerrain(record)
    _TERRAIN_CACHE[key] = terrain
    return terrain


class BotBreakableTests(TerrainMixin, BotFireRoom):
    """★★★ 可破坏物：**碎了就放行、过一阵长回来**（用户 2026-08-30 / §138）。

    「真人对战时，也是一个人破坏之后，其他人就可以通过了。过一段时间后，
    恢复原状，所有人无法通过，需要再次破坏。」

    原版**一发同步包都没有** —— 每台客户端从同一批爆炸里各算各的，
    服务端要跟上就得自己记同一本账（`botbreak`）。
    """

    def setUp(self):
        super().setUp()
        self.terrain = self.install_terrain(ice_terrain("ice_gap"))
        self.place_bot(560.0, 150.0)

    def ledger(self):
        return bot._breakables(self.room)

    def test_the_bot_sees_the_ice_while_it_is_intact(self):
        seen = bot._terrain(self.room)
        self.assertIs(self.terrain, seen, "完好时就是根那一份")
        self.assertEqual(3, seen.cell(680, 160), "坑上该罩着冰")
        self.assertTrue(seen.is_solid(680, 160))

    def test_breaking_it_opens_the_way(self):
        ledger = self.ledger()
        self.assertTrue(ledger.blast(self.terrain, 680, 160, 0.0, 99))
        seen = bot._terrain(self.room)
        self.assertIsNot(self.terrain, seen, "碎了要换一份地形")
        self.assertEqual(0, seen.cell(680, 160), "碎了那儿该是空的")

    def test_it_grows_back_and_blocks_again(self):
        ledger = self.ledger()
        ledger.blast(self.terrain, 680, 160, 0.0, 99, now=500.0)
        self.assertEqual(frozenset(),
                         ledger.alive(self.terrain, now=500.0))
        # 到点之后原样长回来 —— 又是根那一份地形（连可达图都能复用）。
        self.assertEqual(frozenset([0]),
                         ledger.alive(self.terrain, now=516.0))
        self.assertIs(self.terrain, bot._terrain(self.room))

    def test_a_human_break_is_read_straight_off_the_packet(self):
        """★★★ 真人砸的那一下服务端**照包里的数扣**，不重算（§139）。

        `rpSplashDamaged +4` 填的是破坏物的世界句柄，`+8` 是他那台机器
        已经扣掉的伤害。不记这本账，他从洞里过去了 bot 还以为堵着。
        """
        handle = self.terrain.breakables[0].handle
        self.assertTrue(bot._note_peer_breakable(self.room, handle, 20))
        self.assertEqual(frozenset([0]), self.ledger().alive(self.terrain))
        self.assertTrue(bot._note_peer_breakable(self.room, handle, 30))
        self.assertEqual(frozenset(), self.ledger().alive(self.terrain))

    def test_a_splash_packet_for_a_mob_is_not_mistaken_for_ice(self):
        """怪的句柄不能被当成破坏物 —— 它得继续走喂怪物表那一路。"""
        self.assertFalse(bot._note_peer_breakable(self.room, 1100419, 20))

    def test_a_bot_shot_that_misses_the_ice_leaves_it_alone(self):
        """炸在半张图外 —— 那 11 个采样点一个都碰不到它。"""
        class _Shell(object):
            handle = 200001

        hits = bot._blast_breakables(self.room, self.bot_conn,
                                     weapondata.get(1001030), _Shell(),
                                     (200.0, 150.0))
        self.assertEqual([], hits)
        self.assertEqual(frozenset([0]), self.ledger().alive(self.terrain))

    def test_a_bot_break_is_broadcast_so_it_breaks_on_screen_too(self):
        """★★★ bot 打碎的那一下**必须补发** `rpSplashDamaged`（§139）。

        别人机器上跑不出这一下：那条遍历外面套着 `0x50d294`
        =「这颗弹是我的 / 中立的吗」，bot 的弹两样都不是 ⇒ 整段跳过。
        不补发，真人屏幕上的冰就永远不碎。
        """
        weapon = weapondata.get(1001030)

        class _Shell(object):
            handle = 200001

        self.clear()
        hits = bot._blast_breakables(self.room, self.bot_conn, weapon,
                                     _Shell(), (680.0, 160.0))
        self.assertTrue(hits, "砸在冰上却一件都没打到")
        item, hurt, _where, _broke = hits[0]
        self.assertGreater(hurt, 0)
        splashes = [f for f in bot_frames(self.alice, self.bot_seat)
                    if header(f)["opcode"] == botsync.OP_SPLASH_DAMAGED]
        self.assertTrue(splashes, "没补发 rpSplashDamaged")
        body = splashes[-1][12:]
        target, = struct.unpack_from("<i", body, 4)
        self.assertEqual(item.handle, target,
                         "+4 该填破坏物的世界句柄")

    def test_the_route_is_dropped_when_the_terrain_flips(self):
        """★ 路线是在**上一份**地形上算出来的 —— 状态一翻就作废。"""
        self.bot_conn.nav_path = ["假的一条边"]
        self.ledger().blast(self.terrain, 680, 160, 0.0, 99)
        bot._refresh_breakables(self.room)
        self.assertEqual([], self.bot_conn.nav_path)


class ShellClockTests(BotFireRoom):
    """★★★★★ D106 的**核心不变式**：同一条有序流上，一颗弹体的 `rpFire` 与
    `rpExplode` 的发出间隔恰好是 `k × 32 ms`。

    ## 为什么这是最要紧的一条（§147）

    收方对**远端弹体**每 32 ms 自己推一格，撞地形 / 引信到期就**本地自灭**；
    `rpExplode` 晚到一步就被 `0x492750` 静默丢弃 —— 不扣血、不建溅射对象、
    **计数器不 +1**，而服务端照记 ⇒ 这个座位的弹体句柄从此永久错开，
    「子弹照飞、一滴血不掉」，一局之内不自愈。

    收方那份弹体的时钟锚在它**收到 `rpFire` 的那一帧**，两发又走同一条有序流
    ⇒ 网络延迟对两发是同一份、自动抵消。剩下唯一的自变量就是**我们两发之间
    的间隔**。所以这里一格一格地量，不看挂钟。
    """

    def one_shot(self):
        """逼出**恰好一颗**在飞的子弹，返回它。"""
        self.approach_far(settle=False)
        shells = list(self.bot_conn.pending_shots)
        self.assertTrue(shells, "该有一颗在飞")
        # ★ 把枪口封住：后面一格一格推的时候不许再打，否则数不清是哪一发炸了。
        self.bot_conn.next_fire_at = self.now() + 3600.0
        self.clear()
        return shells[0]

    def test_the_explode_goes_out_on_the_very_tick_of_the_collision(self):
        """★★★ 撞上是第几格，`rpExplode` 就在第几格发出去 —— 一格不多一格不少。"""
        shell = self.one_shot()
        fired_at = shell.born_tick
        exploded_at = None
        for _ in range(shell.max_ticks + 2):
            tick = self.loop().done
            self.advance(1)
            if explode_frames(self.alice, self.bot_seat):
                exploded_at = tick
                break
        self.assertIsNotNone(exploded_at, "总得炸 —— 句柄账一发都不许漏（§42）")
        self.assertEqual(shell.ticks, exploded_at - fired_at,
                         "rpFire 到 rpExplode 的间隔必须等于撞上的那一格")

    def test_the_shell_does_not_move_on_the_tick_it_was_fired(self):
        """★★ 出膛那一格**一步都不推**：收方也是下一帧才推第一格（语料实测，
        2610 对 `rpFire`/`rpExplode` 的残差中位 +13 ms，不是 ±32）。"""
        shell = self.one_shot()
        self.assertEqual(self.loop().done - 1 - shell.born_tick, shell.ticks)


class HeartbeatCadenceTests(BotFrameRoom):
    """★★ 心跳 4 格一发（128 ms），事件包**在发生的那一格**发（D106）。"""

    def beats_only(self):
        return [f for f in bot_frames(self.alice, self.bot_seat)
                if udpsync.is_heartbeat(f)]

    def test_one_heartbeat_every_four_ticks(self):
        self.walk(self.alice, [(0.0, 100.0), (200.0, 100.0)])
        self.clear()
        self.advance(gameserver.HEARTBEAT_TICKS * 3)
        self.assertEqual(3, len(self.beats_only()))

    def test_three_ticks_are_not_enough_for_a_second_one(self):
        self.walk(self.alice, [(0.0, 100.0), (200.0, 100.0)])
        self.clear()
        self.advance(gameserver.HEARTBEAT_TICKS - 1)
        self.assertEqual([], self.beats_only())


class CatchUpHeartbeatTests(BotFrameRoom):
    """★★★★★ 追赶时**不刷位置心跳**（V0.3 §153 / D115）。

    房间循环落后时会把欠的格一口气补完 —— 这是对的（弹体一格都不许跳，
    §147）。可要是每 4 格照发一发心跳，落后 38 格就是 9 发心跳挤在几毫秒里，
    而在落后的那一秒多里一发都没有。收方在静默期一直拿最后那份按键掩码替
    bot 走（最高 ~690 px/s），恢复时被一把拽回去 = 用户看到的「bot 瞬移
    一段距离」。原版客户端卡了一秒之后也只发**一发**位置包。
    """

    def beats_only(self):
        return [f for f in bot_frames(self.alice, self.bot_seat)
                if udpsync.is_heartbeat(f)]

    def test_a_catch_up_burst_reports_the_position_once(self):
        self.walk(self.alice, [(0.0, 100.0), (200.0, 100.0)])
        self.clear()
        ticks = gameserver.HEARTBEAT_TICKS * 3
        self.assertEqual(ticks, self.loop().advance(ticks, burst=True))
        self.assertEqual(1, len(self.beats_only()),
                         "12 格一口气补完 —— 只该在追平的那一格报一次位置")

    def test_the_same_ticks_at_normal_pace_report_three_times(self):
        """★ 对照组：同样 12 格，正常节奏下照旧 3 发（相位一个字没变）。"""
        self.walk(self.alice, [(0.0, 100.0), (200.0, 100.0)])
        self.clear()
        self.advance(gameserver.HEARTBEAT_TICKS * 3)
        self.assertEqual(3, len(self.beats_only()))

    def test_a_burst_that_swallowed_no_beat_owes_nothing(self):
        """★ 追赶只跨了一两格、没吞掉任何一发时，不该凭空多补一发。"""
        self.walk(self.alice, [(0.0, 100.0), (200.0, 100.0)])
        self.advance(gameserver.HEARTBEAT_TICKS)   # 相位对齐到刚发完
        self.clear()
        self.loop().advance(2, burst=True)
        self.assertEqual([], self.beats_only())

    def test_the_burst_only_holds_back_the_position(self):
        """★★★ 追赶押后的**只有位置**：物理照跑、事件包一发不少（§147）。

        对照的办法是跑两遍同样的格数 —— 一遍追赶、一遍正常节奏 ——
        然后比走到哪、以及发了几发**非心跳**的包。
        """
        ticks = gameserver.HEARTBEAT_TICKS * 4

        def run(burst):
            self.setUp()
            self.walk(self.alice, [(0.0, 100.0), (200.0, 100.0)])
            self.clear()
            self.loop().advance(ticks, burst=burst)
            frames = bot_frames(self.alice, self.bot_seat)
            return (self.room.seats[self.bot_seat].conn.battle_pos,
                    len([f for f in frames if not udpsync.is_heartbeat(f)]))

        burst_pos, burst_events = run(True)
        normal_pos, normal_events = run(False)
        self.assertEqual(normal_pos, burst_pos,
                         "追赶不许改变物理 —— 欠的格是一格一格补完的")
        self.assertEqual(normal_events, burst_events,
                         "事件包是句柄账本，追赶途中一发都不许省")


class ReceiverLedgerTests(BotFireRoom):
    """★★★★ **收方账本重放对账**：把 bot 发出去的那串包按收方的规矩重放一遍，
    看每一发 `rpExplode` 指的弹体在收方那边是不是真的存在（§42 / §147）。

    这正是 2026-08-31 那次实机定位漂移用的方法（`bshook` 的 `PROJ+` 序列和
    服务端的计数器逐条并排）—— 只是这里用的是单测里的那串字节。
    """

    def replay(self):
        """按收方的规矩重放一遍，返回 `(建过的弹体数, 对不上号的 rpExplode)`。"""
        alive = set()
        created = 0
        strays = []
        cursor = botsync.projectile_handle(self.bot_seat, 0)
        for frame in bot_frames(self.alice, self.bot_seat):
            opcode = header(frame)["opcode"]
            body = body_of(frame)
            if opcode == botsync.OP_FIRE:
                ammo = struct.unpack_from("<i", body, 2)[0]
                shots = struct.unpack_from("<i", body, 22)[0]
                weapon = weapondata.get(ammo)
                self.assertIsNotNone(weapon, f"武器 {ammo} 不在表里")
                for offset in range(shots):
                    alive.add(cursor + offset)
                created += shots
                cursor += weapon.fire_step
            elif opcode == botsync.OP_EXPLODE:
                handle = struct.unpack_from("<i", body, 0)[0]
                if handle not in alive:
                    strays.append(handle)
                    continue
                alive.discard(handle)
                # ★ 带溅射的武器在**爆炸那一刻**多建一个对象（§86）。
                cursor += 1 if self.explode_step_of(handle) else 0
            elif opcode == botsync.OP_DASH:
                cursor += 1                 # `DashDamage`（§64）
            elif opcode == botsync.OP_SET_ON_FIRE:
                cursor += botsync.fire_wall_handles(
                    self.bot_conn.weapon.raw.get("spawn_count"))
        return created, strays

    def explode_step_of(self, handle):
        """这一发爆炸建不建溅射对象 —— 单测里全场只用一把枪，直接问它。"""
        return self.bot_conn.weapon.explode_step

    def test_every_explode_names_a_shell_the_receiver_really_has(self):
        """★★★ 一发对不上号，这个座位从此就打不掉血了（§42）。"""
        for _ in range(6):
            self.bot_conn.next_fire_at = 0.0
            self.approach()
        created, strays = self.replay()
        self.assertGreater(created, 2, "总得打出几发来")
        self.assertEqual([], strays,
                         "有 rpExplode 指向收方根本没建过的弹体（§147 的漂移）")


class RoomLoopLifecycleTests(BotFrameRoom):
    """★★ 循环什么时候起、什么时候停（D106）。"""

    def test_the_last_human_leaving_stops_it(self):
        """房里没有真人了 = 这一局没有意义了 —— 循环自己停掉，不留野线程。"""
        loop = self.loop()
        self.assertTrue(loop.running())
        for conn in list(self.room.human_members()):
            gameserver.Conn.on_game_packet(conn, OP_LEAVE_SESSION, b"")
            gameserver.Conn.leave_game_result(conn)
        loop.advance(1)
        self.assertFalse(loop.running())

    def test_a_map_change_stops_it_and_the_release_starts_a_new_generation(self):
        """★★ 换图那一段真人在加载画面里 —— bot 一格都不许动；放行之后
        起**新的一代**，tick 从 0 重新数（旧代排在堆里的定时任务自动作废）。"""
        loop = self.loop()
        was = loop.gen
        self.walk(self.alice, [(0.0, 100.0), (200.0, 100.0)])
        self.assertGreater(loop.done, 0)
        gameserver.Conn.on_game_packet(
            self.alice, gameserver.OP_REQ_CHANGE_TO_NEXT_MAP,
            gameserver.w_wstr("Stage02"))
        self.assertFalse(loop.running(), "换图期间循环该停")
        for conn in self.room.human_members():
            gameserver.Conn.on_game_packet(
                conn, gameserver.OP_MAP_LOADING_DONE, b"")
        loop = self.loop()
        self.assertTrue(loop.running(), "放行 0x0418 之后该起新的一代")
        self.assertNotEqual(was, loop.gen)
        self.assertEqual(0, loop.done)

    def test_the_room_thread_really_ticks(self):
        """★ 线程那条路的冒烟：`ROOM_LOOP_THREADED` 打开之后房间自己会走格子。

        上面所有用例都用 `advance()` 手动推（那样才确定），所以**节拍器线程 +
        房间线程**这两条一行都没被跑过 —— 而生产上跑的就是它们。
        这里只验「真的会自己走、停得掉」，不验时序精度（那个在
        `test_roomclock` 里用假时钟量）。
        """
        self.walk(self.alice, [(0.0, 100.0), (200.0, 100.0)])
        loop = self.loop()
        loop.stop("重新起一代，这回带线程")
        saved = gameserver.ROOM_LOOP_THREADED
        self.addCleanup(loop.stop, "用例收尾")
        self.addCleanup(setattr, gameserver, "ROOM_LOOP_THREADED", saved)
        gameserver.ROOM_LOOP_THREADED = True
        loop.start("线程冒烟")
        want = gameserver.HEARTBEAT_TICKS * 2
        deadline = time.monotonic() + 5.0
        while loop.done < want and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertGreaterEqual(loop.done, want, "房间线程没在走格子")
        loop.stop("验完了")
        settled = loop.done
        time.sleep(0.1)
        self.assertEqual(settled, loop.done, "停了还在走")

    def test_a_stuck_human_does_not_slow_the_room_tick(self):
        """★★★★ **一个人卡死不许拖住整个房间**（D108）。

        D106 之前「谁发包谁被堵」只坑发送者自己；房间循环上线之后，房里所有
        bot 的包都是**同一条线程**挨个发的 —— 排在卡死那个人后面的所有人
        跟着一起停 8 秒（`GAME_SEND_DEADLINE_S`），那一段的 `rpExplode`
        全部迟到，句柄账当场错开（§147）。

        现在写 socket 是每条连接自己那条发送线程的活，这一格该在几毫秒里跑完。
        """
        self.walk(self.alice, [(0.0, 100.0), (200.0, 100.0)])

        class StuckSocket:
            """永远收不走字节的对端（卡在 WER 弹窗上的客户端）。"""

            def __init__(self):
                self.gate = threading.Event()

            def sendall(self, _data):
                self.gate.wait(10.0)

        stuck = StuckSocket()
        self.bob.sock = stuck
        self.bob.cout = gameserver.SimpleCipher.server_to_client()
        del self.bob.send                    # 用回真的 `Conn.send`
        self.addCleanup(stuck.gate.set)

        loop = self.loop()
        started = time.monotonic()
        loop.advance(gameserver.HEARTBEAT_TICKS)
        spent = time.monotonic() - started
        self.assertLess(spent, 1.0,
                        f"房里有人卡死，这一帧跑了 {spent * 1000:.0f} ms")
        # ★ 卡死的是 bob，alice 那边照样收得到 bot 的心跳。
        self.assertTrue([f for f in bot_frames(self.alice, self.bot_seat)
                         if udpsync.is_heartbeat(f)])

    def test_a_stalled_loop_catches_up_tick_by_tick(self):
        """★★★ 落后了要**逐格补跑**，一格都不许跳 —— 跳掉的那一格里弹体不
        推进，它的 `rpExplode` 就又变成迟到（§147）。"""
        loop = self.loop()
        self.walk(self.alice, [(0.0, 100.0), (200.0, 100.0)])
        ran = []
        original = bot.tick_room

        def counted(room, tick, now, behind=0):
            ran.append(tick)
            return original(room, tick, now, behind)

        bot.tick_room = counted
        gameserver.BOT_ROOM_TICK = counted
        self.addCleanup(setattr, gameserver, "BOT_ROOM_TICK", original)
        self.addCleanup(setattr, bot, "tick_room", original)
        # 假装节拍器晚了 10 格才叫醒房间。
        start = loop.done
        loop._on_due(loop.gen, roomclock.deadline_of(loop.t0, start),
                     roomclock.deadline_of(loop.t0, start + 9))
        loop.advance(loop.scheduled - loop.done)
        self.assertEqual(list(range(start, start + 10)), ran)
