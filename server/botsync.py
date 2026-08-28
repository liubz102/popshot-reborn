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
#: `0x0004 rpSplashDamaged` —— **溅射伤到了谁**（body 33 字节，§67）。
#:
#: 收方在处理 `rpExplode` 时会替带 `SplashRange` 的武器创建一个
#: `SplashDamage` 对象（§54 那个多出来的句柄），但**算伤害的还是射手那台
#: 机器**（`0x47eb4e` 的 `IsMine || IsNeutral` 守卫）。bot 没有本机 ⇒
#: 不补这一发，火箭 / 手雷炸在人脚边一滴血都不掉。
OP_SPLASH_DAMAGED = 0x0004

#: `0x0005 rpSetOnFire` —— **地面燃烧**（body 14 字节，§75 / packet_api §5.4d）。
#:
#: 火焰弹（`CreatingClass=FlamingBottle`）炸在地上之后铺的那道火墙。
#: ★ 原版**只有射手那台机器**发这一发（创建点 `0x4829d0` 套在 `IsMine` 门里，
#: §72）—— bot 没有本机，不补这一发就一点火都不着，用户 2026-08-27 报的
#: 「2 号角色 2 号武器扔在地上会持续燃烧，现在 bot 的没有燃烧」就是它。
OP_SET_ON_FIRE = 0x0005

OP_JUMP = 0x0006

#: `0x0007 rpDash` —— **双击左右方向键的近身攻击**（body 11 字节，§64）。
#: 消耗体力（`ChrProps.ini` 的 `DashNN-SpCost`），伤害由射手那台机器自己判。
OP_DASH = 0x0007

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

# ---------------------------------------------------------------------------
# ★★★ 对象句柄（§42 / §43）—— M3b 的地基
# ---------------------------------------------------------------------------
#: 一个 owner 占多大一段句柄区间。客户端 `0x473e65`：
#: `owner = (h − 100000) / 100000 + 10`，`h < 100000` 一律算 20（怪 / 中立）。
HANDLE_SPAN = 100000

#: 座位 0 的区间从这儿开始。`0x405f02: imul ecx,ecx,0x186a0; add ecx,0x186a1`
HANDLE_BASE = 100000

#: **角色**在自己区间里的位置：基址 `+1`。
CHARACTER_HANDLE_OFFSET = 1

#: **弹体**计数器的初值：基址 `+2`（角色占了 `+1`）。
#: 出处 `ProjectileMgr::Reset` `0x47346f`：`0x473520 mov [ebp-8], 0x186a2`
#: = 100002，每个 owner 一格、每格 `+= 0x186a0`，共 30 格（owner 10..39）。
#: ★ 语料实证：14 个文件里**自家弹体句柄的最小值恒等于这个数**，0 例外（§43）。
PROJECTILE_HANDLE_OFFSET = 2

#: 怪 / 中立的 owner 编码（`HandleToOwner` 对 `h < 100000` 的返回值）。
OWNER_NEUTRAL = 20

#: owner 编码的起点：`10 + 座位号`。`rpFire` body `+0` 用的是同一套编码。
OWNER_SEAT_BASE = 10


def character_handle(seat):
    """座位号 -> **角色**的对象句柄（`座位 × 100000 + 100001`）。

    `rpExplode +4`「命中目标的句柄」填的就是它。语料里实测到的
    `100001 / 200001 / 300001 / 400001` 正是座位 0/1/2/3（§23 / §43）。
    """
    return int(seat) * HANDLE_SPAN + HANDLE_BASE + CHARACTER_HANDLE_OFFSET


def projectile_handle(seat, index):
    """座位号 + **本图第几发** -> 那颗子弹会拿到的弹体句柄。

    ★★ 这是 M3b 全靠的那条预测。收方每收到一发 `rpFire` 就从
    `mgr[0x14 + owner*4]` 取当前值当句柄再 `++`（`0x484920` / `0x49172e`），
    计数器在 `ForceReloadTerrain`（开局 / 换图）时重置成本函数的 `index=0`。

    **每个 owner 一格计数器**，所以别人打多少枪都不动 bot 这一格 ——
    只要 bot 自己的发弹数记对了，句柄就一定对（§43）。
    """
    return (int(seat) * HANDLE_SPAN + HANDLE_BASE
            + PROJECTILE_HANDLE_OFFSET + int(index))


