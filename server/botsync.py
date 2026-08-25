#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bot 的同步流合成器 —— V0.3 M3。

房间 bot 没有客户端，所以它在别人屏幕上的一举一动，全靠服务端**自己造**
一份 `UdpPacket` 再走现成的转发路发出去。这个模块只负责**造字节**和
**记账**，不做任何决策（走哪、打谁是 M5 的事，在 `botai.py`）。

## 分工

| 谁 | 干什么 |
|---|---|
| `botsync.py`（本文件） | 线格式 + 序号记账 + D5 的三条不变式 |
| `bot.py` | 谁在什么时候动一下（`BotConn.sync` 就是这里的 `BotSyncStream`） |
| `relayserver.deliver()` | 投递（中继 / `0x040f` / UDP 旁路三条路） |

## 导入方向

本文件**只** import `relayserver` 和 `udpsync`，**不** import `gameserver`
—— 那两个模块反过来也不认识 `gameserver`，所以这条边不会成环，而且
`BotSyncStream` 拿一个假连接就能单测（不用起房间、不用起 socket）。

## ★★ 三条不变式（D5）—— 违反了当场炸，不许只写在注释里

1. **事件包序号（头 `+8`）从 0 严格连续递增**；
2. **心跳里的 N（body `+0..1`）恒等于已发出的事件包数**，绝不越过；
3. **每份包盖的局号（头 `+4`）是 bot 自己那一代的号** ——
   收件人那一头的重新盖章由 `relayserver.deliver()` 负责。

为什么值得写成断言：违反 1 / 2 的后果是**收方那个座位的弹体句柄分配器
永久错位** —— 伤害数字照出、血照掉一丝、**就是打不死人**，而且一局之内
不会自愈（V0.2 §216 / §217 花了整整一轮才定位）。这种「症状离原因十万
八千里」的 bug，唯一划算的防法是在**生成的那一刻**就炸。

炸出来的 `SyncInvariantError` 由调用方（`bot.py` 的那一圈）接住并把这个
bot 的流停掉 —— bot 出问题最多是它自己不动，**不能连累真人**（D1）。

## 线格式出处

