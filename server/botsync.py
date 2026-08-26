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

import math
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

#: `0x000b rpCrouch` —— **蹲下 / 起立**（body 2 字节，§41）。
#:
#: ★ 蹲这件事**心跳里一个位都没有**：只有按下和松开那两下各发一发这个
#: **事件包**，收方照着设 `[char+0x2b5]`。所以发漏了一发，那个角色就会在
#: 别人屏幕上一直保持错的姿势 —— 它和 `rpJump` 一样是可靠有序的（D5）。
OP_CROUCH = 0x000B

OP_HEARTBEAT = udpsync.OPCODE_HEARTBEAT          # 0x4001

#: `0x4005` —— **加载进度**（body = 一个 int32，取值 0..100，§30）。
#:
#: 客户端在 stage 6 加载关卡的整个过程里广播它，最后一发恒 100，紧接着才发
#: `0x0403`「加载完了」。别人屏幕上那根进度条读的就是它 —— bot 不发，
#: 它那一格就是空的（用户 2026-08-26 实机报的「bot 没有进度条」）。
OP_LOAD_PROGRESS = 0x4005

#: 加载进度的上下限（客户端自己发的最后一发恒 100）。
LOAD_PROGRESS_MIN = 0
LOAD_PROGRESS_MAX = 100

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

#: 位域 bit2 = `[char+0x128]` —— ★★ **「我此刻踩在地面上」**（§35）。
#:
#: ★★★ **不是「我静止着」**。会话 07 按「速度全 0 ↔ bit2=1 相关性 99%」
#: 定的那个名字（§31）是错的：那条相关来自**共同原因**「在地上」，
#: 而不是因果。引入「位置变没变」这第三个变量之后，语料 67186 发分成四格：
#:
#: | 位置变了 | bit2 | 速度全 0 | 发数 |
#: |---|---|---|---|
#: | 否 | 1 | 是 | 28267 —— 站着不动 |
#: | **是** | **1** | **是** | **20341 —— ★ 在地上走：位置在变，速度却是 0** |
#: | 是 | 0 | 否 | 18070 —— 腾空（跳 / 被击飞 / 下落） |
#: | 是 | 1 | 否 | **9** —— 几乎不存在 |
#:
#: 收方拿它分流位置更新（`0x504215`：**上一发**的 `[char+0x128]` == 0
#: 且**这一发** bit2 == 1 才**硬置**坐标，否则 0.6/0.4 插值，常数
#: `0x6937c4`/`0x6937c0`）—— 也就是「落地那一发精确归位、腾空时插值」。
HEARTBEAT_BIT_ONGROUND = 0x04

#: 位域 bit3 = `[char+0x4bc]` —— ★★ **「我在冲刺」**（按住鼠标右键，§40）。
#:
#: 收方拿它把**这个角色整帧的 `dt` 乘上 `FastRunRate`**（`0x507594`）：
#: 走路位移、空中积分、动画播放速率（`0x508041` / `0x5080a9`）全都跟着快，
#: 同时按 `FastRunSpCost` 扣能量（`[char+0x2a4]`），扣光了就地清掉这一位。
#:
#: 语料实测（在地上、按着方向键的帧）：置起时每帧 `|dx|` 中位数 **33**、
#: 没置起 **22** —— 正好 1.5 倍，也就是这一版配置里的 `FastRunRate`。
#: 1003 : 3 的比例说明它**只在真的在走的时候**出现（原版进冲刺的条件里
#: 就有一条 `[char+0x4b4] != 0`，`0x515ced`）。
#:
#: ⚠ **扬尘特效（`CH_Common/efx/FastRun00.efx`）不归它管**：全镜像只有
#: `0x515d29` 一处引用，在**本地输入处理**里 —— 别人在你屏幕上冲刺时原版
#: 也没有扬尘（§40）。这一位管的是速度和腿的动画速率。
HEARTBEAT_BIT_FASTRUN = 0x08