def handle_owner(handle):
    """对象句柄 -> owner 编码。**逐指令照抄 `0x473e65`**，别自己简化。

    ```asm
    00473e65  add eax, -0x186a0            ; h − 100000
    00473e6a  jns 0x473e70                 ; ★ 结果 < 0 -> 直接返回 20
    00473e6c  push 0x14 ; pop eax ; ret
    00473e70  cdq ; mov ecx,0x186a0 ; idiv ecx ; add eax,0xa
    ```

    ★ `idiv` 是**朝零截断**的，负数上和 Python 的 `//` 不一样 ——
    不过上面那道 `jns` 已经把负数挡掉了，所以这里用 `//` 是安全的。
    """
    value = int(handle) - HANDLE_BASE
    if value < 0:
        return OWNER_NEUTRAL
    return value // HANDLE_SPAN + OWNER_SEAT_BASE


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

#: ★★★ `rpFire` body `+1` = 发射者的**碰撞排除组**（§63）。**不是武器槽。**
#:
#: 收方把这一格原样写进弹体的 `[proj+0x15c]`（`0x4921ad` → `0x47d7ca`），
#: 而三处碰撞判定 —— `0x47e01e`（该不该撞的谓词）、`0x48042e`（弹体逐对象
#: 扫描）、`0x47e2cc`（火箭末段追踪选目标）—— 都拿它和对方的 `[char+0x15c]`
#: 比：**相同就整个跳过这次碰撞**。`0` 是特例，撞所有人。
#:
#: 取值口径（65927 发语料 + `0x4fb848` 逐指令）：
#:
#:     组队战 / 闯关   队伍号 1 或 2      ← 队友之间不碰撞（友军伤害关掉）
#:     个人战          座位号 + 1        ← 各是各的组，人人可打
#:     怪              8
#:     道具 / 陷阱     255
#:
#: 填错的后果是**静默**的：填成 1 的话，个人战里座位 0 那个人的身上
#: 一发都撞不着（组号相同），弹体从他身体里穿过去 —— 而服务端自己发的
#: `rpExplode` 照样扣血，于是「明明躲开了还掉血」。
FIRE_GROUP_MONSTER = 8
FIRE_GROUP_ITEM = 255


def fire_group(seat_index, team=0):
    """这个座位的弹体该盖哪个碰撞排除组（§63）。

    `team` 就是 `lobby.Seat.team`（`TEAM_NONE` / `TEAM_A` / `TEAM_B`）——
    组队和闯关房里它是 1 / 2，个人战里是 0。
    """
    team = int(team)
    if team in (1, 2):
        return team
    return (int(seat_index) & 0xFF) + 1

#: `rpFire` body `+22`：一次打几发。6975 : 45 : 20 = 1 : 2 : 3。
#: ★ 收侧把它当**弹体总数**用：外层轮数 = `count / SpreadFrags`（整数除法），
#: 每轮造 `SpreadFrags` 颗（§46）。填的比 `SpreadFrags` 小 = 一颗都造不出来。
FIRE_SHOTS_DEFAULT = 1

#: 上限：`0x491f41: cmp [ebp+0x20],0x1e; jge 退出` —— **≥ 30 的整包被丢弃**。
FIRE_SHOTS_MAX = 29

#: `rpFire` body `+18`：力度。`PowerControl=0` 的武器在语料里**恒 1.0**
#: —— 那一格只对蓄力武器（`PowerControl=1 / 2`，8~531）有意义，
#: 具体填多少由 `ballistics.power_for_speed()` 反解。
FIRE_POWER_FIXED = 1.0

_FIRE = struct.Struct("<BBiffffi")
_EXPLODE = struct.Struct("<iiffiif")
_JUMP = struct.Struct("<BB")
_CROUCH = struct.Struct("<BB")
_DASH = struct.Struct("<Bbbff")
_CHANGE_WEAPON = struct.Struct("<Bi")