`UdpPacket` 头见 `re/packet_api.md` §1.3，内层 body 见 §5。
逆向经过在 `FINDINGS.md` §23（组包点 + 写原语）/ §24（心跳 body）/
§25（角色状态结构的收发两侧逐字段）。
"""
from __future__ import annotations

import struct
import threading

import relayserver
import udpsync

# ---------------------------------------------------------------------------
# `UdpPacket` 头（12 字节，§1.3）
# ---------------------------------------------------------------------------
#: 头 `+0`。不是它，客户端 `0x4078ab` 连队列都不让进就丢掉。
PEER_MAGIC = 0xFF

#: 头 `+2` = 目标座位，`0xff` = 广播。
#:
#: ★ 我们**永远**只发广播：三个原版发送点（`0x4058cc` / `0x4077db` /
#: `0x408257`）在组包时也都写 `0xff`，41 个语料文件里 91526 发一个例外都没有。
#: （原版 UDP 直连那条路会在逐座位发送前把它改成座位号，那是投递层的事，
#: 见 `udpsync.as_broadcast`。）
PEER_TARGET_BROADCAST = -1

#: 头 `+3`。`packet_api.md` 原来标 ❓未知 —— 语料里 **91526 发恒 0**，
#: 照填 0（V0.3 §25）。
PEER_HEADER_PAD = 0

#: 校验和（头 `+6`）的种子和乘数，出处 `0x5bbdc1`（§4）。
CHECKSUM_SEED = 0x17
CHECKSUM_MULTIPLIER = 0x103

#: 头：magic / 发送方座位(i8) / 目标座位(i8) / 填充。
_HEADER_HEAD = struct.Struct("<BbbB")
#: 头：局号 / 校验和 / 序号 / 内层 opcode，四个 u16。
_HEADER_TAIL = struct.Struct("<HHHH")

# ---------------------------------------------------------------------------
# 内层 opcode（§5）。分界线是 `0x4000`：
#   `< 0x4000` -> 收方的 `PktQueue`，**可靠、按序、丢一发永久错位**；
#   `>= 0x4000` -> 立刻处理，可丢。
# ---------------------------------------------------------------------------
OP_CHANGE_WEAPON = 0x0001
OP_FIRE = 0x0002
OP_EXPLODE = 0x0003
OP_JUMP = 0x0006
OP_HEARTBEAT = udpsync.OPCODE_HEARTBEAT          # 0x4001

#: 事件包（进 `PktQueue` 的那一类）的分界线。
RELIABLE_OPCODE_MAX = 0x4000


class SyncInvariantError(AssertionError):
    """D5 的三条不变式被违反了。**继承 `AssertionError` 是故意的** ——
    它就是一条断言，只是不用 `assert` 语句（`python -O` 会把那个整句删掉，
    而这三条恰恰是「平时不出事、一出事查一整轮」的那种，绝不能被优化掉）。
    """


def _require(condition, message):
    if not condition:
        raise SyncInvariantError(message)


def udp_checksum(body):
    """`UdpPacket` 头 `+6` 的校验和（客户端 `0x5bbdc1`，§4）。

    ```asm
    005bbdc8  push 0x17 / pop eax        ; 种子
    005bbdcb  lea esi, [buf+0xc]         ; ★ 只覆盖 body，不含 12 字节头
    005bbdd2  movsx dx, byte [esi]       ; ★ 逐字节**带符号**扩展
    005bbdd6  imul eax, eax, 0x103
    005bbddc  add  eax, edx
    ```

    比对在 `0x4078f0`（`cmp word [esi+6], ax`）—— **只比低 16 位**。

    ★ 不算它的后果：真客户端在收包入口就把包丢掉，连队列都进不去
    （V0.2 会话 34 实机踩过）。本版拿 41 个语料文件的 **91526 发**逐包重算，
    命中 91526、失配 0。
    """
    acc = CHECKSUM_SEED
    for byte in bytes(body):
        signed = byte - 256 if byte >= 128 else byte
        acc = (acc * CHECKSUM_MULTIPLIER + signed) & 0xFFFF
    return acc


def build_peer_packet(seat, opcode, body, game_id, sequence=0):
    """拼一份完整的 `UdpPacket`（12 字节头 + body）。

    `game_id` 就是头 `+4` 的局号，必须是**发送方自己**那一代的号；
    转发给每个收件人时 `relayserver.deliver()` 会按收件人的号重新盖章
    （校验和不覆盖头，所以改它不用重算）。
    """
    body = bytes(body)
    return (_HEADER_HEAD.pack(PEER_MAGIC, int(seat), PEER_TARGET_BROADCAST,
                              PEER_HEADER_PAD)
            + _HEADER_TAIL.pack(int(game_id) & 0xFFFF, udp_checksum(body),
                                int(sequence) & 0xFFFF, int(opcode))
            + body)


# ---------------------------------------------------------------------------
# 心跳 `0x4001`（body 31 字节，§24 / §25）
# ---------------------------------------------------------------------------
#: body `+2..5`：「这一发带几个角色的状态」。**永远只带自己那一个**，
#: 语料里 67186 发恒 1。
HEARTBEAT_CHARACTER_COUNT = 1

#: 角色状态结构（body `+7..30`，24 字节）里的两个**朝向**取值。
#:
#: 位域（body `+19..22`）的低 2 位是 `[char+0x2d0]`，收方按 2 位**有符号**
#: 还原（`shl eax,30 ; sar eax,30`），实测只出现 -1 / 0 / 1 三种。
#: 🤔 「1 = 朝右、-1 = 朝左」是**推测**：拿紧跟着那一发 `rpFire` 的发射角
#: 对穿，低 2 位 = 1 时发射角落在右半边 2689 : 1294（68%），
#: = -1 时落在左半边 2000 : 1054（65%）—— 有倾向但**没到证明**。
#: ★ 万一反了，症状是「bot 背对着开枪」，纯外观，实机看一眼就能定（M3 验收清单里有）。
FACING_RIGHT = 1
FACING_LEFT = -1

#: 位域 bit2 = `[char+0x128]`。
#:
#: ★★ 它决定收方**怎么用**位置：反序列化器 `0x504215` 判
#: 「上一发的 `[char+0x128]` == 0 **且** 这一发 bit2 == 1」才**硬置**坐标，
#: 否则走**插值**：`[char+0x34] = 旧*0.6 + 新*0.4`（常数 `0x6937c4`/`0x6937c0`）。
#: 语料里真客户端 bit2 绝大多数时候就是 1（最常见的 bit2..5 组合是 `0b0001`），
#: 也就是说**插值才是常态** —— 我们照着来，收方的行为才和看真人时一致。
#: 代价：bot 换位置时会滑过去而不是瞬移（0.6^5 ≈ 8%，约 5 发心跳收敛）。
HEARTBEAT_BIT_ONGROUND = 0x04

#: 准星（鼠标）的屏幕坐标，body `+25..28`，收方存进 `[char+0x680]`/`[0x684]`。
#:
#: 发侧是 `0x4295cb` -> `0x429569`：直接取输入系统里那对光标坐标
#: （没有光标时用 `(400, 500)`，正是 800×600 的中心）。语料里的取值
#: `(1112, 548)` / `(1023, 799)` / `(396, 622)` 全都在屏幕分辨率量级 ——
#: ⇒ 它是**纯展示量**，和玩法无关。bot 给一个屏幕中心附近的常数就行。
CURSOR_DEFAULT = (512, 384)

#: 「位移 -> 速度字段」的换算：body `+11..12` / `+13..14` 是速度
#: （收方原样写进 `[char+0x120]` / `[char+0x124]`），发侧是
#: `round([char+0x4c4] + [char+0x120])`。
#:
#: ★ 这个 4.111 是**回归出来的**，不是逆出来的：拿语料里 10886 组
#: 「这一发的速度」对「下一发的 X 位移」，同号率 95.5%、比值中位数 4.111。
#: 服务端没有别的办法知道客户端那边的积分口径（那要连客户端的帧循环一起逆）。
#: 填 0 也不会错，只是角色会**滑行而不播走路动画** —— 所以这里宁可用一个
#: 有量纲依据的估计值。★ 它只影响动画观感，不影响任何判定。
VELOCITY_PER_STEP = 4.111

#: body `+15` = `[char+0x594]`：语料里 59117/67186 是 0，其余是零散的 34~80。
#: 语义未知，填 0。
HEARTBEAT_STATE_BYTE = 0

#: body `+17..18` = `[char+0x584]` 的**度数**。
#:
#: ★ 发侧 `× 57.29577637`（= 180/π，`0x69379c`）、收侧 `× 0.017453292`
#: （= π/180，`0x693778`）⇒ `[char+0x584]` 是**弧度角**，线上传的是**度**。
#: （§3 / §24 把这一格写成「速度」，那是按取值猜的，**错了** —— 见 §25。）
#: 语义 🤔 像炮口角度：取值全落在 [-256, 255]，0 是压倒性的众数。bot 填 0。
HEARTBEAT_ANGLE_DEG = 0

#: 心跳 body 的定长部分：`u16 N` + `i32 1` + `u8 座位`。
_HEARTBEAT_HEAD = struct.Struct("<HiB")
#: 角色状态结构（24 字节）：位置 / 速度 / 状态字节 / 填充 / 角度 / 位域 /
#: 六位掩码 / 准星 / 填充。
_CHARACTER_STATE = struct.Struct("<hhhhBBhiHhhH")

#: 心跳 body 的总长度。**必须是 31** —— 语料里 67186 发全是这个长度，
#: 收方 `0x407b94` 也是按定长读的。
HEARTBEAT_BODY_SIZE = _HEARTBEAT_HEAD.size + _CHARACTER_STATE.size


def clamp_i16(value):
    """f32 -> i16，**照客户端那个转换器的口径**（`0x5f895c`），再夹到 i16。

    ★ `0x5f895c` 不是「四舍五入」——它是 MSVC 的 `_ftol2`，**朝零截断**
    （C 的 `(int)` 语义）：先 `fistp`（就近偶数）取一个整数，再用
    `fsubp` 比一下差值的符号，`adc` / `sbb` 把它掰回靠近零的那一侧。
    Python 的 `int()` 正是同一个口径；`round()` 就不是（`round(2.7)` = 3，
    截断是 2）。差别只有 1 个单位、看不出来，但既然逆清楚了就照着来。

    夹边界：坐标在包里是 i16，超界的值只可能是脏数据 —— 与其让
    `struct.error` 炸在转发路径上，不如夹住。
    """
    return max(-32768, min(32767, int(value)))


def character_state(x, y, vx=0, vy=0, facing=FACING_RIGHT,
                    angle_deg=HEARTBEAT_ANGLE_DEG, cursor=CURSOR_DEFAULT,
                    buff_mask=0, state_byte=HEARTBEAT_STATE_BYTE):
    """那 24 字节角色状态结构（心跳 body `+7..30`）。

    逐字段的收发两侧对照见 `FINDINGS.md` §25。两处「序列化器从没写过」的
    填充（结构 `+0x09` / `+0x16`）**收方也不读**，一律填 0。
    """
    field = (int(facing) & 0x03) | HEARTBEAT_BIT_ONGROUND
    return _CHARACTER_STATE.pack(
        clamp_i16(x), clamp_i16(y),              # +0x00 / +0x02  位置
        clamp_i16(vx), clamp_i16(vy),            # +0x04 / +0x06  速度
        int(state_byte) & 0xFF,                  # +0x08  [char+0x594]
        0,                                       # +0x09  ★ 填充，收方不读
        clamp_i16(angle_deg),                    # +0x0a  角度（度）
        field,                                   # +0x0c  位域
        int(buff_mask) & 0x3F,                   # +0x10  六位掩码
        clamp_i16(cursor[0]), clamp_i16(cursor[1]),   # +0x12 / +0x14  准星
        0,                                       # +0x16  ★ 填充，收方不读
    )


def heartbeat_body(next_event_seq, seat, state):
    """一发 `0x4001` 心跳的 body（31 字节）。

    `next_event_seq` 就是 §216 的那个 **N**：收方拿它 `FlushTo(N)`，
    而 `base` **只进不退** ⇒ 报大了就把还没到的事件包判死。
    调用方必须传「已经发出去的事件包数」，别的都是错的。
    """
    body = (_HEARTBEAT_HEAD.pack(int(next_event_seq) & 0xFFFF,
                                 HEARTBEAT_CHARACTER_COUNT, int(seat) & 0xFF)
            + bytes(state))
    _require(len(body) == HEARTBEAT_BODY_SIZE,
             f"心跳 body 长度 {len(body)} != {HEARTBEAT_BODY_SIZE}")
    return body


# ---------------------------------------------------------------------------
# 事件包的 body（§23 / packet_api §5.2 ~ §5.4）
# ---------------------------------------------------------------------------
#: `rpFire` body `+0` = **`10 + 座位号`**（怪是 20）。7040 发和头 `+1` 的
#: 发送方 100% 一致、0 例外。
FIRE_SOURCE_PLAYER_BASE = 10

#: `rpFire` body `+1`：武器槽 / 发射源。玩家 1~5、怪恒 8、道具 255。
#: 语料里玩家发的绝大多数是 1。
FIRE_SLOT_DEFAULT = 1

#: `rpFire` body `+22`：一次打几发。6975 : 45 : 20 = 1 : 2 : 3。
FIRE_SHOTS_DEFAULT = 1

_FIRE = struct.Struct("<BBiffffi")
_EXPLODE = struct.Struct("<iiffiif")
_JUMP = struct.Struct("<BB")
_CHANGE_WEAPON = struct.Struct("<Bi")


def fire_body(seat, ammo_id, x, y, angle, power,
              slot=FIRE_SLOT_DEFAULT, shots=FIRE_SHOTS_DEFAULT):
    """`0x0002 rpFire`（26 字节）。

    `angle` 是**弧度**（调用方 `atan2`，`0x4176bc`），`power` 实测 1~130。

    ★ **包里没有弹体句柄** —— 收方按同样顺序自己分配。这就是「丢一发就
    永久错位、打不死人」的机制（V0.2 §216 / §217），也是 D5 那三条不变式
    存在的全部理由。
    """
    return _FIRE.pack(FIRE_SOURCE_PLAYER_BASE + (int(seat) & 0xFF),
                      int(slot) & 0xFF, int(ammo_id),
                      float(x), float(y), float(angle), float(power),
                      int(shots))


def explode_body(handle, target_handle, x, y, hit_kind=0, flags=0, radius=3.0):
    """`0x0003 rpExplode`（28 字节）。

    `handle` = 弹体句柄，`target_handle` = 命中目标的句柄（`0` = 没命中，
    玩家是「座位 × 100000 + 100001」）。`hit_kind`：0 没命中 / 1 命中别的
    对象 / 2·5 命中角色。`flags` 那一格（`+20`）语义仍是 ❓。
    """
    return _EXPLODE.pack(int(handle), int(target_handle), float(x), float(y),
                         int(hit_kind), int(flags), float(radius))


def jump_body(seat, stage=1):
    """`0x0006 rpJump`（2 字节）：座位号 + 第几段跳（1 / 2）。"""
    return _JUMP.pack(int(seat) & 0xFF, int(stage) & 0xFF)


def change_weapon_body(seat, weapon_id):
    """`0x0001 rpChangeWeapon`（5 字节）：座位号 + 武器 id。"""
    return _CHANGE_WEAPON.pack(int(seat) & 0xFF, int(weapon_id))


# ---------------------------------------------------------------------------
# 一条 bot 的同步流
# ---------------------------------------------------------------------------
class BotSyncStream:
    """一个 bot 的「同步流」—— 序号记账 + 组包，**不含任何决策**。

    住在 `BotConn` 上（D9：只属于 bot 的机器状态跟着「它那台机器」走）。

    ## 为什么要有 `events` 这本账

    收方对每个座位维护一条 `PktQueue`。事件包按头 `+8` 的序号入队，
    心跳里的 N 会让它 `FlushTo(N)`，而 `base` **只进不退**。所以：

    * 事件序号跳号 -> 收方永远等那一号，之后的全卡住；
    * 心跳的 N 报大了 -> 收方把还没发的号直接判死，**连讨重传都不会讨**。

    两种都是「伤害数字照出、血照掉一丝、就是打不死人」，且一局之内不自愈。
    ⇒ 这两条在本类里是**断言**，不是注释（D5）。

    ## 换代

    发送方换代（服务端广播 `0x0400` / `0x0403`）时客户端会 `ResetQueues`，
    自己的事件序号回 0。bot 这边同一个道理：局号一变就把 `events` 清零。
    判据是**局号真的变了**这个事实本身（`relayserver.epoch_state`），
    不是定时器、也不是「开局时记得调一下」（铁律 10）。
    """

    __slots__ = ("conn", "events", "_epoch_value", "_lock", "sent",
                 "dropped", "broken")

    def __init__(self, conn):
        #: 这条流属于哪个 `BotConn`（局号和座位号都从它身上问）。
        self.conn = conn
        #: 已经发出去的**事件包**数 = 下一个事件包的序号 = 心跳里的 N。
        self.events = 0
        #: 上一次组包时看到的局号，用来发现换代。
        self._epoch_value = None
        self._lock = threading.RLock()
        #: 统计（进 `status` / 日志，判断「这条流到底有没有在动」）。
        self.sent = 0
        self.dropped = 0
        #: 不变式炸过一次之后就把这条流停掉 —— 继续发只会把收方的队列
        #: 越弄越乱，而 bot 不动至少是个**看得见**的故障。
        self.broken = False

    # -- 局号 ---------------------------------------------------------------
    def epoch_value(self):
        """bot 现在这一代的局号（头 `+4` 要盖的那个数）。

        `BotConn.send()` 会照真人那条路跑一遍 `note_epoch_from_frame()`，
        所以 bot 的换代模型和房里真人的是**同一串字节**推出来的 ——
        `0x0400` 一发出去，两边一起 +1（V0.3 §26）。
        """
        return relayserver.epoch_state(self.conn).value

    def _sync_epoch(self):
        """局号变了就把事件序号清回 0（对称于 `Conn.sync_peer_epoch()`）。"""
        value = self.epoch_value()
        if self._epoch_value is None:
            self._epoch_value = value
            return
        if value != self._epoch_value:
            self._epoch_value = value
            self.events = 0

    # -- 组包 ---------------------------------------------------------------
    def heartbeat(self, state):
        """一发心跳。**N 恒等于已发出的事件包数**（不变式 2）。"""
        with self._lock:
            self._sync_epoch()
            body = heartbeat_body(self.events, self.conn.my_seat, state)
            # ★ 心跳的头 `+8` 恒 0（语料 67186 发只有这一个取值）——
            #   它没有任何可判新旧的原版字段，所以下行也绝不能双发。
            return build_peer_packet(self.conn.my_seat, OP_HEARTBEAT, body,
                                     self.epoch_value(), sequence=0)

    def event(self, opcode, body):
        """一发事件包（内层 `< 0x4000`）。序号从 0 严格连续（不变式 1）。"""
        _require(0 < int(opcode) < RELIABLE_OPCODE_MAX,
                 f"内层 opcode {opcode:#06x} 不是事件包，别走这条路")
        with self._lock:
            self._sync_epoch()
            sequence = self.events
            packet = build_peer_packet(self.conn.my_seat, opcode, body,
                                       self.epoch_value(), sequence=sequence)
            self.events = sequence + 1
            return packet

    # -- 投递 ---------------------------------------------------------------
    def deliver(self, packet, deliver_fn):
        """把一份包交给投递层，返回真的送到了几个人。

        `deliver_fn` 就是 `relayserver.RelayServer.deliver`（调用方注入，
        本模块不 import `gameserver`）。**不新增第二条投递路**：中继 /
        `0x040f` 回退 / UDP 旁路三条路的选择、以及按收件人重新盖局号，
        全在那边（不变式 3 的后半段）。
        """
        sent = deliver_fn(self.conn, packet)
        if sent:
            self.sent += 1
        else:
            self.dropped += 1
        return sent

    def summary(self):
        """给日志用的一行现状；一发都没发过就返回 ``None``（不打）。"""
        if not (self.sent or self.dropped):
            return None
        return (f"同步流 已发 {self.sent} 无人收 {self.dropped} "
                f"事件 {self.events} 局号 {self.epoch_value()}"
                + ("　★已停（不变式炸过）" if self.broken else ""))