# ---------------------------------------------------------------------------
# ★★★ body `+23..24` 的六位掩码 = **方向键的按下状态**（§39）
# ---------------------------------------------------------------------------
#: 收方 `0x5042fc` 把这 6 个位摊回 `[char+0x2b8 + i*4]`
#: （置位写 `0x41`、未置写 `0x10`，走路那段读的是它们的 **bit0**）。
#:
#: ★★★ **走路动画和收方的本地行走全靠它**，不是靠坐标在变（§39）：
#:
#: * `0x5073c2`：`[0x2b8]&1` → 走路方向 `[char+0x4b4] = −1`；
#:   `[0x2c0]&1` → `+1`；两个都没有 → `0`。
#: * `0x507fb5`（动画选择）：踩地 + 方向 `0` → **`Stand%02d`（站着）**；
#:   方向 == 朝向 → `Run-F%02d`；方向 ≠ 朝向 → `Run-B%02d`（倒着走）。
#: * `0x507660`：踩地时收方**自己**把角色按「走路速度 × 方向 × dt」挪过去，
#:   心跳只做 0.6/0.4 的位置修正。
#:
#: ⇒ 掩码填 0 的 bot：收方画站姿、也不替它走，每收一发心跳位置被拉一格
#: —— 用户 2026-08-26 第三轮实机报的「**没有走路动画、一格一格地平移**」。
#:
#: 语料实证（67186 发，按同座位相邻两发比位移）：
#: 在地上往右走 bit2 置起 9070 : 掩码全 0 1010；往左走 bit0 置起 7330 : 931；
#: 站着不动掩码全 0 26744 : 置起 2122。
KEY_LEFT = 0x01
KEY_UP = 0x02
KEY_RIGHT = 0x04
KEY_DOWN = 0x08

#: 掩码里 bot 用不到、语料里也从没出现过的两位（bit4 / bit5，恒 0）。
KEY_MASK_ALL = 0x3F


def walk_keys(direction):
    """把「我这一帧往哪边走」翻译成心跳里的**按键掩码**（§39）。

    `direction` > 0 → 右键、< 0 → 左键、== 0 → 一个键都没按（站着）。
    传 `FACING_RIGHT` / `FACING_LEFT` 正好，两边取值是一样的。
    """
    if direction > 0:
        return KEY_RIGHT
    if direction < 0:
        return KEY_LEFT
    return 0


#: 准星的**世界坐标**，body `+25..28`，收方存进 `[char+0x680]`/`[0x684]`。
#:
#: ★★ **不是屏幕坐标**（§36 推翻了 §25）：发侧 `0x4295cb` 先用 `0x429569`
#: 取光标的屏幕坐标，**再经 `0x5cc42f`（this = `[0x6e9b94]` 视口）转成世界
#: 坐标**才写进包里。语料实测：`cx` 的 p95 = 5056（远超任何屏幕宽度）、
#: 和角色世界 x 相关系数 0.47、`|准星 − 角色|` 中位数 529。
#:
#: ⇒ 它决定**角色的身体朝向**（用户 2026-08-26 实机：「走路时身体朝向是
#: 跟着鼠标瞄准位置走的」）。填一个死常数 = bot 永远瞄着地图左上角，
#: 也就是用户看到的「全是头看向屏幕这个方向」。**bot 必须算一个跟着自己
#: 走的瞄准点**，见 `aim_point()`。
#:
#: 这个常数只留给「还不知道自己站在哪」的兜底路径。
CURSOR_DEFAULT = (512, 384)

#: bot 的瞄准点摆在自己前方多远（世界坐标单位）。
#:
#: 取语料里 `|准星 − 角色|` 的中位数 **529**（67186 发），量级和真人一致。
#: ★ 它只影响朝向和身体姿势这类外观，不参与任何判定；M5 真要瞄人的时候
#: 这个点会换成「敌人所在的位置」，那时本常数只当兜底。
AIM_DISTANCE = 529.0

#: body `+15` = `[char+0x594]`：语料里 59117/67186 是 0，其余是零散的 34~80。
#: 语义未知，填 0。
HEARTBEAT_STATE_BYTE = 0

#: body `+17..18` = `[char+0x584]` 的**度数**。
#:
#: ★ 发侧 `× 57.29577637`（= 180/π，`0x69379c`）、收侧 `× 0.017453292`
#: （= π/180，`0x693778`）⇒ `[char+0x584]` 是**弧度角**，线上传的是**度**。
#: （§3 / §24 把这一格写成「速度」，那是按取值猜的，**错了** —— 见 §25。）
#:
#: ★★ 语义已经查实（§37）：它是**瞄准方向相对「面朝方向」的仰角**，
#: y 轴向下为正 —— 朝右时 `atan2(准星y − 我y, 准星x − 我x)`、朝左时把
#: x 分量取反再算。语料对穿的中位误差：朝右 6.9°、朝左 7.4°。
#: 收侧 `0x504338` 会把它折回渲染角（`a > π/2` -> `π − a`）。
#: ⇒ **它和准星、朝向位是同一件事的三种说法，bot 必须三个一起算**
#: （`aim_state()`）。这个 0 只留给兜底路径。
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