def fire_body(seat, ammo_id, x, y, angle, power,
              group=None, shots=FIRE_SHOTS_DEFAULT):
    """`0x0002 rpFire`（26 字节）。

    `angle` 是**弧度**（调用方 `atan2`，`0x4176bc`），`power` 实测 1~130。
    `group` = 碰撞排除组（`fire_group()` 算的那个，§63）；不给就按
    「个人战」当作 `座位 + 1` —— **别再填死 1 了**，那正是「躲开了还掉血」。

    ★ **包里没有弹体句柄** —— 收方按同样顺序自己分配。这就是「丢一发就
    永久错位、打不死人」的机制（V0.2 §216 / §217），也是 D5 那三条不变式
    存在的全部理由。
    """
    if group is None:
        group = fire_group(seat)
    return _FIRE.pack(FIRE_SOURCE_PLAYER_BASE + (int(seat) & 0xFF),
                      int(group) & 0xFF, int(ammo_id),
                      float(x), float(y), float(angle), float(power),
                      int(shots))


#: `rpExplode +16` 命中类型。收侧只拿它做表现分流，扣血看的是 `+4` 的目标句柄。
HIT_NONE = 0            # 什么都没打中（打到空气 / 飞出图外）
HIT_OBJECT = 1          # 打中别的对象（破坏物之类）
HIT_CHARACTER = 2       # ★ 打中角色
HIT_CHARACTER_ALT = 5   # 也是打中角色（语料 248 发，和 2 联动的 `+20` 不同）


def explode_body(handle, target_handle, x, y,
                 hit_kind=HIT_NONE, flags=0, damage=3.0):
    """`0x0003 rpExplode`（28 字节）。

    `handle` = 弹体句柄（`projectile_handle()` 算的那个），
    `target_handle` = 命中目标的句柄（`0` = 没命中，角色用 `character_handle()`）。

    ★★ `damage`（`+24`）**就是伤害值**（§42）：分发器 `0x491930` 把它
    朝零截断成 int，原样交给目标角色的 `Character::OnHit`（`0x4ff27d`）——
    **收方不重算**。所以这一格填多少，对面就掉多少血。
    数值取自 `weapon.ini` 的 `Damage`（`server/weapondata.py`），别自己编。

    ⚠ **两个句柄都必须对得上**：收侧 `0x492750` / `0x492856` 查不到就
    **静默丢弃**（不报错、不重试、一局之内不自愈），表现是「子弹飞过去
    不炸、一滴血不掉」。
    """
    return _EXPLODE.pack(int(handle), int(target_handle), float(x), float(y),
                         int(hit_kind), int(flags), float(damage))


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


_SPLASH = struct.Struct("<iifBffffi")


def splash_body(source_handle, target_handle, damage, x, y,
                push_x=0.0, push_y=0.0):
    """`0x0004 rpSplashDamaged`（33 字节）：**这个爆炸溅到了那个人**（§67）。

    ```
    +0   i32  伤害源的句柄（弹体 / 溅射对象 / 冲刺伤害对象）
    +4   i32  ★ 受害者的**角色句柄**（`character_handle()`）
    +8   f32  ★ 伤害值
    +12  u8   语料 13160 发**恒 0**
    +13  f32  击退向量 X（±15 / ±4 那一类）
    +17  f32  击退向量 Y（观测多为负 = 往上顶）
    +21  f32  受击点 X
    +25  f32  受击点 Y
    +29  i32  语料 13160 发**恒 0**
    ```

    出处：组包点 `0x492b83`（§23 已经量出长度 33 和字段宽度），字段含义是
    从 13160 发真人语料反推的 —— `+4` 全部是 `座位×100000+100001` 那一族
    角色句柄，`+8` 落在 0~23 的整数伤害上，`+21/+25` 是地图坐标。

    ⚠ 这一发**不吃弹体句柄**（它不创建对象，只是报「谁被溅到了」），
    但它是事件包，照样吃一个事件序号。
    """
    return _SPLASH.pack(int(source_handle), int(target_handle), float(damage),
                        0, float(push_x), float(push_y),
                        float(x), float(y), 0)


