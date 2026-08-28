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

import ballistics                                              # noqa: E402
import bot                                                     # noqa: E402
import botsync                                                 # noqa: E402
import chrprops                                                # noqa: E402
import gameserver                                              # noqa: E402
import lobby                                                   # noqa: E402
import mapdata                                                 # noqa: E402
import relayserver                                             # noqa: E402
import udpsync                                                 # noqa: E402
import weapondata                                              # noqa: E402
from gameserver import OP_LEAVE_SESSION, OP_PEER_DATA_DOWN, \
    OP_PEER_DATA_UP                                            # noqa: E402
from test_battle import opcodes                                # noqa: E402
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

    ★ 每个用例自己喂真人的心跳 —— bot 的帧是**被真人的同步包驱动**的（D17），
    不喂就一帧都不该有。
    """

    #: ★★ 「进图 / 复活之后 2 秒不能动手」那道锁（§74）默认**放开**。
    #:
    #: 不放开的话这一大批用例全废：它们在几微秒里喂完整局心跳，而真实的
    #: 2 秒锁会把每一发 `rpFire` 都挡掉。锁本身有 `BotActionLockTests`
    #: 单独验（同 `BotFireRoom.melee` 的路数）。
    action_lock = False

    def setUp(self):
        super().setUp()
        if not self.action_lock:
            self.unlock_bots()

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
                        velocity=(0, 0), fast_run=False):
        """让 `conn` 发一发带位置的心跳（走真的 `0x040e` 入口）。

        ★ 不用再复位什么闸门：bot 的帧判据是「这个真人报了一个新位置」
        （`sync_trail_seq` 变了），喂一发就走一帧（V0.3 §32）。

        ★ 默认「踩在地上、速度 0」—— 那是真人**走路**时的样子（§35），
        绝大多数用例要的就是它。跳跃的段落显式传 `on_ground=False`。

        ★ `action_lock = False` 时**每一发之前都放一次锁**：换图 / 复活
        都会重新上锁（§74），逐发放开才不用在每个用例里记着这件事。
        """
        if not self.action_lock:
            self.unlock_bots()
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
        """让在飞的子弹**把整条弹道跑完**（把出膛时刻拨到很久以前）。

        ★ 单测里的一帧和一帧之间只差几微秒，而真实的 bot 帧是 ~125 ms ——
        `_advance_shells()` 是按**真实流逝的时间**决定推几个 tick 的（§65），
        所以在单测里得手动把出膛时刻往回拨。
        ★ 贴脸那种一个 tick 之内就撞上的本来就当场结算，用不着这个。
        """
        for shell in self.bot_conn.pending_shots:
            shell.born -= 3600.0

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
    """`/w N M` —— 房主手动换枪（自动换是 M5 的事，见 `_cmd_gun` 的注释）。"""

    def gun(self, *args):
        """敲一条 `/w`，返回房主看到的那几行系统提示。"""
        self.alice.sent.clear()
        self.assertTrue(bot.handle_command(
            self.alice, "/w " + " ".join(map(str, args))))
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
        """`/c` 换到一个没有那个槽位的角色 —— 退回首选，**不是**哑火。

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
    """`/s N` —— 让 bot 站住，好测「隔着墙打不打得到」。"""

    def hold(self, *args):
        self.alice.sent.clear()
        self.assertTrue(bot.handle_command(
            self.alice, "/s " + " ".join(map(str, args))))
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
        # 出膛那一下只推了收方的第一步，还没走完。
        self.assertEqual(1, shell.ticks)

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
        bot.handle_command(self.alice, f"/s {self.bot_seat}")
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        far = self.bot_conn.battle_pos[0] + 700.0
        self.walk(self.alice, [(far, 100.0), (far + 5.0, 100.0)])
        self.assertTrue(fire_frames(self.alice, self.bot_seat),
                        "700 个单位是原版玩家常见的交战距离，必须开火")

    def test_nothing_is_shot_at_beyond_the_engagement_range(self):
        self.approach()
        bot.handle_command(self.alice, f"/s {self.bot_seat}")
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
        self.walk(self.alice, [(0.0, 100.0), (600.0, 100.0)])
        self.settle()
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(600.0, 100.0)])
        self.charge()
        self.walk(self.alice, [(600.0, 100.0)])
        shots = list(self.bot_conn.pending_shots)
        self.assertTrue(shots, "该有一颗苹果雷在飞")
        before = self.bot_conn.sync.projectiles
        self.walk(self.alice, [(3000.0, 100.0)])       # 躲开 —— 别被砸中
        for shell in self.bot_conn.pending_shots:
            shell.max_ticks = min(shell.max_ticks, 20)
        self.clear()
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
        bot._split_shell(self.room, self.bot_conn, shell, (100.0, 50.0), None)
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
        bot._split_shell(self.room, self.bot_conn, shell, (100.0, 50.0), 0)
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
        bot._split_shell(self.room, self.bot_conn, shell, (100.0, 50.0), None)
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
        self.assertEqual([], fire_frames(self.alice, self.bot_seat),
                         "手指才刚按下去，一发都不该有（§73）")
        self.charge()
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
        self.walk(self.alice, [(0.0, 100.0), (600.0, 100.0)])
        self.settle()
        self.clear()
        self.bot_conn.next_fire_at = 0.0
        self.walk(self.alice, [(600.0, 100.0)])      # 手指按下去，开始蓄力
        self.charge()
        self.walk(self.alice, [(600.0, 100.0)])      # 松手，扔出去
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
        self.grenade()
        self.approach()
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
        self.bot_conn.next_fire_at = 0.0
        self.approach(x=140.0)
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
        # ★ 花掉 `SpCost`，同一帧里又按 `SpCharging` 回了一丁点 —— 取个余量。
        self.assertAlmostEqual(full - move.sp_cost, self.bot_conn.stamina,
                               delta=1.0)
        # 体力不够就不冲了。
        self.bot_conn.stamina = move.sp_cost - 1.0
        self.bot_conn.dash_swing = None
        self.clear()
        self.walk(self.alice, [(160.0, 100.0), (170.0, 100.0)])
        self.assertEqual([], dash_frames(self.alice, self.bot_seat),
                         "体力不够就不该冲")

    def test_a_connecting_dash_reports_the_damage(self):
        """★ `rpDash` 里**没有伤害** —— 判中和扣血是射手那台机器的活（D28），
        补一发 `rpSplashDamaged`（§67）。"""
        self.approach()
        self.assertTrue(dash_frames(self.alice, self.bot_seat))
        # 把动作推到伤害帧。
        swing = self.bot_conn.dash_swing
        self.assertIsNotNone(swing)
        handle = swing.handle
        swing.born -= 1.0
        self.clear()
        self.walk(self.alice, [tuple(self.alice.sync_trail[-1][:2])])
        hits = splash_frames(self.alice, self.bot_seat)
        self.assertTrue(hits, "贴着打这一下该打中")
        body = body_of(hits[0])
        source, victim = struct.unpack_from("<ii", body, 0)
        damage = struct.unpack_from("<f", body, 8)[0]
        self.assertEqual(botsync.character_handle(0), victim)
        move = chrprops.get(self.bot_conn.character_id).dash()
        # ★ 夺分模式（夹具默认 args[1] = 3）伤害 ×2（§87）。
        self.assertAlmostEqual(float(move.damage) * bot._damage_scale(self.room),
                               damage, places=3)
        self.assertEqual(handle, source, "伤害源就是这一下的句柄")

    def test_one_dash_damages_at_most_once(self):
        self.approach()
        swing = self.bot_conn.dash_swing
        self.assertIsNotNone(swing)
        swing.born -= 1.0
        for _ in range(3):
            self.walk(self.alice, [tuple(self.alice.sync_trail[-1][:2])])
        self.assertLessEqual(len(splash_frames(self.alice, self.bot_seat)), 1)

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
        """`/s` 的意思是「站住别动」—— 那就别冲出去。"""
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
        self.bot_conn.move_at = time.monotonic()

    def beats(self, count, x, y=150.0):
        """真人在 `(x, y)` 站着发 `count` 发心跳 —— bot 跟着走 `count` 帧。

        ★ 每帧**把上一次推运动的时刻往回拨一发心跳的时间**：单测里两帧只
        差几微秒，而真实的 bot 帧是 ~125 ms = 4 个 tick（§71）。不拨的话
        一帧只推得动 1 个 tick，走几百个单位要喂上百发心跳。
        和 `arrive()` / `settle()` 拨子弹出膛时刻是同一个手法。
        """
        for _ in range(count):
            self.bot_conn.move_at -= bot.botmove.TICKS_PER_BEAT \
                * bot.botmove.TICK_MS / 1000.0
            self.human_heartbeat(self.alice, x, y)