def aim_point(x, y, facing, distance=AIM_DISTANCE):
    """把「站在 (x, y)、面朝 `facing`」翻译成一个**世界坐标的瞄准点**。

    真人那个点是鼠标位置转出来的（§36），bot 没有鼠标 —— 就放在自己正前方
    `distance` 远、同高度的地方。够让身体朝向、上半身姿势和走的方向一致。

    ★★ **M5 要把这个点换成「敌人 / 瞄准目标的世界坐标」**（用户 2026-08-26
    提醒）：这个游戏的朝向跟的是准星、不是移动方向，所以「一边后退一边朝
    身后开枪」是**合法且常见**的姿势 —— 收方那边就是 `Run-B%02d` 那条分支
    （§39）。换掉这一个函数就够了，朝向位 / 角度 / 正走还是倒走都由
    `aim_state()` 自动跟着变。**现在摆在正前方 = bot 永远朝前走**，
    那是「还没有战斗目标」时的兜底，不是设计上的限制。
    """
    return (x + (AIM_DISTANCE if distance is None else distance)
            * (1 if int(facing) >= 0 else -1), y)


def aim_state(x, y, cursor, facing=None):
    """由「我在哪」+「瞄哪儿」算出**互相自洽**的 `(facing, angle_deg, cursor)`。

    三个字段说的是同一件事，必须一起算（§37）：

    * **朝向位**（位域 bit0..1）跟的是「准星在我左边还是右边」——
      语料对穿：`facing = +1` 时准星在右 31829 : 702（**97.8%**），
      `facing = -1` 时准星在左 16311 : 5943（73.3%）。
      ⇒ 「+1 朝右 / −1 朝左」不再是 §25 那个 68% 的倾向，是**实证**。
    * **角度**是**相对面朝方向**的仰角（y 向下为正），所以朝左时要把 x
      分量取反再 `atan2`。

    `facing` 传 `None`（默认）= 按准星在哪一侧自己定；显式传值时只用来
    决定角度的镜像方向，**朝向位仍然照准星算** —— 免得出现「说自己朝右、
    却瞄着左边」这种真客户端里不存在的组合。
    """
    dx, dy = cursor[0] - x, cursor[1] - y
    if dx or dy:
        side = FACING_RIGHT if dx >= 0 else FACING_LEFT
    else:
        side = FACING_RIGHT if facing is None or int(facing) >= 0 else FACING_LEFT
    angle = math.degrees(math.atan2(dy, dx if side == FACING_RIGHT else -dx))
    return side, angle, cursor