_SET_ON_FIRE = struct.Struct("<BBffi")

#: 火墙的碰撞排除组：`SplashTeam` 一个武器都没填 ⇒ 恒 **255 = 撞所有人**
#: （§69 / packet_api §5.4d）。队友站上去照样烧。
FIRE_GROUP_EVERYONE = 255


def set_on_fire_body(seat, x, y, slice_id, group=FIRE_GROUP_EVERYONE):
    """`0x0005 rpSetOnFire`（14 字节）：**在这儿铺一道火墙**（§75）。

    ```
    +0   u8   `10 + 座位号`（owner，和 rpFire body+0 同一套编码）
    +1   u8   碰撞排除组 —— `SplashTeam` 没填就是 255（撞所有人）
    +2   f32  X（着火点，`[char+0x34]` 那一族坐标）
    +6   f32  Y
    +10  i32  `SliceId` —— 火焰那一节的武器 id
    ```

    出处：组包点 `0x4923e2`、调用现场 `0x4829d0`（packet_api §5.4d）。
    """
    return _SET_ON_FIRE.pack(
        FIRE_SOURCE_PLAYER_BASE + (int(seat) & 0xFF), int(group) & 0xFF,
                             float(x), float(y), int(slice_id))


def fire_wall_handles(spawn_count):
    """★★ 一发 `rpSetOnFire` **在收方吃掉几个弹体句柄**（§75）。

    收侧 `OnSetOnFire`（`0x492471`）：

        0x4924a3  mov eax, [weapondef + 0xb8]   ; SpawnCount（缺省 4）
        0x4924a9  lea eax, [eax + eax + 1]      ; ★ 2n + 1

    语料两处独立对上：`ch01-02a` `SpawnCount=4` ⇒ 9，而 §70 量到
    `FlamingBottle` 的句柄残差主峰正是 **+9**；`ch100-02a` / `ch103-02a`
    `SpawnCount=8` ⇒ 17，对应残差的另一个峰 **+17**。

    ⚠ 不跟着推进这个数的话，之后每一发 `rpExplode` 都会对不上号、被静默
    丢弃 —— 「子弹照飞、一滴血不掉」，一局之内不自愈（§42）。
    """
    return 2 * int(spawn_count or 0) + 1


#: 冲刺攻击的方向（body `+1`）。客户端 `0x515b32` 双击 ← 发 `-1`、
#: `0x515b9a` 双击 → 发 `+1`。
DASH_LEFT = -1
DASH_RIGHT = 1