class BotOwnMovementTests(TerrainMixin, BotFireRoom):
    """★★★ **bot 自己走位**（M5 / §71）—— 不再只回放真人的轨迹（D16）。

    规则只有两条，都不是我们发明的（D50）：**打得到就站住打**、
    **打不到就朝最近的敌人走过去**（§48 量出来的真人交战距离）。
    地形只回答「这一步走不走得成」。
    """

    melee = False                       # 近身会抢在开枪前面，这批用例不验它

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
        """前面是爬不上去的坎就跳 —— 真人卡在墙根时也是这么干的。"""
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

    def test_without_terrain_data_it_falls_back_to_the_human_trail(self):
        """没有地形产物的图上退回 D16 那条老路 —— 少走两步好过走进墙里。"""
        self.room.map_name = "没有这张图"
        self.walk(self.alice, [(0.0, 50.0), (120.0, 50.0), (240.0, 50.0)])
        self.assertIsNone(self.bot_conn.body)
        last = bot_frames(self.alice, self.bot_seat)[-1]
        self.assertEqual((120, 50), udpsync.heartbeat_position(last))


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
    """★ 闯关房**照旧回放真人的轨迹** —— 那儿要的就是「跟着推进」，
    而怪的位置服务端手里没有（`_hostile_targets` 在闯关房恒为空）。"""

    def test_a_coop_bot_still_follows_the_trail(self):
        self.install_terrain(synth_terrain("flat"))
        self.walk(self.alice, [(0.0, 150.0), (120.0, 150.0), (240.0, 150.0)])
        self.assertIsNone(self.bot_conn.body, "闯关房不该接管走位")
        last = bot_frames(self.alice, self.bot_seat)[-1]
        self.assertEqual((120, 150), udpsync.heartbeat_position(last))


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
        self.walk(self.alice, [(700.0, 150.0)])
        self.assertIsNone(self.bot_conn.body)
        self.walk(self.alice, [(700.0, 150.0)])
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
        `trail_point(真人轨迹)`，bot 被拽到他身边。现在该**原地站着**。
        """
        self.terrain()
        self.walk(self.alice, [(700.0, 150.0)])
        here = self.bot_conn.battle_pos
        self.assertIsNotNone(here)
        # 真人躺下（服务端上闩 = `_lying_dead` 为真）
        self.room.quest.arm_respawn_watchdog(
            self.room.seat_index_of(self.alice), (700.0, 150.0), after=5.0)
        self.assertEqual([], bot._hostile_targets(self.room, self.bot_seat))
        self.walk(self.alice, [(700.0, 150.0), (700.0, 150.0)])
        self.assertEqual(here, self.bot_conn.battle_pos)


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


class BotTakesKnockbackTests(TerrainMixin, BotFireRoom):
    """★★★ **bot 挨打也要被顶飞**（§92）—— 用户 2026-08-28 报的那条。

    bot 的位置是服务端说了算的：客户端各自把它的模型顶飞，服务端这边不动
    的话下一发心跳当场拽回去，看着就是「只是原地跳一下」。
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
        """★ 没引信的照旧撞上什么炸在那儿 —— 别把火焰弹一起改了。"""
        weapon = weapondata.get(1001020)
        self.assertFalse(bot._bounces_off_terrain(weapon))
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
        bot._split_shell(self.room, self.bot_conn, shell, (100.0, 100.0), None)
        frags = list(self.bot_conn.pending_shots)
        self.assertEqual(4, len(frags), "该有四片碎片在飞")
        handles = sorted(s.handle for s in frags)
        self.assertEqual(list(range(handles[0], handles[0] + 4)), handles)


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
        flame = weapondata.get(1001500)
        for mode, want in ((gameserver.PVP_MODE_SURVIVAL, flame.damage),
                           (gameserver.PVP_MODE_DEATHMATCH, flame.damage * 2)):
            self.set_mode(mode)
            self.bot_conn.burnt = {}
            self.walk(self.alice, [(400, 100)])
            spot = self.alice.sync_trail[-1][:2]
            life = bot._fire_wall_ticks(flame)
            wall = bot.FireWall(
                botsync.projectile_handle(self.bot_seat, 0), flame,
                [bot.Flame(botsync.projectile_handle(self.bot_seat, 0),
                           float(spot[0]), float(spot[1]), 0, life)],
                time.monotonic(), life)
            wall.born_tick -= bot.BOT_FIRE_REBURN_TICKS
            self.bot_conn.fires = [wall]
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