def character_state(x, y, vx=0, vy=0, facing=FACING_RIGHT, on_ground=True,
                    angle_deg=HEARTBEAT_ANGLE_DEG, cursor=None,
                    keys=0, fast_run=False,
                    state_byte=HEARTBEAT_STATE_BYTE):
    """那 24 字节角色状态结构（心跳 body `+7..30`）。

    逐字段的收发两侧对照见 `FINDINGS.md` §24 / §25，四处勘误见 §35 ~ §37 + §39。
    两处「序列化器从没写过」的填充（结构 `+0x09` / `+0x16`）**收方也不读**，
    一律填 0。

    ## ★★★ `keys`：走路动画的**唯一**开关（§39）

    六位掩码（`+0x10`）是**方向键的按下状态**，不是什么 buff 位。收方
    `0x5073c2` 拿它算出走路方向 `[char+0x4b4]`，`0x507fb5` 再拿走路方向
    选动画（`0` → `Stand%02d`、非 0 → `Run-F/B%02d`），`0x507660` 还会
    照着它**自己**把角色走过去。所以：

    * 掩码填 0 而坐标一直在变 = 收方画一个站着的人被一格一格拖过去
      （用户 2026-08-26 第三轮实机报的症状）；
    * 走的时候按 `walk_keys(方向)` 置起对应的位，动画和位移就都对了。

    ★ **腾空时调用方应当填 0**：那一段动画是 `Jump`（不看掩码），而收方
    `0x507402` 会拿按键**覆写**空中速度（`× 1.5`），把抄来的抛体速度冲掉。

    ## ★★ `fast_run`：真人按着右键跑的那一段（§40）

    收方拿它把这个角色的 `dt` 乘上 `FastRunRate`（这一版配置 = 1.5）——
    **位移和腿的动画速率一起变快**。bot 抄的坐标本来就是 1.5 倍步长的，
    不跟着报这一位，收方只按普通走速替它挪、心跳再一发发把它拽回来。
    ★ 只有「在地上、真的在走」时才该置起（原版进冲刺就要求 `[0x4b4] != 0`）。

    ## ★★★ `on_ground` 和速度两格：调用方**必须一起给对**（§35）

    `on_ground=True`（踩在地上）时速度**就该是 0**，哪怕角色正在走 ——
    真人的包就是这样（20341 发「位置在变、bit2=1、速度 0」）。
    地上走却填非 0 速度，收方会拿那个速度自己往前推算，和下一发心跳里的
    坐标一打架就是「走一步、停一下」的抽搐，而且**不播走路动画**
    （用户 2026-08-26 第二轮实机报的症状）。

    所以这里不再替调用方猜：**踩地时速度被强制归零**，并在两者明显矛盾时
    以 `on_ground` 为准 —— 谁在地上谁腾空，是回放真人轨迹时抄来的事实
    （`bot.trail_point`），不该由这一层反推。

    `cursor` 传 `None` = 按 `facing` 在正前方自己摆一个（`aim_point`），
    朝向位和角度都跟着它算（`aim_state`），三个字段因此永远自洽。
    """
    if cursor is None:
        cursor = aim_point(x, y, facing)
    facing, angle_deg, cursor = aim_state(x, y, cursor, facing)
    on_ground = bool(on_ground)
    packed_vx, packed_vy = ((0, 0) if on_ground
                            else (clamp_i16(vx), clamp_i16(vy)))
    field = int(facing) & 0x03
    if on_ground:
        field |= HEARTBEAT_BIT_ONGROUND
    if fast_run:
        field |= HEARTBEAT_BIT_FASTRUN
    return _CHARACTER_STATE.pack(
        clamp_i16(x), clamp_i16(y),              # +0x00 / +0x02  位置
        packed_vx, packed_vy,                    # +0x04 / +0x06  空中速度
        int(state_byte) & 0xFF,                  # +0x08  [char+0x594]
        0,                                       # +0x09  ★ 填充，收方不读
        clamp_i16(angle_deg),                    # +0x0a  角度（度）
        field,                                   # +0x0c  位域
        int(keys) & KEY_MASK_ALL,                # +0x10  ★ 方向键掩码（§39）
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
_CROUCH = struct.Struct("<BB")
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


def crouch_body(seat, down=True):
    """`0x000b rpCrouch`（2 字节）：座位号 + `1` 蹲下 / `0` 起立（§41）。

    语料 394 发：`+0` 和 `UdpPacket` 头 `+1` 的发送方**100% 一致**，
    `+1` 只有 0（181 发）和 1（213 发）两种取值。

    收方 `0x492f5f` 拿它调 `0x5026e1` 设 `[char+0x2b5]`，那一位管三件事：
    姿势换成 `Crouch*`、**移动速度 × 1/3**（`0x507607`）、
    **体力恢复 × 2**（`0x507250`）。
    """
    return _CROUCH.pack(int(seat) & 0xFF, 1 if down else 0)


def change_weapon_body(seat, weapon_id):
    """`0x0001 rpChangeWeapon`（5 字节）：座位号 + 武器 id。"""
    return _CHANGE_WEAPON.pack(int(seat) & 0xFF, int(weapon_id))


def load_progress_body(percent):
    """`0x4005` 加载进度（4 字节，一个 int32，§30）。

    语料里一次加载就是一串 `0 -> 34 -> 50 -> 62 -> 72 -> 81 -> 90 -> 98 -> 100`，
    最后那发 100 的下一发就是 `0x0403`「加载完了」。**夹到 0..100** ——
    进度条是拿它当百分比画的，越界只会画出画面外去。
    """
    return struct.pack("<i", max(LOAD_PROGRESS_MIN,
                                 min(LOAD_PROGRESS_MAX, int(percent))))


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

    def volatile(self, opcode, body):
        """一发**非**事件包（内层 `>= 0x4000`）—— 收方立刻处理、可丢、不排队。

        心跳走的是上面那个专用口（它还要填 N）；这条是给别的立即包用的，
        眼下只有 `0x4005` 加载进度。★ **一个序号都不许吃**：头 `+8` 恒 0，
        和心跳同一个道理（语料里 618 发 `0x4005` 全是 0）。
        """
        _require(int(opcode) >= RELIABLE_OPCODE_MAX,
                 f"内层 opcode {opcode:#06x} 是事件包，得走 event()")
        with self._lock:
            self._sync_epoch()
            return build_peer_packet(self.conn.my_seat, opcode, body,
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