def dash_body(seat, direction, index, x, y):
    """`0x0007 rpDash`（11 字节）：**双击左右方向键的近身攻击**（§64）。

    ```
    +0  u8   座位号
    +1  i8   方向：-1 左 / +1 右   ← `0x515b32` push -1 / `0x515b9a` push 1
    +2  u8   第几式（`ChrProps.ini` 的 `DashNN`，0..5）← `[char+0x5d0]`
    +3  f32  发起时的角色 X       ← `[char+0x34]`
    +7  f32  发起时的角色 Y       ← `[char+0x38]`
    ```

    出处：组包点 `0x492d83`（三发 `0x5d5901` 一字节 + 两发 `0x5d591f` 四字节），
    调用现场 `0x51515c`（`push [char+0x2ac]` = 座位号、`lea esi,[char+0x34]`
    = 坐标），再往上就是 `0x515b03` / `0x515b6a` 那两段**双击判定**
    （同一个方向键两次按下相隔 `< 0xfa` = 250 ms）。

    ★ 伤害**不在这一发里** —— `ChrProps.ini` 的 `DashNN-Damage`
    是射手那台机器自己算的，命中之后另发扣血包。bot 这边同理（`bot.py`）。
    """
    return _DASH.pack(int(seat) & 0xFF,
                      DASH_RIGHT if int(direction) >= 0 else DASH_LEFT,
                      int(index) & 0xFF, float(x), float(y))


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
                 "dropped", "broken", "projectiles")

    def __init__(self, conn):
        #: 这条流属于哪个 `BotConn`（局号和座位号都从它身上问）。
        self.conn = conn
        #: 已经发出去的**事件包**数 = 下一个事件包的序号 = 心跳里的 N。
        self.events = 0
        #: ★★ **本图已经发出去的弹体数** —— 收方那边的弹体句柄计数器
        #: 就是按这个数往前走的（§42 / §43）。下一发子弹的句柄 =
        #: `projectile_handle(座位, self.projectiles)`。
        #:
        #: ★ 清零时机**不是**换代，是**换图**（客户端那边叫
        #: `ForceReloadTerrain`）—— `reset_projectiles()` 由
        #: `BotConn.reset_battle_frame()` 调，那正好是开局 / 换图两处，
        #: 和 `gameserver.reset_sync_trails()` 同一个口径（D28 的硬约束 2）。
        self.projectiles = 0
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
        """一发心跳。**N 恒等于已发出的事件包数**（不变式 2）。

        ⚠ 心跳的 N **不是**「收方什么时候执行事件包」的开关（旧 §50 是这么
        以为的，错了 —— 见 §52）：收方每帧都 flush，事件包一入队就把队列上界
        抬到 `seq+1`。N 的作用是**丢包时的兜底上界** —— 它比收到的最大序号
        还大，就说明中间有空洞，收方据此发 `0x4002` 讨重传。
        """
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

    # -- 弹体句柄（M3b）-----------------------------------------------------
    def reset_projectiles(self):
        """本图的发弹数清零 —— 对称于客户端的 `ForceReloadTerrain`。

        由 `BotConn.reset_battle_frame()` 调（开局 / 换图各一次）。
        **不要挂在换代上**：客户端重置弹体管理器看的是「重新加载地形」这件事，
        和局号 `+1` 不是同一个事件（D28 的硬约束 2）。
        """
        with self._lock:
            self.projectiles = 0

    def fire(self, ammo_id, x, y, angle, power, handle_step, shots=1,
             source_seat=None, group=None, **kwargs):
        """一发 `rpFire`，**同时把句柄记账推进**。返回 `(包, 头一颗弹体的句柄)`。

        ★★ 组包和记账**必须在一次加锁里做完**：句柄是「收方按到达顺序自己
        分配」的，服务端这边只要有两发交错，预测的号就和收方的错开 ——
        而错开的后果是 `rpExplode` 被静默丢弃（§42），一局之内不自愈。

        `shots` = 这一发造几颗弹体（`weapondata.Weapon.shots` = `SpreadFrags`）。
        收侧 `OnFire` 在**开火那一刻**就把这几颗连着注册完（`0x49231e` 的
        `call 0x473e7c` 在内层循环里，§46），所以它们的句柄是
        `base + 0 … base + shots − 1`，调用方按这个序去发 `rpExplode`。

        `handle_step` = 这一发在**开火那一刻**吃掉几个句柄。
        ★★★ 正常路径下调用方传的是 `weapon.fire_step`（= `shots`），
        爆炸时那几个溅射对象由 `explode()` 单独记（§86）——
        **别再把 `weapon.handle_step`（总数）直接传进来**：分裂弹的 4 片
        碎片是同时在飞的，按总数记会把它们的号排成 `base, base+2, …`，
        而收方给的是连号，从此每一发 `rpExplode` 都被静默丢弃（§42）。

        `group` = body `+1` 的**碰撞排除组**（§63）。不给就退成
        「个人战」口径（座位 + 1）—— 组队房里调用方必须自己传队伍号，
        否则 bot 的子弹会从队友身上穿过去 / 撞在本该无视的人身上。
        """
        shots = int(shots)
        _require(1 <= shots <= FIRE_SHOTS_MAX,
                 f"一发打 {shots} 颗不合法（收侧 0x491f41 丢弃 ≥ 30 的整包）")
        _require(isinstance(handle_step, int) and handle_step >= shots,
                 f"弹体句柄步进 {handle_step!r} 小于弹体数 {shots}，这把武器不能用")
        # ★ `source_seat` 只有**取证**才会传（`bot.BOT_DIAG_FIRE_ANYWHERE`）：
        #   把 body `+0` 的 owner 换成别人的座位，看收方画不画得出来。
        #   包头 `+1` **不动** —— 那一格决定包进哪条收包队列，动了序号就乱了。
        #   ⚠ 换了 owner 之后句柄记账必然和收方对不上（收方按 body 的 owner
        #     分配），所以这条路**只能用来看画面，不能用来验命中**。
        with self._lock:
            handle = projectile_handle(self.conn.my_seat, self.projectiles)
            packet = self.event(OP_FIRE, fire_body(
                self.conn.my_seat if source_seat is None else int(source_seat),
                ammo_id, x, y, angle, power,
                group=group, shots=shots, **kwargs))
            self.projectiles += int(handle_step)
            return packet, handle

    def explode(self, handle, target_handle, x, y, hit_kind, damage,
                spawns):
        """一发 `rpExplode`，**同时把爆炸对象的句柄记账推进 `spawns` 个**。

        ★★★ `spawns` = `weapon.explode_step`（带溅射的 1、不带的 0，§86）。
        收方处理这一发时会创建那个溅射对象，它和弹体**共用同一个句柄
        计数器** —— 少记一个，之后每一发 `rpExplode` 都对不上号、被静默
        丢弃（§42），一局之内不自愈。

        和 `fire()` 同一个理由：组包和记账**必须在一次加锁里做完**。
        """
        with self._lock:
            packet = self.event(OP_EXPLODE, explode_body(
                handle, target_handle, x, y,
                hit_kind=hit_kind, damage=damage))
            self.projectiles += int(spawns)
            return packet

    def set_on_fire(self, x, y, slice_id, spawn_count):
        """一发 `rpSetOnFire`（地面燃烧），**同时把句柄记账推进 `2n+1`**。

        返回 `(包, 这道火墙吃掉的句柄数)`。

        ★ 和 `dash()` 同一个道理：收方 `OnSetOnFire`（`0x492471`）会替这道
        火墙创建一串对象，它们和弹体**共用同一个句柄计数器**。数错了之后
        每一发 `rpExplode` 都会被静默丢弃（§42），所以组包和记账必须在
        **同一次加锁**里做完。数目见 `fire_wall_handles()`（§75）。
        """
        with self._lock:
            step = fire_wall_handles(spawn_count)
            packet = self.event(OP_SET_ON_FIRE, set_on_fire_body(
                self.conn.my_seat, x, y, slice_id))
            self.projectiles += step
            return packet, step

    def dash(self, direction, index, x, y):
        """一发 `rpDash`（近身冲刺攻击），**同时把句柄记账推进 1**。

        返回 `(包, 这一下的伤害对象句柄)`。

        ## ★★★ 为什么这里也要动句柄

        收方处理 `rpDash` 时会创建一个 `DashDamage` 对象（`0x502229` 那一发
        `ProjectileMgr::Add`），它和弹体**共用同一个句柄计数器** ——
        也就是说**每发一次冲刺，收方那一格就往前走一格**。
        服务端不跟着走，之后每一发 `rpExplode` 都会对不上号、被静默丢弃，
        表现是「子弹照飞、一滴血不掉」，而且一局之内不自愈（§42）。

        **消耗恰好 1 个**是从语料量出来的（§64）：按「上一发 `rpFire` 的
        基址 + `handle_step`」预测下一发的基址，中间夹 0 / 1 / 2 / 3 发
        `rpDash` 时残差分别是 0 / 1 / 2 / 3，占比和「不夹 dash」那一档的
        基线噪声一样（2392/2640 对 27076/32838）。
        """
        with self._lock:
            handle = projectile_handle(self.conn.my_seat, self.projectiles)
            packet = self.event(OP_DASH, dash_body(
                self.conn.my_seat, direction, index, x, y))
            self.projectiles += 1
            return packet, handle

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
