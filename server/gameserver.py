#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gameserver.py —— 假游戏服（阶段 5 里程碑 B），监听 127.0.0.1:27799

和认证服（47611 / NMCO / nmconew.dll）完全是两套协议。这一层是游戏自有的：

整条 TCP 流被 SimpleCipher 逐字节加密（见 server/simple.py），**先解密再分帧**。

握手：
    1. 客户端连上后立刻发 4 字节 int32 = 311（版本号，硬编码 0x54d98f）
    2. 服务端回一个 0xFE 控制帧，载荷 = int32 结果码（0 = 版本 OK）
       客户端 ServerConnection 虚表槽12 (0x54dbf6) 处理：
           读 int32；==0 → 0x54d67c（上报 Dump\\LastCrashReport.txt，没有就跳过）
                            → 槽15 0x54d520 组 opcode 0x0100 登录包发出
                      !=0 → 再读一个字符串，走升级/报错分支
    3. 之后双向都是 0xFF 游戏帧

帧格式（两种，客户端在 0x5bcb19 的接收循环里按首字节区分）：
    0xFE 控制帧（4 字节头）
        [0]     0xFE
        [1]     未用
        [2..3]  u16 LE 载荷长度
        [4..]   载荷           → 交给虚表槽12
    0xFF 游戏帧（10 字节头）
        [0]     0xFF
        [1]     未用
        [2..3]  u16 LE 载荷长度（= 总长 - 10）
        [4..5]  u16 = 0
        [6..7]  u16 = 0        （TcpConnection::Send 0x5bc9c1 固定传 0）
        [8..9]  u16 = opcode   ★ RawPacket::SetType 0x5bba0a 写这里
        [10..]  载荷           → 交给虚表槽13

载荷里的基本类型（RawPacket 的读写函数）：
    int32   4 字节小端
    string  u16 字符数 + UTF-16LE

用法：
    python server/gameserver.py                 # 回版本 OK，然后只收不回并解码日志
    python server/gameserver.py --hold          # 连版本都不回，纯抓包
    python server/gameserver.py --version-result 1
"""
import argparse
import contextlib
import datetime
import os
import random
import select
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from simple import SimpleCipher
from account_store import (AccountStore, BASE_CHARACTER_IDS,
                           PREMIUM_CHARACTER_IDS, QUEST_DIFFICULTY_MAX,
                           character_item_id, character_item_ids,
                           character_unlock_all, display_name,
                           experience_bounds, owned_characters,
                           player_character, player_experience, player_level,
                           player_money, quest_cleared_difficulty,
                           quest_difficulty_records, quest_unlock_all,
                           tutorial_state)
import eventlog
import lobby as lobby_module
# ★ `SESSION_STATUS_WAITING` 不从 lobby 导入：本模块下面有一份带完整考据的
#   同名常量（V0.1 §102），两处值必须一样，import 进来只会让人以为它只有一处定义。
from lobby import (Lobby, Seat, SESSION_TYPE_GAME_TYPES,
                   MOVE_INTO_ALREADY_PLAYING,
                   MOVE_INTO_BAD_PASSWORD, MOVE_INTO_FULL,
                   MOVE_INTO_NO_SUCH_ROOM, MOVE_INTO_OK,
                   TEAM_A, TEAM_B, TEAM_NONE, TEAM_LAYOUT_COOP,
                   TEAM_LAYOUT_FREE, TEAM_LAYOUT_TEAMS, default_team,
                   item_mode_of, team_layout_of)
from netlisten import create_listener, describe as describe_listen, tune_stream
import relayserver
from tickets import TicketStore, short as short_ticket
import udpsync
import versioning

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGDIR = os.path.join(ROOT, "logs")
os.makedirs(LOGDIR, exist_ok=True)

CLIENT_VERSION = 311        # 0x137，硬编码在 0x54d98f

#: 版本门禁拒绝时回给客户端的结果码。客户端只判「非 0」：非 0 就再读一个
#: 字符串、走原版自带的「升级/报错」分支弹框（re/packet_api.md §1.2），
#: 拉起 game_patched\BsPatcherChn.exe（= 我们自己的更新引导器）。
#: 达标的正常连接永远回 0（`app.py` 固定 `version_result = 0`）。
VERSION_REJECT_RESULT = 1
#: 版本门禁拒绝时附带的提示文案（兜底版：BUILD.ver 读不出服务器自身版本
#: 时用它）。正常情况走 `version_reject_message()` —— 文案里**带上服务器
#: 自己的版本号**，客户端更新器（updater\src\probe.c 的探针）连上来
#: 重演一次握手，从这句话里解析出该升到哪个版本，保证「成对发布」的
#: 客户端 / 服务端批次对得上（D079）。改文案时必须保留版本号的格式
#: ``V主.次.修订``（`versioning.format_version` 的输出）。
VERSION_REJECT_MESSAGE = "客户端版本过旧，请下载最新版客户端后再连接。"


def version_reject_message(root=None):
    """拒绝文案：能读到服务器自身版本就带版本号，读不到用兜底文案。

    ★ 带的是**服务器自己的版本**（包根 BUILD.ver），不是门禁的最低版本
      （server-ClientFilter.config）—— 例：服务器 0.2.15 / 最低 0.2.10 /
      客户端 0.2.7 被拒，文案说的是「请更新到 V0.2.15」：客户端要和服务器
      **同批次**（成对发布，D079），而不是踩着最低线当个「半新不旧」的版本。
    ★ 这句话是**给机器读的协议**，不只是给人看的：探针按
      ``[vV]数字.数字[.数字]`` 的模式从文案里抠版本号。
    """
    own, warnings = versioning.load_own_version(root)
    if own is not None:
        return (f"客户端版本过旧，请更新到 {versioning.format_version(own)} "
                f"后再连接。")
    for warning in warnings:
        print(f"[版本门禁] {warning}", file=sys.stderr)
    return VERSION_REJECT_MESSAGE

MAGIC_CTRL = 0xFE
MAGIC_GAME = 0xFF

#: 游戏服方向的发送截止时间（秒）。战斗包只有几十字节，正常客户端一帧
#: （16 ms）内就收走了；超过这个数还不收包的客户端要么已经崩了卡在
#: 写 dump / WER 弹窗上，要么网络半死 —— 再等只会把所有往它广播的线程
#: 一起拖死（见 `send_all_bounded` 的注释）。
GAME_SEND_DEADLINE_S = 8.0

OP_REP_LOGIN = 0x0100
#: `gspRepLogin` 的结果码 3 = 客户端 `0x54f3cf`「断开」（V0.1 §44）。
#: 弹的框是 `Data\Chinese.ini` 里的「在无法连接的地方尝试了连接。」（§132）。
#:
#: ★ **只留给「压根没带票据」这种协议级错误**（空字符串 / 解不出来）。
#:   一张认不出来的票据**不再**回它 —— 见 `LOGIN_RESULT_SUPERSEDED` 和 D097。
LOGIN_RESULT_BAD_TICKET = 3
#: 结果码 2 = 客户端 `0x54f416`，和结果码 3 的分支**一模一样**（都是
#: `[conn+0x898]=0` → `0x5bc415` 断开 → 弹框），只有文案不同：
#: 「现有连接已断开。请重新尝试连接。」
#:
#: ★★ **所有「票据认不出来」的情况都回这个码**（D097）：被顶号（§132 / D071）、
#:   过期、以及**服务端重启后客户端拿旧票据来重连**（票据只在内存里，D097）。
#:   三种情况玩家该做的事完全一样 —— 重新登录一次 —— 而这句话正是这个意思；
#:   回 3 那句「在无法连接的地方尝试了连接。」只会让人以为是被封 IP 或走错服务器。
LOGIN_RESULT_SUPERSEDED = 2
#: ★★★ 服务端 -> 客户端：**告诉客户端「你自己叫什么」**（§222）。
#:
#: 分发 `0x54e036` 的 switch 把 0x0102 送到 `ServerConnection` 虚表**槽 17**
#: （`0x54f23a`）。那个函数只做三件事：
#:
#:     读一个 wstring（`0x5d5b3a` = u16 字符数 + UTF-16LE）
#:     -> `[0x72e2a4]+0x14`
#:     -> ★ 全局 wstring **`0x72e328`** = 「本机玩家自己的名字」
#:
#: `0x72e328` 是**唯一**的来源：全镜像写它的只有这个函数体
#: （另一条连接上的 `0x5568c1` 是它一模一样的副本），其余全是读。而读它的地方包括
#: 建房对话框的默认房间名（`0x43616f`：`Format(dest, tr("%s님과 가볍게 한겜"),
#: [0x72e328])`，`Chinese.ini` 译文是「与%s一起游戏吧!」）以及
#: `0x422c5f` 那种「这条消息是不是我自己发的」比较。
#:
#: 不发这一发，那个 `%s` 就是空串 —— 用户看到的「与一起游戏吧」正是这个。
OP_SET_PLAYER_NAME = 0x0102
#: 房间列表。两个方向同号：客户端方向是请求（12 字节，§139），
#: 服务端方向是列表（§138）。
OP_LIST_SESSION = 0x0200
#: 加入房间。客户端方向 `gcpReqMoveInto`（int32 房间号 + 密码 + int32），
#: 服务端方向是 4 个 int32 的结果（§140）。
#: ★ **快速加入 `0x0205` 成功时也回这个号** —— `0x0205` 的服务端方向只是
#: 「拿一个本地化 key 弹提示框」（处理器 `0x55027d`），进不了房间。
OP_MOVE_INTO_SESSION = 0x0202
OP_LEAVE_SESSION = 0x0203
#: 快速加入。客户端方向的载荷就是一个 `SessionDescriptor`
#: （序列化 `0x404f59` 直接转发给内嵌的描述符）。
OP_QUICK_JOIN_SESSION = 0x0205
OP_MOVE_CHANNEL_BY_GAME_TYPE = 0x020b
#: 客户端方向：大厅右侧「玩家列表」面板要一页在线玩家（`0x554513`，每 10 秒
#: 一发）。★ **服务端方向的同号包不是列表**（`0x553c5f` 只是个弹窗），
#: 真正的应答是 `0x0212`，见 §166。
OP_REQ_USER_LIST = 0x020d
#: 服务端方向：`gspRepUserList`（vft `0x6918d4`，处理器 `0x55458b`）——
#: 大厅右侧那张玩家列表的**唯一**数据源（§166）。
OP_REP_USER_LIST = 0x0212
#: 服务端方向：`gspQuestReachedDifficulty` = 「每一关你打到第几个难度了」的
#: 全量快照。客户端处理器 `0x5539c2` 先把 map `[0x72e35c]` 清空再逐条灌进去。
#: 不发这个包，那张 map 永远是空的 —— 每一关就只有「简单」能开局（§118）。
#: 客户端方向没有这个 opcode。
OP_QUEST_REACHED_DIFFICULTY = 0x020c
OP_SESSION_MEMBERS = 0x0300
#: 单个座位的变更事件。两个方向都用这个号但**载荷不同**：服务端方向多一个
#: 开头的 action 字节（`0x40648d`），客户端方向没有（`0x558dcb`）。
OP_SESSION_MEMBER_UPDATE = 0x0301
OP_CHANGE_SESSION = 0x0302
OP_UPDATE_SESSION = 0x0303
#: 聊天。客户端方向 `gcpSendChatMsg`（u8 类型 + 正文），服务端方向
#: `gspReceiveChatMsg`（u16 座位号 + 显示名 + 正文 + int32 类型），见 §141。
OP_CHAT = 0x0305
#: `Packet_gspSlotEquippedList`（vft `0x65e0f8`）—— 某个座位的背包/装备清单。
#: ★ **这是「人物选择里有几个头像」的唯一开关**（FINDINGS §119）。
#: 客户端方向没有这个 opcode。
OP_SLOT_EQUIPPED_LIST = 0x030b
#: **同号反向**：客户端方向的 `0x030b` 是 `Packet_gcpKickOut`（vft `0x66ae20`，
#: 序列化 `0x46ba38`）—— 房主在房间里踢人。载荷是
#: `int32 座位号 + int32（由 1 字节零扩展）`。
#: 和上面的座位物品清单**只靠方向区分**，回显它等于把角色清单发成踢人。
OP_KICK_OUT = 0x030b
#: 客户端方向：房间里按「游戏准备 / READY」（或 F5）。载荷是**一个 int32**
#: （由 1 字节零扩展，序列化 `0x558e78`）= 切换之后的准备状态，**不带座位号**
#: —— 谁发的就是谁，服务端按连接认人。
#:
#: ★ 客户端**自己先把 `[座位+0x2e]` 改掉再发**（`0x468e12` 起
#: `cmp/sete/mov`），所以按下去自己那格立刻显示「准备中」；
#: 房里其他人要看到，只能靠服务端把这个座位广播回去（§165）。
#: 服务端方向的同号包是另一回事（`0x40899a`，组队匹配完成的提示框），不要回。
OP_SEAT_READY = 0x030e
OP_REQ_FIRST_USER_RESULT = 0x030f
OP_REP_MONEY = 0x0600
OP_PREPARE_GAME = 0x0400
OP_TRIGGER_COUNT_GAME = 0x0401
OP_COUNT_GAME_READY = 0x0402
OP_LOADING_DONE = 0x0403

#: ★★ 服务端方向的这两发包会让客户端的局号（`[GameSession+0x3c]`）**+1**，
#: 同时 `GameSession::ResetQueues`（`0x407678`）把六条收包队列全部清空
#: —— 也就是「换一代」（§218 / D137）。值是这一代的**种类**，
#: 传给 `lobby.Room.advance_generation()`：
#:
#:   `0x0400 gspPrepareGame` -> `0x551605` -> `0x5517a3`：`inc` / 阶段 4 / 切 stage 6
#:   `0x0403`（结算看完）    -> `0x5518fb` -> `0x551900`：`inc` / 阶段 2 / 切 stage 5
EPOCH_ADVANCING_OPS = {
    OP_PREPARE_GAME: "battle",
    OP_LOADING_DONE: "room",
}

#: ★★ 而这一发是**直接设定**：`0x0303 gspSession` 的包尾 u16 就是局号
#: （`0x406258` -> `0x406756` -> `0x556ed1`，最后两句 `movsx eax, ax` /
#: `mov [LobbyStage+0x3c], eax`）。客户端自己只会 `inc` 和「复位成 -1」，
#: **这是原版留给服务端的唯一一个「说几就是几」的入口** ——
#: 中途进房的人靠它和全房间对齐（D138）。
EPOCH_ASSIGNING_OP = 0x0303

#: ★★ 这两发（**而且业务结果码为 0** 时）会让客户端把 `GameSession` 重建或复位，
#: 局号回到 **-1**、六条队列清空（§218）：
#:
#:   `0x0100 gspRepLogin`(0)          -> `0x54f2cc` 新建 `GameSession`（`0x4050f8` 里复位）
#:   `0x0203 gspRepLeaveSession`(0)   -> `0x54fffe` -> `0x550092` -> `0x552943` -> `0x4054fa`
#:
#: 结果码非 0 时客户端只弹一个错误框，什么都不复位，所以必须验第一个 int32。
#: （还有一发 `0x030a` 也走 `0x4054fa`，我们不发，留个名字在这里备查。）
EPOCH_RESETTING_OPS = frozenset((0x0100, 0x0203))
#: 客户端方向 0x0406（32 字节）= **`gcpCreateItem`「我要在这里生成一个掉落物」**。
#:
#: ⚠ **这是对 §108/§109「位置同步」那个记法的勘误**（会话 17，§112）。
#: `RawPacket::SetType(0x406)` 在整个镜像里**只有一个**调用点 `0x493a57`
#: （`GameContext::SendCreateItem`，序列化 `0x48c84f`），而它写的正是
#: 「物件 id + 坐标 + 速度」这 8 个字段 —— 实测载荷逐字段对得上。
#: 客户端发完就**等服务端回 `0x0404 gspCreatedItem`**，不回就什么都不出现。
OP_CREATE_ITEM = 0x0406
#: 服务端方向 0x0404 = `gspCreatedItem`（处理器 `0x551a11`，分发链 `0x54e0a5`
#: 的 `cmp eax,0x404 / je 0x54e300` 那一格）。**掉落物真正出现在地上靠它**。
OP_CREATED_ITEM = 0x0404
OP_REP_GAME_RESULT = 0x0309
#: 客户端在结算界面上停留约 9 秒后发的空包（会话 11 实测）。当成「结算看完了」，
#: 服务端据此把它切回房间。服务端方向的同号包是另一回事，见 handle 分支的注释。
OP_LEAVE_RESULT = 0x0405

#: 客户端方向 0x0407（8 字节）= **`gcpGetItem`「我踩到这件掉落物了，能捡吗」**
#: （会话 18，§115）。发送点 `GameContext::SendGetItem`（`0x493a99`，序列化
#: `0x558e9a` 写两个 int32），唯一调用方是 `Character::CheckItemPickup`
#: （`0x5154d3`）—— 它遍历 World 的物件表，碰撞成立就发这个包，
#: **然后把 `[item+0x2a8]` 置 1 防止重发**。
#:
#: ★★ 所以服务端不回的后果不是「这一次没捡到」，而是**这件东西从此再也捡不起来**
#: —— 用户报的「走过去捡不起武器和金币」就是这个。
OP_GET_ITEM = 0x0407

#: 服务端方向 0x0405 = **拾取放行**（处理器 `0x551d35`，跳表 `0x54e5ae` 的第 0 格）。
#: 读两个 int32：`座位号 -> [LobbyStage + 座位*4 + 0x1d0]` 取角色对象、
#: `实例句柄 -> World::Find(0x474225)` 取物件，再 `dynamic_cast<Item*>` 成功后调
#: `Item::vf_d4`（`0x51f447`）= 「结算这次拾取」：先 `vf_11c` 生效（金币累加
#: 本局拾取面额并弹数字、红心回血、武器换枪），再 `vf_20` 把物件从世界里删掉。
#:
#: ★ **和客户端方向的 `rawLeaveGameResult` 同号**（D028 的又一例）。
#: 客户端发的那一发是**空载荷**、意思是「结算界面看完了」，两者只能靠方向区分。
OP_PICKED_ITEM = 0x0405

# ---------------------------------------------------------------------------
# ★★★ 道具槽那三个包（§194）—— 「捡到了但道具栏不显示、也用不了」的根因。
#
# `0x0405` 拾取放行只做两件事：把物件从世界里删掉 + 放拾取特效和音效
# （PvP 道具的 `vf_d4` = `0x5224fe`，它调完基类 `0x51f447` 就只剩特效了）。
# **它一个字都没往角色的道具槽里写。**  17 个 PvP 道具建构时第 4 个参数是 1
# （`[item+0x2a9]`），所以基类那条 `if ([+0x2a9]==0) vf_11c()` 的「当场生效」
# 分支对它们**恒不成立** —— 金币 / 红心走的才是那条。
#
# 逐个查过调用点，PvP 一局里这三件事各自**只有一条**通路，全在服务端手上：
#
#   | 要发生的事 | 客户端里唯一的入口 | 谁能触发 |
#   |---|---|---|
#   | 道具进 4 个槽 `[Character+0x764..0x770]` | `Character::AddItem` `0x517037` | **服务端发 `0x040b`**（`0x55206b`）|
#   | 道具离开槽 | `Character::RemoveItem` `0x5170b4` | **服务端发 `0x040c`**（`0x552089`）|
#   | 道具效果生效 | `Character::UseItemEffect` `0x508441` | **服务端发 `0x040a`**（`0x551d95`）|
#
# `AddItem` 的另一个调用点（`0x493ff3`）是换角色时从 `GameContext` 的备份里
# 恢复，不是新道具；`RemoveItem` 和 `UseItemEffect` 则**各自只有那一个**
# 调用点。按 Ctrl 时客户端做的全部事情就是发一发 `0x040c`（槽位恒 0，
# `0x516335` 那段）再放一声音效 —— 效果、扣道具**都等服务端**。
#
# ⇒ 这是 V0.1 §108 / §111 / §113 / §115 和 V0.2 §191 之后**同一个形状的第六条链**。
OP_ITEM_EFFECT = 0x040a       # 服务端 -> 客户端：让某个座位吃到某件道具的效果
OP_GRANT_ITEM = 0x040b        # 服务端 -> 客户端：往**收包这个人**的道具槽里塞一件
OP_USE_ITEM = 0x040c          # 两个方向同号：客户端「我要用第 N 格」/ 服务端「把第 N 格拿掉」

# ---------------------------------------------------------------------------
# ★★★ 第四个道具包：`0x040d`「某个座位的道具效果结束了」（§200）
#
# `0x040a` 只管**开始**。效果**结束**是另一条独立的链，而且它天生只有
# 一台机器知道：
#
#   Character::RemoveAttrEffect  `0x50982e`
#     if ([char+0x2ac] == 我的座位)          ← 只有本机玩家自己的角色才发
#         SendRemoveCharAttr(座位, 属性号)   ← `0x54ec50` -> 序列化 `0x558f8e` -> 0x040d
#     ...按属性号拆掉对应的模型 / 特效...
#
# 收侧 `0x551dfb`（派发表 `0x54e5ae` 第 8 项，基址 0x405）：
#
#   读两个 int32（`0x5590bb`）= (座位, 属性号)
#   if (座位 == 我的座位) return;            ← ★ 自己那一发直接丢掉
#   角色 = 座位 -> 角色（`0x404ff6`）
#   Character::RemoveAttrEffect(属性号)      拆模型 / 特效
#   AttrList::Remove(属性号)                 `[char+0x6a0]`
#
# ⇒ **服务端不转发的话，别人屏幕上那个效果永远不会结束。**
#
# 为什么用户只在「三重射击 / 毒药 / 致命射击」上看得见这个 bug：
# 这三件的 `Status.ini` 记录**没有 `Time`、只有 `Magazine=3`**（§201），
# 于是 `UseItemEffect` 给的 duration 是 **-1（无限）**，真正的结束条件是
# 「本机玩家打完 3 发」—— 只有他自己那台机器数得出来。有 `Time` 的那些
# （护盾 8 秒、加速 8 秒、隐身 10 秒…）每台机器各自倒计时，所以看起来正常。
OP_REMOVE_CHAR_ATTR = 0x040d  # 两个方向同号：客户端「我这个效果结束了」/ 服务端「某座位的效果结束了」

OP_END_QUEST = 0x040f
OP_UPDATE_QUEST_SCORE = 0x0410

#: ★★★ 战斗内联机的三个包（FINDINGS §149 / §150 / §151）。
#:
#: 客户端把「玩家之间」的同步数据装在一个 `UdpPacket`（12 字节头）里，
#: 有两条并行的通道送出去：
#:   通道 B  UDP 直连对方上报的**内网 IP**（客户端自己开的，服务端管不着）
#:   通道 A  `0x040e` 塞进**已有的游戏服连接**（`0x408619`）—— 我们用的就是这条
#: 通道 A 有一个总开关 `[GameSession+0x3e4]`，默认 0、**每次退房都会被清回 0**，
#: 由服务端发 `0x0410`（int32 1）打开。开关一开客户端立刻开始发 `0x040e`。
#:
#: ★ 这三个 opcode 的**客户端方向**另有含义（`0x040f gcpEndQuest`、
#: `0x0410 gcpUpdateQuestScore`），gcp / gsp 是两套独立编号，别混。
OP_PEER_DATA_UP = 0x040e        # 客户端 -> 服务端：包裹好的 UdpPacket
OP_PEER_DATA_DOWN = 0x040f      # 服务端 -> 客户端：**原样**转给同房间的其他人
OP_TOGGLE_PEER_RELAY = 0x0410   # 服务端 -> 客户端：gspToggleUdpClientCommunication
OP_END_GAME = 0x0411

#: 原版 TCP 中继那一路（里程碑 J.3 / D078 / §157）。
#: `0x0310` 是客户端要中继，`0x0210` 是我们把「连哪儿 + 拿什么认人」告诉它，
#: `0x0211` 是唯一安全的拆连接方式（走析构，不触发 `OnDisconnected`，§158）。
OP_START_TCP_RELAY = 0x0310     # 客户端 -> 服务端：gcpStartTcpRelay（8 字节）
OP_JOIN_RELAY = 0x0210          # 服务端 -> 客户端：gspJoinRelay（18 字节）
OP_LEAVE_RELAY = 0x0211         # 服务端 -> 客户端：拆掉中继连接（载荷被无视）
OP_MARK_QUEST_SUCCESS = 0x0417
OP_RESPAWN_CHARACTER = 0x0419

#: 客户端方向 `0x0106 gcpReportHack` —— **客户端自己觉得"这个玩家不对劲"时的上报**。
#: V0.1 §88 拿它当过免费的正确性检查器（服务端把人传送到地图边缘，23 毫秒后
#: 就收到一发）。**只记不回**：客户端发完就继续玩，没有任何应答需求。
#:
#: 载荷是一个 wstring，正文由发现问题的那一处自己拼。已知的一种（§183）：
#:
#:     (FastFire) wpnIdx=%d,lastFireTime=%d,currFireTime=%d,Interval=%d/%d
#:
#: 判据在 `0x51540a`（调用点 `0x5153fb`）：两次开火间隔 < `武器间隔/2` 就计数，
#: 攒到 **5 次**发一发。也就是说**客户端确实有"输入太快就当异常"的逻辑** ——
#: 所以把这行解成人话打进 `[online]`，"连按 A/D 会不会撞上同类判据"就变成了
#: 用户跑一次就能看见的事实，而不用靠猜。
OP_REPORT_HACK = 0x0106

#: 客户端方向 0x0408（18 字节）= **「某个角色 HP 归零了」上报**（会话 15，§108）。
#: 发送点 `Character::OnHpZero` 0x4ffab0 -> `GameContext::ReportDeath` 0x493855
#: -> 序列化 `0x558f16`（`push 0x408`）。**不是遥测，是死亡判定的请求**。
OP_REPORT_HP_ZERO = 0x0408

#: 客户端方向 0x0413 = `gcpRespawnCharacter`（发送点 `0x553e48`，4 个 int32）。
#: 角色死后 5 秒由 `0x4fe78f` 发出，等服务端回 `0x0419` 才真的重生。
OP_REQ_RESPAWN = 0x0413

#: 服务端方向 0x0415 = `gspUpdateQuestScore`（处理器 `0x4a3efe`，两个 int32：
#: 座位 + **累计**分数）。它把分数写进 `[GameContextQuest + 座位*4 + 0x3b8]`，
#: 也就是右上角战绩面板「分数」那一列的唯一数据源（`0x4a4a86` 读它）。
#: 不回这个包，战绩面板的分数永远显示 0（§109）。
OP_REP_QUEST_SCORE = 0x0415

#: 服务端方向 0x0406 = **死亡广播**（和客户端方向的 `gcpCreateItem` 同号、
#: 语义完全不同，D028）。处理器 `0x4938d2`（`GameContext::vf_e0` 的分发表
#: `0x493808` 里 `0x406` 那一格），读 `u32 句柄 + u8 座位 + u8 参数 + u32`，
#: 然后调 `Character::Die()`。
OP_BROADCAST_DEATH = 0x0406

#: `gspRespawnCharacter` 的兜底重生坐标。取自实测 0x0406 包里的
#: float 3225.0 / 635.0（线上是 int32，客户端 fild 转 float）。
#: 正常路径**不用它** —— 重生坐标由客户端在 `0x0413` 里自报，服务端原样回显。
#: 只有调试控制通道 `gs_ctl.py respawn` 在没有任何位置记录时才会落到这里。
DEFAULT_RESPAWN_X = 3225
DEFAULT_RESPAWN_Y = 635

# ---------------------------------------------------------------------------
# 控制权交接（`0x0414 gspChangeControllerSlot`，§180 / D103）
#
# ★★ 这个游戏的**怪 / Boss / 刷怪点全部只由「控制者」那一台客户端模拟**。
#    谁是控制者由 `[GameContext + 0x244 + 句柄类别*4]` 那张表说了算
#    （`GameContext::IsControlledByMe` = `0x491225`，102 个调用点）。
#    表的初值是客户端在 `GameContext::StartGame` 里按「开局那一刻在座的座位」
#    轮转算出来的，**客户端自己永远不会因为「有人走了」重算它**。
#
#    闯关里的怪都落在**类别 20**（§180：句柄 1,100,027 -> 类别 20），
#    控制者恒等于座位号最小的在座玩家 = 通常的房主。房主中途退出之后，
#    那一格指着一个已经空了的座位 -> 每台机器都算出「不是我」 ->
#    没有人刷怪、没有人跑怪 AI -> 关卡的闸门再也不开 ->
#    玩家看到的就是「走到屏幕最右边被屏幕挡住」。
#
#    服务端补一发这个包就能把控制权交给还在的人。
#
# ⚠ 又是一对同号反向（D028 的老规律）：**客户端**方向的 `0x0414` 是
#   `gcpRepFirstAidBox`（见上面的 `GCP_NAMES`），和这个包毫无关系。
OP_CHANGE_CONTROLLER_SLOT = 0x0414

#: 客户端处理器 `0x493780` 只改**类别 20~25** 这 6 格（`add eax, 0x294` 起，
#: 6 轮）。类别 10~15（六个玩家自己的角色）它不碰 —— 那本来就该归各人自己。
CONTROLLER_CATEGORY_FIRST = 20
CONTROLLER_SLOT_COUNT = 6

#: 调试控制通道的默认端口（`tools/gs_ctl.py` 连它）。0 = 关闭。
CONTROL_PORT = 27800
OP_REP_COUNT_DOWN = 0x0412
OP_LADDER_START_GAME = 0x0416
OP_REP_MOVE_INTO = 0x0701

# ---------------------------------------------------------------------------
# 关卡内换图（走到地图最右边 -> 传送到下一张地图），FINDINGS §111。
#
# ★ 这四个号里有三个和别的**服务端方向**包同号，别记混（D028 的老规律）：
#     0x0411  客户端方向 = gcpReqChangeToNextMap  / 服务端方向 = gspEndGame
#     0x0417  服务端方向 = gspRepChangeToNextMap  / 客户端方向 = gcpMarkQuestSuccess
#     0x0412  客户端方向 = 换图加载完成轮询       / 服务端方向 = gspRepCountDown
#
# 完整链路（每一步的地址见 FINDINGS §111）：
#     地图脚本喊 `nextmap` (0x4e65a5)
#       -> LobbyStage::ReqChangeToNextMap 0x4083e1
#          客户端**自己**从地图目录 [0x72e3d8] 查出下一张地图名，
#          发 0x0411 带着这个名字，并置 [LobbyStage+0x3f9]=1（= 鼠标沙漏）
#       ────── 服务端必须回 0x0417（原样回显地图名）──────
#       -> 处理器 0x408526 清 0x3f9/0x3fa、存地图名，调 0x47900a 真正换图
#       -> 0x47900a 起后台线程加载新地图，主线程进「加载中」循环：
#          加载完成后每 5 秒发一次 0x0412，直到 [LobbyStage+0x3fa] != 0
#       ────── 服务端必须回 0x0418（空包）──────
#       -> 0x406302 置 [LobbyStage+0x3fa]=1 -> 循环退出 -> 新地图开打
OP_REQ_CHANGE_TO_NEXT_MAP = 0x0411   # 客户端 -> 服务端
OP_REP_CHANGE_TO_NEXT_MAP = 0x0417   # 服务端 -> 客户端
OP_MAP_LOADING_DONE = 0x0412         # 客户端 -> 服务端（加载完成后的轮询）
OP_MAP_CHANGE_READY = 0x0418         # 服务端 -> 客户端（放行）

# 客户端 -> 服务端 opcode（从 RawPacket::SetType 0x5bba0a 的 109 个调用点静态提取，
# 类名由附近的 vftable 赋值对上；完整表见 .claude/FINDINGS.md §45）
GCP_NAMES = {
    0x0100: "gcpReqLogin",
    0x0104: "gcpRepPing",
    0x0105: "gcpStopPing",
    0x0106: "gcpReportHack",
    0x0109: "gcpReqDbgLogin",
    0x010a: "gcpRepDbgLoginId",
    0x0200: "gcpReqListSession",
    0x0201: "gcpReqCreateSession",
    0x0205: "gcpReqQuickJoinSession",
    0x0208: "gcpReqMaster",
    0x020b: "gcpReqMoveChannelByGameType",
    0x020d: "gcpReqUserList",
    0x020e: "gcpReqNotAcceptWhisper",
    0x0302: "gcpChangeSession",
    0x0305: "gcpSendChatMsg",
    0x030b: "gcpKickOut",
    # 「游戏准备」按钮。RawPacket（没有 gcp 类名），唯一发送点 0x558e78，
    # 只被 0x468de2（切换自己座位的准备状态）调用（FINDINGS §165）。
    0x030e: "rawToggleReady",
    0x030f: "gcpReqFirstUserResult",
    0x0310: "gcpStartTcpRelay",
    0x0311: "gcpReqQuestRecord",
    # These are constructed as RawPacket instances, so no matching gcp RTTI
    # class exists in the client image.
    0x0400: "rawCountDownFinished",
    0x0402: "rawCountGameReady",
    0x0403: "rawLoadingDone",
    0x0405: "rawLeaveGameResult",
    # 拾取请求。RawPacket（8 字节 = 座位号 + 物件实例句柄），唯一发送点
    # 0x558e9a，只被 GameContext::SendGetItem(0x493a99) 调用（FINDINGS §115）。
    0x0407: "gcpGetItem",
    # 「按 Ctrl 用道具」。RawPacket（一个 int32 = 道具槽序号，客户端恒发 0），
    # 唯一发送点 0x559205，只被 Character 的输入处理 0x516367 调用（§194）。
    0x040c: "rawUseItem",
    # 「我身上那个道具效果结束了」。RawPacket（两个 int32 = 座位 + 属性号），
    # 唯一发送点 0x558f8e，只被 Character::RemoveAttrEffect(0x50982e) 调用，
    # 而且只在那个角色是本机玩家自己时才发（§200）。
    0x040d: "rawRemoveCharAttr",
    # 玩家之间的同步数据，外面包一层送到游戏服（§149）。RawPacket，没有 gcp 类名。
    0x040e: "rawPeerData",
    0x040f: "gcpEndQuest",
    0x0410: "gcpUpdateQuestScore",
    0x0411: "gcpReqChangeToNextMap",
    # 换图加载完成后的轮询。RawPacket（空载荷），唯一发送点 0x4084e1，
    # 只被换图流程 0x47900a 的加载循环调用（FINDINGS §111）。
    0x0412: "rawMapLoadingDone",
    0x0413: "gcpRespawnCharacter",
    0x0414: "gcpRepFirstAidBox",
    0x0416: "rawLadderStartGame",
    0x0417: "gcpMarkQuestSuccess",
    0x0505: "gcpAccumulatedWeaponDamage",
    0x0606: "gcpReqComposeItem",
    0x0607: "gcpReqGiftList",
    0x0609: "gcpReqGiftAction",
    0x0705: "gcpReqMyInfo",
    0x0800: "gcpReqMoveChannel",
    0x0802: "gcpMoveChannelTest",
    0x0803: "gcpReqChannelList",
    0x0900: "gcpReqUserInfo",
    0x0901: "gcpInviteCancel",
    0x0902: "gcpReqInvite",
    0x0903: "gcpReqInviteStart",
    0x0a01: "gcpNoticeAllServer",
    0x0b01: "gcpRepGameGuard",
    0x0b02: "gspReqHackShieldCheck",
}


# ----------------------------------------------------------------------------
# 组帧 / 拆帧
# ----------------------------------------------------------------------------
def build_ctrl(payload):
    """0xFE 控制帧"""
    return bytes([MAGIC_CTRL, 0]) + struct.pack("<H", len(payload)) + payload


def _frame_result_ok(plain):
    """一帧游戏包的第一个业务 int32 是不是 0（= 成功）。

    `0x0100 gspRepLogin` 和 `0x0203 gspRepLeaveSession` 的第一个 int32 都是
    结果码，而且**只有 0 那一条**会让客户端重建 / 复位 `GameSession`
    （局号归 -1，§218）；非 0 只弹一个错误框。载荷不足 4 字节按「不是」算。
    """
    return len(plain) >= 14 and int.from_bytes(plain[10:14], "little") == 0


def build_game(opcode, payload=b""):
    """0xFF 游戏帧"""
    return (bytes([MAGIC_GAME, 0]) + struct.pack("<HHHH", len(payload), 0, 0, opcode)
            + payload)


def take_frame(buf):
    """从已解密的缓冲里取一帧。返回 (kind, opcode, payload, 消费字节数) 或 None。
    kind = 'ctrl' / 'game'。与客户端 0x5bcb19 的判定顺序保持一致。"""
    if len(buf) >= 4 and buf[0] == MAGIC_CTRL:
        n = struct.unpack_from("<H", buf, 2)[0] + 4
        if len(buf) < n:
            return None
        return ("ctrl", None, bytes(buf[4:n]), n)
    if len(buf) >= 10 and buf[0] == MAGIC_GAME:
        n = struct.unpack_from("<H", buf, 2)[0] + 10
        if len(buf) < n:
            return None
        op = struct.unpack_from("<H", buf, 8)[0]
        return ("game", op, bytes(buf[10:n]), n)
    return None


def send_all_bounded(sock, data, deadline):
    """带截止时间的 sendall（socket 自身的 timeout 对跨线程的收发是共享的，
    不能拿来当发送上限用，这里用 select 自己掐表）。

    ★ 为什么必须有它：`SimpleCipher` 是逐字节推进的流密码，调用方总是先
    `encrypt()` 再发送 —— 一旦发送中途超时 / 半发送，密码状态已经前进而
    字节没送到，**这条流从此对端再也解不开**。所以：
      * 发送必须有一个明确的截止时间，不能永远堵着（一个不收包的客户端
        会把所有往它广播的线程一起冻住 —— bug调查/4 最后一局「三个人
        全员躺着不能复活、请求石沉大海」的形态）；
      * 超时 / 失败后由调用方把连接拆掉（流已经废了，留着只会更糟）。
    """
    deadline = float(deadline)
    if not isinstance(sock, socket.socket):
        # 测试里的假 socket（只实现了 sendall）—— 没法 select，按老路走。
        sock.sendall(data)
        return
    end = time.monotonic() + deadline
    view = memoryview(data)
    while view:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise socket.timeout(
                f"send deadline {deadline:.1f}s exceeded, "
                f"{len(view)} bytes unsent")
        _, writable, _ = select.select([], [sock], [], remaining)
        if not writable:
            continue
        sent = sock.send(view)
        view = view[sent:]


# ----------------------------------------------------------------------------
# 载荷里的基本类型
# ----------------------------------------------------------------------------
class Reader:
    def __init__(self, data):
        self.d = data
        self.p = 0

    def left(self):
        return len(self.d) - self.p

    def take(self, n):
        if n < 0 or self.p + n > len(self.d):
            raise ValueError(
                f"payload truncated at offset {self.p}: need {n}, have {self.left()}"
            )
        value = self.d[self.p:self.p + n]
        self.p += n
        return value

    def i32(self):
        return struct.unpack("<i", self.take(4))[0]

    def u16(self):
        return struct.unpack("<H", self.take(2))[0]

    def wstr(self):
        n = self.u16()
        return self.take(n * 2).decode("utf-16le", "replace")


def w_i32(v):
    return struct.pack("<i", v)


def w_u8(v):
    return struct.pack("<B", 1 if v else 0)


def w_wstr(s):
    b = s.encode("utf-16le")
    return struct.pack("<H", len(s)) + b


# ----------------------------------------------------------------------------
# 服务端 -> 客户端的包体
# ----------------------------------------------------------------------------
def build_gsp_rep_login(result=0, account=None, channel_code=0, channel_index=0):
    """opcode 0x0100 —— Packet_gspRepLogin

    字段顺序直接读自 `Packet_gspRepLogin::Deserialize`（vft 0x6915d8 槽1 = 0x54c35c）：
        int32  结果码      → 0=成功 / 1 / 2 / 3=断开
        string             (+0x08)
        string             (+0x0c)
        int32 ×8           (+0x10 .. +0x2c)，其中第 1 个(+0x10) 是玩家等级、
                          第 7 个(+0x28) 是新手教程状态（>=3 已完成）
        string             (+0x30)
        int32              (+0x34)
        byte               (+0x38)
        byte               (+0x39)
    结果码 0 走 0x54f4af：置 [conn+0x898]=2 并 new(0x4a8) 建大厅对象存进全局 0x72e29c。

    ★ 8 个业务 int32 的落位全部查明了（处理器 `0x54f2cc`，FINDINGS §95）——
    它把包对象的字段逐条搬进连接和全局，一条不落：

        [0] +0x10 -> 0x72e338     玩家等级（§63）
        [1] +0x14 -> [conn+0x89c] 频道码
        [2] +0x18 -> [conn+0x8a0] 频道序号
        [3] +0x1c -> 0x72e33c     ★ 总经验
        [4] +0x20 -> 0x72e340     ★ 本级起始总经验
        [5] +0x24 -> 0x72e344     ★ 下一级所需总经验
        [6] +0x28 -> [0x72e2a4]+0x64  新手教程状态（§54）
        [7] +0x2c -> 0x72e378     语义未查，按 D019 填 0

    经验三件套和 `gspEndGame` 用的是同一组全局，所以编码规则也一样：三个都是
    **绝对累计值**，右上角数据栏自己做减法（§94）。

    ★ **金币不在这个包里** —— `0x54f2cc` 全程没碰 `0x72e330`。它只能由
    `0x0600 gspRepMoney` 下发，见 `build_rep_money`。

    等级不能留 0：「建立房间(任务)」的四个下拉框都按 `等级 >= 条目要求等级`
    过滤（`0x436833` 起），闯关关卡记录的要求等级是 1，等级 0 会让任务
    下拉框整个空掉，房也就建不成。
    """
    account = account or {}
    experience = player_experience(account)
    level_start_exp, next_level_exp = experience_bounds(experience)
    login_ints = [
        player_level(account),
        channel_code,
        channel_index,
        experience,
        level_start_exp,
        next_level_exp,
        tutorial_state(account),
        0,
    ]
    return (w_i32(result)
            + w_wstr("") + w_wstr("")
            + b"".join(w_i32(v) for v in login_ints)
            + w_wstr("")
            + w_i32(0)
            + w_u8(0) + w_u8(0))


def build_set_player_name(nickname):
    """opcode 0x0102 —— 「你自己叫什么」。载荷就是一个 wstring，没有别的字段。

    处理器是 `ServerConnection` 虚表槽 17（`0x54f23a`）：读一个 wstring，
    写进 `[0x72e2a4]+0x14` 和全局 `0x72e328`。**全镜像写 `0x72e328` 的只有
    这个函数体**（`0x5568c1` 是它的副本），而建房对话框的默认房间名
    （`0x43616f`）拿它当 `%s` —— 不发就是「与一起游戏吧」（§222）。
    """
    return w_wstr(str(nickname or ""))


def build_rep_money(money=0, experience=0, level_start_exp=0, next_level_exp=0,
                    level=1, unknown_04=0, unknown_1c=0, unknown_20=0):
    """opcode 0x0600 —— `Packet_gspRepMoney`（vft `0x69185c`）。整份玩家数据栏。

    **这是唯一能下发金币的包**（FINDINGS §95）。登录包 `gspRepLogin` 一路写到
    `0x72e378` 都没碰 `0x72e330`，所以以前每次重登金币都回到 0。

    反序列化 `0x54c7c3`（vft 槽 1）读 7 个 int32 + 1 个 u16，共 **30 字节**；
    处理器 `0x553855` 紧接着把它们逐条搬进全局，一条不落：

        +0x04  int32  -> 0x72e334   语义未查（另一种货币？）
        +0x08  int32  -> 0x72e330   ★ 金币
        +0x0c  int32  -> 0x72e33c   ★ 总经验
        +0x10  int32  -> 0x72e340   ★ 本级起始总经验
        +0x14  int32  -> 0x72e344   ★ 下一级所需总经验
        +0x18  u16    -> 0x72e338   ★ 等级（`0x5d59f1` 读 **2 字节**，movsx 扩展）
        +0x1c  int32  -> 0x72e390   语义未查（全镜像只有这一处写，没有读）
        +0x20  int32  -> 0x72e394   同上

    ★ 等级那一格是 **u16**，别当成 int32 写 —— 后面两个 int32 会整体错位 2 字节。

    这些正是 `gspEndGame` 结算时更新的同一组全局，所以经验三件套的编码规则
    一样：三个都是**绝对累计值**，UI 自己做减法（§94）。金币这里是**绝对总额**
    （直接 `mov`），和 `gspEndGame` 里那个 `+=` 的本局所得不是一回事。

    分发树 `0x54e40c` 的 `sub eax,0x507 / sub eax,0xf9 / je` 链把 0x0600 定位到
    `0x553855`；同一条链上 0x0601 -> `0x55420e`、0x0507 -> `0x553dc9`。
    """
    return (w_i32(unknown_04)
            + w_i32(money)
            + w_i32(experience)
            + w_i32(level_start_exp)
            + w_i32(next_level_exp)
            + struct.pack("<H", max(0, int(level)) & 0xFFFF)
            + w_i32(unknown_1c)
            + w_i32(unknown_20))


def build_rep_money_for(account):
    """按账号存档组一份 `gspRepMoney`。经验三件套与 `send_end_game` 同源。"""
    experience = player_experience(account)
    level_start_exp, next_level_exp = experience_bounds(experience)
    return build_rep_money(
        money=player_money(account),
        experience=experience,
        level_start_exp=level_start_exp,
        next_level_exp=next_level_exp,
        level=player_level(account),
    )


def parse_first_user_result(payload):
    """opcode 0x030f `gcpReqFirstUserResult`（客户端 -> 服务端）—— 教程进度上报。

    序列化 `0x404ee8` 只写包对象 `+0x04` 一个 int32（`0x5d591f` 写 4 字节），
    所以载荷就是 **4 字节**。

    ★ 它的唯一发送点 `0x4f22c1` 把「教程完成」这件事写死在了三条指令里：

        004f22d4  call 0x40e47f          ; edx=4 -> 切回 stage 4（大厅）
        004f22df  mov [eax+0x64], esi    ; ★ eax = [0x72e2a4]，+0x64 就是大厅
                                         ;   0x43b354 拿去 `cmp 3 / jge` 的那个
                                         ;   新手教程状态（§54）
        004f22e2  call 0x5538f2          ; ★ 把同一个 esi 发上来

    也就是说**客户端先自己记下新状态，再把它原样告诉服务端**，我们只要存下来
    就行，不用推断也不用换算。三个调用点传的值：`0x4f3aa1` 推 **5**，
    `0x4f41c9` / `0x4f41fd` 推 **4**。都 >= 3，都算完成。

    服务端方向的同号包有处理器 `0x4089c3`（大厅跳表），但它**不读包体**，
    只是清 `[LobbyStage+0x49c]`、置 `[LobbyStage+4]=2` 再加载两份 UI 资源。
    客户端自己已经切回大厅了，所以按 D028 不回显 —— 只记账。
    """
    reader = Reader(payload)
    progress = reader.i32()
    if reader.left():
        raise ValueError(
            f"first-user-result payload has {reader.left()} trailing bytes")
    return progress


#: 房间列表里每项的最后一个字段（`int32` 当 bool 用，进包对象 +0x24 的 vector）。
#: **列表 UI 里还没找到读它的地方**（§138），按 D019 填 0。
SESSION_LIST_UNKNOWN_FLAG = 0


def build_session_entry(room, player_count=None, max_players=None):
    """房间列表里的**一项**：`Session` + u16 房间号 + u8 + int32(bool)（§138）。

    `Session` 本身的线格式见 `build_session()` / §137 —— 和 `0x0303` 的载荷
    是同一份布局，只是列表里的每一项**没有**结尾那个 u16（那个是 `0x556ed1`
    比 `0x556e80` 多读的，只有 `0x0303` 才有）。
    """
    if player_count is None:
        player_count = room.player_count()
    if max_players is None:
        max_players = ROOM_SEAT_COUNT
    return (build_session(room.status, room.session_type, room.arguments,
                          title=room.title, map_name=room.map_name,
                          player_count=player_count,
                          random_map=getattr(room, "random_map", False))
            + struct.pack("<H", room.room_id & 0xFFFF)
            + struct.pack("<B", max_players & 0xFF)
            + w_i32(SESSION_LIST_UNKNOWN_FLAG))


def build_rep_list_session(rooms=(), total=None):
    """opcode 0x0200 —— 房间列表（§138）

    反序列化 `0x559009`：
        u16 房间数 n           n <= 0 就整个跳过循环（0x559023 的 jle）
        n 次 { Session + u16 房间号 + u8 + int32(bool) }
        u16                    循环之后还读一个，存到 [ebx+0x38]（-> 模型 +0x44）

    `rooms` 为空时退化成 V0.1 那 4 个字节 `00 00 00 00`（实测客户端接受）。
    `total` 不给就取 `len(rooms)`。
    """
    rooms = list(rooms)
    if total is None:
        total = len(rooms)
    out = [struct.pack("<H", len(rooms) & 0xFFFF)]
    out.extend(build_session_entry(room) for room in rooms)
    out.append(struct.pack("<H", total & 0xFFFF))
    return b"".join(out)


#: 房间列表的过滤开关（`0x0200` 请求的第 4 个字段，一个 u8，§170）。
#: 大厅左下那两个按钮：`[frame+0x11c]`「全部」发 0，`[frame+0x118]`「待机」发 1。
#: **进大厅时客户端自己发的是 1**（`0x44057a` 初始化 `[frame+0xcc]=1`）。
SESSION_LIST_FILTER_ALL = 0
SESSION_LIST_FILTER_WAITING = 1


def parse_list_session_request(payload):
    """解客户端方向的 `0x0200`（12 字节，序列化 `0x54c0e2`，§139 / §170）。

        u16   起始房间号   分页锚点（列表里 index 0 的房间号）
        u16   ?            恒 0
        u16   每页几个     客户端写死 10（`0x44056c`，大厅一页正好 10 格）
        u8    过滤开关     0 = 全部房间，1 = 只看待机房间（**默认**，§170）
        u8    ?            恒 0
        int32 游戏类型     由频道码翻译：7->2 8->6 9->5 其余->1

    第 4 个字节就是大厅左下角「全部 / 待机」那对按钮的状态（§170）——
    以前当成「未定字段」原样丢掉，于是那两个按钮怎么点都没反应。
    """
    reader = Reader(payload)
    start_room = reader.u16()
    unknown_06 = reader.u16()
    page_size = reader.u16()
    waiting_only = reader.take(1)[0]
    unknown_0b = reader.take(1)[0]
    game_type = reader.i32()
    if reader.left():
        raise ValueError(
            f"list-session payload has {reader.left()} trailing bytes")
    return {
        "start_room": start_room,
        "page_size": page_size,
        "filter": waiting_only,
        "waiting_only": waiting_only != SESSION_LIST_FILTER_ALL,
        "game_type": game_type,
        "game_type_name": GAME_TYPE_NAMES.get(game_type, "unknown"),
        "unknown": (unknown_06, unknown_0b),
    }


def build_rep_leave_session(result=0):
    """opcode 0x0203 —— 离开房间的应答（FINDINGS §101）

    客户端处理器 `0x54fffe` 只读**一个 int32**（`0x5d5984`）：

        == 0  -> `0x552943`：`LobbyStage::ResetSession` 0x4054fa，
                 再按当前 stage（5 房间 / 7 游戏）`ChangeStage(4)` 切回大厅
        != 0  -> 弹韩文错误框「퇴장 실패 / 방에서 나갈수 없습니다.」

    ★ 不回这个包，客户端就**永远留在房间里**：请求方 `0x406191` 发完就等，
    没有超时、没有本地兜底。90 秒挂机提示框正是这么堆起来的（§101）。
    """
    return w_i32(result)


def build_rep_create_session(session_id=1):
    """opcode 0x0201 —— 建房结果（最小成功应答）

    反序列化 `0x5590bb` 只读两个 int32。处理器 `0x54f6fb` 把第一个当
    结果码：非 0 走错误弹窗，0 才创建/切换到 RoomStage；第二个写入
    LobbyStage+0x1c8，作为新建 session 的 id。
    """
    return w_i32(0) + w_i32(session_id)


SESSION_TYPE_NAMES = {
    1: "normal",
    2: "quest",
    5: "ladder",
}

#: 闯关房。描述符的两个参数是 `(关卡 id, 难度)`（§68），准入校验
#: `0x4683ba` 拿它俩去查「已达成难度」（§118）。
#:
#: ★ 结算时也靠它分「合作」还是「对战」：客户端自己判的就是
#: `[LobbyStage+0x1c] == 2` —— `0x4a4b4c`（结算界面写「完成/未完成」还是
#: 「CLEAR/FAILED」）和 `0x552242`（选 `BGM-StageClear` 还是 `BGM-Victory`）
#: 两处（§161）。其余类型（0/1 对战、5 天梯、6 练习）走对战那一支。
SESSION_TYPE_QUEST = 2

#: `SessionDescriptor::Serialize`（0x557374）按房间类型写几个 int32 参数。
#: 解析客户端**发来**的描述符用这张表。
DESCRIPTOR_SENT_ARGUMENT_COUNTS = {0: 1, 1: 3, 2: 2, 3: 1, 4: 1, 5: 3, 6: 3}

#: `SessionDescriptor::Deserialize`（0x557401）按房间类型读几个 int32 参数。
#: 组服务端**下发**的描述符用这张表。
#:
#: 类型 3 / 4 两侧不对称：序列化只写 1 个参数，反序列化却读 2 个，而且读的
#: 第一个还写回 `+0x04`（也就是把 type 字段自己覆盖掉）。这是客户端自己的
#: bug，所以这两种类型不放进本表 —— 服务端不下发它们。
DESCRIPTOR_READ_ARGUMENT_COUNTS = {0: 1, 1: 3, 2: 2, 5: 3, 6: 3}


def read_session_descriptor(reader):
    """从 `reader` 里读一个客户端序列化出来的 `SessionDescriptor`。

    返回 `(类型, 参数元组)`。客户端的写入侧是 `0x557374`，所以参数个数按
    `DESCRIPTOR_SENT_ARGUMENT_COUNTS` 取。0x0201 和 0x0302 都以它结尾。
    """
    session_type = reader.i32()
    if session_type not in DESCRIPTOR_SENT_ARGUMENT_COUNTS:
        raise ValueError(f"unsupported session descriptor type {session_type}")
    arguments = tuple(
        reader.i32()
        for _ in range(DESCRIPTOR_SENT_ARGUMENT_COUNTS[session_type]))
    return session_type, arguments


def build_session_descriptor(session_type, arguments):
    """`SessionDescriptor` 的线格式：int32 类型 + 按类型定数量的 int32 参数。

    客户端的 `SessionDescriptor::Deserialize`（0x557401）先读 int32 类型，
    再按类型跳分支读参数：类型 0 读 1 个（写 +0x08）、类型 2 读 2 个
    （+0x08 / +0x0c）、类型 1 / 5 / 6 读 3 个（+0x08 / +0x0c / +0x10）。
    参数个数发错会让后面的字段整体错位。
    """
    expected = DESCRIPTOR_READ_ARGUMENT_COUNTS.get(session_type)
    if expected is None:
        raise ValueError(
            f"session descriptor type {session_type} is not safe to send")
    if len(arguments) != expected:
        raise ValueError(
            f"session descriptor type {session_type} needs {expected} "
            f"arguments, got {len(arguments)}")
    return w_i32(session_type) + b"".join(w_i32(value) for value in arguments)


def describe_room_arguments(session_type, arguments):
    """把房间描述符的参数翻成人话，只给日志用（§190）。

    `type == 1` 的三个参数分别是 `(组队, 游戏模式, 道具模式)`，客户端的三个
    取值器 `0x409df1` / `0x409e0a` / `0x409dd9` 读的就是它们。

    ★ 玩家报「道具模式下地图里找不到道具」时，第一件要看的事就是这一行里的
      `道具模式=是` 到底有没有出现 —— 没有就说明房间压根没进道具模式，
      和服务端刷不刷道具无关。
    """
    arguments = tuple(arguments)
    if int(session_type) != lobby_module.SESSION_TYPE_NORMAL:
        return ""
    parts = []
    if arguments:
        parts.append("组队战" if int(arguments[0]) == 1 else "个人战")
    if len(arguments) > 1:
        parts.append(f"游戏模式={arguments[1]}")
    parts.append("道具模式=" + ("是" if item_mode_of(session_type, arguments) else "否"))
    return "[" + " ".join(parts) + "]"


#: `Session+0x04` = 房间状态。**2 = 待机中**，其余值一律是「游戏中」。
#:
#: 判据在房间列表的渲染里，`0x43e5de` 一句 `cmp [session+4], 2`：
#: 相等推 `0x665700`（'대기중' = 待机中），不等推 `0x6656f8`（'게임중' = 游戏中）。
#:
#: ★ **它同时决定房间里建哪个 3D 场景**，这是「待机房间背景纯黑」的真凶
#: （FINDINGS §102）。`RoomStage` 构造函数 `0x466979` 在 `0x466a88` 用
#: `(状态, 描述符, 地图名)` 调游戏上下文工厂 `0x494509`，工厂开头就是：
#:
#:     00494526  cmp [ebp+8], 2 -> je 0x4948c2      ; 状态 2/5/6
#:     0049453c  cmp [ebp+8], 5 -> je 0x4948c2      ;   -> new GameContextWaitingRoom
#:     00494546  cmp [ebp+8], 6 -> je 0x4948c2      ;      = 待机房间场景
#:     0049454c  否则按 descriptor.type 建**战斗**上下文（闯关房就是
#:               GameContextQuest03），而战斗上下文要靠 stage 6 的加载流程
#:               才能把场景铺起来 —— 直接在房间里建出来就是一片黑。
#:
#: `GameContextWaitingRoom::Init`（`0x494b9f`）加载的是固定地图
#: `Maps/ReadyRoom.map`（实机解析成 `room-06`），和房间选的关卡地图无关。
#: 服务端方向的 `0x0403`（结算完回房间）在 `0x551904` 硬写 `[LobbyStage+4]=2`,
#: 所以打完一关回来的房间是对的 —— 只有建房那一路是黑的。
SESSION_STATUS_WAITING = 2

#: 「游戏中」。客户端只判「是不是 2」，别的值一律当游戏中，所以填几都行；
#: 3 是为了和 `lobby.SESSION_STATUS_PLAYING` 一致。
#: 开局时把房间切到这个状态，大厅列表就写「游戏中」，`Lobby.join` 也会用
#: `MOVE_INTO_ALREADY_PLAYING`（「此房间已开始游戏。」）挡住半路想进来的人 ——
#: 不挡的话新人会坐进一个正在打的房间，而关卡是开局那一刻按座位表加载的。
SESSION_STATUS_PLAYING = 3


def build_session(status, session_type, arguments, title="", map_name="",
                  player_count=0, random_map=False):
    """`Session` 对象的线格式（0x30 字节，反序列化 `0x556e80`，FINDINGS §137）。

        int32              -> +0x04   房间状态，见 SESSION_STATUS_WAITING
        string             -> +0x08   房间标题
        int32              -> +0x0c   ★ 房间列表里 `%s(%d/%d)` 的**第一个** %d
        string             -> +0x10   地图名
        int32（存 1 字节）  -> +0x14   ★ **随机地图开关**（§228）
        SessionDescriptor  -> +0x18   房间类型 + 参数

    ★ `+0x14` 不再是「语义未知」：客户端两处独立读它 —— 开局校验 `0x468176`
    的 `mov al,[esi+0x14] / test / jz` 和 `0x0400 gspPrepareGame` 的处理器
    `0x551605` 开头同一段。非 0 就走「按 seed 随机挑一张图」的分支。
    **不回传它，客户端点了「随机」就会当场被这一发清成 0、按钮弹回原地图。**

    两个地方用同一份布局：`0x0303`（后面多一个 u16）和 `0x0200` 房间列表的
    每一项（后面跟 u16 房间号 + u8 + int32）。未查明的字段按 D019 填 0 / 空串。

    ⚠ `(%d/%d)` 两个数**谁是当前人数、谁是上限还没实机确认**（§138）：
    这里按「+0x0c = 当前人数、列表项的 u8 = 上限」实现，一眼看反就把两处对调。
    """
    return (w_i32(status)
            + w_wstr(title)
            + w_i32(player_count)
            + w_wstr(map_name)
            + w_i32(1 if random_map else 0)
            + build_session_descriptor(session_type, arguments))


def build_update_session(session_type, arguments, title="", map_name="",
                         status=SESSION_STATUS_WAITING, player_count=0,
                         game_id=0, random_map=False):
    """opcode 0x0303 —— 把整个 `Session` 灌进客户端的 `LobbyStage`

    大厅分发器 `0x4061e2` 的跳表 `@0x406332` 索引 3 → `0x406258` →
    `0x406756`，后者直接把包体反序列化进 `LobbyStage`（`0x556ed1`）：

        int32              -> +0x04   ★ 房间状态，见 SESSION_STATUS_WAITING
        string             -> +0x08   房间标题
        int32              -> +0x0c   语义未知
        string             -> +0x10   ★ 地图名，见下面的警告
        int32              -> +0x14   ★ **随机地图开关**（读 4 字节存 1 字节，§228）
        SessionDescriptor  -> +0x18   ★ 房间类型 + 参数
        u16                -> +0x3c   ★★ **局号**（= 每座位收包队列的纪元号）

    ★★ 最后那个 u16 **不是「语义未知」** —— 反序列化 `0x556ed1` 的最后两句是

        005d596f 读一个 u16 -> movsx eax, ax -> mov [LobbyStage+0x3c], eax

    而 `[GameSession+0x3c]` 就是 `UdpPacket` 头 `+4` 那个局号（§213 / §218）：
    客户端拿它和收到的每一发同步包硬比，不等整包丢。**它是原版留给服务端的
    「直接设定」入口**（客户端自己只会 `inc` 和「复位成 -1」），
    中途进房的人就是靠这一发和全房间对上号的 —— 详见 D138。

    其余未查明的字段按 D019 一律填 0 / 空串。

    ★ `map_name` 默认且**必须**是空串。建房应答处理器 `0x54f747` 在
    `0x54f82e` 拿 `LobbyStage+0x10` 和 `L""` 比一次（`0x4040f5` 相等回 0），
    `jne 0x54fb0c` 直接跳到函数的 `ret` —— 地图名一旦非空，客户端收到
    `gspRepCreateSession` 后就什么都不做，房间面板根本不会建起来。
    地图名是后续由客户端的 `0x0302 gcpChangeSession` 提交、再由服务端下发的。
    """
    return (build_session(status, session_type, arguments, title=title,
                          map_name=map_name, player_count=player_count,
                          random_map=random_map)
            + struct.pack("<H", int(game_id) & 0xFFFF))


def parse_create_session_request(payload):
    """Decode opcode 0x0201 ``Packet_gcpReqCreateSession``.

    Its serializer at 0x43abc1 writes three strings, one int32, then a
    variable-width session descriptor through vft 0x65e09c / 0x557374.
    The descriptor's first int32 is the session type. Wire types 0, 3 and 4
    carry one argument; type 2 carries two; types 1, 5 and 6 carry three.

    The three create dialogs construct descriptor type 1 for normal rooms,
    type 2 for quest rooms, and type 5 for ladder rooms. Field meanings after
    the type differ by mode, so keep neutral names until each is proven.
    """
    reader = Reader(payload)
    text_fields = tuple(reader.wstr() for _ in range(3))
    option = reader.i32()
    session_type, arguments = read_session_descriptor(reader)
    if reader.left():
        raise ValueError(f"create-session payload has {reader.left()} trailing bytes")
    return {
        "texts": text_fields,
        "option": option,
        "session_type": session_type,
        "session_type_name": SESSION_TYPE_NAMES.get(session_type, "unknown"),
        "arguments": arguments,
    }


def parse_change_session_request(payload):
    """Decode opcode 0x0302 ``Packet_gcpChangeSession``.

    The client sends this right after it has processed a successful
    ``gspRepCreateSession``: handler 0x54f747 ends at 0x54fae9 by calling the
    send helper 0x54e5e9, which stamps type 0x0302 at 0x54e639.

    Its serializer 0x54c18c writes, in order::

        int32              (+0x04)   自由座位数（来自 0x556f40）
        string             (+0x08)   回显 LobbyStage+0x08 的房间标题
        string             (+0x0c)   模式名，查表自 0x40b6a2
        int32              (+0x10)   ★ **随机地图开关**（1 字节零扩展）
        int32              (+0x11)   ⚠ **发送点从来没赋值**，是栈垃圾，别读
        SessionDescriptor  (+0x14)   房间类型 + 参数

    两个 1 字节字段都是经 0x5d5a4c 零扩展后按 4 字节写出去的，所以线上是 int32。

    ★ **第一个是「随机地图」开关**（§228）：发送点 `0x54e5ea` 只推了 5 个参数
    （自由座位数 / 标题 / 描述符 / 地图名 / **这个字节**），最后那个来自
    `[LobbyStage+0x14]`（房间设定面板 `0x463fb7` 那一段：点的是 randomMapBtn
    就取按钮状态，否则回显 LobbyStage 里的旧值）。

    ⚠ **第二个字段没有语义**：`[obj+0x11]` 全镜像没有任何一处写它，构造函数也
    不清它 —— 线上看到的是**未初始化的栈垃圾**。实机日志里第一个只出现过 0/1，
    第二个出现过 200/250/121/248/255/72/54/18/15/224… 二十多种值
    （`bug调查/6/server_logs/game_026_27799.txt`）。**永远不要读它。**
    """
    reader = Reader(payload)
    free_slots = reader.i32()
    text_fields = (reader.wstr(), reader.wstr())
    flags = (reader.i32(), reader.i32())
    session_type, arguments = read_session_descriptor(reader)
    if reader.left():
        raise ValueError(f"change-session payload has {reader.left()} trailing bytes")
    return {
        "free_slots": free_slots,
        "texts": text_fields,
        # `flags` 原样留着只为打日志（第二格是垃圾，看看就行）。
        "flags": flags,
        "random_map": bool(flags[0]),
        "session_type": session_type,
        "session_type_name": SESSION_TYPE_NAMES.get(session_type, "unknown"),
        "arguments": arguments,
    }


#: 客户端把频道码翻译成游戏类型的表，逐分支抄自 `0x5545ec`。
#: 负数和表外的码都会被翻译成 -1，客户端认为「当前不在任何可玩频道」。
CHANNEL_CODE_GAME_TYPES = {0: 1, 1: 1, 2: 1, 3: 1, 4: 1, 6: 1, 7: 2, 9: 5, 10: 1}

#: 反向表：每种游戏类型挑一个规范频道码。普通用 0，与登录包保持一致。
GAME_TYPE_CHANNEL_CODES = {1: 0, 2: 7, 5: 9}

#: 大厅标签页索引 -> 游戏类型，抄自 `0x43b61a`。标签 3 的类型 6 没有任何
#: 频道码能映射回来，所以服务端无法把客户端移进去。
LOBBY_TAB_GAME_TYPES = {0: 1, 1: 2, 2: 5, 3: 6}

GAME_TYPE_NAMES = {1: "normal", 2: "quest", 5: "ladder", 6: "practice"}

#: `0x0202` 失败码 -> 日志里的人话。**和客户端弹出来的那句话保持一致**，
#: 这样玩家报「我看到 XXX」时日志能一秒对上（文案见 §140）。
MOVE_INTO_REASONS = {
    MOVE_INTO_ALREADY_PLAYING: "此房间已开始游戏",
    MOVE_INTO_FULL: "已超出人数限制的房间",
    MOVE_INTO_NO_SUCH_ROOM: "没有符合条件的房间",
    MOVE_INTO_BAD_PASSWORD: "密码错误",
}


def lobby_game_type(session_type):
    """房间描述符类型 -> 大厅游戏类型（房间列表按它过滤，§139）。"""
    return SESSION_TYPE_GAME_TYPES.get(int(session_type), 1)


def parse_move_channel_by_game_type(payload):
    """Decode opcode 0x020b ``gcpReqMoveChannelByGameType``.

    The sender at 0x55395a writes a single int32 holding the game type the
    lobby tab asks for. 0x43b61a maps tab 0/1/2 to game type 1/2/5, so the
    quest tab always arrives here as 2.
    """
    reader = Reader(payload)
    game_type = reader.i32()
    if reader.left():
        raise ValueError(
            f"move-channel payload has {reader.left()} trailing bytes")
    return game_type


def build_rep_move_into(ok=True, channel_code=0, channel_index=0):
    """opcode 0x0701 —— Packet_gspRepMoveInto（vft 0x6915c0）

    这是 0x020b 的**成功**应答。同号的 0x020b 服务端包只用来报失败
    （客户端 0x54fbfa 的两条分支都是韩文原版的「移动到能玩该游戏的
    频道失败」提示），所以切频道成功时绝对不能回 0x020b。

    反序列化 `0x54c891` 依次读三个 4 字节字段：
        int32 ok            非 0 才走成功路径（0x5d59de 读 4 字节再折成 bool）
        int32 channel_code  写入 [conn+0x89c]
        int32 channel_index 写入 [conn+0x8a0]
    处理器 `0x552b47` 随后把 [conn+0x898] 置 2，用 `0x5545ec` 把频道码
    翻成游戏类型，再调 `0x43b63b` 切换大厅标签页。
    """
    return w_i32(1 if ok else 0) + w_i32(channel_code) + w_i32(channel_index)


def build_quest_reached_difficulty(records):
    """opcode 0x020c —— Packet_gspQuestReachedDifficulty（vft 0x691608）

    ★ **这是「普通 / 困难难度能不能开局」的唯一开关**（FINDINGS §118）。

    反序列化 `0x54cf4a → 0x555315` 读的是一个 `vector<pair<int32,int32>>`：

        int32                        条目数（`0x5d5984`）
        条目数 × { int32 关卡 id, int32 已达成难度 }   （`0x5558f2` 读两个 int32）

    处理器 `0x5539c2` 先 `0x401312` 把全局 map `[0x72e35c]` **清空**，
    再逐条 `0x47197a` 插进去 —— 所以这个包是**全量快照**，重发一次就是
    整张表的新版本，不需要考虑增量。

    客户端开局准入校验 `0x468176` 在闯关分支 `0x4683ba` 里：

        reached = map[关卡 id]              ; 查不到就是 0
        allowed = min(reached + 1, 4)
        if 房间选的难度 > allowed:  错误码 5
            -> 弹 0x66a758「플레이할 수 없는 난이도 입니다. 난이도를 낮춰주세요」
               （中文本地化 =「无法进行的难度，请降低难度」）

    也就是说服务端一直不发这个包 == map 恒空 == `allowed` 恒为 1 ==
    每一关都只有「简单」能开。
    """
    items = sorted((int(quest_id), int(difficulty))
                   for quest_id, difficulty in dict(records).items())
    payload = [w_i32(len(items))]
    for quest_id, difficulty in items:
        payload.append(w_i32(quest_id))
        payload.append(w_i32(difficulty))
    return b"".join(payload)


#: 房间座位数。`LobbyStage+0x40` 起，每项 0x3c 字节，固定 6 项
#: —— `0x556eec` 的循环写死 `cmp [ebp-4], 6`，`0x404d42` 的取值器也拒收
#: 越界下标（`cmp ecx,6 / jge`）。
ROOM_SEAT_COUNT = 6


def build_session_slot(occupied=False, nickname="", team=0,
                       character_id=0, item_ids=(), unknown_28=0,
                       unknown_2c=0, level=0, ready=False,
                       unknown_12=0, unknown_text="", unknown_34=False,
                       closed=False):
    """房间里一个座位（`SessionSlot`）的线格式。

    反序列化在 `0x556d9d`（`this` = 座位指针，`eax` = 流）。它先读占用标记，
    只有占用时才继续读后面的字段，最后**无论占用与否**都再读一个标记：

        int32(bool) 占用       -> +0x00   0x5d5956 读 4 字节折成 bool
        占用时:
            string  昵称       -> +0x04   0x5d5b3a = u16 字符数 + UTF-16LE
            u8      ★ 队伍     -> +0x08   ★ 0x5d5942 读**1 字节**，不是 4
            int32   角色 id    -> +0x0c   0x0301 的 action 4 拿它去 0x557128 查名字
            int32×n + int32 0  -> +0x1c   0 结尾的 int32 列表（0x556de1 起的循环）
            int32   ?          -> +0x28
            u16     ?          -> +0x2c
            u16     ★ 等级     -> +0x10
            int32(bool) ★ 准备 -> +0x2e
            u16     ?          -> +0x12
            string  ?          -> +0x30
            int32(bool) ?      -> +0x34
        不占用时:
            （无字段，客户端调 0x556c55 把整个座位清零）
        int32(bool) 关闭       -> +0x01   0x556f40 数 `+0x41 == 0` 的座位当空位

    ★ **`team`（+0x08）和 `ready`（+0x2e）在 V0.2 会话 10 查明**（§165）。
    在这之前它们叫 `unknown_u8` / `unknown_2e`，服务端一直发 0，
    于是「变更队伍」和「游戏准备」两个按钮在联机时都不工作。

    `team`（`LobbyStage + 座位*0x3c + 0x48`，取值器 `0x40462c`）：

        - 房间里角色模型站哪边：`0x405e5d` 把它当分组号交给 `0x473cb2`；
        - 战斗里的友军伤害：`0x4fedfc` / `0x4ffec3` 比两边的队伍号，
          相同就**不结算伤害**；
        - **只有组队模式才读**：三处读它之前都先 `0x409df1(描述符) == 1`
          （描述符 type==1 时返回 `arguments[0]`，type==5 恒为 1）。
        - 取值 1 / 2；0 = 没分队。客户端的「变更队伍」只会在 1 和 2 之间切。

    `ready`（`+0x2e`，客户端按 1 字节读写）：

        - 房间里玩家名字旁边那行「준비중 / 准备中」：`0x46c330`；
        - 房主能不能按「开始」：`0x4696cd` 数「已准备的人」，
          **房主自己那格无条件算已准备**（`0x4696f8`）；
        - 非房主的按钮在自己已准备时变灰（`0x46b5f9`）。

    `level`（+0x10）是**里程碑 C 的关键字段**：房间里按「F5 游戏开始」时，
    `0x468242` 起的检查拿**房主座位**的这个 u16 去和关卡要求等级比：

        00468242  mov edi, [esi+0x34]      ; 房主座位号
        00468256  call 0x404d42            ; -> 座位指针
        00468277  movzx ecx, word [edi+0x10]   ; ★ 座位里的等级
        0046827b  cmp ecx, eax             ; eax = 0x464848() = 关卡要求等级
        0046827d  jge 0x468316             ; 够 -> 放行
                                           ; 不够 -> 弹 0x66a7fc
                                           ;   '레벨이 낮아서 퀘스트를 선택할 수 없습니다'
                                           ;   （中文本地化 = 「等级太低，无法选择任务。」）

    它和 `gspRepLogin` 下发、存在全局 `0x72e338` 的那个等级**是两回事**：
    后者只管大厅 UI 和下拉框过滤，这里只认座位里的值。等级 30 照样弹框
    就是因为服务端从来没发过任何房间成员包（FINDINGS §75）。

    未查明语义的字段按 D019 一律填 0 / 空串。
    """
    if not occupied:
        return w_i32(0) + w_i32(1 if closed else 0)
    # ★ 队伍号在这里**夹到 0..2**（bug调查/8_2 §212）。客户端的队伍记录数组
    #   只有两格，`vf34`（`0x55c696`）按 `this + 40*(队伍号-1)` 写、没有上界
    #   检查，发出去 >= 3 就会踩掉别人的每座位战绩（死亡次数被越写越大 ->
    #   剩余生命归零 -> 活着进观战 / 死了永不重生）。线格式这一处是所有
    #   `0x0300` / `0x0301` 的必经之路，闸设在这里就漏不掉。
    team = int(team)
    if not 0 <= team <= 2:
        team = 0
    return (w_i32(1)
            + w_wstr(nickname)
            + struct.pack("<B", team & 0xFF)
            + w_i32(character_id)
            + b"".join(w_i32(v) for v in item_ids) + w_i32(0)
            + w_i32(unknown_28)
            + struct.pack("<H", unknown_2c)
            + struct.pack("<H", level)
            + w_i32(1 if ready else 0)
            + struct.pack("<H", unknown_12)
            + w_wstr(unknown_text)
            + w_i32(1 if unknown_34 else 0)
            + w_i32(1 if closed else 0))


def build_session_members(host_seat=0, seats=()):
    """opcode 0x0300 —— 整个房间的座位快照。

    大厅分发器 `0x4061e2` 的跳表 `@0x406332` 索引 0 → `0x406232` →
    `0x40637a`，后者把包体交给 `0x556eec`（`this` = `LobbyStage`）：

        int32   房主座位号   -> LobbyStage+0x34
        int32   ?            -> LobbyStage+0x38
        SessionSlot × 6      -> LobbyStage+0x40 起，每项 0x3c 字节

    处理器随后置 `[LobbyStage+0x1c4] = 1`、把「我」的座位 IP 写成
    `0x0100007f`（127.0.0.1）、调 `0x405a74` 刷新房间 UI。结尾那段拼
    `"Slot %d : %s, open/closed"` 的循环是死调试代码，字符串建完就析构掉了。

    **必须排在 `0x0201` 建房应答之后**：`0x54f747` 在 `0x54f815` 会把
    `[LobbyStage+0x4c]`（= 座位 0 的 +0x0c，角色 id）清零，先发就被冲掉。
    """
    if len(seats) != ROOM_SEAT_COUNT:
        raise ValueError(
            f"room snapshot needs exactly {ROOM_SEAT_COUNT} seats, "
            f"got {len(seats)}")
    if not 0 <= host_seat < ROOM_SEAT_COUNT:
        raise ValueError(f"host seat {host_seat} is out of range")
    return (w_i32(host_seat) + w_i32(0)
            + b"".join(build_session_slot(**seat) for seat in seats))


#: 装备清单开头那 12 个字节 = 3 个 int32 位掩码（「哪几个装备槽被占了」）。
#: 客户端只在**自己往清单里加/删物品**时才碰它们（`0x5583ab` 或进 `0x558423`
#: 出），下发全 0 就等于「一个槽都没占」，正是我们要的初始状态。
EQUIPPED_SLOT_MASK_COUNT = 3


def build_slot_equipped_list(seat_index, item_ids=(), slot_masks=(0, 0, 0)):
    """opcode 0x030b —— 某个座位的背包/装备物品清单。

    ★ **这是「房间『人物选择』里能出现几个头像」的唯一开关**（FINDINGS §119）。

    大厅分发器 `0x4061e2` 的跳表 `@0x406332` 索引 0x0b → `0x40628a` →
    `0x406ea1`。处理器把包体反序列化成一个临时清单对象，再拿它**整体替换**
    `[LobbyStage + 座位*4 + 0x250]`（先 `vf+8` 删旧的，再 `0x5f399e` 分配
    0x50 字节新的、`0x414d95` 拷进去），最后 `0x406f42` 把清单套到该座位的
    角色对象上。

    线格式（`Packet_gspSlotEquippedList::Deserialize 0x404f1e`
    → 清单自己的 `0x404c3f`）：

        int32       座位号            -> 包 +0x04（处理器拿它算 0x250 的下标）
        12 字节     槽位掩码 ×3       -> 清单 +0x0c（`0x5d59c1` 原样读 12 字节）
        int32       物品数            -> `0x5d5984`
        物品数 × int32  物品 id       -> 清单 +0x18 的 `vector<int32>`

    角色选择怎么用它（`CharacterChanger` 建按钮时，`0x4f586c`）：

        0x40713a(LobbyStage, 0)   数出按钮个数
            for id in 100..110:  0x4070c2(id) 为真就 +1
            return 计数 + 3                      ★ 0/1/2 三个基础角色白送
        0x4070c2(id) = 我的座位(`+0x1cc`)已占用
                       且 `[LobbyStage + 我的座位*4 + 0x250]` 非空
                       且 0x55853c(清单, id)
        0x55853c(清单, id):  id < 3 -> true
                             否则在 `vector<int32>` 里找落在
                             `[(id+1)*1e6, (id+2)*1e6)` 区间的物品

    所以「只有 3 个角色可选」不是等级不够也不是资源缺失 —— 是这一发从来
    没发过，清单恒空，11 个商城角色一个都过不了持有判定。

    ⚠ 按钮是在**房间 UI 构造时**一次性建出来的（`0x40bd3b → 0x4776a3 →
    0x4f586c`），后面再发这个包也不会重建。而 `ChangeStage`（`0x40e47f`）
    只是记下工厂函数、下一帧才真的建 —— 所以只要和 `0x0300` 一起在建房应答
    之后发出去，就赶得上（座位的「已占用」标记也正是 `0x0300` 写的）。
    """
    if not 0 <= seat_index < ROOM_SEAT_COUNT:
        raise ValueError(f"seat {seat_index} is out of range")
    masks = tuple(slot_masks)
    if len(masks) != EQUIPPED_SLOT_MASK_COUNT:
        raise ValueError(
            f"equipped list needs exactly {EQUIPPED_SLOT_MASK_COUNT} slot "
            f"masks, got {len(masks)}")
    items = [int(item_id) for item_id in item_ids]
    return (w_i32(seat_index)
            + b"".join(w_i32(mask) for mask in masks)
            + w_i32(len(items))
            + b"".join(w_i32(item_id) for item_id in items))


#: `0x0301` 的 action 码 —— 客户端 `0x4064f7` 起按它分支，每个码的副作用差别很大。
#: 0 是唯一会**建**座位角色对象的（`0x405e1c` + `0x406f42`）。
SEAT_ACTION_JOIN = 0
#: 有人离开 / 被踢：**必须发 1（或 2），不能发 3**。
#: 只有 1/2 会走到 `0x406676 → 0x405f8f` —— 那是唯一会把座位的 3D 角色对象
#: **销毁**并把 `[LobbyStage+0x1d0+i*4]` 清 0 的分支。3 走的是 `0x406628`
#: （「把模型同步到座位数据」，只重建不销毁），发 3 的话玩家列表里的名字没了、
#: 上面蓝天白云那块的模型却还杵着（§147，用户实机报的缺陷）。
SEAT_ACTION_LEAVE = 1
#: 3 = 「按座位数据重建模型」，不是「离开」。action 4（换角色）处理完消息之后
#: 就是落到这条路上重建模型的。服务端目前没有单独发 3 的场景，留着是为了
#: 让「3 不销毁模型」这条结论有个名字，别再有人望文生义地拿它当离开用。
SEAT_ACTION_RESYNC = 3
#: 换角色。客户端点房间右下角「人物选择」的头像时先把整个座位用
#: **客户端方向的 `0x0301`** 报上来，然后**什么都不做地等**；只有服务端把这个
#: action 广播回来，`0x406520` 才播「%s님이 %s 캐릭터로 선택되었습니다.」
#: 并走到 `0x406628` 重建座位的角色对象 —— 中下那个 3D 预览就是这时才换的。
#: 不回这一发，点头像就是完全没反应（FINDINGS §103）。
SEAT_ACTION_CHANGE_CHARACTER = 4


def build_session_member_update(seat_index, action=SEAT_ACTION_JOIN, **seat):
    """opcode 0x0301 —— 单个座位的变更事件。

    大厅跳表 `@0x406332` 索引 1 → `0x40623f` → **`0x40648d`**，载荷：

        u8      action     ★ 0x5d5942 读 1 字节（不是 4）
        int32   座位号     客户端校验 0 <= n < 6，越界直接丢包
        SessionSlot        与 0x0300 里的每一项同格式，见 build_session_slot

    action 分支（`0x4064f7` 起）。★ **所有分支都先把包里的 SessionSlot 反序列化
    进座位**（`0x4064d6 → 0x556d9d`），action 只决定「模型怎么动」：

        0 → 0x406691  进房：清 IP/端口，**`0x405e1c` 建座位的角色对象**、
                      `0x406f42`、`0x4089fa`，最后 `0x405a74` 刷 UI
        1/2 → 0x406676 清 IP/端口 + `0x405f8f`（**销毁**角色对象并把
                      `[LobbyStage+0x1d0+i*4]` 清 0）+ 刷 UI
                      —— **离开 / 被踢走这条**，两个码在客户端里完全等价
        3 → 0x406628  按座位数据**重建**模型（占用且模型在 → `0x405fba`
                      对齐角色 id；不占用 → 什么都不做地返回）。
                      ★ 它**不销毁**模型，拿它当「离开」用会留下鬼影（§147）
        4 → 0x406520  换角色：用 `seat+0x0c` 查角色名，播
                      `'%s님이 %s 캐릭터로 선택되었습니다.'`，然后落到 0x406628

    ★ action 0 会把 `seat[+0x14]`（对端 IP）和 `seat[+0x18]`（端口）清 0。
    `0x0300` 的处理器会把「我」的座位 IP 写成 `0x0100007f`，所以两个包同时发时
    **`0x0301` 要排在 `0x0300` 前面**，否则 IP 又被清掉。
    """
    if not 0 <= seat_index < ROOM_SEAT_COUNT:
        raise ValueError(f"seat index {seat_index} is out of range")
    if action not in (SEAT_ACTION_JOIN, SEAT_ACTION_LEAVE, 2,
                      SEAT_ACTION_RESYNC, SEAT_ACTION_CHANGE_CHARACTER):
        raise ValueError(
            f"seat action {action} is not one of the codes the client "
            f"dispatches at 0x4064f7 (0/1/2/3/4); it would be dropped")
    return (struct.pack("<B", action & 0xFF)
            + w_i32(seat_index)
            + build_session_slot(**seat))


def parse_report_hack(payload):
    """解 `0x0106 gcpReportHack` 的载荷 -> 一行正文。

    形状是**一个 wstring**（`0x5d5b3a` = u16 字符数 + UTF-16LE）。
    解不动就退回 hexdump 的可打印形式 —— 这个包**纯粹是给人看的**，
    绝不能因为格式没猜准就抛异常把连接带崩（它在战斗中随时可能来）。
    """
    try:
        reader = Reader(payload)
        text = reader.wstr()
        if reader.left():
            text += f"（另有 {reader.left()} 字节未解）"
        return text
    except (ValueError, struct.error, UnicodeDecodeError):
        printable = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in payload)
        return f"<解不出 wstring，{len(payload)} 字节: {printable[:120]}>"


def parse_move_into_request(payload):
    """解客户端方向的 `0x0202 gcpReqMoveInto`（序列化 `0x558d9d`，§140）。

        int32   房间号
        string  密码
        int32（由 1 字节零扩展）  ?   —— 输过密码那条路（`0x5501d4`）填 1

    双击房间列表和「输密码后重试」走的是同一个包，服务端不需要区分。
    """
    reader = Reader(payload)
    room_id = reader.i32()
    password = reader.wstr()
    flag = reader.i32()
    if reader.left():
        raise ValueError(f"move-into payload has {reader.left()} trailing bytes")
    return {"room_id": room_id, "password": password, "flag": flag}


def build_rep_move_into_session(result=MOVE_INTO_OK, room_id=0, seat_index=0):
    """opcode 0x0202 —— 加入房间的结果（反序列化 `0x5590d5` = 4 个 int32）。

    处理器 `0x54fd07` 按第一个 int32 分支（§140）：

        0 -> 成功：房间号写 `LobbyStage+0x1c8`、座位号经 `0x405a1f` 写 +0x1cc，
             座位标成已占用、角色 id 清 0，然后 `ChangeStage(5)` 进房间
        1 -> 「进入失败 / 此房间已开始游戏。」
        2 -> 「进入失败 / 已超出人数限制的房间。」
        3 -> 「进入失败 / 没有符合条件的房间。」
        4 -> 「进入失败 / 密码错误。」
        5 -> 先清座位 0/1/2 的 +0x14 再走成功分支（疑似观战席）
        其它 -> 「进入失败 / 无法进入房间。」

    ★ 失败时**必须回对码**：全都回 3 的话密码错也会被说成「没有符合条件的
    房间」，那是在撒谎，排查时会把人往错误方向带（同 D071 的道理）。

    第 4 个 int32 成功分支没读，按 D019 填 0。
    """
    return (w_i32(result) + w_i32(room_id) + w_i32(seat_index) + w_i32(0))


def parse_quick_join_request(payload):
    """解客户端方向的 `0x0205 gcpReqQuickJoinSession`。

    序列化 `0x404f59` 只有三条指令 —— `add ecx,4` 之后直接 `jmp` 到内嵌
    `SessionDescriptor` 的 Serialize，所以**载荷就是一个描述符**，
    没有别的字段。返回 `(类型, 参数元组)`。
    """
    reader = Reader(payload)
    session_type, arguments = read_session_descriptor(reader)
    if reader.left():
        raise ValueError(f"quick-join payload has {reader.left()} trailing bytes")
    return session_type, arguments


#: `gspReceiveChatMsg` 的座位号字段。房间外（大厅/系统消息）没有座位，
#: 发一个越界值让客户端的 `0x4045f9` 判定落空即可 —— 它只用这个号去查
#: 「点名字」用的昵称，查不到就跳过，不影响正文显示（§141）。
CHAT_NO_SEAT = 0xFFFF


def parse_chat_message(payload):
    """解客户端方向的 `0x0305 gcpSendChatMsg`（序列化 `0x54c26c`，§141）。

        u8      聊天类型
        string  正文
    """
    reader = Reader(payload)
    chat_type = reader.take(1)[0]
    text = reader.wstr()
    if reader.left():
        raise ValueError(f"chat payload has {reader.left()} trailing bytes")
    return chat_type, text


def build_receive_chat(text, sender="", seat_index=CHAT_NO_SEAT, chat_type=0):
    """opcode 0x0305 —— `gspReceiveChatMsg`（序列化 `0x404e3b`，§141）。

        u16     发言者座位号   0..5；客户端拿它去座位里查昵称（点名字用）
        string  发言者显示名   ★ 非空 -> 渲染成 '%s : %s'；空 -> 只显示正文
        string  正文
        int32   聊天类型       传给 0x40605d 决定颜色

    所以**系统提示把 `sender` 留空**就能得到一行没有「XXX : 」前缀的白话。
    """
    return (struct.pack("<H", seat_index & 0xFFFF)
            + w_wstr(sender)
            + w_wstr(text)
            + w_i32(chat_type))


def parse_kick_out_request(payload):
    """解客户端方向的 `0x030b gcpKickOut`（vft `0x66ae20`，序列化 `0x46ba38`）。

        int32   被踢的座位号   来自 `[RoomStage+0x170]`
        int32（由 1 字节零扩展）  `[LobbyStage+0x3da]`（观战标记）

    ⚠ 同号反向：服务端方向的 `0x030b` 是座位物品清单（§119）。
    """
    reader = Reader(payload)
    seat_index = reader.i32()
    flag = reader.i32()
    if reader.left():
        raise ValueError(f"kick-out payload has {reader.left()} trailing bytes")
    return seat_index, flag


def parse_session_slot(reader):
    """读一个客户端序列化出来的 `SessionSlot`（`0x556ccc`），字段见 build_session_slot。

    收发两侧是同一份布局，唯一要小心的还是那两个非 4 字节的原语：
    `+0x08`（队伍）是 1 字节、`+0x2c` / `+0x10`（等级）/ `+0x12` 是 u16。

    ★ 客户端方向的 `0x0301` 有**两种**用途，靠这里解出来的字段差异区分：
    角色 id 变了 = 换角色，`team` 变了 = 变更队伍（§165）。
    """
    occupied = bool(reader.i32())
    slot = {"occupied": occupied}
    if occupied:
        slot["nickname"] = reader.wstr()
        slot["team"] = reader.take(1)[0]
        slot["character_id"] = reader.i32()
        item_ids = []
        while True:
            value = reader.i32()
            if value == 0:
                break
            item_ids.append(value)
        slot["item_ids"] = tuple(item_ids)
        slot["unknown_28"] = reader.i32()
        slot["unknown_2c"] = reader.u16()
        slot["level"] = reader.u16()
        slot["ready"] = bool(reader.i32())
        slot["unknown_12"] = reader.u16()
        slot["unknown_text"] = reader.wstr()
        slot["unknown_34"] = bool(reader.i32())
    slot["closed"] = bool(reader.i32())
    return slot


def parse_seat_change_request(payload):
    """解客户端方向的 `0x0301`（房间里改自己那个座位的某个字段时发的）。

    序列化点 `0x558dcb` 只写两样东西：

        push 0x301               ; opcode
        call 0x5bba0a
        push [esi]  / 0x5d591f   ; int32 座位号
        esi = [esi+4] / 0x556ccc ; SessionSlot（与 0x0300 里的每一项同格式）

    **注意它没有 action 字节** —— 服务端方向的同号包才有（`0x40648d` 先读
    1 字节 action 再读座位号）。实机 59 字节载荷逐字段解对：
    `seat=0, occupied=1, nickname='testuser', character_id=0/1/2, level=1`，
    连点三个头像时只有 `character_id` 在变（FINDINGS §103）。

    ★ **这个包有两个来源**（§165），载荷形状完全一样，服务端只能靠
    「哪个字段变了」区分：

        换角色    `0x467050` / `0x4692d0` 起 —— `character_id` 变
        变更队伍  `0x469f95` / `0x46deaa` —— `team`（+0x08）在 1 / 2 之间翻

    两条路客户端都是**改一份座位的副本就发出来，自己一动不动**，
    等服务端广播回来才真的生效。回错 action 的后果是用户报的那两条：
    变更队伍会播一行换角色的韩文提示，而队伍其实没变。
    """
    reader = Reader(payload)
    seat_index = reader.i32()
    slot = parse_session_slot(reader)
    if reader.left():
        raise ValueError(f"seat change payload has {reader.left()} trailing bytes")
    if not 0 <= seat_index < ROOM_SEAT_COUNT:
        raise ValueError(f"seat index {seat_index} is out of range")
    return seat_index, slot


def build_rep_quest_record_in_pvp(records=None):
    """opcode 0x0311 —— 房间里**每个座位**一项的任务/PvP 记录。

    大厅分发器 `0x4061e2` 把 0x0311 交给 `0x408a1c`。后者构造
    `Packet_gspRepQuestRecordInPvp`（vft 0x65e14c），其反序列化函数
    `0x404fb9` 经 `0x408fc9` 读取：

        int32 n
        int32 records[n]

    ★ 六项是**六个座位**，不是「六种游戏类型」（V0.2 会话 08 查明，§161）——
    处理器就是一个 `for (i = 0; i < 6; i++)`：

        if (!seatOccupied(i)) continue;              ; 0x4045f9
        [LobbyStage + i*4 + 0x1ac] = records[i];     ; 0x408a66
        0x4089fa(LobbyStage)                          ; 拿它去调座位对象的 0x50b6a1

    全镜像里 `[LobbyStage + 座位*4 + 0x1ac]` 只有这一处写、只有 `0x4089fa`
    一处读，而后者只是把值交给座位对象 —— 纯表现层（多半是头顶的段位标记）。
    **具体是什么数仍未查明**，所以按 D019 继续全填 0：回空表客户端照常工作。
    """
    if records is None:
        records = (0, 0, 0, 0, 0, 0)
    if len(records) != 6:
        raise ValueError("quest record response requires exactly 6 entries")
    return w_i32(len(records)) + b"".join(w_i32(v) for v in records)


def build_trigger_count_game(result=0):
    """opcode 0x0401 -- Packet_gspTriggerCountGame.

    The lobby dispatcher at 0x4061e2 constructs this class in handler
    0x4074de. Its payload is one int32. Values 2 and 3 select ladder
    maintenance errors; 0 enters the normal start-confirmation path.
    """
    return w_i32(result)


def build_rep_count_down(state=0):
    """opcode 0x0412 -- Packet_gspRepCountDown.

    Handler 0x493755 reads one int32 and passes it to 0x468e78: value 0
    starts the roughly six-second client countdown, while 1 stops it.
    """
    return w_i32(state)


def build_prepare_game(seed=0):
    """opcode 0x0400 -- Packet_gspPrepareGame.

    The 0x0400 branch of dispatcher 0x54e036 enters handler 0x551605.
    Its sole int32 is the shared random seed used by map/spawn selection,
    so the single-player fake backend can safely use deterministic seed 0.
    """
    return w_i32(seed)


#: `gspEndGame` 的业务字段个数（`pkt+0x0c` .. `pkt+0x38`）。
#: 反序列化 `0x54cea3` 一共读 14 个 4 字节字段，前两个是座位号和成功标志。
END_GAME_VALUE_COUNT = 12

#: 12 个业务值里已查明语义的下标（FINDINGS §94），其余按 D019 填 0。
END_GAME_EXPERIENCE = 1        # pkt+0x10 -> [0x72e33c]  玩家的总经验值
END_GAME_NEXT_LEVEL_EXP = 2    # pkt+0x14 -> [0x72e344]  升到下一级所需的总经验
END_GAME_LEVEL_START_EXP = 3   # pkt+0x18 -> [0x72e340]  当前等级的起始总经验
END_GAME_MONEY_GAINED = 4      # pkt+0x1c -> [0x72e330]  ★ 金币，客户端是 `+=`

#: 结算界面「分数 / 生命」那一行的**分数**（会话 18，§116）。
#:
#: `0x0411` 的处理器 `0x551804` 把包里的字段搬成一张 13 个 dword 的结算表
#: （`0x5518af call 0x4a4096` -> `[GameContextQuest + 座位*0x34 + 0x3ec]`），
#: 结算界面画玩家槽的 `0x4a4af5` 把这张表整个搬到栈上，然后在 `0x4a4e40`：
#:
#:     ecx = 表[7] + 表[6] + 表[5]        ; 三格相加
#:     "%d / %d" % (ecx * 系数, 剩余生命)  ; 0x668114 @ (0x12d, 0xa1)
#:
#: 表[5]/表[6]/表[7] 分别来自 `pkt+0x20` / `pkt+0x24` / `pkt+0x28`
#: = 业务值下标 5 / 6 / 7。**界面显示的是三者之和**，所以原版多半是
#: 「击杀分 / 时间分 / 收集分」之类的拆分；单机把本局总分全放进第一格就够了。
END_GAME_SCORE_PARTS = (5, 6, 7)


def build_end_game_values(experience=0, next_level_exp=0, level_start_exp=0,
                          money_gained=0, score=0):
    """按 §94 / §116 的语义组 `gspEndGame` 的 12 个业务值。

    右上角玩家数据栏那三个显示全是客户端自己做的减法：

        当前经验 = 总经验     - 本级起点
        升级所需 = 下一级所需 - 本级起点
        进度条   = 前者 / 后者

    会话 10 发 `endgame-probe`（101..112）时画面显示 `经验值: -2/-1`、`200%`、
    `金币: 105`，与 `102-104` / `103-104` / `105` 逐项吻合。所以这三个必须是
    **绝对累计值** —— 填「本级内的差值」会让进度条错乱。

    ★ `money_gained` 在客户端是 `[0x72e330] +=`（`0x5518c0`），
    发的是**本局所得**，不是账号总额。

    `score` 落进 `END_GAME_SCORE_PARTS` 的第一格 = 结算界面「分数 / 生命」
    那一行的分数（另外两格保持 0，界面显示的是三格之和）。
    """
    values = [0] * END_GAME_VALUE_COUNT
    values[END_GAME_EXPERIENCE] = int(experience)
    values[END_GAME_NEXT_LEVEL_EXP] = int(next_level_exp)
    values[END_GAME_LEVEL_START_EXP] = int(level_start_exp)
    values[END_GAME_MONEY_GAINED] = int(money_gained)
    values[END_GAME_SCORE_PARTS[0]] = int(score)
    return values


def build_end_game(seat_id=0, success=True, values=None):
    """opcode 0x0411 —— `Packet_gspEndGame`（vft `0x691874`）。关卡结束 + 结算。

    反序列化 `0x54cea3` 读 **14 个 4 字节字段**（全部走 `0x5d59ff`/`0x5d59de`）：

        int32   -> +0x04   ★ 座位号
        bool32  -> +0x08   （`0x5d59de` 读 4 字节折成 bool）成功/失败
        int32   -> +0x0c .. +0x38    12 个业务值

    处理器 `0x551804`（分发树 `0x54e39d` → 这里）：

        0x55189f  push 0xd / rep movsd        ; 把 +0x0c 起的 13 个 dword 搬成一个结构
        0x5518af  call 0x4a4096               ; eax = +0x04(座位号)，交给结算 UI
        0x5518b4  call 0x409f7d / cmp ebx,eax ; ★ 座位号 == 我的座位才更新下面四个全局
        0x5518c0  [0x72e330] += pkt+0x1c      ; ★ 累加（疑似经验或金钱）
        0x5518c9  [0x72e33c]  = pkt+0x10
        0x5518d1  [0x72e340]  = pkt+0x18
        0x5518d9  [0x72e344]  = pkt+0x14
        0x5518e4  call 0x4913fc               ; 结算界面
        0x5518ef  call 0x4087f0

    12 个业务值的语义还没查（只知道 `+0x1c` 是累加的、`+0x10/+0x14/+0x18` 各自落位），
    按 D019 一律填 0。**注意 `+0x1c` 是 `+=`**，所以填 0 最安全 —— 填别的值会把
    玩家数据越加越多。

    `seat_id` 必须等于客户端认为的自己的座位号（`[LobbyStage+0x1cc]`，建房时是 0），
    否则那四个全局不会更新；但结算界面 `0x4913fc` 无论如何都会弹
    （`0x55184e` 的 `je 0x5518de` 只跳过搬运那一段）。
    """
    values = list(values if values is not None else [0] * END_GAME_VALUE_COUNT)
    if len(values) != END_GAME_VALUE_COUNT:
        raise ValueError(
            f"gspEndGame needs exactly {END_GAME_VALUE_COUNT} trailing values, "
            f"got {len(values)}")
    return (w_i32(seat_id)
            + w_i32(1 if success else 0)
            + b"".join(w_i32(v) for v in values))


def end_game_order(my_seat, seats):
    """结算时 `0x0411` 发给某一个人的顺序：**自己那份在最前**，其余升序。

    为什么自己必须是第一发（§178）：`0x551804` 处理**每一发** `0x0411` 都会走到
    `0x5518e4 call 0x4913fc`（弹结算界面），而那个函数第一句就是
    `cmp byte [esi+4], 0 / jne 尾部` —— 真正把界面建出来的是**第一发**。
    只有座位号 == 自己时才更新的那四个全局（右上角数据栏）在 `0x4913fc`
    **之前**写，所以「自己那份排第一」= 界面弹出来的那一刻，
    自己的经验/金币已经是新值，和 V0.1 实机验过的单人时序逐字一致。

    队友那几行是界面弹出来之后补进去的，照样显示得出来：结算界面画每一行时
    （`0x4a4b42`）是**当场**从 `[GameContextQuest + 座位*0x34 + 0x3ec]`
    拷 13 个 dword，不是弹窗时快照的。
    """
    return ([my_seat] if my_seat in seats else []) + \
           sorted(s for s in seats if s != my_seat)


#: `gspRepGameResult` 里座位号之后的业务字段个数（反序列化 `0x54c6b4`）。
GAME_RESULT_VALUE_COUNT = 12

#: 尾部数组的最小长度。`0x5521d2` 的循环**无条件**读 6 个 dword 搬到
#: `[GameContext+0x184]`，数组更短就会读到 vector 边界外，为空更是直接读 NULL。
GAME_RESULT_TAIL_COUNT = 6

#: 尾部数组第 i 项填这个值 = 「第 i 号座位通关了」。
#:
#: ★★ 这就是结算界面那个「未完成」标签的开关（会话 17，§112）。
#: 尾部数组落进 `[GameContext + 座位*4 + 0x184]`，读它的是
#: `GameContext::vf_10c`（`0x48c9ff`，整个函数只有
#: `mov eax,[ecx+eax*4+0x184] / ret 4`）。结算界面画玩家槽的
#: `0x4a4af5` 在 `0x4a4ba9` 处判
#:
#:     vf_10c(座位) == 1  且  vf_a4(座位) > 0（= 剩余生命 > 0）
#:
#: 两条都成立才画「完成」那一帧，否则画「未完成」。我们一直发全 0，
#: 所以哪怕真通关也永远是「未完成」。`0x0411 gspEndGame` 的 `success`
#: 字段跟这个标签没关系（§99 实测 `success=True` 照样写「未完成」）。
GAME_RESULT_CLEARED = 1

#: 尾部数组第 i 项填这个值 = 「第 i 号座位**输了**」（对战的败方，§161）。
#:
#: 同一格还被 `0x55223f` 拿去 `setge` 选结算 BGM：**`>= 0` 放胜利曲**
#: （闯关 `BGM-StageClear` / 对战 `BGM-Victory`），负数才放失败曲
#: （`BGM-Failed` / `BGM-Lose`，串在 `0x672434` / `0x691d0c`）。
#: 所以「没通关」的 0 和「输了」的 -1 是两个不同的档，别合并：
#: V0.1 单机没通关时发的就是 0，改成 -1 会让单机的失败也开始放失败曲，
#: 那是没验过的行为变更。
GAME_RESULT_DEFEATED = -1

#: 12 个业务值里已查明语义的下标（会话 18，§116）。
#:
#: 客户端的**内存**布局不是 12 个 dword —— 反序列化 `0x54c6b4` 里 `+0x24`
#: 和 `+0x25` 是两个挨着的 bool（`0x5d59de` 线上读 4 字节、只存 1 字节），
#: 所以「线上第 k 个业务值」和「结构体偏移」差了 6 字节。下面这三个下标是
#: 按线序数出来的，别拿结构体偏移去推。
#:
#: 处理器 `0x55210d` 把它们搬进 GameContext，结算界面的基类画法
#: `GameContext::vf_44`（`0x48db6c`）再读出来画成三行「+%d」：
#:
#:     值 9  -> [ctx + 座位*4 + 0x2c]  读于 0x48e239  画在 y=0x15b  「经验值」
#:     值 10 -> [ctx + 座位*4 + 0x5c]  读于 0x48e4e7  画在 y=0x1b8  「金币」  (PIXEL)
#:     值 11 -> [ctx + 座位*4 + 0x44]  读于 0x48e3e6  画在 y=0x189  「竞技场分数」(LADDER POINT)
#:
#: 三行的顺序按 y 坐标排是 经验值 -> 竞技场分数 -> 金币，和截图一致；
#: 标签串 `LADDER POINT`(`0x670f2c`) / `PIXEL`(`0x660848`) 就写在各自的画法旁边。
GAME_RESULT_EXPERIENCE = 9      # 「经验值 +N」
GAME_RESULT_MONEY = 10          # 「金币 +N」
GAME_RESULT_LADDER_POINT = 11   # 「竞技场分数 +N」（闯关模式没有天梯分，发 0）

#: 剩下 9 个值一律 0。⚠ **别一次全填** —— `gameresult-probe` 把 12 个值填成
#: 201..212 时客户端 20 毫秒内主动断链（§100），至今没查出是哪一格干的。
#: 这三个是逐格验过的，其余保持 D019 的「不懂就填 0」。


#: ═══ 一局打完给多少经验 / 金币（§227 / D148）════════════════════════════
#:
#: ★ **背景**：V0.2 会话 43 之前，`settle_quest` 里一个 `score` 变量同时当
#: 「入账经验」「入账金币」「界面上的经验 +N」「界面上的金币 +N」和「分数」
#: 五份用 —— 玩家看到的三行数一模一样，用户实机报的就是这个。
#: 协议侧本来就是三个独立字段（`0x0309` 的值 9/10/11 和 `0x0411` 的 4 号位），
#: 只是被喂了同一个数。
#:
#: ★ **数值从哪来**：原版的**每局**奖励是服务端算的，客户端包里没有。
#: 能借的只有 `Pack_decrypt/Data/Promotion-chn.ini`（一次性**晋级任务**奖励，
#: 132 条 `RewardN=类型,数值`：类型 0=金币 100~1000、2=经验 20~120）——
#: 我们只借它的**量纲**，不照搬语义。
#:
#: 闯关基础奖励按关卡 id 递增：经验 20/30/…/80，金币 100/150/…/400。
QUEST_BASE_EXPERIENCE = 20
QUEST_BASE_EXPERIENCE_STEP = 10
QUEST_BASE_MONEY = 100
QUEST_BASE_MONEY_STEP = 50

#: 难度加成。闯关房描述符的第二个参数就是难度（1=简单 / 2=普通 / 3=困难，§68）。
#: 表外的难度按 1.0 处理（别为一个没见过的数把奖励算成 0）。
QUEST_DIFFICULTY_BONUS = {1: 1.0, 2: 1.6, 3: 2.5}

#: 没通关时基础奖励打几折。**不是 0** —— 打了半天不能一无所获，
#: 而且客户端在「未完成」时照样弹结算界面。
QUEST_FAILED_RATIO = 0.3

#: 关卡分数换成**经验**的除数。分数常见几百~上千，按 1/20 折成经验 ——
#: 打得好有回报，但主导项仍然是「打的是哪一关、什么难度」。
QUEST_SCORE_PER_EXPERIENCE = 20

#: 对战一局的奖励。经验：参战底薪 + 每杀 + 胜方加成；金币：只有底薪 + 胜方加成。
#: 对战一局比闯关短得多，数值也就小一档。
PVP_BASE_EXPERIENCE = 10
PVP_EXPERIENCE_PER_KILL = 3
PVP_WIN_EXPERIENCE = 15
PVP_BASE_MONEY = 30
PVP_WIN_MONEY = 50


def quest_reward(quest_id, difficulty, score, cleared):
    """闯关一局的 `(经验, 金币)`。纯函数，好单测。

    `quest_id` / `difficulty` 来自 `Conn.current_quest()`（闯关房描述符的两个
    参数）；拿不到（不是闯关房、参数不全）时按 1 级关卡、难度 1 算。

    ★★ **金币不吃分数加成**（D152）：它 = 关卡固定奖励 × 难度系数，**就这些**。
    本局在地上捡到的金币由 `settle_quest` 另加（`RoomQuest.coins`）——
    「打得好」的回报走经验，「捡得勤」的回报走地上那些金币，两条线分开。
    """
    try:
        quest_id = max(1, int(quest_id))
    except (TypeError, ValueError):
        quest_id = 1
    try:
        difficulty = int(difficulty)
    except (TypeError, ValueError):
        difficulty = 1
    score = max(0, int(score))
    bonus = QUEST_DIFFICULTY_BONUS.get(difficulty, 1.0)
    ratio = bonus if cleared else bonus * QUEST_FAILED_RATIO
    base_exp = QUEST_BASE_EXPERIENCE + QUEST_BASE_EXPERIENCE_STEP * (quest_id - 1)
    base_money = QUEST_BASE_MONEY + QUEST_BASE_MONEY_STEP * (quest_id - 1)
    experience = int(base_exp * ratio) + score // QUEST_SCORE_PER_EXPERIENCE
    money = int(base_money * ratio)
    return experience, money


def pvp_reward(kills, won):
    """对战一局的 `(经验, 金币)`。`won` 就是尾部数组里那一格 == 1。

    ★ 输了也给底薪：对战的一局可能就几分钟，一分不给会逼人挂机刷闯关。

    ★★ **金币不吃杀敌数**（D152，和闯关同一条口径）：杀敌数就是对战的分数，
    所以金币只剩「参战底薪 + 胜方加成」这两个固定值，再由 `settle_quest`
    加上本局捡到的金币。经验仍然按杀敌数走 —— 技术好照样有回报。
    """
    kills = max(0, int(kills))
    experience = (PVP_BASE_EXPERIENCE + PVP_EXPERIENCE_PER_KILL * kills
                  + (PVP_WIN_EXPERIENCE if won else 0))
    money = PVP_BASE_MONEY + (PVP_WIN_MONEY if won else 0)
    return experience, money


def build_game_result_values(experience=0, money=0, ladder_point=0):
    """按 §116 的语义组 `gspRepGameResult` 的 12 个业务值（其余全 0）。

    三个都是**本局增量**（界面上就写成 `+%d`），不是账号总额 ——
    和 `0x0411 gspEndGame` 那三个「绝对累计值」正好相反，别搞混。
    """
    values = [0] * GAME_RESULT_VALUE_COUNT
    values[GAME_RESULT_EXPERIENCE] = int(experience)
    values[GAME_RESULT_MONEY] = int(money)
    values[GAME_RESULT_LADDER_POINT] = int(ladder_point)
    return values


def build_game_result_tail(seat_id=0, cleared=False):
    """组 `gspRepGameResult` 的尾部数组：只有通关时把自己那一格置 1。

    其余座位留 0（单机只有一个人在座位 0）。见 `GAME_RESULT_CLEARED`。
    """
    tail = [0] * GAME_RESULT_TAIL_COUNT
    if cleared and 0 <= seat_id < GAME_RESULT_TAIL_COUNT:
        tail[seat_id] = GAME_RESULT_CLEARED
    return tail


def build_rep_game_result(seat_id=0, values=None, tail=None):
    """opcode 0x0309 —— `Packet_gspRepGameResult`（vft `0x691898`）。结算界面的数据源。

    ★ **它必须排在 `0x0411 gspEndGame` 之前**（FINDINGS §99）。两个包分工不同：

        0x0411  -> 右上角玩家数据栏的四个全局，发的是**绝对累计值**
        0x0309  -> GameContext 里的结算表 -> **结算界面**，发的是**本局增量**

    只发 `0x0411` 的话结算界面构造得出来却没有数据，于是画面上什么都不显示 ——
    §93 观察到的「`[GameContext+4]==1` 但看不见界面」就是这个原因。

    反序列化 `0x54c6b4` 依次读 13 个 4 字节字段再读一个 int32 数组。**两个 bool
    字段在线上也是 4 字节**（`0x5d59de` 读 4 字节再折成 1 字节存），别写成 1 字节
    —— 但它们在**结构体里**只占 1 字节且挨在一起（`+0x24` / `+0x25`），
    所以从这两个字段之后，「线序」比「结构体偏移」少 6 字节（§116）。

    处理器 `0x55210d` 的落位（`ebx` = `[0x72e2dc]` = GameContext）：

        pkt+0x04  座位号   ← 先过 `0x4045f9` 的座位有效性检查，不合法整包丢弃
        pkt+0x0c  bool     -> [GameContext + seat + 0x164]   （值 1）
        pkt+0x18  非 0 时走 `0x552170` 的提示分支             （值 4）
        pkt+0x25  bool     -> `0x493dd4` 的第 2 个参数        （值 8）
        pkt+0x28           -> [GameContext + seat*4 + 0x2c]  （值 9  = 经验值）
        pkt+0x2c           -> [GameContext + seat*4 + 0x5c]  （值 10 = 金币）
        pkt+0x30           -> [GameContext + seat*4 + 0x44]  （值 11 = 竞技场分数）
        pkt+0x34  数组     -> 前 6 个 dword 搬进 [GameContext+0x184]
                             ★ **这就是「完成 / 未完成」标签**，见
                               `GAME_RESULT_CLEARED` / `build_game_result_tail`
        之后 `0x552242` 按 `[LobbyStage+0x1c] == 2`（闯关）分支选胜/负文本

    ★ **`0x0309` 只能在战斗中发**：`0x55210d` 直接解引用 GameContext，
    关卡一结束它就变 0（§93 实测），那时再发就是空指针崩溃。

    12 个业务值里查明了三个（会话 18，§116，见 `build_game_result_values`）——
    结算界面「经验值 / 金币 / 竞技场分数」三行；剩下 9 个按 D019 保持 0。
    **通关标签不在这 12 个里，在尾部数组里**（会话 17 查明，§112）；
    **「分数 / 生命」那一行也不在这个包里，在 `0x0411` 里**（§116）。
    """
    values = list(values if values is not None else [0] * GAME_RESULT_VALUE_COUNT)
    if len(values) != GAME_RESULT_VALUE_COUNT:
        raise ValueError(
            f"gspRepGameResult needs exactly {GAME_RESULT_VALUE_COUNT} values "
            f"after the seat id, got {len(values)}")
    tail = list(tail if tail is not None else [0] * GAME_RESULT_TAIL_COUNT)
    if len(tail) < GAME_RESULT_TAIL_COUNT:
        raise ValueError(
            f"gspRepGameResult 的尾部数组至少要 {GAME_RESULT_TAIL_COUNT} 项"
            f"（0x5521d2 无条件读 6 个 dword），只给了 {len(tail)} 项")
    return (w_i32(seat_id)
            + b"".join(w_i32(v) for v in values)
            + w_i32(len(tail))
            + b"".join(w_i32(v) for v in tail))


#: `0x513278 ObjectFactory` 认得的物件 id（跳表 `0x513b2a` + `0x5134cd` 起的
#: 大 id 比较链，类名按构造函数里写的 vftable 反查 `re/vftables.json` 得到）。
#: 只列关卡里会掉的那些；101..111 / 200..210 是地图编辑器摆的场景物件。
ITEM_NAMES = {
    10000: "ItemBox 宝箱",
    10001: "LuckBag 幸运袋",
    10100: "HeartItem 心（回血）",
    10101: "CoinItem1 金币×1",
    10102: "CoinItem5 金币×5",
    10103: "ItemCard 称号卡片",
    10104: "ItemEventFruit 活动果实",
    10200: "NukeLauncherItem 核弹发射器",
    10201: "FireThrowerItem 火焰喷射器",
    10202: "WaterCannonItem 水炮",
    10300: "ShieldItem 护盾",
    10301: "SpeedUpItem 加速",
    10302: "SpUpItem SP 上升",
    10303: "ReflectItem 反射",
    10304: "SizeDownItem 迷你",
    10306: "TripleShotItem 三连射",
    10307: "PowerShotItem 强力射击",
    10308: "HpChargeItem HP 回复剂",
    10309: "SpChargeItem SP 回复剂",
    10310: "FreezerItem 冰冻",
    10311: "HudDevilItem 幽灵",
    10312: "CloakingItem 隐身",
    10313: "TeamHpChargeItem 全队 HP 回复剂",
    10314: "TeamReflectItem 全队反射",
    10400: "SlowMineItem 胶水",
    10401: "SmokeItem 烟雾弹",
    10500: "BulletPoisonItem 毒",
    10603: "SlowMineObject 减速地雷",
}

#: ★★ **`Data/Item.ini` 里真的有记录的物件 id**（§201）。
#:
#: `Character::UseItemEffect`（`0x508441`）**第一件事**就是拿物件 id 去
#: 记录表（`0x72e7f0`，哈希表，查找 `0x4157bf`）里取记录，
#: **取不到就直接 return，一个字节都不做**：
#:
#:     lea esi, [ebp+8]          ; &物件id
#:     mov eax, 0x72e7f0
#:     call 0x417a76             ; 查不到会插一个空槽再返回它的地址
#:     mov eax, [eax]
#:     cmp eax, 0
#:     je  0x508dc6              ; ★ 没记录 -> 什么都不发生
#:
#: 而那张表的唯一数据源就是 `Item.ini`（记录里 `+8` 那一格 = `CharAttr`，
#: 全客户端只有 `Item.ini` 把 `ItemId` 和 `CharAttr` 配在一起）。
#: ⇒ **不在这张表里的物件，哪怕工厂建得出箱子、捡得起来、进得了道具槽，
#: 按 Ctrl 也永远不会有任何效果。**
ITEM_INI_ITEM_IDS = frozenset({
    10300, 10301, 10303, 10304, 10305, 10306, 10307, 10308, 10309,
    10310, 10311, 10312, 10313, 10314, 10315, 10316,
    10400, 10401, 10500, 10600, 10601, 10602, 10603, 10604,
})

#: ★ **道具模式下服务端往地图上刷的道具**（§191）。
#:
#: 这些类的构造函数都 push `Game/ItemBox`，所以玩家看到的都是一个箱子；
#: 捡起来进角色的 4 个道具槽（`[Character+0x764..0x770]`），按 Ctrl 使用。
#:
#: ⚠ **10305（FastShot）不在里面**：`Item.ini` 有这一条，但工厂 `0x513278`
#: 的跳表 `0x513b56` 里 10305 那一格指的是 default 分支（§191）——
#: 发下去客户端建不出对象，绝对不要加回来。
#: ⚠ **10302（SpUpItem）也不在里面**，理由**正好相反**（§201）：工厂建得出
#: 箱子、`UseItemEffect` 里甚至还有它专门的分支（`0x50891a`），但这一版的
#: `Item.ini` **压根没有 `[SpUp]` 这一节** —— 于是上面那条 `je 0x508dc6`
#: 恒成立，玩家捡到「SP 上升」按 Ctrl 是**彻底没反应**（道具还白扣一格）。
#: ⚠ 10000 `ItemBox` / 10001 `LuckBag` / 10100~10202 是**闯关**的掉落物，
#: 不是 PvP 道具，别混进来。
PVP_ITEM_IDS = (10300, 10301, 10303, 10304, 10306, 10307, 10308,
                10309, 10310, 10311, 10312, 10400, 10401, 10500)

#: 只在组队模式下刷的两件（效果作用于「全队」，个人战里等于只对自己生效，
#: 摆出来只会让人以为捡错了）。
PVP_TEAM_ITEM_IDS = (10313, 10314)

#: ★★★ **道具模式地图上的三把「捡了就换武器」的特殊武器**（§223）。
#:
#: 上面那 14 + 2 件外观全是一个 `Game/ItemBox` 箱子；**这三件不是** ——
#: 它们的构造函数 push 的是角色武器网格，地上躺着的就是那把枪：
#:
#: | id | 类 | 模型 | `vf_11c` 给的武器（`weapon.ini`）|
#: |---|---|---|---|
#: | 10200 | `NukeLauncherItem` | `Ch00/ch00D0005.msh` | 1900020 `미니핵런처` 迷你核弹发射器 |
#: | 10201 | `FireThrowerItem`  | `Ch00/ch00D0004.msh` | 1900000 `화염방사기` 火焰喷射器 |
#: | 10202 | `WaterCannonItem`  | `Ch00/ch00D0004.msh` | 1900030 `물대포` 水炮 |
#:
#: ★ **绝不能进 `GRANTABLE_ITEM_IDS`**：它们建构时基类第 4 个参数是 **0**
#: （`0x522240` 把它原样转给 `0x51f24a`，写进 `[item+0x2a9]`），所以
#: `Item::vf_d4`（`0x51f447`）那条「当场生效」的分支对它们**成立** ——
#: 拾取放行 `0x0405` 一到，客户端自己就调 `vf_11c` 换枪了（`0x5231de` /
#: `0x521fad` / `0x5251a4`：`Character::GiveWeapon(0x517121)`）。
#: 再补一发 `0x040b` 等于凭空往道具槽里多塞一件（§194 已经写死了这条规矩）。
#:
#: ⚠ `vf_11c` 里有一句 `cmp [character+0x2ac], 0x409f7d()` —— **只有那台机器上的
#: 本地玩家换枪**。所以拾取放行必须**广播**（`on_get_item` 本来就是广播），
#: 捡的人那台机器才轮得到换枪，别人那台只把枪从地上抹掉。
#:
#: ⚠ 它们**不在** `Item.ini` 里，也不该在 —— `Item.ini` 那张表是给
#: `UseItemEffect`（按 Ctrl）用的，换武器根本不走那条路（§201 只约束进槽的道具）。
PVP_WEAPON_ITEM_IDS = (10200, 10201, 10202)

#: ★ 捡到之后**要进道具槽**的物件 id（§194）。
#:
#: 判据不是「谁刷的」而是**物件本身的类型**：这 17 个类构造时基类第 4 个参数
#: 是 1（写进 `[item+0x2a9]`），于是 `Item::vf_d4`（`0x51f447`）那条
#: 「当场生效」的分支对它们恒不成立 —— 它们必须先进槽、再按 Ctrl 用。
#: 金币（10101/10102）、红心（10100）、武器（10200~10202）那些参数是 0，
#: 拾取当场就生效，**绝不能**给它们发 `0x040b`（客户端会凭空多出一件道具）。
#: ★★ **地上捡到的金币面额**（完整逆向见 `re/packet_api.md` 的 `0x0405`）。
#:
#: 旧调查只跟到 `CoinItem1/5::vf_11c` 前半段的音效 / 特效，漏了函数后半段：
#: `CoinItem1`（`0x52156f`）把 **1**、`CoinItem5`（`0x521b2c`）把 **5** 传给
#: `0x493d96`；后者做 `本局金币[座位] += 面额`。闯关模式随后由 `0x493d76`
#: 取出**累计值**交给浮字函数，所以连续捡金币×1 显示 `1, 2, 3`，连续捡
#: 金币×5 才显示 `5, 10, 15`。它不是拾取序号，也不是每次都显示单枚面额。
#:
#: 这个本局计数不直接改大厅账户金币 `[0x72e330]`，所以服务端仍须在拾取仲裁时
#: 按同样的 1 / 5 记账，并在结算时持久化；否则头顶累计值和实际到账会不一致。
#:
#:   · **通关金币雨**在 `0x4a5552` 硬编码请求 `10101`（金币×1），共
#:     `20 × 通关时还活着的玩家数` 枚，因此当前显示 `1, 2, 3 …` 是原版行为。
#:   · **可破坏场景物**在 `0x4faaad` 以 6% 概率请求 `10102`（金币×5），
#:     捡这种币时累计数字才按 5 跳。
COIN_ITEM_VALUES = {10101: 1, 10102: 5}


def coin_value(item_id):
    """这件东西捡起来值几个金币？不是金币就是 0。"""
    return COIN_ITEM_VALUES.get(item_id, 0)


GRANTABLE_ITEM_IDS = frozenset(PVP_ITEM_IDS + PVP_TEAM_ITEM_IDS)

#: 角色身上的道具槽格数 —— `Character::AddItem`（`0x517037`）扫
#: `[Character+0x764]` 起的 **4** 格找空位，满了就**整个函数什么都不做**。
#: 服务端的镜像必须用同一个上限，否则「服务端以为你有 5 件」。
ITEM_SLOT_COUNT = 4

#: `0x040a` 里那两个跟着道具 id 一起下发的参数。
#:
#: 抄的是客户端自己那条路：`PvpItem::vf_11c`（`0x5225fb`）调
#: `Character::UseItemEffect(道具id, 0, -1, "")`。`0x040a` 的处理器
#: （`0x551d95`）做的就是把包里三个字段填进同一个调用，所以照抄这一组
#: = 和客户端本地生效**逐参数一致**。两个参数在 `0x508441` 里都只被当作
#: 局部变量重写，没有一条分支把它们当输入读，所以填什么其实都一样 ——
#: 但保真项目里没有理由不照抄（D110）。
ITEM_EFFECT_ARG2 = 0
ITEM_EFFECT_ARG3 = -1

#: 开局之后多久刷第一件、之后每隔多久刷一件、地图上最多同时躺几件。
#:
#: ★ **原版的数值已经无从考证**（服务端 15 年前就没了），这三个是我们定的
#: （D109）。想调就在这里改，改完**必须重启服务端**（铁律 7）。
ITEM_SPAWN_FIRST_DELAY = 5.0
ITEM_SPAWN_INTERVAL = 10.0
ITEM_SPAWN_MAX_ALIVE = 8

#: 一次刷新里，三把特殊武器（`PVP_WEAPON_ITEM_IDS`）各占几个签。
#:
#: 池子是「每个 id 一个签，`random.choice` 均匀抽」，所以这个数就是武器的
#: 相对权重：1 = 和普通道具一样常见（个人战 3/17 ≈ 18% 的刷新是武器），
#: 0 = 干脆不刷武器。★ **原版的比例同样无从考证**（D109 的老规矩），
#: 这个数是我们定的，想调就在这里改，改完**必须重启服务端**（铁律 7）。
PVP_WEAPON_SPAWN_WEIGHT = 1

#: 刷新点的坐标范围。**服务端不需要知道地图几何**（§192）：客户端收到
#: `0x0404` 会先 `fmod` 进地图（`X % World.width` / `Y % World.height`），
#: 再把埋在地形里的物件以 5 像素为步长顶到 300 像素内的空位上。
#: 所以这里给的是「比任何一张图都大的一个范围」，取模之后自然铺满整张图：
#: 实测最宽的图 11400、最高的 2048。
#: Y 取靠下的一段（Y 向下增大，§192）—— 落在地形里正好被顶到地面上，
#: 悬在半空的概率小得多。
ITEM_SPAWN_X_RANGE = (0.0, 11400.0)
ITEM_SPAWN_Y_RANGE = (0.0, 2048.0)

#: `0x0404` 后三个字段：客户端自己掉东西时发的就是 `3, -1, -1`
#: （`+0x18 == 1` 才走「宠物捡到」的特效分支，`+0x1c` 是配套的座位号）。
#: 服务端刷的道具照抄这一组，客户端那条路径就和原版掉落完全一致。
ITEM_SPAWN_TAIL = (3, -1, -1)

#: `gcpCreateItem`(0x0406，客户端 -> 服务端) 的线格式，序列化 `0x48c84f`。
#: 8 个字段紧凑排列 = 32 字节。
CREATE_ITEM_FORMAT = "<iffffiii"
CREATE_ITEM_SIZE = struct.calcsize(CREATE_ITEM_FORMAT)

#: 掉落物实例句柄的起点。句柄落进 `[obj+0xd0]`，`World::Add`(`0x473e7c`)
#: 拿它当 map 的 key，撞了就会覆盖已有物件。关卡自己的物件句柄实测在
#: `0x0010c9xx` 一带（死亡上报包里的 handle 就是同一个字段），
#: 起点拉到 `0x40000000` 谁也够不着，而且还是正 int32、不等于 -1。
ITEM_HANDLE_BASE = 0x40000000


def item_spawn_pool(team_mode=False):
    """道具模式一次刷新可以抽到的全部物件 id（含重复 = 权重）。

    三块：**箱子道具**（`PVP_ITEM_IDS`，捡了进 4 格道具槽）+
    **特殊武器**（`PVP_WEAPON_ITEM_IDS`，捡了当场换枪，§223）+
    组队战才加的**全队道具**（`PVP_TEAM_ITEM_IDS`）。

    武器按 `PVP_WEAPON_SPAWN_WEIGHT` 重复几遍来配比例；填 0 就是不刷武器。
    """
    weapons = tuple(PVP_WEAPON_ITEM_IDS) * max(0, int(PVP_WEAPON_SPAWN_WEIGHT))
    return (tuple(PVP_ITEM_IDS) + weapons
            + (tuple(PVP_TEAM_ITEM_IDS) if team_mode else ()))


def parse_create_item(payload):
    """opcode 0x0406（客户端 -> 服务端，32 字节）—— `gcpCreateItem`。

    ⚠ **这个包以前被记成「位置同步」（§108/§109），是错的**（§112 勘误）。
    整个镜像里 `push 0x406 / call 0x5bba0a`（`RawPacket::SetType`）**只有
    `0x493a57` 一个调用点**，就在 `GameContext::SendCreateItem`
    （`0x4939c0`）里，序列化器 `0x48c84f` 写的字段是：

        +0x00  int32  物件 id（见 ITEM_NAMES；10101 = 金币、10103 = 称号卡片…）
        +0x04  float  X
        +0x08  float  Y
        +0x0c  float  速度 X
        +0x10  float  速度 Y
        +0x14  int32  实测恒为 3
        +0x18  int32  实测恒为 -1
        +0x1c  int32  实测恒为 -1

    实测一条（怪物死后 62 毫秒）：

        da 27 00 00 | 00 60 19 45 | 00 80 97 43 | 0 | 0 | 03 | -1 | -1
        └ 10202 水炮 └ x=2454.0    └ y=303.0

    「位置同步」那个旧读法之所以看起来能用，是因为第 2、3 个 dword 恰好
    真的是**坐标**（掉落点就在角色/怪物附近），拿去当重生兜底坐标不会出事。

    客户端发完这个包就干等 `0x0404 gspCreatedItem`，**服务端不回就什么都不
    掉落** —— 和 §108 血量归零、§111 换图是同一类病。

    返回 8 个字段的元组；长度不够就抛 ValueError。
    """
    if len(payload) < CREATE_ITEM_SIZE:
        raise ValueError(
            f"gcpCreateItem 只有 {len(payload)} 字节，要 {CREATE_ITEM_SIZE}")
    return struct.unpack_from(CREATE_ITEM_FORMAT, payload, 0)


def build_created_item(instance_id, fields):
    """opcode 0x0404 —— `Packet_gspCreatedItem`（vft `0x69188c`）。掉落物落地。

    反序列化 `0x54c523` 读 **9** 个 4 字节字段（`0x5d59ff`），处理器
    `0x551a11` 的用法：

        +0x00  int32  ★ 实例句柄 -> 物件对象的 `[obj+0xd0]`，World 拿它当 map key
        +0x04  int32  物件 id -> `0x513278 ObjectFactory` 的分支选择
        +0x08  float  X ┐ 会先按地图记录的地面高度 `[map+0x50]` 夹一次，
        +0x0c  float  Y ┘ 再用 `0x473969` 找一个不卡在地形里的落点
        +0x10  float  速度 X -> `[obj+0x120]`
        +0x14  float  速度 Y -> `[obj+0x124]`
        +0x18  int32  == 1 时走「宠物捡到」的音效/特效分支（客户端发的是 3，不进）
        +0x1c  int32  座位号 0..5，配合上一格用；客户端发的是 -1
        +0x20  int32  **处理器整个函数都没读过它**（客户端那 8 个字段的最后一个
                      正好落在这里，实测是 -1）

    也就是说：**在客户端自报的 8 个字段前面插一个服务端分配的实例句柄**，
    9 个字段 36 字节，就是一发合法的应答。服务端不需要知道任何物件数据 ——
    掉什么、掉在哪、初速多少全是关卡脚本算好了报上来的（同 D046 的理由）。
    """
    fields = tuple(fields)
    if len(fields) != 8:
        raise ValueError(f"gspCreatedItem 要 8 个来自客户端的字段，给了 {len(fields)}")
    return w_i32(instance_id) + struct.pack(CREATE_ITEM_FORMAT, *fields)


#: `0x0407 gcpGetItem` / `0x0405`（服务端方向）共用的线格式：两个 int32。
#: 序列化 `0x558e9a` 写 `[esi]` / `[esi+4]`，反序列化 `0x5590bb` 读回同样两个，
#: **收发完全对称**，所以服务端原样回显就是合法应答（同 D046）。
GET_ITEM_FORMAT = "<ii"
GET_ITEM_SIZE = struct.calcsize(GET_ITEM_FORMAT)


def parse_get_item(payload):
    """opcode 0x0407（客户端 -> 服务端，8 字节）—— `gcpGetItem`「我要捡这件」。

    调用链（全静态定位，FINDINGS §115）：

        Character::CheckItemPickup 0x5154d3
            if [char+0x2b4] != 0: return          ; 只有本地操控的角色才检测
            遍历 World 的物件表 [World+0xdc]
              item = dynamic_cast<Item*>(节点)     ; 0x515516 推的类型描述符
              if [item+0x2a8] != 0: continue      ; ★ 这件已经报过一次了
              if [item+0x2aa] == 0: continue      ; 「可拾取」标志
              if !碰撞(0x50f410): continue
              GameContext::SendGetItem(0x493a99)( [char+0x2ac], [item+0xd0] )
              [item+0x2a8] = 1                    ; ★ 防重发，一件只报一次

    线格式：

        +0x00  int32  座位号   （= `[Character+0x2ac]`，单机固定 0）
        +0x04  int32  实例句柄 （= `[Item+0xd0]`，就是我们在 `0x0404` 里发的那个）

    实测两发（会话 18 的日志，句柄正是本局第 4 / 第 7 件金币）：

        00 00 00 00 | 03 00 00 40      座位 0，句柄 0x40000003
        00 00 00 00 | 06 00 00 40      座位 0，句柄 0x40000006

    ★★ **`[item+0x2a8] = 1` 是关键**：客户端发完就把这件标记成「已上报」，
    服务端不回的话它既不生效也不会再报第二次 —— 东西就永远躺在地上。
    这不是「偶尔捡不到」，是**彻底捡不起来**。

    返回 `(座位号, 实例句柄)`；长度不够就抛 ValueError。
    """
    if len(payload) < GET_ITEM_SIZE:
        raise ValueError(
            f"gcpGetItem 只有 {len(payload)} 字节，要 {GET_ITEM_SIZE}")
    return struct.unpack_from(GET_ITEM_FORMAT, payload, 0)


def build_picked_item(seat_id, instance_id):
    """opcode 0x0405（服务端方向）—— 拾取放行。**原样回显客户端那两个字段**。

    处理器 `0x551d35`：

        0x5590bb  读两个 int32
        0x404ff6  座位号 -> [LobbyStage + 座位*4 + 0x1d0] = 角色对象（越界返回 0）
        0x474225  实例句柄 -> World::Find -> dynamic_cast<Item*>（`0x6e2328`）
        0x551d89  两个都非空才 item->vf_d4(角色)

    `Item::vf_d4`（`0x51f447`）：

        if [item+0x2a9] == 0: vf_11c(角色)   ; 生效（CoinItem1 加钱、HeartItem 回血…）
        vf_20()                              ; 从世界里删掉物件

    ★★ **对 PvP 道具来说，这一发只是「把箱子从地上抹掉 + 放特效」。**
    17 个 PvP 道具类的 `vf_d4` 是重写过的 `0x5224fe`：它调完基类
    `0x51f447` 之后只剩「放拾取特效 / 音效 / 冒一行提示」，而基类那条
    `if ([item+0x2a9] == 0) vf_11c()`（当场生效）对它们**恒不成立**
    —— 参数是 1。所以拾取放行**不会**让道具进道具槽，那要另发
    `0x040b`（`build_grant_item`，§194）。金币 / 红心 / 武器的
    `[+0x2a9]` 是 0，走的才是当场生效那条，不需要 `0x040b`。
    """
    return struct.pack(GET_ITEM_FORMAT, int(seat_id), int(instance_id))


#: `0x040b`（服务端方向）/ `0x040c`（两个方向）的线格式：**一个 int32**。
#:
#: ⚠ §193 初记的「u16」是错的：两个处理器读字段用的都是 `0x5d5984`，
#: 而它是 `Read(&buf, 4)`（`0x5d598a` 那句 `push 4`）—— 读一个 int32。
#: u16 的那个原语是 `0x5d5942`（`push 1` 是 u8）。已在 §194 更正。
ITEM_SLOT_FORMAT = "<i"
ITEM_SLOT_SIZE = struct.calcsize(ITEM_SLOT_FORMAT)


def build_grant_item(item_id):
    """opcode 0x040b（服务端 -> 客户端，4 字节）—— 「往你的道具槽里塞一件」。

    处理器 `0x55206b`：

        0x5d5984  读一个 int32 = 物件 id
        0x409f39  取**本机玩家自己**的角色（`[GameSession]` -> `0x409e20`）
        0x517037  Character::AddItem(id, 播提示=1)

    ★★ **它认的是「收包的这台机器上的本地玩家」，包里没有座位号** ——
    所以这一发**只能发给捡到东西的那个人**，广播出去等于人手一件。

    `AddItem` 做三件事：把 id 写进第一个空槽 `[Character+0x764+i*4]`、
    用 `ObjectFactory`（句柄 -1，不进 World）建一个图标对象存进
    `[+0x778+i*4]` 和 `[+0x798]`、再起一个 3000 毫秒的提示计时器
    （`[+0x788]`）。**4 格满了就整个函数什么都不做**，所以服务端要自己
    卡住 `ITEM_SLOT_COUNT`，否则两边的槽会错位。
    """
    return struct.pack(ITEM_SLOT_FORMAT, int(item_id))


def parse_use_item(payload):
    """opcode 0x040c（客户端 -> 服务端，4 字节）—— 「我要用道具槽第 N 格」。

    发送点 `0x516335`（`Character` 的输入处理）：按住 / 按下那个键
    （输入状态 `[0x72e2bc]` 的 `+0x3a7` / `+0x3a5`）就

        [栈上的字段] = 0            ; ★ 客户端**恒发 0**
        序列化 0x559205（push 0x40c，写一个 int32）
        发出去
        放一声音效（id 0xd）

    —— **然后什么都不做，等服务端**。效果和扣道具都在服务端手上（§194）。

    恒发 0 是因为 `RemoveItem` 拿掉一格之后会把后面的往前挪，
    所以「下一件要用的」永远在第 0 格。服务端照着包里的序号取，
    不要自作主张改成别的 —— 万一将来客户端真发了别的值，跟着走才是对的。

    返回槽位序号；长度不够就抛 ValueError。
    """
    if len(payload) < ITEM_SLOT_SIZE:
        raise ValueError(
            f"rawUseItem 只有 {len(payload)} 字节，要 {ITEM_SLOT_SIZE}")
    return struct.unpack_from(ITEM_SLOT_FORMAT, payload, 0)[0]


def build_use_item(slot_index):
    """opcode 0x040c（服务端 -> 客户端，4 字节）—— 「把你的第 N 格拿掉」。

    处理器 `0x552089`：读一个 int32 -> 取**本机**角色 -> `Character::RemoveItem`
    （`0x5170b4`）：销毁那一格的图标对象、把后面的往前挪、末格清空。

    ★ 和 `0x040b` 一样按「收包的本地玩家」认人，**只发给用道具的那个人**。
    ★ `RemoveItem` 在整个客户端里**只有这一个调用点** —— 不回这一发，
      道具就永远卡在槽里，玩家会觉得「按了没反应」。
    """
    return struct.pack(ITEM_SLOT_FORMAT, int(slot_index))


#: `0x040a`（服务端方向）的线格式：4 个 int32，反序列化 `0x5590d5`。
#:
#:     +0x00  int32  座位号 -> [LobbyStage + 座位*4 + 0x1d0] 取角色（`0x404ff6`）
#:     +0x04  int32  ┐ 处理器把这三个原样填进
#:     +0x08  int32  │ `Character::UseItemEffect(第 0x08 个, 第 0x0c 个, 第 0x04 个, "")`
#:     +0x0c  int32  ┘ （`0x551dc7`~`0x551dd2` 的 push 顺序）
#:
#: 也就是说**道具 id 在第 3 个字段**，不是第 2 个。顺序看着别扭，
#: 但那正是客户端读的顺序，别按直觉重排。
ITEM_EFFECT_FORMAT = "<iiii"
ITEM_EFFECT_SIZE = struct.calcsize(ITEM_EFFECT_FORMAT)


def build_item_effect(seat_id, item_id,
                      arg2=ITEM_EFFECT_ARG2, arg3=ITEM_EFFECT_ARG3):
    """opcode 0x040a（服务端 -> 客户端，16 字节）—— 「某个座位吃到某件道具的效果」。

    处理器 `0x551d95` 把包里的座位号换成角色对象，再调
    `Character::UseItemEffect`（`0x508441`）。那个函数先按 id 去
    `Item.ini` 的记录表（`0x72e7f0`）里查这件道具的数据，查不到直接返回；
    查到了就按 id 分支（全队道具 10313/10314 会在那里被换成 10308/10303
    并对六个座位各来一遍），其余落到通用分支 `0x508de6` 按记录里的
    数值加 buff。**服务端因此一点道具数值都不需要知道**（同 D046 的理由）。

    ★★ **这一发要广播给全房间**：处理器按包里的**座位号**找角色，
    每台机器上算出来的是同一个人。不广播的话别人屏幕上你既不会加速、
    也不会亮护盾 —— 而伤害结算是各机器各算的，那就直接对不上了。
    """
    return struct.pack(ITEM_EFFECT_FORMAT,
                       int(seat_id), int(arg3), int(item_id), int(arg2))


#: 角色属性号（`Data/Status.ini` 的小节名）-> 人话。**只给日志用**。
#:
#: 属性号不是道具 id：`Item.ini` 每件道具有一格 `CharAttr`，
#: `UseItemEffect` 就是拿它当索引去 `Status.ini` 取时长 / 弹数（§201）。
#: `0x040d` 的载荷里带的是**属性号**，所以要靠这张表才看得懂日志。
CHAR_ATTR_NAMES = {
    0: "基本状态",
    1: "护盾",
    2: "加速",
    3: "反射",
    4: "迷你",
    5: "快速射击",
    6: "三重射击",
    7: "致命射击（双倍伤害）",
    8: "HP 回复中",
    9: "SP 回复中",
    10: "毒弹",
    11: "中毒",
    12: "冰冻",
    13: "幽灵缠身",
    14: "减速",
    15: "无法射击",
    16: "无法移动",
    17: "隐身",
    18: "SP 消耗减半",
    19: "复活无敌",
    20: "任务主状态",
}

#: `Status.ini` 一共 21 个小节（0~20），`Character::AddAttrVisual` 的跳表
#: （`0x5097de`）也正好 20 项（属性号 1~20）。越界的属性号客户端会
#: `ja` 到 return，转发出去无害，但我们照样挡掉 —— 没有合法来源。
CHAR_ATTR_MAX = 20

#: `0x040d` 两个方向的线格式：**两个 int32**（序列化 `0x558f8e`、
#: 反序列化 `0x5590bb`，都是 4 字节读写），共 8 字节。
REMOVE_CHAR_ATTR_FORMAT = "<ii"
REMOVE_CHAR_ATTR_SIZE = struct.calcsize(REMOVE_CHAR_ATTR_FORMAT)


def parse_remove_char_attr(payload):
    """opcode 0x040d（客户端 -> 服务端，8 字节）—— 「我这个道具效果结束了」。

        +0x00  int32  座位号（发送点 `0x509858` 推的是 `[char+0x2ac]`，
                      而那一条分支的前置判据就是「== 我的座位」，
                      所以**只可能是发包人自己**）
        +0x04  int32  属性号（`Status.ini` 的小节号，见 `CHAR_ATTR_NAMES`）

    返回 `(座位号, 属性号)`；长度不够抛 ValueError。
    """
    if len(payload) < REMOVE_CHAR_ATTR_SIZE:
        raise ValueError(
            f"rawRemoveCharAttr 只有 {len(payload)} 字节，"
            f"要 {REMOVE_CHAR_ATTR_SIZE}")
    return struct.unpack_from(REMOVE_CHAR_ATTR_FORMAT, payload, 0)


def build_remove_char_attr(seat_id, attr_id):
    """opcode 0x040d（服务端 -> 客户端，8 字节）—— 「某个座位的效果结束了」。

    处理器 `0x551dfb` 拿座位号找角色，调 `Character::RemoveAttrEffect`
    （`0x50982e`，拆模型 / 特效）+ `AttrList::Remove`（`[char+0x6a0]`）。

    ★★ **广播，但可以不发给上报的那个人**：他自己那台机器早就拆完了，
    而且 `0x551dfb` 开头那句 `if (座位 == 我的座位) return` 会把回给他的
    那一发直接丢掉 —— 发了也只是白费字节。
    """
    return struct.pack(REMOVE_CHAR_ATTR_FORMAT, int(seat_id), int(attr_id))


#: `0x0408`（客户端方向）/ `0x0406`（服务端方向）的**线上**布局。
#:
#: ★★ **和客户端栈对象的字段偏移不是一回事。** 序列化器 `0x558f16` 是逐字段
#: 紧凑写的，所以线上是 `u32 句柄 | u8 座位 | u8 凶手 | i32 死亡次数 | f32 X | f32 Y`
#: = 4+1+1+4+4+4 = 18 字节，**死亡次数在线偏移 6**；而客户端结构体里它在 `+0x08`
#: （中间那两个字节是编译器的对齐填充）。
#:
#: 会话 15 在这里踩过一次：按 `+0x08` 去就地改包，实际改到了「死亡次数的高半边 +
#: X 的低半边」，死亡次数变成六万多，客户端左下角状态面板算出的
#: `剩余生命 = 最大生命 - 死亡次数` 成了大负数，`0x472527` 拿它当数组下标
#: （`[心形数组 + esi*4] = 0`）当场越界 C0000005。
#: **所以收发一律用这同一个格式串重新打包，不要手算偏移。**
DEATH_REPORT_FORMAT = "<IBBiff"
DEATH_REPORT_SIZE = struct.calcsize(DEATH_REPORT_FORMAT)


def parse_report_hp_zero(payload):
    """opcode 0x0408（客户端 -> 服务端，18 字节）—— 「某个角色 HP 归零了」。

    **这是死亡链的第一环，不是遥测**（FINDINGS §108 推翻了「0x0408 语义未查明
    但纯遥测」的旧记法）。整条链是：

        HP<=0 (`0x50f778`)
          -> `Character::OnHpZero` vft+0xd8 = `0x4ffab0`
          -> `GameContext::vf_1c()` = `0x46e188` **恒返回 0**（闯关/对战/房间都是）
             所以客户端**不会自己判死**，
          -> `GameContext::ReportDeath` `0x493855` -> `0x558f16` 发本包
          -> 等服务端回 `0x0406`（服务端方向 = 死亡广播）才真的倒下。

    ★★ **线偏移和客户端结构体偏移不是一回事**，见 `DEATH_REPORT_FORMAT`：

        线 +0x00  int32  角色对象句柄（[char+0xd0]），World::Find 用它找对象
        线 +0x04  u8     座位号（[char+0x2ac]）；怪物是 0xff = -1
        线 +0x05  u8     传给 `Character::Die()` 的参数（[char+0x158]，凶手 id；
                         实测怪物打死是 0xff，掉岩浆/自杀是 0x00）
        线 +0x06  int32  ★ **死亡次数**：`Character::vf_c0()` = `[char+0x600]`，
                         也就是「我死之前已经死过几次」。见 build_broadcast_death
        线 +0x0a  float  X
        线 +0x0e  float  Y

    返回一个 dict。长度不够就抛 ValueError。
    """
    if len(payload) < DEATH_REPORT_SIZE:
        raise ValueError(f"0x0408 只有 {len(payload)} 字节，"
                         f"至少要 {DEATH_REPORT_SIZE}")
    # 句柄是不透明 id，按无符号读，日志里才好和探针读到的 [char+0xd0] 对上。
    handle, seat, arg, deaths, x, y = struct.unpack_from(DEATH_REPORT_FORMAT,
                                                         payload, 0)
    return {"handle": handle, "seat": seat if seat < 0x80 else seat - 0x100,
            "arg": arg, "deaths": deaths, "x": x, "y": y}


def build_broadcast_death(handle=0, seat=0, arg=0, death_count=1):
    """opcode 0x0406（服务端 -> 客户端）—— 死亡广播。

    处理器 `0x4938d2`（由 `GameContext` 的包分发表 `0x493808` 在 opcode 0x406
    这一格调过去；`ServerConnection::OnPacket` `0x54e036` 一进门就先问
    `GameContext::vf_e0`，所以战斗中这个分发表优先于大厅/游戏跳表）：

        u32 -> World::Find(句柄) 拿到角色对象，找不到就整包丢掉
        u8  -> 座位号；0<=座位<6 时给 `[ctx+0x384]` 的战绩表记一次死亡（`0x48c942`）
        u8  -> `Character::Die()` 的参数
        u32 -> ★ `Character::vf_c4(n)` = `0x4ff1fd`：`[char+0x600] = n`

    ★★ **`[char+0x600]` 就是 HUD 上那排心形的数据源**（§109）。
    战绩面板 `0x4a49a4` 起画心形的公式是：

        实心心形数 = GameContext::vf_a0(座位)      // = 最大生命
                   - Character::vf_c0()            // = [char+0x600]

    所以**这个字段必须是「新的死亡次数」，也就是客户端报上来的值 +1**。
    照抄回去（我们一开始就是这么干的）等于告诉客户端「你的死亡次数没变」，
    心形就永远是 3 颗 —— 而 `[ctx+0x384]` 那份战绩表是 `0x48c942` **本地**加的，
    所以「死 3 次判负」照常生效，只有显示不动。用户报的正是这个组合。

    ★★ **`[char+0x600]` 跨换图不清零**（bug调查/12 / §241）：`0x47900a` 把
    六个座位的角色**原样挂回世界**（同一批指针），不是卸掉重建。所以服务端的
    每句柄权威计数换图时只能清怪 / 物件那一份 —— 见 `RoomQuest.begin_map_change`。

    ★★ 客户端其实有**两个**死亡计数器，都靠这一发喂，漏发一发两个都会歪：
    `[char+0x600]`（本包这一格写，画心形）和 `[ctx+0x384]` 那份战绩表的
    `[vc + 座位*0x2c + 0x60]`（`0x48c942` **每收到一发本包就 +1**，不看这一格
    的值），后者决定还能不能重生 —— `Die()` 在 `0x501976` 读它，剩余生命为 0
    就把 `[char+0x2d8]` 写成 -1，永不重生。**最大生命不是恒定的 3**：生存模式
    是客户端写死的 3（`0x55db69`），闯关是 `[vc + 座位*4 + 0x198]`，来自关卡
    脚本，服务端拿不到；夺分 / 计时是 `0x7fffffff`（无限命）。

    ★ 读侧只读 **10 字节**，写侧的 0x0408 是 18 字节。实际下发时我们把收到的
    18 字节里这一格 +1 之后原样发回（多出来的 X/Y 客户端不读）。
    """
    return struct.pack("<IBBi", handle & 0xFFFFFFFF, seat & 0xFF, arg & 0xFF,
                       death_count)


def build_rep_quest_score(seat=0, score=0):
    """opcode 0x0415 `gspUpdateQuestScore`（服务端 -> 客户端）—— 战绩面板的分数。

    处理器 `0x4a3efe`（`GameContextQuest::vf_e0` 在基类不认时自己接的那一个）：

        int32 座位  int32 分数
        -> [GameContextQuest + 座位*4 + 0x3b8] = 分数     （面板读这里）
        -> [QuestVictoryCondition + 座位*0x2c + 0x7c] = 分数
        -> 0x55c2c8（刷新）

    ★ **分数是累计值，不是增量。** 客户端侧 `0x4a414a` 先算
    `[ctx+0x3b4] + 增量`，再把**新的累计值**交给 `0x4a40f8` 发 `0x0410`
    （500 毫秒节流，节流掉的那次增量不会丢，下一发带着最新累计值）。
    所以服务端把收到的数原样带座位发回去就对。
    """
    return w_i32(seat) + w_i32(score)


def build_change_controller_slot(old_seat=0, new_seat=0):
    """opcode 0x0414 `gspChangeControllerSlot`（服务端 -> 客户端，8 字节）。

    载荷 = **两个 int32**（反序列化 `0x54cfbf` = 两发 `0x5d59ff`，§180）：

        int32 old   把控制者表里等于这个座位号的格子…
        int32 new   …全部改成这个座位号

    处理器 `0x493780`（`GameContext` 的战斗包分发表 `0x493808` 里
    `0x0414` 那一格）做两件事：

        1. `[GameContext + 0x294 + i*4]`（= 句柄类别 20~25）6 格里
           凡是等于 `old` 的都换成 `new`；
        2. 遍历 `World` 的对象表，逐个调 `GameObject::vf_E8()` ——
           怪 / Boss / 刷怪点在那里重新问一次「这只归不归我」，
           归自己的就当场接管（刷怪点还会把刷怪计时重置到当前时刻）。

    `vf_E8` 在基类是 `ret`，不是控制者的对象整个 no-op，所以这个包
    **发给谁都安全**、重复发也只是让已经属于自己的怪重起一次 AI。
    """
    return w_i32(old_seat) + w_i32(new_seat)


def parse_req_change_to_next_map(payload):
    """opcode 0x0411 `gcpReqChangeToNextMap`（客户端 -> 服务端）—— 换图请求。

    载荷只有一个字段：**下一张地图的名字**（`u16 字符数 + UTF-16LE`，
    序列化 `0x404f49` -> `0x5d5a5a`，和别处的 wstring 完全同一套编码）。

    ★ **地图名是客户端自己查出来的**，服务端不需要（也没有）地图数据：
    地图脚本喊 `nextmap` 时传的是空串，`LobbyStage::ReqChangeToNextMap`
    (`0x4083e1`) 见到空串就走 `0x40841e`：先用 `0x405669` 取当前地图名
    （`[LobbyStage+0x3fc]` 非空则用它，否则用 `Session` 里的 `[+0x10]`），
    再用 `0x40b595` 在全局地图目录 `[0x72e3d8]` 里按名字查记录，
    取 `记录+0x0c` = 下一张地图名填进包里。查不到就**根本不发包**。

    所以服务端**原样回显这个名字**就是对的（同 D046 的思路）。
    """
    name = Reader(payload).wstr()
    if not name:
        # 客户端只在查到下一张地图时才发包，空名字说明我们把包读错了。
        raise ValueError("0x0411 的地图名是空的")
    return name


def build_rep_change_to_next_map(map_name):
    """opcode 0x0417 `gspRepChangeToNextMap`（服务端 -> 客户端）—— 放行换图。

    处理器 `0x408526`（大厅分发链 `0x4062cd` 的 `0x0417` 那一格）：

        反序列化 `0x419388` -> `0x5d5b3a`，一个 wstring = 新地图名
        -> [LobbyStage+0x3f9] = 0     ★ 解除「等服务端」状态（鼠标沙漏消失）
        -> [LobbyStage+0x3fa] = 0
        -> 0x4083c9: [LobbyStage+0x3fc] = 地图名; [LobbyStage+0x400] += 1
        -> 0x47900a: 卸掉六个座位的角色对象、起后台线程加载新地图

    ⚠ **绝不能回显客户端的 `0x0411`** —— 服务端方向的同号包是
    `gspEndGame`（结算），回显等于在关卡中途把玩家踢进结算界面。
    """
    return w_wstr(map_name)


def parse_respawn_request(payload):
    """opcode 0x0413 `gcpRespawnCharacter`（客户端 -> 服务端，16 字节）。

    发送点 `0x553e48`，和服务端方向的 `0x0419` **共用同一个反序列化器**
    `0x54c5d0`，所以线格式逐字相同：

        int32 -> +0x00   角色 id（[char+0x2ac]）
        int32 -> +0x04   X（客户端已用 ftol 把 float 截成整数）
        int32 -> +0x08   Y
        int32 -> +0x0c   重生点索引（[char+0x2b0]）

    ★ **坐标由客户端自己算好报上来**（`0x4fe70e` 选的重生点），所以服务端
    原样回显就一定落在本场景的合法位置 —— 会话 09 那次「写死 3225/635 把角色
    传到地图边缘、23 毫秒后收到 `0x0106 gcpReportHack`」的坑（§88）从根上没了。
    """
    if len(payload) < 16:
        raise ValueError(f"0x0413 只有 {len(payload)} 字节，至少要 16")
    character_id, x, y, spawn_index = struct.unpack_from("<iiii", payload, 0)
    return {"character_id": character_id, "x": x, "y": y,
            "spawn_index": spawn_index}


def build_respawn_character(character_id=0, x=DEFAULT_RESPAWN_X,
                            y=DEFAULT_RESPAWN_Y, unknown=0):
    """opcode 0x0419 —— `Packet_gspRespawnCharacter`（vft `0x6916b0`）。让角色重生。

    反序列化 `0x54c5d0` 读 **4 个 int32**（全部 `0x5d59ff`）：

        int32 -> +0x04   角色 / 座位 id
        int32 -> +0x08   ★ X 坐标
        int32 -> +0x0c   ★ Y 坐标
        int32 -> +0x10   语义未查

    处理器 `0x553ecc`（分发树 `0x54e3f6` → 这里）读完直接调
    `[stage_vft+0xd4](id, (float)X, (float)Y, +0x10)`。

    ★ **坐标在线上是整数**：客户端用 `fild` 把它们转成 float
    （`0x553ee4` / `0x553ef5`），所以这里写 int32 而不是 IEEE754 float。
    """
    return w_i32(character_id) + w_i32(x) + w_i32(y) + w_i32(unknown)


#: ★ 开局种子的取值范围。**下界是 1，不能出 0 也不能出 -1**：
#: `-1` 在客户端 `0x40b6e5` 里是「用线程本地随机源」的哨兵（`0x40b737` 的
#: `cmp [ebp+0x18], -1`），一旦发出去房里每个人就会各随各的图；`0` 是我们
#: V0.1 单机时代的固定值，留着只会让「随机地图」永远随到同一张（§228）。
GAME_SEED_MIN = 1
GAME_SEED_MAX = 2 ** 31 - 2


def new_game_seed():
    """开一局用的共享随机种子。房里每个人拿到的必须是**同一个**。"""
    return random.randint(GAME_SEED_MIN, GAME_SEED_MAX)


class StartGameHandshake:
    """闯关房「F5 游戏开始」之后的开局握手（单客户端）。

    返回值是一串 ``(opcode, payload)``，按顺序下发。

    ## 这两个 opcode 都是「命令客户端换 stage」，不是普通应答

    房间/游戏分发器 `0x54e036` 里：

        0x0400 gspPrepareGame -> 0x551605
            闯关（descriptor.type == 2）不走前面那段只对 type 1/5 生效的逻辑，
            直接落到 `0x5517a3`：
                inc [LobbyStage+0x3c] / mov [LobbyStage+4], 4 / call 0x407678
                push 6 / pop edx / call 0x40e47f      ← ★ 切到 stage 6（准备/加载）
        0x0402 -> 0x5517d0
            记时间戳到 [LobbyStage+0x3dc]、清 [0x72e2cc]+0xf7c 的倒计时状态，然后
                push 7 / pop edx / jmp 0x40e47f       ← ★ 切到 stage 7（游戏）

    `0x40e47f` 就是 `0x54f820` 建房时用来切 RoomStage(5) 的那个函数。

    ## 所以顺序是硬约束：先 6 后 7，一次只推一个 stage

    会话 09 实测：第二个 0x0402 一到就回 `0x0402` + `0x0412`，客户端被直接踢进
    stage 7，房间的座位角色对象（`[LobbyStage+0x1d0+i*4]`）随之销毁，而开局数据
    从没下发，渲染路径解引用那个已经变成 NULL 的指针 -> `C0000005 @ 0x50a368`
    （FINDINGS §82，探针 `logs/probe_s09b_seats.txt` 拍到对象从 0x20144020 变 0）。

    现在改成第二个 0x0402 只回 **`0x0400`**，让客户端先进 stage 6 把关卡加载起来，
    stage 7 等它加载完自己开口要（观测到下一批请求再决定发什么）。

    ## 不发 `0x0412`

    `0x0412` 在 `0x54e036` 的 `0x0405..0x0413` 跳表（`@0x54e5ae`）里映射到
    `0x54e546` = **未处理**，客户端收到就丢。`build_rep_count_down` 先留着，
    等确认它属于哪个 stage 的分发器再说。
    """

    WAIT_START = "wait_start"
    WAIT_CONFIRM = "wait_confirm"
    PREPARING = "preparing"
    IN_GAME = "in_game"

    def __init__(self, seed=0, seed_source=None):
        self.seed = seed
        self.state = self.WAIT_START
        #: ★ **每局重取一次**的种子发号器（§228）。`0x0400` 里那个 int32 是
        #: 客户端「按条件过滤地图目录 + 随机取一张」`0x40b6e5` 的第 5 个参数：
        #: 不是 -1 就用它构造一个**确定性**随机源（`0x5d8cb6`），所以房里每个人
        #: 算出来的是同一张图。以前这个数**恒为 0**，随机地图会永远随到同一张。
        #: 注入点留着是为了单测能复现（传一个返回常量的函数即可）。
        self.seed_source = seed_source or new_game_seed

    def reset(self):
        self.state = self.WAIT_START

    def next_seed(self):
        """要发 `0x0400` 了，取这一局的种子。取不出来就沿用上一局的。"""
        try:
            self.seed = int(self.seed_source())
        except Exception as error:          # noqa: BLE001 —— 发号器坏了不该拦住开局
            log(f"⚠ 取开局种子失败（{error!r}），沿用上一局的 {self.seed}")
        return self.seed

    def on_client_packet(self, opcode, payload):
        if payload:
            return []

        if opcode == OP_COUNT_GAME_READY:
            if self.state == self.WAIT_START:
                self.state = self.WAIT_CONFIRM
                return [(OP_TRIGGER_COUNT_GAME, build_trigger_count_game(0))]
            if self.state == self.WAIT_CONFIRM:
                self.state = self.PREPARING
                # ★ 种子在**真要发 `0x0400` 的这一刻**重取 —— 而不是建房、
                #   也不是 `reset()`。这样「这一局用哪个种子」和「这一局开始了」
                #   是同一个事件，中途谁进谁出都不会让它变。
                return [(OP_PREPARE_GAME, build_prepare_game(self.next_seed()))]
            return []

        # 客户端在 stage 6 把关卡加载到 100% 之后，每 5 秒发一次空 0x0403
        # 轮询「可以开始了吗」（会话 09 实测：00:21:17 起每 5.02 秒一发）。
        # 这时候才轮到 0x0402 —— 它把客户端切进 stage 7（游戏本体）。
        # 只回第一发：客户端 0x5517f9 有 `cmp [stage+0x54], 7 / je` 的幂等保护，
        # 重复发不会出错，但没必要。
        if opcode == OP_LOADING_DONE and self.state == self.PREPARING:
            self.state = self.IN_GAME
            return [(OP_COUNT_GAME_READY, b"")]

        return []


class RoomStartGame:
    """**房间级**开局握手 —— 里程碑 J.3 的第一块（多人开局链）。

    单人时的包序列和 V0.1 一模一样（下面的 `host` 就是原来那台状态机，
    一个字节都没改），多人时多做两件事：

    1. **房主的应答广播给全房间** —— `0x0401`（倒计时）和 `0x0400`（准备开局，
       **同一个 seed**）如果只发给房主，别人根本不会去加载关卡；
    2. **所有人都加载完才放行** —— `0x0403` 是每个客户端加载到 100% 之后
       每 5 秒一发的轮询（V0.1 会话 09 实测）。收齐所有人的之后才发那一发
       `0x0402` 把大家一起推进 stage 7。谁没加载完就先晾着它继续轮询。

    ★ 只认**房主**发的 `0x0402`。非房主的 `0x0402` 一律忽略：
      开局是房主的权力，认了就等于谁都能替房主开局。
    """

    def __init__(self, seed=0, seed_source=None):
        #: 房主那条状态机。**原样复用**，别在这里重写它的状态迁移。
        #: ★ 种子也在它手里（每局重取一次），房间级的广播直接读 `self.seed`
        #: —— 全房间同一个数就是这么保证的。
        self.host = StartGameHandshake(seed, seed_source)
        #: 已经报过 `0x0403`（关卡加载完）的连接。
        self.loaded = set()
        #: ★ **在「关卡还在加载」这段里走掉的座位号**（§180 / D103）。
        #: 客户端的控制者表是它自己建的，我们不知道它到底在
        #: 「stage 6 加载完」还是「进 stage 7」那一刻建 —— 如果它建得比
        #: 那个人走掉更早，表里就留了一个已经空了的座位，那一局的怪从一开始
        #: 就没人模拟。所以这段时间走掉的人记下来，等真进了关卡（IN_GAME）
        #: 立刻各补一发交接包。客户端那边如果表是后建的（里面本来就没有他），
        #: 那一发就什么都不匹配 = 无害的空操作。
        self.left_while_loading = []

    @property
    def state(self):
        return self.host.state

    def reset(self):
        self.host.reset()
        self.loaded.clear()
        self.left_while_loading.clear()

    def note_left_while_loading(self, seat_index):
        """加载途中有人走了。返回 True = 真记下了（这时确实在加载）。

        「加载中」= `PREPARING`：`0x0400 gspPrepareGame` 已经发出去了
        （那一发就是「切到 stage 6 去加载关卡」的命令），但还没收齐
        所有人的 `0x0403`。倒计时（`WAIT_CONFIRM`）那一段还没有人开始
        加载关卡，谁在那时候走都轮不到控制者表的事。
        """
        if self.state != StartGameHandshake.PREPARING:
            return False
        seat = int(seat_index)
        if seat not in self.left_while_loading:
            self.left_while_loading.append(seat)
        return True

    def on_host_ready(self, opcode, payload):
        """房主发来的 `0x0402` —— 返回要**广播给全房间**的包。"""
        return self.host.on_client_packet(opcode, payload)

    def on_loaded(self, conn, members):
        """某条连接报了 `0x0403`。收齐了就返回要广播的放行包，否则空列表。

        `conn=None` = 没有新的报告，只是**重新算一次**（有人退房时用）。

        `members` 是**调用时**房间里的连接快照 —— 有人中途退房时靠它自动缩小
        「要等谁」的集合，不然剩下的人会永远卡在加载界面。
        """
        if self.host.state != StartGameHandshake.PREPARING:
            return []
        if conn is not None:
            self.loaded.add(conn)
        waiting = [m for m in members if m not in self.loaded]
        if waiting:
            return []
        return self.host.on_client_packet(OP_LOADING_DONE, b"")

    def waiting_for(self, members):
        """还在等谁加载完（只给日志用）。"""
        return [m for m in members if m not in self.loaded]


#: ★★ 对战（房间描述符 type 1）**必须由服务端判胜负并结束**（§167 / §204）。
#:
#: 客户端自带的那套（`GameContextQuest::CheckMatchOver` `0x4a3cf7` ->
#: `IVictoryCondition::vf8` -> 6 秒后发 `0x040f gcpEndQuest`）在对战里
#: **永远跑不起来**：它第一行就是 `cmp [this+0x3b0], 2`，而全镜像里唯一把这个
#: 状态设成 2 的地方（`0x4f7164`）要求 `0x4e71c0([0x72e260]) == 3`，也就是
#: 「地图带剧本」。对战地图没有剧本 -> 状态停在 1 -> 这一局永远不结束。
#: 用户 2026-08-12 报的「分出胜负后无法退出返回房间、死的人无法复活、
#: 倒计时结束也不退出」就是这个 —— 实机日志里整局**一发 `0x040f` 都没有**。
#:
#: 游戏模式是房间描述符的 `arguments[1]`（客户端 `0x409e0a`）。工厂
#: `0x55e0de` 的原版分流是：0 / 2 -> `SurvivalVictoryCondition`，
#: 1 -> `TimeAttackVictoryCondition`，3 -> `DeathMatchVictoryCondition`。
PVP_MODE_SURVIVAL = 0
PVP_MODE_TIME_ATTACK = 1
PVP_MODE_FIGHT = 2
PVP_MODE_DEATHMATCH = 3

#: 生存类构造函数 `0x55e018` 给每个在座角色写死三条命；模式 0 的时限是
#: 240000 ms，模式 2 复用同一个胜负类但时限是 300000 ms（`0x55e2da`）。
PVP_SURVIVAL_LIVES = 3
PVP_SURVIVAL_TIME_LIMIT_MS = 240000
PVP_FIGHT_TIME_LIMIT_MS = 300000

#: 下面这些数抄的是客户端 `DeathMatchVictoryCondition`，判据要和它一致。
#:
#: 时间上限：工厂 `0x55e0de` 对「type 1 + arguments[1] == 3」这一路在
#: `0x55e133` 处 `mov esi, 0x3a980` = 240000 ms，然后 `vf(+0x18)` 存进
#: `[victory+0x188]`。
PVP_TIME_LIMIT_MS = 240000

#: 分数（= 杀敌数）上限 `[victory+0x198]`，由 `0x55be71` 按人数定：
#: 组队战看「人数 // 2」，个人战直接看人数；表里没有的一律 5（构造时的默认值）。
PVP_SCORE_LIMIT_DEFAULT = 5
PVP_SCORE_LIMIT_TEAM = {1: 4, 2: 6, 3: 8}
PVP_SCORE_LIMIT_FREE = {2: 4, 3: 6, 4: 8, 5: 9, 6: 10}

#: 同一只**怪**的两发死亡上报隔多久才算「真的又死了一次」（bug调查/8）。
#:
#: 怪是每台机器各自模拟的，同一次死亡会被好几台几乎同时报上来（实测同一只
#: 怪 76 毫秒内来了两发，而且两发报的「之前死过几次」**不一样** —— 句柄按
#: 控制者座位分段复用，同号对象未必是同一只，`[obj+0x600]` 跨对象残留就差 1，
#: `(句柄, 报的次数)` 那把键当场失效）。怪的重生周期远长于 3 秒，所以这扇窗
#: 只吃重复、不吃真的下一次。
#:
#: ★ **只对怪生效**。玩家的死亡从 bug调查/8 起只认本人上报（`on_report_hp_zero`），
#: 一次死亡天然只有一发，用不着时间窗；而给玩家也套一扇窗只会多一种
#: 「真死亡被吃掉」的可能（服务端补重生之后 3 秒内又死是完全可能的）。
MONSTER_DEATH_DEDUP_WINDOW_S = 3.0

#: 广播完死亡之后，最多等本人的 `0x0413 gcpRespawnCharacter` 多久（秒）；
#: 超时服务端自己补一发 `0x0419` 把人拉起来（bug调查/8「死了不复活」）。
#:
#: 客户端是死后 5 秒发（`0x5019a8` 写 `[char+0x2d8] = now + 5000`），所以这个
#: 值必须**明显大于 5 秒**，不然会抢在正常重生前面；又不能太大，不然玩家
#: 干瞪眼太久。8 秒 = 5 秒倒计时 + 一个 RTT + 掉帧余量。
RESPAWN_WATCHDOG_S = 8.0


def pvp_score_limit(player_count, team_mode):
    """这一局要拿几分（几个人头）才算赢。抄自 `0x55be71`（§167）。

    ★ `player_count` 必须是**开局那一刻**的人数，不是「现在还剩几个人」——
    客户端只在建关卡时算一次（§220）。别直接调它，走
    `RoomQuest.score_limit()`，那里替你拿的是开局快照。
    """
    count = int(player_count)
    if team_mode:
        return PVP_SCORE_LIMIT_TEAM.get(count // 2, PVP_SCORE_LIMIT_DEFAULT)
    return PVP_SCORE_LIMIT_FREE.get(count, PVP_SCORE_LIMIT_DEFAULT)


class RoomQuest:
    """**房间级**的一局关卡状态 —— J.3 的战斗逻辑。

    V0.1 的战斗应答全是「原样回显给发包的那一个人」，多人时那样做等于
    六个人各打各的。这个类把「一局」里必须由服务端**仲裁**或者**广播**的
    东西集中起来。挂在 `lobby.Room.quest` 上，回房间时整个丢掉。

    ## 为什么这些包广播出去是安全的（逐个查过反汇编，§161）

    客户端解这几个包时用的都是**跨机器一致的 id**，不是本机指针：

    | 包 | 客户端怎么找到目标 |
    |---|---|
    | `0x0406` 死亡广播 | `World::Find(句柄)`；玩家角色的句柄 = **座位×100000+100001**（`0x405f02` 写死的公式），六台机器上一模一样 |
    | `0x0419` 重生 | `GameContext::vf_d4(座位…)` -> `[GameSession+座位*4+0x1d0]` |
    | `0x0404` 掉落物落地 | 句柄是**服务端分配**的，我们发给谁谁就用这个号建对象 |
    | `0x0405` 拾取放行 | 座位 -> 角色（`0x404ff6`）、句柄 -> 物件（`World::Find`），两个都查得到才生效 |
    | `0x0415` 分数 | `[GameContextQuest + 座位*4 + 0x3b8]` |
    | `0x0309` 结算 | 全部按 `pkt+0x04` 的座位号索引 |

    找不到就整包丢掉（`0x493914` / `0x551d7c` 都有 `test/je`），**不会崩**。

    ## 服务端在这里必须仲裁的两件事

    1. **一件掉落物只能被一个人捡到。** 客户端 `Character::CheckItemPickup`
       （`0x5154d3`）发完 `0x0407` 就把 `[item+0x2a8]` 置 1，它自己不会再问
       第二次；谁先到我们这儿谁拿走，晚到的那一发**不回任何包**。
    2. **同一个角色的死亡只广播一次。** 多台机器各自模拟怪物，同一只怪
       可能被两台机器同时报 `0x0408`；广播两遍等于战绩表多记一次死亡。

    ## 句柄分配器为什么必须是房间级的

    `0x0404` 的句柄进客户端的 `World` 当 map 的 key（`0x473e7c`）。六个人
    各自从 `ITEM_HANDLE_BASE` 开始分配的话，A 的第 1 件和 B 的第 1 件是同一个
    号，后到的那件会**覆盖**先到的那件。
    """

    def __init__(self, item_handle_base=ITEM_HANDLE_BASE, seats=()):
        #: 掉落物句柄分配器（房间级，见类注释）。
        self.next_item_handle = int(item_handle_base)
        #: 已经被谁捡走的掉落物句柄 -> 座位号。仲裁靠它。
        self.items_taken = {}
        #: ★ **道具模式**：服务端刷在地图上、还没被人捡走的道具句柄（§191）。
        #: 只用来卡「地图上最多同时躺几件」，捡走了就从这里去掉。
        self.items_on_map = set()
        #: 本局下发过的每一件 `0x0404` 的「句柄 -> 物件 id」（服务端刷的和
        #: 客户端掉的都记）。★ 拾取放行时**只有靠它才知道捡到的是什么** ——
        #: `0x0407` 只带句柄，而要不要补一发 `0x040b` 完全取决于物件类型（§194）。
        self.item_handles = {}
        #: ★ 每个座位**本局在地上捡到的金币总额**（§230）。结算时原样加进
        #: 「金币 +N」——闯关里怪和 boss 掉的、对战里偶尔掉的，都算这一份。
        #: 每局随 `RoomQuest` 重建，换图不清零（一整轮算一份）。
        self.coins = [0] * ROOM_SEAT_COUNT
        #: 每个座位手上的道具 id（FIFO，最多 `ITEM_SLOT_COUNT` 件）。
        #: 这是客户端 `[Character+0x764..0x770]` 那 4 格的**镜像**：
        #: 是我们发 `0x040b` 把它填进去的，也只有我们发 `0x040c` 能拿掉，
        #: 所以两边天然同步。按 Ctrl 的 `0x040c` 只带槽位序号不带 id，
        #: **要发 `0x040a` 就必须靠这份镜像把 id 找回来**。
        self.item_slots = [[] for _ in range(ROOM_SEAT_COUNT)]
        #: 已经广播过的死亡事件，键是 **(句柄, 客户端报的死亡次数)**（去重）。
        #: 为什么键里要带死亡次数：同一个角色会死很多次，只按句柄去重的话
        #: 第二次死就被吃掉了。而重复上报（两台机器同时判同一只怪死了）
        #: 两发的死亡次数**通常**相同 —— 那一格是 `[char+0x600]`，只由我们
        #: 广播的 `0x0406` 写。真正的第二次死亡则会带着更大的数上来。
        #:
        #: ★ bug调查/8 之后这张表只是最便宜的第一道闸：实测同一只怪的两发
        #: 上报**次数可以不一样**（句柄分段复用，见 `MONSTER_DEATH_DEDUP_WINDOW_S`），
        #: 那种情况靠下面的权威计数 + 时间窗拦。
        self.dead_events = set()
        #: 每个句柄**服务端权威**的已广播死亡次数。下发值以它为准，不再直接
        #: 用客户端报的「之前死过几次」+1 —— 正常路径两者恒等（`[char+0x600]`
        #: 只由我们的广播写），不等就是句柄撞号 / 跨对象残留，这时候跟着客户端
        #: 跳会把心形一次扣好几颗。
        self.death_counts = {}
        #: 每个句柄最后一次广播死亡的时刻（`time.monotonic()`），
        #: 给 `MONSTER_DEATH_DEDUP_WINDOW_S` 那扇窗用。
        self.last_death_broadcast_at = {}
        #: 见过的**玩家**角色句柄 -> 座位。句柄的公式是「座位×100000+100001」
        #: （`0x405f02`），但我们不去算它 —— 谁是玩家由 `0x0408` 里的座位号
        #: 说了算，这样公式改了也不会跟着错。
        #: 唯一的用处是换图时把玩家那几份死亡计数留下来（`begin_map_change`）。
        self.player_handles = {}
        #: ★ 重生看门狗（bug调查/8）：座位 -> `(到点时刻, 兜底坐标)`。
        #: 广播死亡时上闩，收到本人的 `0x0413` 就撤闩；到点还没撤说明客户端
        #: 那条「5 秒后自己发 0x0413」的链断了，服务端自己补一发 `0x0419`。
        self.respawn_due = {}
        #: 客户端自报过的重生点：座位 -> `(x, y, 重生点索引)`。看门狗补重生时
        #: 优先用本人上次用过的那个点，其次用**任何人**在这张图上用过的点。
        #: 换图必须清 —— 换了图重生点表整个换了。
        self.respawn_hints = {}
        #: 这张图上最近一次有人自报的重生点（座位不限），看门狗的第二顺位。
        self.last_respawn_hint = None
        #: 每个座位死了几次。**这是权威值** —— HUD 上那排心形读的就是它
        #: （§109：`[char+0x600]`，由我们在 `0x0406` 里下发）。
        self.deaths = [0] * ROOM_SEAT_COUNT
        #: 本局是否通关。任何人报了 `0x0417 gcpMarkQuestSuccess(1)` 就算 ——
        #: 合作模式里关底是大家一起打的，谁的脚本先喊到不重要。
        self.success = False
        #: 正在换的那张地图名（`0x0411` -> `0x0417`），没有换图在飞时是 None。
        self.pending_map = None
        #: 已经报过 `0x0412`（新图加载完）的连接。
        self.map_loaded = set()
        #: 本局已经结算过了。★ 房间级，不是连接级 —— 六个人会各发一发
        #: `0x040f gcpEndQuest`，只有第一发能触发结算。
        self.settled = False
        #: 已放行的地图名（只给日志和调试通道看）。
        self.maps_entered = []
        #: 本局开打的时刻（`time.monotonic()`）。对战的时间上限从这里算（§167）。
        self.started_at = time.monotonic()
        #: 下一次该往地图上刷道具的时刻（道具模式才用，见 `due_item_spawn`）。
        self.next_item_spawn_at = self.started_at + ITEM_SPAWN_FIRST_DELAY
        #: 每个座位杀了几个人。`0x0408` 里的「凶手」字段就是杀人者的座位号
        #: （`[char+0x158]`，由 `0x4fedee` 写成开火者的座位），所以服务端不用
        #: 客户端另外上报分数就能数出对战成绩（§167）。
        self.kills = [0] * ROOM_SEAT_COUNT
        #: 对战已经判过胜负了（只判一次，日志里也只写一行）。
        self.pvp_reason = None
        #: ★ **开局那一刻**在座的座位号（升序）。§220：客户端所有「按人数
        #: 定死的常量」都在建 `GameContextQuest` 那一刻算一次就再也不动了 ——
        #: 夺分模式右上角那个「MAX N」（胜利分数线 `[victory+0x198]`）
        #: 全镜像里只有构造函数 `0x55be71` 写过，**没有任何包能改它**。
        #: 所以中途有人掉线时服务端也必须继续用这份快照算胜利线，
        #: 否则线悄悄下移、HUD 上那个数字却纹丝不动（D139）。
        #:
        #: 「只剩一边了」那一条**不**用它 —— 那条要的就是「现在还剩几个人」。
        self.start_seats = sorted({int(seat) for seat in seats
                                   if 0 <= int(seat) < ROOM_SEAT_COUNT})
        #: ★ 客户端那张控制者表（句柄类别 20~25 那 6 格）的**镜像**，§180 / D103。
        #: 值是座位号。**权威在客户端** —— 它自己算初值、自己应用我们发的替换。
        #: 这份镜像只用来回答两个问题：「走的人到底欠着控制权吗」、
        #: 「交给谁最划算」。哪怕它和客户端错开，我们挑出来的接管者仍然是一个
        #: 在座的座位，客户端照样接得过去 —— 错开的代价只是负载不均。
        #: **不要拿它当权威去做别的判断。**
        self.controllers = [0] * CONTROLLER_SLOT_COUNT
        self.assign_controllers(seats)

    # -- 控制权（怪 / 刷怪点归谁模拟，§180）----------------------------------
    def assign_controllers(self, seats):
        """按客户端 `GameContext::StartGame` 的公式算初值（§180）。

            for (i = 0; i < 6; i++)
                [ctx+0x294+i*4] = 在座座位[i % 在座人数]     // 一个人都没有 -> 0

        `seats` = 开局那一刻**在座的座位号**（升序，和客户端的
        `0x4045f9` 扫 0..5 同一个口径）。
        """
        seats = [int(s) for s in seats if 0 <= int(s) < ROOM_SEAT_COUNT]
        if not seats:
            self.controllers = [0] * CONTROLLER_SLOT_COUNT
            return list(self.controllers)
        self.controllers = [seats[i % len(seats)]
                            for i in range(CONTROLLER_SLOT_COUNT)]
        return list(self.controllers)

    def controller_load(self, seats=None):
        """每个座位现在扛着几格控制权。`seats` 给了就只统计这些座位。"""
        load = {int(s): 0 for s in (seats if seats is not None else [])}
        for owner in self.controllers:
            if seats is None or owner in load:
                load[owner] = load.get(owner, 0) + 1
        return load

    def handover_controller(self, leaver_seat, survivors, force=False):
        """走的人那几格控制权交给还在的人。返回接管者座位号，没事干就 ``None``。

        接管者 = 还在座的人里**当前扛得最少**的那个，并列取座位号最小的
        （D103：六人房里房主走了之后，把他那几格全压给新房主等于让新房主
        一台机器跑两倍的怪）。

        走的人一格都不占（或者没人接得住）时返回 ``None`` —— 这时**一个包都
        不该发**，客户端那张表本来就是对的。

        `force=True` 只给「关卡加载途中走的人」用：**镜像里必然没有他**
        （这一局的镜像是他走之后才建的），可客户端那张表**可能有** ——
        那时必须照发，让客户端自己去匹配（匹配不上就是无害的空操作）。
        """
        leaver = int(leaver_seat)
        survivors = sorted({int(s) for s in survivors
                            if 0 <= int(s) < ROOM_SEAT_COUNT} - {leaver})
        if not survivors or (leaver not in self.controllers and not force):
            return None
        load = self.controller_load(survivors)
        heir = min(survivors, key=lambda seat: (load[seat], seat))
        self.controllers = [heir if owner == leaver else owner
                            for owner in self.controllers]
        return heir

    # -- 掉落物 -------------------------------------------------------------
    def allocate_item(self):
        """给一件新的掉落物分配句柄。"""
        handle = self.next_item_handle
        self.next_item_handle += 1
        return handle

    def remember_item(self, handle, item_id):
        """记下「这个句柄上躺的是哪件东西」（§194）。

        服务端刷的道具和客户端掉的金币**都要记** —— 拾取请求 `0x0407`
        只带句柄，捡到之后要不要补一发 `0x040b` 全靠这张表反查物件类型。
        """
        self.item_handles[handle & 0xFFFFFFFF] = int(item_id)

    def item_id_of(self, handle):
        """这个句柄上是哪件东西？没记过就是 ``None``（当成不进道具槽处理）。"""
        return self.item_handles.get(handle & 0xFFFFFFFF)

    def claim_item(self, handle, seat_id):
        """拾取仲裁。第一个来的返回 True，之后一律 False。

        返回 False 时**什么包都不要回** —— 回 `0x0405` 就等于同一件东西
        被两个人捡走了。
        """
        handle &= 0xFFFFFFFF
        if handle in self.items_taken:
            return False
        self.items_taken[handle] = int(seat_id)
        # 服务端刷的那件被人捡走了，地图上就少一件（配额腾出来）。
        self.items_on_map.discard(handle)
        # ★ 金币在**这里**入账，不在 `grant_picked_item` 里 —— 那个函数只管
        #   「要不要补一发 0x040b」，金币根本不进道具槽、会被它提前 return 掉。
        #   仲裁这一步才是「这一件确定归这个座位了」的唯一判定点。
        self.add_coins(seat_id, coin_value(self.item_id_of(handle)))
        return True

    def add_coins(self, seat_id, amount):
        """把捡到的金币记到这个座位头上。返回它本局的金币总额。"""
        seat = int(seat_id)
        if not 0 <= seat < ROOM_SEAT_COUNT or amount <= 0:
            return 0
        self.coins[seat] += int(amount)
        return self.coins[seat]

    def coins_of(self, seat_id):
        """这个座位本局捡了多少金币。座位越界按 0 算。"""
        seat = int(seat_id)
        return self.coins[seat] if 0 <= seat < ROOM_SEAT_COUNT else 0

    # -- 道具槽（§194）------------------------------------------------------
    def grant_item(self, seat_id, item_id):
        """把一件道具记进这个座位的槽。放得下返回 True，满了返回 False。

        满了要返回 False 是**硬要求**：客户端 `AddItem`（`0x517037`）扫不到
        空槽就**整个函数什么都不做**，我们这边却记上了的话，之后按 Ctrl
        就会用出一件客户端根本没有的道具（效果照样生效，但那是凭空变的）。
        """
        seat = int(seat_id)
        if not 0 <= seat < ROOM_SEAT_COUNT:
            return False
        slots = self.item_slots[seat]
        if len(slots) >= ITEM_SLOT_COUNT:
            return False
        slots.append(int(item_id))
        return True

    def use_item(self, seat_id, slot_index):
        """用掉这个座位第 N 格的道具。返回物件 id；那一格是空的就 ``None``。

        取走之后后面的往前挪 —— 和客户端 `RemoveItem`（`0x5170b4`）里那段
        「`[eax] = [eax+4]` 挪三次、末格清 0」是同一个语义，所以下一件
        永远在第 0 格（客户端也正是恒发 0）。
        """
        seat = int(seat_id)
        if not 0 <= seat < ROOM_SEAT_COUNT:
            return None
        slots = self.item_slots[seat]
        index = int(slot_index)
        if not 0 <= index < len(slots):
            return None
        return slots.pop(index)

    # -- 道具模式：往地图上刷道具（§191 / D109）------------------------------
    def due_item_spawn(self, now=None, team_mode=False, random_source=None):
        """现在该不该刷一件道具？该刷就返回 `(句柄, 物件 id, X, Y)`，否则 ``None``。

        ★ **调用点是每收到一发 `0x040e` 就问一次**（约 8 Hz，和
        `check_pvp_finished` 同一个套路）—— 服务端没有定时器线程，
        而「房里没人发包」正好等于「没人在打」，那时也不需要刷。
        房间级状态，所以六条连接抢着问也只会刷出一件。

        坐标是随便给的正数：客户端会 `fmod` 进地图、再把埋在地形里的物件
        顶到地面上（§192），服务端不需要任何地图几何数据。

        抽签的池子见 `item_spawn_pool()` —— 除了箱子道具，还有三把
        **捡了当场换枪**的特殊武器（核弹发射器 / 火焰喷射器 / 水炮，§223）。
        """
        now = time.monotonic() if now is None else now
        if now < self.next_item_spawn_at:
            return None
        # 先把下一次的时刻推掉，再决定这一次刷不刷 —— 配额满的时候
        # 也要正常走时钟，不然配额一空就会连着补刷一堆。
        self.next_item_spawn_at = now + ITEM_SPAWN_INTERVAL
        if len(self.items_on_map) >= ITEM_SPAWN_MAX_ALIVE:
            return None
        rng = random_source if random_source is not None else random
        pool = item_spawn_pool(team_mode)
        item_id = rng.choice(pool)
        x = rng.uniform(*ITEM_SPAWN_X_RANGE)
        y = rng.uniform(*ITEM_SPAWN_Y_RANGE)
        handle = self.allocate_item() & 0xFFFFFFFF
        self.items_on_map.add(handle)
        # ★ 记进「句柄 -> 物件 id」表：捡起来之后要靠它才知道该不该补
        # 一发 `0x040b` 把道具塞进槽（§194）。记在这里而不是调用方，
        # 是为了「刷了一件却忘了记」这种漂移压根发生不了。
        self.remember_item(handle, item_id)
        return handle, item_id, x, y

    # -- 死亡 / 重生 --------------------------------------------------------
    def record_death(self, handle, seat, reported, now=None):
        """记一次死亡。返回 `(要下发的死亡次数, 这一发要不要广播)`。

        ## 判重三层（bug调查/8 重写）

        1. `(句柄, 报的次数)` 见过了 —— 最便宜的一条，挡住「同一发重复上报」
           和「换图瞬间还在飞的旧上报」。
        2. **权威计数**：`报的值 + 1 <= 这个句柄已经广播过的次数` 一律不广播。
           重复上报、以及那台机器计数落后（句柄撞号 / 跨对象残留）都长这样。
        3. **时间窗**（`MONSTER_DEATH_DEDUP_WINDOW_S`，★ **只对怪**）：
           怪由每台机器各自模拟，同一次死亡会被几台几乎同时报上来，而且
           两发报的次数**可以不一样**，前两层都拦不住。玩家不走这一层 ——
           玩家的死亡只认本人上报（`Conn.on_report_hp_zero`），一次死亡天然
           只有一发，加窗只会平白吃掉真死亡。

        ★ **下发的死亡次数以服务端权威计数为准。** V0.1 §109 的契约不变：
        `[char+0x600]` 只由我们广播的 `0x0406` 写，所以正常路径下权威计数
        恒等于「客户端报的值 + 1」。两者不等时**以我们的为准** —— 客户端
        报的值更大只可能是句柄撞号，跟着跳会把 HUD 心形一次扣好几颗。

        每座位的 `deaths` 镜像成**这一发实际下发的次数**；生存模式拿它算
        `3 - deaths`。用 ``max`` 而不是直接赋值只是保险 —— 换图**不清**玩家
        那几份计数（bug调查/12，见 `begin_map_change`），正常路径上两者同步。
        """
        handle = int(handle) & 0xFFFFFFFF
        reported = int(reported)
        seat = int(seat)
        now = time.monotonic() if now is None else now
        is_player = 0 <= seat < ROOM_SEAT_COUNT
        if is_player:
            # 换图要靠它认出「这个句柄是玩家的」（`begin_map_change`）。
            # 记在判重之前 —— 被判重吃掉的那一发同样证明了句柄归属。
            self.player_handles[handle] = seat
        count = self.death_counts.get(handle, 0)
        key = (handle, reported)
        if key in self.dead_events:
            return count, False
        if reported + 1 <= count:
            return count, False
        if not is_player:
            last = self.last_death_broadcast_at.get(handle)
            if last is not None and now - last < MONSTER_DEATH_DEDUP_WINDOW_S:
                return count, False
        count += 1
        self.death_counts[handle] = count
        self.last_death_broadcast_at[handle] = now
        self.dead_events.add(key)
        if is_player:
            self.deaths[seat] = max(self.deaths[seat], count)
        return count, True

    # -- 重生看门狗（bug调查/8）---------------------------------------------
    def arm_respawn_watchdog(self, seat, position, now=None, after=None):
        """广播了 `seat` 的死亡 -> 上闩，等他自己的 `0x0413`。

        `position` = 死亡地点（`0x0408` 自报的 float 坐标），实在找不到重生点
        时拿它当兜底 —— 原地站起来虽然不是原版行为，但比一直躺着强。
        `after` = 等多少秒（`--respawn-watchdog`）；**<= 0 就是不上闩**，
        整个兜底关掉，回到 bug调查/8 之前的行为（留取证窗口用）。
        """
        after = RESPAWN_WATCHDOG_S if after is None else float(after)
        if after <= 0 or not 0 <= int(seat) < ROOM_SEAT_COUNT:
            return False
        now = time.monotonic() if now is None else now
        x, y = position
        self.respawn_due[int(seat)] = (now + after, (int(x), int(y)))
        return True

    def disarm_respawn_watchdog(self, seat):
        """本人的 `0x0413` 到了（或者他走了 / 这局结束了）-> 撤闩。"""
        return self.respawn_due.pop(int(seat), None) is not None

    def remember_respawn_point(self, seat, x, y, spawn_index):
        """记下客户端自报的重生点，给看门狗补包时用。"""
        point = (int(x), int(y), int(spawn_index))
        if 0 <= int(seat) < ROOM_SEAT_COUNT:
            self.respawn_hints[int(seat)] = point
        self.last_respawn_hint = point
        return point

    def respawn_point_for(self, seat, fallback):
        """看门狗要发的 `0x0419` 用哪个坐标。

        优先级：本人上次用过的重生点 -> 这张图上任何人用过的 -> 死亡地点。
        前两者都是客户端自己算出来的合法重生点（`0x4fe70e` 选的
        `[char+0x2b0]`），重生点表是**整张图共用**的，所以借别人的那个
        一样落在地图内、不会触发 §88 那种「传送到地图边缘 + 0x0106」。
        """
        point = self.respawn_hints.get(int(seat)) or self.last_respawn_hint
        if point is not None:
            return point
        x, y = fallback
        return (int(x), int(y), 0)

    def due_respawns(self, now=None):
        """到点还没等到 `0x0413` 的座位，按座位号升序返回 `(座位, 兜底坐标)`。

        ★ **只报，不撤闩** —— 撤闩交给调用方，它还要看这个座位到底该不该
        重生（生存模式命用完了就不该）。
        """
        now = time.monotonic() if now is None else now
        return [(seat, position)
                for seat, (deadline, position) in sorted(self.respawn_due.items())
                if now >= deadline]

    def record_kill(self, killer_seat, victim_seat, *,
                    teams=None, team_mode=False):
        """按客户端 `Character::Die` 的口径给凶手加分 / **扣分**（§224）。

        返回这一发让凶手的分数**变了多少**（`+1` / `-1` / `0`）。

        `killer_seat` 来自 `0x0408` 的第二个字节（`[char+0x158]`）：
        **开火者的座位号**（`0x4fedee` 写的），怪物 / 环境是 0xff，
        自杀是自己的座位号。

        ★★ **自杀和杀队友要扣一分，不是「不记分」**（§224）。这是照抄
        `Character::Die`（`0x4ffbb7`）—— 那个函数拿凶手座位查角色
        （`0x404ff6`，座位没人就是 NULL、一分不动），然后三选一：

        | 情况 | 客户端 | 服务端必须一样 |
        |---|---|---|
        | 凶手座位 == 受害者座位（`0x4fff15`）| `OnSuicide`（`0x506eba`）| **-1** |
        | 组队战且两人同队（`0x500165`：先问 `0x409df1(描述符+0x18)==1`，再比队伍号）| 同上 | **-1** |
        | 其余 | `OnKill`（`0x506e8c`）| **+1** |

        `OnKill` / `OnSuicide` 干的是同两件事：改 `[char+0x604]`（**HUD 上
        那个杀敌数就是它**，`0x497536` 每帧读来画）和
        `GameContext::AddScore(座位, ±1)`（虚表槽 `+0x100` -> `0x48c98f`，
        写 `[victory + 座位*0x2c + 0x5c]` —— 夺分胜负线 `0x55bf20` 读的那一格）。
        两个数是一起动的，所以**玩家看见的数字和胜负判据永远相等**。

        ★ 扣分有下限：`0x506eba` 开头 `test ecx,ecx / jle` —— 杀敌数
        已经是 0 就整个函数什么都不做（**AddScore 也不调**），所以
        扣不出负数，两边一起停在 0。

        `teams` / `team_mode` 不给（协议试探 / 控制通道手搓包那条路，
        那时压根没有房间）就只判自杀，行为和 §224 之前一个字节不差。
        """
        killer = int(killer_seat)
        victim = int(victim_seat)
        if not 0 <= killer < ROOM_SEAT_COUNT:
            # 怪 / 环境（0xff）。客户端 `0x404ff6` 查不到角色，一分不动。
            return 0
        if not 0 <= victim < ROOM_SEAT_COUNT:
            return 0
        if teams is not None and killer not in teams:
            # 凶手已经退房了 —— 客户端那边 `0x4045f9` 判定这个座位没人，
            # `0x404ff6` 返回 NULL，加分扣分都不会发生。
            return 0
        penalty = killer == victim
        if not penalty and team_mode and teams is not None:
            penalty = (int(teams.get(killer, TEAM_NONE)) ==
                       int(teams.get(victim, TEAM_NONE)))
        if penalty:
            if self.kills[killer] <= 0:
                return 0
            self.kills[killer] -= 1
            return -1
        self.kills[killer] += 1
        return 1

    def score_limit(self, seats, team_mode):
        """夺分模式这一局要拿几分才算赢 —— **客户端右上角那个「MAX N」**。

        ★ 按**开局那一刻**的人数算，不按「现在还剩几个人」（§220 / D139）。

        客户端的 `DeathMatchVictoryCondition` 是建 `GameContextQuest` 时
        一次性造出来的（`0x4a36af` 调工厂 `0x55e0de`），构造函数
        `0x55be71` 当场扫一遍六个座位、把分数线写进 `[victory+0x198]`。
        全镜像里写这一格的只有那个构造函数，`[quest+0x384]` 那个指针也只在
        构造函数里赋值 —— **中途没有任何包能让客户端改这个数**，
        而 HUD（`0x497dec` 走 vtable `+0x24` 取这一格，配 `0x671a48 "MAX"`
        和 `0x65e0b0 "%d"`）每帧都照它画。

        所以三人个人战开打后掉线一个，客户端仍然认 6 分；服务端要是跟着
        `len(seats)` 掉到 2 人份的 4 分，就会在 HUD 写着「MAX 6」的时候
        按 4 分结算 —— 用户报的正是这个。

        `start_seats` 为空的只有「协议试探 / 控制通道手搓包」建出来的那份
        （`RoomQuest()` 不带座位），那条路退回按现在的人数算，行为一个字节不变。
        """
        count = len(self.start_seats) or len(seats)
        return pvp_score_limit(count, team_mode)

    def pvp_finished(self, seats, teams, score_limit, *, team_mode=True,
                     time_limit_ms=PVP_TIME_LIMIT_MS, now=None):
        """夺分模式这一局该不该结束了？结束就返回一句人话，否则 ``None``。

        ★ **这三条是照抄客户端 `DeathMatchVictoryCondition::vf8`（`0x55bf20`）**
        （§167）—— 客户端自己那套永远跑不起来（它要求
        `GameContextQuest.state == 2`，而那个状态只有剧本关才会进），
        所以判胜负这件事只能由服务端做，判据必须和客户端的口径一致，
        不然玩家看到的「差一个人头」和服务端算的对不上。

            ① 打到时间上限（对战默认 4 分钟 = 240000 ms，
               来自工厂 `0x55e133` 里的 `mov esi, 0x3a980`）
            ② 有人的杀敌数 >= 分数上限（`[victory+0x198]`，见 `pvp_score_limit`）
            ③ 只剩一边了（`0x55c594`：组队模式下在座的人全是同一队，
               非组队模式下在座不足两人）

        `seats` = 有人的座位号列表，`teams` = `{座位号: 队伍号}`。

        ★ `team_mode=False` 时**不看队伍号**，只看在座人数 —— 和客户端
        `0x55c594` 一致：那个函数开头 `0x409df1(描述符) == 1` 不成立就直接
        跳到非组队分支（`0x55c5d1`），一格队伍号都不读。以前个人战靠
        「每人一队」让 `sides` 天然 >= 2 才没露馅，而个人战现在按
        `lobby.default_team` 一律发 0（那里说明了为什么必须这样），
        再不分模式就会一开局就判「只剩一边」。
        """
        if not seats:
            return None
        now = time.monotonic() if now is None else now
        elapsed_ms = (now - self.started_at) * 1000.0
        if elapsed_ms > time_limit_ms:
            return f"时间到（{elapsed_ms / 1000:.0f} 秒 > {time_limit_ms / 1000:.0f} 秒）"
        for seat in seats:
            if 0 <= seat < ROOM_SEAT_COUNT and self.kills[seat] >= score_limit:
                return f"座位 {seat} 拿到 {self.kills[seat]} 分，达到上限 {score_limit}"
        if len(seats) < 2:
            return "只剩一边了"
        if team_mode:
            sides = {teams.get(seat, TEAM_NONE) for seat in seats}
            if len(sides) < 2:
                return "只剩一边了"
        return None

    def remaining_lives(self, seat, max_lives=PVP_SURVIVAL_LIVES):
        """生存模式里这个座位还剩几条命；死亡数超出上限也只返回 0。"""
        seat = int(seat)
        if not 0 <= seat < ROOM_SEAT_COUNT:
            return 0
        return max(0, int(max_lives) - int(self.deaths[seat]))

    def survival_finished(self, seats, teams, *, team_mode,
                          time_limit_ms=PVP_SURVIVAL_TIME_LIMIT_MS, now=None):
        """照原版 `SurvivalVictoryCondition::vf8`（`0x55db6f`）判结束。

        组队战逐队看：队里**任意**一名成员还有生命，这队就还活着；两队中
        至多只剩一队时结束。个人战则在至多只剩一名玩家有生命时结束。
        两条都先判淘汰、再判时间，和原函数的分支顺序一致（§204）。
        """
        seats = [int(seat) for seat in seats
                 if 0 <= int(seat) < ROOM_SEAT_COUNT]
        if not seats:
            return None

        if team_mode:
            living_teams = []
            eliminated_teams = []
            for team in (TEAM_A, TEAM_B):
                members = [seat for seat in seats
                           if int(teams.get(seat, TEAM_NONE)) == team]
                if not members:
                    continue
                if any(self.remaining_lives(seat) > 0 for seat in members):
                    living_teams.append(team)
                else:
                    eliminated_teams.append(team)
            if len(living_teams) <= 1:
                if len(eliminated_teams) == 1:
                    team = eliminated_teams[0]
                    return (f"队伍 {team} 所有成员的 {PVP_SURVIVAL_LIVES} 条"
                            "生命都用完了")
                if len(eliminated_teams) > 1:
                    return "两队所有成员的生命都用完了"
                if living_teams:
                    return f"只剩队伍 {living_teams[0]} 还有生命"
                return "已经没有存活队伍了"
        else:
            living = [seat for seat in seats if self.remaining_lives(seat) > 0]
            if len(living) <= 1:
                if living:
                    return f"只剩座位 {living[0]} 还有生命"
                return "所有玩家的生命都用完了"

        now = time.monotonic() if now is None else now
        elapsed_ms = (now - self.started_at) * 1000.0
        if elapsed_ms > time_limit_ms:
            return f"时间到（{elapsed_ms / 1000:.0f} 秒 > {time_limit_ms / 1000:.0f} 秒）"
        return None

    def survival_ranking(self, seats, teams, *, team_mode):
        """生存模式的 `0x0309` 胜负尾数组，按剩余生命而不是杀敌数算。"""
        tail = [0] * GAME_RESULT_TAIL_COUNT
        seats = [int(seat) for seat in seats
                 if 0 <= int(seat) < GAME_RESULT_TAIL_COUNT]
        if not seats:
            return tail

        if not team_mode:
            # 原版 `0x55de78`：还有生命就是 +1，耗尽就是 -1。
            for seat in seats:
                tail[seat] = (GAME_RESULT_CLEARED
                              if self.remaining_lives(seat) > 0
                              else GAME_RESULT_DEFEATED)
            return tail

        totals = {}
        for team in (TEAM_A, TEAM_B):
            members = [seat for seat in seats
                       if int(teams.get(seat, TEAM_NONE)) == team]
            if members:
                totals[team] = sum(self.remaining_lives(seat) for seat in members)
        if not totals:
            return tail
        for seat in seats:
            team = int(teams.get(seat, TEAM_NONE))
            if team in totals:
                # 原版 `0x55de5d` -> `0x498ef0`：本队总剩余生命 > 0
                # 就是 +1，否则 -1。时间到时两队都还有命 = 双方都是 +1。
                tail[seat] = (GAME_RESULT_CLEARED if totals[team] > 0
                              else GAME_RESULT_DEFEATED)
        return tail

    # -- 换图 ---------------------------------------------------------------
    def begin_map_change(self, map_name):
        """开一次换图。已经在换同一张图时返回 False（别广播第二遍）。

        两个玩家同时走到地图边缘就会各发一发 `0x0411`。第二发再广播一次
        `0x0417` 的话，先收到的人会被要求**再卸一次场景**。
        """
        if self.pending_map == map_name:
            return False
        self.pending_map = map_name
        self.map_loaded.clear()
        self.maps_entered.append(map_name)
        # 换图会把场景里的物件全部卸掉重建（`0x47900a`），旧句柄随之作废 ——
        # 去重表和拾取表必须跟着清，否则新图里的物件会被当成「已经捡过了」。
        # 重生点表也必须清 —— 换了图，旧坐标就是别的地方了。
        #
        # ★★ **玩家那几份死亡计数不能清**（bug调查/12）。`0x47900a` 对六个座位
        # 的角色**不是**卸掉重建：它先把 `0x404ff6(座位)` 拿到的指针存进栈上
        # 的数组，卸完场景之后又把**同一批指针**原样挂回世界（`0x473e7c`），
        # 连 `[[0x72e2d4]+0x3c]` 起的 30 个 dword 都是**按 `max` 合并**回去的
        # （`0x4790fc`）—— 换图根本不是一次干净的重置。
        # 所以 `[char+0x600]`（= HUD 心形的数据源）跨图**照旧累计** ——
        # 心形本来就得跨图记账。跟着清的话服务端从 0 重新数，会连出两个 bug：
        #   · 第二张图第一次死，客户端报「我死过 1 次」，服务端回「你死了
        #     1 次」，心形原地不动（用户报的「换图后再死一次还是 2 颗心」）；
        #   · 再死一次，客户端报的还是 1，`(句柄, 报的次数)` 撞上上一发的老键，
        #     整发被当成重复上报吃掉 —— 死亡广播不发，客户端就永远不会调
        #     `Character::Die()`，人躺在地上再也起不来（「被打死后无法复活」）。
        keep = self.player_handles
        self.items_taken.clear()
        self.dead_events = {key for key in self.dead_events if key[0] in keep}
        self.death_counts = {handle: count
                             for handle, count in self.death_counts.items()
                             if handle in keep}
        self.last_death_broadcast_at = {
            handle: at for handle, at in self.last_death_broadcast_at.items()
            if handle in keep}
        self.respawn_due.clear()
        self.respawn_hints.clear()
        self.last_respawn_hint = None
        return True

    def map_done(self, conn, members):
        """某条连接报了 `0x0412`。所有人都报到了才返回 True（放行 `0x0418`）。

        `conn=None` = 只重新算一次（有人中途退房时用），和
        `RoomStartGame.on_loaded` 是同一个套路：等的人走了就别再等他。
        """
        if conn is not None:
            self.map_loaded.add(conn)
        return not [m for m in members if m not in self.map_loaded]

    def finish_map_change(self):
        self.pending_map = None
        self.map_loaded.clear()

    def waiting_for_map(self, members):
        """还在等谁把新图加载完（只给日志用）。"""
        return [m for m in members if m not in self.map_loaded]

    # -- 通关 / 胜负 --------------------------------------------------------
    def mark_success(self, ok):
        """`0x0417 gcpMarkQuestSuccess`。只会从 False 变 True，不会被冲回去。"""
        if ok:
            self.success = True
        return self.success

    def ranking(self, scores, quest_mode, teams=None, *, team_mode=False):
        """算每个座位在 `0x0309` 尾部数组里的那一格。

        `scores` = `{座位号: 本局分数}`，只包含**有人的**座位。
        `teams`  = `{座位号: 队伍号}`（`lobby.TEAM_A` / `TEAM_B` / `TEAM_NONE`），
        只在 `team_mode` 为真时用得上。

        尾部数组落进 `[GameContext + 座位*4 + 0x184]`，客户端读它的地方有两处
        （§112 + §161）：

            结算界面标签 `0x4a4ba9`：== 1  且 剩余生命 > 0  -> 「完成」/「CLEAR」
            结算 BGM     `0x55223f`：>= 0（`setge`）        -> 胜利曲，否则失败曲

        所以三个档：**1 = 赢**、**0 = 没赢但不放失败曲**（V0.1 单机没通关时
        就是这一档，保持不变）、**-1 = 输**（对战的败方）。

        `quest_mode` 为真（房间类型 2 = 闯关）时是**合作**：通关了大家一起 1。

        ★ 对战这一路**照抄原版客户端** `DeathMatchVictoryCondition` 虚表槽 14
        （`0x55bfda`，§226）—— 我们判胜负是因为客户端那套跑不到（§167），
        但口径必须和它一致，否则玩家的直觉和屏幕对不上：

            座位没人                                  -> 0
            个人战：合成分 = (分数 + 1) * 1000 - 死亡数
                    全员并列                          -> 0（谁都不判）
                    合成分 == 最大（含并列）           -> +1，其余 -1
            组队战：在座的人全同队（`0x55c594`）       -> +1（没有对手，全员胜）
                    队伍合成分 = (队伍总分 + 1) * 100 - 队伍总死亡
                    我队 > 敌队 -> +1；相等 -> 0；小于 -> -1

        ⚠ 原版的队伍合成分还有一个更高优先级的项（`(c*100 + 总分 + 1)*100 - 总死亡`
        里的 `c` = 队伍记录 `+0x0c`），夺分里它恒 0，我们不实现 —— 真要用到它的
        玩法（回合制）本来就没上线。这里等价成「先比队伍总分，平了比谁死得少」。
        """
        tail = [0] * GAME_RESULT_TAIL_COUNT
        scores = {int(seat): int(score) for seat, score in dict(scores).items()
                  if 0 <= int(seat) < GAME_RESULT_TAIL_COUNT}
        if not scores:
            return tail
        if quest_mode:
            if self.success:
                for seat in scores:
                    tail[seat] = GAME_RESULT_CLEARED
            return tail
        if team_mode:
            return self._team_ranking(scores, teams or {}, tail)
        return self._free_ranking(scores, tail)

    def _seat_rank_value(self, seat, score):
        """个人合成分，照抄 `0x55c072`：`(分数 + 1) * 1000 - 死亡数`。

        死亡数只当**破平局**用（原版靠 ×1000 把它压在低位），所以「分数一样时
        死得少的赢」，而不会让死亡数翻盘。
        """
        deaths = (self.deaths[seat]
                  if 0 <= seat < ROOM_SEAT_COUNT else 0)
        return (int(score) + 1) * 1000 - int(deaths)

    def _free_ranking(self, scores, tail):
        """个人战（含全场 0 分那种没打就散了的局）。"""
        values = {seat: self._seat_rank_value(seat, score)
                  for seat, score in scores.items()}
        best = max(values.values())
        # ★ 全员并列 -> 一个都不判（原版 `0x55c0bb cmp ebx, 在座人数 / je -> 0`）。
        #   「全场 0 分」自然落进这一条，和旧实现的 `best <= 0` 结果相同。
        if all(value == best for value in values.values()):
            return tail
        for seat, value in values.items():
            tail[seat] = (GAME_RESULT_CLEARED if value == best
                          else GAME_RESULT_DEFEATED)
        return tail

    def team_rank_values(self, scores, teams):
        """`{队伍号: (队伍总分, 队伍总死亡)}`，只含**有人**的队伍。日志也用它。"""
        totals = {}
        for seat, score in scores.items():
            team = int(teams.get(seat, TEAM_NONE))
            if team not in (TEAM_A, TEAM_B):
                continue
            score_sum, death_sum = totals.get(team, (0, 0))
            deaths = self.deaths[seat] if 0 <= seat < ROOM_SEAT_COUNT else 0
            totals[team] = (score_sum + int(score), death_sum + int(deaths))
        return totals

    def _team_ranking(self, scores, teams, tail):
        """组队战，照抄 `0x55bfda` 的组队分支。"""
        totals = self.team_rank_values(scores, teams)
        if not totals:
            # 一个人都没分到队（队伍号全是 TEAM_NONE）—— 退回个人口径，
            # 总比全场判 0 有信息量。
            return self._free_ranking(scores, tail)
        if len(totals) == 1:
            # `0x55c594`：在座的人全同队，没有对手 -> 全员胜。
            for seat in scores:
                if int(teams.get(seat, TEAM_NONE)) in totals:
                    tail[seat] = GAME_RESULT_CLEARED
            return tail
        # 队伍合成分：先比总分，平了比谁死得少（原版是把两者packed进一个整数）。
        ranked = {team: (score_sum, -death_sum)
                  for team, (score_sum, death_sum) in totals.items()}
        best = max(ranked.values())
        if all(value == best for value in ranked.values()):
            return tail            # 两队完全打平 -> 谁都不判（不放失败曲）
        for seat in scores:
            team = int(teams.get(seat, TEAM_NONE))
            if team not in ranked:
                continue
            tail[seat] = (GAME_RESULT_CLEARED if ranked[team] == best
                          else GAME_RESULT_DEFEATED)
        return tail


def build_rep_user_list():
    """opcode 0x020d —— 频道用户列表（空）

    反序列化 `0x54d0d3`：int32 / string / string / int32(当 bool 用)
    第一个 int32 == 0 时 `0x553c80` 的 je 会跳过后面整段列表处理。

    ⚠ **这个包不是大厅右侧那张「玩家列表」**（§166）。它的处理器
    `0x553c5f` 只是把两个字符串塞进一个弹窗对象；真正的列表走 `0x0212`，
    见 `build_rep_user_list_page()`。留着它是因为客户端确实会发
    客户端方向的 `0x020d`，回一个空的最省事。
    """
    return w_i32(0) + w_wstr("") + w_wstr("") + w_i32(0)


#: 玩家列表的过滤开关（`0x020d` 请求的第 5 个字节，§169）。
#: 大厅右侧那两个按钮 `[frame+0xe0]` / `[frame+0xe4]` 各自对应一个值：
#: **进大厅时客户端自己发的是 1**（`0x441bf7` 初始化 `[frame+0xcc]=1`）。
USER_LIST_FILTER_RECOMMENDED = 0   # 「추천상대 / 推荐对手」按钮（`0x442122`）
USER_LIST_FILTER_WAITING = 1       # 「대기유저 / 待机玩家」按钮（`0x442139`），默认

#: `UserSnap` 第三个 int32 = 竞技场（래더）等级，客户端拿 `20 - 它` 去
#: `Images/General/LadderMark.smf`（20 帧）里取图，越界一律取第 0 帧（§169）。
#: 存档里**没有**竞技场等级这项数据，统一发「最低档 20」——
#: 和发 0 画出来是同一张图，但线上的值落在合法区间里。
LADDER_GRADE_UNRANKED = 20


def parse_user_list_request(payload):
    """解客户端方向的 `0x020d gcpReqUserList`（5 字节，§166 / §169）。

    发送点 `0x554513`（由 `0x43d0c9` 调），线格式和应答的头三个字段一样：

        u16   页号        从 0 起
        u16   每页几条    客户端写死 0x12 = 18（`0x441bed` / `0x44215c`）
        u8    过滤开关    1 = 只看待机玩家（**默认**），0 = 推荐对手（§169）

    客户端每 10 秒重发一次（`0x43d0c9` 里的 0x2710 节流）。
    """
    reader = Reader(payload)
    page = reader.u16()
    page_size = reader.u16()
    flag = reader.take(1)[0]
    if reader.left():
        raise ValueError(f"user-list request has {reader.left()} trailing bytes")
    return {"page": page, "page_size": page_size, "flag": flag}


def build_rep_user_list_page(page=0, page_size=18,
                             flag=USER_LIST_FILTER_WAITING, users=()):
    """opcode **`0x0212`** —— `Packet_gspRepUserList`（vft `0x6918d4`，§166）。

    ★★ **这才是大厅右侧那张「유저리스트 / 玩家列表」的数据源。**
    以前一直以为是 `0x020d`（PROGRESS 里留了「数据源还没找到」的待办），
    实际上 `0x020d` 的服务端方向是个弹窗（`0x553c5f`）。
    分发跳表 `@0x54e58a`（索引 = opcode - 0x020e）第 4 格 -> `0x54e276`
    -> 处理器 `0x55458b`。

    反序列化 `0x54d343`：

        u16     页号        -> pkt+0x04   `0x5d59f1` 读 2 字节
        u16     每页几条    -> pkt+0x06   同上
        u8      过滤开关    -> pkt+0x08   `0x5d59d0` 读 1 字节
        int32   条目数
        条目 × { string 昵称, int32 在打游戏, int32 等级, int32 竞技场等级 }

    每一项是一个 `UserSnap`（vft `0x665374`），反序列化 `0x43cf5c`：
    先一个字符串（`0x5d5b3a`）再三个 int32（`0x5d59ff`），共 0x14 字节。

    ★ **三个 int32 的含义已查明**（§169，渲染函数 `0x441df5`）：

    | 位置 | 落到 | 客户端拿它干什么 |
    |---|---|---|
    | 第 1 个 | `UserSnap+0x08` | `!= 0` 画 `P`（游戏中）、`== 0` 画 `W`（待机）|
    | 第 2 个 | `UserSnap+0x0c` | 等级图标：`LevelMark.smf` 第 `等级-1` 帧（60 帧）|
    | 第 3 个 | `UserSnap+0x10` | 竞技场等级：`LadderMark.smf` 第 `20-它` 帧（20 帧）|

    上一版把**等级填进了第 1 个**，于是每一行都是「P + 1 级」——
    这就是用户报的「所有玩家显示的一样，分不清谁是谁」（§169）。

    处理器把头三个字段原样塞进列表管理器 `[0x72e674]` 的
    `+0x14` / `+0x18` / `+0x1c`，也就是**服务端要把请求里的页号 / 每页几条 /
    开关原样回显**，客户端翻页全靠它。
    """
    payload = [struct.pack("<HH", page & 0xFFFF, page_size & 0xFFFF),
               struct.pack("<B", flag & 0xFF),
               w_i32(len(users))]
    for entry in users:
        nickname = entry[0]
        values = tuple(entry[1:]) + (0, 0, 0)
        payload.append(w_wstr(nickname))
        payload.extend(w_i32(v) for v in values[:3])
    return b"".join(payload)


# ----------------------------------------------------------------------------
#: 详细日志开关（`--verbose`）。关掉时不打逐包 hexdump、不打「试解」、
#: 不打下面 NOISY_OPCODES 里的高频战斗包。
#:
#: 为什么默认关（会话 14）：战斗中 `0x0406` 位置同步每秒好几发，每条都要
#: hexdump + `print(flush=True)` + 文件 `flush()`。协议已经解完了，日常游玩
#: 这些行没人看，只是在跟游戏抢磁盘 I/O、把日志撑到几十 MB。
#: 排查协议问题时加 `--verbose` 就回到原来的全量行为。
VERBOSE = False

#: 回不回 `0x0210 gspJoinRelay`，也就是原版 TCP 中继那条路开不开（D078）。
#: **默认开** —— 用户拍板要「原版的连接方式全部原样还原」。
#:
#: ★ 这就是 D078 写的那个反悔开关：中继一断，客户端会自己退出房间（§158），
#:   真在实机上撞见这类坑，`app.py --no-tcp-relay` 一关，客户端立刻退回
#:   `0x040e` 那条**同样是原版的**回退路径，其余一个字都不用改。
TCP_RELAY_ENABLED = True

#: 战斗中每秒多发、且语义早已查明的包。非 verbose 时只在**第一次**出现时记一行，
#: 之后静音（完全不记会让「客户端到底还在不在发」这种判断失去依据）。
NOISY_OPCODES = {
    0x0406,   # gcpCreateItem 掉落物请求（§105 之前是日志量最大的来源）。
              #   ★ 静音的只是通用那行；on_create_item() 会另打「本局第一件」。
              #   通关后的金币雨每 300 毫秒一发，真要逐件看得开 --verbose
    0x0408,   # HP 归零上报。★ 静音的只是这行「★ 游戏包 …」通用行；
              #   on_report_hp_zero() 每次都会另打两行可读的死亡日志（§108）
    0x0407,   # gcpGetItem 拾取请求。★ 静音的只是通用那行；
              #   on_get_item() 会另打「本局第一件」
    0x0410,   # gcpUpdateQuestScore
    0x0104,   # gcpRepPing
    0x040e,   # ★ 玩家间同步数据（§149）。开关一开就是 ~8 Hz，一局能有上万发。
              #   静音的只是这行通用行；on_peer_data() 每个房间**第一发**会另打
              #   一行带 hexdump 的，够看清 UdpPacket 的 12 字节头了
}


def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def hexdump(b, maxlen=512):
    out = []
    d = b[:maxlen]
    for i in range(0, len(d), 16):
        row = d[i:i + 16]
        hx = " ".join(f"{x:02x}" for x in row)
        asc = "".join(chr(x) if 0x20 <= x < 0x7F else "." for x in row)
        out.append(f"    {i:04x}  {hx:<47}  |{asc}|")
    if len(b) > maxlen:
        out.append(f"    ... 还有 {len(b) - maxlen} 字节")
    return "\n".join(out)


#: `UdpPacket` 的头长度（§151）。`RawPacket` 是 10 字节，这个是 12。
PEER_HEADER_SIZE = 12

#: 多久往 `[online]` 汇总一次同步数据的转发耗时（秒）。见 `report_peer_timing`。
PEER_TIMING_REPORT_INTERVAL = 30.0


#: 内层 opcode 的名字。`< 0x4000` 那批是从 `GameSession::ProcessReliableQueue`
#: （`0x407c84`）里那张 switch 表的宽字符串字面量逐个读出来的；`>= 0x4000`
#: 那批按 `0x407956` 的分发表编号（§216）。
#:
#: ★ 排查「打不死人」这类同步问题时，能一眼看出**丢的是哪一发**全靠这张表：
#: `rpChangeWeapon` / `rpFire` 掉一发，收件人那台的对象句柄分配器就永久错位，
#: 之后 `rpExplode` 里的句柄全对不上 => 整局零伤害（§216）。
PEER_INNER_NAMES = {
    0x0001: "rpChangeWeapon", 0x0002: "rpFire", 0x0003: "rpExplode",
    0x0004: "rpSplashDamaged", 0x0005: "rpSetOnFire", 0x0006: "rpJump",
    0x0007: "rpDash", 0x000B: "rpCrouch", 0x000C: "rpRespawn",
    0x000D: "rpReqState", 0x000E: "rpRepState", 0x000F: "rpReqDie",
    0x0011: "rpAiMsg", 0x0018: "rpGuard", 0x001B: "rpCreateTotem",
    0x4001: "心跳（body 头 2 字节 = 发送方下一个事件序号 N）",
    0x4002: "讨重传", 0x4003: "重传应答", 0x4004: "?", 0x4005: "读图心跳",
}


def describe_peer_header(payload):
    """把 `0x040e` 载荷开头那 12 字节的 `UdpPacket` 头解成一行人话（§151）。

    只给日志看。转发路径上只动头里的局号（`+4`，见
    `relayserver.restamp_peer_game_id`），这个函数解错了也不会影响转发。
    """
    if len(payload) < PEER_HEADER_SIZE:
        return f"    （只有 {len(payload)} 字节，装不下 12 字节的 UdpPacket 头）"
    magic, sender, target, unknown3 = struct.unpack_from("<BbbB", payload, 0)
    game_id, checksum, sequence, inner = struct.unpack_from("<HHHH", payload, 4)
    name = PEER_INNER_NAMES.get(inner, "?")
    line = ("    UdpPacket 头: magic=0x%02x 发送方座位=%d 目标座位=%s ?[3]=%d "
            "局号=%d 校验和=0x%04x 序列号=%d 内层opcode=0x%04x (%s) body=%d 字节"
            % (magic, sender,
               "广播" if target == -1 else str(target),
               unknown3, game_id, checksum, sequence, inner, name,
               len(payload) - PEER_HEADER_SIZE))
    # ★ 心跳里那个 N 是「收包队列基线」的唯一来源（`FlushTo`，§216），
    #   排查跨纪元中毒时要的就是它，所以顺手解出来。
    if inner == 0x4001 and len(payload) >= PEER_HEADER_SIZE + 2:
        nxt, = struct.unpack_from("<H", payload, PEER_HEADER_SIZE)
        line += " N=%d" % nxt
    return line


_seq = 0
_lock = threading.Lock()

# ----------------------------------------------------------------------------
# 调试控制通道
# ----------------------------------------------------------------------------
#: 活动连接表，最新的排在最后。控制通道不指定账号时操作最后一个。
#:
#: V0.2 起同时可能有好几个玩家在线，所以「最后一个」只是**默认值**，
#: `tools/gs_ctl.py --user <账号>` 可以指定操作谁（多于一个连接时必须指定）。
_conns = []
_conns_lock = threading.Lock()

#: 全进程唯一的大厅房间表（里程碑 I）。做成模块级单例而不是 `Conn` 的属性，
#: 理由和 `_conns` 一样：房间是**跨连接**的共享状态，认证服和游戏服又本来就
#: 在同一个进程里（D064）。测试里的 `Conn` 夹具是 `__new__` 出来的、不进任何
#: 房间，所以 `LOBBY.leave(conn)` 对它们是无害的空操作。
LOBBY = Lobby()


def _relay_room_members(game_conn):
    """中继投递时用：和这条连接同房间的**其他**连接。"""
    room = LOBBY.room_of(game_conn)
    if room is None:
        return []
    return room.members(exclude=game_conn)


def room_generation(room, kind=None):
    """房间当前的**代号**；给了 `kind` 就按它推进（同 kind 幂等，§218 / D137）。

    `kind=None` = 「只要号，别改变代」——**锚定**时用的就是这一种：
    刚建出来的房间还没有号，给它补一个「房间代」；已经有号的原样返回。
    这样锚定永远不会把一个正在打的房间倒回「房间代」去。
    """
    if kind is None:
        kind = room.epoch_kind or "room"
    return room.advance_generation(kind, relayserver.next_generation)


def _relay_generation_of(game_conn):
    """`RelayServer` 的补锚回调：这条连接当前房间的代号（不在房间里就 None）。

    正常路径上每条连接在建房 / 进房时就锚定过了，这是防御性的第二道。
    """
    room = LOBBY.room_of(game_conn)
    return None if room is None else room_generation(room)


def _relay_fallback(member, udp_packet):
    """对方还没接上中继时的退路：走它自己的游戏服连接发 `0x040f`。

    这不是权宜之计 —— `0x408619` 在「没有中继连接」时走的就是这条
    （§149 / D078）。中继连接是异步建的，进房之后必然有一小段
    「有人还没连上中继」的窗口，那几秒里同步不能断。
    """
    member.send(build_game(OP_PEER_DATA_DOWN, udp_packet))


def _relay_battle_tick(game_conn):
    """每转发一份同步数据就跑一次的房间级判断。

    ★ **必须挂在 `RelayServer.deliver` 上，不能挂在 `Conn.on_peer_data` 上**：
    原版中继一旦建起来，整局连一发 `0x040e` 都不会再有（§160），
    而 `deliver()` 是两条通道唯一的汇合点。

    这里放的两件事都是「到点了就动一下」，本身带房间级去重：

    - `check_pvp_finished()` —— 对战的 240 秒时间上限（§167）。挂在这儿之前
      它只在有人死的时候才会被问到，中继模式下一局不死人就永远不结算。
    - `maybe_spawn_item()` —— 道具模式往地图上刷道具（§191 / D109）。
    - `check_respawn_watchdog()` —— 死了 8 秒还没发 `0x0413` 的人由服务端
      补一发 `0x0419`（bug调查/8「死了不复活」）。同步数据战斗中恒定 ~8 Hz，
      拿它当心跳的精度绰绰有余。

    ★ 排在最后的两件事都**不许**把同步转发带崩：`deliver()` 已经把本函数
    整个包在 try 里（见 `RelayServer.deliver`），这里不再另加一层。
    """
    game_conn.check_pvp_finished()
    game_conn.maybe_spawn_item()
    game_conn.check_respawn_watchdog()


#: 全进程唯一的原版 TCP 中继（里程碑 J.3 / D078）。和 `LOBBY` 同一个理由做成
#: 模块级单例：中继连接是**跨连接**的共享状态。
#: ★ `relayserver` 不 import 本模块（反过来才对），查房间成员、回退投递和
#: 战斗节拍三件事都靠这里注入的回调完成。
PEER_RELAY = relayserver.RelayServer(
    members_of=_relay_room_members,
    fallback=_relay_fallback,
    on_traffic=_relay_battle_tick,
    generation_of=_relay_generation_of,
    # 位置数据的 UDP 旁路（bug调查/9）。`deliver()` 里它是**优先级最高、
    # 准入条件最窄**的那一条路：只有位置心跳、只有自证过能收的收件人、
    # 而且只有 N 没变的那一发才走它。见 `udpsync.may_send_heartbeat`。
    udp_sender=udpsync.SERVER,
    logger=lambda msg: print(f"[{ts()}] [relay] {msg}", flush=True),
)


def _conn_for_udp_ticket(ticket):
    """`票据 -> 已登录的游戏连接`。`udpsync` 认 UDP 流时调它。

    ★ 只认**已经登录成功**的连接（`login_ticket` 是在 `tickets.bind()` 旁边
    才写上的），所以拿一张没用过的票据从 UDP 上来是查不到任何东西的。
    """
    ticket = str(ticket or "")
    if not ticket:
        return None
    with _conns_lock:
        for conn in _conns:
            if conn.login_ticket and conn.login_ticket == ticket:
                return conn
    return None


udpsync.SERVER.bind_lookup(
    _conn_for_udp_ticket,
    logger=lambda msg: print(f"[{ts()}] [udpsync] {msg}", flush=True))


def register_conn(conn):
    with _conns_lock:
        _conns.append(conn)


def unregister_conn(conn):
    with _conns_lock:
        if conn in _conns:
            _conns.remove(conn)


def latest_conn():
    with _conns_lock:
        return _conns[-1] if _conns else None


def all_conns():
    with _conns_lock:
        return list(_conns)


def conn_is_playing(conn):
    """这条连接现在是不是**在打游戏**（而不是待在大厅/房间里）。

    判据就是大厅那份房间状态：所有人一起进 stage 7 时房间被标成
    `SESSION_STATUS_PLAYING`（`on_start_game_packet`），结算完回房间又标回
    待机。坐在「待机中」的房间里等人**算待机** —— 房间列表也是这么显示的，
    两边口径一致。
    """
    room = LOBBY.room_of(conn)
    return room is not None and room.status == SESSION_STATUS_PLAYING


def room_in_battle(room):
    """这一局是不是**真开打了**（全员加载完、一起进 stage 7）。

    加载期（`PREPARING`）房间就已经标「游戏中」挡人了（bug调查/1 的
    死锁修复，见 `broadcast_start_game`），但那一局还没开打：战斗状态
    `room.quest` 要等收齐所有人的 `0x0403` 才按在座座位建。刷道具 /
    判胜负 / 控制权交接这些「战斗内」逻辑必须用这个判，别直接看
    `room.status` —— 否则加载期一有人退房就会把 `quest_state()` 的
    懒惰分支踩出来，提前建一份座位不对的战斗状态，真进关卡时反而
    不重建了。
    """
    return (room is not None
            and room.status == SESSION_STATUS_PLAYING
            and room.battle is not None
            and room.battle.state == StartGameHandshake.IN_GAME)


def room_started(room):
    """这一局的 `0x0400 gspPrepareGame` 已经发出去了吗？

    ⚠ 和 `room_in_battle()` **不是一回事**，别混用：那个要求握手已经走到
    `IN_GAME`（收齐所有人的 `0x0403`、发完 `0x0402`），是「战斗内逻辑」
    （刷道具 / 判胜负 / 控制权交接）的判据；这个只问「关卡加载是不是已经
    开始了」，因此把 `PREPARING`（stage 6 加载中）也算上。

    随机地图模式下房主的那一发地图汇报 `0x0302` 恰恰落在 `PREPARING`
    —— 它是紧跟着 `0x0400` 的处理发出来的（§228）。
    """
    return (room is not None
            and room.battle is not None
            and room.battle.state in (StartGameHandshake.PREPARING,
                                      StartGameHandshake.IN_GAME))


def online_user_snapshots(viewer=None, waiting_only=False):
    """在线玩家列表，喂给 `0x0212`（§166 / §169）。

    每项 `(昵称, 在打游戏 0/1, 等级, 竞技场等级)` —— 顺序就是线上的顺序，
    别再把等级放到第一格（那一格是 `P`/`W` 徽章，§169）。

    - `viewer` 是发请求的那条连接：**它自己不进列表**。客户端那张列表
      没有任何「这是你」的标记（渲染函数 `0x441df5` 从头到尾不比昵称），
      所以只能靠不列自己来消除歧义（D095）。
    - `waiting_only=True`（客户端默认的「待机玩家」档）只留没在打游戏的人。

    ★ 按账号去重：同一个账号同一时刻只该有一条连接（顶号会关掉旧的），
    但断线重连的窗口里可能短暂有两条，列表里出现两个同名很难看。
    没登录成功（还没认出账号）的连接不进列表。
    """
    out = []
    seen = set()
    viewer_name = (getattr(viewer, "account_name", "") or "") if viewer else ""
    for conn in all_conns():
        name = getattr(conn, "account_name", "") or ""
        if not name or name in seen:
            continue
        if viewer_name and name == viewer_name:
            continue
        seen.add(name)
        account = getattr(conn, "account", None) or {}
        nickname = display_name(account) or name
        playing = 1 if conn_is_playing(conn) else 0
        if waiting_only and playing:
            continue
        out.append((nickname, playing, player_level(account),
                    LADDER_GRADE_UNRANKED))
    out.sort(key=lambda item: item[0])
    return out


def conns_for_user(username):
    """某个账号当前的全部连接（正常只会有一条）。"""
    username = str(username or "")
    with _conns_lock:
        return [c for c in _conns if c.account_name == username]


def pick_conn(username=""):
    """控制通道用：按账号名挑一条连接。

    不给账号名时：只有一条就用它；有多条就返回 ``None`` 并让调用方报错，
    **绝不猜** —— 拿错连接会把包发给别的玩家。
    """
    username = str(username or "").strip()
    if username:
        found = conns_for_user(username)
        return found[-1] if found else None
    conns = all_conns()
    if len(conns) == 1:
        return conns[0]
    return None


class Conn:
    # ★ 类级默认值：`Conn.__new__` 造的测试实例不走 `__init__`，
    #   这两个标志必须在类上也有一份默认，否则新代码一碰就 AttributeError。
    send_broken = False
    last_relay_reissue_at = 0.0
    # 版本门禁的两个状态同理（见 __init__ 里的说明）。
    client_version = None
    version_rejected = False

    def __init__(self, sock, addr, args, accounts=None, tickets=None):
        global _seq
        with _lock:
            _seq += 1
            self.seq = _seq
        self.sock = sock
        self.addr = addr
        self.args = args
        # 服务端视角：收 = 客户端的发送流，发 = 客户端的接收流
        self.cin = SimpleCipher.client_to_server()
        self.cout = SimpleCipher.server_to_client()
        self.buf = bytearray()
        self.got_version = False
        # 握手裸发版本号解码出的复活项目版本（元组）；None = 旧版客户端
        #（没上报版本，含原版 311）。on_game_login 和断开日志都要带着它。
        self.client_version = None
        # 握手时被版本门禁拒过 —— 正常情况下客户端会弹升级提示框并停下，
        # 万一它没停、把 0x0100 发过来了，on_game_login 靠这个标志兜底拦截。
        self.version_rejected = False
        # ★ 账号存储和票据表由 `server/app.py` 建好后传进来，全进程共用一份
        #   —— 票据是认证服签发的，游戏服要能查到它（D064）。
        #   单独跑 gameserver.py 做协议试探时各自新建，行为退化成 V0.1 的样子。
        self.accounts = accounts if accounts is not None else AccountStore(args.accounts)
        self.tickets = tickets if tickets is not None else TicketStore()
        self.account_name = None
        self.account = None
        self.start_game = StartGameHandshake(seed=0)
        # 我的座位号。建房时客户端把自己放在座位 0（0x54f807，FINDINGS §75）。
        self.my_seat = 0
        # gcpUpdateQuestScore(0x0410) 报上来的累计分数，结算时用。
        self.quest_score = 0
        # 本局是不是通关了。唯一来源是客户端的 0x0417 gcpMarkQuestSuccess ——
        # 打死关底时关卡脚本调 GameContextQuest::vf_e4(1) 发出（0x4a3faa），
        # 实测比 0x040f 早 30 秒到，所以结算时这个标志一定已经就位。
        # 反过来「时间到 / 生命耗尽」那条路是 EndQuest() 之后才 vf_e4(0)
        # （0x4a3dac -> 0x4a3dbd），到不了这里，正好也就是「未完成」。
        self.quest_success = False
        # 本局结算包是否已下发。只有发过之后收到的 0x0405 才当「结算看完了」。
        self.settled = False
        self.last_packet_at = time.time()
        # 通道 A（`0x040e`/`0x040f` 玩家间同步）的开关状态，见 §149 / §150。
        # 客户端那边默认就是关的，所以初值 False。★ 客户端**退房时会自己把
        # 开关清 0**（`0x406191`），所以我们也必须在离开房间时跟着清回 False，
        # 否则下次进房 `send_toggle_peer_relay()` 会以为「已经开着」而不重发。
        self.peer_relay_on = False
        # 发送流是否已经废了（见 `send_all_bounded` 的注释：一次发送超时/
        # 半发送之后密码流错位，这条连接对客户端来说已经不可读，只能拆）。
        self.send_broken = False
        # 上一次因为「要不到中继」而重发 0x0211+0x0210 的时刻（节流用）。
        self.last_relay_reissue_at = 0.0
        # 这条连接**进当前这个房间之后**转发的第一发 `0x040e` 打不打 hexdump。
        self.peer_data_dumped = False
        self.peer_data_in = 0
        self.peer_data_out = 0
        # 这台客户端**最近一次自报的局号**（`UdpPacket` 头 +4）。只给日志和
        # 探针看；转发的判定用的是下面那份换代模型。
        self.peer_game_id = None
        # ★★ 这条连接的**换代状态**（`relayserver.PeerEpoch`，§218 / D137）。
        #
        # 局号不是客户端的私有计数器，是**只有服务端能推动的换代号**：
        # `0x0400`/`0x0403` 各 +1，登录成功 / `0x0203` 归 -1，每一次都同时
        # 清空六条收包队列。所以这份模型由 `send()` 认出那几发包自己维护
        # （见 `note_epoch_from_frame`），转发时按「代」判定能不能投递。
        self.peer_epoch = relayserver.PeerEpoch()
        # 同步数据的转发耗时 / 到达间隔（都按毫秒），每 30 秒汇总一行。
        # 存在的意义是**排除嫌疑**：实机还嫌卡时，这两个数字能立刻说清
        # 「不是服务端转发慢」，省掉一整轮猜（§182）。
        self.peer_forward_ms = relayserver.RttStats()
        self.peer_gap_ms = relayserver.RttStats()
        self.peer_last_at = None
        self.peer_report_at = time.monotonic()
        # ★ **转发**间隔（两条路合流之后）。和上面那个 `peer_gap_ms`（TCP 到达
        #   间隔）一起打进同一行日志，两个数字并排就是「UDP 到底救回了多少」：
        #   bug调查/9 那一局 TCP 到达间隔 p95=432ms，转发间隔应该落到 ~130ms。
        self.peer_out_gap_ms = relayserver.RttStats()
        self.peer_out_last_at = None
        # ★ 位置数据 UDP 旁路的**排序闸门**（`udpsync` 开头那四条铁律）。
        #   上行是 UDP + TCP 双发，两条路的心跳在这里合成一条有序流；
        #   事件包（内层 < 0x4000）只走 TCP，但要在这里记账 —— 铁律 3 靠它。
        self.peer_order = udpsync.HeartbeatOrder()
        # ★ 同步数据的上行现在有**三个**线程会走到：这条连接自己的线程
        #   （`0x040e`）、原版中继连接的线程（rcp opcode 3）、以及 UDP 收包
        #   线程（`udpsync`）。闸门、统计和转发必须串起来，否则两条路可能
        #   同时判「该我转」而把同一发心跳投两次 —— 那正是铁律 4 要防的。
        #   ⚠ 顺序永远是「先 peer_lock 再 send_lock」，反过来拿会死锁。
        self.peer_lock = threading.RLock()
        # 上一次看到的局号，用来判断「换代了，闸门要归零」。
        self.peer_order_epoch = None
        # 登录时那张票据。UDP 那条流靠它认人（`udpsync._on_hello`）——
        # 票据本来就是重连凭证（`tickets.py`），不引入新的秘密。
        self.login_ticket = ""
        # 本局回了几次 0x0406 死亡广播 / 几次 0x0419 重生（只用来打日志编号）。
        self.deaths_broadcast = 0
        self.respawn_sent = 0
        # 最近一次解析成功的建房请求（0x0201），下发 0x0303 时原样回显。
        self.room = None
        # 登录包第 2 个业务 int32 就是频道码，当前发 0（普通频道 0），
        # 所以客户端一进大厅就停在「对战」标签页上。
        self.channel_code = 0
        self.channel_index = 0
        # 战斗中客户端 0x0406 gcpCreateItem 自报的最后一个掉落点（float）。
        # 掉落点就在角色/怪物脚下，所以拿它当控制通道 respawn 的兜底坐标是
        # 合适的；正式重生走 0x0413 -> 0x0419 的回显，用不着它（§112 勘误：
        # 这个包不是「位置同步」，只是它的第 2、3 个 dword 确实是坐标）。
        self.last_position = None
        # 本局关卡的状态**不在房间里时**用的那一份（协议试探 / 控制通道手搓包）。
        # 在房间里时用的是 `lobby.Room.quest`，见 `quest_state()`。
        self.solo_quest = RoomQuest()
        self.items_created = 0
        # 本局回了几次 0x0405 拾取放行（客户端每踩到一件掉落物发一发 0x0407）。
        self.items_picked = 0
        # 本局转发过几发 0x040d「效果结束」（§200）。死后每 5 秒就有一发
        # `(座位, 属性 0)`，非 verbose 时只报第一条，免得刷屏。
        self.attrs_removed = 0
        # 已经报过一次的高频 opcode（见 NOISY_OPCODES）。
        self.noisy_seen = set()
        # ★ 逐连接的三个文件**全部只在 --verbose 下建**（D112）。
        #   非 verbose 时 `self.log()` 照样打到 stdout（= `logs/server.out`），
        #   一行都不会少 —— 少的只是「每条连接一个文件」。
        #   以前那份 .txt 是无条件建的，于是玩一次就多一个文件、清也没人清：
        #   这台开发机的 logs/ 里攒了 1076 份（用户 2026-08-14 报的正是这件事）。
        if VERBOSE:
            self.ft = open(os.path.join(LOGDIR, f"game_{self.seq:03d}_27799.txt"),
                           "w", encoding="utf-8")
            self.fb_raw = open(os.path.join(LOGDIR, f"game_{self.seq:03d}_27799.raw.bin"), "wb")
            self.fb_dec = open(os.path.join(LOGDIR, f"game_{self.seq:03d}_27799.dec.bin"), "wb")
        else:
            self.ft = self.fb_raw = self.fb_dec = None
        # 控制通道的线程会从另一个线程调 send()，加密流是有状态的，必须串行化。
        # 用 RLock：send_batch() 会先拿锁再在同一个线程里反复调 send()。
        self.send_lock = threading.RLock()
        # send_batch() 期间攒着的明文包；非 None 时 send() 只入队不发。
        self.send_queue = None
        # 只给 `--room-burst-delay` 用：每发完一个包故意等这么久，复现 §120。
        self.batch_delay_ms = 0
        #: 连上来的时刻，断开时用来算在线时长（连接事件日志用）。
        self.connected_at = time.monotonic()
        register_conn(self)

    def log(self, msg):
        line = f"[{ts()}] #{self.seq} {msg}"
        print(line, flush=True)
        # `self.ft` 只在 --verbose 下存在（见构造函数）。stdout 那一份一直都在，
        # 被启动脚本重定向进 `logs/server.out`。
        if self.ft is not None:
            self.ft.write(line + "\n")
            self.ft.flush()

    def peer(self):
        """本连接对端的 ``ip:port``（v4-mapped 前缀已剥掉）。"""
        return eventlog.peer(self.addr)

    def online(self, msg):
        """连接事件（上线 / 下线 / 顶号）。**精简模式下也照打**，见 eventlog.py。"""
        eventlog.online(f"游戏服 #{self.seq} {msg}")

    def online_debug(self, msg):
        """遥测 / 客户端噪声（转发耗时、异常上报）。**只有 --verbose 才落盘**。

        判据见 eventlog.py 开头那张表：频率由**定时器**决定的都归这一档，
        频率由**玩家动作**决定的才留在 `online()`（D112）。
        """
        eventlog.debug(f"游戏服 #{self.seq} {msg}")

    def vlog(self, msg):
        """逐包细节（hexdump / 字段试解）。只在 `--verbose` 时落盘。"""
        if VERBOSE:
            self.log(msg)

    def kill_stream(self, why):
        """发送流已废：直接拆 socket，让客户端走重连。

        ★ 只在「密码流已经错位」时调用（发送超时/半发送之后）。这种连接
        留着只会变成「客户端能发、我们也能收、但它永远解不开我们发的任何
        包」的半死连接 —— 玩家躺在地上等一枚永远不来的 0x0419
        （bug调查/4 最后一局的形态）。拆掉 socket，客户端会像崩溃恢复那样
        自动回登录重连，比无声卡死好。
        """
        if self.send_broken:
            return
        self.send_broken = True
        try:
            self.log(f"!! 发送流已损坏（{why}），拆除连接让客户端重连")
        except Exception:                    # 日志绝不能挡拆连接
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def room_generation(self, kind=None):
        """本连接当前房间在 `kind` 这一代的代号（`kind=None` = 只要当前那个）。

        不在房间里就单开一代。

        「不在房间里」= 协议试探 / 单机那条路（`on_start_game_packet` 里
        `room is None` 的分支）—— 那时根本没有别人，单开一代最安全：
        它和谁都不同代，谁的同步数据也不会串进来。
        """
        room = self.lobby_room()
        if room is None:
            return relayserver.next_generation()
        return room_generation(room, kind)

    def anchor_epoch(self, room, why=""):
        """建房 / 进房：把这条连接的当前局号锚定到房间**当前**这一代。

        局号本身不会因为进房而变（客户端只在 `0x0400`/`0x0403`/复位那三种
        包上动它，§218），所以进房要做的只是「记下它现在属于哪一代」。
        中途进房的人和老玩家的**编号起点不同**（一个从 -1 数起、一个已经
        数了好几局），但只要在同一代里，转发时按收件人的编号盖章就能互通。
        """
        gen = room_generation(room)
        state = relayserver.epoch_state(self)
        state.anchor(gen)
        self.log(f"   换代锚定：代 {gen}，我的局号 {state.value}{why}")

    def note_epoch_from_frame(self, plain):
        """认出「会让客户端换代的那几发包」，把换代模型跟着推一格（§218 / D137）。

        三种迁移，上面那三张表列全了：
        `EPOCH_ADVANCING_OPS`（`0x0400`/`0x0403` 各 +1）、
        `EPOCH_ASSIGNING_OP`（`0x0303` 直接设成包尾那个 u16）、
        `EPOCH_RESETTING_OPS`（登录成功 / `0x0203` 归 -1）。

        ★ **为什么挂在 `send()` 而不是各个业务分支上**：客户端局号的每一次
        变化都是这几发字节造成的，把状态迁移挂在**字节离开的地方**，
        就没有哪个发送点能漏登记 —— 进房四连发、结算回房间、房间参数变更的
        广播、控制通道的 `back-to-room`、将来新增的路径，全都自动被覆盖。

        代价只有几百纳秒（一次首字节判定 + 一次 u16 解包 + 一次字典查），
        相对客户端自己那 100 ms 的心跳（§187）可以忽略。
        """
        if len(plain) < 10 or plain[0] != MAGIC_GAME:
            return
        opcode = int.from_bytes(plain[8:10], "little")
        kind = EPOCH_ADVANCING_OPS.get(opcode)
        if kind is not None:
            relayserver.epoch_state(self).advance(self.room_generation(kind))
            return
        if opcode == EPOCH_ASSIGNING_OP and len(plain) >= 12:
            # 包尾那个 u16 就是我们要它认的局号（`build_update_session`）。
            said = relayserver.as_signed_epoch(
                int.from_bytes(plain[-2:], "little"))    # 客户端按 int16 读
            relayserver.epoch_state(self).assign(said, self.room_generation())
            return
        if opcode in EPOCH_RESETTING_OPS and _frame_result_ok(plain):
            # 结果码 0 才复位；非 0 客户端只弹个错误框，什么都不动。
            relayserver.epoch_state(self).reset()

    def send(self, plain):
        # SimpleCipher 是逐字节推进的流密码：两个线程交错加密会让整条流错位，
        # 客户端从此再也解不回来。控制通道存在之后这不再是理论风险。
        with self.send_lock:
            if self.send_broken:
                return                       # 流已废，发了也只是垃圾
            # ★ 换代模型的**唯一**迁移点，见 `note_epoch_from_frame`。
            self.note_epoch_from_frame(plain)
            # ★ 这个 `if` 不是多余的：`vlog` 自己也判 VERBOSE，但 f-string 和
            #   `hexdump()` 是**在调用之前**就求值的，非 verbose 时白算再丢掉。
            #   实测 53 字节的包要 18.4 µs，而战斗中每份同步数据都要走这里
            #   （§187）。延迟上不值一提，但白烧的 CPU 没有理由留着。
            if VERBOSE:
                self.vlog(f"→ 发出 {len(plain)} 字节明文\n{hexdump(plain)}")
            if self.send_queue is not None:
                self.send_queue.append(plain)
                return
            wire = self.cout.encrypt(plain)
            try:
                send_all_bounded(self.sock, wire, GAME_SEND_DEADLINE_S)
            except OSError as error:
                self.kill_stream(f"send: {error!r}")
                raise
            if self.batch_delay_ms:
                time.sleep(self.batch_delay_ms / 1000.0)

    @contextlib.contextmanager
    def send_batch(self, reason=""):
        """把块内所有 `send()` 攒成**一次** `sendall`。

        ★ 这不是性能优化，是**修一个时序 bug**（FINDINGS §120）。

        客户端是「每帧 recv 一次 → 把收到的包全部分发完 → 下一帧才真正
        构造新 stage」。`ChangeStage`(`0x40e47f`) 只记下工厂函数，
        `RoomStage` 构造函数 `0x466979` 要到下一帧才跑，它在
        `0x466ea3` 处**一次性**建出「人物选择」的头像按钮，之后除了拖滚动条
        再也不会重建。

        所以只要 `0x0201`（触发 ChangeStage）和 `0x030b`（角色清单）落在
        **同一帧的那一次 recv 里**就万事大吉；一旦客户端的 recv 恰好插进
        两次 `sendall` 之间，房间就用一份空清单建 UI —— 头像缩回 3 个，
        而且这一局再也回不来。实测两次 sendall 之间隔着一次 `log()`
        （约 1 ms），客户端一帧 16 ms，所以是「小概率、时有时无」。

        攒成一次写之后，四个包要么一起进客户端的接收缓冲、要么一起不进,
        这个窗口就没有了。SimpleCipher 是逐字节流密码，
        `encrypt(a+b) == encrypt(a)+encrypt(b)`，下发字节一个都没变。
        """
        delay_ms = getattr(self.args, "room_burst_delay", 0) or 0
        if delay_ms > 0:
            # --room-burst-delay：故意退回「一包一次 sendall」并拉开间隔，
            # 用来复现这个 bug（同 D047 的思路：留一个能一键回到坏行为的开关）。
            self.log(f"   （--room-burst-delay {delay_ms}ms：不合并，"
                     f"逐包发送{reason}）")
            with self.send_lock:
                self.batch_delay_ms = delay_ms
                try:
                    yield
                finally:
                    self.batch_delay_ms = 0
            return
        with self.send_lock:
            if self.send_queue is not None:
                # 已经在批里了，不嵌套（内层 with 结束就把整批发出去会破坏语义）。
                yield
                return
            self.send_queue = []
            try:
                yield
            finally:
                packets, self.send_queue = self.send_queue, None
                if packets:
                    plain = b"".join(packets)
                    self.log(f"   （{len(packets)} 个包 {len(plain)} 字节"
                             f"合并成一次发送{reason}）")
                    wire = self.cout.encrypt(plain)
                    try:
                        send_all_bounded(self.sock, wire, GAME_SEND_DEADLINE_S)
                    except OSError as error:
                        self.kill_stream(f"send_batch: {error!r}")
                        raise

    # -- 大厅 / 房间 ---------------------------------------------------------
    def lobby_room(self):
        """本连接当前所在的房间（`lobby.Room`），不在房间里就是 ``None``。"""
        return LOBBY.room_of(self)

    # ---- 战斗逻辑的三个共用取数口（J.3）------------------------------------
    #
    # ★ **战斗处理器只许有一条代码路径。** 不在房间里（协议试探、控制通道
    #   手搓包）时用连接自带的 `self.solo_quest` 和「房里只有我一个人」的
    #   成员表顶上，行为就退化成 V0.1 的单连接回显 —— 一个字节都不差。
    #   写成 `if room is None: 老逻辑 else: 新逻辑` 的话，单机那一支永远没人
    #   测，迟早和联机那一支长歪（同 CLAUDE.md 铁律 8 的道理）。

    def quest_state(self):
        """本局关卡的战斗状态（`RoomQuest`）。在房间里就是房间那一份。"""
        room = self.lobby_room()
        if room is None:
            return self.solo_quest
        if room.quest is None:
            # 正常开局走 `broadcast_start_game`（那里带着在座座位建），
            # 这条懒惰分支只有「协议试探 / 控制通道手搓包」会走到。
            room.quest = RoomQuest(seats=self.battle_seats())
        return room.quest

    def battle_members(self):
        """要收到战斗广播的连接（**含自己**）。不在房间里就只有自己。"""
        room = self.lobby_room()
        if room is None:
            return [self]
        members = room.members(exclude=None)
        return members if self in members else members + [self]

    def battle_seats(self):
        """本局有人的座位号（升序）。不在房间里就只有自己那一个。"""
        room = self.lobby_room()
        if room is None:
            return [self.my_seat]
        return [i for i, seat in enumerate(room.seats) if seat is not None]

    @property
    def maps_entered(self):
        """本局已放行的地图名（房间级，见 `RoomQuest`）。只读。"""
        return self.quest_state().maps_entered

    @property
    def map_change_pending(self):
        """有没有一次换图正在飞（房间级）。只读。"""
        return self.quest_state().pending_map is not None

    def quest_mode(self):
        """这一局是不是**闯关**（房间描述符 type == 2，见 SESSION_TYPE_QUEST）。"""
        room = self.lobby_room()
        if room is not None:
            return room.session_type == SESSION_TYPE_QUEST
        # 没有大厅房间时看自己解析到的建房请求；再没有就按闯关算
        # （V0.1 的单机主线就是闯关，保持老行为）。
        return int((self.room or {}).get("session_type",
                                         SESSION_TYPE_QUEST)) == SESSION_TYPE_QUEST

    def pvp_game_mode(self):
        """普通对战房的游戏模式号（描述符 `arguments[1]`）。

        参数缺失 / 损坏的调试路径退回夺分模式，保持会话 25 以前的老行为；
        正常客户端建的 type 1 房固定有三个参数，不会走这个兜底。
        """
        room = self.lobby_room()
        if room is not None:
            arguments = room.arguments or ()
        else:
            arguments = (self.room or {}).get("arguments") or ()
        if len(arguments) <= 1:
            return PVP_MODE_DEATHMATCH
        try:
            return int(arguments[1])
        except (TypeError, ValueError):
            return PVP_MODE_DEATHMATCH

    def item_mode(self):
        """这一局是不是**道具模式**（아이템전，§190）。

        判据全在 `lobby.item_mode_of` 里，和客户端 `0x409dd9` 同一个口径。
        不在房间里（协议试探 / 控制通道手搓包）时按自己解析到的建房请求算，
        解析不到就是「不是」—— 单机闯关那条老路一个字节都不受影响。
        """
        room = self.lobby_room()
        if room is not None:
            return room.item_mode()
        request = self.room or {}
        return item_mode_of(request.get("session_type", SESSION_TYPE_QUEST),
                            request.get("arguments", ()))

    def maybe_spawn_item(self, now=None):
        """道具模式：到点了就往地图上刷一件道具，并广播给全房间（§191 / D109）。

        ★ **原版服务端的活儿**：地图上的道具没有任何客户端来源 ——
        `.map` 文件里一件都没放，客户端也没有任何一处会自己请求生成
        （唯一能传任意物件 id 的口子是关卡脚本的 lua 绑定，而 PvP 图没有脚本）。
        服务端不发，玩家就「找不到道具」。

        用的是 `0x0404 gspCreatedItem` 而**不是** `0x0413 gspCreateObject`
        （D109）：前者客户端会先把坐标取模进地图、再把埋进地形的物件顶到
        空地上，所以服务端不需要知道任何地图几何。

        返回真的刷出来了没有。
        """
        room = self.lobby_room()
        if not room_in_battle(room):
            return False
        if not self.item_mode():
            return False
        quest = self.quest_state()
        if quest.settled or quest.pvp_reason is not None:
            return False
        spawn = quest.due_item_spawn(
            now=now, team_mode=room.team_layout() == TEAM_LAYOUT_TEAMS)
        if spawn is None:
            return False
        handle, item_id, x, y = spawn
        fields = (item_id, x, y, 0.0, 0.0) + ITEM_SPAWN_TAIL
        self.log(f"← 刷道具 gspCreatedItem(0x0404) 句柄=0x{handle:08x} "
                 f"物件={item_id} {ITEM_NAMES.get(item_id, '未知物件')} "
                 f"@ ({x:.0f}, {y:.0f})；地图上现有 "
                 f"{len(quest.items_on_map)}/{ITEM_SPAWN_MAX_ALIVE} 件")
        self.battle_broadcast(
            build_game(OP_CREATED_ITEM, build_created_item(handle, fields)),
            reason="：道具模式刷新")
        return True

    def battle_broadcast(self, plain, reason="", exclude=None):
        """把一个已经组好帧的战斗包发给房里**每一个人（含自己）**。

        和 `broadcast()` 的区别就是「含自己」：死亡 / 重生 / 掉落 / 拾取 /
        分数这几条，发包的那个人自己也必须收到应答才会动作（V0.1 §108 起
        的四条链），所以不能沿用大厅那个默认排除自己的广播。

        返回实际发出去的份数。
        """
        sent = 0
        for member in self.battle_members():
            if member is exclude:
                continue
            if getattr(member, "send_broken", False):
                continue            # 发送流已废（等它的读线程收尾拆连接）
            try:
                member.send(plain)
                sent += 1
            except OSError as error:
                member.log(f"   战斗广播发送失败（{error!r}），忽略")
        if reason and sent > 1:
            self.log(f"   → 广播给房里 {sent} 人{reason}")
        return sent

    def seat_snapshot(self):
        """按存档给自己做一个座位快照（昵称 / 等级 / 角色）。

        房间里别人的连接查不到我的存档，所以进房间的那一刻就把这三样存进
        `lobby.Seat`，后面广播直接读它。
        """
        return Seat(self,
                    username=self.account_name or "",
                    nickname=(display_name(self.account)
                              or (self.account_name or "")),
                    level=player_level(self.account),
                    character_id=player_character(self.account))

    def refresh_seat(self):
        """把大厅里我这个座位的昵称/等级/角色刷成存档里的当前值。"""
        room = self.lobby_room()
        if room is None:
            return None
        seat = room.seat_of(self)
        if seat is not None:
            seat.update(nickname=(display_name(self.account)
                                  or (self.account_name or "")),
                        level=player_level(self.account),
                        character_id=player_character(self.account))
        return seat

    def broadcast(self, plain, exclude_self=True, reason=""):
        """把一个已经组好帧的包发给同房间的其他人。

        ★ 广播时**每个连接各自合并一次**，绝不跨连接攒（V0.1 §120 / D058）。
        这里一个包一次 `send()`，多包合并由调用方在**每个目标连接上**分别用
        `send_batch()` 完成。

        发送前先把目标列表取成快照 —— `send()` 会阻塞在 socket 上，
        绝不能拿着大厅锁去阻塞。
        """
        room = self.lobby_room()
        if room is None:
            return 0
        targets = room.members(exclude=self if exclude_self else None)
        sent = 0
        for other in targets:
            if getattr(other, "send_broken", False):
                continue            # 发送流已废（等它的读线程收尾拆连接）
            try:
                other.send(plain)
                sent += 1
            except OSError as error:
                other.log(f"   广播发送失败（{error!r}），忽略")
        if sent and reason:
            self.log(f"   → 广播给房里另外 {sent} 人{reason}")
        return sent

    def send_update_session(self, map_name=None, random_map=None):
        """把当前房间的 `Session` 下发给客户端（opcode 0x0303）。

        没解出建房请求就不发：这个包的唯一作用就是把请求里那份描述符原样
        灌回客户端，凭空编一个类型只会让客户端进错房间面板。

        `map_name` 只有在**回应 0x0302** 时才填。建房那一次必须留空，
        理由见 `build_update_session` 的文档。加入别人的房间时要填**房间当前
        的地图名**，否则进去看到的是「还没选关卡」。
        """
        if self.room is None:
            self.log("   没有已解析的建房请求; 不下发 0x0303")
            return
        room = self.lobby_room()
        if map_name is None:
            map_name = "" if room is None else room.map_name
        if random_map is None:
            random_map = False if room is None else room.random_map
        player_count = 1 if room is None else room.player_count()
        status = SESSION_STATUS_WAITING if room is None else room.status
        # ★★ 局号：房里所有人共用一个数（§218 / D138）。中途进房的人就是靠
        #    这一发和全房间对上的 —— 客户端拿它和每一发同步包硬比，不等整包丢。
        game_id = (relayserver.EPOCH_UNSET if room is None
                   else room.epoch_value)
        try:
            payload = build_update_session(
                self.room["session_type"],
                self.room["arguments"],
                title=self.room["texts"][0],
                map_name=map_name,
                status=status,
                player_count=player_count,
                game_id=game_id,
                random_map=random_map,
            )
        except ValueError as error:
            self.log(f"   无法下发 0x0303: {error}")
            return
        self.log(
            f"← 回 0x0303 Session(type={self.room['session_type']} "
            f"({self.room['session_type_name']}) args={self.room['arguments']} "
            f"title={self.room['texts'][0]!r} map={map_name!r} "
            f"随机图={'开' if random_map else '关'} "
            f"人数={player_count} 局号={game_id})")
        self.send(build_game(OP_UPDATE_SESSION, payload))

    def send_session_members(self, host_seat=None):
        """把房间座位快照下发给客户端（opcode 0x0300）。

        客户端建房时只自己写了「座位 0 已占用」和房主座位号，昵称/等级等
        字段一个都没填（`0x54f807` 起的三条指令）。房间里按「F5 游戏开始」
        时的准入检查读的正是**房主座位里的等级**，所以不发这个包就一定弹
        「等级太低，无法选择任务。」——与 `gspRepLogin` 下发的账号等级无关
        （FINDINGS §75、§77）。

        座位数据取自大厅房间表（里程碑 I）；还没进大厅房间的场合（协议试探、
        控制通道）退回 V0.1 的行为：自己占 `host_seat`，其余五个空着。
        空座位发「未关闭」，这样 `0x556f40` 数出来的空位数和客户端
        `0x0302` 自报的一致。
        """
        room = self.lobby_room()
        if room is not None:
            self.refresh_seat()
            if host_seat is None:
                host_seat = room.host_seat
            seats = room.seat_snapshots()
            who = ", ".join(
                f"{i}:{s['nickname']}" for i, s in enumerate(seats)
                if s.get("occupied"))
            self.log(f"← 回 0x0300 房间座位快照(房间 #{room.room_id} "
                     f"host_seat={host_seat} 座位: {who})")
        else:
            if host_seat is None:
                host_seat = self.my_seat if 0 <= self.my_seat < ROOM_SEAT_COUNT else 0
            level = player_level(self.account)
            nickname = display_name(self.account) or (self.account_name or "")
            character = player_character(self.account)
            seats = [{"occupied": False}] * ROOM_SEAT_COUNT
            seats[host_seat] = {
                "occupied": True,
                "nickname": nickname,
                "level": level,
                "character_id": character,
                # 没有大厅房间（协议试探 / 控制通道）时只有「我」一个座位，
                # 分不分队都无所谓，给 1 队即可。
                "team": TEAM_A,
            }
            self.log(f"← 回 0x0300 房间座位快照(host_seat={host_seat} "
                     f"nickname={nickname!r} level={level} 角色={character})")
        self.send(build_game(OP_SESSION_MEMBERS,
                             build_session_members(host_seat, seats)))

    def send_slot_equipped_list(self, seat_index=None, reason=""):
        """把「这个座位持有哪些物品」下发给客户端（opcode 0x030b）。

        ★ 不发这一发，房间右下角的「人物选择」永远只有 3 个基础角色 ——
        11 个商城角色全部卡在客户端的持有判定上（FINDINGS §119）。

        **必须排在 `0x0300` 之后**：持有判定 `0x4070da` 第一步就是
        `0x4045f9` 查「我的座位已占用吗」，那个标记只有 `0x0300` 会写。
        """
        if seat_index is None:
            seat_index = self.my_seat
        item_ids = character_item_ids(self.account)
        characters = owned_characters(self.account)
        self.log(f"← 回 0x030b 座位 {seat_index} 物品清单("
                 f"{len(item_ids)} 件; 商城角色 {characters}"
                 f"{'，全开' if character_unlock_all(self.account) else '，按存档'}"
                 f"){reason}")
        try:
            payload = build_slot_equipped_list(seat_index, item_ids)
        except ValueError as error:
            self.log(f"   无法下发 0x030b: {error}")
            return
        self.send(build_game(OP_SLOT_EQUIPPED_LIST, payload))

    def broadcast_seat_slot(self, room, seat_index, action, reason):
        """把某个座位**当前的服务端快照**用 `0x0301` 发给房里每一个人（含自己）。

        「含自己」是必须的：换角色和变更队伍两条路，客户端都是改一份副本
        就发出来、自己一动不动，等的就是这一发（§103 / §165）。
        """
        seat = room.seats[seat_index]
        if seat is None:
            return
        packet = build_game(OP_SESSION_MEMBER_UPDATE,
                            build_session_member_update(seat_index, action,
                                                        **seat.snapshot()))
        self.send(packet)
        self.broadcast(packet, reason=reason)

    def on_seat_change(self, payload):
        """客户端方向的 `0x0301` —— 房间里换角色，**或者变更队伍**。

        客户端把改好的整个座位报上来之后**自己不动**，等服务端广播。
        服务端存下新值，再把同一个座位发回去，客户端才会真的动
        （FINDINGS §103 / §165）。

        ★ **两条路载荷形状完全一样，只能靠「哪个字段变了」区分**：

            `team`（+0x08）和服务端存的不一样  -> 变更队伍，回 **action 3**
            其余                               -> 换角色，  回 **action 4**

        action 4 会播一行「%s님이 %s 캐릭터로 선택되었습니다.」（`0x406520`），
        所以变更队伍**绝不能**用它 —— 用户报的「点变更队伍冒出一条和换角色
        一样的韩文」就是这么来的。action 3 走 `0x406628`：把包里的座位数据
        灌进去、重建模型、刷房间 UI，一个字都不播，正是我们要的。

        座位里除「变了的那一样」以外的字段以服务端为准（昵称/等级来自存档），
        免得客户端自报的旧值把数据栏刷回去。
        """
        try:
            seat_index, slot = parse_seat_change_request(payload)
        except (ValueError, struct.error) as error:
            self.log(f"   座位变更请求解析失败: {error}; 不回包")
            return
        if not slot.get("occupied"):
            self.log("   座位未占用; 不回包")
            return
        character = slot.get("character_id", 0)
        team = slot.get("team", 0)
        room = self.lobby_room()
        my_seat = None if room is None else room.seat_index_of(self)

        # ---- 是变更队伍吗？----------------------------------------------
        # 判据：**角色 id 没变、而队伍那一格变了**。两条路发的都是座位的实况
        # 副本，所以「没动的字段」必然等于服务端上一次发下去的值。
        # ★ 角色优先：两样都对不上时按换角色算（V0.1 起就只有这一条路，
        #   而队伍判错的代价是玩家换不了角色）。
        target_seat = None
        if room is not None and 0 <= seat_index < ROOM_SEAT_COUNT:
            target_seat = room.seats[seat_index]
        if (target_seat is not None
                and int(team) != int(target_seat.team)
                and int(character) == int(target_seat.character_id)):
            # 客户端自己也判过一次「不是房主就只能改自己那格」（`0x469f4f`），
            # 服务端再判一次 —— 客户端的判定不是安全边界。
            if seat_index != my_seat and room.host_seat != my_seat:
                self.log(f"   变更队伍: 座位 {seat_index} 不是自己的"
                         f"（我在 {my_seat}）且我不是房主; 不回包")
                return
            if int(team) not in (TEAM_A, TEAM_B):
                self.log(f"   变更队伍: 队伍号 {team} 不是 1 / 2; 不回包")
                return
            old = target_seat.team
            target_seat.update(team=team)
            self.log(f"   变更队伍: 座位 {seat_index} 队伍 {old} -> {team}"
                     f"（{target_seat.nickname!r}）")
            self.log(f"← 回 0x0301 座位变更(action=3 重建, 座位={seat_index}, "
                     f"队伍={team}) —— 变更队伍不能用 action 4，那会播换角色的提示")
            self.broadcast_seat_slot(room, seat_index, SEAT_ACTION_RESYNC,
                                     reason="：变更队伍")
            return

        # ---- 剩下的都当换角色 --------------------------------------------
        self.log(f"   换角色: 座位 {seat_index} -> 角色 id {character} "
                 f"(昵称={slot.get('nickname')!r} 等级={slot.get('level')})")
        if self.account_name:
            try:
                self.account = self.accounts.set_character(self.account_name,
                                                           character)
            except KeyError:
                self.log(f"   存档里没有账号 {self.account_name!r}; 只广播不记账")
        # 这是唯一一个「客户端主动告诉服务端自己坐在几号位」的包，顺手校准一下
        # （建房时客户端固定把自己放在座位 0，§75）。★ 但**以大厅里的实际座位
        # 为准** —— 进别人房间时客户端报的是它自己那份可能还没更新的座位号，
        # 信它会让广播打到别人的位置上。
        if my_seat is not None and my_seat != seat_index:
            self.log(f"   客户端自报座位 {seat_index}，"
                     f"以大厅里的实际座位 {my_seat} 为准")
            seat_index = my_seat
        self.my_seat = seat_index
        seat = self.refresh_seat()
        self.log(f"← 回 0x0301 座位变更(action=4 换角色, 座位={seat_index}, "
                 f"角色={player_character(self.account)})")
        if room is not None and seat is not None:
            # 队伍 / 准备状态要跟着一起发回去，否则这一发会把它们抹成 0。
            self.broadcast_seat_slot(room, seat_index,
                                     SEAT_ACTION_CHANGE_CHARACTER,
                                     reason="：换角色")
            return
        packet = build_game(OP_SESSION_MEMBER_UPDATE, build_session_member_update(
            seat_index,
            SEAT_ACTION_CHANGE_CHARACTER,
            occupied=True,
            nickname=display_name(self.account) or (self.account_name or ""),
            level=player_level(self.account),
            character_id=player_character(self.account),
        ))
        self.send(packet)
        # 房里其他人也要看到这次换角色（中下那个 3D 预览就是靠这一发换的，§103）。
        self.broadcast(packet, reason="：换角色")

    def on_toggle_ready(self, payload):
        """客户端方向的 `0x030e` —— 房间里按「游戏准备 / READY」（§165）。

        载荷只有一个 int32（新的准备状态），**不带座位号** —— 谁发的就是谁。
        客户端**自己已经把 `[座位+0x2e]` 改好了**，所以按的人立刻能看到
        自己那行「准备中」；房里其他人要看到，只能靠服务端把这个座位广播
        回去（用户报的第 2 条就是缺了这一发）。

        ★ 顺带这也是「房主为什么按不动开始」的原因：房主的客户端在
        `0x4696cd` 里数的是**它本地那份座位数据**，服务端不广播，
        它就永远只数得到自己一个人，于是弹「半数以上玩家处于准备状态下才可
        开始游戏」。
        """
        try:
            reader = Reader(payload)
            ready = bool(reader.i32())
            if reader.left():
                raise ValueError(f"ready payload has {reader.left()} trailing bytes")
        except (ValueError, struct.error) as error:
            self.log(f"   准备状态请求解析失败: {error}; 不回包")
            return
        room = self.lobby_room()
        seat_index = None if room is None else room.seat_index_of(self)
        if room is None or seat_index is None:
            self.log(f"   准备状态 -> {ready}，但不在任何房间里; 不回包")
            return
        seat = room.seats[seat_index]
        seat.update(ready=ready)
        self.log(f"   游戏准备: 座位 {seat_index}（{seat.nickname!r}）"
                 f"-> {'已准备' if ready else '取消准备'}")
        self.log(f"← 回 0x0301 座位变更(action=3 重建, 座位={seat_index}, "
                 f"准备={ready}) —— 房里其他人的「准备中」标记靠这一发")
        self.broadcast_seat_slot(room, seat_index, SEAT_ACTION_RESYNC,
                                 reason="：游戏准备")

    def on_game_login(self, payload):
        """`0x0100 gcpReqLogin` —— 用认证服签发的票据认人，然后回 `gspRepLogin`。

        载荷第一个字段就是票据（认证服放进 `CULoginReplyPacket` 的字符串字段，
        客户端原样转发，FINDINGS §123）。V0.1 靠 `accounts.json` 里的
        `active_account` 认人，那只在「全世界只有一个玩家」时成立。

        ★ 票据查不到就**拒绝登录**，绝不回退到「随便给个本地账号」——
        那等于谁都能顶别人的名字进来。

        ★★ **认不出来的票据回 `result=2`，不是 3**（D097）。三种情况都会走到这里，
        而玩家该做的事完全一样（重新登录一次）：

        | 情况 | 什么时候发生 |
        |---|---|
        | 被顶号 | 同一个账号在别处登录，旧客户端自动重连重放旧票据（§132）|
        | 过期 | 票据签发后一直没用，或网络断得太久（超过 `BOUND_TTL_SECONDS`）|
        | **服务端重启过** | 票据只在内存里（D097），重启后全部作废，而客户端会拿旧票据来重连（§171）|

        `result=2` 的客户端文案是「现有连接已断开。请重新尝试连接。」，
        三种情况都说得通；`result=3` 那句「在无法连接的地方尝试了连接。」
        只会让人以为是被封 IP / 连错服务器。**只有「压根没带票据」才回 3。**
        """
        if getattr(self, "version_rejected", False):
            # 握手时已经被版本门禁拒了。正常情况下客户端收到非零结果码会走
            # 原版自带的升级/报错分支弹框并停下；这一层是兜底 —— 万一它没停、
            # 还是把 gcpReqLogin 发过来了，绝不放它进大厅。回 2（D097 同款
            # 理由：三句固定文案里「请重新尝试连接」最不误导）。
            self.log("✗ 版本门禁已拒，gcpReqLogin 兜底拦截：回 "
                     f"gspRepLogin(result={LOGIN_RESULT_SUPERSEDED}) 并断开")
            self.online(f"✗ 登录被拒 账号=? ip={self.peer()} "
                        f"原因=客户端版本过旧（兜底拦截）")
            if not self.args.hold:
                self.send(build_game(OP_REP_LOGIN,
                                     build_gsp_rep_login(LOGIN_RESULT_SUPERSEDED)))
                self.close_now()
            return
        try:
            ticket = Reader(payload).wstr()
        except Exception:
            ticket = ""
        if not str(ticket or "").strip():
            # 连票据字段都是空的：这不是重连，是协议级错误（手搓包 / 试探）。
            self.log(f"✗ gcpReqLogin 没带票据；"
                     f"回 gspRepLogin(result={LOGIN_RESULT_BAD_TICKET}) 断开")
            self.online(f"✗ 登录被拒 账号=? ip={self.peer()} 原因=没带票据")
            self.send(build_game(OP_REP_LOGIN,
                                 build_gsp_rep_login(LOGIN_RESULT_BAD_TICKET)))
            return
        username = self.tickets.resolve(ticket)
        if username is None:
            # 顶号那一路能报出账号名（`_revoked` 表里记着），日志里说清楚是谁；
            # 其余（过期 / 服务端重启过）报不出账号，但回给客户端的码一样。
            revoked = self.tickets.revoked_reason(ticket)
            who = f"账号={revoked[0]!r}" if revoked else "账号=?"
            why = ("账号已在别处登录" if revoked
                   else "票据已过期或服务端重启过（票据只在内存里）")
            self.log(f"✗ gcpReqLogin 的票据认不出来（{short_ticket(ticket)}，{why}）；"
                     f"回 gspRepLogin(result={LOGIN_RESULT_SUPERSEDED}) "
                     f"→ 客户端提示「现有连接已断开。请重新尝试连接。」")
            self.online(f"✗ 登录被拒 {who} ip={self.peer()} 原因={why}")
            self.send(build_game(OP_REP_LOGIN,
                                 build_gsp_rep_login(LOGIN_RESULT_SUPERSEDED)))
            return
        name, account = self.accounts.get_account(username)
        if account is None:
            # 票据有效但账号在这期间被删了（存档转移助手可能改过 JSON）。
            self.log(f"✗ 票据 {short_ticket(ticket)} 指向的账号 {username!r} 已不存在；"
                     f"回 gspRepLogin(result={LOGIN_RESULT_BAD_TICKET}) 断开")
            self.online(f"✗ 登录被拒 账号={username!r} ip={self.peer()} 原因=账号已不存在")
            self.send(build_game(OP_REP_LOGIN,
                                 build_gsp_rep_login(LOGIN_RESULT_BAD_TICKET)))
            return
        # 同一个账号在别处已经连着：把旧连接踢掉。不这么做的话两条连接会
        # 同时往同一份存档里写，谁最后写谁赢。
        for other in conns_for_user(name):
            if other is not self:
                other.log(f"⚠ 账号 {name!r} 在别处重新登录，本连接被顶掉")
                other.online(f"⚠ 被顶号 账号={name!r} ip={other.peer()} "
                             f"顶它的是 ip={self.peer()}")
                other.close_now()
        self.account_name, self.account = name, account
        # ★ 这张票据从现在起也是这个玩家的**重连凭证**：服务端一断，客户端会
        #   自己反复重连并原样重放它（§171），全程不回认证服。所以要把它标成
        #   「已登进游戏服」换一个长得多的有效期，并落盘扛住服务端重启（D096）。
        self.tickets.bind(ticket)
        # UDP 同步那条流拿这张票据认人（`udpsync._on_hello`）。记在连接上，
        # 这样「票据 -> 哪条游戏连接」不用再翻一遍 TicketStore。
        self.login_ticket = ticket
        self.online(f"✓ 登录 账号={name!r} ip={self.peer()} "
                    f"等级={player_level(account)} 金币={player_money(account)}")
        state = tutorial_state(self.account)
        self.log(f"← 回 gspRepLogin(result={self.args.login_result}) "
                 f"账号={self.account_name!r} 票据={short_ticket(ticket)} "
                 f"stored_level={self.account.get('level', 1)} "
                 f"下发等级={player_level(self.account)} "
                 f"经验={player_experience(self.account)} "
                 f"金币={player_money(self.account)} "
                 f"tutorial_completed={self.account.get('tutorial_completed', False)} "
                 f"(客户端状态={state})")
        self.send(build_game(OP_REP_LOGIN, build_gsp_rep_login(
            self.args.login_result, self.account,
            self.channel_code, self.channel_index)))
        # 登录包三个字符串字段一个都没落到「我自己的名字」上（`0x54f2cc` 只把
        # 第三个存进只写不读的 `0x72e37c`），所以要单独补一发 0x0102。
        self.send_player_name(reason="（登录后下发）")
        # 登录包带得动等级和经验，唯独带不动金币（`0x54f2cc` 不写 0x72e330）。
        # 补一发 0x0600，右上角数据栏才和存档完全一致。
        self.send_rep_money(reason="（登录后补发，登录包没有金币字段）")
        # 每一关的「已达成难度」。这张 map 只有服务端能填，不发就等于
        # 全部关卡只有「简单」能开局（§118）。
        self.send_quest_reached_difficulty(reason="（登录后下发）")

    def close_now(self):
        """立刻切断这条连接（被顶号、被踢时用）。`run()` 的收包循环会自己收尾。"""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def reload_account(self):
        """从盘上重读当前账号。用户手改 accounts.json 之后不必重登游戏。"""
        if not self.account_name:
            return
        name, account = self.accounts.get_account(self.account_name)
        if account is not None:
            self.account_name, self.account = name, account

    def my_nickname(self):
        """「我」在别人眼里叫什么。和座位里那一格（`build_session_slot`）同源。"""
        return display_name(self.account) or (self.account_name or "")

    def send_player_name(self, reason=""):
        """把「你自己叫什么」下发给客户端（opcode 0x0102，§222）。

        ★ 不发这一发，客户端全局 `0x72e328` 永远是空串，建房对话框的默认
        房间名就变成「与一起游戏吧!」（格式串 `与%s一起游戏吧!` 的 `%s`
        取的正是这个全局）。写它的只有 `0x54f23a`（= 收 0x0102）那个函数体。
        """
        nickname = self.my_nickname()
        self.log(f"← 回 gspSetPlayerName(0x0102) 昵称={nickname!r}"
                 f" —— 建房默认房名的那个 %s{reason}")
        self.send(build_game(OP_SET_PLAYER_NAME,
                             build_set_player_name(nickname)))

    def send_rep_money(self, reason=""):
        """把存档里的金币/经验/等级下发（opcode 0x0600）。

        登录包带不动金币（`0x54f2cc` 不写 `0x72e330`），所以每次要让右上角
        玩家数据栏和存档对齐，都得补这一发。
        """
        experience = player_experience(self.account)
        level_start_exp, next_level_exp = experience_bounds(experience)
        self.log(f"← 回 gspRepMoney(金币={player_money(self.account)} "
                 f"经验={experience} 本级 {level_start_exp}..{next_level_exp} "
                 f"等级={player_level(self.account)}){reason}")
        self.send(build_game(OP_REP_MONEY, build_rep_money_for(self.account)))

    def send_quest_reached_difficulty(self, reason=""):
        """把每一关「已达成难度」的全量快照下发（opcode 0x020c）。

        ★ 不发这一发，房间里选「普通 / 困难」按开始就弹「无法进行的难度，
        请降低难度」—— 客户端那张 map 只有服务端能填（FINDINGS §118）。
        """
        records = quest_difficulty_records(self.account)
        self.log(f"← 回 gspQuestReachedDifficulty({len(records)} 关: "
                 f"{ {qid: lv for qid, lv in sorted(records.items())} }"
                 f"{'，全开' if quest_unlock_all(self.account) else '，逐级解锁'}"
                 f"){reason}")
        self.send(build_game(OP_QUEST_REACHED_DIFFICULTY,
                             build_quest_reached_difficulty(records)))

    def current_quest(self):
        """当前房间的 `(关卡 id, 难度)`；不是闯关房或参数不全就返回 None。

        闯关房的描述符是 type=2 + 两个参数 `(关卡 id, 难度)`（§68），
        建房时由客户端发上来、我们原样存进 `self.room`。

        ★ `self.room` 只有**房主**有（它是 `0x0201` 建房请求的解析结果）。
        后进房的人手上没有，所以先看大厅里那一份 —— 建房和 `0x0302` 选地图
        都会把描述符同步进去，房里每个人读到的都一样。不这么做的话，
        「客人打通了关卡但难度没解锁」（J.3 多人才会暴露的坑）。
        """
        lobby_room = self.lobby_room()
        if lobby_room is not None:
            if lobby_room.session_type != SESSION_TYPE_QUEST:
                return None
            arguments = lobby_room.arguments or ()
            if len(arguments) >= 2:
                try:
                    return int(arguments[0]), int(arguments[1])
                except (TypeError, ValueError):
                    return None
            # 大厅那份参数不全（老路径/调试造的房）时退回自己解析的那一份。
        room = self.room or {}
        if room.get("session_type") != SESSION_TYPE_QUEST:
            return None
        arguments = room.get("arguments") or ()
        if len(arguments) < 2:
            return None
        try:
            return int(arguments[0]), int(arguments[1])
        except (TypeError, ValueError):
            return None

    def record_quest_clear(self):
        """通关入账：把「这一关的这个难度打通了」记进存档并重发 0x020c。

        只有通关（客户端 `0x0417` 报 True）才调。记完如果解锁的难度真的往上
        走了一格，就立刻重发一次全量快照 —— 结算完回房间就能直接选新难度，
        不用重登。
        """
        quest = self.current_quest()
        if quest is None or not self.account_name:
            return
        quest_id, difficulty = quest
        if difficulty <= quest_cleared_difficulty(self.account, quest_id):
            return
        try:
            self.account = self.accounts.set_quest_cleared(
                self.account_name, quest_id, difficulty)
        except KeyError:
            self.log(f"   存档里没有账号 {self.account_name!r}；通关难度未记账")
            return
        unlocked = min(difficulty + 1, QUEST_DIFFICULTY_MAX)
        self.log(f"   ★ 关卡 {quest_id} 难度 {difficulty} 通关入账 "
                 f"-> 可选难度上限 {unlocked}")
        self.send_quest_reached_difficulty(reason="（通关解锁新难度）")

    def on_first_user_result(self, payload):
        """客户端跑完新手教程后上报的进度值，落盘到 accounts.json。"""
        try:
            progress = parse_first_user_result(payload)
        except (ValueError, struct.error) as error:
            self.log(f"   教程进度上报解析失败: {error}; 不记账")
            return
        if not self.account_name:
            self.log(f"   教程进度 {progress} 无账号可记")
            return
        try:
            self.account = self.accounts.set_tutorial_progress(
                self.account_name, progress)
        except KeyError:
            self.log(f"   存档里没有账号 {self.account_name!r}；教程进度未记账")
            return
        self.log(f"   ★ 教程完成上报 progress={progress} -> 存档 "
                 f"tutorial_completed={self.account['tutorial_completed']} "
                 f"(下次登录下发状态 {tutorial_state(self.account)})")

    def battle_teams(self):
        """`(队伍表, 是不是组队战)` —— 判胜负和记分都要的两样东西。

        队伍表 = `{座位号: 队伍号}`，**只含还在座的座位**。客户端那边
        `0x4045f9`（读描述符 `[desc + 座位*0x3c + 0x40]`）判定座位有没有人
        用的是同一份快照，所以「谁还在」两边是一致的（§224）。

        不在房间里（协议试探 / 控制通道手搓包）时返回 `(None, False)` ——
        调用方据此退回「没有房间信息」的老行为，别拿空字典当「全都不在座」。
        """
        room = self.lobby_room()
        if room is None:
            return None, False
        teams = {i: seat.team for i, seat in enumerate(room.seats)
                 if seat is not None}
        return teams, room.team_layout() == TEAM_LAYOUT_TEAMS

    def check_pvp_finished(self, now=None):
        """对战这一局打完了没有？打完了就**由服务端**结算（§167）。

        ★ 为什么非服务端不可：客户端自带的结束链在对战里跑不到
        （`GameContextQuest.state` 永远不是 2，见 `PVP_TIME_LIMIT_MS` 的注释），
        所以整局既不会发 `0x040f`，也不会自己进结算界面 —— 用户报的
        「分出胜负后无法退出返回房间」。

        闯关（type 2）不走这里：那一路客户端会自己发 `0x040f`，V0.1 起就跑通了。

        调用点有三处，都很便宜：死亡广播之后、每收到一发 `0x040e`
        （约 8 Hz，用来盯时间上限）、以及有人中途退房之后。
        """
        room = self.lobby_room()
        if not room_in_battle(room):
            return False
        if self.quest_mode():
            return False
        quest = self.quest_state()
        if quest.settled or quest.pvp_reason is not None:
            return False
        seats = self.battle_seats()
        teams, team_mode = self.battle_teams()
        game_mode = self.pvp_game_mode()
        if game_mode in (PVP_MODE_SURVIVAL, PVP_MODE_FIGHT):
            time_limit = (PVP_FIGHT_TIME_LIMIT_MS
                          if game_mode == PVP_MODE_FIGHT
                          else PVP_SURVIVAL_TIME_LIMIT_MS)
            reason = quest.survival_finished(
                seats, teams, team_mode=team_mode,
                time_limit_ms=time_limit, now=now)
            detail = ("剩余生命 "
                      f"{ {seat: quest.remaining_lives(seat) for seat in seats} }")
            rule = "生存"
        else:
            # 当前中文客户端房间列表可见的另一项是模式 3（夺分）。模式 1
            # 的 TimeAttack 仍沿用旧兜底；它在这版 UI 中不可选，另案再还原。
            # ★ 胜利线按**开局那一刻**的人数算（§220 / D139）：客户端右上角
            #   那个「MAX N」在建关卡时就定死了，中途掉线不会重算，
            #   服务端跟着现在的人数往下调就会和 HUD 对不上。
            limit = quest.score_limit(seats, team_mode)
            reason = quest.pvp_finished(seats, teams, limit,
                                        team_mode=team_mode, now=now)
            detail = (f"杀敌数 {quest.kills}；胜利线 {limit} 分"
                      f"（按开局 {len(quest.start_seats) or len(seats)} 人算）")
            rule = "夺分"
        if reason is None:
            return False
        quest.pvp_reason = reason
        self.log(f"   ★ {rule}模式结束：{reason}；{detail} "
                 f"—— 服务端主动结算（客户端在对战里不会自己发 0x040f，§167）")
        self.send_end_game()
        return True

    def on_req_user_list(self, payload):
        """`0x020d gcpReqUserList` —— 大厅右侧「玩家列表」要一页在线玩家。

        ★ **应答是 `0x0212`，不是同号回显**（§166）。回 `0x020d` 只会喂给
        `0x553c5f` 那个弹窗对象，列表永远是空的 —— 用户报的
        「大厅右侧玩家列表看不见其他人」就是这么来的。

        页号 / 每页几条 / 过滤开关**原样回显**，客户端翻页全靠它。

        ★ 过滤开关（第 5 个字节）**要真的过滤**（§169 / D095）：

        | 值 | 大厅右下那两个按钮 | 我们回什么 |
        |---|---|---|
        | 1（客户端默认）| 「待机玩家」| 只有**没在打游戏**的人 |
        | 0 | 「推荐对手」| 全部在线的人，按**等级和我接近**排前面 |

        两档都**不含自己**（D095）。
        """
        try:
            request = parse_user_list_request(payload)
        except (ValueError, struct.error, IndexError) as error:
            self.log(f"   用户列表请求解析失败: {error}; 按默认参数回一页")
            request = {"page": 0, "page_size": 18,
                       "flag": USER_LIST_FILTER_WAITING}
        page = max(0, int(request["page"]))
        page_size = int(request["page_size"]) or 18
        flag = int(request["flag"])
        waiting_only = (flag != USER_LIST_FILTER_RECOMMENDED)
        everyone = online_user_snapshots(viewer=self, waiting_only=waiting_only)
        if not waiting_only:
            # 「推荐对手」：原版服务端怎么挑对手已经无从得知（D095），
            # 拿「等级和我最接近」排序 —— 名单不会因此少一个人，只是顺序变了。
            my_level = player_level(self.account or {})
            everyone.sort(key=lambda item: (abs(item[2] - my_level), item[0]))
        start = page * page_size
        chunk = everyone[start:start + page_size]
        mode = "待机玩家" if waiting_only else "推荐对手"
        self.log(f"← 回 0x0212 gspRepUserList({mode}; 第 {page} 页 "
                 f"每页 {page_size} 条; 能看见 {len(everyone)} 人，"
                 f"本页 {len(chunk)} 人: "
                 + "、".join(f"{u[0]}({'游戏中' if u[1] else '待机'}"
                             f" Lv.{u[2]})" for u in chunk) + ")")
        self.send(build_game(OP_REP_USER_LIST, build_rep_user_list_page(
            page, page_size, flag, chunk)))

    def respawn_position(self):
        """重生用的整数坐标。优先用客户端最近一次 `0x0406` 报的位置。

        `gspRespawnCharacter` 的坐标在线上是 int32（客户端 `fild` 转 float），
        而 `0x0406` 报的是 float，所以这里要截成整数。
        """
        if self.last_position is None:
            return (DEFAULT_RESPAWN_X, DEFAULT_RESPAWN_Y)
        x, y = self.last_position
        return (int(x), int(y))

    def battle_seat_index(self):
        """这条连接在房里坐哪个座位；不在房间 / 没入座返回 ``None``。

        和 `self.my_seat` 的区别：`my_seat` 是连接自己记的一份镜像，房间外的
        协议试探路径上也有值（默认 0）。要判「这一发是不是本人报的」必须用
        **大厅那份权威座位表**，不然房间外的调试路径会被误判成座位 0。
        """
        room = self.lobby_room()
        if room is None:
            return None
        return room.seat_index_of(self)

    def on_report_hp_zero(self, payload):
        """0x0408「我 HP 归零了」-> 回 0x0406 死亡广播，角色这才真的倒下。

        ★ **这是「血量归零后角色不死、卡住不能操作」的根治点**（§108）。
        在此之前服务端从来没回过这个包，`Character::OnHpZero` 报完就一直等，
        `[char+0x2b4]`（死亡标记）永远是 0，于是既不播死亡动画、也不进 5 秒
        重生倒计时，角色就冻在原地 —— 掉进岩浆同理（岩浆只是另一种伤害源）。

        回显策略：**把收到的 18 字节里的「死亡次数」那一格换成服务端的权威值，
        其余原样发回，并广播给房间里所有人（自己也收得到）**。
        死亡次数是服务端定的 —— 客户端报的是「我死之前死过几次」，服务端回的是
        「你现在死了几次」。**这一格就是 HUD 心形的数据源**（§109），
        照抄回去心形就永远不减少。

        ## 多人（J.3）

        - **广播出去是安全的**：读侧 `0x4938d2` 用 `World::Find(句柄)` 找角色，
          而玩家角色的句柄 = **座位 × 100000 + 100001**（`0x405f02` 写死的公式，
          §161），六台机器上完全一致；找不到就整包丢掉（`0x493914` 的 `je`），
          不会崩。所以「A 死了」这件事在 B 那边也能正确落到 A 的角色上。
        - **同一个句柄只广播一次**：怪物是各台机器各自模拟的，同一只怪可能被
          两个客户端同时报上来。广播两遍等于战绩表（`0x48c942`）多记一次死亡。

        ## ★★ 玩家的死亡只认**本人**上报（bug调查/8）

        每台客户端各自模拟全场的伤害。射手那台算「我的火箭溅射炸死了他」、
        受害者自己那台算「我躲过去了」的分歧是**必然**的（弹道和溅射各算各的）。
        旧逻辑对 `0x0408` 来者不拒、先到先广播，于是受害者的客户端会对
        **活着的自己**执行 `Die()` —— 用户报的「人还活着，画面却突然进了观战
        模式、左键变切视角」就是这个。实测一天的线上日志里 613 发玩家死亡广播
        有 46 发**受害者本人从头到尾没报过**（他那台机器上他压根没死）。

        谁死没死只有本人的 HP 模拟说了算，所以玩家座位（0..5）的上报**只接受
        「上报的连接就坐在那个座位」**的那一发；别人替报的一律忽略。
        真死了的话本人那发最多晚一个 RTT（TCP 可靠，实测同一次死亡各机相隔
        十几到几十毫秒），不会丢。**凶手那一格也跟着变准**：改由受害者本机
        `[char+0x158]` 提供，而不是自称打中了的那台。

        怪 / NPC（座位 0xff）**不受这条限制** —— 怪由控制者那台模拟，谁都
        可能替它报，去重仍然由 `RoomQuest.record_death` 负责。

        ★ 房间外的调试 / 协议试探路径（`battle_seat_index()` 返回 ``None``）
        保持老行为，不然手搓包那一路全被挡掉。
        """
        if getattr(self.args, "no_death_reply", False):
            self.log("   [no-death-reply] 收到 0x0408 但不回死亡广播")
            return
        try:
            info = parse_report_hp_zero(payload)
        except (ValueError, struct.error) as error:
            self.log(f"   0x0408 解析失败: {error}；不回死亡广播")
            return
        seat = info["seat"]
        who = f"玩家座位 {seat}" if 0 <= seat < 6 else f"NPC/怪物 (座位={seat})"
        reporter = self.battle_seat_index()
        if 0 <= seat < ROOM_SEAT_COUNT and reporter is not None and reporter != seat:
            self.log(f"   ✗ 幽灵死亡上报：{who} 是座位 {reporter} 替报的 —— "
                     f"本人那台机器上他没死，不广播（bug调查/8）")
            return
        quest = self.quest_state()
        deaths, first = quest.record_death(info["handle"], seat, info["deaths"])
        self.log(f"   HP 归零上报: {who} 句柄=0x{info['handle']:08x} "
                 f"凶手={info['arg']} 死亡次数={info['deaths']} "
                 f"位置=({info['x']:.0f}, {info['y']:.0f})")
        if not first:
            # 已经替这个句柄报过了。再广播一次就多记一次死亡。
            self.vlog(f"   句柄 0x{info['handle']:08x} 的死亡已经广播过；"
                      f"这一发忽略（多台机器各自模拟同一只怪，§161）")
            return
        # ★ 用**和解析同一个格式串**重新打包，别去手算「死亡次数在第几字节」——
        #   线上是紧凑的（死亡次数在偏移 6），客户端结构体里却在 +0x08，
        #   按后者去改包会写坏 X 并把死亡次数冲成六万多，客户端当场越界崩溃。
        reply = struct.pack(DEATH_REPORT_FORMAT, info["handle"],
                            info["seat"] & 0xFF, info["arg"], deaths,
                            info["x"], info["y"])
        self.deaths_broadcast += 1
        self.log(f"← 回 0x0406 死亡广播（第 {self.deaths_broadcast} 次）"
                 f" 死亡次数 -> {deaths}"
                 f" —— 客户端收到才会调 Character::Die()，心形也靠它减")
        self.battle_broadcast(build_game(OP_BROADCAST_DEATH, reply),
                              reason="：死亡广播")
        # ★ 上闩等他自己的 0x0413；到点没等到就由 `check_respawn_watchdog()`
        #   补一发 0x0419（bug调查/8「死了不复活」）。
        quest.arm_respawn_watchdog(seat, (info["x"], info["y"]),
                                   after=self.respawn_watchdog_seconds())
        # 对战里「杀敌数」就是分数：凶手那一格是开火者的座位号（§167）。
        # ★ 自杀 / 杀队友要**扣**一分 —— 客户端 `Character::Die` 就是这么算的，
        #   服务端不扣就会「HUD 写着 5、服务端已经数到 6」（§224）。
        teams, team_mode = self.battle_teams()
        delta = quest.record_kill(info["arg"], seat,
                                  teams=teams, team_mode=team_mode)
        if delta:
            change = "+1" if delta > 0 else "-1（自杀 / 杀队友要扣分，§224）"
            self.log(f"   对战计分: 座位 {info['arg']} 杀敌数 {change} -> "
                     f"{quest.kills[info['arg']]}")
        self.check_pvp_finished()

    def on_respawn_request(self, payload):
        """0x0413 gcpRespawnCharacter -> 原样回 0x0419，角色在自报的位置复活。

        角色倒下 5 秒后（`Die()` 在 `0x5019a8` 写 `[char+0x2d8] = now + 5000`），
        每帧的 `0x4fe78f` 走到 `0x4fe8d7` 发这个包。**坐标是客户端自己选好的
        重生点**（`0x4fe70e` -> `[char+0x2b0]`），所以原样回显一定合法，
        不会再出现 §88 那种「传送到地图边缘 + 0x0106 gcpReportHack」。

        ## 多人（J.3）：广播给全房间

        - **只有本人会请求**：`0x4fe70e` 有 `[char+0x2ac] != [LobbyStage+0x1cc]`
          这条判据，别人的角色不会替你发 `0x0413`。所以不需要去重。
        - **广播出去是安全的**：读侧 `0x553ecc` -> `GameContext::vf_d4`
          （`0x4931c2`）第一件事就是 `0x404ff6(座位)` = `[GameSession+座位*4+0x1d0]`
          按座位取角色，座位号跨机器一致；空座位直接 return（§161）。
          不广播的话，别人屏幕上你就一直躺着。
        """
        try:
            info = parse_respawn_request(payload)
        except (ValueError, struct.error) as error:
            self.log(f"   0x0413 解析失败: {error}；不回重生包")
            return
        self.respawn_sent += 1
        self.log(f"   重生请求: id={info['character_id']} "
                 f"坐标=({info['x']}, {info['y']}) "
                 f"重生点索引={info['spawn_index']}")
        # 本人的 0x0413 到了 -> 撤看门狗的闩，并把这个重生点记下来
        # （bug调查/8：以后要替别人补 0x0419 时就用它）。
        quest = self.quest_state()
        seat = self.battle_seat_index()
        if seat is None:
            seat = info["character_id"]
        quest.disarm_respawn_watchdog(seat)
        quest.remember_respawn_point(seat, info["x"], info["y"],
                                     info["spawn_index"])
        self.log(f"← 回 gspRespawnCharacter(0x0419) 原样回显"
                 f"（第 {self.respawn_sent} 次）")
        self.battle_broadcast(
            build_game(OP_RESPAWN_CHARACTER, build_respawn_character(
                info["character_id"], info["x"], info["y"],
                info["spawn_index"])),
            reason="：重生")

    def respawn_watchdog_seconds(self):
        """看门狗等多久（秒）。`--respawn-watchdog 0` = 整个兜底关掉。"""
        value = getattr(self.args, "respawn_watchdog", None)
        return RESPAWN_WATCHDOG_S if value is None else float(value)

    def check_respawn_watchdog(self, now=None):
        """★ 死了 8 秒还没发 `0x0413` 的人，由服务端补一发 `0x0419` 拉起来。

        ## 为什么需要它（bug调查/8）

        用户报的「3 人以上对战，有时人死了就再也不复活」。线上日志实锤：
        服务端**正确**广播了 `0x0406`，全房间的客户端也都收到了，可受害者
        那台从此**再没发过 `0x0413`** —— 他自己屏幕上是观战画面、还能聊天
        （实测有人一边卡着一边打字「我又观战了」「不能复活了」），别人屏幕上
        他就一直躺在地上，直到这一局结束。一天的日志里 15 次，全部集中在
        3 人以上的局。

        客户端那条链是 `Die()`（`0x5019a8` 写 `[char+0x2d8] = now + 5000`）
        -> 每帧 `0x4fe78f` -> `0x4fe8d7` 发包，中间还有几道守卫
        （`[LobbyStage+0x1c]`、剩余生命、`[char+0x614]`）。**到底是哪一道
        卡住的还没查实**（会话 30 的注入实验把「凶手=自己」和「移动污染
        0x614」两个假设都证伪了），但那条链的**出口**是确定的：客户端只是
        在等一发 `0x0419`，而 `0x0419` 完全由服务端说了算。所以不管客户端
        卡在哪一道守卫上，服务端主动补这一发都能把人拉起来 —— 而且
        `0x0419` 本身就会清掉 `[char+0x614]`，等于顺手把死锁解开。

        ## 判据

        - 只在**真开打了**的局里跑（`room_in_battle`），结算完就不管了。
        - 座位上得有人：中途退房的不补。
        - 生存类模式（0 / 2）要**还有命**才补 —— 三条命用完就该躺着，
          那是原版规则（§204），补了反而是作弊。夺分 / 闯关没有命数上限。
        - 坐标优先用本人上次自报的重生点，其次是这张图上任何人用过的，
          最后才退回死亡地点（见 `RoomQuest.respawn_point_for`）。

        正常路径下这个函数永远什么都不做：客户端 5 秒就发 `0x0413` 了，
        闩早在第 5 秒就被 `on_respawn_request` 撤掉。
        """
        room = self.lobby_room()
        if not room_in_battle(room):
            return 0
        quest = self.quest_state()
        if quest.settled or quest.pvp_reason is not None:
            return 0
        due = quest.due_respawns(now)
        if not due:
            return 0
        survival = (not self.quest_mode()
                    and self.pvp_game_mode() in (PVP_MODE_SURVIVAL,
                                                 PVP_MODE_FIGHT))
        sent = 0
        for seat, position in due:
            quest.disarm_respawn_watchdog(seat)
            if not (0 <= seat < len(room.seats)) or room.seats[seat] is None:
                continue                    # 人走了，没什么好复活的
            if survival and quest.remaining_lives(seat) <= 0:
                self.vlog(f"   [重生看门狗] 座位 {seat} 三条命用完了，不补重生")
                continue
            x, y, spawn_index = quest.respawn_point_for(seat, position)
            self.log(f"★ [重生看门狗] 座位 {seat} 死了 {RESPAWN_WATCHDOG_S:.0f} 秒"
                     f"还没发 0x0413 —— 服务端补一发 gspRespawnCharacter(0x0419) "
                     f"坐标=({x}, {y}) 重生点索引={spawn_index}"
                     f"（bug调查/8「死了不复活」）")
            self.battle_broadcast(
                build_game(OP_RESPAWN_CHARACTER,
                           build_respawn_character(seat, x, y, spawn_index)),
                reason="：看门狗补重生")
            sent += 1
        return sent

    def on_req_change_to_next_map(self, payload):
        """0x0411「我要去下一张地图」-> 原样回 0x0417，客户端这才开始换图。

        ★ **这是「走到地图最右边角色卡住、鼠标变沙漏」的根治点**（§111）。
        在此之前服务端从来没回过这个包：客户端 `0x4083e1` 发完请求就把
        `[LobbyStage+0x3f9]` 置 1 表示「等服务端」，然后什么都不做地等 ——
        角色不能操作、光标停在等待状态，和「血量归零不死」是同一类病
        （§108：客户端把判定交给服务端，而我们没接）。

        回显策略：**把客户端自报的地图名原样发回去**。下一张地图叫什么
        只有客户端的地图目录知道（服务端手上没有任何地图数据），
        这和死亡/重生用的是同一条理由（D046）。

        ## 多人（J.3）：**全房间一起换图**

        走到地图最右边的只有一个人，但换图必须是全房间的事 —— 不广播的话
        他一个人进了新图，其余人还留在旧图，两边的场景对不上，之后所有
        按句柄/座位定位的包（掉落物、死亡）就都对不上号了。

        两个人同时走到边缘会各发一发 `0x0411`。第二发不再广播
        （`RoomQuest.begin_map_change` 判重），否则先收到的人会被要求
        **再卸一次场景**。
        """
        try:
            map_name = parse_req_change_to_next_map(payload)
        except (ValueError, struct.error) as error:
            self.log(f"   0x0411 解析失败: {error}；不回换图应答")
            return
        quest = self.quest_state()
        if not quest.begin_map_change(map_name):
            self.log(f"   换图请求: {map_name!r} 已经在换了；不重复广播 0x0417")
            return
        self.log(f"   换图请求: 下一张地图 = {map_name!r}"
                 f"（本局第 {len(quest.maps_entered)} 次换图）")
        self.log("← 回 gspRepChangeToNextMap(0x0417) 原样回显 —— "
                 "客户端收到才会卸场景、加载新地图")
        self.battle_broadcast(
            build_game(OP_REP_CHANGE_TO_NEXT_MAP,
                       build_rep_change_to_next_map(map_name)),
            reason="：换图")

    def on_map_loading_done(self):
        """0x0412「新地图加载完了」-> 回 0x0418，客户端才从加载画面里出来。

        换图流程 `0x47900a` 起一个后台线程去加载地图，主线程在
        `0x47928d..0x479628` 的加载循环里泵消息、画进度，**加载完成之后**
        每 5 秒发一次这个空包，直到 `[LobbyStage+0x3fa] != 0` 才退出循环。
        那个标志位只有服务端方向的 `0x0418`（处理器 `0x406302`）会置。

        ★ **必须等这一发轮询到了再放行，不能在 0x0417 后面顺手把 0x0418
        一起发出去**：加载循环是 `do-while` 的**前置**判断（`0x47961d`），
        标志位要是在进循环前就被置 1，客户端会直接跳过整段加载等待，
        而后台加载线程还在跑 —— 这正是 D035「一次只推一个 stage、
        且必须等客户端报到」要防的那类事故。

        ## 多人（J.3）：**等所有人都把新图加载完才放行**

        和开局链的 `0x0403` 完全同一个套路（`RoomStartGame.on_loaded`）：
        每个客户端加载完之后每 5 秒轮询一发，收齐了才一起放行。
        先放行快的那一个人的话，他已经在新图里跑了，慢的还在加载画面 ——
        中间这段时间两边的世界是错开的。

        谁没加载完就先晾着它继续轮询；**它自己那一发轮询也不回**，
        所以不会出现「放行了一半」的状态。
        """
        quest = self.quest_state()
        if quest.pending_map is None:
            # 没有换图在飞时收到它，说明我们对触发条件的理解有偏差。照样放行
            # （这个包只是置一个在换图期间才有意义的标志），但把它记下来。
            self.log("   收到 0x0412 但本地没有记录在飞的换图；仍然放行")
            self.send(build_game(OP_MAP_CHANGE_READY))
            return
        members = self.battle_members()
        if not quest.map_done(self, members):
            still = quest.waiting_for_map(members)
            self.log(f"   新图加载完了；还在等 {len(still)} 人，先不放行 0x0418")
            return
        quest.finish_map_change()
        self.log("← 回 0x0418（空包）—— 置 [LobbyStage+0x3fa]=1，"
                 "客户端退出换图加载循环")
        self.battle_broadcast(build_game(OP_MAP_CHANGE_READY),
                              reason="：换图放行")

    def on_create_item(self, payload):
        """0x0406 `gcpCreateItem`「在这里生成一个掉落物」-> 回 0x0404，它才落地。

        ★ 这条链和 §108（血量归零不死）、§111（换图卡住）是同一个形状：
        **闯关模式的物件生成判定也在服务端**，客户端把「掉什么、掉在哪、
        初速多少」算好报上来就干等，我们从来没接过 —— 所以打死怪不掉东西、
        通关后也没有那阵金币雨。

        修法同 D046：**原样回显 + 补一个服务端分配的实例句柄**。
        服务端手上没有、也不需要有任何物件数据。

        ## 多人（J.3）：广播 + **房间级**句柄分配

        - **句柄必须由房间分配**：它进客户端 `World` 的 map 当 key
          （`0x473e7c`）。每条连接各自从 `ITEM_HANDLE_BASE` 数的话，
          A 的第 1 件和 B 的第 1 件同号，后到的会**覆盖**先到的。
        - **广播出去**：不广播的话这件东西只在掉它的那个人屏幕上存在，
          别人走过去什么都没有 —— 而拾取 `0x0405` 又是按句柄找物件的。
        - 客户端**只在事件属于自己时才发这个包**（`0x508cbb` 和 `0x4faa94`
          两个掉落点都有 `[char+0x2ac] == 我的座位` 这条判据，§161），
          所以不需要去重。
        """
        try:
            fields = parse_create_item(payload)
        except (ValueError, struct.error) as error:
            self.log(f"   0x0406 gcpCreateItem 解析失败: {error}；不回掉落应答")
            return
        item_id, x, y = fields[0], fields[1], fields[2]
        # 掉落点在角色/怪物脚下，拿来当 respawn 的兜底坐标是合适的。
        self.last_position = (x, y)
        quest = self.quest_state()
        handle = quest.allocate_item()
        # 客户端掉的东西也要记类型：金币 / 红心不进道具槽，但关卡脚本
        # 万一掉出一件 PvP 道具，捡起来照样要补 `0x040b`（§194）。
        quest.remember_item(handle, item_id)
        self.items_created += 1
        name = ITEM_NAMES.get(item_id, "未知物件")
        self.vlog(f"   掉落请求: {item_id} {name} @ ({x:.0f}, {y:.0f}) "
                  f"速度=({fields[3]:.0f}, {fields[4]:.0f})")
        if self.items_created == 1 or VERBOSE:
            self.log(f"← 回 gspCreatedItem(0x0404) 句柄=0x{handle:08x} "
                     f"物件={item_id} {name} @ ({x:.0f}, {y:.0f})"
                     + ("" if VERBOSE else "（本局第一件，后续同类静音）"))
        self.battle_broadcast(
            build_game(OP_CREATED_ITEM, build_created_item(handle, fields)))

    def on_get_item(self, payload):
        """0x0407 `gcpGetItem`「我踩到这件了」-> 回 0x0405，客户端才真的捡起来。

        ★ 这是 §108（血量归零不死）、§111（换图卡住）、§113（打死怪不掉东西）
        之后的**第四条同形状的链**：判定在服务端，客户端报完就等着。
        掉落物在会话 17 已经能掉出来了，但走上去捡不起来 —— 缺的就是这一环。

        比前三条更难忍的地方在于 `Character::CheckItemPickup`（`0x5154d3`）
        发完包就把 `[item+0x2a8]` 置 1，**同一件东西一局只报一次**。
        所以服务端漏回不是「这次没捡到，再走一遍」，而是那件东西作废了。

        修法同 D046：**原样回显**。服务端不查距离、不算效果 ——
        碰撞是客户端算的，物品效果也是客户端算的（§113 已经确认拾取不再发包）。

        ## 多人（J.3）：★ **一件东西只能被一个人捡到，这一条必须服务端仲裁**

        两个人几乎同时踩到同一件东西时，两台机器**各自**都会判「我碰到了」
        并发 `0x0407`。谁的包先到我们这儿谁拿走，晚到的那一发
        **什么包都不回** —— 那件东西对他就是没捡到（客户端已经把
        `[item+0x2a8]` 置 1，本来也不会再问第二次）。

        放行则**广播给全房间**：读侧 `0x551d35` 用
        座位 -> 角色（`0x404ff6`）+ 句柄 -> 物件（`World::Find`）两把钥匙，
        两个都查得到才调 `item->vft[0xd4](角色)`，所以别人机器上会看到
        「东西没了、是那个人拿走的」。不广播的话东西只在捡的人那边消失。

        ## ★★ PvP 道具还要再补一发 `0x040b`（§194）

        `0x0405` 对 PvP 道具**只是把箱子抹掉 + 放特效**（它们的 `vf_d4`
        重写成了 `0x5224fe`，基类那条「当场生效」的分支恒不成立）。
        道具真正进 4 个槽只有一条路：服务端发 `0x040b` ->
        `Character::AddItem`。用户报的「有捡起动画、箱子也没了，
        但道具栏里没东西、也用不了」就是缺这一发。

        它按「收包的本地玩家」认人（包里没有座位号），所以**只发给捡到的
        那个人**，而且只发给 `GRANTABLE_ITEM_IDS` 里的物件 —— 金币 / 红心
        走的是当场生效那条，多发一发等于凭空多一件道具。
        """
        try:
            seat_id, handle = parse_get_item(payload)
        except (ValueError, struct.error) as error:
            self.log(f"   0x0407 gcpGetItem 解析失败: {error}；不回拾取放行")
            return
        quest = self.quest_state()
        if not quest.claim_item(handle, seat_id):
            winner = quest.items_taken.get(handle & 0xFFFFFFFF)
            self.log(f"   拾取被拒: 句柄=0x{handle & 0xffffffff:08x} "
                     f"已经被座位 {winner} 捡走了；座位 {seat_id} 这一发不回包")
            return
        self.items_picked += 1
        if self.items_picked == 1 or VERBOSE:
            self.log(f"← 回拾取放行(0x0405) 座位={seat_id} "
                     f"句柄=0x{handle & 0xffffffff:08x}"
                     + ("" if VERBOSE else "（本局第一件，后续静音）"))
        self.battle_broadcast(
            build_game(OP_PICKED_ITEM, build_picked_item(seat_id, handle)))
        self.grant_picked_item(seat_id, handle, quest)

    def grant_picked_item(self, seat_id, handle, quest):
        """拾取放行之后，如果捡到的是 PvP 道具就补一发 `0x040b`（§194）。

        返回真的发出去了没有。发不出去的三种情况都不该有任何包：

        - 捡到的不是道具（金币 / 红心 / 武器 —— 它们拾取当场就生效了）；
        - 这个句柄我们没记过（协议试探造的假句柄）；
        - 那个人 4 格已经满了 —— 客户端 `AddItem` 在这种情况下**什么都不做**，
          我们跟着不发才对得上（发了它也不收，镜像却会多一件）。
        """
        item_id = quest.item_id_of(handle)
        if item_id is None or item_id not in GRANTABLE_ITEM_IDS:
            return False
        target = self.seat_conn(seat_id)
        if target is None:
            self.log(f"   ⚠ 座位 {seat_id} 找不到对应连接；"
                     f"物件 {item_id} 这一发 0x040b 不发")
            return False
        if not quest.grant_item(seat_id, item_id):
            self.log(f"   座位 {seat_id} 的道具槽已满 "
                     f"({ITEM_SLOT_COUNT}/{ITEM_SLOT_COUNT})；"
                     f"物件 {item_id} 不进槽（客户端 AddItem 同样会丢掉）")
            return False
        held = quest.item_slots[seat_id]
        self.log(f"← 发道具 gspGiveItem(0x040b) 给座位 {seat_id} "
                 f"物件={item_id} {ITEM_NAMES.get(item_id, '未知物件')}"
                 f"；他手上现在 {len(held)}/{ITEM_SLOT_COUNT} 件 {held}")
        try:
            target.send(build_game(OP_GRANT_ITEM, build_grant_item(item_id)))
        except OSError as error:
            self.log(f"   0x040b 发送失败（{error!r}），忽略")
        return True

    def seat_conn(self, seat_id):
        """座位号 -> 那个座位上的连接。找不到就是 ``None``。

        ★ 只在「这个包必须发给某一个特定的人」时才用（`0x040b` / `0x040c`
        都按收包方的本地玩家认人，广播出去就错了）。不在房间里时退化成
        「只有我自己」，和 `battle_members()` 同一个口径（D084）。
        """
        seat = int(seat_id)
        room = self.lobby_room()
        if room is None:
            return self if seat == self.my_seat else None
        if not 0 <= seat < len(room.seats):
            return None
        entry = room.seats[seat]
        return None if entry is None else entry.conn

    def on_use_item(self, payload):
        """0x040c（客户端方向）「我按 Ctrl 要用第 N 格的道具」（§194）。

        客户端按下那一刻做的全部事情就是发这一发（槽位恒 0）再放一声音效
        —— **效果和扣道具都在服务端**。所以要回两个包：

        | 包 | 发给谁 | 客户端做什么 |
        |---|---|---|
        | `0x040c`（同号回显）| **只发给他自己** | `RemoveItem` 把那一格拿掉、后面的往前挪 |
        | `0x040a` | **广播全房间** | `UseItemEffect(道具id, …)` 让那个座位吃到效果 |

        两个包在客户端里各自**只有一个**调用点，缺哪个都是残的：
        不回 `0x040c` 道具永远卡在槽里（玩家：按了没反应）；
        不回 `0x040a` 则道具没了但什么也没发生（玩家：用了个寂寞）。

        道具 id 只能从服务端自己那份槽镜像里查 —— 包里只有槽位序号。
        槽是空的（没捡过就按、或者连着按两下）就**一个包都不回**，
        和拾取仲裁被拒时同一个处置。
        """
        try:
            slot_index = parse_use_item(payload)
        except (ValueError, struct.error) as error:
            self.log(f"   0x040c rawUseItem 解析失败: {error}；不回包")
            return
        quest = self.quest_state()
        seat_id = self.my_seat
        item_id = quest.use_item(seat_id, slot_index)
        if item_id is None:
            held = (quest.item_slots[seat_id]
                    if 0 <= seat_id < ROOM_SEAT_COUNT else [])
            self.log(f"   座位 {seat_id} 按了「用道具」但第 {slot_index} 格是空的"
                     f"（手上 {held}）；一个包都不回")
            return
        held = quest.item_slots[seat_id]
        self.log(f"★ 座位 {seat_id} 用道具: 第 {slot_index} 格 "
                 f"物件={item_id} {ITEM_NAMES.get(item_id, '未知物件')}"
                 f"；他手上还剩 {len(held)}/{ITEM_SLOT_COUNT} 件 {held}")
        self.log(f"← 回 0x040c（把第 {slot_index} 格拿掉）只发给他自己")
        try:
            self.send(build_game(OP_USE_ITEM, build_use_item(slot_index)))
        except OSError as error:
            self.log(f"   0x040c 发送失败（{error!r}），忽略")
        self.battle_broadcast(
            build_game(OP_ITEM_EFFECT, build_item_effect(seat_id, item_id)),
            reason=f"：道具效果 {item_id} 作用于座位 {seat_id}")

    def on_remove_char_attr(self, payload):
        """0x040d（客户端方向）「我身上那个道具效果结束了」（§200）。

        `0x040a` 只管效果**开始**，结束是另一条链，而且天生只有一台机器
        知道 —— 弹数型的道具（三重射击 / 致命射击 / 毒弹，`Status.ini` 里
        只有 `Magazine` 没有 `Time`）的 duration 是 **-1（无限）**，真正的
        终止条件是「本机玩家把那 3 发打完了」。

        所以这一发**必须原样广播给房里其他人**，否则别人屏幕上那把三连射
        的枪 / 那圈毒雾永远不会变回去（用户报的「自己看得到模型恢复了，
        别人看不到」就是这个）。

        两条口径：

        - **座位号以发包的连接为准**，不信包里那个。客户端的发送点
          （`0x509843`）本来就只会填自己的座位，重填一次等于把「谁能替谁
          撤效果」这件事钉死在服务端；
        - **不发给上报的人自己** —— 他那台机器早就拆完了，而且客户端
          `0x551dfb` 第一句就是 `if (座位 == 我的座位) return`。
        """
        try:
            reported_seat, attr_id = parse_remove_char_attr(payload)
        except (ValueError, struct.error) as error:
            self.log(f"   0x040d rawRemoveCharAttr 解析失败: {error}；不转发")
            return
        if not 0 <= attr_id <= CHAR_ATTR_MAX:
            self.log(f"   0x040d 属性号 {attr_id} 超出 0~{CHAR_ATTR_MAX}；不转发")
            return
        seat_id = self.my_seat
        if reported_seat != seat_id:
            self.log(f"   ⚠ 0x040d 报的是座位 {reported_seat}，"
                     f"但这条连接坐的是 {seat_id}；按 {seat_id} 转发")
        name = CHAR_ATTR_NAMES.get(attr_id, "未知属性")
        sent = self.battle_broadcast(
            build_game(OP_REMOVE_CHAR_ATTR,
                       build_remove_char_attr(seat_id, attr_id)),
            exclude=self)
        self.attrs_removed += 1
        if self.attrs_removed == 1 or VERBOSE:
            self.log(f"★ 座位 {seat_id} 的属性 {attr_id} {name} 结束了 —— "
                     f"0x040d 转给房里另外 {sent} 人"
                     + ("" if VERBOSE else "（本局第一条，后续静音）"))

    def on_mark_quest_success(self, payload):
        """0x0417 `gcpMarkQuestSuccess`「这一关我打通了」—— 只记，不回。

        关卡脚本打死关底时调 `GameContextQuest::vf_e4(1)`（`0x4a3faa`），
        载荷是一个 bool（线上 4 字节）。发送点有 `[ctx+0x558]` 保证一局只发一次。

        实测（岩浆巨龙真通关）：boss 倒下后 43 毫秒就到，比 `0x040f gcpEndQuest`
        **早整整 30 秒**（中间是金币雨），所以结算时这个标志一定已经就位。
        「时间到 / 生命耗尽」那条路相反 —— `0x4a3dac` 先 `EndQuest()` 再
        `vf_e4(0)`，赶不上结算，于是自然就是「未完成」。两条路都对。

        ★ 服务端方向的同号包是 `gspRepChangeToNextMap`（换图放行），
        **绝对不能回显**，否则会在关卡结束时触发一次换图（D028）。

        ## 多人（J.3）：**记在房间上，谁报到都算**

        合作模式的关底是大家一起打的，关卡脚本在每台机器上都会喊
        `vf_e4(1)`，但 `[ctx+0x558]` 只保证「一台机器一局只发一次」。
        通关是全房间的事，所以只要有一个人报了 1，这一局就算通关 ——
        `RoomQuest.mark_success` 只会从 False 变 True，不会被后到的 0 冲掉。
        """
        try:
            cleared = bool(Reader(payload).i32())
        except ValueError:
            # 载荷形状不对时保守当作没通关，别把「未完成」误报成「完成」。
            self.log("   0x0417 载荷解析失败；本局按未通关处理")
            return
        self.quest_success = self.quest_state().mark_success(cleared)
        self.log(f"   客户端报 gcpMarkQuestSuccess({cleared}) —— "
                 f"结算界面的「完成 / 未完成」标签认这个"
                 f"（本局通关标志 = {self.quest_success}）")

    def settlement_seats(self):
        """`{座位号: 连接}` —— 本局要结算的每一个人。

        不在大厅房间里（协议试探 / 控制通道）时就只有自己那一个座位，
        于是整条结算路径退化成 V0.1 的单人版本，一个字节都不差。
        """
        room = self.lobby_room()
        if room is None:
            return {self.my_seat: self}
        found = {}
        for index, seat in enumerate(room.seats):
            if seat is not None and seat.conn is not None:
                found[index] = seat.conn
        # 大厅表里没有我（不该发生，但控制通道能造出来）时也别把自己漏掉。
        found.setdefault(self.my_seat, self)
        return found

    def send_end_game(self, success=None):
        """结算这一局：把所得记进存档，再把新的经验/金币下发（0x0411）。

        奖励取客户端 `0x0410 gcpUpdateQuestScore` 报上来的**累计分数**
        （实测一局 4→12→…→64）。真服务器怎么换算不可知，这里 1 分 = 1 点经验
        = 1 金币，够让「打一局有长进」这件事成立；要调直接改这里。

        存档写在下发**之前**：客户端拿到的必须是已经入账的总经验，
        否则重登一次就退回去了（D024，JSON 是真源）。

        ★ 先发 `0x0309 gspRepGameResult` 再发 `0x0411`（§99）。前者把本局增量
        写进 GameContext 供结算界面显示，后者结束关卡并更新数据栏。顺序反了
        或者漏掉前者，结算界面就构造得出来却不显示。

        `success` 显式传入时会**盖住**本局的通关标志（控制通道的
        `endgame 1/0` 用），不传就用 `0x0417 gcpMarkQuestSuccess` 报上来的。

        ## 多人（J.3）：每座位一份 `0x0309`，每人一份自己的 `0x0411`

        - **结算只做一次**：房里每个人的客户端都会发一发 `0x040f gcpEndQuest`，
          第一发触发结算，之后的直接忽略（`RoomQuest.settled`）。
          不挡的话六个人打完一局要入账六次。
        - **`0x0309` 每个在座座位各发一份给每一个人**：处理器 `0x55210d`
          全程按 `pkt+0x04` 的座位号索引（`[ctx+座位*4+0x2c/0x44/0x5c]` 三行数值、
          `[ctx+0x184]` 名次表），六份下去结算界面上每个人那一行才有数。
          只发自己那一份的话，队友那几行全是 0。
        - **`0x0411` 也每座位一份**（会话 15 改，D101 / §178）：那 13 个 dword
          由 `0x4a4096` 写进 `[GameContextQuest + 座位*0x34 + 0x3ec]`，
          **只有 `0x0411` 会写**，所以不每座位发一份的话，结算界面上
          队友那一行的「分数 / 生命」是 0 —— 两个人看到的数还对不上。
          三处「重复投递安不安全」现在都读到底了：
          · `0x4a4096` 纯按座位号索引写 13 个 dword，各写各的；
          · `0x5518b9 cmp 包里的座位, 我的座位` 只护着右上角数据栏那四个全局
            （`+0x1c` 是 `+=`），别人那份走不到，钱不会串；
          · `0x4913fc`（弹结算界面）第一句就是 `cmp byte [esi+4], 0 / jne 尾部`，
            第二发起整个函数是空转；
          · `0x4087f0` 是 `0x0505 gcpAccumulatedWeaponDamage` 的**排空式**上报：
            `0x55bc5f` 把 `GameSession+0x404` 的累计伤害和一个刚构造的空对象
            **swap**（`mov [esi],edx / mov [eax+esi],ecx`）再发、再析构，
            所以第二发起送的是空表，既不重复计数也不改别的状态。
        - **自己那一份排在最前面**：`0x4913fc` 在处理**每一发** `0x0411` 的
          末尾都会被调到，弹界面的是第一发。把自己那份放第一发，
          「先更新四个全局、再弹界面」的时序和 V0.1 实机验过的单人版一模一样，
          队友那几行只是在界面弹出来之后补进去的额外信息。
        """
        quest = self.quest_state()
        if quest.settled:
            self.log("   本局已经结算过了；忽略（房里每个人都会发一发 0x040f）")
            return
        quest.settled = True
        if success is not None:
            quest.success = bool(success)
        cleared = quest.success
        seats = self.settlement_seats()
        quest_mode = self.quest_mode()
        pvp_mode = None if quest_mode else self.pvp_game_mode()
        # ★ 对战里客户端**从不发 `0x0410 gcpUpdateQuestScore`**（实机整局日志里
        #   一发都没有），所以 `quest_score` 恒为 0，光靠它排名会永远判成
        #   「全场 0 分不判」。对战的分数就是杀敌数，服务端自己从 `0x0408`
        #   的凶手字段数出来（§167）。取两者较大的，闯关那一路一个字不变。
        scores = {seat: max(0, int(conn.quest_score),
                            0 if quest_mode else quest.kills[seat]
                            if 0 <= seat < ROOM_SEAT_COUNT else 0)
                  for seat, conn in seats.items()}
        # ★ 队伍表两条分支都要用（§226 修好之前只有生存那一路算它，
        #   于是夺分的组队战里队友被逐个当成对手排名 —— 用户实机报的
        #   「只有得分最高的一个人显示胜利」）。`battle_teams()` 是现成的。
        teams, team_mode = self.battle_teams()
        teams = teams or {}
        # 尾部数组 = 每座位的「完成 / 输赢」。闯关是合作（通关了大家一起 1）；
        # 生存按剩余生命 / 队伍判，夺分按分数排名 —— 组队时按**队伍合成分**
        # （§161 / §204 / §226）。
        if pvp_mode in (PVP_MODE_SURVIVAL, PVP_MODE_FIGHT):
            tail = quest.survival_ranking(
                seats, teams, team_mode=team_mode)
        else:
            tail = quest.ranking(scores, quest_mode,
                                 teams, team_mode=team_mode)
        if not quest_mode:
            if pvp_mode in (PVP_MODE_SURVIVAL, PVP_MODE_FIGHT):
                remaining = {seat: quest.remaining_lives(seat) for seat in seats}
                self.log(f"   生存胜负: 剩余生命 {remaining} -> 尾部数组 {tail}"
                          f"（1=胜 / -1=负 / 0=不判）")
            elif team_mode:
                # 队伍汇总打出来，实机时能直接拿它和结算界面对。
                totals = quest.team_rank_values(scores, teams)
                detail = "、".join(
                    f"队伍{team} {score} 分 / 死 {deaths}"
                    for team, (score, deaths) in sorted(totals.items()))
                self.log(f"   夺分胜负(组队): 个人分 {scores}；{detail}"
                          f" -> 尾部数组 {tail}（1=胜 / -1=负 / 0=不判）")
            else:
                self.log(f"   对战胜负: 分数 {scores} -> 尾部数组 {tail}"
                          f"（1=胜 / -1=负 / 0=不判）")

        # ---- ① 每个人先入账，并把「他那一份 0x0309 / 0x0411」备好 --------
        results = {}     # 座位 -> 0x0309 的载荷
        end_games = {}   # 座位 -> (0x0411 的载荷, 日志用的数)
        # ★ 闯关的关卡 id / 难度：房里每个人读到的是同一份（`current_quest()`
        #   先看大厅那一份），所以在循环外取一次就够。
        quest_info = self.current_quest() if quest_mode else None
        for seat, conn in sorted(seats.items()):
            score = scores[seat]
            # `0x0411` 的 success 跟着尾部数组走，两个包才不会自相矛盾。
            seat_cleared = (tail[seat] == GAME_RESULT_CLEARED
                            if 0 <= seat < GAME_RESULT_TAIL_COUNT else cleared)
            if quest_mode and cleared:
                # 通关了才解锁下一个难度。★ 每个人的存档各解各的。
                conn.record_quest_clear()
            # ★★ 分数 / 经验 / 金币是**三件事**（§227 / D148）。以前它们是同一个
            #    `score`，于是结算界面三行数一模一样、而且一局能给上千经验。
            #    分数栏仍然发本局分数，经验和金币各按自己的公式算。
            if quest_mode:
                quest_id, difficulty = quest_info or (1, 1)
                gained_exp, gained_money = quest_reward(
                    quest_id, difficulty, score, seat_cleared)
            else:
                gained_exp, gained_money = pvp_reward(score, seat_cleared)
            # ★★ 再加上**本局在地上捡到的金币**。闯关里怪和 boss 掉的、对战里
            #    偶尔掉的，都在 `RoomQuest.claim_item()` 那一步按座位记好了。
            #    客户端的 1 / 5 累加只用于战局内浮字，不改持久账户余额；这里才把
            #    同一面额真正写进账号，所以不会重复入账。
            picked_coins = quest.coins_of(seat)
            gained_money += picked_coins
            if conn.account_name:
                try:
                    conn.account = conn.accounts.add_quest_reward(
                        conn.account_name,
                        experience=gained_exp, money=gained_money)
                except KeyError:
                    conn.log(f"   存档里没有账号 {conn.account_name!r}；奖励未入账")
            experience = int((conn.account or {}).get("experience", 0))
            level_start_exp, next_level_exp = experience_bounds(experience)
            #   · 业务值 9/10/11 = 界面上「经验值 / 金币 / 竞技场分数」三行的 +N
            #     （§116）。闯关模式没有天梯分，第三格发 0。其余 9 个仍按 D019 填 0
            #     —— §100 那次「12 个值一次全填」会让客户端 20 毫秒内断链。
            results[seat] = build_rep_game_result(
                seat,
                values=build_game_result_values(experience=gained_exp,
                                                money=gained_money),
                tail=tail)
            end_games[seat] = (
                build_end_game(seat, seat_cleared, build_end_game_values(
                    experience=experience,
                    next_level_exp=next_level_exp,
                    level_start_exp=level_start_exp,
                    money_gained=gained_money,
                    score=score,
                )),
                (score, experience, level_start_exp, next_level_exp),
            )
            conn.log(f"   结算 座位{seat}: 分数={score} "
                     f"{'完成/胜' if seat_cleared else '未完成'} "
                     f"-> 本局经验+{gained_exp} "
                     f"金币+{gained_money}"
                     f"（固定 {gained_money - picked_coins} + 捡到 {picked_coins}）；"
                     f"总经验={experience} (本级 {level_start_exp}..{next_level_exp})")

        # ---- ② 再逐个连接下发 --------------------------------------------
        # ★ 不合并成一次 sendall：V0.1 单人时这两个包就是分开发的，实机验过
        #   （§99）。合并只在「包会触发 ChangeStage 且客户端要在同一帧内看到
        #   后续包」时才是必须的（§120），结算这两个包不属于那一类。
        for seat, conn in sorted(seats.items()):
            try:
                # 结算界面的数据源，必须排在 0x0411 之前，且只能在 GameContext
                # 还活着的时候发（§99）。每个在座座位一份。
                for other_seat in sorted(results):
                    conn.send(build_game(OP_REP_GAME_RESULT,
                                         results[other_seat]))
                # 0x0411 也是每座位一份，但**自己那份必须是第一发**
                # （弹结算界面的是第一发，见上面的注释）。
                for other_seat in end_game_order(seat, end_games):
                    payload, _ = end_games[other_seat]
                    conn.send(build_game(OP_END_GAME, payload))
            except OSError as error:
                conn.log(f"   结算下发失败（{error!r}），忽略")
                continue
            conn.settled = True
            conn.quest_success = cleared
        self.log(f"← 已结算本局：每人各收到 {len(results)} 份"
                 f" gspRepGameResult(0x0309) + {len(end_games)} 份"
                 f" gspEndGame(0x0411)（自己那份在最前）"
                 f"（{'闯关' if quest_mode else '对战'}，"
                 f"{'通关' if cleared else '未通关'}）")

    # -- 里程碑 I：大厅联机 ---------------------------------------------------
    def on_list_session(self, payload):
        """`0x0200 gcpReqListSession` —— 回真实房间列表（§138 / §139）。

        大厅四个标签页各看各的：请求里那个 int32 是游戏类型，按它过滤。
        第 4 个字节是左下角「全部 / 待机」那对按钮，选「待机」就只回
        待机中的房间（§170）。
        解析失败就退回「不过滤」，宁可多列几个房间，也不要让列表整个空掉
        （空列表和「服务端挂了」在玩家眼里长得一模一样）。
        """
        game_type = None
        waiting_only = False
        try:
            request = parse_list_session_request(payload)
        except (ValueError, struct.error, IndexError) as error:
            self.log(f"   房间列表请求解析失败: {error}; 不按类型/状态过滤")
        else:
            game_type = request["game_type"]
            waiting_only = request["waiting_only"]
            self.vlog(f"   房间列表请求: 游戏类型={game_type} "
                      f"({request['game_type_name']}) "
                      f"起始房间号={request['start_room']} "
                      f"每页={request['page_size']} "
                      f"过滤={request['filter']}"
                      f"（{'只看待机' if waiting_only else '全部'}）"
                      f" 未定字段={request['unknown']}")
        rooms = LOBBY.rooms(game_type, waiting_only=waiting_only)
        if rooms:
            self.log(f"← 回 gspRepListSession"
                     f"（{'只看待机；' if waiting_only else ''}"
                     f"{len(rooms)} 个房间："
                     + "；".join(r.describe() for r in rooms) + "）")
        else:
            self.vlog("← 回 gspRepListSession（空房间列表）")
        self.send(build_game(OP_LIST_SESSION, build_rep_list_session(rooms)))

    def enter_room(self, room, seat_index, reason=""):
        """进房间之后要发的一整串包（自己收的那份）。

        顺序是硬约束，和建房那一路同因（§140 / V0.1 §119 / §120）：

            0x0303 Session   -> RoomStage 构造时读的就是它（状态/描述符/地图名）
            0x0202 应答      -> 处理器最后一句是 ChangeStage(5)
            0x0300 座位快照  -> 必须在 0x0202 **之后**（0x54f815 会清座位 0 的角色 id）
            0x030b 物品清单  -> 必须在 0x0300 **之后**（持有判定先查座位已占用）

        四个包**合并成一次 sendall**，否则「人物选择」会小概率缩回 3 个头像。
        """
        self.my_seat = seat_index
        # `self.room` 是「下发 0x0303 用的那份描述符」，进别人的房间时要按
        # 房间的当前参数重建 —— 它原本只在自己建房时才有。
        self.room = {
            "texts": (room.title, room.map_name, ""),
            "option": 0,
            "session_type": room.session_type,
            "session_type_name": SESSION_TYPE_NAMES.get(room.session_type,
                                                        "unknown"),
            "arguments": room.arguments,
        }
        self.start_game.reset()
        self.reset_quest_state()
        # 上一个房间的开关记录作废（客户端离开时已经自己清 0 了）。
        self.forget_peer_relay()
        # 换代：进房不会改变客户端的局号，但要记下「它现在属于哪一代」。
        self.anchor_epoch(room, "（进房）")
        with self.send_batch("；进房四连发不能被客户端的 recv 切开"):
            self.send_update_session(map_name=room.map_name)
            self.log(f"← 回 gspRepMoveInto(result=0, 房间 #{room.room_id}, "
                     f"座位={seat_index}){reason}")
            self.send(build_game(
                OP_MOVE_INTO_SESSION,
                build_rep_move_into_session(MOVE_INTO_OK, room.room_id,
                                            seat_index)))
            self.send_session_members()
            self.send_slot_equipped_list(reason="（进房后下发）")

    def announce_join(self, room, seat_index):
        """把「有人进来了」广播给房里的其他人（`0x0301` action 0）。

        ★ action 0 会把座位的对端 IP / 端口清 0，而 `0x0300` 的处理器会把
        「我」的座位 IP 写成 127.0.0.1 —— 两个包同时发时 `0x0301` 必须排在
        `0x0300` **前面**。这里只发 `0x0301`，房里其他人的 `0x0300` 不用重发
        （他们的座位表由这一发增量更新）。
        """
        seat = room.seats[seat_index]
        if seat is None:
            return
        packet = build_game(OP_SESSION_MEMBER_UPDATE, build_session_member_update(
            seat_index, SEAT_ACTION_JOIN, **seat.snapshot()))
        self.broadcast(packet, reason=f"：座位 {seat_index} 加入")

    def on_move_into_session(self, payload):
        """`0x0202 gcpReqMoveInto` —— 加入指定房间（§140）。"""
        try:
            request = parse_move_into_request(payload)
        except (ValueError, struct.error) as error:
            self.log(f"   加入房间请求解析失败: {error}; 回 result=3")
            self.send(build_game(OP_MOVE_INTO_SESSION,
                                 build_rep_move_into_session(
                                     MOVE_INTO_NO_SUCH_ROOM)))
            return
        self.log(f"   加入房间请求: 房间 #{request['room_id']} "
                 f"密码={'有' if request['password'] else '无'} "
                 f"flag={request['flag']}")
        self.join_room(request["room_id"], request["password"])

    def on_quick_join_session(self, payload):
        """`0x0205 gcpReqQuickJoinSession` —— 随便找个房间进。

        ★ 成功时回的是 **`0x0202`**，不是 `0x0205`：`0x0205` 的服务端方向
        （处理器 `0x55027d`）只会拿包里那个字符串去本地化表查一次然后弹提示框，
        进不了房间。失败也回 `0x0202 result=3`，客户端弹的正好是
        「没有符合条件的房间。」，文案不用我们写（同 D069）。
        """
        game_type = None
        try:
            session_type, arguments = parse_quick_join_request(payload)
        except (ValueError, struct.error) as error:
            self.log(f"   快速加入请求解析失败: {error}; 不按类型过滤")
        else:
            game_type = lobby_game_type(session_type)
            self.log(f"   快速加入请求: 描述符 type={session_type} "
                     f"args={arguments} -> 游戏类型 {game_type}")
        result, room, seat_index = LOBBY.quick_join(
            self, game_type, seat=self.seat_snapshot())
        if result != MOVE_INTO_OK:
            self.log("← 回 gspRepMoveInto(result=3) —— 没有可进的房间")
            self.send(build_game(OP_MOVE_INTO_SESSION,
                                 build_rep_move_into_session(
                                     MOVE_INTO_NO_SUCH_ROOM)))
            return
        self.finish_join(room, seat_index, "（快速加入）")

    def join_room(self, room_id, password=""):
        """`0x0202` / 控制通道共用的进房逻辑。"""
        result, room, seat_index = LOBBY.join(self, room_id, password,
                                              seat=self.seat_snapshot())
        if result != MOVE_INTO_OK:
            why = MOVE_INTO_REASONS.get(result, "无法进入房间")
            self.log(f"← 回 gspRepMoveInto(result={result}) —— {why}")
            self.online(f"房间 ✗ 加入失败 账号={self.account_name!r} "
                        f"房间 #{room_id} 原因={why}")
            self.send(build_game(OP_MOVE_INTO_SESSION,
                                 build_rep_move_into_session(result)))
            return False
        self.finish_join(room, seat_index)
        return True

    def finish_join(self, room, seat_index, reason=""):
        """进房成功之后：先给自己发四连发，再广播给房里其他人。

        顺序不能反 —— 广播会阻塞在别人的 socket 上，先把自己的那份发出去，
        进房的人才不会觉得卡。
        """
        self.online(f"房间 ✓ 加入 账号={self.account_name!r} "
                    f"房间 #{room.room_id}「{room.title}」座位={seat_index} "
                    f"（房里现在 {room.player_count()} 人）")
        self.enter_room(room, seat_index, reason)
        self.announce_join(room, seat_index)
        self.broadcast_system_chat(
            f"{display_name(self.account) or self.account_name} 进入了房间。")
        # ★ 必须排在四连发**之后**（§150）：房里够两个人了就把玩家间同步打开。
        self.sync_peer_relay(room, reason="（有人进房）")
        # 有新人进来，上一轮的开局握手作废（不清的话新人不在 `loaded` 里，
        # 房主再按开始时会等一个从没收到过 0x0400 的人）。
        # ★ 这条只在待机/倒计时阶段走得通：加载中（PREPARING）的房间在
        #   `broadcast_start_game` 里已提前标「游戏中」挡人 —— 加载中的
        #   客户端没法重新按「开始」，把握手作废就等于死锁（bug调查/1）。
        # 上一局的战斗状态一并作废：新人进得来说明房间是待机中的，
        # 那一局早结束了，留着只会让下一局带上旧的掉落物句柄和 `settled`。
        room.battle = None
        room.quest = None

    def register_room(self):
        """把刚建好的房间登记进大厅（`0x0201` 解析成功之后立刻调）。

        建房的三个字符串里第一个是标题（V0.1 §69），第二个是地图名 ——
        但**建房那一刻地图名必须是空的**（`0x54f82e` 的相等判断，见
        `build_update_session`），真正的地图名要等客户端的 `0x0302` 才来。
        所以这里只登记标题，地图名留空。
        """
        if self.room is None:
            return None
        texts = self.room.get("texts") or ("",)
        room = LOBBY.create_room(
            self,
            title=texts[0] if texts else "",
            map_name="",
            session_type=self.room["session_type"],
            arguments=self.room["arguments"],
            seat=self.seat_snapshot(),
        )
        self.my_seat = 0
        self.anchor_epoch(room, "（建房）")
        self.online(f"房间 + 建房 账号={self.account_name!r} {room.describe()}")
        return room

    # ---- 开局链（多人）------------------------------------------------------

    def room_battle(self, room):
        """取（必要时建）房间的开局状态机。"""
        if room.battle is None:
            # ★ 发号器跟着连接走 —— 单测在 `conn.start_game` 上注入一个常量
            #   发号器，房间这条也会用同一个，一处注入两边生效。
            room.battle = RoomStartGame(
                seed=self.start_game.seed,
                seed_source=self.start_game.seed_source)
        return room.battle

    def broadcast_start_game(self, room, replies, why):
        """把开局握手的应答发给**房间里每一个人**（含自己）。

        为什么必须广播：`0x0400 gspPrepareGame` 是「切到 stage 6 去加载关卡」
        的命令，只发给房主的话别人连关卡都不会加载，自然也永远等不到他们的
        `0x0403`。**seed 也必须是同一个**，否则各人生成的关卡不一样。

        ★ 换代：这一发 `0x0400` 会让房里每个人的局号 +1，房间也跟着进
        「战斗代」—— 全在 `Conn.send()` 的 `note_epoch_from_frame` 里自动完成。
        大家的号本来就是一样的（进房那一发 `0x0303` 已经对齐过，D138），
        所以 +1 之后仍然一样。
        """
        if not replies:
            return
        members = room.members(exclude=None)
        for member in members:
            try:
                with member.send_batch(f"；{why}"):
                    for reply_opcode, reply_payload in replies:
                        member.send(build_game(reply_opcode, reply_payload))
            except OSError as error:
                member.log(f"   开局握手发送失败（{error!r}），忽略")
            # 每条连接自己的状态机跟着走一格，`status` 才不会说瞎话。
            member.start_game.state = room.battle.state
        packets = " ".join(f"0x{op:04x}" for op, _ in replies)
        self.log(f"← 广播开局握手 {packets} 给房里 {len(members)} 人（{why}）")
        # 房间什么时候标「游戏中」（大厅列表跟着变，`Lobby.join` 也会用
        # MOVE_INTO_ALREADY_PLAYING 把半路想进来的人挡在外面 —— 关卡是
        # 开局那一刻按座位表加载的，中途多一个人进来两边就对不上）：
        #
        # ★ 从 0x0400 发出（PREPARING，全员开始加载关卡）就挡，不是等
        #   真进关卡（IN_GAME）：加载中的客户端困在加载界面，**没法重新
        #   按「开始」**。这时放进新人，`finish_join` 会把 `room.battle`
        #   作废，之后每一发 0x0403 轮询都会新建一个停在 WAIT_START 的
        #   状态机 —— 剩下的人永远等不齐（bug调查/1 事故：dk 在 6 人
        #   加载途中进房，全员「还在等 6 人」一直等到散伙）。
        #   倒计时（WAIT_CONFIRM）阶段不挡：那时大家都还在房间界面，
        #   房主可以重新按 F5，「作废 -> 重来」这条路是通的。
        if (room.battle.state == StartGameHandshake.PREPARING
                and room.status != SESSION_STATUS_PLAYING):
            LOBBY.update_room(room, status=SESSION_STATUS_PLAYING)
            self.log(f"   房间 #{room.room_id} -> 游戏中"
                     f"（0x0400 已发、全员加载中 —— 这时进房只会把开局"
                     f"握手作废成死锁，提前挡人）")

        # 真进了关卡（所有人都加载完、一起进 stage 7）之后的收尾。
        if (room.battle.state == StartGameHandshake.IN_GAME
                and room.quest is None):
            # 这一局的战斗状态重新起一份（上一局的掉落物句柄/死亡表全作废）。
            # ★ 控制者表要按**这一刻在座的座位**算，和客户端
            # `GameContext::StartGame` 同一个口径（§180）——
            # 客户端就是在进 stage 7 的路上建它的。
            room.quest = RoomQuest(seats=[i for i, seat in enumerate(room.seats)
                                          if seat is not None])
            # 「准备好了」跟着客户端一起清 —— 它进 stage 6 时自己清了一遍
            # （`LoadingStage` 构造函数，§165）。不跟着清就会两边不一致。
            room.clear_ready()
            # ★ 每局开始都无条件重发一次 0x0410（§150：客户端退房 / 中继断开
            #   都会把通道 A 的开关自己清回 0，服务端缓存的「已经开着」不可信。
            #   bug调查/4：第一局正常、第二局开始有人「自己能动但别人看他
            #   一动不动」，且他的客户端一直每 10 秒发 0x0310 讨通道 —— 就是
            #   开关被清了而服务端以为还开着，一直没重发）。
            self.sync_peer_relay(room, reason="（新一局开始，无条件重发）",
                                 force=True)
            # ★ 关卡加载途中走掉的人，现在补交接（§180 / D103）——
            # 客户端可能在他走之前就把控制者表建好了。**必须排在
            # 那一发 `0x0402` 之后**（上面的循环已经发完了）：客户端要先
            # 进 stage 7 把 GameContext 建起来，才有表可改。
            for seat in list(room.battle.left_while_loading):
                self.handover_controller_slots(
                    room, seat, why="（关卡加载途中走的）", force=True)
            room.battle.left_while_loading.clear()

    def on_start_game_packet(self, opcode, payload):
        """`0x0400` / `0x0402` / `0x0403` —— 开局链（多人，J.3）。

        不在房间里时退回 V0.1 的单连接行为（理论上到不了这儿：开局只能在
        房间里按，但协议试探时会手搓包，别让它炸）。
        """
        room = self.lobby_room()
        if room is None:
            old = self.start_game.state
            replies = self.start_game.on_client_packet(opcode, payload)
            self.log(f"   开局握手(无房间): {old} -> {self.start_game.state}; "
                     f"待下发 {len(replies)} 包")
            for reply_opcode, reply_payload in replies:
                self.send(build_game(reply_opcode, reply_payload))
            return

        battle = self.room_battle(room)
        members = room.members(exclude=None)
        old = battle.state

        if opcode == OP_LOADING_DONE:
            replies = battle.on_loaded(self, members)
            still = battle.waiting_for(members)
            if replies:
                self.log(f"   开局握手: 全部 {len(members)} 人都加载完了 -> 放行")
                self.broadcast_start_game(room, replies, "所有人加载完成，一起进 stage 7")
            else:
                # 客户端每 5 秒轮询一发，别每发都记一行。
                if self not in battle.loaded or still:
                    self.log(f"   开局握手: 我加载完了；还在等 {len(still)} 人")
            return

        # 0x0402（以及协议试探用的 0x0400）——**只认房主的**。
        if self is not room.host_conn:
            self.log("   开局握手: 不是房主发的，忽略（开局是房主的权力）")
            return
        replies = battle.on_host_ready(opcode, payload)
        self.log(f"   开局握手: {old} -> {battle.state}; 待广播 {len(replies)} 包")
        self.broadcast_start_game(room, replies, "房主开局")

    # ---- 战斗内联机：玩家之间的同步数据（§149 / §150 / §151）----------------

    def forget_peer_relay(self):
        """离开房间时跟着客户端把通道 A 的开关记录清回「关」。

        客户端在 `0x406191`（发 `0x0203` 离开房间）里自己就把
        `[GameSession+0x3e4]` 清 0 了。我们不跟着清的话，下次进房
        `send_toggle_peer_relay()` 会以为开关还开着而不重发 `0x0410`，
        结果就是「第二次进房之后再也同步不上」。
        """
        self.peer_relay_on = False
        self.peer_data_dumped = False

    def send_toggle_peer_relay(self, enabled, force=False):
        """发 `0x0410 gspToggleUdpClientCommunication`（载荷 = int32 0/1）。

        客户端处理器 `0x408703` 做两件事：把 `[GameSession+0x3e4]` 设成这个值、
        再给游戏服的 socket 设一次 `TCP_NODELAY`。开关一开客户端立刻开始
        往我们这儿发 `0x040e`（实测约 8 Hz），关掉就立刻停。

        ★ **发送失败必须把标志滚回去**，否则这一发永远不会再试 —— 客户端
        的通道 A 从此关着，表现为「他自己玩得好好的，别人看他一动不动」
        （bug调查/4 最后一局：第一局正常、第二局开始有人静止，且他的客户端
        一直每 10 秒发 `0x0310` 讨通道）。`force=True` 供开局链无条件重发
        （§150：客户端退房 / 中继断开都会自己把开关清回 0，服务端缓存的
        「已经开过」不可信）。
        """
        enabled = bool(enabled)
        if not force and self.peer_relay_on == enabled:
            return
        self.peer_relay_on = enabled
        self.log(f"← 回 0x0410 gspToggleUdpClientCommunication({int(enabled)})"
                 f" —— 玩家间同步{'走本服转发' if enabled else '关闭'}")
        try:
            self.send(build_game(OP_TOGGLE_PEER_RELAY, w_i32(int(enabled))))
        except OSError as error:
            self.log(f"   0x0410 发送失败（{error!r}），标志滚回去等下次重试")
            self.peer_relay_on = not enabled

    def sync_peer_relay(self, room=None, reason="", force=False):
        """按「房里现在有几个人」给房间里每个人开 / 关通道 A。

        一个人的房间不用开 —— 开了客户端也只是每 128 毫秒往我们这儿丢一发
        没人要的包。第二个人进来才开，掉回一个人再关掉。

        ★ 必须在**进房四连发之后**调（`0x0410` 绝不能挤进那一次合并的
        `sendall` 里，V0.1 §120 / D058）。

        `force=True`：不看缓存状态，每人无条件重发一次。开局链在全员进
        stage 7 之后用它 —— 客户端可能在两局之间自己把开关清了（退房 /
        中继断开都会清，§150 / §158），服务端缓存的「已经开着」不可信。
        """
        if room is None:
            room = self.lobby_room()
        if room is None:
            return
        # 按「有几条**连接**」判，不是按 player_count()（它把 `fakeroom` 造的
        # 无连接假座位也算进去，白让真客户端每秒发 8 发没人收的包，§145 同因）。
        members = room.members(exclude=None)
        wanted = len(members) >= 2
        for member in members:
            # ★ 想关（wanted=False）时不看 force：从没开过的连接一发都不多发，
            #   单人房的包序列保持和 V0.1 逐字节一致（test_room 的既定不变量）；
            #   只有「缓存说还开着」的才补一发关。想开（wanted=True）才吃
            #   force —— 开局链靠它无条件重发。
            if not wanted and not member.peer_relay_on:
                continue
            try:
                member.send_toggle_peer_relay(wanted,
                                              force=force and wanted)
            except OSError as error:
                member.log(f"   0x0410 发送失败（{error!r}），忽略")
        if reason:
            self.log(f"   玩家间同步开关 -> {int(wanted)}{reason}")

    def handover_controller_slots(self, room, leaver_seat, why="", force=False):
        """有人离开**正在打的一局** -> 把他扛的控制权交给还在的人（§180 / D103）。

        发 `0x0414 gspChangeControllerSlot(走的人的座位, 接管者的座位)`
        给还留着的每一个人。客户端收到就把类别 20~25 里等于第一个座位号的
        格子全换成第二个，再让世界里每只怪 / 每个刷怪点重问一次
        「这只归不归我」，归自己的当场接管。

        三种情况**一个包都不发**：

        * 房间不在「游戏中」—— 等待房里没有怪，发了没有意义（D103）。
          ★ 例外：关卡**正在加载**（`PREPARING`）时走的人要记下来，
          等真进了关卡再补发 —— 客户端可能已经把他算进控制者表了；
        * 这一局的状态已经丢了（`room.quest is None`）；
        * 走的人一格控制权都不占（那张表本来就是对的）。

        返回接管者的座位号，没发包就是 ``None``。
        """
        if room is None:
            return None
        if not room_in_battle(room):
            if (room.battle is not None
                    and room.battle.note_left_while_loading(leaver_seat)):
                self.log(f"   控制权: 座位 {leaver_seat} 在关卡加载途中走了，"
                         f"等进了关卡再补一发交接")
            return None
        quest = room.quest
        if quest is None:
            return None
        members = room.members(exclude=None)
        if not members:
            return None
        survivors = [i for i, seat in enumerate(room.seats) if seat is not None]
        heir = quest.handover_controller(leaver_seat, survivors, force=force)
        if heir is None:
            self.log(f"   控制权: 座位 {leaver_seat} 没扛着任何一格，"
                     f"不用交接{why}")
            return None
        packet = build_game(OP_CHANGE_CONTROLLER_SLOT,
                            build_change_controller_slot(leaver_seat, heir))
        sent = 0
        for member in members:
            try:
                member.send(packet)
                sent += 1
            except OSError as error:
                member.log(f"   0x0414 发送失败（{error!r}），忽略")
        self.log(f"← 广播 0x0414 gspChangeControllerSlot("
                 f"座位 {leaver_seat} -> {heir}) 给 {sent} 人{why}"
                 f" —— 怪 / 刷怪点改由座位 {heir} 模拟"
                 f"（现在的表 {quest.controllers}）")
        return heir

    def maybe_join_relay(self):
        """回一发 `0x0210 gspJoinRelay`，让客户端接上原版的 TCP 中继（D078）。

        ★★ **一条游戏连接只回一次。** 客户端收到就无条件 `new RelayConnection`
        并覆盖全局指针 `[0x72e290]`，旧对象既不释放也不关 socket ——
        等它哪天收到 FD_CLOSE，`OnDisconnected` 照样触发，
        把**新**连接的指针清成 0 再发一发 `0x0203` 把玩家踢出房间（§158 / §159）。
        而客户端的 `0x0310` 是**每个别人坐着的座位每 10 秒一发**，
        重复请求是常态，去重责任 100% 在服务端 —— `RelayServer.issue()` 挡着。

        地址固定填 `127.0.0.1:<中继端口>`：客户端的 `connect` 参数是纯 IPv4 的
        `sockaddr_in`，服务端又不知道自己的公网地址（和注册页同一个老问题），
        所以沿用 47611 / 27799 那一套 —— 由客户端包的 `bshook` 按「本机服务器 /
        远程服务器」把这个端口转出去（D065 / D066 / D079）。
        """
        if not TCP_RELAY_ENABLED:
            return False
        room = self.lobby_room()
        if room is None:
            return False
        seat_index = room.seat_index_of(self)
        if seat_index is None:
            return False
        auth = PEER_RELAY.issue(self, room.room_id, seat_index)
        if auth is None:            # 已经回过了，绝不重发
            return False
        self.log(f"← 回 0x0210 gspJoinRelay 127.0.0.1:{PEER_RELAY.port} "
                 f"认证={auth} —— 客户端这就去连原版中继")
        self.send(build_game(OP_JOIN_RELAY, relayserver.build_join_relay(
            "127.0.0.1", PEER_RELAY.port, auth)))
        return True

    def leave_relay(self, reason=""):
        """发 `0x0211` 让客户端**干净地**拆掉中继连接。

        ★ 这是唯一安全的拆法：`0x55437b` 走的是析构（`0x54bcb3`），
        **不经过 `OnDisconnected`** —— 而后者会让客户端自己发 `0x0203`
        退出房间（§158）。所以任何时候都别去关中继的 socket，要拆就发这个包。
        """
        if not PEER_RELAY.has_issued(self):
            return False
        PEER_RELAY.forget(self)
        self.log(f"← 回 0x0211 拆掉中继连接{reason}")
        try:
            self.send(build_game(OP_LEAVE_RELAY))
        except OSError as error:
            self.log(f"   0x0211 发送失败（{error!r}），忽略")
        return True

    def on_start_tcp_relay(self, payload):
        """`0x0310 gcpStartTcpRelay`（8 字节 = 我的座位 + 对方座位，§152）。

        客户端在要一条到对方的中继通道，房里每个「别人坐着的座位」每 10 秒一发。
        两件事：确认通道 A 的总开关是开的（走中继也要它，§157 末尾），
        再看要不要回 `0x0210`。

        ★ 自愈（bug调查/4）：客户端反复发 `0x0310` 本身就是「它那边通道断了」
        的信号 —— 中继 TCP 半死（NAT 超时 / 网络抖动）时客户端收不到 FIN，
        会一边继续玩一边每 10 秒讨一次通道。以前服务端因为「一条游戏连接
        只回一次 0x0210」的铁律永远不理它。现在：只要服务端这边**确实**没有
        一条活着的中继连接（没注册过 / 掉了 / 发送流已废 / 长时间没有任何
        入站），就先 `0x0211` 让客户端干净拆掉旧对象（不走 `OnDisconnected`，
        不会被踢出房间），再发一张**新票据**让它重连 —— 中断的几秒里同步
        自动走 `0x040e/0x040f` 回退路径，不断流。铁律 2 防的「两张活票据
        并存」在这里不成立：重发前 `leave_relay()` 已经把旧票据作废。
        """
        if len(payload) >= 8:
            mine, other = struct.unpack_from("<ii", payload, 0)
            self.vlog(f"   要中继通道: 我={mine} 对方={other}")
        self.sync_peer_relay(reason="（收到 0x0310 要中继）")
        self.maybe_join_relay()
        self.recover_peer_relay()

    def recover_peer_relay(self, min_interval=15.0, redeem_grace=10.0):
        """中继连接已经不在了（或半死了）就重发一轮 `0x0211` + 新 `0x0210`。

        `min_interval` 节流：客户端一秒钟可以来好几发 `0x0310`（每个座位
        每 10 秒一发），重发一轮就够了，别把它刷成闪烁。
        `redeem_grace`：票据刚签发的这几秒里客户端可能正在连（异步的），
        这时它还没注册上来是**正常**的，不能当成「连接已死」去重发。
        """
        if not TCP_RELAY_ENABLED:
            return False
        if self.lobby_room() is None:
            return False
        now = time.monotonic()
        if now - self.last_relay_reissue_at < min_interval:
            return False
        relay = PEER_RELAY.conn_for(self)
        if relay is not None and not PEER_RELAY.stalled(self):
            return False                    # 通道活着，不用动
        # 走到这：要么压根没有连接（票据已兑过 → 曾经有过），要么连接半死。
        if relay is None:
            if not PEER_RELAY.has_issued(self):
                return False                # 从没发过票据，maybe_join_relay 管
            issued_at = PEER_RELAY.issued_at(self)
            if (issued_at is not None
                    and time.time() - issued_at < redeem_grace):
                return False                # 票才发出去，先给客户端连接的窗口
        self.last_relay_reissue_at = now
        why = "连接半死（长时间无入站）" if relay is not None else "连接已不在"
        self.log(f"   ★ 客户端在讨中继通道而服务端这边的{why}："
                 f"重发 0x0211 拆旧 + 新票据 0x0210 让它重连")
        self.leave_relay(reason="（自愈：中继连接已废）")
        self.maybe_join_relay()
        return True

    def on_report_hack(self, payload):
        """`0x0106 gcpReportHack` —— 客户端自己觉得不对劲时的上报。**只记不回**。

        ★ **调试级**（D112）。它当初是「连按 A/D 会不会被客户端当成异常输入」
        的取证口（§183），而那件事已经结案（§186 / D106）。客户端自带的
        `(FastFire)` 判据只看按键频率，玩家一激动就连报几十上百行 ——
        典型的「频率由客户端决定」的噪声，不该占着运营流水。
        """
        text = parse_report_hack(payload)
        who = self.account_name or "?"
        self.online_debug(f"⚠ 客户端上报异常 账号={who!r} ip={self.peer()} 正文={text}")

    def on_peer_data(self, payload):
        """`0x040e` —— 把玩家之间的同步数据转给同房间的其他人（§149）。

        载荷是**一个完整的 `UdpPacket`**（12 字节头 + body，见 §151），
        除了头里那个局号（`+4`，见 `relayserver.peer_game_id`）之外
        **一个字节都不改**地放进 `0x040f` 再发出去 —— 客户端收到后
        `0x4086b5` 剥掉外层 10 字节头，剩下的就还原成原来那个 `UdpPacket`，
        和 UDP 直连收到的走同一个入口 `0x407869`。

        ★ 为什么可以无脑广播：三个发送点（`0x4058cc` / `0x4077db` /
        `0x408257`）在调 `0x408619` 之前都把头里的**目标座位写成 0xff**
        （广播），这条通道上不存在单播。
        ★ 为什么重复投递无害：客户端按头里的 u16 序列号在
        `PktQueue::Insert`（`0x54bb8c`）里去重，同一号收两次会被丢掉 ——
        所以局域网里 UDP 那一路也送到了也不会双重结算（§151）。
        """
        self.peer_data_in += 1
        room = self.lobby_room()
        if room is None:
            return
        if not self.peer_data_dumped:
            self.peer_data_dumped = True
            self.log(f"   玩家间同步：本房间第一发 {len(payload)} 字节\n"
                     f"{hexdump(payload)}\n"
                     f"{describe_peer_header(payload)}")
        # ★ 到达间隔量的是**这条 TCP 连接**上的到达节奏，和 UDP 那条路无关 ——
        #   它是「客户端发得准不准 / 链路抖不抖」的尺子（bug调查/9 就是靠它定的案），
        #   语义必须和历史日志保持一致，所以放在去重**之前**。
        arrived = time.monotonic()
        with self.peer_lock:
            if self.peer_last_at is not None:
                self.peer_gap_ms.add((arrived - self.peer_last_at) * 1000.0)
            self.peer_last_at = arrived
            self.sync_peer_epoch(payload)
            if udpsync.is_heartbeat(payload):
                if not self.peer_order.take_tcp(payload):
                    # UDP 那一份已经先送到了，这一份是它的影子 —— 丢掉。
                    # **绝不能两份都转**：心跳没有任何可判新旧的原版字段，
                    # 后到的那份会把角色拉回旧位置（`udpsync` 铁律 4）。
                    return
            else:
                # 事件包（内层 < 0x4000）永远走 TCP，这里只记账 ——
                # 「已经转发到第几发事件」是放行 UDP 心跳的硬判据（铁律 3）。
                self.peer_order.note_event(udpsync.peer_sequence(payload))
            self.forward_peer_data(payload, arrived)

    def sync_peer_epoch(self, payload):
        """局号一变就把排序闸门里的**事件计数**归零（`udpsync` 铁律 3）。

        发送方换代时 `ResetQueues` 会把自己的事件序号清回 0，我们这边的
        「已转发几发事件」必须同步清，否则新一代的心跳（N 从 0 起）会被
        上一代的旧账放行得太宽。索引那两个计数器**不清** —— `bshook` 那边
        是按连接数的，两边要用同一个起点。
        """
        game_id = relayserver.peer_game_id(payload)
        if game_id is None:
            return
        if self.peer_order_epoch is None:
            self.peer_order_epoch = game_id
            return
        if game_id != self.peer_order_epoch:
            self.peer_order_epoch = game_id
            self.peer_order.new_epoch()

    def feed_peer_udp(self, index, payload):
        """UDP 那条路的入口 —— `udpsync` 收到位置数据后调它。

        ★ 只有**位置心跳**能走到这儿（`udpsync._on_data` 已经把非心跳挡掉了），
        这里再过一遍排序闸门：旧的不要、会越过还没转发的事件包的不要。
        """
        room = self.lobby_room()
        if room is None:
            return
        with self.peer_lock:
            self.sync_peer_epoch(payload)
            if not self.peer_order.take_udp(index, payload):
                return
            self.peer_data_in += 1
            self.forward_peer_data(payload, time.monotonic())

    def forward_peer_data(self, payload, arrived):
        """两条路合流之后**唯一**的转发出口。

        ⚠ `PEER_RELAY.deliver()` 会顺带调 `_relay_battle_tick`（房间级的
        「每帧问一次」），所以它**只能在去重之后被调一次** —— UDP 和 TCP
        各调一次的话，对战计时和道具刷新都会变成两倍速。
        """
        # ★ 走 `PEER_RELAY.deliver` 而不是直接广播 `0x040f`：房里可能有人已经
        #   接上原版中继了，那些人要走中继收（原版路径），剩下的才走 `0x040f`。
        #   两条路在客户端进的是同一个入口 `0x407869`，谁收哪条都一样。
        self.peer_data_out += PEER_RELAY.deliver(self, payload)
        if self.peer_out_last_at is not None:
            self.peer_out_gap_ms.add((arrived - self.peer_out_last_at) * 1000.0)
        self.peer_out_last_at = arrived
        self.peer_forward_ms.add((time.monotonic() - arrived) * 1000.0)
        self.report_peer_timing(arrived)
        # ★ 「每帧问一次」的房间级判断（对战时间上限、道具模式刷道具）**不在
        #   这里**，在 `_relay_battle_tick` 里 —— 上面那发 `deliver()` 会调它。
        #   原因见那个函数：中继一建起来，`0x040e` 整局就不再出现了（§160），
        #   挂在本函数上等于在中继模式下彻底失效。

    def report_peer_timing(self, now=None, force=False):
        """每 `PEER_TIMING_REPORT_INTERVAL` 秒往 `[online]` 汇总一行转发耗时。

        **不是逐包打** —— 逐包正是 §182 之前那种「跟游戏抢磁盘 I/O」的老毛病，
        NOISY_OPCODES 就是为它存在的。
        """
        now = time.monotonic() if now is None else now
        if not force and now - self.peer_report_at < PEER_TIMING_REPORT_INTERVAL:
            return
        self.peer_report_at = now
        forward = self.peer_forward_ms.summary()
        if forward is None:
            return
        gap = self.peer_gap_ms.summary()
        out_gap = self.peer_out_gap_ms.summary()
        udp = self.peer_order.summary()
        who = self.account_name or "?"
        # ★ 这一行是**调试级**（D112）：它每 30 秒一发、每条连接各一份，
        #   一局下来能把 online.log 撑得比运营事件多一个数量级，
        #   而它只在专门查延迟的那几天有用（§187 那一轮就是靠它量出来的）。
        #
        # ★★ 「到达间隔」和「转发间隔」要并排看：前者是这条 **TCP** 上的到达
        #    节奏（跨境线路抖不抖），后者是**真正转给别人**的节奏（别人屏幕上
        #    的流畅度）。UDP 旁路生效时，后者的 p95 会明显比前者小 ——
        #    bug调查/9 那一局前者 p95=432ms，后者应该在 ~130ms。
        self.online_debug(f"同步转发 账号={who!r} 转发耗时 {forward}"
                          + (f"；到达间隔 {gap}" if gap else "")
                          + (f"；转发间隔 {out_gap}" if out_gap else "")
                          + (f"；{udp}" if udp else "；UDP 未启用"))
        self.peer_forward_ms.reset()
        self.peer_gap_ms.reset()
        self.peer_out_gap_ms.reset()

    def broadcast_system_chat(self, text):
        """房间里的一行系统提示（没有「谁 : 」前缀，§141）。"""
        if not text:
            return
        self.broadcast(build_game(OP_CHAT, build_receive_chat(text)),
                       reason="：系统提示")

    def on_chat(self, payload):
        """`0x0305 gcpSendChatMsg` -> `gspReceiveChatMsg` 广播（§141）。

        **不持久化**（里程碑 I 的要求）。房间里就发给房里的人；不在房间里
        （大厅）暂时只回给自己 —— 频道聊天要先有频道用户表，那是 `0x020d`
        那条线的事，还没做。
        """
        try:
            chat_type, text = parse_chat_message(payload)
        except (ValueError, struct.error) as error:
            self.log(f"   聊天包解析失败: {error}; 不回包")
            return
        text = text.strip()
        if not text:
            return
        sender = display_name(self.account) or (self.account_name or "")
        room = self.lobby_room()
        seat_index = CHAT_NO_SEAT
        if room is not None:
            index = room.seat_index_of(self)
            if index is not None:
                seat_index = index
        packet = build_game(OP_CHAT, build_receive_chat(
            text, sender=sender, seat_index=seat_index, chat_type=chat_type))
        # ★ 发言的人自己也要收到一份 —— 客户端发完 0x0305 什么都不做，
        #   本地不回显（和换角色 §103 是同一个套路）。
        self.log(f"   聊天(type={chat_type}) {sender!r}: {text!r} "
                 f"座位={seat_index if seat_index != CHAT_NO_SEAT else '-'}")
        self.send(packet)
        if room is not None:
            self.broadcast(packet, reason="：聊天")

    def on_kick_out(self, payload):
        """客户端方向的 `0x030b gcpKickOut` —— 房主踢人。

        ⚠ 同号反向：服务端方向的 `0x030b` 是座位物品清单。
        """
        try:
            seat_index, flag = parse_kick_out_request(payload)
        except (ValueError, struct.error) as error:
            self.log(f"   踢人请求解析失败: {error}; 不动作")
            return
        room = self.lobby_room()
        if room is None:
            self.log("   不在任何房间里，忽略踢人请求")
            return
        my_seat = room.seat_index_of(self)
        if my_seat != room.host_seat:
            # 只有房主能踢。客户端 UI 本来就只给房主那个按钮，但服务端不能
            # 因此就信它 —— 这是一条改包就能滥用的路。
            self.log(f"   座位 {my_seat} 不是房主（房主 {room.host_seat}），"
                     f"拒绝踢人")
            return
        if seat_index == my_seat:
            self.log("   房主想踢自己，忽略")
            return
        victim_seat = room.seats[seat_index] if 0 <= seat_index < ROOM_SEAT_COUNT else None
        victim = victim_seat.conn if victim_seat is not None else None
        self.log(f"   踢人: 座位 {seat_index}"
                 f"（{victim_seat.nickname if victim_seat else '空位'}）flag={flag}")
        if victim is None:
            return
        result = LOBBY.kick(room, seat_index)
        if result is None:
            return
        self.online(f"房间 ⚠ 踢出 房间 #{room.room_id} "
                    f"被踢={victim.account_name!r} 房主={self.account_name!r}")
        # 被踢的人：回一发 0x0203，客户端自己切回大厅（§101）。
        victim.log("← 回 gspRepLeaveSession(result=0) —— 被房主踢出")
        try:
            victim.send(build_game(OP_LEAVE_SESSION, build_rep_leave_session(0)))
        except OSError as error:
            victim.log(f"   踢出通知发送失败（{error!r}）")
        victim.room = None
        victim.my_seat = 0
        victim.forget_peer_relay()
        victim.reset_quest_state()
        victim.start_game.reset()
        self.after_someone_left(result, f"{victim_seat.nickname} 被房主请出了房间。")

    def after_someone_left(self, result, system_text=""):
        """有人离开房间之后，给**还留着的人**补广播。

        三件事：座位变更（`0x0301` **action 1**）、房主换人了就再补一发座位快照、
        一行系统提示。房间已经解散就什么都不用发。

        ★ action 必须是 1（`SEAT_ACTION_LEAVE`），不能是 3 —— 只有 1/2 会走
        `0x405f8f` 把座位的 3D 模型销毁掉。发 3 的话「玩家列表」里的名字确实没了
        （那是座位数据被反序列化冲掉的效果），但房间上方那块天空里的角色模型
        会一直杵着不走（§147）。
        """
        if result is None or result.closed:
            return
        room = result.room
        leave_packet = build_game(OP_SESSION_MEMBER_UPDATE,
                                  build_session_member_update(
                                      result.seat_index, SEAT_ACTION_LEAVE,
                                      occupied=False))
        chat_packet = (build_game(OP_CHAT, build_receive_chat(system_text))
                       if system_text else b"")
        for other in result.remaining:
            try:
                with other.send_batch("；离开广播合并"):
                    other.send(leave_packet)
                    if result.new_host_seat is not None:
                        # 房主换人了：整份座位快照重发一次最稳
                        # ——「谁是房主」只有 0x0300 的第一个 int32 说了算。
                        other.send_session_members()
                    if chat_packet:
                        other.send(chat_packet)
            except OSError as error:
                other.log(f"   离开广播发送失败（{error!r}），忽略")
        if result.new_host_seat is not None:
            self.log(f"   房主转移到座位 {result.new_host_seat}")
        # 掉回一个人就把玩家间同步关掉，省得客户端对着空房间每秒发 8 发。
        self.sync_peer_relay(room, reason="（有人离开房间）")
        # ★ 走的人可能正扛着「怪 / 刷怪点由谁模拟」的控制权 —— 不交接的话
        #   剩下的人再也刷不出怪、关卡的闸门永远不开（§180 / D103）。
        self.handover_controller_slots(room, result.seat_index)
        # ★ 走的人可能正是「还没加载完」的那一个 —— 不重新算一次的话，
        #   剩下的人会永远卡在加载界面等一个已经不在房里的人。
        members = room.members(exclude=None)
        if room.battle is not None:
            replies = room.battle.on_loaded(None, members) if members else []
            if replies:
                self.log("   开局握手: 等的人走了，剩下的已经全加载完 -> 放行")
                self.broadcast_start_game(room, replies, "等的人走了，放行")
        # 同一个道理，换图也别等一个已经走了的人（`0x0412` 那条链）。
        quest = room.quest
        if (quest is not None and members and quest.pending_map is not None
                and quest.map_done(None, members)):
            quest.finish_map_change()
            self.log("   换图: 等的人走了，剩下的已经全加载完 -> 放行 0x0418")
            packet = build_game(OP_MAP_CHANGE_READY)
            for other in members:
                try:
                    other.send(packet)
                except OSError as error:
                    other.log(f"   0x0418 发送失败（{error!r}），忽略")
        # ★ 走的人可能是对战里的最后一个对手 —— 「只剩一边了」这一条要立刻
        #   重新判一次，否则剩下的人会一直站在空地图上等（§167）。
        #   自己已经不在房里了，所以要让**留下的人**去判。
        if members:
            members[0].check_pvp_finished()

    def leave_room(self, system_text=""):
        """把自己从大厅房间里摘掉并广播。退房 / 断线 / 被顶号共用。"""
        result = LOBBY.leave(self)
        if result is None:
            return None
        who = display_name(self.account) or (self.account_name or "?")
        self.online(f"房间 - 离开 账号={self.account_name!r} "
                    f"房间 #{result.room.room_id} 座位={result.seat_index} "
                    + ("房间已解散" if result.closed
                       else f"房里还剩 {len(result.remaining)} 人"))
        self.after_someone_left(result, system_text or f"{who} 离开了房间。")
        return result

    def leave_session(self):
        """离开房间：回 `0x0203 result=0`，客户端自己切回大厅（FINDINGS §101）。

        `0x0203`（客户端方向）只有一个发送点 `0x406191`，四个调用方共用：

        * `0x46739c` —— RoomStage 的「90 秒没动作」提示框弹完顺手发的
        * `0x4a50f4` / `0x4a5a85` —— 房间里的退出/ESC
        * `0x54be4c` —— 网络层的状态处理

        也就是说**「挂机踢出」和「玩家自己退房」发的是同一个空包**，
        服务端无法区分，也不需要区分：两种情况都该让客户端回大厅。

        不回的后果（会话 12 实测）：客户端留在房间里，`RoomStage::Update`
        每 90 秒重新弹一次提示框，弹出来的框一个摞一个 ——
        用户看到的就是「点确认没反应、关不掉」。
        """
        self.log("← 回 gspRepLeaveSession(result=0) —— 离开房间，客户端切回大厅")
        self.send(build_game(OP_LEAVE_SESSION, build_rep_leave_session(0)))
        # 房间没了，跟房间绑定的状态全部作废，否则下次建房会带着上一局的残留。
        self.room = None
        self.my_seat = 0
        self.forget_peer_relay()
        self.reset_quest_state()
        self.start_game.reset()
        # 大厅那边也要摘掉，并把「谁走了 / 房主换成谁」广播给还留着的人。
        self.leave_room()

    def leave_game_result(self):
        """结算界面看完了：切回房间，再把玩家数据栏刷成存档里的值。

        ★ **回房间后金币会变成 0**（会话 11 实测，§100）—— 经验和等级都还在，
        唯独金币这一格被清掉。补一发 `0x0600` 就好了，顺带也让经验/等级和存档
        重新对齐一次。

        座位的物品清单也顺手补一发 `0x030b`。这一发是**防御性**的：
        `0x406e4e`（把某个座位的清单重建成空的）有五个调用点，其中
        `0x40f55c` / `0x40f619` / `0x40f7b9` 都在切 stage 的路上，
        没有逐条读到底。实测走「关卡 → 结算 → 回房间」这条路清单**没被清**，
        但重发一次是幂等的整份替换，成本一个包，比漏了让「人物选择」
        缩回 3 个头像划算（§119）。

        ★ 三个包必须一起进客户端的同一次 recv：`0x0403` 的处理器 `0x551904`
        最后就是 `ChangeStage(5)`，房间 UI 会在**下一帧**重建「人物选择」
        的头像按钮。晚一帧到的 `0x030b` 就白发了（§120，和建房那一处同因）。
        """
        with self.send_batch("；回房间三连发不能被客户端的 recv 切开"):
            self.log("← 回 0x0403（结算看完 -> 切回 stage 5 房间）")
            self.send(build_game(OP_LOADING_DONE, b""))
            self.send_rep_money(reason="（回房间后金币会被清 0，重新同步）")
            self.send_slot_equipped_list(reason="（回房间后清单会被重建，重新同步）")
        # 回到房间就可以再开一局，把开局状态机和本局的关卡状态复位。
        self.reset_room_for_next_round()
        self.reset_quest_state()
        self.start_game.reset()

    def reset_room_for_next_round(self):
        """结算看完回到房间：把**房间**复位，好让房主能再开一局。

        ★ 三件事缺一不可：

        1. `room.status` 回「待机中」—— 不回的话大厅列表永远写「游戏中」，
           而且 `Lobby.join` 会一直用「此房间已开始游戏。」把新人挡在门外。
        2. `room.battle.reset()` —— 开局握手的状态机停在 `IN_GAME`，
           房主再按一次 F5 发来的 `0x0402` 会被 `StartGameHandshake`
           当成「已经在游戏里了」直接丢掉，**第二局根本开不起来**。
        3. `room.quest = None` —— 上一局的掉落物句柄、拾取表、死亡去重表、
           `settled` 标志全部作废。
        4. `room.clear_ready()` —— 准备状态跟客户端一起清（§165）。

        幂等：房里每个人看完结算都会走这一条，谁先到谁做，后面的再做一遍
        也是同样的结果。
        """
        room = self.lobby_room()
        if room is None:
            return
        if room.status != SESSION_STATUS_WAITING:
            LOBBY.update_room(room, status=SESSION_STATUS_WAITING)
            self.log(f"   房间 #{room.room_id} -> 待机中（本局结束，可以再开一局）")
        if room.battle is not None:
            room.battle.reset()
        room.quest = None
        # 4. 准备状态清掉。客户端在进「加载中」那一格时已经自己清过一遍
        #    （§165），这里跟上，免得下一局两边对不上。
        room.clear_ready()

    def reset_quest_state(self):
        """把「跟这一局关卡绑定」的状态全部清掉，准备下一局。

        ★ `quest_success` 一定要清：不清的话打通一次之后，后面每一局
        （哪怕是被时间耗光的）结算界面都会挂着「完成」。
        `items_created` / `items_picked` 只影响日志的「本局第一件」判断。

        ★ 房间级那一份（掉落物句柄 / 拾取表 / 换图等谁）由
        `reset_room_for_next_round()` 负责；这里只清连接自己的，外加
        「不在房间里时」用的那份 `solo_quest`。
        """
        self.settled = False
        self.quest_success = False
        self.quest_score = 0
        self.solo_quest = RoomQuest()
        self.items_created = 0
        self.items_picked = 0
        self.attrs_removed = 0

    # -- 帧处理 ------------------------------------------------------------
    def on_game_packet(self, opcode, payload):
        self.last_packet_at = time.time()
        name = GCP_NAMES.get(opcode, "?")
        # 高频包非 verbose 时只报第一次，之后静音 —— 应答逻辑照常走，只是不记。
        noisy = opcode in NOISY_OPCODES and not VERBOSE
        if not noisy or opcode not in self.noisy_seen:
            self.log(f"★ 游戏包 opcode=0x{opcode:04x} ({name}) 载荷 {len(payload)} 字节"
                     + (f"\n{hexdump(payload)}" if VERBOSE else
                        "（高频包，后续同号静音；--verbose 可全记）" if noisy else ""))
        self.noisy_seen.add(opcode)
        # 试着按 "string + int32*" 解一下（gcpReqLogin 0x0100 就是这个形状）。
        # ★ 整段夹在 VERBOSE 里：这个试解**只为日志**，非 verbose 时结果直接丢掉，
        #   而战斗中每发 0x040e 都会走到这里（而且每次都以抛异常收场，§187）。
        if VERBOSE and opcode == 0x040e:
            # ★ 同步包不是 "string + int32*" 那个形状，硬试必然抛（§187）。
            #   verbose 下把 12 字节的 UdpPacket 头**逐发**解出来：出问题时
            #   要对的就是「谁在哪个局号发到第几号、内层是什么」（§216）。
            self.vlog(describe_peer_header(payload))
        elif VERBOSE:
            try:
                r = Reader(payload)
                s = r.wstr()
                ints = []
                while r.left() >= 4:
                    ints.append(r.i32())
                self.vlog(f"   试解: str={s!r} ints={ints} 剩余={r.left()}")
            except Exception as e:
                self.vlog(f"   试解失败: {e}")

        if self.args.hold_lobby:
            self.log("   [hold-lobby] 不回应答")
            return
        if opcode == 0x0100:
            self.on_game_login(payload)
        elif opcode == OP_LIST_SESSION:
            self.on_list_session(payload)
        elif opcode == OP_MOVE_INTO_SESSION:
            self.on_move_into_session(payload)
        elif opcode == OP_QUICK_JOIN_SESSION:
            self.on_quick_join_session(payload)
        elif opcode == OP_CHAT:
            self.on_chat(payload)
        elif opcode == 0x0201:
            self.start_game.reset()
            self.room = None
            self.forget_peer_relay()
            self.reset_quest_state()
            # 建新房之前先从旧房间里出来（并广播），否则旧房间会留一个幽灵座位。
            self.leave_room()
            try:
                self.room = parse_create_session_request(payload)
                self.log(
                    "   建房参数: "
                    f"type={self.room['session_type']} "
                    f"({self.room['session_type_name']}) "
                    f"texts={self.room['texts']!r} option={self.room['option']} "
                    f"args={self.room['arguments']} "
                    f"{describe_room_arguments(self.room['session_type'], self.room['arguments'])}"
                    "; 开局状态机已重置"
                )
                self.register_room()
            except ValueError as error:
                # Keep the minimal successful reply while logging an exact
                # diagnostic. This preserves compatibility with an unobserved
                # regional packet variant without hiding the mismatch.
                self.log(f"   建房参数解析失败: {error}; 开局状态机仍已重置")
            # ★ 这四个包必须一起进客户端的**同一次 recv**，否则「人物选择」
            #   会小概率缩回 3 个头像 —— 房间 UI 只在构造时建一次按钮，
            #   而 0x0201 一被分发就排好了 ChangeStage（§120）。
            # ★ 顺序是硬约束：0x0303 必须排在 0x0201 应答**之前**。
            # 建房应答处理器 0x54f747 在 0x54f875 处直接读 [LobbyStage+0x1c]
            # 决定建哪个房间面板；`LobbyStage` 构造函数 0x4052ff 把它初始化成
            # -1，客户端自己永远不会填。先回 0x0201 的话，那一刻描述符还是
            # -1，客户端就会建 PvP 面板，其每帧刷新拿 -1 去索引模式名表读到
            # 空指针，约 5 秒后 C0000005 崩溃（FINDINGS §64 / §65）。
            with self.send_batch("；建房四连发不能被客户端的 recv 切开"):
                self.send_update_session()
                self.log("← 回 gspRepCreateSession(result=0, session_id=1)")
                self.send(build_game(0x0201, build_rep_create_session(1)))
                # 反过来，座位快照必须排在 0x0201 **之后**：0x54f815 会把座位 0
                # 的角色 id 清零，先发就被冲掉。
                self.send_session_members()
                # 再补一发座位的物品清单。它决定「人物选择」里有几个头像，
                # 而且必须排在 0x0300 之后（持有判定要先看座位已占用，§119）。
                self.send_slot_equipped_list(reason="（建房后下发）")
        elif opcode == OP_SESSION_MEMBER_UPDATE:
            self.on_seat_change(payload)
        elif opcode == OP_SEAT_READY:
            self.on_toggle_ready(payload)
        elif opcode == OP_KICK_OUT:
            # ⚠ 同号反向：0x030b 服务端方向是座位物品清单，客户端方向是踢人。
            self.on_kick_out(payload)
        elif opcode == OP_LEAVE_SESSION:
            self.leave_session()
        elif opcode == OP_CHANGE_SESSION:
            try:
                request = parse_change_session_request(payload)
            except ValueError as error:
                self.log(f"   换房请求解析失败: {error}; 不回包")
                return
            self.log(
                "   换房参数: "
                f"type={request['session_type']} "
                f"({request['session_type_name']}) "
                f"texts={request['texts']!r} args={request['arguments']} "
                f"{describe_room_arguments(request['session_type'], request['arguments'])} "
                f"free_slots={request['free_slots']} "
                f"随机图={'开' if request['random_map'] else '关'} "
                f"flags={request['flags']}（第 2 格是栈垃圾，别读）"
            )
            # 客户端在这里把它选定的地图名提交上来，服务端把整份 Session
            # 广播回去，房间的「选择地图」面板才会显示出关卡。此时地图名
            # 必须非空 —— 建房那一次留空的约束只针对 0x0201 那一步。
            if self.room is None:
                self.log("   没有已解析的建房请求; 不回 0x0303")
                return
            room = self.lobby_room()
            # ★★ 开局之后还会来一发：随机地图模式下，房主在处理
            #    `0x0400 gspPrepareGame` 时自己挑好图（`0x551699` 用包里的 seed）、
            #    写进 `[LobbyStage+0x10]`，**然后回发一发 `0x0302` 把结果报上来**
            #    （`0x551774` 先比「我是不是房主」，`0x551799` 才发）。
            #    这一发是**汇报**不是请求：全房间本来就用同一个 seed 各自算出
            #    同一张图，不需要广播。而此刻大家正在 stage 6 加载关卡，
            #    一发 `0x0303` 会把 Session 的状态字段和局号重新灌进去 ——
            #    那是没验过的动作。所以只记账。
            if room_started(room):
                LOBBY.update_room(room, map_name=request["texts"][1],
                                  random_map=request["random_map"])
                self.log(f"   开局后的地图汇报: map={request['texts'][1]!r} "
                         f"随机图={'开' if request['random_map'] else '关'}"
                         f" —— 只记账，不回 0x0303")
                return
            self.room = dict(self.room,
                             session_type=request["session_type"],
                             arguments=request["arguments"])
            # 房间在大厅里也要跟着改：房间列表和后进来的人读的是大厅那一份。
            regrouped = []
            if room is not None:
                old_layout = room.team_layout()
                LOBBY.update_room(room,
                                  title=request["texts"][0] or room.title,
                                  map_name=request["texts"][1],
                                  session_type=request["session_type"],
                                  arguments=request["arguments"],
                                  random_map=request["random_map"])
                # ★ 房主在房间里点了「组队战 / 个人战」就会走到这儿，而分队
                #   口径是跟着模式走的（§165）：组队战按座位奇偶分两队、
                #   个人战每人一队、闯关全在一队。**只在口径真的变了时重排** ——
                #   否则会把别人手动选的队伍冲掉。
                if room.team_layout() != old_layout:
                    regrouped = room.reassign_teams()
                    if regrouped:
                        self.log(f"   模式变了（{old_layout} -> "
                                 f"{room.team_layout()}），重排队伍: "
                                 + "、".join(
                                     f"座位 {i}->{room.seats[i].team}"
                                     for i in regrouped))
            self.send_update_session(map_name=request["texts"][1],
                                     random_map=request["random_map"])
            # 房里其他人也要看到新地图 —— 不然他们的「选择地图」面板还停在旧的。
            if room is not None and room.player_count() > 1:
                others = build_game(OP_UPDATE_SESSION, build_update_session(
                    room.session_type, room.arguments, title=room.title,
                    map_name=room.map_name, status=room.status,
                    player_count=room.player_count(),
                    game_id=room.epoch_value,
                    random_map=room.random_map))
                self.broadcast(others, reason="：房间参数变更")
            # 重排过的座位要挨个广播出去（action 3 = 灌数据 + 重建模型 +
            # 刷 UI，不播任何提示），否则名牌颜色和站位还停在旧模式上。
            for index in regrouped:
                self.broadcast_seat_slot(room, index, SEAT_ACTION_RESYNC,
                                         reason=f"：座位 {index} 改队伍（模式变了）")
        elif opcode == OP_MOVE_CHANNEL_BY_GAME_TYPE:
            try:
                game_type = parse_move_channel_by_game_type(payload)
            except ValueError as error:
                self.log(f"   切频道请求解析失败: {error}; 不回包")
                return
            name = GAME_TYPE_NAMES.get(game_type, "unknown")
            channel_code = GAME_TYPE_CHANNEL_CODES.get(game_type)
            if channel_code is None:
                # 类型 6（练习标签）没有任何频道码能映射回来，服务端无法
                # 把客户端移进去。回失败包只会弹原版韩文错误框，不如不回。
                self.log(f"   请求游戏类型 {game_type} ({name}) 没有对应频道码; 不回包")
                return
            self.channel_code = channel_code
            self.log(f"   请求游戏类型 {game_type} ({name}) -> 频道码 {channel_code}")
            self.log(f"← 回 gspRepMoveInto(ok=1, channel_code={channel_code}, "
                     f"channel_index={self.channel_index})")
            self.send(build_game(
                OP_REP_MOVE_INTO,
                build_rep_move_into(True, channel_code, self.channel_index)))
        elif opcode == OP_REQ_USER_LIST:
            self.on_req_user_list(payload)
        elif opcode == OP_REQ_FIRST_USER_RESULT:
            # 教程跑完了。客户端自己已经切回大厅并更新了本地状态，服务端只负责
            # 把它记进存档，这样下次登录就不会再被强制拉去教学（见 parse_ 的注释）。
            self.on_first_user_result(payload)
        elif opcode == 0x0311:
            self.log("← 回 gspRepQuestRecordInPvp（6 项空记录）")
            self.send(build_game(0x0311, build_rep_quest_record_in_pvp()))
        elif opcode == OP_END_QUEST:
            # 关卡结束（倒计时归零或通关）。客户端只把事件报上来就等着，
            # 服务端不回 0x0411 的话关卡永远停在原地不进结算页（FINDINGS §86）。
            # 绝不能回显 0x040f —— 它的服务端方向在 0x54e5ae 跳表里是未处理。
            self.send_end_game()
        elif opcode == OP_LEAVE_RESULT:
            # 客户端在结算界面上停留约 9 秒后发这个空包（会话 11 实测：0x0411
            # 之后 9 秒整）。只有结算包已经发过时才当「看完了」——服务端方向的
            # 同号 0x0405 是另一回事（`0x551d35` 读两个 int32 再调角色对象的
            # vft+0xd4），别把这里的判断放宽。
            if self.settled:
                self.leave_game_result()
            else:
                self.log("   收到 0x0405 但本局还没结算过；不动作")
        elif opcode == OP_CREATE_ITEM:
            # 客户端方向的 0x0406 = gcpCreateItem（掉落物请求，§112）。
            # ★ 回的是 **0x0404 gspCreatedItem**，绝不能回显同号 ——
            # 服务端方向的 0x0406 是死亡广播，回显等于随机杀角色（D028）。
            self.on_create_item(payload)
        elif opcode == OP_GET_ITEM:
            # 客户端方向的 0x0407 = gcpGetItem（拾取请求，§115）。
            # 回的是 **服务端方向的 0x0405**（和 rawLeaveGameResult 同号，
            # 但那是客户端方向的空包，两者只靠方向区分）。
            # 不回 = 那件掉落物作废，因为客户端已经把它标成「已上报」了。
            self.on_get_item(payload)
        elif opcode == OP_USE_ITEM:
            # 客户端方向的 0x040c = 「按 Ctrl 用道具槽第 N 格」（§194）。
            # 回两个包：0x040c 同号回显（**只给他自己**，扣掉那一格）+
            # 0x040a 广播（让全房间都算上那个效果）。
            # 不回的话玩家会觉得「捡了道具但按了没反应」。
            self.on_use_item(payload)
        elif opcode == OP_REMOVE_CHAR_ATTR:
            # 客户端方向的 0x040d = 「我身上那个效果结束了」（§200）。
            # 原样广播给房里其他人（发包的人自己不用收）——
            # 不转发的话弹数型道具（三重射击 / 致命射击 / 毒弹）的模型
            # 在别人屏幕上永远变不回去。
            self.on_remove_char_attr(payload)
        elif opcode == OP_MARK_QUEST_SUCCESS:
            # 「这一关打通了」。只记不回：服务端方向的同号 0x0417 是换图放行。
            self.on_mark_quest_success(payload)
        elif opcode == OP_REQ_CHANGE_TO_NEXT_MAP:
            # 关卡内换图。★ 服务端方向的同号包是 gspEndGame（结算），
            # 千万别回显 —— 那会在关卡中途把玩家踢进结算界面（§111）。
            self.on_req_change_to_next_map(payload)
        elif opcode == OP_MAP_LOADING_DONE:
            self.on_map_loading_done()
        elif opcode == OP_REPORT_HP_ZERO:
            self.on_report_hp_zero(payload)
        elif opcode == OP_REQ_RESPAWN:
            self.on_respawn_request(payload)
        elif opcode == OP_REPORT_HACK:
            self.on_report_hack(payload)
        elif opcode == OP_PEER_DATA_UP:
            self.on_peer_data(payload)
        elif opcode == OP_START_TCP_RELAY:
            # gcpStartTcpRelay：客户端要一条到对方的中继通道。
            # D078 起我们**真的回 0x0210** 把原版那条路接上；`--no-tcp-relay`
            # 关掉之后客户端的 [0x72e290] 一直是 NULL，同步数据自然退回
            # `0x040e` 那条 else 分支（§149）—— 那也是原版的回退路径。
            self.on_start_tcp_relay(payload)
        elif opcode == OP_UPDATE_QUEST_SCORE:
            # 客户端每次加分都发一次，载荷是**累计**分数（客户端侧 0x4a414a
            # 先加到 [ctx+0x3b4] 再发，500ms 节流）。记下来给结算用，
            # 并回一发 0x0415 —— 右上角战绩面板的「分数」列只认那个包（§109）。
            #
            # ★ 多人：**广播给全房间**。处理器 `0x4a3efe` 写的是
            #   `[GameContextQuest + 座位*4 + 0x3b8]`，按座位索引，所以别人
            #   机器上就是「那个座位的分数变了」。不广播的话战绩面板上
            #   队友那一行永远是 0，对战模式更是压根看不到对手的分。
            try:
                self.quest_score = Reader(payload).i32()
            except ValueError:
                pass
            else:
                self.battle_broadcast(
                    build_game(OP_REP_QUEST_SCORE,
                               build_rep_quest_score(self.my_seat,
                                                     self.quest_score)))
        elif opcode in (OP_PREPARE_GAME, OP_COUNT_GAME_READY, OP_LOADING_DONE):
            self.on_start_game_packet(opcode, payload)

    def on_ctrl_packet(self, payload):
        self.log(f"★ 控制包(0xFE) 载荷 {len(payload)} 字节\n{hexdump(payload)}")

    def resolve_min_client_version(self):
        """这条握手该按哪个「最低客户端版本」判。返回元组或 None（不限制）。

        * `versioning.FOLLOW_FILE`（app.py 统一入口用的哨兵）：每次握手都来
          查 `server-ClientFilter.config`（按 mtime 缓存）—— 改配置**不用
          重启服务器**，下一条连接就按新值判。
        * 元组 / None：CLI `--client-min-version` 或测试直接给的值，钉死不动。
        * 没这个属性（测试手搓的 Namespace）：不限制。
        """
        spec = getattr(self.args, "client_min_version", None)
        if spec == versioning.FOLLOW_FILE:
            min_version, warnings = versioning.load_client_filter()
            for warning in warnings:
                self.log(f"⚠ 版本门禁配置: {warning}")
            return min_version
        return spec

    def feed(self, data):
        # 原始/明文流落盘只对协议逆向有用。战斗中每个 0x0406 都要 write+flush
        # 两个文件，日常游玩纯属跟游戏抢 I/O，所以跟着 --verbose 走。
        plain = self.cin.decrypt(data)
        if VERBOSE:
            self.fb_raw.write(data)
            self.fb_raw.flush()
            self.fb_dec.write(plain)
            self.fb_dec.flush()
        self.buf += plain

        if not self.got_version:
            if len(self.buf) < 4:
                return
            ver = struct.unpack_from("<i", self.buf, 0)[0]
            del self.buf[:4]
            self.got_version = True
            self.client_version = versioning.decode_wire(ver)
            if self.client_version is not None:
                self.log(f"★★ 握手：裸发版本号 = {ver} -> 复活项目版本 "
                         f"{versioning.format_version(self.client_version)}")
            else:
                why = ("原版 311，未上报复活版本 = 旧版客户端"
                       if ver == CLIENT_VERSION else "认不出的值，按旧版处理")
                self.log(f"★★ 握手：客户端版本 = {ver} (0x{ver:x}) —— {why}")
            have = (versioning.format_version(self.client_version)
                    if self.client_version is not None else "旧版(未上报版本)")
            min_version = self.resolve_min_client_version()
            if (min_version is not None
                    and (self.client_version is None
                         or self.client_version < min_version)):
                # 版本门禁：客户端没上报版本（旧版）或低于最低要求。
                # 回非零结果码 + 提示文案，客户端走原版自带的升级/报错弹框
                # 分支（packet_api.md §1.2）；不主动断开，等它自己停。
                # 万一它没停继续发 0x0100，on_game_login 有兜底。
                self.version_rejected = True
                need = versioning.format_version(min_version)
                self.log(f"✗ 版本门禁：客户端 {have} < 最低要求 {need}；"
                         f"回 0xFE 控制帧（结果码 {VERSION_REJECT_RESULT}"
                         f" + 提示文案）")
                self.online(f"✗ 版本门禁 拒绝 ip={self.peer()} "
                            f"客户端版本={have} 最低要求={need}")
                if not self.args.hold and self.args.version_result == 0:
                    # 文案带服务器自身版本号：探针（updater\src\probe.c）
                    # 靠它知道该升到哪个版本（见 version_reject_message）。
                    self.send(build_ctrl(w_i32(VERSION_REJECT_RESULT)
                                         + w_wstr(version_reject_message())))
                return
            # ★ 上下线流水里必须能查到「这条连接跑的是哪个版本」——
            #   server.out 每次启动都被覆盖，版本号要进 online.log 才留得住
            #   （这本来就是给「拿到 log 不知道对方版本」的排查场景用的）。
            self.online(f"+ 版本上报 ip={self.peer()} 客户端版本={have}")
            if self.args.hold:
                self.log("[hold] 不回版本应答")
            else:
                res = self.args.version_result
                self.log(f"回 0xFE 控制帧，结果码 = {res}"
                         f"{'（0 = 版本通过）' if res == 0 else ''}")
                self.send(build_ctrl(w_i32(res)))

        while True:
            got = take_frame(self.buf)
            if got is None:
                if self.buf:
                    # 首字节既不是 FE 也不是 FF = 解密流对不上，立刻报出来
                    if self.buf[0] not in (MAGIC_CTRL, MAGIC_GAME):
                        self.log(f"!! 缓冲首字节 0x{self.buf[0]:02x} 不是 FE/FF —— "
                                 f"解密流可能已错位\n{hexdump(bytes(self.buf))}")
                        self.buf.clear()
                break
            kind, op, payload, n = got
            del self.buf[:n]
            # ★ 单个包的处理异常绝不能把整条连接带走：`run()` 的兜底会退出
            #   读循环（= 玩家被断线）。战斗正打到一半时，一枚坏包换一次
            #   断线太亏 —— 记下来，跳过这发，继续服务（bug调查/4 的教训）。
            try:
                if kind == "ctrl":
                    self.on_ctrl_packet(payload)
                else:
                    self.on_game_packet(op, payload)
            except OSError:
                raise                      # 发送类错误按老路走（可能拆连接）
            except Exception as error:      # noqa: BLE001 —— 单包隔离
                self.log(f"!! 处理 0x{op:04x} 时抛了 {error!r}，"
                         f"跳过这发继续服务")

    def run(self):
        self.log(f"+++ 连接来自 {self.addr[0]}:{self.addr[1]}")
        self.online(f"+ 连上游戏服 ip={self.peer()}（还没报票据）")
        # 超时只是为了让 recv 别永久阻塞（收工时线程能退），不代表连接有问题。
        self.sock.settimeout(1.0)
        try:
            while True:
                try:
                    data = self.sock.recv(8192)
                except socket.timeout:
                    continue
                if not data:
                    self.log("对端关闭")
                    break
                self.feed(data)
        except ConnectionResetError:
            self.log("被对端重置")
        except Exception as e:
            self.log(f"异常: {e!r}")
        finally:
            # 窗口里没打完的样本也要有个去处，不然短局一个数字都留不下。
            try:
                self.report_peer_timing(force=True)
            except Exception:                # 收尾路径上绝不能再抛
                pass
            unregister_conn(self)
            # 中继票据跟着游戏连接一起作废。**不去关中继 socket** ——
            # 游戏连接一断，客户端那条中继连接自己也会走掉；而主动关别人的
            # 中继连接会触发 `OnDisconnected`，那是把玩家踢出房间（§158）。
            PEER_RELAY.forget(self)
            # UDP 那条流也跟着作废。它是无连接的，不忘掉的话对端下次带着
            # 同一个源地址上来会被认成还活着的旧连接。
            udpsync.SERVER.forget(self)
            # ★ 断线也要把座位腾出来并广播，否则房间里会留一个永远不动的
            #   幽灵玩家，而且房主要是他，那个房间就再也开不了局。
            try:
                who = display_name(self.account) or (self.account_name or "?")
                self.leave_room(f"{who} 断线了。")
            except Exception as error:      # 收尾路径上绝不能再抛
                self.log(f"   离开房间时出错（忽略）: {error!r}")
            self.log("--- 连接结束")
            who = repr(self.account_name) if self.account_name else "?（没登录成功）"
            self.online(f"- 断开 账号={who} ip={self.peer()} "
                        f"在线 {eventlog.duration(time.monotonic() - self.connected_at)}")
            for f in (self.ft, self.fb_raw, self.fb_dec):
                if f is None:      # 非 verbose 时抓包文件根本没开
                    continue
                try:
                    f.close()
                except Exception:
                    pass
            try:
                self.sock.close()
            except Exception:
                pass


# ----------------------------------------------------------------------------
# 调试控制通道：一行一命令的纯文本协议
# ----------------------------------------------------------------------------
CONTROL_HELP = """命令（一行一条，大小写不敏感）：
  --user <账号>                   ★ 可加在任何命令里，指定操作哪个玩家的连接。
                                  只有一条连接时可以省略；有多条时**必须**指定，
                                  服务端不会替你猜（猜错就把包发给别人了）
  who                             列出当前所有活动连接和它们的账号
  rooms                           列出大厅里现在有哪些房间
  fakeroom [标题] [类型] [人数]   造一个**没有玩家**的假房间（默认「测试房间」/
                                  类型 2 闯关 / 1 人）。只为在单机上验证房间列表
                                  的线格式：(%d/%d) 哪个数是人数、房间号显示成几号
  delroom <房间号>                强行解散一个房间
  relay                           战斗数据三条出路各投了多少人次
                                  （UDP 旁路 / 原版中继 / 0x040f 回退）+ 中继 RTT
  status                          当前连接 / 开局状态 / 座位 / 分数 / 最后坐标
  raw <op> [payload-hex]          发任意游戏包，op 是十六进制（例：raw 0411 ...）
  endgame                         按存档真结算一局（记经验+金币再发 0x0411），
                                  和客户端打完关卡发 0x040f 走同一条路
  endgame <seat> <success> [v0..v11]
                                  发原始 0x0411，自己指定每个字段（协议试探用）。
                                  success 用 0/1，业务值不足 12 个的补 0
  endgame-probe [base]            发 0x0411，12 个业务值填 base+0..base+11
                                  （默认 101..112），用来看结算界面哪格显示什么
  gameresult [seat] [v0..v11]     发 0x0309 gspRepGameResult（结算界面的数据源）。
                                  ★ 只能在战斗中发，且必须排在 0x0411 之前
  gameresult-probe [base]         发 0x0309，12 个业务值填 base+0..base+11
                                  （默认 201..212），尾部数组填 base+100..+105，
                                  用来看结算界面哪格显示什么
  kill <handle> [seat] [deaths] [killer]
                                  发 0x0406 死亡广播，让指定角色立刻倒下。
                                  handle = 角色对象句柄（[char+0xd0]，十六进制），
                                  用 tools/probe_death.py 读得到。
                                  deaths = 下发的**新死亡次数**（默认 1），HUD 心形
                                  数 = 最大生命 - 它，所以手动推第 2 次要写 2。
                                  ★ 正常游玩不用它 —— 客户端自己会用 0x0408
                                  报上来，服务端按「报上来的值 +1」自动回
  respawn [id] [x] [y] [unk]      发 0x0419 gspRespawnCharacter；
                                  x/y 省略时用客户端 0x0406 自报的最后坐标。
                                  ★ 正常路径是回显客户端的 0x0413，这条只给试探用
  nextmap <地图名>                发 0x0417 gspRepChangeToNextMap 直接换图。
                                  ★ 正常路径是回显客户端的 0x0411（§111），
                                  这条只给试探用；地图名要写客户端认识的，
                                  写错了会加载失败卡在加载画面
  map-ready                       发 0x0418（空包）= 放行换图加载循环。
                                  正常路径由客户端的 0x0412 轮询自动触发
  drop [物件id] [x] [y] [vx] [vy] 发 0x0404 gspCreatedItem，在指定坐标凭空掉一件。
                                  物件 id 默认 10101（金币×1），可用的见
                                  gameserver.py 的 ITEM_NAMES；坐标省略时用
                                  客户端最近一次 0x0406 报的掉落点。
                                  ★ 正常路径是回显客户端的 0x0406（§112），
                                  这条只给试探用（比如试某个 id 长什么样）
  pickup <句柄> [座位]            发服务端方向的 0x0405 = 「放行拾取」，
                                  让指定座位捡起指定句柄的掉落物。
                                  ★ 正常路径是回显客户端的 0x0407（§115），
                                  这条只给试探用；句柄看日志里 gspCreatedItem
                                  那行（drop 出来的从 0x40000000 起往上数）
  back-to-room                    发 0x0403（服务端方向 = 切回 stage 5 房间）
  sync-account                    重读 accounts.json 并发 0x0600 gspRepMoney
                                  + 0x020c gspQuestReachedDifficulty
                                  + 0x030b gspSlotEquippedList，
                                  把数据栏、难度解锁和角色解锁刷成存档里的样子
  quest-difficulty [id 难度 ...]  发 0x020c 全量快照 = 「每关打到第几个难度」。
                                  不带参数就按存档发（和登录时那一发一样）；
                                  带参数就发指定的表（协议试探用），例如
                                  `quest-difficulty 3 0` 把关卡 3 锁回只剩简单。
                                  ★ 客户端只放行「已达成难度 + 1」以内的难度
  equipped [座位 物品id ...]      发 0x030b 座位物品清单 = 「人物选择里有几个
                                  头像」。不带参数就按存档发；`equipped 0`
                                  把座位 0 的清单清空，复现只有 3 个角色的
                                  原始状态。★ 要在房间里发才看得出效果，
                                  而且按钮只在进房间那一刻建一次
  help                            这段
"""


def _control_status(conn):
    position = ("未知" if conn.last_position is None
                else f"({conn.last_position[0]:.1f}, {conn.last_position[1]:.1f})")
    account = getattr(conn, "account", None)
    return (f"conn=#{conn.seq} account={conn.account_name!r} "
            f"level={player_level(account)} exp={player_experience(account)} "
            f"money={player_money(account)} "
            f"tutorial={tutorial_state(account)} "
            f"start_game={conn.start_game.state} my_seat={conn.my_seat} "
            f"quest_score={conn.quest_score} "
            f"quest_success={getattr(conn, 'quest_success', False)} "
            f"items_created={getattr(conn, 'items_created', 0)} "
            f"items_picked={getattr(conn, 'items_picked', 0)} "
            f"last_position={position} "
            f"maps_entered={getattr(conn, 'maps_entered', [])} "
            f"map_change_pending={getattr(conn, 'map_change_pending', False)} "
            f"room_type={(conn.room or {}).get('session_type')} "
            f"quest={conn.current_quest()} "
            f"quest_unlock_all={quest_unlock_all(account)} "
            f"quest_difficulty={ {qid: lv for qid, lv in sorted(quest_difficulty_records(account).items())} } "
            f"character={player_character(account)} "
            f"character_unlock_all={character_unlock_all(account)} "
            f"owned_characters={owned_characters(account)}")


def _control_ints(words, count, default=0):
    """把剩余的词按 int 解析并补齐到 count 个。支持 0x 前缀。"""
    values = [int(w, 0) for w in words]
    if len(values) > count:
        raise ValueError(f"最多 {count} 个数，给了 {len(values)} 个")
    return values + [default] * (count - len(values))


def handle_control_command(line):
    """解析并执行一条控制命令，返回要回给控制台的一行文本。

    **这个通道存在的理由**：`0x0411 gspEndGame` 只有在客户端真把关打完/打输
    时才会被 `0x040f gcpEndQuest` 触发，而闯关模式有强制推进机制
    （「15秒内没有向前移动将强制退出」，§88），挂机等 12:30 这条路走不通。
    会话 09 因此始终没能实机验证结算包。有了这个通道，就能在战斗中任意时刻
    主动把包推给客户端，把「验证一个战斗应答」的成本从「打完一整关」降到一行命令。

    参数写错（多给一个数、hex 打错）是调试时的常态，一律翻成 `err ...` 一行
    带回去 —— 控制台崩掉会连带把游戏服务端的调试线程也带走。
    """
    try:
        return _dispatch_control_command(line)
    except Exception as error:
        return f"err {error}"


def _dispatch_control_command(line):
    words = line.split()
    if not words:
        return "ok"
    # `--user <账号>` 可以出现在任何位置：V0.2 起同时可能有好几个玩家在线，
    # 不指定就只在「刚好只有一条连接」时才继续 —— 拿错连接会把包发给别人。
    username = ""
    if "--user" in words:
        index = words.index("--user")
        if index + 1 >= len(words):
            return "err --user 后面要跟账号名"
        username = words[index + 1]
        words = words[:index] + words[index + 2:]
    if not words:
        return "ok"
    cmd = words[0].lower()
    if cmd == "help":
        return CONTROL_HELP.strip()
    if cmd == "who":
        conns = all_conns()
        if not conns:
            return "ok 当前没有活动连接"
        return "ok " + "; ".join(
            f"#{c.seq} {c.account_name or '(未登录)'}" for c in conns)

    # -- 房间（里程碑 I）：这几条不需要连接，放在 pick_conn 之前 ------------
    if cmd == "rooms":
        found = LOBBY.rooms()
        if not found:
            return "ok 当前没有房间"
        return "ok " + "; ".join(r.describe() for r in found)

    if cmd == "fakeroom":
        # 造一个**没有连接**的房间，专门用来在只有一个客户端的机器上验证
        # 房间列表的线格式（`(%d/%d)` 哪个数是人数、房间号显示成几号）。
        # 座位的 conn 是 None，广播时会被 `Room.members()` 过滤掉。
        title = words[1] if len(words) > 1 else "测试房间"
        session_type = _control_ints(words[2:3], 1, 2)[0]
        players = max(1, min(ROOM_SEAT_COUNT, _control_ints(words[3:4], 1, 1)[0]))
        room = LOBBY.create_room(None, title=title, map_name="",
                                 session_type=session_type,
                                 arguments=(3, 1) if session_type == 2 else (1, 2, 3),
                                 seat=Seat(None, nickname="测试玩家1", level=9))
        for index in range(1, players):
            room.seats[index] = Seat(None, nickname=f"测试玩家{index + 1}",
                                     level=9)
        return f"ok 已建 {room.describe()}"

    if cmd == "relay":
        # 战斗数据三条出路各投了多少（UDP 旁路 / 原版中继 / 0x040f 回退）
        # + 每条中继连接的 RTT。★ `RelayServer.status()` 以前**没有任何调用者**，
        #   于是「UDP 下行到底投了多少」在服务端只能靠数日志（§225 第六节）。
        return "ok " + PEER_RELAY.status()

    if cmd == "delroom":
        if len(words) < 2:
            return "err 用法: delroom <房间号>"
        room = LOBBY.close_room(int(words[1]))
        if room is None:
            return f"err 没有房间 #{words[1]}"
        return f"ok 已删房间 #{room.room_id}"

    conn = pick_conn(username)
    if conn is None:
        if username:
            return f"err 账号 {username!r} 当前没有活动连接（用 who 看在线的）"
        if all_conns():
            return ("err 当前有多条活动连接，请用 --user <账号> 指定操作谁"
                    "（用 who 看在线的）")
        return "err 当前没有活动连接"

    if cmd == "status":
        return "ok " + _control_status(conn)

    if cmd == "raw":
        if len(words) < 2:
            return "err 用法: raw <op-hex> [payload-hex]"
        opcode = int(words[1], 16)
        payload = bytes.fromhex("".join(words[2:]))
        conn.log(f"[ctl] ← 手动发 opcode=0x{opcode:04x} "
                 f"payload={payload.hex() or '<empty>'}")
        conn.send(build_game(opcode, payload))
        return f"ok 已发 0x{opcode:04x} ({len(payload)} 字节载荷)"

    if cmd == "endgame" and len(words) == 1:
        # 不带参数 = 走真正的结算路径（记账 + 按存档下发），和客户端自己
        # 打完关卡发 0x040f 时完全一样。带参数的形式是给协议试探用的。
        conn.log("[ctl] ← 手动触发结算（与 0x040f 同一条路径）")
        conn.send_end_game()
        return "ok 已按存档结算并发 0x0411"

    if cmd in ("endgame", "endgame-probe"):
        if cmd == "endgame-probe":
            base = int(words[1], 0) if len(words) > 1 else 101
            seat, success = conn.my_seat, 1
            values = [base + i for i in range(END_GAME_VALUE_COUNT)]
        else:
            seat = int(words[1], 0) if len(words) > 1 else conn.my_seat
            success = int(words[2], 0) if len(words) > 2 else 1
            values = _control_ints(words[3:], END_GAME_VALUE_COUNT)
        conn.log(f"[ctl] ← 手动发 gspEndGame(seat={seat}, "
                 f"success={bool(success)}, values={values})")
        conn.send(build_game(OP_END_GAME,
                             build_end_game(seat, bool(success), values)))
        return f"ok 已发 0x0411 seat={seat} success={bool(success)} values={values}"

    if cmd in ("gameresult", "gameresult-probe"):
        if cmd == "gameresult-probe":
            base = int(words[1], 0) if len(words) > 1 else 201
            seat = conn.my_seat
            values = [base + i for i in range(GAME_RESULT_VALUE_COUNT)]
            tail = [base + 100 + i for i in range(GAME_RESULT_TAIL_COUNT)]
        else:
            seat = int(words[1], 0) if len(words) > 1 else conn.my_seat
            values = _control_ints(words[2:], GAME_RESULT_VALUE_COUNT)
            tail = None
        conn.log(f"[ctl] ← 手动发 gspRepGameResult(seat={seat}, values={values}, "
                 f"tail={tail})")
        conn.send(build_game(OP_REP_GAME_RESULT,
                             build_rep_game_result(seat, values, tail)))
        return f"ok 已发 0x0309 seat={seat} values={values} tail={tail}"

    if cmd == "kill":
        if len(words) < 2:
            return "err 用法: kill <handle-hex> [seat] [deaths] [killer]"
        handle = int(words[1], 16)
        seat = int(words[2], 0) if len(words) > 2 else conn.my_seat
        # 死亡次数是权威值，手动推的时候要自己数（正常路径由 0x0408 的值 +1 得到）。
        deaths = int(words[3], 0) if len(words) > 3 else 1
        killer = int(words[4], 0) if len(words) > 4 else 0xFF
        conn.log(f"[ctl] ← 手动发 0x0406 死亡广播(handle=0x{handle:08x}, "
                 f"seat={seat}, 死亡次数={deaths}, 凶手={killer})")
        conn.send(build_game(OP_BROADCAST_DEATH,
                             build_broadcast_death(handle, seat, killer, deaths)))
        return (f"ok 已发 0x0406 死亡广播 handle=0x{handle:08x} "
                f"seat={seat} 死亡次数={deaths}")

    if cmd == "respawn":
        character_id = int(words[1], 0) if len(words) > 1 else conn.my_seat
        if len(words) > 3:
            x, y = int(words[2], 0), int(words[3], 0)
        else:
            x, y = conn.respawn_position()
        unknown = int(words[4], 0) if len(words) > 4 else 0
        conn.log(f"[ctl] ← 手动发 gspRespawnCharacter(id={character_id}, "
                 f"x={x}, y={y}, unk={unknown})")
        conn.send(build_game(OP_RESPAWN_CHARACTER,
                             build_respawn_character(character_id, x, y, unknown)))
        return f"ok 已发 0x0419 id={character_id} x={x} y={y}"

    if cmd == "nextmap":
        if len(words) < 2:
            return "err 用法: nextmap <地图名>"
        map_name = words[1]
        conn.log(f"[ctl] ← 手动发 gspRepChangeToNextMap(0x0417) 地图={map_name!r}")
        # ★ 走和真客户端同一份房间级状态（`RoomQuest`），不然手动推的换图
        #   和 `0x0412` 那条等人链会各说各话。
        conn.quest_state().begin_map_change(map_name)
        conn.send(build_game(OP_REP_CHANGE_TO_NEXT_MAP,
                             build_rep_change_to_next_map(map_name)))
        return f"ok 已发 0x0417 地图={map_name}"

    if cmd == "map-ready":
        conn.log("[ctl] ← 手动发 0x0418（放行换图加载循环）")
        conn.quest_state().finish_map_change()
        conn.send(build_game(OP_MAP_CHANGE_READY))
        return "ok 已发 0x0418"

    if cmd == "drop":
        item_id = int(words[1], 0) if len(words) > 1 else 10101
        if len(words) > 3:
            x, y = float(words[2]), float(words[3])
        elif conn.last_position is not None:
            x, y = conn.last_position
        else:
            return "err 客户端还没报过任何掉落点；请写全坐标: drop <id> <x> <y>"
        vx = float(words[4]) if len(words) > 4 else 0.0
        vy = float(words[5]) if len(words) > 5 else 0.0
        # 后三个字段照客户端自己发的填（3 / -1 / -1）：3 不是「宠物掉落」，
        # -1 不是任何座位，正好让处理器跳过那段音效/特效分支。
        fields = (item_id, x, y, vx, vy, 3, -1, -1)
        handle = conn.quest_state().allocate_item()
        name = ITEM_NAMES.get(item_id, "未知物件")
        conn.log(f"[ctl] ← 手动发 gspCreatedItem(0x0404) 句柄=0x{handle:08x} "
                 f"物件={item_id} {name} @ ({x:.0f}, {y:.0f})")
        conn.send(build_game(OP_CREATED_ITEM, build_created_item(handle, fields)))
        return f"ok 已发 0x0404 物件={item_id} {name} @ ({x:.0f}, {y:.0f})"

    if cmd == "pickup":
        if len(words) < 2:
            return "err 用法: pickup <句柄> [座位]"
        handle = int(words[1], 0)
        seat_id = int(words[2], 0) if len(words) > 2 else conn.my_seat
        conn.log(f"[ctl] ← 手动发拾取放行(0x0405) 座位={seat_id} "
                 f"句柄=0x{handle & 0xffffffff:08x}")
        conn.send(build_game(OP_PICKED_ITEM, build_picked_item(seat_id, handle)))
        return f"ok 已发 0x0405 座位={seat_id} 句柄=0x{handle & 0xffffffff:08x}"

    if cmd == "back-to-room":
        # 0x0403 的服务端方向处理器 0x5518fb 是
        #   mov [LobbyStage+4], 2 / push 5 / call 0x40e47f  = 切回 stage 5
        # 也就是看完结算之后回房间那一步（§87 末）。
        conn.log("[ctl] ← 手动发 0x0403（服务端方向 = 切回 stage 5 房间）")
        conn.send(build_game(OP_LOADING_DONE, b""))
        return "ok 已发 0x0403"

    if cmd == "quest-difficulty":
        if len(words) == 1:
            conn.reload_account()
            conn.send_quest_reached_difficulty(reason="（ctl）")
            return "ok 已按存档发 0x020c " + _control_status(conn)
        numbers = [int(word, 0) for word in words[1:]]
        if len(numbers) % 2:
            return "err 用法: quest-difficulty [关卡id 难度 ...]（成对给）"
        records = dict(zip(numbers[0::2], numbers[1::2]))
        conn.log(f"[ctl] ← 手动发 0x020c gspQuestReachedDifficulty({records})")
        conn.send(build_game(OP_QUEST_REACHED_DIFFICULTY,
                             build_quest_reached_difficulty(records)))
        return f"ok 已发 0x020c {records}"

    if cmd == "equipped":
        # 不带参数 = 按存档重发；带参数 = 发指定的物品 id 表（协议试探用），
        # 例如 `equipped 0` 把清单清空，复现「只有 3 个角色」的原始状态。
        seat = conn.my_seat
        if len(words) == 1:
            conn.reload_account()
            conn.send_slot_equipped_list(seat, reason="（ctl）")
            return "ok 已按存档发 0x030b " + _control_status(conn)
        seat = int(words[1], 0)
        item_ids = [int(word, 0) for word in words[2:]]
        conn.log(f"[ctl] ← 手动发 0x030b gspSlotEquippedList(座位={seat}, "
                 f"{len(item_ids)} 件: {item_ids})")
        try:
            payload = build_slot_equipped_list(seat, item_ids)
        except ValueError as error:
            return f"err {error}"
        conn.send(build_game(OP_SLOT_EQUIPPED_LIST, payload))
        return f"ok 已发 0x030b 座位={seat} {len(item_ids)} 件 {item_ids}"

    if cmd == "sync-account":
        # 直接改了 accounts.json 之后不用重登游戏，一条命令就能让画面跟上。
        conn.reload_account()
        conn.send_rep_money(reason="（sync-account）")
        conn.send_quest_reached_difficulty(reason="（sync-account）")
        conn.send_slot_equipped_list(reason="（sync-account）")
        return "ok 已重读存档并发 0x0600 + 0x020c + 0x030b " + _control_status(conn)

    return f"err 未知命令 {cmd!r}；用 help 看命令表"


def serve_control(port):
    """控制通道监听线程。一行一命令，一行一应答，处理完就断。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    except OSError as error:
        print(f"[{ts()}] !! 控制端口 {port} 绑定失败: {error}", flush=True)
        return
    s.listen(4)
    print(f"[{ts()}] [gameserver] 控制通道监听 127.0.0.1:{port}", flush=True)
    while True:
        sock, _ = s.accept()
        try:
            sock.settimeout(5.0)
            line = sock.makefile("r", encoding="utf-8").readline().strip()
            reply = handle_control_command(line)
            print(f"[{ts()}] [ctl] {line!r} -> {reply.splitlines()[0]}", flush=True)
            sock.sendall((reply + "\n").encode("utf-8"))
        except Exception as error:
            print(f"[{ts()}] [ctl] 控制连接异常: {error!r}", flush=True)
        finally:
            try:
                sock.close()
            except Exception:
                pass


def _parse_min_version_arg(text):
    """``--client-min-version`` 的取值：版本号文本 -> 元组；0 -> None（不限制）。"""
    version = versioning.parse_version_text(text)
    if version is None:
        raise argparse.ArgumentTypeError(
            f"认不出版本号 {text!r}（要形如 0.2.7 / v0.2.7）")
    return None if version == (0, 0, 0) else version


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=27799)
    ap.add_argument("--host", default="::", help="监听地址，默认 :: （双栈）")
    ap.add_argument("--hold", action="store_true", help="连版本应答都不回，纯抓包")
    ap.add_argument("--version-result", type=int, default=0,
                    help="0xFE 控制帧里的结果码，0 = 版本通过")
    ap.add_argument("--client-min-version", default=None, metavar="版本",
                    type=_parse_min_version_arg,
                    help="允许的最低客户端版本（如 0.2.7 / v0.2.7），低于它的"
                         "连接按「版本过旧」拒绝；0 或不填 = 不限制。"
                         "app.py 统一入口不用这个参数 —— 它让每条连接直接热重载"
                         " config\\server-ClientFilter.config")
    ap.add_argument("--hold-lobby", action="store_true",
                    help="握手照回，但游戏包一律不应答（纯抓包）")
    ap.add_argument("--login-result", type=int, default=0,
                    help="gspRepLogin 的结果码，0 = 成功")
    ap.add_argument("--accounts", default=None,
                    help="账号 JSON 路径（默认 server/data/accounts.json）")
    ap.add_argument("--no-death-reply", action="store_true",
                    help="收到 0x0408 也不回死亡广播（回到会话 14 及以前的行为，"
                         "角色血量归零后不死不重生）。只在对比排查时用。")
    ap.add_argument("--respawn-watchdog", type=float, default=None, metavar="秒",
                    help="死了多少秒还没等到客户端的 0x0413，服务端就自己补一发 "
                         "0x0419 把人拉起来（bug调查/8「死了不复活」的兜底）。"
                         f"默认 {RESPAWN_WATCHDOG_S:.0f} 秒；**0 = 关掉兜底**。"
                         "调大或关掉是为了留出取证窗口 —— 兜底一开，卡住的人 8 秒"
                         "就被捞起来了，来不及在他那台跑 probe-death.bat。")
    ap.add_argument("--room-burst-delay", type=int, default=0, metavar="毫秒",
                    help="建房/回房间的那一串包不合并，并且每个之间等这么久"
                         "（回到会话 21 及以前的行为）。用来**复现**「进房间只剩"
                         "3 个角色」：客户端只要在这个缝里 recv 一次，房间就用"
                         "空清单建 UI。0 = 合并成一次发送（默认，§120）。")
    ap.add_argument("--control-port", type=int, default=CONTROL_PORT,
                    help="调试控制通道端口（tools/gs_ctl.py 连它）；0 = 关闭")
    ap.add_argument("--verbose", action="store_true",
                    help="逐包 hexdump + 字段试解 + 抓包落盘 + 不静音高频战斗包。"
                         "协议逆向时开；日常游玩别开（会跟游戏抢 I/O）")
    args = ap.parse_args()

    global VERBOSE
    VERBOSE = args.verbose

    if args.control_port:
        threading.Thread(target=serve_control, args=(args.control_port,),
                         daemon=True).start()

    print(f"[{ts()}] [gameserver] 监听 {describe_listen(args.host, args.port)} "
          f"{'(hold)' if args.hold else f'version_result={args.version_result}'} "
          f"日志={'详细（逐包 dump）' if VERBOSE else '精简（--verbose 开全量）'}",
          flush=True)
    try:
        serve(args.port, args, host=args.host)
    except OSError as e:
        print(f"!! 端口 {args.port} 绑定失败（旧进程没退？）: {e}", flush=True)


def listen(port, host="::"):
    """建一个监听 socket。默认 `::` 双栈，IPv4 和 IPv6 都能连进来（D063）。"""
    return create_listener(host, port)


def serve(port, args, accounts=None, tickets=None, host="::", ready=None):
    """在 `port` 上接受游戏连接（阻塞）。`app.py` 会把它丢进一个线程。"""
    s = listen(port, host)
    if ready is not None:
        ready.set()
    while True:
        conn, addr = s.accept()
        # 关 Nagle。`0x040f`（同步数据的回退路径）走的就是这条连接，见 D104。
        tune_stream(conn)
        threading.Thread(
            target=Conn(conn, addr, args, accounts, tickets).run,
            daemon=True).start()


if __name__ == "__main__":
    main()
